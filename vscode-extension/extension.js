const vscode = require("vscode");
const childProcess = require("child_process");
const crypto = require("crypto");
const fs = require("fs");
const os = require("os");
const path = require("path");

const EXT_ID = "aiworkhub";
const DISPLAY_NAME = "AIWorkHub";
const WSP_STATE_KEY_REPO_URI = "aiworkhub.repositoryUri";
const PANEL_VIEW_TYPE = "aiworkhub.dashboard";
const EXPECTED_MCP_PACKAGE_VERSION = "0.6.77";
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
  "clearSystemLogs",
  "copySystemLogs",
  "requestMemory",
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
});

const TASK_ID_RE = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$/;
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

// VS Code Language Model bridge.  Custom models contributed to Copilot Chat
// (for example customendpoint/glm-5.2) are available only inside the
// extension host -- the standalone `copilot` CLI does not inherit that model
// registration or its editor authorization.  Requests are exchanged through
// an owner-only runtime spool; task state and outputs stay repository-bound.
const VSCODE_LM_REQUEST_SCHEMA = "aiworkhub.vscode_lm.request.v1";
const VSCODE_LM_HOST_SCHEMA = "aiworkhub.vscode_lm.host.v1";
const VSCODE_LM_RESPONSE_SCHEMA = "aiworkhub.vscode_lm.response.v1";
const VSCODE_LM_EDIT_RESPONSE_SCHEMA = "aiworkhub.vscode_lm.edit_response.v1";
const VSCODE_LM_MODEL = "glm-5.2";
const VSCODE_LM_POLL_MS = 500;
const VSCODE_LM_HEARTBEAT_MS = 10000;
const VSCODE_LM_MAX_REQUEST_BYTES = 8 * 1024 * 1024;
const VSCODE_LM_MAX_AGENT_TURNS = 24;
const VSCODE_LM_PERMISSION_KEY = "aiworkhub.vscodeLmWorkerPermission.v1";
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
  const tmp = path.join(path.dirname(file), `.${path.basename(file)}.${process.pid}.${Date.now()}.tmp`);
  fs.writeFileSync(tmp, `${JSON.stringify(payload, null, 2)}\n`, "utf8");
  fs.renameSync(tmp, file);
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

function findPythonCommand(root) {
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
    if (REAL_THREAD_ID_RE.test(String(threadId || "")) || attempts >= STARTUP_ROUTE_CONVERGENCE_MAX_ATTEMPTS) {
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
    this._clearLifecycleOwnership(candidate);
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
    this.startingPromise = this._startWithVersionRepair().finally(() => {
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
        if (!message.includes("mcp_version_mismatch_pre_service")) throw err;
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
    const python = findPythonCommand(root);
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
      // The extension already owns the repository-bound Codex App Server
      // through the transparent mux. Deliver callbacks through that exact
      // mux instead of spawning a second app-server process, which can strand
      // an inflight batch behind a long lease after reload.
      AIWORKHUB_CALLBACK_TRANSPORT: "sideband",
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
    this.buffer = "";
    this.nextId = 1;
    this.pending.clear();
    this.pendingChildren.clear();

    const child = childProcess.spawn(python.command, [...python.argsPrefix, "-m", "aiworkhub.server"], {
      cwd: runtimeDir || root,
      env,
      stdio: ["pipe", "pipe", "pipe"],
    });
    this.child = child;
    this.lifecycleChild = child;
    this.lifecyclePid = child.pid;

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
    // Dispatcher / Source Graph convergence is deliberately background-only.
    // A high-level tool call here would recurse through ensureStarted() while
    // ensureStarted() is already waiting for _handshake(), leaving the Webview
    // stuck at "MCP runtime checking" / "Loading queue".  Use raw JSON-RPC in
    // a fire-and-forget task so the dashboard can render health/snapshot first.
    this._convergeBackgroundServices();
  }


  async _assertRuntimeVersionBeforeServices() {
    let health;
    try {
      health = extractToolResult(await this.request("tools/call", { name: DASHBOARD_TOOLS.health, arguments: {} }));
    } catch (err) {
      throw new Error(`mcp_health_preflight_failed:${sanitizeErrorMessage(err)}`);
    }
    const runtimeVersion = String((health && (health.server_version || health.version || health.package_version)) || "unavailable");
    if (runtimeVersion !== EXPECTED_MCP_PACKAGE_VERSION) {
      throw new Error(`mcp_version_mismatch_pre_service:${runtimeVersion}`);
    }
  }

  async _callToolRaw(name, args, timeoutMs = MCP_REQUEST_TIMEOUT_MS) {
    return extractToolResult(await this.request("tools/call", { name, arguments: args || {} }, timeoutMs));
  }

  _convergeBackgroundServices() {
    const run = async () => {
      try {
        await this._callToolRaw(DISPATCHER_TOOLS.ensureStarted, {}, 5000);
      } catch (err) {
        this.outputChannel.appendLine(`[mcp] callback dispatcher background convergence failed: ${sanitizeErrorMessage(err)}`);
      }
      try {
        await this._callToolRaw(SOURCE_GRAPH_DAEMON_TOOLS.ensureStarted, {}, 5000);
      } catch (err) {
        this.outputChannel.appendLine(`[mcp] source graph background convergence failed: ${sanitizeErrorMessage(err)}`);
      }
    };
    run().catch((err) => {
      this.outputChannel.appendLine(`[mcp] background service convergence failed: ${sanitizeErrorMessage(err)}`);
    });
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
    // An intentionally replaced child can emit its exit after the new MCP
    // singleton has already started. Ignore that stale event so it cannot
    // tear down the replacement or consume the bounded restart budget.
    if (this.child !== exitedChild) {
      return;
    }
    this.child = null;
    this._clearLifecycleOwnership(exitedChild);
    this.initialized = false;
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
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        this.pendingChildren.delete(id);
        if (this.child === requestChild && !this.initialized) {
          this.child = null;
          this._terminateOwnedChild(requestChild);
        }
        reject(new Error("mcp_request_timeout"));
      }, timeoutMs);
      this.pending.set(id, { resolve, reject, timer });
      this.pendingChildren.set(id, requestChild);
      requestChild.stdin.write(payload, (err) => {
        if (err) {
          this.pending.delete(id);
          this.pendingChildren.delete(id);
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
    this._clearRecoveryTimer();
    this.intentionalStop = !restart;
    const child = this.lifecycleChild;
    this.child = null;
    this.initialized = false;
    this._failPendingForChild(child, new Error(restart ? "mcp_restarting" : "mcp_stopped"));
    this._terminateOwnedChild(child);
  }

  // Best-effort service shutdown followed by exact-object child termination.
  async stopDispatcherThenTerminate({ restart = false } = {}) {
    this._clearRecoveryTimer();
    const ownedChild = this.lifecycleChild;
    this.intentionalStop = !restart;
    if (this.running && this.initialized && this.child === ownedChild) {
      try {
        await this.request(
          "tools/call",
          { name: DISPATCHER_TOOLS.stop, arguments: {} },
          MCP_REQUEST_TIMEOUT_MS,
        );
      } catch (_err) {
        // Best-effort -- proceed to terminate the child regardless.
      }
      try {
        await this.request(
          "tools/call",
          { name: SOURCE_GRAPH_DAEMON_TOOLS.stop, arguments: {} },
          MCP_REQUEST_TIMEOUT_MS,
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

function isGlm52LanguageModel(model) {
  return vscodeLmModelFields(model).some((value) => {
    const normalized = value.toLowerCase().replace(/_/g, "-");
    return normalized === VSCODE_LM_MODEL || normalized.includes(VSCODE_LM_MODEL);
  });
}

function selectGlm52LanguageModel(models) {
  const matches = (Array.isArray(models) ? models : []).filter(isGlm52LanguageModel);
  return matches.sort((left, right) => {
    const leftExact = vscodeLmModelFields(left).some((value) => value.toLowerCase() === VSCODE_LM_MODEL) ? 0 : 1;
    const rightExact = vscodeLmModelFields(right).some((value) => value.toLowerCase() === VSCODE_LM_MODEL) ? 0 : 1;
    return leftExact - rightExact || String(left.id || "").localeCompare(String(right.id || ""));
  })[0] || null;
}

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
  if (String(payload.model || "").toLowerCase() !== VSCODE_LM_MODEL) {
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
  const deadline = Date.parse(String(payload.deadline || ""));
  if (!Number.isFinite(deadline) || deadline <= Date.now()) throw new Error("vscode_lm_request_expired");
  return { ...payload, requestId, workspacePath, workspaceHome, responsePath, allowedWrites };
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
        mode: { type: "string", enum: ["focus", "slice", "bundle"] },
        query: { type: "string", minLength: 1, maxLength: 512 },
        budget: { type: "integer", minimum: 8, maximum: 160 },
        target: { type: ["string", "null"], maxLength: 256 },
        bundle_type: { type: "string", enum: ["bugfix", "feature", "refactor", "audit", "optimize", "explore"] },
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

async function invokeVscodeLmPrivateTool(call) {
  const permitted = VSCODE_LM_PRIVATE_TOOLS.find((tool) => tool.name === call.name);
  if (!permitted) throw new Error(`vscode_lm_tool_not_allowed:${String(call.name || "")}`);
  if (!mcpClient || !activeRepoIdentity) throw new Error("vscode_lm_mcp_unavailable");
  if (mcpClient.repositoryRoot !== activeRepoIdentity.root) throw new Error("vscode_lm_mcp_repo_mismatch");
  return mcpClient.callTool(permitted.name, call.input || {});
}

function glmAgentProtocolPrompt(prompt, allowedWrites) {
  return `${prompt}\n\nAIWorkHub VS Code GLM worker contract:\n` +
    `- Source Graph is mandatory throughout code discovery; never request or simulate grep/rg/find/tree.\n` +
    `- Use the supplied AIWorkHub Session Manager, AI Memory and KB tools when relevant.\n` +
    `- At completion output ONLY one JSON object with schema_id ${VSCODE_LM_EDIT_RESPONSE_SCHEMA}.\n` +
    `- files must contain complete UTF-8 content and paths must match allowed_writes.\n` +
    `- allowed_writes=${JSON.stringify(allowedWrites)}\n` +
    `Required shape: {"schema_id":"${VSCODE_LM_EDIT_RESPONSE_SCHEMA}","summary":"...","files":[{"path":"repo/relative","content":"complete content"}]}`;
}

async function runVscodeLmAgent(model, request, cancellationToken) {
  if (!model || !model.capabilities || !model.capabilities.toolCalling) {
    throw new Error("vscode_lm_tool_calling_unavailable");
  }
  const messages = [vscode.LanguageModelChatMessage.User(glmAgentProtocolPrompt(request.prompt, request.allowedWrites))];
  let sourceGraphAcknowledged = false;
  for (let turn = 0; turn < VSCODE_LM_MAX_AGENT_TURNS; turn += 1) {
    const availableTools = sourceGraphAcknowledged ? VSCODE_LM_PRIVATE_TOOLS : [VSCODE_LM_PRIVATE_TOOLS[0]];
    const options = {
      justification: "Run this explicitly queued AIWorkHub repository task using the user's existing VS Code model authorization.",
      tools: availableTools,
      toolMode: sourceGraphAcknowledged ? vscode.LanguageModelChatToolMode.Auto : vscode.LanguageModelChatToolMode.Required,
    };
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
    if (calls.length === 0) {
      if (!sourceGraphAcknowledged) throw new Error("vscode_lm_source_graph_not_acknowledged");
      const text = textParts.join("").trim();
      if (!text) throw new Error("vscode_lm_empty_response");
      return text;
    }
    messages.push(vscode.LanguageModelChatMessage.Assistant(assistantParts));
    const results = [];
    for (const call of calls) {
      let result;
      try {
        result = await invokeVscodeLmPrivateTool(call);
      } catch (err) {
        result = { ok: false, error: sanitizeErrorMessage(err) };
      }
      if (call.name === "aiworkhub_manager_source_graph_query" && result && result.ok === true) {
        sourceGraphAcknowledged = true;
      }
      results.push(languageModelToolResultPart(call.callId, result));
    }
    messages.push(vscode.LanguageModelChatMessage.User(results));
  }
  throw new Error("vscode_lm_agent_turn_limit");
}

class VscodeLmBridgeHost {
  constructor(context) {
    this.context = context;
    this.repoInfo = null;
    this.pollTimer = null;
    this.heartbeatTimer = null;
    this.processing = false;
    this.permissionPrompt = null;
    this.disposed = false;
  }

  async start(repoInfo) {
    this.stop();
    if (!repoInfo || !REAL_REPO_ID_RE.test(String(repoInfo.repoId || ""))) return;
    this.repoInfo = { ...repoInfo };
    await this.publishHeartbeat();
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
    return vscode.lm.selectChatModels();
  }

  async publishHeartbeat() {
    if (!this.repoInfo || this.disposed) return;
    const model = selectGlm52LanguageModel(await this.models());
    const globalState = this.context && this.context.globalState;
    const permissionGranted = Boolean(
      globalState && typeof globalState.get === "function"
        ? globalState.get(VSCODE_LM_PERMISSION_KEY, false)
        : false,
    );
    const payload = {
      schema_id: VSCODE_LM_HOST_SCHEMA,
      repo_id: this.repoInfo.repoId,
      window_id: WINDOW_SCOPE_ID,
      extension_host_pid: process.pid,
      models: model ? [VSCODE_LM_MODEL] : [],
      model_metadata: model ? { id: model.id, family: model.family, name: model.name, vendor: model.vendor, version: model.version, maxInputTokens: model.maxInputTokens } : null,
      permission_granted: permissionGranted,
      updated_at: new Date().toISOString(),
    };
    const hostPath = path.join(vscodeLmBridgeRoot(), "hosts", this.repoInfo.repoId, `${WINDOW_SCOPE_ID}.json`);
    atomicWriteOwnerJson(hostPath, payload);
  }

  async ensurePermission() {
    const globalState = this.context && this.context.globalState;
    if (globalState && typeof globalState.get === "function" && globalState.get(VSCODE_LM_PERMISSION_KEY, false)) return true;
    if (!this.permissionPrompt) {
      this.permissionPrompt = vscode.window.showInformationMessage(
        "AIWorkHub has a queued GLM‑5.2 worker task. Allow it to use the GLM model already authorized in VS Code?",
        "Allow GLM workers",
      ).then(async (choice) => {
        const granted = choice === "Allow GLM workers";
        if (granted && globalState && typeof globalState.update === "function") {
          await globalState.update(VSCODE_LM_PERMISSION_KEY, true);
        }
        return granted;
      }).finally(() => { this.permissionPrompt = null; });
    }
    return this.permissionPrompt;
  }

  async poll() {
    if (this.processing || !this.repoInfo || this.disposed) return;
    const requestDir = path.join(vscodeLmBridgeRoot(), "requests", this.repoInfo.repoId);
    let names;
    try { names = fs.readdirSync(requestDir).filter((name) => VSCODE_LM_REQUEST_ID_RE.test(path.basename(name, ".json")) && name.endsWith(".json")).sort(); }
    catch (_err) { return; }
    if (!names.length) return;
    this.processing = true;
    let claimPath = null;
    try {
      const requestPath = path.join(requestDir, names[0]);
      claimPath = `${requestPath}.claim-${WINDOW_SCOPE_ID}`;
      try { fs.renameSync(requestPath, claimPath); } catch (_err) { return; }
      if (!ownerOnlyRegularFile(claimPath)) throw new Error("vscode_lm_request_not_owner_only");
      const payload = JSON.parse(fs.readFileSync(claimPath, "utf8"));
      const request = validateVscodeLmRequest(payload, this.repoInfo);
      const models = await this.models();
      const model = selectGlm52LanguageModel(models);
      if (!model) throw new Error("vscode_lm_model_not_visible");
      if (!(await this.ensurePermission())) throw new Error("vscode_lm_permission_denied");
      const source = new vscode.CancellationTokenSource();
      const remainingMs = Math.max(1, Date.parse(String(request.deadline)) - Date.now());
      const timer = setTimeout(() => source.cancel(), remainingMs);
      let text = "";
      let error = "";
      try { text = await runVscodeLmAgent(model, request, source.token); }
      catch (err) { error = sanitizeErrorMessage(err); }
      finally { clearTimeout(timer); source.dispose(); }
      atomicWriteOwnerJson(request.responsePath, {
        schema_id: VSCODE_LM_RESPONSE_SCHEMA,
        request_id: request.requestId,
        repo_id: this.repoInfo.repoId,
        model: { id: model.id, family: model.family, name: model.name, vendor: model.vendor, version: model.version },
        text,
        error,
        completed_at: new Date().toISOString(),
      });
    } catch (err) {
      recordSystemLog(`[glm bridge] ERROR ${sanitizeErrorMessage(err)}`);
    } finally {
      if (claimPath) { try { fs.unlinkSync(claimPath); } catch (_err) { /* absent */ } }
      this.processing = false;
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

function pruneSystemLogs(entries) {
  const cutoff = Date.now() - SYSTEM_LOG_RETENTION_MS;
  const retained = entries
    .filter((entry) => Number.isFinite(Date.parse(entry.timestamp)) && Date.parse(entry.timestamp) >= cutoff)
    .slice(0, SYSTEM_LOG_MAX_PERSISTED_ENTRIES);
  while (retained.length > 1 && Buffer.byteLength(JSON.stringify(retained), "utf8") > SYSTEM_LOG_MAX_FILE_BYTES) {
    retained.pop();
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
    appendLine(value);
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

/** Publish a usable runtime pointer before any expensive fingerprint/copy work.
 *
 * VS Code activates extensions concurrently.  Codex may spawn its App Server
 * a few hundred milliseconds after AIWorkHub activation starts.  Computing a
 * full runtime fingerprint and materializing an immutable generation before
 * configuring chatgpt.cliExecutable left a startup window in which Codex
 * permanently started the bundled binary directly.  The extension's packaged
 * runtime is already complete and immutable for the lifetime of this installed
 * extension, so point the stable launcher at it immediately.  The normal
 * materializer replaces this bootstrap pointer atomically with the content-
 * addressed generation later in the same activation.
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

/** Ensure Codex App Server traffic is wrapped by AIWorkHub's transparent,
 * repository-resolving sideband mux.  This is the callback route's actual
 * thread-observation source; a running dispatcher alone cannot discover the
 * active Codex thread.  Apply only when the setting is empty or already
 * AIWorkHub-owned, never overwrite a user's unrelated custom executable.
 *
 * The setting is remote-host global because chatgpt.cliExecutable is an
 * application-scoped OpenAI extension setting.  The mux itself remains
 * repository-safe: it resolves exactly one live repo route from the spawning
 * extension-host PID and transparently passes through when no unique repo is
 * available.  Linux/macOS and Windows use their packaged native launchers.
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

/** Install the stable command name contributed as chatgpt.cliExecutable's
 * default. Manifest defaults are loaded before extension activation, unlike a
 * Remote-SSH ConfigurationTarget.Global write which is not authoritative for
 * an application-scoped setting. Explicit user configuration still wins and
 * is never overwritten. */
function materializePathMuxShim(stableLauncher, options = {}) {
  const platform = options.platform || process.platform;
  const home = options.home || os.homedir();
  const env = options.env || process.env;
  let binDir;
  if (platform === "win32") {
    const normalizedHome = path.resolve(home).toLowerCase();
    const candidates = String(env.PATH || "").split(path.delimiter).filter(Boolean);
    binDir = candidates.find((candidate) => {
      try {
        const resolved = path.resolve(candidate).toLowerCase();
        return resolved === normalizedHome || resolved.startsWith(normalizedHome + path.sep.toLowerCase());
      } catch (_err) {
        return false;
      }
    }) || path.join(home, "AppData", "Local", "Microsoft", "WindowsApps");
  } else {
    binDir = path.join(home, ".local", "bin");
  }
  fs.mkdirSync(binDir, { recursive: true });
  const shim = path.join(
    binDir,
    platform === "win32" ? "aiworkhub-app-server-mux.cmd" : "aiworkhub-app-server-mux",
  );
  if (platform === "win32") {
    const escaped = stableLauncher.replace(/"/g, '""');
    fs.writeFileSync(shim, `@echo off\r\n"${escaped}" %*\r\n`, "utf8");
  } else {
    const escaped = stableLauncher.replace(/'/g, `'\\''`);
    fs.writeFileSync(shim, `#!/bin/sh\nexec '${escaped}' "$@"\n`, { encoding: "utf8", mode: 0o755 });
    fs.chmodSync(shim, 0o755);
  }
  return shim;
}

async function ensureCodexCallbackMuxConfigured(context, launcherOverride = "") {
  const launcherName = process.platform === "win32"
    ? "aiworkhub-app-server-mux.cmd"
    : "aiworkhub-app-server-mux";
  const launcher = launcherOverride || path.join(context.extensionUri.fsPath, "bin", launcherName);
  try {
    const stat = fs.statSync(launcher);
    if (!stat.isFile()) {
      return { ok: false, changed: false, reason: "mux_launcher_not_file" };
    }
  } catch (_err) {
    return { ok: false, changed: false, reason: "mux_launcher_missing" };
  }

  try {
    const config = vscode.workspace.getConfiguration("chatgpt");
    const current = String(config.get("cliExecutable", "") || "").trim();
    const aiworkhubOwned = !current || /aiworkhub-app-server-mux(?:\.cmd)?$/i.test(current);
    if (!aiworkhubOwned) {
      if (outputChannel) outputChannel.appendLine("[codex] callback mux not applied: custom chatgpt.cliExecutable is preserved");
      return { ok: false, changed: false, reason: "custom_cli_executable_preserved" };
    }
    if (path.resolve(current || launcher) === path.resolve(launcher) && current) {
      return { ok: true, changed: false, launcher };
    }
    if (!config || typeof config.update !== "function") {
      return { ok: false, changed: false, reason: "configuration_update_unavailable" };
    }
    await config.update("cliExecutable", launcher, true);
    if (outputChannel) outputChannel.appendLine("[codex] configured stable AIWorkHub App Server callback mux");
    return { ok: true, changed: true, launcher };
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
function repairWorkspaceMcpConfigObject(document, runtimeDir, repoRoot, python) {
  if (!document || typeof document !== "object" || !document.servers || typeof document.servers !== "object") {
    return { document, changed: false };
  }
  let changed = false;
  for (const [name, value] of Object.entries(document.servers)) {
    if (!value || typeof value !== "object") continue;
    const args = Array.isArray(value.args) ? value.args.map(String) : [];
    const isAiWorkHub = name.toLowerCase() === "aiworkhub" || args.includes("aiworkhub.server");
    if (!isAiWorkHub) continue;
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
      document.servers[name] = next;
      changed = true;
    }
  }
  return { document, changed };
}

function ensureWorkspaceMcpConfigsRepaired(context) {
  const folders = vscode.workspace.workspaceFolders || [];
  const runtimeDir = extensionRuntimeDir || resolveExtensionRuntimeDir(context.extensionUri.fsPath);
  let repaired = 0;
  for (const folder of folders) {
    const repoRoot = canonicalRepositoryRoot(folder.uri.fsPath);
    const configPath = path.join(repoRoot, ".vscode", "mcp.json");
    let document;
    try {
      document = JSON.parse(fs.readFileSync(configPath, "utf8"));
    } catch (_err) {
      continue;
    }
    const python = findPythonCommand(repoRoot);
    const result = repairWorkspaceMcpConfigObject(document, runtimeDir, repoRoot, python);
    if (!result.changed) continue;
    try {
      fs.writeFileSync(configPath, `${JSON.stringify(result.document, null, 2)}\n`, "utf8");
      repaired += 1;
    } catch (err) {
      outputChannel.appendLine(`[mcp] failed to repair workspace MCP registration: ${sanitizeErrorMessage(err)}`);
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
    // Manifest identity is enough to persist routing ownership. The Python
    // child remains the sole authority for DB/storage readiness, but waiting
    // for a dashboard snapshot here would leave callbacks unbound during
    // startup -- exactly when the dispatcher needs this window identity.
    if (REPO_ID_RE.test(identity.repoId)) {
      refreshCoordinatorRouteOwnership(identity);
    }
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

async function pushSnapshotOnce(view) {
  const requestSeq = ++view.snapshotRequestSeq;
  let lastError = null;
  const client = getMcpClient();
  view.bindClient(client);
  for (let attempt = 0; attempt < MCP_SNAPSHOT_RECOVERY_ATTEMPTS; attempt += 1) {
    if (attempt > 0) {
      if (client.recovery.open) break;
      await new Promise((resolve) => setTimeout(resolve, MCP_RECOVERY_BACKOFF_MS[attempt - 1]));
    }
    try {
      if (activeRepoIdentity) refreshCoordinatorRouteOwnership(activeRepoIdentity);
      const payload = await client.callTool(DASHBOARD_TOOLS.snapshot, {});
      if (payload && view.stillBoundTo(client) && requestSeq === view.snapshotRequestSeq) {
        view.postMessage({
          type: OUTBOUND_TYPES.snapshot,
          payload: sanitizeWebviewPayload({ ...payload, system_logs: systemLogSnapshot() }),
        });
      } else if (!payload && requestSeq === view.snapshotRequestSeq) {
        view.postMessage({ type: OUTBOUND_TYPES.error, message: "snapshot_unavailable" });
      }
      return;
    } catch (err) {
      lastError = err;
      client.recovery.category = "snapshot";
      client.recovery.reason = sanitizeErrorMessage(err);
      client.recovery.attempts = attempt + 1;
      if (attempt + 1 >= MCP_SNAPSHOT_RECOVERY_ATTEMPTS) client.recovery.open = true;
    }
  }
  if (requestSeq === view.snapshotRequestSeq) {
    view.postMessage({
      type: OUTBOUND_TYPES.offline,
      reason: sanitizeErrorMessage(lastError),
      recovery: client.recoveryStatus(),
    });
  }
}

async function pushSnapshotNoRetry(view) {
  const requestSeq = ++view.snapshotRequestSeq;
  try {
    const client = getMcpClient();
    view.bindClient(client);
    if (activeRepoIdentity) refreshCoordinatorRouteOwnership(activeRepoIdentity);
    const payload = await client.callTool(DASHBOARD_TOOLS.snapshot, {});
    if (payload && view.stillBoundTo(client) && requestSeq === view.snapshotRequestSeq) {
      view.postMessage({
        type: OUTBOUND_TYPES.snapshot,
        payload: sanitizeWebviewPayload({ ...payload, system_logs: systemLogSnapshot() }),
      });
    } else if (!payload && requestSeq === view.snapshotRequestSeq) {
      view.postMessage({ type: OUTBOUND_TYPES.error, message: "snapshot_unavailable" });
    }
  } catch (err) {
    if (requestSeq === view.snapshotRequestSeq) {
      view.postMessage({ type: OUTBOUND_TYPES.offline, reason: sanitizeErrorMessage(err) });
    }
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
    const refreshedRepo = getActiveRepositoryRoot(extensionContext);
    activeRepoLabel = repositoryLabel(vscode.workspace.workspaceFolders || [], refreshedRepo.uriStr);
    activeRepoIdentity = { ...refreshedRepo, label: activeRepoLabel };
    refreshCoordinatorRouteOwnership(activeRepoIdentity);
    if (vscodeLmBridgeHost) {
      await vscodeLmBridgeHost.start(activeRepoIdentity);
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
      pushSnapshot(view);
      break;
    case "retry": {
      const client = getMcpClient();
      client.beginExplicitRecovery();
      pushSnapshot(view);
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

    <button class="header-storage" id="header-storage" type="button" title="Open detailed storage metrics">
      <span class="header-storage-label">Storage</span>
      <strong id="header-storage-managed">Calculating</strong>
      <span id="header-storage-free">Free —</span>
    </button>

    <div class="header-tool-use" id="header-source-graph" title="Authenticated Source Graph use across the latest observed worker task runs">
      <span class="header-storage-label">Source Graph</span>
      <strong id="header-source-graph-rate">—</strong>
      <span id="header-source-graph-detail">No evidence</span>
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

    <section class="activity-peek" aria-label="Repository diagnostics">
      <span class="activity-peek-label">Last log</span>
      <span class="activity-peek-message" id="last-system-log">No system events yet</span>
      <button type="button" id="open-system-log">Logs</button>
      <button type="button" id="open-ai-memory">Memory</button>
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
              <button type="button" role="tab" tabindex="-1" aria-selected="false" aria-controls="panel-tool-use" id="tab-tool-use" data-tab="tool-use">Tool Use</button>
              <button type="button" role="tab" tabindex="-1" aria-selected="false" aria-controls="panel-storage" id="tab-storage" data-tab="storage">Storage</button>
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
          <div class="tab-panel" role="tabpanel" id="panel-tool-use" aria-labelledby="tab-tool-use" hidden>
            <div class="stat-list" id="tool-use-list"></div>
          </div>
          <div class="tab-panel" role="tabpanel" id="panel-storage" aria-labelledby="tab-storage" hidden>
            <div class="stat-list" id="storage-list"></div>
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
    pushRuntimeInfo(panel.__aiworkhubViewState);
  }
  refreshDashboardCommand();
}

async function activate(context) {
  extensionContext = context;
  outputChannel = createManagedOutputChannel();
  context.subscriptions.push(outputChannel);
  vscodeLmBridgeHost = new VscodeLmBridgeHost(context);
  context.subscriptions.push(vscodeLmBridgeHost);

  // Bootstrap the callback topology first.  Nothing expensive may precede
  // these operations: OpenAI's onStartupFinished activation runs concurrently
  // and otherwise wins the spawn race with an unwrapped Codex App Server.
  const stableMuxLauncher = materializeStableMuxLauncher(context);
  materializePathMuxShim(stableMuxLauncher);
  primeStableMuxRuntimePointer(context);
  await ensureCodexCallbackMuxConfigured(context, stableMuxLauncher);

  // Publish this extension-host PID immediately.  A newly launched mux waits
  // on this exact repo/PID binding before it starts the real App Server.
  try {
    const repo = getActiveRepositoryRoot(context);
    activeRepoLabel = repositoryLabel(vscode.workspace.workspaceFolders || [], repo.uriStr);
    activeRepoIdentity = { ...repo, label: activeRepoLabel };
    bindSystemLogRepository(repo.root);
    refreshCoordinatorRouteOwnership(activeRepoIdentity);
  } catch (err) {
    if (err.message === "no_repository_selected") {
      activeRepoLabel = "Select a repository";
    } else {
      activeRepoLabel = "No workspace";
    }
  }

  // The callback launcher and repo route are now available.  Materialize the
  // heavier content-addressed runtime generation without blocking Codex from
  // entering the mux.
  const stableRuntime = materializeStableRuntimeGeneration(context);
  extensionRuntimeDir = stableRuntime.runtimeDir;
  outputChannel.appendLine(`[runtime] using immutable generation ${path.basename(stableRuntime.generationRoot)}`);
  ensureCodexConfigTomlRepaired(context);
  migrateCodexConfigTomlRuntimePath(context);
  ensureCodexManagerGatesRepaired();
  ensureWorkspaceMcpConfigsRepaired(context);

  if (activeRepoIdentity && activeRepoIdentity.root) {
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

  // Startup activation is the callback lifecycle owner.  An initialized
  // repository must start its MCP child and dispatcher even when the user
  // never opens the dashboard during this window/session.
  if (activeRepoIdentity && activeRepoIdentity.root) {
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
}

async function deactivate() {
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
    findPythonCommand,
    _preflightPythonCandidate,
    _buildPreflightDiagnostic,
    getMcpClient,
    sanitizeErrorMessage,
    codexConfigTomlPath,
    resolveCodexConfigTomlPath,
    resolveExtensionRuntimeDir,
    primeStableMuxRuntimePointer,
    materializeStableRuntimeGeneration,
    repairCodexConfigTomlText,
    ensureCodexManagerGatesTomlText,
    ensureCodexCallbackMuxConfigured,
    materializeStableMuxLauncher,
    materializePathMuxShim,
    recordSystemLog,
    systemLogSnapshot,
    clearSystemLogs,
    migrateCodexConfigTomlText,
    migrateCodexConfigTomlRuntimePath,
    repairWorkspaceMcpConfigObject,
    ensureWorkspaceMcpConfigsRepaired,
    splitCodexPythonPathValue,
    ensureCodexConfigTomlRepaired,
    CODEX_OWNED_RUNTIME_SEGMENT_RE,
    windowRouteStatePath,
    removeWindowRouteRecord,
    renewWindowRouteLease,
    startWindowRouteRenewalTimer,
    stopWindowRouteRenewalTimer,
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
    isGlm52LanguageModel,
    selectGlm52LanguageModel,
    validateVscodeLmRequest,
    runVscodeLmAgent,
    VSCODE_LM_PRIVATE_TOOLS,
    constants: {
      MCP_REQUEST_TIMEOUT_MS,
      MCP_SNAPSHOT_RECOVERY_ATTEMPTS,
      MCP_MAX_RUNTIME_REPAIR_ATTEMPTS,
      EXPECTED_MCP_PACKAGE_VERSION,
      WINDOW_ROUTE_LEASE_TTL_MS,
      WINDOW_ROUTE_RENEWAL_INTERVAL_MS,
      STARTUP_ROUTE_CONVERGENCE_INTERVAL_MS,
      STARTUP_ROUTE_CONVERGENCE_MAX_ATTEMPTS,
      APP_SERVER_MUX_SIDEBAND_DIR_ENV,
      REAL_THREAD_ID_RE,
      VSCODE_LM_MODEL,
      VSCODE_LM_REQUEST_SCHEMA,
      VSCODE_LM_HOST_SCHEMA,
      VSCODE_LM_RESPONSE_SCHEMA,
    },
  },
};
