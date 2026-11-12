import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const html = readFileSync(join(here, "..", "index.html"), "utf8");
const css = readFileSync(join(here, "..", "assets", "styles.css"), "utf8");

test("primary sidebar navigation uses one compact label and one shared geometry", () => {
  assert.match(html, /data-nav="parameters" aria-label="一键配置新参数"/);
  assert.match(html, /<span>一键配置新参数<\/span>/);
  assert.doesNotMatch(html, /class="nav-item nav-item-long"/);
  assert.match(css, /\.nav-item \{[^}]*height: 48px;[^}]*min-height: 48px;[^}]*white-space: nowrap;/s);
  assert.doesNotMatch(css, /\.nav-item-long/);
  assert.match(css, /grid-template-columns: repeat\(6, minmax\(0, 1fr\)\)/);
  assert.match(css, /\.primary-nav \{ display: contents; \}/);
});
