import {test,before,after} from 'node:test';
import assert from 'node:assert/strict';
import {chromium,type Browser} from 'playwright';
import {capturePreparedPage} from '../../browser/capture.ts';
import {sourceDocument} from '../../browser/source_capture.ts';
let browser:Browser;
before(async()=>{browser=await chromium.launch({headless:true,args:['--disable-partial-raster']});});
after(async()=>{await browser.close();});
const html='<style>body{background:white;color:#111;font:20px Arial;margin:24px}.label{display:block;white-space:nowrap;width:max-content}</style><canvas width="80" height="80"></canvas><span class="label">Save account settings</span><script>window.sessionReady=true;const c=document.querySelector("canvas").getContext("2d");c.fillStyle="red";c.fillRect(0,0,80,80)</script>';
const request={operator:'text_clip_x',seed:42,viewport:[800,600] as [number,number],dpr:1};

test('live mutation preserves the loaded application and canvas with exact rollback',async()=>{
  const context=await browser.newContext({viewport:{width:800,height:600}}),page=await context.newPage();
  try{
    await page.setContent(html);const before=await page.screenshot({animations:'disabled',caret:'hide'});
    const result=await capturePreparedPage(page,request);
    assert.equal(result.status,'accepted_bug',JSON.stringify(result.evidence));
    assert.equal(result.rollback,true);assert.equal(result.capture_mode,'live-session');
    assert.ok(Buffer.from(result.reference!,'base64').equals(before));
    assert.ok((await page.screenshot({animations:'disabled',caret:'hide'})).equals(before));
    assert.equal(await page.evaluate(()=>Boolean((window as any).sessionReady)),true);
    assert.equal(page.isClosed(),false);
  }finally{await context.close();}
});

test('new network requests are frozen during live mutation and original routing is restored',async()=>{
  const context=await browser.newContext({viewport:{width:800,height:600}}),page=await context.newPage();let calls=0;
  await page.route('https://example.invalid/**',async route=>{calls++;await route.fulfill({body:'ok'});});
  try{
    await page.setContent(html+'<script>window.observer=new MutationObserver(()=>fetch("https://example.invalid/update").catch(()=>{}));window.observer.observe(document.querySelector(".label"),{attributes:true})</script>');
    const result=await capturePreparedPage(page,request);
    assert.equal(result.status,'accepted_bug');assert.equal(calls,0);
    assert.ok(result.live_blocked_requests!>0);
    await page.evaluate(async()=>{(window as any).observer.disconnect();await fetch('https://example.invalid/after');});
    assert.equal(calls,1);
  }finally{await context.close();}
});

test('live baseline rejects viewport or DPR mismatch before mutation',async()=>{
  const context=await browser.newContext({viewport:{width:800,height:600}}),page=await context.newPage();
  try{
    await page.setContent(html);
    await assert.rejects(capturePreparedPage(page,{...request,dpr:2}),/profile mismatch/);
    await assert.rejects(capturePreparedPage(page,{...request,viewport:[390,844]}),/profile mismatch/);
    assert.equal(page.isClosed(),false);
  }finally{await context.close();}
});

test('live event errors and unrelated DOM drift cannot become accepted labels',async()=>{
  for(const content of [html+'<button onfocus="throw new Error(\'broken focus\')">Save</button>',
    html+'<p id="status">Original</p><script>new MutationObserver(()=>document.getElementById("status").textContent="Changed application state").observe(document.querySelector(".label"),{attributes:true})</script>']){
    const context=await browser.newContext({viewport:{width:800,height:600}}),page=await context.newPage();
    try{
      await page.setContent(content);
      const result=await capturePreparedPage(page,{...request,operator:content.includes('onfocus')?'clean':'text_clip_x'});
      assert.ok(!result.status.startsWith('accepted'),result.status);
    }finally{await context.close();}
  }
});

test('live resize uses the loaded JS application and preserves the requested capture profile',async()=>{
  const context=await browser.newContext({viewport:{width:1280,height:720},deviceScaleFactor:2}),page=await context.newPage();
  try{
    await page.setContent(html+'<p id="width">Initial</p><script>addEventListener("resize",()=>document.getElementById("width").textContent="Width: "+innerWidth)</script>');
    const result=await capturePreparedPage(page,{...request,viewport:[390,844],dpr:2,resize_path:[[1280,720],[390,844]]});
    assert.equal(result.status,'accepted_bug',JSON.stringify(result.evidence));
    assert.equal(await page.locator('#width').textContent(),'Width: 390');
    assert.deepEqual(page.viewportSize(),{width:390,height:844});
  }finally{await context.close();}
});

test('a failure after mutation disposes a live page whose rollback cannot be confirmed',async()=>{
  const context=await browser.newContext({viewport:{width:800,height:600}}),page=await context.newPage();
  try{
    await page.setContent(html+'<script>new MutationObserver(()=>{window.getComputedStyle=()=>{throw new Error("application rejected style")}}).observe(document.querySelector(".label"),{attributes:true,attributeFilter:["style"]})</script>');
    await assert.rejects(capturePreparedPage(page,request),/application rejected style/);
    assert.equal(page.isClosed(),true,'an unconfirmed mutation must not survive into the next capture');
  }finally{await context.close();}
});

test('live inspection snapshot strips scripts and private form values without changing the application',async()=>{
  const context=await browser.newContext({viewport:{width:800,height:600}}),page=await context.newPage();
  try{
    await page.setContent(html+'<input value="private-form-value"><button data-token="private-token" onclick="window.clicked=true">Continue</button>');
    const snapshot=await sourceDocument(page,true);
    assert.ok(!snapshot.includes('<script'));
    assert.ok(!snapshot.includes('private-form-value'));assert.ok(!snapshot.includes('private-token'));
    assert.equal(await page.locator('input').inputValue(),'private-form-value');
    await page.locator('button').click();assert.equal(await page.evaluate(()=>Boolean((window as any).clicked)),true);
  }finally{await context.close();}
});
