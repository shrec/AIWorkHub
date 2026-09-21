"use strict";

const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const root = path.resolve(__dirname, "..");
const extension = fs.readFileSync(path.join(root, "extension.js"), "utf8");
const app = fs.readFileSync(path.join(root, "media", "app.js"), "utf8");
const css = fs.readFileSync(path.join(root, "media", "app.css"), "utf8");

// ── 1. DOM placement: a second keyboard-accessible control beside the "i" ──
const identityInfo = extension.indexOf('id="identity-info"');
assert.ok(identityInfo !== -1, "Manager route details control must exist");
const waveInfo = extension.indexOf('id="wave-mini-roadmap-info"');
assert.ok(waveInfo !== -1, "Wave mini-roadmap control must exist");
assert.ok(waveInfo > identityInfo, "Wave control must sit immediately beside (after) the route details control");

const waveSummary = extension.match(/<summary class="wave-mini-roadmap-button" aria-label="([^"]+)"[^>]*>/);
assert.ok(waveSummary, "Wave control must expose a labelled <summary> (native keyboard/mouse toggle)");
assert.ok(waveSummary[1].trim().length > 0, "Wave control accessible label must not be empty");
assert.ok(extension.includes('id="wave-mini-roadmap-content"'), "Wave popup panel must exist");

// ── 2. Canonical wave selection + goal checklist via extracted pure helpers ──
const helperStart = app.indexOf("wave-mini-roadmap-helpers-begin");
const helperEnd = app.indexOf("wave-mini-roadmap-helpers-end");
assert.ok(helperStart !== -1 && helperEnd !== -1 && helperEnd > helperStart, "Helper block markers must exist");
const helperBlock = app.slice(app.indexOf("\n", helperStart) + 1, helperEnd);
const sandbox = {};
vm.createContext(sandbox);
vm.runInContext(helperBlock, sandbox);
for (const name of [
  "waveCurrentReady", "waveCurrentReason", "waveCurrentEntries", "waveVersionText",
  "waveGoalsFromDetail", "waveTaskRowsById", "waveTaskEvidence", "waveGoalState", "waveGoalChecklist",
  "waveServerGoalGate", "waveTaskStates", "waveWatchedTaskIds", "waveTaskStatesChanged",
]) {
  assert.strictEqual(typeof sandbox[name], "function", `${name} must be extractable`);
}
for (const name of ["waveSelectActive", "waveSemver", "waveIsActive", "waveVersionCompare"]) {
  assert.strictEqual(typeof sandbox[name], "undefined", `${name}: the popup must not rank waves locally`);
}

// Only a ready server projection naming one non-blank wave id selects a wave.
assert.strictEqual(sandbox.waveCurrentReady({ state: "ready", wave_id: "RM-1" }), true);
for (const current of [
  null, undefined, "ready", {}, { state: "ready" }, { state: "ready", wave_id: "" }, { state: "ready", wave_id: " " },
  { state: "ready", wave_id: 7 }, { state: "READY", wave_id: "RM-1" }, { state: "UNKNOWN", wave_id: "RM-1" },
]) {
  assert.strictEqual(sandbox.waveCurrentReady(current), false, `${JSON.stringify(current)} must not select a wave`);
}

// UNKNOWN keeps the server's typed reason visible.
assert.strictEqual(
  sandbox.waveCurrentReason({ state: "UNKNOWN", selection_reason: "ambiguous_active_wave" }),
  "Wave selection is ambiguous: several active waves share the highest target (ambiguous_active_wave)",
);
assert.strictEqual(sandbox.waveCurrentReason(null), "The server reported no current-wave projection");
assert.strictEqual(sandbox.waveCurrentReason({ state: "UNKNOWN" }), "The server current-wave projection is not ready");
assert.strictEqual(
  sandbox.waveCurrentReason({ state: "UNKNOWN", selection_reason: "toString" }),
  "The server current-wave projection is not ready (toString)",
  "Only own reason keys may map to text",
);

// The server's exact wave id picks list rows; version order plays no part.
const projectedRows = [
  { id: "RM-0000-00051", milestone: "0.11.51", status: "in_progress" },
  { id: "RM-0000-00050", milestone: "0.11.50", status: "in_progress" },
];
const plainRows = (rows) => Array.from(rows, (row) => row.id);
assert.deepStrictEqual(
  plainRows(sandbox.waveCurrentEntries({ state: "ready", wave_id: "RM-0000-00050" }, projectedRows)),
  ["RM-0000-00050"],
  "The server-selected id wins over a higher-version active row",
);
assert.deepStrictEqual(plainRows(sandbox.waveCurrentEntries({ state: "ready", wave_id: "RM-0000-0005" }, projectedRows)), [], "Ids match exactly, never by prefix");
assert.deepStrictEqual(plainRows(sandbox.waveCurrentEntries({ state: "UNKNOWN", wave_id: "RM-0000-00050" }, projectedRows)), []);
assert.deepStrictEqual(plainRows(sandbox.waveCurrentEntries({ state: "ready", wave_id: "RM-0000-00050" }, null)), []);

assert.strictEqual(sandbox.waveVersionText(" 0.11.53 "), "0.11.53");
for (const value of [null, undefined, "", "  ", 53]) {
  assert.strictEqual(sandbox.waveVersionText(value), "UNKNOWN", `${JSON.stringify(value)} is not a version`);
}

// Goal checklist: a goal is checked only when every listed task has exactly one
// canonical finished/accepted row; everything else stays unchecked or UNKNOWN.
const plain = (value) => JSON.parse(JSON.stringify(value));
const goalItems = (goals, rows) => plain(sandbox.waveGoalChecklist(goals, rows)).items;
const goalItem = (goal, rows) => goalItems([goal], rows)[0];
const rowOf = (task_id, status) => ({ task_id, status });
const goalOf = (task_ids, extra) => Object.assign({ id: "goal-1", label: "Ship it", task_ids }, extra);

assert.deepStrictEqual(
  goalItem(goalOf(["t1", "t2"]), [rowOf("t1", "finished"), rowOf("t2", "accepted")]),
  { label: "Ship it", state: "checked", unresolved: false, blocked: false },
  "Finished/accepted tasks must check their goal",
);
for (const status of ["review_ready", "pending", "processing", "in_progress", "archived", "needs_fix"]) {
  assert.deepStrictEqual(
    goalItem(goalOf(["t1", "t2"]), [rowOf("t1", "finished"), rowOf("t2", status)]),
    { label: "Ship it", state: "open", unresolved: false, blocked: false },
    `${status} must keep the goal unchecked`,
  );
}
assert.deepStrictEqual(
  goalItem(goalOf(["t1", "t2"]), [rowOf("t1", "finished"), rowOf("t2", "blocked_on_review")]),
  { label: "Ship it", state: "open", unresolved: false, blocked: true },
  "A blocked task must keep the goal unchecked and flagged",
);

// Canonical joined statuses feed the checklist, not raw row.status alone:
// completed / stale_already_done / a done worker finish a task; blocked* status
// and blocked*/deferred* workers are blocked.
const canonicalDoneRows = [
  ["completed", rowOf("t1", "completed")],
  ["stale_already_done", rowOf("t1", "stale_already_done")],
  ["done worker", { task_id: "t1", status: "in_progress", worker_status: "done" }],
];
for (const [name, row] of canonicalDoneRows) {
  assert.deepStrictEqual(
    goalItem(goalOf(["t1"]), [row]),
    { label: "Ship it", state: "checked", unresolved: false, blocked: false },
    `${name} must check its goal`,
  );
}
const canonicalBlockedRows = [
  ["blocked_on_human status", rowOf("t1", "blocked_on_human")],
  ["deferred_review worker", { task_id: "t1", status: "queued", worker_status: "deferred_review" }],
];
for (const [name, row] of canonicalBlockedRows) {
  assert.deepStrictEqual(
    goalItem(goalOf(["t1"]), [row]),
    { label: "Ship it", state: "open", unresolved: false, blocked: true },
    `${name} must keep the goal unchecked and flagged blocked`,
  );
}

// Missing, duplicate or malformed evidence is UNKNOWN and never checked.
const unresolvedCases = [
  ["a missing task row", goalOf(["t1", "t2"]), [rowOf("t1", "finished")]],
  ["no task rows at all", goalOf(["t1"]), undefined],
  ["non-array task rows", goalOf(["t1"]), "not-an-array"],
  ["a missing status", goalOf(["t1"]), [{ task_id: "t1" }]],
  ["a blank status", goalOf(["t1"]), [rowOf("t1", "  ")]],
  ["a non-string status", goalOf(["t1"]), [rowOf("t1", 7)]],
  ["a missing-status marker", goalOf(["t1"]), [rowOf("t1", "missing")]],
  ["an unknown-status marker", goalOf(["t1"]), [rowOf("t1", "unknown")]],
  ["a duplicated finished row", goalOf(["t1"]), [rowOf("t1", "finished"), rowOf("t1", "finished")]],
  ["a row without a usable task_id", goalOf(["t1"]), [{ task_id: 1, status: "finished" }]],
  ["a goal with no task ids", goalOf([]), [rowOf("t1", "finished")]],
  ["a goal with non-array task ids", goalOf("t1"), [rowOf("t1", "finished")]],
  ["a repeated task id", goalOf(["t1", "t1"]), [rowOf("t1", "finished")]],
  ["a non-string task id", goalOf(["t1", 2]), [rowOf("t1", "finished")]],
  ["a blank label", goalOf(["t1"], { label: "" }), [rowOf("t1", "finished")]],
  ["a non-string label", goalOf(["t1"], { label: 5 }), [rowOf("t1", "finished")]],
  ["a missing goal id", goalOf(["t1"], { id: undefined }), [rowOf("t1", "finished")]],
];
for (const [name, goal, rows] of unresolvedCases) {
  const item = goalItem(goal, rows);
  assert.strictEqual(item.state, "unknown", `${name} must be UNKNOWN, never checked`);
  assert.strictEqual(item.unresolved, true, `${name} must be flagged unresolved`);
}
for (const goal of [null, undefined, "goal", 7]) {
  const item = goalItem(goal, [rowOf("t1", "finished")]);
  assert.strictEqual(item.state, "unknown", "A malformed goal entry must be UNKNOWN");
  assert.strictEqual(item.label, "Unlabelled goal", "A goal without a label must say so");
}
assert.deepStrictEqual(
  goalItems([goalOf(["t1"]), goalOf(["t1"], { label: "Twin" })], [rowOf("t1", "finished")]).map((item) => item.state),
  ["unknown", "unknown"],
  "Duplicate goal ids must both be UNKNOWN",
);

// A known unfinished or blocked task keeps the goal open even beside unresolved evidence,
// which stays flagged; a duplicate that holds a blocked row is both UNKNOWN and blocked, never checked.
assert.deepStrictEqual(
  goalItem(goalOf(["t1", "t2"]), [rowOf("t1", "processing")]),
  { label: "Ship it", state: "open", unresolved: true, blocked: false },
);
assert.deepStrictEqual(
  goalItem(goalOf(["t1", "t2"]), [rowOf("t1", "blocked")]),
  { label: "Ship it", state: "open", unresolved: true, blocked: true },
);
for (const rows of [
  [rowOf("t1", "finished"), rowOf("t1", "blocked")],
  [rowOf("t1", "blocked"), rowOf("t1", "finished")],
  [rowOf("t1", "blocked"), rowOf("t1", "blocked_on_review")],
]) {
  assert.deepStrictEqual(
    goalItem(goalOf(["t1"]), rows),
    { label: "Ship it", state: "open", unresolved: true, blocked: true },
    "A duplicate holding a blocked row must stay UNKNOWN and flagged blocked, and keep the goal unchecked",
  );
}

// Blocked tasks outside every goal are still counted, once per task id.
assert.strictEqual(
  plain(sandbox.waveGoalChecklist(
    [goalOf(["t1"])],
    [
      rowOf("t1", "finished"),
      rowOf("t9", "blocked"),
      rowOf("t9", "blocked"),
      rowOf("t8", "blocked_on_review"),
      { task_id: "t6", status: "queued", worker_status: "deferred_review" },
      rowOf("t7", "pending"),
      rowOf("t5", "completed"),
    ],
  )).blockedTasks,
  3,
);

// Only an array at provenance.wave_goals counts as goals.
assert.strictEqual(plain(sandbox.waveGoalsFromDetail({ provenance: { wave_goals: [goalOf(["t1"])] } })).length, 1);
for (const detail of [
  null, undefined, {}, { provenance: null }, { provenance: "text" }, { provenance: {} },
  { provenance: { wave_goals: "Ship it" } }, { provenance: { wave_goals: { 0: goalOf(["t1"]) } } },
]) {
  assert.strictEqual(sandbox.waveGoalsFromDetail(detail), null, "Anything but an array of goals counts as absent");
}

// Canonical task-state changes are detected only for tasks the active wave depends on.
const statesBefore = sandbox.waveTaskStates(null, [
  { task_id: "t1", status: "blocked" },
  { task_id: "t2", status: "finished", worker_status: "done" },
  null,
  {},
]);
assert.strictEqual(statesBefore.rows.size, 2, "Only tasks with an id are fingerprinted");
const statesAfter = sandbox.waveTaskStates(null, [
  { task_id: "t1", status: "finished" },
  { task_id: "t2", status: "finished", worker_status: "done" },
]);
assert.strictEqual(sandbox.waveTaskStatesChanged(statesBefore, statesAfter, new Set(["t1"])), true, "A status change on a watched task must be detected");
assert.strictEqual(sandbox.waveTaskStatesChanged(statesBefore, statesAfter, new Set(["t2"])), false, "An unchanged watched task is not a change");
assert.strictEqual(sandbox.waveTaskStatesChanged(statesBefore, statesAfter, new Set(["t3"])), false, "An id absent from both snapshots is not a change");
assert.strictEqual(sandbox.waveTaskStatesChanged(statesBefore, sandbox.waveTaskStates(null, []), new Set(["t1"])), true, "A watched task leaving the snapshot is a change");

// A snapshot has no rows for blocked/superseded/finished/archived tasks; their exact status_counts totals are the only trace.
const totalsOf = (counts) => sandbox.waveTaskStates({ status_counts: counts }, []);
const totalsBase = totalsOf({ pending: 1, blocked: 1, finished: 2 });
const watchedC = new Set(["task-c"]);
assert.strictEqual(sandbox.waveTaskStatesChanged(totalsBase, totalsOf({ pending: 1, blocked: 1, finished: 2 }), watchedC), false, "Equal totals are no change");
assert.strictEqual(sandbox.waveTaskStatesChanged(totalsBase, totalsOf({ pending: 1, blocked: 0, finished: 3 }), watchedC), true, "A blocked task finishing moves the totals though no row changes");
for (const status of ["blocked", "superseded", "finished", "archived"]) {
  const moved = Object.assign({ pending: 1, blocked: 1, finished: 2 }, { [status]: 7 });
  assert.strictEqual(sandbox.waveTaskStatesChanged(totalsBase, totalsOf(moved), watchedC), true, `A moved ${status} total must be detected`);
}
assert.strictEqual(
  sandbox.waveTaskStatesChanged(totalsBase, totalsOf({ pending: 9, processing: 4, review: 3, active: 16, stale: 2, blocked: 1, finished: 2 }), watchedC),
  false,
  "Churn among active statuses moves no rowless total",
);
assert.strictEqual(sandbox.waveTaskStatesChanged(totalsBase, totalsOf({ blocked: 0, finished: 3 }), new Set()), false, "Nothing watched means nothing to refresh");

// Missing or malformed totals read as one fixed unknown value: never a throw, never a phantom change.
const unknownTotals = sandbox.waveTaskStates(null, []).totals;
for (const snapshotLike of [
  {}, { status_counts: null }, { status_counts: "many" }, { status_counts: [] },
  { status_counts: { finished: "3" } }, { status_counts: { finished: NaN } }, { status_counts: { blocked: null } },
]) {
  assert.strictEqual(sandbox.waveTaskStates(snapshotLike, []).totals, unknownTotals, `${JSON.stringify(snapshotLike)} must read as unknown totals`);
}

const watchedEntries = [
  { id: "RM-1", milestone: "0.1.1", status: "in_progress", task_ids: ["t1", 5] },
  { id: "RM-0", milestone: "0.1.0", status: "in_progress", task_ids: ["old"] },
];
const watchedDetail = {
  id: "RM-1",
  task_ids: ["t2"],
  provenance: { wave_goals: [goalOf(["t3", "t1"]), null] },
};
const watchedCurrent = {
  state: "ready",
  wave_id: "RM-1",
  goals: [{ id: "g", label: "G", state: "open", tasks: [{ task_id: "t4", status: "pending" }, null] }],
};
assert.deepStrictEqual(
  Array.from(sandbox.waveWatchedTaskIds(watchedCurrent, watchedEntries, null)).sort(),
  ["t1", "t4"],
  "The server-selected wave's list row and projected goal tasks are watched",
);
assert.deepStrictEqual(
  Array.from(sandbox.waveWatchedTaskIds(watchedCurrent, watchedEntries, watchedDetail)).sort(),
  ["t1", "t2", "t3", "t4"],
  "The detail's task ids and goal task ids join the watch list",
);
assert.deepStrictEqual(
  Array.from(sandbox.waveWatchedTaskIds(watchedCurrent, watchedEntries, Object.assign({}, watchedDetail, { id: "RM-0" }))).sort(),
  ["t1", "t4"],
  "A detail for another wave adds nothing",
);
assert.deepStrictEqual(
  Array.from(sandbox.waveWatchedTaskIds({ state: "ready", wave_id: "RM-0" }, watchedEntries, null)).sort(),
  ["old"],
  "The server's wave is watched even when a higher-version active row exists",
);
assert.strictEqual(sandbox.waveWatchedTaskIds(null, watchedEntries, null), null, "No projection means nothing to watch");
assert.strictEqual(sandbox.waveWatchedTaskIds(unknownCurrent("no_active_wave"), watchedEntries, null), null, "An UNKNOWN projection means nothing to watch");

// ── 3. Refresh/reload reconciles canonical list/detail state; no shadow checklist ──
assert.ok(app.includes("function renderWaveMiniRoadmap(snapshot)"), "Wave renderer must exist");
assert.ok(app.includes("renderWaveMiniRoadmap(snapshot);"), "Wave renderer must run on every snapshot refresh");
assert.ok(app.includes("waveCurrentEntries(current, state.waveMiniRoadmapEntries)"), "Wave rows must come from the popup's isolated list state, never snapshot.roadmap.items");
assert.ok(!app.includes("waveCurrentEntries(current, state.roadmapEntries)"), "Popup must never select from the Roadmap dialog's shared list");
assert.ok(app.includes("payload.current_wave"), "The popup must consume the server's current_wave projection");
assert.ok(app.includes("roadmapId: current.wave_id"), "Detail must be requested for the server's exact wave id");
assert.ok(!/function (waveSelectActive|waveSemver|waveVersionCompare)\b/.test(app), "No local highest-semver wave selection may remain");
assert.ok(app.includes("state.waveMiniRoadmapDetail"), "Wave acceptance/tasks must come from canonical detail state");
assert.ok(app.includes('type: "requestRoadmap"'), "Popup must reuse the list bridge");
assert.ok(app.includes('type: "requestRoadmapDetail"'), "Popup must reuse the detail bridge");
assert.ok(app.includes("requestWaveMiniRoadmap"), "Popup must request list+detail on open");
assert.ok(app.includes("waveGoalsFromDetail(detail)"), "Renderer must read the goals from the canonical detail's provenance.wave_goals");
assert.ok(app.includes("waveGoalChecklist(goals, detail.tasks)"), "Renderer must join every goal with the canonical detail's task rows");
assert.ok(app.includes("roadmap.truncated === true"), "Renderer must fail closed on truncated roadmap");
assert.ok(app.includes("UNKNOWN"), "Renderer must fail closed to UNKNOWN");
assert.ok(app.includes("WAVE_COMPLETE_STATUSES.has(status)"), "DOM complete mark must use the same finished/accepted whitelist");
assert.ok(app.includes('purpose: "waveMiniRoadmap"'), "Popup list request must tag its purpose so responses route to the isolated list");
assert.ok(app.includes("function renderWaveMiniRoadmapList"), "Popup must have an isolated list-response handler");
assert.ok(app.includes("state.waveMiniRoadmapEntries"), "Popup must keep a separate ephemeral list projection");
assert.ok(extension.includes('waveMiniRoadmap: "waveMiniRoadmap"'), "Extension must expose a dedicated waveMiniRoadmap outbound type");
assert.ok(extension.includes('waveMiniRoadmapDetail: "waveMiniRoadmapDetail"'), "Extension must expose a dedicated waveMiniRoadmapDetail outbound type");
assert.ok(app.includes("function renderWaveMiniRoadmapDetail"), "Popup must have an isolated detail-response handler");
assert.ok(app.includes('case "waveMiniRoadmapDetail"'), "Wave detail responses must route to the isolated wave handler");
assert.ok(!app.includes("snapshot.roadmap.items"), "Renderer must never read snapshot.roadmap.items (summary has no items)");
assert.ok(!app.includes("RM-2026-00064"), "Roadmap IDs must not be hard-coded");
assert.ok(!app.includes("roadmap.sqlite"), "Webview must not read Roadmap storage directly");

// ── 4. Popup interaction + layout isolation ──
assert.ok(css.includes(".wave-mini-roadmap-info"), "Wave popup styling must exist");
assert.ok(css.includes(".wave-mini-roadmap-button"), "Wave button styling must exist");
assert.ok(css.includes(".wave-mini-roadmap-panel"), "Wave popup panel styling must exist");
for (const rule of [
  /\.wave-mini-roadmap-goals\s*\{/, /\.wave-goal\s*\{/, /\.wave-goal-mark\s*\{/,
  /\.wave-goal-label\s*\{/, /\.wave-goal-flag\s*\{/, /\.wave-goal-flag-blocked\s*\{/,
  /\.wave-goal-checked \.wave-goal-mark\s*\{/, /\.wave-goal-unknown \.wave-goal-mark\s*\{/,
]) {
  assert.ok(rule.test(css), `Goal checklist styling must define ${rule}`);
}
assert.ok(!/\.wave-mini-roadmap-(task|section)/.test(css), "The raw task list styling must go with the raw task list");

// ── 5. Real renderSnapshot → renderWaveMiniRoadmap path against a DOM mock ──
// The canonical summary snapshot exposes only {available,error,active,total,
// truncated} for roadmap — never items. The wave renderer must select the
// active wave from state.waveMiniRoadmapEntries (isolated popup list bridge) and join
// provenance.wave_goals with the task rows of state.waveMiniRoadmapDetail (detail bridge).
function extractFunction(source, name) {
  const start = source.indexOf(`function ${name}(`);
  assert.ok(start !== -1, `missing function ${name}`);
  const brace = source.indexOf("{", start);
  let depth = 0;
  for (let i = brace; i < source.length; i += 1) {
    if (source[i] === "{") depth += 1;
    else if (source[i] === "}") {
      depth -= 1;
      if (depth === 0) return source.slice(start, i + 1);
    }
  }
  assert.fail(`unbalanced braces in ${name}`);
}

function makeMockElement(tag) {
  return {
    tagName: String(tag || "div").toUpperCase(),
    className: "",
    textContent: "",
    title: "",
    attributes: {},
    children: [],
    setAttribute(name, value) {
      this.attributes[name] = String(value);
    },
    append(...nodes) {
      for (const node of nodes) this.children.push(node);
      return this;
    },
    appendChild(node) {
      this.children.push(node);
      return node;
    },
    replaceChildren(...nodes) {
      this.children = nodes.filter(Boolean);
    },
  };
}

function findByClass(root, className) {
  if (root.className === className) return root;
  for (const child of root.children || []) {
    const hit = findByClass(child, className);
    if (hit) return hit;
  }
  return null;
}

function classTokens(node) {
  return String(node.className || "").split(/\s+/).filter(Boolean);
}

function findAllByToken(root, token, hits = []) {
  if (classTokens(root).includes(token)) hits.push(root);
  for (const child of root.children || []) findAllByToken(child, token, hits);
  return hits;
}

function textOf(root) {
  return [root.textContent || "", ...(root.children || []).map(textOf)].join(" ");
}

function goalCount(content) {
  const count = findByClass(content, "wave-mini-roadmap-count");
  return count && count.textContent;
}

// The checklist as a reader or assistive technology meets it: role, checked state, glyph, label and visible flags.
function goalViews(content) {
  return findAllByToken(content, "wave-goal").map((row) => {
    const mark = findByClass(row, "wave-goal-mark");
    const label = findByClass(row, "wave-goal-label");
    return {
      role: row.attributes.role,
      checked: row.attributes["aria-checked"],
      readonly: row.attributes["aria-readonly"],
      state: classTokens(row).find((token) => /^wave-goal-(checked|open|unknown)$/.test(token)),
      mark: mark && mark.textContent,
      markHidden: mark && mark.attributes["aria-hidden"],
      label: label && label.textContent,
      flags: findAllByToken(row, "wave-goal-flag").map((flag) => flag.textContent),
    };
  });
}

const ARIA_CHECKED_BY_STATE = { "wave-goal-checked": "true", "wave-goal-open": "false", "wave-goal-unknown": "mixed" };

function assertAccessibleRows(content, message) {
  for (const view of goalViews(content)) {
    assert.strictEqual(view.role, "checkbox", `${message}: rows must be checkbox-like`);
    assert.strictEqual(view.readonly, "true", `${message}: rows must be read-only`);
    assert.strictEqual(view.checked, ARIA_CHECKED_BY_STATE[view.state], `${message}: aria-checked must follow the state`);
  }
}

const renderSource = [
  helperBlock,
  extractFunction(app, "createElement"),
  extractFunction(app, "asArray"),
  extractFunction(app, "renderWaveMiniRoadmap"),
  extractFunction(app, "renderWaveMiniRoadmapState"),
  extractFunction(app, "waveGoalRow"),
  extractFunction(app, "renderSnapshot"),
].join("\n");

// Every sibling renderer renderSnapshot invokes is stubbed; only the wave renderer
// and the helpers it needs run for real.
const SNAPSHOT_SIBLING_RENDERERS = [
  "renderStorageState", "renderSummary", "renderManagerIdentity",
  "renderCallbackObservability", "renderKnownRepositories", "renderSourceHealth",
  "renderFilterOptions", "renderTaskTable", "renderStats", "renderKpis",
  "renderUsage", "renderPlanDag", "renderWorkforce", "renderToolUse",
  "renderStorage", "renderSystemLogs", "renderReturns", "renderRuns",
  "renderWarnings", "applyHistorySnapshot", "clearTaskDetail",
];

function stubSnapshotSiblings(target) {
  for (const name of SNAPSHOT_SIBLING_RENDERERS) {
    target[name] = function noop() {};
  }
  target.renderStorageState = function renderStorageStateStub() { return true; };
}

function buildIntegrationSandbox() {
  const content = makeMockElement("div");
  const elements = {
    waveMiniRoadmapContent: content,
    offlineAlert: { hidden: false },
  };
  const state = {
    tasks: [],
    snapshot: null,
    selectedTaskId: null,
    roadmapEntries: [],
    waveMiniRoadmapEntries: [],
    waveMiniRoadmapDetail: null,
  };
  const sandbox2 = {
    document: {
      createElement(tag) { return makeMockElement(tag); },
      createDocumentFragment() { return makeMockElement("#fragment"); },
    },
    elements,
    state,
    console,
  };
  stubSnapshotSiblings(sandbox2);
  sandbox2.flattenTasks = function flattenTasksStub() { return []; };
  vm.createContext(sandbox2);
  vm.runInContext(renderSource, sandbox2);
  return { sandbox: sandbox2, content };
}

const summaryOnlySnapshot = {
  roadmap: { available: true, error: null, active: 1, total: 2, truncated: false },
};

const ACCEPTANCE_PROSE = "Current wave provenance.wave_goals render as concise labels with accessible checkbox-like states; raw task IDs and full acceptance prose stay out of the default view.";

const oldWaveEntry = { id: "RM-0000-00050", title: "Wave 0.11.50", status: "in_progress", milestone: "0.11.50", task_ids: ["task-old"] };
const newWaveEntry = { id: "RM-0000-00051", title: "Wave 0.11.51", status: "in_progress", milestone: "0.11.51", task_ids: ["task-a", "task-b", "task-c"] };
const listEntries = [
  { id: "RM-0000-00049", title: "Wave 0.11.49", status: "completed", milestone: "0.11.49", task_ids: [] },
  oldWaveEntry,
  newWaveEntry,
];

const oldWaveDetail = {
  id: "RM-0000-00050",
  title: "Wave 0.11.50",
  status: "in_progress",
  milestone: "0.11.50",
  acceptance: ["Old acceptance prose the checklist never shows"],
  task_ids: ["task-old"],
  tasks: [{ task_id: "task-old", status: "finished" }],
  provenance: { wave_goals: [{ id: "goal-old", label: "Ship the 0.11.50 release", task_ids: ["task-old"] }] },
};

const newWaveGoals = [
  { id: "goal-checklist", label: "Popover shows a short goal checklist", task_ids: ["task-a", "task-b"] },
  { id: "goal-live", label: "Open popover updates when a task finishes", task_ids: ["task-c"] },
];

const detailItem = {
  id: "RM-0000-00051",
  title: "Wave 0.11.51",
  status: "in_progress",
  milestone: "0.11.51",
  acceptance: [ACCEPTANCE_PROSE],
  needfix_ids: ["NF-2026-00901"],
  task_ids: ["task-a", "task-b", "task-c"],
  tasks: [
    { task_id: "task-a", status: "finished" },
    { task_id: "task-b", status: "accepted" },
    { task_id: "task-c", status: "blocked" },
  ],
  provenance: { wave_goals: newWaveGoals, needfix_ids: ["NF-2026-00901"] },
};

const allFinished = [
  { task_id: "task-a", status: "finished" },
  { task_id: "task-b", status: "accepted" },
  { task_id: "task-c", status: "finished" },
];

function withTasks(rows) {
  return Object.assign({}, detailItem, { tasks: rows });
}

function withGoals(goals, rows) {
  return Object.assign({}, detailItem, { provenance: { wave_goals: goals }, tasks: rows });
}

// The server's ready current_wave projection (wave_roadmap.project_current_wave) for one detail.
// Every goal it lists is server-checked, so the local join alone decides these
// fixtures; the server-gate tests below lower individual goals explicitly.
function currentFor(detail, overrides) {
  const declared = detail && detail.provenance && Array.isArray(detail.provenance.wave_goals) ? detail.provenance.wave_goals : [];
  const goals = declared
    .filter((goal) => goal && typeof goal === "object" && typeof goal.id === "string")
    .map((goal) => ({
      id: goal.id,
      label: goal.label,
      state: "checked",
      tasks: (Array.isArray(goal.task_ids) ? goal.task_ids : []).map((task_id) => ({ task_id, status: "finished" })),
    }));
  return Object.assign({
    state: "ready",
    selection_reason: "unique_highest_active_wave",
    wave_id: detail.id,
    installed_version: "0.11.51",
    target_milestone: detail.milestone,
    overdue: false,
    goals,
  }, overrides);
}

// The server's typed UNKNOWN projection.
function unknownCurrent(reason) {
  return {
    state: "UNKNOWN",
    selection_reason: reason,
    wave_id: null,
    installed_version: "0.11.53",
    target_milestone: null,
    overdue: null,
    goals: [],
  };
}

function listPayload(entries = listEntries, current = currentFor(detailItem)) {
  return { ok: true, entries, current_wave: current };
}

function targetOf(content) {
  const target = findByClass(content, "wave-mini-roadmap-target");
  return target && target.textContent;
}

function renderDetail(detail, current = currentFor(detail && typeof detail === "object" ? detail : detailItem), entries = listEntries) {
  const { sandbox: isb, content } = buildIntegrationSandbox();
  isb.state.waveMiniRoadmapEntries = entries;
  isb.state.waveMiniRoadmapCurrent = current;
  isb.state.waveMiniRoadmapDetail = detail;
  isb.renderSnapshot(summaryOnlySnapshot);
  return content;
}

// Initial canonical state: the server-selected wave 0.11.51, one goal checked and one held open by a blocked task.
{
  const { sandbox: isb, content } = buildIntegrationSandbox();
  isb.state.waveMiniRoadmapEntries = listEntries;
  isb.state.waveMiniRoadmapCurrent = currentFor(detailItem);
  isb.state.waveMiniRoadmapDetail = detailItem;
  isb.renderSnapshot(summaryOnlySnapshot);
  assert.strictEqual(targetOf(content), "Target 0.11.51", "Initial wave must render the server-selected 0.11.51 target");
  assert.strictEqual(findByClass(content, "wave-mini-roadmap-installed").textContent, "Installed 0.11.51");
  assert.strictEqual(findByClass(content, "wave-mini-roadmap-overdue"), null, "An on-time wave is not overdue");
  const title = findByClass(content, "wave-mini-roadmap-title");
  assert.strictEqual(title && title.textContent, "Wave 0.11.51");
  assert.strictEqual(goalCount(content), "1/2 goals done · 1 blocked task", "The summary must count goals, not tasks");
  assert.deepStrictEqual(goalViews(content), [
    {
      role: "checkbox", checked: "true", readonly: "true", state: "wave-goal-checked",
      mark: "✓", markHidden: "true", label: "Popover shows a short goal checklist", flags: [],
    },
    {
      role: "checkbox", checked: "false", readonly: "true", state: "wave-goal-open",
      mark: "", markHidden: "true", label: "Open popover updates when a task finishes", flags: ["blocked"],
    },
  ], "Goals must render as read-only checkbox rows: one checked, one open with a visible blocked flag");
  const group = findByClass(content, "wave-mini-roadmap-goals");
  assert.strictEqual(group && group.attributes.role, "group", "The checklist must be a group");
  assert.strictEqual(group && group.attributes["aria-label"], "Wave goals", "The checklist group must be labelled");

  // The default view is plain language only: no task IDs, NeedFix IDs, roadmap IDs or acceptance prose.
  const text = textOf(content);
  for (const raw of ["task-a", "task-b", "task-c", "NF-2026-00901", "RM-0000-00051", ACCEPTANCE_PROSE.slice(0, 32)]) {
    assert.ok(!text.includes(raw), `The default view must not show ${raw}`);
  }
  assert.ok(text.includes("Popover shows a short goal checklist"), "Goal labels must render");

  // Refresh: the same renderer reconciles the once-blocked task when the canonical detail says finished.
  isb.state.waveMiniRoadmapDetail = withTasks(allFinished);
  isb.renderSnapshot(summaryOnlySnapshot);
  assert.strictEqual(goalCount(content), "2/2 goals done", "Refresh must reconcile task states");
  assert.deepStrictEqual(goalViews(content).map((view) => view.checked), ["true", "true"]);
}

// One goal per canonical status: only finished/accepted check a goal; blocked stays open and flagged; missing/unknown are UNKNOWN.
{
  const cases = [
    ["finished", "wave-goal-checked", []],
    ["accepted", "wave-goal-checked", []],
    ["review_ready", "wave-goal-open", []],
    ["pending", "wave-goal-open", []],
    ["blocked", "wave-goal-open", ["blocked"]],
    ["archived", "wave-goal-open", []],
    ["missing", "wave-goal-unknown", ["UNKNOWN"]],
    ["unknown", "wave-goal-unknown", ["UNKNOWN"]],
  ];
  const goals = cases.map(([status], index) => ({ id: `goal-${index}`, label: `Goal ${status}`, task_ids: [`task-${index}`] }));
  const rows = cases.map(([status], index) => ({ task_id: `task-${index}`, status }));
  const content = renderDetail(withGoals(goals, rows));
  assert.deepStrictEqual(
    goalViews(content).map((view) => [view.label, view.state, view.flags]),
    cases.map(([status, state, flags]) => [`Goal ${status}`, state, flags]),
    "Only finished/accepted may check a goal; blocked stays open and flagged; missing/unknown are UNKNOWN",
  );
  assertAccessibleRows(content, "Every status");
  assert.strictEqual(goalCount(content), "2/8 goals done · 2 UNKNOWN · 1 blocked task");
}

// Partial and missing joins never check a goal; the unresolved goal is visibly UNKNOWN and a blocker stays visible.
{
  const cases = [
    {
      name: "one goal's task row is missing",
      tasks: [{ task_id: "task-a", status: "finished" }, { task_id: "task-b", status: "accepted" }],
      views: [["wave-goal-checked", []], ["wave-goal-unknown", ["UNKNOWN"]]],
      count: "1/2 goals done · 1 UNKNOWN",
    },
    {
      name: "a goal has one finished and one missing task",
      tasks: [{ task_id: "task-a", status: "finished" }, { task_id: "task-c", status: "finished" }],
      views: [["wave-goal-unknown", ["UNKNOWN"]], ["wave-goal-checked", []]],
      count: "1/2 goals done · 1 UNKNOWN",
    },
    {
      name: "there are no task rows at all",
      tasks: undefined,
      views: [["wave-goal-unknown", ["UNKNOWN"]], ["wave-goal-unknown", ["UNKNOWN"]]],
      count: "0/2 goals done · 2 UNKNOWN",
    },
    {
      name: "the task rows are not an array",
      tasks: "task-a,task-b,task-c",
      views: [["wave-goal-unknown", ["UNKNOWN"]], ["wave-goal-unknown", ["UNKNOWN"]]],
      count: "0/2 goals done · 2 UNKNOWN",
    },
    {
      name: "a task row is duplicated",
      tasks: allFinished.concat([{ task_id: "task-c", status: "finished" }]),
      views: [["wave-goal-checked", []], ["wave-goal-unknown", ["UNKNOWN"]]],
      count: "1/2 goals done · 1 UNKNOWN",
    },
    {
      name: "a duplicated task row holds a blocked one",
      tasks: allFinished.concat([{ task_id: "task-c", status: "blocked" }]),
      views: [["wave-goal-checked", []], ["wave-goal-open", ["UNKNOWN", "blocked"]]],
      count: "1/2 goals done · 1 UNKNOWN · 1 blocked task",
    },
    {
      name: "a known unfinished task sits beside a missing one",
      tasks: [{ task_id: "task-a", status: "processing" }],
      views: [["wave-goal-open", ["UNKNOWN"]], ["wave-goal-unknown", ["UNKNOWN"]]],
      count: "0/2 goals done · 2 UNKNOWN",
    },
    {
      name: "a blocked task sits beside a missing one",
      tasks: [{ task_id: "task-a", status: "blocked" }],
      views: [["wave-goal-open", ["UNKNOWN", "blocked"]], ["wave-goal-unknown", ["UNKNOWN"]]],
      count: "0/2 goals done · 2 UNKNOWN · 1 blocked task",
    },
  ];
  for (const { name, tasks, views, count } of cases) {
    const content = renderDetail(withTasks(tasks));
    assert.deepStrictEqual(goalViews(content).map((view) => [view.state, view.flags]), views, `When ${name}: goal states`);
    assert.strictEqual(goalCount(content), count, `When ${name}: summary`);
    assertAccessibleRows(content, `When ${name}`);
  }
}

// Malformed goal entries are UNKNOWN rows, never checked and never dropped.
{
  const content = renderDetail(withGoals([null, "text", { id: "goal-x", label: "Only an id" }], allFinished));
  assert.deepStrictEqual(
    goalViews(content).map((view) => [view.label, view.state, view.flags]),
    [
      ["Unlabelled goal", "wave-goal-unknown", ["UNKNOWN"]],
      ["Unlabelled goal", "wave-goal-unknown", ["UNKNOWN"]],
      ["Only an id", "wave-goal-unknown", ["UNKNOWN"]],
    ],
    "Malformed goal entries must render as UNKNOWN rows",
  );
  assert.strictEqual(goalCount(content), "0/3 goals done · 3 UNKNOWN");
}

// Missing or malformed wave_goals fail closed to UNKNOWN with no checklist and no task ID.
for (const [name, provenance] of [
  ["absent provenance", undefined],
  ["null provenance", null],
  ["provenance without goals", {}],
  ["non-array goals", { wave_goals: "Ship it" }],
  ["empty goals", { wave_goals: [] }],
]) {
  const content = renderDetail(Object.assign({}, detailItem, { provenance }));
  const unknown = findByClass(content, "wave-mini-roadmap-unknown");
  assert.ok(unknown, `${name} must render UNKNOWN`);
  assert.strictEqual(unknown.textContent, "UNKNOWN");
  assert.strictEqual(findByClass(content, "wave-mini-roadmap-goals"), null, `${name} must not render a checklist`);
  assert.ok(!textOf(content).includes("task-a"), `${name} must not fall back to raw task IDs`);
}

// Detail not yet loaded (list only) → UNKNOWN (fail closed).
{
  const content = renderDetail(null);
  assert.ok(findByClass(content, "wave-mini-roadmap-unknown"), "Missing detail must fail closed to UNKNOWN");
}

// Detail for a different wave than the server selected → UNKNOWN (fail closed).
{
  const content = renderDetail(Object.assign({}, detailItem, { id: "RM-0000-00049" }), currentFor(detailItem));
  assert.ok(findByClass(content, "wave-mini-roadmap-unknown"), "Mismatched detail must fail closed to UNKNOWN");
}

// A detail whose target disagrees with the projection is stale evidence → UNKNOWN.
for (const target of ["0.11.52", null, ""]) {
  const content = renderDetail(detailItem, currentFor(detailItem, { target_milestone: target }));
  assert.ok(findByClass(content, "wave-mini-roadmap-unknown"), `Target ${target} must fail closed to UNKNOWN`);
  assert.strictEqual(findByClass(content, "wave-mini-roadmap-goals"), null, `Target ${target} must not render a checklist`);
}

// Truncated or unavailable roadmap → UNKNOWN (fail closed).
{
  const { sandbox: isb, content } = buildIntegrationSandbox();
  isb.state.waveMiniRoadmapEntries = listEntries;
  isb.state.waveMiniRoadmapCurrent = currentFor(detailItem);
  isb.state.waveMiniRoadmapDetail = detailItem;
  isb.renderSnapshot({ roadmap: { available: true, error: null, active: 1, total: 2, truncated: true } });
  assert.ok(findByClass(content, "wave-mini-roadmap-unknown"), "Truncated roadmap must fail closed to UNKNOWN");
  isb.renderSnapshot({ roadmap: { available: false, error: "Roadmap offline" } });
  const reason = findByClass(content, "wave-mini-roadmap-reason");
  assert.strictEqual(reason && reason.textContent, "Roadmap offline", "An unavailable roadmap must say why");
}

// Every non-ready server projection is a typed UNKNOWN with its reason and no checklist,
// even while the list and a detail look complete.
for (const reason of [
  "no_active_wave", "ambiguous_active_wave", "invalid_wave_version", "invalid_installed_version",
  "truncated_roadmap", "missing_goal_data", "malformed_roadmap_row",
]) {
  const content = renderDetail(detailItem, unknownCurrent(reason));
  const unknown = findByClass(content, "wave-mini-roadmap-unknown");
  assert.strictEqual(unknown && unknown.textContent, "UNKNOWN", `${reason} must render UNKNOWN`);
  const shown = findByClass(content, "wave-mini-roadmap-reason");
  assert.ok(shown && shown.textContent.endsWith(`(${reason})`), `${reason} must be named`);
  assert.strictEqual(findByClass(content, "wave-mini-roadmap-goals"), null, `${reason} must not render a checklist`);
}
for (const [name, current] of [
  ["no projection", null],
  ["a non-object projection", "ready"],
  ["a ready projection without a wave id", currentFor(detailItem, { wave_id: null })],
  ["a blank wave id", currentFor(detailItem, { wave_id: "  " })],
  ["a differently cased state", currentFor(detailItem, { state: "READY" })],
  ["an unrecognised reason", { state: "UNKNOWN", selection_reason: "odd_reason" }],
]) {
  const content = renderDetail(detailItem, current);
  assert.ok(findByClass(content, "wave-mini-roadmap-unknown"), `${name} must render UNKNOWN`);
  assert.strictEqual(findByClass(content, "wave-mini-roadmap-goals"), null, `${name} must not render a checklist`);
}

// Two list rows carrying the server's exact wave id are ambiguous → UNKNOWN.
{
  const content = renderDetail(detailItem, currentFor(detailItem), [newWaveEntry, Object.assign({}, newWaveEntry)]);
  assert.ok(findByClass(content, "wave-mini-roadmap-unknown"), "A duplicated wave id must fail closed to UNKNOWN");
}

// The server, never a local highest-semver ranking, selects the wave: higher active rows cannot displace it.
{
  const content = renderDetail(oldWaveDetail, currentFor(oldWaveDetail), [
    oldWaveEntry,
    newWaveEntry,
    { id: "RM-0000-00060", title: "Wave 0.11.60", status: "in_progress", milestone: "0.11.60", task_ids: [] },
  ]);
  assert.strictEqual(targetOf(content), "Target 0.11.50", "Higher active rows must not displace the server's wave");
  assert.strictEqual(goalCount(content), "1/1 goal done");
}

// A server-selected wave outside the bounded list still renders from its exact detail.
{
  const content = renderDetail(detailItem, currentFor(detailItem), []);
  assert.strictEqual(targetOf(content), "Target 0.11.51");
  assert.strictEqual(findByClass(content, "wave-mini-roadmap-title").textContent, "Wave 0.11.51");
}

// Installed runtime and wave target are distinct: 0.11.53 installed, 0.11.51 target overdue.
// Overdue neither retargets the wave nor completes a goal.
{
  const current = currentFor(detailItem, { installed_version: "0.11.53", overdue: true });
  current.goals[1].state = "open";
  const content = renderDetail(detailItem, current);
  assert.strictEqual(findByClass(content, "wave-mini-roadmap-installed").textContent, "Installed 0.11.53");
  assert.strictEqual(targetOf(content), "Target 0.11.51", "The target must stay the wave's own milestone");
  assert.strictEqual(findByClass(content, "wave-mini-roadmap-overdue").textContent, " (overdue)");
  assert.strictEqual(findByClass(content, "wave-mini-roadmap-milestone").title, "Installed 0.11.53 · Target 0.11.51 (overdue)");
  assert.ok(!targetOf(content).includes("0.11.53"), "The target must never be derived from the installed version");
  assert.ok(!findByClass(content, "wave-mini-roadmap-title").textContent.includes("0.11.53"), "The wave must not be called an installed-version wave");
  assert.strictEqual(goalCount(content), "1/2 goals done · 1 blocked task", "Overdue must not complete a goal");
  assert.deepStrictEqual(goalViews(content).map((view) => view.state), ["wave-goal-checked", "wave-goal-open"]);
}
for (const overdue of [false, null, "true", 1, undefined]) {
  const content = renderDetail(detailItem, currentFor(detailItem, { installed_version: "0.11.53", overdue }));
  assert.strictEqual(findByClass(content, "wave-mini-roadmap-overdue"), null, `overdue=${overdue} must not render overdue`);
}
for (const installed of [null, "", 53]) {
  const content = renderDetail(detailItem, currentFor(detailItem, { installed_version: installed }));
  assert.strictEqual(findByClass(content, "wave-mini-roadmap-installed").textContent, "Installed UNKNOWN");
}

// The server holds the goal verdict: locally finished evidence never checks a goal the server left open or UNKNOWN.
for (const [serverState, state, flags] of [
  ["open", "wave-goal-open", []],
  ["UNKNOWN", "wave-goal-unknown", ["UNKNOWN"]],
  ["checked ", "wave-goal-unknown", ["UNKNOWN"]],
  [undefined, "wave-goal-unknown", ["UNKNOWN"]],
]) {
  const current = currentFor(detailItem);
  current.goals[1].state = serverState;
  const content = renderDetail(withTasks(allFinished), current);
  assert.deepStrictEqual(
    goalViews(content).map((view) => [view.state, view.flags]),
    [["wave-goal-checked", []], [state, flags]],
    `A server ${serverState} goal must never render checked`,
  );
  assertAccessibleRows(content, `Server ${serverState}`);
}
{
  const missing = currentFor(detailItem);
  missing.goals.pop();
  const doubled = currentFor(detailItem);
  doubled.goals.push(Object.assign({}, doubled.goals[1]));
  for (const [name, current] of [["missing on the server", missing], ["duplicated on the server", doubled]]) {
    const content = renderDetail(withTasks(allFinished), current);
    assert.deepStrictEqual(
      goalViews(content).map((view) => [view.state, view.flags]),
      [["wave-goal-checked", []], ["wave-goal-unknown", ["UNKNOWN"]]],
      `A goal ${name} must be UNKNOWN`,
    );
  }
  const extra = currentFor(detailItem);
  extra.goals.push({ id: "goal-server-only", label: "Only the server knows", state: "checked", tasks: [] });
  const content = renderDetail(withTasks(allFinished), extra);
  assert.deepStrictEqual(
    goalViews(content).map((view) => [view.label, view.state]),
    [
      ["Popover shows a short goal checklist", "wave-goal-checked"],
      ["Open popover updates when a task finishes", "wave-goal-checked"],
      ["Only the server knows", "wave-goal-unknown"],
    ],
    "A goal only the server lists must render UNKNOWN",
  );
  assert.strictEqual(goalCount(content), "2/3 goals done · 1 UNKNOWN");
}

// An archived predecessor with an unbound pending successor never checks its goal; a stale row is UNKNOWN.
{
  const goals = [{ id: "goal-lsp", label: "LSP index integration", task_ids: ["task-lsp-v1"] }];
  const archived = withGoals(goals, [
    { task_id: "task-lsp-v1", status: "finished", archived_at: "2026-09-20T10:00:00Z" },
    { task_id: "task-lsp-v2", status: "pending" },
  ]);
  const archivedView = renderDetail(archived, currentFor(archived));
  assert.deepStrictEqual(goalViews(archivedView).map((view) => [view.state, view.flags]), [["wave-goal-open", []]], "An archived predecessor must keep its goal open");
  const stale = withGoals(goals, [{ task_id: "task-lsp-v1", status: "finished", stale: true }]);
  const staleView = renderDetail(stale, currentFor(stale));
  assert.deepStrictEqual(goalViews(staleView).map((view) => [view.state, view.flags]), [["wave-goal-unknown", ["UNKNOWN"]]], "Stale evidence must be UNKNOWN");
}

// Compact rendering: at most 20 rows, long labels are cut with the full text on hover, and goals past the cap still count.
{
  const goals = Array.from({ length: 23 }, (_, index) => ({
    id: `goal-${index}`,
    label: index === 0 ? "L".repeat(400) : `Goal ${index}`,
    task_ids: [`task-${index}`],
  }));
  const rows = goals.map((goal, index) => ({ task_id: `task-${index}`, status: index === 22 ? "blocked" : "finished" }));
  const content = renderDetail(withGoals(goals, rows));
  const views = goalViews(content);
  assert.strictEqual(views.length, 20, "At most 20 goal rows render");
  const more = findByClass(content, "wave-mini-roadmap-more");
  assert.strictEqual(more && more.textContent, "...3 more", "The rows past the cap are summarised");
  assert.strictEqual(views[0].label.length, 160, "A long label is cut to 160 characters");
  assert.ok(views[0].label.endsWith("…"), "A cut label ends with an ellipsis");
  const labels = findAllByToken(content, "wave-goal-label");
  assert.strictEqual(labels[0].title, "L".repeat(400), "The full label stays reachable on hover");
  assert.strictEqual(labels[1].title, "", "A short label needs no hover text");
  assert.strictEqual(goalCount(content), "22/23 goals done · 1 blocked task", "Goals past the cap still count in the summary");
}

// Isolation regression: Roadmap dialog list filter/fail must never alter the
// popup, while the popup's own failed list response must fail closed.
{
  const { sandbox: isb, content } = buildIntegrationSandbox();
  isb.state.waveMiniRoadmapEntries = listEntries;
  isb.state.waveMiniRoadmapCurrent = currentFor(detailItem);
  isb.state.waveMiniRoadmapDetail = detailItem;
  isb.renderSnapshot(summaryOnlySnapshot);
  assert.strictEqual(targetOf(content), "Target 0.11.51", "Isolation: wave must render 0.11.51 before dialog list changes");
  assert.strictEqual(goalCount(content), "1/2 goals done · 1 blocked task", "Isolation: initial goals must be 1/2 done");

  // Simulate the Roadmap dialog filtering to a different wave, then failing its
  // list entirely. Both overwrite the shared state.roadmapEntries, but the
  // popup's isolated projection must remain intact.
  isb.state.roadmapEntries = [listEntries[0]];
  isb.renderSnapshot(summaryOnlySnapshot);
  assert.strictEqual(targetOf(content), "Target 0.11.51", "Isolation: dialog filter must not change the popup wave");
  isb.state.roadmapEntries = [];
  isb.renderSnapshot(summaryOnlySnapshot);
  assert.strictEqual(targetOf(content), "Target 0.11.51", "Isolation: dialog list failure must not change the popup wave");
  assert.strictEqual(goalCount(content), "1/2 goals done · 1 blocked task", "Isolation: dialog list failure must not alter popup goals");

  // The popup's own list response failing/unavailable must clear its isolated
  // list and fail closed to UNKNOWN.
  isb.state.waveMiniRoadmapEntries = [];
  isb.state.waveMiniRoadmapDetail = null;
  isb.renderSnapshot(summaryOnlySnapshot);
  const unknown = findByClass(content, "wave-mini-roadmap-unknown");
  assert.ok(unknown, "Isolation: popup's own failed list must fail closed");
  assert.strictEqual(unknown.textContent, "UNKNOWN");
}

// ── 6. Dashboard Refresh reconciles an OPEN popup through the request/response path ──
// requestRefresh must, while the wave popup is open, re-issue the wave list/detail
// flow so a task that became finished in canonical Roadmap updates the open
// popup's count (no close/reopen needed). This drives the real outbound messages
// and the real list→detail→render path rather than mutating detail directly.
const activeStatusesDeclaration = app.match(/^const ACTIVE_STATUSES = \[[^\]]*\];$/m);
assert.ok(activeStatusesDeclaration, "flattenTasks needs the ACTIVE_STATUSES declaration");

const refreshFlowSource = [
  activeStatusesDeclaration[0],
  helperBlock,
  extractFunction(app, "createElement"),
  extractFunction(app, "asArray"),
  extractFunction(app, "flattenTasks"),
  extractFunction(app, "waveCycleCurrent"),
  extractFunction(app, "renderWaveMiniRoadmap"),
  extractFunction(app, "renderWaveMiniRoadmapState"),
  extractFunction(app, "waveGoalRow"),
  extractFunction(app, "renderWaveMiniRoadmapList"),
  extractFunction(app, "reconcileWaveMiniRoadmap"),
  extractFunction(app, "renderWaveMiniRoadmapDetail"),
  extractFunction(app, "requestWaveMiniRoadmap"),
  extractFunction(app, "refreshWaveMiniRoadmapOnTaskChange"),
  extractFunction(app, "closeIdentityInfoPopover"),
  extractFunction(app, "renderSnapshot"),
  extractFunction(app, "requestRefresh"),
].join("\n");

// The listener the webview registers for every host message; it runs against the functions above.
const messageListenerStart = app.indexOf('window.addEventListener("message"');
assert.ok(messageListenerStart !== -1, "The webview must listen for host messages");
const messageListenerSource = app.slice(messageListenerStart, app.indexOf("\n});", messageListenerStart) + 4);

function buildRefreshSandbox() {
  const content = makeMockElement("div");
  const messages = [];
  const elements = {
    refreshButton: { disabled: false, textContent: "Refresh" },
    tableLoading: { hidden: true },
    offlineAlert: { hidden: false },
    identityInfo: { open: false },
    waveMiniRoadmap: { open: true },
    waveMiniRoadmapContent: content,
    roadmapDetailPanel: { replaceChildren() {} },
  };
  const state = {
    snapshot: { roadmap: { available: true, error: null, active: 1, total: 2, truncated: false } },
    tasks: [],
    selectedTaskId: null,
    roadmapDetail: null,
    waveMiniRoadmapEntries: [],
    waveMiniRoadmapDetail: null,
    waveMiniRoadmapWaitingFor: null,
    waveMiniRoadmapRequested: false,
    waveMiniRoadmapGeneration: 0,
    waveMiniRoadmapTaskStates: null,
  };
  const listeners = {};
  const sandbox3 = {
    document: {
      createElement(tag) { return makeMockElement(tag); },
      createDocumentFragment() { return makeMockElement("#fragment"); },
    },
    window: { setTimeout() {}, addEventListener(type, handler) { listeners[type] = handler; } },
    stopReadyRetry() {},
    vscode: { postMessage(message) { messages.push(message); } },
    elements,
    state,
    console,
    setConnection() {},
    appendNeedfixObject() {},
    appendNeedfixEvents() {},
  };
  stubSnapshotSiblings(sandbox3);
  vm.createContext(sandbox3);
  vm.runInContext(refreshFlowSource, sandbox3);
  vm.runInContext(messageListenerSource, sandbox3);
  assert.strictEqual(typeof listeners.message, "function", "The webview must register its message listener");
  const post = (message) => listeners.message({ data: message });
  return { sandbox: sandbox3, content, messages, post };
}

// Open popup: Refresh must still post refresh AND re-issue the wave list request.
{
  const { sandbox: rs, content, messages } = buildRefreshSandbox();
  rs.requestRefresh();
  assert.ok(messages.some((m) => m.type === "refresh"), "Refresh must still post refresh");
  const listReq = messages.find((m) => m.type === "requestRoadmap" && m.purpose === "waveMiniRoadmap");
  assert.ok(listReq, "Refresh with an open popup must re-issue the wave list request");
  assert.strictEqual(listReq && listReq.waveGeneration, 1, "Wave list request must carry its generation");

  // Canonical list response selects the active wave and requests its detail.
  rs.renderWaveMiniRoadmapList(listPayload(), listReq.waveGeneration);
  const detailReq = messages.find((m) => m.type === "requestRoadmapDetail");
  assert.strictEqual(detailReq && detailReq.roadmapId, "RM-0000-00051", "List response must request the newest active wave's detail");
  assert.strictEqual(detailReq && detailReq.purpose, "waveMiniRoadmap", "Wave detail request must tag its purpose so the response routes to the isolated handler");
  assert.strictEqual(detailReq && detailReq.waveGeneration, listReq.waveGeneration, "Wave detail request must echo the same generation as its list");

  // Detail response arrives with the blocked task now finished; the open popup
  // must check its goal without closing/reopening.
  rs.renderWaveMiniRoadmapDetail({ ok: true, item: withTasks(allFinished) }, detailReq.waveGeneration);
  const count = findByClass(content, "wave-mini-roadmap-count");
  assert.strictEqual(count && count.textContent, "2/2 goals done", "Refresh flow must update the open popup's goals");
}

// Closed popup: Refresh must NOT issue a wave list request.
{
  const { sandbox: rs, messages } = buildRefreshSandbox();
  rs.elements.waveMiniRoadmap.open = false;
  rs.requestRefresh();
  assert.ok(messages.some((m) => m.type === "refresh"), "Refresh must still post refresh when popup is closed");
  assert.ok(!messages.some((m) => m.type === "requestRoadmap" && m.purpose === "waveMiniRoadmap"), "Closed popup must not trigger a wave list request on refresh");
}

// ── 7. Detail routing isolation: wave vs dialog detail responses ──
// renderRoadmapDetail owns only the dialog; renderWaveMiniRoadmapDetail owns
// only the popup. A concurrent dialog failure while wave detail is pending
// must not poison the popup, and a successful wave detail must not touch the
// dialog panel.
const detailRoutingSource = [
  helperBlock,
  extractFunction(app, "createElement"),
  extractFunction(app, "asArray"),
  extractFunction(app, "waveCycleCurrent"),
  extractFunction(app, "renderWaveMiniRoadmap"),
  extractFunction(app, "renderWaveMiniRoadmapState"),
  extractFunction(app, "waveGoalRow"),
  extractFunction(app, "renderRoadmapDetail"),
  extractFunction(app, "renderWaveMiniRoadmapDetail"),
].join("\n");

function buildDetailRoutingSandbox() {
  const content = makeMockElement("div");
  let dialogDraws = 0;
  const elements = {
    waveMiniRoadmapContent: content,
    roadmapDetailPanel: {
      replaceChildren() { dialogDraws += 1; },
    },
  };
  const state = {
    snapshot: { roadmap: { available: true, error: null, active: 1, total: 2, truncated: false } },
    tasks: [],
    roadmapDetail: null,
    waveMiniRoadmapEntries: [],
    waveMiniRoadmapDetail: null,
    waveMiniRoadmapWaitingFor: null,
    waveMiniRoadmapRequested: false,
    waveMiniRoadmapGeneration: 0,
  };
  const sandbox4 = {
    document: {
      createElement(tag) { return makeMockElement(tag); },
      createDocumentFragment() { return makeMockElement("#fragment"); },
    },
    elements,
    state,
    console,
    appendNeedfixObject() {},
    appendNeedfixEvents() {},
  };
  vm.createContext(sandbox4);
  vm.runInContext(detailRoutingSource, sandbox4);
  return { sandbox: sandbox4, content, dialogDraws: () => dialogDraws };
}

// Concurrent dialog failure while wave detail is pending must not poison the popup.
{
  const { sandbox: ds, content } = buildDetailRoutingSandbox();
  ds.state.waveMiniRoadmapEntries = listEntries;
  ds.state.waveMiniRoadmapWaitingFor = "RM-0000-00050";
  ds.state.waveMiniRoadmapRequested = true;
  ds.state.waveMiniRoadmapDetail = null;
  const before = JSON.stringify(content.children);
  ds.renderRoadmapDetail({ ok: false, error: "dialog boom" });
  assert.strictEqual(ds.state.roadmapDetail, null, "Dialog failure must clear only dialog state");
  assert.strictEqual(ds.state.waveMiniRoadmapWaitingFor, "RM-0000-00050", "Dialog failure must not clear the pending wave detail marker");
  assert.strictEqual(ds.state.waveMiniRoadmapRequested, true, "Dialog failure must not cancel the wave detail request");
  assert.strictEqual(JSON.stringify(content.children), before, "Dialog failure must not re-render the popup");
}

// Successful wave detail must update the popup without touching the dialog.
{
  const { sandbox: ds, content, dialogDraws } = buildDetailRoutingSandbox();
  const dialogDetail = { id: "RM-0000-00001", title: "Existing dialog detail" };
  ds.state.roadmapDetail = dialogDetail;
  ds.state.waveMiniRoadmapEntries = listEntries;
  ds.state.waveMiniRoadmapCurrent = currentFor(detailItem);
  ds.state.waveMiniRoadmapWaitingFor = "RM-0000-00050";
  ds.state.waveMiniRoadmapRequested = true;
  ds.renderWaveMiniRoadmapDetail({ ok: true, item: withTasks(allFinished) });
  assert.strictEqual(ds.state.roadmapDetail, dialogDetail, "Wave detail success must not overwrite the dialog detail");
  assert.strictEqual(dialogDraws(), 0, "Wave detail success must not redraw the dialog panel");
  assert.strictEqual(ds.state.waveMiniRoadmapWaitingFor, null, "Wave detail success must clear the wait marker");
  const count = findByClass(content, "wave-mini-roadmap-count");
  assert.strictEqual(count && count.textContent, "2/2 goals done", "Wave detail success must render the popup");
}

// ── 8. Adjacent popover interaction: opening either closes the other ──
assert.ok(app.includes("closeIdentityInfoPopover();"), "Opening the wave popover must close identity info");
assert.ok(app.includes("closeWaveMiniRoadmapPopover();"), "Opening identity info must close the wave popover");

const popoverInteractionSource = [
  extractFunction(app, "closeIdentityInfoPopover"),
  extractFunction(app, "closeWaveMiniRoadmapPopover"),
].join("\n");

function buildPopoverSandbox() {
  const elements = {
    identityInfo: { open: false },
    waveMiniRoadmap: { open: false },
  };
  const sandbox5 = { elements };
  vm.createContext(sandbox5);
  vm.runInContext(popoverInteractionSource, sandbox5);
  return { sandbox: sandbox5, elements };
}

// Opening the wave popover closes identity info and leaves the wave popover open.
{
  const { sandbox: ps, elements } = buildPopoverSandbox();
  elements.identityInfo.open = true;
  elements.waveMiniRoadmap.open = true;
  ps.closeIdentityInfoPopover();
  assert.strictEqual(elements.identityInfo.open, false, "Wave popover opening must close identity info");
  assert.strictEqual(elements.waveMiniRoadmap.open, true, "Wave popover itself must stay open");
}

// Opening identity info closes the wave popover and leaves identity info open.
{
  const { sandbox: ps, elements } = buildPopoverSandbox();
  elements.identityInfo.open = true;
  elements.waveMiniRoadmap.open = true;
  ps.closeWaveMiniRoadmapPopover();
  assert.strictEqual(elements.waveMiniRoadmap.open, false, "Identity info opening must close the wave popover");
  assert.strictEqual(elements.identityInfo.open, true, "Identity info itself must stay open");
}

// Closing an already-closed popover is a no-op (native details behavior preserved).
{
  const { sandbox: ps, elements } = buildPopoverSandbox();
  ps.closeIdentityInfoPopover();
  ps.closeWaveMiniRoadmapPopover();
  assert.strictEqual(elements.identityInfo.open, false);
  assert.strictEqual(elements.waveMiniRoadmap.open, false);
}

// ── 9. Stale in-flight wave detail must not poison a newer refresh cycle ──
// A per-popup monotonic generation is tagged on every wave list/detail request
// and echoed back by the extension bridge as `correlation`. Responses from a
// superseded cycle are ignored; only the current cycle may settle the popup.

// A-detail-before-B-list: the stale detail from cycle A arrives after cycle B
// has started; it must be ignored so cycle B still requests its fresh detail.
{
  const { sandbox: rs, content, messages } = buildRefreshSandbox();

  // Cycle A: list A -> detail A requested (generation 1).
  rs.requestWaveMiniRoadmap();
  const listA = messages.find((m) => m.type === "requestRoadmap" && m.purpose === "waveMiniRoadmap");
  assert.strictEqual(listA && listA.waveGeneration, 1, "Cycle A list must carry generation 1");
  rs.renderWaveMiniRoadmapList(listPayload(), listA.waveGeneration);
  const detailA = messages.find((m) => m.type === "requestRoadmapDetail" && m.purpose === "waveMiniRoadmap");
  assert.strictEqual(detailA && detailA.roadmapId, "RM-0000-00051", "Cycle A must request active wave detail");
  assert.strictEqual(detailA && detailA.waveGeneration, 1, "Cycle A detail must carry generation 1");

  // Cycle B starts (generation 2).
  rs.requestWaveMiniRoadmap();
  const listB = messages.filter((m) => m.type === "requestRoadmap" && m.purpose === "waveMiniRoadmap").pop();
  assert.strictEqual(listB && listB.waveGeneration, 2, "Cycle B list must carry generation 2");

  // Stale detail A arrives before list B: ignored, leaving cycle B alive.
  rs.renderWaveMiniRoadmapDetail({ ok: true, item: withTasks(allFinished) }, detailA.waveGeneration);
  assert.strictEqual(rs.state.waveMiniRoadmapRequested, true, "Stale detail A before list B must not cancel cycle B");
  assert.strictEqual(rs.state.waveMiniRoadmapWaitingFor, null, "Stale detail A before list B must not clear the wait marker");

  // List B arrives: cycle B must now request its own fresh detail.
  rs.renderWaveMiniRoadmapList(listPayload(), listB.waveGeneration);
  const detailB = messages.filter((m) => m.type === "requestRoadmapDetail" && m.purpose === "waveMiniRoadmap").pop();
  assert.strictEqual(detailB && detailB.waveGeneration, 2, "Cycle B must request a fresh detail after stale detail A");
  assert.strictEqual(detailB && detailB.roadmapId, "RM-0000-00051", "Cycle B detail must target the active wave");

  // Fresh detail B checks every goal.
  rs.renderWaveMiniRoadmapDetail({ ok: true, item: withTasks(allFinished) }, detailB.waveGeneration);
  const count = findByClass(content, "wave-mini-roadmap-count");
  assert.strictEqual(count && count.textContent, "2/2 goals done", "Cycle B must render the fresh goal count");
}

// A-detail-after-B-detail: a stale detail from cycle A that arrives after cycle
// B has already rendered must be ignored and must not regress the popup.
{
  const { sandbox: rs, content, messages } = buildRefreshSandbox();

  // Cycle A.
  rs.requestWaveMiniRoadmap();
  const genA = messages.find((m) => m.type === "requestRoadmap" && m.purpose === "waveMiniRoadmap").waveGeneration;
  rs.renderWaveMiniRoadmapList(listPayload(), genA);

  // Cycle B checks every goal.
  rs.requestWaveMiniRoadmap();
  const genB = messages.filter((m) => m.type === "requestRoadmap" && m.purpose === "waveMiniRoadmap").pop().waveGeneration;
  rs.renderWaveMiniRoadmapList(listPayload(), genB);
  rs.renderWaveMiniRoadmapDetail({ ok: true, item: withTasks(allFinished) }, genB);
  let count = findByClass(content, "wave-mini-roadmap-count");
  assert.strictEqual(count && count.textContent, "2/2 goals done", "Cycle B must render every goal checked first");

  // Stale detail A (task-c still blocked) arrives after B: ignored, popup stays checked.
  rs.renderWaveMiniRoadmapDetail({ ok: true, item: detailItem }, genA);
  count = findByClass(content, "wave-mini-roadmap-count");
  assert.strictEqual(count && count.textContent, "2/2 goals done", "Stale detail A after B must not regress the popup");
}

// Stale failures: a superseded cycle's failed list or detail must not poison the
// current cycle; only a current-cycle failure renders UNKNOWN.
{
  const { sandbox: rs, content, messages } = buildRefreshSandbox();

  rs.requestWaveMiniRoadmap(); // cycle A (generation 1)
  rs.requestWaveMiniRoadmap(); // cycle B (generation 2)
  rs.renderWaveMiniRoadmapList(listPayload(), 2);
  const detailB = messages.filter((m) => m.type === "requestRoadmapDetail" && m.purpose === "waveMiniRoadmap").pop();
  assert.strictEqual(detailB && detailB.waveGeneration, 2, "Cycle B must request detail");

  // Stale failed list A must not cancel or clear cycle B.
  rs.renderWaveMiniRoadmapList({ ok: false, error: "stale list boom", entries: [] }, 1);
  assert.strictEqual(rs.state.waveMiniRoadmapRequested, true, "Stale failed list A must not cancel cycle B");
  assert.strictEqual(rs.state.waveMiniRoadmapWaitingFor, "RM-0000-00051", "Stale failed list A must not clear cycle B wait marker");

  // Stale failed detail A must not settle cycle B either.
  rs.renderWaveMiniRoadmapDetail({ ok: false, error: "stale detail boom" }, 1);
  assert.strictEqual(rs.state.waveMiniRoadmapRequested, true, "Stale failed detail A must not cancel cycle B");
  assert.strictEqual(rs.state.waveMiniRoadmapWaitingFor, "RM-0000-00051", "Stale failed detail A must not clear cycle B wait marker");

  // Current-cycle failure must render UNKNOWN (fail closed).
  rs.renderWaveMiniRoadmapDetail({ ok: false, error: "current detail boom" }, 2);
  assert.strictEqual(rs.state.waveMiniRoadmapRequested, false, "Current-cycle detail failure must settle the cycle");
  assert.ok(findByClass(content, "wave-mini-roadmap-unknown"), "Current-cycle failure must render UNKNOWN");
}

// ── 10. Opening the popover, and canonical task updates while it is open ──
// The toggle listener is registered inline, so run its real source against a
// stub popover instead of asserting on text.
const toggleStart = app.indexOf('elements.waveMiniRoadmap.addEventListener("toggle"');
assert.ok(toggleStart !== -1, "Wave popover must listen for its toggle event");
const toggleSource = app.slice(toggleStart, app.indexOf("});", toggleStart) + 3);

function waveListRequests(messages) {
  return messages.filter((m) => m.type === "requestRoadmap" && m.purpose === "waveMiniRoadmap");
}

function waveDetailRequests(messages) {
  return messages.filter((m) => m.type === "requestRoadmapDetail" && m.purpose === "waveMiniRoadmap");
}

// Opening fetches the list, then the detail of the server-selected wave; a popover
// that is not open fetches nothing.
{
  const { sandbox: ts, content, messages } = buildRefreshSandbox();
  let onToggle = null;
  ts.elements.waveMiniRoadmap.open = false;
  ts.elements.waveMiniRoadmap.addEventListener = (type, handler) => {
    if (type === "toggle") onToggle = handler;
  };
  ts.elements.identityInfo.open = true;
  vm.runInContext(toggleSource, ts);
  assert.strictEqual(typeof onToggle, "function", "The toggle handler must register");
  onToggle();
  assert.strictEqual(messages.length, 0, "A popover that is not open must not request anything");

  ts.elements.waveMiniRoadmap.open = true;
  onToggle();
  assert.strictEqual(ts.elements.identityInfo.open, false, "Opening the wave popover must close the identity popover");
  assert.strictEqual(waveListRequests(messages).length, 1, "Opening must request the Roadmap list exactly once");
  const listReq = waveListRequests(messages)[0];
  assert.strictEqual(listReq.status, "", "Opening must not narrow the list by status");
  assert.strictEqual(listReq.includeArchived, false, "Opening must not pull archived waves");
  ts.renderWaveMiniRoadmapList(listPayload(), listReq.waveGeneration);
  assert.strictEqual(waveDetailRequests(messages).length, 1, "The list response must request exactly one detail");
  assert.strictEqual(waveDetailRequests(messages)[0].roadmapId, "RM-0000-00051", "Opening must fetch the server's current_wave.wave_id");
  ts.renderWaveMiniRoadmapDetail({ ok: true, item: detailItem }, listReq.waveGeneration);
  assert.strictEqual(goalCount(content), "1/2 goals done · 1 blocked task", "Opening must render the fetched checklist");
}

// 0.11.50 stays current until the server selects 0.11.51; each open shows the server's wave.
{
  const { sandbox: rs, content, messages } = buildRefreshSandbox();

  rs.requestWaveMiniRoadmap();
  rs.renderWaveMiniRoadmapList(listPayload([oldWaveEntry, Object.assign({}, newWaveEntry, { status: "proposed" })], currentFor(oldWaveDetail)), 1);
  assert.strictEqual(waveDetailRequests(messages).pop().roadmapId, "RM-0000-00050", "While the server selects 0.11.50, it is the current wave");
  rs.renderWaveMiniRoadmapDetail({ ok: true, item: oldWaveDetail }, 1);
  assert.strictEqual(targetOf(content), "Target 0.11.50", "The old wave must render while the server selects it");
  assert.ok(textOf(content).includes("Ship the 0.11.50 release"), "The old wave's own goal must render");
  assert.strictEqual(goalCount(content), "1/1 goal done", "A single goal must be counted in the singular");

  // The server selects 0.11.51; the next open must switch to it.
  rs.requestWaveMiniRoadmap();
  rs.renderWaveMiniRoadmapList(listPayload(), 2);
  assert.strictEqual(waveDetailRequests(messages).pop().roadmapId, "RM-0000-00051", "The next open must request the new wave's detail");
  rs.renderWaveMiniRoadmapDetail({ ok: true, item: detailItem }, 2);
  assert.strictEqual(targetOf(content), "Target 0.11.51", "The new wave must replace the old one");
  const text = textOf(content);
  assert.ok(text.includes("Popover shows a short goal checklist"), "The new wave's goals must render");
  assert.ok(!text.includes("Ship the 0.11.50 release"), "The old wave's goals must not linger");
}

// build_snapshot() carries task rows for the active statuses (and stale) only: a blocked, finished or
// archived task reaches the webview as an exact status_counts total, never as a row.
function productionSnapshot(counts, rows) {
  const status_counts = Object.assign(
    { pending: 0, processing: 0, review: 0, blocked: 0, superseded: 0, finished: 0, archived: 0, stale: 0 },
    counts,
  );
  status_counts.active = status_counts.pending + status_counts.processing + status_counts.review;
  return {
    roadmap: summaryOnlySnapshot.roadmap,
    tasks: Object.assign({ pending: [], processing: [], review: [], stale: [] }, rows),
    status_counts,
  };
}

const WATCHED_IDS = ["task-a", "task-b", "task-c"];
// task-a and task-b are finished and task-c is blocked, then task-c finishes; none of the three has a row.
const blockedSnapshot = productionSnapshot({ blocked: 1, finished: 2 });
const unblockedSnapshot = productionSnapshot({ finished: 3 });

// A popover opened on `first` whose wave list and detail have landed on generation 1.
function settledPopover(first) {
  const built = buildRefreshSandbox();
  const { sandbox: ps, messages, post } = built;
  ps.elements.waveMiniRoadmap.open = false;
  post({ type: "snapshot", payload: first });
  ps.elements.waveMiniRoadmap.open = true;
  ps.requestWaveMiniRoadmap();
  ps.renderWaveMiniRoadmapList(listPayload(), 1);
  ps.renderWaveMiniRoadmapDetail({ ok: true, item: detailItem }, 1);
  assert.strictEqual(waveListRequests(messages).length, 1, "Settling costs one list request");
  assert.strictEqual(waveDetailRequests(messages).length, 1, "Settling costs one detail request");
  return built;
}

// The webview meets snapshots only through its window message listener. A full snapshot renders first and only
// then compares task states (the comparison reads what renderSnapshot just stored); the summary never does.
{
  const { sandbox: ms, post } = buildRefreshSandbox();
  const calls = [];
  ms.renderSnapshot = (payload) => calls.push(["renderSnapshot", payload]);
  ms.refreshWaveMiniRoadmapOnTaskChange = () => calls.push(["refreshWaveMiniRoadmapOnTaskChange"]);
  ms.renderSummaryProjection = (payload) => calls.push(["renderSummaryProjection", payload]);
  const full = { marker: "full snapshot" };
  const summary = { marker: "summary snapshot" };
  post({ type: "snapshot", payload: full });
  post({ type: "snapshotSummary", payload: summary });
  assert.deepStrictEqual(calls, [
    ["renderSnapshot", full],
    ["refreshWaveMiniRoadmapOnTaskChange"],
    ["renderSummaryProjection", summary],
  ], "A snapshot message must refresh the open popover right after renderSnapshot, and a summary must not");
}

// Blocked -> finished while the popover is open. The watched tasks have no row before or after, yet exactly one
// guarded refresh follows the change: no polling, and replies from superseded cycles never overwrite the view.
{
  const { sandbox: rs, content, messages, post } = buildRefreshSandbox();
  const arrive = (snapshot) => post({ type: "snapshot", payload: snapshot });
  const watchedRows = () => rs.state.tasks.filter((task) => WATCHED_IDS.includes(task.task_id));

  // A snapshot lands while the popover is closed, then the user opens it.
  rs.elements.waveMiniRoadmap.open = false;
  arrive(blockedSnapshot);
  assert.strictEqual(messages.length, 0, "A closed popover must not request on snapshot arrival");
  assert.strictEqual(watchedRows().length, 0, "A production snapshot carries no row for the blocked or finished tasks");
  rs.elements.waveMiniRoadmap.open = true;
  rs.requestWaveMiniRoadmap();
  rs.renderWaveMiniRoadmapList(listPayload(), 1);
  rs.renderWaveMiniRoadmapDetail({ ok: true, item: detailItem }, 1);
  assert.strictEqual(goalCount(content), "1/2 goals done · 1 blocked task", "The blocked task must keep its goal open");
  const settled = messages.length;

  // Identical snapshots and churn on active tasks outside the wave are free.
  arrive(blockedSnapshot);
  arrive(blockedSnapshot);
  arrive(productionSnapshot({ blocked: 1, finished: 2, processing: 1 }, { processing: [{ task_id: "unrelated", status: "processing" }] }));
  arrive(productionSnapshot({ blocked: 1, finished: 2, review: 1 }, { review: [{ task_id: "unrelated", status: "review" }] }));
  assert.strictEqual(messages.length, settled, "Unchanged watched tasks must not request again");

  // task-c becomes finished, still without a row: exactly one refresh cycle on the next generation.
  arrive(unblockedSnapshot);
  assert.strictEqual(watchedRows().length, 0, "The finished task still has no row to compare");
  assert.strictEqual(waveListRequests(messages).length, 2, "A canonical status change must restart the cycle");
  assert.strictEqual(waveListRequests(messages)[1].waveGeneration, 2, "The refresh must ride the next generation");
  arrive(unblockedSnapshot);
  assert.strictEqual(waveListRequests(messages).length, 2, "The same change must not request twice");
  assert.strictEqual(goalCount(content), "1/2 goals done · 1 blocked task", "The previous view stays until the refreshed detail lands");

  // A late generation-1 detail is ignored; the generation-2 detail checks the goal.
  rs.renderWaveMiniRoadmapDetail({ ok: true, item: detailItem }, 1);
  rs.renderWaveMiniRoadmapList(listPayload(), 2);
  assert.strictEqual(waveDetailRequests(messages).pop().waveGeneration, 2, "The refresh cycle must request its own detail");
  rs.renderWaveMiniRoadmapDetail({ ok: true, item: withTasks(allFinished) }, 2);
  assert.strictEqual(goalCount(content), "2/2 goals done", "The refreshed canonical detail must check the goal");

  // A change while a refresh is still in flight supersedes it; the old replies are dropped.
  arrive(blockedSnapshot);
  arrive(unblockedSnapshot);
  assert.strictEqual(waveListRequests(messages).pop().waveGeneration, 4, "Each change must take the next generation");
  const detailsBefore = waveDetailRequests(messages).length;
  rs.renderWaveMiniRoadmapList(listPayload(), 3);
  assert.strictEqual(waveDetailRequests(messages).length, detailsBefore, "A superseded list must not request a detail");
  rs.renderWaveMiniRoadmapList(listPayload(), 4);
  assert.strictEqual(waveDetailRequests(messages).length, detailsBefore + 1, "Only the newest cycle requests its detail");
  rs.renderWaveMiniRoadmapDetail({ ok: true, item: detailItem }, 3);
  assert.strictEqual(goalCount(content), "2/2 goals done", "A superseded detail must not regress the popup");

  // Closing the popover stops all refreshing.
  rs.elements.waveMiniRoadmap.open = false;
  const total = messages.length;
  arrive(blockedSnapshot);
  assert.strictEqual(messages.length, total, "A closed popover must not restart the cycle");
}

// A watched task that still has a row (an active status) is compared row by row.
{
  const pendingC = productionSnapshot({ pending: 1, finished: 2 }, { pending: [{ task_id: "task-c", status: "pending" }] });
  const processingC = productionSnapshot({ processing: 1, finished: 2 }, { processing: [{ task_id: "task-c", status: "processing" }] });
  const { messages, post } = settledPopover(pendingC);
  post({ type: "snapshot", payload: pendingC });
  assert.strictEqual(waveListRequests(messages).length, 1, "An unchanged watched row must not request again");
  post({ type: "snapshot", payload: processingC });
  assert.strictEqual(waveListRequests(messages).length, 2, "A watched row changing status must restart the cycle");
  post({ type: "snapshot", payload: processingC });
  assert.strictEqual(waveListRequests(messages).length, 2, "The same change must not request twice");
}

// A watched task that leaves its row and reaches a rowless status moves both signals in one snapshot: still one refresh.
{
  const processingC = productionSnapshot({ processing: 1, finished: 2 }, { processing: [{ task_id: "task-c", status: "processing" }] });
  const { messages, post } = settledPopover(processingC);
  post({ type: "snapshot", payload: unblockedSnapshot });
  assert.strictEqual(waveListRequests(messages).length, 2, "A watched task finishing must restart the cycle once");
  assert.strictEqual(waveListRequests(messages)[1].waveGeneration, 2, "Both signals ride one generation");
}

// Every rowless status total is a signal on its own.
for (const status of ["blocked", "superseded", "finished", "archived"]) {
  const { messages, post } = settledPopover(blockedSnapshot);
  const moved = productionSnapshot({ blocked: 1, finished: 2 });
  moved.status_counts[status] += 1;
  post({ type: "snapshot", payload: moved });
  assert.strictEqual(waveListRequests(messages).length, 2, `A moved ${status} total must restart the cycle`);
}

// An unrelated task reaching a rowless status moves the same total a watched one would: one refresh, then quiet.
{
  const { messages, post } = settledPopover(blockedSnapshot);
  const unrelatedFinished = productionSnapshot({ blocked: 1, finished: 3 });
  post({ type: "snapshot", payload: unrelatedFinished });
  post({ type: "snapshot", payload: unrelatedFinished });
  assert.strictEqual(waveListRequests(messages).length, 2, "A moved rowless total costs one refresh, not one per snapshot");
}

// A popover opened before the first snapshot only arms its baseline from that snapshot.
{
  const { sandbox: rs, messages, post } = buildRefreshSandbox();
  rs.state.snapshot = null;
  rs.requestWaveMiniRoadmap();
  assert.strictEqual(rs.state.waveMiniRoadmapTaskStates, null, "No snapshot yet means no baseline");
  rs.renderWaveMiniRoadmapList(listPayload(), 1);
  post({ type: "snapshot", payload: blockedSnapshot });
  assert.strictEqual(waveListRequests(messages).length, 1, "The first snapshot must not request a second cycle");
  assert.strictEqual(
    rs.state.waveMiniRoadmapTaskStates.totals,
    rs.waveTaskStates(blockedSnapshot, []).totals,
    "The first snapshot must arm the baseline",
  );
  post({ type: "snapshot", payload: unblockedSnapshot });
  assert.strictEqual(waveListRequests(messages).length, 2, "Once armed, a change restarts the cycle");
}

// With no active wave known there is nothing to watch, so task changes cost nothing.
{
  const { sandbox: rs, messages, post } = buildRefreshSandbox();
  rs.requestWaveMiniRoadmap();
  const requested = messages.length;
  post({ type: "snapshot", payload: blockedSnapshot });
  post({ type: "snapshot", payload: unblockedSnapshot });
  assert.strictEqual(messages.length, requested, "No active wave means no refresh on task changes");
}

// ── 11. The server's exact current_wave id is the only selection receipt ──
// A projection naming the lower 0.11.50 wave fetches that wave although a
// higher active 0.11.51 row sits in the same list.
{
  const { sandbox: rs, content, messages } = buildRefreshSandbox();
  rs.requestWaveMiniRoadmap();
  rs.renderWaveMiniRoadmapList(listPayload(listEntries, currentFor(oldWaveDetail)), 1);
  assert.strictEqual(waveDetailRequests(messages).pop().roadmapId, "RM-0000-00050", "Detail must follow the server's wave id, never the highest local semver");
  rs.renderWaveMiniRoadmapDetail({ ok: true, item: oldWaveDetail, current_wave: currentFor(oldWaveDetail) }, 1);
  assert.strictEqual(targetOf(content), "Target 0.11.50");
  assert.strictEqual(goalCount(content), "1/1 goal done");
}

// No projection, or a non-ready one, requests no detail and renders the typed UNKNOWN.
for (const [name, extra, reason] of [
  ["a list without current_wave", {}, "The server reported no current-wave projection"],
  ["a null current_wave", { current_wave: null }, "The server reported no current-wave projection"],
  ["an ambiguous projection", { current_wave: unknownCurrent("ambiguous_active_wave") }, "(ambiguous_active_wave)"],
]) {
  const { sandbox: rs, content, messages } = buildRefreshSandbox();
  rs.requestWaveMiniRoadmap();
  rs.renderWaveMiniRoadmapList(Object.assign({ ok: true, entries: listEntries }, extra), 1);
  assert.strictEqual(waveDetailRequests(messages).length, 0, `${name} must not request a detail`);
  assert.strictEqual(rs.state.waveMiniRoadmapRequested, false, `${name} must settle the cycle`);
  const shown = findByClass(content, "wave-mini-roadmap-reason");
  assert.ok(shown && shown.textContent.includes(reason), `${name} must say why`);
  assert.strictEqual(findByClass(content, "wave-mini-roadmap-goals"), null, `${name} must not render a checklist`);
}

// A detail whose own projection now names another wave is stale: UNKNOWN, never the old checklist.
{
  const { sandbox: rs, content } = buildRefreshSandbox();
  rs.requestWaveMiniRoadmap();
  rs.renderWaveMiniRoadmapList(listPayload(), 1);
  rs.renderWaveMiniRoadmapDetail({ ok: true, item: withTasks(allFinished), current_wave: currentFor(oldWaveDetail) }, 1);
  assert.ok(findByClass(content, "wave-mini-roadmap-unknown"), "A current wave that moved mid-cycle must fail closed");
  assert.strictEqual(findByClass(content, "wave-mini-roadmap-goals"), null, "A stale detail must not render its checklist");

  rs.requestWaveMiniRoadmap();
  rs.renderWaveMiniRoadmapList({ ok: false, error: "list boom" }, 2);
  assert.strictEqual(rs.state.waveMiniRoadmapCurrent, null, "A failed list must drop the previous projection");
}

console.log("Wave mini-roadmap contract verified");
