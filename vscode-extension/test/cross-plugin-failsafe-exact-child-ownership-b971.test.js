const assert = require("assert");
const fs = require("fs");
const path = require("path");
const Module = require("module");
const { EventEmitter } = require("events");

const extensionPath = path.resolve(__dirname, "..", "extension.js");
const source = fs.readFileSync(extensionPath, "utf8");

for (const forbidden of [
  'getConfiguration("chatgpt")',
  'cliExecutable',
  "classifyImmediateCodexChildren",
  "process.kill(",
  "pkill",
  "killall",
  "AIWORKHUB_CALLBACK_TRANSPORT: \"sideband\"",
  "bindCodexSidebandEnvironment",
]) {
  assert.ok(!source.includes(forbidden), `foreign integration remains: ${forbidden}`);
}

const fakeVscode = {
  workspace: {
    workspaceFolders: [],
    getConfiguration: () => ({ get: (_key, fallback) => fallback }),
  },
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

function child(pid) {
  const proc = new EventEmitter();
  proc.pid = pid;
  proc.killed = false;
  proc.killCount = 0;
  proc.kill = () => {
    proc.killCount += 1;
    proc.killed = true;
  };
  proc.stdin = { write: (_payload, callback) => callback && callback() };
  proc.stdout = new EventEmitter();
  proc.stderr = new EventEmitter();
  return proc;
}

const client = new internals.McpStdioClient(
  "/repo/a",
  { appendLine() {} },
  { repoId: "repo_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", uriStr: "file:///repo/a" },
  "episode-a",
);
const owned = child(41);
client.child = owned;
client.lifecycleChild = owned;
client.lifecyclePid = 41;

const foreignDifferentPid = child(42);
assert.strictEqual(client._terminateOwnedChild(foreignDifferentPid), false);
assert.strictEqual(foreignDifferentPid.killCount, 0);

const foreignSamePid = child(41);
assert.strictEqual(client._terminateOwnedChild(foreignSamePid), false);
assert.strictEqual(foreignSamePid.killCount, 0);
assert.strictEqual(client._terminateOwnedChild(owned), true);
assert.strictEqual(owned.killCount, 1);
assert.strictEqual(client.lifecycleChild, null);
assert.strictEqual(client.lifecyclePid, null);
assert.strictEqual(client._terminateOwnedChild(owned), false, "stale ownership must not authorize a second kill");

client.beginExplicitRecovery();
assert.deepStrictEqual(client.recoveryStatus(), {
  category: "manual_retry",
  reason: "",
  attempts: 0,
  maxAttempts: 3,
  open: false,
});
client.recovery.open = true;
client.recovery.attempts = 3;
assert.rejects(client.ensureStarted(), /mcp_recovery_circuit_open/);
client.beginExplicitRecovery();
assert.strictEqual(client.recovery.open, false);
assert.strictEqual(client.recovery.attempts, 0);

assert.strictEqual(internals.constants.MCP_MAX_RUNTIME_REPAIR_ATTEMPTS, 3);
assert.strictEqual(internals.constants.MCP_SNAPSHOT_RECOVERY_ATTEMPTS, 3);
console.log("cross-plugin exact child ownership B971: ok");
