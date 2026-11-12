import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
const source=fs.readFileSync(new URL('../assets/app.js',import.meta.url),'utf8');
test('history adjustment inherits automatic group difference without mutating locked request',()=>{
  const start=source.indexOf('  function rawPlanContextForItem('),end=source.indexOf('  function effectivePlanSpecsForTemplate(',start);
  const resolve=Function(`${source.slice(start,end)};return rawPlanContextForItem;`)();
  for (const value of [2,10]) {
    const request={generation_defaults:{automatic_group_max_difference:value},batch_plans:[{plan_id:'p'}]};
    const before=JSON.stringify(request);
    assert.equal(resolve({plan_id:'p'},request).spec.generation.automatic_group_max_difference,value);
    assert.equal(JSON.stringify(request),before);
    request.batch_plans[0].generation={automatic_group_max_difference:0};
    assert.equal(resolve({plan_id:'p'},request).spec.generation.automatic_group_max_difference,0);
  }
});
test('search logs report thousand-attempt milestones and new candidates only',()=>{
  const start=source.indexOf('  function visibleSearchEvents('),end=source.indexOf('  function renderActivityTimeline(',start);
  assert.ok(start>=0);
  const filter=Function(`${source.slice(start,end)};return visibleSearchEvents;`)();
  const events=[0,10,999,1000,1010,1500,2000].map((n,i)=>({sequence:i,type:'candidate_search_progress',details:{template_key:'a',plan_id:'s5',candidate_count:n>=1500?1:0,search_progress:{attempts_searched:n}}}));
  assert.deepEqual(filter(events).map(e=>e.details.search_progress.attempts_searched),[1000,1500,2000]);
});
test('single-layer source preview excludes lower layers without changing source data',()=>{
  const start=source.indexOf('  function renderBoard('), end=source.indexOf('  function setActiveWork(',start);
  const render=Function('clamp','escapeHtml','TYPE_COLORS',`${source.slice(start,end)};return renderBoard;`)((x,a,b)=>Math.max(a,Math.min(b,x)),String,{A:'#fff'});
  const board={tiles:[1,2,3].map(layer=>({layer,z:layer-1,x:0,y:0,type:'A',tile_index:layer})),bounds:{layer:{max:3}}};
  const before=JSON.stringify(board);const container={innerHTML:''};
  render(container,board,{source:true,minLayer:2,maxLayer:2});
  assert.deepEqual([...container.innerHTML.matchAll(/data-layer="(\d+)"/g)].map(m=>m[1]),['2']);
  assert.match(container.innerHTML,/显示第 2 至 2 层/);
  assert.equal(JSON.stringify(board),before);
  const viewBox=container.innerHTML.match(/viewBox="([^"]+)"/)[1];
  render(container,board,{source:true,maxLayer:2});
  assert.deepEqual([...container.innerHTML.matchAll(/data-layer="(\d+)"/g)].map(m=>m[1]),['1','2']);
  assert.equal(container.innerHTML.match(/viewBox="([^"]+)"/)[1],viewBox);
});

test('history navigation refreshes shared progress before choosing terminal review layout',()=>{
  const load=source.slice(source.indexOf('  async function loadJob('),source.indexOf('  async function loadExportRecord('));
  const refresh=load.indexOf('renderGenerationProgress(job);');
  const terminalBranch=load.indexOf('if (state.jobResult && successfulWorkItems().length');
  assert.ok(refresh>0 && refresh<terminalBranch);
});
