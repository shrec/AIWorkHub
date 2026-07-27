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
assert.ok(!source.includes("aiworkhub-app-server-mux"));
assert.ok(!source.includes("mux_path"));

console.log("AIWorkHub stable runtime upgrade regression passed");
