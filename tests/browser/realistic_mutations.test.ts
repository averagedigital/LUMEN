import test, {before, after} from 'node:test';
import assert from 'node:assert/strict';
import {chromium, type Browser} from 'playwright';
import {captureCase} from '../../browser/capture.ts';

let browser: Browser;
before(async () => {browser = await chromium.launch({headless: true});});
after(async () => {await browser.close();});

const fixtures = {
  font_size_regression: '<div style="width:240px;height:30px;overflow:hidden"><span style="display:block;white-space:nowrap">Account preferences</span></div>',
  nowrap_regression: '<div style="width:160px;overflow:hidden"><span style="display:block;white-space:normal">Account preferences and notification settings</span></div>',
  word_break_regression: '<div style="width:160px;overflow:hidden"><span style="display:block;overflow-wrap:anywhere">AccountPreferencesAndNotificationSettings</span></div>',
  letter_spacing_regression: '<div style="width:220px;overflow:hidden"><span style="display:block;white-space:nowrap">Account preferences</span></div>',
  line_height_regression: '<div style="width:220px;height:32px;overflow:hidden"><span style="display:block">Account preferences</span></div>',
  font_family_regression: '<div style="width:220px;overflow:hidden"><span style="display:block;white-space:nowrap">Minimum initial limits</span></div>',
  text_indent_regression: '<div style="width:220px;overflow:hidden"><span style="display:block;white-space:nowrap">Account preferences</span></div>',
  relative_offset_regression: '<div style="width:220px;overflow:hidden"><span style="display:block;white-space:nowrap">Account preferences</span></div>',
  padding_box_regression: '<div style="width:220px;height:32px;overflow:hidden"><span style="display:block;box-sizing:border-box;width:220px;white-space:nowrap">Account preferences</span></div>',
};
for (const [operator, content] of Object.entries(fixtures)) {
  const html = `<body style="background:white;color:#111;font:20px/28px Arial;margin:32px">${content}</body>`;
  test(`${operator}: external HTML, semantic boxes, deterministic replay and rollback`, async () => {
    const request = {html, operator, seed: 4, viewport: [800, 600] as [number, number], dpr: 1};
    assert.equal((await captureCase(browser, {...request, operator: 'clean'})).status, 'accepted_clean');
    const result = await captureCase(browser, request);
    assert.equal(result.status, 'accepted_bug', JSON.stringify(result.evidence));
    assert.equal(result.rollback, true);
    assert.notEqual(result.reference, result.current);
    assert.ok(result.issues.some(issue => ['text_clip_x', 'text_clip_y'].includes(issue.symptom)));
    for (const issue of result.issues) {
      assert.ok(issue.bbox[0] >= 0 && issue.bbox[1] >= 0 && issue.bbox[2] <= 800 && issue.bbox[3] <= 600);
      assert.ok(issue.bbox[2] > issue.bbox[0] && issue.bbox[3] > issue.bbox[1]);
    }
    const receipt = result.receipt as {mechanism_id: string; symptoms: string[]; search: {attempts: number; limit: number}};
    assert.equal(receipt.mechanism_id, operator);
    assert.ok(receipt.symptoms.length > 0);
    assert.ok(receipt.search.attempts <= receipt.search.limit);
    assert.equal((await captureCase(browser, request)).current, result.current);
  });
  test(`${operator}: intentional ellipsis is a hard negative`, async () => {
    const request = {html: '<body style="background:white;color:#111;font:20px Arial"><p>Page heading</p><span style="display:block;width:100px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">Account preferences and notifications</span></body>',
      operator, seed: 0, viewport: [800, 600] as [number, number], dpr: 1};
    assert.equal((await captureCase(browser, {...request, operator: 'clean'})).status, 'accepted_clean');
    assert.equal((await captureCase(browser, request)).status, 'inapplicable');
  });
  test(`${operator}: no-op never becomes a positive`, async () => {
    const result = await captureCase(browser, {html: '<body style="background:white;color:#111;font:12px Arial"><span>Short label</span></body>',
      operator, seed: 0, viewport: [1200, 900], dpr: 1});
    assert.ok(['no_visible_effect', 'unverified', 'inapplicable'].includes(result.status), result.status);
    assert.equal(result.issues.length, 0);
  });
}

test('ancestor horizontal clipping keeps its axis and visible bounds', async () => {
  const html = '<body style="background:white;color:#111;font:20px/28px Arial;margin:32px">' + fixtures.relative_offset_regression + '</body>';
  const result = await captureCase(browser, {html, operator: 'relative_offset_regression', seed: 4, viewport: [800,600], dpr: 1});
  assert.equal(result.status, 'accepted_bug');
  assert.equal(result.issues[0].symptom, 'text_clip_x');
  assert.ok(result.issues[0].bbox[2] <= 254, JSON.stringify(result.issues[0]));
});

test('valid near-boundary parameter must preserve neighboring text', async () => {
  const markup = (size = 20) => `<body style="background:white;color:#111;font:20px Arial;margin:32px"><div style="width:240px;height:48px;overflow:hidden"><span style="display:block;white-space:nowrap;font-size:${size}px">Account settings</span><span style="display:block">Save changes</span></div></body>`;
  const request = {html: markup(), operator: 'font_size_regression', seed: 0, viewport: [800,600] as [number,number], dpr: 1};
  const result = await captureCase(browser, request);
  assert.equal(result.status, 'accepted_bug');
  const receipt = result.receipt as {search: {valid_near_boundary: number | null}};
  const value = receipt.search.valid_near_boundary;
  if (value !== null) {
    const valid = await captureCase(browser, {...request, html: markup(20 * value), operator: 'clean'});
    assert.equal(valid.status, 'accepted_clean', JSON.stringify(valid.evidence));
  }
});
