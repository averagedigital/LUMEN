import test, { after, before } from 'node:test';
import assert from 'node:assert/strict';
import { chromium, type Browser } from 'playwright';
import { captureCase } from '../../browser/capture.ts';
import { mkdtemp, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createHash } from 'node:crypto';

let browser: Browser;
before(async () => { browser = await chromium.launch({ headless: true }); });
after(async () => { await browser.close(); });

const html = `<html><head><style>
body{margin:24px;background:#fff;color:#17212b;font:18px Arial}
button{display:block;margin:20px 0;padding:12px;border:1px solid #333;background:#fff;color:#17212b}
.label{display:block;white-space:nowrap;width:max-content;line-height:28px}
</style></head><body><h1>Account settings</h1>
<button><span class="label" data-lumen="a">Save account settings</span></button>
<button><span class="label" data-lumen="b">Review notifications</span></button></body></html>`;

for (const operator of ['text_clip_x', 'text_clip_y', 'text_contrast_failure', 'viewport_partial_cutoff', 'sibling_occlusion']) {
  test(`${operator} has independently verified visible effect and rollback`, async () => {
    const result = await captureCase(browser, {html, operator, seed: 42, viewport: [800, 600], dpr: 1});
    assert.equal(result.status, 'accepted_bug', JSON.stringify(result.evidence));
    assert.ok(result.issues.length > 0);
    assert.equal(result.rollback, true);
    assert.notEqual(result.reference, result.current);
    const again = await captureCase(browser, {html, operator, seed: 42, viewport: [800, 600], dpr: 1});
    assert.equal(result.current, again.current);
  });
}

test('clean and benign changes carry no defect boxes', async () => {
  for (const operator of ['clean', 'benign_theme']) {
    const result = await captureCase(browser, {html, operator, seed: 8, viewport: [800, 600], dpr: 1});
    assert.equal(result.status, operator === 'clean' ? 'accepted_clean' : 'accepted_benign_change');
    assert.deepEqual(result.issues, []);
  }
});

test('unavailable target is rejected instead of becoming a negative', async () => {
  const result = await captureCase(browser, {html: '<html><body></body></html>', operator: 'text_clip_y', seed: 1, viewport: [390, 800], dpr: 1});
  assert.equal(result.status, 'inapplicable');
});

test('occlusion without a second visible element is inapplicable', async () => {
  const result = await captureCase(browser, {html: '<body style="background:white;color:black"><h1>Only heading</h1></body>',
    operator: 'sibling_occlusion', seed: 1, viewport: [800, 600], dpr: 1});
  assert.equal(result.status, 'inapplicable');
  assert.deepEqual(result.issues, []);
});

test('intended ellipsis and scrolling are excluded from clipping candidates', async () => {
  const page = html.replace('<h1>Account settings</h1>', '')
    .replace('width:max-content', 'width:80px;overflow:hidden;text-overflow:ellipsis');
  const result = await captureCase(browser, {html: page, operator: 'text_clip_x', seed: 1, viewport: [390, 800], dpr: 1});
  assert.equal(result.status, 'inapplicable');
});

for (const text of ['Сохранить настройки', '保存账户设置', 'حفظ إعدادات الحساب']) {
  test(`mobile DPR 2 capture: ${text}`, async () => {
    const page = html.replace('Save account settings', text).replace('Review notifications', text);
    const result = await captureCase(browser, {html: page, operator: 'text_clip_y', seed: 5, viewport: [390, 800], dpr: 2});
    assert.equal(result.status, 'accepted_bug');
    for (const issue of result.issues) {
      assert.ok(issue.bbox[0] >= 0 && issue.bbox[2] <= 780);
      assert.ok(issue.bbox[1] >= 0 && issue.bbox[3] <= 1600);
    }
  });
}

test('unavailable external image is an invalid baseline', async () => {
  const result = await captureCase(browser, {html: html.replace('</body>', '<img src="https://example.invalid/private.png"></body>'), operator: 'clean', seed: 1, viewport: [800, 600], dpr: 1});
  assert.equal(result.status, 'invalid_baseline');
});

test('partially cut off baseline text is never a clean negative', async () => {
  const page = html.replace('</body>', '<p style="position:fixed;left:760px;top:300px;white-space:nowrap">Important account details</p></body>');
  const result = await captureCase(browser, {html: page, operator: 'clean', seed: 1, viewport: [800, 600], dpr: 1});
  assert.equal(result.status, 'invalid_baseline');
});

test('fixed text clipped by the top viewport edge is not a clean negative', async () => {
  const page = html.replace('</body>', '<p style="position:fixed;left:100px;top:-20px;line-height:30px;margin:0">Important account details</p></body>');
  const result = await captureCase(browser, {html: page, operator: 'clean', seed: 1, viewport: [800, 600], dpr: 1});
  assert.equal(result.status, 'invalid_baseline');
});

test('pointer-events none does not make opaque overlays visually transparent', async () => {
  const page = html.replace('</body>', '<div style="position:fixed;inset:0;background:white;pointer-events:none;z-index:100"></div></body>');
  const result = await captureCase(browser, {html: page, operator: 'clean', seed: 1, viewport: [800, 600], dpr: 1});
  assert.ok(!result.status.startsWith('accepted'));
});

test('invisible ancestor text cannot produce an accepted defect on empty pixels', async () => {
  const page = '<body style="background:white"><div style="opacity:0"><span style="display:block;line-height:30px">Hidden account information</span></div><p>Visible account information</p></body>';
  const result = await captureCase(browser, {html: page, operator: 'text_clip_y', seed: 0, viewport: [800, 600], dpr: 1});
  if (result.status === 'accepted_bug') assert.ok(result.issues.every(issue => issue.id !== '0'));
});

test('native action records a benign transition and optional structural sidecars', async () => {
  const page = html.replace('</body>', '<details><summary data-element-id="help-toggle">Help</summary><p>Account changes are saved locally.</p></details></body>');
  const result = await captureCase(browser, {html: page, operator: 'benign_action', seed: 1,
    viewport: [800, 600], dpr: 1, annotations: true});
  assert.equal(result.status, 'accepted_benign_change');
  assert.equal(result.interaction?.origin, 'synthetic');
  assert.ok(result.elements?.some(e => e.element_id === 'help-toggle' && e.role === 'button'));
  assert.equal(result.rollback, true);
  assert.deepEqual(result.scrollBefore, result.scrollAfter);
});

test('closed details content is not a visible occlusion defect', async () => {
  const page = html.replace('</body>', '<details><summary>Help</summary><p>Hidden help text should not be measured.</p></details></body>');
  const result = await captureCase(browser, {html: page, operator: 'clean', seed: 1, viewport: [800, 600], dpr: 1});
  assert.equal(result.status, 'accepted_clean');
});

test('vertical clipping box stays around text instead of the full block width', async () => {
  const page = '<body style="margin:20px;background:white;color:black;font:20px Arial"><div style="width:700px;text-align:right"><span style="display:block;line-height:30px">Account settings</span></div></body>';
  const result = await captureCase(browser, {html: page, operator: 'text_clip_y', seed: 0, viewport: [800,600], dpr: 1});
  assert.equal(result.status, 'accepted_bug');
  assert.ok(result.issues.every(issue => issue.bbox[2] - issue.bbox[0] < 250));
});

test('offline stylesheet is served only when its checksum matches', async () => {
  const root = await mkdtemp(join(tmpdir(), 'lumen-assets-'));
  const previous = process.env.LUMEN_ASSET_ROOT;
  process.env.LUMEN_ASSET_ROOT = root;
  const css = 'body{background:white;color:black;font:20px Arial}';
  const hash = createHash('sha256').update(css).digest('hex');
  const url = 'https://example.invalid/style.css';
  const request = {html: `<link rel="stylesheet" href="${url}"><h1>Account settings</h1>`,
    operator: 'clean', seed: 0, viewport: [800,600] as [number,number], dpr: 1,
    assets: {[url]: {sha256: hash, content_type: 'text/css'}}};
  try {
    await writeFile(join(root, hash), css);
    assert.equal((await captureCase(browser, request)).status, 'accepted_clean');
    await writeFile(join(root, hash), 'tampered');
    assert.equal((await captureCase(browser, request)).status, 'invalid_baseline');
  } finally {
    if (previous === undefined) delete process.env.LUMEN_ASSET_ROOT;
    else process.env.LUMEN_ASSET_ROOT = previous;
    await rm(root, {recursive: true});
  }
});
