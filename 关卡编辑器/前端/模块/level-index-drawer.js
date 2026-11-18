import {parseCsv} from './csv.js';









export function levelIndexRows(text        , configRows                     )                  {
  const configById = new Map(configRows.map(row => [row.levelId, row]));
  return parseCsv(text).slice(3).map(fields => {
    const levelIndex = parseInt(fields[0] ?? '', 10);
    const levelId = (fields[4] ?? '').trim();
    const templateId = configById.get(levelId)?.templateId.trim() ?? '';
    return { levelIndex, levelId, templateId, loadable: levelId !== '' && templateId !== '' };
  }).filter(row => Number.isFinite(row.levelIndex) && row.levelId !== '')
    .sort((a, b) => a.levelIndex - b.levelIndex);
}

export function filterLevelIndexRows(rows                          , query        , limit = 500)                  {
  const q = query.trim().toLocaleLowerCase();
  const filtered = q === '' ? rows : rows.filter(row =>
    [String(row.levelIndex), row.levelId, row.templateId]
      .some(value => value.toLocaleLowerCase().includes(q)),
  );
  return filtered.slice(0, Math.max(0, limit));
}
