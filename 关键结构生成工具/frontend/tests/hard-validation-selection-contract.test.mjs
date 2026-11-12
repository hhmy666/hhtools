import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";

const app = fs.readFileSync(new URL("../assets/app.js", import.meta.url), "utf8");

const helperStart = app.indexOf("  function experienceAnalysis");
const helperEnd = app.indexOf("\n  function experienceArchetypeCopy", helperStart);
assert.notEqual(helperStart, -1, "experienceAnalysis must exist");
assert.notEqual(helperEnd, -1, "hard-validation helpers must remain testable");

const helperSource = app.slice(helperStart, helperEnd);
const helpers = Function(
  `"use strict";
  const firstDifficultyValue = (...values) => values.find((value) => value !== undefined && value !== null && value !== "");
  const difficultyNumber = (...values) => { const raw = firstDifficultyValue(...values); const value = Number(raw); return Number.isFinite(value) ? value : null; };
  const candidateDifficultyReport = (generated) => ({
    distance: null,
    target: "fidelity",
    qualified: generated?.candidate?.difficulty_simulation?.qualified ?? true,
    trendOk: generated?.candidate?.difficulty_simulation?.trend?.safety_qualified ?? true,
  });
  const difficultyTargetCopy = () => "难度保真";
  const candidateSummary = (generated) => generated?.board?.validation_summary || {};
  ${helperSource}
  return { experienceAnalysis, candidateExportEligibility };`,
)();

function candidate(status, passed, validationPassed = true) {
  return {
    candidate: {
      experience_analysis: {
        hard_validation: { status, passed },
      },
    },
    board: { validation_summary: { passed: validationPassed } },
  };
}

function syntheticCandidate() {
  const generated = candidate("proved", true);
  generated.candidate.experience_analysis.evidence_coverage = {
    overall_status: "inconclusive",
    materialization_fidelity: "synthetic",
  };
  return generated;
}

test("hard validation remains a strict three-state model", () => {
  const proved = helpers.experienceAnalysis(candidate("proved", true));
  const inconclusive = helpers.experienceAnalysis(candidate("inconclusive", false));
  const disproved = helpers.experienceAnalysis(candidate("disproved", false));

  assert.equal(proved.hardStatus, "proved");
  assert.equal(proved.hardPassed, true);
  assert.equal(inconclusive.hardStatus, "inconclusive");
  assert.equal(inconclusive.hardPassed, null, "inconclusive must not collapse into failed or passed");
  assert.equal(disproved.hardStatus, "disproved");
  assert.equal(disproved.hardPassed, false);
});

test("all non-proved rule outcomes remain manually exportable", () => {
  const proved = helpers.candidateExportEligibility(candidate("proved", true));
  const inconclusive = helpers.candidateExportEligibility(candidate("inconclusive", false));
  const disproved = helpers.candidateExportEligibility(candidate("disproved", false));

  assert.equal(proved.eligible, true);
  assert.equal(proved.autoEligible, true);
  assert.equal(proved.label, "硬校验已证明");
  assert.equal(inconclusive.eligible, true);
  assert.equal(inconclusive.autoEligible, false);
  assert.equal(inconclusive.manualOverrideRequired, true);
  assert.equal(inconclusive.status, "inconclusive");
  assert.match(inconclusive.label, /非推荐/);
  assert.match(inconclusive.label, /需人工确认/);
  assert.equal(disproved.eligible, true);
  assert.equal(disproved.autoEligible, false);
  assert.equal(disproved.manualOverrideRequired, true);
  assert.equal(disproved.status, "disproved");
  assert.match(disproved.label, /可人工导出/);
});

test("difficulty failures are not recommended but remain manually exportable", () => {
  const generated = candidate("proved", true);
  generated.candidate.difficulty_simulation = {
    qualified: false,
    trend: { safety_qualified: false },
  };
  const eligibility = helpers.candidateExportEligibility(generated);
  assert.equal(eligibility.eligible, true);
  assert.equal(eligibility.autoEligible, false);
  assert.equal(eligibility.manualOverrideRequired, true);
  assert.equal(eligibility.status, "difficulty_not_met");
  assert.match(eligibility.label, /可人工导出/);
});

test("synthetic difficulty evidence is not recommended but remains manually exportable", () => {
  const eligibility = helpers.candidateExportEligibility(syntheticCandidate());
  assert.equal(eligibility.eligible, true);
  assert.equal(eligibility.autoEligible, false);
  assert.equal(eligibility.manualOverrideRequired, true);
  assert.equal(eligibility.status, "inconclusive");
  assert.match(eligibility.reason, /人工确认/);
});

test("selection, confirmation, stale-state cleanup, and launch paths preserve user choice", () => {
  assert.match(app, /availableByWork[\s\S]*filter\(\(candidate\) => candidateExportEligibility\(candidate\)\.eligible\)/);
  assert.match(app, /state\.selectedCandidates\.set\(key, validIds\)/);
  assert.doesNotMatch(app, /candidate-export-blocked[\s\S]*disabled/);
  assert.match(app, /if \(!selected && !exportEligibility\.eligible\)/);
  assert.match(app, /let count = 0; let manualCount = 0/);
  assert.match(app, /launch\.disabled = count === 0 \|\| Boolean\(locked\)/);
  assert.match(app, /const staleSelections = \[\]/);
  assert.match(app, /候选已不存在或生成记录已失效，已清除陈旧选择/);
  assert.match(app, /manual_evidence_override: eligibility\.manualOverrideRequired/);
  assert.match(app, /确认导出[\s\S]*非推荐候选/);
  assert.match(app, /结构规则未通过、未达难度目标或动态依据不足/);
  assert.match(app, />选择并导出<\/button>/);
});
