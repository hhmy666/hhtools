"use strict";
const assert = require("node:assert/strict");
const test = require("node:test");
const vm = require("../../frontend/assets/view-model.js");
const fs = require("node:fs");
const path = require("node:path");
const sandbox = require("node:vm");

const parameters = {ratio: .25, strategy: "top_first", max_tiles_per_layer: 20, max_added_layers: 2, seed: 1, retained_counts: null};
function rowWith(schemes, status = "ready") {
  return vm.normalizeTemplate({parameters, status: "failed", plan_summary: {
    parameter_advice: {status, schemes}, recommendations: ["内部搜索 max_added_layers", "提高塔占比"]
  }}, "a", 0);
}
function scheme(changes, overrides = {}) {
  return {verified: true, title: "保留顶层优先", parameters: {...parameters, ...overrides}, changes,
    facts: {tower_ratio: .23, move_count: 12, added_layers: 2}};
}
function adviceView(row) {
  return typeof vm.parameterAdviceView === "function" ? vm.parameterAdviceView(row) : {schemes: [], message: ""};
}
test("verified schemes show Chinese control names and exact old-to-new values", () => {
  const row = rowWith([scheme([{field: "ratio", from: .25, to: .246}], {ratio: .246})]);
  const view = adviceView(row);
  assert.equal(view.schemes.length, 1);
  assert.deepEqual(view.schemes[0].adjustments, ["塔 Tile 占比上限：25% → 24.6%"]);
  assert.match(view.schemes[0].outcome, /23%/);
  assert.match(view.schemes[0].unchanged, /最多新增层数.*2/);
  assert.doesNotMatch([view.message, ...view.schemes.flatMap(item => [item.title, ...item.adjustments, item.unchanged, item.outcome])].join(" "), /max_added_layers|提高塔占比/);
});
test("unverified or ratio-increasing schemes cannot be presented as successful advice", () => {
  const raised = scheme([{field: "ratio", from: .25, to: .26}], {ratio: .26});
  const unverified = {...scheme([{field: "max_added_layers", from: 2, to: 3}], {max_added_layers: 3}), verified: false};
  assert.equal(adviceView(rowWith([raised, unverified])).schemes.length, 0);
});
test("old jobs and exhausted searches show honest next actions rather than raw search messages", () => {
  const legacy = vm.normalizeTemplate({status: "failed", parameters, plan_summary: {recommendations: ["内部字段21到68"]}}, "a", 0);
  assert.match(adviceView(legacy).message, /重新运行/);
  assert.match(adviceView(rowWith([], "limited")).message, /未找到.*已验收/);
  assert.doesNotMatch(adviceView(legacy).message, /21|68/);
});
test("both result and rerun advice render explicit values without duplicated technical recommendations", () => {
  const source = fs.readFileSync(path.join(__dirname, "../../frontend/assets/app.js"), "utf8");
  const start = source.indexOf("function renderParameterAdvice(");
  const row = rowWith([scheme([{field: "ratio", from: .25, to: .246}], {ratio: .246})]);
  let html = "";
  if (start >= 0) {
    const context = {VM: vm, escapeHTML: value => String(value)};
    sandbox.runInNewContext(source.slice(start, source.indexOf("\nfunction ", start + 1)), context);
    html = context.renderParameterAdvice(row, true);
  }
  assert.match(html, /25% → 24.6%/);
  assert.match(html, /data-fill-advice="0"/);
  assert.equal((html.match(/25% → 24.6%/g) || []).length, 1);
  assert.doesNotMatch(html, /max_added_layers|提高塔占比/);
});
test("filling an accepted scheme changes only structural inputs and does not submit or alter the saved job", () => {
  const source = fs.readFileSync(path.join(__dirname, "../../frontend/assets/app.js"), "utf8");
  const start = source.indexOf("function fillAdviceScheme(");
  const row = rowWith([scheme([{field: "ratio", from: .25, to: .246}], {ratio: .246})]);
  row.key = "a";
  const nodes = new Map();
  const node = id => nodes.get(id) || (nodes.set(id, {value: ""}), nodes.get(id));
  node("rerun-new-id").value = "my_custom_name";
  const context = {VM: vm, state: {rerunKey: "a", currentJob: {templates: [row]}}, byId: node,
    updateReplenishGuidance: () => {},
    clearInvalid: () => {}, showFeedback: (id, message) => { node(id).textContent = message; }};
  sandbox.runInNewContext(source.slice(start, source.indexOf("\nfunction ", start + 1)), context);
  context.fillAdviceScheme(0);
  assert.equal(node("rerun-ratio").value, 24.6);
  assert.equal(node("rerun-strategy").value, "top_first");
  assert.equal(node("rerun-max-layers").value, 2);
  assert.equal(node("rerun-layer-limit").value, 20);
  assert.equal(node("rerun-new-id").value, "my_custom_name");
  assert.equal(row.parameters.ratio, .25);
  assert.match(node("rerun-feedback").textContent, /重新生成并验收/);
});
