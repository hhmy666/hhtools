'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const VM = require('../../frontend/assets/view-model.js');
const INCREASE = require('../../frontend/assets/increase-ui.js');
const ORDINARY = require('../../frontend/assets/ordinary-add-ui.js');
const source = fs.readFileSync(path.join(__dirname, '../../frontend/assets/app.js'), 'utf8');

// Execute the real application declarations, excluding only its DOM bootstrap.
// Rendering unrelated to these state transitions is replaced below; import,
// job creation/loading, workbench switching and tower controls remain real.
const declarations = source.slice(0, source.indexOf('\nif (window.TowerReducerParameters)'));
const plain = value => JSON.parse(JSON.stringify(value));

function imported(id, stacks = []) {
  return {import_id: `import-${id}`, files: [{file_id: `file-${id}`,
    original_name: `${id}_m.bytes`, status: 'ready', inspection: {
      template_id: id, total_tiles: 60, a_count: 60, b_count: 0,
      tower_count: stacks.length, tower_tile_count: stacks.reduce((a, b) => a + b, 0),
      stacks: stacks.map(count => ({count})), issues: [],
    }}]};
}

function job(id, mode) {
  return {job_id: id, status: 'succeeded', revision: 1, templates: {
    [`tpl-${id}`]: {key: `tpl-${id}`, source_template_id: 'source', status: 'verified',
      parameters: {mode, naming_rule: mode === 'ordinary_add' ? 'moreB{原模板ID}' : 'lessT{原模板ID}'},
      plan_summary: {operation: mode}, artifacts: []},
  }, artifacts: []};
}

function setup(request = async () => ({})) {
  const nodes = new Map();
  let towerInputs = [];
  let step = '2';
  function makeNode(id) {
    let markup = '';
    const node = {id, value: '', dataset: {}, hidden: false, disabled: false,
      textContent: '', className: '', classList: {add() {}, remove() {}, toggle() {}},
      setAttribute() {}, removeAttribute() {}, toggleAttribute() {}, focus() {},
      querySelector() { return null; }, querySelectorAll() { return []; }};
    Object.defineProperty(node, 'innerHTML', {
      get: () => markup,
      set(value) {
        markup = value;
        if (id !== 'tower-fields') return;
        for (const input of towerInputs) nodes.delete(input.id);
        towerInputs = [];
        for (const match of value.matchAll(/<input\b[^>]*>/g)) {
          const attrs = Object.fromEntries([...match[0].matchAll(/([\w-]+)="([^"]*)"/g)]
            .map(attribute => [attribute[1], attribute[2]]));
          if (!attrs['data-tower-file']) continue;
          const input = makeNode(attrs.id);
          input.value = attrs.value || '';
          input.max = attrs.max;
          input.dataset.towerFile = attrs['data-tower-file'];
          input.dataset.towerIndex = attrs['data-tower-index'];
          nodes.set(input.id, input);
          towerInputs.push(input);
        }
      },
    });
    return node;
  }
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, makeNode(id));
    return nodes.get(id);
  };
  const storage = new Map();
  const batchImport = {createSession: options => ({...options}), run: async () => imported('ordinary')};
  const context = {
    AbortController, structuredClone, clearTimeout() {}, setTimeout() {},
    document: {getElementById: node,
      querySelector: selector => selector === '[data-flow]:not([hidden])' ? {dataset: {flow: step}} : null,
      querySelectorAll: selector => selector === '[data-tower-file]' ? towerInputs : []},
    window: {TowerReducerViewModel: VM, TowerIncrease: INCREASE, TowerOrdinaryAdd: ORDINARY,
      TowerReducerBatchImport: batchImport, clearTimeout() {},
      localStorage: {getItem: key => storage.get(key) || '',
        setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key)}},
  };
  vm.createContext(context);
  vm.runInContext(declarations + '\nglobalThis.testState = state;', context);
  context.apiRequest = request;
  context.showToast = () => {};
  context.showFeedback = () => {};
  context.clearFeedback = () => {};
  context.clearInvalid = () => {};
  context.renderImportResponse = () => {};
  context.renderJob = () => {};
  context.showBatchEditor = () => { context.testState.activeOverride = ''; };
  context.updateParameterSummaries = () => {};
  context.updateImportControls = () => {};
  context.validateConfiguration = () => true;
  context.schedulePoll = () => {};
  context.routeTo = () => {};
  context.showStep = value => { step = String(value); };
  node('source-repository').value = 'tile';
  node('export-naming-rule').value = 'lessT{原模板ID}';
  node('tower-mode').value = 'auto';
  node('placement-strategy').value = 'top_first';
  node('target-ratio').value = '25';
  node('min-target-ratio').value = '0';
  node('layer-limit').value = '20';
  node('max-new-layers').value = '2';
  return {context, state: context.testState, node, batchImport};
}

function seedTower(harness, stacks = []) {
  const {context, state, node} = harness;
  const files = VM.normalizeImported(imported('tower', stacks));
  state.importId = files.importId;
  state.importedFiles = files.files;
  state.mode = 'reduce';
  state.workbenchGroup = 'tower';
  node('template-ids').value = 'tower';
  context.renderConfigurationList(files.files);
}

async function importFile(harness, id) {
  const {context, state, batchImport} = harness;
  state.importMode = 'files';
  state.selectedFiles = [{name: `${id}_m.bytes`, size: 1}];
  batchImport.run = async () => imported(id);
  await context.importTemplates({preventDefault() {}, currentTarget: {}});
  assert.equal(state.importId, `import-${id}`, 'the new import must complete before checking preserved state');
}

test('opening cross-workbench history never saves a mixed job identity in the previous workbench', async () => {
  const harness = setup(async () => job('ordinary-job', 'ordinary_add'));
  const {context, state} = harness;
  seedTower(harness);
  state.currentJobId = 'tower-job';
  state.currentJob = VM.normalizeJob(job('tower-job', 'reduce'));
  await context.loadJob('ordinary-job');
  assert.equal(state.currentJob.id, 'ordinary-job');
  assert.equal(context.switchWorkbench('tower'), true);
  assert.equal(state.currentJob.id, 'tower-job');
  assert.equal(state.currentJobId, 'tower-job', 'the visible payload and the ID used by API requests must belong to the same job');
});

test('a delayed job-creation response cannot overwrite the other workbench', async () => {
  let finish;
  const harness = setup(() => new Promise(resolve => { finish = resolve; }));
  const {context, state} = harness;
  seedTower(harness);
  const pending = context.createJob({preventDefault() {}, currentTarget: {}});
  assert.equal(typeof finish, 'function');
  const switched = context.switchWorkbench('ordinary');
  const activeImport = state.importId;
  finish({job_id: 'tower-created', status: 'queued'});
  await pending;
  if (!switched) {
    // Blocking the switch while the submission is pending is also safe.
    assert.equal(state.workbenchGroup, 'tower');
    assert.equal(state.currentJobId, 'tower-created');
    return;
  }
  assert.equal(state.workbenchGroup, 'ordinary');
  assert.equal(state.mode, 'ordinary_add');
  assert.equal(state.importId, activeImport);
  assert.equal(state.currentJob, null, 'a tower response must not populate the ordinary workbench');
  assert.equal(state.currentJobId, '');
});

test('importing in either workbench preserves the other workbench single-template overrides', async () => {
  const harness = setup();
  const {context, state} = harness;
  seedTower(harness);
  state.overrides = {'file-tower': {ratio: 0.1}};
  state.increaseOverrides = {'file-tower': {...INCREASE.defaults(), added_tiles: 12}};
  context.switchWorkbench('ordinary');
  await importFile(harness, 'ordinary');
  context.switchWorkbench('tower');
  assert.equal(state.overrides['file-tower']?.ratio, 0.1);
  assert.equal(state.increaseOverrides['file-tower']?.added_tiles, 12);

  context.switchWorkbench('ordinary');
  state.ordinaryOverrides = {'file-ordinary': {...ORDINARY.defaults(), added_tiles: 21}};
  context.switchWorkbench('tower');
  await importFile(harness, 'tower-replacement');
  context.switchWorkbench('ordinary');
  assert.equal(state.ordinaryOverrides['file-ordinary']?.added_tiles, 21);
});

test('switching workbenches preserves edited advanced retained counts in controls and submitted options', () => {
  const harness = setup();
  const {context, state, node} = harness;
  seedTower(harness, [6, 9]);
  node('tower-mode').value = 'advanced';
  node('tower-file-tower-0').value = '3';
  node('tower-file-tower-1').value = '6';
  assert.deepEqual(plain(context.collectRetainedCounts()), {'file-tower': [3, 6]});
  assert.equal(context.switchWorkbench('ordinary'), true);
  assert.equal(context.switchWorkbench('tower'), true);
  assert.equal(state.mode, 'reduce');
  assert.equal(node('tower-file-tower-0').value, '3');
  assert.equal(node('tower-file-tower-1').value, '6');
  assert.deepEqual(plain(context.collectOverrides()['file-tower'].retained_counts), [3, 6]);
});
