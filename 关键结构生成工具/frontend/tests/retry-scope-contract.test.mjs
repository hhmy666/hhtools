import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const appSource = readFileSync(new URL("../assets/app.js", import.meta.url), "utf8");
const helperStart = appSource.indexOf("  function derivedRetryScopeFields");
const helperEnd = appSource.indexOf("\n  async function runStagedTemplateAdjustments", helperStart);

assert.notEqual(helperStart, -1, "derived retry-scope helper must remain in app.js");
assert.notEqual(helperEnd, -1, "derived retry-scope helper must remain independently testable");

const helperSource = appSource.slice(helperStart, helperEnd);
const derivedRetryScopeFields = Function(
  `"use strict";\n${helperSource}\nreturn derivedRetryScopeFields;`,
)();

test("a multi-template derived search sends only retry_scopes", () => {
  const scopes = [
    { template_key: "first", plan_id: "s5" },
    { template_key: "second", plan_id: "s6" },
  ];
  const payload = derivedRetryScopeFields(scopes);

  assert.deepEqual(payload.retry_scopes, scopes);
  assert.equal(Object.hasOwn(payload, "retry_scope"), false);
});

test("a singleton derived search keeps the matching legacy alias", () => {
  const scopes = [{ template_key: "first", plan_id: "s5" }];
  const payload = derivedRetryScopeFields(scopes);

  assert.deepEqual(payload.retry_scopes, scopes);
  assert.deepEqual(payload.retry_scope, scopes[0]);
});
