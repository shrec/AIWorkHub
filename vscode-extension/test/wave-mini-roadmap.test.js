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

// ── 2. Canonical wave selection + counts via extracted pure helpers ──
const helperStart = app.indexOf("wave-mini-roadmap-helpers-begin");
const helperEnd = app.indexOf("wave-mini-roadmap-helpers-end");
assert.ok(helperStart !== -1 && helperEnd !== -1 && helperEnd > helperStart, "Helper block markers must exist");
const helperBlock = app.slice(app.indexOf("\n", helperStart) + 1, helperEnd);
const sandbox = {};
vm.createContext(sandbox);
vm.runInContext(helperBlock, sandbox);
assert.strictEqual(typeof sandbox.waveSelectActive, "function", "waveSelectActive must be extractable");
assert.strictEqual(typeof sandbox.waveTaskCounts, "function", "waveTaskCounts must be extractable");
assert.strictEqual(typeof sandbox.waveSemver, "function", "waveSemver must be extractable");

// Latest active versioned wave wins by semver order, never by hard-coded ID.
const picked = sandbox.waveSelectActive([
  { id: "A", milestone: "0.11.49", title: "older", status: "in_progress" },
  { id: "B", milestone: "0.11.50", title: "current", status: "in_progress" },
  { id: "C", milestone: "v0.11.48", title: "older", status: "current" },
  { id: "D", milestone: "next-wave", title: "unversioned", status: "in_progress" },
]);
assert.strictEqual(picked && picked.id, "B", "Latest semver milestone wave must be selected");

const parsed = sandbox.waveSemver("v1.2.3");
assert.strictEqual(parsed.major, 1);
assert.strictEqual(parsed.minor, 2);
assert.strictEqual(parsed.patch, 3);
const parsedSuffix = sandbox.waveSemver("1.2.3-alpha.1");
assert.strictEqual(parsedSuffix.major, 1);
assert.strictEqual(parsedSuffix.minor, 2);
assert.strictEqual(parsedSuffix.patch, 3);
assert.strictEqual(sandbox.waveSemver("soon"), null);
assert.strictEqual(sandbox.waveSemver(undefined), null);

// Only canonical active/current waves are eligible. A higher-version
// proposed/completed/archived milestone must never displace the in_progress wave.
const activePicked = sandbox.waveSelectActive([
  { id: "proposed-next", milestone: "0.11.60", status: "proposed" },
  { id: "completed-next", milestone: "0.11.70", status: "completed" },
  { id: "archived-next", milestone: "0.11.80", status: "archived" },
  { id: "current", milestone: "0.11.50", status: "in_progress" },
  { id: "old", milestone: "0.11.49", status: "in_progress" },
]);
assert.strictEqual(
  activePicked && activePicked.id,
  "current",
  "A newer proposed/completed/archived wave must not displace the current in_progress wave",
);
assert.strictEqual(sandbox.waveIsActive({ status: "in_progress" }), true);
assert.strictEqual(sandbox.waveIsActive({ status: "current" }), true);
assert.strictEqual(sandbox.waveIsActive({ status: "active" }), true);
assert.strictEqual(sandbox.waveIsActive({ status: "proposed" }), false);
assert.strictEqual(sandbox.waveIsActive({ status: "completed" }), false);
assert.strictEqual(sandbox.waveIsActive({ status: "archived" }), false);
assert.strictEqual(sandbox.waveIsActive({ status: "In_Progress" }), true, "Status matching is case-insensitive");
assert.strictEqual(sandbox.waveIsActive(null), false);

// Fail-closed: no versioned milestone or an empty list → null (UNKNOWN), never
// an empty-green state.
assert.strictEqual(sandbox.waveSelectActive([{ milestone: "next", status: "in_progress" }]), null);
assert.strictEqual(sandbox.waveSelectActive([]), null);
assert.strictEqual(sandbox.waveSelectActive(null), null);
// Fail-closed: ambiguous tie at the top → null (UNKNOWN).
assert.strictEqual(
  sandbox.waveSelectActive([
    { milestone: "0.11.50", status: "in_progress" },
    { milestone: "0.11.50", status: "in_progress" },
  ]),
  null,
);

// Counts: only authenticated finished/accepted tasks count as complete.
const counts = sandbox.waveTaskCounts([
  { task_id: "t1", status: "finished" },
  { task_id: "t2", status: "accepted" },
  { task_id: "t3", status: "review_ready" },
  { task_id: "t4", status: "pending" },
  { task_id: "t5", status: "blocked" },
  { task_id: "t6", status: "archived" },
  { task_id: "t7", status: "missing" },
  { task_id: "t8" },
]);
assert.strictEqual(counts.complete, 2, "Only finished/accepted count as complete");
assert.strictEqual(counts.total, 8);
assert.strictEqual(counts.states.finished, 1);
assert.strictEqual(counts.states.accepted, 1);
assert.strictEqual(counts.states.unknown, 1, "Missing task status must fail closed to unknown");

// Task-status join completeness: counts are only trustworthy when every linked
// task_id has a matching task row with a non-empty status.
assert.strictEqual(typeof sandbox.waveTaskJoinComplete, "function", "waveTaskJoinComplete must be extractable");
assert.strictEqual(
  sandbox.waveTaskJoinComplete({
    task_ids: ["t1", "t2"],
    tasks: [{ task_id: "t1", status: "finished" }, { task_id: "t2", status: "pending" }],
  }),
  true,
  "Complete task join must pass",
);
assert.strictEqual(
  sandbox.waveTaskJoinComplete({
    task_ids: ["t1", "t2"],
    tasks: [{ task_id: "t1", status: "finished" }],
  }),
  false,
  "Missing task row must fail closed",
);
assert.strictEqual(
  sandbox.waveTaskJoinComplete({
    task_ids: ["t1", "t2"],
    tasks: [{ task_id: "t1", status: "finished" }, { task_id: "t2", status: "" }],
  }),
  false,
  "Empty task status must fail closed",
);
assert.strictEqual(
  sandbox.waveTaskJoinComplete({ task_ids: ["t1", "t2"], tasks: "not-an-array" }),
  false,
  "Non-array tasks must fail closed",
);
assert.strictEqual(
  sandbox.waveTaskJoinComplete({ task_ids: ["t1"], tasks: undefined }),
  false,
  "Missing tasks with linked ids must fail closed",
);
assert.strictEqual(
  sandbox.waveTaskJoinComplete({ task_ids: [], tasks: undefined }),
  true,
  "No linked task ids needs no join",
);
assert.strictEqual(sandbox.waveTaskJoinComplete(null), true, "No wave needs no join");

// ── 3. Refresh/reload reconciles canonical list/detail state; no shadow checklist ──
assert.ok(app.includes("function renderWaveMiniRoadmap(snapshot)"), "Wave renderer must exist");
assert.ok(app.includes("renderWaveMiniRoadmap(snapshot);"), "Wave renderer must run on every snapshot refresh");
assert.ok(app.includes("waveSelectActive(state.waveMiniRoadmapEntries)"), "Wave selection must read the popup's isolated list state, never snapshot.roadmap.items");
assert.ok(!app.includes("waveSelectActive(state.roadmapEntries)"), "Popup must never select from the Roadmap dialog's shared list");
assert.ok(app.includes("state.waveMiniRoadmapDetail"), "Wave acceptance/tasks must come from canonical detail state");
assert.ok(app.includes('type: "requestRoadmap"'), "Popup must reuse the list bridge");
assert.ok(app.includes('type: "requestRoadmapDetail"'), "Popup must reuse the detail bridge");
assert.ok(app.includes("requestWaveMiniRoadmap"), "Popup must request list+detail on open");
assert.ok(app.includes("waveTaskJoinComplete(wave)"), "Renderer must require a complete task-status join");
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

// ── 5. Real renderSnapshot → renderWaveMiniRoadmap path against a DOM mock ──
// The canonical summary snapshot exposes only {available,error,active,total,
// truncated} for roadmap — never items. The wave renderer must select the
// active wave from state.waveMiniRoadmapEntries (isolated popup list bridge) and join acceptance/
// task states from state.waveMiniRoadmapDetail (detail bridge).
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
    children: [],
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

const renderSource = [
  helperBlock,
  extractFunction(app, "createElement"),
  extractFunction(app, "asArray"),
  extractFunction(app, "renderWaveMiniRoadmap"),
  extractFunction(app, "renderWaveMiniRoadmapState"),
  extractFunction(app, "renderSnapshot"),
].join("\n");

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
  // Stub every sibling renderer renderSnapshot invokes; only the wave renderer
  // and the createElement/asArray helpers run for real.
  for (const name of [
    "flattenTasks", "renderStorageState", "renderSummary", "renderManagerIdentity",
    "renderCallbackObservability", "renderKnownRepositories", "renderSourceHealth",
    "renderFilterOptions", "renderTaskTable", "renderStats", "renderKpis",
    "renderUsage", "renderPlanDag", "renderWorkforce", "renderToolUse",
    "renderStorage", "renderSystemLogs", "renderReturns", "renderRuns",
    "renderWarnings", "applyHistorySnapshot", "clearTaskDetail",
  ]) {
    sandbox2[name] = function noop() {};
  }
  sandbox2.renderStorageState = function renderStorageStateStub() { return true; };
  sandbox2.flattenTasks = function flattenTasksStub() { return []; };
  vm.createContext(sandbox2);
  vm.runInContext(renderSource, sandbox2);
  return { sandbox: sandbox2, content };
}

const summaryOnlySnapshot = {
  roadmap: { available: true, error: null, active: 1, total: 2, truncated: false },
};

const listEntries = [
  { id: "RM-0000-00049", title: "Wave 0.11.49", status: "in_progress", milestone: "0.11.49", task_ids: [] },
  { id: "RM-0000-00050", title: "Wave 0.11.50", status: "in_progress", milestone: "0.11.50", task_ids: ["t1", "t2"] },
];

const detailItem = {
  id: "RM-0000-00050",
  title: "Wave 0.11.50",
  status: "in_progress",
  milestone: "0.11.50",
  acceptance: ["Ship release", "Pass CI"],
  task_ids: ["t1", "t2"],
  tasks: [
    { task_id: "t1", status: "finished" },
    { task_id: "t2", status: "pending" },
  ],
};

// Initial canonical state: current 0.11.50 wave, 1/2 finished.
{
  const { sandbox: isb, content } = buildIntegrationSandbox();
  isb.state.waveMiniRoadmapEntries = listEntries;
  isb.state.waveMiniRoadmapDetail = detailItem;
  isb.renderSnapshot(summaryOnlySnapshot);
  const milestone = findByClass(content, "wave-mini-roadmap-milestone");
  assert.strictEqual(milestone && milestone.textContent, "0.11.50", "Initial wave must render current 0.11.50");
  const count = findByClass(content, "wave-mini-roadmap-count");
  assert.strictEqual(count && count.textContent, "1/2 finished", "Initial counts must be 1/2 finished");
  const goals = findByClass(content, "wave-mini-roadmap-goals");
  assert.strictEqual(goals && goals.children.length, 2, "Both acceptance goals must render");
  const taskList = findByClass(content, "wave-mini-roadmap-tasks");
  assert.ok(taskList, "Task list must render");
  assert.strictEqual(taskList.children.length, 2, "Both linked tasks must render");
  assert.ok(taskList.children[0].className.includes("wave-task-complete"), "Finished task must render complete");
  assert.ok(taskList.children[1].className.includes("wave-task-incomplete"), "Pending task must render incomplete");

  // Refresh: the same renderer reconciles new task states from refreshed detail.
  isb.state.waveMiniRoadmapDetail = Object.assign({}, detailItem, {
    tasks: [
      { task_id: "t1", status: "finished" },
      { task_id: "t2", status: "finished" },
    ],
  });
  isb.renderSnapshot(summaryOnlySnapshot);
  const refreshedCount = findByClass(content, "wave-mini-roadmap-count");
  assert.strictEqual(refreshedCount && refreshedCount.textContent, "2/2 finished", "Refresh must reconcile task states");
}

// accepted/finished both count complete.
{
  const { sandbox: isb, content } = buildIntegrationSandbox();
  isb.state.waveMiniRoadmapEntries = listEntries;
  isb.state.waveMiniRoadmapDetail = Object.assign({}, detailItem, {
    tasks: [
      { task_id: "t1", status: "accepted" },
      { task_id: "t2", status: "finished" },
    ],
  });
  isb.renderSnapshot(summaryOnlySnapshot);
  const count = findByClass(content, "wave-mini-roadmap-count");
  assert.strictEqual(count && count.textContent, "2/2 finished", "accepted/finished must both count complete");
}

// review_ready / pending / blocked / archived / missing / unknown never complete.
{
  const { sandbox: isb, content } = buildIntegrationSandbox();
  isb.state.waveMiniRoadmapEntries = [
    { id: "RM-0000-00050", title: "Wave 0.11.50", status: "in_progress", milestone: "0.11.50", task_ids: ["t1", "t2", "t3", "t4", "t5", "t6", "t7"] },
  ];
  isb.state.waveMiniRoadmapDetail = {
    id: "RM-0000-00050",
    milestone: "0.11.50",
    acceptance: [],
    task_ids: ["t1", "t2", "t3", "t4", "t5", "t6", "t7"],
    tasks: [
      { task_id: "t1", status: "review_ready" },
      { task_id: "t2", status: "pending" },
      { task_id: "t3", status: "blocked" },
      { task_id: "t4", status: "archived" },
      { task_id: "t5", status: "missing" },
      { task_id: "t6", status: "unknown" },
      { task_id: "t7", status: "finished" },
    ],
  };
  isb.renderSnapshot(summaryOnlySnapshot);
  const count = findByClass(content, "wave-mini-roadmap-count");
  assert.strictEqual(count && count.textContent, "1/7 finished", "Only finished/accepted count complete");
}

// Partial task join → UNKNOWN (fail closed), never an empty-green 0/0.
{
  const { sandbox: isb, content } = buildIntegrationSandbox();
  isb.state.waveMiniRoadmapEntries = listEntries;
  isb.state.waveMiniRoadmapDetail = Object.assign({}, detailItem, {
    tasks: [{ task_id: "t1", status: "finished" }],
  });
  isb.renderSnapshot(summaryOnlySnapshot);
  const unknown = findByClass(content, "wave-mini-roadmap-unknown");
  assert.ok(unknown, "Partial task join must render UNKNOWN");
  assert.strictEqual(unknown.textContent, "UNKNOWN");
}

// Detail not yet loaded (list only) → UNKNOWN (fail closed).
{
  const { sandbox: isb, content } = buildIntegrationSandbox();
  isb.state.waveMiniRoadmapEntries = listEntries;
  isb.state.waveMiniRoadmapDetail = null;
  isb.renderSnapshot(summaryOnlySnapshot);
  assert.ok(findByClass(content, "wave-mini-roadmap-unknown"), "Missing detail must fail closed to UNKNOWN");
}

// Detail for a different wave → UNKNOWN (fail closed).
{
  const { sandbox: isb, content } = buildIntegrationSandbox();
  isb.state.waveMiniRoadmapEntries = listEntries;
  isb.state.waveMiniRoadmapDetail = Object.assign({}, detailItem, { id: "RM-0000-00049" });
  isb.renderSnapshot(summaryOnlySnapshot);
  assert.ok(findByClass(content, "wave-mini-roadmap-unknown"), "Mismatched detail must fail closed to UNKNOWN");
}

// Truncated roadmap → UNKNOWN (fail closed).
{
  const { sandbox: isb, content } = buildIntegrationSandbox();
  isb.state.waveMiniRoadmapEntries = listEntries;
  isb.state.waveMiniRoadmapDetail = detailItem;
  isb.renderSnapshot({ roadmap: { available: true, error: null, active: 1, total: 2, truncated: true } });
  assert.ok(findByClass(content, "wave-mini-roadmap-unknown"), "Truncated roadmap must fail closed to UNKNOWN");
}

// Higher proposed/completed waves must not displace the in_progress wave.
{
  const { sandbox: isb, content } = buildIntegrationSandbox();
  isb.state.waveMiniRoadmapEntries = [
    { id: "RM-0000-00050", title: "Wave 0.11.50", status: "in_progress", milestone: "0.11.50", task_ids: ["t1"] },
    { id: "RM-0000-00060", title: "Wave 0.11.60", status: "proposed", milestone: "0.11.60", task_ids: [] },
    { id: "RM-0000-00070", title: "Wave 0.11.70", status: "completed", milestone: "0.11.70", task_ids: [] },
  ];
  isb.state.waveMiniRoadmapDetail = {
    id: "RM-0000-00050",
    milestone: "0.11.50",
    acceptance: [],
    task_ids: ["t1"],
    tasks: [{ task_id: "t1", status: "finished" }],
  };
  isb.renderSnapshot(summaryOnlySnapshot);
  const milestone = findByClass(content, "wave-mini-roadmap-milestone");
  assert.strictEqual(milestone && milestone.textContent, "0.11.50", "Proposed/completed higher waves must not displace in_progress");
}

// Isolation regression: Roadmap dialog list filter/fail must never alter the
// popup, while the popup's own failed list response must fail closed.
{
  const { sandbox: isb, content } = buildIntegrationSandbox();
  isb.state.waveMiniRoadmapEntries = listEntries;
  isb.state.waveMiniRoadmapDetail = detailItem;
  isb.renderSnapshot(summaryOnlySnapshot);
  const milestone = findByClass(content, "wave-mini-roadmap-milestone");
  assert.strictEqual(milestone && milestone.textContent, "0.11.50", "Isolation: wave must render 0.11.50 before dialog list changes");
  const count = findByClass(content, "wave-mini-roadmap-count");
  assert.strictEqual(count && count.textContent, "1/2 finished", "Isolation: initial counts must be 1/2 finished");

  // Simulate the Roadmap dialog filtering to a different wave, then failing its
  // list entirely. Both overwrite the shared state.roadmapEntries, but the
  // popup's isolated projection must remain intact.
  isb.state.roadmapEntries = [listEntries[0]];
  isb.renderSnapshot(summaryOnlySnapshot);
  const afterFilter = findByClass(content, "wave-mini-roadmap-milestone");
  assert.strictEqual(afterFilter && afterFilter.textContent, "0.11.50", "Isolation: dialog filter must not change the popup wave");
  isb.state.roadmapEntries = [];
  isb.renderSnapshot(summaryOnlySnapshot);
  const afterFail = findByClass(content, "wave-mini-roadmap-milestone");
  assert.strictEqual(afterFail && afterFail.textContent, "0.11.50", "Isolation: dialog list failure must not change the popup wave");
  const afterFailCount = findByClass(content, "wave-mini-roadmap-count");
  assert.strictEqual(afterFailCount && afterFailCount.textContent, "1/2 finished", "Isolation: dialog list failure must not alter popup counts");

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
const refreshFlowSource = [
  helperBlock,
  extractFunction(app, "createElement"),
  extractFunction(app, "asArray"),
  extractFunction(app, "waveCycleCurrent"),
  extractFunction(app, "renderWaveMiniRoadmap"),
  extractFunction(app, "renderWaveMiniRoadmapState"),
  extractFunction(app, "renderWaveMiniRoadmapList"),
  extractFunction(app, "reconcileWaveMiniRoadmap"),
  extractFunction(app, "renderWaveMiniRoadmapDetail"),
  extractFunction(app, "requestWaveMiniRoadmap"),
  extractFunction(app, "requestRefresh"),
].join("\n");

function buildRefreshSandbox() {
  const content = makeMockElement("div");
  const messages = [];
  const elements = {
    refreshButton: { disabled: false, textContent: "Refresh" },
    tableLoading: { hidden: true },
    waveMiniRoadmap: { open: true },
    waveMiniRoadmapContent: content,
    roadmapDetailPanel: { replaceChildren() {} },
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
  const sandbox3 = {
    document: {
      createElement(tag) { return makeMockElement(tag); },
      createDocumentFragment() { return makeMockElement("#fragment"); },
    },
    window: { setTimeout() {} },
    vscode: { postMessage(message) { messages.push(message); } },
    elements,
    state,
    console,
    setConnection() {},
    appendNeedfixObject() {},
    appendNeedfixEvents() {},
  };
  vm.createContext(sandbox3);
  vm.runInContext(refreshFlowSource, sandbox3);
  return { sandbox: sandbox3, content, messages };
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
  rs.renderWaveMiniRoadmapList({ ok: true, entries: listEntries }, listReq.waveGeneration);
  const detailReq = messages.find((m) => m.type === "requestRoadmapDetail");
  assert.strictEqual(detailReq && detailReq.roadmapId, "RM-0000-00050", "List response must request the active wave's detail");
  assert.strictEqual(detailReq && detailReq.purpose, "waveMiniRoadmap", "Wave detail request must tag its purpose so the response routes to the isolated handler");
  assert.strictEqual(detailReq && detailReq.waveGeneration, listReq.waveGeneration, "Wave detail request must echo the same generation as its list");

  // Detail response arrives with the second task now finished; the open popup
  // count must update without closing/reopening.
  rs.renderWaveMiniRoadmapDetail({
    ok: true,
    item: Object.assign({}, detailItem, {
      tasks: [
        { task_id: "t1", status: "finished" },
        { task_id: "t2", status: "finished" },
      ],
    }),
  }, detailReq.waveGeneration);
  const count = findByClass(content, "wave-mini-roadmap-count");
  assert.strictEqual(count && count.textContent, "2/2 finished", "Refresh flow must update the open popup's count");
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
  ds.state.waveMiniRoadmapWaitingFor = "RM-0000-00050";
  ds.state.waveMiniRoadmapRequested = true;
  ds.renderWaveMiniRoadmapDetail({
    ok: true,
    item: Object.assign({}, detailItem, {
      tasks: [
        { task_id: "t1", status: "finished" },
        { task_id: "t2", status: "finished" },
      ],
    }),
  });
  assert.strictEqual(ds.state.roadmapDetail, dialogDetail, "Wave detail success must not overwrite the dialog detail");
  assert.strictEqual(dialogDraws(), 0, "Wave detail success must not redraw the dialog panel");
  assert.strictEqual(ds.state.waveMiniRoadmapWaitingFor, null, "Wave detail success must clear the wait marker");
  const count = findByClass(content, "wave-mini-roadmap-count");
  assert.strictEqual(count && count.textContent, "2/2 finished", "Wave detail success must render the popup");
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
  rs.renderWaveMiniRoadmapList({ ok: true, entries: listEntries }, listA.waveGeneration);
  const detailA = messages.find((m) => m.type === "requestRoadmapDetail" && m.purpose === "waveMiniRoadmap");
  assert.strictEqual(detailA && detailA.roadmapId, "RM-0000-00050", "Cycle A must request active wave detail");
  assert.strictEqual(detailA && detailA.waveGeneration, 1, "Cycle A detail must carry generation 1");

  // Cycle B starts (generation 2).
  rs.requestWaveMiniRoadmap();
  const listB = messages.filter((m) => m.type === "requestRoadmap" && m.purpose === "waveMiniRoadmap").pop();
  assert.strictEqual(listB && listB.waveGeneration, 2, "Cycle B list must carry generation 2");

  // Stale detail A arrives before list B: ignored, leaving cycle B alive.
  rs.renderWaveMiniRoadmapDetail({
    ok: true,
    item: Object.assign({}, detailItem, {
      tasks: [
        { task_id: "t1", status: "finished" },
        { task_id: "t2", status: "finished" },
      ],
    }),
  }, detailA.waveGeneration);
  assert.strictEqual(rs.state.waveMiniRoadmapRequested, true, "Stale detail A before list B must not cancel cycle B");
  assert.strictEqual(rs.state.waveMiniRoadmapWaitingFor, null, "Stale detail A before list B must not clear the wait marker");

  // List B arrives: cycle B must now request its own fresh detail.
  rs.renderWaveMiniRoadmapList({ ok: true, entries: listEntries }, listB.waveGeneration);
  const detailB = messages.filter((m) => m.type === "requestRoadmapDetail" && m.purpose === "waveMiniRoadmap").pop();
  assert.strictEqual(detailB && detailB.waveGeneration, 2, "Cycle B must request a fresh detail after stale detail A");
  assert.strictEqual(detailB && detailB.roadmapId, "RM-0000-00050", "Cycle B detail must target the active wave");

  // Fresh detail B renders 2/2.
  rs.renderWaveMiniRoadmapDetail({
    ok: true,
    item: Object.assign({}, detailItem, {
      tasks: [
        { task_id: "t1", status: "finished" },
        { task_id: "t2", status: "finished" },
      ],
    }),
  }, detailB.waveGeneration);
  const count = findByClass(content, "wave-mini-roadmap-count");
  assert.strictEqual(count && count.textContent, "2/2 finished", "Cycle B must render the fresh 2/2 count");
}

// A-detail-after-B-detail: a stale detail from cycle A that arrives after cycle
// B has already rendered must be ignored and must not regress the popup.
{
  const { sandbox: rs, content, messages } = buildRefreshSandbox();

  // Cycle A.
  rs.requestWaveMiniRoadmap();
  const genA = messages.find((m) => m.type === "requestRoadmap" && m.purpose === "waveMiniRoadmap").waveGeneration;
  rs.renderWaveMiniRoadmapList({ ok: true, entries: listEntries }, genA);

  // Cycle B renders 2/2.
  rs.requestWaveMiniRoadmap();
  const genB = messages.filter((m) => m.type === "requestRoadmap" && m.purpose === "waveMiniRoadmap").pop().waveGeneration;
  rs.renderWaveMiniRoadmapList({ ok: true, entries: listEntries }, genB);
  rs.renderWaveMiniRoadmapDetail({
    ok: true,
    item: Object.assign({}, detailItem, {
      tasks: [
        { task_id: "t1", status: "finished" },
        { task_id: "t2", status: "finished" },
      ],
    }),
  }, genB);
  let count = findByClass(content, "wave-mini-roadmap-count");
  assert.strictEqual(count && count.textContent, "2/2 finished", "Cycle B must render 2/2 first");

  // Stale detail A (1/2 finished) arrives after B: ignored, popup stays 2/2.
  rs.renderWaveMiniRoadmapDetail({
    ok: true,
    item: Object.assign({}, detailItem, {
      tasks: [
        { task_id: "t1", status: "finished" },
        { task_id: "t2", status: "pending" },
      ],
    }),
  }, genA);
  count = findByClass(content, "wave-mini-roadmap-count");
  assert.strictEqual(count && count.textContent, "2/2 finished", "Stale detail A after B must not regress the popup");
}

// Stale failures: a superseded cycle's failed list or detail must not poison the
// current cycle; only a current-cycle failure renders UNKNOWN.
{
  const { sandbox: rs, content, messages } = buildRefreshSandbox();

  rs.requestWaveMiniRoadmap(); // cycle A (generation 1)
  rs.requestWaveMiniRoadmap(); // cycle B (generation 2)
  rs.renderWaveMiniRoadmapList({ ok: true, entries: listEntries }, 2);
  const detailB = messages.filter((m) => m.type === "requestRoadmapDetail" && m.purpose === "waveMiniRoadmap").pop();
  assert.strictEqual(detailB && detailB.waveGeneration, 2, "Cycle B must request detail");

  // Stale failed list A must not cancel or clear cycle B.
  rs.renderWaveMiniRoadmapList({ ok: false, error: "stale list boom", entries: [] }, 1);
  assert.strictEqual(rs.state.waveMiniRoadmapRequested, true, "Stale failed list A must not cancel cycle B");
  assert.strictEqual(rs.state.waveMiniRoadmapWaitingFor, "RM-0000-00050", "Stale failed list A must not clear cycle B wait marker");

  // Stale failed detail A must not settle cycle B either.
  rs.renderWaveMiniRoadmapDetail({ ok: false, error: "stale detail boom" }, 1);
  assert.strictEqual(rs.state.waveMiniRoadmapRequested, true, "Stale failed detail A must not cancel cycle B");
  assert.strictEqual(rs.state.waveMiniRoadmapWaitingFor, "RM-0000-00050", "Stale failed detail A must not clear cycle B wait marker");

  // Current-cycle failure must render UNKNOWN (fail closed).
  rs.renderWaveMiniRoadmapDetail({ ok: false, error: "current detail boom" }, 2);
  assert.strictEqual(rs.state.waveMiniRoadmapRequested, false, "Current-cycle detail failure must settle the cycle");
  assert.ok(findByClass(content, "wave-mini-roadmap-unknown"), "Current-cycle failure must render UNKNOWN");
}

console.log("Wave mini-roadmap contract verified");
