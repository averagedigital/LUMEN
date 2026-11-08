import test from 'node:test';
import assert from 'node:assert/strict';
import { chromium } from 'playwright';
import { annotations, visibleScenarios } from '../../browser/annotations.ts';

const fixtures: [string, string][] = [
  ['ellipsis', '<p class="ellipsis" style="width:50px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">A long truncated sentence</p>'],
  ['scrollable_table', '<div style="width:100px;overflow:auto"><table style="width:300px"><tr><td>First column</td><td>Second column</td></tr></table></div>'],
  ['overlapping_avatars', '<div class="avatar-stack" style="display:flex"><span class="avatar" style="width:40px;height:40px;background:blue">AB</span><span class="avatar" style="width:40px;height:40px;background:red;margin-left:-10px">CD</span></div>'],
  ['badge', '<div style="position:relative;width:100px"><div style="height:80px;background:gray"></div><span class="badge" data-lumen-intentional style="position:absolute;top:0;right:0;background:white">New</span></div>'],
  ['dialog', '<dialog open style="position:static">Account settings</dialog>'],
  ['dropdown', '<details class="dropdown" open><summary>Settings</summary><nav role="menu"><button>Export</button></nav></details>'],
  ['skeleton', '<div class="skeleton" role="status"><i style="display:block;background:gray;width:100px;height:20px"></i></div>'],
  ['disabled_control', '<button disabled>Save changes</button>'],
  ['image_placeholder', '<figure class="image-placeholder" role="img" style="width:100px;height:100px;background:#ddd">No image selected</figure>'],
  ['sticky_header', '<header style="position:sticky;top:0">Main navigation</header>'],
];

test('scenario tags require a visible component and disappear when hidden, offscreen or covered', async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 390, height: 800}});
    for (const [tag, markup] of fixtures) {
      await page.setContent(`<main>${markup}</main>`);
      assert.ok((await visibleScenarios(page)).includes(tag), `visible ${tag}`);
      for (const style of ['display:none', 'opacity:0', 'position:absolute;top:1200px']) {
        await page.locator('main').evaluate((el, style) => el.setAttribute('style', style), style);
        assert.ok(!(await visibleScenarios(page)).includes(tag), `${style}: ${tag}`);
      }
      await page.locator('main').evaluate(el => el.removeAttribute('style'));
      await page.evaluate(() => {
        const cover = document.createElement('div');
        cover.style.cssText = 'position:fixed;inset:0;background:white;z-index:999';
        document.body.append(cover);
      });
      assert.ok(!(await visibleScenarios(page)).includes(tag), `covered ${tag}`);
    }
  } finally { await browser.close(); }
});

test('actual overflow and overlap are required; closed dropdown and CSS declarations are insufficient', async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage();
    await page.setContent(`<p class="ellipsis" style="text-overflow:ellipsis">Short</p>
      <div style="overflow:auto;width:400px"><table><tr><td>Fits</td></tr></table></div>
      <div class="avatar-stack"><span class="avatar">AB</span> <span class="avatar">CD</span></div>
      <span class="badge" data-lumen-intentional>Normal inline badge</span>
      <details class="dropdown"><summary>Closed menu</summary><nav>Export</nav></details>
      <div role="dialog" aria-modal="true">Declared modality</div>`);
    const tags = await visibleScenarios(page);
    for (const tag of ['ellipsis', 'scrollable_table', 'overlapping_avatars', 'badge', 'dropdown', 'modal', 'responsive']) {
      assert.ok(!tags.includes(tag), tag);
    }
  } finally { await browser.close(); }
});

test('native modal scopes tags to its own visible content without hiding defects', async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage();
    await page.setContent('<div class="skeleton" role="status" style="height:30px;background:gray">Loading</div><dialog><button disabled>Save changes</button><p style="color:white;background:white">Invalid contrast</p></dialog>');
    await page.evaluate(() => document.querySelector('dialog')!.showModal());
    const tags = await visibleScenarios(page);
    assert.ok(tags.includes('modal'));
    assert.ok(tags.includes('disabled_control'));
    assert.ok(!tags.includes('skeleton'));
    assert.ok(!tags.includes('dialog'));
  } finally { await browser.close(); }
});

test('nested modal annotations block interaction with the lower dialog', async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage();
    await page.setContent('<dialog id="outer"><button data-element-id="lower">Lower action</button><dialog id="inner"><button data-element-id="upper">Upper action</button></dialog></dialog>');
    await page.evaluate(() => {
      document.querySelector<HTMLDialogElement>('#outer')!.showModal();
      document.querySelector<HTMLDialogElement>('#inner')!.showModal();
    });
    const states = await annotations(page);
    assert.equal(states.find(s => s.element_id === 'lower')!.state.interaction_blocked, true);
    assert.equal(states.find(s => s.element_id === 'upper')!.state.interaction_blocked, false);
  } finally { await browser.close(); }
});
