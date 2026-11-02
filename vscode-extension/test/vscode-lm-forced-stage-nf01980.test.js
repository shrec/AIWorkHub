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

// NF-2026-01980 (card A): required_outputs [a, b], a still missing, and the model
// stages a valid new-file create of b. The forced-stage selector names a, but b
// is a still-missing required output too: the create is progress, it resets the
// bounded refusal count, and a is still demanded next. Before the fix the run
// kept a's spent corrections and died as vscode_lm_semantic_edit_stage_required.
const alpha = "src/a.js";
const beta = "src/b.js";
const gamma = "src/c.js";
const STAGE = "aiworkhub_manager_semantic_edit_stage";
const PREPARE = "aiworkhub_worker_semantic_edit_prepare";
const APPLY = "aiworkhub_worker_semantic_edit_apply";
const SOURCE_GRAPH = "aiworkhub_worker_source_graph_query";
const baseSha = { [alpha]: "a".repeat(64), [gamma]: "c".repeat(64) };
const betaContent = "const b = 1;\nmodule.exports = { b };\n";

const workerRequest = () => ({
  requestId: "7".repeat(32), request_kind: "worker", prompt: "Edit a, create b.",
  allowedWrites: [alpha, beta, gamma], required_outputs: [alpha, beta],
  path_contracts: {
    [alpha]: { action: "edit", current_sha256: baseSha[alpha], line_count: 60, parent_existed: true },
    [beta]: { action: "create", parent_existed: true },
    [gamma]: { action: "edit", current_sha256: baseSha[gamma], line_count: 60, parent_existed: true },
  },
  initial_source_graph_request: { mode: "focus", query: alpha },
  initial_source_graph_result: { ok: true, content: "prefetched graph" },
});
const anchored = (filePath, start, end) => ({
  file_path: filePath, start_line: start, end_line: end,
  expected_first_line: `first of ${start}`, expected_last_line: `last of ${end}`,
});
const stageAt = (filePath, start, end, text) => ({ operation: "replace_range", ...anchored(filePath, start, end), new: text });
const createB = { operation: "create", file_path: beta, content: betaContent };

// Mock worker transport: typed replies exercise bridge bookkeeping only.
function fakeWorker() {
  const calls = [];
  const targets = new Map();
  const sha = { ...baseSha };
  const worker = async (call) => {
    if (call.name === SOURCE_GRAPH) {
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
  worker.appliesTo = (filePath) => calls.filter((call) => call.name === APPLY &&
    (call.input.file_path === filePath || targets.get(call.input.target_id) === filePath)).length;
  return worker;
}

const lastContent = (messages) => String(messages[messages.length - 1].content);
const toolRequest = (step) => JSON.stringify({ schema_id: bridge.constants.VSCODE_LM_TOOL_REQUEST_SCHEMA, ...step });
const reply = (value) => ({ stream: (async function* () { yield { value }; })() });
const probe = (turn) => ({ name: SOURCE_GRAPH, input: { mode: "focus", query: `unrelated probe ${turn}` } });
// An optional (non-required) output edited by the worker gives the run a change,
// so the stall watchdog arms forced staging with BOTH required outputs missing.
const warmup = { name: APPLY, input: { ...anchored(gamma, 3, 4), new: "const g = 1;\nconst h = 2;", idempotency_key: "k-gamma" } };
const forcedEntry = `Required output ${alpha} is still missing`;

function scriptedModel(forced, seen) {
  const state = { turns: 0, forcedAt: -1 };
  state.model = {
    capabilities: { toolCalling: false },
    sendRequest: async (messages) => {
      const content = lastContent(messages);
      assert.ok(state.turns < 60, "the run terminates boundedly");
      const turn = state.turns++;
      if (turn === 0) return reply(toolRequest(warmup));
      if (state.forcedAt < 0 && content.startsWith(forcedEntry)) state.forcedAt = turn;
      if (state.forcedAt < 0) return reply(toolRequest(probe(turn)));
      seen.push(content);
      const step = forced[turn - state.forcedAt];
      assert.ok(step !== undefined, `no scripted forced step ${turn - state.forcedAt}`);
      return reply(typeof step === "string" ? step : toolRequest(step));
    },
  };
  return state;
}

// The collector lists every still-missing output in required_outputs order.
function collectorListsEveryMissingOutput() {
  const collector = bridge.createVscodeLmStagedEditCollector(workerRequest());
  assert.deepEqual(collector.missingRequired(), [
    { path: alpha, action: "replace_range" }, { path: beta, action: "create" },
  ]);
}

// Card A replay: two refusals spend a's budget, the create of b is accepted as
// progress and resets it, two more refusals are tolerated, then a is staged.
async function createOfLaterOutputIsProgress() {
  const worker = fakeWorker();
  const seen = [];
  const forced = [
    "I will stage the missing output now.",
    "Staging next.",
    { name: STAGE, input: createB },
    "Now the remaining output.",
    "Staging it next.",
    { name: STAGE, input: stageAt(alpha, 10, 11, "const d = 4;\nconst e = 5;") },
  ];
  const state = scriptedModel(forced, seen);
  const result = JSON.parse(await bridge.runVscodeLmTextProtocol(state.model, workerRequest(), undefined, worker));
  assert.ok(state.forcedAt > 0, "forced staging was reached");
  assert.equal(state.turns, state.forcedAt + forced.length, "every forced step ran, then the run finalized");
  // seen[3] is what the model saw right after the create of b.
  const afterCreate = JSON.parse(seen[3]);
  assert.equal(afterCreate.result.ok, true, "the create of b is accepted");
  assert.equal(afterCreate.result.path, beta);
  assert.ok(afterCreate.instruction.includes(forcedEntry), "a is still demanded next");
  assert.ok(!afterCreate.instruction.includes(`${beta} is still missing`));
  assert.deepEqual(result.creates, [{ path: beta, content: betaContent }]);
  assert.deepEqual(result.edits, [], "a and c were applied through the worker");
  assert.equal(worker.appliesTo(alpha), 1);
  assert.equal(worker.appliesTo(gamma), 1);
}

// A rejected stage names the refusal and every exact expected path/action, and
// the terminal failure carries the rejection reason, not only the outcome label.
async function rejectedStageNamesExpectedPathsAndReason() {
  const worker = fakeWorker();
  const seen = [];
  const badCreate = { name: STAGE, input: { operation: "create", file_path: alpha, content: "const a = 1;\n" } };
  const forced = [badCreate, badCreate, badCreate];
  const state = scriptedModel(forced, seen);
  const reason = `semantic_edit_stage_rejected:action_mismatch:${alpha}:create`;
  let failure;
  try {
    await bridge.runVscodeLmTextProtocol(state.model, workerRequest(), undefined, worker);
  } catch (err) {
    failure = err;
  }
  assert.ok(failure, "repeated rejected stages terminate");
  assert.equal(String(failure.message), `vscode_lm_semantic_edit_stage_required:${reason}`);
  assert.equal(failure.rejectionReason, reason);
  assert.equal(failure.missingRequiredOutput, alpha);
  assert.equal(state.turns, state.forcedAt + forced.length, "two corrections, then the third rejection is terminal");
  const correction = JSON.parse(seen[1]);
  assert.equal(correction.result.ok, false);
  assert.ok(correction.instruction.includes(`rejected: ${reason}`), correction.instruction);
  assert.ok(correction.instruction.includes(`${alpha} (operation replace_range, action replace_range)`), correction.instruction);
  assert.ok(correction.instruction.includes(`${beta} (operation create, action v3_create)`), correction.instruction);
  assert.equal(worker.appliesTo(alpha), 0, "a rejected stage never reaches the worker");
}

(async () => {
  collectorListsEveryMissingOutput();
  await createOfLaterOutputIsProgress();
  await rejectedStageNamesExpectedPathsAndReason();
  console.log("vscode-lm forced-stage NF-2026-01980 tests passed");
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
