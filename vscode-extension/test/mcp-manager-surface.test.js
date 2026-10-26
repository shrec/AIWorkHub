"use strict";

// NF-2026-01676: LLM-facing MCP configs carry AIWORKHUB_MCP_SURFACE=manager;
// the dashboard webview's own MCP children must never receive it.

const assert = require("assert");
const childProcess = require("child_process");
const fs = require("fs");
const Module = require("module");
const os = require("os");
const path = require("path");

const SURFACE = "AIWORKHUB_MCP_SURFACE";
const RUNTIME = "/extensions/shrec.aiworkhub/runtime";
const REPO = "/repo/fresh";
const PYTHON = { command: "python3", argsPrefix: [] };

const extensionPath = path.resolve(__dirname, "..", "extension.js");
const fakeVscode = {
  workspace: {
    workspaceFolders: [],
    getConfiguration: () => ({ get: () => "", inspect: () => ({}), update: async () => {} }),
    registerWebviewPanelSerializer: () => ({ dispose: () => {} }),
    registerWebviewViewProvider: () => ({ dispose: () => {} }),
  },
  window: {
    createOutputChannel: () => ({ appendLine: () => {}, dispose: () => {} }),
    setStatusBarMessage: () => {},
    showErrorMessage: () => {},
    showInformationMessage: () => {},
    showQuickPick: async () => undefined,
  },
  commands: { registerCommand: () => ({ dispose: () => {} }) },
  Uri: { joinPath: (...parts) => ({ fsPath: parts.map((p) => p.fsPath || p).join("/") }) },
  ViewColumn: { Active: 1 },
  ConfigurationTarget: { Global: 1 },
};
const originalLoad = Module._load;
Module._load = function patchedLoad(request, parent, isMain) {
  if (request === "vscode") return fakeVscode;
  return originalLoad.call(this, request, parent, isMain);
};
let extensionModule;
try {
  delete require.cache[extensionPath];
  extensionModule = require(extensionPath);
} finally {
  Module._load = originalLoad;
}

const {
  repairWorkspaceMcpConfigObject,
  repairClaudeMcpConfigObject,
  ensureWorkspaceMcpConfigsRepaired,
  ensureCodexMcpRegistrationTomlText,
  ensureCodexStableMcpRegistrationTomlText,
  materializeStableMcpLauncher,
  McpStdioClient,
} = extensionModule.__testInternals;

function completeEntry(extraEnv = {}) {
  return {
    command: "python3",
    args: ["-m", "aiworkhub.server"],
    env: {
      PYTHONPATH: RUNTIME,
      AIWORKHUB_REPO: REPO,
      AIWORKHUB_REPO_ROOT: REPO,
      AIWORKHUB_MCP_STDIO_BACKEND: "stdlib",
      AIWORKHUB_ALLOW_WRITES: "1",
      AIWORKHUB_ALLOW_LAUNCH: "1",
      ...extraEnv,
    },
    type: "stdio",
  };
}

async function withTempDir(prefix, body) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), prefix));
  try {
    return await body(dir);
  } finally {
    try { fs.rmSync(dir, { recursive: true, force: true }); } catch (_err) { /* best-effort cleanup */ }
  }
}

function setAmbientSurface(value) {
  if (value === undefined) delete process.env[SURFACE];
  else process.env[SURFACE] = value;
}

function testRepairedConfigObjects() {
  const targets = [
    { label: ".vscode/mcp.json", container: "servers", repair: repairWorkspaceMcpConfigObject },
    { label: ".mcp.json", container: "mcpServers", repair: repairClaudeMcpConfigObject },
  ];
  for (const { label, container, repair } of targets) {
    const other = { command: "node", args: ["server.js"], type: "stdio" };
    const fresh = repair({ [container]: { Other: other } }, RUNTIME, REPO, PYTHON);
    assert.strictEqual(fresh.changed, true, label);
    assert.strictEqual(fresh.document[container].AIWorkHub.env[SURFACE], "manager", label);
    assert.strictEqual(fresh.document[container].Other, other, label);
    assert.deepStrictEqual(other, { command: "node", args: ["server.js"], type: "stdio" }, label);

    const upgraded = repair({ [container]: { AIWorkHub: completeEntry() } }, RUNTIME, REPO, PYTHON);
    assert.strictEqual(upgraded.changed, true, `${label}: an entry without the key is upgraded`);
    assert.strictEqual(upgraded.document[container].AIWorkHub.env[SURFACE], "manager", label);
    assert.strictEqual(repair(upgraded.document, RUNTIME, REPO, PYTHON).changed, false, `${label}: stable once upgraded`);

    const pinned = repair({ [container]: { AIWorkHub: completeEntry({ [SURFACE]: "all" }) } }, RUNTIME, REPO, PYTHON);
    assert.strictEqual(pinned.changed, false, `${label}: an operator-chosen surface is not rewritten`);
    assert.strictEqual(pinned.document[container].AIWorkHub.env[SURFACE], "all", label);
  }
}

function testRepairedConfigFiles() {
  return withTempDir("aiworkhub-mcp-surface-cfg-", (root) => {
    const repo = path.join(root, "repo");
    fs.mkdirSync(repo, { recursive: true });
    const files = [
      { file: path.join(repo, ".vscode", "mcp.json"), container: "servers" },
      { file: path.join(repo, ".mcp.json"), container: "mcpServers" },
    ];
    const readEnvs = () => files.map(({ file, container }) => JSON.parse(fs.readFileSync(file, "utf8"))[container].AIWorkHub.env);
    const context = { extensionUri: { fsPath: "/extension" } };
    fakeVscode.workspace.workspaceFolders = [{ uri: { fsPath: repo } }];
    try {
      assert.strictEqual(ensureWorkspaceMcpConfigsRepaired(context), 2);
      for (const env of readEnvs()) {
        assert.strictEqual(env[SURFACE], "manager");
        assert.strictEqual(env.AIWORKHUB_MCP_STDIO_BACKEND, "stdlib");
      }
      assert.strictEqual(ensureWorkspaceMcpConfigsRepaired(context), 0, "a repaired config is stable");

      for (const { file, container } of files) {
        const document = JSON.parse(fs.readFileSync(file, "utf8"));
        document[container].AIWorkHub.env[SURFACE] = "all";
        fs.writeFileSync(file, `${JSON.stringify(document, null, 2)}\n`, "utf8");
      }
      assert.strictEqual(ensureWorkspaceMcpConfigsRepaired(context), 0);
      for (const env of readEnvs()) assert.strictEqual(env[SURFACE], "all");
    } finally {
      fakeVscode.workspace.workspaceFolders = [];
    }
  });
}

function testCodexAndLauncher() {
  const registration = ensureCodexMcpRegistrationTomlText('model = "gpt-5.5"\n', RUNTIME, PYTHON);
  assert.strictEqual(registration.changed, true);
  const tomlLines = registration.text.split("\n");
  const envHeader = tomlLines.indexOf("[mcp_servers.aiworkhub.env]");
  const backend = tomlLines.indexOf('AIWORKHUB_MCP_STDIO_BACKEND = "stdlib"');
  assert.ok(envHeader >= 0 && envHeader < backend, "backend line lives in the aiworkhub env table");
  assert.strictEqual(tomlLines[backend + 1], 'AIWORKHUB_MCP_SURFACE = "manager"');
  assert.strictEqual(ensureCodexMcpRegistrationTomlText(registration.text, RUNTIME, PYTHON).changed, false);

  return withTempDir("aiworkhub-mcp-surface-launcher-", (root) => {
    const context = { globalStorageUri: { fsPath: path.join(root, "storage") }, extensionUri: { fsPath: "/extension" } };
    const launcher = materializeStableMcpLauncher(context);
    const lines = fs.readFileSync(launcher, "utf8").split(/\r?\n/);
    const forced = lines.indexOf('os.environ["AIWORKHUB_MCP_STDIO_BACKEND"] = "stdlib"');
    assert.ok(forced >= 0, "launcher forces the stdlib backend");
    assert.strictEqual(lines[forced + 1], 'os.environ.setdefault("AIWORKHUB_MCP_SURFACE", "manager")');
    assert.ok(
      !lines.some((line) => /os\.environ\[["']AIWORKHUB_MCP_SURFACE["']\]\s*=/.test(line)),
      "launcher defaults the surface and never overwrites an explicit one",
    );

    const stable = ensureCodexStableMcpRegistrationTomlText('model = "gpt-5.5"\n', launcher, PYTHON);
    assert.strictEqual(stable.changed, true);
    assert.ok(stable.text.includes(`args = [${JSON.stringify(launcher)}]`));
    assert.ok(!/AIWORKHUB_MCP_SURFACE\s*=\s*"(?!manager")/.test(stable.text), "Codex is never pinned to another surface");
  });
}

async function captureWebviewChildSpawn(clientOptions, ambient) {
  return withTempDir("aiworkhub-mcp-surface-webview-", async (root) => {
    const captured = [];
    const originalSpawn = childProcess.spawn;
    const originalExecFile = childProcess.execFile;
    const originalAmbient = process.env[SURFACE];
    childProcess.spawn = (command, args, options) => {
      captured.push({ command, args, options, ambientSeen: process.env[SURFACE] });
      throw new Error("mcp-manager-surface: spawn stopped after capture");
    };
    childProcess.execFile = (_command, _args, _options, callback) => {
      setImmediate(() => callback(null, "", ""));
    };
    setAmbientSurface(ambient);
    let failure = null;
    try {
      const client = new McpStdioClient(root, { appendLine: () => {} }, { repoId: "repo_test" }, "episode", clientOptions);
      try {
        await client._start();
      } catch (err) {
        failure = err;
      }
    } finally {
      childProcess.spawn = originalSpawn;
      childProcess.execFile = originalExecFile;
      setAmbientSurface(originalAmbient);
    }
    assert.strictEqual(captured.length, 1, `expected exactly one spawn; start failed with: ${failure && failure.message}`);
    return { ...captured[0], root };
  });
}

async function testWebviewChildEnv() {
  const children = [
    { name: "dashboard", options: {}, writes: false, launch: false },
    { name: "manager loop", options: { grantManagerLoopGates: true }, writes: true, launch: true },
    { name: "worker bridge", options: { grantWorkerBridgeWrites: true }, writes: true, launch: false },
  ];
  for (const ambient of ["manager", "all", undefined]) {
    for (const child of children) {
      const label = `${child.name} child, ambient ${SURFACE}=${ambient}`;
      const spawned = await captureWebviewChildSpawn(child.options, ambient);
      const env = spawned.options.env;
      assert.strictEqual(spawned.ambientSeen, ambient, `${label}: the ambient value was really present`);
      assert.ok(!Object.prototype.hasOwnProperty.call(env, SURFACE), `${label}: surface leaked into the webview child`);
      assert.strictEqual(env.AIWORKHUB_MCP_STDIO_BACKEND, "stdlib", label);
      assert.strictEqual(env.AIWORKHUB_REPO_ROOT, spawned.root, label);
      assert.strictEqual("AIWORKHUB_ALLOW_WRITES" in env, child.writes, label);
      assert.strictEqual("AIWORKHUB_ALLOW_LAUNCH" in env, child.launch, label);
      assert.deepStrictEqual(spawned.args.slice(-2), ["-m", "aiworkhub.server"], label);
      assert.strictEqual(spawned.options.shell, false, label);
    }
  }
}

async function main() {
  testRepairedConfigObjects();
  await testRepairedConfigFiles();
  await testCodexAndLauncher();
  await testWebviewChildEnv();
}

main().then(
  () => console.log("mcp-manager-surface: PASS"),
  (err) => {
    console.error(err);
    process.exitCode = 1;
  },
);
