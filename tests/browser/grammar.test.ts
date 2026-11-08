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

test('each family exposes distinct deterministic structural variants', () => {
  for (const archetype of ARCHETYPES) {
    const overview = buildScene({seed: 0, archetype, locale: 'en', width: 1280});
    const detailed = buildScene({seed: 15, archetype, locale: 'en', width: 1280});
    assert.notEqual(overview.page.layout_variant, detailed.page.layout_variant, archetype);
    assert.equal(overview.page.structural_family, detailed.page.structural_family);
    const tags = (html: string) => html.replace(/<style>[\s\S]*?<\/style>/g, '').match(/<\/?[a-z][^>]*>/g)?.map(tag => tag.match(/^<\/?[a-z]+/)?.[0]).join('');
    assert.notEqual(tags(overview.html), tags(detailed.html), archetype);
  }
});

test('hard negative components are represented without marking whole pages intentional', () => {
  const scenes = ARCHETYPES.map(archetype => buildScene({seed: 3, archetype, locale: 'en', width: 1280}));
  const combined = scenes.map(scene => scene.html).join('');
  for (const feature of ['text-overflow:ellipsis', 'class="ellipsis"', 'class="avatar-stack"', 'class="badge"', '<dialog', 'position:sticky',
    'role="menu"', 'class="skeleton"', '<button disabled', 'class="image-placeholder"', 'class="input-adornment"', '<fieldset']) {
    assert.ok(combined.includes(feature), feature);
  }
  for (const scene of scenes) assert.ok(!/<(?:body|main)[^>]*data-lumen-intentional/.test(scene.html));
});

test('structural variants render cleanly on desktop and mobile', async () => {
  const browser = await chromium.launch({headless: true, args: ['--disable-partial-raster']});
  try {
    for (const width of [390, 1280]) {
      for (const archetype of ARCHETYPES) {
        const scene = buildScene({seed: 3, archetype, locale: 'en', width});
        const result = await captureCase(browser, {html: scene.html, operator: 'clean', seed: 3, viewport: [width, 960], dpr: 1});
        assert.equal(result.status, 'accepted_clean', `${archetype}/${width}: ${JSON.stringify(result.evidence)}`);
      }
    }
  } finally { await browser.close(); }
});

test('negative scenario tags describe present components', () => {
  const scene = buildScene({seed: 3, archetype: 'search', locale: 'en', width: 390});
  assert.ok(scene.state.hard_negative_types.includes('badge'));
  assert.ok(scene.state.hard_negative_types.includes('image_placeholder'));
  assert.ok(!scene.state.hard_negative_types.includes('dialog'));
  assert.ok(buildScene({seed: 3, archetype: 'settings', locale: 'en', width: 390}).state.hard_negative_types.includes('dialog'));
  assert.ok(buildScene({seed: 3, archetype: 'dashboard', locale: 'en', width: 390}).state.hard_negative_types.includes('scrollable_table'));
  assert.ok(!buildScene({seed: 3, archetype: 'dashboard', locale: 'en', width: 1280}).state.hard_negative_types.includes('scrollable_table'));
  assert.equal(scene.task.origin, 'synthetic');
});

test('detail variants retain clean baselines across locales and typography', async () => {
  const browser = await chromium.launch({headless: true, args: ['--disable-partial-raster']});
  try {
    for (const [index, locale] of ['en', 'ru', 'de', 'es', 'zh', 'ar'].entries()) {
      for (const archetype of ['settings', 'crm', 'shop'] as const) {
        const seed = 3 + index % 3 * 6;
        const scene = buildScene({seed, archetype, locale, width: 390});
        const result = await captureCase(browser, {html: scene.html, operator: 'clean', seed, viewport: [390, 960], dpr: 1, locale});
        assert.equal(result.status, 'accepted_clean', `${archetype}/${locale}/${seed}: ${JSON.stringify(result.evidence)}`);
      }
    }
  } finally { await browser.close(); }
});

test('desktop detail tables provide visible non-scrollable component targets', async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 1280, height: 960}});
    await page.setContent(buildScene({seed: 3, archetype: 'dashboard', locale: 'en', width: 1280}).html);
    const tables = await page.locator('table[data-lumen-component="table"]').evaluateAll(elements => elements.map(table => ({
      overflow: getComputedStyle(table.parentElement!).overflowX,
      cells: table.querySelectorAll('tr:first-child th').length,
      width: table.getBoundingClientRect().width,
      parentWidth: table.parentElement!.getBoundingClientRect().width,
    })));
    assert.ok(tables.length > 0);
    assert.ok(tables.every(table => table.overflow === 'visible' && table.cells >= 2 && table.width <= table.parentWidth));
  } finally { await browser.close(); }
});

test('synthetic confirmation scenes expose a native modal task and valid transition', async () => {
  const browser = await chromium.launch();
  try {
    for (const locale of ['en', 'ru', 'ar']) for (const width of [390, 1280]) {
      const scene = buildScene({seed: 15, archetype: 'settings', locale, width});
      assert.equal(scene.state.modal_element_id, 'confirm-dialog');
      assert.equal(scene.task.target_element_id, 'confirm-close');
      const result = await captureCase(browser, {html: scene.html, initialModal: scene.state.modal_element_id,
        operator: 'benign_action', seed: 42, viewport: [width, 800], dpr: 1, locale});
      assert.equal(result.status, 'accepted_benign_change', `${locale}/${width}: ${JSON.stringify(result.evidence)}`);
      assert.ok(result.scenario_tags?.includes('modal'));
      assert.equal(result.rollback, true);
    }
  } finally { await browser.close(); }
});
