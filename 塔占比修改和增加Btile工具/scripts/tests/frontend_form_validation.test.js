"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const test = require("node:test");

const source = fs.readFileSync(path.join(__dirname, "../../frontend/assets/app.js"), "utf8");

function functionSource(name) {
  const start = source.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `Missing application function ${name}`);
  const next = source.indexOf("\nfunction ", start + 1);
  return source.slice(start, next < 0 ? undefined : next);
}

// A small form fixture models display:none: focusing a hidden control has no
// effect. The validation and editor-transition functions are the real app code.
function fixture({mode = "automatic", customEditor = false, layer = 20, tower = -1, importedFiles = [], overrides = {}} = {}) {
  const elements = new Map();
  const field = (id, value, parentId = "batch-parameter-fields") => {
    const element = {id, value: String(value), parentId, hidden: false, invalid: false, max: "9"};
    elements.set(id, element);
    return element;
  };
  ["batch-parameter-fields", "parameter-override-editor", "advanced-tower-card", "parameter-editor-title", "restore-batch-parameters"].forEach((id) => {
    elements.set(id, {id, hidden: false, innerHTML: "", textContent: ""});
  });
  elements.get("batch-parameter-fields").hidden = customEditor;
  elements.get("parameter-override-editor").hidden = !customEditor;
  elements.get("advanced-tower-card").hidden = customEditor || mode !== "advanced";
  field("target-ratio", 25);
  field("min-target-ratio", 0);
  field("layer-limit", layer);
  field("max-new-layers", 2);
  field("placement-strategy", "top_first");
  field("random-seed", 1);
  field("tower-mode", mode);
  Object.keys(overrides).forEach((id) => field(`override-ratio-${id}`, overrides[id].ratio * 100, "parameter-override-editor"));
  const towerInput = field("tower-a-0", tower, "advanced-tower-card");
  let focused = "";
  const form = {};
  const context = {
    VM: require("../../frontend/assets/view-model.js"),
    state: {overrides, importedFiles, activeOverride: customEditor ? "a" : ""},
    byId: (id) => elements.get(id),
    all: (selector) => selector === "[data-tower-file]" ? [towerInput] : [],
    clearInvalid: () => elements.forEach((element) => { element.invalid = false; }),
    setInvalid: (element, message) => { element.invalid = true; element.error = message; },
    focusFirstInvalid: () => {
      const element = [...elements.values()].find((item) => item.invalid);
      if (!element) return false;
      if (!element.hidden && !elements.get(element.parentId)?.hidden) focused = element.id;
      return true;
    },
    updateParameterSummaries: () => {},
    showToast: () => {},
    setOverrideMode: (fileId) => {
      context.state.activeOverride = fileId;
      elements.get("parameter-override-editor").hidden = false;
    },
  };
  vm.createContext(context);
  vm.runInContext(`${functionSource("showBatchEditor")}\n${functionSource("validateConfiguration")}`, context);
  return {context, elements, validate: () => context.validateConfiguration(form), focus: () => focused};
}

test("automatic tower allocation ignores invalid values in the inactive advanced panel", () => {
  const view = fixture({mode: "automatic", tower: -1});
  assert.equal(view.validate(), true);
  assert.equal(view.elements.get("tower-a-0").invalid, false);
  assert.equal(view.elements.get("advanced-tower-card").hidden, true);
});

test("an invalid advanced tower value reveals its panel before focusing the field", () => {
  const view = fixture({mode: "advanced", customEditor: true, tower: -1});
  assert.equal(view.validate(), false);
  assert.equal(view.elements.get("batch-parameter-fields").hidden, false);
  assert.equal(view.elements.get("advanced-tower-card").hidden, false);
  assert.equal(view.elements.get("parameter-override-editor").hidden, true);
  assert.equal(view.focus(), "tower-a-0");
});

test("invalid batch defaults reopen the batch editor while a template editor is visible", () => {
  const view = fixture({mode: "automatic", customEditor: true, layer: 0, tower: 3});
  assert.equal(view.validate(), false);
  assert.equal(view.elements.get("batch-parameter-fields").hidden, false);
  assert.equal(view.elements.get("parameter-override-editor").hidden, true);
  assert.equal(view.focus(), "layer-limit");
});

test("27 percent batch ceiling is allowed for 35.4 and 29.3 percent source templates", () => {
  const view = fixture({importedFiles: [{fileId: 'a', templateId: 'high', ratio: .354}, {fileId: 'b', templateId: 'low', ratio: .293}]});
  view.elements.get('target-ratio').value = '27';
  assert.equal(view.validate(), true);
});

test("batch ceiling above one source identifies that template and preserves the entered value", () => {
  const view = fixture({importedFiles: [{fileId: 'a', templateId: 'high', ratio: .354}, {fileId: 'b', templateId: 'low', ratio: .293}]});
  view.elements.get('target-ratio').value = '30';
  assert.equal(view.validate(), false);
  assert.match(view.elements.get('target-ratio').error, /low.*29\.3/);
  assert.equal(view.elements.get('target-ratio').value, '30');
});

test("a per-template override may satisfy its source without reducing another template batch ceiling", () => {
  const view = fixture({importedFiles: [{fileId: 'a', templateId: 'high', ratio: .354}, {fileId: 'b', templateId: 'low', ratio: .293}], overrides: {b: {ratio: .28}}});
  view.elements.get('target-ratio').value = '35';
  assert.equal(view.validate(), true);
  assert.equal(view.elements.get('target-ratio').value, '35');
});
