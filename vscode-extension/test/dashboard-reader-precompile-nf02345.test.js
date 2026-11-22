"use strict";

// NF-2026-02345: the first dashboard reader of a fresh stable runtime
// generation starts one compileall child beside it. The reader never waits for
// that child, and no compileall outcome ever reaches the reader. Fakes only:
// no real Python or provider process is started.

const assert = require("assert");
const childProcess = require("child_process");
const EventEmitter = require("events");
const fs = require("fs");
const Module = require("module");
const os = require("os");
const path = require("path");

const extensionPath = path.resolve(__dirname, "..", "extension.js");
const packageVersion = require("../package.json").version;
const { FakeDashboardChild, isDashboardReaderSpawn } = require("./dashboard-reader-nf02287.test.js");

const COMPILEALL_ARGS_TAIL = ["-I", "-m", "compileall", "-q", "-j"];
const TOKEN_KEYS = ["BITNN_TASKCTL_COORDINATOR_TOKEN", "BITNN_TASKCTL_COORDINATOR_TOKEN_FILE"];

function isCompileallSpawn(args) {
  return Array.isArray(args) && args.includes("compileall");
}

// A child that never exits on its own: the reader must not depend on it.
class SilentChild extends EventEmitter {
  constructor() {
    super();
    this.pid = 4242;
    this.killed = false;
    this.stdin = null;
    this.stdout = null;
    this.stderr = null;
  }

  kill() {
    this.killed = true;
  }
}

class FakeMcpChild extends SilentChild {
  constructor() {
    super();
    this.stdout = new EventEmitter();
    this.stderr = new EventEmitter();
    this.stdin = { write: (_payload, cb) => { if (cb) cb(); } };
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

function loadExtensionHost(repoRoot, extensionRoot, withStorage) {
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
      extensionUri: { fsPath: extensionRoot },
      extension: { packageJSON: { version: packageVersion } },
      subscriptions: [],
      workspaceState: { update: () => {}, get: () => undefined },
    };
    if (withStorage) context.globalStorageUri = { fsPath: path.join(path.dirname(extensionRoot), "global-storage") };
    return { extension, context };
  } finally {
    Module._load = originalLoad;
  }
}

const settle = (ms = 20) => new Promise((resolve) => setTimeout(resolve, ms));

// Installs fakes for every child the extension may start. Records each spawn;
// `compileallBehavior` chooses how the compileall child misbehaves.
function installFakes({ compileallBehavior = "silent" } = {}) {
  const originalSpawn = childProcess.spawn;
  const originalExecFile = childProcess.execFile;
  const originalSpawnSync = childProcess.spawnSync;
  const originalParallelism = os.availableParallelism;
  const compileallChildren = [];
  const readerChildren = [];
  const okResult = (request, spawnRecord) => (request.tool === "aiworkhub_dashboard_snapshot"
    ? { repo_id: spawnRecord.env.AIWORKHUB_REPO_ID, storage: { ready: true }, tasks: [], summary: {} }
    : { ok: true });
  childProcess.spawn = (cmd, args, options) => {
    const spawnRecord = {
      cmd, args, cwd: options.cwd, env: options.env, shell: options.shell,
      stdio: options.stdio, windowsHide: options.windowsHide, toolCalls: [],
    };
    if (isCompileallSpawn(args)) {
      compileallChildren.push(spawnRecord);
      if (compileallBehavior === "throw") throw new Error("synthetic_spawn_throw");
      const child = new SilentChild();
      if (compileallBehavior === "error") setImmediate(() => child.emit("error", new Error("synthetic_error")));
      if (compileallBehavior === "exit-1") setImmediate(() => child.emit("exit", 1, null));
      return child;
    }
    if (isDashboardReaderSpawn(args)) {
      readerChildren.push(spawnRecord);
      return new FakeDashboardChild(spawnRecord, "live", okResult);
    }
    return new FakeMcpChild();
  };
  childProcess.execFile = (_cmd, _args, _options, callback) => { setImmediate(() => callback(null, "", "")); };
  childProcess.spawnSync = () => ({ status: 0, stdout: "", stderr: "", signal: null });
  return {
    compileallChildren,
    readerChildren,
    setCores: (cores) => { os.availableParallelism = () => cores; },
    restore: () => {
      os.availableParallelism = originalParallelism;
      childProcess.spawn = originalSpawn;
      childProcess.execFile = originalExecFile;
      childProcess.spawnSync = originalSpawnSync;
    },
  };
}

// Seeds bytecode file names into the materialized generation, before the reader
// starts. The generation is the runtime_dir named by the stable pointer that
// activate writes; the files are real, nothing is faked.
function seedBytecode(host, names) {
  const pointerPath = path.join(host.context.globalStorageUri.fsPath, "runtime", "current.json");
  const pointer = JSON.parse(fs.readFileSync(pointerPath, "utf8"));
  const pycacheDir = path.join(pointer.runtime_dir, "aiworkhub", "__pycache__");
  fs.mkdirSync(pycacheDir, { recursive: true });
  for (const name of names) fs.writeFileSync(path.join(pycacheDir, name), "");
}

function makeInstalledRoot(tmp, name) {
  const installedRoot = path.join(tmp, name);
  const packageRoot = path.join(installedRoot, "runtime", "aiworkhub");
  fs.mkdirSync(packageRoot, { recursive: true });
  fs.writeFileSync(path.join(packageRoot, "__init__.py"), "# precompile fixture\n");
  fs.writeFileSync(path.join(packageRoot, "server.py"), "# precompile fixture\n");
  return installedRoot;
}

async function openReader(host, view) {
  view.setVisible(true);
  await host.extension.__testInternals.pushSnapshot(view);
  await settle(20);
}

async function runCase(name, { cores = 8, withStorage = true, compileallBehavior = "silent" } = {}, body) {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "aiworkhub-nf02345-"));
  const repoRoot = path.join(tmp, "repo");
  fs.mkdirSync(repoRoot);
  writeRepo(repoRoot, `repo_${"02345".padEnd(32, "0")}`, "nf02345");
  const installedRoot = makeInstalledRoot(tmp, "installed-extension");
  const fakes = installFakes({ compileallBehavior });
  fakes.setCores(cores);
  try {
    const host = loadExtensionHost(repoRoot, installedRoot, withStorage);
    await host.extension.activate(host.context);
    await body({ host, fakes });
    await host.extension.deactivate();
  } finally {
    fakes.restore();
    fs.rmSync(tmp, { recursive: true, force: true });
  }
  console.log(`NF-2026-02345 ${name} passed`);
}

async function main() {
  // Each case reloads the extension, which adds one process exit listener.
  process.setMaxListeners(64);

  // Fresh stable generation: one compileall beside the first reader, and the
  // reader is not delayed by it (the compileall child never exits here).
  await runCase("fresh generation starts one compileall", {}, async ({ host, fakes }) => {
    const view = new host.extension.__testInternals.ViewState(() => {});
    await openReader(host, view);
    assert.strictEqual(fakes.compileallChildren.length, 1, "exactly one compileall must start");
    assert.strictEqual(fakes.readerChildren.length, 1, "the reader must start");
    const compile = fakes.compileallChildren[0];
    const reader = fakes.readerChildren[0];
    // cores 8 -> jobs floor(8 / 2) = 4; the package dir is <runtimeDir>/aiworkhub.
    assert.deepStrictEqual(compile.args.slice(-7), [...COMPILEALL_ARGS_TAIL, "4", path.join(compile.cwd, "aiworkhub")]);
    assert.strictEqual(compile.cwd, reader.cwd, "compileall must run in the reader's runtime dir");
    assert.strictEqual(compile.shell, false);
    assert.strictEqual(compile.windowsHide, true);
    assert.strictEqual(compile.stdio, "ignore");
    const client = host.extension.__testInternals.dashboardProcessClientForTest();
    assert.ok(client && client.running, "the reader must run while compileall is still alive");
  });

  // Coordinator tokens in process.env must never reach the compileall child.
  const savedTokens = TOKEN_KEYS.map((key) => [key, process.env[key]]);
  for (const key of TOKEN_KEYS) process.env[key] = "synthetic-token";
  try {
    await runCase("compileall env strips coordinator tokens", {}, async ({ host, fakes }) => {
      const view = new host.extension.__testInternals.ViewState(() => {});
      await openReader(host, view);
      assert.strictEqual(fakes.compileallChildren.length, 1);
      for (const key of TOKEN_KEYS) {
        assert.ok(!Object.prototype.hasOwnProperty.call(fakes.compileallChildren[0].env, key), `${key} leaked into compileall`);
      }
    });
  } finally {
    for (const [key, value] of savedTokens) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  }

  // A synchronous throw, an error event or a non-zero exit never fails the reader.
  for (const compileallBehavior of ["throw", "error", "exit-1"]) {
    await runCase(`compileall ${compileallBehavior} leaves the reader intact`, { compileallBehavior }, async ({ host, fakes }) => {
      const view = new host.extension.__testInternals.ViewState(() => {});
      await openReader(host, view);
      assert.strictEqual(fakes.readerChildren.length, 1, "the reader must still start");
      const client = host.extension.__testInternals.dashboardProcessClientForTest();
      assert.ok(client && client.running, "the reader must stay running");
      assert.strictEqual(fakes.compileallChildren.length, 1, "a failed compileall is never retried");
    });
  }

  // Reader respawn after dispose, and a second view: still one compileall.
  await runCase("respawn does not start a second compileall", {}, async ({ host, fakes }) => {
    const first = new host.extension.__testInternals.ViewState(() => {});
    await openReader(host, first);
    first.dispose();
    const second = new host.extension.__testInternals.ViewState(() => {});
    await openReader(host, second);
    assert.strictEqual(fakes.readerChildren.length, 2, "the reader must respawn");
    assert.strictEqual(fakes.compileallChildren.length, 1, "respawn must not compile the same runtimeDir again");
  });

  // Gates: no compileall without storageRoot, with the reader's own bytecode cached, or below two jobs.
  await runCase("no storageRoot means no compileall", { withStorage: false }, async ({ host, fakes }) => {
    const view = new host.extension.__testInternals.ViewState(() => {});
    await openReader(host, view);
    assert.strictEqual(fakes.readerChildren.length, 1, "the reader must still start");
    assert.strictEqual(fakes.compileallChildren.length, 0, "bundled or test-host runtime must never get bytecode written");
  });

  await runCase("bytecode of earlier processes still gets the precompile", {}, async ({ host, fakes }) => {
    seedBytecode(host, ["__init__.cpython-312.pyc", "server.cpython-312.pyc"]);
    assert.strictEqual(fakes.readerChildren.length, 0, "the seed must precede the reader");
    const view = new host.extension.__testInternals.ViewState(() => {});
    await openReader(host, view);
    assert.strictEqual(fakes.compileallChildren.length, 1, "bytecode of other modules must not suppress the precompile");
    assert.strictEqual(fakes.readerChildren.length, 1, "the reader must still start");
  });

  await runCase("cached reader bytecode means no compileall", {}, async ({ host, fakes }) => {
    seedBytecode(host, ["__init__.cpython-312.pyc", "server.cpython-312.pyc", "dashboard_reader.cpython-312.pyc"]);
    assert.strictEqual(fakes.readerChildren.length, 0, "the seed must precede the reader");
    const view = new host.extension.__testInternals.ViewState(() => {});
    await openReader(host, view);
    assert.strictEqual(fakes.compileallChildren.length, 0, "the reader's own bytecode is already cached");
    assert.strictEqual(fakes.readerChildren.length, 1, "the reader must still start");
  });

  await runCase("jobs below two means no compileall", { cores: 3 }, async ({ host, fakes }) => {
    const view = new host.extension.__testInternals.ViewState(() => {});
    await openReader(host, view);
    assert.strictEqual(fakes.readerChildren.length, 1, "the reader must still start");
    assert.strictEqual(fakes.compileallChildren.length, 0, "floor(3 / 2) is 1, below the minimum of 2");
  });

  console.log("NF-2026-02345 dashboard reader precompile passed");
}

if (require.main === module) {
  main().then(() => process.exit(0)).catch((err) => {
    console.error(err);
    process.exit(1);
  });
}
