import { bindBatchTools } from '../assets/batch-tools.js';
import { prepareSimulation, simulateClear, strategyLabels, summarizeRuns } from './clear-simulation.js';
/**
 * 关卡编辑器（U1 + D3）主控。左半摆关（tile/塔/debuff/层/批量平移/撤销），右半套 DupPara 即时试玩。
 * 摆放手感与通关推演仿 feat-playbook-tilematch；数据/导出走 D3（_m.bytes + config_level.csv）。
 */
import {History} from './history.js';
import {newMaster, placeTile, removeTileAt, removeTopTileAt, nextStackZ, placeTower, removeTowerAt, addDebuffAt, removeDebuffAt, raiseAll, lowerAll, translateAll, coverFlags, toSave, fromSave, masterStats, snapAnchor, layerParity, subMasterOfGroup, NO_GROUP, fullyCoversAdjacent, fullCoverViolationIndices, topTileIndexAt, replaceTiles, tileKey} from './level-model.js';
import {computeTemplateStats, sha256Bytes} from './level-template-stats.js';
import {encodeLevelSave, decodeLevelSave, sortTilesForSave} from './level-bytes.js';
import {parseTileCatalog, buildPalette, entryColor, hashColor, normalColorTags, COLOR_HEX, ERASER, DEBUFFS} from './level-palette.js';
import {TILE_BG_DATA_URI} from './tile-bg.js';
import {render, tileDisplayLabel, fitView, screenToGrid, rectOf, playRectOf, towerOffset, RANGE_MAX_X, RANGE_MAX_Y} from './level-render.js';
import {deriveMaster, isVariantType, variantKey, VARIANT_BASE} from './level-derive.js';
import {generateFaces, parseLevelTilePools, pickPoolRow} from './level-gen.js';
const trackUsageAction = (_action        ) => {};
import {generateFacesStructured, tryLoadSeedConfig} from './level-gen-seed.js';
import {applyStrategyGroup, isNewTemplate, strategySeed, UseStrategyType} from './level-gen-strategy.js';
import {effectiveDup, DEFAULT_DUP, DUP_SLOTS, findRow, newLevelRow, levelListRows, filterLevelListRows} from './level-csv.js';
import {initialDrawerState, reduceDrawer} from './level-drawer.js';
import {filterLevelIndexRows} from './level-index-drawer.js';
import {copyDupParasText, drawerDockHeight, drawerHeights, initialDupDrawerState, reduceDupDrawer, savableLevelId, updateDupSlot} from './dup-para-drawer.js';
import {REQUIRED_LEVEL_CSV_NAMES, validateLevelCsvNames} from './level-csv-import.js';
import {adaptLevelData, importLevelCsvBundle, levelRecordToRow, levelRowOperations, LevelDataConflictError, loadLevelData, patchLevelData, persistTemplateStats} from './level-data-client.js';
import {readLevelEditorDraft, removeLevelEditorDraft, resolveDraftStorage, writeLevelEditorDraft} from './level-editor-draft.js';
import {computeCover, findUnclearableTypes, simulate, makeRng, isRandomMarker, propPush, propResolve, propUndo, propMagneto, propCrane, propShuffle, propStageToBag} from './logic.js';

const CATALOG_URL = '../csv/config_client_tile.csv';
const CONFIG_LEVEL_TILE_URL = '../csv/config_client_level_tile.csv';
const TEMPLATE_URL = (id        ) => `/templates/${encodeURIComponent(id)}_m.bytes`;
const ICON_BASE = '../icon/';
const VARIANT_PALETTE = ['#f5b544', '#58b6f0', '#5fcf8e', '#f5748b', '#a98bff', '#f0a048', '#7f9cf5', '#e05f9c', '#33c3c9', '#b07a4f', '#f1c531', '#8d6ed6', '#46c76b'];

// ---------- 状态 ----------



                                                              
                                          
                                    






                                        

                                                  
                                                                      
                                                                               
                                                                                  

                                                             

                                                                     
                                                              
                                                     


const GROUP_COLORS = ['#f5b544', '#58b6f0', '#5fcf8e', '#f5748b', '#a98bff', '#f0a048', '#33c3c9', '#ec5fa8'];
const GROUP_COUNT = GROUP_COLORS.length;
function groupColorOf(groupId        )                {
  return groupId === NO_GROUP ? null : GROUP_COLORS[groupId % GROUP_COUNT] ;
}

let history = new History        (200);
let sourceRaw                    = null;
let sourceKey = '', detachedSource = false, busy = false, savedName = '', savedSignature = '', initialSignature = '';
let initialBoard                = null;
let readRequest = 0, sourceSerial = 0, pendingUpload = false;
let iapCut = 0, groupRules = false;
let translateX = 0, translateY = 0;
const coverEditor = () => (globalThis       ).CoverEditor;
const localHeaders = { 'X-Level-Editor': 'local' };
const currentSignature = () => JSON.stringify(st.master);
const hasEdits = () => Boolean(initialSignature && currentSignature() !== (savedSignature || initialSignature));
function allowReplace() { return !hasEdits() || confirm('当前编辑尚未另存，确定更换模板？'); }
function invalidateSaved() {
  const button = $('[data-writerepo]')                            ;
  if (button) button.disabled = busy || !savedName || savedSignature !== currentSignature() || ($('[data-tplid]')                    ).value !== savedName;
}
function localStatus(text        , error = false) {
  const el = $('[data-localstatus]') ; el.textContent = text; el.classList.toggle('is-error',error);
  el.setAttribute('role',error?'alert':'status');
}
function adoptSource(key        , raw                    = null) {
  sourceRaw = raw;
  sourceKey = key; detachedSource = false; sourceSerial++; initialBoard = structuredClone(st.master);
  initialSignature = currentSignature(); savedName = savedSignature = '';
  translateX = translateY = 0;
  endFillPreview();
  const id = $('[data-tplid]')                    ;
  id.value = st.master.templateId;
  id.removeAttribute('aria-invalid');
  invalidateSaved(); localStatus(st.master.templateId ? '模板已载入。另存时直接修改模板 ID。' : '已新建空模板。摆牌后填写模板 ID 并另存。');
}
async function readLocal(res          ) {
  const result = await res.json();
  if (!res.ok) throw new Error(result.error || '操作失败');
  return result;
}
async function writeLocal(path        , body             ) {
  return readLocal(await fetch(path, {method:'POST', headers:localHeaders, body}));
}

let levelPools                     = []; // config_client_level_tile 类型池分段（启动时载入）
let seedGroupTable                 = new Map(); // config_level_randomSeed_group（种子组→档位 seedID）
let seedTable            = new Map();           // config_level_randomSeed（seedID→结构化参数）
let strategyGroupTable                     = new Map(); // config_level_strategy_group（策略组→档位策略串）
let catalog              = [];
let palette             = [];
const colorMap = new Map                ();
const iconMap = new Map                ();   // tileId → icon 名
const icons = new Map                          ();
const debuffIcons = new Map                          ();
let drawer              = initialDrawerState;
let dupDrawer                 = initialDupDrawerState;
let indexDrawer                                                                                                                                   = {
  open: false, status: 'idle', rows: [], selectedLevelId: '', error: '',
};
let drawerConfig                     = null;
let levelData                        = null;
let levelDataLoading                                 = null;

function applyLevelData(store                )       {
  const adapted = adaptLevelData(store);
  levelData = store;
  drawerConfig = adapted.config;
  drawer = reduceDrawer(drawer, { type: 'loaded', rows: levelListRows(drawerConfig.rows) });
  indexDrawer = { ...indexDrawer, status: 'ready', rows: adapted.indexRows, error: '' };
  seedGroupTable = adapted.seedGroupTable;
  seedTable = adapted.seedTable;
  strategyGroupTable = adapted.strategyGroupTable;
  playSession = null;
  renderTemplateMetadata();
}

async function ensureLevelDataLoaded()                          {
  if (levelData) return levelData;
  if (!levelDataLoading) levelDataLoading = loadLevelData().then(store => { applyLevelData(store); return store; })
    .finally(() => { levelDataLoading = null; });
  return levelDataLoading;
}

async function refreshLevelData()                {
  applyLevelData(await loadLevelData());
  renderIndexDrawer(); renderLevelDrawer(); drawPlay(); renderRightPanels();
}

function reportLevelDataError(prefix        , error         )       {
  const message = prefix + (error         ).message;
  const status = $('[data-leveldatastatus]');
  if (status) { status.textContent = message; status.hidden = false; }
  if (error instanceof LevelDataConflictError) alert(message);
  else toast(message);
}
let drawerQuery = '';
let indexDrawerQuery = '';
let currentLevelId = '';
const LEVEL_EDITOR_DRAFT_KEY = 'tile-level-editor.latest-draft.v1';
const DRAFT_SAVE_DELAY_MS = 350;
let draftSaveTimer                                       = null;

const st        = {
  master: newMaster(''),
  currentZ: 0, showMode: 'all', showZ: 0, whole: false, upper: 'fade',
  cat: { key: 'normal', name: '普通', brushes: [] },
  brush: ERASER,
  editView: { s: 24, ox: 0, oy: 0 }, playView: { s: 24, ox: 0, oy: 0 },
  hover: null,
  dup: curveDefault(), slot: 0, levelNum: 1, strategyGroupId: '', randomSeedGroupId: '', tray: 7,
  activeGroup: NO_GROUP, showGroups: false, normalColor: null,
  selectMode: false, selection: new Set(),
};

function curveDefault()                     {
  const curve = [12, 12, 11, 11, 10, 10, 9, 9, 8, 8];
  return curve.map(a => ({ a, seed: 0, lucky: 100, b: 4 }));
}

// ---------- DOM 助手 ----------
const $ =                                  (sel        ) => document.querySelector(sel)            ;
const $$ = (sel        ) => Array.from(document.querySelectorAll(sel));
function toast(msg        )       {
  const t = $('[data-toast]'); if (!t) return;
  t.textContent = msg; t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 1600);
}

function captureDraft()                       {
  return {
    master: st.master,
    currentLevelId,
    dup: st.dup,
    strategyGroupId: st.strategyGroupId,
    randomSeedGroupId: st.randomSeedGroupId,
    levelNum: st.levelNum,
    tray: st.tray,
  };
}

function cancelDraftSave()       {
  if (draftSaveTimer !== null) window.clearTimeout(draftSaveTimer);
  draftSaveTimer = null;
}

function browserDraftStorage()                      {
  return resolveDraftStorage(() => window.localStorage);
}

function takeGeneratedLevelDraft()                {
  if (new URLSearchParams(location.search).get('generatedDraft') !== '1') return null;
  const raw = sessionStorage.getItem('generated-level-draft');
  if (!raw) return null;
  try {
    const value = JSON.parse(raw)                   ;
    if (typeof value.templateId !== 'string' || !Array.isArray(value.tiles) || !Array.isArray(value.towers) || !Array.isArray(value.debuffs)) return null;
    sessionStorage.removeItem('generated-level-draft');
    return { ...newMaster(value.templateId), ...value }          ;
  } catch {
    return null;
  }
}

function scheduleDraftSave()       {
  cancelDraftSave();
  draftSaveTimer = window.setTimeout(() => {
    draftSaveTimer = null;
    const storage = browserDraftStorage();
    if (storage) writeLevelEditorDraft(storage, LEVEL_EDITOR_DRAFT_KEY, captureDraft());
  }, DRAFT_SAVE_DELAY_MS);
}

function flushPendingDraftSave()       {
  if (draftSaveTimer === null) return;
  cancelDraftSave();
  const storage = browserDraftStorage();
  if (storage) writeLevelEditorDraft(storage, LEVEL_EDITOR_DRAFT_KEY, captureDraft());
}

function applyDraft(draft                        )       {
  cancelDraftSave();
  const data = draft.data;
  st.master = data.master;
  currentLevelId = data.currentLevelId;
  st.dup = data.dup.map(value => value ? { ...value } : null);
  st.strategyGroupId = data.strategyGroupId;
  st.randomSeedGroupId = data.randomSeedGroupId;
  st.levelNum = data.levelNum;
  st.tray = data.tray;
  st.currentZ = 0;
  st.slot = 0;
  history.clear();
  clearSelection();
  playSession = null;
  const id = st.master.templateId || currentLevelId;
  const idInput = $('[data-tplid]')                           ;
  if (idInput) idInput.value = id;
  const levelInput = $('[data-levelnum]')                           ;
  if (levelInput) levelInput.value = String(st.levelNum);
  const trayInput = $('[data-tray]')                           ;
  if (trayInput) trayInput.value = String(st.tray);
  st.editView = fitView(st.master, canvas('edit').clientWidth, canvas('edit').clientHeight);
  st.playView = { ...st.editView };
  setFn(`${id || '未命名母版'}（已恢复草稿）`);
}

function offerLatestDraftRestore()          {
  const storage = browserDraftStorage();
  if (!storage) return false;
  const draft = readLevelEditorDraft(storage, LEVEL_EDITOR_DRAFT_KEY);
  if (!draft) {
    removeLevelEditorDraft(storage, LEVEL_EDITOR_DRAFT_KEY);
    return false;
  }
  const id = draft.data.master.templateId || draft.data.currentLevelId || '未命名关卡';
  const savedTime = new Date(draft.savedAt).toLocaleString('zh-CN', { hour12: false });
  if (!confirm(`发现上次编辑的草稿：${id}\n保存时间：${savedTime}\n\n是否恢复？`)) {
    removeLevelEditorDraft(storage, LEVEL_EDITOR_DRAFT_KEY);
    return false;
  }
  applyDraft(draft);
  return true;
}

// ---------- 颜色 / 图标 ----------
function colorOf(tileId        )         {
  if (isVariantType(tileId)) return VARIANT_PALETTE[variantKey(tileId) % VARIANT_PALETTE.length] ;
  return colorMap.get(tileId) ?? hashColor(tileId);
}
let availableTileIcons                     = null;
function iconOf(tileId        )                     {
  const name = iconMap.get(tileId);
  return name && (!availableTileIcons || availableTileIcons.has(name)) ? name : undefined;
}

// tile 槽位背景图（tile_bg.png，内联）——画布 tile 和笔刷共用
const tileBg = new Image();
tileBg.onload = () => draw();
tileBg.src = TILE_BG_DATA_URI;
function ensureIcon(name                    )       {
  if (!name || icons.has(name)) return;
  const img = new Image();
  img.onload = () => draw();
  img.onerror = () => { /* 缺图就用色块，不报错 */ };
  img.src = ICON_BASE + encodeURIComponent(name) + '.png';
  icons.set(name, img);
}

function ensureDebuffIcon(debuffId        , src        )       {
  if (debuffIcons.has(debuffId)) return;
  const img = new Image();
  img.onload = () => { drawEdit(); drawPlay(); };
  img.onerror = () => { /* 缺图时保留底层 tile，不阻断编辑 */ };
  img.src = src;
  debuffIcons.set(debuffId, img);
}

// ---------- 画布 ----------
function canvas(which                 )                    { return $(`[data-canvas="${which}"]`) ; }
function fitCanvas(cv                   )                           {
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  const w = cv.clientWidth, h = cv.clientHeight;
  cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr);
  const g = cv.getContext('2d') ; g.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { w, h };
}

function drawEdit()       {
  invalidateSaved();
  if (($('[data-editpanel]')               ).hidden) return;
  const problems = boardProblems();
  const spot = $('[data-locate-cover]')                     ; spot.textContent = '定位全遮盖（'+problems.cover.length+'）'; spot.disabled = !problems.cover.length;
  const float = $('[data-locate-float]')                     ; float.textContent = '定位悬空（'+problems.floating.length+'）'; float.disabled = !problems.floating.length;
  if (fillPreview && fillPreview.source !== st.master) endFillPreview();
  renderTemplateStatistics(st.master);
  renderLayer();
  updateUnityPalette();
  paintCoverBoard();
}

// ---------- 可点试玩会话 ----------

                                                           
                                                                          
                                       
                                                         
                                                                      



                                                    

let playSession                     = null;

/**
 * 阵型 + 副本 → 牌面（逐 bit 对齐主玩原流程）。
 * 把母版 tile 按 TileDatas(Z→Y→X) 顺序交给 generateFaces 出真实 tileId，再装回 Master。
 * 类型池按「关卡序号」选段（config_client_level_tile）。配置没载入时兜底回旧等价预览。
 */
function derivePlayMaster(seedOverride         , job             )         {
  const master = job?.master ?? st.master;
  const dup = job?.dup ?? { ...curDup(), seed: seedOverride ?? curDup().seed };
  const level = job?.level ?? st.levelNum;
  const slot = job?.slot ?? st.slot;
  const useGroups = job ? Boolean(job.seedGroup || job.strategyGroup) : groupRules;
  const pool = levelPools.length ? pickPoolRow(levelPools, level) : null;
  if (!pool) return deriveMaster(master, dup, slot);
  const sorted = sortTilesForSave(master.tiles);
  const dupParam = { a: dup.a, seed: dup.seed, b: dup.b };
  // Phase 3：类型分配前先套策略组重塑阵型标记（C..N → A/B 等）。命中新模板+策略组才生效。
  const strat = useGroups ? currentStrategyResult(sorted, dup.seed, job) : null;
  const genInput = (strat && strat.applied)
    ? sorted.map((t, i) => ({ ...t, tileId: strat.tileIds[i]  }))
    : sorted;
  const seedCfg = useGroups ? currentSeedConfig(job) : null;
  let tileIds          ;
  if (seedCfg) {
    const towerKeys = new Set(master.towers.map(w => `${w.x},${w.y}`));
    tileIds = generateFacesStructured(genInput, (x, y) => towerKeys.has(`${x},${y}`), dupParam, pool, seedCfg);
  } else {
    tileIds = generateFaces(genInput, dupParam, pool, job ? 0 : iapCut).tileIds;
  }
  const tiles = genInput.map((t, i) => ({ ...t, tileId: tileIds[i]  }));
  return { ...master, tiles };
}

/** 当前档位命中的种子组配置（randomSeed_groupID 非空且查到）；否则 null → 走原流程 */
function currentSeedConfig(job             )                                       {
  const g = (job ? job.seedGroup : st.randomSeedGroupId).trim();
  if (g === '') return null;
  const groupId = parseInt(g, 10);
  if (!Number.isFinite(groupId)) return null;
  return tryLoadSeedConfig(seedGroupTable, seedTable, groupId, job?.slot ?? st.slot);
}

/** 当前档位命中的策略组结果（新模板 + strategyGroupId 非空且查到）；否则 null → 不套策略 */
function currentStrategyResult(sorted                 , seedOverride         , job             )                        {
  const g = (job ? job.strategyGroup : st.strategyGroupId).trim();
  if (g === '' || !isNewTemplate((job?.master ?? st.master).templateId)) return null;
  const groupId = parseInt(g, 10);
  if (!Number.isFinite(groupId)) return null;
  const dup = curDup();
  const seed = strategySeed(job?.level ?? st.levelNum, job?.slot ?? st.slot, seedOverride ?? dup.seed, groupId);
  return applyStrategyGroup(sorted, job?.slot ?? st.slot, groupId, strategyGroupTable, seed);
}

/** 牌面签名：派生后的 tile 几何+类型全量 + 背包格。变了就代表要换一局。 */
function playSig(d        , cap        )         {
  return cap + '|' + d.tiles.map(t => `${t.x},${t.y},${t.z},${t.tileId}`).join(';');
}

/** 取当前试玩会话；牌面/背包格变了（或主动重开置空）就按当前档位重新派生一局。 */
function ensurePlaySession()              {
  const d = derivePlayMaster();
  const sig = playSig(d, st.tray);
  if (!playSession || playSession.sig !== sig) {
    playSession = { sig, tiles: d.tiles, removed: d.tiles.map(() => false), bag: [], stage: [], capacity: st.tray, won: false, lost: false, seq: 0 };
  }
  return playSession;
}

/** 主动重开当前这一局（牌面不变，清空已消/背包）。 */
function restartPlay()       { playSession = null; resetSimulation(); drawPlay(); }

/** 改背包容量：当场生效，保留当前牌面/背包/整理区（同步签名，不触发重开）。 */
function setTray(n        )       {
  resetSimulation();
  st.tray = Math.max(1, n);
  const input = $('[data-tray]')                           ;
  if (input) input.value = String(st.tray);
  const s = playSession;
  if (s) {
    s.capacity = st.tray;
    s.sig = playSig(derivePlayMaster(), st.tray);  // 跟 st.tray 对齐，ensurePlaySession 就不会判定要重开
    updatePlayState(s);                              // 缩格可能当场判负、扩格可能解除
  }
  renderBagToggle();
  drawPlay();
  scheduleDraftSave();
}

/** 背包切换按钮文案（显示当前格数）。 */
function renderBagToggle()       {
  const b = $('[data-bagtoggle]'); if (b) b.textContent = `🎒 背包 ${st.tray} 格`;
}

/** 把会话包成道具纯逻辑用的 PropState：geo/removed/bag/stage 共享引用，types 是快照（操作后 syncBack 回填）。 */
function propView(s             )            {
  return { geo: s.tiles, types: s.tiles.map(t => t.tileId), removed: s.removed, bag: s.bag, stage: s.stage, capacity: s.capacity, seq: s.seq };
}
/** 把道具/落子结果写回会话：bag/stage/seq 直接取回，types 变更回填到 tiles[i].tileId（洗牌/磁铁用）。 */
function syncBack(s             , ps           )       {
  s.bag = ps.bag; s.stage = ps.stage; s.seq = ps.seq;
  ps.types.forEach((t, i) => { if (s.tiles[i] .tileId !== t) s.tiles[i] = { ...s.tiles[i] , tileId: t }; });
}
/** 消除后重算胜负：牌面清空 + 背包空 + 整理区空 才算通关（整理区的牌也得消掉）。 */
function updatePlayState(s             )       {
  s.won = s.removed.every(r => r) && s.bag.length === 0 && s.stage.length === 0;
  s.lost = !s.won && s.bag.length >= s.capacity;
}

/** 点一个 tile：落位 → 先消除 → 再判满，顺序对齐主玩 simulate。 */
function playTileAt(i        )       {
  const s = playSession; if (!s || s.won || s.lost || s.removed[i]) return;
  const ps = propView(s);
  propPush(ps, i);
  propResolve(ps);
  syncBack(s, ps);
  updatePlayState(s);
  drawPlay();
}

// ---------- 局内道具（只做功能，不做表现；规则对齐 tile 主玩道具二期，逻辑在 logic.ts） ----------

/** 道具按钮分发：把会话包成 PropState → 调 logic.ts 纯逻辑 → 写回 → 刷新。 */
function runProp(kind        )       {
  const s = ensurePlaySession();
  if (s.won) { toast("已通关，点 ↻ 重开再试道具"); return; }
  const rescueable = kind === "undo" || kind === "spring";
  if (s.lost && !rescueable) { toast("已失败 · 可用撤回/弹簧挽救，或点 ↻ 重开"); return; }
  const ps = propView(s);
  let msg = "";
  switch (kind) {
    case "undo":    { const n = propUndo(ps, 1); msg = n ? `撤回：退回整理区 ${n} 张` : "背包是空的"; break; }
    case "spring":  { const n = propUndo(ps, 3); msg = n ? `弹簧：退回整理区 ${n} 张` : "背包是空的"; break; }
    case "magneto": { const n = propMagneto(ps); msg = n ? `磁铁：吸入 ${n} 张` : "没有可吸的图案"; break; }
    case "crane":   { const n = propCrane(ps, 3); msg = n ? `抓钩：抓进整理区 ${n} 张` : "没有可抓的表面 tile"; break; }
    case "shuffle": { propShuffle(ps, Math.random); msg = "洗牌完成"; break; }
    default: return;
  }
  syncBack(s, ps);
  updatePlayState(s);
  drawPlay();
  toast(msg);
}

/** 点整理区第 k 张 → 送进背包（可能触发消除/判负）。 */
function clickStage(k        )       {
  const s = playSession; if (!s || s.won || s.lost) return;
  const ps = propView(s);
  if (!propStageToBag(ps, k)) return;
  syncBack(s, ps);
  updatePlayState(s);
  drawPlay();
}


function drawPlay()       {
  if (($('.playpanel')               ).hidden) return;
  const cv = canvas('play'); const { w, h } = fitCanvas(cv);
  const g = cv.getContext('2d') ;
  const s = ensurePlaySession();
  const vis = s.tiles.filter((_, i) => !s.removed[i]);
  for (const t of vis) ensureIcon(iconOf(t.tileId));
  const board         = { ...st.master, tiles: vis };
  render(g, w, h, {
    master: board, view: st.playView, currentZ: 9999, upper: 'fade',
    showMaxZ: null, cover: coverFlags(board), colorOf, icons, iconOf, labelOf: editorTileLabel, hover: null, showRange: false, tileBg, debuffIcons,
    tileRect: playRectOf, cropTileBgSides: true, iconOffsetY: -14,
  });
  renderTray();
  renderStage();
}

/** 把一个槽格填成某图案（图标优先，否则色块/编号）。 */
function fillCell(cell             , type        )       {
  cell.classList.add('on');
  const icon = iconOf(type);
  if (icon && icons.get(icon)?.naturalWidth) { const img = document.createElement('img'); img.alt = editorTileLabel(type); img.src = ICON_BASE + encodeURIComponent(icon) + '.png'; cell.appendChild(img); }
  else { cell.style.background = colorOf(type); cell.textContent = editorTileLabel(type); }
}

/** 点击命中：取光标下最靠上（z 最大）且未被遮挡的 tile。 */
function onPlayPointerDown(e              )       {
  const s = ensurePlaySession();
  if (s.won || s.lost) return;
  const cv = canvas('play');
  const rect = cv.getBoundingClientRect();
  const mx = e.clientX - rect.left, my = e.clientY - rect.top;
  let best = -1, bestZ = -Infinity;
  s.tiles.forEach((t, i) => {
    if (s.removed[i]) return;
    const off = towerOffset(st.master.towers, t);
    const r = playRectOf(st.playView, t, off.ox, off.oy);
    if (mx >= r.x && mx <= r.x + r.w && my >= r.y && my <= r.y + r.h && t.z > bestZ) { bestZ = t.z; best = i; }
  });
  if (best < 0) return;
  if (computeCover(s.tiles, s.removed)[best]) { toast('被遮挡，先消上面的 tile'); return; }
  playTileAt(best);
}

/** 背包条 + 胜负横幅。 */
function renderTray()       {
  const s = playSession;
  const box = $('[data-trayui]'); if (!box || !s) return;
  box.innerHTML = '';
  for (let i = 0; i < s.capacity; i++) {
    const cell = document.createElement('div');
    cell.className = 'tslot';
    const type = s.bag[i]?.type;
    if (type !== undefined) fillCell(cell, type);
    box.appendChild(cell);
  }
  const banner = $('[data-playstate]');
  if (banner) {
    const left = s.removed.filter(r => !r).length;
    if (s.won) { banner.textContent = '🎉 通关！'; banner.className = 'pstate ok'; }
    else if (s.lost) { banner.textContent = '💥 背包满了，失败 · 点 ↻ 重开'; banner.className = 'pstate err'; }
    else { banner.textContent = `剩 ${left} 个 · 背包 ${s.bag.length}/${s.capacity} · 整理区 ${s.stage.length}`; banner.className = 'pstate'; }
  }
}

/** 整理区：撤回/弹簧/抓钩放进来的牌，点一下送进背包。 */
function renderStage()       {
  const s = playSession;
  const box = $('[data-stageui]'); if (!box || !s) return;
  box.innerHTML = '';
  const label = $('[data-stagelabel]');
  if (label) label.textContent = `整理区 (${s.stage.length})`;
  s.stage.forEach((item, k) => {
    const cell = document.createElement('div');
    cell.className = 'tslot stcell';
    fillCell(cell, item.type);
    cell.title = '点击送进背包';
    cell.onclick = () => clickStage(k);
    box.appendChild(cell);
  });
  if (s.stage.length === 0) {
    const hint = document.createElement('span');
    hint.className = 'stempty';
    hint.textContent = '空 · 撤回/弹簧/抓钩的 tile 会放到这里';
    box.appendChild(hint);
  }
}

function draw()       { drawEdit(); drawPlay(); }

// ---------- 渲染面板 ----------
function renderAll()       {
  renderTemplateMetadata();
  renderColorBar();
  renderBrushes();
  renderGroupRow();
  renderDupRow();
  renderDupDrawer();
  renderGroupConfig();
  renderBagToggle();
  renderLayer();
  draw();
  renderRightPanels();
  updateUndoButtons();
}

function renderCats()       {}

/** 颜色词 → 中文名（给 title / “其他”标签用） */
const COLOR_NAMES                         = {
  red: '红', orange: '橙', yellow: '黄', green: '绿', cyan: '青', blue: '蓝',
  purple: '紫', pink: '粉', brown: '棕', white: '白', color: '彩',
};

/** 普通笔刷的颜色筛选条（仅“普通”分类显示） */
function renderColorBar()       {}

function renderBrushes()       {
  renderUnityPalette();
}

/** 把选中的 tile 批量换成同一个 tileId（走撤销栈，保留选择） */
function replaceSelected(tileId        )       {
  const before = st.master;
  const r = replaceTiles(st.master, st.selection, tileId);
  if (!r.changed) return;
  st.master = r.master;
  history.push(before); updateUndoButtons();
  drawEdit(); drawPlay(); renderRightPanels();
  scheduleDraftSave();
  toast(`已把选中的 ${st.selection.size} 张牌换成「${editorTileLabel(tileId)}」`);
}

/** 选择态开关 / 提示 / 光标 */
function renderSelMode()       {
  const chip = $('[data-selmode]'); if (chip) { chip.classList.toggle('act', st.selectMode); chip.setAttribute('aria-pressed', String(st.selectMode)); }
  canvas('edit').style.cursor = st.selectMode ? 'pointer' : 'crosshair';
  renderSelHint();
}
function renderSelHint()       {
  const h = $('[data-selhint]'); if (!h) return;
  (h               ).hidden = !st.selectMode;
  h.textContent = !st.selectMode ? '' : st.selection.size ? `选中 ${st.selection.size} 个 · 点笔刷换成它` : '点 tile 选中（可多选）';
}
function clearSelection()       { if (st.selection.size) { st.selection.clear(); renderSelHint(); } }

function sameBrush(a       , b       )          { return a.kind === b.kind && a.value === b.value; }

function renderLayer()       {
  const input = $('[data-showzlabel]')                    ;
  const maxZ = displayedMaxZ();
  input.value = maxZ ? String(maxZ) : '无限制';
  ($('[data-showz="dn"]')                     ).disabled = maxZ === 0;
  ($('[data-showz="up"]')                     ).disabled = maxZ >= maxEditableZ();
}

function renderGroupRow()       {
  if (!document.querySelector('[data-grouprow]')) return;
  const box = $('[data-grouprow]'); if (!box) return;
  box.innerHTML = '';
  const mk = (gid        , label        , color               ) => {
    const el = document.createElement('span');
    el.className = 'chip' + (st.activeGroup === gid ? ' act' : '');
    el.textContent = label;
    if (color && st.activeGroup !== gid) { el.style.borderColor = color; el.style.color = color; }
    el.onclick = () => { st.activeGroup = gid; renderGroupRow(); };
    box.appendChild(el);
  };
  mk(NO_GROUP, '无', null);
  for (let i = 0; i < GROUP_COUNT; i++) mk(i, String(i + 1), GROUP_COLORS[i] );
  const gv = $('[data-groupview]'); if (gv) gv.classList.toggle('act', st.showGroups);
}

function saveGroup()       {
  if (st.activeGroup === NO_GROUP) { toast('先选一个分组（1–8）'); return; }
  const sub = subMasterOfGroup(st.master, st.activeGroup, '');
  if (sub.tiles.length === 0) { toast('该分组还没有 tile'); return; }
  const id = (prompt('存为新模板号：', `${currentId()}_g${st.activeGroup + 1}`) || '').trim();
  if (!id) return;
  const save = toSave({ ...sub, templateId: id });
  save.tiles = sortTilesForSave(save.tiles);
  download(`${id}_m.bytes`, encodeLevelSave(save));
  toast(`已导出分组 ${st.activeGroup + 1} → ${id}_m.bytes（${save.tiles.length} tile）`);
}

// DupPara 档位
function curDup()          { return st.dup[st.slot] ?? DEFAULT_DUP; }
function renderDupRow()       {
  const row = $('[data-duprow]'); if (!row) return;
  row.innerHTML = '';
  for (let i = 0; i < DUP_SLOTS; i++) {
    const c = document.createElement('span');
    c.className = 'dchip' + (i === st.slot ? ' on' : '');
    c.textContent = String(i + 1);
    c.onclick = () => { st.slot = i; loadDupFields(); renderDupRow(); drawPlay(); renderRightPanels(); };
    row.appendChild(c);
  }
  loadDupFields();
  const slotLab = $('[data-slot]'); if (slotLab) slotLab.textContent = `第 ${st.slot + 1} 次玩`;
}
function loadDupFields()       {
  const d = curDup();
  for (const f of $$('[data-dup] input')) {
    const key = (f                    ).dataset.p                 ;
    (f                    ).value = String(d[key]);
  }
}
function commitDupField(key               , raw        )       {
  st.dup = updateDupSlot(st.dup, st.slot, key, raw);
  loadDupFields(); renderDupDrawer();
  drawPlay(); renderRightPanels();
  scheduleDraftSave();
}

/** 把 st 的策略组/种子组同步到输入框 */
function renderGroupConfig()       {
  const sg = $('[data-strategygroup]')                           ;
  const rg = $('[data-seedgroup]')                           ;
  if (sg) sg.value = st.strategyGroupId;
  if (rg) rg.value = st.randomSeedGroupId;
}
/** 归一化 int 文本：只留数字，空保持空 */
function normIntField(raw        )         {
  const s = raw.replace(/[^0-9]/g, '');
  return s;
}

// ---------- 右栏：校验 / 统计 / 推演 ----------
function renderRightPanels()       {
  const d = derivePlayMaster();
  const play             = d.tiles.map(t => ({ x: t.x, y: t.y, z: t.z, type: t.tileId }));
  // 校验
  const issues = $('[data-issues]'); if (issues) {
    issues.innerHTML = '';
    const li                     = [];
    if (play.length === 0) li.push(['e', '母版还没有 tile']);
    if (groupRules && st.randomSeedGroupId.trim() !== '') {
      const cfg = currentSeedConfig();
      if (cfg) li.push(['o', `种子组 ${st.randomSeedGroupId} · 档${st.slot + 1} → seed ${cfg.seedId}：已套结构化流程`]);
      else li.push(['w', `种子组 ${st.randomSeedGroupId} · 档${st.slot + 1}：查无 seed 配置，按原流程`]);
    }
    if (groupRules && st.strategyGroupId.trim() !== '') {
      if (!isNewTemplate(st.master.templateId)) {
        li.push(['w', `策略组 ${st.strategyGroupId}：当前模板非新模板（需含 model/hhmy/zwd），策略不生效`]);
      } else {
        const sr = currentStrategyResult(sortTilesForSave(st.master.tiles));
        if (!sr || sr.useStrategyType === UseStrategyType.NoUse) {
          li.push(['w', `策略组 ${st.strategyGroupId} · 档${st.slot + 1}：该档不使用策略（NoUse）`]);
        } else {
          const kind = sr.useStrategyType === UseStrategyType.RandomUse ? '随机用' : '顺序用';
          const eff = sr.effective.length ? sr.effective.join(';') : '无';
          const extra = sr.failed.length ? ` · 失败顺延 ${sr.failed.join(';')}` : '';
          li.push(['o', `策略组 ${st.strategyGroupId} · 档${st.slot + 1}（${kind}）：生效策略 ${eff}${extra}（种子化等价预览，非逐 bit 还原）`]);
        }
      }
    }
    for (const u of findUnclearableTypes(play.map(t => t.type), 3)) {
      const name = editorTileLabel(u.type);
      li.push(['e', `${name} 共 ${u.count} 个，不是 3 的倍数，这档赢不了`]);
    }
    const s = masterStats(st.master);
    const bad = fullCoverViolationIndices(st.master);
    if (bad.size > 0) li.push(['e', `相邻层完全覆盖 ${bad.size} 个 tile（同位置上下层），请错开半格`]);
    if (s.towers > 0 && s.debuffs > 0) li.push(['o', `塔 ${s.towers} · debuff ${s.debuffs}`]);
    if (li.length === 0) li.push(['o', '各类型均为 3 的倍数 ✓']);
    for (const [sev, text] of li) { const el = document.createElement('li'); el.className = sev; el.textContent = text; issues.appendChild(el); }
  }
  // 统计
  const statBox = $('[data-stats]'); if (statBox) {
    const s = masterStats(st.master);
    statBox.innerHTML = '';
    const rows                     = [
      ['总 tile / 层', `${s.tiles} · ${s.layers}`],
      ['随机槽 / 塔 / debuff', `${s.randomSlots} · ${s.towers} · ${s.debuffs}`],
      ['本档类型数', `A${curDup().a} · B${curDup().b}`],
    ];
    for (const [k, v] of rows) { const a = document.createElement('span'); a.textContent = k; const b = document.createElement('b'); b.textContent = v; statBox.appendChild(a); statBox.appendChild(b); }
  }
}

function runSim()       { void runClearSimulation(); }

// ---------- 摆放 ----------
let painting = false;
let strokeStart                = null;
let lastCell = '';

function gridUnder(cv                   , e              , view        )                             {
  const rect = cv.getBoundingClientRect();
  return screenToGrid(view, e.clientX - rect.left, e.clientY - rect.top);
}

/** 锚点是否落在 17×19 摆放范围内（位置 0..16 × 0..18，同 Values.SupportMaxPositionX/Y） */
function inRange(x        , y        )          {
  return x >= 0 && x <= RANGE_MAX_X - 1 && y >= 0 && y <= RANGE_MAX_Y - 1;
}

function applyBrush(gx        , gy        , erase         )          {
  const parity = layerParity(st.master.tiles, st.currentZ);
  const a = snapAnchor(gx, gy, st.whole, parity);
  const cx = Math.round(gx), cy = Math.round(gy);
  let res                                                         ;
  if (erase || st.brush.kind === 'erase') {
    // 级联删：debuff → 最上层 tile → 塔（删栈顶，和叠放对称）
    res = removeDebuffAt(st.master, st.currentZ, cx, cy);
    if (!res.changed) res = st.showMode === 'all' ? removeTopTileAt(st.master, cx, cy) : removeTileAt(st.master, st.showZ, cx, cy);
    if (!res.changed) res = removeTowerAt(st.master, a.x, a.y);
  } else if (st.brush.kind === 'tower') {
    if (!inRange(a.x, a.y)) return false;
    res = placeTower(st.master, a.x, a.y, st.brush.value);
  } else if (st.brush.kind === 'debuff') {
    res = addDebuffAt(st.master, cx, cy, st.brush.value);
  } else {
    if (!inRange(a.x, a.y)) return false;
    // 自动叠放：footprint 有重叠就堆到栈顶，否则落在当前层——范围内每个点都放得下
    const z = st.showMode === 'all' ? nextStackZ(st.master, a.x, a.y, st.currentZ) : st.showZ;
    // 规则：相邻层不能完全覆盖——同锚点正上/下方已有 tile 时拒绝，必须错开半格
    // Overlap across layers is advisory in the workbench.
    res = placeTile(st.master, a.x, a.y, z, st.brush.value);
  }
  if (res.changed) { st.master = res.master; return true; }
  return false;
}

function onPointerMove(e              )       {
  const cv = canvas('edit');
  if (st.selectMode) { st.hover = null; return; }  // 选择态不画摆放预览
  const { gx, gy } = gridUnder(cv, e, st.editView);
  const parity = layerParity(st.master.tiles, st.currentZ);
  const a = snapAnchor(gx, gy, st.whole, parity);
  const erasing = st.brush.kind === 'erase';
  const anchored = st.brush.kind === 'tower' || (!erasing && st.brush.kind !== 'debuff');
  const isTile = !erasing && st.brush.kind !== 'tower' && st.brush.kind !== 'debuff';
  let blocked = anchored && !inRange(a.x, a.y);
  // 规则：相邻层不能完全覆盖——预览时若会完全盖住正下方同锚点 tile，标红
  if (isTile && !blocked && inRange(a.x, a.y)) {
    const z = st.showMode === 'all' ? nextStackZ(st.master, a.x, a.y, st.currentZ) : st.showZ;
    if (z < 0) blocked = true;
  }
  st.hover = {
    kind: erasing ? 'erase' : st.brush.kind === 'tower' ? 'tower' : st.brush.kind === 'debuff' ? 'debuff' : 'place',
    x: a.x, y: a.y, color: st.brush.color, blocked,
  };
  if (painting) {
    const cell = `${a.x},${a.y}`;
    if (cell !== lastCell) { lastCell = cell; if (applyBrush(gx, gy, e.buttons === 2)) { renderAfterEdit(false); } }
  }
  drawEdit();
}

function onPointerDown(e              )       {
  if (panKey) return;
  const cv = canvas('edit');
  // 选择态：点已摆 tile 选中/取消（取最上层），点空白清空；不摆、不拖涂
  if (st.selectMode) {
    const { gx, gy } = gridUnder(cv, e, st.editView);
    const idx = topTileIndexAt(st.master.tiles, Math.round(gx), Math.round(gy));
    if (idx >= 0) {
      const key = tileKey(st.master.tiles[idx] );
      if (st.selection.has(key)) st.selection.delete(key); else st.selection.add(key);
    } else {
      st.selection.clear();
    }
    renderSelHint(); drawEdit();
    return;
  }
  cv.setPointerCapture(e.pointerId);
  painting = true; strokeStart = st.master; lastCell = '';
  const { gx, gy } = gridUnder(cv, e, st.editView);
  const parity = layerParity(st.master.tiles, st.currentZ);
  const a = snapAnchor(gx, gy, st.whole, parity);
  lastCell = `${a.x},${a.y}`;
  if (applyBrush(gx, gy, e.button === 2)) renderAfterEdit(false);
  else if (e.button !== 2 && st.brush.kind !== 'erase' && st.brush.kind !== 'tower' && st.brush.kind !== 'debuff' && inRange(a.x, a.y)) {
    const z = st.showMode === 'all' ? nextStackZ(st.master, a.x, a.y, st.currentZ) : st.showZ;
    if (fullyCoversAdjacent(st.master, a.x, a.y, z)) toast('相邻层不能完全覆盖，请错开半格摆放');
  }
  drawEdit();
}

function onPointerUp()       {
  if (painting && strokeStart && strokeStart !== st.master) { history.push(strokeStart); updateUndoButtons(); }
  painting = false; strokeStart = null;
}

/** 一次编辑后刷新（pushHistory=是否立即记撤销；摆放用 stroke 结束才记） */
function renderAfterEdit(pushHistory         , before         )       {
  if (pushHistory && before) { history.push(before); updateUndoButtons(); }
  drawPlay(); renderRightPanels();
  scheduleDraftSave();
}

function edit(fn                                            )       {
  if (fillPreview) return;
  const before = st.master;
  const r = fn(st.master);
  const next = 'master' in (r                      ) ? (r                      ).master : (r          );
  if (next === before) return;
  st.master = next;
  clearSelection();  // 结构性改动后旧选择失效（换 tile 走 replaceSelected，不经这里）
  renderAfterEdit(true, before);
  drawEdit();
}

// ---------- 缩放 / 平移 ----------
let panKey = false;
let panning = false; let panStart = { x: 0, y: 0, ox: 0, oy: 0 };
function zoom(view        , factor        , cx        , cy        )       {
  const s2 = Math.max(6, Math.min(160, view.s * factor));
  view.ox = cx - (cx - view.ox) * (s2 / view.s);
  view.oy = cy - (cy - view.oy) * (s2 / view.s);
  view.s = s2;
}
function zoomLabel()       {
  const e = $('[data-zl]'); if (e) e.textContent = Math.round((st.editView.s / 24) * 100) + '%';
  const p = $('[data-pzl]'); if (p) p.textContent = Math.round((st.playView.s / 24) * 100) + '%';
}

// ---------- 文件 I/O ----------
function download(name        , data            )       {
  const blob = new Blob([data], { type: 'application/octet-stream' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a'); a.href = url; a.download = name; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function currentId()         { return st.master.templateId || 'untitled'; }
function encodeCurrent(id        )             {
  const save = toSave({ ...st.master, templateId: id });
  save.tiles = sortTilesForSave(save.tiles);
  save.towers = save.towers.filter(Boolean);
  return encodeLevelSave(save);
}

async function exportBytes()                {
  const input = $('[data-tplid]')                    , name = input.value;
  if (busy) return;
  if (!/^[A-Za-z0-9]{1,80}$/.test(name)) { input.setAttribute('aria-invalid','true'); input.focus(); localStatus('另存时，模板 ID 只能使用英文字母和数字，最多 80 个字符。',true); return; }
  const signature = currentSignature(), serial = sourceSerial;
  busy = true; ($('[data-export]')                     ).disabled = true; invalidateSaved();
  localStatus('正在另存新模板…');
  try {
    const raw = encodeCurrent(st.master.templateId || 'NewTemplate');
    const source = sourceRaw ? (await writeLocal('/api/upload', sourceRaw)).key : sourceKey;
    const result = await writeLocal('/api/save?'+new URLSearchParams({name,source}),raw);
    if (sourceSerial === serial) { savedName = result.name; savedSignature = signature; }
    localStatus('已保存到输出模板：'+result.file+(result.warnings.length?'。有 '+result.warnings.length+' 项摆放提示。':'。'));
  } catch(e) { localStatus((e         ).message,true); }
  finally { busy = false; ($('[data-export]')                     ).disabled = false; invalidateSaved(); }
}

/** Write bytes first. Later failures must never conceal the successful file write. */
async function writeRepo()                {
  if (busy || !savedName || savedSignature !== currentSignature() || ($('[data-tplid]')                    ).value !== savedName) return;
  busy = true; invalidateSaved(); localStatus('正在同步新模板…');
  try {
    const result = await writeLocal('/api/sync?'+new URLSearchParams({name:savedName}));
    savedName=''; localStatus('已同步到模板仓库：'+result.file+'。');
  } catch(e) { localStatus((e         ).message+' 本地输出仍保留。',true); }
  finally { busy = false; invalidateSaved(); }
}

let metadataTemplateId = '';
let metadataDirty = false;
let metadataEditGeneration = 0;

function markTemplateMetadataEdited()       {
  metadataDirty = true;
  metadataEditGeneration++;
}

function renderTemplateMetadata()       {
  const name = $('[data-templatename]')                           ;
  const author = $('[data-templateauthor]')                           ;
  if (!name || !author) return;
  const id = currentId();
  if (metadataTemplateId === id && metadataDirty) return;
  if (metadataTemplateId !== id) metadataEditGeneration++;
  const template = levelData?.templates[id];
  name.value = template?.name ?? (id === 'untitled' ? '' : id);
  author.value = template?.author ?? $('#authWho')?.getAttribute('data-username') ?? '';
  metadataTemplateId = id;
  metadataDirty = false;
}

async function saveTemplateMetadata()                {
  const id = currentId();
  if (id === 'untitled') { toast('先填模板号'); return; }
  const editGeneration = metadataEditGeneration;
  const name = ($('[data-templatename]')                    ).value.trim();
  const author = ($('[data-templateauthor]')                    ).value.trim();
  const button = $('[data-templatemetasave]')                            ;
  if (button?.disabled) return;
  if (button) button.disabled = true;
  try {
    const store = await ensureLevelDataLoaded();
    await patchLevelData(store.revision, [{ op: store.templates[id] ? 'update' : 'add', entity: 'templates', key: id,
      values: { templateId: id, name, author } }]);
    if (currentId() === id && metadataEditGeneration === editGeneration) metadataDirty = false;
    await refreshLevelData();
    const status = $('[data-leveldatastatus]'); if (status) status.hidden = true;
    toast(`已保存 ${id} 的模板名称和作者`);
  } catch (e) {
    reportLevelDataError('模板信息保存失败：', e);
  } finally {
    if (button) button.disabled = false;
  }
}

async function writeConfigRow(id        , templateId = st.master.templateId || currentId())                {
  const store = await ensureLevelDataLoaded();
  const existing = store.levels[id];
  const base = existing ? levelRecordToRow(existing) : newLevelRow(id, id);
  const row = { ...base, templateId, dup: st.dup.slice(), strategyGroupId: st.strategyGroupId, randomSeedGroupId: st.randomSeedGroupId };
  await patchLevelData(store.revision, levelRowOperations(store, row));
  await refreshLevelData();
  const status = $('[data-leveldatastatus]'); if (status) status.hidden = true;
}

async function saveDupConfig()                {
  const id = savableLevelId(currentLevelId || currentId());
  if (!id) { toast('先载入或填写副本 ID'); return; }
  try {
    await writeConfigRow(id);
    await ensureLevelDrawerLoaded();
    toast(`已保存 ${id} 的 DupPara1–10`);
  } catch (e) {
    reportLevelDataError('保存失败：', e);
  }
}

async function copyDupParams()                {
  const text = copyDupParasText(st.dup);
  const buttons = $$('[data-duparacopy]')                       ;
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
    } else {
      const area = document.createElement('textarea');
      area.value = text; area.style.position = 'fixed'; area.style.opacity = '0';
      document.body.appendChild(area); area.select();
      const copied = document.execCommand('copy');
      area.remove();
      if (!copied) throw new Error('浏览器拒绝复制');
    }
    if (buttons.length) {
      for (const button of buttons) button.textContent = '已复制';
      window.setTimeout(() => {
        for (const button of buttons) button.textContent = '复制参数';
      }, 1200);
    }
  } catch (e) {
    alert('复制失败：' + (e         ).message);
  }
}

/** 从服务器按模板号载入 {id}_m.bytes */
async function loadTemplate(id        )                {
  if (!id) { localStatus('请先输入模板 ID。',true); return; }
  if (busy || !allowReplace()) return;
  const request = ++readRequest;
  localStatus('正在读取模板…');
  try {
    const res = await fetch(TEMPLATE_URL(id.replace(/_m\.bytes$/, '')));
    if (!res.ok) throw new Error((await res.json()).error || `模板 ${id} 不存在`);
    const buf = new Uint8Array(await res.arrayBuffer());
    if (request !== readRequest) return;
    st.dup = curveDefault(); st.strategyGroupId = ''; st.randomSeedGroupId = '';
    adoptMaster(fromSave(decodeLevelSave(buf)), `${id.replace(/_m\.bytes$/, '')}_m.bytes`);
    adoptSource(res.headers.get('X-Source') || '', buf);
    renderGroupConfig();
    await loadDupFromConfig(st.master.templateId);
    scheduleDraftSave();
    toast(`已载入 ${id}_m.bytes`);
  } catch (e) {
    localStatus('载入失败：' + (e         ).message,true);
  }
}

/** config_level.csv 里有这个 id 的行 → 把它的 DupPara 读进来 */
async function loadDupFromConfig(id        )                {
  const generation = sourceSerial;
  try {
    await ensureLevelDataLoaded();
    if (generation !== sourceSerial || st.master.templateId !== id || batchPhase !== 'single') return;
    const cfg = drawerConfig ;
    const i = findRow(cfg, id);
    if (i >= 0) {
      const r = cfg.rows[i] ;
      st.dup = r.dup.slice(); st.slot = 0;
      st.strategyGroupId = r.strategyGroupId; st.randomSeedGroupId = r.randomSeedGroupId;
      renderDupRow(); renderDupDrawer(); renderGroupConfig(); restartPlay(); renderRightPanels();
    }
  } catch { /* 没有就用默认曲线 */ }
}

function applyLevelRow(row          )       {
  st.dup = row.dup.slice(); st.slot = 0;
  st.strategyGroupId = row.strategyGroupId; st.randomSeedGroupId = row.randomSeedGroupId;
  renderDupRow(); renderDupDrawer(); renderGroupConfig(); restartPlay(); renderRightPanels();
}

async function loadLevelRow(row          )                {
  if (!row.templateId.trim() || busy || !allowReplace()) return;
  try {
    const res = await fetch(TEMPLATE_URL(row.templateId));
    if (!res.ok) throw new Error(`模板 ${row.templateId} 不存在`);
    const buf = new Uint8Array(await res.arrayBuffer());
    adoptMaster(fromSave(decodeLevelSave(buf)), `${row.templateId}_m.bytes · ${row.levelId}`);
    adoptSource(res.headers.get('X-Source') || '', buf);
    currentLevelId = row.levelId;
    applyLevelRow(row);
    drawer = reduceDrawer(drawer, { type: 'select', levelId: row.levelId, loadable: true });
    indexDrawer = { ...indexDrawer, selectedLevelId: row.levelId };
    renderLevelDrawer();
    renderIndexDrawer();
    scheduleDraftSave();
    toast(`已载入 ${row.levelId}`);
  } catch (e) {
    localStatus('载入失败：' + (e         ).message,true);
  }
}

function renderIndexDrawer()       {
  document.body.classList.toggle('indexdrawer-open', indexDrawer.open);
  renderDrawerLayout();
  const toggle = $('[data-indexdrawertoggle]');
  const body = $('[data-indexdrawerbody]');
  const count = $('[data-indexdrawercount]');
  const state = $('[data-indexdrawerstate]');
  const table = $('[data-indextable]')                           ;
  const rows = $('[data-indexrows]');
  if (toggle) toggle.setAttribute('aria-expanded', String(indexDrawer.open));
  if (body) body.setAttribute('aria-hidden', String(!indexDrawer.open));
  const visibleRows = filterLevelIndexRows(indexDrawer.rows, indexDrawerQuery);
  if (count) count.textContent = indexDrawer.status === 'ready'
    ? indexDrawerQuery.trim() ? `显示 ${visibleRows.length} / ${indexDrawer.rows.length}` : `显示前 ${visibleRows.length} / ${indexDrawer.rows.length}`
    : indexDrawer.open ? '正在加载…' : '点击展开';
  if (!state || !table || !rows) return;
  if (indexDrawer.status !== 'ready' || indexDrawer.rows.length === 0) {
    table.hidden = true; state.hidden = false;
    state.textContent = indexDrawer.status === 'error' ? indexDrawer.error : indexDrawer.status === 'loading'
      ? '正在加载关卡数据…'
      : indexDrawer.status === 'ready' ? '暂无关卡' : '点击“关卡列表”加载';
  } else {
    state.hidden = true; table.hidden = false; rows.innerHTML = '';
    for (const item of visibleRows) {
      const tr = document.createElement('tr');
      tr.tabIndex = item.loadable ? 0 : -1;
      tr.classList.toggle('disabled', !item.loadable);
      tr.classList.toggle('selected', item.levelId === indexDrawer.selectedLevelId);
      tr.setAttribute('aria-disabled', String(!item.loadable));
      const values = [String(item.levelIndex), item.levelId, item.templateId || '—'];
      for (const value of values) { const td = document.createElement('td'); td.textContent = value; td.title = value; tr.appendChild(td); }
      const activate = ()       => {
        if (!item.loadable || !drawerConfig) return;
        const row = drawerConfig.rows.find(r => r.levelId === item.levelId);
        if (row) {
          st.levelNum = Math.max(1, item.levelIndex);
          const levelInput = $('[data-levelnum]')                           ;
          if (levelInput) levelInput.value = String(st.levelNum);
          loadLevelRow(row);
        }
      };
      tr.onclick = activate;
      tr.onkeydown = e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); activate(); } };
      rows.appendChild(tr);
    }
  }
  requestAnimationFrame(() => draw());
}

function renderLevelDrawer()       {
  document.body.classList.toggle('drawer-open', drawer.open);
  renderDrawerLayout();
  const toggle = $('[data-drawertoggle]');
  const body = $('[data-drawerbody]');
  const count = $('[data-drawercount]');
  const state = $('[data-drawerstate]');
  const table = $('[data-leveltable]')                           ;
  const rows = $('[data-levelrows]');
  if (toggle) toggle.setAttribute('aria-expanded', String(drawer.open));
  if (body) body.setAttribute('aria-hidden', String(!drawer.open));
  const visibleRows = filterLevelListRows(drawer.rows, drawerQuery);
  if (count) count.textContent = drawer.status === 'ready'
    ? drawerQuery.trim() ? `显示 ${visibleRows.length} / ${drawer.rows.length}` : `显示前 ${visibleRows.length} / ${drawer.rows.length}`
    : drawer.open ? '正在加载…' : '点击展开';
  if (!state || !table || !rows) return;
  if (drawer.status !== 'ready' || drawer.rows.length === 0) {
    table.hidden = true; state.hidden = false;
    state.textContent = drawer.status === 'error' ? drawer.error : drawer.status === 'loading' ? '正在加载模板数据…' : drawer.status === 'ready' ? '暂无模板' : '点击“模板列表”加载';
  } else {
    state.hidden = true; table.hidden = false; rows.innerHTML = '';
    for (const item of visibleRows) {
      const tr = document.createElement('tr');
      tr.tabIndex = item.loadable ? 0 : -1;
      tr.classList.toggle('disabled', !item.loadable);
      tr.classList.toggle('selected', item.levelId === drawer.selectedLevelId);
      tr.setAttribute('aria-disabled', String(!item.loadable));
      const values = [item.levelId, item.des || '—', item.templateId || '—', item.randomTypeLabel, item.strategyGroupId || '—', item.randomSeedGroupId || '—'];
      for (const value of values) { const td = document.createElement('td'); td.textContent = value; td.title = value; tr.appendChild(td); }
      const activate = ()       => {
        if (!item.loadable || !drawerConfig) return;
        const row = drawerConfig.rows.find(r => r.levelId === item.levelId);
        if (row) loadLevelRow(row);
      };
      tr.onclick = activate;
      tr.onkeydown = e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); activate(); } };
      rows.appendChild(tr);
    }
  }
  requestAnimationFrame(() => draw());
}

function renderDrawerLayout()       {
  const heights = drawerHeights(dupDrawer.open, indexDrawer.open, drawer.open, window.innerHeight);
  document.body.style.setProperty('--dupdrawer-h', `${heights.dup}px`);
  document.body.style.setProperty('--indexdrawer-h', `${heights.index}px`);
  document.body.style.setProperty('--leveldrawer-h', `${heights.level}px`);
  document.body.style.setProperty('--drawerdock-h', `${drawerDockHeight(heights)}px`);
  requestAnimationFrame(() => draw());
}

function renderDupDrawer()       {
  document.body.classList.toggle('dupdrawer-open', dupDrawer.open);
  const toggle = $('[data-duparatoggle]');
  const body = $('[data-duparabody]');
  const count = $('[data-duparacount]');
  const rows = $('[data-dupararows]');
  if (toggle) toggle.setAttribute('aria-expanded', String(dupDrawer.open));
  if (body) body.setAttribute('aria-hidden', String(!dupDrawer.open));
  if (count) count.textContent = `${currentLevelId || currentId()} · 10 档`;
  if (rows) {
    rows.innerHTML = '';
    for (let slot = 0; slot < DUP_SLOTS; slot++) {
      const value = st.dup[slot] ?? DEFAULT_DUP;
      const tr = document.createElement('tr');
      tr.classList.toggle('current', slot === st.slot);
      const label = document.createElement('td');
      label.textContent = `DupPara${slot + 1}`;
      tr.appendChild(label);
      for (const key of ['a', 'seed', 'lucky', 'b']         ) {
        const td = document.createElement('td');
        const input = document.createElement('input');
        input.className = 'in'; input.inputMode = 'numeric'; input.value = String(value[key]);
        input.setAttribute('aria-label', `DupPara${slot + 1} ${key}`);
        input.onchange = () => {
          st.dup = updateDupSlot(st.dup, slot, key, input.value);
          input.value = String((st.dup[slot] ?? DEFAULT_DUP)[key]);
          if (slot === st.slot) loadDupFields();
          restartPlay(); renderRightPanels();
          scheduleDraftSave();
        };
        td.appendChild(input); tr.appendChild(td);
      }
      rows.appendChild(tr);
    }
  }
  renderDrawerLayout();
}

function toggleDupDrawer()       {
  dupDrawer = reduceDupDrawer(dupDrawer, { type: 'toggle' });
  renderDupDrawer();
}

function closeDupDrawer()       {
  dupDrawer = reduceDupDrawer(dupDrawer, { type: 'close' });
  renderDupDrawer();
}

async function ensureIndexDrawerLoaded()                {
  if (indexDrawer.status === 'ready') return;
  try {
    await ensureLevelDataLoaded();
  } catch (e) {
    indexDrawer = { ...indexDrawer, status: 'error', rows: [], error: '关卡列表加载失败：' + (e         ).message };
  }
  renderIndexDrawer();
}

function toggleIndexDrawer()       {
  const open = !indexDrawer.open;
  indexDrawer = { ...indexDrawer, open, status: open && indexDrawer.status === 'idle' ? 'loading' : indexDrawer.status };
  renderIndexDrawer();
  if (open && indexDrawer.status !== 'ready') ensureIndexDrawerLoaded();
}

function closeIndexDrawer()       {
  indexDrawer = { ...indexDrawer, open: false };
  renderIndexDrawer();
}

async function ensureLevelDrawerLoaded()                {
  if (drawerConfig) return;
  try {
    await ensureLevelDataLoaded();
  } catch (e) {
    drawer = reduceDrawer(drawer, { type: 'failed', error: '模板列表加载失败：' + (e         ).message });
  }
  renderLevelDrawer();
}

function toggleLevelDrawer()       {
  drawer = reduceDrawer(drawer, { type: 'toggle' });
  renderLevelDrawer();
  if (drawer.open && drawer.status === 'loading') ensureLevelDrawerLoaded();
}

function closeLevelDrawer()       {
  drawer = reduceDrawer(drawer, { type: 'close' });
  renderLevelDrawer();
}

function adoptMaster(m        , fnLabel        )       {
  st.master = m; history.clear(); st.currentZ = 0; st.showMode = 'all'; st.showZ = 0; st.slot = 0; clearSelection();
  currentLevelId = m.templateId;
  const idInput = $('[data-tplid]')                           ;
  if (idInput && m.templateId) idInput.value = m.templateId;
  st.editView = fitView(m, canvas('edit').clientWidth, canvas('edit').clientHeight);
  st.playView = { ...st.editView };
  setFn(fnLabel); renderAll(); zoomLabel();
}

async function loadTemplateList()                {
  try {
    const res = await fetch('/api/templates'); if (!res.ok) return;
    const j = await res.json();
    const dl = $('[data-tpllist]'); if (!dl || !Array.isArray(j.ids)) return;
    dl.innerHTML = '';
    for (const id of j.ids            ) { const o = document.createElement('option'); o.value = id; dl.appendChild(o); }
  } catch { /* 离线忽略 */ }
}

async function openFile(file      )                {
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
  } catch (e) { localStatus('打开失败：' + (e         ).message,true); }
  finally { pendingUpload = false; }
}

const IMPORT_CSV_PROMPT = `请一次选择以下 5 个 CSV 文件：\n\n${REQUIRED_LEVEL_CSV_NAMES.join('\n')}\n\n文件名必须完全一致，五表将作为一个批次导入 JSON，并由服务端同步 CSV。`;

function csvValidationMessage(validation                                          )         {
  const lines = ['CSV 文件选择不完整，尚未导入：'];
  if (validation.missing.length) lines.push(`缺少：${validation.missing.join('、')}`);
  if (validation.duplicates.length) lines.push(`重复：${validation.duplicates.join('、')}`);
  if (validation.extras.length) lines.push(`不需要：${validation.extras.join('、')}`);
  return lines.join('\n');
}

async function importLevelCsvFiles(selected                   )                {
  const files = Array.from(selected);
  const validation = validateLevelCsvNames(files.map(file => file.name));
  if (!validation.ok) { alert(csvValidationMessage(validation)); return; }
  if (!confirm(`将作为一个批次导入以下 5 个文件：\n\n${REQUIRED_LEVEL_CSV_NAMES.join('\n')}\n\n服务端将更新 JSON 并同步 CSV。确定继续吗？`)) return;
  try {
    const store = await ensureLevelDataLoaded();
    const result = await importLevelCsvBundle(files, store.revision);
    await refreshLevelData();
    const report = result.report;
    const message = `导入完成（版本 ${result.revision}）：新增 ${report.added}，更新 ${report.updated}，未变 ${report.unchanged}，恢复 ${report.restored}，标记缺失 ${report.markedMissing}。`;
    const status = $('[data-leveldatastatus]');
    if (status) { status.textContent = message + (report.warnings.length ? '\n' + report.warnings.join('\n') : ''); status.hidden = false; }
    toast(message);
  } catch (e) {
    reportLevelDataError('CSV 导入失败：', e);
  }
}

function setFn(name        )       { const fn = $('[data-fn]'); if (fn) fn.textContent = name; }

// ---------- 撤销 ----------
function updateUndoButtons()       {
  ($('[data-undo]')                     ).toggleAttribute('disabled', !history.canUndo);
  ($('[data-redo]')                     ).toggleAttribute('disabled', !history.canRedo);
}
function doUndo()       { if (fillPreview) return; const p = history.undo(st.master); if (p) { st.master = p; clearSelection(); drawEdit(); drawPlay(); renderRightPanels(); updateUndoButtons(); scheduleDraftSave(); } }
function doRedo()       { if (fillPreview) return; const n = history.redo(st.master); if (n) { st.master = n; clearSelection(); drawEdit(); drawPlay(); renderRightPanels(); updateUndoButtons(); scheduleDraftSave(); } }

// ---------- 事件绑定 ----------
function bind()       {
  const cv = canvas('edit');
  cv.addEventListener('pointermove', onPointerMove);
  cv.addEventListener('pointerdown', onPointerDown);
  cv.addEventListener('pointerup', onPointerUp);
  cv.addEventListener('pointerleave', () => { st.hover = null; drawEdit(); });
  cv.addEventListener('contextmenu', e => e.preventDefault());
  cv.addEventListener('wheel', e => {
    e.preventDefault();
    const rect = cv.getBoundingClientRect();
    zoom(st.editView, e.deltaY < 0 ? 1.1 : 1 / 1.1, e.clientX - rect.left, e.clientY - rect.top);
    zoomLabel(); drawEdit();
  }, { passive: false });
  // 空格拖动平移
  cv.addEventListener('pointerdown', e => { if (panKey) { panning = true; panStart = { x: e.clientX, y: e.clientY, ox: st.editView.ox, oy: st.editView.oy }; } });
  window.addEventListener('pointermove', e => {
    if (!panning) return;
    st.editView.ox = panStart.ox + (e.clientX - panStart.x);
    st.editView.oy = panStart.oy + (e.clientY - panStart.y);
    drawEdit();
  });
  window.addEventListener('pointerup', () => { panning = false; });

  // 试玩画布：点 tile 进背包 + 缩放/平移
  const pcv = canvas('play');
  pcv.style.cursor = 'pointer';
  pcv.addEventListener('pointerdown', onPlayPointerDown);
  pcv.addEventListener('wheel', e => {
    e.preventDefault(); const rect = pcv.getBoundingClientRect();
    zoom(st.playView, e.deltaY < 0 ? 1.1 : 1 / 1.1, e.clientX - rect.left, e.clientY - rect.top);
    zoomLabel(); drawPlay();
  }, { passive: false });

  // 工具栏
  bindLocal();
  $('[data-undo]') .onclick = doUndo;
  $('[data-redo]') .onclick = doRedo;
  $('[data-export]') .onclick = () => { trackUsageAction('level_export'); exportBytes(); };
  $('[data-writerepo]') .onclick = () => { trackUsageAction('level_save'); void writeRepo(); };
  $('[data-templatemetasave]') .onclick = saveTemplateMetadata;
  for (const input of $$('[data-templatename], [data-templateauthor]')) input.addEventListener('input', markTemplateMetadataEdited);
  $('[data-tplid]') .addEventListener('input', renderTemplateMetadata);
  document.addEventListener('level-editor-user', renderTemplateMetadata);
  $('[data-drawertoggle]') .onclick = toggleLevelDrawer;
  $('[data-drawerclose]') .onclick = closeLevelDrawer;
  $('[data-duparatoggle]') .onclick = toggleDupDrawer;
  $('[data-duparaclose]') .onclick = closeDupDrawer;
  for (const button of $$('[data-duparacopy]')) (button               ).onclick = copyDupParams;
  $('[data-duparasave]') .onclick = saveDupConfig;
  $('[data-indexdrawertoggle]') .onclick = toggleIndexDrawer;
  $('[data-indexdrawerclose]') .onclick = closeIndexDrawer;
  const indexSearch = $('[data-indexdrawersearch]')                           ;
  if (indexSearch) indexSearch.oninput = () => { indexDrawerQuery = indexSearch.value; renderIndexDrawer(); };
  const drawerSearch = $('[data-drawersearch]')                           ;
  if (drawerSearch) drawerSearch.oninput = () => { drawerQuery = drawerSearch.value; renderLevelDrawer(); };
  $('[data-load]') .onclick = () => loadTemplate(($('[data-tplid]')                    ).value.trim());
  $('[data-new]') .onclick = () => {
    if (busy || !allowReplace()) return;
    ++readRequest; const id = '';
    st.dup = curveDefault(); st.strategyGroupId = ''; st.randomSeedGroupId = '';
    currentLevelId = id;
    st.master = newMaster(id); history.clear(); st.currentZ = 0; st.slot = 0; clearSelection();
    st.editView = fitView(st.master, cv.clientWidth, cv.clientHeight); st.playView = { ...st.editView };
    setFn('未命名模板'); adoptSource(''); renderAll(); zoomLabel();
    scheduleDraftSave();
  };
  $('[data-open]') .onclick = () => ($('[data-file]')                    ).click();
  ($('[data-file]')                    ).onchange = e => { const input = e.target                    ; const f = input.files?.[0]; input.value = ''; if (f) openFile(f); };
  $('[data-importcsv]') .onclick = () => {
    alert(IMPORT_CSV_PROMPT);
    ($('[data-csvfiles]')                    ).click();
  };
  ($('[data-csvfiles]')                    ).onchange = e => {
    const input = e.target                    ;
    const files = Array.from(input.files ?? []);
    input.value = '';
    if (files?.length) importLevelCsvFiles(files);
  };
  $('[data-clear]') .onclick = () => { if (st.master.tiles.length || st.master.towers.length) edit(m => ({ master: { ...m, tiles: [], towers: [], debuffs: [] } })); };

  // 层（层 −/z/+ 按钮已移除，换层改用键盘 [ ]；保留整体升降）
  $('[data-raise]') .onclick = () => edit(m => raiseAll(m));
  $('[data-lower]') .onclick = () => edit(m => lowerAll(m));

  $('[data-selmode]') .onclick = () => { st.selectMode = !st.selectMode; if (!st.selectMode) st.selection.clear(); renderSelMode(); drawEdit(); };

  // 批量平移
  const moves                                   = { l: [-1, 0], r: [1, 0], u: [0, -1], d: [0, 1] };
  for (const [k, [dx, dy]] of Object.entries(moves)) $(`[data-move="${k}"]`) .onclick = () => moveBoard(dx,dy);

  // 缩放按钮
  $('[data-zoom="in"]') .onclick = () => { zoom(st.editView, 1.15, cv.clientWidth / 2, cv.clientHeight / 2); zoomLabel(); drawEdit(); };
  $('[data-zoom="out"]') .onclick = () => { zoom(st.editView, 1 / 1.15, cv.clientWidth / 2, cv.clientHeight / 2); zoomLabel(); drawEdit(); };
  $('[data-fit]') .onclick = () => { st.editView = fitView(st.master, cv.clientWidth, cv.clientHeight); zoomLabel(); drawEdit(); };
  $('[data-zoom="pin"]') .onclick = () => { zoom(st.playView, 1.15, pcv.clientWidth / 2, pcv.clientHeight / 2); zoomLabel(); drawPlay(); };
  $('[data-zoom="pout"]') .onclick = () => { zoom(st.playView, 1 / 1.15, pcv.clientWidth / 2, pcv.clientHeight / 2); zoomLabel(); drawPlay(); };
  $('[data-pfit]') .onclick = () => { st.playView = fitPlayableBoard(derivePlayMaster(), pcv.clientWidth, pcv.clientHeight); zoomLabel(); drawPlay(); };

  // 副本档位
  for (const f of $$('[data-dup] input')) {
    const inp = f                    ;
    inp.onchange = () => commitDupField(inp.dataset.p                 , inp.value);
  }
  $('[data-slotdec]') .onclick = () => { if (st.slot > 0) { st.slot--; resetSimulation(); renderDupRow(); drawPlay(); renderRightPanels(); } };
  $('[data-slotinc]') .onclick = () => { if (st.slot < DUP_SLOTS - 1) { st.slot++; resetSimulation(); renderDupRow(); drawPlay(); renderRightPanels(); } };
  $('[data-reroll]') .onclick = () => { trackUsageAction('level_reroll'); restartPlay(); };
  for (const b of $$('[data-prop]')) (b               ).onclick = () => runProp((b               ).dataset.prop );
  $('[data-bagtoggle]') .onclick = () => setTray(st.tray >= 8 ? 7 : 8);
  renderBagToggle();
  ($('[data-levelnum]')                    ).onchange = e => {
    st.levelNum = Math.max(1, parseInt((e.target                    ).value, 10) || 1);
    restartPlay(); renderRightPanels();
    scheduleDraftSave();
  };
  // 策略组 / 种子组（整关共用）
  const sgInp = $('[data-strategygroup]')                           ;
  if (sgInp) sgInp.onchange = () => { st.strategyGroupId = normIntField(sgInp.value); sgInp.value = st.strategyGroupId; restartPlay(); renderRightPanels(); scheduleDraftSave(); };
  const rgInp = $('[data-seedgroup]')                           ;
  if (rgInp) rgInp.onchange = () => { st.randomSeedGroupId = normIntField(rgInp.value); rgInp.value = st.randomSeedGroupId; restartPlay(); renderRightPanels(); scheduleDraftSave(); };
  ($('[data-tray]')                    ).onchange = e => setTray(parseInt((e.target                    ).value, 10) || 7);
  $('[data-sim]') .onclick = () => { trackUsageAction('level_simulate'); runSim(); };

  // 试玩最小化：收起右半、编辑器变大；再点标题栏任意处展开
  const setPlayMin = (min         )       => {
    const grid = $('[data-editgrid]'); if (!grid) return;
    grid.classList.toggle('play-min', min);
    const btn = $('[data-playmin]');
    if (btn) { btn.textContent = min ? '⊞' : '⊟'; btn.title = min ? '展开试玩' : '最小化试玩'; }
    requestAnimationFrame(() => draw());
  };
  const playMinBtn = null;
  if (playMinBtn) playMinBtn.onclick = e => { e.stopPropagation(); setPlayMin(!$('[data-editgrid]')?.classList.contains('play-min')); };
  const playHead = $('.playpanel .hh');
  if (playHead) playHead.addEventListener('click', () => { if ($('[data-editgrid]')?.classList.contains('play-min')) setPlayMin(false); });

  // 键盘
  window.addEventListener('keydown', e => {
    if (fillPreview || e.target instanceof HTMLInputElement) return;
    if (!e.metaKey && !e.ctrlKey && !e.altKey) {
      // WASD 批量平移
      const mv                                   = { w: [0, -2], s: [0, 2], a: [-2, 0], d: [2, 0] };
      const m = mv[e.key.toLowerCase()];
      if (m) { e.preventDefault(); edit(mm => translateAll(mm, m[0], m[1])); return; }
      // 显示层：Q 减 / E 加（全部模式下自动切多层）
      const k = e.key.toLowerCase();
      if (k === 'e') { e.preventDefault(); st.showZ++; if (st.showMode === 'all') st.showMode = 'multi'; renderLayer(); drawEdit(); return; }
      if (k === 'q') { e.preventDefault(); if (st.showZ > 0) st.showZ--; if (st.showMode === 'all') st.showMode = 'multi'; renderLayer(); drawEdit(); return; }
    }
    if (e.code === 'Space') { panKey = true; cv.style.cursor = 'grab'; e.preventDefault(); }
    else if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'z') { e.preventDefault(); e.shiftKey ? doRedo() : doUndo(); }
    else if (e.key === '[') { if (st.currentZ > 0) { st.currentZ--; renderLayer(); drawEdit(); } }
    else if (e.key === ']') { st.currentZ++; renderLayer(); drawEdit(); }
  });
  window.addEventListener('keyup', e => { if (e.code === 'Space') { panKey = false; cv.style.cursor = 'crosshair'; } });

  window.addEventListener('resize', () => { renderDrawerLayout(); if (!($('.playpanel')               ).hidden) $('[data-pfit]') .dispatchEvent(new Event('click')); else drawEdit(); });
  window.addEventListener('beforeunload', flushPendingDraftSave);
}

function boardProblems() {
  const board = { tiles: st.master.tiles.map((t,i)=>({index:i,x:t.x,y:t.y,z:t.z,tile_id:t.tileId})),
    towers: st.master.towers.map((t,i)=>({...t,index:i})) };
  const cc = coverEditor();
  const cover = cc.issueIndices(board);
  return {cover:board.tiles.filter(t=>cover.has(t.index)), floating:cc.unsupportedTiles(board)};
}
function moveBoard(dx       ,dy       ) {
  translateX += dx; translateY += dy;
  edit(m=>translateAll(m,dx,dy));
}
function bindLocal() {
  bindBatchTools({
    read: async (id        ) => {
      const response = await fetch(TEMPLATE_URL(id) + '?preview=1');
      if (!response.ok) await readLocal(response);
      return new Uint8Array(await response.arrayBuffer());
    },
    upload: (raw            ) => writeLocal('/api/upload?preview=1', raw),
    config: async () => {
      await ensureLevelDataLoaded();
      return drawerConfig?.rows ?? [];
    },
    levelNumber: (id        ) => indexDrawer.rows.find(row => row.levelId === id)?.levelIndex
      ?? (Number(id) || 1),
    capture: captureTemplate,
    openScreenshotFolder: () => writeLocal('/api/template-screenshot/open-folder'),
  });
  bindCoverBoard();
  document.addEventListener('keydown', event => {
    const target = event.target           ;
    if (fillPreview || event.isComposing || event.ctrlKey || event.metaKey || event.altKey ||
        target?.closest('input,textarea,select,[contenteditable],dialog') ||
        ($('[data-editpanel]')               ).hidden) return;
    const shortcut = event.key.toLowerCase();
    const button = [...document.querySelectorAll                   ('[data-palette-shortcut]')]
      .find(tool => tool.dataset.paletteShortcut?.toLowerCase() === shortcut);
    if (!button) return;
    event.preventDefault();
    event.stopPropagation();
    button.click();
  }, true);
  const maxLayer = $('[data-showzlabel]')                    ;
  maxLayer.addEventListener('change', () => {
    const value = maxLayer.value.trim();
    const number = value === '无限制' || !value ? 0 : Number(value);
    if (!Number.isSafeInteger(number) || number < 0) {
      localStatus('MaxZ 请输入非负整数，0 表示无限制。', true);
      renderLayer();
      return;
    }
    setMaxZ(number);
  });
  maxLayer.addEventListener('keydown', event => {
    if (event.key === 'Enter' && !event.isComposing) {
      event.preventDefault();
      maxLayer.blur();
    }
  });
  $('[data-showz="up"]') .addEventListener('click', () => setMaxZ(displayedMaxZ() + 1));
  $('[data-showz="dn"]') .addEventListener('click', () => setMaxZ(displayedMaxZ() - 1));
  $('[data-maxz-reset]') .addEventListener('click', () => setMaxZ(0));
  window.addEventListener('keydown', e=>{
    if(e.key.toLowerCase()!=='z'||e.ctrlKey||e.metaKey||e.altKey||e.isComposing||(e.target           )?.closest('input,textarea,select,[contenteditable]'))return;
    if(fillPreview || ($('[data-editpanel]')               ).hidden)return;
    e.preventDefault(); if(e.shiftKey) doRedo(); else doUndo();
  });
  const id = $('[data-tplid]')                    ;
  id.addEventListener('keydown', e=>{ if(e.key==='Enter' && !e.isComposing) {e.preventDefault(); ($('[data-load]')                     ).click();} });
  id.addEventListener('input',()=>{
    id.removeAttribute('aria-invalid');
    invalidateSaved();
  });
  window.addEventListener('beforeunload',e=>{if(hasEdits() || batchEntries.some((entry, index) => index !== batchActive && entry.context && entry.context.signature !== JSON.stringify(entry.context.state.master)) ||
    (batchPhase !== 'single' && singleContext && singleContext.signature !== JSON.stringify(singleContext.state.master))){e.preventDefault();}});
  $('[data-mode-play]') .addEventListener('click', () => setPlayMode(($('.playpanel')               ).hidden));
  bindBatch();
  bindManual();
  $('[data-reset-move]') .addEventListener('click',()=>{if(translateX||translateY) moveBoard(-translateX,-translateY);});
  for (const button of $$('[data-rotate]')) button.addEventListener('click',()=>{
    const clockwise=(button               ).dataset.rotate==='cw';
    const transform = (t                    )=>clockwise?{x:17-t.y,y:t.x+1}:{x:t.y-1,y:17-t.x};
    [translateX,translateY] = clockwise?[-translateY,translateX]:[translateY,-translateX];
    edit(m=>({...m, tiles:m.tiles.map(t=>({...t,...transform(t)})),
      towers:m.towers.map(t=>({...t,...transform(t),direction:(t.direction+(clockwise?3:1))%4}))}));
  });
  $('[data-restore]') .addEventListener('click',()=>{
    if(initialBoard && hasEdits() && confirm('恢复到刚载入的模板？可以撤销本次恢复。')) {
      translateX=translateY=0; edit(()=>structuredClone(initialBoard ));
    }
  });
  for (const button of $$('[data-clear-layers]')) button.addEventListener('click',()=>{
    if(st.showMode==='all') {localStatus('请先输入 MaxZ，选择要操作的层。',true); return;}
    const above = (button               ).dataset.clearLayers==='above';
    const keep = (t       )=>above?t.z<=st.showZ:t.z!==st.showZ;
    const tiles = st.master.tiles.filter(keep), n = st.master.tiles.length-tiles.length;
    if(n && !above) {
      const dialog=$('[data-clear-layer-dialog]')                     ;
      const z=st.showZ;
      dialog.querySelector('[data-clear-count]') .textContent='当前层共 '+n+' 张。删除整层会将上方牌下移一层。';
      dialog.querySelectorAll('button[data-delete-choice]').forEach(el=>{(el                     ).onclick=()=>{
        dialog.close(); const closeGap=(el               ).dataset.deleteChoice==='layer';
        edit(m=>({...m,tiles:m.tiles.filter(t=>t.z!==z).map(t=>closeGap&&t.z>z?{...t,z:t.z-1}:t)}));
        if(closeGap) setMaxZ(z);
      };});
      dialog.showModal();
    } else if(n && confirm('删除 '+n+' 张牌？可以撤销。')) edit(m=>({...m,tiles}));
    else if(!n) localStatus('所选范围没有牌。');
  });
  for(const button of $$('[data-coordinate]')) button.addEventListener('click',()=>{
    const fields = ['x','y','z'].map(k=>($('[data-coord-'+k+']')                    ).value);
    if(fields.some(v=>v===''||!Number.isSafeInteger(Number(v)))) {localStatus('请填写整数坐标。',true);return;}
    const [x,y,z]=fields.map(Number);
    if(!inRange(x,y)||z<0||z>99) {localStatus('坐标范围为 X 0–16、Y 0–18、Z 0–99。',true);return;}
    if((button               ).dataset.coordinate==='remove') edit(m=>({...m,tiles:m.tiles.filter(t=>t.x!==x||t.y!==y||t.z!==z)}));
    else if(st.brush.kind==='tile'||st.brush.kind==='random') edit(m=>placeTile(m,x,y,z,st.brush.value));
    else localStatus('请选择一个 Tile 笔刷。',true);
  });
  const previewButton = $('[data-fill-preview]')                     ;
  previewButton.addEventListener('click', () => {
    if (fillPreview) {
      endFillPreview();
      drawEdit();
      localStatus('已退出预览，返回编辑。');
      return;
    }
    if (busy || !st.master.tiles.length) {
      localStatus('请先载入模板或摆牌。', true);
      return;
    }
    const seed = curDup().seed;
    fillPreview = { source: st.master, board: derivePlayMaster(seed), maxZ: displayedMaxZ() };
    const seedInput = $('[data-preview-seed]')                    ;
    seedInput.value = String(seed);
    previewButton.textContent = '退出预览';
    previewButton.setAttribute('aria-pressed', 'true');
    setPreviewControls(true);
    localStatus('预览第 ' + (st.slot + 1) + ' 次玩的牌面。可调整随机种子和显示层。');
    drawEdit();
    seedInput.focus();
  });
  const seedInput = $('[data-preview-seed]')                    ;
  seedInput.addEventListener('input', () => {
    if (!fillPreview) return;
    const seed = seedInput.valueAsNumber;
    if (!Number.isInteger(seed) || !seedInput.validity.valid) {
      seedInput.setAttribute('aria-invalid', 'true');
      $('[data-preview-seed-status]') .textContent = '请输入有效的 32 位整数种子。';
      return;
    }
    seedInput.removeAttribute('aria-invalid');
    $('[data-preview-seed-status]') .textContent = '';
    fillPreview.board = derivePlayMaster(seed);
    paintCoverBoard();
  });
  document.addEventListener('keydown', event => {
    if (fillPreview && event.key === 'Escape' && !document.querySelector('dialog[open]')) {
      event.preventDefault();
      previewButton.click();
      previewButton.focus();
    }
  });
  ($('[data-pool-file]')                    ).addEventListener('change',async e=>{
    const field=e.target                    , file=field.files?.[0]; field.value='';
    if(!file)return;
    try {
      if(file.size>2*1024*1024) throw Error('文件不能超过 2 MB。');
      const rows=parseLevelTilePools(await file.text());
      if(!rows.length) throw Error('没有读到类型池分段，请选择 config_client_level_tile.csv。');
      levelPools=rows; restartPlay(); renderRightPanels();
      localStatus('已应用实验组类型池：'+file.name+'（'+rows.length+' 个分段）。');
    } catch(e) {localStatus((e         ).message,true);}
  });
  let problemPosition=0;
  for(const button of $$('[data-locate-cover],[data-locate-float]')) button.addEventListener('click',()=>{
    const list=button.hasAttribute('data-locate-cover')?boardProblems().cover:boardProblems().floating;
    if(!list.length)return;
    const p=list[problemPosition++%list.length] ;
    st.showMode='multi';st.showZ=p.z; issueSpot=p.x+','+p.y+','+p.z;
    for(const k of ['x','y','z']         ) ($('[data-coord-'+k+']')                    ).value=String(p[k]);
    renderLayer();drawEdit(); localStatus('已定位：X '+p.x+'，Y '+p.y+'，Z '+p.z+'。');
  });
  const clear = $('[data-clear]') ;
  clear.addEventListener('click',e=>{if(st.master.tiles.length&&!confirm('清空模板？可以撤销。'))e.stopImmediatePropagation();},true);
  const seedMode=$('[data-seed-mode]')                     ;
  seedMode.addEventListener('change',()=>{groupRules=seedMode.value==='group'; restartPlay();renderRightPanels();});
  ($('[data-iap-cut]')                    ).addEventListener('change',e=>{iapCut=Math.max(0,Number((e.target                    ).value)||0);restartPlay();});
  $('[data-apply-ab]') .addEventListener('click',()=>{
    const input=$('[data-effective-dup]')                    ;
    const text=input.value.trim();
    if(!/^\d+\|-?\d+\|\d+\|\d+$/.test(text)) {localStatus('粘贴生效 DupPara：A种类数|随机种子|塔幸运值|B种类数。',true);return;}
    const [a,seed,lucky,b]=text.split('|').map(Number);
    if([a,seed,lucky,b].some(v=>!Number.isSafeInteger(v)||Math.abs(v)>2147483647)||a>100||b>100||lucky>100) {localStatus('请核对数值：A/B 种类数和塔幸运值为 0–100，随机种子为 32 位有符号整数。',true);return;}
    st.dup[st.slot]={a,seed,lucky,b}; renderDupRow();renderDupDrawer();restartPlay();renderRightPanels();scheduleDraftSave();
    localStatus('已将实验组生效参数应用到第 '+(st.slot+1)+' 档。');
  });
  $('[data-copy-board]') .addEventListener('click',async()=>{
    const r=derivePlayMaster();
    const data={templateId:st.master.templateId,level:st.levelNum,dupSlot:st.slot+1,dupPara:curDup(),iapCut,rule:groupRules?'seedGroup':'unityDupPara',seedGroup:groupRules?st.randomSeedGroupId:null,strategyGroup:groupRules?st.strategyGroupId:null,tiles:r.tiles.map(t=>[t.x,t.y,t.z,t.tileId])};
    try{await navigator.clipboard.writeText(JSON.stringify(data,null,2));localStatus('已复制本次试玩参数和牌面。');}
    catch{localStatus('无法复制，请检查浏览器剪贴板权限。',true);}
  });
  bindRepositorySettings();
  for(const d of $$('dialog')) {d.querySelector('button[data-dismiss]')?.addEventListener('click',()=> (d                     ).close());}
}



async function captureTemplate(job            , patterned         , name        ) {
  const board = patterned ? derivePlayMaster(undefined, job) : job.master;
  const names = patterned ? new Set(board.tiles.map(tile => iconOf(tile.tileId))) : new Set        ();
  for (const icon of names) ensureIcon(icon);
  const images = [...names].flatMap(name => name && icons.has(name) ? [icons.get(name) ] : []);
  await Promise.allSettled([tileBg, ...images, ...(patterned ? debuffIcons.values() : [])].map(image => image.decode()));
  const surface = document.createElement('canvas');
  surface.width = 1200;
  surface.height = 1350;
  render(surface.getContext('2d') , surface.width, surface.height, {
    master: board, view: patterned ? fitPlayableBoard(board, surface.width, surface.height)
      : {s:64, ox:surface.width / 2 - 9 * 64, oy:surface.height / 2 - 10 * 64},
    currentZ: 9999, templateScreenshot: !patterned,
    upper: 'fade', showMaxZ: null, cover: coverFlags(board), colorOf,
    icons: patterned ? icons : undefined, iconOf: patterned ? iconOf : undefined,
    labelOf: patterned ? editorTileLabel : id => editorTileLabel(id) + (id === -10 || id === -11 ? '?' : ''),
    hover: null, showRange: false, tileBg,
    debuffIcons: patterned ? debuffIcons : undefined,
    tileRect: playRectOf, cropTileBgSides: true, iconOffsetY: -14,
  });
  const png = await new Promise             (resolve => surface.toBlob(resolve, 'image/png'));
  if (!png) throw new Error('浏览器未能生成 PNG。');
  return writeLocal('/api/template-screenshot?' + new URLSearchParams({name}), new Uint8Array(await png.arrayBuffer()));
}

function bindRepositorySettings() {
  const dialog = $                   ('[data-settings-dialog]') ;
  const form = $                 ('[data-repository-form]') ;
  const path = $                  ('#repository-path') ;
  const output = $                  ('#output-path') ;
  const game = document.querySelector('#game-path');
  const pickGame = document.querySelector('[data-game-pick]');
  const status = $('[data-repository-status]') ;
  const pick = $                   ('[data-repository-pick]') ;
  const pickOutput = $                   ('[data-output-pick]') ;
  const save = $                   ('[data-repository-save]') ;
  const message = (text        , error = false) => {
    status.textContent = text;
    status.classList.toggle('is-error', error);
  };
  const setPending = (pending         ) => {
    path.readOnly = output.readOnly = game.readOnly = pending;
    pick.disabled = pickOutput.disabled = pickGame.disabled = save.disabled = pending;
  };
  async function loadSettings() {
    setPending(true);
    try {
      const result = await readLocal(await fetch('/api/status'));
      path.value = result.repository;
      output.value = result.output;
      game.value = result.game || '';
      message(result.ready ? '' : '未找到模板仓库，请输入路径或选取目录。', !result.ready);
    } catch {
      message('无法读取本地设置，请重新打开启动器。', true);
    } finally { setPending(false); }
  }
  $('[data-settings]') .addEventListener('click', () => {
    if (!dialog.open) dialog.showModal();
    if (!save.disabled) void loadSettings();
  });
  async function pickDirectory(target                         , input                  ) {
    setPending(true);
    message('请在系统窗口里选取目录。');
    try {
      const result = await writeLocal('/api/repository/pick?target=' + target);
      if (result[target]) input.value = result[target];
      message(result[target] ? '点击「保存路径」应用所选目录。' : '');
    } catch (error) { message((error         ).message, true); }
    finally { setPending(false); }
  }
  pick.addEventListener('click', () => void pickDirectory('repository', path));
  pickGame.addEventListener('click', () => void pickDirectory('game', game));
  pickOutput.addEventListener('click', () => void pickDirectory('output', output));
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (save.disabled) return;
    setPending(true);
    try {
      const response = await fetch('/api/repository', {
        method: 'POST', headers: {...localHeaders, 'Content-Type': 'application/json'},
        body: JSON.stringify({repository: path.value, output: output.value, game: game.value}),
      });
      const result = await readLocal(response);
      path.value = result.repository;
      output.value = result.output;
      if (result.outputChanged) {
        savedName = '';
        if (singleContext) singleContext.savedName = '';
        for (const entry of batchEntries) if (entry.context) entry.context.savedName = '';
        invalidateSaved();
      }
      await loadTemplateList();
      message('路径已保存。');
    } catch (error) { message((error         ).message, true); }
    finally { setPending(false); }
  });
}

let boardPointer = { x:2, y:2 }, shownCursor = false;
let issueSpot = '';
const coverGrid = (() => {
  const points = [];
  for(let y=0;y<19;y++) for(let x=0;x<17;x++)
    points.push('<circle cx="'+(x+1)+'" cy="'+(y+1)+'" r=".035" class="cc-dot-'+(x%2===y%2?'red':'blue')+'"/>');
  return '<svg class="cc-grid" viewBox="0 0 18 20" aria-hidden="true"><rect x="2.5" y="2.5" width="13" height="15" class="cc-frame"/><path d="M9 0V20M0 10H18" class="cc-axis"/>'+points.join('')+'</svg>';
})();
function coversForEditor(master = st.master) {
  const board = { tiles:master.tiles.map((t,i)=>({index:i,x:t.x,y:t.y,z:t.z,tile_id:t.tileId,
    tower_index:master.towers.findIndex(tw=>tw.x===t.x&&tw.y===t.y)})), towers:master.towers.map((t,i)=>({...t,index:i})) };
  return board;
}
function paintCoverBoard() {
  const master = fillPreview?.board ?? st.master;
  const active = $('[data-cover-board]') ;
  $('[data-cover-board]') .classList.toggle('is-fill-preview', Boolean(fillPreview));
  active.innerHTML = coverMarkup(master, displayedMaxZ(), true, Boolean(fillPreview));
  coverCursor(false);
}

const tileNames = new Map                ();
const COIN_BLOCKER        = { kind: 'tile', value: 10000, label: '金币障碍牌', color: '#f1c531' };

function editorTileLabel(id        )         {
  if (isRandomMarker(id)) return tileDisplayLabel(id);
  if (isVariantType(id)) return '随' + (variantKey(id) + 1);
  return tileNames.get(id) ?? '未知牌';
}

function escapeTileLabel(label        )         {
  return label.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');
}

function coverMarkup(master        , maxZ = 0, interactive = false, preview = false) {
  const cc = coverEditor(), board = coversForEditor(master);
  const tiles=cc.visibleTiles(board,maxZ,false);
  const issues=cc.issueIndices(board);
  const floating=new Set(cc.unsupportedTiles(board).map((t    )=>t.index));
  return coverGrid+tiles.map((t    )=>{
    const position=cc.projection(board,t), anchor=cc.anchor(position);
    const covered=tiles.some((other    )=>other.z>t.z&&cc.overlap(cc.projection(board,other),position)>0);
    const id=t.tile_id, css=['cc-tile',covered?'is-covered':'',maxZ > 0 && t.z + 1 < maxZ?'is-context':'',issues.has(t.index)?'is-issue':'',floating.has(t.index)?'is-unsupported':'',
       interactive && ((st.selectMode&&st.selection.has(t.x+','+t.y+','+t.z))||issueSpot===t.x+','+t.y+','+t.z)?'is-located':''].join(' ');
    const label = escapeTileLabel(editorTileLabel(id));
    const detail=label+' · 第 '+(t.z+1)+' 层 · ('+t.x+', '+t.y+', '+t.z+')'+(t.tower_index>=0?' · 塔内':'')+(floating.has(t.index)?' · 悬空':'');
    const debuff=master.tiles[t.index]?.debuffIndex;
    const icon = iconOf(id);
    const tag = preview ? 'span' : 'button';
    const attributes = preview ? 'role="img"' : 'type="button" tabindex="-1"';
    const face = (icon ? '<img class="preview-tile-icon" src="' + ICON_BASE + encodeURIComponent(icon) + '.png" alt="">' : '')
      + '<span class="cc-tile-label"' + (icon ? ' hidden' : '') + '>' + label + '</span>';
    return '<'+tag+' '+attributes+' class="'+css+'" data-board-index="'+t.index+'" data-tile-id="'+id+'" style="left:'+anchor.left+'%;top:'+anchor.top+'%" title="'+detail+'" aria-label="'+detail+'"><span class="cc-tile-layer">'+(t.z+1)+'</span>'+face+(typeof debuff==='number'&&debuff>=0?'<span class="board-debuff">特殊</span>':'')+'</'+tag+'>';
  }).join('')+board.towers.filter(t=>!board.tiles.some(p=>p.x===t.x&&p.y===t.y)).map(t=>{
    const a=cc.anchor(t);return '<span class="board-tower" style="left:'+a.left+'%;top:'+a.top+'%" title="塔底座">'+(['↑','←','↓','→'][t.direction]||'塔')+'</span>';
  }).join('')+(tiles.length?'':'<p class="cc-board-empty">'+(board.tiles.length?'这一层没有牌':'输入模板 ID 读取，或在画布上摆牌')+'</p>')+'<div class="cc-tile-cursor" hidden aria-hidden="true"></div>';
}
function coverCursor(erase        ) {
  const cursor=$('[data-cover-board] .cc-tile-cursor')               ;
  if(!cursor)return;
  const cc=coverEditor(), board=coversForEditor(), {x,y}=boardPointer;
  const position=cc.cursorPlacement(board,erase?'delete':'add',x,y,st.showMode==='all'?0:st.showZ+1);
  const anchor=cc.anchor(position);
  cursor.style.left=anchor.left+'%'; cursor.style.top=anchor.top+'%';
  cursor.hidden=Boolean(fillPreview)||!shownCursor||!inRange(x,y)||(erase&&position.z==null);
  cursor.classList.toggle('is-delete',erase);
  const brush = st.brush;
  const brushLabel = brush?.kind === 'tile' || brush?.kind === 'random'
    ? editorTileLabel(brush.value) : brush?.label || '摆牌';
  cursor.dataset.label=(erase?'删除':activeTowerMove!==null?'移动塔':brushLabel)+(position.z==null?'':' · 第 '+(position.z+1)+' 层');
}
function bindCoverBoard() {
  const el=$('[data-cover-board]')               , cc=coverEditor();
  document.addEventListener('error', event => {
    const image = event.target;
    if (!(image instanceof HTMLImageElement) || !image.matches('.cc-tile > .preview-tile-icon')) return;
    image.hidden = true;
    image.nextElementSibling?.removeAttribute('hidden');
  }, true);
  let dragging=false, changed=false, start            =null, last='';
  function action(erase        ,index        ) {
    if (fillPreview) {
      localStatus('点击「返回母版」继续摆牌。');
      return false;
    }
    const {x,y}=boardPointer, m=st.master;
    if(!inRange(x,y)||!st.brush)return false;
    const brush=st.brush, board=coversForEditor(), layer=st.showMode==='all'?0:st.showZ+1;
    const tile=cc.target(board,'delete',x,y,layer,index);
    if (activeTowerMove !== null && !erase) {
      const anchor = tile?.tower_index >= 0 ? board.towers[tile.tower_index] : { x, y };
      const tower = m.towers.find(item => item.x === anchor.x && item.y === anchor.y);
      if (!tower) return false;
      const [dx, dy] = TOWER_MOVE_TOOLS[activeTowerMove].delta;
      const nextX = tower.x + dx;
      const nextY = tower.y + dy;
      if (!inRange(nextX, nextY) || m.towers.some(item => item.x === nextX && item.y === nextY)) return false;
      st.master = {
        ...m,
        towers: m.towers.map(item => item === tower ? { ...item, x: nextX, y: nextY } : item),
        tiles: m.tiles.map(item => item.x === tower.x && item.y === tower.y
          ? { ...item, x: nextX, y: nextY } : item),
      };
      renderAfterEdit(false);
      drawEdit();
      return true;
    }
    if(st.selectMode&&!erase) {
      if(tile){const k=tileKey(m.tiles[tile.index]); if(st.selection.has(k))st.selection.delete(k);else st.selection.add(k);}
      else st.selection.clear();
      renderSelHint();drawEdit();return false;
    }
    let result;
    if(erase||brush.kind==='erase'){
      if(tile && m.tiles[tile.index] .debuffIndex>=0) result=removeDebuffAt(m,tile.z,tile.x,tile.y);
      else if(tile) result={master:{...m,tiles:m.tiles.filter((_,i)=>i!==tile.index)},changed:true};
      else result=removeTowerAt(m,x,y);
    } else if(brush.kind==='tower') result=placeTower(m,x,y,brush.value);
    else if(brush.kind==='debuff') {
      if(tile) result=addDebuffAt(m,tile.x,tile.y,brush.value);
      else return false;
    } else {
      // Tile hit-testing is for selecting and erasing. Placement follows the grid cursor.
      const p=cc.target(board,'add',x,y,layer);
      result=placeTile(m,p.x,p.y,p.z,brush.value);
    }
    if(result.changed){st.master=result.master;renderAfterEdit(false);drawEdit();return true;}
    return false;
  }
  function finish() {
    if(!dragging)return;
    dragging=false;
    if(changed&&start){history.push(start);updateUndoButtons();scheduleDraftSave();}
    start=null;changed=false;last='';
  }
  function pointer(e             ) {
    boardPointer=cc.point(e.clientX,e.clientY,el.getBoundingClientRect());
    shownCursor=true;
  }
  el.addEventListener('pointerdown',e=>{
    if(fillPreview || (e.button!==0&&e.button!==2))return;
    e.preventDefault();el.focus({preventScroll:true});pointer(e);
    const index=(e.target           ).closest('[data-board-index]')?.getAttribute('data-board-index');
    start=st.master;dragging=true;last=boardPointer.x+':'+boardPointer.y;
    changed=action(e.button===2,index==null?undefined:Number(index));
    coverCursor(e.button===2);
  });
  el.addEventListener('pointermove',e=>{
    pointer(e);
    if(dragging&&e.buttons&&activeTowerMove===null) {
      const cell=boardPointer.x+':'+boardPointer.y;
      if(last!==cell){last=cell;changed=action((e.buttons&2)!==0)||changed;}
    }
    coverCursor((e.buttons&2)!==0);
  });
  el.addEventListener('contextmenu',e=>e.preventDefault());
  el.addEventListener('pointerleave',()=>{shownCursor=false;coverCursor(false);});
  window.addEventListener('pointerup',finish);window.addEventListener('pointercancel',finish);
  el.addEventListener('keydown',e=>{
    if (fillPreview) return;
    const delta                        ={ArrowLeft:[-1,0],ArrowRight:[1,0],ArrowUp:[0,-1],ArrowDown:[0,1]};
    if(e.altKey && delta[e.key]) {e.preventDefault();boardPointer={x:Math.max(0,Math.min(16,boardPointer.x+delta[e.key][0] )),y:Math.max(0,Math.min(18,boardPointer.y+delta[e.key][1] ))};shownCursor=true;coverCursor(false);}
    else if(e.key==='Enter'||e.key==='Delete'||e.key===' ') {e.preventDefault();e.stopPropagation();start=st.master;dragging=true;changed=action(e.key==='Delete');finish();}
  });
}

const TOWER_MOVE_TOOLS = [
  { key: 'W', label: '向上移动塔', delta: [0, -1] },
  { key: 'A', label: '向左移动塔', delta: [-1, 0] },
  { key: 'S', label: '向下移动塔', delta: [0, 1] },
  { key: 'D', label: '向右移动塔', delta: [1, 0] },
]         ;
let activeTowerMove                = null;
let normalPaletteExpanded = false;

let fillPreview                                                         = null;

function setPreviewControls(preview         ) {
  document.body.classList.toggle('is-previewing', preview);
  ($('[data-preview-options]')               ).hidden = !preview;
  // Keep the editing controls out of the tab order and prevent accidental clicks.
  const selectors = [
    '.workspace-topbar .work-row', '.batch-toolbar', '[data-mode-play]', '[data-settings]',
    '.inspector-action-row', '.inspector-controls > .edit-tools', '.unity-tool-heading',
    '.unity-palette', '.editor-play-settings', '.drawerdock', '.batch-card header',
    '.batch-board-placeholder',
  ];
  for (const el of $$(selectors.join(','))) (el               ).inert = preview;
}

function endFillPreview() {
  if (fillPreview) setPreviewControls(false);
  fillPreview = null;
  ($('[data-preview-seed]')                    ).removeAttribute('aria-invalid');
  $('[data-preview-seed-status]') .textContent = '';
  const button = $('[data-fill-preview]') ;
  button.textContent = '预览';
  button.setAttribute('aria-pressed', 'false');
}

function showPaletteCount(element         , total        ) {
  const missing = (3 - total % 3) % 3;
  element.textContent = total + ' (' + missing + ')';
  element.classList.toggle('is-incomplete', missing > 0);
}

function renderUnityPalette() {
  const paletteRoot = $('[data-brush]') ;
  const counts = new Map                ();
  for (const tile of st.master.tiles) {
    counts.set(tile.tileId, (counts.get(tile.tileId) ?? 0) + 1);
  }
  const focused = (document.activeElement                      )?.dataset.paletteKey;
  paletteRoot.replaceChildren();

  function addSection(title        , brushes         , normal = false)                             {
    if (!brushes.length) return;
    const section = document.createElement('section');
    section.className = 'unity-palette-section';
    const heading = document.createElement('h4');
    heading.textContent = title;
    const sectionHeader = document.createElement('div');
    sectionHeader.className = 'palette-section-header';
    sectionHeader.append(heading);
    let expandButton                               ;
    if (normal && brushes.length > 8) {
      expandButton = document.createElement('button');
      expandButton.type = 'button';
      expandButton.className = 'palette-expand';
      expandButton.setAttribute('aria-controls', 'normal-palette');
      sectionHeader.append(expandButton);
    }
    const grid = document.createElement('div');
    grid.className = normal ? 'unity-palette-grid unity-normal-grid' : 'unity-palette-grid';
    section.append(sectionHeader, grid);
    paletteRoot.append(section);

    for (const brush of brushes) {
      const button = document.createElement('button');
      const matchesBrush = sameBrush(brush, st.brush);
      const shortcuts = ['Q', 'E', '1', '2', '3', '4', '5', '6', '7', '8', '9', '0', 'M', 'N'];
      const shortcut = brush.kind === 'random' ? shortcuts[-10 - brush.value]
        : brush.kind === 'erase' ? 'X'
        : brush.kind === 'tower' ? ['ArrowUp', 'ArrowLeft', 'ArrowDown', 'ArrowRight'][brush.value]
        : '';
      const name = brush.kind === 'erase' ? '橡皮擦'
        : brush.kind === 'random' ? '随机标记 ' + editorTileLabel(brush.value)
        : brush.kind === 'tile' ? editorTileLabel(brush.value) : brush.des || brush.label;
      button.type = 'button';
      button.className = 'unity-tool';
      button.dataset.paletteKey = brush.kind + ':' + brush.value;
      button.setAttribute('aria-pressed', String(activeTowerMove === null && matchesBrush));
      button.setAttribute('aria-label', shortcut ? name + '，快捷键 ' + shortcut : name);
      button.title = button.getAttribute('aria-label') ;
      if (brush.kind === 'tile' || brush.kind === 'random') {
        const count = document.createElement('span');
        count.className = 'unity-tool-count';
        showPaletteCount(count, counts.get(brush.value) ?? 0);
        button.append(count);
      }

      const face = document.createElement('span');
      face.className = 'unity-tool-face';
      const faceLabel = brush.kind === 'random'
        ? brush.label + (brush.value === -10 || brush.value === -11 ? '?' : '')
        : name;
      const tileIcon = brush.kind === 'tile' ? iconOf(brush.value) : undefined;
      const asset = brush.kind === 'erase' ? '/assets/unity/Editor_Delete.png'
        : brush.kind === 'tower' ? '/assets/unity/Direction' + brush.value + '.png'
        : brush.iconSrc || (tileIcon ? ICON_BASE + encodeURIComponent(tileIcon) + '.png' : '');
      if (asset) {
        const image = document.createElement('img');
        image.alt = '';
        image.src = asset;
        image.addEventListener('error', () => { face.textContent = faceLabel; });
        face.append(image);
      } else {
        face.textContent = faceLabel;
        if (brush.value === -10) face.classList.add('random-a');
        if (brush.value === -11) face.classList.add('random-b');
      }
      button.append(face);

      if (shortcut) {
        const key = document.createElement('kbd');
        button.dataset.paletteShortcut = shortcut;
        key.textContent = shortcut.startsWith('Arrow') ? ['↑', '←', '↓', '→'][brush.value] : shortcut;
        button.append(key);
      }
      button.addEventListener('click', () => {
        if (fillPreview) return;
        activeTowerMove = null;
        st.brush = brush;
        if (st.selectMode && st.selection.size && (brush.kind === 'tile' || brush.kind === 'random')) {
          replaceSelected(brush.value);
        }
        updateUnityPalette();
        coverCursor(false);
      });
      grid.append(button);
    }
    if (expandButton) {
      const toggle = expandButton;
      grid.id = 'normal-palette';
      const syncExpanded = () => {
        toggle.textContent = normalPaletteExpanded ? '收起' : '展开';
        toggle.setAttribute('aria-expanded', String(normalPaletteExpanded));
        [...grid.children].forEach((button, index) => {
          (button               ).hidden = !normalPaletteExpanded && index >= 8;
        });
      };
      toggle.addEventListener('click', () => {
        normalPaletteExpanded = !normalPaletteExpanded;
        syncExpanded();
      });
      syncExpanded();
    }
    return grid;
  }

  const brushes = (key        ) => palette.find(category => category.key === key)?.brushes ?? [];
  const basic = addSection('基础', [ERASER, ...brushes('tower'), ...brushes('random')]) ;
  TOWER_MOVE_TOOLS.forEach((tool, direction) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'unity-tool';
    button.dataset.paletteKey = 'move:' + direction;
    button.dataset.paletteShortcut = tool.key;
    button.setAttribute('aria-pressed', String(activeTowerMove === direction));
    button.setAttribute('aria-label', tool.label + '，快捷键 ' + tool.key);
    button.title = button.getAttribute('aria-label') ;
    const face = document.createElement('span');
    face.className = 'unity-tool-face';
    const image = document.createElement('img');
    image.alt = '';
    image.src = '/assets/unity/Direction' + direction + '.png';
    face.append(image);
    const key = document.createElement('kbd');
    key.textContent = tool.key;
    button.append(face, key);
    button.addEventListener('click', () => {
      if (fillPreview) return;
      activeTowerMove = direction;
      updateUnityPalette();
      coverCursor(false);
    });
    basic.append(button);
  });
  addSection('普通牌', brushes('normal'), true);
  addSection('附加效果', brushes('debuff'));
  addSection('功能牌', [...brushes('func'), COIN_BLOCKER]);
  if (focused) {
    [...paletteRoot.querySelectorAll                   ('button')]
      .find(button => button.dataset.paletteKey === focused)?.focus({ preventScroll: true });
  }
}

function updateUnityPalette() {
  const selectedKey = activeTowerMove === null ? st.brush.kind + ':' + st.brush.value : 'move:' + activeTowerMove;
  const counts = new Map                ();
  for (const tile of st.master.tiles) {
    counts.set(tile.tileId, (counts.get(tile.tileId) ?? 0) + 1);
  }
  for (const button of $$('[data-palette-key]')) {
    const tool = button                     ;
    tool.setAttribute('aria-pressed', String(tool.dataset.paletteKey === selectedKey));
    const count = tool.querySelector('.unity-tool-count');
    if (count) showPaletteCount(count, counts.get(Number(tool.dataset.paletteKey?.split(':')[1])) ?? 0);
  }
}

function displayedMaxZ()         {
  return fillPreview?.maxZ ?? (st.showMode === 'all' ? 0 : st.showZ + 1);
}

function maxEditableZ()         {
  const tiles = fillPreview?.board.tiles ?? st.master.tiles;
  return tiles.reduce((maximum, tile) => Math.max(maximum, tile.z + 1), 2);
}

function setMaxZ(value        ) {
  const layer = Math.max(0, Math.min(maxEditableZ(), value));
  if (fillPreview) fillPreview.maxZ = layer;
  else {
    st.showMode = layer ? 'multi' : 'all';
    st.showZ = Math.max(0, layer - 1);
  }
  drawEdit();
}

function renderTemplateStatistics(master        ) {
  // Match Unity's editor statistics: normal IDs and the A–N random markers.
  const towerPositions = new Set(master.towers.map(tower => tower.x + ',' + tower.y));
  const inTower = (tile        ) => towerPositions.has(tile.x + ',' + tile.y);
  const normal = master.tiles.filter(tile => (tile.tileId >= 1000 && tile.tileId <= 9999)
    || (tile.tileId >= -23 && tile.tileId <= -10));
  const ratio = (value        , total        ) => total ? value / total : 0;
  const averagePerLayer = (tiles          ) => ratio(tiles.length, new Set(tiles.map(tile => tile.z)).size);
  const number = (value        ) => value.toFixed(2);
  const ordinary = normal.filter(tile => !inTower(tile));
  const averageOrdinary = Number(number(averagePerLayer(ordinary)));
  const averageSurface = averageOrdinary + master.towers.length;
  // Unity uses this same formula in both the A and B columns.
  const initialCount = Math.trunc(averageSurface / 1.5);
  const group = (id        ) => {
    const tiles = normal.filter(tile => tile.tileId === id);
    const outside = tiles.filter(tile => !inTower(tile));
    return [
      ['总数', tiles.length],
      ['占比', number(ratio(tiles.length, normal.length))],
      ['塔 Tile 占比', number(ratio(tiles.length - outside.length, tiles.length))],
      ['平均非塔表面数', number(averagePerLayer(outside))],
      ['初始 Tile 数', initialCount],
    ];
  };
  const stats = [
    { title: '总模板', items: [
      ['Tile 总数', normal.length],
      ['塔 Tile 数占比', number(ratio(normal.length - ordinary.length, normal.length))],
      ['平均非塔表面数', number(averageOrdinary)],
      ['平均表面数', number(averageSurface)],
    ] },
    { title: 'A 牌', items: group(-10) },
    { title: 'B 牌', items: group(-11) },
  ];
  const target = $('[data-template-stat-groups]') ;
  target.replaceChildren(...stats.map(({ title, items }) => {
    const section = document.createElement('section');
    const heading = document.createElement('h4');
    heading.textContent = title;
    const list = document.createElement('dl');
    for (const [label, value] of items) {
      const row = document.createElement('div');
      const labelEl = document.createElement('dt');
      const valueEl = document.createElement('dd');
      labelEl.textContent = String(label);
      valueEl.textContent = String(value);
      row.append(labelEl, valueEl);
      list.append(row);
    }
    section.append(heading, list);
    return section;
  }));

  const layers = new Map                ();
  for (const tile of master.tiles) {
    if (!inTower(tile)) layers.set(tile.z + 1, (layers.get(tile.z + 1) ?? 0) + 1);
  }
  const layerList = $('[data-template-layer-counts]') ;
  layerList.replaceChildren(...(layers.size ? [...layers].sort(([a], [b]) => a - b).map(([layer, count]) => {
    const item = document.createElement('li');
    item.textContent = layer + ' → ' + count;
    item.setAttribute('aria-label', '第 ' + layer + ' 层 ' + count + ' 张');
    return item;
  }) : [Object.assign(document.createElement('li'), { textContent: '暂无牌' })]));
}

/** Fit the actual playable footprints, including the offset of each tower tile. */
function fitPlayableBoard(master        , width        , height        )         {
  if (!master.tiles.length) return fitView(master, width, height);
  const unit = { s: 1, ox: 0, oy: 0 };
  let left = Infinity, top = Infinity, right = -Infinity, bottom = -Infinity;
  for (const tile of master.tiles) {
    const offset = towerOffset(master.towers, tile);
    const rect = playRectOf(unit, tile, offset.ox, offset.oy);
    left = Math.min(left, rect.x);
    top = Math.min(top, rect.y);
    right = Math.max(right, rect.x + rect.w);
    bottom = Math.max(bottom, rect.y + rect.h);
  }
  const scale = Math.max(1, Math.min(144, (width - 40) / (right - left), (height - 52) / (bottom - top)));
  return { s: scale, ox: width / 2 - (left + right) * scale / 2, oy: (height + 18) / 2 - (top + bottom) * scale / 2 };
}

let simulationGeneration = 0;
function resetSimulation() {
  simulationGeneration++;
  const result = $('[data-simout]') ;
  result.textContent = '';
  ($('[data-sim]')                     ).disabled = false;
}

async function runClearSimulation() {
  const output = $('[data-simout]') ;
  const button = $('[data-sim]')                     ;
  const input = $('[data-sim-runs]')                    ;
  const runs = Number(input.value);
  if (!Number.isInteger(runs) || runs < 1 || runs > 500) {
    output.textContent = '模拟局数请输入 1–500。';
    input.focus();
    return;
  }
  const master = derivePlayMaster();
  if (!master.tiles.length) { output.textContent = '请先载入可试玩的模板。'; return; }
  const structures = sortTilesForSave(st.master.tiles);
  const board = prepareSimulation(master.tiles.map((tile, index) => ({
    ...tile, structure: (structures[index]?.tileId ?? 0) <= -12,
  })));
  const generation = ++simulationGeneration;
  const seed = curDup().seed | 0;
  const capacity = st.tray;
  const signature = playSig(master, capacity);
  button.disabled = true;
  const rows = [];
  try {
    for (const [strategy, label] of Object.entries(strategyLabels)) {
      const trials = strategy === 'random' ? runs : 1;
      const results = [];
      for (let n = 0; n < trials; n++) {
        if (n % 10 === 0) {
          output.textContent = '正在推演 ' + label + '（' + n + '/' + trials + '）';
          await new Promise(resolve => setTimeout(resolve, 0));
          if (generation !== simulationGeneration) return;
        }
        results.push(simulateClear(board, capacity, strategy, makeRng(seed + n * 2654435761 + 1001)));
      }
      rows.push({ strategy, label, trials, ...summarizeRuns(results) });
    }
    if (signature !== playSig(derivePlayMaster(), st.tray)) {
      output.textContent = '试玩参数已变化，请重新运行推演。';
      return;
    }
    output.textContent = '';
    const caption = document.createElement('p');
    caption.textContent = '副本 ' + (st.slot + 1) + ' · 背包 ' + capacity + ' 格 · 同一开局牌面';
    const table = document.createElement('table');
    table.className = 'simulation-results';
    table.innerHTML = '<thead><tr><th>策略</th><th>结果</th><th>背包峰值</th><th>点击</th></tr></thead>';
    const body = table.createTBody();
    for (const row of rows) {
      const cells = body.insertRow();
      const result = row.strategy === 'random'
        ? (row.rate * 100).toFixed(1) + '%（' + row.wins + '/' + row.trials + '）'
        : row.wins ? '通关' : '失败';
      for (const value of [row.label, result, row.peak, row.averageClicks.toFixed(row.trials > 1 ? 1 : 0)]) {
        cells.insertCell().textContent = String(value);
      }
      if (row.firstFailure) cells.title = '示例失败：第 ' + row.firstFailure.clicks + ' 步，' + row.firstFailure.reason;
    }
    output.append(caption, table);
  } catch (error) {
    output.textContent = '推演失败：' + (error         ).message;
  } finally {
    if (generation === simulationGeneration) button.disabled = false;
  }
}

function setPlayMode(play         , restart = true) {
    if (fillPreview) return;
    if(play && !st.master.tiles.length) { localStatus('请先载入模板或摆牌，再一键试玩。',true); return; }
    if (play && restart) playSession = null;
    resetSimulation();
    ($('[data-editpanel]')               ).hidden = play;
    ($('.playpanel')               ).hidden = !play;
    const playButton = $('[data-mode-play]') ;
    playButton.textContent = play ? '返回编辑' : '一键试玩';
    playButton.setAttribute('aria-pressed', String(play));
    $('[data-status-home="'+(play ? 'play' : 'edit')+'"]') .append($('[data-localstatus]') );
    updateBatchNavigation();
    requestAnimationFrame(()=>{ (play?$('[data-pfit]'):$('[data-fit]')) .dispatchEvent(new Event('click')); draw(); renderRightPanels(); if (!play && batchPhase === 'edit') showActiveBatchCard(); });
  }







let batchPhase                               = 'single';
let batchEntries               = [];
let batchActive = -1;
let singleContext                       = null;
let batchMethod = 'ids';
let batchFiles         = [];
let batchLoading = false;
let batchFailures           = [];

function captureEditor() {
  const status = $('[data-localstatus]') ;
  return {
    state: { ...st, dup: st.dup.slice(), selection: new Set(st.selection), editView: { ...st.editView }, playView: { ...st.playView } },
    key: sourceKey, raw: sourceRaw, original: initialBoard, history, signature: savedSignature || initialSignature,
    initialSignature, savedSignature, savedName, translateX, translateY, playSession, currentLevelId,
    groupRules, iapCut, issueSpot,
    id: ($('[data-tplid]')                    ).value,
    label: $('[data-fn]') .textContent || '',
    status: status.textContent || '', error: status.classList.contains('is-error'),
  };
}

function restoreEditor(ctx               ) {
  Object.assign(st, ctx.state);
  st.dup = ctx.state.dup.slice();
  st.selection = new Set(ctx.state.selection);
  st.editView = { ...ctx.state.editView };
  st.playView = { ...ctx.state.playView };
  sourceKey = ctx.key;
  sourceRaw = ctx.raw;
  sourceSerial++;
  history = ctx.history;
  initialBoard = ctx.original;
  initialSignature = ctx.initialSignature;
  savedSignature = ctx.savedSignature;
  savedName = ctx.savedName;
  translateX = ctx.translateX;
  translateY = ctx.translateY;
  playSession = ctx.playSession;
  currentLevelId = ctx.currentLevelId;
  groupRules = ctx.groupRules;
  iapCut = ctx.iapCut;
  issueSpot = ctx.issueSpot;
  endFillPreview();
  ($('[data-seed-mode]')                     ).value = groupRules ? 'group' : 'unity';
  ($('[data-iap-cut]')                    ).value = String(iapCut);
  ($('[data-levelnum]')                    ).value = String(st.levelNum);
  ($('[data-tray]')                    ).value = String(st.tray);
  const id = $('[data-tplid]')                    ;
  id.value = ctx.id;
  id.removeAttribute('aria-invalid');
  setFn(ctx.label);
  localStatus(ctx.status, ctx.error);
  renderSelMode();
  renderAll();
}

function batchStatus(entry            ) {
  if (!entry.context) return entry.master.tiles.length + ' 张';
  const ctx = entry.context;
  return ctx.signature !== JSON.stringify(ctx.state.master) ? '有未另存修改'
    : ctx.savedSignature ? '已另存' : ctx.state.master.tiles.length + ' 张';
}

function renderBatchCard(entry            , index        ) {
  const selected = index === batchActive;
  entry.card .classList.toggle('is-active', selected);
  entry.card .querySelector('[data-batch-card-state]') .textContent = batchStatus(entry);
  entry.card .querySelector('[data-batch-select]') .setAttribute('aria-pressed', String(selected));
  entry.placeholder .hidden = selected;
  if (!selected) {
    const ctx = entry.context;
    const max = ctx && ctx.state.showMode !== 'all' ? ctx.state.showZ + 1 : 0;
    entry.placeholder .innerHTML = coverMarkup(ctx?.state.master ?? entry.master, max);
  }
}

function selectBatch(index        , startPlay = false) {
  if (fillPreview || busy || !batchEntries[index]) return;
  const entry = batchEntries[index];
  if (batchActive !== index) {
    const previous = batchActive;
    if (previous >= 0) batchEntries[previous].context = captureEditor();
    batchActive = index;
    entry.mount .append($('[data-editor-frame]') );
    if (previous >= 0) renderBatchCard(batchEntries[previous], previous);
    if (entry.context) restoreEditor(entry.context);
    else {
      const cfg = drawerConfig;
      const rowIndex = cfg ? findRow(cfg, entry.master.templateId) : -1;
      const row = cfg && rowIndex >= 0 ? cfg.rows[rowIndex] : null;
      history = new History        (200);
      st.master = entry.master;
      st.dup = row ? row.dup.slice() : curveDefault();
      st.strategyGroupId = row?.strategyGroupId ?? '';
      st.randomSeedGroupId = row?.randomSeedGroupId ?? '';
      st.slot = st.currentZ = st.showZ = 0;
      st.showMode = 'all';
      st.selectMode = false;
      st.selection.clear();
      issueSpot = '';
      playSession = null;
      currentLevelId = entry.master.templateId;
      setFn(entry.label);
      adoptSource('', entry.raw);
      renderSelMode();
      renderAll();
    }
    if (!($('.playpanel')               ).hidden) {
      resetSimulation();
      ($('[data-pfit]')                     ).click();
    }
    renderBatchCard(entry, index);
  }
  if (startPlay) setPlayMode(true);
  updateBatchNavigation();
}

function showActiveBatchCard() {
  const card = batchEntries[batchActive]?.card;
  if (card) card.scrollIntoView({ block: 'start', inline: 'nearest' });
}

function updateBatchNavigation() {
  const nav = $('[data-batch-navigation]')               ;
  nav.hidden = batchPhase !== 'edit' || batchActive < 0;
  if (nav.hidden) return;
  ($('[data-batch-prev]')                     ).disabled = batchActive === 0;
  ($('[data-batch-next]')                     ).disabled = batchActive >= batchEntries.length - 1;
  $('[data-batch-play-id]') .textContent = (st.master.templateId || batchEntries[batchActive].label)
    + ' · ' + (batchActive + 1) + '/' + batchEntries.length;
}

function batchScreen(phase                              ) {
  batchPhase = phase;
  document.body.dataset.batchPhase = phase;
  ($('[data-batch-import]')               ).hidden = phase !== 'import';
  ($('[data-editgrid]')               ).hidden = phase === 'import';
  ($('[data-batch-cards]')               ).hidden = phase !== 'edit';
  $('[data-batch-open]') .textContent = phase === 'single' ? '批量修改模板' : '返回单模板';
  ($('[data-batch-resume]')               ).hidden = batchEntries.length === 0;
  updateBatchNavigation();
}

function buildBatchCards() {
  const list = $('[data-batch-cards]') ;
  list.replaceChildren();
  if (batchFailures.length) {
    const issues = document.createElement('details');
    issues.className = 'batch-import-issues';
    const summary = document.createElement('summary');
    summary.textContent = batchFailures.length + ' 项读取失败';
    issues.append(summary, ...batchFailures.map(failure => {
      const item = document.createElement('p'); item.textContent = failure; return item;
    }));
    list.append(issues);
  }
  batchEntries.forEach((entry, index) => {
    const card = document.createElement('article');
    card.className = 'batch-card';
    card.innerHTML = '<header><div><h3></h3><span data-batch-card-state></span></div><button type="button" class="chip" data-batch-select aria-pressed="false">编辑</button><button class="chip act" type="button" data-batch-play>试玩</button></header><div class="batch-board-mount"><div class="cc-board batch-board-placeholder"></div></div>';
    card.querySelector('h3') .textContent = entry.master.templateId || entry.label;
    const placeholder = card.querySelector             ('.batch-board-placeholder') ;
    entry.card = card;
    entry.placeholder = placeholder;
    entry.mount = card.querySelector             ('.batch-board-mount') ;
    card.querySelector('[data-batch-select]') .addEventListener('click', () => selectBatch(index));
    card.querySelector('[data-batch-play]') .addEventListener('click', () => selectBatch(index, true));
    placeholder.addEventListener('pointerdown', event => {
      if (event.button !== 0 && event.button !== 2 || busy) return;
      const tile = (event.target               ).closest             ('[data-board-index]');
      selectBatch(index);
      const board = $('[data-cover-board]') ;
      const target = tile
        ? [...board.querySelectorAll             ('[data-board-index]')].find(el => el.dataset.boardIndex === tile.dataset.boardIndex)
        : board;
      (target ?? board).dispatchEvent(new PointerEvent('pointerdown', {
        bubbles: true, cancelable: true, button: event.button, buttons: event.buttons,
        clientX: event.clientX, clientY: event.clientY, pointerId: event.pointerId,
      }));
    });
    placeholder.addEventListener('contextmenu', event => event.preventDefault());
    list.append(card);
    renderBatchCard(entry, index);
  });
}

async function importBatch() {
  if (batchLoading) return;
  const ids = [...new Set(($('[data-batch-input="ids"] textarea')                       ).value.split(/[\s,，;；]+/).filter(Boolean))];
  const inputs = batchMethod === 'ids' ? ids : batchFiles;
  const feedback = $('[data-batch-feedback]') ;
  const errors = $('[data-batch-errors]') ;
  errors.replaceChildren();
  if (!inputs.length) { feedback.textContent = '请先输入模板 ID 或选择文件。'; return; }
  if (batchEntries.some(entry => entry.context && entry.context.signature !== JSON.stringify(entry.context.state.master))
      && !confirm('上一个批次有未另存的修改，确定导入新的批次？')) return;
  batchLoading = true;
  ($('[data-batch-submit]')                     ).disabled = true;
  ($('[data-batch-open]')                     ).disabled = true;
  ($('[data-batch-resume]')                     ).disabled = true;
  const loaded               = [];
  const failures           = [];
  try {
    await ensureLevelDataLoaded().catch(() => {});
    for (const input of inputs) {
      let label = typeof input === 'string' ? input : input.name;
      feedback.textContent = '正在读取 ' + (loaded.length + failures.length + 1) + '/' + inputs.length + ' · ' + label;
      try {
        let raw            ;
        if (typeof input === 'string') {
          const response = await fetch(TEMPLATE_URL(input.replace(/_m\.bytes$/i, '')) + '?preview=1');
          if (!response.ok) throw new Error((await response.json()).error || '仓库未找到模板');
          raw = new Uint8Array(await response.arrayBuffer());
          label = input.replace(/_m\.bytes$/i, '') + '_m.bytes';
        } else {
          raw = new Uint8Array(await input.arrayBuffer());
          await writeLocal('/api/upload?preview=1', raw);
        }
        loaded.push({ master: fromSave(decodeLevelSave(raw)), raw, label });
      } catch (error) {
        failures.push(label + '：' + (error         ).message);
      }
    }
    if (!loaded.length) {
      feedback.textContent = '没有读到可编辑的模板。';
      for (const failure of failures) {
        const line = document.createElement('li'); line.textContent = failure; errors.append(line);
      }
      return;
    }
    batchActive = -1;
    batchEntries = loaded;
    batchFailures = failures;
    buildBatchCards();
    batchScreen('edit');
    selectBatch(0);
    localStatus('已读取 ' + loaded.length + ' 张模板' + (failures.length ? '，' + failures.length + ' 项失败。' : '。'));
  } finally {
    batchLoading = false;
    ($('[data-batch-submit]')                     ).disabled = false;
    ($('[data-batch-open]')                     ).disabled = false;
    ($('[data-batch-resume]')                     ).disabled = false;
  }
}

function bindBatch() {
  const button = $('[data-batch-open]') ;
  const frame = $('[data-editor-frame]') ;
  const singleParent = frame.parentElement ;
  button.addEventListener('click', () => {
    if (busy || batchLoading || pendingUpload) return;
    ++readRequest;
    if (batchPhase === 'single') {
      singleContext = captureEditor();
      setPlayMode(false);
      batchScreen('import');
      ($('#batch-ids')                       ).focus();
      return;
    }
    if (batchPhase === 'edit' && batchActive >= 0) batchEntries[batchActive].context = captureEditor();
    singleParent.append(frame);
    if (batchActive >= 0) {
      const prior = batchActive;
      batchActive = -1;
      renderBatchCard(batchEntries[prior], prior);
    }
    batchScreen('single');
    setPlayMode(false);
    if (singleContext) restoreEditor(singleContext);
  });

  $('[data-batch-resume]') .addEventListener('click', () => {
    batchScreen('edit');
    selectBatch(0);
  });
  $('[data-batch-form]') .addEventListener('submit', event => { event.preventDefault(); void importBatch(); });
  $('[data-batch-prev]') .addEventListener('click', () => selectBatch(batchActive - 1));
  $('[data-batch-next]') .addEventListener('click', () => selectBatch(batchActive + 1));
  const fileInput = $('#batch-files')                    ;
  const drop = $('.batch-drop') ;
  const replaceFiles = (files                 ) => {
    batchFiles = Array.from(files ?? []);
    $('[data-batch-file-count]') .textContent = batchFiles.length
      ? batchFiles.map(file => file.name).join('、')
      : '支持 .bytes';
  };
  fileInput.addEventListener('change', () => replaceFiles(fileInput.files));
  drop.addEventListener('dragover', event => event.preventDefault());
  drop.addEventListener('drop', event => {
    event.preventDefault();
    replaceFiles((event             ).dataTransfer?.files ?? null);
  });
  for (const item of $$('[data-batch-method]')) {
    item.addEventListener('click', () => {
      batchMethod = (item               ).dataset.batchMethod ;
      for (const tab of $$('[data-batch-method]')) tab.setAttribute('aria-pressed', String(tab === item));
      for (const panel of $$('[data-batch-input]')) (panel               ).hidden = (panel               ).dataset.batchInput !== batchMethod;
    });
  }
}

function bindManual() {
  const dialog = $('[data-help-dialog]')                     ;
  const search = $('[data-manual-search]')                    ;
  const content = $('[data-manual-content]')               ;
  const sections = [...content.querySelectorAll             ('section')];
  const links = [...dialog.querySelectorAll                   ('nav a')];
  function choose(section             ) {
    $('[data-manual-current]') .textContent = section.querySelector('h3') .textContent;
    for (const link of links) {
      if (link.hash === '#' + section.id) link.setAttribute('aria-current', 'location');
      else link.removeAttribute('aria-current');
    }
    content.scrollTop = section.offsetTop;
  }
  function filter() {
    const terms = search.value.trim().toLowerCase().split(/\s+/).filter(Boolean);
    for (const section of sections) section.hidden = terms.some(term => !section.textContent .toLowerCase().includes(term));
    const matches = sections.filter(section => !section.hidden);
    ($('[data-manual-empty]')               ).hidden = matches.length > 0;
    if (terms.length && matches.length) choose(matches[0]);
    else if (!matches.length) {
      $('[data-manual-current]') .textContent = '无匹配章节';
      links.forEach(link => link.removeAttribute('aria-current'));
    }
    content.scrollTop = 0;
  }
  search.addEventListener('input', filter);
  links.forEach(link => link.addEventListener('click', event => {
    event.preventDefault();
    search.value = '';
    filter();
    choose(sections.find(section => '#' + section.id === link.hash) );
  }));
  $('[data-help]') .addEventListener('click', () => {
    search.value = '';
    filter();
    dialog.showModal();
    const topic = !($('.playpanel')               ).hidden ? 'manual-play' : batchPhase !== 'single' ? 'manual-batch' : 'manual-start';
    choose(sections.find(section => section.id === topic) );
  });
}

// ---------- 启动 ----------
async function boot()                {
  palette = buildPalette([]);
  st.cat = palette[0] ;
  st.brush = st.cat.brushes[0] ?? ERASER;
  bind();
  renderBrushes();
  if (!document.activeElement || document.activeElement === document.body) ($('[data-tplid]')                    ).focus();
  try {
    const [catalogResponse, iconResponse] = await Promise.allSettled([
      fetch(CATALOG_URL), fetch('/api/tile-icons'),
    ]);
    if (catalogResponse.status === 'fulfilled' && catalogResponse.value.ok) {
      catalog = parseTileCatalog(await catalogResponse.value.text());
    }
    if (iconResponse.status === 'fulfilled' && iconResponse.value.ok) {
      const { icons } = await iconResponse.value.json();
      if (Array.isArray(icons)) availableTileIcons = new Set(icons);
    }
  } catch { /* 离线/缺文件也能用内置兜底 */ }
  try {
    const res = await fetch(CONFIG_LEVEL_TILE_URL);
    if (res.ok) levelPools = parseLevelTilePools(await res.text());
  } catch { /* 没载到类型池就兜底回等价预览 */ }
  try {
    await ensureLevelDataLoaded();
  } catch (e) {
    const error = '关卡数据加载失败：' + (e         ).message;
    drawer = reduceDrawer(drawer, { type: 'failed', error });
    indexDrawer = { ...indexDrawer, status: 'error', error };
    reportLevelDataError('', new Error(error));
  }
  palette = buildPalette(catalog);
  for (const category of palette) {
    for (const brush of category.brushes) {
      if (brush.kind !== 'tile') continue;
      const name = brush.des?.trim() || brush.label;
      if (name && !/^-?\d+$/.test(name)) tileNames.set(brush.value, name);
    }
  }
  for (const entry of catalog) {
    const theme = /^zm(\d{3})$/.exec(entry.icon);
    if (theme && entry.des.trim().toLowerCase() === 'zm') {
      tileNames.set(entry.tileId, '主题牌 ' + theme[1]);
    } else if (entry.des.trim()) {
      tileNames.set(entry.tileId, entry.des.trim());
    }
  }
  tileNames.set(COIN_BLOCKER.value, COIN_BLOCKER.label);
  for (const c of palette) for (const b of c.brushes) {
    if (b.kind === 'tile' || b.kind === 'random') colorMap.set(b.value, b.color);
  }
  for (const [debuffId, , iconSrc] of DEBUFFS) ensureDebuffIcon(debuffId, iconSrc);
  for (const e of catalog) if (e.icon) iconMap.set(e.tileId, e.icon);
  if (!iconMap.has(COIN_BLOCKER.value)) iconMap.set(COIN_BLOCKER.value, '10000_coin');
  st.cat = palette[0] ; st.brush = palette[0] .brushes[0] ?? ERASER;

  const generatedDraft = null;
  const restoredDraft = false;
  if (generatedDraft) {
    adoptMaster(generatedDraft         , `${(generatedDraft          ).templateId}_m（自动生成）`);
    scheduleDraftSave();
    toast('已载入自动生成的关卡');
  } else if (!restoredDraft) {
    st.editView = fitView(st.master, canvas('edit').clientWidth, canvas('edit').clientHeight);
    st.playView = { ...st.editView };
  }
  renderCats(); renderAll(); zoomLabel();
  if (!initialSignature) { initialBoard = structuredClone(st.master); initialSignature = currentSignature(); }
  renderDupDrawer();
  renderIndexDrawer();
  renderLevelDrawer();
  loadTemplateList();

  // 深链 ?tpl=666 直接载入该母版
  const tpl = new URLSearchParams(location.search).get('tpl');
  if (tpl && !restoredDraft) { (($('[data-tplid]')                    ).value = tpl); loadTemplate(tpl); }
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();
