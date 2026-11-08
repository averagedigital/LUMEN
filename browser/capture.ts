import type { Browser, Page } from 'playwright';
import { snapshot } from './snapshot.ts';
import { apply, discover, OPERATORS, type Operator } from './mutations.ts';
import { validate, textProblems, type Finding } from './validate.ts';
import { annotations, visibleScenarios } from './annotations.ts';
import { COMPONENT_OPERATORS, componentSnapshot, componentBaselineProblems,
  discoverComponent, applyComponent, validateComponents, componentSymptom } from './component_defects.ts';
import { readFile, stat } from 'node:fs/promises';
import { join } from 'node:path';
import { createHash } from 'node:crypto';
import { REALISTIC_OPERATORS, BOUNDARY_OPERATORS, TEXT_SYMPTOMS, discoverRealistic, applyRealistic } from './realistic_mutations.ts';
import { COMPOSITE_OPERATORS, applyComposite } from './composite_mutations.ts';
import { RESPONSIVE_OPERATORS, applyResponsive, breakpointCatalog } from './responsive_mutations.ts';

export type Request = { html: string; operator: string; seed: number; viewport: [number, number]; dpr: number; locale?: string; annotations?: boolean; initialModal?: string;
  render_mode?: 'static' | 'offline-js';
  resize_path?: number[][];
  inspect_breakpoints?: boolean;
  control_of?: string; control_component?: 0 | 1;
  assets?: Record<string, {sha256: string; content_type: string}> };
export type CaptureResult = {
  status: string; issues: Finding[]; evidence?: unknown; reference?: string; current?: string;
  rollback?: boolean; receipt?: unknown; browser?: string; timings?: Record<string, number>;
  elements?: Awaited<ReturnType<typeof annotations>>;
  statesAfter?: Awaited<ReturnType<typeof annotations>>;
  interaction?: {origin: string; action: string; element_id: string};
  scrollBefore?: number[]; scrollAfter?: number[];
  scenario_tags?: string[];
  resize_trace?: number[][];
  responsive_breakpoints?: Awaited<ReturnType<typeof breakpointCatalog>>;
};

async function stable(page: Page): Promise<Buffer | null> {
  const first = await page.screenshot({animations: 'disabled', caret: 'hide', timeout: 15000});
  const second = await page.screenshot({animations: 'disabled', caret: 'hide', timeout: 15000});
  return first.equals(second) ? first : null;
}

async function componentIssues(page: Page, findings: Finding[], target: string, operator: string): Promise<Finding[]> {
  const root = findings.find(f => f.id === target && f.symptom === operator);
  if (!root) return findings;
  const descendants = await page.evaluate(target => {
    const element = document.querySelector(`[data-lumen-component-id="${CSS.escape(target)}"]`);
    return element ? [element, ...element.querySelectorAll('*')].flatMap(el =>
      ['data-lumen-id', 'data-lumen-component-id'].map(name => el.getAttribute(name)).filter(id => id !== null)) : [];
  }, target);
  const ids = new Set(descendants);
  const related = findings.filter(f => ids.has(f.id));
  if (related.length < 2) return findings;
  const bbox = [...root.bbox] as Finding['bbox'];
  for (const finding of related) {
    bbox[0] = Math.min(bbox[0], finding.bbox[0]); bbox[1] = Math.min(bbox[1], finding.bbox[1]);
    bbox[2] = Math.max(bbox[2], finding.bbox[2]); bbox[3] = Math.max(bbox[3], finding.bbox[3]);
  }
  return [{...root, bbox, evidence: {...root.evidence, observations: related}}, ...findings.filter(f => !ids.has(f.id))];
}

export async function captureCase(browser: Browser, request: Request): Promise<CaptureResult> {
  const {html, seed, viewport, dpr} = request;
  const javascript = request.render_mode === 'offline-js';
  if (request.render_mode !== undefined && !['static', 'offline-js'].includes(request.render_mode)) throw new Error('invalid capture request');
  if (!OPERATORS.includes(request.operator as Operator)) throw new Error('unknown operator');
  const boundaryControl = request.operator === 'benign_near_boundary';
  const compositeControl = request.operator === 'benign_composite_control';
  if ((boundaryControl && !BOUNDARY_OPERATORS.includes(request.control_of ?? '')) ||
      (compositeControl && (!COMPOSITE_OPERATORS.includes(request.control_of ?? '') ||
        ![0,1].includes(request.control_component as number)))) throw new Error('invalid control request');
  const mechanism = boundaryControl || compositeControl ? request.control_of! : request.operator;
  if ((request.inspect_breakpoints !== undefined && typeof request.inspect_breakpoints !== 'boolean') ||
      !Number.isSafeInteger(seed) || seed < 0 || html.length > 2_000_000 || ![1, 2].includes(dpr) ||
      viewport.length !== 2 || viewport.some(x => !Number.isInteger(x) || x < 240 || x > 3840) ||
      viewport[0] * viewport[1] * dpr ** 2 > 16_777_216) throw new Error('invalid capture request');
  if (request.resize_path !== undefined && (!Array.isArray(request.resize_path) ||
      request.resize_path.length < 1 || request.resize_path.length > 8 ||
      request.resize_path.some(size => !Array.isArray(size) || size.length !== 2 ||
        size.some(x => !Number.isInteger(x) || x < 240 || x > 3840) ||
        size[0] * size[1] * dpr ** 2 > 16_777_216))) throw new Error('invalid capture request');
  const initial = request.resize_path?.[0] ?? viewport;
  const started = performance.now();
  const context = await browser.newContext({viewport: {width: initial[0], height: initial[1]},
    deviceScaleFactor: dpr, locale: request.locale ?? 'en-US', timezoneId: 'UTC',
    colorScheme: 'light', reducedMotion: 'reduce', serviceWorkers: 'block', javaScriptEnabled: javascript,
    acceptDownloads: false});
  const blocked: string[] = [];
  const fonts = new Map<string, Buffer>();
  let javascriptErrors = 0;
  if (javascript) await context.routeWebSocket('**/*', socket => { blocked.push('websocket'); socket.close(); });
  await context.route('**/*', async route => {
    const asset = request.assets?.[route.request().url()];
    if (asset && process.env.LUMEN_ASSET_ROOT && /^[a-f0-9]{64}$/.test(asset.sha256) &&
        ['text/css', 'font/woff2', 'image/jpeg', 'image/png', 'image/svg+xml', 'image/webp',
          ...(javascript ? ['text/javascript', 'application/javascript'] : [])].includes(asset.content_type)) {
      try {
        const path = join(process.env.LUMEN_ASSET_ROOT, asset.sha256);
        if ((await stat(path)).size > 8 * 1024 * 1024) throw new Error('asset size limit');
        const body = await readFile(path);
        if (createHash('sha256').update(body).digest('hex') !== asset.sha256) throw new Error('asset checksum');
        if (asset.content_type === 'font/woff2') fonts.set(asset.sha256, body);
        return await route.fulfill({status: 200, contentType: asset.content_type,
          headers: {'access-control-allow-origin': '*'}, body});
      } catch { blocked.push('invalid_local_asset'); return route.abort(); }
    }
    blocked.push(new URL(route.request().url()).protocol);
    return route.abort();
  });
  const deadline = setTimeout(() => { void context.close().catch(() => {}); }, 45000);
  try {
    const page = await context.newPage();
    page.setDefaultTimeout(15000);
    page.on('pageerror', () => { javascriptErrors++; });
    if (javascript) {
      // Offline JS has a fixed initialization window; later timers stay paused.
      await page.clock.install({time: new Date('2026-01-01T00:00:00Z')});
      await page.clock.pauseAt(new Date('2026-01-01T00:00:01Z'));
      await page.evaluate(seed => {
        let state = seed >>> 0;
        Math.random = () => { state = (Math.imul(1664525, state) + 1013904223) >>> 0; return state / 4294967296; };
      }, seed);
    }
    await page.setContent(html, {waitUntil: 'load', timeout: 15000});
    if (javascript) await page.clock.runFor(1000);
    if (request.resize_path) {
      for (const size of [...request.resize_path.slice(1), viewport]) {
        await page.setViewportSize({width: size[0], height: size[1]});
        if (javascript) await page.clock.runFor(100);
        await page.evaluate(async () => { await document.fonts.ready; });
      }
      await page.evaluate(async () => {
        await Promise.all(Array.from(document.images).map(image => image.decode().catch(() => {})));
      });
    }
    await page.evaluate(async () => { await document.fonts.ready; });
    for (const body of fonts.values()) {
      const valid = await page.evaluate(async encoded => {
        try {
          await new FontFace('LumenAssetCheck', Uint8Array.from(atob(encoded), c => c.charCodeAt(0))).load();
          return true;
        } catch { return false; }
      }, body.toString('base64'));
      if (!valid) return {status: 'invalid_baseline', issues: [], evidence: {reason: 'invalid_font'}};
    }
    await page.evaluate(() => {
      const style = document.createElement('style');
      style.textContent = '*,*::before,*::after{animation:none!important;transition:none!important;caret-color:transparent!important}';
      document.head.append(style);
    });
    if (javascriptErrors) return {status: 'invalid_baseline', issues: [], evidence: {reason: 'javascript_error', count: javascriptErrors}};
    const imagesReady = await page.evaluate(() => Array.from(document.images).every(i => i.complete && i.naturalWidth > 0));
    if (!imagesReady || blocked.length) return {status: 'invalid_baseline', issues: [], evidence: {reason: 'missing_assets', blocked: blocked.length}};
    if (request.initialModal) {
      await page.evaluate(id => {
        const dialog = document.getElementById(id);
        if (!(dialog instanceof HTMLDialogElement) || dialog.open) throw new Error('invalid initial modal');
        dialog.showModal();
      }, request.initialModal);
    }
    const actionSelector = request.initialModal ? 'dialog:modal [data-lumen-action="close"]' : 'summary[data-element-id="help-toggle"]';
    if (request.operator === 'benign_action') {
      const target = page.locator(actionSelector);
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
    const scenario_tags = await visibleScenarios(page);
    const elements = request.annotations ? await annotations(page) : undefined;
    const before = await snapshot(page);
    const discoverLayouts = request.operator === 'flex_wrap_regression';
    const componentsBefore = await componentSnapshot(page, discoverLayouts);
    const componentOperator = COMPONENT_OPERATORS.some(name => name === request.operator);
    const componentCandidates = discoverComponent(componentsBefore, request.operator);
    const compositeOperator = COMPOSITE_OPERATORS.includes(mechanism);
    const responsiveOperator = RESPONSIVE_OPERATORS.some(name => name === request.operator);
    const realisticOperator = compositeOperator || REALISTIC_OPERATORS.some(name => name === mechanism);
    const candidates = realisticOperator ? await discoverRealistic(page, before, mechanism) : discover(before, request.operator);
    const baselineProblems = textProblems(before, viewport);
    if (baselineProblems.length) return {status: 'invalid_baseline', issues: [], evidence: baselineProblems};
    const componentProblems = componentBaselineProblems(componentsBefore);
    if (componentProblems.length) return {status: 'invalid_baseline', issues: [], evidence: componentProblems};
    const negative = request.operator === 'clean' || request.operator.startsWith('benign_');
    if (!responsiveOperator && (componentOperator ? !componentCandidates.length : !candidates.length && !(negative && !realisticOperator && componentsBefore.length))) {
      return {status: 'inapplicable', issues: []};
    }
    const reference = await stable(page);
    if (!reference) return {status: 'unstable', issues: [], evidence: {reason: 'baseline_capture'}};
    const responsive_breakpoints = request.inspect_breakpoints ? await breakpointCatalog(page) : undefined;
    const captured = performance.now();
    const styles = await page.evaluate(() => Array.from(document.querySelectorAll<HTMLElement>('body,body *'))
      .map(el => el.getAttribute('style')));
    const svgViewBoxes = request.operator === 'svg_viewbox_removal' ? await page.evaluate(() =>
      Array.from(document.querySelectorAll('svg')).map(el => el.getAttribute('viewBox'))) : null;
    const operator = request.operator as Operator;
    let receipt: unknown = null;
    let restoreResponsive: (() => Promise<void>) | undefined;
    let interaction: CaptureResult['interaction'];
    const detailsBefore = await page.evaluate(() => Array.from(document.querySelectorAll('details')).map(e => e.open));
    const dialogsBefore = await page.evaluate(() => Array.from(document.querySelectorAll('dialog')).map(e =>
      ({open: e.open, modal: e.matches(':modal'), returnValue: e.returnValue})));
    const activeBefore = await page.evaluate(() => Array.from(document.querySelectorAll('body,body *')).indexOf(document.activeElement!));
    if (operator === 'benign_theme') {
      await page.evaluate(() => { document.body.style.backgroundColor = '#eff6ff'; });
    } else if (operator === 'benign_action') {
      const elementId = await page.locator(actionSelector).getAttribute('data-element-id');
      if (!elementId) throw new Error('action target has no stable id');
      await page.locator(actionSelector).click();
      await page.evaluate(scroll => window.scrollTo(scroll[0], scroll[1]), scrollBefore);
      interaction = {origin: 'synthetic', action: 'click', element_id: elementId};
    } else if (responsiveOperator) {
      const mutation = await applyResponsive(page, operator, before, viewport, dpr, seed, request.resize_path);
      if (!mutation.receipt) return {status:'inapplicable',issues:[],evidence:mutation.diagnostic};
      receipt = mutation.receipt;
      restoreResponsive = mutation.restore;
      const ready = await page.evaluate(async () => {
        await document.fonts.ready;
        const results = await Promise.allSettled(Array.from(document.images).map(image => image.decode()));
        return results.every(result => result.status === 'fulfilled');
      });
      if (!ready || blocked.length) return {status:'unverified',issues:[],evidence:{reason:'responsive_assets_unavailable'}};
    } else if (componentOperator) {
      receipt = await applyComponent(page, operator, componentCandidates[seed % componentCandidates.length], componentsBefore, seed);
    } else if (compositeOperator) {
      receipt = await applyComposite(page, mechanism, candidates[seed % candidates.length], before, viewport, dpr,
        compositeControl ? request.control_component : undefined);
    } else if (realisticOperator) {
      receipt = await applyRealistic(page, mechanism, candidates[seed % candidates.length], before, viewport, dpr, boundaryControl);
    } else if (operator !== 'clean') {
      receipt = await apply(page, operator, candidates[seed % candidates.length], before, seed);
    }
    const after = await snapshot(page);
    const statesAfter = request.annotations ? await annotations(page) : undefined;
    const scrollAfter = await page.evaluate(() => [scrollX, scrollY]);
    const componentsAfter = await componentSnapshot(page, discoverLayouts);
    let issues = [...validate(before, after, viewport, dpr),
      ...validateComponents(componentsBefore, componentsAfter, viewport, dpr)];
    const validAfter = !negative || (!textProblems(after, viewport).length && !componentBaselineProblems(componentsAfter).length);
    if (componentOperator && receipt) {
      issues = await componentIssues(page, issues, (receipt as {target: string}).target, componentSymptom(operator));
    }
    if (realisticOperator && receipt) {
      const mutation = receipt as Awaited<ReturnType<typeof applyRealistic>>;
      mutation.symptoms = [...new Set(issues.filter(issue => TEXT_SYMPTOMS.includes(issue.symptom)).map(issue => issue.symptom))];
    }
    const current = await stable(page);
    if (!current) return {status: 'unstable', issues: [], evidence: {reason: 'current_capture'}};
    const verified = performance.now();
    if (restoreResponsive) await restoreResponsive();
    await page.evaluate(styles => Array.from(document.querySelectorAll<HTMLElement>('body,body *')).forEach((el, i) => {
      if (styles[i] === null) el.removeAttribute('style'); else el.setAttribute('style', styles[i]);
    }), styles);
    if (svgViewBoxes) await page.evaluate(values => document.querySelectorAll('svg').forEach((el, i) => {
      if (values[i] === null) el.removeAttribute('viewBox'); else el.setAttribute('viewBox', values[i]!);
    }), svgViewBoxes);
    await page.evaluate(({detailsBefore, dialogsBefore, activeBefore, scrollBefore}) => {
      document.querySelectorAll('details').forEach((el, i) => { el.open = detailsBefore[i]; });
      document.querySelectorAll('dialog').forEach((el, i) => {
        const old = dialogsBefore[i];
        if (el.open !== old.open || el.matches(':modal') !== old.modal) {
          if (el.open) el.close();
          if (old.open) { if (old.modal) el.showModal(); else el.show(); }
        }
        el.returnValue = old.returnValue;
      });
      if (activeBefore >= 0) document.querySelectorAll<HTMLElement>('body,body *')[activeBefore]?.focus({preventScroll: true});
      else (document.activeElement as HTMLElement)?.blur();
      window.scrollTo(scrollBefore[0], scrollBefore[1]);
    }, {detailsBefore, dialogsBefore, activeBefore, scrollBefore});
    await page.mouse.move(0, 0);
    const restored = await stable(page);
    if (!restored?.equals(reference)) return {status: 'unstable', issues: [], evidence: {reason: 'rollback_failed'}};
    const changed = !reference.equals(current);
    let status = 'unverified';
    if (operator === 'clean' && !changed && issues.length === 0) status = 'accepted_clean';
    else if (operator.startsWith('benign_') && changed && issues.length === 0 && validAfter) status = 'accepted_benign_change';
    else if (!negative && changed && issues.some(issue => responsiveOperator || (realisticOperator ?
      TEXT_SYMPTOMS.includes(issue.symptom) :
      issue.symptom === componentSymptom(operator)))) status = 'accepted_bug';
    else if (!changed) status = 'no_visible_effect';
    if (javascriptErrors || blocked.length) return {status: 'invalid_baseline', issues: [],
      evidence: {reason: javascriptErrors ? 'javascript_error' : 'missing_assets', blocked: blocked.length, count: javascriptErrors}};
    return {status, issues, receipt, reference: reference.toString('base64'), current: current.toString('base64'),
      rollback: true, browser: browser.version(), elements, statesAfter, interaction, scrollBefore, scrollAfter, scenario_tags,
      ...(request.resize_path ? {resize_trace: [...request.resize_path, viewport]} : {}),
      ...(responsive_breakpoints ? {responsive_breakpoints} : {}),
      timings: {baseline: captured - started,
        mutation_capture_oracle: verified - captured, rollback: performance.now() - verified}};
  } catch (error) {
    if (error instanceof Error && ['scene_limit: media rules', 'scene_limit: media breakpoints',
      'scene_limit: nested media rules'].some(message => error.message.includes(message))) {
      return {status: 'inapplicable', issues: [], evidence: {reason: 'responsive_rule_limit'}};
    }
    if (error instanceof Error && error.message.includes('scene_limit: text elements')) {
      return {status: 'invalid_baseline', issues: [], evidence: {reason: 'text_element_limit'}};
    }
    if (error instanceof Error && error.message.includes('scene_limit: component')) {
      return {status: 'invalid_baseline', issues: [], evidence: {reason: 'component_element_limit'}};
    }
    throw error;
  } finally { clearTimeout(deadline); await context.close(); }
}
