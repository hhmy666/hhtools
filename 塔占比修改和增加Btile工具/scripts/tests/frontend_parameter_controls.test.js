'use strict';
const test = require('node:test'), assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path');
const P = require('../../frontend/assets/parameter-config.js');
const root = path.join(__dirname, '../..');
function mounted(request = async () => ({})) {
  const elements = new Map(), invalid = [];
  const document = {querySelectorAll: () => [], getElementById(id) {
    if (!elements.has(id)) {
      const listeners = {}, attrs = {};
      elements.set(id, {id, value:'', checked:false, hidden:false, disabled:false, innerHTML:'', dataset:{},
        classList:{toggle(){}}, setAttribute(k,v){attrs[k]=v;}, getAttribute(k){return attrs[k];}, removeAttribute(k){delete attrs[k];},
        addEventListener(type, fn){listeners[type]=fn;}, querySelector(){return {textContent:''};},
        emit(type){return listeners[type]?.({preventDefault(){},currentTarget:this,target:this});},
        click(){if (!this.disabled) return this.emit('click');}});
    }
    return elements.get(id);
  }};
  const ui = P.mount({document, request, clearInvalid(){invalid.length=0;}, setInvalid(el,message){invalid.push({id:el.id,message});}, focusFirstInvalid(){return Boolean(invalid.length);}});
  ui.workflow.state.context = {config_ready:true};
  return {ui, elements, invalid};
}
const ticks = async () => {for(let i=0;i<12;i++) await Promise.resolve();};
test('matching options follow associations and precede the bottom calculate control', () => {
  const html = fs.readFileSync(path.join(root,'frontend/index.html'),'utf8');
  assert.ok(html.indexOf('id="parameter-file-associations-details"') < html.indexOf('id="parameter-matching-options"'));
  assert.ok(html.indexOf('id="parameter-matching-options"') < html.indexOf('id="parameter-calculate-button"'));
  assert.match(html,/id="parameter-calculate-side-button"/);
});
test('both calculate entries share an immediate pending lock and can each start work', async () => {
  const calls=[]; let finish;
  const {ui,elements}=mounted((url,options)=>{calls.push(options.json);return new Promise(resolve=>finish=resolve);});
  ui.workflow.input({ids:'noTsample'});
  const left=elements.get('parameter-calculate-button'), right=elements.get('parameter-calculate-side-button');
  assert.equal(right.disabled,false); right.click(); await ticks();
  assert.equal(left.disabled,true); assert.equal(right.disabled,true); assert.equal(right.getAttribute('aria-busy'),'true');
  left.click(); assert.equal(calls.length,1);
  finish({task_id:'task-one',status:'succeeded',selection:{selection_id:'s',entries:[]},result:{rows:[]}}); await ticks();
  assert.equal(left.disabled,false); assert.equal(right.disabled,false);
  left.click(); await ticks(); assert.equal(calls.length,2);
  finish({task_id:'task-two',status:'succeeded',selection:{selection_id:'s',entries:[]},result:{rows:[]}}); await ticks();
});
test('both buttons and Enter run the same field validation without starting a request', () => {
  let calls=0; const {ui,elements,invalid}=mounted(async()=>{calls++;});
  for(const trigger of ['parameter-calculate-button','parameter-calculate-side-button','parameter-source-form']) {
    ui.workflow.input({ids:'bad/name'});
    elements.get(trigger).emit(trigger.endsWith('form')?'submit':'click');
    assert.equal(calls,0); assert.equal(invalid[0]?.id,'parameter-template-ids');
  }
});
test('upload mode hides the whole source status and progress is not duplicated in feedback', () => {
  const {ui,elements}=mounted(); ui.workflow.input({mode:'files'});
  assert.equal(elements.get('parameter-source-status').hidden,true);
  ui.workflow.state.pending='calculate'; ui.workflow.state.progress='读取生成记录 · 51 / 635';
  ui.workflow.search(''); assert.equal(elements.get('parameter-feedback').hidden,true);
  ui.workflow.input({mode:'ids'}); assert.equal(elements.get('parameter-source-status').hidden,false);
});
test('phase completion cannot fill the overall progress bar while the batch is running', () => {
  const {ui,elements}=mounted();
  ui.workflow.state.pending='calculate'; ui.workflow.state.task={status:'running',phase:'indexing',completed:635,total:635,overall_progress:{completed:2500,total:10000,stage_count:4}};
  ui.workflow.search(''); const meter=elements.get('parameter-task-meter');
  assert.ok(meter.value < meter.max); assert.equal(meter.value/meter.max,.25);
});
test('LevelId dropdown composes a custom prefix or the new template ID and retains the prefix', () => {
  const {ui,elements}=mounted();
  ui.workflow.delivery({rule:'noT{原关卡ID}'});
  const mode=elements.get('parameter-level-id-rule'), prefix=elements.get('parameter-level-id-prefix');
  assert.equal(mode.value,'prefix'); assert.equal(prefix.value,'noT');
  prefix.value='custom_'; prefix.emit('input');
  assert.equal(ui.workflow.state.rule,'custom_{原关卡ID}');
  mode.value='template'; mode.emit('change');
  assert.equal(elements.get('parameter-level-id-prefix-field').hidden,false);
  assert.equal(ui.workflow.state.rule,'custom_{新模板ID}');
  prefix.value=''; prefix.emit('input');
  assert.equal(ui.workflow.state.rule,'{新模板ID}');
  prefix.value='custom_'; prefix.emit('input');
  assert.equal(ui.workflow.state.rule,'custom_{新模板ID}');
  mode.value='prefix'; mode.emit('change');
  assert.equal(ui.workflow.state.rule,'custom_{原关卡ID}');
  assert.equal(elements.get('parameter-level-id-prefix-field').hidden,false);
  ui.workflow.delivery({preserveLevelPrefix:true});
  assert.equal(mode.disabled,true); assert.equal(prefix.disabled,true);
});
