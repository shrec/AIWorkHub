"use strict";

const { test, after } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const Module = require("node:module");

let currentValue = "";
const updates = [];
const fakeVscode = {
  ConfigurationTarget: { Global: 1, Workspace: 2, WorkspaceFolder: 3 },
  workspace: {
    workspaceFolders: [],
    getConfiguration: (section) => {
      assert.equal(section, "chatgpt");
      return {
        get: (key, fallback) => key === "cliExecutable" ? currentValue : fallback,
        update: async (key, value, global) => {
          updates.push({ key, value, global });
          currentValue = value;
        },
      };
    },
  },
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

const temp = fs.mkdtempSync(path.join(os.tmpdir(), "aiworkhub-mux-config-"));
const bin = path.join(temp, "bin");
fs.mkdirSync(bin);
const launcher = path.join(bin, process.platform === "win32" ? "aiworkhub-app-server-mux.cmd" : "aiworkhub-app-server-mux");
fs.writeFileSync(launcher, "launcher", { mode: 0o755 });
const globalStorage = path.join(temp, "global-storage");
const context = {
  extensionUri: { fsPath: temp },
  globalStorageUri: { fsPath: globalStorage },
};

after(() => fs.rmSync(temp, { recursive: true, force: true }));

test("empty Codex executable remains native and is never configured", async () => {
  currentValue = "";
  updates.length = 0;
  const result = await __testInternals.ensureCodexCallbackMuxConfigured(context);
  assert.equal(result.ok, true);
  assert.equal(result.changed, false);
  assert.deepEqual(updates, []);
});

test("legacy absolute AIWorkHub mux path is removed instead of rewritten", async () => {
  currentValue = path.join(temp, "old", "aiworkhub-app-server-mux");
  updates.length = 0;
  const result = await __testInternals.ensureCodexCallbackMuxConfigured(context);
  assert.equal(result.changed, true);
  assert.deepEqual(updates, [{ key: "cliExecutable", value: undefined, global: 1 }]);
  assert.equal(result.launcher, "");
  assert.equal(result.mode, "native_codex");
});

test("legacy mux paths from Linux macOS and Windows hosts are all recognized", () => {
  for (const value of [
    "/home/metal/.vscode-server/data/User/globalStorage/shrec.aiworkhub/bin/aiworkhub-app-server-mux",
    "/Users/alice/Library/Application Support/Code/User/globalStorage/shrec.aiworkhub/bin/aiworkhub-app-server-mux",
    "C:\\Users\\bob\\AppData\\Roaming\\Code\\User\\globalStorage\\shrec.aiworkhub\\bin\\aiworkhub-app-server-mux.cmd",
  ]) {
    assert.equal(__testInternals.isLegacyAiWorkHubMuxPath(value), true, value);
  }
});

test("machine-neutral AIWorkHub command is never persisted again", async () => {
  currentValue = "aiworkhub-app-server-mux";
  updates.length = 0;
  const result = await __testInternals.ensureCodexCallbackMuxConfigured(context);
  assert.equal(result.ok, true);
  assert.equal(result.changed, false);
  assert.deepEqual(updates, []);
});

test("unrelated custom Codex executable is never overwritten", async () => {
  currentValue = path.join(temp, "custom-codex");
  updates.length = 0;
  const result = await __testInternals.ensureCodexCallbackMuxConfigured(context);
  assert.equal(result.ok, false);
  assert.equal(result.reason, "custom_cli_executable_preserved");
  assert.deepEqual(updates, []);
});

test("legacy cleanup does not depend on the optional mux launcher existing", async () => {
  currentValue = "/home/old/.vscode-server/extensions/shrec.aiworkhub-missing/bin/aiworkhub-app-server-mux";
  updates.length = 0;
  const missingContext = {
    extensionUri: { fsPath: path.join(temp, "does-not-exist") },
  };
  const result = await __testInternals.ensureCodexCallbackMuxConfigured(missingContext);
  assert.equal(result.ok, true);
  assert.equal(result.changed, true);
  assert.deepEqual(updates, [{ key: "cliExecutable", value: undefined, global: 1 }]);
});

test("stable launcher lives outside versioned VSIX and follows runtime current.json", async () => {
  const runtimeDir = path.join(globalStorage, "runtime", "generations", "current", "runtime");
  fs.mkdirSync(path.join(runtimeDir, "aiworkhub"), { recursive: true });
  fs.writeFileSync(path.join(runtimeDir, "aiworkhub", "app_server_mux.py"), "def main(): return 0\n");
  fs.mkdirSync(path.join(globalStorage, "runtime"), { recursive: true });
  fs.writeFileSync(
    path.join(globalStorage, "runtime", "current.json"),
    JSON.stringify({ runtime_dir: runtimeDir }),
  );
  const stable = __testInternals.materializeStableMuxLauncher(context);
  assert.ok(stable.startsWith(path.join(globalStorage, "bin")));
  assert.ok(!stable.startsWith(context.extensionUri.fsPath + path.sep + "bin"));
  const pythonLauncher = path.join(path.dirname(stable), "aiworkhub-app-server-mux.py");
  assert.ok(fs.readFileSync(pythonLauncher, "utf8").includes('runtime / "aiworkhub" / "app_server_mux.py"'));
  if (process.platform === "win32") {
    assert.ok(stable.endsWith(".cmd"));
    assert.ok(fs.readFileSync(stable, "utf8").includes("aiworkhub-app-server-mux.py"));
  } else {
    assert.ok((fs.statSync(stable).mode & 0o111) !== 0);
  }
});

test("bootstrap runtime pointer is available before immutable generation materialization", () => {
  const packagedRuntime = path.join(temp, "runtime");
  fs.mkdirSync(path.join(packagedRuntime, "aiworkhub"), { recursive: true });
  fs.writeFileSync(path.join(packagedRuntime, "aiworkhub", "__init__.py"), "__version__ = 'test'\n");
  fs.writeFileSync(path.join(packagedRuntime, "aiworkhub", "app_server_mux.py"), "def main(): return 0\n");
  const result = __testInternals.primeStableMuxRuntimePointer(context);
  const pointer = JSON.parse(
    fs.readFileSync(path.join(globalStorage, "runtime", "current.json"), "utf8"),
  );
  assert.equal(result.runtimeDir, packagedRuntime);
  assert.equal(pointer.runtime_dir, packagedRuntime);
  assert.equal(pointer.bootstrap, true);
});

test("manifest-default mux command is materialized on the extension-host PATH", () => {
  const home = path.join(temp, "shim-home");
  const env = { PATH: path.join(home, "existing-bin") };
  const shim = __testInternals.materializePathMuxShim(launcher, {
    platform: "linux",
    home,
    env,
  });
  assert.equal(shim, path.join(home, ".local", "bin", "aiworkhub-app-server-mux"));
  assert.ok(env.PATH.startsWith(path.join(home, ".local", "bin") + path.delimiter));
  assert.ok(fs.readFileSync(shim, "utf8").includes(launcher));
  if (process.platform !== "win32") {
    assert.ok((fs.statSync(shim).mode & 0o111) !== 0);
  }
});

test("Windows PATH shim is a command wrapper and never depends on POSIX mode bits", () => {
  const home = path.join(temp, "windows-shim-home");
  const shim = __testInternals.materializePathMuxShim(launcher, {
    platform: "win32",
    home,
    env: { PATH: "" },
  });
  assert.equal(
    shim,
    path.join(home, "AppData", "Local", "Microsoft", "WindowsApps", "aiworkhub-app-server-mux.cmd"),
  );
  const content = fs.readFileSync(shim, "utf8");
  assert.ok(content.startsWith("@echo off"));
  assert.ok(content.includes(launcher));
});

test("package never contributes an application-scoped Codex executable default", () => {
  const manifest = JSON.parse(fs.readFileSync(path.resolve(__dirname, "..", "package.json"), "utf8"));
  assert.ok(!manifest.contributes.configurationDefaults);
});
