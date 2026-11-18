/**
 * config_level.csv（关卡配置表）读写 + DupPara 派生档位解析。D3 的「哪个 LevelId 用哪个母版 + 每次玩怎么派生」。
 * 真实列（见本机 csv/config_level.csv 三行表头）：
 *   LevelId, Des, TemplateId, RandomType(0=按第n次玩/1=随机),
 *   DupPara1 … DupPara10, StrategyGroupId, randomSeed_groupID
 * 每个 DupParaN = `ATile种类数 | 随机种子 | 塔副本幸运值 | BTile种类数`，例 `12|0|100|4`；空 = 沿用上一档。
 * 纯逻辑，node --test 可直接测。
 */
import {parseCsv, toCsvLine} from './csv.js';



/** 全没配时的兜底档位（设计稿「全没配给默认 1|0|0|1」） */
export const DEFAULT_DUP          = { a: 1, seed: 0, lucky: 0, b: 1 };
export const DUP_SLOTS = 10;






                                   





/** 解析一个 DupPara 文本 `A|seed|lucky|B`；空串或字段不全 → null */
export function parseDupPara(s        )                 {
  const str = s.trim();
  if (str === '') return null;
  const p = str.split('|').map(x => x.trim());
  if (p.length < 4) return null;
  const n = p.map(x => parseInt(x, 10));
  if (n.some(x => !Number.isFinite(x))) return null;
  return { a: n[0] , seed: n[1] , lucky: n[2] , b: n[3]  };
}

export function formatDupPara(d                )         {
  return d ? `${d.a}|${d.seed}|${d.lucky}|${d.b}` : '';
}

/** 取第 playIndex（0-based）次玩实际生效的档位：当前档位，没配则回溯上一个已配的，再没有给默认 */
export function effectiveDup(row          , playIndex        )          {
  for (let i = Math.min(playIndex, DUP_SLOTS - 1); i >= 0; i--) {
    const d = row.dup[i];
    if (d) return d;
  }
  return DEFAULT_DUP;
}

const FIXED_COLS = 4; // LevelId, Des, TemplateId, RandomType

export function parseLevelRow(fields                   )           {
  const dup                     = [];
  for (let i = 0; i < DUP_SLOTS; i++) dup.push(parseDupPara(fields[FIXED_COLS + i] ?? ''));
  return {
    levelId: fields[0] ?? '',
    des: fields[1] ?? '',
    templateId: fields[2] ?? '',
    randomType: parseInt(fields[3] ?? '0', 10) || 0,
    dup,
    strategyGroupId: fields[FIXED_COLS + DUP_SLOTS] ?? '',
    randomSeedGroupId: fields[FIXED_COLS + DUP_SLOTS + 1] ?? '',
  };
}

export function levelRowToFields(row          )           {
  return [
    row.levelId, row.des, row.templateId, String(row.randomType),
    ...row.dup.map(formatDupPara),
    row.strategyGroupId, row.randomSeedGroupId,
  ];
}


                                








                             





/** 把配置行投影成底部副本抽屉只需的展示数据。 */
export function levelListRows(rows                     )                 {
  return rows.map(row => ({
    levelId: row.levelId,
    des: row.des,
    templateId: row.templateId,
    randomTypeLabel: row.randomType === 1 ? '随机' : '固定',
    strategyGroupId: row.strategyGroupId,
    randomSeedGroupId: row.randomSeedGroupId,
    loadable: row.templateId.trim() !== '',
  }));
}

export function filterLevelListRows(rows                         , query        , limit = 200)                 {
  const q = query.trim().toLocaleLowerCase();
  const filtered = q === '' ? rows : rows.filter(row =>
    [row.levelId, row.des, row.templateId, row.strategyGroupId, row.randomSeedGroupId]
      .some(value => value.toLocaleLowerCase().includes(q)),
  );
  return filtered.slice(0, Math.max(0, limit));
}

export function parseConfigLevel(text        )              {
  const all = parseCsv(text);
  const header = all.slice(0, 3);
  const rows = all.slice(3).map(parseLevelRow);
  return { header, rows };
}

export function serializeConfigLevel(cfg             )         {
  const lines = [
    ...cfg.header.map(toCsvLine),
    ...cfg.rows.map(r => toCsvLine(levelRowToFields(r))),
  ];
  return lines.join('\n') + '\n';
}

/** 新建一关的默认行：A 种类数按难度曲线递减（越玩越简单），其余默认 */
export function newLevelRow(levelId        , templateId        , des = '')           {
  const curve = [12, 12, 11, 11, 10, 10, 9, 9, 8, 8];
  return {
    levelId, des, templateId, randomType: 0,
    dup: curve.map(a => ({ a, seed: 0, lucky: 100, b: 4 })),
    strategyGroupId: '', randomSeedGroupId: '',
  };
}

/** 按 levelId 查行下标，没有返回 -1 */
export function findRow(cfg             , levelId        )         {
  return cfg.rows.findIndex(r => r.levelId === levelId);
}

/** 插入或覆盖一行（按 levelId）。返回新 ConfigLevel */
export function upsertRow(cfg             , row          )              {
  const i = findRow(cfg, row.levelId);
  const rows = cfg.rows.slice();
  if (i >= 0) rows[i] = row; else rows.push(row);
  return { header: cfg.header, rows };
}
