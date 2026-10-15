const assert = require("node:assert/strict");
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
const required = "tests/required.js";
const request = {
  requestId: "9".repeat(32), request_kind: "worker", prompt: "Create the required test and its allowed helper.",
  allowedWrites: [helper, required], required_outputs: [required],
  path_contracts: { [helper]: { action: "create" }, [required]: { action: "create" } },
  initial_source_graph_request: { mode: "focus", query: helper },
  initial_source_graph_result: { ok: true, content: "prefetched graph" },
};
const helperInput = { operation: "create", file_path: helper, content: "module.exports = () => true;\n" };
const requiredInput = { operation: "create", file_path: required, content: "const check = (value) => { if (value !== true) throw new Error('check failed'); };\ncheck(true);\n" };

const STAGE = "aiworkhub_manager_semantic_edit_stage";
const FINALIZE = "aiworkhub_manager_semantic_edit_finalize";
const PREPARE = "aiworkhub_worker_semantic_edit_prepare";
const APPLY = "aiworkhub_worker_semantic_edit_apply";
const baseSha = "a".repeat(64);
const twoLines = "const a = 1;\nconst b = 2;";
const anchoredRequest = () => ({
  ...request, allowedWrites: [helper], required_outputs: [helper],
  path_contracts: { [helper]: { action: "edit", current_sha256: baseSha, line_count: 60, parent_existed: true } },
});
const stageAt = (start, end, text, extra = {}) => ({
  operation: "replace_range", file_path: helper, start_line: start, end_line: end,
  expected_first_line: `first of ${start}`, expected_last_line: `last of ${end}`, new: text, ...extra,
});
const relocation = (from, to) => ({
  start_line: to[0], end_line: to[1], anchor_relocated_from: { start_line: from[0], end_line: from[1] },
});

// Mock worker transport: typed replies exercise bridge bookkeeping, not a real worker or HMAC acceptance claim.
function fakeWorker(prepareReply = () => ({})) {
  const calls = [];
  const targets = new Map();
  let sha = baseSha;
  const worker = async (call) => {
    calls.push(call);
    if (call.name === PREPARE) {
      const reply = prepareReply(call.input);
      if (reply.ok === false) return reply;
      const targetId = `anchor-target-${calls.length}`;
      targets.set(targetId, call.input.file_path);
      return { ok: true, tool: "semantic_edit_prepare", target_id: targetId, path: call.input.file_path,
        start_line: call.input.start_line, end_line: call.input.end_line,
        current_sha256: sha, fragment_sha256: "f".repeat(64), ...reply };
    }
    assert.equal(call.name, APPLY);
    const before = sha;
    sha = require("node:crypto").createHash("sha256").update(before + call.input.new).digest("hex");
    return { ok: true, schema_id: "aiworkhub.semantic_edit_apply_receipt.v1", target_id: call.input.target_id,
      path: targets.get(call.input.target_id), before_sha256: before, after_sha256: sha,
      idempotency_key: call.input.idempotency_key, preimage_verified: true };
  };
  worker.calls = calls;
  return worker;
}

function stageToolSchemaAndPromptRequireAnchors() {
  const stageTool = bridge.VSCODE_LM_PRIVATE_TOOLS.find((tool) => tool.name === STAGE);
  const replaceRange = stageTool.inputSchema.oneOf.find((branch) => branch.properties.operation.const === "replace_range");
  for (const key of ["expected_first_line", "expected_last_line"]) {
    assert.equal(replaceRange.properties[key].type, "string", key);
    assert.ok(replaceRange.required.includes(key), `${key} must be required for replace_range`);
  }
  const prompt = bridge.glmTextToolProtocolPrompt("bounded", [helper]);
  const canonical = prompt.split("\n").filter((line) => line.includes('"operation":"replace_range"'));
  assert.ok(canonical.length >= 2, "the native and the text prompt both carry a canonical replace_range request");
  for (const line of canonical) {
    assert.ok(line.includes('"expected_first_line":') && line.includes('"expected_last_line":'), line);
  }
  assert.ok(prompt.includes("semantic_edit_anchor_mismatch"));
}

async function anchorRequired() {
  const worker = fakeWorker();
  for (const anchors of [
    {}, { expected_first_line: "first" }, { expected_last_line: "last" },
    { expected_first_line: 7, expected_last_line: "last" },
    { expected_first_line: "first", expected_last_line: null },
    { expected_first_line: ["first"], expected_last_line: ["last"] },
  ]) {
    for (const authenticate of [undefined, worker]) {
      const collector = bridge.createVscodeLmStagedEditCollector(anchoredRequest());
      const rejected = await collector.stage({ operation: "replace_range", file_path: helper,
        start_line: 3, end_line: 4, new: twoLines, ...anchors }, authenticate);
      assert.equal(rejected.ok, false, JSON.stringify(anchors));
      assert.equal(rejected.reason, "semantic_edit_stage_rejected:anchor_required", JSON.stringify(anchors));
      assert.equal(collector.hasChanges(), false);
    }
  }
  assert.deepEqual(worker.calls, [], "an unanchored stage must never reach the worker");
  const extra = await bridge.createVscodeLmStagedEditCollector(anchoredRequest())
    .stage(stageAt(3, 4, twoLines, { bogus: 1 }));
  assert.equal(extra.reason, `semantic_edit_stage_rejected:stage_payload_extra_fields:${helper}:bogus`,
    "the anchors are stage fields; only the unknown key is extra");
}

async function offlineAnchorsAreAcceptedUnverified() {
  const collector = bridge.createVscodeLmStagedEditCollector(anchoredRequest());
  const staged = await collector.stage(stageAt(2, 3, twoLines, {
    expected_first_line: "text found nowhere in the file", expected_last_line: "",
  }));
  assert.equal(staged.ok, true, JSON.stringify(staged));
  assert.deepEqual([staged.start_line, staged.end_line], [2, 3]);
  assert.equal(Object.hasOwn(staged, "anchor_relocated_from"), false);
  const finished = collector.finalize("offline anchors are carried but never verified");
  assert.equal(finished.ok, true, JSON.stringify(finished));
  assert.deepEqual(finished.__finalEnvelope.edits.map((entry) => entry.ranges), [[
    { start_line: 2, end_line: 3, new: twoLines, preserve_trailing_newline: true },
  ]]);
  assert.equal(JSON.stringify(finished.__finalEnvelope).includes("expected_"), false,
    "anchors are stage-time evidence and never reach the final envelope");
}

async function finalEnvelopeRangesNeedNoAnchors() {
  const worker = fakeWorker();
  const collector = bridge.createVscodeLmStagedEditCollector(anchoredRequest());
  const ingested = await collector.ingestFinalEnvelope({
    schema_id: bridge.constants.VSCODE_LM_EDIT_RESPONSE_SCHEMA, summary: "direct v3 envelope",
    edits: [{ path: helper, current_sha256: baseSha, ranges: [
      { start_line: 20, end_line: 21, new: twoLines, preserve_trailing_newline: true },
    ] }],
    creates: [],
  }, worker);
  assert.equal(ingested.ok, true, JSON.stringify(ingested));
  assert.deepEqual(worker.calls[0].input, { file_path: helper, start_line: 20, end_line: 21 },
    "a final-envelope range carries no anchors, so none are sent to the worker");
}

async function relocatedStageKeepsBookkeepingOnRelocatedCoordinates() {
  const worker = fakeWorker((input) => input.start_line === 10 ? relocation([10, 11], [14, 15]) : {});
  const collector = bridge.createVscodeLmStagedEditCollector(anchoredRequest());
  const plain = await collector.stage(stageAt(30, 30, "const tail = 1;"), worker);
  assert.equal(plain.ok, true, JSON.stringify(plain));
  assert.deepEqual([plain.start_line, plain.end_line], [30, 30]);
  assert.equal(Object.hasOwn(plain, "anchor_relocated_from"), false);

  const moved = await collector.stage(stageAt(10, 11, twoLines), worker);
  assert.equal(moved.ok, true, JSON.stringify(moved));
  assert.deepEqual([moved.start_line, moved.end_line], [14, 15]);
  assert.deepEqual(moved.anchor_relocated_from, { start_line: 10, end_line: 11 });
  assert.equal(moved.mcp_receipt.ok, true);
  assert.deepEqual(worker.calls[2].input, { file_path: helper, start_line: 10, end_line: 11,
    expected_first_line: "first of 10", expected_last_line: "last of 11" });
  assert.deepEqual(worker.calls.map((call) => call.name), [PREPARE, APPLY, PREPARE, APPLY]);

  const settled = worker.calls.length;
  const overlapsRelocated = await collector.stage(stageAt(15, 16, "const c = 3;\nconst d = 4;"), worker);
  assert.equal(overlapsRelocated.reason, `semantic_edit_stage_rejected:range_overlap:${helper}:15:16`,
    "the relocated range, not the requested one, is what is recorded");
  const restagedRequest = await collector.stage(stageAt(10, 11, "const e = 5;\nconst f = 6;"), worker);
  assert.equal(restagedRequest.reason, `semantic_edit_stage_rejected:range_overlap:${helper}:14:15`);
  assert.deepEqual(worker.calls.slice(settled).map((call) => call.name), [PREPARE], "a refused relocation never applies");
  const replay = await collector.stage(stageAt(14, 15, twoLines), worker);
  assert.equal(replay.ok, true, JSON.stringify(replay));
  assert.equal(replay.idempotent_replay, true);
  assert.deepEqual([replay.start_line, replay.end_line], [14, 15]);
  assert.equal(worker.calls.length, settled + 1, "an exact replay of the relocated range does not re-apply");

  const finished = collector.finalize("relocated and plain stages");
  assert.equal(finished.ok, true, JSON.stringify(finished));
  assert.equal(finished.staged_edit_count, 2);
  assert.deepEqual(finished.__finalEnvelope.edits, [], "authenticated applies are not emitted twice");
}

async function relocatedGrowthShiftsFromTheRelocatedStart() {
  const worker = fakeWorker((input) => input.start_line === 10 ? relocation([10, 10], [13, 13]) : {});
  const collector = bridge.createVscodeLmStagedEditCollector(anchoredRequest());
  const grown = await collector.stage(stageAt(10, 10, "const l1 = 1;\nconst l2 = 2;\nconst l3 = 3;"), worker);
  assert.equal(grown.ok, true, JSON.stringify(grown));
  assert.deepEqual([grown.start_line, grown.end_line], [13, 13]);
  const above = await collector.stage(stageAt(11, 12, "const m1 = 1;\nconst m2 = 2;"), worker);
  assert.equal(above.ok, true, `lines above the relocated range keep their coordinates: ${JSON.stringify(above)}`);
  const below = await collector.stage(stageAt(13, 13, "const n1 = 1;"), worker);
  assert.equal(below.ok, false);
  assert.match(below.reason, /coordinates_shifted_requires_fresh_worker_pair/);
  assert.equal(below.corrective, true);
}

async function unprovenRelocationIsRefused() {
  for (const [label, reply] of [
    ["moved without a relocation marker", { start_line: 14, end_line: 15 }],
    ["marker for a different request", relocation([9, 10], [14, 15])],
    ["relocated span changed", relocation([10, 11], [14, 16])],
    ["relocated above line one", relocation([10, 11], [0, 1])],
    ["relocated end line not an integer", { ...relocation([10, 11], [14, 15]), end_line: "15" }],
  ]) {
    const worker = fakeWorker(() => reply);
    const collector = bridge.createVscodeLmStagedEditCollector(anchoredRequest());
    const refused = await collector.stage(stageAt(10, 11, twoLines), worker);
    assert.equal(refused.ok, false, label);
    assert.equal(refused.reason, `semantic_edit_stage_rejected:prepare_identity_mismatch:${helper}`, label);
    assert.deepEqual(worker.calls.map((call) => call.name), [PREPARE], `${label}: apply must not run`);
    assert.equal(collector.hasChanges(), false, label);
  }
}

async function anchorMismatchRefusalReachesTheModelAndRestages() {
  const refusal = {
    ok: false, tool: "semantic_edit_prepare", reason: `semantic_edit_anchor_mismatch:${helper}`,
    actual_first_line: "const real = 0;", actual_last_line: "const real = 0;",
    numbered_context: ["19\tconst before = 1;", "20\tconst real = 1;", "21\tconst real = 2;", "22\tconst after = 3;"],
    candidates: [{ start_line: 20, end_line: 21 }],
    next_call: { name: PREPARE, input: { file_path: helper, start_line: 20, end_line: 21 } },
  };
  const worker = fakeWorker((input) => input.expected_first_line === "const real = 1;" ? {} : refusal);
  const received = [];
  const model = {
    capabilities: { toolCalling: false },
    sendRequest: async (messages) => {
      const turn = received.length;
      received.push(turn === 0 ? null : JSON.parse(messages[messages.length - 1].content).result);
      let step;
      if (turn === 0) {
        step = { name: STAGE, input: stageAt(3, 4, twoLines) };
      } else if (turn === 1) {
        const { candidates, numbered_context: context } = received[1];
        const lineText = (number) => context.find((row) => row.startsWith(`${number}\t`)).slice(`${number}\t`.length);
        const [candidate] = candidates;
        step = { name: STAGE, input: stageAt(candidate.start_line, candidate.end_line, twoLines, {
          expected_first_line: lineText(candidate.start_line), expected_last_line: lineText(candidate.end_line),
        }) };
      } else if (turn === 2) {
        step = { name: FINALIZE, input: { summary: "Restaged from the anchor mismatch candidates." } };
      } else {
        throw new Error("unplanned provider turn");
      }
      const value = JSON.stringify({ schema_id: bridge.constants.VSCODE_LM_TOOL_REQUEST_SCHEMA, ...step });
      return { stream: (async function* () { yield { value }; })() };
    },
  };
  const result = JSON.parse(await bridge.runVscodeLmTextProtocol(model, anchoredRequest(), undefined, worker));
  const refused = received[1];
  assert.equal(refused.ok, false);
  assert.equal(refused.reason, `semantic_edit_stage_rejected:semantic_edit_anchor_mismatch:${helper}`);
  assert.equal(refused.actual_first_line, refusal.actual_first_line);
  assert.equal(refused.actual_last_line, refusal.actual_last_line);
  assert.deepEqual(refused.numbered_context, refusal.numbered_context);
  assert.deepEqual(refused.candidates, refusal.candidates);
  assert.equal(refused.corrective, true);
  assert.equal(Object.hasOwn(refused, "next_call"), false, "the worker's next_call is not forwarded to the model");
  assert.equal(received[2].ok, true, JSON.stringify(received[2]));
  assert.deepEqual([received[2].start_line, received[2].end_line], [20, 21]);
  assert.deepEqual(worker.calls.map((call) => call.name), [PREPARE, PREPARE, APPLY], "the refused stage never applies");
  assert.deepEqual(worker.calls[1].input, { file_path: helper, start_line: 20, end_line: 21,
    expected_first_line: "const real = 1;", expected_last_line: "const real = 2;" });
  assert.deepEqual(result.edits, [], "an applied edit is not emitted twice");
}

async function main() {
  const collector = bridge.createVscodeLmStagedEditCollector(request);
  const optional = await collector.stage(helperInput);
  assert.equal(optional.ok, true, JSON.stringify(optional));
  assert.equal(optional.required_output_count, 1);
  assert.deepEqual(optional.completed_outputs, []);
  assert.deepEqual(optional.missing_outputs, [required]);
  assert.equal(collector.finalize("helper alone cannot satisfy the required test").ok, false);
  assert.equal((await collector.stage(requiredInput)).ok, true);
  const finished = collector.finalize("both outputs staged");
  assert.equal(finished.ok, true);
  assert.deepEqual(finished.completed_outputs, [required]);
  assert.deepEqual(finished.__finalEnvelope.creates.map((entry) => entry.path), [helper, required]);

  for (const denied of [
    { ...request, allowedWrites: [] },
    { ...request, allowedWrites: [required] },
    { ...request, path_contracts: { [required]: { action: "create" } } },
    { ...request, path_contracts: { ...request.path_contracts, [helper]: { action: "edit" } } },
  ]) {
    assert.equal((await bridge.createVscodeLmStagedEditCollector(denied).stage(helperInput)).ok, false);
  }
  for (const content of ["", "TODO", "..."]) {
    const rejected = await bridge.createVscodeLmStagedEditCollector(request).stage({ ...helperInput, content });
    assert.equal(rejected.ok, false, "optional outputs still need substantive content");
    assert.match(rejected.reason, /fidelity_rejected/);
  }

  const optionalEdit = bridge.createVscodeLmStagedEditCollector({
    ...request,
    path_contracts: { ...request.path_contracts, [helper]: {
      action: "edit", current_sha256: "a".repeat(64), line_count: 1, parent_existed: true,
    } },
  });
  const edit = await optionalEdit.stage({ operation: "replace_range", file_path: helper,
    start_line: 1, end_line: 1, expected_first_line: "module.exports = () => true;",
    expected_last_line: "module.exports = () => true;", new: "module.exports = true;" });
  assert.equal(edit.ok, true, JSON.stringify(edit));
  assert.deepEqual(edit.missing_outputs, [required]);
  const overlap = await optionalEdit.stage({ operation: "replace_range", file_path: helper,
    start_line: 1, end_line: 1, expected_first_line: "module.exports = () => true;",
    expected_last_line: "module.exports = () => true;", new: "module.exports = false;" });
  assert.equal(overlap.ok, false);
  assert.match(overlap.reason, /range_conflict/);

  // Exercise the production text transport, not just the collector in isolation.
  // This mocked provider is component evidence, not a live model/MCP acceptance receipt.
  const steps = [
    { name: "aiworkhub_manager_semantic_edit_stage", input: helperInput },
    { name: "aiworkhub_manager_semantic_edit_stage", input: requiredInput },
    // NF-2026-01378: staging the last required output is readiness, not completion.
    { name: "aiworkhub_manager_semantic_edit_finalize", input: { summary: "Staged both outputs." } },
  ];
  let calls = 0;
  const model = {
    capabilities: { toolCalling: false },
    sendRequest: async (messages) => {
      if (calls) {
        const receipt = JSON.parse(messages[messages.length - 1].content);
        assert.equal(receipt.result.ok, true, JSON.stringify(receipt.result));
      }
      const step = steps[calls++];
      assert.ok(step, "the successful path must terminate without a corrective retry");
      const value = JSON.stringify({ schema_id: bridge.constants.VSCODE_LM_TOOL_REQUEST_SCHEMA, ...step });
      return { stream: (async function* () { yield { value }; })() };
    },
  };
  const result = JSON.parse(await bridge.runVscodeLmTextProtocol(model, request, undefined,
    async () => { throw new Error("new-file staging must not fabricate an existing-file MCP apply"); }));
  assert.equal(calls, 3, "the worker's own finalize ends the run");
  assert.deepEqual(result.creates.map((entry) => entry.path), [helper, required]);

  stageToolSchemaAndPromptRequireAnchors();
  await anchorRequired();
  await offlineAnchorsAreAcceptedUnverified();
  await finalEnvelopeRangesNeedNoAnchors();
  await relocatedStageKeepsBookkeepingOnRelocatedCoordinates();
  await relocatedGrowthShiftsFromTheRelocatedStart();
  await unprovenRelocationIsRefused();
  await anchorMismatchRefusalReachesTheModelAndRestages();
}

main().then(() => console.log("VS Code LM allowed optional output staging: ok"))
  .catch((error) => { console.error(error); process.exitCode = 1; });
