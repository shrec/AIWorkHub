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
const EXPECTED_VERSION = "0.6.19";

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
  childProcess.spawn = (cmd, args, options) => {
    const spawnRecord = {
      cmd,
      args,
      env: options.env,
      repoRoot: options.env.AIWORKHUB_REPO_ROOT,
      repoId: options.env.AIWORKHUB_REPO_ID,
      windowId: options.env.AIWORKHUB_WINDOW_ID,
    };
    spawns.push(spawnRecord);
    const queue = generationsByRoot.get(spawnRecord.repoRoot) || [];
    const generation = queue.shift() || { version: EXPECTED_VERSION, missingTools: [], dieBeforeInitialize: false };
    return new FakeChild(spawnRecord, generation);
  };
  return { spawns, restore: () => { childProcess.spawn = original; } };
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
    assert.strictEqual(last.payload.repaired, true);
    assert.strictEqual(last.payload.repairAttempted, true);
    assert.strictEqual(last.payload.runtimeVersion, EXPECTED_VERSION);

    const snapshotMsg = messages.find((m) => m.type === "snapshot");
    assert.ok(snapshotMsg, "an already-open dashboard tab must reconnect automatically (a snapshot must follow a successful repair)");
    assert.strictEqual(snapshotMsg.payload.repo_id, "repo_selfheal00000000000000000000001");

    await host.extension.deactivate();
  } finally {
    fake.restore();
  }
}

async function testBoundedRetryOnPersistentMismatch(tmp) {
  const repoRoot = path.join(tmp, "persistent-mismatch");
  fs.mkdirSync(repoRoot);
  writeRepo(repoRoot, "repo_persist000000000000000000000002", "persistent-mismatch");

  const generations = new Map([[repoRoot, [
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
    assert.strictEqual(fake.spawns.length, 2, "a persistent mismatch spends exactly the bounded repair budget (1 restart)");
    let last = messages.filter((m) => m.type === "runtimeInfo").pop();
    assert.strictEqual(last.payload.reloadRequired, false);
    assert.strictEqual(last.payload.degraded, true);
    assert.strictEqual(last.payload.repairAttempted, true);
    assert.ok(last.payload.reason.includes("mismatch_after_repair"), last.payload.reason);

    // A second mismatch check (e.g. the user reopens the tab again) must NOT
    // spawn a third child -- the bounded budget for this mismatch episode is
    // already spent, and it must degrade visibly instead of looping forever.
    await host.extension.__testInternals.pushRuntimeInfo(view);
    assert.strictEqual(fake.spawns.length, 2, "exhausted repair budget must never spawn again for the same mismatch episode");
    last = messages.filter((m) => m.type === "runtimeInfo").pop();
    assert.strictEqual(last.payload.reloadRequired, false);
    assert.strictEqual(last.payload.degraded, true);
    assert.strictEqual(last.payload.repairAttempted, false);
    assert.strictEqual(last.payload.reason, "runtime_repair_budget_exhausted");

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
  ]]]);
  const fake = installSpawnFake(generations);
  try {
    const host = loadExtensionHost(repoRoot);
    await host.extension.activate(host.context);
    const { view, messages } = makeView(host);
    await host.extension.__testInternals.pushRuntimeInfo(view);

    assert.strictEqual(fake.spawns.length, 2);
    const last = messages.filter((m) => m.type === "runtimeInfo").pop();
    assert.strictEqual(last.payload.reloadRequired, false, "a failed restart must never fall back to a manual reload instruction");
    assert.strictEqual(last.payload.degraded, true);
    assert.strictEqual(last.payload.repairAttempted, true);
    assert.ok(typeof last.payload.reason === "string" && last.payload.reason.length > 0, "a failed restart must surface a readable degraded reason");
    assert.ok(last.payload.reason.startsWith("runtime_repair_failed:"));

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
    assert.strictEqual(lastA.payload.repaired, true);
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
      assert.deepStrictEqual(resolved, { command: "py", argsPrefix: ["-3"] });
    });
  });

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
  for (const platform of ["win32", "darwin", "linux"]) {
    withPlatform(platform, () => {
      const configuredHost = loadExtensionHost(repoRoot, "/opt/custom/python");
      withExistsSyncStub(new Set(["/opt/custom/python"]), () => {
        const resolved = configuredHost.extension.__testInternals.findPythonCommand(repoRoot);
        assert.strictEqual(resolved.command, "/opt/custom/python");
      });
    });
  }
}

(async () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "aiworkhub-reloadless-"));
  await testSelfHealsAndReconnectsWithoutReload(tmp);
  await testBoundedRetryOnPersistentMismatch(tmp);
  await testFailedRestartDegradesWithoutCrossRepoFallback(tmp);
  await testTwoWorkspacesRepairInIsolation(tmp);
  testPlatformPythonResolution(tmp);
  console.log("AIWorkHub reloadless runtime-repair regression passed");
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
