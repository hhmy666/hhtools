const test=require('node:test'),assert=require('node:assert/strict');
const E=require('../../frontend/assets/manual-edit.js');
const fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync(require('node:path').join(__dirname,'../../frontend/assets/app.js'),'utf8');
const board=()=>({width:17,height:19,tiles:[{index:0,x:4,y:5,z:0,tile_id:-10},{index:1,x:8,y:10,z:1,tile_id:-11},{index:2,x:8,y:10,z:2,tile_id:-11,tower_index:0}],towers:[{index:0,x:8,y:10,direction:0,count:2}]});
test('clockwise rotates all layers and tower direction about the canvas center; four turns restore',()=>{
 const d=E.create(board()),r=E.rotate(d,'cw').draft;
 assert.equal(r.board.tiles[0].x,12);assert.equal(r.board.tiles[0].y,5);assert.equal(r.board.tiles[1].z,1);
 assert.equal(r.board.towers[0].x,7);assert.equal(r.board.towers[0].y,9);assert.equal(r.board.towers[0].direction,3);
 let four=d;for(let i=0;i<4;i++)four=E.rotate(four,'cw').draft;
 assert.deepEqual(four.board,d.board);assert.deepEqual(E.undo(r).board,d.board);
});
test('translation, rotation and additions compose exactly once in final coordinates',()=>{
 let d=E.translate(E.create(board()),2,1).draft;
 d=E.apply(d,{action:'add',x:6,y:6,z:0,tile_id:-11}).draft;
 d=E.rotate(d,'cw').draft;
 const p=E.payload(d);
 assert.deepEqual(p.transform,{quarter_turns:1,dx:-1,dy:2});assert.equal(p.translation,undefined);
 assert.deepEqual(p.additions,[{x:11,y:7,z:0,tile_id:-11}]);
 assert.equal(d.board.tiles[0].x,11);assert.equal(d.board.tiles[0].y,7);
 const undo=E.undo(d);assert.deepEqual(E.payload(undo).translation,{dx:2,dy:1});
});
test('rotation and bulk layer removal never prevalidate geometry or counts',()=>{
 const b=board();b.tiles[0].y=18;
 const d=E.rotate(E.create(b),'cw').draft;assert.ok(d);assert.ok(E.saveProblem(d));
 const cut=E.clearLayer(d,2,'layer').draft;
 assert.equal(cut.board.tiles.some(t=>t.z===1),false);assert.equal(cut.board.tiles.some(t=>t.z===2),true);
 assert.deepEqual(E.undo(cut).board,d.board);
});
test('delete above excludes selected layer but includes hidden higher levels; one undo restores pending additions',()=>{
 let d=E.apply(E.create(board()),{action:'add',x:10,y:10,z:5,tile_id:-10}).draft;
 const before=E.payload(d),r=E.clearLayer(d,2,'above');
 assert.equal(r.deletedCount,2);assert.equal(r.draft.board.tiles.length,2);
 assert.equal(r.draft.board.tiles.some(t=>t.z===1),true);assert.deepEqual(r.draft.additions,[]);
 assert.deepEqual(E.payload(E.undo(r.draft)),before);
 assert.ok(E.clearLayer(d,0,'above').error);assert.equal(E.clearLayer(d,8,'layer').draft,d);
});
function ui(request=async()=>({artifact_id:'b',output:board()})){
 const nodes=new Map(),node=id=>{if(!nodes.has(id))nodes.set(id,{value:'2',open:false,focus(){},textContent:''});return nodes.get(id);};
 const state={currentJobId:'j',previewJobId:'j',previewTemplateKey:'k',previewRequest:1,requestGeneration:1,currentJob:{status:'succeeded',templates:[{key:'k',status:'verified',artifacts:[{id:'a'}]}]},templatePreview:{artifact_id:'a',output:board()}};
 const ctx={state,byId:node,window:{TowerManualEdit:E},TERMINAL_JOB_STATES:new Set(['succeeded']),encodeURIComponent,apiRequest:request,renderTemplatePreview(){},updateNamingRegenerationControl(){},invalidatePlans(){},openDialog(d){d.open=true;},closeDialog(d){d.open=false;},loadJob:async()=>{state.currentJob.templates[0].artifacts=[{id:'b'}];}};
 for(const name of ['translationPending','previewTranslationAvailable','rotateTemplatePreview','undoTemplatePreview','requestLayerDeletion','confirmLayerDeletion']){
  const start=source.search(new RegExp(`(?:async )?function ${name}\\(`)),end=source.slice(start+1).search(/\n(?:async )?function /);
  vm.runInNewContext(source.slice(start,end<0?source.length:start+1+end),ctx);
 }
 return {ctx,state,node};
}
test('nonedit rotation posts current artifact once, publishes saved board, and ignores late previews',async()=>{
 let finish,calls=0;
 const {ctx,state}=ui((url,opts)=>{calls++;assert.equal(url,'/api/jobs/j/rotate/k');assert.deepEqual(JSON.parse(JSON.stringify(opts.json)),{artifact_id:'a',direction:'cw'});return new Promise(resolve=>finish=resolve);});
 const pending=ctx.rotateTemplatePreview('cw');await ctx.rotateTemplatePreview('cw');assert.equal(calls,1);assert.equal(state.templatePreview.artifact_id,'a');
 finish({artifact_id:'b',output:board()});await pending;assert.equal(state.templatePreview.artifact_id,'b');assert.equal(state.translationBusy,null);
 const later=ui(()=>new Promise(resolve=>finish=resolve));const old=later.ctx.rotateTemplatePreview('cw');later.state.previewRequest++;finish({artifact_id:'b',output:board()});await old;assert.equal(later.state.templatePreview.artifact_id,'a');
});
test('editing rotation stays local; rejected saved rotation preserves the prior artifact',async()=>{
 let calls=0;const {ctx,state}=ui(async()=>{calls++;const e=new Error('旋转后越界');e.code='edit_verification_failed';throw e;});
 state.manualDraft=E.create(board());await ctx.rotateTemplatePreview('cw');assert.equal(calls,0);assert.equal(state.manualDraft.quarterTurns,1);
 state.manualDraft=null;await ctx.rotateTemplatePreview('cw');assert.equal(state.templatePreview.artifact_id,'a');assert.match(state.translationFeedback,/越界/);
});
test('bulk deletion confirms actual count, opens a draft, supports undo, and ignores stale confirmation',()=>{
 const {ctx,state,node}=ui();ctx.requestLayerDeletion('above');
 assert.equal(state.manualDraft,undefined);assert.equal(node('manual-layer-delete-dialog').open,true);assert.match(node('manual-layer-delete-description').textContent,/1 张.*不含所选层/);
 ctx.confirmLayerDeletion();assert.equal(state.manualDraft.board.tiles.length,2);assert.equal(state.templatePreview.output.tiles.length,3);assert.equal(E.undo(state.manualDraft).board.tiles.length,3);
 state.manualDraft=null;ctx.requestLayerDeletion('layer');state.previewRequest++;ctx.confirmLayerDeletion();assert.equal(state.manualDraft,null);
});
test('nonediting undo uses current artifact and blocks duplicate requests; draft undo remains local',async()=>{
 let finish,calls=0;
 const {ctx,state}=ui((url,options)=>{calls++;assert.equal(url,'/api/jobs/j/undo/k');assert.equal(options.json.artifact_id,'a');return new Promise(resolve=>finish=resolve);});
 state.templatePreview.can_undo=true;
 const pending=ctx.undoTemplatePreview();await ctx.undoTemplatePreview();assert.equal(calls,1);
 finish({artifact_id:'b',can_undo:false,output:board()});await pending;
 assert.equal(state.templatePreview.artifact_id,'b');assert.equal(state.translationBusy,null);assert.match(state.translationFeedback,/撤回.*保存/);
 state.manualDraft=E.rotate(E.create(board()),'cw').draft;
 await ctx.undoTemplatePreview();assert.equal(calls,1);assert.equal(state.manualDraft.quarterTurns,0);
});
test('failed or late saved undo cannot replace the current board',async()=>{
 const fail=ui(async()=>{throw new Error('产物已变化');});fail.state.templatePreview.can_undo=true;
 await fail.ctx.undoTemplatePreview();assert.equal(fail.state.templatePreview.artifact_id,'a');assert.match(fail.state.translationFeedback,/撤回未保存/);
 let finish;const late=ui(()=>new Promise(resolve=>finish=resolve));late.state.templatePreview.can_undo=true;
 const pending=late.ctx.undoTemplatePreview();late.state.previewRequest++;
 finish({artifact_id:'b',can_undo:false,output:board()});await pending;assert.equal(late.state.templatePreview.artifact_id,'a');
});
