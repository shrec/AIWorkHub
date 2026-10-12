// NF-2026-01435: a handshake serverInfo.version equal to the packaged
// runtime version must skip the dashboard_health preflight call entirely --
// a hanging dashboard_health must never block startup or a worker callTool.
// Any other handshake version (an SDK version, or a real mismatch) must
// still fall back to the existing dashboard_health evidence path unchanged.

const assert = require("assert");
const childProcess = require("child_process");
const EventEmitter = require("events");
const fs = require("fs");
const Module = require("module");
const os = require("os");
const path = require("path");

const extensionPath = path.resolve(__dirname, "..", "extension.js");
const PACKAGE_VERSION = require("../package.json").version;

const DASHBOARD_TOOL_NAMES = [
  "aiworkhub_dashboard_snapshot",
  "aiworkhub_dashboard_task_detail",
  "aiworkhub_dashboard_health",
  "aiworkhub_dashboard_task_live_output",
  "aiworkhub_dashboard_memory",
  "aiworkhub_dashboard_sessions",
  "aiworkhub_dashboard_kb",
  "aiworkhub_dashboard_settings",
];

function writeRepo(root, repoId, repoName) {
  fs.mkdirSync(path.join(root, ".aiworkhub"), { recursive: true });
  fs.writeFileSync(
    path.join(root, ".aiworkhub", "project.json"),
    JSON.stringify({
      schema_id: "aiworkhub.project_manifest.v1",
      manifest_version: 1,
      layout_version: 1,
      repo_id: repoId,
      repo_name: repoName,
      layout: {
        durable: {
          tasking: "tasking",
          source_graph: "source_graph",
          sessions: "sessions",
          memory: "memory",
          kb: "kb",
          config: "config",
        },
        runtime: { path: "runtime", durable: false, ignored: true },
      },
    }) + "\n",
    "utf8",
  );
}

// A fake stdio child whose aiworkhub_dashboard_health NEVER answers -- if a
// regression reintroduces a redundant dashboard_health call on the fast
// handshake path, the request hangs until MCP_REQUEST_TIMEOUT_MS instead of
// silently passing.
class FakeChild extends EventEmitter {
  constructor(spawnRecord, generation) {
    super();
    this.spawnRecord = spawnRecord;
    this.generation = generation;
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
    if (message.method === "initialize") {
      this._send(
        { protocolVersion: "2024-11-05", capabilities: {}, serverInfo: { name: "fake", version: this.generation.version } },
        message.id,
      );
      return;
    }
    if (message.method === "notifications/initialized") {
      return;
    }
    if (message.method === "tools/list") {
      this._send({ tools: DASHBOARD_TOOL_NAMES.map((name) => ({ name })) }, message.id);
      return;
    }
    if (message.method !== "tools/call") {
      this._send({}, message.id);
      return;
    }
    const tool = message.params.name;
    if (tool === "aiworkhub_dashboard_health") {
      return;
    }
    if (tool === "aiworkhub_dispatcher_ensure_started") {
      this._send({ content: [{ type: "text", text: JSON.stringify({ ok: true, dispatcher_started: true }) }] }, message.id);
      return;
    }
    this._send({ content: [{ type: "text", text: "{}" }] }, message.id);
  }
}

function loadExtensionHost(repoRoot, extensionRoot) {
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
      extension: { packageJSON: { version: PACKAGE_VERSION } },
      subscriptions: [],
      workspaceState: { update: () => {}, get: () => undefined },
    };
    return { extension, context };
  } finally {
    Module._load = originalLoad;
  }
}

function installSpawnFake(generationQueue) {
  const spawns = [];
  const original = childProcess.spawn;
  const originalExecFile = childProcess.execFile;
  childProcess.spawn = (_cmd, _args, _options) => {
    const spawnRecord = { requests: [] };
    spawns.push(spawnRecord);
    const generation = generationQueue.shift() || { version: PACKAGE_VERSION };
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

async function testFastPathSkipsHangingHealthCall(tmp) {
  const repoRoot = path.join(tmp, "fast-path");
  fs.mkdirSync(repoRoot);
  writeRepo(repoRoot, "repo_fastpath0000000000000000001", "fast-path");

  // r1 died on the host at bundledRuntimeFallback (bundled_runtime_missing)
  // because the sparse worktree has no vscode-extension/runtime dir. Seed a
  // minimal one under this test's own extensionUri so activate() never hits
  // that fallback.
  const extensionRoot = path.join(tmp, "ext-fast-path");
  fs.mkdirSync(path.join(extensionRoot, "runtime", "aiworkhub"), { recursive: true });
  fs.writeFileSync(path.join(extensionRoot, "runtime", "aiworkhub", "__init__.py"), "");

  const fake = installSpawnFake([{ version: PACKAGE_VERSION }, { version: PACKAGE_VERSION }]);
  try {
    const host = loadExtensionHost(repoRoot, extensionRoot);
    await host.extension.activate(host.context);
    const client = host.extension.__testInternals.getMcpClient(host.context);
    await client.ensureStarted();

    assert.strictEqual(fake.spawns.length, 1, "activation preflight must spawn exactly one child");
    assert.strictEqual(
      fake.spawns[0].requests.some((r) => r.method === "tools/call" && r.tool === "aiworkhub_dashboard_health"),
      false,
      "a handshake serverInfo.version matching the package version must never call dashboard_health",
    );
    assert.ok(client.runtimePreflight, "handshake must record preflight evidence");
    assert.strictEqual(client.runtimePreflight.matches, true, "handshake evidence must match the packaged runtime");
    assert.strictEqual(client.runtimePreflight.runtimeVersion, PACKAGE_VERSION);

    const workerOutput = { appendLine: () => {} };
    const worker = new host.extension.__testInternals.McpStdioClient(
      client.repositoryRoot,
      workerOutput,
      client.repositoryIdentity,
      client.claimEpisode,
      { grantWorkerBridgeWrites: true },
    );
    const result = await worker.callTool("aiworkhub_dispatcher_ensure_started", {});
    assert.ok(result && result.ok === true, "worker callTool must resolve despite a never-answering dashboard_health");
    assert.strictEqual(fake.spawns.length, 2, "the worker client must spawn its own child");
    assert.strictEqual(
      fake.spawns[1].requests.some((r) => r.method === "tools/call" && r.tool === "aiworkhub_dashboard_health"),
      false,
      "the worker's own handshake must also skip the hanging dashboard_health call",
    );

    await host.extension.deactivate();
  } finally {
    fake.restore();
  }
}

async function testSdkVersionFallsBackToHealth() {
  const host = loadExtensionHost(path.join(os.tmpdir(), "nf1435-unused-b"), path.join(os.tmpdir(), "nf1435-unused-ext-b"));
  const client = new host.extension.__testInternals.McpStdioClient(
    "/nf1435-unused-root",
    { appendLine: () => {} },
    { repoId: "repo_unused" },
    "episode-unused",
  );
  let healthCalls = 0;
  client.notify = () => {};
  client.request = async (method, params) => {
    if (method === "initialize") {
      return { protocolVersion: "2024-11-05", capabilities: {}, serverInfo: { name: "fake", version: "1.29.0" } };
    }
    if (method === "tools/list") {
      return { tools: DASHBOARD_TOOL_NAMES.map((name) => ({ name })) };
    }
    if (method === "tools/call" && params && params.name === "aiworkhub_dashboard_health") {
      healthCalls += 1;
      return { content: [{ type: "text", text: JSON.stringify({ ok: true, server_version: PACKAGE_VERSION }) }] };
    }
    throw new Error(`unexpected request:${method}`);
  };

  await client._handshake();

  assert.strictEqual(healthCalls, 1, "an SDK version in serverInfo.version must still fall back to exactly one dashboard_health call");
  assert.strictEqual(client.runtimePreflight.matches, true, "the dashboard_health evidence must still establish a match");
  assert.strictEqual(client.runtimePreflight.runtimeVersion, PACKAGE_VERSION);
}

async function testRealMismatchStillRejects() {
  const host = loadExtensionHost(path.join(os.tmpdir(), "nf1435-unused-c"), path.join(os.tmpdir(), "nf1435-unused-ext-c"));
  const client = new host.extension.__testInternals.McpStdioClient(
    "/nf1435-unused-root",
    { appendLine: () => {} },
    { repoId: "repo_unused" },
    "episode-unused",
  );
  client.notify = () => {};
  client.request = async (method, params) => {
    if (method === "initialize") {
      return { protocolVersion: "2024-11-05", capabilities: {}, serverInfo: { name: "fake", version: "0.0.1-mismatch" } };
    }
    if (method === "tools/list") {
      return { tools: DASHBOARD_TOOL_NAMES.map((name) => ({ name })) };
    }
    if (method === "tools/call" && params && params.name === "aiworkhub_dashboard_health") {
      return { content: [{ type: "text", text: JSON.stringify({ ok: true, server_version: "0.0.1-mismatch" }) }] };
    }
    throw new Error(`unexpected request:${method}`);
  };

  await assert.rejects(client._handshake(), /mcp_version_mismatch_pre_service:0\.0\.1-mismatch/);
  assert.strictEqual(client.runtimePreflight.matches, false, "a real mismatch must never be reported as matching");
}

(async () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "aiworkhub-nf1435-"));
  const codexHome = path.join(tmp, "codex-home");
  fs.mkdirSync(codexHome, { recursive: true });
  process.env.CODEX_HOME = codexHome;
  await testFastPathSkipsHangingHealthCall(tmp);
  await testSdkVersionFallsBackToHealth();
  await testRealMismatchStillRejects();
  console.log("AIWorkHub vscode_lm worker preflight regression passed");
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
