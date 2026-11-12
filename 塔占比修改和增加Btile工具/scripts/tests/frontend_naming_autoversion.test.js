'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const model = require('../../frontend/assets/view-model.js');
const source = fs.readFileSync(require('node:path').join(__dirname, '../../frontend/assets/app.js'), 'utf8');

test('naming rules allocate unique versions while reserving explicit version names', () => {
  assert.deepEqual(model.validateNamingRule('copy{原模板ID}', ['a','A','av2','a']).outputIds,
    ['copya','copyAv3','copyav2','copyav4']);
  assert.equal(model.validateNamingRule('copy{原模板ID}', ['a','a']).ok, true);
  assert.deepEqual(model.validateNamingRule('fixed', ['a','b']).outputIds, ['fixed','fixedv2']);
});

test('untouched export naming never blocks valid current artifact names', () => {
  const controls = {'export-naming-rule':{value:'bad rule',dataset:{}}, 'export-directory':{value:'/tmp/out'}};
  let invalid = false, checked = 0;
  const context = {byId:id=>controls[id],clearInvalid(){invalid=false;},
    validateNamingControl(){checked++;invalid=true;return {ok:false,message:'bad'};},
    setInvalid(){invalid=true;},focusFirstInvalid:()=>invalid,showFeedback(){}};
  const start = source.indexOf('function validateExport(');
  const end = source.indexOf('\nasync function ', start);
  vm.runInNewContext(source.slice(start,end),context);
  assert.equal(context.validateExport({},true),true);
  assert.equal(checked,0);
  controls['export-naming-rule'].dataset.edited='true';
  assert.equal(context.validateExport({},true),false);
  assert.equal(checked,1);
});

test('batch version explanations do not claim that an external file exists', () => {
  const controls = new Map();
  const node = id => controls.get(id) || (controls.set(id, {}), controls.get(id));
  const context = {state:{currentJob:{templates:[]}}, byId:node, asArray:value=>value || [],
    asObject:value=>value || {},asText:(value,fallback='')=>typeof value==='string'?value:fallback,
    escapeHTML:String,updateExportCommitAvailability(){}};
  const start = source.indexOf('function renderExportPlan(');
  const end = source.indexOf('\nfunction ',start+1);
  vm.runInNewContext(source.slice(start,end),context);
  context.renderExportPlan({items:[{template_key:'second',classification:'create',filename:'sharedv2_m.bytes',
    original_filename:'shared_m.bytes',renamed:true}]});
  assert.match(node('export-plan-list').innerHTML,/名称.*已被占用.*自动顺延/);
  assert.doesNotMatch(node('export-plan-list').innerHTML,/已有同名文件/);
  assert.match(node('export-plan-list').innerHTML,/内部模板 ID 同步更新/);
});
