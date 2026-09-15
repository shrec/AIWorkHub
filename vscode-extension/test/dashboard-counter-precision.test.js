"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const dashboardSource = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "aiworkhub", "dashboard_static", "dashboard.js"),
  "utf8",
);

// Extracts a verbatim block of the shipped dashboard script so the assertions
// below exercise the real production formatter rather than a parallel
// test-only reimplementation of it.
function extractRange(source, startMarker, tailMarker) {
  const start = source.indexOf(startMarker);
  assert.notEqual(start, -1, `snippet start marker not found in dashboard.js: ${startMarker}`);
  const tail = source.indexOf(tailMarker, start);
  assert.notEqual(tail, -1, `snippet tail marker not found in dashboard.js: ${tailMarker}`);
  const end = source.indexOf("\n}\n", tail);
  assert.notEqual(end, -1, `snippet end not found in dashboard.js: ${tailMarker}`);
  return source.slice(start, end + 2);
}

const FORMATTER_SNIPPET = extractRange(
  dashboardSource,
  "function numberValue(value) {",
  "function formatCount(value, locale) {",
);

const SUMMARY_SNIPPET = extractRange(
  dashboardSource,
  "function renderSummary(snapshot) {",
  "function renderSummary(snapshot) {",
);

function loadFormatter(language) {
  const context = { navigator: language === undefined ? undefined : { language } };
  vm.createContext(context);
  vm.runInContext(
    `${FORMATTER_SNIPPET}\nglobalThis.formatCount = formatCount;\nglobalThis.formatCompactNumber = formatCompactNumber;`,
    context,
  );
  return context;
}

const enUS = loadFormatter("en-US");
const compact = (value) => enUS.formatCompactNumber(value, "en-US");

// de-DE only behaves differently from en-US on a full-ICU build; a small-icu
// Node would silently fall back to en-US and make the locale claim vacuous.
const deDeAvailable = new Intl.NumberFormat("de-DE").resolvedOptions().locale.startsWith("de");

test("values below the compact threshold render as the exact integer", () => {
  assert.equal(compact(0), "0");
  assert.equal(compact(7), "7");
  assert.equal(compact(42), "42");
  assert.equal(compact(999), "999");
});

test("the 1k boundary stays visually progressive", () => {
  assert.equal(compact(1000), "1k");
  assert.equal(compact(1001), "1.001k");
  assert.equal(compact(1010), "1.01k");
  assert.equal(compact(1100), "1.1k");
  assert.equal(compact(1999), "1.999k");
  assert.equal(compact(9999), "9.999k");
});

test("every increment in the 1k decade produces a distinct label", () => {
  const rendered = new Set();
  for (let value = 1000; value < 2000; value += 1) {
    rendered.add(compact(value));
  }
  assert.equal(rendered.size, 1000, "a non-zero increment in the 1k range was rounded away");
});

test("large values roll over into the next tier instead of showing 1000 of the previous one", () => {
  assert.equal(compact(999999), "1M");
  assert.equal(compact(1000000), "1M");
  assert.equal(compact(1234567), "1.235M");
  assert.equal(compact(999999999), "1B");
  assert.equal(compact(1000000000), "1B");
  assert.equal(compact(1500000000000), "1.5T");
  assert.equal(compact(999999999999999), "1000T");
});

test("compact output never carries a grouping separator", () => {
  const values = [1000, 1001, 12345, 123456, 999999, 999999999, 1500000000000, 999999999999999];
  for (const value of values) {
    const rendered = compact(value);
    // A sign, digits, one optional en-US decimal point and one tier suffix --
    // anything else (a comma, a thin space, an exponent) is a grouping artifact.
    assert.match(
      rendered,
      /^-?[0-9]+(\.[0-9]+)?[kMBT]?$/,
      `grouping artifact in en-US rendering of ${value}: ${rendered}`,
    );
  }
});

test("negative counts keep their sign and tier", () => {
  assert.equal(compact(-999), "-999");
  assert.equal(compact(-1001), "-1.001k");
});

test("non-numeric input degrades to zero rather than raw JSON or NaN", () => {
  assert.equal(compact(null), "0");
  assert.equal(compact(undefined), "0");
  assert.equal(compact("not-a-number"), "0");
  assert.equal(compact("1001"), "1.001k");
});

test("the compact KPI layout keeps its six-character budget", () => {
  const values = [0, 7, 999, 1000, 1001, 9999, 12345, 99999, 123456, 999999, 1000000, 1234567,
    999999999, 1500000000000, 999999999999999];
  for (const value of values) {
    const rendered = compact(value);
    assert.ok(rendered.length <= 6, `compact rendering of ${value} is too wide for the KPI tile: ${rendered}`);
  }
});

test("the decimal separator follows the requested locale", { skip: deDeAvailable ? false : "full ICU unavailable" }, () => {
  const deDe = loadFormatter("de-DE");
  assert.equal(deDe.formatCompactNumber(1001, "de-DE"), "1,001k");
  assert.equal(deDe.formatCompactNumber(1500000000000, "de-DE"), "1,5T");
  assert.ok(
    !deDe.formatCompactNumber(123456, "de-DE").includes("."),
    "de-DE rendering must not emit a grouping dot",
  );
});

test("formatCount resolves the browser locale when no locale is passed", () => {
  assert.equal(enUS.formatCount(1001), "1.001k");
  const deDe = loadFormatter("de-DE");
  assert.equal(deDe.formatCount(1001), deDeAvailable ? "1,001k" : "1.001k");
  const noNavigator = loadFormatter(undefined);
  assert.equal(typeof noNavigator.formatCount(1001), "string");
});

function makeFakeElement() {
  return { textContent: "", title: "" };
}

function renderSummaryWith(statusCounts, totals) {
  const nodes = new Map();
  const nodeFor = (selector) => {
    if (!nodes.has(selector)) {
      nodes.set(selector, makeFakeElement());
    }
    return nodes.get(selector);
  };
  const context = {
    navigator: { language: "en-US" },
    document: { querySelector: nodeFor },
    elements: {
      lastSync: makeFakeElement(),
      headerStorageManaged: makeFakeElement(),
      headerStorageFree: makeFakeElement(),
    },
    formatMoney: () => "$0.00",
    formatRelativeTime: () => "Now",
    formatBytes: () => "0 B",
  };
  vm.createContext(context);
  vm.runInContext(
    `${FORMATTER_SNIPPET}\n${SUMMARY_SNIPPET}\nglobalThis.renderSummary = renderSummary;`,
    context,
  );
  context.renderSummary({
    status_counts: statusCounts,
    cost_usage: { totals },
  });
  return nodes;
}

test("lifecycle counters render compactly while the title keeps the exact integer", () => {
  const nodes = renderSummaryWith(
    {
      active: 1001,
      pending: 999,
      processing: 0,
      review: 1000,
      blocked: 999999999,
      finished: 1500000000000,
      stale: 7,
    },
    { total_tokens: 1234567, cost_usd: 0 },
  );

  const expected = [
    ["#metric-active", "1.001k", "1001"],
    ["#metric-pending", "999", "999"],
    ["#metric-processing", "0", "0"],
    ["#metric-review", "1k", "1000"],
    ["#metric-blocked", "1B", "999999999"],
    ["#metric-finished", "1.5T", "1500000000000"],
    ["#metric-stale", "7", "7"],
  ];
  for (const [selector, text, title] of expected) {
    const node = nodes.get(selector);
    assert.ok(node, `${selector} was never rendered`);
    assert.equal(node.textContent, text, `${selector} compact text`);
    assert.equal(node.title, title, `${selector} must expose the exact count on hover`);
  }

  const tokens = nodes.get("#metric-tokens");
  assert.equal(tokens.textContent, "1.235M");
  assert.equal(tokens.title, "1234567 tokens");
});
