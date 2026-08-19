"use strict";

// NF-2026-00243: the extension must write a resolved absolute path into
// .mcp.json (read directly by Claude Code, which cannot expand VS Code
// variables), never an unexpanded ${workspaceFolder}. .vscode/mcp.json (read by
// VS Code) flattens a repo-relative venv python to its bare interpreter name --
// VS Code does not substitute variables in `command` and its remote spawn is
// ENOENT-prone on repo-local absolute paths, so neither file carries an
// unexpanded variable in `command`. A check refuses to persist an unexpanded VS
// Code variable into any file consumed outside VS Code.

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
  containsUnexpandedVsCodeVariable,
  findUnexpandedVsCodeVariables,
  assertMcpConfigConsumable,
} = extensionModule.__testInternals;

const ABSOLUTE_PYTHON = "/repo/current/.venv/bin/python3";

// Claude Code's .mcp.json gets the RESOLVED ABSOLUTE path, never a variable.
{
  const result = repairClaudeMcpConfigObject(
    {},
    "/extensions/shrec.aiworkhub/runtime",
    "/repo/current",
    { command: ABSOLUTE_PYTHON, argsPrefix: [] },
  );
  assert.strictEqual(result.changed, true);
  const command = result.document.mcpServers.AIWorkHub.command;
  assert.strictEqual(command, ABSOLUTE_PYTHON, ".mcp.json carries the resolved absolute path");
  assert(!command.includes("${"), "no unexpanded VS Code variable in the Claude command");
  assert.strictEqual(
    findUnexpandedVsCodeVariables(result.document).length,
    0,
    "the whole Claude document is free of VS Code variables",
  );
}

// The VS Code / Copilot .vscode/mcp.json flattens a repo-relative venv python
// to its bare interpreter name. VS Code's MCP variable substitution does NOT
// apply to `command` (only `args`/`env`), and its remote spawn fails with
// ENOENT on repo-local absolute interpreter paths, while a bare name resolves
// reliably -- so `command` must never carry ${workspaceFolder} either. This
// guards against over-correcting the NF-2026-00243 .mcp.json fix into the
// VS-Code-consumed file. (workspace-mcp-runtime-repair.test.js asserts the
// same bare-name contract; both stay in agreement.)
{
  const result = repairWorkspaceMcpConfigObject(
    {},
    "/extensions/shrec.aiworkhub/runtime",
    "/repo/current",
    { command: ABSOLUTE_PYTHON, argsPrefix: [] },
  );
  assert.strictEqual(result.changed, true);
  assert.strictEqual(
    result.document.servers.AIWorkHub.command,
    "python3",
    ".vscode/mcp.json flattens the repo-relative venv python to its bare name",
  );
  assert(
    !result.document.servers.AIWorkHub.command.includes("${"),
    "no unexpanded VS Code variable in the VS Code command either",
  );
}

// The check that refuses to write an unexpanded VS Code variable, exercised with
// ${workspaceFolder} AND at least one other variable (${env:...}, ${config:...}).
{
  assert.strictEqual(containsUnexpandedVsCodeVariable("${workspaceFolder}/.venv/bin/python3"), true);
  assert.strictEqual(containsUnexpandedVsCodeVariable("${env:HOME}/tools/py"), true);
  assert.strictEqual(containsUnexpandedVsCodeVariable("${config:python.defaultInterpreterPath}"), true);
  assert.strictEqual(containsUnexpandedVsCodeVariable("${workspaceFolderBasename}"), true);
  assert.strictEqual(containsUnexpandedVsCodeVariable("/repo/current/.venv/bin/python3"), false);
  assert.strictEqual(containsUnexpandedVsCodeVariable("python3"), false);
}

// A document consumed outside VS Code that still carries VS Code variables is
// REFUSED, and the named reason lists the offending values. ${workspaceFolder}
// and ${env:HOME} are both present.
{
  const document = {
    mcpServers: {
      AIWorkHub: {
        command: "${workspaceFolder}/.venv/bin/python3",
        args: ["-m", "aiworkhub.server"],
        env: { EXTRA_PATH: "${env:HOME}/bin" },
        type: "stdio",
      },
    },
  };
  const hits = findUnexpandedVsCodeVariables(document);
  assert.strictEqual(hits.length, 2, "both variables detected");
  const values = hits.map((hit) => hit.value).join(" ");
  assert(values.includes("${workspaceFolder}"));
  assert(values.includes("${env:HOME}"));

  const verdict = assertMcpConfigConsumable(document, { vscodeVariables: false });
  assert.strictEqual(verdict.ok, false, "refused: a non-VS-Code consumer cannot expand these");
  assert(verdict.reason.includes("${workspaceFolder}"), "reason names ${workspaceFolder}");
  assert(verdict.reason.includes("${env:HOME}"), "reason names the other variable");

  // The same document IS acceptable for a VS-Code-consumed file: VS Code
  // performs the substitution before any consumer observes the value.
  assert.strictEqual(
    assertMcpConfigConsumable(document, { vscodeVariables: true }).ok,
    true,
    "VS-Code-consumed file is exempt",
  );
}

// The absolute-path Claude document produced by the repair passes the check.
{
  const result = repairClaudeMcpConfigObject(
    {},
    "/extensions/shrec.aiworkhub/runtime",
    "/repo/current",
    { command: ABSOLUTE_PYTHON, argsPrefix: [] },
  );
  assert.strictEqual(assertMcpConfigConsumable(result.document, { vscodeVariables: false }).ok, true);
}

console.log("mcp-config-paths.test.js: ok");
