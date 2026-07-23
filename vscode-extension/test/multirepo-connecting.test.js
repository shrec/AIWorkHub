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
  constructor(spawnRecord, behavior) {
    super();
    this.spawnRecord = spawnRecord;
    this.behavior = behavior;
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
    if (this.behavior === "die-before-initialize" && message.method === "initialize") {
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
    if (message.method !== "tools/call") {
      this._send({}, message.id);
      return;
    }
    const tool = message.params.name;
    if (tool === "aiworkhub_dashboard_snapshot") {
      const repoId = this.spawnRecord.env.AIWORKHUB_REPO_ID;
      const result = {
        content: [{ type: "text", text: JSON.stringify({
          repo_id: repoId,
          storage: { ready: true },
          tasks: [{ task_id: `task-${repoId.slice(-4)}`, repo_id: repoId }],
          summary: { active: 1 },
        }) }],
      };
      if (this.behavior === "slow-snapshot") {
        setTimeout(() => this._send(result, message.id), 30);
      } else {
        this._send(result, message.id);
      }
    } else if (tool === "aiworkhub_dashboard_health") {
      this._send({
        content: [{ type: "text", text: JSON.stringify({ ok: true, server_version: "0.6.30" }) }],
      }, message.id);
    } else if (tool === "aiworkhub_dispatcher_ensure_started") {
      this._send({
        content: [{ type: "text", text: JSON.stringify({ ok: true, dispatcher_started: true }) }],
      }, message.id);
    } else {
      this._send({ content: [{ type: "text", text: "{}" }] }, message.id);
    }
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
      extension: { packageJSON: { version: "0.6.30" } },
      subscriptions: [],
      workspaceState: { update: () => {}, get: () => undefined },
    };
    return { extension, context };
  } finally {
    Module._load = originalLoad;
  }
}

async function snapshotFor(host) {
  const messages = [];
  const view = new host.extension.__testInternals.ViewState((message) => messages.push(message));
  await host.extension.__testInternals.pushSnapshot(view);
  return messages;
}

(async () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "aiworkhub-multirepo-"));
  const repoA = path.join(tmp, "alpha");
  const repoB = path.join(tmp, "beta");
  fs.mkdirSync(repoA);
  fs.mkdirSync(repoB);
  writeRepo(repoA, "repo_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "alpha");
  writeRepo(repoB, "repo_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "beta");

  const spawns = [];
  const behaviors = new Map([
    [repoA, ["slow-snapshot"]],
    [repoB, ["die-before-initialize", "live"]],
  ]);
  const originalSpawn = childProcess.spawn;
  childProcess.spawn = (cmd, args, options) => {
    const spawnRecord = {
      cmd,
      args,
      cwd: options.cwd,
      env: options.env,
      repoRoot: options.env.AIWORKHUB_REPO_ROOT,
      repoId: options.env.AIWORKHUB_REPO_ID,
      windowId: options.env.AIWORKHUB_WINDOW_ID,
    };
    spawns.push(spawnRecord);
    const queue = behaviors.get(spawnRecord.repoRoot) || [];
    return new FakeChild(spawnRecord, queue.shift() || "live");
  };

  const hostA = loadExtensionHost(repoA);
  const hostB = loadExtensionHost(repoB);

  try {
    await hostA.extension.activate(hostA.context);
    await hostB.extension.activate(hostB.context);
    for (const repoRoot of [repoA, repoB]) {
      const route = JSON.parse(fs.readFileSync(
        path.join(repoRoot, ".aiworkhub", "config", "routing", "coordinator-targets.json"),
        "utf8",
      ));
      assert.strictEqual(route.extension_host_pid, process.pid);
      assert.ok(route.window_id.startsWith("window_"));
    }
    const managedOldMux = path.join(tmp, "shrec.aiworkhub-0.6.2", "bin", "aiworkhub-app-server-mux");
    fs.mkdirSync(path.dirname(managedOldMux), { recursive: true });
    fs.writeFileSync(managedOldMux, "old");
    assert.strictEqual(hostA.extension.__testInternals.shouldRepairCodexMuxSetting(managedOldMux), true);
    const customMux = path.join(tmp, "custom-codex-wrapper");
    fs.writeFileSync(customMux, "custom");
    assert.strictEqual(hostA.extension.__testInternals.shouldRepairCodexMuxSetting(customMux), false);
    const firstMessages = [];
    const coalescedView = new hostA.extension.__testInternals.ViewState((message) => firstMessages.push(message));
    const slowFirst = hostA.extension.__testInternals.pushSnapshot(coalescedView);
    const overlappingRefresh = hostA.extension.__testInternals.pushSnapshot(coalescedView);
    assert.strictEqual(slowFirst, overlappingRefresh, "overlapping refresh did not reuse the in-flight snapshot");
    await Promise.all([slowFirst, overlappingRefresh]);
    assert.strictEqual(firstMessages.filter((message) => message.type === "snapshot").length, 1);
    const secondMessages = await snapshotFor(hostB);

    const snapA = firstMessages.find((m) => m.type === "snapshot");
    const snapB = secondMessages.find((m) => m.type === "snapshot");
    assert.ok(snapA, `repo A did not reach Live: ${JSON.stringify(firstMessages)}`);
    assert.ok(snapB, `repo B did not reach Live after reconnect: ${JSON.stringify(secondMessages)}`);
    assert.strictEqual(snapA.payload.repo_id, "repo_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa");
    assert.strictEqual(snapB.payload.repo_id, "repo_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb");
    assert.notStrictEqual(spawns[0].windowId, spawns[1].windowId);
    assert.ok(
      spawns.every((spawn) => spawn.env.BITNN_TASKCTL_COORDINATOR_TOKEN),
      JSON.stringify(spawns.map((spawn) => ({ repoRoot: spawn.repoRoot, repoId: spawn.repoId, token: Boolean(spawn.env.BITNN_TASKCTL_COORDINATOR_TOKEN) }))),
    );
    assert.ok(spawns.every((spawn) => spawn.env.BITNN_TASKCTL_COORDINATOR_TOKEN_FILE.startsWith(spawn.repoRoot)));
    assert.deepStrictEqual(new Set(spawns.map((spawn) => spawn.repoId)), new Set([
      "repo_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "repo_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    ]));
    assert.ok(!JSON.stringify(secondMessages).includes("repo_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"));
    assert.ok(!JSON.stringify(firstMessages).includes("repo_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"));

    await hostA.extension.deactivate();
    await hostB.extension.deactivate();
  } finally {
    childProcess.spawn = originalSpawn;
  }
  console.log("AIWorkHub two-repo Connecting regression passed");
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
