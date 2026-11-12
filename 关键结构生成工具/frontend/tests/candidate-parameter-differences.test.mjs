import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";

const app = fs.readFileSync(new URL("../assets/app.js", import.meta.url), "utf8");
const start = app.indexOf("  function candidateParameterRows(");
const end = app.indexOf("  function openCandidateParameterDialog(", start);
const helpers = start < 0 ? {} : Function(`${app.slice(start, end)}; return { candidateParameterComparison };`)();
const snapshot = (limit, span = 5) => ({
  tower_mode: "disallow", layout: "single", units: [{ unit_id: "U1", step: {kind:"exact",value:5} }],
  generation: {total_percent_limit:limit, max_structure_layer_span:span, candidate_count:6, attempt_budget:2000},
  unit_settings: [{type_count:{kind:"range",minimum:3,maximum:5},uniform_groups_per_type:0,groups_per_type:[]}],
});

test("info highlights actual changed values and names the uniform reference", () => {
  assert.equal(typeof helpers.candidateParameterComparison, "function");
  const generated = { configured_parameters:snapshot(40), uniform_parameters:snapshot(35), uses_independent_parameters:true };
  const result = helpers.candidateParameterComparison(generated, [generated]);
  const differences = result.rows.filter(row => row.changed);
  assert.deepEqual(differences.map(row => row.label), ["结构块数量上限"]);
  assert.equal(differences[0].value, "40%");
  assert.deepEqual(differences[0].comparisons, [{label:"统一方案",value:"35%"}]);
});

test("retained candidates show differences against the other candidate batch", () => {
  const old = {candidate:{candidate_index:1}, configured_parameters:snapshot(35,5)};
  const current = {candidate:{candidate_index:2}, configured_parameters:snapshot(35,8)};
  const result = helpers.candidateParameterComparison(old,[old,current]);
  const differences = result.rows.filter(row => row.changed);
  assert.deepEqual(differences.map(row => row.label), ["结构块最大层间距"]);
  assert.equal(differences[0].value,"5 层");
  assert.deepEqual(differences[0].comparisons,[{label:"候选 02",value:"8 层"}]);
});

test("random candidate outcomes never highlight unchanged range configuration", () => {
  const a = {configured_parameters:snapshot(35),resolved_generation_options:{unit_options:[{type_count:3,groups_per_type:[2,1,1]}]}};
  const b = {configured_parameters:snapshot(35),resolved_generation_options:{unit_options:[{type_count:4,groups_per_type:[1,1,1,1]}]}};
  const result=helpers.candidateParameterComparison(a,[a,b]);
  assert.equal(result.rows.filter(row=>row.changed).length,0);
  assert.equal(result.rows.find(row=>row.label==="谜题 1 · 结构块种类数").value,"3–5 种");
  assert.equal(result.rows.find(row=>row.label==="谜题 1 · 每类组数").value,"自动均衡（每类至少 1 组，差值≤3）");
});
