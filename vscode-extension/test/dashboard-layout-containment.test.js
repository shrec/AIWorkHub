"use strict";

// The owner reported that enabling model routes in Repository Settings
// "creates a mass of scrollbars in the window and then everything jumps".
//
// Reproduced in a browser against the real stylesheet and the real settings
// markup, with six providers and eighteen model routes:
//
//   * 37 overflow declarations, 14 of them `overflow: auto`, and
//     `scrollbar-gutter` used exactly once in a 61 KB stylesheet
//   * three scroll containers on one chain: .settings-tabs (overflow-x) inside
//     #settings-list (overflow-y) inside the <dialog> (UA overflow: auto)
//   * `.settings-model-route` computed `width: 100%`, not its declared
//     `calc(100% - 18px)`, because `.settings-row { width: 100% }` is declared
//     LATER at the same specificity and won -- so every route row rendered 18px
//     past the list that contained it
//   * the <dialog> grew a second vertical scroll range that tracked the list's
//     content: 0px with no routes, 224px with four, 1038px with eighteen
//   * scrolling to the last provider and toggling one off collapsed that range
//     to 767px, the browser clamped scrollTop, and the whole frame -- heading,
//     tab strip, footnote -- jumped 271px
//
// After the fix, measured the same way: dialog scroll range 0 at every route
// count, heading jump 0px, one vertical scrollbar instead of two, no
// horizontal overflow of the settings list, nested scroll chains down from 7
// to 4 and the deepest from 3 to 2.
//
// These tests hold the structural invariants that produced those numbers.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const root = path.resolve(__dirname, "..");
const cssSource = fs.readFileSync(path.join(root, "media", "app.css"), "utf8");
const appSource = fs.readFileSync(path.join(root, "media", "app.js"), "utf8");
const css = cssSource.replace(/\/\*[\s\S]*?\*\//g, "");

// Every rule block in the stylesheet, as { selector, body }.
function rules() {
  const out = [];
  const re = /(^|[\n}])\s*([^{}@][^{}]*?)\{([^{}]*)\}/g;
  let m;
  while ((m = re.exec(css)) !== null) {
    out.push({ selector: m[2].trim().replace(/\s+/g, " "), body: m[3] });
  }
  return out;
}

const SCROLLS = /(?:^|[\s;])overflow(?:-x|-y)?:\s*(auto|scroll)/;

function scrollRules() {
  return rules().filter((r) => SCROLLS.test(r.body));
}

test("every scroll container reserves its scrollbar gutter", () => {
  // The jump is a reflow: content width changes the instant a scrollbar
  // appears. Reserving the gutter means appearing costs nothing.
  const offenders = scrollRules()
    .filter((r) => !/scrollbar-gutter:\s*stable/.test(r.body))
    .map((r) => r.selector);
  assert.deepEqual(offenders, [],
    `these scroll containers reflow when their scrollbar appears: ${offenders.join(" | ")}`);
});

test("every scroll container contains its overflow instead of leaking it upward", () => {
  // Measured: without containment a nested scroll region still contributes to
  // the scrollable overflow of the <dialog> or the page above it, which is
  // literally where the second scrollbar came from. Either `contain: paint`
  // (the box clips and cannot contribute) or `overscroll-behavior: contain`
  // (a wheel that reaches the end does not chain into the parent) is required,
  // and the deep ones carry both.
  const offenders = scrollRules()
    .filter((r) => !/contain:\s*paint/.test(r.body) && !/overscroll-behavior(?:-x|-y)?:\s*contain/.test(r.body))
    .map((r) => r.selector);
  assert.deepEqual(offenders, [],
    `these scroll containers leak their overflow to an ancestor: ${offenders.join(" | ")}`);
});

test("the settings list is the settings dialog's only scroll region", () => {
  const list = rules().find((r) => r.selector === ".settings-list");
  assert.ok(list, ".settings-list must exist");
  assert.match(list.body, /overflow-y:\s*auto/);
  assert.match(list.body, /scrollbar-gutter:\s*stable/);
  assert.match(list.body, /contain:\s*paint/,
    "without paint containment the list's content became the dialog's scroll range");

  // The tab strip inside it must not be a third scroll container on the chain.
  const tabs = rules().find((r) => r.selector === ".settings-tabs");
  assert.ok(tabs, ".settings-tabs must exist");
  assert.doesNotMatch(tabs.body, /overflow/,
    "the tab strip must wrap, not scroll inside a scroll region inside a dialog");
  assert.match(tabs.body, /flex-wrap:\s*wrap/);
});

test("chip and tab strips wrap rather than minting a scrollbar of their own", () => {
  for (const selector of [".settings-tabs", ".tab-list", ".status-filters"]) {
    const rule = rules().find((r) => r.selector === selector);
    assert.ok(rule, `${selector} must exist`);
    assert.doesNotMatch(rule.body, /overflow-x:\s*(auto|scroll)/,
      `${selector} must wrap instead of hiding half its controls behind its own scrollbar`);
    assert.match(rule.body, /flex-wrap:\s*wrap/, `${selector} must wrap`);
  }
});

test("the plan DAG keeps the one horizontal scroller that a long axis earns", () => {
  const rule = rules().find((r) => r.selector === ".plan-dag-grid");
  assert.ok(rule);
  assert.match(rule.body, /overflow-x:\s*auto/, "a dependency DAG genuinely has a long axis");
  assert.match(rule.body, /scrollbar-gutter:\s*stable/);
  assert.match(rule.body, /overscroll-behavior-x:\s*contain/);
});

test("an indented settings row cannot desynchronise from its own width", () => {
  // The exact defect: `width: calc(100% - 18px)` lost the cascade to
  // `.settings-row { width: 100% }` declared later at equal specificity, so the
  // margin survived and the width did not. Indenting with padding removes the
  // whole class of bug -- there is no width to disagree with.
  const route = rules().find((r) => r.selector === ".settings-model-route");
  assert.ok(route, ".settings-model-route must exist");
  assert.doesNotMatch(route.body, /(?:^|[\s;])width:/,
    "must not restate a width that a later same-specificity rule can override");
  assert.doesNotMatch(route.body, /margin-left:/,
    "must not indent with a margin that the width no longer accounts for");
  assert.match(route.body, /padding-left:/, "indent with padding, which cannot disagree with the width");

  // The narrow override must follow the same rule.
  const narrow = css.match(/@media \(max-width: 620px\) \{([\s\S]*?)\n\}/);
  assert.ok(narrow);
  const narrowRoute = narrow[1].match(/\.settings-model-route \{([^}]*)\}/);
  assert.ok(narrowRoute, "the narrow override must still exist");
  assert.doesNotMatch(narrowRoute[1], /width:|margin-left:/);
});

test("responsiveness comes from intrinsic sizing, not from more breakpoints", () => {
  // The stylesheet had eight hard breakpoints (820/720/620/900/560) and no
  // clamp(), container-type or @container anywhere. Four of those rules are
  // retired: the grids that needed them now size on how much room a component
  // needs.
  for (const selector of [".kpi-chart-grid", ".kpi-grid", ".header-insights", ".summary-strip", ".filter-bar"]) {
    const rule = rules().find((r) => r.selector === selector);
    assert.ok(rule, `${selector} must exist`);
    assert.match(rule.body, /grid-template-columns:\s*repeat\(auto-fit,\s*minmax\(min\(100%,/,
      `${selector} must size intrinsically, with min() guarding a narrow container`);
  }
  assert.match(css, /container-type:\s*inline-size/, "container queries must be in use");
  assert.match(css, /@container /, "at least one container query must exist");
  assert.match(css, /clamp\(/, "clamp() must be in use for fluid sizing");

  // The retired breakpoints must not quietly come back.
  const mediaBlocks = [...css.matchAll(/@media \(max-width: (\d+)px\) \{([\s\S]*?)\n\}/g)];
  for (const [, width, body] of mediaBlocks) {
    for (const selector of [".header-insights", ".summary-strip", ".kpi-chart-grid"]) {
      assert.ok(!new RegExp(`\\${selector}\\s*\\{[^}]*grid-template-columns`).test(body),
        `${selector} must not be re-columned by a ${width}px breakpoint again`);
    }
  }
});

test("keyboard focus stays visible everywhere", () => {
  // Two rules used to remove the ring outright: .plan-node:focus-visible ended
  // in `outline: none`, and .dialog-search declared `outline: none` on the
  // element itself. A keyboard operator had a faint border tint and nothing else.
  for (const rule of rules()) {
    if (!/:focus(-visible)?/.test(rule.selector)) continue;
    assert.doesNotMatch(rule.body, /outline:\s*none/,
      `${rule.selector} must not remove the focus ring`);
  }
  const search = rules().find((r) => r.selector === ".dialog-search");
  assert.ok(search);
  assert.doesNotMatch(search.body, /outline:\s*none/);

  const base = css.match(/button:focus-visible,\s*\ninput:focus-visible,[\s\S]*?\{([^}]*)\}/);
  assert.ok(base, "a shared focus-visible rule must exist");
  assert.match(base[1], /outline:\s*2px solid var\(--vscode-focusBorder\)/,
    "a hairline ring is easy to lose against a row border at this density");
  assert.match(base[1], /outline-offset:/);

  const planNode = css.match(/\.plan-node:focus-visible \{([^}]*)\}/);
  assert.ok(planNode, ".plan-node must keep its own focus ring");
  assert.match(planNode[1], /outline:\s*2px solid var\(--vscode-focusBorder\)/);
});

test("reduced motion covers animation as well as transition", () => {
  const block = css.match(/@media \(prefers-reduced-motion: reduce\) \{([\s\S]*?)\n\}/);
  assert.ok(block, "the reduced-motion block must survive");
  assert.match(block[1], /transition-duration:\s*0\.01ms\s*!important/);
  assert.match(block[1], /animation-duration:\s*0\.01ms\s*!important/);
  assert.match(block[1], /animation-iteration-count:\s*1\s*!important/);
  assert.match(block[1], /scroll-behavior:\s*auto\s*!important/);
});

test("a value that was never measured never renders as a number", () => {
  // The payload distinguishes unknown / no_sample / not_available from a
  // measured zero, and numberValue() coerces every one of them to 0. An
  // operator reading "we never sampled the callback backlog" as "the backlog
  // is 0" has been told the opposite of the truth.
  assert.match(appSource, /const NO_MEASUREMENT_STATES = new Set\(\[/);
  for (const sentinel of ["unknown", "no_sample", "not_available"]) {
    assert.match(appSource, new RegExp(`"${sentinel}"`), `${sentinel} must be recognised as "not measured"`);
  }
  assert.match(appSource, /function isMeasured\(value\)/);
  assert.match(appSource, /const NO_MEASUREMENT_LABEL = "not measured"/,
    "the marker must be a word -- in a column of figures a bare dash still scans as a value");

  // Headline metrics can legitimately be absent, so they go through the
  // measured formatter rather than through the coercing one.
  assert.doesNotMatch(appSource, /formatCount\(headline\./,
    "a headline metric must never be rendered by a formatter that turns absent into 0");
  assert.match(appSource, /measuredCount\(headline\./);

  // kpiPercent must reject the sentinels, not just non-numbers.
  assert.match(appSource, /function kpiPercent\(value\) \{\s*return isMeasured\(value\)/);

  // And the inverse error: a measured 0 must render as a duration, not as
  // "unknown". `if (!value) return "—"` made both cells the same.
  assert.match(appSource, /if \(!isMeasured\(milliseconds\)\) return NO_MEASUREMENT_LABEL;/);
  assert.doesNotMatch(appSource, /const value = numberValue\(milliseconds\);\s*\n\s*if \(!value\) return "—";/);

  // An unmeasured cell is styled as prose, so it cannot be scanned as a figure.
  assert.match(appSource, /is-unmeasured/);
  assert.match(css, /\.kpi-card\.is-unmeasured \.kpi-card-value \{[^}]*color:\s*var\(--muted\)/);
});

test("the panel gets its hierarchy from weight and rule, not from more boxes", () => {
  // Three deliberate weights, not the seven the stylesheet had drifted to.
  const weights = new Set([...css.matchAll(/font-weight:\s*(\d+)/g)].map((m) => m[1]));
  assert.deepEqual([...weights].sort(), ["400", "600", "700"],
    `expected three deliberate weights, found: ${[...weights].sort().join(", ")}`);

  // ALL-CAPS eyebrow labels are gone; uppercase survives only where it is an
  // identity badge rather than a label above a heading.
  const uppercase = rules().filter((r) => /text-transform:\s*uppercase/.test(r.body)).map((r) => r.selector);
  assert.deepEqual(uppercase.sort(), [".readonly-badge", ".status-badge, .signal-badge"],
    `uppercase must be reserved for identity badges; found: ${uppercase.join(" | ")}`);
  assert.doesNotMatch(css, /letter-spacing:\s*\.?0?\.0[48]em/,
    "the tracking existed only to make the caps readable");

  // The metric field is ruled, not twenty-two identical bordered cards. Each
  // cell paints its own hairline into the 1px gap; the grid must NOT paint a
  // field colour behind everything, because auto-fit leaves trailing tracks
  // empty and the field then shows through as a solid block -- a black
  // rectangle under the last row on High Contrast Light.
  const grid = rules().find((r) => r.selector === ".kpi-grid");
  assert.match(grid.body, /gap:\s*1px/, "the 1px gap is the rule");
  assert.doesNotMatch(grid.body, /background:\s*var\(--line\)/,
    "the grid must not paint the border colour behind its empty tracks");
  const card = rules().find((r) => r.selector === ".kpi-card");
  assert.doesNotMatch(card.body, /border-radius/, "a ruled field has no rounded cells");
  assert.match(card.body, /box-shadow:\s*1px 0 0 var\(--line\), 0 1px 0 var\(--line\)/,
    "the rule is a zero-blur hairline painted into the gap");
  assert.match(card.body, /border-left:\s*2px solid transparent/,
    "the status gutter must default to nothing, so a colour there means something");

  // A blurred drop shadow is legitimate on something that genuinely floats
  // above the plane -- a modal, a popover, a toast. It is the generic default
  // on anything that does not. These three are flush regions of one instrument
  // surface, and every one of them used to carry `box-shadow: var(--shadow)`.
  const FLOATS_ABOVE_THE_PLANE = new Set([
    ".diagnostic-dialog",
    ".identity-info-panel",
    ".toast",
  ]);
  const shadowed = rules()
    .filter((r) => /box-shadow:/.test(r.body) && !/box-shadow:\s*(none|inset)/.test(r.body))
    .map((r) => r.selector)
    .filter((selector) => !FLOATS_ABOVE_THE_PLANE.has(selector));
  // Whatever is left must be a zero-blur hairline rule, not a soft shadow.
  for (const selector of shadowed) {
    const body = rules().find((r) => r.selector === selector).body;
    const value = /box-shadow:\s*([^;]+)/.exec(body)[1];
    assert.ok(/^(?:inset )?\s*-?\d+px 0 0 |^1px 0 0 |^0 -?\d+px 0 /.test(value.trim()),
      `${selector} may only paint a zero-blur rule, got: ${value.trim()}`);
  }
  const panels = rules().find((r) => r.selector === ".task-workspace, .detail-panel, .operations-panel");
  assert.ok(panels);
  assert.doesNotMatch(panels.body, /box-shadow/,
    "flush panels of one instrument surface carry a hairline border, not a drop shadow");

  // Figures align and do not shift width as they update.
  assert.match(css, /\.kpi-card-value \{[^}]*font-variant-numeric:\s*tabular-nums/);
  assert.match(css, /\.kpi-card-label \{[^}]*min-height:/,
    "labels reserve their lines so every figure in a row shares a baseline");
});
