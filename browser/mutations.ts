import type { Page } from 'playwright';
import type { ElementState } from './snapshot.ts';

export const OPERATORS = ['text_clip_x', 'text_clip_y', 'text_contrast_failure',
  'viewport_partial_cutoff', 'sibling_occlusion', 'clean', 'benign_theme'] as const;
export type Operator = typeof OPERATORS[number];
export type Receipt = { operator: string; target: string; params: Record<string, unknown> };

export function discover(states: ElementState[], operator: string): ElementState[] {
  return states.filter(s => !s.intentional && s.fullyInViewport && s.visibleRatio > .99 &&
    s.hitRatio > .95 && s.rect.width > 40 && s.rect.height > 12 &&
    (operator !== 'text_contrast_failure' || (s.contrast !== null && s.contrast >= 4.5 && s.background !== null)));
}

export async function apply(page: Page, operator: Operator, target: ElementState, states: ElementState[], seed: number): Promise<Receipt> {
  const ratio = [.45, .6, .7][seed % 3];
  const params: Record<string, unknown> = {ratio};
  if (operator === 'sibling_occlusion') {
    const other = states.find(s => s.id !== target.id && s.fullyInViewport && s.hitRatio > .95 && !s.intentional && s.background);
    if (!other) throw new Error('inapplicable: second element');
    params.other = other.id;
    params.background = other.background;
  }
  await page.evaluate(({operator, target, params}) => {
    const el = document.querySelector<HTMLElement>(`[data-lumen-id="${target.id}"]`)!;
    const ratio = Number(params.ratio);
    if (operator === 'text_clip_x' || operator === 'text_clip_y') {
      Object.assign(el.style, {display: 'block', overflow: 'hidden', whiteSpace: 'nowrap',
        boxSizing: 'border-box', minWidth: '0', minHeight: '0', flexShrink: '0'});
      if (operator === 'text_clip_x') el.style.width = `${target.textRect.width * ratio}px`;
      else el.style.height = `${target.textRect.height * ratio}px`;
    } else if (operator === 'text_contrast_failure') {
      el.style.color = '#c5c5c5';
      el.style.backgroundColor = '#d8d8d8';
    } else if (operator === 'viewport_partial_cutoff') {
      Object.assign(el.style, {position: 'fixed', left: `${innerWidth - target.textRect.width * ratio}px`,
        top: `${target.rect.y}px`, width: `${target.rect.width}px`, zIndex: '10', whiteSpace: 'nowrap'});
    } else if (operator === 'sibling_occlusion') {
      const other = document.querySelector<HTMLElement>(`[data-lumen-id="${params.other}"]`)!;
      Object.assign(other.style, {position: 'fixed', left: `${target.textRect.x + target.textRect.width * .25}px`,
        top: `${target.textRect.y}px`, width: `${target.rect.width}px`, zIndex: '10',
        backgroundColor: String(params.background), whiteSpace: 'nowrap'});
    }
  }, {operator, target, params});
  return {operator, target: target.id, params};
}
