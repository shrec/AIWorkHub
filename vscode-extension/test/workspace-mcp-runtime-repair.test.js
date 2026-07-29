"use strict";

const assert = require("assert");
const Module = require("module");
const path = require("path");

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
  ensureCodexMcpRegistrationTomlText,
} = extensionModule.__testInternals;

{
  const result = repairWorkspaceMcpConfigObject(
    {}, "/extensions/shrec.aiworkhub/runtime", "/repo/fresh",
    { command: "python3", argsPrefix: [] },
  );
  assert.strictEqual(result.changed, true);
  assert.strictEqual(result.document.servers.AIWorkHub.command, "python3");
  assert.strictEqual(result.document.servers.AIWorkHub.env.AIWORKHUB_REPO, "/repo/fresh");
}

{
  const result = repairClaudeMcpConfigObject(
    {}, "/extensions/shrec.aiworkhub/runtime", "/repo/fresh",
    { command: "python3", argsPrefix: [] },
  );
  assert.strictEqual(result.changed, true);
  assert.deepStrictEqual(result.document.mcpServers.AIWorkHub.args, ["-m", "aiworkhub.server"]);
}

{
  const result = ensureCodexMcpRegistrationTomlText(
    'model = "gpt-5.5"\n',
    "/extensions/shrec.aiworkhub/runtime",
    { command: "python3", argsPrefix: [] },
  );
  assert.strictEqual(result.changed, true);
  assert.ok(result.text.includes("[mcp_servers.aiworkhub]"));
  assert.ok(result.text.includes('args = ["-m", "aiworkhub.server"]'));
  assert.ok(!result.text.includes("AIWORKHUB_REPO ="));
  assert.strictEqual(
    ensureCodexMcpRegistrationTomlText(result.text, "/other/runtime", { command: "python", argsPrefix: [] }).changed,
    false,
  );
}

{
  const untouched = { command: "node", args: ["server.js"], type: "stdio" };
  const document = {
    servers: {
      Perplexity: untouched,
      AIWorkHub: {
        command: "/usr/bin/python3",
        args: ["-m", "aiworkhub.server"],
        env: {
          PYTHONPATH: "/repo/tools/geoai-task-mcp/src",
          AIWORKHUB_REPO: "/wrong/repo",
          AIWORKHUB_ALLOW_WRITES: "1",
        },
        type: "stdio",
      },
    },
  };
  const result = repairWorkspaceMcpConfigObject(
    document,
    "/extensions/shrec.aiworkhub-0.6.49/runtime",
    "/repo/current",
    { command: "/repo/current/.venv/bin/python", argsPrefix: [] },
  );
  assert.strictEqual(result.changed, true);
  assert.strictEqual(result.document.servers.Perplexity, untouched);
  assert.deepStrictEqual(result.document.servers.AIWorkHub.args, ["-m", "aiworkhub.server"]);
  assert.strictEqual(result.document.servers.AIWorkHub.command, "/repo/current/.venv/bin/python");
  assert.strictEqual(result.document.servers.AIWorkHub.env.PYTHONPATH, "/extensions/shrec.aiworkhub-0.6.49/runtime");
  assert.strictEqual(result.document.servers.AIWorkHub.env.AIWORKHUB_REPO, "/repo/current");
  assert.strictEqual(result.document.servers.AIWorkHub.env.AIWORKHUB_REPO_ROOT, "/repo/current");
  assert.strictEqual(result.document.servers.AIWorkHub.env.AIWORKHUB_ALLOW_WRITES, "1");
  assert.strictEqual(result.document.servers.AIWorkHub.env.AIWORKHUB_ALLOW_LAUNCH, "1");
}

{
  const document = {
    servers: {
      AIWorkHub: {
        command: "py",
        args: ["-3", "-m", "aiworkhub.server"],
        env: {
          PYTHONPATH: "C:\\Users\\dev\\.vscode\\extensions\\shrec.aiworkhub-0.6.49\\runtime",
          AIWORKHUB_REPO: "C:\\src\\project",
          AIWORKHUB_REPO_ROOT: "C:\\src\\project",
          AIWORKHUB_ALLOW_WRITES: "1",
          AIWORKHUB_ALLOW_LAUNCH: "1",
        },
        type: "stdio",
      },
    },
  };
  const result = repairWorkspaceMcpConfigObject(
    document,
    "C:\\Users\\dev\\.vscode\\extensions\\shrec.aiworkhub-0.6.49\\runtime",
    "C:\\src\\project",
    { command: "py", argsPrefix: ["-3"] },
  );
  assert.strictEqual(result.changed, false);
}

console.log("workspace-mcp-runtime-repair: PASS");
