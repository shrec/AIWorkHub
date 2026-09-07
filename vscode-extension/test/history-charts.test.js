"use strict";

// The History page renders eight canonical-store series that nothing rendered
// before: 6,232 terminal events across 43 observed days, 4,628 cards, 6,511
// usage records.
//
// These tests hold the three properties that decide whether the page tells the
// truth, and each one is written so that removing the production code that
// makes it true fails the test:
//
//   1. UNKNOWN IS NOT ZERO. The canonical store has no row for 2026-07-25,
//      07-26, 07-29, 08-01 or 08-02, and no usage record at all for two more
//      days that DO carry terminal events. Every one of those must render as a
//      gap column, never as a plotted zero, and an unmeasured rate must render
//      as a word rather than a number.
//   2. COLOUR FOLLOWS THE ENTITY, NEVER ITS RANK. Filtering the page to one
//      population changes the series count; it must not repaint a survivor.
//   3. AN OMITTED FIELD IS NOT AN UNAVAILABLE ONE. The default snapshot is
//      snapshot_mode "summary" and never carries history_series; rendering
//      that as "unavailable" is NF-2026-00675, and this page must not repeat
//      it.
//
// The block under test is extracted VERBATIM from the shipped media/app.js so
// the assertions exercise the real production path, not a parallel copy.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const root = path.resolve(__dirname, "..");
const appSource = fs.readFileSync(path.join(root, "media", "app.js"), "utf8");
const cssSource = fs.readFileSync(path.join(root, "media", "app.css"), "utf8");
const extensionSource = fs.readFileSync(path.join(root, "extension.js"), "utf8");

const START = "// ═══ HISTORY_PAGE_BEGIN";
const END = "// ═══ HISTORY_PAGE_END";

function historySource() {
  const start = appSource.indexOf(START);
  const end = appSource.indexOf(END, start);
  assert.ok(start >= 0, "app.js must carry the HISTORY_PAGE_BEGIN marker");
  assert.ok(end > start, "app.js must carry the HISTORY_PAGE_END marker");
  return appSource.slice(start, end);
}

// ── A DOM small enough to reason about and real enough to catch a mistake ──
function makeElement(tag) {
  return {
    tag,
    className: "",
    textContent: "",
    title: "",
    style: {},
    attrs: {},
    children: [],
    setAttribute(name, value) { this.attrs[name] = String(value); },
    getAttribute(name) { return this.attrs[name]; },
    appendChild(child) { this.children.push(child); return child; },
    append(...nodes) { this.children.push(...nodes); },
    replaceChildren(...nodes) { this.children = nodes; },
  };
}

function walk(node, out = []) {
  if (!node || typeof node !== "object") return out;
  out.push(node);
  for (const child of node.children || []) walk(child, out);
  return out;
}

const byClass = (node, className) => walk(node).filter(
  (item) => String(item.className || "").split(/\s+/).includes(className),
);

// The production helpers the extracted block leans on, copied from app.js by
// the same extraction rule so a change to them shows up here too.
function sliceBetween(startMarker, endMarker) {
  const start = appSource.indexOf(startMarker);
  assert.ok(start >= 0, `app.js must declare ${startMarker}`);
  const end = appSource.indexOf(endMarker, start);
  assert.ok(end > start, `app.js must still declare ${endMarker} after ${startMarker}`);
  return appSource.slice(start, end);
}

function loadHistory(overrides = {}) {
  const helpers = [
    sliceBetween("const NO_MEASUREMENT_STATES = new Set([", "function measuredCount("),
    sliceBetween("function formatCount(value)", "function formatRelativeTime("),
  ].join("\n");
  const harness = `
    "use strict";
    function numberValue(value) {
      const parsed = Number(value);
      return Number.isFinite(parsed) ? parsed : 0;
    }
    function asArray(value) { return Array.isArray(value) ? value : []; }
    function createElement(tag, className, text) {
      const element = document.createElement(tag);
      if (className) { element.className = className; }
      if (text !== undefined && text !== null) { element.textContent = String(text); }
      return element;
    }
    ${helpers}
    ${historySource()}
    api.snapshotFieldState = snapshotFieldState;
    api.applyHistorySnapshot = applyHistorySnapshot;
    api.renderHistoryPage = renderHistoryPage;
    api.historyDayKeys = historyDayKeys;
    api.historyEntityClass = historyEntityClass;
    api.historyDailyOutcomesPanel = historyDailyOutcomesPanel;
    api.historyDecisionsPanel = historyDecisionsPanel;
    api.historyTokensByDayPanel = historyTokensByDayPanel;
    api.historyRiskPanel = historyRiskPanel;
    api.historyLatencyPanel = historyLatencyPanel;
    api.historyRunnerPanel = historyRunnerPanel;
    api.historyOverviewPanel = historyOverviewPanel;
    api.HISTORY_SLOT_STATES = HISTORY_SLOT_STATES;
  `;
  const api = {};
  const body = makeElement("div");
  const context = {
    api,
    document: { createElement: makeElement },
    Intl,
    Date,
    Number,
    Math,
    Object,
    Array,
    Map,
    Set,
    String,
    state: { historySeries: null, historyState: "pending", ...(overrides.state || {}) },
    elements: {
      historyBody: body,
      historyDialog: { open: false },
      historyPopulation: { value: overrides.population || "all" },
      headerHistoryValue: makeElement("strong"),
      headerHistoryDetail: makeElement("span"),
      ...(overrides.elements || {}),
    },
  };
  vm.runInNewContext(harness, context);
  return { api, body, context };
}

// ── A fixture with the measured shape, including its real holes ────────────
const WINDOW = { days: 180, since: "2026-03-11", observed_days: 5, first_day: "2026-07-23", last_day: "2026-07-29", truncated: false };

function fixture() {
  return {
    schema_id: "aiworkhub.dashboard.history_series.v1",
    measured: true,
    absent_metrics_are_unknown_not_zero: true,
    baseline_substatus: "review_ready",
    excluded_topics: ["quality_review"],
    population_note: "note",
    resolution_note: "resolution",
    window: WINDOW,
    query_cost: { measured: true, total_ms: 2900.9, query_count: 7 },
    terminal_composition: {
      measured: true,
      events: 116,
      baseline: { substatus: "review_ready", events: 23, share: 0.198 },
      failures: {
        events: 93,
        share: 0.802,
        by_substatus: { validation_failed: 59, launch_failed: 15, worker_failed: 5, timed_out: 6, cancelled: 4, scope_rejected: 1, liveness_lost: 3 },
      },
      by_population: {
        work_card: { baseline: 21, failures: { validation_failed: 59, launch_failed: 15, timed_out: 6, cancelled: 4, scope_rejected: 1 } },
        reviewer_child: { baseline: 2, failures: { worker_failed: 5, liveness_lost: 3 } },
        unknown_topic: { baseline: 0, failures: {} },
      },
      evidence_cause_by_substatus: {
        review_ready: { nothing_measured: 10, evidence_measured: 8, verdict_absent: 5 },
        validation_failed: { verdict_absent: 30, evidence_measured: 20, nothing_measured: 9 },
      },
      evidence_cause_legend: {
        nothing_measured: "the deterministic evidence verdict ran and measured nothing",
        evidence_measured: "the verdict measured at least one signal",
        verdict_absent: "no deterministic verdict was recorded on the event; unknown, not a pass and not a failure",
      },
    },
    // 2026-07-23, -24, -27 and -28 only: -25, -26 and -29 have NO ROW.
    daily_outcomes: {
      measured: true,
      events: 116,
      day_count: 4,
      truncated: false,
      days: [
        { day: "2026-07-23", total: 6, baseline: { work_card: 0, reviewer_child: 0, unknown_topic: 0 }, failures: { work_card: { cancelled: 1, launch_failed: 1, validation_failed: 4 }, reviewer_child: {}, unknown_topic: {} } },
        { day: "2026-07-24", total: 6, baseline: { work_card: 2, reviewer_child: 0, unknown_topic: 0 }, failures: { work_card: { launch_failed: 1, validation_failed: 2 }, reviewer_child: { worker_failed: 1 }, unknown_topic: {} } },
        { day: "2026-07-27", total: 100, baseline: { work_card: 19, reviewer_child: 2, unknown_topic: 0 }, failures: { work_card: { validation_failed: 53, launch_failed: 13, timed_out: 6, scope_rejected: 1 }, reviewer_child: { worker_failed: 4, liveness_lost: 3 }, unknown_topic: {} } },
        { day: "2026-07-28", total: 4, baseline: { work_card: 0, reviewer_child: 0, unknown_topic: 0 }, failures: { work_card: { cancelled: 3 }, reviewer_child: {}, unknown_topic: {} } },
      ],
    },
    // 2026-07-28 has terminal events but NO DECISION ROW.
    daily_decisions: {
      measured: true,
      counting_note: "one vote per distinct card per day",
      day_count: 3,
      truncated: false,
      totals: {
        work_card: { accepted: 13, rejected: 26, decided: 39, acceptance_rate: 0.3333 },
        reviewer_child: { accepted: 2, rejected: 0, decided: 2, acceptance_rate: 1 },
        unknown_topic: { accepted: 0, rejected: 0, decided: 0, acceptance_rate: null },
      },
      days: [
        { day: "2026-07-23", work_card: { accepted: 0, rejected: 3 }, reviewer_child: { accepted: 0, rejected: 0 }, unknown_topic: { accepted: 0, rejected: 0 } },
        { day: "2026-07-24", work_card: { accepted: 1, rejected: 0 }, reviewer_child: { accepted: 0, rejected: 0 }, unknown_topic: { accepted: 0, rejected: 0 } },
        { day: "2026-07-27", work_card: { accepted: 12, rejected: 23 }, reviewer_child: { accepted: 2, rejected: 0 }, unknown_topic: { accepted: 0, rejected: 0 } },
      ],
    },
    // A usage record exists only for two of the seven days in the span.
    usage: {
      measured: true,
      records: 9,
      total_tokens: 8020898,
      day_count: 2,
      truncated: false,
      by_day: [
        { day: "2026-07-23", records: 5, total_tokens: 4280587, cost_observed_records: 0, cost_usd_observed: 0, zero_token_records: 0, cost_coverage: 0, cost_is_lower_bound: true },
        { day: "2026-07-24", records: 4, total_tokens: 3740311, cost_observed_records: 0, cost_usd_observed: 0, zero_token_records: 0, cost_coverage: 0, cost_is_lower_bound: true },
      ],
      by_model: { "claude-sonnet-5": { records: 5, total_tokens: 4280587, cost_observed_records: 4, cost_usd_observed: 10.5, cost_coverage: 0.8, cost_is_lower_bound: true } },
      by_role: {},
      cost_quality: { cost_observed_records: 4, cost_unknown_records: 5, coverage: 0.44, cost_usd_observed: 10.5, cost_usd_total: null, zero_cost_is_free: false },
      telemetry_absence: {
        zero_token_records: 3,
        share_of_records: 0.333,
        by_adapter: [{ adapter_id: "unknown_adapter", provider: "glm_vscode_lm", records: 3, zero_token_records: 3, zero_token_share: 1 }],
        note: "records whose transport reported no token telemetry at all",
      },
    },
    retry_economics: {
      measured: true,
      attempts: { first_attempt: { records: 5, tasks: 5, total_tokens: 10 }, retry: { records: 4, tasks: 2, total_tokens: 6 }, records: 9, total_tokens: 16 },
      retry_share: { of_tokens: 0.375, of_records: 0.444 },
      rejection_depth: {
        counting_unit: "distinct_card",
        note: "cards keyed by how many times they were rejected",
        by_population: {
          work_card: {
            0: { cards: 13, eventually_accepted: 13, eventual_acceptance_rate: 1 },
            1: { cards: 20, eventually_accepted: 3, eventual_acceptance_rate: 0.15 },
            // Depth 2 is absent on purpose; depth 3 carries an unmeasured rate.
            3: { cards: 2, eventually_accepted: null, eventual_acceptance_rate: null },
          },
          reviewer_child: { 0: { cards: 2, eventually_accepted: 2, eventual_acceptance_rate: 1 } },
          unknown_topic: {},
        },
      },
    },
    model_outcomes: {
      measured: true,
      sample_note: "acceptance_rate is unknown, not zero, when a runner has decided no cards",
      work_card: {
        runner_count: 2,
        truncated: false,
        runners: [
          { runner: "codex_gpt-5.6-sol", accepted: 9, rejected: 21, decided: 30, sample_count: 30, acceptance_rate: 0.3 },
          { runner: "glm_5.3", accepted: 0, rejected: 9, decided: 9, sample_count: 9, acceptance_rate: 0 },
        ],
      },
      reviewer_child: { runner_count: 1, truncated: false, runners: [{ runner: "codex_cli", accepted: 2, rejected: 0, decided: 2, sample_count: 2, acceptance_rate: 1 }] },
      unknown_topic: { runner_count: 0, truncated: false, runners: [] },
    },
    latency: {
      measured: true,
      cards: 41,
      queue_latency: {
        definition: "created_at -> started_at",
        by_population: {
          work_card: { measured: true, samples: 14, p50_seconds: 224, p90_seconds: 8606, p95_seconds: 25563, max_seconds: 307237 },
          reviewer_child: { measured: true, samples: 2, p50_seconds: 1, p90_seconds: 108, p95_seconds: 247, max_seconds: 9350 },
          unknown_topic: { measured: false, reason: "no_observed_durations", samples: 0, p50_seconds: null, p90_seconds: null, p95_seconds: null, max_seconds: null },
        },
      },
      run_latency: {
        definition: "started_at -> completed_at",
        by_population: {
          work_card: { measured: true, samples: 13, p50_seconds: 240, p90_seconds: 753, p95_seconds: 1060, max_seconds: 5404 },
          reviewer_child: { measured: true, samples: 2, p50_seconds: 199, p90_seconds: 534, p95_seconds: 815, max_seconds: 86018 },
          unknown_topic: { measured: false, reason: "no_observed_durations", samples: 0, p50_seconds: null, p90_seconds: null, p95_seconds: null, max_seconds: null },
        },
      },
      anomalies: { negative_queue: 0, negative_run: 36, note: "durations where the later timestamp precedes the earlier one" },
    },
    risk_tier_distribution: {
      measured: true,
      cards: 41,
      by_population: {
        work_card: { cards: 30, risk_tier_known: 18, risk_tier_unknown: 12, coverage: 0.6, tiers: { high: 12, medium: 3, critical: 2, low: 1 } },
        reviewer_child: { cards: 11, risk_tier_known: 0, risk_tier_unknown: 11, coverage: 0, tiers: {} },
        unknown_topic: { cards: 0, risk_tier_known: 0, risk_tier_unknown: 0, coverage: null, tiers: {} },
      },
      coverage_note: "risk_tier is derived at card creation",
    },
  };
}

// ═══ 1. Unknown is not zero ═══════════════════════════════════════════════

test("a day the store has no row for renders as a gap, never as a zero column", () => {
  const { api } = loadHistory({ population: "all" });
  const panel = api.historyDailyOutcomesPanel(fixture(), "all");
  const columns = byClass(panel, "history-column");

  // Seven calendar days in the span 07-23 .. 07-29, not four observed rows
  // squeezed together.
  assert.equal(columns.length, 7, "the axis must be continuous across the observed span");

  const gaps = columns.filter((column) => String(column.className).includes("is-gap"));
  assert.deepEqual(
    gaps.map((column) => column.attrs["aria-label"]),
    [
      "2026-07-25: not measured",
      "2026-07-26: not measured",
      "2026-07-29: not measured",
    ],
    "every day with no row must be named and marked as not measured",
  );

  // The load-bearing half: a gap must carry NO segment at all. A zero-height
  // segment is still a plotted zero.
  for (const gap of gaps) {
    assert.equal(byClass(gap, "history-segment").length, 0,
      "a gap column must plot nothing; a zero-height mark still reads as a measured zero");
    assert.doesNotMatch(String(gap.title), /\b0\b/,
      "a gap's tooltip must say a word, not a digit");
  }

  // And a measured day must still plot.
  const measured = columns.filter((column) => !String(column.className).includes("is-gap"));
  assert.equal(measured.length, 4);
  assert.ok(byClass(measured[0], "history-segment").length > 0);
});

test("the gap is visible in the stylesheet, not only in the accessible name", () => {
  // A screen-reader-only gap is not a rendered gap. The axis segment under a
  // gap column must be dashed, and the fill of an unknown must be no fill.
  assert.match(cssSource, /\.history-column\.is-gap \.history-stack \{[^}]*border-bottom-style: dashed/);
  assert.match(cssSource, /\.history-diverging-column\.is-gap \.history-diverging-down \{[^}]*border-top-style: dashed/);
  assert.match(cssSource, /\.history-row\.is-unknown \.history-row-track \{[^}]*background: transparent[^}]*border: 1px dashed/);
  assert.match(cssSource, /\.entity-unknown \{ --history-fill: transparent; \}/,
    "the unknown entity must resolve to the absence of a fill, not to a colour");
});

test("a usage series with fewer days than the outcome series keeps its own gaps", () => {
  // The two series measure different things: a day with terminal events but no
  // usage record is not a day of zero tokens. Five of the seven days in this
  // span have no usage record.
  const { api } = loadHistory();
  const panel = api.historyTokensByDayPanel(fixture());
  const columns = byClass(panel, "history-column");
  assert.equal(columns.length, 7);
  const gaps = columns.filter((column) => String(column.className).includes("is-gap"));
  assert.equal(gaps.length, 5, "only 2026-07-23 and 07-24 carry a usage record");
  assert.ok(gaps.every((gap) => byClass(gap, "history-segment").length === 0));
});

test("a day with terminal events but no decision is a gap in the decisions chart", () => {
  const { api } = loadHistory();
  const panel = api.historyDecisionsPanel(fixture(), "work_card");
  const columns = byClass(panel, "history-diverging-column");
  const gaps = columns.filter((column) => String(column.className).includes("is-gap"));
  assert.ok(gaps.some((gap) => gap.attrs["aria-label"] === "2026-07-28: not measured"),
    "2026-07-28 has four terminal events and no decision; the decisions chart must not draw it as 0/0");
  for (const gap of gaps) {
    assert.equal(byClass(gap, "history-diverging-bar").length, 0);
  }
});

test("an unmeasured rate renders as a word and never as a number", () => {
  const { api } = loadHistory();

  // A population that decided no cards reports acceptance_rate: null.
  // numberValue() would coerce that to 0 and print "0.0%", which says the
  // opposite of what was measured.
  const empty = fixture();
  empty.daily_decisions.totals.work_card = { accepted: 0, rejected: 0, decided: 0, acceptance_rate: null };
  const overview = api.historyOverviewPanel(empty);
  const values = byClass(overview, "history-stat-value").map((node) => node.textContent);
  assert.ok(values.includes("not measured"),
    `an unmeasured stat must print the words "not measured"; got ${JSON.stringify(values)}`);
  assert.ok(
    byClass(api.historyOverviewPanel(fixture()), "history-stat-value").some((node) => node.textContent === "33.3%"),
    "a measured rate must still print as a number",
  );

  // The same rule inside a percentile panel that reports measured: false.
  const latency = api.historyLatencyPanel(fixture(), "unknown_topic", "queue_latency", "Queue latency");
  const unknownRows = byClass(latency, "history-row").filter((row) => String(row.className).includes("is-unknown"));
  assert.equal(unknownRows.length, 1);
  assert.equal(unknownRows[0].children[0].children[1].textContent, "not measured");
  assert.equal(byClass(unknownRows[0], "history-row-fill").length, 0,
    "an unmeasured percentile block must draw no fill");
});

test("a rejection depth whose eventual acceptance was never measured draws no bar", () => {
  const { api, body } = loadHistory({
    state: { historySeries: fixture(), historyState: "present" },
    population: "work_card",
  });
  api.renderHistoryPage();
  const panel = byClass(body, "history-panel").find(
    (node) => byClass(node, "history-panel-title")[0].textContent === "Rejection depth · Work cards",
  );
  assert.ok(panel);
  // Depth 3 carries cards: 2 with eventual_acceptance_rate: null. The card
  // count is measured and draws; the rate is not and does not.
  const unknown = byClass(panel, "history-row").filter((row) => String(row.className).includes("is-unknown"));
  assert.equal(unknown.length, 1, "exactly one row in this fixture has an unmeasured rate");
  assert.equal(unknown[0].children[0].children[1].textContent, "not measured");
  assert.equal(byClass(unknown[0], "history-row-fill").length, 0);
  // Depth 2 has no cards at all and is simply not a row: the query enumerates
  // every card, so an absent depth is a real zero and inventing a row for it
  // would be the mirror-image lie.
  const labels = byClass(panel, "history-row-label").map((node) => node.textContent);
  assert.deepEqual(labels, [
    "0 rejections", "eventually accepted",
    "1 rejection", "eventually accepted",
    "3 rejections", "eventually accepted",
  ]);
});

test("the cost total stays unknown and the observed sum is labelled a lower bound", () => {
  const { api } = loadHistory();
  const overview = api.historyOverviewPanel(fixture());
  const tile = byClass(overview, "history-stat").find(
    (node) => byClass(node, "history-stat-label")[0].textContent === "Cost observed",
  );
  assert.ok(tile, "the overview must carry a cost tile");
  const detail = byClass(tile, "history-stat-detail")[0].textContent;
  assert.match(detail, /lower bound/);
  assert.match(detail, /total not measured/,
    "cost_usd_total is null in the payload; printing the observed sum as the total understates it");
});

test("cards whose risk tier was never recorded stay in the panel as an unknown", () => {
  const { api } = loadHistory();
  const panel = api.historyRiskPanel(fixture(), "work_card");
  const labels = byClass(panel, "history-row-label").map((node) => node.textContent);
  assert.deepEqual(labels, ["critical", "high", "medium", "low", "no tier recorded"],
    "tiers run in severity order and the untiered cards are a row, not an omission");
  const unknownFill = byClass(panel, "history-row-fill").find(
    (node) => String(node.className).includes("entity-unknown"),
  );
  assert.ok(unknownFill, "the untiered band must wear the unknown treatment, not a tier colour");
});

// ═══ 2. Colour follows the entity, never its rank ═════════════════════════

test("filtering to one population does not repaint the survivors", () => {
  const { api } = loadHistory();
  const series = fixture();

  const classOf = (panel, label) => {
    const item = byClass(panel, "history-legend-item").find(
      (node) => byClass(node, "history-legend-name")[0].textContent === label,
    );
    return item ? String(item.className).split(/\s+/).find((name) => name.startsWith("entity-")) : null;
  };

  const all = api.historyDailyOutcomesPanel(series, "all");
  const workOnly = api.historyDailyOutcomesPanel(series, "work_card");

  // "all" draws worker_failed and liveness_lost as well; "work_card" drops
  // them, so the series count changes and the rank of everything below the cut
  // changes with it.
  assert.ok(classOf(all, "worker failed"), "the unfiltered panel must include the reviewer-child failures");
  assert.equal(classOf(workOnly, "worker failed"), null, "the filter must actually drop a series");

  for (const label of ["review ready", "validation failed", "launch failed", "cancelled"]) {
    assert.equal(classOf(all, label), classOf(workOnly, label),
      `${label} changed colour when the series count changed; colour must follow the entity, not its rank`);
  }
  assert.equal(classOf(all, "review ready"), "entity-review_ready");
  assert.equal(classOf(all, "validation failed"), "entity-validation_failed");
});

test("no fill is derived from a position, and every fill is a theme token", () => {
  const source = historySource();
  assert.doesNotMatch(source, /137\.508/, "the golden-angle formula must never come back");
  assert.doesNotMatch(source, /const hue =|hsl\(/, "no hue may be computed on this page");
  assert.doesNotMatch(source, /\.style\.background\s*=/,
    "no inline colour may be written; the fill must stay a class over a theme token");
  // The page's whole fill vocabulary, declared once and keyed by entity.
  for (const entity of [
    "review_ready", "decided", "validation_failed", "worker_failed",
    "run_failed", "blocked", "other",
  ]) {
    assert.match(cssSource, new RegExp(`\\.entity-${entity} \\{ --history-fill: var\\(--outcome-[a-z0-9-]+\\); \\}`),
      `entity-${entity} must resolve to one of the validated outcome tokens`);
  }
});

test("the page's slot map cannot drift from the KPI panel's", () => {
  // The KPI map is function-scoped inside renderKpis(); the History page keeps
  // its own declaration. This test is what makes that a checked mirror rather
  // than a second source of truth.
  const block = appSource.match(/const OUTCOME_SLOTS = \[([\s\S]*?)\n  \];/);
  assert.ok(block, "app.js must still declare OUTCOME_SLOTS");
  const kpi = new Map();
  const re = /\["([a-z_]+)",\s*"[^"]+",\s*\[([^\]]*)\]\]/g;
  let match;
  while ((match = re.exec(block[1])) !== null) {
    kpi.set(match[1], match[2].split(",").map((s) => s.trim().replace(/^"|"$/g, "")).filter(Boolean));
  }
  const { api } = loadHistory();
  // The block runs in its own realm, so its arrays are copied onto host ones
  // before comparison; the values are what is under test, not the constructor.
  const history = new Map([...api.HISTORY_SLOT_STATES].map(([slot, states]) => [slot, [...states].map(String)]));
  assert.deepEqual([...history.keys()], [...kpi.keys()], "both maps must carry the same slots in the same order");
  for (const [slot, states] of kpi) {
    assert.deepEqual(history.get(slot), states, `slot ${slot} disagrees between the KPI panel and the History page`);
  }
});

test("a review rejection takes the decision colour, not the machine-failure colour", () => {
  const { api } = loadHistory();
  assert.equal(api.historyEntityClass("rejected"), "entity-decided",
    "red is reserved for the run breaking; a manager rejecting a card is a decision");
  assert.equal(api.historyEntityClass("accepted"), "entity-review_ready");
  assert.equal(api.historyEntityClass("verdict_absent"), "entity-unknown",
    "the payload's own legend calls verdict_absent unknown, not a pass and not a failure");
  assert.equal(api.historyEntityClass("some_new_thing_failed"), "entity-run_failed");
  assert.equal(api.historyEntityClass("something_else"), "entity-other",
    "an unrecognised entity folds into neutral rather than being guessed into an alarm colour");
});

test("two or more series always get a legend and up to four are direct-labelled", () => {
  const { api } = loadHistory();
  const series = fixture();

  const decisions = api.historyDecisionsPanel(series, "work_card");
  assert.equal(byClass(decisions, "history-legend-item").length, 2, "two series must be legended");
  assert.equal(byClass(decisions, "history-axis-label").length, 2, "two series must also be direct-labelled");

  const tokens = api.historyTokensByDayPanel(series);
  assert.equal(byClass(tokens, "history-legend-item").length, 0,
    "one series needs no legend; the panel title names it");
});

// ═══ 3. An omitted field is not an unavailable one (NF-2026-00675) ════════

test("a field the summary snapshot never carries reads as not-yet-loaded", () => {
  const { api } = loadHistory();
  // The live summary payload: snapshot_mode "summary", full_snapshot_available
  // true, and -- measured against the running server -- history_series absent
  // from omitted_fields as well as from the payload.
  const summary = {
    snapshot_mode: "summary",
    full_snapshot_available: true,
    omitted_fields: ["development_rules", "skills", "tool_recipes"],
    status_counts: {},
  };
  assert.equal(api.snapshotFieldState(summary, "history_series"), "pending",
    "snapshot_mode is the authority; a field missing from omitted_fields is still not-yet-sent");
  assert.equal(api.snapshotFieldState(summary, "development_rules"), "pending");
  assert.equal(api.snapshotFieldState({ snapshot_mode: "full", history_series: fixture() }, "history_series"), "present");
  assert.equal(api.snapshotFieldState({ snapshot_mode: "full" }, "history_series"), "absent",
    "a full snapshot that genuinely carries none is the only case that is absent");
  assert.equal(api.snapshotFieldState(null, "history_series"), "pending",
    "before any snapshot arrives, nothing has been measured either way");
});

test("the page says loading for an omitted field and never says unavailable", () => {
  const { api, body, context } = loadHistory();
  api.applyHistorySnapshot({ snapshot_mode: "summary", full_snapshot_available: true, omitted_fields: [] });
  assert.equal(context.state.historyState, "pending");
  api.renderHistoryPage();
  const text = body.children.map((node) => node.textContent).join(" ");
  assert.match(text, /Loading the full snapshot/);
  assert.doesNotMatch(text, /[Uu]navailable/,
    "NF-2026-00675: a field the server deliberately did not send must not assert an unavailability nobody measured");
  assert.doesNotMatch(text, /No evidence/);

  // And the header card says the same thing.
  assert.equal(context.elements.headerHistoryValue.textContent, "Loading");
  assert.match(context.elements.headerHistoryDetail.textContent, /not carried by the summary snapshot/);
});

test("a summary refresh never blanks a page the full snapshot already filled", () => {
  const { api, context } = loadHistory();
  api.applyHistorySnapshot({ snapshot_mode: "full", history_series: fixture() });
  assert.equal(context.state.historyState, "present");
  // The 30-second refresh posts the summary first. It must not erase the last
  // measured series.
  api.applyHistorySnapshot({ snapshot_mode: "summary", full_snapshot_available: true, omitted_fields: [] });
  assert.equal(context.state.historyState, "present");
  assert.ok(context.state.historySeries, "the last full snapshot must survive a summary refresh");
  assert.equal(context.elements.headerHistoryValue.textContent, "5 days");
});

test("an unmeasured series says so with its reason and draws nothing", () => {
  const { api, body } = loadHistory();
  api.applyHistorySnapshot({
    snapshot_mode: "full",
    history_series: { measured: false, reason: "storage_not_ready", absent_metrics_are_unknown_not_zero: true },
  });
  api.renderHistoryPage();
  const text = body.children.map((node) => node.textContent).join(" ");
  assert.match(text, /not measured: storage_not_ready/);
  assert.equal(byClass(body, "history-panel").length, 0,
    "an unmeasured history must draw no panels rather than a history of zeroes");
});

// ═══ The page itself ══════════════════════════════════════════════════════

test("the page renders every one of the eight series", () => {
  const { api, body } = loadHistory({
    state: { historySeries: fixture(), historyState: "present" },
    population: "work_card",
  });
  api.renderHistoryPage();
  const titles = byClass(body, "history-panel-title").map((node) => node.textContent);
  for (const expected of [
    "Failure modes",                                  // terminal_composition
    "Evidence verdict coverage",                      // terminal_composition
    "Terminal outcomes by day",                       // daily_outcomes
    "Decisions by day",                               // daily_decisions
    "Acceptance by runner · Work cards",              // model_outcomes
    "Rejection depth · Work cards",                   // retry_economics
    "Tokens by day",                                  // usage
    "Transports reporting no token telemetry",        // usage
    "Tokens by model",                                // usage
    "Queue latency · Work cards",                     // latency
    "Run latency · Work cards",                       // latency
    "Risk tier coverage · Work cards",                // risk_tier_distribution
  ]) {
    assert.ok(titles.includes(expected), `the page must render "${expected}"; got ${JSON.stringify(titles)}`);
  }
});

test("the failure taxonomy is uncapped and the baseline gets one line", () => {
  const { api, body } = loadHistory({
    state: { historySeries: fixture(), historyState: "present" },
    population: "all",
  });
  api.renderHistoryPage();
  const failurePanel = byClass(body, "history-panel").find(
    (node) => byClass(node, "history-panel-title")[0].textContent === "Failure modes",
  );
  const rows = byClass(failurePanel, "history-row-label").map((node) => node.textContent);
  assert.deepEqual(rows, [
    "validation failed", "launch failed", "timed out", "worker failed",
    "cancelled", "liveness lost", "scope rejected",
  ], "every failure class keeps its own row, ranked by count, with nothing cut from the rare end");
  // Success collapses to one footer line and is never subdivided.
  const footers = byClass(failurePanel, "history-footer");
  assert.equal(footers.length, 1);
  assert.equal(byClass(failurePanel, "history-row").length, 7,
    "the baseline must not take a bar row of its own");
});

test("the page renders on open, not on every snapshot", () => {
  // Twelve panels over 43 days and 6,232 events is real DOM work; a 30-second
  // refresh must not pay for it while the page is shut.
  assert.match(appSource, /function openHistoryDialog\(\) \{[\s\S]*?renderHistoryPage\(\);/,
    "opening the dialog must be what builds the page");
  assert.match(appSource, /if \(elements\.historyDialog && elements\.historyDialog\.open\) renderHistoryPage\(\);/,
    "a snapshot may only re-render the page while it is already open");
  const render = appSource.match(/function renderSnapshot\(snapshot\) \{([\s\S]*?)\n\}/);
  assert.ok(render);
  assert.doesNotMatch(render[1], /renderHistoryPage\(/,
    "renderSnapshot must not build the charts page; the snapshot render cannot wait on it");
  assert.match(render[1], /applyHistorySnapshot\(snapshot\);/);
});

// ═══ Layout and markup ════════════════════════════════════════════════════

test("the history dialog is reachable from the dashboard and closes like the others", () => {
  assert.match(extensionSource, /id="open-history"/, "the diagnostics strip must carry a History button");
  assert.match(extensionSource, /id="header-history"/, "the header must carry a History card");
  assert.match(extensionSource, /<dialog class="diagnostic-dialog history-dialog" id="history-dialog">/);
  assert.match(extensionSource, /data-close-dialog="history-dialog"/);
  assert.match(extensionSource, /id="history-body"/);
  assert.match(extensionSource, /id="history-population"/);
});

test("the page has exactly one scroll region and it cannot spawn a second", () => {
  // The model selector "creates a mass of scrollbars and then everything jumps"
  // was a nested scroll chain leaking its overflow into the <dialog>. The
  // history body reserves its gutter and contains its paint so it can never do
  // that, and nothing inside it scrolls on its own.
  const css = cssSource.replace(/\/\*[\s\S]*?\*\//g, "");
  const historyRules = [];
  const re = /(^|[\n}])\s*([^{}@][^{}]*?)\{([^{}]*)\}/g;
  let match;
  while ((match = re.exec(css)) !== null) {
    const selector = match[2].trim().replace(/\s+/g, " ");
    if (selector.includes(".history-")) historyRules.push({ selector, body: match[3] });
  }
  const scrollers = historyRules.filter((rule) => /overflow(-x|-y)?:\s*(auto|scroll)/.test(rule.body));
  assert.deepEqual(scrollers.map((rule) => rule.selector), [".history-body"],
    "the history body must be the page's only scroll region");
  assert.match(scrollers[0].body, /contain: paint/);
  assert.match(scrollers[0].body, /overscroll-behavior: contain/);
  assert.match(scrollers[0].body, /scrollbar-gutter: stable/);

  // Wide content sizes intrinsically instead of forcing the body sideways.
  const grid = historyRules.find((rule) => rule.selector === ".history-grid");
  assert.ok(grid);
  assert.match(grid.body, /grid-template-columns: repeat\(auto-fit, minmax\(min\(100%,/);
  const strip = historyRules.find((rule) => rule.selector === ".history-stat-strip");
  assert.ok(strip);
  assert.match(strip.body, /grid-template-columns: repeat\(auto-fit, minmax\(min\(100%,/);
});

test("every history font size comes from the type scale", () => {
  const css = cssSource.replace(/\/\*[\s\S]*?\*\//g, "");
  const start = css.indexOf(".entity-review_ready");
  assert.ok(start > 0);
  for (const match of css.slice(start).matchAll(/font-size:\s*([^;]+);/g)) {
    assert.match(match[1].trim(), /^var\(--fs-[a-z0-9]+\)$/,
      `the history page must size type from the scale, not from a literal: ${match[1]}`);
  }
});
