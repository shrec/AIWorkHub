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
      const result = JSON.parse(await run(model, request(), undefined, async () => ({ ok: true })));
      assert.equal(model.turns, calls.length, "worker must explicitly finish after production staging");
      assert.equal(result.summary, "Complete wired feature");
      assert.equal(result.edits.length, 1);
      assert.equal(result.edits[0].path, "src/app.py");
      assert.equal(result.edits[0].ranges[0].new, "run_feature()");
      assert.equal(result.creates.length, 1);
    });
  }
  test(`${label}: explicit required outputs retain automatic completion`, async () => {
    const model = modelFor(toolCalling, [{ name: stageName, input: create }]);
    const result = JSON.parse(await run(model, request(["tests/new.py"]), undefined, async () => ({ ok: true })));
    assert.equal(model.turns, 1);
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
