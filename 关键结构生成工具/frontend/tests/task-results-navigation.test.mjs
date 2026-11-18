import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
const source = fs.readFileSync(new URL('../assets/app.js', import.meta.url), 'utf8');
function fn(name, next, args, values) {
  const start = source.indexOf(`  ${name}`);
  assert.notEqual(start, -1);
  return Function(...args, `${source.slice(start, source.indexOf(`\n  ${next}`, start))}; return ${name.match(/function (\w+)/)[1]};`)(...values);
}
test('task history coalesces double resume clicks and never resumes a running job', async () => {
  const state = {taskControlsPending: new Set()};
  let release; let posts = 0; let reads = 0;
  const api = async (path, options) => {
    if (options?.method === 'POST') { posts++; return {}; }
  };
  const readJobSummary = async () => {
    reads++; await new Promise(resolve => { release = resolve; }); return {status:'running'};
  };
  const control = fn('async function controlTask', 'async function refreshReports',
    ['state','api','readJobSummary','refreshTasks','toast','isUnsupportedResumeActionError','syncTaskControlButtons'],
    [state, api, readJobSummary, async()=>{}, ()=>{}, ()=>false, ()=>{}]);
  const first = control('generation','job-a','resume_unfinished');
  const second = control('generation','job-a','resume_unfinished');
  assert.equal(reads,1);
  release(); await Promise.all([first,second]);
  assert.equal(posts,0);
  assert.equal(state.taskControlsPending.size,0);
});
test('candidate list and generation do not duplicate a batch roster', () => {
  const html = fs.readFileSync(new URL('../index.html', import.meta.url), 'utf8');
  assert.doesNotMatch(html, /id="(?:generation|candidate)-results"/);
  assert.doesNotMatch(source, /function renderTaskResults|data-view-result-row/);
});
test('resume history sends one mutation and releases its lock after an error', async () => {
  const state={taskControlsPending:new Set()};
  const actions=[]; const messages=[];
  const control=fn('async function controlTask','async function refreshReports',
    ['state','api','readJobSummary','refreshTasks','toast','isUnsupportedResumeActionError','syncTaskControlButtons'],
    [state,async(path,options)=>{actions.push(options.body.action); throw new Error('server unavailable');},async()=>({status:'partial'}),async()=>{},text=>messages.push(text),()=>false,()=>{}]);
  await control('generation','job-a','resume_unfinished');
  assert.deepEqual(actions,['resume_unfinished']);
  assert.deepEqual(messages,['server unavailable']);
  assert.equal(state.taskControlsPending.size,0);
});

test('paged batch details cover all 39 rows of the same task revision', async () => {
  const items = Array.from({length: 39}, (_, index) => ({template_key: `template-${index}`, candidates: Array(10).fill({})}));
  const offsets = [];
  const api = async path => {
    const url = new URL(path, 'http://localhost');
    assert.equal(url.pathname, '/jobs/job-a/result');
    assert.equal(url.searchParams.get('revision'), '4');
    const start = Number(url.searchParams.get('start'));
    offsets.push(start);
    return {job_id:'job-a', revision:4, start, total_items:39, result:{batch_seed:'12345678901234567890', items:items.slice(start, start + 3)}};
  };
  const read = fn('async function readJobDetails', 'async function pollJob', ['api'], [api]);
  const summary = {job_id:'job-a', status:'completed', revision:4, result_item_count:39};
  const result = await read(summary, () => true);
  assert.equal(result.status, 'completed');
  assert.deepEqual(result.result.items, items);
  assert.deepEqual(offsets, Array.from({length:13}, (_, index) => index * 3));
  await assert.rejects(read({...summary, result_item_count:40}, () => true), /未完整返回/);
});
test('incomplete snapshots preserve explicit choices until all work items are loaded', () => {
  const selected = new Map([['a',['a1']],['b',[]],['c',['c2']]]);
  const state = {jobId:'job-a', jobSnapshot:{status:'running',work_item_count:3},jobResult:{items:[{template_key:'a'}]}};
  const initialize = fn('function restoreCandidateSelections', 'function ensureReviewButton',
    ['state','selectionsForJob','successfulWorkItems','workKey','candidateExportEligibility','candidateId','persistSelections','recommendedCandidateId'],
    [state,()=>selected,()=>[{template_key:'a',candidates:[{id:'a1'}]}],row=>row.template_key,()=>({eligible:true}),row=>row.id,()=>{},()=>null]);
  initialize();
  assert.deepEqual([...selected], [['a',['a1']],['b',[]],['c',['c2']]]);
  state.jobSnapshot.status = 'completed';
  initialize();
  assert.deepEqual([...selected], [['a',['a1']],['b',[]],['c',['c2']]]);
  state.jobResult.items.push({template_key:'b'}, {template_key:'c'});
  initialize();
  assert.deepEqual([...selected], [['a',['a1']]]);
});

test('a stale partial status does not make a full candidate row unfinished', () => {
  const start = source.indexOf('  function itemCandidateProgress(');
  const end = source.indexOf('  function itemErrorCode(', start);
  const needsMore = Function(`${source.slice(start, end)}; return itemNeedsMoreCandidates;`)();
  const row = { status: 'partial', candidates: [{}, {}, {}, {}], target_candidate_count: 4 };
  assert.equal(needsMore(row), false);
  assert.equal(needsMore({ ...row, candidates: row.candidates.slice(0, 3) }), true);
});
