"use strict";

// NF-2026-02288: a stale-coordinates semantic-edit refusal re-grounds the vscode_lm worker
// (a corrective tool result naming the range to re-prepare) instead of killing the attempt,
// bounded PER PATH by VSCODE_LM_MAX_STALE_COORDINATE_RETRIES (re-ground while retries < cap);
// after the bound the attempt still fails closed with vscode_lm_semantic_edit_stage_required.
// A provider turn whose stream carried only a data part is a no-progress turn, never a work turn.
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const test = require("node:test");
const Module = require("node:module");

class DataPart { constructor(data, mimeType) { this.data = data; this.mimeType = mimeType; } }
const vscode = {
  workspace: { workspaceFolders: [], getConfiguration: () => ({ get: (_key, fallback) => fallback }) },
  LanguageModelChatToolMode: { Auto: 1, Required: 2 },
  LanguageModelDataPart: DataPart,
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

const helper = "src/helper.js";
const STAGE = "aiworkhub_manager_semantic_edit_stage";
const PREPARE = "aiworkhub_worker_semantic_edit_prepare";
const APPLY = "aiworkhub_worker_semantic_edit_apply";
const SOURCE_GRAPH = "aiworkhub_worker_source_graph_query";
const IDLE = "aiworkhub_worker_session_current_state";
const CAP = bridge.VSCODE_LM_MAX_STALE_COORDINATE_RETRIES;
const baseSha = "a".repeat(64);
const threeLines = "const a = 1;\nconst b = 2;\nconst c = 3;";
const twoLines = "const d = 4;\nconst e = 5;";

const workerRequest = () => ({
  requestId: "8".repeat(32), request_kind: "worker", prompt: "Edit the helper.",
  allowedWrites: [helper], required_outputs: [helper],
  path_contracts: { [helper]: { action: "edit", current_sha256: baseSha, line_count: 60, parent_existed: true } },
  initial_source_graph_request: { mode: "focus", query: helper },
  initial_source_graph_result: { ok: true, content: "prefetched graph" },
});
const stageAt = (start, end, text) => ({
  operation: "replace_range", file_path: helper, start_line: start, end_line: end,
  expected_first_line: `first of ${start}`, expected_last_line: `last of ${end}`, new: text,
});

// Mock worker transport: typed prepare/apply replies whose content hash moves on every apply,
// so a prepare bound before an apply is stale afterwards. Not a real worker or HMAC acceptance.
function fakeWorker() {
  const calls = [];
  const targets = new Map();
  let sha = baseSha;
  const worker = async (call) => {
    if (call.name === SOURCE_GRAPH) {
      return { ok: true, tool: "source_graph", mode: "body", authority_source: "canonical", authority_repo: "repo",
        content: JSON.stringify({ matches: [{
          file_path: helper, line_start: 1, line_end: 20,
          source: Array.from({ length: 20 }, (_, index) => `line ${index + 1}`).join("\n"),
          freshness: { state: "fresh", disk_source_hash: sha },
        }] }) };
    }
    if (call.name === IDLE) return { ok: true, content: "idle" };
    calls.push(call);
    if (call.name === PREPARE) {
      const targetId = `target-${calls.length}`;
      targets.set(targetId, { path: call.input.file_path, sha });
      worker.lastTarget = targetId;
      return { ok: true, tool: "semantic_edit_prepare", target_id: targetId, path: call.input.file_path,
        start_line: call.input.start_line, end_line: call.input.end_line,
        current_sha256: sha, fragment_sha256: "f".repeat(64) };
    }
    assert.equal(call.name, APPLY);
    const target = targets.get(call.input.target_id);
    if (!target || target.sha !== sha) {
      return { ok: false, reason: `semantic_edit_preimage_mismatch:${target ? target.path : "unknown"}` };
    }
    const before = sha;
    sha = crypto.createHash("sha256").update(before + call.input.new).digest("hex");
    return { ok: true, schema_id: "aiworkhub.semantic_edit_apply_receipt.v1", target_id: call.input.target_id,
      path: target.path, before_sha256: before, after_sha256: sha,
      idempotency_key: call.input.idempotency_key, preimage_verified: true };
  };
  worker.calls = calls;
  worker.lastTarget = "";
  return worker;
}
const appliedTexts = (worker) => worker.calls.filter((call) => call.name === APPLY).map((call) => call.input.new);
const toolRequest = (name, input) => JSON.stringify({ schema_id: bridge.constants.VSCODE_LM_TOOL_REQUEST_SCHEMA, name, input });
const lastUserText = (messages) => {
  const last = messages[messages.length - 1];
  return last && last.role === "user" ? String(last.content) : "";
};
const toolResultOf = (text) => { try { return JSON.parse(text).result || null; } catch (_err) { return null; } };

// Text protocol: grow the file with a staged prepare+apply, have the stale second stage refused,
// idle until forced staging asks for the recovery, then run `forcedPlan` (a function of the worker).
async function runForcedRecovery(forcedPlan, turnLimit = 80) {
  const worker = fakeWorker();
  const seen = [];
  let turns = 0;
  let plan = null;
  const opening = [toolRequest(STAGE, stageAt(3, 4, threeLines)), toolRequest(STAGE, stageAt(10, 11, twoLines))];
  const model = {
    capabilities: { toolCalling: false },
    sendRequest: async (messages) => {
      turns += 1;
      assert.ok(turns <= turnLimit, `the run must terminate boundedly, turns=${turns}`);
      const instruction = lastUserText(messages);
      seen.push(instruction);
      let value;
      if (opening.length) value = opening.shift();
      else {
        // Forced staging announces the recovery as a plain instruction; a tool-result JSON naming
        // the same recovery (outside forced staging) does not start the plan.
        if (!plan && /needs recovery/.test(instruction) && !instruction.startsWith("{")) plan = forcedPlan(worker);
        value = plan ? plan.next(instruction) : toolRequest(IDLE, { limit: 1 });
      }
      return { stream: (async function* () { yield { value }; })() };
    },
  };
  let output = null;
  let error = null;
  try { output = await bridge.runVscodeLmTextProtocol(model, workerRequest(), undefined, worker); }
  catch (err) { error = err; }
  return { worker, seen, turns, output, error, forced: Boolean(plan) };
}

test("a repeated stale stage and an early prepare re-ground the worker; a fresh prepare+apply then succeeds", async () => {
  const observed = [];
  const run = await runForcedRecovery((worker) => {
    const steps = [
      () => toolRequest(STAGE, stageAt(10, 11, twoLines)), // stale repeat while the recovery is pending
      () => toolRequest(PREPARE, { file_path: helper, start_line: 10, end_line: 11 }), // prepare before the fresh read
      () => toolRequest(SOURCE_GRAPH, { mode: "body", target: helper, query: "helper" }),
      () => toolRequest(PREPARE, { file_path: helper, start_line: 12, end_line: 13 }),
      () => toolRequest(APPLY, { target_id: worker.lastTarget, new: twoLines, idempotency_key: "nf02288-fresh" }),
    ];
    return { next: (instruction) => {
      observed.push(toolResultOf(instruction));
      const step = steps.shift();
      assert.ok(step, "the recovered run finalizes offline without another provider turn");
      return step();
    } };
  });
  assert.equal(run.error, null, `the attempt is not terminated: ${run.error && run.error.message}`);
  assert.ok(run.forced, "the scenario reached forced staging");
  const final = JSON.parse(run.output);
  assert.deepEqual(final.edits, [], "authenticated applies are never emitted twice");
  assert.deepEqual(appliedTexts(run.worker), [threeLines, twoLines], "the first edit is kept and the fresh pair lands once");

  // observed[i] is the tool result the model saw before step i: [1] after the stale repeat,
  // [2] after the early prepare (the arming refusal before forced staging was retry 0).
  const regrounds = observed.slice(1, 3);
  regrounds.forEach((result, index) => {
    assert.ok(result, `retry ${index + 1} returned a tool result`);
    assert.equal(result.ok, false);
    assert.equal(result.corrective, true);
    assert.match(result.reason, /coordinates_shifted_requires_fresh_worker_pair:src\/helper\.js$/);
    assert.deepEqual(result.reprepare_range, { start_line: 10, end_line: 11 });
    assert.equal(result.stale_coordinate_retries, index + 1);
    assert.equal(result.stale_coordinate_retry_limit, CAP);
    assert.match(result.instruction, /re-prepare exactly that range/);
    assert.match(result.instruction, /lines 10-11/);
  });
});

test("after the retry cap is exhausted the attempt still fails closed with the existing error code", async () => {
  const run = await runForcedRecovery(() => ({ next: () => toolRequest(STAGE, stageAt(10, 11, twoLines)) }));
  assert.ok(run.error, "a worker that never re-grounds is terminated");
  assert.match(run.error.message, /^vscode_lm_semantic_edit_stage_required/);
  const outcomes = (run.error.protocolTrace || []).map((entry) => entry.outcome);
  // The arming refusal (before forced staging) is retry 0 of the path's CAP re-grounds.
  assert.equal(outcomes.filter((outcome) => outcome === "stage_regrounded").length, CAP - 1);
  assert.ok(outcomes.filter((outcome) => outcome === "stage_rejected").length >= 1);
  assert.deepEqual(appliedTexts(run.worker), [threeLines], "refused stale stages never reach the worker");
});

test("an exhausted bound also stops re-grounding early recovery calls", async () => {
  const run = await runForcedRecovery(() => ({
    next: () => toolRequest(PREPARE, { file_path: helper, start_line: 10, end_line: 11 }),
  }));
  assert.ok(run.error);
  assert.match(run.error.message, /^vscode_lm_semantic_edit_stage_required/);
  const outcomes = (run.error.protocolTrace || []).map((entry) => entry.outcome);
  assert.equal(outcomes.filter((outcome) => outcome === "recovery_call_regrounded").length, CAP);
  assert.ok(outcomes.includes("non_stage_tool_rejected"));
});

test("a stale worker apply after a shifting apply is refused without killing the attempt", async () => {
  // Two prepares on one file, then the first apply shifts its lines: the stale second apply is
  // refused by the worker, comes back to the model as a tool result, and a fresh pair succeeds.
  const worker = fakeWorker();
  const collector = bridge.createVscodeLmStagedEditCollector(workerRequest());
  const first = { name: PREPARE, input: { file_path: helper, start_line: 3, end_line: 4 } };
  const firstPrepared = await worker(first);
  collector.observeSemanticEdit(first, firstPrepared);
  const second = { name: PREPARE, input: { file_path: helper, start_line: 10, end_line: 11 } };
  const secondPrepared = await worker(second);
  collector.observeSemanticEdit(second, secondPrepared);
  const applyFirst = { name: APPLY, input: { target_id: firstPrepared.target_id, new: threeLines, idempotency_key: "k1" } };
  assert.equal(collector.observeSemanticEdit(applyFirst, await worker(applyFirst)), true);
  const applyStale = { name: APPLY, input: { target_id: secondPrepared.target_id, new: twoLines, idempotency_key: "k2" } };
  const stale = await worker(applyStale);
  assert.equal(stale.ok, false);
  assert.equal(collector.observeSemanticEdit(applyStale, stale), false);
  // The staged route refuses the stale coordinates with the corrective re-prepare result.
  const refused = await collector.stage(stageAt(10, 11, twoLines), worker);
  assert.equal(refused.corrective, true);
  assert.deepEqual(refused.reprepare_range, { start_line: 10, end_line: 11 });
  assert.equal(bridge.vscodeLmForcedStageRegrounds(STAGE, refused, collector), true);
  const fresh = { name: PREPARE, input: { file_path: helper, start_line: 11, end_line: 12 } };
  const freshPrepared = await worker(fresh);
  collector.observeSemanticEdit(fresh, freshPrepared);
  const applyFresh = { name: APPLY, input: { target_id: freshPrepared.target_id, new: twoLines, idempotency_key: "k3" } };
  const applied = await worker(applyFresh);
  assert.equal(applied.ok, true);
  assert.equal(collector.observeSemanticEdit(applyFresh, applied), true);
  assert.deepEqual(appliedTexts(worker), [threeLines, twoLines, twoLines],
    "first apply, the refused stale apply, then the fresh apply");
  assert.equal(collector.hasChanges(), true);
});

test("forced-stage re-grounding is bounded per path", async () => {
  const worker = fakeWorker();
  const collector = bridge.createVscodeLmStagedEditCollector(workerRequest());
  assert.equal((await collector.stage(stageAt(3, 4, threeLines), worker)).ok, true);
  const verdicts = [];
  for (let attempt = 0; attempt <= CAP; attempt += 1) {
    const refused = await collector.stage(stageAt(10, 11, twoLines), worker);
    assert.match(refused.reason, /coordinates_shifted_requires_fresh_worker_pair/);
    verdicts.push(bridge.vscodeLmForcedStageRegrounds(STAGE, refused, collector));
  }
  // Re-ground while retries < CAP (the arming refusal is retry 0); the next one spends corrections.
  assert.deepEqual(verdicts, [...Array(CAP).fill(true), false]);
  assert.equal(bridge.vscodeLmRecoveryCallReground(collector, [{ name: PREPARE, input: {} }]), null);
  assert.equal(bridge.vscodeLmRecoveryCallReground(collector, [{ name: STAGE, input: {} }]), null);
});

test("alternating stale refusals on two paths stay bounded per path and never reset", async () => {
  const other = "src/other.js";
  const request = workerRequest();
  request.allowedWrites = [helper, other];
  request.required_outputs = [helper, other];
  request.path_contracts[other] = { action: "edit", current_sha256: baseSha, line_count: 60, parent_existed: true };
  const worker = fakeWorker();
  const collector = bridge.createVscodeLmStagedEditCollector(request);
  assert.equal((await collector.stage(stageAt(3, 4, threeLines), worker)).ok, true);
  // The mock's content hash is global, so the second path starts from its own fresh mock.
  assert.equal((await collector.stage({ ...stageAt(3, 4, threeLines), file_path: other }, fakeWorker())).ok, true);
  const regrounded = { [helper]: 0, [other]: 0 };
  const rounds = 3 * CAP;
  for (let attempt = 0; attempt < 2 * rounds; attempt += 1) {
    const filePath = attempt % 2 === 0 ? helper : other;
    const refused = await collector.stage({ ...stageAt(10, 11, twoLines), file_path: filePath }, worker);
    assert.equal(refused.reason, `semantic_edit_stage_rejected:coordinates_shifted_requires_fresh_worker_pair:${filePath}`);
    // The reported count never exceeds the limit, however many refusals follow.
    assert.ok(refused.stale_coordinate_retries <= CAP, JSON.stringify(refused.stale_coordinate_retries));
    const used = Number(/retries used: (\d+) of (\d+)/.exec(refused.instruction)[1]);
    assert.ok(used <= CAP, refused.instruction);
    if (bridge.vscodeLmForcedStageRegrounds(STAGE, refused, collector)) regrounded[filePath] += 1;
  }
  assert.equal(regrounded[helper], CAP, "switching paths never resets the helper's retries");
  assert.equal(regrounded[other], CAP, "switching paths never resets the other path's retries");
  assert.ok(regrounded[helper] + regrounded[other] <= 2 * CAP);
  // Both bounds are spent: an early recovery call on the current path no longer re-grounds either.
  assert.equal(bridge.vscodeLmRecoveryCallReground(collector, [{ name: PREPARE, input: {} }]), null);
});

test("native: a mixed turn runs the allowed fresh read and re-grounds only the refused prepare", async () => {
  const worker = fakeWorker();
  const invoked = [];
  const tracked = async (call, ...rest) => { invoked.push(call.name); return worker(call, ...rest); };
  const opening = [
    [{ callId: "s1", name: STAGE, input: stageAt(3, 4, threeLines) }],
    [{ callId: "s2", name: STAGE, input: stageAt(10, 11, twoLines) }],
  ];
  let mixedReply = null;
  let invokedBeforeMixed = -1;
  const plan = [
    () => {
      invokedBeforeMixed = invoked.length;
      return [
        { callId: "m1", name: SOURCE_GRAPH, input: { mode: "body", target: helper, query: "helper" } },
        { callId: "m2", name: PREPARE, input: { file_path: helper, start_line: 10, end_line: 11 } },
      ];
    },
    (last) => {
      mixedReply = last;
      return [{ callId: "p1", name: PREPARE, input: { file_path: helper, start_line: 12, end_line: 13 } }];
    },
    () => [{ callId: "a1", name: APPLY,
      input: { target_id: worker.lastTarget, new: twoLines, idempotency_key: "nf02288-native" } }],
  ];
  let forced = false;
  let turns = 0;
  const model = {
    id: "nf02288-native", capabilities: { toolCalling: true },
    sendRequest: async (messages) => {
      turns += 1;
      assert.ok(turns <= 80, `the run must terminate boundedly, turns=${turns}`);
      const last = messages[messages.length - 1];
      let parts;
      if (opening.length) parts = opening.shift();
      else {
        if (!forced && last && last.role === "user" && typeof last.content === "string" &&
            /needs recovery/.test(last.content)) forced = true;
        if (forced) {
          const step = plan.shift();
          assert.ok(step, "the recovered run finalizes offline without another provider turn");
          parts = step(last);
        } else parts = [{ value: "idle" }];
      }
      return { stream: (async function* () { for (const part of parts) yield part; })() };
    },
  };
  const output = await bridge.runVscodeLmAgent(model, workerRequest(), undefined, tracked);
  assert.ok(forced, "the scenario reached forced staging");
  assert.deepEqual(JSON.parse(output).edits, [], "authenticated applies are never emitted twice");
  assert.ok(mixedReply && Array.isArray(mixedReply.content), "the mixed turn was answered with tool results");
  const byCallId = new Map(mixedReply.content.map((part) => [part.callId, JSON.stringify(part.content)]));
  assert.deepEqual([...byCallId.keys()], ["m1", "m2"], "every call of the mixed turn is answered in order");
  assert.equal(invoked[invokedBeforeMixed], SOURCE_GRAPH, "the allowed fresh read executed");
  assert.equal(invoked.slice(invokedBeforeMixed).filter((name) => name === PREPARE).length, 1,
    "the refused prepare never reached the worker; only the fresh prepare did");
  assert.match(byCallId.get("m2"), /coordinates_shifted_requires_fresh_worker_pair/);
  assert.match(byCallId.get("m2"), /re-prepare exactly that range/);
  assert.doesNotMatch(byCallId.get("m1"), /coordinates_shifted_requires_fresh_worker_pair/);
  assert.deepEqual(appliedTexts(worker), [threeLines, twoLines], "the first edit is kept and the fresh pair lands once");
});

test("vscodeLmIsDataOnlyTurn recognises only streams made purely of data parts", () => {
  const data = new DataPart(new Uint8Array([1]), "stateful_marker");
  assert.equal(bridge.vscodeLmIsDataOnlyTurn([data]), true);
  assert.equal(bridge.vscodeLmIsDataOnlyTurn([data, { mimeType: "cache_control", data: Buffer.from("x") }]), true);
  assert.equal(bridge.vscodeLmIsDataOnlyTurn([]), false);
  assert.equal(bridge.vscodeLmIsDataOnlyTurn([data, { value: "text" }]), false);
  assert.equal(bridge.vscodeLmIsDataOnlyTurn([data, { callId: "c", name: STAGE, input: {} }]), false);
});

function dataOnlyModel(toolCalling) {
  const model = {
    id: "nf02288-data-only", capabilities: { toolCalling },
    turns: 0,
    sendRequest: async () => {
      model.turns += 1;
      assert.ok(model.turns <= 80, "data-only turns stay bounded by the progress watchdog");
      return { stream: (async function* () { yield new DataPart(new Uint8Array([7]), "stateful_marker"); })() };
    },
  };
  return model;
}

for (const [label, toolCalling, run] of [
  ["native", true, (model, request) => bridge.runVscodeLmAgent(model, request, undefined, async () => ({ ok: true }))],
  ["text", false, (model, request) => bridge.runVscodeLmTextProtocol(model, request, undefined, async () => ({ ok: true }))],
]) {
  test(`${label}: data-only turns are no-progress turns, not work turns or stage violations`, async () => {
    const model = dataOnlyModel(toolCalling);
    const request = workerRequest();
    if (!toolCalling) {
      // Give the text loop a staged edit so its stall arms forced staging, as in the native loop.
      const worker = fakeWorker();
      let first = true;
      model.sendRequest = async () => {
        model.turns += 1;
        assert.ok(model.turns <= 80, "data-only turns stay bounded by the progress watchdog");
        if (first) {
          first = false;
          return { stream: (async function* () { yield { value: toolRequest(STAGE, stageAt(3, 4, threeLines)) }; })() };
        }
        return { stream: (async function* () { yield new DataPart(new Uint8Array([7]), "stateful_marker"); })() };
      };
      request.required_outputs = [helper, "src/other.js"];
      request.allowedWrites = [helper, "src/other.js"];
      request.path_contracts["src/other.js"] = { action: "edit", current_sha256: "b".repeat(64), line_count: 5, parent_existed: true };
      await assert.rejects(() => bridge.runVscodeLmTextProtocol(model, request, undefined, worker), (err) => {
        assertDataOnlyTrace(err);
        return true;
      });
      return;
    }
    await assert.rejects(() => run(model, request), (err) => {
      assertDataOnlyTrace(err);
      return true;
    });
  });
}

function assertDataOnlyTrace(err) {
  const trace = err.protocolTrace || [];
  const dataOnly = trace.filter((entry) => entry.outcome === "data_only");
  assert.ok(dataOnly.length > 0, JSON.stringify(trace).slice(0, 400));
  assert.ok(dataOnly.every((entry) => entry.phase === "no_progress"), "a data-only turn is never a work turn");
  assert.equal(trace.filter((entry) => entry.outcome === "empty").length, 0);
  // Not a forced-stage violation: the run ends through the progress watchdog, not stage_required.
  assert.doesNotMatch(String(err.message), /vscode_lm_semantic_edit_stage_required/);
  assert.ok(trace.some((entry) => entry.phase === "progress_watchdog" && entry.outcome === "stalled"),
    "the existing progress watchdog bounds data-only turns");
}
