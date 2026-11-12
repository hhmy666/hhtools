import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";

const html = fs.readFileSync(new URL("../index.html", import.meta.url), "utf8");
const app = fs.readFileSync(new URL("../assets/app.js", import.meta.url), "utf8");

test("replacement source defaults to original A/B majority with A ties", () => {
  assert.match(html, /id="replacement-sources"[^>]*><option value="majority_a_on_tie" selected>A≥B 替换 A；A&lt;B 替换 B/);
  assert.match(app, /replacementSources:\s*MAJORITY_A_ON_TIE/);
  assert.ok(html.includes("实际总数"));
});

test("saved legacy A-first rule remains visible and round-trips unchanged", () => {
  assert.match(html, /<option value="prefer_a_else_b">兼容旧任务：优先 A，无可用 A 则 B<\/option>/);
  assert.match(app, /\[MAJORITY_A_ON_TIE, LEGACY_PREFER_A_ELSE_B\]\.includes\(replacementSourceValue\)/);
  assert.match(app, /plan\.replacementSources = generation\.replacement_sources \?\? plan\.replacementSources/);
  assert.ok(!app.includes("generation.replacement_sources === LEGACY_PREFER_A_ELSE_B\n      ? MAJORITY_A_ON_TIE"));
});
