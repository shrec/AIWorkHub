// B893: reloadless, repository-isolated runtime repair.
//
// A detected MCP runtime version/capability mismatch must be fixed by a
// bounded restart of THIS window's own repo-bound MCP child, with the
// already-open dashboard tab reconnecting automatically -- never a manual
// "Developer: Reload Window" instruction, never a fallback to a different
// repository or a host-global runtime. This file exercises
// __testInternals.pushRuntimeInfo / McpStdioClient.attemptRuntimeRepair
// directly (no real VS Code, no media/app.js dependency) and the
// findPythonCommand platform branches deterministically for win32/darwin/
// linux in a single Node process.

const assert = require("assert");
const childProcess = require("child_process");
const EventEmitter = require("events");
const fs = require("fs");
const Module = require("module");
const os = require("os");
const path = require("path");

const extensionPath = path.resolve(__dirname, "..", "extension.js");
const EXPECTED_VERSION = require("../package.json").version;

function writeRepo(root, repoId, repoName) {
  fs.mkdirSync(path.join(root, ".aiworkhub"), { recursive: true });
  fs.writeFileSync(
    path.join(root, ".aiworkhub", "project.json"),
    JSON.stringify({ repo_id: repoId, repo_name: repoName }) + "\n",
    "utf8",
  );
}

// A fake stdio child whose health/tools-list responses are driven by a
// per-repo-root FIFO queue of "generations" -- each spawn of that repo root
// pulls the next generation off the queue, so a test can script "stale on
// first boot, healthy after the bounded repair restart" deterministically.
class FakeChild extends EventEmitter {
  constructor(spawnRecord, generation) {
    super();
    this.spawnRecord = spawnRecord;
    this.generation = generation || { version: EXPECTED_VERSION, missingTools: [], dieBeforeInitialize: false };
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
    this.stdout.emit("data", Buffer.from(JSON.stringify({ jsonrpc: "2.0", id, result }) + "\n"));
  }

  _message(message) {
    this.spawnRecord.requests.push({
      method: message.method,
      tool: message.params && message.params.name,
    });
    if (this.generation.dieBeforeInitialize && message.method === "initialize") {
      this.kill();
      return;
    }
    if (message.method === "initialize") {
      this._send({ protocolVersion: "2024-11-05", capabilities: {}, serverInfo: { name: "fake" } }, message.id);
      return;
    }
    if (message.method === "notifications/initialized") {
      return;
    }
    if (message.method === "tools/list") {
      const names = [
        "aiworkhub_dashboard_snapshot",
        "aiworkhub_dashboard_task_detail",
        "aiworkhub_dashboard_health",
        "aiworkhub_dashboard_task_live_output",
        "aiworkhub_dashboard_memory",
        "aiworkhub_dashboard_sessions",
        "aiworkhub_dashboard_kb",
        "aiworkhub_dashboard_settings",
      ].filter((name) => !this.generation.missingTools.includes(name));
      this._send({ tools: names.map((name) => ({ name })) }, message.id);
      return;
    }
    if (message.method !== "tools/call") {
      this._send({}, message.id);
      return;
    }
    const tool = message.params.name;
    if (tool === "aiworkhub_dashboard_snapshot") {
      const repoId = this.spawnRecord.env.AIWORKHUB_REPO_ID;
      this._send({
        content: [{ type: "text", text: JSON.stringify({ repo_id: repoId, storage: { ready: true }, tasks: [], summary: {} }) }],
      }, message.id);
    } else if (tool === "aiworkhub_dashboard_health") {
      this._send({
        content: [{ type: "text", text: JSON.stringify({ ok: true, server_version: this.generation.version }) }],
      }, message.id);
    } else if (tool === "aiworkhub_dispatcher_ensure_started" || tool === "aiworkhub_dispatcher_stop") {
      this._send({ content: [{ type: "text", text: JSON.stringify({ ok: true, dispatcher_started: true }) }] }, message.id);
    } else {
      this._send({ content: [{ type: "text", text: "{}" }] }, message.id);
    }
  }
}

function loadExtensionHost(repoRoot, pythonPath) {
  delete require.cache[extensionPath];
  const originalLoad = Module._load;
  const fakeVscode = {
    workspace: {
      workspaceFolders: [{ name: path.basename(repoRoot), uri: { fsPath: repoRoot, toString: () => `file://${repoRoot}` } }],
      getConfiguration: () => ({ get: (key) => (key === "pythonPath" ? (pythonPath || "") : ""), inspect: () => ({}), update: async () => {} }),
    },
    window: {
      createOutputChannel: () => ({ appendLine: () => {}, dispose: () => {} }),
      setStatusBarMessage: () => {},
      showErrorMessage: () => {},
      showInformationMessage: () => {},
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
      extension: { packageJSON: { version: EXPECTED_VERSION } },
      subscriptions: [],
      workspaceState: { update: () => {}, get: () => undefined },
    };
    return { extension, context };
  } finally {
    Module._load = originalLoad;
  }
}

function installSpawnFake(generationsByRoot) {
  const spawns = [];
  const original = childProcess.spawn;
  const originalExecFile = childProcess.execFile;
  // extension.js canonicalizes repository roots with realpath before
  // spawning.  macOS commonly rewrites /var -> /private/var and Windows may
  // normalize drive casing, so key the scripted generations by the same
  // physical identity instead of accidentally returning the default healthy
  // generation on non-Linux runners.
  const canonicalGenerations = new Map(
    [...generationsByRoot.entries()].map(([root, generations]) => [
      path.normalize(fs.realpathSync.native(root)),
      generations,
    ]),
  );
  childProcess.spawn = (cmd, args, options) => {
    const spawnRecord = {
      cmd,
      args,
      env: options.env,
      shell: options.shell,
      windowsHide: options.windowsHide,
      repoRoot: options.env.AIWORKHUB_REPO_ROOT,
      repoId: options.env.AIWORKHUB_REPO_ID,
      windowId: options.env.AIWORKHUB_WINDOW_ID,
      requests: [],
    };
    spawns.push(spawnRecord);
    const canonicalRoot = path.normalize(fs.realpathSync.native(spawnRecord.repoRoot));
    const queue = canonicalGenerations.get(canonicalRoot) || [];
    const generation = queue.shift() || { version: EXPECTED_VERSION, missingTools: [], dieBeforeInitialize: false };
    return new FakeChild(spawnRecord, generation);
  };
  childProcess.execFile = (_cmd, _args, _options, callback) => {
    setImmediate(() => callback(null, "", ""));
  };
  return {
    spawns,
    restore: () => {
      childProcess.execFile = originalExecFile;
      childProcess.spawn = original;
    },
  };
}

// A real ViewState (not a bare {postMessage} stub) -- pushSnapshot/
// pushRuntimeInfo rely on its bindClient/stillBoundTo/snapshotRequestSeq
// bookkeeping, exactly like the live openDashboardCommand/reviveDashboardPanel
// paths use.
function makeView(host) {
  const messages = [];
  const view = new host.extension.__testInternals.ViewState((m) => messages.push(m));
  return { view, messages };
}

async function testSelfHealsAndReconnectsWithoutReload(tmp) {
  const repoRoot = path.join(tmp, "self-heal");
  fs.mkdirSync(repoRoot);
  writeRepo(repoRoot, "repo_selfheal00000000000000000000001", "self-heal");

  const generations = new Map([[repoRoot, [
    { version: "0.6.18", missingTools: [], dieBeforeInitialize: false }, // initial boot: stale
    { version: EXPECTED_VERSION, missingTools: [], dieBeforeInitialize: false }, // post-repair: healthy
  ]]]);
  const fake = installSpawnFake(generations);
  try {
    const host = loadExtensionHost(repoRoot);
    await host.extension.activate(host.context);
    const { view, messages } = makeView(host);
    await host.extension.__testInternals.pushRuntimeInfo(view);

    assert.strictEqual(fake.spawns.length, 2, "expected exactly one bounded repair restart (2 spawns total)");
    assert.strictEqual(fake.spawns[0].repoRoot, fake.spawns[1].repoRoot, "repair must restart the SAME repository, never a different one");
    assert.strictEqual(fake.spawns[0].repoId, fake.spawns[1].repoId, "repair must never rotate repo_id");

    const runtimeMsgs = messages.filter((m) => m.type === "runtimeInfo");
    const last = runtimeMsgs[runtimeMsgs.length - 1];
    assert.strictEqual(last.payload.reloadRequired, false, "must never instruct a manual reload");
    assert.strictEqual(last.payload.degraded, false);
    // Current lifecycle repairs stale runtimes during ensureStarted(), before
    // the dashboard asks for runtimeInfo. Older behavior reported the repair
    // from pushRuntimeInfo itself. Both are valid as long as the user sees a
    // healthy runtime without a manual reload.
    assert.strictEqual(Boolean(last.payload.repaired), false);
    assert.strictEqual(Boolean(last.payload.repairAttempted), false);
    assert.strictEqual(last.payload.runtimeVersion, EXPECTED_VERSION);

    await host.extension.__testInternals.pushSnapshot(view);
    const snapshotMsg = messages.find((m) => m.type === "snapshot");
    assert.ok(snapshotMsg, "an already-open dashboard tab must reconnect automatically after startup repair");
    assert.strictEqual(snapshotMsg.payload.repo_id, "repo_selfheal00000000000000000000001");

    await host.extension.deactivate();
  } finally {
    fake.restore();
  }
}

async function testRuntimeInfoReusesHandshakeEvidenceDuringBackgroundConvergence(tmp) {
  const repoRoot = path.join(tmp, "preflight-cache");
  fs.mkdirSync(repoRoot);
  writeRepo(repoRoot, "repo_preflightcache000000000000000006", "preflight-cache");

  const fake = installSpawnFake(new Map([[repoRoot, [
    { version: EXPECTED_VERSION, missingTools: [], dieBeforeInitialize: false },
  ]]]));
  try {
    const host = loadExtensionHost(repoRoot);
    await host.extension.activate(host.context);
    assert.strictEqual(fake.spawns.length, 0, "safe activation must not start an MCP child before dashboard demand");
    const client = host.extension.__testInternals.getMcpClient(host.context);
    await client.ensureStarted();
    assert.strictEqual(
      fake.spawns.length,
      1,
      `explicit startup unexpectedly spawned ${fake.spawns.length} children: ${JSON.stringify(client.runtimePreflight)}`,
    );
    assert.strictEqual(client.running, true, "healthy activation must keep its child running");
    assert.strictEqual(fake.spawns[0].shell, false, "MCP child must never use a command shell");
    assert.strictEqual(fake.spawns[0].windowsHide, true, "MCP child must not create a Windows console window");
    assert.strictEqual(
      host.extension.__testInternals.getMcpClient(host.context),
      client,
      "runtime info must resolve the same repo-bound client that completed the handshake",
    );
    const { view, messages } = makeView(host);
    await host.extension.__testInternals.pushRuntimeInfo(view);

    assert.strictEqual(fake.spawns.length, 1, "a healthy handshaken child must never be restarted by a redundant dashboard probe");
    assert.strictEqual(
      fake.spawns[0].requests.filter((request) => request.method === "tools/list").length,
      1,
      "runtime info must reuse the handshake tools/list evidence",
    );
    assert.strictEqual(
      fake.spawns[0].requests.filter(
        (request) => request.method === "tools/call" && request.tool === "aiworkhub_dashboard_health",
      ).length,
      1,
      "runtime info must reuse the handshake health evidence",
    );
    assert.ok(client.runtimePreflight, "runtime info must join startup and retain handshake evidence");
    assert.strictEqual(client.runtimePreflight.matches, true, "handshake evidence must match the packaged runtime");
    const last = messages.filter((m) => m.type === "runtimeInfo").pop();
    assert.ok(last, "runtimeInfo must be emitted from cached handshake evidence");
    assert.strictEqual(last.payload.degraded, false);
    assert.strictEqual(last.payload.runtimeVersion, EXPECTED_VERSION);
    assert.strictEqual(client.recoveryStatus().open, false);

    await host.extension.deactivate();
  } finally {
    fake.restore();
  }
}

async function testExplicitRetryAlwaysReplacesWindowOwnedChild(tmp) {
  const repoRoot = path.join(tmp, "explicit-window-replace");
  fs.mkdirSync(repoRoot);
  writeRepo(repoRoot, "repo_explicitreplace00000000000000007", "explicit-window-replace");
  const fake = installSpawnFake(new Map([[repoRoot, [
    { version: EXPECTED_VERSION, missingTools: [], dieBeforeInitialize: false },
    { version: EXPECTED_VERSION, missingTools: [], dieBeforeInitialize: false },
  ]]]));
  try {
    const host = loadExtensionHost(repoRoot);
    await host.extension.activate(host.context);
    const client = host.extension.__testInternals.getMcpClient(host.context);
    await client.ensureStarted();
    const firstChild = client.lifecycleChild;
    const result = await client.replaceForExplicitRecovery();

    assert.strictEqual(fake.spawns.length, 2);
    assert.notStrictEqual(client.lifecycleChild, firstChild);
    assert.strictEqual(firstChild.killed, true);
    assert.strictEqual(result.replaced, true);
    assert.strictEqual(result.phase, "ready");
    assert.strictEqual(client.running, true);
    assert.strictEqual(client.initialized, true);
    await host.extension.deactivate();
  } finally {
    fake.restore();
  }
}

async function testHandshakeFailuresExposeExactPhase(tmp) {
  const repoRoot = path.join(tmp, "phase-diagnostics");
  fs.mkdirSync(repoRoot);
  writeRepo(repoRoot, "repo_phasediagnostics000000000000000008", "phase-diagnostics");
  const host = loadExtensionHost(repoRoot);
  const client = new host.extension.__testInternals.McpStdioClient(
    repoRoot,
    { appendLine: () => {} },
    { repoId: "repo_phasediagnostics000000000000000008" },
    "claim-phase",
  );
  client.request = async () => { throw new Error("mcp_request_timeout"); };
  await assert.rejects(client._handshake(), /mcp_initialize_failed:mcp_request_timeout/);

  client.request = async (method) => {
    if (method === "tools/list") return { tools: [] };
    throw new Error("mcp_request_timeout");
  };
  await assert.rejects(
    client._assertRuntimeVersionBeforeServices(),
    /mcp_dashboard_health_failed:mcp_request_timeout/,
  );

  client.request = async () => { throw new Error("mcp_request_timeout"); };
  await assert.rejects(
    client._assertRuntimeVersionBeforeServices(),
    /mcp_tools_list_failed:mcp_request_timeout/,
  );
}

async function testBoundedRetryOnPersistentMismatch(tmp) {
  const repoRoot = path.join(tmp, "persistent-mismatch");
  fs.mkdirSync(repoRoot);
  writeRepo(repoRoot, "repo_persist000000000000000000000002", "persistent-mismatch");

  const generations = new Map([[repoRoot, [
    { version: "0.6.10", missingTools: [], dieBeforeInitialize: false },
    { version: "0.6.10", missingTools: [], dieBeforeInitialize: false },
    { version: "0.6.10", missingTools: [], dieBeforeInitialize: false },
    { version: "0.6.10", missingTools: [], dieBeforeInitialize: false },
  ]]]);
  const fake = installSpawnFake(generations);
  try {
    const host = loadExtensionHost(repoRoot);
    await host.extension.activate(host.context);
    const { view, messages } = makeView(host);

    await host.extension.__testInternals.pushRuntimeInfo(view);
    await host.extension.__testInternals.pushRuntimeInfo(view);
    await host.extension.__testInternals.pushRuntimeInfo(view);
    assert.strictEqual(fake.spawns.length, 4, "a persistent mismatch spends exactly three bounded repair attempts");
    let last = messages.filter((m) => m.type === "runtimeInfo").pop();
    assert.strictEqual(last.payload.reloadRequired, false);
    assert.strictEqual(last.payload.degraded, true);
    assert.ok(
      last.payload.reason.includes("mismatch") || last.payload.reason.includes("runtime_repair_budget_exhausted"),
      last.payload.reason,
    );

    // A later mismatch check must NOT spawn another child -- the bounded budget for this mismatch episode is
    // already spent, and it must degrade visibly instead of looping forever.
    await host.extension.__testInternals.pushRuntimeInfo(view);
    assert.strictEqual(fake.spawns.length, 4, "exhausted repair budget must never spawn again for the same mismatch episode");
    last = messages.filter((m) => m.type === "runtimeInfo").pop();
    assert.strictEqual(last.payload.reloadRequired, false);
    assert.strictEqual(last.payload.degraded, true);
    assert.strictEqual(last.payload.repairAttempted, false);
    assert.ok(last.payload.reason.includes("runtime_repair_budget_exhausted") || last.payload.reason.includes("mismatch"));

    await host.extension.deactivate();
  } finally {
    fake.restore();
  }
}

async function testFailedRestartDegradesWithoutCrossRepoFallback(tmp) {
  const repoRoot = path.join(tmp, "restart-fails");
  fs.mkdirSync(repoRoot);
  writeRepo(repoRoot, "repo_restartfail0000000000000000003", "restart-fails");

  const generations = new Map([[repoRoot, [
    { version: "0.6.11", missingTools: [], dieBeforeInitialize: false },
    { version: EXPECTED_VERSION, missingTools: [], dieBeforeInitialize: true }, // the repair restart itself fails to handshake
    { version: EXPECTED_VERSION, missingTools: [], dieBeforeInitialize: true },
    { version: EXPECTED_VERSION, missingTools: [], dieBeforeInitialize: true },
  ]]]);
  const fake = installSpawnFake(generations);
  try {
    const host = loadExtensionHost(repoRoot);
    await host.extension.activate(host.context);
    const { view, messages } = makeView(host);
    await host.extension.__testInternals.pushRuntimeInfo(view);

    assert.ok(fake.spawns.length <= 4, "failed recovery must remain within the three-attempt episode");
    const last = messages.filter((m) => m.type === "runtimeInfo").pop();
    assert.strictEqual(last.payload.reloadRequired, false, "a failed restart must never fall back to a manual reload instruction");
    assert.strictEqual(last.payload.degraded, true);
    assert.ok(last.payload.attempts >= 1, `failed repair must expose spent attempts, got ${last.payload.attempts}`);
    assert.strictEqual(last.payload.maxAttempts, 3);
    assert.ok(typeof last.payload.reason === "string" && last.payload.reason.length > 0, "a failed restart must surface a readable degraded reason");
    // Never silently attaches another repo: the bound client is still keyed
    // to this exact repository root/id after the failed repair.
    const client = host.extension.__testInternals.getMcpClient(host.context);
    assert.strictEqual(client.repositoryRoot, fs.realpathSync.native(repoRoot));
    assert.strictEqual(client.repositoryIdentity.repoId, "repo_restartfail0000000000000000003");

    await host.extension.deactivate();
  } finally {
    fake.restore();
  }
}

function testExplicitRetryPreservesPartialRuntimeRepairBudget(tmp) {
  const repoRoot = path.join(tmp, "retry-budget");
  fs.mkdirSync(repoRoot);
  writeRepo(repoRoot, "repo_retrybudget00000000000000000007", "retry-budget");
  const host = loadExtensionHost(repoRoot);
  const client = new host.extension.__testInternals.McpStdioClient(
    repoRoot,
    { repoId: "repo_retrybudget00000000000000000007", repoName: "retry-budget" },
    { appendLine: () => {} },
    { claimEpisode: "episode_retry_budget" },
  );

  client.runtimeRepairAttempts = 1;
  client.beginExplicitRecovery();
  assert.strictEqual(client.runtimeRepairAttempts, 1, "manual retry must continue a partially-spent repair episode");

  client.runtimeRepairAttempts = 3;
  client.runtimeRepairBlockedReason = "runtime_repair_budget_exhausted:test";
  client.beginExplicitRecovery();
  assert.strictEqual(client.runtimeRepairAttempts, 0, "manual retry may start fresh only after bounded exhaustion");
  assert.strictEqual(client.runtimeRepairBlockedReason, "");
}

async function testTwoWorkspacesRepairInIsolation(tmp) {
  const repoA = path.join(tmp, "iso-alpha");
  const repoB = path.join(tmp, "iso-beta");
  fs.mkdirSync(repoA);
  fs.mkdirSync(repoB);
  writeRepo(repoA, "repo_isoalpha000000000000000000004", "iso-alpha");
  writeRepo(repoB, "repo_isobeta0000000000000000000005", "iso-beta");

  const generations = new Map([
    [repoA, [
      { version: "0.6.5", missingTools: [], dieBeforeInitialize: false },
      { version: EXPECTED_VERSION, missingTools: [], dieBeforeInitialize: false },
    ]],
    [repoB, [
      { version: EXPECTED_VERSION, missingTools: [], dieBeforeInitialize: false },
    ]],
  ]);
  const fake = installSpawnFake(generations);
  try {
    const hostA = loadExtensionHost(repoA);
    const hostB = loadExtensionHost(repoB);
    await hostA.extension.activate(hostA.context);
    await hostB.extension.activate(hostB.context);

    const a = makeView(hostA);
    const b = makeView(hostB);
    await hostA.extension.__testInternals.pushRuntimeInfo(a.view);
    await hostB.extension.__testInternals.pushRuntimeInfo(b.view);

    const spawnsForA = fake.spawns.filter((s) => s.repoRoot === fs.realpathSync.native(repoA));
    const spawnsForB = fake.spawns.filter((s) => s.repoRoot === fs.realpathSync.native(repoB));
    assert.strictEqual(spawnsForA.length, 2, "repo A's mismatch must trigger its own bounded repair");
    assert.strictEqual(spawnsForB.length, 1, "repo B was already healthy and must never be restarted by A's repair");
    assert.notStrictEqual(spawnsForA[0].repoId, spawnsForB[0].repoId);
    assert.notStrictEqual(spawnsForA[0].windowId, spawnsForB[0].windowId);

    const lastA = a.messages.filter((m) => m.type === "runtimeInfo").pop();
    const lastB = b.messages.filter((m) => m.type === "runtimeInfo").pop();
    assert.strictEqual(lastA.payload.degraded, false);
    assert.strictEqual(lastA.payload.runtimeVersion, EXPECTED_VERSION);
    assert.strictEqual(lastB.payload.repairAttempted, false);
    assert.strictEqual(lastB.payload.degraded, false);

    await hostA.extension.deactivate();
    await hostB.extension.deactivate();
  } finally {
    fake.restore();
  }
}

// ── findPythonCommand: deterministic Linux / Windows / macOS branches ──────
function withPlatform(value, fn) {
  const original = Object.getOwnPropertyDescriptor(process, "platform");
  Object.defineProperty(process, "platform", { value, configurable: true });
  try {
    return fn();
  } finally {
    Object.defineProperty(process, "platform", original);
  }
}

function withExistsSyncStub(existingPaths, fn) {
  const original = fs.existsSync;
  fs.existsSync = (candidate) => existingPaths.has(candidate);
  try {
    return fn();
  } finally {
    fs.existsSync = original;
  }
}

function testPlatformPythonResolution(tmp) {
  const repoRoot = path.join(tmp, "platform-repo");
  fs.mkdirSync(repoRoot, { recursive: true });
  const host = loadExtensionHost(repoRoot);
  const findPythonCommand = host.extension.__testInternals.findPythonCommand;

  // Windows: prefers the venv's Scripts\python.exe when present.
  const originalSpawnSync = childProcess.spawnSync;
  childProcess.spawnSync = () => ({ status: 0, stderr: "", signal: null });
  try {
    withPlatform("win32", () => {
      const winVenvPython = path.join(repoRoot, ".venv", "Scripts", "python.exe");
      withExistsSyncStub(new Set([winVenvPython]), () => {
        const resolved = findPythonCommand(repoRoot);
        assert.strictEqual(resolved.command, winVenvPython);
        assert.deepStrictEqual(resolved.argsPrefix, []);
      });
      // No venv present: deterministic `py -3` fallback, never a bare `python`.
      withExistsSyncStub(new Set(), () => {
        const resolved = findPythonCommand(repoRoot);
        assert.strictEqual(resolved.command, "py");
        assert.deepStrictEqual(resolved.argsPrefix, ["-3"]);
      });
    });
  } finally {
    childProcess.spawnSync = originalSpawnSync;
  }

  // macOS and Linux share the same POSIX venv layout and fallback.
  for (const platform of ["darwin", "linux"]) {
    withPlatform(platform, () => {
      const posixVenvPython3 = path.join(repoRoot, ".venv", "bin", "python3");
      withExistsSyncStub(new Set([posixVenvPython3]), () => {
        const resolved = findPythonCommand(repoRoot);
        assert.strictEqual(resolved.command, posixVenvPython3);
        assert.deepStrictEqual(resolved.argsPrefix, []);
      });
      withExistsSyncStub(new Set(), () => {
        const resolved = findPythonCommand(repoRoot);
        assert.deepStrictEqual(resolved, { command: "python3", argsPrefix: [] });
      });
    });
  }

  // An explicit aiworkhub.pythonPath setting wins on every platform when it
  // resolves to a real file -- deterministic across all three.
  childProcess.spawnSync = () => ({ status: 0, stderr: "", signal: null });
  try {
    for (const platform of ["win32", "darwin", "linux"]) {
      withPlatform(platform, () => {
        // Load while spawnSync is stubbed because extension.js captures the
        // function at module evaluation time. This keeps the Windows branch
        // independent of the host runner's actual Python installation.
        const configuredHost = loadExtensionHost(repoRoot, "/opt/custom/python");
        withExistsSyncStub(new Set(["/opt/custom/python"]), () => {
          const resolved = configuredHost.extension.__testInternals.findPythonCommand(repoRoot);
          assert.strictEqual(resolved.command, "/opt/custom/python");
        });
      });
    }
  } finally {
    childProcess.spawnSync = originalSpawnSync;
  }
}

async function testConcurrentRequestFramingAndCorrelation(tmp) {
  const repoRoot = path.join(tmp, "concurrent-framing");
  fs.mkdirSync(repoRoot);
  writeRepo(repoRoot, "repo_concurrent0000000000000000009", "concurrent-framing");
  const host = loadExtensionHost(repoRoot);
  const client = new host.extension.__testInternals.McpStdioClient(
    repoRoot,
    { appendLine: () => {} },
    { repoId: "repo_concurrent0000000000000000009", repoName: "concurrent-framing" },
    "claim-concurrent",
  );
  const writes = [];
  const child = {
    pid: 24680,
    stdin: { write: (payload, cb) => { writes.push({ payload, cb }); return true; } },
  };
  client.child = child;
  client.lifecycleChild = child;
  client.lifecyclePid = child.pid;

  const p1 = client.request(
    "tools/call",
    { name: "aiworkhub_task_show", arguments: { task_id: "T-1" } },
    5000,
  );
  const p2 = client.request(
    "tools/call",
    {
      name: "aiworkhub_manager_source_graph_query",
      arguments: { mode: "focus", query: "Q-2" },
    },
    5000,
  );
  const p3 = client.request(
    "tools/call",
    { name: "aiworkhub_task_show", arguments: { task_id: "T-3" } },
    5000,
  );

  assert.strictEqual(writes.length, 1, "only one complete frame may be in flight");
  writes[0].cb();
  assert.strictEqual(writes.length, 2);
  writes[1].cb();
  assert.strictEqual(writes.length, 3);
  writes[2].cb();
  const frames = writes.map(({ payload }) => {
    assert.ok(payload.endsWith("\n"));
    assert.strictEqual(payload.indexOf("\n"), payload.length - 1);
    return JSON.parse(payload);
  });
  assert.deepStrictEqual(frames.map(({ id }) => id), [1, 2, 3]);
  assert.strictEqual(frames[0].params.arguments.task_id, "T-1");
  assert.strictEqual(frames[1].params.arguments.query, "Q-2");
  assert.strictEqual(frames[2].params.arguments.task_id, "T-3");

  client._onMessage(child, JSON.stringify({ jsonrpc: "2.0", id: 3, result: { tag: "R-3" } }));
  client._onMessage(child, JSON.stringify({ jsonrpc: "2.0", id: 1, result: { tag: "R-1" } }));
  client._onMessage(child, JSON.stringify({ jsonrpc: "2.0", id: 2, result: { tag: "R-2" } }));
  const results = await Promise.all([p1, p2, p3]);
  assert.deepStrictEqual(results.map(({ tag }) => tag), ["R-1", "R-2", "R-3"]);
  assert.strictEqual(client.pending.size, 0);
}

async function testPoisonedInvalidParamsRepairsOnlyOwnedChild(tmp) {
  const repoRoot = path.join(tmp, "poisoned-transport");
  fs.mkdirSync(repoRoot);
  writeRepo(repoRoot, "repo_poisoned000000000000000000009", "poisoned-transport");
  const host = loadExtensionHost(repoRoot);
  const client = new host.extension.__testInternals.McpStdioClient(
    repoRoot,
    { appendLine: () => {} },
    { repoId: "repo_poisoned000000000000000000009", repoName: "poisoned-transport" },
    "claim-poison",
  );
  const child = { pid: 4321 };
  client.lifecycleChild = child;
  client.lifecyclePid = child.pid;
  let replacements = 0;
  client.replaceForExplicitRecovery = async () => { replacements += 1; };
  const deliver = async (id, body) => {
    const settled = new Promise((resolve, reject) => {
      client.pending.set(id, { resolve, reject, timer: setTimeout(() => {}, 5000) });
      client.pendingChildren.set(id, child);
    });
    client._onMessage(child, JSON.stringify({ jsonrpc: "2.0", id, ...body }));
    await settled.catch(() => {});
  };
  const invalid = { error: { code: -32602, message: "Invalid request parameters" } };
  await deliver(1, invalid);
  await deliver(2, { result: { ok: true } });
  await deliver(3, invalid);
  await deliver(4, invalid);
  await new Promise((resolve) => setImmediate(resolve));
  assert.strictEqual(replacements, 1);
  await deliver(5, { result: { ok: true } });
  await deliver(6, { error: { code: -32602, message: "missing request_id", data: { field: "request_id" } } });
  await deliver(7, { error: { code: -32602, message: "missing request_id", data: { field: "request_id" } } });
  await new Promise((resolve) => setImmediate(resolve));
  assert.strictEqual(replacements, 1, "detailed caller errors must not poison transport");
}

async function testLivePoisonedInvalidParamsShapeRepairsOnce(tmp) {
  const repoRoot = path.join(tmp, "poisoned-live-shape");
  fs.mkdirSync(repoRoot);
  writeRepo(repoRoot, "repo_liveshape00000000000000000009", "poisoned-live-shape");
  const host = loadExtensionHost(repoRoot);
  const client = new host.extension.__testInternals.McpStdioClient(
    repoRoot,
    { appendLine: () => {} },
    { repoId: "repo_liveshape00000000000000000009", repoName: "poisoned-live-shape" },
    "claim-live-shape",
  );
  const child = { pid: 4322 };
  client.lifecycleChild = child;
  client.lifecyclePid = child.pid;
  let replacements = 0;
  client.replaceForExplicitRecovery = async () => { replacements += 1; };
  const deliver = async (id, body) => {
    const settled = new Promise((resolve, reject) => {
      client.pending.set(id, { resolve, reject, timer: setTimeout(() => {}, 5000) });
      client.pendingChildren.set(id, child);
    });
    client._onMessage(child, JSON.stringify({ jsonrpc: "2.0", id, ...body }));
    await settled.catch(() => {});
  };

  const liveShape = { error: { code: -32602, message: 'Invalid request parameters("")' } };
  const liveShapeEmptyData = { error: { code: -32602, message: 'Invalid request parameters("")', data: "" } };

  await deliver(1, { result: { ok: true } });
  await deliver(2, liveShape);
  await deliver(3, liveShapeEmptyData);
  await new Promise((resolve) => setImmediate(resolve));
  assert.strictEqual(replacements, 1);

  await deliver(4, { result: { ok: true } });
  await deliver(5, liveShape);
  await new Promise((resolve) => setImmediate(resolve));
  assert.strictEqual(replacements, 1, "single live-shaped error after reset must not replace");

  await deliver(6, { error: { code: -32602, message: "missing request_id", data: { field: "request_id" } } });
  await deliver(7, { error: { code: -32602, message: "missing request_id", data: { field: "request_id" } } });
  await deliver(8, { error: { code: -32602, message: 'Invalid request parameters("boom")' } });
  await deliver(9, { error: { code: -32602, message: 'Invalid request parameters("boom")' } });
  await new Promise((resolve) => setImmediate(resolve));
  assert.strictEqual(replacements, 1, "detailed caller errors must not poison transport");
}

(async () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "aiworkhub-reloadless-"));
  await testSelfHealsAndReconnectsWithoutReload(tmp);
  await testRuntimeInfoReusesHandshakeEvidenceDuringBackgroundConvergence(tmp);
  await testExplicitRetryAlwaysReplacesWindowOwnedChild(tmp);
  await testHandshakeFailuresExposeExactPhase(tmp);
  await testBoundedRetryOnPersistentMismatch(tmp);
  await testFailedRestartDegradesWithoutCrossRepoFallback(tmp);
  await testTwoWorkspacesRepairInIsolation(tmp);
  testExplicitRetryPreservesPartialRuntimeRepairBudget(tmp);
  await testConcurrentRequestFramingAndCorrelation(tmp);
  await testPoisonedInvalidParamsRepairsOnlyOwnedChild(tmp);
  await testLivePoisonedInvalidParamsShapeRepairsOnce(tmp);
  testPlatformPythonResolution(tmp);
  console.log("AIWorkHub reloadless runtime-repair regression passed");
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
