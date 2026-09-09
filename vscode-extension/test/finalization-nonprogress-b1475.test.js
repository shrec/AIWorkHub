const assert = require("node:assert");
const test = require("node:test");
const path = require("node:path");
const Module = require("node:module");

const extensionPath = path.resolve(__dirname, "..", "extension.js");
const fakeVscode = {
  workspace: {
    workspaceFolders: [],
    getConfiguration: () => ({ get: (_key, fallback) => fallback }),
  },
  LanguageModelChatToolMode: { Auto: 1, Required: 2 },
  LanguageModelToolResultPart: class LanguageModelToolResultPart {
    constructor(callId, content) {
      this.callId = callId;
      this.content = content;
    }
  },
  LanguageModelChatMessage: {
    User: (content) => ({ role: "user", content }),
    Assistant: (content) => ({ role: "assistant", content }),
  },
};

const originalLoad = Module._load;
Module._load = function patchedLoad(request, parent, isMain) {
  if (request === "vscode") return fakeVscode;
  return originalLoad.call(this, request, parent, isMain);
};
let internals;
try {
  delete require.cache[extensionPath];
  internals = require(extensionPath).__testInternals;
} finally {
  Module._load = originalLoad;
}

function createRequest(paths = ["tests/new.py"]) {
  const pathContracts = {};
  for (const filePath of paths) {
    pathContracts[filePath] = {
      action: "create",
      current_sha256: "",
      line_count: 0,
      parent_existed: false,
    };
  }
  return {
    requestId: "b".repeat(32),
    request_kind: "worker",
    prompt: "create the required regression",
    allowedWrites: ["tests/*.py"],
    required_outputs: paths,
    path_contracts: pathContracts,
    initial_source_graph_request: {
      mode: "focus",
      query: "finalization non-progress regression",
      workflow_stage: "implementation",
    },
    initial_source_graph_result: { ok: true, content: "prefetched graph" },
  };
}

function finalEnvelope(creates = [], summary = "candidate") {
  return JSON.stringify({
    schema_id: internals.constants.VSCODE_LM_EDIT_RESPONSE_SCHEMA,
    summary,
    edits: [],
    creates,
  });
}

function lastUserText(messages) {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (message && message.role === "user" && typeof message.content === "string") {
      return message.content;
    }
  }
  return "";
}

function textResponse(value) {
  return { stream: (async function* stream() { yield { value }; }()) };
}

function rotatingMissingCreateModel(paths, toolCalling) {
  let providerTurns = 0;
  return {
    get providerTurns() { return providerTurns; },
    capabilities: { toolCalling },
    sendRequest: async () => {
      const presentPath = providerTurns === 0 ? null : paths[(providerTurns - 1) % paths.length];
      const creates = presentPath ? [{ path: presentPath, content: `VALUE = ${providerTurns}\n` }] : [];
      providerTurns += 1;
      return textResponse(finalEnvelope(creates, `candidate ${providerTurns}`));
    },
  };
}

test("text protocol stops on the second identical missing-create rejection", async () => {
  const prompts = [];
  let providerTurns = 0;
  const model = {
    capabilities: { toolCalling: false },
    sendRequest: async (messages) => {
      providerTurns += 1;
      prompts.push(lastUserText(messages));
      return textResponse(
        `provider prose turn ${providerTurns}\n${finalEnvelope([], `candidate ${providerTurns}`)}`,
      );
    },
  };

  await assert.rejects(
    internals.runVscodeLmTextProtocol(model, createRequest(), undefined, async () => ({ ok: true })),
    (error) => {
      assert.match(String(error && error.message || error), /vscode_lm_finalization_nonprogress/);
      assert.doesNotMatch(String(error && error.message || error), /vscode_lm_finalization_limit/);
      assert.strictEqual(error.nonprogressReason, "repeated_missing_required_create");
      assert.strictEqual(error.missingCreatePath, "tests/new.py");
      assert.strictEqual(error.missingCreateAction, "v3_create");
      return true;
    },
  );

  assert.strictEqual(providerTurns, 2);
  assert.match(prompts[1], /tests\/new\.py/);
  assert.match(prompts[1], /action v3_create/);
});

test("tool-call protocol stops on the second identical missing-create rejection", async () => {
  const prompts = [];
  let providerTurns = 0;
  const model = {
    capabilities: { toolCalling: true },
    sendRequest: async (messages) => {
      providerTurns += 1;
      prompts.push(lastUserText(messages));
      return textResponse(
        `provider prose turn ${providerTurns}\n${finalEnvelope([], `candidate ${providerTurns}`)}`,
      );
    },
  };

  await assert.rejects(
    internals.runVscodeLmAgent(model, createRequest(), undefined, async () => ({ ok: true })),
    (error) => {
      assert.match(String(error && error.message || error), /vscode_lm_finalization_nonprogress/);
      assert.doesNotMatch(String(error && error.message || error), /vscode_lm_finalization_limit/);
      assert.strictEqual(error.nonprogressReason, "repeated_missing_required_create");
      assert.strictEqual(error.missingCreatePath, "tests/new.py");
      assert.strictEqual(error.missingCreateAction, "v3_create");
      return true;
    },
  );

  assert.strictEqual(providerTurns, 2);
  assert.match(prompts[1], /tests\/new\.py/);
  assert.match(prompts[1], /action v3_create/);
});

test("text protocol rotating missing-create paths still hit the finalization cap", async () => {
  const paths = ["tests/rotating-a.py", "tests/rotating-b.py"];
  const model = rotatingMissingCreateModel(paths, false);

  await assert.rejects(
    internals.runVscodeLmTextProtocol(model, createRequest(paths), undefined, async () => ({ ok: true })),
    (error) => {
      assert.match(String(error && error.message || error), /vscode_lm_finalization_limit/);
      assert.notStrictEqual(error.nonprogressReason, "repeated_missing_required_create");
      return true;
    },
  );

  assert.ok(model.providerTurns > 2);
});

test("tool-call protocol rotating missing-create paths still hit the finalization cap", async () => {
  const paths = ["tests/rotating-a.py", "tests/rotating-b.py"];
  const model = rotatingMissingCreateModel(paths, true);

  await assert.rejects(
    internals.runVscodeLmAgent(model, createRequest(paths), undefined, async () => ({ ok: true })),
    (error) => {
      assert.match(String(error && error.message || error), /vscode_lm_finalization_limit/);
      assert.notStrictEqual(error.nonprogressReason, "repeated_missing_required_create");
      return true;
    },
  );

  assert.ok(model.providerTurns > 2);
});

test("a changed missing-create identity receives a new bounded correction", async () => {
  const firstPath = "tests/a.py";
  const secondPath = "tests/b.py";
  const responses = [
    finalEnvelope(),
    finalEnvelope([{ path: firstPath, content: "A = 1\n" }]),
    finalEnvelope([
      { path: firstPath, content: "A = 1\n" },
      { path: secondPath, content: "B = 1\n" },
    ]),
  ];
  const prompts = [];
  let providerTurns = 0;
  const model = {
    capabilities: { toolCalling: false },
    sendRequest: async (messages) => {
      prompts.push(lastUserText(messages));
      const response = responses[providerTurns];
      providerTurns += 1;
      return textResponse(response);
    },
  };

  const result = JSON.parse(await internals.runVscodeLmTextProtocol(
    model,
    createRequest([firstPath, secondPath]),
    undefined,
    async () => ({ ok: true }),
  ));

  assert.strictEqual(providerTurns, 3);
  assert.match(prompts[1], /tests\/a\.py/);
  assert.match(prompts[2], /tests\/b\.py/);
  assert.match(prompts[1], /action v3_create/);
  assert.match(prompts[2], /action v3_create/);
  assert.deepStrictEqual(result.creates.map((entry) => entry.path), [firstPath, secondPath]);
});
