import { createInterface } from 'node:readline';
import { chromium } from 'playwright';
import { captureCase } from './capture.ts';
import { buildScene } from '../scenes/grammar.ts';

const browser = await chromium.launch({headless: true, args: ['--disable-partial-raster']});
try {
  for await (const line of createInterface({input: process.stdin, crlfDelay: Infinity})) {
    try {
      if (line.length > 2_100_000) throw new Error('request size limit');
      const request = JSON.parse(line);
      const scene = request.scene ? buildScene(request.scene) : null;
      const result = await captureCase(browser, {...request, annotations: Boolean(scene),
        initialModal: scene?.state.modal_element_id, html: scene?.html ?? request.html});
      process.stdout.write(JSON.stringify({...result, scene}) + '\n');
    } catch (error) {
      process.stdout.write(JSON.stringify({status: 'resource_error', issues: [],
        error: error instanceof Error ? error.message.slice(0, 300) : 'worker failed'}) + '\n');
    }
  }
} finally { await browser.close(); }
