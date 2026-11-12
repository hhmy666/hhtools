'use strict';
const test=require('node:test'), assert=require('node:assert/strict'), fs=require('node:fs'), sandbox=require('node:vm');
const source=fs.readFileSync(require('node:path').join(__dirname,'../../frontend/assets/app.js'),'utf8');
const preview=require('../../frontend/assets/template-preview.js');
function setup(request){
  const nodes=new Map(), node=id=>nodes.get(id) || (nodes.set(id,{value:'',innerHTML:'',textContent:'',disabled:false,className:'',setAttribute(){}}),nodes.get(id));
  const state={currentJobId:'job-one',currentJob:{templates:[{key:'tile-one',templateId:'old'}]},importId:'import-one',importedFiles:[{fileId:'source-one'},{fileId:'source-two'}],previewRequest:0,previewController:null};
  const context={state,byId:node,openDialog:()=>{},apiRequest:request,AbortController,encodeURIComponent,document:{activeElement:{dataset:{}}},
    asObject:v=>v || {},escapeHTML:v=>String(v),window:{TowerTemplatePreview:preview},
    updatePreviewTranslationControls:()=>{},
    renderManualControls:()=>{},
    renderTemplatePreview:()=>{node('rendered').textContent=state.templatePreview.source.template_id;}};
  const controlsStart=source.indexOf('function updatePreviewLayerControls(');
  const controlsEnd=source.indexOf('\nfunction setPreviewLayer(',controlsStart);
  sandbox.runInNewContext(source.slice(controlsStart,controlsEnd),context);
  const start=source.indexOf('async function openTemplatePreview(');
  if(start>=0){
    const next=source.slice(start+1).search(/\n(?:async )?function /);
    sandbox.runInNewContext(source.slice(start,next<0?source.length:start+1+next),context);
  } else context.openTemplatePreview=async()=>{};
  const cancelStart=source.indexOf('function cancelTemplatePreview(');
  if(cancelStart>=0){const next=source.slice(cancelStart+1).search(/\n(?:async )?function /);sandbox.runInNewContext(source.slice(cancelStart,next<0?source.length:cancelStart+1+next),context);}
  return {context,state,node};
}
function loadFunctions(context,names){
  for(const name of names){
    const start=source.search(new RegExp(`(?:async )?function ${name}\\(`));
    const next=source.slice(start+1).search(/\n(?:async )?function /);
    sandbox.runInNewContext(source.slice(start,next<0?source.length:start+1+next),context);
  }
}
test('preview fetch displays real source/new IDs and makes layer controls available after loading',async()=>{
  const payload={source:{template_id:'old',tiles:[{z:0}]},output:{template_id:'lessTold',tiles:[{z:2}]} };
  const {context,node,state}=setup(async(url)=>{assert.equal(url,'/api/jobs/job-one/preview/tile-one');return payload;});
  await context.openTemplatePreview('tile-one');
  assert.equal(state.templatePreview?.output.template_id,'lessTold');
  assert.equal(node('rendered').textContent,'old');
  assert.equal(node('preview-layer').max,'3');
  assert.equal(node('preview-layer').value,'0');
});
test('failed-template tower removal requests a preview-only recovery seed, not generation or export',async()=>{
 const {context,state,node}=setup(async(url,options)=>{
   assert.equal(url,'/api/jobs/job-one/manual-recovery/tile-one');assert.equal(options.method,'POST');
   assert.deepEqual(JSON.parse(JSON.stringify(options.json)),{});
   return {source:{template_id:'old',tiles:[{z:0}]},output:{template_id:'lessTold',tiles:[{z:0}]},manual_recovery:true,recovery_token:'seed'};
 });
 await context.openTemplatePreview('tile-one','recovery');
 assert.equal(state.templatePreview.manual_recovery,true);
 assert.match(node('template-preview-description').textContent,/清塔|手动/);
});
test('response from another job cannot replace a preview after switching tasks',async()=>{
  let finish;
  const {context,state,node}=setup(()=>new Promise(resolve=>finish=resolve));
  const pending=context.openTemplatePreview('tile-one');
  assert.equal(typeof finish,'function');
  state.currentJobId='job-two';
  finish({source:{template_id:'wrong',tiles:[]},output:null});
  await pending;
  assert.equal(state.templatePreview,null);
  assert.notEqual(node('rendered').textContent,'wrong');
});
test('configuration preview reads the registered import without starting a generation or requiring a job',async()=>{
  const {context,state,node}=setup(async(url,options)=>{
    assert.equal(url,'/api/imports/import-one/preview/source-one');assert.equal(options.method,undefined);
    return {source:{template_id:'original',tiles:[{z:3}]},output:null,changed_count:0};
  });
  state.currentJobId='';state.currentJob=null;
  await context.openTemplatePreview('source-one','import');
  assert.equal(state.templatePreview.source.template_id,'original');assert.equal(node('preview-layer').max,'4');
  assert.equal(node('template-preview-title').textContent,'原模板预览');
});
test('switching import templates aborts the old request and ignores its late response',async()=>{
  const pending=[];const {context,state}=setup((_url,options)=>new Promise(resolve=>pending.push({resolve,signal:options.signal})));
  const one=context.openTemplatePreview('source-one','import');
  const two=context.openTemplatePreview('source-two','import');assert.equal(pending[0].signal.aborted,true);
  pending[1].resolve({source:{template_id:'second',tiles:[]},output:null});await two;
  pending[0].resolve({source:{template_id:'late-first',tiles:[]},output:null});await one;
  assert.equal(state.templatePreview.source.template_id,'second');
});
test('closing an import preview cancels loading and isolates the delayed result',async()=>{
  let finish,signal;const {context,state}=setup((_url,options)=>{signal=options.signal;return new Promise(resolve=>finish=resolve);});
  const pending=context.openTemplatePreview('source-one','import');
  assert.equal(typeof context.cancelTemplatePreview,'function');context.cancelTemplatePreview();assert.equal(signal.aborted,true);
  finish({source:{template_id:'closed',tiles:[]},output:null});await pending;assert.equal(state.templatePreview,null);
});
test('changing import identity prevents a failed old request from replacing current feedback',async()=>{
  let fail;const {context,state,node}=setup(()=>new Promise((_resolve,reject)=>fail=reject));
  const pending=context.openTemplatePreview('source-one','import');state.importId='import-two';node('preview-feedback').textContent='current';
  fail(new Error('old error'));await pending;assert.equal(node('preview-feedback').textContent,'current');
});
test('source-only import view uses the shared real renderer and cumulative layer control',async()=>{
  const {context,node}=setup(async()=>({source:{template_id:'source',towers:[],tiles:[
    {index:0,x:4,y:4,z:0,tile_id:-10,tower_index:null},{index:1,x:6,y:6,z:2,tile_id:-11,tower_index:null},
  ]},output:null,changed_count:0}));
  loadFunctions(context,['renderTemplatePreview','setPreviewLayer','showAllPreviewLayers']);
  await context.openTemplatePreview('source-one','import');
  assert.match(node('preview-source-board').innerHTML,/data-tile-index="1"/);assert.equal(node('preview-output-side').hidden,true);
  assert.equal(node('preview-moves').hidden,true);assert.equal(node('preview-change-control').hidden,true);
  context.setPreviewLayer(1);assert.match(node('preview-source-board').innerHTML,/data-tile-index="0"/);
  assert.doesNotMatch(node('preview-source-board').innerHTML,/data-tile-index="1"/);
  context.showAllPreviewLayers();assert.match(node('preview-source-board').innerHTML,/data-tile-index="1"/);
});
test('read-only source preview describes layer viewing without implying missing generated results',async()=>{
  const {context,node}=setup(async()=>({source:{template_id:'source',towers:[],tiles:[
    {index:0,x:4,y:4,z:0,tile_id:-10,tower_index:null},
  ]},output:null,changed_count:0}));
  loadFunctions(context,['renderTemplatePreview']);
  await context.openTemplatePreview('source-one','import');
  assert.match(node('preview-feedback').textContent,/原模板只读.*按层查看/);
  assert.equal(node('preview-source-summary').textContent,'显示 1 张 · 完全无遮盖 1 张');
  assert.equal(node('preview-move-legend').hidden,true);
  await context.openTemplatePreview('tile-one');
  assert.equal(node('preview-feedback').textContent,'仅显示原模板；当前没有可用的新模板。');
  assert.match(node('preview-source-summary').textContent,/新增 0 张 · 挪动 0 张/);
  assert.equal(node('preview-move-legend').hidden,false);
});
test('shared dialog restores the original row focus after replacing a source preview and closing',async()=>{
  const {context,node}=setup(async()=>({source:{template_id:'source',tiles:[]},output:null}));
  let focused='row';const trigger={focus(){focused='row';}},close={focus(){focused='close';}};
  context.document={activeElement:trigger,contains:element=>element===trigger};
  const dialog=node('template-preview-dialog');dialog.dataset={};dialog.open=false;
  dialog.showModal=()=>{dialog.open=true;context.document.activeElement=close;};dialog.querySelector=()=>close;
  dialog.close=()=>{dialog.open=false;context.cancelTemplatePreview();};
  loadFunctions(context,['openDialog','closeDialog']);
  await context.openTemplatePreview('source-one','import');assert.equal(focused,'close');assert.equal(node('app-shell').inert,true);
  await context.openTemplatePreview('source-two','import');context.closeDialog(dialog);
  assert.equal(focused,'row');assert.equal(node('app-shell').inert,false);
});
