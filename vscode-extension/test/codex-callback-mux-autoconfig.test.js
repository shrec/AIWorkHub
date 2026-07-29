"use strict";

const { test, after } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const Module = require("node:module");

let currentValue = "";
let ignoredSettings = [];
const updates = [];
const fakeVscode = {
  ConfigurationTarget: { Global: 1, Workspace: 2, WorkspaceFolder: 3 },
  workspace: {
    workspaceFolders: [],
    getConfiguration: (section) => {
      if (section === "settingsSync") {
        return {
          get: (key, fallback) => key === "ignoredSettings" ? ignoredSettings : fallback,
          update: async (key, value, global) => {
            updates.push({ section, key, value, global });
            ignoredSettings = value;
          },
        };
      }
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

test("co-located Codex is configured through one host-local stable mux", async () => {
  const codexRoot = path.join(temp, "openai-chatgpt");
  const arch = process.arch === "arm64" ? "aarch64" : process.arch === "x64" ? "x86_64" : process.arch;
  const platformName = process.platform === "win32" ? "windows" : process.platform;
  const executable = path.join(codexRoot, "bin", `${platformName}-${arch}`, process.platform === "win32" ? "codex.exe" : "codex");
  fs.mkdirSync(path.dirname(executable), { recursive: true });
  fs.writeFileSync(executable, "codex", { mode: 0o755 });
  fakeVscode.extensions.getExtension = (id) => id === "openai.chatgpt" ? { extensionPath: codexRoot } : null;
  const previousSidebandDir = process.env.AIWORKHUB_APP_SERVER_MUX_SIDEBAND_DIR;
  const testSidebandDir = path.join(temp, "sideband");
  process.env.AIWORKHUB_APP_SERVER_MUX_SIDEBAND_DIR = testSidebandDir;
  currentValue = "";
  ignoredSettings = [];
  updates.length = 0;
  try {
    const result = await __testInternals.ensureCodexCallbackMuxConfigured(context);
    assert.equal(result.ok, true);
    assert.equal(result.mode, "app_server_sideband");
    assert.equal(result.changed, true);
    assert.equal(result.launcher, "aiworkhub-app-server-mux");
    assert.equal(currentValue, result.launcher);
    assert.ok(ignoredSettings.includes("chatgpt.cliExecutable"));
    const pin = fs.readFileSync(path.join(testSidebandDir, "real_executable"), "utf8").trim();
    assert.equal(pin, executable);
  } finally {
    if (previousSidebandDir === undefined) delete process.env.AIWORKHUB_APP_SERVER_MUX_SIDEBAND_DIR;
    else process.env.AIWORKHUB_APP_SERVER_MUX_SIDEBAND_DIR = previousSidebandDir;
    fakeVscode.extensions.getExtension = () => null;
  }
});

test("stale equal launcher receives one bounded activation pulse, never a reload loop", async () => {
  const codexRoot = path.join(temp, "openai-chatgpt-pulse");
  const arch = process.arch === "arm64" ? "aarch64" : process.arch === "x64" ? "x86_64" : process.arch;
  const platformName = process.platform === "win32" ? "windows" : process.platform;
  const executable = path.join(codexRoot, "bin", `${platformName}-${arch}`, process.platform === "win32" ? "codex.exe" : "codex");
  fs.mkdirSync(path.dirname(executable), { recursive: true });
  fs.writeFileSync(executable, "codex", { mode: 0o755 });
  fakeVscode.extensions.getExtension = () => ({ extensionPath: codexRoot });
  const previousSidebandDir = process.env.AIWORKHUB_APP_SERVER_MUX_SIDEBAND_DIR;
  process.env.AIWORKHUB_APP_SERVER_MUX_SIDEBAND_DIR = path.join(temp, "sideband-pulse");
  const marker = new Map();
  const pulseContext = {
    ...context,
    globalState: {
      get: (key, fallback) => marker.has(key) ? marker.get(key) : fallback,
      update: async (key, value) => marker.set(key, value),
    },
  };
  __testInternals.materializeStableMuxLauncher(pulseContext);
  currentValue = "aiworkhub-app-server-mux";
  updates.length = 0;
  try {
    const first = await __testInternals.ensureCodexCallbackMuxConfigured(pulseContext);
    assert.equal(first.activation_refreshed, true);
    assert.deepEqual(
      updates.filter((entry) => entry.key === "cliExecutable"),
      [
        { key: "cliExecutable", value: undefined, global: 1 },
        { key: "cliExecutable", value: currentValue, global: 1 },
      ],
    );
    updates.length = 0;
    const second = await __testInternals.ensureCodexCallbackMuxConfigured(pulseContext);
    assert.equal(second.changed, false);
    assert.deepEqual(updates, []);
  } finally {
    if (previousSidebandDir === undefined) delete process.env.AIWORKHUB_APP_SERVER_MUX_SIDEBAND_DIR;
    else process.env.AIWORKHUB_APP_SERVER_MUX_SIDEBAND_DIR = previousSidebandDir;
    fakeVscode.extensions.getExtension = () => null;
  }
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

test("packaged Windows host installs a native shell-free launcher with an exact target", () => {
  const home = path.join(temp, "windows-native-home");
  const native = path.join(temp, "packaged", "aiworkhub-app-server-mux.exe");
  fs.mkdirSync(path.dirname(native), { recursive: true });
  fs.writeFileSync(native, Buffer.from("MZ-native-launcher"));
  const shim = __testInternals.materializePathMuxShim(launcher, {
    platform: "win32",
    home,
    env: { PATH: "" },
    windowsNativeLauncher: native,
  });
  assert.equal(
    shim,
    path.join(home, "AppData", "Local", "Microsoft", "WindowsApps", "aiworkhub-app-server-mux.exe"),
  );
  assert.deepEqual(fs.readFileSync(shim), fs.readFileSync(native));
  assert.equal(
    fs.readFileSync(`${shim}.target`, "utf8").trim(),
    path.join(path.dirname(launcher), "aiworkhub-app-server-mux.py"),
  );
});

test("package contributes only the machine-neutral Codex mux command", () => {
  const manifest = JSON.parse(fs.readFileSync(path.resolve(__dirname, "..", "package.json"), "utf8"));
  assert.deepEqual(manifest.contributes.configurationDefaults, {
    "chatgpt.cliExecutable": "aiworkhub-app-server-mux",
  });
});
