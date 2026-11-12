import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";

const html = fs.readFileSync(new URL("../index.html", import.meta.url), "utf8");
const app = fs.readFileSync(new URL("../assets/app.js", import.meta.url), "utf8");
const css = fs.readFileSync(new URL("../assets/styles.css", import.meta.url), "utf8");

test("work navigation follows the candidate cards and candidate pager", () => {
  assert.ok(html.indexOf('id="candidate-grid"') < html.indexOf('id="work-prev"'));
  assert.ok(html.indexOf('id="candidate-next"') < html.indexOf('id="work-prev"'));
  assert.ok(html.includes('id="work-navigation-label"'));
});

test("parameter page exposes independent online and unlaunched index modes", () => {
  assert.ok(html.includes('data-parameter-mode="online"'));
  assert.ok(html.includes('data-parameter-mode="unlaunched"'));
  assert.ok(html.includes('aria-controls="parameter-mode-online-panel"'));
  assert.ok(html.includes('role="tabpanel" aria-labelledby="parameter-mode-unlaunched-tab"'));
  assert.ok(html.indexOf('id="parameter-template-ids"') < html.indexOf('id="parameter-mode-online-panel"'));
  assert.ok(!html.includes('id="unlaunched-config-set-select"'));
  assert.ok(!html.includes('id="unlaunched-template-ids"'));
  assert.ok(app.includes('/parameters/unlaunched-index'));
  assert.ok(app.includes('selection_id: selectionId'));
  assert.ok(app.includes('item.config_level_path'));
  assert.ok(app.includes('duplicate-id-chip'));
  assert.ok(app.includes('["ArrowLeft", "ArrowRight", "Home", "End"]'));
});

test("parameter index panels and strategy choices keep a common visual grid", () => {
  assert.match(html, /class="parameter-mode-row"[\s\S]*class="segmented-control parameter-mode-switch"/);
  assert.match(html, /class="strategy-fieldset" aria-label="副本参数方案"/);
  assert.ok(!html.includes("参数配置方式"));
  assert.match(html, /class="parameter-settings parameter-index-settings porcelain-card"/);
  assert.match(html, /class="primary-button parameter-primary-action" id="resolve-parameter-templates"/);
  assert.match(css, /\.parameter-flow\s*\{[^}]*align-items:\s*stretch/);
  assert.match(css, /\.parameter-mode-row,[\s\S]*\.parameter-flow\s*\{[^}]*grid-template-columns:\s*minmax\(440px, \.85fr\) minmax\(520px, 1\.15fr\)/);
  assert.match(css, /\.parameter-mode-switch\s*\{[^}]*grid-column:\s*1[^}]*width:\s*100%/);
  assert.match(css, /\.parameter-mode-panel\.is-active\s*\{[^}]*min-height:\s*100%/);
  assert.match(css, /\.parameter-index-settings\s*\{[^}]*flex:\s*1 0 auto/);
  assert.match(css, /\.parameter-primary-action\s*\{[^}]*width:\s*100%/);
  assert.match(css, /\.strategy-group \.strategy-options\s*\{[^}]*grid-auto-rows:\s*1fr/);
  assert.match(css, /\.rebuild-strategy-group \.strategy-options\s*\{[^}]*repeat\(5, minmax\(0, 1fr\)\)/);
  assert.match(css, /\.rebuild-strategy-group \.strategy-option span\s*\{[^}]*min-height:\s*88px/);
  assert.match(css, /@media \(max-width: 1350px\)[\s\S]*\.rebuild-strategy-group \.strategy-options\s*\{[^}]*repeat\(6, minmax\(0, 1fr\)\)/);
  assert.match(css, /\.rebuild-strategy-group \.strategy-option:nth-child\(4\)\s*\{[^}]*grid-column:\s*2 \/ span 2/);
  assert.match(css, /\.rebuild-strategy-group \.strategy-option:nth-child\(5\)\s*\{[^}]*grid-column:\s*4 \/ span 2/);
});

test("online parameter outputs span the page below the two-column input flow", () => {
  const flowStart = html.indexOf('class="parameter-flow"');
  const flowEnd = html.indexOf('class="parameter-output-stack"');
  const onlinePanelStart = html.indexOf('id="parameter-mode-online-panel"');
  const onlinePanelEnd = html.indexOf('id="parameter-mode-unlaunched-panel"');
  const resultsStart = html.indexOf('id="parameter-results"');
  assert.ok(flowStart >= 0 && flowEnd > flowStart);
  assert.ok(resultsStart > flowEnd);
  assert.ok(!(resultsStart > onlinePanelStart && resultsStart < onlinePanelEnd));
  assert.match(html, /class="parameter-output-stack" data-parameter-mode-output="online"/);
  assert.match(css, /\.parameter-output-stack\s*\{[^}]*display:\s*grid[^}]*gap:\s*14px[^}]*margin-top:\s*14px/);
  assert.match(css, /\.parameter-output-stack > \.parameter-results\s*\{[^}]*min-width:\s*0[^}]*margin-top:\s*0/);
  assert.ok(app.includes("output.hidden = output.dataset.parameterModeOutput !== state.parameterMode"));
});

test("frontend risk grouping uses distinct risk values so ties share a tier", () => {
  assert.ok(app.includes("const distinctValues = [...new Set(values.map((entry) => entry.value))]"));
  assert.ok(app.includes("valueIndex / (distinctValues.length - 1)"));
});
