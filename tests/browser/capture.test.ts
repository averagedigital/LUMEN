import test, { after, before } from 'node:test';
import assert from 'node:assert/strict';
import { chromium, type Browser } from 'playwright';
import { captureCase } from '../../browser/capture.ts';
import { mkdtemp, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createHash } from 'node:crypto';
import { snapshot } from '../../browser/snapshot.ts';
import { apply, discover } from '../../browser/mutations.ts';

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

test('oversized text scene is rejected as a baseline instead of stopping the worker', async () => {
  const result = await captureCase(browser, {html: '<body>' + '<p>Visible text</p>'.repeat(501) + '</body>',
    operator: 'clean', seed: 1, viewport: [800, 600], dpr: 1});
  assert.equal(result.status, 'invalid_baseline');
  assert.deepEqual(result.evidence, {reason: 'text_element_limit'});
});

test('occlusion without a second visible element is inapplicable', async () => {
  const result = await captureCase(browser, {html: '<body style="background:white;color:black"><h1>Only heading</h1></body>',
    operator: 'sibling_occlusion', seed: 1, viewport: [800, 600], dpr: 1});
  assert.equal(result.status, 'inapplicable');
  assert.deepEqual(result.issues, []);
});

for (const [foreground, background] of [['#17212b', '#f8fafc'], ['#f8fafc', '#152520']]) {
  test(`contrast mutation preserves the scene background ${background}`, async () => {
    const page = await browser.newPage({viewport: {width: 800, height: 600}});
    try {
      await page.setContent(`<body style="background:${background};color:${foreground}"><h1>Account settings</h1></body>`);
      const before = await snapshot(page);
      const target = discover(before, 'text_contrast_failure')[0];
      assert.ok(target);
      const original = await page.locator('h1').evaluate(el => getComputedStyle(el).backgroundColor);
      await apply(page, 'text_contrast_failure', target, before, 42);
      assert.equal(await page.locator('h1').evaluate(el => getComputedStyle(el).backgroundColor), original);
      const after = (await snapshot(page)).find(el => el.id === target.id)!;
      assert.ok(after.contrast !== null && after.contrast < 2);
    } finally { await page.close(); }
  });
}

test('sRGB computed colors use normalized channels for contrast', async () => {
  const page = await browser.newPage();
  try {
    await page.setContent('<body style="background:color(srgb 1 1 1);color:color(srgb 0 0 0)"><h1>Account settings</h1></body>');
    const states = await snapshot(page);
    assert.equal(states[0].contrast, 21);
  } finally { await page.close(); }
});

test('horizontal clipping does not move inline text onto another line', async () => {
  const page = await browser.newPage();
  try {
    await page.setContent('<body style="background:white;color:black"><p><span>Project one</span> <span data-element-id="target">Ready for review</span></p></body>');
    const before = await snapshot(page);
    const target = before.find(el => el.id === 'target')!;
    await apply(page, 'text_clip_x', target, before, 42);
    const after = (await snapshot(page)).find(el => el.id === 'target')!;
    assert.ok(Math.abs(after.textRect.y - target.textRect.y) < 1);
    assert.ok(after.visibleRatio < .85);
  } finally { await page.close(); }
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

test('offline image contributes pixels and rejects corruption or unsupported MIME', async () => {
  const root = await mkdtemp(join(tmpdir(), 'lumen-image-'));
  const previous = process.env.LUMEN_ASSET_ROOT;
  process.env.LUMEN_ASSET_ROOT = root;
  const svg = '<svg xmlns="http://www.w3.org/2000/svg" width="80" height="80"><rect width="80" height="80" fill="red"/></svg>';
  const hash = createHash('sha256').update(svg).digest('hex');
  const url = 'https://example.invalid/image.svg';
  const request = {html: `<body style="background:white;color:black;font:20px Arial"><h1>Account settings</h1><img src="${url}" width="80" height="80"></body>`,
    operator: 'clean', seed: 0, viewport: [800,600] as [number,number], dpr: 1,
    assets: {[url]: {sha256: hash, content_type: 'image/svg+xml'}}};
  try {
    await writeFile(join(root, hash), svg);
    const result = await captureCase(browser, request);
    assert.equal(result.status, 'accepted_clean', JSON.stringify(result.evidence));
    const withoutImage = await captureCase(browser, {...request, html: request.html.replace(/<img[^>]+>/, '')});
    assert.equal(withoutImage.status, 'accepted_clean');
    assert.notEqual(result.current, withoutImage.current);
    assert.equal((await captureCase(browser, {...request,
      assets: {[url]: {sha256: hash, content_type: 'text/html'}}})).status, 'invalid_baseline');
    await writeFile(join(root, hash), 'tampered');
    assert.equal((await captureCase(browser, request)).status, 'invalid_baseline');
  } finally {
    if (previous === undefined) delete process.env.LUMEN_ASSET_ROOT;
    else process.env.LUMEN_ASSET_ROOT = previous;
    await rm(root, {recursive: true});
  }
});

test('undecodable checksummed font cannot produce an accepted fallback baseline', async () => {
  const root = await mkdtemp(join(tmpdir(), 'lumen-font-'));
  const previous = process.env.LUMEN_ASSET_ROOT;
  process.env.LUMEN_ASSET_ROOT = root;
  const body = Buffer.from('not a font');
  const hash = createHash('sha256').update(body).digest('hex');
  const url = 'https://example.invalid/font.woff2';
  try {
    await writeFile(join(root, hash), body);
    const result = await captureCase(browser, {
      html: `<style>@font-face{font-family:Test;src:url('${url}') format('woff2')}body{background:white;color:black;font:20px Test,Arial}</style><h1>Account settings</h1>`,
      operator: 'clean', seed: 0, viewport: [800,600], dpr: 1,
      assets: {[url]: {sha256: hash, content_type: 'font/woff2'}}});
    assert.equal(result.status, 'invalid_baseline');
    assert.deepEqual(result.evidence, {reason: 'invalid_font'});
  } finally {
    if (previous === undefined) delete process.env.LUMEN_ASSET_ROOT;
    else process.env.LUMEN_ASSET_ROOT = previous;
    await rm(root, {recursive: true});
  }
});

test('absent optional local font does not invalidate a source-declared system fallback', async () => {
  const result = await captureCase(browser, {
    html: '<style>@font-face{font-family:Optional;src:local("LumenDefinitelyMissingFont")}body{background:white;color:black;font:20px Optional,Arial}</style><h1>Account settings</h1>',
    operator: 'clean', seed: 0, viewport: [800,600], dpr: 1});
  assert.equal(result.status, 'accepted_clean', JSON.stringify(result.evidence));
});
