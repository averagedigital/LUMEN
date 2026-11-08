import test, {before, after} from 'node:test';
import assert from 'node:assert/strict';
import {chromium, type Browser} from 'playwright';
import {captureCase} from '../../browser/capture.ts';

let browser: Browser;
before(async () => { browser = await chromium.launch({args: ['--disable-partial-raster']}); });
after(async () => { await browser.close(); });
const svg = '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100"><rect width="200" height="100" fill="#1673a8"/><circle cx="100" cy="50" r="35" fill="#ffbb44"/></svg>';
const image = 'data:image/svg+xml;base64,' + Buffer.from(svg).toString('base64');
const html = (style: string) => `<html><body style="margin:24px;background:white"><img src="${image}" style="${style}"></body></html>`;
const request = {seed: 42, viewport: [800, 600] as [number, number], dpr: 1};

test('nontext mutation passes complete capture validation and exact rollback', async () => {
  const payload = {...request, html: html('width:200px;height:100px'), operator: 'image_aspect_distortion'};
  const result = await captureCase(browser, payload);
  assert.equal(result.status, 'accepted_bug', JSON.stringify(result.evidence));
  assert.equal(result.rollback, true);
  assert.equal(result.issues.length, 1);
  assert.equal(result.issues[0].symptom, 'image_aspect_distortion');
  assert.notEqual(result.reference, result.current);
  const replay = await captureCase(browser, payload);
  assert.equal(replay.current, result.current);
  assert.equal(replay.reference, result.reference);
});

test('clean image-only screen is accepted but pre-existing distortion is not', async () => {
  const clean = await captureCase(browser, {...request, html: html('width:200px;height:100px'), operator: 'clean'});
  assert.equal(clean.status, 'accepted_clean');
  assert.deepEqual(clean.issues, []);
  const broken = await captureCase(browser, {...request, html: html('width:400px;height:100px'), operator: 'clean'});
  assert.equal(broken.status, 'invalid_baseline');
});

test('object-fit contain is a valid nontext lookalike', async () => {
  const result = await captureCase(browser, {...request, html: html('width:400px;height:100px;object-fit:contain'), operator: 'clean'});
  assert.equal(result.status, 'accepted_clean');
  assert.deepEqual(result.issues, []);
});

test('component scene limit rejects the scene without killing the worker', async () => {
  const result = await captureCase(browser, {...request, html: '<body>' + '<div></div>'.repeat(5001) + '</body>', operator: 'clean'});
  assert.equal(result.status, 'invalid_baseline');
  assert.deepEqual(result.evidence, {reason: 'component_element_limit'});
});

test('one component mutation groups descendant symptoms without losing their evidence', async () => {
  const markup = '<body style="margin:32px;color:#111;background:white;font:20px Arial"><header style="display:flex;gap:80px;width:650px;height:80px"><span>Project settings</span><span>Account preferences</span></header></body>';
  const result = await captureCase(browser, {...request, html: markup, operator: 'header_collision'});
  assert.equal(result.status, 'accepted_bug', JSON.stringify(result.evidence));
  assert.equal(result.issues.length, 1);
  assert.equal(result.issues[0].symptom, 'header_collision');
  const observations = result.issues[0].evidence.observations as typeof result.issues;
  assert.ok(observations.length > 1);
  for (const finding of observations) {
    assert.equal(finding.evidence.baseline_passed, true);
    assert.equal(finding.evidence.after_failed, true);
    assert.ok(result.issues[0].bbox[0] <= finding.bbox[0] && result.issues[0].bbox[1] <= finding.bbox[1]);
    assert.ok(result.issues[0].bbox[2] >= finding.bbox[2] && result.issues[0].bbox[3] >= finding.bbox[3]);
  }
});
