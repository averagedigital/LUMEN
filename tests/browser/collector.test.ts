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
  const hits=new Map<string,number>();
  const faultOutput=join(root,'fault-output');let collide=true;let collision='';
  const style='<style>body{margin:24px;font:20px Arial;background:white;color:#17212b}a{display:block}</style>';
  const server=createServer({key:await readFile(join(root,'key.pem')),cert:await readFile(join(root,'cert.pem'))},(req,res)=>{
    const path=req.url!;hits.set(path,(hits.get(path)??0)+1);
    if(path==='/robots.txt'){res.setHeader('content-type','text/plain');return res.end('User-agent: *\nDisallow: /app/0-blocked\n');}
    res.setHeader('content-type','text/html');
    if(path==='/queue'){
      const respond=()=>res.end(style+'<h1>Queue dashboard</h1><a href="/queue/child">Child</a>');
      if(collide){void mkdir(collision).then(respond,e=>res.destroy(e));return;}
      respond();return;
    }
    if(path==='/app')return res.end(style+'<h1 id="title"></h1><script>document.cookie="marker=first";document.getElementById("title").textContent="JS dashboard"</script>'+['0-blocked','settings','tables','extra'].map(p=>`<a href="/app/${p}">${p}</a>`).join('')+'<a href="/outside">Outside</a><a href="/app?token=private">Query</a>');
    if(path==='/other')return res.end(style+'<h1 id="title"></h1><script>setTimeout(()=>{document.getElementById("title").textContent=document.cookie.includes("marker=first")?"context leak":"Isolated dashboard"},200)</script>');
    if(path==='/fail'){res.statusCode=503;return res.end('unavailable');}
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
  }finally{
    server.closeAllConnections();await new Promise<void>((resolve,reject)=>server.close(e=>e?reject(e):resolve()));
    await rm(root,{recursive:true});
  }
});
