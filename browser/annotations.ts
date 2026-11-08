import type { Page } from 'playwright';

export async function visibleScenarios(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const modal = document.elementFromPoint(0, 0)?.closest('dialog:modal');
    const visible = (el: Element): boolean => {
      if (modal && !modal.contains(el)) return false;
      if (!el.checkVisibility({opacityProperty: true, visibilityProperty: true, contentVisibilityAuto: true})) return false;
      const r = el.getBoundingClientRect();
      const left = Math.max(0, r.left), right = Math.min(innerWidth, r.right);
      const top = Math.max(0, r.top), bottom = Math.min(innerHeight, r.bottom);
      if (right <= left || bottom <= top) return false;
      for (const x of [.1, .5, .9]) for (const y of [.1, .5, .9]) {
        if (el.contains(document.elementFromPoint(left + (right - left) * x, top + (bottom - top) * y))) return true;
      }
      return false;
    };
    const some = (selector: string, predicate: (el: HTMLElement) => boolean = () => true) =>
      Array.from(document.querySelectorAll<HTMLElement>(selector)).some(el => visible(el) && predicate(el));
    const overlap = (a: Element, b: Element) => {
      const x = a.getBoundingClientRect(), y = b.getBoundingClientRect();
      return Math.min(x.right, y.right) - Math.max(x.left, y.left) > 1 &&
        Math.min(x.bottom, y.bottom) - Math.max(x.top, y.top) > 1;
    };
    const tags: string[] = [];
    if (some('.ellipsis', el => getComputedStyle(el).textOverflow === 'ellipsis' &&
        ['hidden', 'clip'].includes(getComputedStyle(el).overflowX) && el.scrollWidth > el.clientWidth)) tags.push('ellipsis');
    if (some('table', el => {
      for (let parent = el.parentElement; parent; parent = parent.parentElement) {
        if (visible(parent) && ['auto', 'scroll'].includes(getComputedStyle(parent).overflowX) &&
            parent.scrollWidth > parent.clientWidth) return true;
        if (parent.matches('dialog:modal')) break;
      }
      return false;
    })) tags.push('scrollable_table');
    if (some('.avatar-stack', el => {
      const avatars = Array.from(el.querySelectorAll('.avatar')).filter(visible);
      return avatars.some((a, i) => avatars.slice(i + 1).some(b => overlap(a, b)));
    })) tags.push('overlapping_avatars');
    if (some('.badge[data-lumen-intentional]', el => Array.from(el.parentElement?.children ?? [])
      .some(other => other !== el && visible(other) && overlap(el, other)))) tags.push('badge');
    if (some('dialog:modal')) tags.push('modal');
    if (some('dialog[open]:not(:modal),[role="dialog"]:not(dialog)')) tags.push('dialog');
    if (some('details.dropdown[open]', el => Array.from(el.querySelectorAll('nav,[role="menu"]')).some(visible))) tags.push('dropdown');
    if (some('.skeleton[role="status"]', el => Array.from(el.children).some(visible))) tags.push('skeleton');
    if (some('button:disabled,input:disabled,select:disabled,textarea:disabled,[aria-disabled="true"]')) tags.push('disabled_control');
    if (some('.image-placeholder[role="img"]')) tags.push('image_placeholder');
    if (some('header', el => getComputedStyle(el).position === 'sticky')) tags.push('sticky_header');
    return tags;
  });
}

export async function annotations(page: Page) {
  return page.evaluate(() => {
    const nodes = Array.from(document.querySelectorAll<HTMLElement>('body,body *'))
      .filter(el => !['SCRIPT', 'STYLE', 'NOSCRIPT', 'PATH'].includes(el.tagName));
    if (nodes.length > 2000) throw new Error('scene_limit: annotation nodes');
    nodes.forEach((el, i) => { el.dataset.lumenNodeId ??= el.dataset.elementId ?? `node-${i}`; });
    const implicit: Record<string, string> = {BUTTON: 'button', SUMMARY: 'button', INPUT: 'textbox',
      SELECT: 'combobox', TEXTAREA: 'textbox', A: 'link', NAV: 'navigation', DIALOG: 'dialog',
      MAIN: 'main', HEADER: 'banner', IMG: 'img', TABLE: 'table'};
    const modal = document.elementFromPoint(0, 0)?.closest('dialog:modal');
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
          modal: el.matches('dialog:modal'), interaction_blocked: Boolean(modal && !modal.contains(el)) || Boolean(el.closest('[inert]')),
          visible: el.checkVisibility({opacityProperty: true, visibilityProperty: true, contentVisibilityAuto: true}) &&
            box.width > 0 && box.height > 0 && style.visibility === 'visible',
          disabled: el.matches(':disabled,[aria-disabled="true"]'), selected: el.matches('[aria-selected="true"],option:checked'),
          checked: input?.checked ?? null, focused: document.activeElement === el,
          hovered: el.matches(':hover'), scroll: [el.scrollLeft, el.scrollTop]}};
    });
  });
}
