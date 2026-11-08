import test, {before, after} from 'node:test';
import assert from 'node:assert/strict';
import {chromium, type Browser} from 'playwright';
import {captureCase} from '../../browser/capture.ts';

let browser: Browser;
before(async () => { browser = await chromium.launch({args: ['--disable-partial-raster']}); });
after(async () => { await browser.close(); });
const html = (background = 'Workspace overview') => `<body style="margin:24px;color:#111;background:white;font:18px Arial">
<main>${background}</main><dialog id="confirm" data-element-id="confirm" aria-label="Confirm changes" style="width:min(400px,80vw);box-sizing:border-box;padding:20px;overflow:visible;color:#111;background:white">
<section><h2>Save changes</h2><p>Review the selected records.</p></section><form method="dialog"><button data-element-id="confirm-close" data-lumen-action="close">Confirm</button></form></dialog></body>`;
const request = {seed: 42, viewport: [800, 600] as [number, number], dpr: 1, initialModal: 'confirm', annotations: true};

for (const width of [390, 800]) test(`native modal is a clean negative and closes through a real action at ${width}px`, async () => {
  const payload = {...request, viewport: [width, 720] as [number, number], html: html()};
  const clean = await captureCase(browser, {...payload, operator: 'clean'});
  assert.equal(clean.status, 'accepted_clean', JSON.stringify(clean.evidence));
  assert.equal(clean.elements?.find(e => e.element_id === 'confirm')?.state.modal, true);
  const changed = await captureCase(browser, {...payload, operator: 'benign_action'});
  assert.equal(changed.status, 'accepted_benign_change', JSON.stringify(changed.evidence));
  assert.equal(changed.rollback, true);
  assert.deepEqual(changed.interaction, {origin: 'synthetic', action: 'click', element_id: 'confirm-close'});
  assert.notEqual(changed.reference, changed.current);
  assert.deepEqual(changed.issues, []);
  assert.equal(changed.statesAfter?.find(e => e.element_id === 'confirm')?.state.modal, false);
});

test('closing a modal cannot label a newly revealed invalid screen as a negative', async () => {
  const result = await captureCase(browser, {...request,
    html: html('<span style="display:block;width:20px;white-space:nowrap;overflow:hidden">Broken hidden background</span>'), operator: 'benign_action'});
  assert.equal(result.status, 'unverified');
});

test('a defect inside a modal remains detectable and reproducible', async () => {
  const payload = {...request, html: html(), operator: 'modal_content_overflow'};
  const first = await captureCase(browser, payload);
  assert.equal(first.status, 'accepted_bug', JSON.stringify(first.evidence));
  assert.ok(first.issues.some(i => i.symptom === 'modal_content_overflow'));
  assert.equal(first.rollback, true);
  const second = await captureCase(browser, payload);
  assert.equal(second.reference, first.reference);
  assert.equal(second.current, first.current);
});
