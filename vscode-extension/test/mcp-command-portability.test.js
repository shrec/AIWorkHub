"use strict";

// NF-2026-00352. One config file, TWO consumer processes, and the original
// contract was written for one. repairMcpConfigObject used to compute `command`
// differently per file: the root .mcp.json branch (vscodeVariables: false) wrote
// findPythonCommand's repo-local absolute path (<repo>/.venv/bin/python3)
// VERBATIM, while only the .vscode/mcp.json branch flattened it to a bare name.
//
// But Copilot on Remote-SSH reads the ROOT .mcp.json (server name aiworkhub), and
// VS Code's remote MCP spawn resolves `command` on the SERVER side, where that
// absolute path ENOENTs -- observed repeatedly, with same-minute counter-evidence
// that the extension host spawned the identical binary fine. A bare name serves
// BOTH consumers: it is not a VS Code variable (Claude Code, which reads .mcp.json
// directly and cannot expand one, stays satisfied -- the NF-2026-00243 guarantee),
// and it resolves via PATH on the spawning host (the remote spawn works).
//
// This test constructs the repo-.venv case EXPLICITLY. It must not rely on this
// machine's .mcp.json, which is masked by an aiworkhub.pythonPath=python3 pin in
// .vscode/settings.json (both configs currently read "python3" there).

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
  findUnexpandedVsCodeVariables,
} = extensionModule.__testInternals;

// The repo-local absolute interpreter findPythonCommand returns when a repo
// .venv is present. Built here, not read from disk.
const REPO_ROOT = "/repo/current";
const REPO_LOCAL_PYTHON = "/repo/current/.venv/bin/python3";
const OUTSIDE_PYTHON = "/opt/shared/python3";
const RUNTIME_DIR = "/extensions/shrec.aiworkhub/runtime";

// 1. BOTH branches flatten the repo-local absolute interpreter to its bare name.
{
  const claude = repairClaudeMcpConfigObject(
    {}, RUNTIME_DIR, REPO_ROOT, { command: REPO_LOCAL_PYTHON, argsPrefix: [] },
  );
  assert.strictEqual(claude.changed, true);
  assert.strictEqual(
    claude.document.mcpServers.AIWorkHub.command,
    "python3",
    "root .mcp.json flattens repo-local venv python to bare name (the Copilot fix)",
  );

  const workspace = repairWorkspaceMcpConfigObject(
    {}, RUNTIME_DIR, REPO_ROOT, { command: REPO_LOCAL_PYTHON, argsPrefix: [] },
  );
  assert.strictEqual(workspace.changed, true);
  assert.strictEqual(
    workspace.document.servers.AIWorkHub.command,
    "python3",
    ".vscode/mcp.json flattens repo-local venv python to bare name",
  );
}

// 2. An interpreter configured OUTSIDE the repository is preserved unchanged in
// BOTH branches -- the operator's choice; the repo-relocation hazard is absent.
{
  const claude = repairClaudeMcpConfigObject(
    {}, RUNTIME_DIR, REPO_ROOT, { command: OUTSIDE_PYTHON, argsPrefix: [] },
  );
  assert.strictEqual(
    claude.document.mcpServers.AIWorkHub.command,
    OUTSIDE_PYTHON,
    "root .mcp.json preserves an interpreter configured outside the repository",
  );

  const workspace = repairWorkspaceMcpConfigObject(
    {}, RUNTIME_DIR, REPO_ROOT, { command: OUTSIDE_PYTHON, argsPrefix: [] },
  );
  assert.strictEqual(
    workspace.document.servers.AIWorkHub.command,
    OUTSIDE_PYTHON,
    ".vscode/mcp.json preserves an interpreter configured outside the repository",
  );
}

// 3. The NF-2026-00243 guarantee still holds: a bare name is not a variable, so
// no unexpanded VS Code variable reaches .mcp.json.
{
  const claude = repairClaudeMcpConfigObject(
    {}, RUNTIME_DIR, REPO_ROOT, { command: REPO_LOCAL_PYTHON, argsPrefix: [] },
  );
  assert(
    !claude.document.mcpServers.AIWorkHub.command.includes("${"),
    "bare name carries no VS Code variable token",
  );
  assert.strictEqual(
    findUnexpandedVsCodeVariables(claude.document).length,
    0,
    "no unexpanded VS Code variable anywhere in the Claude .mcp.json document",
  );
}

console.log("mcp-command-portability.test.js: ok");
