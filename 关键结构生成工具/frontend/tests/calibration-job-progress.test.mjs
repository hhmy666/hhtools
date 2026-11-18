import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
const source=fs.readFileSync(new URL("../assets/app.js",import.meta.url),"utf8");
const affixHelper = source.slice(source.indexOf("  function calibrationBatchAffixes("),
  source.indexOf("  function calibrationNameMatchContext("));
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

test("calibration requests track the profile, match options and deduction, independently of the parameter index",()=>{
  const range=(a,b)=>source.slice(source.indexOf(a),source.indexOf(b,source.indexOf(a)));
  const state={consumptionCalibrationToken:1,calibrationSelectionId:"selection",consumptionFirstDupOverrides:{},defaultConfigSetId:"set-A",profile:{profile:{profile_id:"p",ulinkconfig_root:"/config/A"}}};
  const fields={"#consumption-template-prefix":"","#consumption-template-suffix":""};
  const helpers=Function("state","$","$$",`${affixHelper}${range("  function selectedConsumptionStrategy(","  function syncConsumptionSeedPolicy(")}${range("  function consumptionCalibrationContext(","  function invalidateConsumptionCalibration(")}${range("  function calibrationOperationIsCurrent(","  async function requestConsumptionCalibration(")}return {fingerprint:consumptionCalibrationFingerprint,current:calibrationOperationIsCurrent};`)(state,selector=>typeof fields[selector] === "object" ? fields[selector] : ({value:fields[selector]||""}),()=>[]);
  const operation={token:1,selectionId:"selection",fingerprint:helpers.fingerprint()};
  assert.equal(helpers.current(operation),true);
  state.defaultConfigSetId="set-B";
  assert.equal(helpers.current(operation),true);
  state.profile.profile.ulinkconfig_root="/config/B";
  assert.equal(helpers.current(operation),false);
  state.profile.profile.ulinkconfig_root="/config/A";fields["#consumption-template-prefix"]="aipuzzlelessT";
  assert.equal(helpers.current(operation),false);
  fields["#consumption-template-prefix"]="";
  fields["#consumption-fixed-first-max"]={checked:true};
  fields["#consumption-first-max-deduction-count"]="2";
  const updated = { ...operation, fingerprint: helpers.fingerprint() };
  fields["#consumption-first-max-deduction-count"]="3";
  assert.equal(helpers.current(updated),false);
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
  const body=Function("state","$","$$","fileToBase64Payload","selectedConsumptionStrategy",`${affixHelper}${source.slice(begin,finish)}return consumptionCalibrationRequestBody;`)(
    {calibrationSelectionId:"selection",consumptionHistoryFile:{name:"history.csv"},defaultConfigSetId:".levelTemplate/backups"},
    selector=>fields[selector]||{value:""},()=>[],async file=>file,()=>({id:"1003",mode:"fixed"}),
  );
  assert.equal((await body()).auto_first_duplicate_max,false);
  assert.equal((await body()).config_set_id,undefined);
  fields["#consumption-template-prefix"] = {value:"aipuzzlenoT， aipuzzlelessT、aipuzzlenoT"};
  fields["#consumption-template-suffix"] = {value:"s5,v2s5"};
  const focused = await body({focus_output_template_id:"one"});
  assert.deepEqual(focused.template_name_prefixes, ["aipuzzlenoT", "aipuzzlelessT"]);
  assert.deepEqual(focused.template_name_suffixes, ["s5", "v2s5"]);
  fields["#consumption-auto-first-max"].checked=true;
  assert.equal((await body()).auto_first_duplicate_max,true);
  assert.equal((await body({focus_output_template_id:"one",first_duplicate_max:7})).auto_first_duplicate_max,true);
  fields["#consumption-auto-first-max"].checked=false;
  fields["#consumption-fixed-first-max"]={checked:true};
  fields["#consumption-first-max-deduction-count"]={value:"2"};
  const specified = await body({focus_output_template_id:"one",first_duplicate_max:7});
  assert.equal(specified.auto_first_duplicate_max,false);
  assert.equal(specified.first_duplicate_structure_deduction_count,2);
  fields["#consumption-first-max-deduction-count"].value="2.5";
  await assert.rejects(body(), /1–12/);
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

test("only batch calibration reports progress while polling", async () => {
  const begin = source.indexOf("  async function requestConsumptionCalibration(");
  const end = source.indexOf("  async function runConsumptionCalibration(", begin);
  const records = [];
  const result = { rows: [{ output_template_id: "one" }] };
  const request = Function("api", "calibrationOperationIsCurrent", "renderConsumptionCalibrationProgress", "window",
    `${source.slice(begin, end)}return requestConsumptionCalibration;`)(
    async (_path, options) => options ? { status: "queued", job_id: "test-job" } : { status: "completed", result },
    () => true,
    record => records.push(record.status),
    { setTimeout: resolve => resolve() },
  );
  assert.deepEqual(await request({ focus_output_template_id: "one" }, {}), result);
  assert.deepEqual(records, []);
  assert.deepEqual(await request({}, {}), result);
  assert.deepEqual(records, ["queued", "completed"]);
});

test("focused calibration forwards the user's fallback decision without showing global progress", async () => {
  const begin = source.indexOf("  async function requestConsumptionCalibration(");
  const end = source.indexOf("  async function runConsumptionCalibration(", begin);
  const result = { rows: [{ output_template_id: "one" }] };
  for (const proceed of [false, true]) {
    const requests = [];
    const request = Function("api", "calibrationOperationIsCurrent", "renderConsumptionCalibrationProgress", "window",
      `${source.slice(begin, end)}return requestConsumptionCalibration;`)(
      async (path, options) => {
        requests.push([path, options]);
        return requests.length === 1 ? {
          status: "awaiting_confirmation", job_id: "job",
          fallback_confirmation: { template_id: "one", duplicate_index: 2, reason: "geometry: 牌面不一致" },
        } : { status: "completed", result };
      },
      () => true,
      () => assert.fail("focused calibration must not show global progress"),
      { confirm: message => { assert.match(message, /one.*副本 2[\s\S]*牌面不一致/); return proceed; } },
    );
    assert.deepEqual(await request({ focus_output_template_id: "one" }, {}), proceed ? result : null);
    assert.deepEqual(requests[1], ["/parameters/consumption-calibration-jobs/job/fallback", {
      method: "POST", body: { proceed },
    }]);
  }
});
