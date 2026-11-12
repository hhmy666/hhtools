import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";

const html = fs.readFileSync(new URL("../index.html", import.meta.url), "utf8");
const app = fs.readFileSync(new URL("../assets/app.js", import.meta.url), "utf8");
const css = fs.readFileSync(new URL("../assets/styles.css", import.meta.url), "utf8");

test("new plans start from the approved six-candidate search profile", () => {
  assert.match(html, /id="candidate-count"[^>]+value="6"/);
  assert.ok(html.includes("建议使用 6–10 个"));
  assert.match(html, /id="search-attempt-budget"[^>]+value="2000"/);
  assert.match(html, /id="max-structure-layer-span-kind"[\s\S]*?<option value="absolute" selected>最多<\/option>/);
  assert.match(html, /id="quality-position"[\s\S]*?<option value="unrestricted" selected>不限制<\/option>/);
  assert.match(html, /id="quality-symmetry"[\s\S]*?<option value="off" selected>关闭<\/option>/);
  assert.match(html, /id="quality-density"[\s\S]*?<option value="compact" selected>紧凑<\/option>/);
  assert.match(app, /candidateCount:\s*6/);
  assert.match(app, /searchAttemptBudget:\s*2000/);
  assert.match(app, /maxStructureLayerSpanKind:\s*"absolute"/);
  assert.match(app, /position:\s*"unrestricted"/);
  assert.match(app, /symmetry:\s*"off"/);
  assert.match(app, /density:\s*"compact"/);
  assert.match(app, /numberValue\("#candidate-count", 6\)/);
  assert.match(app, /numberValue\("#search-attempt-budget", 2000\)/);
});

test("advanced settings expose three accessible difficulty targets", () => {
  const advancedStart = html.indexOf('<details class="advanced-panel">');
  const difficultyStart = html.indexOf('class="difficulty-target-fieldset"');
  assert.ok(advancedStart >= 0 && difficultyStart > advancedStart);
  assert.ok(html.includes('name="difficulty-target" value="fidelity" checked'));
  assert.ok(html.includes('name="difficulty-target" value="standard"'));
  assert.ok(html.includes('name="difficulty-target" value="strong"'));
  assert.match(app, /quality:\s*\{[\s\S]*difficulty_target:/);
  assert.ok(app.includes("generation.quality?.difficulty_target"));
  assert.ok(css.includes(".difficulty-target-options input:focus-visible + span"));
  assert.match(css, /\.difficulty-target-options span\s*\{[^}]*min-height:\s*68px/);
  assert.match(css, /\.difficulty-target-options b\s*\{[^}]*white-space:\s*nowrap/);
  assert.ok(html.includes("贴近原模板"));
  assert.ok(html.includes("平衡结构压力"));
  assert.ok(html.includes("主动增强压力"));
  assert.ok(!html.includes("允许高压路径，适合明确需要强卡点的场景"));
});

test("candidate details progressively disclose the complete difficulty evidence", () => {
  assert.ok(app.includes("difficulty_simulation"));
  assert.ok(app.includes("source_difficulty_distance"));
  assert.ok(app.includes("capacity7_failure_path_ratio"));
  assert.ok(app.includes("naked_pass_probability"));
  assert.ok(app.includes("first_recycle_click"));
  assert.ok(app.includes("first_full_bag_click"));
  assert.ok(app.includes("pass_probability"));
  assert.ok(app.includes("strategies.local_optimal"));
  assert.ok(app.includes('["unlock", "解锁优先"]'));
  assert.ok(app.includes('["adversarial", "压力测试"]'));
  assert.ok(app.includes("五类模拟策略"));
  assert.ok(app.includes('coverageRaw === "complete"'));
  assert.ok(app.includes("未找到副本参数，使用结构参考"));
  assert.ok(app.includes("查看难度策略样本"));
  assert.ok(app.includes("查看本候选整板体验依据"));
  assert.ok(css.includes(".difficulty-table-wrap"));
  assert.ok(app.includes("modernDistance?.primary_distance"));
  assert.ok(css.includes(".experience-technical-details"));
});

test("parameter strategies default to per-duplicate fidelity and isolate gradient rebuild choices", () => {
  assert.match(app, /currentSelection[^\n]+\|\| "fidelity"/);
  assert.match(app, /preferredOrder = \["fidelity", "balanced"/);
  assert.ok(app.includes("保真原副本参数"));
  assert.ok(app.includes("重做副本梯度"));
  assert.ok(app.includes("逐副本保留 Seed / Luck"));
  assert.ok(app.includes("主侧 4、次侧 3 的经验底线"));
  assert.ok(app.includes("参数仅供参考，不能保证消耗相同"));
  assert.ok(app.includes('balanced: "A 从副本 2、B 从副本 3 开始递减"'));
  assert.ok(app.includes('gentle: "副本 3 / 4 起，每 3 档减 1"'));
});
