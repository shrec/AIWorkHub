"use strict";

// NF-2026-02113: one VS Code extension host was measured holding three
// `-m aiworkhub.server` children, two of them from the previous evening.
// These tests drive the real McpStdioClient lifecycle (start -> restart ->
// dispose) against a fake spawn and assert the owner/child invariant:
// exactly one live server child per owner, zero after dispose, and a
// terminal stop during interpreter selection never spawns an unowned child.

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const Module = require("node:module");
const childProcess = require("node:child_process");
const { EventEmitter } = require("node:events");
const { PassThrough } = require("node:stream");

const VSCODE_STUB_ID = "\0aiworkhub-vscode-stub-process-count";
const originalResolveFilename = Module._resolveFilename;
Module._resolveFilename = function patchedResolveFilename(request, ...rest) {
  if (request === "vscode") return VSCODE_STUB_ID;
  return originalResolveFilename.call(this, request, ...rest);
};
require.cache[VSCODE_STUB_ID] = {
  id: VSCODE_STUB_ID,
  filename: VSCODE_STUB_ID,
  loaded: true,
  exports: {
    workspace: {
      // An existing absolute interpreter path keeps findPythonCommandForLaunch
      // on its first candidate; the faked execFile preflight accepts it.
      getConfiguration: () => ({
        get: (key, fallback) => (key === "pythonPath" ? process.execPath : fallback),
      }),
      workspaceFolders: [],
    },
    window: {},
    commands: {},
    extensions: { getExtension: () => null },
    ConfigurationTarget: { Global: 1 },
  },
};

const { __testInternals } = require("../extension.js");
const {
  McpStdioClient,
  liveOwnedMcpChildCount,
  terminateAllOwnedMcpChildren,
} = __testInternals;

let nextFakePid = 4_000_000;
const spawnedServers = [];
const taskkillPids = [];

class FakeServerChild extends EventEmitter {
  constructor() {
    super();
    nextFakePid += 1;
    this.pid = nextFakePid;
    this.stdin = new PassThrough();
    this.stdout = new PassThrough();
    this.stderr = new PassThrough();
    this.exitCode = null;
    this.signalCode = null;
    this.killed = false;
  }

  get alive() {
    return this.exitCode == null && this.signalCode == null;
  }

  _exit(code, signal) {
    if (!this.alive) return;
    this.exitCode = code;
    this.signalCode = signal;
    setImmediate(() => this.emit("exit", code, signal));
  }

  kill() {
    if (!this.alive) return false;
    this.killed = true;
    this._exit(null, "SIGTERM");
    return true;
  }
}

const originalSpawn = childProcess.spawn;
const originalExecFile = childProcess.execFile;
const originalSpawnSync = childProcess.spawnSync;

function isServerArgs(args) {
  return Array.isArray(args) && args.includes("aiworkhub.server") && args.includes("-m");
}

childProcess.spawn = function fakeSpawn(command, args, options) {
  if (!isServerArgs(args)) return originalSpawn.call(this, command, args, options);
  const child = new FakeServerChild();
  spawnedServers.push(child);
  return child;
};
childProcess.execFile = function fakeExecFile(command, args, options, callback) {
  if (Array.isArray(args) && args.includes("import aiworkhub.server")) {
    setImmediate(() => callback(null, "", ""));
    return new EventEmitter();
  }
  return originalExecFile.call(this, command, args, options, callback);
};
childProcess.spawnSync = function fakeSpawnSync(command, args, options) {
  if (command === "taskkill" && Array.isArray(args)) {
    // Never reach the real taskkill: a fake pid may be a real process.
    const pid = Number(args[args.indexOf("/PID") + 1]);
    taskkillPids.push(pid);
    const target = spawnedServers.find((child) => child.pid === pid);
    if (!target || !target.alive) return { status: 128, error: null };
    target._exit(1, null);
    return { status: 0, error: null };
  }
  return originalSpawnSync.call(this, command, args, options);
};

test.after(() => {
  childProcess.spawn = originalSpawn;
  childProcess.execFile = originalExecFile;
  childProcess.spawnSync = originalSpawnSync;
});

const repoRoot = fs.mkdtempSync(path.join(os.tmpdir(), "awh-process-count-"));
test.after(() => fs.rmSync(repoRoot, { recursive: true, force: true }));

function makeOwner(options = {}) {
  const outputChannel = { appendLine() {} };
  const identity = { repoId: "repo_processcount", root: repoRoot, label: "process-count" };
  const client = new McpStdioClient(repoRoot, outputChannel, identity, "episode_process_count", options);
  // The handshake belongs to the real server; the owner/child bookkeeping
  // under test is everything around it.
  client._handshake = async function fakeHandshake() {
    this.initialized = true;
  };
  return client;
}

function liveServers() {
  return spawnedServers.filter((child) => child.alive).length;
}

test("start -> restart -> dispose keeps exactly one live server per owner, zero after dispose", async () => {
  const dashboard = makeOwner();
  const worker = makeOwner({ grantWorkerBridgeWrites: true });
  const managerLoop = makeOwner({ grantManagerLoopGates: true });
  const owners = [dashboard, worker, managerLoop];
  const before = liveServers();

  await Promise.all(owners.map((owner) => owner.ensureStarted()));
  for (const owner of owners) {
    assert.equal(liveOwnedMcpChildCount(owner), 1, "one live server child per owner after start");
  }
  assert.equal(liveServers() - before, 3);
  // Idempotent: a second ensureStarted on a ready owner spawns nothing.
  await Promise.all(owners.map((owner) => owner.ensureStarted()));
  assert.equal(liveServers() - before, 3);

  const firstDashboardChild = dashboard.lifecycleChild;
  const replaced = await dashboard.replaceForExplicitRecovery();
  assert.equal(replaced.replaced, true);
  assert.notEqual(dashboard.lifecycleChild, firstDashboardChild);
  assert.equal(firstDashboardChild.alive, false, "the replaced server is stopped, not orphaned");
  if (process.platform === "win32") {
    assert.ok(taskkillPids.includes(firstDashboardChild.pid), "Windows replacement uses a tree kill");
  }
  for (const owner of owners) {
    assert.equal(liveOwnedMcpChildCount(owner), 1, "restart keeps one live server child per owner");
  }

  // Bounded runtime-repair restart takes the same owner-replacement path.
  const beforeRepair = worker.lifecycleChild;
  const repair = await worker.attemptRuntimeRepair("test_mismatch");
  assert.equal(repair.repaired, true);
  assert.equal(beforeRepair.alive, false);
  assert.equal(liveOwnedMcpChildCount(worker), 1);
  assert.equal(liveServers() - before, 3);

  dashboard.stop({ restart: false });
  worker.stop({ restart: false });
  await managerLoop.stopDispatcherThenTerminate({ restart: false, timeoutMs: 50 });
  for (const owner of owners) {
    assert.equal(liveOwnedMcpChildCount(owner), 0, "dispose leaves no live server child");
  }
  assert.equal(liveServers(), before);
});

test("a terminal stop during interpreter selection never spawns an unowned server", async () => {
  const owner = makeOwner();
  const spawnedBefore = spawnedServers.length;
  const startup = owner.ensureStarted();
  // Deactivate / repository switch arrives while the preflight is pending.
  owner.stop({ restart: false });
  await assert.rejects(startup, /mcp_stopped_during_startup/);
  assert.equal(spawnedServers.length, spawnedBefore, "no server was spawned after its owner let go");
  assert.equal(liveOwnedMcpChildCount(owner), 0);

  // The same owner may still start again later, exactly once.
  await owner.ensureStarted();
  assert.equal(liveOwnedMcpChildCount(owner), 1);
  owner.stop({ restart: false });
  assert.equal(liveOwnedMcpChildCount(owner), 0);
});

test("a terminal stop while waiting on the previous owner's shutdown cancels the start", async () => {
  const owner = makeOwner();
  let releaseBarrier;
  owner.startupBarrier = new Promise((resolve) => { releaseBarrier = resolve; });
  const spawnedBefore = spawnedServers.length;
  const startup = owner.ensureStarted();
  owner.stop({ restart: false });
  releaseBarrier();
  await assert.rejects(startup, /mcp_stopped_during_startup/);
  assert.equal(spawnedServers.length, spawnedBefore);
});

test("ensureStarted after a terminal stop of a pending start begins a fresh start", async () => {
  const owner = makeOwner();
  const spawnedBefore = spawnedServers.length;
  const doomed = owner.ensureStarted();
  owner.stop({ restart: false });
  assert.equal(owner.startingPromise, null, "a terminal stop drops the doomed start's promise");
  // Hold the fresh start until the doomed one has settled: on Linux the fake
  // preflight lets it finish first, which would empty the slot legitimately.
  let releaseFresh;
  owner.startupBarrier = new Promise((resolve) => { releaseFresh = resolve; });
  const fresh = owner.ensureStarted();
  assert.notEqual(fresh, doomed, "the new caller does not join the doomed start");
  await assert.rejects(doomed, /mcp_stopped_during_startup/);
  // The doomed start settling must not clear the fresh start's slot.
  assert.equal(owner.startingPromise, fresh);
  releaseFresh();
  await fresh;
  assert.equal(owner.startingPromise, null);
  assert.equal(liveOwnedMcpChildCount(owner), 1, "exactly one live server child after the fresh start");
  assert.equal(spawnedServers.length - spawnedBefore, 1, "only the fresh start spawned a server");
  owner.stop({ restart: false });
  assert.equal(liveOwnedMcpChildCount(owner), 0);
});

test("the globalStorage launcher starts the base interpreter, not the venv redirector", () => {
  const source = fs.readFileSync(path.join(__dirname, "..", "extension.js"), "utf8");
  const start = source.indexOf("def serve_command(runtime_dir):");
  assert.ok(start > 0, "launcher defines serve_command");
  const body = source.slice(start, source.indexOf("class Child:", start));
  assert.match(body, /sys, "_base_executable"/);
  assert.match(body, /env\["__PYVENV_LAUNCHER__"\] = executable/);
  assert.match(body, /os\.name == "nt"/);
  assert.match(source, /command, env = serve_command\(runtime_dir\)/);
});

test("the served runtime drops the venv hand-off variable before the server runs", () => {
  const source = fs.readFileSync(path.join(__dirname, "..", "extension.js"), "utf8");
  const start = source.indexOf("def serve_runtime(runtime_dir):");
  assert.ok(start > 0, "launcher defines serve_runtime");
  const body = source.slice(start, source.indexOf("def serve_command(runtime_dir):", start));
  const statements = body.split("\n").slice(1)
    .map((line) => line.trim())
    .filter((line) => line && !line.startsWith("#"));
  // First statement: every subprocess the server starts (py -3, worker
  // python) must resolve its own prefix, not this venv's.
  assert.equal(statements[0], 'os.environ.pop("__PYVENV_LAUNCHER__", None)');
  assert.ok(body.indexOf("__PYVENV_LAUNCHER__") < body.indexOf("from aiworkhub.server import main"));
});
