const assert = require("assert");
const fs = require("fs");
const os = require("os");
const path = require("path");
const Module = require("module");

const extensionPath = path.resolve(__dirname, "..", "extension.js");
const fakeVscode = {
  workspace: { workspaceFolders: [], getConfiguration: () => ({ get: (_key, fallback) => fallback }) },
  LanguageModelChatToolMode: { Auto: 1, Required: 2 },
  LanguageModelToolResultPart: class LanguageModelToolResultPart {
    constructor(callId, content) { this.callId = callId; this.content = content; }
  },
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
const internalClaude = { id: "claude-haiku-4.5", family: "claude-haiku-4.5", name: "Claude Haiku 4.5", vendor: "copilotcli", version: "1", capabilities: { toolCalling: false } };
const publicClaude = { id: "auto", family: "claude-sonnet-4.6", name: "Auto", vendor: "copilot", version: "claude-sonnet-4.6", capabilities: { toolCalling: false } };
const aliasedGlm = { id: "custom-auto", family: "glm52", name: "GLM 5.2", vendor: "customendpoint", version: "latest", capabilities: { toolCalling: true } };
const aliasedDeepseek = { id: "auto", family: "deepseek-v4pro", name: "DeepSeek V4 Pro", vendor: "copilot", version: "latest", capabilities: { toolCalling: true } };
const internalDeepseekUtility = { id: "copilot-utility", family: "copilot-utility", name: "DeepSeek V4 Flash", vendor: "copilot", version: "v4", capabilities: { toolCalling: false } };
const publicDeepseekFlash = { id: "deepseek-v4-flash", family: "deepseek", name: "DeepSeek V4 Flash", vendor: "deepseek", version: "v4", capabilities: { toolCalling: false } };
assert.strictEqual(internals.selectGlm52LanguageModel([unrelated, exact]), exact);
assert.strictEqual(internals.selectGlm52LanguageModel([unrelated]), null);
assert.strictEqual(internals.selectVscodeLanguageModel([unrelated, deepseek], "deepseek-v4-pro"), deepseek);
assert.strictEqual(internals.selectVscodeLanguageModel([unrelated], "deepseek-v4-pro"), null);
assert.strictEqual(internals.selectVscodeLanguageModel([aliasedGlm], "glm-5.2"), aliasedGlm);
assert.strictEqual(internals.selectVscodeLanguageModel([aliasedDeepseek], "deepseek-v4-pro"), aliasedDeepseek);
assert.strictEqual(internals.isCallableVscodeLmProvider(internalDeepseekUtility), false);
assert.strictEqual(
  internals.selectVscodeLanguageModel([internalDeepseekUtility, publicDeepseekFlash], "deepseek-v4-flash"),
  publicDeepseekFlash,
);
assert.ok(
  internals.vscodeLmModelSelectionRank(publicDeepseekFlash, "deepseek-v4-flash") <
    internals.vscodeLmModelSelectionRank(internalDeepseekUtility, "deepseek-v4-flash"),
);
assert.strictEqual(internals.isCallableVscodeLmProvider(internalClaude), false);
assert.strictEqual(internals.isCallableVscodeLmProvider(publicClaude), true);
assert.strictEqual(internals.isCallableVscodeLmProvider(null), false);
assert.strictEqual(internals.isCallableVscodeLmProvider(undefined), false);
fakeVscode.lm = { accessInformation: { canSendRequest: (model) => model === exact } };
assert.strictEqual(internals.vscodeLmAccessState(exact), "granted");
assert.strictEqual(internals.vscodeLmAccessState(deepseek), "not_granted");
fakeVscode.lm = {};
assert.strictEqual(internals.vscodeLmAccessState(exact), "unknown");
assert.strictEqual(internals.vscodeLmPermissionStorageKey(exact), internals.vscodeLmPermissionStorageKey(exact));
assert.notStrictEqual(internals.vscodeLmPermissionStorageKey(exact), internals.vscodeLmPermissionStorageKey(deepseek));
assert.ok(internals.VSCODE_LM_PRIVATE_TOOLS.some((tool) => tool.name === "aiworkhub_manager_source_graph_query"));
assert.ok(internals.VSCODE_LM_PRIVATE_TOOLS.some((tool) => tool.name === "aiworkhub_manager_session_write_intent"));
assert.ok(internals.VSCODE_LM_PRIVATE_TOOLS.some((tool) => tool.name === "aiworkhub_manager_ai_memory_write_intent"));
assert.ok(internals.VSCODE_LM_PRIVATE_TOOLS.some((tool) => tool.name === "aiworkhub_manager_kb_write_intent"));
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
  assert.deepStrictEqual(
    internals.parseVscodeLmJsonEnvelope(`${toolRequest}\n${finalResponse}`),
    JSON.parse(toolRequest),
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

  const invalidThenFinal = ["I should inspect the project first.", finalResponse];
  const recoveringTextModel = {
    capabilities: { toolCalling: false },
    sendRequest: async () => ({
      stream: (async function* stream() { yield { value: invalidThenFinal.shift() }; }()),
    }),
  };
  const recoveredTextResult = await internals.runVscodeLmTextProtocol(
    recoveringTextModel,
    {
      prompt: "bounded",
      allowedWrites: ["out/result.json"],
      initial_source_graph_request: { mode: "focus", query: "model" },
    },
    undefined,
    async () => ({ ok: true, content: "graph" }),
  );
  assert.strictEqual(recoveredTextResult, finalResponse);

  const textChannelModel = {
    capabilities: { toolCalling: false },
    sendRequest: async () => ({
      stream: (async function* stream() {})(),
      text: (async function* text() { yield finalResponse; }()),
    }),
  };
  const textChannelResult = await internals.runVscodeLmTextProtocol(
    textChannelModel,
    {
      prompt: "bounded",
      allowedWrites: ["out/result.json"],
      initial_source_graph_request: { mode: "focus", query: "model" },
    },
    undefined,
    async () => ({ ok: true, content: "graph" }),
  );
  assert.strictEqual(textChannelResult, finalResponse);
}

async function malformedCatalogChecks() {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), "aiworkhub-lm-null-catalog-"));
  const previousRoot = process.env.AIWORKHUB_VSCODE_LM_BRIDGE_ROOT;
  try {
    process.env.AIWORKHUB_VSCODE_LM_BRIDGE_ROOT = temp;
    fakeVscode.lm = {
      selectChatModels: async () => [null, undefined, internalClaude, exact],
      accessInformation: { canSendRequest: () => false },
    };
    const host = new internals.VscodeLmBridgeHost({ globalState: { get: () => false } });
    const repoInfo = { root: temp, repoId: `repo_${"e".repeat(32)}` };
    await host.start(repoInfo);
    const hostsDir = path.join(temp, "hosts", repoInfo.repoId);
    const files = fs.readdirSync(hostsDir);
    assert.strictEqual(files.length, 1);
    const heartbeat = JSON.parse(fs.readFileSync(path.join(hostsDir, files[0]), "utf8"));
    assert.ok(heartbeat.models.includes("glm-5.2"));
    assert.ok(heartbeat.model_metadata.every((entry) => entry && typeof entry.id === "string"));
    assert.strictEqual(heartbeat.max_parallel_requests, 3);
    assert.strictEqual(heartbeat.active_request_count, 0);
    host.dispose();

    fakeVscode.lm.selectChatModels = async () => { throw new Error("provider catalog failed"); };
    const degraded = new internals.VscodeLmBridgeHost({ globalState: { get: () => false } });
    await degraded.start(repoInfo);
    degraded.dispose();
  } finally {
    if (previousRoot === undefined) delete process.env.AIWORKHUB_VSCODE_LM_BRIDGE_ROOT;
    else process.env.AIWORKHUB_VSCODE_LM_BRIDGE_ROOT = previousRoot;
    fs.rmSync(temp, { recursive: true, force: true });
  }
}

async function permissionPersistenceChecks() {
  const remembered = new Map();
  let prompts = 0;
  fakeVscode.lm = { accessInformation: { canSendRequest: () => false } };
  fakeVscode.window = {
    showInformationMessage: async () => {
      prompts += 1;
      return "Allow VS Code models";
    },
  };
  const host = new internals.VscodeLmBridgeHost({
    globalState: {
      get: (key, fallback) => remembered.has(key) ? remembered.get(key) : fallback,
      update: async (key, value) => { remembered.set(key, value); },
    },
  });

  assert.strictEqual(await host.ensurePermission(exact), true);
  assert.strictEqual(host.modelAccessState(exact), "granted_remembered");
  assert.strictEqual(await host.ensurePermission(exact), true);
  assert.strictEqual(prompts, 1, "explicit approval must survive provider failure/retry");
}

async function boundedParallelBridgeChecks() {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), "aiworkhub-lm-parallel-"));
  const previousRoot = process.env.AIWORKHUB_VSCODE_LM_BRIDGE_ROOT;
  try {
    process.env.AIWORKHUB_VSCODE_LM_BRIDGE_ROOT = temp;
    const oldRepoInfo = { root: path.join(temp, "old"), repoId: `repo_${"1".repeat(32)}` };
    const newRepoInfo = { root: path.join(temp, "new"), repoId: `repo_${"2".repeat(32)}` };
    const requestDir = path.join(temp, "requests", oldRepoInfo.repoId);
    fs.mkdirSync(requestDir, { recursive: true });
    for (const suffix of ["a", "b", "c", "d"]) {
      fs.writeFileSync(path.join(requestDir, `${suffix.repeat(32)}.json`), "{}\n", { mode: 0o600 });
    }

    const host = new internals.VscodeLmBridgeHost({});
    host.repoInfo = oldRepoInfo;
    const releases = [];
    const observedRepos = [];
    host.processClaim = async (_claimPath, repoInfo) => {
      observedRepos.push(repoInfo.repoId);
      await new Promise((resolve) => releases.push(resolve));
    };

    const firstWave = [host.poll(), host.poll(), host.poll()];
    await new Promise((resolve) => setImmediate(resolve));
    assert.strictEqual(host.activeClaims.size, 3);
    assert.strictEqual(releases.length, 3);
    assert.strictEqual(fs.readdirSync(requestDir).filter((name) => name.endsWith(".json")).length, 1);

    await host.poll();
    assert.strictEqual(releases.length, 3, "the configured concurrency bound must hold");

    host.repoInfo = newRepoInfo;
    releases.splice(0).forEach((resolve) => resolve());
    await Promise.all(firstWave);
    assert.deepStrictEqual(observedRepos, [oldRepoInfo.repoId, oldRepoInfo.repoId, oldRepoInfo.repoId]);
    assert.strictEqual(host.activeClaims.size, 0);
  } finally {
    if (previousRoot === undefined) delete process.env.AIWORKHUB_VSCODE_LM_BRIDGE_ROOT;
    else process.env.AIWORKHUB_VSCODE_LM_BRIDGE_ROOT = previousRoot;
    fs.rmSync(temp, { recursive: true, force: true });
  }
}

async function nativeProtocolChecks() {
  const finalResponse = JSON.stringify({
    schema_id: internals.constants.VSCODE_LM_EDIT_RESPONSE_SCHEMA,
    summary: "bounded native",
    files: [{ path: "out/result.json", content: "{}\n" }],
  });
  const turns = [
    [{ callId: "call-1", name: "aiworkhub_manager_source_graph_query", input: { mode: "focus", query: "model" } }],
    [],
    [{ value: `Completed:\n\`\`\`json\n${finalResponse}\n\`\`\`` }],
  ];
  const model = {
    capabilities: { toolCalling: true },
    sendRequest: async () => {
      const parts = turns.shift();
      return { stream: (async function* stream() { for (const part of parts) yield part; }()) };
    },
  };
  const originalInvoke = internals.VSCODE_LM_PRIVATE_TOOLS;
  const result = await internals.runVscodeLmAgent(
    model,
    { requestId: "a".repeat(32), prompt: "bounded", allowedWrites: ["out/result.json"] },
    undefined,
    async () => ({ ok: true, content: "graph" }),
  );
  assert.strictEqual(result, finalResponse);
  assert.ok(originalInvoke.length > 0);

  let boundedTurns = 0;
  const boundedOptions = [];
  const loopingModel = {
    capabilities: { toolCalling: true },
    sendRequest: async (_messages, options) => {
      boundedOptions.push(options);
      boundedTurns += 1;
      if (!Object.prototype.hasOwnProperty.call(options, "tools")) {
        return { stream: (async function* stream() { yield { value: finalResponse }; }()) };
      }
      return {
        stream: (async function* stream() {
          yield {
            callId: `loop-${boundedTurns}`,
            name: "aiworkhub_manager_source_graph_query",
            input: { mode: "focus", query: `model-${boundedTurns}` },
          };
        }()),
      };
    },
  };
  const forcedFinal = await internals.runVscodeLmAgent(
    loopingModel,
    { requestId: "b".repeat(32), prompt: "bounded", allowedWrites: ["out/result.json"] },
    undefined,
    async () => ({ ok: true, content: "graph" }),
  );
  assert.strictEqual(forcedFinal, finalResponse);
  assert.strictEqual(boundedTurns, 14);
  assert.ok(!Object.prototype.hasOwnProperty.call(boundedOptions[13], "tools"));

  let emptyTurns = 0;
  const emptyOptions = [];
  const emptyLoopModel = {
    capabilities: { toolCalling: true },
    sendRequest: async (_messages, options) => {
      emptyOptions.push(options);
      emptyTurns += 1;
      if (emptyTurns === 1) {
        return {
          stream: (async function* stream() {
            yield { callId: "source", name: "aiworkhub_manager_source_graph_query", input: { mode: "focus", query: "model" } };
          }()),
        };
      }
      if (!Object.prototype.hasOwnProperty.call(options, "tools")) {
        return { stream: (async function* stream() { yield { value: finalResponse }; }()) };
      }
      return { stream: (async function* stream() {})() };
    },
  };
  const emptyForcedFinal = await internals.runVscodeLmAgent(
    emptyLoopModel,
    { requestId: "c".repeat(32), prompt: "bounded", allowedWrites: ["out/result.json"] },
    undefined,
    async () => ({ ok: true, content: "graph" }),
  );
  assert.strictEqual(emptyForcedFinal, finalResponse);
  assert.strictEqual(emptyTurns, 14);
  assert.ok(!Object.prototype.hasOwnProperty.call(emptyOptions[13], "tools"));
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

async function main() {
  await textProtocolChecks();
  await nativeProtocolChecks();
  await malformedCatalogChecks();
  await permissionPersistenceChecks();
  await boundedParallelBridgeChecks();
}

main().then(() => {
  console.log("GLM VS Code LM bridge: ok");
}).catch((err) => {
  console.error(err);
  process.exitCode = 1;
});
