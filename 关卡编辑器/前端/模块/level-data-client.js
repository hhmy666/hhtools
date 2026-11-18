/** Canonical level-data API and adapters for the existing editor consumers. */




import {REQUIRED_LEVEL_CSV_NAMES, validateLevelCsvNames} from './level-csv-import.js';



























































export class LevelDataConflictError extends Error {
           revision        ;
           updatedBy        ;
           updatedAt        ;
  constructor(data                                                            ) {
    super(`数据已由 ${data.updatedBy} 于 ${data.updatedAt} 更新（版本 ${data.revision}）。请重新加载页面后再保存；本次配置变更尚未写入。`);
    this.name = 'LevelDataConflictError'; this.revision = data.revision;
    this.updatedBy = data.updatedBy; this.updatedAt = data.updatedAt;
  }
}
async function responseJson   (response          )             {
  const data = await response.json();
  if (response.status === 409) throw new LevelDataConflictError(data);
  if (!response.ok || data.ok === false) throw new Error(data.error || `HTTP ${response.status}`);
  return data     ;
}
export async function loadLevelData(fetcher                 = fetch)                          {
  return responseJson                (await fetcher('/api/level-data', { cache: 'no-store' }));
}
const SERVER_OWNED = new Set(['createdAt', 'createdBy', 'updatedAt', 'updatedBy', 'firstSeenAt', 'lastSeenAt',
  'computedAt', 'computedBy', 'author', 'revision', 'managedBy', 'sourceFile', 'sourceStatus', 'bytes', 'stats', 'statsStale']);
function writableValues(value         , templateMetadata = false)          {
  if (Array.isArray(value)) return value.map(item => writableValues(item));
  if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value)
    .filter(([key]) => !SERVER_OWNED.has(key) || (templateMetadata && key === 'author')).map(([key, nested]) => [key, writableValues(nested)]));
  return value;
}
export async function patchLevelData(baseRevision        , operations                               , fetcher                 = fetch)                                {
  return responseJson                      (await fetcher('/api/level-data', {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ baseRevision, operations: operations.map(operation => ({
      op: operation.op, entity: operation.entity, key: operation.key,
      ...(operation.values ? { values: writableValues(operation.values, operation.entity === 'templates') } : {}),
    })) }),
  }));
}
/** Statistics contain no audit values; the server validates the live bytes hash. */
export async function persistTemplateStats(baseRevision        , templateId        , sourceSha256        ,
  stats                                                                         , fetcher                 = fetch)                                {
  return responseJson                      (await fetcher('/api/level-data/template-stats', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ baseRevision, templateId, sourceSha256, stats }),
  }));
}
export async function importLevelCsvBundle(files                 , baseRevision        , fetcher                 = fetch)                                {
  if (!validateLevelCsvNames(files.map(file => file.name)).ok) throw new Error(`请一次选择且仅选择 5 个指定 CSV：${REQUIRED_LEVEL_CSV_NAMES.join('、')}`);
  const form = new FormData();
  form.append('baseRevision', String(baseRevision));
  for (const file of files) form.append('files', file, file.name);
  return responseJson                      (await fetcher('/api/level-data/import-csv', { method: 'POST', body: form }));
}
export function levelRowOperations(store                , row          )                       {
  const existing = store.levels[row.levelId];
  const operations                       = [{ op: existing ? 'update' : 'add', entity: 'levels', key: row.levelId, values: {
    levelId: row.levelId, des: row.des, templateId: row.templateId, randomType: row.randomType,
    dupParams: row.dup.map(dup => dup ? { aTileKinds: dup.a, seed: dup.seed, towerLucky: dup.lucky, bTileKinds: dup.b } : null),
    strategyGroupId: row.strategyGroupId.trim() === '' ? null : Number(row.strategyGroupId),
    randomSeedGroupId: row.randomSeedGroupId.trim() === '' ? null : Number(row.randomSeedGroupId),
  } }];
  if (existing?.sourceStatus === 'missingFromCsv') operations.push({ op: 'restore', entity: 'levels', key: row.levelId });
  return operations;
}
/** Adapt one retained record, including records currently absent from CSV. */
export function levelRecordToRow(row             )           {
  return {
    levelId: row.levelId, des: row.des, templateId: row.templateId, randomType: row.randomType,
    dup: row.dupParams.map(dup => dup ? { a: dup.aTileKinds, seed: dup.seed, lucky: dup.towerLucky, b: dup.bTileKinds } : null),
    strategyGroupId: row.strategyGroupId === null ? '' : String(row.strategyGroupId),
    randomSeedGroupId: row.randomSeedGroupId === null ? '' : String(row.randomSeedGroupId),
  };
}
function active                        (records                   )      {
  return Object.values(records).filter(row => row.sourceStatus === 'active');
}
export function adaptLevelData(store                )


  {
  const rows = active(store.levels).map(levelRecordToRow);
  const byId = new Map(rows.map(row => [row.levelId, row]));
  return {
    config: { header: [], rows },
    indexRows: active(store.levelIndexes).map(row => {
      const templateId = byId.get(row.levelId)?.templateId.trim() ?? '';
      return { levelIndex: row.levelIndex, levelId: row.levelId, templateId, loadable: row.levelId !== '' && templateId !== '' };
    }).sort((a, b) => a.levelIndex - b.levelIndex),
    seedGroupTable: new Map(active(store.randomSeedGroups).map(row => [row.groupId, row.seedIds.map(id => id ?? 0)])),
    seedTable: new Map(active(store.randomSeeds).map(row => [row.seedId, {
      seedId: row.seedId, groupId: row.groupId, chokeTileNum: row.chokeTileCount, chokeStartLayer: row.chokeTileStartLayer,
      chokeLayerGap: row.chokeTileParam, normalLayerGap: row.normalTileParam ?? 0,
      stagePercents: [...(row.stageParam ?? [])], stageKindNums: [...(row.tileKindNum ?? [])],
    }])),
    strategyGroupTable: new Map(active(store.strategyGroups).map(row => [row.strategyGroupId, row.slots.map(slot =>
      slot ? `${slot.useType}|${slot.targetCount}|${slot.strategyIds.length ? slot.strategyIds.join(';') : '0'}` : '')])),
  };
}
