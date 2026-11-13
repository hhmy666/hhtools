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
    reads++; await new Promise(resolve => { release = resolve; }); return {status:'running'};
  };
  const control = fn('async function controlTask', 'async function refreshReports',
    ['state','api','refreshTasks','toast','isUnsupportedResumeActionError','syncTaskControlButtons'],
    [state, api, async()=>{}, ()=>{}, ()=>false, ()=>{}]);
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
    ['state','api','refreshTasks','toast','isUnsupportedResumeActionError','syncTaskControlButtons'],
    [state,async(path,options)=>{if (!options) return {status:'partial'}; actions.push(options.body.action); throw new Error('server unavailable');},async()=>{},text=>messages.push(text),()=>false,()=>{}]);
  await control('generation','job-a','resume_unfinished');
  assert.deepEqual(actions,['resume_unfinished']);
  assert.deepEqual(messages,['server unavailable']);
  assert.equal(state.taskControlsPending.size,0);
});
test('partial live snapshots preserve absent templates explicit choices', () => {
  const selected = new Map([['a',['a1']],['b',[]],['c',['c2']]]);
  const state = {jobId:'job-a', jobSnapshot:{status:'running',work_item_count:3},jobResult:{items:[{template_key:'a'}]}};
  const initialize = fn('function initializeDefaultSelections', 'function ensureReviewButton',
    ['state','selectionsForJob','successfulWorkItems','workKey','candidateExportEligibility','candidateId','persistSelections','recommendedCandidateId'],
    [state,()=>selected,()=>[{template_key:'a',candidates:[{id:'a1'}]}],row=>row.template_key,()=>({eligible:true}),row=>row.id,()=>{},()=>null]);
  initialize();
  assert.deepEqual([...selected], [['a',['a1']],['b',[]],['c',['c2']]]);
  state.jobSnapshot.status = 'completed';
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
