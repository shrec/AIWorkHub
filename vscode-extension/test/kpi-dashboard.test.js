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
  assert.match(extensionSource, /<section aria-label="Live tasks">/);
  assert.match(extensionSource, /<section aria-label="Task outcomes">/);
  assert.doesNotMatch(extensionSource, /<h2[^>]*>Live tasks<\/h2>/);
  assert.doesNotMatch(extensionSource, /<h2[^>]*>Task outcomes<\/h2>/);
  assert.doesNotMatch(extensionSource, /Current canonical task states/);
  assert.doesNotMatch(extensionSource, /Accepted and rejected are all-time manager ledger decisions/);
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
  // This used to assert `@media (max-width: 820px)`, which stopped meaning
  // anything the moment an unrelated rule elsewhere in the file also matched
  // that string. The chart grid is intrinsic now: it reflows on how much room
  // a panel needs, not on a list of window widths, so what is asserted is the
  // sizing itself.
  const chartGridRule = cssSource.match(/\n\.kpi-chart-grid \{([^}]*)\}/)[1];
  assert.match(chartGridRule, /grid-template-columns:\s*repeat\(auto-fit,\s*minmax\(min\(100%,\s*\d+px\),\s*1fr\)\)/,
    "the chart grid must size intrinsically, and min() must stop a track exceeding a narrow container");
  assert.doesNotMatch(chartGridRule, /repeat\(\s*\d+\s*,/,
    "a fixed column count is what forced the breakpoint this replaced");
  // The one panel that answers "is the line healthy right now" gets the extra
  // column, and it asks the grid's own width rather than the window's.
  assert.match(cssSource, /@container \(min-width: \d+px\) \{\s*\.kpi-chart-grid > \.kpi-chart-panel:first-child \{ grid-column: span 2; \}/);
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

// ── Outcome colour contract ────────────────────────────────────────────────
// These five tests used to assert the OPPOSITE of what they assert now, and
// the change is deliberate. They pinned a golden-angle formula that derived
// each state's hue from its position in the list, and required every state to
// receive its own unique generated colour. That formula painted review_ready
// -- the successful outcome and 58.7% of 6,232 measured terminal runs -- pure
// red, and painted validation_failed and scope_rejected green. The old tests
// were green the whole time the chart was telling the operator the opposite of
// the truth, because "distinct" was the only property they checked.
//
// What is asserted instead is what actually matters: colour follows the
// entity, the successful outcome reads as success, no failure wears the
// success colour, and identity never depends on colour at all. What survived
// unchanged: no state is dropped or collapsed, every state keeps its own
// segment, tooltip and accessible label, ordering is locale-independent, and
// nothing is sized by a height percentage.

const SLOT_OF = {
  review_ready: "review_ready",
  cancelled: "decided",
  scope_rejected: "decided",
  validation_failed: "validation_failed",
  worker_failed: "worker_failed",
  launch_failed: "run_failed",
  timed_out: "run_failed",
  exited: "run_failed",
  finalize_failed: "run_failed",
  liveness_lost: "run_failed",
  output_budget_exceeded: "run_failed",
  token_budget_exceeded: "run_failed",
};

// The fixed slot order. It is declared in app.js rather than derived from the
// payload because the order decides which fills touch inside a stacked column,
// and those touching pairs are the ones the palette validator was run on.
const SLOT_ORDER = [
  "review_ready",
  "decided",
  "validation_failed",
  "worker_failed",
  "run_failed",
  "blocked",
  "other",
];

function slotOfSegment(segment) {
  const match = /\bstate-([a-z_]+)\b/.exec(segment.className || "");
  assert.ok(match, `segment must carry an entity-keyed slot class, got: ${segment.className}`);
  return match[1];
}

test("outcome colours are keyed by the state, never generated from its list position", () => {
  assert.doesNotMatch(appSource, /137\.508/,
    "the golden-angle hue formula must be gone -- it is what made review_ready red");
  assert.doesNotMatch(appSource, /hsl\(\$\{hue/,
    "no generated hue may remain");
  assert.doesNotMatch(appSource, /const stateColor = \(index\)/,
    "colour must not be a function of an index");
  assert.match(appSource, /const slotForState = \(name\)/,
    "colour must be a function of the state name");
  // A fill written into element.style would override the user's VS Code theme.
  assert.doesNotMatch(appSource, /segment\.style\.background/,
    "segment fills must come from a themed CSS token, not an inline colour");
  assert.doesNotMatch(appSource, /swatch\.style\.background/,
    "legend swatches must come from a themed CSS token, not an inline colour");
});

test("Daily worker outcomes chart renders every observed state as its own labelled segment in its own semantic slot", () => {
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

  const [column] = chart.children;
  const [stack] = column.children;

  // Nothing is dropped or merged away: 11 observed states, 11 segments.
  assert.equal(stack.children.length, 11, "no state should be dropped or collapsed");

  for (const segment of stack.children) {
    assert.equal(segment.style.background, undefined,
      "fills must be themed CSS tokens, never an inline colour that overrides the user's VS Code theme");
    assert.equal(segment.style.height, undefined,
      "segments must not be sized by a height percentage -- that is what let many low-count states overflow past 100%");
    assert.ok(Number(segment.style.flexGrow) > 0, "segments must be sized by a flex weight");
    assert.equal(segment.style.flexBasis, "0%");
    assert.ok(segment.title.includes("2026-08-01"));
    // Identity is never colour-alone: the exact state name is on every
    // segment, for the tooltip and for a screen reader, even where the fill is
    // shared with another state in the same slot.
    assert.ok(segment.attrs["aria-label"].includes("2026-08-01"));
  }

  const bySlot = stack.children.map(slotOfSegment);
  const byState = stack.children.map((s) => s.attrs["aria-label"].split(":")[0].replaceAll(" ", "_"));

  // Every canonical state lands in the slot its meaning demands.
  for (let i = 0; i < byState.length; i += 1) {
    const expected = SLOT_OF[byState[i]] || (byState[i] === "blocked" ? "blocked" : "other");
    assert.equal(bySlot[i], expected, `${byState[i]} must render in the ${expected} slot`);
  }

  // The successful outcome owns the success slot, and nothing else may enter it.
  const successSegments = stack.children.filter((s) => slotOfSegment(s) === "review_ready");
  assert.equal(successSegments.length, 1);
  assert.equal(successSegments[0].attrs["aria-label"].split(":")[0], "review ready");

  // No failure may wear the success colour. This is the exact inversion the
  // generated palette shipped: validation_failed at 137 degrees was green.
  for (const failure of ["validation_failed", "worker_failed", "launch_failed", "timed_out", "exited"]) {
    assert.notEqual(SLOT_OF[failure], "review_ready", `${failure} must never share the success slot`);
  }
  // A cancellation is a human decision, not a failure.
  assert.equal(SLOT_OF.cancelled, "decided");
  assert.notEqual(SLOT_OF.cancelled, "validation_failed");
  assert.notEqual(SLOT_OF.cancelled, "worker_failed");
  assert.notEqual(SLOT_OF.cancelled, "run_failed");

  // Fills render in the fixed slot order, so the pairs that touch inside a
  // column are the pairs the palette was validated on.
  const ranks = bySlot.map((slot) => SLOT_ORDER.indexOf(slot));
  assert.ok(ranks.every((r) => r >= 0), `every slot must be a declared one: ${bySlot.join(",")}`);
  assert.deepEqual(ranks, [...ranks].sort((a, b) => a - b),
    "segments must render in the fixed slot order that the palette was validated against");

  // ONE LEGEND ROW PER STATE, never one per colour. The ordinal gate admits
  // exactly three lightness steps for failure severity (a four-step ramp fails
  // the adjacent-dL floor in both modes -- measured), so several failure modes
  // necessarily share a band. Sharing a band must not cost a mode its name:
  // every one gets its own row, its own count and its own share, and nothing
  // is ever collapsed into an anonymous "other".
  assert.equal(legend.children.length, stack.children.length,
    "one legend row per observed state -- a failure mode must never be anonymous");
  assert.deepEqual(
    legend.children.map((item) => item.attrs["aria-label"].split(":")[0].replaceAll(" ", "_")),
    byState,
    "legend rows name every state, in the order they render",
  );
  for (const item of legend.children) {
    assert.match(item.attrs["aria-label"], /^[a-z ]+: \d+ outcomes, [\d.]+%$/,
      "every legend row carries its count and share, so the legend is the chart's table view");
    assert.ok(item.children[0].attrs["aria-hidden"] === "true", "the swatch is decorative");
    assert.equal(item.children[0].style.background, undefined,
      "swatch colour is a themed CSS class, not an inline colour");
  }
  // The modes that share the run-failure band each keep a row of their own.
  const sharedBand = legend.children.filter((i) => i.className.includes("state-run_failed"));
  assert.deepEqual(sharedBand.map((i) => i.attrs["aria-label"].split(":")[0]),
    ["launch failed", "timed out", "exited"],
    "three modes share one lightness band and keep three separate identities");
});

test("Daily worker outcomes chart still renders 13+ observed states, folding the unrecognised ones rather than inventing hues", () => {
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

  // The measured tail is what justifies folding: over 6,232 terminal outcomes
  // the top three states are 93.3% and nothing else clears 3%. A 3% state is a
  // sliver at the segment floor, so eight more unique hues would be eight
  // slivers no reader can separate -- the ninth series folds, it never gets a
  // generated hue.
  const slots = new Set(stack.children.map(slotOfSegment));
  assert.ok(slots.size <= SLOT_ORDER.length,
    `13 states must fold into at most ${SLOT_ORDER.length} slots, got ${slots.size}`);
  for (const slot of slots) {
    assert.ok(SLOT_ORDER.includes(slot), `unexpected slot ${slot}`);
  }

  // An unrecognised state is neutral, not a guessed alarm...
  const custom = stack.children.filter((s) => s.attrs["aria-label"].startsWith("custom "));
  assert.equal(custom.length, 4);
  for (const segment of custom) {
    assert.equal(slotOfSegment(segment), "other",
      "an unrecognised state takes the neutral fold, never a failure colour it did not earn");
  }
  // ...unless it names itself a failure in the convention the backend already
  // uses for every failure it emits.
  const named = renderDailyPanel(buildKpis([{
    date: "2026-08-01",
    runs: 2,
    states: [{ state: "review_ready", count: 1 }, { state: "finalize_failed", count: 1 }],
  }]));
  const namedSegments = named.children[1].children[0].children[0].children;
  assert.equal(slotOfSegment(namedSegments[1]), "run_failed");

  // Thirteen states, thirteen legend rows: folding a colour band never folds
  // an identity.
  assert.equal(legend.children.length, 13);
});

test("Daily worker outcomes ordering of unknown states is deterministic and does not depend on locale collation", () => {
  const counts = dayStates({ review_ready: 1 });
  counts.Beta_custom = 1;
  counts.alpha_custom = 1;
  const kpis = buildKpis([{ date: "2026-08-01", runs: 3, states: statesList(counts) }]);

  const panel = renderDailyPanel(kpis);
  const [, chart, legend] = panel.children;
  const [stack] = chart.children[0].children;

  const nonterminal = stack.children
    .map((segment) => segment.attrs["aria-label"].split(":")[0])
    .filter((label) => label.includes("custom"));

  // Ordinal (code-point) order: uppercase "B" (0x42) sorts before lowercase
  // "a" (0x61) -- a locale-aware compare (e.g. localeCompare) would instead
  // put "alpha custom" first, which is exactly the drift this guards against.
  // Slot folding does not disturb it: both states fold into the same slot, so
  // their relative order is still the payload's.
  assert.deepEqual(nonterminal, ["Beta custom", "alpha custom"]);

  // The legend names both of them individually, in the same order.
  assert.deepEqual(
    legend.children.map((item) => item.attrs["aria-label"].split(":")[0]).filter((l) => l.includes("custom")),
    ["Beta custom", "alpha custom"],
  );
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

  // The legend spans the whole window, so a state observed on any day stays in
  // it, one row per state, carrying the window totals and shares.
  assert.deepEqual(legend.children.map((item) => item.attrs["aria-label"].split(":")[0]),
    ["review ready", "validation failed", "custom nonterminal alpha", "custom nonterminal beta"]);
  assert.equal(legend.children[0].attrs["aria-label"], "review ready: 2 outcomes, 28.6%");
  assert.equal(legend.children[1].attrs["aria-label"], "validation failed: 3 outcomes, 42.9%");
  assert.equal(legend.children[2].attrs["aria-label"], "custom nonterminal alpha: 1 outcomes, 14.3%");
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
});

test("the payload keeps authority over which states exist and over their order inside a slot", () => {
  // The palette fixes the order BETWEEN slots, because that order is what
  // makes the colours safe. It must not take over the order WITHIN a slot --
  // that still belongs to the payload, and a state the backend has never
  // mentioned must still render.
  const forwardOrder = ["launch_failed", "timed_out", "exited"];
  const build = (order) => {
    const panel = renderDailyPanel(buildKpis(
      [{
        date: "2026-08-01",
        runs: 4,
        states: [
          { state: "review_ready", count: 1 },
          { state: "launch_failed", count: 1 },
          { state: "timed_out", count: 1 },
          { state: "exited", count: 1 },
        ],
      }],
      ["review_ready", ...order],
    ));
    return panel.children[1].children[0].children[0].children
      .map((segment) => segment.attrs["aria-label"].split(":")[0].replaceAll(" ", "_"));
  };

  assert.deepEqual(build(forwardOrder), ["review_ready", ...forwardOrder]);
  const reversed = [...forwardOrder].reverse();
  assert.deepEqual(build(reversed), ["review_ready", ...reversed],
    "three states that share one colour slot must still render in the order the payload supplied");

  // A state absent from daily_state_order entirely still renders.
  const panel = renderDailyPanel(buildKpis(
    [{ date: "2026-08-01", runs: 2, states: [{ state: "review_ready", count: 1 }, { state: "brand_new_state", count: 1 }] }],
    ["review_ready"],
  ));
  const labels = panel.children[1].children[0].children[0].children.map((s) => s.attrs["aria-label"].split(":")[0]);
  assert.deepEqual(labels, ["review ready", "brand new state"]);
});

test("Daily worker outcomes legend wraps responsively instead of relying on fixed-width layout", () => {
  assert.match(cssSource, /\.kpi-legend\s*\{[^}]*flex-wrap:\s*wrap/);
  assert.match(cssSource, /\.kpi-legend-item\s*\{[^}]*flex:\s*0 1 auto/);
  assert.doesNotMatch(cssSource, /--kpi-swatch-\d+:/,
    "colors must come from a stable per-index formula, not a fixed-size swatch variable pool that repeats past 12 states");
  assert.match(cssSource, /\.kpi-day-segment\s*\{[^}]*min-height:\s*0/,
    "segments must not carry a pixel min-height floor -- that is what let many-state stacks sum past 100%");
});

function hostArray(value) {
  return Array.from(value || []);
}

function makeSettingsElement(tag) {
  const element = {
    tag,
    className: "",
    textContent: "",
    hidden: false,
    id: "",
    type: "",
    checked: false,
    disabled: false,
    dataset: {},
    attrs: {},
    children: [],
    parentNode: null,
    listeners: {},
    classList: {
      _owner: null,
      toggle(name, force) {
        const parts = String(this._owner.className || "").split(/\s+/).filter(Boolean);
        const has = parts.includes(name);
        const next = force === undefined ? !has : Boolean(force);
        const updated = next ? (has ? parts : parts.concat(name)) : parts.filter((item) => item !== name);
        this._owner.className = updated.join(" ");
      },
    },
    style: {},
    setAttribute(name, value) {
      this.attrs[name] = String(value);
      if (name.startsWith("data-")) {
        const key = name.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
        this.dataset[key] = String(value);
      }
      if (name === "id") this.id = String(value);
    },
    getAttribute(name) {
      return this.attrs[name];
    },
    appendChild(child) {
      if (child && typeof child === "object") child.parentNode = this;
      this.children.push(child);
      return child;
    },
    append(...nodes) {
      for (const child of nodes) {
        if (child && typeof child === "object") child.parentNode = this;
      }
      this.children.push(...nodes);
    },
    replaceChildren(...nodes) {
      this.children.splice(0, this.children.length, ...nodes);
    },
    contains() {
      return false;
    },
    querySelector(selector) {
      const visit = (node) => {
        if (!node || typeof node !== "object") return null;
        if (selector.startsWith(".") && String(node.className || "").split(/\s+/).includes(selector.slice(1))) {
          return node;
        }
        for (const child of hostArray(node.children)) {
          const found = visit(child);
          if (found) return found;
        }
        return null;
      };
      return visit(this);
    },
    querySelectorAll(selector) {
      const found = [];
      const visit = (node) => {
        if (!node || typeof node !== "object") return;
        if (selector.startsWith("[data-settings-tab]") && node.dataset && node.dataset.settingsTab) found.push(node);
        if (selector.startsWith("[data-settings-panel]") && node.dataset && node.dataset.settingsPanel) found.push(node);
        if (selector.startsWith(".") && String(node.className || "").split(/\s+/).includes(selector.slice(1))) {
          found.push(node);
        }
        for (const child of hostArray(node.children)) visit(child);
      };
      visit(this);
      return found;
    },
    closest(selector) {
      let node = this;
      while (node) {
        if (selector.startsWith(".") && String(node.className || "").split(/\s+/).includes(selector.slice(1))) {
          return node;
        }
        if (selector === "[data-model-family-toggle]" && node.dataset && node.dataset.modelFamilyToggle) {
          return node;
        }
        node = node.parentNode;
      }
      return null;
    },
    addEventListener(type, handler) {
      if (!this.listeners[type]) this.listeners[type] = [];
      this.listeners[type].push(handler);
    },
    dispatchEvent(event) {
      for (const handler of this.listeners[event.type] || []) handler(event);
      return true;
    },
  };
  element.classList._owner = element;
  return element;
}

function collectByClass(node, className, acc = []) {
  if (!node || typeof node !== "object") return acc;
  if (String(node.className || "").split(/\s+/).includes(className)) acc.push(node);
  for (const child of hostArray(node.children)) collectByClass(child, className, acc);
  return acc;
}

test("Models CSS distinguishes indented OpenCode child rows without the dashboard-card overflow regression", () => {
  assert.match(cssSource, /\.settings-model-family\s*\{/);
  assert.match(cssSource, /\.settings-model-family-toggle\s*\{/);
  assert.match(cssSource, /\.settings-model-family-children\s*\{/);
  assert.match(cssSource, /\.settings-model-route\s*\{[^}]*padding-left:\s*30px/);
  assert.match(cssSource, /\.settings-model-truth\s*,\s*\n\s*\.settings-model-warning\s*\{/);
  assert.doesNotMatch(cssSource, /\.settings-model-route\s*\{[^}]*width:\s*calc/);
  assert.doesNotMatch(cssSource, /\.settings-model-family[^{]*\{[^}]*dashboard-card/);
});

test("Models renders one OpenCode family with collapsible exact-model children and honest unknown truth", () => {
  const start = appSource.indexOf("const OPENCODE_ADAPTER_ID = \"opencode_cli\";");
  const end = appSource.indexOf("function renderSettings(payload, options = {})");
  assert.notEqual(start, -1);
  assert.notEqual(end, -1);
  const helpers = appSource.slice(start, end);
  const renderStart = appSource.indexOf("function renderSettings(payload, options = {})");
  const renderEnd = appSource.indexOf("\n// ═══ HISTORY_PAGE_BEGIN", renderStart);
  assert.notEqual(renderStart, -1);
  assert.notEqual(renderEnd, -1);
  const renderFn = appSource.slice(renderStart, renderEnd);
  const clickStart = appSource.indexOf('elements.settingsList.addEventListener("click", (event) => {');
  const clickEnd = appSource.indexOf("\nelements.kbSearch.addEventListener", clickStart);
  assert.notEqual(clickStart, -1);
  assert.notEqual(clickEnd, -1);
  const clickListener = appSource.slice(clickStart, clickEnd);
  const settingsList = makeSettingsElement("div");
  settingsList.scrollTop = 0;
  settingsList.scrollHeight = 0;
  const settingsDialog = makeSettingsElement("dialog");
  settingsDialog.scrollTop = 0;
  const settingsSummary = makeSettingsElement("span");
  const result = { families: null, routes: null, truths: null, toggle: null, children: null };
  const harness = `
    "use strict";
    const FEATURE_LABELS = {};
    const state = { settingsTab: "models", settingsCollapsedFamilies: {}, settingsPendingIdentity: null, featureSettings: null };
    const elements = {
      settingsList,
      settingsDialog,
      settingsSummary,
    };
    function settingsTabDefinitions() {
      return [
        ["features", "Features"],
        ["models", "Models"],
        ["source-graph", "Source Graph"],
        ["retention", "Retention"],
        ["telemetry", "Telemetry"],
      ];
    }
    function createElement(tag, className, text) {
      const element = document.createElement(tag);
      if (className) element.className = className;
      if (text !== undefined && text !== null) element.textContent = String(text);
      return element;
    }
    function settingsControlIdentity() { return null; }
    function restoreSettingsFocus() {}
    function setSettingsPending() {}
    function renderSettingsPlaceholder() {}
    function settingsStateMessage() { return document.createElement("div"); }
    function numberValue(value) { const parsed = Number(value); return Number.isFinite(parsed) ? parsed : 0; }
    function formatCount(value) { return String(value); }
    function formatBytes() { return "0 B"; }
    function formatMoney(value) { return String(value); }
    ${helpers}
    ${renderFn}
    ${clickListener}
    renderSettings({
      ok: true,
      revision: 1,
      features: {},
      model_policy: {
        ok: true,
        revision: 1,
        providers: {},
        catalog: {
          discovered_model_count: 2,
          workers: [
            {
              provider: "opencode",
              adapter: "opencode_cli",
              model: "opencode/glm-4.5-free",
              worker_id: "",
              catalog_enabled: true,
              effective_enabled: true,
              inventory_only: true,
              discovered_from_opencode: true,
            },
            {
              provider: "opencode",
              adapter: "opencode_cli",
              model: "openai/gpt-4o",
              worker_id: "",
              catalog_enabled: true,
              effective_enabled: false,
              inventory_only: true,
              discovered_from_opencode: true,
            },
            {
              provider: "copilot",
              adapter: "vscode_lm",
              model: "gpt-5.6-sol",
              worker_id: "copilot",
              catalog_enabled: true,
              effective_enabled: true,
              inventory_only: false,
            },
          ],
        },
      },
    });
    const families = [];
    const collect = (node) => {
      if (!node || typeof node !== "object") return;
      if (String(node.className || "").split(/\\s+/).includes("settings-model-family")) families.push(node);
      for (const child of Array.from(node.children || [])) collect(child);
    };
    collect(settingsList);
    result.families = families;
  `;
  const context = vm.createContext({
    document: {
      createElement: (tag) => makeSettingsElement(tag),
      createDocumentFragment: () => makeSettingsElement("fragment"),
      activeElement: null,
      scrollingElement: null,
    },
    settingsList,
    settingsDialog,
    settingsSummary,
    result,
  });
  vm.runInContext(harness, context);
  const families = hostArray(result.families);
  assert.equal(families.length, 2);
  const labels = families.map((family) => {
    const provider = collectByClass(family, "settings-model-provider")[0];
    const strong = collectByClass(provider || family, "settings-copy")[0];
    const title = hostArray((strong || {}).children).find((child) => child.tag === "strong");
    return title ? title.textContent : "";
  });
  assert.deepEqual(labels, ["copilot", "OpenCode"]);
  const openCode = families[1];
  const routes = collectByClass(openCode, "settings-model-route");
  assert.equal(routes.length, 2);
  const models = routes.map((row) => {
    const copy = collectByClass(row, "settings-copy")[0];
    const title = hostArray((copy || {}).children).find((child) => child.tag === "strong");
    return title ? title.textContent : "";
  });
  assert.deepEqual(models, ["opencode/glm-4.5-free", "openai/gpt-4o"]);
  const truths = routes.map((row) => {
    const truth = collectByClass(row, "settings-model-truth")[0];
    return truth ? truth.textContent : "";
  });
  assert.match(truths[0], /opencode discovery/);
  assert.match(truths[0], /access unknown/);
  assert.match(truths[0], /round-trip unknown/);
  assert.match(truths[0], /cost unknown/);
  assert.doesNotMatch(truths[1], /\bfree\b/);
  assert.match(truths[1], /not launchable/);
  const warning = collectByClass(openCode, "settings-model-warning")[0];
  assert.ok(warning);
  const toggle = collectByClass(openCode, "settings-model-family-toggle")[0];
  const children = collectByClass(openCode, "settings-model-family-children")[0];
  assert.equal(toggle.attrs["aria-expanded"], "true");
  assert.equal(children.hidden, false);
  settingsList.dispatchEvent({ type: "click", target: toggle });
  assert.equal(children.hidden, true);
  assert.equal(toggle.attrs["aria-expanded"], "false");
  assert.equal(toggle.attrs["aria-label"], "OpenCode routes, collapsed");
  assert.equal(toggle.textContent, "▸");
});

function collectByTag(node, tag, acc = []) {
  if (!node || typeof node !== "object") return acc;
  if (node.tag === tag) acc.push(node);
  for (const child of hostArray(node.children)) collectByTag(child, tag, acc);
  return acc;
}

test("Models renders every provider the bounded catalog returned, with per-provider truncation truth and a toggleable xai route", () => {
  // Mirrors what the backend publishes for an 81-row catalog: the compact
  // selection keeps every provider, and provider_counts carries the
  // total/returned/truncated truth the row list alone cannot state.
  const bounded = [];
  for (const [provider, adapter, kept] of [
    ["anthropic", "claude_cli", 32],
    ["copilot", "vscode_lm", 31],
  ]) {
    for (let index = 0; index < kept; index += 1) {
      bounded.push({
        provider,
        adapter,
        model: `${provider}-model-${String(index).padStart(2, "0")}`,
        worker_id: `${provider}-${index}`,
        catalog_enabled: true,
        // Three of copilot's shown routes are switched off, so its shown
        // enabled count (28) differs from both its shown row count and its
        // provider-wide enabled total (36). Without that gap the label could
        // pass by coincidence.
        effective_enabled: !(provider === "copilot" && index < 3),
        inventory_only: false,
      });
    }
  }
  bounded.push({
    provider: "xai",
    adapter: "grok_kilo_cli",
    model: "grok-4.6",
    worker_id: "xai-grok",
    catalog_enabled: true,
    effective_enabled: true,
    inventory_only: false,
  });
  // enabled_total is the provider's, counted before the bound was spent;
  // enabled_returned is the shown rows'. The two differ exactly where the
  // provider is truncated, which is what the label has to survive.
  const providerCounts = [
    {
      provider: "anthropic",
      total: 40,
      returned: 32,
      truncated: true,
      enabled_total: 40,
      enabled_returned: 32,
    },
    {
      provider: "copilot",
      total: 40,
      returned: 31,
      truncated: true,
      enabled_total: 36,
      enabled_returned: 28,
    },
    {
      provider: "xai",
      total: 1,
      returned: 1,
      truncated: false,
      enabled_total: 1,
      enabled_returned: 1,
    },
  ];

  const start = appSource.indexOf("const OPENCODE_ADAPTER_ID = \"opencode_cli\";");
  const end = appSource.indexOf("function renderSettings(payload, options = {})");
  assert.notEqual(start, -1);
  assert.notEqual(end, -1);
  const helpers = appSource.slice(start, end);
  const renderStart = appSource.indexOf("function renderSettings(payload, options = {})");
  const renderEnd = appSource.indexOf("\n// ═══ HISTORY_PAGE_BEGIN", renderStart);
  assert.notEqual(renderStart, -1);
  assert.notEqual(renderEnd, -1);
  const renderFn = appSource.slice(renderStart, renderEnd);
  const settingsList = makeSettingsElement("div");
  settingsList.scrollTop = 0;
  settingsList.scrollHeight = 0;
  const settingsDialog = makeSettingsElement("dialog");
  settingsDialog.scrollTop = 0;
  const settingsSummary = makeSettingsElement("span");
  const result = { families: null };
  const harness = `
    "use strict";
    const FEATURE_LABELS = {};
    const state = { settingsTab: "models", settingsCollapsedFamilies: {}, settingsPendingIdentity: null, featureSettings: null };
    const elements = { settingsList, settingsDialog, settingsSummary };
    function settingsTabDefinitions() {
      return [
        ["features", "Features"],
        ["models", "Models"],
        ["source-graph", "Source Graph"],
        ["retention", "Retention"],
        ["telemetry", "Telemetry"],
      ];
    }
    function createElement(tag, className, text) {
      const element = document.createElement(tag);
      if (className) element.className = className;
      if (text !== undefined && text !== null) element.textContent = String(text);
      return element;
    }
    // A real identity, never null: settingsPendingIdentity is null when no
    // control is in flight, so a null-returning stub would mark every control
    // pending and disable it, hiding whether a route is actually toggleable.
    function settingsControlIdentity(input) {
      const data = (input && input.dataset) || {};
      return [data.modelProvider || "", data.modelAdapter || "", data.modelName || ""].join("|");
    }
    function restoreSettingsFocus() {}
    function setSettingsPending() {}
    function renderSettingsPlaceholder() {}
    function settingsStateMessage() { return document.createElement("div"); }
    function numberValue(value) { const parsed = Number(value); return Number.isFinite(parsed) ? parsed : 0; }
    function formatCount(value) { return String(value); }
    function formatBytes() { return "0 B"; }
    function formatMoney(value) { return String(value); }
    ${helpers}
    ${renderFn}
    renderSettings({
      ok: true,
      revision: 2,
      features: {},
      model_policy: {
        ok: true,
        revision: 2,
        providers: {},
        catalog: {
          discovered_model_count: 0,
          worker_count: 81,
          returned_worker_count: ${bounded.length},
          row_limit: 64,
          truncated: true,
          provider_counts: ${JSON.stringify(providerCounts)},
          workers: ${JSON.stringify(bounded)},
        },
      },
    });
    const families = [];
    const collect = (node) => {
      if (!node || typeof node !== "object") return;
      if (String(node.className || "").split(/\\s+/).includes("settings-model-family")) families.push(node);
      for (const child of Array.from(node.children || [])) collect(child);
    };
    collect(settingsList);
    result.families = families;
  `;
  const context = vm.createContext({
    document: {
      createElement: (tag) => makeSettingsElement(tag),
      createDocumentFragment: () => makeSettingsElement("fragment"),
      activeElement: null,
      scrollingElement: null,
    },
    settingsList,
    settingsDialog,
    settingsSummary,
    result,
  });
  vm.runInContext(harness, context);

  const families = hostArray(result.families);
  const familyText = (family, className) => {
    const provider = collectByClass(family, "settings-model-provider")[0];
    const copy = collectByClass(provider || family, "settings-copy")[0];
    const node = hostArray((copy || {}).children).find((child) => child.tag === className);
    return node ? node.textContent : "";
  };

  // No provider is missing, including the one that sorts last.
  assert.equal(families.length, 3);
  assert.deepEqual(families.map((family) => familyText(family, "strong")), [
    "anthropic",
    "copilot",
    "xai",
  ]);

  // A truncated provider says so; an intact one still reads as a plain count.
  // The enabled counts move with it, and each one names the population it was
  // counted over: the shown rows' enabled count is stated against the shown
  // rows, the provider's against the provider. Neither number is ever printed
  // against the other's denominator.
  assert.match(
    familyText(families[0], "small"),
    /^32 of 40 routes shown · 32 of 32 shown enabled · 40 of 40 enabled provider-wide · /,
  );
  assert.match(
    familyText(families[1], "small"),
    /^31 of 40 routes shown · 28 of 31 shown enabled · 36 of 40 enabled provider-wide · /,
  );
  assert.match(familyText(families[2], "small"), /^1 route · 1 enabled · /);
  // The bare "N enabled" form survives only where nothing was truncated, and
  // the old mixed-population form is gone rather than merely reworded.
  assert.doesNotMatch(familyText(families[0], "small"), /· 32 enabled ·/);
  assert.doesNotMatch(familyText(families[1], "small"), /· 28 enabled ·/);
  assert.doesNotMatch(familyText(families[1], "small"), /28 of 36 enabled/);

  // xai/grok-4.6 is both rendered and toggleable.
  const xaiRoutes = collectByClass(families[2], "settings-model-route");
  assert.equal(xaiRoutes.length, 1);
  const routeInput = collectByTag(xaiRoutes[0], "input")[0];
  assert.ok(routeInput);
  assert.equal(routeInput.disabled, false);
  assert.equal(routeInput.checked, true);
  assert.equal(routeInput.dataset.modelProvider, "xai");
  assert.equal(routeInput.dataset.modelName, "grok-4.6");
});

// Renders the Models tree for one bounded payload and returns both the family
// nodes and the section heading, so a test can state the payload it cares about
// instead of restating the harness that drives the shipped renderSettings block.
// The heading matters on its own: what a hard ceiling refused is a fact about
// the whole tree rather than about any one family, so it has nowhere else to be
// stated and a test that only ever sees families could not catch its absence.
function renderModelSettings(catalog) {
  const start = appSource.indexOf("const OPENCODE_ADAPTER_ID = \"opencode_cli\";");
  const end = appSource.indexOf("function renderSettings(payload, options = {})");
  assert.notEqual(start, -1);
  assert.notEqual(end, -1);
  const helpers = appSource.slice(start, end);
  const renderStart = end;
  const renderEnd = appSource.indexOf("\n// ═══ HISTORY_PAGE_BEGIN", renderStart);
  assert.notEqual(renderEnd, -1);
  const renderFn = appSource.slice(renderStart, renderEnd);
  const settingsList = makeSettingsElement("div");
  settingsList.scrollTop = 0;
  settingsList.scrollHeight = 0;
  const settingsDialog = makeSettingsElement("dialog");
  settingsDialog.scrollTop = 0;
  const settingsSummary = makeSettingsElement("span");
  const result = { families: null, headings: null };
  const harness = `
    "use strict";
    const FEATURE_LABELS = {};
    const state = { settingsTab: "models", settingsCollapsedFamilies: {}, settingsPendingIdentity: null, featureSettings: null };
    const elements = { settingsList, settingsDialog, settingsSummary };
    function settingsTabDefinitions() {
      return [
        ["features", "Features"],
        ["models", "Models"],
        ["source-graph", "Source Graph"],
        ["retention", "Retention"],
        ["telemetry", "Telemetry"],
      ];
    }
    function createElement(tag, className, text) {
      const element = document.createElement(tag);
      if (className) element.className = className;
      if (text !== undefined && text !== null) element.textContent = String(text);
      return element;
    }
    function settingsControlIdentity(input) {
      const data = (input && input.dataset) || {};
      return [data.modelProvider || "", data.modelAdapter || "", data.modelName || ""].join("|");
    }
    function restoreSettingsFocus() {}
    function setSettingsPending() {}
    function renderSettingsPlaceholder() {}
    function settingsStateMessage() { return document.createElement("div"); }
    function numberValue(value) { const parsed = Number(value); return Number.isFinite(parsed) ? parsed : 0; }
    function formatCount(value) { return String(value); }
    function formatBytes() { return "0 B"; }
    function formatMoney(value) { return String(value); }
    ${helpers}
    ${renderFn}
    renderSettings({
      ok: true,
      revision: 2,
      features: {},
      model_policy: { ok: true, revision: 2, providers: {}, catalog: ${JSON.stringify(catalog)} },
    });
    const families = [];
    const headings = [];
    const collect = (node) => {
      if (!node || typeof node !== "object") return;
      const classes = String(node.className || "").split(/\\s+/);
      if (classes.includes("settings-model-family")) families.push(node);
      if (classes.includes("settings-group-heading")) headings.push(node);
      for (const child of Array.from(node.children || [])) collect(child);
    };
    collect(settingsList);
    result.families = families;
    result.headings = headings;
  `;
  const context = vm.createContext({
    document: {
      createElement: (tag) => makeSettingsElement(tag),
      createDocumentFragment: () => makeSettingsElement("fragment"),
      activeElement: null,
      scrollingElement: null,
    },
    settingsList,
    settingsDialog,
    settingsSummary,
    result,
  });
  vm.runInContext(harness, context);
  return {
    families: hostArray(result.families),
    headings: hostArray(result.headings),
  };
}

function renderModelFamilies(catalog) {
  return renderModelSettings(catalog).families;
}

// Every warning line the Models heading carries, joined, so a test states what
// the reader is told rather than which child index carried it.
function headingWarningText(headings) {
  return headings
    .flatMap((heading) => hostArray(heading.children))
    .filter((child) => String(child.className || "").split(/\s+/).includes("settings-model-warning"))
    .map((child) => child.textContent)
    .join(" · ");
}

function familySmallText(family) {
  const provider = collectByClass(family, "settings-model-provider")[0];
  const copy = collectByClass(provider || family, "settings-copy")[0];
  const node = hostArray((copy || {}).children).find((child) => child.tag === "small");
  return node ? node.textContent : "";
}

test("Models states ingestion loss against the population it was counted over, and keeps the xai route toggleable", () => {
  // The backend's ingestion cap runs BEFORE the render bound, so a provider can
  // lose rows that the render bound never saw. Two things went wrong with that.
  //
  // First, provider_counts.total was taken from the rows that survived the cap,
  // so a 600-route provider published "511 of 511" and this tree drew a heavily
  // truncated provider as complete. The total is now the provider's own size.
  //
  // Second, the loss beside it was keyed by the vendor spelling ("xai") while
  // this tree groups by the canonical policy owner ("opencode"), so an
  // xai/opencode_cli shortfall named a family that is never drawn: published,
  // and still unreachable. Both are now in one canonical key space.
  const families = renderModelFamilies({
    discovered_model_count: 0,
    worker_count: 512,
    returned_worker_count: 2,
    row_limit: 64,
    truncated: true,
    provider_counts: [
      {
        provider: "opencode",
        total: 600,
        ingested: 511,
        returned: 2,
        truncated: true,
        enabled_total: 511,
        enabled_counted_over: 511,
        enabled_returned: 2,
      },
    ],
    source_ingestion_loss: [
      {
        provider: "opencode",
        total: 600,
        ingested: 511,
        dropped: 89,
        absent_routes: 89,
        vendor_providers: ["xai"],
      },
    ],
    workers: [
      {
        provider: "opencode",
        adapter: "opencode_cli",
        model: "opencode/model-00-free",
        worker_id: "",
        vendor_provider: "opencode",
        catalog_enabled: true,
        effective_enabled: true,
        inventory_only: true,
        discovered_from_opencode: true,
      },
      {
        provider: "opencode",
        adapter: "opencode_cli",
        model: "xai/grok-4.6",
        worker_id: "grok_opencode",
        vendor_provider: "xai",
        catalog_enabled: true,
        effective_enabled: true,
        inventory_only: false,
      },
    ],
  });

  assert.equal(families.length, 1);
  const label = familySmallText(families[0]);
  // The provider's real size, not the ingestion cap's arithmetic.
  assert.match(label, /^2 of 600 routes shown · /);
  // The enabled figure names the population it was counted over. Claiming 511
  // of 511 "provider-wide" for a 600-route provider is the same lie one field
  // over, so that phrasing must be absent rather than merely reworded.
  assert.match(label, /511 of 511 enabled among loaded routes/);
  assert.doesNotMatch(label, /provider-wide/);
  assert.doesNotMatch(label, /511 of 511 routes shown/);
  // The loss is reachable from the family the tree actually draws.
  assert.match(label, /89 not loaded \(catalog bound\)/);

  // And the route this whole card is about is still drawn with a live control.
  const routes = collectByClass(families[0], "settings-model-route");
  const grok = routes.find((route) => collectByTag(route, "input")[0].dataset.modelName === "xai/grok-4.6");
  assert.ok(grok);
  const grokInput = collectByTag(grok, "input")[0];
  assert.equal(grokInput.disabled, false);
  assert.equal(grokInput.checked, true);
  // Toggling writes the canonical policy owner the launcher consults, which is
  // what makes a checked box and a launch-eligible route the same claim.
  assert.equal(grokInput.dataset.modelProvider, "opencode");
  assert.equal(grokInput.dataset.modelAdapter, "opencode_cli");
});

test("Models still claims a provider-wide enabled total when nothing was lost to ingestion", () => {
  // The counterpart to the test above: with no ingestion loss the enabled count
  // really is the provider's, and qualifying it would understate the payload.
  const families = renderModelFamilies({
    discovered_model_count: 0,
    worker_count: 40,
    returned_worker_count: 1,
    row_limit: 64,
    truncated: true,
    provider_counts: [
      {
        provider: "anthropic",
        total: 40,
        ingested: 40,
        returned: 1,
        truncated: true,
        enabled_total: 36,
        enabled_counted_over: 40,
        enabled_returned: 1,
      },
    ],
    source_ingestion_loss: [],
    workers: [
      {
        provider: "anthropic",
        adapter: "claude_cli",
        model: "anthropic-model-00",
        worker_id: "anthropic-0",
        catalog_enabled: true,
        effective_enabled: true,
        inventory_only: false,
      },
    ],
  });

  const label = familySmallText(families[0]);
  assert.match(label, /^1 of 40 routes shown · 1 of 1 shown enabled · 36 of 40 enabled provider-wide · /);
  assert.doesNotMatch(label, /among loaded routes/);
  assert.doesNotMatch(label, /not loaded \(catalog bound\)/);
  assert.doesNotMatch(label, /not loaded \(discovery bound\)/);
  assert.doesNotMatch(label, /not loaded \(editor bound\)/);
});

test("Models names the OpenCode discovery bound's loss separately from the catalog bound's", () => {
  // The OpenCode discovery probe is a second ingestion source with its own
  // bound, and it kept the head slice the configured catalog had already given
  // up: a vendor past the cap vanished from this tree with every published
  // number still adding up, because the counts were taken after the slice.
  //
  // Its loss now arrives keyed by the canonical policy owner -- the same key
  // space this tree groups by -- and is named in its own clause rather than
  // summed into the catalog bound's, because "not loaded" has a different
  // cause and a different remedy depending on which bound refused the row.
  const families = renderModelFamilies({
    discovered_model_count: 512,
    opencode_discovered_model_count: 512,
    worker_count: 512,
    returned_worker_count: 2,
    row_limit: 64,
    truncated: true,
    provider_counts: [
      {
        provider: "opencode",
        total: 601,
        ingested: 512,
        returned: 2,
        truncated: true,
        enabled_total: 512,
        enabled_counted_over: 512,
        enabled_returned: 2,
      },
    ],
    source_ingestion_loss: [],
    opencode_source: {
      total: 601,
      returned: 512,
      truncated: true,
      row_limit: 512,
      row_limit_honoured: 512,
      ingestion_loss: [
        {
          provider: "opencode",
          total: 601,
          ingested: 512,
          dropped: 89,
          absent_routes: 89,
          vendor_providers: ["opencode"],
        },
      ],
    },
    workers: [
      {
        provider: "opencode",
        adapter: "opencode_cli",
        model: "opencode/model-599-free",
        worker_id: "",
        vendor_provider: "opencode",
        catalog_enabled: true,
        effective_enabled: true,
        inventory_only: true,
        discovered_from_opencode: true,
      },
      {
        provider: "opencode",
        adapter: "opencode_cli",
        model: "xai/grok-4.6",
        worker_id: "",
        vendor_provider: "xai",
        catalog_enabled: true,
        effective_enabled: true,
        inventory_only: true,
        discovered_from_opencode: true,
      },
    ],
  });

  assert.equal(families.length, 1);
  const label = familySmallText(families[0]);
  // The provider's real size, which now includes what the discovery bound cost
  // it -- the row list alone would have read as 2 of 512.
  assert.match(label, /^2 of 601 routes shown · /);
  // Named as the discovery bound's loss, and not mislabelled as the catalog's.
  assert.match(label, /89 not loaded \(discovery bound\)/);
  assert.doesNotMatch(label, /not loaded \(catalog bound\)/);

  // The vendor the discovery sequence listed last is still drawn, and still
  // toggleable through the canonical owner the launcher consults.
  const routes = collectByClass(families[0], "settings-model-route");
  const grok = routes.find((route) => collectByTag(route, "input")[0].dataset.modelName === "xai/grok-4.6");
  assert.ok(grok);
  const grokInput = collectByTag(grok, "input")[0];
  assert.equal(grokInput.disabled, false);
  assert.equal(grokInput.checked, true);
  assert.equal(grokInput.dataset.modelProvider, "opencode");
  assert.equal(grokInput.dataset.modelAdapter, "opencode_cli");
});

test("Models names both ingestion bounds when a provider lost rows to each", () => {
  // A provider can be cut twice over: configured catalog rows refused by the
  // catalog bound, and discovered identities refused by the discovery bound.
  // Summing them would publish one number that is true of neither population,
  // so each clause is stated against the bound that produced it.
  const families = renderModelFamilies({
    discovered_model_count: 0,
    worker_count: 3,
    returned_worker_count: 1,
    row_limit: 64,
    truncated: true,
    provider_counts: [
      {
        provider: "opencode",
        total: 20,
        ingested: 3,
        returned: 1,
        truncated: true,
        enabled_total: 3,
        enabled_counted_over: 3,
        enabled_returned: 1,
      },
    ],
    source_ingestion_loss: [
      {
        provider: "opencode",
        total: 10,
        ingested: 2,
        dropped: 8,
        absent_routes: 8,
        vendor_providers: ["xai"],
      },
    ],
    opencode_source: {
      total: 10,
      returned: 1,
      truncated: true,
      row_limit: 512,
      row_limit_honoured: 512,
      ingestion_loss: [
        {
          provider: "opencode",
          total: 10,
          ingested: 1,
          dropped: 9,
          absent_routes: 9,
          vendor_providers: ["opencode"],
        },
      ],
    },
    workers: [
      {
        provider: "opencode",
        adapter: "opencode_cli",
        model: "xai/grok-4.6",
        worker_id: "grok_opencode",
        vendor_provider: "xai",
        catalog_enabled: true,
        effective_enabled: true,
        inventory_only: false,
      },
    ],
  });

  const label = familySmallText(families[0]);
  assert.match(label, /8 not loaded \(catalog bound\) · 9 not loaded \(discovery bound\)/);
  // Not collapsed into a single 17, which would be true of no population here.
  assert.doesNotMatch(label, /17 not loaded/);
});

test("Models names the editor bridge bound's loss as its own clause", () => {
  // The editor bridge is the third bounded ingestion source and was the last
  // one still cut silently: a Copilot host reporting more identities than the
  // cap lost whichever it listed last, and the count printed beside it
  // described the survivors rather than the host. Its loss now arrives in the
  // same canonical key space as the other two, so the copilot family can say
  // which bound refused its rows instead of showing a routes-shown figure that
  // simply stops adding up.
  const families = renderModelFamilies({
    discovered_model_count: 512,
    worker_count: 512,
    returned_worker_count: 1,
    row_limit: 64,
    truncated: true,
    provider_counts: [
      {
        provider: "copilot",
        total: 600,
        ingested: 512,
        returned: 1,
        truncated: true,
        enabled_total: 512,
        enabled_counted_over: 512,
        enabled_returned: 1,
      },
    ],
    source_ingestion_loss: [],
    editor_source: {
      total: 600,
      returned: 512,
      truncated: true,
      row_limit: 512,
      row_limit_honoured: 512,
      ingestion_loss: [
        {
          provider: "copilot",
          total: 600,
          ingested: 512,
          dropped: 88,
          absent_routes: 88,
          vendor_providers: ["copilot"],
        },
      ],
    },
    workers: [
      {
        provider: "copilot",
        adapter: "vscode_lm",
        model: "copilot-model-599",
        worker_id: "",
        vendor_provider: "",
        catalog_enabled: true,
        effective_enabled: true,
        inventory_only: true,
        discovered_from_editor: true,
      },
    ],
  });

  assert.equal(families.length, 1);
  const label = familySmallText(families[0]);
  // The host's real size, not the count of identities the bound let through.
  assert.match(label, /^1 of 600 routes shown · /);
  // Named as the editor bound's loss, and not attributed to either of the
  // other two sources, which refused nothing here.
  assert.match(label, /88 not loaded \(editor bound\)/);
  assert.doesNotMatch(label, /not loaded \(catalog bound\)/);
  assert.doesNotMatch(label, /not loaded \(discovery bound\)/);

  // The identity the host listed last is drawn with a live control, which is
  // the whole point of keeping it: a route nobody can see is a route nobody
  // can switch back on.
  const routes = collectByClass(families[0], "settings-model-route");
  const input = collectByTag(routes[0], "input")[0];
  assert.equal(input.dataset.modelName, "copilot-model-599");
  assert.equal(input.disabled, false);
  assert.equal(input.checked, true);
});

test("Models counts 'not loaded' over the deduped routes, not a source's raw refusals", () => {
  // The three ingestion sources describe overlapping populations, so a bound's
  // raw drop count and the rows this tree is actually missing are two different
  // numbers. The backend already dedupes the denominator -- a discovery
  // identity the bound refused is not a missing route when the configured
  // catalog supplied the same one -- and the clause beside it was still built
  // from the raw count, so a probe that refused 90 identities of which exactly
  // one was a route nothing else carried announced "90 not loaded" next to a
  // 602-route total short by a single row. Two populations, one sentence, true
  // of neither.
  const families = renderModelFamilies({
    discovered_model_count: 512,
    opencode_discovered_model_count: 512,
    worker_count: 601,
    returned_worker_count: 2,
    row_limit: 64,
    truncated: true,
    provider_counts: [
      {
        provider: "opencode",
        total: 602,
        ingested: 601,
        returned: 2,
        truncated: true,
        enabled_total: 601,
        enabled_counted_over: 601,
        enabled_returned: 2,
      },
    ],
    source_ingestion_loss: [],
    opencode_source: {
      total: 602,
      returned: 512,
      truncated: true,
      row_limit: 512,
      row_limit_honoured: 512,
      ingestion_loss: [
        {
          provider: "opencode",
          // What the probe refused, which is a fact about the probe.
          dropped: 90,
          // How many of those are routes no other source supplied, which is
          // the only one of the two counted over the population the
          // routes-shown denominator uses.
          absent_routes: 1,
          total: 602,
          ingested: 512,
          vendor_providers: ["opencode"],
        },
      ],
    },
    workers: [
      {
        provider: "opencode",
        adapter: "opencode_cli",
        model: "opencode/model-000-free",
        worker_id: "opencode-000",
        vendor_provider: "opencode",
        catalog_enabled: true,
        effective_enabled: true,
        inventory_only: false,
      },
      {
        provider: "opencode",
        adapter: "opencode_cli",
        model: "xai/grok-4.6",
        worker_id: "grok_opencode",
        vendor_provider: "xai",
        catalog_enabled: true,
        effective_enabled: true,
        inventory_only: false,
      },
    ],
  });

  assert.equal(families.length, 1);
  const label = familySmallText(families[0]);
  // The denominator is the deduped provider size, as before.
  assert.match(label, /^2 of 602 routes shown · /);
  // And the missing-row claim is now counted over that same population: one
  // route absent, stated against the bound that refused it.
  assert.match(label, /· 1 not loaded \(discovery bound\)/);
  // The raw refusal count must not appear as a claim about this tree at all,
  // neither as itself nor summed into another clause.
  assert.doesNotMatch(label, /90 not loaded/);
  assert.doesNotMatch(label, /not loaded \(catalog bound\)/);
  assert.doesNotMatch(label, /not loaded \(editor bound\)/);

  // The route the card is about stays visible and toggleable through the
  // canonical owner the launcher consults.
  const routes = collectByClass(families[0], "settings-model-route");
  const grok = routes.find((route) => collectByTag(route, "input")[0].dataset.modelName === "xai/grok-4.6");
  assert.ok(grok);
  const grokInput = collectByTag(grok, "input")[0];
  assert.equal(grokInput.disabled, false);
  assert.equal(grokInput.checked, true);
  assert.equal(grokInput.dataset.modelProvider, "opencode");
  assert.equal(grokInput.dataset.modelAdapter, "opencode_cli");
});

test("Models draws a declared-only route as toggleable without claiming anything discovered it", () => {
  // The backend now materialises a row for a route the owner named in
  // models.json when every discovery list reaching it had already been cut
  // upstream -- otherwise an explicitly enabled route past the producer's cap
  // has no checkbox at all, which is the defect this card is about.
  //
  // Such a row is inventory_only (no worker) but nothing observed it, so the
  // tree must not borrow a discovery label for it. "discovered in VS Code"
  // over a route no VS Code host ever reported is a measurement claim the
  // payload cannot back, and it reads exactly like a verified one.
  const families = renderModelFamilies({
    discovered_model_count: 0,
    declared_only_model_count: 1,
    worker_count: 2,
    returned_worker_count: 2,
    row_limit: 64,
    truncated: false,
    provider_counts: [
      {
        provider: "opencode",
        total: 2,
        ingested: 2,
        returned: 2,
        truncated: false,
        enabled_total: 1,
        enabled_counted_over: 2,
        enabled_returned: 1,
      },
    ],
    workers: [
      {
        provider: "opencode",
        adapter: "opencode_cli",
        model: "opencode/model-000",
        worker_id: "",
        vendor_provider: "opencode",
        catalog_enabled: true,
        effective_enabled: false,
        inventory_only: true,
        discovered_from_opencode: true,
      },
      {
        provider: "opencode",
        adapter: "opencode_cli",
        model: "xai/grok-4.6",
        worker_id: "",
        vendor_provider: "opencode",
        catalog_enabled: true,
        effective_enabled: true,
        inventory_only: true,
        declared_only: true,
      },
    ],
  });

  assert.equal(families.length, 1);
  const routes = collectByClass(families[0], "settings-model-route");
  const declared = routes.find(
    (route) => collectByTag(route, "input")[0].dataset.modelName === "xai/grok-4.6",
  );
  assert.ok(declared);

  // Drawn checked and enabled: the owner's decision is reachable, which is the
  // whole point of materialising the row.
  const declaredInput = collectByTag(declared, "input")[0];
  assert.equal(declaredInput.disabled, false);
  assert.equal(declaredInput.checked, true);
  assert.equal(declaredInput.dataset.modelProvider, "opencode");
  assert.equal(declaredInput.dataset.modelAdapter, "opencode_cli");

  // And labelled for what it is. Both label sites are asserted because either
  // one alone would still print a false discovery claim beside the row.
  const smalls = hostArray(collectByClass(declared, "settings-copy")[0].children)
    .filter((child) => child.tag === "small")
    .map((child) => child.textContent);
  const subtitle = smalls[0];
  const truth = collectByClass(declared, "settings-model-truth")[0].textContent;
  assert.match(subtitle, /declared in models\.json/);
  assert.match(subtitle, /not offered by discovery/);
  assert.doesNotMatch(subtitle, /discovered/);
  assert.match(truth, /declared, not discovered/);
  assert.doesNotMatch(truth, /opencode discovery/);
  assert.doesNotMatch(truth, /editor discovery/);

  // The genuinely discovered sibling is unchanged, so the new branch did not
  // reword every inventory row on its way past.
  const found = routes.find(
    (route) => collectByTag(route, "input")[0].dataset.modelName === "opencode/model-000",
  );
  assert.match(
    collectByClass(found, "settings-model-truth")[0].textContent,
    /opencode discovery/,
  );
});

test("Models keeps a source-truncated configured route's worker and catalog truth", () => {
  // A configured route the backend's ingestion ceiling refused is brought back
  // because the owner had declared it, and it arrives with source_truncated
  // set. It is deliberately NOT declared_only: the catalog did supply this
  // route, it carries a worker_id, and it carries the "enabled": false the
  // catalog stated about it. The previous payload sent exactly this row as a
  // declared-only one, so this tree printed "declared, not discovered" and
  // "unidentified worker" over evidence the payload was holding, and drew the
  // switch as though the catalog had never turned the route off.
  const families = renderModelFamilies({
    discovered_model_count: 0,
    declared_only_model_count: 0,
    source_truncated_model_count: 1,
    worker_count: 2,
    returned_worker_count: 2,
    row_limit: 64,
    truncated: false,
    provider_counts: [
      {
        provider: "anthropic",
        total: 2,
        ingested: 2,
        returned: 2,
        truncated: false,
        enabled_total: 1,
        enabled_counted_over: 2,
        enabled_returned: 1,
      },
    ],
    workers: [
      {
        provider: "anthropic",
        adapter: "claude_cli",
        model: "anthropic-model-0000",
        worker_id: "anthropic-refused",
        vendor_provider: "anthropic",
        catalog_enabled: false,
        effective_enabled: false,
        inventory_only: false,
        source_truncated: true,
      },
      {
        provider: "anthropic",
        adapter: "claude_cli",
        model: "anthropic-model-0001",
        worker_id: "anthropic-0001",
        vendor_provider: "anthropic",
        catalog_enabled: true,
        effective_enabled: true,
        inventory_only: false,
      },
    ],
  });

  assert.equal(families.length, 1);
  const routes = collectByClass(families[0], "settings-model-route");
  const recovered = routes.find(
    (route) => collectByTag(route, "input")[0].dataset.modelName === "anthropic-model-0000",
  );
  assert.ok(recovered);

  // The worker the payload is carrying, printed rather than replaced by a
  // declaration line that would deny the catalog row exists.
  const smalls = hostArray(collectByClass(recovered, "settings-copy")[0].children)
    .filter((child) => child.tag === "small")
    .map((child) => child.textContent);
  assert.match(smalls[0], /anthropic-refused/);
  assert.doesNotMatch(smalls[0], /declared in models\.json/);
  assert.doesNotMatch(smalls[0], /unidentified worker/);

  // And named for what it is: supplied by the catalog, reached this tree only
  // past a bound that came up short. Never "declared, not discovered", which
  // would claim no source ever offered it.
  const truth = collectByClass(recovered, "settings-model-truth")[0].textContent;
  assert.match(truth, /configured, past the source bound/);
  assert.doesNotMatch(truth, /declared, not discovered/);
  assert.doesNotMatch(truth, /discovered/);

  // The catalog switched this route off, so the control says so instead of
  // offering a toggle the repository would refuse.
  const recoveredInput = collectByTag(recovered, "input")[0];
  assert.equal(recoveredInput.checked, false);
  assert.equal(recoveredInput.disabled, true);

  // An ordinary configured sibling is untouched, so the new branch did not
  // relabel every catalog row on its way past.
  const sibling = routes.find(
    (route) => collectByTag(route, "input")[0].dataset.modelName === "anthropic-model-0001",
  );
  const siblingTruth = collectByClass(sibling, "settings-model-truth")[0].textContent;
  assert.match(siblingTruth, /^configured · /);
  assert.doesNotMatch(siblingTruth, /past the source bound/);
});

test("Models states the configured routes a hard ceiling refused rather than stopping short", () => {
  // The compact bound is raised to a correctness floor, and that floor was
  // derived from models.json -- a file the extension does not control and
  // nothing bounded. One declared leaf per catalog row therefore pinned every
  // row and lifted the "bound" to the whole catalog. With a real ceiling in
  // place the opposite risk appears: routes the owner explicitly configured are
  // now absent from the row list, so they have no checkbox, and the only
  // visible symptom would be a tree that quietly stops short of its own total.
  const { families, headings } = renderModelSettings({
    discovered_model_count: 0,
    worker_count: 1025,
    returned_worker_count: 2,
    row_limit: 64,
    row_limit_honoured: 256,
    row_limit_ceiling: 256,
    pinned_routes_refused: 769,
    declared_leaf_count: 1400,
    declared_leaf_limit: 1024,
    declared_leaves_truncated: true,
    truncated: true,
    provider_counts: [
      {
        provider: "anthropic",
        total: 1024,
        ingested: 1024,
        returned: 1,
        truncated: true,
        enabled_total: 1024,
        enabled_counted_over: 1024,
        enabled_returned: 1,
      },
      {
        provider: "xai",
        total: 1,
        ingested: 1,
        returned: 1,
        truncated: false,
        enabled_total: 1,
        enabled_counted_over: 1,
        enabled_returned: 1,
      },
    ],
    source_ingestion_loss: [],
    workers: [
      {
        provider: "anthropic",
        adapter: "claude_cli",
        model: "anthropic-model-000",
        worker_id: "anthropic-000",
        catalog_enabled: true,
        effective_enabled: true,
        inventory_only: false,
      },
      {
        provider: "xai",
        adapter: "grok_kilo_cli",
        model: "grok-4.6",
        worker_id: "xai-grok",
        vendor_provider: "xai",
        catalog_enabled: true,
        effective_enabled: true,
        inventory_only: false,
      },
    ],
  });

  const warning = headingWarningText(headings);
  // Both facts, and each named for what it is: a decision the ceiling could not
  // fit is not the same failure as one the read never reached.
  assert.match(warning, /769 configured routes past the 256-row ceiling are not shown/);
  assert.match(warning, /models\.json declares 1400 routes, more than the 1024 this view reads/);
  assert.match(warning, /edit \.aiworkhub\/config\/models\.json/);

  // And the provider that sorts last is still drawn and still toggleable, which
  // is what representation-before-pins buys at the ceiling.
  assert.equal(families.length, 2);
  const xaiRoutes = collectByClass(families[1], "settings-model-route");
  const xaiInput = collectByTag(xaiRoutes[0], "input")[0];
  assert.equal(xaiInput.dataset.modelName, "grok-4.6");
  assert.equal(xaiInput.dataset.modelProvider, "xai");
  assert.equal(xaiInput.disabled, false);
  assert.equal(xaiInput.checked, true);
});

test("Models says nothing about a ceiling that never bound", () => {
  // The counterpart. A warning printed over a payload that fitted perfectly
  // well is the same defect with the opposite sign: it would tell every reader
  // their settings file is too large, on every ordinary repository.
  const { headings } = renderModelSettings({
    discovered_model_count: 0,
    worker_count: 2,
    returned_worker_count: 2,
    row_limit: 64,
    row_limit_honoured: 64,
    row_limit_ceiling: 256,
    pinned_routes_refused: 0,
    declared_leaf_count: 2,
    declared_leaf_limit: 1024,
    declared_leaves_truncated: false,
    truncated: false,
    provider_counts: [
      {
        provider: "anthropic",
        total: 2,
        ingested: 2,
        returned: 2,
        truncated: false,
        enabled_total: 2,
        enabled_counted_over: 2,
        enabled_returned: 2,
      },
    ],
    source_ingestion_loss: [],
    workers: [
      {
        provider: "anthropic",
        adapter: "claude_cli",
        model: "anthropic-model-000",
        worker_id: "anthropic-000",
        catalog_enabled: true,
        effective_enabled: true,
        inventory_only: false,
      },
      {
        provider: "anthropic",
        adapter: "claude_cli",
        model: "anthropic-model-001",
        worker_id: "anthropic-001",
        catalog_enabled: true,
        effective_enabled: true,
        inventory_only: false,
      },
    ],
  });

  assert.equal(headingWarningText(headings), "");
});

test("Models keeps a vendor's second rendered provider visible under a bounded catalog", () => {
  // One vendor spelling can span two of the providers this tree draws: an "xai"
  // vendor reaches opencode through opencode_cli and stays xai through
  // grok_kilo_cli. The backend used to spend ingestion fairness per vendor and
  // report per rendered provider, so the whole xai family could be ingested
  // away while its loss entry was filed under a family with no rows left. This
  // is the payload the fixed backend produces: both providers present, and the
  // loss named against one that is actually drawn.
  const { families } = renderModelSettings({
    discovered_model_count: 0,
    worker_count: 512,
    returned_worker_count: 2,
    row_limit: 64,
    row_limit_honoured: 64,
    row_limit_ceiling: 256,
    pinned_routes_refused: 0,
    truncated: true,
    provider_counts: [
      {
        provider: "opencode",
        total: 512,
        ingested: 511,
        returned: 1,
        truncated: true,
        enabled_total: 511,
        enabled_counted_over: 511,
        enabled_returned: 1,
      },
      {
        provider: "xai",
        total: 1,
        ingested: 1,
        returned: 1,
        truncated: false,
        enabled_total: 1,
        enabled_counted_over: 1,
        enabled_returned: 1,
      },
    ],
    source_ingestion_loss: [
      {
        provider: "opencode",
        total: 512,
        ingested: 511,
        dropped: 1,
        absent_routes: 1,
        vendor_providers: ["xai"],
      },
    ],
    workers: [
      {
        provider: "opencode",
        adapter: "opencode_cli",
        model: "xai/model-000",
        worker_id: "xai-opencode-000",
        vendor_provider: "xai",
        catalog_enabled: true,
        effective_enabled: true,
        inventory_only: false,
      },
      {
        provider: "xai",
        adapter: "grok_kilo_cli",
        model: "grok-4.6",
        worker_id: "xai-grok",
        vendor_provider: "xai",
        catalog_enabled: true,
        effective_enabled: true,
        inventory_only: false,
      },
    ],
  });

  // Two families, and the second one is the provider a vendor-only fairness
  // rule used to delete outright.
  assert.equal(families.length, 2);
  assert.match(familySmallText(families[0]), /^1 of 512 routes shown · /);
  // The OpenCode loss names the vendor underneath it, so canonical keying never
  // hides which half of the family was cut.
  assert.match(familySmallText(families[0]), /1 not loaded \(catalog bound\)/);
  assert.match(familySmallText(families[1]), /^1 route · 1 enabled · /);

  const xaiRoutes = collectByClass(families[1], "settings-model-route");
  assert.equal(xaiRoutes.length, 1);
  const xaiInput = collectByTag(xaiRoutes[0], "input")[0];
  assert.equal(xaiInput.dataset.modelName, "grok-4.6");
  assert.equal(xaiInput.dataset.modelProvider, "xai");
  assert.equal(xaiInput.disabled, false);
  assert.equal(xaiInput.checked, true);
});

test("Models states an editor-host ceiling as a floor rather than as a total", () => {
  // The bridge head-slices the host's model list at 128 and publishes no total
  // beside the slice, so the backend's 512-row ingestion bound cannot bind on
  // this source at all. While every count was taken after that slice, a host
  // offering three hundred identities rendered here as "128 of 128 routes
  // shown" -- a truncated provider drawn as complete, which is the same defect
  // the 511-of-511 case above closed, one boundary further upstream.
  //
  // The remainder is genuinely unknowable from here, so the fix is not a bigger
  // number: it is a weaker claim. The total is published as a floor and the
  // label says "at least", because inventing a count would be the same error
  // with a different value.
  const families = renderModelFamilies({
    discovered_model_count: 128,
    worker_count: 129,
    returned_worker_count: 2,
    row_limit: 64,
    truncated: true,
    provider_counts: [
      {
        provider: "copilot",
        total: 129,
        total_is_lower_bound: true,
        ingested: 129,
        returned: 2,
        truncated: true,
        enabled_total: 129,
        enabled_counted_over: 129,
        enabled_returned: 2,
      },
    ],
    editor_source: {
      total: 128,
      delivered: 128,
      upstream_refused: 0,
      upstream_ceiling: 128,
      returned: 128,
      truncated: true,
      total_is_lower_bound: true,
    },
    workers: [
      {
        provider: "copilot",
        adapter: "vscode_lm",
        model: "copilot-model-000",
        worker_id: "",
        vendor_provider: "",
        catalog_enabled: true,
        effective_enabled: true,
        inventory_only: true,
        discovered_from_editor: true,
      },
      {
        provider: "copilot",
        adapter: "vscode_lm",
        model: "copilot-model-999",
        worker_id: "",
        vendor_provider: "copilot",
        catalog_enabled: true,
        effective_enabled: true,
        inventory_only: true,
        source_truncated: true,
        upstream_truncated: true,
      },
    ],
  });

  assert.equal(families.length, 1);
  const label = familySmallText(families[0]);
  // The denominator carries its own qualification, so the number cannot be read
  // as the host's catalog size.
  assert.match(label, /^2 of at least 129 routes shown · /);
  assert.doesNotMatch(label, /^2 of 129 routes shown/);
  // "provider-wide" is a claim about a known population, and this one is not.
  assert.doesNotMatch(label, /provider-wide/);
  assert.match(label, /129 of 129 enabled among loaded routes/);
  // And the shortfall is named without a fabricated count beside it.
  assert.match(label, /more may not be loaded \(editor host bound\)/);
  assert.doesNotMatch(label, /0 not loaded/);

  // The explicitly configured route the host's slice omitted is still drawn and
  // still toggleable, which is the failure this whole card is about.
  const routes = collectByClass(families[0], "settings-model-route");
  const declared = routes.find(
    (route) => collectByTag(route, "input")[0].dataset.modelName === "copilot-model-999",
  );
  assert.ok(declared);
  const declaredInput = collectByTag(declared, "input")[0];
  assert.equal(declaredInput.disabled, false);
  assert.equal(declaredInput.checked, true);
});

test("Models keeps an exact total exact when no producer ceiling bound", () => {
  // The control for the test above, and the reason the floor is keyed off a
  // backend flag rather than applied to every provider. A source that returned
  // everything it had states a measurement, and hedging it to "at least" would
  // make the label unfalsifiable -- true of every payload and informative about
  // none of them.
  const families = renderModelFamilies({
    discovered_model_count: 40,
    worker_count: 40,
    returned_worker_count: 2,
    row_limit: 64,
    truncated: true,
    provider_counts: [
      {
        provider: "copilot",
        total: 40,
        total_is_lower_bound: false,
        ingested: 40,
        returned: 2,
        truncated: true,
        enabled_total: 40,
        enabled_counted_over: 40,
        enabled_returned: 2,
      },
    ],
    workers: [
      {
        provider: "copilot",
        adapter: "vscode_lm",
        model: "copilot-model-000",
        worker_id: "",
        vendor_provider: "",
        catalog_enabled: true,
        effective_enabled: true,
        inventory_only: true,
        discovered_from_editor: true,
      },
      {
        provider: "copilot",
        adapter: "vscode_lm",
        model: "copilot-model-001",
        worker_id: "",
        vendor_provider: "",
        catalog_enabled: true,
        effective_enabled: true,
        inventory_only: true,
        discovered_from_editor: true,
      },
    ],
  });

  const label = familySmallText(families[0]);
  assert.match(label, /^2 of 40 routes shown · /);
  assert.doesNotMatch(label, /at least/);
  assert.doesNotMatch(label, /editor host bound/);
  // A fully counted provider may still say "provider-wide", so the hedge above
  // is the exception the evidence forces and not the new default.
  assert.match(label, /40 of 40 enabled provider-wide/);
});

test("Models draws an OpenCode provider over the size its producer parsed, not the size it delivered", () => {
  // The OpenCode boundary differs from the editor one in what is recoverable:
  // the snapshot its 64-row parser read is reachable from the backend, so the
  // identities that cap refused are named, deduped against the other sources
  // and folded into the provider's size. The tree therefore gets an exact total
  // and an exact "not loaded" count here, where the editor family can only get
  // a floor -- two boundaries, two different strengths of claim, and the label
  // must not blur them into one.
  const families = renderModelFamilies({
    discovered_model_count: 64,
    worker_count: 64,
    returned_worker_count: 2,
    row_limit: 64,
    truncated: true,
    provider_counts: [
      {
        provider: "opencode",
        total: 201,
        total_is_lower_bound: false,
        ingested: 64,
        returned: 2,
        truncated: true,
        enabled_total: 64,
        enabled_counted_over: 64,
        enabled_returned: 2,
      },
    ],
    opencode_source: {
      total: 201,
      delivered: 64,
      upstream_refused: 137,
      upstream_ceiling: 64,
      returned: 64,
      truncated: true,
      total_is_lower_bound: false,
      ingestion_loss: [
        {
          provider: "opencode",
          total: 201,
          ingested: 64,
          dropped: 137,
          absent_routes: 137,
          // A strict subset of absent_routes, not a second number to add to
          // it. Here it is the whole of it: the parser cut 137 identities
          // before this backend's own bound was offered anything.
          upstream_absent_routes: 137,
          vendor_providers: ["opencode", "xai"],
        },
      ],
    },
    workers: [
      {
        provider: "opencode",
        adapter: "opencode_cli",
        model: "opencode/model-000",
        worker_id: "",
        vendor_provider: "opencode",
        catalog_enabled: true,
        effective_enabled: true,
        inventory_only: true,
        discovered_from_opencode: true,
      },
      {
        provider: "opencode",
        adapter: "opencode_cli",
        model: "xai/grok-4.6",
        worker_id: "",
        vendor_provider: "xai",
        catalog_enabled: true,
        effective_enabled: true,
        inventory_only: true,
        source_truncated: true,
        discovered_from_opencode: true,
      },
    ],
  });

  const label = familySmallText(families[0]);
  // 201 is what the host offered; 64 is what the parser handed over. Printing
  // the second as the provider's size was the understatement.
  assert.match(label, /^2 of 201 routes shown · /);
  assert.doesNotMatch(label, /2 of 64 routes shown/);
  assert.doesNotMatch(label, /at least/);
  // An exact loss, named against the bound that actually caused it. Every one
  // of the 137 was refused by the OpenCode parser's own ceiling: this backend
  // ingested all 64 identities it was handed (ingested === delivered), so its
  // discovery bound refused nothing at all. "137 not loaded (discovery bound)"
  // therefore blamed a cap that never saw the rows, and an owner raising that
  // limit would recover none of them.
  assert.match(label, /137 not loaded \(OpenCode host bound\)/);
  assert.doesNotMatch(label, /not loaded \(discovery bound\)/);

  // And xai/grok-4.6 -- the route past the producer's cut -- is drawn with a
  // live, checked control rather than missing from the tree.
  const routes = collectByClass(families[0], "settings-model-route");
  const grok = routes.find(
    (route) => collectByTag(route, "input")[0].dataset.modelName === "xai/grok-4.6",
  );
  assert.ok(grok);
  const grokInput = collectByTag(grok, "input")[0];
  assert.equal(grokInput.disabled, false);
  assert.equal(grokInput.checked, true);
});

test("Models names the OpenCode host, not the editor, when an OpenCode total is a floor", () => {
  // The floor clause carries no number, so the only thing a reader can check it
  // against is the host it names -- and it named the editor for every family.
  // An OpenCode total is only ever a floor because the OpenCode host/producer
  // cut its list above the backend, so "editor host bound" pointed the owner at
  // a cap that had never been offered these routes and cannot be raised to
  // recover them. That is the same misattribution the split "not loaded"
  // clauses fixed, restated in the one clause with no count beside it.
  const families = renderModelFamilies({
    discovered_model_count: 64,
    worker_count: 64,
    returned_worker_count: 2,
    row_limit: 64,
    truncated: true,
    provider_counts: [
      {
        provider: "opencode",
        total: 64,
        total_is_lower_bound: true,
        ingested: 64,
        returned: 2,
        truncated: true,
        enabled_total: 64,
        enabled_counted_over: 64,
        enabled_returned: 2,
      },
    ],
    // The snapshot arrived at the producer's own 64-row cap and every identity
    // in it parsed, so there is nothing to recover and nothing to count: the
    // backend publishes a floor and no loss entry at all.
    opencode_source: {
      total: 64,
      delivered: 64,
      upstream_refused: 0,
      upstream_ceiling: 64,
      returned: 64,
      truncated: true,
      total_is_lower_bound: true,
    },
    workers: [
      {
        provider: "opencode",
        adapter: "opencode_cli",
        model: "opencode/model-000",
        worker_id: "",
        vendor_provider: "opencode",
        catalog_enabled: true,
        effective_enabled: true,
        inventory_only: true,
        discovered_from_opencode: true,
      },
      {
        provider: "opencode",
        adapter: "opencode_cli",
        model: "xai/grok-4.6",
        worker_id: "",
        vendor_provider: "xai",
        catalog_enabled: true,
        effective_enabled: true,
        inventory_only: true,
        discovered_from_opencode: true,
      },
    ],
  });

  assert.equal(families.length, 1);
  const label = familySmallText(families[0]);
  // The denominator still hedges itself, as it does for the editor family.
  assert.match(label, /^2 of at least 64 routes shown · /);
  // And the clause beside it names the host that actually cut the tail.
  assert.match(label, /more may not be loaded \(OpenCode host bound\)/);
  assert.doesNotMatch(label, /editor host bound/);
  // Still no invented count: the shortfall is unknown, so none is printed.
  assert.doesNotMatch(label, /not loaded \(discovery bound\)/);
  assert.doesNotMatch(label, /0 not loaded/);

  // And the route past the producer's cut keeps a live, checked control.
  const routes = collectByClass(families[0], "settings-model-route");
  const grok = routes.find(
    (route) => collectByTag(route, "input")[0].dataset.modelName === "xai/grok-4.6",
  );
  assert.ok(grok);
  const grokInput = collectByTag(grok, "input")[0];
  assert.equal(grokInput.disabled, false);
  assert.equal(grokInput.checked, true);
});

test("Models never calls an upstream-truncated route discovered at either label site", () => {
  // The backend publishes upstream_truncated for a configured route it cannot
  // classify: the source that would have offered it was cut by its own
  // producer without saying by how much, so "a host offers this" and "no host
  // offers this" are both unmeasured. The row is still inventory_only -- it
  // has no worker -- and carries NO discovered_from_* flag.
  //
  // Both label sites used to answer that row from the inventory_only fallback
  // alone, which sits after the truncation flags in evidence strength but used
  // to sit before them in code: the subtitle said "discovered in VS Code" and
  // the truth line said "discovered", inventing exactly the measurement the
  // backend had refused to assert. Order is the fix, so both sites are pinned.
  const families = renderModelFamilies({
    discovered_model_count: 1,
    worker_count: 2,
    returned_worker_count: 2,
    row_limit: 64,
    truncated: false,
    upstream_truncated_model_count: 1,
    provider_counts: [
      {
        provider: "copilot",
        total: 2,
        ingested: 2,
        returned: 2,
        truncated: false,
        enabled_total: 2,
        enabled_counted_over: 2,
        enabled_returned: 2,
      },
    ],
    workers: [
      {
        provider: "copilot",
        adapter: "vscode_lm",
        model: "copilot-model-000",
        worker_id: "",
        vendor_provider: "copilot",
        catalog_enabled: true,
        effective_enabled: true,
        inventory_only: true,
        discovered_from_editor: true,
      },
      {
        provider: "copilot",
        adapter: "vscode_lm",
        model: "copilot-model-999",
        worker_id: "",
        vendor_provider: "copilot",
        catalog_enabled: true,
        effective_enabled: true,
        inventory_only: true,
        source_truncated: true,
        upstream_truncated: true,
      },
    ],
  });

  assert.equal(families.length, 1);
  const routes = collectByClass(families[0], "settings-model-route");
  const unknown = routes.find(
    (route) => collectByTag(route, "input")[0].dataset.modelName === "copilot-model-999",
  );
  assert.ok(unknown);

  const subtitle = hostArray(collectByClass(unknown, "settings-copy")[0].children)
    .filter((child) => child.tag === "small")
    .map((child) => child.textContent)[0];
  const truth = collectByClass(unknown, "settings-model-truth")[0].textContent;

  // Neither false discovery label, at either site.
  assert.doesNotMatch(subtitle, /discovered/);
  assert.doesNotMatch(truth, /discovered/);
  assert.doesNotMatch(subtitle, /discovered in VS Code/);
  assert.doesNotMatch(subtitle, /discovered by OpenCode/);

  // And what IS said is the bound, which is all the payload measured.
  assert.match(subtitle, /the host's list was cut before this view read it/);
  assert.match(truth, /configured, origin unknown past the host bound/);
  assert.doesNotMatch(truth, /declared, not discovered/);

  // Still the owner's control to flip, which is what materialising the row was
  // for in the first place.
  const unknownInput = collectByTag(unknown, "input")[0];
  assert.equal(unknownInput.disabled, false);
  assert.equal(unknownInput.checked, true);

  // The sibling that a host really did report keeps its discovery label, so
  // the reordering did not simply delete the claim everywhere.
  const observed = routes.find(
    (route) => collectByTag(route, "input")[0].dataset.modelName === "copilot-model-000",
  );
  assert.match(
    collectByClass(observed, "settings-model-truth")[0].textContent,
    /editor discovery/,
  );
  assert.match(
    hostArray(collectByClass(observed, "settings-copy")[0].children)
      .filter((child) => child.tag === "small")
      .map((child) => child.textContent)[0],
    /discovered in VS Code/,
  );
});

test("Models never claims discovery refused an OpenCode route its producer hid", () => {
  // The OpenCode twin of the case above, and the one that went wrong before the
  // tree ever saw it: the backend treated only vscode_lm routes as unknowable
  // under a lower-bound source, so a configured xai/grok-4.6 sitting past the
  // OpenCode parser's 64-row cap arrived as declared_only and this subtitle
  // printed "not offered by discovery" -- a measurement the OpenCode producer
  // was never in a position to make. The payload now sends the same
  // upstream_truncated pair the editor case sends, and both label sites have to
  // read it as the bound it is rather than as a verdict on the host.
  const families = renderModelFamilies({
    discovered_model_count: 1,
    declared_only_model_count: 0,
    upstream_truncated_model_count: 1,
    worker_count: 2,
    returned_worker_count: 2,
    row_limit: 64,
    truncated: true,
    provider_counts: [
      {
        provider: "opencode",
        total: 64,
        ingested: 64,
        returned: 2,
        truncated: true,
        total_is_lower_bound: true,
        enabled_total: 2,
        enabled_counted_over: 64,
        enabled_returned: 2,
      },
    ],
    workers: [
      {
        provider: "opencode",
        adapter: "opencode_cli",
        model: "opencode/model-000",
        worker_id: "",
        vendor_provider: "opencode",
        catalog_enabled: true,
        effective_enabled: true,
        inventory_only: true,
        discovered_from_opencode: true,
      },
      {
        provider: "opencode",
        adapter: "opencode_cli",
        model: "xai/grok-4.6",
        worker_id: "",
        vendor_provider: "xai",
        catalog_enabled: true,
        effective_enabled: true,
        inventory_only: true,
        source_truncated: true,
        upstream_truncated: true,
      },
    ],
  });

  assert.equal(families.length, 1);
  const routes = collectByClass(families[0], "settings-model-route");
  const grok = routes.find(
    (route) => collectByTag(route, "input")[0].dataset.modelName === "xai/grok-4.6",
  );
  assert.ok(grok);

  const subtitle = hostArray(collectByClass(grok, "settings-copy")[0].children)
    .filter((child) => child.tag === "small")
    .map((child) => child.textContent)[0];
  const truth = collectByClass(grok, "settings-model-truth")[0].textContent;

  // The claim this rework exists to remove, named exactly rather than inferred
  // from the absence of some other string.
  assert.doesNotMatch(subtitle, /not offered by discovery/);
  assert.doesNotMatch(subtitle, /declared in models\.json/);
  assert.doesNotMatch(truth, /declared, not discovered/);
  // And no discovery claim invented in its place -- the opposite error.
  assert.doesNotMatch(subtitle, /discovered/);
  assert.doesNotMatch(truth, /discovered/);

  // What IS said is the bound, which is the only thing anyone measured.
  assert.match(subtitle, /the host's list was cut before this view read it/);
  assert.match(truth, /configured, origin unknown past the host bound/);

  // Visible and toggleable, which is the pair of claims this card is about.
  const grokInput = collectByTag(grok, "input")[0];
  assert.equal(grokInput.disabled, false);
  assert.equal(grokInput.checked, true);
  assert.equal(grokInput.dataset.modelProvider, "opencode");
  assert.equal(grokInput.dataset.modelAdapter, "opencode_cli");

  // The family's own uncertainty still belongs to the OpenCode producer, so the
  // split "not loaded" clauses did not start borrowing the editor's bound.
  assert.doesNotMatch(familySmallText(families[0]), /not loaded \(editor host bound\)/);

  // The sibling OpenCode really did list keeps its discovery label, so the
  // relabelling did not simply delete the claim across the family.
  const observed = routes.find(
    (route) => collectByTag(route, "input")[0].dataset.modelName === "opencode/model-000",
  );
  assert.match(
    collectByClass(observed, "settings-model-truth")[0].textContent,
    /opencode discovery/,
  );
});

test("Models counts a producer's cap apart from this module's discovery bound", () => {
  // Both bounds lose rows here, and they are different facts with different
  // remedies: 137 identities the OpenCode parser refused before the backend was
  // handed anything, and 3 more the backend's own discovery bound then dropped.
  // Folded together the label read "140 not loaded (discovery bound)", which
  // blames one cap for another's loss -- raising the discovery bound recovers
  // three routes, not a hundred and forty.
  const families = renderModelFamilies({
    discovered_model_count: 64,
    worker_count: 205,
    returned_worker_count: 2,
    row_limit: 64,
    truncated: true,
    provider_counts: [
      {
        provider: "opencode",
        total: 205,
        total_is_lower_bound: false,
        ingested: 64,
        returned: 2,
        truncated: true,
        enabled_total: 64,
        enabled_counted_over: 64,
        enabled_returned: 2,
      },
    ],
    opencode_source: {
      total: 205,
      delivered: 68,
      upstream_refused: 137,
      upstream_ceiling: 64,
      returned: 64,
      truncated: true,
      total_is_lower_bound: false,
      ingestion_loss: [
        {
          provider: "opencode",
          total: 205,
          ingested: 64,
          dropped: 141,
          absent_routes: 140,
          upstream_absent_routes: 137,
          vendor_providers: ["opencode", "xai"],
        },
      ],
    },
    workers: [
      {
        provider: "opencode",
        adapter: "opencode_cli",
        model: "opencode/model-000",
        worker_id: "",
        vendor_provider: "opencode",
        catalog_enabled: true,
        effective_enabled: true,
        inventory_only: true,
        discovered_from_opencode: true,
      },
      {
        provider: "opencode",
        adapter: "opencode_cli",
        model: "xai/grok-4.6",
        worker_id: "",
        vendor_provider: "xai",
        catalog_enabled: true,
        effective_enabled: true,
        inventory_only: true,
        source_truncated: true,
        discovered_from_opencode: true,
      },
    ],
  });

  const label = familySmallText(families[0]);
  assert.match(label, /3 not loaded \(discovery bound\)/);
  assert.match(label, /137 not loaded \(OpenCode host bound\)/);
  // The producer's share is subtracted from this bound's clause, never added
  // beside it: 140 is the deduped population both clauses are drawn from, so
  // printing it under either one would restate the conflation.
  assert.doesNotMatch(label, /140 not loaded/);
  assert.doesNotMatch(label, /137 not loaded \(discovery bound\)/);

  // The provider-complete property this card exists for is unchanged: the
  // explicitly enabled xai route is still drawn and still toggleable.
  const routes = collectByClass(families[0], "settings-model-route");
  const grok = routes.find(
    (route) => collectByTag(route, "input")[0].dataset.modelName === "xai/grok-4.6",
  );
  assert.ok(grok);
  const grokInput = collectByTag(grok, "input")[0];
  assert.equal(grokInput.disabled, false);
  assert.equal(grokInput.checked, true);
});
