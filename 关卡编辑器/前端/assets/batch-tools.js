import { parseCsv, toCsvLine } from '../modules/csv.js';
import { DEFAULT_DUP, parseLevelRow } from '../modules/level-csv.js';
import { decodeLevelSave } from '../modules/level-bytes.js';
import { fromSave } from '../modules/level-model.js';

const columns = [
  'template_id', 'Tile数量', '非塔层数', '平均非塔表面数', '塔Tile数', '塔数量',
  '塔最高层数', '塔平均层数', '塔Tile数占比', '塔隐藏比例', '总宽度', '总高度',
  'Atile总数量', 'Atile占总tile比例', 'A塔tile占A总tile比例', 'A平均非塔表面数',
  'Btile总数量', 'Btile占总tile比例', 'B塔tile占B总tile比例', 'B平均非塔表面数',
  '平均表面数', '初始 ATile 数', '每层 Tile 数',
];

const cleanId = value => value.trim().replace(/_m\.bytes$/i, '');
const idPattern = /^[\w.-]{1,172}$/;
const dupPattern = /^(-?\d+)\s*\|\s*(-?\d+)\s*\|\s*(-?\d+)\s*\|\s*(-?\d+)$/;

function readDup(value) {
  const parts = dupPattern.exec(value.trim());
  if (!parts) throw new Error('参数应为 A种类数|随机种子|塔副本幸运值|B种类数。');
  const [a, seed, lucky, b] = parts.slice(1).map(Number);
  if (seed < -2147483648 || seed > 2147483647 || [a, lucky, b].some(n => n < 0 || n > 100)) {
    throw new Error('种子须为 32 位整数，其他参数须在 0–100 之间。');
  }
  return {a, seed, lucky, b};
}

function pasteRows(text, errors, readParameters = true) {
  const result = [];
  for (const [lineIndex, raw] of text.split(/\r?\n/).entries()) {
    const line = raw.trim().replace(/\\\|/g, '|');
    if (!line || /^\|?[\s:|-]+$/.test(line)) continue;
    const row = line.replace(/^\|\s*|\s*\|$/g, '');
    if (/^(template[_ ]?id|level[_ ]?id|模板\s*ID|关卡\s*ID)(?:[\s,|]|$)/i.test(row)) continue;
    try {
      const parameterRow = /^([\w.-]+)[\s,|]+(.+)$/.exec(row);
      if (parameterRow && parameterRow[2].includes('|')) {
        const id = cleanId(parameterRow[1]);
        result.push(readParameters ? {id, dup:readDup(parameterRow[2].replace(/^"|"$/g, ''))} : {id});
      } else {
        const ids = row.split(/[\s,，、]+/).map(cleanId);
        if (!ids.every(id => idPattern.test(id) && id[0] !== '.')) throw new Error('模板 ID 格式无效。');
        result.push(...ids.map(id => ({id})));
      }
    } catch (error) {
      errors.push('第 ' + (lineIndex + 1) + ' 行：' + error.message);
    }
  }
  return result;
}

function importedConfig(text) {
  const matrix = parseCsv(text.replace(/^\uFEFF/, ''));
  const headerIndex = matrix.findIndex(fields => fields[0]?.trim().toLowerCase() === 'levelid'
    && fields[2]?.trim().toLowerCase() === 'templateid');
  if (headerIndex < 0) return null;
  const rows = matrix.slice(headerIndex + 1).filter(fields =>
    !['string', 'int', '关卡ID', '关卡id'].includes(fields[0]?.trim()) && idPattern.test(fields[2]?.trim() ?? '')
    && idPattern.test(fields[0]?.trim() ?? ''));
  if (!rows.length) throw new Error('配置表里没有可用关卡。');
  return rows.map(fields => {
    const row = parseLevelRow(fields);
    row.dup = row.dup.map((_, index) => {
      const raw = fields[4 + index]?.trim();
      return raw ? readDup(raw) : null;
    });
    return row;
  });
}

function jobsFor(input, rows) {
  const matches = rows.filter(row => cleanId(row.levelId) === input.id);
  const selected = matches.length ? matches : rows.filter(row => cleanId(row.templateId) === input.id);
  if (input.dup) {
    const row = !input.file ? matches.find(row => cleanId(row.templateId) !== input.id) : undefined;
    return [{...input, id:row ? cleanId(row.templateId) : input.id, levelId:row?.levelId, slot:0}];
  }
  if (!selected.length) return [input];
  return selected.flatMap(row => {
    const common = { ...input, id:cleanId(row.templateId), levelId:row.levelId,
      seedGroup:row.randomSeedGroupId, strategyGroup:row.strategyGroupId };
    const jobs = row.dup.flatMap((dup, slot) => dup ? [{...common, dup, slot}] : []);
    return jobs.length ? jobs : [common];
  });
}

const ratio = (count, total) => total ? count / total : 0;
const rounded = value => Number(value.toFixed(2));
const averagePerLayer = tiles => ratio(tiles.length, new Set(tiles.map(tile => tile.z)).size);

function statistics(master, job) {
  // Use the same normal-tile population and surface formulas as the editor panel.
  const towers = new Map(master.towers.map(tower => [tower.x + ',' + tower.y, 0]));
  const key = tile => tile.x + ',' + tile.y;
  const normal = master.tiles.filter(({tileId:id}) => id >= 1000 && id <= 9999 || id >= -23 && id <= -10);
  const nonTower = normal.filter(tile => !towers.has(key(tile)));
  for (const tile of normal) if (towers.has(key(tile))) towers.set(key(tile), towers.get(key(tile)) + 1);
  const layers = new Map();
  for (const tile of master.tiles) if (!towers.has(key(tile))) layers.set(tile.z, (layers.get(tile.z) ?? 0) + 1);
  const group = id => {
    const tiles = normal.filter(tile => tile.tileId === id);
    const outside = tiles.filter(tile => !towers.has(key(tile)));
    return [tiles.length, rounded(ratio(tiles.length, normal.length)),
      rounded(ratio(tiles.length - outside.length, tiles.length)), rounded(averagePerLayer(outside))];
  };
  const towerTiles = normal.length - nonTower.length;
  const heights = [...towers.values()];
  const surface = rounded(averagePerLayer(nonTower)) + towers.size;
  const dimension = axis => {
    const values = master.tiles.map(tile => tile[axis]);
    return values.length ? Math.max(...values) - Math.min(...values) + 2 : 0;
  };
  const a = group(-10);
  const b = group(-11);
  const byLayer = [...layers].sort(([a], [b]) => a - b).map(([, count]) => count);
  return [
    master.templateId || job.id, normal.length, layers.size, rounded(averagePerLayer(nonTower)),
    towerTiles, towers.size, Math.max(1, ...heights), rounded(ratio(towerTiles, towers.size)),
    rounded(ratio(towerTiles, normal.length)), rounded(ratio(towerTiles - heights.filter(Boolean).length, towerTiles)),
    dimension('x'), dimension('y'), ...a, ...b, rounded(surface), Math.trunc(surface / 1.5),
    byLayer.length ? byLayer.join('|') + '|' : '',
  ];
}

function renderTable(table, rows) {
  table.replaceChildren();
  const append = (part, values, tag) => {
    const tr = part.insertRow();
    for (const value of values) {
      const cell = document.createElement(tag);
      if (tag === 'th') cell.scope = 'col';
      cell.textContent = String(value);
      tr.append(cell);
    }
  };
  append(table.createTHead(), columns, 'th');
  const body = table.createTBody();
  for (const row of rows) append(body, row, 'td');
}

export function bindBatchTools(api) {
  const dialog = document.querySelector('[data-template-batch]');
  const field = selector => dialog.querySelector(selector);
  const form = field('[data-tool-form]');
  const ids = field('#template-tool-ids');
  const templates = field('#template-tool-files');
  const config = field('#template-tool-config');
  const status = field('[data-tool-status]');
  const openFolder = field('[data-tool-open-folder]');
  const errorList = field('[data-tool-errors]');
  const results = field('[data-tool-results]');
  const run = field('[data-tool-run]');
  const table = results.querySelector('table');
  let dataMode = false;
  let running = false;
  let rows = [];
  const usesParameters = () => !dataMode && field('[name=shot-style]:checked').value === 'pattern';
  function updateInputMode() {
    const parameters = usesParameters();
    field('[data-tool-config]').hidden = !parameters;
    field('[data-tool-id-label]').textContent = parameters ? '模板 ID 或关卡 ID' : '模板 ID';
    ids.placeholder = parameters ? '每行一个 ID，或粘贴两列：xkd141  15|3|100|4' : '粘贴模板 ID，每个模板只处理一次';
  }
  field('[data-tool-shot]').addEventListener('change', updateInputMode);
  openFolder.addEventListener('click', async () => {
    openFolder.disabled = true;
    try {
      await api.openScreenshotFolder();
    } catch (error) {
      errorList.replaceChildren(Object.assign(document.createElement('li'), {textContent:error.message}));
    } finally {
      openFolder.disabled = false;
    }
  });

  for (const trigger of document.querySelectorAll('[data-template-tool]')) {
    trigger.addEventListener('click', () => {
      dataMode = trigger.dataset.templateTool === 'data';
      form.reset();
      form.hidden = false;
      results.hidden = true;
      field('[data-tool-shot]').hidden = dataMode;
      field('[data-tool-auto]').checked = !dataMode;
      updateInputMode();
      field('h2').textContent = trigger.textContent;
      run.textContent = dataMode ? '查看模板数据' : '生成 PNG';
      status.textContent = '';
      openFolder.hidden = true;
      errorList.replaceChildren();
      dialog.showModal();
      ids.focus();
    });
  }
  field('[data-tool-close]').addEventListener('click', () => dialog.close());
  dialog.addEventListener('cancel', event => { if (running) event.preventDefault(); });
  field('[data-tool-back]').addEventListener('click', () => {
    results.hidden = true;
    form.hidden = false;
    ids.focus();
  });
  field('[data-tool-csv]').addEventListener('click', () => {
    const safe = value => typeof value === 'string' && /^[=+@-]/.test(value) ? "'" + value : value;
    const csv = [columns, ...rows].map(row => toCsvLine(row.map(safe))).join('\r\n');
    const link = document.createElement('a');
    link.href = URL.createObjectURL(new Blob(['\uFEFF', csv], {type:'text/csv;charset=utf-8'}));
    link.download = '关卡模板数据.csv';
    link.click();
    setTimeout(() => URL.revokeObjectURL(link.href), 1000);
  });

  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (running) return;
    running = run.disabled = field('[data-tool-close]').disabled = true;
    openFolder.hidden = true;
    errorList.replaceChildren();
    rows = [];
    const errors = [];
    const cache = new Map();
    const handledTemplates = new Set();
    let written = 0;
    let directory = '';
    try {
      const patterned = usesParameters();
      status.textContent = patterned ? '正在读取模板与配置…' : '正在读取模板…';
      const pasted = pasteRows(ids.value, errors, patterned);
      const uploads = new Map([...templates.files].map(file => [cleanId(file.name), file]));
      const text = patterned && config.files[0] ? await config.files[0].text() : '';
      const parsed = text ? importedConfig(text) : [];
      const imported = parsed ?? [];
      const importedParameters = parsed === null ? pasteRows(text, errors) : [];
      const matchingParameters = new Map();
      for (const row of importedParameters) {
        if (!matchingParameters.has(row.id)) matchingParameters.set(row.id, []);
        matchingParameters.get(row.id).push(row);
      }
      const specified = pasted.length || uploads.size
        ? [...pasted, ...[...uploads.keys()].filter(id => !pasted.some(row => row.id === id)).map(id => ({id}))]
        : importedParameters;
      const inputs = specified.flatMap(row => {
        const entries = !row.dup && matchingParameters.has(row.id) ? matchingParameters.get(row.id) : [row];
        return entries.map(entry => ({...entry, file:uploads.get(row.id)}));
      });
      const auto = patterned && field('[data-tool-auto]').checked ? await api.config().catch(error => {
        errors.push('本机关卡配置未能加载：' + error.message);
        return [];
      }) : [];
      const configRows = [...imported, ...auto.filter(row => !imported.some(entry => entry.levelId === row.levelId))];
      const source = inputs.length || errors.length ? inputs : [...new Set(imported.map(row => row.levelId))].map(id => ({id}));
      if (!source.length) throw new Error('请输入模板 ID，或上传模板文件或配置表。');
      const jobs = patterned ? source.flatMap(input => jobsFor(input, configRows))
        : [...new Map(source.map(input => [input.id, input])).values()];
      if (jobs.length > 500) throw new Error('每批最多处理 500 行，请缩小范围。');
      for (const [index, job] of jobs.entries()) {
        status.textContent = '正在处理 ' + (index + 1) + '/' + jobs.length + ' · ' + job.id;
        try {
          const key = job.file ?? job.id;
          if (!cache.has(key)) {
            if (!job.file && (!idPattern.test(job.id) || job.id.startsWith('.'))) throw new Error('模板 ID 无效。');
            const raw = job.file ? new Uint8Array(await job.file.arrayBuffer()) : await api.read(job.id);
            if (job.file) await api.upload(raw);
            cache.set(key, fromSave(decodeLevelSave(raw)));
          }
          const master = cache.get(key);
          const templateId = master.templateId || job.id;
          if (!patterned) {
            if (handledTemplates.has(templateId)) continue;
            handledTemplates.add(templateId);
          }
          if (dataMode) {
            rows.push(statistics(master, job));
          } else {
            const dup = job.dup ?? DEFAULT_DUP;
            const id = templateId.replace(/[^\w.-]/g, '_').replace(/^\.+/, '').slice(0, 172) || 'template';
            const name = patterned && job.dup ? id + '_' + [dup.a, dup.seed, dup.lucky, dup.b].join('|') : id;
            const {directory: savedDirectory} = await api.capture({
              master, dup, level: api.levelNumber(job.levelId ?? ''),
              slot:job.slot ?? 0, seedGroup:job.seedGroup ?? '', strategyGroup:job.strategyGroup ?? '',
            }, patterned, name);
            directory = savedDirectory;
            written++;
          }
        } catch (error) { errors.push(job.id + '（第 ' + (index + 1) + ' 项）：' + error.message); }
      }
      if (dataMode && rows.length) {
        renderTable(table, rows);
        form.hidden = true;
        results.hidden = false;
        field('[data-tool-csv]').focus();
      }
      status.textContent = dataMode
        ? '已生成 ' + rows.length + ' 行，每个模板一行。'
        : '已保存 ' + written + ' 张 PNG。' + (directory ? '目录：' + directory : '');
      openFolder.hidden = dataMode || written === 0;
    } catch (error) {
      status.textContent = '未完成。';
      errors.push(error.message);
    } finally {
      errorList.replaceChildren(...errors.map(message => Object.assign(document.createElement('li'), {textContent:message})));
      running = run.disabled = field('[data-tool-close]').disabled = false;
    }
  });
}
