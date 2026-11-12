const test = require('node:test');
const assert = require('node:assert/strict');
const E = require('../frontend/assets/editor.js');
const fs = require('node:fs');

const tile = (index,x,y,z,tile_id=-10) => ({index,x,y,z,tile_id,tower_index:null});

test('snapped anchor matches the original 18 by 20 editor coordinate frame at edges', () => {
  const rect = {left:40,top:20,width:360,height:400};
  assert.deepEqual(E.point(100,100,rect), {x:2,y:3});
  assert.deepEqual(E.point(40,20,rect), {x:-1,y:-1});
  assert.deepEqual(E.point(400,420,rect), {x:17,y:19});
  assert.ok(Math.abs(E.anchor({x:2,y:3}).left-100/6)<1e-10);
  assert.equal(E.anchor({x:2,y:3}).top,20);
});

test('unlimited placement uses highest positive-area overlap in the full tile footprint', () => {
  const board = {tiles:[tile(0,2,2,0),tile(1,3,3,4),tile(2,4,2,9)]};
  assert.deepEqual(E.placement(board,2,2), {x:2,y:2,z:5});
  assert.deepEqual(E.placement(board,9,9), {x:9,y:9,z:0});
  assert.deepEqual(E.target(board,'add',2,2,3), {x:2,y:2,z:2});
});

test('deletion selects the actual clicked tile and fixed layers cannot delete another layer', () => {
  const board = {tiles:[tile(0,2,2,0),tile(1,2,2,2),tile(2,3,3,1)]};
  assert.equal(E.target(board,'delete',3,3,0,2).index,2);
  assert.equal(E.target(board,'delete',2,2,0).index,1);
  assert.equal(E.target(board,'delete',2,2,1,1).index,0);
  assert.equal(E.target(board,'delete',8,8,1,1),undefined);
});

test('draft editing allows overlapping and unsupported tiles, then undo restores exactly', () => {
  const original = {tiles:[tile(0,2,2,0)],towers:[]};
  const start = E.create(original);
  const added = E.apply(start,{action:'add',x:2,y:2,z:0,tile_id:-11});
  assert.equal(added.tiles.length,2);
  assert.equal(added.tiles[1].tile_id,-11);
  assert.ok(added.tiles[1].index<0);
  const floating = E.apply(added,{action:'add',x:9,y:9,z:6,tile_id:-10});
  assert.equal(floating.tiles.at(-1).z,6);
  const deleted = E.apply(floating,{action:'delete',index:0});
  assert.equal(deleted.tiles.length,2);
  assert.deepEqual(E.undo(deleted).tiles,floating.tiles);
  assert.deepEqual(E.undo(E.undo(E.undo(deleted))).tiles,original.tiles);
  assert.deepEqual(original.tiles,[tile(0,2,2,0)]);
});

test('layer view stays synchronized and only-selected ignores all other layers', () => {
  const board = {tiles:[tile(0,2,2,0),tile(1,2,2,1),tile(2,3,3,2)]};
  assert.deepEqual(E.visibleTiles(board,0,false).map(t=>t.index),[0,1,2]);
  assert.deepEqual(E.visibleTiles(board,2,false).map(t=>t.index),[0,1]);
  assert.deepEqual(E.visibleTiles(board,2,true).map(t=>t.index),[1]);
});

test('both issue members are red and issue positioning alternates lower and upper layer', () => {
  const board = {tiles:[tile(0,2,2,0),tile(1,2,2,2)],issues:[{kind:'floating',x:2,y:2,lower_layer:1,upper_layer:3,lower_index:0,upper_index:1}]};
  assert.deepEqual([...E.issueIndices(board)],[0,1]);
  assert.equal(E.issueLayer(board.issues[0],0),1);
  assert.equal(E.issueLayer(board.issues[0],1),3);
  assert.equal(E.issueLayer(board.issues[0],2),1);
});

test('tower projection and issue rescanning share the detector geometry', () => {
  const board = {towers:[{index:0,x:4,y:4,direction:3}],tiles:[tile(0,2,2,0),tile(1,2,2,2),{...tile(2,4,4,1),tower_index:0}]};
  assert.deepEqual(E.projection(board,board.tiles[2]),{x:4+16/85,y:4});
  assert.equal(E.scanIssues(board).length,1);
  board.tiles.push(tile(3,3,3,1));
  assert.equal(E.scanIssues(board).length,0);
});

test('live full-cover rescanning includes every duplicate tile across consecutive distinct Z levels',()=>{
  const board={tiles:[tile(0,5,5,0),tile(1,5,5,0),tile(2,5,5,1),tile(3,5,5,1),tile(4,5,5,3)],towers:[]};
  assert.deepEqual(E.scanIssues(board).map(i=>[i.kind,i.lower_index,i.upper_index]),[
    ['adjacent',0,2],['adjacent',0,3],['adjacent',1,2],['adjacent',1,3],
    ['floating',2,4],['floating',3,4]
  ]);
  assert.deepEqual([...E.issueIndices(board)].sort((a,b)=>a-b),[0,1,2,3,4]);
  board.tiles.push(tile(5,6,6,2));
  assert.deepEqual(E.scanIssues(board).map(i=>[i.lower_index,i.upper_index]),[[0,2],[0,3],[1,2],[1,3]]);
});

test('draft tiles added at an existing tower anchor inherit the tower projection', () => {
  const board = {towers:[{index:0,x:4,y:4,direction:3}],tiles:[]};
  const added = {...tile(-1,4,4,3),added:true};
  assert.deepEqual(E.projection(board,added),{x:4+3*16/85,y:4});
});

test('unlimited placement checks the actual projected footprint of existing tower tiles', () => {
  const board = {towers:[{index:0,x:4,y:4,direction:3}],tiles:[{...tile(0,4,4,10),tower_index:0}]};
  assert.deepEqual(E.placement(board,7,4),{x:7,y:4,z:11});
  assert.deepEqual(E.placement(board,4,1),{x:4,y:1,z:0});
});

test('placement rechecks the added tower tile footprint after its layer changes', () => {
  const board = {towers:[{index:0,x:4,y:4,direction:3}],tiles:[tile(0,4,4,0),{...tile(1,4,4,10),tower_index:0},tile(2,8,4,15)]};
  assert.deepEqual(E.placement(board,4,4),{x:4,y:4,z:16});
});

test('hit testing uses the rendered tower footprint and ignores its empty unprojected anchor', () => {
  const board = {towers:[{index:0,x:4,y:4,direction:3}],tiles:[{...tile(0,4,4,10),tower_index:0}]};
  assert.equal(E.hit(board,4.5,4.5),undefined);
  assert.equal(E.hit(board,6,4).index,0);
});

test('continuous pointer coordinates and hit bounds use the same centered anchor frame', () => {
  const board={tiles:[tile(0,2,2,0)],towers:[]},rect={left:40,top:20,width:360,height:400};
  assert.deepEqual(E.boardPoint(90,80,rect),{x:1.5,y:2});
  assert.equal(E.hit(board,1,1).index,0);
  assert.equal(E.hit(board,0.999,2),undefined);
  assert.equal(E.hit(board,3,2),undefined);
});

class EditorElement {
  constructor(){this.attributes=new Map();this.nodes=new Map();this.listeners={};this.dataset={};this.style={};this.classList={toggle(){}};this.rect={left:0,top:0,width:360,height:400};}
  setAttribute(key,value){this.attributes.set(key,String(value));}
  getAttribute(key){return this.attributes.get(key)??null;}
  addEventListener(event,callback){this.listeners[event]=callback;}
  querySelector(selector){if(!this.nodes.has(selector))this.nodes.set(selector,new EditorElement());return this.nodes.get(selector);}
  querySelectorAll(){return [];}
  focus(){} scrollIntoView(){} remove(){} showModal(){this.open=true;} close(){this.open=false;}
  getBoundingClientRect(){return this.rect;}
}
async function editorDOM(run){
  const previous=global.document,children=[];
  global.document={activeElement:new EditorElement(),createElement:()=>new EditorElement(),body:{append:(...items)=>children.push(...items)}};
  try{await run(children);}finally{global.document=previous;}
}
function clickEditor(dialog,selector,props={}){
  const target=dialog.querySelector(selector);target.closest=query=>query===selector?target:null;
  dialog.listeners.click({target,...props});
}
function pointerEditor(dialog,event,side='current',index=null,props={}){
  const board=dialog.querySelector(`[data-board="${side}"]`),target=index==null?board:new EditorElement();
  if(index!=null)target.dataset.tileIndex=String(index);
  target.closest=selector=>selector===`[data-board="${side}"]`?board:selector==='[data-tile-index]'&&index!=null?target:null;
  let prevented=false;
  dialog.listeners[event]?.({target,clientX:60,clientY:60,preventDefault(){prevented=true;},...props});
  return prevented;
}
function closeEditor(editor,children){editor.close();if(children[1].open)children[1].querySelector('[data-discard]').listeners.click();}

test('clicking a visually empty tower anchor never deletes the displaced tile', async () => editorDOM(async children => {
  const board={template_id:'tower',towers:[{index:0,x:4,y:4,direction:3}],tiles:[{...tile(0,4,4,10),tower_index:0}]};
  const editor=E.open({board}),dialog=editor.element;
  clickEditor(dialog,'[data-edit]');clickEditor(dialog,'[data-delete]');
  clickEditor(dialog,'[data-board="current"]',{clientX:90,clientY:90});
  try{assert.equal(editor.getBoard().tiles.length,1);}finally{editor.close();if(children[1].open)children[1].querySelector('[data-discard]').listeners.click();}
}));

test('saving marks only the save button busy so inline live feedback remains available', async () => editorDOM(async () => {
  let resolveSave,savedTiles;
  const board={template_id:'one',tiles:[],towers:[]};
  const editor=E.open({board,onSave:tiles=>{savedTiles=tiles;return new Promise(resolve=>resolveSave=resolve);}}),dialog=editor.element;
  clickEditor(dialog,'[data-edit]');clickEditor(dialog,'[data-board="current"]',{clientX:60,clientY:60});clickEditor(dialog,'[data-save]');
  try{
    assert.notEqual(dialog.getAttribute('aria-busy'),'true');
    assert.equal(dialog.querySelector('[data-save]').getAttribute('aria-busy'),'true');
    assert.match(dialog.querySelector('.cc-feedback').textContent,/正在保存/);
  }finally{
    resolveSave({...board,tiles:savedTiles});await new Promise(resolve=>setImmediate(resolve));editor.close();
  }
}));

test('right-click preserves native menus outside the editing current board', async () => editorDOM(async children => {
  const editor=E.open({board:{tiles:[tile(0,2,2,0)],towers:[]}}),dialog=editor.element;
  try{
    assert.equal(pointerEditor(dialog,'contextmenu'),false);
    clickEditor(dialog,'[data-edit]');
    assert.equal(pointerEditor(dialog,'contextmenu','original',0),false);
    assert.equal(editor.getBoard().tiles.length,1);
  }finally{closeEditor(editor,children);}
}));

test('right-click deletes the actual visible tile without changing the B brush', async () => editorDOM(async children => {
  const editor=E.open({board:{tiles:[tile(0,2,2,0),tile(1,3,3,1)],towers:[]}}),dialog=editor.element;
  clickEditor(dialog,'[data-edit]');dialog.querySelector('[data-brush]').dataset.brush='-11';clickEditor(dialog,'[data-brush]');
  try{
    assert.equal(pointerEditor(dialog,'contextmenu','current',0,{clientX:50,clientY:50}),true);
    assert.deepEqual(editor.getBoard().tiles.map(t=>t.index),[1]);
    clickEditor(dialog,'[data-board="current"]',{clientX:180,clientY:180});
    assert.equal(editor.getBoard().tiles.at(-1).tile_id,-11);
  }finally{closeEditor(editor,children);}
}));

test('fixed-layer right-click cannot delete a clicked tile from another layer', async () => editorDOM(async children => {
  const editor=E.open({board:{tiles:[tile(0,4,4,2),tile(1,2,2,0)],towers:[]}}),dialog=editor.element;
  clickEditor(dialog,'[data-edit]');dialog.listeners.change({target:{value:'1',matches:selector=>selector==='[data-layer]'}});
  try{
    assert.equal(pointerEditor(dialog,'contextmenu','current',0,{clientX:100,clientY:100}),true);
    assert.equal(editor.getBoard().tiles.length,2);
    pointerEditor(dialog,'contextmenu','current',1);
    assert.deepEqual(editor.getBoard().tiles.map(t=>t.index),[0]);
  }finally{closeEditor(editor,children);}
}));

test('cursor placement projects the actual added or deleted tower tile', () => {
  const board={towers:[{index:0,x:4,y:8,direction:0}],tiles:[{...tile(0,4,8,0),tower_index:0}]};
  assert.deepEqual(E.cursorPlacement(board,'add',4,8,2),{x:4,y:8-16/85,z:1});
  const top={...tile(1,4,8,3),tower_index:0};board.tiles.push(top);
  assert.deepEqual(E.cursorPlacement(board,'delete',4,8,0,1),{x:4,y:8-3*16/85,z:3});
  assert.deepEqual(E.cursorPlacement(board,'add',9,9,1),{x:9,y:9,z:0});
});

test('the visible add cursor uses the projected placement and matches the newly rendered tile', async () => editorDOM(async children => {
  const board={towers:[{index:0,x:4,y:8,direction:0}],tiles:[tile(0,4,8,0)]};
  const editor=E.open({board}),dialog=editor.element;
  clickEditor(dialog,'[data-edit]');dialog.listeners.change({target:{value:'2',matches:selector=>selector==='[data-layer]'}});
  try{
    pointerEditor(dialog,'pointermove','current',null,{clientX:100,clientY:180});
    const visible=dialog.querySelector('.cc-tile-cursor'),expected=E.anchor({x:4,y:8-16/85});
    assert.equal(visible.hidden,false);
    assert.ok(Math.abs(parseFloat(visible.style.top)-expected.top)<1e-10);
    clickEditor(dialog,'[data-board="current"]',{clientX:100,clientY:180});
    const current=editor.getBoard(),added=current.tiles.at(-1),addedAnchor=E.anchor(E.projection(current,added));
    assert.ok(Math.abs(parseFloat(dialog.querySelector('.cc-tile-cursor').style.top)-addedAnchor.top)<1e-10);
  }finally{closeEditor(editor,children);}
}));

test('the delete cursor follows the actual projected hit rather than the snapped mouse anchor', async () => editorDOM(async children => {
  const board={towers:[{index:0,x:4,y:8,direction:0}],tiles:[{...tile(0,4,8,3),tower_index:0}]};
  const editor=E.open({board}),dialog=editor.element;clickEditor(dialog,'[data-edit]');clickEditor(dialog,'[data-delete]');
  try{
    pointerEditor(dialog,'pointermove','current',0,{clientX:100,clientY:170});
    const expected=E.anchor(E.projection(board,board.tiles[0]));
    assert.ok(Math.abs(parseFloat(dialog.querySelector('.cc-tile-cursor').style.top)-expected.top)<1e-10);
  }finally{closeEditor(editor,children);}
}));

// Resolve this stylesheet's explicit selector declarations at a viewport width.
// Browser QA owns layout; this pins the original tool's authored visual contract.
function editorStyle(selector,width=1200){
  const css=fs.readFileSync(require.resolve('../frontend/assets/editor.css'),'utf8'),style={};
  function read(source){
    let start=0;
    while(start<source.length){
      const open=source.indexOf('{',start);if(open<0)break;
      const header=source.slice(start,open).trim();let depth=1,end=open+1;
      while(end<source.length&&depth){if(source[end]==='{')depth++;if(source[end]==='}')depth--;end++;}
      const body=source.slice(open+1,end-1);start=end;
      if(header.startsWith('@media')){const max=header.match(/max-width:\s*(\d+)px/);if(max&&width<=Number(max[1]))read(body);continue;}
      if(!header.split(',').some(value=>value.trim()===selector))continue;
      for(const item of body.split(';')){const colon=item.indexOf(':');if(colon>0)style[item.slice(0,colon).trim()]=item.slice(colon+1).trim();}
    }
  }
  read(css);return style;
}

test('canvas keeps original tile dimensions, corners, shadow and separate label positions', () => {
  const tileStyle=editorStyle('.cc-tile'),boardStyle=editorStyle('.cc-board'),layerStyle=editorStyle('.cc-tile-layer');
  assert.equal(boardStyle['aspect-ratio'],'18/20');
  assert.equal(boardStyle['border-radius'],'10px');
  assert.equal(tileStyle.width,'11.111111%');assert.equal(tileStyle.height,'10%');
  assert.equal(tileStyle['border-radius'],'6px');
  assert.equal(tileStyle['box-shadow'],'0 3px 1px rgba(68,57,34,.4)');
  assert.equal(editorStyle('.cc-tile-label')['font-size'],'18px');
  assert.equal(layerStyle.top,'1px');assert.equal(layerStyle.right,'3px');assert.equal(layerStyle['font-size'],'12px');
});

test('390px preview uses the original single-column board and 14px labels without shrinking tile corners', () => {
  assert.equal(editorStyle('.cc-comparison',390)['grid-template-columns'],'minmax(0,1fr)');
  assert.equal(editorStyle('.cc-comparison',390).gap,'24px');
  assert.equal(editorStyle('.cc-side',390)['row-gap'],'0');
  assert.equal(editorStyle('.cc-tile-label',390)['font-size'],'14px');
  assert.equal(editorStyle('.cc-tile-layer',390).right,'3px');
  assert.equal(editorStyle('.cc-tile',390)['border-radius'],'6px');
  assert.equal(editorStyle('.cc-side h3 span',390)['font-size'],'14px');
});

test('list thumbnail uses the same projected 2x2 tile geometry with no crowded text labels', () => {
  const board={towers:[{index:0,x:4,y:4,direction:3}],tiles:[tile(0,2,2,0),tile(1,2,2,1,-11),{...tile(2,4,4,1),tower_index:0}]};
  const markup=E.thumbnailMarkup(board),rects=[...markup.matchAll(/<rect\b([^>]*)\/>/g)].map(match=>Object.fromEntries([...match[1].matchAll(/([\w-]+)="([^"]*)"/g)].map(attribute=>[attribute[1],attribute[2]])));
  assert.match(markup,/viewBox="0 0 18 20"/);assert.equal(rects.length,3);
  for(const rect of rects){assert.equal(rect.width,'2');assert.equal(rect.height,'2');assert.equal(rect.rx,'.12');}
  assert.equal(Number(rects[2].x),4+16/85);assert.equal(Number(rects[2].y),4);
  assert.match(rects[0].class,/is-covered/);assert.match(rects[1].class,/is-b/);
  assert.doesNotMatch(markup,/<(?:text|span)\b/);
});

test('whole-board draft translation and rotation preserve tower identity, direction and undo metadata without geometry limits', () => {
  const original={tiles:[{...tile(0,4,8,3),tower_index:7}],towers:[{index:7,x:4,y:8,direction:0,count:1,custom:{keep:true}}]};
  const start=E.create(original),moved=E.translate(start,100,-100),turned=E.rotate(moved,'cw');
  assert.equal(moved.tiles[0].x,104);assert.equal(moved.towers[0].y,-92);
  assert.deepEqual(turned.tiles.map(t=>[t.x,t.y,t.z,t.tower_index]),[[109,105,3,7]]);
  assert.deepEqual(turned.towers[0],{index:7,x:109,y:105,direction:3,count:1,custom:{keep:true}});
  assert.deepEqual(E.undo(turned),moved);assert.deepEqual(E.undo(moved),start);
  assert.deepEqual(original.towers[0],{index:7,x:4,y:8,direction:0,count:1,custom:{keep:true}});
});

test('clear layer excludes selected layer from above and restores all tower data on undo', () => {
  const board={tiles:[tile(0,2,2,0),{...tile(1,4,8,1),tower_index:7},{...tile(2,4,8,9),tower_index:7}],towers:[{index:7,x:4,y:8,direction:0,count:2,custom:'retain'}]};
  const start=E.create(board),above=E.clearLayer(start,2,'above');
  assert.deepEqual(above.tiles.map(t=>t.index),[0,1]);assert.equal(above.towers[0].count,1);
  assert.deepEqual(E.undo(above),start);
  const single=E.clearLayer(start,2,'layer');assert.deepEqual(single.tiles.map(t=>t.index),[0,2]);
  const emptied=E.apply(E.apply(start,{action:'delete',index:1}),{action:'delete',index:2});assert.deepEqual(emptied.towers,[{index:7,x:4,y:8,direction:0,count:0,custom:'retain'}]);
  const refilled=E.apply(emptied,{action:'add',x:4,y:8,z:0,tile_id:-10});assert.equal(refilled.tiles.at(-1).tower_index,7);assert.equal(refilled.towers[0].count,1);
  assert.deepEqual(E.undo(E.undo(emptied)),start);
});

test('editor reuses two complete old toolbar rows and main action changes from edit to save', async () => editorDOM(async children=>{
  const editor=E.open({board:{tiles:[tile(0,2,2,0)],towers:[]}}),dialog=editor.element;
  try{
    const markup=dialog.innerHTML;
    const heading=markup.match(/<h2 id="([^"]+)"/)[1];assert.equal(dialog.querySelector(`[id="${heading}"]`).textContent,'原模板与新模板对比');
    const controls=['data-layer-unlimited','data-layer-minus','data-layer-range','data-layer-plus','data-layer-label','data-only','data-clear-layer','data-clear-above'];
    assert.ok(controls.every((name,index)=>markup.indexOf(name)> (index?markup.indexOf(controls[index-1]):-1)));
    assert.match(markup,/data-translate="up"/);assert.match(markup,/data-rotate="ccw"/);assert.match(markup,/data-coordinate-add/);
    assert.equal(dialog.querySelector('[data-edit]').textContent,'去编辑');
    clickEditor(dialog,'[data-edit]');assert.equal(dialog.querySelector('[data-edit]').textContent,'退出编辑');
    clickEditor(dialog,'[data-board="current"]',{clientX:180,clientY:180});
    assert.equal(dialog.querySelector('[data-edit]').textContent,'保存编辑');
    assert.equal(dialog.querySelector('[data-brush-count="-10"]').textContent,'2 (1)');
  }finally{closeEditor(editor,children);}
}));

test('layer stepping and unlimited synchronize view and reset only-layer mode', async () => editorDOM(async children=>{
  const editor=E.open({board:{tiles:[tile(0,2,2,0),tile(1,3,3,2)],towers:[]}}),dialog=editor.element;
  try{
    clickEditor(dialog,'[data-layer-plus]');assert.equal(dialog.querySelector('[data-layer-range]').value,1);
    dialog.listeners.change({target:{checked:true,matches:s=>s==='[data-only]'}});
    clickEditor(dialog,'[data-layer-unlimited]');assert.equal(dialog.querySelector('[data-only]').checked,false);assert.equal(dialog.querySelector('[data-only]').disabled,true);
    clickEditor(dialog,'[data-layer-minus]');assert.equal(dialog.querySelector('[data-layer-range]').value,0);
  }finally{closeEditor(editor,children);}
}));

test('non-edit transform saves towers and undo invokes persisted history while edit transform stays in draft', async () => editorDOM(async children=>{
  const board={tiles:[{...tile(0,4,8,0),tower_index:7}],towers:[{index:7,x:4,y:8,direction:0,custom:'keep'}],can_undo:true};let saves=0,undos=0;
  const editor=E.open({board,onSave:async(tiles,metadata)=>{saves++;return {...board,tiles,towers:metadata.towers,can_undo:true};},onUndo:async()=>{undos++;return {...board,can_undo:false};}}),dialog=editor.element;
  try{
    const right=dialog.querySelector('[data-translate]');right.dataset.translate='right';clickEditor(dialog,'[data-translate]');await new Promise(r=>setImmediate(r));
    assert.equal(saves,1);assert.equal(editor.getBoard().towers[0].x,5);assert.equal(editor.getBoard().tiles[0].x,5);
    clickEditor(dialog,'[data-undo]');await new Promise(r=>setImmediate(r));assert.equal(undos,1);assert.equal(editor.getBoard().tiles[0].x,4);
    clickEditor(dialog,'[data-edit]');clickEditor(dialog,'[data-translate]');assert.equal(saves,1);assert.equal(editor.getBoard().tiles[0].x,5);
    clickEditor(dialog,'[data-undo]');assert.equal(undos,1);assert.equal(editor.getBoard().tiles[0].x,4);
  }finally{closeEditor(editor,children);}
}));

test('coordinate additions permit arbitrary draft positions and failed save retains towers and draft', async () => editorDOM(async children=>{
  const board={tiles:[],towers:[{index:4,x:40,y:-20,direction:2}]};let received;
  const editor=E.open({board,onSave:async(tiles,metadata)=>{received={tiles,metadata};throw Error('保存校验失败');}}),dialog=editor.element;
  try{
    clickEditor(dialog,'[data-edit]');dialog.querySelector('[data-coordinate-x]').value='40';dialog.querySelector('[data-coordinate-y]').value='-20';
    clickEditor(dialog,'[data-coordinate-add]');assert.equal(editor.getBoard().tiles[0].x,40);
    clickEditor(dialog,'[data-edit]');await new Promise(r=>setImmediate(r));
    assert.equal(received.metadata.towers[0].index,4);assert.equal(editor.getBoard().tiles.length,1);assert.match(dialog.querySelector('.cc-feedback').textContent,/保存校验失败/);
  }finally{closeEditor(editor,children);}
}));

test('bulk deletion confirms hidden upper tiles and starts a reversible draft without saving', async () => editorDOM(async children=>{
  let saves=0;const board={tiles:[tile(0,2,2,0),tile(1,3,3,1),tile(2,4,4,9)],towers:[]};
  const editor=E.open({board,onSave:async()=>{saves++;return board;}}),dialog=editor.element;
  try{
    dialog.listeners.change({target:{value:'2',matches:s=>s==='[data-layer]'}});clickEditor(dialog,'[data-clear-above]');
    const confirmation=children[2];assert.equal(confirmation.open,true);assert.match(confirmation.querySelector('[data-clear-description]').textContent,/1 张.*不含/);
    confirmation.querySelector('[data-clear-confirm]').listeners.click();assert.deepEqual(editor.getBoard().tiles.map(t=>t.index),[0,1]);assert.equal(saves,0);
    clickEditor(dialog,'[data-undo]');assert.deepEqual(editor.getBoard().tiles.map(t=>t.index),[0,1,2]);
  }finally{closeEditor(editor,children);}
}));

test('both toolbar rows retain 44px controls and local horizontal scrolling on narrow screens',()=>{
  assert.equal(editorStyle('.cover-editor-dialog').border,'0');
  assert.equal(editorStyle('.cc-comparison')['max-width'],'none');
  for(const width of [1200,390]){
    assert.equal(editorStyle('.cc-toolbar',width)['flex-wrap'],'nowrap');
    assert.equal(editorStyle('.cc-edit-toolbar',width)['flex-wrap'],'nowrap');
    assert.equal(editorStyle('.cc-toolbar',width)['overflow-x'],'auto');
    assert.equal(editorStyle('.cc-edit-toolbar',width)['overflow-x'],'auto');
    assert.equal(editorStyle('.cc-layer-control .icon-button',width).width,'44px');
    assert.equal(editorStyle('.cc-transform-actions .icon-button',width).width,'44px');
    assert.equal(editorStyle('.cc-brush-choice',width)['min-height'],'44px');
    assert.equal(editorStyle('.cc-save',width)['margin-left'],'auto');
  }
});

test('unlimited add on a projected tower top reuses its unprojected anchor and membership',()=>{
  const board={tiles:[{...tile(0,4,8,10),tower_index:7}],towers:[{index:7,x:4,y:8,direction:3}]};
  assert.deepEqual(E.target(board,'add',6,8,0,0),{x:4,y:8,z:11});
  const next=E.apply(E.create(board),{action:'add',...E.target(board,'add',6,8,0,0),tile_id:-10});
  assert.equal(next.tiles.at(-1).tower_index,7);
});

test('checkbox deletion can toggle off and the same-position replacement remains an unsaved edit',async()=>editorDOM(async children=>{
  const editor=E.open({board:{tiles:[tile(0,2,2,0)],towers:[]}}),dialog=editor.element;
  try{
    clickEditor(dialog,'[data-edit]');clickEditor(dialog,'[data-delete]');assert.equal(dialog.querySelector('[data-delete]').checked,true);
    clickEditor(dialog,'[data-delete]');assert.equal(dialog.querySelector('[data-delete]').checked,false);
    pointerEditor(dialog,'contextmenu','current',0);clickEditor(dialog,'[data-board="current"]',{clientX:60,clientY:60});
    assert.equal(dialog.querySelector('[data-edit]').textContent,'保存编辑');
  }finally{closeEditor(editor,children);}
}));

test('marked movement can be hidden while issue outlines remain visible',async()=>editorDOM(async children=>{
  const editor=E.open({board:{tiles:[tile(0,2,2,0),tile(1,2,2,1)],towers:[]}}),dialog=editor.element;
  try{
    clickEditor(dialog,'[data-edit]');const button=dialog.querySelector('[data-translate]');button.dataset.translate='right';clickEditor(dialog,'[data-translate]');
    assert.match(dialog.querySelector('[data-current-board]').innerHTML,/is-moved/);
    dialog.listeners.change({target:{checked:false,matches:s=>s==='[data-mark-changes]'}});
    assert.doesNotMatch(dialog.querySelector('[data-current-board]').innerHTML,/is-moved/);assert.match(dialog.querySelector('[data-current-board]').innerHTML,/is-issue/);
  }finally{closeEditor(editor,children);}
}));

test('closing a draft can save and close only after a successful save',async()=>editorDOM(async children=>{
  const board={tiles:[],towers:[]};let fail=true;
  const editor=E.open({board,onSave:async tiles=>{if(fail)throw Error('未通过校验');return {...board,tiles};}}),dialog=editor.element;
  clickEditor(dialog,'[data-edit]');clickEditor(dialog,'[data-board="current"]',{clientX:60,clientY:60});editor.close();
  const prompt=children[1];assert.equal(prompt.open,true);
  try{
    prompt.querySelector('[data-save-close]').listeners.click();await new Promise(r=>setImmediate(r));
    assert.equal(dialog.open,true);assert.equal(prompt.open,true);assert.equal(editor.getBoard().tiles.length,1);assert.match(prompt.querySelector('[data-discard-feedback]').textContent,/未通过校验/);
    fail=false;prompt.querySelector('[data-save-close]').listeners.click();await new Promise(r=>setImmediate(r));assert.equal(dialog.open,false);
  }finally{closeEditor(editor,children);}
}));

test('failed automatic transforms do not replace current board and duplicate pending operations are blocked',async()=>editorDOM(async children=>{
  const board={tiles:[tile(0,2,2,0)],towers:[]};let reject,calls=0;
  const editor=E.open({board,onSave:()=>{calls++;return new Promise((resolve,no)=>reject=no);}}),dialog=editor.element;
  try{
    const button=dialog.querySelector('[data-translate]');button.dataset.translate='right';clickEditor(dialog,'[data-translate]');clickEditor(dialog,'[data-translate]');
    assert.equal(calls,1);assert.equal(editor.getBoard().tiles[0].x,2);reject(Error('超出保存范围'));await new Promise(r=>setImmediate(r));
    assert.equal(editor.getBoard().tiles[0].x,2);assert.match(dialog.querySelector('.cc-feedback').textContent,/本次变换未保存/);
  }finally{closeEditor(editor,children);}
}));

test('the move coordinate disclosure compares source and current locations after translation',async()=>editorDOM(async children=>{
  const editor=E.open({board:{tiles:[tile(0,2,2,0)],towers:[]}}),dialog=editor.element;
  try{
    clickEditor(dialog,'[data-edit]');const button=dialog.querySelector('[data-translate]');button.dataset.translate='right';clickEditor(dialog,'[data-translate]');
    assert.match(dialog.querySelector('[data-moves-body]').innerHTML,/\(2, 2, 0\).*\(3, 2, 0\)/);
    assert.match(dialog.querySelector('.cc-preview-feedback').textContent,/挪动 1 张/);
  }finally{closeEditor(editor,children);}
}));

test('problem locator is available before editing and cycles both members without saving',async()=>editorDOM(async children=>{
  const board={tiles:[tile(0,5,5,0),tile(1,5,5,1)],towers:[]};
  const editor=E.open({board}),dialog=editor.element;
  try{
    const locate=dialog.querySelector('[data-locate-issue]');
    assert.equal(locate.hidden,false);assert.match(locate.textContent,/（2）/);
    assert.equal(dialog.querySelector('.cc-manual-feedback').hidden,false);
    clickEditor(dialog,'[data-locate-issue]');assert.equal(dialog.querySelector('[data-layer-range]').value,1);
    clickEditor(dialog,'[data-locate-issue]');assert.equal(dialog.querySelector('[data-layer-range]').value,2);
    clickEditor(dialog,'[data-locate-issue]');assert.equal(dialog.querySelector('[data-layer-range]').value,1);
    assert.equal(dialog.querySelector('[data-edit]').textContent,'去编辑');
    clickEditor(dialog,'[data-edit]');assert.equal(locate.hidden,false);
  }finally{closeEditor(editor,children);}
}));

test('unsupported locator derives floating tiles and ignores legacy bounds validation',async()=>editorDOM(async children=>{
  const board={tiles:[tile(0,5,5,3),tile(1,1,1,0)],towers:[],validation_errors:['悬空：(5,5)第4层（Z=3）没有紧邻下层的正面积承接','坐标超出安全范围：(1,1)第1层（Z=0）']};
  let editor=E.open({board}),dialog=editor.element;
  try{
    assert.equal(dialog.querySelector('[data-locate-issue]').hidden,true);
    assert.equal(dialog.querySelector('[data-locate-unsupported]').textContent,'定位悬空（1）');
    clickEditor(dialog,'[data-locate-unsupported]');assert.equal(dialog.querySelector('[data-layer-range]').value,4);
    assert.match(dialog.querySelector('[data-current-board]').innerHTML,/is-unsupported/);
    assert.doesNotMatch(dialog.querySelector('[data-current-board]').innerHTML,/is-issue/);
    assert.doesNotMatch(dialog.querySelector('[data-current-board]').innerHTML,/全遮盖问题牌/);
    clickEditor(dialog,'[data-locate-unsupported]');assert.equal(dialog.querySelector('[data-layer-range]').value,4);
  }finally{closeEditor(editor,children);}
  editor=E.open({board:{tiles:[tile(0,5,5,0)],towers:[]}});dialog=editor.element;
  try{assert.equal(dialog.querySelector('[data-locate-issue]').hidden,true);assert.equal(dialog.querySelector('[data-locate-unsupported]').hidden,true);}finally{closeEditor(editor,children);}
}));

test('repaired comparison can still locate the original problem without reporting it in the new template',async()=>editorDOM(async children=>{
  const original={tiles:[tile(0,5,5,0),tile(1,5,5,1)],towers:[]};
  const board={tiles:[tile(0,5,5,0),tile(1,9,9,0)],towers:[]};
  const editor=E.open({board,original}),dialog=editor.element;
  try{
    assert.equal(dialog.querySelector('[data-locate-issue]').hidden,false);
    clickEditor(dialog,'[data-locate-issue]');assert.match(dialog.querySelector('.cc-feedback').textContent,/原模板/);
    assert.doesNotMatch(dialog.querySelector('[data-current-board]').innerHTML,/is-issue/);
  }finally{closeEditor(editor,children);}
}));

test('technical save failures retain the draft without turning error coordinates into red issues',async()=>editorDOM(async children=>{
  const board={tiles:[tile(0,40,-20,5)],towers:[]};
  const editor=E.open({board,onSave:async()=>{throw Error('第 6 层 (40, -20, 5) 超出保存范围');}}),dialog=editor.element;
  try{
    clickEditor(dialog,'[data-edit]');const button=dialog.querySelector('[data-translate]');button.dataset.translate='right';clickEditor(dialog,'[data-translate]');clickEditor(dialog,'[data-edit]');await new Promise(r=>setImmediate(r));
    assert.equal(dialog.querySelector('[data-locate-issue]').hidden,true);clickEditor(dialog,'[data-locate-unsupported]');assert.equal(dialog.querySelector('[data-layer-range]').value,6);assert.equal(editor.getBoard().tiles.length,1);
    assert.doesNotMatch(dialog.querySelector('[data-current-board]').innerHTML,/is-issue/);
  }finally{closeEditor(editor,children);}
}));

test('support requires positive area on Z minus one using actual tower projections',()=>{
  const board={towers:[{index:7,x:4,y:8,direction:3}],tiles:[tile(0,7,8,2),{...tile(1,4,8,1),tower_index:7},tile(2,6,8,2),tile(3,0,0,1),tile(4,0,0,-1)]};
  assert.deepEqual(E.unsupportedTiles(board).map(t=>t.index),[0,1,3]);
  const edge={towers:[{index:7,x:4,y:8,direction:3}],tiles:[tile(0,22,8,86),{...tile(1,4,8,85),tower_index:7}]};
  assert.deepEqual(E.unsupportedTiles(edge).map(t=>t.index),[0,1]);
  edge.tiles[0].x=21;
  assert.deepEqual(E.unsupportedTiles(edge).map(t=>t.index),[1]);
});

test('full cover and unsupported locate cycles remain independent in the existing feedback row',async()=>editorDOM(async children=>{
  const board={tiles:[tile(0,5,5,0),tile(1,5,5,2),tile(2,9,9,4)],towers:[]};
  const editor=E.open({board}),dialog=editor.element;
  try{
    assert.equal(dialog.querySelector('[data-locate-issue]').textContent,'定位全遮盖（2）');
    assert.equal(dialog.querySelector('[data-locate-unsupported]').textContent,'定位悬空（2）');
    clickEditor(dialog,'[data-locate-issue]');assert.equal(dialog.querySelector('[data-layer-range]').value,1);
    clickEditor(dialog,'[data-locate-unsupported]');assert.equal(dialog.querySelector('[data-layer-range]').value,3);
    clickEditor(dialog,'[data-locate-issue]');assert.equal(dialog.querySelector('[data-layer-range]').value,3);
    clickEditor(dialog,'[data-locate-unsupported]');assert.equal(dialog.querySelector('[data-layer-range]').value,5);
    clickEditor(dialog,'[data-locate-issue]');assert.equal(dialog.querySelector('[data-layer-range]').value,1);
    const markup=dialog.innerHTML;
    assert.match(markup,/<div class="cc-manual-feedback">[\s\S]*data-locate-issue[\s\S]*data-locate-unsupported[\s\S]*?<\/div>/);
    assert.equal((markup.match(/class="cc-toolbar"/g)||[]).length,1);
    assert.equal((markup.match(/class="cc-edit-toolbar"/g)||[]).length,1);
  }finally{closeEditor(editor,children);}
}));

test('red full cover and blue unsupported marks can coexist and remain live while editing',async()=>editorDOM(async children=>{
  const board={tiles:[tile(0,5,5,0),tile(1,5,5,2),tile(2,9,9,4),tile(3,-20,40,0)],towers:[],validation_errors:['坐标超出安全范围：(-20,40)第1层（Z=0）']};
  const editor=E.open({board}),dialog=editor.element;
  const tileTag=index=>dialog.querySelector('[data-current-board]').innerHTML.match(new RegExp('<(?:span|button) [^>]*data-tile-index="'+index+'"[^>]*>'))[0];
  try{
    assert.match(tileTag(0),/is-issue/);assert.doesNotMatch(tileTag(0),/is-unsupported/);
    assert.match(tileTag(1),/is-issue/);assert.match(tileTag(1),/is-unsupported/);
    assert.doesNotMatch(tileTag(2),/is-issue/);assert.match(tileTag(2),/is-unsupported/);
    assert.doesNotMatch(tileTag(3),/is-issue|is-unsupported/);
    assert.match(dialog.innerHTML,/蓝框悬空/);assert.doesNotMatch(dialog.innerHTML,/安全核验/);
    clickEditor(dialog,'[data-edit]');pointerEditor(dialog,'contextmenu','current',2,{clientX:200,clientY:200});
    assert.equal(dialog.querySelector('[data-locate-unsupported]').textContent,'定位悬空（2）');
    clickEditor(dialog,'[data-locate-unsupported]');clickEditor(dialog,'[data-locate-unsupported]');
    assert.match(dialog.querySelector('.cc-feedback').textContent,/原模板/);assert.doesNotMatch(dialog.querySelector('.cc-feedback').textContent,/原模板 \/ 新模板/);
    assert.equal(editorStyle('.cc-tile.is-unsupported').outline,'3px solid var(--cc-unsupported)');
    assert.match(editorStyle('.cc-tile.is-issue.is-unsupported')['box-shadow'],/inset.*var\(--cc-unsupported\)/);
    assert.equal(editorStyle('.cc-tile.is-moved.is-unsupported').outline,'3px solid var(--cc-unsupported)');
  }finally{closeEditor(editor,children);}
}));

test('successful issue-bearing saves are saved warnings and remain available for export',async()=>editorDOM(async children=>{
  const board={tiles:[tile(0,5,5,0),tile(1,5,5,2),tile(2,9,9,4)],towers:[]};let received;
  const editor=E.open({board,onSave:async tiles=>{received=tiles;return {...board,tiles};}}),dialog=editor.element;
  try{
    clickEditor(dialog,'[data-edit]');dialog.querySelector('[data-coordinate-x]').value='-100';dialog.querySelector('[data-coordinate-y]').value='100';clickEditor(dialog,'[data-coordinate-add]');
    assert.doesNotMatch(dialog.querySelector('.cc-instructions').textContent,/保存时核验|修正.*保存/);
    clickEditor(dialog,'[data-edit]');await new Promise(r=>setImmediate(r));
    assert.equal(received.length,4);assert.equal(received.at(-1).x,-100);assert.equal(dialog.querySelector('[data-edit]').textContent,'去编辑');
    const feedback=dialog.querySelector('.cc-feedback').textContent;
    assert.match(feedback,/已保存.*全遮盖.*悬空.*仅提示，不影响导出/);assert.doesNotMatch(feedback,/草稿|请.*修正|验收/);
  }finally{closeEditor(editor,children);}
}));

test('saved transformations still show moved marks after server normalizes transient tile fields',async()=>editorDOM(async children=>{
  const board={tiles:[tile(0,2,2,0)],towers:[]};const editor=E.open({board,onSave:async tiles=>({...board,tiles:tiles.map(({changed,...t})=>t)})}),dialog=editor.element;
  try{
    const button=dialog.querySelector('[data-translate]');button.dataset.translate='right';clickEditor(dialog,'[data-translate]');await new Promise(r=>setImmediate(r));
    assert.ok(dialog.querySelector('[data-current-board]').innerHTML.includes('is-moved'));
  }finally{closeEditor(editor,children);}
}));

test('coordinate details stays anchored below its toolbar button on desktop',async()=>editorDOM(async children=>{
  const priorWidth=global.innerWidth,priorHeight=global.innerHeight;global.innerWidth=1440;global.innerHeight=1100;
  const editor=E.open({board:{tiles:[],towers:[]}}),dialog=editor.element;
  try{
    clickEditor(dialog,'[data-edit]');const details=dialog.querySelector('.cc-coordinate');details.open=true;details.matches=s=>s==='.cc-coordinate';
    dialog.querySelector('.cc-coordinate>summary').rect={left:1020,top:272,right:1064,bottom:316,width:44,height:44};
    dialog.listeners.toggle({target:details});assert.equal(dialog.querySelector('.cc-coordinate-actions').style.top,'316px');assert.equal(dialog.querySelector('.cc-coordinate-actions').style.right,'376px');
  }finally{global.innerWidth=priorWidth;global.innerHeight=priorHeight;closeEditor(editor,children);}
}));

test('holding right mouse button previews deletion without switching the chosen brush',async()=>editorDOM(async children=>{
  const editor=E.open({board:{tiles:[tile(0,2,2,0)],towers:[]}}),dialog=editor.element;
  try{
    clickEditor(dialog,'[data-edit]');pointerEditor(dialog,'pointermove','current',0,{buttons:2});
    assert.equal(dialog.querySelector('.cc-tile-cursor').dataset.label,'删除 · 第 1 层');assert.equal(dialog.querySelector('.cc-tile-cursor').textContent,'');
    pointerEditor(dialog,'pointermove','current',0,{buttons:0});assert.equal(dialog.querySelector('.cc-tile-cursor').dataset.label,'A · 第 2 层');
  }finally{closeEditor(editor,children);}
}));

test('saved reindexing uses source identity to distinguish deletion and addition from movement',async()=>editorDOM(async children=>{
  const original={tiles:[{...tile(0,2,2,0),source_index:0},{...tile(1,6,2,0),source_index:1},{...tile(2,10,2,0),source_index:2}],towers:[]};
  const editor=E.open({board:original,onSave:async(tiles,metadata)=>({...original,tiles:tiles.map((t,index)=>({...t,index,source_index:t.index<0?null:t.source_index,added:false})),towers:metadata.towers})}),dialog=editor.element;
  try{
    clickEditor(dialog,'[data-edit]');pointerEditor(dialog,'contextmenu','current',0);dialog.querySelector('[data-coordinate-x]').value='14';dialog.querySelector('[data-coordinate-y]').value='2';clickEditor(dialog,'[data-coordinate-add]');clickEditor(dialog,'[data-edit]');await new Promise(r=>setImmediate(r));
    assert.equal(dialog.querySelector('.cc-preview-feedback').textContent,'新增 1 张，删除 1 张，挪动 0 张 Tile。');
    assert.match(dialog.querySelector('[data-moves-body]').innerHTML,/暂无挪动/);
    const markup=dialog.querySelector('[data-current-board]').innerHTML;assert.equal((markup.match(/is-moved/g)||[]).length,0);assert.equal((markup.match(/is-added/g)||[]).length,1);
  }finally{closeEditor(editor,children);}
}));

test('unsaved edits warn on page unload and closing removes the lifecycle listeners',async()=>editorDOM(async children=>{
  const previous=global.window,listeners=new Map();global.window={addEventListener:(name,callback)=>listeners.set(name,callback),removeEventListener:name=>listeners.delete(name)};
  const editor=E.open({board:{tiles:[],towers:[]}}),dialog=editor.element;
  try{
    let prevented=false;listeners.get('beforeunload')({preventDefault(){prevented=true;}});assert.equal(prevented,false);
    clickEditor(dialog,'[data-edit]');clickEditor(dialog,'[data-board="current"]',{clientX:60,clientY:60});const event={preventDefault(){prevented=true;}};listeners.get('beforeunload')(event);assert.equal(prevented,true);assert.equal(event.returnValue,'');
    closeEditor(editor,children);assert.equal(listeners.has('beforeunload'),false);
  }finally{closeEditor(editor,children);global.window=previous;}
}));
