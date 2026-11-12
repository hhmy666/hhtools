import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";

const app = fs.readFileSync(new URL("../assets/app.js", import.meta.url), "utf8");

function loadHelpers() {
  const start = app.indexOf("  function sourceTemplateMetrics");
  const end = app.indexOf("\n  function candidateId", start);
  assert.notEqual(start, -1, "source template metrics helper must exist");
  assert.notEqual(end, -1, "candidate detail metrics helpers must remain independently testable");
  return Function(
    `"use strict";${app.slice(start, end)};return { sourceTemplateMetrics, candidateStructureLayerMetrics };`,
  )();
}

test("candidate detail metrics expose the four source-template facts", () => {
  const { sourceTemplateMetrics } = loadHelpers();
  assert.deepEqual(
    sourceTemplateMetrics({
      inspection: {
        tile_count: 102,
        non_tower_tile_count: 84,
        max_layer: 15,
        non_tower_max_layer: 6,
      },
    }),
    {
      tileCount: 102,
      nonTowerTileCount: 84,
      maxLayer: 15,
      nonTowerMaxLayer: 6,
    },
  );

  assert.deepEqual(sourceTemplateMetrics(), {
    tileCount: "—",
    nonTowerTileCount: "—",
    maxLayer: "—",
    nonTowerMaxLayer: "—",
  });
});

test("candidate structure layers derive from replacement z coordinates", () => {
  const { candidateStructureLayerMetrics } = loadHelpers();
  assert.deepEqual(
    candidateStructureLayerMetrics({
      candidate: {
        replacements: [
          { tile_index: 2, z: 4 },
          { tile_index: 7, z: 1 },
          { tile_index: 8, z: 4 },
        ],
      },
    }),
    { minimum: 2, maximum: 5, span: 3 },
  );

  assert.deepEqual(candidateStructureLayerMetrics({}), {
    minimum: "—",
    maximum: "—",
    span: "—",
  });
});

test("candidate structure layers can fall back to existing metrics", () => {
  const { candidateStructureLayerMetrics } = loadHelpers();
  assert.deepEqual(
    candidateStructureLayerMetrics({
      candidate: { quality_metrics: { structure_layer_span: 4 } },
      board: { validation_summary: { highest_structure_layer: 7 } },
    }),
    { minimum: 3, maximum: 7, span: 4 },
  );
});

test("candidate focus summary renders source and current structure layer labels", () => {
  const start = app.indexOf("  function renderFocusSummary");
  const end = app.indexOf("\n  function playerUnits", start);
  const markup = app.slice(start, end);
  for (const label of [
    "原模板 Tile 总数",
    "原模板非塔 Tile 总数",
    "原模板总层数",
    "原模板非塔层数",
    "结构块最低层",
    "结构块最高层",
    "结构块层间距",
  ]) {
    assert.match(markup, new RegExp(label));
  }
});

test("candidate focus summary aligns identity with mechanism pressure", () => {
  const start = app.indexOf("  function renderFocusSummary");
  const end = app.indexOf("\n  function playerUnits", start);
  const markup = app.slice(start, end);

  assert.match(markup, /class="focus-summary-heading"/);
  assert.match(markup, /class="focus-summary-identity"/);
  assert.match(markup, /候选 \$\{escapeHtml\(displayCandidateIndex\(generated\)\)\} ·/);
  assert.match(markup, /focusExperienceSummaryMarkup\(generated\)/);
  assert.match(markup, /experienceSummaryMarkup\(generated,\s*\{\s*includeArchetype:\s*false,\s*includePressure:\s*false,\s*showEmptyGuidance:\s*false\s*\}\)/);
});
