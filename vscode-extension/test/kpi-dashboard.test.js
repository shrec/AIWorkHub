"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const extensionSource = fs.readFileSync(path.join(__dirname, "..", "extension.js"), "utf8");
const appSource = fs.readFileSync(path.join(__dirname, "..", "media", "app.js"), "utf8");
const cssSource = fs.readFileSync(path.join(__dirname, "..", "media", "app.css"), "utf8");

const DAILY_TERMINAL_STATE_ORDER = [
  "review_ready",
  "validation_failed",
  "worker_failed",
  "launch_failed",
  "timed_out",
  "cancelled",
  "scope_rejected",
  "blocked",
  "exited",
];

function makeFakeElement(tag) {
  return {
    tag,
    className: "",
    textContent: "",
    title: "",
    style: {},
    attrs: {},
    children: [],
    setAttribute(name, value) {
      this.attrs[name] = String(value);
    },
    getAttribute(name) {
      return this.attrs[name];
    },
    appendChild(child) {
      this.children.push(child);
      return child;
    },
    append(...nodes) {
      this.children.push(...nodes);
    },
  };
}

// Extracts the exact shipped Daily Worker Outcomes rendering block from
// app.js (verbatim, not reimplemented) so the assertions below exercise the
// real production code path rather than a parallel test-only copy of it.
function extractDailyOutcomesSnippet(source) {
  const startMarker = "const DAILY_STATE_ORDER = asArray(kpis.daily_state_order)";
  const endMarker = "chartGrid.appendChild(dailyPanel);";
  const start = source.indexOf(startMarker);
  const end = source.indexOf(endMarker, start);
  assert.notEqual(start, -1, "daily outcomes snippet start marker not found in app.js");
  assert.notEqual(end, -1, "daily outcomes snippet end marker not found in app.js");
  return source.slice(start, end + endMarker.length);
}

function renderDailyPanel(kpis) {
  const snippet = extractDailyOutcomesSnippet(appSource);
  const harness = `
    "use strict";
    function asArray(value) { return Array.isArray(value) ? value : []; }
    function numberValue(value) {
      const parsed = Number(value);
      return Number.isFinite(parsed) ? parsed : 0;
    }
    function createElement(tag, className, text) {
      const element = document.createElement(tag);
      if (className) { element.className = className; }
      if (text !== undefined && text !== null) { element.textContent = String(text); }
      return element;
    }
    const chartGrid = { dailyPanel: null, appendChild(node) { this.dailyPanel = node; } };
    ${snippet}
    result.value = chartGrid.dailyPanel;
  `;
  const result = { value: null };
  const context = vm.createContext({
    document: {
      createElement: (tag) => makeFakeElement(tag),
      createTextNode: (text) => ({ nodeType: 3, textContent: String(text) }),
    },
    kpis,
    result,
  });
  vm.runInContext(harness, context);
  return result.value;
}

function dayStates(overrides) {
  const base = Object.fromEntries(DAILY_TERMINAL_STATE_ORDER.map((state) => [state, 0]));
  return { ...base, ...overrides };
}

function statesList(counts) {
  return Object.entries(counts).map(([state, count]) => ({ state, count }));
}

// Mirrors the backend contract: build_kpi_snapshot() publishes
// "daily_state_order" alongside "daily" so the Webview always renders the
// order the backend actually supplied, never a locally hardcoded copy.
function buildKpis(daily, order = DAILY_TERMINAL_STATE_ORDER) {
  return { daily, daily_state_order: order };
}

test("dashboard exposes the complete operations surface in a dedicated popup", () => {
  assert.match(extensionSource, /id="open-operations"[^>]+title="Open repository operations"/);
  assert.match(extensionSource, /class="diagnostic-dialog operations-dialog" id="operations-dialog"/);
  assert.match(extensionSource, /id="kpi-dashboard"/);
  assert.match(extensionSource, /id="tab-kpis"[^>]+data-tab="kpis"/);
  assert.match(extensionSource, /id="panel-kpis"[^>]+aria-labelledby="tab-kpis"/);
  assert.match(extensionSource, /aria-selected="false"[^>]+id="tab-topics"/);
  assert.match(appSource, /elements\.operationsDialog\.showModal\(\)/);
  assert.match(cssSource, /\.diagnostic-dialog\.operations-dialog/);
  assert.match(cssSource, /\.operations-dialog \.tab-panel:not\(\[hidden\]\)/);
});

test("KPI renderer separates worker outcomes from explicit manager decisions", () => {
  assert.match(appSource, /function renderKpis\(snapshot\)/);
  assert.match(appSource, /worker outcomes and explicit manager decisions are separate/i);
  assert.match(appSource, /no token-savings or causal quality claim is inferred/i);
  assert.match(appSource, /renderKpis\(snapshot\)/);
});

test("dashboard labels queue, canonical outcomes, bounded failures, and effectiveness honestly", () => {
  assert.match(extensionSource, /Current queue/);
  assert.match(extensionSource, /Accepted and rejected are all-time manager ledger decisions/);
  assert.match(extensionSource, /metric-accepted/);
  assert.match(extensionSource, /metric-rejected/);
  assert.match(appSource, /Actionable review-ready/);
  assert.match(appSource, /Validation failure \(recent window\)/);
  assert.match(appSource, /Callback delivery \/ backlog/);
  assert.match(appSource, /no invocation evidence; efficacy unavailable/);
});

test("KPI visualizations include responsive chart and bar primitives", () => {
  assert.match(cssSource, /\.kpi-chart-grid/);
  assert.match(cssSource, /\.kpi-daily-chart/);
  assert.match(cssSource, /\.kpi-bar-track/);
  assert.match(cssSource, /@media \(max-width: 820px\)/);
});

test("KPI v4 renders Source Graph workflow, generations, call gaps and byte economics", () => {
  assert.match(appSource, /aiworkhub\.kpi\.dashboard\.v4/);
  assert.match(appSource, /Source Graph workflow stages/);
  assert.match(appSource, /Source Graph modes/);
  assert.match(appSource, /Tool-use cohorts/);
  assert.match(appSource, /Delivery reduction/);
  assert.match(appSource, /Delivery overhead/);
  assert.match(appSource, /estimated bytes added/);
  assert.match(appSource, /Optional suppression/);
  assert.match(appSource, /Envelope overhead/);
  assert.match(appSource, /serialization bytes added/);
  assert.match(appSource, /Provider cache hit/);
  assert.match(appSource, /Cost \/ review-ready/);
  assert.match(appSource, /Source Graph latency p50/);
  assert.match(appSource, /SG call gap p95/);
  assert.match(appSource, /SG long gaps/);
  assert.match(appSource, /not proof that the model was inactive/);
  assert.match(appSource, /SG evidence rows/);
  assert.match(appSource, /Source Graph index generations/);
  assert.match(appSource, /signed net delta between pre-optimization tool-section payload and delivered bundle bytes/);
  assert.match(appSource, /not raw repository-file, counterfactual read, or token-savings evidence/);
});

test("Operations KPIs render semantic-edit structural evidence without token claims", () => {
  assert.match(appSource, /Focused semantic edits/);
  assert.match(appSource, /Replacement \/ file bytes/);
  assert.match(appSource, /Old bytes re-emitted by model/);
  assert.match(appSource, /byte-shape evidence, not a token, cost, speed, or quality-savings claim/i);
  assert.match(appSource, /Paired baselines are required/);
});

test("Operations KPIs render truthful worker read-efficiency evidence", () => {
  assert.match(appSource, /snapshot\.read_efficiency_telemetry/);
  assert.match(appSource, /Read trace coverage/);
  assert.match(appSource, /Bounded file reads/);
  assert.match(appSource, /Worker read efficiency/);
  assert.match(appSource, /Read evidence by adapter/);
  assert.match(appSource, /legacy excluded/);
  assert.match(appSource, /incompatible legacy task\(s\) excluded/);
  assert.match(appSource, /Provider event\/byte evidence only; no token or savings claim/);
});

test("Daily worker outcomes chart no longer collapses states into a review/failed/other three-bucket legend", () => {
  assert.doesNotMatch(appSource, /"Other\/active"/);
  assert.doesNotMatch(appSource, /total - review - failed/);
  assert.doesNotMatch(appSource, /review-ready, validation-failed and other states/);
  assert.doesNotMatch(cssSource, /\.kpi-day-segment\.good/);
  assert.doesNotMatch(cssSource, /\.kpi-day-segment\.bad/);
  assert.doesNotMatch(cssSource, /\.kpi-day-segment\.neutral/);
  assert.doesNotMatch(cssSource, /\.kpi-legend-item i\.good/);
  assert.doesNotMatch(cssSource, /\.kpi-legend-item i\.bad/);
  assert.doesNotMatch(cssSource, /\.kpi-legend-item i\.neutral/);
});

test("Daily worker outcomes chart renders every observed state as its own accessible, distinctly colored segment", () => {
  const counts = dayStates({
    review_ready: 1,
    validation_failed: 1,
    worker_failed: 1,
    launch_failed: 1,
    timed_out: 1,
    cancelled: 1,
    scope_rejected: 1,
    blocked: 1,
    exited: 1,
  });
  counts.pending = 1;
  counts.processing = 1;
  const kpis = buildKpis([{ date: "2026-08-01", runs: 11, states: statesList(counts) }]);

  const panel = renderDailyPanel(kpis);
  assert.equal(panel.children.length, 3, "expected title, chart and legend");
  const [heading, chart, legend] = panel.children;
  assert.equal(heading.textContent, "Daily worker outcomes");
  assert.equal(chart.attrs.role, "img");
  assert.match(chart.attrs["aria-label"], /11 states/);

  const expectedOrder = [...DAILY_TERMINAL_STATE_ORDER, "pending", "processing"];

  const [column] = chart.children;
  const [stack] = column.children;
  assert.equal(stack.children.length, expectedOrder.length, "no state should be dropped or collapsed");
  assert.deepEqual(stack.children.map((segment) => segment.attrs["aria-label"].split(":")[0]),
    expectedOrder.map((state) => state.replaceAll("_", " ")));
  for (const segment of stack.children) {
    assert.match(segment.style.background, /^hsl\([\d.]+ 65% 55%\)$/);
    assert.equal(segment.style.height, undefined,
      "segments must not be sized by a height percentage -- that is what let many low-count states overflow past 100%");
    assert.ok(Number(segment.style.flexGrow) > 0, "segments must be sized by a flex weight");
    assert.equal(segment.style.flexBasis, "0%");
    assert.ok(segment.title.includes("2026-08-01"));
    assert.ok(segment.attrs["aria-label"].includes("2026-08-01"));
  }
  const distinctColors = new Set(stack.children.map((segment) => segment.style.background));
  assert.equal(distinctColors.size, expectedOrder.length, "every observed state must get a distinct color");

  assert.equal(legend.children.length, expectedOrder.length);
  assert.deepEqual(legend.children.map((item) => item.attrs["aria-label"]),
    expectedOrder.map((state) => state.replaceAll("_", " ")));
  for (let index = 0; index < legend.children.length; index += 1) {
    const item = legend.children[index];
    const swatch = item.children[0];
    assert.equal(swatch.attrs["aria-hidden"], "true");
    assert.equal(swatch.style.background, stack.children[index].style.background,
      "legend swatch color must match the chart segment color for the same state");
  }
});

test("Daily worker outcomes chart assigns every one of 13+ observed states its own non-repeating color", () => {
  const counts = dayStates({
    review_ready: 1,
    validation_failed: 1,
    worker_failed: 1,
    launch_failed: 1,
    timed_out: 1,
    cancelled: 1,
    scope_rejected: 1,
    blocked: 1,
    exited: 1,
  });
  for (const name of ["alpha", "bravo", "charlie", "delta"]) {
    counts[`custom_${name}`] = 1;
  }
  const runs = Object.keys(counts).length;
  const kpis = buildKpis([{ date: "2026-08-01", runs, states: statesList(counts) }]);

  const panel = renderDailyPanel(kpis);
  const [, chart, legend] = panel.children;
  const [column] = chart.children;
  const [stack] = column.children;

  assert.equal(stack.children.length, 13, "all 13 observed states must render, none dropped or collapsed");
  const colors = stack.children.map((segment) => segment.style.background);
  assert.equal(new Set(colors).size, 13,
    "the 13th and later states must not repeat an earlier state's color from a fixed-size swatch pool");
  assert.equal(new Set(legend.children.map((item) => item.children[0].style.background)).size, 13);
});

test("Daily worker outcomes ordering of unknown states is deterministic and does not depend on locale collation", () => {
  const counts = dayStates({ review_ready: 1 });
  counts.Beta_custom = 1;
  counts.alpha_custom = 1;
  const kpis = buildKpis([{ date: "2026-08-01", runs: 3, states: statesList(counts) }]);

  const panel = renderDailyPanel(kpis);
  const [, , legend] = panel.children;
  const nonterminalLabels = legend.children
    .map((item) => item.attrs["aria-label"])
    .filter((label) => label.includes("custom"));

  // Ordinal (code-point) order: uppercase "B" (0x42) sorts before lowercase
  // "a" (0x61) -- a locale-aware compare (e.g. localeCompare) would instead
  // put "alpha custom" first, which is exactly the drift this guards against.
  assert.deepEqual(nonterminalLabels, ["Beta custom", "alpha custom"]);
});

test("Daily worker outcomes chart hides zero-count states per day but keeps them in the shared legend once observed", () => {
  const day1 = dayStates({ review_ready: 2 });
  day1.custom_nonterminal_beta = 1;
  day1.custom_nonterminal_alpha = 1;
  const day2 = dayStates({ validation_failed: 3 });
  const kpis = buildKpis([
    { date: "2026-08-01", runs: 4, states: statesList(day1) },
    { date: "2026-08-02", runs: 3, states: statesList(day2) },
  ]);

  const panel = renderDailyPanel(kpis);
  const [, chart, legend] = panel.children;
  const [column1, column2] = chart.children;

  assert.equal(column1.children[0].children.length, 3, "only nonzero states render as segments for day 1");
  assert.equal(column2.children[0].children.length, 1, "only nonzero states render as segments for day 2");

  assert.deepEqual(legend.children.map((item) => item.attrs["aria-label"]), [
    "review ready",
    "validation failed",
    "custom nonterminal alpha",
    "custom nonterminal beta",
  ]);
});

test("Daily worker outcomes chart sources its terminal order from the backend KPI payload with no independent hardcoded copy", () => {
  assert.match(appSource, /const DAILY_STATE_ORDER = asArray\(kpis\.daily_state_order\)/,
    "app.js must read the canonical daily terminal order from kpis.daily_state_order, not define its own copy");
  assert.doesNotMatch(
    appSource,
    /"review_ready",\s*\n\s*"validation_failed",\s*\n\s*"worker_failed",\s*\n\s*"launch_failed"/,
    "app.js must not keep a second hardcoded copy of the backend's daily terminal order array",
  );
  assert.doesNotMatch(
    appSource,
    /paletteIndex\.has\(name\)\s*\?\s*paletteIndex\.get\(name\)\s*:\s*-1/,
    "colorForState must not carry an unreachable fallback -- every rendered name is always present in paletteOrder",
  );
  assert.doesNotMatch(
    appSource,
    /const safeIndex = index >= 0 \? index : 0;/,
    "stateColor must not carry an unreachable defensive branch for a negative index that colorForState never produces",
  );
});

test("Daily worker outcomes chart renders in whatever terminal order the backend supplies, not a fixed default", () => {
  // A deliberately reversed order proves rendering follows the *supplied*
  // kpis.daily_state_order value rather than any order baked into app.js.
  const reversedOrder = [...DAILY_TERMINAL_STATE_ORDER].reverse();
  const counts = dayStates({ review_ready: 1, exited: 1, blocked: 1 });
  const kpis = buildKpis(
    [{ date: "2026-08-01", runs: 3, states: statesList(counts) }],
    reversedOrder,
  );

  const panel = renderDailyPanel(kpis);
  const [, chart, legend] = panel.children;
  const [column] = chart.children;
  const [stack] = column.children;

  assert.deepEqual(
    stack.children.map((segment) => segment.attrs["aria-label"].split(":")[0]),
    ["exited", "blocked", "review ready"],
    "segment order must follow the supplied backend order (reversed), not the canonical default order",
  );
  assert.deepEqual(
    legend.children.map((item) => item.attrs["aria-label"]),
    ["exited", "blocked", "review ready"],
  );
});

test("Daily worker outcomes legend wraps responsively instead of relying on fixed-width layout", () => {
  assert.match(cssSource, /\.kpi-legend\s*\{[^}]*flex-wrap:\s*wrap/);
  assert.match(cssSource, /\.kpi-legend-item\s*\{[^}]*flex:\s*0 1 auto/);
  assert.doesNotMatch(cssSource, /--kpi-swatch-\d+:/,
    "colors must come from a stable per-index formula, not a fixed-size swatch variable pool that repeats past 12 states");
  assert.match(cssSource, /\.kpi-day-segment\s*\{[^}]*min-height:\s*0/,
    "segments must not carry a pixel min-height floor -- that is what let many-state stacks sum past 100%");
});
