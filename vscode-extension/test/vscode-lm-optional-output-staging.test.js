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
    start_line: 1, end_line: 1, new: "module.exports = true;" });
  assert.equal(edit.ok, true, JSON.stringify(edit));
  assert.deepEqual(edit.missing_outputs, [required]);
  const overlap = await optionalEdit.stage({ operation: "replace_range", file_path: helper,
    start_line: 1, end_line: 1, new: "module.exports = false;" });
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
}

main().then(() => console.log("VS Code LM allowed optional output staging: ok"))
  .catch((error) => { console.error(error); process.exitCode = 1; });
