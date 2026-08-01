// A pid is our child's identity only while that child is still running.
// `taskkill /PID <pid> /T /F` kills the target AND every descendant, so
// issuing it for an already-exited (therefore possibly recycled) pid can
// destroy an unrelated process tree -- including one containing this
// extension host, which would take every extension in the window down.
const assert = require("assert");
const childProcess = require("child_process");
const Module = require("module");

// Minimal `vscode` stub so extension.js can be required outside the host.
const originalResolve = Module._resolveFilename;
Module._resolveFilename = function (request, ...rest) {
  if (request === "vscode") return "vscode-stub";
  return originalResolve.call(this, request, ...rest);
};
require.cache["vscode-stub"] = {
  id: "vscode-stub",
  filename: "vscode-stub",
  loaded: true,
  exports: {
    workspace: { getConfiguration: () => ({ get: (_k, d) => d, update: async () => {} }), workspaceFolders: [] },
    window: { createOutputChannel: () => ({ appendLine() {}, dispose() {} }) },
    commands: { registerCommand: () => ({ dispose() {} }) },
    Uri: { joinPath: () => ({ fsPath: "" }) },
    extensions: { getExtension: () => null },
    ConfigurationTarget: { Global: 1 },
  },
};

const { __testInternals } = require("../extension.js");
const { McpStdioClient } = __testInternals;

function makeClient() {
  const channel = { appendLine() {} };
  return new McpStdioClient("D:\\repo", channel, { repoId: "repo_" + "a".repeat(32) }, "episode_test");
}

function fakeChild(pid, { exitCode = null, signalCode = null } = {}) {
  return { pid, exitCode, signalCode, killed: false, kill() { this.killed = true; } };
}

let taskkillCalls = [];
const realSpawnSync = childProcess.spawnSync;
childProcess.spawnSync = function (cmd, args, opts) {
  if (cmd === "taskkill") {
    taskkillCalls.push(args);
    return { error: null, status: 0, stdout: "", stderr: "" };
  }
  return realSpawnSync.call(this, cmd, args, opts);
};

const originalPlatform = Object.getOwnPropertyDescriptor(process, "platform");
Object.defineProperty(process, "platform", { value: "win32", configurable: true });

try {
  // 1. A still-running owned child is tree-killed exactly once, by its pid.
  taskkillCalls = [];
  let client = makeClient();
  let child = fakeChild(4242);
  client.lifecycleChild = child;
  client.lifecyclePid = child.pid;
  assert.strictEqual(client._terminateOwnedChild(child), true);
  assert.deepStrictEqual(
    taskkillCalls,
    [["/PID", "4242", "/T", "/F"]],
    "a live owned child must still be tree-killed",
  );

  // 2. An already-exited child must NEVER be tree-killed: that pid may now
  //    belong to somebody else, and /T would take out their whole tree.
  taskkillCalls = [];
  client = makeClient();
  child = fakeChild(4242, { exitCode: 0 });
  client.lifecycleChild = child;
  client.lifecyclePid = child.pid;
  client._terminateOwnedChild(child);
  assert.deepStrictEqual(taskkillCalls, [], "exited child pid must not be taskkill'd");

  // 3. Same for a child reaped via a signal.
  taskkillCalls = [];
  client = makeClient();
  child = fakeChild(4242, { signalCode: "SIGTERM" });
  client.lifecycleChild = child;
  client.lifecyclePid = child.pid;
  client._terminateOwnedChild(child);
  assert.deepStrictEqual(taskkillCalls, [], "signal-reaped child pid must not be taskkill'd");

  // 4. A child we do not own is never touched at all.
  taskkillCalls = [];
  client = makeClient();
  const owned = fakeChild(1111);
  const foreign = fakeChild(2222);
  client.lifecycleChild = owned;
  client.lifecyclePid = owned.pid;
  assert.strictEqual(client._terminateOwnedChild(foreign), false);
  assert.deepStrictEqual(taskkillCalls, [], "foreign child must never be terminated");

  console.log("windows-recycled-pid-tree-kill: PASS");
} finally {
  childProcess.spawnSync = realSpawnSync;
  if (originalPlatform) Object.defineProperty(process, "platform", originalPlatform);
  Module._resolveFilename = originalResolve;
}
