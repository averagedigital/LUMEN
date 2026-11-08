import test, {after, before} from 'node:test';
import assert from 'node:assert/strict';
import {chromium, type Browser} from 'playwright';
import {mkdtemp, readFile, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {createHash} from 'node:crypto';
import {createServer, type Server} from 'node:https';
import {execFileSync} from 'node:child_process';
import {captureSource, SourceRejected, visitSource} from '../../browser/source_capture.ts';

let browser: Browser;
let certificates: string;
let previousCA: string | undefined;
before(async () => {
  certificates=await mkdtemp(join(tmpdir(),'lumen-source-ca-'));
  execFileSync('openssl',['req','-x509','-newkey','rsa:2048','-nodes','-keyout',join(certificates,'key.pem'),
    '-out',join(certificates,'cert.pem'),'-days','1','-subj','/CN=localhost',
    '-addext','subjectAltName=DNS:localhost,IP:127.0.0.1'],{stdio:'ignore'});
  previousCA=process.env.CURL_CA_BUNDLE;
  process.env.CURL_CA_BUNDLE=join(certificates,'cert.pem');
  browser = await chromium.launch({headless:true,args:['--disable-partial-raster']});
});
after(async () => {
  await browser.close();
  if(previousCA===undefined) delete process.env.CURL_CA_BUNDLE; else process.env.CURL_CA_BUNDLE=previousCA;
  await rm(certificates,{recursive:true});
});
const url='https://localhost/preview/hero';
const css='body{margin:24px;background:#fff;color:#17212b;font:20px Arial}';
const svg='<svg xmlns="http://www.w3.org/2000/svg" width="80" height="80"><rect width="80" height="80" fill="red"/></svg>';

async function fixture(html: string, extra?: Parameters<typeof createServer>[1]) {
  const context=await browser.newContext({viewport:{width:800,height:600},locale:'en-US',timezoneId:'UTC',
    colorScheme:'light',reducedMotion:'reduce',serviceWorkers:'block',acceptDownloads:false,ignoreHTTPSErrors:true});
  const page=await context.newPage();
  const server: Server=createServer({key:await readFile(join(certificates,'key.pem')),cert:await readFile(join(certificates,'cert.pem'))},(req,res)=> {
    if(extra && req.url?.startsWith('/special')) return extra(req,res);
    const path=req.url;
    if(path==='/style.css'||path==='/assets/style.css') {res.setHeader('content-type','text/css');return res.end(css);}
    if(path==='/image.svg'||path==='/assets/image.svg') {res.setHeader('content-type','image/svg+xml');return res.end(svg);}
    if(path==='/missing.svg') {res.statusCode=404;return res.end('missing');}
    res.setHeader('content-type','text/html');return res.end(html);
  });
  await new Promise<void>((resolve,reject)=>{server.once('error',reject);server.listen(443,resolve);});
  const root=await mkdtemp(join(tmpdir(),'lumen-source-'));
  return {page,context,root,close:async()=>{await context.close();server.closeAllConnections();
    await new Promise<void>((resolve,reject)=>server.close(error=>error?reject(error):resolve()));await rm(root,{recursive:true});}};
}

test('public source captures complete offline assets and removes sensitive form values', async()=>{
  const f=await fixture('<link rel="stylesheet" href="/style.css"><h1>Account settings</h1><img src="/image.svg"><input value="sensitive-value"><textarea>sensitive-note</textarea><input type="hidden" value="secret-csrf"><script>window.privateConfig="secret-config"</script><noscript><img src="https://tracker.invalid/pixel"></noscript><button data-token="secret-token">Save changes</button>');
  try {
    const result=await captureSource(f.page,{url,allowed_hosts:['localhost'],asset_root:f.root});
    assert.ok(!/sensitive-value|sensitive-note|secret-csrf|secret-config|secret-token|tracker.invalid/.test(result.html));
    assert.ok(result.html.includes('Account settings'));
    assert.equal(result.source_sha256,result.restored_sha256);
    assert.equal(result.assets['https://localhost/image.svg'].content_type,'image/svg+xml');
    for(const asset of Object.values(result.assets)) {
      const body=await readFile(join(f.root,asset.sha256));
      assert.equal(createHash('sha256').update(body).digest('hex'),asset.sha256);
    }
    assert.ok(result.elements?.some(element=>element.accessible_name==='Save changes'));
    assert.equal(result.defect_status,'unverified');
  } finally {await f.close();}
});

for(const image of ['/missing.svg','https://unapproved.invalid/image.svg']) {
  test(`public source refuses incomplete images: ${image}`,async()=>{
    const f=await fixture(`<h1>Account settings</h1><img src="${image}">`);
    try {
      await assert.rejects(captureSource(f.page,{url,allowed_hosts:['localhost'],asset_root:f.root}),/source images/);
    } finally {await f.close();}
  });
}

test('public source refuses a capture whose pixels depend on a live canvas',async()=>{
  const f=await fixture('<h1>Account settings</h1><canvas width="80" height="80"></canvas><script>const c=document.querySelector("canvas").getContext("2d");c.fillStyle="red";c.fillRect(0,0,80,80)</script>');
  try {
    await assert.rejects(captureSource(f.page,{url,allowed_hosts:['localhost'],asset_root:f.root}),/offline pixels/);
  } finally {await f.close();}
});

test('public source preserves an existing base URL',async()=>{
  const f=await fixture('<base href="/assets/"><link rel="stylesheet" href="style.css"><h1>Settings</h1><img src="image.svg">');
  try {
    const result=await captureSource(f.page,{url,allowed_hosts:['localhost'],asset_root:f.root});
    assert.equal(result.source_sha256,result.restored_sha256);
    assert.ok(result.html.includes('https://localhost/assets/'));
  } finally {await f.close();}
});

test('same-origin integrity-protected CSS remains active in offline captures',async()=>{
  const integrity='sha384-'+createHash('sha384').update(css).digest('base64');
  const f=await fixture(`<link rel="stylesheet" href="/style.css" integrity="${integrity}"><h1>Account settings</h1>`);
  try {
    const expected=await f.context.newPage();
    await expected.setContent(`<style>${css}</style><h1>Account settings</h1>`);
    const pixels=await expected.screenshot();
    const result=await captureSource(f.page,{url,allowed_hosts:['localhost'],asset_root:f.root});
    assert.ok(result.reference.equals(pixels));
    assert.equal(result.source_sha256,result.restored_sha256);
    assert.ok(result.html.includes(integrity));
  } finally {await f.close();}
});

test('public source records annotation node limit without losing an exact offline capture',async()=>{
  const f=await fixture('<h1>Account settings</h1><div hidden>'+'<span>Hidden item</span>'.repeat(2001)+'</div>');
  try {
    const result=await captureSource(f.page,{url,allowed_hosts:['localhost'],asset_root:f.root});
    assert.equal(result.elements_status,'node_limit');
    assert.equal(result.elements,undefined);
    assert.equal(result.source_sha256,result.restored_sha256);
    assert.equal(result.defect_status,'unverified');
  } finally {await f.close();}
});

test('public source preserves dynamically inserted CSSOM rules offline',async()=>{
  const f=await fixture('<h1>Account settings</h1><script>const style=document.createElement("style");document.head.append(style);style.sheet.insertRule("h1{color:rgb(10,80,120);font-size:48px;margin-left:90px}")</script>');
  try {
    const result=await captureSource(f.page,{url,allowed_hosts:['localhost'],asset_root:f.root});
    assert.equal(result.source_sha256,result.restored_sha256);
    assert.ok(result.html.includes('rgb(10, 80, 120)'));
    assert.ok(!result.html.includes('<script'));
  } finally {await f.close();}
});

test('CSSOM serialization preserves a disabled stylesheet without changing source pixels',async()=>{
  const f=await fixture('<h1>Account settings</h1><style>h1{color:red;font-size:80px}</style><script>document.querySelector("style").sheet.disabled=true</script>');
  try {
    const expected=await f.context.newPage();
    await expected.setContent('<h1>Account settings</h1>');
    const pixels=await expected.screenshot();
    const result=await captureSource(f.page,{url,allowed_hosts:['localhost'],asset_root:f.root});
    assert.ok(result.reference.equals(pixels));
    assert.equal(result.source_sha256,result.restored_sha256);
  } finally {await f.close();}
});

test('public source never sends a chained redirected request to an unapproved host',async()=>{
  let forbidden=0;
  const f=await fixture('<h1>Settings</h1><img src="/special-redirect">',(req,res)=>{
    if(req.url==='/special-redirect') {res.writeHead(302,{location:'/special-hop'});res.end();}
    else if(req.url==='/special-hop') {res.writeHead(302,{location:'https://127.0.0.1/special-private'});res.end();}
    else {forbidden++;res.setHeader('content-type','image/svg+xml');res.end(svg);}
  });
  try {
    const redirected:string[]=[];
    f.page.on('request',request=>{if(/special-hop|special-private/.test(request.url())) redirected.push(request.url());});
    await assert.rejects(captureSource(f.page,{url,allowed_hosts:['localhost'],asset_root:f.root}),/source redirect: use canonical URL/);
    assert.equal(forbidden,0);
    assert.deepEqual(redirected,[]);
  } finally {await f.close();}
});

test('public source explicitly rejects document redirects',async()=>{
  const f=await fixture('<h1>Settings</h1><img src="/image.svg">',(_req,res)=>{
    res.writeHead(302,{location:'/preview/hero'});res.end();
  });
  try {
    await assert.rejects(captureSource(f.page,{url:'https://localhost/special-start',allowed_hosts:['localhost'],asset_root:f.root}),/source redirect: use canonical URL/);
  } finally {await f.close();}
});

test('public source explicitly rejects asset redirects before browser networking',async()=>{
  const f=await fixture('<h1>Settings</h1><img src="/special-image">',(_req,res)=>{
    res.writeHead(302,{location:'/image.svg'});res.end();
  });
  try {
    const redirected:string[]=[];
    f.page.on('request',request=>{if(request.url().endsWith('/image.svg')) redirected.push(request.url());});
    await assert.rejects(captureSource(f.page,{url,allowed_hosts:['localhost'],asset_root:f.root}),/source redirect: use canonical URL/);
    assert.deepEqual(redirected,[]);
  } finally {await f.close();}
});

test('public source rejects oversized raster headers before browser decode',async()=>{
  const png=execFileSync('python',['-c','import io,struct,zlib; from PIL import Image; b=io.BytesIO(); Image.new("RGB",(2,2)).save(b,format="PNG"); c=b.getvalue(); h=b"IHDR"+struct.pack(">II",5000,5000)+c[24:29]; import sys; sys.stdout.buffer.write(c[:12]+h+struct.pack(">I",zlib.crc32(h))+c[33:])']);
  const f=await fixture('<h1>Settings</h1><img src="/special-large.png">',(_req,res)=>{
    res.setHeader('content-type','image/png');res.end(png);
  });
  try {
    await assert.rejects(captureSource(f.page,{url,allowed_hosts:['localhost'],asset_root:f.root}),/source raster/);
  } finally {await f.close();}
});

test('public source stops a chunked oversized script before browser execution',async()=>{
  let sent=0;
  const f=await fixture('<h1>Settings</h1><script src="/special-large.js"></script>',(_req,res)=>{
    res.setHeader('content-type','application/javascript');
    const timer=setInterval(()=>{
      sent+=65536;
      res.write(Buffer.alloc(65536,32));
      if(sent>=12*2**20) {clearInterval(timer);res.end(';window.oversizedExecuted=true;');}
    },1);
    res.on('close',()=>clearInterval(timer));
  });
  try {
    await assert.rejects(captureSource(f.page,{url,allowed_hosts:['localhost'],asset_root:f.root}),/source.*(budget|limit)/);
    assert.ok(sent<12*2**20,`received ${sent} bytes`);
    assert.equal(await f.page.evaluate(()=>Boolean((window as unknown as {oversizedExecuted?:boolean}).oversizedExecuted)),false);
  } finally {await f.close();}
});

test('public source rejects more than 128 concurrent asset URLs',async()=>{
  const html='<h1>Settings</h1>'+Array.from({length:129},(_,i)=>`<img width="1" height="1" src="/special-${i}.svg">`).join('');
  const f=await fixture(html,(_req,res)=>{res.setHeader('content-type','image/svg+xml');res.end(svg);});
  try {
    await assert.rejects(captureSource(f.page,{url,allowed_hosts:['localhost'],asset_root:f.root}),/source asset count limit/);
  } finally {await f.close();}
});

for(const status of [429,503]) for(const resource of ['document','image']) {
  test(`public source treats HTTP ${status} ${resource} as a recoverable operational failure`,async()=>{
    const f=await fixture('<h1>Settings</h1><img src="/special-unavailable">',(_req,res)=>{
      res.writeHead(status);res.end('temporarily unavailable');
    });
    try {
      await assert.rejects(captureSource(f.page,{url:resource==='document'?'https://localhost/special-unavailable':url,
        allowed_hosts:['localhost'],asset_root:f.root}),error=>error instanceof Error&&
          !(error instanceof SourceRejected)&&error.message.includes(`HTTP ${status}`));
    } finally {await f.close();}
  });
}

test('public source waits for asynchronous SPA content before accepting a baseline',async()=>{
  const f=await fixture('<div id="root"></div><script>setTimeout(()=>{document.querySelector("#root").innerHTML="<h1>Ready dashboard</h1>"},1800)</script>');
  try{
    const result=await captureSource(f.page,{url,allowed_hosts:['localhost'],asset_root:f.root});
    assert.ok(result.elements?.some(el=>el.tag==='h1'&&el.text==='Ready dashboard'&&el.state.visible),'empty SPA shell was accepted');
    assert.equal(result.source_sha256,result.restored_sha256);
  }finally{await f.close();}
});

test('transparent shell text and media do not make an empty SPA ready',async()=>{
  const f=await fixture('<p style="opacity:0">Loading</p><canvas style="opacity:0" width="80" height="80"></canvas><div id="root"></div><script>setTimeout(()=>{document.querySelector("#root").innerHTML="<h1>Ready dashboard</h1>"},1800)</script>');
  try{
    await visitSource(f.page,{url,allowed_hosts:['localhost'],asset_root:f.root},async()=>{
      assert.equal(await f.page.locator('h1').isVisible(),true,'transparent shell was accepted');
      assert.equal(await f.page.locator('h1').innerText(),'Ready dashboard');
    });
  }finally{await f.close();}
});

for(const handled of [true,false]){
  test(`HTML returned for a missing avatar requires a complete rendered replacement: ${handled}`,async()=>{
    const handler=handled?'onerror="this.replaceWith(Object.assign(document.createElement(\'span\'),{textContent:\'AB\'}))"':'';
    const f=await fixture(`<h1>Team settings</h1><img src="/special/avatar.png" ${handler}>`,(_req,res)=>{
      res.setHeader('content-type','text/html');res.end('<!doctype html><html><body>Application shell</body></html>');
    });
    try{
      const capture=()=>captureSource(f.page,{url,allowed_hosts:['localhost'],asset_root:f.root});
      if(!handled){await assert.rejects(capture(),/missing source images/);return;}
      const result=await capture();
      assert.equal(result.source_sha256,result.restored_sha256);
      assert.ok(result.elements?.some(el=>el.tag==='span'&&el.text==='AB'&&el.state.visible));
      assert.ok(!result.html.includes('/special/avatar.png'));
      assert.equal(result.assets['https://localhost/special/avatar.png'],undefined);
    }finally{await f.close();}
  });
}

test('HTML returned for a stylesheet is still an unsupported source asset',async()=>{
  const f=await fixture('<h1>Team settings</h1><link rel="stylesheet" href="/special/missing.css">',(_req,res)=>{
    res.setHeader('content-type','text/html');res.end('<!doctype html><html><body>Application shell</body></html>');
  });
  try{await assert.rejects(captureSource(f.page,{url,allowed_hosts:['localhost'],asset_root:f.root}),/unsupported source asset type/);}
  finally{await f.close();}
});
