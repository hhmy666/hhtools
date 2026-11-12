const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync(require('node:path').join(__dirname,'../../frontend/assets/app.js'),'utf8');
function load(context,name){const start=source.search(new RegExp(`(?:async )?function ${name}\\(`)),next=source.slice(start+1).search(/\n(?:async )?function /);vm.runInNewContext(source.slice(start,next<0?source.length:start+1+next),context);}
function setup(problem=''){
 const nodes=new Map(),node=id=>{if(!nodes.has(id))nodes.set(id,{id,open:false,dataset:{},focus(){},querySelector(){return null;},showModal(){this.open=true;},close(){this.open=false;}});return nodes.get(id);};
 const state={manualDraft:{history:[{}]},lastFocused:{focus(){}}};
 const context={state,byId:node,window:{TowerManualEdit:{saveProblem:()=>problem}},translationPending:()=>false,document:{activeElement:node('close-icon'),contains:()=>true}};
 load(context,'openDialog');load(context,'closeDialog');node('template-preview-dialog').open=true;
 return {context,state,node};
}
test('close icon opens a save/discard choice without discarding the draft or closing comparison',()=>{
 const {context,state,node}=setup();context.closeDialog(node('template-preview-dialog'));
 assert.equal(node('manual-close-dialog').open,true);assert.equal(node('template-preview-dialog').open,true);
 assert.equal(state.manualDraft.history.length,1);assert.equal(node('manual-close-save').disabled,false);
 context.closeDialog(node('manual-close-dialog'));assert.equal(node('app-shell').inert,true);assert.equal(node('template-preview-dialog').open,true);
});
test('closing never prevalidates or disables save; validation occurs after the save action',()=>{
 const {context,node}=setup('A/B各须为3的倍数');context.closeDialog(node('template-preview-dialog'));
 assert.equal(node('manual-close-save').disabled,false);assert.doesNotMatch(node('manual-close-feedback').textContent,/3的倍数/);
});
test('pending save cannot open a misleading discard confirmation or close the preview',()=>{
 const {context,state,node}=setup();context.translationPending=()=>true;
 context.closeDialog(node('template-preview-dialog'));
 assert.equal(node('manual-close-dialog').open,false);assert.equal(node('template-preview-dialog').open,true);
 assert.equal(state.manualDraft.history.length,1);
});
test('clean exit returns focus to the visible editing entry',()=>{
 const {context,state,node}=setup();state.manualDraft.history=[];
 let focused=false,renders=0;node('preview-all-layers').focus=()=>{focused=true;};
 context.renderTemplatePreview=()=>{renders++;};load(context,'exitPreviewEditing');context.exitPreviewEditing();
 assert.equal(state.manualDraft,null);assert.equal(focused,true);assert.equal(renders,1);
});
test('tower removal without added tiles remains a pending change protected on close',()=>{
 const {context,state,node}=setup();context.all=()=>[];state.manualDraft.history=[];state.manualDraft.recovery=true;
 context.closeDialog(node('template-preview-dialog'));
 assert.equal(node('manual-close-dialog').open,true);assert.equal(node('template-preview-dialog').open,true);
});
