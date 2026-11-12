'use strict';
const test = require('node:test'), assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../../frontend/assets/app.js'), 'utf8');
function setup(request) {
  const nodes = new Map();
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, {value:'', textContent:'', className:'', setAttribute(){}});
    return nodes.get(id);
  };
  const buttons = ['up','down','left','right','reset'].map(direction => ({dataset:{translatePreview:direction},disabled:false}));
  const state = {currentJobId:'job', requestGeneration:1, previewRequest:1, previewJobId:'job', previewTemplateKey:'one',
    currentJob:{status:'succeeded',templates:[{key:'one',status:'verified',artifacts:[{id:'a'}]}]},
    templatePreview:{artifact_id:'a',source:{tiles:[{x:3,y:4,z:0}]},output:{tiles:[{x:4,y:4,z:0}]},
      translation:{dx:0,dy:0,min_dx:-2,max_dx:2,min_dy:-2,max_dy:2}}};
  let invalidations = 0, renders = 0, loads = 0;
  const context = {state,byId:node,all:()=>buttons,TERMINAL_JOB_STATES:new Set(['succeeded']),encodeURIComponent,
    apiRequest:request, invalidatePlans:()=>{invalidations++;state.exportPlan=null;state.syncPlan=null;},
    updateNamingRegenerationControl:()=>{},renderTemplatePreview:()=>{renders++;},
    loadJob:async()=>{loads++;state.currentJob.templates[0].artifacts=[{id:'b'}];}};
  for (const name of ['translationPending','previewTranslationAvailable','updatePreviewTranslationControls','translateTemplatePreview']) {
    const start = source.search(new RegExp(`(?:async )?function ${name}\\(`));
    const end = source.slice(start+1).search(/\n(?:async )?function /);
    vm.runInNewContext(source.slice(start,end<0?source.length:start+1+end),context);
  }
  return {context,state,node,buttons,counts:()=>({invalidations,renders,loads})};
}
const updated = {artifact_id:'b',source:{tiles:[{x:3,y:4,z:0}]},output:{tiles:[{x:5,y:4,z:0}]},translation:{dx:1,dy:0,min_dx:-2,max_dx:2,min_dy:-2,max_dy:2}};
test('verified comparison exposes translation without entering brush editing and explains autosave',()=>{
  const {context,state,node,buttons}=setup(async()=>updated);
  assert.equal(state.manualDraft,undefined);
  context.updatePreviewTranslationControls();
  assert.equal(node('preview-translation').hidden,false);
  assert.equal(buttons.find(button=>button.dataset.translatePreview==='right').disabled,false);
  assert.match(node('preview-translation-status').textContent,/自动保存/);
  assert.match(node('preview-translation-status').title,/自动.*验收.*保存/);
});
test('whole-template move saves absolute offset with current artifact, then publishes server board and invalidates plans', async()=>{
  let finish,body;
  const {context,state,node,counts}=setup((url,options)=>{assert.equal(url,'/api/jobs/job/translate/one');body=options.json;return new Promise(resolve=>finish=resolve);});
  node('preview-mode').value='single';node('preview-layer').value='2';
  const original = JSON.stringify(state.templatePreview.source);
  const pending=context.translateTemplatePreview('right');
  assert.equal(context.translationPending(),true);assert.equal(state.templatePreview.artifact_id,'a');
  assert.deepEqual(JSON.parse(JSON.stringify(body)),{artifact_id:'a',dx:1,dy:0});
  finish(updated);await pending;
  assert.equal(state.templatePreview.artifact_id,'b');assert.equal(JSON.stringify(state.templatePreview.source),original);
  assert.equal(node('preview-layer').value,'2');assert.equal(node('preview-mode').value,'single');
  assert.equal(context.translationPending(),false);assert.deepEqual(counts(),{invalidations:2,renders:1,loads:1});
  assert.match(node('preview-translation-status').textContent,/已验收并保存/);
});
test('pending save blocks duplicate movement and keeps old board',async()=>{
  let finish,calls=0;const {context,state,buttons}=setup(()=>{calls++;return new Promise(resolve=>finish=resolve);});
  const pending=context.translateTemplatePreview('right');await context.translateTemplatePreview('left');
  assert.equal(calls,1);assert.ok(buttons.every(button=>button.disabled));assert.equal(state.templatePreview.artifact_id,'a');
  finish(updated);await pending;
});
test('clean and dirty editing drafts translate locally and stay unsaved until one atomic save',async()=>{
  let calls=0;
  const {context,state}=setup(async()=>{calls++;return updated;});
  const engine=require('../../frontend/assets/manual-edit.js');
  state.manualDraft=engine.create({tiles:[{index:0,x:4,y:4,z:0,tile_id:-10}],towers:[]});
  state.manualDraft.validationRequested=true;state.manualError=true;
  context.window={TowerManualEdit:engine};
  await context.translateTemplatePreview('right');
  assert.equal(calls,0);assert.equal(state.manualDraft.board.tiles[0].x,5);assert.equal(state.manualDraft.history.length,1);
  assert.equal(state.manualDraft.validationRequested,false);assert.equal(state.manualError,false);
  context.updatePreviewTranslationControls();assert.equal(context.translationPending(),false);
  await context.translateTemplatePreview('left');assert.equal(calls,0);assert.equal(state.manualDraft.board.tiles[0].x,4);
  assert.equal(state.templatePreview.artifact_id,'a');
});
test('editing translation controls never disable a direction based on draft geometry',()=>{
 const {context,state,buttons}=setup(async()=>updated);
 state.manualDraft={board:{tiles:[],towers:[]},translation:{dx:100,dy:100},history:[]};
 context.window={TowerManualEdit:{translationBounds(){throw new Error('draft geometry must not gate controls');}}};
 context.updatePreviewTranslationControls();assert.ok(buttons.every(button=>button.disabled===false));
});
test('source-only, no artifact, in-flight row/job and changed artifact cannot mutate',async()=>{
  for (const alter of [s=>s.previewSourceOnly=true,s=>s.previewJobId='',s=>s.templatePreview.artifact_id='',
    s=>s.currentJob.templates[0].status='running',s=>s.currentJob.status='running',s=>s.currentJob.templates[0].artifacts=[{id:'other'}]]) {
    let calls=0;const {context,state,node}=setup(async()=>{calls++;});alter(state);
    context.updatePreviewTranslationControls();await context.translateTemplatePreview('right');
    assert.equal(node('preview-translation').hidden,true);assert.equal(calls,0);
  }
});
test('boundary disables its direction; reset sends zero rather than reversing one step',async()=>{
  let sent;const {context,state,buttons}=setup(async(_url,options)=>{sent=options.json;return {...updated,translation:{...updated.translation,dx:0}};});
  state.templatePreview.translation.dx=2;
  context.updatePreviewTranslationControls();assert.equal(buttons.find(b=>b.dataset.translatePreview==='right').disabled,true);
  await context.translateTemplatePreview('right');assert.equal(sent,undefined);
  await context.translateTemplatePreview('reset');assert.equal(sent.dx,0);assert.equal(sent.dy,0);
});
test('boundary failure keeps persisted board and exposes retry feedback',async()=>{
  const {context,state,node}=setup(async()=>{const error=new Error('达到安全边界');error.code='translation_out_of_bounds';throw error;});
  await context.translateTemplatePreview('right');assert.equal(state.templatePreview.artifact_id,'a');
  assert.match(node('preview-translation-status').textContent,/移动未保存.*安全边界/);assert.equal(context.translationPending(),false);
});
test('late save cannot overwrite a different job or a reopened comparison',async()=>{
  for (const alter of [s=>s.currentJobId='other',s=>s.previewRequest++]) {
    let finish;const {context,state,counts}=setup(()=>new Promise(resolve=>finish=resolve));
    const pending=context.translateTemplatePreview('right');alter(state);finish(updated);await pending;
    assert.equal(state.templatePreview.artifact_id,'a');assert.equal(counts().renders,0);
    assert.equal(state.translationBusy,null);
  }
});
test('stale artifact error disables translation and explains reopening',async()=>{
  const {context,state,node}=setup(async()=>{const error=new Error('产物已变化');error.code='stale_artifact';throw error;});
  await context.translateTemplatePreview('right');assert.equal(state.templatePreview.artifact_id,'');
  assert.match(node('preview-feedback').textContent,/重新打开/);assert.equal(node('preview-translation').hidden,true);
});
