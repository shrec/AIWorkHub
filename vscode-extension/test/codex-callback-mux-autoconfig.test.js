"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");

// RM-2026-00082: the App Server mux opt-in is retired. Activation only resets
// a chatgpt.cliExecutable that still names an AIWorkHub mux launcher; it never
// writes a launcher, never edits Settings Sync and leaves any other value
// alone. The fake host serves only the chatgpt section, so a regression that
// reaches for another one fails the call instead of passing quietly.
let currentValue = "";
let updateSupported = true;
let updateFailure = null;
const updates = [];
const fakeVscode = {
  ConfigurationTarget: { Global: 1, Workspace: 2, WorkspaceFolder: 3 },
  workspace: {
    workspaceFolders: [],
    getConfiguration: (section) => {
      assert.equal(section, "chatgpt");
      const config = { get: (key, fallback) => key === "cliExecutable" ? currentValue : fallback };
      if (updateSupported) {
        config.update = async (key, value, target) => {
          if (updateFailure) throw updateFailure;
          updates.push({ key, value, target });
          currentValue = value;
        };
      }
      return config;
    },
  },
  extensions: { getExtension: () => null },
  window: { createOutputChannel: () => ({ appendLine() {}, dispose() {} }) },
  commands: {},
  Uri: {},
  EventEmitter: class {},
  CancellationTokenSource: class {},
  ProgressLocation: { Notification: 15 },
};
const originalLoad = Module._load;
Module._load = function patchedLoad(request, parent, isMain) {
  if (request === "vscode") return fakeVscode;
  return originalLoad.call(this, request, parent, isMain);
};
const extensionPath = path.resolve(__dirname, "..", "extension.js");
delete require.cache[extensionPath];
const { __testInternals } = require(extensionPath);
Module._load = originalLoad;
const source = fs.readFileSync(extensionPath, "utf8");

const GLOBAL_RESET = [{ key: "cliExecutable", value: undefined, target: 1 }];
const RESET = { ok: true, changed: true, reason: "aiworkhub_mux_executable_reset" };
const FS_MUTATORS = [
  "writeFileSync",
  "appendFileSync",
  "copyFileSync",
  "renameSync",
  "mkdirSync",
  "rmSync",
  "unlinkSync",
  "symlinkSync",
  "chmodSync",
];

// Run the activation-time reset against `value` while refusing every
// synchronous filesystem mutation: restoring native Codex must never
// materialize a launcher, a PATH shim or an executable pin on this host.
async function restoreFrom(value) {
  currentValue = value;
  updates.length = 0;
  const mutations = [];
  const originals = FS_MUTATORS.map((name) => [name, fs[name]]);
  for (const name of FS_MUTATORS) {
    fs[name] = (target) => {
      mutations.push(`${name}:${String(target)}`);
      throw new Error(`unexpected fs.${name} while restoring native Codex`);
    };
  }
  try {
    return await __testInternals.restoreNativeCodexExecutable();
  } finally {
    for (const [name, original] of originals) fs[name] = original;
    assert.deepEqual(mutations, []);
  }
}

test("bare and packaged mux commands are reset to native Codex at Global scope", async () => {
  for (const value of [
    "aiworkhub-app-server-mux",
    "aiworkhub-app-server-mux.cmd",
    "aiworkhub-app-server-mux.exe",
    "AIWorkHub-App-Server-Mux.EXE",
    "  aiworkhub-app-server-mux  ",
  ]) {
    assert.equal(__testInternals.isAiWorkHubMuxExecutable(value), true, value);
    assert.equal(__testInternals.isLegacyAiWorkHubMuxPath(value), false, value);
    assert.deepEqual(await restoreFrom(value), RESET, value);
    assert.deepEqual(updates, GLOBAL_RESET, value);
    assert.equal(currentValue, undefined, value);
  }
});

test("legacy mux paths from other Linux, macOS and Windows hosts are reset at Global scope", async () => {
  for (const value of [
    "/home/user/.vscode-server/data/User/globalStorage/shrec.aiworkhub/bin/aiworkhub-app-server-mux",
    "/home/user/.local/bin/aiworkhub-app-server-mux",
    "/Users/alice/Library/Application Support/Code/User/globalStorage/shrec.aiworkhub/bin/aiworkhub-app-server-mux",
    "C:\\Users\\user\\AppData\\Roaming\\Code\\User\\globalStorage\\shrec.aiworkhub\\bin\\aiworkhub-app-server-mux.cmd",
    "C:\\Users\\user\\AppData\\Roaming\\Code\\User\\globalStorage\\ivanechkheidze.aiworkhub\\bin\\aiworkhub-app-server-mux.exe",
    "/home/user/.vscode-server/extensions/shrec.aiworkhub-missing/bin/aiworkhub-app-server-mux",
  ]) {
    assert.equal(__testInternals.isLegacyAiWorkHubMuxPath(value), true, value);
    assert.deepEqual(await restoreFrom(value), RESET, value);
    assert.deepEqual(updates, GLOBAL_RESET, value);
    assert.equal(currentValue, undefined, value);
  }
});

test("a custom Codex executable is never touched", async () => {
  for (const value of [
    "/usr/local/bin/codex",
    "C:\\tools\\codex\\codex.exe",
    "codex",
    "my-aiworkhub-app-server-mux",
    "aiworkhub-app-server-mux-wrapper",
    "aiworkhub-app-server-mux.sh",
    "/opt/aiworkhub-app-server-mux/bin/codex",
  ]) {
    assert.equal(__testInternals.isAiWorkHubMuxExecutable(value), false, value);
    assert.deepEqual(
      await restoreFrom(value),
      { ok: true, changed: false, reason: "custom_cli_executable_preserved" },
      value,
    );
    assert.deepEqual(updates, [], value);
    assert.equal(currentValue, value, value);
  }
});

test("an empty Codex executable is never touched", async () => {
  for (const value of ["", "   ", undefined, null]) {
    assert.deepEqual(
      await restoreFrom(value),
      { ok: true, changed: false, reason: "cli_executable_unset" },
      String(value),
    );
    assert.deepEqual(updates, [], String(value));
    assert.equal(currentValue, value, String(value));
  }
});

test("the reset is idempotent: the next activation finds native Codex and writes nothing", async () => {
  assert.deepEqual(await restoreFrom("aiworkhub-app-server-mux.exe"), RESET);
  assert.deepEqual(updates, GLOBAL_RESET);
  assert.deepEqual(
    await restoreFrom(currentValue),
    { ok: true, changed: false, reason: "cli_executable_unset" },
  );
  assert.deepEqual(updates, []);
});

test("a mux value that cannot be reset is reported as a failure, never thrown", async () => {
  updateSupported = false;
  try {
    assert.deepEqual(
      await restoreFrom("aiworkhub-app-server-mux"),
      { ok: false, changed: false, reason: "configuration_update_unavailable" },
    );
  } finally {
    updateSupported = true;
  }
  assert.equal(currentValue, "aiworkhub-app-server-mux");
  updateFailure = new Error("EACCES: settings.json is read-only");
  try {
    assert.deepEqual(
      await restoreFrom("aiworkhub-app-server-mux"),
      { ok: false, changed: false, reason: "configuration_update_failed" },
    );
  } finally {
    updateFailure = null;
  }
  assert.deepEqual(updates, []);
  assert.equal(currentValue, "aiworkhub-app-server-mux");
});

test("activation resets the Codex executable unconditionally", () => {
  const activateStart = source.indexOf("async function activate(context)");
  const activateEnd = source.indexOf("async function deactivate()", activateStart);
  assert.ok(activateStart >= 0 && activateEnd > activateStart, "activate() markers are missing or renamed");
  const activateSource = source.slice(activateStart, activateEnd);
  // Two-space indentation is the activate() body itself, so no feature flag
  // or branch can skip the reset.
  assert.match(activateSource, /\r?\n {2}await restoreNativeCodexExecutable\(\);\r?\n/);
  assert.equal(activateSource.split("restoreNativeCodexExecutable(").length, 2);
});

test("no extension code path writes a launcher into chatgpt.cliExecutable", () => {
  const writes = [...source.matchAll(/\.update\(\s*"cliExecutable",\s*([^,\s)]+)/g)].map((match) => match[1]);
  assert.deepEqual(writes, ["undefined"]);
  for (const retired of [
    "ensureCodexCallbackMuxConfigured",
    "materializeStableMuxLauncher",
    "materializePathMuxShim",
    "primeStableMuxRuntimePointer",
    "codexMuxLauncherSetting",
  ]) {
    assert.equal(__testInternals[retired], undefined, retired);
  }
});

test("the package contributes no Codex mux opt-in and no configuration defaults", () => {
  const manifest = JSON.parse(fs.readFileSync(path.resolve(__dirname, "..", "package.json"), "utf8"));
  assert.equal(manifest.contributes.configurationDefaults, undefined);
  assert.equal(manifest.contributes.configuration.properties["aiworkhub.enableCodexCallbackMux"], undefined);
});
