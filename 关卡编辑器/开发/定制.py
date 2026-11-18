"""Apply the standalone workbench adaptations to the tracked Tile WebEditor snapshot."""
from pathlib import Path
import re
import sys
if len(sys.argv) != 2: raise SystemExit('请传入 WebEditor/src 的目录')
SRC = Path(sys.argv[1])
HERE = Path(__file__).resolve().parent
js = (SRC/'level-editor.ts').read_text()
def rep(a,b):
    global js
    assert a in js, a
    js=js.replace(a,b)
def fun(a,b):
    global js
    i=js.index(a); j=js.index('\n}',i)+2
    js=js[:i]+b+js[j:]
rep("import { startUsageTracking, trackUsageAction } from './usage-tracker.ts';\nstartUsageTracking('level');","const trackUsageAction = (_action: string) => {};")
rep("let history", "let history") if "let history" in js else None
rep("const history = new History<Master>(200);", """const history = new History<Master>(200);
let sourceRaw: Uint8Array | null = null;
let sourceKey = '', detachedSource = false, busy = false, savedName = '', savedSignature = '', initialSignature = '';
let initialBoard: Master | null = null;
let readRequest = 0, sourceSerial = 0, pendingUpload = false;
let iapCut = 0, groupRules = false;
let translateX = 0, translateY = 0;
const coverEditor = () => (globalThis as any).CoverEditor;
const localHeaders = { 'X-Level-Editor': 'local' };
const currentSignature = () => JSON.stringify(st.master);
const hasEdits = () => Boolean(initialSignature && currentSignature() !== (savedSignature || initialSignature));
function allowReplace() { return !hasEdits() || confirm('当前编辑尚未另存，确定更换模板？'); }
function invalidateSaved() {
  const button = $('[data-writerepo]') as HTMLButtonElement | null;
  if (button) button.disabled = busy || !savedName || savedSignature !== currentSignature() || ($('[data-tplid]') as HTMLInputElement).value !== savedName;
}
function localStatus(text: string, error = false) {
  const el = $('[data-localstatus]')!; el.textContent = text; el.classList.toggle('is-error',error);
  el.setAttribute('role',error?'alert':'status');
}
function adoptSource(key: string, raw: Uint8Array | null = null) {
  sourceRaw = raw;
  sourceKey = key; detachedSource = false; sourceSerial++; initialBoard = structuredClone(st.master);
  initialSignature = currentSignature(); savedName = savedSignature = '';
  translateX = translateY = 0;
  endFillPreview();
  const id = $('[data-tplid]') as HTMLInputElement;
  id.value = st.master.templateId;
  id.removeAttribute('aria-invalid');
  invalidateSaved(); localStatus(st.master.templateId ? '模板已载入。另存时直接修改模板 ID。' : '已新建空模板。摆牌后填写模板 ID 并另存。');
}
async function readLocal(res: Response) {
  const result = await res.json();
  if (!res.ok) throw new Error(result.error || '操作失败');
  return result;
}
async function writeLocal(path: string, body?: Uint8Array) {
  return readLocal(await fetch(path, {method:'POST', headers:localHeaders, body}));
}
""")
rep("const seedCfg = currentSeedConfig();","const seedCfg = groupRules ? currentSeedConfig() : null;")
rep("const strat = currentStrategyResult(sorted);","const strat = groupRules ? currentStrategyResult(sorted) : null;")
rep("generateFaces(genInput, dupParam, pool).tileIds","generateFaces(genInput, dupParam, pool, iapCut).tileIds")
rep("function drawPlay(): void {","function drawPlay(): void {\n  if (($('.playpanel') as HTMLElement).hidden) return;")
rep("function drawEdit(): void {","function drawEdit(): void {\n  invalidateSaved();\n  if (($('[data-editpanel]') as HTMLElement).hidden) return;\n  const problems = boardProblems();\n  const spot = $('[data-locate-cover]') as HTMLButtonElement; spot.textContent = '定位全遮盖（'+problems.cover.length+'）'; spot.disabled = !problems.cover.length;\n  const float = $('[data-locate-float]') as HTMLButtonElement; float.textContent = '定位悬空（'+problems.floating.length+'）'; float.disabled = !problems.floating.length;")
rep("groupColorOf: st.showGroups ? groupColorOf : undefined, showRange: true, tileBg, debuffIcons,", "showRange: true, tileBg, debuffIcons,")
rep("showTowerStacks: true, badIndices: fullCoverViolationIndices(st.master),","showTowerStacks: true, badIndices: new Set(problems.cover.map((t:any)=>t.index)), unsupportedIndices: new Set(problems.floating.map((t:any)=>t.index)),")
rep("function renderGroupRow(): void {","function renderGroupRow(): void {\n  if (!document.querySelector('[data-grouprow]')) return;")
rep("  if (fullyCoversAdjacent(st.master, a.x, a.y, z)) return false;","  // Overlap across layers is advisory in the workbench.")
rep("if (fullyCoversAdjacent(st.master, a.x, a.y, z)) blocked = true;","if (z < 0) blocked = true;")
rep("const z = nextStackZ(st.master, a.x, a.y, st.currentZ);","const z = st.showMode === 'all' ? nextStackZ(st.master, a.x, a.y, st.currentZ) : st.showZ;")
rep("if (!res.changed) res = removeTopTileAt(st.master, cx, cy);","if (!res.changed) res = st.showMode === 'all' ? removeTopTileAt(st.master, cx, cy) : removeTileAt(st.master, st.showZ, cx, cy);")
# remove the group-only controls and no longer use group ids for placement
rep("st.brush.value, st.activeGroup","st.brush.value")
rep("  $('[data-groupview]')!.onclick = () => { st.showGroups = !st.showGroups; renderGroupRow(); drawEdit(); };\n  $('[data-savegroup]')!.onclick = saveGroup;","")
rep("const generatedDraft = takeGeneratedLevelDraft();","const generatedDraft = null;")
rep("const restoredDraft = generatedDraft ? false : offerLatestDraftRestore();","const restoredDraft = false;")
rep("    adoptMaster(generatedDraft,", "    adoptMaster(generatedDraft as never,")
rep("${generatedDraft.templateId}", "${(generatedDraft as Master).templateId}")
rep("async function loadTemplate(id: string): Promise<void> {\n  if (!id) { toast('先填模板号'); return; }","""async function loadTemplate(id: string): Promise<void> {
  if (!id) { localStatus('请先输入模板 ID。',true); return; }
  if (busy || !allowReplace()) return;
  const request = ++readRequest;
  localStatus('正在读取模板…');""")
rep("    adoptMaster(fromSave(decodeLevelSave(buf)), `${id}_m.bytes`);","""    if (request !== readRequest) return;
    st.dup = curveDefault(); st.strategyGroupId = ''; st.randomSeedGroupId = '';
    adoptMaster(fromSave(decodeLevelSave(buf)), `${id.replace(/_m\\.bytes$/, '')}_m.bytes`);
    adoptSource(res.headers.get('X-Source') || '', buf);
    renderGroupConfig();""")
rep("await loadDupFromConfig(id);","await loadDupFromConfig(st.master.templateId);")
rep("const res = await fetch(TEMPLATE_URL(id));\n    if (!res.ok) throw new Error(`模板 ${id} 不存在`);","const res = await fetch(TEMPLATE_URL(id.replace(/_m\\.bytes$/, '')));\n    if (!res.ok) throw new Error((await res.json()).error || `模板 ${id} 不存在`);")
rep("  if (!row.templateId.trim()) return;","  if (!row.templateId.trim() || busy || !allowReplace()) return;")
rep("    currentLevelId = row.levelId;\n    applyLevelRow(row);","    adoptSource(res.headers.get('X-Source') || '', buf);\n    currentLevelId = row.levelId;\n    applyLevelRow(row);")
rep("toast('载入失败：' + (e as Error).message);","localStatus('载入失败：' + (e as Error).message,true);")
fun('async function openFile',"""async function openFile(file: File): Promise<void> {
  if (busy || pendingUpload || !allowReplace()) return;
  pendingUpload = true;
  const request = ++readRequest;
  try {
    localStatus('正在读取模板文件…');
    const raw = new Uint8Array(await file.arrayBuffer());
    const result = await writeLocal('/api/upload',raw);
    if (request !== readRequest) return;
    st.dup = curveDefault(); st.strategyGroupId = ''; st.randomSeedGroupId = '';
    adoptMaster(fromSave(decodeLevelSave(raw)), file.name); adoptSource(result.key, raw);
    await loadDupFromConfig(st.master.templateId);
  } catch (e) { localStatus('打开失败：' + (e as Error).message,true); }
  finally { pendingUpload = false; }
}""")
fun('function exportBytes',"""async function exportBytes(): Promise<void> {
  const input = $('[data-tplid]') as HTMLInputElement, name = input.value;
  if (busy) return;
  if (!/^[A-Za-z0-9]{1,80}$/.test(name)) { input.setAttribute('aria-invalid','true'); input.focus(); localStatus('另存时，模板 ID 只能使用英文字母和数字，最多 80 个字符。',true); return; }
  const signature = currentSignature(), serial = sourceSerial;
  busy = true; ($('[data-export]') as HTMLButtonElement).disabled = true; invalidateSaved();
  localStatus('正在另存新模板…');
  try {
    const result = await writeLocal('/api/save?'+new URLSearchParams({name,source:sourceKey}),encodeCurrent(st.master.templateId || 'NewTemplate'));
    if (sourceSerial === serial) { savedName = result.name; savedSignature = signature; }
    localStatus('已保存到输出模板：'+result.file+(result.warnings.length?'。有 '+result.warnings.length+' 项摆放提示。':'。'));
  } catch(e) { localStatus((e as Error).message,true); }
  finally { busy = false; ($('[data-export]') as HTMLButtonElement).disabled = false; invalidateSaved(); }
}""")
fun('async function writeRepo',"""async function writeRepo(): Promise<void> {
  if (busy || !savedName || savedSignature !== currentSignature() || ($('[data-tplid]') as HTMLInputElement).value !== savedName) return;
  busy = true; invalidateSaved(); localStatus('正在同步新模板…');
  try {
    const result = await writeLocal('/api/sync?'+new URLSearchParams({name:savedName}));
    savedName=''; localStatus('已同步到模板仓库：'+result.file+'。');
  } catch(e) { localStatus((e as Error).message+' 本地输出仍保留。',true); }
  finally { busy = false; invalidateSaved(); }
}""")
rep("const cv = canvas('edit');\n  cv.style.cursor", "const cv = canvas('edit');\n  cv.style.cursor") if "const cv = canvas('edit');\n  cv.style.cursor" in js else None
rep("  $('[data-undo]')!.onclick = doUndo;","  bindLocal();\n  $('[data-undo]')!.onclick = doUndo;")
rep("    const id = ($('[data-tplid]') as HTMLInputElement).value.trim();\n    currentLevelId = id;","    if (busy || !allowReplace()) return;\n    ++readRequest; const id = '';\n    st.dup = curveDefault(); st.strategyGroupId = ''; st.randomSeedGroupId = '';\n    currentLevelId = id;")
rep("setFn(id ? `${id}_m（新建）` : '未命名母版'); renderAll(); zoomLabel();","setFn('未命名模板'); adoptSource(''); renderAll(); zoomLabel();")
rep("const f = (e.target as HTMLInputElement).files?.[0]; if (f) openFile(f);","const input = e.target as HTMLInputElement; const f = input.files?.[0]; input.value = ''; if (f) openFile(f);")
rep("for (const [k, [dx, dy]] of Object.entries(moves)) $", "for (const [k, [dx, dy]] of Object.entries(moves)) $")
rep("const moves: Record<string, [number, number]> = { l: [-2, 0], r: [2, 0], u: [0, -2], d: [0, 2] };","const moves: Record<string, [number, number]> = { l: [-1, 0], r: [1, 0], u: [0, -1], d: [0, 1] };")
rep("() => edit(m => translateAll(m, dx, dy));","() => moveBoard(dx,dy);")
# Upstream has two mode panes. Only one canvas is visible at a time.
rep("  const playMinBtn = $('[data-playmin]');","  const playMinBtn = null;")
rep("  palette = buildPalette(catalog);","  palette = buildPalette(catalog);")
rep("  renderCats(); renderAll(); zoomLabel();\n  renderDupDrawer();","  renderCats(); renderAll(); zoomLabel();\n  initialBoard = structuredClone(st.master); initialSignature = currentSignature();\n  renderDupDrawer();")
rep("if (st.randomSeedGroupId.trim() !== '') {","if (groupRules && st.randomSeedGroupId.trim() !== '') {")
rep("if (st.strategyGroupId.trim() !== '') {","if (groupRules && st.strategyGroupId.trim() !== '') {")
rep("const slbl = $('[data-showzlabel]'); if (slbl) slbl.textContent = `z ${st.showZ}`;","""const slbl = $('[data-showzlabel]'); if (slbl) slbl.textContent = '第 '+(st.showZ+1)+' 层';
  $('[data-drop-label]')!.textContent = '落牌层：'+(st.showMode==='all'?'自动 / 从第 '+(st.currentZ+1)+' 层起':'第 '+(st.showZ+1)+' 层');""")
rep("  const problems = boardProblems();","""  const problems = boardProblems();
  const counts = [-10,-11].map(id=>st.master.tiles.filter(t=>t.tileId===id).length);
  $('[data-counts]')!.textContent = '共 '+st.master.tiles.length+' 张 · A '+counts[0]+' 张 · B '+counts[1]+' 张';""")
rep("async function boot(): Promise<void> {\n  bind();","""async function boot(): Promise<void> {
  palette = buildPalette([]);
  st.cat = palette[0]!;
  st.brush = st.cat.brushes[0] ?? ERASER;
  bind();
  renderBrushes();
  if (!document.activeElement || document.activeElement === document.body) ($('[data-tplid]') as HTMLInputElement).focus();""")
rep("  initialBoard = structuredClone(st.master); initialSignature = currentSignature();", "  if (!initialSignature) { initialBoard = structuredClone(st.master); initialSignature = currentSignature(); }") if "  initialBoard = structuredClone(st.master); initialSignature = currentSignature();" in js else None
rep("const slbl = $('[data-showzlabel]'); if (slbl) slbl.textContent = '第 '+(st.showZ+1)+' 层';","const slbl = $('[data-showzlabel]') as HTMLInputElement; if (slbl) slbl.value = st.showMode==='all'?'':String(st.showZ+1);")
fun("function drawEdit(): void {", """function drawEdit(): void {
  invalidateSaved();
  if (($('[data-editpanel]') as HTMLElement).hidden) return;
  const problems = boardProblems();
  const spot = $('[data-locate-cover]') as HTMLButtonElement; spot.textContent = '定位全遮盖（'+problems.cover.length+'）'; spot.disabled = !problems.cover.length;
  const float = $('[data-locate-float]') as HTMLButtonElement; float.textContent = '定位悬空（'+problems.floating.length+'）'; float.disabled = !problems.floating.length;
  if (fillPreview && fillPreview.source !== st.master) endFillPreview();
  const counts = [-10,-11].map(id=>st.master.tiles.filter(t=>t.tileId===id).length);
  $('[data-tile-count]')!.textContent = String(st.master.tiles.length);
  $('[data-a-count]')!.textContent = String(counts[0]);
  $('[data-b-count]')!.textContent = String(counts[1]);
  updateUnityPalette();
  paintCoverBoard();
}""")
fun("function renderBrushes(): void {", """function renderBrushes(): void {
  renderUnityPalette();
}""")
fun("function renderCats(): void {", "function renderCats(): void {}")
fun("function renderColorBar(): void {", "function renderColorBar(): void {}")
rep("  $('[data-snap]')!.onclick = () => { st.whole = !st.whole; renderLayer(); };", "")
rep("  const snap = $('[data-snap]'); if (snap) { snap.textContent = st.whole ? '整格吸附' : '半格吸附'; snap.classList.toggle('act', st.whole); }", "")
rep("const chip = $('[data-selmode]'); if (chip) chip.classList.toggle('act', st.selectMode);",
    "const chip = $('[data-selmode]'); if (chip) { chip.classList.toggle('act', st.selectMode); chip.setAttribute('aria-pressed', String(st.selectMode)); }")
rep("  h.textContent = !st.selectMode", "  (h as HTMLElement).hidden = !st.selectMode;\n  h.textContent = !st.selectMode")
fun("function currentId(): string {", "function currentId(): string { return st.master.templateId || 'untitled'; }")
fun("function renderLayer(): void {", """function renderLayer(): void {
  const input = $('[data-showzlabel]') as HTMLInputElement;
  const maxZ = displayedMaxZ();
  input.value = maxZ ? String(maxZ) : '无限制';
  ($('[data-showz="dn"]') as HTMLButtonElement).disabled = maxZ === 0;
  ($('[data-showz="up"]') as HTMLButtonElement).disabled = maxZ >= maxEditableZ();
}""")
start = js.index('  // 显示：全部 / 多层')
end = js.index('  // 批量平移', start)
js = js[:start] + js[end:]
rep("st.master = m; history.clear(); st.currentZ = 0; st.slot = 0; clearSelection();",
    "st.master = m; history.clear(); st.currentZ = 0; st.showMode = 'all'; st.showZ = 0; st.slot = 0; clearSelection();")
rep("  const counts = [-10,-11].map(id=>st.master.tiles.filter(t=>t.tileId===id).length);\n  $('[data-tile-count]')!.textContent = String(st.master.tiles.length);\n  $('[data-a-count]')!.textContent = String(counts[0]);\n  $('[data-b-count]')!.textContent = String(counts[1]);",
    "  renderTemplateStatistics(st.master);\n  renderLayer();")
js = "import { prepareSimulation, simulateClear, strategyLabels, summarizeRuns } from './clear-simulation.js';\n" + js
rep('const history = new History<Master>(200);', 'let history = new History<Master>(200);')
fun("function runSim(): void {", "function runSim(): void { void runClearSimulation(); }")
rep("st.playView = fitView(derivePlayMaster(), pcv.clientWidth, pcv.clientHeight)", "st.playView = fitPlayableBoard(derivePlayMaster(), pcv.clientWidth, pcv.clientHeight)")
rep("Math.min(60, view.s * factor)", "Math.min(160, view.s * factor)")
rep("function restartPlay(): void { playSession = null; drawPlay(); }", "function restartPlay(): void { playSession = null; resetSimulation(); drawPlay(); }")
rep("function setTray(n: number): void {", "function setTray(n: number): void {\n  resetSimulation();")
rep("function saveDupConfig(): Promise<void> {", "function saveDupConfig(): Promise<void> {")
rep("const result = await writeLocal('/api/save?'+new URLSearchParams({name,source:sourceKey}),encodeCurrent(st.master.templateId || 'NewTemplate'));", """const raw = encodeCurrent(st.master.templateId || 'NewTemplate');
    const source = sourceRaw ? (await writeLocal('/api/upload', sourceRaw)).key : sourceKey;
    const result = await writeLocal('/api/save?'+new URLSearchParams({name,source}),raw);""")
rep("  renderTemplateMetadata();\n  renderColorBar();", "  renderTemplateMetadata();\n  renderColorBar();")
rep("async function loadDupFromConfig(id: string): Promise<void> {\n  try {\n    await ensureLevelDataLoaded();", """async function loadDupFromConfig(id: string): Promise<void> {
  const generation = sourceSerial;
  try {
    await ensureLevelDataLoaded();
    if (generation !== sourceSerial || st.master.templateId !== id || batchPhase !== 'single') return;""")
rep("st.slot--; renderDupRow();", "st.slot--; resetSimulation(); renderDupRow();")
rep("st.slot++; renderDupRow();", "st.slot++; resetSimulation(); renderDupRow();")
rep("window.addEventListener('resize', () => { renderDrawerLayout(); draw(); });",
    "window.addEventListener('resize', () => { renderDrawerLayout(); if (!($('.playpanel') as HTMLElement).hidden) $('[data-pfit]')!.dispatchEvent(new Event('click')); else drawEdit(); });")
rep("function derivePlayMaster(): Master {", """function derivePlayMaster(seedOverride?: number): Master {
  const dup = { ...curDup(), seed: seedOverride ?? curDup().seed };""")
rep("return deriveMaster(st.master, curDup(), st.slot);", "return deriveMaster(st.master, dup, st.slot);")
rep("  const dup = curDup();\n  const dupParam", "  const dupParam")
rep("currentStrategyResult(sorted) : null", "currentStrategyResult(sorted, dup.seed) : null")
rep("function currentStrategyResult(sorted: Master['tiles']): StrategyResult | null {",
    "function currentStrategyResult(sorted: Master['tiles'], seedOverride?: number): StrategyResult | null {")
rep("strategySeed(st.levelNum, st.slot, dup.seed, groupId)", "strategySeed(st.levelNum, st.slot, seedOverride ?? dup.seed, groupId)")
rep("if (e.target instanceof HTMLInputElement) return;", "if (fillPreview || e.target instanceof HTMLInputElement) return;")
rep("function doUndo(): void {", "function doUndo(): void { if (fillPreview) return;")
rep("function doRedo(): void {", "function doRedo(): void { if (fillPreview) return;")
rep("function edit(fn: (m: Master) => Master | { master: Master }): void {",
    "function edit(fn: (m: Master) => Master | { master: Master }): void {\n  if (fillPreview) return;")
addon=(HERE/'工作台扩展.txt').read_text()
js = "import { bindBatchTools } from '../assets/batch-tools.js';\n" + js
rep("function derivePlayMaster(seedOverride?: number): Master {\n  const dup = { ...curDup(), seed: seedOverride ?? curDup().seed };",
    """function derivePlayMaster(seedOverride?: number, job?: CaptureJob): Master {
  const master = job?.master ?? st.master;
  const dup = job?.dup ?? { ...curDup(), seed: seedOverride ?? curDup().seed };
  const level = job?.level ?? st.levelNum;
  const slot = job?.slot ?? st.slot;
  const useGroups = job ? Boolean(job.seedGroup || job.strategyGroup) : groupRules;""")
start = js.index('function derivePlayMaster(')
end = js.index('/** 当前档位', start)
derive = js[start:end].replace('pickPoolRow(levelPools, st.levelNum)', 'pickPoolRow(levelPools, level)')
derive = derive.replace('deriveMaster(st.master, dup, st.slot)', 'deriveMaster(master, dup, slot)')
derive = derive.replace('sortTilesForSave(st.master.tiles)', 'sortTilesForSave(master.tiles)')
derive = derive.replace('groupRules ? currentStrategyResult(sorted, dup.seed)', 'useGroups ? currentStrategyResult(sorted, dup.seed, job)')
derive = derive.replace('groupRules ? currentSeedConfig()', 'useGroups ? currentSeedConfig(job)')
derive = derive.replace('st.master.towers.map', 'master.towers.map').replace('dupParam, pool, iapCut', 'dupParam, pool, job ? 0 : iapCut')
js = js[:start] + derive.replace('...st.master, tiles', '...master, tiles') + js[end:]
rep('function currentSeedConfig():', 'function currentSeedConfig(job?: CaptureJob):')
rep('const g = st.randomSeedGroupId.trim();', "const g = (job ? job.seedGroup : st.randomSeedGroupId).trim();")
rep('tryLoadSeedConfig(seedGroupTable, seedTable, groupId, st.slot)', 'tryLoadSeedConfig(seedGroupTable, seedTable, groupId, job?.slot ?? st.slot)')
rep("function currentStrategyResult(sorted: Master['tiles'], seedOverride?: number):", "function currentStrategyResult(sorted: Master['tiles'], seedOverride?: number, job?: CaptureJob):")
rep('const g = st.strategyGroupId.trim();', 'const g = (job ? job.strategyGroup : st.strategyGroupId).trim();')
rep('!isNewTemplate(st.master.templateId)) return null;', '!isNewTemplate((job?.master ?? st.master).templateId)) return null;')
rep('strategySeed(st.levelNum, st.slot, seedOverride ?? dup.seed, groupId)', 'strategySeed(job?.level ?? st.levelNum, job?.slot ?? st.slot, seedOverride ?? dup.seed, groupId)')
rep('applyStrategyGroup(sorted, st.slot, groupId, strategyGroupTable, seed)', 'applyStrategyGroup(sorted, job?.slot ?? st.slot, groupId, strategyGroupTable, seed)')
rep("import { render, fitView,", "import { render, tileDisplayLabel, fitView,")
rep("function iconOf(tileId: number): string | undefined { return iconMap.get(tileId); }",
    """let availableTileIcons: Set<string> | null = null;
function iconOf(tileId: number): string | undefined {
  const name = iconMap.get(tileId);
  return name && (!availableTileIcons || availableTileIcons.has(name)) ? name : undefined;
}""")
rep("    const res = await fetch(CATALOG_URL);\n    if (res.ok) catalog = parseTileCatalog(await res.text());",
    """    const [catalogResponse, iconResponse] = await Promise.allSettled([
      fetch(CATALOG_URL), fetch('/api/tile-icons'),
    ]);
    if (catalogResponse.status === 'fulfilled' && catalogResponse.value.ok) {
      catalog = parseTileCatalog(await catalogResponse.value.text());
    }
    if (iconResponse.status === 'fulfilled' && iconResponse.value.ok) {
      const { icons } = await iconResponse.value.json();
      if (Array.isArray(icons)) availableTileIcons = new Set(icons);
    }""")
rep("showMaxZ: null, cover: coverFlags(board), colorOf, icons, iconOf, hover: null",
    "showMaxZ: null, cover: coverFlags(board), colorOf, icons, iconOf, labelOf: editorTileLabel, hover: null")
rep("icons.get(icon)?.complete) { const img = document.createElement('img'); img.src = ICON_BASE",
    "icons.get(icon)?.naturalWidth) { const img = document.createElement('img'); img.alt = editorTileLabel(type); img.src = ICON_BASE")
rep("isVariantType(type) ? '随' : String(type)", "editorTileLabel(type)")
rep("个 tile 换成 ${tileId}", "张牌换成「${editorTileLabel(tileId)}」")
rep("isVariantType(u.type) ? `随机类型 ${variantKey(u.type)}` : `TileId ${u.type}`", "editorTileLabel(u.type)")
rep("palette = buildPalette(catalog);", """palette = buildPalette(catalog);
  for (const category of palette) {
    for (const brush of category.brushes) {
      if (brush.kind !== 'tile') continue;
      const name = brush.des?.trim() || brush.label;
      if (name && !/^-?\\d+$/.test(name)) tileNames.set(brush.value, name);
    }
  }
  for (const entry of catalog) {
    const theme = /^zm(\\d{3})$/.exec(entry.icon);
    if (theme && entry.des.trim().toLowerCase() === 'zm') {
      tileNames.set(entry.tileId, '主题牌 ' + theme[1]);
    } else if (entry.des.trim()) {
      tileNames.set(entry.tileId, entry.des.trim());
    }
  }
  tileNames.set(COIN_BLOCKER.value, COIN_BLOCKER.label);""")
rep("  for (const e of catalog) if (e.icon) iconMap.set(e.tileId, e.icon);",
    """  for (const e of catalog) if (e.icon) iconMap.set(e.tileId, e.icon);
  if (!iconMap.has(COIN_BLOCKER.value)) iconMap.set(COIN_BLOCKER.value, '10000_coin');""")
js=js.replace('// ---------- 启动 ----------',addon+'\n// ---------- 启动 ----------')
(HERE/'level-editor.ts').write_text(js)
ren=(SRC/'level-render.ts').read_text()
ren=ren.replace('export interface RenderState {','export interface RenderState {\n  labelOf: (tileId: number) => string;')
ren=ren.replace('const label = tileDisplayLabel(t.tileId);','const label = st.labelOf(t.tileId);')
ren=ren.replace('g.strokeText(label, r.x + r.w / 2, r.y + r.h / 2);',
                'g.strokeText(label, r.x + r.w / 2, r.y + r.h / 2, r.w * 0.85);')
ren=ren.replace("g.fillStyle = randomMarkerColor(t.tileId) ?? '#fff'; g.fillText(label, r.x + r.w / 2, r.y + r.h / 2);",
                "g.fillStyle = randomMarkerColor(t.tileId) ?? '#fff'; g.fillText(label, r.x + r.w / 2, r.y + r.h / 2, r.w * 0.85);")
ren=ren.replace('export interface RenderState {','export interface RenderState {\n  unsupportedIndices?: ReadonlySet<number>;')
ren=ren.replace("g.fillStyle = '#16181d';","g.fillStyle = '#f5f9fd';")
ren=ren.replace("rgba(255,255,255,0.1)","rgba(73,122,171,0.12)").replace("rgba(255,255,255,0.04)","rgba(73,122,171,0.05)").replace('rgba(90,200,160,0.55)','rgba(78,133,183,0.66)')
ren=ren.replace("    if (st.badIndices?.has(i)) {","    if (st.unsupportedIndices?.has(i)) { g.strokeStyle = '#2469a5'; g.lineWidth = 2.5; g.strokeRect(r.x + 2, r.y + 2, r.w - 4, r.h - 4); }\n    if (st.badIndices?.has(i)) {")
ren=ren.replace("g.strokeStyle = '#fff'; g.lineWidth = 1.5","g.strokeStyle = '#8bb4dc'; g.lineWidth = 1.5")
ren=ren.replace("st.cover[i]) { g.fillStyle = 'rgba(0,0,0,0.5)'","st.cover[i]) { g.fillStyle = 'rgba(39,67,98,0.27)'")
ren=ren.replace('export interface RenderState {','export interface RenderState {\n  templateScreenshot?: boolean;')
ren=ren.replace("  g.fillStyle = '#f5f9fd';\n  g.fillRect(0, 0, w, h);\n  drawGrid(g, w, h, st);",
                """  g.fillStyle = st.templateScreenshot ? '#e0f8e2' : '#f5f9fd';
  g.fillRect(0, 0, w, h);
  if (st.templateScreenshot) drawTemplateScreenshotGrid(g, w, h, st.view);
  else drawGrid(g, w, h, st);""")
ren=ren.replace("g.fillStyle = 'rgba(39,67,98,0.27)'; roundRect(g, r, r.w * 0.14);",
                "g.fillStyle = st.templateScreenshot ? 'rgba(0,0,0,0.48)' : 'rgba(39,67,98,0.27)'; roundRect(g, r, r.w * 0.14);")
ren=ren.replace('  if (img && img.complete && img.naturalWidth > 0) {', '''  if (st.templateScreenshot) {
    const face = st.labelOf(t.tileId);
    g.font = `bold ${r.w * 0.53}px system-ui, sans-serif`;
    g.textAlign = 'center'; g.textBaseline = 'middle';
    g.fillStyle = t.tileId === RANDOM_ID_A ? '#926707'
      : t.tileId === RANDOM_ID_A - 1 ? '#35418e' : '#101417';
    g.fillText(face, r.x + r.w / 2, r.y + r.h * 0.43, r.w * 0.87);
    g.font = `bold ${r.w * 0.2}px system-ui, sans-serif`;
    g.textAlign = 'right'; g.textBaseline = 'top'; g.fillStyle = '#151515';
    g.fillText(String(t.z + 1), r.x + r.w * 0.95, r.y + r.w * 0.05);
    g.textAlign = 'left';
    g.fillText(String(Math.max(0, t.groupId)), r.x + r.w * 0.05, r.y + r.w * 0.05);
    return;
  }
  if (img && img.complete && img.naturalWidth > 0) {''', 1)
ren += '''
function drawTemplateScreenshotGrid(g: CanvasRenderingContext2D, w: number, h: number, view: ViewXf): void {
  for (let x = 0; x < RANGE_MAX_X; x++) {
    for (let y = 0; y < RANGE_MAX_Y; y++) {
      const point = ptScreen(view, x, y);
      g.fillStyle = x % 2 === y % 2 ? '#e02d28' : '#2748e3';
      g.beginPath();
      g.arc(point.x, point.y, Math.max(1.5, view.s * 0.045), 0, Math.PI * 2);
      g.fill();
    }
  }
  const center = ptScreen(view, 8, 9);
  const left = center.x - RANGE_SQUARE_W * view.s / 2;
  const top = center.y - RANGE_SQUARE_H * view.s / 2;
  g.strokeStyle = '#81988a';
  g.lineWidth = 1.5;
  g.setLineDash([4, 5]);
  g.strokeRect(left, top, RANGE_SQUARE_W * view.s, RANGE_SQUARE_H * view.s);
  g.beginPath();
  g.moveTo(center.x, 0); g.lineTo(center.x, h);
  g.moveTo(0, center.y); g.lineTo(w, center.y);
  g.stroke();
  g.setLineDash([]);
}
'''
(HERE/'level-render.ts').write_text(ren)
