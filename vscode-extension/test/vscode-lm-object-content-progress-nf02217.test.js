"use strict";

// NF-2026-02217: a non-paged Source Graph reply may carry `content` as an object. It must
// count as evidence (guard and recovery tracking), and a forced-stage mode=file START-END
// read of a declared path must be allowed without loosening the read limits.
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
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

const SG = "aiworkhub_worker_source_graph_query";
const STAGE = "aiworkhub_manager_semantic_edit_stage";
const sha = (seed) => crypto.createHash("sha256").update(String(seed)).digest("hex");
const FILE = "src/app.py";

const bodyPayload = (line = 1) => ({
  matches: [{
    file_path: FILE, source_hash: sha("body"), line_start: line, line_end: line,
    source: `value_${line} = ${line}`, kind: "function", qualname: `app.f${line}`,
  }],
});
const filePayload = (path = FILE, hash = sha(`file:${path}`)) => ({
  matches: [{
    file_path: path, language: "python", status: "indexed", source_hash: hash,
    kind: "file", name: path, qualname: path, line_start: 1, line_end: 1,
  }],
  contexts: [{
    file_path: path,
    file: { file_path: path, language: "python", status: "indexed", source_hash: hash },
    entities: [], edges: [], found: true,
    source_preview: "line_1 = 1\nline_2 = 2\n", source_preview_bytes: 22,
    source_preview_truncated: false,
  }],
  mode: "file", query: path, truncated: false,
});
const reply = (mode, content) => ({
  ok: true, tool: "source_graph", mode, authority_source: "canonical",
  authority_repo: "D:/repo/AIWorkHub", hit_count: 1, content,
});
const call = (mode, query, target) => ({ name: SG, input: { mode, query, target } });

test("an object-shaped body or file reply counts as progress in the guard", () => {
  for (const [mode, payload, query] of [
    ["body", bodyPayload(1), "app.f1"],
    ["file", filePayload(), FILE],
  ]) {
    for (const [shape, content] of [["object", payload], ["JSON string", JSON.stringify(payload)]]) {
      const guard = bridge.createVscodeLmSourceGraphGuard();
      assert.equal(guard.progressRevision(), 0);
      guard.observed(call(mode, query, FILE), reply(mode, content));
      assert.equal(guard.progressRevision(), 1, `${mode} ${shape} content is new evidence`);
      guard.observed(call(mode, query, FILE), reply(mode, content));
      assert.equal(guard.progressRevision(), 1, `${mode} ${shape} replay is not new evidence`);
    }
  }
});

test("a non-JSON string reply still counts as no progress", () => {
  for (const mode of ["body", "file"]) {
    const guard = bridge.createVscodeLmSourceGraphGuard();
    guard.observed(call(mode, FILE, FILE), reply(mode, "bounded graph"));
    assert.equal(guard.progressRevision(), 0, mode);
  }
});

// Drives the collector into recovery for FILE: a line-count-changing stage shifts coordinates
// and a second stage below the shift is refused, which arms noteRecoverySourceGraph.
async function recoveringCollector() {
  const afterHash = sha("after-first-stage");
  const request = {
    requestId: "2".repeat(32), request_kind: "worker", allowedWrites: [FILE], required_outputs: [FILE],
    path_contracts: { [FILE]: { action: "edit", current_sha256: sha("start"), line_count: 20, parent_existed: true } },
  };
  const prepared = new Map();
  const authenticate = async (toolCall) => {
    if (toolCall.name.endsWith("_prepare")) {
      const id = `target-${prepared.size}`;
      prepared.set(id, toolCall.input.file_path);
      return {
        ok: true, tool: "semantic_edit_prepare", target_id: id, path: toolCall.input.file_path,
        current_sha256: sha("start"), fragment_sha256: "c".repeat(64),
        start_line: toolCall.input.start_line, end_line: toolCall.input.end_line,
      };
    }
    return {
      ok: true, schema_id: "aiworkhub.semantic_edit_apply_receipt.v1",
      target_id: toolCall.input.target_id, path: prepared.get(toolCall.input.target_id),
      before_sha256: sha("start"), after_sha256: afterHash,
      idempotency_key: toolCall.input.idempotency_key, preimage_verified: true,
    };
  };
  const collector = bridge.createVscodeLmStagedEditCollector(request);
  const stageInput = (start, end, text) => ({
    operation: "replace_range", file_path: FILE, start_line: start, end_line: end,
    expected_first_line: "before = 0", expected_last_line: "before = 0", new: text,
  });
  const first = await collector.stage(stageInput(1, 1, "x = 1\ny = 2\n"), authenticate);
  assert.equal(first.ok, true, JSON.stringify(first));
  const second = await collector.stage(stageInput(5, 5, "z = 3\n"), authenticate);
  assert.match(String(second.reason), /coordinates_shifted_requires_fresh_worker_pair/);
  assert.deepEqual(collector.recoveryRequiredOutput(), { path: FILE, action: "replace_range", recovery: true });
  return { collector, afterHash };
}

test("noteRecoverySourceGraph sees an object-shaped body reply, and not a non-JSON string", async () => {
  const prepareCall = { name: "aiworkhub_worker_semantic_edit_prepare",
    input: { file_path: FILE, start_line: 1, end_line: 2 } };
  const fresh = (afterHash) => ({ matches: [{
    file_path: FILE, kind: "function", qualname: "app.f", line_start: 1, line_end: 3,
    source: "x = 1\ny = 2\nw = 4", freshness: { state: "fresh", disk_source_hash: afterHash },
  }] });
  for (const shape of ["object", "JSON string"]) {
    const { collector, afterHash } = await recoveringCollector();
    assert.equal(collector.allowsRecoveryCall(prepareCall), false, "no evidence before the read");
    const payload = fresh(afterHash);
    collector.noteRecoverySourceGraph(call("body", "app.f", FILE),
      reply("body", shape === "object" ? payload : JSON.stringify(payload)));
    assert.equal(collector.allowsRecoveryCall(prepareCall), true, `${shape} content is recovery evidence`);
  }
  const { collector } = await recoveringCollector();
  collector.noteRecoverySourceGraph(call("body", "app.f", FILE), reply("body", "bounded graph"));
  assert.equal(collector.allowsRecoveryCall(prepareCall), false, "a non-JSON string is no evidence");
});

// Forced staging, text protocol: first stage once the worker stalls, then run `plan` of reads.
function nf02217MockEditor(request, fallback) {
  const targets = new Map(), hashes = new Map();
  return async (toolCall, requestId, providerCallId) => {
    if (!toolCall.name.startsWith("aiworkhub_worker_semantic_edit_")) return fallback(toolCall, requestId, providerCallId);
    if (toolCall.name.endsWith("_prepare")) {
      const input = toolCall.input, file = input.file_path;
      const current = hashes.get(file) || request.path_contracts[file].current_sha256;
      const target = `mock-target-${targets.size}`;
      targets.set(target, { file, current });
      return { ok: true, tool: "semantic_edit_prepare", target_id: target, path: file,
        current_sha256: current, fragment_sha256: "c".repeat(64),
        start_line: input.start_line, end_line: input.end_line };
    }
    const input = toolCall.input, target = targets.get(input.target_id);
    assert.ok(target, "apply must refer to a real mock prepare");
    const after = sha(input.new);
    hashes.set(target.file, after);
    return { ok: true, tool: "semantic_edit_apply", schema_id: "aiworkhub.semantic_edit_apply_receipt.v1",
      target_id: input.target_id, path: target.file, before_sha256: target.current,
      after_sha256: after, idempotency_key: input.idempotency_key, idempotent_replay: false,
      preimage_verified: true };
  };
}

async function forcedStageReads(reads) {
  const first = "src/first.js", second = "src/second.js", dep = "src/dep.js";
  const outside = "src/outside.js";
  const request = {
    requestId: "7".repeat(32), request_kind: "worker", prompt: "Edit both required files.",
    allowedWrites: [first, second, dep], required_outputs: [first, second],
    path_contracts: {
      [first]: { action: "edit", current_sha256: "a".repeat(64), line_count: 1, parent_existed: true },
      [second]: { action: "edit", current_sha256: "b".repeat(64), line_count: 1, parent_existed: true },
      [dep]: { action: "edit", current_sha256: "c".repeat(64), line_count: 1, parent_existed: true },
    },
    initial_source_graph_result: { ok: true, content: "prefetched graph" },
  };
  const names = { first, second, dep, outside };
  const toolRequest = (name, input) => JSON.stringify({
    schema_id: bridge.constants.VSCODE_LM_TOOL_REQUEST_SCHEMA, name, input,
  });
  const readFile = ({ target, query }) => toolRequest(SG, {
    mode: "file", query: query === undefined ? names[target] : query, target: names[target],
    workflow_stage: "implementation",
  });
  const stage = (path, text) => toolRequest(STAGE, {
    operation: "replace_range", file_path: path, start_line: 1, end_line: 1,
    expected_first_line: "const before = 0;", expected_last_line: "const before = 0;", new: text,
  });
  let discoveryNumber = 0;
  const discover = () => toolRequest(SG, {
    mode: "focus", query: `orientation-${++discoveryNumber}`, workflow_stage: "implementation",
  });
  const idle = () => toolRequest("aiworkhub_worker_session_current_state", { limit: 1 });
  const stall = bridge.constants.VSCODE_LM_STALL_TURNS;

  const executed = [];
  let turns = 0, stagedFirst = false, plan = null;
  const model = {
    capabilities: { toolCalling: false },
    sendRequest: async (messages) => {
      turns += 1;
      const last = messages[messages.length - 1];
      const instruction = last && last.role === "user" ? String(last.content) : "";
      const nextStage = stagedFirst && instruction.includes(`Required output ${second}`);
      let value;
      if (!stagedFirst && turns >= 2) {
        stagedFirst = true;
        value = stage(first, "const first = dep();\n");
      } else {
        if (!plan && nextStage && turns > stall + 2) {
          plan = [...reads.map(readFile), stage(second, "const second = 2;\n")];
        }
        value = plan && plan.length ? plan.shift() : stagedFirst ? idle() : discover();
      }
      return { stream: (async function* stream() { yield { value }; }()) };
    },
  };
  const final = JSON.parse(await bridge.runVscodeLmTextProtocol(
    model, request, undefined, nf02217MockEditor(request, async (toolCall) => {
      if (toolCall.name === SG && toolCall.input.mode === "file") {
        executed.push([toolCall.input.target, toolCall.input.query]);
      }
      return { ok: true, content: "bounded graph" };
    }),
  ));
  assert.deepEqual(final.edits, []);
  return { executed, dep, second, first, outside };
}

test("forced staging allows a START-END file read of a declared path", async () => {
  const { executed, dep } = await forcedStageReads([{ target: "dep", query: "5-9" }]);
  assert.deepEqual(executed, [[dep, "5-9"]]);
});

test("forced staging still allows the whole-path file read", async () => {
  const { executed, dep } = await forcedStageReads([{ target: "dep" }]);
  assert.deepEqual(executed, [[dep, dep]]);
});

test("forced staging refuses a START-END file read of an undeclared path", async () => {
  const { executed } = await forcedStageReads([{ target: "outside", query: "5-9" }]);
  assert.deepEqual(executed, []);
});

for (const query of ["abc", "5-", "-9", "5-9-12", "5,9", "5-9\nx", ""]) {
  test(`forced staging refuses a malformed file query ${JSON.stringify(query)}`, async () => {
    const { executed } = await forcedStageReads([{ target: "dep", query }]);
    assert.deepEqual(executed, []);
  });
}

test("forced-stage read limits are unchanged for ranged reads", async () => {
  const perTarget = await forcedStageReads([
    { target: "dep", query: "1-2" }, { target: "dep", query: "3-4" }, { target: "dep", query: "5-6" },
  ]);
  assert.deepEqual(perTarget.executed, [[perTarget.dep, "1-2"], [perTarget.dep, "3-4"]],
    "a third distinct read of one target is refused");
  const total = await forcedStageReads([
    { target: "dep", query: "1-2" }, { target: "dep", query: "3-4" },
    { target: "second", query: "1-2" }, { target: "second", query: "3-4" },
    { target: "first", query: "1-2" },
  ]);
  assert.equal(total.executed.length, 4, "the fifth distinct read across targets is refused");
});
