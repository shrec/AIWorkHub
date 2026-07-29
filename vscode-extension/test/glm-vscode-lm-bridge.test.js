const assert = require("assert");
const fs = require("fs");
const os = require("os");
const path = require("path");
const Module = require("module");

const extensionPath = path.resolve(__dirname, "..", "extension.js");
const fakeVscode = {
  workspace: { workspaceFolders: [], getConfiguration: () => ({ get: (_key, fallback) => fallback }) },
  LanguageModelChatToolMode: { Auto: 1, Required: 2 },
  LanguageModelChatMessage: {
    User: (content) => ({ role: "user", content }),
    Assistant: (content) => ({ role: "assistant", content }),
  },
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
const deepseek = { id: "deepseek-v4-pro", family: "deepseek-v4-pro", name: "DeepSeek V4 Pro", vendor: "copilot", version: "1", capabilities: { toolCalling: true } };
assert.strictEqual(internals.selectGlm52LanguageModel([unrelated, exact]), exact);
assert.strictEqual(internals.selectGlm52LanguageModel([unrelated]), null);
assert.strictEqual(internals.selectVscodeLanguageModel([unrelated, deepseek], "deepseek-v4-pro"), deepseek);
assert.strictEqual(internals.selectVscodeLanguageModel([unrelated], "deepseek-v4-pro"), null);
assert.ok(internals.VSCODE_LM_PRIVATE_TOOLS.some((tool) => tool.name === "aiworkhub_manager_source_graph_query"));
assert.ok(!internals.VSCODE_LM_PRIVATE_TOOLS.some((tool) => /grep|find|shell/.test(tool.name)));
assert.strictEqual(internals.vscodeLmPathMatchesPattern("research/result.json", "research/*.json"), true);
assert.strictEqual(internals.vscodeLmPathMatchesPattern("../escape.json", "research/*.json"), false);

async function textProtocolChecks() {
  const toolRequest = JSON.stringify({
    schema_id: internals.constants.VSCODE_LM_TOOL_REQUEST_SCHEMA,
    name: "aiworkhub_manager_source_graph_query",
    input: { mode: "focus", query: "model", budget: 48 },
  });
  const finalResponse = JSON.stringify({
    schema_id: internals.constants.VSCODE_LM_EDIT_RESPONSE_SCHEMA,
    summary: "bounded",
    files: [{ path: "out/result.json", content: "{}\n" }],
  });
  const queued = [toolRequest, finalResponse];
  const options = [];
  const model = {
    capabilities: { toolCalling: false },
    sendRequest: async (_messages, requestOptions) => {
      options.push(requestOptions);
      const value = queued.shift();
      return { stream: (async function* stream() { yield { value }; }()) };
    },
  };
  const calls = [];
  const result = await internals.runVscodeLmTextProtocol(
    model,
    { prompt: "bounded", allowedWrites: ["out/result.json"] },
    undefined,
    async (call) => { calls.push(call); return { ok: true, content: "graph" }; },
  );
  assert.strictEqual(result, finalResponse);
  assert.strictEqual(calls.length, 1);
  assert.strictEqual(calls[0].name, "aiworkhub_manager_source_graph_query");
  assert.ok(options.every((entry) => !Object.prototype.hasOwnProperty.call(entry, "tools")));

  const premature = {
    capabilities: { toolCalling: false },
    sendRequest: async () => ({ stream: (async function* stream() { yield { value: finalResponse }; }()) }),
  };
  await assert.rejects(
    internals.runVscodeLmTextProtocol(
      premature,
      { prompt: "bounded", allowedWrites: ["out/result.json"] },
      undefined,
      async () => ({ ok: true }),
    ),
    /source_graph_not_acknowledged/,
  );

  const fencedEnvelope = `Here is the requested object:\n\`\`\`json\n${toolRequest}\n\`\`\``;
  assert.deepStrictEqual(
    internals.parseVscodeLmJsonEnvelope(fencedEnvelope),
    JSON.parse(toolRequest),
  );
  assert.throws(
    () => internals.parseVscodeLmJsonEnvelope(`${toolRequest}\n${finalResponse}`),
    /ambiguous_json/,
  );
  assert.deepStrictEqual(
    internals.parseVscodeLmJsonEnvelope(`${toolRequest}\n${toolRequest}`),
    JSON.parse(toolRequest),
  );
  assert.deepStrictEqual(
    internals.parseVscodeLmJsonEnvelope(`${toolRequest}\n${finalResponse}`, { preferFinal: true }),
    JSON.parse(finalResponse),
  );
  const laterFinal = JSON.stringify({
    schema_id: internals.constants.VSCODE_LM_EDIT_RESPONSE_SCHEMA,
    summary: "actual final",
    files: [{ path: "out/result.json", content: '{"ok":true}\n' }],
  });
  assert.deepStrictEqual(
    internals.parseVscodeLmJsonEnvelope(`${finalResponse}\nreasoning\n${laterFinal}`, { preferFinal: true }),
    JSON.parse(laterFinal),
  );
  assert.deepStrictEqual(
    internals.parseVscodeLmJsonEnvelope(JSON.stringify({
      name: "aiworkhub_manager_source_graph_query",
      arguments: JSON.stringify({ mode: "focus", query: "model" }),
    })),
    {
      schema_id: internals.constants.VSCODE_LM_TOOL_REQUEST_SCHEMA,
      name: "aiworkhub_manager_source_graph_query",
      input: { mode: "focus", query: "model" },
    },
  );
  assert.deepStrictEqual(
    internals.parseVscodeLmJsonEnvelope(JSON.stringify({
      tool_calls: [{ function: { name: "aiworkhub_manager_kb_search", arguments: '{"query":"contract"}' } }],
    })),
    {
      schema_id: internals.constants.VSCODE_LM_TOOL_REQUEST_SCHEMA,
      name: "aiworkhub_manager_kb_search",
      input: { query: "contract" },
    },
  );
  assert.throws(
    () => internals.parseVscodeLmJsonEnvelope('{"name":"shell","arguments":{"cmd":"pwd"}}'),
    /invalid_json/,
  );

  const wrappedQueued = [fencedEnvelope, `Completed:\n\`\`\`json\n${finalResponse}\n\`\`\``];
  const wrappedModel = {
    capabilities: { toolCalling: false },
    sendRequest: async () => {
      const value = wrappedQueued.shift();
      return { stream: (async function* stream() { yield { value }; }()) };
    },
  };
  const wrappedResult = await internals.runVscodeLmTextProtocol(
    wrappedModel,
    { prompt: "bounded", allowedWrites: ["out/result.json"] },
    undefined,
    async () => ({ ok: true, content: "graph" }),
  );
  assert.strictEqual(wrappedResult, finalResponse);

  const prefetchedOptions = [];
  const prefetchedCalls = [];
  const prefetchedModel = {
    capabilities: { toolCalling: false },
    sendRequest: async (messages, requestOptions) => {
      prefetchedOptions.push({ messages, requestOptions });
      return { stream: (async function* stream() { yield { value: finalResponse }; }()) };
    },
  };
  const prefetchedResult = await internals.runVscodeLmTextProtocol(
    prefetchedModel,
    {
      prompt: "bounded",
      allowedWrites: ["out/result.json"],
      allowed_writes: ["out/result.json"],
      initial_source_graph_request: { mode: "focus", query: "model", budget: 48 },
    },
    undefined,
    async (call) => { prefetchedCalls.push(call); return { ok: true, content: "live graph" }; },
  );
  assert.strictEqual(prefetchedResult, finalResponse);
  assert.strictEqual(prefetchedCalls.length, 1);
  assert.strictEqual(prefetchedCalls[0].name, "aiworkhub_manager_source_graph_query");
  assert.ok(String(prefetchedOptions[0].messages[0].content).includes("INITIAL_SOURCE_GRAPH_RESULT"));

  const wrongPath = JSON.stringify({
    schema_id: internals.constants.VSCODE_LM_EDIT_RESPONSE_SCHEMA,
    summary: "placeholder",
    files: [{ path: "repo/relative", content: "bad" }],
  });
  const correctedQueued = [wrongPath, finalResponse];
  const correctingModel = {
    capabilities: { toolCalling: false },
    sendRequest: async () => ({
      stream: (async function* stream() { yield { value: correctedQueued.shift() }; }()),
    }),
  };
  const correctedResult = await internals.runVscodeLmTextProtocol(
    correctingModel,
    {
      prompt: "bounded",
      allowedWrites: ["out/result.json"],
      initial_source_graph_request: { mode: "focus", query: "model" },
    },
    undefined,
    async () => ({ ok: true, content: "graph" }),
  );
  assert.strictEqual(correctedResult, finalResponse);
}

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
  assert.strictEqual(internals.validateVscodeLmRequest({ ...validated, model: "deepseek-v4-pro" }, repoInfo).model, "deepseek-v4-pro");
  assert.throws(() => internals.validateVscodeLmRequest({ ...validated, repo_id: `repo_${"b".repeat(32)}` }, repoInfo), /repo_id_mismatch/);
  assert.throws(() => internals.validateVscodeLmRequest({ ...validated, response_path: path.join(repo, "escape.json") }, repoInfo), /response_path_invalid/);
} finally {
  fs.rmSync(temp, { recursive: true, force: true });
}

textProtocolChecks().then(() => {
  console.log("GLM VS Code LM bridge: ok");
}).catch((err) => {
  console.error(err);
  process.exitCode = 1;
});
