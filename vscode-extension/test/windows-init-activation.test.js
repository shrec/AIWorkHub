"use strict";

const assert = require("assert");
const childProcess = require("child_process");
const { EventEmitter } = require("events");
const Module = require("module");
const path = require("path");

const extensionPath = path.resolve(__dirname, "..", "extension.js");
const originalLoad = Module._load;
const fakeVscode = {
  workspace: {
    workspaceFolders: [],
    getConfiguration: () => ({ get: () => 10000, inspect: () => ({}) }),
  },
  window: {
    createOutputChannel: () => ({ appendLine: () => {}, dispose: () => {} }),
  },
  Uri: { joinPath: (...parts) => ({ fsPath: parts.map((part) => part.fsPath || part).join("/") }) },
  ViewColumn: { Active: 1 },
  ConfigurationTarget: { Global: 1 },
};

Module._load = function patchedLoad(request, parent, isMain) {
  if (request === "vscode") return fakeVscode;
  return originalLoad.call(this, request, parent, isMain);
};

let extension;
try {
  extension = require(extensionPath);
} finally {
  Module._load = originalLoad;
}

(async () => {
  const messages = [];
  let resolveSnapshot;
  let convergenceCalls = 0;
  const initializedClient = {
    repositoryIdentity: {
      uriStr: "file:///C:/work/repo",
      repoId: "manifest-missing",
    },
    claimEpisode: "episode_windows_init",
    callTool: async () => new Promise((resolve) => { resolveSnapshot = resolve; }),
    _convergeBackgroundServices: () => { convergenceCalls += 1; },
  };
  const view = new extension.__testInternals.ViewState((message) => messages.push(message));

  const snapshot = extension.__testInternals.pushSnapshotNoRetry(view, {
    client: initializedClient,
    authoritative: true,
    convergeBackgroundServices: false,
  });

  // A timer/old poll can advance the normal request sequence while Windows is
  // still draining the initialized child. The explicit post-init snapshot is
  // authoritative and must still make storage readiness visible.
  view.snapshotRequestSeq += 1;
  resolveSnapshot({
    repo_id: "repo_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    storage: { ready: true },
    tasks: [],
    summary: {},
  });

  const result = await snapshot;
  assert.strictEqual(result.posted, true);
  assert.strictEqual(result.payload.storage.ready, true);
  assert.strictEqual(messages.filter((message) => message.type === "snapshot").length, 1);
  assert.strictEqual(messages[0].payload.storage.ready, true);
  assert.strictEqual(convergenceCalls, 0, "the pre-rebind child must not restart background services");

  const lifecycleClient = new extension.__testInternals.McpStdioClient(
    "C:\\work\\repo",
    { appendLine: () => {} },
    initializedClient.repositoryIdentity,
    "episode_windows_process_tree",
  );
  let exactChildKillCalls = 0;
  const exactChild = {
    pid: 424242,
    killed: false,
    kill: () => { exactChildKillCalls += 1; },
  };
  lifecycleClient.child = exactChild;
  lifecycleClient.lifecycleChild = exactChild;
  lifecycleClient.lifecyclePid = exactChild.pid;
  const originalSpawnSync = childProcess.spawnSync;
  const taskkillCalls = [];
  childProcess.spawnSync = (command, args, options) => {
    taskkillCalls.push({ command, args, options });
    return { status: 0, error: null };
  };
  try {
    assert.strictEqual(lifecycleClient._terminateOwnedChild(exactChild), true);
  } finally {
    childProcess.spawnSync = originalSpawnSync;
  }
  if (process.platform === "win32") {
    assert.deepStrictEqual(taskkillCalls[0].args, ["/PID", "424242", "/T", "/F"]);
    assert.strictEqual(exactChildKillCalls, 0, "Windows must terminate the owned child tree even without spawnfile metadata");
  } else {
    assert.strictEqual(taskkillCalls.length, 0, "Linux/macOS must retain the existing exact-child kill path");
    assert.strictEqual(exactChildKillCalls, 1);
  }

  const streamClient = new extension.__testInternals.McpStdioClient(
    "C:\\work\\repo",
    { appendLine: () => {} },
    initializedClient.repositoryIdentity,
    "episode_windows_stream_error",
  );
  const streamChild = {
    pid: 434343,
    stdin: new EventEmitter(),
    stdout: new EventEmitter(),
    stderr: new EventEmitter(),
  };
  streamClient.lifecycleChild = streamChild;
  streamClient.lifecyclePid = streamChild.pid;
  streamClient._attachChildStreamErrorGuards(streamChild);
  assert.doesNotThrow(() => streamChild.stdin.emit("error", new Error("EPIPE")));
  assert.doesNotThrow(() => streamChild.stdout.emit("error", new Error("ERR_STREAM_DESTROYED")));
  assert.doesNotThrow(() => streamChild.stderr.emit("error", new Error("ERR_STREAM_DESTROYED")));

  await assert.doesNotReject(() => extension.__testInternals.runBackgroundTask(
    "sync throw regression",
    () => { throw new Error("sync event callback failure"); },
  ));
  await assert.doesNotReject(() => extension.__testInternals.runBackgroundTask(
    "rejection regression",
    () => Promise.reject(new Error("async event callback failure")),
  ));

  console.log("AIWorkHub Windows init activation regression passed");
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
