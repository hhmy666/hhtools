import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
const source=fs.readFileSync(new URL("../assets/app.js",import.meta.url),"utf8");
const start=source.indexOf("  function calibrationProgressModel(");
const end=source.indexOf("  function renderConsumptionCalibrationProgress(",start);
const model=start<0?null:Function(`const clamp=(x,a,b)=>Math.max(a,Math.min(b,x));${source.slice(start,end)}return calibrationProgressModel;`)();
test("calibration progress uses actual completed duplicates, not elapsed time",()=>{
  assert.equal(typeof model,"function");
  const value=model({status:"running",elapsed_s:600,progress:{duplicate_completed:3,duplicate_total_all:800,template_completed:0,template_total:80,current_template:"sample",duplicate_index:4,message:"正在评估盘面"}});
  assert.equal(value.completed,3);
  assert.equal(value.total,800);
  assert.equal(value.percent,0.375);
  assert.equal(value.elapsed,600);
  assert.match(value.detail,/sample.*4/);
});
test("queued and failed calibration preserve honest progress and failure copy",()=>{
  assert.equal(typeof model,"function");
  assert.equal(model({status:"queued",progress:{}}).total,0);
  assert.equal(model({status:"failed",error:{message:"CSV 缺少必要列"},progress:{}}).message,"CSV 缺少必要列");
});

test("calibration reveals the next action without animation under reduced motion",()=>{
  const begin=source.indexOf("  function revealConsumptionCalibrationStage(");
  const finish=source.indexOf("  async function resolveCalibrationTemplates(",begin);
  assert.notEqual(begin,-1);
  const calls=[];
  const reveal=Function("$","window",`${source.slice(begin,finish)}return revealConsumptionCalibrationStage;`)(
    selector=>({scrollIntoView:options=>calls.push({selector,...options})}),
    {matchMedia:()=>({matches:true}),requestAnimationFrame:callback=>callback()},
  );
  reveal("action");reveal("result");
  assert.deepEqual(calls,[{selector:"#run-consumption-calibration",behavior:"auto",block:"center"},{selector:"#consumption-calibration-result",behavior:"auto",block:"start"}]);
});

test("calibration requests become stale when configuration source or name matching changes",()=>{
  const range=(a,b)=>source.slice(source.indexOf(a),source.indexOf(b,source.indexOf(a)));
  const state={consumptionCalibrationToken:1,calibrationSelectionId:"selection",consumptionFirstDupOverrides:{},defaultConfigSetId:"set-A",profile:{profile:{profile_id:"p",ulinkconfig_root:"/config/A"}}};
  const fields={"#consumption-template-prefix":"","#consumption-template-suffix":""};
  const helpers=Function("state","$","$$",`${range("  function selectedConsumptionStrategy(","  function syncConsumptionSeedPolicy(")}${range("  function consumptionCalibrationContext(","  function invalidateConsumptionCalibration(")}${range("  function calibrationOperationIsCurrent(","  async function requestConsumptionCalibration(")}return {fingerprint:consumptionCalibrationFingerprint,current:calibrationOperationIsCurrent};`)(state,selector=>({value:fields[selector]||""}),()=>[]);
  const operation={token:1,selectionId:"selection",fingerprint:helpers.fingerprint()};
  assert.equal(helpers.current(operation),true);
  state.defaultConfigSetId="set-B";
  assert.equal(helpers.current(operation),false);
  state.defaultConfigSetId="set-A";state.profile.profile.ulinkconfig_root="/config/B";
  assert.equal(helpers.current(operation),false);
  state.profile.profile.ulinkconfig_root="/config/A";fields["#consumption-template-prefix"]="aipuzzlelessT";
  assert.equal(helpers.current(operation),false);
});

test("changing form availability never re-enables an active calibration button",()=>{
  const begin=source.indexOf("  function updateConsumptionCalibrationActionState(");
  const finish=source.indexOf("  async function fileToBase64Payload(",begin);
  const button={dataset:{isBusy:"true"},disabled:true};
  const update=Function("state","$","setPrimaryActionAvailability",`${source.slice(begin,finish)}return updateConsumptionCalibrationActionState;`)(
    {calibrationSelectionId:"selection",consumptionHistoryFile:{name:"history.csv"},profile:{profile:{ulinkconfig_root:"/config"}}},
    ()=>button,(target,enabled)=>target.disabled=!enabled,
  );
  update();assert.equal(button.disabled,true);
});

test("automatic first duplicate cap defaults off and is forwarded for full and focused recalculation",async()=>{
  const html=fs.readFileSync(new URL("../index.html",import.meta.url),"utf8");
  const checkbox=html.match(/<input\b[^>]*id="consumption-auto-first-max"[^>]*>/)?.[0];
  assert.ok(checkbox);
  assert.doesNotMatch(checkbox,/\bchecked\b/);
  const fields={"#consumption-auto-first-max":{checked:false}};
  const begin=source.indexOf("  async function consumptionCalibrationRequestBody(");
  const finish=source.indexOf("  function calibrationProgressModel(",begin);
  const body=Function("state","$","$$","fileToBase64Payload","selectedConsumptionStrategy",`${source.slice(begin,finish)}return consumptionCalibrationRequestBody;`)(
    {calibrationSelectionId:"selection",consumptionHistoryFile:{name:"history.csv"}},
    selector=>fields[selector]||{value:""},()=>[],async file=>file,()=>({id:"1003",mode:"fixed"}),
  );
  assert.equal((await body()).auto_first_duplicate_max,false);
  fields["#consumption-auto-first-max"].checked=true;
  assert.equal((await body()).auto_first_duplicate_max,true);
  assert.equal((await body({focus_output_template_id:"one",first_duplicate_max:7})).auto_first_duplicate_max,true);
  assert.match(source,/consumption-auto-first-max"\)\.addEventListener\("change", invalidateConsumptionCalibration\)/);
});

test("recalculation refuses to merge a row into a draft from another configuration",async()=>{
  const begin=source.indexOf("  async function recalculateConsumptionTemplate(");
  const finish=source.indexOf("  async function writeConsumptionCalibrationConfig(",begin);
  const state={consumptionCalibrationResult:{config_set_id:"A",rows:[{output_template_id:"one"}]},consumptionCalibrationFingerprint:"A"};
  let requests=0;const messages=[];
  const recalc=Function("state","$","CSS","consumptionCalibrationFingerprint","toast","setBusy","renderConsumptionCalibrationProgress","consumptionCalibrationRequestBody","requestConsumptionCalibration","calibrationOperationIsCurrent","renderConsumptionCalibrationResult","updateConsumptionCalibrationActionState","invalidateConsumptionCalibration",`${source.slice(begin,finish)}return recalculateConsumptionTemplate;`)(
    state,selector=>selector==="#run-consumption-calibration"?{dataset:{}}:{value:"7",min:"1",max:"14"},
    {escape:value=>value},()=>"B",text=>messages.push(text),()=>{},()=>{},async()=>({}),async()=>{requests++;return {rows:[{output_template_id:"one"}]};},()=>true,()=>{},()=>{},()=>{state.consumptionCalibrationResult=null;},
  );
  await recalc("one",{});
  assert.equal(requests,0);
  assert.match(messages.join(" "),/重新生成/);
});
