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
