import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { chromium, type Browser } from 'playwright';
import { captureCase } from '../../browser/capture.ts';

let browser: Browser;
before(async()=>{browser=await chromium.launch({headless:true,args:['--disable-partial-raster']});});
after(async()=>{await browser.close();});

const html = '<style>body{margin:24px;font:20px Arial;background:white;color:#111}main{display:flex;gap:20px}@media(max-width:600px){main{flex-direction:column}}</style><main><span>Account settings</span><span>Save changes</span></main>';

const breakpointHTML = '<style>body{margin:20px;font:20px Arial;background:white;color:#111}main{display:flex;gap:20px;overflow:hidden}.item{flex:0 0 400px;white-space:nowrap}@media(max-width:600px){main{flex-direction:column}.item{flex-basis:auto}}</style><main><span class="item">Account settings</span><span class="item">Save changes to profile</span></main>';

test('a one-pixel breakpoint shift creates a verified boundary defect and rolls back',async()=>{
  const request={html:breakpointHTML,operator:'breakpoint_shift_regression',seed:0,viewport:[600,700] as [number,number],dpr:2};
  assert.equal((await captureCase(browser,{...request,operator:'clean'})).status,'accepted_clean');
  const result=await captureCase(browser,request);
  assert.equal(result.status,'accepted_bug');
  assert.ok(result.issues.some(i=>i.symptom==='text_clip_x'));
  assert.equal((result.receipt as any).params.delta,-1);
  assert.equal(result.rollback,true);
  assert.equal((await captureCase(browser,request)).current,result.current);
  for(const issue of result.issues)assert.ok(issue.bbox[2]<=1200&&issue.bbox[3]<=1400);
});

test('valid responsive reflow is not a breakpoint defect',async()=>{
  const result=await captureCase(browser,{html,operator:'breakpoint_shift_regression',seed:0,viewport:[600,700],dpr:1});
  assert.notEqual(result.status,'accepted_bug');
  assert.deepEqual(result.issues,[]);
  assert.equal(result.rollback,true);
});

test('missing pixel breakpoint is inapplicable',async()=>{
  const result=await captureCase(browser,{html:'<p>Account settings</p>',operator:'breakpoint_shift_regression',seed:0,viewport:[600,700],dpr:1});
  assert.equal(result.status,'inapplicable');
});

test('CSS rule budget excludes one scene without breaking subsequent captures',async()=>{
  const huge='<style>'+Array.from({length:3001},(_,i)=>`.unused${i}{color:black}`).join('')+'</style><p>Account settings</p>';
  const request={html:huge,operator:'breakpoint_shift_regression',seed:0,viewport:[600,700] as [number,number],dpr:1};
  const result=await captureCase(browser,request);
  assert.equal(result.status,'inapplicable');
  assert.deepEqual(result.evidence,{reason:'responsive_rule_limit'});
  assert.equal((await captureCase(browser,{...request,html,operator:'clean'})).status,'accepted_clean');
});

test('stale cached container width breaks only after dynamic resize',async()=>{
  const html='<style>body{margin:24px;font:20px Arial;background:white;color:#111}main{display:flex;justify-content:space-between}span{white-space:nowrap}</style><main><span>Settings</span><span>Save changes</span></main>';
  const request={html,operator:'resize_stale_width_regression',seed:0,viewport:[390,844] as [number,number],dpr:1,resize_path:[[480,720]]};
  assert.equal((await captureCase(browser,{...request,operator:'clean'})).status,'accepted_clean');
  const result=await captureCase(browser,request);
  assert.equal(result.status,'accepted_bug');
  assert.ok(result.issues.some(i=>i.symptom==='viewport_partial_cutoff'));
  assert.equal(result.rollback,true);
  assert.equal((await captureCase(browser,request)).current,result.current);
  const noResize=await captureCase(browser,{...request,resize_path:[[390,844]]});
  assert.notEqual(noResize.status,'accepted_bug');
  assert.deepEqual(noResize.issues,[]);
});
