"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const sandbox = require("node:vm");
const test = require("node:test");
const VM = require("../../frontend/assets/view-model.js");
const source = fs.readFileSync(path.join(__dirname, "../../frontend/assets/app.js"), "utf8");

function functionSource(name) {
  const start = source.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `Missing application function ${name}`);
  const next = source.indexOf("\nfunction ", start + 1);
  return source.slice(start, next < 0 ? undefined : next);
}

function formContext(values = {}) {
  const nodes = new Map();
  let focused = "";
  const node = (id) => nodes.get(id) || (nodes.set(id, {id, value: "", hidden: false, invalid: false}), nodes.get(id));
  for (const [id, value] of Object.entries(values)) node(id).value = String(value);
  const context = {
    VM, state: {settings: {namingRule: "lessT{原模板ID}"}, overrides: {}, importedFiles: []}, byId: node,
    asText: (value, fallback = "") => typeof value === "string" && value ? value : fallback,
    asObject: (value) => value && typeof value === "object" ? value : {},
    asArray: (value) => Array.isArray(value) ? value : [],
    asNumber: (value, fallback = 0) => Number.isFinite(Number(value)) ? Number(value) : fallback,
    clearInvalid: () => nodes.forEach((item) => { item.invalid = false; }),
    setInvalid: (item, message) => { item.invalid = true; item.error = message; },
    focusFirstInvalid: () => {
      const invalid = [...nodes.values()].find((item) => item.invalid);
      if (invalid) focused = invalid.id;
      return Boolean(invalid);
    },
    all: () => [], updateParameterSummaries: () => {}, invalidatePlans: () => {},
    showToast: () => {}, clearFeedback: () => {}, openDialog: () => {},
    renderParameterAdvice: () => "", renderReplenishGuidance: () => "", updateReplenishGuidance: () => {},
    escapeHTML: (value) => String(value), showFeedback: () => {},
  };
  sandbox.createContext(context);
  return {context, node, focus: () => focused, load: (...names) => sandbox.runInContext(names.map(functionSource).join("\n"), context)};
}

test("recommendations use original non-tower counts instead of global minority counts", () => {
  for (const [a, b, expected] of [[12, 0, "A"], [0, 12, "B"], [18, 6, "A"], [6, 18, "B"]]) {
    const imported = VM.normalizeImported({files: [{inspection: {a_count: 90, b_count: 3, non_tower_a_count: a, non_tower_b_count: b}}]}).files[0];
    assert.equal(imported.nonTowerACount, a);
    assert.equal(imported.nonTowerBCount, b);
    const advice = VM.replenishRecommendation(imported);
    assert.equal(advice.type, expected);
    assert.match(advice.message, new RegExp(`推荐补 ${expected}.*非塔 A ${a} / B ${b}`));
  }
});

test("ties and missing legacy source evidence never select a recommendation", () => {
  const tie = VM.normalizeImported({files: [{inspection: {non_tower_a_count: 9, non_tower_b_count: 9}}]}).files[0];
  assert.equal(VM.replenishRecommendation(tie).type, null);
  assert.match(VM.replenishRecommendation(tie).message, /均可/);
  const legacy = VM.normalizeTemplate({plan_summary: {source_tile_counts: {A: 90, B: 3}}}, "old", 0);
  assert.equal(legacy.nonTowerACount, null);
  assert.equal(legacy.nonTowerBCount, null);
  assert.equal(VM.replenishRecommendation(legacy).type, null);
  const row = VM.normalizeTemplate({source: {non_tower_a_count: 0, non_tower_b_count: 15}}, "new", 0);
  assert.equal(VM.replenishRecommendation(row).type, "B");
  const publicJob = VM.normalizeJob({templates: [{non_tower_a_count: 21, non_tower_b_count: 6}]}).templates[0];
  assert.equal(VM.replenishRecommendation(publicJob).type, "A");
});

test("ratio range has an inclusive lower bound, strict upper bound and zero clear exception", () => {
  assert.deepEqual(VM.validateRatioRangePercent(10, 25), {ok: true, field: "", message: ""});
  assert.equal(VM.validateRatioRangePercent(0, 0).ok, true);
  for (const [min, max] of [[-1, 25], [26, 25], [25, 25], [5, 0], [NaN, 25]]) {
    const result = VM.validateRatioRangePercent(min, max);
    assert.equal(result.ok, false);
    assert.equal(result.field, "min_ratio");
  }
  assert.equal(VM.validateRatioRangePercent(0, 26).ok, true);
  assert.equal(VM.validateRatioRangePercent(0, 101).field, "ratio");
});

test("batch requests retain the lower bound and automatic fill default", () => {
  const view = formContext({"min-target-ratio": 10, "target-ratio": 25, "placement-strategy": "balanced", "layer-limit": 24, "max-new-layers": 3, "random-seed": 8});
  view.load("collectDefaults", "collectOverrides");
  const defaults = view.context.collectDefaults();
  assert.equal(defaults.min_ratio, .1);
  assert.equal(defaults.replenish_type, "auto");
  view.context.state.overrides.a = {min_ratio: .12, replenish_type: "B", seed: 9};
  view.node("tower-mode").value = "automatic";
  const overrides = view.context.collectOverrides();
  assert.deepEqual(JSON.parse(JSON.stringify(overrides.a)), {min_ratio: .12, replenish_type: "B", seed: 9});
});

test("rerun preserves the actual lower bound and explicit fill type in its request", () => {
  const view = formContext({"min-target-ratio": 0, "target-ratio": 25, "placement-strategy": "top_first", "layer-limit": 20, "max-new-layers": 2, "random-seed": 1});
  const row = VM.normalizeTemplate({parameters: {min_ratio: .1, ratio: .2, replenish_type: "B", strategy: "bottom_first", max_tiles_per_layer: 18, max_added_layers: 1, seed: 7, naming_rule: "custom{原模板ID}"}, source: {non_tower_a_count: 30, non_tower_b_count: 6}}, "a", 0);
  view.context.state.currentJob = {templates: [row]};
  view.load("collectDefaults", "openRerunEditor", "rerunDefaults");
  view.context.openRerunEditor("a");
  assert.equal(view.node("rerun-min-ratio").value, 10);
  assert.equal(view.node("rerun-replenish-type").value, "B");
  const defaults = view.context.rerunDefaults({});
  assert.equal(defaults.min_ratio, .1);
  assert.equal(defaults.replenish_type, "B");
  assert.equal(defaults.naming_rule, "custom{原模板ID}");
});

test("invalid batch lower bound reopens its editor and focuses the lower field", () => {
  const view = formContext({"min-target-ratio": 25, "target-ratio": 25, "layer-limit": 20, "max-new-layers": 2, "placement-strategy": "top_first", "random-seed": 1, "tower-mode": "automatic"});
  view.context.state.activeOverride = "a";
  view.load("showBatchEditor", "validateConfiguration");
  assert.equal(view.context.validateConfiguration({}), false);
  assert.equal(view.focus(), "min-target-ratio");
  assert.equal(view.node("batch-parameter-fields").hidden, false);
});

test("invalid rerun lower bound focuses the lower field and blocks a request", () => {
  const view = formContext({"rerun-min-ratio": 10, "rerun-ratio": 10, "rerun-layer-limit": 20, "rerun-max-layers": 2, "rerun-seed": 1, "rerun-new-id": "", "rerun-retained-counts": "", "rerun-replenish-type": "A"});
  view.load("rerunDefaults");
  assert.equal(view.context.rerunDefaults({}), null);
  assert.equal(view.focus(), "rerun-min-ratio");
});

test("adopting a recommendation changes only the chosen template fill type", () => {
  const view = formContext();
  view.context.state.activeOverride = "a";
  view.context.state.importedFiles = [{fileId: "a", nonTowerACount: 18, nonTowerBCount: 6}];
  view.context.state.overrides = {a: {min_ratio: .1, ratio: .2, replenish_type: "auto", seed: 7}, b: {replenish_type: "B"}};
  view.load("applyReplenishRecommendation");
  view.context.applyReplenishRecommendation("override");
  assert.equal(view.context.state.overrides.a.replenish_type, "A");
  assert.equal(view.context.state.overrides.a.min_ratio, .1);
  assert.equal(view.context.state.overrides.a.seed, 7);
  assert.equal(view.context.state.overrides.b.replenish_type, "B");
});

test("new absent source type warning depends on known global source counts", () => {
  const row = VM.normalizeTemplate({plan_summary: {source_tile_counts: {A: 60, B: 0}}, source: {non_tower_a_count: 36, non_tower_b_count: 0}}, "a", 0);
  assert.match(VM.replenishTypeWarning(row, "B"), /手动.*副本基准/);
  assert.equal(VM.replenishTypeWarning(row, "A"), "");
  assert.equal(VM.replenishTypeWarning(row, "auto"), "");
  assert.equal(VM.replenishTypeWarning(VM.normalizeTemplate({}, "legacy", 0), "B"), "");
});

test("parameter summaries include both ratio endpoints and explicit fill type", () => {
  const view = formContext();
  view.load("parameterSummary");
  const summary = view.context.parameterSummary({min_ratio: .1, ratio: .25, replenish_type: "B", strategy: "top_first", max_tiles_per_layer: 20, max_added_layers: 2});
  assert.match(summary, /10%.*≤.*塔占比.*<25%/);
  assert.match(summary, /补 B/);
});

test("switching template editors preserves each range and fill type; reset clears only one override", () => {
  const view = formContext({"min-target-ratio": 5, "target-ratio": 25, "placement-strategy": "balanced", "layer-limit": 24, "max-new-layers": 3, "random-seed": 8, "tower-mode": "automatic"});
  view.context.document = {querySelector: () => ({})};
  view.context.CSS = {escape: (value) => value};
  view.node("parameter-override-editor").querySelector = () => ({value: ""});
  view.context.state.importedFiles = [{fileId: "a", templateId: "a"}, {fileId: "b", templateId: "b"}];
  view.load("showBatchEditor", "setOverrideMode");
  view.context.setOverrideMode("a", "custom");
  assert.equal(view.context.state.overrides.a.min_ratio, .05);
  assert.equal(view.context.state.overrides.a.replenish_type, "auto");
  Object.assign(view.context.state.overrides.a, {min_ratio: .1, replenish_type: "A"});
  view.context.setOverrideMode("b", "custom");
  Object.assign(view.context.state.overrides.b, {min_ratio: .15, replenish_type: "B"});
  view.context.setOverrideMode("a", "custom");
  assert.equal(view.context.state.overrides.a.min_ratio, .1);
  assert.equal(view.node("override-replenish-a").value, "A");
  view.context.setOverrideMode("a", "reset");
  assert.equal(view.context.state.overrides.a, undefined);
  assert.equal(view.context.state.overrides.b.min_ratio, .15);
  assert.equal(view.context.state.overrides.b.replenish_type, "B");
  assert.equal(view.node("batch-parameter-fields").hidden, false);
});

test("a hidden invalid template lower bound opens that template and focuses its lower field", () => {
  const view = formContext({"min-target-ratio": 0, "target-ratio": 25, "layer-limit": 20, "max-new-layers": 2});
  view.context.state.overrides = {a: {min_ratio: .2, ratio: .2, replenish_type: "B"}};
  view.context.setOverrideMode = (fileId) => { view.context.state.activeOverride = fileId; };
  view.load("validateConfiguration");
  assert.equal(view.context.validateConfiguration({}), false);
  assert.equal(view.context.state.activeOverride, "a");
  assert.equal(view.focus(), "override-min-ratio-a");
});

test("filling older advice preserves manually chosen lower bound and fill type", () => {
  const parameters = {min_ratio: .1, ratio: .25, replenish_type: "B", strategy: "top_first", max_tiles_per_layer: 20, max_added_layers: 2, seed: 1, retained_counts: null};
  const legacyProposal = {ratio: .2, strategy: "top_first", max_tiles_per_layer: 20, max_added_layers: 2, seed: 1, retained_counts: null};
  const row = VM.normalizeTemplate({parameters, plan_summary: {parameter_advice: {schemes: [{verified: true, parameters: legacyProposal, changes: [{field: "ratio", from: .25, to: .2}], facts: {tower_ratio: .18}}]}}}, "a", 0);
  const view = formContext({"rerun-min-ratio": 12, "rerun-replenish-type": "A", "rerun-new-id": "custom"});
  view.context.state.currentJob = {templates: [row]};
  view.context.state.rerunKey = "a";
  view.load("fillAdviceScheme");
  view.context.fillAdviceScheme(0);
  assert.equal(view.node("rerun-min-ratio").value, "12");
  assert.equal(view.node("rerun-replenish-type").value, "A");
  assert.equal(view.node("rerun-new-id").value, "custom");
  assert.equal(view.node("rerun-ratio").value, 20);
});

test("filling complete advice keeps manually selected range floor and fill unless explicitly adjusted", () => {
  const parameters = {min_ratio: .1, ratio: .25, replenish_type: "B", strategy: "top_first", max_tiles_per_layer: 20, max_added_layers: 2, seed: 1, retained_counts: null};
  const row = VM.normalizeTemplate({parameters, plan_summary: {parameter_advice: {schemes: [{verified: true, parameters: {...parameters, ratio: .2}, changes: [{field: "ratio", from: .25, to: .2}], facts: {tower_ratio: .18}}]}}}, "a", 0);
  const view = formContext({"rerun-min-ratio": 12, "rerun-replenish-type": "A"});
  view.context.state.currentJob = {templates: [row]};
  view.context.state.rerunKey = "a";
  view.load("fillAdviceScheme");
  view.context.fillAdviceScheme(0);
  assert.equal(view.node("rerun-min-ratio").value, "12");
  assert.equal(view.node("rerun-replenish-type").value, "A");
});

test("advice that would put the upper bound below the retained lower bound is not presented", () => {
  const parameters = {min_ratio: .1, ratio: .25, replenish_type: "B", strategy: "top_first", max_tiles_per_layer: 20, max_added_layers: 2, seed: 1};
  const row = VM.normalizeTemplate({parameters, plan_summary: {parameter_advice: {schemes: [{verified: true, parameters: {...parameters, ratio: .09}, changes: [{field: "ratio", from: .25, to: .09}], facts: {tower_ratio: .08}}]}}}, "a", 0);
  assert.equal(VM.parameterAdviceView(row).schemes.length, 0);
});
