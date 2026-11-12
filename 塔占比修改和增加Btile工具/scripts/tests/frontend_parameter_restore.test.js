'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function restoredMount() {
  const saved = {ticket: {action: 'calculate', taskId: 'task-saved',
    request: {request_id: 'saved-request', payload: {selection_id: 'saved-selection'}}},
    draft: {mode: 'ids', ids: 'saved-new', source: 'library', fileAssociations: 'old=saved-new',
      levelStart: '20', levelEnd: '30', rule: 'custom{原关卡ID}', ruleEdited: true,
      unifyDescription: true, description: 'saved description', preserveLevelPrefix: true,
      stripPrefix: 'noT', stripSuffix: '_new'}};
  const memory = new Map([['tower-parameter-task-v1', JSON.stringify(saved)]]);
  const storage = {getItem: key => memory.get(key), setItem: (key, value) => memory.set(key, value),
    removeItem: key => memory.delete(key)};
  const elements = new Map();
  const document = {querySelectorAll: () => [], getElementById(id) {
    if (!elements.has(id)) elements.set(id, {value: '', checked: false, hidden: false,
      innerHTML: '', classList: {toggle() {}}, setAttribute() {}, removeAttribute() {},
      addEventListener() {}, querySelector() {return {textContent: ''};}});
    return elements.get(id);
  }};
  const sandbox = {module: {exports: {}}, sessionStorage: storage, AbortController,
    setTimeout, clearTimeout};
  const filename = path.join(__dirname, '../../frontend/assets/parameter-config.js');
  vm.runInNewContext(fs.readFileSync(filename, 'utf8'), sandbox, {filename});
  const ui = sandbox.module.exports.mount({document, request: async url => {
    if (url.endsWith('/context')) return {config_ready: true, config_root: '/fixture/config',
      config_path: '/fixture/config/config_level.csv', output_root: '/fixture/output', jobs: []};
    return {task_id: 'task-saved', status: 'succeeded', phase: 'completed', total: 0, completed: 0,
      selection: {selection_id: 'saved-selection', entries: []},
      result: {calculation_id: 'saved-calculation', rows: []}};
  }});
  return {ui, elements};
}

test('restored task draft is visible before and after loading its configuration context', async () => {
  const {ui, elements} = restoredMount();
  const check = () => {
    for (const [id, key, expected] of [
      ['parameter-template-ids', 'ids', 'saved-new'],
      ['parameter-file-associations', 'fileAssociations', 'old=saved-new'],
      ['parameter-level-start', 'levelStart', '20'], ['parameter-level-end', 'levelEnd', '30'],
      ['parameter-source', 'source', 'library'], ['parameter-description', 'description', 'saved description'],
      ['parameter-strip-prefix', 'stripPrefix', 'noT'], ['parameter-strip-suffix', 'stripSuffix', '_new']]) {
      assert.equal(ui.workflow.state[key], expected, key);
      assert.equal(elements.get(id).value, expected, `${id} must expose the restored value`);
    }
    assert.equal(elements.get('parameter-unify-description').checked, true);
    assert.equal(ui.workflow.state.unifyDescription, true);
    assert.equal(elements.get('parameter-preserve-level-prefix').checked, true);
    assert.equal(ui.workflow.state.rule, 'custom{原关卡ID}');
    assert.equal(elements.get('parameter-level-id-rule').value, 'prefix');
    assert.equal(elements.get('parameter-level-id-prefix').value, 'custom');
    assert.equal(ui.workflow.state.preserveLevelPrefix, true);
  };
  check();
  await ui.workflow.loadContext();
  check();
});

test('a recovered completed task displays a full progress meter even with no count total', async () => {
  const {ui, elements} = restoredMount();
  await ui.workflow.loadContext();
  await ui.workflow.resume();
  assert.equal(ui.workflow.state.result.calculation_id, 'saved-calculation');
  assert.equal(elements.get('parameter-task-meter').max, 1);
  assert.equal(elements.get('parameter-task-meter').value, 1);
  assert.equal(elements.get('parameter-task-stage').textContent, '处理完成 · 总进度 100%');
});
