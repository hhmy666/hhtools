/**
 * 母版画布绘制（canvas）。半格制同主玩、同 playbook render.ts：半格 (gx,gy) 屏幕 = (ox+gx·s, oy+gy·s)，
 * tile 占 2×2 半格、中心在 (x+1,y+1)。层间不位移靠绘制顺序区分；被遮挡压暗、当前层描白框、更高层淡出。
 * 扩展主玩 LevelEditor：塔（按方向把其上 tile 错位堆叠）、debuff 图标覆盖、显示最大 Z 过滤。
 * 纯几何助手（screenToGrid/fitView/rectOf）可 node --test；draw* 碰 canvas 由手测。
 */
import { BOARD_STEP_RATIO, RANDOM_ID_A, RANDOM_ID_N } from './logic.ts';
import { randomMarkerColor } from './random-marker.ts';
import type { Master, EdTile, EdTower } from './level-model.ts';

export interface ViewXf { s: number; ox: number; oy: number; } // s = 一个半格的像素

// ---------- 摆放范围（与 tile 工程 LevelEditor 的 Values 一致） ----------
/** 支持的最大横向位置数 = Values.SupportMaxPositionX（位置 0..16） */
export const RANGE_MAX_X = 17;
/** 支持的最大纵向位置数 = Values.SupportMaxPositionY（位置 0..18） */
export const RANGE_MAX_Y = 19;
/** squareLines 内框宽（单位格）= _singleWidth * (7 + 6) */
const RANGE_SQUARE_W = 13;
/** squareLines 内框高（单位格）= _singleWidth * (8 + 7) */
const RANGE_SQUARE_H = 15;

export function screenToGrid(v: ViewXf, mx: number, my: number): { gx: number; gy: number } {
  return { gx: (mx - v.ox) / v.s, gy: (my - v.oy) / v.s };
}

/** 模型坐标 (x,y) 的屏幕中心点（与 tile 中心同口径：screen = ox + (x+1)·s） */
function ptScreen(v: ViewXf, x: number, y: number): { x: number; y: number } {
  return { x: v.ox + (x + 1) * v.s, y: v.oy + (y + 1) * v.s };
}

/** 固定铺满整个摆放范围（17×19 点阵，与 Unity LevelEditor 的棋盘一致，与内容无关）。
 *  棋盘中心 (dot 8,9) 居中到画布中心；tile 在边缘点会外扩半个 tile，故留 ±1 半格余量。 */
export function fitView(_m: Master, w: number, h: number): ViewXf {
  const x0 = 0, x1 = RANGE_MAX_X + 1; // dot 0..16 → 中心 1..17 → tile 外扩 ±1 → 0..18
  const y0 = 0, y1 = RANGE_MAX_Y + 1; // dot 0..18 → 中心 1..19 → 0..20
  const s = Math.max(6, Math.min(48, Math.min((w * 0.86) / (x1 - x0), (h * 0.86) / (y1 - y0))));
  return { s, ox: w / 2 - ((x0 + x1) / 2) * s, oy: h / 2 - ((y0 + y1) / 2) * s };
}

export interface Rect { x: number; y: number; w: number; h: number; }

export function tileDisplayLabel(tileId: number): string {
  return tileId <= RANDOM_ID_A && tileId >= RANDOM_ID_N
    ? String.fromCharCode(65 + RANDOM_ID_A - tileId)
    : String(tileId);
}
export const randomMarkerTextColor = randomMarkerColor;

/** tile 的屏幕矩形（2×2 半格见方，留一点缝看边界） */
export function rectOf(v: ViewXf, t: { x: number; y: number }, offX = 0, offY = 0): Rect {
  const inset = Math.max(1, v.s * 0.05);
  const side = 2 * v.s;
  return {
    x: v.ox + (t.x + 1 + offX) * v.s - side / 2 + inset,
    y: v.oy + (t.y + 1 + offY) * v.s - side / 2 + inset,
    w: side - 2 * inset,
    h: side - 2 * inset,
  };
}

/** 试玩主玩尺寸：横向步距/可见宽 148px，素材高度 180px；横向相邻 tile 不留缝。 */
export function playRectOf(v: ViewXf, t: { x: number; y: number }, offX = 0, offY = 0): Rect {
  const w = 2 * v.s;
  const h = w * (180 / 148);
  const cx = v.ox + (t.x + 1 + offX) * v.s;
  const cy = v.oy + (t.y + 1 + offY) * v.s;
  return { x: cx - w / 2, y: cy - h / 2, w, h };
}

const DIRS: [number, number][] = [[0, -1], [-1, 0], [0, 1], [1, 0]]; // 上左下右（主玩 Direction 0/1/2/3）
const STACK_SHIFT = 0.5; // 塔上每层错位（半格）

/** 某 tile 若落在塔位上，按塔方向 × z 的错位量（半格） */
export function towerOffset(towers: readonly EdTower[], t: EdTile): { ox: number; oy: number } {
  const tw = towers.find(w => w.x === t.x && w.y === t.y);
  if (!tw) return { ox: 0, oy: 0 };
  const [dx, dy] = DIRS[tw.direction] ?? [0, 0];
  return { ox: dx * STACK_SHIFT * t.z, oy: dy * STACK_SHIFT * t.z };
}

export type UpperMode = 'fade' | 'hide';

export interface Hover {
  kind: 'place' | 'erase' | 'tower' | 'debuff';
  x: number; y: number; color: string; blocked: boolean;
}

export interface RenderState {
  templateScreenshot?: boolean;
  unsupportedIndices?: ReadonlySet<number>;
  labelOf: (tileId: number) => string;
  master: Master;
  view: ViewXf;
  currentZ: number;
  upper: UpperMode;
  /** 显示最大 Z：null=不限；否则隐藏 z>showMaxZ（多层模式=只看到这层及以下） */
  showMaxZ: number | null;
  /** 显示最小 Z：null=不限；否则隐藏 z<showMinZ（单层模式=min=max=只看这一层） */
  showMinZ?: number | null;
  cover: boolean[];
  /** tileId → 颜色 */
  colorOf: (tileId: number) => string;
  /** 图案名 → 已加载图片（可选，有则画真图） */
  icons?: Map<string, HTMLImageElement>;
  /** tileId → 图案名 */
  iconOf?: (tileId: number) => string | undefined;
  /** debuffId → 已加载的覆盖图标 */
  debuffIcons?: Map<number, HTMLImageElement>;
  hover: Hover | null;
  /** 分组视图：开则按 groupId 给 tile 描色边 */
  groupColorOf?: (groupId: number) => string | null;
  /** 画摆放范围（点阵 + 外边界 + squareLines 内框）；默认开，传 false 关闭 */
  showRange?: boolean;
  /** tile 槽位背景图（tile_bg.png）；有则作为每个 tile 的底，图案画其上 */
  tileBg?: HTMLImageElement;
  /** 在每座塔上标注其堆叠的 tile 层数（仅编辑画布用） */
  showTowerStacks?: boolean;
  /** 违反「相邻层不能完全覆盖」规则的 tile 下标，画红框示警（仅编辑画布用） */
  badIndices?: Set<number>;
  /** 选择态：命中这些 key（x,y,z）的 tile 画青色高亮框（仅编辑画布用） */
  selectedKeys?: Set<string>;
  /** 自定义 tile 几何；试玩区使用 playRectOf。 */
  tileRect?: typeof rectOf;
  /** 裁掉底图左右透明边（162px 原图取中间 148px），仅试玩主玩画面使用。 */
  cropTileBgSides?: boolean;
  /** icon 的纵向偏移，按 148px 设计宽度等比缩放；负数向上。 */
  iconOffsetY?: number;
}

function stateRect(st: RenderState, t: { x: number; y: number }, offX = 0, offY = 0): Rect {
  return (st.tileRect ?? rectOf)(st.view, t, offX, offY);
}

export function render(g: CanvasRenderingContext2D, w: number, h: number, st: RenderState): void {
  g.clearRect(0, 0, w, h);
  g.fillStyle = st.templateScreenshot ? '#e0f8e2' : '#f5f9fd';
  g.fillRect(0, 0, w, h);
  if (st.templateScreenshot) drawTemplateScreenshotGrid(g, w, h, st.view);
  else drawGrid(g, w, h, st);
  if (st.showRange !== false) drawPlacementRange(g, st);
  drawTowers(g, st);

  const m = st.master;
  const order = m.tiles.map((_, i) => i).sort((a, b) => {
    const p = m.tiles[a]!, q = m.tiles[b]!;
    return p.z - q.z || p.y - q.y || p.x - q.x;
  });
  for (const i of order) {
    const t = m.tiles[i]!;
    // 全部=无上下限全显；多层=只显示 z≤showMaxZ；单层=showMinZ==showMaxZ 只显示这一层
    if (st.showMaxZ !== null && t.z > st.showMaxZ) continue;
    if (st.showMinZ != null && t.z < st.showMinZ) continue;
    g.globalAlpha = 1;
    const off = towerOffset(m.towers, t);
    const r = stateRect(st, t, off.ox, off.oy);
    drawTile(g, r, t, st);
    if (st.cover[i]) { g.fillStyle = st.templateScreenshot ? 'rgba(0,0,0,0.48)' : 'rgba(39,67,98,0.27)'; roundRect(g, r, r.w * 0.14); g.fill(); }
    if (t.debuffIndex !== -1) drawDebuffOverlay(g, r, m.debuffs[t.debuffIndex]?.debuffId, st.debuffIcons);
    const gc = st.groupColorOf?.(t.groupId);
    if (gc) { g.strokeStyle = gc; g.lineWidth = 3; g.strokeRect(r.x + 1.5, r.y + 1.5, r.w - 3, r.h - 3); }
    if (t.z === st.currentZ) { g.strokeStyle = '#8bb4dc'; g.lineWidth = 1.5; g.strokeRect(r.x + 0.75, r.y + 0.75, r.w - 1.5, r.h - 1.5); }
    if (st.unsupportedIndices?.has(i)) { g.strokeStyle = '#2469a5'; g.lineWidth = 2.5; g.strokeRect(r.x + 2, r.y + 2, r.w - 4, r.h - 4); }
    if (st.badIndices?.has(i)) {
      g.setLineDash([4, 3]); g.strokeStyle = '#ff5a5a'; g.lineWidth = 2.5;
      g.strokeRect(r.x + 1, r.y + 1, r.w - 2, r.h - 2); g.setLineDash([]);
    }
    if (st.selectedKeys?.has(`${t.x},${t.y},${t.z}`)) {
      g.fillStyle = 'rgba(88,182,240,0.28)'; roundRect(g, r, r.w * 0.14); g.fill();
      g.strokeStyle = '#58b6f0'; g.lineWidth = 3; g.strokeRect(r.x + 1.5, r.y + 1.5, r.w - 3, r.h - 3);
    }
  }
  g.globalAlpha = 1;
  if (st.showTowerStacks) drawTowerStacks(g, st);
  drawHover(g, st);
}

/** 塔上堆叠的 tile 数：锚点正好落在塔位 (x,y) 的 tile，跨所有 z 累计 */
export function towerTileCount(m: Master, tw: EdTower): number {
  let n = 0;
  for (const t of m.tiles) if (t.x === tw.x && t.y === tw.y) n++;
  return n;
}

/** 给每座塔画一个「×层数」角标，位置落在堆叠反方向一侧、画在 tile 之上，保证可读 */
function drawTowerStacks(g: CanvasRenderingContext2D, st: RenderState): void {
  for (const tw of st.master.towers) {
    const n = towerTileCount(st.master, tw);
    const r = stateRect(st, tw);
    const [dx, dy] = DIRS[tw.direction] ?? [0, 0];
    const fs = Math.max(9, r.w * 0.28);
    const label = `×${n}`;
    g.font = `bold ${fs}px ui-monospace, monospace`;
    g.textAlign = 'center'; g.textBaseline = 'middle';
    const padX = fs * 0.45;
    const bw = g.measureText(label).width + padX * 2;
    const bh = fs * 1.3;
    // 塔沿 (dx,dy) 方向堆叠，角标放反方向一侧，避开被上层 tile 盖住
    const cx = r.x + r.w / 2 - dx * (r.w * 0.62);
    const cy = r.y + r.h / 2 - dy * (r.h * 0.62);
    g.fillStyle = n > 0 ? '#f5b544' : '#444b58';
    g.strokeStyle = '#16181d'; g.lineWidth = 2;
    roundRect(g, { x: cx - bw / 2, y: cy - bh / 2, w: bw, h: bh }, bh / 2); g.fill(); g.stroke();
    g.fillStyle = n > 0 ? '#14161b' : '#aab3c0';
    g.fillText(label, cx, cy);
  }
}

function drawTile(g: CanvasRenderingContext2D, r: Rect, t: EdTile, st: RenderState): void {
  const icon = st.iconOf?.(t.tileId);
  const img = icon && st.icons?.get(icon);
  const bg = st.tileBg;
  if (bg && bg.complete && bg.naturalWidth > 0) {
    if (st.cropTileBgSides) {
      const sourceW = Math.round(bg.naturalWidth * BOARD_STEP_RATIO);
      const sourceX = (bg.naturalWidth - sourceW) / 2;
      g.drawImage(bg, sourceX, 0, sourceW, bg.naturalHeight, r.x, r.y, r.w, r.h);
    } else {
      // 编辑区保留完整底图，便于观察素材边界。
      g.drawImage(bg, r.x, r.y, r.w, r.h);
    }
  } else {
    g.fillStyle = randomMarkerColor(t.tileId) ? '#252933' : st.colorOf(t.tileId);
    roundRect(g, r, r.w * 0.14); g.fill();
    g.strokeStyle = 'rgba(0,0,0,0.4)'; g.lineWidth = 1; g.stroke();
  }
  if (st.templateScreenshot) {
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
  if (img && img.complete && img.naturalWidth > 0) {
    const k = Math.min((r.w * 0.82 * BOARD_STEP_RATIO) / img.naturalWidth, (r.h * 0.82) / img.naturalHeight);
    const fw = img.naturalWidth * k, fh = img.naturalHeight * k;
    const iconOffsetY = (st.iconOffsetY ?? 0) * (r.w / 148);
    g.drawImage(img, r.x + (r.w - fw) / 2, r.y + (r.h - fh) / 2 + iconOffsetY, fw, fh);
  } else {
    const fs = Math.max(8, r.w * 0.3);
    g.font = `bold ${fs}px ui-monospace, monospace`;
    g.textAlign = 'center'; g.textBaseline = 'middle';
    g.lineWidth = Math.max(2, fs * 0.16); g.lineJoin = 'round';
    g.strokeStyle = 'rgba(0,0,0,0.55)';
    const label = st.labelOf(t.tileId);
    g.strokeText(label, r.x + r.w / 2, r.y + r.h / 2, r.w * 0.85);
    g.fillStyle = randomMarkerColor(t.tileId) ?? '#fff'; g.fillText(label, r.x + r.w / 2, r.y + r.h / 2, r.w * 0.85);
  }
  if (t.z > 0) {
    g.font = `9px ui-monospace, monospace`; g.textAlign = 'right'; g.textBaseline = 'bottom';
    g.fillStyle = 'rgba(0,0,0,0.55)'; g.fillText('z' + t.z, r.x + r.w - 2, r.y + r.h - 1);
  }
}

function drawTowers(g: CanvasRenderingContext2D, st: RenderState): void {
  const arrows = ['↑', '←', '↓', '→'];
  for (const tw of st.master.towers) {
    const r = stateRect(st, tw);
    g.fillStyle = '#2d3340'; roundRect(g, r, r.w * 0.14); g.fill();
    g.strokeStyle = '#3c4453'; g.lineWidth = 1.5; g.stroke();
    const fs = Math.max(10, r.w * 0.5);
    g.font = `bold ${fs}px system-ui`; g.textAlign = 'center'; g.textBaseline = 'middle';
    g.fillStyle = '#9aa3b0'; g.fillText(arrows[tw.direction] ?? '◆', r.x + r.w / 2, r.y + r.h / 2);
  }
}

function drawDebuffOverlay(g: CanvasRenderingContext2D, r: Rect, debuffId: number | undefined, icons: Map<number, HTMLImageElement> | undefined): void {
  const img = debuffId === undefined ? undefined : icons?.get(debuffId);
  if (!img?.complete || img.naturalWidth <= 0) return;
  g.drawImage(img, r.x, r.y, r.w, r.h);
}

function roundRect(g: CanvasRenderingContext2D, r: Rect, rad: number): void {
  g.beginPath(); g.roundRect(r.x, r.y, r.w, r.h, rad);
}

function drawGrid(g: CanvasRenderingContext2D, w: number, h: number, st: RenderState): void {
  const v = st.view; if (v.s < 5) return;
  const gx0 = Math.floor(-v.ox / v.s), gx1 = Math.ceil((w - v.ox) / v.s);
  const gy0 = Math.floor(-v.oy / v.s), gy1 = Math.ceil((h - v.oy) / v.s);
  g.lineWidth = 1;
  for (let gx = gx0; gx <= gx1; gx++) {
    const x = Math.round(v.ox + gx * v.s) + 0.5;
    g.strokeStyle = gx % 2 === 0 ? 'rgba(73,122,171,0.12)' : 'rgba(73,122,171,0.05)';
    g.beginPath(); g.moveTo(x, 0); g.lineTo(x, h); g.stroke();
  }
  for (let gy = gy0; gy <= gy1; gy++) {
    const y = Math.round(v.oy + gy * v.s) + 0.5;
    g.strokeStyle = gy % 2 === 0 ? 'rgba(73,122,171,0.12)' : 'rgba(73,122,171,0.05)';
    g.beginPath(); g.moveTo(0, y); g.lineTo(w, y); g.stroke();
  }
}

/** 摆放范围：17×19 红/蓝点阵 + 最外圈边界 + 居中 squareLines 内框（画在 tile 之下作参考底） */
function drawPlacementRange(g: CanvasRenderingContext2D, st: RenderState): void {
  const v = st.view;
  const dotMaxX = RANGE_MAX_X - 1; // 16
  const dotMaxY = RANGE_MAX_Y - 1; // 18

  // 点阵（parity 相同为红、不同为蓝，同 Unity redDot/blueDot）
  if (v.s >= 7) {
    const rad = Math.max(1, v.s * 0.07);
    for (let x = 0; x <= dotMaxX; x++) {
      for (let y = 0; y <= dotMaxY; y++) {
        const p = ptScreen(v, x, y);
        g.beginPath(); g.arc(p.x, p.y, rad, 0, Math.PI * 2);
        g.fillStyle = (x % 2 === y % 2) ? 'rgba(229,72,77,0.32)' : 'rgba(74,163,236,0.28)';
        g.fill();
      }
    }
  }

  // 最外圈边界（17×19 点阵最外层点连成的矩形）
  const a = ptScreen(v, 0, 0), b = ptScreen(v, dotMaxX, dotMaxY);
  g.strokeStyle = 'rgba(78,133,183,0.66)'; g.lineWidth = 1.5;
  g.strokeRect(a.x, a.y, b.x - a.x, b.y - a.y);
  drawRangeLabel(g, `${RANGE_MAX_X}×${RANGE_MAX_Y}`, a.x, a.y, 'rgba(90,200,160,0.95)');

  // squareLines 内框（居中，13×15 单位格）
  const cx = dotMaxX / 2, cy = dotMaxY / 2; // 8, 9（棋盘中心）
  const s0 = ptScreen(v, cx - RANGE_SQUARE_W / 2, cy - RANGE_SQUARE_H / 2);
  const s1 = ptScreen(v, cx + RANGE_SQUARE_W / 2, cy + RANGE_SQUARE_H / 2);
  g.setLineDash([6, 4]); g.strokeStyle = 'rgba(255,255,255,0.45)'; g.lineWidth = 1.5;
  g.strokeRect(s0.x, s0.y, s1.x - s0.x, s1.y - s0.y); g.setLineDash([]);
  drawRangeLabel(g, `${RANGE_SQUARE_W}×${RANGE_SQUARE_H}`, s0.x, s0.y, 'rgba(255,255,255,0.9)');
}

/** 在矩形左上角外侧标注尺寸文字（带描边保证可读） */
function drawRangeLabel(g: CanvasRenderingContext2D, text: string, x: number, y: number, color: string): void {
  g.font = 'bold 12px ui-monospace, monospace';
  g.textAlign = 'left'; g.textBaseline = 'bottom';
  g.lineWidth = 3; g.lineJoin = 'round'; g.strokeStyle = 'rgba(0,0,0,0.75)';
  g.strokeText(text, x + 2, y - 3);
  g.fillStyle = color; g.fillText(text, x + 2, y - 3);
}

function drawHover(g: CanvasRenderingContext2D, st: RenderState): void {
  const hv = st.hover; if (!hv) return;
  const r = stateRect(st, { x: hv.x, y: hv.y });
  if (hv.kind === 'erase') {
    g.strokeStyle = '#ff5a5a'; g.lineWidth = 3;
    g.beginPath();
    g.moveTo(r.x + 4, r.y + 4); g.lineTo(r.x + r.w - 4, r.y + r.h - 4);
    g.moveTo(r.x + r.w - 4, r.y + 4); g.lineTo(r.x + 4, r.y + r.h - 4);
    g.stroke(); g.strokeRect(r.x, r.y, r.w, r.h);
    return;
  }
  g.globalAlpha = 0.45; g.fillStyle = hv.blocked ? '#ff5a5a' : hv.color;
  roundRect(g, r, r.w * 0.14); g.fill(); g.globalAlpha = 1;
  g.setLineDash([5, 4]); g.strokeStyle = hv.blocked ? '#ff5a5a' : '#fff'; g.lineWidth = 2;
  g.strokeRect(r.x, r.y, r.w, r.h); g.setLineDash([]);
}

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
