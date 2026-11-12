'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const source=fs.readFileSync(require('node:path').join(__dirname,'../../frontend/assets/app.js'),'utf8');
const VM=require('../../frontend/assets/view-model.js');
function load(name,context){const start=source.indexOf(`function ${name}(`),end=source.indexOf('\nfunction ',start+1);vm.runInNewContext(source.slice(start,end),context);}
test('adjusted badge data persists for edits and translations but not fresh generation',()=>{
 for(const plan of [{adjusted:true},{manual_edit:{added:0,deleted:0}},{translation:{dx:0,dy:1}}])assert.equal(VM.normalizeTemplate({plan_summary:plan},'one',0).adjusted,true);
 assert.equal(VM.normalizeTemplate({plan_summary:{}},'one',0).adjusted,false);
});
test('closing comparison restores its replaced row focus without scrolling to the export heading',()=>{
 const focus=[],scroll=[],region={scrollLeft:0},shell={inert:true};
 const row={dataset:{previewTemplate:'one'},focus:options=>focus.push(options)};
 const context={state:{currentJobId:'job',previewReturnPosition:{jobId:'job',key:'one',x:0,y:1500,tableX:90}},
  translationPending:()=>false,all:()=>[row],byId:id=>id==='app-shell'?shell:{closest:()=>region},window:{scrollTo:(...args)=>scroll.push(args)}};
 load('closeDialog',context);
 context.closeDialog({id:'template-preview-dialog',open:true,dataset:{},close(){this.open=false;}});
 assert.deepEqual(JSON.parse(JSON.stringify(focus)),[{preventScroll:true}]);assert.deepEqual(scroll,[[0,1500]]);assert.equal(region.scrollLeft,90);
});
test('thumbnail requests remain bounded across rerenders and stale artifacts never paint',async()=>{
 let observer;const requests=[],buttons=Array.from({length:4},(_,i)=>({dataset:{resultThumbnail:String(i),artifact:'a'+i},isConnected:true,innerHTML:''}));
 const state={currentJobId:'job',resultPreviewCache:new Map(),resultPreviewWork:{active:0,queue:[],inflight:new Set()}};
 const context={state,window:{IntersectionObserver:class{constructor(fn){this.fn=fn;observer=this;}observe(){}unobserve(){}disconnect(){}},TowerTemplatePreview:{thumbnailMarkup:b=>b?'real':'none'}},
  all:()=>buttons,byId:()=>({}),encodeURIComponent,apiRequest:url=>new Promise(resolve=>requests.push({url,resolve}))};
 load('observeResultThumbnails',context);context.observeResultThumbnails({id:'job'});
 observer.fn(buttons.map(target=>({target,isIntersecting:true})));assert.equal(requests.length,2);
 context.observeResultThumbnails({id:'job'});observer.fn(buttons.map(target=>({target,isIntersecting:true})));assert.equal(requests.length,2);
 requests[0].resolve({artifact_id:'stale',source:{},output:{}});await new Promise(setImmediate);
 assert.equal(buttons[0].innerHTML,'');assert.equal(requests.length,3);
 requests[1].resolve({artifact_id:'a1',source:{},output:{}});await new Promise(setImmediate);
 assert.match(buttons[1].innerHTML,/real/);assert.equal(requests.length,4);
});
