import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
const source = fs.readFileSync(new URL("../assets/app.js", import.meta.url), "utf8");
const begin = source.indexOf("  function calibrationSeedSimulationCopy(");
const end = source.indexOf("  function renderConsumptionCalibrationResult(", begin);
const helpers = Function(`const formatCalibrationNumber=(n)=>String(n);${source.slice(begin,end)}return {expected:calibrationExpectedConsumptionCopy,source:calibrationExpectedSourceCopy,summary:typeof calibrationReviewNotice==='function'?calibrationReviewNotice:null,recommendation:calibrationRecommendationCopy,bounds:typeof calibrationSearchBoundsCopy==='function'?calibrationSearchBoundsCopy:null,parameters:typeof calibrationParameterReference==='function'?calibrationParameterReference:null,simulation:calibrationSyntheticCopy};`)();
test("a target copied as a fallback never appears to be an exact consumption prediction", () => {
  const expected = {value:500,difference:0,source:"source_target_conservative_baseline",structure_correction:{applied:false,in_scope:false,reasons:["outside_level_range"]}};
  assert.doesNotMatch(helpers.expected(expected), /500|\+0/);
  assert.match(helpers.expected(expected), /无法预估|未预估/);
  assert.match(helpers.expected({value:510,difference:10,source:"source_target_empirical_structure_correction",structure_correction:{applied:true}}), /510.*\+10/);
});
test("a calculated transfer estimate is visible even outside the training band", () => {
  const expected = {value:1618.09,target:1532.74,difference:85.35,source:"source_target_transfer_structure_estimate",estimate_mode:"transfer_estimate",structure_correction:{applied:true,in_scope:false}};
  assert.match(helpers.expected(expected),/1618.09.*\+85.35/);
  assert.equal(helpers.source(expected),"跨关段预估");
  assert.equal(helpers.recommendation({expected_consumption:expected,recommendation:{status:"transfer_estimate_close",label:"预估接近",relative_gap:0.0557,is_recommended:false}}),"预估接近");
  assert.equal(helpers.recommendation({expected_consumption:{...expected,value:767.57,difference:-765.17},recommendation:{status:"transfer_estimate_far",label:"偏差较大",relative_gap:0.4992,is_recommended:false}}),"偏差较大");
});
test("the transfer notice states training provenance and the authorized source assumption once", () => {
  const expected = {value:1618.09,source:"source_target_transfer_structure_estimate",estimate_mode:"transfer_estimate",structure_correction:{applied:true,in_scope:false}};
  const copy = helpers.summary({coverage:{history_level_range:{start:1000,end:1200}},consumption_prediction_model:{scope:{level_index_min:200,level_index_max:300}},rows:[{consumption_anchor:{kind:"processed_source_assumed_equal_online",observed:false},duplicates:Array(10).fill({expected_consumption:expected})}]});
  assert.match(copy,/1000.*1200/);
  assert.match(copy,/200.*300.*训练|训练.*200.*300/);
  assert.match(copy,/跨关段预估/);
  assert.match(copy,/加工源.*暂按.*线上目标/);
  assert.match(copy,/实测.*校验/);
  assert.doesNotMatch(copy,/仅支持|无法预估|已验证|已实测/);
  assert.ok(copy.length<160);
});
test("duplicate-only anchors and manual targets are described without claiming online observations", () => {
  const copy = helpers.summary({rows:[{duplicates:[{
    expected_consumption:{value:1618.09,estimate_mode:"transfer_estimate",structure_correction:{applied:true,in_scope:false}},
    consumption_anchor:{kind:"processed_source_assumed_equal_online",target_source:"user_override",observed:false},
    target_consumption:{source:"user_override",value:1500},
  }]}]});
  assert.match(copy,/加工源.*暂按本次目标/);
  assert.doesNotMatch(copy,/按原线上目标|已实测/);
});
test("a partial in-band result still discloses its processed-source assumption", () => {
  const copy = helpers.summary({rows:[{consumption_anchor:{kind:"processed_source_assumed_equal_online"},duplicates:[
    {expected_consumption:{value:800,estimate_mode:"in_scope_model",structure_correction:{applied:true,in_scope:true}}},
    {expected_consumption:{value:null,estimate_mode:"unavailable",structure_correction:{applied:false,reasons:["source_target_unavailable"]}}},
  ]}]});
  assert.match(copy,/加工源.*暂按原线上目标/);
  assert.match(copy,/缺少必要/);
  assert.ok(copy.length<160);
});
test("shared estimation limitations are summarized once with a next action", () => {
  assert.equal(typeof helpers.summary,"function");
  const duplicate = {expected_consumption:{source:"source_target_conservative_baseline",structure_correction:{applied:false,reasons:["outside_level_range"]}}};
  const copy = helpers.summary({rows:[{duplicates:Array(10).fill(duplicate)},{duplicates:Array(10).fill(duplicate)}],consumption_model:{scope:{level_index_min:200,level_index_max:300}}});
  assert.match(copy,/200.*300/);
  assert.match(copy,/人工|确认|复核/);
  assert.ok(copy.length<160);
  assert.doesNotMatch(copy,/模型不适用|合成回退|邻居|因果/);
});
test("uploaded history range is distinct from the built-in consumption model range", () => {
  const duplicate = {expected_consumption:{source:"source_target_conservative_baseline",structure_correction:{applied:false,reasons:["outside_level_range"]}}};
  const copy = helpers.summary({
    rows:[{duplicates:[duplicate]}],
    coverage:{history_level_range:{start:300,end:400}},
    consumption_prediction_model:{scope:{level_index_min:200,level_index_max:300}},
  });
  assert.match(copy,/CSV.*300.*400/);
  assert.match(copy,/内置.*200.*300/);
  assert.match(copy,/仅按模拟排序/);
  assert.match(copy,/不代表.*消耗.*匹配/);
  assert.doesNotMatch(copy,/现有测试数据仅覆盖/);
  assert.ok(copy.length < 160);
});
test("missing uploaded range is never invented, and unestimated results are only simulation-ranked", () => {
  const expected = {source:"source_target_conservative_baseline",structure_correction:{applied:false,reasons:["outside_level_range"]}};
  const copy = helpers.summary({rows:[{duplicates:[{expected_consumption:expected}]}]});
  assert.doesNotMatch(copy,/CSV.*300.*400/);
  assert.equal(helpers.recommendation({expected_consumption:expected,synthetic_difficulty:{distance:1},recommendation:{is_recommended:true}}),"仅模拟排序");
  assert.equal(helpers.recommendation({expected_consumption:expected,synthetic_difficulty:{distance:null}}),"待人工确认");
});
test("processed and online parameters explain the actual A/B search limits separately", () => {
  assert.equal(typeof helpers.bounds,"function");
  const comparison = {status:"different",processed_template_id:"lessThr0127",online_template_id:"hr0127",processed_dup:["9|1|100|9"],first_duplicate_max:9};
  const copy = helpers.bounds(comparison);
  assert.match(copy,/各档 A\/B 上限/);
  assert.match(copy,/首副本 A≤9、B≤9/);
  assert.match(copy,/目标消耗仍取 hr0127/);
  assert.equal(helpers.bounds({...comparison,status:"same"}),"");
  assert.doesNotMatch(helpers.bounds({...comparison,processed_dup:undefined}),/NaN|undefined/);
  assert.match(helpers.bounds(comparison,true),/目标消耗优先取手动值/);
  assert.doesNotMatch(helpers.bounds(comparison,true),/目标消耗仍取/);
});
test("the source parameter displayed matches the reference used for Seed preservation", () => {
  assert.equal(typeof helpers.parameters,"function");
  assert.deepEqual(helpers.parameters({original_dup:"12|1|100|6",ranking_reference_dup:"9|3|100|9",seed_changed:false}),{source:"9|3|100|9",online:"12|1|100|6"});
  assert.deepEqual(helpers.parameters({original_dup:"9|1|100|9",ranking_reference_dup:"9|1|100|9"}),{source:"9|1|100|9",online:""});
  assert.deepEqual(helpers.parameters({original_dup:"9|1|100|9"}),{source:"9|1|100|9",online:""});
});
test("missing or invalid simulation distances never display as a perfect zero", () => {
  for (const distance of [null, undefined, NaN, Infinity, false, ""]) {
    assert.equal(helpers.simulation({distance}), "未评估");
  }
  assert.equal(helpers.simulation({distance:0}), "0");
  assert.equal(helpers.simulation({distance:0.12}), "0.12");
});
test("review export does not export copied targets as predictions and preserves the processed-source cap", () => {
  const state = {consumptionCalibrationFingerprint:"current", consumptionCalibrationResult:{rows:[{
    source_template_id:"lessTmxd0003", output_template_id:"aipuzzlelessTmxd0003s5", canonical_template_id:"mxd0003",
    source_parameter_comparison:{status:"different",processed_template_id:"lessTmxd0003",online_template_id:"mxd0003",first_duplicate_max:5},
    duplicates:[{duplicate_index:1,original_dup:"12|1|100|6",ranking_reference_dup:"9|3|100|9",expected_consumption:{value:500,difference:0,source:"source_target_conservative_baseline"},recommendation:{relative_gap:0,is_recommended:false},target_consumption:{value:500},synthetic_difficulty:{distance:1,stable_tiebreak:0.25}}],
  }]}};
  let csv;
  const start = source.indexOf("  function downloadCalibrationReviewCsv(");
  const finish = source.indexOf("  function downloadCalibrationDraftCsv(",start);
  const download = Function("state","consumptionCalibrationFingerprint","downloadCsvFile",`const formatCalibrationNumber=n=>String(n);${source.slice(begin,end)}${source.slice(start,finish)}return downloadCalibrationReviewCsv;`)(state,()=>"current",(headers,rows)=>{csv={headers,rows};});
  download();
  assert.equal(csv.rows[0][csv.headers.indexOf("预期人均消耗_估算")], "");
  assert.equal(csv.rows[0][csv.headers.indexOf("目标偏差比例")], "");
  assert.equal(csv.rows[0][csv.headers.indexOf("对应CSV模板ID")], "mxd0003");
  assert.equal(csv.rows[0][csv.headers.indexOf("加工源首副本上限")], 5);
  assert.equal(csv.rows[0][csv.headers.indexOf("原DupPara")], "12|1|100|6");
  assert.equal(csv.rows[0][csv.headers.indexOf("搜索源参数")], "9|3|100|9");
  assert.equal(csv.rows[0][csv.headers.indexOf("模拟次级差异_诊断")], 0.25);
});
test("review export preserves numeric transfer estimates and their model and assumption provenance", () => {
  const state = {consumptionCalibrationFingerprint:"current",consumptionCalibrationResult:{
    consumption_prediction_model:{model_id:"consumption-structure-correction-s5-200-300-v1",scope:{level_index_min:200,level_index_max:300}},
    rows:[{source_template_id:"lessThr0127",output_template_id:"aipuzzlelessThr0127s5",canonical_template_id:"hr0127",consumption_anchor:{kind:"processed_source_assumed_equal_online",source_dup:"9|1|100|9",observed:false},duplicates:[{
      duplicate_index:1,original_dup:"12|1|100|6",ranking_reference_dup:"9|1|100|9",recommended_dup:"9|1|100|9",target_consumption:{value:1532.74},
      expected_consumption:{value:1618.09,difference:85.35,source:"source_target_transfer_structure_estimate",estimate_mode:"transfer_estimate",range:[1103,2392],interval_kind:"training_residual_reference",structure_correction:{applied:true,in_scope:false}},
      recommendation:{status:"transfer_estimate_close",relative_gap:0.0557,is_recommended:false},
    }]}],
  }};
  let csv;
  const start = source.indexOf("  function downloadCalibrationReviewCsv(");
  const finish = source.indexOf("  function downloadCalibrationDraftCsv(",start);
  const download = Function("state","consumptionCalibrationFingerprint","downloadCsvFile",`const formatCalibrationNumber=n=>String(n);${source.slice(begin,end)}${source.slice(start,finish)}return downloadCalibrationReviewCsv;`)(state,()=>"current",(headers,rows)=>{csv={headers,rows};});
  download();
  const cell = name => csv.rows[0][csv.headers.indexOf(name)];
  assert.equal(cell("预期人均消耗_估算"),1618.09);
  assert.equal(cell("目标偏差比例"),0.0557);
  assert.equal(cell("预估方式"),"跨关段预估");
  assert.equal(cell("消耗模型版本"),"consumption-structure-correction-s5-200-300-v1");
  assert.equal(cell("模型训练关段"),"200–300");
  assert.equal(cell("加工源消耗按本次目标_假设"),"是");
  assert.equal(cell("预估参照参数"),"9|1|100|9");
  assert.match(cell("区间说明"),/历史误差参考.*非.*实测保证/);
  assert.equal(cell("历史误差参考上界")-cell("历史误差参考下界"),1289);
});
test("parameter draft export retains the model version and source-target assumption", () => {
  const state = {consumptionCalibrationFingerprint:"current",consumptionCalibrationResult:{
    consumption_prediction_model:{model_id:"consumption-structure-correction-s5-200-300-v1",scope:{level_index_min:200,level_index_max:300}},
    rows:[{output_template_id:"aipuzzlelessThr0127s5",source_template_id:"lessThr0127",canonical_template_id:"hr0127",
      consumption_anchor:{kind:"processed_source_assumed_equal_online",observed:false},
      duplicates:[{recommended_dup:"9|10|100|9",expected_consumption:{value:1618.09,estimate_mode:"transfer_estimate",structure_correction:{applied:true,in_scope:false}}}],
    }],
  }};
  let csv;
  const start = source.indexOf("  function downloadCalibrationDraftCsv(");
  const finish = source.indexOf("  function switchParameterSource(", start);
  const download = Function("state","consumptionCalibrationFingerprint","downloadCsvFile",`const formatCalibrationNumber=n=>String(n);${source.slice(begin,end)}${source.slice(start,finish)}return downloadCalibrationDraftCsv;`)(state,()=>"current",(headers,rows)=>{csv={headers,rows};});
  download();
  const cell = name => csv.rows[0][csv.headers.indexOf(name)];
  assert.equal(cell("DupPara1"),"9|10|100|9");
  assert.equal(cell("预估方式"),"跨关段预估");
  assert.equal(cell("消耗模型版本"),"consumption-structure-correction-s5-200-300-v1");
  assert.equal(cell("模型训练关段"),"200–300");
  assert.equal(cell("加工源消耗按本次目标_假设"),"是");
  assert.equal(csv.rows[0].length,csv.headers.length);
  state.consumptionCalibrationResult.rows[0].duplicates[0].expected_consumption = {value:null,estimate_mode:"unavailable",model_id:"consumption-structure-correction-s5-200-300-v1",structure_correction:{applied:false}};
  download();
  assert.equal(cell("预估方式"),"未预估");
  assert.equal(cell("消耗模型版本"),"consumption-structure-correction-s5-200-300-v1");
  state.consumptionCalibrationResult.rows[0].duplicates = [];
  download();
  assert.equal(cell("消耗模型版本"),"consumption-structure-correction-s5-200-300-v1");
});
