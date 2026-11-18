/**
 * 笔刷调色板：从 config_client_tile.csv 读真实 tile 目录，按主玩 TileTypeValues 的 id 分段归类，
 * 再补随机标记 / 塔方向 / debuff / 功能等「非目录」笔刷。给摆关面板用。
 * 解析纯逻辑可测；颜色从 Icon 文件名里的颜色词猜（与 tile 编辑器一致），ColorTag 有值时优先。
 */
import {parseCsv} from './csv.js';
import {isNormalTile, isSkillTile, isBlockerTile, RANDOM_ID_A, RANDOM_ID_N} from './logic.js';
import {randomMarkerColor} from './random-marker.js';











/** 颜色词 → hex（与 tile-editor COLOR_HEX 对齐） */
export const COLOR_HEX                         = {
  red: '#e5484d', green: '#46c76b', orange: '#f0872f', blue: '#4aa3ec', purple: '#a472f0',
  yellow: '#f1c531', pink: '#ec5fa8', cyan: '#33c3c9', brown: '#b07a4f', color: '#9b8cff',
  grey: '#8a93a3', gray: '#8a93a3', white: '#dfe3ea', black: '#4a4f5a',
};
const KNOWN_COLORS = Object.keys(COLOR_HEX);

/** 从 Icon 文件名里的颜色词猜颜色（如 1003_blue_1-v2 → blue） */
export function colorFromIcon(icon        )         {
  const parts = icon.toLowerCase().split(/[_\-]/);
  for (const p of parts) if (KNOWN_COLORS.includes(p)) return p;
  return '';
}

export function entryColor(e           )         {
  const tag = (e.colorTag || colorFromIcon(e.icon)).toLowerCase();
  return COLOR_HEX[tag] || hashColor(e.tileId);
}

/** 没有颜色信息时按 id 稳定取色 */
export function hashColor(id        )         {
  const h = (Math.abs(id) * 2654435761) % 360;
  return `hsl(${h} 42% 55%)`;
}

/** 解析 config_client_tile.csv（前三行表头）→ 条目 */
export function parseTileCatalog(text        )              {
  const rows = parseCsv(text).slice(3);
  const out              = [];
  for (const f of rows) {
    const tileId = parseInt(f[0] ?? '', 10);
    if (!Number.isFinite(tileId)) continue;
    out.push({
      tileId,
      des: f[1] ?? '',
      iconType: parseInt(f[2] ?? '0', 10) || 0,
      board: f[3] ?? 'tile_bg',
      icon: f[4] ?? '',
      colorTag: f[5] ?? '',
      themeTag: f[6] ?? '',
    });
  }
  return out;
}





                                                                



                      

                            


                                                 



/** tile 的颜色词：ColorTag 优先，否则从 icon 文件名猜；都没有返回 ''（无色/其他） */
export function tileColorTag(e           )         {
  return (e.colorTag || colorFromIcon(e.icon)).toLowerCase();
}

/** 颜色分组在调色板里的固定顺序（只展示普通 tile 里实际出现的） */
export const COLOR_ORDER = ['red', 'orange', 'yellow', 'green', 'cyan', 'blue', 'purple', 'pink', 'brown', 'white', 'color'];

/** 普通 tile 实际用到的颜色词（按 COLOR_ORDER 排序；含 '' 表示有无色 tile） */
export function normalColorTags(brushes                  )           {
  const present = new Set(brushes.map(b => b.colorTag ?? ''));
  const ordered = COLOR_ORDER.filter(c => present.has(c));
  if (present.has('')) ordered.push('');
  return ordered;
}





/** 随机标记 A..N（-10..-23），开局按 DupPara 的种类数抽真实 tile 填进去 */
const RANDOM_LABELS = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'I', 'J', 'K', 'L', 'M', 'N'];

/** 已知 debuff（主玩 TileTypeValues）：id → 名 */
export const DEBUFFS                             = [
  [21000, '金壳', '../image/21000_golden_4.png'],
  [21004, '迷雾', '../image/icon_tile_cloud.png'],
  [21003, '冰块', '../image/icon_tile_icecube.png'],
];

/** 已知功能/blocker 兜底（csv 里没有时也能摆） */
const FUNC_BRUSHES          = [
  { kind: 'tile', value: 9000, label: '金币', color: '#f1c531', des: '普通金币' },
  { kind: 'tile', value: 10, label: '万能', color: '#9b8cff', des: '万能 tile' },
  { kind: 'tile', value: 11, label: '磁铁', color: '#4aa3ec', des: '超级磁铁' },
];
const BLOCKER_FALLBACK                     = [
  [10000, 'blocker金币'], [10010, 'blocker宝箱'], [10017, 'blocker锤石'], [10032, 'blocker玻璃柜'],
];

/** 把目录 + 内置笔刷整理成分类调色板 */
export function buildPalette(catalog                      )             {
  const normal          = [];
  const skill          = [];
  const blocker          = [];
  for (const e of catalog) {
    const b        = { kind: 'tile', value: e.tileId, label: String(e.tileId), color: entryColor(e), icon: e.icon, des: e.des, colorTag: tileColorTag(e) };
    if (isBlockerTile(e.tileId)) blocker.push(b);
    else if (isNormalTile(e.tileId)) normal.push(b);
    else if (isSkillTile(e.tileId)) skill.push(b);
    else normal.push(b);
  }
  // blocker 兜底：csv 没有的已知 blocker 也加上
  const have = new Set(blocker.map(b => b.value));
  for (const [id, name] of BLOCKER_FALLBACK) if (!have.has(id)) blocker.push({ kind: 'tile', value: id, label: name, color: '#8a93a3', des: name });

  const random          = [];
  for (let i = 0; i <= RANDOM_ID_A - RANDOM_ID_N; i++) {
    const id = RANDOM_ID_A - i;
    random.push({ kind: 'random', value: id, label: RANDOM_LABELS[i], color: randomMarkerColor(id) , des: `随机标记 ${RANDOM_LABELS[i]}（TileId ${id}）` });
  }

  const tower          = [
    { kind: 'tower', value: 0, label: '塔↑', color: '#2d3340' },
    { kind: 'tower', value: 1, label: '塔←', color: '#2d3340' },
    { kind: 'tower', value: 2, label: '塔↓', color: '#2d3340' },
    { kind: 'tower', value: 3, label: '塔→', color: '#2d3340' },
  ];

  const debuff          = DEBUFFS.map(([id, name, iconSrc]) => ({ kind: 'debuff'         , value: id, label: name, color: '#c56bd0', iconSrc }));

  return [
    { key: 'random', name: '随机', brushes: random },
    { key: 'normal', name: '普通', brushes: normal },
    { key: 'tower', name: '塔', brushes: tower },
    { key: 'debuff', name: 'Debuff', brushes: debuff },
    { key: 'func', name: '功能', brushes: FUNC_BRUSHES },
  ];
}

export const ERASER        = { kind: 'erase', value: 0, label: '⌫', color: '#39414f' };
