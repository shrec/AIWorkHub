"use strict";

const assert = require("assert");
const fs = require("fs");
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
assert.ok(source.includes("ensureCodexCallbackMuxConfigured(context, stableMuxLauncher)"));
assert.ok(source.includes("primeStableMuxRuntimePointer(context)"));

const activateStart = source.indexOf("async function activate(context)");
const activateEnd = source.indexOf("async function deactivate()", activateStart);
const activateSource = source.slice(activateStart, activateEnd);
assert.ok(
  activateSource.indexOf("primeStableMuxRuntimePointer(context)")
    < activateSource.indexOf("materializeStableRuntimeGeneration(context)"),
  "the mux runtime pointer must be published before expensive generation materialization",
);
assert.ok(
  activateSource.indexOf("ensureCodexCallbackMuxConfigured(context, stableMuxLauncher)")
    < activateSource.indexOf("materializeStableRuntimeGeneration(context)"),
  "Codex mux configuration must win the concurrent extension startup race",
);
assert.ok(
  activateSource.indexOf("refreshCoordinatorRouteOwnership(activeRepoIdentity)")
    < activateSource.indexOf("materializeStableRuntimeGeneration(context)"),
  "the exact extension-host repo route must exist before the mux starts Codex",
);

console.log("AIWorkHub stable runtime upgrade regression passed");
