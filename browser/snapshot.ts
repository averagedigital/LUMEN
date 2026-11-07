import type { Page } from 'playwright';
import type { Rect } from './geometry.ts';

export type ElementState = {
  id: string; text: string; rect: Rect; textRect: Rect; visibleRatio: number;
  hitRatio: number; contrast: number | null; background: string | null;
  intentional: boolean; fullyInViewport: boolean; viewportBound: boolean;
};

export async function snapshot(page: Page): Promise<ElementState[]> {
  return page.evaluate(() => {
    const rect = (r: DOMRect): {x: number; y: number; width: number; height: number} =>
      ({x: r.x, y: r.y, width: r.width, height: r.height});
    const canvas = document.createElement('canvas');
    canvas.width = canvas.height = 1;
    const colors = canvas.getContext('2d', {willReadFrequently: true});
    if (!colors) throw new Error('color conversion unavailable');
    const rgb = (s: string) => {
      if (!/^(rgba?\(|color\(srgb )/.test(s)) {
        if (!CSS.supports('color', s)) return [];
        colors.clearRect(0, 0, 1, 1);
        colors.fillStyle = s;
        colors.fillRect(0, 0, 1, 1);
        const [r, g, b, alpha] = colors.getImageData(0, 0, 1, 1).data;
        const sourceAlpha = s.match(/\/\s*([\d.e+-]+)(%)?\s*\)$/i);
        if (s.includes('/') && !sourceAlpha) return [];
        return [r, g, b, sourceAlpha ? Number(sourceAlpha[1]) / (sourceAlpha[2] ? 100 : 1) : alpha / 255];
      }
      const values = s.match(/[-+]?(?:\d*\.)?\d+(?:e[-+]?\d+)?/gi)?.map(Number) ?? [];
      return s.startsWith('color(') ? values.map((v, i) => i < 3 ? Math.max(0, Math.min(1, v)) * 255 : v) : values;
    };
    const luminance = (v: number[]) => v.slice(0, 3).reduce((sum, c, i) => {
      const n = c / 255;
      return sum + (n <= .04045 ? n / 12.92 : ((n + .055) / 1.055) ** 2.4) * [.2126, .7152, .0722][i];
    }, 0);
    const elements = Array.from(document.querySelectorAll<HTMLElement>('body *'))
      .filter(e => !['SCRIPT', 'STYLE', 'NOSCRIPT', 'SVG', 'PATH'].includes(e.tagName) &&
        e.children.length === 0 && (e.textContent?.trim().length ?? 0) >= 3);
    if (elements.length > 500) throw new Error('scene_limit: text elements');
    // Hit-testing must include visible layers even when they ignore pointer input.
    const pointerStyle = document.createElement('style');
    pointerStyle.textContent = '*,*::before,*::after{pointer-events:auto!important}';
    document.head.append(pointerStyle);
    const result = elements.flatMap((el, i) => {
      el.dataset.lumenId ??= el.dataset.elementId ?? String(i);
      if (!el.checkVisibility({contentVisibilityAuto: true})) return [];
      const style = getComputedStyle(el), bounds = el.getBoundingClientRect();
      if (style.display === 'none' || style.visibility !== 'visible' || Number(style.opacity) === 0 ||
          bounds.width <= 0 || bounds.height <= 0) return [];
      const range = document.createRange();
      range.selectNodeContents(el);
      const textBounds = range.getBoundingClientRect();
      if (textBounds.width <= 0 || textBounds.height <= 0) return [];
      let left = -Infinity, top = -Infinity, right = Infinity, bottom = Infinity;
      let intentional = Boolean(el.closest('[data-lumen-intentional], [disabled], [aria-disabled="true"]'));
      let background: string | null = null;
      let backgroundComplex = false;
      let effectiveOpacity = 1;
      let viewportBound = false;
      for (let ancestor: HTMLElement | null = el; ancestor; ancestor = ancestor.parentElement) {
        const css = getComputedStyle(ancestor), r = ancestor.getBoundingClientRect();
        if (['absolute', 'fixed'].includes(css.position) && r.width <= 1 && r.height <= 1 &&
            css.overflowX === 'hidden' && css.overflowY === 'hidden' &&
            (css.clip.replace(/[,\s]/g, '') === 'rect(0px0px0px0px)' || css.clipPath === 'inset(50%)')) return [];
        effectiveOpacity *= Number(css.opacity);
        viewportBound ||= ['fixed', 'sticky'].includes(css.position);
        if (['auto', 'scroll'].includes(css.overflowX) || ['auto', 'scroll'].includes(css.overflowY) ||
            css.textOverflow === 'ellipsis' || (css.webkitLineClamp !== 'none' && css.webkitLineClamp !== '0')) intentional = true;
        if (['hidden', 'clip', 'auto', 'scroll'].includes(css.overflowX)) {
          left = Math.max(left, r.left + ancestor.clientLeft);
          right = Math.min(right, r.left + ancestor.clientLeft + ancestor.clientWidth);
        }
        if (['hidden', 'clip', 'auto', 'scroll'].includes(css.overflowY)) {
          top = Math.max(top, r.top + ancestor.clientTop);
          bottom = Math.min(bottom, r.top + ancestor.clientTop + ancestor.clientHeight);
        }
        if (background === null) {
          if (css.backgroundImage !== 'none' || Number(css.opacity) < 1) backgroundComplex = true;
          const color = rgb(css.backgroundColor);
          if (!color.length) backgroundComplex = true;
          if (color.length === 3 || color[3] === 1) background = css.backgroundColor;
          else if (color[3] > 0) backgroundComplex = true;
        }
      }
      if (effectiveOpacity < .99) return [];
      const fragments = Array.from(range.getClientRects());
      const area = fragments.reduce((sum, r) => sum + r.width * r.height, 0);
      const visible = fragments.reduce((sum, r) => sum + Math.max(0, Math.min(r.right, right) - Math.max(r.left, left)) *
        Math.max(0, Math.min(r.bottom, bottom) - Math.max(r.top, top)), 0);
      let hits = 0, samples = 0;
      const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
      for (let node = walker.nextNode(); node; node = walker.nextNode()) {
        const text = node.textContent ?? '';
        for (let j = 0; j < text.length && samples < 96; j++) {
          if (!text[j].trim()) continue;
          range.setStart(node, j); range.setEnd(node, j + 1);
          const r = range.getBoundingClientRect();
          const x = r.x + r.width / 2, y = r.y + r.height / 2;
          if (x < 0 || y < 0 || x >= innerWidth || y >= innerHeight) continue;
          const hit = document.elementFromPoint(x, y);
          if (hit && (hit === el || el.contains(hit))) hits++;
          samples++;
        }
      }
      const foreground = rgb(style.color), bg = background ? rgb(background) : [];
      const contrast = backgroundComplex || bg.length < 3 || foreground.length < 3 ||
        (foreground.length === 4 && foreground[3] !== 1) ? null :
        (Math.max(luminance(foreground), luminance(bg)) + .05) /
        (Math.min(luminance(foreground), luminance(bg)) + .05);
      return [{id: el.dataset.lumenId, text: el.textContent?.trim() ?? '', rect: rect(bounds),
        textRect: rect(textBounds), visibleRatio: area ? visible / area : 0,
        hitRatio: samples ? hits / samples : 0, contrast, background,
        intentional, viewportBound, fullyInViewport: textBounds.left >= 0 && textBounds.top >= 0 &&
          textBounds.right <= innerWidth && textBounds.bottom <= innerHeight}];
    });
    pointerStyle.remove();
    return result;
  });
}
