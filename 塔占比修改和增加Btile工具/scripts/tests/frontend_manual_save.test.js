const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync(require('node:path').join(__dirname,'../../frontend/assets/app.js'),'utf8');
test('save click requests validation, locates failures and keeps the editable draft',async()=>{
 const state={manualDraft:{history:[{}]},manualError:false};let located=0,rendered=0;
 const ctx={state,translationPending:()=>false,window:{TowerManualEdit:{saveProblem:draft=>{assert.equal(draft.validationRequested,true);return '第3层没有承接';},issues:()=>[{blocking:true}]}},locateManualIssue(){located++;},renderTemplatePreview(){rendered++;}};
 const start=source.indexOf('async function saveManualDraft('),end=source.indexOf('\nfunction exitPreviewEditing(',start);vm.runInNewContext(source.slice(start,end),ctx);
 assert.equal(await ctx.saveManualDraft(),false);assert.equal(state.manualDraft.validationRequested,true);assert.equal(state.manualError,true);assert.equal(located,1);assert.equal(rendered,1);
});
test('a removal-only draft commits its recovery token without requiring a prior artifact or additions',async()=>{
 const state={manualDraft:{history:[],recovery:true},currentJobId:'job',previewTemplateKey:'tile',previewRequest:1,
  templatePreview:{manual_recovery:true,recovery_token:'seed',output:{}},translationBusy:null};
 let posted;
 const ctx={state,byId:()=>({open:false}),encodeURIComponent,translationPending:()=>false,
  window:{TowerManualEdit:{saveProblem:()=>'',payload:()=>({deleted_indices:[],additions:[]})}},invalidatePlans(){},renderTemplatePreview(){},updateNamingRegenerationControl(){},
  apiRequest:async(url,opts)=>{posted={url,payload:opts.json};return {artifact_id:'saved',output:{}};},
  loadJob:async()=>{state.currentJob={templates:[{key:'tile',artifacts:[{id:'saved'}]}]};}};
 const start=source.indexOf('async function saveManualDraft('),end=source.indexOf('\nfunction exitPreviewEditing(',start);
 vm.runInNewContext(source.slice(start,end),ctx);
 assert.equal(await ctx.saveManualDraft(),true);
 assert.equal(posted.url,'/api/jobs/job/manual-recovery/tile');
 assert.deepEqual(JSON.parse(JSON.stringify(posted.payload)),{recovery_token:'seed',deleted_indices:[],additions:[]});
 assert.equal(state.manualDraft,null);
});
test('pending manual save cannot be discarded through close, then restores entry focus after success',async()=>{
 let finish,focused=0;
 const nodes=new Map(),node=id=>{if(!nodes.has(id))nodes.set(id,{id,open:id==='template-preview-dialog',dataset:{},focus(){focused++;},close(){this.open=false;}});return nodes.get(id);};
 const state={manualDraft:{history:[{}]},currentJobId:'job',previewTemplateKey:'tile',previewRequest:1,templatePreview:{artifact_id:'a',output:{}},translationBusy:null};
 const context={state,byId:node,encodeURIComponent,window:{TowerManualEdit:{saveProblem:()=>'',payload:()=>({deleted_indices:[],additions:[]})}},
  invalidatePlans:()=>{},renderTemplatePreview:()=>{},updateNamingRegenerationControl:()=>{},
  apiRequest:()=>new Promise(resolve=>finish=resolve),loadJob:async()=>{state.currentJob={templates:[{key:'tile',artifacts:[{id:'b'}]}]};}};
 for(const name of ['translationPending','closeDialog','saveManualDraft']){
  const start=source.search(new RegExp(`(?:async )?function ${name}\\(`)),next=source.slice(start+1).search(/\n(?:async )?function /);
  vm.runInNewContext(source.slice(start,next<0?source.length:start+1+next),context);
 }
 const pending=context.saveManualDraft();assert.equal(context.translationPending(),true);
 context.closeDialog(node('template-preview-dialog'));assert.equal(node('template-preview-dialog').open,true);assert.equal(node('manual-close-dialog').open,false);
 finish({artifact_id:'b',output:{}});assert.equal(await pending,true);assert.equal(state.manualDraft,null);assert.equal(focused,1);
});
test('late manual save response never replaces a newer artifact loaded from the job',async()=>{
 const old={artifact_id:'original',output:{template_id:'old'}};
 const state={manualDraft:{},currentJobId:'job',previewTemplateKey:'tile',previewRequest:1,templatePreview:old,translationBusy:null};
 const context={state,encodeURIComponent,window:{TowerManualEdit:{saveProblem:()=>'',payload:()=>({deleted_indices:[],additions:[]})}},
  translationPending:()=>false,invalidatePlans:()=>{},renderTemplatePreview:()=>{},updateNamingRegenerationControl:()=>{},
  apiRequest:async()=>({artifact_id:'save-one',output:{template_id:'late'}}),
  loadJob:async()=>{state.currentJob={templates:[{key:'tile',artifacts:[{id:'save-two'}]}]};}};
 const start=source.indexOf('async function saveManualDraft('),end=source.indexOf('\nfunction bindManualEditor(',start);
 vm.runInNewContext(source.slice(start,end),context);await context.saveManualDraft();
 assert.equal(state.templatePreview.artifact_id,'original');assert.ok(state.manualDraft);assert.match(state.manualMessage,/产物.*变化|新版本/);
});
