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
// NF134: Verify both manager-scoped and worker-scoped aliases exist in the central registry.
assert.ok(internals.VSCODE_LM_PRIVATE_TOOLS.some((tool) => tool.name === "aiworkhub_manager_source_graph_query"));
assert.ok(internals.VSCODE_LM_PRIVATE_TOOLS.some((tool) => tool.name === "aiworkhub_worker_source_graph_query"));
assert.ok(internals.VSCODE_LM_PRIVATE_TOOLS.some((tool) => tool.name === "aiworkhub_manager_semantic_edit_prepare"));
assert.ok(internals.VSCODE_LM_PRIVATE_TOOLS.some((tool) => tool.name === "aiworkhub_manager_semantic_edit_stage"));
assert.ok(internals.VSCODE_LM_PRIVATE_TOOLS.some((tool) => tool.name === "aiworkhub_manager_semantic_edit_finalize"));
const stageTool = internals.VSCODE_LM_PRIVATE_TOOLS.find((tool) => tool.name === "aiworkhub_manager_semantic_edit_stage");
assert.ok(Array.isArray(stageTool.inputSchema && stageTool.inputSchema.oneOf), "stage tool schema must be oneOf");
assert.strictEqual(stageTool.inputSchema.oneOf.length, 2);
assert.ok(stageTool.inputSchema.oneOf.every((schema) => schema.additionalProperties === false));
assert.notStrictEqual(stageTool.inputSchema.additionalProperties, false, "outer oneOf schema must not reject branch properties");
const schemaMatches = (schema, value) => {
  if (!schema || schema.type !== "object" || !value || typeof value !== "object" || Array.isArray(value)) return false;
  if (Array.isArray(schema.oneOf)) return schema.oneOf.filter((branch) => schemaMatches(branch, value)).length === 1;
  const properties = schema.properties || {};
  if ((schema.required || []).some((key) => !Object.prototype.hasOwnProperty.call(value, key))) return false;
  if (schema.additionalProperties === false && Object.keys(value).some((key) => !Object.prototype.hasOwnProperty.call(properties, key))) return false;
  return Object.entries(value).every(([key, field]) => {
    const rule = properties[key];
    if (!rule) return schema.additionalProperties !== false;
    if (Object.prototype.hasOwnProperty.call(rule, "const") && field !== rule.const) return false;
    if (rule.type === "string" && typeof field !== "string") return false;
    if (rule.type === "integer" && (!Number.isInteger(field) || field < (rule.minimum || Number.MIN_SAFE_INTEGER))) return false;
    if (rule.type === "boolean" && typeof field !== "boolean") return false;
    return true;
  });
};
assert.strictEqual(schemaMatches(stageTool.inputSchema, {
  operation: "create", file_path: "new.js", content: "export {};\n",
}), true);
assert.strictEqual(schemaMatches(stageTool.inputSchema, {
  operation: "replace_range", file_path: "old.js", start_line: 1, end_line: 1, new: "export {};",
}), true);
for (const invalidStageInput of [
  { operation: "create", file_path: "new.js" },
  { operation: "replace_range", file_path: "old.js", start_line: 1, end_line: 1 },
  { operation: "move", file_path: "old.js", start_line: 1, end_line: 1, new: "x" },
  { operation: "create", file_path: "new.js", content: "x", start_line: 1 },
  { operation: "replace_range", file_path: "old.js", start_line: 1, end_line: 1, new: "x", content: "y" },
]) assert.strictEqual(schemaMatches(stageTool.inputSchema, invalidStageInput), false);
assert.ok(internals.VSCODE_LM_PRIVATE_TOOLS.some((tool) => tool.name === "aiworkhub_worker_session_current_state"));
assert.ok(internals.VSCODE_LM_PRIVATE_TOOLS.some((tool) => tool.name === "aiworkhub_worker_ai_memory_search"));
assert.ok(internals.VSCODE_LM_PRIVATE_TOOLS.some((tool) => tool.name === "aiworkhub_worker_kb_search"));
// Worker routes must expose only worker-scoped MCP tools (plus bridge-internal stage/finalize).
const workerVisibleTools = internals.vscodeLmToolsForRequest({ request_kind: "worker" }, true);
assert.ok(!workerVisibleTools.some((tool) => tool.name === "aiworkhub_manager_semantic_edit_prepare"));
assert.ok(!workerVisibleTools.some((tool) => tool.name === "aiworkhub_manager_source_graph_query"));
assert.ok(!workerVisibleTools.some((tool) => tool.name === "aiworkhub_manager_session_current_state"));
assert.ok(workerVisibleTools.some((tool) => tool.name === "aiworkhub_worker_source_graph_query"));
assert.ok(workerVisibleTools.some((tool) => tool.name === "aiworkhub_worker_session_current_state"));
assert.ok(workerVisibleTools.some((tool) => tool.name === "aiworkhub_worker_ai_memory_search"));
assert.ok(workerVisibleTools.some((tool) => tool.name === "aiworkhub_worker_kb_search"));
assert.ok(workerVisibleTools.some((tool) => tool.name === "aiworkhub_manager_semantic_edit_stage"));
assert.ok(workerVisibleTools.some((tool) => tool.name === "aiworkhub_manager_semantic_edit_finalize"));
assert.ok(!workerVisibleTools.some((tool) => tool.name === "aiworkhub_worker_quality_review_submit"));
// Manager routes expose manager-scoped MCP tools.
const managerVisibleTools = internals.vscodeLmToolsForRequest({ request_kind: "manager" }, true);
assert.ok(managerVisibleTools.some((tool) => tool.name === "aiworkhub_manager_source_graph_query"));
assert.ok(managerVisibleTools.some((tool) => tool.name === "aiworkhub_manager_session_current_state"));
assert.ok(!managerVisibleTools.some((tool) => tool.name === "aiworkhub_worker_source_graph_query"));
assert.ok(!managerVisibleTools.some((tool) => tool.name === "aiworkhub_worker_session_current_state"));
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
assert.ok(internals.glmTextToolProtocolPrompt("bounded", ["src/app.py"]).includes("Use mode=focus only for broad orientation"));
assert.ok(internals.glmTextToolProtocolPrompt("bounded", ["src/app.py"]).includes("Never coerce or repeat mode=focus for an exact file/body lookup"));
assert.ok(!internals.glmTextToolProtocolPrompt("bounded", ["src/app.py"]).includes("For every tool call output ONLY: {\"schema_id\":\"aiworkhub.vscode_lm.tool_request.v1\",\"name\":\"aiworkhub_worker_source_graph_query\",\"input\":{\"mode\":\"focus\""));
assert.ok(internals.glmTextToolProtocolPrompt("bounded", ["src/app.py"]).includes("prepare is an internal bridge primitive"));
assert.ok(!internals.glmTextToolProtocolPrompt("bounded", ["src/app.py"]).includes('"aiworkhub_manager_semantic_edit_prepare"'));
assert.ok(internals.glmTextToolProtocolPrompt("bounded", ["src/app.py"]).includes("semantic_edit_stage"));
assert.ok(internals.glmTextToolProtocolPrompt("bounded", ["src/app.py"]).includes("assembles the final envelope offline"));
assert.ok(internals.glmTextToolProtocolPrompt("bounded", ["src/app.py"]).includes("file_path, start_line, end_line, and new"));
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
assert.ok(qualityReviewTextPrompt.includes("MUST finish by calling aiworkhub_worker_quality_review_submit"));
assert.ok(!qualityReviewTextPrompt.includes("aiworkhub_manager_semantic_edit_apply"));
assert.ok(!qualityReviewTextPrompt.includes("Output ONLY one final aiworkhub.vscode_lm.edit_response"));
assert.ok(internals.glmTextToolProtocolPrompt("bounded", ["src/app.py"]).includes("Canonical stage request"));
assert.ok(internals.glmTextToolProtocolPrompt("bounded", ["src/app.py"]).includes("Canonical stage request (create)"));
assert.ok(internals.glmTextToolProtocolPrompt("bounded", ["src/app.py"]).includes("Canonical stage request (replace_range)"));
assert.ok(internals.glmTextToolProtocolPrompt("bounded", ["src/app.py"]).includes("Canonical finalize request"));
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
    name: "aiworkhub_worker_quality_review_submit",
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
    "aiworkhub_worker_quality_review_submit",
  ]);

  // NF-2026-00168 / NF166: a substantive GLM correctness-review response may
  // omit the tool-request wrapper and emit the findings object directly. Both
  // failure shapes below previously threw vscode_lm_text_protocol_invalid_json;
  // they must now normalize to aiworkhub_worker_quality_review_submit.
  const substantiveFindings = [
    {
      id: "NF166-1",
      severity: "high",
      summary: "correctness reviewer dropped the mandatory submit call",
      evidence: "extension.js parseVscodeLmJsonEnvelope",
    },
  ];
  const substantiveReviewText = JSON.stringify({
    packet_sha256: "c".repeat(64),
    lens: "correctness",
    findings: substantiveFindings,
  });
  assert.deepStrictEqual(
    internals.parseVscodeLmJsonEnvelope(substantiveReviewText, { preferFinal: true }),
    {
      schema_id: internals.constants.VSCODE_LM_TOOL_REQUEST_SCHEMA,
      name: "aiworkhub_worker_quality_review_submit",
      input: {
        packet_sha256: "c".repeat(64),
        lens: "correctness",
        findings: substantiveFindings,
      },
    },
  );
  assert.deepStrictEqual(
    internals.parseVscodeLmJsonEnvelope(JSON.stringify({
      summary: "Correctness review complete",
      findings: substantiveFindings,
    }), { preferFinal: true }),
    {
      schema_id: internals.constants.VSCODE_LM_TOOL_REQUEST_SCHEMA,
      name: "aiworkhub_worker_quality_review_submit",
      input: { findings: substantiveFindings },
    },
  );

  const substantiveSubmissionId = "d".repeat(64);
  const substantiveQueued = [substantiveReviewText];
  const substantiveReviewCalls = [];
  const substantiveReviewModel = {
    capabilities: { toolCalling: false },
    sendRequest: async () => ({
      stream: (async function* stream() { yield { value: substantiveQueued.shift() }; }()),
    }),
  };
  const substantiveReviewResult = await internals.runVscodeLmTextProtocol(
    substantiveReviewModel,
    { prompt: "bounded correctness review", request_kind: "quality_review", allowedWrites: [] },
    undefined,
    async (call) => {
      substantiveReviewCalls.push(call);
      return { ok: true, durable: true, submission_id: substantiveSubmissionId };
    },
  );
  assert.deepStrictEqual(JSON.parse(substantiveReviewResult), {
    schema_id: internals.constants.VSCODE_LM_EDIT_RESPONSE_SCHEMA,
    summary: `quality review submitted:${substantiveSubmissionId}`,
    edits: [],
    creates: [],
  });
  assert.deepStrictEqual(substantiveReviewCalls.map((call) => call.name), [
    "aiworkhub_worker_quality_review_submit",
  ]);
  assert.deepStrictEqual(substantiveReviewCalls[0].input.findings, substantiveFindings);

  // Manager/worker authority must remain fail-closed: a non-quality_review
  // route emitting the same findings object is rejected before invocation.
  const wrongRoleCalls = [];
  const wrongRoleModel = {
    capabilities: { toolCalling: false },
    sendRequest: async () => ({
      stream: (async function* stream() { yield { value: substantiveReviewText }; }()),
    }),
  };
  await assert.rejects(
    internals.runVscodeLmTextProtocol(
      wrongRoleModel,
      { prompt: "worker edit", request_kind: "worker", allowedWrites: ["out/result.json"] },
      undefined,
      async (call) => {
        wrongRoleCalls.push(call);
        return { ok: true, durable: true, submission_id: "e".repeat(64) };
      },
    ),
    /vscode_lm_tool_not_allowed:aiworkhub_worker_quality_review_submit/,
  );
  assert.strictEqual(wrongRoleCalls.length, 0);

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
  // NF389/r6: the launch-time fallback prefetch tags itself provenance="prefetch"
  // so the worker bridge binds it as auditable-but-never-live.
  assert.strictEqual(prefetchedCalls[0].input.provenance, "prefetch");
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

  const stagedTextTurns = [
    JSON.stringify({
      schema_id: internals.constants.VSCODE_LM_TOOL_REQUEST_SCHEMA,
      name: "aiworkhub_manager_semantic_edit_stage",
      input: {
        operation: "replace_range",
        file_path: "src/app.py",
        start_line: 2,
        end_line: 2,
        new: "return 2",
      },
    }),
    JSON.stringify({
      schema_id: internals.constants.VSCODE_LM_TOOL_REQUEST_SCHEMA,
      name: "aiworkhub_manager_semantic_edit_stage",
      input: {
        operation: "create",
        file_path: "tests/test_app.py",
        content: "def test_app():\n    assert True\n",
      },
    }),
    JSON.stringify({
      schema_id: internals.constants.VSCODE_LM_TOOL_REQUEST_SCHEMA,
      name: "aiworkhub_manager_semantic_edit_finalize",
      input: { summary: "Updated app and added its focused test." },
    }),
  ];
  const stagedTextModel = {
    capabilities: { toolCalling: false },
    sendRequest: async () => ({
      stream: (async function* stream() { yield { value: stagedTextTurns.shift() }; }()),
    }),
  };
  const stagedTextCalls = [];
  const stagedTextResult = JSON.parse(await internals.runVscodeLmTextProtocol(
    stagedTextModel,
    {
      requestId: "1".repeat(32),
      prompt: "stage bounded edits",
      allowedWrites: ["src/app.py", "tests/test_app.py"],
      path_contracts: {
        ...editContract,
        "tests/test_app.py": {
          action: "create",
          current_sha256: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
          line_count: 0,
          parent_existed: false,
        },
      },
      initial_source_graph_request: { mode: "focus", query: "staged edit", budget: 48 },
      initial_source_graph_result: { ok: true, content: "prefetched graph" },
    },
    undefined,
    async (call) => {
      stagedTextCalls.push(call);
      throw new Error("staged edits must not require an MCP round-trip");
    },
  ));
  assert.strictEqual(stagedTextCalls.length, 0);
  assert.strictEqual(stagedTextResult.summary, "Updated app and added its focused test.");
  assert.deepStrictEqual(stagedTextResult.edits, [{
    path: "src/app.py",
    current_sha256: "a".repeat(64),
    ranges: [{
      start_line: 2,
      end_line: 2,
      new: "return 2",
      preserve_trailing_newline: true,
    }],
  }]);
  assert.deepStrictEqual(stagedTextResult.creates, [{
    path: "tests/test_app.py",
    content: "def test_app():\n    assert True\n",
  }]);

  assert.strictEqual(
    internals.vscodeLmProtocolToolTransport(" aiworkhub_manager_semantic_edit_stage "),
    "offline_staged",
  );
  assert.strictEqual(
    internals.vscodeLmProtocolToolTransport("aiworkhub_manager_source_graph_query"),
    "mcp",
  );

  const atomicCollector = internals.createVscodeLmStagedEditCollector({
    allowedWrites: ["src/app.py"],
    path_contracts: editContract,
  });
  assert.strictEqual((await atomicCollector.stage({
    operation: "replace_range",
    file_path: "src/app.py",
    start_line: 2,
    end_line: 2,
    new: "return 4",
  })).ok, true);
  const rejectedOverlap = await atomicCollector.stage({
    operation: "replace_range",
    file_path: "src/app.py",
    start_line: 1,
    end_line: 2,
    new: "corrupt overlap",
  });
  assert.strictEqual(rejectedOverlap.ok, false);
  assert.match(rejectedOverlap.reason, /range_overlap/);
  const atomicFinal = atomicCollector.finalize("Kept only the verified staged range.");
  assert.strictEqual(atomicFinal.ok, true);
  assert.strictEqual(atomicFinal.__finalEnvelope.edits[0].ranges.length, 1);
  assert.strictEqual(atomicFinal.__finalEnvelope.edits[0].ranges[0].new, "return 4");
  assert.ok(!Object.prototype.hasOwnProperty.call(atomicFinal, "required_output_count"));
  assert.ok(!Object.prototype.hasOwnProperty.call(atomicFinal, "completed_outputs"));
  assert.ok(!Object.prototype.hasOwnProperty.call(atomicFinal, "missing_outputs"));
  assert.ok(!Object.prototype.hasOwnProperty.call(atomicFinal, "completion_rate"));
  const mixedCollector = internals.createVscodeLmStagedEditCollector({
    allowedWrites: ["src/app.py", "tests/created.py"],
    path_contracts: {
      ...editContract,
      "tests/created.py": {
        action: "create",
        current_sha256: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        line_count: 0,
        parent_existed: false,
      },
    },
  });
  assert.strictEqual((await mixedCollector.stage({
    operation: "replace_range",
    file_path: "src/app.py",
    start_line: 2,
    end_line: 2,
    new: "return 10",
  })).ok, true);
  assert.strictEqual((await mixedCollector.stage({
    operation: "create",
    file_path: "tests/created.py",
    content: "def created():\n    return True\n",
  })).ok, true);
  const mixedReplaceMissingNew = await mixedCollector.stage({
    operation: "replace_range", file_path: "src/app.py", start_line: 1, end_line: 1,
  });
  assert.strictEqual(mixedReplaceMissingNew.ok, false);
  assert.match(mixedReplaceMissingNew.reason, /range_invalid/);
  const mixedReplaceExtra = await mixedCollector.stage({
    operation: "replace_range", file_path: "src/app.py", start_line: 2, end_line: 2,
    new: "replace_extra", content: "forbidden",
  });
  assert.strictEqual(mixedReplaceExtra.ok, false);
  assert.match(mixedReplaceExtra.reason, /stage_payload_extra_fields/);
  const mixedCreateMissingContent = await mixedCollector.stage({
    operation: "create", file_path: "tests/created.py",
  });
  assert.strictEqual(mixedCreateMissingContent.ok, false);
  assert.match(mixedCreateMissingContent.reason, /content_invalid/);
  const mixedCreateExtra = await mixedCollector.stage({
    operation: "create", file_path: "tests/created.py", content: "ok",
    start_line: 1, end_line: 1, new: "bad",
  });
  assert.strictEqual(mixedCreateExtra.ok, false);
  assert.match(mixedCreateExtra.reason, /stage_payload_extra_fields/);
  const mixedOperationInvalid = await mixedCollector.stage({
    operation: "move_block", file_path: "src/app.py", start_line: 1, end_line: 1, new: "bad",
  });
  assert.strictEqual(mixedOperationInvalid.ok, false);
  assert.match(mixedOperationInvalid.reason, /operation_invalid/);

  const requiredContracts = {
    ...editContract,
    "tests/created.py": {
      action: "create",
      current_sha256: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
      line_count: 0,
      parent_existed: false,
    },
  };
  const requiredCollector = internals.createVscodeLmStagedEditCollector({
    allowedWrites: ["src/app.py", "tests/created.py"],
    path_contracts: requiredContracts,
    required_outputs: ["src/app.py", "tests\\created.py", "src/app.py", ""],
  });
  const requiredStage1 = await requiredCollector.stage({
    operation: "replace_range",
    file_path: "src/app.py",
    start_line: 2,
    end_line: 2,
    new: "return 10",
  });
  assert.strictEqual(requiredStage1.ok, true);
  assert.strictEqual(requiredStage1.required_output_count, 2);
  assert.deepStrictEqual(requiredStage1.completed_outputs, ["src/app.py"]);
  assert.deepStrictEqual(requiredStage1.missing_outputs, ["tests/created.py"]);
  assert.strictEqual(requiredStage1.completion_rate, 0.5);
  const incompleteFinal = requiredCollector.finalize("missing required create");
  assert.strictEqual(incompleteFinal.ok, false);
  assert.strictEqual(incompleteFinal.reason, "semantic_edit_required_outputs_incomplete");
  assert.strictEqual(incompleteFinal.required_output_count, 2);
  assert.deepStrictEqual(incompleteFinal.completed_outputs, ["src/app.py"]);
  assert.deepStrictEqual(incompleteFinal.missing_outputs, ["tests/created.py"]);
  assert.strictEqual(incompleteFinal.completion_rate, 0.5);
  assert.ok(!Object.prototype.hasOwnProperty.call(incompleteFinal, "__finalEnvelope"));
  const requiredStage2 = await requiredCollector.stage({
    operation: "create",
    file_path: "tests/created.py",
    content: "def created():\n    return True\n",
  });
  assert.strictEqual(requiredStage2.ok, true);
  assert.strictEqual(requiredStage2.required_output_count, 2);
  assert.deepStrictEqual(requiredStage2.completed_outputs, ["src/app.py", "tests/created.py"]);
  assert.deepStrictEqual(requiredStage2.missing_outputs, []);
  assert.strictEqual(requiredStage2.completion_rate, 1);
  const completeRequiredFinal = requiredCollector.finalize("complete required outputs");
  assert.strictEqual(completeRequiredFinal.ok, true);
  assert.strictEqual(completeRequiredFinal.required_output_count, 2);
  assert.deepStrictEqual(completeRequiredFinal.completed_outputs, ["src/app.py", "tests/created.py"]);
  assert.deepStrictEqual(completeRequiredFinal.missing_outputs, []);
  assert.strictEqual(completeRequiredFinal.completion_rate, 1);
  assert.strictEqual(completeRequiredFinal.__finalEnvelope.edits.length, 1);
  assert.strictEqual(completeRequiredFinal.__finalEnvelope.creates.length, 1);
  assert.strictEqual(completeRequiredFinal.__finalEnvelope.edits[0].ranges[0].new, "return 10");

  const extraPathCollector = internals.createVscodeLmStagedEditCollector({
    allowedWrites: ["src/app.py", "tests/created.py"],
    path_contracts: editContract,
    required_outputs: ["src/app.py"],
  });
  assert.strictEqual((await extraPathCollector.stage({
    operation: "replace_range",
    file_path: "src/app.py",
    start_line: 2,
    end_line: 2,
    new: "return 11",
  })).ok, true);
  const extraPathConflict = await extraPathCollector.stage({
    operation: "create",
    file_path: "src/app.py",
    content: "conflict",
  });
  assert.strictEqual(extraPathConflict.ok, false);
  assert.match(extraPathConflict.reason, /action_mismatch|path_conflict/);
  const extraReplay = await extraPathCollector.stage({
    operation: "replace_range",
    file_path: "src/app.py",
    start_line: 2,
    end_line: 2,
    new: "return 12",
  });
  assert.strictEqual(extraReplay.ok, false);
  assert.match(extraReplay.reason, /range_conflict/);
  const extraPathStage = await extraPathCollector.stage({
    operation: "create",
    file_path: "tests/created.py",
    content: "def created():\n    return True\n",
  });
  assert.strictEqual(extraPathStage.ok, false);
  assert.match(extraPathStage.reason, /path_not_required/);
  assert.strictEqual(extraPathStage.required_output_count, 1);
  assert.deepStrictEqual(extraPathStage.completed_outputs, ["src/app.py"]);
  assert.deepStrictEqual(extraPathStage.missing_outputs, []);
  const extraPathFinal = extraPathCollector.finalize("required path complete without extra create");
  assert.strictEqual(extraPathFinal.ok, true);
  assert.strictEqual(extraPathFinal.required_output_count, 1);
  assert.deepStrictEqual(extraPathFinal.completed_outputs, ["src/app.py"]);
  assert.deepStrictEqual(extraPathFinal.missing_outputs, []);
  assert.strictEqual(extraPathFinal.completion_rate, 1);
  assert.strictEqual(extraPathFinal.__finalEnvelope.creates.length, 0);

  const boundedCorrectionCollector = internals.createVscodeLmStagedEditCollector({
    allowedWrites: ["src/app.py", "tests/created.py"],
    path_contracts: requiredContracts,
    required_outputs: ["src/app.py", "tests/created.py"],
  });
  assert.strictEqual((await boundedCorrectionCollector.stage({
    operation: "replace_range",
    file_path: "src/app.py",
    start_line: 2,
    end_line: 2,
    new: "return 13",
  })).ok, true);
  const firstIncomplete = boundedCorrectionCollector.finalize("first incomplete");
  assert.strictEqual(firstIncomplete.ok, false);
  assert.strictEqual(firstIncomplete.reason, "semantic_edit_required_outputs_incomplete");
  const secondIncomplete = boundedCorrectionCollector.finalize("second incomplete");
  assert.strictEqual(secondIncomplete.ok, false);
  assert.strictEqual(secondIncomplete.reason, "semantic_edit_required_outputs_correction_exhausted");
  assert.ok(!Object.prototype.hasOwnProperty.call(secondIncomplete, "__finalEnvelope"));
  const lateStage = await boundedCorrectionCollector.stage({
    operation: "create",
    file_path: "tests/created.py",
    content: "def created():\n    return True\n",
  });
  assert.strictEqual(lateStage.ok, false);
  assert.match(lateStage.reason, /required_outputs_correction_exhausted/);
  const terminalFinal = boundedCorrectionCollector.finalize("after exhausted");
  assert.strictEqual(terminalFinal.ok, false);
  assert.strictEqual(terminalFinal.reason, "semantic_edit_required_outputs_correction_exhausted");

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

function windowTargetClaimChecks() {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), "aiworkhub-lm-window-target-"));
  try {
    const legacy = path.join(temp, "legacy.json");
    const targeted = path.join(temp, "targeted.json");
    const other = path.join(temp, "other.json");
    fs.writeFileSync(legacy, JSON.stringify({ schema_id: internals.constants.VSCODE_LM_REQUEST_SCHEMA }), { mode: 0o600 });
    fs.writeFileSync(targeted, JSON.stringify({
      schema_id: internals.constants.VSCODE_LM_REQUEST_SCHEMA,
      target_window_id: internals.constants.WINDOW_SCOPE_ID,
    }), { mode: 0o600 });
    fs.writeFileSync(other, JSON.stringify({
      schema_id: internals.constants.VSCODE_LM_REQUEST_SCHEMA,
      target_window_id: "window_other",
    }), { mode: 0o600 });
    assert.strictEqual(internals.vscodeLmRequestClaimableByThisWindow(legacy), true);
    assert.strictEqual(internals.vscodeLmRequestClaimableByThisWindow(targeted), true);
    assert.strictEqual(internals.vscodeLmRequestClaimableByThisWindow(other), false);
  } finally {
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

  const stagedNativeTurns = [
    [{
      callId: "stage-1",
      name: "aiworkhub_manager_semantic_edit_stage",
      input: {
        operation: "replace_range",
        file_path: "src/app.py",
        start_line: 2,
        end_line: 2,
        new: "return 3",
      },
    }],
    [{
      callId: "finalize-1",
      name: "aiworkhub_manager_semantic_edit_finalize",
      input: { summary: "Applied one staged native edit." },
    }],
  ];
  const stagedNativeModel = {
    capabilities: { toolCalling: true },
    sendRequest: async () => ({
      stream: (async function* stream() {
        for (const part of stagedNativeTurns.shift()) yield part;
      }()),
    }),
  };
  const stagedNativeCalls = [];
  const stagedNativeResult = JSON.parse(await internals.runVscodeLmAgent(
    stagedNativeModel,
    {
      requestId: "2".repeat(32),
      prompt: "stage native edit",
      allowedWrites: ["src/app.py"],
      path_contracts: {
        "src/app.py": {
          action: "edit",
          current_sha256: "a".repeat(64),
          line_count: 2,
          parent_existed: true,
        },
      },
      initial_source_graph_result: { ok: true, content: "prefetched graph" },
    },
    undefined,
    async (call) => {
      stagedNativeCalls.push(call);
      throw new Error("staged edits must not require an MCP round-trip");
    },
  ));
  assert.strictEqual(stagedNativeCalls.length, 0);
  assert.strictEqual(stagedNativeResult.summary, "Applied one staged native edit.");
  assert.strictEqual(stagedNativeResult.edits[0].ranges[0].new, "return 3");

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
  assert.strictEqual(nativePrefetchOptions[0].toolMode, fakeVscode.LanguageModelChatToolMode.Required);
  assert.deepStrictEqual(
    internals.vscodeLmToolsForRequest(
      { request_kind: "code" }, true, true,
    ).map((tool) => tool.name),
    ["aiworkhub_manager_semantic_edit_stage"],
  );

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
              name: "aiworkhub_worker_quality_review_submit",
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
  assert.strictEqual(nativeReviewCalls[0].name, "aiworkhub_worker_quality_review_submit");
  assert.strictEqual(nativeReviewOptions[0].toolMode, fakeVscode.LanguageModelChatToolMode.Required);
  assert.deepStrictEqual(
    nativeReviewOptions[0].tools.map((tool) => tool.name),
    ["aiworkhub_worker_source_graph_query", "aiworkhub_worker_quality_review_submit"],
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
      const lastMessage = _messages[_messages.length - 1];
      const lastUserText = lastMessage && lastMessage.role === "user" &&
        typeof lastMessage.content === "string" ? lastMessage.content : "";
      if (lastUserText.includes("The bounded discovery phase is complete")) {
        return {
          stream: (async function* stream() {
            yield {
              callId: `stage-${boundedTurns}`,
              name: "aiworkhub_manager_semantic_edit_stage",
              input: { operation: "create", file_path: "out/result.json", content: "{}\n" },
            };
          }()),
        };
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
    {
      requestId: "b".repeat(32),
      prompt: "bounded",
      allowedWrites: ["out/result.json"],
      path_contracts: {
        "out/result.json": {
          action: "create",
          current_sha256: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
          line_count: 0,
          parent_existed: false,
        },
      },
    },
    undefined,
    async () => ({ ok: true, content: "graph" }),
  );
  assert.deepStrictEqual(JSON.parse(forcedFinal), {
    schema_id: internals.constants.VSCODE_LM_EDIT_RESPONSE_SCHEMA,
    summary: "Applied validated staged semantic edits.",
    edits: [],
    creates: [{ path: "out/result.json", content: "{}\n" }],
  });
  assert.strictEqual(boundedTurns, 14);
  const boundedStageNames = boundedOptions[13].tools.map((tool) => tool.name);
  assert.deepStrictEqual(boundedStageNames, ["aiworkhub_manager_semantic_edit_stage"]);
  assert.strictEqual(
    boundedOptions[13].toolMode,
    fakeVscode.LanguageModelChatToolMode.Required,
  );

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
      const lastMessage = _messages[_messages.length - 1];
      const lastUserText = lastMessage && lastMessage.role === "user" &&
        typeof lastMessage.content === "string" ? lastMessage.content : "";
      if (lastUserText.includes("The bounded discovery phase is complete")) {
        return {
          stream: (async function* stream() {
            yield {
              callId: "stage",
              name: "aiworkhub_manager_semantic_edit_stage",
              input: {
                operation: "create",
                file_path: "out/result.json",
                content: "{}\n",
              },
            };
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
    {
      requestId: "c".repeat(32),
      prompt: "bounded",
      allowedWrites: ["out/result.json"],
      path_contracts: {
        "out/result.json": {
          action: "create",
          current_sha256: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
          line_count: 0,
          parent_existed: false,
        },
      },
    },
    undefined,
    async () => ({ ok: true, content: "graph" }),
  );
  assert.deepStrictEqual(JSON.parse(emptyForcedFinal), {
    schema_id: internals.constants.VSCODE_LM_EDIT_RESPONSE_SCHEMA,
    summary: "Applied validated staged semantic edits.",
    edits: [],
    creates: [{ path: "out/result.json", content: "{}\n" }],
  });
  assert.strictEqual(emptyTurns, 14);
  const emptyStageNames = emptyOptions[13].tools.map((tool) => tool.name);
  assert.deepStrictEqual(emptyStageNames, ["aiworkhub_manager_semantic_edit_stage"]);
  assert.strictEqual(
    emptyOptions[13].toolMode,
    fakeVscode.LanguageModelChatToolMode.Required,
  );

  // During forced staging only the offline stage tool is advertised; prior
  // assistant/tool-result history remains valid independently of advertisement.
  let historyCompatTurns = 0;
  const historyCompatModel = {
    capabilities: { toolCalling: true },
    sendRequest: async (_messages, options) => {
      historyCompatTurns += 1;
      if (!Object.prototype.hasOwnProperty.call(options, "tools")) {
        return { stream: (async function* stream() { yield { value: finalResponse }; }()) };
      }
      const lastMessage = _messages[_messages.length - 1];
      const lastUserText = lastMessage && lastMessage.role === "user" &&
        typeof lastMessage.content === "string" ? lastMessage.content : "";
      if (lastUserText.includes("The bounded discovery phase is complete")) {
        const availableNames = options.tools.map((tool) => tool.name);
        if (availableNames.length !== 1 ||
            availableNames[0] !== "aiworkhub_manager_semantic_edit_stage") {
          throw new Error("forced_stage_unexpected_tool_set");
        }
        return {
          stream: (async function* stream() {
            yield {
              callId: "stage-compat",
              name: "aiworkhub_manager_semantic_edit_stage",
              input: { operation: "create", file_path: "out/result.json", content: "{}\n" },
            };
          }()),
        };
      }
      return {
        stream: (async function* stream() {
          yield {
            callId: `history-${historyCompatTurns}`,
            name: "aiworkhub_manager_source_graph_query",
            input: { mode: "focus", query: `model-${historyCompatTurns}` },
          };
        }()),
      };
    },
  };
  const historyCompatResult = await internals.runVscodeLmAgent(
    historyCompatModel,
    {
      requestId: "d".repeat(32),
      prompt: "bounded",
      allowedWrites: ["out/result.json"],
      path_contracts: {
        "out/result.json": {
          action: "create",
          current_sha256: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
          line_count: 0,
          parent_existed: false,
        },
      },
    },
    undefined,
    async () => ({ ok: true, content: "graph" }),
  );
  assert.deepStrictEqual(JSON.parse(historyCompatResult), {
    schema_id: internals.constants.VSCODE_LM_EDIT_RESPONSE_SCHEMA,
    summary: "Applied validated staged semantic edits.",
    edits: [],
    creates: [{ path: "out/result.json", content: "{}\n" }],
  });

  // NF164: one bounded corrective turn for a non-stage call, then the exact stage
  // tool is accepted; the rejected call must never reach MCP.
  let correctiveTurns = 0;
  const correctiveExecutedCalls = [];
  const correctiveModel = {
    capabilities: { toolCalling: true },
    sendRequest: async (_messages, options) => {
      correctiveTurns += 1;
      if (!Object.prototype.hasOwnProperty.call(options, "tools")) {
        return { stream: (async function* stream() { yield { value: finalResponse }; }()) };
      }
      const lastMessage = _messages[_messages.length - 1];
      const lastUserText = lastMessage && lastMessage.role === "user" &&
        typeof lastMessage.content === "string" ? lastMessage.content : "";
      if (lastUserText.includes("bounded semantic-edit stage")) {
        return {
          stream: (async function* stream() {
            yield {
              callId: "stage-after-correction",
              name: "aiworkhub_manager_semantic_edit_stage",
              input: { operation: "create", file_path: "out/result.json", content: "{}\n" },
            };
          }()),
        };
      }
      if (lastUserText.includes("The bounded discovery phase is complete")) {
        return {
          stream: (async function* stream() {
            yield {
              callId: "forbidden-non-stage",
              name: "aiworkhub_manager_source_graph_query",
              input: { mode: "focus", query: "forbidden-non-stage" },
            };
          }()),
        };
      }
      return {
        stream: (async function* stream() {
          yield {
            callId: `pre-${correctiveTurns}`,
            name: "aiworkhub_manager_source_graph_query",
            input: { mode: "focus", query: `model-${correctiveTurns}` },
          };
        }()),
      };
    },
  };
  const correctiveResult = await internals.runVscodeLmAgent(
    correctiveModel,
    {
      requestId: "e".repeat(32),
      prompt: "bounded",
      allowedWrites: ["out/result.json"],
      path_contracts: {
        "out/result.json": {
          action: "create",
          current_sha256: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
          line_count: 0,
          parent_existed: false,
        },
      },
    },
    undefined,
    async (call) => {
      correctiveExecutedCalls.push(call.name + ":" + (call.input && call.input.query || ""));
      return { ok: true, content: "graph" };
    },
  );
  assert.deepStrictEqual(JSON.parse(correctiveResult), {
    schema_id: internals.constants.VSCODE_LM_EDIT_RESPONSE_SCHEMA,
    summary: "Applied validated staged semantic edits.",
    edits: [],
    creates: [{ path: "out/result.json", content: "{}\n" }],
  });
  assert.strictEqual(correctiveTurns, 15);
  assert.ok(!correctiveExecutedCalls.some((entry) => entry.includes("forbidden-non-stage")));

  // NF164: a repeated non-stage call fails structurally with
  // vscode_lm_semantic_edit_stage_required after one bounded corrective turn.
  let repeatedViolationTurns = 0;
  const repeatedViolationModel = {
    capabilities: { toolCalling: true },
    sendRequest: async (_messages, options) => {
      repeatedViolationTurns += 1;
      if (!Object.prototype.hasOwnProperty.call(options, "tools")) {
        return { stream: (async function* stream() { yield { value: finalResponse }; }()) };
      }
      const lastMessage = _messages[_messages.length - 1];
      const lastUserText = lastMessage && lastMessage.role === "user" &&
        typeof lastMessage.content === "string" ? lastMessage.content : "";
      if (lastUserText.includes("bounded semantic-edit stage") ||
          lastUserText.includes("The bounded discovery phase is complete")) {
        return {
          stream: (async function* stream() {
            yield {
              callId: `bad-${repeatedViolationTurns}`,
              name: "aiworkhub_manager_source_graph_query",
              input: { mode: "focus", query: "again-non-stage" },
            };
          }()),
        };
      }
      return {
        stream: (async function* stream() {
          yield {
            callId: `pre-${repeatedViolationTurns}`,
            name: "aiworkhub_manager_source_graph_query",
            input: { mode: "focus", query: `model-${repeatedViolationTurns}` },
          };
        }()),
      };
    },
  };
  await assert.rejects(
    internals.runVscodeLmAgent(
      repeatedViolationModel,
      {
        requestId: "f".repeat(32),
        prompt: "bounded",
        allowedWrites: ["out/result.json"],
        path_contracts: {
          "out/result.json": {
            action: "create",
            current_sha256: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            line_count: 0,
            parent_existed: false,
          },
        },
      },
      undefined,
      async () => ({ ok: true, content: "graph" }),
    ),
    /vscode_lm_semantic_edit_stage_required/,
  );

  // NF164: the exact stage tool is always handled offline; an MCP stub that
  // throws mcp_unavailable must never be invoked for it.
  let mcpUnavailableTurns = 0;
  const mcpUnavailableCalls = [];
  const mcpUnavailableModel = {
    capabilities: { toolCalling: true },
    sendRequest: async (_messages, options) => {
      mcpUnavailableTurns += 1;
      if (!Object.prototype.hasOwnProperty.call(options, "tools")) {
        return { stream: (async function* stream() { yield { value: finalResponse }; }()) };
      }
      const lastMessage = _messages[_messages.length - 1];
      const lastUserText = lastMessage && lastMessage.role === "user" &&
        typeof lastMessage.content === "string" ? lastMessage.content : "";
      if (lastUserText.includes("The bounded discovery phase is complete")) {
        return {
          stream: (async function* stream() {
            yield {
              callId: "stage-offline",
              name: "aiworkhub_manager_semantic_edit_stage",
              input: { operation: "create", file_path: "out/result.json", content: "{}\n" },
            };
          }()),
        };
      }
      return {
        stream: (async function* stream() {
          yield {
            callId: `sg-${mcpUnavailableTurns}`,
            name: "aiworkhub_manager_source_graph_query",
            input: { mode: "focus", query: `model-${mcpUnavailableTurns}` },
          };
        }()),
      };
    },
  };
  const mcpUnavailableResult = await internals.runVscodeLmAgent(
    mcpUnavailableModel,
    {
      requestId: "g".repeat(32),
      prompt: "bounded",
      allowedWrites: ["out/result.json"],
      path_contracts: {
        "out/result.json": {
          action: "create",
          current_sha256: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
          line_count: 0,
          parent_existed: false,
        },
      },
    },
    undefined,
    async (call) => {
      mcpUnavailableCalls.push(call.name);
      if (call.name === "aiworkhub_manager_semantic_edit_stage") throw new Error("mcp_unavailable");
      return { ok: true, content: "graph" };
    },
  );
  assert.deepStrictEqual(JSON.parse(mcpUnavailableResult), {
    schema_id: internals.constants.VSCODE_LM_EDIT_RESPONSE_SCHEMA,
    summary: "Applied validated staged semantic edits.",
    edits: [],
    creates: [{ path: "out/result.json", content: "{}\n" }],
  });
  assert.ok(!mcpUnavailableCalls.some((name) => name === "aiworkhub_manager_semantic_edit_stage"));
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

// NF-2026-00168: strict fake-provider history validation — verify that corrective
// retry never leaves unmatched assistant tool-call parts in the provider history.
async function nf168ProviderHistoryValidation() {
  const finalResponse = JSON.stringify({
    schema_id: internals.constants.VSCODE_LM_EDIT_RESPONSE_SCHEMA,
    summary: "Applied validated staged semantic edits.",
    edits: [],
    creates: [{ path: "out/result.json", content: "{}\n" }],
  });

  // Track invokeCount for cross-role enforcement: manager request must not invoke
  // worker-prefixed tools, and vice versa.
  const invokeCalls = [];
  const invokeCountByRole = { manager: 0, worker: 0 };

  // Track the message history the fake model receives on the corrective retry turn.
  // Retain raw message arrays from the actual corrective forced-stage sendRequest
  // turn. Never reconstruct synthetic contentParts — the raw content is already
  // in provider-compatible format (VS Code LanguageModelChatMessage content arrays).
  let retryMessages = null;
  let forceStagedViolationTurns = 0;
  const historyValidatorModel = {
    capabilities: { toolCalling: true },
    sendRequest: async (_messages, options) => {
      forceStagedViolationTurns += 1;
      if (!Object.prototype.hasOwnProperty.call(options, "tools")) {
        return { stream: (async function* stream() { yield { value: finalResponse }; }()) };
      }
      const lastMessage = _messages[_messages.length - 1];
      const lastUserText = lastMessage && lastMessage.role === "user" &&
        typeof lastMessage.content === "string" ? lastMessage.content : "";
      if (lastUserText.includes("bounded semantic-edit stage")) {
        // This is the corrective retry turn — capture the full raw history for inspection.
        // Shallow-clone each message but retain the original content arrays.
        retryMessages = _messages.map((msg) => ({
          role: msg.role,
          content: msg.content,
        }));
        return {
          stream: (async function* stream() {
            yield {
              callId: "stage-after-nf168",
              name: "aiworkhub_manager_semantic_edit_stage",
              input: { operation: "create", file_path: "out/result.json", content: "{}\n" },
            };
          }()),
        };
      }
      if (lastUserText.includes("The bounded discovery phase is complete")) {
        return {
          stream: (async function* stream() {
            yield {
              callId: "forbidden-non-stage-nf168",
              name: "aiworkhub_manager_source_graph_query",
              input: { mode: "focus", query: "forbidden-non-stage" },
            };
          }()),
        };
      }
      return {
        stream: (async function* stream() {
          yield {
            callId: `pre-${forceStagedViolationTurns}`,
            name: "aiworkhub_manager_source_graph_query",
            input: { mode: "focus", query: `model-${forceStagedViolationTurns}` },
          };
        }()),
      };
    },
  };

  const nf168Result = await internals.runVscodeLmAgent(
    historyValidatorModel,
    {
      requestId: "h".repeat(32),
      request_kind: "manager",
      prompt: "bounded",
      allowedWrites: ["out/result.json"],
      path_contracts: {
        "out/result.json": {
          action: "create",
          current_sha256: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
          line_count: 0,
          parent_existed: false,
        },
      },
    },
    undefined,
    async (call) => {
      invokeCalls.push({ name: call.name, input: call.input });
      if (call.name.startsWith("aiworkhub_worker_")) invokeCountByRole.worker += 1;
      if (call.name.startsWith("aiworkhub_manager_")) invokeCountByRole.manager += 1;
      return { ok: true, content: "graph" };
    },
  );
  assert.deepStrictEqual(JSON.parse(nf168Result), {
    schema_id: internals.constants.VSCODE_LM_EDIT_RESPONSE_SCHEMA,
    summary: "Applied validated staged semantic edits.",
    edits: [],
    creates: [{ path: "out/result.json", content: "{}\n" }],
  });
  assert.ok(retryMessages !== null, "corrective retry must have been triggered");

  // NF-2026-00168: invokeCount=0 cross-role. A manager request must never
  // invoke worker-prefixed tools.
  assert.strictEqual(invokeCountByRole.worker, 0,
    "manager request must have zero worker tool invocations");
  assert.ok(invokeCountByRole.manager > 0,
    "manager request must have invoked manager tools");

  // NF-2026-00168: directly validate raw message history with exact callId sets.
  // The raw content arrays are VS Code LanguageModelChatMessage parts — already
  // in provider-compatible format. Assistant content arrays contain tool-call
  // parts ({callId, name, input}) and text parts ({value}). User result content
  // arrays contain LanguageModelToolResultPart-compatible parts ({callId, content}).
  // Verify exact adjacent assistant callId ↔ user result callId set equality.
  let lastAssistantHadToolCalls = false;
  for (let i = 0; i < retryMessages.length; i += 1) {
    const msg = retryMessages[i];
    if (msg.role === "assistant" && Array.isArray(msg.content)) {
      const toolCallParts = msg.content.filter(
        (p) => typeof p === "object" && p !== null &&
          Object.prototype.hasOwnProperty.call(p, "callId") &&
          Object.prototype.hasOwnProperty.call(p, "name"),
      );
      if (toolCallParts.length > 0) {
        if (lastAssistantHadToolCalls) {
          assert.fail("consecutive assistant messages with tool-call parts — unmatched");
        }
        lastAssistantHadToolCalls = true;
        const next = retryMessages[i + 1];
        assert.ok(
          next && next.role === "user" && Array.isArray(next.content),
          `assistant[${i}] tool-calls must be followed by user with tool-result parts`,
        );
        const toolCallIds = new Set(toolCallParts.map((tc) => tc.callId));
        // Detect LanguageModelToolResultPart-compatible parts: must have
        // both callId (string) and content (not undefined).
        const resultParts = next.content.filter(
          (p) => p && typeof p.callId === "string" && p.content !== undefined,
        );
        const resultCallIds = new Set(resultParts.map((rp) => rp.callId));
        // NF-2026-00168: exact callId set equality — every tool-call must have
        // a matching LanguageModelToolResultPart-compatible result, and every
        // result must match a tool-call (no orphaned results).
        for (const cid of toolCallIds) {
          assert.ok(resultCallIds.has(cid),
            `assistant[${i}] callId=${cid}:unmatched — no result part in user[${i + 1}]`);
        }
        for (const cid of resultCallIds) {
          assert.ok(toolCallIds.has(cid),
            `user[${i + 1}] callId=${cid}:orphaned_result — no tool-call in assistant[${i}]`);
        }
        // NF-2026-00168: each result part must have concrete content (not undefined).
        for (const rp of resultParts) {
          assert.ok(rp.content !== undefined && rp.content !== null,
            `user[${i + 1}] callId=${rp.callId} result content must be concrete`);
        }
      } else {
        lastAssistantHadToolCalls = false;
      }
    } else if (msg.role === "user") {
      lastAssistantHadToolCalls = false;
    }
  }
  assert.strictEqual(lastAssistantHadToolCalls, false,
    "final assistant message must not have unmatched tool-call parts");

  // NF-2026-00168: direct validateProviderHistory call on the raw captured
  // message history. The raw messages are already in provider-compatible format
  // — no synthetic reconstruction needed.
  const unmatched = internals.validateProviderHistory(retryMessages);
  assert.deepStrictEqual(unmatched, [],
    `validateProviderHistory found unmatched parts: ${unmatched.join("; ")}`);
}

// NF-2026-00168: validateProviderHistory unit test — direct validation of
// the fake-provider history validator with known good and bad histories.
async function nf168ValidateProviderHistoryUnit() {
  const { validateProviderHistory } = internals;

  // Valid: empty history, no tool calls.
  assert.deepStrictEqual(validateProviderHistory([]), []);

  // Valid: assistant text-only message (no unmatched parts).
  assert.deepStrictEqual(validateProviderHistory([
    { role: "assistant", content: [{ value: "hello" }] },
    { role: "user", content: "reply" },
  ]), []);

  // Valid: assistant tool-call followed by user with matching callId + content
  // (LanguageModelToolResultPart-compatible) result part.
  assert.deepStrictEqual(validateProviderHistory([
    { role: "assistant", content: [{ callId: "abc", name: "test", input: {} }] },
    { role: "user", content: [{ callId: "abc", content: [{ type: "text", text: "ok" }] }] },
  ]), []);

  // Valid: consecutive assistants without tool-calls.
  assert.deepStrictEqual(validateProviderHistory([
    { role: "assistant", content: [{ value: "first" }] },
    { role: "user", content: "middle" },
    { role: "assistant", content: [{ value: "second" }] },
  ]), []);

  // Invalid: assistant tool-call with no subsequent user message.
  const noUser = validateProviderHistory([
    { role: "assistant", content: [{ callId: "orphan", name: "orphan", input: {} }] },
  ]);
  assert.strictEqual(noUser.length, 1);
  assert.ok(noUser[0].includes("no_user_result"), `expected no_user_result, got ${noUser[0]}`);

  // Invalid: assistant tool-call followed by non-user role.
  const badRole = validateProviderHistory([
    { role: "assistant", content: [{ callId: "abc", name: "t", input: {} }] },
    { role: "assistant", content: [{ value: "oops" }] },
  ]);
  assert.strictEqual(badRole.length, 1);
  assert.ok(badRole[0].includes("no_user_result"));

  // Invalid: assistant tool-call with user that has no matching callId.
  // NF-2026-00168: exact callId set equality — tool-call "abc" is unmatched
  // AND result "xyz" is orphaned, producing two errors.
  const unmatched = validateProviderHistory([
    { role: "assistant", content: [{ callId: "abc", name: "t", input: {} }] },
    { role: "user", content: [{ callId: "xyz", content: [{ type: "text", text: "ok" }] }] },
  ]);
  assert.strictEqual(unmatched.length, 2);
  assert.ok(unmatched.some((e) => e.includes("unmatched")), `expected unmatched, got ${unmatched.join(";")}`);

  // Invalid: user result part missing content (not LanguageModelToolResultPart-compatible).
  const missingContent = validateProviderHistory([
    { role: "assistant", content: [{ callId: "abc", name: "t", input: {} }] },
    { role: "user", content: [{ callId: "abc", name: "result", value: "ok" }] },
  ]);
  assert.strictEqual(missingContent.length, 1);
  assert.ok(missingContent[0].includes("unmatched"),
    `expected unmatched (no content), got ${missingContent[0]}`);

  // Invalid: standalone user result without preceding assistant — must fail.
  // NF-2026-00168: validateProviderHistory rejects orphan result-only histories.
  const orphanedResult = validateProviderHistory([
    { role: "user", content: [{ callId: "ghost", content: [{ type: "text", text: "??" }] }] },
  ]);
  assert.strictEqual(orphanedResult.length, 1);
  assert.ok(orphanedResult[0].includes("orphaned_no_tool_call"),
    `expected orphaned_no_tool_call, got ${orphanedResult[0]}`);
  // NF-2026-00168: third pass — orphan result part in a user message whose
  // immediately preceding assistant turn lacks the exact matching callId,
  // even when other valid call/result pairs exist in the same history.
  const orphanWithValid = validateProviderHistory([
    { role: "assistant", content: [{ callId: "callA", name: "good", input: {} }] },
    { role: "user", content: [{ callId: "callA", content: [{ type: "text", text: "ok" }] },
                              { callId: "callB", content: [{ type: "text", text: "orphan" }] }] },
  ]);
  assert.strictEqual(orphanWithValid.length, 1,
    `expected 1 orphaned_result, got ${JSON.stringify(orphanWithValid)}`);
  assert.ok(orphanWithValid[0].includes("orphaned_result"),
    `expected orphaned_result, got ${orphanWithValid[0]}`);
  // NF-2026-00168: result with non-assistant predecessor (e.g., system turns)
  // is flagged by the secondary pass (no tool-call exists anywhere).
  const orphanAfterSystem = validateProviderHistory([
    { role: "system", content: "boot" },
    { role: "user", content: [{ callId: "orphanSys", content: [{ type: "text", text: "?" }] }] },
  ]);
  assert.strictEqual(orphanAfterSystem.length, 1,
    `expected 1 orphaned_no_tool_call after system, got ${JSON.stringify(orphanAfterSystem)}`);
  assert.ok(orphanAfterSystem[0].includes("orphaned_no_tool_call"),
    `expected orphaned_no_tool_call, got ${orphanAfterSystem[0]}`);
  // Invalid: non-array messages.
  const bad = validateProviderHistory({});
  assert.deepStrictEqual(bad, ["messages_must_be_array"]);
}

// NF-2026-00169: worker Source Graph acknowledgement in both native and fallback
// text protocol paths. A worker-scoped source graph tool must trigger
// sourceGraphAcknowledged without accepting manager tools in worker authority.
async function nf169WorkerSourceGraphAck() {
  // Native protocol: worker source_graph_query tool call.
  const nativeFinal = JSON.stringify({
    schema_id: internals.constants.VSCODE_LM_EDIT_RESPONSE_SCHEMA,
    summary: "native worker sg",
    edits: [],
    creates: [{ path: "out/result.json", content: "{}\n" }],
  });
  let nativeWorkerSgTurns = 0;
  const workerSgModel = {
    capabilities: { toolCalling: true },
    sendRequest: async (_messages, options) => {
      nativeWorkerSgTurns += 1;
      if (!Object.prototype.hasOwnProperty.call(options, "tools")) {
        return { stream: (async function* stream() { yield { value: nativeFinal }; }()) };
      }
      const lastMessage = _messages[_messages.length - 1];
      const lastUserText = lastMessage && lastMessage.role === "user" &&
        typeof lastMessage.content === "string" ? lastMessage.content : "";
      if (lastUserText.includes("The bounded discovery phase is complete")) {
        return {
          stream: (async function* stream() {
            yield {
              callId: "stage-nf169",
              name: "aiworkhub_manager_semantic_edit_stage",
              input: { operation: "create", file_path: "out/result.json", content: "{}\n" },
            };
          }()),
        };
      }
      return {
        stream: (async function* stream() {
          yield {
            callId: `wsg-${nativeWorkerSgTurns}`,
            name: "aiworkhub_worker_source_graph_query",
            input: { mode: "focus", query: `worker-sg-${nativeWorkerSgTurns}` },
          };
        }()),
      };
    },
  };
  const workerNativeResult = await internals.runVscodeLmAgent(
    workerSgModel,
    {
      requestId: "i".repeat(32),
      request_kind: "worker",
      prompt: "bounded",
      allowedWrites: ["out/result.json"],
      path_contracts: {
        "out/result.json": {
          action: "create",
          current_sha256: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
          line_count: 0,
          parent_existed: false,
        },
      },
    },
    undefined,
    async (call) => {
      // NF-2026-00169: invokeCount=0 cross-role. Worker must never invoke
      // manager-scoped tools; manager tools are rejected at the authority gate.
      assert.ok(
        call.name === "aiworkhub_worker_source_graph_query" ||
        call.name === "aiworkhub_manager_semantic_edit_stage",
        `worker invokeTool must only invoke worker or bridge tools, got: ${call.name}`,
      );
      assert.ok(
        !call.name.startsWith("aiworkhub_manager_") ||
        call.name === "aiworkhub_manager_semantic_edit_stage",
        `worker must not invoke manager-scoped tools: ${call.name}`,
      );
      return { ok: true, content: "graph" };
    },
  );
  assert.deepStrictEqual(JSON.parse(workerNativeResult), {
    schema_id: internals.constants.VSCODE_LM_EDIT_RESPONSE_SCHEMA,
    summary: "Applied validated staged semantic edits.",
    edits: [],
    creates: [{ path: "out/result.json", content: "{}\n" }],
  });

  // Text protocol fallback: worker source_graph_query in JSON envelope.
  const workerToolRequest = JSON.stringify({
    schema_id: internals.constants.VSCODE_LM_TOOL_REQUEST_SCHEMA,
    name: "aiworkhub_worker_source_graph_query",
    input: { mode: "focus", query: "worker-text", budget: 48 },
  });
  const textFinal = JSON.stringify({
    schema_id: internals.constants.VSCODE_LM_EDIT_RESPONSE_SCHEMA,
    summary: "text worker sg",
    edits: [],
    creates: [{ path: "out/result.json", content: "{}\n" }],
  });
  const textQueued = [workerToolRequest, textFinal];
  const workerTextCalls = [];
  const workerTextModel = {
    capabilities: { toolCalling: false },
    sendRequest: async () => ({
      stream: (async function* stream() { yield { value: textQueued.shift() }; }()),
    }),
  };
  const workerTextResult = await internals.runVscodeLmTextProtocol(
    workerTextModel,
    { prompt: "bounded", request_kind: "worker", allowedWrites: ["out/result.json"] },
    undefined,
    async (call) => {
      workerTextCalls.push(call);
      return { ok: true, content: "graph" };
    },
  );
  assert.strictEqual(workerTextResult, textFinal);
  assert.strictEqual(workerTextCalls.length, 1);
  assert.strictEqual(workerTextCalls[0].name, "aiworkhub_worker_source_graph_query");

  // Contextless worker fallback: a worker request with NO initial source graph
  // context should still acknowledge the worker tool and not throw
  // source_graph_not_acknowledged.
  const contextlessFinal = JSON.stringify({
    schema_id: internals.constants.VSCODE_LM_EDIT_RESPONSE_SCHEMA,
    summary: "contextless fallback",
    edits: [],
    creates: [],
  });
  const contextlessCalls = [];
  const contextlessModel = {
    capabilities: { toolCalling: false },
    sendRequest: async () => ({
      stream: (async function* stream() {
        yield { value: JSON.stringify({
          schema_id: internals.constants.VSCODE_LM_TOOL_REQUEST_SCHEMA,
          name: "aiworkhub_worker_source_graph_query",
          input: { mode: "focus", query: "contextless", budget: 48 },
        }) };
        yield { value: contextlessFinal };
      }()),
    }),
  };
  const contextlessResult = await internals.runVscodeLmTextProtocol(
    contextlessModel,
    { prompt: "contextless worker", request_kind: "worker", allowedWrites: [] },
    undefined,
    async (call) => {
      contextlessCalls.push(call);
      return { ok: true, content: "graph" };
    },
  );
  assert.strictEqual(contextlessResult, contextlessFinal);
  assert.strictEqual(contextlessCalls.length, 1);
  assert.strictEqual(contextlessCalls[0].name, "aiworkhub_worker_source_graph_query");
}

async function nf383McpFirstCallReadinessChecks() {
  const requestId = "e".repeat(32);
  const durableReceipt = { ok: true, durable: true, content: "graph" };
  const finalResponse = JSON.stringify({
    schema_id: internals.constants.VSCODE_LM_EDIT_RESPONSE_SCHEMA,
    summary: "readiness",
    edits: [],
    creates: [],
  });
  const workerSg = "aiworkhub_worker_source_graph_query";
  const bindReady = () => internals.bindVscodeLmProviderBridgeForTest({
    mcpClient: { repositoryRoot: "/tmp/nf383-repo" },
    activeRepoIdentity: { root: "/tmp/nf383-repo" },
  });
  const collector = () => internals.createVscodeLmStagedEditCollector({
    allowedWrites: ["out/result.json"],
    path_contracts: {
      "out/result.json": {
        action: "create",
        current_sha256: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        line_count: 0,
        parent_existed: false,
      },
    },
  });
  const runProtocol = async (kind, toolName, input, invokeTool, requestKind = "worker", extra = {}) => {
    const request = {
      requestId,
      request_kind: requestKind,
      prompt: "readiness",
      allowedWrites: extra.allowedWrites || [],
      path_contracts: extra.path_contracts || {},
      initial_source_graph_request: extra.initial_source_graph_request,
      initial_source_graph_result: extra.initial_source_graph_result,
    };
    if (kind === "text") {
      const queued = [
        JSON.stringify({
          schema_id: internals.constants.VSCODE_LM_TOOL_REQUEST_SCHEMA,
          name: toolName,
          input,
        }),
        extra.finalResponse || finalResponse,
      ];
      const model = {
        capabilities: { toolCalling: false },
        sendRequest: async () => ({
          stream: (async function* stream() { yield { value: queued.shift() }; }()),
        }),
      };
      return internals.runVscodeLmTextProtocol(model, request, undefined, invokeTool);
    }
    let turn = 0;
    const model = {
      capabilities: { toolCalling: true },
      sendRequest: async () => {
        turn += 1;
        if (turn === 1) {
          return {
            stream: (async function* stream() {
              yield { callId: "nf383-1", name: toolName, input };
            }()),
          };
        }
        return {
          stream: (async function* stream() { yield { value: extra.finalResponse || finalResponse }; }()),
        };
      },
    };
    return internals.runVscodeLmAgent(model, request, undefined, invokeTool);
  };

  try {
    for (const kind of ["native", "text"]) {
      internals.resetVscodeLmWorkerSourceGraphReadinessForTest();
      internals.armVscodeLmProviderBridgeReadiness({ forceWaitForTest: true });
      internals.setVscodeLmWorkerSourceGraphReadyTimeoutMsForTest(200);
      const successCalls = [];
      const bindTimer = setTimeout(bindReady, 15);
      const successResult = await runProtocol(
        kind,
        workerSg,
        { mode: "focus", query: `nf383-${kind}-success` },
        async (call) => {
          successCalls.push(call.name);
          return durableReceipt;
        },
      );
      clearTimeout(bindTimer);
      assert.strictEqual(successResult, finalResponse);
      assert.deepStrictEqual(successCalls, [workerSg]);

      internals.resetVscodeLmWorkerSourceGraphReadinessForTest();
      internals.armVscodeLmProviderBridgeReadiness({ forceWaitForTest: true });
      internals.setVscodeLmWorkerSourceGraphReadyTimeoutMsForTest(50);
      const failCalls = [];
      const failStarted = Date.now();
      await assert.rejects(
        runProtocol(
          kind,
          workerSg,
          { mode: "focus", query: `nf383-${kind}-fail` },
          async (call) => {
            failCalls.push(call.name);
            return durableReceipt;
          },
        ),
        /vscode_lm_(mcp_unavailable|source_graph_not_acknowledged)/,
      );
      assert.deepStrictEqual(failCalls, []);
      assert.ok(Date.now() - failStarted >= 40);

      const laterCalls = [];
      const laterStarted = Date.now();
      await assert.rejects(
        internals.invokeVscodeLmProtocolTool(
          { name: workerSg, input: { mode: "focus", query: `nf383-${kind}-later` } },
          requestId,
          async (call) => {
            laterCalls.push(call.name);
            return durableReceipt;
          },
          collector(),
        ),
        /vscode_lm_mcp_unavailable/,
      );
      assert.deepStrictEqual(laterCalls, []);
      assert.ok(Date.now() - laterStarted < 40);

      internals.resetVscodeLmWorkerSourceGraphReadinessForTest();
      internals.armVscodeLmProviderBridgeReadiness({ forceWaitForTest: true });
      internals.setVscodeLmWorkerSourceGraphReadyTimeoutMsForTest(200);
      const writePrefetch = {
        initial_source_graph_request: { mode: "focus", query: `nf383-${kind}-prefetch` },
        initial_source_graph_result: { ok: true, content: "prefetched graph" },
      };
      for (const [toolName, requestKind, input, extra] of [
        ["aiworkhub_manager_source_graph_query", "manager", { mode: "focus", query: `nf383-${kind}-manager` }, {}],
        ["aiworkhub_worker_session_write_intent", "worker", { action: "upsert", content: "x", idempotency_key: "k", provenance: "test" }, writePrefetch],
      ]) {
        const exclusionCalls = [];
        const exclusionStarted = Date.now();
        await runProtocol(
          kind,
          toolName,
          input,
          async (call) => {
            exclusionCalls.push(call.name);
            return durableReceipt;
          },
          requestKind,
          extra,
        );
        assert.ok(Date.now() - exclusionStarted < 80, `${kind} ${toolName} waited for worker SG readiness`);
        assert.deepStrictEqual(exclusionCalls, [toolName]);
      }
      const stageCalls = [];
      const stageStarted = Date.now();
      await internals.invokeVscodeLmProtocolTool(
        {
          name: "aiworkhub_manager_semantic_edit_stage",
          input: { operation: "create", file_path: "out/result.json", content: "{}\n" },
        },
        requestId,
        async (call) => {
          stageCalls.push(call.name);
          return durableReceipt;
        },
        collector(),
      );
      assert.ok(Date.now() - stageStarted < 80, `${kind} stage waited for worker SG readiness`);
      assert.deepStrictEqual(stageCalls, []);
    }

    internals.resetVscodeLmWorkerSourceGraphReadinessForTest();
    internals.armVscodeLmProviderBridgeReadiness({ forceWaitForTest: true });
    internals.setVscodeLmWorkerSourceGraphReadyTimeoutMsForTest(50);
    await assert.rejects(
      internals.invokeVscodeLmProtocolTool(
        { name: workerSg, input: { mode: "focus", query: "nf383-direct-fail" } },
        requestId,
        async () => durableReceipt,
        collector(),
      ),
      /vscode_lm_mcp_unavailable/,
    );

    internals.resetVscodeLmWorkerSourceGraphReadinessForTest();
    let privateCalls = 0;
    internals.bindVscodeLmProviderBridgeForTest({
      mcpClient: {
        repositoryRoot: "/tmp/nf383-a",
        callTool: async () => {
          privateCalls += 1;
          return durableReceipt;
        },
      },
      activeRepoIdentity: { root: "/tmp/nf383-b" },
    });
    await assert.rejects(
      internals.invokeVscodeLmProtocolTool(
        { name: workerSg, input: { mode: "focus", query: "nf383-mismatch" } },
        requestId,
        internals.invokeVscodeLmPrivateTool,
        collector(),
      ),
      /vscode_lm_mcp_repo_mismatch/,
    );
    assert.strictEqual(privateCalls, 0);

    internals.resetVscodeLmWorkerSourceGraphReadinessForTest();
    const host = new internals.VscodeLmBridgeHost({});
    await host.start({ repoId: `repo_${"a".repeat(32)}`, root: "/tmp/nf383-host" });
    internals.armVscodeLmProviderBridgeReadiness({ forceWaitForTest: true });
    internals.setVscodeLmWorkerSourceGraphReadyTimeoutMsForTest(200);
    const hostCalls = [];
    const hostTimer = setTimeout(bindReady, 15);
    const hostResult = await internals.invokeVscodeLmProtocolTool(
      { name: workerSg, input: { mode: "focus", query: "nf383-host-arm" } },
      requestId,
      async (call) => {
        hostCalls.push(call.name);
        return durableReceipt;
      },
      collector(),
    );
    clearTimeout(hostTimer);
    host.stop();
    assert.deepStrictEqual(hostResult, durableReceipt);
    assert.deepStrictEqual(hostCalls, [workerSg]);
  } finally {
    internals.resetVscodeLmWorkerSourceGraphReadinessForTest();
  }
}

// NF-2026-00169: contextless worker native protocol regression test.
// A worker request with no initial source graph context (null prefetch)
// must correctly acknowledge the worker SG tool on the native path,
// without accepting manager tools in worker authority.
async function nf169ContextlessWorkerNative() {
  let nativeTurns = 0;
  let acknowledged = false;
  let wrongRoleSeen = false;
  const invokeCalls = [];
  const invokeCountByRole = { manager: 0, worker: 0 };
  const finalResponse = JSON.stringify({
    schema_id: internals.constants.VSCODE_LM_EDIT_RESPONSE_SCHEMA,
    summary: "contextless native ok",
    edits: [],
    creates: [],
  });
  const model = {
    capabilities: { toolCalling: true },
    sendRequest: async (_messages, options) => {
      nativeTurns += 1;
      if (!Object.prototype.hasOwnProperty.call(options, "tools")) {
        return { stream: (async function* stream() { yield { value: finalResponse }; }()) };
      }
      // Contextless worker: first turn with tools — acknowledge worker SG.
      acknowledged = true;
      return {
        stream: (async function* stream() {
          yield {
            callId: `ctxless-native-${nativeTurns}`,
            name: "aiworkhub_worker_source_graph_query",
            input: { mode: "focus", query: "contextless-native", budget: 48, workflow_stage: "orientation" },
          };
        }()),
      };
    },
  };
  // No initial_source_graph_result in request — truly contextless.
  // invokeTool is a mock that returns ok for any tool call.
  const result = await internals.runVscodeLmAgent(
    model,
    {
      requestId: "m".repeat(32),
      request_kind: "worker",
      prompt: "contextless worker native",
      allowedWrites: [],
      path_contracts: {},
    },
    undefined,
    async (call) => {
      invokeCalls.push({ name: call.name, input: call.input });
      if (call.name.startsWith("aiworkhub_worker_")) invokeCountByRole.worker += 1;
      if (call.name.startsWith("aiworkhub_manager_")) invokeCountByRole.manager += 1;
      // Acknowledge worker SG, reject manager SG (authority gate).
      if (call.name === "aiworkhub_worker_source_graph_query") {
        return { ok: true, content: "graph" };
      }
      if (call.name === "aiworkhub_manager_source_graph_query") {
        wrongRoleSeen = true;
        return { ok: false, error: "manager_sg_rejected_in_worker" };
      }
      return { ok: true, content: "done" };
    },
  );
  const parsed = JSON.parse(result);
  assert.strictEqual(parsed.summary, "contextless native ok");
  assert.ok(acknowledged, "worker SG tool must have been acknowledged on contextless native path");
  assert.strictEqual(wrongRoleSeen, false, "manager SG must not appear on contextless worker native path");
  assert.ok(invokeCalls.length > 0, "must have at least one tool invocation");
  assert.strictEqual(invokeCountByRole.manager, 0, "invokeCountByRole.manager must be 0 for worker request");
  assert.ok(invokeCountByRole.worker > 0, "invokeCountByRole.worker must be > 0 for worker request");
}

// NF-2026-00168: force-final callId pairing two-strikes test.
// First force-final violation gets one corrective retry; second violation
// must throw vscode_lm_finalization_tool_violation structurally.
async function nf168ForceFinalCallIdPairing() {
  let forceFinalTurns = 0;
  const invokeCountByRole = { manager: 0, worker: 0 };
  const finalResponse = JSON.stringify({
    schema_id: internals.constants.VSCODE_LM_EDIT_RESPONSE_SCHEMA,
    summary: "ok",
    edits: [],
    creates: [],
  });
  let correctiveTurnMessages = null;
  const violatorModel = {
    capabilities: { toolCalling: true },
    sendRequest: async (_messages, options) => {
      forceFinalTurns += 1;
      // Capture raw messages on the corrective-retry turn (when forceFinal is on).
      if (!Object.prototype.hasOwnProperty.call(options, "tools") && forceFinalTurns > 1) {
        correctiveTurnMessages = _messages.map((msg) => ({
          role: msg.role,
          content: msg.content,
        }));
      }
      // Always return tool calls — this triggers the force-final violation path
      // which produces tool_call_rejected protocolTrace entries.
      return {
        stream: (async function* stream() {
          yield {
            callId: `fv-${forceFinalTurns}`,
            name: "aiworkhub_worker_source_graph_query",
            input: { mode: "focus", query: `fv-sg-${forceFinalTurns}` },
          };
        }()),
      };
    },
  };
  try {
    await internals.runVscodeLmAgent(
      violatorModel,
      {
        requestId: "j".repeat(32),
        request_kind: "worker",
        prompt: "bounded finalization violator",
        allowedWrites: [],
        path_contracts: {},
      },
      undefined,
      async (call) => {
        if (call.name.startsWith("aiworkhub_manager_")) invokeCountByRole.manager += 1;
        if (call.name.startsWith("aiworkhub_worker_")) invokeCountByRole.worker += 1;
        return { ok: true, content: "graph" };
      },
    );
    assert.fail("expected vscode_lm_finalization_tool_violation");
  } catch (err) {
    assert.match(String(err.message || err), /vscode_lm_finalization_tool_violation/,
      "second force-final violation must throw");
    assert.ok(forceFinalTurns >= 2, "must have had at least one turn before corrective retry");
    assert.strictEqual(invokeCountByRole.manager, 0, "invokeCountByRole.manager must be 0 for worker requests");
    // NF-2026-00168: protocolTrace must capture exact rejectedCallIds.
    const traceEntries = err.protocolTrace || [];
    const rejectedEntries = traceEntries.filter((e) => e.outcome === "tool_call_rejected");
    assert.ok(rejectedEntries.length > 0,
      `must have at least one tool_call_rejected trace entry, got ${JSON.stringify(traceEntries)}`);
    const expectedRejectedIds = rejectedEntries.flatMap((e) => e.rejectedCallIds || []);
    assert.ok(expectedRejectedIds.length > 0,
      "rejectedCallIds must be nonempty");
    assert.deepStrictEqual(expectedRejectedIds, ["fv-14", "fv-15"],
      `exact rejectedCallIds must be ["fv-14","fv-15"], got ${JSON.stringify(expectedRejectedIds)}`);
    // NF-2026-00168: verify corrective retry turn history is provider-valid.
    assert.ok(correctiveTurnMessages !== null,
      "correctiveTurnMessages must be non-null");
    // Directly validate raw concrete result parts with exact callId+content
    // and both directions of adjacent assistant-call/user-result ID-set equality.
    for (let mi = 0; mi < correctiveTurnMessages.length; mi += 1) {
      const msg = correctiveTurnMessages[mi];
      if (msg.role === "assistant" && Array.isArray(msg.content)) {
        const tcParts = msg.content.filter(
          (p) => p && typeof p.callId === "string" && typeof p.name === "string",
        );
        if (tcParts.length > 0) {
          const next = correctiveTurnMessages[mi + 1];
          assert.ok(next && next.role === "user" && Array.isArray(next.content),
            `assistant[${mi}] tool-calls must be followed by user with result parts`);
          const tcIds = new Set(tcParts.map((tc) => tc.callId));
          const rpParts = next.content.filter(
            (p) => p && typeof p.callId === "string" && p.content !== undefined,
          );
          const rpIds = new Set(rpParts.map((rp) => rp.callId));
          assert.deepStrictEqual([...tcIds].sort(), [...rpIds].sort(),
            `assistant[${mi}]↔user[${mi + 1}] callId sets must match exactly`);
          for (const rp of rpParts) {
            assert.ok(rp.content !== undefined && rp.content !== null,
              `user[${mi + 1}] callId=${rp.callId} result content must be concrete`);
          }
        }
      }
    }
    const historyErrors = internals.validateProviderHistory(correctiveTurnMessages);
    assert.deepStrictEqual(historyErrors, [],
      `corrective retry history invalid: ${historyErrors.join("; ")}`);
  }
}

// NF-2026-00168: force-final text protocol two-strikes regression test.
// Verify text protocol force-final violation follows bounded two-strikes:
// first violation gets corrective retry, second throws structurally.
async function nf168ForceFinalTextProtocol() {
  let textTurns = 0;
  const invokeCountByRole = { manager: 0, worker: 0 };
  let correctiveTurnMessages = null;
  const finalResponse = JSON.stringify({
    schema_id: internals.constants.VSCODE_LM_EDIT_RESPONSE_SCHEMA,
    summary: "should not succeed",
    edits: [],
    creates: [],
  });
  // Model always returns tool requests (never finalizes) to trigger force-final.
  const toolName = "aiworkhub_worker_source_graph_query";
  const toolEnvelope = JSON.stringify({
    schema_id: internals.constants.VSCODE_LM_TOOL_REQUEST_SCHEMA,
    name: toolName,
    input: { mode: "focus", query: "fv-text", budget: 48 },
  });
  const model = {
    capabilities: { toolCalling: false },
    sendRequest: async (_messages) => {
      textTurns += 1;
      if (textTurns > 1) {
        correctiveTurnMessages = _messages.map((msg) => ({
          role: msg.role,
          content: msg.content,
        }));
      }
      return {
        stream: (async function* stream() {
          yield { value: toolEnvelope };
        }()),
      };
    },
  };
  try {
    await internals.runVscodeLmTextProtocol(
      model,
      { prompt: "force final text violator", request_kind: "worker", allowedWrites: [] },
      undefined,
      async (call) => {
        if (call.name.startsWith("aiworkhub_manager_")) invokeCountByRole.manager += 1;
        if (call.name.startsWith("aiworkhub_worker_")) invokeCountByRole.worker += 1;
        return { ok: true, content: "graph" };
      },
    );
    assert.fail("text protocol force-final second violation must throw");
  } catch (err) {
    const msg = String(err.message || err);
    assert.match(msg, /vscode_lm_finalization_tool_violation/,
      "second force-final violation must throw in text protocol");
    assert.ok(textTurns >= 2, `must have at least 2 turns (had ${textTurns})`);
    assert.strictEqual(invokeCountByRole.manager, 0, "invokeCountByRole.manager must be 0 for worker text protocol");
    // NF-2026-00168: protocolTrace must capture exact rejectedTool name.
    const traceEntries = err.protocolTrace || [];
    const rejectedEntries = traceEntries.filter((e) => e.outcome === "tool_request_rejected");
    assert.ok(rejectedEntries.length > 0,
      `must have at least one tool_request_rejected trace entry, got ${JSON.stringify(traceEntries)}`);
    const expectedRejectedTools = rejectedEntries.map((e) => e.rejectedTool).filter(Boolean);
    assert.ok(expectedRejectedTools.length > 0,
      "rejectedTool must be nonempty");
    assert.deepStrictEqual(expectedRejectedTools,
      ["aiworkhub_worker_source_graph_query", "aiworkhub_worker_source_graph_query"],
      `exact rejectedTools must be ["aiworkhub_worker_source_graph_query","aiworkhub_worker_source_graph_query"], got ${JSON.stringify(expectedRejectedTools)}`);
    // NF-2026-00168: verify corrective retry turn history is provider-valid.
    assert.ok(correctiveTurnMessages !== null,
      "correctiveTurnMessages must be non-null");
    // Directly validate raw concrete result parts with exact callId+content
    // and both directions of adjacent assistant-call/user-result ID-set equality.
    for (let mi = 0; mi < correctiveTurnMessages.length; mi += 1) {
      const msg = correctiveTurnMessages[mi];
      if (msg.role === "assistant" && Array.isArray(msg.content)) {
        const tcParts = msg.content.filter(
          (p) => p && typeof p.callId === "string" && typeof p.name === "string",
        );
        if (tcParts.length > 0) {
          const next = correctiveTurnMessages[mi + 1];
          assert.ok(next && next.role === "user" && Array.isArray(next.content),
            `assistant[${mi}] tool-calls must be followed by user with result parts`);
          const tcIds = new Set(tcParts.map((tc) => tc.callId));
          const rpParts = next.content.filter(
            (p) => p && typeof p.callId === "string" && p.content !== undefined,
          );
          const rpIds = new Set(rpParts.map((rp) => rp.callId));
          assert.deepStrictEqual([...tcIds].sort(), [...rpIds].sort(),
            `assistant[${mi}]↔user[${mi + 1}] callId sets must match exactly`);
          for (const rp of rpParts) {
            assert.ok(rp.content !== undefined && rp.content !== null,
              `user[${mi + 1}] callId=${rp.callId} result content must be concrete`);
          }
        }
      }
    }
    const historyErrors = internals.validateProviderHistory(correctiveTurnMessages);
    assert.deepStrictEqual(historyErrors, [],
      `corrective retry history invalid: ${historyErrors.join("; ")}`);
  }
}

// NF-2026-00169: authority rejection — worker must not acknowledge manager SG,
// manager must not acknowledge worker SG, in both native and text protocol paths.
async function nf169AuthorityRejection() {
  const finalResponse = JSON.stringify({
    schema_id: internals.constants.VSCODE_LM_EDIT_RESPONSE_SCHEMA,
    summary: "should not reach",
    edits: [],
    creates: [],
  });
  // Native: worker request calling manager SG must fail with zero onToolTurn/invoke.
  // NF-2026-00169: use invocation counters, assert exactly zero.
  let nativeTurn = 0;
  let nativeOnToolTurns = 0;
  const nativeInvokeCalls = [];
  const nativeWrongRoleModel = {
    capabilities: { toolCalling: true },
    sendRequest: async (_messages, options) => {
      nativeTurn += 1;
      if (!Object.prototype.hasOwnProperty.call(options, "tools")) {
        return { stream: (async function* stream() { yield { value: finalResponse }; }()) };
      }
      return {
        stream: (async function* stream() {
          yield {
            callId: `wrong-${nativeTurn}`,
            name: "aiworkhub_manager_source_graph_query",
            input: { mode: "focus", query: `wrong-role-${nativeTurn}` },
          };
        }()),
      };
    },
  };
  try {
    await internals.runVscodeLmAgent(
      nativeWrongRoleModel,
      {
        requestId: "k".repeat(32),
        request_kind: "worker",
        prompt: "worker with manager SG",
        allowedWrites: [],
        path_contracts: {},
      },
      undefined,
      async (call) => { nativeInvokeCalls.push(call); return { ok: true, content: "graph" }; },
      (name, _state) => { nativeOnToolTurns += 1; },
    );
    assert.fail("worker must not acknowledge manager SG in native path");
  } catch (err) {
    // NF-2026-00169: authority gate rejects wrong-role calls before invocation.
    // Zero onToolTurn/invoke calls for the worker→manager SG path.
    assert.strictEqual(nativeOnToolTurns, 0,
      "worker→manager SG must produce zero onToolTurn calls");
    assert.strictEqual(nativeInvokeCalls.length, 0,
      "worker→manager SG must produce zero invokeTool calls");
    const msg = String(err.message || err);
    assert.ok(
      /vscode_lm_agent_turn_limit/.test(msg) || /source_graph_not_acknowledged/.test(msg) || /authority_gate/.test(msg),
      `worker native must reject manager SG, got: ${msg}`,
    );
  }
  // Text protocol: worker request with manager SG must also fail.
  const managerSgRequest = JSON.stringify({
    schema_id: internals.constants.VSCODE_LM_TOOL_REQUEST_SCHEMA,
    name: "aiworkhub_manager_source_graph_query",
    input: { mode: "focus", query: "manager-in-worker", budget: 48 },
  });
  const textQueued = [managerSgRequest, finalResponse];
  const wrongRoleTextInvokeCalls = [];
  const wrongRoleTextModel = {
    capabilities: { toolCalling: false },
    sendRequest: async () => ({
      stream: (async function* stream() { yield { value: textQueued.shift() }; }()),
    }),
  };
  try {
    await internals.runVscodeLmTextProtocol(
      wrongRoleTextModel,
      { prompt: "worker text wrong role", request_kind: "worker", allowedWrites: [] },
      undefined,
      async (call) => { wrongRoleTextInvokeCalls.push(call); return { ok: true, content: "graph" }; },
    );
    assert.fail("worker text must not acknowledge manager SG");
  } catch (err) {
    // NF-2026-00169: text protocol rejects wrong-role tools by name (not_allowed)
    // before invocation. Zero invokeTool calls for worker→manager SG in text path.
    assert.strictEqual(wrongRoleTextInvokeCalls.length, 0,
      "worker→manager SG text path must produce zero invokeTool calls");
    const msg = String(err.message || err);
    assert.ok(
      /source_graph_not_acknowledged/.test(msg) || /vscode_lm_tool_not_allowed/.test(msg),
      `worker text protocol must reject manager SG, got: ${msg}`,
    );
  }

  // Native: manager request calling worker SG must fail with zero onToolTurn/invoke.
  // NF-2026-00169: use invocation counters, assert exactly zero.
  let mgrNativeTurn = 0;
  let mgrOnToolTurns = 0;
  const mgrInvokeCalls = [];
  const mgrWrongRoleModel = {
    capabilities: { toolCalling: true },
    sendRequest: async (_messages, options) => {
      mgrNativeTurn += 1;
      if (!Object.prototype.hasOwnProperty.call(options, "tools")) {
        return { stream: (async function* stream() { yield { value: finalResponse }; }()) };
      }
      return {
        stream: (async function* stream() {
          yield {
            callId: `mgr-wrong-${mgrNativeTurn}`,
            name: "aiworkhub_worker_source_graph_query",
            input: { mode: "focus", query: `worker-in-manager-${mgrNativeTurn}` },
          };
        }()),
      };
    },
  };
  try {
    await internals.runVscodeLmAgent(
      mgrWrongRoleModel,
      {
        requestId: "l".repeat(32),
        request_kind: "manager",
        prompt: "manager with worker SG",
        allowedWrites: [],
        path_contracts: {},
      },
      undefined,
      async (call) => { mgrInvokeCalls.push(call); return { ok: true, content: "graph" }; },
      (name, _state) => { mgrOnToolTurns += 1; },
    );
    assert.fail("manager must not acknowledge worker SG in native path");
  } catch (err) {
    // NF-2026-00169: authority gate rejects wrong-role calls before invocation.
    // Zero onToolTurn/invoke calls for the manager→worker SG path.
    assert.strictEqual(mgrOnToolTurns, 0,
      "manager→worker SG must produce zero onToolTurn calls");
    assert.strictEqual(mgrInvokeCalls.length, 0,
      "manager→worker SG must produce zero invokeTool calls");
    const msg = String(err.message || err);
    assert.ok(
      /vscode_lm_agent_turn_limit/.test(msg) || /source_graph_not_acknowledged/.test(msg) || /authority_gate/.test(msg),
      `manager native must reject worker SG, got: ${msg}`,
    );
  }
}


async function nf202600229QualityReviewSubmitBoundaryChecks() {
  const reviewRequest = (requestId) => ({
    requestId,
    request_kind: "quality_review",
    prompt: "bounded review",
    allowedWrites: [],
    path_contracts: {},
  });
  const sgRequestText = (query) => JSON.stringify({
    schema_id: internals.constants.VSCODE_LM_TOOL_REQUEST_SCHEMA,
    name: "aiworkhub_worker_source_graph_query",
    input: { mode: "focus", query, workflow_stage: "review" },
  });
  const submitRequestText = () => JSON.stringify({
    schema_id: internals.constants.VSCODE_LM_TOOL_REQUEST_SCHEMA,
    name: "aiworkhub_worker_quality_review_submit",
    input: { packet_sha256: "e".repeat(64), lens: "correctness", findings: [] },
  });
  const sealedReview = (submissionId) => ({
    schema_id: internals.constants.VSCODE_LM_EDIT_RESPONSE_SCHEMA,
    summary: `quality review submitted:${submissionId}`,
    edits: [],
    creates: [],
  });
  const sgInput = (query) => ({ mode: "focus", query, workflow_stage: "review" });
  const submitInput = () => ({ packet_sha256: "e".repeat(64), lens: "correctness", findings: [] });

  // NF-2026-00229 (GLM text path): after three Source Graph work turns, one
  // more Source Graph request receives a single corrective, non-executing
  // result; the next authenticated submit succeeds exactly once.
  const textCalls = [];
  let textTurn = 0;
  const textModel = {
    capabilities: { toolCalling: false },
    sendRequest: async () => {
      textTurn += 1;
      const value = textTurn <= 4 ? sgRequestText(`review-${textTurn}`) : submitRequestText();
      return { stream: (async function* stream() { yield { value }; }()) };
    },
  };
  const textResult = await internals.runVscodeLmTextProtocol(
    textModel,
    reviewRequest("f".repeat(32)),
    undefined,
    async (call) => {
      textCalls.push(call.name);
      return call.name === "aiworkhub_worker_quality_review_submit"
        ? { ok: true, durable: true, submission_id: "a".repeat(64) }
        : { ok: true, content: "graph" };
    },
  );
  assert.deepStrictEqual(JSON.parse(textResult), sealedReview("a".repeat(64)));
  assert.deepStrictEqual(textCalls, [
    "aiworkhub_worker_source_graph_query",
    "aiworkhub_worker_source_graph_query",
    "aiworkhub_worker_source_graph_query",
    "aiworkhub_worker_quality_review_submit",
  ]);
  assert.strictEqual(textTurn, 5);

  // NF-2026-00229 (GLM text path): a repeated non-submit violation terminalizes
  // with bounded vscode_lm_quality_review_submit_required diagnostics.
  const repeatCalls = [];
  let repeatTurn = 0;
  const repeatModel = {
    capabilities: { toolCalling: false },
    sendRequest: async () => {
      repeatTurn += 1;
      const value = sgRequestText(`repeat-${repeatTurn}`);
      return { stream: (async function* stream() { yield { value }; }()) };
    },
  };
  await assert.rejects(
    internals.runVscodeLmTextProtocol(
      repeatModel,
      reviewRequest("g".repeat(32)),
      undefined,
      async (call) => { repeatCalls.push(call.name); return { ok: true, content: "graph" }; },
    ),
    /vscode_lm_quality_review_submit_required/,
  );
  assert.strictEqual(repeatTurn, 5);
  assert.deepStrictEqual(repeatCalls, [
    "aiworkhub_worker_source_graph_query",
    "aiworkhub_worker_source_graph_query",
    "aiworkhub_worker_source_graph_query",
  ]);

  // NF-2026-00229 (GLM native path): one corrective, non-executing tool turn
  // (paired by callId) followed by a sealed submit.
  const nativeCalls = [];
  let nativeTurn = 0;
  const nativeModel = {
    capabilities: { toolCalling: true },
    sendRequest: async () => {
      nativeTurn += 1;
      const part = nativeTurn <= 4
        ? { callId: `sg-${nativeTurn}`, name: "aiworkhub_worker_source_graph_query", input: sgInput(`review-${nativeTurn}`) }
        : { callId: "submit-final", name: "aiworkhub_worker_quality_review_submit", input: submitInput() };
      return { stream: (async function* stream() { yield part; }()) };
    },
  };
  const nativeResult = await internals.runVscodeLmAgent(
    nativeModel,
    reviewRequest("a".repeat(32)),
    undefined,
    async (call) => {
      nativeCalls.push(call.name);
      return call.name === "aiworkhub_worker_quality_review_submit"
        ? { ok: true, durable: true, submission_id: "b".repeat(64) }
        : { ok: true, content: "graph" };
    },
  );
  assert.deepStrictEqual(JSON.parse(nativeResult), sealedReview("b".repeat(64)));
  assert.deepStrictEqual(nativeCalls, [
    "aiworkhub_worker_source_graph_query",
    "aiworkhub_worker_source_graph_query",
    "aiworkhub_worker_source_graph_query",
    "aiworkhub_worker_quality_review_submit",
  ]);
  assert.strictEqual(nativeTurn, 5);

  // NF-2026-00229 (GLM native path): a repeated non-submit violation fails
  // truthfully with no infinite loop.
  const nativeRepeatCalls = [];
  let nativeRepeatTurn = 0;
  const nativeRepeatModel = {
    capabilities: { toolCalling: true },
    sendRequest: async () => {
      nativeRepeatTurn += 1;
      return {
        stream: (async function* stream() {
          yield { callId: `sg-${nativeRepeatTurn}`, name: "aiworkhub_worker_source_graph_query", input: sgInput(`review-${nativeRepeatTurn}`) };
        }()),
      };
    },
  };
  await assert.rejects(
    internals.runVscodeLmAgent(
      nativeRepeatModel,
      reviewRequest("b".repeat(32)),
      undefined,
      async (call) => { nativeRepeatCalls.push(call.name); return { ok: true, content: "graph" }; },
    ),
    /vscode_lm_quality_review_submit_required/,
  );
  assert.strictEqual(nativeRepeatTurn, 5);
  assert.deepStrictEqual(nativeRepeatCalls, [
    "aiworkhub_worker_source_graph_query",
    "aiworkhub_worker_source_graph_query",
    "aiworkhub_worker_source_graph_query",
  ]);

  // NF-2026-00229 (DeepSeek native path): the same phase truth holds on the
  // DeepSeek-compatible protocol route.
  const dsCalls = [];
  let dsTurn = 0;
  const dsModel = {
    ...deepseek,
    sendRequest: async () => {
      dsTurn += 1;
      const part = dsTurn <= 4
        ? { callId: `sg-${dsTurn}`, name: "aiworkhub_worker_source_graph_query", input: sgInput(`review-${dsTurn}`) }
        : { callId: "submit-final", name: "aiworkhub_worker_quality_review_submit", input: submitInput() };
      return { stream: (async function* stream() { yield part; }()) };
    },
  };
  const dsResult = await internals.runVscodeLmAgent(
    dsModel,
    reviewRequest("d".repeat(32)),
    undefined,
    async (call) => {
      dsCalls.push(call.name);
      return call.name === "aiworkhub_worker_quality_review_submit"
        ? { ok: true, durable: true, submission_id: "c".repeat(64) }
        : { ok: true, content: "graph" };
    },
  );
  assert.deepStrictEqual(JSON.parse(dsResult), sealedReview("c".repeat(64)));
  assert.deepStrictEqual(dsCalls, [
    "aiworkhub_worker_source_graph_query",
    "aiworkhub_worker_source_graph_query",
    "aiworkhub_worker_source_graph_query",
    "aiworkhub_worker_quality_review_submit",
  ]);
  assert.strictEqual(dsTurn, 5);

  // NF-2026-00229: a Source Graph mcp_unavailable result is recoverable tool
  // evidence, never terminalized and never turned into a candidate finding —
  // all three work turns fail with mcp_unavailable yet the review still seals.
  const mcpCalls = [];
  let mcpTurn = 0;
  const mcpModel = {
    capabilities: { toolCalling: true },
    sendRequest: async () => {
      mcpTurn += 1;
      const part = mcpTurn <= 3
        ? { callId: `sg-${mcpTurn}`, name: "aiworkhub_worker_source_graph_query", input: sgInput(`review-${mcpTurn}`) }
        : { callId: "submit-final", name: "aiworkhub_worker_quality_review_submit", input: submitInput() };
      return { stream: (async function* stream() { yield part; }()) };
    },
  };
  const mcpResult = await internals.runVscodeLmAgent(
    mcpModel,
    reviewRequest("e".repeat(32)),
    undefined,
    async (call) => {
      mcpCalls.push(call.name);
      if (call.name === "aiworkhub_worker_quality_review_submit") {
        return { ok: true, durable: true, submission_id: "d".repeat(64) };
      }
      throw new Error("mcp_unavailable");
    },
  );
  assert.deepStrictEqual(JSON.parse(mcpResult), sealedReview("d".repeat(64)));
  assert.deepStrictEqual(mcpCalls, [
    "aiworkhub_worker_source_graph_query",
    "aiworkhub_worker_source_graph_query",
    "aiworkhub_worker_source_graph_query",
    "aiworkhub_worker_quality_review_submit",
  ]);
}

async function nf179ForcedStageRecoveryChecks() {
  const request = {
    requestId: "7".repeat(32),
    request_kind: "worker",
    prompt: "bounded NF179 stage recovery",
    allowedWrites: ["out/result.json"],
    path_contracts: {
      "out/result.json": {
        action: "create",
        current_sha256: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        line_count: 0,
        parent_existed: false,
      },
    },
    initial_source_graph_result: { ok: true, content: "prefetched graph" },
  };
  const toolRequest = (name, input) => JSON.stringify({
    schema_id: internals.constants.VSCODE_LM_TOOL_REQUEST_SCHEMA,
    name,
    input,
  });
  const lastUserText = (messages) => {
    const last = messages[messages.length - 1];
    return last && last.role === "user" && typeof last.content === "string" ? last.content : "";
  };

  const textInvocations = [];
  const textModel = {
    capabilities: { toolCalling: false },
    sendRequest: async (messages) => {
      const instruction = lastUserText(messages);
      let value;
      if (instruction.includes("Only aiworkhub_manager_semantic_edit_stage")) {
        value = toolRequest("aiworkhub_manager_semantic_edit_stage", {
          operation: "create", file_path: "out/result.json", content: "{}\n",
        });
      } else if (instruction.includes("The bounded discovery phase is complete")) {
        value = toolRequest("aiworkhub_worker_session_current_state", {
          mode: "focus",
          query: "forced-stage-corrective",
          workflow_stage: "implementation",
        });
      } else {
        value = toolRequest("aiworkhub_worker_source_graph_query", {
          mode: "focus",
          query: "work",
          workflow_stage: "implementation",
        });
      }
      return { stream: (async function* stream() { yield { value }; }()) };
    },
  };
  const textResult = await internals.runVscodeLmTextProtocol(
    textModel,
    request,
    undefined,
    async (call) => {
      textInvocations.push(call);
      if (call.name === "aiworkhub_manager_semantic_edit_stage") throw new Error("mcp_unavailable");
      return { ok: true, content: "graph" };
    },
  );
  assert.strictEqual(JSON.parse(textResult).creates[0].path, "out/result.json");
  assert.ok(!textInvocations.some((call) => call.name === "aiworkhub_worker_session_current_state"));
  assert.ok(!textInvocations.some((call) => call.input && call.input.query === "forced-stage-corrective"));

  let repeatedTurns = 0;
  const repeatedModel = {
    capabilities: { toolCalling: false },
    sendRequest: async () => {
      repeatedTurns += 1;
      return {
        stream: (async function* stream() {
          yield { value: toolRequest("aiworkhub_worker_source_graph_query", {
            mode: "focus", query: `repeat-${repeatedTurns}`, workflow_stage: "implementation",
          }) };
        }()),
      };
    },
  };
  await assert.rejects(
    internals.runVscodeLmTextProtocol(
      repeatedModel, request, undefined, async () => ({ ok: true, content: "graph" }),
    ),
    /vscode_lm_semantic_edit_stage_required/,
  );

  const nativeWrongStageModel = {
    capabilities: { toolCalling: true },
    sendRequest: async (messages, options) => {
      const instruction = lastUserText(messages);
      if (!Object.prototype.hasOwnProperty.call(options, "tools")) {
        return { stream: (async function* stream() { yield { value: finalResponse }; }()) };
      }
      if (instruction.includes("bounded discovery phase")) {
        return { stream: (async function* stream() {
          yield { callId: "nf179-native-wrong", name: "aiworkhub_worker_session_current_state", input: {} };
        }()) };
      }
      if (instruction.includes("Only aiworkhub_manager_semantic_edit_stage")) {
        return { stream: (async function* stream() {
          yield { callId: "nf179-native-stage", name: "aiworkhub_manager_semantic_edit_stage", input: { operation: "create", file_path: "out/result.json", content: "{}\n" } };
        }()) };
      }
      return { stream: (async function* stream() {
        yield { callId: "nf179-native-source", name: "aiworkhub_worker_source_graph_query", input: { mode: "focus", query: "work", workflow_stage: "implementation" } };
      }()) };
    },
  };
  const nativeWrongCalls = [];
  const nativeWrongResult = await internals.runVscodeLmAgent(
    nativeWrongStageModel,
    request,
    undefined,
    async (call) => {
      nativeWrongCalls.push(call);
      if (call.name === "aiworkhub_manager_semantic_edit_stage") return { ok: true, content: "graph" };
      throw new Error("mcp_unavailable");
    },
  );
  assert.strictEqual(JSON.parse(nativeWrongResult).creates[0].path, "out/result.json");
  assert.ok(!nativeWrongCalls.some((call) => call.name === "aiworkhub_worker_session_current_state"));

  const nativeInvocations = [];
  const forcedToolSets = [];
  const nativeModel = {
    capabilities: { toolCalling: true },
    sendRequest: async (messages, options) => {
      const instruction = lastUserText(messages);
      if (instruction.includes("bounded discovery phase") && Array.isArray(options.tools)) {
        forcedToolSets.push(options.tools.map((tool) => tool.name));
        return { stream: (async function* stream() {
          yield {
            callId: "nf179-corrective",
            name: "aiworkhub_worker_source_graph_query",
            input: { mode: "focus", query: "forced-stage-corrective", workflow_stage: "implementation" },
          };
        }()) };
      }
      if (instruction.includes("Only aiworkhub_manager_semantic_edit_stage")) {
        return { stream: (async function* stream() {
          yield {
            callId: "nf179-stage",
            name: "aiworkhub_manager_semantic_edit_stage",
            input: { operation: "create", file_path: "out/result.json", content: "{}\n" },
          };
        }()) };
      }
      return { stream: (async function* stream() {
        yield {
          callId: `nf179-work-${nativeInvocations.length}`,
          name: "aiworkhub_worker_source_graph_query",
          input: { mode: "focus", query: "work", workflow_stage: "implementation" },
        };
      }()) };
    },
  };
  const nativeResult = await internals.runVscodeLmAgent(
    nativeModel,
    request,
    undefined,
    async (call) => {
      nativeInvocations.push(call);
      if (call.name === "aiworkhub_manager_semantic_edit_stage") throw new Error("mcp_unavailable");
      return { ok: true, content: "graph" };
    },
  );
  assert.strictEqual(JSON.parse(nativeResult).creates[0].path, "out/result.json");
  assert.ok(forcedToolSets.length > 0);
  assert.ok(forcedToolSets.every((names) =>
    names.length === 1 && names[0] === "aiworkhub_manager_semantic_edit_stage"));
  assert.ok(!nativeInvocations.some((call) => call.name === "aiworkhub_manager_semantic_edit_stage"));
  assert.ok(!nativeInvocations.some((call) => call.input && call.input.query === "forced-stage-corrective"));
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
  windowTargetClaimChecks();
  await progressReceiptChecks();
  await claimedCancellationChecks();
  await cancellationToolBoundaryChecks();
  await nf168ProviderHistoryValidation();
  await nf169WorkerSourceGraphAck();
  await nf383McpFirstCallReadinessChecks();
  await nf168ForceFinalCallIdPairing();
  await nf169AuthorityRejection();
  await nf168ValidateProviderHistoryUnit();
  await nf168ForceFinalTextProtocol();
  await nf169ContextlessWorkerNative();
  await nf202600229QualityReviewSubmitBoundaryChecks();
  await nf179ForcedStageRecoveryChecks();
}
async function cancellationToolBoundaryChecks() {
  const toolEnvelope = JSON.stringify({
    schema_id: internals.constants.VSCODE_LM_TOOL_REQUEST_SCHEMA,
    name: "aiworkhub_worker_session_current_state",
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
    const forgedBytes = fs.readFileSync(cancelled.responsePath);
    fs.writeFileSync(forgedReceipt, forgedBytes, { mode: 0o600 });
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

  // NF389: deterministic bounded provider-call id synthesis for the text and
  // native bridge paths. Same (request, turn, canonical messages) → same id;
  // different turn or input → different id; always bounded and printable.
  const canonicalA = internals.canonicalizeVscodeLmMessages([
    { role: "user", content: "hello" },
  ]);
  const canonicalB = internals.canonicalizeVscodeLmMessages([
    { role: "user", content: "world" },
  ]);
  assert.strictEqual(canonicalA, canonicalA);
  assert.notStrictEqual(canonicalA, canonicalB);
  const pci1 = internals.synthesizeVscodeLmProviderCallId("req-1", 0, canonicalA);
  const pci1Repeat = internals.synthesizeVscodeLmProviderCallId("req-1", 0, canonicalA);
  const pci2 = internals.synthesizeVscodeLmProviderCallId("req-1", 1, canonicalA);
  const pci3 = internals.synthesizeVscodeLmProviderCallId("req-2", 0, canonicalA);
  assert.strictEqual(pci1, pci1Repeat);
  assert.notStrictEqual(pci1, pci2);
  assert.notStrictEqual(pci1, pci3);
  for (const pci of [pci1, pci2, pci3]) {
    assert.match(pci, /^pci_[a-z0-9]+$/);
    assert.ok(pci.length <= 32, `provider_call_id must be bounded, got ${pci}`);
    // NF389 rework: the 28 base36 characters after the `pci_` prefix carry
    // ~144 bits of SHA-256-derived identity — at least the 128-bit floor.
    assert.strictEqual(pci.length, 32, `provider_call_id must carry ≥128 bits, got ${pci}`);
  }
  // NF389 sealed correction: model identity and canonical options/tool context
  // participate in the synthesized identity (aligned with the single-flight key).
  const canonicalOptsA = internals.canonicalizeVscodeLmOptions({ justification: "a" });
  const canonicalOptsB = internals.canonicalizeVscodeLmOptions({ justification: "b" });
  const pciModelA = internals.synthesizeVscodeLmProviderCallId("req-1", 0, canonicalA, "model-a", canonicalOptsA);
  const pciModelB = internals.synthesizeVscodeLmProviderCallId("req-1", 0, canonicalA, "model-b", canonicalOptsA);
  const pciOptsB = internals.synthesizeVscodeLmProviderCallId("req-1", 0, canonicalA, "model-a", canonicalOptsB);
  assert.notStrictEqual(pciModelA, pciModelB, "distinct model must yield a distinct id");
  assert.notStrictEqual(pciModelA, pciOptsB, "distinct canonical options must yield a distinct id");
  assert.strictEqual(
    pciModelA,
    internals.synthesizeVscodeLmProviderCallId("req-1", 0, canonicalA, "model-a", canonicalOptsA),
    "same model/options must remain deterministic",
  );

  // NF389: atomic single-flight dedup — concurrent identical calls execute
  // once, waiters replay, and state is removed on success and on failure.
  internals.clearVscodeLmInFlightCalls();
  let sendCount = 0;
  let pendingResolve = null;
  let pendingReject = null;
  const fakeModel = {
    id: "deepseek-v4-pro",
    sendRequest: () => {
      sendCount += 1;
      return new Promise((resolve, reject) => {
        pendingResolve = resolve;
        pendingReject = reject;
      });
    },
  };
  const messages = [{ role: "user", content: "dedupe me" }];
  const options = { justification: "test" };
  const first = internals.dedupeVscodeLmSendRequest(fakeModel, messages, options, undefined, "req-dedupe", 0);
  const second = internals.dedupeVscodeLmSendRequest(fakeModel, messages, options, undefined, "req-dedupe", 0);
  assert.strictEqual(first.promise, second.promise, "concurrent identical calls must share one in-flight promise");
  assert.strictEqual(sendCount, 1, "identical concurrent call must execute exactly once");
  assert.strictEqual(internals.vscodeLmInFlightCallsSize(), 1);
  // The single-flight entry preserves the deterministic bounded provider-call
  // id it was synthesized from (request, turn, canonical messages).
  const expectedPci = internals.synthesizeVscodeLmProviderCallId(
    "req-dedupe", 0, internals.canonicalizeVscodeLmMessages(messages),
    fakeModel.id, internals.canonicalizeVscodeLmOptions(options),
  );
  assert.strictEqual(first.providerCallId, expectedPci);
  assert.strictEqual(second.providerCallId, expectedPci);
  assert.match(first.providerCallId, /^pci_[a-z0-9]+$/);
  assert.ok(first.providerCallId.length <= 32);
  pendingResolve({ stream: [] });
  await first.promise;
  await second.promise;
  assert.strictEqual(sendCount, 1);
  assert.strictEqual(internals.vscodeLmInFlightCallsSize(), 0, "success must remove in-flight state");
  // A fresh identical call after settle re-executes (failure retry path) and
  // removes its own in-flight state on rejection.
  const retry = internals.dedupeVscodeLmSendRequest(fakeModel, messages, options, undefined, "req-dedupe", 0);
  assert.strictEqual(sendCount, 2, "failure must retry (re-execute after settle)");
  assert.strictEqual(internals.vscodeLmInFlightCallsSize(), 1);
  let retryRejected = false;
  pendingReject(new Error("boom")); // reject the retry promise so it settles
  await retry.promise.catch(() => { retryRejected = true; });
  assert.strictEqual(retryRejected, true);
  assert.strictEqual(internals.vscodeLmInFlightCallsSize(), 0, "failure must remove in-flight state");
  internals.clearVscodeLmInFlightCalls();

  // NF389 rework: cancellation releases the exact single-flight entry even when
  // the provider ignores cancellation and the promise stays pending; an
  // immediate identical retry must therefore execute a fresh request instead of
  // replaying the stale pending entry.
  internals.clearVscodeLmInFlightCalls();
  let cancelSendCount = 0;
  let cancelListeners = [];
  const cancelToken = {
    onCancellationRequested: (listener) => {
      cancelListeners.push(listener);
      return { dispose: () => { cancelListeners = cancelListeners.filter((l) => l !== listener); } };
    },
  };
  const ignoringModel = {
    id: "deepseek-v4-pro",
    sendRequest: () => {
      cancelSendCount += 1;
      return new Promise(() => {}); // never settles: provider ignores cancellation
    },
  };
  const pendingCall = internals.dedupeVscodeLmSendRequest(
    ignoringModel, messages, options, cancelToken, "req-cancel", 0,
  );
  assert.strictEqual(cancelSendCount, 1);
  assert.strictEqual(internals.vscodeLmInFlightCallsSize(), 1);
  assert.strictEqual(cancelListeners.length, 1);
  // Fire cancellation; the provider never settles, but the entry must release.
  cancelListeners[0]();
  assert.strictEqual(internals.vscodeLmInFlightCallsSize(), 0, "cancellation must release the pending entry");
  // Immediate identical retry must execute a fresh provider call.
  const retryAfterCancel = internals.dedupeVscodeLmSendRequest(
    ignoringModel, messages, options, cancelToken, "req-cancel", 0,
  );
  assert.strictEqual(cancelSendCount, 2, "identical retry after cancellation must re-execute");
  assert.notStrictEqual(retryAfterCancel.promise, pendingCall.promise);
  assert.strictEqual(internals.vscodeLmInFlightCallsSize(), 1);
  // Firing the stale first cancellation listener again must NOT delete the
  // newer replacement entry (exact-entry release).
  cancelListeners[0]();
  assert.strictEqual(internals.vscodeLmInFlightCallsSize(), 1, "stale cancellation must not drop the replacement");
  internals.clearVscodeLmInFlightCalls();

  // NF389 sealed correction #3: single-flight equivalence includes canonical
  // options/tool context and turn, so distinct calls never collapse.
  let distinctSendCount = 0;
  const distinctModel = {
    id: "deepseek-v4-pro",
    sendRequest: () => {
      distinctSendCount += 1;
      return Promise.resolve({ stream: [] });
    },
  };
  const optionsA = { justification: "test", tools: [{ name: "t1", inputSchema: {} }], toolMode: 1 };
  const optionsB = { justification: "test", tools: [{ name: "t2", inputSchema: {} }], toolMode: 1 };
  const d1 = internals.dedupeVscodeLmSendRequest(distinctModel, messages, optionsA, undefined, "req-distinct", 0);
  const d2 = internals.dedupeVscodeLmSendRequest(distinctModel, messages, optionsB, undefined, "req-distinct", 0);
  assert.notStrictEqual(d1.promise, d2.promise, "different tool context must not collapse");
  assert.strictEqual(distinctSendCount, 2);
  const d3 = internals.dedupeVscodeLmSendRequest(distinctModel, messages, optionsA, undefined, "req-distinct", 1);
  assert.notStrictEqual(d1.promise, d3.promise, "different turn must not collapse");
  assert.strictEqual(distinctSendCount, 3);
  await Promise.all([d1.promise, d2.promise, d3.promise]);
  assert.strictEqual(internals.vscodeLmInFlightCallsSize(), 0);
  internals.clearVscodeLmInFlightCalls();

  // NF389 sealed correction #2: the synthesized provider-call id is consumed
  // and forwarded through the native/text worker invocation into the
  // authenticated audit identity (tool_input.provider_call_id), not left as an
  // unused Promise property.
  let forwardedToolInput = null;
  let forwardedToolName = null;
  internals.bindVscodeLmProviderBridgeForTest({
    mcpClient: {
      repositoryRoot: "/tmp/nf389-forward",
      callTool: async (name, args) => {
        forwardedToolName = name;
        forwardedToolInput = args.tool_input;
        return { ok: true };
      },
    },
    activeRepoIdentity: { root: "/tmp/nf389-forward" },
  });
  const privateResult = await internals.invokeVscodeLmPrivateTool(
    { name: "aiworkhub_worker_source_graph_query", input: { mode: "focus", query: "nf389" } },
    "f".repeat(32),
    expectedPci,
  );
  assert.deepStrictEqual(privateResult, { ok: true });
  assert.strictEqual(forwardedToolName, "aiworkhub_vscode_lm_worker_tool");
  assert.strictEqual(forwardedToolInput.provider_call_id, expectedPci);
  assert.strictEqual(forwardedToolInput.mode, "focus");
  // A call without a synthesized id (older transport) must not fabricate one.
  forwardedToolInput = null;
  await internals.invokeVscodeLmPrivateTool(
    { name: "aiworkhub_worker_source_graph_query", input: { mode: "focus", query: "nf389-legacy" } },
    "f".repeat(32),
  );
  assert.strictEqual(forwardedToolInput.provider_call_id, undefined);
  internals.resetVscodeLmWorkerSourceGraphReadinessForTest();
}

main().then(() => {
  console.log("GLM VS Code LM bridge: ok");
}).catch((err) => {
  console.error(err);
  process.exitCode = 1;
});
