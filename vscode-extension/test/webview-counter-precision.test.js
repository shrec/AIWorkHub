"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const appSource = fs.readFileSync(path.join(__dirname, "..", "media", "app.js"), "utf8");

// Extracts a verbatim block of the shipped webview script so the assertions
// below exercise the real production formatter rather than a parallel
// test-only reimplementation of it. The browser dashboard has its own suite;
// this file must fail when media/app.js regresses, not when dashboard.js does.
function extractRange(source, startMarker, tailMarker) {
  const start = source.indexOf(startMarker);
  assert.notEqual(start, -1, `snippet start marker not found in app.js: ${startMarker}`);
  const tail = source.indexOf(tailMarker, start);
  assert.notEqual(tail, -1, `snippet tail marker not found in app.js: ${tailMarker}`);
  const end = source.indexOf("\n}\n", tail);
  assert.notEqual(end, -1, `snippet end not found in app.js: ${tailMarker}`);
  return source.slice(start, end + 2);
}

// The production one-argument declaration. Other extension suites extract this
// same literal seam, so an assertion here fails loudly if a future refactor
// widens the signature again.
const FORMATCOUNT_SEAM = "function formatCount(value) {";

// history-charts.test.js slices [formatCount .. formatRelativeTime) out of
// app.js and evaluates that block on its own beside a three-helper harness, so
// every helper formatCount reaches for has to be declared inside that region.
const HISTORY_SEAM_TAIL = "function formatRelativeTime(";

// The snippet reaches past the seam to the last compact helper: the helpers sit
// below formatCount so the history extraction above stays self-contained.
const FORMATTER_SNIPPET = extractRange(
  appSource,
  "function numberValue(value) {",
  "function formatCompactNumber(value, locale) {",
);

function historySeamRegion() {
  const start = appSource.indexOf(FORMATCOUNT_SEAM);
  assert.notEqual(start, -1, `media/app.js must declare ${FORMATCOUNT_SEAM}`);
  const end = appSource.indexOf(HISTORY_SEAM_TAIL, start);
  assert.notEqual(end, -1, "media/app.js must declare formatRelativeTime after formatCount");
  return appSource.slice(start, end);
}

// The lifecycle/outcome KPI writes, sliced verbatim out of renderSummary() so
// the hover-truth assertions run the shipped rendering rather than a copy.
const COUNTER_BLOCK_START = "  const counts = snapshot.status_counts || {};";
const COUNTER_BLOCK_END =
  "document.querySelector(\"#metric-tokens\").title = `${numberValue(totals.total_tokens)} tokens`;";

function extractCounterBlock(source) {
  const start = source.indexOf(COUNTER_BLOCK_START);
  assert.notEqual(start, -1, "lifecycle counter block start marker not found in app.js");
  const end = source.indexOf(COUNTER_BLOCK_END, start);
  assert.notEqual(end, -1, "lifecycle counter block end marker not found in app.js");
  return source.slice(start, end + COUNTER_BLOCK_END.length);
}

const COUNTER_SNIPPET = extractCounterBlock(appSource);

test("the extracted snippets are the shipped precision formatter", () => {
  assert.match(FORMATTER_SNIPPET, /const COMPACT_COUNT_TIERS = \[/);
  assert.match(FORMATTER_SNIPPET, /function formatCompactNumber\(value, locale\)/);
  assert.ok(
    !appSource.includes('notation: "compact"'),
    "media/app.js still reaches for Intl compact notation, which collapses 1000 and 1001",
  );
  assert.match(COUNTER_SNIPPET, /target\.title = String\(numberValue\(counts\[metric\]\)\);/);
});

test("the shipped formatCount keeps its one-argument public seam", () => {
  assert.ok(
    appSource.includes(FORMATCOUNT_SEAM),
    `media/app.js must declare ${FORMATCOUNT_SEAM} -- other extension suites extract that literal`,
  );
  assert.ok(
    !/function formatCount\([^)]*,/.test(appSource),
    "formatCount must not take a second parameter; resolve locale through compactLocale() instead",
  );
  assert.match(FORMATTER_SNIPPET, /function compactLocale\(\)/);
});

// The regression this file exists to prevent twice over: the seam kept its
// one-argument shape, but its helpers lived above it, so history-charts.test.js
// evaluated the extracted block and hit a ReferenceError. Reproduce that exact
// harness -- the same three helpers the history suite injects, nothing else --
// so the coupling fails here instead of only in the history suite.
test("the extracted formatCount seam region evaluates on its own", () => {
  const context = { navigator: { language: "en-US" } };
  vm.createContext(context);
  vm.runInContext(
    `function numberValue(value) {
      const parsed = Number(value);
      return Number.isFinite(parsed) ? parsed : 0;
    }
    function asArray(value) { return Array.isArray(value) ? value : []; }
    function createElement(tag, className, text) { return { tag, className, textContent: text }; }
    ${historySeamRegion()}
    globalThis.formatCount = formatCount;`,
    context,
  );
  assert.equal(context.formatCount(999), "999");
  assert.equal(context.formatCount(1000), "1k");
  assert.equal(context.formatCount(1001), "1.001k");
  assert.equal(context.formatCount(999999999), "1B");
  assert.equal(context.formatCount(1.5e12), "1.5T");
});

function loadFormatter(language) {
  const context = { navigator: language === undefined ? undefined : { language } };
  vm.createContext(context);
  vm.runInContext(
    `${FORMATTER_SNIPPET}
globalThis.formatCount = formatCount;
globalThis.formatCompactNumber = formatCompactNumber;
globalThis.measuredCount = measuredCount;`,
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
  assert.equal(compact(999), "999");
});

test("the 1k boundary stays visually progressive", () => {
  assert.equal(compact(1000), "1k");
  assert.equal(compact(1001), "1.001k");
  assert.notEqual(compact(1000), compact(1001));
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
  assert.equal(compact(1.5e12), "1.5T");
  assert.equal(compact(999999999999999), "1000T");
});

test("compact output never carries a grouping separator", () => {
  const values = [1000, 1001, 12345, 123456, 999999, 999999999, 1.5e12, 999999999999999];
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
  assert.equal(compact({ total: 1001 }), "0");
  assert.equal(compact("1001"), "1.001k");
});

test("an unmeasured metric still reads as unmeasured, not as zero", () => {
  assert.equal(enUS.measuredCount("no_sample"), "not measured");
  assert.equal(enUS.measuredCount(null), "not measured");
  assert.equal(enUS.measuredCount(1001), "1.001k");
});

test("the compact KPI tile keeps its six-character budget", () => {
  const values = [0, 7, 999, 1000, 1001, 9999, 12345, 99999, 123456, 999999, 1000000, 1234567,
    999999999, 1.5e12, 999999999999999];
  for (const value of values) {
    const rendered = compact(value);
    assert.ok(rendered.length <= 6, `compact rendering of ${value} is too wide for the KPI tile: ${rendered}`);
  }
});

test("the decimal separator follows the requested locale", { skip: deDeAvailable ? false : "full ICU unavailable" }, () => {
  const deDe = loadFormatter("de-DE");
  assert.equal(deDe.formatCompactNumber(1001, "de-DE"), "1,001k");
  assert.equal(deDe.formatCompactNumber(1.5e12, "de-DE"), "1,5T");
  assert.ok(
    !deDe.formatCompactNumber(123456, "de-DE").includes("."),
    "de-DE rendering must not emit a grouping dot",
  );
});

test("formatCount resolves the host locale internally from its single argument", () => {
  assert.equal(enUS.formatCount(1001), "1.001k");
  assert.equal(enUS.formatCount.length, 1, "formatCount must stay a one-argument function");
  const deDe = loadFormatter("de-DE");
  assert.equal(deDe.formatCount(1001), deDeAvailable ? "1,001k" : "1.001k");
  // A webview host without navigator.language must still render, not throw.
  const noNavigator = loadFormatter(undefined);
  assert.equal(typeof noNavigator.formatCount(1001), "string");
});

function makeFakeElement() {
  return { textContent: "", title: "" };
}

function renderCountersWith(snapshot) {
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
    elements: { lastSync: makeFakeElement() },
    formatRelativeTime: () => "Now",
  };
  vm.createContext(context);
  vm.runInContext(
    `${FORMATTER_SNIPPET}
globalThis.renderCounters = function (snapshot) {
${COUNTER_SNIPPET}
};`,
    context,
  );
  context.renderCounters(snapshot);
  return nodes;
}

test("lifecycle counters render compactly while the title keeps the exact integer", () => {
  const nodes = renderCountersWith({
    status_counts: {
      active: 1001,
      pending: 999,
      processing: 0,
      review: 1000,
      blocked: 999999999,
      stale: 7,
    },
    outcome_counts: {
      accepted: 1001,
      rejected: 1000,
      archived: 999,
      superseded: 0,
      finished: 1.5e12,
    },
    cost_usage: { totals: { total_tokens: 1234567 } },
  });

  const expected = [
    ["#metric-active", "1.001k", "1001"],
    ["#metric-pending", "999", "999"],
    ["#metric-processing", "0", "0"],
    ["#metric-review", "1k", "1000"],
    ["#metric-blocked", "1B", "999999999"],
    ["#metric-stale", "7", "7"],
    ["#metric-accepted", "1.001k", "1001"],
    ["#metric-rejected", "1k", "1000"],
    ["#metric-archived", "999", "999"],
    ["#metric-superseded", "0", "0"],
    ["#metric-finished", "1.5T", "1500000000000"],
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

test("a missing counts payload renders zeros rather than raw JSON or undefined", () => {
  const nodes = renderCountersWith({});
  for (const selector of ["#metric-active", "#metric-finished", "#metric-tokens"]) {
    const node = nodes.get(selector);
    assert.equal(node.textContent, "0", `${selector} must degrade to 0`);
  }
  assert.equal(nodes.get("#metric-active").title, "0");
  assert.equal(nodes.get("#metric-tokens").title, "0 tokens");
});
