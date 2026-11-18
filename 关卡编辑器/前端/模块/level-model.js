/**
 * 关卡母版的编辑态数据与操作。纯逻辑、不碰 DOM，node --test 直接测。
 *
 * 摆放手感仿照 `feat-playbook-tilematch` 分支（半格制、同层重叠拒绝、换图案、层奇偶吸附），
 * 在它之上补主玩 LevelEditor 的能力：塔（4 方向）、debuff（只挂未遮挡的普通/blocker tile）、
 * 层升降、整体平移、分组。所有改动都返回新对象（不原地改），撤销栈存整份快照——母版只有几 KB。
 */
import {computeCover, isNormalTile, isBlockerTile, LEVEL_WIDTH, LEVEL_HEIGHT} from './logic.js';
import {NO_DEBUFF} from './level-bytes.js';

/** 编辑态 tile：落盘字段 + groupId（编辑态分组色标，不进 .bytes） */














export const NO_GROUP = -1;

export function newMaster(templateId = '', width = LEVEL_WIDTH, height = LEVEL_HEIGHT)         {
  return { templateId, version: 0, width, height, tiles: [], towers: [], debuffs: [] };
}

// ---------- 与 .bytes 互转 ----------

export function toSave(m        )            {
  return {
    version: m.version, templateId: m.templateId, duplicateId: 'm',
    width: m.width, height: m.height,
    tiles: m.tiles.map(t => ({ x: t.x, y: t.y, z: t.z, tileId: t.tileId, debuffIndex: t.debuffIndex })),
    towers: m.towers.map(t => ({ x: t.x, y: t.y, direction: t.direction })),
    debuffs: m.debuffs.map(d => ({ debuffId: d.debuffId })),
  };
}

export function fromSave(s           )         {
  return {
    templateId: s.templateId, version: s.version, width: s.width || LEVEL_WIDTH, height: s.height || LEVEL_HEIGHT,
    tiles: s.tiles.map((t          ) => ({ x: t.x, y: t.y, z: t.z, tileId: t.tileId, debuffIndex: t.debuffIndex, groupId: NO_GROUP })),
    towers: s.towers.map(t => ({ x: t.x, y: t.y, direction: t.direction })),
    debuffs: s.debuffs.map(d => ({ debuffId: d.debuffId })),
  };
}

// ---------- 摆放几何（半格制，仿 playbook model.ts） ----------

/** 同层两个 tile 的 2×2 半格有交集（x、y 都差不到 2） */
export function overlaps(a                                     , b                                     )          {
  return a.z === b.z && Math.abs(a.x - b.x) < 2 && Math.abs(a.y - b.y) < 2;
}

/** 第 z 层上盖住半格 (gx,gy) 的 tile 下标，没有返回 -1 */
export function tileIndexAt(tiles                   , z        , gx        , gy        )         {
  return tiles.findIndex(t => t.z === z && gx >= t.x && gx <= t.x + 1 && gy >= t.y && gy <= t.y + 1);
}

function mod2(n        )         { return ((n % 2) + 2) % 2; }

/** 整格吸附的奇偶：层上已有 tile 跟第一个对齐，空层按层号错开半格（与主玩阶梯摆法一致） */
export function layerParity(tiles                   , z        )                             {
  const first = tiles.find(t => t.z === z);
  if (first) return { px: mod2(first.x), py: mod2(first.y) };
  return { px: mod2(z), py: mod2(z) };
}

/** 鼠标半格坐标 → tile 锚点（tile 占 2×2，鼠标落中心，锚点=鼠标−1；整格模式再按层奇偶落到步长 2） */
export function snapAnchor(gx        , gy        , whole         , parity                            )                           {
  if (!whole) return { x: Math.round(gx - 1), y: Math.round(gy - 1) };
  return {
    x: Math.round((gx - 1 - parity.px) / 2) * 2 + parity.px,
    y: Math.round((gy - 1 - parity.py) / 2) * 2 + parity.py,
  };
}



/**
 * 在 (x,y,z) 放一个 tileId。同位置已有 → 换图案；与同层别的 tile 重叠 → 不放、blocked。
 * groupId 跟随当前选中分组。
 */
export function placeTile(m        , x        , y        , z        , tileId        , groupId = NO_GROUP)              {
  const same = m.tiles.findIndex(o => o.x === x && o.y === y && o.z === z);
  if (same >= 0) {
    if (m.tiles[same] .tileId === tileId) return { master: m, changed: false, blocked: false };
    const tiles = m.tiles.slice();
    tiles[same] = { ...tiles[same] , tileId };
    return { master: { ...m, tiles }, changed: true, blocked: false };
  }
  if (m.tiles.some(o => overlaps(o, { x, y, z }))) return { master: m, changed: false, blocked: true };
  const tile         = { x, y, z, tileId, debuffIndex: NO_DEBUFF, groupId };
  return { master: { ...m, tiles: [...m.tiles, tile] }, changed: true, blocked: false };
}

/**
 * 放置时自动计算 Z（与主玩 Helper.GetNextPositionZToAddForPositionXY 同口径）：
 * 新 tile 的 2×2 footprint 若与任何已有 tile 重叠，则叠到它们之上（maxZ+1），否则落在 minZ。
 * 因此点阵内每个点都能放 tile——永远不会被“重叠”挡住，而是自然往上堆叠成阶梯。
 */
export function nextStackZ(m        , x        , y        , minZ = 0)         {
  let max = -Infinity;
  for (const t of m.tiles) {
    if (Math.abs(t.x - x) < 2 && Math.abs(t.y - y) < 2 && t.z > max) max = t.z;
  }
  return max === -Infinity ? minZ : Math.max(minZ, max + 1);
}

/** 盖住 (gx,gy) 的最上层 tile 下标（跨所有 z 取 maxZ），没有返回 -1 */
export function topTileIndexAt(tiles                   , gx        , gy        )         {
  let best = -1, bestZ = -Infinity;
  for (let i = 0; i < tiles.length; i++) {
    const t = tiles[i] ;
    if (gx >= t.x && gx <= t.x + 1 && gy >= t.y && gy <= t.y + 1 && t.z > bestZ) { bestZ = t.z; best = i; }
  }
  return best;
}

/** 删掉盖住 (gx,gy) 的最上层 tile（与叠放对称：删栈顶），连带清理它挂的 debuff 引用 */
export function removeTopTileAt(m        , gx        , gy        )                                       {
  const i = topTileIndexAt(m.tiles, gx, gy);
  if (i < 0) return { master: m, changed: false };
  return { master: removeTileByIndex(m, i), changed: true };
}

/** 删掉盖住 (gx,gy) 的第 z 层 tile；连带清理它挂的 debuff 引用 */
export function removeTileAt(m        , z        , gx        , gy        )                                       {
  const i = tileIndexAt(m.tiles, z, gx, gy);
  if (i < 0) return { master: m, changed: false };
  return { master: removeTileByIndex(m, i), changed: true };
}

function removeTileByIndex(m        , i        )         {
  const victim = m.tiles[i] ;
  let next         = { ...m, tiles: m.tiles.filter((_, j) => j !== i) };
  if (victim.debuffIndex !== NO_DEBUFF) next = detachDebuffIfUnused(next, victim.debuffIndex);
  return next;
}

// ---------- 编辑规则：非塔位相邻层不能完全覆盖 ----------

/**
 * 关卡编辑规则：非塔坐标的相邻两层（z 相差 1）不允许同锚点 tile；塔坐标豁免。
 * tile 都是 2×2 同尺寸，唯有锚点完全相同时上层才会「完全覆盖」下层——必须错开半格摆成阶梯。
 * （金字塔式「一上压四下」的半格错位不算完全覆盖，仍合法。）
 *
 * 判断在 (x,y,z) 放一个 tile 是否会与相邻层同锚点 tile 完全重叠。
 */
export function fullyCoversAdjacent(m        , x        , y        , z        )          {
  if (towerAt(m.towers, x, y) >= 0) return false;
  return m.tiles.some(t => t.x === x && t.y === y && Math.abs(t.z - z) === 1);
}

/** 母版里违反该规则的 tile 下标集合（塔坐标除外，供高亮/校验用） */
export function fullCoverViolationIndices(m        )              {
  const towerAnchors = new Set(m.towers.map(t => `${t.x},${t.y}`));
  const byAnchor = new Map                                    ();
  m.tiles.forEach((t, i) => {
    const k = `${t.x},${t.y}`;
    if (towerAnchors.has(k)) return;
    let arr = byAnchor.get(k); if (!arr) { arr = []; byAnchor.set(k, arr); }
    arr.push({ z: t.z, i });
  });
  const bad = new Set        ();
  for (const arr of byAnchor.values()) {
    if (arr.length < 2) continue;
    const byZ = new Map(arr.map(e => [e.z, e.i]         ));
    for (const e of arr) { const up = byZ.get(e.z + 1); if (up !== undefined) { bad.add(e.i); bad.add(up); } }
  }
  return bad;
}

// ---------- 批量换 tile（选择态） ----------

/** tile 的唯一定位键（x,y,z 唯一确定一个 tile，placeTile 保证不重复） */
export function tileKey(t                                     )         {
  return `${t.x},${t.y},${t.z}`;
}

/** 把 keys 命中的所有 tile 的 tileId 批量换成 newId（选择态「一起换成同一个」），返回新母版 */
export function replaceTiles(m        , keys                     , newId        )                                       {
  let changed = false;
  const tiles = m.tiles.map(t => {
    if (keys.has(tileKey(t)) && t.tileId !== newId) { changed = true; return { ...t, tileId: newId }; }
    return t;
  });
  return changed ? { master: { ...m, tiles }, changed: true } : { master: m, changed: false };
}

// ---------- 塔 ----------

export function towerAt(towers                    , x        , y        )         {
  return towers.findIndex(t => t.x === x && t.y === y);
}

/** 放塔：一个位置至多一座，重复位置改方向 */
export function placeTower(m        , x        , y        , direction        )                                       {
  const i = towerAt(m.towers, x, y);
  if (i >= 0) {
    if (m.towers[i] .direction === direction) return { master: m, changed: false };
    const towers = m.towers.slice();
    towers[i] = { ...towers[i] , direction };
    return { master: { ...m, towers }, changed: true };
  }
  return { master: { ...m, towers: [...m.towers, { x, y, direction }] }, changed: true };
}

export function removeTowerAt(m        , x        , y        )                                       {
  const i = towerAt(m.towers, x, y);
  if (i < 0) return { master: m, changed: false };
  return { master: { ...m, towers: m.towers.filter((_, j) => j !== i) }, changed: true };
}

// ---------- debuff ----------

/** 哪些 tile 当前被遮挡（供 debuff 规则与显示用） */
export function coverFlags(m        )            {
  return computeCover(m.tiles, m.tiles.map(() => false));
}

/** tile 能挂 debuff：普通/blocker 型、未遮挡、当前没挂 debuff（主玩 Helper 规则） */
export function canDebuffTile(m        , index        , cover = coverFlags(m))          {
  const t = m.tiles[index];
  if (!t) return false;
  if (cover[index]) return false;
  if (t.debuffIndex !== NO_DEBUFF) return false;
  return isNormalTile(t.tileId) || isBlockerTile(t.tileId);
}

/**
 * 给 (gx,gy) 处最上层、可挂 debuff 的 tile 挂一个 debuffId。
 * 新建一条 DebuffData，并把该 tile 的 debuffIndex 指过去。
 */
export function addDebuffAt(m        , gx        , gy        , debuffId        )                                       {
  const cover = coverFlags(m);
  // 取盖住此格、未遮挡、可挂的最上层 tile
  let best = -1, bestZ = -Infinity;
  for (let i = 0; i < m.tiles.length; i++) {
    const t = m.tiles[i] ;
    if (gx < t.x || gx > t.x + 1 || gy < t.y || gy > t.y + 1) continue;
    if (!canDebuffTile(m, i, cover)) continue;
    if (t.z > bestZ) { bestZ = t.z; best = i; }
  }
  if (best < 0) return { master: m, changed: false };
  const debuffs = [...m.debuffs, { debuffId }];
  const tiles = m.tiles.slice();
  tiles[best] = { ...tiles[best] , debuffIndex: debuffs.length - 1 };
  return { master: { ...m, tiles, debuffs }, changed: true };
}

/** 删掉 (gx,gy) 最上层 tile 上挂的 debuff（不删 tile） */
export function removeDebuffAt(m        , z        , gx        , gy        )                                       {
  const i = tileIndexAt(m.tiles, z, gx, gy);
  if (i < 0 || m.tiles[i] .debuffIndex === NO_DEBUFF) return { master: m, changed: false };
  const idx = m.tiles[i] .debuffIndex;
  const tiles = m.tiles.slice();
  tiles[i] = { ...tiles[i] , debuffIndex: NO_DEBUFF };
  return { master: detachDebuffIfUnused({ ...m, tiles }, idx), changed: true };
}

/** 没有 tile 再引用某 debuff 下标时，删掉它并把更大的下标前移一位 */
function detachDebuffIfUnused(m        , idx        )         {
  if (m.tiles.some(t => t.debuffIndex === idx)) return m;
  const debuffs = m.debuffs.filter((_, j) => j !== idx);
  const tiles = m.tiles.map(t => t.debuffIndex > idx ? { ...t, debuffIndex: t.debuffIndex - 1 } : t);
  return { ...m, tiles, debuffs };
}

// ---------- 层 / 平移 ----------

/** 整体层升（所有 tile z+1） */
export function raiseAll(m        )         {
  return { ...m, tiles: m.tiles.map(t => ({ ...t, z: t.z + 1 })) };
}
/** 整体层降（所有 tile z-1）；有 tile 在 z=0 时拒绝，返回原样 */
export function lowerAll(m        )                                       {
  if (m.tiles.some(t => t.z <= 0)) return { master: m, changed: false };
  return { master: { ...m, tiles: m.tiles.map(t => ({ ...t, z: t.z - 1 })) }, changed: true };
}

/** 包围盒（半格坐标），空母版返回 null */
export function bbox(m        )                                                                    {
  if (m.tiles.length === 0 && m.towers.length === 0) return null;
  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  const pts                             = [...m.tiles.map(t => ({ x: t.x + 1, y: t.y + 1 })), ...m.towers];
  for (const t of pts) { minX = Math.min(minX, t.x); maxX = Math.max(maxX, t.x); minY = Math.min(minY, t.y); maxY = Math.max(maxY, t.y); }
  return { minX, maxX, minY, maxY };
}

/** 整体平移 dx,dy（tile 与塔一起）。grid=可摆放区半宽/高时，越界则拒绝 */
export function translateAll(m        , dx        , dy        )                                       {
  if (dx === 0 && dy === 0) return { master: m, changed: false };
  const tiles = m.tiles.map(t => ({ ...t, x: t.x + dx, y: t.y + dy }));
  const towers = m.towers.map(t => ({ ...t, x: t.x + dx, y: t.y + dy }));
  return { master: { ...m, tiles, towers }, changed: true };
}

// ---------- 分组（编辑态，不进 .bytes） ----------

/** 切出只含某分组 tile 的子母版（存为新模板用）；塔/debuff 若被该组 tile 引用则一并带上 */
export function subMasterOfGroup(m        , groupId        , templateId        )         {
  const tiles = m.tiles.filter(t => t.groupId === groupId);
  // 重建 debuff 列表：只保留被选中 tile 引用的，重排下标
  const usedDebuff = new Map                ();
  const debuffs             = [];
  const outTiles = tiles.map(t => {
    if (t.debuffIndex === NO_DEBUFF) return { ...t };
    let ni = usedDebuff.get(t.debuffIndex);
    if (ni === undefined) { ni = debuffs.length; debuffs.push(m.debuffs[t.debuffIndex] ?? { debuffId: 0 }); usedDebuff.set(t.debuffIndex, ni); }
    return { ...t, debuffIndex: ni };
  });
  // 塔：位置上有该组 tile 的就带上
  const occupied = new Set(tiles.map(t => `${t.x},${t.y}`));
  const towers = m.towers.filter(w => occupied.has(`${w.x},${w.y}`));
  return { templateId, version: 0, width: m.width, height: m.height, tiles: outTiles, towers, debuffs };
}

/** 当前用到的分组 id（升序，不含 NO_GROUP） */
export function usedGroups(m        )           {
  return [...new Set(m.tiles.map(t => t.groupId).filter(g => g !== NO_GROUP))].sort((a, b) => a - b);
}

// ---------- 统计 ----------



export function masterStats(m        )              {
  const counts = new Map                ();
  for (const t of m.tiles) counts.set(t.tileId, (counts.get(t.tileId) ?? 0) + 1);
  const randomSlots = m.tiles.filter(t => t.tileId <= -10 && t.tileId >= -23).length;
  return {
    tiles: m.tiles.length,
    layers: new Set(m.tiles.map(t => t.z)).size,
    towers: m.towers.length,
    debuffs: m.debuffs.length,
    randomSlots,
    counts: [...counts].sort((a, b) => a[0] - b[0]),
  };
}
