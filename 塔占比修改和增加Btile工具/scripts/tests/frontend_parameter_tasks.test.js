'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const P = require('../../frontend/assets/parameter-config.js');
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');

test('workspace restart cannot discard an uncertain write after HTTP waiting has stopped',()=>{
  const app=fs.readFileSync(path.join(__dirname,'../../frontend/assets/app.js'),'utf8');
  const start=app.indexOf('function restartWorkspace('),end=app.indexOf('\nfunction ',start+1),messages=[];
  const context={window:{towerParameterUI:{workflow:{state:{pending:''},writeLocked:()=>true},reset(){throw new Error('discarded uncertain write');}}},showFeedback:(_id,message)=>messages.push(message),showToast:message=>messages.push(message)};
  assert.doesNotThrow(()=>vm.runInNewContext(app.slice(start,end)+'\nrestartWorkspace();',context));
  assert.equal(messages.length,2);assert.match(messages[0],/等待写入结果/);
});

test('calculate uploads selected files and prepares them automatically through a background task', async () => {
  const calls = [], progress = [];
  const flow = P.createWorkflow({background: true, pollDelay: 0,
    upload: async () => ({import_id: 'import-files'}),
    onChange: state => progress.push(state.progress),
    request: async (url, options) => {
      calls.push({url, json: options?.json});
      if (url.endsWith('/start')) return {task_id: 'parameter-task-1', status: 'running', phase: 'resolving', completed: 0, total: 76, current_filename: 'new_m.bytes'};
      return {task_id: 'parameter-task-1', status: 'succeeded', phase: 'completed',
        selection: {selection_id: 'selection-1', entries: []}, result: {calculation_id: 'calc-1', rows: []}};
    }});
  flow.input({mode: 'files', files: [{name: 'new_m.bytes', size: 100}]});
  await flow.calculate();
  assert.equal(calls[0].json.action, 'calculate');
  assert.deepEqual(calls[0].json.payload.source, {mode: 'files', import_id: 'import-files'});
  assert.equal(flow.state.result.calculation_id, 'calc-1');
  assert.equal(flow.state.selection.selection_id, 'selection-1');
  assert.ok(progress.some(value => /0.*76.*new_m/.test(value)));
});

test('lost start response retries with the same request identity and never repeats uploaded files', async () => {
  let uploads = 0; const ids = [];
  const flow = P.createWorkflow({background: true, pollDelay: 0,
    upload: async () => { uploads++; return {import_id: 'import-files'}; },
    request: async (url, options) => {
      ids.push(options.json.request_id);
      if (ids.length === 1) throw new Error('connection lost');
      return {task_id: 'parameter-task-1', status: 'succeeded', selection: {selection_id: 's'}, result: {calculation_id: 'c', rows: []}};
    }});
  flow.input({mode: 'files', files: [{name: 'new_m.bytes', size: 100}]});
  await flow.calculate();
  assert.equal(uploads, 1);
  assert.equal(ids.length, 2);
  assert.equal(ids[0], ids[1]);
  assert.equal(flow.state.result.calculation_id, 'c');
});

test('a late task response cannot restore stale selection after changing template input', async () => {
  let finish;
  const flow = P.createWorkflow({background: true, pollDelay: 0,
    request: () => new Promise(resolve => { finish = resolve; })});
  flow.input({ids: 'first'});
  const pending = flow.calculate(); await Promise.resolve(); await Promise.resolve();
  flow.input({ids: 'second'});
  finish({task_id: 'old', status: 'succeeded', selection: {selection_id: 'old'}, result: {calculation_id: 'old', rows: []}});
  await pending;
  assert.equal(flow.state.selection, null);
  assert.equal(flow.state.result, null);
  assert.equal(flow.state.taskTicket, null);
});

test('page reload resumes the existing calculation without asking for its files again', async () => {
  const memory = new Map(), storage = {getItem: key => memory.get(key), setItem: (key, value) => memory.set(key, value), removeItem: key => memory.delete(key)};
  const first = P.createWorkflow({background: true, pollDelay: 0, storage,
    upload: async () => ({import_id: 'import-one'}),
    request: async () => { throw Object.assign(new Error('offline'), {status: 503}); }});
  first.input({mode: 'files', files: [{name: 'one_m.bytes', size: 100}]});
  await first.calculate();
  const calls = [];
  const second = P.createWorkflow({background: true, pollDelay: 0, storage,
    request: async (url, options) => { calls.push({url, options}); return {task_id: 'existing', status: 'succeeded', selection: {selection_id: 's'}, result: {calculation_id: 'c', rows: []}}; }});
  await second.resume();
  assert.equal(second.state.result.calculation_id, 'c');
  assert.equal(calls[0].options.json.payload.source.import_id, 'import-one');
});

test('an uncertain configuration write retains its plan and blocks input reset until status recovery', async () => {
  let unavailable = true;
  const flow = P.createWorkflow({background: true, pollDelay: 0, request: async (_url, options) => {
    const action = options?.json?.action;
    if (action === 'commit' && unavailable) throw Object.assign(new Error('offline'), {status: 503});
    return {task_id: 'task', status: 'succeeded', result: {status: 'committed', written: true}};
  }});
  flow.state.selection = {selection_id: 's'};
  flow.state.result = {calculation_id: 'c', rows: []};
  flow.state.plan = {plan_id: 'p', status: 'ready'};
  await flow.commit();
  flow.input({ids: 'replacement'}); flow.reset();
  assert.equal(flow.state.plan.plan_id, 'p');
  assert.equal(flow.state.taskTicket.action, 'commit');
  unavailable = false;
  await flow.resume();
  assert.equal(flow.state.committed.status, 'committed');
});

test('recalculation keeps the automatically chosen increase LevelId rule', async () => {
  let body;
  const flow = P.createWorkflow({background: true, pollDelay: 0, request: async (_url, options) => {
    body = options.json.payload;
    return {task_id: 'task', status: 'succeeded', result: {calculation_id: 'calc', rows: []}};
  }});
  flow.state.selection = {selection_id: 'selection'};
  flow.state.rule = 'moreB{原关卡ID}';
  await flow.calculate();
  assert.equal(body.level_id_rule, 'moreB{原关卡ID}');
});

test('recovering a write error retains actual backup evidence without claiming completion', async () => {
  const flow = P.createWorkflow({background: true, pollDelay: 0, request: async () => ({task_id: 'task', status: 'succeeded',
    result: {status: 'error', written: true, backup_path: '/tmp/actual-backup.csv', message: 'readback changed'}})});
  flow.state.taskTicket = {action: 'commit', request: {request_id: 'parameter-commit-test', payload: {plan_id: 'p'}}};
  await flow.resume();
  assert.equal(flow.state.committed, null);
  assert.equal(flow.state.commitFailure.backup_path, '/tmp/actual-backup.csv');
  assert.match(flow.state.error, /actual-backup/);
});
