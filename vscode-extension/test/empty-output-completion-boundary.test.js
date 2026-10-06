const assert = require("node:assert/strict");
const test = require("node:test");
const Module = require("node:module");

class TextPart { constructor(value) { this.value = value; } }
class ToolCallPart {
  constructor(callId, name, input) { Object.assign(this, { callId, name, input }); }
}
const vscode = {
  workspace: { workspaceFolders: [], getConfiguration: () => ({ get: (_key, fallback) => fallback }) },
  LanguageModelTextPart: TextPart,
  LanguageModelToolCallPart: ToolCallPart,
  LanguageModelToolResultPart: class { constructor(callId, content) { Object.assign(this, { callId, content }); } },
  LanguageModelChatToolMode: { Auto: 1, Required: 2 },
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

const stageName = "aiworkhub_manager_semantic_edit_stage";
const finishName = "aiworkhub_manager_semantic_edit_finalize";
const request = (requiredOutputs = []) => ({
  requestId: "c".repeat(32), request_kind: "worker", prompt: "Add tests and wire production",
  allowedWrites: ["tests/new.py", "src/app.py"], required_outputs: requiredOutputs,
  initial_source_graph_result: { ok: true, content: "injected graph" },
  initial_source_graph_request: { mode: "focus", query: "app" },
  path_contracts: {
    "tests/new.py": { action: "create", parent_existed: false, line_count: 0, current_sha256: "" },
    "src/app.py": { action: "edit", parent_existed: true, line_count: 2, current_sha256: "a".repeat(64) },
  },
});
const create = { operation: "create", file_path: "tests/new.py", content: "def test_wiring():\n    assert True\n" };

const edit = { operation: "replace_range", file_path: "src/app.py", start_line: 2, end_line: 2, new: "run_feature()" };
// Request-local transport fixture only: typed receipts exercise bridge wiring,
// not a real worker actor or production HMAC acceptance.
function authenticatedEditor(req, sourceGraph = async () => ({ ok: true })) {
  const semanticCalls = [];
  const handles = new Map();
  const transport = async (call) => {
    if (call.name === "aiworkhub_worker_source_graph_query") return sourceGraph(call);
    semanticCalls.push(call);
    const input = call.input;
    if (call.name === "aiworkhub_worker_semantic_edit_prepare") {
      const targetId = "fixture-target-" + semanticCalls.length;
      handles.set(targetId, input);
      return { ok: true, target_id: targetId, path: input.file_path,
        start_line: input.start_line, end_line: input.end_line,
        current_sha256: req.path_contracts[input.file_path].current_sha256,
        fragment_sha256: "f".repeat(64) };
    }
    assert.equal(call.name, "aiworkhub_worker_semantic_edit_apply");
    const prepared = handles.get(input.target_id);
    assert.ok(prepared, "apply must use this request's prepared handle");
    assert.equal(input.new, edit.new);
    return { ok: true, schema_id: "aiworkhub.semantic_edit_apply_receipt.v1",
      target_id: input.target_id, path: prepared.file_path,
      before_sha256: req.path_contracts[prepared.file_path].current_sha256,
      after_sha256: require("node:crypto").createHash("sha256").update(input.new).digest("hex"),
      idempotency_key: input.idempotency_key, preimage_verified: true };
  };
  transport.semanticCalls = semanticCalls;
  return transport;
}

function modelFor(toolCalling, calls) {
  let turns = 0;
  return {
    capabilities: { toolCalling }, get turns() { return turns; },
    async sendRequest() {
      const call = calls[turns++];
      assert.ok(call, "bridge requested an unplanned provider turn");
      const part = toolCalling
        ? new ToolCallPart(`call-${turns}`, call.name, call.input)
        : new TextPart(JSON.stringify({ schema_id: bridge.constants.VSCODE_LM_TOOL_REQUEST_SCHEMA, ...call }));
      return { stream: (async function* () { yield part; }()) };
    },
  };
}

for (const toolCalling of [false, true]) {
  const label = toolCalling ? "native" : "text";
  const run = toolCalling ? bridge.runVscodeLmAgent : bridge.runVscodeLmTextProtocol;
  for (const lateFirstStage of [false, true]) {
    test(`${label}: empty outputs do not complete a tests-only ${lateFirstStage ? "late" : "early"} stage`, async () => {
      const calls = Array.from({ length: bridge.constants.VSCODE_LM_MAX_POST_SOURCE_TURNS }, (_, index) => ({
        name: "aiworkhub_worker_source_graph_query",
        input: { mode: "body", query: `src/app.py.symbol_${index}`, target: "src/app.py" },
      }));
      calls.splice(lateFirstStage ? calls.length : 0, 0, { name: stageName, input: create });
      calls.push({ name: stageName, input: edit }, { name: finishName, input: { summary: "Complete wired feature" } });
      const model = modelFor(toolCalling, calls);
      const transport = authenticatedEditor(request());
      const result = JSON.parse(await run(model, request(), undefined, transport));
      assert.equal(model.turns, calls.length, "worker must explicitly finish after production staging");
      assert.equal(result.summary, "Complete wired feature");
      assert.equal(transport.semanticCalls.length, 2);
      assert.deepEqual(result.edits, [], "authenticated edit must not be applied twice");
      assert.equal(result.creates.length, 1);
    });
  }
  test(`${label}: explicit required outputs complete once the worker stops staging`, async () => {
    // NF-2026-01378: readiness is not completion; the replayed stage is the stall that finalizes offline.
    const model = modelFor(toolCalling, [{ name: stageName, input: create }, { name: stageName, input: create }]);
    const result = JSON.parse(await run(model, request(["tests/new.py"]), undefined, async () => ({ ok: true })));
    assert.equal(model.turns, 2);
    assert.equal(result.creates[0].path, "tests/new.py");
  });
  test(`${label}: unspecified legacy outputs retain the phase-count boundary`, async () => {
    const calls = [{ name: stageName, input: create }, ...Array.from({
      length: bridge.constants.VSCODE_LM_MAX_POST_SOURCE_TURNS,
    }, (_, index) => ({ name: "aiworkhub_worker_source_graph_query",
      input: { mode: "body", query: `legacy_${index}`, target: "src/app.py" } }))];
    const model = modelFor(toolCalling, calls);
    const legacy = request();
    delete legacy.required_outputs;
    const result = JSON.parse(await run(model, legacy, undefined, async () => ({ ok: true })));
    assert.equal(model.turns, bridge.constants.VSCODE_LM_MAX_POST_SOURCE_TURNS);
    assert.equal(result.creates[0].path, "tests/new.py");
    assert.equal(result.edits.length, 0);
  });
}

test("the existing global provider-turn safety bound is unchanged", () => {
  assert.equal(bridge.constants.VSCODE_LM_MAX_AGENT_TURNS, 24);
});

for (const toolCalling of [false, true]) {
  test(`${toolCalling ? "native" : "text"}: eighteen genuine body reads can stage and explicitly finish`, async () => {
    const calls = Array.from({ length: 18 }, (_, n) => ({
      name: "aiworkhub_worker_source_graph_query",
      input: { mode: "body", query: `app.f${n}`, target: "src/app.py" },
    }));
    calls.push({ name: stageName, input: create }, { name: stageName, input: edit }, { name: finishName, input: { summary: "Verified production edit" } });
    const model = modelFor(toolCalling, calls);
    let reads = 0;
    const run = toolCalling ? bridge.runVscodeLmAgent : bridge.runVscodeLmTextProtocol;
    const transport = authenticatedEditor(request(), async () => ({
      ok: true, tool: "source_graph", mode: "body", authority_source: "canonical",
      authority_repo: "D:/Dev/AIWorkHub", hit_count: 1,
      content: JSON.stringify({ matches: [{
        file_path: "src/app.py", source_hash: "a".repeat(64), line_start: ++reads,
        line_end: reads, source: `def f${reads}(): pass`, qualname: `app.f${reads}`,
      }] }),
    }));
    const result = JSON.parse(await run(model, request(), undefined, transport));
    assert.equal(reads, 18);
    assert.equal(model.turns, 21);
    assert.equal(transport.semanticCalls.length, 2);
    assert.deepEqual(result.edits, [], "authenticated edit must not be applied twice");
    assert.equal(result.summary, "Verified production edit");
  });
}

const crypto = require("node:crypto");
const hash = (bytes) => crypto.createHash("sha256").update(bytes).digest("hex");
for (const toolCalling of [false, true]) for (const withTarget of [true, false]) {
  test(`${toolCalling ? "native" : "text"}: eighteen verified base64 body pages reach production staging${withTarget ? "" : " without target"}`, async () => {
    const bytes = Buffer.from(JSON.stringify({ matches: [{
      file_path: "src/app.py", line_start: 1, line_end: 2,
      source_hash: "a".repeat(64), source: Array.from({ length: 100 }, (_, n) => `# ქართული ${n}\n`).join(""),
    }] }));
    const size = Math.ceil(bytes.length / 18);
    const contentHash = hash(bytes);
    // Test-only signer: production cursor authentication remains in the backend.
    const cursor = (page) => {
      const fields = { content_sha256: contentHash, page_index: page,
        schema_id: "aiworkhub.task_mcp.source_graph_continuation.v1", store_id: "test-store" };
      const hmac_sha256 = crypto.createHmac("sha256", "test-only-key")
        .update(JSON.stringify(fields)).digest("hex");
      const token = { ...fields, hmac_sha256 };
      return Buffer.from(JSON.stringify(token, Object.keys(token).sort()))
        .toString("base64").replace(/\+/g, "-").replace(/\//g, "_");
    };
    const calls = Array.from({ length: 18 }, (_, page) => ({
      name: "aiworkhub_worker_source_graph_query",
      input: { mode: "body", query: "src/aiworkhub/core.py.reroute_launch_identity", ...(withTarget ? { target: "src/app.py" } : {}),
        ...(page ? { continuation_cursor: cursor(page) } : {}) },
    }));
    calls.push({ name: stageName, input: create }, { name: stageName, input: edit },
      { name: finishName, input: { summary: "Paged body completed" } });
    let reads = 0;
    const model = modelFor(toolCalling, calls);
    const run = toolCalling ? bridge.runVscodeLmAgent : bridge.runVscodeLmTextProtocol;
    const transport = authenticatedEditor(request(), async () => {
      const page = reads++;
      const chunk = bytes.subarray(page * size, (page + 1) * size);
      return { ok: true, tool: "source_graph", mode: "body", authority_source: "canonical",
        authority_repo: "D:/Dev/AIWorkHub", hit_count: 1, target: withTarget ? "src/app.py" : null,
        content_encoding: "base64", content: chunk.toString("base64"),
        content_sha256: contentHash, page_sha256: hash(chunk), page_index: page,
        page_count: 18, bytes: chunk.length, full_bytes: bytes.length,
        continuation_cursor: page < 17 ? cursor(page + 1) : null };
    });
    const result = JSON.parse(await run(model, request(), undefined, transport));
    assert.equal(reads, 18);
    assert.equal(model.turns, 21);
    assert.equal(transport.semanticCalls.length, 2);
    assert.deepEqual(result.edits, [], "authenticated edit must not be applied twice");
    assert.equal(result.summary, "Paged body completed");
  });
}
