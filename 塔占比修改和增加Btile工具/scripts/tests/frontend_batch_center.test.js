const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../../frontend/assets/app.js'),'utf8');
function setup(request=async()=>({centered:true})){
 const nodes=new Map(),node=id=>{if(!nodes.has(id))nodes.set(id,{textContent:'',hidden:true,disabled:false,attributes:{},classList:{toggle(){}},setAttribute(k,v){this.attributes[k]=v;},closest(){return table;}});return nodes.get(id);};
 const table={scrollLeft:75},calls=[],toasts=[],scrolls=[];
 const job={id:'job1',status:'partial_failed',templates:[{key:'a',templateId:'hr0127',status:'verified',artifacts:[{id:'aa'}]},{key:'b',templateId:'n6413',status:'verified',artifacts:[{id:'bb'}]},{key:'bad',status:'failed',artifacts:[]}]};
 const state={currentJob:job,currentJobId:'job1',requestGeneration:3,resultSelection:['a'],resultPreviewCache:new Map([['old',{}]])};
 const ctx={state,byId:node,TERMINAL_JOB_STATES:new Set(['succeeded','partial_failed']),translationPending:()=>false,invalidatePlans(){ctx.invalidated=(ctx.invalidated||0)+1;},updateResultSelection(){ctx.updateBatchCenterControls();},async loadJob(){ctx.loaded=(ctx.loaded||0)+1;},showToast(...args){toasts.push(args);},window:{scrollX:0,scrollY:420,scrollTo(pos){scrolls.push(pos);}},async apiRequest(url,options){calls.push([url,options]);return request(url,options);}};
 vm.runInNewContext(source.slice(source.indexOf('function batchCenterTargets('),source.indexOf('function openSelectedRerunEditor(')),ctx);
 return {ctx,state,node,calls,toasts,scrolls,table};
}
test('button is immediately left of rerun, shared styling and accessible progress',()=>{
 const html=fs.readFileSync(path.join(__dirname,'../../frontend/index.html'),'utf8');
 assert.match(html,/class="secondary-button" id="batch-center-button"[^>]*>批量居中新模板<\/button><button class="secondary-button" id="batch-rerun-button"/);
 assert.match(html,/id="batch-center-status" role="status" aria-live="polite"/);
 assert.match(html,/<progress id="batch-center-progress" max="100" value="0" aria-label="批量居中新模板进度"/);
});
test('centers all verified outputs ignoring selection, preserves scroll and continues individual failures',async()=>{
 const s=setup(async url=>{if(url.endsWith('/b'))throw new Error('产物已变化');return {centered:true};});
 await s.ctx.centerAllTemplates();
 assert.deepEqual(s.calls.map(c=>c[0]),['/api/jobs/job1/center/a','/api/jobs/job1/center/b']);
 assert.equal(s.calls[1][1].json.artifact_id,'bb');
 assert.match(s.node('batch-center-status').textContent,/已处理 2\/2：已平移居中 1，.*失败 1。n6413：产物已变化/);
 assert.equal(s.toasts[0][1],'warning');assert.equal(s.ctx.loaded,1);assert.equal(s.state.resultPreviewCache.size,0);
 assert.equal(s.scrolls[0].top,420);assert.equal(s.table.scrollLeft,75);assert.equal(s.state.batchCenter,null);
});
test('already centered results skip honestly and prevent double submission',async()=>{
 let finish;const s=setup(()=>new Promise(resolve=>{finish=resolve;}));
 const run=s.ctx.centerAllTemplates();await s.ctx.centerAllTemplates();assert.equal(s.calls.length,1);
 finish({centered:false,skipped:true});await new Promise(resolve=>setImmediate(resolve));
 assert.equal(s.calls.length,2);finish({centered:false,skipped:true});await run;
 assert.match(s.node('batch-center-status').textContent,/已平移居中 0，未平移 2，失败 0/);
});
test('late result cannot mutate a different job or continue its queue',async()=>{
 let finish;const s=setup(()=>new Promise(resolve=>{finish=resolve;}));const run=s.ctx.centerAllTemplates();
 s.state.currentJobId='job2';s.state.requestGeneration++;finish({centered:true});await run;
 assert.equal(s.calls.length,1);assert.equal(s.ctx.loaded,undefined);assert.equal(s.toasts.length,0);assert.equal(s.state.batchCenter,null);
});
test('controls disable for active jobs, missing outputs, drafts and batch work',()=>{
 const s=setup();s.ctx.updateBatchCenterControls();assert.equal(s.node('batch-center-button').disabled,false);
 for(const change of [()=>s.state.currentJob.status='running',()=>s.state.currentJob.templates=[],()=>s.state.manualDraft={},()=>s.state.batchCenter={}]){
   s.state.currentJob.status='succeeded';s.state.currentJob.templates=[{key:'a',status:'verified',artifacts:[{id:'aa'}]}];s.state.manualDraft=null;s.state.batchCenter=null;
   change();s.ctx.updateBatchCenterControls();assert.equal(s.node('batch-center-button').disabled,true);
 }
});
test('visible progress tracks requests and finishes only after result refresh',async()=>{
 let finish,refresh;const s=setup(()=>new Promise(resolve=>{finish=resolve;}));
 s.ctx.loadJob=()=>new Promise(resolve=>{refresh=resolve;});
 const run=s.ctx.centerAllTemplates();
 assert.equal(s.node('batch-center-progress-panel').hidden,false);
 assert.equal(s.node('batch-center-progress').value,0);
 assert.match(s.node('batch-center-status').textContent,/hr0127/);
 finish({centered:true});await new Promise(resolve=>setImmediate(resolve));
 assert.equal(s.node('batch-center-progress').value,50);
 assert.match(s.node('batch-center-status').textContent,/n6413/);
 finish({skipped:true});await new Promise(resolve=>setImmediate(resolve));
 assert.equal(s.node('batch-center-progress').value,99);
 assert.match(s.node('batch-center-progress-title').textContent,/刷新/);
 refresh();await run;
 assert.equal(s.node('batch-center-progress').value,100);
 assert.equal(s.node('batch-center-progress-value').textContent,'100%');
 assert.equal(s.node('batch-center-progress-title').textContent,'居中完成');
});
