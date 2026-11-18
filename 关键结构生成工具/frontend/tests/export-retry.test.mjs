import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const source = readFileSync(new URL('../assets/app.js', import.meta.url), 'utf8');
const css = readFileSync(new URL('../assets/styles.css', import.meta.url), 'utf8');
const segment = (start, end) => source.slice(source.indexOf(`  ${start}`), source.indexOf(`  ${end}`));

test('a rejected launch keeps the selection and resubmits it with the same key', async () => {
  const button = { dataset: {}, disabled: false, textContent: '导出', innerHTML: '导出' };
  const failure = { textContent: '' };
  const choices = new Map([['demo::s5', ['candidate_1234']]]);
  const state = { jobId: 'job-demo', selectedCandidates: choices, exportLoadToken: 0, exportLaunches: new Map() };
  const requests = [];
  let nextKey = 0;
  const item = { template_key: 'demo', plan_id: 's5', candidates: [{ id: 'candidate_1234' }, { id: 'unselected' }] };
  const unrelated = { template_key: 'other', plan_id: 's5', get candidates() {
    throw new Error('export should not inspect this row');
  } };
  const run = Function('state', '$', 'workKey', 'candidateId',
    'candidateExportEligibility', 'uuid', 'api', 'toast', 'renderExportSelection',
    `${segment('function setBusy', 'function syncAdjustmentRunControls')}
     ${segment('function selectedExportRows', 'function renderExportSelection')}
     ${segment('async function exportSelected', 'function exportPhase')}
     return exportSelected;`)(
    { ...state, jobResult: { items: [unrelated, item] } },
    selector => selector === '#export-selected' ? button : failure,
    row => `${row.template_key}::${row.plan_id}`, candidate => candidate.id,
    () => { throw new Error('export should not reevaluate recommendation'); },
    () => `key-${++nextKey}`, async (path, options) => {
      requests.push(options.body);
      throw Object.assign(new Error('服务未响应'), { status: 400 });
    }, () => {}, () => {},
  );
  await run();
  assert.equal(button.disabled, false);
  assert.match(failure.textContent, /服务未响应/);
  await run();
  assert.equal(requests.length, 2);
  assert.deepEqual(requests[0].selections, [
    { template_key: 'demo', resolved_plan_id: 's5', candidate_id: 'candidate_1234', manual_evidence_override: true },
  ]);
  assert.deepEqual(requests[1], requests[0]);
  assert.deepEqual([...choices], [['demo::s5', ['candidate_1234']]]);
});

test('a failed run can show its retry action even though progress is terminal', () => {
  assert.equal(/\.export-progress\.is-terminal\s+\.export-control-row\s*\{/.test(css), false);
});

test('a stalled response body releases the launch when its deadline expires', async () => {
  let responseSignal;
  const fetch = async (url, request) => {
    responseSignal = request.signal;
    return {
      ok: true,
      json: () => new Promise((resolve, reject) => {
        request.signal.addEventListener('abort', () => reject(request.signal.reason));
      }),
    };
  };
  const api = Function('state', 'API_ROOT', 'window', 'fetch',
    `${segment('async function api', 'function newPlanModel')}; return api;`)(
    { csrf: 'token' }, '/api/v1', { setTimeout, clearTimeout }, fetch,
  );
  await assert.rejects(api('/jobs/demo/exports', { method: 'POST', timeoutMs: 5 }), /超时/);
  assert.equal(responseSignal.aborted, true);
});

test('malformed JSON never becomes an empty successful response', async () => {
  const api = Function('state', 'API_ROOT', 'window', 'fetch',
    `${segment('async function api', 'function newPlanModel')}; return api;`)(
    { csrf: 'token' }, '/api/v1', { setTimeout, clearTimeout },
    async () => ({ ok: true, json: async () => { throw new SyntaxError('truncated'); } }),
  );
  await assert.rejects(api('/jobs/demo'), /无法连接|响应/);
});

test('incomplete task snapshots cannot reset the displayed task', () => {
  const render = Function('state',
    `${segment('function renderGenerationProgress', 'function jobSearchBudget')}; return renderGenerationProgress;`)(
    { jobId: 'job-demo', jobSnapshot: { job_id: 'job-demo', status: 'partial' } },
  );
  assert.doesNotThrow(() => render({}));
  assert.doesNotThrow(() => render({ job_id: 'other', status: 'completed' }));
});
