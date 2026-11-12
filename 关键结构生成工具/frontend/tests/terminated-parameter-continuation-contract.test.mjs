import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";

const html = fs.readFileSync(new URL("../index.html", import.meta.url), "utf8");
const app = fs.readFileSync(new URL("../assets/app.js", import.meta.url), "utf8");
const css = fs.readFileSync(new URL("../assets/styles.css", import.meta.url), "utf8");

test("terminated generation exposes an in-place multi-template parameter editor", () => {
  assert.match(html, /id="generation-adjust-and-continue"[^>]*>[^<]*<svg[^>]*>.*修改参数并继续/s);
  assert.match(html, /id="continuation-adjustment-scope"/);
  assert.match(html, /name="continuation-adjustment-mode" value="uniform"/);
  assert.match(html, /name="continuation-adjustment-mode" value="individual"/);
  assert.match(app, /function openContinuationParameterEditor\(/);
  assert.match(app, /function switchContinuationAdjustmentTarget\(/);
  assert.match(app, /runStagedTemplateAdjustments\([^,]+,\s*\{[^}]*includeAllUnfinished:\s*false/s);
  assert.match(css, /\.continuation-adjustment-scope/);
});

test("candidate cards disclose non-unified parameter snapshots through an info dialog", () => {
  assert.match(html, /id="candidate-parameter-dialog"/);
  assert.match(app, /非统一参数/);
  assert.match(app, /data-candidate-parameter-info/);
  assert.match(app, /parameter_provenance/);
  assert.match(app, /resolved_generation_options/);
  assert.match(css, /\.candidate-parameter-info/);
});
