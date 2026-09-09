// NF651: bounded corrective stage for missing/empty required creates in the
// final envelope, on both the text-protocol and native VS Code LM paths.
const assert = require("assert");
const path = require("path");
const Module = require("module");

const extensionPath = path.resolve(__dirname, "..", "extension.js");
const fakeVscode = {
  workspace: { workspaceFolders: [], getConfiguration: () => ({ get: (_key, fallback) => fallback }) },
  LanguageModelChatToolMode: { Auto: 1, Required: 2 },
  LanguageModelToolResultPart: class LanguageModelToolResultPart {
    constructor(callId, content) { this.callId = callId; this.content = content; }
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

const EDIT_SCHEMA = internals.constants.VSCODE_LM_EDIT_RESPONSE_SCHEMA;
const TOOL_REQUEST_SCHEMA = internals.constants.VSCODE_LM_TOOL_REQUEST_SCHEMA;
const invokeToolOk = async () => ({ ok: true, content: "graph" });

const sourceGraphAckRequest = JSON.stringify({
  schema_id: TOOL_REQUEST_SCHEMA,
  name: "aiworkhub_worker_source_graph_query",
  input: { mode: "focus", query: "model", budget: 48 },
});

// Text-protocol model: every turn's response is one queued JSON string.
function textModel(envelopeQueue) {
  const queue = [sourceGraphAckRequest, ...envelopeQueue];
  return {
    capabilities: { toolCalling: false },
    sendRequest: async () => ({
      stream: (async function* stream() { yield { value: queue.shift() }; }()),
    }),
  };
}

// Native-protocol model: turn 0 is a real tool call; every following turn
// emits its queued final-envelope text directly (no tool call).
function nativeModel(envelopeQueue) {
  const turns = [
    [{ callId: "sg-1", name: "aiworkhub_worker_source_graph_query", input: { mode: "focus", query: "model", budget: 48 } }],
    ...envelopeQueue.map((text) => [{ value: text }]),
  ];
  return {
    capabilities: { toolCalling: true },
    sendRequest: async () => {
      const parts = turns.shift();
      return { stream: (async function* stream() { for (const part of parts) yield part; }()) };
    },
  };
}

async function textProtocolSuccessfulCorrection() {
  const contract = { "out/created.js": { action: "create", current_sha256: "", line_count: 0, parent_existed: false } };
  const missing = JSON.stringify({ schema_id: EDIT_SCHEMA, summary: "missing create", edits: [], creates: [] });
  const corrected = JSON.stringify({
    schema_id: EDIT_SCHEMA, summary: "corrected", edits: [],
    creates: [{ path: "out/created.js", content: "export {};\n" }],
  });
  const result = await internals.runVscodeLmTextProtocol(
    textModel([missing, corrected]),
    { request_kind: "worker", prompt: "bounded", allowedWrites: ["out/created.js"], path_contracts: contract },
    undefined,
    invokeToolOk,
  );
  assert.strictEqual(result, corrected);
}

async function nativeProtocolSuccessfulCorrection() {
  const contract = { "out/created-native.js": { action: "create", current_sha256: "", line_count: 0, parent_existed: false } };
  const missing = JSON.stringify({ schema_id: EDIT_SCHEMA, summary: "missing create native", edits: [], creates: [] });
  const corrected = JSON.stringify({
    schema_id: EDIT_SCHEMA, summary: "corrected native", edits: [],
    creates: [{ path: "out/created-native.js", content: "export {};\n" }],
  });
  const result = await internals.runVscodeLmAgent(
    nativeModel([missing, corrected]),
    {
      requestId: "1".repeat(32), request_kind: "worker", prompt: "bounded",
      allowedWrites: ["out/created-native.js"], path_contracts: contract,
    },
    undefined,
    invokeToolOk,
  );
  assert.strictEqual(result, corrected);
}

async function textProtocolUnchangedOutputTerminates() {
  const contract = { "out/unchanged.js": { action: "create", current_sha256: "", line_count: 0, parent_existed: false } };
  const missing = JSON.stringify({ schema_id: EDIT_SCHEMA, summary: "missing create", edits: [], creates: [] });
  await assert.rejects(
    internals.runVscodeLmTextProtocol(
      textModel([missing, missing]),
      { request_kind: "worker", prompt: "bounded", allowedWrites: ["out/unchanged.js"], path_contracts: contract },
      undefined,
      invokeToolOk,
    ),
    (err) => {
      assert.match(String(err.message || err), /vscode_lm_finalization_nonprogress/);
      assert.strictEqual(err.missingCreatePath, "out/unchanged.js");
      assert.strictEqual(err.missingCreateAction, "v3_create");
      assert.ok(err.protocolTrace.some((entry) => /missing_required_create/.test(entry.outcome || "")));
      return true;
    },
  );
}

async function backslashPathNormalizationTerminates() {
  // The contract key uses a Windows-style backslash path; the envelope must
  // still be judged against the SAME normalized identity, and the reported
  // missing path in the terminal outcome must be the normalized (forward
  // slash) form regardless of the backslash used in path_contracts.
  const contract = { "tests\\new.py": { action: "create", current_sha256: "", line_count: 0, parent_existed: false } };
  const missing = JSON.stringify({ schema_id: EDIT_SCHEMA, summary: "missing v3", edits: [], creates: [] });
  await assert.rejects(
    internals.runVscodeLmTextProtocol(
      textModel([missing, missing]),
      { request_kind: "worker", prompt: "bounded", allowedWrites: ["tests/new.py"], path_contracts: contract },
      undefined,
      invokeToolOk,
    ),
    (err) => {
      assert.match(String(err.message || err), /vscode_lm_finalization_nonprogress/);
      assert.strictEqual(err.missingCreatePath, "tests/new.py");
      assert.strictEqual(err.missingCreateAction, "v3_create");
      return true;
    },
  );

  // The corrective stage must also succeed when the repaired create uses the
  // forward-slash form for a backslash-declared contract path.
  const repaired = JSON.stringify({
    schema_id: EDIT_SCHEMA, summary: "repaired v3", edits: [],
    creates: [{ path: "tests/new.py", content: "VALUE = 1\n" }],
  });
  const result = await internals.runVscodeLmTextProtocol(
    textModel([missing, repaired]),
    { request_kind: "worker", prompt: "bounded", allowedWrites: ["tests/new.py"], path_contracts: contract },
    undefined,
    invokeToolOk,
  );
  assert.strictEqual(result, repaired);
}

async function nativeProtocolMultipleMissingCreatesSubsetRepaired() {
  // Two required creates are missing on the first attempt; the bounded
  // corrective stage repairs only one of them. The changed missing identity
  // gets one correction of its own, then the repeated identity terminates.
  const contract = {
    "multi/a.js": { action: "create", current_sha256: "", line_count: 0, parent_existed: false },
    "multi/b.js": { action: "create", current_sha256: "", line_count: 0, parent_existed: false },
  };
  const bothMissing = JSON.stringify({ schema_id: EDIT_SCHEMA, summary: "both missing", edits: [], creates: [] });
  const subsetRepaired = JSON.stringify({
    schema_id: EDIT_SCHEMA, summary: "subset repaired", edits: [],
    creates: [{ path: "multi/a.js", content: "export const a = 1;\n" }],
  });
  await assert.rejects(
    internals.runVscodeLmAgent(
      nativeModel([bothMissing, subsetRepaired, subsetRepaired]),
      {
        requestId: "2".repeat(32), request_kind: "worker", prompt: "bounded",
        allowedWrites: ["multi/*.js"], path_contracts: contract,
      },
      undefined,
      invokeToolOk,
    ),
    (err) => {
      assert.match(String(err.message || err), /vscode_lm_finalization_nonprogress/);
      assert.strictEqual(err.missingCreatePath, "multi/b.js");
      assert.strictEqual(err.missingCreateAction, "v3_create");
      return true;
    },
  );
}

async function forbiddenOutputStillRejectedGenerically() {
  // A create for a path outside allowed_writes must keep failing with the
  // pre-existing final_path_not_allowed check, never misclassified as (and
  // never consuming the bounded budget of) a missing-required-create retry.
  const contract = { "out/allowed.js": { action: "create", current_sha256: "", line_count: 0, parent_existed: false } };
  const forbidden = JSON.stringify({
    schema_id: EDIT_SCHEMA, summary: "forbidden extra create", edits: [],
    creates: [
      { path: "out/allowed.js", content: "export {};\n" },
      { path: "forbidden/evil.js", content: "export const evil = true;\n" },
    ],
  });
  const corrected = JSON.stringify({
    schema_id: EDIT_SCHEMA, summary: "dropped forbidden create", edits: [],
    creates: [{ path: "out/allowed.js", content: "export {};\n" }],
  });
  const result = await internals.runVscodeLmTextProtocol(
    textModel([forbidden, forbidden, corrected]),
    { request_kind: "worker", prompt: "bounded", allowedWrites: ["out/allowed.js"], path_contracts: contract },
    undefined,
    invokeToolOk,
  );
  assert.strictEqual(result, corrected);
}

async function uncontractedEmptyCreateDoesNotConsumeRequiredCreateBudget() {
  // An allowed-writes glob admits a create for a path that has NO path_contract
  // entry at all. If that create is empty, per-item fidelity still rejects it
  // with the same empty_required_create shape used for genuine required
  // creates -- but since the path is not a contract-required create, this must
  // be treated as a generic rejection, never the bounded required-create
  // correction. Proven by then hitting a REAL missing required create and
  // still getting its one bounded corrective retry (three turns total).
  const contract = { "out/created.js": { action: "create", current_sha256: "", line_count: 0, parent_existed: false } };
  const emptyUncontracted = JSON.stringify({
    schema_id: EDIT_SCHEMA, summary: "uncontracted empty create", edits: [],
    creates: [{ path: "out/uncontracted.js", content: "" }],
  });
  const stillMissingRequired = JSON.stringify({ schema_id: EDIT_SCHEMA, summary: "still missing required", edits: [], creates: [] });
  const corrected = JSON.stringify({
    schema_id: EDIT_SCHEMA, summary: "corrected", edits: [],
    creates: [{ path: "out/created.js", content: "export {};\n" }],
  });
  const result = await internals.runVscodeLmTextProtocol(
    textModel([emptyUncontracted, stillMissingRequired, corrected]),
    {
      request_kind: "worker", prompt: "bounded",
      allowedWrites: ["out/created.js", "out/uncontracted.js"], path_contracts: contract,
    },
    undefined,
    invokeToolOk,
  );
  assert.strictEqual(result, corrected);
}

async function main() {
  await textProtocolSuccessfulCorrection();
  await nativeProtocolSuccessfulCorrection();
  await textProtocolUnchangedOutputTerminates();
  await backslashPathNormalizationTerminates();
  await nativeProtocolMultipleMissingCreatesSubsetRepaired();
  await forbiddenOutputStillRejectedGenerically();
  await uncontractedEmptyCreateDoesNotConsumeRequiredCreateBudget();
}

main().then(() => {
  console.log("NF651 VS Code LM finalization missing-create: ok");
}).catch((err) => {
  console.error(err);
  process.exitCode = 1;
});
