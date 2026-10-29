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

// NF-2026-01605: a worker that edits one required output through the worker
// prepare/apply tools must be credited for it, or forced staging re-demands that
// output, every stage is refused, and the run dies with zero changes.
const alpha = "src/alpha.js";
const beta = "src/beta.js";
const STAGE = "aiworkhub_manager_semantic_edit_stage";
const PREPARE = "aiworkhub_worker_semantic_edit_prepare";
const APPLY = "aiworkhub_worker_semantic_edit_apply";
const SOURCE_GRAPH = "aiworkhub_worker_source_graph_query";
const baseSha = { [alpha]: "a".repeat(64), [beta]: "b".repeat(64) };
const threeLines = "const a = 1;\nconst b = 2;\nconst c = 3;";
const twoLines = "const d = 4;\nconst e = 5;";
const oneLine = "const z = 0;";

const workerRequest = () => ({
  requestId: "5".repeat(32), request_kind: "worker", prompt: "Edit both helpers.",
  allowedWrites: [alpha, beta], required_outputs: [alpha, beta],
  path_contracts: {
    [alpha]: { action: "edit", current_sha256: baseSha[alpha], line_count: 60, parent_existed: true },
    [beta]: { action: "edit", current_sha256: baseSha[beta], line_count: 60, parent_existed: true },
  },
  initial_source_graph_request: { mode: "focus", query: alpha },
  initial_source_graph_result: { ok: true, content: "prefetched graph" },
});
const anchored = (filePath, start, end) => ({
  file_path: filePath, start_line: start, end_line: end,
  expected_first_line: `first of ${start}`, expected_last_line: `last of ${end}`,
});
const stageAt = (filePath, start, end, text) => ({ operation: "replace_range", ...anchored(filePath, start, end), new: text });

// Mock worker transport: typed replies exercise bridge bookkeeping, not a real worker or HMAC acceptance claim.
// Like the real worker, a prepare reports the file's CURRENT hash, so a stage bound to the
// launch hash of a file the worker already changed is refused.
function fakeWorker() {
  const calls = [];
  const targets = new Map();
  const sha = { ...baseSha };
  const worker = async (call) => {
    if (call.name === SOURCE_GRAPH) {
      if (call.input.mode === "body") return freshBody(call.input.target, sha[call.input.target], 1, 20);
      return { ok: true, tool: "source_graph", mode: call.input.mode, content: JSON.stringify({ matches: [] }) };
    }
    calls.push(call);
    if (call.name === PREPARE) {
      const targetId = `target-${calls.length}`;
      targets.set(targetId, call.input.file_path);
      return { ok: true, tool: "semantic_edit_prepare", target_id: targetId, path: call.input.file_path,
        start_line: call.input.start_line, end_line: call.input.end_line,
        current_sha256: sha[call.input.file_path], fragment_sha256: "f".repeat(64) };
    }
    assert.equal(call.name, APPLY);
    const oneShot = !call.input.target_id;
    const targetId = oneShot ? `bound-${calls.length}` : call.input.target_id;
    const filePath = oneShot ? call.input.file_path : targets.get(targetId);
    const before = sha[filePath];
    sha[filePath] = crypto.createHash("sha256").update(before + call.input.new).digest("hex");
    return { ok: true, schema_id: "aiworkhub.semantic_edit_apply_receipt.v1", target_id: targetId,
      path: filePath, before_sha256: before, after_sha256: sha[filePath],
      idempotency_key: call.input.idempotency_key, preimage_verified: true,
      ...(oneShot ? { mode: "one_shot" } : {}) };
  };
  worker.calls = calls;
  worker.lastTarget = () => [...targets.keys()].pop();
  worker.appliesTo = (filePath) => calls.filter((call) => call.name === APPLY &&
    (call.input.file_path === filePath || targets.get(call.input.target_id) === filePath)).length;
  return worker;
}

function freshBody(filePath, hash, lineStart, lineEnd) {
  return {
    ok: true, tool: "source_graph", mode: "body", authority_source: "canonical", authority_repo: "repo",
    content: JSON.stringify({ matches: [{
      file_path: filePath, line_start: lineStart, line_end: lineEnd,
      source: Array.from({ length: lineEnd - lineStart + 1 }, (_, index) => `line ${lineStart + index}`).join("\n"),
      freshness: { state: "fresh", disk_source_hash: hash },
    }] }),
  };
}

const lastContent = (messages) => String(messages[messages.length - 1].content);
const toolRequest = (step) => JSON.stringify({ schema_id: bridge.constants.VSCODE_LM_TOOL_REQUEST_SCHEMA, ...step });
const reply = (value) => ({ stream: (async function* () { yield { value }; })() });
const probe = (turn) => ({ name: SOURCE_GRAPH, input: { mode: "focus", query: `unrelated probe ${turn}` } });

// The collector itself: a one-call apply (no target_id) credits its output, and
// only a receipt that matches the requested path does.
async function oneShotApplyCreditsTheOutput() {
  const collector = bridge.createVscodeLmStagedEditCollector(workerRequest());
  const worker = fakeWorker();
  const call = { name: APPLY, input: { ...anchored(alpha, 3, 4), new: threeLines, idempotency_key: "k-alpha" } };
  const receipt = await worker(call);
  assert.equal(collector.observeSemanticEdit(call, { ...receipt, path: beta }), false,
    "a receipt for another path never credits the requested output");
  assert.equal(collector.nextMissingRequired().path, alpha);
  assert.equal(collector.observeSemanticEdit(call, receipt), true);
  assert.equal(collector.hasChanges(), true);
  assert.deepEqual(collector.nextMissingRequired(), { path: beta, action: "replace_range" });
}

// The measured trap: a one-call worker apply on output 1, a stage on output 2,
// then a stall. Before the fix output 1 stayed "missing", forced staging
// re-demanded it, every stage was refused and the run threw
// vscode_lm_semantic_edit_stage_required with nothing kept.
async function workerPairEditCountsTowardReadiness() {
  const worker = fakeWorker();
  const steps = [
    { name: APPLY, input: { ...anchored(alpha, 3, 4), new: threeLines, idempotency_key: "k-alpha" } },
    { name: STAGE, input: stageAt(beta, 5, 5, oneLine) },
  ];
  const seen = [];
  let turns = 0;
  const model = {
    capabilities: { toolCalling: false },
    sendRequest: async (messages) => {
      const content = lastContent(messages);
      seen.push(content);
      assert.ok(turns < 40, "the run terminates boundedly");
      const turn = turns++;
      if (turn < steps.length) return reply(toolRequest(steps[turn]));
      // A model told its edited output is still missing re-stages it at launch coordinates.
      if (content.includes(`${alpha} is still missing`)) {
        return reply(toolRequest({ name: STAGE, input: stageAt(alpha, 10, 11, twoLines) }));
      }
      return reply(toolRequest(probe(turn)));
    },
  };
  const result = JSON.parse(await bridge.runVscodeLmTextProtocol(model, workerRequest(), undefined, worker));
  assert.deepEqual(result.edits, [], "both outputs were already applied by the worker");
  assert.equal(worker.appliesTo(alpha), 1);
  assert.equal(worker.appliesTo(beta), 1);
  assert.ok(seen.every((content) => !content.includes(`${alpha} is still missing`)),
    "an output the worker already edited is never demanded again");
  assert.ok(turns <= 4, `finalized right after the stall, turns=${turns}`);
}

// Forced staging, its corrections already spent, and a stage on the file the
// worker itself changed is refused for shifted coordinates: that re-grounds
// (fresh body read, re-stage on the post-apply file) instead of throwing.
async function shiftedRefusalDuringForcedStagingRegrounds() {
  const worker = fakeWorker();
  const before = [
    () => ({ name: PREPARE, input: anchored(alpha, 3, 4) }),
    () => ({ name: APPLY, input: { target_id: worker.lastTarget(), new: threeLines, idempotency_key: "k-pair" } }),
  ];
  const forced = [
    "I will stage the missing output now.",
    "Staging next.",
    { name: STAGE, input: stageAt(alpha, 10, 11, twoLines) },
    { name: SOURCE_GRAPH, input: { mode: "body", target: alpha, query: "alpha helper" } },
    { name: STAGE, input: stageAt(alpha, 12, 13, twoLines) },
    { name: STAGE, input: stageAt(beta, 5, 5, oneLine) },
  ];
  let turns = 0;
  let forcedAt = -1;
  const model = {
    capabilities: { toolCalling: false },
    sendRequest: async (messages) => {
      const content = lastContent(messages);
      assert.ok(turns < 40, "the run terminates boundedly");
      const turn = turns++;
      if (turn < before.length) return reply(toolRequest(before[turn]()));
      if (forcedAt < 0 && content.startsWith(`Required output ${beta} is still missing`)) forcedAt = turn;
      if (forcedAt < 0) return reply(toolRequest(probe(turn)));
      const step = forced[turn - forcedAt];
      assert.ok(step, "the re-grounded run finalizes after its last stage");
      return reply(typeof step === "string" ? step : toolRequest(step));
    },
  };
  const result = JSON.parse(await bridge.runVscodeLmTextProtocol(model, workerRequest(), undefined, worker));
  assert.ok(forcedAt > 0, "forced staging was reached");
  assert.equal(turns, forcedAt + forced.length, "every forced step ran, then the run finalized");
  assert.deepEqual(result.edits, []);
  assert.equal(worker.appliesTo(alpha), 2, "the re-stage landed on the post-apply file");
  assert.equal(worker.appliesTo(beta), 1);
  assert.equal(worker.calls.filter((call) => call.name === APPLY && call.input.new === twoLines).length, 1,
    "the shifted stage itself never reached the worker");
}

(async () => {
  await oneShotApplyCreditsTheOutput();
  await workerPairEditCountsTowardReadiness();
  await shiftedRefusalDuringForcedStagingRegrounds();
  console.log("vscode-lm forced-stage NF-2026-01605 tests passed");
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
