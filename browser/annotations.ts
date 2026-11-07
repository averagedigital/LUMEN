import type { Page } from 'playwright';

export async function annotations(page: Page) {
  return page.evaluate(() => {
    const nodes = Array.from(document.querySelectorAll<HTMLElement>('body,body *'))
      .filter(el => !['SCRIPT', 'STYLE', 'NOSCRIPT', 'PATH'].includes(el.tagName));
    if (nodes.length > 2000) throw new Error('scene_limit: annotation nodes');
    nodes.forEach((el, i) => { el.dataset.lumenNodeId ??= el.dataset.elementId ?? `node-${i}`; });
    const implicit: Record<string, string> = {BUTTON: 'button', SUMMARY: 'button', INPUT: 'textbox',
      SELECT: 'combobox', TEXTAREA: 'textbox', A: 'link', NAV: 'navigation', DIALOG: 'dialog',
      MAIN: 'main', HEADER: 'banner', IMG: 'img', TABLE: 'table'};
    return nodes.map(el => {
      const box = el.getBoundingClientRect(), style = getComputedStyle(el);
      const input = el instanceof HTMLInputElement ? el : null;
      const sensitive = input?.type === 'password' || input?.type === 'hidden';
      const role = el.getAttribute('role') ?? (input?.type === 'checkbox' ? 'checkbox' :
        input?.type === 'radio' ? 'radio' : implicit[el.tagName] ?? null);
      const labelledBy = (el.getAttribute('aria-labelledby') ?? '').split(/\s+/)
        .map(id => document.getElementById(id)?.textContent ?? '').join(' ').trim();
      const name = sensitive ? null : el.getAttribute('aria-label') || labelledBy ||
        (input?.labels ? Array.from(input.labels).map(l => l.textContent ?? '').join(' ') : '') ||
        (role === 'button' || role === 'link' ? el.textContent?.trim() : null);
      return {element_id: el.dataset.lumenNodeId!, parent_id: el.parentElement?.dataset.lumenNodeId ?? null,
        tag: el.tagName.toLowerCase(), role, accessible_name: name?.slice(0, 256) ?? null,
        text: !sensitive && el.children.length === 0 && !['INPUT','TEXTAREA','OPTION'].includes(el.tagName) ?
          (el.textContent?.trim().slice(0, 256) ?? null) : null,
        state: {bbox_xyxy_css: [box.left, box.top, box.right, box.bottom],
          visible: el.checkVisibility({opacityProperty: true, visibilityProperty: true, contentVisibilityAuto: true}) &&
            box.width > 0 && box.height > 0 && style.visibility === 'visible',
          disabled: el.matches(':disabled,[aria-disabled="true"]'), selected: el.matches('[aria-selected="true"],option:checked'),
          checked: input?.checked ?? null, focused: document.activeElement === el,
          hovered: el.matches(':hover'), scroll: [el.scrollLeft, el.scrollTop]}};
    });
  });
}
