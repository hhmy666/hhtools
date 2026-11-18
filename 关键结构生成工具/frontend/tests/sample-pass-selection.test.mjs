import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
const source=fs.readFileSync(new URL('../assets/app.js',import.meta.url),'utf8');
function helpers(){
 const begin=source.indexOf('  function candidateSamplePassModel(');
 assert.notEqual(begin,-1,'sample pass ranking is available');
 const end=source.indexOf('  function candidateRiskWarnings(',begin);
 return Function('difficultyStrategyRows','candidateDifficultyReport','candidateSummary','experienceAnalysis','candidateId','displayCandidateIndex','candidateExportEligibility',`${source.slice(begin,end)}return {candidateSamplePassModel,candidateRecommendation,orderedCandidates};`)(
  report=>['随机策略','贪心策略','局部最优','解锁优先','压力测试'].map((label,i)=>({label,pass:report.rates?.[i]??null})),
  candidate=>({rates:candidate.rates}),candidate=>candidate.summary||{},candidate=>({hardStatus:candidate.hardStatus||'proved'}),candidate=>candidate.id,candidate=>candidate.index,()=>({eligible:true}),
 );
}
const candidate=(id,rates,index=1)=>({id,rates,index});
test('five pass rates determine a single recommendation by sum rather than peak or old rank',()=>{
 const h=helpers();const a=candidate('a',[.1,.1,.1,0,0],1),b=candidate('b',[0,.3,0,0,0],2),c=candidate('c',[.1,.3,0,0,0],3);
 const item={candidates:[a,b,c],recommended_candidate_ids:['a']};
 assert.equal(h.candidateRecommendation(item,c).recommended,true);
 assert.equal(h.candidateRecommendation(item,a).recommended,false);
 assert.equal(h.orderedCandidates(item)[0].id,'c');
 item.candidates=[a,b];
 assert.deepEqual(item.candidates.filter(value=>h.candidateRecommendation(item,value).recommended).map(value=>value.id),['a']);
});
test('zero, missing and invalid samples never recommend, while raw hard failures remain excluded',()=>{
 const h=helpers();
 for(const rates of [[0,0,0,0,0],[null,.8,0,0,0],[NaN,0,0,0,0],[-.1,.8,0,0,0]]){
  const a=candidate('a',rates);assert.equal(h.candidateRecommendation({candidates:[a]},a).recommended,false);
 }
 const failed={...candidate('bad',[1,1,1,1,1]),summary:{passed:false}};
 const good=candidate('good',[0,.1,0,0,0]);
 assert.equal(h.candidateRecommendation({candidates:[failed,good]},good).recommended,true);
});
test('opening the candidate list restores existing picks and leaves new items unselected', () => {
  const begin = source.indexOf('  function restoreCandidateSelections(');
  const end = source.indexOf('  function bulkCandidateWinner(', begin);
  const selected = new Map([['manual', ['b']], ['cleared', []]]);
  const candidates = [candidate('a', [.1,0,0,0,0]), candidate('b', [.2,0,0,0,0])];
  const items = ['new', 'manual', 'cleared'].map(key => ({key, candidates}));
  const state = {jobId: 'job'};
  const restore = Function('state','selectionsForJob','successfulWorkItems','workKey','candidateExportEligibility','candidateId','persistSelections',
    `${source.slice(begin,end)};return restoreCandidateSelections;`)(
      state, () => selected, () => items, item => item.key, () => ({eligible: true}), item => item.id, () => {}
    );
  restore();
  assert.deepEqual([...selected], [['manual', ['b']], ['cleared', []]]);
});

test('bulk selection uses inclusive bounds, random ties and zero-rate strategy fallback', () => {
  const begin = source.indexOf('  function bulkCandidateWinner(');
  const end = source.indexOf('  async function runBulkCandidateSelection(', begin);
  const winner = Function('candidateSamplePassModel', `${source.slice(begin,end)};return bulkCandidateWinner;`)(helpers().candidateSamplePassModel);
  const row = (id, rate, sample = 0) => ({id, rate, candidate: candidate(id, [sample,0,0,0,0])});
  const rows = [row('lower', .1), row('upper', .35), row('outside', .36), row('unknown', null, .9)];
  assert.equal(winner(rows, .1, .35), 'upper');
  assert.equal(winner(rows, .36, .36), 'outside');
  assert.equal(winner(rows, .02, .09), null);
  assert.equal(winner([row('above-a', .6), row('above-b', .31)], 0, .3), 'above-b');
  assert.equal(winner([row('below', .1), row('above', .35)], .2, .3), null);
  assert.equal(winner([row('above', .35), row('unknown', null)], 0, .3), null);
  assert.equal(winner([row('tie-a', .2, .9), row('tie-b', .2)], .2, .2, () => .9), 'tie-b');
  const zeros = [row('zero-a', 0, .1), row('zero-b', 0, .2), row('zero-c', 0, .2), row('unknown', null, .9)];
  assert.equal(winner(zeros, 0, 1, () => .1), 'zero-b');
  assert.equal(winner(zeros, 0, 1, () => .9), 'zero-c');
  assert.equal(winner(zeros, .01, 1), null);
  assert.equal(winner([row('zero', 0), row('unknown', null, .9)], 0, 1), null);
});

test('work navigation displays the stable review order only for the job that ran bulk selection', () => {
  const begin = source.indexOf('  function successfulWorkItems(');
  const end = source.indexOf('  function activeWorkItem(', begin);
  const items = ['one', 'two', 'three', 'four'].map(template_key => ({template_key, plan_id:'p', candidates:[{}]}));
  const state = {jobId: 'job', jobResult:{items}, bulkReviewOrder:{jobId:'job', unselected:new Set(['four::p','two::p'])}};
  const list = Function('state', `${source.slice(begin,end)};return successfulWorkItems;`)(state);
  assert.deepEqual(list().map(item => item.template_key), ['two','four','one','three']);
  state.jobId = 'different-job';
  assert.deepEqual(list().map(item => item.template_key), ['one','two','three','four']);
});
test('sample summary keeps six metrics together and separates missing player evidence from zero',()=>{
 const begin=source.indexOf('  function playerWinRate(');
 const end=source.indexOf('  function candidateRecommendation(',begin);
 const rows=[{label:'随机策略',pass:.008},{label:'贪心策略',pass:.2},{label:'局部最优',pass:.3},{label:'解锁优先',pass:0},{label:'压力测试',pass:null}];
 const render=Function('candidateSamplePassModel','candidateDifficultyReport','difficultyPercent','escapeHtml',`${source.slice(begin,end)}return samplePassSummaryMarkup;`)(
  ()=>({rows}),value=>({raw:{online_player_simulation:value}}),value=>Number.isFinite(value)?`${value*100}%`:'—',String
 );
 const html=render({});
 assert.equal((html.match(/<dd>/g)||[]).length,6);
 assert.equal((html.match(/class="sample-pass-grid"/g)||[]).length,1);
 assert.match(html,/0.8%/);assert.match(html,/>0%<\/dd>/);assert.match(html,/>—<\/dd>/);
 assert.match(html,/>待标定<\/dd>/);
 assert.match(render({status:'validated',trial_count:1000,naked_pass_probability:0}),/玩家裸胜<\/dt><dd>0%/);
 assert.match(render({status:'pending',trial_count:1000,naked_pass_probability:.8}),/玩家裸胜<\/dt><dd>待标定/);
 assert.match(render({status:'research',trial_count:1200,scenario_count:10,naked_pass_probability:.125}),/裸胜试算<\/dt><dd>12.5%/);
 assert.match(render({status:'research',trial_count:999,naked_pass_probability:.125}),/裸胜试算<\/dt><dd>待标定/);
});

test('changing work items moves visible estimates ahead of the background queue', async () => {
  const start = source.indexOf('  function requestPlayerEstimate(');
  const end = source.indexOf('  async function loadFocusedPlayerEstimate(', start);
  const state = {
    jobId: 'job', playerEstimates: new Map(), playerEstimateRequests: new Map(),
    playerEstimateQueue: [], playerEstimateRunning: false,
  };
  let active = {candidates: [{id: 'first'}, {id: 'background'}]};
  let release;
  const order = [];
  const api = async path => {
    const id = path.split('/').at(-1);
    order.push(id);
    if (id === 'first') await new Promise(resolve => { release = resolve; });
    return {status: 'research', trial_count: 1200, naked_pass_probability: .2};
  };
  const request = Function('state','candidateId','activeWorkItem','api',
    `${source.slice(start,end)};return requestPlayerEstimate;`)(
      state, candidate => candidate?.id, () => active, api
    );
  const first = request('job', 'first');
  const background = request('job', 'background');
  const visible = request('job', 'visible');
  active = {candidates: [{id: 'visible'}]};
  assert.equal(request('job', 'visible'), visible);
  release();
  await Promise.all([first, background, visible]);
  assert.deepEqual(order, ['first', 'visible', 'background']);
});
