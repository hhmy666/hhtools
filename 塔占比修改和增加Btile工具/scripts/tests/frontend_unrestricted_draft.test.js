const test=require('node:test'),assert=require('node:assert/strict');
const E=require('../../frontend/assets/manual-edit.js');
const board=()=>({width:17,height:19,towers:[],tiles:[{index:0,x:4,y:4,z:0,tile_id:-10},{index:1,x:6,y:4,z:0,tile_id:-10},{index:2,x:8,y:4,z:0,tile_id:-10}]});
test('draft accepts overlap, adjacent full-cover, floating and outside-safe-zone additions; save validates',()=>{
 for(const p of [{x:4,y:4,z:0},{x:4,y:4,z:1},{x:12,y:12,z:6},{x:0,y:0,z:0}]){
  const result=E.apply(E.create(board()),{action:'add',...p,tile_id:-11});
  assert.ok(result.draft,result.error);assert.equal(result.draft.board.tiles.length,4);
  assert.ok(E.saveProblem(result.draft));assert.equal(E.undo(result.draft).board.tiles.length,3);
 }
});
test('draft deletion accepts a middle tower tile and special tile, with final checks deferred',()=>{
 const b=board();b.towers=[{index:0,x:4,y:4,direction:0}];b.tiles=[0,1,2].map(z=>({index:z,x:4,y:4,z,tile_id:z===1?99:-10,tower_index:0}));
 const r=E.apply(E.create(b),{action:'delete',index:1});assert.ok(r.draft,r.error);
 assert.ok(E.saveProblem(r.draft));assert.equal(E.undo(r.draft).board.tiles.length,3);
});
test('draft translations may leave bounds and undo safely; bounds checked on save',()=>{
 const r=E.translate(E.create(board()),-10,-10);assert.ok(r.draft,r.error);
 assert.ok(E.saveProblem(r.draft));assert.equal(E.undo(r.draft).board.tiles[0].x,4);
});
test('one-on-one means adjacent Z only, never an entire reserved XY column',()=>{
 const b=board();b.tiles=[{index:0,x:4,y:4,z:0,tile_id:-10},{index:1,x:4,y:4,z:1,tile_id:-10},{index:2,x:5,y:5,z:2,tile_id:-10},{index:3,x:4,y:4,z:3,tile_id:-10},{index:4,x:10,y:10,z:0,tile_id:-10},{index:5,x:12,y:10,z:0,tile_id:-10}];
 b.geometry_warnings=[{code:'inherited_full_cover',x:4,y:4,lower_z:0,upper_z:1,lower_index:0,upper_index:1}];
 const d=E.create(b);assert.equal(E.saveProblem(d),'');
 assert.equal(E.issues(d.board,d.translationOrigin).filter(i=>i.blocking!==false).length,0);
});
test('unlimited auto placement matches native editor and never selects a lower gap',()=>{
 const b=board();b.tiles.push({index:3,x:5,y:5,z:2,tile_id:-11});
 const p=E.placement(b,5,4,3,0);assert.equal(p.z,3);
});
