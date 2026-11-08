import test from 'node:test';
import assert from 'node:assert/strict';
import { chromium } from 'playwright';
import { snapshot } from '../../browser/snapshot.ts';

const html = `<style>body{font:16px Arial;color:#111;background:white}dialog{width:240px;background:white;color:inherit;border:1px solid #111;overflow:visible}p{margin:12px 0}</style>
<p data-element-id="background">Background navigation</p>
<section style="overflow:hidden;width:8px;height:8px"><dialog aria-labelledby="title"><h2 id="title" data-element-id="title">Account settings</h2><p data-element-id="content">Visible dialog content</p><button>Close dialog</button></dialog></section>`;

test('native modal text escapes ancestor clipping and excludes inert background', async () => {
  const browser = await chromium.launch({headless: true});
  try {
    for (const width of [390, 1280]) {
      const page = await browser.newPage({viewport: {width, height: 800}, javaScriptEnabled: false});
      await page.setContent(html);
      await page.evaluate(() => document.querySelector('dialog')!.showModal());
      const states = await snapshot(page);
      assert.ok(!states.some(s => s.id === 'background'));
      const content = states.find(s => s.id === 'content')!;
      assert.ok(content, 'modal content remains observable');
      assert.equal(content.visibleRatio, 1);
      assert.equal(content.hitRatio, 1);
      assert.ok(content.contrast! > 4.5, 'inherited foreground uses modal background');
      assert.equal(content.intentional, false);
      await page.close();
    }
  } finally { await browser.close(); }
});

test('inline dialogs and aria-modal declarations do not hide background from the oracle', async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage();
    for (const markup of ['<dialog open>', '<section role="dialog" aria-modal="true">']) {
      await page.setContent(`<p data-element-id="background">Background content</p>${markup}<p>Dialog content</p>`);
      assert.ok((await snapshot(page)).some(s => s.id === 'background'));
    }
  } finally { await browser.close(); }
});

test('clipped and low-contrast text inside a native modal remain invalid', async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage();
    await page.setContent(html);
    await page.evaluate(() => {
      document.querySelector('dialog')!.showModal();
      Object.assign(document.querySelector<HTMLElement>('[data-element-id="content"]')!.style,
        {width: '60px', overflow: 'hidden', whiteSpace: 'nowrap', color: '#eee'});
    });
    const content = (await snapshot(page)).find(s => s.id === 'content')!;
    assert.ok(content.visibleRatio > .1 && content.visibleRatio < .85);
    assert.ok(content.contrast! < 2);
    assert.equal(content.intentional, false);
  } finally { await browser.close(); }
});

test('only the upper modal is observed when modal opening order differs from DOM order', async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage();
    await page.setContent('<dialog id="first"><p data-element-id="first-text">First dialog</p></dialog><dialog id="second"><p data-element-id="second-text">Second dialog</p></dialog>');
    await page.evaluate(() => {
      document.querySelector<HTMLDialogElement>('#second')!.showModal();
      document.querySelector<HTMLDialogElement>('#first')!.showModal();
    });
    const states = await snapshot(page);
    assert.ok(states.some(s => s.id === 'first-text'));
    assert.ok(!states.some(s => s.id === 'second-text'));
  } finally { await browser.close(); }
});
