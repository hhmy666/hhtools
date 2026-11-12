(function(root,factory) {
  const api=factory();
  if(typeof module==='object' && module.exports) module.exports=api;
  else root.TowerIncrease=api;
})(typeof window!=='undefined'?window:globalThis,function() {
  'use strict';
  const clone=value=>structuredClone(value);
  function normalizeOptions(options) {
    const normalized=clone(options);
    if(normalized.stack_count===0)normalized.stack_count=null;
    return normalized;
  }
  const esc=value=>String(value??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;').replaceAll("'",'&#039;');
  function defaults() { return {target_mode:'ratio',added_tiles:30,total_multiplier:1.5,min_ratio:.2,max_ratio:.25,
    operation:'new',stack_count:null,regions:['auto'],fill_type:'auto',direction:0,spread:'compact',spacing:0,formation_gap:0,
    translate_x:0,translate_y:0,auto_translate:true,height_mode:'difference',tower_counts:[],tower_positions:[],tower_directions:[],locked_towers:[]}; }
  function validate(o) {
    if(!['ratio','count','multiplier','manual'].includes(o.target_mode)) return '请选择数量目标。';
    if(o.target_mode==='count' && (!Number.isFinite(o.added_tiles)||o.added_tiles<=0)) return '新增张数应为正数，实际合法张数由验收确定。';
    if(o.target_mode==='multiplier' && (!Number.isFinite(o.total_multiplier)||o.total_multiplier<=1)) return '最终总量倍数应大于 1。';
    if(o.target_mode==='ratio' && (!Number.isFinite(o.min_ratio)||!Number.isFinite(o.max_ratio)||o.min_ratio<0||o.max_ratio>1||o.min_ratio>=o.max_ratio)) return '增加模式塔占比应为 0–100%，下限小于上限。';
    if(!['new','existing','both'].includes(o.operation)) return '请选择新建塔、原塔加层或混合。';
    if(o.stack_count!==null && (!Number.isInteger(o.stack_count)||o.stack_count<0)) return '堆数填 0 自动推荐，或填写正整数。';
    if(!o.regions?.length) return '至少选择一个摆放区域。';
    if(o.target_mode==='manual' && !o.tower_counts?.length) return '逐塔目标需要填写每一堆最终张数。';
    if(o.tower_counts.some(value=>!Number.isInteger(value)||value<1)) return '逐塔最终张数应为正整数。';
    for(const key of ['spacing','formation_gap','translate_x','translate_y']) if(!Number.isInteger(o[key])||(['spacing','formation_gap'].includes(key)&&o[key]<0)) return '间距和平移使用整数网格；间距不能小于 0。';
    return '';
  }
  function summary(o) {
    const target=o.target_mode==='count'?`新增约 ${o.added_tiles} 张`:o.target_mode==='multiplier'?`最终总量 ×${o.total_multiplier}`:o.target_mode==='manual'?`逐塔 ${o.tower_counts.join(' / ')} 张`:`${Number((o.min_ratio*100).toFixed(2))}% ≤ 塔占比 <${Number((o.max_ratio*100).toFixed(2))}%`;
    return `增加塔 · ${target} · ${{new:'新建塔',existing:'原塔加层',both:'混合'}[o.operation]} · ${o.stack_count===null||o.stack_count===0?'自动堆数':`${o.stack_count} 堆`}`;
  }
  function invalidField(o) {
    if(!['ratio','count','multiplier','manual'].includes(o.target_mode)) return 'target_mode';
    if(o.target_mode==='count'&&(!Number.isFinite(o.added_tiles)||o.added_tiles<=0))return 'added_tiles';
    if(o.target_mode==='multiplier'&&(!Number.isFinite(o.total_multiplier)||o.total_multiplier<=1))return 'total_multiplier';
    if(o.target_mode==='ratio'&&(!Number.isFinite(o.min_ratio)||o.min_ratio<0||o.min_ratio>=o.max_ratio))return 'min_ratio';
    if(o.target_mode==='ratio'&&(!Number.isFinite(o.max_ratio)||o.max_ratio>1))return 'max_ratio';
    if(!['new','existing','both'].includes(o.operation))return 'operation';
    if(o.stack_count!==null&&(!Number.isInteger(o.stack_count)||o.stack_count<0))return 'stack_count';
    if(o.tower_counts.some(v=>!Number.isInteger(v)||v<1)||(o.target_mode==='manual'&&!o.tower_counts.length))return 'tower_counts';
    return ['spacing','formation_gap','translate_x','translate_y'].find(key=>!Number.isInteger(o[key])||(['spacing','formation_gap'].includes(key)&&o[key]<0))||'target_mode';
  }
  function editTower(options,towers,index,patch,bindMirror=true) {
    const next=clone(options), tower=towers.find(t=>t.index===index);
    if(!tower) return next;
    const upsert=(key,value)=>{next[key]=next[key].filter(t=>t.index!==value.index);next[key].push(value);next[key].sort((a,b)=>a.index-b.index);};
    if(patch.count!==undefined) {next.height_mode='manual';next.tower_counts=towers.map(t=>next.tower_counts[t.index]??t.count);next.tower_counts[index]=patch.count;}
    if(patch.direction!==undefined) upsert('tower_directions',{index,direction:patch.direction});
    if(patch.x!==undefined||patch.y!==undefined) {
      const x=patch.x??tower.x,y=patch.y??tower.y;
      upsert('tower_positions',{index,x,y});
      if(bindMirror && towers.length>1) {
        // Bind positions around the visible tower group's centre; the API owns
        // authoritative symmetry and lock validation, including more complex groups.
        const cx=(Math.min(...towers.map(t=>t.x))+Math.max(...towers.map(t=>t.x)))/2;
        const cy=(Math.min(...towers.map(t=>t.y))+Math.max(...towers.map(t=>t.y)))/2;
        const dx=x-tower.x,dy=y-tower.y;
        for(const other of towers) {
          if(other.index===index) continue;
          const mirrorX=other.x===2*cx-tower.x && other.y===tower.y;
          const mirrorY=other.y===2*cy-tower.y && other.x===tower.x;
          const rotation=other.x===2*cx-tower.x && other.y===2*cy-tower.y;
          if(mirrorX||mirrorY||rotation) upsert('tower_positions',{index:other.index,x:other.x+(mirrorX||rotation?-dx:dx),y:other.y+(mirrorY||(!mirrorX&&rotation)?-dy:dy)});
        }
      }
    }
    return next;
  }
  function createPreviewFlow({request,onChange=()=>{}}) {
    const state={input:null,schemes:[],pending:false,error:'',stale:true,history:[],initial:null,selected:0};
    let epoch=0,controller=null;
    const notify=()=>onChange(state);
    const stop=()=>{epoch++;controller?.abort();controller=null;state.pending=false;};
    const api={state,
      configure(input) {stop();state.input=clone(input);state.initial=clone(input.increase);state.history=[];state.schemes=[];state.error='';state.stale=true;state.selected=0;notify();},
      change(options) {stop();state.history.push(clone(state.input.increase));state.input.increase=clone(options);state.stale=true;state.error='';notify();},
      adopt(index) {const scheme=state.schemes[index];if(!scheme?.options||state.stale) return;state.history.push(clone(state.input.increase));state.input.increase=clone(scheme.options);state.selected=index;notify();return clone(scheme.options);},
      undo() {if(!state.history.length)return;stop();state.input.increase=state.history.pop();state.stale=true;state.error='';notify();return clone(state.input.increase);},
      reset() {api.change(state.initial);return clone(state.input.increase);},
      cancel() {stop();notify();},
      async preview() {
        if(!state.input) return;
        stop();const invalid=validate(state.input.increase);if(invalid){state.error=invalid;notify();return;}
        const token=++epoch;controller=new AbortController();const active=controller;let timedOut=false;
        const timeout=setTimeout(()=>{timedOut=true;active.abort();},45000);
        state.pending=true;state.error='';notify();
        try {
          const payload=await request('/api/increase/preview',{method:'POST',json:{...clone(state.input),increase:normalizeOptions(state.input.increase)},signal:controller.signal});
          if(token!==epoch)return;
          if(payload.error) throw new Error(payload.error.message||'推荐未完成，请调整参数后重试。');
          state.schemes=Array.isArray(payload.schemes)?payload.schemes:[];state.selected=0;state.stale=false;
          if(!state.schemes.length)state.error='本次搜索范围内未找到已验收方案；请调整数量、区域或自动平移后重试。';
        } catch(error) {if(token!==epoch||(error.name==='AbortError'&&!timedOut))return;
          state.error=timedOut?'本次推荐超过 45 秒，输入已保留。可调整参数或点击重新推荐。':error.code==='preview_busy'?`${error.message}；请稍候点击“重新推荐”，不会自动重复提交。`:error.message;state.stale=true;}
        finally {clearTimeout(timeout);if(token===epoch){state.pending=false;controller=null;notify();}}
      },
    };return api;
  }
  function fieldsMarkup(prefix,options) {
    const o={...defaults(),...options};
    const select=(key,label,items)=>`<div class="field${['target_mode','fill_type'].includes(key)?' span-two':''}"><label for="${prefix}-${key}">${label}</label><select id="${prefix}-${key}" data-increase-field="${key}">${items.map(([value,text])=>`<option value="${value}" ${String(o[key])===String(value)?'selected':''}>${text}</option>`).join('')}</select></div>`;
    const number=(key,label,value=o[key],extra='')=>`<div class="field"><label for="${prefix}-${key}">${label}</label><input id="${prefix}-${key}" type="number" data-increase-field="${key}" value="${esc(value??'')}" ${extra}></div>`;
    return `<div class="form-grid increase-fields">
      ${select('target_mode','数量目标',[['ratio','最终塔占比'],['count','新增张数'],['multiplier','最终总量倍数'],['manual','逐塔最终张数']])}
      <div class="span-two" data-increase-target="count">${number('added_tiles','期望新增张数',o.added_tiles,'min="1" step="1"')}</div>
      <div class="span-two" data-increase-target="multiplier">${number('total_multiplier','最终总量 / 原总量',o.total_multiplier,'min="1.01" step="0.1"')}</div>
      <div data-increase-target="ratio">${number('min_ratio','塔占比下限（%）',o.min_ratio*100,'min="0" max="100" step="0.1"')}</div>
      <div data-increase-target="ratio">${number('max_ratio','塔占比上限（%）',o.max_ratio*100,'min="0" max="100" step="0.1"')}</div>
      <div class="field span-two" data-increase-target="manual"><label for="${prefix}-tower_counts">逐塔最终张数</label><input id="${prefix}-tower_counts" data-increase-field="tower_counts" value="${esc(o.tower_counts.join(', '))}" inputmode="numeric" aria-describedby="${prefix}-counts-help"><p id="${prefix}-counts-help">先原塔、再新塔，逗号分隔；单塔不要求 3 的倍数。</p></div>
      ${select('operation','增加方式',[['new','新建塔'],['existing','原塔加层'],['both','原塔加层 + 新建塔']])}
      ${number('stack_count','新增堆数（填0自动）',o.stack_count??0,'min="0" step="1"')}
      ${select('fill_type','新增 Tile 类型',[['auto','自动 · 原模板多数类型'],['A','新增 A'],['B','新增 B']])}
      <p class="field-help span-two">按最近合法数量取整，等距向上；下限包含、上限不包含。打开单模板预览查看实际数量。</p>
      <fieldset class="increase-regions span-two"><legend>摆放区域（可多选）</legend>${[['auto','自动'],['top','上'],['bottom','下'],['left','左'],['right','右'],['center','内部空白']].map(([value,label])=>`<label for="${prefix}-region-${value}"><input id="${prefix}-region-${value}" type="checkbox" data-increase-region="${value}" ${o.regions.includes(value)?'checked':''}>${label}</label>`).join('')}</fieldset>
      <details class="optional-section span-two"><summary>高度、方向与摆放</summary><div class="form-grid">
      ${select('height_mode','堆高关系',[['difference','保留高低差值'],['proportion','保留高度比例'],['manual','手动高度']])}
      ${select('direction','新塔默认方向',[['0','朝上'],['1','朝左'],['2','朝下'],['3','朝右']])}
      ${select('spread','摆放密度',[['compact','紧凑'],['distributed','分散']])}
      ${number('spacing','塔与塔间距',o.spacing,'min="0" step="1"')}${number('formation_gap','塔与主阵型间距',o.formation_gap,'min="0" step="1"')}
      ${number('translate_x','整体平移 X',o.translate_x,'step="1"')}${number('translate_y','整体平移 Y',o.translate_y,'step="1"')}
      <label class="checkbox-field span-two" for="${prefix}-auto_translate"><input id="${prefix}-auto_translate" type="checkbox" data-increase-field="auto_translate" ${o.auto_translate?'checked':''}>空间不足时推荐整体平移</label>
      </div></details></div>`;
  }
  function mountEditor(element,{prefix='increase',options,onChange=()=>{},onPreview=null}={}) {
    let current={...defaults(),...clone(options||{})};
    element.innerHTML=fieldsMarkup(prefix,current)+(onPreview?'<button class="secondary-button increase-preview-button" type="button" data-increase-open>推荐、预览与微调</button>':'');
    const conditional=()=>element.querySelectorAll('[data-increase-target]').forEach(node=>{node.hidden=node.dataset.increaseTarget!==current.target_mode;});
    conditional();
    element.oninput=element.onchange=event=>{
      const input=event.target;
      if(input.dataset.increaseRegion) {
        const region=input.dataset.increaseRegion;
        if(region==='auto'&&input.checked) current.regions=['auto'];
        else {current.regions=current.regions.filter(value=>value!=='auto'&&value!==region);if(input.checked)current.regions.push(region);if(!current.regions.length)current.regions=['auto'];}
        element.querySelectorAll('[data-increase-region]').forEach(node=>{node.checked=current.regions.includes(node.dataset.increaseRegion);});
      } else if(input.dataset.increaseField) {
        const key=input.dataset.increaseField;
        current[key]=key==='auto_translate'?input.checked:key==='tower_counts'?input.value.trim().split(/[,，\s]+/).filter(Boolean).map(Number):input.validity?.badInput?NaN:key==='stack_count'&&(input.value===''||Number(input.value)===0)?null:
          ['target_mode','operation','fill_type','spread','height_mode'].includes(key)?input.value:Number(input.value)/(['min_ratio','max_ratio'].includes(key)?100:1);
      } else return;
      conditional();onChange(clone(current));
    };
    element.onclick=event=>{if(event.target.closest('[data-increase-open]'))onPreview?.(clone(current));};
    return {value:()=>clone(current)};
  }
  function previewBoardMarkup(board,side,state,renderer,options={}) {
    if(board) return renderer.boardMarkup(board,options);
    const message=state.pending?(side==='source'?'正在读取原模板…':'正在试算新模板…'):
      state.error?(side==='source'?'未能读取原模板，请查看上方原因并重试。':'未能生成新模板预览，请查看上方原因并重试。'):
      side==='source'?'等待原模板预览。':'当前没有可用的新模板预览，请查看推荐状态。';
    return `<div class="preview-empty">${message}</div>`;
  }
  function mountPreview({document,request,openDialog,closeDialog,renderer,onApplied=()=>{}}) {
    const el=id=>document.getElementById(id),dialog=el('increase-preview-dialog');
    if(!dialog)return null;
    let context=null,timer=null,selectedTower=0,layerMode='all',drag=null;
    const flow=createPreviewFlow({request,onChange:render});
    function scheme(){return flow.state.schemes[flow.state.selected];}
    function towers(){return scheme()?.preview?.output?.towers||[];}
    function schedule(){clearTimeout(timer);timer=setTimeout(()=>flow.preview(),350);}
    function changed(options){flow.change(options);context?.onChange?.(clone(options));onApplied();schedule();}
    function render() {
      const s=flow.state;if(!context)return;
      el('increase-preview-title').textContent=`${context.label} · 增加塔预览`;
      el('increase-preview-status').textContent=s.pending?'正在搜索并独立验收推荐方案…':s.error|| (s.stale?'参数已修改；当前图为上次预览，正在等待重新验收。':'选择方案后点击“采用此方案”，只填入当前模板。');
      el('increase-preview-status').classList.toggle('is-error',Boolean(s.error));
      el('increase-preview-results').setAttribute('aria-busy',String(s.pending));
      el('increase-refresh').disabled=s.pending;
      el('increase-apply').disabled=s.pending||s.stale||!scheme()?.options;
      el('increase-undo').disabled=!s.history.length;
      el('increase-schemes').innerHTML=s.schemes.map((item,index)=>`<button class="${index===s.selected?'secondary-button is-selected':'text-button'}" type="button" data-increase-scheme="${index}" aria-pressed="${index===s.selected}">${esc(item.label||`方案 ${index+1}`)}</button>`).join('');
      const data=scheme()?.preview;
      const allLayers=[...(data?.source?.tiles||[]),...(data?.output?.tiles||[])].map(t=>t.z+1);
      const maximum=allLayers.reduce((maximum,value)=>Math.max(maximum,value),1);
      el('increase-layer').max=String(maximum);
      if(layerMode==='all')el('increase-layer').value=String(maximum);
      const layer=Math.min(maximum,Number(el('increase-layer').value)||maximum);
      el('increase-layer-value').textContent=layerMode==='all'?`全部 ${maximum} 层`:`第 1–${layer} 层`;
      for(const side of ['source','output'])el(`increase-${side}-board`).innerHTML=previewBoardMarkup(data?.[side],side,s,renderer,{mode:layerMode,layer,interactive:side==='output',markChanges:true});
      el('increase-tower-fields').hidden=!data?.output;
      const list=towers();if(!list.some(t=>t.index===selectedTower))selectedTower=list[0]?.index??0;
      el('increase-selected-tower').innerHTML=list.map(t=>`<option value="${t.index}" ${t.index===selectedTower?'selected':''}>第 ${t.index+1} 堆 · ${t.count} 张</option>`).join('');
      const tower=list.find(t=>t.index===selectedTower);
      if(tower) {
        const position=s.input.increase.tower_positions.find(t=>t.index===selectedTower)||tower;
        el('increase-tower-x').value=position.x;el('increase-tower-y').value=position.y;
        el('increase-tower-count').value=s.input.increase.tower_counts[selectedTower]??tower.count;
        el('increase-tower-direction').value=s.input.increase.tower_directions.find(t=>t.index===selectedTower)?.direction??tower.direction;
        el('increase-tower-lock').checked=s.input.increase.locked_towers.includes(selectedTower);
      }
      const facts=scheme()?.summary;
      el('increase-preview-facts').textContent=facts?describeSummary(facts):'导入后推荐使用服务端真实模板试算；预览不会生成可导出的产物。';
    }
    function edit(patch) {const base=flow.state.stale?flow.state.input.increase:(scheme()?.options||flow.state.input.increase);changed(editTower(base,towers(),selectedTower,patch,el('increase-mirror-position').checked));}
    dialog.addEventListener('click',event=>{
      const choice=event.target.closest('[data-increase-scheme]');if(choice){flow.state.selected=Number(choice.dataset.increaseScheme);render();return;}
      const tile=event.target.closest('[data-tower-index]');if(tile){selectedTower=Number(tile.dataset.towerIndex);render();return;}
      const move=event.target.closest('[data-tower-move]');if(move){const [dx,dy]=move.dataset.towerMove.split(',').map(Number);edit({x:Number(el('increase-tower-x').value)+dx,y:Number(el('increase-tower-y').value)+dy});}
    });
    dialog.addEventListener('keydown',event=>{
      if(!event.target.closest('[data-tower-index]')||!['ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(event.key))return;
      event.preventDefault();selectedTower=Number(event.target.closest('[data-tower-index]').dataset.towerIndex);
      const [dx,dy]={ArrowLeft:[-1,0],ArrowRight:[1,0],ArrowUp:[0,-1],ArrowDown:[0,1]}[event.key];
      const tower=flow.state.input.increase.tower_positions.find(t=>t.index===selectedTower)||towers().find(t=>t.index===selectedTower);if(tower)edit({x:tower.x+dx,y:tower.y+dy});
      el('increase-output-board').querySelector(`[data-tower-index="${selectedTower}"]`)?.focus();
    });
    const board=el('increase-output-board');
    board.addEventListener('pointerdown',event=>{const tile=event.target.closest('[data-tower-index]');if(!tile||event.button!==0)return;const t=towers().find(t=>t.index===Number(tile.dataset.towerIndex));if(!t)return;drag={index:t.index,x:t.x,y:t.y,clientX:event.clientX,clientY:event.clientY,rect:tile.closest('.editor-board').getBoundingClientRect()};board.setPointerCapture?.(event.pointerId);});
    board.addEventListener('pointerup',event=>{if(!drag)return;const d=drag;drag=null;const dx=Math.round((event.clientX-d.clientX)/d.rect.width*18),dy=Math.round((event.clientY-d.clientY)/d.rect.height*20);if(dx||dy){selectedTower=d.index;edit({x:d.x+dx,y:d.y+dy});}});
    board.addEventListener('pointercancel',()=>{drag=null;});
    el('increase-selected-tower').addEventListener('change',event=>{selectedTower=Number(event.target.value);render();});
    el('increase-tower-update').addEventListener('click',()=>edit({x:Number(el('increase-tower-x').value),y:Number(el('increase-tower-y').value),count:Number(el('increase-tower-count').value),direction:Number(el('increase-tower-direction').value)}));
    el('increase-tower-lock').addEventListener('change',event=>{const options=clone(scheme()?.options||flow.state.input.increase);options.tower_counts=towers().map(t=>options.tower_counts[t.index]??t.count);options.locked_towers=options.locked_towers.filter(i=>i!==selectedTower);if(event.target.checked)options.locked_towers.push(selectedTower);changed(options);});
    el('increase-refresh').addEventListener('click',()=>{clearTimeout(timer);flow.preview();});
    el('increase-apply').addEventListener('click',()=>{const options=flow.adopt(flow.state.selected);if(options){context.onChange(options);onApplied();el('increase-preview-status').textContent='已采用此方案，仅当前模板已更新；正式生成仍会重新验收。';}});
    el('increase-undo').addEventListener('click',()=>{const options=flow.undo();if(options){context.onChange(options);onApplied();schedule();}});
    el('increase-reset').addEventListener('click',()=>{const options=flow.reset();context.onChange(options);onApplied();schedule();});
    el('increase-layer').addEventListener('input',()=>{layerMode='through';render();});
    for(const [id,delta] of [['increase-layer-minus',-1],['increase-layer-plus',1]])el(id).addEventListener('click',()=>{layerMode='through';el('increase-layer').value=String(Math.max(1,Math.min(Number(el('increase-layer').max),Number(el('increase-layer').value)+delta)));render();});
    el('increase-layer-all').addEventListener('click',()=>{layerMode='all';render();});
    dialog.addEventListener('close',()=>{const previous=context;clearTimeout(timer);flow.cancel();context=null;previous?.onClose?.();});
    return {flow,open(next){context=next;selectedTower=0;layerMode='all';flow.configure({import_id:next.import_id,file_id:next.file_id,increase:next.options,...(next.new_id?{new_id:next.new_id}:{})});openDialog(dialog);flow.preview();},cancel(){clearTimeout(timer);flow.cancel();if(dialog.open)closeDialog(dialog);}};
  }
  function describeSummary(s) {
    const total=s.original_total!=null&&s.final_total!=null?`总量 ${s.original_total} → ${s.final_total} 张`:'';
    const added=Array.isArray(s.original_counts)&&Array.isArray(s.final_counts)?`新增 A ${s.final_counts[0]-s.original_counts[0]} / B ${s.final_counts[1]-s.original_counts[1]}`:
      typeof s.added_tiles==='object'?`新增 A ${s.added_tiles.A??0} / B ${s.added_tiles.B??0}`:`新增 ${s.added_tiles??'—'} 张`;
    const ratio=Number.isFinite(s.final_ratio)?`最终塔占比 ${Number((s.final_ratio*100).toFixed(2))}%`:'';
    const translation=s.translation?`整体平移 ${JSON.stringify(s.translation)}`:'';
    return [total,added,ratio,translation,s.adjustment?`数量调整 ${typeof s.adjustment==='string'?s.adjustment:JSON.stringify(s.adjustment)}`:''].filter(Boolean).join(' · ');
  }
  return {defaults,normalizeOptions,validate,invalidField,summary,editTower,createPreviewFlow,fieldsMarkup,mountEditor,previewBoardMarkup,mountPreview,describeSummary};
});
