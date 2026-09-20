"use strict";

const assert = require("assert");
const path = require("path");
const Module = require("module");

const extensionPath = path.resolve(__dirname, "..", "extension.js");
const fakeVscode = {
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

async function oversizedToolInputIsCorrectedWithoutInvocation() {
  const oversizedQuery = "x".repeat(17000);
  const toolName = "aiworkhub_worker_source_graph_query";
  const toolEnvelope = JSON.stringify({
    schema_id: internals.constants.VSCODE_LM_TOOL_REQUEST_SCHEMA,
    name: toolName,
    input: { mode: "focus", query: oversizedQuery },
  });
  const finalEnvelope = JSON.stringify({
    schema_id: internals.constants.VSCODE_LM_EDIT_RESPONSE_SCHEMA,
    summary: "No changes needed.",
    edits: [],
    creates: [],
  });
  let turns = 0;
  let correction = null;
  const invoked = [];
  const toolStates = [];
  const model = {
    capabilities: { toolCalling: false },
    sendRequest: async (messages) => {
      turns += 1;
      if (turns === 2) {
        correction = JSON.parse(messages[messages.length - 1].content);
        assert.ok(!JSON.stringify(messages).includes(oversizedQuery),
          "rejected input must not be retained in provider history");
      }
      return {
        stream: (async function* stream() {
          yield { value: turns === 1 ? toolEnvelope : turns === 2
            ? JSON.stringify({
              schema_id: internals.constants.VSCODE_LM_TOOL_REQUEST_SCHEMA,
              name: toolName,
              input: { mode: "focus", query: "bounded follow-up" },
            })
            : finalEnvelope };
        }()),
      };
    },
  };

  const result = await internals.runVscodeLmTextProtocol(
    model,
    {
      requestId: "0123456789abcdef0123456789abcdef",
      request_kind: "worker",
      prompt: "Inspect a bounded source slice.",
      allowedWrites: [],
      initial_source_graph_request: { mode: "focus", query: "bounded" },
      initial_source_graph_result: { ok: true, content: "bounded source graph receipt" },
    },
    undefined,
    async (call) => { invoked.push(call); return { ok: true, content: "bounded result" }; },
    (_name, event) => { toolStates.push(event.tool_state); },
  );

  assert.strictEqual(result, finalEnvelope);
  assert.strictEqual(turns, 3, "worker should get a corrective turn and continue");
  assert.deepStrictEqual(invoked.map((call) => call.input.query), ["bounded follow-up"],
    "only the smaller follow-up may reach the tool");
  assert.deepStrictEqual(toolStates, ["failed", "started", "completed"],
    "a rejected oversized call must not claim the tool started");
  assert.strictEqual(correction.name, toolName);
  assert.strictEqual(correction.result.ok, false);
  assert.strictEqual(correction.result.error, "vscode_lm_tool_input_too_large");
  assert.strictEqual(correction.result.max_bytes, 16384);
  assert.ok(correction.result.actual_bytes > correction.result.max_bytes);
  assert.ok(!JSON.stringify(correction).includes(oversizedQuery), "correction must not echo input");
}

async function oversizedReviewSubmitKeepsBoundedRetryRule() {
  const toolEnvelope = JSON.stringify({
    schema_id: internals.constants.VSCODE_LM_TOOL_REQUEST_SCHEMA,
    name: "aiworkhub_worker_quality_review_submit",
    input: { summary: "x".repeat(17000) },
  });
  let turns = 0;
  let invoked = 0;
  const model = {
    capabilities: { toolCalling: false },
    sendRequest: async () => {
      turns += 1;
      return { stream: (async function* stream() { yield { value: toolEnvelope }; }()) };
    },
  };
  await assert.rejects(
    internals.runVscodeLmTextProtocol(
      model,
      {
        requestId: "fedcba9876543210fedcba9876543210",
        request_kind: "quality_review",
        prompt: "Submit bounded review evidence.",
        allowedWrites: [],
      },
      undefined,
      async () => { invoked += 1; return { ok: true }; },
    ),
    /vscode_lm_quality_review_submit_required/,
  );
  assert.strictEqual(turns, 2, "repeated invalid submits must stop at the existing two-strike gate");
  assert.strictEqual(invoked, 0, "oversized review submissions must never be invoked");
}

async function oversizedForcedStageKeepsMissingOutputInstruction() {
  const sourceGraphEnvelope = JSON.stringify({
    schema_id: internals.constants.VSCODE_LM_TOOL_REQUEST_SCHEMA,
    name: "aiworkhub_worker_source_graph_query",
    input: { mode: "focus", query: "bounded" },
  });
  const oversizedStageEnvelope = JSON.stringify({
    schema_id: internals.constants.VSCODE_LM_TOOL_REQUEST_SCHEMA,
    name: "aiworkhub_manager_semantic_edit_stage",
    input: { operation: "v3_create", path: "out/needed.js", content: "x".repeat(17000) },
  });
  let turns = 0;
  let invoked = 0;
  const model = {
    capabilities: { toolCalling: false },
    sendRequest: async (messages) => {
      turns += 1;
      if (turns === 14) {
        const correction = JSON.parse(messages[messages.length - 1].content);
        assert.strictEqual(correction.result.error, "vscode_lm_tool_input_too_large");
        assert.match(correction.instruction, /out\/needed\.js/);
        assert.match(correction.instruction, /create/);
      }
      return {
        stream: (async function* stream() {
          yield { value: turns <= 12 ? sourceGraphEnvelope : oversizedStageEnvelope };
        }()),
      };
    },
  };
  await assert.rejects(
    internals.runVscodeLmTextProtocol(
      model,
      {
        requestId: "abcdef0123456789abcdef0123456789",
        request_kind: "worker",
        prompt: "Stage the required output.",
        allowedWrites: ["out/needed.js"],
        path_contracts: { "out/needed.js": { action: "create", current_sha256: "", line_count: 0, parent_existed: false } },
        required_outputs: ["out/needed.js"],
        initial_source_graph_request: { mode: "focus", query: "initial" },
        initial_source_graph_result: { ok: true, content: "initial source graph receipt" },
      },
      undefined,
      async () => { invoked += 1; return { ok: true, content: "bounded result" }; },
    ),
    /vscode_lm_semantic_edit_stage_required/,
  );
  assert.strictEqual(turns, 14, "forced stage should stop after one corrective retry");
  assert.strictEqual(invoked, 12, "oversized stage calls must not be invoked");
}

Promise.resolve()
  .then(oversizedToolInputIsCorrectedWithoutInvocation)
  .then(oversizedReviewSubmitKeepsBoundedRetryRule)
  .then(oversizedForcedStageKeepsMissingOutputInstruction)
  .catch((error) => {
    console.error(error);
    process.exitCode = 1;
  });
