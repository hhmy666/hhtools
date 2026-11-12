"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const sandbox = require("node:vm");
const VM = require("../../frontend/assets/view-model.js");
const INCREASE = require("../../frontend/assets/increase-ui.js");
const app = fs.readFileSync(path.join(__dirname, "../../frontend/assets/app.js"), "utf8");
const sourceSha = "a".repeat(64);
const outputSha = "b".repeat(64);

function fixture() {
  const current = {...INCREASE.defaults(), target_mode: "count", operation: "both", stack_count: 2};
  const options = {...current, operation: "existing", height_mode: "manual", tower_counts: [27, 27],
    tower_positions: [{index: 0, x: 4, y: 6}, {index: 1, x: 12, y: 6}],
    tower_directions: [{index: 0, direction: 0}, {index: 1, direction: 0}], auto_translate: false};
  const scheme = {verified: true, title: "改为原塔加层", source_sha256: sourceSha, output_sha256: outputSha,
    parameters: {mode: "increase", increase: options}, options,
    changes: [
      {field: "operation", from: "both", to: "existing"},
      {field: "height_mode", from: "difference", to: "manual"},
      {field: "tower_counts", from: [], to: [27, 27]},
      {field: "tower_positions", from: [], to: [{index: 0, x: 4, y: 6}, {index: 1, x: 12, y: 6}]},
      {field: "tower_directions", from: [], to: [{index: 0, direction: 0}, {index: 1, direction: 0}]},
      {field: "auto_translate", from: true, to: false},
    ], facts: {added_tiles: 30, final_total: 90, final_tower_tiles: 54, tower_ratio: .6,
      new_tower_count: 0, extended_tower_count: 2, tower_counts: [27, 27], final_counts: [60, 30], translation: [0, 0]}};
  const raw = {key: "a", status: "failed", source_template_id: "templateA", source_sha256: sourceSha,
    parameters: {mode: "increase", increase: current, naming_rule: "custom{原模板ID}"},
    error: {code: "increase_new_tower_space", message: "当前区域没有可用的新塔位置。"},
    plan_summary: {operation: "increase", source_sha256: sourceSha,
      parameter_advice: {operation: "increase", status: "ready", source_sha256: sourceSha, schemes: [scheme]}}};
  return {raw, current, options, scheme, row: VM.normalizeTemplate(raw, "a", 0)};
}

function functionCode(name) {
  const start = app.indexOf(`function ${name}(`);
  assert.ok(start >= 0, `production function ${name} exists`);
  const end = app.indexOf("\nfunction ", start + 1);
  return app.slice(start, end < 0 ? undefined : end);
}

function context(names, row = fixture().row) {
  const nodes = new Map();
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, {id, value: "", hidden: false, open: false, dataset: {},
      textContent: "", innerHTML: "", querySelectorAll: () => [], closest: () => ({hidden: false}),
      focus() {this.focused = true;}, setAttribute() {}});
    return nodes.get(id);
  };
  const other = {key: "b", parameters: {mode: "increase", increase: {...INCREASE.defaults(), added_tiles: 48}}};
  const state = {currentJobId: "job-a", currentJob: {id: "job-a", templates: [row, other]}, rerunKey: "",
    importedFiles: [], importId: "", increaseOverrides: {b: other.parameters.increase}, increaseDefaults: INCREASE.defaults()};
  const ctx = {VM, INCREASE, state, structuredClone, byId: node, collectDefaults: () => ({}),
    escapeHTML: value => String(value).replaceAll('"', "&quot;"),
    asObject: value => value && typeof value === "object" ? value : {}, asArray: value => Array.isArray(value) ? value : [],
    asText: (value, fallback = "") => typeof value === "string" && value ? value : fallback,
    asNumber: (value, fallback = 0) => Number.isFinite(Number(value)) ? Number(value) : fallback,
    updateReplenishGuidance() {}, clearInvalid() {}, clearFeedback() {},
    showFeedback(id, message) {node(id).textContent = message;},
    showToast(message) {ctx.toast = message;}, openDialog(dialog) {dialog.open = true;},
    closeDialog(dialog) {dialog.open = false;}, queueMicrotask(callback) {callback();},
    window: {}, document: {createElement: () => node("created")},
    setStatusPill() {}, statusLabel: value => value, statusTone: () => "danger", icon: () => "",
    TERMINAL_TEMPLATE_STATES: new Set(["verified", "failed", "interrupted", "no_op"])};
  sandbox.createContext(ctx);
  const dependencies = names.includes("mountRerunIncreaseEditor") ? ["rerunIncreasePreviewFile", ...names] : names;
  sandbox.runInContext(dependencies.map(functionCode).join("\n"), ctx);
  return {ctx, state, node, row, other};
}

test("verified increase recovery shows exact values and actual new versus extended tower counts above 25%", () => {
  const {row} = fixture();
  const view = VM.parameterAdviceView(row);
  assert.equal(view.schemes.length, 1);
  assert.deepEqual(view.schemes[0].adjustments, [
    "增加方式：原塔加层 + 新建塔 → 原塔加层",
    "堆高关系：保留高低差值 → 手动高度",
    "逐塔最终张数：自动 → 27 / 27 张",
    "逐塔位置：自动 → 第 1 堆 (4, 6)；第 2 堆 (12, 6)",
    "逐塔方向：保留默认 → 第 1 堆 朝上；第 2 堆 朝上",
    "空间不足时推荐整体平移：开启 → 关闭",
  ]);
  assert.match(view.schemes[0].outcome, /新增 30 张.*新建 0 堆.*原塔加层 2 堆.*60%/);
  assert.match(view.schemes[0].unchanged, /期望新增张数 30 张/);
  assert.deepEqual(view.schemes[0].parameters.increase.tower_counts, [27, 27]);
});

test("increase recovery rejects unverified, stale, incomplete and unexplained option changes", () => {
  const mutations = [
    scheme => {scheme.verified = false;},
    scheme => {scheme.source_sha256 = "c".repeat(64);},
    scheme => {scheme.options.operation = "new";},
    scheme => {scheme.changes[0].from = "new";},
    scheme => {scheme.changes.push({field: "unknown", from: 0, to: 1});},
    scheme => {delete scheme.parameters.increase.tower_positions;},
    scheme => {scheme.changes = scheme.changes.filter(change => change.field !== "auto_translate");},
    scheme => {delete scheme.facts.new_tower_count;},
    scheme => {scheme.facts.final_total = NaN;},
    scheme => {scheme.parameters.increase.tower_positions = [null];},
    scheme => {scheme.changes[0] = null;},
  ];
  for (const mutate of mutations) {
    const {raw} = fixture();
    const scheme = structuredClone(raw.plan_summary.parameter_advice.schemes[0]);
    mutate(scheme);
    raw.plan_summary.parameter_advice.schemes = [scheme];
    assert.equal(VM.parameterAdviceView(VM.normalizeTemplate(raw, "a", 0)).schemes.length, 0);
  }
  const {raw} = fixture();
  raw.plan_summary.parameter_advice.schemes = [null, "unavailable"];
  assert.deepEqual(VM.parameterAdviceView(VM.normalizeTemplate(raw, "a", 0)).schemes, []);
});

test("increase recovery limits verified choices to two and distinguishes limited search from proof of no solution", () => {
  const {raw, scheme} = fixture();
  raw.plan_summary.parameter_advice.schemes = [scheme, scheme, scheme];
  assert.equal(VM.parameterAdviceView(VM.normalizeTemplate(raw, "a", 0)).schemes.length, 2);
  raw.plan_summary.parameter_advice.schemes = [];
  raw.plan_summary.parameter_advice.status = "limited";
  assert.match(VM.parameterAdviceView(VM.normalizeTemplate(raw, "a", 0)).message, /未找到.*已验收.*不能.*无解/);
});

test("towerless recovery explains why neither existing-tower extension nor the current new positions can work", () => {
  const {raw} = fixture();
  raw.plan_summary.original_tower_tiles = 0;
  raw.plan_summary.parameter_advice.status = "not_applicable";
  raw.plan_summary.parameter_advice.schemes = [];
  raw.error = {code: "increase_new_tower_positions_unavailable", message: "当前区域没有新增塔对称位置。",
    recommendations: ["当前设置不能新增塔堆；原塔能否加层请以已验收方案为准。"]};
  const message = VM.parameterAdviceView(VM.normalizeTemplate(raw, "a", 0)).message;
  assert.match(message, /原模板没有可加层的塔/);
  assert.match(message, /当前约束下.*没有合规的新塔位置.*无法完成本次增加/);
  assert.doesNotMatch(message, /原塔能否加层|以已验收方案为准/);
});

test("towerless recovery never turns an incomplete or different failure into proof that no new tower fits", () => {
  for (const code of ["increase_search_budget", "search_budget", "increase_verification_failed"]) {
    const {raw} = fixture();
    raw.plan_summary.parameter_advice.status = "not_applicable";
    raw.plan_summary.parameter_advice.schemes = [];
    raw.error = {code, message: "尚未完成本次尝试", recommendations: ["原塔能否加层请以已验收方案为准"]};
    const message = VM.parameterAdviceView(VM.normalizeTemplate(raw, "a", 0)).message;
    assert.match(message, /原模板没有可加层的塔/);
    assert.doesNotMatch(message, /没有合规的新塔位置|无法完成本次增加|原塔能否加层/);
    if (code.includes("search_budget")) assert.match(message, /未完成|尚未判断/);
  }
});

test("legacy known increase attempt-limit errors are normalized without changing reduce or unknown failures", () => {
  for (const code of ["search_budget", "increase_search_budget"]) {
    const raw = {status: "failed", parameters: {mode: "increase"},
      error: {code, message: "搜索预算耗尽，请增加预算", recommendations: ["增加搜索预算"]}};
    const row = VM.normalizeTemplate(raw, "a", 0);
    assert.doesNotMatch([row.error.message, ...row.error.recommendations, VM.parameterAdviceView(row).message].join(" "), /搜索预算/);
    assert.match(row.error.message, /未.*完成|暂未.*完整/);
    assert.equal(raw.error.message, "搜索预算耗尽，请增加预算");
  }
  for (const [mode, code] of [["reduce", "search_budget"], ["increase", "future_code"]]) {
    const row = VM.normalizeTemplate({parameters: {mode}, error: {code, message: "具体服务端原因"}}, "a", 0);
    assert.equal(row.error.message, "具体服务端原因");
  }
});

test("increase failed result rows expose the verified adjustments and a one-click current-template action", () => {
  const {row} = fixture();
  const {ctx, node} = context(["increaseAdviceContext", "renderParameterAdvice", "renderJobResults"], row);
  ctx.renderJobResults({id: "job-a", status: "failed", templates: [row], progress: {total: 1}, artifacts: []});
  const html = node("result-table-body").innerHTML;
  assert.match(html, /原塔加层 \+ 新建塔 → 原塔加层/);
  assert.match(html, /新建 0 堆.*原塔加层 2 堆/);
  assert.match(html, /data-apply-increase-advice="0"/);
  assert.match(html, /data-advice-template="a"/);
  assert.match(html, /一键应用推荐参数/);
});

test("one-click recovery fills only current rerun options, preserves naming, focuses next action and sends no request", () => {
  const {ctx, state, node, row, other} = context(["increaseAdviceContext", "renderParameterAdvice", "mountRerunIncreaseEditor", "openRerunEditor", "fillAdviceScheme", "applyIncreaseAdvice"]);
  const saved = JSON.stringify(row);
  const sibling = JSON.stringify(other);
  ctx.applyIncreaseAdvice("a", 0, ctx.increaseAdviceContext(row));
  assert.equal(node("rerun-dialog").open, true);
  assert.equal(node("rerun-recommendations").hidden, false);
  assert.equal(state.rerunIncrease.operation, "existing");
  assert.deepEqual(Array.from(state.rerunIncrease.tower_counts), [27, 27]);
  assert.equal(node("confirm-rerun-button").focused, true);
  assert.match(node("rerun-feedback").textContent, /仅当前模板.*重新生成并验收/);
  node("rerun-new-id").value = "my_custom_name";
  ctx.fillAdviceScheme(0);
  assert.equal(node("rerun-new-id").value, "my_custom_name");
  assert.equal(JSON.stringify(row), saved);
  assert.equal(JSON.stringify(other), sibling);
  assert.equal(state.increaseDefaults.operation, "new");
  assert.equal(state.increaseOverrides.a, undefined);
});

test("applied recovery offers preview only when the matching current import can open it", () => {
  for (const available of [true, false]) {
    const {ctx, state, node} = context(["increaseAdviceContext", "renderParameterAdvice", "mountRerunIncreaseEditor", "openRerunEditor", "fillAdviceScheme"]);
    state.importId = available ? "import-a" : "";
    state.importedFiles = [{fileId: "file-a", sourceSha256: sourceSha}];
    ctx.window.towerIncreasePreview = {open() {}};
    ctx.openRerunEditor("a");
    ctx.fillAdviceScheme(0);
    const message = node("rerun-feedback").textContent;
    if (available) {
      assert.match(message, /可先预览/);
    } else {
      assert.doesNotMatch(message, /预览/);
      assert.match(message, /请点击.*重新生成并验收.*完成后查看阵型对比/);
    }
  }
});

test("stale recommendation buttons and a job changing while rerun is open preserve edited inputs", () => {
  const {ctx, state, node, row} = context(["increaseAdviceContext", "renderParameterAdvice", "mountRerunIncreaseEditor", "openRerunEditor", "fillAdviceScheme", "applyIncreaseAdvice"]);
  const previous = ctx.increaseAdviceContext(row);
  row.attemptId += 1;
  ctx.applyIncreaseAdvice("a", 0, previous);
  assert.equal(node("rerun-dialog").open, false);
  ctx.openRerunEditor("a");
  state.rerunIncrease.added_tiles = 48;
  row.attemptId += 1;
  ctx.fillAdviceScheme(0);
  assert.equal(state.rerunIncrease.added_tiles, 48);
  assert.match(node("rerun-feedback").textContent, /已更新|已过时/);
  row.parameterAdvice.schemes = [];
  node("rerun-feedback").textContent = "";
  ctx.fillAdviceScheme(0);
  assert.match(node("rerun-feedback").textContent, /已更新|已过时/);
});

test("rerun preview applies only against the same current import snapshot and discards late callbacks", () => {
  const {ctx, state, node, row} = context(["increaseAdviceContext", "renderParameterAdvice", "mountRerunIncreaseEditor", "openRerunEditor", "fillAdviceScheme"]);
  state.importId = "import-a";
  state.importedFiles = [{fileId: "file-a", sourceSha256: sourceSha}];
  let opened;
  ctx.window.towerIncreasePreview = {open: value => {opened = value;}};
  ctx.openRerunEditor("a");
  node("rerun-increase-fields").onclick({target: {closest: () => ({})}});
  assert.equal(node("rerun-dialog").open, false, "preview hands off the shared modal rather than nesting dialogs");
  assert.equal(opened.file_id, "file-a");
  assert.equal(opened.import_id, "import-a");
  opened.onChange({...state.rerunIncrease, operation: "existing"});
  assert.equal(state.rerunIncrease.operation, "existing");
  opened.onClose();
  assert.equal(node("rerun-dialog").open, true);
  const late = opened;
  ctx.openRerunEditor("a");
  state.rerunIncrease.added_tiles = 48;
  late.onChange({...row.parameters.increase, added_tiles: 21});
  assert.equal(state.rerunIncrease.added_tiles, 48);
});

test("increase preview closes its prior context before notifying the owning rerun editor", async () => {
  const nodes = new Map();
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, {value: "1", innerHTML: "", textContent: "", classList: {toggle() {}},
      setAttribute() {}, querySelector() {return null;}, addEventListener(name, listener) {this[name] = listener;}});
    return nodes.get(id);
  };
  let closed = 0;
  const preview = INCREASE.mountPreview({document: {getElementById: node}, request: async () => ({schemes: []}),
    renderer: {boardMarkup: () => ""}, openDialog() {}, closeDialog() {}});
  preview.open({label: "a", import_id: "i", file_id: "f", options: INCREASE.defaults(), onClose() {closed += 1;}});
  node("increase-preview-dialog").close();
  assert.equal(closed, 1);
  await Promise.resolve();
  assert.equal(preview.flow.state.pending, false);
});

test("source SHA remains available for rerun preview matching when it belongs to the snapshot rather than a plan", () => {
  assert.equal(VM.normalizeImported({import_id: "i", files: [{file_id: "f", sha256: sourceSha}]}).files[0].sourceSha256, sourceSha);
  assert.equal(VM.normalizeTemplate({source_sha256: sourceSha}, "a", 0).sourceSha256, sourceSha);
});
