import {test,before,after} from 'node:test';
import assert from 'node:assert/strict';
import {chromium,type Browser} from 'playwright';
import {captureCase} from '../../browser/capture.ts';
let browser:Browser;
before(async()=>{browser=await chromium.launch({headless:true});});
after(async()=>{await browser.close();});
const shell='<style>body{background:white;color:#111;font:18px Arial;margin:24px}.label{display:block;white-space:nowrap;width:max-content}</style><main id="app"></main>';
const script='<script>setTimeout(()=>{document.getElementById("app").innerHTML="<button><span class=label>Save account settings</span></button>"},100)</script>';
const request={html:shell+script,operator:'text_clip_x',seed:42,viewport:[800,600] as [number,number],dpr:1};
test('JS renders timed content before mutation with exact replay and rollback',async()=>{
  const result=await captureCase(browser,{...request,render_mode:'offline-js'});
  assert.equal(result.status,'accepted_bug',JSON.stringify(result.evidence));
  assert.equal(result.rollback,true);
  assert.equal((await captureCase(browser,{...request,render_mode:'offline-js'})).current,result.current);
  assert.notEqual((await captureCase(browser,request)).status,'accepted_bug');
});
test('JS errors are rejected instead of labeled clean',async()=>{
  const result=await captureCase(browser,{...request,render_mode:'offline-js',operator:'clean',html:shell+'<p>Visible</p><script>throw new Error("broken application")</script>'});
  assert.equal(result.status,'invalid_baseline');
  assert.equal((result.evidence as any).reason,'javascript_error');
});
test('JS network and websocket dependencies cannot escape offline capture',async()=>{
  for(const script of ['fetch("https://example.invalid/private")','new WebSocket("wss://example.invalid/socket")']){
    const result=await captureCase(browser,{...request,render_mode:'offline-js',operator:'clean',html:shell+'<p>Visible</p><script>'+script+'</script>'});
    assert.equal(result.status,'invalid_baseline');
  }
});
test('invalid execution mode fails explicitly',async()=>{
  await assert.rejects(captureCase(browser,{...request,render_mode:'anything' as any}),/invalid capture request/);
});
test('event handler errors after initialization cannot become clean labels',async()=>{
  const result=await captureCase(browser,{...request,render_mode:'offline-js',operator:'clean',
    html:shell+'<button onfocus="throw new Error(\'broken focus\')">Save</button>'});
  assert.equal(result.status,'invalid_baseline');
});
