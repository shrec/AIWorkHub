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

for (const name of ["aiworkhub_worker_source_graph_query", "aiworkhub_manager_source_graph_query"]) {
  for (const cursorKey of ["continuation_cursor", "cursor"]) {
    test(`${name}: distinct ${cursorKey} pages reach authority verification`, () => {
      const guard = bridge.createVscodeLmSourceGraphGuard();
      const input = { mode: "body", query: "src/app.py.render" };
      assert.equal(guard.before({ name, input }), null);
      for (const cursor of ["signed-page-1", "signed-page-2"]) {
        assert.equal(guard.before({ name, input: { ...input, [cursorKey]: cursor } }), null);
      }
      const repeated = { name, input: { ...input, [cursorKey]: "signed-page-2" } };
      assert.equal(guard.before(repeated).error, "vscode_lm_source_graph_duplicate");
      assert.throws(() => guard.before(repeated), /vscode_lm_source_graph_no_progress/);
    });
  }
}

test("changing cursor cannot bypass the existing discovery ceiling", () => {
  const guard = bridge.createVscodeLmSourceGraphGuard();
  const name = "aiworkhub_worker_source_graph_query";
  const call = (page) => ({ name, input: {
    mode: "body", query: "src/app.py.render", continuation_cursor: `page-${page}`,
  } });
  const limit = bridge.constants.VSCODE_LM_MAX_SOURCE_GRAPH_WITHOUT_EDIT;
  assert.equal(typeof limit, "number");
  for (let page = 0; page < limit; page++) assert.equal(guard.before(call(page)), null);
  assert.throws(() => guard.before(call(limit)), /vscode_lm_source_graph_no_progress/);
});

const crypto = require("node:crypto");
const digest = (bytes) => crypto.createHash("sha256").update(bytes).digest("hex");
const trusted = (content, overrides = {}) => ({
  ok: true, tool: "source_graph", mode: "body", authority_source: "canonical",
  authority_repo: "D:/Dev/AIWorkHub", hit_count: 1, content: JSON.stringify(content), ...overrides,
});
const evidence = (line = 1) => ({ matches: [{
  file_path: "src/app.py", source_hash: "a".repeat(64), line_start: line, line_end: line,
  source: `print("ქართული ${line}")`, kind: "function", qualname: `app.f${line}`,
}] });
const sgCall = (page) => ({ name: "aiworkhub_worker_source_graph_query",
  input: { mode: "body", query: `app.f${page}`, target: "src/app.py" } });

test("verified novel body evidence permits more than sixteen reads without weakening duplicates", () => {
  const guard = bridge.createVscodeLmSourceGraphGuard();
  for (let page = 0; page < 18; page++) {
    const call = sgCall(page);
    assert.equal(guard.before(call), null);
    guard.observed(call, trusted(evidence(page + 1)));
  }
  assert.equal(guard.before(sgCall(17)).error, "vscode_lm_source_graph_duplicate");
  assert.throws(() => guard.before(sgCall(17)), /source_graph_no_progress/);
});

test("verified base64 body chunks credit raw bytes across split UTF8 and JSON boundaries", () => {
  const guard = bridge.createVscodeLmSourceGraphGuard();
  const bytes = Buffer.from(JSON.stringify(evidence(1)));
  const chunkSize = 11;
  const pages = Math.ceil(bytes.length / chunkSize);
  for (let page = 0; page < pages; page++) {
    const chunk = bytes.subarray(page * chunkSize, (page + 1) * chunkSize);
    const call = { ...sgCall(1), input: { ...sgCall(1).input,
      continuation_cursor: page ? `signed-${page}` : undefined } };
    assert.equal(guard.before(call), null);
    guard.observed(call, trusted(null, {
      content: chunk.toString("base64"), content_encoding: "base64",
      content_sha256: digest(bytes), page_sha256: digest(chunk),
      page_index: page, page_count: pages, full_bytes: bytes.length, bytes: chunk.length,
      continuation_cursor: page + 1 < pages ? `signed-${page + 1}` : null,
    }));
  }
});

for (const [label, result] of [
  ["zero hit nonce", (n) => trusted({ matches: [], nonce: n }, { hit_count: 0 })],
  ["malformed", () => trusted(null, { content: "not JSON" })],
  ["error", (n) => trusted(evidence(n), { ok: false })],
  ["untrusted", (n) => trusted(evidence(n), { authority_source: "legacy" })],
  ["repeated evidence", () => trusted(evidence(1))],
  ["bad page hash", (n) => trusted(null, { content_encoding: "base64",
    content: Buffer.from(`nonce-${n}`).toString("base64"), page_sha256: "a".repeat(64),
    content_sha256: "b".repeat(64), page_index: n, page_count: 100, bytes: 7, full_bytes: 700 })],
]) {
  test(`${label} cannot reset the no-progress ceiling`, () => {
    const guard = bridge.createVscodeLmSourceGraphGuard();
    // Repeated real evidence can credit once, never once per query/receipt.
    const limit = bridge.constants.VSCODE_LM_MAX_SOURCE_GRAPH_WITHOUT_EDIT + (label === "repeated evidence" ? 1 : 0);
    for (let n = 0; n < limit; n++) {
      const call = sgCall(n);
      assert.equal(guard.before(call), null);
      guard.observed(call, result(n));
    }
    assert.throws(() => guard.before(sgCall(limit)), /source_graph_no_progress/);
  });
}

test("valid page hashes without hits or with an unbound continuation never earn progress", () => {
  for (const hitCount of [0, 1]) {
    const guard = bridge.createVscodeLmSourceGraphGuard();
    const limit = bridge.constants.VSCODE_LM_MAX_SOURCE_GRAPH_WITHOUT_EDIT;
    for (let n = 0; n < limit; n++) {
      const chunk = Buffer.from(`unique chunk ${n}`);
      const call = sgCall(n);
      assert.equal(guard.before(call), null);
      guard.observed(call, trusted(null, {
        hit_count: hitCount, content_encoding: "base64", content: chunk.toString("base64"),
        content_sha256: "b".repeat(64), page_sha256: digest(chunk),
        page_index: hitCount ? n + 1 : 0, page_count: 100, bytes: chunk.length, full_bytes: 10000,
        continuation_cursor: `unbound-${n}`,
      }));
    }
    assert.throws(() => guard.before(sgCall(limit)), /source_graph_no_progress/);
  }
});

test("decoded indexed hits earn progress but source-less nonce rows do not", () => {
  const guard = bridge.createVscodeLmSourceGraphGuard();
  for (let n = 0; n < 18; n++) {
    const call = sgCall(n);
    assert.equal(guard.before(call), null);
    guard.observed(call, trusted({ matches: [{
      file_path: "src/app.py", line_start: n + 1, line_end: n + 1,
      kind: "body_match", qualname: `src/app.py:${n + 1}`, signature: `def f${n}():`,
    }] }));
  }
  const limit = bridge.constants.VSCODE_LM_MAX_SOURCE_GRAPH_WITHOUT_EDIT;
  for (let n = 0; n < limit; n++) {
    const call = sgCall(n + 100);
    assert.equal(guard.before(call), null);
    guard.observed(call, trusted({ matches: [{ file_path: "src/app.py", nonce: n }] }));
  }
  assert.throws(() => guard.before(sgCall(999)), /source_graph_no_progress/);
});

for (const changed of ["authority_repo", "page_count", "full_bytes"]) {
  test(`continuation cannot change ${changed} to earn progress`, () => {
    const guard = bridge.createVscodeLmSourceGraphGuard();
    const limit = bridge.constants.VSCODE_LM_MAX_SOURCE_GRAPH_WITHOUT_EDIT;
    for (let n = 0; n <= limit; n++) {
      const chunk = Buffer.from(`body bytes ${n}`);
      const call = { ...sgCall(0), input: { ...sgCall(0).input,
        ...(n ? { continuation_cursor: `next-${n}` } : {}) } };
      assert.equal(guard.before(call), null);
      const result = trusted(null, {
        content_encoding: "base64", content: chunk.toString("base64"),
        content_sha256: "b".repeat(64), page_sha256: digest(chunk),
        page_index: n, page_count: 100, bytes: chunk.length, full_bytes: 10000,
        continuation_cursor: `next-${n + 1}`,
      });
      if (n) result[changed] = changed === "authority_repo" ? "D:/Other" :
        result[changed] + n;
      guard.observed(call, result);
    }
    assert.throws(() => guard.before({ ...sgCall(0), input: { ...sgCall(0).input,
      continuation_cursor: `next-${limit + 1}` } }), /source_graph_no_progress/);
  });
}

test("empty body query cannot earn page progress even with a positive backend hit", () => {
  const guard = bridge.createVscodeLmSourceGraphGuard();
  const limit = bridge.constants.VSCODE_LM_MAX_SOURCE_GRAPH_WITHOUT_EDIT;
  for (let n = 0; n < limit; n++) {
    const chunk = Buffer.from(`body ${n}`);
    const call = { ...sgCall(n), input: { mode: "body", query: " ", cursor: `scan-${n}` } };
    assert.equal(guard.before(call), null);
    guard.observed(call, trusted(null, {
      target: "src/app.py", content_encoding: "base64", content: chunk.toString("base64"),
      content_sha256: "b".repeat(64), page_sha256: digest(chunk),
      page_index: 0, page_count: 100, bytes: chunk.length, full_bytes: 10000,
    }));
  }
  assert.throws(() => guard.before({ ...sgCall(0),
    input: { mode: "body", query: " ", cursor: "scan-last" } }), /source_graph_no_progress/);
});
