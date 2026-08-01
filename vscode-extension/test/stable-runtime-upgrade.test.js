"use strict";

const assert = require("assert");
const fs = require("fs");
const Module = require("module");
const path = require("path");

const source = fs.readFileSync(path.resolve(__dirname, "..", "extension.js"), "utf8");

assert.ok(source.includes("function materializeStableRuntimeGeneration(context)"));
assert.ok(source.includes("STABLE_RUNTIME_SCHEMA"));
assert.ok(source.includes("_runtimeTreeFingerprint(sourceRuntime)"));
assert.ok(source.includes("fs.renameSync(staging, generationRoot)"));
assert.ok(source.includes("if (!fs.existsSync(manifestPath)) throw err"));
assert.ok(source.includes("generationName = `${version}-${fingerprint.slice(0, 16)}`"));
assert.ok(!source.includes("mux_path"));
assert.ok(source.includes("materializeStableMuxLauncher(context)"));
assert.ok(source.includes("ensureCodexCallbackMuxConfigured(context)"));
assert.ok(source.includes("primeStableMuxRuntimePointer(context)"));
assert.ok(source.includes("stableRuntime = bundledRuntimeFallback(context)"));
assert.ok(source.includes('const runtimeLabel = stableRuntime.generationRoot'));

const activateStart = source.indexOf("async function activate(context)");
const activateEnd = source.indexOf("async function deactivate()", activateStart);
const activateSource = source.slice(activateStart, activateEnd);
assert.ok(
  activateSource.indexOf("primeStableMuxRuntimePointer(context)")
    < activateSource.indexOf("materializeStableRuntimeGeneration(context)"),
  "the mux runtime pointer must be published before expensive generation materialization",
);
assert.ok(
  activateSource.indexOf("materializeStableMuxLauncher(context)")
    < activateSource.indexOf("ensureCodexCallbackMuxConfigured(context)"),
  "the fail-open mux launcher must exist before a co-located Codex can select it",
);
assert.ok(
  activateSource.indexOf("refreshCoordinatorRouteOwnership(activeRepoIdentity)")
    < activateSource.indexOf("ensureCodexCallbackMuxConfigured(context)"),
  "the exact repo/extension-host route must exist before Codex can start through the mux",
);
assert.ok(
  activateSource.indexOf("refreshCoordinatorRouteOwnership(activeRepoIdentity)")
    < activateSource.indexOf("materializeStableRuntimeGeneration(context)"),
  "the exact extension-host repo route must exist before the mux starts Codex",
);

const extensionPath = path.resolve(__dirname, "..", "extension.js");
const originalLoad = Module._load;
Module._load = function patchedLoad(request, parent, isMain) {
  if (request === "vscode") {
    return {
      workspace: { workspaceFolders: [], getConfiguration: () => ({ get: () => "" }) },
      window: { createOutputChannel: () => ({ appendLine() {}, dispose() {} }) },
      Uri: {},
    };
  }
  return originalLoad.call(this, request, parent, isMain);
};
let extension;
try {
  extension = require(extensionPath);
} finally {
  Module._load = originalLoad;
}
const fallback = extension.__testInternals.bundledRuntimeFallback({
  extensionUri: { fsPath: path.resolve(__dirname, "..") },
  extension: { packageJSON: { version: require("../package.json").version } },
});
assert.ok(fs.existsSync(path.join(fallback.runtimeDir, "aiworkhub", "server.py")));
assert.strictEqual(fallback.generationRoot, null);
assert.strictEqual(fallback.storageRoot, null);

console.log("AIWorkHub stable runtime upgrade regression passed");
