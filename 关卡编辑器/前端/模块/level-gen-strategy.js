/**
 * Phase 3：策略组（StrategyGroupId）。移植自 tile 工程 main 分支
 * `ulink/.../Data/LevelGenerator/Challenge/*`，在类型分配（generateFaces/Structured）之前
 * 重塑阵型标记：把结构块标记 C..N(-12..-23) 转成 A(-10)/B(-11) 或别的结构块，从而改变 A/B 的分布与数量。
 *
 * 【与主玩的差异 —— 已与用户确认用「种子化等价预览」】
 * 主玩 `RandomUtils.GetRandomItems` 用无种子静态 System.Random，type=2(RandomUse) 又用无种子
 * new System.Random() 洗策略顺序 —— 真机本身每次进关都可能不同、不可复现。这里把这些随机点统一
 * 换成按「关卡/副本/种子/策略组」派生的确定种子 → 预览稳定、可复现、具代表性。
 *   · 策略 7/8（全量转 A/B，天然无随机挑选）与真机逻辑等价，逐 tile 一致。
 *   · 策略 1-6 / type=2 是「一种可能结果」，非某次真机的还原。
 *
 * 只对「新模板」生效：templateId 含 model / hhmy / zwd（对齐 LevelSelectHelper.IsNewTemplate）。
 */
import {makeRng} from './logic.js';

export const RANDOM_ID_A = -10;
export const RANDOM_ID_B = -11;
/** 结构块（“新随机 tile”）阈值：SourceTileId <= -12，对齐 TileData.IsNewRandomTile */
export const STRUCTURE_THRESHOLD = -12;

// 用 const 对象而非 TS enum：enum 非可擦除语法，Node --test 的 type-stripping 会拒绝。
export const UseStrategyType = { NoUse: 0, SequenceUse: 1, RandomUse: 2 }         ;


/** config_level_strategy_group.csv：groupId → 10 个副本档的原始字符串（"type|number|s1;s2;..."） */







                                                                             



                                                                         


                                                                     
                                                                  
                                                            


// ---------- 新模板判定（对齐 LevelSelectHelper.IsNewTemplate） ----------
export function isNewTemplate(templateId                           )          {
  if (!templateId) return false;
  return templateId.includes('model') || templateId.includes('hhmy') || templateId.includes('zwd');
}

// ---------- 种子化 RNG（替换主玩无种子 System.Random） ----------


export function makeSeededRng(seed        )            {
  const r = makeRng(seed >>> 0);
  return { nextInt: (n        ) => (n <= 0 ? 0 : Math.floor(r() * n)) };
}

/** 稳定组合 seed：同（关卡/副本/副本种子/策略组）→ 同序列 → 预览可复现 */
export function strategySeed(levelNum        , dupIndex        , dupSeed        , groupId        )         {
  let h = 2166136261 >>> 0;
  for (const v of [levelNum, dupIndex, dupSeed, groupId, 0x53545241 /* 'STRA' 盐 */]) {
    h = Math.imul(h ^ (v | 0), 16777619) >>> 0;
  }
  return h >>> 0;
}

/**
 * 对齐 RandomUtils.GetRandomItems：无放回随机取 count 个。
 * 边界完全照搬：items 空 / count<=0 / count>items.length → 原样返回（不复制、保持原序）。
 * 注意 count===items.length 时仍走抽样循环 → 返回的是随机排列（主玩就是这个行为）。
 */
export function getRandomItems   (items     , count        , rng           )      {
  if (items == null || count <= 0 || count > items.length) return items;
  const copy = items.slice();
  const result      = [];
  for (let i = 0; i < count; i++) {
    const idx = rng.nextInt(copy.length);
    result.push(copy[idx] );
    copy.splice(idx, 1);
  }
  return result;
}

function toInt(s        , def = 0)         {
  const n = parseInt(s, 10);
  return Number.isFinite(n) ? n : def;
}

// ---------- 解析 config_level_strategy_group.csv ----------
export function parseStrategyGroupTable(csv        )                     {
  const lines = csv.split(/\r?\n/).filter(l => l.trim().length > 0);
  const t                     = new Map();
  for (let i = 3; i < lines.length; i++) {           // 前 3 行：表头/类型/注释
    const c = lines[i] .split(',');
    const gid = parseInt(c[0] , 10);
    if (!Number.isFinite(gid)) continue;
    t.set(gid, c.slice(1, 11).map(x => (x ?? '').trim()));  // InstanceId1..10 → 副本 0..9
  }
  return t;
}

// ---------- 取档位 meta（对齐 StrategyGroupConfig.GetStrategyGroupMeta） ----------
export function getStrategyGroupMeta(
  table                    , dupIndex        , groupId        , rng           ,
)                    {
  const noUse = (config = '')                    =>
    ({ useStrategyType: UseStrategyType.NoUse, targetCount: 0, strategyIds: [], strategyGroupId: groupId, config });

  const row = table.get(groupId);
  if (!row) return noUse();

  const str = row[dupIndex] ?? '';
  if (str === '') return noUse(str);

  const list = str.split('|');
  if (list.length <= 2) return noUse(str);
  if ((list[2] ?? '') === '') return noUse(str);

  const type = toInt(list[0] );
  if (type !== 1 && type !== 2) return noUse(str);       // 0 或其它 = 不使用

  const number = toInt(list[1] );
  if (number <= 0) return noUse(str);

  const strategyList = list[2] .split(';');
  const count = Math.min(number, strategyList.length);

  if (type === 2) {
    // RandomUse：Fisher-Yates 洗策略顺序（主玩用无种子 Random，这里用种子化等价）
    for (let i = strategyList.length - 1; i > 0; i--) {
      const j = rng.nextInt(i + 1);
      if (i !== j) { const tmp = strategyList[i] ; strategyList[i] = strategyList[j] ; strategyList[j] = tmp; }
    }
  }

  const results = strategyList.map(s => toInt(s));       // 含全部策略；count 控制实际应用个数
  return {
    useStrategyType: type === 1 ? UseStrategyType.SequenceUse : UseStrategyType.RandomUse,
    targetCount: count, strategyIds: results, strategyGroupId: groupId, config: str,
  };
}

// ---------- 工作态 tile：分开追踪 TileId / SourceTileId ----------

const isNewRandom = (t           ) => t.sourceTileId <= STRUCTURE_THRESHOLD;

function groupByZLayer(tiles             )                           {
  const m = new Map                     ();
  for (const t of tiles) {
    let g = m.get(t.z);
    if (!g) { g = []; m.set(t.z, g); }
    g.push(t);
  }
  return m;
}
function entriesByZ(tiles             , desc         )                          {
  const e = [...groupByZLayer(tiles).entries()];
  e.sort((a, b) => desc ? b[0] - a[0] : a[0] - b[0]);
  return e;
}

/** 对齐 BaseChallengeStrategy.ReplaceTileType */
function replaceTileType(all             , layer             , tileTypeValue        , count        , rng           )          {
  const randomSet           = [];                       // 去重保序（等价 C# HashSet 小集合插入序）
  const seen = new Set        ();
  for (const t of layer) {
    if (isNewRandom(t) && !seen.has(t.sourceTileId)) { seen.add(t.sourceTileId); randomSet.push(t.sourceTileId); }
  }
  if (randomSet.length === 0) return false;
  const picked = getRandomItems(randomSet, count, rng);
  for (const sourceTileId of picked) {
    for (const t of all) {
      if (t.sourceTileId === sourceTileId) { t.tileId = tileTypeValue; t.sourceTileId = tileTypeValue; }
    }
  }
  return true;
}

// 第二层（含结构块的层，从上往下数）→ A/B；最上层 → A/B
function strategyTopNth(all             , nth        , target        , rng           )          {
  let index = 0, z                = null;
  for (const [key, tiles] of entriesByZ(all, true)) {
    if (tiles.some(isNewRandom)) { index++; if (index === nth) { z = key; break; } }
  }
  if (index < nth || z === null) return false;
  return replaceTileType(all, groupByZLayer(all).get(z) , target, 1, rng);
}

/** 策略 4：倒数 1-2 层找 3 个同款 a/b → 变成该模板内某结构块类型 */
function strategy4(all             , rng           )          {
  const listZ           = [];
  const sourceTileIds           = [];
  const seen = new Set        ();
  let index = 0;
  const asc = entriesByZ(all, false);
  for (const [z, tiles] of asc) {
    for (const t of tiles) if (isNewRandom(t) && !seen.has(t.sourceTileId)) { seen.add(t.sourceTileId); sourceTileIds.push(t.sourceTileId); }
    index++;
    if (index <= 2) listZ.push(z);
  }
  if (listZ.length <= 0 || sourceTileIds.length <= 0) return false;

  const byZ = groupByZLayer(all);
  let randomTiles              = [];
  for (let i = listZ.length - 1; i >= 0; i--) {
    const tiles = byZ.get(listZ[i] );
    if (!tiles) continue;
    const aTiles = tiles.filter(t => t.tileId === RANDOM_ID_A);
    const bTiles = tiles.filter(t => t.tileId === RANDOM_ID_B);
    const aRnd = getRandomItems(aTiles, 3, rng);
    if (aRnd.length >= 3) { randomTiles = aRnd; break; }
    const bRnd = getRandomItems(bTiles, 3, rng);
    if (bRnd.length >= 3) { randomTiles = bRnd; break; }
  }
  if (randomTiles.length <= 0) return false;

  const tileTypes = getRandomItems(sourceTileIds, 1, rng);
  const tileType = tileTypes.length > 0 ? tileTypes[0]  : 0;
  if (tileType <= STRUCTURE_THRESHOLD && randomTiles.length >= 3) {
    for (let i = 0; i < 3; i++) { randomTiles[i] .tileId = tileType; randomTiles[i] .sourceTileId = tileType; }
    return true;
  }
  return false;
}

/** 策略 6：随机选 2 种结构块，一种全变成另一种 */
function strategy6(all             , rng           )          {
  const groups = new Map                     ();
  for (const t of all) {
    if (isNewRandom(t)) {
      let g = groups.get(t.sourceTileId);
      if (!g) { g = []; groups.set(t.sourceTileId, g); }
      g.push(t);
    }
  }
  if (groups.size === 0) return false;
  const picked = getRandomItems([...groups.keys()], 2, rng);
  if (picked.length < 2) return false;
  const src = picked[0] , tgt = picked[1] ;
  for (const t of groups.get(src) ) { t.tileId = tgt; t.sourceTileId = tgt; }
  return true;
}

/** 策略 7/8：所有结构块全变 A/B */
function strategyAll(all             , target        )          {
  let any = false;
  for (const t of all) if (isNewRandom(t)) { t.tileId = target; t.sourceTileId = target; any = true; }
  return any;
}

function runStrategy(id        , all             , rng           )          {
  switch (id) {
    case 1: return strategyTopNth(all, 2, RANDOM_ID_A, rng);
    case 2: return strategyTopNth(all, 2, RANDOM_ID_B, rng);
    case 3: return strategyTopNth(all, 1, RANDOM_ID_A, rng);
    case 5: return strategyTopNth(all, 1, RANDOM_ID_B, rng);
    case 4: return strategy4(all, rng);
    case 6: return strategy6(all, rng);
    case 7: return strategyAll(all, RANDOM_ID_A);
    case 8: return strategyAll(all, RANDOM_ID_B);
    default: return false;
  }
}

/** 对齐 ChallengeSimplifier.UseStrategyGroup：按 targetCount 依次取策略，失败则顺延（count++） */
function useStrategyGroup(all             , meta                   , rng           )                                            {
  const effective           = [], failed           = [];
  let count = meta.targetCount;
  for (let i = 0; i < count; i++) {
    if (i >= meta.strategyIds.length) break;
    const sid = meta.strategyIds[i] ;
    if (runStrategy(sid, all, rng)) effective.push(sid);
    else { failed.push(sid); count++; }
  }
  return { effective, failed };
}

/**
 * 主入口：阵型标记（须为 TileDatas Z→Y→X 顺序）+ 档位 → 策略重塑后的标记 tileId。
 * 调用方自行判定 isNewTemplate + groupId 有效后再调。seed 用 strategySeed() 派生。
 */
export function applyStrategyGroup(
  tiles                                          ,
  dupIndex        , groupId        , table                    , seed        ,
)                 {
  const rng = makeSeededRng(seed);
  const meta = getStrategyGroupMeta(table, dupIndex, groupId, rng);
  const work              = tiles.map(t => ({ z: t.z, tileId: t.tileId, sourceTileId: t.tileId }));

  if (meta.useStrategyType === UseStrategyType.NoUse) {
    return { tileIds: work.map(w => w.tileId), useStrategyType: meta.useStrategyType, config: meta.config, effective: [], failed: [], applied: false };
  }
  const { effective, failed } = useStrategyGroup(work, meta, rng);
  return { tileIds: work.map(w => w.tileId), useStrategyType: meta.useStrategyType, config: meta.config, effective, failed, applied: true };
}
