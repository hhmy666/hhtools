const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync(require('node:path').join(__dirname,'../../frontend/assets/app.js'),'utf8');
function load(context,name){const start=source.search(new RegExp(`(?:async )?function ${name}\\(`));assert.notEqual(start,-1,`${name} implemented`);const next=source.slice(start+1).search(/\n(?:async )?function /);vm.runInNewContext(source.slice(start,next<0?source.length:start+1+next),context);}
test('bulk rerun sends every selected row while preserving each naming rule and advanced tower counts',async()=>{
 const rows=[{key:'a',parameters:{naming_rule:'one{原模板ID}',new_id:'customA',retained_counts:[1,2]}},{key:'b',outputId:'exportedBv2',parameters:{naming_rule:'two{原模板ID}',retained_counts:null}},{key:'c',parameters:{}}];
 const calls=[],feedback=[];let closed=false;
 const state={currentJobId:'job',rerunKey:'a',rerunKeys:['a','b'],currentJob:{templates:rows},resultSelection:['a','b'],requestGeneration:1};
 const context={state,encodeURIComponent,asText:(v,f)=>v||f,asArray:v=>v||[],byId:()=>({}),rerunDefaults:()=>({ratio:0,strategy:'top_first',retained_counts:[99],naming_rule:'wrong'}),
  setDialogPending(){},invalidatePlans(){},apiRequest:async(url,options)=>{calls.push({url,defaults:options.json.defaults});return {status:'queued'};},closeDialog(){closed=true;},schedulePoll(){},showToast(){},showFeedback:(...args)=>feedback.push(args)};
 load(context,'submitRerun');await context.submitRerun({preventDefault(){},currentTarget:{}});
 assert.equal(calls.length,2);assert.match(calls[1].url,/\/rerun\/b$/);assert.equal(calls[0].defaults.naming_rule,'one{原模板ID}');assert.deepEqual(calls[0].defaults.retained_counts,[1,2]);assert.equal(calls[1].defaults.retained_counts,null);assert.equal(rows[2].parameters.naming_rule,undefined);assert.equal(closed,true);
 assert.equal(calls[0].defaults.new_id,'customA');assert.equal(calls[1].defaults.new_id,'exportedBv2');
});
test('partial batch rejection preserves delivery selection and retries only rejected submissions',async()=>{
 const state={currentJobId:'job',rerunKey:'a',rerunKeys:['a','b'],currentJob:{templates:[{key:'a',parameters:{}},{key:'b',parameters:{}}]},resultSelection:['a','b'],requestGeneration:1};
 const messages=[];
 const context={state,encodeURIComponent,asText:(v,f)=>v||f,asArray:v=>v||[],byId:()=>({}),rerunDefaults:()=>({ratio:0}),setDialogPending(){},invalidatePlans(){},apiRequest:async url=>{if(url.endsWith('/b'))throw Error('该模板已在队列中');return {};},closeDialog(){},schedulePoll(){},showToast(){},showFeedback:(_id,message)=>messages.push(message)};
 load(context,'submitRerun');await context.submitRerun({preventDefault(){},currentTarget:{}});
 assert.deepEqual(Array.from(state.resultSelection),['a','b']);assert.deepEqual(Array.from(state.rerunKeys),['b']);assert.match(messages.join(' '),/1.*已提交|已提交.*1/);assert.match(messages.join(' '),/该模板已在队列中/);
});
