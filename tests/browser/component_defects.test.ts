import test, { before, after } from 'node:test';
import assert from 'node:assert/strict';
import { chromium, type Browser } from 'playwright';
import { COMPONENT_OPERATORS, componentSnapshot, componentBaselineProblems, discoverComponent, applyComponent, validateComponents } from '../../browser/component_defects.ts';
let browser: Browser;
before(async () => { browser = await chromium.launch({ args: ['--disable-partial-raster'] }); });
after(async () => { await browser.close(); });
const art = `<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100"><rect width="200" height="100" fill="#1673a8"/><circle cx="100" cy="50" r="35" fill="#ffbb44"/></svg>`;
const image = 'data:image/svg+xml;base64,' + Buffer.from(art).toString('base64');
const fixtures: Record<string, string> = {
    flex_overflow: '<section data-lumen-component="flex" style="display:flex;width:420px;gap:10px"><button>Account settings</button><button>Export records</button></section>',
    grid_track_collapse: '<section data-lumen-component="grid" style="display:grid;width:420px;grid-template-columns:1fr 1fr;gap:10px"><article>Account settings</article><article>Export records</article></section>',
    container_height_collapse: '<section data-lumen-component="panel" style="width:340px;height:180px"><p>Account settings</p><button>Save preferences</button></section>',
    header_collision: '<header style="display:flex;width:500px;justify-content:space-between"><button>Account settings</button><button>Export records</button></header>',
    table_cell_overlap: '<table style="width:500px"><tbody><tr><td>Account settings</td><td>Export records</td></tr></tbody></table>',
    dropdown_clipping: '<section role="menu" style="width:260px"><button>Account settings</button><button>Export records</button><button>Sign out</button></section>',
    modal_content_overflow: '<section role="dialog" style="width:340px;height:180px"><article>Account settings</article><button>Save preferences</button></section>',
    input_adornment_collision: '<section data-lumen-component="adornment" style="position:relative;width:300px"><input value="Account settings" style="width:300px;padding-left:48px"><span role="img" aria-label="Search" style="position:absolute;left:12px;top:12px;width:22px;height:22px;background:#1673a8">★</span></section>',
    image_aspect_distortion: `<img src="${image}" style="width:200px;height:100px;object-fit:fill">`,
    svg_clipping: art,
};
function pageHTML(body: string) { return `<html><style>body{margin:30px;background:white;color:#17212b;font:18px/1.5 Arial}section,header,table{border:2px solid #39475d;padding:12px;box-sizing:border-box}button,input,td,article{font:18px/1.5 Arial;color:#17212b;background:#eee;border:1px solid #555;padding:10px;box-sizing:border-box}button{white-space:nowrap}section[role=menu]>button{display:block;margin:4px}input{height:48px}svg{display:block}</style><body>${body}</body></html>`; }
for (const [operator, html] of Object.entries(fixtures)) {
    test(`${operator}: baseline, visible effect, independent oracle, replay and rollback`, async () => {
        const outputs: string[] = [];
        for (let replay = 0; replay < 2; replay++) {
            const page = await browser.newPage({ viewport: { width: 900, height: 700 } });
            try {
                await page.setContent(pageHTML(html));
                const before = await componentSnapshot(page);
                assert.deepEqual(componentBaselineProblems(before), [], JSON.stringify(before));
                assert.deepEqual(validateComponents(before, before, [900, 700], 1), []);
                const targets = discoverComponent(before, operator);
                assert.ok(targets.length, operator);
                const reference = await page.screenshot();
                const styles = await page.locator('body,body *').evaluateAll(els => els.map(e => e.getAttribute('style')));
                const receipt = await applyComponent(page, operator, targets[0], before, 2);
                assert.ok(receipt.cause_region_css.width > 0);
                const current = await page.screenshot();
                assert.notDeepEqual(current, reference);
                outputs.push(current.toString('base64'));
                const after = await componentSnapshot(page);
                const findings = validateComponents(before, after, [900, 700], 1);
                assert.equal(findings.length, 1, JSON.stringify({ before, after, findings }));
                assert.equal(findings[0].symptom, operator);
                assert.equal(findings[0].evidence.baseline_passed, true);
                const [x, y, right, bottom] = findings[0].bbox;
                assert.ok(x >= 0 && y >= 0 && right <= 900 && bottom <= 700 && right > x && bottom > y);
                assert.ok(componentBaselineProblems(after).length > 0);
                await page.locator('body,body *').evaluateAll((els, styles) => els.forEach((el, i) => styles[i] === null ? el.removeAttribute('style') : el.setAttribute('style', styles[i]!)), styles);
                assert.deepEqual(await page.screenshot(), reference);
            }
            finally {
                await page.close();
            }
        }
        assert.equal(outputs[0], outputs[1]);
    });
    test(`${operator}: intentional or scrollable lookalike is not a defect`, async () => {
        const page = await browser.newPage({ viewport: { width: 900, height: 700 } });
        try {
            await page.setContent(pageHTML(html));
            const before = await componentSnapshot(page);
            const target = discoverComponent(before, operator)[0];
            assert.ok(target);
            await applyComponent(page, operator, target, before, 2);
            await page.locator(`[data-lumen-component-id="${target.id}"]`).evaluate(el => el.setAttribute('data-lumen-intentional', ''));
            const after = await componentSnapshot(page);
            assert.deepEqual(validateComponents(before, after, [900, 700], 1), []);
        }
        finally {
            await page.close();
        }
    });
}
test('scrollable table, object-fit contain and viewBox SVG are valid without exemption flags', async () => {
    const page = await browser.newPage({ viewport: { width: 900, height: 700 } });
    try {
        await page.setContent(pageHTML(`<section style="overflow:auto;width:240px">${fixtures.table_cell_overlap}</section><img src="${image}" style="width:100px;height:100px;object-fit:contain"><svg width="100" height="100" viewBox="0 0 200 100"><rect width="200" height="100" fill="blue"/></svg>`));
        const states = await componentSnapshot(page);
        assert.deepEqual(componentBaselineProblems(states), []);
        assert.deepEqual(validateComponents(states, states, [900, 700], 1), []);
    }
    finally {
        await page.close();
    }
});
test('operator catalogue contains ten distinct component mechanisms', () => { assert.equal(new Set(COMPONENT_OPERATORS).size, 10); });
for (const [operator, html] of Object.entries(fixtures)) {
    test(`${operator}: ordinary valid counterpart needs no exemption marker`, async () => {
        const page = await browser.newPage({ viewport: { width: 900, height: 700 } });
        try {
            await page.setContent(pageHTML(html));
            const before = await componentSnapshot(page), target = discoverComponent(before, operator)[0];
            assert.ok(target);
            await page.locator(`[data-lumen-component-id="${target.id}"]`).evaluate((el, operator) => {
                const e = el as HTMLElement, children = Array.from(e.children) as HTMLElement[];
                switch (operator) {
                    case 'flex_overflow':
                        e.style.flexWrap = 'wrap';
                        children.forEach(c => c.style.minWidth = '260px');
                        break;
                    case 'grid_track_collapse':
                        e.style.gridTemplateColumns = '160px 1fr';
                        break;
                    case 'container_height_collapse':
                        e.style.height = 'auto';
                        break;
                    case 'header_collision':
                        children[1].style.transform = 'translateX(-4px)';
                        break;
                    case 'table_cell_overlap':
                        (e.querySelector('td') as HTMLElement).style.padding = '18px';
                        break;
                    case 'dropdown_clipping':
                        e.style.maxHeight = '90px';
                        e.style.overflow = 'auto';
                        break;
                    case 'modal_content_overflow':
                        e.style.overflow = 'auto';
                        children[0].style.minHeight = '300px';
                        break;
                    case 'input_adornment_collision':
                        (e.querySelector('input') as HTMLElement).style.paddingLeft = '55px';
                        break;
                    case 'image_aspect_distortion':
                        e.style.width = '300px';
                        e.style.objectFit = 'contain';
                        break;
                    case 'svg_clipping':
                        e.setAttribute('viewBox', '0 0 200 100');
                        e.style.width = '100px';
                        break;
                }
            }, operator);
            const after = await componentSnapshot(page);
            assert.deepEqual(componentBaselineProblems(after), [], JSON.stringify(after));
            assert.deepEqual(validateComponents(before, after, [900, 700], 1), []);
        }
        finally {
            await page.close();
        }
    });
}
test('ordinary viewport crop is not SVG clipping', async () => {
    const page = await browser.newPage({ viewport: { width: 900, height: 700 } });
    try {
        await page.setContent(pageHTML(`<div style="height:620px"></div>${art}`));
        const states = await componentSnapshot(page);
        assert.deepEqual(componentBaselineProblems(states), []);
        assert.deepEqual(discoverComponent(states, 'svg_clipping'), []);
    }
    finally {
        await page.close();
    }
});

for(const conceal of ['opacity:0','visibility:hidden']){
 test(`hidden component children do not create collision: ${conceal}`,async()=>{
  const page=await browser.newPage({viewport:{width:900,height:700}});
  try{
   await page.setContent(pageHTML(`<header style="position:relative;width:500px;height:100px"><button>Visible control</button><button style="position:absolute;left:12px;top:12px;${conceal}">Invisible control</button></header>`));
   const states=await componentSnapshot(page);
   assert.deepEqual(componentBaselineProblems(states),[]);
  }finally{await page.close();}
 });
}

test('dense overlapping components fail at the explicit pair budget',async()=>{
 const page=await browser.newPage({viewport:{width:900,height:700}});
 try{
  await page.setContent(pageHTML(`<header style="position:relative;width:500px;height:100px">${'<span style="position:absolute;left:0;top:0">Dense text</span>'.repeat(700)}</header>`));
  await assert.rejects(()=>componentSnapshot(page),/scene_limit: component overlap pairs/);
 }finally{await page.close();}
});

test('a dense valid table uses the spatial pair budget without false rejection',async()=>{
 const page=await browser.newPage({viewport:{width:900,height:700}});
 try{
  await page.setContent(pageHTML(`<table><tbody>${'<tr><td>First cell</td><td>Second cell</td><td>Third cell</td></tr>'.repeat(200)}</tbody></table>`));
  const states=await componentSnapshot(page);
  assert.deepEqual(componentBaselineProblems(states),[]);
 }finally{await page.close();}
});

test('image padding does not change the rendered intrinsic aspect ratio',async()=>{
 const page=await browser.newPage({viewport:{width:900,height:700}});
 try{
  await page.setContent(pageHTML(`<img src="${image}" style="width:200px;height:100px;padding:50px 20px;box-sizing:content-box;object-fit:fill">`));
  const states=await componentSnapshot(page);
  assert.deepEqual(componentBaselineProblems(states),[]);
 }finally{await page.close();}
});

test('right-aligned input text does not collide with a left adornment', async () => {
    const page = await browser.newPage({ viewport: { width: 900, height: 700 } });
    try {
        await page.setContent(pageHTML(fixtures.input_adornment_collision));
        await page.locator('input').evaluate(el => { el.style.direction = 'rtl'; el.style.textAlign = 'start'; el.style.paddingLeft = '8px'; });
        assert.deepEqual(componentBaselineProblems(await componentSnapshot(page)), []);
    } finally { await page.close(); }
});

const nativeDialog = '<dialog style="width:340px;height:180px;padding:12px;box-sizing:border-box;overflow:visible"><article>Account settings</article><button>Save preferences</button></dialog>';

test('native modal escapes ancestor clipping, excludes inert background and restores exact baseline', async () => {
    const page = await browser.newPage({ viewport: { width: 900, height: 700 } });
    try {
        await page.setContent(pageHTML(`<header><button>Background</button></header><div style="width:20px;height:20px;overflow:hidden">${nativeDialog}</div>`));
        const initial = await page.screenshot();
        await page.locator('dialog').evaluate((el: HTMLDialogElement) => el.showModal());
        const reference = await page.screenshot();
        const before = await componentSnapshot(page);
        assert.equal(before.some(s => s.kind === 'header'), false);
        const target = discoverComponent(before, 'modal_content_overflow')[0];
        assert.ok(target);
        assert.equal(target.visible.width, target.rect.width);
        assert.deepEqual(componentBaselineProblems(before), []);
        const styles = await page.locator('dialog,dialog *').evaluateAll(elements => elements.map(el => el.getAttribute('style')));
        await applyComponent(page, 'modal_content_overflow', target, before, 7);
        const after = await componentSnapshot(page);
        assert.equal(validateComponents(before, after, [900,700], 1).length, 1);
        assert.notDeepEqual(await page.screenshot(), reference);
        await page.locator('dialog,dialog *').evaluateAll((elements, styles) => elements.forEach((el,i) => styles[i] === null ? el.removeAttribute('style') : el.setAttribute('style', styles[i]!)), styles);
        assert.deepEqual(await page.screenshot(), reference);
        await page.locator('dialog').evaluate((el: HTMLDialogElement) => el.close());
        assert.deepEqual(await page.screenshot(), initial);
        assert.equal((await componentSnapshot(page)).some(s => s.kind === 'header'), true);
    } finally { await page.close(); }
});

for (const markup of [nativeDialog.replace('<dialog ', '<dialog open '), '<section role="dialog" aria-modal="true">Accessible dialog</section>']) {
    test(`non-modal markup keeps background components: ${markup.slice(0,35)}`, async () => {
        const page = await browser.newPage({ viewport: { width: 900, height: 700 } });
        try {
            await page.setContent(pageHTML(`<header><button>Background</button></header>${markup}`));
            const states = await componentSnapshot(page);
            assert.equal(states.some(s => s.kind === 'header'), true);
            assert.equal(states.some(s => s.kind === 'modal'), true);
        } finally { await page.close(); }
    });
}

for (const ancestor of [false, true]) {
    test(`rotated image is excluded from aspect verification: ancestor=${ancestor}`, async () => {
        const page = await browser.newPage({ viewport: { width: 900, height: 700 } });
        try {
            const img = `<img src="${image}" style="width:200px;height:100px;${ancestor ? '' : 'transform:rotate(45deg)'}">`;
            await page.setContent(pageHTML(ancestor ? `<div style="transform:rotate(45deg)">${img}</div>` : img));
            const states = await componentSnapshot(page);
            assert.equal(states.some(s => s.kind === 'image'), false);
        } finally { await page.close(); }
    });
}

test('hidden text inside visible wrappers does not cause header collision', async () => {
    const page = await browser.newPage({ viewport: { width: 900, height: 700 } });
    try {
        await page.setContent(pageHTML('<header style="position:relative;width:500px;height:100px"><div>Visible heading</div><div style="position:absolute;left:12px;top:12px"><span style="visibility:hidden">Hidden heading</span><span style="position:absolute;left:250px">Other</span></div></header>'));
        assert.deepEqual(componentBaselineProblems(await componentSnapshot(page)), []);
    } finally { await page.close(); }
});

test('auto-height native modal mutation keeps the baseline container height', async () => {
    const page = await browser.newPage({ viewport: { width: 900, height: 700 } });
    try {
        await page.setContent(pageHTML(nativeDialog.replace('height:180px;', '')));
        await page.locator('dialog').evaluate((el: HTMLDialogElement) => el.showModal());
        const before = await componentSnapshot(page);
        const target = discoverComponent(before, 'modal_content_overflow')[0];
        assert.ok(target);
        const reference = await page.screenshot();
        const styles = await page.locator('dialog,dialog *').evaluateAll(elements => elements.map(el => el.getAttribute('style')));
        await applyComponent(page, 'modal_content_overflow', target, before, 7);
        const after = await componentSnapshot(page);
        assert.equal(validateComponents(before, after, [900,700], 1).length, 1);
        assert.notDeepEqual(await page.screenshot(), reference);
        await page.locator('dialog,dialog *').evaluateAll((elements, styles) => elements.forEach((el,i) => styles[i] === null ? el.removeAttribute('style') : el.setAttribute('style', styles[i]!)), styles);
        assert.deepEqual(await page.screenshot(), reference);
    } finally { await page.close(); }
});
