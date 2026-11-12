import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";

const html = fs.readFileSync(new URL("../index.html", import.meta.url), "utf8");
const app = fs.readFileSync(new URL("../assets/app.js", import.meta.url), "utf8");
const css = fs.readFileSync(new URL("../assets/styles.css", import.meta.url), "utf8");

test("completed export offers an equal-size open-directory action", () => {
  assert.match(html, /id="open-export-directory"[^>]*>[\s\S]*?打开输出目录<\/button>/);
  assert.match(css, /\.export-success-actions\s*\{[^}]*grid-template-columns:\s*repeat\(3, minmax\(0, 1fr\)\)[^}]*\}/s);
});

test("open-directory action targets the immutable completed export record", () => {
  assert.match(app, /async function openCompletedExportDirectory\(\)/);
  assert.match(app, /state\.recentExport\?\.exportId/);
  assert.match(app, /`\/exports\/\$\{encodeURIComponent\(exportId\)\}\/open-directory`/);
  assert.match(app, /method:\s*"POST"/);
  assert.match(app, /\$\("#open-export-directory"\)\.addEventListener\("click", openCompletedExportDirectory\)/);
});
