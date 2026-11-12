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
test('default selection uses sample winner without erasing manual selections or zero opt-outs',()=>{
 const h=helpers();const begin=source.indexOf('  function initializeDefaultSelections('),end=source.indexOf('  function ensureReviewButton(',begin);
 const selected=new Map([['manual',['b']],['cleared',[]]]);
 const candidates=[candidate('a',[.1,0,0,0,0]),candidate('b',[.2,0,0,0,0],2)];
 const items=[{key:'new',candidates},{key:'manual',candidates},{key:'cleared',candidates},{key:'zero',candidates:[candidate('z',[0,0,0,0,0])]}];
 const state={jobId:'job',jobSnapshot:{status:'completed'}};
 const initialize=Function('state','selectionsForJob','successfulWorkItems','workKey','candidateExportEligibility','candidateId','persistSelections','candidateRecommendation','qualityScore',`${source.slice(begin,end)}return initializeDefaultSelections;`)(state,()=>selected,()=>items,value=>value.key,()=>({eligible:true,autoEligible:false}),value=>value.id,()=>{},h.candidateRecommendation,()=>0);
 initialize();
 assert.deepEqual(selected.get('new'),['b']);assert.deepEqual(selected.get('manual'),['b']);assert.deepEqual(selected.get('cleared'),[]);assert.equal(selected.has('zero'),false);
});
test('sample summary groups five compact metrics without a visible heading and retains zeros and missing values',()=>{
 const begin=source.indexOf('  function samplePassSummaryMarkup(');
 assert.notEqual(begin,-1);
 const end=source.indexOf('  function candidateRecommendation(',begin);
 const render=Function('candidateSamplePassModel','difficultyPercent','escapeHtml',`${source.slice(begin,end)}return samplePassSummaryMarkup;`)(()=>({rows:[{label:'随机策略',pass:.008},{label:'贪心策略',pass:.2},{label:'局部最优',pass:.3},{label:'解锁优先',pass:0},{label:'压力测试',pass:null}]}),value=>Number.isFinite(value)?`${value*100}%`:'—',String);
 const html=render({});
 assert.equal((html.match(/<dt>/g)||[]).length,5);assert.equal((html.match(/<dd>/g)||[]).length,5);
 assert.equal((html.match(/class="sample-pass-metric"/g)||[]).length,5);
 assert.doesNotMatch(html,/<h[1-6]/);
 assert.match(html,/aria-label="策略样本通过率"/);
 assert.match(html,/0.8%/);assert.match(html,/>0%<\/dd>/);assert.match(html,/>—<\/dd>/);
});
