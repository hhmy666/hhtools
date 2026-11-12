import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";

const source = fs.readFileSync(new URL("../assets/app.js", import.meta.url), "utf8");
test("settings screen headings provide a continuous screen-reader outline", () => {
  const html = fs.readFileSync(new URL("../index.html", import.meta.url), "utf8");
  const settings = html.match(/<section[^>]*data-view="settings"[^>]*>([\s\S]*?)<\/section>/)[1];
  const levels = [...settings.matchAll(/<h([1-6])\b/g)].map(match => Number(match[1]));
  assert.equal(levels[0], 1);
  assert.ok(levels.length > 1);
  for (let i = 1; i < levels.length; i++) {
    assert.ok(levels[i] <= levels[i - 1] + 1, `heading jumps from h${levels[i - 1]} to h${levels[i]}`);
  }
});
function harness({ path = "/repo/templates", response = { path: "/new/templates" }, fail = false } = {}) {
  const field = { value: path, readOnly: true, focus() { this.focused = true; }, setAttribute() {}, removeAttribute() {} };
  const button = { hidden: false, disabled: false, setAttribute() {}, removeAttribute() {} };
  const save = { disabled: false, setAttribute() {}, removeAttribute() {} };
  const error = { textContent: "", hidden: true };
  const modes = ["picker", "manual"].map(value => ({ value, checked: value === "picker", disabled: false }));
  const nodes = { "#setting-template-path": field, "#choose-template-directory": button, "#save-source-setting": save, "#source-setting-error": error };
  const calls = [];
  const start = source.indexOf("  function setSourceDirectoryMode(");
  const end = source.indexOf("  async function saveOutputSetting(", start);
  const state = { profile: { profile: { tile_repo_root: "/repo", template_dir: "/repo/templates" } } };
  const helpers = Function("$", "$$", "state", "chooseDirectory", "saveProfilePart", "toast", `${source.slice(start, end)}; return {setSourceDirectoryMode, chooseTemplateDirectory, saveSourceSetting};`)(
    selector => nodes[selector], () => modes, state,
    async purpose => { calls.push(["pick", purpose]); if (fail) throw Error("目录选择失败，请手动输入"); return response.cancelled ? null : response.path; },
    async (part, body) => { calls.push([part, body]); if (fail) throw Error("模板目录不存在"); }, () => {},
  );
  return { ...helpers, field, button, save, error, modes, calls };
}

test("source directory methods are exclusive and switching preserves the draft", () => {
  const h = harness();
  assert.equal(typeof h.setSourceDirectoryMode, "function");
  h.setSourceDirectoryMode("manual");
  assert.equal(h.field.readOnly, false);
  assert.equal(h.button.hidden, true);
  assert.deepEqual(h.modes.map(m => m.checked), [false, true]);
  h.field.value = "/new folder/关卡";
  h.setSourceDirectoryMode("picker");
  assert.equal(h.field.readOnly, true);
  assert.equal(h.button.hidden, false);
  assert.deepEqual(h.modes.map(m => m.checked), [true, false]);
  assert.equal(h.field.value, "/new folder/关卡");
  assert.deepEqual(h.calls, []);
});

test("picking a template folder only fills it, saving uses the same payload as typing", async () => {
  const picked = harness();
  await picked.chooseTemplateDirectory();
  assert.equal(picked.field.value, "/new/templates");
  assert.deepEqual(picked.calls, [["pick", "template"]]);
  await picked.saveSourceSetting();
  const typed = harness({ path: " /new/templates/ " });
  typed.setSourceDirectoryMode("manual");
  await typed.saveSourceSetting();
  const want = ["source", { tile_repo_root: "/new/templates", template_dir: "/new/templates" }];
  assert.deepEqual(picked.calls.at(-1), want);
  assert.deepEqual(typed.calls.at(-1), want);
});

test("saving an unchanged legacy template path preserves its existing repository boundary", async () => {
  const h = harness();
  await h.saveSourceSetting();
  assert.deepEqual(h.calls, [["source", { tile_repo_root: "/repo", template_dir: "/repo/templates" }]]);
});

test("cancelled or failed folder dialogs preserve the path and restore controls", async () => {
  for (const options of [{ response: { cancelled: true } }, { fail: true }]) {
    const h = harness(options);
    await h.chooseTemplateDirectory();
    assert.equal(h.field.value, "/repo/templates");
    assert.equal(h.button.disabled, false);
    assert.ok(h.modes.every(m => !m.disabled));
    if (options.fail) assert.match(h.error.textContent, /手动输入/);
  }
});

test("empty and relative source paths never save; errors retain input and focus the field", async () => {
  for (const path of ["", "Tile/Assets/Resources", "../templates"]) {
    const h = harness({ path });
    await h.saveSourceSetting();
    assert.deepEqual(h.calls, []);
    assert.equal(h.error.hidden, false);
    assert.equal(h.field.focused, true);
  }
  const h = harness({ path: "/missing/templates", fail: true });
  await h.saveSourceSetting();
  assert.equal(h.field.value, "/missing/templates");
  assert.equal(h.error.textContent, "模板目录不存在");
  assert.equal(h.save.disabled, false);
});

test("source saves ignore repeated submissions while one is pending", async () => {
  const h = harness();
  const saving = h.saveSourceSetting();
  await h.saveSourceSetting();
  await saving;
  assert.equal(h.calls.length, 1);
});
