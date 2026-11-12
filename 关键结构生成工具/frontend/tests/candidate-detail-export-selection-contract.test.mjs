import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";

const html = fs.readFileSync(new URL("../index.html", import.meta.url), "utf8");
const app = fs.readFileSync(new URL("../assets/app.js", import.meta.url), "utf8");
const css = fs.readFileSync(new URL("../assets/styles.css", import.meta.url), "utf8");

test("candidate detail exposes one aligned export-selection action and live total", () => {
  assert.match(html, /id="focus-selection-count"[^>]*role="status"[^>]*aria-live="polite"/);
  assert.match(html, /id="focus-select-candidate"[^>]*aria-pressed="false"[^>]*>[^<]*加入本次导出/);
  assert.match(css, /\.focus-heading-actions\s*\{[^}]*--focus-action-width:\s*150px[^}]*display:\s*grid[^}]*align-items:\s*center[^}]*\}/s);
  assert.match(css, /\.focus-detail-controls\s*\{[^}]*display:\s*flex[^}]*align-items:\s*center[^}]*\}/s);
  assert.match(css, /\.focus-select-candidate\s*\{[^}]*width:\s*var\(--focus-action-width\)[^}]*height:\s*44px[^}]*\}/s);
  assert.match(css, /\.focus-detail-controls > #close-focus\s*\{[^}]*width:\s*var\(--focus-action-width\)[^}]*height:\s*44px[^}]*\}/s);
});

test("detail selection reuses the canonical candidate choice state", () => {
  assert.match(app, /function syncFocusSelectionAction\(\)/);
  assert.match(app, /button\.dataset\.selectCandidate\s*=\s*id/);
  assert.match(app, /function selectedCandidateCountForTemplate\(templateKey\)/);
  assert.match(app, /item\.template_key === templateKey/);
  assert.match(app, /const currentTemplateTotal = item \? selectedCandidateCountForTemplate\(item\.template_key\) : 0/);
  assert.match(app, /本模板已加入 \$\{currentTemplateTotal\} 个 · 全部模板已加入 \$\{total\} 个/);
  assert.match(app, /renderCandidateWorkspace\(\); renderExportSelection\(\); syncFocusSelectionAction\(\);/);
  assert.match(app, /renderFocusSummary\(\); setupPlayer\(\); renderPlayer\(\); syncFocusCandidateNavigation\(\); syncFocusSelectionAction\(\);/);
});
