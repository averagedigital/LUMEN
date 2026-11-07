export const ARCHETYPES = ['dashboard', 'crm', 'admin', 'shop', 'product', 'checkout',
  'settings', 'chat', 'inbox', 'calendar', 'kanban', 'docs', 'article', 'pricing',
  'files', 'editor', 'spreadsheet', 'wizard', 'search', 'media'] as const;
type Archetype = typeof ARCHETYPES[number];
type Spec = { seed: number; archetype: Archetype; locale: string; width: number };

const WORDS: Record<string, string[]> = {
  en: ['Workspace', 'Overview', 'Settings', 'Export report', 'Search records', 'Save changes', 'Project', 'Help', 'Recent activity', 'Ready for review', 'Your changes are saved locally.'],
  ru: ['Рабочее пространство', 'Обзор', 'Настройки', 'Экспорт отчёта', 'Поиск записей', 'Сохранить изменения', 'Проект', 'Помощь', 'Последние события', 'Готово к проверке', 'Изменения сохранены локально.'],
  de: ['Arbeitsbereich', 'Übersicht', 'Einstellungen', 'Bericht exportieren', 'Datensätze suchen', 'Änderungen speichern', 'Projekt', 'Hilfe', 'Letzte Aktivitäten', 'Bereit zur Prüfung', 'Die Änderungen wurden lokal gespeichert.'],
  es: ['Espacio de trabajo', 'Resumen', 'Ajustes', 'Exportar informe', 'Buscar registros', 'Guardar cambios', 'Proyecto', 'Ayuda', 'Actividad reciente', 'Listo para revisión', 'Los cambios se guardaron localmente.'],
  zh: ['工作空间', '概览', '设置', '导出报告', '搜索记录', '保存更改', '项目', '帮助', '最近活动', '准备审核', '更改已保存在本地。'],
  ar: ['مساحة العمل', 'نظرة عامة', 'الإعدادات', 'تصدير التقرير', 'البحث في السجلات', 'حفظ التغييرات', 'مشروع', 'مساعدة', 'النشاط الأخير', 'جاهز للمراجعة', 'تم حفظ التغييرات محليًا.'],
};

export function buildScene(spec: Spec) {
  if (!ARCHETYPES.includes(spec.archetype) || !Number.isSafeInteger(spec.seed) || spec.seed < 0 || !WORDS[spec.locale]) throw new Error('invalid scene spec');
  const w = WORDS[spec.locale], n = spec.seed, count = 3 + n % 5, dark = n % 3 === 0;
  let serial = 0;
  const id = () => `data-element-id="e${serial++}"`;
  const text = (s: string) => `<span ${id()}>${s}</span>`;
  const button = (label: string) => `<button ${id()} type="button">${text(label)}</button>`;
  const rows = Array.from({length: count}, (_, i) => `${w[6]} ${n * 7 + i + 1}`);
  const cards = rows.map((name, i) => `<article class="panel"><h3>${text(name)}</h3><p>${text(w[9])}</p><strong>${text(String(120 + (n * 13 + i * 83) % 9000))}</strong>${button(w[1])}</article>`).join('');
  const table = `<div class="table-scroll" data-lumen-intentional><table><thead><tr>${[w[6], w[9], '2026', w[2]].map(s => `<th>${text(s)}</th>`).join('')}</tr></thead><tbody>${rows.map((s, i) => `<tr><td>${text(s)}</td><td>${text(w[9])}</td><td>${text(String(100 + i * 31 + n))}</td><td>${button(w[1])}</td></tr>`).join('')}</tbody></table></div>`;
  const form = `<form><label>${text(w[6])}<input ${id()} name="project" value="Project ${n}" autocomplete="off"></label><label>${text(w[2])}<select ${id()}><option>${w[1]}</option><option>${w[9]}</option></select></label><label class="check"><input ${id()} type="checkbox">${text(w[8])}</label>${button(w[5])}</form>`;
  const chart = `<svg viewBox="0 0 500 140" role="img" aria-label="Synthetic project activity"><path d="M0 120 L50 ${40 + n % 70} L100 80 L150 30 L200 65 L250 20 L300 40 L350 10 L400 40 L500 5" fill="none" stroke="${dark ? '#7dd3fc' : '#0369a1'}" stroke-width="3"/></svg>`;
  const art = `<svg viewBox="0 0 320 220" role="img" aria-label="Synthetic desk lamp"><rect width="320" height="220" fill="#e2e8f0"/><ellipse cx="160" cy="190" rx="80" ry="15" fill="#94a3b8"/><path d="M160 185V80L225 40" fill="none" stroke="#334155" stroke-width="12"/><path d="M190 30L255 80L275 30Z" fill="#f59e0b"/></svg>`;
  const list = `<ul>${rows.map(s => `<li>${text(s)} ${text(w[9])}</li>`).join('')}</ul>`;
  const layouts: Record<Archetype, string> = {
    dashboard: `<section class="grid">${cards}</section><section class="panel">${chart}${table}</section>`,
    crm: `<div class="toolbar">${button(w[3])}<input aria-label="${w[4]}" placeholder="${w[4]}"></div>${table}<aside class="panel">${text(w[8])}${list}</aside>`,
    admin: `<div class="split"><aside>${list}</aside><section><h2>${text(w[2])}</h2>${form}${table}</section></div>`,
    shop: `<div class="toolbar">${button(w[4])}<select aria-label="Sort"><option>${w[8]}</option></select></div><div class="grid">${rows.map((s, i) => `<article class="panel">${art}<h3>${text(s)}</h3><p>${text('$' + (39 + i * 10 + n % 40))}</p>${button(w[1])}</article>`).join('')}</div>`,
    product: `<div class="split"><figure>${art}<figcaption>${text(rows[0])}</figcaption><section><h2>${text(rows[0])}</h2><p>${text(w[10])}</p>${list}${button(w[5])}</section></div>`,
    checkout: `<ol class="steps"><li>${text(w[6])}</li><li>${text(w[2])}</li><li>${text(w[5])}</li></ol><div class="split"><section>${form}</section><aside class="panel"><h2>${text(w[1])}</h2>${list}<hr><strong>${text('$' + (79 + n % 500))}</strong></aside></div>`,
    settings: `<nav class="tabs">${button(w[1])}${button(w[2])}${button(w[8])}</nav><section class="panel">${form}<fieldset><legend>${text(w[8])}</legend>${list}</fieldset></section>`,
    chat: `<div class="chat"><aside>${list}</aside><section>${rows.map((s, i) => `<p class="bubble ${i % 2 ? 'reply' : ''}">${text(s)}<br>${text(w[10])}</p>`).join('')}<form class="toolbar"><input aria-label="Message" placeholder="${w[4]}">${button(w[5])}</form></section></div>`,
    inbox: `<div class="mail"><aside>${button(w[3])}${list}</aside><section>${rows.map(s => `<article><input type="checkbox" aria-label="Select"><strong>${text(s)}</strong><p>${text(w[10])}</p></article>`).join('')}</section><aside class="panel">${text(w[9])}</aside></div>`,
    calendar: `<div class="toolbar"><h2>${text('November 2026')}</h2>${button(w[2])}</div><div class="calendar">${Array.from({length: 28}, (_, i) => `<section><time>${i + 1}</time>${i % 3 === n % 3 ? `<p>${text(rows[i % count])}</p>` : ''}</section>`).join('')}</div>`,
    kanban: `<div class="board">${[w[6], w[9], w[5]].map(s => `<section><h2>${text(s)}</h2>${cards}</section>`).join('')}</div>`,
    docs: `<div class="documentation"><nav>${list}</nav><article><h2>${text(w[7])}</h2><p>${text(w[10])}</p><pre><code>export const project = ${n};\nawait workspace.save(project);</code></pre><h3>${text(w[2])}</h3>${list}</article></div>`,
    article: `<article class="reading"><p>${text('November ' + (n % 28 + 1) + ', 2026')}</p><h2>${text(rows[0])}</h2>${art}<blockquote>${text(w[10])}</blockquote>${rows.map(s => `<h3>${text(s)}</h3><p>${text(w[10])} ${text(w[9])}</p>`).join('')}</article>`,
    pricing: `<div class="pricing">${rows.slice(0, 3).map((s, i) => `<section class="panel"><h2>${text(s)}</h2><p class="price">${text('$' + (9 + i * 20))}</p>${list}${button(w[5])}</section>`).join('')}</div><p>${text(w[10])}</p>`,
    files: `<nav class="breadcrumbs">${text(w[0])} / ${text(w[6])}</nav><div class="files">${rows.map((s, i) => `<button class="file" type="button"><span aria-hidden="true">▤</span>${text(s + (i % 2 ? '.csv' : '.txt'))}</button>`).join('')}</div><footer>${text(count + ' files')}</footer>`,
    editor: `<div class="editor"><aside>${list}</aside><section><div class="tabs">${text('project.ts')}</div><pre><code>${rows.map((s, i) => `${i + 1}  const project${i} = "${s}";`).join('\n')}\n\n  export default workspace;</code></pre><footer>${text('TypeScript • UTF-8')}</footer></section></div>`,
    spreadsheet: `<div class="toolbar">${button(w[3])}<label>${text('fx')}<input value="=SUM(B2:B8)" aria-label="Formula"></label></div><div class="sheet" data-lumen-intentional>${table}</div><nav>${button(w[6])}${button(w[1])}</nav>`,
    wizard: `<div class="wizard"><aside><ol>${[w[6], w[2], w[5]].map(s => `<li>${text(s)}</li>`).join('')}</ol></aside><section><progress value="${n % 3 + 1}" max="3"></progress><h2>${text(w[2])}</h2>${form}</section></div>`,
    search: `<form class="searchbar"><input aria-label="${w[4]}" value="Project ${n}">${button(w[4])}</form><div class="results"><aside>${form}</aside><section>${rows.map(s => `<article><h3><a href="#result">${text(s)}</a></h3><p>${text(w[10])}</p></article>`).join('')}</section></div>`,
    media: `<div class="player"><figure>${art}<figcaption>${text(rows[0])}</figcaption></figure><progress value="${20 + n % 70}" max="100"></progress><div class="toolbar">${button('Previous')}${button('Play')}${button('Next')}<input type="range" aria-label="Volume" value="60"></div></div><section class="playlist">${list}</section>`,
  };
  const html = `<!doctype html><html lang="${spec.locale}" dir="${spec.locale === 'ar' ? 'rtl' : 'ltr'}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>
*{box-sizing:border-box}body{margin:0;background:${dark ? '#111827' : '#f1f5f9'};color:${dark ? '#f8fafc' : '#17212b'};font:${14 + n % 3}px/1.5 'Noto Sans',Arial,sans-serif}header{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:16px 24px;background:${dark ? '#1e293b' : '#fff'};border-bottom:1px solid #94a3b8;flex-wrap:wrap}header nav,.toolbar,.tabs,.steps{display:flex;gap:12px;align-items:center;flex-wrap:wrap}main{max-width:${1080 + n % 5 * 80}px;margin:auto;padding:20px}h1{font-size:28px}h2{font-size:22px}h3{font-size:18px}h1,h2,h3,p{overflow-wrap:anywhere}button,input,select{font:inherit;color:inherit;background:${dark ? '#334155' : '#fff'};border:1px solid #64748b;border-radius:${n % 3 * 4}px;padding:9px 12px;max-width:100%}button{cursor:pointer;white-space:normal;text-align:start}button span{display:inline-block}input{min-width:0}label{display:grid;gap:6px;margin:12px 0}label.check{display:flex;align-items:center}a{color:${dark ? '#7dd3fc' : '#075985'}}.panel{background:${dark ? '#1e293b' : '#fff'};border:1px solid #94a3b8;padding:16px;border-radius:${n % 4 * 4}px}.grid,.pricing,.files{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,220px),1fr));gap:16px}.grid .panel strong{display:block;margin:10px 0}.split,.admin,.wizard,.results{display:grid;grid-template-columns:1fr 1fr;gap:24px}.table-scroll{overflow:auto}table{border-collapse:collapse;width:100%;min-width:500px}th,td{text-align:start;padding:12px;border-bottom:1px solid #94a3b8}svg{display:block;width:100%;max-height:260px}figure{margin:0}ul{padding-inline-start:24px}li{padding:7px 0}.chat,.documentation,.editor{display:grid;grid-template-columns:220px 1fr;gap:20px}.mail{display:grid;grid-template-columns:180px 1fr 200px;gap:16px}.mail article,.results article{padding:12px;border-bottom:1px solid #94a3b8}.bubble{max-width:80%;padding:12px;background:${dark ? '#334155' : '#dbeafe'};border-radius:12px}.reply{margin-inline-start:auto}.calendar{display:grid;grid-template-columns:repeat(7,1fr)}.calendar section{min-height:90px;border:1px solid #94a3b8;padding:8px;min-width:0}.calendar p{font-size:12px}.board{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px}.board .panel{margin:12px 0}.reading{max-width:720px;margin:auto}.price{font-size:36px}.file{display:grid;gap:12px;padding:24px}.file>span:first-child{font-size:40px}pre{overflow:auto;padding:16px;background:${dark ? '#020617' : '#e2e8f0'}}progress{width:100%;height:12px}.player{max-width:700px;margin:auto}.playlist{margin-top:20px}.searchbar{display:flex;gap:12px;margin-bottom:20px}.searchbar input{flex:1}details{margin:16px 0}summary{cursor:pointer}footer{margin-top:16px}button:focus-visible,input:focus-visible,summary:focus-visible{outline:3px solid #0ea5e9;outline-offset:2px}
.steps{list-style:none;padding:0}
@media(max-width:700px){main{padding:12px}header{padding:12px}.split,.chat,.documentation,.editor,.wizard,.results,.mail{grid-template-columns:1fr}.board{grid-template-columns:1fr}.calendar section{padding:3px;min-height:70px}.calendar p{font-size:10px}h1{font-size:24px}.chat>aside,.editor>aside{display:none}}
</style></head><body><header><strong>${text(w[0])}</strong><nav aria-label="Primary">${button(w[1])}${button(w[2])}</nav></header><main><h1>${text(w[1] + ' · ' + rows[0])}</h1>${layouts[spec.archetype]}<details><summary data-element-id="help-toggle">${w[7]}</summary><p>${text(w[10])}</p></details></main></body></html>`;
  let elementIndex = 0;
  const identifiedHtml = html.replace(/data-element-id="e\d+"/g, () => `data-element-id="e${elementIndex++}"`);
  return {html: identifiedHtml, page: {archetype: spec.archetype, structural_family: spec.archetype},
    design: {theme: dark ? 'dark' : 'light', font: 'Noto Sans', density: count},
    content: {locale: spec.locale, seed: n}, state: {details_open: false},
    task: {origin: 'synthetic', goal: w[7], target_element_id: 'help-toggle',
      allowed_actions: ['click'], sequence: [{action: 'click', element_id: 'help-toggle'}]}};
}
