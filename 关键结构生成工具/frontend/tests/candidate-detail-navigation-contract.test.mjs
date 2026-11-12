import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";

const html = fs.readFileSync(new URL("../index.html", import.meta.url), "utf8");
const app = fs.readFileSync(new URL("../assets/app.js", import.meta.url), "utf8");
const css = fs.readFileSync(new URL("../assets/styles.css", import.meta.url), "utf8");

test("candidate detail exposes labeled item and candidate arrow groups beside the primary actions", () => {
  assert.match(html, /class="focus-navigation-group"[^>]*aria-label="成品项切换[^>]*>[\s\S]*>成品项<[^]*id="focus-work-prev"[^>]*aria-label="上一成品项"[^>]*title="[^"\n]+"/);
  assert.match(html, /id="focus-work-position"[^>]*aria-live="polite"/);
  assert.match(html, /id="focus-work-next"[^>]*aria-label="下一成品项"[^>]*title="[^"\n]+"/);
  assert.match(html, /class="focus-navigation-group"[^>]*aria-label="候选切换"[^>]*>[\s\S]*>候选<[^]*id="focus-candidate-prev"[^>]*aria-label="上一个候选"[^>]*title="[^"\n]+"/);
  assert.match(html, /id="focus-candidate-position"[^>]*aria-live="polite"/);
  assert.match(html, /id="focus-candidate-next"[^>]*aria-label="下一个候选"[^>]*title="[^"\n]+"/);
  assert.match(html, /class="primary-button" id="close-focus"[^>]*>返回候选列表</);
  assert.match(css, /\.focus-navigation-arrow\s*\{[^}]*width:\s*36px[^}]*min-height:\s*36px[^}]*height:\s*36px[^}]*\}/s);
});

test("focus navigation derives previous and next candidates from the visible ordered sequence", () => {
  const start = app.indexOf("  function focusedCandidateNavigation");
  const end = app.indexOf("\n  function syncFocusCandidateNavigation", start);
  assert.notEqual(start, -1, "focusedCandidateNavigation helper must exist");
  assert.notEqual(end, -1, "focusedCandidateNavigation helper must remain independently testable");

  const ordered = [{ id: "recommended" }, { id: "regular-2" }, { id: "regular-5" }];
  let receivedItem = null;
  const focusedCandidateNavigation = Function(
    "activeWorkItem",
    "orderedCandidates",
    "candidateId",
    "state",
    `"use strict";${app.slice(start, end)};return focusedCandidateNavigation;`,
  )(
    () => ({ id: "active" }),
    (item) => { receivedItem = item; return ordered; },
    (candidate) => candidate?.id || "",
    { focusCandidate: ordered[1] },
  );

  const result = focusedCandidateNavigation();
  assert.deepEqual(receivedItem, { id: "active" });
  assert.equal(result.index, 1);
  assert.equal(result.previous.candidate.id, "recommended");
  assert.equal(result.previous.page, 0);
  assert.equal(result.next.candidate.id, "regular-5");
  assert.equal(result.next.page, 0);
  assert.equal(result.page, 0);

  const first = focusedCandidateNavigation({ id: "manual" }, ordered[0]);
  assert.equal(first.previous, null);
  assert.equal(first.next.candidate.id, "regular-2");

  const only = focusedCandidateNavigation({}, { id: "only" }, [{ id: "only" }]);
  assert.equal(only.previous, null);
  assert.equal(only.next, null);

  const missing = focusedCandidateNavigation({}, { id: "missing" }, ordered);
  assert.equal(missing.previous, null);
  assert.equal(missing.next, null);
});

test("returning after cross-page detail navigation restores the original list page and candidate", () => {
  const start = app.indexOf("  function focusedCandidateNavigation");
  const end = app.indexOf("\n  function setFocusLayer", start);
  assert.notEqual(start, -1, "candidate detail navigation helpers must exist");
  assert.notEqual(end, -1, "candidate detail navigation helpers must remain independently testable");

  const candidates = Array.from({ length: 4 }, (_, index) => ({
    id: `candidate-${index + 1}`,
    candidate: { plan_signature: "s5" },
    board: { bounds: { layer: { max: 1 } } },
  }));
  const item = { candidates, plan_signature: "s5" };
  const state = {
    candidatePage: 0,
    focusCandidate: null,
    focusReturnId: null,
    focusReturnPage: null,
    player: { unitIndex: 4, routeKey: "alternate", step: 9 },
  };
  let renderedPage = null;
  let focusedCandidateId = null;
  const classList = { add() {}, remove() {} };
  const elements = new Map([
    ["#focus-candidate-prev", { disabled: false }],
    ["#focus-candidate-next", { disabled: false }],
    ["#focus-workspace", { classList }],
    ["#candidate-workspace", { classList }],
    ["#compare-workspace", { classList }],
    ["#focus-layer", { max: "1" }],
    ["#focus-title", { textContent: "", focus() {} }],
    ["#focus-board-label", { textContent: "" }],
    ["#active-work-label", { focus() {} }],
  ]);
  const query = (selector) => {
    if (elements.has(selector)) return elements.get(selector);
    const match = selector.match(/^\[data-focus-candidate="([^"]+)"\]$/);
    return match ? { focus: () => { focusedCandidateId = match[1]; } } : null;
  };
  const navigation = Function(
    "activeWorkItem",
    "orderedCandidates",
    "candidateId",
    "state",
    "$",
    "stopPlayer",
    "renderCandidateCards",
    "renderCandidateWorkspace",
    "findCandidate",
    "successfulWorkItems",
    "workKey",
    "setFocusLayer",
    "displayCandidateIndex",
    "renderFocusSummary",
    "setupPlayer",
    "renderPlayer",
    "window",
    `"use strict";${app.slice(start, end)};return { openFocus, moveFocusedCandidate, closeFocus };`,
  )(
    () => item,
    () => candidates,
    (candidate) => candidate?.id || "",
    state,
    query,
    () => {},
    () => { renderedPage = state.candidatePage; },
    () => { renderedPage = state.candidatePage; },
    (id) => candidates.find((candidate) => candidate.id === id) || null,
    () => [item],
    () => "origin",
    () => {},
    (candidate) => candidates.indexOf(candidate) + 1,
    () => {},
    () => {},
    () => {},
    { scrollTo() {}, setTimeout(callback) { callback(); } },
  );

  navigation.openFocus("candidate-3");
  navigation.moveFocusedCandidate(1);
  assert.equal(state.focusCandidate, candidates[3], "next must cross from the first list page to the second");

  navigation.closeFocus();
  assert.equal(renderedPage, 0, "return must render the page where detail was first opened");
  assert.equal(focusedCandidateId, "candidate-3", "return must focus the candidate that opened detail");
  assert.deepEqual(state.player, { unitIndex: 0, routeKey: "threshold", step: 0 });

  assert.match(app, /#focus-candidate-prev[\s\S]*moveFocusedCandidate\(-1\)/);
  assert.match(app, /#focus-candidate-next[\s\S]*moveFocusedCandidate\(1\)/);
  assert.match(app, /#close-focus[\s\S]*closeFocus\(\)/);
});

function focusHarness(items, initial = {}) {
  const state = {
    jobResult: { items },
    activeWorkIndex: 0,
    candidatePage: 0,
    focusCandidate: null,
    focusReturnId: null,
    focusReturnPage: null,
    focusReturnWorkKey: null,
    selectedCandidates: new Map([["template-1::s5", ["a-2"]], ["template-2::s5", ["b-1"]]]),
    compareIds: new Set(),
    player: { unitIndex: 4, routeKey: "alternate", step: 9 },
    ...initial,
  };
  let restoredContext = null;
  let focusedId = null;
  const element = () => ({
    disabled: false, textContent: "", title: "", max: "1",
    classList: { add() {}, remove() {} },
    setAttribute() {},
    focus() {},
  });
  const elements = new Map([
    "#focus-candidate-prev", "#focus-candidate-next", "#focus-candidate-position",
    "#focus-work-prev", "#focus-work-next", "#focus-work-position",
    "#focus-workspace", "#candidate-workspace", "#compare-workspace",
    "#focus-layer", "#focus-title", "#focus-board-label", "#active-work-label",
  ].map((selector) => [selector, element()]));
  const query = (selector) => {
    const match = selector.match(/^\[data-focus-candidate="([^"]+)"\]$/);
    return match ? { focus: () => { focusedId = match[1]; } } : elements.get(selector) || null;
  };
  const start = app.indexOf("  function focusedCandidateNavigation");
  const end = app.indexOf("\n  function setFocusLayer", start);
  const workStart = app.indexOf("  function successfulWorkItems()");
  const workEnd = app.indexOf("\n  function sourceTemplateMetrics", workStart);
  const orderingStart = app.indexOf("  function orderedCandidates(");
  const orderingEnd = app.indexOf("\n  function candidateRiskWarnings", orderingStart);
  const restore = () => { restoredContext = { workIndex: state.activeWorkIndex, page: state.candidatePage }; };
  const navigation = Function(
    "state", "$", "clamp", "candidateRecommendation", "candidateId", "stopPlayer", "renderCandidateCards",
    "renderCandidateWorkspace", "setFocusLayer", "displayCandidateIndex", "renderFocusSummary", "setupPlayer", "renderPlayer", "window",
    `"use strict";${app.slice(workStart, workEnd)}${app.slice(orderingStart, orderingEnd)}
      function findCandidate(id) { return activeWorkItem()?.candidates?.find(candidate => candidateId(candidate) === id) || null; }
      ${app.slice(start, end)}
      return { openFocus, moveFocusedCandidate, moveFocusedWorkItem, syncFocusCandidateNavigation, closeFocus };`,
  )(
    state, query, (value, minimum, maximum) => Math.min(maximum, Math.max(minimum, value)),
    (_item, generated) => generated.recommendation || { recommended: false, rank: null }, (candidate) => candidate?.id || "", () => {}, restore, restore,
    () => {}, (candidate) => candidate?.candidate_index || 1, () => {}, () => {}, () => {},
    { scrollTo() {}, setTimeout(callback) { callback(); } },
  );
  return { state, elements, navigation, get restoredContext() { return restoredContext; }, get focusedId() { return focusedId; } };
}

const candidate = (id, index = 1) => ({ id, candidate_index: index, candidate: { plan_signature: "s5" }, board: { bounds: { layer: { max: 1 } } } });
const work = (template, candidates, plan = "s5") => ({ template_key: template, plan_id: plan, plan_signature: plan, candidates });

test("detail item arrows skip empty items, preserve selections, and open the next item's list-first candidate", () => {
  const a = [candidate("a-1"), candidate("a-2", 2)];
  const b = [candidate("b-1"), { ...candidate("b-first", 3), recommendation: { recommended: true, rank: 1 } }];
  const harness = focusHarness([work("template-1", a), work("empty", []), work("template-2", b)]);
  const selections = structuredClone(harness.state.selectedCandidates);

  harness.navigation.openFocus("a-2");
  assert.equal(harness.elements.get("#focus-work-prev").disabled, true);
  assert.equal(harness.elements.get("#focus-work-next").disabled, false);
  assert.equal(harness.elements.get("#focus-work-position").textContent, "1 / 2");
  harness.navigation.moveFocusedWorkItem(1);

  assert.equal(harness.state.activeWorkIndex, 1);
  assert.equal(harness.state.focusCandidate.id, "b-first");
  assert.equal(harness.elements.get("#focus-candidate-position").textContent, "1 / 2");
  assert.equal(harness.elements.get("#focus-work-position").textContent, "2 / 2");
  assert.equal(harness.elements.get("#focus-work-next").disabled, true);
  assert.match(harness.elements.get("#focus-work-position").title, /跳过 1 个无候选成品项/);
  assert.match(harness.elements.get("#focus-title").textContent, /template-2.*s5/);
  assert.deepEqual(harness.state.selectedCandidates, selections);

  harness.navigation.moveFocusedCandidate(1);
  assert.equal(harness.state.focusCandidate.id, "b-1");
  harness.navigation.moveFocusedWorkItem(-1);
  assert.equal(harness.state.focusCandidate.id, "a-1");
  assert.deepEqual(harness.state.selectedCandidates, selections);
});

test("return after navigating through other items restores the original item's candidate page and trigger", () => {
  const a = Array.from({ length: 6 }, (_, index) => candidate(`a-${index + 1}`, index + 1));
  const harness = focusHarness([work("template-1", a), work("template-1", [candidate("other-plan")], "s6"), work("template-2", [candidate("b-1")])], { candidatePage: 1 });
  const selections = structuredClone(harness.state.selectedCandidates);

  harness.navigation.openFocus("a-5");
  harness.navigation.moveFocusedWorkItem(1);
  harness.navigation.moveFocusedWorkItem(1);
  harness.navigation.closeFocus();

  assert.deepEqual(harness.restoredContext, { workIndex: 0, page: 1 });
  assert.equal(harness.focusedId, "a-5");
  assert.deepEqual(harness.state.selectedCandidates, selections);
  assert.equal(harness.state.focusReturnWorkKey, null);
});

test("single or missing candidate items disable detail arrows without changing the active item", () => {
  const harness = focusHarness([work("empty", []), work("template-1", [candidate("a-1")])]);
  harness.navigation.openFocus("a-1");
  for (const selector of ["#focus-candidate-prev", "#focus-candidate-next", "#focus-work-prev", "#focus-work-next"]) {
    assert.equal(harness.elements.get(selector).disabled, true, selector);
  }
  harness.navigation.moveFocusedWorkItem(-1);
  harness.navigation.moveFocusedWorkItem(1);
  assert.equal(harness.state.activeWorkIndex, 0);
  assert.equal(harness.state.focusCandidate.id, "a-1");

  harness.state.jobResult.items = [work("empty", [])];
  harness.state.focusCandidate = null;
  harness.navigation.syncFocusCandidateNavigation();
  assert.equal(harness.elements.get("#focus-work-position").textContent, "0 / 0");
  assert.equal(harness.elements.get("#focus-candidate-position").textContent, "0 / 0");
  harness.navigation.moveFocusedWorkItem(1);
  assert.equal(harness.state.activeWorkIndex, 0);
});
