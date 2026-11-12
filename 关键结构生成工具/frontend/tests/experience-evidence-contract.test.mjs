import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";

const html = fs.readFileSync(new URL("../index.html", import.meta.url), "utf8");
const app = fs.readFileSync(new URL("../assets/app.js", import.meta.url), "utf8");
const css = fs.readFileSync(new URL("../assets/styles.css", import.meta.url), "utf8");

const helperStart = app.indexOf("  function hasReliableDifficultyDistance");
const helperEnd = app.indexOf("\n  function candidateRecommendation", helperStart);
assert.notEqual(helperStart, -1, "experience evidence helpers must exist");
assert.notEqual(helperEnd, -1, "experience evidence helpers must remain independently testable");

const helperSource = app.slice(helperStart, helperEnd);
const helpers = Function(
  `"use strict";
  const firstDifficultyValue = (...values) => values.find((value) => value !== undefined && value !== null && value !== "");
  const difficultyNumber = (...values) => { const raw = firstDifficultyValue(...values); const value = Number(raw); return Number.isFinite(value) ? value : null; };
  const candidateDifficultyReport = () => ({ distance: null });
  const difficultyPercent = (value) => Number.isFinite(value) ? \`${"${Math.abs(value) <= 1 ? value * 100 : value}"}%\` : "—";
  const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[character]);
  ${helperSource}
  return { experienceAnalysis, experienceSummaryMarkup, experienceEvidenceMarkup, experienceOnlineStyleCopy,
    focusExperienceSummaryMarkup: typeof focusExperienceSummaryMarkup === "function" ? focusExperienceSummaryMarkup : null };`,
)();

test("experience controls stay inside advanced settings and map to quality fields", () => {
  const advancedStart = html.indexOf('<details class="advanced-panel">');
  for (const id of ["experience-archetype", "special-preset", "long-range-wave-count", "online-style-reference"]) {
    assert.ok(html.indexOf(`id="${id}"`) > advancedStart, `${id} must remain advanced`);
  }
  for (const value of ["auto", "3", "4", "intrinsic_gate", "board_reveal", "hybrid", "none", "all_types_long_range", "serial_mechanism", "level_200_300_v1", "off"]) {
    assert.ok(html.includes(`value="${value}"`));
  }
  assert.match(app, /experience_archetype:\s*"auto"/);
  assert.match(app, /special_preset:\s*"none"/);
  assert.match(app, /long_range_wave_count:\s*"auto"/);
  assert.match(app, /online_style_reference:\s*"level_200_300_v1"/);
  assert.ok(app.includes('experience_archetype: $("#experience-archetype").value'));
  assert.ok(app.includes('long_range_wave_count: $("#long-range-wave-count").value'));
  assert.ok(css.includes(".experience-option-row"));
});

test("special presets are gated by plan shape before submission", () => {
  assert.match(app, /function specialPresetCompatibility\(plan, preset\)/);
  assert.match(app, /option\.disabled\s*=\s*!compatibility\.ok/);
  assert.match(app, /if \(!presetCompatibility\.ok\) throw new Error/);
  assert.match(app, /all_types_long_range[\s\S]*Number\(plan\.unitCount\) !== 1/);
  assert.match(app, /serial_mechanism[\s\S]*Number\(plan\.unitCount\) !== 2/);
  assert.match(app, /serial_mechanism[\s\S]*plan\.layout !== "z"/);
  assert.match(app, /\["auto", "3", "4"\]/);
  assert.match(html, /\u5168\u7c7b\u578b\u957f\u7a0b\u578b[\s\S]{0,120}C–N 12 \u7c7b×3/);
});

test("candidate summary exposes only reliable archetype, difficulty distance, and mechanism pressure", () => {
  const candidate = {
    candidate: {
      experience_analysis: {
        archetype: { primary: "board_reveal" },
        difficulty_distance: { total: 0.125 },
        simulation_pressure: { status: "directional", level: "high" },
        evidence_coverage: { config_coverage: "exact_unique", materialization_fidelity: "reconstructed" },
      },
    },
  };
  const markup = helpers.experienceSummaryMarkup(candidate);
  assert.equal((markup.match(/<div(?:\s[^>]*)?>/g) || []).length - 1, 3);
  assert.match(markup, /体验原型/);
  assert.match(markup, /整板揭露型/);
  assert.match(markup, /与原模板难度距离/);
  assert.match(markup, />0\.13</);
  assert.match(markup, /机制压力/);
  assert.match(markup, /高 · 不优先推荐/);
  assert.doesNotMatch(markup, /体验风险|玩家风险/);

  const withoutPressure = helpers.experienceSummaryMarkup(candidate, {
    includePressure: false,
    showEmptyGuidance: false,
  });
  assert.doesNotMatch(withoutPressure, /机制压力|不优先推荐/);
});

test("detail heading pairs archetype before pressure without duplicating supporting metrics", () => {
  const candidate = { candidate: { experience_analysis: {
    archetype: { primary: "board_reveal" },
    difficulty_distance: { total: 0.125 },
    simulation_pressure: { status: "directional", level: "high" },
    evidence_coverage: { config_coverage: "exact_unique", materialization_fidelity: "reconstructed" },
  } } };
  assert.equal(typeof helpers.focusExperienceSummaryMarkup, "function");
  const heading = helpers.focusExperienceSummaryMarkup(candidate);
  assert.ok(heading.indexOf("体验原型") < heading.indexOf("机制压力"));
  assert.match(heading, /整板揭露型/);
  assert.match(heading, /高 · 不优先推荐/);
  const supporting = helpers.experienceSummaryMarkup(candidate, {
    includeArchetype: false, includePressure: false, showEmptyGuidance: false,
  });
  assert.match(supporting, /与原模板难度距离/);
  assert.doesNotMatch(supporting, /体验原型|机制压力/);
  assert.equal(helpers.focusExperienceSummaryMarkup({}), "");
});

test("experience detail renders every evidence family with actual returned components", () => {
  const candidate = {
    candidate: {
      experience_signature: "exp-v1-demo",
      experience_analysis: {
        hard_validation: { status: "passed", passed: true, reasons: ["无循环死锁"] },
        archetype: { requested: "auto", primary: "hybrid", scores: { intrinsic_gate: 0.61, board_reveal: 0.78, hybrid: 0.91 }, classification_confidence: 0.84 },
        difficulty_distance: { total: 0.182, components: { path_failure_delta: 0.06 } },
        online_style_distance: { status: "directional", profile_version: "level_200_300_v1", distance: 0.241, components: { cluster_distance: 0.08 } },
        simulation_pressure: { status: "directional", level: "medium", signals: ["悬置两张路径较多"] },
        observed_risk: { status: "unavailable", reason: "not calibrated" },
        evidence_coverage: { static_geometry: "complete", dynamic_reveal: "directional", risk_calibration: "unavailable", config_coverage: "exact_unique", materialization_fidelity: "reconstructed" },
        reveal_curve: { status: "directional", stages: [{ exposed_count: 3, completed_groups: 1, released_ab: 2 }] },
      },
    },
  };
  const markup = helpers.experienceEvidenceMarkup(candidate);
  for (const heading of ["结构规则与可完成路径", "体验原型", "与原模板难度距离", "线上风格距离", "机制压力", "已观测玩家风险", "证据覆盖", "整板揭露曲线"]) {
    assert.ok(markup.includes(heading), `${heading} evidence must render`);
  }
  assert.match(markup, /无循环死锁/);
  assert.match(markup, /0\.241/);
  assert.match(markup, /0\.080/);
  assert.match(markup, /已暴露 C–N/);
  assert.match(markup, /机制压力不等于玩家风险/);
  assert.match(markup, /查看全部技术明细/);
});

test("whole-board evidence summarizes five equal-weight stages and the online dynamic reference", () => {
  const distribution = (median, minimum = median, maximum = median) => ({ median, minimum, maximum });
  const curve = (offset) => ({
    curve: [0, 1, 2, 3, 4].map((stage) => ({
      progress_from: stage / 5,
      progress_to: (stage + 1) / 5,
      cumulative_exposed_structure_count: distribution(offset + stage, offset + stage - 1, offset + stage + 1),
      complete_structure_group_type_count: distribution(Math.floor(stage / 2)),
      dangling_one_type_count: distribution(stage % 2),
      dangling_two_type_count: distribution((stage + 1) % 2),
    })),
    first_three_structure_matches: { "1": { progress: distribution(0.4, 0.3, 0.5) } },
    released_ab_per_structure_click: distribution(0.8, 0, 2),
    released_ab_on_match_completion_click: distribution(2, 1, 3),
    released_ab_total_per_path: distribution(7, 5, 9),
  });
  const candidate = {
    candidate: {
      experience_analysis: {
        archetype: { primary: "board_reveal" },
        difficulty_distance: {
          primary_distance: 0.21,
          reveal_reference_distance: {
            status: "compared",
            reference_selection_status: "size_reference",
            effective_group: "9/all",
            reference_sample_size: 18,
            reference_member_count: 44,
            primary_distance: 0.214,
            coverage: 0.96,
          },
        },
        reveal_curve: {
          version: "multi-path-reveal-v2",
          per_duplicate_strategy_reveal: [
            {
              duplicate_index: 1,
              strategies: {
                random: { reveal_curve: curve(1) },
                greedy: { reveal_curve: curve(2) },
              },
            },
          ],
        },
      },
    },
  };

  const markup = helpers.experienceEvidenceMarkup(candidate);
  assert.equal((markup.match(/class="reveal-stage-row"/g) || []).length, 5);
  for (const stage of ["0–20%", "20–40%", "40–60%", "60–80%", "80–100%"]) {
    assert.match(markup, new RegExp(stage));
  }
  for (const copy of ["累计暴露 C–N", "悬置 1 / 2 张", "每次结构点击释放 A/B", "三消点击释放 A/B", "单路径累计释放 A/B", "首组三消位置"]) {
    assert.ok(markup.includes(copy), `${copy} must render`);
  }
  assert.match(markup, /线上动态参考/);
  assert.match(markup, /已比对/);
  assert.match(markup, /9\/all · 18 个同组模板/);
  assert.match(markup, /机制距离 0\.214 · 覆盖 96%/);
  assert.doesNotMatch(markup, /\[object Object\]/);
  for (const selector of [".reveal-reference-strip", ".reveal-stage-table", ".reveal-release-summary", "@media (max-width: 480px)"]) {
    assert.ok(css.includes(selector), `${selector} responsive styling must exist`);
  }
});

test("whole-board evidence degrades cleanly when stage or reference data is incomplete", () => {
  const markup = helpers.experienceEvidenceMarkup({
    candidate: {
      experience_analysis: {
        reveal_curve: { version: "legacy" },
        difficulty_distance: {
          reveal_reference_distance: { status: "inconclusive", reason: "no_exact_size_dynamic_reference" },
        },
      },
    },
  });
  assert.match(markup, /参考样本不足/);
  assert.match(markup, /机制距离暂不展示/);
  assert.match(markup, /未返回可汇总的 5 阶段路径/);
  assert.doesNotMatch(markup, /undefined|null|\[object Object\]/);

  const sourceBaseline = helpers.experienceEvidenceMarkup({
    candidate: {
      experience_analysis: {
        difficulty_distance: { whole_board_reveal_baseline: { mode: "source_template", source_has_cn: true } },
      },
    },
  });
  assert.match(sourceBaseline, /动态基线/);
  assert.match(sourceBaseline, /与源模板直接比对/);
  assert.match(sourceBaseline, /当前源模板/);
  assert.doesNotMatch(sourceBaseline, /线上动态参考/);
});

test("uncertain archetype is omitted from cards and details instead of rendered as a value", () => {
  const candidate = {
    candidate: {
      experience_analysis: {
        archetype: {
          primary: null,
          automatic_primary: "hybrid",
          classification_status: "uncertain",
          classification_confidence: 0.42,
          top2: [
            { archetype: "hybrid", score: 0.51 },
            { archetype: "board_reveal", score: 0.49 },
          ],
          requested_matches_automatic: false,
        },
      },
    },
  };
  const report = helpers.experienceAnalysis(candidate);
  const summary = helpers.experienceSummaryMarkup(candidate);
  const detail = helpers.experienceEvidenceMarkup(candidate);

  assert.equal(report.archetypeKey, "uncertain");
  assert.doesNotMatch(summary, /体验原型|原型未确定|未形成可靠分类/);
  assert.doesNotMatch(detail, /体验原型|原型未确定|候选原型 Top 2/);
  assert.match(summary, /暂无可用于排序的体验指标/);
});

test("synthetic difficulty distance is omitted from cards and details", () => {
  const candidate = {
    candidate: {
      experience_analysis: {
        difficulty_distance: { total: 0.125 },
        evidence_coverage: { config_coverage: "exact_unique", materialization_fidelity: "synthetic" },
      },
    },
  };
  const summary = helpers.experienceSummaryMarkup(candidate);
  const detail = helpers.experienceEvidenceMarkup(candidate);
  assert.doesNotMatch(summary, /与原模板难度距离|配置不足|仅方向性/);
  assert.doesNotMatch(detail, /与原模板难度距离|配置不足|仅方向性/);
  assert.match(detail, /证据覆盖/);
  assert.match(detail, /简化模拟/);
});

test("special preset objects and disabled style references render as user-facing copy", () => {
  const candidate = {
    candidate: {
      experience_signature: "exp-v2-demo",
      experience_analysis: {
        special_preset: { requested: "serial_mechanism", status: "matched" },
        archetype: { primary: "hybrid" },
        online_style_distance: { status: "off", total: 0 },
      },
    },
  };
  const report = helpers.experienceAnalysis(candidate);
  const markup = helpers.experienceEvidenceMarkup(candidate);
  assert.equal(report.specialPreset, "serial_mechanism");
  assert.equal(helpers.experienceOnlineStyleCopy(report), "未启用");
  assert.match(markup, /特殊预设 串行机关型/);
  assert.doesNotMatch(markup, /\[object Object\]/);
  assert.doesNotMatch(markup, />0\.000</);
});

test("legacy candidates degrade to directional reference without throwing", () => {
  assert.doesNotThrow(() => helpers.experienceEvidenceMarkup({ candidate: {} }));
  const summary = helpers.experienceSummaryMarkup({ candidate: {} });
  const detail = helpers.experienceEvidenceMarkup({ candidate: {} });
  assert.match(summary, /暂无可用于排序的体验指标/);
  assert.doesNotMatch(summary, /体验原型|与原模板难度距离|机制压力/);
  assert.match(detail, /旧记录/);
  assert.match(detail, /未评估/);
  assert.doesNotMatch(detail, /体验原型|与原模板难度距离/);
  assert.doesNotMatch(app, /结构位置、对称性与疏密分布已统一纳入综合质量分/);
});

test("legacy automatic archetype and distance without evidence stay hidden", () => {
  const legacy = {
    candidate: {
      experience_analysis: {
        archetype: { automatic_primary: "hybrid" },
        difficulty_distance: { total: 0.125 },
      },
    },
  };
  const summary = helpers.experienceSummaryMarkup(legacy);
  const detail = helpers.experienceEvidenceMarkup(legacy);
  assert.doesNotMatch(summary, /体验原型|与原模板难度距离/);
  assert.doesNotMatch(detail, /体验原型|与原模板难度距离/);
});
