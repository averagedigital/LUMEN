import json
from pathlib import Path

from .storage import Store


def build_review(run, limit=1000):
    store = Store(run)
    rows = store.db.execute('''SELECT record FROM (
        SELECT record, row_number() OVER (PARTITION BY json_extract(record,'$.operator') ORDER BY id) AS rank
        FROM cases) ORDER BY rank LIMIT ?''', (limit,))
    records = [json.loads(row[0]) for row in rows]
    payload = json.dumps(records, ensure_ascii=False).replace('<', '\\u003c')
    root = Path(run)
    template = '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>LUMEN review</title><style>body{margin:24px;font:16px system-ui;background:#111827;color:#f8fafc}button,select{font:inherit;padding:8px;margin:4px}main{display:grid;grid-template-columns:1fr 1fr;gap:20px}.frame{position:relative}img{display:block;width:100%}svg{position:absolute;inset:0;width:100%;height:100%;pointer-events:none}pre{white-space:pre-wrap;overflow-wrap:anywhere}#mask{max-width:500px}@media(max-width:800px){main{grid-template-columns:1fr}}</style>
<h1>LUMEN review</h1><p id="status" aria-live="polite"></p>
<nav><button id="prev">Previous</button><button id="next">Next</button><button data-decision="accept">Accept</button><button data-decision="reject">Reject</button><button data-decision="needs_review">Needs review</button><button id="relabel">Relabel</button><button id="save">Download decisions</button></nav>
<main><section><h2>Reference</h2><img id="before" alt="Reference interface"></section><section><h2>Current</h2><div class="frame"><img id="after" alt="Current interface"><svg id="boxes"></svg></div></section></main>
<h2>Change mask</h2><img id="mask" alt="Changed pixels"><h2>Issue crop</h2><canvas id="crop"></canvas><h2>Evidence</h2><pre id="meta"></pre>
<script>const records=__DATA__;let index=0;const key='lumen-review-'+(records[0]?.case_id||'empty');let decisions=JSON.parse(localStorage.getItem(key)||'{}');
const asset=h=>'objects/'+h.slice(0,2)+'/'+h+'.png';const get=id=>document.getElementById(id);
function render(){const r=records[index];if(!r){get('status').textContent='No accepted cases';return}get('status').textContent=(index+1)+' / '+records.length+' · '+r.operator+' · '+(decisions[r.case_id]?.decision||'unreviewed');get('before').src=asset(r.assets.reference);get('after').src=asset(r.assets.current);get('mask').src=asset(r.assets.raw_change_mask);get('boxes').setAttribute('viewBox','0 0 '+r.capture.image_px.join(' '));get('boxes').replaceChildren();for(const issue of r.issues){const b=issue.bbox_xyxy_px;const rect=document.createElementNS('http://www.w3.org/2000/svg','rect');for(const [name,value] of Object.entries({x:b[0],y:b[1],width:b[2]-b[0],height:b[3]-b[1],fill:'none',stroke:'#f43f5e','stroke-width':2}))rect.setAttribute(name,value);get('boxes').append(rect)}get('meta').textContent=JSON.stringify({case_id:r.case_id,source:r.source_project_id,verification:r.verification,issues:r.issues},null,2);get('after').onload=()=>{const b=r.issues[0]?.context_bbox_xyxy_px||[0,0,Math.min(400,r.capture.image_px[0]),Math.min(200,r.capture.image_px[1])];const c=get('crop');c.width=b[2]-b[0];c.height=b[3]-b[1];c.getContext('2d').drawImage(get('after'),b[0],b[1],c.width,c.height,0,0,c.width,c.height)}}
function record(decision,labels=null){if(!records[index])return;decisions[records[index].case_id]={schema_version:'lumen.review.v1',case_id:records[index].case_id,decision,labels,reviewed_at:new Date().toISOString()};localStorage.setItem(key,JSON.stringify(decisions));render()}
get('prev').onclick=()=>{index=Math.max(0,index-1);render()};get('next').onclick=()=>{index=Math.min(records.length-1,index+1);render()};document.querySelectorAll('[data-decision]').forEach(b=>b.onclick=()=>record(b.dataset.decision));get('relabel').onclick=()=>{const value=prompt('Replacement issue annotations (JSON array)');if(value===null)return;try{const labels=JSON.parse(value);if(!Array.isArray(labels))throw Error('Expected array');record('relabel',labels)}catch(e){alert(e.message)}};get('save').onclick=()=>{const url=URL.createObjectURL(new Blob([Object.values(decisions).map(x=>JSON.stringify(x)).join('\\n')+'\\n'],{type:'application/x-ndjson'}));const a=document.createElement('a');a.href=url;a.download='review-decisions.jsonl';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)};render();</script></html>'''
    (root / 'review.html').write_text(template.replace('__DATA__', payload))
    return {'path': str(root / 'review.html'), 'cases': len(records), 'human_verified': 0}
