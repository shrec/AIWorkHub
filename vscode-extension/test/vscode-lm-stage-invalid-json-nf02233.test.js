"use strict";
// NF-2026-02233: an invalid-JSON reply during forced semantic-edit staging gets a
// corrective turn restating the stage envelope and quoting at most 300 chars of the
// rejected reply. Corrections share the forced-stage budget (vscodeLmNoteForcedStageFailure):
// the third consecutive violation for the same missing output is terminal with
// vscode_lm_semantic_edit_stage_required.
const assert = require("assert");
const path = require("path");
const Module = require("module");

function deepStub() {
  const target = function stub() {};
  return new Proxy(target, {
    get(_t, key) {
      if (typeof key === "symbol" || key === "then") return undefined;
      return deepStub();
    },
    apply() { return deepStub(); },
    construct() { return deepStub(); },
  });
}

class LanguageModelTextPart { constructor(value) { this.value = value; } }
class LanguageModelToolCallPart {
  constructor(callId, name, input) { this.callId = callId; this.name = name; this.input = input; }
}
class LanguageModelToolResultPart {
  constructor(callId, content) { this.callId = callId; this.content = content; }
}
const textContent = (content) => (typeof content === "string" ? [new LanguageModelTextPart(content)] : content);

const vscodeStub = new Proxy({
  LanguageModelTextPart,
  LanguageModelToolCallPart,
  LanguageModelToolResultPart,
  LanguageModelChatMessage: {
    User: (content) => ({ role: "user", content: textContent(content) }),
    Assistant: (content) => ({ role: "assistant", content: textContent(content) }),
  },
}, {
  get(target, key) {
    if (key in target) return target[key];
    if (typeof key === "symbol" || key === "then") return undefined;
    return deepStub();
  },
});

const originalLoad = Module._load;
Module._load = function load(request, ...rest) {
  if (request === "vscode") return vscodeStub;
  return originalLoad.call(this, request, ...rest);
};
const extension = require(path.join(__dirname, "..", "extension.js"));
Module._load = originalLoad;
const { runVscodeLmTextProtocol } = extension.__testInternals;

const CREATE_CONTRACT = {
  action: "create",
  current_sha256: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
  line_count: 0,
  parent_existed: false,
};
const PROSE = "I will stage the next file once I have finished reading.";
const stage = (filePath, content) => JSON.stringify({
  schema_id: "aiworkhub.vscode_lm.tool_request.v1",
  name: "aiworkhub_manager_semantic_edit_stage",
  input: { operation: "create", file_path: filePath, content },
});
const STAGE_A = stage("src/a.txt", "alpha\n");
const STAGE_B = stage("src/b.txt", "beta\n");

// Turn 0 stages src/a.txt (progress). Twelve identical re-stages are valid
// envelopes that make no progress, so forced staging starts at turn 13.
const STALLED_TO_FORCED = [STAGE_A, ...Array.from({ length: 12 }, () => STAGE_A)];

const token = { isCancellationRequested: false, onCancellationRequested: () => ({ dispose() {} }) };

function makeRequest() {
  return {
    requestId: "2".repeat(32),
    prompt: "stage two created files",
    allowedWrites: ["src/a.txt", "src/b.txt"],
    required_outputs: ["src/a.txt", "src/b.txt"],
    path_contracts: { "src/a.txt": CREATE_CONTRACT, "src/b.txt": CREATE_CONTRACT },
    initial_source_graph_request: { mode: "focus", query: "stage", budget: 48 },
    initial_source_graph_result: { ok: true, content: "prefetched graph" },
  };
}

async function runStaging(replies) {
  const state = { turns: 0 };
  const sentMessages = [];
  const model = {
    capabilities: { toolCalling: false },
    sendRequest: async (messages) => {
      sentMessages.push(JSON.stringify(messages[messages.length - 1]));
      const value = replies[Math.min(state.turns, replies.length - 1)];
      state.turns += 1;
      return { stream: (async function* stream() { yield new LanguageModelTextPart(value); })() };
    },
  };
  const invokeTool = async () => { throw new Error("staged_edits_must_not_invoke_mcp"); };
  let result = null;
  let error = null;
  try {
    result = await runVscodeLmTextProtocol(model, makeRequest(), token, invokeTool, () => {});
  } catch (err) {
    error = err;
  }
  return { result, error, turns: state.turns, sentMessages };
}

async function testOneInvalidReplyThenValidStageSucceeds() {
  const run = await runStaging([...STALLED_TO_FORCED, PROSE, STAGE_B]);
  assert.strictEqual(run.error, null, `a single invalid reply must not fail the run: ${run.error && run.error.message}`);
  const envelope = JSON.parse(run.result);
  assert.deepStrictEqual(
    envelope.creates.map((item) => item.path).sort(),
    ["src/a.txt", "src/b.txt"],
    "the valid stage call after the corrective turn completes both outputs",
  );
  assert.strictEqual(run.turns, 15, "turn 13 is the invalid reply, turn 14 the corrected stage");
  const correction = run.sentMessages[14];
  assert.ok(correction.includes(PROSE.slice(0, 30)), "the corrective turn quotes the rejected reply");
  assert.ok(correction.includes("src/b.txt"), "the corrective turn names the still-missing output");
  assert.ok(correction.includes("aiworkhub_manager_semantic_edit_stage"), "the corrective turn restates the stage envelope");
}

async function testCorrectionQuotesAtMost300Chars() {
  const tail = "TAILMARK";
  const long = `I will stage the next file ${"word ".repeat(120)}${tail}`;
  const run = await runStaging([...STALLED_TO_FORCED, long, STAGE_B]);
  assert.strictEqual(run.error, null, `a single long invalid reply must not fail the run: ${run.error && run.error.message}`);
  const correction = run.sentMessages[14];
  assert.ok(correction.includes("I will stage the next file"), "the corrective turn quotes the start of the rejected reply");
  assert.ok(!correction.includes(tail), "the quoted rejected reply is bounded to 300 characters");
}

async function testTwoConsecutiveInvalidRepliesAreCorrected() {
  const run = await runStaging([...STALLED_TO_FORCED, PROSE, PROSE, STAGE_B]);
  assert.strictEqual(run.error, null, `two consecutive invalid replies share one budget of two corrections: ${run.error && run.error.message}`);
  const envelope = JSON.parse(run.result);
  assert.deepStrictEqual(
    envelope.creates.map((item) => item.path).sort(),
    ["src/a.txt", "src/b.txt"],
    "the valid stage call after the second corrective turn completes both outputs",
  );
  assert.strictEqual(run.turns, 16, "turns 13 and 14 are corrected invalid replies, turn 15 the corrected stage");
  const secondCorrection = run.sentMessages[15];
  assert.ok(secondCorrection.includes(PROSE.slice(0, 30)), "the second corrective turn quotes the rejected reply");
  assert.ok(secondCorrection.includes("src/b.txt"), "the second corrective turn names the still-missing output");
}

async function testThirdConsecutiveInvalidReplyFailsWithStageIdentity() {
  const run = await runStaging([...STALLED_TO_FORCED, PROSE, PROSE, PROSE, STAGE_B]);
  assert.ok(run.error, "a third consecutive invalid reply exhausts the shared forced-stage budget");
  assert.match(String(run.error.message), /vscode_lm_semantic_edit_stage_required/);
  assert.doesNotMatch(String(run.error.message), /vscode_lm_finalization_limit|vscode_lm_agent_turn_limit/);
  assert.strictEqual(run.turns, 16, "the third consecutive invalid reply is terminal on turn 15");
  const outcomes = run.error.protocolTrace.map((entry) => entry.outcome);
  assert.strictEqual(outcomes.filter((outcome) => outcome === "invalid_json_corrected").length, 2,
    "exactly two invalid replies were corrected before the shared budget ran out");
  assert.deepStrictEqual(
    outcomes.slice(-3),
    ["vscode_lm_text_protocol_invalid_json", "invalid_json_corrected", "vscode_lm_text_protocol_invalid_json"],
  );
}

(async () => {
  await testOneInvalidReplyThenValidStageSucceeds();
  await testCorrectionQuotesAtMost300Chars();
  await testTwoConsecutiveInvalidRepliesAreCorrected();
  await testThirdConsecutiveInvalidReplyFailsWithStageIdentity();
  console.log("ok vscode-lm-stage-invalid-json-nf02233");
})().catch((err) => {
  console.error(err && err.stack || err);
  process.exit(1);
});
