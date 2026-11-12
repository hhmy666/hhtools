const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../../frontend/assets/app.js'),'utf8');
const engine=require('../../frontend/assets/manual-edit.js');
test('brushes use plain A/B letters without oversized tile-face question marks',()=>{
  assert.match(source,/class="manual-brush-face" aria-hidden="true">A<\/span>/);
  assert.match(source,/class="manual-brush-face" aria-hidden="true">B<\/span>/);
  assert.doesNotMatch(source,/class="manual-brush-face"[^>]*>[AB]\?/);
});
function setup(A,B){
  const nodes=new Map(),node=id=>{if(!nodes.has(id))nodes.set(id,{value:'',textContent:'',hidden:false,disabled:false,checked:false,classList:{toggle(){}},attributes:{},setAttribute(k,v){this.attributes[k]=v;}});return nodes.get(id);};
  node('manual-tile-type').value='-10';node('preview-layer').value='1';
  const tiles=[...Array.from({length:A},(_,index)=>({index,tile_id:-10})),...Array.from({length:B},(_,i)=>({index:A+i,tile_id:-11}))];
  const state={manualDraft:engine.create({tiles,towers:[]}),manualMessage:''};
  const context={state,byId:node,all:()=>[],translationPending:()=>false,updateManualCursor(){},window:{TowerManualEdit:engine}};
  for(const name of ['renderManualControls','selectManualBrush']){
    const start=source.indexOf(`function ${name}(`);
    if(start<0)continue;
    const end=source.indexOf('\nfunction ',start+1);
    vm.runInNewContext(source.slice(start,end<0?source.length:end),context);
  }
  return {context,state,node};
}
test('brush totals show the missing additions to the next triple, including zero',()=>{
  for(const [A,B,wantA,wantB] of [[107,0,'107 (1)','0 (0)'],[1,2,'1 (2)','2 (1)'],[108,6,'108 (0)','6 (0)']]){
    const {context,node}=setup(A,B);context.renderManualControls();
    assert.equal(node('manual-brush-a-count').textContent,wantA);
    assert.equal(node('manual-brush-b-count').textContent,wantB);
  }
});
test('the right toolbar action labels clean, dirty and inactive drafts distinctly',()=>{
 const {context,state,node}=setup(3,0);
 context.renderManualControls();assert.equal(node('preview-all-layers').textContent,'退出编辑');
 state.manualDraft.history.push({});context.renderManualControls();
 assert.equal(node('preview-all-layers').textContent,'保存编辑');
 state.manualDraft=null;context.renderManualControls();assert.equal(node('preview-all-layers').textContent,'去编辑');
});
test('removal-only recovery offers save even without brush history',()=>{
 const {context,state,node}=setup(3,0);state.manualDraft.recovery=true;
 context.renderManualControls();assert.equal(node('preview-all-layers').textContent,'保存编辑');
 assert.match(node('manual-counts').textContent,/未保存/);
});
test('unlimited pointer actions edit actual tiles and auto-place additions without changing the mode',()=>{
 const tiles=[{index:0,x:4,y:4,z:0,tile_id:-10},{index:1,x:5,y:5,z:1,tile_id:-10}];
 const state={manualDraft:engine.create({tiles,towers:[]})};
 const nodes={'preview-layer':{value:'0'},'manual-tile-type':{value:'-11'}};
 const ctx={state,byId:id=>nodes[id],translationPending:()=>false,window:{TowerManualEdit:engine},renderTemplatePreview(){}};
 const start=source.indexOf('function manualAction('),end=source.indexOf('\nasync function saveManualDraft',start);
 vm.runInNewContext(source.slice(start,end),ctx);
 ctx.manualAction('delete',4,4,0);
 assert.equal(state.manualDraft.board.tiles.some(t=>t.index===0),false);
 state.manualDraft=engine.undo(state.manualDraft);
 ctx.manualAction('add',6,6);
 assert.equal(state.manualDraft.additions[0].z,2);
 assert.equal(nodes['preview-layer'].value,'0');
 state.manualDraft=engine.undo(state.manualDraft);
 ctx.manualAction('add',12,12);
 assert.equal(state.manualDraft.additions[0].z,0);
 state.manualDraft.validationRequested=true;state.manualError=true;
 ctx.manualAction('add',14,14);
 assert.equal(state.manualDraft.validationRequested,false);assert.equal(state.manualError,false);
});
test('editing shows original and current totals and exposes save problems as an alert',()=>{
 const {context,state,node}=setup(1,0),classes=new Set();
 state.templatePreview={source:{tiles:Array(123)}};
 node('manual-status').classList.toggle=(name,on)=>on?classes.add(name):classes.delete(name);
 state.manualDraft.validationRequested=true;
 context.renderManualControls();
 assert.match(node('manual-counts').textContent,/123 \/ 1/);
 assert.equal(node('manual-status').attributes.role,'alert');assert.equal(classes.has('is-error'),true);
 state.manualDraft.board.tiles.push({index:1,tile_id:-10},{index:2,tile_id:-10});
 context.renderManualControls();assert.match(node('manual-counts').textContent,/123 \/ 3/);
 assert.equal(node('manual-status').attributes.role,'status');assert.equal(classes.has('is-error'),false);
});
test('choosing B activates its brush, exits erase and refreshes counts after draft changes',()=>{
  const {context,state,node}=setup(3,0);node('manual-erase').checked=true;
  assert.equal(typeof context.selectManualBrush,'function');
  context.selectManualBrush(-11);
  assert.equal(node('manual-tile-type').value,'-11');assert.equal(node('manual-erase').checked,false);
  assert.equal(node('manual-brush-b').attributes['aria-pressed'],'true');
  assert.equal(node('manual-brush-a').attributes['aria-pressed'],'false');
  state.manualDraft.board.tiles.push({index:3,tile_id:-11});context.renderManualControls();
  assert.equal(node('manual-brush-b-count').textContent,'1 (2)');
  state.manualDraft.board.tiles.pop();context.renderManualControls();
  assert.equal(node('manual-brush-b-count').textContent,'0 (0)');
});
test('inline feedback prioritizes the correction and retains the full accessible explanation',()=>{
 const {context,state,node}=setup(1,0);
 state.manualMessage='草稿已更新，尚未保存。';
 context.renderManualControls();
 assert.equal(node('manual-status').textContent,'草稿已更新，尚未保存。');
 state.manualDraft.validationRequested=true;context.renderManualControls();
 assert.equal(node('manual-status').textContent,'数量未成组，请增删或撤销');
 assert.match(node('manual-status').attributes['aria-label'],/总牌量及A\/B各自需为3的倍数/);
 assert.match(node('manual-status').attributes.title,/总牌量及A\/B各自需为3的倍数/);
 state.manualDraft.board.tiles.push({index:1,tile_id:-10},{index:2,tile_id:-10});
 context.renderManualControls();
 assert.equal(node('manual-status').textContent,'草稿已更新，尚未保存。');
 assert.equal(node('manual-status').attributes.title,'草稿已更新，尚未保存。');
});
test('draft validation is not invoked by routine editing controls before a save request',()=>{
 const {context,state,node}=setup(1,0);let calls=0;
 context.window.TowerManualEdit={...engine,saveProblem(){calls++;return '不能保存';}};
 context.renderManualControls();assert.equal(calls,0);assert.doesNotMatch(node('manual-status').textContent,/不能保存/);
 state.manualDraft.validationRequested=true;context.renderManualControls();assert.equal(calls,1);assert.equal(node('manual-status').textContent,'不能保存');
});
