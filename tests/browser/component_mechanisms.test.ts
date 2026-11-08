import test, {before, after} from 'node:test';
import assert from 'node:assert/strict';
import {chromium, type Browser} from 'playwright';
import {captureCase} from '../../browser/capture.ts';

let browser: Browser;
before(async () => {browser = await chromium.launch();});
after(async () => {await browser.close();});
const image = 'data:image/svg+xml;base64,' + Buffer.from('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100"><rect width="200" height="100" fill="blue"/><circle cx="100" cy="50" r="30" fill="orange"/></svg>').toString('base64');
const examples = [
  {operator:'flex_wrap_regression', symptom:'flex_overflow', html:'<section style="display:flex;flex-wrap:wrap;width:240px;gap:12px"><span style="width:180px;flex-shrink:0">Account settings</span><span style="width:180px;flex-shrink:0">Project settings</span></section>'},
  {operator:'header_negative_margin', symptom:'header_collision', html:'<header style="display:flex;gap:30px;width:460px;height:50px"><span>Account settings</span><span>Project settings</span></header>'},
  {operator:'table_fixed_layout_regression', symptom:'table_cell_overlap', html:'<table style="width:400px;white-space:nowrap"><tr><td>Account notification preferences</td><td>Ready</td></tr></table>'},
  {operator:'dropdown_ancestor_overflow', symptom:'dropdown_clipping', html:'<section style="width:240px;height:95px"><nav role="menu" style="width:220px;height:120px"><p>Account settings</p><p>Project settings</p><p>Save changes</p></nav></section>'},
  {operator:'image_object_fit_regression', symptom:'image_aspect_distortion', html:`<img src="${image}" style="width:200px;height:160px;object-fit:contain">`},
  {operator:'svg_viewbox_removal', symptom:'svg_clipping', html:'<svg width="120" height="100" viewBox="0 0 200 100"><rect width="200" height="100" fill="blue"/><circle cx="100" cy="50" r="30" fill="orange"/></svg>'},
];
for (const example of examples) {
  test(`${example.operator}: independent symptom, replay and rollback`, async () => {
    const request = {html:`<body style="margin:32px;background:white;color:#111;font:20px/24px Arial">${example.html}</body>`,
      operator:example.operator, seed:0, viewport:[800,600] as [number,number], dpr:1};
    const clean = await captureCase(browser, {...request, operator:'clean'});
    assert.equal(clean.status, 'accepted_clean', JSON.stringify(clean.evidence));
    assert.deepEqual(clean.issues, []);
    const result = await captureCase(browser, request);
    assert.equal(result.status, 'accepted_bug', JSON.stringify({evidence:result.evidence, receipt:result.receipt, issues:result.issues}));
    assert.ok(result.issues.some(f => f.symptom === example.symptom));
    assert.equal(result.rollback, true);
    assert.notEqual(result.reference, result.current);
    assert.equal((await captureCase(browser, request)).current, result.current);
    for (const issue of result.issues) {
      assert.ok(issue.bbox[0] >= 0 && issue.bbox[1] >= 0 && issue.bbox[2] <= 800 && issue.bbox[3] <= 600);
    }
  });
  test(`${example.operator}: unsuitable HTML is inapplicable`, async () => {
    const result = await captureCase(browser, {html:'<body><p>Account settings</p></body>',
      operator:example.operator, seed:0, viewport:[800,600], dpr:1});
    assert.equal(result.status, 'inapplicable');
    assert.deepEqual(result.issues, []);
  });
  test(`${example.operator}: constrained no-op does not produce a positive`, async () => {
    const prevent = {
      flex_wrap_regression:'section{flex-wrap:wrap!important}',
      header_negative_margin:'header>*{margin-left:0!important}',
      table_fixed_layout_regression:'table{table-layout:auto!important}',
      dropdown_ancestor_overflow:'section{overflow:visible!important}',
      image_object_fit_regression:'img{object-fit:contain!important}',
      svg_viewbox_removal:'',
    }[example.operator];
    const html = example.operator === 'svg_viewbox_removal' ? example.html.replace('width="120"', 'width="200"') : example.html;
    const result = await captureCase(browser, {html:`<style>${prevent}</style><body style="margin:32px;background:white;color:#111;font:20px/24px Arial">${html}</body>`,
      operator:example.operator, seed:0, viewport:[800,600], dpr:1});
    assert.equal(result.status, 'no_visible_effect');
    assert.deepEqual(result.issues, []);
  });
}

test('reset object-fit does not create distortion at intrinsic aspect ratio', async () => {
  const result = await captureCase(browser, {html:`<img src="${image}" style="width:200px;height:100px;object-fit:contain">`,
    operator:'image_object_fit_regression', seed:0, viewport:[800,600], dpr:1});
  assert.equal(result.status, 'no_visible_effect');
  assert.deepEqual(result.issues, []);
});

test('flex discovery does not hide an existing header collision', async () => {
  const html = `<body style="background:white;color:#111;font:20px Arial;margin:32px"><header style="display:flex;width:400px;height:100px"><img src="${image}" style="width:200px;height:100px"><img src="${image}" style="width:200px;height:100px;margin-left:-50px"></header>${examples[0].html}</body>`;
  const result = await captureCase(browser, {html, operator:'flex_wrap_regression', seed:1, viewport:[800,600], dpr:1});
  assert.equal(result.status, 'invalid_baseline');
});
