'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const P = require('../../frontend/assets/parameter-config.js');
const BatchImport = require('../../frontend/assets/batch-import.js');
const row = (id, status = 'ready', changes = true) => ({row_id: id, level_indexes: [Number(id) || 12], original_level_id: `level${id}`, original_template_id: `old${id}`, new_template_id: `new${id}`, original_parameters: Array(10).fill('7,3,seed,luck'), new_parameters: Array(10).fill(changes ? '8,2,seed,luck' : '7,3,seed,luck'), status});
function fixture(handler) {
  const calls = [];
  const workflow = P.createWorkflow({request: async (url, options) => {
    calls.push({url, body: options?.json, signal: options?.signal});
    if (handler) return handler(url, options, calls);
    if (url.endsWith('/resolve')) return {selection_id: 'selection', entries: []};
    if (url.endsWith('/calculate')) return {calculation_id: 'calc', rows: [row('1'), row('2', 'unchanged', false), row('3', 'failed')], config_path: '/config.csv'};
    if (url.endsWith('/preview')) return {plan_id: 'plan', rows: [], diff: '+ new'};
    if (url.endsWith('/commit')) return {status: 'committed', rows: []};
    return {config_ready: true, jobs: []};
  }});
  workflow.input({ids: 'old=new'});
  return {workflow, calls};
}
test('ID association names the new template and source explicitly', () => {
  assert.deepEqual(P.parseTemplateInput('old=new\nnew2_m.bytes\nnew2'), {template_ids: ['new', 'new2'], source_map: {new: 'old'}});
  assert.throws(() => P.parseTemplateInput('old='), /原ID=新ID/);
});
test('resolve uses output source and preserves explicit associations', async () => {
  const {workflow, calls} = fixture(); await workflow.resolve();
  assert.deepEqual(calls[0].body, {mode: 'ids', source: 'output', template_ids: ['new'], source_map: {new: 'old'}});
});
test('calculation uses optional range and initial valid-row selection', async () => {
  const {workflow, calls} = fixture(); await workflow.resolve(); await workflow.calculate();
  assert.equal(calls[1].body.level_start, null); assert.equal(calls[1].body.level_end, null);
  assert.deepEqual([...workflow.state.selected], ['1', '2']);
  assert.deepEqual(P.changedRows(workflow.state.result.rows).map(r => r.row_id), ['1']);
});
test('global search reaches off-page rows and paging clamps after filtering', () => {
  const rows = Array.from({length: 71}, (_, i) => row(String(i + 1)));
  const filtered = P.filterRows(rows, 'new71');
  assert.equal(filtered[0].row_id, '71');
  assert.deepEqual(P.pageRows(filtered, 4, 20), {rows: filtered, page: 1, pages: 1, total: 1, start: 1, end: 1});
});
test('cross-page selection reports all-result scope and excludes failed rows', async () => {
  const {workflow} = fixture(); await workflow.resolve(); await workflow.calculate();
  workflow.selectAll(false); workflow.selectAll(true);
  assert.deepEqual([...workflow.state.selected], ['1', '2']);
  workflow.toggle('3', true); assert.equal(workflow.state.selected.has('3'), false);
  workflow.selectPage([row('1')], false); assert.deepEqual([...workflow.state.selected], ['2']);
});
test('custom LevelId and intentional empty description invalidate preview', async () => {
  const {workflow, calls} = fixture(); await workflow.resolve(); await workflow.calculate(); await workflow.preview();
  workflow.delivery({rule: 'new_{新模板ID}', unifyDescription: true, description: ''});
  assert.equal(workflow.state.plan, null); await workflow.preview();
  assert.equal(calls.at(-1).body.level_id_rule, 'new_{新模板ID}'); assert.equal(calls.at(-1).body.description, '');
});
test('XLSX snapshot uses edited metadata without clearing cross-page selection', async () => {
  const {workflow, calls} = fixture(); await workflow.resolve(); await workflow.calculate(); workflow.toggle('2', false);
  workflow.delivery({rule: 'report_{原关卡ID}', unifyDescription: true, description: '新备注'});
  const result = await workflow.exportResult(); assert.equal(result.calculation_id, 'calc');
  assert.equal(calls.at(-1).body.description, '新备注'); assert.equal(calls.at(-1).body.level_id_rule, 'report_{原关卡ID}');
  assert.deepEqual([...workflow.state.selected], ['1']);
});
test('new calculation row IDs preserve selection by the original configuration identity', async () => {
  let calculated = 0;
  const {workflow} = fixture(url => {
    if (url.endsWith('/resolve')) return {selection_id: 'selection'};
    if (url.endsWith('/calculate')) { calculated++; return {calculation_id: `calc${calculated}`, rows: [row('1'), row('2')].map(r => ({...r, row_id: `attempt${calculated}-${r.row_id}`}))}; }
  });
  await workflow.resolve(); await workflow.calculate(); workflow.toggle('attempt1-2', false);
  await workflow.exportResult(); assert.deepEqual([...workflow.state.selected], ['attempt2-1']);
});
test('a write followed by a validation error keeps the actual backup and recovery evidence', async () => {
  const {workflow} = fixture(url => {
    if (url.endsWith('/resolve')) return {selection_id: 'selection'};
    if (url.endsWith('/calculate')) return {calculation_id: 'calc', rows: [row('1')]};
    if (url.endsWith('/preview')) return {plan_id: 'plan'};
    return {status: 'error', written: true, backup_path: '/backup/config.csv', message: '读回检查失败'};
  });
  await workflow.resolve(); await workflow.calculate(); await workflow.preview(); await workflow.commit();
  assert.equal(workflow.state.committed, null); assert.equal(workflow.state.commitFailure.written, true);
  assert.match(workflow.state.error, /已发生写入/); assert.match(workflow.state.error, /backup\/config.csv/);
});
test('failed operation retains input and retries with a bounded pending state', async () => {
  let fail = true;
  const {workflow} = fixture((url) => {
    if (fail) throw new Error('读取失败');
    return {selection_id: 'retry', entries: []};
  });
  await workflow.resolve(); assert.equal(workflow.state.pending, ''); assert.match(workflow.state.error, /读取失败/);
  assert.equal(workflow.state.ids, 'old=new'); fail = false;
  await workflow.resolve(); assert.equal(workflow.state.selection.selection_id, 'retry'); assert.equal(workflow.state.error, '');
});
test('late response cannot overwrite a changed selection or a reset', async () => {
  let complete;
  const {workflow, calls} = fixture(() => new Promise(resolve => { complete = resolve; }));
  const pending = workflow.resolve(); assert.equal(workflow.state.pending, 'resolve');
  workflow.input({ids: 'newer'}); assert.equal(calls[0].signal.aborted, true);
  complete({selection_id: 'stale'}); await pending; assert.equal(workflow.state.selection, null);
  const again = workflow.resolve(); workflow.reset(); complete({selection_id: 'stale2'}); await again;
  assert.equal(workflow.state.selection, null); assert.equal(workflow.state.ids, '');
});
test('settings context invalidation aborts an earlier context read and cannot restore old paths', async () => {
  const deferred = [], calls = [];
  const workflow = P.createWorkflow({request: (url, options) => {
    calls.push({url, signal: options.signal});
    return new Promise(resolve => { deferred.push(resolve); });
  }});
  const earlier = workflow.loadContext();
  workflow.contextChanged();
  assert.equal(calls[0].signal.aborted, true);
  const current = workflow.loadContext();
  deferred[1]({config_root: '/new', config_path: '/new/config_level.csv', output_root: '/new-output', config_ready: true, jobs: []});
  await current;
  deferred[0]({config_root: '/old', config_path: '/old/config_level.csv', output_root: '/old-output', config_ready: true, jobs: []});
  await earlier;
  assert.equal(workflow.state.context.config_root, '/new'); assert.equal(workflow.state.context.output_root, '/new-output');
});
test('a changed context response during commit cannot abort or discard committed evidence', async () => {
  let contextReads = 0, finishContext, finishCommit; const calls = [];
  const oldContext = {config_root: '/old', config_path: '/old/config_level.csv', output_root: '/old-output', config_ready: true, jobs: []};
  const workflow = P.createWorkflow({request: (url, options) => {
    calls.push({url, signal: options.signal});
    if (url.endsWith('/context')) return ++contextReads === 1 ? Promise.resolve(oldContext) : new Promise(resolve => { finishContext = resolve; });
    if (url.endsWith('/resolve')) return Promise.resolve({selection_id: 'selection'});
    if (url.endsWith('/calculate')) return Promise.resolve({calculation_id: 'calc', config_path: oldContext.config_path, rows: [row('1')]});
    if (url.endsWith('/preview')) return Promise.resolve({plan_id: 'plan', status: 'ready'});
    return new Promise(resolve => { finishCommit = resolve; });
  }});
  await workflow.loadContext(); workflow.input({ids: 'new'}); await workflow.resolve(); await workflow.calculate(); await workflow.preview();
  const contextRead = workflow.loadContext(); const commit = workflow.commit(); const commitSignal = calls.at(-1).signal;
  finishContext({...oldContext, config_root: '/new', config_path: '/new/config_level.csv', output_root: '/new-output'}); await contextRead;
  assert.equal(commitSignal.aborted, false); assert.equal(workflow.state.pending, 'commit');
  assert.equal(workflow.state.plan.plan_id, 'plan'); assert.equal(workflow.state.result.calculation_id, 'calc');
  finishCommit({status: 'committed', written: true, backup_path: '/backup/config.csv', config_path: oldContext.config_path}); await commit;
  assert.equal(workflow.state.committed.backup_path, '/backup/config.csv'); assert.equal(workflow.state.result.config_path, oldContext.config_path);
  assert.equal(workflow.state.context.config_root, '/new'); assert.equal(workflow.state.selection, null);
});
test('saving settings during commit cancels only the context read and preserves the write outcome', async () => {
  let finishContext, finishCommit; const calls = [];
  const workflow = P.createWorkflow({request: (url, options) => {
    calls.push({url, signal: options.signal});
    if (url.endsWith('/context')) return new Promise(resolve => { finishContext = resolve; });
    if (url.endsWith('/resolve')) return Promise.resolve({selection_id: 'selection'});
    if (url.endsWith('/calculate')) return Promise.resolve({calculation_id: 'calc', rows: [row('1')]});
    if (url.endsWith('/preview')) return Promise.resolve({plan_id: 'plan', status: 'ready'});
    return new Promise(resolve => { finishCommit = resolve; });
  }});
  workflow.input({ids: 'new'}); await workflow.resolve(); await workflow.calculate(); await workflow.preview();
  const contextRead = workflow.loadContext(); const contextSignal = calls.at(-1).signal;
  const commit = workflow.commit(); const commitSignal = calls.at(-1).signal; workflow.contextChanged();
  assert.equal(contextSignal.aborted, true); assert.equal(commitSignal.aborted, false);
  finishContext({config_root: '/old', config_ready: true}); await contextRead;
  finishCommit({status: 'committed', written: true, backup_path: '/backup/actual.csv'}); await commit;
  assert.equal(workflow.state.committed.backup_path, '/backup/actual.csv'); assert.equal(workflow.state.result.calculation_id, 'calc');
  assert.equal(workflow.state.context, null); assert.equal(workflow.state.selection, null);
});
test('files use bounded multipart import rather than base64 conversion', async () => {
  const {workflow, calls} = fixture(); let uploadCalls = 0;
  workflow.upload = async ({files, signal}) => { uploadCalls++; assert.equal(files.length, 1); assert.equal(signal.aborted, false); return {import_id: 'import-test'}; };
  workflow.input({mode: 'files', files: [{name: 'new_m.bytes', size: 100}]}); await workflow.resolve();
  assert.equal(uploadCalls, 1); assert.deepEqual(calls[0].body, {mode: 'files', import_id: 'import-test'});
});
test('files can send explicit original-to-new associations by internal template ID', async () => {
  const {workflow, calls} = fixture(); let uploads = 0;
  workflow.upload = async () => { uploads++; return {import_id: 'import-files'}; };
  workflow.input({mode: 'files', files: [{name: 'arbitrary_filename_m.bytes', size: 100}], fileAssociations: 'hr0029=custom0029\nhr0044=custom0044'});
  await workflow.resolve();
  assert.equal(uploads, 1);
  assert.deepEqual(calls[0].body, {mode: 'files', import_id: 'import-files', source_map: {custom0029: 'hr0029', custom0044: 'hr0044'}});
});
test('a changed file association invalidates the old result and ignores its late response', async () => {
  let finish;
  const {workflow, calls} = fixture(() => new Promise(resolve => { finish = resolve; }));
  workflow.upload = async () => ({import_id: 'import-files'});
  workflow.input({mode: 'files', files: [{name: 'custom_m.bytes', size: 100}], fileAssociations: 'original=custom'});
  const pending = workflow.resolve(); await Promise.resolve();
  workflow.input({fileAssociations: 'correct=custom'});
  assert.equal(calls[0].signal.aborted, true); finish({selection_id: 'stale'}); await pending;
  assert.equal(workflow.state.selection, null); assert.equal(workflow.state.fileAssociations, 'correct=custom');
});
test('file association input must use pairs and is validated before reading files', async () => {
  const {workflow, calls} = fixture(); let uploads = 0;
  workflow.upload = async () => { uploads++; return {import_id: 'import-files'}; };
  workflow.input({mode: 'files', files: [{name: 'custom_m.bytes', size: 100}], fileAssociations: 'custom'});
  await workflow.resolve(); assert.equal(uploads, 0); assert.equal(calls.length, 0); assert.match(workflow.state.error, /原ID=新ID/);
});
test('oversized upload remains a rejected entry while valid files resolve together', async () => {
  const calls = [];
  const request = async (url, options) => {
    calls.push(url);
    if (url === '/api/parameters/resolve') return {selection_id: 'selection', entries: [{new_template_id: 'large', status: 'failed', message: '单文件最多32 MB'}, {new_template_id: 'valid', status: 'ready'}]};
    if (url === '/api/imports/merge') return {import_id: 'import-' + '3'.repeat(32), files: [{status: 'failed'}, {status: 'ready'}]};
    return {import_id: 'import-' + (url === '/api/import-rejections' ? '1' : '2').repeat(32), files: [{status: url === '/api/import-rejections' ? 'failed' : 'ready'}]};
  };
  const workflow = P.createWorkflow({request, upload: ({files, signal}) => BatchImport.run(BatchImport.createSession({items: files, mode: 'files'}), {request, signal, formFactory: () => ({append() {}})})});
  workflow.input({mode: 'files', files: [{name: 'large_m.bytes', size: P.MAX_FILE_BYTES + 1}, {name: 'valid_m.bytes', size: 500}]});
  await workflow.resolve();
  assert.deepEqual(calls, ['/api/import-rejections', '/api/import', '/api/imports/merge', '/api/parameters/resolve']);
  assert.equal(workflow.state.selection.entries.length, 2); assert.equal(workflow.state.selection.entries[1].status, 'ready');
});
test('missing new parameters from a failed row never become a fabricated comparison', () => {
  const failed = {...row('failed', 'failed'), new_parameters: []};
  assert.deepEqual(P.changedRows([failed]), []); assert.equal(P.renderComparisonRows([failed]), '');
});
test('commit requires the current preview and cannot duplicate a pending submit', async () => {
  const {workflow, calls} = fixture(); await workflow.resolve(); await workflow.calculate();
  await workflow.commit(); assert.equal(calls.some(c => c.url.endsWith('/commit')), false);
  await workflow.preview(); await workflow.commit(); assert.equal(workflow.state.committed.status, 'committed');
  assert.equal(workflow.state.plan, null);
});
test('blocked preview is reviewable but cannot send a commit request', async () => {
  const {workflow, calls} = fixture(url => {
    if (url.endsWith('/resolve')) return {selection_id: 'selection'};
    if (url.endsWith('/calculate')) return {calculation_id: 'calc', rows: [row('1')]};
    return {plan_id: 'plan', status: 'blocked', rows: [{...row('1'), status: 'blocked', message: '目标内容不同'}]};
  });
  await workflow.resolve(); await workflow.calculate(); await workflow.preview(); await workflow.commit();
  assert.equal(workflow.state.plan.rows[0].message, '目标内容不同');
  assert.equal(calls.some(call => call.url.endsWith('/commit')), false); assert.match(workflow.state.error, /冲突/);
});
test('restart cannot reset or abort a configuration commit that is still pending', async () => {
  let finish;
  const {workflow, calls} = fixture(url => {
    if (url.endsWith('/resolve')) return {selection_id: 'selection'};
    if (url.endsWith('/calculate')) return {calculation_id: 'calc', rows: [row('1')]};
    if (url.endsWith('/preview')) return {plan_id: 'plan', status: 'ready'};
    return new Promise(resolve => { finish = resolve; });
  });
  await workflow.resolve(); await workflow.calculate(); await workflow.preview(); const commit = workflow.commit();
  workflow.reset(); workflow.cancel();
  assert.equal(workflow.state.pending, 'commit'); assert.equal(workflow.state.selection.selection_id, 'selection');
  assert.equal(calls.at(-1).signal.aborted, false);
  const app = fs.readFileSync(path.join(__dirname, '../../frontend/assets/app.js'), 'utf8');
  const start = app.indexOf('function restartWorkspace('); const end = app.indexOf('\nfunction ', start + 1);
  const messages = []; let resetCalled = false;
  vm.runInNewContext(app.slice(start, end) + '\nrestartWorkspace();', {
    window: {towerParameterUI: {workflow, reset() { resetCalled = true; }}},
    showFeedback: (_id, message) => messages.push(message), showToast: message => messages.push(message),
  });
  assert.equal(resetCalled, false); assert.equal(messages.length, 2); assert.match(messages[0], /等待写入结果/);
  finish({status: 'committed', written: true}); await commit;
  assert.equal(workflow.state.committed.status, 'committed');
});
test('mapping and changed comparison keep ten parameters in matching columns', () => {
  const item = row('1'); const html = P.renderMappingRows([item], new Set(['1']));
  assert.equal((html.match(/class="parameter-value"/g) || []).length, 10);
  assert.match(html, /<td class="parameter-id">new1<\/td>/);
  const comparison = P.renderComparisonRows([item, row('2', 'unchanged', false)]);
  assert.equal((comparison.match(/class="parameter-value/g) || []).length, 20);
  assert.match(comparison, /旧参数/); assert.match(comparison, /新参数/); assert.doesNotMatch(comparison, /level2/);
});
