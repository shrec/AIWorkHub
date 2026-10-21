"use strict";

const assert = require("assert");
const path = require("path");
const Module = require("module");

const extensionPath = path.resolve(__dirname, "..", "extension.js");
const fakeVscode = {
  LanguageModelChatMessage: {
    User: (content) => ({ role: "user", content }),
    Assistant: (content) => ({ role: "assistant", content }),
  },
  LanguageModelChatToolMode: { Auto: 1, Required: 2 },
  LanguageModelToolResultPart: class {
    constructor(callId, content) {
      this.callId = callId;
      this.content = content;
    }
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

const SG = "aiworkhub_worker_source_graph_query";
const SUBMIT = "aiworkhub_worker_quality_review_submit";
const SUBMITTED = { ok: true, durable: true, submission_id: "b".repeat(64) };
const TRANSPORTS = ["native", "text"];

// Turns 1..sgTurns read Source Graph; every later turn calls submit.
function buildModel(transport, sgTurns) {
  let turn = 0;
  return {
    capabilities: { toolCalling: transport === "native" },
    sendRequest: async () => {
      turn += 1;
      const n = turn;
      const call = n <= sgTurns
        ? { name: SG, input: { mode: "file", target: "src/a.py", query: `${n * 10}-${n * 10 + 5}` } }
        : { name: SUBMIT, input: { summary: "ok" } };
      const part = transport === "native"
        ? { callId: `call-${n}`, ...call }
        : {
            value: JSON.stringify({
              schema_id: internals.constants.VSCODE_LM_TOOL_REQUEST_SCHEMA,
              ...call,
            }),
          };
      return {
        stream: (async function* stream() {
          yield part;
        })(),
      };
    },
  };
}

function req() {
  return {
    requestId: "0123456789abcdef0123456789abcdef",
    request_kind: "quality_review",
    prompt: "Review.",
    allowedWrites: [],
  };
}

// newRows(sgIndex) is how many never-seen source rows the sgIndex-th read returns.
function buildHandler(newRows, submitResults) {
  const counts = { sg: 0, submit: 0 };
  let rowId = 0;
  const handler = async (call) => {
    if (call.name === SUBMIT) {
      counts.submit += 1;
      return submitResults.length > 0 ? submitResults.shift() : SUBMITTED;
    }
    counts.sg += 1;
    const rows = newRows(counts.sg);
    if (rows === 0) return { ok: true, content: "graph" };
    const matches = [];
    for (let i = 0; i < rows; i += 1) {
      rowId += 1;
      matches.push({
        file_path: "src/a.py",
        line_start: rowId * 10,
        line_end: rowId * 10 + 5,
        qualname: "src/a.py",
        kind: "file",
        signature: `range-${rowId}`,
      });
    }
    return {
      ok: true,
      tool: "source_graph",
      mode: "file",
      authority_source: "canonical",
      authority_repo: "D:/repo",
      content: JSON.stringify({ matches }),
    };
  };
  return { handler, counts };
}

async function distinctEvidenceExtendsSubmitBudget(transport) {
  const { handler, counts } = buildHandler(() => 1, []);
  await internals.runVscodeLmAgent(buildModel(transport, 6), req(), undefined, handler);
  assert.strictEqual(counts.sg, 6, `${transport}: all six distinct source reads must execute`);
  assert.strictEqual(counts.submit, 1, `${transport}: the reviewer must still reach its submit`);
}

async function noEvidenceKeepsOldBudget(transport) {
  const { handler, counts } = buildHandler(() => 0, []);
  await assert.rejects(
    internals.runVscodeLmAgent(buildModel(transport, 20), req(), undefined, handler),
    /vscode_lm_quality_review_submit_required/,
  );
  assert.strictEqual(counts.sg, 3, `${transport}: old 3-turn budget must apply without new evidence`);
  assert.strictEqual(counts.submit, 0);
}

async function multiRowReplyEarnsOnlyOneTurn(transport) {
  const { handler, counts } = buildHandler((index) => (index === 1 ? 9 : 0), []);
  await assert.rejects(
    internals.runVscodeLmAgent(buildModel(transport, 20), req(), undefined, handler),
    /vscode_lm_quality_review_submit_required/,
  );
  assert.strictEqual(counts.sg, 4, `${transport}: nine rows in one reply earn one extra turn, not nine`);
  assert.strictEqual(counts.submit, 0);
}

async function maxExtensionKeepsTransientSubmitRetry(transport) {
  const { handler, counts } = buildHandler(() => 1, [{ ok: false, error: "transient" }]);
  await internals.runVscodeLmAgent(buildModel(transport, 10), req(), undefined, handler);
  assert.strictEqual(counts.sg, 10, `${transport}: all ten distinct source reads must execute`);
  assert.strictEqual(counts.submit, 2, `${transport}: a transient submit failure must still be retried`);
}

async function main() {
  for (const transport of TRANSPORTS) {
    await distinctEvidenceExtendsSubmitBudget(transport);
    await noEvidenceKeepsOldBudget(transport);
    await multiRowReplyEarnsOnlyOneTurn(transport);
    await maxExtensionKeepsTransientSubmitRetry(transport);
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
