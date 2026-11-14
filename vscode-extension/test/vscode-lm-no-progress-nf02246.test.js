// NF-2026-02246: vscode_lm workers on small cards died in under three minutes
// with vscode_lm_source_graph_no_progress because the second exact repeat of a
// Source Graph request tripped the detector. An exact repeat (same
// mode+query+target+cursor) is still never executed and still gets the duplicate
// corrective; only the trip point moved to VSCODE_LM_MAX_IDENTICAL_SOURCE_GRAPH_REPEATS
// consecutive repeats with no other tool call or edit in between. The error is unchanged.
const assert = require("node:assert/strict");
const test = require("node:test");
const Module = require("node:module");
const originalLoad = Module._load;
Module._load = function (name, parent, isMain) {
  if (name === "vscode") return {
    workspace: { workspaceFolders: [], getConfiguration: () => ({ get: (_key, fallback) => fallback }) },
  };
  return originalLoad.call(this, name, parent, isMain);
};
let bridge;
try {
  bridge = require("../extension.js").__testInternals;
} finally {
  Module._load = originalLoad;
}

const REPEATS = bridge.constants.VSCODE_LM_MAX_IDENTICAL_SOURCE_GRAPH_REPEATS;
const SG = "aiworkhub_manager_source_graph_query";
const sg = (query, extra = {}) => ({ name: SG, input: { mode: "focus", query, ...extra } });
const reply = (content, extra = {}) => ({
  ok: true, tool: "source_graph", mode: "focus", authority_source: "canonical",
  authority_repo: "D:/repo/AIWorkHub", receipt_id: Math.random().toString(16).slice(2),
  content: JSON.stringify(content), ...extra,
});

// Run one guarded Source Graph call the way the dispatcher does.
const run = (guard, call, result) => {
  const blocked = guard.before(call);
  if (blocked) return blocked;
  return guard.observed(call, result) || result;
};

test("the repeat bound is a small fixed number above the old two-duplicate trip", () => {
  assert.equal(REPEATS, 4);
  assert.ok(REPEATS < bridge.constants.VSCODE_LM_MAX_SOURCE_GRAPH_WITHOUT_EDIT);
});

test("two identical Source Graph replies followed by an edit do not trip no_progress", () => {
  const guard = bridge.createVscodeLmSourceGraphGuard();
  const call = sg("src/app.py");
  const same = { matches: [] };
  for (let round = 0; round < 3; round++) {
    assert.equal(run(guard, call, reply(same)).ok, true);
    assert.equal(run(guard, call, reply(same)).error, "vscode_lm_source_graph_duplicate");
    assert.equal(run(guard, call, reply(same)).error, "vscode_lm_source_graph_duplicate");
    // A successful edit ends the repeat run; so does a failed one.
    guard.staged(round === 1 ? { ok: false } : { ok: true });
  }
});

test("N exact repeats with an identical reply hash and no other activity still trip", () => {
  const guard = bridge.createVscodeLmSourceGraphGuard();
  const call = sg("src/app.py", { target: "src/app.py", cursor: "page-1" });
  assert.equal(run(guard, call, reply({ matches: [] })).ok, true);
  for (let n = 0; n < REPEATS; n++) {
    // An exact repeat is not executed, so its reply is the identical prior reply.
    const corrective = run(guard, call, reply({ matches: [] }));
    assert.equal(corrective.ok, false);
    assert.equal(corrective.duplicate, true);
    assert.equal(corrective.schema_id, "aiworkhub.vscode_lm.source_graph_duplicate.v1");
  }
  assert.throws(() => guard.before(call), (err) => {
    assert.equal(err.message, "vscode_lm_source_graph_no_progress");
    return true;
  });
});

test("a different query resets the repeat counter", () => {
  const guard = bridge.createVscodeLmSourceGraphGuard();
  const first = sg("src/app.py");
  const other = sg("src/other.py");
  run(guard, first, reply({ matches: [] }));
  for (let n = 0; n < REPEATS; n++) run(guard, first, reply({ matches: [] }));
  assert.equal(run(guard, other, reply({ matches: [1] })).ok, true);
  run(guard, first, reply({ matches: [] }));
  for (let n = 0; n < REPEATS; n++) {
    assert.equal(run(guard, first, reply({ matches: [] })).duplicate, true);
  }
  assert.throws(() => guard.before(first), /^Error: vscode_lm_source_graph_no_progress$/);
});

test("a different cursor, target or mode is not an exact repeat", () => {
  for (const variant of [{ cursor: "page-2" }, { target: "src/app.py" }, { mode: "body" }]) {
    const guard = bridge.createVscodeLmSourceGraphGuard();
    const base = sg("app.render");
    run(guard, base, reply({ matches: [] }));
    for (let n = 0; n < REPEATS; n++) run(guard, base, reply({ matches: [] }));
    const changed = { name: SG, input: { ...base.input, ...variant } };
    assert.equal(guard.before(changed), null, JSON.stringify(variant));
  }
});

test("an intervening non-Source-Graph tool call resets the repeat run", () => {
  const guard = bridge.createVscodeLmSourceGraphGuard();
  const call = sg("src/app.py");
  run(guard, call, reply({ matches: [] }));
  for (let n = 0; n < REPEATS; n++) run(guard, call, reply({ matches: [] }));
  assert.equal(guard.before({ name: "aiworkhub_worker_kb_search", input: { query: "x" } }), null);
  assert.equal(run(guard, call, reply({ matches: [] })).ok, true);
});

test("the discovery ceiling is unchanged by the repeat rule", () => {
  const guard = bridge.createVscodeLmSourceGraphGuard();
  const limit = bridge.constants.VSCODE_LM_MAX_SOURCE_GRAPH_WITHOUT_EDIT;
  for (let n = 0; n < limit; n++) {
    // Alternate two queries so no exact repeat ever accumulates.
    assert.equal(guard.before(sg(n % 2 ? "a" : "b")), null);
  }
  assert.throws(() => guard.before(sg("c")), /vscode_lm_source_graph_no_progress/);
});

test("the dispatcher returns the duplicate corrective and then fails with the same error", async () => {
  const guard = bridge.createVscodeLmSourceGraphGuard();
  const stagedEdits = {
    noteRecoverySourceGraph() {},
    observeSemanticEdit() {},
  };
  let invoked = 0;
  const invokeTool = async () => { invoked += 1; return reply({ matches: [] }); };
  const call = sg("src/app.py");
  const first = await bridge.invokeVscodeLmProtocolTool(call, "req", invokeTool, stagedEdits, "", guard);
  assert.equal(first.ok, true);
  for (let n = 0; n < REPEATS; n++) {
    const repeat = await bridge.invokeVscodeLmProtocolTool(call, "req", invokeTool, stagedEdits, "", guard);
    assert.equal(repeat.error, "vscode_lm_source_graph_duplicate");
  }
  // An exact repeat is never executed live; only the first call reached the tool.
  assert.equal(invoked, 1);
  await assert.rejects(
    bridge.invokeVscodeLmProtocolTool(call, "req", invokeTool, stagedEdits, "", guard),
    /^Error: vscode_lm_source_graph_no_progress$/,
  );
  assert.equal(invoked, 1);
});
