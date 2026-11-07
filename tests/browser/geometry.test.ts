import test from 'node:test';
import assert from 'node:assert/strict';
import { imageBox } from '../../browser/geometry.ts';

test('viewport CSS boxes convert to image pixels with DPR and crop offsets', () => {
  assert.deepEqual(imageBox({x: 10, y: 20, width: 30, height: 40}, 2, [4, 8]), [16, 32, 76, 112]);
});

test('rejects empty and nonfinite geometry', () => {
  assert.throws(() => imageBox({x: 0, y: 0, width: 0, height: 10}, 1));
  assert.throws(() => imageBox({x: NaN, y: 0, width: 10, height: 10}, 1));
});
