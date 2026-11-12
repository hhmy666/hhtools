'use strict';
const test=require('node:test'),assert=require('node:assert/strict');
const P=require('../../frontend/assets/parameter-config.js');

test('optional affixes are sent for files and IDs, but omitted when automatic',async()=>{
  for(const mode of ['ids','files']){
    const calls=[];const flow=P.createWorkflow({background:true,pollDelay:0,upload:async()=>({import_id:'import-test'}),
      request:async(_url,options)=>{calls.push(options.json);return {task_id:'task',status:'succeeded',selection:{selection_id:'s',entries:[]},result:{rows:[]}};}});
    flow.input({mode,ids:'noThr0029_end',files:[{name:'noThr0029_end_m.bytes',size:30}],stripPrefix:'noT',stripSuffix:'_end'});
    await flow.calculate();assert.equal(calls[0].payload.source.strip_prefix,'noT');assert.equal(calls[0].payload.source.strip_suffix,'_end');
    flow.input({stripPrefix:'',stripSuffix:''});await flow.calculate();
    assert.equal(calls[1].payload.source.strip_prefix,undefined);assert.equal(calls[1].payload.source.strip_suffix,undefined);
  }
});

test('ambiguous matches offer choices and allow multiple new templates to use one original',async()=>{
  const calls=[];let attempt=0;
  const entries=['noThr0029','copyhr0029'].map(new_template_id=>({new_template_id,status:'failed',message:'请选择原模板',source_candidates:[{original_template_id:'hr0029'},{original_template_id:'hr0029v2'}]}));
  const flow=P.createWorkflow({background:true,pollDelay:0,request:async(_url,options)=>{
    calls.push(options.json);attempt++;
    return {task_id:'task'+attempt,status:'succeeded',selection:{selection_id:'s'+attempt,entries:attempt===1?entries:[]},result:{rows:[]}};
  }});
  flow.input({ids:'noThr0029\ncopyhr0029'});await flow.calculate();
  assert.equal(flow.state.matchCandidates.length,2);
  assert.equal(flow.chooseSource('noThr0029','not-a-candidate'),false);
  assert.equal(flow.chooseSource('noThr0029','hr0029'),true);
  assert.equal(flow.state.result,null);assert.equal(flow.state.matchCandidates.length,2);
  assert.equal(flow.chooseSource('copyhr0029','hr0029'),true);await flow.calculate();
  assert.deepEqual(calls.at(-1).payload.source.source_map,{noThr0029:'hr0029',copyhr0029:'hr0029'});
  assert.equal(flow.state.matchCandidates.length,0);
});

test('a new input clears old candidate choices and a late result cannot restore them',async()=>{
  let finish;const flow=P.createWorkflow({background:true,pollDelay:0,request:()=>new Promise(resolve=>finish=resolve)});
  flow.input({ids:'noThr0029'});const pending=flow.calculate();await Promise.resolve();await Promise.resolve();
  flow.input({ids:'other'});
  finish({task_id:'old',status:'succeeded',selection:{entries:[{new_template_id:'noThr0029',source_candidates:[{original_template_id:'hr0029'}]}]},result:{rows:[]}});
  await pending;assert.deepEqual(flow.state.sourceChoices,{});assert.deepEqual(flow.state.matchCandidates,[]);
});

test('candidate rendering escapes values and labels the mapping instead of silently selecting one',()=>{
  const html=P.renderSourceCandidates([{new_template_id:'noT<x>',source_candidates:[{original_template_id:'hr&1'},{original_template_id:'hr2'}]}],{});
  assert.match(html,/选择原模板（可多选）/);assert.match(html,/noT&lt;x&gt;/);assert.match(html,/hr&amp;1/);assert.doesNotMatch(html,/<x>/);
  assert.match(html,/data-parameter-source-choice/);assert.match(html,/aria-label=/);
});

test('one new template can select multiple originals and repeated explicit mappings preserve them',async()=>{
  assert.deepEqual(P.parseTemplateInput('hr1=new\nhr2=new').source_map,{new:['hr1','hr2']});
  const calls=[];const flow=P.createWorkflow({background:true,pollDelay:0,request:async(url,options)=>{
    calls.push(options.json);return {task_id:'task',status:'succeeded',selection:{selection_id:'s',entries:[{new_template_id:'new',source_candidates:[{original_template_id:'hr1'},{original_template_id:'hr2'}]}]},result:{rows:[]}};
  }});
  flow.input({ids:'new'});await flow.calculate();assert.equal(flow.chooseSource('new',['hr1','hr2']),true);await flow.calculate();
  assert.deepEqual(calls.at(-1).payload.source.source_map,{new:['hr1','hr2']});
});

test('optional original level prefix is sent to calculation and preview without changing default',async()=>{
  const calls=[];const flow=P.createWorkflow({background:true,pollDelay:0,request:async(url,options)=>{
    calls.push(options.json);return {task_id:'task'+calls.length,status:'succeeded',selection:{selection_id:'s',entries:[]},result:{calculation_id:'c',rows:[{row_id:'r',status:'ready'}]}};
  }});
  flow.input({ids:'new'});await flow.calculate();assert.equal(calls[0].payload.preserve_level_prefix,undefined);
  flow.delivery({preserveLevelPrefix:true});await flow.calculate();assert.equal(calls.at(-1).payload.preserve_level_prefix,true);
  await flow.preview();assert.equal(calls.at(-1).payload.preserve_level_prefix,true);
});
