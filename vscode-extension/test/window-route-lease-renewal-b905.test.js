const assert = require("assert");
const childProcess = require("child_process");
const EventEmitter = require("events");
const fs = require("fs");
const Module = require("module");
const os = require("os");
const path = require("path");

const extensionPath = path.resolve(__dirname, "..", "extension.js");

function writeRepo(root, repoId, repoName) {
  fs.mkdirSync(path.join(root, ".aiworkhub"), { recursive: true });
  fs.writeFileSync(
    path.join(root, ".aiworkhub", "project.json"),
    JSON.stringify({ repo_id: repoId, repo_name: repoName }) + "\n",
    "utf8",
  );
}

class FakeChild extends EventEmitter {
  constructor() {
    super();
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
    if (message.method === "initialize") {
      this._send({ protocolVersion: "2024-11-05", capabilities: {}, serverInfo: { name: "fake" } }, message.id);
      return;
    }
    if (message.method === "notifications/initialized") {
      return;
    }
    if (message.method !== "tools/call") {
      this._send({}, message.id);
      return;
    }
    this._send({ content: [{ type: "text", text: JSON.stringify({ ok: true, dispatcher_started: true }) }] }, message.id);
  }
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
      showInformationMessage: () => {},
      showQuickPick: async () => null,
      registerWebviewPanelSerializer: () => ({ dispose: () => {} }),
      registerWebviewViewProvider: () => ({ dispose: () => {} }),
      createWebviewPanel: () => null,
    },
    commands: {
      registerCommand: () => ({ dispose: () => {} }),
    },
    Uri: {
      joinPath: (...parts) => ({ fsPath: parts.map((p) => p.fsPath || p).join("/") }),
    },
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
      extension: { packageJSON: { version: "0.6.29" } },
      subscriptions: [],
      workspaceState: { update: () => {}, get: () => undefined },
    };
    return { extension, context };
  } finally {
    Module._load = originalLoad;
  }
}

(async () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "aiworkhub-lease-"));
  const repoA = path.join(tmp, "alpha");
  fs.mkdirSync(repoA);
  writeRepo(repoA, "repo_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "alpha");

  const originalSpawn = childProcess.spawn;
  childProcess.spawn = () => new FakeChild();

  const host = loadExtensionHost(repoA);
  try {
    // The renewal interval must be strictly and substantially shorter than
    // the 15-minute lease TTL -- guards against a future edit accidentally
    // widening it toward (or past) the TTL.
    const { constants } = host.extension.__testInternals;
    assert.ok(constants.WINDOW_ROUTE_RENEWAL_INTERVAL_MS < constants.WINDOW_ROUTE_LEASE_TTL_MS / 2);

    await host.extension.activate(host.context);
    assert.strictEqual(host.extension.__testInternals.isWindowRouteRenewalTimerActive(), true, "timer must start on activate");

    const routePath = host.extension.__testInternals.windowRouteStatePath(repoA, "");
    const windowRouteDir = path.dirname(routePath);
    const windowFiles = () => fs.readdirSync(windowRouteDir).filter((f) => f.endsWith(".json"));
    assert.strictEqual(windowFiles().length, 1, "exactly one window route file expected after activation");
    const [fileName] = windowFiles();
    const firstRecord = JSON.parse(fs.readFileSync(path.join(windowRouteDir, fileName), "utf8"));

    // Simulate one lease-renewal tick directly (avoids a real multi-minute
    // sleep in the test) and confirm the lease timestamp advances.
    await new Promise((resolve) => setTimeout(resolve, 5));
    host.extension.__testInternals.renewWindowRouteLease();
    const renewedRecord = JSON.parse(fs.readFileSync(path.join(windowRouteDir, fileName), "utf8"));
    assert.notStrictEqual(renewedRecord.updated_at, firstRecord.updated_at, "renewal must refresh updated_at/lease_expires_at");
    assert.ok(
      new Date(renewedRecord.lease_expires_at).getTime() >= new Date(firstRecord.lease_expires_at).getTime(),
      "renewal must not shorten the lease",
    );

    // Reload cycle: deactivate must dispose the timer and remove this
    // window's own route file.
    await host.extension.deactivate();
    assert.strictEqual(host.extension.__testInternals.isWindowRouteRenewalTimerActive(), false, "timer must be disposed on deactivate");
    assert.strictEqual(windowFiles().length, 0, "window route file must be removed on deactivate");

    // Restart safely: activating again must not throw and must restore
    // exactly one timer + one route file, never doubling either.
    await host.extension.activate(host.context);
    assert.strictEqual(host.extension.__testInternals.isWindowRouteRenewalTimerActive(), true, "timer must restart on reactivation");
    assert.strictEqual(windowFiles().length, 1, "exactly one window route file expected after reactivation");
    host.extension.__testInternals.startWindowRouteRenewalTimer();
    assert.strictEqual(host.extension.__testInternals.isWindowRouteRenewalTimerActive(), true, "starting again must not throw or leave the timer stopped");

    await host.extension.deactivate();
  } finally {
    childProcess.spawn = originalSpawn;
  }
  console.log("AIWorkHub window route lease renewal regression passed");
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
