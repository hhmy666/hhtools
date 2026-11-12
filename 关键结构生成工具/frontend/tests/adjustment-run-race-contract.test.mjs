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
