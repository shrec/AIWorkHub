"use strict";

// NF-2026-00243 bought ONE guarantee that still holds: no unexpanded VS Code
// variable (e.g. ${workspaceFolder}) may ever reach .mcp.json, which Claude Code
// reads directly and cannot expand. This file still asserts exactly that.
//
// NF-2026-00352 CHANGED the OTHER half. The original NF-2026-00243 reader
// concluded Claude Code was .mcp.json's only consumer and wrote the RESOLVED
// ABSOLUTE path there. It is not the only consumer: Copilot on Remote-SSH reads
// the ROOT .mcp.json too, and VS Code's remote MCP spawn resolves `command` on
// the SERVER side, where a repo-local absolute interpreter path ENOENTs even
// though the binary exists (the extension host spawned that same binary the same
// minute). So .mcp.json now flattens a REPO-LOCAL venv python to its bare name
// as well -- a bare name is not a variable (Claude Code still fine) and resolves
// via PATH (the remote spawn works). This test was updated from asserting the
// absolute path to asserting the bare name for that reason. An interpreter
// configured OUTSIDE the repository is still preserved verbatim. .vscode/mcp.json
// (read by VS Code) already flattened repo-local venv pythons for the same
// ENOENT reason. A check still refuses any unexpanded VS Code variable in a file
// consumed outside VS Code.

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

// A REPO-LOCAL venv interpreter (findPythonCommand returns this when a repo
// .venv is present). Constructed explicitly so the test does not depend on this
// machine's .mcp.json, which is masked by an aiworkhub.pythonPath=python3 pin.
const REPO_LOCAL_PYTHON = "/repo/current/.venv/bin/python3";

// NF-2026-00352: Claude Code's .mcp.json now flattens a REPO-LOCAL venv python
// to its bare name -- previously this asserted REPO_LOCAL_PYTHON verbatim, but
// Copilot's remote spawn ENOENTs on that absolute path (see the file header).
// The unchanged guarantee: still never a variable.
{
  const result = repairClaudeMcpConfigObject(
    {},
    "/extensions/shrec.aiworkhub/runtime",
    "/repo/current",
    { command: REPO_LOCAL_PYTHON, argsPrefix: [] },
  );
  assert.strictEqual(result.changed, true);
  const command = result.document.mcpServers.AIWorkHub.command;
  assert.strictEqual(command, "python3", ".mcp.json flattens the repo-local venv python to its bare name");
  assert(!command.includes("${"), "no unexpanded VS Code variable in the Claude command");
  assert.strictEqual(
    findUnexpandedVsCodeVariables(result.document).length,
    0,
    "the whole Claude document is free of VS Code variables",
  );
}

// An interpreter configured OUTSIDE the repository is the operator's choice; the
// repo-relocation hazard does not apply, so .mcp.json preserves it verbatim.
{
  const result = repairClaudeMcpConfigObject(
    {},
    "/extensions/shrec.aiworkhub/runtime",
    "/repo/current",
    { command: "/opt/shared/python3", argsPrefix: [] },
  );
  assert.strictEqual(result.changed, true);
  assert.strictEqual(
    result.document.mcpServers.AIWorkHub.command,
    "/opt/shared/python3",
    ".mcp.json preserves an interpreter configured outside the repository",
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
    { command: REPO_LOCAL_PYTHON, argsPrefix: [] },
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

// The bare-name Claude document produced by the repair passes the check.
{
  const result = repairClaudeMcpConfigObject(
    {},
    "/extensions/shrec.aiworkhub/runtime",
    "/repo/current",
    { command: REPO_LOCAL_PYTHON, argsPrefix: [] },
  );
  assert.strictEqual(assertMcpConfigConsumable(result.document, { vscodeVariables: false }).ok, true);
}

console.log("mcp-config-paths.test.js: ok");
