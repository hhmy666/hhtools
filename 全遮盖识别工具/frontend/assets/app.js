(function () {
 'use strict';
 const V=window.CoverView,$=id=>document.getElementById(id);
 const previewCache=new Map();
 const selectedRows=new Set();
 let context=null,mode='ids',files=[],task=null,pending=false,query='',filter='all',page=1,size=20,screen='import',returnFocus=null,activity=null;
 const pathFields={
  'template-root': data=>data.template_root,
  'export-directory': data=>data.output_dir,
  'sync-template-directory': data=>data.template_dir||data.template_root,
  'sync-tile-directory': data=>data.tile_dir,
 };
 for(const id of Object.keys(pathFields)){
  $(id).addEventListener('change',()=>{
   try{window.localStorage.setItem('covercheck.path.'+id,$(id).value.trim());}catch(_){}
  });
 }
 function restorePaths(){
  for(const [id,defaultValue] of Object.entries(pathFields)){
   let saved=null;
   try{saved=window.localStorage.getItem('covercheck.path.'+id);}catch(_){}
   $(id).value=saved??(defaultValue(context)||'');
  }
 }
 const taskUrl=()=>'/api/tasks/'+encodeURIComponent(task.task_id);
 const unfinished=()=>task?.status==='running'||activity?.status==='running';
 const locked=()=>pending||unfinished();
 async function request(url,options={}) {
  const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),30000);
  try {
   const response=await fetch(url,{...options,signal:controller.signal,headers:{'Content-Type':'application/json',...(context?{'X-CSRF-Token':context.csrf_token}:{}),...options.headers}});
   let data;try{data=await response.json();}catch(_){throw Error('本地服务返回了无法读取的结果，请重试。');}
   if(!response.ok){if(url==='/api/tasks'&&response.status===409&&data.error?.task_id)return {task_id:data.error.task_id};const details=Array.isArray(data.error?.errors)?data.error.errors.filter(x=>typeof x==='string'):[],message=data.error?.message||'本地服务请求失败，请重试。';throw Object.assign(Error([...new Set([message,...details.filter(detail=>!message.includes(detail))])].join('；')),{code:data.error?.code,operation:data.error?.operation});}
   return data;
  }catch(error){if(error.name==='AbortError')throw Error('本地服务暂未响应，请稍后继续查看进度。');if(error instanceof TypeError)throw Error('与本地服务的连接中断，请检查启动器是否仍在运行。');throw error;}finally{clearTimeout(timer);}
 }
 function showScreen(value,focus=true){screen=value;$('import-page').hidden=value!=='import';$('results-page').hidden=value!=='results';$('step-import').setAttribute('aria-current',value==='import'?'step':'false');$('step-results').setAttribute('aria-current',value==='results'?'step':'false');$('step-results').classList.toggle('is-current',value==='results');if(focus){window.scrollTo({top:0,behavior:'auto'});$(value==='import'?'page-title':'results-page-title').focus({preventScroll:true});}}
 function feedback(message,field=null){
  const target=$(screen==='results'?'work-error':'input-error');for(const id of ['input-error','work-error']){$(id).hidden=true;$(id).textContent='';}target.hidden=!message;target.textContent=message||'';
  for(const [id,help]of[['template-ids','ids-help'],['template-root','root-help'],['template-files','files-help']]){$(id).removeAttribute('aria-invalid');$(id).setAttribute('aria-describedby',help);}
  if(field){$(field).setAttribute('aria-invalid','true');$(field).setAttribute('aria-describedby',$(field).getAttribute('aria-describedby')+' '+target.id);$(field).focus();}else if(message&&screen==='results'){target.scrollIntoView({block:'center',behavior:'auto'});}
 }
 function switchMode(value){if(locked())return;mode=value;for(const m of ['ids','files']){const active=m===value;$(m+'-tab').classList.toggle('is-selected',active);$(m+'-tab').setAttribute('aria-selected',String(active));$(m+'-tab').tabIndex=active?0:-1;$(m+'-panel').hidden=!active;}$('source-settings').hidden=value!=='ids';feedback('');renderControls();}
 function renderFiles(){$('files-list').innerHTML=files.length?files.map((f,i)=>`<li><div class="file-name">${V.esc(f.name)} <span class="file-meta">${(f.size/1024).toFixed(1)} KB</span></div><button type="button" class="text-button" data-remove="${i}" aria-label="移除 ${V.esc(f.name)}">移除</button></li>`).join(''):'<li class="empty-copy">尚未选择模板。</li>';renderControls();}
 function renderControls(){
  let count=0;try{count=V.parseIds($('template-ids').value).length;}catch(_){}const busy=locked();$('id-count').textContent=`已输入 ${count} 项`;
  $('start').disabled=busy||!context||(mode==='ids'?!$('template-ids').value.trim():!files.length);$('start').textContent=task?.status==='running'?'正在评估…':pending&&screen==='import'?'正在读取模板…':'评估模板 →';$('start').setAttribute('aria-busy',String(pending));
  for(const id of ['ids-tab','files-tab','template-ids','template-root','template-files','reset','back-import','export-pattern','export-directory','sync-template','sync-tile','sync-template-directory','sync-tile-directory','select-page','select-all-changed'])$(id).disabled=busy;
  const changed=task?.rows?.some(r=>r.edited&&r.status!=='error');
  $('repair-all').disabled=busy||!task?.rows?.some(r=>r.status==='issue');$('export-new').disabled=busy||!changed;$('export-zip').disabled=busy||!changed;$('confirm-write').disabled=busy;$('close-export-preview').disabled=busy;$('clear-selection').disabled=busy||!selectedRows.size;$('open-results').hidden=!task?.rows?.length||task.status!=='succeeded';$('open-results').disabled=busy;
  document.querySelectorAll('[data-remove],[data-preview],[data-detail],[data-select-row]').forEach(b=>b.disabled=busy||b.dataset.selectRow!=null&&!task?.rows[Number(b.dataset.selectRow)]?.edited);
 }
 function paintProgress(prefix,job,title){$(prefix+'-region').hidden=false;const percent=V.progress(job);$(prefix).value=percent;$(prefix+'-title').textContent=title;$(prefix+'-count').textContent=`${job.completed||0} / ${job.total||0} · ${percent}%`;$(prefix+'-current').textContent=job.current?`当前模板：${job.current}`:job.status==='succeeded'?'整批处理完成。':'正在准备…';}
 function render(){if(task){paintProgress('progress',task,task.status==='succeeded'?'评估完成':task.status==='failed'?'评估未完成':'正在评估整批模板');$('results').hidden=!task.rows?.length;if(task.rows?.length)renderResults();}renderControls();}
 function renderResults(){
  const rows=task.rows,summary=task.summary||{};$('summary').textContent=`共 ${rows.length} 个模板 · ${summary.issue??rows.filter(r=>r.status==='issue').length} 个命中 · ${summary.clean??rows.filter(r=>r.status==='clean').length} 个未发现 · ${summary.error??rows.filter(r=>r.status==='error').length} 个读取失败`;
  const selected=V.filtered(rows,query,filter),p=V.pageRows(selected,page,size);page=p.page;
  const available=rows.map((row,i)=>row.edited&&row.status!=='error'?i:-1).filter(i=>i>=0);
  for(const index of selectedRows)if(!available.includes(index))selectedRows.delete(index);
  $('selection-count').textContent=`已选 ${selectedRows.size} 个模板${selectedRows.size?'（跨页保留）':''}`;
  $('export-selection-hint').textContent=selectedRows.size?`将导出勾选的 ${selectedRows.size} 个已修改模板。`:`未勾选：将导出全部 ${available.length} 个有修改的新模板（机器或人工修改）。`;
  const pageIndices=p.rows.map(r=>rows.indexOf(r)).filter(i=>available.includes(i));
  $('select-page').checked=!!pageIndices.length&&pageIndices.every(i=>selectedRows.has(i));$('select-page').indeterminate=pageIndices.some(i=>selectedRows.has(i))&&!$('select-page').checked;
  $('result-body').innerHTML=p.rows.map(row=>{const index=rows.indexOf(row),error=row.status==='error',label={issue:'存在全遮盖',clean:row.edited?'修复后无全遮盖':'未发现全遮盖',error:'读取失败'}[row.status]||row.status,reason=row.repair_reason||row.reason||'';
   const savedLabel='修改已保存 · 可导出';
   const invalid=row.status==='clean'&&row.validation_errors?.length;
   const warningCount=row.validation_warnings?.length||0;
   return `<tr><td class="selection-cell"><input type="checkbox" data-select-row="${index}" aria-label="选择导出 ${V.esc(row.template_id||row.name)}" ${selectedRows.has(index)?'checked':''} ${!row.edited||error?'disabled':''}></td><td class="template-name">${V.esc(row.template_id||row.name)}${row.template_id&&row.name!==row.template_id?`<small class="file-meta"> · ${V.esc(row.name)}</small>`:''}${row.edited?`<small class="edited-label">${savedLabel}</small>`:''}</td><td><span class="status ${invalid?'issue':V.esc(row.status)}">${invalid?'无全遮盖 · 有摆放提示':V.esc(label)}</span>${error?`<small class="row-error">${V.esc(row.error)}</small>`:row.repair_status==='unrepairable'?`<small class="row-error">机器未修复：${V.esc(reason||'当前未找到合规方案，可手动编辑。')}</small>`:row.repair_status==='repaired'&&reason?`<small class="row-note">${V.esc(reason)}</small>`:''}${!error&&warningCount?`<small class="row-note">${row.support_issues?.length||0} 张悬空牌 · ${warningCount} 条摆放提示，不影响保存和导出</small>`:''}</td><td>${error?'—':row.adjacent_count||0}</td><td>${error?'—':row.floating_count||0}</td><td>${error?'—':row.coordinate_count||0}</td><td>${error?'—':thumbnailPair(row,index)}</td><td>${error?'—':`<div class="row-actions"><button type="button" class="row-action" data-preview="${index}">查看／编辑</button>${row.status==='issue'?`<button type="button" class="row-action" data-detail="${index}">坐标明细</button>`:''}</div>`}</td></tr>`;
  }).join('')||'<tr><td class="no-results" colspan="8">没有匹配结果，试试清空搜索或切换显示范围。</td></tr>';
  $('page-info').textContent=`${p.start}–${p.end} / ${selected.length} 个模板 · 第 ${page} / ${p.pages} 页`;$('previous').disabled=page===1;$('next').disabled=page===p.pages;
  for(const [id,detail]of[['download-templates','templates'],['download-coordinates','coordinates']]){$(id).hidden=task.status!=='succeeded';$(id).href=`${taskUrl()}/download?detail=${detail}`;}
  if(task.status==='succeeded')void hydrateThumbnails(p.rows,rows);
 }
 const previewKey=(row,index)=>`${task.task_id}:${index}:${row.revision||0}`;
 function thumbnailPair(row,index){const saved=previewCache.get(previewKey(row,index)),E=window.CoverEditor;return `<button type="button" class="result-thumbnails" data-preview="${index}" aria-label="查看 ${V.esc(row.template_id||row.name)} 改前改后模板"><span><span class="thumbnail-caption">原模板</span><span class="thumbnail-slot" data-thumb-original="${index}">${saved?.board&&E?.thumbnailMarkup?E.thumbnailMarkup(saved.board.original_board||saved.board):'读取预览…'}</span></span><span><span class="thumbnail-caption">新模板</span><span class="thumbnail-slot" data-thumb-current="${index}">${saved?.board&&E?.thumbnailMarkup?E.thumbnailMarkup(saved.board):'读取预览…'}</span></span></button>`;}
 async function hydrateThumbnails(pageRows,allRows){
  if(typeof window.CoverEditor?.thumbnailMarkup!=='function')return;
  const batchId=task.task_id,base=taskUrl(),queue=pageRows.filter(r=>r.status!=='error').map(row=>({row,index:allRows.indexOf(row)}));
  async function worker(){while(queue.length){if(task?.task_id!==batchId)return;const {row,index}=queue.shift(),key=`${batchId}:${index}:${row.revision||0}`;if(previewCache.has(key))continue;previewCache.set(key,{loading:true});let board;
   try{board=await request(`${base}/rows/${index}`);previewCache.set(key,{board});}catch(_){previewCache.delete(key);}
   if(task?.task_id!==batchId||previewKey(task.rows[index],index)!==key)continue;
   for(const [side,data]of[['original',board?.original_board||board],['current',board]]){const slot=document.querySelector(`[data-thumb-${side}="${index}"]`);if(slot)slot.innerHTML=data?window.CoverEditor.thumbnailMarkup(data):'点击查看';}
  }}
  await Promise.all(Array.from({length:Math.min(4,queue.length)},()=>worker()));
 }
 async function poll(){while(task?.status==='running'){await new Promise(resolve=>setTimeout(resolve,250));task=await request(taskUrl());render();}if(task?.status==='failed')throw Error(task.error?.message||task.error||'评估未完成，请重新评估。');if(task?.status==='succeeded'&&task.rows?.length){showScreen('results');}}
 function asBase64(file){return new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve({name:file.name,base64:String(reader.result).split(',')[1]});reader.onerror=()=>reject(Error(`无法读取 ${file.name}，请重新选择。`));reader.readAsDataURL(file);});}
 async function showTaskError(error,field=null,retry=resume){
  if(error.operation){try{await continueOperation(error.operation,true);feedback('');return;}catch(next){error=next;retry=resumeOperation;}}
  feedback(error.message,field);if(error.code==='task_not_found'){task=null;activity=null;sessionStorage.removeItem('covercheck-task');$('results').hidden=true;$('progress-region').hidden=true;showScreen('import');feedback(error.message);try{context=await request('/api/context');}catch(_){}return;}
  if(unfinished()){const button=document.createElement('button');button.type='button';button.className='row-action';button.id='resume';button.textContent='继续查看进度';button.addEventListener('click',retry);$(screen==='results'?'work-error':'input-error').append(' ',button);}
 }
 async function start(event){
  event?.preventDefault();if(locked())return;feedback('');let payload;
  try{
   if(mode==='ids'){const ids=V.parseIds($('template-ids').value);if(!ids.length){feedback('请先输入模板 ID。','template-ids');return;}if(!$('template-root').value.trim()){feedback('请填写模板仓路径。','template-root');return;}payload={mode,ids,template_root:$('template-root').value.trim()};}else{if(!files.length){feedback('请先选择模板文件。','template-files');return;}payload={mode};}
   pending=true;task=null;activity=null;selectedRows.clear();previewCache.clear();page=1;query='';filter='all';$('search').value='';$('filter').value='all';$('clear-search').hidden=true;$('results').hidden=true;$('download-zip').hidden=true;renderControls();paintProgress('progress',{completed:0,total:mode==='ids'?payload.ids.length:files.length},mode==='files'?'正在读取文件…':'正在准备评估…');
   if(mode==='files'){payload.files=[];let total=0;for(const file of files){total+=file.size;if(file.size>32*1024*1024)throw Error(`${file.name} 超过32 MB，请移除后重新评估。`);if(total*4/3>62*1024*1024)throw Error('这批文件总量超过单次传输限制，请分批评估。');payload.files.push(await asBase64(file));}}
   const created=await request('/api/tasks',{method:'POST',body:JSON.stringify(payload)});task={...created,status:'running',rows:[],total:mode==='ids'?payload.ids.length:files.length,completed:0};sessionStorage.setItem('covercheck-task',task.task_id);render();await poll();feedback('');
  }catch(error){await showTaskError(error,!pending?(mode==='ids'?'template-ids':'template-files'):null);}finally{pending=false;render();}
 }
 async function resume(){if(pending||!task?.task_id)return;pending=true;feedback('');renderControls();try{task=await request(taskUrl());render();await poll();}catch(error){await showTaskError(error);}finally{pending=false;render();}}
 async function repairAll(){if(locked()||!task)return;pending=true;feedback('');$('download-zip').hidden=true;renderControls();try{activity={...await request(taskUrl()+'/repair',{method:'POST',body:JSON.stringify({indices:task.rows.map((r,i)=>r.status==='issue'?i:-1).filter(i=>i>=0)})}),kind:'repair'};await continueOperation(activity);}catch(error){await showTaskError(error,null,resumeRepair);}finally{pending=false;render();}}
 async function finishRepair(){
  while(activity?.status==='running'){paintProgress('operation-progress',activity,'正在逐个修复模板');await new Promise(resolve=>setTimeout(resolve,250));activity={...await request(taskUrl()+'/repair'),kind:'repair'};}
  paintProgress('operation-progress',activity,activity.status==='succeeded'?'整批修复已完成':'修复未完成');task=await request(taskUrl());render();
  if(activity.status==='failed')throw Error(activity.error?.message||activity.error||'修复未完成，请重试。');
  const rows=activity.rows||[],repaired=rows.filter(r=>r.status==='repaired').length,failed=rows.filter(r=>r.status==='unrepairable'||r.status==='error').length;$('operation-progress-current').textContent=`${repaired} 个修复成功，${failed} 个机器未修复。成功项可继续人工编辑并导出；失败项保留原样。`;
 }
 async function resumeRepair(){if(pending)return;pending=true;feedback('');try{activity=await request(taskUrl()+'/repair');await finishRepair();}catch(error){await showTaskError(error,null,resumeRepair);}finally{pending=false;render();}}
 function exportPayload(){return {pattern:$('export-pattern').value.trim(),...(selectedRows.size?{indices:[...selectedRows]}:{})};}
 async function exportNew(){if(locked()||!task)return;pending=true;feedback('');$('download-zip').hidden=true;renderControls();try{activity={...await request(taskUrl()+'/export/preview',{method:'POST',body:JSON.stringify({...exportPayload(),output_dir:$('export-directory').value.trim(),sync_targets:['template','tile'].filter(name=>$('sync-'+name).checked),template_dir:$('sync-template-directory').value.trim(),tile_dir:$('sync-tile-directory').value.trim()})}),kind:'export'};await continueOperation(activity);}catch(error){await showTaskError(error,null,resumeExport);}finally{pending=false;render();}}
 async function exportZip(){if(locked()||!task)return;pending=true;feedback('');$('download-zip').hidden=true;renderControls();try{activity={...await request(taskUrl()+'/export',{method:'POST',body:JSON.stringify(exportPayload())}),kind:'export'};await continueOperation(activity);}catch(error){await showTaskError(error,null,resumeExport);}finally{pending=false;render();}}
 async function confirmWrite(){if(locked()||!activity?.export_id||activity.mode!=='preview')return;pending=true;feedback('');renderControls();try{activity={...await request(`${taskUrl()}/export/${activity.export_id}/write`,{method:'POST',body:'{}'}),kind:'export'};$('export-dialog').close();await continueOperation(activity,false);}catch(error){await showTaskError(error,null,resumeExport);}finally{pending=false;render();}}
 async function finishExport(download=true){
  const exportId=activity.export_id;
  const title=()=>activity.mode==='preview'?'正在准备导出预览':activity.mode==='write'?'正在写入新模板':'正在命名并打包新模板';
  while(activity?.status==='running'){paintProgress('operation-progress',activity,title());await new Promise(resolve=>setTimeout(resolve,250));activity={...await request(`${taskUrl()}/export/${encodeURIComponent(exportId)}/status`),kind:'export'};}
  paintProgress('operation-progress',activity,activity.status==='succeeded'?(activity.mode==='preview'?'导出预览已完成':activity.mode==='write'?'新模板写入处理完成':'新模板打包完成'):'新模板未能导出');
  if(activity.status!=='succeeded')throw Error(activity.error?.message||activity.error||'没有已保存的修改可导出，请查看各行原因。');
  if(activity.mode==='preview'){
   const count=activity.files?.length||0,targets=Object.keys(activity.targets||{}),labels={output:'输出模板',template:'模板仓',tile:'Tile 仓（编辑器）'};
   $('export-preview-summary').textContent=`${count} 个新模板，将写入 ${targets.length} 个目录；共 ${count*targets.length} 个文件。${activity.warnings?.length?`保留 ${activity.warnings.length} 条摆放提示，不阻止导出。`:''}`;
   $('export-preview-list').innerHTML=(activity.rows||[]).map(row=>`<li><strong>${V.esc(row.filename)}</strong>${(row.targets||[]).map(t=>`<p>${V.esc(labels[t.target]||t.target)}：${V.esc(t.path)}</p>`).join('')}</li>`).join('');
   $('confirm-write').textContent=`确认写入 ${count*targets.length} 个文件`;
   openDialog('export-dialog',$('export-new'));return;
  }
  const filenames=activity.files||[],skipped=activity.skipped||[],deliveries=(activity.deliveries||[]).flatMap(r=>r.targets||[]),failed=deliveries.filter(t=>t.status==='failed');$('operation-progress-current').textContent=activity.mode==='write'?`已处理 ${deliveries.length} 个文件：${deliveries.length-failed.length} 个写入，${failed.length} 个未写入。新模板ZIP可单独下载。`:`已生成 ${filenames.length} 个新模板${skipped.length?`，跳过 ${skipped.length} 项`:''}。源文件未被覆盖。`;$('export-skipped').hidden=!skipped.length&&!failed.length;$('export-skipped').textContent=[...skipped.map(r=>`${r.name}：${r.reason}`),...failed.map(t=>`${t.path}：${t.error}${t.applied?'（文件可能已写入，请核对）':''}`)].join('\n');$('export-names').textContent=filenames.join('、');$('download-zip').href=`${taskUrl()}/export/${encodeURIComponent(exportId)}`;$('download-zip').hidden=false;if(download&&activity.mode!=='write'){$('download-zip').click();$('download-zip').scrollIntoView({block:'center',behavior:'auto'});}
 }
 async function resumeExport(){if(pending||!activity?.export_id)return;pending=true;feedback('');try{await finishExport();}catch(error){await showTaskError(error,null,resumeExport);}finally{pending=false;render();}}
 async function continueOperation(job,download=true,focus=true){activity=job;showScreen('results',false);paintProgress('operation-progress',activity,job.kind==='export'?'正在校验、命名并打包新模板':'正在逐个修复模板');renderControls();if(focus)$('operation-progress-region').scrollIntoView({block:'center',behavior:'auto'});if(job.kind==='export')await finishExport(download);else await finishRepair();}
 async function resumeOperation(){if(pending||!activity)return;pending=true;feedback('');try{const url=activity.kind==='export'?`${taskUrl()}/export/${encodeURIComponent(activity.export_id)}/status`:taskUrl()+'/repair';await continueOperation({...await request(url),kind:activity.kind});}catch(error){await showTaskError(error,null,resumeOperation);}finally{pending=false;render();}}
 async function preview(index,trigger){
  if(locked())return;pending=true;feedback('');renderControls();
  try{
   let board=await request(`${taskUrl()}/rows/${index}`);
   async function replaceFrom(endpoint,payload){
    const saved=await request(`${taskUrl()}/rows/${index}/${endpoint}`,{method:'POST',body:JSON.stringify({...payload,revision:board.revision||0})});
    board=saved;task=await request(taskUrl());$('download-zip').hidden=true;render();return saved;
   }
   window.CoverEditor.open({board,original:board.original_board||board,
    onSave:(tiles,metadata={})=>replaceFrom('edit',{tiles,...(metadata.towers?{towers:metadata.towers}:{}),...(metadata.translation?{translation:metadata.translation}:{})}),
    onUndo:()=>replaceFrom('undo',{}),
    onClose:()=>{document.querySelector(`[data-preview="${index}"]`)?.focus({preventScroll:true});}});
  }catch(error){feedback(error.message);trigger?.focus();}finally{pending=false;renderControls();}
 }
 function addFiles(incoming){if(locked())return;const next=[...files];for(const f of incoming){if(!/\.bytes$/i.test(f.name)){feedback(`${f.name} 不是 .bytes 模板文件，请重新选择。`);continue;}if(!next.some(x=>x.name===f.name&&x.size===f.size&&x.lastModified===f.lastModified))next.push(f);}if(next.length>500){feedback('单次最多500个模板，请分批评估。');return;}files=next;renderFiles();}
 function openDialog(id,trigger){returnFocus=trigger||document.activeElement;$(id).showModal();$(id).querySelector('button')?.focus();}
 for(const id of ['help-dialog','detail-dialog','export-dialog'])$(id).addEventListener('close',()=>returnFocus?.focus({preventScroll:true}));
 document.querySelectorAll('[data-close]').forEach(b=>b.addEventListener('click',()=>$(b.dataset.close).close()));$('help').addEventListener('click',event=>openDialog('help-dialog',event.currentTarget));
 $('result-body').addEventListener('click',event=>{const p=event.target.closest('[data-preview]');if(p){preview(Number(p.dataset.preview),p);return;}const button=event.target.closest('[data-detail]');if(!button)return;const row=task.rows[Number(button.dataset.detail)];$('detail-title').textContent=row.template_id||row.name;$('detail-summary').textContent=`邻层 ${row.adjacent_count||0} 对 · 悬空 ${row.floating_count||0} 对 · ${row.coordinate_count||0} 个坐标组`;$('detail-body').innerHTML=(row.issues||[]).map(i=>`<tr><td>${i.kind==='adjacent'?'邻层一压一':'悬空全遮盖'}</td><td>(${i.x}, ${i.y})</td><td>第${i.lower_layer}层</td><td>第${i.upper_layer}层</td></tr>`).join('');openDialog('detail-dialog',button);});
 $('import-form').addEventListener('submit',start);$('ids-tab').addEventListener('click',()=>switchMode('ids'));$('files-tab').addEventListener('click',()=>switchMode('files'));
 for(const m of ['ids','files'])$(m+'-tab').addEventListener('keydown',event=>{if(['ArrowLeft','ArrowRight','Home','End'].includes(event.key)){event.preventDefault();const target=event.key==='Home'?'ids':event.key==='End'?'files':mode==='ids'?'files':'ids';switchMode(target);$(target+'-tab').focus();}});
 $('template-ids').addEventListener('input',renderControls);$('template-files').addEventListener('change',event=>{addFiles([...event.target.files]);event.target.value='';});$('files-list').addEventListener('click',event=>{const b=event.target.closest('[data-remove]');if(b&&!locked()){files.splice(Number(b.dataset.remove),1);renderFiles();}});
 for(const name of ['dragenter','dragover'])$('drop-zone').addEventListener(name,event=>{event.preventDefault();if(!locked())$('drop-zone').classList.add('is-dragging');});for(const name of ['dragleave','drop'])$('drop-zone').addEventListener(name,event=>{event.preventDefault();$('drop-zone').classList.remove('is-dragging');if(name==='drop')addFiles([...event.dataTransfer.files]);});
 $('search').addEventListener('input',()=>{query=$('search').value;page=1;$('clear-search').hidden=!query;render();});$('clear-search').addEventListener('click',()=>{$('search').value='';query='';page=1;$('clear-search').hidden=true;render();$('search').focus();});$('filter').addEventListener('change',()=>{filter=$('filter').value;page=1;render();});$('page-size').addEventListener('change',()=>{size=Number($('page-size').value);page=1;render();});$('previous').addEventListener('click',()=>{page--;render();});$('next').addEventListener('click',()=>{page++;render();});
 $('back-import').addEventListener('click',()=>{if(!locked())showScreen('import');});$('open-results').addEventListener('click',()=>showScreen('results'));$('repair-all').addEventListener('click',repairAll);$('export-new').addEventListener('click',exportNew);$('export-zip').addEventListener('click',exportZip);$('confirm-write').addEventListener('click',confirmWrite);
 function rememberSelection(){if(task)sessionStorage.setItem('covercheck-selection:'+task.task_id,JSON.stringify([...selectedRows]));render();}
 $('result-body').addEventListener('change',event=>{const input=event.target.closest('[data-select-row]');if(!input||locked())return;const i=Number(input.dataset.selectRow);if(task.rows[i]?.edited&&task.rows[i]?.status!=='error'){if(input.checked)selectedRows.add(i);else selectedRows.delete(i);rememberSelection();}});
 $('select-page').addEventListener('change',()=>{if(locked()||!task)return;for(const row of V.pageRows(V.filtered(task.rows,query,filter),page,size).rows){const i=task.rows.indexOf(row);if(row.edited&&row.status!=='error'){if($('select-page').checked)selectedRows.add(i);else selectedRows.delete(i);}}rememberSelection();});
 $('select-all-changed').addEventListener('click',()=>{if(locked()||!task)return;task.rows.forEach((row,i)=>{if(row.edited&&row.status!=='error')selectedRows.add(i);});rememberSelection();});$('clear-selection').addEventListener('click',()=>{if(locked())return;selectedRows.clear();rememberSelection();});
 const home=()=>showScreen(screen);$('home').addEventListener('click',home);$('workspace').addEventListener('click',home);$('settings').addEventListener('click',()=>{if(locked())return;showScreen('import',false);switchMode('ids');$('template-root').scrollIntoView({block:'center'});$('template-root').focus();});
 $('reset').addEventListener('click',()=>{if(locked())return;task=null;activity=null;selectedRows.clear();files=[];query='';filter='all';page=1;$('search').value='';$('filter').value='all';$('template-ids').value='';$('results').hidden=true;$('progress-region').hidden=true;$('operation-progress-region').hidden=true;$('download-zip').hidden=true;sessionStorage.removeItem('covercheck-task');feedback('');renderFiles();showScreen('import');});
 async function initialize(){showScreen('import',false);try{context=await request('/api/context');restorePaths();$('service').textContent='本地服务已连接';$('service').classList.add('is-online');renderControls();const saved=sessionStorage.getItem('covercheck-task');if(saved){try{task=await request('/api/tasks/'+encodeURIComponent(saved));try{for(const i of JSON.parse(sessionStorage.getItem('covercheck-selection:'+task.task_id)||'[]'))if(Number.isInteger(i))selectedRows.add(i);}catch(_){}render();if(task.status==='running')await resume();else if(task.rows?.length){showScreen('results',false);$('results-title').textContent='上次评估结果';if(task.operation){pending=true;try{await continueOperation(task.operation,task.operation.status==='running',false);}catch(error){await showTaskError(error,null,resumeOperation);}finally{pending=false;render();}}}}catch(_){sessionStorage.removeItem('covercheck-task');task=null;}}}catch(error){$('service').textContent='本地服务未连接';$('service').classList.add('is-offline');feedback(error.message+' 请通过启动器重新打开工具。');}}
 initialize();
})();
