"use strict";
// NF-2026-01201 (string tool-failure reasons keep their identity) and
// NF-2026-01200 (the invalid-JSON budget counts only CONSECUTIVE replies).
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
const { sanitizeErrorMessage, runVscodeLmTextProtocol } = extension.__testInternals;

function testSanitizeErrorMessage() {
  assert.strictEqual(sanitizeErrorMessage("tool_result_not_ok"), "tool_result_not_ok");
  assert.strictEqual(sanitizeErrorMessage("vscode_lm_tool_input_too_large"), "vscode_lm_tool_input_too_large");
  assert.strictEqual(sanitizeErrorMessage(new Error("mcp_request_timeout")), "mcp_request_timeout");
  assert.strictEqual(sanitizeErrorMessage(new Error("two words")), "two words");
  assert.strictEqual(
    sanitizeErrorMessage(sanitizeErrorMessage(new Error("mcp_request_timeout"))), "mcp_request_timeout",
  );
  for (const empty of [null, undefined, ""]) {
    assert.strictEqual(sanitizeErrorMessage(empty), "mcp_unavailable");
  }
  const unsafe = ["has space", "a/b", "C:\\Users\\someone\\secret.txt", 'say "hi"', "it's", "line1\nline2", "tab\there"];
  const literals = new Set();
  for (const input of unsafe) {
    const out = sanitizeErrorMessage(input);
    assert.notStrictEqual(out, input);
    assert.notStrictEqual(out, "mcp_unavailable");
    assert.ok(out.length > 0 && out.length <= 200);
    assert.ok(!/[\s/\\"']/.test(out), "fixed literal must itself be reason-code-shaped");
    assert.strictEqual(sanitizeErrorMessage(out), out);
    literals.add(out);
  }
  assert.strictEqual(literals.size, 1, "every unsafe string maps to one fixed literal");
  assert.strictEqual(sanitizeErrorMessage("a".repeat(300)).length, 200);
  assert.ok(sanitizeErrorMessage(new Error("b".repeat(300))).length <= 200);
}

function makeModel(responses) {
  const state = { turns: 0 };
  return {
    state,
    model: {
      capabilities: { toolCalling: false },
      sendRequest: async () => {
        const value = responses[Math.min(state.turns, responses.length - 1)];
        state.turns += 1;
        return { stream: (async function* stream() { yield new LanguageModelTextPart(value); })() };
      },
    },
  };
}

const token = { isCancellationRequested: false, onCancellationRequested: () => ({ dispose() {} }) };

function makeRequest() {
  return {
    requestId: "0".repeat(32),
    prompt: "do the task",
    allowedWrites: ["src/a.txt"],
    path_contracts: {},
    request_kind: "manager",
  };
}

const PROSE = "I will look at the repository first and then decide.";
const sgRequest = (query) => JSON.stringify({
  schema_id: "aiworkhub.vscode_lm.tool_request.v1",
  name: "aiworkhub_manager_source_graph_query",
  input: { mode: "focus", query, target: null, workflow_stage: "orientation" },
});

async function runCase(responses, invokeTool) {
  const { model, state } = makeModel(responses);
  const toolTurns = [];
  let error = null;
  try {
    await runVscodeLmTextProtocol(
      model, makeRequest(), token, invokeTool, (name, info) => toolTurns.push({ name, ...info }),
    );
  } catch (err) {
    error = err;
  }
  return { error, turns: state.turns, toolTurns };
}

async function testToolFailureCodes() {
  const thrown = await runCase(
    [sgRequest("one"), PROSE],
    async () => { throw new Error("mcp_request_timeout"); },
  );
  const thrownFailed = thrown.toolTurns.filter((turn) => turn.tool_state === "failed");
  assert.ok(thrownFailed.length >= 1, "a thrown invoker error reports a failed tool turn");
  assert.strictEqual(thrownFailed[0].error_code, "mcp_request_timeout");

  const returned = await runCase(
    [sgRequest("one"), PROSE],
    async () => ({ ok: false, error: "tool_result_not_ok" }),
  );
  const returnedFailed = returned.toolTurns.filter((turn) => turn.tool_state === "failed");
  assert.ok(returnedFailed.length >= 1, "an ok:false result reports a failed tool turn");
  assert.strictEqual(returnedFailed[0].error_code, "tool_result_not_ok");

  for (const turn of [...thrownFailed, ...returnedFailed]) {
    assert.notStrictEqual(turn.error_code, "mcp_unavailable");
  }
}

async function testInvalidJsonBudget() {
  const okInvoker = async () => ({ ok: true, content: "result" });
  const alternating = await runCase(
    [PROSE, sgRequest("one"), PROSE, sgRequest("two"), PROSE, PROSE],
    okInvoker,
  );
  assert.ok(alternating.error, "the run still ends");
  assert.strictEqual(alternating.error.message, "vscode_lm_text_protocol_invalid_json");
  assert.strictEqual(
    alternating.turns, 6,
    "prose separated by valid envelopes must not exhaust the budget; only the final consecutive pair does",
  );

  const consecutive = await runCase([PROSE, PROSE], okInvoker);
  assert.ok(consecutive.error);
  assert.strictEqual(consecutive.error.message, "vscode_lm_text_protocol_invalid_json");
  assert.strictEqual(consecutive.turns, 2);
  assert.ok(Array.isArray(consecutive.error.protocolTrace) && consecutive.error.protocolTrace.length >= 2);
  assert.ok(String(consecutive.error.protocolPreview || "").length > 0, "the offending reply is retained");
}

(async () => {
  testSanitizeErrorMessage();
  await testToolFailureCodes();
  await testInvalidJsonBudget();
  console.log("ok tool-error-code-invalid-json-budget-nf01201");
})().catch((err) => {
  console.error(err && err.stack || err);
  process.exit(1);
});
