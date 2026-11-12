import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";

const css = fs.readFileSync(new URL("../assets/styles.css", import.meta.url), "utf8");

const allowedSizeTokens = new Set([
  "var(--font-size-display)",
  "var(--font-size-title)",
  "var(--font-size-section)",
  "var(--font-size-body)",
  "var(--font-size-meta)",
]);

test("all rendered typography uses the five canonical size tokens", () => {
  const declarations = [...css.matchAll(/font-size:\s*([^;}]+)/g)]
    .map((match) => match[1].replace(/\s*!important\s*$/, "").trim())
    .filter((value) => value !== "inherit");

  assert.ok(declarations.length > 0, "stylesheet must declare typography roles");
  assert.deepEqual(
    [...new Set(declarations)].sort(),
    [...allowedSizeTokens].sort(),
  );
});

test("UI elements inherit one canonical local font stack without local family overrides", () => {
  const familyDeclarations = [...css.matchAll(/font-family:\s*([^;}]+)/g)]
    .map((match) => match[1].trim())
    .filter((value) => value !== "inherit");
  assert.deepEqual(familyDeclarations, [
    '"Inter Local"',
    '"Source Han Sans SC Local"',
    '"Inter Local", "Source Han Sans SC Local", sans-serif',
  ]);

  const nonInheritedShorthands = [...css.matchAll(/(?:^|[;{])\s*font:\s*([^;}]+)/gm)]
    .map((match) => match[1].trim())
    .filter((value) => value !== "inherit");
  assert.deepEqual(nonInheritedShorthands, []);
});

test("browser default heading, small, and code typography cannot create extra combinations", () => {
  assert.match(css, /body\s*\{[^}]*font-size:\s*var\(--font-size-body\)/s);
  assert.match(css, /h1,\s*h2,\s*h3,\s*h4,\s*h5,\s*h6\s*\{[^}]*font-size:\s*inherit/s);
  assert.match(css, /small\s*\{[^}]*font-size:\s*var\(--font-size-meta\)/s);
  assert.match(css, /code,\s*pre,\s*kbd,\s*samp\s*\{[^}]*font-family:\s*inherit/s);
});

test("target calibration controls use the canonical body type and standard control height", () => {
  assert.match(
    css,
    /\.calibration-controls\s*>\s*div\s*>\s*input,\s*\.calibration-controls\s*>\s*div\s*>\s*select\s*\{[^}]*height:\s*40px[^}]*font-size:\s*var\(--font-size-body\)/s,
  );
});
