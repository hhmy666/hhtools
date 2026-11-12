"use strict";

const assert = require("node:assert/strict");
const vm = require("../../frontend/assets/view-model.js");
const fs = require("node:fs");
const path = require("node:path");
const sandbox = require("node:vm");

function testTemplateIdsKeepExactIdentityAndDeduplicatePastedBatches() {
  assert.deepEqual(vm.parseTemplateIds(" mxd0057v1\nhr0029，hr0044;hr0029；mxd5010 \t"),
    ["mxd0057v1", "hr0029", "hr0044", "mxd5010"]);
  assert.deepEqual(vm.parseTemplateIds("a,A,a_m.bytes,../a"), ["a", "A", "a_m.bytes", "../a"]);
  assert.deepEqual(vm.parseTemplateIds(" \n，；"), []);
}

function testHelpSearchIncludesAllChaptersBodyAndAliases() {
  const context = {window: {}};
  sandbox.runInNewContext(fs.readFileSync(path.join(__dirname, "../../frontend/assets/help-content.js"), "utf8"), context);
  const topics = context.window.TowerReducerHelp;
  assert.equal(topics.length, 10);
  assert.ok(vm.searchHelpTopics(topics, "增加Btile工具").some(topic => topic.id === "ordinary_add"));
  assert.deepEqual(Array.from(vm.searchHelpTopics(topics, "五五开"), topic => topic.id), ["config"]);
  assert.deepEqual(Array.from(vm.searchHelpTopics(topics, "审计记录未落盘"), topic => topic.id), ["sync", "faq"]);
  assert.ok(vm.searchHelpTopics(topics, "修改路径").some(topic => topic.id === "start"));
  assert.deepEqual(Array.from(vm.searchHelpTopics(topics, "绝对不存在的关键词XYZ")), []);
  assert.deepEqual(vm.searchHelpTopics(topics, ""), []);
  assert.equal(vm.searchHelpTopics([{title: "一", body: "<p>LOWER</p>", aliases: ["检索"]}], "lower 检索").length, 1);
  assert.equal(vm.searchHelpTopics([{title: "一", body: "<p>LOWER</p>", aliases: ["检索"]}], "lower 其他").length, 0);
}

function testImportModeChangeInvalidatesParsedBatchForEveryInputMethod() {
  const source = fs.readFileSync(path.join(__dirname, "../../frontend/assets/app.js"), "utf8");
  const code = source.slice(source.indexOf("function setImportMode("), source.indexOf("async function importTemplates("));
  const state = {importBusy: false, importMode: "ids", importId: "parsed-old-ids"};
  const nodes = new Map();
  const node = id => nodes.get(id) || (nodes.set(id, {hidden: false}), nodes.get(id));
  const resetCode = source.slice(source.indexOf("function resetImportSession("), source.indexOf("function setImportBusy("));
  const setMode = new Function("state", "all", "byId", "clearFeedback", "updateImportControls", "icon", `${resetCode}\n${code}\nreturn setImportMode;`)(state, () => [], node, () => {}, () => {}, () => "");
  setMode("files");
  assert.equal(state.importId, "", "Keyboard and pointer tab changes must both require a new parse");
  assert.equal(node("import-ids-panel").hidden, true);
  assert.equal(node("import-files-panel").hidden, false);
  state.importId = "parsed-files";
  setMode("files");
  assert.equal(state.importId, "parsed-files", "Selecting the current tab must preserve the parsed batch");
  state.importBusy = true;
  setMode("ids");
  assert.equal(state.importId, "parsed-files");
  assert.equal(state.importMode, "files");
}

function testFileQueueCannotChangeDuringParseAndRemovingAllInvalidatesImport() {
  const source = fs.readFileSync(path.join(__dirname, "../../frontend/assets/app.js"), "utf8");
  const code = source.slice(source.indexOf("function updateSelectedFiles("), source.indexOf("function templateIds("));
  const state = {importBusy: true, selectedFiles: [{name: "a_m.bytes"}], importId: "parsed-original"};
  const list = {innerHTML: "original"};
  const resetCode = source.slice(source.indexOf("function resetImportSession("), source.indexOf("function setImportBusy("));
  const update = new Function("state", "byId", "updateImportControls", "icon", `${resetCode}\n${code}\nreturn updateSelectedFiles;`)(state, () => list, () => {}, () => "");
  update([], false);
  assert.equal(state.selectedFiles.length, 1, "In-flight removal must not change the input batch");
  assert.equal(state.importId, "parsed-original");
  assert.equal(list.innerHTML, "original");
  state.importBusy = false;
  update([], false);
  assert.equal(state.selectedFiles.length, 0);
  assert.equal(state.importId, "", "Removing the last file must not leave a usable old import");
}

function testFailedImportPlaceholderCannotBlockVerifiedSiblingExport() {
  const source = fs.readFileSync(path.join(__dirname, "../../frontend/assets/app.js"), "utf8");
  const code = source.slice(source.indexOf("function currentSourceTemplateIds("), source.indexOf("function validateNamingControl("));
  const state = {currentJob: {templates: [{templateId: "hr0029"}, {templateId: "mxd0057v1"}, {templateId: "—", status: "failed"}]}};
  const sourceIds = new Function("state", "asArray", "asObject", "asText", `${code}\nreturn currentSourceTemplateIds;`)(state, value => value || [], value => value || {}, value => typeof value === "string" ? value : "");
  const result = vm.validateNamingRule("lessT{原模板ID}", sourceIds());
  assert.equal(result.ok, true, "A failed import without a real template ID must not block exporting verified templates");
  assert.deepEqual(result.outputIds, ["lessThr0029", "lessTmxd0057v1"]);
}

function testMixedResultRenderingUsesRealIdsAndKeepsImportFailureAction() {
  const source = fs.readFileSync(path.join(__dirname, "../../frontend/assets/app.js"), "utf8");
  const code = source.slice(source.indexOf("function renderParameterAdvice("), source.indexOf("async function loadJob("));
  const job = vm.normalizeJob({job_id: "mixed", status: "partial_failed", artifacts: [{id:"art-good"}], templates: {
    good: {source_template_id: "hr0029", status: "verified", plan_summary: {new_id: "lessThr0029", move_count: 12}, artifacts: [{id: "art-good"}]},
    bad: {original_names: ["missing_template_test_m.bytes"], status: "failed", stage: "import", attempt_id: 1, error: {message: "未找到"}},
  }});
  const nodes = new Map();
  const byId = id => nodes.get(id) || (nodes.set(id, {}), nodes.get(id));
  const context = {VM: vm, byId, setStatusPill: () => {}, statusLabel: value => value, statusTone: () => "neutral", icon: () => "", escapeHTML: value => String(value), asObject: value => value && typeof value === "object" ? value : {}, asText: (value, fallback = "") => typeof value === "string" && value ? value : fallback, asNumber: (value, fallback = 0) => Number.isFinite(Number(value)) ? Number(value) : fallback};
  context.TERMINAL_TEMPLATE_STATES = new Set(["verified", "failed", "no_op", "interrupted"]);
  sandbox.runInNewContext(code, context);
  context.renderJobResults(job);
  assert.match(byId("result-table-body").innerHTML, /lessThr0029/);
  assert.match(byId("result-table-body").innerHTML, /查看 missing_template_test 的生成与验收明细/);
  assert.match(byId("result-table-body").innerHTML, /data-reimport-template/);
  assert.equal(byId("download-zip-button").disabled, false);
}

function testImportCanonicalShapeAndNoTower() {
  const result = vm.normalizeImported({
    import_id: "import-1",
    files: [
      {
        file_id: "towered",
        original_name: "a_m.bytes",
        status: "ready",
        inspection: {
          template_id: "a",
          total_tiles: 60,
          a_count: 36,
          b_count: 24,
          tower_count: 2,
          tower_tile_count: 18,
          tower_ratio: 0.3,
          stacks: [{count: 6}, {count: 12}],
          symmetry: ["left_right"],
          issues: [],
        },
      },
      {
        file_id: "plain",
        status: "no_tower",
        inspection: {template_id: "plain", total_tiles: 45, stacks: []},
      },
    ],
  });
  assert.equal(result.importId, "import-1");
  assert.deepEqual(result.files[0].towerStacks, [6, 12]);
  assert.equal(result.files[0].tileCount, 60);
  assert.equal(result.files[1].status, "no_tower");
  assert.equal(result.files[1].tileCount, 45);
}

function testImportInspectionPresentationAndBlockingIssues() {
  const result = vm.normalizeImported({
    import_id: "import-issues",
    files: [{
      file_id: "towered",
      original_name: "a_m.bytes",
      status: "ready",
      inspection: {
        template_id: "a",
        total_tiles: 60,
        a_count: 36,
        b_count: 24,
        tower_count: 2,
        tower_tile_count: 18,
        tower_ratio: 0.3,
        stacks: [{count: 6}, {count: 12}],
        non_tower_layers: [0, 1, 2],
        symmetry: ["horizontal"],
        profiles: [
          [[-1, -1], [-1, 1], [1, -1], [1, 1]],
          [[-1, 0], [1, 0]],
        ],
        issues: [
          {severity: "warning", message: "存在少量非主堆叠形式"},
          {severity: "error", message: "上层 Tile 缺少直接下层"},
        ],
      },
    }],
  });
  const file = result.files[0];
  const presentation = vm.describeImportInspection(file);
  assert.equal(file.hasBlockingIssues, false, "A parsed source is imported as-is, not rejected by geometry rules");
  assert.equal(file.hasWarnings, true);
  assert.deepEqual(presentation.facts, [
    "A / B Tile：36 / 24",
    "塔：2 堆，共 18 张（6 / 12）",
    "塔占比：30.0%",
    "非塔层：0 / 1 / 2",
    "对称：左右轴对称",
    "堆叠：一压四 / 一压二",
  ]);
  assert.deepEqual(presentation.issues, [
    {severity: "warning", message: "存在少量非主堆叠形式"},
    {severity: "error", message: "上层 Tile 缺少直接下层"},
  ]);
}

function testFailedImportWithoutInspectionDoesNotInventStructureFacts() {
  const result = vm.normalizeImported({
    import_id: "import-failed",
    files: [{
      file_id: "broken",
      original_name: "broken_m.bytes",
      status: "failed",
      error: {code: "invalid_template", message: "无法解析模板文件"},
    }],
  });
  const file = result.files[0];
  const presentation = vm.describeImportInspection(file);
  assert.equal(file.hasInspection, false);
  assert.equal(file.aCount, null);
  assert.equal(file.bCount, null);
  assert.equal(file.towerCount, null);
  assert.equal(file.towerTileCount, null);
  assert.equal(file.ratio, null);
  assert.deepEqual(presentation, {available: false, facts: [], issues: []});
}

function testJobCanonicalShapeAndVerification() {
  const result = vm.normalizeJob({
    job_id: "job-1",
    revision: 4,
    status: "succeeded",
    progress: {completed_templates: 1, total_templates: 1, stage: "register_output"},
    templates: {
      key1: {
        key: "key1",
        source_template_id: "mxd0057v1",
        original_names: ["mxd0057v1_m.bytes"],
        status: "verified",
        plan_summary: {
          new_id: "lessTmxd0057v1",
          source_sha256: "source-sha",
          output_sha256: "output-sha",
          source_tower_counts: [6, 9, 9, 6],
          retained_counts: [2, 3, 3, 2],
          source_tower_tile_count: 30,
          result_tower_tile_count: 10,
          source_tower_ratio: 0.375,
          result_tower_ratio: 0.125,
          move_type: "A",
          move_count: 20,
          added_layers: 2,
          strategy: "top_first",
          seed: 7,
          destinations: [{x: 2, y: 3, z: 6}],
          recommendations: ["保持当前参数"],
        },
        verification: {status: "passed", ok: true, facts: {tower_ratio: 0.125}, issues: []},
        artifacts: [{id: "art-1", new_id: "lessTmxd0057v1", sha256: "output-sha"}],
      },
    },
  }, "fallback-job");
  const row = result.templates[0];
  assert.equal(result.id, "job-1");
  assert.equal(row.outputId, "lessTmxd0057v1");
  assert.equal(row.oldRatio, 0.375);
  assert.equal(row.newRatio, 0.125);
  assert.deepEqual(row.retained, [2, 3, 3, 2]);
  assert.equal(row.moveType, "A");
  assert.equal(row.moveCount, 20);
  assert.equal(row.verification.ok, true);
  assert.equal(row.sourceSha256, "source-sha");
  assert.equal(row.outputSha256, "output-sha");
}

function testMissingResultNumbersRemainMissingAndOutcomeCountsAreExclusive() {
  const failed = vm.normalizeTemplate({
    status: "failed",
    plan_summary: {
      source_tower_ratio: 0.4,
      result_tower_ratio: null,
      result_tower_tile_count: null,
    },
  }, "failed-key", 0);
  assert.equal(failed.newRatio, null);
  assert.equal(failed.resultTowerTileCount, null);

  const summary = vm.summarizeTemplateOutcomes([
    {status: "verified", isNoTower: true, moveCount: 0},
    {status: "verified", isNoTower: false, moveCount: 8},
    {status: "failed", isNoTower: false, moveCount: 0},
  ]);
  assert.deepEqual(summary, {passed: 1, failed: 1, noOp: 1});
  assert.equal(summary.passed + summary.failed + summary.noOp, 3);
}

function testRerunRetainedCountsMatchEverySourceTower() {
  assert.deepEqual(vm.validateRetainedCounts([3, 4, 4, 3], [6, 9, 9, 6]), {ok: true, message: ""});
  assert.deepEqual(vm.validateRetainedCounts([3, 4], [6, 9, 9, 6]), {
    ok: false,
    message: "需要填写 4 个保留数，当前为 2 个。",
  });
  assert.deepEqual(vm.validateRetainedCounts([7, 4, 4, 3], [6, 9, 9, 6]), {
    ok: false,
    message: "第 1 堆保留数只可为 0–6 的整数。",
  });
}

function testSameJobResponsesCannotMoveRevisionBackwards() {
  const base = {
    requestJobId: "job-1",
    currentJobId: "job-1",
    requestGeneration: 4,
    activeGeneration: 4,
    currentJobIdForRevision: "job-1",
    currentRevision: 8,
  };
  assert.equal(vm.shouldAcceptJobResponse({...base, responseRevision: 7}), false);
  assert.equal(vm.shouldAcceptJobResponse({...base, responseRevision: 8}), true);
  assert.equal(vm.shouldAcceptJobResponse({...base, responseRevision: 9}), true);
  assert.equal(vm.shouldAcceptJobResponse({...base, currentJobId: "job-2", responseRevision: 9}), false);
  assert.equal(vm.shouldAcceptJobResponse({...base, activeGeneration: 5, responseRevision: 9}), false);
}

function testSyncPlanKeepsParentTransactionStateAndReportUsesRowStatus() {
  const entries = vm.normalizeSyncPlanEntries([{
    status: "blocked",
    filename: "lessTa_m.bytes",
    error: {message: "其中一个目标不可写入"},
    targets: [
      {binding_key: "template", classification: "create"},
      {binding_key: "tile", classification: "blocked"},
    ],
  }]);
  assert.equal(entries.length, 2);
  assert.equal(entries[0].parentStatus, "blocked");
  assert.equal(entries[0].classification, "blocked");
  assert.equal(entries[0].targetClassification, "create");
  assert.equal(entries[1].classification, "blocked");

  assert.deepEqual(vm.summarizeCommitItems([
    {status: "synced", targets: [{backup: {relative_path: "backups/backup-a_m.bytes"}}]},
    {status: "skipped_same_sha", targets: [{classification: "same_sha"}]},
    {status: "failed", targets: [{classification: "create"}]},
  ]), {written: 1, skipped: 1, failed: 1, rolledBack: 0, backups: 1});
}

function testSyncReportKeepsMixedRolledBackAndSyncedResults() {
  const outcome = vm.interpretSyncReport({
    status: "partial_failed",
    rows: [
      {status: "rolled_back", filename: "lessTa_m.bytes"},
      {status: "synced", filename: "lessTb_m.bytes"},
    ],
  });
  assert.deepEqual(outcome.summary, {
    written: 1,
    skipped: 0,
    failed: 0,
    rolledBack: 1,
    backups: 0,
  });
  assert.equal(outcome.recoveryRequired, false);
  assert.equal(outcome.allWriteAttemptsRolledBack, false);
  assert.equal(outcome.hasMixedCommittedAndRolledBack, true);
  assert.equal(outcome.closeDialog, true);
}

function testSyncAuditFailurePreservesActualRowsAndRequiresRecovery() {
  const outcome = vm.interpretSyncReport({
    status: "audit_failed",
    rows: [
      {status: "synced", filename: "lessTa_m.bytes"},
      {status: "synced", filename: "lessTb_m.bytes"},
    ],
  });
  assert.deepEqual(outcome.summary, {
    written: 2,
    skipped: 0,
    failed: 0,
    rolledBack: 0,
    backups: 0,
  });
  assert.equal(outcome.recoveryRequired, true);
  assert.equal(outcome.auditFailed, true);
  assert.equal(outcome.closeDialog, false);

  const recovery = vm.interpretSyncReport({
    status: "recovery_required",
    rows: [{status: "recovery_required", filename: "lessTc_m.bytes"}],
  });
  assert.equal(recovery.recoveryRequired, true);
  assert.equal(recovery.auditFailed, false);
  assert.equal(recovery.consumePlan, true);
  assert.equal(recovery.closeDialog, false);
}

function testSyncConfirmationRequiredKeepsTheSamePlanRetryable() {
  const outcome = vm.interpretSyncReport({
    status: "confirmation_required",
    rows: [{
      status: "blocked",
      error: {
        code: "conflict_confirmation_required",
        evidence: {required_conflict_ids: ["conflict-1"]},
      },
    }],
  });
  assert.equal(outcome.confirmationRequired, true);
  assert.equal(outcome.consumePlan, false);
  assert.equal(outcome.closeDialog, false);
  assert.deepEqual(outcome.requiredConflictIds, ["conflict-1"]);
}

function testHistoryUsesOnlyRealBootstrapFields() {
  assert.deepEqual(vm.normalizeHistoryRows([{
    job_id: "job-1",
    status: "succeeded",
    revision: 7,
    template_count: 3,
    updated_at: "2026-10-12T06:00:00Z",
  }]), [{
    id: "job-1",
    updated: "2026-10-12T06:00:00Z",
    count: 3,
    status: "succeeded",
  }]);

  assert.deepEqual(vm.normalizeHistoryRows([{
    job_id: "job-1",
    status: "running",
    template_count: 2,
    updated_at: "2026-10-12T05:00:00Z",
  }], {
    id: "job-1",
    status: "succeeded",
    updatedAt: "2026-10-12T06:00:00Z",
    progress: {total: 3},
  }), [{
    id: "job-1",
    updated: "2026-10-12T06:00:00Z",
    count: 3,
    status: "succeeded",
  }]);

  assert.deepEqual(vm.normalizeHistoryRows([{
    job_id: "job-1",
    status: "running",
    template_count: 2,
    updated_at: "2026-10-12T05:00:00Z",
  }], {
    id: "job-1",
    status: "succeeded",
    progress: {total: 3},
  }), [{
    id: "job-1",
    updated: "2026-10-12T05:00:00Z",
    count: 3,
    status: "succeeded",
  }]);
}

function testCommitSummaryAndSafetyHelpers() {
  assert.deepEqual(vm.summarizeCommitItems([
    {status: "written", backup_path: "/tmp/a"},
    {status: "skipped_same_sha"},
    {status: "skipped_conflict"},
    {status: "failed"},
    {status: "rolled_back"},
  ]), {written: 1, skipped: 2, failed: 1, rolledBack: 1, backups: 1});
  assert.equal(vm.resolveCandidate(["/a"], ""), "/a");
  assert.equal(vm.resolveCandidate(["/a", "/b"], ""), "");
  assert.equal(vm.resolveCandidate(["/a", "/b"], "/bound/elsewhere"), "/bound/elsewhere");
  assert.equal(vm.nextPollDelay(1), 1800);
  assert.equal(vm.nextPollDelay(10), 8000);
  assert.notEqual(
    vm.planFingerprint({jobId: "job", revision: 1, parameters: {ratio: 0.25}}),
    vm.planFingerprint({jobId: "job", revision: 2, parameters: {ratio: 0.25}}),
  );
  assert.throws(() => vm.validateRatioPercent(100.1), /0.*100/);
  assert.equal(vm.validateRatioPercent(25), 25);
  assert.equal(vm.validateRatioPercent(27), 27);
  assert.equal(vm.validateRatioPercent(100), 100);
  assert.equal(vm.validateRatioRangePercent(0, 27, 35.4, 'source-a').ok, true);
  assert.equal(vm.validateRatioRangePercent(0, 35.4, 35.4, 'source-a').ok, true);
  const invalid = vm.validateRatioRangePercent(0, 36, 35.4, 'source-a');
  assert.equal(invalid.ok, false);
  assert.equal(invalid.field, 'ratio');
  assert.match(invalid.message, /source-a.*35\.4/);
  assert.equal(vm.validateRatioRangePercent(0, 0, 35.4).ok, true);
  assert.equal(vm.validateRatioRangePercent(27, 27, 35.4).ok, false);
  assert.equal(vm.validateRatioRangePercent(0, 101).ok, false);
}

function testNavigationTransitionRespectsReducedMotion() {
  assert.equal(vm.navigationScrollBehavior(false), "smooth");
  assert.equal(vm.navigationScrollBehavior(true), "auto");
}

function testReturningToConfigurationRequiresCurrentImport() {
  assert.deepEqual(vm.resolveFlowStep("2", ""), {
    step: "1",
    blocked: true,
    message: "历史任务的原始文件需重新选择并检查。",
  });
  assert.deepEqual(vm.resolveFlowStep(2, "import-current"), {
    step: "2",
    blocked: false,
    message: "",
  });
  assert.deepEqual(vm.resolveFlowStep(3, ""), {
    step: "3",
    blocked: false,
    message: "",
  });
}

function testNamingRuleMatchesBackendTokenAndAsciiIdContract() {
  assert.deepEqual(vm.validateNamingRule("lessT{原模板ID}", ["mxd0057v1"]), {
    ok: true,
    message: "",
    outputIds: ["lessTmxd0057v1"],
  });

  assert.deepEqual(vm.validateNamingRule("lessT", ["a", "b"]).outputIds, ["lessT", "lessTv2"]);
  for (const rule of ["{原模板ID}{原模板ID}", "x{other}", "{{原模板ID}}"]) {
    const result = vm.validateNamingRule(rule, ["mxd0057v1"]);
    assert.equal(result.ok, false, rule);
    assert.match(result.message, /一个.*\{原模板ID\}/);
  }

  for (const rule of ["中文{原模板ID}", "less T{原模板ID}", "less.T{原模板ID}"]) {
    const result = vm.validateNamingRule(rule, ["mxd0057v1"]);
    assert.equal(result.ok, false, rule);
    assert.match(result.message, /ASCII.*中文.*空格/);
  }

  const unchanged = vm.validateNamingRule("{原模板ID}", ["mxd0057v1"]);
  assert.equal(unchanged.ok, false);
  assert.match(unchanged.message, /不能与原模板 ID 相同/);

  const tooLong = vm.validateNamingRule(`${"a".repeat(181)}{原模板ID}`, ["mxd0057v1"]);
  assert.equal(tooLong.ok, false);
  assert.match(tooLong.message, /180 字节/);
}

testTemplateIdsKeepExactIdentityAndDeduplicatePastedBatches();
testHelpSearchIncludesAllChaptersBodyAndAliases();
testImportModeChangeInvalidatesParsedBatchForEveryInputMethod();
testFileQueueCannotChangeDuringParseAndRemovingAllInvalidatesImport();
testFailedImportPlaceholderCannotBlockVerifiedSiblingExport();
testMixedResultRenderingUsesRealIdsAndKeepsImportFailureAction();
testImportCanonicalShapeAndNoTower();
testImportInspectionPresentationAndBlockingIssues();
testFailedImportWithoutInspectionDoesNotInventStructureFacts();
testJobCanonicalShapeAndVerification();
testMissingResultNumbersRemainMissingAndOutcomeCountsAreExclusive();
testRerunRetainedCountsMatchEverySourceTower();
testSameJobResponsesCannotMoveRevisionBackwards();
testSyncPlanKeepsParentTransactionStateAndReportUsesRowStatus();
testSyncReportKeepsMixedRolledBackAndSyncedResults();
testSyncAuditFailurePreservesActualRowsAndRequiresRecovery();
testSyncConfirmationRequiredKeepsTheSamePlanRetryable();
testHistoryUsesOnlyRealBootstrapFields();
testCommitSummaryAndSafetyHelpers();
testNavigationTransitionRespectsReducedMotion();
testReturningToConfigurationRequiresCurrentImport();
testNamingRuleMatchesBackendTokenAndAsciiIdContract();
console.log("frontend view-model behavior: ok");
