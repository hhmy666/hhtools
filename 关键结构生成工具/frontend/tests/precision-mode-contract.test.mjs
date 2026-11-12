import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";

const html = fs.readFileSync(new URL("../index.html", import.meta.url), "utf8");
const app = fs.readFileSync(new URL("../assets/app.js", import.meta.url), "utf8");
const css = fs.readFileSync(new URL("../assets/styles.css", import.meta.url), "utf8");

function loadPrecisionToggle(state) {
  const start = app.indexOf("  function applyPrecisionDefaults");
  const end = app.indexOf("\n  function renderConfigurationSummary", start);
  assert.notEqual(start, -1, "precision defaults helper must exist");
  assert.notEqual(end, -1, "precision mode toggle must remain independently testable");
  return Function(
    "state",
    "clone",
    "invalidateGenerationForPlanChange",
    "editBatchPlan",
    "syncPrecisionModeUi",
    "toast",
    `"use strict";${app.slice(start, end)};return setPrecisionMode;`,
  )(state, (value) => JSON.parse(JSON.stringify(value)), () => {}, () => {}, () => {}, () => {});
}

test("configuration header exposes precision mode before returning to import", () => {
  const precision = html.indexOf('id="precision-mode-toggle"');
  const back = html.indexOf('id="back-to-import"');
  assert.ok(precision >= 0, "precision mode entry must exist");
  assert.ok(back > precision, "precision mode entry must sit left of return import");
  assert.match(html, /id="precision-mode-toggle"[^>]+aria-pressed="false"/);
  assert.ok(html.includes("进入精准模式"));
});

test("precision mode applies the requested search profile without changing other defaults", () => {
  assert.match(app, /precisionMode:\s*false/);
  assert.ok(app.includes("function setPrecisionMode"));
  assert.match(app, /plan\.candidateCount\s*=\s*3/);
  assert.match(app, /plan\.searchAttemptBudget\s*=\s*20000/);
  assert.match(app, /plan\.maxStructureLayerSpanKind\s*=\s*"absolute"/);
  assert.match(app, /plan\.maxStructureLayerSpan\s*=\s*5/);
  assert.match(app, /plan\.quality\.position\s*=\s*"unrestricted"/);
  assert.match(app, /plan\.quality\.symmetry\s*=\s*"off"/);
  assert.match(app, /plan\.quality\.density\s*=\s*"compact"/);
  assert.ok(app.includes("确认清单并进行精准生成"));
  assert.ok(app.includes("退出精准模式"));
});

test("precision mode is serialized explicitly only when enabled", () => {
  assert.match(app, /if \(plan\.precisionMode\) generation\.precision_mode = true;/);
  assert.ok(app.includes("generation.precision_mode"));
  assert.ok(css.includes("#precision-mode-toggle.is-active"));
});

test("missing precision evidence keeps its own failure status instead of budget or parameter conflict", () => {
  const kindStart = app.indexOf("  function failureKind(item) {");
  const kindEnd = app.indexOf("\n  function isDeterministicFailure", kindStart);
  const statusStart = app.indexOf("  function failureStatusCopy(item) {");
  const statusEnd = app.indexOf("\n  function failureAdjustmentHint", statusStart);
  assert.ok(kindStart >= 0 && kindEnd > kindStart && statusStart >= 0 && statusEnd > statusStart);
  const classify = Function(
    `"use strict";
    const itemErrorCode = item => item.error?.code || "";
    const itemErrorCopy = item => item.error?.message || "";
    const itemNeedsMoreCandidates = () => false;
    ${app.slice(kindStart, kindEnd)}
    function isDeterministicFailure(item) { return failureKind(item) === "parameter_conflict"; }
    function isBudgetFailure(item) { return failureKind(item) === "budget_exhausted"; }
    ${app.slice(statusStart, statusEnd)}
    return { failureKind, failureStatusCopy };`,
  )();
  const item = {
    status: "failed",
    candidates: [],
    error: {
      code: "NO_VALID_CANDIDATE",
      failure_kind: "missing_required_evidence",
      message: "精准模式缺少唯一可确认的原关卡 DupPara1–10",
    },
  };
  assert.equal(classify.failureKind(item), "missing_required_evidence");
  assert.equal(classify.failureStatusCopy(item), "缺少精准模式所需证据");
});

test("precision toggle restores the exact batch and template plans that existed before entry", () => {
  const batchPlan = {
    precisionMode: false,
    candidateCount: 8,
    searchAttemptBudget: 4000,
    maxStructureLayerSpanKind: "none",
    maxStructureLayerSpan: 9,
    quality: { position: "center", symmetry: "soft", density: "loose" },
  };
  const overridePlan = {
    precisionMode: false,
    candidateCount: 6,
    searchAttemptBudget: 2000,
    maxStructureLayerSpanKind: "none",
    maxStructureLayerSpan: 2,
    quality: { position: "edge", symmetry: "hard", density: "loose" },
  };
  const state = {
    batchPlan,
    precisionPlanSnapshot: null,
    templateRules: new Map([
      ["template-a", { mode: "replace", plan: overridePlan }],
    ]),
  };
  const toggle = loadPrecisionToggle(state);

  toggle(true);
  for (const plan of [batchPlan, overridePlan]) {
    assert.equal(plan.precisionMode, true);
    assert.equal(plan.candidateCount, 3);
    assert.equal(plan.searchAttemptBudget, 20000);
    assert.equal(plan.maxStructureLayerSpanKind, "absolute");
    assert.equal(plan.maxStructureLayerSpan, 5);
    assert.deepEqual(plan.quality, {
      position: "unrestricted",
      symmetry: "off",
      density: "compact",
    });
  }

  batchPlan.candidateCount = 2;
  batchPlan.searchAttemptBudget = 35000;
  batchPlan.quality.position = "balanced";
  overridePlan.maxStructureLayerSpan = 4;

  toggle(false);
  assert.deepEqual(state.batchPlan, {
    precisionMode: false,
    candidateCount: 8,
    searchAttemptBudget: 4000,
    maxStructureLayerSpanKind: "none",
    maxStructureLayerSpan: 9,
    quality: { position: "center", symmetry: "soft", density: "loose" },
  });
  assert.deepEqual(state.templateRules.get("template-a").plan, {
    precisionMode: false,
    candidateCount: 6,
    searchAttemptBudget: 2000,
    maxStructureLayerSpanKind: "none",
    maxStructureLayerSpan: 2,
    quality: { position: "edge", symmetry: "hard", density: "loose" },
  });
  assert.equal(state.precisionPlanSnapshot, null);
});

test("exiting a hydrated precision plan without a session snapshot restores normal defaults", () => {
  const state = {
    precisionPlanSnapshot: null,
    batchPlan: {
      precisionMode: true,
      candidateCount: 3,
      searchAttemptBudget: 20000,
      maxStructureLayerSpanKind: "absolute",
      maxStructureLayerSpan: 5,
      quality: { position: "unrestricted", symmetry: "off", density: "compact" },
    },
    templateRules: new Map(),
  };
  const toggle = loadPrecisionToggle(state);

  toggle(false);

  assert.equal(state.batchPlan.precisionMode, false);
  assert.equal(state.batchPlan.candidateCount, 6);
  assert.equal(state.batchPlan.searchAttemptBudget, 2000);
  assert.equal(state.batchPlan.maxStructureLayerSpanKind, "absolute");
  assert.equal(state.batchPlan.maxStructureLayerSpan, 5);
  assert.deepEqual(state.batchPlan.quality, {
    position: "unrestricted",
    symmetry: "off",
    density: "compact",
  });
});
