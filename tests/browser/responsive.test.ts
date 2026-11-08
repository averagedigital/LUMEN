import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { chromium, type Browser } from 'playwright';
import { captureCase } from '../../browser/capture.ts';

let browser: Browser;
before(async()=>{browser=await chromium.launch({headless:true,args:['--disable-partial-raster']});});
after(async()=>{await browser.close();});

const html = '<style>body{margin:24px;font:20px Arial;background:white;color:#111}main{display:flex;gap:20px}@media(max-width:600px){main{flex-direction:column}}</style><main><span>Account settings</span><span>Save changes</span></main>';

test('dynamic viewport path preserves final responsive pixels at DPR 2',async()=>{
  const request={html,operator:'clean',seed:0,viewport:[390,844] as [number,number],dpr:2};
  const direct=await captureCase(browser,request);
  const resized=await captureCase(browser,{...request,resize_path:[[1280,720],[844,390]]});
  assert.equal(direct.status,'accepted_clean');
  assert.equal(resized.status,'accepted_clean');
  assert.equal(resized.current,direct.current);
  assert.deepEqual(resized.resize_trace,[[1280,720],[844,390],[390,844]]);
  assert.equal(resized.rollback,true);
});

test('resize path dimensions and budget are validated',async()=>{
  for(const resize_path of [[],[[0,720]],Array(9).fill([800,600])]) {
    await assert.rejects(captureCase(browser,{html,operator:'clean',seed:0,viewport:[390,844],dpr:1,resize_path}),/invalid capture request/);
  }
});

