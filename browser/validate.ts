import type { ElementState } from './snapshot.ts';
import { imageBox, type Box } from './geometry.ts';

export type Finding = { id: string; symptom: string; family: string; bbox: Box; evidence: Record<string, unknown> };

export function textProblems(states: ElementState[], viewport: [number, number]) {
  return states.filter(s => !s.intentional &&
    ((s.fullyInViewport && (s.visibleRatio < .98 || s.hitRatio < .9 || (s.contrast !== null && s.contrast < 2))) ||
     (s.textRect.y < viewport[1] && s.textRect.y + s.textRect.height > 0 &&
      s.textRect.x < viewport[0] && s.textRect.x + s.textRect.width > 0 &&
      (s.textRect.x < 0 || s.textRect.x + s.textRect.width > viewport[0] ||
       (s.viewportBound && (s.textRect.y < 0 || s.textRect.y + s.textRect.height > viewport[1]))))));
}

export function validate(before: ElementState[], after: ElementState[], viewport: [number, number], dpr: number): Finding[] {
  const findings: Finding[] = [];
  for (const state of after) {
    const old = before.find(s => s.id === state.id);
    if (!old || old.intentional || state.intentional || !old.fullyInViewport || old.visibleRatio < .99 || old.hitRatio < .95) continue;
    const r = state.textRect;
    let symptom: string | null = null, family = 'text_geometry';
    if (state.visibleRatio < .85 && state.visibleRatio > .1) {
      symptom = state.visibleRatioX < state.visibleRatioY ? 'text_clip_x' : 'text_clip_y';
    } else if (state.contrast !== null && old.contrast !== null && old.contrast >= 4.5 && state.contrast < 2) {
      symptom = 'text_contrast_failure'; family = 'readability';
    } else if (!state.fullyInViewport && r.x < viewport[0] && r.x + r.width > 0 && r.y < viewport[1] && r.y + r.height > 0 &&
        (r.x < 0 || r.x + r.width > viewport[0] || state.viewportBound)) {
      symptom = 'viewport_partial_cutoff'; family = 'layout';
    } else if (state.hitRatio < .8 && state.hitRatio > 0) {
      symptom = 'sibling_occlusion'; family = 'occlusion';
    }
    if (!symptom) continue;
    // Bound the visible symptom; vanished content cannot receive a single-frame box.
    const clipped = symptom === 'text_clip_x' || symptom === 'text_clip_y';
    const shown = clipped ? state.visibleTextRect : r;
    const x1 = Math.max(0, shown.x - 2);
    const y1 = Math.max(0, shown.y - 2);
    const x2 = Math.min(viewport[0], shown.x + shown.width + 2);
    const y2 = Math.min(viewport[1], shown.y + shown.height + 2);
    if (x2 <= x1 || y2 <= y1) continue;
    findings.push({id: state.id, symptom, family, bbox: imageBox({x: x1, y: y1, width: x2 - x1, height: y2 - y1}, dpr),
      evidence: {oracle_id: 'dom-text-v1', baseline_passed: true, after_failed: true,
        visible_before: old.visibleRatio, visible_after: state.visibleRatio,
        hit_before: old.hitRatio, hit_after: state.hitRatio,
        contrast_before: old.contrast, contrast_after: state.contrast}});
  }
  return findings;
}
