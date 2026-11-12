'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../../frontend/assets/app.js'), 'utf8');
const model = require('../../frontend/assets/view-model.js');
const renderer = require('../../frontend/assets/template-preview.js');

function load(context, names) {
  if(names.includes('updateNamingRegenerationControl')&&!context.deliverySelection)load(context,['deliverySelection']);
  if (!context.translationPending && !names.includes('translationPending')) load(context, ['translationPending']);
  if (!context.updatePreviewTranslationControls) context.updatePreviewTranslationControls = () => {};
  if (!context.renderManualControls) context.renderManualControls = () => {};
  for (const name of names) {
    const start = source.search(new RegExp(`(?:async )?function ${name}\\(`));
    if (start < 0) { context[name] = () => {}; continue; }
    const end = source.slice(start + 1).search(/\n(?:async )?function /);
    vm.runInNewContext(source.slice(start, end < 0 ? source.length : start + 1 + end), context);
  }
}
function nodes() {
  const map = new Map();
  const get = id => {
    if (!map.has(id)) map.set(id, {value: '', textContent: '', innerHTML: '', disabled: false,
      hidden: false, dataset: {}, attributes: {}, classList: {toggle() {}},
      setAttribute(k,v) { this.attributes[k] = String(v); }, querySelector() { return get(id + '-text'); }});
    map.get(id).removeAttribute = k => { delete map.get(id).attributes[k]; };
    return map.get(id);
  };
  return get;
}
test('both export preview entries share artifact eligibility without blocking naming drafts',()=>{
  const node=nodes(); node('export-naming-rule').value='moreB{原模板ID}';
  const state={currentJobId:'job',currentJob:{status:'succeeded',artifacts:[{}]},namingRegenerationActive:false};
  let rules=['moreB{原模板ID}'];
  const ctx={state,byId:node,currentJobNamingRules:()=>rules,TERMINAL_JOB_STATES:new Set(['succeeded']),setStatusPill:()=>{}};
  load(ctx,['updateNamingRegenerationControl']);
  for(const [artifacts,naming,expected] of [[[],rules,true],[[{}],rules,false],[[{}],['changed'],false]]){
    state.currentJob.artifacts=artifacts; rules=naming; ctx.updateNamingRegenerationControl();
    assert.equal(node('preview-export-button').disabled,expected);
    assert.equal(node('preview-export-bottom-button').disabled,expected);
  }
});
test('existing verified templates remain previewable when a different naming draft has not been applied',()=>{
  const node=nodes();node('export-naming-rule').value='new{原模板ID}';
  const state={currentJobId:'job',currentJob:{status:'succeeded',artifacts:[{}]},namingRegenerationActive:false};
  const ctx={state,byId:node,currentJobNamingRules:()=>['old{原模板ID}'],TERMINAL_JOB_STATES:new Set(['succeeded']),setStatusPill:()=>{}};
  load(ctx,['updateNamingRegenerationControl']);ctx.updateNamingRegenerationControl();
  assert.equal(node('preview-export-button').disabled,false);
  assert.match(node('export-availability-hint').textContent,/命名.*未.*应用|命名.*变化/);
});
test('delivery selection never widens an empty eligible selection to all templates',()=>{
 const state={resultSelection:[],currentJob:{artifacts:[{template_key:'a'},{template_key:'b'}]}};
 const ctx={state};load(ctx,['deliverySelection']);
 assert.equal(ctx.deliverySelection().selected.length,2);assert.equal(ctx.deliverySelection().keys,null);
 state.resultSelection=['b','no-op'];
 assert.equal(JSON.stringify(ctx.deliverySelection().keys),'["b"]');
 state.resultSelection=['no-op'];assert.equal(ctx.deliverySelection().selected.length,0);
 assert.equal(JSON.stringify(ctx.deliverySelection().keys),'[]');
});
test('polling a selected queued row retains its scope and excludes stale artifacts',()=>{
 const node=nodes();const state={resultSelection:['b'],resultSelectionJob:'job',currentJob:{id:'job',templates:[{key:'a',status:'verified'},{key:'b',status:'queued'}],artifacts:[{template_key:'a'},{template_key:'b'}]}};
 const ctx={state,byId:node,VM:model,all:()=>[],updateNamingRegenerationControl(){}};
 load(ctx,['updateResultSelection','deliverySelection']);ctx.updateResultSelection();
 assert.equal(JSON.stringify(state.resultSelection),'["b"]');assert.equal(ctx.deliverySelection().selected.length,0);
 assert.equal(node('batch-rerun-button').disabled,true);
 state.currentJob.templates[1].status='verified';ctx.updateResultSelection();
 assert.equal(JSON.stringify(ctx.deliverySelection().keys),'["b"]');
});
test('selected export applies naming only to delivery without rerunning or altering current artifacts',async()=>{
 const node=nodes(),calls=[],dialogs=[],validations=[];
 node('export-naming-rule').dataset.edited='true';node('export-naming-rule').value='new';
 const state={currentJobId:'job',currentJob:{},resultSelection:['b']};
 const ctx={state,byId:node,clearFeedback(){},showFeedback(){},validateExport:(_form,naming)=>{validations.push(naming);return true;},
 exportOptions:()=>({naming_rule:'new',export_root:'/tmp/out'}),currentJobNamingRules:()=>['old'],
 updateNamingRegenerationControl(){},openDialog:d=>dialogs.push(d),deliverySelection:()=>({selected:[{}],keys:['b']}),
 setDialogPending(){},updateExportCommitAvailability(){},
 planContextFingerprint:()=> 'same',apiRequest:async(...args)=>{calls.push(args);return {items:[]};},
 asObject:v=>v,asArray:v=>v,setStatusPill(){},renderExportPlan(){},encodeURIComponent};
 load(ctx,['renderExportWriteProgress','previewExport']);const event={preventDefault(){},currentTarget:node('export-form')};
 await ctx.previewExport(event);assert.equal(calls.length,1);assert.match(calls[0][0],/\/export$/);
 assert.equal(JSON.stringify(calls[0][1].json.template_keys),'["b"]');assert.equal(calls[0][1].json.naming_rule,'new');assert.equal(validations[0],true);
 assert.equal(dialogs.includes(node('export-naming-choice-dialog')),false);
});
test('ZIP download posts selected keys, blocks duplicate requests and recovers from errors',async()=>{
 const state={currentJobId:'job',zipBusy:false},calls=[],notices=[];
 let finish,downloaded=0,revoked=0;
 const ctx={state,byId:nodes(),deliverySelection:()=>({selected:[{}],keys:['chosen']}),updateNamingRegenerationControl(){},
 apiRequest:(...args)=>{calls.push(args);return new Promise(resolve=>{finish=resolve;});},
 URL:{createObjectURL:()=> 'blob:zip',revokeObjectURL:()=>{revoked++;}},document:{createElement:()=>({click(){downloaded++;}})},
 window:{setTimeout:fn=>fn()},showToast:message=>notices.push(message),encodeURIComponent};
 load(ctx,['downloadBundle']);const pending=ctx.downloadBundle();await ctx.downloadBundle();
 assert.equal(calls.length,1);assert.equal(calls[0][1].binary,true);
 assert.equal(JSON.stringify(calls[0][1].json),'{"template_keys":["chosen"]}');
 finish({});await pending;assert.equal(downloaded,1);assert.equal(revoked,1);assert.equal(state.zipBusy,false);
 ctx.apiRequest=async()=>{throw new Error('已失效');};await ctx.downloadBundle();
 assert.equal(state.zipBusy,false);assert.match(notices[0],/下载失败.*已失效/);
});
test('sync defaults initialize eligible choices without overwriting this batch on later settings reads',()=>{
  const node=nodes(), current=['template','tile'].map(value=>({value,checked:false}));
  const defaults=['template','tile'].map(value=>({value,checked:false}));
  const ctx={state:{syncDefaultsInitialized:false},all:selector=>selector.includes('default_sync_targets')?defaults:current};
  load(ctx,['applySyncDefaults']);
  const settings={defaultSyncTargets:['template','tile'],templateConfirmed:true,tileConfirmed:false};
  ctx.applySyncDefaults(settings);
  assert.deepEqual(current.map(i=>i.checked),[true,false]);
  assert.deepEqual(defaults.map(i=>i.checked),[true,true]);
  current[0].checked=false;
  ctx.applySyncDefaults({...settings,tileConfirmed:true});
  assert.deepEqual(current.map(i=>i.checked),[false,false]);
  ctx.applySyncDefaults({...settings,tileConfirmed:true},true);
  assert.deepEqual(current.map(i=>i.checked),[true,true]);
  ctx.applySyncDefaults({...settings,defaultSyncTargets:[]},true);
  assert.deepEqual(current.map(i=>i.checked),[false,false]);
});
function layers() {
  const node = nodes();
  const tile = (index,z) => ({index,z,x:4,y:4,tile_id:-10,tower_index:null,changed:false});
  const state = {templatePreview: {source:{template_id:'old',towers:[],tiles:[tile(0,0),tile(1,2)]},
    output:{template_id:'new',towers:[],tiles:[tile(0,0),tile(1,5)]},changed_count:1}};
  node('preview-mode').value = 'all'; node('preview-layer').value = '6'; node('preview-mark-changes').checked = true;
  const ctx = {state,byId:node,window:{TowerTemplatePreview:renderer},escapeHTML:v=>String(v)};
  load(ctx, ['updatePreviewLayerControls','renderTemplatePreview','setPreviewLayer','stepPreviewLayer','showAllPreviewLayers']);
  return {node,ctx,state};
}
test('all layers keeps the slider operable, using the original/new union maximum', () => {
  const {ctx,node} = layers(); ctx.renderTemplatePreview();
  assert.equal(node('preview-layer').disabled, false);
  assert.equal(Number(node('preview-layer').max), 6);
  assert.equal(node('preview-layer-value').textContent, '6 层');
  assert.equal(node('preview-all-layers').attributes['aria-pressed'], 'false');
});
test('minus, plus and slider change both boards cumulatively and clamp boundaries', () => {
  const {ctx,node} = layers();
  ctx.stepPreviewLayer(-1);
  assert.equal(node('preview-layer').value, '5');
  assert.match(node('preview-source-board').innerHTML, /data-tile-index="1"/);
  assert.doesNotMatch(node('preview-output-board').innerHTML, /data-tile-index="1"/);
  ctx.stepPreviewLayer(1); assert.equal(node('preview-layer').value, '6');
  ctx.setPreviewLayer(0); assert.equal(node('preview-layer').value, '0');
  assert.equal(node('preview-layer-minus').disabled, true);
  ctx.setPreviewLayer(999); assert.equal(node('preview-layer').value, '6');
  assert.equal(node('preview-layer-plus').disabled, true);
});
test('fixed layers allow single viewing, unlimited clears and disables single viewing', () => {
  const {ctx,node} = layers();
  node('preview-mode').value='single'; ctx.setPreviewLayer(3);
  assert.equal(node('preview-single-layer').disabled,false);
  assert.match(node('preview-source-board').innerHTML, /data-tile-index="0"/);
  node('preview-single-layer').checked=true;ctx.renderTemplatePreview();
  assert.doesNotMatch(node('preview-source-board').innerHTML,/data-tile-index="0"/);
  ctx.showAllPreviewLayers();
  assert.equal(node('preview-layer').value, '0');
  assert.equal(node('preview-single-layer').disabled,true);
  assert.equal(node('preview-single-layer').checked,false);
  assert.equal(node('preview-layer-value').textContent,'全部层');
  assert.match(node('preview-output-board').innerHTML, /data-tile-index="1"/);
});
test('rerendering keeps the chosen ceiling instead of resetting it to the top',()=>{
  const {ctx,node}=layers(); ctx.setPreviewLayer(2);
  node('preview-mode').value='all'; ctx.renderTemplatePreview();
  assert.equal(node('preview-layer').value,'2');
  assert.equal(node('preview-layer-value').textContent,'2 层');
  assert.doesNotMatch(node('preview-output-board').innerHTML,/data-tile-index="1"/);
});

function commitContext(kind, request) {
  const node=nodes(), notices=[], completions=[];
  const plan={plan_id:'plan',confirmation_token:'token',export_root:'/tmp/output',
    rows:[{filename:'new_m.bytes',targets:[{binding_key:'tile',binding_path:'/tmp/tile/Tile/Assets/Resources/TileMatchLevelData',classification:'create'}]}]};
  const state={currentJobId:'job',exportPlan:plan,exportPlanContext:'ctx',syncPlan:plan,syncPlanContext:'ctx["tile"]'};
  const ctx={state,byId:node,VM:model,asObject:v=>v||{},asArray:v=>Array.isArray(v)?v:[],asText:(v,f='')=>typeof v==='string'&&v?v:f,
    planContextFingerprint:()=> 'ctx',invalidatePlans:()=>{},all:()=>[],selectedBindings:()=>['tile'],
    setDialogPending:()=>{},setStatusPill:()=>{},showFeedback:()=>{},closeDialog:()=>{},
    showToast:(...args)=>notices.push(args),apiRequest:request,conflictDecisions:()=>({}),
    showDeliveryCompletion:(...args)=>completions.push(args),
    summarizeCommitItems:model.summarizeCommitItems,loadJob:async()=>{},updateNamingRegenerationControl:()=>{},
    updateExportCommitAvailability:()=>{},updateSyncCommitAvailability:()=>{},encodeURIComponent};
  load(ctx,kind==='export'?['renderExportWriteProgress','commitExport']:['commitSync']);
  return {ctx,node,state,notices,completions};
}
test('export completion dialog waits for commit and uses server-issued directory actions', async () => {
  let finish;
  const {ctx,notices,completions}=commitContext('export',()=>new Promise(resolve=>{finish=resolve;}));
  const pending=ctx.commitExport(); assert.equal(completions.length,0);
  finish({ok:true,items:[{status:'exported',filename:'new_m.bytes'}],directories:[{key:'issued',label:'输出目录',path:'/tmp/output'}]}); await pending;
  assert.equal(notices.length,0);
  assert.equal(completions.length,1); assert.equal(completions[0][0],'导出成功');
  assert.equal(completions[0][2][0].key,'issued'); assert.equal(completions[0][2][0].path,'/tmp/output');
});
test('sync success names the actual target, including identical-content completion', async () => {
  for(const status of ['synced','skipped']) {
    const {ctx,notices,completions}=commitContext('sync',async()=>({status:'committed',rows:[{status,filename:'new_m.bytes',targets:[]}],
      directories:[{key:'a',label:'模板仓',path:'/tmp/template'},{key:'b',label:'Tile 仓',path:'/tmp/tile'}]}));
    await ctx.commitSync();
    assert.equal(notices.length,0); assert.equal(completions.length,1);
    assert.equal(completions[0][2].length,2);
    if(status==='skipped') assert.match(completions[0][1],/相同/);
  }
});
test('failed, partial and audit-failed commits never display a success notice', async () => {
  const scenarios=[['export',{ok:false,items:[{status:'exported'},{status:'failed'}]}],
    ['sync',{status:'partial_failed',rows:[{status:'synced'},{status:'failed'}]}],
    ['sync',{status:'audit_failed',rows:[{status:'synced'}]}]];
  for(const [kind,result] of scenarios) {
    const {ctx,notices,completions}=commitContext(kind,async()=>result);
    await ctx[kind==='export'?'commitExport':'commitSync']();
    assert.equal(notices.some(row=>row[1]==='success'),false);
    assert.equal(completions.length,0);
  }
});

test('opening a completed directory sends only its issued key and keeps failures recoverable',async()=>{
  const node=nodes(), calls=[];
  const button=node('open'); button.dataset.directoryKey='allowed';
  const dialog=node('delivery-completion-dialog'); dialog.open=true;
  const ctx={state:{deliveryCompletion:1,deliveryDirectories:[{key:'allowed',path:'/tmp/output'}]},byId:node,
    apiRequest:async(...args)=>{calls.push(args);throw new Error('目录不可访问');},
    showFeedback:(id,message)=>{node(id).textContent=message;node(id).hidden=false;}};
  load(ctx,['openCompletedDirectory']);
  await ctx.openCompletedDirectory(button);
  assert.equal(calls.length,1); assert.equal(calls[0][0],'/api/open-directory');
  assert.equal(JSON.stringify(calls[0][1].json),' {"key":"allowed"}'.trim());
  assert.match(node('completion-feedback').textContent,/不可访问/);
  assert.equal(button.disabled,false); assert.equal(dialog.open,true);
  button.dataset.directoryKey='invented'; await ctx.openCompletedDirectory(button);
  assert.equal(calls.length,1);
});

test('completion dialog remembers the page trigger, not the dismissed commit dialog',()=>{
  const node=nodes(), state={deliveryCompletion:0};
  node('completion-acknowledge').focus=()=>{};
  const ctx={state,byId:node,asArray:v=>v||[],asText:(v,f)=>v||f,escapeHTML:v=>String(v),
    openDialog:()=>{state.lastFocused=node('dismissed-dialog-button');},showFeedback:()=>{}};
  load(ctx,['showDeliveryCompletion']);
  ctx.showDeliveryCompletion('导出成功','已完成',[{key:'issued',label:'输出目录',path:'/tmp/out'}],'preview-export-button');
  assert.equal(state.lastFocused,node('preview-export-button'));
});
test('request errors keep recovery feedback and do not announce completion',async()=>{
  for(const kind of ['export','sync']) {
    const {ctx,node,notices}=commitContext(kind,async()=>{throw new Error('network unavailable');});
    await ctx[kind==='export'?'commitExport':'commitSync']();
    assert.equal(notices.length,0);
    assert.equal(node(kind==='export'?'export-plan-status':'sync-recovery-status').hidden,false);
  }
});

test('shared toast centers completion, replaces the previous notice and dismisses at its timeout',()=>{
  const timers=new Map(), classes=new Set(); let clock=0;
  const create=()=>({children:[],attributes:{},textContent:'',className:'',removed:false,
    setAttribute(k,v){this.attributes[k]=v;},append(...items){this.children.push(...items);},
    remove(){this.removed=true;}});
  const region={children:[],classList:{toggle(k,on){if(on)classes.add(k);else classes.delete(k);}},
    append(t){this.children.push(t);},replaceChildren(...items){this.children=items;}};
  const context={state:{toastTimer:null},document:{createElement:create},byId:()=>region,
    window:{setTimeout(fn,ms){const id=++clock;timers.set(id,{fn,ms});return id;},clearTimeout(id){timers.delete(id);}}};
  load(context,['showToast']);
  context.showToast('导出成功','success',{placement:'center',detail:'已导出至 /tmp/output',duration:2400});
  assert.equal(classes.has('is-centered'),true);
  assert.equal(region.children[0].attributes.role,'status');
  assert.equal(region.children[0].children[1].textContent,'已导出至 /tmp/output');
  assert.equal(timers.get(clock).ms,2400);
  context.showToast('已保存');
  assert.equal(region.children.length,1,'no overlapping notification stack');
  assert.equal(classes.has('is-centered'),false);
  assert.equal(timers.size,1,'old dismissal cannot erase the new notification');
  const toast=region.children[0]; timers.get(clock).fn(); assert.equal(toast.removed,true);
});
