/**
 * 摆放几何 + 遮挡 + 通关推演的「权威逻辑」。
 *
 * 直接搬自 `feat-playbook-tilematch` 分支的组件逻辑
 * `BuildTools/playbook-component-skill/tileMatch/logic.ts`（占用格、遮挡、随机分配、
 * 背包落点、通关推演都与主玩一致）。关卡编辑器的 tile 摆放手感与右栏即时试玩都仿照它。
 *
 * 与原版的唯一区别：这里的 tile 用真实 `tileId`（1000+ 普通 / 10000+ blocker / -10..-23 随机标记），
 * 不是组件里的 0–9 笔刷号；凡是按「种类」判等的地方（消除、背包）都拿 tileId 当键，几何/遮挡只看 x/y/z。
 * 纯逻辑、不碰 DOM，node --test 可直接测。
 */

/** 一个 tile 的摆放信息。坐标是主玩的半格制：x/y 以半个 tile 为 1 格，z 从底往上 */






/**
 * tile 的占用格：锚在 (x,y)，占 {0,1}×{0,1} 共 4 格。
 * 对应主玩 Helper.GetOccupiedGrids(size(1,1)) 生成的 x∈[0,1], y∈[0,1]。
 */
export function occupiedGrids(x        , y        )                     {
  return [[x, y], [x + 1, y], [x, y + 1], [x + 1, y + 1]];
}

/**
 * 棋盘格距 ÷ 底板图宽，抄主玩：横纵格距都 = 底板实心宽 148px（tile_bg.png 整图 162×180）→ 148/162。
 * 横向实心部分贴边、羽化边互压，纵向上下 tile 重叠。层与层之间不位移（靠绘制顺序区分层级）。
 */
export const BOARD_STEP_RATIO = 148 / 162;

/** 网格尺寸（主玩 Values.cs：宽 17 高 19） */
export const LEVEL_WIDTH = 17;
export const LEVEL_HEIGHT = 19;

// ---------- tileId 分段（主玩 TileTypeValues.cs，序列化相关、不可乱改） ----------

/** 随机标记 RandomIdA..N = -10..-23；开局时从 A/B 类型池里抽真实 tileId 填进去 */
export const RANDOM_ID_A = -10;
export const RANDOM_ID_N = -23;
export const SKILL_ID_BEGIN = 0, SKILL_ID_END = 999;
export const NORMAL_ID_BEGIN = 1000, NORMAL_ID_END = 9999;
export const BLOCKER_ID_BEGIN = 10000, BLOCKER_ID_END = 19999;

/** 随机标记（-10..-23）：摆的是「这里开局随机出一种」而非定死某图案 */
export function isRandomMarker(tileId        )          {
  return tileId <= RANDOM_ID_A && tileId >= RANDOM_ID_N;
}
export function isSkillTile(tileId        )          {
  return tileId >= SKILL_ID_BEGIN && tileId <= SKILL_ID_END;
}
export function isNormalTile(tileId        )          {
  return tileId >= NORMAL_ID_BEGIN && tileId <= NORMAL_ID_END;
}
export function isBlockerTile(tileId        )          {
  return tileId >= BLOCKER_ID_BEGIN && tileId <= BLOCKER_ID_END;
}

// ---------- 遮挡 ----------

/**
 * 逐个判定是否被遮挡：任一占用格上存在更高 z 的 tile 即为被遮挡、不可点。
 * 与主玩 Helper._doChange 同式（那边是增量更新，这里几十个 tile 全量重算就够）。
 * 格子 key 用数字避免拼字符串：|y| < 32768 时 x*65536+y 一一对应（负数也成立）。
 */
export function computeCover(tiles                    , removed                    )            {
  const maxZ = new Map                ();
  for (let i = 0; i < tiles.length; i++) {
    if (removed[i]) continue;
    const t = tiles[i] ;
    for (const [gx, gy] of occupiedGrids(t.x, t.y)) {
      const k = gx * 65536 + gy;
      const cur = maxZ.get(k);
      if (cur === undefined || t.z > cur) maxZ.set(k, t.z);
    }
  }
  return tiles.map((t, i) => {
    if (removed[i]) return false;
    for (const [gx, gy] of occupiedGrids(t.x, t.y)) {
      if ((maxZ.get(gx * 65536 + gy) ?? t.z) > t.z) return true;
    }
    return false;
  });
}

// ---------- 随机分配（D3 派生的等价实现） ----------

/** Fisher–Yates 原地洗牌 */
export function shuffle   (a     , rand              )       {
  for (let i = a.length - 1; i > 0; i--) {
    const j = Math.floor(rand() * (i + 1));
    const tmp = a[i] ; a[i] = a[j] ; a[j] = tmp;
  }
}

/**
 * 给 count 个随机位分配「类型键」（pool 里的下标，调用方再映射到真实 tileId），返回与随机位同序的键。
 *
 * 1. 有效种类 K = min(kinds, poolSize)，从池里无放回抽 K 张；
 * 2. 3 个一组分组，第 i 组给第 i % K 种 —— 组数够时每种至少一组；
 * 3. 展开成标签后整体洗牌（不是只洗组序：否则同组 3 张总挨在一起，一眼看穿）。
 *
 * count 不是 3 的倍数时最后一组不满，照样填——由调用方的诊断去报。poolSize 为 0 时返回空数组。
 * 对齐组件 assignRandomTypes；这是 D3「类型逐级去重 + 洗牌」的等价确定性预览（同 rand 同结果）。
 */
export function assignRandomTypes(
  count        , poolSize        , kinds        , rand               = Math.random,
)           {
  if (poolSize <= 0 || count <= 0) return [];
  const k = Math.max(1, Math.min(Math.floor(kinds), poolSize));
  const pool = Array.from({ length: poolSize }, (_, i) => i);
  shuffle(pool, rand);
  const picked = pool.slice(0, k);
  const labels           = [];
  for (let i = 0; i < count; i++) labels.push(picked[Math.floor(i / 3) % k] );
  shuffle(labels, rand);
  return labels;
}

/**
 * 确定性随机：MWC/xorshift 变体（mulberry32）。同一个 seed → 同一串数，供 D3 派生「同种子同结果」。
 * 不要求与 Unity CustomRandom 逐 bit 一致（已与用户确认用等价确定性预览）。
 */
export function makeRng(seed        )               {
  let s = seed >>> 0;
  return function () {
    s = (s + 0x6D2B79F5) | 0;
    let t = Math.imul(s ^ (s >>> 15), 1 | s);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

// ---------- 背包 / 消除 ----------

/** 背包里首个达到 matchCount 个的类型，无则 null */
export function matchedType(trayTypes                   , matchCount        )                {
  const count = new Map                ();
  for (const t of trayTypes) {
    const n = (count.get(t) ?? 0) + 1;
    count.set(t, n);
    if (n >= matchCount) return t;
  }
  return null;
}

/**
 * 新 tile 进背包的落点：插在**最后一个同类的右边**；没有同类则插末尾。
 * 对齐主玩 BagView.AttachTileViewAndData —— 不是「按类型全量排序」，不同类之间按进包顺序保留。
 */
export function bagInsertIndex(bagTypes                   , type        , matchCount        )         {
  let sameCount = 0;
  let insert = bagTypes.length;
  for (let i = 0; i < bagTypes.length; i++) {
    if (bagTypes[i] !== type) continue;
    insert = i + 1;
    sameCount += 1;
  }
  if (sameCount === matchCount) insert = bagTypes.length;
  return insert;
}

/** 一种 type 的计数不是 matchCount 的倍数，永远凑不齐一组、无法被消除 */


/** 找出计数不是 matchCount 倍数的 type（这种 tile 摸到最后总有落单的，关卡赢不了） */
export function findUnclearableTypes(types                   , matchCount        )                    {
  const counts = new Map                ();
  for (const t of types) counts.set(t, (counts.get(t) ?? 0) + 1);
  const out                    = [];
  for (const [type, count] of counts) if (count % matchCount !== 0) out.push({ type, count });
  return out.sort((a, b) => a.type - b.type);
}

// ---------- 通关推演 ----------

/** 带类型的推演 tile：几何 + 开局已定下来的消除类型键 */


/**
 * 纯逻辑推演一局，用于验证布局可解性。玩法规则的参考实现，与主玩共用遮挡/落点/消除原语。
 * 只演算**严格串行**玩法（每次落子完整落位结算完才点下一个），不建模多 tile 同时在飞。
 * 结算顺序与运行时一致：落位 → 先消除 → 再判满。对齐组件 simulate。
 */
export function simulate(
  tiles                     ,
  capacity        ,
  matchCount        ,
  pick                                 ,
)                                                             {
  const removed = tiles.map(() => false);
  let bag           = [];
  let maxBagAfterSettle = 0;

  for (let guard = 0; guard <= tiles.length; guard++) {
    if (removed.every(r => r) && bag.length === 0) {
      return { maxBagAfterSettle, won: true, lost: false };
    }
    const cover = computeCover(tiles, removed);
    const clickable           = [];
    for (let i = 0; i < tiles.length; i++) if (!removed[i] && !cover[i]) clickable.push(i);
    if (clickable.length === 0 || bag.length >= capacity) {
      return { maxBagAfterSettle, won: false, lost: true };
    }

    const idx = pick(clickable);
    removed[idx] = true;
    const type = tiles[idx] .type;
    bag.splice(bagInsertIndex(bag, type, matchCount), 0, type);

    const mt = matchedType(bag, matchCount);
    if (mt !== null) {
      let left = matchCount;
      bag = bag.filter(t => (t === mt && left-- > 0 ? false : true));
    }
    if (bag.length > maxBagAfterSettle) maxBagAfterSettle = bag.length;
    if (bag.length >= capacity) return { maxBagAfterSettle, won: false, lost: true };
  }
  return { maxBagAfterSettle, won: false, lost: true };
}

// ---------- 局内道具纯逻辑（移植 tile 主玩道具二期的「功能」，不含表现；无 DOM，可 node --test） ----------
//
// 对齐来源（tile 主仓 ulink/TileEnv/TileMatch/GamePlayer/Data/Handlers）：
//   撤回 Undo(502)  PropUndoClickHandler        弹簧 Spring(505) PropSpringClickHandler（多格撤回）
//   磁铁 Magneto(503) MagnetoHelper 新实现       抓钩 Crane(506)  CraneHelper 新实现（四级优先级，这里取前两级+表面最大化）
//   洗牌 Shuffle(515) ShuffleHelper 新实现（随机重排 + 一定帮忙）
// 一组 = MATCH_COUNT 张同图案（主玩 BagService.EliminateSameCount=3）。

/** 背包/整理区里的一张牌：type=消除键(tileId)，src=来自哪张牌面 tile(可回溯)，seq=入场流水号(撤回取最新)。 */

/**
 * 道具操作的局面。三个区域对齐主玩：
 *   牌面(geo/types/removed) —点击/抓钩/磁铁→ 离开牌面(removed=true)
 *   整理区(stage)          —撤回/弹簧从背包退到这；抓钩从牌面抓到这；点击整理区的牌→背包
 *   背包(bag)              —凑 MATCH_COUNT 张同图案即消除；满 capacity 判负
 * geo=几何(固定)，types=图案(洗牌会改写，与 geo 同序)，removed=是否已离开牌面。
 */









export const MATCH_COUNT = 3;

/** 背包各图案张数。 */
export function propBagCounts(bag                    )                      {
  const m = new Map                ();
  for (const e of bag) m.set(e.type, (m.get(e.type) ?? 0) + 1);
  return m;
}

/** 把一张牌放进背包（落点同主玩 BagView：插在最后一个同类之后）。 */
function insertIntoBag(st           , item         )       {
  const at = bagInsertIndex(st.bag.map(e => e.type), item.type, MATCH_COUNT);
  st.bag.splice(at, 0, item);
}

/** 把牌面第 i 张放进背包。 */
export function propPush(st           , i        )       {
  st.removed[i] = true;
  insertIntoBag(st, { type: st.types[i] , src: i, seq: st.seq++ });
}

/** 点整理区第 k 张 → 进背包（刷新 seq，使其成为背包里最新的一张，便于再撤回）。返回是否成功。 */
export function propStageToBag(st           , k        )          {
  const item = st.stage[k];
  if (!item) return false;
  st.stage.splice(k, 1);
  item.seq = st.seq++;
  insertIntoBag(st, item);
  propResolve(st);
  return true;
}

/** 背包任一图案凑够 MATCH_COUNT 张就消除；循环到没有可消（道具一次吸多张可能连消）。 */
export function propResolve(st           )       {
  for (;;) {
    const mt = matchedType(st.bag.map(e => e.type), MATCH_COUNT);
    if (mt === null) break;
    let left = MATCH_COUNT;
    st.bag = st.bag.filter(e => !(e.type === mt && left-- > 0));
  }
}

/** 假设抓掉第 i 张后，能新翻开（从被遮挡变不遮挡）多少张 tile。 */
export function propRevealCount(st           , i        )         {
  const before = computeCover(st.geo, st.removed);
  const tmp = st.removed.slice(); tmp[i] = true;
  const after = computeCover(st.geo, tmp);
  let n = 0;
  for (let j = 0; j < st.geo.length; j++) if (!tmp[j] && before[j] && !after[j]) n++;
  return n;
}

/** 撤回(502)/弹簧(505)：把背包里最近入包的 count 张退回整理区。返回实际退回数。 */
export function propUndo(st           , count        )         {
  let done = 0;
  for (let n = 0; n < count && st.bag.length; n++) {
    let pick = 0;
    for (let i = 1; i < st.bag.length; i++) if (st.bag[i] .seq > st.bag[pick] .seq) pick = i;
    const e = st.bag.splice(pick, 1)[0] ;
    st.stage.push(e);  // 退到整理区（对齐主玩 StageArea），不直接回牌面
    done++;
  }
  return done;
}

/** 磁铁(503)：优先补齐背包里已成对的那组，从牌面/整理区吸同图案进背包并消除。返回吸入数。 */
export function propMagneto(st           )         {
  const counts = propBagCounts(st.bag);
  let target                = null, have = -1;
  for (const [t, c] of counts) { if (c >= MATCH_COUNT) continue; if (c > have) { have = c; target = t; } }
  let need        ;
  if (target === null) {
    // 背包空：从牌面挑数量最多的普通图案起一组
    const avail = new Map                ();
    for (let i = 0; i < st.geo.length; i++) if (!st.removed[i] && isNormalTile(st.types[i] )) {
      const t = st.types[i] ; avail.set(t, (avail.get(t) ?? 0) + 1);
    }
    let bestC = 0;
    for (const [t, c] of avail) if (c > bestC) { bestC = c; target = t; }
    if (target === null) return 0;
    need = Math.min(MATCH_COUNT, bestC);
  } else {
    need = MATCH_COUNT - have;
  }
  const cover = computeCover(st.geo, st.removed);
  let pulled = 0;
  // pass0 牌面表面 → pass1 牌面被遮挡 → pass2 整理区（对齐主玩：先牌面高亮/全面，再整理区）
  for (let pass = 0; pass < 3 && pulled < need; pass++) {
    if (pass < 2) {
      for (let i = 0; i < st.geo.length && pulled < need; i++) {
        if (st.removed[i] || st.types[i] !== target) continue;
        if (pass === 0 && cover[i]) continue;
        propPush(st, i); pulled++;
      }
    } else {
      for (let k = st.stage.length - 1; k >= 0 && pulled < need; k--) {
        if (st.stage[k] .type !== target) continue;
        const item = st.stage.splice(k, 1)[0] ;
        item.seq = st.seq++;
        insertIntoBag(st, item); pulled++;
      }
    }
  }
  if (pulled) propResolve(st);
  return pulled;
}

/** 抓钩(506)：抓 count 张表面 tile 到整理区；优先抓背包用得上的图案，其次翻开被盖最多的。返回抓取数。 */
export function propCrane(st           , count        )         {
  let grabbed = 0;
  for (let n = 0; n < count; n++) {
    const cover = computeCover(st.geo, st.removed);
    const counts = propBagCounts(st.bag);
    const surf           = [];
    for (let i = 0; i < st.geo.length; i++) if (!st.removed[i] && !cover[i] && isNormalTile(st.types[i] )) surf.push(i);
    if (!surf.length) break;
    // 1) 背包已有该图案但没凑齐(1~2 张) → 抓来放整理区，玩家随后可点进背包凑组
    let pick = surf.find(i => { const c = counts.get(st.types[i] ) ?? 0; return c >= 1 && c < MATCH_COUNT; }) ?? -1;
    // 2) 表面最大化：抓掉后翻开最多被盖 tile
    if (pick < 0) {
      let best = -1, bestReveal = -1;
      for (const i of surf) { const r = propRevealCount(st, i); if (r > bestReveal) { bestReveal = r; best = i; } }
      pick = best;
    }
    if (pick < 0) break;
    st.removed[pick] = true;
    st.stage.push({ type: st.types[pick] , src: pick, seq: st.seq++ });  // 抓进整理区（对齐主玩），不直接进背包
    grabbed++;
  }
  return grabbed;
}

/** 洗牌(515)：未消除 tile 的图案原位随机重排(数量守恒→可解性不变)；再「一定帮忙」把背包成对图案缺的那张换到表面。 */
export function propShuffle(st           , rand              )       {
  const idxs           = [];
  for (let i = 0; i < st.geo.length; i++) if (!st.removed[i]) idxs.push(i);
  if (idxs.length < 2) return;
  const pool = idxs.map(i => st.types[i] );
  shuffle(pool, rand);
  idxs.forEach((i, k) => { st.types[i] = pool[k] ; });
  // 一定帮忙：让背包里已成对(差 1 张)的图案，缺的那张出现在表面可点层
  const counts = propBagCounts(st.bag);
  const cover = computeCover(st.geo, st.removed);  // 遮挡只看几何+removed，洗牌不改
  const surface = idxs.filter(i => !cover[i]);
  const locked = new Set        ();
  for (const [t, c] of counts) {
    if (c !== MATCH_COUNT - 1) continue;
    if (surface.some(i => !locked.has(i) && st.types[i] === t)) continue;  // 表面已有
    const slot = surface.find(i => !locked.has(i));
    const donor = idxs.find(i => cover[i] && st.types[i] === t);
    if (slot === undefined || donor === undefined) continue;
    const old = st.types[slot] ;
    st.types[slot] = t;
    st.types[donor] = old;
    locked.add(slot);
  }
}
