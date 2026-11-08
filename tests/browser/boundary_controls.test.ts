import test, {before, after} from 'node:test';
import assert from 'node:assert/strict';
import {chromium, type Browser} from 'playwright';
import {captureCase} from '../../browser/capture.ts';

let browser: Browser;
before(async () => {browser=await chromium.launch();});
after(async () => {await browser.close();});
const html='<body style="margin:32px;font:20px Arial;background:white;color:#111"><div style="width:150px;height:80px;overflow:hidden"><span style="display:block;width:150px;white-space:nowrap">Account settings</span></div></body>';
test('last valid threshold is emitted as a changed negative with the same baseline', async () => {
  const request={html:html.replaceAll('150px','200px'), operator:'font_size_regression', seed:0, viewport:[800,600] as [number,number], dpr:1};
  const broken=await captureCase(browser, request);
  assert.equal(broken.status,'accepted_bug');
  const control=await captureCase(browser,{...request,operator:'benign_near_boundary',control_of:request.operator});
  assert.equal(control.status,'accepted_benign_change');
  assert.deepEqual(control.issues,[]);
  assert.equal(control.reference,broken.reference);
  assert.notEqual(control.current,control.reference);
  assert.equal(control.rollback,true);
  const receipt=control.receipt as {params:{value:number};control_for:string;search:{broken_near_boundary:number}};
  assert.equal(receipt.control_for,request.operator);
  assert.ok(receipt.params.value < receipt.search.broken_near_boundary);
});
for (const component of [0,1] as const) test(`composite constituent ${component} is emitted as a hard negative`,async()=>{
  const request={html:html.replace('Account settings','Account'),operator:'compound_font_padding',seed:0,viewport:[800,600] as [number,number],dpr:1};
  const broken=await captureCase(browser,request);
  assert.equal(broken.status,'accepted_bug');
  const control=await captureCase(browser,{...request,operator:'benign_composite_control',control_of:request.operator,control_component:component});
  assert.equal(control.status,'accepted_benign_change');
  assert.deepEqual(control.issues,[]);
  assert.equal(control.reference,broken.reference);
  assert.notEqual(control.current,control.reference);
  assert.notEqual(control.current,broken.current);
  assert.equal(control.rollback,true);
});
test('control requests require a compatible explicit mechanism',async()=>{
  const request={html,operator:'benign_near_boundary',seed:0,viewport:[800,600] as [number,number],dpr:1};
  await assert.rejects(captureCase(browser,request),/control/);
  await assert.rejects(captureCase(browser,{...request,control_of:'clean'}),/control/);
  await assert.rejects(captureCase(browser,{...request,operator:'benign_composite_control',control_of:'compound_font_padding'}),/control/);
});
for (const operator of ['benign_near_boundary','benign_composite_control']) test(`${operator}: image-only scenes are inapplicable`,async()=>{
  const image='data:image/svg+xml;base64,'+Buffer.from('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100"><rect width="200" height="100" fill="blue"/></svg>').toString('base64');
  const result=await captureCase(browser,{html:`<img src="${image}" width="200" height="100">`,operator,
    control_of:operator==='benign_near_boundary'?'font_size_regression':'compound_font_padding',control_component:0,
    seed:0,viewport:[800,600],dpr:1});
  assert.equal(result.status,'inapplicable');
  assert.deepEqual(result.issues,[]);
});

test('a valid constituent that reflows scrollable content is never accepted as a bug',async()=>{
  const html='<body style="margin:32px;font:20px Arial;background:white;color:#111"><div style="width:150px;overflow:hidden"><span style="display:block;width:150px;white-space:nowrap">Account</span></div><div style="height:520px"></div><span>Save settings</span></body>';
  const result=await captureCase(browser,{html,operator:'benign_composite_control',control_of:'compound_font_padding',
    control_component:0,seed:0,viewport:[800,600],dpr:1});
  assert.equal(result.status,'accepted_benign_change');
  assert.deepEqual(result.issues,[]);
});
