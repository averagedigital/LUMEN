import type {BrowserContext, Page} from 'playwright';
import {spawn} from 'node:child_process';
import {createHash, randomUUID} from 'node:crypto';
import {mkdir, open, readFile, rename, rm, stat, statfs} from 'node:fs/promises';
import {join} from 'node:path';
import {annotations} from './annotations.ts';

export type SourceRequest = {url: string; allowed_hosts: string[]; asset_root: string};
export class SourceRejected extends Error {}

type Asset = {sha256: string; content_type: string};
const sha = (value: Buffer | string) => createHash('sha256').update(value).digest('hex');
const contentTypes = ['text/css', 'font/woff2', 'image/jpeg', 'image/png', 'image/svg+xml', 'image/webp'];

function fetchResource(url: string, headers: Record<string,string>, signal: AbortSignal, account: (bytes:number)=>void) {
  return new Promise<{status:number;headers:Record<string,string>;body:Buffer}>((resolve,reject)=>{
    const args=['--disable','--silent','--show-error','--compressed','--max-time','15','--connect-timeout','10',
      '--proto','=https','--include','--suppress-connect-headers'];
    for(const name of ['user-agent','accept','accept-language']) {
      if(headers[name]) args.push('--header',`${name}: ${headers[name]}`);
    }
    const child=spawn('curl',[...args,url],{stdio:['ignore','pipe','ignore'],signal});
    const chunks:Buffer[]=[];
    let bytes=0, headerBytes=0, header=Buffer.alloc(0), status=0, failure:Error|undefined;
    const fields:Record<string,string>={};
    const fail=(error:Error)=>{failure??=error;child.kill('SIGKILL');};
    child.stdout.on('data',(chunk:Buffer)=>{
      if(failure) return;
      try {
        if(!status) {
          header=Buffer.concat([header,chunk]);
          while(!status) {
            const end=header.indexOf('\r\n\r\n');
            if(end<0) {
              if(headerBytes+header.length>65536) throw new SourceRejected('source header size limit');
              return;
            }
            headerBytes+=end+4;
            if(headerBytes>65536) throw new SourceRejected('source header size limit');
            const lines=header.subarray(0,end).toString('latin1').split('\r\n');
            const code=Number(lines.shift()?.split(' ')[1]);
            if(!Number.isInteger(code)||code<100||code>599) throw new SourceRejected('source HTTP headers');
            header=header.subarray(end+4);
            if(code<200) continue;
            status=code;
            for(const line of lines) {
              const colon=line.indexOf(':');
              if(colon>0) fields[line.slice(0,colon).toLowerCase()]=line.slice(colon+1).trim();
            }
          }
          chunk=header;header=Buffer.alloc(0);
        }
        bytes+=chunk.length;account(chunk.length);
        if(bytes>8*2**20) throw new SourceRejected('source response size limit');
        chunks.push(chunk);
      } catch(error) {fail(error as Error);}
    });
    child.on('error',error=>{failure??=error;});
    child.on('close',(code,exitSignal)=>{
      if(failure||code!==0||!status) return reject(failure??new Error(`source transport failed (${code??exitSignal})`));
      for(const name of ['content-encoding','content-length','transfer-encoding','connection','set-cookie']) delete fields[name];
      resolve({status,headers:fields,body:Buffer.concat(chunks)});
    });
  });
}

async function validateRaster(body:Buffer, contentType:string, signal:AbortSignal) {
  const suffix=({'image/png':'png','image/jpeg':'jpg','image/webp':'webp'} as Record<string,string>)[contentType];
  if(!suffix) return 0;
  return new Promise<number>((resolve,reject)=>{
    const child=spawn('python',['-m','lumen_data.asset_validation',suffix],{stdio:['pipe','pipe','ignore'],signal});
    let output='', failure:Error|undefined;
    child.stdout.on('data',chunk=>{output+=chunk.toString();if(output.length>100) child.kill('SIGKILL');});
    child.stdin.on('error',error=>{failure??=error;});
    child.on('error',error=>{failure??=error;});
    child.on('close',code=>{
      if(code===0&&!failure&&Number.isInteger(Number(output))) resolve(Number(output));
      else if(code===2&&!failure) reject(new SourceRejected('source raster validation failed'));
      else reject(new Error('source raster validator unavailable'));
    });
    child.stdin.end(body);
  });
}

function permitted(url: string, hosts: string[]) {
  const parsed = new URL(url);
  return parsed.protocol === 'https:' && !parsed.username && !parsed.password &&
    (!parsed.port || parsed.port === '443') && hosts.includes(parsed.hostname) &&
    !Array.from(parsed.searchParams.keys()).some(key => /token|secret|password|credential|signature|api.?key/i.test(key));
}

async function saveAsset(root: string, body: Buffer) {
  const digest = sha(body), path = join(root, digest);
  const disk = await statfs(root);
  if (disk.bavail * disk.bsize < 40 * 2**30) throw new Error('source disk reserve');
  try {
    if ((await stat(path)).size > 8 * 2**20 || sha(await readFile(path)) !== digest) throw new Error('source cached asset checksum');
    return digest;
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error;
  }
  const temporary = `${path}.${randomUUID()}.part`;
  try {
    const file = await open(temporary, 'wx');
    try {await file.writeFile(body); await file.sync();} finally {await file.close();}
    await rename(temporary, path);
    const directory = await open(root, 'r');
    try {await directory.sync();} finally {await directory.close();}
  } finally {await rm(temporary, {force:true});}
  return digest;
}

export async function captureSource(page: Page, request: SourceRequest) {
  if (!permitted(request.url, request.allowed_hosts)) throw new SourceRejected('unapproved source URL');
  const viewport = page.viewportSize();
  if (!viewport || viewport.width * viewport.height > 4_194_304) throw new SourceRejected('source viewport limit');
  const context = page.context(), browser = context.browser();
  if (!browser) throw new Error('source browser unavailable');
  await mkdir(request.asset_root, {recursive:true});
  let requests = 0, offline: BrowserContext | undefined, totalBytes=0, totalPixels=0, active=0;
  let transportError:Error|undefined;
  const assets:Record<string,Asset>={}, assetURLs=new Set<string>(), pending=new Set<Promise<void>>(), waiting:Array<()=>void>=[];
  const abort=new AbortController();
  const deadline = setTimeout(() => {
    abort.abort();
    void context.close().catch(() => {});
    void offline?.close().catch(() => {});
  }, 60000);
  try {
    await context.route('**/*', route => {
      const work=(async()=>{
        const req=route.request(), url=req.url();
        if(++requests>300) transportError??=new SourceRejected('source request limit');
        if(transportError||abort.signal.aborted||req.method()!=='GET'||!permitted(url,request.allowed_hosts)) return route.abort();
        if(active>=4) await new Promise<void>(resolve=>waiting.push(resolve));
        active++;
        try {
          if(transportError||abort.signal.aborted) return await route.abort();
          const response=await fetchResource(url,req.headers(),abort.signal,bytes=>{
            totalBytes+=bytes;if(totalBytes>32*2**20) throw new SourceRejected('source response budget');
          });
          if([408,429].includes(response.status)||response.status>=500) throw new Error(`source HTTP ${response.status}`);
          const contentType=(response.headers['content-type']??'').split(';')[0].trim();
          if(response.status>=300&&response.status<400) throw new SourceRejected('source redirect: use canonical URL');
          if(response.status>=200&&response.status<300) {
            totalPixels+=await validateRaster(response.body,contentType,abort.signal);
            if(totalPixels>64*2**20) throw new SourceRejected('source pixel budget');
            if(['stylesheet','font','image'].includes(req.resourceType())) {
              if(!contentTypes.includes(contentType)) throw new SourceRejected('unsupported source asset type');
              assetURLs.add(url);
              if(assetURLs.size>128) throw new SourceRejected('source asset count limit');
              const asset={sha256:await saveAsset(request.asset_root,response.body),content_type:contentType};
              assets[url]=asset;
            }
          }
          await route.fulfill(response);
        } catch(error) {
          transportError??=error as Error;
          await route.abort().catch(()=>{});
        } finally {active--;waiting.shift()?.();}
      })();
      pending.add(work);void work.finally(()=>pending.delete(work)).catch(()=>{});
      return work;
    });
    await context.routeWebSocket('**/*', socket => socket.close());
    const response = await page.goto(request.url, {waitUntil:'load',timeout:15000}).catch(error=>{throw transportError??error;});
    if (!response?.ok() || !permitted(page.url(), request.allowed_hosts)) throw new SourceRejected('source HTTP or redirect');
    await page.evaluate(async () => {await document.fonts.ready;});
    await page.waitForTimeout(1000);
    if(transportError) throw transportError;
    await page.evaluate(() => {
      const source=document.baseURI;
      document.querySelectorAll('script,noscript,iframe,object,embed,meta[http-equiv],input[type="hidden"],link[as="script"]').forEach(el => el.remove());
      document.querySelectorAll('meta[name]').forEach(el => {
        if (/token|csrf|secret|password/i.test(el.getAttribute('name') ?? '')) el.remove();
      });
      document.querySelectorAll('input,textarea').forEach(el => {
        (el as HTMLInputElement).value = '';
        el.removeAttribute('value');
        if (el.tagName === 'TEXTAREA') el.textContent = '';
      });
      document.querySelectorAll('*').forEach(el => {
        for (const attribute of Array.from(el.attributes)) {
          if (/^on/i.test(attribute.name) || /nonce|token|secret|password/i.test(attribute.name)) el.removeAttribute(attribute.name);
        }
      });
      const base = document.createElement('base'); base.href = source; document.head.prepend(base);
    });
    if (!await page.evaluate(() => Array.from(document.images).every(image => image.complete && image.naturalWidth > 0))) {
      throw new SourceRejected('missing source images');
    }
    let elements: Awaited<ReturnType<typeof annotations>> | undefined;
    let elementsStatus = 'captured';
    try {elements = await annotations(page);} catch (error) {
      if (!(error instanceof Error) || !['scene_limit: annotation nodes',
        'page.evaluate: Error: scene_limit: annotation nodes'].includes(error.message.split('\n')[0])) throw error;
      elementsStatus = 'node_limit';
    }
    const reference = await page.screenshot({animations:'disabled',caret:'hide',timeout:15000});
    if (!reference.equals(await page.screenshot({animations:'disabled',caret:'hide',timeout:15000}))) throw new SourceRejected('unstable source pixels');
    await page.evaluate(() => {
      for (const style of document.querySelectorAll('style')) {
        if (!style.sheet) continue;
        const rules = Array.from(style.sheet.cssRules).map(rule => rule.cssText).join('\n');
        if (style.sheet.disabled) style.media = 'not all';
        style.textContent = rules;
      }
    });
    const html = await page.content();
    if (Buffer.byteLength(html) > 2_000_000) throw new SourceRejected('source HTML size limit');
    const dpr = await page.evaluate(() => devicePixelRatio);
    offline = await browser.newContext({viewport,deviceScaleFactor:dpr,locale:'en-US',timezoneId:'UTC',
      colorScheme:'light',reducedMotion:'reduce',serviceWorkers:'block',javaScriptEnabled:false,acceptDownloads:false});
    let blocked = 0;
    await offline.route('**/*', async route => {
      const asset = assets[route.request().url()];
      if (!asset) {blocked++; return route.abort();}
      const body = await readFile(join(request.asset_root, asset.sha256));
      if (sha(body) !== asset.sha256) {blocked++; return route.abort();}
      return route.fulfill({status:200,contentType:asset.content_type,headers:{'access-control-allow-origin':'*'},body});
    });
    const restored = await offline.newPage();
    await restored.setContent(html, {waitUntil:'load',timeout:15000});
    await restored.evaluate(async () => {await document.fonts.ready;});
    const image = await restored.screenshot({animations:'disabled',caret:'hide',timeout:15000});
    if (blocked || !await restored.evaluate(() => Array.from(document.images).every(i => i.complete && i.naturalWidth > 0))) {
      throw new SourceRejected('missing offline source assets');
    }
    if (!image.equals(reference)) throw new SourceRejected('offline pixels differ from source');
    return {html,assets,elements,elements_status:elementsStatus,reference,
      scene_id:sha(html),source_url:request.url,final_url:page.url(),captured_at:new Date().toISOString(),
      source_sha256:sha(reference),restored_sha256:sha(image),defect_status:'unverified' as const,
      viewport:[viewport.width,viewport.height],dpr,asset_bytes:totalBytes};
  } finally {
    clearTimeout(deadline);
    abort.abort();
    await Promise.allSettled(pending);
    await context.unrouteAll({behavior:'wait'});
    await offline?.close();
  }
}
