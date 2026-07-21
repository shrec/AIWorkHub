const vscode = require("vscode");
const childProcess = require("child_process");
const crypto = require("crypto");
const fs = require("fs");
const path = require("path");

const EXT_ID = "aiworkhub";
const DISPLAY_NAME = "AIWorkHub";
const WSP_STATE_KEY_REPO_URI = "aiworkhub.repositoryUri";
const PANEL_VIEW_TYPE = "aiworkhub.dashboard";
const EXPECTED_MCP_PACKAGE_VERSION = "0.6.0";
const WINDOW_SCOPE_ID = `window_${crypto.randomBytes(12).toString("hex")}`;

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
});

const TASK_ID_RE = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$/;
const ALLOWED_REFRESH_INTERVALS_MS = new Set([10000, 30000, 60000]);
const DEFAULT_REFRESH_INTERVAL_MS = 30000;
const REPO_ID_RE = /^repo_[a-f0-9]{32}$|^[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}$/;
const TARGET_PROVIDERS = Object.freeze(["codex", "claude"]);
const TARGET_ROUTE_KEY = "routing/coordinator-targets.json";

// ── The exact, narrow, read-only MCP tool allowlist this extension may call.
// Nothing else is ever sent as a tools/call `name`. See
// src/aiworkhub/dashboard_mcp_app.py.
const DASHBOARD_TOOLS = Object.freeze({
  snapshot: "aiworkhub_dashboard_snapshot",
  taskDetail: "aiworkhub_dashboard_task_detail",
  health: "aiworkhub_dashboard_health",
  liveOutput: "aiworkhub_dashboard_task_live_output",
});
// B857: the ONE lifecycle-owned callback dispatcher per repository lives
// inside this repo's own McpStdioClient child process (one dispatcher
// thread per "python -m aiworkhub.server" process, and this extension
// already guarantees exactly one such process per active repository --
// see getMcpClient()/McpStdioClient above). These tools never spawn a
// second process, a systemd unit, or an HTTP server; they only start/stop/
// inspect the in-process background thread.
const DISPATCHER_TOOLS = Object.freeze({
  ensureStarted: "aiworkhub_dispatcher_ensure_started",
  health: "aiworkhub_dispatcher_health",
  stop: "aiworkhub_dispatcher_stop",
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
const MCP_MAX_PENDING_REQUESTS = 16;
const MCP_MAX_LINE_BYTES = 8 * 1024 * 1024;
const MCP_MAX_STDERR_LOG_BYTES = 4096;
const MCP_MAX_RESTART_ATTEMPTS = 1;

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

function atomicWriteJson(file, payload) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const tmp = path.join(path.dirname(file), `.${path.basename(file)}.${process.pid}.${Date.now()}.tmp`);
  fs.writeFileSync(tmp, `${JSON.stringify(payload, null, 2)}\n`, "utf8");
  fs.renameSync(tmp, file);
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
    context.workspaceState.update(WSP_STATE_KEY_REPO_URI, folder.uri.toString());
    return {
      root: folder.uri.fsPath,
      label: folder.name,
      uriStr: folder.uri.toString(),
      ...readRepositoryManifestInfo(folder.uri.fsPath, folder.name),
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
    root: match.uri.fsPath,
    label: match.name,
    uriStr: match.uri.toString(),
    ...readRepositoryManifestInfo(match.uri.fsPath, match.name),
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

function findPythonExecutable(root) {
  const configured = vscode.workspace.getConfiguration("aiworkhub").get("pythonPath");
  const candidates = [
    typeof configured === "string" ? configured.trim() : "",
    path.join(root, ".venv", "bin", "python3"),
  ];
  for (const candidate of candidates) {
    if (candidate && fs.existsSync(candidate)) return candidate;
  }
  return "python3";
}

function routeStatePath(root) {
  return path.join(root, ".aiworkhub", "config", TARGET_ROUTE_KEY);
}

function defaultCoordinatorTargets(repoInfo) {
  return {
    schema_id: "aiworkhub.coordinator_targets.v1",
    repo_id: repoInfo.repoId,
    window_id: WINDOW_SCOPE_ID,
    claim_episode: activeClaimEpisode,
    updated_at: new Date().toISOString(),
    selected_provider: "codex",
    targets: {
      codex: {
        provider: "codex",
        capability_state: "available",
        route: { repo_id: repoInfo.repoId, window_id: WINDOW_SCOPE_ID, claim_episode: activeClaimEpisode, thread_id: `codex:${WINDOW_SCOPE_ID}`, session_id: activeClaimEpisode },
        wake: { mode: "direct_api_or_callback_inbox", supported: true },
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
  next.updated_at = new Date().toISOString();
  atomicWriteJson(routeStatePath(repoInfo.root), next);
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
    this.buffer = "";
    this.nextId = 1;
    this.pending = new Map();
    this.initialized = false;
    this.startingPromise = null;
    this.restartAttempts = 0;
    this.intentionalStop = false;
    // B859: an ``ok: true`` dispatcher_ensure_started response can still
    // report ``dispatcher_started: false`` / ``status: "start_failed"`` --
    // the tool call itself succeeded, but the dispatcher did not. Never
    // inferred from the absence of a transport error; only ever set by
    // _recordDispatcherEnsureResult().
    this.dispatcherReady = false;
  }

  // B859: the ONE place that decides whether a dispatcher_ensure_started
  // response counts as ready. An ok:true/dispatcher_started:false result
  // (e.g. the packaged runtime's callback dependency failed to import) is a
  // visible failure -- logged with a bounded, non-secret diagnostic -- not
  // a silently-ignored success.
  _recordDispatcherEnsureResult(result, context) {
    const started = Boolean(result && result.dispatcher_started);
    this.dispatcherReady = started;
    if (started) {
      return result;
    }
    const status = String((result && result.status) || "unknown");
    const reason = String((result && result.reason) || (result && result.error) || "").slice(0, 300);
    this.outputChannel.appendLine(
      `[mcp] dispatcher not ready after ${context}: status=${status}${reason ? ` reason=${reason}` : ""}`,
    );
    return result;
  }

  get running() {
    return Boolean(this.child && !this.child.killed);
  }

  // Never spawns a second child while one is starting/running -- callers
  // always go through this single bounded entry point.
  ensureStarted() {
    if (this.running && this.initialized) {
      return Promise.resolve();
    }
    if (this.startingPromise) {
      return this.startingPromise;
    }
    this.startingPromise = this._start().finally(() => {
      this.startingPromise = null;
    });
    return this.startingPromise;
  }

  async _start() {
    const root = this.repositoryRoot;
    const python = findPythonExecutable(root);
    const runtimeDir = extensionRuntimeDir;
    const env = {
      ...process.env,
      PYTHONIOENCODING: "utf-8",
      AIWORKHUB_REPO_ROOT: root,
      AIWORKHUB_REPO: root,
      AIWORKHUB_REPO_ID: this.repositoryIdentity.repoId,
      AIWORKHUB_WINDOW_ID: WINDOW_SCOPE_ID,
      AIWORKHUB_CLAIM_EPISODE: this.claimEpisode,
    };
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

    this.intentionalStop = false;
    this.initialized = false;
    this.buffer = "";
    this.nextId = 1;

    const child = childProcess.spawn(python, ["-m", "aiworkhub.server"], {
      cwd: runtimeDir || root,
      env,
      stdio: ["pipe", "pipe", "pipe"],
    });
    this.child = child;

    child.stdout.on("data", (chunk) => this._onStdout(chunk));
    child.stderr.on("data", (chunk) => {
      this.outputChannel.appendLine(`[mcp stderr] ${sanitizeStderrChunk(chunk)}`);
    });
    child.on("exit", (code, signal) => this._onExit(child, code, signal, null));
    child.on("error", (err) => this._onExit(child, null, null, err));

    await this._handshake();
  }

  async _handshake() {
    await this.request("initialize", {
      protocolVersion: MCP_PROTOCOL_VERSION,
      capabilities: {},
      clientInfo: { name: "aiworkhub-vscode", version: installedExtensionVersion() },
    });
    this.notify("notifications/initialized", {});
    this.initialized = true;
    this.restartAttempts = 0;
    // B857: converge on exactly one live repository-bound dispatcher right
    // after every successful handshake (covers activation, tab-
    // deserialization, and reload alike -- all of them go through
    // ensureStarted()/_start()/_handshake()). Idempotent server-side
    // (aiworkhub.core.dispatcher_ensure_started); a failure here must never
    // fail the MCP connection itself. B859: a transport-level success (no
    // thrown error) still needs its OWN result checked -- an
    // ``ok: true``/``dispatcher_started: false``/``status: "start_failed"``
    // payload is a visible failure, not silently-ignored readiness.
    try {
      const result = await this.callTool(DISPATCHER_TOOLS.ensureStarted, {});
      this._recordDispatcherEnsureResult(result, "handshake");
    } catch (err) {
      this.dispatcherReady = false;
      this.outputChannel.appendLine(`[mcp] dispatcher ensure-started failed: ${sanitizeErrorMessage(err)}`);
    }
  }

  _onStdout(chunk) {
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
      this._onMessage(line);
    }
  }

  _onMessage(line) {
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
    if (!pending) {
      return;
    }
    this.pending.delete(message.id);
    clearTimeout(pending.timer);
    if (message.error) {
      pending.reject(new Error((message.error && message.error.message) || "mcp_error"));
    } else {
      pending.resolve(message.result);
    }
  }

  _onExit(exitedChild, code, signal, spawnError) {
    // An intentionally replaced child can emit its exit after the new MCP
    // singleton has already started. Ignore that stale event so it cannot
    // tear down the replacement or consume the bounded restart budget.
    if (this.child !== exitedChild) {
      return;
    }
    this.child = null;
    this.initialized = false;
    const failure = spawnError || new Error(`mcp_child_exited code=${code} signal=${signal}`);
    this._failPending(failure);
    if (this.intentionalStop) {
      return;
    }
    if (this.restartAttempts < MCP_MAX_RESTART_ATTEMPTS) {
      this.restartAttempts += 1;
      this.outputChannel.appendLine("[mcp] child exited unexpectedly -- attempting one bounded restart");
      this.ensureStarted().catch((restartErr) => {
        this.outputChannel.appendLine(`[mcp] bounded restart failed: ${restartErr.message}`);
      });
    } else {
      this.outputChannel.appendLine("[mcp] child exited and the restart budget is exhausted -- offline until a manual restart");
    }
  }

  _failPending(err) {
    for (const pending of this.pending.values()) {
      clearTimeout(pending.timer);
      pending.reject(err);
    }
    this.pending.clear();
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
    const payload = `${JSON.stringify({ jsonrpc: "2.0", id, method, params: params || {} })}\n`;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error("mcp_request_timeout"));
      }, timeoutMs);
      this.pending.set(id, { resolve, reject, timer });
      this.child.stdin.write(payload, (err) => {
        if (err) {
          this.pending.delete(id);
          clearTimeout(timer);
          reject(err);
        }
      });
    });
  }

  notify(method, params) {
    if (!this.child) {
      return;
    }
    const payload = `${JSON.stringify({ jsonrpc: "2.0", method, params: params || {} })}\n`;
    this.child.stdin.write(payload);
  }

  async callTool(name, args) {
    await this.ensureStarted();
    const result = await this.request("tools/call", { name, arguments: args || {} });
    return extractToolResult(result);
  }

  async listTools() {
    await this.ensureStarted();
    const result = await this.request("tools/list", {});
    return Array.isArray(result && result.tools) ? result.tools : [];
  }

  stop({ restart = false } = {}) {
    this.intentionalStop = !restart;
    if (restart) {
      this.restartAttempts = 0;
    }
    const child = this.child;
    this.child = null;
    this.initialized = false;
    if (child && !child.killed) {
      try {
        child.kill();
      } catch (_err) {
        /* ignore */
      }
    }
  }

  // B865: stop the ONE lifecycle-owned dispatcher for this repository
  // BEFORE terminating this client's own MCP child process. The dispatcher's
  // CallbackBridge can hold a nested AppServerClient subprocess
  // (start_new_session=True -- its own process group), which a bare
  // SIGTERM to this outer child would never reach, orphaning it. Routing
  // through the aiworkhub_dispatcher_stop tool first lets the server join
  // the dispatcher thread and call that nested client's own .stop()
  // (see aiworkhub.callback_bridge.CallbackBridge.daemon/stop_daemon)
  // before this outer child dies. Best-effort and bounded: only sent while
  // the child is alive and handshaken; a transport failure here never
  // blocks terminating the child -- deactivate/reload/repo-switch must
  // never hang on a dead connection.
  async stopDispatcherThenTerminate({ restart = false } = {}) {
    if (this.running && this.initialized) {
      try {
        await this.request("tools/call", { name: DISPATCHER_TOOLS.stop, arguments: {} }, MCP_REQUEST_TIMEOUT_MS);
      } catch (_err) {
        // Best-effort -- proceed to terminate the child regardless.
      }
    }
    this.stop({ restart });
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

function installedExtensionVersion() {
  return String((extensionContext && extensionContext.extension && extensionContext.extension.packageJSON && extensionContext.extension.packageJSON.version) || "0.3.0");
}

function getMcpClient(context) {
  const repo = getActiveRepositoryRoot(context || extensionContext);
  const root = repo.root;
  const displayLabel = repositoryLabel(vscode.workspace.workspaceFolders || [], repo.uriStr);
  const identity = { ...repo, label: displayLabel };
  if (!mcpClient || mcpClient.repositoryRoot !== root || mcpClient.repositoryIdentity.repoId !== identity.repoId) {
    if (mcpClient) {
      // Fire-and-forget: getMcpClient() is synchronous and must return the
      // new client immediately; the stale client's dispatcher-stop-then-
      // terminate cleanup (see stopDispatcherThenTerminate) runs in the
      // background so it can never block binding to the new repository.
      mcpClient.stopDispatcherThenTerminate({ restart: false }).catch(() => {});
    }
    activeClaimEpisode = `episode_${crypto.randomBytes(12).toString("hex")}`;
    activeRepoIdentity = identity;
    activeRepoLabel = displayLabel;
    mcpClient = new McpStdioClient(root, outputChannel, identity, activeClaimEpisode);
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

function runtimeMismatchPayload(reason, runtimeVersion) {
  return {
    extensionVersion: installedExtensionVersion(),
    expectedMcpVersion: EXPECTED_MCP_PACKAGE_VERSION,
    runtimeVersion: runtimeVersion || "unavailable",
    reloadRequired: true,
    reason,
  };
}

async function pushRuntimeInfo(view) {
  try {
    const client = getMcpClient();
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
    if (missing.length || runtimeVersion !== EXPECTED_MCP_PACKAGE_VERSION) {
      view.postMessage({
        type: OUTBOUND_TYPES.runtimeInfo,
        payload: runtimeMismatchPayload(missing.length ? "mcp_capability_mismatch" : "mcp_version_mismatch", runtimeVersion),
      });
      return;
    }
    view.postMessage({
      type: OUTBOUND_TYPES.runtimeInfo,
      payload: {
        extensionVersion: installedExtensionVersion(),
        expectedMcpVersion: EXPECTED_MCP_PACKAGE_VERSION,
        runtimeVersion,
        reloadRequired: false,
        reason: "ok",
      },
    });
  } catch (err) {
    view.postMessage({
      type: OUTBOUND_TYPES.runtimeInfo,
      payload: runtimeMismatchPayload(sanitizeErrorMessage(err), "unavailable"),
    });
  }
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

async function pushSnapshot(view) {
  try {
    const client = getMcpClient();
    view.bindClient(client);
    const payload = await client.callTool(DASHBOARD_TOOLS.snapshot, {});
    if (payload && view.stillBoundTo(client)) {
      view.postMessage({ type: OUTBOUND_TYPES.snapshot, payload: sanitizeWebviewPayload(payload) });
    } else if (!payload) {
      view.postMessage({ type: OUTBOUND_TYPES.error, message: "snapshot_unavailable" });
    }
  } catch (err) {
    view.postMessage({ type: OUTBOUND_TYPES.offline, reason: sanitizeErrorMessage(err) });
  }
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
// HTML-escapes before this ever reaches the Webview. NOTE: the Webview-side
// renderer for this message (media/app.js) is a follow-up -- this function
// and its message-contract entries (ALLOWED_INBOUND_MESSAGE_TYPES,
// OUTBOUND_TYPES.liveOutput, DASHBOARD_TOOLS.liveOutput) are the host-side
// half of the wiring.
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

// The sole initialization trigger: one bounded MCP tool call, tied to the
// active repo_id/window/claim episode -- the window/claim-episode binding is
// implicit in the AIWORKHUB_WINDOW_ID/AIWORKHUB_CLAIM_EPISODE env vars the
// MCP child was already spawned with (see McpStdioClient._start); the
// repo_id argument is only ever a real "repo_<hex>" identity already known
// to this window, never a placeholder label, so a first-time init on an
// uninitialized repository is never refused. Never called from activation
// or repository selection -- only from this explicit user-clicked action.
async function pushInitializeStorage(view) {
  try {
    const client = getMcpClient();
    view.bindClient(client);
    const repoId = activeRepoIdentity && REAL_REPO_ID_RE.test(String(activeRepoIdentity.repoId || ""))
      ? activeRepoIdentity.repoId
      : "";
    const payload = await client.callTool(INITIALIZE_TOOL, { repo_id: repoId });
    if (!payload || payload.ok !== true) {
      view.postMessage({
        type: OUTBOUND_TYPES.error,
        message: (payload && (payload.message || payload.error)) || "initialize_failed",
      });
      return;
    }
    if (view.stillBoundTo(client)) {
      pushRepositoryInfo(view, activeRepoIdentity);
      await pushSnapshot(view);
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
    case "refresh":
    case "retry":
      pushSnapshot(view);
      break;
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
        // B857: an explicit coordinator-target switch must re-bind the
        // ONE live dispatcher to the newly selected provider -- never a
        // second dispatcher, never left routing to the stale provider.
        // Fire-and-forget: the Webview already has its optimistic update
        // above; a transport hiccup here surfaces on the next health poll.
        {
          const client = getMcpClient();
          client
            .callTool(DISPATCHER_TOOLS.ensureStarted, {})
            .then((result) => client._recordDispatcherEnsureResult(result, "coordinator-target switch"))
            .catch((err) => {
              client.dispatcherReady = false;
              outputChannel.appendLine(`[mcp] dispatcher re-bind after target switch failed: ${sanitizeErrorMessage(err)}`);
            });
        }
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
    <div class="brand-block">
      <div class="brand-mark" aria-hidden="true">G</div>
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
      <strong>Reload/Restart required</strong>
      <span id="reload-alert-message"></span>
    </section>

    <section class="source-alert uninitialized-alert" id="uninitialized-alert" aria-live="polite" hidden>
      <strong>AIWorkHub is not initialized for this repository</strong>
      <span id="uninitialized-alert-message"></span>
      <button class="primary-button" id="initialize-button" type="button">Initialize AIWorkHub</button>
    </section>

    <section class="target-selector" aria-label="Coordinator target">
      <span>Coordinator target</span>
      <button type="button" data-provider="codex">Codex</button>
      <button type="button" data-provider="claude">Claude</button>
      <button type="button" data-provider="copilot">Copilot</button>
      <strong id="target-state">Loading target state</strong>
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
              <pre id="detail-live-output-container"></pre>
              <pre id="detail-live-output-stderr" hidden></pre>
            </div>
            <div class="result-block" id="detail-ai-infra-block" hidden>
              <h3>AI context</h3>
              <div class="ai-infra-grid" id="detail-ai-infra"></div>
            </div>
            <div class="result-block" id="detail-writes-block">
              <h3>Allowed writes</h3>
              <ul class="path-list" id="detail-writes"></ul>
            </div>
          </div>
        </section>

        <section class="operations-panel" aria-labelledby="operations-heading">
          <div class="panel-heading tabs-heading">
            <h2 id="operations-heading">Operations</h2>
            <div class="tab-list" role="tablist" aria-label="Operational views">
              <button type="button" role="tab" aria-selected="true" aria-controls="panel-topics" id="tab-topics" data-tab="topics">Topics</button>
              <button type="button" role="tab" tabindex="-1" aria-selected="false" aria-controls="panel-runners" id="tab-runners" data-tab="runners">Runners</button>
              <button type="button" role="tab" tabindex="-1" aria-selected="false" aria-controls="panel-usage" id="tab-usage" data-tab="usage">Usage</button>
              <button type="button" role="tab" tabindex="-1" aria-selected="false" aria-controls="panel-returns" id="tab-returns" data-tab="returns">Returns</button>
              <button type="button" role="tab" tabindex="-1" aria-selected="false" aria-controls="panel-runs" id="tab-runs" data-tab="runs">Runs</button>
              <button type="button" role="tab" tabindex="-1" aria-selected="false" aria-controls="panel-warnings" id="tab-warnings" data-tab="warnings">Warnings</button>
            </div>
          </div>

          <div class="tab-panel" role="tabpanel" id="panel-topics" aria-labelledby="tab-topics">
            <div class="stat-list" id="topic-stats"></div>
          </div>
          <div class="tab-panel" role="tabpanel" id="panel-runners" aria-labelledby="tab-runners" hidden>
            <div class="stat-list" id="runner-stats"></div>
          </div>
          <div class="tab-panel" role="tabpanel" id="panel-usage" aria-labelledby="tab-usage" hidden>
            <div class="stat-list" id="usage-list"></div>
          </div>
          <div class="tab-panel" role="tabpanel" id="panel-returns" aria-labelledby="tab-returns" hidden>
            <div class="signal-list" id="return-list"></div>
          </div>
          <div class="tab-panel" role="tabpanel" id="panel-runs" aria-labelledby="tab-runs" hidden>
            <div class="signal-list" id="run-list"></div>
          </div>
          <div class="tab-panel" role="tabpanel" id="panel-warnings" aria-labelledby="tab-warnings" hidden>
            <div class="signal-list" id="warning-list"></div>
          </div>
        </section>
      </aside>
    </div>
  </main>

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
    webviewView.webview.html = getHtmlForNavigatorWebview(webviewView.webview);

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
          pushSnapshot(view);
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

function getHtmlForNavigatorWebview(webview) {
  const nonceValue = nonce();
  const csp = [
    "default-src 'none'",
    `style-src 'nonce-${nonceValue}'`,
    `script-src 'nonce-${nonceValue}'`,
    "img-src data:",
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
.mark{width:28px;height:28px;margin-bottom:8px}
h1{font-size:13px;margin:0 0 4px}p{margin:0 0 10px;color:var(--vscode-descriptionForeground);line-height:1.4}
button{width:100%;height:30px;margin:0 0 8px;color:var(--vscode-button-foreground);background:var(--vscode-button-background);border:0;border-radius:4px;font:inherit}
button.secondary{color:var(--vscode-button-secondaryForeground);background:var(--vscode-button-secondaryBackground)}
#repo{font-size:11px;word-break:break-word}
</style></head><body>
<svg class="mark" viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M12 2 21 7v10l-9 5-9-5V7l9-5Zm0 3.1L5.7 8.6v6.8l6.3 3.5 6.3-3.5V8.6L12 5.1Zm0 3.2 3.4 1.9v3.6L12 15.7l-3.4-1.9v-3.6L12 8.3Z"/></svg>
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
      pushRuntimeInfo(view);
      pushCoordinatorTargets(view);
    }
  });

  panel.onDidChangeViewState(() => {
    view.setVisible(panel.visible);
    if (panel.visible) {
      pushSnapshot(view);
    }
  });
  panel.onDidDispose(() => {
    view.dispose();
    panel = null;
  });

  pushSnapshot(view);
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
      pushRuntimeInfo(view);
      pushCoordinatorTargets(view);
    }
  });
  panel.onDidChangeViewState(() => {
    view.setVisible(panel.visible);
    if (panel.visible) {
      pushSnapshot(view);
    }
  });
  panel.onDidDispose(() => {
    view.dispose();
    panel = null;
  });
  pushSnapshot(view);
}

function refreshDashboardCommand() {
  if (panel && panel.__aiworkhubViewState) {
    pushSnapshot(panel.__aiworkhubViewState);
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
  // subprocess) and the old MCP child, then clear state.
  if (mcpClient) {
    const oldClient = mcpClient;
    mcpClient = null;
    await oldClient.stopDispatcherThenTerminate({ restart: false });
  }
  activeRepoIdentity = null;
  activeRepoLabel = "Switching repository";

  // Update the active label.
  activeRepoLabel = repositoryLabel(folders, choice.uriStr);
  const selectedRepo = getActiveRepositoryRoot(ctx);
  activeRepoIdentity = { ...selectedRepo, label: activeRepoLabel };

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
    pushRuntimeInfo(panel.__aiworkhubViewState);
  }
  refreshDashboardCommand();
}

function activate(context) {
  extensionContext = context;
  extensionRuntimeDir = path.join(context.extensionUri.fsPath, "runtime");
  outputChannel = vscode.window.createOutputChannel("AIWorkHub");
  context.subscriptions.push(outputChannel);

  // Resolve the initial active repository and label.
  try {
    const repo = getActiveRepositoryRoot(context);
    activeRepoLabel = repositoryLabel(vscode.workspace.workspaceFolders || [], repo.uriStr);
    activeRepoIdentity = { ...repo, label: activeRepoLabel };
  } catch (err) {
    if (err.message === "no_repository_selected") {
      activeRepoLabel = "Select a repository";
    } else {
      activeRepoLabel = "No workspace";
    }
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
}

async function deactivate() {
  if (mcpClient) {
    const oldClient = mcpClient;
    mcpClient = null;
    // B865: stop the dispatcher before the child dies -- extension host
    // teardown (deactivate on reload/uninstall/window close) is exactly the
    // path that previously let a nested app-server subprocess survive as an
    // orphan (see stopDispatcherThenTerminate).
    await oldClient.stopDispatcherThenTerminate({ restart: false });
  }
  extensionContext = null;
}

module.exports = { activate, deactivate };
