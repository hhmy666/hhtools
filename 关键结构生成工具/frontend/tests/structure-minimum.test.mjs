import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";

const app = fs.readFileSync(new URL("../assets/app.js", import.meta.url), "utf8");
const slice = (start, end) => app.slice(app.indexOf(start), app.indexOf(end, app.indexOf(start)));
const helpers = Function(`
  const STRUCTURE_TYPES = "CDEFGHIJKLMN".split("");
  const MAJORITY_A_ON_TIE = "majority_a_on_tie";
  const clamp = (n, low, high) => Math.max(low, Math.min(high, n));
  const clone = value => structuredClone(value);
  ${slice("  function newPlanModel(", "  function planSignature(")}
  ${slice("  function parseStructureGroupVector(", "  function unitRoles(")}
  ${slice("  function generationSpec(", "  function toPlanSpec(")}
  ${slice("  function planModelFromSpec(", "  function rawPlanContextForItem(")}
  return {newPlanModel, generationSpec, planModelFromSpec};
`)();

test("same-type layer span defaults to two for new tasks and preserves explicit zero and old tasks", () => {
  const plan = helpers.newPlanModel();
  assert.equal(helpers.generationSpec(plan).min_same_type_layer_span, 2);
  for (const value of [0, 2, 7]) {
    plan.minSameTypeLayerSpan = value;
    const generation = helpers.generationSpec(plan);
    assert.equal(generation.min_same_type_layer_span, value);
    assert.equal(helpers.planModelFromSpec({generation}).minSameTypeLayerSpan, value);
  }
  assert.equal(helpers.planModelFromSpec({generation:{}}).minSameTypeLayerSpan, 0);
});

test("non-tower percentage limits preserve 70 and 100 through serialization", () => {
  for (const value of [70, 100]) {
    const plan = helpers.newPlanModel();
    plan.towerMode = "disallow";
    plan.totalLimitKind = "percent";
    plan.totalLimitValue = value;
    const generation = helpers.generationSpec(plan);
    assert.equal(generation.total_percent_limit, value);
    assert.equal(helpers.planModelFromSpec({generation}).totalLimitValue, value);
  }
});

test("new plans use variable groups and preserve the lower bound through resume and edits", () => {
  const plan = helpers.newPlanModel();
  assert.equal(helpers.generationSpec(plan).unit_defaults.uniform_groups_per_type, 0);
  assert.equal(plan.totalMinimumKind, "percent");
  assert.equal(helpers.generationSpec(plan).total_percent_minimum, 0);
  const defaultRestored = helpers.planModelFromSpec({generation: helpers.generationSpec(plan)});
  assert.equal(defaultRestored.totalMinimumKind, "percent");
  assert.equal(defaultRestored.totalCountMinimum, 0);
  plan.totalMinimumKind = "absolute";
  plan.totalCountMinimum = 13;
  const restored = helpers.planModelFromSpec({generation: helpers.generationSpec(plan)});
  assert.equal(restored.totalCountMinimum, 13);
  assert.equal(helpers.generationSpec(restored).total_count_minimum, 13);
  assert.equal(restored.uniformGroupsPerType, 0);
});

test("percent and unrestricted minima round-trip without retaining the other mode", () => {
  const plan = helpers.newPlanModel();
  plan.totalCountMinimum = 25;
  let generation = helpers.generationSpec(plan);
  assert.equal(generation.total_percent_minimum, 25);
  assert.equal(generation.total_count_minimum, undefined);
  let restored = helpers.planModelFromSpec({generation});
  assert.equal(restored.totalMinimumKind, "percent");
  assert.equal(restored.totalCountMinimum, 25);
  plan.totalMinimumKind = "none";
  generation = helpers.generationSpec(plan);
  assert.equal(generation.total_percent_minimum, undefined);
  assert.equal(generation.total_count_minimum, 0);
  restored = helpers.planModelFromSpec({generation});
  assert.equal(restored.totalMinimumKind, "none");
});

test("old requests retain one group and default the absent minimum to zero", () => {
  for (const unit_defaults of [{}, {uniform_groups_per_type: 1}]) {
    const restored = helpers.planModelFromSpec({generation: {unit_defaults}});
    assert.equal(restored.uniformGroupsPerType, 1);
    assert.equal(restored.totalCountMinimum, 0);
    assert.equal(helpers.generationSpec(restored).unit_defaults.uniform_groups_per_type, undefined);
  }
});
