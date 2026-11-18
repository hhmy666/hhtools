import {DEFAULT_DUP, DUP_SLOTS, formatDupPara} from './level-csv.js';







export const initialDupDrawerState                 = { open: false };

export function savableLevelId(raw        )                {
  const id = raw.trim();
  return id === '' || id === 'untitled' ? null : id;
}

export function assertConfigReadable(ok         , status        )       {
  if (!ok) throw new Error(`config_level.csv 读取失败（${status}）`);
}

/** 复制当前十档，字段顺序与 config_level.csv 的 DupPara1–10 一致。 */
export function copyDupParasText(dup                             )         {
  return Array.from({ length: DUP_SLOTS }, (_, slot) => formatDupPara(dup[slot] ?? DEFAULT_DUP)).join(',');
}

const CLOSED_HEIGHT = 38;

export function drawerHeights(
  dupOpen         ,
  indexOpen         ,
  levelOpen         ,
  viewportHeight        ,
)                                                {
  const vh = Math.max(0, viewportHeight);
  const open = [dupOpen, indexOpen, levelOpen];
  const openHeight = Math.max(CLOSED_HEIGHT, (vh - 42) / 3);
  const heights = open.map(isOpen => Math.round(isOpen ? openHeight : CLOSED_HEIGHT));
  return { dup: heights[0] , index: heights[1] , level: heights[2]  };
}

export function drawerDockHeight(heights                                               )         {
  return Math.max(heights.dup, heights.index, heights.level);
}

export function reduceDupDrawer(state                , action                 )                 {
  return { open: action.type === 'toggle' ? !state.open : false };
}

export function updateDupSlot(
  dup                             ,
  slot        ,
  key               ,
  raw        ,
)                     {
  const next = dup.slice();
  while (next.length <= slot) next.push(null);
  let value = parseInt(raw.replace(/[^0-9-]/g, ''), 10);
  if (!Number.isFinite(value)) value = 0;
  next[slot] = { ...(next[slot] ?? DEFAULT_DUP), [key]: value };
  return next;
}
