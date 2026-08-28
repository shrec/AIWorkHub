"use strict";

const assert = require("assert");
const fs = require("fs");
const os = require("os");
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
  portableWorkspaceMcpCommand,
  ensureCodexMcpRegistrationTomlText,
  McpStdioClient,
  ensureWorkspaceMcpConfigsRepaired,
} = extensionModule.__testInternals;

{
  const client = new McpStdioClient("/repo", { appendLine: () => {} }, { repoId: "repo_test" }, "episode");
  const child = { pid: 42, killed: false };
  client.child = child;
  client.lifecycleChild = child;
  client.lifecyclePid = child.pid;
  let received;
  client._onMessage = (_child, line) => { received = JSON.parse(line); };
  const expected = { jsonrpc: "2.0", id: 1, result: { payload: `${"x".repeat(70 * 1024)} საქართველო 😀` } };
  const frame = Buffer.from(`${JSON.stringify(expected)}\n`, "utf8");
  const split = frame.indexOf(Buffer.from("საქართველო", "utf8")) + 1;
  client._onStdout(child, frame.subarray(0, split));
  client._onStdout(child, frame.subarray(split, split + 2));
  client._onStdout(child, frame.subarray(split + 2));
  assert.deepStrictEqual(received, expected);
  assert.ok(!JSON.stringify(received).includes("\uFFFD"));
}

{
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "aiworkhub-mcp-repair-"));
  const configPath = path.join(root, ".vscode", "mcp.json");
  fs.mkdirSync(path.dirname(configPath), { recursive: true });
  const valid = '{\n  "custom": true,\n  "servers": { "Other": { "command": "node" } }\n}\n';
  fs.writeFileSync(configPath, valid, "utf8");
  fakeVscode.workspace.workspaceFolders = [{ uri: { fsPath: root } }];
  const originalRename = fs.renameSync;
  fs.renameSync = (from, to) => { if (to === configPath) throw new Error("interrupted"); return originalRename(from, to); };
  try { ensureWorkspaceMcpConfigsRepaired({ extensionUri: { fsPath: "/extension" } }); } finally { fs.renameSync = originalRename; }
  assert.strictEqual(fs.readFileSync(configPath, "utf8"), valid);
  assert.strictEqual(ensureWorkspaceMcpConfigsRepaired({ extensionUri: { fsPath: "/extension" } }), 1);
  const preserved = JSON.parse(fs.readFileSync(configPath, "utf8"));
  assert.strictEqual(preserved.custom, true);
  assert.deepStrictEqual(preserved.servers.Other, { command: "node" });
  assert.ok(preserved.servers.AIWorkHub);
  fs.writeFileSync(configPath, "{ partial", "utf8");
  ensureWorkspaceMcpConfigsRepaired({ extensionUri: { fsPath: "/extension" } });
  const repaired = JSON.parse(fs.readFileSync(configPath, "utf8"));
  assert.ok(repaired.servers.AIWorkHub);
  for (const invalidRoot of ["[]", "null"]) {
    fs.writeFileSync(configPath, invalidRoot, "utf8");
    assert.strictEqual(ensureWorkspaceMcpConfigsRepaired({ extensionUri: { fsPath: "/extension" } }), 1);
    const normalized = JSON.parse(fs.readFileSync(configPath, "utf8"));
    assert.ok(normalized && typeof normalized === "object" && !Array.isArray(normalized));
    assert.ok(normalized.servers.AIWorkHub);
  }
  fs.writeFileSync(configPath, JSON.stringify(repaired, null, 2) + "\n", "utf8");
  const before = fs.readFileSync(configPath, "utf8");
  assert.strictEqual(ensureWorkspaceMcpConfigsRepaired({ extensionUri: { fsPath: "/extension" } }), 0);
  assert.strictEqual(fs.readFileSync(configPath, "utf8"), before);
  fs.rmSync(root, { recursive: true, force: true });
}

{
  const result = repairWorkspaceMcpConfigObject(
    {}, "/extensions/shrec.aiworkhub/runtime", "/repo/fresh",
    { command: "python3", argsPrefix: [] },
  );
  assert.strictEqual(result.changed, true);
  assert.strictEqual(result.document.servers.AIWorkHub.command, "python3");
  assert.strictEqual(result.document.servers.AIWorkHub.env.AIWORKHUB_REPO, "/repo/fresh");
  assert.strictEqual(result.document.servers.AIWorkHub.env.AIWORKHUB_MCP_STDIO_BACKEND, "stdlib");
}

{
  const result = repairClaudeMcpConfigObject(
    {}, "/extensions/shrec.aiworkhub/runtime", "/repo/fresh",
    { command: "python3", argsPrefix: [] },
  );
  assert.strictEqual(result.changed, true);
  assert.deepStrictEqual(result.document.mcpServers.AIWorkHub.args, ["-m", "aiworkhub.server"]);
  assert.strictEqual(result.document.mcpServers.AIWorkHub.env.AIWORKHUB_MCP_STDIO_BACKEND, "stdlib");
}

{
  // NF-2026-00352: command-shape assertions for the ROOT .mcp.json branch,
  // mirroring the .vscode/mcp.json block below. Their ABSENCE from this file is
  // why the regression shipped: only the VS-Code-consumed config had them, so a
  // repo-local absolute interpreter reaching Copilot's .mcp.json went unguarded.
  const untouched = { command: "node", args: ["server.js"], type: "stdio" };
  const document = {
    mcpServers: {
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
  const result = repairClaudeMcpConfigObject(
    document,
    "/extensions/shrec.aiworkhub-0.6.49/runtime",
    "/repo/current",
    { command: "/repo/current/.venv/bin/python", argsPrefix: [] },
  );
  assert.strictEqual(result.changed, true);
  assert.strictEqual(result.document.mcpServers.Perplexity, untouched);
  assert.deepStrictEqual(result.document.mcpServers.AIWorkHub.args, ["-m", "aiworkhub.server"]);
  // Repo-local venv interpreter flattens to its bare name here too.
  assert.strictEqual(result.document.mcpServers.AIWorkHub.command, "python");
  assert.strictEqual(result.document.mcpServers.AIWorkHub.env.PYTHONPATH, "/extensions/shrec.aiworkhub-0.6.49/runtime");
  assert.strictEqual(result.document.mcpServers.AIWorkHub.env.AIWORKHUB_REPO, "/repo/current");
  assert.strictEqual(result.document.mcpServers.AIWorkHub.env.AIWORKHUB_REPO_ROOT, "/repo/current");
  assert.strictEqual(result.document.mcpServers.AIWorkHub.env.AIWORKHUB_ALLOW_WRITES, "1");
  assert.strictEqual(result.document.mcpServers.AIWorkHub.env.AIWORKHUB_ALLOW_LAUNCH, "1");
  assert.strictEqual(result.document.mcpServers.AIWorkHub.env.AIWORKHUB_MCP_STDIO_BACKEND, "stdlib");
}

{
  // An interpreter configured OUTSIDE the repository is preserved unchanged in
  // the root .mcp.json branch -- the operator's choice, no relocation hazard.
  const result = repairClaudeMcpConfigObject(
    {}, "/extensions/shrec.aiworkhub/runtime", "/repo/current",
    { command: "/opt/shared/python3", argsPrefix: [] },
  );
  assert.strictEqual(result.changed, true);
  assert.strictEqual(result.document.mcpServers.AIWorkHub.command, "/opt/shared/python3");
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
  assert.strictEqual(result.document.servers.AIWorkHub.command, "python");
  assert.strictEqual(result.document.servers.AIWorkHub.env.PYTHONPATH, "/extensions/shrec.aiworkhub-0.6.49/runtime");
  assert.strictEqual(result.document.servers.AIWorkHub.env.AIWORKHUB_REPO, "/repo/current");
  assert.strictEqual(result.document.servers.AIWorkHub.env.AIWORKHUB_REPO_ROOT, "/repo/current");
  assert.strictEqual(result.document.servers.AIWorkHub.env.AIWORKHUB_ALLOW_WRITES, "1");
  assert.strictEqual(result.document.servers.AIWorkHub.env.AIWORKHUB_ALLOW_LAUNCH, "1");
  assert.strictEqual(result.document.servers.AIWorkHub.env.AIWORKHUB_MCP_STDIO_BACKEND, "stdlib");
}

{
  // Repo-local venv interpreter paths flatten to the bare interpreter name:
  // VS Code's remote MCP spawn has been observed failing with ENOENT on
  // repo-local absolute interpreter paths even when the binary exists, while
  // bare names resolve via PATH reliably. External/configured interpreters
  // and literals like ${workspaceFolder}-style placeholders never appear here.
  assert.strictEqual(
    portableWorkspaceMcpCommand("/repo/current/.venv/bin/python3", "/repo/current"),
    "python3",
  );
  assert.strictEqual(
    portableWorkspaceMcpCommand("/opt/shared/python3", "/repo/current"),
    "/opt/shared/python3",
  );
  assert.strictEqual(portableWorkspaceMcpCommand("", "/repo/current"), "");
  assert.strictEqual(portableWorkspaceMcpCommand("python3", "/repo/current"), "python3");
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
          AIWORKHUB_MCP_STDIO_BACKEND: "stdlib",
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
