import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";

const html = fs.readFileSync(new URL("../index.html", import.meta.url), "utf8");
const app = fs.readFileSync(new URL("../assets/app.js", import.meta.url), "utf8");
const css = fs.readFileSync(new URL("../assets/styles.css", import.meta.url), "utf8");

const typeHelperStart = app.indexOf("  function parseStructureGroupVector");
const typeHelperEnd = app.indexOf("\n  function unitRoles", typeHelperStart);
assert.notEqual(typeHelperStart, -1, "type configuration helpers must exist");
assert.notEqual(typeHelperEnd, -1, "type configuration helpers must remain independently testable");

const typeHelpers = Function(
  `"use strict";
  const STRUCTURE_TYPES = "CDEFGHIJKLMN".split("");
  const clamp = (value, minimum, maximum) => Math.max(minimum, Math.min(maximum, value));
  ${app.slice(typeHelperStart, typeHelperEnd)}
  return { parseStructureGroupVector, effectiveStructureTypeConfiguration };`,
)();

const syncHelperStart = app.indexOf("  function syncStructureCountMode");
const syncHelperEnd = app.indexOf("\n  function refreshConditionalPlanFields", syncHelperStart);
assert.notEqual(syncHelperStart, -1, "special-preset UI synchronizer must exist");
assert.notEqual(syncHelperEnd, -1, "special-preset UI synchronizer must remain independently testable");

function classList() {
  const values = new Set();
  return {
    toggle(name, enabled) { enabled ? values.add(name) : values.delete(name); },
    contains(name) { return values.has(name); },
  };
}

test("all-types preset presents one truthful 12-by-3 effective configuration", () => {
  assert.match(html, /id="all-types-effective-config"[\s\S]*12 种 × 每种 3 格[\s\S]*C–N 全部使用，共 36 格/);
  assert.match(html, /id="type-count-row"/);
  assert.match(html, /id="allowed-types-fieldset"/);
  assert.ok(css.includes(".preset-effective-config"));
});

test("effective all-types settings do not overwrite the user's stored type range", () => {
  const plan = {
    typeCountKind: "range",
    typeMin: 3,
    typeMax: 5,
    structureGroupMode: "standard",
    uniformGroupsPerType: 1,
    customGroupsPerType: "2；1；1",
    allowedTypes: ["C", "D", "E", "F"],
    quality: { special_preset: "all_types_long_range" },
  };
  const original = structuredClone(plan);
  const effective = typeHelpers.effectiveStructureTypeConfiguration(plan);

  assert.equal(effective.overridden, true);
  assert.equal(effective.kind, "exact");
  assert.equal(effective.minimum, 12);
  assert.equal(effective.maximum, 12);
  assert.deepEqual(effective.allowedTypes, "CDEFGHIJKLMN".split(""));
  assert.equal(effective.tilesPerType, 3);
  assert.equal(effective.totalTiles, 36);
  assert.deepEqual(effective.groupsPerType, Array(12).fill(1));
  assert.deepEqual(plan, original, "selecting the preset must not pollute the stored batch or template plan");

  plan.quality.special_preset = "none";
  const restored = typeHelpers.effectiveStructureTypeConfiguration(plan);
  assert.equal(restored.overridden, false);
  assert.equal(restored.kind, "range");
  assert.equal(restored.minimum, 3);
  assert.equal(restored.maximum, 5);
  assert.deepEqual(restored.allowedTypes, ["C", "D", "E", "F"]);
  assert.equal(restored.tilesPerType, 3);
  assert.equal(restored.totalTiles, null);
});

test("shared plan form hides contradictory controls and restores them after deselection", () => {
  const nodes = Object.fromEntries([
    "#type-count-row",
    "#all-types-effective-config",
    "#allowed-types-fieldset",
    "#type-count-kind",
    "#type-count-min",
    "#type-count-max",
    "#structure-count-mode-fieldset",
    "#standard-structure-count-panel",
    "#custom-structure-count-panel",
    "#uniform-groups-per-type",
    "#custom-groups-per-type",
    "#total-limit-kind",
    "#total-limit-value",
  ].map((selector) => [selector, { classList: classList(), disabled: false }]));
  const allowedInputs = [{ disabled: false }, { disabled: false }];
  const modeInputs = [{ disabled: false }, { disabled: false }];
  const plan = {
    typeCountKind: "range",
    typeMin: 3,
    typeMax: 5,
    structureGroupMode: "standard",
    uniformGroupsPerType: 1,
    customGroupsPerType: "2；1；1",
    totalLimitKind: "percent",
    allowedTypes: ["C", "D", "E"],
    quality: { special_preset: "all_types_long_range" },
  };
  const helper = Function(
    "effectiveStructureTypeConfiguration",
    "$",
    "$$",
    "currentEditedPlan",
    "announce",
    "parseStructureGroupVector",
    `"use strict";${app.slice(syncHelperStart, syncHelperEnd)};return syncSpecialPresetEffectiveConfiguration;`,
  )(
    typeHelpers.effectiveStructureTypeConfiguration,
    (selector) => nodes[selector] || null,
    (selector) => selector === "#allowed-types input" ? allowedInputs : selector.includes("structure-group-mode") ? modeInputs : [],
    () => plan,
    () => {},
    typeHelpers.parseStructureGroupVector,
  );

  helper(plan);
  assert.equal(nodes["#type-count-row"].classList.contains("is-hidden"), true);
  assert.equal(nodes["#all-types-effective-config"].classList.contains("is-hidden"), false);
  assert.equal(nodes["#allowed-types-fieldset"].classList.contains("is-hidden"), true);
  assert.equal(nodes["#type-count-kind"].disabled, true);
  assert.ok(modeInputs.every((input) => input.disabled));
  assert.ok(allowedInputs.every((input) => input.disabled));

  plan.quality.special_preset = "none";
  helper(plan);
  assert.equal(nodes["#type-count-row"].classList.contains("is-hidden"), false);
  assert.equal(nodes["#all-types-effective-config"].classList.contains("is-hidden"), true);
  assert.equal(nodes["#allowed-types-fieldset"].classList.contains("is-hidden"), false);
  assert.equal(nodes["#type-count-kind"].disabled, false);
  assert.ok(modeInputs.every((input) => !input.disabled));
  assert.ok(allowedInputs.every((input) => !input.disabled));
  assert.equal(plan.typeMin, 3);
  assert.equal(plan.typeMax, 5);
  assert.deepEqual(plan.allowedTypes, ["C", "D", "E"]);
});

test("the same synchronizer is used by batch, template, and adjustment-plan form sync", () => {
  assert.match(app, /function currentEditedPlan\(\)[\s\S]*state\.adjustmentDialog\?\.draft[\s\S]*state\.editorScope === "batch"/);
  assert.match(app, /function syncPlanForm\(plan\)[\s\S]*refreshConditionalPlanFields\(\)/);
  assert.match(app, /function refreshSpecialPresetAvailability\(plan = currentEditedPlan\(\)\)[\s\S]*syncSpecialPresetEffectiveConfiguration\(plan\)/);
});
