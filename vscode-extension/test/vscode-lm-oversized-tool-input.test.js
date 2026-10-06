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
  // NF-2026-01179: each discovery turn must carry a distinct query. This scenario
  // exists to prove the missing-required-output instruction survives an oversized
  // stage payload, and it asserts below that all twelve discovery calls execute;
  // twelve *identical* queries would instead be stopped by the unchanged
  // repeated-discovery guard, which glm-vscode-lm-bridge.test.js already covers.
  const sourceGraphEnvelope = (turn) => JSON.stringify({
    schema_id: internals.constants.VSCODE_LM_TOOL_REQUEST_SCHEMA,
    name: "aiworkhub_worker_source_graph_query",
    input: { mode: "focus", query: `bounded-${turn}` },
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
          yield { value: turns <= 12 ? sourceGraphEnvelope(turns) : oversizedStageEnvelope };
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

// NF-2026-01179: the oversized-stage strike is keyed to the output that is still
// owed, not to the run. This run stages alpha correctly and then oversizes bravo
// exactly ONCE -- it has repeated nothing, and bravo has never been corrected
// before -- so it must get the same first corrective turn bravo would have got as
// the only required output, and the run must still be able to finish. A run-global
// counter instead ended this shape as vscode_lm_semantic_edit_stage_required on
// bravo's very first oversized payload, blaming a loop that never happened. The
// scenario above still proves the SAME output oversized twice stays terminal.
async function oversizedStrikeIsKeyedToTheMissingOutput() {
  const alpha = "out/alpha.js";
  const bravo = "out/bravo.js";
  const stageEnvelope = (input) => JSON.stringify({
    schema_id: internals.constants.VSCODE_LM_TOOL_REQUEST_SCHEMA,
    name: "aiworkhub_manager_semantic_edit_stage",
    input,
  });
  const sourceGraphEnvelope = JSON.stringify({
    schema_id: internals.constants.VSCODE_LM_TOOL_REQUEST_SCHEMA,
    name: "aiworkhub_worker_source_graph_query",
    input: { mode: "focus", query: "bounded orientation" },
  });
  const boundedAlpha = stageEnvelope({ operation: "create", file_path: alpha, content: "const alpha = 1;\n" });
  // Authorized create stages have a 255 KiB bound; this fixture must exceed it.
  const oversizedBravo = stageEnvelope({ operation: "create", file_path: bravo, content: "x".repeat(255 * 1024 + 1) });
  const boundedBravo = stageEnvelope({ operation: "create", file_path: bravo, content: "const bravo = 2;\n" });
  let turns = 0;
  let invoked = 0;
  let correction = null;
  const model = {
    capabilities: { toolCalling: false },
    sendRequest: async (messages) => {
      turns += 1;
      if (turns === 4) correction = JSON.parse(messages[messages.length - 1].content);
      return {
        stream: (async function* stream() {
          yield {
            value: turns === 1 ? sourceGraphEnvelope
              : turns === 2 ? boundedAlpha
              : turns === 3 ? oversizedBravo
              : boundedBravo,
          };
        }()),
      };
    },
  };

  const result = await internals.runVscodeLmTextProtocol(
    model,
    {
      requestId: "9876543210fedcba9876543210fedcba",
      request_kind: "worker",
      prompt: "Create both required outputs.",
      allowedWrites: [alpha, bravo],
      path_contracts: {
        [alpha]: { action: "create", current_sha256: "", line_count: 0, parent_existed: false },
        [bravo]: { action: "create", current_sha256: "", line_count: 0, parent_existed: false },
      },
      required_outputs: [alpha, bravo],
      initial_source_graph_request: { mode: "focus", query: "initial" },
      initial_source_graph_result: { ok: true, content: "initial source graph receipt" },
    },
    undefined,
    async () => { invoked += 1; return { ok: true, content: "bounded result" }; },
  );

  const envelope = JSON.parse(result);
  assert.strictEqual(envelope.schema_id, internals.constants.VSCODE_LM_EDIT_RESPONSE_SCHEMA);
  assert.deepStrictEqual(envelope.creates.map((create) => create.path), [alpha, bravo],
    "both required outputs must still reach the final envelope");
  // NF-2026-01378: turn 5 replays the bravo stage; that stall finalizes offline.
  assert.strictEqual(turns, 5,
    "one oversized payload for a never-corrected output must not terminate the run");
  assert.strictEqual(correction.result.error, "vscode_lm_tool_input_too_large",
    "bravo's first oversized payload gets the normal corrective re-prompt");
  assert.match(correction.instruction, /out\/bravo\.js/);
  assert.strictEqual(invoked, 1, "only the bounded discovery call may reach the tool");
}

Promise.resolve()
  .then(oversizedToolInputIsCorrectedWithoutInvocation)
  .then(oversizedReviewSubmitKeepsBoundedRetryRule)
  .then(oversizedForcedStageKeepsMissingOutputInstruction)
  .then(oversizedStrikeIsKeyedToTheMissingOutput)
  .catch((error) => {
    console.error(error);
    process.exitCode = 1;
  });
