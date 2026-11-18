/**
 * 种子组「结构化图案分配」—— 逐行移植 tile 工程 main 分支的 RandomSeed 流水线。
 * 当 config_level 的 randomSeed_groupID 命中种子组配置（seedConfig != null）时，主玩走这条，
 * 替代原流程（均分+洗牌）。本模块复刻其**全部确定性逻辑**与 RNG 消费顺序。
 *
 * 源（ulink/.../Data/LevelGenerator/RandomSeed/）：
 *   RandomSeedConfigLoader / RandomSeedConfig / PatternClassifier / StageDivider /
 *   RandomSeedAllocator / ChokeLayerPlanner / ConsecutiveLayerPlanner / TileAssigner
 * 入口对齐 LevelDupGenerator.RandomizeLevelDataTiles 的 seedConfig 分支：
 *   单一 CustomRandom(GetSeed(seed)) 连续喂给 Classify → A 分配 → B 分配。
 *
 * 未移植（主玩里本就没走）：NonChokeLayerPlanner、TileAssigner.FixDivisibility/ApplyStageKindConstraint、
 * AllocationReport（纯上报）。低 ID 标记 C..N 仍不在此处理。
 */
import {CustomRandom, getSeed, prepareAllNeedTileIds, RANDOM_ID_A, RANDOM_ID_B, ELIMINATE_SAME_COUNT} from './level-gen.js';

const GOLDEN_SHELL_TILE_ID = 1010; // TileTypeValues.GoldenShellTileId：金壳下 tile，不能当卡点

// ---------- 配置（config_level_randomSeed_group.csv + config_level_randomSeed.csv） ----------





/** groupId → [dup1..dup10 的 seedID] */

/** seedId → 原始行字段 */


function parseIntList(s        )           {
  return (s ?? '').split('|').map(x => parseInt(x, 10)).filter(n => Number.isFinite(n));
}

export function parseSeedGroupTable(csv        )                 {
  const lines = csv.split(/\r?\n/).filter(l => l.trim().length > 0);
  const t                 = new Map();
  for (let i = 3; i < lines.length; i++) {
    const c = lines[i] .split(',');
    const gid = parseInt(c[0] , 10);
    if (!Number.isFinite(gid)) continue;
    t.set(gid, c.slice(1, 11).map(x => parseInt(x, 10) || 0));
  }
  return t;
}

export function parseSeedTable(csv        )            {
  // seedID,GroupID,ChokeTile_num,ChokeTile_startLayer,ChokeTile_param,NormalTile_param,Stage_param,TileKind_num
  const lines = csv.split(/\r?\n/).filter(l => l.trim().length > 0);
  const t            = new Map();
  for (let i = 3; i < lines.length; i++) {
    const c = lines[i] .split(',');
    const seedId = parseInt(c[0] , 10);
    if (!Number.isFinite(seedId)) continue;
    t.set(seedId, {
      seedId, groupId: parseInt(c[1] , 10) || 0,
      chokeTileNum: parseInt(c[2] , 10) || 0,
      chokeStartLayer: parseInt(c[3] , 10) || 0,
      chokeLayerGap: parseInt(c[4] , 10) || 0,
      normalLayerGap: parseInt(c[5] , 10) || 0,
      stagePercents: parseIntList(c[6] ?? ''),
      stageKindNums: parseIntList(c[7] ?? ''),
    });
  }
  return t;
}

/** RandomSeedConfigLoader.TryLoad：groupId<=0 或查不到 → null；dupIndex 0-based（≥9 取第10档） */
export function tryLoadSeedConfig(groupTable                , seedTable           , groupId        , dupIndex        )                    {
  if (groupId <= 0) return null;
  const group = groupTable.get(groupId);
  if (!group) return null;
  const seedId = group[Math.min(dupIndex, 9)] ?? 0;
  if (seedId <= 0) return null;
  return seedTable.get(seedId) ?? null;
}

// ---------- 工作态 tile ----------


// ---------- PatternClassifier ----------

export function patternClassify(candidate          , tileTypeCount        , chokeTileNum        , rng              )                 {
  const effectiveCount = Math.min(tileTypeCount, candidate.length);
  const pool           = [];
  for (let i = 0; i < effectiveCount; i++) pool.push(candidate[i] );

  const chokePool = pool.slice();
  const gi = chokePool.indexOf(GOLDEN_SHELL_TILE_ID);
  if (gi >= 0) chokePool.splice(gi, 1);

  const actualChokeNum = Math.min(chokeTileNum, chokePool.length);
  const choke           = [];
  for (let i = 0; i < actualChokeNum; i++) {
    const idx = rng.getRandom(chokePool.length);
    choke.push(chokePool[idx] );
    chokePool.splice(idx, 1);
  }
  const chokeSet = new Set(choke);
  const nonChoke = pool.filter(p => !chokeSet.has(p));
  return { choke, nonChoke };
}

// ---------- TileData 排序 / 收集 ----------
/** TileData.CompareTo：Z→Y→X 升序 */
function cmpZYX(a          , b          )         { return a.z - b.z || a.y - b.y || a.x - b.x; }

// ---------- 层分配计划结构 ----------




// ---------- ChokeLayerPlanner ----------
function mapSum(m                     )         { let s = 0; for (const v of m.values()) s += v; return s; }

function chokeGetAvailableLayers(cap                     , maxLayerZ        , excludeTop        )           {
  const threshold = maxLayerZ - excludeTop + 1;
  const out           = [];
  for (const [k, v] of cap) if (k <= threshold && v > 0) out.push(k);
  out.sort((a, b) => a - b);
  return out;
}
function findValidGapPositions(avail          , requiredGap        )                     {
  const pos                     = [];
  for (let i = 0; i < avail.length - 1; i++) if (avail[i + 1]  - avail[i]  >= requiredGap) pos.push([i, i + 1]);
  if (pos.length > 0) return pos;
  for (let i = 0; i < avail.length - 1; i++)
    for (let j = i + 1; j < avail.length; j++)
      if (avail[j]  - avail[i]  >= requiredGap) pos.push([i, j]);
  return pos;
}
function splitCountNotDiv3(total        , rng              )                   {
  if (total <= 1) return [total, 0];
  const maxLower = total - 1;
  let lower = 1 + rng.getRandom(maxLower);
  let upper = total - lower;
  for (let a = 0; a < total; a++) {
    if (lower % 3 !== 0 && upper % 3 !== 0) break;
    lower++; if (lower >= total) lower = 1; upper = total - lower;
  }
  return [lower, upper];
}
function adjustForCapacity(total        , lowerCap        , upperCap        )                   {
  const totalCap = lowerCap + upperCap;
  if (totalCap < total) return [-1, -1];
  let lower = Math.floor(total * lowerCap / totalCap);
  let upper = total - lower;
  if (lower > lowerCap) { lower = lowerCap; upper = total - lower; }
  if (upper > upperCap) { upper = upperCap; lower = total - upper; }
  for (let a = 0; a < 6; a++) {
    if (lower % 3 !== 0 && upper % 3 !== 0) break;
    if (lower > 1 && upper < upperCap) { lower--; upper++; }
    else if (upper > 1 && lower < lowerCap) { upper--; lower++; }
    else break;
  }
  return [lower, upper];
}
function distributeToLayers(count        , layers          , cap                     , result                     , rng              )       {
  if (layers.length === 0 || count <= 0) return;
  const perLayer = Math.floor(count / layers.length);
  const remainder = count % layers.length;
  const shuffled = layers.slice();
  for (let i = shuffled.length - 1; i > 0; i--) { const j = rng.getRandom(i + 1); const t = shuffled[i] ; shuffled[i] = shuffled[j] ; shuffled[j] = t; }
  let distributed = 0;
  for (let i = 0; i < layers.length && distributed < count; i++) {
    const z = shuffled[i] ;
    const c = cap.get(z) ;
    let alloc = perLayer + (i < remainder ? 1 : 0);
    if (alloc > c) alloc = c;
    result.set(z, (result.get(z) ?? 0) + alloc);
    distributed += alloc;
  }
  if (distributed < count) {
    for (const z of shuffled) {
      if (distributed >= count) break;
      const c = cap.get(z) ;
      const cur = result.get(z) ?? 0;
      const extra = c - cur;
      if (extra <= 0) continue;
      const add = Math.min(count - distributed, extra);
      result.set(z, (result.get(z) ?? 0) + add);
      distributed += add;
    }
  }
}
function tryAllocateWithGap(total        , avail          , cap                     , requiredGap        , rng              )                        {
  const gapPositions = findValidGapPositions(avail, requiredGap);
  if (gapPositions.length === 0) return null;
  const gapIdx = rng.getRandom(gapPositions.length);
  const [lowerLastIdx, upperFirstIdx] = gapPositions[gapIdx] ;
  const lowerLayers           = []; for (let i = 0; i <= lowerLastIdx; i++) lowerLayers.push(avail[i] );
  const upperLayers           = []; for (let i = upperFirstIdx; i < avail.length; i++) upperLayers.push(avail[i] );
  const actualGap = avail[upperFirstIdx]  - avail[lowerLastIdx] ;
  let [lowerCount, upperCount] = splitCountNotDiv3(total, rng);
  const lowerCap = lowerLayers.reduce((s, l) => s + cap.get(l) , 0);
  const upperCap = upperLayers.reduce((s, l) => s + cap.get(l) , 0);
  if (lowerCount > lowerCap || upperCount > upperCap) {
    [lowerCount, upperCount] = adjustForCapacity(total, lowerCap, upperCap);
    if (lowerCount < 0 || upperCount < 0) return null;
  }
  const result = new Map                ();
  distributeToLayers(lowerCount, lowerLayers, cap, result, rng);
  distributeToLayers(upperCount, upperLayers, cap, result, rng);
  if (mapSum(result) < total) return null;
  return { layer: result, actualMaxGap: actualGap, degraded: false };
}
function chokePlan(total        , cap                     , chokeStartLayer        , chokeLayerGap        , maxLayerZ        , rng              )                 {
  const plan                 = { layer: new Map(), actualMaxGap: 0, degraded: false };
  if (total <= 0) return plan;
  let excludeTop = chokeStartLayer;
  let gap = chokeLayerGap;
  for (;;) {
    const avail = chokeGetAvailableLayers(cap, maxLayerZ, excludeTop);
    if (avail.length === 0) {
      if (excludeTop > 0) { excludeTop--; plan.degraded = true; continue; }
      plan.degraded = true; break;
    }
    const result = tryAllocateWithGap(total, avail, cap, gap, rng);
    if (result) {
      plan.layer = result.layer; plan.actualMaxGap = result.actualMaxGap;
      plan.degraded = excludeTop !== chokeStartLayer || gap !== chokeLayerGap;
      break;
    }
    if (excludeTop > 0) { excludeTop--; plan.degraded = true; }
    else if (gap > 1) { gap--; plan.degraded = true; }
    else {
      distributeToLayers(total, avail, cap, plan.layer, rng);
      plan.degraded = true; break;
    }
  }
  return plan;
}

// ---------- ConsecutiveLayerPlanner（不消费 RNG） ----------
function consecutivePlan(total        , cap                     , consecutiveLayers        )                  {
  const plan                  = { layer: new Map(), degraded: false, overflow: 0 };
  if (total <= 0) return plan;
  const avail           = [];
  for (const [k, v] of cap) if (v > 0) avail.push(k);
  avail.sort((a, b) => a - b);
  if (avail.length === 0) { plan.degraded = true; plan.overflow = total; return plan; }

  let targetN = consecutiveLayers;
  if (targetN <= 0) targetN = 1;
  if (total < targetN) targetN = total;
  if (targetN > avail.length) targetN = avail.length;

  const perLayer = Math.floor(total / targetN);
  const remainder = total % targetN;
  let overflow = 0;
  for (let i = 0; i < targetN; i++) {
    const z = avail[i] ;
    const ideal = (i < targetN - remainder) ? perLayer : perLayer + 1;
    const wanted = ideal + overflow;
    const c = cap.get(z) ;
    const actual = wanted <= c ? wanted : c;
    if (actual > 0) plan.layer.set(z, actual);
    overflow = wanted - actual;
  }
  if (overflow > 0) {
    plan.degraded = true;
    for (let i = targetN; i < avail.length && overflow > 0; i++) {
      const z = avail[i] ; const c = cap.get(z) ;
      const actual = overflow <= c ? overflow : c;
      if (actual > 0) plan.layer.set(z, (plan.layer.get(z) ?? 0) + actual);
      overflow -= actual;
    }
  }
  if (overflow > 0) {
    for (let i = 0; i < avail.length && overflow > 0; i++) {
      const z = avail[i] ;
      const used = plan.layer.get(z) ?? 0;
      const c = cap.get(z)  - used;
      if (c <= 0) continue;
      const actual = overflow <= c ? overflow : c;
      plan.layer.set(z, (plan.layer.get(z) ?? 0) + actual);
      overflow -= actual;
    }
  }
  plan.overflow = overflow;
  return plan;
}

// ---------- TileAssigner ----------
function calculatePatternQuotas(totalTiles        , choke          , nonChoke          )                      {
  const all = [...choke, ...nonChoke];
  const groupCount = Math.floor(totalTiles / ELIMINATE_SAME_COUNT);
  const perPattern = all.length > 0 ? Math.floor(groupCount / all.length) : 0;
  const remainder = all.length > 0 ? groupCount % all.length : 0;
  const q = new Map                ();
  for (let i = 0; i < all.length; i++) q.set(all[i] , (perPattern + (i < remainder ? 1 : 0)) * ELIMINATE_SAME_COUNT);
  return q;
}
/** AssignTiles：按层把 slot 洗牌后顺序贴到该层 tile（tiles 已 Z→Y→X 排序） */
function assignTiles(sortedTiles            , assignments              , rng              )       {
  const layerTiles = new Map                    ();
  for (const t of sortedTiles) { if (!layerTiles.has(t.z)) layerTiles.set(t.z, []); layerTiles.get(t.z) .push(t); }

  const layerSlots = new Map                                 ();
  for (const a of assignments) {
    for (const [z, count] of a.layer) {
      if (!layerSlots.has(z)) layerSlots.set(z, []);
      const arr = layerSlots.get(z) ;
      for (let i = 0; i < count; i++) arr.push({ patternId: a.patternId });
    }
  }
  for (const slots of layerSlots.values()) {
    for (let i = slots.length - 1; i > 0; i--) { const j = rng.getRandom(i + 1); const t = slots[i] ; slots[i] = slots[j] ; slots[j] = t; }
  }
  for (const [z, tiles] of layerTiles) {
    const slots = layerSlots.get(z);
    if (!slots) continue;
    const n = Math.min(slots.length, tiles.length);
    for (let i = 0; i < n; i++) tiles[i] .tileId = slots[i] .patternId;
  }
}

// ---------- GroupPatternsByStagePercents ----------
function groupPatternsByStagePercents(patterns          , stagePercents          )             {
  const groups             = [];
  if (patterns.length === 0) { groups.push([]); return groups; }
  if (stagePercents.length === 0) { groups.push(patterns.slice()); return groups; }
  const total = patterns.length;
  let assigned = 0;
  let percentSum = stagePercents.reduce((s, p) => s + p, 0);
  if (percentSum <= 0) percentSum = 100;
  for (let i = 0; i < stagePercents.length; i++) {
    let count        ;
    if (i === stagePercents.length - 1) count = total - assigned;
    else {
      count = Math.floor((total * stagePercents[i]  * 2 + percentSum) / (percentSum * 2));
      if (assigned + count > total) count = total - assigned;
    }
    if (count < 0) count = 0;
    groups.push(patterns.slice(assigned, assigned + count));
    assigned += count;
  }
  return groups;
}

// ---------- EnsureAllTilesAssignedAndDivisible ----------
function ensureAllTilesAssignedAndDivisible(allTiles            , nonChoke          , rng              )       {
  if (nonChoke.length === 0) return;
  const ec = ELIMINATE_SAME_COUNT;
  const patternTiles = new Map                    ();
  const orphans             = [];
  for (const t of allTiles) {
    if (t.tileId < 0) { orphans.push(t); continue; }
    if (!patternTiles.has(t.tileId)) patternTiles.set(t.tileId, []);
    patternTiles.get(t.tileId) .push(t);
  }
  for (const list of patternTiles.values()) {
    const excess = list.length % ec;
    if (excess === 0) continue;
    for (let i = 0; i < excess; i++) orphans.push(list[list.length - 1 - i] );
  }
  if (orphans.length === 0) return;
  let idx = 0;
  const groupCount = Math.floor(orphans.length / ec);
  for (let g = 0; g < groupCount; g++) {
    const patternId = nonChoke[rng.getRandom(nonChoke.length)] ;
    for (let j = 0; j < ec; j++) { orphans[idx] .tileId = patternId; idx++; }
  }
  if (idx < orphans.length) {
    const countMap = new Map                ();
    for (const t of allTiles) if (t.tileId > 0) countMap.set(t.tileId, (countMap.get(t.tileId) ?? 0) + 1);
    let bestPatternId = nonChoke[0] ; let bestCount = 0;
    for (const pid of nonChoke) { const c = countMap.get(pid) ?? 0; if (c > bestCount) { bestCount = c; bestPatternId = pid; } }
    for (; idx < orphans.length; idx++) orphans[idx] .tileId = bestPatternId;
  }
}

// ---------- AllocatePatterns（编排器） ----------
function allocatePatterns(tiles            , towerTiles            , candidate          , tileTypeCount        ,
  config            , rng              , shared                , group           )       {
  if (tiles.length === 0) return;

  const sorted = tiles.slice().sort(cmpZYX);

  const layerCapacity = new Map                ();
  for (const t of sorted) layerCapacity.set(t.z, (layerCapacity.get(t.z) ?? 0) + 1);
  let maxLayerZ = -Infinity; for (const k of layerCapacity.keys()) if (k > maxLayerZ) maxLayerZ = k;

  const isA = group === 'A';
  const effectiveChoke = isA ? shared.choke : [];
  const nonChoke = shared.nonChoke;

  const towerCount = towerTiles.length;
  const totalCount = sorted.length + towerCount;
  const quotas = calculatePatternQuotas(totalCount, effectiveChoke, nonChoke);

  const capacityRemaining = new Map(layerCapacity);
  const assignments               = [];

  // 7. 卡点图案（仅 A）
  for (const chokePattern of effectiveChoke) {
    const quota = quotas.get(chokePattern) ?? 0;
    const plan = chokePlan(quota, capacityRemaining, config.chokeStartLayer, config.chokeLayerGap, maxLayerZ, rng);
    assignments.push({ patternId: chokePattern, layer: plan.layer, isChoke: true });
    deductCapacity(capacityRemaining, plan.layer);
  }

  // 8. 非卡点
  let formationRemaining = 0; for (const v of capacityRemaining.values()) formationRemaining += v;
  const nonChokeCount = nonChoke.length;
  let totalNonChokeQuota = 0; for (const p of nonChoke) totalNonChokeQuota += quotas.get(p) ?? 0;
  const denominator = formationRemaining + towerCount;
  const nonChokeFormationTotal = denominator > 0 ? Math.floor(totalNonChokeQuota * formationRemaining / denominator) : 0;
  const perPatternFormation = nonChokeCount > 0 ? Math.floor(nonChokeFormationTotal / nonChokeCount) : 0;
  const formationDistRemainder = nonChokeCount > 0 ? nonChokeFormationTotal % nonChokeCount : 0;

  const patternGroups = groupPatternsByStagePercents(nonChoke, config.stagePercents);

  const shuffledTower = towerTiles.slice();
  for (let i = shuffledTower.length - 1; i > 0; i--) { const j = rng.getRandom(i + 1); const t = shuffledTower[i] ; shuffledTower[i] = shuffledTower[j] ; shuffledTower[j] = t; }
  let towerTileIdx = 0;

  let nIdx = 0;
  for (let gIdx = 0; gIdx < patternGroups.length; gIdx++) {
    const consecutiveLayers = (gIdx < config.stageKindNums.length) ? config.stageKindNums[gIdx]  : 1;
    for (const normalPattern of patternGroups[gIdx] ) {
      const quota = quotas.get(normalPattern) ?? 0;
      const formationQuota = perPatternFormation + (nIdx < formationDistRemainder ? 1 : 0);
      const towerQuota = quota - formationQuota;
      for (let i = 0; i < towerQuota && towerTileIdx < shuffledTower.length; i++, towerTileIdx++) shuffledTower[towerTileIdx] .tileId = normalPattern;
      const plan = consecutivePlan(formationQuota, capacityRemaining, consecutiveLayers);
      if (plan.overflow > 0) {
        for (let i = 0; i < plan.overflow && towerTileIdx < shuffledTower.length; i++, towerTileIdx++) shuffledTower[towerTileIdx] .tileId = normalPattern;
      }
      assignments.push({ patternId: normalPattern, layer: plan.layer, isChoke: false });
      deductCapacity(capacityRemaining, plan.layer);
      nIdx++;
    }
  }

  if (nonChokeCount > 0) {
    for (; towerTileIdx < shuffledTower.length; towerTileIdx++) shuffledTower[towerTileIdx] .tileId = nonChoke[rng.getRandom(nonChokeCount)] ;
  }

  // 9. 层内随机赋值
  assignTiles(sorted, assignments, rng);

  // 10. 兜底修正
  const allTiles = [...sorted, ...shuffledTower];
  ensureAllTilesAssignedAndDivisible(allTiles, nonChoke, rng);
}

function deductCapacity(capacityRemaining                     , layer                     )       {
  for (const [z, v] of layer) if (capacityRemaining.has(z)) capacityRemaining.set(z, capacityRemaining.get(z)  - v);
}

// ---------- 入口 ----------

/**
 * 结构化生成牌面。tiles 须按与导出一致的顺序（随后按 idx 原序读回）；isTowerAt 判断 (x,y) 是否塔位。
 * 返回与输入同序的真实 tileId（A/B 替换；C..N 等标记保留）。iapCut 默认 0。
 */
export function generateFacesStructured(
  tiles                       , isTowerAt                                   ,
  dup          , pool                  , config            , iapCut = 0,
)           {
  const need = prepareAllNeedTileIds(dup, pool);
  const work             = tiles.map((t, i) => ({ x: t.x, y: t.y, z: t.z, tileId: t.tileId, tower: isTowerAt(t.x, t.y), idx: i }));

  const rng = new CustomRandom();
  rng.setSeed(getSeed(dup.seed));

  const tileTypeCountA = dup.a - iapCut;
  const effectiveTypeCount = Math.max(tileTypeCountA, dup.b);
  const shared = patternClassify(need, effectiveTypeCount, config.chokeTileNum, rng);

  // A 组
  const nonTowerA = work.filter(w => w.tileId === RANDOM_ID_A && !w.tower);
  const towerA = work.filter(w => w.tileId === RANDOM_ID_A && w.tower);
  allocatePatterns(nonTowerA, towerA, need, tileTypeCountA, config, rng, shared, 'A');

  // B 组
  const nonTowerB = work.filter(w => w.tileId === RANDOM_ID_B && !w.tower);
  const towerB = work.filter(w => w.tileId === RANDOM_ID_B && w.tower);
  allocatePatterns(nonTowerB, towerB, need, dup.b, config, rng, shared, 'B');

  return work.map(w => w.tileId);
}
