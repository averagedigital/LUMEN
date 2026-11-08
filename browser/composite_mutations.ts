import type {Page} from 'playwright';
import {snapshot, type ElementState} from './snapshot.ts';
import {textProblems, validate} from './validate.ts';
import {componentBaselineProblems, componentSnapshot} from './component_defects.ts';
import {TEXT_SYMPTOMS} from './realistic_mutations.ts';

const SCENARIOS = {
  compound_font_letter:['font', 'letter'], compound_font_indent:['font', 'indent'],
  compound_font_offset:['font', 'offset'], compound_font_padding:['font', 'padding'],
  compound_letter_indent:['letter', 'indent'], compound_letter_offset:['letter', 'offset'],
  compound_letter_padding:['letter', 'padding'], compound_indent_offset:['indent', 'offset'],
  compound_indent_padding:['indent', 'padding'], compound_offset_padding:['offset', 'padding'],
} as const;
export const COMPOSITE_OPERATORS = Object.keys(SCENARIOS);
type Declarations = Record<string, string>;

export async function applyComposite(page: Page, operator: string, target: ElementState,
  before: ElementState[], viewport: [number, number], dpr: number, controlComponent?: 0 | 1) {
  const scenario = SCENARIOS[operator as keyof typeof SCENARIOS];
  if (!scenario) throw new Error('unknown composite scenario');
  const initial = await page.evaluate(id => {
    const el = document.querySelector<HTMLElement>(`[data-lumen-id="${CSS.escape(id)}"]`);
    if (!el) throw new Error('composite target missing');
    return {style:el.getAttribute('style'), font:parseFloat(getComputedStyle(el).fontSize), width:el.clientWidth};
  }, target.id);
  const set = async (declarations: Declarations) => page.evaluate(({id, style, declarations}) => {
    const el = document.querySelector<HTMLElement>(`[data-lumen-id="${CSS.escape(id)}"]`)!;
    if (style === null) el.removeAttribute('style'); else el.setAttribute('style', style);
    for (const [key, value] of Object.entries(declarations)) el.style.setProperty(key, value);
  }, {id:target.id, style:initial.style, declarations});
  const changes = (kind: string, value: number): Declarations => {
    if (kind === 'font') return {'font-size':`${initial.font * value}px`};
    if (kind === 'letter') return {'letter-spacing':`${initial.font * (value - 1) * .3}px`};
    const distance = `${initial.width * (value - 1) * .5}px`;
    if (kind === 'indent') return {'text-indent':distance};
    if (kind === 'offset') return {position:'relative', left:distance};
    if (kind === 'padding') return {'padding-left':distance};
    throw new Error('unknown composite component');
  };
  const values = [1.2, 1.4, 1.6, 1.8, 2];
  const controls: {operator:string; value:number; declarations:Declarations; standalone_valid:true}[][] = [];
  let attempts = 0;
  for (const kind of scenario) {
    const valid = [];
    for (const value of values) {
      const declarations = changes(kind, value);
      await set(declarations);
      attempts++;
      const state = await snapshot(page);
      if (!textProblems(state, viewport).length && !validate(before, state, viewport, dpr).length &&
          !componentBaselineProblems(await componentSnapshot(page)).length) {
        valid.push({operator:kind, value, declarations, standalone_valid:true as const});
      }
    }
    controls.push(valid);
  }
  const receipt = {operator, mechanism_id:operator, target:target.id, origin:'synthetic',
    cause_region_css:target.rect, symptoms:[] as string[],
    components:[] as (typeof controls)[number], search:{strategy:'bounded_pair_grid', attempts:0, limit:35},
    joint_verified:false};
  for (const first of controls[0]) for (const second of controls[1]) {
    await set({...first.declarations, ...second.declarations});
    attempts++;
    const findings = validate(before, await snapshot(page), viewport, dpr);
    if (findings.some(f => TEXT_SYMPTOMS.includes(f.symptom))) {
      if (controlComponent !== undefined) await set([first, second][controlComponent].declarations);
      return {...receipt, components:[first, second], joint_verified:true,
        control_for:controlComponent === undefined ? undefined : operator, selected_control:controlComponent,
        search:{...receipt.search, attempts}};
    }
  }
  await set({});
  return {...receipt, search:{...receipt.search, attempts}};
}
