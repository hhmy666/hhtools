(function(root,factory){
  if(typeof module==='object'&&module.exports)module.exports=factory();
  else root.CoverEditor=factory();
})(typeof window!=='undefined'?window:this,function(){
  'use strict';
  const clone=value=>JSON.parse(JSON.stringify(value));
  const esc=value=>String(value??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;').replaceAll("'",'&#039;');
  const overlap=(a,b)=>Math.max(0,2-Math.abs(a.x-b.x))*Math.max(0,2-Math.abs(a.y-b.y));
  function boardPoint(x,y,rect){return {x:(x-rect.left)/rect.width*18-1,y:(y-rect.top)/rect.height*20-1};}
  function point(x,y,rect){const p=boardPoint(x,y,rect);return {x:Math.round(p.x),y:Math.round(p.y)};}
  function anchor(p){return {left:(p.x+1)/18*100,top:(p.y+1)/20*100};}
  function projection(board,tile){
    const tower=(board.towers||[]).find(t=>t.index===tile.tower_index||(t.x===tile.x&&t.y===tile.y));
    const vector=tower?[[0,-1],[-1,0],[0,1],[1,0]][tower.direction]||[0,0]:[0,0];
    return {x:tile.x+vector[0]*16/85*tile.z,y:tile.y+vector[1]*16/85*tile.z};
  }
  function footprint(board,tile){
    const tower=(board.towers||[]).find(t=>t.index===tile.tower_index||(t.x===tile.x&&t.y===tile.y));
    const vector=tower?[[0,-1],[-1,0],[0,1],[1,0]][tower.direction]||[0,0]:[0,0];
    return {x:tile.x*85+vector[0]*16*tile.z,y:tile.y*85+vector[1]*16*tile.z};
  }
  function supportsTile(board,tile){
    const p=footprint(board,tile);
    return (board.tiles||[]).filter(below=>{const q=footprint(board,below);return below.z===tile.z-1&&Math.abs(p.x-q.x)<170&&Math.abs(p.y-q.y)<170;});
  }
  function unsupportedTiles(board){return (board.tiles||[]).filter(tile=>tile.z>0&&!supportsTile(board,tile).length);}
  function visibleTiles(board,layer=0,only=false){
    return (board.tiles||[]).filter(t=>!layer||(only?t.z===layer-1:t.z<layer)).slice().sort((a,b)=>a.z-b.z||a.y-b.y||a.index-b.index);
  }
  function isCovered(board,tile,tiles){const p=projection(board,tile);return tiles.some(t=>t.z>tile.z&&overlap(projection(board,t),p)>0);}
  function thumbnailMarkup(board){
    if(!board)return '<span class="thumbnail-empty">未生成</span>';
    const tiles=visibleTiles(board);
    return `<svg class="result-thumbnail-image" viewBox="0 0 18 20" aria-hidden="true">${tiles.map(tile=>{const p=projection(board,tile);return `<rect class="thumbnail-tile${isCovered(board,tile,tiles)?' is-covered':''}${tile.tile_id===-11?' is-b':''}" x="${p.x}" y="${p.y}" width="2" height="2" rx=".12"/>`;}).join('')}</svg>`;
  }
  function placement(board,x,y,index=null){
    const clicked=(board.tiles||[]).find(t=>t.index===index);if(clicked?.tower_index!=null){x=clicked.x;y=clicked.y;}
    const tiles=(board.tiles||[]).map(t=>({...projection(board,t),z:t.z}));
    let z=0;
    while(true){
      const footprint=projection(board,{x,y,z});
      const next=tiles.filter(t=>overlap(t,footprint)>0).reduce((highest,t)=>Math.max(highest,t.z+1),z);
      if(next===z)return {x,y,z};
      z=next;
    }
  }
  function hit(board,x,y,layer=0){
    return visibleTiles(board,layer,false).filter(t=>{
      if(layer&&t.z!==layer-1)return false;
      const p=projection(board,t);
      return x>=p.x-1&&x<p.x+1&&y>=p.y-1&&y<p.y+1;
    }).at(-1);
  }
  function target(board,action,x,y,layer=0,index=null){
    if(action==='add')return layer?{x,y,z:layer-1}:placement(board,x,y,index);
    const clicked=(board.tiles||[]).find(t=>t.index===index&&(!layer||t.z===layer-1));
    return clicked||hit(board,x,y,layer);
  }
  function cursorPlacement(board,action,x,y,layer=0,index=null,raw=null){
    const tile=target(board,action,action==='delete'&&raw?raw.x:x,action==='delete'&&raw?raw.y:y,layer,index);
    return tile?{...projection(board,tile),z:tile.z}:{x,y,z:null};
  }
  function create(board){return {tiles:clone(board.tiles||[]),towers:clone(board.towers||[]),translation:clone(board.translation||{dx:0,dy:0}),quarterTurns:0,history:[]};}
  function snapshot(draft){const {history,...state}=draft;return clone(state);}
  function changed(draft,tiles,towers=draft.towers,extra={}){return {...draft,tiles,towers:clone(towers||[]),...extra,history:[...draft.history,snapshot(draft)]};}
  function refreshTowers(tiles,towers){return (towers||[]).map(tower=>({...tower,count:tiles.filter(t=>t.x===tower.x&&t.y===tower.y).length}));}
  function apply(draft,op){
    const tiles=clone(draft.tiles);
    if(op.action==='add'){
      if(![op.x,op.y,op.z].every(Number.isSafeInteger)||![-10,-11].includes(op.tile_id))return draft;
      tiles.push({index:Math.min(0,...tiles.map(t=>t.index))-1,source_index:null,x:op.x,y:op.y,z:op.z,tile_id:op.tile_id,tower_index:(draft.towers||[]).find(t=>t.x===op.x&&t.y===op.y)?.index??null,added:true});
    }else if(op.action==='delete'){
      const at=tiles.findIndex(t=>t.index===op.index);
      if(at<0)return draft;
      tiles.splice(at,1);
    }else return draft;
    return changed(draft,tiles,refreshTowers(tiles,draft.towers));
  }
  function undo(draft){if(!draft.history.length)return draft;const saved=clone(draft.history.at(-1));return {...draft,...(Array.isArray(saved)?{tiles:saved}:saved),history:draft.history.slice(0,-1)};}
  function translate(draft,dx,dy){
    if(![dx,dy].every(Number.isSafeInteger)||!dx&&!dy)return draft;
    const move=t=>({...t,x:t.x+dx,y:t.y+dy}),offset=draft.translation||{dx:0,dy:0};
    return changed(draft,draft.tiles.map(t=>({...move(t),changed:true})),(draft.towers||[]).map(move),{translation:{dx:offset.dx+dx,dy:offset.dy+dy}});
  }
  function rotate(draft,direction){
    if(!['cw','ccw'].includes(direction))return draft;const q=direction==='cw'?1:3;
    const turn=t=>{t=clone(t);for(let i=0;i<q;i++){const x=t.x;t.x=17-t.y;t.y=x+1;}return t;};
    let {dx,dy}=draft.translation||{dx:0,dy:0};for(let i=0;i<q;i++){const x=dx;dx=-dy;dy=x;}
    return changed(draft,draft.tiles.map(t=>({...turn(t),changed:true})),(draft.towers||[]).map(t=>({...turn(t),direction:(t.direction-q+4)%4})),{translation:{dx,dy},quarterTurns:((draft.quarterTurns||0)+q)%4});
  }
  function clearLayer(draft,layer,mode){
    if(!Number.isSafeInteger(layer)||layer<1||!['layer','above'].includes(mode))return draft;
    const tiles=draft.tiles.filter(t=>!(mode==='layer'?t.z===layer-1:t.z>layer-1));
    return tiles.length===draft.tiles.length?draft:changed(draft,tiles,refreshTowers(tiles,draft.towers));
  }
  function scanIssues(board){
    const groups=new Map(),tiles=board.tiles||[];
    const towerAt=t=>(board.towers||[]).some(s=>s.x===t.x&&s.y===t.y);
    for(const tile of tiles){if(towerAt(tile))continue;const key=`${tile.x}:${tile.y}`;if(!groups.has(key))groups.set(key,[]);groups.get(key).push(tile);}
    const projected=tiles.map(t=>({...projection(board,t),z:t.z})),issues=[];
    for(const group of groups.values()){
      const layers=new Map();for(const tile of group){if(!layers.has(tile.z))layers.set(tile.z,[]);layers.get(tile.z).push(tile);}
      const levels=[...layers.keys()].sort((a,b)=>a-b);
      for(let i=1;i<levels.length;i++){
        for(const lower of layers.get(levels[i-1]))for(const upper of layers.get(levels[i])){
          const gap=upper.z-lower.z;
          if(gap>1&&projected.some(t=>t.z>lower.z&&t.z<upper.z&&overlap(t,lower)>0))continue;
          issues.push({kind:gap===1?'adjacent':'floating',x:lower.x,y:lower.y,lower_layer:lower.z+1,upper_layer:upper.z+1,lower_index:lower.index,upper_index:upper.index});
        }
      }
    }
    return issues;
  }
  function issueIndices(board){return new Set((board.issues||scanIssues(board)).flatMap(i=>[i.lower_index,i.upper_index]));}
  function issueLayer(issue,turn){return turn%2?issue.upper_layer:issue.lower_layer;}
  function sameTiles(a,b){return a.length===b.length&&a.every((t,i)=>['x','y','z','tile_id'].every(k=>t[k]===b[i][k]));}
  const icons={close:'<path d="m6 6 12 12M18 6 6 18"/>',undo:'<path d="M9 5 4 10l5 5M4 10h10a6 6 0 0 1 0 12"/>',minus:'<path d="M5 12h14"/>',plus:'<path d="M5 12h14M12 5v14"/>',up:'<path d="M12 19V5m-6 6 6-6 6 6"/>',down:'<path d="M12 5v14m-6-6 6 6 6-6"/>',left:'<path d="M19 12H5m6-6-6 6 6 6"/>',right:'<path d="M5 12h14m-6-6 6 6-6 6"/>',reset:'<path d="M4 10a8 8 0 1 1 1 8M4 4v6h6"/>',cw:'<path d="M20 10a8 8 0 1 0-1 8M20 4v6h-6"/>',ccw:'<path d="M4 10a8 8 0 1 1 1 8M4 4v6h6"/>',coordinate:'<path d="M5 5h14v14H5zM9 2v6m6-6v6M2 9h6m-6 6h6m8-6h6m-6 6h6"/>'};
  const icon=name=>`<svg viewBox="0 0 24 24" aria-hidden="true">${icons[name]}</svg>`;
  let sequence=0,active=null;
  function open(options){
    if(typeof document==='undefined')throw Error('编辑器需要在浏览器中打开。');
    if(active){active.element.querySelector('[data-close-editor]').focus();return active;}
    let current=clone(options.board||{tiles:[],towers:[]});
    let original=clone(options.original||current.original_board||current);
    let draft=create(current),editing=false,pending=false,layer=0,only=false,brush=-10,action='add',cursor={x:2,y:2},cursorVisible=false,cursorRight=false,cursorHit={index:null,raw:null};
    let focused=null,issueTurns=new Map(),locateTurns=new Map(),message='',error=false,discardMode='close',disposed=false,markChanges=true,clearRequest=null;
    const trigger=document.activeElement,uid=`cover-editor-${++sequence}`;
    const dialog=document.createElement('dialog');dialog.className='cover-editor-dialog';dialog.setAttribute('aria-labelledby',uid+'-title');
    dialog.innerHTML=`<header class="cc-dialog-header"><h2 id="${uid}-title"></h2><button type="button" class="icon-button" data-close-editor aria-label="关闭模板对比" title="关闭">${icon('close')}</button></header>
      <div class="cc-editor-body"><p class="cc-summary"></p>
      <div class="cc-toolbar"><div class="cc-layer-control" role="group" aria-label="查看层控制"><label for="${uid}-layer" class="cc-layer-prefix">层级</label><button type="button" class="secondary-button cc-unlimited" data-layer-unlimited aria-pressed="true" title="显示全部层，增删不限层，新增自动落层">无限制</button><button type="button" class="icon-button" data-layer-minus aria-label="减少一层">${icon('minus')}</button><input id="${uid}-layer" type="range" data-layer-range data-layer min="0" step="1" value="0" aria-label="查看与编辑层，0为无限制"><button type="button" class="icon-button" data-layer-plus aria-label="增加一层">${icon('plus')}</button><output data-layer-label for="${uid}-layer">全部层</output><label class="cc-checkbox"><input type="checkbox" data-only>仅看当前层</label><button type="button" class="secondary-button cc-layer-delete" data-clear-layer>删除本层</button><button type="button" class="secondary-button cc-layer-delete" data-clear-above>删除本层以上</button></div><label class="cc-checkbox cc-mark-changes"><input type="checkbox" data-mark-changes checked>标记挪动</label></div><p class="cc-preview-feedback"></p>
      <div class="cc-edit-toolbar"><div class="cc-transform-actions" role="group" aria-label="整体平移新模板"><span>平移</span>${['up','down','left','right','reset'].map((key,i)=>`<button type="button" class="icon-button" data-translate="${key}" aria-label="${['新模板整体上移一格','新模板整体下移一格','新模板整体左移一格','新模板整体右移一格','复位整体平移'][i]}" title="${key==='reset'?'复位整体平移':'每次移动 1 格'}">${icon(key)}</button>`).join('')}</div><div class="cc-rotation-actions" role="group" aria-label="整体旋转新模板"><span>旋转</span>${['ccw','cw'].map(key=>`<button type="button" class="icon-button" data-rotate="${key}" aria-label="新模板整体${key==='cw'?'顺':'逆'}时针旋转90度" title="${key==='cw'?'顺':'逆'}时针90° · 绕画布中心">${icon(key)}</button>`).join('')}</div><p class="cc-transform-status" role="status" aria-live="polite"></p><div class="cc-edit-tools" hidden><div class="cc-brushes" role="group" aria-label="新增 Tile 笔刷，括号内为补到3的倍数还差的张数">${[-10,-11].map((type,i)=>`<button type="button" class="cc-brush-choice" data-brush="${type}" aria-pressed="${i===0}"><span class="cc-brush-count" data-brush-count="${type}">0 (0)</span><span class="cc-brush-face" aria-hidden="true">${i?'B':'A'}</span></button>`).join('')}</div><label class="cc-checkbox"><input type="checkbox" data-delete>删除</label></div><details class="cc-coordinate" hidden><summary aria-label="按坐标增删" title="按坐标增删">${icon('coordinate')}</summary><div class="cc-coordinate-actions"><label for="${uid}-x">X</label><input id="${uid}-x" type="number" data-coordinate-x step="1" value="8"><label for="${uid}-y">Y</label><input id="${uid}-y" type="number" data-coordinate-y step="1" value="9"><button type="button" class="secondary-button" data-coordinate-add>增加 Tile</button><button type="button" class="secondary-button" data-coordinate-delete>删除 Tile</button></div></details><button type="button" class="icon-button cc-undo" data-undo aria-label="撤回上一次已保存修改" title="撤回上一次已保存修改">${icon('undo')}</button><button type="button" class="secondary-button cc-save" data-edit data-save aria-pressed="false">去编辑</button></div><div class="cc-manual-feedback"><p class="cc-counts"></p><details class="cc-status-details"><summary><span class="cc-feedback" role="status" aria-live="polite"></span></summary><p class="cc-status-detail"></p></details><span class="cc-locate-actions"><button type="button" class="row-action" data-locate-issue hidden>定位全遮盖</button><button type="button" class="row-action" data-locate-unsupported hidden>定位悬空</button></span></div>
      <p class="cc-instructions visually-hidden" id="${uid}-instructions"></p>
      <div class="cc-comparison"><section class="cc-side"><h3>原模板 <span data-original-name></span></h3><div data-original-board></div><p class="cc-board-summary" data-original-summary></p></section><section class="cc-side"><h3>新模板 <span data-current-name></span></h3><div data-current-board></div><p class="cc-board-summary" data-current-summary></p></section></div>
      <div class="cc-legend"><span><i class="cc-legend-uncovered"></i>完全无遮盖</span><span><i class="cc-legend-covered"></i>有遮盖</span><span><i class="cc-legend-issue"></i>红框全遮盖</span><span><i class="cc-legend-unsupported"></i>蓝框悬空</span></div><details class="cc-moves"><summary>查看挪动坐标</summary><div class="cc-move-table"><table><thead><tr><th>Tile 序号</th><th>类型</th><th>原坐标 (X,Y,Z)</th><th>新坐标 (X,Y,Z)</th></tr></thead><tbody data-moves-body></tbody></table></div></details><details class="cc-problems" open><summary>问题坐标 <span data-issue-count></span></summary><p>点击坐标在问题牌的上下层之间切换，两侧同步定位。</p><div class="cc-issue-list"></div></details></div>`;
    const discard=document.createElement('dialog');discard.className='cc-discard-dialog';discard.setAttribute('aria-labelledby',uid+'-discard-title');discard.setAttribute('aria-describedby',uid+'-discard-help');
    discard.innerHTML=`<div class="cc-dialog-header"><h2 id="${uid}-discard-title">保存本次微调吗？</h2><button type="button" class="icon-button" data-keep-close aria-label="返回继续编辑">${icon('close')}</button></div><p id="${uid}-discard-help" data-discard-feedback role="status" aria-live="polite">本次增删和平移旋转尚未保存。</p><div class="cc-discard-actions"><button type="button" class="row-action" data-keep>继续编辑</button><button type="button" class="secondary-button" data-discard>关闭不保存</button><button type="button" class="primary-button" data-save-close>保存后关闭</button></div>`;
    const clearDialog=document.createElement('dialog');clearDialog.className='cc-discard-dialog';clearDialog.setAttribute('aria-labelledby',uid+'-clear-title');clearDialog.setAttribute('aria-describedby',uid+'-clear-description');
    clearDialog.innerHTML=`<h2 id="${uid}-clear-title" data-clear-title></h2><p id="${uid}-clear-description" data-clear-description></p><div class="cc-discard-actions"><button type="button" class="secondary-button" data-clear-cancel>取消</button><button type="button" class="secondary-button cc-discard" data-clear-confirm></button></div>`;
    document.body.append(dialog,discard,clearDialog);
    const $=selector=>dialog.querySelector(selector);
    function maximumLayer(){return Math.max(1,...[...(original.tiles||[]),...draft.tiles].map(t=>t.z+1))+(editing?1:0);}
    function workingBoard(){const board={...current,tiles:draft.tiles,towers:draft.towers};return {...board,issues:editing?scanIssues(board):(current.issues||scanIssues(board))};}
    function dirty(){return draft.history.length>0||!sameTiles(draft.tiles,current.tiles||[])||JSON.stringify(draft.towers)!==JSON.stringify(current.towers||[]);}
    function problemTargets(category='cover'){
      const targets=new Map(),add=(point,label)=>{
        const key=`${point.x}:${point.y}:${point.z}`;
        if(targets.has(key)){if(!targets.get(key).labels.includes(label))targets.get(key).labels.push(label);}
        else targets.set(key,{...point,labels:[label]});
      };
      if(category==='unsupported')for(const [label,board] of [['原模板',original],['新模板',workingBoard()]])for(const tile of unsupportedTiles(board))add(tile,label);
      else for(const {issue,labels} of issueEntries())for(const z of [issue.lower_layer-1,issue.upper_layer-1])for(const label of labels)add({x:issue.x,y:issue.y,z},label);
      return [...targets.values()];
    }
    function notify(text,isError=false){message=text;error=isError;const hint=layer?`增删仅在第 ${layer} 层进行；左键新增，右键删除。`:'显示全部层；左键在落点最高牌上方新增，右键删除点中的牌。';$('.cc-feedback').textContent=message||hint;$('.cc-feedback').classList.toggle('is-error',error);$('.cc-feedback').setAttribute('role',error?'alert':'status');$('.cc-status-detail').textContent=message?[message,hint].join(' '):hint;$('.cc-manual-feedback').hidden=!editing&&!message&&!problemTargets().length&&!problemTargets('unsupported').length;}
    function updateControls(){
      const max=maximumLayer();layer=Math.min(max,layer);if(!layer)only=false;
      $('[data-layer-range]').max=max;$('[data-layer-range]').value=layer;$('[data-layer-label]').textContent=layer?`${layer} 层`:'全部层';
      $('[data-layer-range]').setAttribute('aria-valuetext',layer?`第 ${layer} 层增删，${only?'仅看当前层':'显示第 1 层至当前层'}`:'无限制，显示全部层，自动落层');
      $('[data-layer-unlimited]').disabled=pending;$('[data-layer-unlimited]').setAttribute('aria-pressed',String(!layer));$('[data-layer-minus]').disabled=pending||!layer;$('[data-layer-plus]').disabled=pending||layer>=max;
      $('[data-only]').disabled=pending||!layer;$('[data-only]').checked=only;
      $('[data-edit]').textContent=editing?dirty()?'保存编辑':'退出编辑':'去编辑';$('[data-edit]').disabled=pending;$('[data-edit]').setAttribute('aria-pressed',String(editing));$('[data-edit]').classList.toggle('is-save',editing&&dirty());
      $('.cc-edit-tools').hidden=!editing;$('.cc-coordinate').hidden=!editing;$('[data-save]').disabled=pending;$('[data-save]').setAttribute('aria-busy',String(pending));
      $('[data-undo]').disabled=pending||!(editing?draft.history.length:current.can_undo&&options.onUndo);$('[data-undo]').title=editing?'撤回上一步编辑':'撤回上一次已保存修改';$('[data-undo]').setAttribute('aria-label',$('[data-undo]').title);
      $('[data-close-editor]').disabled=pending;
      for(const [category,selector,label] of [['cover','[data-locate-issue]','全遮盖'],['unsupported','[data-locate-unsupported]','悬空']]){const problems=problemTargets(category),locate=$(selector);locate.hidden=!problems.length;locate.disabled=pending;locate.textContent=`定位${label}（${problems.length}）`;}
      for(const control of dialog.querySelectorAll('[data-brush],[data-delete],[data-layer],[data-layer-range],[data-translate],[data-rotate],[data-coordinate-x],[data-coordinate-y],[data-coordinate-add],[data-coordinate-delete]'))control.disabled=pending;
      const offset=draft.translation||{dx:0,dy:0};$('[data-translate="reset"]').disabled=pending||!offset.dx&&!offset.dy;$('.cc-transform-status').textContent=`X ${offset.dx} / Y ${offset.dy}${pending?' · 保存中…':editing?draft.quarterTurns?` · ${draft.quarterTurns*90}°`:'':' · 自动保存'}`;
      for(const [selector,mode] of [['[data-clear-layer]','layer'],['[data-clear-above]','above']]){const count=layer?draft.tiles.filter(t=>mode==='layer'?t.z===layer-1:t.z>layer-1).length:0;$(selector).disabled=pending||!count;$(selector).title=layer?`${mode==='above'?'只删除更高层，不含本层':'只删除本层'} · ${count} 张 · 可撤回，保存后生效`:'先选择具体层';}
      for(const button of dialog.querySelectorAll('[data-brush]'))button.setAttribute('aria-pressed',String(action==='add'&&brush===Number(button.dataset.brush)));
      $('[data-delete]').checked=action==='delete';
      for(const type of [-10,-11]){const total=draft.tiles.filter(t=>t.tile_id===type).length,missing=(3-total%3)%3,count=$(`[data-brush-count="${type}"]`);count.textContent=`${total} (${missing})`;count.classList.toggle('is-incomplete',missing!==0);$(`[data-brush="${type}"]`).setAttribute('aria-label',`${type===-10?'A':'B'} Tile 笔刷，共 ${total} 张，距离3的倍数还差 ${missing} 张`);}
      $('.cc-counts').hidden=!editing;$('.cc-counts').textContent=`原模板 / 新模板 Tile 总数：${original.tiles?.length||0} / ${draft.tiles.length} · ${layer?'第 '+layer+' 层增删':'无限制 · 自动落层'}${dirty()?' · 未保存':''}`;
      $('.cc-instructions').textContent=editing?`点击右侧画板${action==='delete'?'删除牌':'添加'+(brush===-10?' A':' B')+'牌'}。${layer?'仅操作第 '+layer+' 层。':'无限制时新增牌自动落在覆盖范围的最高层之上。'}可自由增删；全遮盖和悬空仅提示，不影响保存与导出。方向键移动锚点，Enter 或空格执行。`:'左右画板尺度一致；选择层级后同步查看，红框标出全遮盖，蓝框标出悬空。';
      dialog.classList.toggle('cc-is-editing',editing);
    }
    function boardMarkup(board,side){
      const tiles=visibleTiles(board,layer,only),indices=issueIndices(board),unsupported=new Set(unsupportedTiles(board).map(t=>t.index)),movedIndices=new Set(movedTiles().map(t=>t.index)),sideLabel=side==='current'?'新模板':'原模板';
      const points=[];for(let y=0;y<19;y++)for(let x=0;x<17;x++)points.push(`<circle cx="${x+1}" cy="${y+1}" r=".035" class="cc-dot-${x%2===y%2?'red':'blue'}"/>`);
      const grid=`<svg class="cc-grid" viewBox="0 0 18 20" aria-hidden="true"><rect x="2.5" y="2.5" width="13" height="15" class="cc-frame"/><path d="M9 0V20M0 10H18" class="cc-axis"/>${points.join('')}</svg>`;
      return `<div class="cc-board" data-board="${side}" ${side==='current'?'tabindex="0" aria-describedby="'+uid+'-instructions"':''} role="group" aria-label="${side==='original'?'原模板':'新模板'}坐标画板">${grid}${tiles.map(t=>{
        const projected=projection(board,t),pos=anchor(projected),covered=isCovered(board,t,tiles),context=layer&&!only&&t.z!==layer-1;
        const isFocused=focused&&(!focused.labels||focused.labels.includes(sideLabel))&&t.x===focused.x&&t.y===focused.y&&t.z===focused.z;
        const label=t.tile_id===-10?'A':t.tile_id===-11?'B':String(t.tile_id);
        const supports=supportsTile(board,t),support=(t.tower_index!=null?' · 塔内':'')+(t.z<=0?' · 位于底层':supports.length?` · 下层承接：${supports.map(p=>{const area=overlap(projected,projection(board,p));return `Tile ${p.index}（${({1:'1/4压盖',2:'1/2压盖',4:'完全压盖'})[area]||'存在压盖'}）`;}).join('、')}`:' · 悬空：紧邻下层无承接');
        const addition=side==='current'&&isAddition(t),detail=`Tile ${t.index} · 第 ${t.z+1} 层 · (${t.x}, ${t.y}, ${t.z})${addition?' · 新增':side==='current'&&movedIndices.has(t.index)?' · 已挪动':''}${support}${indices.has(t.index)?' · 全遮盖问题牌':''}${context?' · 下层参考':''}${covered?' · 有遮盖':' · 完全无遮盖'}`;
        const editable=editing&&side==='current',tag=editable?'button':'span';
        return `<${tag} ${editable?'type="button"':'role="img"'} tabindex="-1" class="cc-tile${covered?' is-covered':''}${context?' is-context':''}${indices.has(t.index)?' is-issue':''}${unsupported.has(t.index)?' is-unsupported':''}${markChanges&&side==='current'&&!context?(addition?' is-added':movedIndices.has(t.index)?' is-moved':''):''}${isFocused?' is-located':''}" data-tile-index="${t.index}" style="left:${pos.left}%;top:${pos.top}%" title="${esc(detail)}" aria-label="${esc(detail)}"><span class="cc-tile-layer">${t.z+1}</span><span class="cc-tile-label">${esc(label)}</span></${tag}>`;
      }).join('')}${tiles.length?'':'<p class="cc-board-empty">这一层没有牌</p>'}${side==='current'?'<div class="cc-tile-cursor" hidden aria-hidden="true"></div>':''}</div>`;
    }
    function renderBoards(){
      const board=workingBoard();
      $('[data-original-board]').innerHTML=boardMarkup(original,'original');$('[data-current-board]').innerHTML=boardMarkup(board,'current');
      $('[data-original-summary]').textContent=`${original.tiles?.length||0} 张牌 · ${original.issues?.length??scanIssues(original).length} 对全遮盖 · ${unsupportedTiles(original).length} 张悬空`;
      $('[data-current-summary]').textContent=`${draft.tiles.length} 张牌 · ${board.issues.length} 对全遮盖 · ${unsupportedTiles(board).length} 张悬空${editing&&dirty()?' · 未保存':''}`;
      renderCursor();
    }
    function issueEntries(){
      const map=new Map();for(const [label,board] of [['原模板',original],['新模板',workingBoard()]])for(const issue of board.issues||scanIssues(board)){
        const key=`${issue.x}:${issue.y}:${issue.lower_layer}:${issue.upper_layer}`;
        if(map.has(key))map.get(key).labels.push(label);else map.set(key,{key,issue,labels:[label]});
      }return [...map.values()];
    }
    function renderIssues(){
      const entries=issueEntries();$('[data-issue-count]').textContent=`（${entries.length} 组）`;
      $('.cc-issue-list').innerHTML=entries.map(({key,issue,labels},index)=>`<button type="button" class="cc-issue-button" data-issue="${index}" data-issue-key="${key}" ${pending?'disabled':''}>(${issue.x}, ${issue.y}) · 第 ${issue.lower_layer}、${issue.upper_layer} 层<span>${issue.kind==='adjacent'?'邻层一压一':'隔层悬空'} · ${labels.join(' / ')}</span></button>`).join('')||'<p class="cc-no-issues">当前未发现全遮盖问题。</p>';
    }
    function sourceIdentity(tile){return Object.prototype.hasOwnProperty.call(tile,'source_index')?tile.source_index:tile.index;}
    function originalTile(tile){const identity=sourceIdentity(tile);return identity==null?null:(original.tiles||[]).find(p=>sourceIdentity(p)===identity);}
    function isAddition(tile){return !originalTile(tile);}
    function movedTiles(){return draft.tiles.filter(t=>{const source=originalTile(t);return source&&['x','y','z'].some(k=>source[k]!==t[k]);});}
    function renderMoves(){
      const moves=movedTiles(),coordinate=t=>`(${t.x}, ${t.y}, ${t.z})`;
      $('[data-moves-body]').innerHTML=moves.map(t=>`<tr><td>${t.index}</td><td>${t.tile_id===-10?'A':t.tile_id===-11?'B':esc(t.tile_id)}</td><td>${coordinate(originalTile(t))}</td><td>${coordinate(t)}</td></tr>`).join('')||'<tr><td colspan="4">暂无挪动 Tile。</td></tr>';
      const additions=draft.tiles.filter(isAddition).length,deleted=(original.tiles||[]).filter(t=>!draft.tiles.some(p=>sourceIdentity(p)!=null&&sourceIdentity(p)===sourceIdentity(t))).length;
      $('.cc-preview-feedback').textContent=`新增 ${additions} 张，删除 ${deleted} 张，挪动 ${moves.length} 张 Tile。`;
    }
    function render(){
      $('[id="'+uid+'-title"]').textContent='原模板与新模板对比';
      $('.cc-summary').textContent='相同编辑器坐标和比例，左右同步切换显示层。';$('[data-original-name]').textContent=original.template_id||original.name||'';$('[data-current-name]').textContent=current.template_id||current.name||'';
      updateControls();renderBoards();renderIssues();renderMoves();notify(message,error);
    }
    function renderCursor(){
      const element=$('.cc-tile-cursor');if(!element)return;element.hidden=!editing||!cursorVisible||pending;
      const operation=cursorRight?'delete':action,board={...current,tiles:draft.tiles,towers:draft.towers},tile=cursorPlacement(board,operation,cursor.x,cursor.y,layer,cursorHit.index,cursorHit.raw),pos=anchor(tile),z=tile.z;
      element.hidden=element.hidden||z==null;element.style.left=pos.left+'%';element.style.top=pos.top+'%';element.classList.toggle('is-delete',operation==='delete');element.classList.toggle('is-right-edge',pos.left>80);
      element.dataset.label=`${operation==='delete'?'删除':brush===-10?'A':'B'}${z==null?'':` · 第 ${z+1} 层`}`;element.textContent='';
    }
    function finishClose(){
      if(disposed)return;disposed=true;if(typeof window!=='undefined'){window.removeEventListener('beforeunload',beforeUnload);window.removeEventListener('resize',viewportChanged);}dialog.close();discard.remove();clearDialog.remove();dialog.remove();active=null;trigger?.focus?.({preventScroll:true});options.onClose?.();
    }
    function requestClose(mode='close'){
      if(pending)return;
      if(dirty()){discardMode=mode;discard.showModal();discard.querySelector('[data-keep]').focus();return;}
      if(mode==='exit'){editing=false;cursorVisible=false;render();}else finishClose();
    }
    function changeLayer(value){if(pending)return;layer=Math.min(maximumLayer(),Math.max(0,Math.round(Number(value)||0)));if(!layer)only=false;focused=null;render();}
    function updatePointer(event,board){
      const rect=board.getBoundingClientRect(),tile=event.target.closest('[data-tile-index]');
      cursor=point(event.clientX,event.clientY,rect);cursorVisible=true;cursorRight=Boolean(event.buttons&2)||event.type==='contextmenu';
      cursorHit={index:tile?Number(tile.dataset.tileIndex):null,raw:boardPoint(event.clientX,event.clientY,rect)};
    }
    function operate(index=null,raw=null,operation=action){
      if(!editing||pending)return;const board=workingBoard();
      const chosen=target(board,operation,operation==='delete'&&raw?raw.x:cursor.x,operation==='delete'&&raw?raw.y:cursor.y,layer,index);
      if(!chosen){notify(layer?'点击处在指定层没有牌。':'点击处没有牌。',true);return;}
      draft=apply(draft,operation==='add'?{action:'add',...chosen,tile_id:brush}:{action:'delete',index:chosen.index});focused=null;notify('');render();
    }
    function accept(result,next){
      if(!result||!Array.isArray(result.tiles))throw Error('未收到保存结果，请重新读取模板后重试。');
      current=clone(result);if(!current.translation&&next)current.translation=clone(next.translation);if(result.original_board)original=clone(result.original_board);draft=create(current);focused=null;issueTurns=new Map();locateTurns=new Map();
    }
    async function save(next=draft,automatic=false){
      if(pending||!automatic&&(!editing||!dirty()))return;pending=true;cursorVisible=false;notify('正在保存修改…');updateControls();renderCursor();renderIssues();
      try{
        const result=await options.onSave(clone(next.tiles),{towers:clone(next.towers),translation:clone(next.translation)});
        accept(result,next);editing=false;const cover=workingBoard().issues.length,unsupported=unsupportedTiles(current).length,warnings=[cover?`${cover} 对全遮盖`:'',unsupported?`${unsupported} 张悬空`:''].filter(Boolean);notify(warnings.length?`已保存；仍有${warnings.join('、')}，仅提示，不影响导出。`:'修改已保存。');return true;
      }catch(reason){notify(`${reason?.message||'保存失败'}${automatic?'；本次变换未保存。':'；修改仍保留，请检查后重试。'}`,true);return false;}finally{pending=false;if(!disposed)render();}
    }
    async function undoAction(){
      if(pending)return;
      if(editing){draft=undo(draft);focused=null;notify('已撤回上一步。');render();return;}
      if(!current.can_undo||!options.onUndo)return;pending=true;notify('正在撤回上一次已保存修改…');updateControls();
      try{accept(await options.onUndo());notify('已撤回并保存。');}catch(reason){notify(reason?.message||'撤回未保存，请重新打开对比核对当前结果。',true);}finally{pending=false;if(!disposed)render();}
    }
    function transform(kind,value){
      if(pending)return;let next;
      if(kind==='rotate')next=rotate(draft,value);
      else{const offsets={up:[0,-1],down:[0,1],left:[-1,0],right:[1,0],reset:[-draft.translation.dx,-draft.translation.dy]};if(!offsets[value])return;next=translate(draft,...offsets[value]);}
      if(next===draft)return;
      if(editing){draft=next;focused=null;notify('草稿已更新，尚未保存。');render();}else void save(next,true);
    }
    function requestClear(mode){
      if(pending||!layer)return;const count=draft.tiles.filter(t=>mode==='layer'?t.z===layer-1:t.z>layer-1).length;if(!count)return;
      clearRequest={layer,mode,count};clearDialog.querySelector('[data-clear-title]').textContent=mode==='layer'?`删除第 ${layer} 层？`:`删除第 ${layer} 层以上的牌？`;
      clearDialog.querySelector('[data-clear-description]').textContent=`将从新模板草稿中删除 ${count} 张 Tile${mode==='above'?'（不含所选层，只删更高层，含当前未显示的牌）':''}。原模板不变，可撤回；点击“保存编辑”后生效。`;
      clearDialog.querySelector('[data-clear-confirm]').textContent=`删除 ${count} 张 Tile`;clearDialog.showModal();clearDialog.querySelector('[data-clear-cancel]').focus();
    }
    dialog.addEventListener('cancel',event=>{event.preventDefault();requestClose();});
    dialog.addEventListener('click',event=>{
      if(event.target.closest('[data-close-editor]'))return requestClose();
      if(event.target.closest('[data-edit]')){if(pending)return;if(editing){if(dirty())void save();else requestClose('exit');}else{editing=true;draft=create(current);notify('');render();}return;}
      if(event.target.closest('[data-save]'))return void save();
      if(event.target.closest('[data-undo]'))return void undoAction();
      if(event.target.closest('[data-locate-issue]')||event.target.closest('[data-locate-unsupported]')){
        const category=event.target.closest('[data-locate-unsupported]')?'unsupported':'cover',targets=problemTargets(category);if(pending||!targets.length)return;
        const turn=locateTurns.get(category)||0,target=targets[turn%targets.length];locateTurns.set(category,turn+1);
        layer=target.z+1;focused={...target};notify(`已定位${category==='unsupported'?'悬空':'全遮盖'} · ${target.labels.join(' / ')} · 第 ${layer} 层 (${target.x}, ${target.y})；再次点击查看下一张${category==='unsupported'?'悬空':'全遮盖'}牌。`);render();
        const candidates=[...dialog.querySelectorAll('.cc-tile.is-located')],element=candidates.at(-1);element?.focus({preventScroll:true});element?.scrollIntoView({block:'center',inline:'nearest',behavior:'auto'});return;
      }
      if(event.target.closest('[data-layer-unlimited]'))return changeLayer(0);
      if(event.target.closest('[data-layer-minus]'))return changeLayer(layer-1);
      if(event.target.closest('[data-layer-plus]'))return changeLayer(layer+1);
      if(event.target.closest('[data-clear-layer]'))return requestClear('layer');
      if(event.target.closest('[data-clear-above]'))return requestClear('above');
      const translation=event.target.closest('[data-translate]');if(translation)return transform('translate',translation.dataset.translate);
      const rotation=event.target.closest('[data-rotate]');if(rotation)return transform('rotate',rotation.dataset.rotate);
      const coordinateAdd=event.target.closest('[data-coordinate-add]'),coordinateDelete=event.target.closest('[data-coordinate-delete]');
      if(coordinateAdd||coordinateDelete){const x=Number($('[data-coordinate-x]').value),y=Number($('[data-coordinate-y]').value);if(![x,y].every(Number.isSafeInteger)){notify('请输入有效整数坐标。',true);return;}cursor={x,y};cursorHit={index:null,raw:null};return operate(null,null,coordinateAdd?'add':'delete');}
      const brushButton=event.target.closest('[data-brush]');if(brushButton&&!pending){brush=Number(brushButton.dataset.brush);action='add';updateControls();renderCursor();return;}
      if(event.target.closest('[data-delete]')&&!pending){action=action==='delete'?'add':'delete';updateControls();renderCursor();return;}
      const problem=event.target.closest('[data-issue]');if(problem&&!pending){
        const entry=issueEntries()[Number(problem.dataset.issue)];if(!entry)return;const turn=issueTurns.get(entry.key)||0;layer=issueLayer(entry.issue,turn);issueTurns.set(entry.key,turn+1);focused={x:entry.issue.x,y:entry.issue.y,z:layer-1,labels:entry.labels};render();
        const candidates=[...dialog.querySelectorAll('.cc-tile.is-located')],focus=candidates.at(-1);focus?.focus({preventScroll:true});focus?.scrollIntoView({block:'nearest',inline:'nearest',behavior:'auto'});return;
      }
      const board=event.target.closest('[data-board="current"]');if(board&&editing&&!pending){
        updatePointer(event,board);operate(cursorHit.index,cursorHit.raw);
      }
    });
    dialog.addEventListener('contextmenu',event=>{
      const board=event.target.closest('[data-board="current"]');if(!board||!editing)return;
      event.preventDefault();if(pending)return;
      updatePointer(event,board);operate(cursorHit.index,cursorHit.raw,'delete');
    });
    dialog.addEventListener('change',event=>{if(event.target.matches('[data-layer]'))changeLayer(event.target.value);if(event.target.matches('[data-only]')){only=layer>0&&event.target.checked;render();}if(event.target.matches('[data-delete]')){action=event.target.checked?'delete':'add';updateControls();renderCursor();}if(event.target.matches('[data-mark-changes]')){markChanges=event.target.checked;renderBoards();}});
    dialog.addEventListener('input',event=>{if(event.target.matches('[data-layer-range]'))changeLayer(event.target.value);});
    function positionCoordinate(){
      if(!$('.cc-coordinate').open)return;const panel=$('.cc-coordinate-actions'),rect=$('.cc-coordinate>summary').getBoundingClientRect(),width=globalThis.innerWidth,height=globalThis.innerHeight;
      panel.style.top=width>760?Math.max(12,Math.min(rect.bottom,height-panel.getBoundingClientRect().height-12))+'px':'auto';panel.style.bottom=width>760?'auto':'24px';panel.style.right=width>760?Math.max(12,width-rect.right)+'px':'24px';
    }
    function beforeUnload(event){if(editing&&dirty()){event.preventDefault();event.returnValue='';}}
    function viewportChanged(){cursorVisible=false;renderCursor();positionCoordinate();}
    if(typeof window!=='undefined'){window.addEventListener('beforeunload',beforeUnload);window.addEventListener('resize',viewportChanged);}
    dialog.addEventListener('toggle',event=>{if(event.target.matches('.cc-coordinate'))positionCoordinate();},true);
    dialog.addEventListener('scroll',()=>{cursorVisible=false;renderCursor();positionCoordinate();},true);
    dialog.addEventListener('pointermove',event=>{const board=event.target.closest('[data-board="current"]');if(!board||!editing||pending)return;updatePointer(event,board);renderCursor();});
    dialog.addEventListener('pointerout',event=>{const board=event.target.closest('[data-board="current"]');if(board&&!board.contains(event.relatedTarget)){cursorVisible=false;renderCursor();}});
    dialog.addEventListener('keydown',event=>{
      const board=event.target.closest('[data-board="current"]');if(!board||!editing||pending||event.isComposing)return;
      const deltas={ArrowLeft:[-1,0],ArrowRight:[1,0],ArrowUp:[0,-1],ArrowDown:[0,1]};
      if(deltas[event.key]){event.preventDefault();cursor={x:cursor.x+deltas[event.key][0],y:cursor.y+deltas[event.key][1]};cursorHit={index:null,raw:null};cursorVisible=true;renderCursor();}
      else if(event.key==='Enter'||event.key===' '){event.preventDefault();cursorVisible=true;operate(cursorHit.index,cursorHit.raw);$('[data-board="current"]').focus({preventScroll:true});}
    });
    discard.querySelector('[data-keep]').addEventListener('click',()=>{discard.close();$('[data-edit]').focus();});
    discard.querySelector('[data-keep-close]').addEventListener('click',()=>{if(!pending){discard.close();$('[data-edit]').focus();}});
    discard.querySelector('[data-save-close]').addEventListener('click',async()=>{if(pending)return;for(const selector of ['[data-keep]','[data-keep-close]','[data-discard]','[data-save-close]'])discard.querySelector(selector).disabled=true;try{if(await save()){discard.close();finishClose();}else discard.querySelector('[data-discard-feedback]').textContent=message;}finally{for(const selector of ['[data-keep]','[data-keep-close]','[data-discard]','[data-save-close]'])discard.querySelector(selector).disabled=false;}});
    discard.querySelector('[data-discard]').addEventListener('click',()=>{discard.close();draft=create(current);focused=null;if(discardMode==='exit'){editing=false;cursorVisible=false;notify('未保存的修改已放弃。');render();$('[data-edit]').focus();}else finishClose();});
    discard.addEventListener('cancel',event=>{if(pending)event.preventDefault();else setTimeout(()=>$('[data-edit]')?.focus(),0);});
    clearDialog.querySelector('[data-clear-cancel]').addEventListener('click',()=>{clearDialog.close();clearRequest=null;$('[data-edit]').focus();});
    clearDialog.querySelector('[data-clear-confirm]').addEventListener('click',()=>{if(pending||!clearRequest)return;draft=clearLayer(draft,clearRequest.layer,clearRequest.mode);editing=true;focused=null;notify(`已删除 ${clearRequest.count} 张 Tile，可撤回；尚未保存。`);clearRequest=null;clearDialog.close();render();$('[data-edit]').focus();});
    clearDialog.addEventListener('cancel',()=>{clearRequest=null;});
    render();dialog.showModal();$('[data-close-editor]').focus();
    active={element:dialog,close:()=>requestClose(),getBoard:()=>clone(workingBoard())};return active;
  }
  return {open,thumbnailMarkup,boardPoint,point,anchor,overlap,projection,visibleTiles,placement,hit,target,cursorPlacement,create,apply,undo,translate,rotate,clearLayer,scanIssues,issueIndices,issueLayer,unsupportedTiles};
});
