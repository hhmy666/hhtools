"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const modulePath = path.resolve(__dirname, "../../frontend/assets/batch-import.js");
const Bulk = fs.existsSync(modulePath) ? require(modulePath) : {};
const items = (n) => Array.from({length: n}, (_, i) => `template${i}`);
const limits = {file_count: 20, file_bytes: 32 * 1024 * 1024, request_bytes: 128 * 1024 * 1024};
function harness(failAt = 0, total = 105) {
  const calls = []; let count = 0;
  return {calls, async request(url, options) {
    calls.push([url, options]); count += 1;
    if (count === failAt) throw new Error("connection lost");
    if (url.endsWith("/merge")) return {import_id: "import-" + "f".repeat(32), files: Array.from({length: total}, () => ({}))};
    const ids = options.json.template_ids;
    return {import_id: "import-" + String(count).padStart(32, "0"),
      source_binding: "a".repeat(64), files: ids.map((id) => ({original_name: id + "_m.bytes"}))};
  }};
}
test("105 IDs are read in bounded batches, progress advances only on completed responses, then one merge", async () => {
  assert.equal(typeof Bulk.createSession, "function");
  const session = Bulk.createSession({items: items(105), mode: "ids", source: "tile", limits});
  const h = harness(), progress = [];
  await Bulk.run(session, {request: h.request, onProgress: (p) => progress.push(p)});
  assert.deepEqual(h.calls.slice(0, -1).map(([, o]) => o.json.template_ids.length), [20,20,20,20,20,5]);
  assert.equal(h.calls.at(-1)[0], "/api/imports/merge");
  assert.equal(h.calls.at(-1)[1].json.import_ids.length, 6);
  assert.deepEqual([...new Set(progress.map((p) => p.completed))], [0,20,40,60,80,100,105]);
  assert.equal(h.calls[0][1].json.expected_binding, undefined);
  assert.equal(h.calls[1][1].json.expected_binding, "a".repeat(64));
});
test("retry resumes unfinished request without reading the successful first batch again", async () => {
  assert.equal(typeof Bulk.createSession, "function");
  const session = Bulk.createSession({items: items(45), mode: "ids", source: "template", limits});
  const first = harness(2);
  await assert.rejects(Bulk.run(session, {request: first.request}), /connection lost/);
  assert.equal(session.completed, 20);
  const second = harness(0,45);
  await Bulk.run(session, {request: second.request});
  assert.deepEqual(second.calls[0][1].json.template_ids, items(45).slice(20,40));
  assert.equal(second.calls.at(-1)[1].json.import_ids.length, 3);
});
test("files are split by request bytes as well as count without reading their contents in JS", async () => {
  assert.equal(typeof Bulk.createSession, "function");
  const files = items(21).map((id) => ({name: id + "_m.bytes", size: 32 * 1024 * 1024}));
  const session = Bulk.createSession({items: files, mode: "files", limits});
  assert.deepEqual(session.batches.map((b) => b.length), [3,3,3,3,3,3,3]);
  let n = 0; const sent = [];
  await Bulk.run(session, {formFactory: () => ({append: (_key, file) => sent.push(file.name)}),
    request: async (url) => url.endsWith("/merge") ? {import_id: "import-" + "f".repeat(32), files: Array.from({length: 21}, () => ({}))} : {
      import_id: "import-" + String(++n).padStart(32, "0"), files: [{},{},{}]}});
  assert.equal(sent.length, 21);
});
test("one internal batch does not need merge; bad row count is not silently accepted", async () => {
  assert.equal(typeof Bulk.createSession, "function");
  const s = Bulk.createSession({items: items(1), mode: "ids", source: "tile", limits});
  const h = harness();
  await Bulk.run(s, {request: h.request});
  assert.equal(h.calls.length, 1);
  const bad = Bulk.createSession({items: items(3), mode: "ids", source: "tile", limits});
  await assert.rejects(Bulk.run(bad, {request: async () => ({import_id: "import-" + "1".repeat(32), files: [{}]})}), /数量/);
  assert.equal(bad.completed, 0);
});
test("merge failure can retry merge alone; an aborted response cannot advance or publish results", async () => {
  assert.equal(typeof Bulk.createSession, "function");
  const s = Bulk.createSession({items: items(21), mode: "ids", source: "tile", limits});
  await assert.rejects(Bulk.run(s, {request: harness(3).request}));
  const h = harness(0,21); await Bulk.run(s, {request: h.request});
  assert.equal(h.calls.length, 1); assert.equal(h.calls[0][0], "/api/imports/merge");
  const controller = new AbortController();
  const cancelled = Bulk.createSession({items: items(1), mode: "ids", source: "tile", limits});
  await assert.rejects(Bulk.run(cancelled, {signal: controller.signal, request: async () => {
    controller.abort(); return {import_id: "import-" + "1".repeat(32), files: [{}]};
  }}), {name: "AbortError"});
  assert.equal(cancelled.completed, 0);
});
test("ID read-size overflow is automatically split again rather than asking user to divide batches", async () => {
  assert.equal(typeof Bulk.createSession, "function");
  const s = Bulk.createSession({items: items(20), mode: "ids", source: "tile", limits});
  const h = harness(0,20); let once = true;
  await Bulk.run(s, {request: async (url, options) => {
    if (once) { once = false; throw Object.assign(new Error("too large"), {code: "import_too_large"}); }
    return h.request(url, options);
  }});
  assert.deepEqual(h.calls.slice(0,-1).map(([,o]) => o.json.template_ids.length), [10,10]);
});
test("an oversized file is retained as an individual rejected row without blocking valid files", async () => {
  const s = Bulk.createSession({items: [
    {name: 'ok_m.bytes', size: 1024}, {name: 'large_m.bytes', size: 33 * 1024 * 1024},
    {name: 'last_m.bytes', size: 1024}], mode: 'files', limits});
  const calls = [];
  await Bulk.run(s, {formFactory: () => ({append() {}}), request: async (url, options) => {
    calls.push([url, options]);
    return {import_id: 'import-' + String(calls.length).padStart(32,'0'),
      files: Array.from({length: url.endsWith('/merge') ? 3 : 1}, () => ({}))};
  }});
  assert.deepEqual(calls.map(([url]) => url), ['/api/import', '/api/import-rejections', '/api/import', '/api/imports/merge']);
  assert.deepEqual(calls[1][1].json.files, [{name:'large_m.bytes', size:33 * 1024 * 1024}]);
  assert.equal(s.completed, 3);
});
