const assert = require("assert");
const fs = require("fs");
const os = require("os");
const path = require("path");
const Module = require("module");

const extensionPath = path.resolve(__dirname, "..", "extension.js");
const fakeVscode = {
  workspace: { workspaceFolders: [], getConfiguration: () => ({ get: (_key, fallback) => fallback }) },
  LanguageModelChatToolMode: { Auto: 1, Required: 2 },
};
const originalLoad = Module._load;
Module._load = function patchedLoad(request, parent, isMain) {
  if (request === "vscode") return fakeVscode;
  return originalLoad.call(this, request, parent, isMain);
};
let internals;
try {
  delete require.cache[extensionPath];
  internals = require(extensionPath).__testInternals;
} finally {
  Module._load = originalLoad;
}

const exact = { id: "glm-5.2", family: "glm-5.2", name: "GLM-5.2", vendor: "customendpoint", version: "1.0.0", capabilities: { toolCalling: true } };
const unrelated = { id: "gpt-5.4", family: "gpt-5.4", name: "GPT-5.4", vendor: "copilot", version: "1", capabilities: { toolCalling: true } };
assert.strictEqual(internals.selectGlm52LanguageModel([unrelated, exact]), exact);
assert.strictEqual(internals.selectGlm52LanguageModel([unrelated]), null);
assert.ok(internals.VSCODE_LM_PRIVATE_TOOLS.some((tool) => tool.name === "aiworkhub_manager_source_graph_query"));
assert.ok(!internals.VSCODE_LM_PRIVATE_TOOLS.some((tool) => /grep|find|shell/.test(tool.name)));

const temp = fs.mkdtempSync(path.join(os.tmpdir(), "aiworkhub-glm-bridge-test-"));
try {
  const repo = path.join(temp, "repo");
  const requestId = "d".repeat(32);
  const workspacePath = path.join(temp, requestId, "worktree");
  const workspaceHome = path.join(temp, requestId, "home");
  fs.mkdirSync(repo);
  fs.mkdirSync(workspacePath, { recursive: true });
  fs.mkdirSync(workspaceHome);
  const repoInfo = { root: repo, repoId: `repo_${"a".repeat(32)}` };
  const validated = internals.validateVscodeLmRequest({
    schema_id: internals.constants.VSCODE_LM_REQUEST_SCHEMA,
    request_id: requestId,
    repo_id: repoInfo.repoId,
    repo_root: repo,
    workspace_path: workspacePath,
    workspace_home: workspaceHome,
    response_path: path.join(workspaceHome, ".aiworkhub_vscode_lm_response.json"),
    model: "glm-5.2",
    prompt: "bounded task",
    allowed_writes: ["out/*.json"],
    deadline: new Date(Date.now() + 60000).toISOString(),
  }, repoInfo);
  assert.strictEqual(validated.requestId, requestId);
  assert.throws(() => internals.validateVscodeLmRequest({ ...validated, repo_id: `repo_${"b".repeat(32)}` }, repoInfo), /repo_id_mismatch/);
  assert.throws(() => internals.validateVscodeLmRequest({ ...validated, response_path: path.join(repo, "escape.json") }, repoInfo), /response_path_invalid/);
} finally {
  fs.rmSync(temp, { recursive: true, force: true });
}

console.log("GLM VS Code LM bridge: ok");
