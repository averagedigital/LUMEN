import type {Page} from 'playwright';
import {snapshot, type ElementState} from './snapshot.ts';
import {validate} from './validate.ts';
import {componentSnapshot, validateComponents} from './component_defects.ts';

export const RESPONSIVE_OPERATORS = ['breakpoint_shift_regression', 'resize_stale_width_regression'] as const;

export async function breakpointCatalog(page: Page) {
  return page.evaluate(() => {
    const points: {sheet:number;path:number[];query:string;axis:string;value:number;start:number;length:number;prefix:string}[]=[];
    const unsupported: string[]=[];
    let visited=0;
    const walk=(rules:CSSRuleList,sheet:number,path:number[])=>{
      for(let i=0;i<rules.length;i++){
        if(++visited>3000)throw Error('scene_limit: media rules');
        const rule=rules[i],current=[...path,i];
        if(rule instanceof CSSMediaRule){
          const query=rule.media.mediaText;
          const matches=[...query.matchAll(/\(((?:min|max)-(width|height)\s*:\s*)(\d+(?:\.\d+)?)px\)/g)];
          if(!matches.length)unsupported.push(query);
          for(const match of matches){
            if(points.length>=64)throw Error('scene_limit: media breakpoints');
            points.push({sheet,path:current,query,axis:match[2],value:Number(match[3]),
              start:match.index!,length:match[0].length,prefix:match[1]});
          }
        }
        if(rule instanceof CSSGroupingRule){
          if(current.length>8)throw Error('scene_limit: nested media rules');
          walk(rule.cssRules,sheet,current);
        }
      }
    };
    Array.from(document.styleSheets).forEach((sheet,index)=>{
      if(sheet.disabled)return;
      let rules:CSSRuleList;
      try{rules=sheet.cssRules;}catch{unsupported.push('inaccessible_stylesheet');return;}
      walk(rules,index,[]);
    });
    return {points,unsupported};
  });
}

async function mediaText(page: Page, point:{sheet:number;path:number[]}, query:string){
  await page.evaluate(({point,query})=>{
    let rules=document.styleSheets[point.sheet].cssRules;
    for(const index of point.path.slice(0,-1))rules=(rules[index] as CSSGroupingRule).cssRules;
    const rule=rules[point.path.at(-1)!];
    if(!(rule instanceof CSSMediaRule))throw Error('media mutation target missing');
    rule.media.mediaText=query;
  },{point,query});
}

export async function applyResponsive(page:Page, operator:string, before:ElementState[],
  viewport:[number,number],dpr:number,seed:number,resizePath?:number[][]){
  if(operator==='breakpoint_shift_regression'){
    const catalog=await breakpointCatalog(page);
    const candidates=catalog.points.filter(p=>Math.abs(p.value-viewport[p.axis==='width'?0:1])<=64);
    if(!candidates.length)return {receipt:null,restore:async()=>{},diagnostic:catalog};
    const point=candidates[seed%candidates.length];
    const components=await componentSnapshot(page);
    const restore=()=>mediaText(page,point,point.query);
    let attempts=0,broken:number|null=null;
    for(const distance of [1,2,4,8,16,32,64]){
      for(const delta of [-distance,distance]){
        if(point.value+delta<0)continue;
        const query=point.query.slice(0,point.start)+`(${point.prefix}${point.value+delta}px)`+point.query.slice(point.start+point.length);
        await mediaText(page,point,query);attempts++;
        const issues=[...validate(before,await snapshot(page),viewport,dpr),
          ...validateComponents(components,await componentSnapshot(page),viewport,dpr)];
        if(issues.length){broken=delta;break;}
      }
      if(broken!==null)break;
    }
    if(broken===null)await restore();
    return {restore,receipt:{operator,mechanism_id:operator,origin:'synthetic',target:`media:${point.sheet}:${point.path.join('.')}`,
      params:{query:point.query,axis:point.axis,breakpoint:point.value,delta:broken},
      search:{strategy:'bounded_discrete',attempts,limit:14},
      cause_region_css:{x:0,y:0,width:viewport[0],height:viewport[1]}}};
  }
  if(!resizePath)return {receipt:null,restore:async()=>{},diagnostic:{reason:'resize_path_required'}};
  const eligible=before.filter(s=>s.fullyInViewport&&!s.intentional&&s.visibleRatio>.99&&s.hitRatio>.95);
  const targets=await page.evaluate(ids=>ids.filter(id=>{
    const text=document.querySelector<HTMLElement>(`[data-lumen-id="${CSS.escape(id)}"]`);
    const parent=text?.parentElement;
    return parent&&parent!==document.body&&['flex','grid'].includes(getComputedStyle(parent).display);
  }),eligible.map(s=>s.id));
  if(!targets.length)return {receipt:null,restore:async()=>{},diagnostic:{reason:'resize_container_missing'}};
  const id=targets[seed%targets.length];
  const initial=resizePath[0];
  await page.setViewportSize({width:initial[0],height:initial[1]});
  const width=await page.evaluate(id=>{
    const parent=document.querySelector<HTMLElement>(`[data-lumen-id="${CSS.escape(id)}"]`)!.parentElement!;
    const width=getComputedStyle(parent).width;
    parent.style.width=width;
    return width;
  },id);
  for(const size of [...resizePath.slice(1),viewport]){
    await page.setViewportSize({width:size[0],height:size[1]});
    await page.evaluate(async()=>{await document.fonts.ready;});
  }
  const cause=await page.evaluate(id=>{
    const box=document.querySelector<HTMLElement>(`[data-lumen-id="${CSS.escape(id)}"]`)!.parentElement!.getBoundingClientRect();
    return {x:box.x,y:box.y,width:box.width,height:box.height};
  },id);
  return {restore:async()=>{},receipt:{operator,mechanism_id:operator,origin:'synthetic',target:id,
    params:{cached_width:width,resize_path:[...resizePath,viewport]},cause_region_css:cause}};
}
