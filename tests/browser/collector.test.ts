import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp, readFile, writeFile, readdir, rm, mkdir} from 'node:fs/promises';
import {createHash} from 'node:crypto';
import {createServer} from 'node:https';
import {execFile, execFileSync} from 'node:child_process';
import {promisify} from 'node:util';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {routesFor, validateCollectorConfig} from '../../browser/collector.ts';

const seed={id:'demo',url:'https://example.invalid/app',scope:'/app',family:'dashboard',
  allowed_hosts:['example.invalid'],license:'MIT'};
const config={seeds:[seed],max_pages_per_project:3,max_depth:1,concurrency:2,
  requests_per_minute:60,domain_delay_seconds:1};

test('collector follows only bounded same-origin routes without queries or fragments',()=>{
  assert.deepEqual(routesFor(seed,[seed.url,seed.url+'/settings',seed.url+'/settings',
    seed.url+'-other',seed.url+'?page=2',seed.url+'#menu','http://example.invalid/app',
    'https://other.invalid/app','javascript:void(0)',seed.url+'/logout']),
    [seed.url+'/settings']);
});

test('collector requires explicit scope, hosts and finite crawl budgets',()=>{
  validateCollectorConfig(config);
  for(const change of [{max_pages_per_project:0},{concurrency:20},{max_depth:-1},
    {seeds:[{...seed,allowed_hosts:['other.invalid']}]},
    {seeds:[{...seed,scope:'/outside'}]}, {seeds:[{...seed,url:'https://u:p@example.invalid/app'}]}]){
    assert.throws(()=>validateCollectorConfig({...config,...change}));
  }
});

test('collector captures JS, obeys robots, isolates contexts, bounds routes and resumes',async()=>{
  const root=await mkdtemp(join(tmpdir(),'lumen-collector-'));
  execFileSync('openssl',['req','-x509','-newkey','rsa:2048','-nodes','-keyout',join(root,'key.pem'),
    '-out',join(root,'cert.pem'),'-days','1','-subj','/CN=localhost','-addext','subjectAltName=DNS:localhost'],{stdio:'ignore'});
  const hits=new Map<string,number>();let robotsStatus=200;
  const faultOutput=join(root,'fault-output');let collide=true;let collision='';
  const style='<style>body{margin:24px;font:20px Arial;background:white;color:#17212b}a{display:block}</style>';
  const server=createServer({key:await readFile(join(root,'key.pem')),cert:await readFile(join(root,'cert.pem'))},(req,res)=>{
    const path=req.url!;hits.set(path,(hits.get(path)??0)+1);
    if(path==='/robots.txt'){res.statusCode=robotsStatus;res.setHeader('content-type','text/plain');return res.end('User-agent: *\nDisallow: /app/0-blocked\n');}
    res.setHeader('content-type','text/html');
    if(path==='/queue'){
      const respond=()=>res.end(style+'<h1>Queue dashboard</h1><a href="/queue/child">Child</a>');
      if(collide){void mkdir(collision).then(respond,e=>res.destroy(e));return;}
      respond();return;
    }
    if(path==='/app')return res.end(style+'<h1 id="title"></h1><script>document.cookie="marker=first";document.getElementById("title").textContent="JS dashboard"</script>'+['0-blocked','settings','tables','extra'].map(p=>`<a href="/app/${p}">${p}</a>`).join('')+'<a href="/outside">Outside</a><a href="/app?token=private">Query</a>');
    if(path==='/other')return res.end(style+'<h1 id="title"></h1><script>setTimeout(()=>{document.getElementById("title").textContent=document.cookie.includes("marker=first")?"context leak":"Isolated dashboard"},200)</script>');
    if(path==='/fail'){res.statusCode=503;return res.end('unavailable');}
    if(path==='/live')return res.end(style+'<style>.label{display:block;width:max-content;white-space:nowrap}</style><h1>Live dashboard</h1><canvas width="80" height="80"></canvas><span class="label">Save account settings</span><script>const c=document.querySelector("canvas").getContext("2d");c.fillStyle="red";c.fillRect(0,0,80,80)</script>');
    res.end(style+'<h1>Settings</h1>');
  });
  await new Promise<void>(resolve=>server.listen(0,'127.0.0.1',resolve));
  const address=server.address();assert.ok(address&&typeof address==='object');
  const origin=`https://localhost:${address.port}`;
  const output=join(root,'output');const path=join(root,'config.json');
  await writeFile(path,JSON.stringify({...config,domain_delay_seconds:0,requests_per_minute:600,
    seeds:[{...seed,url:origin+'/app',allowed_hosts:[`localhost:${address.port}`]},
      {...seed,id:'other',url:origin+'/other',scope:'/other',allowed_hosts:[`localhost:${address.port}`]},
      {...seed,id:'failure',url:origin+'/fail',scope:'/fail',allowed_hosts:[`localhost:${address.port}`]}]}));
  const run=()=>promisify(execFile)('node',['--experimental-strip-types','browser/collector.ts',path,output],
    {env:{...process.env,CURL_CA_BUNDLE:join(root,'cert.pem'),NODE_EXTRA_CA_CERTS:join(root,'cert.pem')},timeout:120000,maxBuffer:2**20});
  try{
    await run();
    const manifest=await readFile(join(output,'manifest.jsonl'),'utf8');
    const rows=manifest.trim().split('\n').map(line=>JSON.parse(line));
    assert.ok(rows.some(r=>r.html.includes('JS dashboard')));
    assert.ok(rows.some(r=>r.html.includes('Isolated dashboard')));
    assert.ok(rows.every(r=>!r.html.includes('context leak')));
    const project=rows.filter(r=>r.source_project_id==='demo');
    assert.ok(project.length>=2&&project.length<=3);
    assert.equal(new Set(project.map(r=>r.split_group)).size,1);
    assert.ok(rows.every(r=>r.source_sha256===r.restored_sha256));
    for(const forbidden of ['/app/0-blocked','/outside','/app?token=private','/app/extra'])assert.equal(hits.get(forbidden),undefined);
    assert.equal(hits.get('/fail'),1);
    assert.ok((await readdir(join(output,'results'))).length>=rows.length+1);
    const before=new Map(hits);
    await rm(join(output,'manifest.jsonl'));
    await run();
    assert.equal(await readFile(join(output,'manifest.jsonl'),'utf8'),manifest);
    for(const [url,count] of before)if(url!='/robots.txt')assert.equal(hits.get(url),count,'resume recaptured '+url);
    const faultConfig=join(root,'fault-config.json');
    await writeFile(faultConfig,JSON.stringify({...config,domain_delay_seconds:0,requests_per_minute:600,
      seeds:[{...seed,id:'queue',url:origin+'/queue',scope:'/queue',allowed_hosts:[`localhost:${address.port}`]}]}));
    const childKey=createHash('sha256').update(JSON.stringify(['queue',origin+'/queue/child'])).digest('hex');
    collision=join(faultOutput,'results',childKey+'.json');
    const runFault=()=>promisify(execFile)('node',['--experimental-strip-types','browser/collector.ts',faultConfig,faultOutput],
      {env:{...process.env,CURL_CA_BUNDLE:join(root,'cert.pem'),NODE_EXTRA_CA_CERTS:join(root,'cert.pem')},timeout:120000,maxBuffer:2**20});
    await assert.rejects(runFault());
    const parentKey=createHash('sha256').update(JSON.stringify(['queue',origin+'/queue'])).digest('hex');
    const saved=JSON.parse(await readFile(join(faultOutput,'results',parentKey+'.json'),'utf8'));
    assert.equal(saved.status,'captured');assert.ok(saved.record.html.includes('Queue dashboard'));
    collide=false;await rm(collision,{recursive:true});
    await runFault();
    assert.equal(hits.get('/queue'),1,'saved parent was recaptured after queue failure');
    assert.equal((await readFile(join(faultOutput,'manifest.jsonl'),'utf8')).trim().split('\n').length,2);
    const liveConfig=join(root,'live-config.json'),liveOutput=join(root,'live-output');
    await writeFile(liveConfig,JSON.stringify({...config,mode:'live-session',domain_delay_seconds:0,requests_per_minute:600,
      seeds:[{...seed,id:'live',url:origin+'/live',scope:'/live',allowed_hosts:[`localhost:${address.port}`]}],
      live_operators:['text_clip_x','clean'],viewport_sweep:[
        {viewport:[800,600],dpr:1},{viewport:[390,844],dpr:2},
        {viewport:[844,390],dpr:1,resize_path:[[1280,720],[390,844]]}]}));
    await promisify(execFile)('node',['--experimental-strip-types','browser/collector.ts',liveConfig,liveOutput],
      {env:{...process.env,CURL_CA_BUNDLE:join(root,'cert.pem'),NODE_EXTRA_CA_CERTS:join(root,'cert.pem')},timeout:120000,maxBuffer:2**20});
    const liveRows=(await readFile(join(liveOutput,'live-manifest.jsonl'),'utf8')).trim().split('\n').map(line=>JSON.parse(line));
    assert.equal(liveRows.length,6);
    const raw=await Promise.all(liveRows.map(async row=>{
      const content=await readFile(join(liveOutput,'live-cases',row.sha256+'.json'));
      assert.equal(createHash('sha256').update(content).digest('hex'),row.sha256);
      return JSON.parse(content.toString());
    }));
    assert.equal(new Set(raw.map(r=>r.payload.source.base_scene_id)).size,1);
    assert.equal(new Set(raw.map(r=>r.payload.source.split_group)).size,1);
    assert.equal(new Set(raw.map(r=>JSON.stringify([r.payload.viewport,r.payload.dpr]))).size,3);
    assert.ok(raw.every(r=>r.result.rollback&&r.payload.source.capture_mode==='live-session'));
    assert.ok(raw.every(r=>!r.payload.source.restored_sha256));
    assert.ok(raw.filter(r=>r.result.status==='accepted_bug').length>=3);
    const liveRun=join(root,'live-run'),liveExport=join(root,'live-yolo');
    const imported=await promisify(execFile)('python',['-m','lumen_data.cli','sources','import-live',
      '--manifest',join(liveOutput,'live-manifest.jsonl'),'--assets',join(liveOutput,'assets'),'--run',liveRun],
      {env:process.env,timeout:60000,maxBuffer:2**20});
    assert.equal(JSON.parse(imported.stdout).accepted,6);
    const exported=await promisify(execFile)('python',['-m','lumen_data.cli','export','--run',liveRun,
      '--output',liveExport,'--format','yolo-singleclass'],{env:process.env,timeout:60000,maxBuffer:2**20});
    assert.equal(JSON.parse(exported.stdout).capture_modes['live-session'],6);
    const robotsConfig=join(root,'robots-config.json'),robotsOutput=join(root,'robots-output');
    await writeFile(robotsConfig,JSON.stringify({...config,domain_delay_seconds:0,requests_per_minute:600,
      seeds:['one','two'].map(id=>({...seed,id,url:origin+'/robots-error/'+id,scope:'/robots-error',allowed_hosts:[`localhost:${address.port}`]}))}));
    const runRobots=(target:string)=>promisify(execFile)('node',['--experimental-strip-types','browser/collector.ts',robotsConfig,target],
      {env:{...process.env,CURL_CA_BUNDLE:join(root,'cert.pem'),NODE_EXTRA_CA_CERTS:join(root,'cert.pem')},timeout:120000,maxBuffer:2**20});
    robotsStatus=503;const robotsBefore=hits.get('/robots.txt')??0;
    await runRobots(robotsOutput);
    assert.equal(hits.get('/robots-error/one'),undefined,'unreachable robots must prevent navigation');
    assert.equal(hits.get('/robots-error/two'),undefined);
    assert.equal((hits.get('/robots.txt')??0)-robotsBefore,1,'robots errors must not be retried per variant');
    const failures=await Promise.all((await readdir(join(robotsOutput,'results'))).map(async file=>JSON.parse(await readFile(join(robotsOutput,'results',file),'utf8'))));
    assert.ok(failures.every(entry=>entry.status==='failed'&&entry.error.includes('robots')));
    robotsStatus=404;
    await runRobots(join(root,'robots-absent'));
    assert.equal(hits.get('/robots-error/one'),1,'absent robots permits navigation');
  }finally{
    server.closeAllConnections();await new Promise<void>((resolve,reject)=>server.close(e=>e?reject(e):resolve()));
    await rm(root,{recursive:true});
  }
});
