"use strict";

// NF-2026-02287: every dashboard view and action is served by the
// dashboard-only process (`python -m aiworkhub.dashboard_reader`), none by the
// MCP child; the process stops when the dashboard is disposed or hidden, and a
// death is reported to the dashboard and then respawned.
//
// Also exports FakeDashboardChild, the one fake of the dashboard process
// protocol the other fake-spawn suites reuse.

const assert = require("assert");
const childProcess = require("child_process");
const EventEmitter = require("events");
const fs = require("fs");
const Module = require("module");
const os = require("os");
const path = require("path");

const extensionPath = path.resolve(__dirname, "..", "extension.js");
const packageVersion = require("../package.json").version;
const DASHBOARD_READER_MODULE = "aiworkhub.dashboard_reader";
const HIDDEN_STOP_MS = 60 * 1000;

function isDashboardReaderSpawn(args) {
  return Array.isArray(args) && args.includes(DASHBOARD_READER_MODULE);
}

// Speaks the dashboard process line protocol: request {id, tool, arguments},
// reply {id, result}. Behaviors: "live", "slow-snapshot",
// "die-on-first-request".
class FakeDashboardChild extends EventEmitter {
  constructor(spawnRecord, behavior = "live", resultFor = null) {
    super();
    this.spawnRecord = spawnRecord;
    this.behavior = behavior;
    this.resultFor = resultFor;
    this.killed = false;
    this.stdout = new EventEmitter();
    this.stderr = new EventEmitter();
    this.stdin = {
      write: (payload, cb) => {
        for (const line of String(payload).split("\n")) {
          if (!line.trim()) continue;
          this._request(JSON.parse(line));
        }
        if (cb) cb();
      },
    };
  }

  kill() {
    if (this.killed) return;
    this.killed = true;
    setImmediate(() => this.emit("exit", null, "SIGTERM"));
  }

  _reply(id, result) {
    if (this.killed) return;
    this.stdout.emit("data", Buffer.from(`${JSON.stringify({ id, result })}\n`));
  }

  _request(request) {
    if (!Array.isArray(this.spawnRecord.toolCalls)) this.spawnRecord.toolCalls = [];
    this.spawnRecord.toolCalls.push(request.tool);
    if (this.behavior === "die-on-first-request") {
      this.killed = true;
      setImmediate(() => this.emit("exit", 1, null));
      return;
    }
    let result;
    if (this.resultFor) {
      result = this.resultFor(request, this.spawnRecord);
    } else if (request.tool === "aiworkhub_dashboard_snapshot") {
      const repoId = this.spawnRecord.env.AIWORKHUB_REPO_ID;
      result = {
        repo_id: repoId,
        storage: { ready: true },
        tasks: [{ task_id: `task-${String(repoId).slice(-4)}`, repo_id: repoId }],
        summary: { active: 1 },
      };
    } else {
      result = {};
    }
    if (this.behavior === "slow-snapshot" && request.tool === "aiworkhub_dashboard_snapshot") {
      setTimeout(() => this._reply(request.id, result), 30);
    } else {
      this._reply(request.id, result);
    }
  }
}

module.exports = { FakeDashboardChild, isDashboardReaderSpawn, DASHBOARD_READER_MODULE };

// ── The MCP side: a minimal handshake-capable fake that records tools/call ──
class FakeMcpChild extends EventEmitter {
  constructor(spawnRecord) {
    super();
    this.spawnRecord = spawnRecord;
    this.killed = false;
    this.stdout = new EventEmitter();
    this.stderr = new EventEmitter();
    this.stdin = {
      write: (payload, cb) => {
        for (const line of String(payload).split("\n")) {
          if (!line.trim()) continue;
          this._message(JSON.parse(line));
        }
        if (cb) cb();
      },
    };
  }

  kill() {
    if (this.killed) return;
    this.killed = true;
    setImmediate(() => this.emit("exit", null, "SIGTERM"));
  }

  _send(result, id) {
    this.stdout.emit("data", Buffer.from(`${JSON.stringify({ jsonrpc: "2.0", id, result })}\n`));
  }

  _message(message) {
    if (message.method === "initialize") {
      this._send({ protocolVersion: "2024-11-05", capabilities: {}, serverInfo: { name: "fake", version: packageVersion } }, message.id);
      return;
    }
    if (message.method === "notifications/initialized") return;
    if (message.method === "tools/list") {
      this._send({ tools: [
        "aiworkhub_dashboard_snapshot",
        "aiworkhub_dashboard_task_detail",
        "aiworkhub_dashboard_health",
        "aiworkhub_dashboard_task_live_output",
        "aiworkhub_dashboard_memory",
        "aiworkhub_dashboard_sessions",
        "aiworkhub_dashboard_kb",
        "aiworkhub_dashboard_settings",
      ].map((name) => ({ name })) }, message.id);
      return;
    }
    if (message.method !== "tools/call") {
      this._send({}, message.id);
      return;
    }
    const tool = message.params.name;
    this.spawnRecord.toolCalls.push(tool);
    let payload = {};
    if (tool === "aiworkhub_dashboard_health") payload = { ok: true, server_version: packageVersion };
    if (tool === "aiworkhub_dispatcher_ensure_started") payload = { ok: true, dispatcher_started: true };
    if (tool === "aiworkhub_source_graph_ensure_started") payload = { ok: true, daemon_started: true };
    this._send({ content: [{ type: "text", text: JSON.stringify(payload) }] }, message.id);
  }
}

function writeRepo(root, repoId, repoName) {
  fs.mkdirSync(path.join(root, ".aiworkhub"), { recursive: true });
  fs.writeFileSync(
    path.join(root, ".aiworkhub", "project.json"),
    `${JSON.stringify({
      schema_id: "aiworkhub.project_manifest.v1",
      manifest_version: 1,
      layout_version: 1,
      repo_id: repoId,
      repo_name: repoName,
      layout: {
        durable: { tasking: "tasking", source_graph: "source_graph", sessions: "sessions", memory: "memory", kb: "kb", config: "config" },
        runtime: { path: "runtime", durable: false, ignored: true },
      },
    })}\n`,
    "utf8",
  );
}

function loadExtensionHost(repoRoot) {
  delete require.cache[extensionPath];
  const originalLoad = Module._load;
  const fakeVscode = {
    workspace: {
      workspaceFolders: [{ name: path.basename(repoRoot), uri: { fsPath: repoRoot, toString: () => `file://${repoRoot}` } }],
      getConfiguration: () => ({ get: () => "", inspect: () => ({}), update: async () => {} }),
    },
    window: {
      createOutputChannel: () => ({ appendLine: () => {}, dispose: () => {} }),
      setStatusBarMessage: () => {},
      showErrorMessage: () => {},
      showInformationMessage: async () => undefined,
      showWarningMessage: async () => undefined,
      registerWebviewPanelSerializer: () => ({ dispose: () => {} }),
      registerWebviewViewProvider: () => ({ dispose: () => {} }),
      createWebviewPanel: () => null,
    },
    commands: { registerCommand: () => ({ dispose: () => {} }) },
    Uri: { joinPath: (...parts) => ({ fsPath: parts.map((p) => p.fsPath || p).join("/") }) },
    ViewColumn: { Active: 1 },
    ConfigurationTarget: { Global: 1 },
  };
  Module._load = function patchedLoad(request, parent, isMain) {
    if (request === "vscode") return fakeVscode;
    return originalLoad.call(this, request, parent, isMain);
  };
  try {
    const extension = require(extensionPath);
    const context = {
      extensionUri: { fsPath: path.resolve(__dirname, "..") },
      extension: { packageJSON: { version: packageVersion } },
      subscriptions: [],
      workspaceState: { update: () => {}, get: () => undefined },
    };
    return { extension, context };
  } finally {
    Module._load = originalLoad;
  }
}

const settle = (ms = 20) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "aiworkhub-nf02287-"));
  const repoRoot = path.join(tmp, "repo");
  fs.mkdirSync(repoRoot);
  const repoId = `repo_${"02287".padEnd(32, "0")}`;
  writeRepo(repoRoot, repoId, "nf02287");

  const mcpSpawns = [];
  const dashboardSpawns = [];
  const dashboardBehaviors = [];
  const okResult = (request, spawnRecord) => (request.tool === "aiworkhub_dashboard_snapshot"
    ? { repo_id: spawnRecord.env.AIWORKHUB_REPO_ID, storage: { ready: true }, tasks: [], summary: {} }
    : { ok: true });
  const originalSpawn = childProcess.spawn;
  const originalExecFile = childProcess.execFile;
  const originalSpawnSync = childProcess.spawnSync;
  childProcess.spawn = (cmd, args, options) => {
    const spawnRecord = { cmd, args, cwd: options.cwd, env: options.env, shell: options.shell, toolCalls: [] };
    if (isDashboardReaderSpawn(args)) {
      dashboardSpawns.push(spawnRecord);
      const child = new FakeDashboardChild(spawnRecord, dashboardBehaviors.shift() || "live", okResult);
      spawnRecord.child = child;
      return child;
    }
    mcpSpawns.push(spawnRecord);
    return new FakeMcpChild(spawnRecord);
  };
  childProcess.execFile = (_cmd, _args, _options, callback) => { setImmediate(() => callback(null, "", "")); };
  childProcess.spawnSync = () => ({ status: 0, stdout: "", stderr: "", signal: null });

  const host = loadExtensionHost(repoRoot);
  const internals = host.extension.__testInternals;
  try {
    await host.extension.activate(host.context);

    // ── 1. every dashboard tool goes to the dashboard process, none to MCP ──
    const messages = [];
    const view = new internals.ViewState((message) => messages.push(message));
    view.setVisible(true);
    await internals.pushSnapshot(view);
    assert.ok(messages.some((m) => m.type === "snapshot"), JSON.stringify(messages));
    await internals.pushSettings(view);
    await internals.pushTaskDetail(view, "task-nf02287");
    await internals.pushLiveOutput(view, "task-nf02287", 0);
    await internals.pushMemory(view);
    await internals.pushSessions(view);
    await internals.pushKb(view);
    await internals.pushNeedfixList(view, {});
    await internals.pushNeedfixDetail(view, "NF-2026-00001");
    await internals.runNeedfixAction(view, "transition", { needfix_id: "NF-2026-00001", action: "triage", confirm: true });
    await internals.runNeedfixAction(view, "capture", { title: "t", description: "d" });
    await internals.pushRoadmapList(view, {});
    await internals.pushRoadmapDetail(view, "RM-2026-00001");
    await internals.pushHubMail(view, {});
    await internals.pushHubMailDetail(view, "0".repeat(32));
    await internals.runHubMailAction(view, "peers", { action: "requests" });
    await internals.updateFeatureSetting(view, "source_graph", true, 1);
    await internals.updateModelSetting(view, "codex", "", "", true, 1);
    await internals.updateSourceGraphLanguage(view, "python", true, 1);
    await internals.runStorageCleanup(view);
    await internals.runTerminalLogCleanup(view);
    await internals.pushInitializeStorage(view);
    await settle(50);

    const servedByDashboard = new Set(dashboardSpawns.flatMap((spawn) => spawn.toolCalls));
    for (const tool of [
      "aiworkhub_dashboard_snapshot",
      "aiworkhub_dashboard_settings",
      "aiworkhub_dashboard_task_detail",
      "aiworkhub_dashboard_task_live_output",
      "aiworkhub_dashboard_memory",
      "aiworkhub_dashboard_sessions",
      "aiworkhub_dashboard_kb",
      "aiworkhub_dashboard_needfix_list",
      "aiworkhub_dashboard_needfix_detail",
      "aiworkhub_dashboard_needfix_transition",
      "aiworkhub_dashboard_needfix_capture",
      "aiworkhub_dashboard_roadmap_list",
      "aiworkhub_dashboard_roadmap_detail",
      "aiworkhub_dashboard_hub_mail",
      "aiworkhub_dashboard_hub_mail_detail",
      "aiworkhub_dashboard_hub_mail_peers",
      "aiworkhub_dashboard_settings_update",
      "aiworkhub_dashboard_model_settings_update",
      "aiworkhub_dashboard_source_graph_settings_update",
      "aiworkhub_dashboard_storage_retention_preview",
      "aiworkhub_dashboard_terminal_log_retention_preview",
      "aiworkhub_dashboard_initialize",
    ]) {
      assert.ok(servedByDashboard.has(tool), `${tool} did not reach the dashboard process`);
      assert.ok(internals.DASHBOARD_PROCESS_TOOLS.has(tool), `${tool} missing from the dashboard allowlist`);
    }
    const mcpDashboardCalls = mcpSpawns.flatMap((spawn) => spawn.toolCalls)
      .filter((tool) => internals.DASHBOARD_PROCESS_TOOLS.has(tool));
    assert.deepStrictEqual(mcpDashboardCalls, [], "a dashboard tool was routed to the MCP client");
    for (const spawn of dashboardSpawns) {
      assert.deepStrictEqual(spawn.args.slice(-4), ["-m", DASHBOARD_READER_MODULE, "--repo", spawn.env.AIWORKHUB_REPO_ROOT]);
      assert.ok(!spawn.args.includes("aiworkhub.server"));
      assert.strictEqual(spawn.shell, false);
      for (const key of ["AIWORKHUB_MCP_SURFACE", "AIWORKHUB_MCP_STDIO_BACKEND", "AIWORKHUB_ALLOW_WRITES", "AIWORKHUB_ALLOW_LAUNCH"]) {
        assert.strictEqual(spawn.env[key], undefined, `${key} leaked into the dashboard process`);
      }
    }
    const client = internals.getDashboardClient(host.context);
    await assert.rejects(client.callTool("aiworkhub_dispatcher_ensure_started", {}), /dashboard_tool_not_allowed/);
    await assert.rejects(client.callTool("aiworkhub_dashboard_task_restart", {}), /dashboard_tool_not_allowed/);

    // ── 2. stopped when disposed, respawned on the next show ──────────────
    const liveBeforeDispose = internals.dashboardProcessClientForTest();
    assert.ok(liveBeforeDispose && liveBeforeDispose.running, "dashboard process should be live while shown");
    const childBeforeDispose = liveBeforeDispose.child;
    view.dispose();
    assert.strictEqual(childBeforeDispose.killed, true, "dispose must stop the dashboard process");
    assert.strictEqual(liveBeforeDispose.running, false);
    const spawnsBeforeShow = dashboardSpawns.length;
    const shownMessages = [];
    const shown = new internals.ViewState((message) => shownMessages.push(message));
    shown.setVisible(true);
    await internals.pushSnapshot(shown);
    assert.strictEqual(dashboardSpawns.length, spawnsBeforeShow + 1, "next show must respawn the dashboard process");
    assert.ok(shownMessages.some((m) => m.type === "snapshot"));

    // Hidden for longer than the bound: the armed stop timer fires.
    const originalSetTimeout = global.setTimeout;
    const hiddenStops = [];
    global.setTimeout = (fn, ms, ...rest) => {
      if (ms === HIDDEN_STOP_MS) {
        hiddenStops.push(fn);
        return { unref() {} };
      }
      return originalSetTimeout(fn, ms, ...rest);
    };
    try {
      shown.setVisible(false);
    } finally {
      global.setTimeout = originalSetTimeout;
    }
    assert.strictEqual(hiddenStops.length, 1, "hiding must arm exactly one bounded stop");
    const hiddenChild = internals.dashboardProcessClientForTest().child;
    hiddenStops[0]();
    assert.strictEqual(hiddenChild.killed, true, "hidden > 60 s must stop the dashboard process");
    shown.setVisible(true);
    await internals.pushSnapshot(shown);
    assert.strictEqual(dashboardSpawns.length, spawnsBeforeShow + 2, "show after a hidden stop must respawn");

    // ── 3. death is reported to the dashboard, then respawned ─────────────
    shown.dispose();
    const deathMessages = [];
    const dying = new internals.ViewState((message) => deathMessages.push(message));
    dying.setVisible(true);
    dashboardBehaviors.push("die-on-first-request", "die-on-first-request");
    const spawnsBeforeDeath = dashboardSpawns.length;
    await internals.pushSnapshot(dying);
    const offline = deathMessages.find((m) => m.type === "offline");
    assert.ok(offline, `death was not reported: ${JSON.stringify(deathMessages)}`);
    assert.ok(/^dashboard_process_/.test(offline.reason), offline.reason);
    assert.ok(!deathMessages.some((m) => m.type === "snapshot"));
    assert.strictEqual(dashboardSpawns.length, spawnsBeforeDeath + 2, "first death respawns immediately, then backs off");
    // After the backoff the next refresh respawns and the dashboard recovers.
    await settle(1100);
    await internals.pushSnapshot(dying);
    assert.strictEqual(dashboardSpawns.length, spawnsBeforeDeath + 3);
    assert.ok(deathMessages.some((m) => m.type === "snapshot"), "dashboard did not recover after respawn");
    const lateMcpDashboardCalls = mcpSpawns.flatMap((spawn) => spawn.toolCalls)
      .filter((tool) => internals.DASHBOARD_PROCESS_TOOLS.has(tool));
    assert.deepStrictEqual(lateMcpDashboardCalls, [], "a failing dashboard process must never fall back to MCP");
    dying.dispose();

    await host.extension.deactivate();
    assert.strictEqual(internals.dashboardProcessClientForTest(), null, "deactivate must dispose the dashboard process");
  } finally {
    childProcess.spawn = originalSpawn;
    childProcess.execFile = originalExecFile;
    childProcess.spawnSync = originalSpawnSync;
  }
  console.log("NF-2026-02287 dashboard process isolation passed");
}

if (require.main === module) {
  main().then(() => process.exit(0)).catch((err) => {
    console.error(err);
    process.exit(1);
  });
}
