import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";

const html = fs.readFileSync(new URL("../index.html", import.meta.url), "utf8");
const app = fs.readFileSync(new URL("../assets/app.js", import.meta.url), "utf8");
const css = fs.readFileSync(new URL("../assets/styles.css", import.meta.url), "utf8");

test("structure limits expose mutually exclusive standard and custom group modes", () => {
  assert.ok(html.includes('name="structure-group-mode" value="standard" checked'));
  assert.ok(html.includes('name="structure-group-mode" value="custom"'));
  assert.ok(html.includes('id="uniform-groups-per-type"'));
  assert.match(html, /id="uniform-groups-per-type"[^>]+value="0"/);
  assert.match(html, /id="uniform-groups-per-type"[^>]+min="0"/);
  assert.match(html, /设为 0 时自动分配/);
  assert.match(html, /自动组数最大差值/);
  assert.ok(html.includes('id="custom-groups-per-type"'));
  assert.ok(html.includes("2；1；1"));
  assert.ok(html.includes("2；2；2；1；1"));
  assert.ok(!html.includes('<option value="random">随机范围</option>'));
  assert.ok(css.includes(".structure-count-mode-options"));
  assert.ok(css.includes(".structure-type-row"));
  assert.ok(css.includes(".structure-capacity-row"));
  assert.ok(css.includes(".structure-custom-panel"));
  assert.match(css, /\.structure-type-row\s*\{[^}]*grid-template-columns:\s*repeat\(3,\s*minmax\(0,\s*1fr\)\)/);
  assert.match(css, /\.structure-capacity-row\s*\{[^}]*grid-template-columns:\s*repeat\(2,\s*minmax\(0,\s*1fr\)\)/);
  assert.doesNotMatch(css, /\.structure-capacity-row > label:first-child\s*\{[^}]*grid-column:\s*1 \/ -1/);
  assert.match(html, /id="uniform-groups-per-type"[\s\S]*?设为 0 时自动分配<\/small>\s*<\/label>\s*<label>\s*<span>自动组数最大差值<\/span>/);
  assert.match(html, /id="automatic-group-difference-help">仅自动分配生效<\/small>/);
  assert.doesNotMatch(html, /structure-setting-card/);
  assert.doesNotMatch(css, /\.structure-setting-card/);
  assert.doesNotMatch(css, /\.structure-count-panel\s*\{[^}]*border:/);
});

test("plan model and wire contract preserve uniform or custom group counts", () => {
  assert.match(app, /structureGroupMode:\s*"standard"/);
  assert.match(app, /uniformGroupsPerType:\s*0/);
  assert.match(app, /customGroupsPerType:/);
  assert.ok(app.includes("uniform_groups_per_type"));
  assert.ok(app.includes("groups_per_type"));
  assert.ok(app.includes("parseStructureGroupVector"));
  assert.ok(app.includes("structureGroupGenerationFields"));
  assert.ok(app.includes("hydrateTypeCountModel"));
  assert.match(app, /legacyTypeCountKind\s*===\s*"random"/);
  assert.match(app, /legacyTypeCountKind === "random" \? "random" : "range"/);
});

test("group vector parser accepts Chinese separators and computes exact tiles", () => {
  const start = app.indexOf("  function parseStructureGroupVector");
  const end = app.indexOf("\n  function unitRoles", start);
  assert.notEqual(start, -1);
  assert.notEqual(end, -1);
  const helpers = Function(
    `"use strict";
    const STRUCTURE_TYPES = "CDEFGHIJKLMN".split("");
    const clamp = (value, minimum, maximum) => Math.max(minimum, Math.min(maximum, value));
    ${app.slice(start, end)}
    return { parseStructureGroupVector, hydrateTypeCountModel, typeChoice, structureGroupGenerationFields, effectiveStructureTypeConfiguration };`,
  )();

  assert.deepEqual(helpers.parseStructureGroupVector("2；1；1").values, [2, 1, 1]);
  assert.deepEqual(helpers.parseStructureGroupVector("2; 2, 2 1 1").values, [2, 2, 2, 1, 1]);
  assert.match(helpers.parseStructureGroupVector("2；0；1").error, /正整数/);
  assert.deepEqual(
    helpers.hydrateTypeCountModel({ kind: "random" }, { typeMin: 3, typeMax: 5 }),
    { kind: "range", legacyKind: "random", minimum: 3, maximum: 7 },
  );

  const custom = helpers.effectiveStructureTypeConfiguration({
    structureGroupMode: "custom",
    customGroupsPerType: "2；1；1",
    typeCountKind: "range",
    typeMin: 3,
    typeMax: 5,
    uniformGroupsPerType: 1,
    allowedTypes: ["C", "D", "E", "F"],
    quality: { special_preset: "none" },
  });
  assert.equal(custom.kind, "exact");
  assert.equal(custom.minimum, 3);
  assert.equal(custom.maximum, 3);
  assert.equal(custom.totalTiles, 12);
  assert.deepEqual(custom.groupsPerType, [2, 1, 1]);

  const standard = helpers.effectiveStructureTypeConfiguration({
    structureGroupMode: "standard",
    customGroupsPerType: "",
    typeCountKind: "exact",
    typeMin: 3,
    typeMax: 3,
    uniformGroupsPerType: 2,
    allowedTypes: ["C", "D", "E", "F"],
    quality: { special_preset: "none" },
  });
  assert.equal(standard.totalTiles, 18);
  assert.equal(standard.tilesPerType, 6);

  assert.deepEqual(
    helpers.structureGroupGenerationFields({
      structureGroupMode: "standard",
      uniformGroupsPerType: 2,
      quality: { special_preset: "none" },
    }),
    { uniform_groups_per_type: 2 },
  );
  assert.deepEqual(
    helpers.structureGroupGenerationFields({
      structureGroupMode: "standard",
      uniformGroupsPerType: 1,
      quality: { special_preset: "none" },
    }),
    {},
  );
  assert.deepEqual(
    helpers.structureGroupGenerationFields({
      structureGroupMode: "standard",
      uniformGroupsPerType: 0,
      quality: { special_preset: "none" },
    }),
    { uniform_groups_per_type: 0 },
  );
  assert.deepEqual(
    helpers.structureGroupGenerationFields({
      structureGroupMode: "custom",
      customGroupsPerType: "2；1；1",
      quality: { special_preset: "none" },
    }),
    { groups_per_type: [2, 1, 1] },
  );

  assert.deepEqual(
    helpers.typeChoice({
      structureGroupMode: "standard",
      legacyTypeCountKind: "random",
      typeCountKind: "range",
      typeMin: 3,
      typeMax: 7,
    }),
    { kind: "random", minimum: 3, maximum: 7 },
  );
});

test("custom group vectors are exact and survive locked-plan hydration", () => {
  assert.ok(app.includes("generation.unit_defaults?.groups_per_type"));
  assert.ok(app.includes('plan.structureGroupMode = "custom"'));
  assert.ok(app.includes("plan.customGroupsPerType"));
  assert.ok(app.includes("plan.typeMin = customGroups.length"));
});

test("locked legacy plans preserve omitted span and random type semantics", () => {
  assert.match(app, /typeKind === "random" \? 3/);
  assert.match(app, /typeKind === "random" \? 7/);
  assert.match(app, /plan\.legacyTypeCountKind\s*=\s*hydratedTypeCount\.legacyKind/);
  assert.match(app, /else plan\.maxStructureLayerSpanKind = "none"/);
});
