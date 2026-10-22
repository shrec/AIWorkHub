"use strict";

// NF-2026-01632: both agent loops end because the model stopped making measured
// progress (novel Source Graph evidence or a new validated stage), never because
// a turn counter reached a constant.
const assert = require("node:assert/strict");
const test = require("node:test");
const Module = require("node:module");

const vscode = {
  workspace: { workspaceFolders: [], getConfiguration: () => ({ get: (_key, fallback) => fallback }) },
  LanguageModelChatToolMode: { Auto: 1, Required: 2 },
  LanguageModelToolResultPart: class { constructor(callId, content) { Object.assign(this, { callId, content }); } },
  LanguageModelChatMessage: {
    User: (content) => ({ role: "user", content }),
    Assistant: (content) => ({ role: "assistant", content }),
  },
};
const originalLoad = Module._load;
Module._load = function (name, parent, isMain) {
  return name === "vscode" ? vscode : originalLoad.call(this, name, parent, isMain);
};
let bridge;
try { bridge = require("../extension.js").__testInternals; }
finally { Module._load = originalLoad; }

const STALL = bridge.constants.VSCODE_LM_STALL_TURNS;
const REVIEW_STALL = bridge.constants.VSCODE_LM_QUALITY_REVIEW_STALL_TURNS;
const NO_ENDGAME = bridge.constants.VSCODE_LM_NO_ENDGAME_STALL_TURNS;
const SG = "aiworkhub_worker_source_graph_query";
const STAGE = "aiworkhub_manager_semantic_edit_stage";
const FINALIZE = "aiworkhub_manager_semantic_edit_finalize";
const SUBMIT = "aiworkhub_worker_quality_review_submit";
const FIRST = "src/first.py";
const SECOND = "src/second.py";
// Reads target a file the worker may not write, so forced staging never treats
// them as declared-file context reads.
const CONTEXT = "src/context.py";
const SUBMISSION = "d".repeat(64);
const TRANSPORTS = [["text", false], ["native", true]];
const run = (native) => (native ? bridge.runVscodeLmAgent : bridge.runVscodeLmTextProtocol);

const read = (n) => ({ name: SG, input: { mode: "body", query: `context.f${n}`, target: CONTEXT } });
const create = (path) => ({ name: STAGE, input: { operation: "create", file_path: path, content: "FLAG = True\n" } });
const submit = { name: SUBMIT, input: { packet_sha256: "e".repeat(64), lens: "correctness", findings: [] } };
const finalText = (summary) => JSON.stringify({
  schema_id: bridge.constants.VSCODE_LM_EDIT_RESPONSE_SCHEMA, summary, edits: [], creates: [],
});
const evidenceReply = (line) => ({
  ok: true, tool: "source_graph", mode: "body", authority_source: "canonical",
  authority_repo: "D:/Dev/AIWorkHub", hit_count: 1,
  content: JSON.stringify({ matches: [{
    file_path: CONTEXT, source_hash: "a".repeat(64), line_start: line, line_end: line,
    source: `def f${line}(): pass`, qualname: `context.f${line}`,
  }] }),
});

// Scripted provider. plan(turn, offeredTools, lastUserText) returns one tool
// call or { text }; offeredTools is null when the turn offered no tools.
function scriptedModel(native, plan) {
  const offered = [];
  return {
    offered,
    capabilities: { toolCalling: native },
    async sendRequest(messages, options) {
      const turn = offered.length;
      const tools = options && Array.isArray(options.tools) ? options.tools.map((tool) => tool.name) : null;
      offered.push(tools);
      const last = messages[messages.length - 1];
      const step = plan(turn, tools, last && typeof last.content === "string" ? last.content : "");
      const part = step.text !== undefined ? { value: step.text }
        : native ? { callId: `nf1632-${turn}`, ...step }
        : { value: JSON.stringify({ schema_id: bridge.constants.VSCODE_LM_TOOL_REQUEST_SCHEMA, ...step }) };
      return { stream: (async function* () { yield part; }()) };
    },
  };
}

// Live tool fixture: read number n carries one never-seen source row when
// evidence(n) holds, otherwise an untrusted reply that is never progress.
function liveTools(evidence = () => true) {
  const reads = [];
  const submits = [];
  const unexpected = [];
  const invoke = async (call) => {
    if (call.name === SUBMIT) {
      submits.push(call.input);
      return { ok: true, durable: true, submission_id: SUBMISSION };
    }
    if (call.name !== SG) {
      unexpected.push(call.name);
      return { ok: false, error: "unexpected_tool" };
    }
    reads.push(call.input.query);
    return evidence(reads.length)
      ? { ...evidenceReply(reads.length), mode: call.input.mode }
      : { ok: true, content: "graph" };
  };
  return Object.assign(invoke, { reads, submits, unexpected });
}

// Only the paths this run may write get create contracts: a legacy request
// (no required_create_paths) must create every one of them before it finalizes.
function workerRequest(writes, requiredOutputs = writes) {
  return {
    requestId: "c".repeat(32), request_kind: "worker", prompt: "Read what the change needs, then stage it.",
    allowedWrites: writes, required_outputs: requiredOutputs,
    path_contracts: Object.fromEntries(writes.map((path) => [path,
      { action: "create", parent_existed: false, line_count: 0, current_sha256: "" }])),
    initial_source_graph_request: { mode: "focus", query: "context" },
    initial_source_graph_result: { ok: true, content: "injected graph" },
  };
}

const readOnlyRequest = () => ({
  requestId: "e".repeat(32), request_kind: "worker", prompt: "Answer from the source alone.",
  allowedWrites: [], path_contracts: {},
  initial_source_graph_request: { mode: "focus", query: "context" },
  initial_source_graph_result: { ok: true, content: "injected graph" },
});

const reviewRequest = () => ({
  requestId: "f".repeat(32), request_kind: "quality_review", prompt: "Review the candidate.",
  allowedWrites: [], path_contracts: {},
});

test("the stall watchdog replaces every work ceiling while the Source Graph budget stays 16", () => {
  assert.equal(STALL, 12);
  assert.equal(REVIEW_STALL, 3);
  assert.equal(bridge.constants.VSCODE_LM_MAX_SOURCE_GRAPH_WITHOUT_EDIT, 16);
  // A stall that can arm no endgame also outlasts the four forced-stage reads, so it
  // never preempts the Source Graph discovery allowance.
  assert.equal(NO_ENDGAME, STALL + 4);
  assert.equal(NO_ENDGAME, bridge.constants.VSCODE_LM_MAX_SOURCE_GRAPH_WITHOUT_EDIT);
  for (const removed of [
    "VSCODE_LM_MAX_AGENT_TURNS", "VSCODE_LM_MAX_TOOL_TURNS", "VSCODE_LM_MAX_POST_SOURCE_TURNS",
    "VSCODE_LM_MAX_QUALITY_REVIEW_TURNS", "VSCODE_LM_MAX_QUALITY_REVIEW_READ_TURNS",
  ]) assert.ok(!(removed in bridge.constants), `${removed} must no longer bound the loops`);
});

test("any revision movement or the first Source Graph acknowledgement restarts the streak", () => {
  const guard = bridge.createVscodeLmSourceGraphGuard();
  let acknowledged = false;
  const watchdog = bridge.createVscodeLmProgressWatchdog(guard, () => acknowledged);
  assert.equal(watchdog.beginTurn(), 0);
  assert.equal(watchdog.beginTurn(), 1);
  assert.equal(watchdog.beginTurn(), 2);
  acknowledged = true;
  assert.equal(watchdog.beginTurn(), 0, "the work phase opens a fresh window");
  assert.equal(watchdog.beginTurn(), 1);
  const call = read(1);
  guard.observed(call, evidenceReply(1));
  assert.equal(watchdog.beginTurn(), 0, "novel source evidence is progress");
  guard.observed(call, evidenceReply(1));
  assert.equal(watchdog.beginTurn(), 1, "repeated evidence is not");
  const stage = { ok: true, schema_id: "aiworkhub.vscode_lm.staged_edit_receipt.v1",
    operation: "create", path: FIRST, content_sha256: "a".repeat(64) };
  guard.staged(stage, true);
  assert.equal(watchdog.beginTurn(), 0, "a new validated stage is progress");
  guard.staged(stage, true);
  assert.equal(watchdog.beginTurn(), 1, "an identical restage is not");
  watchdog.restart();
  assert.equal(watchdog.turnsWithoutProgress(), 0);
  assert.equal(watchdog.beginTurn(), 1);
});

for (const [label, native] of TRANSPORTS) {
  test(`${label}: a writable worker that keeps finding evidence runs past 40 turns and finishes its own edit`, async () => {
    const reads = 45;
    const model = scriptedModel(native, (turn) => {
      if (turn < reads) return read(turn);
      if (turn === reads) return create(FIRST);
      return { name: FINALIZE, input: { summary: "finished after 45 reads" } };
    });
    const tools = liveTools();
    const result = JSON.parse(await run(native)(model, workerRequest([FIRST]), undefined, tools));
    assert.equal(tools.reads.length, reads, "every progressing read executes");
    assert.equal(model.offered.length, reads + 2);
    assert.ok(model.offered.length > 40);
    assert.equal(result.summary, "finished after 45 reads", "the worker's own finalize ends the run");
    assert.deepEqual(result.creates.map((entry) => entry.path), [FIRST]);
    assert.deepEqual(tools.unexpected, []);
    if (native) {
      assert.ok(model.offered.every((names) => Array.isArray(names) && names.includes(SG)),
        "no turn was forced into staging or finalization while the worker progressed");
    }
  });

  test(`${label}: a read-only worker that keeps finding evidence is never forced to finalize`, async () => {
    const reads = STALL + 8;
    const model = scriptedModel(native, (turn) => (turn < reads ? read(turn) : { text: finalText("read-only answer") }));
    const tools = liveTools();
    const result = JSON.parse(await run(native)(model, readOnlyRequest(), undefined, tools));
    assert.equal(tools.reads.length, reads);
    assert.equal(model.offered.length, reads + 1);
    assert.equal(result.summary, "read-only answer");
    if (native) assert.ok(model.offered.every((names) => Array.isArray(names) && names.includes(SG)));
  });

  test(`${label}: a reviewer that keeps reading new evidence is never forced to submit`, async () => {
    // Replaces the NF-2026-01598 3+7 evidence-credit cap: 20 reads, each reply carrying
    // one never-seen row, all execute and the run reaches its own submit.
    const reads = 20;
    const model = scriptedModel(native, (turn) => (turn < reads ? read(turn) : submit));
    const tools = liveTools();
    const result = JSON.parse(await run(native)(model, reviewRequest(), undefined, tools));
    assert.deepEqual({ sg: tools.reads.length, submit: tools.submits.length }, { sg: 20, submit: 1 },
      "no read was refused as a forced-submit violation");
    assert.equal(model.offered.length, reads + 1);
    assert.equal(result.summary, `quality review submitted:${SUBMISSION}`);
    if (native) assert.ok(model.offered.every((names) => names.includes(SG)), "submit-only was never forced");
  });

  test(`${label}: a read-only run is forced to final after exactly ${STALL} stalled turns`, async () => {
    for (const [evidenceAt, expectedForcedAt] of [[0, STALL], [STALL - 1, STALL - 1 + STALL]]) {
      let forcedAt = -1;
      const model = scriptedModel(native, (turn, tools, lastUserText) => {
        const forced = native ? tools === null
          : lastUserText.startsWith("The bounded tool/reasoning phase is complete");
        if (!forced) return read(turn);
        if (forcedAt < 0) forcedAt = turn;
        return { text: finalText("forced answer") };
      });
      // Read evidenceAt (1-based) is the only progressing turn; 0 means none.
      const tools = liveTools((n) => n === evidenceAt);
      const result = JSON.parse(await run(native)(model, readOnlyRequest(), undefined, tools));
      assert.equal(forcedAt, expectedForcedAt, "one progressing turn restarts the stall window");
      assert.equal(tools.reads.length, expectedForcedAt, "every turn before the endgame executed its read");
      assert.equal(result.summary, "forced answer");
    }
  });

  test(`${label}: a writable run is forced to stage after exactly ${STALL} stalled turns`, async () => {
    const model = scriptedModel(native, (turn) => {
      if (turn === 0) return create(FIRST);
      // Turn STALL + 1 is the first forced-staging turn: its read is refused unexecuted.
      if (turn <= STALL + 1) return read(turn);
      return create(SECOND);
    });
    const tools = liveTools(() => false);
    const result = JSON.parse(await run(native)(model, workerRequest([FIRST, SECOND]), undefined, tools));
    assert.equal(tools.reads.length, STALL, "the read after the stall is refused by forced staging");
    assert.equal(model.offered.length, STALL + 3);
    assert.deepEqual(result.creates.map((entry) => entry.path).sort(), [FIRST, SECOND]);
    assert.equal(result.summary, "Applied validated staged semantic edits.");
    if (native) {
      assert.deepEqual(model.offered[STALL + 1], [STAGE], "staging is forced on the first turn after the stall");
      assert.ok(model.offered[STALL].includes(SG), "and not one turn earlier");
    }
  });

  test(`${label}: a reviewer is forced to submit after exactly ${REVIEW_STALL} stalled turns`, async () => {
    for (const [evidenceAt, expectedForcedAt] of [[0, REVIEW_STALL], [2, 2 + REVIEW_STALL]]) {
      const model = scriptedModel(native, (turn) => (turn <= expectedForcedAt ? read(turn) : submit));
      const tools = liveTools((n) => n === evidenceAt);
      const result = JSON.parse(await run(native)(model, reviewRequest(), undefined, tools));
      assert.equal(tools.reads.length, expectedForcedAt,
        "the read on the first forced turn is refused as a forced-submit violation");
      assert.equal(model.offered.length, expectedForcedAt + 2);
      assert.equal(result.summary, `quality review submitted:${SUBMISSION}`);
      if (native) {
        assert.deepEqual(model.offered[expectedForcedAt], [SUBMIT]);
        assert.ok(model.offered[expectedForcedAt - 1].includes(SG));
      }
    }
  });

  test(`${label}: a stall with no endgame to force ends the run as no progress after ${NO_ENDGAME} turns`, async () => {
    // Nothing is owed, so no endgame can be armed at STALL; the run ends only once the
    // stall also outlasts the forced-stage read allowance, before another provider turn.
    const model = scriptedModel(native, (turn) => read(turn));
    const tools = liveTools(() => false);
    await assert.rejects(run(native)(model, workerRequest([FIRST], []), undefined, tools), (error) => {
      assert.equal(error.message, "vscode_lm_source_graph_no_progress");
      assert.deepEqual(error.protocolTrace.at(-1), { turn: NO_ENDGAME, phase: "progress_watchdog", outcome: "stalled" });
      return true;
    });
    assert.equal(tools.reads.length, NO_ENDGAME, "every stalled read inside the window executes");
    assert.equal(model.offered.length, NO_ENDGAME, "the run ends before another provider turn");
  });
}

test("native: the 16-call Source Graph budget still ends a no-evidence batch spin inside the stall window", async () => {
  let turns = 0;
  const model = {
    capabilities: { toolCalling: true },
    async sendRequest() {
      const turn = turns++;
      return { stream: (async function* () {
        yield { callId: `batch-${turn}-a`, ...read(2 * turn) };
        yield { callId: `batch-${turn}-b`, ...read(2 * turn + 1) };
      }()) };
    },
  };
  const tools = liveTools(() => false);
  await assert.rejects(bridge.runVscodeLmAgent(model, workerRequest([FIRST], []), undefined, tools),
    /vscode_lm_source_graph_no_progress/);
  assert.equal(tools.reads.length, bridge.constants.VSCODE_LM_MAX_SOURCE_GRAPH_WITHOUT_EDIT);
  assert.ok(turns < STALL, `the call budget fired at turn ${turns}, inside the stall window`);
});
