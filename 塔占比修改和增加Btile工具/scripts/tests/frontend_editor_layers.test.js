const test=require('node:test'),assert=require('node:assert/strict');
const E=require('../../frontend/assets/manual-edit.js');
const P=require('../../frontend/assets/template-preview.js');
const fs=require('node:fs'),vm=require('node:vm');
const app=fs.readFileSync(require('node:path').join(__dirname,'../../frontend/assets/app.js'),'utf8');
const board={towers:[],tiles:[{index:0,x:4,y:4,z:0,tile_id:-10},{index:1,x:5,y:5,z:3,tile_id:-11}]};
test('unlimited addition stacks above the highest overlapping footprint, not a lower gap',()=>{
 assert.equal(E.placement(board,5,4,Infinity,0).z,4);
 assert.equal(E.placement(board,12,12,Infinity).z,0);
});
test('fixed editor layer targets only that layer, including floating additions',()=>{
 assert.deepEqual(E.target(board,'add',12,12,3),{x:12,y:12,z:2});
 assert.equal(E.target(board,'delete',4,4,3,0),undefined);
 assert.equal(E.target(board,'delete',5,5,4,1).index,1);
 assert.ok(E.apply(E.create(board),{action:'add',...E.target(board,'add',12,12,3),tile_id:-10}).draft);
});
test('unlimited deletion uses the actual clicked tile or the highest tile for coordinates',()=>{
 assert.equal(E.target(board,'delete',5,5,0,0).index,0);
 assert.equal(E.target(board,'delete',5,5,0).index,1);
});
test('single-layer viewing remains single-layer while editing; unlimited includes every layer',()=>{
 assert.deepEqual(P.boardView(board,{mode:'single',layer:4,editLayer:true}).tiles.map(t=>t.index),[1]);
 assert.equal(P.boardView(board,{mode:'all',layer:0,editLayer:true}).tiles.length,2);
});
test('cursor footprint uses the same tile projection and 2 by 2 grid size',()=>{
 const b={towers:[{index:0,x:8,y:14,direction:0}],tiles:[]};
 const p=P.footprint(b,{x:8,y:14,z:2,tower_index:0});
 assert.equal(p.left,50);assert.equal(p.top,(15-32/85)/20*100);
 assert.equal(p.width,100/9);assert.equal(p.height,10);
 assert.equal(E.point(180,200,{left:0,top:0,width:360,height:400}).x,8);
});
function cursorContext(){
 let cursor=null,inside=true,busy=false;
 const nodes={'preview-layer':{value:'3'},'manual-erase':{checked:false},'manual-tile-type':{value:'-11'}};
 const surface={getBoundingClientRect:()=>({left:0,top:0,width:360,height:400}),append(element){cursor=element;}};
 const host={querySelector:()=>cursor,contains:element=>element===surface};nodes['preview-output-board']=host;
 const under={closest:selector=>selector==='.editor-board'?surface:null};
 const ctx={state:{manualDraft:E.create(board)},byId:id=>nodes[id],translationPending:()=>busy,renderTemplatePreview(){},window:{TowerManualEdit:E,TowerTemplatePreview:P},document:{elementFromPoint:()=>inside?under:null,createElement:()=>({style:{},dataset:{},setAttribute(){},remove(){cursor=null;}})}};
 for(const name of ['updateManualCursor','manualAction']){
  const start=app.indexOf(`function ${name}(`),end=app.slice(start+1).search(/\n(?:async )?function /);
  vm.runInNewContext(app.slice(start,end<0?app.length:start+1+end),ctx);
 }
 return {ctx,nodes,getCursor:()=>cursor,outside(){inside=false;},pending(){busy=true;}};
}
test('cursor and actual click share fixed/unlimited XYZ; pointer preview does not mutate draft',()=>{
 const {ctx,nodes,getCursor}=cursorContext();
 ctx.updateManualCursor({clientX:100,clientY:100,buttons:0});
 assert.equal(getCursor().dataset.x,'4');assert.equal(getCursor().dataset.y,'4');assert.equal(getCursor().dataset.z,'2');
 assert.equal(getCursor().dataset.label,'B · 第 3 层');
 assert.equal(getCursor().textContent,'','cursor caption must not overlap the real tile face');
 assert.equal(ctx.state.manualDraft.board.tiles.length,2);
 ctx.manualAction('add',4,4);
 assert.equal(ctx.state.manualDraft.additions[0].z,2);
 nodes['preview-layer'].value='0';ctx.updateManualCursor();
 assert.equal(getCursor().dataset.z,'4');
 assert.equal(parseFloat(getCursor().style.width),100/9);
});
test('cursor hides outside the board, while saving and after editing exits; erase only marks its target',()=>{
 const first=cursorContext();first.ctx.updateManualCursor({clientX:100,clientY:100,buttons:0});
 first.outside();first.ctx.updateManualCursor();assert.equal(first.getCursor(),null);
 const second=cursorContext();second.ctx.updateManualCursor({clientX:100,clientY:100,buttons:0});
 second.pending();second.ctx.updateManualCursor();assert.equal(second.getCursor(),null);
 const third=cursorContext();third.nodes['preview-layer'].value='1';third.nodes['manual-erase'].checked=true;
 third.ctx.updateManualCursor({clientX:100,clientY:100,buttons:0});
 assert.equal(third.getCursor().className,'manual-tile-cursor is-delete');
 assert.equal(third.ctx.state.manualDraft.board.tiles.length,2);
 third.ctx.state.manualDraft=null;third.ctx.updateManualCursor();assert.equal(third.getCursor(),null);
});
