"use strict";
const test = require('node:test');
const assert = require('node:assert/strict');
const increase = require('../../frontend/assets/increase-ui.js');
const fs=require('node:fs'), path=require('node:path'), sandbox=require('node:vm');
const VM=require('../../frontend/assets/view-model.js');
const parameters=require('../../frontend/assets/parameter-config.js');
const preview=require('../../frontend/assets/template-preview.js');
const app=fs.readFileSync(path.join(__dirname,'../../frontend/assets/app.js'),'utf8');
function fn(name){const start=app.indexOf(`function ${name}(`),next=app.indexOf('\nfunction ',start+1);assert.ok(start>=0);return app.slice(start,next<0?undefined:next);}
function appContext(names) {
  const nodes=new Map(),buttons=['reduce','increase'].map(mode=>({dataset:{workspaceMode:mode},classList:{toggle(){}},setAttribute(){}}));
  const node=id=>{if(!nodes.has(id))nodes.set(id,{value:'',dataset:{},hidden:false,textContent:'',focus(){},querySelector(){return node('first-field');}});return nodes.get(id);};
  const state={mode:'reduce',increaseDefaults:increase.defaults(),increaseOverrides:{},overrides:{old:{ratio:.1}},settings:{namingRule:'lessT{原模板ID}',increaseNamingRule:'moreT{原模板ID}'},importedFiles:[]};
  const ctx={state,INCREASE:increase,VM,structuredClone,Number,Object,Array,clearTimeout(){},window:{towerIncreasePreview:{cancel(){}}},byId:node,all:()=>buttons,showBatchEditor(){},renderIncreaseEditor(){},updateImportControls(){},updateParameterSummaries(){},invalidatePlans(){},clearInvalid(){},setInvalid(){},focusFirstInvalid(){},showToast(){},asText:(v,f='')=>typeof v==='string'&&v?v:f,asArray:v=>Array.isArray(v)?v:[],asObject:v=>v&&typeof v==='object'?v:{}};
  sandbox.createContext(ctx);sandbox.runInContext(names.map(fn).join('\n'),ctx);return {ctx,node,state};
}
test('increase defaults preserve four mutually exclusive quantity targets and complete shared options', () => {
  assert.equal(increase.defaults().direction, 0);
  assert.deepEqual(increase.defaults().regions, ['auto']);
  for (const target_mode of ['ratio','count','multiplier','manual']) {
    const options = {...increase.defaults(), target_mode, tower_counts:[12,12]};
    assert.equal(increase.validate(options), '');
  }
  assert.match(increase.validate({...increase.defaults(),target_mode:'count',added_tiles:NaN}), /新增/);
});
test('single tower edits preserve input and bind mirrored positions without editing other parameters', () => {
  const options = increase.defaults();
  const towers = [{index:0,x:2,y:8,count:12,direction:0},{index:1,x:12,y:8,count:12,direction:0}];
  const edited = increase.editTower(options,towers,0,{x:3,y:7,count:14,direction:1},true);
  assert.deepEqual(options.tower_positions, []);
  assert.deepEqual(edited.tower_positions,[{index:0,x:3,y:7},{index:1,x:11,y:7}]);
  assert.deepEqual(edited.tower_counts,[14,12]);
  assert.deepEqual(edited.tower_directions,[{index:0,direction:1}]);
});
test('preview isolates late responses between templates and keeps inputs on backend failure', async () => {
  let pending=[];
  const flow=increase.createPreviewFlow({request:(_path,init)=>new Promise(resolve=>pending.push({resolve,init}))});
  flow.configure({import_id:'batch',file_id:'one',increase:increase.defaults()});
  const first=flow.preview();
  flow.configure({import_id:'batch',file_id:'two',increase:{...increase.defaults(),added_tiles:20}});
  const second=flow.preview();
  pending[1].resolve({schemes:[{scheme_id:'two',options:increase.defaults(),preview:{source:{},output:{}}}]});
  await second;
  pending[0].resolve({schemes:[{scheme_id:'one'}]}); await first;
  assert.equal(flow.state.schemes[0].scheme_id,'two');
  assert.equal(flow.state.input.increase.added_tiles,20);
  const third=flow.preview(); pending[2].resolve({error:{message:'没有空间'},schemes:[]}); await third;
  assert.equal(flow.state.error,'没有空间');
  assert.equal(flow.state.input.increase.added_tiles,20);
});
test('adopting recommendation is explicit, undo and reset restore complete options', async () => {
  const flow=increase.createPreviewFlow({request:async()=>({schemes:[{scheme_id:'s',options:{...increase.defaults(),added_tiles:21}}]})});
  flow.configure({import_id:'b',file_id:'a',increase:{...increase.defaults(),added_tiles:20}});
  await flow.preview(); assert.equal(flow.state.input.increase.added_tiles,20);
  flow.adopt(0); assert.equal(flow.state.input.increase.added_tiles,21);
  flow.undo(); assert.equal(flow.state.input.increase.added_tiles,20);
  flow.change({...increase.defaults(),direction:3}); flow.reset(); assert.equal(flow.state.input.increase.direction,0);
});
test('mode switching preserves both modes drafts, overrides and explicitly edited export rule',()=>{
  const {ctx,state,node}=appContext(['currentNamingRule','setWorkspaceMode']);
  state.increaseDefaults.added_tiles=48;state.increaseOverrides.a={...increase.defaults(),direction:1};
  ctx.setWorkspaceMode('increase');assert.equal(node('export-naming-rule').value,'moreT{原模板ID}');
  node('export-naming-rule').dataset.edited='true';node('export-naming-rule').value='custom{原模板ID}';
  ctx.setWorkspaceMode('reduce');assert.equal(node('export-naming-rule').value,'custom{原模板ID}');
  assert.equal(state.increaseDefaults.added_tiles,48);assert.equal(state.increaseOverrides.a.direction,1);assert.equal(state.overrides.old.ratio,.1);
});
test('single-template recommendation and restoring batch defaults do not change sibling templates',()=>{
  const {ctx,state}=appContext(['setOverrideMode']);state.mode='increase';state.increaseOverrides.b={...increase.defaults(),direction:3};
  ctx.setOverrideMode('a','custom');state.increaseOverrides.a.added_tiles=21;
  assert.equal(state.increaseDefaults.added_tiles,30);assert.equal(state.increaseOverrides.b.direction,3);
  ctx.setOverrideMode('a','reset');assert.equal(state.increaseOverrides.a,undefined);assert.equal(state.increaseOverrides.b.direction,3);
});
test('four target controls reveal exactly the selected target and retain inactive input',()=>{
  const targets=['count','multiplier','ratio','ratio','manual'].map(mode=>({dataset:{increaseTarget:mode},hidden:false}));
  const element={innerHTML:'',querySelectorAll:s=>s==='[data-increase-target]'?targets:[]};
  let received;
  const editor=increase.mountEditor(element,{options:increase.defaults(),onChange:value=>received=value});
  for(const target_mode of ['count','multiplier','manual','ratio']){
    element.onchange({target:{dataset:{increaseField:'target_mode'},value:target_mode}});
    assert.ok(targets.every(t=>t.hidden===(t.dataset.increaseTarget!==target_mode)));
    assert.equal(received.target_mode,target_mode);
  }
  assert.equal(editor.value().added_tiles,30);
});
test('zero and legacy null stack counts display and submit as automatic without swallowing invalid numbers',async()=>{
  for(const stack_count of [null,0]){
    const options={...increase.defaults(),stack_count};
    assert.equal(increase.validate(options),'');assert.match(increase.summary(options),/自动堆数/);
    let sent;const flow=increase.createPreviewFlow({request:async(_url,init)=>{sent=init.json;return {schemes:[]};}});
    flow.configure({import_id:'b',file_id:'a',increase:options});await flow.preview();assert.equal(sent.increase.stack_count,null);
  }
  let received;const node={innerHTML:'',querySelectorAll:()=>[]};
  const editor=increase.mountEditor(node,{options:increase.defaults(),onChange:value=>received=value});
  for(const [value,want] of [['',null],['0',null],['-1',-1],['1.5',1.5]]){
    node.oninput({target:{dataset:{increaseField:'stack_count'},value}});
    assert.equal(received.stack_count,want);
    assert.equal(Boolean(increase.validate(editor.value())),want!==null);
  }
  const invalid={...increase.defaults(),stack_count:NaN};
  assert.match(increase.validate(invalid),/整数/);
  node.oninput({target:{dataset:{increaseField:'stack_count'},value:'',validity:{badInput:true}}});
  assert.equal(Number.isNaN(editor.value().stack_count),true);assert.match(increase.validate(editor.value()),/整数/);
});
test('shared stack editor presents automatic zero and human direction labels with stable numeric values',()=>{
  for(const stack_count of [null,0]){
    const markup=increase.fieldsMarkup('shared',{...increase.defaults(),stack_count});
    assert.match(markup,/新增堆数（填0自动）/);assert.match(markup,/data-increase-field="stack_count" value="0" min="0"/);
    for(const [value,label] of [['0','朝上'],['1','朝左'],['2','朝下'],['3','朝右']])assert.match(markup,new RegExp(`<option value="${value}" [^>]*>${label}</option>`));
  }
});
test('generation normalizes zero stack defaults, individual overrides and rerun to legacy automatic null',()=>{
  const {ctx,state,node}=appContext(['currentNamingRule','collectDefaults','collectOverrides','rerunDefaults']);state.mode='increase';
  state.increaseDefaults.stack_count=0;state.increaseOverrides.a={...increase.defaults(),stack_count:0};
  assert.equal(ctx.collectDefaults().increase.stack_count,null);assert.equal(ctx.collectOverrides().a.increase.stack_count,null);
  state.rerunKey='a';state.currentJob={templates:[{key:'a',parameters:{mode:'increase'}}]};state.rerunIncrease={...increase.defaults(),stack_count:0};node('rerun-new-id').value='';
  assert.equal(ctx.rerunDefaults({}).increase.stack_count,null);
});
test('closing preview aborts its request and a late completion cannot replace state',async()=>{
  let resolve,signal;const flow=increase.createPreviewFlow({request:(_p,o)=>{signal=o.signal;return new Promise(r=>resolve=r);}});
  flow.configure({import_id:'b',file_id:'a',increase:increase.defaults()});const run=flow.preview();flow.cancel();
  assert.equal(signal.aborted,true);resolve({schemes:[{scheme_id:'late'}]});await run;
  assert.deepEqual(flow.state.schemes,[]);assert.equal(flow.state.pending,false);
});
test('generation and rerun carry complete increase options, independent overrides and correct naming',()=>{
  const {ctx,state,node}=appContext(['currentNamingRule','collectDefaults','collectOverrides','rerunDefaults']);state.mode='increase';
  state.increaseOverrides.a=increase.editTower(increase.defaults(),[{index:0,x:2,y:8,count:12,direction:0},{index:1,x:12,y:8,count:12,direction:0}],0,{count:14,direction:2,x:3},true);
  const defaults=ctx.collectDefaults(),overrides=ctx.collectOverrides();
  assert.equal(defaults.mode,'increase');assert.equal(defaults.naming_rule,'moreT{原模板ID}');
  assert.equal(overrides.a.increase.height_mode,'manual');assert.equal(overrides.a.increase.tower_counts[0],14);
  assert.equal(overrides.a.increase.tower_directions[0].direction,2);assert.equal(overrides.a.increase.tower_positions[1].x,11);
  state.rerunKey='a';state.currentJob={templates:[{key:'a',parameters:{mode:'increase',naming_rule:'custom{原模板ID}'}}]};state.rerunIncrease=overrides.a.increase;
  node('rerun-new-id').value='custom001';const rerun=ctx.rerunDefaults({});
  assert.equal(rerun.mode,'increase');assert.equal(rerun.naming_rule,'custom{原模板ID}');assert.equal(rerun.new_id,'custom001');
  assert.deepEqual(rerun.increase.tower_counts,[14,12]);
});
test('settings normalize separate naming rules without changing legacy reduce default',()=>{
  const {ctx}=appContext(['normalizeSettings']);
  const s=ctx.normalizeSettings({settings:{naming_rule:'r{原模板ID}',increase_naming_rule:'i{原模板ID}'}});
  assert.equal(s.namingRule,'r{原模板ID}');assert.equal(s.increaseNamingRule,'i{原模板ID}');
  assert.equal(ctx.normalizeSettings({}).increaseNamingRule,'moreT{原模板ID}');
});
test('increase results show added A/B and count an originally towerless verified output as passed',()=>{
  const row=VM.normalizeTemplate({status:'verified',plan_summary:{operation:'increase',original_total:60,final_total:81,added_tiles:21,original_counts:[60,0],final_counts:[81,0],original_tower_tiles:0,final_tower_tiles:21,final_ratio:21/81,move_count:0}},'a',0);
  assert.equal(row.isNoTower,false);assert.match(VM.tileMovementView(row).summary,/A 21.*B 0/);
  assert.deepEqual(VM.summarizeTemplateOutcomes([row]),{passed:1,failed:0,noOp:0});
});
test('appended indices render as added with unchanged source index identity and interactive tower buttons',()=>{
  const board={template_id:'new',towers:[{index:0,direction:0}],tiles:[{index:0,x:3,y:3,z:0,tile_id:-10,tower_index:null},{index:1,x:4,y:4,z:1,tile_id:-10,tower_index:0,added:true,changed:true}]};
  const markup=preview.boardMarkup(board,{interactive:true});
  assert.match(markup,/data-tile-index="0"/);assert.match(markup,/data-tile-index="1"/);assert.match(markup,/is-added/);assert.match(markup,/<button type="button" data-tower-index="0"/);
});
test('parameter naming follows trusted increase selection but preserves an explicit rule',async()=>{
  const workflow=parameters.createWorkflow({request:async()=>({selection_id:'s',entries:[{operation:'increase',status:'ready'}]})});
  workflow.state.ids='moreT001';await workflow.resolve();assert.equal(workflow.state.rule,'moreT{原关卡ID}');
  workflow.delivery({rule:'my{原关卡ID}'});await workflow.resolve();assert.equal(workflow.state.rule,'my{原关卡ID}');
});
test('result comparison accepts appended tile indices without inventing original coordinates',()=>{
  const {ctx,node,state}=appContext(['renderTemplatePreview']);
  state.templatePreview={source:{template_id:'s',tiles:[{index:0,x:3,y:3,z:0,tile_id:-10}],towers:[]},output:{template_id:'o',tiles:[{index:0,x:3,y:3,z:0,tile_id:-10},{index:1,x:5,y:5,z:1,tile_id:-10,changed:true,added:true}],towers:[{index:0}]},changed_count:1};
  ctx.window.TowerTemplatePreview=preview;ctx.updatePreviewLayerControls=()=>{};ctx.updatePreviewTranslationControls=()=>{};ctx.renderManualControls=()=>{};ctx.escapeHTML=String;
  node('preview-mode').value='all';node('preview-layer').value='2';node('preview-mark-changes').checked=true;
  ctx.renderTemplatePreview();assert.match(node('preview-moves-body').innerHTML,/新增/);assert.match(node('preview-moves-body').innerHTML,/—/);
});
test('increase target and fill controls occupy full rows while ratio limits remain paired',()=>{
  const html=increase.fieldsMarkup('qa',increase.defaults());
  assert.match(html,/<div class="field span-two"><label for="qa-target_mode"/);
  assert.match(html,/<div class="field span-two"><label for="qa-fill_type"/);
  assert.match(html,/<div class="span-two" data-increase-target="count">/);
  assert.match(html,/<div class="span-two" data-increase-target="multiplier">/);
  assert.equal((html.match(/<div data-increase-target="ratio">/g)||[]).length,2);
  assert.match(html,/<div class="field"><label for="qa-operation"/);
  assert.match(html,/<div class="field"><label for="qa-stack_count"/);
});
test('empty preview distinguishes pending and failed boards and retains previous boards while pending',()=>{
  const renderer={boardMarkup:board=>`real-${board.template_id}`};
  assert.match(increase.previewBoardMarkup(null,'source',{pending:true},renderer),/正在读取原模板/);
  assert.match(increase.previewBoardMarkup(null,'output',{pending:true},renderer),/正在试算新模板/);
  assert.match(increase.previewBoardMarkup(null,'source',{error:'网络不可用'},renderer),/未能读取原模板/);
  assert.match(increase.previewBoardMarkup(null,'output',{error:'网络不可用'},renderer),/未能生成新模板预览/);
  assert.equal(increase.previewBoardMarkup({template_id:'old'},'output',{pending:true,stale:true},renderer),'real-old');
});
