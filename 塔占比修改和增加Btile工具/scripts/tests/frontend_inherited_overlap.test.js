const test=require('node:test'),assert=require('node:assert/strict');
const E=require('../../frontend/assets/manual-edit.js');
const P=require('../../frontend/assets/template-preview.js');
const VM=require('../../frontend/assets/view-model.js');
function board(){return {template_id:'inherited',width:17,height:19,towers:[],tiles:[
 {index:0,x:4,y:4,z:0,tile_id:-10},{index:1,x:4,y:4,z:1,tile_id:-10},{index:2,x:10,y:10,z:0,tile_id:-10},
],geometry_warnings:[{code:'inherited_full_cover',x:4,y:4,lower_z:0,upper_z:1,lower_index:0,upper_index:1}]};}
test('trusted inherited full-cover stays nonblocking, both tiles marked with exact layers',()=>{
 const d=E.create(board());assert.equal(E.saveProblem(d),'');
 const issues=E.issues(d.board,d.translationOrigin);assert.equal(issues.length,2);
 assert.ok(issues.every(i=>i.blocking===false&&i.message.includes('第1、2层(4, 4)')));
 const html=P.boardMarkup(d.board);assert.equal((html.match(/is-invalid/g)||[]).length,2);
 assert.match(html,/原模板一压一：第1、2层/);
 assert.match(html,/editor-tile-label">A<\/span>/);assert.doesNotMatch(html,/>[AB]\?<\/span>/);
});
test('translation retains pair identity and current warning coordinates; new pairs never inherit',()=>{
 const original=E.create(board()),moved=E.translate(original,1,1).draft;
 assert.equal(E.saveProblem(moved),'');
 assert.match(E.issues(moved.board,moved.translationOrigin)[0].message,/\(5, 5\)/);
 const addition=E.apply(moved,{action:'add',x:11,y:11,z:1,tile_id:-10});
 assert.ok(addition.draft);assert.match(E.saveProblem(addition.draft),/一压一/);
 const forged=E.create(board());forged.board.tiles[2].inherited_full_cover=true;
 forged.board.tiles.push({index:-1,x:10,y:10,z:1,tile_id:-10,inherited_full_cover:true});
 assert.match(E.saveProblem(forged),/一压一/);
});
test('deleting original upper member clears warning; replacement cannot inherit its identity',()=>{
 const d=E.apply(E.create(board()),{action:'delete',index:1}).draft;
 assert.equal(E.issues(d.board,d.translationOrigin).length,0);
 assert.doesNotMatch(P.boardMarkup(d.board),/is-invalid/);
 const replacement=E.apply(d,{action:'add',x:4,y:4,z:1,tile_id:-10});assert.ok(replacement.draft);assert.match(E.saveProblem(replacement.draft),/一压一/);
});
test('real structural errors retain precedence over nonblocking inherited warnings',()=>{
 const d=E.create(board());d.board.tiles.push({index:-1,x:12,y:12,z:4,tile_id:-11});
 assert.match(E.saveProblem(d),/没有承接/);
 assert.ok(E.issues(d.board,d.translationOrigin).some(i=>i.code==='unsupported'));
});
test('result normalization preserves original and output warning arrays independently',()=>{
 const source=board().geometry_warnings,output=[];
 const row=VM.normalizeTemplate({source_geometry_warnings:source,output_geometry_warnings:output},'one',0);
 assert.deepEqual(row.sourceGeometryWarnings,source);assert.deepEqual(row.outputGeometryWarnings,[]);
});
test('verified removal-only recovery is a passed deliverable, never a zero-movement no-op',()=>{
 const row=VM.normalizeTemplate({status:'verified',plan_summary:{manual_recovery:true,move_count:0,source_tower_tile_count:3,source_tile_counts:{A:9,B:0},result_tile_counts:{A:6,B:0}},artifacts:[{id:'cleared'}]},'clear',0);
 assert.deepEqual(VM.summarizeTemplateOutcomes([row]),{passed:1,failed:0,noOp:0});
 assert.deepEqual(VM.summarizeTemplateOutcomes([{...row,status:'failed',artifacts:[]}]),{passed:0,failed:1,noOp:0});
 assert.deepEqual(VM.summarizeTemplateOutcomes([{...row,status:'no_op',artifacts:[]}]),{passed:0,failed:0,noOp:1});
});
test('nonadjacent layers may reuse original XY columns when final support and counts are valid',()=>{
 let d=E.create(board());
 for(const [x,y,z] of [[5,5,2],[4,4,3],[12,12,0]])d=E.apply(d,{action:'add',x,y,z,tile_id:-10}).draft;
 assert.equal(E.saveProblem(d),'');
 assert.equal(E.issues(d.board,d.translationOrigin).some(i=>i.code==='inherited_column'),false);
 const moved=E.translate(d,1,1).draft;assert.equal(E.saveProblem(moved),'');
});
