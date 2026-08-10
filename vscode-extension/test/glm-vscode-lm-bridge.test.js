const assert = require("assert");
const fs = require("fs");
const os = require("os");
const path = require("path");
const Module = require("module");

const extensionPath = path.resolve(__dirname, "..", "extension.js");
const fakeCancellationSources = [];
class FakeCancellationTokenSource {
  constructor() {
    let resolveCancellation;
    const cancelled = new Promise((resolve) => { resolveCancellation = resolve; });
    this.token = { isCancellationRequested: false, cancelled };
    this.cancelCount = 0;
    this._resolveCancellation = resolveCancellation;
    fakeCancellationSources.push(this);
  }
  cancel() {
    this.cancelCount += 1;
    if (!this.token.isCancellationRequested) {
      this.token.isCancellationRequested = true;
      this._resolveCancellation();
    }
  }
  dispose() {}
}
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
  CancellationTokenSource: FakeCancellationTokenSource,
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
const firstPartyClaudeExtensionModel = { id: "claude-sonnet-5", family: "claude-sonnet-5", name: "Claude Sonnet 5", vendor: "claude-code", version: "1", capabilities: { toolCalling: false } };
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
assert.strictEqual(internals.isCallableVscodeLmProvider(firstPartyClaudeExtensionModel), false);
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
assert.ok(internals.VSCODE_LM_PRIVATE_TOOLS.some((tool) => tool.name === "aiworkhub_manager_semantic_edit_prepare"));
assert.ok(internals.VSCODE_LM_PRIVATE_TOOLS.some((tool) => tool.name === "aiworkhub_manager_session_write_intent"));
assert.ok(internals.VSCODE_LM_PRIVATE_TOOLS.some((tool) => tool.name === "aiworkhub_manager_ai_memory_write_intent"));
assert.ok(internals.VSCODE_LM_PRIVATE_TOOLS.some((tool) => tool.name === "aiworkhub_manager_kb_write_intent"));
assert.ok(!internals.VSCODE_LM_PRIVATE_TOOLS.some((tool) => /grep|find|shell/.test(tool.name)));
assert.strictEqual(
  internals.sanitizeWebviewPayload("failed at C:\\Users\\shrek\\secret.txt"),
  "failed at <redacted-host-path>",
);
assert.strictEqual(
  internals.sanitizeWebviewPayload("failed at /home/shrek/secret.txt"),
  "failed at <redacted-host-path>",
);
assert.strictEqual(
  internals.sanitizeWebviewPayload("failed at \\\\server\\share\\secret.txt"),
  "failed at <redacted-host-path>",
);
assert.strictEqual(internals.vscodeLmPathMatchesPattern("research/result.json", "research/*.json"), true);
assert.strictEqual(internals.vscodeLmPathMatchesPattern("../escape.json", "research/*.json"), false);
assert.ok(internals.glmTextToolProtocolPrompt("bounded", ["src/app.py"]).includes("mode=file with query and target both equal"));
assert.ok(internals.glmTextToolProtocolPrompt("bounded", ["src/app.py"]).includes("mode=body with query equal to the exact indexed symbol name"));
assert.ok(internals.glmTextToolProtocolPrompt("bounded", ["src/app.py"]).includes("semantic_edit_prepare"));
assert.ok(internals.glmTextToolProtocolPrompt("bounded", ["src/app.py"]).includes('"file_path":"repo/relative/path"'));
assert.ok(internals.glmTextToolProtocolPrompt("bounded", ["src/app.py"]).includes("absence of native toolCalling does not mean tools are unavailable"));
assert.ok(internals.glmTextToolProtocolPrompt("bounded", ["src/app.py"]).includes("never report that MCP/callable tools are missing"));
const nf97Prompt = internals.glmTextToolProtocolPrompt("bounded", ["src/app.py"]);
assert.ok(nf97Prompt.includes("complete, substantive replacement"));
assert.ok(nf97Prompt.includes("Every path_contract whose action is create MUST appear"));
assert.ok(!nf97Prompt.includes('"summary":"..."'));
assert.ok(!nf97Prompt.includes('"new":"replacement code only"'));
const qualityReviewTextPrompt = internals.glmTextToolProtocolPrompt(
  "bounded review", [], true, {}, "quality_review",
);
assert.ok(qualityReviewTextPrompt.includes("MUST finish by calling aiworkhub_manager_quality_review_submit"));
assert.ok(!qualityReviewTextPrompt.includes("aiworkhub_manager_semantic_edit_apply"));
assert.ok(!qualityReviewTextPrompt.includes("Output ONLY one final aiworkhub.vscode_lm.edit_response"));
assert.strictEqual(
  internals.validateVscodeLmFinalEnvelope({
    schema_id: internals.constants.VSCODE_LM_EDIT_RESPONSE_SCHEMA,
    summary: "v2",
    edits: [{
      path: "src/app.py",
      current_sha256: "a".repeat(64),
      ranges: [{ start_line: 10, end_line: 12, new: "after", preserve_trailing_newline: true }],
    }],
    creates: [],
  }, ["src/*.py"]),
  "",
);
assert.strictEqual(
  internals.validateVscodeLmFinalEnvelope({
    schema_id: internals.constants.VSCODE_LM_EDIT_RESPONSE_SCHEMA_V1,
    summary: "v1",
    files: [{ path: "src/app.py", content: "complete\n" }],
  }, ["src/*.py"]),
  "",
);
assert.match(
  internals.validateVscodeLmFinalEnvelope({
    schema_id: internals.constants.VSCODE_LM_EDIT_RESPONSE_SCHEMA,
    summary: "bad",
    edits: [{ path: "src/app.py", current_sha256: "A".repeat(64), ranges: [] }],
    creates: [],
  }, ["src/*.py"]),
  /final_hash_invalid/,
);

for (const sentinel of ["…", "replacement code only", "file content", "TODO", "# FIXME", "implementation omitted"]) {
  assert.match(
    internals.validateVscodeLmFinalEnvelope({
      schema_id: internals.constants.VSCODE_LM_EDIT_RESPONSE_SCHEMA,
      summary: "actual change",
      edits: [{
        path: "src/app.py",
        current_sha256: "a".repeat(64),
        ranges: [{ start_line: 1, end_line: 1, new: sentinel }],
      }],
      creates: [],
    }, ["src/*.py"]),
    /final_edit_fidelity_rejected/,
  );
}
let deeplyNestedSentinel = "replacement code only";
for (let depth = 0; depth < 8; depth += 1) {
  const fence = depth % 2 === 0 ? "```" : "~~~";
  deeplyNestedSentinel = `${fence}\n${deeplyNestedSentinel}\n${fence}`;
}
for (const wrappedSentinel of [
  "```text\nreplacement code only\n```",
  "```\n...\n```",
  "```\nreplacement code only\n````",
  "```python title=generated replacement\nfile content\n```",
  "~~~text title=generated replacement\nTODO\n~~~~",
  "```text\nreplacement code only\n   ```",
  "~~~text\nfile content\n  ~~~",
  "   ```text\nreplacement code only\n   ````",
  "  ~~~text\nfile content\n ~~~~",
  "```text\r\nreplacement code only\r\n   ```",
  deeplyNestedSentinel,
  "/* file content */",
  "<!-- implementation omitted -->",
  "/// FIXME: implement this",
  "// …",
  "．．．",
  "ｒｅｐｌａｃｅｍｅｎｔ　ｃｏｄｅ　ｏｎｌｙ",
]) {
  assert.match(
    internals.validateVscodeLmFinalEnvelope({
      schema_id: internals.constants.VSCODE_LM_EDIT_RESPONSE_SCHEMA,
      summary: "actual change",
      edits: [{
        path: "src/app.py",
        current_sha256: "a".repeat(64),
        ranges: [{ start_line: 1, end_line: 1, new: wrappedSentinel }],
      }],
      creates: [],
    }, ["src/*.py"]),
    /final_edit_fidelity_rejected/,
  );
}
for (const nonWrapperFence of [
  "    ```text\nreplacement code only\n```",
  "```text\nreplacement code only\n    ```",
  "```text\nreplacement code only\n~~~",
  "````text\nreplacement code only\n```",
  "<!--\n    ```text\nreplacement code only\n```\n-->",
]) {
  assert.strictEqual(
    internals.validateVscodeLmFinalEnvelope({
      schema_id: internals.constants.VSCODE_LM_EDIT_RESPONSE_SCHEMA,
      summary: "literal non-wrapper fence content",
      edits: [{
        path: "src/app.py",
        current_sha256: "a".repeat(64),
        ranges: [{ start_line: 1, end_line: 1, new: nonWrapperFence }],
      }],
      creates: [],
    }, ["src/*.py"]),
    "",
  );
}
assert.strictEqual(
  internals.validateVscodeLmFinalEnvelope({
    schema_id: internals.constants.VSCODE_LM_EDIT_RESPONSE_SCHEMA,
    summary: "possible abstract stub",
    edits: [{
      path: "src/app.py",
      current_sha256: "a".repeat(64),
      ranges: [{ start_line: 1, end_line: 1, new: "..." }],
    }],
    creates: [],
  }, ["src/*.py"]),
  "",
);
assert.strictEqual(
  internals.validateVscodeLmFinalEnvelope({
    schema_id: internals.constants.VSCODE_LM_EDIT_RESPONSE_SCHEMA,
    summary: "document markers",
    edits: [{
      path: "src/app.js",
      current_sha256: "a".repeat(64),
      ranges: [{ start_line: 1, end_line: 2, new: 'const note = "TODO";\n// FIXME is supported documentation\nreturn note;' }],
    }],
    creates: [],
  }, ["src/*.js"]),
  "",
);
for (const substantiveFence of [
  "```python title=reviewed replacement\n# TODO is documented\nreturn 1\n   ````",
  "  ~~~python title=reviewed replacement\n# TODO is documented\nreturn 1\n ~~~~",
  "   ```python title=reviewed replacement\r\n# TODO is documented\r\nreturn 1\r\n  ```",
]) {
  assert.strictEqual(
    internals.validateVscodeLmFinalEnvelope({
      schema_id: internals.constants.VSCODE_LM_EDIT_RESPONSE_SCHEMA,
      summary: "substantive fenced code",
      edits: [{
        path: "src/app.py",
        current_sha256: "a".repeat(64),
        ranges: [{ start_line: 1, end_line: 2, new: substantiveFence }],
      }],
      creates: [],
    }, ["src/*.py"]),
    "",
  );
}
assert.strictEqual(
  internals.validateVscodeLmFinalEnvelope({
    schema_id: internals.constants.VSCODE_LM_EDIT_RESPONSE_SCHEMA,
    summary: "inline fence text",
    edits: [{
      path: "src/app.py",
      current_sha256: "a".repeat(64),
      ranges: [{ start_line: 1, end_line: 2, new: 'const marker = "```not a whole fence```";\nreturn marker;' }],
    }],
    creates: [],
  }, ["src/*.py"]),
  "",
);
assert.strictEqual(
  internals.validateVscodeLmFinalEnvelope({
    schema_id: internals.constants.VSCODE_LM_EDIT_RESPONSE_SCHEMA,
    summary: "type stub",
    edits: [{
      path: "src/api.pyi",
      current_sha256: "a".repeat(64),
      ranges: [{ start_line: 1, end_line: 1, new: "..." }],
    }],
    creates: [],
  }, ["src/*.pyi"]),
  "",
);
const requiredCreateContract = {
  "tests/new.py": { action: "create", current_sha256: "", line_count: 0, parent_existed: false },
};
const requiredCreateCases = [
  {
    missing: { schema_id: internals.constants.VSCODE_LM_EDIT_RESPONSE_SCHEMA_V1, summary: "missing v1", files: [] },
    empty: { schema_id: internals.constants.VSCODE_LM_EDIT_RESPONSE_SCHEMA_V1, summary: "empty v1", files: [{ path: "tests/new.py", content: " \n" }] },
    valid: { schema_id: internals.constants.VSCODE_LM_EDIT_RESPONSE_SCHEMA_V1, summary: "valid v1", files: [{ path: "tests/new.py", content: "VALUE = 1\n" }] },
  },
  {
    missing: { schema_id: internals.constants.VSCODE_LM_EDIT_RESPONSE_SCHEMA_V2, summary: "missing v2", edits: [], creates: [] },
    empty: { schema_id: internals.constants.VSCODE_LM_EDIT_RESPONSE_SCHEMA_V2, summary: "empty v2", edits: [], creates: [{ path: "tests/new.py", content: " \n" }] },
    valid: { schema_id: internals.constants.VSCODE_LM_EDIT_RESPONSE_SCHEMA_V2, summary: "valid v2", edits: [], creates: [{ path: "tests/new.py", content: "VALUE = 1\n" }] },
  },
  {
    missing: { schema_id: internals.constants.VSCODE_LM_EDIT_RESPONSE_SCHEMA, summary: "missing v3", edits: [], creates: [] },
    empty: { schema_id: internals.constants.VSCODE_LM_EDIT_RESPONSE_SCHEMA, summary: "empty v3", edits: [], creates: [{ path: "tests/new.py", content: " \n" }] },
    valid: { schema_id: internals.constants.VSCODE_LM_EDIT_RESPONSE_SCHEMA, summary: "valid v3", edits: [], creates: [{ path: "tests/new.py", content: "VALUE = 1\n" }] },
  },
];
for (const createCase of requiredCreateCases) {
  assert.match(
    internals.validateVscodeLmFinalEnvelope(createCase.missing, ["tests/*.py"], requiredCreateContract),
    /missing_required_create/,
  );
  assert.match(
    internals.validateVscodeLmFinalEnvelope(createCase.empty, ["tests/*.py"], requiredCreateContract),
    /empty_required_create/,
  );
  assert.strictEqual(
    internals.validateVscodeLmFinalEnvelope(createCase.valid, ["tests/*.py"], requiredCreateContract),
    "",
  );
}
const pyiCreateContract = {
  "tests/new.pyi": { action: "create", current_sha256: "", line_count: 0, parent_existed: false },
};
const pyiCreateCases = [
  (content) => ({
    schema_id: internals.constants.VSCODE_LM_EDIT_RESPONSE_SCHEMA_V1,
    summary: "v1 pyi create",
    files: [{ path: "tests\\new.pyi", content }],
  }),
  (content) => ({
    schema_id: internals.constants.VSCODE_LM_EDIT_RESPONSE_SCHEMA_V2,
    summary: "v2 pyi create",
    edits: [],
    creates: [{ path: "tests/new.pyi", content }],
  }),
  (content) => ({
    schema_id: internals.constants.VSCODE_LM_EDIT_RESPONSE_SCHEMA,
    summary: "v3 pyi create",
    edits: [],
    creates: [{ path: "tests/new.pyi", content }],
  }),
];
for (const makePyiCreate of pyiCreateCases) {
  for (const ellipsis of ["...", "…", "．．．"]) {
    assert.match(
      internals.validateVscodeLmFinalEnvelope(
        makePyiCreate(ellipsis), ["tests/*.pyi"], pyiCreateContract,
      ),
      /ellipsis_only/,
    );
  }
  for (const wrappedEmpty of [
    "```python title=generated stub\n\n````",
    "~~~text title=generated stub\n\n~~~~",
  ]) {
    assert.match(
      internals.validateVscodeLmFinalEnvelope(
        makePyiCreate(wrappedEmpty), ["tests/*.pyi"], pyiCreateContract,
      ),
      /empty_required_create/,
    );
  }
}
assert.strictEqual(
  internals.validateVscodeLmFinalEnvelope(
    pyiCreateCases[0]("class Created:\n    value: int\n"),
    ["tests/*.pyi"],
    pyiCreateContract,
  ),
  "",
);

async function textProtocolChecks() {
  const toolRequest = JSON.stringify({
    schema_id: internals.constants.VSCODE_LM_TOOL_REQUEST_SCHEMA,
    name: "aiworkhub_manager_source_graph_query",
    input: { mode: "focus", query: "model", budget: 48 },
  });
  const finalResponse = JSON.stringify({
    schema_id: internals.constants.VSCODE_LM_EDIT_RESPONSE_SCHEMA,
    summary: "bounded",
    edits: [],
    creates: [{ path: "out/result.json", content: "{}\n" }],
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

  const reviewSubmit = JSON.stringify({
    schema_id: internals.constants.VSCODE_LM_TOOL_REQUEST_SCHEMA,
    name: "aiworkhub_manager_quality_review_submit",
    input: { packet_sha256: "a".repeat(64), lens: "correctness", findings: [] },
  });
  const reviewSubmissionId = "b".repeat(64);
  const reviewQueued = [reviewSubmit];
  const reviewCalls = [];
  const reviewModel = {
    capabilities: { toolCalling: false },
    sendRequest: async () => ({
      stream: (async function* stream() { yield { value: reviewQueued.shift() }; }()),
    }),
  };
  const reviewResult = await internals.runVscodeLmTextProtocol(
    reviewModel,
    { prompt: "bounded review", request_kind: "quality_review", allowedWrites: [] },
    undefined,
    async (call) => {
      reviewCalls.push(call);
      return { ok: true, durable: true, submission_id: reviewSubmissionId };
    },
  );
  assert.deepStrictEqual(JSON.parse(reviewResult), {
    schema_id: internals.constants.VSCODE_LM_EDIT_RESPONSE_SCHEMA,
    summary: `quality review submitted:${reviewSubmissionId}`,
    edits: [],
    creates: [],
  });
  assert.deepStrictEqual(reviewCalls.map((call) => call.name), [
    "aiworkhub_manager_quality_review_submit",
  ]);

  let proseOnlyTurns = 0;
  const proseOnlyReview = {
    capabilities: { toolCalling: false },
    sendRequest: async () => {
      proseOnlyTurns += 1;
      return { stream: (async function* stream() { yield { value: finalResponse }; }()) };
    },
  };
  await assert.rejects(
    internals.runVscodeLmTextProtocol(
      proseOnlyReview,
      { prompt: "bounded review", request_kind: "quality_review", allowedWrites: [] },
      undefined,
      async () => ({ ok: true }),
    ),
    /vscode_lm_quality_review_submit_required/,
  );
  assert.strictEqual(proseOnlyTurns, 1);

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
    edits: [],
    creates: [{ path: "out/result.json", content: '{"ok":true}\n' }],
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
      path_contracts: {
        "out/result.json": {
          action: "create",
          current_sha256: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
          parent_existed: false,
        },
      },
      initial_source_graph_request: { mode: "focus", query: "model", budget: 48 },
    },
    undefined,
    async (call) => { prefetchedCalls.push(call); return { ok: true, content: "live graph" }; },
  );
  assert.strictEqual(prefetchedResult, finalResponse);
  assert.strictEqual(prefetchedCalls.length, 1);
  assert.strictEqual(prefetchedCalls[0].name, "aiworkhub_manager_source_graph_query");
  assert.ok(String(prefetchedOptions[0].messages[0].content).includes("INITIAL_SOURCE_GRAPH_RESULT"));
  assert.ok(String(prefetchedOptions[0].messages[0].content).includes('"action":"create"'));
  assert.ok(String(prefetchedOptions[0].messages[0].content).includes("e3b0c44298fc1c149"));

  const coordinatorPrefetchCalls = [];
  const coordinatorPrefetchedResult = await internals.runVscodeLmTextProtocol(
    prefetchedModel,
    {
      prompt: "bounded",
      allowedWrites: ["out/result.json"],
      allowed_writes: ["out/result.json"],
      path_contracts: {},
      initial_source_graph_request: { mode: "focus", query: "model", budget: 48 },
      initial_source_graph_result: {
        ok: true,
        tool: "source_graph",
        mode: "focus",
        workflow_stage: "orientation",
        content: "prefetched graph",
      },
    },
    undefined,
    async (call) => {
      coordinatorPrefetchCalls.push(call);
      throw new Error("coordinator_prefetch_must_not_requery_transport");
    },
  );
  assert.strictEqual(coordinatorPrefetchedResult, finalResponse);
  assert.strictEqual(coordinatorPrefetchCalls.length, 0);

  await assert.rejects(
    internals.runVscodeLmTextProtocol(
      prefetchedModel,
      {
        prompt: "bounded",
        allowedWrites: [],
        initial_source_graph_request: {
          mode: "focus", query: "DBAccountStatus", budget: 48,
        },
      },
      undefined,
      async () => { throw new Error("database is locked"); },
    ),
    (error) => {
      assert.strictEqual(error.message, "vscode_lm_initial_source_graph_failed");
      assert.strictEqual(error.protocolPhase, "initial_source_graph");
      assert.match(error.protocolCause, /database is locked/);
      assert.strictEqual(error.protocolRequest.query, "DBAccountStatus");
      assert.strictEqual(error.protocolTrace[0].phase, "initial_source_graph");
      return true;
    },
  );

  const wrongPath = JSON.stringify({
    schema_id: internals.constants.VSCODE_LM_EDIT_RESPONSE_SCHEMA,
    summary: "placeholder",
    edits: [],
    creates: [{ path: "repo/relative", content: "bad" }],
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

  const editContract = {
    "src/app.py": { action: "edit", current_sha256: "a".repeat(64), line_count: 2, parent_existed: true },
  };
  const staleV3 = JSON.stringify({
    schema_id: internals.constants.VSCODE_LM_EDIT_RESPONSE_SCHEMA,
    summary: "stale v3",
    edits: [{ path: "src/app.py", current_sha256: "b".repeat(64), ranges: [{ start_line: 2, end_line: 2, new: "stale" }] }],
    creates: [],
  });
  const freshV3 = JSON.stringify({
    schema_id: internals.constants.VSCODE_LM_EDIT_RESPONSE_SCHEMA,
    summary: "fresh v3",
    edits: [{ path: "src/app.py", current_sha256: "a".repeat(64), ranges: [{ start_line: 2, end_line: 2, new: "fresh" }] }],
    creates: [],
  });
  const v3HashTurns = [staleV3, freshV3];
  const v3HashMessages = [];
  const v3HashModel = {
    capabilities: { toolCalling: false },
    sendRequest: async (messages) => {
      v3HashMessages.push(JSON.stringify(messages));
      return { stream: (async function* stream() { yield { value: v3HashTurns.shift() }; }()) };
    },
  };
  const v3HashResult = await internals.runVscodeLmTextProtocol(
    v3HashModel,
    {
      prompt: "bounded hash retry",
      allowedWrites: ["src/app.py"],
      path_contracts: editContract,
      initial_source_graph_request: { mode: "focus", query: "hash contract" },
      initial_source_graph_result: { ok: true, content: "prefetched graph" },
    },
    undefined,
    async () => { throw new Error("prefetched_source_graph_must_not_requery"); },
  );
  assert.strictEqual(v3HashResult, freshV3);
  assert.strictEqual(v3HashMessages.length, 2);
  assert.ok(v3HashMessages[1].includes("final_hash_stale:src/app.py"));

  const copiedSentinelV3 = JSON.stringify({
    schema_id: internals.constants.VSCODE_LM_EDIT_RESPONSE_SCHEMA,
    summary: "attempted bounded change",
    edits: [{ path: "src/app.py", current_sha256: "a".repeat(64), ranges: [{ start_line: 2, end_line: 2, new: deeplyNestedSentinel }] }],
    creates: [],
  });
  const substantiveV3 = JSON.stringify({
    schema_id: internals.constants.VSCODE_LM_EDIT_RESPONSE_SCHEMA,
    summary: "implemented bounded change",
    edits: [{ path: "src/app.py", current_sha256: "a".repeat(64), ranges: [{ start_line: 2, end_line: 2, new: "return 2" }] }],
    creates: [],
  });
  const fidelityTurns = [copiedSentinelV3, substantiveV3];
  const fidelityMessages = [];
  const fidelityModel = {
    capabilities: { toolCalling: false },
    sendRequest: async (messages) => {
      fidelityMessages.push(JSON.stringify(messages));
      return { stream: (async function* stream() { yield { value: fidelityTurns.shift() }; }()) };
    },
  };
  const fidelityResult = await internals.runVscodeLmTextProtocol(
    fidelityModel,
    {
      prompt: "bounded fidelity retry",
      allowedWrites: ["src/app.py"],
      path_contracts: editContract,
      initial_source_graph_request: { mode: "focus", query: "fidelity contract" },
      initial_source_graph_result: { ok: true, content: "prefetched graph" },
    },
    undefined,
    async () => { throw new Error("prefetched_source_graph_must_not_requery"); },
  );
  assert.strictEqual(fidelityResult, substantiveV3);
  assert.strictEqual(fidelityMessages.length, 2);
  assert.ok(fidelityMessages[1].includes("final_edit_fidelity_rejected"));

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

  const emptyTextChannelModel = {
    capabilities: { toolCalling: false },
    sendRequest: async () => ({
      text: (async function* text() {})(),
      stream: (async function* stream() { yield { value: finalResponse }; }()),
    }),
  };
  const streamFallbackResult = await internals.runVscodeLmTextProtocol(
    emptyTextChannelModel,
    {
      prompt: "bounded",
      allowedWrites: ["out/result.json"],
      initial_source_graph_request: { mode: "focus", query: "model", budget: 48 },
    },
    undefined,
    async () => ({ ok: true, content: "graph" }),
  );
  assert.strictEqual(streamFallbackResult, finalResponse);

  const unsupportedPartModel = {
    capabilities: { toolCalling: false },
    sendRequest: async () => ({
      text: (async function* text() { yield { metadata: true }; }()),
      stream: (async function* stream() { yield { value: { nested: true }, marker: "opaque" }; }()),
    }),
  };
  await assert.rejects(
    internals.runVscodeLmTextProtocol(
      unsupportedPartModel,
      {
        prompt: "bounded",
        allowedWrites: [],
        initial_source_graph_request: { mode: "focus", query: "model", budget: 48 },
      },
      undefined,
      async () => ({ ok: true, content: "graph" }),
    ),
    (error) => {
      assert.match(error.message, /vscode_lm_finalization_limit/);
      const first = error.protocolTrace[0];
      assert.strictEqual(first.outcome, "empty");
      assert.strictEqual(first.response.text_channel_available, true);
      assert.strictEqual(first.response.stream_channel_available, true);
      assert.deepStrictEqual(first.response.text_parts, []);
      assert.strictEqual(first.response.stream_parts[0].value_type, "object");
      return true;
    },
  );
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

  let resolvePrompt;
  fakeVscode.window.showInformationMessage = () => new Promise((resolve) => { resolvePrompt = resolve; });
  const cancelledRemembered = new Map();
  const cancelledHost = new internals.VscodeLmBridgeHost({
    globalState: {
      get: (key, fallback) => cancelledRemembered.has(key) ? cancelledRemembered.get(key) : fallback,
      update: async (key, value) => { cancelledRemembered.set(key, value); },
    },
  });
  const permissionSource = new FakeCancellationTokenSource();
  const permission = cancelledHost.ensurePermission(exact, permissionSource.token);
  await new Promise((resolve) => setImmediate(resolve));
  permissionSource.cancel();
  await assert.rejects(permission, /vscode_lm_request_cancelled/);
  resolvePrompt("Allow VS Code models");
  await new Promise((resolve) => setImmediate(resolve));
  assert.strictEqual(cancelledRemembered.size, 0, "cancelled permission must not persist later approval");
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
    edits: [],
    creates: [{ path: "out/result.json", content: "{}\n" }],
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

  const nativePrefetchCalls = [];
  const nativePrefetchMessages = [];
  const nativePrefetchOptions = [];
  const nativePrefetchModel = {
    capabilities: { toolCalling: true },
    sendRequest: async (messages, options) => {
      nativePrefetchMessages.push(messages);
      nativePrefetchOptions.push(options);
      return { stream: (async function* stream() { yield { value: finalResponse }; }()) };
    },
  };
  const nativePrefetched = await internals.runVscodeLmAgent(
    nativePrefetchModel,
    {
      requestId: "9".repeat(32),
      prompt: "bounded",
      allowedWrites: ["out/result.json"],
      initial_source_graph_request: { mode: "focus", query: "model" },
      initial_source_graph_result: {
        ok: true,
        tool: "source_graph",
        mode: "focus",
        workflow_stage: "orientation",
        content: "prefetched native graph",
      },
    },
    undefined,
    async (call) => { nativePrefetchCalls.push(call); return { ok: true }; },
  );
  assert.strictEqual(nativePrefetched, finalResponse);
  assert.strictEqual(nativePrefetchCalls.length, 0);
  assert.ok(String(nativePrefetchMessages[0][0].content).includes("INITIAL_SOURCE_GRAPH_RESULT"));
  assert.strictEqual(nativePrefetchOptions[0].toolMode, fakeVscode.LanguageModelChatToolMode.Auto);

  const editContract = {
    "src/app.py": { action: "edit", current_sha256: "a".repeat(64), line_count: 2, parent_existed: true },
  };
  const staleV2 = JSON.stringify({
    schema_id: internals.constants.VSCODE_LM_EDIT_RESPONSE_SCHEMA_V2,
    summary: "stale v2",
    edits: [{ path: "src/app.py", current_sha256: "b".repeat(64), replacements: [{ old: "before", new: "stale", expected_count: 1 }] }],
    creates: [],
  });
  const freshV2 = JSON.stringify({
    schema_id: internals.constants.VSCODE_LM_EDIT_RESPONSE_SCHEMA_V2,
    summary: "fresh v2",
    edits: [{ path: "src/app.py", current_sha256: "a".repeat(64), replacements: [{ old: "before", new: "fresh", expected_count: 1 }] }],
    creates: [],
  });
  const v2HashTurns = [staleV2, freshV2];
  const v2HashMessages = [];
  const v2HashModel = {
    capabilities: { toolCalling: true },
    sendRequest: async (messages) => {
      v2HashMessages.push(JSON.stringify(messages));
      const value = v2HashTurns.shift();
      return { stream: (async function* stream() { yield { value }; }()) };
    },
  };
  const v2HashResult = await internals.runVscodeLmAgent(
    v2HashModel,
    {
      requestId: "7".repeat(32),
      prompt: "bounded hash retry",
      allowedWrites: ["src/app.py"],
      path_contracts: editContract,
      initial_source_graph_result: { ok: true, content: "prefetched graph" },
    },
    undefined,
    async () => { throw new Error("prefetched_source_graph_must_not_requery"); },
  );
  assert.strictEqual(v2HashResult, freshV2);
  assert.strictEqual(v2HashMessages.length, 2);
  assert.ok(v2HashMessages[1].includes("final_hash_stale:src/app.py"));

  let reviewTurn = 0;
  const nativeReviewSubmissionId = "c".repeat(64);
  const nativeReviewCalls = [];
  const nativeReviewOptions = [];
  const nativeReviewModel = {
    capabilities: { toolCalling: true },
    sendRequest: async (_messages, options) => {
      nativeReviewOptions.push(options);
      reviewTurn += 1;
      if (reviewTurn === 1) {
        return {
          stream: (async function* stream() {
            yield {
              callId: "review-submit-1",
              name: "aiworkhub_manager_quality_review_submit",
              input: { packet_sha256: "a".repeat(64), lens: "security", findings: [] },
            };
          }()),
        };
      }
      throw new Error("durable review submit must end without another provider turn");
    },
  };
  const nativeReview = await internals.runVscodeLmAgent(
    nativeReviewModel,
    {
      requestId: "8".repeat(32),
      request_kind: "quality_review",
      prompt: "bounded review",
      allowedWrites: [],
      path_contracts: {},
    },
    undefined,
    async (call) => {
      nativeReviewCalls.push(call);
      return { ok: true, durable: true, submission_id: nativeReviewSubmissionId };
    },
  );
  assert.deepStrictEqual(JSON.parse(nativeReview), {
    schema_id: internals.constants.VSCODE_LM_EDIT_RESPONSE_SCHEMA,
    summary: `quality review submitted:${nativeReviewSubmissionId}`,
    edits: [],
    creates: [],
  });
  assert.strictEqual(reviewTurn, 1);
  assert.strictEqual(nativeReviewCalls[0].name, "aiworkhub_manager_quality_review_submit");
  assert.strictEqual(nativeReviewOptions[0].toolMode, fakeVscode.LanguageModelChatToolMode.Required);
  assert.deepStrictEqual(
    nativeReviewOptions[0].tools.map((tool) => tool.name),
    ["aiworkhub_manager_source_graph_query", "aiworkhub_manager_quality_review_submit"],
  );

  let nativeProseOnlyTurns = 0;
  const nativeProseOnlyReview = {
    capabilities: { toolCalling: true },
    sendRequest: async () => {
      nativeProseOnlyTurns += 1;
      return {
        stream: (async function* stream() {
          yield { value: finalResponse };
        }()),
      };
    },
  };
  await assert.rejects(
    internals.runVscodeLmAgent(
      nativeProseOnlyReview,
      {
        requestId: "9".repeat(32),
        request_kind: "quality_review",
        prompt: "bounded review",
        allowedWrites: [],
        path_contracts: {},
      },
      undefined,
      async () => ({ ok: true }),
    ),
    /vscode_lm_quality_review_submit_required/,
  );
  assert.strictEqual(nativeProseOnlyTurns, 1);

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
  assert.strictEqual(validated.request_kind, "worker");
  assert.strictEqual(
    internals.validateVscodeLmRequest(
      { ...validated, request_kind: "quality_review" }, repoInfo,
    ).request_kind,
    "quality_review",
  );
  assert.throws(
    () => internals.validateVscodeLmRequest({ ...validated, request_kind: "other" }, repoInfo),
    /request_kind_invalid/,
  );
  assert.strictEqual(internals.validateVscodeLmRequest({ ...validated, model: "deepseek-v4-pro" }, repoInfo).model, "deepseek-v4-pro");
  assert.throws(() => internals.validateVscodeLmRequest({ ...validated, repo_id: `repo_${"b".repeat(32)}` }, repoInfo), /repo_id_mismatch/);
  assert.throws(() => internals.validateVscodeLmRequest({ ...validated, response_path: path.join(repo, "escape.json") }, repoInfo), /response_path_invalid/);
} finally {
  fs.rmSync(temp, { recursive: true, force: true });
}

async function main() {
  const schema = internals.constants.VSCODE_LM_EDIT_RESPONSE_SCHEMA;
  const allowed = ["src/*.py", "tests/*.py"];
  const contracts = {
    "src/app.py": { action: "edit", current_sha256: "a".repeat(64), line_count: 2, parent_existed: true },
  };
  const contractsWithCreate = {
    ...contracts,
    "tests/new.py": { action: "create", current_sha256: "", line_count: 0, parent_existed: false },
  };
  const repaired = {
    schema_id: schema,
    summary: "repair hash",
    edits: [{ path: "src/app.py", ranges: [{ start_line: 2, end_line: 2, new: "fixed" }] }],
    creates: [],
  };
  assert.strictEqual(internals.validateVscodeLmFinalEnvelope(repaired, allowed, contracts), "");
  assert.strictEqual(repaired.edits[0].current_sha256, "a".repeat(64));
  const hashRetry = {
    ...repaired,
    edits: [{ path: "src/app.py", current_sha256: "b".repeat(64), ranges: [{ start_line: 2, end_line: 2, new: "fixed" }] }],
  };
  assert.strictEqual(
    internals.validateVscodeLmFinalEnvelope(hashRetry, allowed, contracts),
    "final_hash_stale:src/app.py",
  );
  hashRetry.edits[0].current_sha256 = "a".repeat(64);
  assert.strictEqual(internals.validateVscodeLmFinalEnvelope(hashRetry, allowed, contracts), "");
  assert.match(internals.validateVscodeLmFinalEnvelope({
    ...repaired,
    edits: [{ path: "src/app.py", current_sha256: "bad", ranges: [{ start_line: 3, end_line: 3, new: "bad" }] }],
  }, allowed, contracts), /final_range_out_of_bounds/);
  assert.match(internals.validateVscodeLmFinalEnvelope({
    schema_id: schema, summary: "wrong action",
    edits: [{ path: "tests/new.py", current_sha256: "b".repeat(64), ranges: [{ start_line: 1, end_line: 1, new: "bad" }] }],
    creates: [],
  }, allowed, contractsWithCreate), /final_action_mismatch/);
  assert.match(internals.validateVscodeLmFinalEnvelope({
    schema_id: schema, summary: "wrong create", edits: [],
    creates: [{ path: "src/app.py", content: "bad\n" }],
  }, allowed, contracts), /final_action_mismatch/);
  assert.match(internals.validateVscodeLmFinalEnvelope({
    schema_id: schema, summary: "no contract",
    edits: [{ path: "src/missing.py", current_sha256: "bad", ranges: [{ start_line: 1, end_line: 1, new: "bad" }] }],
    creates: [],
  }, allowed, contracts), /final_hash_invalid/);
  await textProtocolChecks();
  await nativeProtocolChecks();
  await malformedCatalogChecks();
  await permissionPersistenceChecks();
  await boundedParallelBridgeChecks();
  await progressReceiptChecks();
  await claimedCancellationChecks();
  await cancellationToolBoundaryChecks();
}

async function cancellationToolBoundaryChecks() {
  const toolEnvelope = JSON.stringify({
    schema_id: internals.constants.VSCODE_LM_TOOL_REQUEST_SCHEMA,
    name: "aiworkhub_manager_session_current_state",
    input: { limit: 1 },
  });
  const request = {
    requestId: "9".repeat(32),
    request_kind: "worker",
    prompt: "Cancellation boundary test.",
    allowedWrites: [],
    path_contracts: {},
    initial_source_graph_request: { mode: "focus", query: "cancel boundary", workflow_stage: "orientation" },
    initial_source_graph_result: { ok: true, content: "prefetched graph" },
  };

  const textToken = { isCancellationRequested: false };
  let textToolCalls = 0;
  const textModel = {
    capabilities: { toolCalling: false },
    sendRequest: async () => ({
      stream: (async function* stream() {
        yield { value: toolEnvelope };
        textToken.isCancellationRequested = true;
      }()),
    }),
  };
  await assert.rejects(
    internals.runVscodeLmTextProtocol(
      textModel,
      request,
      textToken,
      async () => { textToolCalls += 1; return { ok: true }; },
    ),
    /vscode_lm_request_cancelled/,
  );
  assert.strictEqual(textToolCalls, 0);

  const afterToolToken = { isCancellationRequested: false };
  let afterToolCalls = 0;
  const afterToolModel = {
    capabilities: { toolCalling: false },
    sendRequest: async () => ({
      stream: (async function* stream() { yield { value: toolEnvelope }; }()),
    }),
  };
  await assert.rejects(
    internals.runVscodeLmTextProtocol(
      afterToolModel,
      request,
      afterToolToken,
      async () => {
        afterToolCalls += 1;
        afterToolToken.isCancellationRequested = true;
        return { ok: true };
      },
    ),
    /vscode_lm_request_cancelled/,
  );
  assert.strictEqual(afterToolCalls, 1);

  const nativeToken = { isCancellationRequested: false };
  let nativeToolCalls = 0;
  const nativeModel = {
    capabilities: { toolCalling: true },
    sendRequest: async () => ({
      stream: (async function* stream() {
        nativeToken.isCancellationRequested = true;
        yield { callId: "cancel-native", name: "aiworkhub_manager_session_current_state", input: { limit: 1 } };
      }()),
    }),
  };
  await assert.rejects(
    internals.runVscodeLmAgent(
      nativeModel,
      request,
      nativeToken,
      async () => { nativeToolCalls += 1; return { ok: true }; },
    ),
    /vscode_lm_request_cancelled/,
  );
  assert.strictEqual(nativeToolCalls, 0);

  const ignoredToolSource = new FakeCancellationTokenSource();
  let ignoredToolStartedResolve;
  const ignoredToolStarted = new Promise((resolve) => { ignoredToolStartedResolve = resolve; });
  let ignoredToolResolve;
  const ignoredTool = new Promise((resolve) => { ignoredToolResolve = resolve; });
  const ignoredToolTurns = [];
  const ignoredToolModel = {
    capabilities: { toolCalling: false },
    sendRequest: async () => ({
      stream: (async function* stream() { yield { value: toolEnvelope }; }()),
    }),
  };
  const ignoredRun = internals.runVscodeLmTextProtocol(
    ignoredToolModel,
    request,
    ignoredToolSource.token,
    async () => {
      ignoredToolStartedResolve();
      return ignoredTool;
    },
    (_toolName, detail) => { ignoredToolTurns.push(detail && detail.tool_state); },
  );
  await ignoredToolStarted;
  ignoredToolSource.cancel();
  await assert.rejects(ignoredRun, /vscode_lm_request_cancelled/);
  ignoredToolResolve({ ok: true });
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepStrictEqual(
    ignoredToolTurns,
    ["started"],
    "cancellation may retain the pre-call liveness receipt but must not emit a late completion",
  );
}

function bridgeRequestFixture(root, repoInfo, requestId, cancelToken) {
  const requestDir = path.join(root, "requests", repoInfo.repoId);
  const requestPath = path.join(requestDir, `${requestId}.json`);
  const workspacePath = path.join(root, "workspaces", requestId, "worktree");
  const workspaceHome = path.join(root, "workspaces", requestId, "home");
  fs.mkdirSync(workspacePath, { recursive: true });
  fs.mkdirSync(workspaceHome);
  const responsePath = path.join(workspaceHome, ".aiworkhub_vscode_lm_response.json");
  const progressPath = path.join(workspaceHome, ".aiworkhub_vscode_lm_progress.json");
  const cancelPath = responsePath;
  internals.atomicWriteOwnerJson(requestPath, {
    schema_id: internals.constants.VSCODE_LM_REQUEST_SCHEMA,
    request_id: requestId,
    repo_id: repoInfo.repoId,
    repo_root: repoInfo.root,
    workspace_path: workspacePath,
    workspace_home: workspaceHome,
    response_path: responsePath,
    progress_path: progressPath,
    cancel_path: cancelPath,
    cancel_token: cancelToken,
    model: "glm-5.2",
    prompt: "Bounded cancellation bridge test.",
    request_kind: "worker",
    allowed_writes: [],
    path_contracts: {},
    initial_source_graph_request: { mode: "focus", query: "cancellation bridge", workflow_stage: "orientation" },
    initial_source_graph_result: { ok: true, content: "prefetched graph" },
    deadline: new Date(Date.now() + 60000).toISOString(),
  });
  return { requestPath, responsePath, progressPath, cancelPath, cancelToken, requestId };
}

function publishCancelDecision(request, repoInfo) {
  internals.atomicWriteOwnerJson(request.cancelPath, {
    schema_id: internals.constants.VSCODE_LM_RESPONSE_SCHEMA,
    request_id: request.requestId,
    repo_id: repoInfo.repoId,
    model: {},
    text: "",
    error: "vscode_lm_request_cancelled",
    diagnostics: { phase: "cancelled", action: "cancel", cancel_token: request.cancelToken },
    decision: { action: "cancel", cancel_token: request.cancelToken },
    completed_at: new Date().toISOString(),
  });
}

async function claimedCancellationChecks() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "aiworkhub-claimed-cancel-"));
  const previousRoot = process.env.AIWORKHUB_VSCODE_LM_BRIDGE_ROOT;
  process.env.AIWORKHUB_VSCODE_LM_BRIDGE_ROOT = root;
  try {
    const repoInfo = { root: path.join(root, "repo"), repoId: `repo_${"c".repeat(32)}` };
    fs.mkdirSync(repoInfo.root);
    const host = new internals.VscodeLmBridgeHost({
      globalState: { get: () => true, update: async () => {} },
    });
    host.repoInfo = { ...repoInfo };
    host.ensurePermission = async () => true;

    const partial = bridgeRequestFixture(root, repoInfo, "1".repeat(32), "1".repeat(64));
    fs.writeFileSync(partial.responsePath, "{", { mode: 0o600 });
    const partialState = { invalidReads: 0 };
    assert.deepStrictEqual(
      internals.readVscodeLmCancelDecision({ ...partial, repo_id: repoInfo.repoId }, partialState),
      { action: "pending" },
    );
    assert.deepStrictEqual(
      internals.readVscodeLmCancelDecision({ ...partial, repo_id: repoInfo.repoId }, partialState),
      { action: "pending" },
    );
    assert.throws(
      () => internals.readVscodeLmCancelDecision({ ...partial, repo_id: repoInfo.repoId }, partialState),
      /vscode_lm_cancel_decision_persistent_invalid_json/,
    );
    assert.strictEqual(fs.readFileSync(partial.responsePath, "utf8"), "{");
    fs.unlinkSync(partial.responsePath);
    fs.unlinkSync(partial.requestPath);

    const cancelled = bridgeRequestFixture(root, repoInfo, "5".repeat(32), "a".repeat(64));
    let providerStartedResolve;
    const providerStarted = new Promise((resolve) => { providerStartedResolve = resolve; });
    const blockingModel = {
      ...exact,
      sendRequest: async (_messages, _options, token) => {
        providerStartedResolve();
        return {
          stream: (async function* stream() {
            await token.cancelled;
            throw new Error("provider_cancelled_by_test");
          }()),
        };
      },
    };
    host.models = async () => [blockingModel];
    const cancelStartIndex = fakeCancellationSources.length;
    const cancelledPoll = host.poll();
    await Promise.race([
      providerStarted,
      new Promise((_resolve, reject) => setTimeout(() => reject(new Error("provider_start_timeout")), 2000)),
    ]);
    publishCancelDecision(cancelled, repoInfo);
    await cancelledPoll;
    assert.strictEqual(JSON.parse(fs.readFileSync(cancelled.responsePath, "utf8")).error, "vscode_lm_request_cancelled");
    assert.ok(fakeCancellationSources.slice(cancelStartIndex).some((source) => source.cancelCount > 0));
    assert.deepStrictEqual(fs.readdirSync(path.dirname(cancelled.requestPath)), []);

    const forgedReceipt = path.join(path.dirname(cancelled.responsePath), "forged-response.json");
    fs.copyFileSync(cancelled.responsePath, forgedReceipt);
    const originalOpenSync = fs.openSync;
    fs.openSync = (filePath, flags, ...args) => (
      path.resolve(String(filePath)) === path.resolve(cancelled.responsePath)
        ? originalOpenSync(forgedReceipt, flags, ...args)
        : originalOpenSync(filePath, flags, ...args)
    );
    try {
      assert.throws(
        () => internals.readVscodeLmCancelDecision({ ...cancelled, repo_id: repoInfo.repoId }),
        /vscode_lm_cancel_decision_identity_changed/,
      );
    } finally {
      fs.openSync = originalOpenSync;
      fs.unlinkSync(forgedReceipt);
    }

    // A cancellation that lands after provider completion but before response
    // publication still wins the exclusive decision race.
    const raced = bridgeRequestFixture(root, repoInfo, "6".repeat(32), "b".repeat(64));
    const finalResponse = JSON.stringify({
      schema_id: internals.constants.VSCODE_LM_EDIT_RESPONSE_SCHEMA,
      summary: "no changes",
      edits: [],
      creates: [],
    });
    const raceModel = {
      ...exact,
      sendRequest: async () => ({
        stream: (async function* stream() {
          publishCancelDecision(raced, repoInfo);
          yield { value: finalResponse };
        }()),
      }),
    };
    host.models = async () => [raceModel];
    await host.poll();
    assert.strictEqual(JSON.parse(fs.readFileSync(raced.responsePath, "utf8")).error, "vscode_lm_request_cancelled");

    // A marker for another request cannot cancel or reserve this request.
    const isolatedMarker = {
      ...raced,
      requestId: "7".repeat(32),
      cancelToken: "c".repeat(64),
      cancelPath: path.join(root, "workspaces", "7".repeat(32), "home", ".aiworkhub_vscode_lm_response.json"),
    };
    fs.mkdirSync(path.dirname(isolatedMarker.cancelPath), { recursive: true });
    publishCancelDecision(isolatedMarker, repoInfo);
    const successful = bridgeRequestFixture(root, repoInfo, "8".repeat(32), "d".repeat(64));
    const successModel = {
      ...exact,
      sendRequest: async () => ({
        stream: (async function* stream() { yield { value: finalResponse }; }()),
      }),
    };
    host.models = async () => [successModel];
    await host.poll();
    assert.ok(
      fs.existsSync(successful.responsePath),
      JSON.stringify(internals.systemLogSnapshot().slice(-8)),
    );
    assert.strictEqual(JSON.parse(fs.readFileSync(successful.responsePath, "utf8")).error, "");
    assert.deepStrictEqual(
      JSON.parse(fs.readFileSync(successful.responsePath, "utf8")).decision,
      { action: "response", cancel_token: successful.cancelToken },
    );
    assert.strictEqual(
      internals.atomicWriteOwnerJsonExclusive(successful.responsePath, {
        schema_id: internals.constants.VSCODE_LM_RESPONSE_SCHEMA,
        request_id: successful.requestId,
        repo_id: repoInfo.repoId,
        error: "vscode_lm_request_cancelled",
        decision: { action: "cancel", cancel_token: successful.cancelToken },
      }),
      false,
      "a durable response decision must win every later cancellation attempt",
    );
    assert.strictEqual(
      internals.readVscodeLmCancelDecision({ ...successful, repo_id: repoInfo.repoId }).action,
      "response",
    );
    assert.strictEqual(fs.existsSync(successful.cancelPath), true);
    assert.strictEqual(fs.existsSync(isolatedMarker.cancelPath), true);
    fs.unlinkSync(isolatedMarker.cancelPath);

    const modelStageHost = new internals.VscodeLmBridgeHost({
      globalState: { get: () => true, update: async () => {} },
    });
    modelStageHost.repoInfo = { ...repoInfo };
    modelStageHost.ensurePermission = async () => true;
    const modelStage = bridgeRequestFixture(root, repoInfo, "a".repeat(32), "e".repeat(64));
    let modelStageStartedResolve;
    const modelStageStarted = new Promise((resolve) => { modelStageStartedResolve = resolve; });
    let resolveModels;
    modelStageHost.models = () => {
      modelStageStartedResolve();
      return new Promise((resolve) => { resolveModels = resolve; });
    };
    const modelStagePoll = modelStageHost.poll();
    await modelStageStarted;
    modelStageHost.stop();
    await Promise.race([
      modelStagePoll,
      new Promise((_resolve, reject) => setTimeout(() => reject(new Error("model_cancel_timeout")), 2000)),
    ]);
    assert.strictEqual(modelStageHost.activeClaims.size, 0);
    assert.strictEqual(JSON.parse(fs.readFileSync(modelStage.responsePath, "utf8")).decision.action, "cancel");
    resolveModels([exact]);
    await new Promise((resolve) => setImmediate(resolve));

    const ignoredProviderHost = new internals.VscodeLmBridgeHost({
      globalState: { get: () => true, update: async () => {} },
    });
    ignoredProviderHost.repoInfo = { ...repoInfo };
    ignoredProviderHost.ensurePermission = async () => true;
    const ignoredProvider = bridgeRequestFixture(root, repoInfo, "b".repeat(32), "f".repeat(64));
    let providerStageStartedResolve;
    const providerStageStarted = new Promise((resolve) => { providerStageStartedResolve = resolve; });
    let resolveProvider;
    const providerPromise = new Promise((resolve) => { resolveProvider = resolve; });
    ignoredProviderHost.models = async () => [{
      ...exact,
      sendRequest: () => {
        providerStageStartedResolve();
        return providerPromise;
      },
    }];
    const ignoredProviderPoll = ignoredProviderHost.poll();
    await providerStageStarted;
    ignoredProviderHost.stop();
    await Promise.race([
      ignoredProviderPoll,
      new Promise((_resolve, reject) => setTimeout(() => reject(new Error("ignored_provider_cancel_timeout")), 2000)),
    ]);
    const cancelBytes = fs.readFileSync(ignoredProvider.responsePath);
    const progressBytes = fs.existsSync(ignoredProvider.progressPath)
      ? fs.readFileSync(ignoredProvider.progressPath)
      : null;
    assert.strictEqual(ignoredProviderHost.activeClaims.size, 0, "cancelled claim must release its slot");
    assert.strictEqual(JSON.parse(cancelBytes.toString("utf8")).decision.action, "cancel");
    resolveProvider({ stream: (async function* stream() { yield { value: finalResponse }; }()) });
    await new Promise((resolve) => setTimeout(resolve, 25));
    assert.deepStrictEqual(fs.readFileSync(ignoredProvider.responsePath), cancelBytes, "late provider must not replace cancellation");
    assert.deepStrictEqual(
      fs.existsSync(ignoredProvider.progressPath) ? fs.readFileSync(ignoredProvider.progressPath) : null,
      progressBytes,
      "late provider must not emit progress",
    );
    host.dispose();
  } finally {
    if (previousRoot === undefined) delete process.env.AIWORKHUB_VSCODE_LM_BRIDGE_ROOT;
    else process.env.AIWORKHUB_VSCODE_LM_BRIDGE_ROOT = previousRoot;
    fs.rmSync(root, { recursive: true, force: true });
  }
}

async function progressReceiptChecks() {
  assert.strictEqual(
    internals.constants.VSCODE_LM_PROGRESS_SCHEMA,
    "aiworkhub.vscode_lm.progress_receipt.v1",
  );
  assert.deepStrictEqual(internals.constants.VSCODE_LM_PROGRESS_PHASES, [
    "request_accepted", "provider_response", "tool_turn", "final_edit", "terminal_error",
  ]);
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "aiworkhub-progress-"));
  try {
    const target = path.join(root, ".aiworkhub_vscode_lm_progress.json");
    const originalRenameSync = fs.renameSync;
    let publishedTempMode = null;
    fs.renameSync = (source, destination) => {
      if (destination === target && process.platform !== "win32") {
        publishedTempMode = fs.statSync(source).mode & 0o777;
      }
      return originalRenameSync(source, destination);
    };
    try {
      internals.atomicWriteOwnerJson(target, {
        schema_id: internals.constants.VSCODE_LM_PROGRESS_SCHEMA,
        request_id: "a".repeat(32),
        repo_id: "repo_test",
        sequence: 1,
        phase: "request_accepted",
        updated_at: new Date().toISOString(),
      });
    } finally {
      fs.renameSync = originalRenameSync;
    }
    assert.strictEqual(JSON.parse(fs.readFileSync(target, "utf8")).sequence, 1);
    assert.strictEqual(internals.ownerOnlyRegularFile(target), true);
    if (process.platform !== "win32") {
      assert.strictEqual(publishedTempMode, 0o600);
    }
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
}

main().then(() => {
  console.log("GLM VS Code LM bridge: ok");
}).catch((err) => {
  console.error(err);
  process.exitCode = 1;
});
