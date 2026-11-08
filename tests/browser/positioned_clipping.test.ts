import test from 'node:test';
import assert from 'node:assert/strict';
import {chromium} from 'playwright';
import {snapshot} from '../../browser/snapshot.ts';
import {validate} from '../../browser/validate.ts';

for (const position of ['absolute', 'fixed']) for (const containing of [false, true]) {
  test(`${position}: overflow clips only at or above its containing block (${containing})`, async () => {
    const browser = await chromium.launch();
    try {
      const page = await browser.newPage({viewport:{width:800,height:600}});
      const block = containing ? (position === 'absolute' ? 'position:relative;' : 'transform:translateX(0);') : '';
      await page.setContent(`<body style="margin:32px;font:20px Arial;background:white;color:#111"><section style="position:relative"><div style="${block}width:100px;height:60px;overflow:hidden"><span style="position:${position};left:0;top:0;white-space:nowrap"><b style="font-weight:normal">Account settings</b></span></div></section></body>`);
      const [state] = await snapshot(page);
      assert.ok(state);
      if (containing) {
        assert.ok(state.visibleRatio < .85 && state.visibleRatio > .1);
        assert.ok(state.hitRatio < 1);
      } else {
        assert.equal(state.visibleRatio, 1);
        assert.equal(state.hitRatio, 1);
      }
    } finally {await browser.close();}
  });
}

for (const fixed of [false,true]) test(`vertical viewport crossing distinguishes flow from fixed content (${fixed})`,async()=>{
  const browser=await chromium.launch();
  try {
    const page=await browser.newPage({viewport:{width:800,height:600}});
    await page.setContent(`<body style="margin:32px;font:20px Arial;background:white;color:#111"><div style="height:540px"></div><span style="${fixed?'position:fixed;top:572px':''}">Save settings</span></body>`);
    const before=await snapshot(page);
    assert.equal(before[0].fullyInViewport,true);
    await page.evaluate(fixed=>{
      if(fixed) document.querySelector('span')!.style.top='590px';
      else document.querySelector('div')!.style.height='558px';
    },fixed);
    const findings=validate(before,await snapshot(page),[800,600],1);
    if(fixed) assert.ok(findings.some(f=>f.symptom==='viewport_partial_cutoff'));
    else assert.deepEqual(findings,[]);
  } finally {await browser.close();}
});
