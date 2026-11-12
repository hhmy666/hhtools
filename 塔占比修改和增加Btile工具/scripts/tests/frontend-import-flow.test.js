"use strict";
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.resolve(__dirname, '../../frontend/assets/app.js'), 'utf8');
const Bulk = require('../../frontend/assets/batch-import.js');
function code(name) {
  let start = source.indexOf(`function ${name}(`);
  if (source.slice(start-6,start) === 'async ') start -= 6;
  const next = source.slice(start + 1).search(/\n(?:async )?function /);
  return source.slice(start, next < 0 ? undefined : start + 1 + next);
}
function fixture() {
  const nodes = new Map();
  const byId = (id) => {
    if (!nodes.has(id)) nodes.set(id, {value: '', hidden: true, disabled: false,
      setAttribute() {}, toggleAttribute() {}, removeAttribute() {},classList:{toggle(){}}});
    return nodes.get(id);
  };
  byId('source-repository').value = 'tile';
  const buttons=['increase','reduce'].map(mode=>Object.assign(byId(mode==='reduce'?'inspect-button':'inspect-increase-button'),{dataset:{importWorkspaceMode:mode}}));
  const state = {mode:'reduce',importBusy:false, importMode:'ids', importEpoch:0, importSession:null,
    importId:'', importedFiles:[], settings:{tileRoot:'original-root',tileConfirmed:true}, bootstrap:{limits:{file_count:20}},
    selectedFiles:[], overrides:{}};
  const context = {state, byId, all:selector=>selector==='[data-import-workspace-mode]'?buttons:[], window:{TowerReducerBatchImport:Bulk}, AbortController,
    templateIds:()=>['one'], clearFeedback(){}, clearInvalid(){}, icon:()=>'',
    setWorkspaceMode(mode){state.mode=mode;},
    setInvalid(){}, focusFirstInvalid(){}, renderImportResponse(){}, renderConfigurationList(){},
    showStep:()=>{ context.stepChanges++; }, stepChanges:0,
    normalizeImported:(p)=>({importId:p.import_id,files:p.files}),
    showFeedback:(_id,msg)=>{context.feedback=msg;},
  };
  vm.createContext(context);
  vm.runInContext(['resetImportSession','setImportBusy','renderImportProgress','updateImportControls','importTemplates'].map(code).join('\n'),context);
  return {context,state,nodes,buttons,submit:(mode='reduce')=>context.importTemplates({preventDefault(){},currentTarget:byId('import-form'),submitter:buttons.find(button=>button.dataset.importWorkspaceMode===mode)})};
}
test('last request cannot publish a batch after the selected library path changes', async()=>{
  const f=fixture();
  f.context.apiRequest=async()=>{
    f.state.settings.tileRoot='new-root';
    return {import_id:'import-'+'1'.repeat(32), files:[{status:'ready'}]};
  };
  await f.submit();
  assert.equal(f.state.importId,''); assert.equal(f.context.stepChanges,0);
  assert.match(f.context.feedback,/路径已修改/); assert.equal(f.state.importSession,null);
  assert.equal(f.state.importBusy,false);
});
test('old import completion and finally cannot overwrite a newer workspace after restart',async()=>{
  const f=fixture(); let resolve;
  f.context.apiRequest=()=>new Promise((done)=>{resolve=done;});
  const pending=f.submit();
  const oldController=f.state.importController;
  // The restart invalidation sequence is the same shipped production code.
  const restart=code('restartWorkspace');
  const prefix=restart.slice(restart.indexOf('{')+1,restart.indexOf('  cancelBatchRegeneration();'));
  vm.runInContext('(function(){'+prefix+'})()',f.context);
  assert.equal(oldController.signal.aborted,true);
  f.state.importId='new-import'; f.state.importBusy=true;
  resolve({import_id:'import-'+'1'.repeat(32),files:[{status:'ready'}]});
  await pending;
  assert.equal(f.state.importId,'new-import'); assert.equal(f.state.importBusy,true);
  assert.equal(f.context.stepChanges,0);
});
test('increase entry locks the mode and disables both actions while sharing one parsing request',async()=>{
  const f=fixture();let resolve,calls=0;
  f.context.apiRequest=()=>{calls++;return new Promise(done=>resolve=done);};
  const first=f.submit('increase');
  assert.equal(f.state.mode,'increase');assert.ok(f.buttons.every(button=>button.disabled));
  assert.match(f.buttons[0].innerHTML||'',/正在解析/);assert.match(f.buttons[1].innerHTML||'',/去减少塔/);
  await f.submit('reduce');assert.equal(calls,1);assert.equal(f.state.mode,'increase');
  resolve({import_id:'import-'+'1'.repeat(32),files:[{status:'ready'}]});await first;
  assert.equal(f.context.stepChanges,1);assert.equal(f.state.mode,'increase');
  assert.ok(f.buttons.every(button=>!button.disabled));
  assert.match(f.buttons[0].innerHTML||'',/去增加塔/);
});
test('resuming a failed batch retains its entry mode, completed items and original ID input',async()=>{
  const f=fixture();f.state.bootstrap.limits.file_count=1;f.context.templateIds=()=>['one','two'];
  let calls=0;f.context.apiRequest=async url=>{
    calls++;
    if(calls===2)throw new Error('连接中断');
    if(url==='/api/imports/merge')return {import_id:'import-'+'3'.repeat(32),files:[{status:'ready'},{status:'ready'}]};
    return {import_id:'import-'+String(calls).repeat(32),files:[{status:'ready'}]};
  };
  await f.submit('increase');assert.equal(f.state.importSession.completed,1);
  assert.equal(f.state.importSession.workspaceMode,'increase');assert.equal(f.state.mode,'increase');
  assert.equal(f.buttons[1].disabled,true);assert.equal(f.buttons[0].disabled,false);
  assert.match(f.context.feedback,/继续/);
  await f.submit('reduce');
  assert.equal(calls,4);assert.equal(f.state.mode,'increase');assert.equal(f.context.stepChanges,1);
  assert.deepEqual(f.context.templateIds(),['one','two']);
});
test('invalid or unconfirmed source blocks keyboard submission without changing mode',async()=>{
  for(const source of ['tile','unknown']){
    const f=fixture();f.nodes.get('source-repository').value=source;f.state.settings.tileConfirmed=false;
    let requests=0,invalid='';f.context.apiRequest=async()=>{requests++;};f.context.setInvalid=(_node,message)=>invalid=message;
    await f.context.importTemplates({preventDefault(){},currentTarget:f.nodes.get('import-form')});
    assert.equal(requests,0);assert.equal(f.state.mode,'reduce');assert.match(invalid,/来源|路径/);
  }
});
test('a synchronous parsing setup error keeps the selected mode and restores both import actions',async()=>{
  const f=fixture();f.context.byId('template-ids').value='one';
  f.context.window.TowerReducerBatchImport={createSession(){throw new Error('文件大小不受支持');}};
  await f.submit('increase');
  assert.equal(f.state.mode,'increase');assert.equal(f.state.importBusy,false);
  assert.ok(f.buttons.every(button=>!button.disabled));assert.equal(f.nodes.get('template-ids').value,'one');
  assert.match(f.context.feedback,/文件大小不受支持/);
});
