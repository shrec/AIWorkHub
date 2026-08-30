"use strict";

// B850: static contract checks for the cross-repository dashboard authority
// fix. Opening/selecting a repository must never bootstrap it and must
// never read task data from a legacy path; an uninitialized repository must
// show an empty UNINITIALIZED dashboard with exactly one explicit
// "Initialize AIWorkHub" action bound to one bounded MCP tool. These checks
// read source text (the same pattern as extension-static.test.js) rather
// than loading the `vscode` module, so they run standalone under plain
// Node.

const assert = require("assert");
const fs = require("fs");
const path = require("path");

const extRoot = path.resolve(__dirname, "..");
const pySrcRoot = path.resolve(extRoot, "..", "src", "aiworkhub");

const readExt = (rel) => fs.readFileSync(path.join(extRoot, rel), "utf8");
const readPy = (rel) => fs.readFileSync(path.join(pySrcRoot, rel), "utf8");

const ext = readExt("extension.js");
const app = readExt("media/app.js");

function assertAbsent(haystack, values, label) {
  const hits = values.filter((value) => haystack.includes(value));
  assert.deepStrictEqual(hits, [], `${label}: found forbidden pattern(s) ${hits.join(", ")}`);
}

function assertPresent(haystack, values, label) {
  const missing = values.filter((value) => !haystack.includes(value));
  assert.deepStrictEqual(missing, [], `${label}: missing required pattern(s) ${missing.join(", ")}`);
}

// ── 1. Activation / repository selection: no bootstrap, no filesystem write,
//      no directory-existence storage-ready heuristic. ─────────────────────
assertAbsent(
  ext,
  ["function bootstrapRepository", "bootstrapRepository(folder", "bootstrapRepository(match"],
  "activation_time_bootstrap forbidden pattern present",
);
const readRepositoryManifestInfoMatch = ext.match(
  /function readRepositoryManifestInfo\(root, label\) \{[\s\S]*?\r?\n\}\r?\n/,
);
assert.ok(readRepositoryManifestInfoMatch, "readRepositoryManifestInfo function body not found");
assert.ok(
  readRepositoryManifestInfoMatch[0].includes(".isDirectory()"),
  "readRepositoryManifestInfo must validate the repository manifest path as a directory",
);
const extWithoutReadRepositoryManifestInfo = ext.replace(readRepositoryManifestInfoMatch[0], "");
assertAbsent(
  extWithoutReadRepositoryManifestInfo,
  [".isDirectory()"],
  "directory_only_storage_ready forbidden pattern present outside readRepositoryManifestInfo",
);

// getActiveRepositoryRoot must remain the single resolution path used by
// activation, selectRepositoryCommand, and getMcpClient -- and it must never
// call a bootstrap/initialize routine itself.
const activeRepoFnMatch = ext.match(/function getActiveRepositoryRoot\(context\) \{[\s\S]*?\r?\n}\r?\n/);
assert.ok(activeRepoFnMatch, "getActiveRepositoryRoot function body not found");
assertAbsent(
  activeRepoFnMatch[0],
  ["bootstrapRepository(", "fs.mkdirSync", "fs.writeFileSync", "atomicWriteJson("],
  "getActiveRepositoryRoot must perform no filesystem write",
);

// ── 2. Exactly one bounded initialize MCP tool, wired to the fixed message
//      enum and never auto-invoked. ─────────────────────────────────────────
assertPresent(
  ext,
  [
    'const INITIALIZE_TOOL = "aiworkhub_dashboard_initialize"',
    '"initializeStorage"',
    "async function pushInitializeStorage(view)",
    "REAL_REPO_ID_RE",
    'case "initializeStorage":',
    "pushInitializeStorage(view)",
  ],
  "initialize MCP tool wiring",
);
const pushInitializeMatch = ext.match(/async function pushInitializeStorage\(view\) \{[\s\S]*?\r?\n}\r?\n/);
assert.ok(pushInitializeMatch, "pushInitializeStorage function body not found");
assertPresent(
  pushInitializeMatch[0],
  [
    "const initializedClient = getMcpClient()",
    "view.bindClient(initializedClient)",
    "client: initializationClient",
    "authoritative: true",
    "convergeBackgroundServices: false",
    "SOURCE_GRAPH_DAEMON_TOOLS.ensureStarted",
    "sourceGraphStart.daemon_started !== true",
    "flushSystemLogs()",
  ],
  "post-init Source Graph convergence",
);
assert.ok(
  pushInitializeMatch[0].indexOf("client: initializationClient")
    < pushInitializeMatch[0].indexOf("const initializedClient = getMcpClient()"),
  "post-init storage snapshot must render on the initializing child before Windows MCP process rebind",
);
// The initialize tool must be excluded from the read-only contract list
// checked by pushRuntimeInfo (EXPECTED_DASHBOARD_TOOL_NAMES), i.e. it must
// not be one of the DASHBOARD_TOOLS values.
const dashboardToolsMatch = ext.match(/const DASHBOARD_TOOLS = Object\.freeze\(\{[\s\S]*?\}\);/);
assert.ok(dashboardToolsMatch, "DASHBOARD_TOOLS block not found");
assert.ok(
  !dashboardToolsMatch[0].includes("aiworkhub_dashboard_initialize"),
  "aiworkhub_dashboard_initialize must not be part of the read-only DASHBOARD_TOOLS contract",
);

// ── 3. The Webview HTML exposes exactly one Initialize AIWorkHub button. ───
const initButtonMatches = ext.match(/id="initialize-button"/g) || [];
assert.strictEqual(initButtonMatches.length, 1, "exactly one Initialize AIWorkHub button must be present");
assert.ok(ext.includes(">Initialize AIWorkHub<"), "Initialize AIWorkHub button label missing");

// ── 4. app.js: the storage gate renders from snapshot.storage (server-
//      verified authority), and the button posts the fixed message only. ──
assertPresent(
  app,
  [
    "function renderStorageState(snapshot)",
    "snapshot.storage",
    'vscode.postMessage({ type: "initializeStorage" });',
    "elements.initializeButton",
  ],
  "app.js storage-gate wiring",
);
const initializeMessageMatches = app.match(/vscode\.postMessage\(\{ type: "initializeStorage" \}\);/g) || [];
assert.strictEqual(initializeMessageMatches.length, 1, "the UI must contain exactly one initializeStorage post");

const storageStateMatch = app.match(
  /function renderStorageState\(snapshot\) \{[\s\S]*?\r?\n\}\r?\n\s*function renderSnapshot/,
);
assert.ok(storageStateMatch, "renderStorageState function body not found");
const storageStateSource = storageStateMatch[0].replace(/\r?\n\s*function renderSnapshot$/, "");

const initializeListenerMatch = app.match(
  /elements\.initializeButton\.addEventListener\("click", \(\) => \{[\s\S]*?\r?\n\}\);/,
);
assert.ok(initializeListenerMatch, "initializeStorage click handler not found");
const initializeListenerSource = initializeListenerMatch[0];

function createStorageHarness() {
  const elements = {
    uninitializedAlert: { hidden: null },
    uninitializedAlertHeading: { textContent: "" },
    uninitializedAlertMessage: { textContent: "" },
    initializeButton: { hidden: null, disabled: null, textContent: "" },
  };
  const connections = [];
  const render = new Function(
    "elements",
    "setConnection",
    `${storageStateSource}; return renderStorageState;`,
  )(elements, (...args) => connections.push(args));
  return { elements, connections, render: (storage) => render({ storage }) };
}

function createActionHarness(label) {
  let click = null;
  let scheduled = null;
  const messages = [];
  const button = {
    disabled: false,
    textContent: label,
    addEventListener(type, handler) {
      assert.strictEqual(type, "click");
      click = handler;
    },
  };
  new Function("elements", "vscode", "window", initializeListenerSource)(
    { initializeButton: button },
    { postMessage: (message) => messages.push(message) },
    { setTimeout: (handler, ms) => { scheduled = { handler, ms }; } },
  );
  return { button, click: () => click(), messages, scheduled: () => scheduled };
}

function exerciseStorageState(storage) {
  const harness = createStorageHarness();
  const ready = harness.render(storage);
  return { ready, elements: harness.elements, connections: harness.connections };
}

const readyStorage = exerciseStorageState({ ready: true, reason: "ready", not_initialized: false });
assert.strictEqual(readyStorage.ready, true);
assert.strictEqual(readyStorage.elements.uninitializedAlert.hidden, true);
assert.strictEqual(readyStorage.elements.initializeButton.hidden, true);
assert.strictEqual(readyStorage.elements.initializeButton.disabled, true);
assert.strictEqual(readyStorage.elements.uninitializedAlertMessage.textContent, "");

const manifestMissing = exerciseStorageState({
  ready: false,
  reason: "repository_manifest_missing",
  not_initialized: true,
});
assert.strictEqual(manifestMissing.ready, false);
assert.strictEqual(manifestMissing.elements.uninitializedAlertHeading.textContent, "AIWorkHub is not initialized for this repository");
assert.strictEqual(manifestMissing.elements.initializeButton.hidden, false);
assert.strictEqual(manifestMissing.elements.initializeButton.disabled, false);
assert.strictEqual(manifestMissing.elements.initializeButton.textContent, "Initialize AIWorkHub");
assert.match(manifestMissing.elements.uninitializedAlertMessage.textContent, /Initialize/);
assert.deepStrictEqual(manifestMissing.connections.at(-1), ["degraded", "Uninitialized"]);

const initializeAction = createActionHarness(manifestMissing.elements.initializeButton.textContent);
initializeAction.click();
assert.deepStrictEqual(initializeAction.messages, [{ type: "initializeStorage" }]);
assert.strictEqual(initializeAction.scheduled().ms, 4000);
initializeAction.scheduled().handler();
assert.strictEqual(initializeAction.button.textContent, "Initialize AIWorkHub");

const schemaIncomplete = exerciseStorageState({
  ready: false,
  reason: "canonical_schema_incomplete",
  not_initialized: false,
});
assert.strictEqual(schemaIncomplete.ready, false);
assert.strictEqual(schemaIncomplete.elements.uninitializedAlert.hidden, false);
assert.strictEqual(schemaIncomplete.elements.uninitializedAlertHeading.textContent, "AIWorkHub storage upgrade required");
assert.strictEqual(schemaIncomplete.elements.initializeButton.hidden, false);
assert.strictEqual(schemaIncomplete.elements.initializeButton.disabled, false);
assert.strictEqual(schemaIncomplete.elements.initializeButton.textContent, "Upgrade AIWorkHub storage");
assert.deepStrictEqual(schemaIncomplete.connections.at(-1), ["degraded", "Storage upgrade required"]);

const upgradeAction = createActionHarness(schemaIncomplete.elements.initializeButton.textContent);
upgradeAction.click();
assert.deepStrictEqual(upgradeAction.messages, [{ type: "initializeStorage" }]);
assert.strictEqual(upgradeAction.button.textContent, "Upgrading AIWorkHub storage");
upgradeAction.scheduled().handler();
assert.strictEqual(upgradeAction.button.textContent, "Upgrade AIWorkHub storage");
assert.strictEqual(upgradeAction.messages.length, 1);

for (const storage of [
  { ready: false, reason: "storage_permission_denied", not_initialized: false },
  { ready: false, reason: "malformed_flag", not_initialized: "true" },
  { ready: false, reason: "missing_flag" },
]) {
  const degraded = exerciseStorageState(storage);
  assert.strictEqual(degraded.ready, false);
  assert.strictEqual(degraded.elements.uninitializedAlert.hidden, false);
  assert.strictEqual(degraded.elements.uninitializedAlertHeading.textContent, "AIWorkHub storage is degraded");
  assert.strictEqual(degraded.elements.initializeButton.hidden, true);
  assert.strictEqual(degraded.elements.initializeButton.disabled, true);
  assert.ok(degraded.elements.uninitializedAlertMessage.textContent.includes(storage.reason));
  assert.ok(!degraded.elements.uninitializedAlertMessage.textContent.includes("Initialize"));
  assert.deepStrictEqual(degraded.connections.at(-1), ["degraded", `Storage degraded: ${storage.reason}`]);
}

const transition = createStorageHarness();
assert.strictEqual(transition.render({ ready: true, reason: "ready", not_initialized: false }), true);
assert.strictEqual(transition.render({ ready: false, reason: "canonical_schema_incomplete", not_initialized: false }), false);
assert.strictEqual(transition.elements.uninitializedAlertHeading.textContent, "AIWorkHub storage upgrade required");
assert.strictEqual(transition.elements.initializeButton.hidden, false);
assert.strictEqual(transition.elements.initializeButton.disabled, false);
assert.strictEqual(transition.elements.initializeButton.textContent, "Upgrade AIWorkHub storage");
assert.strictEqual(transition.render({ ready: false, reason: "repository_manifest_missing", not_initialized: true }), false);
assert.strictEqual(transition.elements.uninitializedAlertHeading.textContent, "AIWorkHub is not initialized for this repository");
assert.strictEqual(transition.elements.initializeButton.hidden, false);
assert.strictEqual(transition.elements.initializeButton.disabled, false);
assert.strictEqual(transition.elements.initializeButton.textContent, "Initialize AIWorkHub");
assert.strictEqual(transition.render({ ready: true, reason: "ready", not_initialized: false }), true);
assert.strictEqual(transition.elements.uninitializedAlert.hidden, true);
assert.strictEqual(transition.elements.uninitializedAlertHeading.textContent, "AIWorkHub storage is degraded");
assert.strictEqual(transition.elements.uninitializedAlertMessage.textContent, "");
assert.strictEqual(transition.elements.initializeButton.hidden, true);
assert.strictEqual(transition.elements.initializeButton.disabled, true);

// ── 5. Python: dashboard.py's task list/detail/exact-counts/callback-health
//      surface (DashboardProvider + exact_status_counts + build_snapshot/
//      build_task_detail) must never import/execute AITools/taskdb.py or
//      AITools/taskctl.py, and must gate on verified storage authority.
//      (The pre-existing, separately scoped HTTP /api/archive and
//      /api/restore mutation routes are untouched by this task and are
//      intentionally excluded from this check.) ───────────────────────────
const dashboardPy = readPy("dashboard.py");
const providerClassMatch = dashboardPy.match(/class DashboardProvider:[\s\S]*?\r?\n\r?\ndef /);
assert.ok(providerClassMatch, "DashboardProvider class body not found");
const exactCountsFnMatch = dashboardPy.match(/def exact_status_counts\([\s\S]*?\r?\n\r?\n\r?\n/);
assert.ok(exactCountsFnMatch, "exact_status_counts function body not found");
assertAbsent(
  providerClassMatch[0] + exactCountsFnMatch[0],
  ["from AITools.taskdb import", "from AITools import taskdb", "import AITools.taskdb", "core.list_tasks(", "core.show_task(", "core.callback_outbox_status("],
  "dashboard.py's task list/detail/exact-counts/callback-health surface must never use AITools/taskdb.py or AITools/taskctl.py",
);
assertPresent(
  dashboardPy,
  [
    "task_store.list_tasks(",
    "task_store.get_task(",
    "task_store.exact_status_counts(",
    "task_store.callback_bridge_health(",
    "get_storage_readiness",
    '"storage": storage_state',
  ],
  "dashboard.py canonical task_store wiring",
);

// ── 6. Python: dashboard_mcp_app.py exposes exactly the bounded initialize
//      tool alongside the three read-only tools. ───────────────────────────
const dashboardMcpAppPy = readPy("dashboard_mcp_app.py");
assertPresent(
  dashboardMcpAppPy,
  [
    'INITIALIZE_TOOL_NAME = "aiworkhub_dashboard_initialize"',
    "def initialize_view(",
    "repository_bootstrap.initialize_repository_full(",
    "storage_observability.invalidate(root)",
  ],
  "dashboard_mcp_app.py initialize tool wiring",
);

// ── 7. Python: task_store.py owns its own schema and never imports the
//      repository's own AITools tooling -- it is portable to any repo. ────
const taskStorePy = readPy("task_store.py");
assertAbsent(
  taskStorePy,
  ["import AITools", "from AITools"],
  "task_store.py must be self-contained (no AITools dependency)",
);
assertPresent(
  taskStorePy,
  [
    "def storage_readiness(",
    "def initialize_repository(",
    "Directory existence alone is never sufficient",
    "legacy files are never deleted",
  ],
  "task_store.py canonical authority + fail-closed init",
);

console.log("AIWorkHub explicit-init / no-legacy-fallback contract checks passed");
