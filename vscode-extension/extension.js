const vscode = require("vscode");
const childProcess = require("child_process");
const crypto = require("crypto");
const fs = require("fs");
const os = require("os");
const path = require("path");
const runtimeRetention = require("./runtime-retention");

const EXT_ID = "aiworkhub";
const DISPLAY_NAME = "AIWorkHub";
const WSP_STATE_KEY_REPO_URI = "aiworkhub.repositoryUri";
const PANEL_VIEW_TYPE = "aiworkhub.dashboard";
const EXPECTED_MCP_PACKAGE_VERSION = "0.8.85";
const WINDOW_SCOPE_ID = `window_${crypto.randomBytes(12).toString("hex")}`;
let extensionDebugTraceFile = "";
let mcpDebugTraceFile = "";
let extensionDebugTraceSequence = 0;

function initializeDebugTracing(context) {
  try {
    const enabled = vscode.workspace.getConfiguration(EXT_ID).get("debugTracing", true) !== false;
    const storageRoot = context && context.globalStorageUri && context.globalStorageUri.fsPath;
    if (!enabled || !storageRoot) return;
    const traceDir = path.join(storageRoot, "debug");
    fs.mkdirSync(traceDir, { recursive: true });
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
    const suffix = `${stamp}-${process.pid}-${WINDOW_SCOPE_ID}`;
    extensionDebugTraceFile = path.join(traceDir, `extension-${suffix}.jsonl`);
    mcpDebugTraceFile = path.join(traceDir, `mcp-${suffix}.jsonl`);
    debugTrace("trace.initialized", { platform: process.platform, arch: process.arch });
    installHostCrashDiagnostics();
  } catch (_err) {
    extensionDebugTraceFile = "";
    mcpDebugTraceFile = "";
  }
}

// Record HOW this extension host dies. Every extension in a window shares one
// extension-host process, so a single fatal error here takes Codex, Copilot
// and Claude down with the dashboard -- and the post-mortem is otherwise
// empty, because the host is gone before anything can be written. These
// listeners are strictly passive observers: VS Code installs its own
// uncaughtException/unhandledRejection handlers during bootstrap, so Node's
// default terminate-on-uncaught behaviour is already suppressed and adding
// another listener changes no control flow. There is no steady-state cost --
// nothing is written until one of these events actually fires.
let hostCrashDiagnosticsInstalled = false;
function installHostCrashDiagnostics() {
  if (hostCrashDiagnosticsInstalled || !extensionDebugTraceFile) return;
  hostCrashDiagnosticsInstalled = true;
  const stackOf = (value) => String((value && value.stack) || "").slice(0, 2000);
  process.on("uncaughtException", (err, origin) => {
    debugTrace("host.uncaught_exception", {
      origin: String(origin || "").slice(0, 40),
      ...debugErrorFields(err),
      stack: stackOf(err),
    });
  });
  process.on("unhandledRejection", (reason) => {
    debugTrace("host.unhandled_rejection", {
      ...debugErrorFields(reason),
      stack: stackOf(reason),
    });
  });
  // A clean exit still tells us the code; an abrupt external TerminateProcess
  // records nothing at all, and that absence is itself the diagnosis.
  process.on("exit", (code) => debugTrace("host.exit", { code }));
  for (const signal of ["SIGTERM", "SIGINT", "SIGHUP", "SIGBREAK"]) {
    try {
      process.on(signal, () => debugTrace("host.signal", { signal }));
    } catch (_err) {
      // Not every signal name is valid on every platform.
    }
  }
}

function debugTrace(event, fields = {}) {
  if (!extensionDebugTraceFile) return;
  try {
    const memory = process.memoryUsage();
    const payload = {
      schema_id: "aiworkhub.extension_debug_trace.v1",
      timestamp: new Date().toISOString(),
      sequence: ++extensionDebugTraceSequence,
      process: "extension-host",
      pid: process.pid,
      window_id: WINDOW_SCOPE_ID,
      event: String(event || "unknown").slice(0, 120),
      memory: {
        rss: memory.rss,
        heap_used: memory.heapUsed,
        heap_total: memory.heapTotal,
        external: memory.external,
      },
      ...fields,
    };
    const fd = fs.openSync(extensionDebugTraceFile, "a", 0o600);
    try {
      fs.writeSync(fd, `${JSON.stringify(payload)}\n`, null, "utf8");
      fs.fsyncSync(fd);
    } finally {
      fs.closeSync(fd);
    }
  } catch (_err) {
    // Diagnostics must never alter extension-host behavior.
  }
}

function debugErrorFields(err) {
  return {
    error_name: String((err && err.name) || "Error").slice(0, 80),
    error_code: String((err && err.code) || "").slice(0, 80),
    error_message: String((err && err.message) || "unknown").slice(0, 500),
  };
}

// ── Webview <-> extension host message contract ────────────────────────────
// The Webview NEVER receives a coordinator token, an environment value, a
// filesystem path, a raw MCP capability, a child-process handle, or an
// arbitrary tool-call primitive. It sends only this fixed, validated message
// enum; the extension host is the only thing that ever calls an MCP tool.
const ALLOWED_INBOUND_MESSAGE_TYPES = new Set([
  "ready",
  "refresh",
  "retry",
  "selectTask",
  "setAutoRefresh",
  "setRefreshInterval",
  "selectCoordinatorTarget",
  "initializeStorage",
  "requestLiveOutput",
  "clearSystemLogs",
  "copySystemLogs",
  "requestMemory",
  "requestSessions",
  "requestKb",
  "requestSettings",
  "updateFeatureSetting",
  "updateSourceGraphLanguage",
  "requestStorageCleanup",
  "requestStorageRegistrationPrune",
  "requestStorageRestore",
  "requestStoragePurge",
  "requestTerminalLogCleanup",
  "requestTerminalLogRestore",
  "requestTerminalLogPurge",
  "requestTaskArchive",
  "requestTaskRestore",
  "requestTaskCleanup",
  "requestTaskQuarantineRestore",
  "requestTaskQuarantinePurge",
  "requestRuntimeCleanup",
  "requestRuntimeRestore",
  "requestRuntimePurge",
]);

// Outbound message types the extension host posts into the Webview.
const OUTBOUND_TYPES = Object.freeze({
  snapshot: "snapshot",
  taskDetail: "taskDetail",
  offline: "offline",
  error: "error",
  repositoryInfo: "repositoryInfo",
  runtimeInfo: "runtimeInfo",
  coordinatorTargets: "coordinatorTargets",
  liveOutput: "liveOutput",
  systemLogs: "systemLogs",
  notification: "notification",
  memory: "memory",
  sessions: "sessions",
  kb: "kb",
  settings: "settings",
});

const TASK_ID_RE = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$/;
const STORAGE_BATCH_ID_RE = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/;
const ALLOWED_REFRESH_INTERVALS_MS = new Set([10000, 30000, 60000]);
const DEFAULT_REFRESH_INTERVAL_MS = 30000;
const REPO_ID_RE = /^repo_[a-f0-9]{32}$|^[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}$/;
const REAL_THREAD_ID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const TARGET_PROVIDERS = Object.freeze(["codex", "claude"]);
const TARGET_ROUTE_KEY = "routing/coordinator-targets.json";
const WINDOW_ROUTE_DIR_KEY = "routing/windows";
const WINDOW_ROUTE_LEASE_TTL_MS = 15 * 60 * 1000;
const SHARED_REPO_ROUTE_SCHEMA = "aiworkhub.shared_repo_route.v1";
const SHARED_REPO_ROUTE_TTL_MS = 15 * 60 * 1000;
// B905 lease renewal: substantially shorter than the 15-minute TTL so a
// window's route record never expires while the extension host is alive --
// a slow tick or a single missed renewal still leaves multiple retries
// before the lease would lapse.
const WINDOW_ROUTE_RENEWAL_INTERVAL_MS = 4 * 60 * 1000;
const DISPATCHER_WATCHDOG_INTERVAL_MS = 15 * 1000;

// VS Code Language Model bridge.  Custom models contributed to Copilot Chat
// (for example customendpoint/glm-5.2) are available only inside the
// extension host -- the standalone `copilot` CLI does not inherit that model
// registration or its editor authorization.  Requests are exchanged through
// an owner-only runtime spool; task state and outputs stay repository-bound.
const VSCODE_LM_REQUEST_SCHEMA = "aiworkhub.vscode_lm.request.v1";
const VSCODE_LM_HOST_SCHEMA = "aiworkhub.vscode_lm.host.v1";
const VSCODE_LM_RESPONSE_SCHEMA = "aiworkhub.vscode_lm.response.v1";
const VSCODE_LM_EDIT_RESPONSE_SCHEMA_V1 = "aiworkhub.vscode_lm.edit_response.v1";
const VSCODE_LM_EDIT_RESPONSE_SCHEMA = "aiworkhub.vscode_lm.semantic_edit_response.v3";
const VSCODE_LM_EDIT_RESPONSE_SCHEMA_V2 = "aiworkhub.vscode_lm.edit_response.v2";
const VSCODE_LM_TOOL_REQUEST_SCHEMA = "aiworkhub.vscode_lm.tool_request.v1";
const VSCODE_LM_TOOL_RESULT_SCHEMA = "aiworkhub.vscode_lm.tool_result.v1";
const VSCODE_LM_MODEL = "glm-5.2";
const VSCODE_LM_SUPPORTED_MODELS = Object.freeze(["glm-5.2", "deepseek-v4-pro", "deepseek-v4-flash", "claude-sonnet-4.6"]);
const VSCODE_LM_MODEL_ALIASES = Object.freeze({
  "glm-5.2": Object.freeze(["glm-5-2", "glm52", "glm-52"]),
  "deepseek-v4-pro": Object.freeze(["deepseek-v4pro", "deepseek-4-pro", "deepseek-v4-pro"]),
  "deepseek-v4-flash": Object.freeze(["deepseek-v4flash", "deepseek-4-flash", "deepseek-v4-flash"]),
  "claude-sonnet-4.6": Object.freeze(["claude-sonnet-46", "sonnet-4-6", "claude-sonnet-4-6"]),
});
const VSCODE_LM_REQUESTED_MODEL_RE = /^[A-Za-z0-9][A-Za-z0-9._:+\/-]{0,127}$/;
const VSCODE_LM_POLL_MS = 500;
const VSCODE_LM_HEARTBEAT_MS = 10000;
const VSCODE_LM_MAX_PARALLEL_REQUESTS = 3;
const VSCODE_LM_MAX_REQUEST_BYTES = 8 * 1024 * 1024;
const VSCODE_LM_MAX_AGENT_TURNS = 24;
const VSCODE_LM_MAX_TOOL_TURNS = 16;
const VSCODE_LM_MAX_POST_SOURCE_TURNS = 12;
const VSCODE_LM_MAX_FINALIZATION_TURNS = 4;
const VSCODE_LM_MAX_EMULATED_RESPONSE_BYTES = 256 * 1024;
const VSCODE_LM_MAX_EMULATED_TOOL_INPUT_BYTES = 16 * 1024;
const VSCODE_LM_REQUEST_ID_RE = /^[a-f0-9]{32}$/;

// ── The exact, narrow, read-only MCP tool allowlist this extension may call.
// Nothing else is ever sent as a tools/call `name`. See
// src/aiworkhub/dashboard_mcp_app.py.
const DASHBOARD_TOOLS = Object.freeze({
  snapshot: "aiworkhub_dashboard_snapshot",
  taskDetail: "aiworkhub_dashboard_task_detail",
  health: "aiworkhub_dashboard_health",
  liveOutput: "aiworkhub_dashboard_task_live_output",
  memory: "aiworkhub_dashboard_memory",
  sessions: "aiworkhub_dashboard_sessions",
  kb: "aiworkhub_dashboard_kb",
  settings: "aiworkhub_dashboard_settings",
});
const SETTINGS_UPDATE_TOOL = "aiworkhub_dashboard_settings_update";
const SOURCE_GRAPH_SETTINGS_UPDATE_TOOL = "aiworkhub_dashboard_source_graph_settings_update";
const FEATURE_SETTING_KEYS = new Set([
  "source_graph",
  "session_manager",
  "ai_memory",
  "knowledge_base",
  "context_graph",
]);
const STORAGE_RETENTION_TOOLS = Object.freeze({
  preview: "aiworkhub_dashboard_storage_retention_preview",
  quarantine: "aiworkhub_dashboard_storage_quarantine",
  pruneRegistrations: "aiworkhub_dashboard_storage_registration_prune",
  restore: "aiworkhub_dashboard_storage_restore",
  purge: "aiworkhub_dashboard_storage_purge",
});
const TERMINAL_LOG_RETENTION_TOOLS = Object.freeze({
  preview: "aiworkhub_dashboard_terminal_log_retention_preview",
  quarantine: "aiworkhub_dashboard_terminal_log_quarantine",
  restore: "aiworkhub_dashboard_terminal_log_restore",
  purge: "aiworkhub_dashboard_terminal_log_purge",
});
const TASK_RETENTION_TOOLS = Object.freeze({
  preview: "aiworkhub_dashboard_task_retention_preview",
  archive: "aiworkhub_dashboard_task_archive",
  restoreTask: "aiworkhub_dashboard_task_restore",
  quarantine: "aiworkhub_dashboard_task_quarantine",
  restoreBatch: "aiworkhub_dashboard_task_quarantine_restore",
  purgeBatch: "aiworkhub_dashboard_task_quarantine_purge",
});
// Callback delivery is a separate background service from Source Graph.
// Both are repo-bound and both must converge after the MCP handshake; neither
// may replace the other in the extension lifecycle.
const DISPATCHER_TOOLS = Object.freeze({
  ensureStarted: "aiworkhub_dispatcher_ensure_started",
  health: "aiworkhub_dispatcher_health",
  stop: "aiworkhub_dispatcher_stop",
});
// 0.6.30: Source Graph automatic indexing lifecycle -- same start/health/stop
// shape as DISPATCHER_TOOLS above, but for the repo-bound background index
// daemon (source_graph_daemon.py) instead of the callback dispatcher. Never
// spawns a shell process or installs anything; only starts/stops/inspects
// the in-process background indexing thread.
const SOURCE_GRAPH_DAEMON_TOOLS = Object.freeze({
  ensureStarted: "aiworkhub_source_graph_ensure_started",
  health: "aiworkhub_source_graph_health",
  stop: "aiworkhub_source_graph_stop",
});
// The one bounded write-capable tool -- kept out of DASHBOARD_TOOLS /
// EXPECTED_DASHBOARD_TOOL_NAMES (the read-only contract check in
// pushRuntimeInfo) since it is intentionally not read-only.
const INITIALIZE_TOOL = "aiworkhub_dashboard_initialize";
const EXPECTED_DASHBOARD_TOOL_NAMES = Object.freeze(Object.values(DASHBOARD_TOOLS));
// Only a repo_id shaped like newRepoId()'s own output is ever sent as an
// identity expectation to the initialize tool -- never one of the
// manifest-missing/manifest-invalid placeholder labels a never-initialized
// repository reports, so a first-time init is never refused by adopting a
// placeholder string as the permanent repo_id.
const REAL_REPO_ID_RE = /^repo_[a-f0-9]{32}$/;

// ── One bounded repo-local Task MCP stdio child + one JSON-RPC session ─────
const MCP_PROTOCOL_VERSION = "2024-11-05";
const MCP_REQUEST_TIMEOUT_MS = 20000;
// Worker-side Source Graph/context calls can legitimately take longer than a
// lightweight dashboard read.  Keep the budget bounded, but do not force the
// editor model through the dashboard's 20 second latency envelope.
const VSCODE_LM_WORKER_TOOL_TIMEOUT_MS = 90000;
const MCP_MAX_PENDING_REQUESTS = 16;
const MCP_MAX_LINE_BYTES = 8 * 1024 * 1024;
const MCP_MAX_STDERR_LOG_BYTES = 4096;
const MCP_MAX_RESTART_ATTEMPTS = 3;
const MCP_SNAPSHOT_RECOVERY_ATTEMPTS = 3;
const MCP_RECOVERY_BACKOFF_MS = Object.freeze([100, 200, 400]);
// B893: bounded self-repair budget for a DETECTED runtime version/capability
// mismatch (a stale bundled runtime after a VSIX update or an in-place
// runtime repair). Separate from MCP_MAX_RESTART_ATTEMPTS, which bounds
// restarts after an unexpected child *exit* -- this bounds restarts of a
// still-running, still-healthy child that simply reports the wrong version,
// so a persistently broken runtime degrades visibly instead of respawning
// forever. Repairing means restarting only THIS client's own child; it never
// rebinds to a different repository.
const MCP_MAX_RUNTIME_REPAIR_ATTEMPTS = 3;

// ── Active repository resolution ──────────────────────────────────────────
// Single-folder workspace: auto-bind to folders[0].
// Multi-root workspace: require an explicit bounded repository selection
// persisted in workspaceState. Never silently falls back to folders[0].
// The returned .fsPath is the host-absolute path used to spawn the MCP child;
// it is NEVER posted into the Webview -- only the folder name is shown.

// Reads ONLY the manifest identity (repo_id/repo_name) for labeling and MCP
// child keying. Never writes, never mkdir's, never calls bootstrapRepository,
// and never computes storageReady from directory existence -- that was the
// cross-repository dashboard authority bug (a fixture repo could show
// "storage ready" from empty durable-layout directories alone, while the
// dashboard actually read a legacy queue). storageReady/storageStatus below
// are always placeholders; the AUTHORITATIVE value is
// ``task_store.storage_readiness()`` on the Python side, delivered in every
// snapshot's ``storage`` field (see pushSnapshot / media/app.js) and updated
// live after a successful "Initialize AIWorkHub" action.
function readRepositoryManifestInfo(root, label) {
  const manifestPath = path.join(root, ".aiworkhub", "project.json");
  try {
    const payload = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
    const repoId = String(payload.repo_id || "");
    if (!REPO_ID_RE.test(repoId)) {
      return { repoId: "manifest-invalid", repoName: label, storageReady: false, storageStatus: "repo_id_invalid" };
    }
    return {
      repoId,
      repoName: String(payload.repo_name || label),
      storageReady: false,
      storageStatus: "pending_verification",
    };
  } catch (_err) {
    return { repoId: "manifest-missing", repoName: label, storageReady: false, storageStatus: "uninitialized" };
  }
}

function canonicalRepositoryRoot(root) {
  try {
    return fs.realpathSync.native(root);
  } catch (_err) {
    return root;
  }
}

function atomicWriteJson(file, payload) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  // Date.now() alone collides when the dashboard, lease renewal, and startup
  // convergence publish in the same Windows millisecond.
  const nonce = crypto.randomBytes(6).toString("hex");
  const tmp = path.join(path.dirname(file), `.${path.basename(file)}.${process.pid}.${Date.now()}.${nonce}.tmp`);
  try {
    fs.writeFileSync(tmp, `${JSON.stringify(payload, null, 2)}\n`, "utf8");
    fs.renameSync(tmp, file);
  } catch (err) {
    try {
      fs.unlinkSync(tmp);
    } catch (_cleanupErr) {
      // Best-effort cleanup; preserve the original filesystem failure.
    }
    throw err;
  }
}

function atomicWriteText(file, text) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const nonce = crypto.randomBytes(6).toString("hex");
  const tmp = path.join(path.dirname(file), `.${path.basename(file)}.${process.pid}.${Date.now()}.${nonce}.tmp`);
  try {
    fs.writeFileSync(tmp, String(text), "utf8");
    fs.renameSync(tmp, file);
  } catch (err) {
    try {
      fs.unlinkSync(tmp);
    } catch (_cleanupErr) {
      // Best-effort cleanup; preserve the original filesystem failure.
    }
    throw err;
  }
}

function sharedRepoRouteDir() {
  return path.join(os.homedir(), ".aiworkhub", "router", "repos");
}

function readSharedRepoRouteRecord(repoId) {
  if (!REAL_REPO_ID_RE.test(String(repoId || ""))) {
    return null;
  }
  try {
    const payload = JSON.parse(fs.readFileSync(path.join(sharedRepoRouteDir(), `${repoId}.json`), "utf8"));
    return payload && typeof payload === "object" ? payload : null;
  } catch (_err) {
    return null;
  }
}

function isSharedRouteRecordLeaseFresh(record) {
  const expiresAt = Date.parse((record && record.lease_expires_at) || "");
  return Number.isFinite(expiresAt) && expiresAt > Date.now();
}

function sharedRouteHasVerifiedCodexThread(record) {
  const route = record && record.targets && record.targets.codex && record.targets.codex.route;
  return Boolean(route) && REAL_THREAD_ID_RE.test(String(route.thread_id || ""));
}

// A lease timestamp alone is not liveness. Reloading or crashing a Remote
// extension host can leave a freshly renewed shared route on disk even though
// its owning PID is gone. Preserving that dead record prevents the replacement
// window from publishing its PID; the App Server mux then cannot bind the
// repository and falls through to unobserved Codex after its startup timeout.
// Signal 0 checks existence without terminating the process and is supported
// by Node on Linux, macOS, and Windows.
function isLocalProcessAlive(pid) {
  const numericPid = Number(pid);
  if (!Number.isInteger(numericPid) || numericPid <= 1) return false;
  try {
    process.kill(numericPid, 0);
    return true;
  } catch (_err) {
    return false;
  }
}

// B1008: the shared repo-wide manifest is a single last-writer-wins file --
// without this gate, a second window on the same repository (still
// route_pending, or observing an ambiguous/stale mux) could overwrite a
// different, still-live window's already-verified Codex thread on its own
// next lease-renewal tick, flipping a routable shared manifest back to
// unroutable out from under whichever window is actually connected to Codex.
// Only withhold the write when ALL of: the existing record belongs to a
// DIFFERENT window, that window's lease has not expired, it currently holds
// a verified thread, and THIS window's own targets are not themselves
// verified -- i.e. never downgrade a foreign live verified route with this
// window's pending/ambiguous observation. A window overwriting its own prior
// record, an expired lease, or this window's own fresh verification always
// proceeds.
function writeSharedRepoRouteRecord(repoInfo, targets) {
  if (!repoInfo || !REAL_REPO_ID_RE.test(String(repoInfo.repoId || ""))) {
    return null;
  }
  const thisWindowCodexRoute = targets && targets.targets && targets.targets.codex && targets.targets.codex.route;
  const thisWindowVerified = Boolean(thisWindowCodexRoute) && REAL_THREAD_ID_RE.test(String(thisWindowCodexRoute.thread_id || ""));
  const existing = readSharedRepoRouteRecord(repoInfo.repoId);
  if (
    existing
    && existing.window_id
    && existing.window_id !== WINDOW_SCOPE_ID
    && !thisWindowVerified
    && isSharedRouteRecordLeaseFresh(existing)
    && isLocalProcessAlive(existing.extension_host_pid)
    && sharedRouteHasVerifiedCodexThread(existing)
  ) {
    return existing;
  }
  const now = new Date();
  const record = {
    schema_id: SHARED_REPO_ROUTE_SCHEMA,
    repo_id: repoInfo.repoId,
    repo_name: repoInfo.repoName || repoInfo.label || path.basename(repoInfo.root || ""),
    repo_root: repoInfo.root,
    window_id: WINDOW_SCOPE_ID,
    extension_host_pid: process.pid,
    selected_provider: targets.selected_provider,
    targets: targets.targets,
    updated_at: now.toISOString(),
    lease_expires_at: new Date(now.getTime() + SHARED_REPO_ROUTE_TTL_MS).toISOString(),
  };
  const recordPath = path.join(sharedRepoRouteDir(), `${repoInfo.repoId}.json`);
  fs.mkdirSync(path.dirname(recordPath), { recursive: true, mode: 0o700 });
  atomicWriteJson(recordPath, record);
  try {
    fs.chmodSync(recordPath, 0o600);
  } catch (_err) {
    // Best-effort on platforms/filesystems that do not support POSIX chmod.
  }
  return record;
}

function removeSharedRepoRouteRecord(repoInfo) {
  if (!repoInfo || !REAL_REPO_ID_RE.test(String(repoInfo.repoId || ""))) {
    return;
  }
  const recordPath = path.join(sharedRepoRouteDir(), `${repoInfo.repoId}.json`);
  try {
    const payload = JSON.parse(fs.readFileSync(recordPath, "utf8"));
    if (payload && payload.window_id === WINDOW_SCOPE_ID) {
      fs.unlinkSync(recordPath);
    }
  } catch (_err) {
    // Missing/unreadable/stale shared discovery record -- nothing to clean.
  }
}

/** Return {root, label, uriStr, repoId, repoName, storageReady} for the active
 *  repository, or throw. Performs NO filesystem write and never calls a
 *  bootstrap/initialize routine -- opening or selecting a repository must
 *  never mutate it. An uninitialized repository is reported as such
 *  (repoId "manifest-missing", storageStatus "uninitialized"); the Webview's
 *  explicit "Initialize AIWorkHub" button is the only initialization
 *  trigger (see INITIALIZE_TOOL / handleInboundMessage).
 */
function getActiveRepositoryRoot(context) {
  const folders = vscode.workspace.workspaceFolders;
  if (!folders || folders.length === 0) {
    throw new Error("no_workspace_folder");
  }
  // Single-folder: auto-bind, persist for consistency.
  if (folders.length === 1) {
    const folder = folders[0];
    const root = canonicalRepositoryRoot(folder.uri.fsPath);
    context.workspaceState.update(WSP_STATE_KEY_REPO_URI, folder.uri.toString());
    return {
      root,
      label: folder.name,
      uriStr: folder.uri.toString(),
      ...readRepositoryManifestInfo(root, folder.name),
    };
  }
  // Multi-root: explicit selection required.
  const savedUri = context.workspaceState.get(WSP_STATE_KEY_REPO_URI);
  if (!savedUri) {
    throw new Error("no_repository_selected");
  }
  const match = folders.find((f) => f.uri.toString() === savedUri);
  if (!match) {
    // The saved URI is no longer a valid workspace folder.
    context.workspaceState.update(WSP_STATE_KEY_REPO_URI, undefined);
    throw new Error("invalid_repository_selection");
  }
  return {
    root: canonicalRepositoryRoot(match.uri.fsPath),
    label: match.name,
    uriStr: match.uri.toString(),
    ...readRepositoryManifestInfo(canonicalRepositoryRoot(match.uri.fsPath), match.name),
  };
}

/** Convert a workspace-folder uri.toString() to a short display label that
 *  never exposes the host-absolute path. Single-folder just uses folder name;
 *  multi-root uses folder name suffixed with a distinguishing parent segment
 *  for same-name disambiguation.
 */
function repositoryLabel(folders, uriStr) {
  const match = folders.find((f) => f.uri.toString() === uriStr);
  if (!match) {
    return "Unknown repository";
  }
  // For same-name disambiguation: if another folder shares the same name,
  // append the immediate parent directory segment.
  const sameName = folders.filter((f) => f.name === match.name);
  if (sameName.length > 1) {
    const parent = match.uri.fsPath.replace(/[\\/]+$/, "").split(/[\\/]/).filter(Boolean).slice(-2, -1)[0] || "";
    if (parent) {
      return `${match.name} (${parent})`;
    }
  }
  return match.name;
}

// B916: on Windows, each candidate Python is preflight-validated by actually
// importing and starting the bundled aiworkhub.server runtime before selection.
// A broken or incompatible repo-local .venv/venv Python is skipped and the
// validated fallback chain continues to py -3 then system python. On
// Linux/macOS the existing behaviour (existence check, no preflight) is
// unchanged.  shell=false everywhere; paths with spaces and Windows separators
// are handled without quoting bugs.  If no candidate works, diagnostics
// include attempted candidate names and a bounded stderr tail, never secrets.

// Bounded sanitised stderr for diagnostic inclusion: never secrets, never
// paths, limited to 500 chars.
function _sanitisePreflightStderr(raw) {
  const text = String(raw || "").slice(0, 500);
  return text.replace(/[A-Za-z0-9_-]{24,}/g, "[REDACTED]").trim() || "(no stderr output)";
}

// Run a single candidate Python through the preflight gate: "import
// aiworkhub.server". Returns {ok, candidate, ...diagnosticFields}. Never
// leaks secrets or host-absolute paths into diagnostic strings.
function _preflightPythonCandidate(command, extraArgs, runtimeDir = extensionRuntimeDir) {
  const args = [...(Array.isArray(extraArgs) ? extraArgs : []), "-c", "import aiworkhub.server"];
  const preflightEnv = { ...process.env };
  if (runtimeDir) {
    preflightEnv.PYTHONPATH = [runtimeDir, process.env.PYTHONPATH].filter(Boolean).join(path.delimiter);
  }
  try {
    const result = childProcess.spawnSync(command, args, {
      timeout: 15000,
      encoding: "utf8",
      stdio: ["pipe", "pipe", "pipe"],
      shell: false,
      cwd: runtimeDir || undefined,
      env: preflightEnv,
    });
    // spawnSync returns result.error for ENOENT / spawn failures without throwing.
    if (result.error) {
      return {
        ok: false,
        candidate: command,
        error: String((result.error && result.error.message) || "spawn_failed").slice(0, 200),
      };
    }
    if (result.status === 0) {
      return { ok: true, candidate: command };
    }
    return {
      ok: false,
      candidate: command,
      exitCode: result.status,
      stderrTail: _sanitisePreflightStderr(result.stderr),
      signal: result.signal || null,
    };
  } catch (err) {
    return {
      ok: false,
      candidate: command,
      error: String((err && err.message) || "spawn_failed").slice(0, 200),
    };
  }
}

function _preflightPythonCandidateAsync(command, extraArgs, runtimeDir = extensionRuntimeDir) {
  const args = [...(Array.isArray(extraArgs) ? extraArgs : []), "-c", "import aiworkhub.server"];
  const preflightEnv = { ...process.env };
  if (runtimeDir) {
    preflightEnv.PYTHONPATH = [runtimeDir, process.env.PYTHONPATH].filter(Boolean).join(path.delimiter);
  }
  return new Promise((resolve) => {
    childProcess.execFile(command, args, {
      timeout: 15000,
      encoding: "utf8",
      windowsHide: true,
      shell: false,
      cwd: runtimeDir || undefined,
      env: preflightEnv,
      maxBuffer: 64 * 1024,
    }, (err, _stdout, stderr) => {
      if (!err) {
        resolve({ ok: true, candidate: command });
        return;
      }
      const exitCode = Number.isInteger(err.code) ? err.code : null;
      const stderrTail = _sanitisePreflightStderr(stderr);
      if (exitCode !== null || (stderr && String(stderr).trim())) {
        resolve({
          ok: false,
          candidate: command,
          exitCode,
          stderrTail,
          signal: err.signal || null,
        });
        return;
      }
      resolve({
        ok: false,
        candidate: command,
        error: String(err.message || "spawn_failed").slice(0, 200),
      });
    });
  });
}

// Build a bounded human-readable diagnostic string from an ordered list of
// per-candidate preflight results. Only failed candidates appear; successful
// ones are omitted. Limited to 1200 total chars.
function _buildPreflightDiagnostic(diagnostics) {
  if (!diagnostics || diagnostics.length === 0) return "";
  const failed = diagnostics.filter((d) => !d.ok);
  if (failed.length === 0) return "";
  const parts = ["Python interpreter preflight diagnostics:"];
  let total = parts[0].length;
  for (const d of failed) {
    const tail = d.stderrTail || d.error || (d.exitCode != null ? `exit_code=${d.exitCode}` : "unknown");
    const line = `  ${d.candidate}: ${String(tail).slice(0, 200)}`;
    if (total + line.length > 1200) {
      parts.push("  ... (diagnostic truncated)");
      break;
    }
    total += line.length;
    parts.push(line);
  }
  return parts.join("\n");
}

function findPythonCommand(root, options = {}) {
  const configured = vscode.workspace.getConfiguration("aiworkhub").get("pythonPath");
  const isWindows = process.platform === "win32";
  const venvCandidates = isWindows
    ? [
        path.join(root, ".venv", "Scripts", "python.exe"),
        path.join(root, "venv", "Scripts", "python.exe"),
      ]
    : [
        path.join(root, ".venv", "bin", "python3"),
        path.join(root, ".venv", "bin", "python"),
        path.join(root, "venv", "bin", "python3"),
        path.join(root, "venv", "bin", "python"),
      ];

  const candidates = [
    typeof configured === "string" && configured.trim() ? configured.trim() : null,
    ...venvCandidates,
  ].filter(Boolean);

  // Configuration repair runs during activation and must not block the
  // extension host on synchronous child-process preflights. The real MCP
  // launch path retains validation before it starts a server child.
  if (isWindows && options.preflight === false) {
    for (const candidate of candidates) {
      const looksLikePath = path.isAbsolute(candidate) || candidate.includes("/") || candidate.includes("\\");
      if (!looksLikePath || fs.existsSync(candidate)) {
        return { command: candidate, argsPrefix: [] };
      }
    }
    return { command: "py", argsPrefix: ["-3"] };
  }

  // ── Windows: preflight-validate each candidate ─────────────────────────
  if (isWindows) {
    const diagnostics = [];
    for (const candidate of candidates) {
      const looksLikePath = path.isAbsolute(candidate) || candidate.includes("/") || candidate.includes("\\");
      if (!looksLikePath) {
        diagnostics.push({ ok: false, candidate, reason: "not_a_path" });
        continue;
      }
      if (!fs.existsSync(candidate)) {
        diagnostics.push({ ok: false, candidate, reason: "not_found" });
        continue;
      }
      const preflight = _preflightPythonCandidate(candidate);
      diagnostics.push(preflight);
      if (preflight.ok) {
        return {
          command: candidate,
          argsPrefix: [],
          preflightDiagnostic: null,
        };
      }
    }
    // All repo-local candidates failed. Try py -3 then system python.
    for (const fallback of [{ cmd: "py", args: ["-3"] }, { cmd: "python", args: [] }]) {
      const pf = _preflightPythonCandidate(fallback.cmd, fallback.args);
      diagnostics.push(pf);
      if (pf.ok) {
        return {
          command: fallback.cmd,
          argsPrefix: fallback.args,
          preflightDiagnostic: null,
        };
      }
    }
    // No candidate works: return the first that at least exists on disk
    // but attach a bounded diagnostic for the output channel.
    for (const candidate of candidates) {
      const looksLikePath = path.isAbsolute(candidate) || candidate.includes("/") || candidate.includes("\\");
      if (looksLikePath && fs.existsSync(candidate)) {
        return {
          command: candidate,
          argsPrefix: [],
          preflightDiagnostic: _buildPreflightDiagnostic(diagnostics),
        };
      }
    }
    return {
      command: "py",
      argsPrefix: ["-3"],
      preflightDiagnostic: _buildPreflightDiagnostic(diagnostics),
    };
  }

  // ── Linux/macOS: unchanged existence-based selection ───────────────────
  for (const candidate of candidates) {
    const looksLikePath = path.isAbsolute(candidate) || candidate.includes("/") || candidate.includes("\\");
    if (!looksLikePath || fs.existsSync(candidate)) {
      return { command: candidate, argsPrefix: [] };
    }
  }
  return { command: "python3", argsPrefix: [] };
}

async function findPythonCommandForLaunch(root) {
  if (process.platform !== "win32") {
    return findPythonCommand(root);
  }
  const configured = vscode.workspace.getConfiguration("aiworkhub").get("pythonPath");
  const candidates = [
    typeof configured === "string" && configured.trim() ? configured.trim() : null,
    path.join(root, ".venv", "Scripts", "python.exe"),
    path.join(root, "venv", "Scripts", "python.exe"),
  ].filter(Boolean);
  const diagnostics = [];
  for (const candidate of candidates) {
    const looksLikePath = path.isAbsolute(candidate) || candidate.includes("/") || candidate.includes("\\");
    if (!looksLikePath) {
      diagnostics.push({ ok: false, candidate, reason: "not_a_path" });
      continue;
    }
    if (!fs.existsSync(candidate)) {
      diagnostics.push({ ok: false, candidate, reason: "not_found" });
      continue;
    }
    const preflight = await _preflightPythonCandidateAsync(candidate, []);
    diagnostics.push(preflight);
    if (preflight.ok) {
      return { command: candidate, argsPrefix: [], preflightDiagnostic: null };
    }
  }
  for (const fallback of [{ cmd: "py", args: ["-3"] }, { cmd: "python", args: [] }]) {
    const preflight = await _preflightPythonCandidateAsync(fallback.cmd, fallback.args);
    diagnostics.push(preflight);
    if (preflight.ok) {
      return { command: fallback.cmd, argsPrefix: fallback.args, preflightDiagnostic: null };
    }
  }
  for (const candidate of candidates) {
    const looksLikePath = path.isAbsolute(candidate) || candidate.includes("/") || candidate.includes("\\");
    if (looksLikePath && fs.existsSync(candidate)) {
      return {
        command: candidate,
        argsPrefix: [],
        preflightDiagnostic: _buildPreflightDiagnostic(diagnostics),
      };
    }
  }
  return {
    command: "py",
    argsPrefix: ["-3"],
    preflightDiagnostic: _buildPreflightDiagnostic(diagnostics),
  };
}

function ensureRepositoryCoordinatorCapability(root) {
  // One capability per repository, never one mutable host-global token shared
  // by every VS Code window.  The latter let opening repo B rotate the token
  // underneath repo A's already-running MCP child, causing deterministic
  // coordinator_capability_denied:token_mismatch on review finalization.
  // Runtime is repo-local and gitignored by the AIWorkHub layout contract.
  const runtimeDir = path.join(root, ".aiworkhub", "runtime");
  fs.mkdirSync(runtimeDir, { recursive: true, mode: 0o700 });
  if (process.platform !== "win32") {
    try {
      fs.chmodSync(runtimeDir, 0o700);
    } catch (_err) {
      // Some Remote-SSH/sandboxed filesystems reject chmod even when mkdir
      // created the directory with the requested private mode. The token file
      // itself is still exclusive-created and content-validated below.
    }
  }
  const tokenFile = path.join(runtimeDir, "coordinator.token");
  try {
    const stat = fs.lstatSync(tokenFile);
    if (!stat.isFile() || stat.isSymbolicLink()) {
      throw new Error("repository coordinator capability is not a regular file");
    }
    if (process.platform !== "win32" && (stat.mode & 0o777) !== 0o600) {
      try {
        fs.chmodSync(tokenFile, 0o600);
      } catch (_err) {
        // Best-effort only; do not rotate or replace an existing repo-local
        // capability just because chmod is unavailable on this filesystem.
      }
    }
    const token = fs.readFileSync(tokenFile, "utf8").trim();
    if (!/^[a-f0-9]{64}$/.test(token)) {
      throw new Error("repository coordinator capability has invalid content");
    }
    return { tokenFile, token };
  } catch (err) {
    if (err && err.code !== "ENOENT") throw err;
  }

  const token = crypto.randomBytes(32).toString("hex");
  try {
    const fd = fs.openSync(tokenFile, "wx", 0o600);
    try {
      fs.writeFileSync(fd, token, { encoding: "utf8" });
      if (process.platform !== "win32") {
        try {
          fs.fchmodSync(fd, 0o600);
        } catch (_err) {
          // The fd was opened with 0600; fchmod is defense in depth.
        }
      }
    } finally {
      fs.closeSync(fd);
    }
    return { tokenFile, token };
  } catch (err) {
    // Another window for the SAME repo may win the exclusive create race.
    // Re-read its owner-controlled result; never rotate/overwrite it.
    if (!err || err.code !== "EEXIST") throw err;
    const existing = fs.readFileSync(tokenFile, "utf8").trim();
    if (!/^[a-f0-9]{64}$/.test(existing)) {
      throw new Error("repository coordinator capability race produced invalid content");
    }
    return { tokenFile, token: existing };
  }
}

function routeStatePath(root) {
  return path.join(root, ".aiworkhub", "config", TARGET_ROUTE_KEY);
}

function windowRouteStatePath(root, windowId) {
  return path.join(root, ".aiworkhub", "config", WINDOW_ROUTE_DIR_KEY, `${windowId}.json`);
}

// B905/isolation: each window owns exactly one routing record file, keyed by
// its own WINDOW_SCOPE_ID, instead of every window in the repo racing to
// overwrite the same coordinator-targets.json ("last writer wins"). Two
// windows on the same repo can never steal each other's active route this
// way -- each reads/refreshes only the file it created. The legacy shared
// file is still written for backward-compatible UI/migration reads, but it
// is never treated as routing authority by manager route discovery.
function writeWindowRouteRecord(repoInfo, targets) {
  const now = new Date();
  const record = {
    schema_id: "aiworkhub.window_route.v1",
    repo_id: repoInfo.repoId,
    window_id: WINDOW_SCOPE_ID,
    extension_host_pid: process.pid,
    selected_provider: targets.selected_provider,
    targets: targets.targets,
    updated_at: now.toISOString(),
    lease_expires_at: new Date(now.getTime() + WINDOW_ROUTE_LEASE_TTL_MS).toISOString(),
  };
  const recordPath = windowRouteStatePath(repoInfo.root, WINDOW_SCOPE_ID);
  fs.mkdirSync(path.dirname(recordPath), { recursive: true, mode: 0o700 });
  atomicWriteJson(recordPath, record);
  writeSharedRepoRouteRecord(repoInfo, targets);
  return record;
}

// B905 isolation cleanup: remove ONLY this window's own routing record file
// -- never another window's file, and never the legacy shared
// coordinator-targets.json, which other windows still read for migration.
// Best-effort: a missing file (already cleaned up, never written, or an
// unavailable filesystem) is silently ignored.
function removeWindowRouteRecord(repoInfo) {
  if (!repoInfo || !repoInfo.root) {
    return;
  }
  try {
    fs.unlinkSync(windowRouteStatePath(repoInfo.root, WINDOW_SCOPE_ID));
  } catch (_err) {
    // ENOENT or an unavailable filesystem -- nothing to clean up.
  }
  removeSharedRepoRouteRecord(repoInfo);
}

// One process-wide interval that keeps renewing THIS window's own route
// lease (never another window's) while the extension host is alive. Bounded
// to activeRepoIdentity, so a window with no bound repository yet simply
// skips a tick instead of writing a bogus record.
let windowRouteRenewalTimer = null;
let startupRouteConvergenceTimer = null;
let dispatcherWatchdogTimer = null;
let dispatcherWatchdogInFlight = false;
const STARTUP_ROUTE_CONVERGENCE_INTERVAL_MS = 250;
const STARTUP_ROUTE_CONVERGENCE_MAX_ATTEMPTS = 40;

function renewWindowRouteLease() {
  if (!activeRepoIdentity || !REPO_ID_RE.test(String(activeRepoIdentity.repoId || ""))) {
    return;
  }
  try {
    refreshCoordinatorRouteOwnership(activeRepoIdentity);
  } catch (_err) {
    // Best-effort -- a failed renewal just lets this tick lapse; the next
    // scheduled tick tries again well before the 15-minute TTL expires.
  }
}

function startWindowRouteRenewalTimer() {
  stopWindowRouteRenewalTimer();
  windowRouteRenewalTimer = setInterval(renewWindowRouteLease, WINDOW_ROUTE_RENEWAL_INTERVAL_MS);
  if (windowRouteRenewalTimer && typeof windowRouteRenewalTimer.unref === "function") {
    windowRouteRenewalTimer.unref();
  }
}

// Route discovery and dispatcher registration are separate asynchronous
// events. A live mux thread may appear after the bounded startup convergence
// window, or the repo-local dispatcher may stop while the verified route stays
// healthy. Reconcile those states independently and idempotently so callbacks
// recover without Reload Window. This watchdog only ever addresses this
// window's active repository and its already-owned MCP child.
async function runDispatcherWatchdogTick() {
  if (
    dispatcherWatchdogInFlight
    || !activeRepoIdentity
    || !REPO_ID_RE.test(String(activeRepoIdentity.repoId || ""))
  ) {
    return null;
  }
  const repoInfo = activeRepoIdentity;
  const verifiedThreadId = findVerifiedMuxThreadId(repoInfo);
  if (!REAL_THREAD_ID_RE.test(String(verifiedThreadId || ""))) {
    return null;
  }
  const persisted = readCoordinatorTargets(repoInfo);
  const persistedThreadId = String(
    persisted && persisted.targets && persisted.targets.codex
      && persisted.targets.codex.route && persisted.targets.codex.route.thread_id || "",
  );
  if (persistedThreadId !== verifiedThreadId) {
    refreshCoordinatorRouteOwnership(repoInfo);
  }
  dispatcherWatchdogInFlight = true;
  try {
    const client = getMcpClient();
    await client.ensureStarted();
    const health = await client.ensureDispatcherStarted(5000);
    if (!health || health.ok !== true) {
      outputChannel.appendLine(
        `[mcp] callback dispatcher watchdog did not converge: ${sanitizeErrorMessage(
          health && (health.reason || health.last_start_error || health.status) || "unknown",
        )}`,
      );
    }
    return health;
  } catch (err) {
    outputChannel.appendLine(`[mcp] callback dispatcher watchdog failed: ${sanitizeErrorMessage(err)}`);
    return null;
  } finally {
    dispatcherWatchdogInFlight = false;
  }
}

function startDispatcherWatchdog() {
  stopDispatcherWatchdog();
  dispatcherWatchdogTimer = setInterval(() => {
    runDispatcherWatchdogTick().catch((err) => {
      outputChannel.appendLine(`[mcp] callback dispatcher watchdog tick failed: ${sanitizeErrorMessage(err)}`);
    });
  }, DISPATCHER_WATCHDOG_INTERVAL_MS);
  if (dispatcherWatchdogTimer && typeof dispatcherWatchdogTimer.unref === "function") {
    dispatcherWatchdogTimer.unref();
  }
}

function stopDispatcherWatchdog() {
  if (dispatcherWatchdogTimer) {
    clearInterval(dispatcherWatchdogTimer);
    dispatcherWatchdogTimer = null;
  }
}

// The mux descriptor becomes ready before its first thread/start event is
// necessarily observed. A single refresh immediately after MCP startup can
// therefore publish route_pending and then leave the truthful UUID invisible
// until the four-minute lease renewal. Converge only during a bounded startup
// window; stop as soon as the exact mux-owned thread is available.
function startStartupRouteConvergence(repoInfo) {
  stopStartupRouteConvergence();
  let attempts = 0;
  const tick = () => {
    startupRouteConvergenceTimer = null;
    if (!activeRepoIdentity || activeRepoIdentity.root !== repoInfo.root) return;
    attempts += 1;
    let route = null;
    try {
      route = refreshCoordinatorRouteOwnership(repoInfo);
    } catch (_err) {
      // A later bounded tick can recover from a transient filesystem race.
    }
    const threadId = route && route.targets && route.targets.codex
      && route.targets.codex.route && route.targets.codex.route.thread_id;
    if (REAL_THREAD_ID_RE.test(String(threadId || ""))) {
      // The MCP child started conservatively in manager_inbox mode. Now that
      // this exact repo/window owns a live mux thread, re-run the idempotent
      // dispatcher ensure so Python promotes delivery to app_server_sideband
      // immediately instead of waiting for a later dashboard refresh/reload.
      try {
        const client = getMcpClient();
        client.ensureStarted().then(async () => {
          if (client.backgroundConvergencePromise) {
            await client.backgroundConvergencePromise;
          }
          return client.ensureDispatcherStarted(5000);
        }).catch((err) => {
          outputChannel.appendLine(`[mcp] sideband dispatcher promotion failed: ${sanitizeErrorMessage(err)}`);
        });
      } catch (err) {
        outputChannel.appendLine(`[mcp] sideband dispatcher promotion unavailable: ${sanitizeErrorMessage(err)}`);
      }
      return;
    }
    if (attempts >= STARTUP_ROUTE_CONVERGENCE_MAX_ATTEMPTS) {
      return;
    }
    startupRouteConvergenceTimer = setTimeout(tick, STARTUP_ROUTE_CONVERGENCE_INTERVAL_MS);
    if (startupRouteConvergenceTimer && typeof startupRouteConvergenceTimer.unref === "function") {
      startupRouteConvergenceTimer.unref();
    }
  };
  tick();
}

function stopStartupRouteConvergence() {
  if (startupRouteConvergenceTimer) {
    clearTimeout(startupRouteConvergenceTimer);
    startupRouteConvergenceTimer = null;
  }
}

// Safe to call from any state (including when no timer is running) --
// activate()/deactivate() and a reload cycle always route through this
// single stop point so a reload never leaves two live intervals ticking.
function stopWindowRouteRenewalTimer() {
  if (windowRouteRenewalTimer) {
    clearInterval(windowRouteRenewalTimer);
    windowRouteRenewalTimer = null;
  }
}

function defaultCoordinatorTargets(repoInfo) {
  return {
    schema_id: "aiworkhub.coordinator_targets.v1",
    repo_id: repoInfo.repoId,
    window_id: WINDOW_SCOPE_ID,
    claim_episode: activeClaimEpisode,
    extension_host_pid: process.pid,
    updated_at: new Date().toISOString(),
    selected_provider: "codex",
    targets: {
      codex: {
        provider: "codex",
        capability_state: "route_pending",
        route: { repo_id: repoInfo.repoId, window_id: WINDOW_SCOPE_ID, claim_episode: activeClaimEpisode, thread_id: "", session_id: activeClaimEpisode },
        wake: { mode: "direct_api_or_callback_inbox", supported: false, reason: "codex_thread_id_not_observed" },
      },
      claude: {
        provider: "claude",
        capability_state: "callback_required",
        route: { repo_id: repoInfo.repoId, window_id: WINDOW_SCOPE_ID, claim_episode: activeClaimEpisode, thread_id: `claude:${WINDOW_SCOPE_ID}`, session_id: activeClaimEpisode },
        wake: { mode: "durable_callback_inbox", supported: false, action: "notify_and_open_chat" },
      },
    },
  };
}

function readCoordinatorTargets(repoInfo) {
  const fallback = defaultCoordinatorTargets(repoInfo);
  try {
    const parsed = JSON.parse(fs.readFileSync(routeStatePath(repoInfo.root), "utf8"));
    if (!parsed || parsed.repo_id !== repoInfo.repoId || !TARGET_PROVIDERS.includes(parsed.selected_provider)) {
      return fallback;
    }
    return { ...fallback, ...parsed, targets: { ...fallback.targets, ...(parsed.targets || {}) } };
  } catch (_err) {
    atomicWriteJson(routeStatePath(repoInfo.root), fallback);
    return fallback;
  }
}

// ── Live Codex mux observation (B1008) ─────────────────────────────────────
// Mirrors src/aiworkhub/app_server_mux.py's per-instance registry contract
// and src/aiworkhub/core.py:_live_mux_active_thread exactly, so this
// extension host and the Python coordinator agree on the one live Codex
// thread (if any) that may ever be published as "verified" for a given
// repo/window -- never a different repository, never a different extension
// host, never fabricated from window/repo/claim-episode identifiers alone.
const APP_SERVER_MUX_SIDEBAND_DIR_ENV = "AIWORKHUB_APP_SERVER_MUX_SIDEBAND_DIR";
const APP_SERVER_MUX_INSTANCES_SUBDIR = "instances";
const APP_SERVER_MUX_REGISTRY_MAX_BYTES = 64 * 1024;
const APP_SERVER_MUX_DEFAULT_OWNER_LEASE_SECONDS = 90.0;

function appServerMuxSidebandDir() {
  const override = process.env[APP_SERVER_MUX_SIDEBAND_DIR_ENV];
  return override && override.trim() ? override.trim() : path.join(os.homedir(), ".aiworkhub", "app_server_mux");
}

function appServerMuxInstancesDir() {
  return path.join(appServerMuxSidebandDir(), APP_SERVER_MUX_INSTANCES_SUBDIR);
}

// Parses and ownership-checks one mux registry descriptor file. Returns
// null (never throws) for anything missing, oversized, foreign-owned, or
// corrupt -- callers must treat that identically to "no such instance".
function readMuxInstanceDescriptor(filePath) {
  let stat;
  try {
    stat = fs.lstatSync(filePath);
  } catch (_err) {
    return null;
  }
  if (!stat.isFile() || stat.size > APP_SERVER_MUX_REGISTRY_MAX_BYTES) {
    return null;
  }
  if (process.platform !== "win32") {
    const ownerOnly = (stat.mode & 0o777) === 0o600
      && (typeof process.getuid !== "function" || stat.uid === process.getuid());
    if (!ownerOnly) {
      return null;
    }
  }
  let payload;
  try {
    payload = JSON.parse(fs.readFileSync(filePath, "utf8"));
  } catch (_err) {
    return null;
  }
  if (!payload || typeof payload !== "object") {
    return null;
  }
  const repoId = String(payload.repo_id || "");
  if (!repoId) {
    return null;
  }
  const ownedThreadIds = Array.isArray(payload.owned_thread_ids)
    ? payload.owned_thread_ids.filter((id) => typeof id === "string" && id)
    : [];
  const activeThreadId = typeof payload.active_thread_id === "string" && ownedThreadIds.includes(payload.active_thread_id)
    ? payload.active_thread_id
    : (ownedThreadIds.length ? ownedThreadIds[ownedThreadIds.length - 1] : "");
  const parentPid = Number.isInteger(payload.parent_pid) && payload.parent_pid > 1 ? payload.parent_pid : 0;
  const heartbeatAt = typeof payload.heartbeat_at === "number" ? payload.heartbeat_at : stat.mtimeMs / 1000;
  const ownerLeaseSeconds = typeof payload.owner_lease_seconds === "number" && payload.owner_lease_seconds > 0
    ? payload.owner_lease_seconds
    : APP_SERVER_MUX_DEFAULT_OWNER_LEASE_SECONDS;
  return {
    repoId,
    parentPid,
    activeThreadId,
    heartbeatAt,
    ownerLeaseSeconds,
    ready: Boolean(payload.ready),
  };
}

// Mirrors SidebandInstance.is_owner_fresh: heartbeat age bounded by the
// instance's own declared lease, never a fixed extension-side timeout.
function isMuxInstanceFresh(instance) {
  if (!(instance.heartbeatAt > 0)) {
    return false;
  }
  const ageSeconds = Math.max(0, Date.now() / 1000 - instance.heartbeatAt);
  return ageSeconds <= instance.ownerLeaseSeconds;
}

// Mirrors src/aiworkhub/core.py:_live_mux_active_thread -- the ONE
// repo/window-scoped live Codex thread this extension host may ever publish
// as verified. Matching requires the immutable repo_id and this exact
// extension-host PID; zero or multiple candidates fail closed to "" so an
// empty, wrong-repo, wrong-host, not-ready, stale, or ambiguous observation
// never becomes a route.
function findVerifiedMuxThreadId(repoInfo) {
  let entries;
  try {
    entries = fs.readdirSync(appServerMuxInstancesDir(), { withFileTypes: true });
  } catch (_err) {
    return "";
  }
  const matches = [];
  for (const entry of entries) {
    if (!entry.isFile() || !entry.name.endsWith(".json")) {
      continue;
    }
    const instance = readMuxInstanceDescriptor(path.join(appServerMuxInstancesDir(), entry.name));
    if (
      instance
      && instance.repoId === repoInfo.repoId
      && instance.parentPid === process.pid
      && instance.ready
      && isMuxInstanceFresh(instance)
      && REAL_THREAD_ID_RE.test(instance.activeThreadId)
    ) {
      matches.push(instance);
    }
  }
  if (matches.length !== 1) {
    return "";
  }
  return matches[0].activeThreadId;
}

// Mirrors src/aiworkhub/core.py:read_selected_coordinator_target's codex
// branch exactly (capability_state "available" / wake "app_server_sideband"
// when verified; "route_pending" / "codex_thread_id_not_observed" otherwise)
// so the extension-published route and the Python-side read-time fallback
// never disagree about what "verified" means.
function sanitizeCoordinatorTargetRoute(provider, target, repoInfo) {
  const next = { ...(target || {}) };
  const route = { ...((next.route && typeof next.route === "object") ? next.route : {}) };
  route.repo_id = repoInfo.repoId;
  route.window_id = WINDOW_SCOPE_ID;
  route.claim_episode = activeClaimEpisode;
  if (provider === "codex") {
    const threadId = String(route.thread_id || "");
    if (REAL_THREAD_ID_RE.test(threadId)) {
      route.session_id = threadId;
      next.capability_state = "available";
      next.wake = { mode: "app_server_sideband", supported: true };
    } else {
      route.thread_id = "";
      route.session_id = activeClaimEpisode;
      next.capability_state = "route_pending";
      next.wake = {
        mode: "direct_api_or_callback_inbox",
        supported: false,
        reason: "codex_thread_id_not_observed",
      };
    }
  }
  next.route = route;
  return next;
}

// The single convergence point: activation, reload, restored-tab revival,
// runtime repair, and every lease-renewal tick all call this same function,
// so each one re-derives the codex route's thread_id fresh from the live mux
// registry rather than trusting whatever was last persisted. A thread that
// is no longer observed (mux instance gone, PID mismatch, lease expired)
// converges back to route_pending on the very next call -- no stale
// "verified" state ever lingers past its own live evidence.
function refreshCoordinatorRouteOwnership(repoInfo) {
  const next = readCoordinatorTargets(repoInfo);
  next.repo_id = repoInfo.repoId;
  next.window_id = WINDOW_SCOPE_ID;
  next.claim_episode = activeClaimEpisode;
  next.extension_host_pid = process.pid;
  next.updated_at = new Date().toISOString();
  const defaults = defaultCoordinatorTargets(repoInfo);
  const verifiedCodexThreadId = findVerifiedMuxThreadId(repoInfo);
  for (const provider of ["codex", "claude"]) {
    const target = { ...defaults.targets[provider], ...((next.targets || {})[provider] || {}) };
    const existingRoute = (((next.targets || {})[provider] || {}).route || {});
    target.route = { ...defaults.targets[provider].route, ...existingRoute };
    if (provider === "codex") {
      target.route.thread_id = verifiedCodexThreadId;
    }
    next.targets = { ...(next.targets || {}), [provider]: sanitizeCoordinatorTargetRoute(provider, target, repoInfo) };
  }
  atomicWriteJson(routeStatePath(repoInfo.root), next);
  writeWindowRouteRecord(repoInfo, next);
  return next;
}

function setCoordinatorTarget(provider) {
  if (!TARGET_PROVIDERS.includes(provider)) {
    return null;
  }
  const repoInfo = activeRepoIdentity || getActiveRepositoryRoot(extensionContext);
  const next = readCoordinatorTargets(repoInfo);
  next.selected_provider = provider;
  next.repo_id = repoInfo.repoId;
  next.window_id = WINDOW_SCOPE_ID;
  next.claim_episode = activeClaimEpisode;
  next.extension_host_pid = process.pid;
  next.updated_at = new Date().toISOString();
  atomicWriteJson(routeStatePath(repoInfo.root), next);
  writeWindowRouteRecord(repoInfo, next);
  return next;
}

// Bounded, redacted stderr line for the output channel: never surfaces a
// long opaque token/secret verbatim, and never grows past a fixed cap.
function sanitizeStderrChunk(buffer) {
  const text = buffer.toString("utf8").slice(0, MCP_MAX_STDERR_LOG_BYTES);
  return text.replace(/[A-Za-z0-9_-]{24,}/g, "[REDACTED]");
}

function extractToolResult(result) {
  if (!result || typeof result !== "object") {
    return null;
  }
  if (result.structuredContent && typeof result.structuredContent === "object") {
    return result.structuredContent;
  }
  const content = Array.isArray(result.content) ? result.content : [];
  for (const item of content) {
    if (item && item.type === "text" && typeof item.text === "string") {
      try {
        return JSON.parse(item.text);
      } catch (_err) {
        continue;
      }
    }
  }
  return null;
}

// Canonical dashboard rows may contain an absolute Remote-SSH host path in
// old process errors or provider diagnostics. Keep all operational fields,
// including repo-relative task metadata, but redact host-absolute paths at
// the final extension-host -> Webview boundary.
function sanitizeWebviewPayload(value) {
  if (typeof value === "string") {
    return value.replace(/(^|[\s("'=])\/[^\s"'<>|)]+/g, "$1<redacted-host-path>");
  }
  if (Array.isArray(value)) {
    return value.map((item) => sanitizeWebviewPayload(item));
  }
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value).map(([key, item]) => [key, sanitizeWebviewPayload(item)]),
    );
  }
  return value;
}

// Real out-of-process stdio JSON-RPC client for exactly one
// `python3 -m aiworkhub.server` child. Mirrors the initialize ->
// notifications/initialized -> tools/call handshake the repo's own
// mcp_stdio_client_smoke.py drives, hand-implemented here (newline-delimited
// JSON-RPC 2.0 over stdio) so this extension has no npm dependency to bundle
// or install offline.
class McpStdioClient {
  constructor(repositoryRoot, outputChannel, repositoryIdentity, claimEpisode) {
    this.repositoryRoot = repositoryRoot;
    this.outputChannel = outputChannel;
    this.repositoryIdentity = repositoryIdentity;
    this.claimEpisode = claimEpisode;
    this.child = null;
    this.lifecycleChild = null;
    this.lifecyclePid = null;
    this.buffer = "";
    this.nextId = 1;
    this.pending = new Map();
    this.pendingChildren = new Map();
    this.initialized = false;
    this.startingPromise = null;
    // A repository-identity replacement can hand this client the previous
    // client's shutdown promise. The next Windows launcher must not start
    // until the old py.exe -> python.exe process tree has released the repo.
    this.startupBarrier = null;
    // Exact version/capability evidence established during this child's
    // post-initialize preflight. Dashboard runtimeInfo reuses this immutable
    // lifecycle fact instead of issuing a redundant tools/list + health pair
    // while dispatcher/Source Graph startup calls are already in flight.
    this.runtimePreflight = null;
    this.dispatcherEnsurePromise = null;
    this.backgroundConvergencePromise = null;
    this.restartAttempts = 0;
    this.intentionalStop = false;
    // B893: how many bounded runtime-repair restarts this client has spent
    // on the CURRENT mismatch episode. Reset to 0 whenever a health check
    // finds the runtime healthy (see pushRuntimeInfo), so a later, distinct
    // mismatch (e.g. a subsequent runtime repair) gets its own fresh budget.
    this.runtimeRepairAttempts = 0;
    this.runtimeRepairBlockedReason = "";
    this.recovery = {
      category: "",
      reason: "",
      attempts: 0,
      maxAttempts: MCP_MAX_RESTART_ATTEMPTS,
      open: false,
      inProgress: false,
      episode: 0,
    };
    this.recoveryTimer = null;
  }

  recoveryStatus() {
    const state = this.recovery;
    return {
      category: String(state.category || "").slice(0, 80),
      reason: String(state.reason || "").slice(0, 240),
      attempts: state.attempts,
      maxAttempts: state.maxAttempts,
      open: state.open,
    };
  }

  beginExplicitRecovery() {
    this._clearRecoveryTimer();
    this.recovery = {
      category: "manual_retry",
      reason: "",
      attempts: 0,
      maxAttempts: MCP_MAX_RESTART_ATTEMPTS,
      open: false,
      inProgress: false,
      episode: this.recovery.episode + 1,
    };
    this.runtimeRepairAttempts = 0;
    this.runtimeRepairBlockedReason = "";
  }

  _clearRecoveryTimer() {
    if (this.recoveryTimer) {
      clearTimeout(this.recoveryTimer);
      this.recoveryTimer = null;
    }
    this.recovery.inProgress = false;
  }

  _ownsChild(candidate) {
    return Boolean(
      candidate
      && candidate === this.lifecycleChild
      && candidate.pid === this.lifecyclePid
    );
  }

  _clearLifecycleOwnership(candidate) {
    if (candidate === this.lifecycleChild) {
      this.lifecycleChild = null;
      this.lifecyclePid = null;
    }
  }

  _terminateOwnedChild(candidate) {
    if (!this._ownsChild(candidate)) return false;
    const ownedPid = candidate.pid;
    this._clearLifecycleOwnership(candidate);
    // A pid identifies our child ONLY while that child is still running.
    // Once it exits, Windows is free to hand the same pid to any new
    // process, and `taskkill /T` kills the target *plus every descendant* --
    // so a recycled pid would destroy an unrelated process tree. If that
    // stranger happens to sit above this extension host, the host dies with
    // it, taking every other extension in the window along. Node still owns
    // the process handle, so exitCode/signalCode are the authoritative
    // "is this pid still mine" answer; an already-exited child needs no
    // tree kill anyway.
    // Loose null check on purpose: a live ChildProcess reports null for both,
    // an exited one reports a number/signal name. Anything that exposes
    // neither field is treated as live, so this never silently skips a
    // termination it used to perform.
    const childStillRunning = candidate.exitCode == null && candidate.signalCode == null;
    if (
      process.platform === "win32"
      && childStillRunning
      && Number.isInteger(ownedPid)
      && ownedPid > 1
    ) {
      try {
        const result = childProcess.spawnSync(
          "taskkill",
          ["/PID", String(ownedPid), "/T", "/F"],
          { windowsHide: true, encoding: "utf8", timeout: 5000 },
        );
        if (!result.error && result.status === 0) return true;
      } catch (_err) {
        // Fall through to Node's exact-child termination below.
      }
    }
    if (!candidate.killed) {
      try {
        candidate.kill();
      } catch (_err) {
        return false;
      }
    }
    return true;
  }

  get running() {
    return Boolean(this.child && !this.child.killed);
  }

  // Never spawns a second child while one is starting/running -- callers
  // always go through this single bounded entry point.
  ensureStarted() {
    // Startup is not complete until the post-handshake runtime-version
    // preflight and any bounded repair restart have finished. Check the
    // shared starting promise BEFORE accepting a handshaken child as ready;
    // otherwise dashboard refresh can race a stale-child repair and observe
    // transient mcp_not_running / degraded state that incorrectly asks the
    // user to intervene manually.
    if (this.startingPromise) {
      return this.startingPromise;
    }
    if (this.recovery.open) {
      return Promise.reject(new Error("mcp_recovery_circuit_open"));
    }
    if (!this.running && this.runtimeRepairBlockedReason) {
      return Promise.reject(new Error(this.runtimeRepairBlockedReason));
    }
    if (this.running && this.initialized) {
      return Promise.resolve();
    }
    const barrier = this.startupBarrier;
    this.startupBarrier = null;
    const startOperation = barrier
      ? Promise.resolve(barrier).catch(() => {}).then(() => this._startWithVersionRepair())
      : this._startWithVersionRepair();
    this.startingPromise = startOperation.finally(() => {
      this.startingPromise = null;
    });
    return this.startingPromise;
  }

  async _startWithVersionRepair() {
    for (;;) {
      try {
        await this._start();
        return;
      } catch (err) {
        const message = sanitizeErrorMessage(err);
        if (
          !message.includes("mcp_version_mismatch_pre_service")
          && !message.includes("mcp_capability_mismatch_pre_service")
        ) throw err;
        if (this.runtimeRepairAttempts >= MCP_MAX_RUNTIME_REPAIR_ATTEMPTS) {
          this.runtimeRepairBlockedReason = `runtime_repair_budget_exhausted:${message}`;
          this.recovery.category = "runtime_mismatch";
          this.recovery.reason = this.runtimeRepairBlockedReason;
          this.recovery.attempts = MCP_MAX_RUNTIME_REPAIR_ATTEMPTS;
          this.recovery.open = true;
          throw err;
        }
        const delay = MCP_RECOVERY_BACKOFF_MS[this.runtimeRepairAttempts];
        this.runtimeRepairAttempts += 1;
        this.outputChannel.appendLine(`[mcp] runtime mismatch (${message}) -- bounded recovery ${this.runtimeRepairAttempts}/${MCP_MAX_RUNTIME_REPAIR_ATTEMPTS}`);
        this.stop({ restart: true });
        await new Promise((resolve) => setTimeout(resolve, delay));
      }
    }
  }

  async _start() {
    const previousChild = this.child;
    const previouslyOwnedChild = this.lifecycleChild;
    if (previousChild && !previousChild.killed) {
      this.outputChannel.appendLine("[mcp] replacing non-ready child before reconnect");
      this._failPendingForChild(previousChild, new Error("mcp_reconnect_replaced_non_ready_child"));
      this.child = null;
      this._terminateOwnedChild(previouslyOwnedChild);
    }
    const root = this.repositoryRoot;
    const python = await findPythonCommandForLaunch(root);
    this._lastPythonResult = python;
    const runtimeDir = extensionRuntimeDir;
    const env = {
      ...process.env,
      PYTHONIOENCODING: "utf-8",
      AIWORKHUB_REPO_ROOT: root,
      AIWORKHUB_REPO: root,
      AIWORKHUB_REPO_ID: this.repositoryIdentity.repoId,
      AIWORKHUB_WINDOW_ID: WINDOW_SCOPE_ID,
      AIWORKHUB_CLAIM_EPISODE: this.claimEpisode,
      // This workspace extension may run on a Remote-SSH host while Codex
      // runs in the local UI host.  Never claim sideband ownership unless a
      // verified UI-host companion exists; keep durable review delivery in
      // the cooperative manager inbox and report direct wake as unavailable.
      AIWORKHUB_CALLBACK_TRANSPORT: "manager_inbox",
    };
    if (mcpDebugTraceFile) {
      env.AIWORKHUB_DEBUG_TRACE_FILE = mcpDebugTraceFile;
    }
    // Extension-local runtime import path: `import aiworkhub` must always
    // resolve to the package this extension bundled under its own
    // `runtime/` directory (see test/package-vsix.js), never to the
    // selected repository, an editable install, or a fixed host path. Both
    // PYTHONPATH and the child's own cwd point at runtimeDir -- `python -m`
    // prepends cwd to sys.path[0] ahead of PYTHONPATH entries, so cwd is the
    // authoritative one; PYTHONPATH is defense in depth for interpreters
    // that alter that ordering. Data authority (which repository this
    // session is bound to) stays entirely in the AIWORKHUB_REPO* env vars
    // above -- never in cwd or an import path.
    if (runtimeDir) {
      env.PYTHONPATH = [runtimeDir, process.env.PYTHONPATH].filter(Boolean).join(path.delimiter);
    }
    // Defense in depth: this extension never enables the write gate or the
    // launch gate, regardless of the ambient extension-host environment.
    delete env.AIWORKHUB_ALLOW_WRITES;
    delete env.AIWORKHUB_ALLOW_LAUNCH;
    // Never inherit a stale coordinator secret from the extension host.
    // Load the current owner-only file into this private child environment;
    // package init scrubs it before any submodule can copy the environment.
    delete env.BITNN_TASKCTL_COORDINATOR_TOKEN;
    delete env.BITNN_TASKCTL_COORDINATOR_TOKEN_FILE;
    try {
      const capability = ensureRepositoryCoordinatorCapability(root);
      env.BITNN_TASKCTL_COORDINATOR_TOKEN = capability.token;
      env.BITNN_TASKCTL_COORDINATOR_TOKEN_FILE = capability.tokenFile;
    } catch (_err) {
      // Missing/unsafe token leaves coordinator mutations fail-closed.
    }

    this.intentionalStop = false;
    this.initialized = false;
    this.runtimePreflight = null;
    this.dispatcherEnsurePromise = null;
    this.backgroundConvergencePromise = null;
    this.buffer = "";
    this.nextId = 1;
    this.pending.clear();
    this.pendingChildren.clear();

    debugTrace("mcp.spawn.begin", {
      command: path.basename(python.command),
      args_prefix: python.argsPrefix,
    });
    const child = childProcess.spawn(python.command, [...python.argsPrefix, "-m", "aiworkhub.server"], {
      cwd: runtimeDir || root,
      env,
      stdio: ["pipe", "pipe", "pipe"],
      shell: false,
      windowsHide: true,
    });
    this.child = child;
    this.lifecycleChild = child;
    this.lifecyclePid = child.pid;
    debugTrace("mcp.spawn.end", { child_pid: child.pid || null });

    this._attachChildStreamErrorGuards(child);
    child.stdout.on("data", (chunk) => this._onStdout(child, chunk));
    child.stderr.on("data", (chunk) => {
      this.outputChannel.appendLine(`[mcp stderr] ${sanitizeStderrChunk(chunk)}`);
    });
    child.on("exit", (code, signal) => this._onExit(child, code, signal, null));
    child.on("error", (err) => this._onExit(child, null, null, err));

    try {
      await this._handshake();
    } catch (err) {
      if (this.child === child) {
        this.child = null;
        this.initialized = false;
      }
      this._failPendingForChild(child, err);
      this._terminateOwnedChild(child);
      throw err;
    }
  }

  _attachChildStreamErrorGuards(child) {
    for (const [name, stream] of [["stdin", child.stdin], ["stdout", child.stdout], ["stderr", child.stderr]]) {
      if (!stream || typeof stream.on !== "function") continue;
      stream.on("error", (err) => {
        if (!this._ownsChild(child) || this.intentionalStop) return;
        this.outputChannel.appendLine(`[mcp] child ${name} stream error: ${sanitizeErrorMessage(err)}`);
      });
    }
  }

  async _handshake() {
    await this.request("initialize", {
      protocolVersion: MCP_PROTOCOL_VERSION,
      capabilities: {},
      clientInfo: { name: "aiworkhub-vscode", version: installedExtensionVersion() },
    });
    this.notify("notifications/initialized", {});
    this.initialized = true;
    // Version/capability convergence is checked by _startWithVersionRepair()
    // immediately after this handshake returns and before callers treat the
    // child as ready. A mismatch triggers a bounded restart of only this
    // repo/window child, so users never need to manually kill stale runtimes.
    await this._assertRuntimeVersionBeforeServices();
    // Do not enqueue Dispatcher / Source Graph work here. On Windows the MCP
    // server can serialize those comparatively slow calls ahead of the first
    // dashboard snapshot, leaving the Webview stuck at "Connecting" even
    // though the handshake succeeded. The first successful snapshot starts
    // background convergence explicitly (see pushSnapshot* below).
  }


  async _assertRuntimeVersionBeforeServices() {
    const startedAt = Date.now();
    let tools;
    let health;
    try {
      const listed = await this.request("tools/list", {});
      tools = Array.isArray(listed && listed.tools) ? listed.tools : [];
      health = extractToolResult(await this.request("tools/call", { name: DASHBOARD_TOOLS.health, arguments: {} }));
    } catch (err) {
      this.outputChannel.appendLine(
        `[mcp] preflight failed error=${sanitizeErrorMessage(err)} duration_ms=${Date.now() - startedAt}`,
      );
      throw new Error(`mcp_health_preflight_failed:${sanitizeErrorMessage(err)}`);
    }
    const names = new Set(tools.map((tool) => String((tool && tool.name) || "")));
    const missing = EXPECTED_DASHBOARD_TOOL_NAMES.filter((name) => !names.has(name));
    const runtimeVersion = String((health && (health.server_version || health.version || health.package_version)) || "unavailable");
    this.runtimePreflight = {
      matches: missing.length === 0 && runtimeVersion === EXPECTED_MCP_PACKAGE_VERSION,
      missing: [...missing],
      runtimeVersion,
    };
    if (missing.length) {
      throw new Error(`mcp_capability_mismatch_pre_service:${missing.join(",")}`);
    }
    if (runtimeVersion !== EXPECTED_MCP_PACKAGE_VERSION) {
      throw new Error(`mcp_version_mismatch_pre_service:${runtimeVersion}`);
    }
    this.outputChannel.appendLine(
      `[mcp] preflight completed version=${runtimeVersion} tools=${tools.length} duration_ms=${Date.now() - startedAt}`,
    );
  }

  async _callToolRaw(name, args, timeoutMs = MCP_REQUEST_TIMEOUT_MS) {
    return extractToolResult(await this.request("tools/call", { name, arguments: args || {} }, timeoutMs));
  }

  ensureDispatcherStarted(timeoutMs = 5000) {
    if (this.dispatcherEnsurePromise) return this.dispatcherEnsurePromise;
    const operation = this._callToolRaw(DISPATCHER_TOOLS.ensureStarted, {}, timeoutMs);
    this.dispatcherEnsurePromise = operation;
    operation.finally(() => {
      if (this.dispatcherEnsurePromise === operation) {
        this.dispatcherEnsurePromise = null;
      }
    }).catch(() => {});
    return operation;
  }

  _convergeBackgroundServices() {
    if (this.backgroundConvergencePromise) return this.backgroundConvergencePromise;
    const run = async () => {
      try {
        await this.ensureDispatcherStarted(5000);
      } catch (err) {
        this.outputChannel.appendLine(`[mcp] callback dispatcher background convergence failed: ${sanitizeErrorMessage(err)}`);
      }
      try {
        await this._callToolRaw(SOURCE_GRAPH_DAEMON_TOOLS.ensureStarted, {}, 5000);
      } catch (err) {
        this.outputChannel.appendLine(`[mcp] source graph background convergence failed: ${sanitizeErrorMessage(err)}`);
      }
    };
    const operation = run();
    this.backgroundConvergencePromise = operation;
    operation.catch((err) => {
      this.outputChannel.appendLine(`[mcp] background service convergence failed: ${sanitizeErrorMessage(err)}`);
    }).finally(() => {
      if (this.backgroundConvergencePromise === operation) {
        this.backgroundConvergencePromise = null;
      }
    });
    return operation;
  }

  _onStdout(emittingChild, chunk) {
    if (emittingChild !== this.child || !this._ownsChild(emittingChild)) return;
    this.buffer += chunk.toString("utf8");
    if (this.buffer.length > MCP_MAX_LINE_BYTES) {
      this.outputChannel.appendLine("[mcp] unterminated stdout exceeded the response-size cap -- restarting");
      this._failPending(new Error("mcp_response_too_large"));
      this.stop({ restart: true });
      this.ensureStarted().catch((err) => {
        this.outputChannel.appendLine(`[mcp] restart after oversize failed: ${err.message}`);
      });
      return;
    }
    let newlineIndex;
    while ((newlineIndex = this.buffer.indexOf("\n")) !== -1) {
      const line = this.buffer.slice(0, newlineIndex);
      this.buffer = this.buffer.slice(newlineIndex + 1);
      if (!line.trim()) {
        continue;
      }
      if (Buffer.byteLength(line, "utf8") > MCP_MAX_LINE_BYTES) {
        this.outputChannel.appendLine("[mcp] dropped one oversized response line");
        continue;
      }
      this._onMessage(emittingChild, line);
    }
  }

  _onMessage(emittingChild, line) {
    let message;
    try {
      message = JSON.parse(line);
    } catch (_err) {
      return;
    }
    if (!message || typeof message !== "object" || message.id === undefined || message.id === null) {
      return;
    }
    const pending = this.pending.get(message.id);
    debugTrace("mcp.response.received", {
      child_pid: emittingChild && emittingChild.pid,
      id: message.id,
      line_bytes: Buffer.byteLength(line, "utf8"),
      has_error: Boolean(message.error),
      pending: Boolean(pending),
    });
    if (!pending || this.pendingChildren.get(message.id) !== emittingChild) {
      return;
    }
    this.pending.delete(message.id);
    this.pendingChildren.delete(message.id);
    clearTimeout(pending.timer);
    if (message.error) {
      pending.reject(new Error((message.error && message.error.message) || "mcp_error"));
    } else {
      pending.resolve(message.result);
    }
  }

  _onExit(exitedChild, code, signal, spawnError) {
    debugTrace("mcp.child.exit", {
      child_pid: exitedChild && exitedChild.pid,
      code,
      signal,
      intentional: this.intentionalStop,
      ...debugErrorFields(spawnError),
    });
    // An intentionally replaced child can emit its exit after the new MCP
    // singleton has already started. Ignore that stale event so it cannot
    // tear down the replacement or consume the bounded restart budget.
    if (this.child !== exitedChild) {
      return;
    }
    this.child = null;
    this._clearLifecycleOwnership(exitedChild);
    this.initialized = false;
    this.runtimePreflight = null;
    this.dispatcherEnsurePromise = null;
    this.backgroundConvergencePromise = null;
    const failure = spawnError || new Error(`mcp_child_exited code=${code} signal=${signal}`);
    this._failPendingForChild(exitedChild, failure);

    // B916: when a Windows child exits non-zero, include the interpreter
    // preflight diagnostic (if any) so the user can see WHY the selected
    // Python failed -- bounded, sanitised, never secrets or paths.
    if (code !== 0 && this._lastPythonResult && this._lastPythonResult.preflightDiagnostic) {
      this.outputChannel.appendLine(this._lastPythonResult.preflightDiagnostic);
    }

    if (this.intentionalStop) {
      return;
    }
    this._scheduleAutomaticRecovery("child_exit", failure);
  }

  _scheduleAutomaticRecovery(category, error) {
    const state = this.recovery;
    if (state.open || state.inProgress) return;
    state.category = category;
    state.reason = sanitizeErrorMessage(error);
    if (state.attempts >= state.maxAttempts) {
      state.open = true;
      return;
    }
    const delay = MCP_RECOVERY_BACKOFF_MS[state.attempts];
    state.attempts += 1;
    state.inProgress = true;
    this.recoveryTimer = setTimeout(() => {
      this.recoveryTimer = null;
      this.ensureStarted().then(() => {
        state.open = false;
        state.reason = "";
      }).catch((restartErr) => {
        state.reason = sanitizeErrorMessage(restartErr);
        if (state.attempts >= state.maxAttempts) state.open = true;
        else this._scheduleAutomaticRecovery(category, restartErr);
      }).finally(() => {
        state.inProgress = false;
        if (!state.open && !this.running) this._scheduleAutomaticRecovery(category, state.reason);
      });
    }, delay);
    if (this.recoveryTimer && typeof this.recoveryTimer.unref === "function") {
      this.recoveryTimer.unref();
    }
  }

  _failPending(err) {
    for (const pending of this.pending.values()) {
      clearTimeout(pending.timer);
      pending.reject(err);
    }
    this.pending.clear();
    this.pendingChildren.clear();
  }

  _failPendingForChild(child, err) {
    for (const [id, pendingChild] of this.pendingChildren.entries()) {
      if (pendingChild !== child) {
        continue;
      }
      const pending = this.pending.get(id);
      if (pending) {
        clearTimeout(pending.timer);
        pending.reject(err);
        this.pending.delete(id);
      }
      this.pendingChildren.delete(id);
    }
  }

  request(method, params, timeoutMs = MCP_REQUEST_TIMEOUT_MS) {
    if (!this.child) {
      return Promise.reject(new Error("mcp_not_running"));
    }
    if (this.pending.size >= MCP_MAX_PENDING_REQUESTS) {
      return Promise.reject(new Error("mcp_too_many_pending_requests"));
    }
    const id = this.nextId;
    this.nextId += 1;
    const requestChild = this.child;
    const payload = `${JSON.stringify({ jsonrpc: "2.0", id, method, params: params || {} })}\n`;
    const tool = method === "tools/call" ? String((params && params.name) || "") : "";
    debugTrace("mcp.request.begin", {
      child_pid: requestChild.pid,
      id,
      method,
      tool,
      timeout_ms: timeoutMs,
      payload_bytes: Buffer.byteLength(payload, "utf8"),
    });
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        this.pendingChildren.delete(id);
        if (this.child === requestChild && !this.initialized) {
          this.child = null;
          this._terminateOwnedChild(requestChild);
        }
        debugTrace("mcp.request.timeout", { child_pid: requestChild.pid, id, method, tool });
        reject(new Error("mcp_request_timeout"));
      }, timeoutMs);
      this.pending.set(id, { resolve, reject, timer });
      this.pendingChildren.set(id, requestChild);
      requestChild.stdin.write(payload, (err) => {
        if (err) {
          this.pending.delete(id);
          this.pendingChildren.delete(id);
          clearTimeout(timer);
          debugTrace("mcp.request.write_error", {
            child_pid: requestChild.pid,
            id,
            method,
            tool,
            ...debugErrorFields(err),
          });
          reject(err);
        } else {
          debugTrace("mcp.request.written", { child_pid: requestChild.pid, id, method, tool });
        }
      });
    });
  }

  notify(method, params) {
    if (!this.child) {
      return;
    }
    const payload = `${JSON.stringify({ jsonrpc: "2.0", method, params: params || {} })}\n`;
    this.child.stdin.write(payload, () => {});
  }

  async callTool(name, args, timeoutMs = MCP_REQUEST_TIMEOUT_MS) {
    const startedAt = Date.now();
    try {
      await this.ensureStarted();
      const result = extractToolResult(
        await this.request("tools/call", { name, arguments: args || {} }, timeoutMs),
      );
      const status = result && result.ok === false
        ? `failed error=${sanitizeErrorMessage(result.message || result.error || "tool_reported_failure")}`
        : `completed ok=${result && result.ok === true ? "true" : "unspecified"}`;
      this.outputChannel.appendLine(`[mcp] tool=${name} ${status} duration_ms=${Date.now() - startedAt}`);
      return result;
    } catch (err) {
      this.outputChannel.appendLine(
        `[mcp] tool=${name} failed error=${sanitizeErrorMessage(err)} duration_ms=${Date.now() - startedAt}`,
      );
      throw err;
    }
  }

  async listTools() {
    await this.ensureStarted();
    const result = await this.request("tools/list", {});
    return Array.isArray(result && result.tools) ? result.tools : [];
  }

  stop({ restart = false } = {}) {
    this._clearRecoveryTimer();
    this.intentionalStop = !restart;
    const child = this.lifecycleChild;
    this.child = null;
    this.initialized = false;
    this.runtimePreflight = null;
    this.dispatcherEnsurePromise = null;
    this.backgroundConvergencePromise = null;
    this._failPendingForChild(child, new Error(restart ? "mcp_restarting" : "mcp_stopped"));
    this._terminateOwnedChild(child);
  }

  // Best-effort service shutdown followed by exact-object child termination.
  async stopDispatcherThenTerminate({ restart = false, timeoutMs = MCP_REQUEST_TIMEOUT_MS } = {}) {
    this._clearRecoveryTimer();
    const ownedChild = this.lifecycleChild;
    this.intentionalStop = !restart;
    if (this.running && this.initialized && this.child === ownedChild) {
      try {
        await this.request(
          "tools/call",
          { name: DISPATCHER_TOOLS.stop, arguments: {} },
          timeoutMs,
        );
      } catch (_err) {
        // Best-effort -- proceed to terminate the child regardless.
      }
      try {
        await this.request(
          "tools/call",
          { name: SOURCE_GRAPH_DAEMON_TOOLS.stop, arguments: {} },
          timeoutMs,
        );
      } catch (_err) {
        // Best-effort -- proceed to terminate the child regardless.
      }
    }
    this.stop({ restart });
  }

  // B893: the ONE reloadless repair path. A detected runtime version or
  // capability mismatch is fixed in place -- one bounded restart of THIS
  // repository's own child, spawned with the exact same
  // repositoryRoot/repositoryIdentity/claimEpisode this client was
  // constructed with (see getMcpClient/_start) -- never a different
  // repository, never a host-global runtime, never a manual "Developer:
  // Reload Window" instruction. Bounded by MCP_MAX_RUNTIME_REPAIR_ATTEMPTS
  // so a persistently broken runtime degrades visibly instead of respawning
  // forever; the caller (pushRuntimeInfo) resets the budget once a
  // subsequent health check reports a genuine match.
  async attemptRuntimeRepair(reason) {
    if (this.runtimeRepairAttempts >= MCP_MAX_RUNTIME_REPAIR_ATTEMPTS) {
      return { attempted: false, repaired: false, reason: "runtime_repair_budget_exhausted" };
    }
    this.runtimeRepairAttempts += 1;
    this.outputChannel.appendLine(`[mcp] runtime mismatch (${reason}) -- attempting one bounded child restart`);
    this.stop({ restart: true });
    try {
      await this.ensureStarted();
    } catch (err) {
      return { attempted: true, repaired: false, reason: sanitizeErrorMessage(err) };
    }
    return { attempted: true, repaired: true, reason };
  }
}

let outputChannel = null;
let mcpClient = null;
let activeRepoIdentity = null;
let sidebarView = null;
let panel = null;
let extensionContext = null;
let activeRepoLabel = "No repository";
let activeClaimEpisode = `episode_${crypto.randomBytes(12).toString("hex")}`;
// The extension-local directory the bundled aiworkhub Python package lives
// under (see vscode-extension/test/package-vsix.js). Set once in activate()
// from context.extensionUri, which VS Code resolves to this extension's own
// install location -- on the remote/workspace host for Remote-SSH, since
// this extension's extensionKind is "workspace". Never derived from the
// selected repository, an editable install, or a fixed host path.
let extensionRuntimeDir = null;
let vscodeLmBridgeHost = null;
let stableRuntimeInfo = null;
let stableRuntimeLease = null;
let stableRuntimeLeaseTimer = null;
let runtimeRetentionCache = null;

function stopStableRuntimeLease() {
  if (stableRuntimeLeaseTimer) clearInterval(stableRuntimeLeaseTimer);
  stableRuntimeLeaseTimer = null;
  if (stableRuntimeLease) stableRuntimeLease.dispose();
  stableRuntimeLease = null;
}

function startStableRuntimeLease(runtimeInfo) {
  stopStableRuntimeLease();
  stableRuntimeInfo = runtimeInfo;
  runtimeRetentionCache = null;
  if (!runtimeInfo || !runtimeInfo.storageRoot || !runtimeInfo.generationRoot) return;
  stableRuntimeLease = runtimeRetention.acquireLease({
    generationRoot: runtimeInfo.generationRoot,
    windowId: WINDOW_SCOPE_ID,
    pid: process.pid,
  });
  stableRuntimeLeaseTimer = setInterval(() => {
    try {
      stableRuntimeLease.heartbeat();
    } catch (err) {
      if (outputChannel) outputChannel.appendLine(`[runtime] lease heartbeat failed: ${sanitizeErrorMessage(err)}`);
    }
  }, 2 * 60 * 1000);
  if (typeof stableRuntimeLeaseTimer.unref === "function") stableRuntimeLeaseTimer.unref();
}

function runtimeRetentionSnapshot({ force = false } = {}) {
  if (!stableRuntimeInfo || !stableRuntimeInfo.storageRoot) {
    return { ok: false, error: "runtime_global_storage_unavailable", candidates: [], quarantine_batches: [] };
  }
  const now = Date.now();
  if (!force && runtimeRetentionCache && now - runtimeRetentionCache.cached_at < 60000) {
    return runtimeRetentionCache.payload;
  }
  try {
    const payload = runtimeRetention.scan({ storageRoot: stableRuntimeInfo.storageRoot, nowMs: now });
    runtimeRetentionCache = { cached_at: now, payload };
    return payload;
  } catch (err) {
    return { ok: false, error: sanitizeErrorMessage(err), candidates: [], quarantine_batches: [] };
  }
}

function vscodeLmBridgeRoot() {
  const override = String(process.env.AIWORKHUB_VSCODE_LM_BRIDGE_ROOT || "").trim();
  return path.resolve(override || path.join(os.homedir(), ".aiworkhub", "vscode_lm_bridge"));
}

function atomicWriteOwnerJson(filePath, payload) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true, mode: 0o700 });
  try { fs.chmodSync(path.dirname(filePath), 0o700); } catch (_err) { /* Windows/filesystem */ }
  atomicWriteJson(filePath, payload);
  try { fs.chmodSync(filePath, 0o600); } catch (_err) { /* Windows/filesystem */ }
}

function vscodeLmModelFields(model) {
  return [model && model.id, model && model.family, model && model.name, model && model.vendor, model && model.version]
    .filter((value) => typeof value === "string" && value.trim())
    .map((value) => value.trim());
}

function isCallableVscodeLmProvider(model) {
  // `copilotcli` and the first-party `claude-code` extension contribute
  // internal agent/picker entries to model
  // discovery, but those entries return empty streams when invoked through
  // the public VS Code Language Model API. The latter was verified with a
  // stream-first live canary: both text and tool-part populations were zero.
  // Copilot also contributes
  // `copilot-utility*` picker entries whose display name mirrors the selected
  // model but whose request metadata has no tokenizer. Calling one through
  // sendRequest fails with `Unknown tokenizer: undefined`. The public
  // Copilot provider uses vendor=`copilot` with a concrete model id/family;
  // custom endpoints such as GLM use their own vendor.
  const id = normalizedVscodeLmModelName(model && model.id);
  const family = normalizedVscodeLmModelName(model && model.family);
  const vendor = normalizedVscodeLmModelName(model && model.vendor);
  return Boolean(
    model &&
    typeof model === "object" &&
    vendor !== "copilotcli" &&
    vendor !== "claude-code" &&
    !id.startsWith("copilot-utility") &&
    !family.startsWith("copilot-utility")
  );
}

function normalizedVscodeLmModelName(value) {
  return String(value || "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
}

function canonicalVscodeLmModelName(value) {
  const normalized = normalizedVscodeLmModelName(value);
  const known = VSCODE_LM_SUPPORTED_MODELS.find((name) =>
    normalizedVscodeLmModelName(name) === normalized ||
    (VSCODE_LM_MODEL_ALIASES[name] || []).some((alias) => normalizedVscodeLmModelName(alias) === normalized)
  );
  if (known) return known;
  const raw = String(value || "").trim();
  return VSCODE_LM_REQUESTED_MODEL_RE.test(raw) ? raw : "";
}

function isVscodeLanguageModel(model, requestedModel) {
  const requestedCanonical = canonicalVscodeLmModelName(requestedModel);
  if (!requestedCanonical) return false;
  return vscodeLmModelFields(model).some((value) =>
    canonicalVscodeLmModelName(value) === requestedCanonical
  );
}

function vscodeLmModelSelectionRank(model, requestedModel) {
  const requestedCanonical = canonicalVscodeLmModelName(requestedModel);
  if (!requestedCanonical) return Number.MAX_SAFE_INTEGER;
  const requested = normalizedVscodeLmModelName(requestedCanonical);
  // Stable provider identities outrank mutable display names. This prevents
  // an internal picker named after the requested model from beating the real
  // model entry merely because its id sorts first alphabetically.
  const fields = [
    [model && model.id, 0],
    [model && model.family, 10],
    [model && model.name, 20],
    [model && model.version, 30],
    [model && model.vendor, 40],
  ];
  let best = Number.MAX_SAFE_INTEGER;
  for (const [value, base] of fields) {
    if (typeof value !== "string" || !value.trim()) continue;
    if (normalizedVscodeLmModelName(value) === requested) best = Math.min(best, base);
    else if (canonicalVscodeLmModelName(value) === requestedCanonical) best = Math.min(best, base + 1);
  }
  return best;
}

function selectVscodeLanguageModel(models, requestedModel) {
  const requestedCanonical = canonicalVscodeLmModelName(requestedModel);
  if (!requestedCanonical) return null;
  const matches = (Array.isArray(models) ? models : []).filter(
    (model) => isCallableVscodeLmProvider(model) && isVscodeLanguageModel(model, requestedCanonical),
  );
  return matches.sort((left, right) => {
    const leftRank = vscodeLmModelSelectionRank(left, requestedCanonical);
    const rightRank = vscodeLmModelSelectionRank(right, requestedCanonical);
    return leftRank - rightRank || String(left.id || "").localeCompare(String(right.id || ""));
  })[0] || null;
}

function vscodeLmAccessState(model) {
  try {
    const accessInformation = vscode.lm && vscode.lm.accessInformation;
    if (!accessInformation || typeof accessInformation.canSendRequest !== "function") return "unknown";
    const allowed = accessInformation.canSendRequest(model);
    if (allowed === true) return "granted";
    if (allowed === false) return "not_granted";
  } catch (_err) {
    // A contributed provider may disappear between discovery and the access
    // check. Treat that as unknown and let the bounded user-gesture path
    // produce the authoritative sendRequest error.
  }
  return "unknown";
}

function vscodeLmPermissionStorageKey(model) {
  const identity = [model && model.vendor, model && model.id, model && model.family]
    .map((value) => String(value || "").trim().toLowerCase())
    .join("\u0000");
  return `aiworkhub.vscodeLmWorkerPermission.v2.${crypto.createHash("sha256").update(identity).digest("hex").slice(0, 24)}`;
}

function isGlm52LanguageModel(model) { return isVscodeLanguageModel(model, VSCODE_LM_MODEL); }
function selectGlm52LanguageModel(models) { return selectVscodeLanguageModel(models, VSCODE_LM_MODEL); }

function ownerOnlyRegularFile(filePath, maxBytes = VSCODE_LM_MAX_REQUEST_BYTES) {
  try {
    const stat = fs.lstatSync(filePath);
    if (!stat.isFile() || stat.isSymbolicLink() || stat.size > maxBytes) return false;
    if (process.platform !== "win32") {
      if ((stat.mode & 0o077) !== 0) return false;
      if (typeof process.getuid === "function" && stat.uid !== process.getuid()) return false;
    }
    return true;
  } catch (_err) {
    return false;
  }
}

function validateVscodeLmRequest(payload, repoInfo) {
  if (!payload || typeof payload !== "object" || payload.schema_id !== VSCODE_LM_REQUEST_SCHEMA) {
    throw new Error("vscode_lm_request_schema_mismatch");
  }
  const requestId = String(payload.request_id || "");
  if (!VSCODE_LM_REQUEST_ID_RE.test(requestId)) throw new Error("vscode_lm_request_id_invalid");
  if (!repoInfo || payload.repo_id !== repoInfo.repoId) throw new Error("vscode_lm_repo_id_mismatch");
  if (canonicalRepositoryRoot(String(payload.repo_root || "")) !== canonicalRepositoryRoot(repoInfo.root)) {
    throw new Error("vscode_lm_repo_root_mismatch");
  }
  const requestedModel = canonicalVscodeLmModelName(payload.model);
  if (!requestedModel) {
    throw new Error("vscode_lm_model_mismatch");
  }
  if (typeof payload.prompt !== "string" || !payload.prompt.trim()) throw new Error("vscode_lm_prompt_missing");
  const workspacePath = path.resolve(String(payload.workspace_path || ""));
  const workspaceHome = path.resolve(String(payload.workspace_home || ""));
  const responsePath = path.resolve(String(payload.response_path || ""));
  if (path.basename(workspacePath) !== "worktree" || path.basename(workspaceHome) !== "home") {
    throw new Error("vscode_lm_workspace_shape_invalid");
  }
  if (path.dirname(workspacePath) !== path.dirname(workspaceHome) || path.basename(path.dirname(workspacePath)) !== requestId) {
    throw new Error("vscode_lm_workspace_request_mismatch");
  }
  if (responsePath !== path.join(workspaceHome, ".aiworkhub_vscode_lm_response.json")) {
    throw new Error("vscode_lm_response_path_invalid");
  }
  const allowedWrites = payload.allowed_writes;
  if (!Array.isArray(allowedWrites) || allowedWrites.some((value) => typeof value !== "string" || value.length > 512)) {
    throw new Error("vscode_lm_allowed_writes_invalid");
  }
  const initialSourceGraphRequest = payload.initial_source_graph_request;
  if (initialSourceGraphRequest !== null && initialSourceGraphRequest !== undefined) {
    if (!initialSourceGraphRequest || typeof initialSourceGraphRequest !== "object" || Array.isArray(initialSourceGraphRequest)) {
      throw new Error("vscode_lm_initial_source_graph_request_invalid");
    }
    const permittedKeys = new Set(["mode", "query", "budget", "target", "bundle_type"]);
    if (Object.keys(initialSourceGraphRequest).some((key) => !permittedKeys.has(key)) ||
        !["focus", "slice", "context", "file", "function", "class", "body", "bodygrep", "impact", "trace", "deps", "bundle", "tags", "hotspots", "coverage", "churn", "reviewqueue", "ownership", "testmap", "calls", "symbols", "bottlenecks", "auditmap", "complexity", "stats", "summarize", "pipeline", "todo", "leaks", "nullrisks", "rawptrs", "casts", "crashes", "looprisks", "deadmethods", "duplicates", "gaps"].includes(initialSourceGraphRequest.mode) ||
        typeof initialSourceGraphRequest.query !== "string" || !initialSourceGraphRequest.query.trim() ||
        initialSourceGraphRequest.query.length > 512) {
      throw new Error("vscode_lm_initial_source_graph_request_invalid");
    }
  }
  const deadline = Date.parse(String(payload.deadline || ""));
  if (!Number.isFinite(deadline) || deadline <= Date.now()) throw new Error("vscode_lm_request_expired");
  return { ...payload, model: requestedModel, requestId, workspacePath, workspaceHome, responsePath, allowedWrites };
}

const VSCODE_LM_PRIVATE_TOOLS = Object.freeze([
  {
    name: "aiworkhub_manager_source_graph_query",
    description: "Mandatory repository-bound Source Graph query. Use instead of grep, rg, find, tree, or broad file reads.",
    inputSchema: {
      type: "object",
      additionalProperties: false,
      required: ["mode", "query"],
      properties: {
        mode: { type: "string", enum: ["focus", "slice", "context", "file", "function", "class", "body", "bodygrep", "impact", "trace", "deps", "bundle", "tags", "hotspots", "coverage", "churn", "reviewqueue", "ownership", "testmap", "calls", "symbols", "bottlenecks", "auditmap", "complexity", "stats", "summarize", "pipeline", "todo", "leaks", "nullrisks", "rawptrs", "casts", "crashes", "looprisks", "deadmethods", "duplicates", "gaps"] },
        query: { type: "string", minLength: 1, maxLength: 512 },
        budget: { type: "integer", minimum: 8, maximum: 160 },
        target: { type: ["string", "null"], maxLength: 256 },
        bundle_type: { type: "string", enum: ["bugfix", "feature", "refactor", "audit", "optimize", "explore"] },
      },
    },
  },
  {
    name: "aiworkhub_manager_semantic_edit_prepare",
    description: "Bind one Source Graph-selected line range in the isolated worktree. Returns only that old fragment plus full-file/fragment hashes for replacement-only output.",
    inputSchema: {
      type: "object",
      additionalProperties: false,
      required: ["file_path", "start_line", "end_line"],
      properties: {
        file_path: { type: "string", minLength: 1, maxLength: 512 },
        start_line: { type: "integer", minimum: 1 },
        end_line: { type: "integer", minimum: 1 },
      },
    },
  },
  {
    name: "aiworkhub_manager_session_current_state",
    description: "Recover bounded current project session state before non-trivial assumptions.",
    inputSchema: { type: "object", additionalProperties: false, properties: { topic: { type: "string", maxLength: 128 }, limit: { type: "integer", minimum: 1, maximum: 20 } } },
  },
  {
    name: "aiworkhub_manager_ai_memory_search",
    description: "Search durable repository AI Memory for task-specific decisions and lessons.",
    inputSchema: { type: "object", additionalProperties: false, required: ["query"], properties: { query: { type: "string", minLength: 1, maxLength: 512 }, limit: { type: "integer", minimum: 1, maximum: 20 } } },
  },
  {
    name: "aiworkhub_manager_kb_search",
    description: "Search authoritative repository knowledge-base contracts and documentation.",
    inputSchema: { type: "object", additionalProperties: false, required: ["query"], properties: { query: { type: "string", minLength: 1, maxLength: 512 }, limit: { type: "integer", minimum: 1, maximum: 20 } } },
  },
  {
    name: "aiworkhub_manager_kb_get",
    description: "Fetch one exact authoritative KB entry after search.",
    inputSchema: { type: "object", additionalProperties: false, required: ["key"], properties: { key: { type: "string", minLength: 1, maxLength: 256 } } },
  },
  {
    name: "aiworkhub_manager_kb_related",
    description: "Fetch bounded related authoritative KB entries.",
    inputSchema: { type: "object", additionalProperties: false, required: ["key"], properties: { key: { type: "string", minLength: 1, maxLength: 256 } } },
  },
  {
    name: "aiworkhub_manager_session_write_intent",
    description: "Propose a bounded Session event for explicit verified-manager disposition; never writes canonical context directly.",
    inputSchema: {
      type: "object", additionalProperties: false,
      required: ["action", "content", "idempotency_key", "provenance"],
      properties: {
        action: { type: "string", enum: ["start", "event", "checkpoint", "state", "handoff", "close"] },
        content: { type: "string", minLength: 1, maxLength: 32768 },
        idempotency_key: { type: "string", minLength: 8, maxLength: 192 },
        provenance: { type: "string", minLength: 1, maxLength: 2048 },
      },
    },
  },
  {
    name: "aiworkhub_manager_ai_memory_write_intent",
    description: "Propose a bounded AI Memory mutation for explicit verified-manager disposition.",
    inputSchema: {
      type: "object", additionalProperties: false,
      required: ["action", "key", "idempotency_key", "provenance"],
      properties: {
        action: { type: "string", enum: ["remember", "update", "supersede", "archive"] },
        key: { type: "string", minLength: 1, maxLength: 256 },
        value: { type: "string", maxLength: 32768 },
        tags: { type: "string", maxLength: 1024 },
        scope: { type: "string", maxLength: 64 },
        idempotency_key: { type: "string", minLength: 8, maxLength: 192 },
        provenance: { type: "string", minLength: 1, maxLength: 2048 },
      },
    },
  },
  {
    name: "aiworkhub_manager_kb_write_intent",
    description: "Propose a bounded KB mutation for explicit verified-manager disposition.",
    inputSchema: {
      type: "object", additionalProperties: false,
      required: ["action", "key", "idempotency_key", "provenance"],
      properties: {
        action: { type: "string", enum: ["upsert", "ingest", "supersede", "archive"] },
        key: { type: "string", minLength: 1, maxLength: 256 },
        title: { type: "string", maxLength: 1024 },
        body: { type: "string", maxLength: 32768 },
        category: { type: "string", maxLength: 128 },
        tags: { type: "string", maxLength: 1024 },
        source_refs: { type: "string", maxLength: 2048 },
        replacement_key: { type: "string", maxLength: 256 },
        idempotency_key: { type: "string", minLength: 8, maxLength: 192 },
        provenance: { type: "string", minLength: 1, maxLength: 2048 },
      },
    },
  },
  {
    name: "aiworkhub_manager_quality_review_submit",
    description: "Submit findings for the exact launcher-bound, read-only quality-review packet.",
    inputSchema: {
      type: "object", additionalProperties: false,
      required: ["packet_sha256", "lens", "findings"],
      properties: {
        packet_sha256: { type: "string", pattern: "^[0-9a-f]{64}$" },
        lens: { type: "string", enum: ["correctness", "security", "code_quality"] },
        findings: {
          type: "array", maxItems: 50,
          items: {
            type: "object", additionalProperties: false,
            required: ["id", "severity", "summary", "evidence"],
            properties: {
              id: { type: "string", minLength: 1, maxLength: 200 },
              severity: { type: "string", enum: ["critical", "high", "medium", "low"] },
              summary: { type: "string", minLength: 1, maxLength: 2000 },
              evidence: { type: "string", minLength: 1, maxLength: 2000 },
            },
          },
        },
      },
    },
  },
]);

function languageModelTextPart(value) {
  return typeof vscode.LanguageModelTextPart === "function" ? new vscode.LanguageModelTextPart(String(value)) : { value: String(value) };
}

function languageModelToolResultPart(callId, value) {
  const content = [languageModelTextPart(JSON.stringify(value))];
  return new vscode.LanguageModelToolResultPart(callId, content);
}

function isLanguageModelToolCallPart(part) {
  return Boolean(part && typeof part.callId === "string" && typeof part.name === "string" && part.input && typeof part.input === "object");
}

async function invokeVscodeLmPrivateTool(call, requestId = "") {
  const permitted = VSCODE_LM_PRIVATE_TOOLS.find((tool) => tool.name === call.name);
  if (!permitted) throw new Error(`vscode_lm_tool_not_allowed:${String(call.name || "")}`);
  if (!mcpClient || !activeRepoIdentity) throw new Error("vscode_lm_mcp_unavailable");
  if (mcpClient.repositoryRoot !== activeRepoIdentity.root) throw new Error("vscode_lm_mcp_repo_mismatch");
  if (!VSCODE_LM_REQUEST_ID_RE.test(String(requestId || ""))) {
    throw new Error("vscode_lm_worker_request_id_invalid");
  }
  return mcpClient.callTool("aiworkhub_vscode_lm_worker_tool", {
    request_id: requestId,
    tool_name: permitted.name,
    tool_input: call.input || {},
  }, VSCODE_LM_WORKER_TOOL_TIMEOUT_MS);
}

function glmAgentProtocolPrompt(prompt, allowedWrites, pathContracts = {}) {
  const examplePath = Array.isArray(allowedWrites) && allowedWrites.length
    ? String(allowedWrites[0])
    : "output.json";
  return `${prompt}\n\nAIWorkHub VS Code GLM worker contract:\n` +
    `- Source Graph is mandatory throughout code discovery; never request or simulate grep/rg/find/tree.\n` +
    `- Source Graph file lookup: mode=file with query and target both equal to one declared repo-relative path returns source_hash for that file.\n` +
    `- Source Graph body lookup: mode=body with query equal to the exact indexed symbol name and target equal to that file path returns bounded source.\n` +
    `- Use the supplied AIWorkHub Session Manager, AI Memory and KB tools when relevant.\n` +
    `- At completion output ONLY one JSON object with schema_id ${VSCODE_LM_EDIT_RESPONSE_SCHEMA}.\n` +
    `- Never read or emit a complete existing file. Use Source Graph body mode for the smallest exact symbol, then return only its replacement in a line range.\n` +
    `- After body discovery call aiworkhub_manager_semantic_edit_prepare with that file and exact line range. Copy current_sha256 from its receipt; the receipt returns only the selected old fragment, never the whole file.\n` +
    `- The prepare tool input is exactly {"file_path":"repo/relative/path","start_line":1,"end_line":1}; use file_path, not path, in that tool request.\n` +
    `- Semantic edits must name an allowed path, copy current_sha256 from path_contracts, and use non-overlapping 1-based inclusive start_line/end_line values from Source Graph evidence.\n` +
    `- new contains only replacement code; it may be empty for deletion. Do not echo old code. preserve_trailing_newline defaults true.\n` +
    `- creates must name an allowed path and complete UTF-8 content. A path_contract action=create means the runtime owns an empty placeholder and the final response MUST use creates for that path.\n` +
    `- For action=edit, copy current_sha256 exactly from semantic-edit prepare (or the identical exact path_contract); do not infer or recompute it.\n` +
    `- allowed_writes=${JSON.stringify(allowedWrites)}\n` +
    `- path_contracts=${JSON.stringify(pathContracts || {})}\n` +
    `Required shape using the real allowed path: {"schema_id":"${VSCODE_LM_EDIT_RESPONSE_SCHEMA}","summary":"...","edits":[{"path":${JSON.stringify(examplePath)},"current_sha256":"<copy from path_contracts>","ranges":[{"start_line":10,"end_line":18,"new":"replacement code only","preserve_trailing_newline":true}]}],"creates":[]}`;
}

function vscodeLmPathMatchesPattern(rawPath, rawPattern) {
  const value = String(rawPath || "").replace(/\\/g, "/");
  const pattern = String(rawPattern || "").replace(/\\/g, "/");
  if (!value || value.startsWith("/") || value.split("/").includes("..")) return false;
  let expression = "^";
  for (let index = 0; index < pattern.length; index += 1) {
    const char = pattern[index];
    if (char === "*" && pattern[index + 1] === "*") {
      expression += ".*";
      index += 1;
    } else if (char === "*") expression += "[^/]*";
    else if (char === "?") expression += "[^/]";
    else expression += char.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  }
  return new RegExp(`${expression}$`).test(value);
}

function validateVscodeLmFinalEnvelope(envelope, allowedWrites) {
  if (!envelope || typeof envelope.summary !== "string") {
    return "final_shape_invalid";
  }
  if (envelope.schema_id === VSCODE_LM_EDIT_RESPONSE_SCHEMA_V1) {
    if (!Array.isArray(envelope.files)) return "final_files_invalid";
    for (const file of envelope.files) {
      if (!file || typeof file.path !== "string" || typeof file.content !== "string") {
        return "final_file_invalid";
      }
      if (!allowedWrites.some((pattern) => vscodeLmPathMatchesPattern(file.path, pattern))) {
        return `final_path_not_allowed:${file.path}`;
      }
    }
    return "";
  }
  if (envelope.schema_id === VSCODE_LM_EDIT_RESPONSE_SCHEMA_V2) {
    if (!Array.isArray(envelope.edits) || !Array.isArray(envelope.creates)) return "final_v2_shape_invalid";
    for (const edit of envelope.edits) {
      if (!edit || typeof edit.path !== "string" || typeof edit.current_sha256 !== "string" || !Array.isArray(edit.replacements)) return "final_edit_invalid";
      if (!/^[0-9a-f]{64}$/.test(edit.current_sha256)) return `final_hash_invalid:${edit.path}`;
      if (!allowedWrites.some((pattern) => vscodeLmPathMatchesPattern(edit.path, pattern))) return `final_path_not_allowed:${edit.path}`;
      for (const replacement of edit.replacements) {
        if (!replacement || typeof replacement.old !== "string" || !replacement.old ||
            typeof replacement.new !== "string" || !replacement.new ||
            !Number.isSafeInteger(replacement.expected_count) || replacement.expected_count < 1) return `final_replacement_invalid:${edit.path}`;
      }
    }
    return "";
  }
  if (envelope.schema_id !== VSCODE_LM_EDIT_RESPONSE_SCHEMA) return "final_schema_invalid";
  if (!Array.isArray(envelope.edits) || !Array.isArray(envelope.creates)) {
    return "final_v2_shape_invalid";
  }
  for (const edit of envelope.edits) {
    if (!edit || typeof edit.path !== "string" || typeof edit.current_sha256 !== "string" || !Array.isArray(edit.ranges)) {
      return "final_edit_invalid";
    }
    if (!/^[0-9a-f]{64}$/.test(edit.current_sha256)) return `final_hash_invalid:${edit.path}`;
    if (!allowedWrites.some((pattern) => vscodeLmPathMatchesPattern(edit.path, pattern))) {
      return `final_path_not_allowed:${edit.path}`;
    }
    for (const range of edit.ranges) {
      if (!range || !Number.isSafeInteger(range.start_line) || range.start_line < 1 ||
          !Number.isSafeInteger(range.end_line) || range.end_line < range.start_line ||
          typeof range.new !== "string" ||
          (range.preserve_trailing_newline !== undefined && typeof range.preserve_trailing_newline !== "boolean")) {
        return `final_range_invalid:${edit.path}`;
      }
    }
  }
  for (const create of envelope.creates) {
    if (!create || typeof create.path !== "string" || typeof create.content !== "string") {
      return "final_create_invalid";
    }
    if (!allowedWrites.some((pattern) => vscodeLmPathMatchesPattern(create.path, pattern))) {
      return `final_path_not_allowed:${create.path}`;
    }
  }
  return "";
}

function glmTextToolProtocolPrompt(prompt, allowedWrites, sourceGraphPrefetched = false, pathContracts = {}) {
  const toolNames = VSCODE_LM_PRIVATE_TOOLS.map((tool) => tool.name);
  return `${glmAgentProtocolPrompt(prompt, allowedWrites, pathContracts)}\n` +
    `This provider does not expose native tool calling. Use the strict JSON transport below.\n` +
    (sourceGraphPrefetched
      ? `- The bridge has already executed the mandatory initial Source Graph request and supplies its result below. Request more tools only when needed.\n`
      : `- Your FIRST response MUST be a Source Graph request and nothing else.\n`) +
    `- For every tool call output ONLY: {"schema_id":"${VSCODE_LM_TOOL_REQUEST_SCHEMA}","name":"aiworkhub_manager_source_graph_query","input":{"mode":"focus","query":"..."}}\n` +
    `- After each tool result, either request another allowlisted tool or output the final ${VSCODE_LM_EDIT_RESPONSE_SCHEMA} object.\n` +
    `- Allowed tool names: ${JSON.stringify(toolNames)}.\n` +
    `- Never wrap JSON in Markdown or add prose.`;
}

async function collectVscodeLmResponseText(response) {
  const textParts = [];
  const observations = { text: [], stream: [] };
  // VS Code documents `response.text` as a filtered view of `response.stream`,
  // not an independent response channel.  Draining the filtered view first
  // can consume and discard non-text parts before diagnostics or a native
  // compatibility path can observe them.  The typed stream is therefore the
  // authority; use `text` only for older/custom responses with no stream.
  const collectParts = async (iterable, channel) => {
    if (!iterable || typeof iterable[Symbol.asyncIterator] !== "function") return;
    for await (const part of iterable) {
      if (part && typeof part.value === "string") textParts.push(part.value);
      else if (typeof part === "string") textParts.push(part);
      else if (observations[channel].length < 4) {
        const value = part && part.value;
        observations[channel].push({
          part_type: typeof part,
          part_constructor: String(part && part.constructor && part.constructor.name || ""),
          keys: part && typeof part === "object" ? Object.keys(part).slice(0, 8) : [],
          value_type: typeof value,
          value_constructor: String(value && value.constructor && value.constructor.name || ""),
        });
      }
    }
  };
  const textChannel = response && response.text;
  const streamChannel = response && response.stream;
  if (streamChannel && typeof streamChannel[Symbol.asyncIterator] === "function") {
    await collectParts(streamChannel, "stream");
  } else {
    await collectParts(textChannel, "text");
  }
  const text = textParts.join("").trim();
  if (!text) {
    const error = new Error("vscode_lm_empty_response");
    error.responseDiagnostics = {
      text_channel_available: Boolean(textChannel && typeof textChannel[Symbol.asyncIterator] === "function"),
      stream_channel_available: Boolean(streamChannel && typeof streamChannel[Symbol.asyncIterator] === "function"),
      text_parts: observations.text,
      stream_parts: observations.stream,
    };
    throw error;
  }
  if (Buffer.byteLength(text, "utf8") > VSCODE_LM_MAX_EMULATED_RESPONSE_BYTES) {
    throw new Error("vscode_lm_text_response_too_large");
  }
  return text;
}

function parseVscodeLmJsonEnvelope(text, { preferFinal = false } = {}) {
  const source = String(text || "").trim();
  const normalize = (candidate) => {
    if (!candidate || typeof candidate !== "object" || Array.isArray(candidate)) return null;
    if ([VSCODE_LM_TOOL_REQUEST_SCHEMA, VSCODE_LM_EDIT_RESPONSE_SCHEMA, VSCODE_LM_EDIT_RESPONSE_SCHEMA_V2, VSCODE_LM_EDIT_RESPONSE_SCHEMA_V1].includes(candidate.schema_id)) {
      return candidate;
    }
    // GLM custom endpoints commonly serialize a function request using an
    // OpenAI-style object even when VS Code reports toolCalling=false.  Map
    // only one allowlisted read-only AIWorkHub call into our private schema.
    let call = candidate;
    if (Array.isArray(candidate.tool_calls) && candidate.tool_calls.length === 1) {
      [call] = candidate.tool_calls;
    }
    if (call && call.function && typeof call.function === "object") call = call.function;
    const name = String((call && (call.name || call.tool)) || "");
    let input = call && (call.input !== undefined ? call.input : call.arguments);
    if (typeof input === "string") {
      try { input = JSON.parse(input); } catch (_err) { return null; }
    }
    if (VSCODE_LM_PRIVATE_TOOLS.some((tool) => tool.name === name) &&
        input && typeof input === "object" && !Array.isArray(input)) {
      return { schema_id: VSCODE_LM_TOOL_REQUEST_SCHEMA, name, input };
    }
    // Likewise tolerate an omitted final schema only when the complete edit
    // response shape is unmistakable; downstream validation still enforces
    // allowed paths and complete UTF-8 file contents.
    if (typeof candidate.summary === "string" && Array.isArray(candidate.edits) && Array.isArray(candidate.creates)) {
      return { ...candidate, schema_id: VSCODE_LM_EDIT_RESPONSE_SCHEMA };
    }
    if (typeof candidate.summary === "string" && Array.isArray(candidate.files)) {
      return { ...candidate, schema_id: VSCODE_LM_EDIT_RESPONSE_SCHEMA_V1 };
    }
    return null;
  };
  let payload;
  try {
    payload = normalize(JSON.parse(source));
    if (!payload) throw new Error("unsupported_json_shape");
  } catch (_err) {
    // Some VS Code LM providers prepend a short explanation or wrap an
    // otherwise valid envelope in a Markdown JSON fence even when instructed
    // not to.  Recover exactly one supported top-level object without ever
    // evaluating text, accepting nested tool input braces, or silently
    // choosing between multiple envelopes.
    const candidates = [];
    let start = -1;
    let depth = 0;
    let inString = false;
    let escaped = false;
    for (let index = 0; index < source.length; index += 1) {
      const char = source[index];
      if (inString) {
        if (escaped) escaped = false;
        else if (char === "\\") escaped = true;
        else if (char === '"') inString = false;
        continue;
      }
      if (char === '"') {
        inString = true;
        continue;
      }
      if (char === "{") {
        if (depth === 0) start = index;
        depth += 1;
      } else if (char === "}" && depth > 0) {
        depth -= 1;
        if (depth === 0 && start >= 0) {
          const candidateText = source.slice(start, index + 1);
          try {
            const candidate = JSON.parse(candidateText);
            const normalized = normalize(candidate);
            if (normalized) candidates.push(normalized);
          } catch (_candidateErr) {
            // Keep scanning; malformed prose fragments are not envelopes.
          }
          start = -1;
        }
      }
    }
    const uniqueCandidates = [];
    const seen = new Set();
    for (const candidate of candidates) {
      const identity = JSON.stringify(candidate);
      if (!seen.has(identity)) {
        seen.add(identity);
        uniqueCandidates.push(candidate);
      }
    }
    if (uniqueCandidates.length === 1) return uniqueCandidates[0];
    if (preferFinal) {
      const finals = uniqueCandidates.filter((candidate) =>
        candidate.schema_id === VSCODE_LM_EDIT_RESPONSE_SCHEMA || candidate.schema_id === VSCODE_LM_EDIT_RESPONSE_SCHEMA_V2 || candidate.schema_id === VSCODE_LM_EDIT_RESPONSE_SCHEMA_V1
      );
      // Text-only GLM endpoints may narrate or restate the prompt's example
      // envelope before emitting the real final object. The last complete
      // final envelope is the provider's answer; downstream validation still
      // fail-closes schema, allowed path, content and required output.
      if (finals.length >= 1) return finals[finals.length - 1];
    }
    const toolRequests = uniqueCandidates.filter(
      (candidate) => candidate.schema_id === VSCODE_LM_TOOL_REQUEST_SCHEMA,
    );
    // Providers may echo a protocol example before their actual request.
    // Every bridge tool is read-only and allowlisted; selecting the last
    // complete request is deterministic and still passes the normal name,
    // input-size, Source Graph ordering and invocation gates below.  Once
    // Source Graph is acknowledged a final envelope still wins above.
    if (toolRequests.length >= 1) return toolRequests[toolRequests.length - 1];
    if (uniqueCandidates.length !== 1) {
      const error = new Error(uniqueCandidates.length > 1
        ? "vscode_lm_text_protocol_ambiguous_json"
        : "vscode_lm_text_protocol_invalid_json");
      error.protocolPreview = source.slice(0, 768).replace(/\s+/g, " ");
      throw error;
    }
    [payload] = uniqueCandidates;
  }
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new Error("vscode_lm_text_protocol_invalid_object");
  }
  return payload;
}

function vscodeLmProtocolFailure(code, trace, preview = "") {
  const error = new Error(code);
  error.protocolPreview = sanitizeStderrChunk(String(preview || "")).slice(0, 768);
  error.protocolTrace = Array.isArray(trace) ? trace.slice(-16) : [];
  return error;
}

async function runVscodeLmTextProtocol(
  model,
  request,
  cancellationToken,
  invokeTool = invokeVscodeLmPrivateTool,
) {
  let sourceGraphAcknowledged = false;
  let initialSourceGraphResult = null;
  if (request.initial_source_graph_request) {
    initialSourceGraphResult = await invokeTool({
      name: "aiworkhub_manager_source_graph_query",
      input: request.initial_source_graph_request,
    }, request.requestId);
    if (!initialSourceGraphResult || initialSourceGraphResult.ok !== true) {
      throw new Error("vscode_lm_initial_source_graph_failed");
    }
    sourceGraphAcknowledged = true;
  }
  const messages = [vscode.LanguageModelChatMessage.User(
      glmTextToolProtocolPrompt(request.prompt, request.allowedWrites, sourceGraphAcknowledged, request.path_contracts) +
      (sourceGraphAcknowledged
        ? `\nINITIAL_SOURCE_GRAPH_RESULT:${JSON.stringify(initialSourceGraphResult)}`
        : ""),
  )];
  let postSourceTurns = 0;
  let finalizationTurns = 0;
  let forceFinal = false;
  const protocolTrace = [];
  let lastProtocolPreview = "";
  for (let turn = 0; turn < VSCODE_LM_MAX_AGENT_TURNS; turn += 1) {
    if (sourceGraphAcknowledged && postSourceTurns >= VSCODE_LM_MAX_POST_SOURCE_TURNS) {
      forceFinal = true;
    }
    if (forceFinal) {
      if (finalizationTurns >= VSCODE_LM_MAX_FINALIZATION_TURNS) {
        throw vscodeLmProtocolFailure("vscode_lm_finalization_limit", protocolTrace, lastProtocolPreview);
      }
      finalizationTurns += 1;
      messages.push(vscode.LanguageModelChatMessage.User(
        `The bounded tool/reasoning phase is complete. Do not request more tools. ` +
        `Output ONLY one final ${VSCODE_LM_EDIT_RESPONSE_SCHEMA} JSON object matching ` +
        `allowed_writes=${JSON.stringify(request.allowedWrites)}.`,
      ));
    }
    const response = await model.sendRequest(messages, {
      justification: "Run this explicitly queued AIWorkHub repository task using the user's existing VS Code model authorization.",
    }, cancellationToken);
    let text;
    try {
      text = await collectVscodeLmResponseText(response);
    } catch (err) {
      if (String(err && err.message || err) !== "vscode_lm_empty_response") throw err;
      messages.push(vscode.LanguageModelChatMessage.User(
        forceFinal
          ? `The previous provider turn contained no text. Output ONLY one final ${VSCODE_LM_EDIT_RESPONSE_SCHEMA} JSON object.`
          : `The previous provider turn contained no text. Retry without prose and output only the next strict ` +
            `${VSCODE_LM_TOOL_REQUEST_SCHEMA} tool request or final ${VSCODE_LM_EDIT_RESPONSE_SCHEMA} JSON object.`,
      ));
      if (sourceGraphAcknowledged) postSourceTurns += 1;
      protocolTrace.push({
        turn,
        phase: forceFinal ? "final" : "work",
        outcome: "empty",
        response: err && err.responseDiagnostics || {},
      });
      continue;
    }
    if (sourceGraphAcknowledged) postSourceTurns += 1;
    let envelope;
    try {
      envelope = parseVscodeLmJsonEnvelope(text, { preferFinal: sourceGraphAcknowledged });
    } catch (err) {
      lastProtocolPreview = String((err && err.protocolPreview) || text || "");
      protocolTrace.push({ turn, phase: forceFinal ? "final" : "work", outcome: sanitizeErrorMessage(err) });
      messages.push(vscode.LanguageModelChatMessage.Assistant([languageModelTextPart(text)]));
      messages.push(vscode.LanguageModelChatMessage.User(
        forceFinal
          ? `The previous response was not valid final JSON. Output ONLY one ${VSCODE_LM_EDIT_RESPONSE_SCHEMA} JSON object.`
          : `The previous response was not a supported transport envelope. Output ONLY one strict ` +
            `${sourceGraphAcknowledged ? `${VSCODE_LM_EDIT_RESPONSE_SCHEMA} final object or allowlisted ${VSCODE_LM_TOOL_REQUEST_SCHEMA} request` : `${VSCODE_LM_TOOL_REQUEST_SCHEMA} Source Graph request`} with no prose.`,
      ));
      continue;
    }
    if (envelope.schema_id === VSCODE_LM_EDIT_RESPONSE_SCHEMA || envelope.schema_id === VSCODE_LM_EDIT_RESPONSE_SCHEMA_V2 || envelope.schema_id === VSCODE_LM_EDIT_RESPONSE_SCHEMA_V1) {
      if (!sourceGraphAcknowledged) throw new Error("vscode_lm_source_graph_not_acknowledged");
      const finalError = validateVscodeLmFinalEnvelope(envelope, request.allowedWrites);
      if (finalError) {
        protocolTrace.push({ turn, phase: forceFinal ? "final" : "work", outcome: finalError });
        messages.push(vscode.LanguageModelChatMessage.Assistant([languageModelTextPart(text)]));
        messages.push(vscode.LanguageModelChatMessage.User(
          `The previous final envelope was rejected (${finalError}). ` +
          `Output ONLY one corrected ${VSCODE_LM_EDIT_RESPONSE_SCHEMA} JSON object. ` +
          `Every file path must match allowed_writes=${JSON.stringify(request.allowedWrites)}.`,
        ));
        continue;
      }
      // Always hand the Python worker a canonical JSON-only payload even when
      // the provider surrounded the envelope with prose or a Markdown fence.
      return JSON.stringify(envelope);
    }
    if (forceFinal) {
      protocolTrace.push({ turn, phase: "final", outcome: "tool_request_rejected" });
      messages.push(vscode.LanguageModelChatMessage.Assistant([languageModelTextPart(text)]));
      messages.push(vscode.LanguageModelChatMessage.User(
        `Tool requests are no longer accepted. Output ONLY one final ${VSCODE_LM_EDIT_RESPONSE_SCHEMA} JSON object.`,
      ));
      continue;
    }
    if (envelope.schema_id !== VSCODE_LM_TOOL_REQUEST_SCHEMA) {
      throw new Error("vscode_lm_text_protocol_schema_mismatch");
    }
    const availableTools = sourceGraphAcknowledged ? VSCODE_LM_PRIVATE_TOOLS : [VSCODE_LM_PRIVATE_TOOLS[0]];
    const permitted = availableTools.find((tool) => tool.name === envelope.name);
    if (!permitted) throw new Error(`vscode_lm_tool_not_allowed:${String(envelope.name || "")}`);
    if (!envelope.input || typeof envelope.input !== "object" || Array.isArray(envelope.input)) {
      throw new Error("vscode_lm_tool_input_invalid");
    }
    if (Buffer.byteLength(JSON.stringify(envelope.input), "utf8") > VSCODE_LM_MAX_EMULATED_TOOL_INPUT_BYTES) {
      throw new Error("vscode_lm_tool_input_too_large");
    }
    let result;
    try {
      result = await invokeTool({ name: envelope.name, input: envelope.input }, request.requestId);
    } catch (err) {
      result = { ok: false, error: sanitizeErrorMessage(err) };
    }
    if (envelope.name === "aiworkhub_manager_source_graph_query" && result && result.ok === true) {
      sourceGraphAcknowledged = true;
    }
    protocolTrace.push({ turn, phase: "work", outcome: `tool:${envelope.name}` });
    messages.push(vscode.LanguageModelChatMessage.Assistant([languageModelTextPart(text)]));
    messages.push(vscode.LanguageModelChatMessage.User(JSON.stringify({
      schema_id: VSCODE_LM_TOOL_RESULT_SCHEMA,
      name: envelope.name,
      result,
      instruction: "Output only the next strict tool-request JSON or final edit-response JSON object.",
    })));
  }
  throw vscodeLmProtocolFailure("vscode_lm_agent_turn_limit", protocolTrace, lastProtocolPreview);
}

async function runVscodeLmAgent(
  model,
  request,
  cancellationToken,
  invokeTool = invokeVscodeLmPrivateTool,
) {
  if (!model) throw new Error("vscode_lm_model_not_visible");
  if (!model.capabilities || !model.capabilities.toolCalling) {
    return runVscodeLmTextProtocol(model, request, cancellationToken);
  }
  const messages = [vscode.LanguageModelChatMessage.User(glmAgentProtocolPrompt(request.prompt, request.allowedWrites, request.path_contracts))];
  let sourceGraphAcknowledged = false;
  let toolTurns = 0;
  let postSourceTurns = 0;
  let finalizationTurns = 0;
  let forceFinal = false;
  const protocolTrace = [];
  let lastProtocolPreview = "";
  for (let turn = 0; turn < VSCODE_LM_MAX_AGENT_TURNS; turn += 1) {
    if (sourceGraphAcknowledged &&
        (toolTurns >= VSCODE_LM_MAX_TOOL_TURNS || postSourceTurns >= VSCODE_LM_MAX_POST_SOURCE_TURNS) &&
        !forceFinal) {
      forceFinal = true;
      messages.push(vscode.LanguageModelChatMessage.User(
        `The bounded tool phase is complete. Do not request more tools. ` +
        `Output ONLY one final ${VSCODE_LM_EDIT_RESPONSE_SCHEMA} JSON object matching ` +
        `allowed_writes=${JSON.stringify(request.allowedWrites)}.`,
      ));
    }
    if (forceFinal) {
      if (finalizationTurns >= VSCODE_LM_MAX_FINALIZATION_TURNS) {
        throw vscodeLmProtocolFailure("vscode_lm_finalization_limit", protocolTrace, lastProtocolPreview);
      }
      finalizationTurns += 1;
    }
    const startedWithSourceGraph = sourceGraphAcknowledged;
    const availableTools = sourceGraphAcknowledged ? VSCODE_LM_PRIVATE_TOOLS : [VSCODE_LM_PRIVATE_TOOLS[0]];
    const options = {
      justification: "Run this explicitly queued AIWorkHub repository task using the user's existing VS Code model authorization.",
    };
    if (!forceFinal) {
      options.tools = availableTools;
      options.toolMode = sourceGraphAcknowledged ? vscode.LanguageModelChatToolMode.Auto : vscode.LanguageModelChatToolMode.Required;
    }
    const response = await model.sendRequest(messages, options, cancellationToken);
    const assistantParts = [];
    const textParts = [];
    const calls = [];
    for await (const part of response.stream) {
      assistantParts.push(part);
      if (isLanguageModelToolCallPart(part)) calls.push(part);
      else if (part && typeof part.value === "string") textParts.push(part.value);
      else if (typeof part === "string") textParts.push(part);
    }
    if (startedWithSourceGraph) postSourceTurns += 1;
    if (calls.length === 0) {
      if (!sourceGraphAcknowledged) throw new Error("vscode_lm_source_graph_not_acknowledged");
      const text = textParts.join("").trim();
      if (!text) {
        protocolTrace.push({ turn, phase: forceFinal ? "final" : "work", outcome: "empty" });
        messages.push(vscode.LanguageModelChatMessage.User(
          `The previous provider turn contained neither a tool call nor text. ` +
          `Output ONLY one final ${VSCODE_LM_EDIT_RESPONSE_SCHEMA} JSON object matching ` +
          `allowed_writes=${JSON.stringify(request.allowedWrites)}.`,
        ));
        continue;
      }
      let envelope;
      try {
        envelope = parseVscodeLmJsonEnvelope(text, { preferFinal: true });
      } catch (err) {
        lastProtocolPreview = String((err && err.protocolPreview) || text || "");
        protocolTrace.push({ turn, phase: forceFinal ? "final" : "work", outcome: sanitizeErrorMessage(err) });
        messages.push(vscode.LanguageModelChatMessage.Assistant([languageModelTextPart(text)]));
        messages.push(vscode.LanguageModelChatMessage.User(
          `The previous response was not a supported JSON envelope. ` +
          `Output ONLY one final ${VSCODE_LM_EDIT_RESPONSE_SCHEMA} JSON object matching ` +
          `allowed_writes=${JSON.stringify(request.allowedWrites)}.`,
        ));
        continue;
      }
      if (envelope.schema_id !== VSCODE_LM_EDIT_RESPONSE_SCHEMA && envelope.schema_id !== VSCODE_LM_EDIT_RESPONSE_SCHEMA_V2 && envelope.schema_id !== VSCODE_LM_EDIT_RESPONSE_SCHEMA_V1) {
        protocolTrace.push({ turn, phase: forceFinal ? "final" : "work", outcome: "non_final_envelope" });
        messages.push(vscode.LanguageModelChatMessage.Assistant([languageModelTextPart(text)]));
        messages.push(vscode.LanguageModelChatMessage.User(
          `Tool discovery is complete. Output ONLY one final ${VSCODE_LM_EDIT_RESPONSE_SCHEMA} JSON object.`,
        ));
        continue;
      }
      const finalError = validateVscodeLmFinalEnvelope(envelope, request.allowedWrites);
      if (finalError) {
        protocolTrace.push({ turn, phase: forceFinal ? "final" : "work", outcome: finalError });
        messages.push(vscode.LanguageModelChatMessage.Assistant([languageModelTextPart(text)]));
        messages.push(vscode.LanguageModelChatMessage.User(
          `The previous final envelope was rejected (${finalError}). ` +
          `Output ONLY one corrected ${VSCODE_LM_EDIT_RESPONSE_SCHEMA} JSON object. ` +
          `Every file path must match allowed_writes=${JSON.stringify(request.allowedWrites)}.`,
        ));
        continue;
      }
      return JSON.stringify(envelope);
    }
    if (forceFinal && calls.length > 0) {
      protocolTrace.push({ turn, phase: "final", outcome: "tool_call_rejected" });
      messages.push(vscode.LanguageModelChatMessage.Assistant(assistantParts));
      messages.push(vscode.LanguageModelChatMessage.User(
        `Tool calls are no longer available in the finalization phase. ` +
        `Output ONLY one final ${VSCODE_LM_EDIT_RESPONSE_SCHEMA} JSON object.`,
      ));
      continue;
    }
    messages.push(vscode.LanguageModelChatMessage.Assistant(assistantParts));
    const results = [];
    for (const call of calls) {
      let result;
      try {
        result = await invokeTool(call, request.requestId);
      } catch (err) {
        result = { ok: false, error: sanitizeErrorMessage(err) };
      }
      if (call.name === "aiworkhub_manager_source_graph_query" && result && result.ok === true) {
        sourceGraphAcknowledged = true;
      }
      results.push(languageModelToolResultPart(call.callId, result));
    }
    toolTurns += 1;
    protocolTrace.push({ turn, phase: "work", outcome: `tools:${calls.length}` });
    messages.push(vscode.LanguageModelChatMessage.User(results));
  }
  throw vscodeLmProtocolFailure("vscode_lm_agent_turn_limit", protocolTrace, lastProtocolPreview);
}

class VscodeLmBridgeHost {
  constructor(context) {
    this.context = context;
    this.repoInfo = null;
    this.pollTimer = null;
    this.heartbeatTimer = null;
    this.activeClaims = new Set();
    this.activeSources = new Set();
    this.maxParallelRequests = VSCODE_LM_MAX_PARALLEL_REQUESTS;
    this.permissionPrompts = new Map();
    this.disposed = false;
  }

  async start(repoInfo) {
    this.stop();
    if (!repoInfo || !REAL_REPO_ID_RE.test(String(repoInfo.repoId || ""))) return;
    this.repoInfo = { ...repoInfo };
    // The broker is an optional execution route.  A malformed third-party
    // model catalog or a transient provider failure must never abort the
    // dashboard/MCP extension activation.  The timer retries and preflight
    // reports the missing/stale heartbeat as bounded degraded evidence.
    try {
      await this.publishHeartbeat();
    } catch (err) {
      recordSystemLog(`[vscode lm bridge] heartbeat unavailable ${sanitizeErrorMessage(err)}`);
    }
    this.pollTimer = setInterval(() => this.poll().catch((err) => recordSystemLog(`[glm bridge] ERROR ${sanitizeErrorMessage(err)}`)), VSCODE_LM_POLL_MS);
    this.heartbeatTimer = setInterval(() => this.publishHeartbeat().catch(() => {}), VSCODE_LM_HEARTBEAT_MS);
    if (this.pollTimer && typeof this.pollTimer.unref === "function") this.pollTimer.unref();
    if (this.heartbeatTimer && typeof this.heartbeatTimer.unref === "function") this.heartbeatTimer.unref();
  }

  stop() {
    if (this.pollTimer) clearInterval(this.pollTimer);
    if (this.heartbeatTimer) clearInterval(this.heartbeatTimer);
    this.pollTimer = null;
    this.heartbeatTimer = null;
    for (const source of this.activeSources) {
      try { source.cancel(); } catch (_err) { /* already disposed */ }
    }
    if (this.repoInfo && REAL_REPO_ID_RE.test(String(this.repoInfo.repoId || ""))) {
      const hostPath = path.join(vscodeLmBridgeRoot(), "hosts", this.repoInfo.repoId, `${WINDOW_SCOPE_ID}.json`);
      try { fs.unlinkSync(hostPath); } catch (_err) { /* absent */ }
    }
    this.repoInfo = null;
  }

  dispose() {
    this.disposed = true;
    this.stop();
  }

  async models() {
    if (!vscode.lm || typeof vscode.lm.selectChatModels !== "function") return [];
    const models = await vscode.lm.selectChatModels();
    return models.filter(isCallableVscodeLmProvider);
  }

  modelAccessState(model) {
    const nativeState = vscodeLmAccessState(model);
    if (nativeState === "granted") return nativeState;
    const globalState = this.context && this.context.globalState;
    if (globalState && typeof globalState.get === "function" &&
        globalState.get(vscodeLmPermissionStorageKey(model), false)) {
      return "granted_remembered";
    }
    return nativeState;
  }

  async rememberModelPermission(model) {
    const globalState = this.context && this.context.globalState;
    if (globalState && typeof globalState.update === "function") {
      await globalState.update(vscodeLmPermissionStorageKey(model), true);
    }
  }

  async publishHeartbeat() {
    if (!this.repoInfo || this.disposed) return;
    const models = await this.models();
    const visibleModels = Array.from(new Set([
      ...VSCODE_LM_SUPPORTED_MODELS.filter((name) => selectVscodeLanguageModel(models, name)),
      ...models.flatMap((model) => vscodeLmModelFields(model))
        .map(canonicalVscodeLmModelName)
        .filter((name) => VSCODE_LM_SUPPORTED_MODELS.includes(name)),
      ...models.flatMap((model) => [model && model.id, model && model.family])
        .filter((name) => typeof name === "string" && VSCODE_LM_REQUESTED_MODEL_RE.test(name.trim()))
        .map((name) => name.trim()),
    ])).slice(0, 128);
    const modelMetadata = visibleModels.slice(0, 64).flatMap((name) => {
      const model = selectVscodeLanguageModel(models, name);
      if (!model) return [];
      return [{
        canonical: name,
        id: model.id,
        family: model.family,
        name: model.name,
        vendor: model.vendor,
        version: model.version,
        maxInputTokens: model.maxInputTokens,
        access_state: this.modelAccessState(model),
      }];
    });
    const payload = {
      schema_id: VSCODE_LM_HOST_SCHEMA,
      repo_id: this.repoInfo.repoId,
      window_id: WINDOW_SCOPE_ID,
      extension_host_pid: process.pid,
      models: visibleModels,
      model_metadata: modelMetadata,
      permission_granted: modelMetadata.some((entry) => entry.access_state.startsWith("granted")),
      max_parallel_requests: this.maxParallelRequests,
      active_request_count: this.activeClaims.size,
      updated_at: new Date().toISOString(),
    };
    const hostPath = path.join(vscodeLmBridgeRoot(), "hosts", this.repoInfo.repoId, `${WINDOW_SCOPE_ID}.json`);
    atomicWriteOwnerJson(hostPath, payload);
  }

  async ensurePermission(model) {
    if (this.modelAccessState(model).startsWith("granted")) return true;
    const permissionKey = vscodeLmPermissionStorageKey(model);
    if (!this.permissionPrompts.has(permissionKey)) {
      const label = String((model && (model.name || model.family || model.id)) || "the selected model");
      const prompt = vscode.window.showInformationMessage(
        `AIWorkHub has a queued worker task for ${label}. Allow this VS Code model?`,
        { modal: true, detail: "Permission is scoped to this exact VS Code model and is remembered immediately after approval. Provider failures will not ask again." },
        "Allow VS Code models",
      ).then(async (choice) => {
        if (choice !== "Allow VS Code models") return false;
        // Persist the user's decision before the provider request.  The old
        // success-only write created a circular failure: a timeout meant the
        // approval was forgotten and the same modal appeared on every retry.
        await this.rememberModelPermission(model);
        try { await this.publishHeartbeat(); } catch (_err) { /* next heartbeat retries */ }
        return true;
      }).finally(() => {
        if (this.permissionPrompts.get(permissionKey) === prompt) {
          this.permissionPrompts.delete(permissionKey);
        }
      });
      this.permissionPrompts.set(permissionKey, prompt);
    }
    return this.permissionPrompts.get(permissionKey);
  }

  async poll() {
    if (this.activeClaims.size >= this.maxParallelRequests || !this.repoInfo || this.disposed) return;
    // A request keeps the repository identity under which it was claimed.
    // start()/stop() may switch the dashboard to another repository while a
    // provider call is still in flight; never validate or report that old
    // request against the new repository identity.
    const repoInfo = { ...this.repoInfo };
    const requestDir = path.join(vscodeLmBridgeRoot(), "requests", repoInfo.repoId);
    let names;
    try { names = fs.readdirSync(requestDir).filter((name) => VSCODE_LM_REQUEST_ID_RE.test(path.basename(name, ".json")) && name.endsWith(".json")).sort(); }
    catch (_err) { return; }
    if (!names.length) return;
    let claimPath = null;
    try {
      const requestPath = path.join(requestDir, names[0]);
      claimPath = `${requestPath}.claim-${WINDOW_SCOPE_ID}`;
      try { fs.renameSync(requestPath, claimPath); } catch (_err) { return; }
      this.activeClaims.add(claimPath);
      await this.processClaim(claimPath, repoInfo);
    } catch (err) {
      recordSystemLog(`[glm bridge] ERROR ${sanitizeErrorMessage(err)}`);
    } finally {
      if (claimPath) {
        this.activeClaims.delete(claimPath);
        try { fs.unlinkSync(claimPath); } catch (_err) { /* absent */ }
      }
    }
  }

  async processClaim(claimPath, repoInfo) {
    let source = null;
    try {
      if (!ownerOnlyRegularFile(claimPath)) throw new Error("vscode_lm_request_not_owner_only");
      const payload = JSON.parse(fs.readFileSync(claimPath, "utf8"));
      const request = validateVscodeLmRequest(payload, repoInfo);
      const models = await this.models();
      const model = selectVscodeLanguageModel(models, request.model);
      if (!model) throw new Error("vscode_lm_model_not_visible");
      if (!(await this.ensurePermission(model))) throw new Error("vscode_lm_permission_denied");
      source = new vscode.CancellationTokenSource();
      this.activeSources.add(source);
      const remainingMs = Math.max(1, Date.parse(String(request.deadline)) - Date.now());
      const timer = setTimeout(() => source.cancel(), remainingMs);
      let text = "";
      let error = "";
      let diagnostics = null;
      try {
        text = await runVscodeLmAgent(model, request, source.token);
      }
      catch (err) {
        error = sanitizeErrorMessage(err);
        diagnostics = {
          protocol_preview: sanitizeStderrChunk(String((err && err.protocolPreview) || "")).slice(0, 768),
          turn_trace: Array.isArray(err && err.protocolTrace) ? err.protocolTrace.slice(-16) : [],
        };
        if (err && err.protocolPreview) {
          recordSystemLog(`[vscode lm bridge] rejected response preview ${diagnostics.protocol_preview}`);
        }
        recordSystemLog(`[vscode lm bridge] ${error} trace=${JSON.stringify(diagnostics.turn_trace)}`);
      }
      finally { clearTimeout(timer); source.dispose(); }
      atomicWriteOwnerJson(request.responsePath, {
        schema_id: VSCODE_LM_RESPONSE_SCHEMA,
        request_id: request.requestId,
        repo_id: repoInfo.repoId,
        model: { id: model.id, family: model.family, name: model.name, vendor: model.vendor, version: model.version },
        text,
        error,
        diagnostics,
        completed_at: new Date().toISOString(),
      });
    } catch (err) {
      recordSystemLog(`[glm bridge] ERROR ${sanitizeErrorMessage(err)}`);
    } finally {
      if (source) this.activeSources.delete(source);
    }
  }
}
const SYSTEM_LOG_MAX_ENTRIES = 1200;
const SYSTEM_LOG_MAX_LINE_CHARS = 800;
const SYSTEM_LOG_RETENTION_MS = 7 * 24 * 60 * 60 * 1000;
const SYSTEM_LOG_MAX_FILE_BYTES = 1024 * 1024;
const SYSTEM_LOG_MAX_PERSISTED_ENTRIES = SYSTEM_LOG_MAX_ENTRIES;
let systemLogSequence = 0;
let systemLogEntries = [];
let systemLogRepoRoot = "";
let systemLogFlushTimer = null;

function systemLogFile(root) {
  return path.join(root, ".aiworkhub", "runtime", "logs", "dashboard-system.json");
}

// Single pass, one measurement per entry. The previous shape re-serialized
// the WHOLE retained array on every iteration of a pop loop, and this runs
// once per log line -- every `[mcp stderr]` chunk and every tool call. At the
// 1 MiB cap that is megabytes of JSON per logged line, on the extension-host
// thread, which is exactly how a host stops answering VS Code's ping and gets
// terminated -- taking every other extension in the window with it.
// Entries arrive newest-first, so filling from the front and stopping at the
// cap keeps the newest and drops the oldest, as before.
function pruneSystemLogs(entries) {
  const cutoff = Date.now() - SYSTEM_LOG_RETENTION_MS;
  const retained = [];
  let totalBytes = 2; // the enclosing "[]"
  for (const entry of entries) {
    if (retained.length >= SYSTEM_LOG_MAX_PERSISTED_ENTRIES) break;
    const at = Date.parse(entry.timestamp);
    if (!Number.isFinite(at) || at < cutoff) continue;
    const size = Buffer.byteLength(JSON.stringify(entry), "utf8") + 1; // + comma
    if (retained.length > 0 && totalBytes + size > SYSTEM_LOG_MAX_FILE_BYTES) break;
    retained.push(entry);
    totalBytes += size;
  }
  return retained;
}

function flushSystemLogs() {
  if (systemLogFlushTimer) {
    clearTimeout(systemLogFlushTimer);
    systemLogFlushTimer = null;
  }
  if (!systemLogRepoRoot || !fs.existsSync(path.join(systemLogRepoRoot, ".aiworkhub", "project.json"))) return;
  systemLogEntries = pruneSystemLogs(systemLogEntries);
  try {
    atomicWriteJson(systemLogFile(systemLogRepoRoot), {
      schema_id: "aiworkhub.dashboard_system_log.v1",
      retention_days: 7,
      max_bytes: SYSTEM_LOG_MAX_FILE_BYTES,
      entries: systemLogEntries,
    });
  } catch (_err) {
    // Logging must never take the dashboard or MCP lifecycle down.
  }
}

function scheduleSystemLogFlush() {
  if (!systemLogRepoRoot || systemLogFlushTimer) return;
  systemLogFlushTimer = setTimeout(flushSystemLogs, 500);
}

function bindSystemLogRepository(root) {
  if (!root || root === systemLogRepoRoot) return;
  flushSystemLogs();
  const startupEntries = systemLogRepoRoot ? [] : systemLogEntries.slice();
  systemLogRepoRoot = root;
  let persisted = [];
  try {
    const file = systemLogFile(root);
    const stat = fs.lstatSync(file);
    if (stat.isFile() && stat.size <= SYSTEM_LOG_MAX_FILE_BYTES + 4096) {
      const payload = JSON.parse(fs.readFileSync(file, "utf8"));
      persisted = Array.isArray(payload.entries) ? payload.entries : [];
    }
  } catch (_err) {
    persisted = [];
  }
  systemLogEntries = pruneSystemLogs([...startupEntries, ...persisted]);
  scheduleSystemLogFlush();
}

function systemLogLevel(message) {
  const normalized = String(message || "").toLowerCase();
  if (normalized.includes("error") || normalized.includes("failed") || normalized.includes("mismatch")) return "error";
  if (normalized.includes("warning") || normalized.includes("degraded")) return "warning";
  return "info";
}

function recordSystemLog(value) {
  const raw = sanitizeStderrChunk(String(value || ""));
  for (const candidate of raw.split(/\r?\n/)) {
    const message = candidate.trim().slice(0, SYSTEM_LOG_MAX_LINE_CHARS);
    if (!message) continue;
    const componentMatch = /^\[([^\]]{1,24})\]\s*/.exec(message);
    systemLogEntries.unshift({
      sequence: ++systemLogSequence,
      timestamp: new Date().toISOString(),
      level: systemLogLevel(message),
      component: componentMatch ? componentMatch[1] : "system",
      message: componentMatch ? message.slice(componentMatch[0].length) : message,
    });
  }
  systemLogEntries = pruneSystemLogs(systemLogEntries);
  scheduleSystemLogFlush();
}

function systemLogSnapshot() {
  return systemLogEntries.slice(0, SYSTEM_LOG_MAX_ENTRIES).map((entry) => ({ ...entry }));
}

function clearSystemLogs() {
  systemLogEntries = [];
  flushSystemLogs();
}

function createManagedOutputChannel() {
  const channel = vscode.window.createOutputChannel("AIWorkHub");
  const appendLine = channel.appendLine.bind(channel);
  channel.appendLine = (value) => {
    try {
      appendLine(value);
    } catch (_err) {
      // Child stderr can race extension-host disposal on Windows. Logging must
      // never turn a normal reload/close into an extension-host exception.
    }
    recordSystemLog(value);
  };
  return channel;
}

// Installed VSIX directories are versioned and VS Code is free to remove the
// previous directory as soon as an upgrade lands.  Long-lived Codex/MCP
// processes must therefore never import from `<extensions>/aiworkhub-X/runtime`:
// a second window upgrading the extension would orphan the first window's
// process and every worker it launches.  Materialise immutable, content-
// addressed runtime generations under extension-global storage instead.
// Generations are intentionally retained; a later bounded lease-aware GC may
// remove unused ones, but an upgrade must never delete code beneath a live
// process.
const STABLE_RUNTIME_SCHEMA = "aiworkhub.stable_runtime.v1";

function _runtimeTreeFingerprint(root) {
  const hash = crypto.createHash("sha256");
  function visit(dir, relBase) {
    const entries = fs.readdirSync(dir, { withFileTypes: true })
      .filter((entry) => entry.name !== "__pycache__" && entry.name !== ".pytest_cache")
      .sort((a, b) => a.name.localeCompare(b.name));
    for (const entry of entries) {
      const rel = path.join(relBase, entry.name);
      const full = path.join(dir, entry.name);
      if (entry.isFile()) {
        hash.update(`F\0${rel}\0`);
        hash.update(fs.readFileSync(full));
      } else if (entry.isSymbolicLink()) {
        throw new Error(`stable_runtime_unsupported_entry:${rel}`);
      } else {
        hash.update(`D\0${rel}\0`);
        visit(full, rel);
      }
    }
  }
  visit(root, "");
  return hash.digest("hex");
}

function _copyRuntimeTree(source, destination) {
  fs.mkdirSync(destination, { recursive: true });
  const entries = fs.readdirSync(source, { withFileTypes: true });
  for (const entry of entries) {
    if (entry.name === "__pycache__" || entry.name === ".pytest_cache") continue;
    const src = path.join(source, entry.name);
    const dst = path.join(destination, entry.name);
    if (entry.isFile()) {
      fs.copyFileSync(src, dst);
    } else if (entry.isSymbolicLink()) {
      throw new Error(`stable_runtime_unsupported_entry:${entry.name}`);
    } else {
      _copyRuntimeTree(src, dst);
    }
  }
}

function materializeStableRuntimeGeneration(context) {
  const sourceRuntime = resolveExtensionRuntimeDir(context.extensionUri.fsPath);
  const sourcePackage = path.join(sourceRuntime, "aiworkhub", "__init__.py");
  if (!fs.existsSync(sourcePackage)) {
    throw new Error(`bundled_runtime_missing:${sourceRuntime}`);
  }
  // Minimal unit-test hosts do not provide VS Code's globalStorageUri.
  if (!context.globalStorageUri || !context.globalStorageUri.fsPath) {
    return {
      runtimeDir: sourceRuntime,
      generationRoot: sourceRuntime,
      fingerprint: _runtimeTreeFingerprint(sourceRuntime),
      version: String((context.extension && context.extension.packageJSON && context.extension.packageJSON.version) || EXPECTED_MCP_PACKAGE_VERSION),
      storageRoot: null,
    };
  }
  const version = String(
    (context.extension && context.extension.packageJSON && context.extension.packageJSON.version)
      || EXPECTED_MCP_PACKAGE_VERSION,
  );
  const fingerprint = _runtimeTreeFingerprint(sourceRuntime);
  const storageRoot = context.globalStorageUri.fsPath;
  const generationsRoot = path.join(storageRoot, "runtime", "generations");
  const generationName = `${version}-${fingerprint.slice(0, 16)}`;
  const generationRoot = path.join(generationsRoot, generationName);
  const runtimeDir = path.join(generationRoot, "runtime");
  const manifestPath = path.join(generationRoot, "manifest.json");

  let ready = false;
  try {
    const manifest = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
    ready = manifest.schema_id === STABLE_RUNTIME_SCHEMA
      && manifest.version === version
      && manifest.fingerprint === fingerprint
      && fs.existsSync(path.join(runtimeDir, "aiworkhub", "server.py"));
  } catch (_err) {
    ready = false;
  }

  if (!ready) {
    fs.mkdirSync(generationsRoot, { recursive: true });
    const staging = path.join(
      generationsRoot,
      `.${generationName}.${process.pid}.${crypto.randomBytes(6).toString("hex")}.tmp`,
    );
    fs.rmSync(staging, { recursive: true, force: true });
    try {
      _copyRuntimeTree(sourceRuntime, path.join(staging, "runtime"));
      atomicWriteJson(path.join(staging, "manifest.json"), {
        schema_id: STABLE_RUNTIME_SCHEMA,
        version,
        fingerprint,
        created_at: new Date().toISOString(),
      });
      // Same-content concurrent activations converge on one immutable
      // generation. Never replace a complete generation another window may
      // already be executing.
      try {
        fs.renameSync(staging, generationRoot);
      } catch (err) {
        if (!fs.existsSync(manifestPath)) throw err;
        fs.rmSync(staging, { recursive: true, force: true });
      }
    } catch (err) {
      fs.rmSync(staging, { recursive: true, force: true });
      throw err;
    }
  }

  atomicWriteJson(path.join(storageRoot, "runtime", "current.json"), {
    schema_id: STABLE_RUNTIME_SCHEMA,
    version,
    fingerprint,
    generation: generationName,
    runtime_dir: runtimeDir,
    updated_at: new Date().toISOString(),
  });
  return { runtimeDir, generationRoot, fingerprint, version, storageRoot };
}

function bundledRuntimeFallback(context) {
  const runtimeDir = resolveExtensionRuntimeDir(context.extensionUri.fsPath);
  if (!fs.existsSync(path.join(runtimeDir, "aiworkhub", "server.py"))) {
    throw new Error(`bundled_runtime_missing:${runtimeDir}`);
  }
  return {
    runtimeDir,
    generationRoot: null,
    fingerprint: "",
    version: String(
      (context.extension && context.extension.packageJSON && context.extension.packageJSON.version)
        || EXPECTED_MCP_PACKAGE_VERSION,
    ),
    storageRoot: null,
  };
}

/** Publish a usable runtime pointer before any expensive fingerprint/copy work.
 *
 * The packaged runtime is already complete and immutable for the lifetime of
 * this installed extension.  Publishing its pointer early keeps the optional
 * same-host mux shim coherent, but does not configure or replace Codex.  The
 * normal materializer replaces this bootstrap pointer atomically with the
 * content-addressed generation later in the same activation.
 */
function primeStableMuxRuntimePointer(context) {
  const runtimeDir = resolveExtensionRuntimeDir(context.extensionUri.fsPath);
  if (!fs.existsSync(path.join(runtimeDir, "aiworkhub", "app_server_mux.py"))) {
    throw new Error(`bundled_mux_runtime_missing:${runtimeDir}`);
  }
  if (!context.globalStorageUri || !context.globalStorageUri.fsPath) {
    return { runtimeDir, storageRoot: null };
  }
  const storageRoot = context.globalStorageUri.fsPath;
  atomicWriteJson(path.join(storageRoot, "runtime", "current.json"), {
    schema_id: STABLE_RUNTIME_SCHEMA,
    version: String(
      (context.extension && context.extension.packageJSON && context.extension.packageJSON.version)
        || EXPECTED_MCP_PACKAGE_VERSION,
    ),
    bootstrap: true,
    runtime_dir: runtimeDir,
    updated_at: new Date().toISOString(),
  });
  return { runtimeDir, storageRoot };
}

// ── Codex config.toml PYTHONPATH runtime migration (B894a) ─────────────────
// 0.6.20 compatibility: resolves ~/.codex/config.toml's location, honoring
// CODEX_HOME when set (matching Codex CLI's own resolution) and otherwise
// falling back to the current user's homedir. Never a hardcoded/global
// repo-relative fallback -- `env` defaults to process.env only so callers
// (and tests) can override it deterministically.
function resolveCodexConfigTomlPath(env) {
  const source = env || process.env || {};
  const codexHome = source.CODEX_HOME;
  if (codexHome) {
    return path.join(codexHome, "config.toml");
  }
  return path.join(os.homedir(), ".codex", "config.toml");
}

function codexConfigTomlPath() {
  return resolveCodexConfigTomlPath(process.env);
}

/** Resolve the directory that must be on PYTHONPATH for `python -m
 *  aiworkhub.server` to import the bundled runtime. A packaged VSIX ships the
 *  package at `<ext>/runtime/aiworkhub`; a development checkout has it at
 *  `<ext>/../src/aiworkhub` and only produces `runtime/` at VSIX build time.
 *  Prefer the packaged `runtime/`, fall back to the repo `src/` -- the SAME
 *  runtime/ -> src/ resolution used in development. Writing a `runtime/` path that does not exist is
 *  exactly what silently breaks Codex's `python -m aiworkhub.server`
 *  (ModuleNotFoundError: No module named 'aiworkhub') and stops the Codex chat
 *  from launching, so this never returns a directory that lacks the aiworkhub
 *  package unless NEITHER candidate exists (then the packaged path, so the
 *  existing repair still runs as before). */
function resolveExtensionRuntimeDir(extensionFsPath) {
  const packaged = path.join(extensionFsPath, "runtime");
  if (fs.existsSync(path.join(packaged, "aiworkhub", "__init__.py"))) return packaged;
  const devSrc = path.resolve(extensionFsPath, "..", "src");
  if (fs.existsSync(path.join(devSrc, "aiworkhub", "__init__.py"))) return devSrc;
  return packaged;
}

// Matches only extension-versioned AIWorkHub runtime paths this extension
// itself ever writes into PYTHONPATH (`.../shrec.aiworkhub-<version>/runtime`),
// on any OS path separator. An optional leading Windows drive letter (e.g.
// `C:`) is captured as part of the match so it is never left behind when the
// match is replaced -- omitting it would duplicate the drive prefix (see
// repairCodexConfigTomlText tests). A custom/non-AIWorkHub MCP entry's
// PYTHONPATH value never matches this shape, so it is left untouched.
const AIWORKHUB_RUNTIME_PATH_RE = /(?<![A-Za-z0-9_])(?:[A-Za-z]:)?[^\s"';:]*[\\/]shrec\.aiworkhub-[^\s"';:\\/]+[\\/]runtime/g;

// Broader match used ONLY by migrateCodexConfigTomlText: any safe VS Code
// extension-publisher prefix (marketplace publisher ids are
// lowercase-alphanumeric-and-hyphen, never a dot) ending in
// `.aiworkhub-<version>/runtime`, on any OS path separator -- a single
// backslash, a TOML-escaped doubled backslash (B894b: a Windows fixture path
// stored as `C:\\Users...publisher.aiworkhub-...\\runtime` in TOML's escaped
// string form), or POSIX `/`. This recognizes a versioned runtime installed
// under a publisher other than `shrec` (e.g. `publisher.aiworkhub-0.6.19/runtime`)
// without broadening to an arbitrary, non-versioned path.
// repairCodexConfigTomlText intentionally keeps using the strict shrec-only
// AIWORKHUB_RUNTIME_PATH_RE above.
const MIGRATABLE_AIWORKHUB_RUNTIME_PATH_RE = /(?<![A-Za-z0-9_])(?:[A-Za-z]:)?[^\s"';:]*(?:\\\\|\\|\/)[A-Za-z0-9][A-Za-z0-9-]*\.aiworkhub-[^\s"';:\\/]+(?:\\\\|\\|\/)runtime/g;

// 0.6.20 compatibility alias: current-main callers/tests refer to this
// pattern as "the AIWorkHub-owned runtime path segment this extension is
// allowed to migrate." Reuses MIGRATABLE_AIWORKHUB_RUNTIME_PATH_RE directly
// rather than duplicating its (unsafe-to-drift) matching logic.
const CODEX_OWNED_RUNTIME_SEGMENT_RE = MIGRATABLE_AIWORKHUB_RUNTIME_PATH_RE;

// TOML table header, e.g. "[mcp_servers.aiworkhub.env]" or
// "[mcp_servers.AIWorkHub_Ultrafast]".
const TOML_SECTION_RE = /^\s*\[([^\]]+)\]\s*$/;

// Canonical AIWorkHub-owned MCP server names this extension is allowed to
// repair PYTHONPATH for. Case-insensitive to tolerate existing config
// casing conventions.
const OWNED_MCP_SERVER_NAMES = new Set(["aiworkhub", "aiworkhub_ultrafast"]);

function isOwnedMcpSection(sectionPath) {
  const segments = String(sectionPath || "").trim().split(".");
  if (segments.length < 2 || segments[0].toLowerCase() !== "mcp_servers") {
    return false;
  }
  return OWNED_MCP_SERVER_NAMES.has(segments[1].toLowerCase());
}

/** Pure text repair: replace every AIWorkHub-owned runtime path segment on a
 *  PYTHONPATH line with currentRuntimeDir, but only inside a canonical
 *  AIWorkHub-owned MCP section (`[mcp_servers.aiworkhub...]` or
 *  `[mcp_servers.aiworkhub_ultrafast...]`, case-insensitive). Leaves
 *  non-PYTHONPATH lines, non-AIWorkHub-owned entries, and PYTHONPATH lines
 *  in custom sections (even ones that happen to point at a
 *  shrec.aiworkhub-<version>/runtime path) untouched. Idempotent -- a path already
 *  equal to currentRuntimeDir is left as-is, so re-running never rewrites a
 *  file that is already current.
 */
function repairCodexConfigTomlText(text, currentRuntimeDir) {
  let changed = false;
  let currentSection = "";
  const nextLines = String(text || "").split("\n").map((line) => {
    const sectionMatch = line.match(TOML_SECTION_RE);
    if (sectionMatch) {
      currentSection = sectionMatch[1];
      return line;
    }
    // A Codex MCP entry is application-global, while repository authority is
    // chat/process-local. Persisting a repository path here makes the last
    // reloaded VS Code window steal every other window's AIWorkHub binding.
    // Remove only AIWorkHub-owned repository bindings; the MCP process will
    // resolve its repository from its own cwd, while the dashboard child is
    // still explicitly bound by McpStdioClient._start().
    if (
      isOwnedMcpSection(currentSection)
      && /^\s*AIWORKHUB_REPO(?:_ROOT)?\s*=/.test(line)
    ) {
      changed = true;
      return null;
    }
    if (!line.includes("PYTHONPATH") || !isOwnedMcpSection(currentSection)) {
      return line;
    }
    if (line.includes(currentRuntimeDir)) return line;
    const migratedLine = line.replace(AIWORKHUB_RUNTIME_PATH_RE, (match) => {
      if (match === currentRuntimeDir) {
        return match;
      }
      changed = true;
      return currentRuntimeDir;
    });
    if (migratedLine !== line) return migratedLine;
    const pythonPathAssignment = line.match(/^(\s*PYTHONPATH\s*=\s*)"(?:[^"\\]|\\.)*"(\s*(?:#.*)?)$/);
    if (pythonPathAssignment) {
      changed = true;
      return `${pythonPathAssignment[1]}"${currentRuntimeDir}"${pythonPathAssignment[2]}`;
    }
    return line;
  }).filter((line) => line !== null);
  return { text: nextLines.join("\n"), changed };
}

/** Ensure the canonical Codex manager MCP entry can perform the task
 * lifecycle it exposes. Repository binding remains process-cwd scoped; these
 * gates only enable already capability-checked manager writes/launches. An
 * explicit existing value is preserved, so an operator can still disable a
 * gate. Dashboard children remain read-only because _start() deletes both
 * variables from their private child environment. */
function ensureCodexManagerGatesTomlText(text) {
  const input = String(text || "");
  const lines = input.split("\n");
  const insertedSuffix = input.includes("\r\n") ? "\r" : "";
  const result = [];
  let currentSection = "";
  let ownedEnv = false;
  let hasWrites = false;
  let hasLaunch = false;
  let changed = false;

  function flushMissingGates() {
    if (!ownedEnv) return;
    if (!hasWrites) {
      result.push(`AIWORKHUB_ALLOW_WRITES = "1"${insertedSuffix}`);
      changed = true;
    }
    if (!hasLaunch) {
      result.push(`AIWORKHUB_ALLOW_LAUNCH = "1"${insertedSuffix}`);
      changed = true;
    }
  }

  for (const line of lines) {
    const sectionMatch = line.match(TOML_SECTION_RE);
    if (sectionMatch) {
      flushMissingGates();
      currentSection = sectionMatch[1];
      const segments = currentSection.trim().split(".");
      ownedEnv = segments.length === 3
        && segments[0].toLowerCase() === "mcp_servers"
        && OWNED_MCP_SERVER_NAMES.has(segments[1].toLowerCase())
        && segments[2].toLowerCase() === "env";
      hasWrites = false;
      hasLaunch = false;
      result.push(line);
      continue;
    }
    if (ownedEnv) {
      if (/^\s*AIWORKHUB_ALLOW_WRITES\s*=/.test(line)) hasWrites = true;
      if (/^\s*AIWORKHUB_ALLOW_LAUNCH\s*=/.test(line)) hasLaunch = true;
    }
    result.push(line);
  }
  flushMissingGates();
  return { text: result.join("\n"), changed };
}

function tomlQuoted(value) {
  return JSON.stringify(String(value));
}

function hasAiWorkHubMcpRegistrationToml(text) {
  let currentSection = "";
  for (const line of String(text || "").split("\n")) {
    const sectionMatch = line.match(TOML_SECTION_RE);
    if (sectionMatch) {
      currentSection = sectionMatch[1];
      continue;
    }
    const segments = currentSection.trim().split(".");
    if (segments.length !== 2 || segments[0].toLowerCase() !== "mcp_servers") continue;
    if (OWNED_MCP_SERVER_NAMES.has(segments[1].toLowerCase())) return true;
    if (/^\s*args\s*=/.test(line) && line.includes("aiworkhub.server")) return true;
  }
  return false;
}

/** Add the canonical application-global Codex MCP registration on a clean
 * install. It deliberately carries no repository path: each Codex chat binds
 * the server from its own cwd, preserving multi-window/repository isolation. */
function ensureCodexMcpRegistrationTomlText(text, runtimeDir, python) {
  const input = String(text || "");
  if (hasAiWorkHubMcpRegistrationToml(input)) return { text: input, changed: false };
  const prefix = input && !input.endsWith("\n") ? "\n\n" : input ? "\n" : "";
  const args = [...(Array.isArray(python.argsPrefix) ? python.argsPrefix : []), "-m", "aiworkhub.server"];
  const block = [
    "[mcp_servers.aiworkhub]",
    `command = ${tomlQuoted(python.command)}`,
    `args = [${args.map(tomlQuoted).join(", ")}]`,
    "",
    "[mcp_servers.aiworkhub.env]",
    `PYTHONPATH = ${tomlQuoted(runtimeDir)}`,
    'AIWORKHUB_ALLOW_WRITES = "1"',
    'AIWORKHUB_ALLOW_LAUNCH = "1"',
    "",
  ].join("\n");
  return { text: `${input}${prefix}${block}`, changed: true };
}

/** Point every AIWorkHub-owned Codex MCP entry at one host-stable launcher.
 *
 * The launcher resolves the current immutable runtime generation at process
 * start.  Extension upgrades therefore update only current.json; they no
 * longer rewrite ~/.codex/config.toml while an existing Codex chat owns an
 * MCP transport.  That removes the Windows config-reload race which could
 * close stdio after a task mutation but before its receipt reached the chat.
 */
function ensureCodexStableMcpRegistrationTomlText(text, launcherPath, python) {
  const input = String(text || "");
  const launcherArgs = [
    ...(Array.isArray(python.argsPrefix) ? python.argsPrefix : []),
    launcherPath,
  ];
  const commandLine = `command = ${tomlQuoted(python.command)}`;
  const argsLine = `args = [${launcherArgs.map(tomlQuoted).join(", ")}]`;
  const lines = input.split("\n");
  const output = [];
  const topSections = new Set();
  const envSections = new Set();
  let currentSection = "";
  let changed = false;

  for (const line of lines) {
    const sectionMatch = line.match(TOML_SECTION_RE);
    if (sectionMatch) currentSection = sectionMatch[1];
    const segments = currentSection.trim().split(".");
    const owned = segments.length >= 2
      && segments[0].toLowerCase() === "mcp_servers"
      && OWNED_MCP_SERVER_NAMES.has(segments[1].toLowerCase());
    const topLevel = owned && segments.length === 2;
    const envLevel = owned && segments.length === 3 && segments[2].toLowerCase() === "env";
    if (topLevel) topSections.add(segments[1]);
    if (envLevel) envSections.add(segments[1].toLowerCase());
    const suffix = line.endsWith("\r") ? "\r" : "";
    if (topLevel && /^\s*command\s*=/.test(line)) {
      const replacement = `${commandLine}${suffix}`;
      output.push(replacement);
      if (replacement !== line) changed = true;
      continue;
    }
    if (topLevel && /^\s*args\s*=/.test(line)) {
      const replacement = `${argsLine}${suffix}`;
      output.push(replacement);
      if (replacement !== line) changed = true;
      continue;
    }
    if (
      envLevel
      && /^\s*(?:PYTHONPATH|AIWORKHUB_REPO(?:_ROOT|_ID)?)\s*=/.test(line)
    ) {
      changed = true;
      continue;
    }
    output.push(line);
  }

  if (topSections.size === 0) {
    const prefix = input && !input.endsWith("\n") ? "\n\n" : input ? "\n" : "";
    output.length = 0;
    output.push(`${input}${prefix}[mcp_servers.aiworkhub]`);
    output.push(commandLine);
    output.push(argsLine);
    output.push("");
    output.push("[mcp_servers.aiworkhub.env]");
    output.push('AIWORKHUB_ALLOW_WRITES = "1"');
    output.push('AIWORKHUB_ALLOW_LAUNCH = "1"');
    output.push("");
    changed = true;
  } else {
    for (const serverName of topSections) {
      if (envSections.has(serverName.toLowerCase())) continue;
      output.push("");
      output.push(`[mcp_servers.${serverName}.env]`);
      output.push('AIWORKHUB_ALLOW_WRITES = "1"');
      output.push('AIWORKHUB_ALLOW_LAUNCH = "1"');
      changed = true;
    }
  }
  const withGates = ensureCodexManagerGatesTomlText(output.join("\n"));
  return { text: withGates.text, changed: changed || withGates.changed };
}

function materializeStableMcpLauncher(context) {
  const storageRoot = context.globalStorageUri && context.globalStorageUri.fsPath
    ? context.globalStorageUri.fsPath
    : path.join(
      os.tmpdir(),
      "aiworkhub-extension-fallback",
      crypto.createHash("sha256").update(String(context.extensionUri.fsPath)).digest("hex").slice(0, 16),
    );
  const binDir = path.join(storageRoot, "bin");
  const launcher = path.join(binDir, "aiworkhub-mcp-server.py");
  const script = `#!/usr/bin/env python3
import json
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
current = json.loads((root / "runtime" / "current.json").read_text(encoding="utf-8"))
runtime = Path(current["runtime_dir"])
server = runtime / "aiworkhub" / "server.py"
if not server.is_file():
    raise SystemExit("AIWorkHub stable MCP runtime is missing")
sys.path.insert(0, str(runtime))
from aiworkhub.server import main
raise SystemExit(main())
`;
  fs.mkdirSync(binDir, { recursive: true });
  let existing = "";
  try {
    existing = fs.readFileSync(launcher, "utf8");
  } catch (_err) {
    existing = "";
  }
  if (existing !== script) {
    fs.writeFileSync(launcher, script, { encoding: "utf8", mode: 0o755 });
  }
  if (process.platform !== "win32") fs.chmodSync(launcher, 0o755);
  return launcher;
}

function ensureCodexStableMcpRegistered(context) {
  const configPath = resolveCodexConfigTomlPath(process.env);
  let original = "";
  try {
    original = fs.readFileSync(configPath, "utf8");
  } catch (err) {
    if (err && err.code !== "ENOENT") {
      outputChannel.appendLine(`[codex] failed to read config.toml: ${sanitizeErrorMessage(err)}`);
      return false;
    }
  }
  const launcher = materializeStableMcpLauncher(context);
  const python = findPythonCommand(os.homedir(), { preflight: false });
  const result = ensureCodexStableMcpRegistrationTomlText(original, launcher, python);
  if (!result.changed || result.text === original) return false;
  try {
    fs.mkdirSync(path.dirname(configPath), { recursive: true });
    // Exactly one write per activation migration. Subsequent versions update
    // only the launcher's current.json pointer and leave this live transport
    // configuration byte-identical.
    atomicWriteText(configPath, result.text);
    outputChannel.appendLine("[codex] migrated AIWorkHub MCP to the host-stable runtime launcher");
    return true;
  } catch (err) {
    outputChannel.appendLine(`[codex] failed to install stable AIWorkHub MCP launcher: ${sanitizeErrorMessage(err)}`);
    return false;
  }
}

function ensureCodexMcpRegistered(context, _repoRoot) {
  const configPath = resolveCodexConfigTomlPath(process.env);
  let original = "";
  try {
    original = fs.readFileSync(configPath, "utf8");
  } catch (err) {
    if (err && err.code !== "ENOENT") {
      outputChannel.appendLine(`[codex] failed to read config.toml: ${sanitizeErrorMessage(err)}`);
      return false;
    }
  }
  const runtimeDir = extensionRuntimeDir || resolveExtensionRuntimeDir(context.extensionUri.fsPath);
  // Codex registration is application-global, so it must never point at one
  // repository's disposable virtualenv. Resolve only a stable user/system
  // interpreter; the packaged runtime itself comes from PYTHONPATH.
  const python = findPythonCommand(os.homedir(), { preflight: false });
  const result = ensureCodexMcpRegistrationTomlText(original, runtimeDir, python);
  if (!result.changed) return false;
  try {
    fs.mkdirSync(path.dirname(configPath), { recursive: true });
    fs.writeFileSync(configPath, result.text, "utf8");
    outputChannel.appendLine("[codex] registered the packaged AIWorkHub MCP runtime for new chats");
    return true;
  } catch (err) {
    outputChannel.appendLine(`[codex] failed to register AIWorkHub MCP: ${sanitizeErrorMessage(err)}`);
    return false;
  }
}

/** B894a backward-compatible pure helper: like repairCodexConfigTomlText, but
 *  also migrates a custom-named MCP server table when (and only when) that
 *  table's own `args` line (in its top-level `[mcp_servers.<name>]` section,
 *  never a subsection) declares both `-m` and `aiworkhub.server`, and its
 *  current PYTHONPATH already points at a versioned
 *  `<publisher>.aiworkhub-<version>/runtime` path
 *  (MIGRATABLE_AIWORKHUB_RUNTIME_PATH_RE, any safe VS Code extension
 *  publisher prefix, not just `shrec`). A
 *  custom table without that args signature, or whose PYTHONPATH is some
 *  other non-versioned/unrelated path, is left byte-for-byte untouched.
 *  Preserves every byte outside the migrated PYTHONPATH values, including
 *  original line endings (splitting/joining on "\n" alone keeps a trailing
 *  "\r" attached to its own line). Returns {content, changed, migrated}
 *  where migrated is the sorted list of server names actually rewritten.
 */
function migrateCodexConfigTomlText(text, currentRuntimeDir) {
  const lines = String(text || "").split("\n");

  function serverNameForSection(sectionPath) {
    const segments = String(sectionPath || "").trim().split(".");
    if (segments.length < 2 || segments[0].toLowerCase() !== "mcp_servers") {
      return null;
    }
    return segments[1];
  }

  // Pass 1: find custom (non-canonical) server names whose OWN top-level
  // table (not a subsection like `.env`) declares an aiworkhub.server args
  // invocation.
  const customAiworkhubServerNames = new Set();
  {
    let currentSection = "";
    for (const line of lines) {
      const sectionMatch = line.match(TOML_SECTION_RE);
      if (sectionMatch) {
        currentSection = sectionMatch[1];
        continue;
      }
      const serverName = serverNameForSection(currentSection);
      if (!serverName) {
        continue;
      }
      const segments = currentSection.trim().split(".");
      const isTopLevelServerSection = segments.length === 2;
      if (
        isTopLevelServerSection
        && !OWNED_MCP_SERVER_NAMES.has(serverName.toLowerCase())
        && /\bargs\b/.test(line)
        && line.includes("-m")
        && line.includes("aiworkhub.server")
      ) {
        customAiworkhubServerNames.add(serverName.toLowerCase());
      }
    }
  }

  // Pass 2: rewrite PYTHONPATH lines within a canonical OR permitted custom
  // server's env subsection.
  let changed = false;
  const migrated = new Set();
  let currentSection = "";
  const nextLines = lines.map((line) => {
    const sectionMatch = line.match(TOML_SECTION_RE);
    if (sectionMatch) {
      currentSection = sectionMatch[1];
      return line;
    }
    const serverName = serverNameForSection(currentSection);
    if (!serverName || !line.includes("PYTHONPATH")) {
      return line;
    }
    const lowerName = serverName.toLowerCase();
    const permitted = OWNED_MCP_SERVER_NAMES.has(lowerName) || customAiworkhubServerNames.has(lowerName);
    if (!permitted) {
      return line;
    }
    return line.replace(MIGRATABLE_AIWORKHUB_RUNTIME_PATH_RE, (match) => {
      if (match === currentRuntimeDir) {
        return match;
      }
      changed = true;
      migrated.add(serverName);
      return currentRuntimeDir;
    });
  });

  return { content: nextLines.join("\n"), changed, migrated: Array.from(migrated).sort() };
}

// B894a backward-compatible pure helper: splits a PYTHONPATH-style value on
// the platform-appropriate delimiter without ever splitting a Windows drive
// letter's own colon (e.g. "C:\foo\runtime" must stay one entry, not become
// ["C", "\foo\runtime"]). A value containing ";" is treated as a
// Windows-style list and split on ";"; otherwise it is treated as a POSIX
// colon-delimited list, with any lone single-letter segment produced by that
// split re-joined onto the following segment (that lone letter is a drive
// letter, never a genuine POSIX path entry).
function splitCodexPythonPathValue(value) {
  const str = String(value || "");
  if (!str) {
    return [];
  }
  if (str.includes(";")) {
    return str.split(";").filter(Boolean);
  }
  const rawParts = str.split(":");
  const parts = [];
  for (let i = 0; i < rawParts.length; i += 1) {
    const part = rawParts[i];
    if (/^[A-Za-z]$/.test(part) && i + 1 < rawParts.length) {
      parts.push(`${part}:${rawParts[i + 1]}`);
      i += 1;
    } else {
      parts.push(part);
    }
  }
  return parts.filter(Boolean);
}

/** Activation-time repair of ~/.codex/config.toml: when an AIWorkHub-owned
 *  MCP entry's PYTHONPATH still points at an older versioned
 *  shrec.aiworkhub-{version}/runtime directory (e.g. after an extension
 *  upgrade changed the install directory name), rewrite only that runtime path
 *  segment to point at the currently installed extension's runtime/
 *  directory. Never touches a custom/non-AIWorkHub MCP entry, and never
 *  creates or otherwise mutates the file beyond that one substitution.
 *  Best-effort: a missing/unreadable/unwritable config.toml is left alone.
 */
function ensureCodexConfigTomlRepaired(context) {
  const configPath = codexConfigTomlPath();
  let original;
  try {
    original = fs.readFileSync(configPath, "utf8");
  } catch (_err) {
    return false;
  }
  const currentRuntimeDir = extensionRuntimeDir || resolveExtensionRuntimeDir(context.extensionUri.fsPath);
  const { text, changed } = repairCodexConfigTomlText(original, currentRuntimeDir);
  if (!changed) {
    return false;
  }
  try {
    fs.writeFileSync(configPath, text, "utf8");
    outputChannel.appendLine("[codex] repaired stale AIWorkHub runtime PYTHONPATH entry in ~/.codex/config.toml");
    return true;
  } catch (err) {
    outputChannel.appendLine(`[codex] failed to repair ~/.codex/config.toml: ${sanitizeErrorMessage(err)}`);
    return false;
  }
}

/** 0.6.20 compatibility: activation-time counterpart to
 *  ensureCodexConfigTomlRepaired that additionally covers the broader,
 *  custom-server-name migration case handled by migrateCodexConfigTomlText
 *  (any safe publisher prefix, not just the strict shrec-only repair path).
 *  Resolves the config.toml location via resolveCodexConfigTomlPath (honors
 *  CODEX_HOME) rather than the hardcoded homedir join, reuses
 *  migrateCodexConfigTomlText for the actual text transform, and is
 *  best-effort: a missing/unreadable/unwritable config.toml is left alone.
 */
function migrateCodexConfigTomlRuntimePath(context) {
  const configPath = resolveCodexConfigTomlPath(process.env);
  let original;
  try {
    original = fs.readFileSync(configPath, "utf8");
  } catch (_err) {
    return false;
  }
  const currentRuntimeDir = extensionRuntimeDir || resolveExtensionRuntimeDir(context.extensionUri.fsPath);
  const { content, changed } = migrateCodexConfigTomlText(original, currentRuntimeDir);
  if (!changed) {
    return false;
  }
  try {
    fs.writeFileSync(configPath, content, "utf8");
    outputChannel.appendLine("[codex] migrated AIWorkHub runtime PYTHONPATH entry in config.toml");
    return true;
  } catch (err) {
    outputChannel.appendLine(`[codex] failed to migrate config.toml: ${sanitizeErrorMessage(err)}`);
    return false;
  }
}

function ensureCodexManagerGatesRepaired() {
  const configPath = resolveCodexConfigTomlPath(process.env);
  let original;
  try {
    original = fs.readFileSync(configPath, "utf8");
  } catch (_err) {
    return false;
  }
  const { text, changed } = ensureCodexManagerGatesTomlText(original);
  if (!changed) return false;
  try {
    fs.writeFileSync(configPath, text, "utf8");
    outputChannel.appendLine("[codex] enabled capability-gated AIWorkHub manager task lifecycle");
    return true;
  } catch (err) {
    outputChannel.appendLine(`[codex] failed to repair manager gates in config.toml: ${sanitizeErrorMessage(err)}`);
    return false;
  }
}

/** Materialize the same-host App Server mux launcher at a stable host-local
 * path. It is advertised only after OpenAI's extension is proven co-located;
 * the setting is excluded from Settings Sync and repaired independently on
 * every installed host.
 */
function materializeStableMuxLauncher(context) {
  if (!context.globalStorageUri || !context.globalStorageUri.fsPath) {
    const fallbackName = process.platform === "win32" ? "aiworkhub-app-server-mux.cmd" : "aiworkhub-app-server-mux";
    return path.join(context.extensionUri.fsPath, "bin", fallbackName);
  }
  const binDir = path.join(context.globalStorageUri.fsPath, "bin");
  const pythonLauncher = path.join(binDir, "aiworkhub-app-server-mux.py");
  const posixLauncher = path.join(binDir, "aiworkhub-app-server-mux");
  const windowsLauncher = path.join(binDir, "aiworkhub-app-server-mux.cmd");
  const script = `#!/usr/bin/env python3
import json
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
current = json.loads((root / "runtime" / "current.json").read_text(encoding="utf-8"))
runtime = Path(current["runtime_dir"])
if not (runtime / "aiworkhub" / "app_server_mux.py").is_file():
    raise SystemExit("AIWorkHub stable mux runtime is missing")
sys.path.insert(0, str(runtime))
from aiworkhub.app_server_mux import main
raise SystemExit(main())
`;
  const cmd = `@echo off\r\nwhere py >nul 2>nul\r\nif %errorlevel% equ 0 (\r\n  py -3 "%~dp0aiworkhub-app-server-mux.py" %*\r\n) else (\r\n  python "%~dp0aiworkhub-app-server-mux.py" %*\r\n)\r\n`;
  fs.mkdirSync(binDir, { recursive: true });
  fs.writeFileSync(pythonLauncher, script, { encoding: "utf8", mode: 0o755 });
  fs.writeFileSync(posixLauncher, script, { encoding: "utf8", mode: 0o755 });
  fs.writeFileSync(windowsLauncher, cmd, "utf8");
  if (process.platform !== "win32") {
    fs.chmodSync(pythonLauncher, 0o755);
    fs.chmodSync(posixLauncher, 0o755);
  }
  return process.platform === "win32" ? windowsLauncher : posixLauncher;
}

/** Install an optional same-host command shim without configuring Codex.
 *
 * A workspace extension runs remotely under Remote-SSH, while Codex may run
 * in the local UI extension host. The shim is materialized only after the
 * co-location gate succeeds. Windows advertises its exact native path because
 * OpenAI can start before this extension mutates PATH; Settings Sync excludes
 * that host-local application setting. POSIX keeps command discovery local to
 * this extension host.
 */
function materializePathMuxShim(stableLauncher, options = {}) {
  const platform = options.platform || process.platform;
  const home = options.home || os.homedir();
  const env = options.env || process.env;
  const arch = options.arch || process.arch;
  let binDir;
  if (platform === "win32") {
    // Keep executable materialization in extension-owned global storage.
    // WindowsApps is an OS-managed execution-alias directory and may reject
    // writes under Store policy, ACL hardening, or endpoint protection.
    binDir = path.dirname(stableLauncher);
  } else {
    binDir = path.join(home, ".local", "bin");
  }
  fs.mkdirSync(binDir, { recursive: true });
  let shim = path.join(binDir, platform === "win32" ? "aiworkhub-app-server-mux.exe" : "aiworkhub-app-server-mux");
  if (platform === "win32") {
    const nativeArch = arch === "arm64" ? "aarch64" : arch === "x64" ? "x86_64" : arch;
    const nativeLauncher = options.windowsNativeLauncher || (
      options.extensionFsPath
        ? path.join(options.extensionFsPath, "bin", `windows-${nativeArch}`, "aiworkhub-app-server-mux.exe")
        : ""
    );
    if (nativeLauncher && fs.existsSync(nativeLauncher)) {
      fs.copyFileSync(nativeLauncher, shim);
      const pythonTarget = path.join(path.dirname(stableLauncher), "aiworkhub-app-server-mux.py");
      fs.writeFileSync(`${shim}.target`, `${pythonTarget}\r\n`, "utf8");
    } else {
      // Development checkout fallback only. Packaged VSIX qualification
      // requires the native .exe for OpenAI's shell:false spawn path.
      shim = path.join(binDir, "aiworkhub-app-server-mux.cmd");
      const escaped = stableLauncher.replace(/"/g, '""');
      fs.writeFileSync(shim, `@echo off\r\n"${escaped}" %*\r\n`, "utf8");
    }
  } else {
    const escaped = stableLauncher.replace(/'/g, `'\\''`);
    fs.writeFileSync(shim, `#!/bin/sh\nexec '${escaped}' "$@"\n`, { encoding: "utf8", mode: 0o755 });
    fs.chmodSync(shim, 0o755);
  }
  // Keep POSIX command discovery local to this extension host. Windows uses
  // the returned absolute native path after the same-host co-location gate,
  // because OpenAI may launch before this PATH mutation runs.
  const pathEntries = String(env.PATH || "").split(path.delimiter).filter(Boolean);
  const normalizedBin = path.resolve(binDir);
  if (!pathEntries.some((entry) => {
    try {
      return path.resolve(entry) === normalizedBin;
    } catch (_err) {
      return false;
    }
  })) {
    env.PATH = [binDir, ...pathEntries].join(path.delimiter);
  }
  return shim;
}

function isAiWorkHubMuxExecutable(value) {
  const current = String(value || "").trim();
  if (!current) return false;
  return /(?:^|[\\/])aiworkhub-app-server-mux(?:\.cmd|\.exe)?$/i.test(current)
    || /^aiworkhub-app-server-mux(?:\.cmd|\.exe)?$/i.test(current);
}

function isLegacyAiWorkHubMuxPath(value) {
  const current = String(value || "").trim();
  return isAiWorkHubMuxExecutable(current)
    && (path.isAbsolute(current) || /[\\/]/.test(current));
}

function codexMuxLauncherSetting(platform, pathLauncher) {
  return platform === "win32" ? pathLauncher : "aiworkhub-app-server-mux";
}

function colocatedCodexExtension() {
  try {
    if (!vscode.extensions || typeof vscode.extensions.getExtension !== "function") return null;
    return vscode.extensions.getExtension("openai.chatgpt") || null;
  } catch (_err) {
    return null;
  }
}

function resolveBundledCodexExecutable(codexExtension) {
  const extensionRoot = codexExtension && (
    codexExtension.extensionPath
    || (codexExtension.extensionUri && codexExtension.extensionUri.fsPath)
  );
  if (!extensionRoot) return "";
  const arch = process.arch === "arm64" ? "aarch64" : process.arch === "x64" ? "x86_64" : process.arch;
  const platformAliases = process.platform === "win32"
    ? ["windows", "win32"]
    : process.platform === "darwin" ? ["darwin", "macos"] : [process.platform];
  const executableName = process.platform === "win32" ? "codex.exe" : "codex";
  const candidates = [];
  for (const platformName of platformAliases) {
    candidates.push(path.join(extensionRoot, "bin", `${platformName}-${arch}`, executableName));
  }
  candidates.push(path.join(extensionRoot, "bin", executableName));
  return candidates.find((candidate) => {
    try {
      fs.accessSync(candidate, fs.constants.X_OK);
      return fs.statSync(candidate).isFile();
    } catch (_err) {
      return false;
    }
  }) || "";
}

function pinMuxRealExecutable(executable, options = {}) {
  if (!executable || !path.isAbsolute(executable)) return false;
  const sidebandDir = options.sidebandDir || appServerMuxSidebandDir();
  fs.mkdirSync(sidebandDir, { recursive: true, mode: 0o700 });
  const pinPath = path.join(sidebandDir, "real_executable");
  fs.writeFileSync(pinPath, `${executable}\n`, { encoding: "utf8", mode: 0o600 });
  if (process.platform !== "win32") {
    fs.chmodSync(sidebandDir, 0o700);
    fs.chmodSync(pinPath, 0o600);
  }
  return true;
}

async function keepMuxSettingHostLocal() {
  try {
    const syncConfig = vscode.workspace.getConfiguration("settingsSync");
    if (!syncConfig || typeof syncConfig.get !== "function" || typeof syncConfig.update !== "function") return false;
    const current = syncConfig.get("ignoredSettings", []);
    const values = Array.isArray(current) ? current.filter((value) => typeof value === "string") : [];
    if (values.includes("chatgpt.cliExecutable")) return false;
    await syncConfig.update(
      "ignoredSettings",
      [...values, "chatgpt.cliExecutable"],
      vscode.ConfigurationTarget ? vscode.ConfigurationTarget.Global : true,
    );
    return true;
  } catch (_err) {
    // Settings Sync may be absent or policy-managed. The co-location gate and
    // activation-time repair still prevent a foreign absolute path from being
    // trusted on this host.
    return false;
  }
}

function aiworkhubFeatureEnabled(key) {
  return vscode.workspace.getConfiguration(EXT_ID).get(key, false) === true;
}

async function restoreNativeCodexExecutable() {
  try {
    const config = vscode.workspace.getConfiguration("chatgpt");
    const current = String(config.get("cliExecutable", "") || "").trim();
    if (!isAiWorkHubMuxExecutable(current) || typeof config.update !== "function") {
      return false;
    }
    await config.update(
      "cliExecutable",
      undefined,
      vscode.ConfigurationTarget ? vscode.ConfigurationTarget.Global : true,
    );
    if (outputChannel) {
      outputChannel.appendLine("[codex] restored native Codex executable; callback mux is disabled");
    }
    return true;
  } catch (err) {
    if (outputChannel) {
      outputChannel.appendLine(`[codex] failed to restore native Codex executable: ${sanitizeErrorMessage(err)}`);
    }
    return false;
  }
}

/** Configure direct callbacks only on the host that owns both extensions.
 *
 * `chatgpt.cliExecutable` has application scope, so the stored launcher is
 * ignored by Settings Sync and recomputed from this host's stable global
 * storage on every activation. Split local/Remote-SSH topologies fail open to
 * native Codex + manager inbox; unrelated user overrides remain untouched.
 */
async function ensureCodexCallbackMuxConfigured(context, options = {}) {
  try {
    const config = vscode.workspace.getConfiguration("chatgpt");
    const current = String(config.get("cliExecutable", "") || "").trim();
    if (current && !isAiWorkHubMuxExecutable(current)) {
      if (outputChannel) outputChannel.appendLine("[codex] callback mux not applied: custom chatgpt.cliExecutable is preserved");
      return { ok: false, changed: false, reason: "custom_cli_executable_preserved" };
    }
    const codexExtension = colocatedCodexExtension();
    const realExecutable = resolveBundledCodexExecutable(codexExtension);
    if (!codexExtension || !realExecutable) {
      if (!isLegacyAiWorkHubMuxPath(current)) {
        return { ok: true, changed: false, launcher: "", mode: "native_codex", reason: "codex_extension_not_colocated" };
      }
      if (!config || typeof config.update !== "function") {
        return { ok: false, changed: false, reason: "configuration_update_unavailable" };
      }
      const globalTarget = vscode.ConfigurationTarget
        ? vscode.ConfigurationTarget.Global
        : true;
      await config.update("cliExecutable", undefined, globalTarget);
      if (outputChannel) {
        outputChannel.appendLine(
          "[codex] removed legacy host-specific AIWorkHub executable override; restored native Codex",
        );
      }
      return { ok: true, changed: true, launcher: "", mode: "native_codex", reason: "codex_extension_not_colocated" };
    }
    const launcher = materializeStableMuxLauncher(context);
    const pathLauncher = materializePathMuxShim(launcher, {
      extensionFsPath: context && context.extensionUri && context.extensionUri.fsPath,
      ...(options.muxShim || {}),
    });
    // The OpenAI extension starts before this workspace extension on Windows,
    // so a bare command that only becomes discoverable after we prepend PATH
    // loses the activation-order race and Codex silently stays native. Persist
    // the extension-owned native launcher path on Windows; Settings Sync is
    // explicitly told to ignore this host-local application setting. Keep the
    // existing machine-neutral command on POSIX hosts.
    const launcherSetting = codexMuxLauncherSetting(process.platform, pathLauncher);
    pinMuxRealExecutable(realExecutable, { sidebandDir: options.sidebandDir });
    await keepMuxSettingHostLocal();
    const activationMarker = `app_server_sideband.v2:${process.platform}:${launcherSetting}`;
    const markerKey = "aiworkhub.codexCallbackMuxActivation";
    const previousMarker = context && context.globalState && typeof context.globalState.get === "function"
      ? String(context.globalState.get(markerKey, "") || "")
      : activationMarker;
    if (current === launcherSetting) {
      // A stale Remote Machine value can equal the correct host-local path
      // while OpenAI's application-scoped configuration never observed a
      // change event and its already-running child remains native. Pulse the
      // value exactly once per host/launcher identity; persistent globalState
      // prevents a reload/restart loop.
      if (previousMarker !== activationMarker && config && typeof config.update === "function") {
        const globalTarget = vscode.ConfigurationTarget ? vscode.ConfigurationTarget.Global : true;
        await config.update("cliExecutable", undefined, globalTarget);
        await config.update("cliExecutable", launcherSetting, globalTarget);
        if (context.globalState && typeof context.globalState.update === "function") {
          await context.globalState.update(markerKey, activationMarker);
        }
        if (outputChannel) {
          outputChannel.appendLine("[codex] refreshed host-local callback mux activation after stale native child state");
        }
        return { ok: true, changed: true, launcher: launcherSetting, mode: "app_server_sideband", activation_refreshed: true };
      }
      return { ok: true, changed: false, launcher: launcherSetting, mode: "app_server_sideband" };
    }
    if (!config || typeof config.update !== "function") {
      return { ok: false, changed: false, reason: "configuration_update_unavailable" };
    }
    await config.update(
      "cliExecutable",
      launcherSetting,
      vscode.ConfigurationTarget ? vscode.ConfigurationTarget.Global : true,
    );
    if (context && context.globalState && typeof context.globalState.update === "function") {
      await context.globalState.update(markerKey, activationMarker);
    }
    if (outputChannel) {
      outputChannel.appendLine("[codex] enabled host-local AIWorkHub App Server callback mux; one Codex host restart may be required");
    }
    return { ok: true, changed: true, launcher: launcherSetting, mode: "app_server_sideband", restart_required: true };
  } catch (err) {
    if (outputChannel) outputChannel.appendLine(`[codex] callback mux configuration failed: ${sanitizeErrorMessage(err)}`);
    return { ok: false, changed: false, reason: "configuration_update_failed" };
  }
}

/** Repair a repository-local VS Code MCP registration created by an older
 *  AIWorkHub release. The dashboard child and Copilot's MCP child are separate
 *  processes: the latter reads `.vscode/mcp.json`, so a stale repository
 *  source checkout in PYTHONPATH can fail even while the dashboard is live.
 *  Keep the registration repository-scoped, but make code authority come from
 *  this extension's bundled runtime on every supported host OS. */
function repairMcpConfigObject(document, containerKey, runtimeDir, repoRoot, python) {
  if (!document || typeof document !== "object" || Array.isArray(document)) {
    return { document, changed: false };
  }
  if (!document[containerKey] || typeof document[containerKey] !== "object" || Array.isArray(document[containerKey])) {
    document[containerKey] = {};
  }
  const servers = document[containerKey];
  let changed = false;
  let found = false;
  for (const [name, value] of Object.entries(servers)) {
    if (!value || typeof value !== "object") continue;
    const args = Array.isArray(value.args) ? value.args.map(String) : [];
    const isAiWorkHub = name.toLowerCase() === "aiworkhub" || args.includes("aiworkhub.server");
    if (!isAiWorkHub) continue;
    found = true;
    const nextArgs = [...(Array.isArray(python.argsPrefix) ? python.argsPrefix : []), "-m", "aiworkhub.server"];
    const nextEnv = {
      ...(value.env && typeof value.env === "object" ? value.env : {}),
      PYTHONPATH: runtimeDir,
      AIWORKHUB_REPO: repoRoot,
      AIWORKHUB_REPO_ROOT: repoRoot,
    };
    if (!Object.prototype.hasOwnProperty.call(nextEnv, "AIWORKHUB_ALLOW_WRITES")) {
      nextEnv.AIWORKHUB_ALLOW_WRITES = "1";
    }
    if (!Object.prototype.hasOwnProperty.call(nextEnv, "AIWORKHUB_ALLOW_LAUNCH")) {
      nextEnv.AIWORKHUB_ALLOW_LAUNCH = "1";
    }
    const next = { ...value, command: python.command, args: nextArgs, env: nextEnv, type: "stdio" };
    if (JSON.stringify(next) !== JSON.stringify(value)) {
      servers[name] = next;
      changed = true;
    }
  }
  if (!found) {
    servers.AIWorkHub = {
      command: python.command,
      args: [...(Array.isArray(python.argsPrefix) ? python.argsPrefix : []), "-m", "aiworkhub.server"],
      env: {
        PYTHONPATH: runtimeDir,
        AIWORKHUB_REPO: repoRoot,
        AIWORKHUB_REPO_ROOT: repoRoot,
        AIWORKHUB_ALLOW_WRITES: "1",
        AIWORKHUB_ALLOW_LAUNCH: "1",
      },
      type: "stdio",
    };
    changed = true;
  }
  return { document, changed };
}

function repairWorkspaceMcpConfigObject(document, runtimeDir, repoRoot, python) {
  return repairMcpConfigObject(document, "servers", runtimeDir, repoRoot, python);
}

function repairClaudeMcpConfigObject(document, runtimeDir, repoRoot, python) {
  return repairMcpConfigObject(document, "mcpServers", runtimeDir, repoRoot, python);
}

function ensureWorkspaceMcpConfigsRepaired(context) {
  const folders = vscode.workspace.workspaceFolders || [];
  const runtimeDir = extensionRuntimeDir || resolveExtensionRuntimeDir(context.extensionUri.fsPath);
  let repaired = 0;
  for (const folder of folders) {
    const repoRoot = canonicalRepositoryRoot(folder.uri.fsPath);
    const python = findPythonCommand(repoRoot, { preflight: false });
    for (const spec of [
      { configPath: path.join(repoRoot, ".vscode", "mcp.json"), container: "servers", label: "VS Code/Copilot" },
      { configPath: path.join(repoRoot, ".mcp.json"), container: "mcpServers", label: "Claude Code" },
    ]) {
      let document = {};
      try {
        document = JSON.parse(fs.readFileSync(spec.configPath, "utf8"));
      } catch (err) {
        if (err && err.code !== "ENOENT") {
          outputChannel.appendLine(`[mcp] ${spec.label} config is invalid/unreadable; preserved without overwrite`);
          continue;
        }
      }
      const result = repairMcpConfigObject(document, spec.container, runtimeDir, repoRoot, python);
      if (!result.changed) continue;
      try {
        fs.mkdirSync(path.dirname(spec.configPath), { recursive: true });
        fs.writeFileSync(spec.configPath, `${JSON.stringify(result.document, null, 2)}\n`, "utf8");
        repaired += 1;
      } catch (err) {
        outputChannel.appendLine(`[mcp] failed to register ${spec.label} MCP: ${sanitizeErrorMessage(err)}`);
      }
    }
  }
  if (repaired) {
    outputChannel.appendLine(`[mcp] repaired ${repaired} repository-local AIWorkHub MCP registration(s) to bundled runtime`);
  }
  return repaired;
}

function installedExtensionVersion() {
  return String((extensionContext && extensionContext.extension && extensionContext.extension.packageJSON && extensionContext.extension.packageJSON.version) || "0.3.0");
}

function getMcpClient(context) {
  const repo = getActiveRepositoryRoot(context || extensionContext);
  const root = repo.root;
  const displayLabel = repositoryLabel(vscode.workspace.workspaceFolders || [], repo.uriStr);
  const identity = { ...repo, label: displayLabel };
  if (!mcpClient || mcpClient.repositoryRoot !== root || mcpClient.repositoryIdentity.repoId !== identity.repoId) {
    let startupBarrier = null;
    if (mcpClient) {
      // getMcpClient() remains synchronous, but the replacement client's first
      // start is gated by this bounded shutdown. This prevents two Windows
      // Python process trees from opening the same repository during init.
      startupBarrier = mcpClient.stopDispatcherThenTerminate({
        restart: false,
        timeoutMs: 3000,
      });
    }
    activeClaimEpisode = `episode_${crypto.randomBytes(12).toString("hex")}`;
    activeRepoIdentity = identity;
    activeRepoLabel = displayLabel;
    // Manifest identity is enough to persist routing ownership. The Python
    // child remains the sole authority for DB/storage readiness, but waiting
    // for a dashboard snapshot here would leave callbacks unbound during
    // startup -- exactly when the dispatcher needs this window identity.
    if (REPO_ID_RE.test(identity.repoId)) {
      try {
        refreshCoordinatorRouteOwnership(identity);
      } catch (err) {
        if (outputChannel) {
          outputChannel.appendLine(`[routing] initial ownership publish deferred: ${sanitizeErrorMessage(err)}`);
        }
      }
    }
    mcpClient = new McpStdioClient(root, outputChannel, identity, activeClaimEpisode);
    mcpClient.startupBarrier = startupBarrier;
  } else {
    activeRepoIdentity = identity;
    activeRepoLabel = displayLabel;
  }
  return mcpClient;
}

/** Push the active repository label into a Webview without exposing the
 *  host-absolute path. Called on initial connect and after every repo switch.
 */
function pushRepositoryInfo(view, repoInfo) {
  const info = repoInfo || activeRepoIdentity || {};
  view.postMessage({
    type: OUTBOUND_TYPES.repositoryInfo,
    label: info.label || activeRepoLabel,
    repoId: info.repoId || "unavailable",
    repoName: info.repoName || info.label || activeRepoLabel,
    storageReady: Boolean(info.storageReady),
    storageStatus: info.storageStatus || "unknown",
    windowId: WINDOW_SCOPE_ID,
    claimEpisode: activeClaimEpisode,
    extensionVersion: installedExtensionVersion(),
    expectedMcpVersion: EXPECTED_MCP_PACKAGE_VERSION,
  });
}

function pushCoordinatorTargets(view) {
  try {
    const repoInfo = activeRepoIdentity || getActiveRepositoryRoot(extensionContext);
    view.postMessage({ type: OUTBOUND_TYPES.coordinatorTargets, payload: sanitizeWebviewPayload(readCoordinatorTargets(repoInfo)) });
  } catch (err) {
    view.postMessage({ type: OUTBOUND_TYPES.error, message: sanitizeErrorMessage(err) });
  }
}

// B893: `reloadRequired` is retained in the payload shape for backward
// compatibility but is NEVER set true anymore -- this extension must never
// instruct a manual window/extension-host reload. A mismatch that cannot be
// repaired within the bounded budget is reported as `degraded` with a
// readable `reason` instead.
function runtimeStatusPayload({ runtimeVersion, degraded, repaired, repairAttempted, reason, attempts, maxAttempts }) {
  return {
    extensionVersion: installedExtensionVersion(),
    expectedMcpVersion: EXPECTED_MCP_PACKAGE_VERSION,
    runtimeVersion: runtimeVersion || "unavailable",
    reloadRequired: false,
    degraded: Boolean(degraded),
    repaired: Boolean(repaired),
    repairAttempted: Boolean(repairAttempted),
    reason: reason || "ok",
    attempts: Number(attempts || 0),
    maxAttempts: Number(maxAttempts || MCP_MAX_RUNTIME_REPAIR_ATTEMPTS),
  };
}

// One bounded tools/list + health round trip against `client`. Never mutates
// client state; callers decide what to do with the result.
async function checkRuntimeHealth(client) {
  // Activation intentionally starts MCP without blocking the extension host.
  // `initialized` flips true immediately after the MCP initialize exchange,
  // while the version/capability preflight is still completing under the
  // shared startup promise. Always join that promise first: otherwise the
  // dashboard can issue a second tools/list + health pair in the narrow gap,
  // race background service convergence, and restart a perfectly healthy
  // child as a false mismatch.
  await client.ensureStarted();
  // _handshake() establishes this exact evidence before ensureStarted()
  // resolves. It is valid only for the currently-owned live child and is
  // cleared on every stop/exit/restart. Reusing it prevents runtimeInfo from
  // racing the deliberately background-only dispatcher/indexer convergence
  // calls and opening a false mismatch recovery circuit on a healthy child.
  if (client.running && client.initialized && client.runtimePreflight) {
    return {
      matches: Boolean(client.runtimePreflight.matches),
      missing: [...client.runtimePreflight.missing],
      runtimeVersion: client.runtimePreflight.runtimeVersion,
    };
  }
  const tools = await client.listTools();
  const names = new Set(tools.map((tool) => String((tool && tool.name) || "")));
  const missing = EXPECTED_DASHBOARD_TOOL_NAMES.filter((name) => !names.has(name));
  let health = null;
  try {
    health = await client.callTool(DASHBOARD_TOOLS.health, {});
  } catch (_err) {
    health = null;
  }
  const runtimeVersion = String((health && (health.server_version || health.version || health.package_version)) || "unavailable");
  return { matches: missing.length === 0 && runtimeVersion === EXPECTED_MCP_PACKAGE_VERSION, missing, runtimeVersion };
}

// B893: the reloadless runtime-repair path. A detected version/capability
// mismatch is fixed by restarting ONLY this window's own repo-bound MCP
// child (client.attemptRuntimeRepair) -- bounded, never a different
// repository, never a manual reload instruction -- and, on success, the
// already-open dashboard reconnects on its own via an immediate pushSnapshot
// (no user action required). A repair that fails or exhausts its bounded
// budget surfaces a readable `degraded`/`reason` pair instead of silently
// attaching another repository or looping forever.
async function pushRuntimeInfo(view) {
  let client;
  try {
    client = getMcpClient();
  } catch (err) {
    view.postMessage({
      type: OUTBOUND_TYPES.runtimeInfo,
      payload: runtimeStatusPayload({ degraded: true, reason: sanitizeErrorMessage(err) }),
    });
    return;
  }

  let status;
  try {
    status = await checkRuntimeHealth(client);
  } catch (err) {
    status = { matches: false, missing: [], runtimeVersion: "unavailable" };
  }

  if (status.matches) {
    client.runtimeRepairAttempts = 0;
    client.runtimeRepairBlockedReason = "";
    view.postMessage({
      type: OUTBOUND_TYPES.runtimeInfo,
      payload: runtimeStatusPayload({ runtimeVersion: status.runtimeVersion }),
    });
    return;
  }

  const mismatchReason = status.missing.length ? "mcp_capability_mismatch" : "mcp_version_mismatch";
  const repair = await client.attemptRuntimeRepair(mismatchReason);
  if (!repair.repaired) {
    view.postMessage({
      type: OUTBOUND_TYPES.runtimeInfo,
      payload: runtimeStatusPayload({
        runtimeVersion: status.runtimeVersion,
        degraded: true,
        repairAttempted: repair.attempted,
        reason: repair.attempted ? `runtime_repair_failed: ${repair.reason}` : repair.reason,
      }),
    });
    return;
  }

  let recheck;
  try {
    recheck = await checkRuntimeHealth(client);
  } catch (err) {
    recheck = { matches: false, missing: [], runtimeVersion: "unavailable" };
  }
  if (!recheck.matches) {
    view.postMessage({
      type: OUTBOUND_TYPES.runtimeInfo,
      payload: runtimeStatusPayload({
        runtimeVersion: recheck.runtimeVersion,
        degraded: true,
        repairAttempted: true,
        reason: recheck.missing.length ? "mcp_capability_mismatch_after_repair" : "mcp_version_mismatch_after_repair",
      }),
    });
    return;
  }

  client.runtimeRepairAttempts = 0;
  client.runtimeRepairBlockedReason = "";
  view.postMessage({
    type: OUTBOUND_TYPES.runtimeInfo,
    payload: runtimeStatusPayload({ runtimeVersion: recheck.runtimeVersion, repaired: true, repairAttempted: true }),
  });
  // The dashboard tab reconnects on its own -- no manual retry/refresh/
  // reload needed after a successful bounded repair. pushSnapshot never
  // rejects (its own errors surface as an "offline" message), so this is
  // safe to await directly.
  await pushSnapshot(view);
}

function readConfiguredRefreshIntervalMs() {
  const configured = Number(
    vscode.workspace.getConfiguration(EXT_ID).get("refreshIntervalMs", DEFAULT_REFRESH_INTERVAL_MS)
  );
  return ALLOWED_REFRESH_INTERVALS_MS.has(configured) ? configured : DEFAULT_REFRESH_INTERVAL_MS;
}

function sanitizeErrorMessage(err) {
  // Every message reaching here is one of this module's own literal Error
  // strings (mcp_not_running / mcp_request_timeout / no_workspace_folder /
  // mcp_child_exited ...) -- never raw child stdout/stderr, never an
  // environment value or filesystem path.
  return String((err && err.message) || "mcp_unavailable").slice(0, 200);
}

function runBackgroundTask(label, operation) {
  return Promise.resolve().then(operation).catch((err) => {
    if (outputChannel) {
      outputChannel.appendLine(`[extension] ${label} failed: ${sanitizeErrorMessage(err)}`);
    }
  });
}

// Host-owned per-webview poll state. Polling lives here (not in the
// Webview's own timers) so visibility -- a host-side fact -- is what starts
// and stops it; a hidden panel's timer is always cleared, never left
// running in a backgrounded script context.
class ViewState {
  constructor(postMessage) {
    this.postMessage = postMessage;
    this.autoRefresh = true;
    this.refreshIntervalMs = readConfiguredRefreshIntervalMs();
    this.timer = null;
    this.visible = true;
    this.repoUriStr = "";
    this.repoId = "";
    this.claimEpisode = "";
    this.snapshotRequestSeq = 0;
    // Coalesce refresh ticks while one repository snapshot is in flight.
    // A slow first snapshot must still be allowed to render; previously each
    // timer tick advanced snapshotRequestSeq, making every eventual response
    // look stale and leaving the Webview on "Connecting" forever.
    this.snapshotInFlight = null;
    this.snapshotRefreshQueued = false;
  }

  bindClient(client) {
    this.repoUriStr = client.repositoryIdentity.uriStr;
    this.repoId = client.repositoryIdentity.repoId;
    this.claimEpisode = client.claimEpisode;
  }

  stillBoundTo(client) {
    return (
      client &&
      this.repoUriStr === client.repositoryIdentity.uriStr &&
      this.repoId === client.repositoryIdentity.repoId &&
      this.claimEpisode === client.claimEpisode
    );
  }

  reschedule() {
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = null;
    }
    if (!this.visible || !this.autoRefresh) {
      return;
    }
    this.timer = setInterval(() => {
      pushSnapshot(this).catch(() => {});
    }, this.refreshIntervalMs);
  }

  setVisible(visible) {
    this.visible = visible;
    this.reschedule();
  }

  dispose() {
    if (this.timer) {
      clearInterval(this.timer);
    }
    this.timer = null;
  }
}

function pushSnapshot(view) {
  if (view.snapshotInFlight) {
    view.snapshotRefreshQueued = true;
    return view.snapshotInFlight;
  }
  view.snapshotRefreshQueued = false;
  const inFlight = pushSnapshotOnce(view).finally(() => {
    if (view.snapshotInFlight === inFlight) {
      view.snapshotInFlight = null;
    }
    if (view.snapshotRefreshQueued && view.visible) {
      view.snapshotRefreshQueued = false;
      return pushSnapshot(view);
    }
  });
  view.snapshotInFlight = inFlight;
  return inFlight;
}

function refreshCoordinatorRouteBeforeSnapshot() {
  if (!activeRepoIdentity) return;
  try {
    refreshCoordinatorRouteOwnership(activeRepoIdentity);
  } catch (err) {
    // Routing publication is callback metadata, not dashboard read authority.
    // Windows can transiently reject renameSync while another process has the
    // JSON open; the repo-bound MCP snapshot must still reach the Webview.
    if (outputChannel) {
      outputChannel.appendLine(
        `[routing] snapshot route refresh deferred: ${sanitizeErrorMessage(err)}`,
      );
    }
  }
}

async function pushSnapshotOnce(view) {
  const requestSeq = ++view.snapshotRequestSeq;
  let lastError = null;
  debugTrace("snapshot.begin", { request_seq: requestSeq });
  const client = getMcpClient();
  view.bindClient(client);
  const snapshotRecovery = {
    category: "snapshot",
    reason: "",
    attempts: 0,
    maxAttempts: MCP_SNAPSHOT_RECOVERY_ATTEMPTS,
    open: false,
  };
  for (let attempt = 0; attempt < MCP_SNAPSHOT_RECOVERY_ATTEMPTS; attempt += 1) {
    if (attempt > 0) {
      if (client.recovery.open) break;
      await new Promise((resolve) => setTimeout(resolve, MCP_RECOVERY_BACKOFF_MS[attempt - 1]));
    }
    try {
      debugTrace("snapshot.attempt.begin", { request_seq: requestSeq, attempt: attempt + 1 });
      refreshCoordinatorRouteBeforeSnapshot();
      const payload = await client.callTool(DASHBOARD_TOOLS.snapshot, { full: true });
      debugTrace("snapshot.payload.received", {
        request_seq: requestSeq,
        attempt: attempt + 1,
        payload_present: Boolean(payload),
      });
      if (payload && view.stillBoundTo(client) && requestSeq === view.snapshotRequestSeq) {
        view.postMessage({
          type: OUTBOUND_TYPES.snapshot,
          payload: sanitizeWebviewPayload({
            ...payload,
            system_logs: systemLogSnapshot(),
            extension_runtime_storage: runtimeRetentionSnapshot(),
          }),
        });
        // The storage state is now visible. Only now may slower background
        // services use this serialized MCP transport.
        client._convergeBackgroundServices();
        debugTrace("snapshot.posted", { request_seq: requestSeq, attempt: attempt + 1 });
      } else if (!payload && requestSeq === view.snapshotRequestSeq) {
        view.postMessage({ type: OUTBOUND_TYPES.error, message: "snapshot_unavailable" });
      }
      return;
    } catch (err) {
      debugTrace("snapshot.attempt.error", {
        request_seq: requestSeq,
        attempt: attempt + 1,
        ...debugErrorFields(err),
      });
      lastError = err;
      // A dashboard read timing out does not prove that the shared MCP child
      // or any model route is corrupt.  Keep snapshot degradation local to
      // the Webview; only child/runtime lifecycle failures may open the global
      // MCP recovery circuit.
      snapshotRecovery.reason = sanitizeErrorMessage(err);
      snapshotRecovery.attempts = attempt + 1;
      snapshotRecovery.open = attempt + 1 >= MCP_SNAPSHOT_RECOVERY_ATTEMPTS;
    }
  }
  if (requestSeq === view.snapshotRequestSeq) {
    view.postMessage({
      type: OUTBOUND_TYPES.offline,
      reason: sanitizeErrorMessage(lastError),
      recovery: snapshotRecovery,
    });
  }
  debugTrace("snapshot.offline", { request_seq: requestSeq, ...debugErrorFields(lastError) });
}

async function pushSnapshotNoRetry(view, options = {}) {
  const requestSeq = ++view.snapshotRequestSeq;
  const authoritative = options.authoritative === true;
  const convergeBackgroundServices = options.convergeBackgroundServices !== false;
  try {
    const client = options.client || getMcpClient();
    view.bindClient(client);
    refreshCoordinatorRouteBeforeSnapshot();
    const payload = await client.callTool(DASHBOARD_TOOLS.snapshot, { full: true });
    if (payload && view.stillBoundTo(client) && (authoritative || requestSeq === view.snapshotRequestSeq)) {
      view.postMessage({
        type: OUTBOUND_TYPES.snapshot,
        payload: sanitizeWebviewPayload({
          ...payload,
          system_logs: systemLogSnapshot(),
          extension_runtime_storage: runtimeRetentionSnapshot(),
        }),
      });
      if (convergeBackgroundServices) client._convergeBackgroundServices();
      return { posted: true, payload };
    } else if (!payload && (authoritative || requestSeq === view.snapshotRequestSeq)) {
      view.postMessage({ type: OUTBOUND_TYPES.error, message: "snapshot_unavailable" });
    }
  } catch (err) {
    if (authoritative || requestSeq === view.snapshotRequestSeq) {
      view.postMessage({ type: OUTBOUND_TYPES.offline, reason: sanitizeErrorMessage(err) });
    }
  }
  return { posted: false, payload: null };
}

async function pushTaskDetail(view, taskId) {
  try {
    const client = getMcpClient();
    view.bindClient(client);
    const payload = await client.callTool(DASHBOARD_TOOLS.taskDetail, { task_id: taskId });
    if (view.stillBoundTo(client)) {
      view.postMessage({ type: OUTBOUND_TYPES.taskDetail, payload: sanitizeWebviewPayload(payload) });
    }
  } catch (err) {
    view.postMessage({ type: OUTBOUND_TYPES.error, message: sanitizeErrorMessage(err) });
  }
}

// Bounded, single-task Live Output read. Calls the ONE read-only MCP tool
// aiworkhub_dashboard_task_live_output for exactly the selected taskId --
// never a dashboard-wide fan-out across other tasks. cursor is forwarded
// unchanged so a repeat call only requests bytes new since the last one; the
// server (aiworkhub.process_launcher.read_live_output_for_task) already
// strips ANSI/control sequences, redacts long token-like runs, and
// HTML-escapes before this ever reaches the Webview. The Webview-side
// renderer for this message (media/app.js: timelineEventFromObject /
// claudeStreamContentBlockDelta) formats each structured provider event --
// including recognizing a Claude CLI stream_event/content_block_delta whose
// delta.type is "signature_delta" as internal protocol metadata that is
// never shown -- this function and its message-contract entries
// (ALLOWED_INBOUND_MESSAGE_TYPES, OUTBOUND_TYPES.liveOutput,
// DASHBOARD_TOOLS.liveOutput) are only the host-side half of the wiring.
async function pushLiveOutput(view, taskId, cursor) {
  try {
    const client = getMcpClient();
    view.bindClient(client);
    const payload = await client.callTool(DASHBOARD_TOOLS.liveOutput, {
      task_id: taskId,
      cursor: Number.isFinite(cursor) && cursor >= 0 ? cursor : 0,
    });
    if (view.stillBoundTo(client)) {
      view.postMessage({ type: OUTBOUND_TYPES.liveOutput, payload: sanitizeWebviewPayload(payload) });
    }
  } catch (err) {
    view.postMessage({ type: OUTBOUND_TYPES.error, message: sanitizeErrorMessage(err) });
  }
}

async function pushMemory(view) {
  try {
    const client = getMcpClient();
    view.bindClient(client);
    const payload = await client.callTool(DASHBOARD_TOOLS.memory, { limit: 200 });
    if (view.stillBoundTo(client)) {
      view.postMessage({ type: OUTBOUND_TYPES.memory, payload: sanitizeWebviewPayload(payload) });
    }
  } catch (err) {
    view.postMessage({ type: OUTBOUND_TYPES.error, message: sanitizeErrorMessage(err) });
  }
}

async function pushSessions(view) {
  try {
    const client = getMcpClient();
    view.bindClient(client);
    const payload = await client.callTool(DASHBOARD_TOOLS.sessions, { limit: 200 });
    if (view.stillBoundTo(client)) {
      view.postMessage({ type: OUTBOUND_TYPES.sessions, payload: sanitizeWebviewPayload(payload) });
    }
  } catch (err) {
    view.postMessage({ type: OUTBOUND_TYPES.error, message: sanitizeErrorMessage(err) });
  }
}

async function pushKb(view) {
  try {
    const client = getMcpClient();
    view.bindClient(client);
    const payload = await client.callTool(DASHBOARD_TOOLS.kb, { limit: 200 });
    if (view.stillBoundTo(client)) {
      view.postMessage({ type: OUTBOUND_TYPES.kb, payload: sanitizeWebviewPayload(payload) });
    }
  } catch (err) {
    view.postMessage({ type: OUTBOUND_TYPES.error, message: sanitizeErrorMessage(err) });
  }
}

async function pushSettings(view) {
  try {
    const client = getMcpClient();
    view.bindClient(client);
    const payload = await client.callTool(DASHBOARD_TOOLS.settings, {});
    if (view.stillBoundTo(client)) {
      view.postMessage({ type: OUTBOUND_TYPES.settings, payload: sanitizeWebviewPayload(payload) });
    }
  } catch (err) {
    view.postMessage({ type: OUTBOUND_TYPES.error, message: sanitizeErrorMessage(err) });
  }
}

async function updateFeatureSetting(view, feature, enabled, expectedRevision) {
  if (!FEATURE_SETTING_KEYS.has(feature) || typeof enabled !== "boolean" ||
      !Number.isSafeInteger(expectedRevision) || expectedRevision < 0) {
    view.postMessage({ type: OUTBOUND_TYPES.error, message: "invalid_feature_setting_update" });
    return;
  }
  try {
    const client = getMcpClient();
    view.bindClient(client);
    const payload = await client.callTool(SETTINGS_UPDATE_TOOL, {
      changes: { [feature]: enabled },
      expected_revision: expectedRevision,
    });
    if (!payload || payload.ok !== true) {
      view.postMessage({ type: OUTBOUND_TYPES.error, message: (payload && payload.error) || "feature_setting_update_failed" });
      await pushSettings(view);
      return;
    }
    if (view.stillBoundTo(client)) {
      await pushSettings(view);
      view.postMessage({ type: OUTBOUND_TYPES.notification, message: `${feature.replaceAll("_", " ")} ${enabled ? "enabled" : "disabled"}` });
      await pushSnapshot(view);
    }
  } catch (err) {
    view.postMessage({ type: OUTBOUND_TYPES.error, message: sanitizeErrorMessage(err) });
    await pushSettings(view);
  }
}

async function updateSourceGraphLanguage(view, language, enabled, expectedRevision) {
  if (!/^[a-z][a-z0-9_]{0,63}$/.test(language) || typeof enabled !== "boolean" ||
      !Number.isSafeInteger(expectedRevision) || expectedRevision < 0) {
    view.postMessage({ type: OUTBOUND_TYPES.error, message: "invalid_source_graph_language_update" });
    return;
  }
  try {
    const client = getMcpClient();
    view.bindClient(client);
    const payload = await client.callTool(SOURCE_GRAPH_SETTINGS_UPDATE_TOOL, {
      language_changes: { [language]: enabled },
      expected_revision: expectedRevision,
    });
    if (!payload || payload.ok !== true) {
      view.postMessage({ type: OUTBOUND_TYPES.error, message: (payload && payload.error) || "source_graph_language_update_failed" });
      await pushSettings(view);
      return;
    }
    if (view.stillBoundTo(client)) {
      await pushSettings(view);
      view.postMessage({ type: OUTBOUND_TYPES.notification, message: `${language.replaceAll("_", " ")} indexing ${enabled ? "enabled" : "disabled"}` });
      await pushSnapshot(view);
    }
  } catch (err) {
    view.postMessage({ type: OUTBOUND_TYPES.error, message: sanitizeErrorMessage(err) });
    await pushSettings(view);
  }
}

async function runStorageCleanup(view) {
  try {
    const client = getMcpClient();
    view.bindClient(client);
    const preview = await client.callTool(STORAGE_RETENTION_TOOLS.preview, {});
    if (!preview || preview.ok !== true) {
      view.postMessage({ type: OUTBOUND_TYPES.error, message: (preview && preview.error) || "storage_preview_failed" });
      return;
    }
    const count = Math.max(0, Number(preview.candidate_count || 0));
    if (!count) {
      view.postMessage({ type: OUTBOUND_TYPES.notification, message: "No policy-aged, clean and fully-pushed worktrees are eligible" });
      return;
    }
    const bytes = Math.max(0, Number(preview.candidate_bytes || 0));
    const choice = await vscode.window.showWarningMessage(
      `Move ${count} retained worktree(s) (${bytes} bytes) into the 7-day AIWorkHub quarantine? Unsaved, unpushed, active and foreign-repository worktrees are excluded.`,
      { modal: true },
      "Quarantine",
    );
    if (choice !== "Quarantine") return;
    const result = await client.callTool(STORAGE_RETENTION_TOOLS.quarantine, {
      preview_digest: String(preview.preview_digest || ""),
      confirm: true,
    });
    if (!result || result.ok !== true) {
      view.postMessage({ type: OUTBOUND_TYPES.error, message: (result && result.error) || "storage_quarantine_failed" });
      return;
    }
    view.postMessage({ type: OUTBOUND_TYPES.notification, message: `Quarantined ${Number(result.quarantined || 0)} worktree(s)` });
    await pushSnapshot(view);
  } catch (err) {
    view.postMessage({ type: OUTBOUND_TYPES.error, message: sanitizeErrorMessage(err) });
  }
}

async function runStorageRegistrationPrune(view) {
  try {
    const client = getMcpClient();
    view.bindClient(client);
    const preview = await client.callTool(STORAGE_RETENTION_TOOLS.preview, {});
    if (!preview || preview.ok !== true) {
      view.postMessage({ type: OUTBOUND_TYPES.error, message: (preview && preview.error) || "storage_preview_failed" });
      return;
    }
    const registrations = preview.registration_health && typeof preview.registration_health === "object"
      ? preview.registration_health
      : {};
    const count = Math.max(0, Number(registrations.stale_candidate_count || 0));
    if (!count) {
      view.postMessage({ type: OUTBOUND_TYPES.notification, message: "No stale AIWorkHub worktree registrations found" });
      return;
    }
    if (Number(registrations.foreign_stale_count || 0) > 0 || registrations.safe_to_prune !== true) {
      view.postMessage({ type: OUTBOUND_TYPES.error, message: "registration_prune_blocked_by_foreign_stale_worktree" });
      return;
    }
    const choice = await vscode.window.showWarningMessage(
      `Prune ${count} stale AIWorkHub Git worktree registration(s)? No checkout files are deleted. Foreign registrations are excluded and block the operation.`,
      { modal: true },
      "Prune Registrations",
    );
    if (choice !== "Prune Registrations") return;
    const result = await client.callTool(STORAGE_RETENTION_TOOLS.pruneRegistrations, {
      preview_digest: String(registrations.preview_digest || ""),
      confirm: true,
    });
    if (!result || result.ok !== true) {
      view.postMessage({ type: OUTBOUND_TYPES.error, message: (result && result.error) || "registration_prune_failed" });
      return;
    }
    view.postMessage({ type: OUTBOUND_TYPES.notification, message: `Pruned ${Number(result.pruned || 0)} stale registration(s)` });
    await pushSnapshot(view);
  } catch (err) {
    view.postMessage({ type: OUTBOUND_TYPES.error, message: sanitizeErrorMessage(err) });
  }
}

async function runStorageRestore(view, batchId) {
  if (!STORAGE_BATCH_ID_RE.test(batchId)) return;
  const choice = await vscode.window.showInformationMessage(
    `Restore AIWorkHub quarantine batch ${batchId}? Existing destinations are never overwritten.`,
    { modal: true },
    "Restore",
  );
  if (choice !== "Restore") return;
  try {
    const client = getMcpClient();
    view.bindClient(client);
    const result = await client.callTool(STORAGE_RETENTION_TOOLS.restore, { batch_id: batchId, confirm: true });
    if (!result || result.ok !== true) {
      view.postMessage({ type: OUTBOUND_TYPES.error, message: (result && result.error) || "storage_restore_failed" });
      return;
    }
    view.postMessage({ type: OUTBOUND_TYPES.notification, message: `Restored ${Number(result.restored || 0)} worktree(s)` });
    await pushSnapshot(view);
  } catch (err) {
    view.postMessage({ type: OUTBOUND_TYPES.error, message: sanitizeErrorMessage(err) });
  }
}

async function runStoragePurge(view, batchId) {
  if (!STORAGE_BATCH_ID_RE.test(batchId)) return;
  const choice = await vscode.window.showWarningMessage(
    `Permanently purge expired AIWorkHub quarantine batch ${batchId}? This cannot be undone.`,
    { modal: true },
    "Permanently Purge",
  );
  if (choice !== "Permanently Purge") return;
  try {
    const client = getMcpClient();
    view.bindClient(client);
    const result = await client.callTool(STORAGE_RETENTION_TOOLS.purge, { batch_id: batchId, confirm: true });
    if (!result || result.ok !== true) {
      view.postMessage({ type: OUTBOUND_TYPES.error, message: (result && result.error) || "storage_purge_failed" });
      return;
    }
    view.postMessage({ type: OUTBOUND_TYPES.notification, message: "Expired quarantine batch permanently purged" });
    await pushSnapshot(view);
  } catch (err) {
    view.postMessage({ type: OUTBOUND_TYPES.error, message: sanitizeErrorMessage(err) });
  }
}

async function runTerminalLogCleanup(view) {
  try {
    const client = getMcpClient();
    view.bindClient(client);
    const preview = await client.callTool(TERMINAL_LOG_RETENTION_TOOLS.preview, {});
    if (!preview || preview.ok !== true) {
      view.postMessage({ type: OUTBOUND_TYPES.error, message: (preview && preview.error) || "terminal_log_preview_failed" });
      return;
    }
    const count = Math.max(0, Number(preview.candidate_count || 0));
    if (!count) {
      view.postMessage({ type: OUTBOUND_TYPES.notification, message: "No policy-aged terminal logs are eligible" });
      return;
    }
    const bytes = Math.max(0, Number(preview.candidate_bytes || 0));
    const choice = await vscode.window.showWarningMessage(
      `Move ${count} policy-aged terminal run(s) (${bytes} bytes) into the 7-day AIWorkHub quarantine? The canonical process ledger and active/review work remain protected.`,
      { modal: true },
      "Quarantine Logs",
    );
    if (choice !== "Quarantine Logs") return;
    const result = await client.callTool(TERMINAL_LOG_RETENTION_TOOLS.quarantine, {
      preview_digest: String(preview.preview_digest || ""),
      confirm: true,
    });
    if (!result || result.ok !== true) {
      view.postMessage({ type: OUTBOUND_TYPES.error, message: (result && result.error) || "terminal_log_quarantine_failed" });
      return;
    }
    view.postMessage({ type: OUTBOUND_TYPES.notification, message: `Quarantined ${Number(result.quarantined || 0)} terminal log file(s)` });
    await pushSnapshot(view);
  } catch (err) {
    view.postMessage({ type: OUTBOUND_TYPES.error, message: sanitizeErrorMessage(err) });
  }
}

async function runTerminalLogRestore(view, batchId) {
  if (!STORAGE_BATCH_ID_RE.test(batchId)) return;
  const choice = await vscode.window.showInformationMessage(
    `Restore terminal-log batch ${batchId}? Existing files are never overwritten.`,
    { modal: true },
    "Restore Logs",
  );
  if (choice !== "Restore Logs") return;
  try {
    const client = getMcpClient();
    view.bindClient(client);
    const result = await client.callTool(TERMINAL_LOG_RETENTION_TOOLS.restore, { batch_id: batchId, confirm: true });
    if (!result || result.ok !== true) {
      view.postMessage({ type: OUTBOUND_TYPES.error, message: (result && result.error) || "terminal_log_restore_failed" });
      return;
    }
    view.postMessage({ type: OUTBOUND_TYPES.notification, message: `Restored ${Number(result.restored || 0)} terminal log file(s)` });
    await pushSnapshot(view);
  } catch (err) {
    view.postMessage({ type: OUTBOUND_TYPES.error, message: sanitizeErrorMessage(err) });
  }
}

async function runTerminalLogPurge(view, batchId) {
  if (!STORAGE_BATCH_ID_RE.test(batchId)) return;
  const choice = await vscode.window.showWarningMessage(
    `Permanently purge expired terminal-log batch ${batchId}? This cannot be undone.`,
    { modal: true },
    "Permanently Purge Logs",
  );
  if (choice !== "Permanently Purge Logs") return;
  try {
    const client = getMcpClient();
    view.bindClient(client);
    const result = await client.callTool(TERMINAL_LOG_RETENTION_TOOLS.purge, { batch_id: batchId, confirm: true });
    if (!result || result.ok !== true) {
      view.postMessage({ type: OUTBOUND_TYPES.error, message: (result && result.error) || "terminal_log_purge_failed" });
      return;
    }
    view.postMessage({ type: OUTBOUND_TYPES.notification, message: "Expired terminal-log batch permanently purged" });
    await pushSnapshot(view);
  } catch (err) {
    view.postMessage({ type: OUTBOUND_TYPES.error, message: sanitizeErrorMessage(err) });
  }
}

async function runTaskArchive(view, taskId) {
  if (!TASK_ID_RE.test(taskId)) return;
  const choice = await vscode.window.showWarningMessage(
    `Archive task ${taskId}? Its evidence remains available and it can be restored until a later retention cleanup is explicitly confirmed.`,
    { modal: true },
    "Archive Task",
  );
  if (choice !== "Archive Task") return;
  try {
    const client = getMcpClient();
    view.bindClient(client);
    const result = await client.callTool(TASK_RETENTION_TOOLS.archive, {
      task_id: taskId,
      reason: "user_archived_from_dashboard",
      confirm: true,
    });
    if (!result || result.ok !== true) {
      view.postMessage({ type: OUTBOUND_TYPES.error, message: (result && result.error) || "task_archive_failed" });
      return;
    }
    view.postMessage({ type: OUTBOUND_TYPES.notification, message: `Archived ${taskId}` });
    await pushSnapshot(view);
    await pushTaskDetail(view, taskId);
  } catch (err) {
    view.postMessage({ type: OUTBOUND_TYPES.error, message: sanitizeErrorMessage(err) });
  }
}

async function runTaskRestore(view, taskId) {
  if (!TASK_ID_RE.test(taskId)) return;
  const choice = await vscode.window.showInformationMessage(
    `Restore archived task ${taskId} to the live queue?`,
    { modal: true },
    "Restore Task",
  );
  if (choice !== "Restore Task") return;
  try {
    const client = getMcpClient();
    view.bindClient(client);
    const result = await client.callTool(TASK_RETENTION_TOOLS.restoreTask, {
      task_id: taskId,
      reason: "user_restored_from_dashboard",
      confirm: true,
    });
    if (!result || result.ok !== true) {
      view.postMessage({ type: OUTBOUND_TYPES.error, message: (result && result.error) || "task_restore_failed" });
      return;
    }
    view.postMessage({ type: OUTBOUND_TYPES.notification, message: `Restored ${taskId}` });
    await pushSnapshot(view);
    await pushTaskDetail(view, taskId);
  } catch (err) {
    view.postMessage({ type: OUTBOUND_TYPES.error, message: sanitizeErrorMessage(err) });
  }
}

async function runTaskCleanup(view) {
  const choices = [
    { label: "30 days", days: 30, description: "Archived at least one month" },
    { label: "90 days", days: 90, description: "Recommended default" },
    { label: "180 days", days: 180, description: "Archived at least six months" },
    { label: "365 days", days: 365, description: "Archived at least one year" },
  ];
  const selected = await vscode.window.showQuickPick(choices, {
    title: "Archived task retention",
    placeHolder: "Choose the minimum archive age",
  });
  if (!selected) return;
  try {
    const client = getMcpClient();
    view.bindClient(client);
    const preview = await client.callTool(TASK_RETENTION_TOOLS.preview, { older_than_days: selected.days });
    if (!preview || preview.ok !== true) {
      view.postMessage({ type: OUTBOUND_TYPES.error, message: (preview && preview.error) || "task_retention_preview_failed" });
      return;
    }
    const count = Math.max(0, Number(preview.candidate_count || 0));
    if (!count) {
      view.postMessage({ type: OUTBOUND_TYPES.notification, message: `No archived tasks older than ${selected.days} days are eligible` });
      return;
    }
    const choice = await vscode.window.showWarningMessage(
      `Move ${count} archived task(s) older than ${selected.days} days into seven-day undo quarantine? Active, Processing, Review and undelivered-callback tasks are protected.`,
      { modal: true },
      "Quarantine Tasks",
    );
    if (choice !== "Quarantine Tasks") return;
    const result = await client.callTool(TASK_RETENTION_TOOLS.quarantine, {
      preview_digest: String(preview.preview_digest || ""),
      older_than_days: selected.days,
      confirm: true,
    });
    if (!result || result.ok !== true) {
      view.postMessage({ type: OUTBOUND_TYPES.error, message: (result && result.error) || "task_retention_quarantine_failed" });
      return;
    }
    view.postMessage({ type: OUTBOUND_TYPES.notification, message: `Quarantined ${Number(result.quarantined || 0)} archived task(s)` });
    await pushSnapshot(view);
  } catch (err) {
    view.postMessage({ type: OUTBOUND_TYPES.error, message: sanitizeErrorMessage(err) });
  }
}

async function runTaskQuarantineRestore(view, batchId) {
  if (!STORAGE_BATCH_ID_RE.test(batchId)) return;
  const choice = await vscode.window.showInformationMessage(
    `Restore archived-task batch ${batchId}? Existing task IDs are never overwritten.`,
    { modal: true },
    "Restore Tasks",
  );
  if (choice !== "Restore Tasks") return;
  try {
    const client = getMcpClient();
    view.bindClient(client);
    const result = await client.callTool(TASK_RETENTION_TOOLS.restoreBatch, { batch_id: batchId, confirm: true });
    if (!result || result.ok !== true) {
      view.postMessage({ type: OUTBOUND_TYPES.error, message: (result && result.error) || "task_retention_restore_failed" });
      return;
    }
    view.postMessage({ type: OUTBOUND_TYPES.notification, message: `Restored ${Number(result.restored || 0)} archived task(s)` });
    await pushSnapshot(view);
  } catch (err) {
    view.postMessage({ type: OUTBOUND_TYPES.error, message: sanitizeErrorMessage(err) });
  }
}

async function runTaskQuarantinePurge(view, batchId) {
  if (!STORAGE_BATCH_ID_RE.test(batchId)) return;
  const choice = await vscode.window.showWarningMessage(
    `Permanently purge archived-task batch ${batchId}? This cannot be undone.`,
    { modal: true },
    "Permanently Purge Tasks",
  );
  if (choice !== "Permanently Purge Tasks") return;
  try {
    const client = getMcpClient();
    view.bindClient(client);
    const result = await client.callTool(TASK_RETENTION_TOOLS.purgeBatch, { batch_id: batchId, confirm: true });
    if (!result || result.ok !== true) {
      view.postMessage({ type: OUTBOUND_TYPES.error, message: (result && result.error) || "task_retention_purge_failed" });
      return;
    }
    view.postMessage({ type: OUTBOUND_TYPES.notification, message: "Archived-task batch permanently purged" });
    await pushSnapshot(view);
  } catch (err) {
    view.postMessage({ type: OUTBOUND_TYPES.error, message: sanitizeErrorMessage(err) });
  }
}

async function runRuntimeCleanup(view) {
  const preview = runtimeRetentionSnapshot({ force: true });
  if (!preview || preview.ok !== true) {
    view.postMessage({ type: OUTBOUND_TYPES.error, message: (preview && preview.error) || "runtime_retention_preview_failed" });
    return;
  }
  const count = Math.max(0, Number(preview.candidate_count || 0));
  if (!count) {
    view.postMessage({ type: OUTBOUND_TYPES.notification, message: "No lease-free obsolete runtime generations are eligible" });
    return;
  }
  const bytes = Math.max(0, Number(preview.candidate_bytes || 0));
  const choice = await vscode.window.showWarningMessage(
    `Move ${count} obsolete AIWorkHub runtime generation(s) (${bytes} bytes) into 7-day quarantine? The current generation, latest three generations and every live/unknown window lease remain protected.`,
    { modal: true },
    "Quarantine Runtimes",
  );
  if (choice !== "Quarantine Runtimes") return;
  try {
    const result = runtimeRetention.quarantine({
      storageRoot: stableRuntimeInfo.storageRoot,
      previewDigest: String(preview.preview_digest || ""),
      confirm: true,
    });
    runtimeRetentionCache = null;
    view.postMessage({ type: OUTBOUND_TYPES.notification, message: `Quarantined ${Number(result.quarantined || 0)} runtime generation(s)` });
    await pushSnapshot(view);
  } catch (err) {
    view.postMessage({ type: OUTBOUND_TYPES.error, message: sanitizeErrorMessage(err) });
  }
}

async function runRuntimeRestore(view, batchId) {
  if (!STORAGE_BATCH_ID_RE.test(batchId) || !stableRuntimeInfo || !stableRuntimeInfo.storageRoot) return;
  const choice = await vscode.window.showInformationMessage(
    `Restore runtime generation batch ${batchId}? Existing generations are never overwritten.`,
    { modal: true },
    "Restore Runtimes",
  );
  if (choice !== "Restore Runtimes") return;
  try {
    const result = runtimeRetention.restore({ storageRoot: stableRuntimeInfo.storageRoot, batchId, confirm: true });
    runtimeRetentionCache = null;
    view.postMessage({ type: OUTBOUND_TYPES.notification, message: `Restored ${Number(result.restored || 0)} runtime generation(s)` });
    await pushSnapshot(view);
  } catch (err) {
    view.postMessage({ type: OUTBOUND_TYPES.error, message: sanitizeErrorMessage(err) });
  }
}

async function runRuntimePurge(view, batchId) {
  if (!STORAGE_BATCH_ID_RE.test(batchId) || !stableRuntimeInfo || !stableRuntimeInfo.storageRoot) return;
  const choice = await vscode.window.showWarningMessage(
    `Permanently purge expired runtime generation batch ${batchId}? This cannot be undone.`,
    { modal: true },
    "Permanently Purge Runtimes",
  );
  if (choice !== "Permanently Purge Runtimes") return;
  try {
    const result = runtimeRetention.purge({ storageRoot: stableRuntimeInfo.storageRoot, batchId, confirm: true });
    runtimeRetentionCache = null;
    view.postMessage({ type: OUTBOUND_TYPES.notification, message: `Purged ${Number(result.bytes || 0)} bytes of expired runtime cache` });
    await pushSnapshot(view);
  } catch (err) {
    view.postMessage({ type: OUTBOUND_TYPES.error, message: sanitizeErrorMessage(err) });
  }
}

// The sole initialization trigger: one bounded MCP tool call, tied to the
// active repo_id/window/claim episode -- the window/claim-episode binding is
// implicit in the AIWORKHUB_WINDOW_ID/AIWORKHUB_CLAIM_EPISODE env vars the
// MCP child was already spawned with (see McpStdioClient._start); the
// repo_id argument is only ever a real "repo_<hex>" identity already known
// to this window, never a placeholder label, so a first-time init on an
// uninitialized repository is never refused. Never called from activation
// or repository selection -- only from this explicit user-clicked action.
async function pushInitializeStorage(view) {
  let payload = null;
  let initializeError = "";
  let initializationClient = null;
  try {
    initializationClient = getMcpClient();
    view.bindClient(initializationClient);
    const repoId = activeRepoIdentity && REAL_REPO_ID_RE.test(String(activeRepoIdentity.repoId || ""))
      ? activeRepoIdentity.repoId
      : "";
    payload = await initializationClient.callTool(INITIALIZE_TOOL, { repo_id: repoId });
    recordSystemLog(
      payload && payload.ok === true
        ? `[initialize] completed repo_id=${sanitizeErrorMessage(payload.repo_id || "unknown")}`
        : `[initialize] failed ${sanitizeErrorMessage(payload && (payload.message || payload.error) || "initialize_failed")}`,
    );
  } catch (err) {
    // Another window can win the first-initialize race after this window's
    // stale MCP child has already observed a missing manifest. Always
    // reconcile from disk below; the fresh snapshot is the authority for
    // whether initialization actually converged.
    initializeError = sanitizeErrorMessage(err);
    recordSystemLog(`[initialize] failed ${initializeError}`);
  }

  try {
    // The process that performed initialization is the only process guaranteed
    // to observe its writes without a Windows launcher/process-tree handoff.
    // Publish storage readiness from that process before changing repo_id and
    // spawning the replacement MCP child. Starting the replacement first can
    // overlap it with the old Source Graph/dispatcher shutdown on Windows and
    // strand the Webview on Connecting even though project.json already exists.
    const reconciliation = initializationClient
      ? await pushSnapshotNoRetry(view, {
        client: initializationClient,
        authoritative: true,
        convergeBackgroundServices: false,
      })
      : { posted: false, payload: null };
    const storageReady = Boolean(
      reconciliation.payload
      && reconciliation.payload.storage
      && reconciliation.payload.storage.ready === true,
    );
    recordSystemLog(
      `[initialize] pre-rebind snapshot posted=${reconciliation.posted ? "true" : "false"} storage_ready=${storageReady ? "true" : "false"}`,
    );
    // A fresh repository had no durable log target before project.json was
    // created. Flush now so the initialize and reconciliation outcomes survive
    // a later child-process failure or window reload.
    flushSystemLogs();

    if ((!payload || payload.ok !== true) && !storageReady) {
      view.postMessage({
        type: OUTBOUND_TYPES.error,
        message: (payload && (payload.message || payload.error)) || initializeError || "initialize_failed",
      });
      return;
    }

    const refreshedRepo = getActiveRepositoryRoot(extensionContext);
    activeRepoLabel = repositoryLabel(vscode.workspace.workspaceFolders || [], refreshedRepo.uriStr);
    activeRepoIdentity = { ...refreshedRepo, label: activeRepoLabel };
    refreshCoordinatorRouteOwnership(activeRepoIdentity);
    if (vscodeLmBridgeHost) {
      await vscodeLmBridgeHost.start(activeRepoIdentity);
    }
    // Only after the ready state is visible may the repository identity change
    // from manifest-missing to its real repo_id and create a replacement child.
    const initializedClient = getMcpClient();
    view.bindClient(initializedClient);
    pushRepositoryInfo(view, activeRepoIdentity);

    // Storage readiness is already rendered. Source Graph convergence on the
    // NEW child can now take as long as Windows process/SQLite cleanup needs
    // without holding the Webview on Connecting.
    const sourceGraphStart = await initializedClient.callTool(
      SOURCE_GRAPH_DAEMON_TOOLS.ensureStarted,
      {},
    );
    if (!sourceGraphStart || sourceGraphStart.ok !== true ||
        (sourceGraphStart.daemon_started !== true && sourceGraphStart.status !== "disabled")) {
      const reason = String((sourceGraphStart && (sourceGraphStart.reason || sourceGraphStart.error || sourceGraphStart.status)) || "source_graph_start_failed");
      outputChannel.appendLine(`[source-graph] post-init automatic index start failed: ${sanitizeErrorMessage(reason)}`);
      view.postMessage({ type: OUTBOUND_TYPES.error, message: `source_graph_start_failed:${sanitizeErrorMessage(reason)}` });
    }
  } catch (err) {
    view.postMessage({ type: OUTBOUND_TYPES.error, message: sanitizeErrorMessage(err) });
  }
}

// The ONLY inbound message handler: rejects anything outside the fixed
// enum, and bounds/validates every payload (task_id pattern, allowlisted
// refresh intervals) before it can influence an MCP tool call.
function handleInboundMessage(view, message) {
  if (!message || typeof message !== "object" || !ALLOWED_INBOUND_MESSAGE_TYPES.has(message.type)) {
    return;
  }
  switch (message.type) {
    case "ready":
      // The first snapshot owns transport priority. Settings and background
      // services must never hold a Windows Webview at "Connecting".
      recordSystemLog("[webview] ready received");
      runBackgroundTask("dashboard ready", async () => {
        await pushSnapshot(view);
        await pushSettings(view);
      });
      break;
    case "refresh":
      runBackgroundTask("dashboard refresh", () => pushSnapshot(view));
      break;
    case "retry": {
      const client = getMcpClient();
      client.beginExplicitRecovery();
      runBackgroundTask("dashboard retry", () => pushSnapshot(view));
      break;
    }
    case "selectTask": {
      const taskId = String(message.taskId || "");
      if (!TASK_ID_RE.test(taskId)) {
        view.postMessage({ type: OUTBOUND_TYPES.error, message: "invalid_task_id" });
        return;
      }
      pushTaskDetail(view, taskId);
      break;
    }
    case "setAutoRefresh":
      view.autoRefresh = Boolean(message.enabled);
      view.reschedule();
      break;
    case "setRefreshInterval": {
      const ms = Number(message.ms);
      if (!ALLOWED_REFRESH_INTERVALS_MS.has(ms)) {
        return;
      }
      view.refreshIntervalMs = ms;
      view.reschedule();
      break;
    }
    case "selectCoordinatorTarget": {
      const targets = setCoordinatorTarget(String(message.provider || ""));
      if (targets) {
        view.postMessage({ type: OUTBOUND_TYPES.coordinatorTargets, payload: sanitizeWebviewPayload(targets) });
      }
      break;
    }
    case "initializeStorage":
      pushInitializeStorage(view);
      break;
    case "requestLiveOutput": {
      const taskId = String(message.taskId || "");
      if (!TASK_ID_RE.test(taskId)) {
        view.postMessage({ type: OUTBOUND_TYPES.error, message: "invalid_task_id" });
        return;
      }
      const cursor = Number(message.cursor || 0);
      pushLiveOutput(view, taskId, Number.isFinite(cursor) && cursor >= 0 ? cursor : 0);
      break;
    }
    case "clearSystemLogs":
      clearSystemLogs();
      view.postMessage({ type: OUTBOUND_TYPES.systemLogs, payload: [] });
      break;
    case "copySystemLogs": {
      const text = systemLogEntries
        .slice()
        .reverse()
        .map((entry) => `${entry.timestamp} ${entry.level.toUpperCase()} [${entry.component}] ${entry.message}`)
        .join("\n");
      Promise.resolve(vscode.env.clipboard.writeText(text)).then(() => {
        view.postMessage({ type: OUTBOUND_TYPES.notification, message: "System log copied" });
      }).catch(() => {
        view.postMessage({ type: OUTBOUND_TYPES.error, message: "system_log_copy_failed" });
      });
      break;
    }
    case "requestMemory":
      pushMemory(view);
      break;
    case "requestSessions":
      pushSessions(view);
      break;
    case "requestKb":
      pushKb(view);
      break;
    case "requestSettings":
      pushSettings(view);
      break;
    case "updateFeatureSetting":
      updateFeatureSetting(
        view,
        String(message.feature || ""),
        message.enabled,
        Number(message.expectedRevision),
      );
      break;
    case "updateSourceGraphLanguage":
      updateSourceGraphLanguage(
        view,
        String(message.language || ""),
        message.enabled,
        Number(message.expectedRevision),
      );
      break;
    case "requestStorageCleanup":
      runStorageCleanup(view);
      break;
    case "requestStorageRegistrationPrune":
      runStorageRegistrationPrune(view);
      break;
    case "requestStorageRestore": {
      const batchId = String(message.batchId || "");
      if (STORAGE_BATCH_ID_RE.test(batchId)) runStorageRestore(view, batchId);
      break;
    }
    case "requestStoragePurge": {
      const batchId = String(message.batchId || "");
      if (STORAGE_BATCH_ID_RE.test(batchId)) runStoragePurge(view, batchId);
      break;
    }
    case "requestTerminalLogCleanup":
      runTerminalLogCleanup(view);
      break;
    case "requestTerminalLogRestore": {
      const batchId = String(message.batchId || "");
      if (STORAGE_BATCH_ID_RE.test(batchId)) runTerminalLogRestore(view, batchId);
      break;
    }
    case "requestTerminalLogPurge": {
      const batchId = String(message.batchId || "");
      if (STORAGE_BATCH_ID_RE.test(batchId)) runTerminalLogPurge(view, batchId);
      break;
    }
    case "requestTaskArchive": {
      const taskId = String(message.taskId || "");
      if (TASK_ID_RE.test(taskId)) runTaskArchive(view, taskId);
      break;
    }
    case "requestTaskRestore": {
      const taskId = String(message.taskId || "");
      if (TASK_ID_RE.test(taskId)) runTaskRestore(view, taskId);
      break;
    }
    case "requestTaskCleanup":
      runTaskCleanup(view);
      break;
    case "requestTaskQuarantineRestore": {
      const batchId = String(message.batchId || "");
      if (STORAGE_BATCH_ID_RE.test(batchId)) runTaskQuarantineRestore(view, batchId);
      break;
    }
    case "requestTaskQuarantinePurge": {
      const batchId = String(message.batchId || "");
      if (STORAGE_BATCH_ID_RE.test(batchId)) runTaskQuarantinePurge(view, batchId);
      break;
    }
    case "requestRuntimeCleanup":
      runRuntimeCleanup(view);
      break;
    case "requestRuntimeRestore": {
      const batchId = String(message.batchId || "");
      if (STORAGE_BATCH_ID_RE.test(batchId)) runRuntimeRestore(view, batchId);
      break;
    }
    case "requestRuntimePurge": {
      const batchId = String(message.batchId || "");
      if (STORAGE_BATCH_ID_RE.test(batchId)) runRuntimePurge(view, batchId);
      break;
    }
    default:
      break;
  }
}

function nonce() {
  return crypto.randomBytes(16).toString("hex");
}

function applyWebviewOptions(webview, extensionUri) {
  webview.options = {
    enableScripts: true,
    localResourceRoots: [vscode.Uri.joinPath(extensionUri, "media")],
  };
}

// Strict nonce CSP: only webview.cspSource + this exact nonce may run/style
// the page -- no remote origin, no inline handler, no eval, no embedded frame.
function getHtmlForWebview(webview, extensionUri) {
  const mediaUri = vscode.Uri.joinPath(extensionUri, "media");
  const scriptUri = webview.asWebviewUri(vscode.Uri.joinPath(mediaUri, "app.js"));
  const styleUri = webview.asWebviewUri(vscode.Uri.joinPath(mediaUri, "app.css"));
  const logoUri = webview.asWebviewUri(vscode.Uri.joinPath(mediaUri, "aiworkhub-icon.png"));
  const nonceValue = nonce();
  const csp = [
    "default-src 'none'",
    `style-src ${webview.cspSource}`,
    `script-src 'nonce-${nonceValue}'`,
    `img-src ${webview.cspSource} data:`,
    `font-src ${webview.cspSource}`,
    "frame-src 'none'",
    "connect-src 'none'",
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'none'",
  ].join("; ");

  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="${csp}">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="${styleUri}">
<title>AIWorkHub</title>
</head>
<body>
  <a class="skip-link" href="#task-table">Skip to task table</a>

  <header class="app-header">
    <div class="header-main">
      <div class="brand-block">
        <img class="brand-logo" src="${logoUri}" alt="" aria-hidden="true">
      <div>
        <h1>AIWorkHub</h1>
        <p class="repo-label" id="repo-label">Loading repository</p>
      </div>
      <span class="readonly-badge">Read only</span>
      <div class="release-metadata" aria-label="AIWorkHub release and runtime">
        <span id="extension-version">Extension 0.3.0</span>
        <span id="mcp-runtime-version">MCP runtime checking</span>
      </div>
      </div>

      <div class="header-actions">
      <div class="connection-state" id="connection-state" role="status" aria-live="polite">
        <span class="connection-dot" aria-hidden="true"></span>
        <span id="connection-label">Connecting</span>
      </div>
      <span class="last-sync" id="last-sync">Not synced</span>
      <label class="switch-control" title="Auto refresh">
        <input id="auto-refresh" type="checkbox" checked>
        <span class="switch-track" aria-hidden="true"></span>
        <span>Auto</span>
      </label>
      <select id="refresh-interval" class="compact-select" aria-label="Refresh interval">
        <option value="10000">10s</option>
        <option value="30000" selected>30s</option>
        <option value="60000">60s</option>
      </select>
      <button class="primary-button" id="refresh-button" type="button">Refresh</button>
      </div>
    </div>

    <div class="header-insights" aria-label="Repository storage and AI infrastructure status">
      <button class="header-insight-card header-storage" id="header-storage" type="button" title="Open detailed storage metrics">
        <span class="header-storage-label">Storage</span>
        <strong id="header-storage-managed">Calculating</strong>
        <span id="header-storage-free">Free —</span>
      </button>

      <button class="header-insight-card header-tool-use" id="header-source-graph" type="button" title="Open Source Graph tool-use telemetry">
        <span class="header-storage-label">Source Graph</span>
        <strong id="header-source-graph-rate">—</strong>
        <span id="header-source-graph-detail">No evidence</span>
      </button>

      <div class="header-insight-card" id="header-session-manager" title="Session Manager context recovered across latest task runs">
        <span class="header-storage-label">Session Manager</span>
        <strong id="header-session-manager-value">—</strong>
        <span class="header-insight-detail" id="header-session-manager-detail">No evidence</span>
      </div>

      <div class="header-insight-card" id="header-ai-memory" title="AI Memory evidence across latest task runs">
        <span class="header-storage-label">AI Memory</span>
        <strong id="header-ai-memory-value">—</strong>
        <span class="header-insight-detail" id="header-ai-memory-detail">No evidence</span>
      </div>

      <div class="header-insight-card" id="header-kb" title="Knowledge Base evidence across latest task runs">
        <span class="header-storage-label">KB</span>
        <strong id="header-kb-value">—</strong>
        <span class="header-insight-detail" id="header-kb-detail">No evidence</span>
      </div>

      <div class="header-insight-card" id="header-preflight" title="Unified repository, policy, Source Graph and provider preflight">
        <span class="header-storage-label">Preflight</span>
        <strong id="header-preflight-value">Checking</strong>
        <span class="header-insight-detail" id="header-preflight-detail">No evidence</span>
      </div>
    </div>
  </header>

  <main>
    <section class="summary-strip" aria-label="Queue summary">
      <div class="summary-item status-all">
        <span class="summary-label">Active</span>
        <strong id="metric-active">0</strong>
      </div>
      <div class="summary-item status-pending">
        <span class="summary-label">Pending</span>
        <strong id="metric-pending">0</strong>
      </div>
      <div class="summary-item status-processing">
        <span class="summary-label">Processing</span>
        <strong id="metric-processing">0</strong>
      </div>
      <div class="summary-item status-review">
        <span class="summary-label">Review</span>
        <strong id="metric-review">0</strong>
      </div>
      <div class="summary-item status-blocked">
        <span class="summary-label">Blocked</span>
        <strong id="metric-blocked">0</strong>
      </div>
      <div class="summary-item status-finished">
        <span class="summary-label">Finished</span>
        <strong id="metric-finished">0</strong>
      </div>
      <div class="summary-item status-stale">
        <span class="summary-label">Stale</span>
        <strong id="metric-stale">0</strong>
      </div>
      <div class="summary-item usage-total">
        <span class="summary-label">Usage</span>
        <strong id="metric-tokens">0</strong>
        <span class="summary-note" id="metric-cost">$0.00</span>
      </div>
    </section>

    <section class="source-alert" id="source-alert" aria-live="polite" hidden>
      <strong id="source-alert-title">Partial data</strong>
      <span id="source-alert-message"></span>
    </section>

    <section class="source-alert offline-alert" id="offline-alert" aria-live="polite" hidden>
      <strong>MCP connection unavailable</strong>
      <span id="offline-alert-message"></span>
      <button class="primary-button" id="offline-retry-button" type="button">Retry</button>
    </section>

    <section class="source-alert reload-alert" id="reload-alert" aria-live="polite" hidden>
      <strong>Runtime repair in progress</strong>
      <span id="reload-alert-message"></span>
    </section>

    <section class="source-alert uninitialized-alert" id="uninitialized-alert" aria-live="polite" hidden>
      <strong>AIWorkHub is not initialized for this repository</strong>
      <span id="uninitialized-alert-message"></span>
      <button class="primary-button" id="initialize-button" type="button">Initialize AIWorkHub</button>
    </section>

    <section class="source-alert identity-alert" id="identity-alert" aria-live="polite" hidden>
      <strong id="identity-alert-title">Manager identity</strong>
      <span id="identity-alert-message"></span>
    </section>

    <section class="callback-observability" id="callback-observability" aria-label="Callback delivery telemetry">
      <span><small>Backlog</small><strong id="callback-backlog">0</strong></span>
      <span><small>Last delivery</small><strong id="callback-delivery">Never</strong></span>
      <span><small>Retries</small><strong id="callback-retries">0</strong></span>
      <span><small>Delivery state</small><strong id="callback-degraded">Unknown</strong></span>
    </section>

    <section class="activity-peek" aria-label="Repository diagnostics">
      <span class="activity-peek-label">Last log</span>
      <span class="activity-peek-message" id="last-system-log">No system events yet</span>
      <span class="context-view-actions" aria-label="Repository context viewers">
        <button class="diagnostic-icon-button" type="button" id="open-system-log" title="Open system logs" aria-label="Open system logs">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 5.5h16v13H4zM7 9l2.5 2.5L7 14m5 0h5"/></svg><span>Logs</span>
        </button>
        <button class="diagnostic-icon-button" type="button" id="open-sessions" title="Open Session Manager" aria-label="Open Session Manager">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 4v5m0 6v5m12-16v5m0 6v5M6 9h12M6 15h12M9 9v6m6-6v6"/></svg><span>Sessions</span>
        </button>
        <button class="diagnostic-icon-button" type="button" id="open-ai-memory" title="Open AI Memory" aria-label="Open AI Memory">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 5.5A3 3 0 0 0 4.5 8v1.2A3.5 3.5 0 0 0 5 16v.5A3.5 3.5 0 0 0 9 20m6-14.5A3 3 0 0 1 19.5 8v1.2A3.5 3.5 0 0 1 19 16v.5a3.5 3.5 0 0 1-4 3.5M9 5.5V20m6-14.5V20M9 10h2m2 4h2"/></svg><span>Memory</span>
        </button>
        <button class="diagnostic-icon-button" type="button" id="open-kb" title="Open Knowledge Base" aria-label="Open Knowledge Base">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 5.5A2.5 2.5 0 0 1 6.5 3H11v16H6.5A2.5 2.5 0 0 0 4 21.5zm16 0A2.5 2.5 0 0 0 17.5 3H13v16h4.5a2.5 2.5 0 0 1 2.5 2.5z"/></svg><span>KB</span>
        </button>
        <button class="diagnostic-icon-button" type="button" id="open-operations" title="Open repository operations" aria-label="Open repository operations">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 19V11h3v8zm6 0V5h3v14zm6 0V8h3v11M2 21h20"/></svg><span>Operations</span>
        </button>
        <button class="diagnostic-icon-button" type="button" id="open-tool-use" title="Open model tool-use telemetry" aria-label="Open model tool-use telemetry">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 19V9m5 10V5m6 14v-7m5 7V3M2 21h20"/></svg><span>Telemetry</span>
        </button>
        <button class="diagnostic-icon-button" type="button" id="open-settings" title="Open repository settings" aria-label="Open repository settings">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 8.5a3.5 3.5 0 1 0 0 7 3.5 3.5 0 0 0 0-7zM19 12l2-1-2-3-2 .5-1.5-1L15 5h-6L8 7.5l-1.5 1L4 8l-1 3 2 1v2l-2 1 1 3 2.5-.5 1.5 1L9 21h6l1-2.5 1.5-1L20 18l1-3-2-1z"/></svg><span>Settings</span>
        </button>
      </span>
    </section>

    <section class="target-selector" aria-label="Coordinator routing">
      <span>Coordinator routing</span>
      <strong id="target-state">Automatic by originating chat</strong>
    </section>

    <section class="repo-router" id="repo-router" aria-label="Shared repository router" hidden>
      <span>Known repos</span>
      <div id="repo-router-list"></div>
    </section>

    <div class="workspace">
      <section class="task-workspace" aria-labelledby="task-heading">
        <div class="section-heading">
          <div>
            <h2 id="task-heading">Tasks</h2>
            <span class="section-count" id="filtered-count">0 shown</span>
          </div>
          <div class="status-filters" id="status-filters" aria-label="Filter by status">
            <button class="status-filter is-active" type="button" data-status="all" aria-pressed="true">All</button>
            <button class="status-filter" type="button" data-status="pending" aria-pressed="false">Pending</button>
            <button class="status-filter" type="button" data-status="processing" aria-pressed="false">Processing</button>
            <button class="status-filter" type="button" data-status="review" aria-pressed="false">Review</button>
            <button class="status-filter" type="button" data-status="blocked" aria-pressed="false">Blocked</button>
            <button class="status-filter" type="button" data-status="finished" aria-pressed="false">Finished</button>
            <button class="status-filter" type="button" data-status="archived" aria-pressed="false">Archived</button>
            <button class="status-filter" type="button" data-status="stale" aria-pressed="false">Stale</button>
          </div>
        </div>

        <div class="filter-bar">
          <label class="search-field">
            <span class="sr-only">Search tasks</span>
            <input id="task-search" type="search" placeholder="Search task, topic, runner" autocomplete="off">
          </label>
          <label>
            <span class="sr-only">Topic</span>
            <select id="topic-filter">
              <option value="all">All topics</option>
            </select>
          </label>
          <label>
            <span class="sr-only">Runner</span>
            <select id="runner-filter">
              <option value="all">All runners</option>
            </select>
          </label>
          <label>
            <span class="sr-only">Sort tasks</span>
            <select id="sort-order">
              <option value="status">Status order</option>
              <option value="updated">Latest activity</option>
              <option value="task">Task ID</option>
              <option value="topic">Topic</option>
            </select>
          </label>
        </div>

        <div class="table-shell" id="task-table" tabindex="-1">
          <table>
            <thead>
              <tr>
                <th scope="col">Status</th>
                <th scope="col">Task</th>
                <th scope="col">Topic</th>
                <th scope="col">Runner</th>
                <th scope="col">Model</th>
                <th scope="col">Activity</th>
                <th scope="col">Signal</th>
              </tr>
            </thead>
            <tbody id="task-table-body"></tbody>
          </table>
          <div class="table-empty" id="table-empty" hidden>No matching tasks</div>
          <div class="table-loading" id="table-loading">Loading queue</div>
        </div>
      </section>

      <aside class="inspector" aria-label="Task inspector and queue statistics">
        <section class="detail-panel" aria-labelledby="detail-heading">
          <div class="panel-heading">
            <div>
              <span class="eyebrow">Selected task</span>
              <h2 id="detail-heading">No task selected</h2>
            </div>
            <span id="detail-status" class="status-badge" hidden></span>
          </div>
          <div id="detail-loading" class="panel-state" hidden>Loading task</div>
          <div id="detail-error" class="panel-state error-state" hidden></div>
          <div id="detail-empty" class="detail-empty">No task selected</div>
          <div id="detail-content" hidden>
            <p class="task-objective" id="detail-objective"></p>
            <div class="task-retention-actions" id="detail-task-actions">
              <button id="detail-archive-task" class="secondary-button" type="button" hidden>Archive task</button>
              <button id="detail-restore-task" class="secondary-button" type="button" hidden>Restore task</button>
            </div>
            <dl class="metadata-grid" id="detail-metadata"></dl>
            <div class="result-block">
              <div class="subheading-row">
                <h3>Result and validation</h3>
                <span id="detail-validation" class="validation-label"></span>
              </div>
              <pre id="detail-result"></pre>
            </div>
            <!-- Live Output container (B855): host-side wiring (the
                 requestLiveOutput/liveOutput message contract and the
                 aiworkhub_dashboard_task_live_output MCP tool call) plus the
                 media/app.js renderer/poller for the selected task's stdout
                 tail and stderr tail below. -->
            <div class="result-block" id="detail-live-output-block" hidden>
              <div class="subheading-row">
                <h3>Live Output</h3>
                <span id="detail-live-output-state" class="validation-label"></span>
              </div>
              <div id="detail-live-output-container" class="live-output-summary"></div>
              <pre id="detail-live-output-stderr" hidden></pre>
              <details id="detail-live-output-raw" class="live-output-raw" hidden>
                <summary>Raw provider output</summary>
                <pre id="detail-live-output-raw-content"></pre>
              </details>
            </div>
            <div class="result-block" id="detail-ai-infra-block" hidden>
              <h3>AI context</h3>
              <div class="ai-infra-grid" id="detail-ai-infra"></div>
            </div>
            <details class="result-block" id="detail-evidence-block" hidden>
              <summary>Portable review evidence</summary>
              <div class="review-evidence" id="detail-evidence"></div>
            </details>
            <div class="result-block" id="detail-writes-block">
              <h3>Allowed writes</h3>
              <ul class="path-list" id="detail-writes"></ul>
            </div>
          </div>
        </section>

        <dialog class="diagnostic-dialog operations-dialog" id="operations-dialog">
        <section class="operations-panel" aria-labelledby="operations-heading">
          <div class="panel-heading tabs-heading">
            <div class="operations-heading-row">
              <div><h2 id="operations-heading">Operations</h2><span>Repository KPIs, planning, workforce, telemetry and retention</span></div>
              <button type="button" class="dialog-close" data-close-dialog="operations-dialog">Close</button>
            </div>
            <div class="tab-list" role="tablist" aria-label="Operational views">
              <button type="button" role="tab" aria-selected="true" aria-controls="panel-kpis" id="tab-kpis" data-tab="kpis">KPIs</button>
              <button type="button" role="tab" tabindex="-1" aria-selected="false" aria-controls="panel-topics" id="tab-topics" data-tab="topics">Topics</button>
              <button type="button" role="tab" tabindex="-1" aria-selected="false" aria-controls="panel-runners" id="tab-runners" data-tab="runners">Runners</button>
              <button type="button" role="tab" tabindex="-1" aria-selected="false" aria-controls="panel-plan" id="tab-plan" data-tab="plan">Plan DAG</button>
              <button type="button" role="tab" tabindex="-1" aria-selected="false" aria-controls="panel-workforce" id="tab-workforce" data-tab="workforce">Workforce</button>
              <button type="button" role="tab" tabindex="-1" aria-selected="false" aria-controls="panel-usage" id="tab-usage" data-tab="usage">Usage</button>
              <button type="button" role="tab" tabindex="-1" aria-selected="false" aria-controls="panel-tool-use" id="tab-tool-use" data-tab="tool-use">Tool Use</button>
              <button type="button" role="tab" tabindex="-1" aria-selected="false" aria-controls="panel-storage" id="tab-storage" data-tab="storage">Storage</button>
              <button type="button" role="tab" tabindex="-1" aria-selected="false" aria-controls="panel-returns" id="tab-returns" data-tab="returns">Returns</button>
              <button type="button" role="tab" tabindex="-1" aria-selected="false" aria-controls="panel-runs" id="tab-runs" data-tab="runs">Runs</button>
              <button type="button" role="tab" tabindex="-1" aria-selected="false" aria-controls="panel-warnings" id="tab-warnings" data-tab="warnings">Warnings</button>
            </div>
          </div>

          <div class="tab-panel" role="tabpanel" id="panel-kpis" aria-labelledby="tab-kpis">
            <div id="kpi-dashboard" aria-live="polite"></div>
          </div>
          <div class="tab-panel" role="tabpanel" id="panel-topics" aria-labelledby="tab-topics" hidden>
            <div class="stat-list" id="topic-stats"></div>
          </div>
          <div class="tab-panel" role="tabpanel" id="panel-runners" aria-labelledby="tab-runners" hidden>
            <div class="stat-list" id="runner-stats"></div>
          </div>
          <div class="tab-panel" role="tabpanel" id="panel-plan" aria-labelledby="tab-plan" hidden>
            <div id="plan-dag"></div>
          </div>
          <div class="tab-panel" role="tabpanel" id="panel-workforce" aria-labelledby="tab-workforce" hidden>
            <div id="workforce-list"></div>
          </div>
          <div class="tab-panel" role="tabpanel" id="panel-usage" aria-labelledby="tab-usage" hidden>
            <div class="stat-list" id="usage-list"></div>
          </div>
          <div class="tab-panel" role="tabpanel" id="panel-tool-use" aria-labelledby="tab-tool-use" hidden>
            <div class="stat-list" id="tool-use-list"></div>
          </div>
          <div class="tab-panel" role="tabpanel" id="panel-storage" aria-labelledby="tab-storage" hidden>
            <div class="storage-action-bar">
              <button id="storage-cleanup-preview" type="button" title="Recompute the repository-scoped retention preview and quarantine eligible worktrees after confirmation">Preview &amp; Quarantine</button>
              <button id="storage-registration-prune" type="button" title="Preview and prune stale Git registrations owned by this repository; no checkout files are deleted">Prune Registrations</button>
              <button id="terminal-log-cleanup-preview" type="button" title="Preview and quarantine only policy-aged terminal run logs; canonical ledgers and active work remain protected">Terminal Logs</button>
              <button id="task-cleanup-preview" type="button" title="Choose an age, preview old archived tasks, and move only eligible tasks into seven-day undo quarantine">Archived Tasks</button>
              <button id="runtime-cleanup-preview" type="button" title="Preview and quarantine obsolete extension runtime generations; current, rollback and live leased generations stay protected">Runtime Cache</button>
            </div>
            <div class="stat-list" id="storage-list"></div>
          </div>
          <div class="tab-panel" role="tabpanel" id="panel-returns" aria-labelledby="tab-returns" hidden>
            <div class="return-toolbar">
              <input id="return-search" type="search" placeholder="Search returns" aria-label="Search worker returns">
              <select id="return-topic" aria-label="Filter returns by topic"><option value="all">All topics</option></select>
              <select id="return-runner" aria-label="Filter returns by runner"><option value="all">All runners</option></select>
              <span class="return-pagination"><button type="button" id="return-previous" aria-label="Previous return page">Prev</button><span id="return-page">0 / 0</span><button type="button" id="return-next" aria-label="Next return page">Next</button></span>
            </div>
            <div class="signal-list" id="return-list"></div>
          </div>
          <div class="tab-panel" role="tabpanel" id="panel-runs" aria-labelledby="tab-runs" hidden>
            <div class="signal-list" id="run-list"></div>
          </div>
          <div class="tab-panel" role="tabpanel" id="panel-warnings" aria-labelledby="tab-warnings" hidden>
            <div class="signal-list" id="warning-list"></div>
          </div>
        </section>
        </dialog>
      </aside>
    </div>
  </main>

  <dialog class="diagnostic-dialog" id="system-log-dialog">
    <div class="dialog-heading">
      <div><h2>System Log</h2><span>Newest first · max 1 MB · retained up to 7 days</span></div>
      <button type="button" class="dialog-close" data-close-dialog="system-log-dialog">Close</button>
    </div>
    <div class="system-log-toolbar">
      <span id="system-log-count">0 events</span>
      <span><button type="button" id="system-log-copy">Copy</button><button type="button" id="system-log-clear">Clear</button></span>
    </div>
    <div class="system-log-terminal" id="system-log-list" role="log" aria-live="polite"></div>
  </dialog>

  <dialog class="diagnostic-dialog" id="ai-memory-dialog">
    <div class="dialog-heading">
      <div><h2>AI Memory</h2><span id="ai-memory-summary">Loading repository memory</span></div>
      <button type="button" class="dialog-close" data-close-dialog="ai-memory-dialog">Close</button>
    </div>
    <input class="dialog-search" id="ai-memory-search" type="search" placeholder="Filter key, value or tags" aria-label="Filter AI Memory">
    <div class="memory-list" id="ai-memory-list"></div>
  </dialog>

  <dialog class="diagnostic-dialog" id="sessions-dialog">
    <div class="dialog-heading">
      <div><h2>Session Manager</h2><span id="sessions-summary">Loading repository sessions</span></div>
      <button type="button" class="dialog-close" data-close-dialog="sessions-dialog">Close</button>
    </div>
    <input class="dialog-search" id="sessions-search" type="search" placeholder="Filter source, kind or content" aria-label="Filter Session Manager entries">
    <div class="memory-list" id="sessions-list"></div>
  </dialog>

  <dialog class="diagnostic-dialog" id="kb-dialog">
    <div class="dialog-heading">
      <div><h2>Knowledge Base</h2><span id="kb-summary">Loading repository knowledge</span></div>
      <button type="button" class="dialog-close" data-close-dialog="kb-dialog">Close</button>
    </div>
    <input class="dialog-search" id="kb-search" type="search" placeholder="Filter key, title, category, tags or body" aria-label="Filter Knowledge Base entries">
    <div class="memory-list" id="kb-list"></div>
  </dialog>

  <dialog class="diagnostic-dialog settings-dialog" id="settings-dialog">
    <div class="dialog-heading">
      <div><h2>Repository Settings</h2><span id="settings-summary">Loading repository feature settings</span></div>
      <button type="button" class="dialog-close" data-close-dialog="settings-dialog">Close</button>
    </div>
    <div class="settings-list" id="settings-list">
      <div class="panel-state">Loading settings</div>
    </div>
    <div class="settings-footnote">Stored only in this repository's <code>.aiworkhub/config/features.json</code> and <code>.aiworkhub/config/source_graph.json</code>. Task orchestration and callback routing remain protected core services.</div>
  </dialog>

  <div class="toast" id="toast" role="status" aria-live="polite" hidden></div>
  <script nonce="${nonceValue}" src="${scriptUri}"></script>
</body>
</html>`;
}

class TaskOperationsViewProvider {
  constructor(extensionUri) {
    this.extensionUri = extensionUri;
  }

  resolveWebviewView(webviewView) {
    sidebarView = webviewView;
    applyWebviewOptions(webviewView.webview, this.extensionUri);
    webviewView.webview.html = getHtmlForNavigatorWebview(webviewView.webview, this.extensionUri);

    const navigatorView = {
      postMessage: (msg) => {
        if (sidebarView === webviewView) {
          webviewView.webview.postMessage(msg);
        }
      },
    };
    webviewView.__aiworkhubNavigator = navigatorView;

    const pushRepoInfo = () => {
      pushRepositoryInfo(navigatorView, activeRepoIdentity);
    };

    webviewView.webview.onDidReceiveMessage((message) => {
      if (!message || typeof message !== "object") {
        return;
      }
      if (message.type === "ready") {
        pushRepoInfo();
      } else if (message.type === "openDashboard") {
        openDashboardCommand(this.extensionUri);
      } else if (message.type === "selectRepository") {
        selectRepositoryCommand();
      }
    });

    if (typeof webviewView.onDidDispose === "function") {
      webviewView.onDidDispose(() => {
        if (sidebarView === webviewView) {
          sidebarView = null;
        }
      });
    }
  }
}

class DashboardViewProvider {
  constructor(extensionUri) {
    this.extensionUri = extensionUri;
  }

  resolveWebviewView(webviewView) {
    const view = new ViewState((msg) => {
      if (sidebarView === webviewView) {
        webviewView.webview.postMessage(msg);
      }
    });
    webviewView.__aiworkhubViewState = view;

    // VS Code does not guarantee an initial onDidChangeVisibility event when
    // a view is first resolved. Seed the host-owned visibility state now so
    // auto-refresh starts immediately instead of waiting for the user to hide
    // the view, toggle Auto, or change the interval once.
    view.setVisible(webviewView.visible);

    webviewView.webview.onDidReceiveMessage((message) => handleInboundMessage(view, message));

    // Push the active repository label once the Webview reports ready.
    const pushRepoInfo = () => {
      pushRepositoryInfo(view, activeRepoIdentity);
    };
    const repoMsgHandler = (message) => {
      if (message && message.type === "ready") {
        pushRepoInfo();
      }
    };
    webviewView.webview.onDidReceiveMessage(repoMsgHandler);

    if (typeof webviewView.onDidChangeVisibility === "function") {
      webviewView.onDidChangeVisibility(() => {
        view.setVisible(webviewView.visible);
        if (webviewView.visible) {
          runBackgroundTask("sidebar visibility refresh", () => pushSnapshot(view));
        }
      });
    }
    if (typeof webviewView.onDidDispose === "function") {
      webviewView.onDidDispose(() => {
        view.dispose();
      });
    }
  }
}

function getHtmlForNavigatorWebview(webview, extensionUri) {
  const logoUri = webview.asWebviewUri(
    vscode.Uri.joinPath(extensionUri, "media", "aiworkhub-icon.png"),
  );
  const nonceValue = nonce();
  const csp = [
    "default-src 'none'",
    `style-src 'nonce-${nonceValue}'`,
    `script-src 'nonce-${nonceValue}'`,
    `img-src ${webview.cspSource} data:`,
    "frame-src 'none'",
    "connect-src 'none'",
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'none'",
  ].join("; ");
  return `<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="${csp}">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style nonce="${nonceValue}">
body{margin:0;padding:12px;color:var(--vscode-foreground);background:var(--vscode-sideBar-background);font:var(--vscode-font-size) var(--vscode-font-family)}
.mark{display:block;width:28px;height:28px;margin-bottom:8px;object-fit:contain}
h1{font-size:13px;margin:0 0 4px}p{margin:0 0 10px;color:var(--vscode-descriptionForeground);line-height:1.4}
button{width:100%;height:30px;margin:0 0 8px;color:var(--vscode-button-foreground);background:var(--vscode-button-background);border:0;border-radius:4px;font:inherit}
button.secondary{color:var(--vscode-button-secondaryForeground);background:var(--vscode-button-secondaryBackground)}
#repo{font-size:11px;word-break:break-word}
</style></head><body>
<img class="mark" src="${logoUri}" alt="" aria-hidden="true">
<h1>AIWorkHub</h1><p id="repo">Repository not selected</p>
<button id="open" type="button">Open Dashboard</button>
<button id="select" class="secondary" type="button">Select Repository</button>
<script nonce="${nonceValue}">
const vscode=acquireVsCodeApi();
document.getElementById("open").addEventListener("click",()=>vscode.postMessage({type:"openDashboard"}));
document.getElementById("select").addEventListener("click",()=>vscode.postMessage({type:"selectRepository"}));
window.addEventListener("message",(event)=>{const m=event.data||{};if(m.type==="repositoryInfo"){document.getElementById("repo").textContent=(m.repoName||m.label||"Repository")+" - "+(m.storageReady?"ready":m.storageStatus||"not ready");}});
vscode.postMessage({type:"ready"});
</script></body></html>`;
}

async function openDashboardCommand(extensionUri) {
  if (panel) {
    panel.reveal(vscode.ViewColumn.Active, false);
    return;
  }
  panel = vscode.window.createWebviewPanel(
    PANEL_VIEW_TYPE,
    "AIWorkHub",
    vscode.ViewColumn.Active,
    { retainContextWhenHidden: true }
  );
  recordSystemLog("[webview] dashboard panel opened");
  applyWebviewOptions(panel.webview, extensionUri);
  panel.webview.html = getHtmlForWebview(panel.webview, extensionUri);

  const view = new ViewState((msg) => {
    if (panel) {
      panel.webview.postMessage(msg);
    }
  });
  panel.__aiworkhubViewState = view;

  // A newly created visible panel likewise may not emit a view-state change.
  // Start its host-side polling schedule at creation time.
  view.setVisible(panel.visible);

  panel.webview.onDidReceiveMessage((message) => handleInboundMessage(view, message));

  // Push the active repository label once the Webview reports ready.
  panel.webview.onDidReceiveMessage((message) => {
    if (message && message.type === "ready") {
      pushRepositoryInfo(view, activeRepoIdentity);
      runBackgroundTask("dashboard runtime info", () => pushRuntimeInfo(view));
      pushCoordinatorTargets(view);
    }
  });

  panel.onDidChangeViewState(() => {
    view.setVisible(panel.visible);
    if (panel.visible) {
      runBackgroundTask("dashboard visibility refresh", () => pushSnapshot(view));
    }
  });
  panel.onDidDispose(() => {
    view.dispose();
    panel = null;
  });

  runBackgroundTask("dashboard initial snapshot", () => pushSnapshot(view));
}

function reviveDashboardPanel(webviewPanel, extensionUri, context) {
  // Dispose any stale controller (poll timer + McpStdioClient binding) from a
  // previous panel of this same PANEL_VIEW_TYPE before adopting the new one,
  // so a reload/deserialize cycle -- however many times it repeats -- never
  // leaves two live pollers or an orphaned ViewState running in the
  // background. See test_aiworkhub_vscode_reload_restore_b855.py.
  if (panel && panel !== webviewPanel && panel.__aiworkhubViewState) {
    panel.__aiworkhubViewState.dispose();
  }
  panel = webviewPanel;
  recordSystemLog("[webview] dashboard panel revived");
  applyWebviewOptions(panel.webview, extensionUri);
  panel.webview.html = getHtmlForWebview(panel.webview, extensionUri);
  // Re-create a fresh McpStdioClient binding for the revived panel -- the
  // extension host, not the Webview, owns MCP identity/session state, and a
  // deserialized panel starts with none of it until this call.
  const client = getMcpClient(context);
  const view = new ViewState((msg) => {
    if (panel) {
      panel.webview.postMessage(msg);
    }
  });
  view.bindClient(client);
  panel.__aiworkhubViewState = view;
  view.setVisible(panel.visible);
  panel.webview.onDidReceiveMessage((message) => handleInboundMessage(view, message));
  panel.webview.onDidReceiveMessage((message) => {
    if (message && message.type === "ready") {
      pushRepositoryInfo(view, activeRepoIdentity);
      runBackgroundTask("revived dashboard runtime info", () => pushRuntimeInfo(view));
      pushCoordinatorTargets(view);
    }
  });
  panel.onDidChangeViewState(() => {
    view.setVisible(panel.visible);
    if (panel.visible) {
      runBackgroundTask("revived dashboard visibility refresh", () => pushSnapshot(view));
    }
  });
  panel.onDidDispose(() => {
    view.dispose();
    panel = null;
  });
  runBackgroundTask("revived dashboard snapshot", () => pushSnapshot(view));
}

function refreshDashboardCommand() {
  if (panel && panel.__aiworkhubViewState) {
    runBackgroundTask("dashboard command refresh", () => pushSnapshot(panel.__aiworkhubViewState));
  }
}

async function restartMcpConnectionCommand() {
  const client = getMcpClient();
  client.stop({ restart: true });
  try {
    await client.ensureStarted();
    // Confirm the fresh session actually answers, not just that the child
    // spawned -- the cheap health tool, never the full snapshot payload.
    const health = await client.callTool(DASHBOARD_TOOLS.health, {});
    if (health && health.ok) {
      vscode.window.setStatusBarMessage("AIWorkHub MCP connection restarted.", 3000);
    } else {
      vscode.window.setStatusBarMessage("AIWorkHub MCP restarted but reported degraded health.", 5000);
    }
  } catch (err) {
    vscode.window.setStatusBarMessage(
      `AIWorkHub MCP restart failed: ${sanitizeErrorMessage(err)}`,
      5000
    );
  }
  refreshDashboardCommand();
}

// ── Repository selection (multi-root) ─────────────────────────────────────
// In a single-folder workspace this command is a no-op (already auto-bound).
// In a multi-root workspace it shows a QuickPick and, on selection, stops the
// old MCP child, clears pending state, persists the new workspace-folder URI,
// and starts one new repo-bound child. Never silently uses folders[0].

async function selectRepositoryCommand() {
  const ctx = extensionContext;
  if (!ctx) {
    vscode.window.showErrorMessage("AIWorkHub: extension not activated.");
    return;
  }
  const folders = vscode.workspace.workspaceFolders;
  if (!folders || folders.length === 0) {
    vscode.window.showErrorMessage("AIWorkHub: no workspace folder is open.");
    return;
  }
  if (folders.length === 1) {
    const repo = getActiveRepositoryRoot(ctx);
    activeRepoLabel = repositoryLabel(folders, repo.uriStr);
    activeRepoIdentity = { ...repo, label: activeRepoLabel };
    bindSystemLogRepository(repo.root);
    vscode.window.showInformationMessage(`AIWorkHub: bound to "${activeRepoLabel}" (single-folder workspace).`);
    return;
  }
  // Multi-root: show QuickPick with disambiguating labels.
  const items = folders.map((f) => {
    const sameName = folders.filter((ff) => ff.name === f.name);
    const displayLabel = sameName.length > 1
      ? `${f.name} — ${f.uri.fsPath.replace(/[\\/]+$/, "").split(/[\\/]/).filter(Boolean).slice(-2, -1)[0] || f.uri.fsPath}`
      : f.name;
    return { label: displayLabel, uriStr: f.uri.toString(), folder: f };
  });
  const choice = await vscode.window.showQuickPick(items, {
    placeHolder: "Select the repository to bind AIWorkHub to",
    title: "AIWorkHub: Select Repository",
  });
  if (!choice) {
    return; // user dismissed
  }
  // Persist the choice.
  ctx.workspaceState.update(WSP_STATE_KEY_REPO_URI, choice.uriStr);

  // Stop the old dispatcher (never orphaning a nested app-server
  // subprocess) and the old MCP child, then clear state. Remove ONLY this
  // window's own route record for the repository being left -- another
  // window still bound to that repository keeps its own file untouched.
  const previousRepoIdentity = activeRepoIdentity;
  if (vscodeLmBridgeHost) {
    vscodeLmBridgeHost.stop();
  }
  if (mcpClient) {
    const oldClient = mcpClient;
    mcpClient = null;
    await oldClient.stopDispatcherThenTerminate({ restart: false });
  }
  removeWindowRouteRecord(previousRepoIdentity);
  activeRepoIdentity = null;
  activeRepoLabel = "Switching repository";

  // Update the active label.
  activeRepoLabel = repositoryLabel(folders, choice.uriStr);
  const selectedRepo = getActiveRepositoryRoot(ctx);
  activeRepoIdentity = { ...selectedRepo, label: activeRepoLabel };
  bindSystemLogRepository(selectedRepo.root);
  if (vscodeLmBridgeHost) {
    await vscodeLmBridgeHost.start(activeRepoIdentity);
  }

  // Start the new client and refresh.
  try {
    const client = getMcpClient();
    await client.ensureStarted();
    vscode.window.setStatusBarMessage(`AIWorkHub: bound to "${activeRepoLabel}".`, 3000);
  } catch (err) {
    vscode.window.setStatusBarMessage(
      `AIWorkHub: repository bind failed: ${sanitizeErrorMessage(err)}`,
      5000
    );
  }
  // Push the new identity after getMcpClient has generated the replacement
  // claim episode for this selected repository.
  if (sidebarView && sidebarView.__aiworkhubNavigator) {
    pushRepositoryInfo(sidebarView.__aiworkhubNavigator, activeRepoIdentity);
  }
  if (panel && panel.__aiworkhubViewState) {
    pushRepositoryInfo(panel.__aiworkhubViewState, activeRepoIdentity);
    runBackgroundTask("repository switch runtime info", () => pushRuntimeInfo(panel.__aiworkhubViewState));
  }
  refreshDashboardCommand();
}

async function activate(context) {
  extensionContext = context;
  initializeDebugTracing(context);
  debugTrace("activation.begin", { version: EXPECTED_MCP_PACKAGE_VERSION });
  outputChannel = createManagedOutputChannel();
  context.subscriptions.push(outputChannel);
  const codexCallbackMuxEnabled = aiworkhubFeatureEnabled("enableCodexCallbackMux");
  const vscodeLmBridgeEnabled = aiworkhubFeatureEnabled("enableVscodeLmBridge");
  debugTrace("activation.features", { codexCallbackMuxEnabled, vscodeLmBridgeEnabled });
  vscodeLmBridgeHost = vscodeLmBridgeEnabled ? new VscodeLmBridgeHost(context) : null;
  if (vscodeLmBridgeHost) context.subscriptions.push(vscodeLmBridgeHost);
  if (typeof vscode.workspace.onDidChangeConfiguration === "function") {
    context.subscriptions.push(vscode.workspace.onDidChangeConfiguration(async (event) => {
      if (!event.affectsConfiguration(`${EXT_ID}.enableVscodeLmBridge`)) return;
      const enabled = aiworkhubFeatureEnabled("enableVscodeLmBridge");
      debugTrace("vscode_lm_bridge.configuration", { enabled });
      if (!enabled) {
        if (vscodeLmBridgeHost) vscodeLmBridgeHost.stop();
        return;
      }
      if (!vscodeLmBridgeHost) {
        vscodeLmBridgeHost = new VscodeLmBridgeHost(context);
        context.subscriptions.push(vscodeLmBridgeHost);
      }
      if (activeRepoIdentity && activeRepoIdentity.root) {
        await vscodeLmBridgeHost.start(activeRepoIdentity);
      }
    }));
  }

  // Materialize the same-host mux as a fail-open building block before it can
  // be advertised to a co-located Codex extension. The bootstrap pointer is
  // cheap and closes the first-install race where Codex starts the configured
  // launcher before the immutable generation copy has completed.
  if (codexCallbackMuxEnabled) {
    try {
      const stableMuxLauncher = materializeStableMuxLauncher(context);
      materializePathMuxShim(stableMuxLauncher, { extensionFsPath: context.extensionUri.fsPath });
      primeStableMuxRuntimePointer(context);
    } catch (err) {
      outputChannel.appendLine(
        `[codex] optional same-host mux unavailable; native Codex preserved: ${sanitizeErrorMessage(err)}`,
      );
    }
  } else {
    debugTrace("activation.restore_native_codex.begin");
    await restoreNativeCodexExecutable();
    debugTrace("activation.restore_native_codex.end");
  }

  // Publish this extension-host PID before changing the Codex executable. A
  // newly launched mux waits
  // on this exact repo/PID binding before it starts the real App Server.
  try {
    debugTrace("activation.repository_bind.begin");
    const repo = getActiveRepositoryRoot(context);
    activeRepoLabel = repositoryLabel(vscode.workspace.workspaceFolders || [], repo.uriStr);
    activeRepoIdentity = { ...repo, label: activeRepoLabel };
    bindSystemLogRepository(repo.root);
    refreshCoordinatorRouteOwnership(activeRepoIdentity);
    debugTrace("activation.repository_bind.end", { repo_id: repo.repoId });
  } catch (err) {
    debugTrace("activation.repository_bind.error", debugErrorFields(err));
    if (err.message === "no_repository_selected") {
      activeRepoLabel = "Select a repository";
    } else {
      activeRepoLabel = "No workspace";
    }
  }

  // Activate direct wake only when OpenAI's extension and this workspace
  // extension are actually co-located on the same host. Split-host
  // Remote-SSH keeps native Codex + durable manager inbox and never receives
  // a foreign launcher path. Any legacy host path is repaired here too.
  if (codexCallbackMuxEnabled) {
    await ensureCodexCallbackMuxConfigured(context);
  }

  // The callback launcher and repo route are now available.  Materialize the
  // heavier content-addressed runtime generation without blocking Codex from
  // entering the mux.
  let stableRuntime;
  try {
    debugTrace("activation.runtime_materialize.begin");
    stableRuntime = materializeStableRuntimeGeneration(context);
    debugTrace("activation.runtime_materialize.end", { generation: path.basename(stableRuntime.generationRoot || "") });
  } catch (err) {
    debugTrace("activation.runtime_materialize.error", debugErrorFields(err));
    stableRuntime = bundledRuntimeFallback(context);
    outputChannel.appendLine(
      `[runtime] immutable generation unavailable; using packaged runtime for this activation: ${sanitizeErrorMessage(err)}`,
    );
  }
  extensionRuntimeDir = stableRuntime.runtimeDir;
  try {
    startStableRuntimeLease(stableRuntime);
  } catch (err) {
    stableRuntimeInfo = stableRuntime;
    runtimeRetentionCache = {
      cached_at: Date.now(),
      payload: { ok: false, error: `runtime_lease_unavailable:${sanitizeErrorMessage(err)}`, candidates: [], quarantine_batches: [] },
    };
    outputChannel.appendLine(`[runtime] retention lease unavailable; cleanup disabled: ${sanitizeErrorMessage(err)}`);
  }
  const runtimeLabel = stableRuntime.generationRoot
    ? `immutable generation ${path.basename(stableRuntime.generationRoot)}`
    : "packaged runtime fallback";
  outputChannel.appendLine(`[runtime] using ${runtimeLabel}`);
  debugTrace("activation.config_repair.begin");
  ensureCodexStableMcpRegistered(context);
  ensureWorkspaceMcpConfigsRepaired(context);
  debugTrace("activation.config_repair.end");

  if (vscodeLmBridgeHost && activeRepoIdentity && activeRepoIdentity.root) {
    await vscodeLmBridgeHost.start(activeRepoIdentity);
  }

  // Sidebar view provider (uses legacy view ID for backward compatibility).
  context.subscriptions.push(
    vscode.window.registerWebviewPanelSerializer(PANEL_VIEW_TYPE, {
      async deserializeWebviewPanel(webviewPanel, state) {
        // `state` is the bounded presentation state VS Code persisted from
        // the webview's own getState()/setState() (selected task id,
        // filters, refresh interval) -- reviveDashboardPanel re-establishes
        // the live controller (ViewState + McpStdioClient) unconditionally;
        // replaying `state` into the Webview itself is left to media/app.js
        // (out of this task's allowed_writes), so a reloaded tab always
        // reaches Live (fresh snapshot) even before that replay lands.
        reviveDashboardPanel(webviewPanel, context.extensionUri, context);
      },
    }),
    vscode.window.registerWebviewViewProvider(
      "aiworkhub.view",
      new TaskOperationsViewProvider(context.extensionUri),
      { webviewOptions: { retainContextWhenHidden: true } }
    ),
    vscode.commands.registerCommand(`${EXT_ID}.openDashboard`, () => openDashboardCommand(context.extensionUri)),
    vscode.commands.registerCommand(`${EXT_ID}.refreshDashboard`, () => refreshDashboardCommand()),
    vscode.commands.registerCommand(`${EXT_ID}.restartDashboard`, () => restartMcpConnectionCommand()),
    vscode.commands.registerCommand(`${EXT_ID}.selectRepository`, () => selectRepositoryCommand())
  );
  debugTrace("activation.providers_registered");

  // Startup activation is the callback lifecycle owner.  An initialized
  // repository must start its MCP child and dispatcher even when the user
  // never opens the dashboard during this window/session.
  if ((codexCallbackMuxEnabled || vscodeLmBridgeEnabled) && activeRepoIdentity && activeRepoIdentity.root) {
    // B1017: publish route_pending first (fail-closed), then immediately
    // re-run route convergence after the MCP child and dispatcher are both
    // alive so a ready/fresh mux descriptor publishes capability_state=
    // available now instead of waiting for the 4-minute renewal tick.
    getMcpClient().ensureStarted().then(() => {
      refreshCoordinatorRouteOwnership(activeRepoIdentity);
      startStartupRouteConvergence(activeRepoIdentity);
    }).catch((err) => {
      outputChannel.appendLine(
        `[mcp] startup dispatcher activation failed: ${sanitizeErrorMessage(err)}`,
      );
    });
  }

  // B905: start the bounded lease-renewal timer once activation has settled
  // -- disposed in deactivate() and always restarted (never doubled) here,
  // so a reload cycle (deactivate -> activate) never leaves two intervals
  // ticking and never leaves the current window's lease to lapse silently.
  startWindowRouteRenewalTimer();
  startDispatcherWatchdog();
  debugTrace("activation.end");
}

async function deactivate() {
  stopStableRuntimeLease();
  stopDispatcherWatchdog();
  stopWindowRouteRenewalTimer();
  stopStartupRouteConvergence();
  if (vscodeLmBridgeHost) {
    vscodeLmBridgeHost.dispose();
    vscodeLmBridgeHost = null;
  }
  if (mcpClient) {
    const oldClient = mcpClient;
    mcpClient = null;
    // Stop dispatcher before child termination to prevent reload orphans.
    await oldClient.stopDispatcherThenTerminate({ restart: false });
  }
  flushSystemLogs();
  // Remove ONLY this window's own route record -- never another window's.
  removeWindowRouteRecord(activeRepoIdentity);
  activeRepoIdentity = null;
  extensionContext = null;
}

module.exports = {
  activate,
  deactivate,
  __testInternals: {
    McpStdioClient,
    ViewState,
    pushSnapshot,
    pushSnapshotNoRetry,
    pushRuntimeInfo,
    runBackgroundTask,
    findPythonCommand,
    findPythonCommandForLaunch,
    _preflightPythonCandidate,
    _preflightPythonCandidateAsync,
    _buildPreflightDiagnostic,
    getMcpClient,
    sanitizeErrorMessage,
    codexConfigTomlPath,
    resolveCodexConfigTomlPath,
    resolveExtensionRuntimeDir,
    primeStableMuxRuntimePointer,
    materializeStableRuntimeGeneration,
    bundledRuntimeFallback,
    repairCodexConfigTomlText,
    ensureCodexManagerGatesTomlText,
    ensureCodexCallbackMuxConfigured,
    restoreNativeCodexExecutable,
    aiworkhubFeatureEnabled,
    isAiWorkHubMuxExecutable,
    isLegacyAiWorkHubMuxPath,
    codexMuxLauncherSetting,
    materializeStableMuxLauncher,
    materializePathMuxShim,
    recordSystemLog,
    systemLogSnapshot,
    clearSystemLogs,
    migrateCodexConfigTomlText,
    migrateCodexConfigTomlRuntimePath,
    repairWorkspaceMcpConfigObject,
    repairClaudeMcpConfigObject,
    ensureWorkspaceMcpConfigsRepaired,
    ensureCodexMcpRegistrationTomlText,
    ensureCodexMcpRegistered,
    ensureCodexStableMcpRegistrationTomlText,
    materializeStableMcpLauncher,
    ensureCodexStableMcpRegistered,
    splitCodexPythonPathValue,
    ensureCodexConfigTomlRepaired,
    CODEX_OWNED_RUNTIME_SEGMENT_RE,
    windowRouteStatePath,
    removeWindowRouteRecord,
    renewWindowRouteLease,
    startWindowRouteRenewalTimer,
    stopWindowRouteRenewalTimer,
    runDispatcherWatchdogTick,
    startDispatcherWatchdog,
    stopDispatcherWatchdog,
    isDispatcherWatchdogTimerActive: () => Boolean(dispatcherWatchdogTimer),
    startStartupRouteConvergence,
    stopStartupRouteConvergence,
    isWindowRouteRenewalTimerActive: () => Boolean(windowRouteRenewalTimer),
    refreshCoordinatorRouteOwnership,
    readCoordinatorTargets,
    sanitizeCoordinatorTargetRoute,
    routeStatePath,
    sharedRepoRouteDir,
    readSharedRepoRouteRecord,
    writeSharedRepoRouteRecord,
    appServerMuxSidebandDir,
    appServerMuxInstancesDir,
    readMuxInstanceDescriptor,
    findVerifiedMuxThreadId,
    VscodeLmBridgeHost,
    vscodeLmBridgeRoot,
    vscodeLmModelFields,
    isCallableVscodeLmProvider,
    isGlm52LanguageModel,
    selectGlm52LanguageModel,
    isVscodeLanguageModel,
    vscodeLmModelSelectionRank,
    selectVscodeLanguageModel,
    vscodeLmAccessState,
    vscodeLmPermissionStorageKey,
    validateVscodeLmRequest,
    runVscodeLmAgent,
    runVscodeLmTextProtocol,
    parseVscodeLmJsonEnvelope,
    validateVscodeLmFinalEnvelope,
    vscodeLmPathMatchesPattern,
    glmTextToolProtocolPrompt,
    VSCODE_LM_PRIVATE_TOOLS,
    constants: {
      MCP_REQUEST_TIMEOUT_MS,
      VSCODE_LM_WORKER_TOOL_TIMEOUT_MS,
      MCP_SNAPSHOT_RECOVERY_ATTEMPTS,
      MCP_MAX_RUNTIME_REPAIR_ATTEMPTS,
      EXPECTED_MCP_PACKAGE_VERSION,
      WINDOW_ROUTE_LEASE_TTL_MS,
      WINDOW_ROUTE_RENEWAL_INTERVAL_MS,
      DISPATCHER_WATCHDOG_INTERVAL_MS,
      STARTUP_ROUTE_CONVERGENCE_INTERVAL_MS,
      STARTUP_ROUTE_CONVERGENCE_MAX_ATTEMPTS,
      APP_SERVER_MUX_SIDEBAND_DIR_ENV,
      REAL_THREAD_ID_RE,
      VSCODE_LM_MODEL,
      VSCODE_LM_SUPPORTED_MODELS,
      VSCODE_LM_REQUEST_SCHEMA,
      VSCODE_LM_HOST_SCHEMA,
      VSCODE_LM_RESPONSE_SCHEMA,
      VSCODE_LM_EDIT_RESPONSE_SCHEMA_V1,
      VSCODE_LM_EDIT_RESPONSE_SCHEMA_V2,
      VSCODE_LM_EDIT_RESPONSE_SCHEMA,
      VSCODE_LM_TOOL_REQUEST_SCHEMA,
      VSCODE_LM_TOOL_RESULT_SCHEMA,
    },
  },
};
