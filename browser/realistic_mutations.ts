import type {Page} from 'playwright';
import {snapshot, type ElementState} from './snapshot.ts';
import {validate, textProblems} from './validate.ts';
import {componentSnapshot, componentBaselineProblems} from './component_defects.ts';

export const REALISTIC_OPERATORS = ['font_size_regression', 'nowrap_regression', 'word_break_regression',
  'letter_spacing_regression', 'line_height_regression', 'font_family_regression',
  'text_indent_regression', 'relative_offset_regression', 'padding_box_regression'] as const;
export const TEXT_SYMPTOMS = ['text_clip_x', 'text_clip_y', 'viewport_partial_cutoff', 'sibling_occlusion'];

export async function discoverRealistic(page: Page, states: ElementState[], operator: string) {
  const eligible = states.filter(s => !s.intentional && s.fullyInViewport && s.visibleRatio > .99 && s.hitRatio > .95);
  const ids = await page.evaluate(({ids, operator}) => ids.filter(id => {
    const el = document.querySelector<HTMLElement>(`[data-lumen-id="${CSS.escape(id)}"]`);
    if (!el) return false;
    const css = getComputedStyle(el);
    let clippedAncestor = false;
    for (let parent: HTMLElement | null = el; parent && parent !== document.body; parent = parent.parentElement) {
      const style = getComputedStyle(parent);
      clippedAncestor ||= ['hidden', 'clip'].includes(style.overflowX) || ['hidden', 'clip'].includes(style.overflowY);
    }
    if (!clippedAncestor) return false;
    if (operator === 'nowrap_regression') return !['nowrap', 'pre'].includes(css.whiteSpace);
    if (operator === 'word_break_regression') return css.overflowWrap !== 'normal' || css.wordBreak !== 'normal';
    if (operator === 'line_height_regression') return Number.isFinite(parseFloat(css.lineHeight));
    return true;
  }), {ids: eligible.map(s => s.id), operator});
  const selected = new Set(ids);
  return eligible.filter(s => selected.has(s.id));
}

export async function applyRealistic(page: Page, operator: string, target: ElementState,
  before: ElementState[], viewport: [number, number], dpr: number) {
  const initial = await page.evaluate(id => {
    const el = document.querySelector<HTMLElement>(`[data-lumen-id="${CSS.escape(id)}"]`);
    if (!el) throw new Error('mutation target missing');
    const css = getComputedStyle(el);
    return {style: el.getAttribute('style'), fontSize: parseFloat(css.fontSize),
      lineHeight: css.lineHeight === 'normal' ? null : parseFloat(css.lineHeight)};
  }, target.id);
  // Linear sampling does not assume monotonic layout through wrapping transitions.
  const values = ['nowrap_regression', 'word_break_regression'].includes(operator) ? [1] :
    operator === 'font_family_regression' ? [1, 2] : [1.05, 1.1, 1.15, 1.2, 1.3, 1.4, 1.6, 1.8, 2];
  let attempts = 0, accepted: number | null = null;
  let lastValid: number | null = null;
  for (const value of values) {
    attempts++;
    await page.evaluate(({id, operator, initial, value}) => {
      const el = document.querySelector<HTMLElement>(`[data-lumen-id="${CSS.escape(id)}"]`)!;
      if (initial.style === null) el.removeAttribute('style'); else el.setAttribute('style', initial.style);
      if (operator === 'font_size_regression') el.style.fontSize = `${initial.fontSize * value}px`;
      else if (operator === 'letter_spacing_regression') el.style.letterSpacing = `${initial.fontSize * (value - 1) * .3}px`;
      else if (operator === 'line_height_regression') {
        if (initial.lineHeight === null) throw new Error('line height must be explicit');
        el.style.lineHeight = `${initial.lineHeight * value}px`;
      }
      else if (operator === 'font_family_regression') el.style.fontFamily = value === 1 ? 'monospace' : 'serif';
      else if (operator === 'text_indent_regression') el.style.textIndent = `${el.clientWidth * (value - 1) * .5}px`;
      else if (operator === 'relative_offset_regression') {
        el.style.position = 'relative'; el.style.left = `${el.clientWidth * (value - 1) * .5}px`;
      } else if (operator === 'padding_box_regression') el.style.paddingLeft = `${el.clientWidth * (value - 1) * .5}px`;
      else if (operator === 'nowrap_regression') el.style.whiteSpace = 'nowrap';
      else if (operator === 'word_break_regression') {
        el.style.overflowWrap = 'normal'; el.style.wordBreak = 'normal';
      } else throw new Error('unknown realistic mutation');
    }, {id: target.id, operator, initial, value});
    const after = await snapshot(page);
    const findings = validate(before, after, viewport, dpr);
    if (findings.some(f => TEXT_SYMPTOMS.includes(f.symptom))) {
      accepted = value;
      break;
    }
    if (!textProblems(after, viewport).length &&
        !componentBaselineProblems(await componentSnapshot(page)).length) lastValid = value;
  }
  return {operator, mechanism_id: operator, target: target.id, params: {value: accepted},
    cause_region_css: target.rect, symptoms: [] as string[], origin: 'synthetic',
    search: {strategy: 'bounded_linear', attempts, limit: values.length,
      valid_near_boundary: lastValid, broken_near_boundary: accepted}};
}
