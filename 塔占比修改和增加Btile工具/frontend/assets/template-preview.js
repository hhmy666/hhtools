(function(root,factory){
  if(typeof module==='object' && module.exports) module.exports=factory();
  else root.TowerTemplatePreview=factory();
})(typeof window!=='undefined' ? window : this,function(){
  'use strict';
  const WIDTH=17, HEIGHT=19, SHIFT=16/85;
  const escape=value=>String(value ?? '').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;').replaceAll("'",'&#039;');
  function projection(board,t){
    const tower=(board?.towers||[]).find(s=>s.index===t.tower_index);
    const vector=tower?[[0,-1],[-1,0],[0,1],[1,0]][tower.direction]||[0,0]:[0,0];
    return {projectedX:t.x+vector[0]*SHIFT*t.z,projectedY:t.y+vector[1]*SHIFT*t.z};
  }
  function footprint(board,t){
    const p=projection(board,t);
    return {left:(p.projectedX+1)/18*100,top:(p.projectedY+1)/20*100,width:100/9,height:10};
  }
  function editorGrid(){
    const points=[];
    for(let y=0;y<HEIGHT;y++) for(let x=0;x<WIDTH;x++) points.push({x,y,color:x%2===y%2?'red':'blue'});
    return points;
  }
  function boardView(board,options={}){
    const issues=new Map((options.issues||[]).map(item=>[item.index,item.message]));
    for(const w of board?.geometry_warnings||[]){
      const lower=board.tiles.find(t=>t.index===w.lower_index),upper=board.tiles.find(t=>t.index===w.upper_index);
      if(w.code!=='inherited_full_cover'||!lower||!upper||lower.x!==upper.x||lower.y!==upper.y||upper.z!==lower.z+1)continue;
      const message=`原模板一压一：第${lower.z+1}、${upper.z+1}层(${upper.x}, ${upper.y})，红框定位原有问题。`;
      for(const t of [lower,upper])if(!issues.has(t.index))issues.set(t.index,message);
    }
    const layer=Number(options.layer), mode=options.mode || 'all';
    const tiles=(board?.tiles || []).filter(t=>mode==='single' ? t.z+1===layer : mode==='through' ? t.z+1<=layer : true);
    const cellTop=new Map();
    for(const t of tiles) for(const dx of [0,1]) for(const dy of [0,1]){
      const key=`${t.x+dx}:${t.y+dy}`;
      cellTop.set(key,Math.max(cellTop.get(key) ?? -Infinity,t.z));
    }
    const projected=tiles.map(t=>{
      const covered=[0,1].some(dx=>[0,1].some(dy=>cellTop.get(`${t.x+dx}:${t.y+dy}`)>t.z));
      const context=Boolean(options.editLayer && mode!=='all' && t.z+1!==layer);
      return {...t,context,issue:issues.get(t.index)||'',highlighted:!covered,...projection(board,t)};
    }).sort((a,b)=>a.z-b.z || a.y-b.y || a.index-b.index);
    return {tiles:projected,highlighted:projected.filter(t=>t.highlighted).length,changed:projected.filter(t=>t.changed).length,added:projected.filter(t=>t.added).length};
  }
  function boardMarkup(board,options={}){
    if(!board) return '<div class="preview-empty">没有新模板；原模板未修改。</div>';
    const view=boardView(board,options);
    const points=editorGrid().map(p=>`<circle cx="${p.x+1}" cy="${p.y+1}" r="0.035" class="editor-dot-${p.color}"/>`).join('');
    const grid=`<svg class="editor-grid" viewBox="0 0 18 20" aria-hidden="true"><rect x="2.5" y="2.5" width="13" height="15" class="editor-frame"/><path d="M 9 0 V 20 M 0 10 H 18" class="editor-axis"/>${points}</svg>`;
    const tiles=view.tiles.map(t=>{
      const moved=t.changed && options.markChanges!==false && !t.context;
      const label=t.tile_id===-10?'A':t.tile_id===-11?'B':String(t.tile_id);
      const support=t.tower_index!=null?'':t.z===0?' · 位于底层':Array.isArray(t.support_indices)
        ? t.support_indices.length ? ` · 下层承接：${t.support_indices.map((index,i)=>`Tile ${index}（${({1:'1/4压盖',2:'1/2压盖',4:'完全压盖'})[t.support_areas?.[i]] || '存在压盖'}）`).join('、')}`
          : ' · 悬空：紧邻下层无承接'
        : '';
      const detail=`Tile ${t.index} · 第 ${t.z+1} 层 · (${t.x}, ${t.y}, ${t.z})${t.tower_index!=null?' · 塔内':''}${t.added?' · 新增':t.changed?' · 已挪动':''}${t.context?' · 下层参考':''}${t.highlighted?' · 完全无遮盖':' · 有遮盖'}${support}${t.issue?` · 问题：${t.issue}`:''}`;
      const classes=`editor-tile${t.highlighted?' is-highlighted':' is-covered'}${t.context?' is-context':''}${moved?' is-moved':''}${t.added&&options.markChanges!==false&&!t.context?' is-added':''}${t.tower_index!=null?' is-tower':''}${t.issue?' is-invalid':''}`;
      const interactive=options.interactive&&t.tower_index!=null;
      const tag=interactive?'button':'span';
      return `<${tag} ${interactive?`type="button" data-tower-index="${t.tower_index}"`:'role="img"'} class="${classes}" data-tile-index="${t.index}" data-highlighted="${t.highlighted}" data-changed="${t.changed}" data-added="${Boolean(t.added)}" style="left:${(t.projectedX+1)/18*100}%;top:${(t.projectedY+1)/20*100}%" title="${escape(detail)}" aria-label="${escape(detail)}"><span class="editor-tile-layer">${t.z+1}</span><span class="editor-tile-label">${escape(label)}</span></${tag}>`;
    }).join('');
    return `<div class="editor-board" aria-label="${escape(board.template_id)} 编辑器坐标预览">${grid}${tiles}${view.tiles.length?'':'<p class="preview-board-empty">这一层没有 Tile</p>'}</div>`;
  }
  function thumbnailMarkup(board){
    if(!board)return '<span class="thumbnail-empty">未生成</span>';
    return `<svg class="result-thumbnail-image" viewBox="0 0 18 20" aria-hidden="true">${boardView(board).tiles.map(t=>`<rect class="thumbnail-tile${t.highlighted?'':' is-covered'}${t.tile_id===-11?' is-b':''}" x="${t.projectedX}" y="${t.projectedY}" width="2" height="2" rx=".12"/>`).join('')}</svg>`;
  }
  return {editorGrid,boardView,boardMarkup,thumbnailMarkup,footprint};
});
