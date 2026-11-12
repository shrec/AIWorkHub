"use strict";

const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const root = path.resolve(__dirname, "..");
const app = fs.readFileSync(path.join(root, "media", "app.js"), "utf8");
const css = fs.readFileSync(path.join(root, "media", "app.css"), "utf8");

// Filter markup and logic exist in the popup.
for (const marker of [
  "function needfixWorkStateBar()",
  "function renderNeedfixWorkStateBar()",
  "function needfixRowsForWorkFilter(entries, finishedEntries, filter)",
  "function needfixWorkStateCounts(byWorkState, entries, finishedEntries)",
  "NEEDFIX_FINISHED_WORK_STATES",
  'id = "needfix-work-state-filter"',
  "dataset.needfixWorkState",
  'needfixWorkFilter: "open"',
  "payload.by_work_state",
  "payload.finished_entries",
  "work-state-badge",
  "work-state-finished",
  "work-state-open",
]) {
  assert.ok(app.includes(marker), `missing work-state filter marker: ${marker}`);
}
assert.ok(!app.includes("derive_work_state"), "the popup must not re-derive work state");

for (const selector of [".needfix-work-state-filter", ".needfix-work-state-chip", ".work-state-badge", ".work-state-finished", ".work-state-open"]) {
  assert.ok(css.includes(selector), `missing work-state CSS: ${selector}`);
}

// Extract the pure helpers and exercise them.
const start = app.indexOf("const NEEDFIX_WORK_STATE_LABELS");
const end = app.indexOf("function needfixFilters()", start);
assert.ok(start >= 0 && end > start, "work-state helpers must be extractable");
const sandbox = {
  asArray(value) { return Array.isArray(value) ? value : []; },
  numberValue(value) { const parsed = Number(value); return Number.isFinite(parsed) ? parsed : 0; },
};
vm.runInNewContext(
  `${app.slice(start, end)}\n` +
  "globalThis.__ws = { needfixRowsForWorkFilter, needfixWorkStateCounts, needfixIsFinished, needfixWorkStateLabel, NEEDFIX_WORK_STATE_FILTERS };",
  sandbox,
);
const { needfixRowsForWorkFilter, needfixWorkStateCounts, needfixIsFinished, needfixWorkStateLabel, NEEDFIX_WORK_STATE_FILTERS } = sandbox.__ws;

const open = [
  { id: "NF-1", work_state: "untriaged" },
  { id: "NF-2", work_state: "in_work" },
];
const finished = [{ id: "NF-9", work_state: "closed" }];

// Rows come back from another vm realm; copy ids into a local array before comparing.
const idsFor = (filter) => Array.from(needfixRowsForWorkFilter(open, finished, filter), (row) => row.id);
assert.deepStrictEqual(idsFor("open"), ["NF-1", "NF-2"], "finished is hidden by default");
assert.deepStrictEqual(idsFor("finished"), ["NF-9"], "one click shows finished");
assert.deepStrictEqual(idsFor("all"), ["NF-1", "NF-2", "NF-9"]);
assert.deepStrictEqual(idsFor("in_work"), ["NF-2"]);
assert.strictEqual(needfixRowsForWorkFilter([...open, finished[0]], finished, "all").length, 3, "rows present in both lists are not duplicated");
assert.ok(needfixIsFinished({ work_state: "closed" }) && !needfixIsFinished({ work_state: "stalled" }));
assert.strictEqual(needfixWorkStateLabel("decided_no_card"), "Decided, no card");
assert.strictEqual(NEEDFIX_WORK_STATE_FILTERS[0], "open", "open work is the default first chip");

const counts = needfixWorkStateCounts({ untriaged: 2, in_work: 1, stalled: 0, closed: 4 }, [], []);
assert.strictEqual(counts.open, 3);
assert.strictEqual(counts.finished, 4);
assert.strictEqual(counts.all, 7);
assert.strictEqual(counts.untriaged, 2);
const fallback = needfixWorkStateCounts(null, open, finished);
assert.strictEqual(fallback.open, 2);
assert.strictEqual(fallback.finished, 1);
