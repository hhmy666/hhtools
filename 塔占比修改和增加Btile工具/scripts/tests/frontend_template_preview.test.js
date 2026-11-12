"use strict";
const test=require('node:test');
const assert=require('node:assert/strict');
let preview;
try {preview=require('../../frontend/assets/template-preview.js');} catch {preview={};}
const tile=(index,x,y,z,other={})=>({index,x,y,z,tile_id:-10,tower_index:null,changed:false,...other});
test('thumbnail uses real shared projections and does not shrink unreadable tile text',()=>{
 const markup=preview.thumbnailMarkup({template_id:'one',tiles:[tile(0,4,6,0),tile(1,5,7,1,{tile_id:-11})],towers:[]});
 assert.match(markup,/viewBox="0 0 18 20"/);assert.match(markup,/x="4" y="6"/);
 assert.match(markup,/thumbnail-tile is-covered/);assert.match(markup,/is-b/);
 assert.doesNotMatch(markup,/<text|A\?|B\?/);
 assert.match(preview.thumbnailMarkup(null),/未生成/);
});
test('invalid draft tile receives an explicit problem label and outline class even as lower context',()=>{
 const board={tiles:[tile(1,5,9,9),tile(2,11,9,9)],towers:[]};
 const markup=preview.boardMarkup(board,{layer:11,editLayer:true,issues:[{index:1,message:'缺少下层承接'}]});
 assert.match(markup,/is-invalid[^>]*data-tile-index="1"/);
 assert.doesNotMatch(markup,/is-invalid[^>]*data-tile-index="2"/);
 assert.match(markup,/aria-label="[^"]*问题：缺少下层承接/);
});

test('editing highlights exposed lower tiles while keeping change markers scoped to the selected layer',()=>{
  const board={tiles:[tile(0,4,4,0,{changed:true,added:true}),tile(1,10,10,1),tile(2,12,12,2)],towers:[]};
  const options={mode:'through',layer:2,editLayer:true,markChanges:true};
  const view=preview.boardView(board,options);
  assert.deepEqual(view.tiles.map(t=>[t.index,t.highlighted]),[[0,true],[1,true]]);
  const markup=preview.boardMarkup(board,options);
  assert.match(markup,/is-context[^>]*data-tile-index="0"/);
  assert.doesNotMatch(markup,/is-moved[^>]*data-tile-index="0"/);
  assert.doesNotMatch(markup,/is-added[^>]*data-tile-index="0"/);
});

test('editing exposure includes ordinary and tower layer six below layer eight, ignoring hidden higher layers',()=>{
  const board={tiles:[tile(0,2,4,5),tile(1,6,14,5,{tower_index:0}),tile(2,10,6,5),tile(3,11,7,7),tile(4,2,4,8)],towers:[{index:0,direction:0}]};
  const view=preview.boardView(board,{mode:'through',layer:8,editLayer:true});
  assert.deepEqual(view.tiles.map(t=>[t.index,t.highlighted]),[[0,true],[2,false],[1,true],[3,true]]);
  const markup=preview.boardMarkup(board,{mode:'through',layer:8,editLayer:true});
  assert.match(markup,/is-highlighted is-context[^>]*data-tile-index="0"/);
  assert.match(markup,/is-highlighted is-context is-tower[^>]*data-tile-index="1"/);
  assert.match(markup,/下层参考 · 完全无遮盖/);
});

test('a tile with even one quarter overlapped is not highlighted; a disjoint higher tile does not cover it',()=>{
  const board={tiles:[tile(0,4,4,0),tile(1,5,5,1),tile(2,10,10,2)],towers:[]};
  const view=preview.boardView ? preview.boardView(board,{mode:'all'}) : {tiles:[]};
  assert.deepEqual(view.tiles.map(t=>[t.index,t.highlighted]),[[0,false],[1,true],[2,true]]);
});
test('single-layer and cumulative modes use the selected layer, preserve moved markers and never mutate input',()=>{
  const board={tiles:[tile(0,4,4,0),tile(1,5,5,1,{changed:true}),tile(2,6,6,2)],towers:[]};
  const before=JSON.stringify(board);
  const single=preview.boardView ? preview.boardView(board,{mode:'single',layer:2}) : {tiles:[]};
  assert.deepEqual(single.tiles.map(t=>[t.index,t.highlighted,t.changed]),[[1,true,true]]);
  const cumulative=preview.boardView(board,{mode:'through',layer:2});
  assert.deepEqual(cumulative.tiles.map(t=>[t.index,t.highlighted]),[[0,false],[1,true]]);
  assert.equal(JSON.stringify(board),before);
});
test('editor grid has 17 by 19 points, same-parity coordinates red, mixed parity blue',()=>{
  const points=preview.editorGrid ? preview.editorGrid() : [];
  assert.equal(points.length,323);
  for(const [x,y,color] of [[0,0,'red'],[1,1,'red'],[0,1,'blue'],[1,0,'blue'],[16,18,'red']]){
    assert.equal(points.find(p=>p.x===x && p.y===y).color,color);
  }
});
test('tower projection preserves the editor shift and all comparison views use the same fixed frame',()=>{
  const board={tiles:[tile(0,4,14,0,{tower_index:0}),tile(1,4,14,1,{tower_index:0})],
    towers:[{index:0,x:4,y:14,direction:0}]};
  const view=preview.boardView ? preview.boardView(board,{mode:'all'}) : {tiles:[]};
  assert.equal(view.tiles.length,2);
  assert.equal(view.tiles[1].projectedY,14-16/85);
  assert.equal(view.tiles[0].highlighted,false);
  assert.equal(view.tiles[1].highlighted,true);
});
test('tile tooltip and accessible label expose actual adjacent-layer support independently of view filter',()=>{
  const board={template_id:'actual',towers:[],tiles:[tile(57,4,5,3),tile(100,4,6,4,{support_indices:[57],support_areas:[2]})]};
  const markup=preview.boardMarkup(board,{mode:'single',layer:5});
  assert.match(markup,/title="[^"]*下层承接：Tile 57（1\/2压盖）/);
  assert.match(markup,/aria-label="[^"]*下层承接：Tile 57（1\/2压盖）/);
  assert.doesNotMatch(markup,/悬空/);
});
test('support labels distinguish bottom, missing evidence, unsupported tiles and complete coverage',()=>{
  const board={towers:[],tiles:[tile(0,4,4,0),tile(1,6,6,1),tile(2,8,8,1,{support_indices:[],support_areas:[]}),tile(3,4,4,1,{support_indices:[0],support_areas:[4]})]};
  const markup=preview.boardMarkup(board);
  assert.match(markup,/位于底层/);assert.match(markup,/悬空：紧邻下层无承接/);assert.match(markup,/Tile 0（完全压盖）/);
  assert.equal((markup.match(/悬空：/g)||[]).length,2);
  assert.doesNotMatch(markup, /Tile 1[^\"]*悬空/);
});
