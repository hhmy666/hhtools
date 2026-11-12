"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const test = require("node:test");
const VM = require("../../frontend/assets/view-model.js");
const app = fs.readFileSync(path.join(__dirname, "../../frontend/assets/app.js"), "utf8");
const help = fs.readFileSync(path.join(__dirname, "../../frontend/assets/help-content.js"), "utf8");

function fixture() {
  const context = {window: {matchMedia: () => ({matches: true})}, VM, state: {helpTopic: "start"}};
  vm.createContext(context);
  vm.runInContext(help, context);
  const nodes = new Map();
  for (const id of ["help-search-input", "clear-help-search", "help-search-summary", "help-current-topic", "help-navigation", "help-content", "help-reading"]) nodes.set(id, {id, hidden: false, value: "", textContent: "", innerHTML: ""});
  const content = nodes.get("help-content");
  content.scrollTop = 0;
  content.getBoundingClientRect = () => ({top: 100});
  content.scrollTo = ({top}) => { content.scrollTop = top; };
  let sections = [];
  let focused = "";
  let html = "";
  nodes.set("help-topic-title", {focus: () => { focused = context.state.helpTopic; }});
  Object.defineProperty(nodes.get("help-reading"), "innerHTML", {
    get: () => html,
    set(value) {
      html = value;
      for (const section of sections) nodes.delete(section.id);
      sections = [...value.matchAll(/<section\b[^>]*id="([^"]+)"[^>]*data-help-section="([^"]+)"/g)].map((match, index) => {
        const section = {id: match[1], dataset: {helpSection: match[2]}, getBoundingClientRect: () => ({top: 128 + index * 500 - content.scrollTop}), querySelector: () => ({focus: () => { focused = match[2]; }})};
        nodes.set(section.id, section);
        return section;
      });
    },
  });
  const navigation = Array.from(context.window.TowerReducerHelp, topic => ({dataset: {helpTopic: topic.id}, attrs: {}, classList: {toggle() {}}, setAttribute(name, value) { this.attrs[name] = value; }, removeAttribute(name) { delete this.attrs[name]; }}));
  context.byId = id => nodes.get(id) || null;
  context.all = selector => selector === "[data-help-section]" ? sections : navigation;
  context.escapeHTML = value => String(value);
  context.getComputedStyle = () => ({paddingTop: "28px"});
  vm.runInContext(app.slice(app.indexOf("function initializeHelp("), app.indexOf("function bindEvents(")), context);
  return {context, nodes, content, navigation, focus: () => focused};
}

test("navigating to a chapter keeps every chapter in one continuous document", () => {
  const view = fixture();
  view.context.renderHelpTopic("config", true);
  const html = view.nodes.get("help-reading").innerHTML;
  for (const topic of view.context.window.TowerReducerHelp) assert.ok(html.includes(topic.intro), topic.id);
  assert.equal(view.content.scrollTop, 1000);
  assert.equal(view.focus(), "config");
  view.context.renderHelpTopic("sync", true);
  assert.equal(view.nodes.get("help-reading").innerHTML, html);
  assert.equal(view.content.scrollTop, 2500);
});

test("manual reading scroll updates the navigation without replacing content", () => {
  const view = fixture();
  view.context.renderHelpTopic("start");
  const html = view.nodes.get("help-reading").innerHTML;
  view.content.scrollTop = 1150;
  view.context.updateHelpReadingPosition();
  assert.equal(view.context.state.helpTopic, "config");
  assert.equal(view.navigation.find(item => item.dataset.helpTopic === "config").attrs["aria-current"], "location");
  assert.equal(view.nodes.get("help-reading").innerHTML, html);
});

test("a search result returns to the full document at its chapter", () => {
  const view = fixture();
  view.context.renderHelpTopic("import");
  view.nodes.get("help-search-input").value = "五五开";
  view.context.searchHelp();
  assert.ok(view.nodes.get("help-reading").innerHTML.includes('data-help-topic="config"'));
  view.context.renderHelpTopic("config");
  for (const topic of view.context.window.TowerReducerHelp) assert.ok(view.nodes.get("help-reading").innerHTML.includes(topic.intro));
  assert.equal(view.nodes.get("help-search-input").value, "");
  assert.equal(view.content.scrollTop, 1000);
});
