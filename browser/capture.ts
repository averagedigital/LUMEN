import type { Browser, Page } from 'playwright';
import { snapshot } from './snapshot.ts';
import { apply, discover, OPERATORS, type Operator } from './mutations.ts';
import { validate, type Finding } from './validate.ts';
import { annotations } from './annotations.ts';

export type Request = { html: string; operator: string; seed: number; viewport: [number, number]; dpr: number; locale?: string; annotations?: boolean };
export type CaptureResult = {
  status: string; issues: Finding[]; evidence?: unknown; reference?: string; current?: string;
  rollback?: boolean; receipt?: unknown; browser?: string; timings?: Record<string, number>;
  elements?: Awaited<ReturnType<typeof annotations>>;
  statesAfter?: Awaited<ReturnType<typeof annotations>>;
  interaction?: {origin: string; action: string; element_id: string};
  scrollBefore?: number[]; scrollAfter?: number[];
};

async function stable(page: Page): Promise<Buffer | null> {
  const first = await page.screenshot({animations: 'disabled', caret: 'hide', timeout: 15000});
  const second = await page.screenshot({animations: 'disabled', caret: 'hide', timeout: 15000});
  return first.equals(second) ? first : null;
}

export async function captureCase(browser: Browser, request: Request): Promise<CaptureResult> {
  const {html, seed, viewport, dpr} = request;
  if (!OPERATORS.includes(request.operator as Operator)) throw new Error('unknown operator');
  if (!Number.isSafeInteger(seed) || seed < 0 || html.length > 2_000_000 || ![1, 2].includes(dpr) ||
      viewport.length !== 2 || viewport.some(x => !Number.isInteger(x) || x < 240 || x > 3840) ||
      viewport[0] * viewport[1] * dpr ** 2 > 16_777_216) throw new Error('invalid capture request');
  const started = performance.now();
  const context = await browser.newContext({viewport: {width: viewport[0], height: viewport[1]},
    deviceScaleFactor: dpr, locale: request.locale ?? 'en-US', timezoneId: 'UTC',
    colorScheme: 'light', reducedMotion: 'reduce', serviceWorkers: 'block', javaScriptEnabled: false,
    acceptDownloads: false});
  const blocked: string[] = [];
  await context.route('**/*', route => { blocked.push(new URL(route.request().url()).protocol); return route.abort(); });
  const deadline = setTimeout(() => { void context.close().catch(() => {}); }, 45000);
  try {
    const page = await context.newPage();
    page.setDefaultTimeout(15000);
    await page.setContent(html, {waitUntil: 'load', timeout: 15000});
    await page.evaluate(async () => { await document.fonts.ready; });
    await page.evaluate(() => {
      const style = document.createElement('style');
      style.textContent = '*,*::before,*::after{animation:none!important;transition:none!important;caret-color:transparent!important}';
      document.head.append(style);
    });
    const imagesReady = await page.evaluate(() => Array.from(document.images).every(i => i.complete && i.naturalWidth > 0));
    if (!imagesReady || blocked.length) return {status: 'invalid_baseline', issues: [], evidence: {reason: 'missing_assets', blocked: blocked.length}};
    if (request.operator === 'benign_action') {
      const target = page.locator('summary[data-element-id="help-toggle"]');
      if (await target.count() !== 1) return {status: 'inapplicable', issues: []};
      await target.scrollIntoViewIfNeeded();
    }
    if (request.operator === 'clean') {
      await page.evaluate(seed => {
        const controls = Array.from(document.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled),summary'));
        controls[seed % controls.length]?.focus({preventScroll: true});
      }, seed);
    }
    const scrollBefore = await page.evaluate(() => [scrollX, scrollY]);
    const elements = request.annotations ? await annotations(page) : undefined;
    const before = await snapshot(page);
    const candidates = discover(before, request.operator);
    if (candidates.length === 0) return {status: 'inapplicable', issues: []};
    const baselineProblems = before.filter(s => !s.intentional &&
      ((s.fullyInViewport && (s.visibleRatio < .98 || s.hitRatio < .9 || (s.contrast !== null && s.contrast < 2))) ||
       (s.textRect.y < viewport[1] && s.textRect.y + s.textRect.height > 0 &&
        s.textRect.x < viewport[0] && s.textRect.x + s.textRect.width > 0 &&
        (s.textRect.x < 0 || s.textRect.x + s.textRect.width > viewport[0] ||
         (s.viewportBound && (s.textRect.y < 0 || s.textRect.y + s.textRect.height > viewport[1]))))));
    if (baselineProblems.length) return {status: 'invalid_baseline', issues: [], evidence: baselineProblems};
    const reference = await stable(page);
    if (!reference) return {status: 'unstable', issues: [], evidence: {reason: 'baseline_capture'}};
    const captured = performance.now();
    const styles = await page.evaluate(() => Array.from(document.querySelectorAll<HTMLElement>('body,body *'))
      .map(el => el.getAttribute('style')));
    const operator = request.operator as Operator;
    let receipt: unknown = null;
    let interaction: CaptureResult['interaction'];
    const detailsBefore = await page.evaluate(() => Array.from(document.querySelectorAll('details')).map(e => e.open));
    const activeBefore = await page.evaluate(() => Array.from(document.querySelectorAll('body,body *')).indexOf(document.activeElement!));
    if (operator === 'benign_theme') {
      await page.evaluate(() => { document.body.style.backgroundColor = '#eff6ff'; });
    } else if (operator === 'benign_action') {
      await page.locator('summary[data-element-id="help-toggle"]').click();
      await page.evaluate(scroll => window.scrollTo(scroll[0], scroll[1]), scrollBefore);
      interaction = {origin: 'synthetic', action: 'click', element_id: 'help-toggle'};
    } else if (operator !== 'clean') {
      receipt = await apply(page, operator, candidates[seed % candidates.length], before, seed);
    }
    const after = await snapshot(page);
    const statesAfter = request.annotations ? await annotations(page) : undefined;
    const scrollAfter = await page.evaluate(() => [scrollX, scrollY]);
    const issues = validate(before, after, viewport, dpr);
    const current = await stable(page);
    if (!current) return {status: 'unstable', issues: [], evidence: {reason: 'current_capture'}};
    const verified = performance.now();
    await page.evaluate(styles => Array.from(document.querySelectorAll<HTMLElement>('body,body *')).forEach((el, i) => {
      if (styles[i] === null) el.removeAttribute('style'); else el.setAttribute('style', styles[i]);
    }), styles);
    await page.evaluate(({detailsBefore, activeBefore, scrollBefore}) => {
      document.querySelectorAll('details').forEach((el, i) => { el.open = detailsBefore[i]; });
      if (activeBefore >= 0) document.querySelectorAll<HTMLElement>('body,body *')[activeBefore]?.focus({preventScroll: true});
      else (document.activeElement as HTMLElement)?.blur();
      window.scrollTo(scrollBefore[0], scrollBefore[1]);
    }, {detailsBefore, activeBefore, scrollBefore});
    const restored = await stable(page);
    if (!restored?.equals(reference)) return {status: 'unstable', issues: [], evidence: {reason: 'rollback_failed'}};
    const changed = !reference.equals(current);
    let status = 'unverified';
    if (operator === 'clean' && !changed && issues.length === 0) status = 'accepted_clean';
    else if (operator.startsWith('benign_') && changed && issues.length === 0) status = 'accepted_benign_change';
    else if (changed && issues.some(issue => issue.symptom === operator)) status = 'accepted_bug';
    else if (!changed) status = 'no_visible_effect';
    return {status, issues, receipt, reference: reference.toString('base64'), current: current.toString('base64'),
      rollback: true, browser: browser.version(), elements, statesAfter, interaction, scrollBefore, scrollAfter,
      timings: {baseline: captured - started,
        mutation_capture_oracle: verified - captured, rollback: performance.now() - verified}};
  } finally { clearTimeout(deadline); await context.close(); }
}
