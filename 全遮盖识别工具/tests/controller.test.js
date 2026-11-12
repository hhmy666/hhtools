'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const source=fs.readFileSync(require.resolve('../frontend/assets/app.js'),'utf8');
class Element{
 constructor(id){this.id=id;this.value='';this.hidden=false;this.disabled=false;this.textContent='';this.listeners={};this.dataset={};this.classList={add(){},toggle(){}};}
 addEventListener(name,fn){this.listeners[name]=fn;}
 setAttribute(k,v){this[k]=v;} getAttribute(k){return this[k];} removeAttribute(k){delete this[k];} focus(){this.focused=true;} scrollIntoView(){this.scrolled=true;}
 append(...items){this.children=(this.children||[]).concat(items);}
 querySelector(){return null;}
 click(){this.clicks=(this.clicks||0)+1;}
 showModal(){this.opened=true;} close(){this.opened=false;}
}
async function app(fetcher,saved={}){
 const elements=new Map(),get=id=>{if(!elements.has(id))elements.set(id,new Element(id));return elements.get(id);};
 const memory=new Map(Object.entries(saved));
 const storage={getItem:k=>memory.get(k)??null,setItem:(k,v)=>memory.set(k,v),removeItem:k=>memory.delete(k)};
 const document={getElementById:get,querySelectorAll:()=>[],createElement:()=>new Element(''),activeElement:null};
 let opened;
 vm.runInNewContext(source,{window:{CoverView:require('../frontend/assets/view.js'),CoverEditor:{open:o=>opened=o},scrollTo(){},localStorage:storage},document,fetch:fetcher,AbortController,sessionStorage:{getItem:k=>memory.get(k),setItem:(k,v)=>memory.set(k,v),removeItem:k=>memory.delete(k)},setTimeout:(fn,ms)=>setTimeout(fn,ms===250?0:ms),clearTimeout,FileReader:class{}});
 await new Promise(r=>setImmediate(r));get('template-ids').value='one';
 return {get,memory,editor:()=>opened,submit:()=>get('import-form').listeners.submit({preventDefault(){}})};
}
const response=data=>({ok:true,json:async()=>data});
test('preparing a new batch immediately hides old results and downloads',async()=>{
 let finish;const a=await app(async url=>url==='/api/context'?response({csrf_token:'t',template_root:'/templates'}):url==='/api/tasks'?new Promise(r=>finish=r):response({task_id:'new',status:'succeeded',total:1,completed:1,rows:[]}));
 a.get('results').hidden=false;const pending=a.submit();
 const hidden=a.get('results').hidden;
 finish(response({task_id:'new'}));await pending;
 assert.equal(hidden,true);
});
test('interrupted polling keeps the original task locked and resumable',async()=>{
 let posts=0,polls=0;
 const a=await app(async url=>{
  if(url==='/api/context')return response({csrf_token:'t',template_root:'/templates'});
  if(url==='/api/tasks'){posts++;return response({task_id:'original'});}
  if(++polls===1)throw Error('连接暂时中断');
  return response({task_id:'original',status:'succeeded',total:1,completed:1,rows:[{template_id:'one',name:'one',status:'clean'}],summary:{clean:1,issue:0,error:0}});
 });
 await a.submit();
 assert.equal(a.get('start').disabled,true);
 assert.equal(a.get('reset').disabled,true);
 await a.submit();assert.equal(posts,1);assert.equal(a.memory.get('covercheck-task'),'original');
 const resume=a.get('input-error').children.find(x=>x instanceof Element);
 await resume.listeners.click();
 assert.equal(a.get('start').disabled,false);assert.equal(a.get('reset').disabled,false);
 assert.equal(a.get('results-page-title').focused,true);
});
test('invalid IDs are associated with their field and live progress is not suppressed',async()=>{
 const a=await app(async()=>response({csrf_token:'t',template_root:'/templates'}));
 a.get('template-ids').value='../secret';await a.submit();
 assert.match(a.get('template-ids').getAttribute('aria-describedby'),/input-error/);
 assert.notEqual(a.get('import-form').getAttribute('aria-busy'),'true');
});
test('a retry after an uncertain submission reconnects the existing batch',async()=>{
 const a=await app(async url=>{
  if(url==='/api/context')return response({csrf_token:'t',template_root:'/templates'});
  if(url==='/api/tasks')return {ok:false,status:409,json:async()=>({error:{code:'task_running',message:'已有任务',task_id:'accepted-before-timeout'}})};
  return response({task_id:'accepted-before-timeout',status:'succeeded',completed:1,total:1,rows:[{template_id:'one',name:'one',status:'clean'}]});
 });
 await a.submit();
 assert.equal(a.memory.get('covercheck-task'),'accepted-before-timeout');
 assert.equal(a.get('progress-title').textContent,'评估完成');
});
test('expired in-memory task unlocks a fresh evaluation instead of endless resume',async()=>{
 let polls=0;const a=await app(async url=>{
  if(url==='/api/context')return response({csrf_token:'new-token',template_root:'/templates'});
  if(url==='/api/tasks')return response({task_id:'expired'});
  if(++polls===1)throw Error('连接中断');
  return {ok:false,status:404,json:async()=>({error:{code:'task_not_found',message:'上次结果已过期，请重新评估'}})};
 });
 await a.submit();const button=a.get('input-error').children.find(x=>x instanceof Element);await button.listeners.click();
 assert.equal(a.get('start').disabled,false);assert.equal(a.get('reset').disabled,false);
 assert.equal(a.memory.get('covercheck-task'),undefined);assert.equal(a.get('results').hidden,true);
});
test('evaluation moves to a separate result screen and can return without discarding results',async()=>{
 const a=await app(async url=>url==='/api/context'?response({csrf_token:'t',template_root:'/templates'}):url==='/api/tasks'?response({task_id:'screen'}):response({task_id:'screen',status:'succeeded',total:1,completed:1,rows:[{template_id:'one',name:'one',status:'issue',issues:[]}]}));
 await a.submit();
 assert.equal(a.get('import-page').hidden,true);assert.equal(a.get('results-page').hidden,false);
 a.get('back-import').listeners.click();assert.equal(a.get('import-page').hidden,false);assert.equal(a.get('results-page').hidden,true);
 a.get('open-results').listeners.click();assert.equal(a.get('results-page').hidden,false);
});
test('successful repair shows independently derived change counts and review caveat',async()=>{
 const row={name:'one',template_id:'one',status:'clean',edited:true,repair_status:'repaired',repair_reason:'问题牌1张，其他普通牌1张，塔牌0张，总牌量+0张；建议试玩复核。'};
 const a=await app(async url=>url==='/api/context'?response({csrf_token:'t',template_root:'/templates'}):url==='/api/tasks'?response({task_id:'impact'}):response({task_id:'impact',status:'succeeded',total:1,completed:1,rows:[row]}));
 await a.submit();
 assert.match(a.get('result-body').innerHTML,/其他普通牌1张，塔牌0张/);
 assert.match(a.get('result-body').innerHTML,/建议试玩复核/);
});
test('selection defaults to changed templates and preview requires explicit write confirmation',async()=>{
 const rows=[{name:'one',template_id:'one',status:'issue',edited:true,revision:1,exportable:true},
             {name:'two',template_id:'two',status:'clean',edited:false,revision:0},
             {name:'three',template_id:'three',status:'clean',edited:true,revision:1,exportable:true}];
 let sent, writes=0;
 const a=await app(async(url,options)=>{
  if(url==='/api/context')return response({csrf_token:'t',template_root:'/templates',output_dir:'/out',template_dir:'/repo',tile_dir:'/editor'});
  if(url==='/api/tasks')return response({task_id:'selected'});
  if(url.endsWith('/export/preview')){sent=JSON.parse(options.body);return response({kind:'export',export_id:'p1',mode:'preview',status:'succeeded',total:1,completed:1,files:['fixedone_m.bytes'],rows:[{index:0,filename:'fixedone_m.bytes',targets:[{target:'output',path:'/out/fixedone_m.bytes',status:'create'}]}],targets:{output:'/out'}});}
  if(url.endsWith('/write')){writes++;return response({kind:'export',export_id:'p1',mode:'write',status:'succeeded',total:1,completed:1,files:['fixedone_m.bytes'],deliveries:[]});}
  return response({task_id:'selected',status:'succeeded',total:3,completed:3,rows});
 });
 await a.submit();
 assert.match(a.get('export-selection-hint').textContent,/2/);
 const checkbox={dataset:{selectRow:'0'},checked:true};
 a.get('result-body').listeners.change({target:{closest:s=>s==='[data-select-row]'?checkbox:null}});
 await a.get('export-new').listeners.click();
 assert.deepEqual(sent.indices,[0]);assert.equal(sent.output_dir,'/out');assert.equal(writes,0);
 assert.equal(a.get('export-dialog').opened,true);
 await a.get('confirm-write').listeners.click();
 assert.equal(writes,1);
});
test('selection persists across pages and current-page selection does not include hidden pages',async()=>{
 let sent; const rows=Array.from({length:21},(_,i)=>({name:'n'+i,status:'clean',edited:true}));
 const a=await app(async(url,options)=>{
  if(url==='/api/context')return response({csrf_token:'t',template_root:'/templates',output_dir:'/out'});
  if(url==='/api/tasks')return response({task_id:'pages'});
  if(url.endsWith('/export/preview')){sent=JSON.parse(options.body);return response({export_id:'page-preview',mode:'preview',status:'succeeded',rows:[],files:[],targets:{output:'/out'}});}
  return response({task_id:'pages',status:'succeeded',rows});
 });
 await a.submit(); a.get('select-page').checked=true;a.get('select-page').listeners.change();
 assert.match(a.get('selection-count').textContent,/20/);
 a.get('next').listeners.click();assert.equal(a.get('select-page').checked,false);
 assert.match(a.get('selection-count').textContent,/20/);
 a.get('select-page').checked=true;a.get('select-page').listeners.change();
 await a.get('export-new').listeners.click();assert.equal(sent.indices.length,21);
 assert.deepEqual(sent.indices,Array.from({length:21},(_,i)=>i));
});
test('manual save errors preserve actionable tile coordinates and layers',async()=>{
 const board={template_id:'one',name:'one',status:'issue',tiles:[],revision:0};
 const a=await app(async url=>{
  if(url==='/api/context')return response({csrf_token:'t',template_root:'/templates'});
  if(url==='/api/tasks')return response({task_id:'manual'});
  if(url.endsWith('/edit'))return {ok:false,status:422,json:async()=>({error:{code:'invalid_board',message:'当前摆放未通过核验',errors:['第5层 (8, 10) 下方没有承接']}})};
  if(url.endsWith('/rows/0'))return response(board);
  return response({task_id:'manual',status:'succeeded',total:1,completed:1,rows:[board]});
 });
 await a.submit();const button={dataset:{preview:'0'}};
 a.get('result-body').listeners.click({target:{closest:selector=>selector==='[data-preview]'?button:null}});
 await new Promise(r=>setImmediate(r));
 await assert.rejects(a.editor().onSave([]),/第5层 \(8, 10\)/);
});
test('grouping hard errors appear once while the editor retains its draft',async()=>{
 const errors=['全部牌共100张，未按三张成组','A牌共100张，未按三张成组'];
 const board={template_id:'one',name:'one',status:'issue',tiles:[],revision:0};
 const a=await app(async url=>{
  if(url==='/api/context')return response({csrf_token:'t',template_root:'/templates'});
  if(url==='/api/tasks')return response({task_id:'grouped'});
  if(url.endsWith('/edit'))return {ok:false,status:422,json:async()=>({error:{code:'invalid_board',message:errors.join('；'),errors}})};
  if(url.endsWith('/rows/0'))return response(board);
  return response({task_id:'grouped',status:'succeeded',rows:[board]});
 });
 await a.submit(); const button={dataset:{preview:'0'}};
 a.get('result-body').listeners.click({target:{closest:s=>s==='[data-preview]'?button:null}});
 await new Promise(r=>setImmediate(r));
 await assert.rejects(a.editor().onSave([]),error=>error.message===errors.join('；'));
});
test('editor saves tower transforms and uses the latest revision for saved undo',async()=>{
 let board={template_id:'one',name:'one',status:'clean',tiles:[],towers:[],revision:0,can_undo:false};
 const payloads=[];
 const a=await app(async(url,options)=>{
  if(url==='/api/context')return response({csrf_token:'t',template_root:'/templates'});
  if(url==='/api/tasks')return response({task_id:'transform'});
  if(url.endsWith('/edit')||url.endsWith('/undo')){payloads.push([url,JSON.parse(options.body)]);board={...board,revision:board.revision+1,can_undo:url.endsWith('/edit')};return response(board);}
  if(url.endsWith('/rows/0'))return response(board);
  return response({task_id:'transform',status:'succeeded',total:1,completed:1,rows:[board]});
 });
 await a.submit();const button={dataset:{preview:'0'}};
 a.get('result-body').listeners.click({target:{closest:s=>s==='[data-preview]'?button:null}});
 await new Promise(r=>setImmediate(r));
 const towers=[{index:0,x:8,y:9,direction:1}];
 await a.editor().onSave([],{towers,translation:{dx:1,dy:0}});
 assert.deepEqual(payloads[0][1].towers,towers);
 assert.deepEqual(payloads[0][1].translation,{dx:1,dy:0});
 assert.equal(typeof a.editor().onUndo,'function');
 await a.editor().onUndo();
 assert.match(payloads[1][0],/\/rows\/0\/undo$/);
 assert.equal(payloads[1][1].revision,1);
 assert.equal(a.get('download-zip').hidden,true);
});
test('reloading restores a completed ZIP link without forcing a second download',async()=>{
 const operation={kind:'export',export_id:'zip1',status:'succeeded',total:1,completed:1,files:['fixedone_m.bytes'],skipped:[]};
 const a=await app(async url=>url==='/api/context'?response({csrf_token:'t',template_root:'/templates'}):response({task_id:'recover',status:'succeeded',rows:[{name:'one',status:'clean'}],operation}),{'covercheck-task':'recover'});
 assert.equal(a.get('download-zip').hidden,false);assert.match(a.get('download-zip').href,/\/export\/zip1$/);assert.equal(a.get('download-zip').clicks,undefined);
});
test('a running export locks controls across reload and resumes real progress',async()=>{
 let finish;
 const operation={kind:'export',export_id:'zip2',status:'running',total:1,completed:0,files:[],skipped:[]};
 const a=await app(async url=>{
  if(url==='/api/context')return response({csrf_token:'t',template_root:'/templates'});
  if(url.endsWith('/status'))return new Promise(r=>finish=r);
  return response({task_id:'recover',status:'succeeded',rows:[{name:'one',status:'clean'}],operation});
 },{'covercheck-task':'recover'});
 await new Promise(r=>setTimeout(r,5));assert.equal(a.get('reset').disabled,true);
 finish(response({...operation,status:'succeeded',completed:1,files:['fixedone_m.bytes']}));await new Promise(r=>setImmediate(r));
 assert.equal(a.get('reset').disabled,false);assert.equal(a.get('download-zip').clicks,1);
});
test('export conflict reconnects the server operation rather than leaving a dead error',async()=>{
 const operation={kind:'export',export_id:'zip3',status:'succeeded',completed:1,total:1,files:['fixedone_m.bytes'],skipped:[]};
 const a=await app(async url=>{
  if(url==='/api/context')return response({csrf_token:'t',template_root:'/templates'});
  if(url==='/api/tasks')return response({task_id:'recover'});
  if(url.endsWith('/export'))return {ok:false,status:409,json:async()=>({error:{code:'export_running',message:'正在导出',operation}})};
  return response({task_id:'recover',status:'succeeded',total:1,completed:1,rows:[{name:'one',status:'clean'}]});
 });
 await a.submit();await a.get('export-new').listeners.click();assert.equal(a.get('download-zip').hidden,false);assert.equal(a.get('download-zip').clicks,1);
});

test('restores saved paths and remembers edits for the next visit',async()=>{
 const ctx=async()=>response({csrf_token:'t',template_root:'',output_dir:'/default-output'});
 const a=await app(ctx,{'covercheck.path.template-root':'/team/Template'});
 assert.equal(a.get('template-root').value,'/team/Template');
 assert.equal(a.get('export-directory').value,'/default-output');
 a.get('sync-tile-directory').value='/team/TileMatchLevelData';
 a.get('sync-tile-directory').listeners.change();
 const b=await app(ctx,Object.fromEntries(a.memory));
 assert.equal(b.get('sync-tile-directory').value,'/team/TileMatchLevelData');
});
