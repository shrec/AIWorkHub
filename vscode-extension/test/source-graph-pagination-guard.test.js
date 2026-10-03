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
