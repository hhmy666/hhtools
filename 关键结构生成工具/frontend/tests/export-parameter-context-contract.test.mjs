import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const appSource = readFileSync(new URL("../assets/app.js", import.meta.url), "utf8");
const helperStart = appSource.indexOf("  function parameterRequestContext");
const helperEnd = appSource.indexOf("\n  async function api", helperStart);

assert.notEqual(helperStart, -1, "parameter context helpers must remain in app.js");
assert.notEqual(helperEnd, -1, "parameter context helpers must remain independently testable");

const helperSource = appSource.slice(helperStart, helperEnd);
const helpers = Function(
  `"use strict";\n${helperSource}\nreturn { parameterRequestContext, parameterRequestContextIsCurrent, recentExportResolveFields };`,
)();

test("recent-export resolve is pinned to one export and its owning job", () => {
  const current = {
    parameterSource: "recent_export",
    recentExport: { exportId: "export-a", jobId: "job-a" },
  };
  const context = helpers.parameterRequestContext("recent_export", current);

  assert.deepEqual(context, {
    source: "recent_export",
    exportId: "export-a",
    jobId: "job-a",
  });
  assert.deepEqual(helpers.recentExportResolveFields(context), {
    export_id: "export-a",
    job_id: "job-a",
  });
});

test("a different export or source invalidates an in-flight parameter request", () => {
  const context = { source: "recent_export", exportId: "export-a", jobId: "job-a" };
  assert.equal(helpers.parameterRequestContextIsCurrent(context, {
    parameterSource: "recent_export",
    recentExport: { exportId: "export-a", jobId: "job-a" },
  }), true);
  assert.equal(helpers.parameterRequestContextIsCurrent(context, {
    parameterSource: "recent_export",
    recentExport: { exportId: "export-b", jobId: "job-a" },
  }), false);
  assert.equal(helpers.parameterRequestContextIsCurrent(context, {
    parameterSource: "output_ids",
    recentExport: { exportId: "export-a", jobId: "job-a" },
  }), false);
});

test("export CTA state is restored from the server marker", () => {
  const showStart = appSource.indexOf("  function rememberCompletedExport");
  const showEnd = appSource.indexOf("\n  async function controlCurrentExport", showStart);
  const showSource = appSource.slice(showStart, showEnd);
  assert.match(showSource, /record\.parameter_configured/);
  assert.match(showSource, /record\.parameter_configured_at/);

  const resolveStart = appSource.indexOf("  async function resolveParameterTemplates");
  const resolveEnd = appSource.indexOf("\n  async function calculateParameters", resolveStart);
  assert.match(appSource.slice(resolveStart, resolveEnd), /recentExportResolveFields\(context\)/);

  const calculateStart = resolveEnd;
  const calculateEnd = appSource.indexOf("\n  function renderParameterResult", calculateStart);
  const calculateSource = appSource.slice(calculateStart, calculateEnd);
  assert.match(calculateSource, /result\.source_export_id === selectionContext\.exportId/);
  assert.match(calculateSource, /parameterConfigurationCompleted = Boolean\(result\.parameter_configured \|\| result\.parameter_configured_at\)/);
});

test("resolved selections retain the server-confirmed export and job identity", () => {
  const resolveStart = appSource.indexOf("  async function resolveParameterTemplates");
  const resolveEnd = appSource.indexOf("\n  async function calculateParameters", resolveStart);
  const resolveSource = appSource.slice(resolveStart, resolveEnd);
  assert.match(resolveSource, /exportId: payload\.source_export_id \|\| context\.exportId/);
  assert.match(resolveSource, /jobId: payload\.source_job_id \|\| context\.jobId/);
});

test("bootstrap restores the latest completed export marker without guessing a job", () => {
  const bootstrapStart = appSource.indexOf("  async function bootstrap");
  const bootstrapEnd = appSource.indexOf("\n  bootstrap();", bootstrapStart);
  const bootstrapSource = appSource.slice(bootstrapStart, bootstrapEnd);
  assert.match(bootstrapSource, /const recoverableExport = \(payload\.exports \|\| \[\]\)\.find/);
  assert.match(bootstrapSource, /rememberCompletedExport\(recoverableExport\)/);
  assert.match(bootstrapSource, /record\?\.status === "completed"/);
});
