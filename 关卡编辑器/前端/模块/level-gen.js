/**
 * 「阵型 + 副本 → 牌面」生成器 —— 逐 bit 对齐 tile 工程 main 分支主玩的**原流程**
 * （`randomSeed_groupID` 与 `StrategyGroupId` 两列均空时走的分支）。
 *
 * 源：`ulink/TileEnv/TileMatch/Data/LevelGenerator/LevelDupGenerator.cs`
 *     `ulink/TileEnv/TileMatch/Data/LevelGenerator/CustomRandom.cs`
 *     `ulink/TileEnv/TileMatch/Config/TileConfigManager.cs`（BaseTiles）
 *     `Tile/Assets/Resources/RemoteCSV/Config/.../config_client_level_tile.csv`（类型池按关卡分段）
 *
 * 覆盖：PrepareAllNeedTileIds（三级选池）+ RandomizeTiles（A/B 均分 + 洗牌 3 轮）。
 * 不含：种子组结构化流程、策略组、DDA、IAP 保护（iapCut 默认 0）、低 ID 标记 C..N
 *       （主玩用无种子 System.Random，不可复现；本模块给确定性占位，另行标注）。
 * 纯逻辑、不碰 DOM，node --test 可直接测。
 */

// ---------- CustomRandom：Marsaglia MWC，逐 bit 复刻 CustomRandom.cs ----------
export class CustomRandom {
          mW = 521288629 >>> 0;
          mZ = 362436069 >>> 0; // ConstMz

  /** SetSeed(seed)：mW=seed，mZ=ConstMz */
  setSeed(seed        )       {
    this.mW = seed >>> 0;
    this.mZ = 362436069 >>> 0;
  }

  /** GetRandom(max)：返回 [0,max)。全程 uint32 语义。 */
  getRandom(max        )         {
    // _mZ = 36969 * (_mZ & 65535) + (_mZ >> 16);
    this.mZ = (36969 * (this.mZ & 0xffff) + (this.mZ >>> 16)) >>> 0;
    // _mW = 18000 * (_mW & 65535) + (_mW >> 16);
    this.mW = (18000 * (this.mW & 0xffff) + (this.mW >>> 16)) >>> 0;
    // (int)(((_mZ << 16) + _mW) % (uint)max)；用 *65536 避免 JS <<16 的 int32 溢出
    const combined = ((this.mZ * 65536) + this.mW) >>> 0;
    return combined % max;
  }
}

/** GetSeed(seed) = (uint)(1411242022 + seed) —— 请勿修改的随机常数（LevelDupGenerator.cs） */
export function getSeed(seed        )         {
  return (1411242022 + seed) >>> 0;
}

// ---------- 常量（TileTypeValues.cs / TileConfigManager.cs / BagService） ----------
export const RANDOM_ID_A = -10;
export const RANDOM_ID_B = -11;
export const ELIMINATE_SAME_COUNT = 3; // BagService.EliminateSameCount
const MIN_TILE_TYPE_COUNT = 1;

/**
 * BaseTiles：TileConfigManager 硬编码的 19 个基础图案（兜底池）。
 * 顺序取自 `GetNormalBaseTileModels()` = 过滤 `_normalTiles`(Resources.LoadAll 顺序) ∩ BaseTiles。
 * 此处按硬编码列表（升序）假定 Resources 为 id 升序；仅当选池穿透到第三级（兜底）才被消费，
 * 若金标准在该情形不符需另导出真实加载顺序。
 */
export const BASE_TILES                    = [
  1000, 1001, 1002, 1003, 1004, 1005, 1006, 1007, 1008, 1009,
  1011, 1012, 1013, 1014, 1300, 1301, 1302, 1304, 1313,
];

// ---------- 类型池配置（config_client_level_tile.csv） ----------


/** 解析 config_client_level_tile.csv → 按 startLevel 升序的池分段 */
export function parseLevelTilePools(csv        )                     {
  const lines = csv.split(/\r?\n/).filter(l => l.trim().length > 0);
  const rows                     = [];
  // 前三行是表头（列名 / 类型 / 中文说明）
  for (let i = 3; i < lines.length; i++) {
    const cols = lines[i] .split(',');
    if (cols.length < 4) continue;
    const startLevel = parseInt(cols[1] , 10);
    if (!Number.isFinite(startLevel)) continue;
    const parseList = (s        )           =>
      (s ?? '').split('|').map(x => parseInt(x, 10)).filter(n => Number.isFinite(n));
    rows.push({ startLevel, list1: parseList(cols[2] ), list2: parseList(cols[3] ) });
  }
  rows.sort((a, b) => a.startLevel - b.startLevel);
  return rows;
}

/** GetCurrentLevelTileRawMeta：startLevel <= level 且差值最小的那段（左闭右开） */
export function pickPoolRow(rows                             , level        )                          {
  let best                          = null;
  let min = Number.POSITIVE_INFINITY;
  for (const r of rows) {
    const d = level - r.startLevel;
    if (d >= 0 && d < min) { min = d; best = r; }
  }
  return best ?? (rows.length ? rows[0]  : null);
}

// ---------- 主流程 ----------
                                                                  // TileTypeCountA / RandomSeed / TileTypeCountB
                                             // 只需 tileId；顺序必须是 TileDatas(Z→Y→X) 顺序

function removeFirst(arr          , val        )       {
  const i = arr.indexOf(val);
  if (i >= 0) arr.splice(i, 1);
}

/**
 * PrepareAllNeedTileIds：三级选池，选出 max(A,B) 个真实 tileId（种子 GetSeed(RandomSeed)）。
 * list1 命中 → 同时从 list2、normals 去重；list2 命中 → 从 normals 去重；仍不够从 normals(BaseTiles) 补。
 */
export function prepareAllNeedTileIds(dup          , pool                  )           {
  let max = Math.max(dup.a, dup.b);
  if (max < MIN_TILE_TYPE_COUNT) max = MIN_TILE_TYPE_COUNT;

  const list1 = pool.list1.slice();
  const list2 = pool.list2.slice();
  const normals = BASE_TILES.slice();
  const rng = new CustomRandom();
  rng.setSeed(getSeed(dup.seed));

  const need           = [];
  while (need.length < max && list1.length > 0) {
    const i = rng.getRandom(list1.length);
    const id = list1[i] ; list1.splice(i, 1);
    removeFirst(list2, id); removeFirst(normals, id);
    need.push(id);
  }
  while (need.length < max && list2.length > 0) {
    const i = rng.getRandom(list2.length);
    const id = list2[i] ; list2.splice(i, 1);
    removeFirst(normals, id);
    need.push(id);
  }
  while (need.length < max && normals.length > 0) {
    const i = rng.getRandom(normals.length);
    const id = normals[i] ; normals.splice(i, 1);
    need.push(id);
  }
  return need;
}

/** ShuffleList：CustomRandom(GetSeed(seed)) 洗 3 轮，原地。 */
function shuffleList(list          , seed        )       {
  const rng = new CustomRandom();
  rng.setSeed(getSeed(seed));
  for (let i = 0; i < 3; i++) {
    for (let j = 0; j < list.length; j++) {
      const temp = list[j] ;
      const ri = rng.getRandom(list.length);
      list[j] = list[ri] ;
      list[ri] = temp;
    }
  }
}

/**
 * GetRandomTileTypeList：按权重（全 100）把 needGroupCount 组均分到 segTypes（已按 id 降序），
 * 每组展开 3 张，再洗牌。返回长度 = needGroupCount*3 的类型序列；配置不足时返回空（与主玩 early-return 对齐）。
 */
export function getRandomTileTypeList(needGroupCount        , seed        , segTypes          )           {
  const totalWeight = 100 * segTypes.length;
  const groupDict = new Map                (); // 插入序 = segTypes 序（对齐 C# Dictionary 无删除时的枚举序）
  let totalGroupCount = 0;
  for (let i = 0; i < segTypes.length; i++) {
    const weight = 100;
    // Mathf.FloorToInt(((float)weight / totalWeight) * needGroupCount) —— 用 fround 复刻 float32
    let cur = Math.floor(Math.fround(Math.fround(Math.fround(weight) / totalWeight) * needGroupCount));
    if (cur === 0) cur = 1;
    if (totalGroupCount + cur > needGroupCount) cur = needGroupCount - totalGroupCount;
    if (cur <= 0) break;
    groupDict.set(segTypes[i] , cur);
    totalGroupCount += cur;
  }

  const leftGroupCount = needGroupCount - totalGroupCount;
  if (segTypes.length < leftGroupCount) return [];
  if (leftGroupCount > 0) {
    for (let i = 0; i < leftGroupCount; i++) {
      groupDict.set(segTypes[i] , (groupDict.get(segTypes[i] ) ?? 0) + 1);
    }
  }

  const ret           = [];
  for (const [key, val] of groupDict) {
    const count = val * ELIMINATE_SAME_COUNT;
    for (let j = 0; j < count; j++) ret.push(key);
  }
  shuffleList(ret, seed);
  return ret;
}

/**
 * RandomizeTiles：给某组（A 或 B）占位牌按 TileDatas 顺序赋真实类型。
 * typeCount = 该组种类数（A 已减 iapCut）。原地写 result。
 */
function randomizeGroup(result          , markerIdx          , typeCount        , need          , seed        )       {
  if (markerIdx.length === 0) return;
  let tileTypeCount = typeCount;
  if (tileTypeCount < MIN_TILE_TYPE_COUNT) tileTypeCount = MIN_TILE_TYPE_COUNT;

  const groupCount = Math.floor(markerIdx.length / ELIMINATE_SAME_COUNT);

  // PrepareWeightThing：池不足 typeCount 时主玩不重建权重表 → 本组不赋值（保持占位）
  if (need.length < tileTypeCount) return;
  // 权重全 100 → 排序等效 id 降序
  const segTypes = need.slice(0, tileTypeCount).sort((a, b) => b - a);

  const randomized = getRandomTileTypeList(groupCount, seed, segTypes);
  if (randomized.length < markerIdx.length) return; // 对齐主玩 "randomizedTypes.Count < tiles.Count → return"

  for (let i = 0; i < markerIdx.length; i++) result[markerIdx[i] ] = randomized[i] ;
}


                                                    

              



/**
 * 主入口：阵型（tiles，须为 TileDatas Z→Y→X 顺序）+ 副本（dup）+ 关卡池（pool）→ 牌面。
 * iapCut 默认 0（无 IAP 保护）。只处理 RandomIdA/B；其余标记原样返回由上层决定如何显示。
 */
export function generateFaces(tiles                    , dup          , pool                  , iapCut = 0)            {
  const need = prepareAllNeedTileIds(dup, pool);
  const result = tiles.map(t => t.tileId);

  const idxA           = [];
  const idxB           = [];
  tiles.forEach((t, i) => {
    if (t.tileId === RANDOM_ID_A) idxA.push(i);
    else if (t.tileId === RANDOM_ID_B) idxB.push(i);
  });

  randomizeGroup(result, idxA, dup.a - iapCut, need, dup.seed);
  randomizeGroup(result, idxB, dup.b, need, dup.seed);

  return { tileIds: result, need };
}
