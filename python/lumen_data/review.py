import json
from pathlib import Path

from .storage import Store
from .resource_guard import guard


MAX_REVIEW_BYTES = 32 * 2**20
REVIEW_FIELDS = ('case_id', 'operator', 'source_project_id', 'structural_family_id',
                 'has_single_frame_defect', 'capture', 'issues', 'assets', 'verification', 'scenario_tags')
REVIEW_PROJECTION = 'json_object(' + ','.join(
    f"'{field}',json_extract(record,'$.{field}')" for field in REVIEW_FIELDS) + ')'


def review_records(run, limit):
    store = Store(run)
    try:
        db = store.db
        db.execute('PRAGMA temp_store=FILE')
        db.execute('BEGIN')
        db.execute('CREATE TEMP TABLE members(case_id TEXT, dimension TEXT, value TEXT)')
        dimensions = ('operator', 'source', 'family', 'polarity', 'viewport', 'locale', 'bbox', 'issues', 'scenario')
        for index, (identity, raw) in enumerate(db.execute(f'SELECT id,{REVIEW_PROJECTION} FROM cases ORDER BY id')):
            if index % 100 == 0:
                guard(store.root)
            if len(raw.encode()) > MAX_REVIEW_BYTES:
                raise ValueError('review payload budget exceeded')
            record = json.loads(raw)
            width, height = record['capture']['image_px']
            areas = [(b[2] - b[0]) * (b[3] - b[1]) / (width * height)
                     for b in (issue['bbox_xyxy_px'] for issue in record['issues'])]
            smallest = min(areas) if areas else None
            size = ('none' if smallest is None else 'tiny' if smallest < .001 else
                    'small' if smallest < .01 else 'medium' if smallest < .1 else 'large')
            values = (record['operator'], record['source_project_id'], record['structural_family_id'],
                      'positive' if record['has_single_frame_defect'] else 'negative',
                      f'{width}x{height}', record['capture']['locale'], size,
                      'multiple' if len(areas) > 1 else 'single' if areas else 'none')
            db.executemany('INSERT INTO members VALUES (?,?,?)',
                           ((identity, dimension, str(value)) for dimension, value in zip(dimensions, values)))
            db.executemany('INSERT INTO members VALUES (?,?,?)',
                           ((identity, 'scenario', tag) for tag in sorted(set(record.get('scenario_tags') or []))))
        db.execute('CREATE INDEX member_value ON members(dimension,value,case_id)')
        db.execute('CREATE INDEX member_case ON members(case_id)')
        db.execute('''CREATE TEMP TABLE strata AS SELECT dimension,value,count(*) AS population,
                      0 AS covered FROM members GROUP BY dimension,value''')
        db.execute('CREATE UNIQUE INDEX stratum ON strata(dimension,value)')
        db.execute('CREATE TEMP TABLE chosen(id TEXT PRIMARY KEY, ordinal INTEGER)')
        count = 0
        while count < limit:
            added = False
            for dimension in dimensions:
                row = db.execute('''SELECT m.case_id FROM strata s JOIN members m
                    ON m.dimension=s.dimension AND m.value=s.value
                    WHERE s.dimension=? AND s.covered=0
                    ORDER BY s.population,s.value,m.case_id LIMIT 1''', (dimension,)).fetchone()
                if row is None:
                    continue
                db.execute('INSERT INTO chosen VALUES (?,?)', (row[0], count))
                db.execute('''UPDATE strata SET covered=1 WHERE (dimension,value) IN
                           (SELECT dimension,value FROM members WHERE case_id=?)''', row)
                count += 1
                added = True
                if count == limit:
                    break
            if not added:
                break
        rows = db.execute('''SELECT id FROM (
            SELECT id,row_number() OVER (PARTITION BY json_extract(record,'$.operator') ORDER BY id) AS rank
            FROM cases WHERE id NOT IN (SELECT id FROM chosen)) ORDER BY rank,id LIMIT ?''', (limit - count,))
        for (identity,) in rows:
            db.execute('INSERT INTO chosen VALUES (?,?)', (identity, count))
            count += 1
        coverage = {dimension: {'available': available, 'selected': covered, 'uncovered': available - covered}
                    for dimension, available, covered in db.execute(
                        'SELECT dimension,count(*),sum(covered) FROM strata GROUP BY dimension')}
        records, payload_bytes = [], 2
        for (raw,) in db.execute(
                f'SELECT {REVIEW_PROJECTION} FROM cases JOIN chosen ON cases.id=chosen.id ORDER BY chosen.ordinal'):
            if len(raw.encode()) > MAX_REVIEW_BYTES - payload_bytes:
                raise ValueError('review payload budget exceeded')
            record = json.loads(raw)
            payload_bytes += len(json.dumps(record, ensure_ascii=False).replace('<', '\\u003c').encode())
            payload_bytes += 2 if records else 0
            if payload_bytes > MAX_REVIEW_BYTES:
                raise ValueError('review payload budget exceeded')
            records.append(record)
        return records, coverage
    finally:
        store.db.close()


def build_review(run, limit=1000):
    if type(limit) is not int or not 500 <= limit <= 1000:
        raise ValueError('review limit must be between 500 and 1000')
    records, coverage = review_records(run, limit)
    payload = json.dumps(records, ensure_ascii=False).replace('<', '\\u003c')
    root = Path(run)
    template = '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>LUMEN review</title><style>body{margin:24px;font:16px system-ui;background:#111827;color:#f8fafc}button,select{font:inherit;padding:8px;margin:4px}main{display:grid;grid-template-columns:1fr 1fr;gap:20px}.frame{position:relative}img{display:block;width:100%}svg{position:absolute;inset:0;width:100%;height:100%;pointer-events:none}pre{white-space:pre-wrap;overflow-wrap:anywhere}#mask{max-width:500px}@media(max-width:800px){main{grid-template-columns:1fr}}</style>
<h1>LUMEN review</h1><h2 id="defect"></h2><p id="status" aria-live="polite"></p>
<nav><label>Case <input id="jump" type="number" min="1" value="1"></label><button id="prev">Previous</button><button id="next">Next</button><button data-decision="accept">Accept</button><button data-decision="reject">Reject</button><button data-decision="needs_review">Needs review</button><button id="relabel">Relabel</button><button id="save">Download decisions</button></nav>
<main><section><h2>Reference</h2><img id="before" alt="Reference interface"></section><section><h2>Current</h2><div class="frame"><img id="after" alt="Current interface"><svg id="boxes"></svg></div></section></main>
<p id="errors" role="alert"></p><h2>Change mask</h2><img id="mask" alt="Changed pixels"><h2>Issue crop</h2><div style="overflow:auto"><canvas id="crop" style="image-rendering:pixelated"></canvas></div><h2>Evidence</h2><pre id="meta"></pre>
<script>const records=__DATA__;let index=0;let generation=0;let ready=false;const key='lumen-review-'+(records[0]?.case_id||'empty');let decisions=JSON.parse(localStorage.getItem(key)||'{}');
const asset=h=>'objects/'+h.slice(0,2)+'/'+h+'.png';const get=id=>document.getElementById(id);
async function render(){
 const token=++generation,r=records[index];ready=false;
 const controls=document.querySelectorAll('[data-decision],#relabel');controls.forEach(b=>b.disabled=true);
 const c=get('crop');c.width=0;c.height=0;c.style.width='0px';
 for(const id of ['before','after','mask'])get(id).removeAttribute('src');
 get('boxes').replaceChildren();get('errors').textContent='';
 if(!r){get('status').textContent='No accepted cases';return}
 const status=(index+1)+' / '+records.length+' · '+r.operator+' · '+(decisions[r.case_id]?.decision||'unreviewed');
 get('status').textContent=status+' · loading';
 get('defect').textContent=r.issues.map(i=>i.symptom).join(', ')||(r.scenario_tags||[]).join(', ')||r.operator;
 get('jump').value=index+1;get('jump').max=records.length;
 get('boxes').setAttribute('viewBox','0 0 '+r.capture.image_px.join(' '));
 for(const issue of r.issues){const b=issue.bbox_xyxy_px;const rect=document.createElementNS('http://www.w3.org/2000/svg','rect');for(const [name,value] of Object.entries({x:b[0],y:b[1],width:b[2]-b[0],height:b[3]-b[1],fill:'none',stroke:'#f43f5e','stroke-width':2}))rect.setAttribute(name,value);get('boxes').append(rect)}
 get('meta').textContent=JSON.stringify({case_id:r.case_id,source:r.source_project_id,scenario_tags:r.scenario_tags||[],verification:r.verification,issues:r.issues},null,2);
 try{
  const sources=[['before',r.assets.reference],['after',r.assets.current],['mask',r.assets.raw_change_mask]];
  const loaded=await Promise.all(sources.map(([id,hash])=>new Promise((resolve,reject)=>{const image=new Image();image.onload=()=>resolve([id,image.src]);image.onerror=()=>reject(Error(id+' image failed to load'));image.src=asset(hash)})));
  if(token!==generation)return;
  for(const [id,src] of loaded)get(id).src=src;
  await Promise.all(loaded.map(([id])=>get(id).decode()));
  if(token!==generation)return;
  const b=r.issues[0]?.context_bbox_xyxy_px||[0,0,Math.min(400,r.capture.image_px[0]),Math.min(200,r.capture.image_px[1])];
  c.width=b[2]-b[0];c.height=b[3]-b[1];c.style.width=(c.width*2)+'px';
  c.getContext('2d').drawImage(get('after'),b[0],b[1],c.width,c.height,0,0,c.width,c.height);
  ready=true;controls.forEach(b=>b.disabled=false);get('status').textContent=status;
 }catch(error){if(token===generation){get('errors').textContent=error.message;get('status').textContent=status+' · unavailable'}}
}
function record(decision,labels=null){if(!ready||!records[index])return;decisions[records[index].case_id]={schema_version:'lumen.review.v1',case_id:records[index].case_id,decision,labels,reviewed_at:new Date().toISOString()};localStorage.setItem(key,JSON.stringify(decisions));render()}
get('jump').onchange=()=>{const n=Number(get('jump').value);if(Number.isInteger(n)&&n>=1&&n<=records.length){index=n-1;render()}};get('prev').onclick=()=>{index=Math.max(0,index-1);render()};get('next').onclick=()=>{index=Math.min(records.length-1,index+1);render()};document.querySelectorAll('[data-decision]').forEach(b=>b.onclick=()=>record(b.dataset.decision));get('relabel').onclick=()=>{if(!ready)return;const value=prompt('Replacement issue annotations (JSON array)');if(value===null)return;try{const labels=JSON.parse(value);if(!Array.isArray(labels))throw Error('Expected array');record('relabel',labels)}catch(e){alert(e.message)}};get('save').onclick=()=>{const url=URL.createObjectURL(new Blob([Object.values(decisions).map(x=>JSON.stringify(x)).join('\\n')+'\\n'],{type:'application/x-ndjson'}));const a=document.createElement('a');a.href=url;a.download='review-decisions.jsonl';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)};render();</script></html>'''
    (root / 'review.html').write_text(template.replace('__DATA__', payload))
    return {'path': str(root / 'review.html'), 'cases': len(records), 'human_verified': 0, 'coverage': coverage}
