import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";

const html = fs.readFileSync(new URL("../index.html", import.meta.url), "utf8");
const css = fs.readFileSync(new URL("../assets/styles.css", import.meta.url), "utf8");
const app = fs.readFileSync(new URL("../assets/app.js", import.meta.url), "utf8");

test("configuration keeps the source formation compact and gives controls enough width", () => {
  assert.match(css, /\.config-layout\s*\{[^}]*grid-template-columns:\s*minmax\(580px,\s*1fr\)\s+430px/s);
  assert.match(css, /\.source-board\s*\{[^}]*height:\s*340px[^}]*min-height:\s*340px/s);
  assert.match(css, /\.source-board svg\s*\{[^}]*height:\s*100%[^}]*min-height:\s*0/s);
});

test("compound controls divide selector and value evenly", () => {
  assert.match(
    css,
    /\.compound-input\s*\{[^}]*grid-template-columns:\s*minmax\(0,\s*1fr\)\s+minmax\(0,\s*1fr\)\s+auto/s,
  );
});

test("layer height and span limits share one equal two-column row", () => {
  assert.match(
    html,
    /class="structure-layer-limits-row"[\s\S]*class="form-row structure-max-layer-row"[\s\S]*class="form-row structure-span-row"/,
  );
  assert.match(css, /\.structure-layer-limits-row\s*\{[^}]*grid-template-columns:\s*repeat\(2,\s*minmax\(0,\s*1fr\)\)/s);
});

test("generation workspace matches candidate width and finished phases show one check", () => {
  assert.match(css, /\.generation-state\s*\{[^}]*grid-template-columns:\s*minmax\(0,\s*1fr\)[^}]*justify-content:\s*stretch/s);
  assert.match(css, /\.generation-state\s*\{[^}]*width:\s*100%/s);
  assert.match(css, /\.task-phase-list li > span\s*\{[^}]*width:\s*36px[^}]*height:\s*36px/s);
  assert.match(css, /\.task-phase-list li\.is-done > span\s*\{[^}]*overflow:\s*hidden[^}]*text-indent:\s*-9999px/s);
  assert.match(css, /\.task-phase-list li\.is-done > span::before\s*\{[^}]*position:\s*absolute[^}]*inset:\s*0[^}]*text-indent:\s*0/s);
  assert.match(css, /\.task-phase-list::before\s*\{[^}]*top:\s*17px/s);
});

test("generation continuation is a hierarchy section instead of a nested card", () => {
  assert.match(css, /#generation-budget-panel \.search-budget-card\s*\{[^}]*border:\s*0[^}]*border-radius:\s*0[^}]*background:\s*transparent[^}]*box-shadow:\s*none/s);
});

test("board tiles keep base labels readable without bolding the formation", () => {
  assert.match(app, /A:\s*"#e0e4e1",\s*B:\s*"#b8c1bd"/);
  assert.match(app, /class="tile-layer-label"[^>]*y="\$\{cy - faceHeight \/ 2 \+ 13\}"/);
  assert.match(css, /\.board-canvas \.tile-label\s*\{[^}]*font-weight:\s*500/s);
  assert.match(css, /\.board-canvas \.tile-layer-label\s*\{[^}]*font-weight:\s*500/s);
  assert.match(css, /\.board-canvas \.tile\.is-base \.tile-label\s*\{[^}]*font-size:\s*var\(--font-size-section\)/s);
});

test("only the source formation distinguishes initially visible and covered tiles", () => {
  assert.match(
    app,
    /renderBoard\(boardRoot,\s*item\?\.board,\s*\{[^}]*highlightInitialVisibility:\s*true[^}]*\}\)/s,
  );
  assert.equal((app.match(/highlightInitialVisibility:\s*true/g) || []).length, 1);
  assert.match(app, /const isInitiallyVisible = tile\.is_initially_visible/);
  assert.match(app, /options\.highlightInitialVisibility[\s\S]*is-visible-layer[\s\S]*is-covered-layer/);
  assert.match(css, /\.source-board \.tile\.is-visible-layer \.tile-face\s*\{[^}]*fill:/s);
  assert.match(css, /\.source-board \.tile\.is-covered-layer \.tile-face\s*\{[^}]*fill:/s);
  assert.doesNotMatch(css, /\.candidate-card[^,{]*\.is-(?:visible|covered)-layer/);
  assert.doesNotMatch(css, /#focus-board[^,{]*\.is-(?:visible|covered)-layer/);
});

test("primary puzzle step and pre-gate recycle controls share one equal row", () => {
  assert.match(
    html,
    /class="form-row two-columns generation-gate-row"[\s\S]*id="unit-1-step"[\s\S]*id="required-pre-gate-recycles"[\s\S]*<\/div>/,
  );
  assert.match(
    html,
    /class="form-row two-columns generation-gate-conditional-row"[\s\S]*id="unit-2-step-wrap"[\s\S]*id="first-recycle-lead-wrap"[\s\S]*<\/div>/,
  );
});

test("structure configuration and candidate heading controls use canonical type roles", () => {
  assert.match(css, /\.structure-count-mode-options b\s*\{[^}]*font-size:\s*var\(--font-size-body\)/s);
  assert.match(
    css,
    /\.structure-type-row > label,\s*\.structure-capacity-row > label,\s*\.structure-layer-limits-row > \.form-row > label\s*\{[^}]*font-size:\s*var\(--font-size-meta\)/s,
  );
  assert.match(
    css,
    /\.candidate-workspace \.heading-actions > button\s*\{[^}]*font-size:\s*var\(--font-size-body\)/s,
  );
  assert.match(
    css,
    /\.candidate-workspace \.heading-actions > button :is\(span, b\)\s*\{[^}]*font-size:\s*inherit/s,
  );
});

test("whole-board experience settings use dividers instead of a nested card", () => {
  assert.match(
    css,
    /\.experience-controls\s*\{[^}]*padding-inline:\s*0[^}]*border:\s*0[^}]*border-block:\s*1px solid var\(--line\)[^}]*border-radius:\s*0[^}]*background:\s*transparent/s,
  );
});

test("candidate detail gives the summary more width and scans metrics in pairs", () => {
  assert.match(css, /\.focus-layout\s*\{[^}]*grid-template-columns:\s*minmax\(0,\s*1\.75fr\)\s+minmax\(400px,\s*1fr\)/s);
  assert.match(
    css,
    /#focus-summary \.summary-list\s*\{[^}]*grid-template-columns:\s*minmax\(0,\s*1fr\)\s+auto\s+minmax\(0,\s*1fr\)\s+auto/s,
  );
  assert.match(css, /#focus-summary \.candidate-experience-summary\s*\{[^}]*grid-template-columns:\s*repeat\(3,\s*minmax\(0,\s*1fr\)\)/s);
  assert.match(css, /\.focus-summary-heading\s*\{[^}]*grid-template-columns:\s*minmax\(0,\s*1fr\)\s+170px/s);
  assert.match(css, /\.focus-experience-signals\s*\{[^}]*grid-row:\s*2\s*\/\s*4[^}]*margin:\s*0/s);
  assert.match(css, /#focus-summary\s+\.focus-experience-signals\s*\{[^}]*grid-template-columns:\s*repeat\(2,\s*minmax\(0,\s*1fr\)\)[^}]*width:\s*100%/s);
});

test("candidate detail disclosures share one readable text role", () => {
  assert.match(
    css,
    /#focus-summary \.experience-breakdown summary,\s*#focus-summary \.difficulty-breakdown summary,\s*#focus-summary \.quality-breakdown summary\s*\{[^}]*min-height:\s*37px[^}]*padding:\s*7px 9px[^}]*font-size:\s*var\(--font-size-body\)/s,
  );
});
