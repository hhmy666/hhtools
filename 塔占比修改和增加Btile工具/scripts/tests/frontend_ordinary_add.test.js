'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const VM = require('../../frontend/assets/view-model.js');
const modulePath = path.join(__dirname, '../../frontend/assets/ordinary-add-ui.js');
const UI = fs.existsSync(modulePath) ? require(modulePath) : {};
const parameters = require('../../frontend/assets/parameter-config.js');

test('ordinary additions are not classified as skipped when source has no towers', () => {
  const row = VM.normalizeTemplate({status:'verified',parameters:{mode:'ordinary_add'},
    plan_summary:{operation:'ordinary_add',original_total:60,final_total:78,
      original_counts:[60,0],final_counts:[60,18],original_tower_tiles:0,final_tower_tiles:0,
      added_layers:2},artifacts:[{id:'artifact'}]}, 'one', 0);
  assert.equal(row.isNoTower, false);
  assert.deepEqual(VM.summarizeTemplateOutcomes([row]), {passed:1,failed:0,noOp:0});
  const move = VM.tileMovementView(row);
  assert.match(move.summary, /新增 A 0 \/ B 18/);
  assert.match(move.changeSummary, /60 → 78/);
});
test('only terminal non-import rows offer single-template editing, including success', () => {
  assert.equal(typeof VM.canRerunTemplate, 'function');
  for (const status of ['verified','succeeded','exported','failed','interrupted','no_op'])
    assert.equal(VM.canRerunTemplate({status,stage:'register_output'}), true);
  for (const status of ['queued','running']) assert.equal(VM.canRerunTemplate({status}), false);
  assert.equal(VM.canRerunTemplate({status:'failed',stage:'import'}), false);
});
test('ordinary-add settings keep exact counts and distinguish automatic, fixed and inclusive range', () => {
  assert.equal(typeof UI.defaults, 'function');
  assert.equal(UI.defaults().tile_type, 'B');
  assert.equal(UI.defaults().placement_strategy, 'bottom_first');
  assert.equal(UI.validate({...UI.defaults(),added_tiles:20}).field, 'added_tiles');
  assert.equal(UI.validate({...UI.defaults(),added_tiles:0}).field, 'added_tiles');
  assert.equal(UI.validate({...UI.defaults(),layer_mode:'range',min_added_layers:4,max_added_layers:2}).field, 'min_added_layers');
  assert.equal(UI.validate({...UI.defaults(),layer_mode:'exact',min_added_layers:2,max_added_layers:3}).field, 'max_added_layers');
  assert.equal(UI.validate({...UI.defaults(),layer_mode:'exact',min_added_layers:0,max_added_layers:0}), null);
  assert.match(UI.summary({...UI.defaults(),layer_mode:'range',min_added_layers:1,max_added_layers:3}), /1–3 层/);
});
test('ordinary-add rerun reads only its current draft and preserves the current output identity', () => {
  const source = fs.readFileSync(path.join(__dirname,'../../frontend/assets/app.js'),'utf8');
  const start = source.indexOf('function rerunDefaults(');
  const end = source.indexOf('\nasync function ',start);
  const code = source.slice(start,end);
  const row={key:'a',parameters:{mode:'ordinary_add',naming_rule:'custom{原模板ID}'}};
  const nodes={'rerun-new-id':{value:'customoldv2'}};
  const context={state:{rerunKey:'a',rerunOrdinary:{...UI.defaults?.(),added_tiles:21},currentJob:{templates:[row]}},
    ORDINARY:UI,structuredClone,byId:id=>nodes[id],clearInvalid(){},setInvalid(){throw Error('unexpected invalid');},focusFirstInvalid(){},
    asText:(v,f)=>v||f,currentNamingRule:()=> 'moreB{原模板ID}'};
  vm.runInNewContext(code,context);
  const result=context.rerunDefaults({});
  assert.equal(result.mode,'ordinary_add');
  assert.equal(result.ordinary_add.added_tiles,21);
  assert.equal(result.new_id,'customoldv2');
  assert.equal(result.naming_rule,'custom{原模板ID}');
});

test('parameter setup selects the ordinary-add naming family from trusted entries and keeps explicit edits', async () => {
  const flow = parameters.createWorkflow({request:async()=>({selection_id:'sel',entries:[{status:'ready',operation:'increase',template_mode:'ordinary_add'}]})});
  flow.input({ids:'moreBsource'});
  await flow.resolve();
  assert.equal(flow.state.rule,'moreB{原关卡ID}');
  flow.delivery({rule:'custom{原关卡ID}'});
  await flow.resolve();
  assert.equal(flow.state.rule,'custom{原关卡ID}');
});

test('switching layer modes keeps the displayed range and fixed count identical to submitted values', () => {
  const nodes = new Map(['min_added_layers','max_added_layers'].map(key=>[key,{value:String(UI.defaults()[key])}]));
  const element={innerHTML:'',querySelectorAll:()=>[],querySelector:selector=>
    selector.startsWith('label') ? {} : nodes.get([...nodes.keys()].find(key=>selector.includes(key)))};
  const editor=UI.mountEditor(element,{options:UI.defaults()});
  const mode=value=>element.onchange({target:{dataset:{ordinaryField:'layer_mode'},value}});
  mode('exact');mode('range');
  assert.equal(Number(nodes.get('min_added_layers').value),editor.value().min_added_layers);
  nodes.get('max_added_layers').value='5';
  element.oninput({target:{dataset:{ordinaryField:'max_added_layers'},value:'5'}});
  mode('auto');mode('exact');
  assert.equal(Number(nodes.get('max_added_layers').value),editor.value().max_added_layers);
});

test('batch rename discards a prior single-rerun identity while preserving structural options', () => {
  assert.equal(typeof VM.namingRegenerationParameters,'function');
  const options=VM.namingRegenerationParameters({mode:'ordinary_add',ordinary_add:{added_tiles:21},new_id:'oldName',naming_rule:'old{原模板ID}'},'new{原模板ID}');
  assert.equal(options.new_id,undefined);
  assert.equal(options.naming_rule,'new{原模板ID}');
  assert.equal(options.ordinary_add.added_tiles,21);
});

test('total range and custom split validate active fields and describe the submitted goal', () => {
  const range={...UI.defaults(),target_mode:'total_range',min_total_tiles:90,max_total_tiles:99,added_tiles:NaN};
  assert.equal(UI.validate(range),null);
  assert.match(UI.summary(range),/90–99/);
  assert.equal(UI.validate({...range,max_total_tiles:89}).field,'max_total_tiles');
  const both={...UI.defaults(),placement_strategy:'both',split_mode:'custom',top_tiles:6,bottom_tiles:12};
  assert.equal(UI.validate(both),null);
  assert.match(UI.summary(both),/上 6 \/ 下 12 张/);
  assert.equal(UI.validate({...both,bottom_tiles:9}).field,'bottom_tiles');
  assert.equal(UI.validate({...both,top_tiles:5,bottom_tiles:13}),null);
  assert.equal(UI.validate({...range,placement_strategy:'both',split_mode:'custom',top_tiles:12,bottom_tiles:18}),null);
});

test('changing quantity and split modes reveals only relevant controls and preserves drafts', () => {
  const targets=['count','total_range'].map(value=>({dataset:{ordinaryTarget:value}}));
  const splits=['both','custom'].map(value=>({dataset:{ordinarySplit:value}}));
  const element={innerHTML:'',querySelector:()=>null,querySelectorAll:selector=>selector.includes('target')?targets:selector.includes('split')?splits:[]};
  const editor=UI.mountEditor(element,{options:UI.defaults()});
  const change=(key,value)=>element.onchange({target:{dataset:{ordinaryField:key},value}});
  change('target_mode','total_range');change('min_total_tiles','90');change('max_total_tiles','99');
  change('placement_strategy','both');change('split_mode','custom');change('top_tiles','12');change('bottom_tiles','18');
  assert.equal(targets[0].hidden,true);assert.equal(targets[1].hidden,false);
  assert.equal(splits[1].hidden,false);
  assert.equal(UI.validate(editor.value()),null);
  change('split_mode','auto');assert.equal(splits[1].hidden,true);
  change('split_mode','custom');assert.equal(editor.value().top_tiles,12);
});
