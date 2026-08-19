"use strict";

// NF-2026-00311 - the dashboard font sizing was hard-coded far below the
// user's editor font (89 of 112 rules were 11px or smaller and ignored
// --vscode-font-size). These tests assert the INVARIANT that fixed the bug -
// every font size derives from --vscode-font-size through a declared type
// scale and nothing uses a sub-floor pixel literal - rather than pinning
// today's specific numbers. Reintroducing a hard-coded small literal fails.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const cssPath = path.join(__dirname, "..", "media", "app.css");
const cssSource = fs.readFileSync(cssPath, "utf8");

// The smallest permitted pixel literal. Anything below this in a font-size
// value is the exact defect this card removed.
const FLOOR_PX = 11;

function stripComments(css) {
  return css.replace(/\/\*[\s\S]*?\*\//g, "");
}

function firstRootBlock(css) {
  const start = css.indexOf(":root");
  assert.notStrictEqual(start, -1, "app.css must declare a :root block");
  const open = css.indexOf("{", start);
  const close = css.indexOf("}", open);
  return css.slice(open + 1, close);
}

function pxLiteralsIn(value) {
  return [...value.matchAll(/(-?\d*\.?\d+)px/g)].map((m) => Number(m[1]));
}

const css = stripComments(cssSource);
const rootBlock = firstRootBlock(css);

// Every font-size declaration in the file (standalone or inline).
const fontSizeDecls = [...css.matchAll(/font-size\s*:\s*([^;}]+)/g)].map(
  (m) => m[1].trim(),
);

test("app.css actually declares font sizes (sanity, not a value pin)", () => {
  assert.ok(
    fontSizeDecls.length > 20,
    `expected many font-size declarations, found ${fontSizeDecls.length}`,
  );
});

test("the type scale is rooted in --vscode-font-size with a fallback", () => {
  // The single place the editor's font size is consumed must carry a fallback.
  const base = rootBlock.match(/--fs-base\s*:\s*([^;]+);/);
  assert.ok(base, ":root must define --fs-base");
  assert.match(
    base[1],
    /var\(\s*--vscode-font-size\s*,\s*(\d+(?:\.\d+)?)px\s*\)/,
    "--fs-base must read var(--vscode-font-size, <fallback>px)",
  );
  const fallback = Number(
    base[1].match(/var\(\s*--vscode-font-size\s*,\s*(\d+(?:\.\d+)?)px/)[1],
  );
  assert.ok(
    fallback >= FLOOR_PX,
    `--fs-base fallback ${fallback}px must not be below the ${FLOOR_PX}px floor`,
  );
});

test("every declared --fs-* scale token derives from --vscode-font-size", () => {
  const tokens = [...rootBlock.matchAll(/(--fs-[a-z0-9-]+)\s*:\s*([^;]+);/g)];
  assert.ok(tokens.length >= 3, "expected a multi-step --fs-* type scale");
  for (const [, name, value] of tokens) {
    // A token either reads the editor variable directly or is expressed
    // relative to --fs-base (which itself reads --vscode-font-size).
    assert.match(
      value,
      /var\(\s*--(?:vscode-font-size|fs-base)\b/,
      `${name} must derive from --vscode-font-size (via --fs-base or directly)`,
    );
    // No token may bake in a sub-floor pixel literal.
    for (const px of pxLiteralsIn(value)) {
      assert.ok(
        px >= FLOOR_PX,
        `${name} carries a ${px}px literal below the ${FLOOR_PX}px floor`,
      );
    }
  }
});

test("no font-size uses a pixel literal below the floor", () => {
  const scaleTokens = new Set(
    [...rootBlock.matchAll(/(--fs-[a-z0-9-]+)\s*:/g)].map((m) => m[1]),
  );
  for (const value of fontSizeDecls) {
    // Regression guard: a reintroduced hard-coded small literal fails here.
    for (const px of pxLiteralsIn(value)) {
      assert.ok(
        px >= FLOOR_PX,
        `font-size: ${value} uses a ${px}px literal below the ${FLOOR_PX}px floor`,
      );
    }
    // Sizing must derive from the type scale / editor variable, never a bare
    // pixel value.
    assert.match(
      value,
      /var\(\s*--(?:vscode-font-size|fs-[a-z0-9-]+)\b/,
      `font-size: ${value} must derive from the --vscode-font-size type scale`,
    );
    const referenced = [...value.matchAll(/var\(\s*(--[a-z0-9-]+)/g)].map(
      (m) => m[1],
    );
    for (const ref of referenced) {
      assert.ok(
        ref === "--vscode-font-size" || scaleTokens.has(ref),
        `font-size: ${value} references ${ref}, which is not a declared scale token`,
      );
    }
  }
});

test("the smallest step reports a readable rendered size at 13px and 16px", () => {
  // Derive the smallest scale multiplier straight from the CSS so this locks
  // in the real outcome the owner asked to see. A token with no explicit
  // multiplier (e.g. --fs-sm: var(--fs-base)) counts as 1.0x.
  const multipliers = [...rootBlock.matchAll(/--fs-[a-z0-9-]+\s*:\s*([^;]+);/g)]
    .map((m) => {
      const mult = m[1].match(/\*\s*(\d*\.?\d+)/);
      return mult ? Number(mult[1]) : 1.0; // base / var-only tokens are 1.0x
    });
  const smallestStep = Math.min(...multipliers);
  assert.ok(
    Number.isFinite(smallestStep) && smallestStep > 0,
    "could not derive a smallest scale multiplier from the --fs-* tokens",
  );
  // If the smallest step ever drops the default (13px) below the floor, this
  // fails - shrinking text back down is how the panel got unreadable.
  for (const editorPx of [13, 16]) {
    const smallest = editorPx * smallestStep;
    assert.ok(
      smallest >= FLOOR_PX,
      `smallest rendered size ${smallest}px at ${editorPx}px editor font is below ${FLOOR_PX}px`,
    );
  }
});
