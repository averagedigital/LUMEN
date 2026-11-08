import {PlaywrightCrawler, Configuration, RequestQueue} from '@crawlee/playwright';
import {RobotsTxtFile} from '@crawlee/utils';
import {createHash, randomUUID} from 'node:crypto';
import {mkdir, open, readFile, readdir, rename, rm, statfs} from 'node:fs/promises';
import {join, resolve, dirname} from 'node:path';
import {spawn} from 'node:child_process';
import {pathToFileURL} from 'node:url';
import {captureSource, saveAsset, SourceRejected, visitSource, sourceDocument} from './source_capture.ts';
import {capturePreparedPage, type Request} from './capture.ts';
import {OPERATORS} from './mutations.ts';

type Seed={id:string;url:string;scope:string;family:string;allowed_hosts:string[];license:string;
  repository?:{url:string;revision:string}};
type Config={seeds:Seed[];max_pages_per_project:number;max_depth:number;concurrency:number;
  requests_per_minute:number;domain_delay_seconds:number;mode?:'offline-replay'|'live-session';
  live_operators?:string[];viewport_sweep?:Array<Pick<Request,'viewport'|'dpr'|'resize_path'>>};
type Variant={operator:string;profile:Pick<Request,'viewport'|'dpr'|'resize_path'>};
type Entry={seed:Seed;url:string;depth:number;status:string;links:string[];variant?:Variant;
  record?:Record<string,unknown>;live_case?:{sha256:string;base_scene_id:string};error?:string};
const hash=(value:string|Buffer)=>createHash('sha256').update(value).digest('hex');

function sourceIdentity(seed:Seed,address:string){
  const repository=seed.repository;
  if(!repository)return {project:seed.id,address,provenance:{source_url:address,origin:'web-crawl'}};
  const url=repository.url.toLowerCase(),route=new URL(address).pathname;
  return {project:'repository-'+hash(url).slice(0,16),address:JSON.stringify([url,repository.revision,route]),
    provenance:{source_url:repository.url+'/tree/'+repository.revision,origin:'repository-render',
      repository_url:repository.url,repository_revision:repository.revision,source_route:route,
      repository_verification:'declared'}};
}

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
  if(c.mode!==undefined&&!['offline-replay','live-session'].includes(c.mode))throw new Error('invalid collector mode');
  if(c.mode==='live-session'){
    if(!Array.isArray(c.live_operators)||!c.live_operators.length||c.live_operators.length>16||
      new Set(c.live_operators).size!==c.live_operators.length||c.live_operators.some(op=>
        !OPERATORS.includes(op as typeof OPERATORS[number])||['benign_near_boundary','benign_composite_control'].includes(op)))throw new Error('invalid live operators');
    if(!Array.isArray(c.viewport_sweep)||!c.viewport_sweep.length||c.viewport_sweep.length>16)throw new Error('invalid live viewport sweep');
    for(const p of c.viewport_sweep){
      if(!p||!Number.isInteger(p.dpr)||![1,2].includes(p.dpr)||
        Object.keys(p).some(k=>!['viewport','dpr','resize_path'].includes(k))||
        (p.resize_path!==undefined&&(!Array.isArray(p.resize_path)||!p.resize_path.length||p.resize_path.length>8)))throw new Error('invalid live viewport');
      for(const size of [p.viewport,...(p.resize_path??[])])if(!Array.isArray(size)||size.length!==2||
        size.some(n=>!Number.isInteger(n)||n<240||n>3840)||size[0]*size[1]*p.dpr**2>16777216)throw new Error('invalid live viewport');
    }
    if(new Set(c.viewport_sweep.map(p=>JSON.stringify([p.viewport,p.dpr,p.resize_path]))).size!==c.viewport_sweep.length)throw new Error('duplicate live viewport');
  }else if(c.live_operators!==undefined||c.viewport_sweep!==undefined)throw new Error('live settings require live-session mode');
  for(const s of c.seeds){
    if(!s||typeof s.id!=='string'||!s.id.match(/^[a-z0-9][a-z0-9_-]{0,63}$/)||
      typeof s.url!=='string'||typeof s.scope!=='string'||!s.scope.startsWith('/')||s.scope.startsWith('//')||
      typeof s.family!=='string'||!s.family||typeof s.license!=='string'||!s.license||
      !Array.isArray(s.allowed_hosts)||!s.allowed_hosts.length||s.allowed_hosts.length>32)throw new Error('invalid collector seed');
    if(s.repository!==undefined){
      const r=s.repository;
      if(!r||typeof r!=='object'||Array.isArray(r)||Object.keys(r).some(k=>!['url','revision'].includes(k))||
        typeof r.url!=='string'||r.url.length>300||r.url.toLowerCase().endsWith('.git')||
        !/^https:\/\/github\.com\/[a-z0-9][a-z0-9_.-]*\/[a-z0-9][a-z0-9_.-]*$/i.test(r.url)||
        typeof r.revision!=='string'||!r.revision.match(/^[a-f0-9]{40}$/))throw new Error('invalid repository provenance');
    }
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
    const codeRevision=revision.digest('hex'), contract=JSON.stringify({config,revision:codeRevision});
    const contractFile=join(root,'contract.json');
    try{if(await readFile(contractFile,'utf8')!==contract)throw new Error('collector contract changed');}
    catch(e){if((e as NodeJS.ErrnoException).code!=='ENOENT')throw e;await atomic(contractFile,contract);}
    const entries=new Map<string,Entry>(), pages=new Map<string,Set<string>>();
    const key=(seed:Seed,url:string,variant?:Variant)=>{
      const identity=sourceIdentity(seed,url);
      return hash(JSON.stringify(variant?[identity.project,identity.address,variant]:[identity.project,identity.address]));
    };
    const reservePage=(seed:Seed,url:string)=>{
      const identity=sourceIdentity(seed,url);
      let urls=pages.get(identity.project);if(!urls){urls=new Set();pages.set(identity.project,urls);}
      if(!urls.has(identity.address)&&urls.size>=config.max_pages_per_project)return false;
      urls.add(identity.address);return true;
    };
    for(const file of (await readdir(results)).filter(f=>/^[a-f0-9]{64}\.json$/.test(f)).sort()){
      const entry:Entry=JSON.parse(await readFile(join(results,file),'utf8'));
      if(file!==key(entry.seed,entry.url,entry.variant)+'.json')throw new Error('collector checkpoint identity mismatch');
      delete entry.record;
      if(!reservePage(entry.seed,entry.url))throw new Error('checkpoint exceeds project URL budget');
      entries.set(file.slice(0,-5),entry);
    }
    const storage=new Configuration({purgeOnStart:false,persistStorage:true,
      storageClientOptions:{localDataDirectory:join(root,'queue')}});
    const queue=await RequestQueue.open('pages',{config:storage});
    const persist=async(id:string,entry:Entry)=>{await atomic(join(results,id+'.json'),JSON.stringify(entry));};
    const variants:Array<Variant|undefined>=config.mode==='live-session'?config.viewport_sweep!.flatMap(p=>{
      const profile={viewport:p.viewport,dpr:p.dpr,...(p.resize_path?{resize_path:p.resize_path}:{})};
      return config.live_operators!.map(operator=>({operator,profile}));
    }):[undefined];
    const enqueue=async(seed:Seed,url:string,depth:number)=>{
      if(depth>config.max_depth||!reservePage(seed,url))return;
      for(const variant of variants){
        const id=key(seed,url,variant);
        if(!entries.has(id)){
          const entry:Entry={seed,url,depth,status:'queued',links:[],...(variant?{variant}:{})};
          entries.set(id,entry);await persist(id,entry);
        }
        await queue.addRequest({url,uniqueKey:id,skipNavigation:true,userData:{id}});
      }
    };
    const robotsFiles=new Map<string,Promise<RobotsTxtFile>>();
    const robotsFor=(address:string)=>{
      const origin=new URL(address).origin;
      let pending=robotsFiles.get(origin);
      if(!pending){
        pending=(async()=>{
          const url=origin+'/robots.txt';
          const response=await fetch(url,{redirect:'manual',signal:AbortSignal.timeout(15000)});
          try{
            if([404,410].includes(response.status))return RobotsTxtFile.from(url,'');
            if(response.status!==200)throw new Error('robots HTTP '+response.status);
            if(!/^text\/plain\b/i.test(response.headers.get('content-type')??''))throw new Error('robots content type');
            const chunks:Buffer[]=[];let bytes=0;
            const reader=response.body!.getReader();
            try{
              for(;;){
                const {done,value}=await reader.read();if(done)break;
                bytes+=value.length;if(bytes>512*1024)throw new Error('robots byte limit');
                chunks.push(Buffer.from(value));
              }
            }finally{await reader.cancel();reader.releaseLock();}
            return RobotsTxtFile.from(url,Buffer.concat(chunks).toString('utf8'));
          }finally{if(response.body&&!response.body.locked)await response.body.cancel();}
        })();
        robotsFiles.set(origin,pending);
      }
      return pending;
    };
    let fatal:Error|undefined;
    const crawler=new PlaywrightCrawler({requestQueue:queue,minConcurrency:1,maxConcurrency:config.concurrency,
      maxRequestsPerMinute:config.requests_per_minute,sameDomainDelaySecs:config.domain_delay_seconds,
      maxRequestRetries:0,maxSessionRotations:0,retryOnBlocked:false,useSessionPool:false,
      respectRobotsTxtFile:false,requestHandlerTimeoutSecs:90,
      browserPoolOptions:{useFingerprints:false},launchContext:{launchOptions:{headless:true,args:['--disable-partial-raster']}},
      async requestHandler({request,page}){
        const id=request.userData.id as string, entry=entries.get(id);
        if(!entry)throw new Error('missing collector reservation');
        if(entry.status!=='queued')return;
        const identity=sourceIdentity(entry.seed,entry.url);
        const baseId=hash(JSON.stringify([identity.project,identity.address,'initial']));
        try{
          if(!(await robotsFor(entry.url)).isAllowed(entry.url)){
            entry.status='skipped';entry.error='robotsTxt';await persist(id,entry);return;
          }
        }catch(e){
          entry.status='failed';entry.error=('robots: '+(e instanceof Error?e.message:String(e))).slice(0,300);
          await persist(id,entry);return;
        }
        const disk=await statfs(root);
        if(disk.bavail*disk.bsize<40*2**30){fatal=new Error('collector disk reserve');crawler.stop();throw fatal;}
        const browser=page.context().browser();if(!browser)throw new Error('collector browser unavailable');
        const initial=entry.variant?(entry.variant.profile.resize_path?.[0]??entry.variant.profile.viewport):[1440,1000];
        const context=await browser.newContext({viewport:{width:initial[0],height:initial[1]},deviceScaleFactor:entry.variant?.profile.dpr??1,
          locale:'en-US',timezoneId:'UTC',colorScheme:'light',reducedMotion:'reduce',serviceWorkers:'block',acceptDownloads:false});
        try{
          const ready=await context.newPage();let javascriptErrors=0;
          ready.on('pageerror',()=>{javascriptErrors++;});
          const sourceRequest={url:entry.url,allowed_hosts:entry.seed.allowed_hosts,asset_root:assets};
          const discover=async()=>{
              entry.links=routesFor(entry.seed,await ready.locator('a[href]').evaluateAll(anchors=>anchors.slice(0,1000).map(a=>(a as HTMLAnchorElement).href)));
          };
          if(entry.variant){
            const variant=entry.variant;
            await visitSource(ready,sourceRequest,async({assets:sourceAssets})=>{
              await discover();
              if(javascriptErrors)throw new SourceRejected('source JavaScript error');
              const seed=parseInt(hash(JSON.stringify([identity.project,identity.address,variant.operator])).slice(0,8),16);
              const result=await capturePreparedPage(ready,{...variant.profile,operator:variant.operator,seed});
              if(javascriptErrors)throw new SourceRejected('source JavaScript error');
              if(!result.status.startsWith('accepted')){entry.status='live-rejected';entry.error=JSON.stringify({status:result.status,evidence:result.evidence});return;}
              if(result.rollback!==true||result.capture_mode!=='live-session')throw new Error('unverified live capture');
              const html=await sourceDocument(ready,true);
              if(Buffer.byteLength(html)>2000000)throw new SourceRejected('source HTML size limit');
              const source={html,assets:structuredClone(sourceAssets),base_scene_id:baseId,
                source_project_id:identity.project,template_lineage_id:identity.project,split_group:'web-project:'+identity.project,
                structural_family_id:entry.seed.family,corpus:'unclassified',render_mode:'live-session',capture_mode:'live-session',
                state_id:'initial',source_sha256:hash(Buffer.from(result.reference!,'base64')),
                provenance:{license:entry.seed.license,assets_rights:'unverified',public_release_status:'research_only',
                  ...identity.provenance,revision:hash(html),capture_mode:'live-session',collector_revision:codeRevision}};
              const payload={...variant.profile,operator:variant.operator,seed,source,scene:null,locale:'en-US',generator_revision:codeRevision};
              const content=JSON.stringify({payload,result});
              if(Buffer.byteLength(content)>32*2**20)throw new SourceRejected('live case byte limit');
              const digest=hash(content), directory=join(root,'live-cases');await mkdir(directory,{recursive:true});
              await atomic(join(directory,digest+'.json'),content);
              entry.live_case={sha256:digest,base_scene_id:baseId};entry.status='live-captured';
            });
          }else{
          const source=await captureSource(ready,sourceRequest,discover);
          const {reference,elements,...metadata}=source;
          const sceneId=entry.seed.repository?baseId:source.scene_id;
          const imageHash=await saveAsset(assets,reference);
          if(imageHash!==source.source_sha256||imageHash!==source.restored_sha256)throw new Error('collector source checksum');
          const sidecars:Record<string,string>={};
          if(elements)sidecars.elements=await saveAsset(assets,Buffer.from(JSON.stringify({schema_version:'lumen.elements.v1',scene_id:sceneId,elements})));
          entry.record={...metadata,scene_id:sceneId,base_scene_id:sceneId,image_hash:imageHash,sidecars,
            kind:'screen',render_mode:'static',corpus:'unclassified',source_project_id:identity.project,
            template_lineage_id:identity.project,split_group:'web-project:'+identity.project,
            structural_family_id:entry.seed.family,provenance:{license:entry.seed.license,
              assets_rights:'unverified',public_release_status:'research_only',...identity.provenance,
              revision:source.scene_id,capture_mode:'offline-replay'}};
          entry.status='captured';
          }
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
    const restoredURLs=new Set<string>();
    for(const entry of [...entries.values()]){
      const pageKey=key(entry.seed,entry.url);
      if(!restoredURLs.has(pageKey)){restoredURLs.add(pageKey);await enqueue(entry.seed,entry.url,entry.depth);}
      for(const url of entry.links)await enqueue(entry.seed,url,entry.depth+1);
    }
    for(const seed of config.seeds)await enqueue(seed,seed.url,0);
    await crawler.run();
    const seenScenes=new Set(),seenImages=new Set(),stats:Record<string,number>={};
    const temporary=join(root,'manifest.'+randomUUID()+'.part'), manifest=await open(temporary,'wx');
    const liveTemporary=join(root,'live-manifest.'+randomUUID()+'.part'),liveManifest=await open(liveTemporary,'wx');
    const liveScenes=new Set<string>();let liveCases=0;
    try{
      for(const [id,entry] of [...entries].sort(([a],[b])=>a.localeCompare(b))){
        stats[entry.status]=(stats[entry.status]??0)+1;
        if(entry.live_case){await liveManifest.write(JSON.stringify(entry.live_case)+'\n');liveCases++;liveScenes.add(entry.live_case.base_scene_id);}
        if(entry.status!=='captured')continue;
        const saved:Entry=JSON.parse(await readFile(join(results,id+'.json'),'utf8'));
        const record=saved.record;if(!record)throw new Error('missing captured source record');
        if(seenScenes.has(record.base_scene_id)||seenImages.has(record.image_hash)){stats.duplicates=(stats.duplicates??0)+1;continue;}
        seenScenes.add(record.base_scene_id);seenImages.add(record.image_hash);
        await manifest.write(JSON.stringify(record)+'\n');
      }
      await manifest.sync();
      await liveManifest.sync();
    }finally{await manifest.close();await liveManifest.close();}
    await rename(temporary,join(root,'manifest.jsonl'));
    await rename(liveTemporary,join(root,'live-manifest.jsonl'));
    const summary={status:fatal?'failed':'completed',counts:stats,unique_sources:seenScenes.size,
      live_cases:liveCases,live_base_scenes:liveScenes.size,reserved_pages:[...pages.values()].reduce((n,s)=>n+s.size,0),
      projects:new Set([...entries.values()].filter(e=>['captured','live-captured'].includes(e.status)).map(e=>sourceIdentity(e.seed,e.url).project)).size};
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
