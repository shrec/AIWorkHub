// The extension discovers its callable model catalog from VS Code, never from
// a hardcoded list. This regression drives every assertion from what discovery
// returns for a fabricated selectChatModels report, so nothing here re-encodes
// the model names as an allowlist the discovery is checked against.
const assert = require("assert");
const path = require("path");
const Module = require("module");

const extensionPath = path.resolve(__dirname, "..", "extension.js");
const fakeVscode = {
  workspace: { workspaceFolders: [], getConfiguration: () => ({ get: (_key, fallback) => fallback }) },
  CancellationTokenSource: class { constructor() { this.token = { isCancellationRequested: false }; } cancel() {} dispose() {} },
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

// The six working GLM models the owner's Copilot custom endpoint (Z.AI,
// vendor=customendpoint) exposes, surfaced into VS Code exactly as
// selectChatModels reports them.
const GLM_IDS = ["glm-5.3", "glm-5.2", "glm-5.1", "glm-5", "glm-5-turbo", "glm-4.7"];
const reportedGlm = GLM_IDS.map((id) => ({
  id,
  family: id,
  name: id.toUpperCase(),
  vendor: "customendpoint",
  version: "1.0.0",
  capabilities: { toolCalling: true },
}));
// Entries the measured non-callable filters must exclude, plus one malformed id
// the requested-name regex must reject.
const excluded = [
  { id: "claude-haiku-4.5", family: "claude-haiku-4.5", vendor: "copilotcli" },
  { id: "claude-sonnet-5", family: "claude-sonnet-5", vendor: "claude-code" },
  { id: "copilot-utility", family: "copilot-utility", vendor: "copilot" },
  { id: "bad model!", family: "bad model!", vendor: "customendpoint" },
  null,
  undefined,
];
const report = [...reportedGlm, ...excluded];

// Discovery is the source: the callable catalog is exactly the six GLM ids the
// editor reported, with the non-callable and malformed entries removed.
const discovered = internals.vscodeLmDiscoverCallableNames(report);
assert.deepStrictEqual([...discovered].sort(), [...GLM_IDS].sort());
assert.strictEqual(discovered.length, 6);

// Every discovered id resolves back to a concrete provider entry -- driven from
// the discovery result, never a literal list.
for (const name of discovered) {
  const model = internals.selectVscodeLanguageModel(report, name);
  assert.ok(model, `discovered model ${name} must resolve to a provider entry`);
  assert.strictEqual(model.vendor, "customendpoint");
}

// A model the editor never reported is absent from the catalog by name.
assert.ok(!discovered.includes("glm-9.9"));
assert.strictEqual(internals.selectVscodeLanguageModel(report, "glm-9.9"), null);

// The non-callable provider filters are preserved: each excluded entry still
// fails isCallableVscodeLmProvider for the reason measurement established.
assert.strictEqual(internals.isCallableVscodeLmProvider(excluded[0]), false); // copilotcli
assert.strictEqual(internals.isCallableVscodeLmProvider(excluded[1]), false); // claude-code
assert.strictEqual(internals.isCallableVscodeLmProvider(excluded[2]), false); // copilot-utility*
assert.strictEqual(internals.isCallableVscodeLmProvider(reportedGlm[0]), true);

// The requested-name regex is preserved: a malformed id is dropped even though
// its vendor is callable.
assert.ok(!discovered.includes("bad model!"));

// A brand-new endpoint model never seen before is discovered purely from the
// report, with no code change and no hand edit.
const novel = internals.vscodeLmDiscoverCallableNames([
  { id: "glm-6-preview", family: "glm-6-preview", vendor: "customendpoint" },
]);
assert.deepStrictEqual(novel, ["glm-6-preview"]);

// No hardcoded supported-model list gates discovery.
assert.deepStrictEqual(internals.vscodeLmDiscoverCallableNames([]), []);

console.log("LM model discovery: ok");
