import test, {before, after} from 'node:test';
import assert from 'node:assert/strict';
import {chromium, type Browser} from 'playwright';
import {captureCase} from '../../browser/capture.ts';

let browser: Browser;
before(async () => {browser = await chromium.launch();});
after(async () => {await browser.close();});
const examples = [
  {operator:'containing_block_removal', html:'<section style="position:relative;width:800px"><div style="position:relative;margin-left:100px;width:300px;height:60px"><span style="position:absolute;right:0;top:10px">Account settings</span></div></section>', prevent:'div{position:relative!important}'},
  {operator:'stacking_context_regression', html:'<section style="position:relative"><div style="width:300px;height:50px"><span style="position:relative;z-index:2">Account settings</span></div><aside style="position:absolute;left:100px;top:0;width:100px;height:24px;background:#ddd;z-index:1"></aside></section>', prevent:'div{isolation:auto!important}'},
  {operator:'flex_min_width_regression', html:'<div style="display:flex;width:240px;overflow:hidden"><span style="flex:1;min-width:0;overflow-wrap:break-word">AccountPreferences</span><span style="flex:0 0 132px;white-space:nowrap">Save changes</span></div>', prevent:'span{min-width:0!important}'},
  {operator:'grid_min_content_regression', html:'<div style="display:grid;grid-template-columns:minmax(0,1fr) 132px;width:240px;overflow:hidden"><span style="overflow-wrap:break-word">AccountPreferences</span><span style="white-space:nowrap">Save changes</span></div>', prevent:'div{grid-template-columns:minmax(0,1fr) 132px!important}'},
];
for (const {operator, html, prevent} of examples) {
  const request = {html:`<body style="margin:32px;background:white;color:#111;font:20px Arial">${html}</body>`,
    operator, seed:0, viewport:[800,600] as [number,number], dpr:1};
  test(`${operator}: cause produces an independently verified symptom and rolls back`, async () => {
    const clean = await captureCase(browser, {...request, operator:'clean'});
    assert.equal(clean.status, 'accepted_clean', JSON.stringify(clean.evidence));
    const result = await captureCase(browser, request);
    assert.equal(result.status, 'accepted_bug', JSON.stringify(result));
    assert.ok(result.issues.length > 0);
    assert.equal(result.rollback, true);
    assert.notEqual(result.reference, result.current);
    assert.equal((await captureCase(browser, request)).current, result.current);
    const receipt = result.receipt as {mechanism_id:string; symptoms:string[]};
    assert.equal(receipt.mechanism_id, operator);
    assert.ok(receipt.symptoms.length > 0);
  });
  test(`${operator}: protected style remains a negative`, async () => {
    const result = await captureCase(browser, {...request, html:`<style>${prevent}</style>${request.html}`});
    assert.ok(['no_visible_effect', 'inapplicable'].includes(result.status), result.status);
    assert.deepEqual(result.issues, []);
  });
  test(`${operator}: ordinary text is not a candidate`, async () => {
    const result = await captureCase(browser, {...request, html:'<body><p>Account settings</p></body>'});
    assert.equal(result.status, 'inapplicable');
    assert.deepEqual(result.issues, []);
  });
}

test('removing a containing block does not label escaped overflow as clipping', async () => {
  const html = '<body style="margin:32px;background:white;color:#111;font:20px Arial"><section style="position:relative"><div style="position:relative;margin-left:100px;width:300px;height:60px;overflow:hidden"><span style="position:absolute;left:60px;top:10px">Account settings</span></div></section></body>';
  const result = await captureCase(browser, {html, operator:'containing_block_removal', seed:0, viewport:[800,600], dpr:1});
  assert.equal(result.status, 'unverified');
  assert.deepEqual(result.issues, []);
  assert.equal(result.rollback, true);
});
