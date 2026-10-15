const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const Module = require("node:module");
const path = require("node:path");

const originalLoad = Module._load;
Module._load = function (name, parent, isMain) {
  if (name === "vscode") return {
    workspace: { workspaceFolders: [], getConfiguration: () => ({ get: (_key, fallback) => fallback }) },
    LanguageModelChatMessage: {
      User: (content) => ({ role: "user", content }),
      Assistant: (content) => ({ role: "assistant", content }),
    },
  };
  return originalLoad.call(this, name, parent, isMain);
};
let bridge;
try {
  bridge = require(path.resolve(__dirname, "../extension.js")).__testInternals;
} finally {
  Module._load = originalLoad;
}

const helper = "src/helper.js";
const STAGE = "aiworkhub_manager_semantic_edit_stage";
const FINALIZE = "aiworkhub_manager_semantic_edit_finalize";
const PREPARE = "aiworkhub_worker_semantic_edit_prepare";
const APPLY = "aiworkhub_worker_semantic_edit_apply";
const SOURCE_GRAPH = "aiworkhub_worker_source_graph_query";
const baseSha = "a".repeat(64);
const threeLines = "const a = 1;\nconst b = 2;\nconst c = 3;";
const twoLines = "const d = 4;\nconst e = 5;";

const workerRequest = () => ({
  requestId: "7".repeat(32), request_kind: "worker", prompt: "Edit the helper.",
  allowedWrites: [helper], required_outputs: [helper],
  path_contracts: { [helper]: { action: "edit", current_sha256: baseSha, line_count: 60, parent_existed: true } },
  initial_source_graph_request: { mode: "focus", query: helper },
  initial_source_graph_result: { ok: true, content: "prefetched graph" },
});
const stageAt = (start, end, text) => ({
  operation: "replace_range", file_path: helper, start_line: start, end_line: end,
  expected_first_line: `first of ${start}`, expected_last_line: `last of ${end}`, new: text,
});

// Mock worker transport: typed replies exercise bridge bookkeeping, not a real worker or HMAC acceptance claim.
function fakeWorker() {
  const calls = [];
  const targets = new Map();
  let sha = baseSha;
  const worker = async (call) => {
    calls.push(call);
    if (call.name === PREPARE) {
      const targetId = `target-${calls.length}`;
      targets.set(targetId, call.input.file_path);
      return { ok: true, tool: "semantic_edit_prepare", target_id: targetId, path: call.input.file_path,
        start_line: call.input.start_line, end_line: call.input.end_line,
        current_sha256: sha, fragment_sha256: "f".repeat(64) };
    }
    assert.equal(call.name, APPLY);
    const before = sha;
    sha = crypto.createHash("sha256").update(before + call.input.new).digest("hex");
    return { ok: true, schema_id: "aiworkhub.semantic_edit_apply_receipt.v1", target_id: call.input.target_id,
      path: targets.get(call.input.target_id), before_sha256: before, after_sha256: sha,
      idempotency_key: call.input.idempotency_key, preimage_verified: true };
  };
  worker.calls = calls;
  worker.currentSha = () => sha;
  return worker;
}

const callNames = (worker) => worker.calls.map((call) => call.name);
const freshBody = (hash, lineStart, lineEnd) => ({
  ok: true, tool: "source_graph", mode: "body", authority_source: "canonical", authority_repo: "repo",
  content: JSON.stringify({ matches: [{
    file_path: helper, line_start: lineStart, line_end: lineEnd,
    source: Array.from({ length: lineEnd - lineStart + 1 }, (_, index) => `line ${lineStart + index}`).join("\n"),
    freshness: { state: "fresh", disk_source_hash: hash },
  }] }),
});
const bodyCall = { name: SOURCE_GRAPH, input: { mode: "body", target: helper, query: "helper" } };

// Stage a growing replace, then a stage below it that is refused for shifted coordinates.
async function shiftedRefusal(worker) {
  const collector = bridge.createVscodeLmStagedEditCollector(workerRequest());
  const grown = await collector.stage(stageAt(3, 4, threeLines), worker);
  assert.equal(grown.ok, true, JSON.stringify(grown));
  const refused = await collector.stage(stageAt(10, 11, twoLines), worker);
  assert.equal(refused.ok, false);
  assert.match(refused.reason, /coordinates_shifted_requires_fresh_worker_pair/);
  return collector;
}

async function pendingRecoveryIsNeverReady() {
  const worker = fakeWorker();
  const collector = await shiftedRefusal(worker);
  assert.equal(collector.hasChanges(), true);
  assert.equal(collector.nextMissingRequired(), null, "the required output itself is staged");
  assert.deepEqual(collector.recoveryRequiredOutput(), { path: helper, action: "replace_range", recovery: true });
  assert.equal(bridge.vscodeLmStagedOutputsReady(collector), false);
  const finalized = collector.finalize("must not finalize while recovering");
  assert.equal(finalized.ok, false);
  assert.equal(finalized.reason, `semantic_edit_recovery_outstanding:${helper}`);
}

// The post-source limit may force a stage but must never let the run finish offline.
async function postSourceLimitDoesNotForceFinal() {
  const worker = fakeWorker();
  const seen = [];
  const steps = [
    { name: STAGE, input: stageAt(3, 4, threeLines) },
    { name: STAGE, input: stageAt(10, 11, twoLines) },
  ];
  let turns = 0;
  const model = {
    capabilities: { toolCalling: false },
    sendRequest: async (messages) => {
      seen.push(String(messages[messages.length - 1].content));
      if (turns > 12) throw new Error("the run must terminate boundedly");
      const step = steps[turns];
      turns += 1;
      const value = step
        ? JSON.stringify({ schema_id: bridge.constants.VSCODE_LM_TOOL_REQUEST_SCHEMA, ...step })
        : "All edits are complete.";
      return { stream: (async function* () { yield { value }; })() };
    },
  };
  await assert.rejects(() => bridge.runVscodeLmTextProtocol(model, workerRequest(), undefined, worker));
  assert.ok(turns > 2 && turns <= 12, `bounded termination, turns=${turns}`);
  const instruction = seen.slice(2).join("\n");
  assert.ok(instruction.includes(helper), "the forced instruction names the recovery path");
  assert.match(instruction, /needs recovery/);
  assert.match(instruction, /fresh bounded Source Graph body/);
  assert.deepEqual(callNames(worker), [PREPARE, APPLY], "the refused stage never reaches the worker");
}

async function freshReadStageClearsRecoveryAndFinalizeSucceeds() {
  for (const [start, end, lineStart, lineEnd] of [[10, 11, 1, 20], [60, 61, 50, 61]]) {
    const worker = fakeWorker();
    const collector = await shiftedRefusal(worker);
    collector.noteRecoverySourceGraph(bodyCall, freshBody(worker.currentSha(), lineStart, lineEnd));
    const staged = await collector.stage(stageAt(start, end, twoLines), worker);
    assert.equal(staged.ok, true, JSON.stringify(staged));
    assert.deepEqual([staged.start_line, staged.end_line], [start, end]);
    assert.equal(collector.recoveryRequiredOutput(), null);
    assert.equal(bridge.vscodeLmStagedOutputsReady(collector), true);
    const finished = collector.finalize("recovered through a fresh read");
    assert.equal(finished.ok, true, JSON.stringify(finished));
    assert.equal(finished.staged_edit_count, 2);
    assert.deepEqual(finished.__finalEnvelope.edits, [], "authenticated applies are not emitted twice");
    assert.deepEqual(callNames(worker), [PREPARE, APPLY, PREPARE, APPLY]);
    assert.equal(worker.calls.filter((call) => call.name === APPLY && call.input.new === twoLines).length, 1,
      "the recovery edit is applied exactly once");
  }
}

async function recoveryLoopFinalizesAfterFreshRead() {
  const worker = fakeWorker();
  const transport = async (call) => call.name === SOURCE_GRAPH
    ? freshBody(worker.currentSha(), 1, 20) : worker(call);
  const steps = [
    { name: STAGE, input: stageAt(3, 4, threeLines) },
    { name: STAGE, input: stageAt(10, 11, twoLines) },
    bodyCall,
    { name: STAGE, input: stageAt(10, 11, twoLines) },
    { name: FINALIZE, input: { summary: "Recovered with a fresh read." } },
  ];
  let turns = 0;
  const model = {
    capabilities: { toolCalling: false },
    sendRequest: async () => {
      const step = steps[turns++];
      assert.ok(step, "the recovered run terminates with its own finalize");
      const value = JSON.stringify({ schema_id: bridge.constants.VSCODE_LM_TOOL_REQUEST_SCHEMA, ...step });
      return { stream: (async function* () { yield { value }; })() };
    },
  };
  const result = JSON.parse(await bridge.runVscodeLmTextProtocol(model, workerRequest(), undefined, transport));
  assert.equal(turns, steps.length);
  assert.deepEqual(result.edits, []);
  assert.equal(worker.calls.filter((call) => call.name === APPLY && call.input.new === twoLines).length, 1);
}

async function staleStageWithoutFreshReadIsStillRefused() {
  const worker = fakeWorker();
  const collector = await shiftedRefusal(worker);
  const settled = worker.calls.length;
  const again = await collector.stage(stageAt(10, 11, twoLines), worker);
  assert.equal(again.ok, false);
  assert.match(again.reason, /coordinates_shifted_requires_fresh_worker_pair/);

  const beforeStale = worker.currentSha();
  collector.noteRecoverySourceGraph(bodyCall, freshBody("b".repeat(64), 1, 20));
  const stale = await collector.stage(stageAt(10, 11, twoLines), worker);
  assert.equal(stale.ok, false, "evidence for a different content hash does not authorize a stage");
  collector.noteRecoverySourceGraph(bodyCall, freshBody(beforeStale, 1, 8));
  const outside = await collector.stage(stageAt(10, 11, twoLines), worker);
  assert.equal(outside.ok, false, "a stage outside the fresh body lines is refused");

  assert.equal(worker.calls.length, settled, "refused stages never reach the worker");
  assert.deepEqual(collector.recoveryRequiredOutput(), { path: helper, action: "replace_range", recovery: true });
  assert.equal(bridge.vscodeLmStagedOutputsReady(collector), false);
}

async function workerPrepareApplyRecoveryStillClears() {
  const worker = fakeWorker();
  const collector = await shiftedRefusal(worker);
  const sha = worker.currentSha();
  collector.noteRecoverySourceGraph(bodyCall, freshBody(sha, 1, 20));
  const prepareCall = { name: PREPARE, input: { file_path: helper, start_line: 10, end_line: 11 } };
  assert.equal(collector.allowsRecoveryCall(prepareCall), true);
  const prepared = { ok: true, tool: "semantic_edit_prepare", target_id: "recover-target", path: helper,
    start_line: 10, end_line: 11, current_sha256: sha, fragment_sha256: "f".repeat(64) };
  assert.equal(collector.observeSemanticEdit(prepareCall, prepared), false);
  const applyCall = { name: APPLY, input: { target_id: "recover-target", new: twoLines } };
  assert.equal(collector.allowsRecoveryCall(applyCall), true);
  const applied = collector.observeSemanticEdit(applyCall, {
    ok: true, schema_id: "aiworkhub.semantic_edit_apply_receipt.v1", target_id: "recover-target", path: helper,
    before_sha256: sha, after_sha256: "c".repeat(64), preimage_verified: true,
  });
  assert.equal(applied, true);
  assert.equal(collector.recoveryRequiredOutput(), null);
  assert.equal(bridge.vscodeLmStagedOutputsReady(collector), true);
  assert.equal(collector.finalize("recovered through worker prepare/apply").ok, true);
}

async function main() {
  await pendingRecoveryIsNeverReady();
  await postSourceLimitDoesNotForceFinal();
  await freshReadStageClearsRecoveryAndFinalizeSucceeds();
  await recoveryLoopFinalizesAfterFreshRead();
  await staleStageWithoutFreshReadIsStillRefused();
  await workerPrepareApplyRecoveryStillClears();
}

main().then(() => console.log("VS Code LM recovery finalize NF-2026-01539: ok"))
  .catch((error) => { console.error(error); process.exitCode = 1; });
