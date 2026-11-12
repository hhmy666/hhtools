const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');const vm=require('node:vm');
const file=require('node:path').join(__dirname,'../../frontend/assets/manual-edit.js');
let E=null;if(fs.existsSync(file)){const context={module:{exports:{}}};vm.runInNewContext(fs.readFileSync(file,'utf8'),context);E=context.module.exports;}
const board={tiles:[{index:0,x:4,y:4,z:0,tile_id:-10},{index:1,x:5,y:5,z:1,tile_id:-10},{index:2,x:10,y:4,z:0,tile_id:-10}],towers:[],width:17,height:19};
test('draft validation identifies every unsupported tile without changing the board',()=>{
 const d=E.create({tiles:[{index:1,x:5,y:9,z:9,tile_id:-11},{index:2,x:11,y:9,z:9,tile_id:-11},{index:3,x:4,y:4,z:0,tile_id:-10}],towers:[]});
 const before=JSON.stringify(d);
 assert.equal(typeof E.issues,'function');
 const problems=E.issues(d.board);
 assert.equal(JSON.stringify(problems.map(p=>[p.index,p.z,p.code])),JSON.stringify([[1,9,'unsupported'],[2,9,'unsupported']]));
 assert.match(problems[0].message,/第10层.*5, 9/);assert.equal(JSON.stringify(d),before);
});
test('manual edits add selected type, undo and build exact commit payload',()=>{
 assert.ok(E,'manual draft engine missing');let d=E.create(board);
 const r=E.apply(d,{action:'add',x:8,y:10,z:0,tile_id:-11});assert.equal(r.error,undefined);d=r.draft;
 assert.equal(d.board.tiles.at(-1).tile_id,-11);assert.equal(E.payload(d).additions.length,1);
 assert.equal(E.undo(d).board.tiles.length,3);
});
test('draft permits full-cover, unsafe points and floating tiles until save',()=>{
 assert.ok(E,'manual draft engine missing');const d=E.create(board);
 for(const op of [{action:'add',x:4,y:4,z:1,tile_id:-11},{action:'add',x:1,y:12,z:0,tile_id:-11}]){const r=E.apply(d,op);assert.ok(r.draft,JSON.stringify(op));assert.ok(E.saveProblem(r.draft));}
 const floating=E.apply(d,{action:'add',x:12,y:12,z:2,tile_id:-11});
 assert.ok(floating.draft);assert.match(E.saveProblem(floating.draft),/悬空/);
 assert.equal(d.board.tiles.length,3);
});
test('pointer scaling and current-layer hit use the current board not hidden layers',()=>{
 assert.ok(E,'manual draft engine missing');assert.equal(E.point(100,100,{left:0,top:0,width:360,height:400}).x,4);
 assert.equal(E.hit(board,5,5,0).index,0);assert.equal(E.hit(board,5,5,1).index,1);
});
test('visible hit selects the actual lower tile and ignores layers above the display ceiling',()=>{
 assert.equal(E.visibleHit(board,4,4,8).index,0);
 assert.equal(E.visibleHit(board,5,5,0).index,0);
 assert.equal(E.visibleHit(board,5,5,8).index,1);
});
test('automatic placement follows visible support instead of the viewing layer',()=>{
 assert.equal(E.placement(board,6,6,7).z,2);
 assert.equal(E.placement(board,6,6,0).z,0);
 assert.equal(E.placement(board,12,12,7).z,0);
 const tower={tiles:[{index:1,x:8,y:14,z:0,tower_index:0},{index:2,x:8,y:14,z:1,tower_index:0}],towers:[{index:0,x:8,y:14}]};
 assert.deepEqual(JSON.parse(JSON.stringify(E.placement(tower,9,14,7,2))),{x:8,y:14,z:2});
 assert.equal(E.placement(tower,9,13,7).z,2,'draft auto placement counts all occupied cells, deferring structure checks');
});
test('tower draft may cross the board edge but cannot save until repaired',()=>{
 assert.ok(E);const tower={...board,tiles:Array.from({length:11},(_,z)=>({index:z,x:2,y:8,z,tile_id:-10,tower_index:0})),towers:[{index:0,x:2,y:8,direction:1,count:11}]};
 const r=E.apply(E.create(tower),{action:'add',x:2,y:8,z:11,tile_id:-10});assert.ok(r.draft);assert.match(E.saveProblem(r.draft),/越过画板/);
});
test('support may be deleted in a draft, but strict save rejects floating until repaired',()=>{
 const d=E.create({width:17,height:19,towers:[],tiles:[{index:5,x:8,y:10,z:0,tile_id:-11},{index:6,x:8,y:11,z:1,tile_id:-11}]});
 const result=E.apply(d,{action:'delete',index:5});
 assert.equal(result.error,undefined);
 assert.equal(result.draft.board.tiles.length,1);
 assert.match(E.saveProblem(result.draft),/第2层.*悬空/);
 const repaired=E.apply(result.draft,{action:'add',x:8,y:10,z:0,tile_id:-11});
 assert.ok(repaired.draft);assert.doesNotMatch(E.saveProblem(repaired.draft),/悬空/);
 assert.equal(E.undo(result.draft).board.tiles.length,2);
 assert.equal(d.board.tiles.length,2);
 assert.equal(E.apply(d,{action:'delete',index:6}).draft.board.tiles.length,1);
});
test('draft translation moves every layer and pending additions atomically and undo restores edits',()=>{
 let d=E.apply(E.create(board),{action:'add',x:8,y:10,z:0,tile_id:-11}).draft;
 const before=JSON.stringify(d);
 const moved=E.translate(d,0,1).draft;
 assert.ok(moved);assert.equal(moved.board.tiles[0].y,5);
 assert.equal(moved.board.tiles[1].y,6);assert.equal(moved.additions[0].y,11);
 assert.deepEqual(JSON.parse(JSON.stringify(E.payload(moved).translation)),{dx:0,dy:1});
 assert.equal(E.payload(moved).additions[0].y,11);
 assert.equal(JSON.stringify(E.undo(moved)),before);
 assert.equal(JSON.stringify(d),before);
});
test('translation preserves inherited edge positions without disabling the free Y direction',()=>{
 const edge={width:17,height:19,towers:[],tiles:[{index:0,x:1,y:4,z:0,tile_id:-10},{index:1,x:15,y:4,z:0,tile_id:-10}]};
 const bounds=E.translationBounds(edge);
 assert.equal(bounds.min_dx,0);assert.equal(bounds.max_dx,0);assert.ok(bounds.max_dy>0);
 assert.ok(E.translate(E.create(edge),0,1).draft);
 const outside=E.translate(E.create(edge),1,0);assert.ok(outside.draft);assert.match(E.saveProblem(outside.draft),/坐标范围/);
});
test('draft can restore a legacy edge after moving inward and reset is not a new baseline',()=>{
 const d=E.create({width:17,height:19,towers:[],tiles:[{index:0,x:1,y:4,z:0,tile_id:-10}]});
 const moved=E.translate(d,1,0).draft;
 assert.equal(E.translate(moved,-1,0).draft.board.tiles[0].x,1);
 assert.equal(E.translate(d,0,0).draft,d);
});
