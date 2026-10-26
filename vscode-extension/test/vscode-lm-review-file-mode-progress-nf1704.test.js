"use strict";

// NF-2026-01704: a mode=file reply is Source Graph evidence per (file, source_hash, returned window).
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

const REVIEW_STALL = bridge.constants.VSCODE_LM_QUALITY_REVIEW_STALL_TURNS;
const SOURCE_BUDGET = bridge.constants.VSCODE_LM_MAX_SOURCE_GRAPH_WITHOUT_EDIT;
const SG = "aiworkhub_worker_source_graph_query";
const SUBMIT = "aiworkhub_worker_quality_review_submit";
const SUBMISSION = "d".repeat(64);
const FILE = "src/pkg/candidate.py";
const READS = SOURCE_BUDGET + 4;
const TRANSPORTS = [["text", false], ["native", true]];
const run = (native) => (native ? bridge.runVscodeLmAgent : bridge.runVscodeLmTextProtocol);
const submit = { name: SUBMIT, input: { packet_sha256: "e".repeat(64), lens: "correctness", findings: [] } };
const sha = (seed) => crypto.createHash("sha256").update(String(seed)).digest("hex");
const pathOf = (n) => `src/pkg/module_${n}.py`;
const lines = (from, to) => Array.from({ length: to - from + 1 }, (_, i) => `line_${from + i} = ${from + i}`)
  .join("\n") + "\n";
const fileCall = (target, query = target) => ({ name: SG, input: { mode: "file", query, target } });

// Shape of source_graph.file_query: the kind=file match row has no source or signature,
// so the evidence lives in contexts[0].
function fileReply(filePath, {
  hash = sha(`hash:${filePath}`), preview = lines(1, 3), window = null, edit = null,
  mode = "file", authority = "canonical",
} = {}) {
  const payload = {
    matches: [{
      file_path: filePath, language: "python", status: "indexed", source_hash: hash,
      kind: "file", name: filePath, qualname: filePath, line_start: 1, line_end: 1,
    }],
    contexts: [{
      file_path: filePath,
      file: { file_path: filePath, language: "python", status: "indexed", source_hash: hash },
      entities: [], edges: [], found: true,
      source_preview: preview, source_preview_bytes: Buffer.byteLength(preview),
      source_preview_truncated: false,
      ...(window ? {
        source_preview_line_start: window[0], source_preview_line_end: window[1],
        source_preview_total_lines: window[1] + 40,
      } : {}),
    }],
    mode, query: filePath, truncated: false,
  };
  if (edit) edit(payload);
  return {
    ok: true, tool: "source_graph", mode, authority_source: authority,
    authority_repo: "D:/Dev/AIWorkHub", hit_count: 1, content: JSON.stringify(payload),
  };
}

// Each edit leaves matches[0].source_hash intact: the credit reads contexts[0] only.
const MALFORMED = {
  "a missing source_hash": (p) => { delete p.contexts[0].file.source_hash; },
  "an empty source_hash": (p) => { p.contexts[0].file.source_hash = ""; },
  "a 63-character source_hash": (p) => { p.contexts[0].file.source_hash = sha("short").slice(1); },
  "a non-hex source_hash": (p) => { p.contexts[0].file.source_hash = "z".repeat(64); },
  "no file object": (p) => { delete p.contexts[0].file; },
  "a missing preview": (p) => { delete p.contexts[0].source_preview; },
  "an empty preview": (p) => { p.contexts[0].source_preview = ""; },
  "a whitespace-only preview": (p) => { p.contexts[0].source_preview = " \n\t "; },
  "a non-string preview": (p) => { p.contexts[0].source_preview = ["line_1 = 1"]; },
  "no file path": (p) => { delete p.contexts[0].file_path; delete p.contexts[0].file.file_path; },
  "a start line without an end line": (p) => { p.contexts[0].source_preview_line_start = 3; },
  "an end line before its start": (p) => {
    Object.assign(p.contexts[0], { source_preview_line_start: 9, source_preview_line_end: 3 });
  },
  "a zero start line": (p) => {
    Object.assign(p.contexts[0], { source_preview_line_start: 0, source_preview_line_end: 3 });
  },
  "non-integer window lines": (p) => {
    Object.assign(p.contexts[0], { source_preview_line_start: "3", source_preview_line_end: "9" });
  },
  "no contexts": (p) => { delete p.contexts; },
  "an empty contexts list": (p) => { p.contexts = []; },
};

// Scripted provider. plan(turn) returns one tool call.
function scriptedModel(native, plan) {
  const offered = [];
  return {
    offered,
    capabilities: { toolCalling: native },
    async sendRequest(_messages, options) {
      const turn = offered.length;
      offered.push(options && Array.isArray(options.tools) ? options.tools.map((tool) => tool.name) : null);
      const step = plan(turn);
      const part = native ? { callId: `nf1704-${turn}`, ...step }
        : { value: JSON.stringify({ schema_id: bridge.constants.VSCODE_LM_TOOL_REQUEST_SCHEMA, ...step }) };
      return { stream: (async function* () { yield part; }()) };
    },
  };
}

// Live tool fixture: replyFor(readNumber, input) is the backend's answer to one Source Graph read.
function fileTools(replyFor) {
  const reads = [];
  const submits = [];
  const unexpected = [];
  const invoke = async (call) => {
    if (call.name === SUBMIT) {
      submits.push(call.input);
      return { ok: true, durable: true, submission_id: SUBMISSION };
    }
    if (call.name !== SG) {
      unexpected.push(call.name);
      return { ok: false, error: "unexpected_tool" };
    }
    reads.push(call.input);
    return replyFor(reads.length, call.input);
  };
  return Object.assign(invoke, { reads, submits, unexpected });
}

const reviewRequest = () => ({
  requestId: "f".repeat(32), request_kind: "quality_review", prompt: "Review the candidate.",
  allowedWrites: [], path_contracts: {},
});

async function reviewRun(native, plan, replyFor) {
  const model = scriptedModel(native, plan);
  const tools = fileTools(replyFor);
  const result = JSON.parse(await run(native)(model, reviewRequest(), undefined, tools));
  return { model, tools, result };
}

function assertForcedAt(native, { model, tools, result }, forcedAt, what = "run") {
  assert.equal(tools.reads.length, forcedAt,
    `${what}: the read on the first forced turn is refused as a forced-submit violation`);
  assert.equal(model.offered.length, forcedAt + 2, what);
  assert.equal(tools.submits.length, 1, what);
  assert.equal(result.summary, `quality review submitted:${SUBMISSION}`, what);
  assert.deepEqual(tools.unexpected, [], what);
  if (native) {
    assert.deepEqual(model.offered[forcedAt], [SUBMIT], what);
    assert.ok(model.offered[forcedAt - 1].includes(SG), what);
  }
}

test("the review stall window stays 3 turns and the Source Graph call budget stays 16", () => {
  assert.equal(REVIEW_STALL, 3);
  assert.equal(SOURCE_BUDGET, 16);
});

test("a mode=file reply is evidence once per file version and returned window", () => {
  const guard = bridge.createVscodeLmSourceGraphGuard();
  const state = () => [guard.progressRevision(), guard.sourceEvidenceRevision()];
  const observe = (path, options) => guard.observed(fileCall(path), fileReply(path, options));
  assert.deepEqual(state(), [0, 0]);
  observe(pathOf(1));
  assert.deepEqual(state(), [1, 1], "the first read of a file is evidence");
  observe(pathOf(1));
  assert.deepEqual(state(), [1, 1], "an identical replay is not");
  observe(pathOf(2));
  assert.deepEqual(state(), [2, 2], "another file is");
  observe(pathOf(1), { hash: sha("edited") });
  assert.deepEqual(state(), [3, 3], "the same path at another source_hash is");
  observe(pathOf(1), { hash: sha("edited"), preview: lines(1, 9) });
  assert.deepEqual(state(), [4, 4], "another unranged preview of the same file version is");
  observe(pathOf(1), { hash: sha("edited"), preview: lines(1, 9) });
  assert.deepEqual(state(), [4, 4], "and its replay is not");
});

test("a ranged reply is keyed by its returned line window, not by the request or the preview text", () => {
  const guard = bridge.createVscodeLmSourceGraphGuard();
  const state = () => [guard.progressRevision(), guard.sourceEvidenceRevision()];
  const read = (query, window, preview = lines(window[0], window[0] + 2)) =>
    guard.observed(fileCall(FILE, query), fileReply(FILE, { window, preview }));
  read("1-120", [1, 140]);
  assert.deepEqual(state(), [1, 1], "the first window is evidence");
  read("1-120", [1, 140]);
  assert.deepEqual(state(), [1, 1], "the identical window again is not");
  read("5-121", [1, 140]);
  assert.deepEqual(state(), [1, 1], "another request that returns the same window is not");
  read("120-240", [100, 260]);
  assert.deepEqual(state(), [2, 2], "another window of the same file is");
  read("120-241", [100, 261]);
  assert.deepEqual(state(), [3, 3], "a window one line longer is");
  read("1-120", [1, 140], "different text\n");
  assert.deepEqual(state(), [3, 3], "the returned window, not the preview text, identifies a ranged reply");
});

test("a mode=file reply without a 64-hex source_hash or non-empty preview text earns nothing", () => {
  const baseline = bridge.createVscodeLmSourceGraphGuard();
  baseline.observed(fileCall(FILE), fileReply(FILE));
  assert.equal(baseline.sourceEvidenceRevision(), 1, "the unmodified reply is evidence");
  for (const [reason, edit] of Object.entries(MALFORMED)) {
    const guard = bridge.createVscodeLmSourceGraphGuard();
    guard.observed(fileCall(FILE), fileReply(FILE, { edit }));
    assert.deepEqual([guard.progressRevision(), guard.sourceEvidenceRevision()], [0, 0], reason);
  }
});

test("file replies stay behind the guard's existing trust checks", () => {
  const untrusted = [
    ["a failed reply", { ...fileReply(FILE), ok: false }],
    ["another tool's reply", { ...fileReply(FILE), tool: "not_source_graph" }],
    ["an unknown authority", fileReply(FILE, { authority: "workspace" })],
    ["a missing authority repo", { ...fileReply(FILE), authority_repo: "" }],
    ["a reply for another mode", fileReply(FILE, { mode: "context" })],
  ];
  for (const [reason, reply] of untrusted) {
    const guard = bridge.createVscodeLmSourceGraphGuard();
    guard.observed(fileCall(FILE), reply);
    assert.equal(guard.sourceEvidenceRevision(), 0, reason);
  }
  const guard = bridge.createVscodeLmSourceGraphGuard();
  guard.observed({ name: SG, input: { mode: "context", query: FILE, target: FILE } }, fileReply(FILE, { mode: "context" }));
  guard.observed({ name: "not_source_graph", input: { mode: "file", query: FILE, target: FILE } }, fileReply(FILE));
  assert.equal(guard.sourceEvidenceRevision(), 0, "only mode=file replies to Source Graph calls are credited");
  for (const authority of ["canonical", "rework_overlay", "candidate_overlay"]) {
    const overlay = bridge.createVscodeLmSourceGraphGuard();
    overlay.observed(fileCall(FILE), fileReply(FILE, {
      authority, edit: (p) => { delete p.contexts[0].file_path; },
    }));
    assert.equal(overlay.sourceEvidenceRevision(), 1, `${authority}: the path may come from the file row alone`);
  }
});

test("file evidence restarts the stall streak and a replay extends it", () => {
  const guard = bridge.createVscodeLmSourceGraphGuard();
  const watchdog = bridge.createVscodeLmProgressWatchdog(guard, () => true);
  assert.equal(watchdog.beginTurn(), 0);
  guard.observed(fileCall(pathOf(1)), fileReply(pathOf(1)));
  assert.equal(watchdog.beginTurn(), 0, "a new file is progress");
  guard.observed(fileCall(pathOf(1), "1-9"), fileReply(pathOf(1)));
  assert.equal(watchdog.beginTurn(), 1, "the same file version and window again is not");
  guard.observed(fileCall(pathOf(2)), fileReply(pathOf(2), { edit: MALFORMED["a missing source_hash"] }));
  assert.equal(watchdog.beginTurn(), 2, "a reply without a source_hash is not");
  guard.observed(fileCall(FILE, "1-60"), fileReply(FILE, { window: [1, 80] }));
  assert.equal(watchdog.beginTurn(), 0, "a new window of another file is");
});

test("new file evidence resets the Source Graph call budget and replays spend it", () => {
  const progressing = bridge.createVscodeLmSourceGraphGuard();
  for (let n = 0; n < 2 * SOURCE_BUDGET; n += 1) {
    const call = fileCall(pathOf(n));
    assert.equal(progressing.before(call), null, `read ${n + 1} is inside the budget`);
    progressing.observed(call, fileReply(pathOf(n)));
  }
  const replaying = bridge.createVscodeLmSourceGraphGuard();
  const reply = fileReply(FILE, { window: [1, 40], preview: lines(1, 40) });
  let calls = 0;
  assert.throws(() => {
    for (let n = 0; n < 4 * SOURCE_BUDGET; n += 1) {
      const call = fileCall(FILE, `1-${100 + n}`);
      calls += 1;
      replaying.before(call);
      replaying.observed(call, reply);
    }
  }, /vscode_lm_source_graph_no_progress/);
  assert.equal(calls, SOURCE_BUDGET + 2, "the first read was evidence, the next 16 were not");
});

for (const [label, native] of TRANSPORTS) {
  test(`${label}: a reviewer reading ${READS} distinct files with mode=file is never forced to submit`, async () => {
    const outcome = await reviewRun(native,
      (turn) => (turn < READS ? fileCall(pathOf(turn)) : submit),
      (_count, input) => fileReply(input.target));
    assert.deepEqual({ reads: outcome.tools.reads.length, submits: outcome.tools.submits.length },
      { reads: READS, submits: 1 }, "no file read was refused as a forced-submit violation");
    assert.equal(outcome.model.offered.length, READS + 1);
    assert.equal(outcome.result.summary, `quality review submitted:${SUBMISSION}`);
    assert.deepEqual(outcome.tools.unexpected, []);
    if (native) {
      assert.ok(outcome.model.offered.every((names) => Array.isArray(names) && names.includes(SG)),
        "submit-only was never forced");
    }
  });

  test(`${label}: a reviewer reading ${READS} distinct windows of one file is never forced to submit`, async () => {
    const queryOf = (n) => `${1 + 50 * n}-${50 + 50 * n}`;
    const outcome = await reviewRun(native,
      (turn) => (turn < READS ? fileCall(FILE, queryOf(turn)) : submit),
      (_count, input) => {
        const [low, high] = input.query.split("-").map(Number);
        const window = [Math.max(1, low - 20), high + 20];
        return fileReply(FILE, { window, preview: lines(window[0], window[0] + 2) });
      });
    assert.deepEqual({ reads: outcome.tools.reads.length, submits: outcome.tools.submits.length },
      { reads: READS, submits: 1 }, "no window read was refused as a forced-submit violation");
    assert.deepEqual(outcome.tools.reads.map((input) => input.query),
      Array.from({ length: READS }, (_, n) => queryOf(n)));
    assert.equal(outcome.model.offered.length, READS + 1);
    assert.equal(outcome.result.summary, `quality review submitted:${SUBMISSION}`);
    if (native) {
      assert.ok(outcome.model.offered.every((names) => Array.isArray(names) && names.includes(SG)),
        "submit-only was never forced");
    }
  });

  test(`${label}: replaying one file window earns nothing and the submit is forced after ${REVIEW_STALL} stalled turns`, async () => {
    const forcedAt = 1 + REVIEW_STALL;
    const outcome = await reviewRun(native,
      (turn) => (turn <= forcedAt ? fileCall(FILE, `1-${100 + turn}`) : submit),
      () => fileReply(FILE, { window: [1, 40], preview: lines(1, 40) }));
    assertForcedAt(native, outcome, forcedAt);
  });

  test(`${label}: new files then replays force the submit ${REVIEW_STALL} turns after the last new file`, async () => {
    const distinct = 5;
    const forcedAt = distinct + REVIEW_STALL;
    const outcome = await reviewRun(native,
      (turn) => {
        if (turn > forcedAt) return submit;
        return turn < distinct ? fileCall(pathOf(turn)) : fileCall(pathOf(distinct - 1), `1-${100 + turn}`);
      },
      (count) => fileReply(pathOf(Math.min(count, distinct) - 1)));
    assertForcedAt(native, outcome, forcedAt);
  });

  test(`${label}: file replies without a source_hash or preview text force the submit after ${REVIEW_STALL} stalled turns`, async () => {
    for (const [reason, edit] of Object.entries(MALFORMED)) {
      const outcome = await reviewRun(native,
        (turn) => (turn <= REVIEW_STALL ? fileCall(pathOf(turn)) : submit),
        (_count, input) => fileReply(input.target, { edit }));
      assertForcedAt(native, outcome, REVIEW_STALL, reason);
    }
  });
}
