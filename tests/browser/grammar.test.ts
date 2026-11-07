import test from 'node:test';
import assert from 'node:assert/strict';
import { buildScene, ARCHETYPES } from '../../scenes/grammar.ts';
import { chromium } from 'playwright';
import { captureCase } from '../../browser/capture.ts';

test('page grammar has twenty structural families and deterministic output', () => {
  assert.equal(ARCHETYPES.length, 20);
  const structures = new Set<string>();
  for (const archetype of ARCHETYPES) {
    const first = buildScene({seed: 7, archetype, locale: 'en', width: 800});
    assert.equal(first.html, buildScene({seed: 7, archetype, locale: 'en', width: 800}).html);
    assert.ok(first.html.includes('data-element-id='));
    assert.ok(first.task.target_element_id);
    structures.add(first.html.replace(/>[^<]*</g, '><'));
  }
  assert.equal(structures.size, 20);
});

test('tasks are synthetic and explicitly linked to a real target', () => {
  const scene = buildScene({seed: 2, archetype: 'settings', locale: 'ru', width: 390});
  assert.equal(scene.task.origin, 'synthetic');
  assert.ok(scene.html.includes(`data-element-id="${scene.task.target_element_id}"`));
  assert.ok(!scene.html.includes('http://'));
});

test('each scene has unique stable element IDs even with repeated sections', () => {
  for (const archetype of ARCHETYPES) {
    const scene = buildScene({seed: 42, archetype, locale: 'en', width: 800});
    const ids = [...scene.html.matchAll(/data-element-id="([^"]+)"/g)].map(match => match[1]);
    assert.equal(new Set(ids).size, ids.length, archetype);
  }
});

test('all twenty desktop structures provide a valid baseline', async () => {
  const browser = await chromium.launch({headless: true});
  try {
    for (const archetype of ARCHETYPES) {
      const scene = buildScene({seed: 42, archetype, locale: 'en', width: 1280});
      const result = await captureCase(browser, {html: scene.html, operator: 'clean', seed: 42, viewport: [1280, 800], dpr: 1});
      assert.equal(result.status, 'accepted_clean', `${archetype}: ${JSON.stringify(result.evidence)}`);
    }
  } finally { await browser.close(); }
});
