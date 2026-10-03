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

async function main() {
  let reads = 0;
  let staged = false;
  const model = {
    capabilities: { toolCalling: false },
    sendRequest: async (messages) => {
      let next;
      if (reads < 12) {
        next = { name: "aiworkhub_worker_source_graph_query", input: {
          mode: "body", query: `src/app.js::part${reads++}`, target: "src/app.js",
          workflow_stage: "implementation",
        } };
      } else if (!staged) {
        const receipt = JSON.parse(messages[messages.length - 1].content);
        assert.equal(receipt.name, "aiworkhub_worker_source_graph_query");
        staged = true;
        next = { name: "aiworkhub_manager_semantic_edit_stage", input: {
          operation: "replace_range", file_path: "src/app.js", start_line: 1, end_line: 1,
          new: "const fixed = true;\n",
        } };
      } else {
        const receipt = JSON.parse(messages[messages.length - 1].content);
        assert.equal(receipt.result.ok, true, JSON.stringify(receipt.result));
        next = { name: "aiworkhub_manager_semantic_edit_finalize", input: { summary: "fixed" } };
      }
      const value = JSON.stringify({ schema_id: bridge.constants.VSCODE_LM_TOOL_REQUEST_SCHEMA, ...next });
      return { stream: (async function* () { yield { value }; })() };
    },
  };
  let liveReads = 0;
  const semanticCalls = []; // Mock transport evidence, not a native/HMAC acceptance claim.
  const result = JSON.parse(await bridge.runVscodeLmTextProtocol(model, {
    requestId: "8".repeat(32), request_kind: "worker", prompt: "Fix src/app.js.",
    allowedWrites: ["src/app.js"], required_outputs: [],
    path_contracts: {
      "src/app.js": { action: "edit", current_sha256: "a".repeat(64), line_count: 1, parent_existed: true },
    },
    initial_source_graph_request: { mode: "focus", query: "src/app.js" },
    initial_source_graph_result: { ok: true, content: "prefetched graph" },
  }, undefined, async (call) => {
    if (call.name === "aiworkhub_worker_semantic_edit_prepare") {
      semanticCalls.push(call);
      assert.deepEqual(call.input, { file_path: "src/app.js", start_line: 1, end_line: 1 });
      return { ok: true, target_id: "discovery-fixture-target", path: "src/app.js",
        start_line: 1, end_line: 1, current_sha256: "a".repeat(64), fragment_sha256: "f".repeat(64) };
    }
    if (call.name === "aiworkhub_worker_semantic_edit_apply") {
      semanticCalls.push(call);
      assert.equal(call.input.target_id, "discovery-fixture-target");
      assert.equal(call.input.new, "const fixed = true;\n");
      return { ok: true, schema_id: "aiworkhub.semantic_edit_apply_receipt.v1",
        target_id: call.input.target_id, path: "src/app.js", before_sha256: "a".repeat(64),
        after_sha256: require("node:crypto").createHash("sha256").update(call.input.new).digest("hex"),
        idempotency_key: call.input.idempotency_key, preimage_verified: true };
    }
    assert.equal(call.name, "aiworkhub_worker_source_graph_query");
    liveReads++;
    return { ok: true, content: "bounded source" };
  }));
  assert.equal(liveReads, 12, "the existing discovery allowance stays available");
  assert.equal(semanticCalls.length, 2, "one authenticated prepare/apply pair");
  assert.deepEqual(result.edits, [], "already-applied edit is not emitted twice");

  const input = { operation: "create", file_path: "src/new.js", content: "const ready = true;\n" };
  const request = { allowedWrites: ["src/new.js"], required_outputs: [],
    path_contracts: { "src/new.js": { action: "create" } } };
  assert.equal((await bridge.createVscodeLmStagedEditCollector(request).stage(input)).ok, true);
  for (const denied of [
    { ...request, allowedWrites: [] },
    { ...request, allowedWrites: ["src/other.js"] },
    { ...request, required_outputs: ["src/other.js"] },
  ]) {
    assert.equal((await bridge.createVscodeLmStagedEditCollector(denied).stage(input)).ok, false);
  }
}

main().then(() => console.log("VS Code LM first-stage discovery transition: ok"))
  .catch((error) => { console.error(error); process.exitCode = 1; });
