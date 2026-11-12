const test = require('node:test');
const assert = require('node:assert/strict');
const VM = require('../../frontend/assets/view-model.js');
const O = require('../../frontend/assets/ordinary-add-ui.js');
function fixture() {
  const current = O.defaults(), next = {...current, placement_strategy:'both'};
  const hash = 'a'.repeat(64);
  return {operation:'ordinary_add', sourceSha256:hash, parameters:{mode:'ordinary_add',ordinary_add:current},
    parameterAdvice:{operation:'ordinary_add',source_sha256:hash,current_options:current,schemes:[{
      verified:true,source_sha256:hash,output_sha256:'b'.repeat(64),parameters:{mode:'ordinary_add',ordinary_add:next},
      changes:[{field:'placement_strategy',from:'bottom_first',to:'both'}],
      facts:{original_total:72,final_total:90,added_tiles:18,top_added_tiles:10,bottom_added_tiles:8,added_layers:2}
    }]}};
}
test('ordinary advice shows actual changes and audited outcome',()=>{
  const view=VM.parameterAdviceView(fixture());
  assert.equal(view.schemes.length,1);
  assert.match(view.schemes[0].adjustments.join(''),/最下层.*上下/);
  assert.match(view.schemes[0].outcome,/10.*8/);
});
test('ordinary advice rejects stale, altered and unverified schemes',()=>{
  for (const corrupt of [r=>r.parameterAdvice.schemes[0].verified=false,
    r=>r.sourceSha256='c'.repeat(64),r=>r.parameters.ordinary_add={...r.parameters.ordinary_add,added_tiles:24},
    r=>r.parameterAdvice.schemes[0].parameters.ordinary_add.tile_type='A',
    r=>r.parameterAdvice.schemes[0].facts.final_total=100,
    r=>r.parameterAdvice.schemes=[null],r=>r.parameterAdvice.schemes[0].changes=[null]]) {
    const row=fixture();corrupt(row);assert.equal(VM.parameterAdviceView(row).schemes.length,0);
  }
});

test('one-click fill changes only current ordinary options and rejects stale context',()=>{
  const fs=require('node:fs'), sandbox=require('node:vm');
  const app=fs.readFileSync(require('node:path').join(__dirname,'../../frontend/assets/app.js'),'utf8');
  const extract=name=>{const start=app.indexOf(`function ${name}(`),end=app.indexOf('\nfunction ',start+1);return app.slice(start,end);};
  const row={...fixture(),key:'selected'}, other={key:'other',parameters:{ordinary_add:O.defaults()}};
  const state={currentJobId:'job',currentJob:{templates:[row,other]},rerunKey:'selected'};
  const nodes=new Map();const byId=id=>{if(!nodes.has(id))nodes.set(id,{value:id==='rerun-new-id'?'custom-id':'',focus(){}});return nodes.get(id);};
  let mounted,feedback;
  const ctx={VM,state,structuredClone,byId,ORDINARY:{mountEditor(node,options){mounted=options.options;}},clearInvalid(){},showFeedback(id,message){feedback=message;}};
  sandbox.createContext(ctx);sandbox.runInContext(extract('increaseAdviceContext')+'\n'+extract('fillAdviceScheme'),ctx);
  state.rerunAdviceContext=ctx.increaseAdviceContext(row);
  ctx.fillAdviceScheme(0);
  assert.equal(mounted.placement_strategy,'both');
  assert.equal(byId('rerun-new-id').value,'custom-id');
  assert.equal(other.parameters.ordinary_add.placement_strategy,'bottom_first');
  assert.equal(row.parameters.ordinary_add.placement_strategy,'bottom_first');
  state.rerunAdviceContext='stale';mounted=null;ctx.fillAdviceScheme(0);
  assert.equal(mounted,null);assert.match(feedback,/建议已更新/);
});
