import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";

const html = fs.readFileSync(new URL("../index.html", import.meta.url), "utf8");
const app = fs.readFileSync(new URL("../assets/app.js", import.meta.url), "utf8");
const css = fs.readFileSync(new URL("../assets/styles.css", import.meta.url), "utf8");

test("target-consumption calibration is an independent navigation workflow", () => {
  assert.ok(html.includes('data-nav="consumption"'));
  assert.ok(html.includes('data-view="consumption"'));
  assert.ok(html.includes('data-calibration-source="output_ids"'));
  assert.ok(html.includes('data-calibration-source="files"'));
  assert.ok(html.includes('data-calibration-source="recent_export"'));
  assert.ok(html.includes('id="resolve-calibration-templates"'));
  assert.ok(app.includes('if (view === "consumption") prepareConsumptionView()'));
});

test("calibration inputs follow source strategy and optional config rules", () => {
  assert.ok(html.includes("模拟结果仅供参考，不保证线上消耗一致"));
  assert.ok(html.includes('id="consumption-history-file"'));
  assert.ok(html.includes("上传原关卡的 Tile关卡副本数据.csv"));
  assert.ok(html.includes("策略组 ID 非必需"));
  assert.ok(html.includes('id="consumption-config-level-file"'));
  assert.ok(html.includes("默认读取当前 ulinkconfig"));
  assert.ok(html.includes('id="consumption-strategy-source"'));
  assert.ok(html.includes('<option value="source" selected>和原关卡一致</option>'));
  assert.ok(html.includes('<option value="1003">1003</option>'));
  assert.ok(html.includes('<option value="1004">1004</option>'));
  assert.ok(html.includes("和原关卡一致"));
  assert.ok(!html.includes('id="consumption-strategy-group"'));
  assert.ok(app.includes("function selectedConsumptionStrategy"));
  assert.ok(app.includes('$("#consumption-strategy-source")?.value'));
  assert.ok(app.includes('value === "1003" || value === "1004"'));
  assert.ok(app.includes("strategy_group_mode: strategy.mode"));
  assert.ok(app.includes("strategy_group_id: strategy.id"));
});

test("manual targets support all ten duplicates and joint Seed search", () => {
  const targetInputs = html.match(/data-consumption-target="\d+"/g) || [];
  assert.equal(targetInputs.length, 10);
  assert.ok(html.includes("留空副本沿用原模板实际值"));
  assert.ok(app.includes('optimize_all_duplicate_seeds: $("#consumption-seed-policy").value === "joint"'));
  assert.ok(app.includes('consider_structure_strategy: $("#consumption-strategy-policy").value !== "ignore"'));
  const requestStart = app.indexOf("async function consumptionCalibrationRequestBody");
  const requestEnd = app.indexOf("async function runConsumptionCalibration", requestStart);
  const requestSource = app.slice(requestStart, requestEnd);
  assert.ok(!requestSource.includes("level_start"));
  assert.ok(!requestSource.includes("level_end"));
});

test("calibration boundary keeps only its concise heading", () => {
  assert.ok(!html.includes('id="consumption-calibration-boundary-copy"'));
  assert.ok(!app.includes('"#consumption-calibration-boundary-copy"'));
  assert.ok(html.includes("模拟结果仅供参考，不保证线上消耗一致"));
});

test("observed Seed mode searches observed values together with the original Seed", () => {
  assert.ok(html.includes("在已观测 Seed 中搜索（含原 Seed）"));
  assert.ok(app.includes('result.seed_search?.mode === "observed_seeds_plus_original"'));
  assert.ok(app.includes("已观测 Seed + 原 Seed 搜索"));
  assert.ok(!html.includes("已观测 Seed 仅作备选"));
  assert.ok(!app.includes("实验性已观测 Seed"));
});

test("large consumption gaps are visibly downgraded without blocking write", () => {
  assert.ok(!html.includes("消耗偏差超过 25%"));
  assert.ok(html.includes('id="write-calibration-config"'));
  assert.ok(app.includes("duplicate.recommendation?.is_recommended === false"));
  assert.ok(app.includes('api("/parameters/write-calibration-config"'));
  assert.ok(app.includes("人工确认候选"));
  assert.ok(app.includes("calibration-recommendation"));
});

test("expected consumption distinguishes calibrated fit from historical context", () => {
  assert.ok(app.includes("function calibrationExpectedSourceCopy"));
  assert.ok(app.includes("按已有测试数据估算"));
  assert.ok(app.includes("仅保留原目标作参考"));
  assert.ok(app.includes("相似历史样本"));
  assert.ok(app.includes("相似历史样本估计_上下文"));
  assert.ok(app.includes("相似历史样本仅供对照"));
  assert.ok(!app.includes("预期消耗来自历史邻近关联"));
  assert.ok(!app.includes("expected?.value ?? association?.point"));
});

test("calibration result and recalculate controls remain aligned and scrollable", () => {
  assert.match(html, /class="calibration-upload-grid"[\s\S]*id="consumption-history-file"[\s\S]*id="consumption-config-level-file"/);
  assert.match(css, /\.calibration-upload-grid\s*\{[^}]*grid-template-columns:\s*repeat\(2,\s*minmax\(0,\s*1fr\)\)/);
  assert.match(css, /\.calibration-controls\s*\{[^}]*grid-template-columns:\s*repeat\(4,\s*minmax\(0,\s*1fr\)\)/);
  assert.doesNotMatch(html, /class="calibration-strategy-choice"/);
  assert.doesNotMatch(css, /\.calibration-controls\s*\{[^}]*display:\s*contents/);
  assert.match(css, /\.calibration-recalculate\s*\{[^}]*grid-template-columns:\s*92px 112px/);
  assert.match(css, /\.calibration-recalculate \.secondary-button\s*\{[^}]*white-space:\s*nowrap/);
  assert.match(css, /\.calibration-table-wrap\s*\{[^}]*overflow:\s*auto/);
  assert.match(css, /@media \(max-width: 650px\)[\s\S]*\.calibration-upload-grid\s*\{[^}]*grid-template-columns:\s*1fr/);
});
