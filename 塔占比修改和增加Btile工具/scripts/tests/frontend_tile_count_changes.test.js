"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const sandbox = require("node:vm");
const vm = require("../../frontend/assets/view-model.js");
const preview = require("../../frontend/assets/template-preview.js");
const appSource = fs.readFileSync(path.join(__dirname, "../../frontend/assets/app.js"), "utf8");

function templateWith(plan = {}, error = {}) {
  return {
    key: "hr0127", source_template_id: "hr0127", status: "verified", stage: "register_output",
    plan_summary: {
      new_id: "lessThr0127", source_tower_ratio: 37 / 108, result_tower_ratio: 25 / 108,
      move_type: "B", move_count: 12, added_layers: 1, strategy: "top_first",
      source_tile_counts: {A: 69, B: 39}, removed_tile_counts: {A: 12, B: 0},
      added_tile_counts: {A: 0, B: 12}, result_tile_counts: {A: 57, B: 51},
      tile_count_changes: {A: -12, B: 12}, ...plan,
    },
    error, verification: {status: "passed", ok: true, facts: {}, issues: []},
    artifacts: [{id: "artifact-hr0127", new_id: "lessThr0127"}],
  };
}

function renderResults(template) {
  const nodes = new Map();
  const node = id => nodes.get(id) || (nodes.set(id, {textContent: "", innerHTML: ""}), nodes.get(id));
  const context = {
    VM: vm, byId: node, setStatusPill() {}, statusLabel: value => value, statusTone: () => "neutral",
    icon: () => "", escapeHTML: value => String(value),
    asObject: value => value && typeof value === "object" ? value : {},
    asText: (value, fallback = "") => typeof value === "string" && value ? value : fallback,
    asNumber: (value, fallback = 0) => Number.isFinite(Number(value)) ? Number(value) : fallback,
    TERMINAL_TEMPLATE_STATES: new Set(["verified", "failed", "no_op", "interrupted"]),
  };
  sandbox.runInNewContext(appSource.slice(appSource.indexOf("function renderParameterAdvice("), appSource.indexOf("async function loadJob(")), context);
  context.renderJobResults(vm.normalizeJob({job_id: "counts", status: "succeeded", templates: [template], artifacts: template.artifacts}));
  return node("result-table-body").innerHTML;
}
test('failed reduction places the aligned manual recovery action directly after rerun, never on verified rows',()=>{
 const failed={...templateWith(),status:'failed',stage:'placement',artifacts:[]};
 const html=renderResults(failed);
 assert.match(html,/单独调整并重新运行<\/button><button[^>]*data-manual-recovery="hr0127">删除所有塔并手动补牌/);
 assert.doesNotMatch(renderResults(templateWith()),/data-manual-recovery/);
});
test('expanded search explains ongoing work instead of rendering a failed result',()=>{
 const html=renderResults({...templateWith(),status:'generating',stage:'placement_expanded',artifacts:[]});
 assert.match(html,/正在继续搜索其他合法摆法/);assert.doesNotMatch(html,/data-manual-recovery/);
});
test('source warnings never imply a failed row already has an exportable artifact',()=>{
 const warnings=[{code:'inherited_full_cover',x:4,y:4,lower_z:2,upper_z:3,lower_index:0,upper_index:1}];
 const failed={...templateWith(),status:'failed',artifacts:[],source_geometry_warnings:warnings};
 const html=renderResults(failed);
 assert.match(html,/原模板存在一压一：第3、4层\(4, 4\)。生成时可原样保留/);
 assert.doesNotMatch(html,/一压一已保留|不影响导出/);
 const passed=renderResults({...templateWith(),source_geometry_warnings:warnings,output_geometry_warnings:warnings});
 assert.match(passed,/原模板一压一已保留/);assert.match(passed,/不影响导出/);
});
test('removal-only recovery reports tower removal rather than no movement or a stale automatic fill',()=>{
 const row=vm.normalizeTemplate(templateWith({manual_recovery:true,move_count:0,removed_tile_counts:{A:3,B:0},added_tile_counts:{A:0,B:0}}),'hr0127',0);
 assert.equal(vm.tileMovementView(row).summary,'清塔移除 A 3 张 → 手动补入 0 张');
});

test("conversion results identify removed and added types and show the actual A/B change", () => {
  const row = vm.normalizeTemplate(templateWith(), "hr0127", 0);
  assert.deepEqual(row.removedTileCounts, {A: 12, B: 0});
  assert.deepEqual(row.addedTileCounts, {A: 0, B: 12});
  assert.deepEqual(row.tileCountChanges, {A: -12, B: 12});
  const html = renderResults(templateWith());
  assert.match(html, /移除 A 12 张 → 补入 B 12 张/);
  assert.match(html, /A 减少 12 张，B 增加 12 张/);
  assert.match(html, /A\/B 总量：69 \/ 39 → 57 \/ 51/);
});

test("removing both tower types shows their real counts rather than the replenishment type", () => {
  const html = renderResults(templateWith({
    removed_tile_counts: {A: 9, B: 3}, result_tile_counts: {A: 60, B: 48},
    tile_count_changes: {A: -9, B: 9},
  }));
  assert.match(html, /移除 A 9 张、B 3 张 → 补入 B 12 张/);
  assert.match(html, /A 减少 9 张，B 增加 9 张/);
});

test("same-type moves keep the cell compact and explain unchanged type totals in details", () => {
  const html = renderResults(templateWith({
    source_tile_counts: {A: 108, B: 0}, move_type: "A", added_tile_counts: {A: 12, B: 0},
    result_tile_counts: {A: 108, B: 0}, tile_count_changes: {A: 0, B: 0},
  }));
  assert.match(html, /搬移 A 12 张/);
  assert.match(html, /A\/B 总量不变：108 \/ 0/);
  assert.doesNotMatch(html, /减少 0 张|增加 0 张|移除 A 12 张 → 补入 A 12 张/);
});

test("historical results without count evidence keep their legacy display and do not infer a conversion", () => {
  const legacy = templateWith();
  legacy.plan_summary = {new_id: "lessThr0127", move_type: "A", move_count: 12, added_layers: 1};
  const row = vm.normalizeTemplate(legacy, "hr0127", 0);
  assert.equal(row.tileCountChanges, null);
  const html = renderResults(legacy);
  assert.match(html, />A 12 张<\//);
  assert.doesNotMatch(html, /移除|补入|减少|增加|A\/B 总量/);
});

test("preview coordinates show an actual source-to-result type conversion for a changed index", () => {
  const nodes = new Map();
  const node = id => nodes.get(id) || (nodes.set(id, {value: "all", checked: true, innerHTML: "", textContent: ""}), nodes.get(id));
  const state = {templatePreview: {
    source: {template_id: "old", towers: [], tiles: [
      {index: 4, tile_id: -10, x: 6, y: 8, z: 4, changed: true},
      {index: 5, tile_id: -11, x: 8, y: 8, z: 4, changed: true},
    ]},
    output: {template_id: "new", towers: [], tiles: [
      {index: 4, tile_id: -11, x: 2, y: 2, z: 5, changed: true},
      {index: 5, tile_id: -11, x: 4, y: 2, z: 5, changed: true},
    ]}, changed_count: 2,
  }};
  const context = {state, byId: node, window: {TowerTemplatePreview: preview}, updatePreviewLayerControls() {}, escapeHTML: value => String(value)};
  sandbox.runInNewContext(appSource.slice(appSource.indexOf("function renderTemplatePreview("), appSource.indexOf("function updatePreviewLayerControls(")), context);
  context.renderTemplatePreview();
  assert.match(node("preview-moves-body").innerHTML, /<td>4<\/td><td>A → B<\/td><td>6, 8, 4<\/td><td>2, 2, 5<\/td>/);
  assert.match(node("preview-moves-body").innerHTML, /<td>5<\/td><td>B<\/td>/);
});

test("failure recovery uses backend correction actions without replacing them with generic manual checking", () => {
  const row = vm.normalizeTemplate({status: "failed", parameters: {ratio: .25}, plan_summary: {parameter_advice: {status: "not_applicable", schemes: []}}, error: {
    code: "final_type_counts_not_multiple_of_three", message: "最终 A/B 数量不满足 3 的倍数。",
    recommendations: ["恢复自动分配后重新生成，或按已验收方案调整逐塔保留数。"],
  }}, "a", 0);
  const advice = vm.parameterAdviceView(row);
  assert.equal(advice.message, "恢复自动分配后重新生成，或按已验收方案调整逐塔保留数。");
  assert.doesNotMatch(advice.message, /人工检查模板/);
});

test("legacy cancelled same-type restriction directs a rerun with the existing parameters", () => {
  const row = vm.normalizeTemplate({status: "failed", error: {code: "single_move_type_unavailable"}}, "a", 0);
  assert.match(vm.parameterAdviceView(row).message, /单独调整并重新运行/);
  assert.match(vm.parameterAdviceView(row).message, /保留原参数/);
});
