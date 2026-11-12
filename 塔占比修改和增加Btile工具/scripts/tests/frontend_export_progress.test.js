'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const model = require('../../frontend/assets/view-model.js');
const source = fs.readFileSync(path.join(__dirname, '../../frontend/assets/app.js'), 'utf8');

function setup(request) {
  const nodes = new Map(), notices = [], completed = [], refreshed = [], calls = [];
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, {value:'',dataset:{},hidden:false,disabled:false,textContent:'',
      innerHTML:'',attributes:{},querySelector:()=>node(id+'-text'),
      removeAttribute(key){delete this.attributes[key];}});
    return nodes.get(id);
  };
  const original = {plan_id:'plan-a',confirmation_token:'token-a',export_root:'/original/output',
    items:[{filename:'a_m.bytes',template_key:'a',classification:'create'}]};
  const state = {currentJobId:'job-a',currentJob:{templates:[],artifacts:[{}]},exportPlan:original,
    exportPlanContext:'context-a',exportTask:null,resultSelection:[]};
  const context = {state,byId:node,asObject:v=>v||{},asArray:v=>Array.isArray(v)?v:[],
    asText:(v,f='')=>typeof v==='string'&&v?v:f,escapeHTML:v=>String(v),encodeURIComponent,
    all:()=>[],translationPending:()=>false,planContextFingerprint:()=> 'context-a',
    conflictDecisions:()=>({}),clearFeedback(){},showFeedback:(id,message)=>notices.push([id,message]),
    showToast(){},setStatusPill(){},setDialogPending:(dialog,pending)=>{dialog.dataset.pending=String(pending);},
    invalidatePlans:()=>{state.exportPlan=null;state.exportPlanContext='';},
    closeDialog:dialog=>{dialog.open=false;},openDialog:dialog=>{dialog.open=true;},
    currentJobNamingRules:()=>[],deliverySelection:()=>({selected:[],keys:null}),
    validateExport:()=>true,exportOptions:()=>({export_root:'/original/output'}),
    crypto:{randomUUID:()=> 'preview-request-one'},
    summarizeCommitItems:model.summarizeCommitItems,
    showDeliveryCompletion:(...args)=>completed.push(args),loadJob:async id=>refreshed.push(id),
    abortableDelay:async()=>{},apiRequest:async(url,options)=>{calls.push([url,options.json]);return request(url,options.json);},
  };
  for (const name of ['renderExportPlan','updateExportCommitAvailability','renderExportWriteProgress',
    'awaitExportWrite','awaitExportPreview','commitExport','previewExport','updateNamingRegenerationControl']) {
    const start = source.search(new RegExp(`(?:async )?function ${name}\\(`));
    assert.notEqual(start,-1,`${name} is implemented`);
    const next = source.slice(start+1).search(/\n(?:async )?function /);
    vm.runInNewContext(source.slice(start,next<0?source.length:start+1+next),context);
  }
  return {context,state,node,notices,completed,refreshed,calls,original};
}

function done() { return {task_id:'task-a',job_id:'job-a',plan_id:'plan-a',status:'succeeded',
  phase:'completed',completed:1,total:1,result:{job_id:'job-a',plan_id:'plan-a',ok:true,
    items:[{filename:'a_m.bytes',status:'exported'}],directories:[{key:'original',path:'/original/output'}]}}; }

test('lost start response retains the original request and recovers it after switching views', async()=>{
  let lost=true;
  const {context,state,node,calls,completed,refreshed,original}=setup(async()=>{
    if(lost){lost=false;throw new TypeError('network unavailable');}
    return done();
  });
  await context.commitExport();
  assert.equal(state.exportTask?.jobId,'job-a');
  assert.equal(state.exportTask?.request.confirmation_token,'token-a');
  assert.equal(state.exportTask?.exportRoot,'/original/output');
  state.currentJobId='job-b';state.currentJob={templates:[],artifacts:[]};
  state.exportPlan=null;state.exportPlanContext='';
  context.updateNamingRegenerationControl();
  for(const id of ['preview-export-button','preview-export-bottom-button']) {
    assert.equal(node(id).disabled,false,'recovery stays accessible without eligible current artifacts');
    assert.match(node(id).textContent,/重查/);
  }
  await context.previewExport({preventDefault(){}});
  assert.equal(calls.length,1,'recovery entry must not preview a new export');
  assert.equal(node('export-preview-dialog').open,true);
  assert.equal(node('commit-export-dialog-button').disabled,false);
  assert.match(node('commit-export-dialog-button').textContent,/重查/);
  const unrelated = {plan_id:'plan-b',confirmation_token:'token-b',export_root:'/other/output'};
  state.exportPlan=unrelated;
  await context.commitExport();
  assert.equal(calls[1][0],'/api/jobs/job-a/export');
  assert.deepEqual(JSON.parse(JSON.stringify(calls[1][1])),{
    action:'start',plan_id:'plan-a',confirmation_token:'token-a',conflict_decisions:{}});
  assert.equal(state.currentJobId,'job-b');
  assert.equal(state.exportPlan,unrelated,'the original result does not consume an unrelated plan');
  assert.equal(refreshed.length,0,'recovering job A must not replace the displayed job B');
  assert.equal(completed[0][2][0].path,original.export_root);
  assert.equal(state.exportTask,null);
});

test('poll errors preserve progress and retry only the original task without a fresh start', async()=>{
  let requests=0,recover=false;
  const {context,state,node,calls}=setup(async(_url,payload)=>{
    requests++;
    if(payload.action==='start')return {task_id:'task-a',status:'running',phase:'writing',completed:1,total:2};
    if(!recover)throw new TypeError('temporary disconnection');
    return done();
  });
  await context.commitExport();
  assert.equal(requests,4);
  assert.equal(state.exportTask?.task_id,'task-a');
  assert.match(node('export-write-label').textContent,/1 \/ 2/);
  state.currentJobId='job-b';state.exportPlan=null;state.exportPlanContext='';recover=true;
  await context.commitExport();
  assert.equal(calls.at(-1)[0],'/api/jobs/job-a/export');
  assert.equal(calls.filter(call=>call[1].action==='start').length,1);
  assert.equal(state.exportTask,null);
});

test('pending start disables duplicate submits before the task id is known', async()=>{
  let release;
  const {context,state,node,calls}=setup(()=>new Promise(resolve=>{release=resolve;}));
  const pending=context.commitExport();
  assert.equal(state.exportTask?.request.plan_id,'plan-a');
  assert.equal(node('export-preview-dialog').dataset.pending,'true');
  assert.equal(node('export-write-progress').hidden,false,'preparing feedback is visible before the start response');
  assert.match(node('export-write-label').textContent,/准备/);
  await context.commitExport();assert.equal(calls.length,1);
  release(done());await pending;
  assert.equal(node('export-preview-dialog').dataset.pending,'false');
});

test('preview opens immediately, blocks duplicate starts and displays actual verification progress',async()=>{
  let releaseStart,releaseStatus;
  const {context,state,node,calls}=setup((_url,payload)=>new Promise(resolve=>{
    if(payload.action==='preview_start')releaseStart=resolve;else releaseStatus=resolve;
  }));
  context.deliverySelection=()=>({selected:[{},{}],keys:['a','b']});
  const event={preventDefault(){},currentTarget:node('export-form')};
  const pending=context.previewExport(event);
  assert.equal(node('export-preview-dialog').open,true);
  assert.equal(node('export-write-progress').hidden,false);
  assert.match(node('export-write-label').textContent,/准备.*预览/);
  assert.equal(node('commit-export-dialog-button').disabled,true);
  await context.previewExport(event);
  assert.equal(calls.length,1);
  assert.equal(calls[0][1].action,'preview_start');
  assert.equal(calls[0][1].request_id,'preview-request-one');
  releaseStart({task_id:'preview-a',kind:'preview',status:'running',phase:'verifying',
    verified:1,verification_total:2,completed:0,total:0,current_filename:'b_m.bytes'});
  await new Promise(resolve=>setImmediate(resolve));
  assert.match(node('export-write-label').textContent,/已复核 1 \/ 2/);
  assert.equal(node('export-write-bar').value,1);
  assert.equal(state.exportPlan,null,'a previous confirmation cannot remain actionable while previewing');
  releaseStatus({task_id:'preview-a',kind:'preview',status:'succeeded',phase:'completed',
    verified:2,verification_total:2,result:{plan_id:'new-plan',confirmation_token:'new-token',items:[{classification:'create'}]}});
  await pending;
  assert.equal(state.exportPlan.plan_id,'new-plan');
  assert.equal(node('export-preview-dialog').dataset.pending,'false');
  assert.equal(node('commit-export-dialog-button').disabled,false);
});

test('a lost preview-start response retries the same request and a changed context rejects its late plan',async()=>{
  let lost=true,release;
  const {context,state,node,calls}=setup(()=>{
    if(lost){lost=false;throw new TypeError('connection lost');}
    return new Promise(resolve=>{release=resolve;});
  });
  context.deliverySelection=()=>({selected:[{}],keys:null});
  const event={preventDefault(){},currentTarget:node('export-form')};
  await context.previewExport(event);
  assert.equal(state.exportPreviewTask?.request.request_id,'preview-request-one');
  assert.match(node('commit-export-dialog-button').textContent,/重查.*预览/);
  const pending=context.previewExport(event);
  assert.equal(calls.length,2);
  assert.equal(calls[0][1].request_id,calls[1][1].request_id);
  state.currentJobId='job-b';
  release({task_id:'preview-a',kind:'preview',status:'succeeded',phase:'completed',
    verified:1,verification_total:1,result:{plan_id:'obsolete-plan',items:[]}});
  await pending;
  assert.equal(state.exportPlan,null);
  assert.equal(node('commit-export-dialog-button').disabled,true);
  assert.equal(node('export-preview-dialog').dataset.pending,'false');
});

test('preflight uses verified counts and never labels them as written files',()=>{
  const {context,node}=setup(async()=>{});
  context.renderExportWriteProgress({kind:'write',phase:'preflight',verified:1,verification_total:3,completed:0,total:3});
  assert.match(node('export-write-label').textContent,/已复核 1 \/ 3/);
  assert.match(node('export-write-label').textContent,/尚未写入/);
  assert.equal(node('export-write-bar').value,1);
});

test('preview polling recovery preserves the task and never launches another preview',async()=>{
  let recover=false;
  const {context,state,node,calls}=setup((_url,payload)=>{
    if(payload.action==='preview_start')return {task_id:'preview-a',kind:'preview',status:'running',
      phase:'verifying',verified:1,verification_total:2};
    if(!recover)throw new TypeError('temporary disconnection');
    return {task_id:'preview-a',kind:'preview',status:'succeeded',phase:'completed',verified:2,verification_total:2,
      result:{plan_id:'recovered-plan',confirmation_token:'token',items:[{classification:'create'}]}};
  });
  context.deliverySelection=()=>({selected:[{},{}],keys:null});
  const event={preventDefault(){},currentTarget:node('export-form')};
  await context.previewExport(event);
  assert.equal(state.exportPreviewTask.task_id,'preview-a');
  assert.match(node('export-write-label').textContent,/1 \/ 2/);
  assert.equal(calls.length,4);
  recover=true;
  await context.commitExport();
  assert.equal(calls.filter(call=>call[1].action==='preview_start').length,1);
  assert.equal(calls.at(-1)[1].task_id,'preview-a');
  assert.equal(state.exportPlan.plan_id,'recovered-plan');
});

test('terminal preview failure leaves the dialog recoverable without a stale confirmation',async()=>{
  const {context,state,node}=setup(()=>({task_id:'preview-a',kind:'preview',status:'failed',phase:'failed',
    verified:0,verification_total:2,error:{code:'invalid_export_root',message:'请选择可写目录'}}));
  context.deliverySelection=()=>({selected:[{},{}],keys:null});
  await context.previewExport({preventDefault(){},currentTarget:node('export-form')});
  assert.equal(state.exportPreviewTask,null);
  assert.equal(state.exportPlan,null);
  assert.equal(node('commit-export-dialog-button').disabled,true);
  assert.equal(node('export-plan-status').hidden,false);
  assert.match(node('export-plan-status-text').textContent,/请选择可写目录/);
  assert.equal(node('export-preview-dialog').open,true);
  assert.equal(node('export-preview-dialog').dataset.pending,'false');
});

test('HTTP polling failures cannot turn an accepted write into a fresh-start retry', async()=>{
  const {context,state}=setup(async(_url,payload)=>{
    if(payload.action==='start')return {task_id:'task-a',status:'running',phase:'writing',completed:0,total:1};
    const error=new Error('authentication unavailable');error.status=403;throw error;
  });
  await context.commitExport();
  assert.equal(state.exportTask?.task_id,'task-a');
  assert.equal(state.exportTask?.request.plan_id,'plan-a');
});
