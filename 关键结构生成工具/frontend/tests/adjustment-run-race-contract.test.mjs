import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const appSource = readFileSync(new URL("../assets/app.js", import.meta.url), "utf8");

function functionSource(name, nextName) {
  const start = appSource.indexOf(`  ${name}`);
  const end = appSource.indexOf(`\n  ${nextName}`, start);
  assert.notEqual(start, -1, `${name} must remain in app.js`);
  assert.notEqual(end, -1, `${name} must remain independently inspectable`);
  return appSource.slice(start, end);
}

test("adjustment operation identity rejects task, load, and token changes", () => {
  const source = functionSource(
    "function adjustmentRunContextIsCurrent",
    "function endAdjustmentRun",
  );
  const isCurrent = Function(
    `"use strict";\n${source}\nreturn adjustmentRunContextIsCurrent;`,
  )();
  const operation = { token: 7, jobLoadToken: 11, parentJobId: "job-a" };
  const current = {
    adjustmentRunPending: true,
    adjustmentRunToken: 7,
    jobLoadToken: 11,
    jobId: "job-a",
  };

  assert.equal(isCurrent(operation, current), true);
  assert.equal(isCurrent(operation, { ...current, adjustmentRunPending: false }), false);
  assert.equal(isCurrent(operation, { ...current, adjustmentRunToken: 8 }), false);
  assert.equal(isCurrent(operation, { ...current, jobLoadToken: 12 }), false);
  assert.equal(isCurrent(operation, { ...current, jobId: "job-b" }), false);
});

test("derived adjustment checks context at both network boundaries", () => {
  const source = functionSource(
    "async function runStagedTemplateAdjustments",
    "function runSingleStagedTemplate",
  );
  const preflight = source.indexOf('const preflight = await api("/plans/resolve"');
  const firstGuard = source.indexOf("if (!adjustmentRunContextIsCurrent(operation))", preflight);
  const create = source.indexOf('const job = await api("/jobs"', firstGuard);
  const secondGuard = source.indexOf("if (!adjustmentRunContextIsCurrent(operation))", create);
  const stateMutation = source.indexOf("state.jobId = job.job_id", secondGuard);

  assert.ok(preflight >= 0 && firstGuard > preflight, "preflight response must be context-checked");
  assert.ok(create > firstGuard && secondGuard > create, "created child must be context-checked");
  assert.ok(stateMutation > secondGuard, "child must not take over UI before the second check");
  assert.match(source, /parent_job_id:\s*parentJobId/);
  assert.match(source, /const operation = beginAdjustmentRun\(parentJobId\)/);
  assert.match(source, /endAdjustmentRun\(operation\)/);
});

test("resume uses the captured job and all run controls share the mutex", () => {
  const source = functionSource(
    "async function continueCurrentJobSearch",
    "function enterAvailableCandidateReview",
  );
  assert.match(source, /const parentJobId = state\.jobId/);
  assert.match(source, /encodeURIComponent\(parentJobId\)/);
  assert.match(source, /const operation = beginAdjustmentRun\(parentJobId\)/);
  assert.match(source, /if \(!adjustmentRunContextIsCurrent\(operation\)\)/);
  assert.match(source, /endAdjustmentRun\(operation\)/);

  const runControlAssignments = appSource.match(/dataset\.adjustmentRunControl\s*=\s*""/g) || [];
  assert.ok(runControlAssignments.length >= 2, "single-run and batch-run buttons must share the mutex");
  assert.match(appSource, /function invalidateAdjustmentRun\(\)/);
});

test("selected recommendations submit one child job with matching overrides and scopes", async () => {
  const source = functionSource("function derivedRetryScopeFields", "function runSingleStagedTemplate");
  const overrides = [
    { template_key: "one", plan_id: "batch_s6", changes: { max_layer_percent: 82 } },
    { template_key: "two", plan_id: "batch_s6", changes: { max_layer_percent: 82 } },
  ];
  const entries = overrides.map((override, index) => ({
    templateKey: override.template_key, planId: "resolved_s6",
    recommendationId: String(index), sourcePlanId: override.plan_id,
  }));
  const items = entries.map(entry => ({ template_key: entry.templateKey, plan_id: entry.planId }));
  const requests = [];
  const state = { jobId: "parent", jobSnapshot: { import_id: "import" }, jobResult: {}, selectedCandidates: new Map() };
  const run = Function(
    "state", "$", "api", "HTMLElement", "document", "toast", "unfinishedCandidateItems",
    "candidateAdjustmentKey", "retryScopeForItem", "beginAdjustmentRun",
    "adjustmentRunContextIsCurrent", "setBusy", "endAdjustmentRun",
    `${source}\nreturn runStagedTemplateAdjustments;`,
  )(
    state, () => null,
    async (path, { body }) => {
      requests.push({ path, body });
      if (path.endsWith("/recommendations/apply")) {
        return { continue_request: { body: { parameter_overrides: [overrides[Number(body.recommendation_id)]] } } };
      }
      return { job_id: "child" };
    },
    class {}, {}, () => {}, () => items,
    item => `${item.template_key}::${item.plan_id}`,
    item => ({ template_key: item.template_key, plan_id: item.plan_id }),
    () => ({}), () => requests.length < 3, () => {}, () => {},
  );
  await run(entries);
  assert.deepEqual(requests[2], { path: "/jobs", body: {
    import_id: "import", parent_job_id: "parent", parameter_overrides: overrides,
    retry_scopes: overrides.map(({ template_key, plan_id }) => ({ template_key, plan_id })),
  } });
  assert.equal(requests.length, 3);
});
