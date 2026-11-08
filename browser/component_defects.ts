import type { Page } from 'playwright';
import type { Rect } from './geometry.ts';
import { imageBox } from './geometry.ts';
import type { Finding } from './validate.ts';
const SYMPTOMS = {
    flex: 'flex_overflow', grid: 'grid_track_collapse', panel: 'container_height_collapse',
    header: 'header_collision', table: 'table_cell_overlap', dropdown: 'dropdown_clipping',
    modal: 'modal_content_overflow', adornment: 'input_adornment_collision',
    image: 'image_aspect_distortion', svg: 'svg_clipping',
} as const;
export const COMPONENT_OPERATORS = Object.values(SYMPTOMS);
export type ComponentKind = keyof typeof SYMPTOMS;
export type ComponentState = {
    id: string;
    kind: ComponentKind;
    rect: Rect;
    visible: Rect;
    issueRect: Rect;
    intentional: boolean;
    metrics: {
        overflowX: number;
        overflowY: number;
        overlap: number;
        clipped: number;
        gridOverflow: number;
        distortion: number;
        children: number;
        viewBox: boolean;
    };
};
export type ComponentReceipt = {
    operator: string;
    target: string;
    params: Record<string, unknown>;
    cause_region_css: Rect;
};
export async function componentSnapshot(page: Page): Promise<ComponentState[]> {
    return page.evaluate(() => {
        const all = Array.from(document.querySelectorAll<HTMLElement>('body *'));
        if (all.length > 5000)
            throw new Error('scene_limit: component elements');
        type R = {
            x: number;
            y: number;
            width: number;
            height: number;
        };
        const rect = (r: DOMRect): R => ({ x: r.x, y: r.y, width: r.width, height: r.height });
        const area = (r: R) => Math.max(0, r.width) * Math.max(0, r.height);
        const intersect = (a: R, b: R): R => { const x = Math.max(a.x, b.x), y = Math.max(a.y, b.y); return { x, y, width: Math.max(0, Math.min(a.x + a.width, b.x + b.width) - x), height: Math.max(0, Math.min(a.y + a.height, b.y + b.height) - y) }; };
        const union = (rs: R[]): R => { if (!rs.length)
            return { x: 0, y: 0, width: 0, height: 0 }; const x = Math.min(...rs.map(r => r.x)), y = Math.min(...rs.map(r => r.y)); return { x, y, width: Math.max(...rs.map(r => r.x + r.width)) - x, height: Math.max(...rs.map(r => r.y + r.height)) - y }; };
        let textNodes = 0;
        let textFragments = 0;
        const modals = Array.from(document.querySelectorAll<HTMLDialogElement>('dialog:modal'));
        const modal = modals.find(dialog => {
            const box = dialog.getBoundingClientRect();
            const hit = document.elementFromPoint(Math.max(0, Math.min(innerWidth - 1, box.x + box.width / 2)), Math.max(0, Math.min(innerHeight - 1, box.y + box.height / 2)));
            return hit?.closest('dialog:modal') === dialog;
        });
        const outOfFlow = (el: Element) => ['absolute', 'fixed'].includes(getComputedStyle(el).position);
        const inkFragments = (el: Element, flowOnly = false, collisionOnly = false): R[] => {
            const paint = (element: Element, box: R) => collisionOnly ? collisionVisible(element, box) : box;
            if (el instanceof SVGGraphicsElement) {
                const b = el.getBBox(), m = el.getScreenCTM();
                if (m) {
                    const p = [new DOMPoint(b.x, b.y), new DOMPoint(b.x + b.width, b.y), new DOMPoint(b.x, b.y + b.height), new DOMPoint(b.x + b.width, b.y + b.height)].map(p => p.matrixTransform(m));
                    return [paint(el, union(p.map(p => ({ x: p.x, y: p.y, width: .001, height: .001 }))))];
                }
            }
            if (el instanceof HTMLInputElement) {
                const c = getComputedStyle(el), r = el.getBoundingClientRect(), canvas = document.createElement('canvas'), ctx = canvas.getContext('2d');
                if (ctx) {
                    ctx.font = c.font;
                    const left = r.x + parseFloat(c.borderLeftWidth) + parseFloat(c.paddingLeft);
                    const right = r.right - parseFloat(c.borderRightWidth) - parseFloat(c.paddingRight);
                    const width = ctx.measureText(el.value).width;
                    const alignedRight = c.textAlign === 'right' || (c.textAlign === 'start' && c.direction === 'rtl') || (c.textAlign === 'end' && c.direction === 'ltr');
                    const x = c.textAlign === 'center' ? (left + right - width) / 2 : alignedRight ? right - width : left;
                    const y = r.y + parseFloat(c.borderTopWidth) + parseFloat(c.paddingTop);
                    const height = parseFloat(c.lineHeight) || parseFloat(c.fontSize);
                    return [paint(el, intersect({ x, y, width, height }, { x: left, y, width: Math.max(0, right - left), height }))];
                }
            }
            if (el.textContent?.trim()) {
                const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
                const fragments: R[] = [];
                let node: Node | null;
                while ((node = walker.nextNode())) {
                    if (++textNodes > 50000)
                        throw new Error('scene_limit: component text nodes');
                    if (!node.textContent?.trim() || !node.parentElement?.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true }))
                        continue;
                    if (flowOnly) {
                        let excluded = false;
                        for (let ancestor: Element | null = node.parentElement; ancestor && ancestor !== el.parentElement; ancestor = ancestor.parentElement) {
                            if (outOfFlow(ancestor)) { excluded = true; break; }
                        }
                        if (excluded) continue;
                    }
                    const range = document.createRange();
                    range.selectNodeContents(node);
                    for (const clientRect of range.getClientRects()) {
                        if (++textFragments > 50000)
                            throw new Error('scene_limit: component text fragments');
                        const box = paint(node.parentElement!, rect(clientRect));
                        if (area(box)) fragments.push(box);
                    }
                }
                return fragments;
            }
            return [paint(el, rect(el.getBoundingClientRect()))];
        };
        const visible = (el: Element, r: R, viewport = true): R => {
            let result = viewport ? intersect(r, { x: 0, y: 0, width: innerWidth, height: innerHeight }) : r;
            for (let a: Element | null = el; a; a = a.parentElement) {
                const c = getComputedStyle(a), b = rect(a.getBoundingClientRect());
                if (['hidden', 'clip', 'auto', 'scroll'].includes(c.overflowX))
                    result = intersect(result, { x: b.x, y: result.y, width: b.width, height: result.height });
                if (['hidden', 'clip', 'auto', 'scroll'].includes(c.overflowY))
                    result = intersect(result, { x: result.x, y: b.y, width: result.width, height: b.height });
                if (a.matches('dialog:modal')) break;
            }
            return result;
        };
        const collisionVisible = (el: Element, box: R): R => {
            let result = intersect(box, {x: 0, y: 0, width: innerWidth, height: innerHeight});
            let skipUntil: Element | null | undefined;
            for (let a: Element | null = el; a; a = a.parentElement) {
                if (a === skipUntil) skipUntil = undefined;
                const css = getComputedStyle(a);
                if (skipUntil === undefined) {
                    const bounds = rect(a.getBoundingClientRect());
                    if (['hidden', 'clip', 'auto', 'scroll'].includes(css.overflowX))
                        result = intersect(result, {x: bounds.x, y: result.y, width: bounds.width, height: result.height});
                    if (['hidden', 'clip', 'auto', 'scroll'].includes(css.overflowY))
                        result = intersect(result, {x: result.x, y: bounds.y, width: result.width, height: bounds.height});
                    if (['fixed', 'absolute'].includes(css.position)) {
                        skipUntil = null;
                        for (let parent = a.parentElement; parent; parent = parent.parentElement) {
                            const style = getComputedStyle(parent);
                            if ((css.position === 'absolute' && style.position !== 'static') || style.transform !== 'none' || style.scale !== 'none' || style.rotate !== 'none' || style.translate !== 'none' || style.perspective !== 'none' || style.filter !== 'none' || /layout|paint|strict|content/.test(style.contain) || /transform|scale|rotate|translate|perspective|filter/.test(style.willChange)) {
                                skipUntil = parent;
                                break;
                            }
                        }
                    }
                }
                if (a.matches('dialog:modal')) break;
            }
            return result;
        };
        const result: ComponentState[] = [];
        for (const [index, el] of all.entries()) {
            if (modal && !modal.contains(el))
                continue;
            const explicit = el.dataset.lumenComponent;
            const kind = explicit ?? (el.tagName === 'HEADER' ? 'header' : el.tagName === 'TABLE' ? 'table' : el.tagName === 'IMG' ? 'image' : el.tagName.toLowerCase() === 'svg' ? 'svg' : el.getAttribute('role') === 'menu' ? 'dropdown' : (el.tagName === 'DIALOG' || el.getAttribute('role') === 'dialog') ? 'modal' : null);
            if (!kind || !['flex', 'grid', 'panel', 'header', 'table', 'dropdown', 'modal', 'adornment', 'image', 'svg'].includes(kind))
                continue;
            const css = getComputedStyle(el), r = rect(el.getBoundingClientRect());
            if (!el.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true }) || area(r) < 16)
                continue;
            if (kind === 'image') {
                let transformed = false;
                for (let a: Element | null = el; a; a = a.parentElement) {
                    const style = getComputedStyle(a);
                    transformed ||= style.transform !== 'none' || style.rotate !== 'none' || style.scale !== 'none' || style.perspective !== 'none';
                }
                if (transformed) continue;
            }
            let intentional = Boolean(el.closest('[data-lumen-intentional]'));
            for (let a: Element | null = el; a; a = a.parentElement) {
                const c = getComputedStyle(a);
                intentional ||= ['auto', 'scroll'].includes(c.overflowX) || ['auto', 'scroll'].includes(c.overflowY) || Number(c.opacity) < .99;
                if (a.matches('dialog:modal')) break;
            }
            el.dataset.lumenComponentId ??= el.dataset.elementId ?? `comp-${index}`;
            const flowOnly = ['grid', 'flex', 'panel'].includes(kind);
            const children = Array.from(kind === 'table' ? el.querySelectorAll('td,th') : el.children).filter(e => (!flowOnly || !outOfFlow(e)) && e.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true }) && area(rect(e.getBoundingClientRect())) > 0);
            const fragments = children.map(child => inkFragments(child, flowOnly, ['header', 'table', 'adornment'].includes(kind)));
            const boxes = fragments.map(parts => union(parts)), content = union(boxes.filter(box => area(box) > 0));
            let overlap = 0;
            let overlapBox: R | null = null;
            if (['header', 'table', 'adornment'].includes(kind)) {
                const ordered = fragments.flatMap((parts, owner) => parts.map(box => ({...box, owner}))).sort((a, b) => a.y - b.y);
                let active: typeof ordered = [];
                let comparisons = 0;
                for (const box of ordered) {
                    active = active.filter(previous => previous.y + previous.height > box.y);
                    for (const previous of active) {
                        if (++comparisons > 100000)
                            throw new Error('scene_limit: component overlap pairs');
                        if (previous.owner === box.owner) continue;
                        const intersection = intersect(previous, box);
                        overlap = Math.max(overlap, area(intersection));
                        if (area(intersection) > 8)
                            overlapBox = overlapBox ? union([overlapBox, intersection]) : intersection;
                    }
                    active.push(box);
                }
            }
            let distortion = 1;
            if (el instanceof HTMLImageElement && el.naturalWidth && el.naturalHeight && css.objectFit === 'fill') {
                const width = r.width - parseFloat(css.paddingLeft) - parseFloat(css.paddingRight) - parseFloat(css.borderLeftWidth) - parseFloat(css.borderRightWidth);
                const height = r.height - parseFloat(css.paddingTop) - parseFloat(css.paddingBottom) - parseFloat(css.borderTopWidth) - parseFloat(css.borderBottomWidth);
                const actual = width / height, expected = el.naturalWidth / el.naturalHeight;
                distortion = Math.max(actual / expected, expected / actual);
            }
            const ownInk = kind === 'svg' ? union(inkFragments(el)) : content;
            const paint = kind === 'svg' ? [ownInk] : boxes;
            const total = paint.reduce((n, b) => n + area(b), 0), shown = paint.reduce((n, b) => n + area(visible(el, b, false)), 0);
            const gridOverflow = children.reduce((n, e, i) => Math.max(n, boxes[i].width - rect(e.getBoundingClientRect()).width), 0);
            result.push({ id: el.dataset.lumenComponentId, kind: kind as ComponentKind, rect: r, visible: visible(el, r), issueRect: overlapBox ? overlapBox : ['flex', 'panel'].includes(kind) && area(content) > 0 ? union([r, content]) : visible(el, r), intentional,
                metrics: { overflowX: area(content) > 0 ? Math.max(0, content.x + content.width - r.x - r.width, r.x - content.x) : 0,
                    overflowY: area(content) > 0 ? Math.max(0, content.y + content.height - r.y - r.height, r.y - content.y) : 0,
                    overlap, clipped: total ? 1 - shown / total : 0, gridOverflow, distortion, children: children.length, viewBox: el.hasAttribute('viewBox') } });
        }
        return result;
    });
}
function broken(state: ComponentState): boolean {
    if (state.intentional)
        return false;
    const m = state.metrics;
    switch (state.kind) {
        case 'flex': return m.overflowX > 4;
        case 'grid': return m.gridOverflow > 4;
        case 'panel': return m.overflowY > 4;
        case 'header':
        case 'table':
        case 'adornment': return m.overlap > 8;
        case 'dropdown': return m.clipped > .15 && m.clipped < .95;
        case 'modal': return m.overflowY > 4 && m.clipped > .15 && m.clipped < .95;
        case 'image': return m.distortion > 1.25;
        case 'svg': return !m.viewBox && m.clipped > .15 && m.clipped < .95;
    }
}
export function componentBaselineProblems(states: ComponentState[]): ComponentState[] { return states.filter(broken); }
export function discoverComponent(states: ComponentState[], operator: string): ComponentState[] {
    return states.filter(s => SYMPTOMS[s.kind] === operator && !s.intentional && !broken(s) && s.visible.width >= s.rect.width - .5 && s.visible.height >= s.rect.height - .5 && s.rect.width > 50 && s.rect.height > 20 &&
        (['image', 'svg'].includes(s.kind) || s.metrics.children >= (['flex', 'grid', 'header', 'table', 'adornment'].includes(s.kind) ? 2 : 1)) && !(s.kind === 'svg' && s.metrics.viewBox));
}
export async function applyComponent(page: Page, operator: string, target: ComponentState, _states: ComponentState[], seed: number): Promise<ComponentReceipt> {
    if (SYMPTOMS[target.kind] !== operator)
        throw new Error('component operator/target mismatch');
    const ratio = .4 + (seed % 3) * .05;
    await page.evaluate(({ id, operator, ratio }) => {
        const el = document.querySelector<HTMLElement>(`[data-lumen-component-id="${CSS.escape(id)}"]`);
        if (!el)
            throw new Error('component target missing');
        const r = el.getBoundingClientRect(), children = Array.from(el.children) as HTMLElement[];
        switch (operator) {
            case 'flex_overflow':
                el.style.flexWrap = 'nowrap';
                for (const child of children) {
                    child.style.minWidth = `${r.width * .75}px`;
                    child.style.flexShrink = '0';
                }
                break;
            case 'grid_track_collapse':
                el.style.gridTemplateColumns = `12px 1fr`;
                break;
            case 'container_height_collapse':
                el.style.height = `${Math.max(20, r.height * ratio)}px`;
                el.style.overflow = 'visible';
                break;
            case 'header_collision': {
                const a = children[0].getBoundingClientRect(), b = children[1].getBoundingClientRect();
                children[1].style.transform = `translateX(${a.x - b.x + 10}px)`;
                break;
            }
            case 'table_cell_overlap': {
                const cells = Array.from(el.querySelectorAll<HTMLElement>('tr:first-child>td,tr:first-child>th'));
                if (cells.length < 2)
                    throw new Error('inapplicable: table cells');
                const a = cells[0].getBoundingClientRect(), b = cells[1].getBoundingClientRect();
                cells[1].style.transform = `translateX(${a.x - b.x + 10}px)`;
                break;
            }
            case 'dropdown_clipping':
                el.style.height = `${r.height * .55}px`;
                el.style.overflow = 'hidden';
                break;
            case 'modal_content_overflow':
                el.style.height = `${r.height}px`;
                el.style.overflow = 'hidden';
                children[0].style.fontSize = `${parseFloat(getComputedStyle(children[0]).fontSize) * 4}px`;
                children[0].style.lineHeight = '1.5';
                break;
            case 'input_adornment_collision': {
                const input = el.querySelector('input');
                if (!input)
                    throw new Error('inapplicable: input');
                input.style.paddingLeft = '8px';
                break;
            }
            case 'image_aspect_distortion':
                el.style.width = `${r.width * 1.7}px`;
                el.style.height = `${r.height}px`;
                el.style.objectFit = 'fill';
                break;
            case 'svg_clipping':
                el.style.width = `${r.width * ratio}px`;
                el.style.overflow = 'hidden';
                break;
            default: throw new Error('unknown component operator');
        }
    }, { id: target.id, operator, ratio });
    return { operator, target: target.id, params: { ratio }, cause_region_css: target.rect };
}
export function validateComponents(before: ComponentState[], after: ComponentState[], viewport: [
    number,
    number
], dpr: number): Finding[] {
    const baseline = new Map(before.map(s => [s.id, s]));
    return after.flatMap(state => {
        const old = baseline.get(state.id);
        if (!old || old.kind !== state.kind || old.intentional || broken(old) || !broken(state))
            return [];
        const r = state.issueRect, x = Math.max(0, r.x), y = Math.max(0, r.y), right = Math.min(viewport[0], r.x + r.width), bottom = Math.min(viewport[1], r.y + r.height);
        if (right - x < 2 || bottom - y < 2)
            return [];
        return [{ id: state.id, symptom: SYMPTOMS[state.kind], family: ['header', 'table', 'adornment'].includes(state.kind) ? 'occlusion' : 'layout',
                bbox: imageBox({ x, y, width: right - x, height: bottom - y }, dpr), evidence: { oracle_id: `component-${state.kind}-v1`, baseline_passed: true, after_failed: true, before: old.metrics, after: state.metrics } }];
    });
}
