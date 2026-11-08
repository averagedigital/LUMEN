import {PlaywrightCrawler, Configuration, RequestQueue} from '@crawlee/playwright';
import {createHash, randomUUID} from 'node:crypto';
import {mkdir, open, readFile, readdir, rename, rm, statfs} from 'node:fs/promises';
import {join, resolve, dirname} from 'node:path';
import {spawn} from 'node:child_process';
import {pathToFileURL} from 'node:url';
import {captureSource, saveAsset, SourceRejected} from './source_capture.ts';

type Seed={id:string;url:string;scope:string;family:string;allowed_hosts:string[];license:string};
type Config={seeds:Seed[];max_pages_per_project:number;max_depth:number;concurrency:number;
  requests_per_minute:number;domain_delay_seconds:number};
type Entry={seed:Seed;url:string;depth:number;status:string;links:string[];record?:Record<string,unknown>;error?:string};
const hash=(value:string|Buffer)=>createHash('sha256').update(value).digest('hex');

function scoped(seed:Seed, address:string){
  const url=new URL(address), origin=new URL(seed.url);
  return url.origin===origin.origin&&!url.username&&!url.password&&!url.search&&!url.hash&&
    !/%2f|%5c/i.test(url.pathname)&&
    (seed.scope==='/'||url.pathname===seed.scope||url.pathname.startsWith(seed.scope.replace(/\/$/,'')+'/'))&&
    !/(?:^|\/)(?:logout|signout|delete|remove|unsubscribe)(?:\/|$)/i.test(decodeURIComponent(url.pathname));
}

export function routesFor(seed:Seed, links:string[]){
  return [...new Set(links.filter(url=>{
    try{return url!==seed.url&&scoped(seed,url);}catch{return false;}
  }))];
}

export function validateCollectorConfig(value:unknown):asserts value is Config{
  if(!value||typeof value!=='object')throw new Error('collector config must be an object');
  const c=value as Config;
  for(const [n,min,max] of [[c.max_pages_per_project,1,1000],[c.max_depth,0,8],
    [c.concurrency,1,4],[c.requests_per_minute,1,600],[c.domain_delay_seconds,0,60]]){
    if(!Number.isInteger(n)||n<min||n>max)throw new Error('invalid collector budget');
  }
  if(!Array.isArray(c.seeds)||!c.seeds.length||c.seeds.length>50000)throw new Error('invalid collector seeds');
  for(const s of c.seeds){
    if(!s||typeof s.id!=='string'||!s.id.match(/^[a-z0-9][a-z0-9_-]{0,63}$/)||
      typeof s.url!=='string'||typeof s.scope!=='string'||!s.scope.startsWith('/')||s.scope.startsWith('//')||
      typeof s.family!=='string'||!s.family||typeof s.license!=='string'||!s.license||
      !Array.isArray(s.allowed_hosts)||!s.allowed_hosts.length||s.allowed_hosts.length>32)throw new Error('invalid collector seed');
    const url=new URL(s.url);
    if(url.protocol!=='https:'||!s.allowed_hosts.includes(url.host)||!scoped(s,s.url))throw new Error('seed outside approved scope');
    for(const host of s.allowed_hosts){
      if(typeof host!=='string'||new URL('https://'+host).host!==host)throw new Error('invalid approved host');
    }
  }
}

async function atomic(path:string, value:string){
  const temporary=path+'.'+randomUUID()+'.part';
  try{
    const file=await open(temporary,'wx');
    try{await file.writeFile(value);await file.sync();}finally{await file.close();}
    await rename(temporary,path);
    const directory=await open(dirname(path),'r');try{await directory.sync();}finally{await directory.close();}
  }finally{await rm(temporary,{force:true});}
}

async function acquireLock(path:string){
  const child=spawn('flock',['--nonblock',path,'sh','-c','echo READY; cat >/dev/null'],{stdio:['pipe','pipe','ignore']});
  const ended=new Promise<void>((resolve,reject)=>{child.once('exit',()=>resolve());child.once('error',reject);});
  await new Promise<void>((resolve,reject)=>{
    child.once('error',reject);child.once('exit',()=>reject(new Error('collector output is locked')));
    child.stdout.once('data',chunk=>String(chunk)==='READY\n'?resolve():reject(new Error('collector lock protocol')));
  });
  return async()=>{child.stdin.end();await ended;};
}

export async function collect(config:Config, output:string){
  validateCollectorConfig(config);
  const root=resolve(output), results=join(root,'results'), assets=join(root,'assets');
  await mkdir(results,{recursive:true});await mkdir(assets,{recursive:true});
  const release=await acquireLock(join(root,'collector.lock'));
  try{
    const revision=createHash('sha256');
    for(const file of (await readdir('browser')).filter(f=>f.endsWith('.ts')).sort())revision.update(file).update(await readFile(join('browser',file)));
    revision.update(await readFile('package-lock.json'));
    const contract=JSON.stringify({config,revision:revision.digest('hex')});
    const contractFile=join(root,'contract.json');
    try{if(await readFile(contractFile,'utf8')!==contract)throw new Error('collector contract changed');}
    catch(e){if((e as NodeJS.ErrnoException).code!=='ENOENT')throw e;await atomic(contractFile,contract);}
    const entries=new Map<string,Entry>(), counts=new Map<string,number>();
    const key=(seed:Seed,url:string)=>hash(JSON.stringify([seed.id,url]));
    for(const file of (await readdir(results)).filter(f=>/^[a-f0-9]{64}\.json$/.test(f)).sort()){
      const entry:Entry=JSON.parse(await readFile(join(results,file),'utf8'));
      if(file!==key(entry.seed,entry.url)+'.json')throw new Error('collector checkpoint identity mismatch');
      delete entry.record;
      entries.set(file.slice(0,-5),entry);counts.set(entry.seed.id,(counts.get(entry.seed.id)??0)+1);
    }
    const storage=new Configuration({purgeOnStart:false,persistStorage:true,
      storageClientOptions:{localDataDirectory:join(root,'queue')}});
    const queue=await RequestQueue.open('pages',{config:storage});
    const persist=async(id:string,entry:Entry)=>{await atomic(join(results,id+'.json'),JSON.stringify(entry));};
    const enqueue=async(seed:Seed,url:string,depth:number)=>{
      if(depth>config.max_depth)return;
      const id=key(seed,url);
      if(!entries.has(id)){
        if((counts.get(seed.id)??0)>=config.max_pages_per_project)return;
        const entry:Entry={seed,url,depth,status:'queued',links:[]};
        entries.set(id,entry);counts.set(seed.id,(counts.get(seed.id)??0)+1);
        await persist(id,entry);
      }
      await queue.addRequest({url,uniqueKey:id,skipNavigation:true,userData:{id}});
    };
    let fatal:Error|undefined;
    const crawler=new PlaywrightCrawler({requestQueue:queue,minConcurrency:1,maxConcurrency:config.concurrency,
      maxRequestsPerMinute:config.requests_per_minute,sameDomainDelaySecs:config.domain_delay_seconds,
      maxRequestRetries:0,maxSessionRotations:0,retryOnBlocked:false,useSessionPool:false,
      respectRobotsTxtFile:true,requestHandlerTimeoutSecs:90,
      browserPoolOptions:{useFingerprints:false},launchContext:{launchOptions:{headless:true,args:['--disable-partial-raster']}},
      async onSkippedRequest({url,reason}){
        for(const [id,entry] of entries)if(entry.url===url&&entry.status==='queued'){
          entry.status='skipped';entry.error=reason;await persist(id,entry);
        }
      },
      async requestHandler({request,page}){
        const id=request.userData.id as string, entry=entries.get(id);
        if(!entry)throw new Error('missing collector reservation');
        if(entry.status!=='queued')return;
        const disk=await statfs(root);
        if(disk.bavail*disk.bsize<40*2**30){fatal=new Error('collector disk reserve');crawler.stop();throw fatal;}
        const browser=page.context().browser();if(!browser)throw new Error('collector browser unavailable');
        const context=await browser.newContext({viewport:{width:1440,height:1000},deviceScaleFactor:1,
          locale:'en-US',timezoneId:'UTC',colorScheme:'light',reducedMotion:'reduce',serviceWorkers:'block',acceptDownloads:false});
        try{
          const source=await captureSource(await context.newPage(),{url:entry.url,
            allowed_hosts:entry.seed.allowed_hosts,asset_root:assets},async ready=>{
              entry.links=routesFor(entry.seed,await ready.locator('a[href]').evaluateAll(anchors=>anchors.slice(0,1000).map(a=>(a as HTMLAnchorElement).href)));
            });
          const {reference,elements,...metadata}=source;
          const imageHash=await saveAsset(assets,reference);
          if(imageHash!==source.source_sha256||imageHash!==source.restored_sha256)throw new Error('collector source checksum');
          const sidecars:Record<string,string>={};
          if(elements)sidecars.elements=await saveAsset(assets,Buffer.from(JSON.stringify({schema_version:'lumen.elements.v1',scene_id:source.scene_id,elements})));
          entry.record={...metadata,base_scene_id:source.scene_id,image_hash:imageHash,sidecars,
            kind:'screen',render_mode:'static',corpus:'unclassified',source_project_id:entry.seed.id,
            template_lineage_id:entry.seed.id,split_group:'web-project:'+entry.seed.id,
            structural_family_id:entry.seed.family,provenance:{license:entry.seed.license,
              assets_rights:'unverified',public_release_status:'research_only',source_url:entry.url,
              revision:source.scene_id,origin:'web-crawl',capture_mode:'offline-replay'}};
          entry.status='captured';
        }catch(e){
          entry.status=e instanceof SourceRejected?'rejected':'failed';entry.error=(e instanceof Error?e.message:String(e)).slice(0,300);
          if(/disk reserve|cached asset checksum|ENOSPC|ENOMEM|EACCES/.test(entry.error)){fatal=e as Error;crawler.stop();}
        }finally{await context.close();}
        await persist(id,entry);
        delete entry.record;
        if(!fatal)for(const url of entry.links)await enqueue(entry.seed,url,entry.depth+1);
      },
      async failedRequestHandler({request},error){
        const id=request.userData.id as string,entry=entries.get(id);
        if(entry){
          const saved:Entry=JSON.parse(await readFile(join(results,id+'.json'),'utf8'));
          if(saved.status!=='queued'){
            fatal??=error;crawler.stop('collector persistence failure');return;
          }
          entry.status='failed';entry.error=error.message.slice(0,300);await persist(id,entry);
        }
      },
    },storage);
    for(const entry of [...entries.values()]){
      await enqueue(entry.seed,entry.url,entry.depth);
      for(const url of entry.links)await enqueue(entry.seed,url,entry.depth+1);
    }
    for(const seed of config.seeds)await enqueue(seed,seed.url,0);
    await crawler.run();
    const seenScenes=new Set(),seenImages=new Set(),stats:Record<string,number>={};
    const temporary=join(root,'manifest.'+randomUUID()+'.part'), manifest=await open(temporary,'wx');
    try{
      for(const [id,entry] of [...entries].sort(([a],[b])=>a.localeCompare(b))){
        stats[entry.status]=(stats[entry.status]??0)+1;
        if(entry.status!=='captured')continue;
        const saved:Entry=JSON.parse(await readFile(join(results,id+'.json'),'utf8'));
        const record=saved.record;if(!record)throw new Error('missing captured source record');
        if(seenScenes.has(record.base_scene_id)||seenImages.has(record.image_hash)){stats.duplicates=(stats.duplicates??0)+1;continue;}
        seenScenes.add(record.base_scene_id);seenImages.add(record.image_hash);
        await manifest.write(JSON.stringify(record)+'\n');
      }
      await manifest.sync();
    }finally{await manifest.close();}
    await rename(temporary,join(root,'manifest.jsonl'));
    const summary={status:fatal?'failed':'completed',counts:stats,unique_sources:seenScenes.size,
      projects:new Set([...entries.values()].filter(e=>e.status==='captured').map(e=>e.seed.id)).size};
    await atomic(join(root,'summary.json'),JSON.stringify(summary));
    if(fatal)throw fatal;
    return summary;
  }finally{
    await release();
  }
}

if(process.argv[1]&&import.meta.url===pathToFileURL(resolve(process.argv[1])).href){
  const [configFile,output]=process.argv.slice(2);
  if(!configFile||!output)throw new Error('Usage: collector.ts CONFIG.json OUTPUT');
  const config:unknown=JSON.parse(await readFile(configFile,'utf8'));validateCollectorConfig(config);
  console.log(JSON.stringify(await collect(config,output)));
}
