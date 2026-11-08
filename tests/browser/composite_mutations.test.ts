import test, {before, after} from 'node:test';
import assert from 'node:assert/strict';
import {chromium, type Browser} from 'playwright';
import {captureCase} from '../../browser/capture.ts';

let browser: Browser;
before(async () => {browser = await chromium.launch();});
after(async () => {await browser.close();});
const operators = ['compound_font_letter', 'compound_font_indent', 'compound_font_offset', 'compound_font_padding',
  'compound_letter_indent', 'compound_letter_offset', 'compound_letter_padding',
  'compound_indent_offset', 'compound_indent_padding', 'compound_offset_padding'];
const markup = (extra = '') => `<body style="margin:32px;font:20px Arial;background:white;color:#111"><div style="width:150px;height:80px;overflow:hidden"><span style="display:block;width:150px;white-space:nowrap;${extra}">Account</span></div></body>`;
for (const operator of operators) {
  const request = {html:markup(), operator, seed:0, viewport:[800,600] as [number,number], dpr:1};
  test(`${operator}: only the combination is defective`, async () => {
    assert.equal((await captureCase(browser, {...request, operator:'clean'})).status, 'accepted_clean');
    const result = await captureCase(browser, request);
    assert.equal(result.status, 'accepted_bug', JSON.stringify(result.receipt));
    assert.ok(result.issues.length > 0);
    assert.equal(result.rollback, true);
    const receipt = result.receipt as {components:{operator:string; declarations:Record<string,string>; standalone_valid:boolean}[]; search:{attempts:number;limit:number}};
    assert.equal(receipt.components.length, 2);
    assert.equal(new Set(receipt.components.map(c => c.operator)).size, 2);
    for (const component of receipt.components) {
      assert.equal(component.standalone_valid, true);
      const css = Object.entries(component.declarations).map(([key, value]) => `${key}:${value}`).join(';');
      const control = await captureCase(browser, {...request, html:markup(css), operator:'clean'});
      assert.equal(control.status, 'accepted_clean', JSON.stringify(control.evidence));
      assert.notEqual(control.current, result.reference);
    }
    assert.ok(receipt.search.attempts <= receipt.search.limit);
    assert.equal((await captureCase(browser, request)).current, result.current);
  });
  test(`${operator}: protected styles do not produce a positive`, async () => {
    const html = '<style>span{font-size:20px!important;letter-spacing:normal!important;text-indent:0!important;left:0!important;padding-left:0!important}</style>'+markup();
    const result = await captureCase(browser, {...request, html});
    assert.equal(result.status, 'no_visible_effect');
    assert.deepEqual(result.issues, []);
  });
}

test('composite search rejects combinations whose font component is already defective', async () => {
  const result = await captureCase(browser, {html:markup().replace('font:20px Arial', 'font:20px/28px Arial'),
    operator:'compound_font_letter', seed:0, viewport:[800,600], dpr:1});
  assert.equal(result.status, 'no_visible_effect');
  assert.deepEqual(result.issues, []);
  assert.equal((result.receipt as {joint_verified:boolean}).joint_verified, false);
});
