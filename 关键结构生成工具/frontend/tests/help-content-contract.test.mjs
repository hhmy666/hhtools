import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";

const root = path.resolve(import.meta.dirname, "..");
const html = fs.readFileSync(path.join(root, "index.html"), "utf8");
const css = fs.readFileSync(path.join(root, "assets", "styles.css"), "utf8");
const app = fs.readFileSync(path.join(root, "assets", "app.js"), "utf8");

const helpMarkup = html.slice(html.indexOf('<dialog class="help-dialog"'), html.indexOf("</dialog>", html.indexOf('<dialog class="help-dialog"')) + 9);
assert.ok(helpMarkup.length > 0, "help dialog must exist");

const topicIds = [...helpMarkup.matchAll(/<section class="help-topic[^\"]*" id="([^"]+)" data-help-topic(?:\s|>)/g)].map((match) => match[1]);
assert.equal(topicIds.length, 15, "help must expose all 15 reviewable topics");
assert.deepEqual(topicIds, [
  "help-start", "help-setup", "help-import", "help-plan", "help-generation",
  "help-candidates", "help-board", "help-export", "help-tasks", "help-reports",
  "help-parameters", "help-calibration", "help-settings", "help-glossary", "help-faq",
]);

assert.equal((helpMarkup.match(/data-help-nav-group/g) || []).length, 4, "directory must use four stable information groups");
assert.ok(helpMarkup.includes('id="help-search-input"'), "help must provide in-dialog search");
assert.ok(helpMarkup.includes('placeholder="搜索使用说明"'), "help search must use concise user-facing copy");
assert.ok(helpMarkup.includes('id="help-search-summary" role="status" aria-live="polite" hidden'), "search feedback must stay hidden until a query exists");
assert.ok(helpMarkup.includes('id="help-search-results"'), "search must expose subsection-level results instead of only filtering whole topics");
assert.ok(helpMarkup.includes('id="help-current-topic"'), "help header must show the currently viewed topic");
assert.ok(helpMarkup.includes('id="help-start-link"'), "help header must provide a first-use shortcut");
assert.ok(helpMarkup.includes("规则与证据"), "the rules group must use a clear section title");

for (const requiredCopy of [
  "StrategyGroupId 非必需",
  "不推荐不等于禁止",
  "一键配置新参数",
  "目标消耗校准",
  "任务记录",
  "验证报告",
  "服务重启后",
  "不改策略表，也不发布线上配置",
  "规则与阈值总表",
  "必须理解",
  "按需查看",
  "技术口径",
  "参数梯度方案，不是消耗复刻",
  "8|0|100|6",
  "8/6 → 7/6 → 7/5 → 6/5 → 6/4 → 5/4 → 5/3 → 4/3 → 4/3 → 4/3",
  "Wiki 页面 277080406 · PRD v85（已核对）",
  "默认 6 个候选",
  "默认 2,000 次",
  "最大层间距最多 5 层",
  "位置不限制、对称关闭、分布紧凑",
  "每种结构块组数",
  "2；1；1",
  "2；2；2；1；1",
  "上一个候选",
  "下一个候选",
  "返回候选列表",
  "和原关卡一致、1003、1004",
  "需要更严格筛选时进入精准模式",
  "3 个候选、每个成品项搜索上限 20,000 次、最大层间距 5 层、位置不限制、对称关闭、分布紧凑",
  "机制压力为低或中的候选",
  "结果可以少于 3 个，不会用未达标候选回填",
  "下拉选择",
]) {
  assert.ok(helpMarkup.includes(requiredCopy), `help must cover: ${requiredCopy}`);
}

assert.equal(
  (helpMarkup.match(/<code(?:\s[^>]*)?>/g) || []).length,
  0,
  "help prose must keep field names, filenames and parameter examples as natural inline text instead of code chips",
);
for (const inlineCopy of ["s5 + s6", "_m.bytes", "config_level.csv"]) {
  assert.ok(helpMarkup.includes(inlineCopy), `help must preserve inline copy after removing code chips: ${inlineCopy}`);
}

assert.ok(css.includes("grid-template-columns: 236px minmax(0, 1fr)"), "desktop help must keep a stable directory and reading pane");
assert.ok(css.includes(".help-search-summary { position: sticky"), "search result context must remain visible while reading");
assert.ok(css.includes(".help-nav-group + .help-nav-group"), "directory groups must have visible section separation");
assert.ok(css.includes(".help-nav-group[hidden], .help-nav a[hidden] { display: none; }"), "search must remove unrelated directory groups and links");
assert.ok(css.includes(".help-topic[hidden] { display: none; }"), "search filtering must have deterministic layout behavior");
assert.ok(helpMarkup.includes("打开使用说明时会自动定位到当前功能。"), "help header must explain automatic positioning");
assert.match(css, /\.help-nav\s*\{[^}]*overflow:\s*hidden;/, "left directory must not create its own scrollbar");
assert.match(css, /\.help-content\s*\{[^}]*overflow-y:\s*auto;/, "help content must remain the only vertical scrolling pane");

for (const helper of ["currentHelpTopic", "filterHelpTopics", "openHelpDialog", "syncHelpTopicFromScroll"]) {
  assert.ok(app.includes(`function ${helper}`), `help behavior must define ${helper}`);
}
for (const helper of ["helpSearchTerms", "renderHelpSearchResults", "updateHelpLocation", "openHelpTarget"]) {
  assert.ok(app.includes(`function ${helper}`), `help behavior must define ${helper}`);
}
for (const alias of ["随机种子", "策略组", "副本参数", "结构降难"]) {
  assert.ok(app.includes(alias), `help search must support alias: ${alias}`);
}
assert.ok(app.includes("<mark>"), "help search results must highlight the matching term");
assert.ok(helpMarkup.includes('data-help-level="must"'), "critical rules must be visibly classified as must understand");
assert.ok(helpMarkup.includes('data-help-level="optional"'), "optional explanations must be visibly classified as on demand");
assert.ok(helpMarkup.includes('data-help-level="technical"'), "technical evidence must be visibly classified as technical convention");
assert.ok(helpMarkup.includes('data-help-target="help-parameters-gradient"'), "complex configuration sections must expose reusable help targets");
assert.ok(app.includes('consumption: "help-calibration"'), "consumption page must open the matching help topic");
assert.ok(app.includes('parameters: "help-parameters"'), "parameter page must open the matching help topic");

const helpNavHandler = app.slice(
  app.indexOf('const link = event.target.closest?.(".help-nav a")'),
  app.indexOf('$("#help-dialog").addEventListener("close"'),
);
assert.match(
  helpNavHandler,
  /search\.value = "";[\s\S]*filterHelpTopics\(""\);[\s\S]*scrollToHelpTopic/,
  "clicking the directory during search must restore hidden topics before scrolling",
);
assert.match(
  app.slice(app.indexOf("function filterHelpTopics"), app.indexOf("function openHelpDialog")),
  /if \(search\.groups\.length\) setActiveHelpTopic\(""\);[\s\S]*else syncHelpTopicFromScroll\(\);/,
  "clearing search must restore the visible topic title even when no scroll event fires",
);

console.log("help content contract passed");
