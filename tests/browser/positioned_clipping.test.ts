import test from 'node:test';
import assert from 'node:assert/strict';
import {chromium} from 'playwright';
import {snapshot} from '../../browser/snapshot.ts';

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
