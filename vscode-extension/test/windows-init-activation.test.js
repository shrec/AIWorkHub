"use strict";

const assert = require("assert");
const childProcess = require("child_process");
const { EventEmitter } = require("events");
const fs = require("fs");
const Module = require("module");
const os = require("os");
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
  const fixtureRoot = fs.mkdtempSync(path.join(os.tmpdir(), "C_drive-work-repo-"));
  const hubRoot = path.join(fixtureRoot, ".aiworkhub");
  const manifestPath = path.join(hubRoot, "project.json");
  fs.mkdirSync(hubRoot);
  const manifest = {
    schema_id: "aiworkhub.project_manifest.v1",
    manifest_version: 1,
    layout_version: 1,
    repo_id: "repo_cccccccccccccccccccccccccccccccc",
    repo_name: "Windows Repo",
    layout: {
      durable: { tasking: "tasking", source_graph: "source_graph", sessions: "sessions", memory: "memory", kb: "kb", config: "config" },
      runtime: { path: "runtime", durable: false, ignored: true },
    },
  };
  const manifestBytes = Buffer.from(`\ufeff${JSON.stringify(manifest)}\n`, "utf8");
  fs.writeFileSync(manifestPath, manifestBytes);
  const readManifest = extension.__testInternals.readRepositoryManifestInfo;
  assert.strictEqual(readManifest(fixtureRoot, "repo").repoId, manifest.repo_id);
  assert.deepStrictEqual(fs.readFileSync(manifestPath), manifestBytes, "discovery must not rewrite the manifest");

  const reorderedManifest = {
    ...manifest,
    layout: {
      ...manifest.layout,
      durable: { config: "config", kb: "kb", memory: "memory", sessions: "sessions", source_graph: "source_graph", tasking: "tasking" },
    },
  };
  const reorderedBytes = Buffer.from(JSON.stringify(reorderedManifest), "utf8");
  fs.writeFileSync(manifestPath, reorderedBytes);
  assert.strictEqual(readManifest(fixtureRoot, "repo").repoId, manifest.repo_id);
  assert.deepStrictEqual(fs.readFileSync(manifestPath), reorderedBytes, "reordered discovery must not rewrite the manifest");

  fs.writeFileSync(manifestPath, "{not json", "utf8");
  assert.strictEqual(readManifest(fixtureRoot, "repo").storageStatus, "manifest_invalid");
  fs.writeFileSync(manifestPath, JSON.stringify({ ...manifest, repo_id: "bad" }), "utf8");
  assert.strictEqual(readManifest(fixtureRoot, "repo").storageStatus, "repo_id_invalid");
  fs.unlinkSync(manifestPath);
  assert.strictEqual(readManifest(fixtureRoot, "repo").storageStatus, "uninitialized");

  const externalRoot = fs.mkdtempSync(path.join(os.tmpdir(), "D_drive-external-repo-"));
  fs.rmdirSync(hubRoot);
  fs.symlinkSync(externalRoot, hubRoot, "dir");
  assert.strictEqual(
    readManifest(fixtureRoot, "repo").storageStatus,
    "manifest_invalid",
    "a hub directory symlink to an empty external directory must never look uninitialized",
  );
  assert.deepStrictEqual(
    fs.readdirSync(externalRoot),
    [],
    "hub symlink rejection must not mutate the external directory",
  );
  fs.unlinkSync(hubRoot);
  fs.mkdirSync(hubRoot);

  const externalManifestPath = path.join(externalRoot, "project.json");
  fs.writeFileSync(externalManifestPath, manifestBytes);
  fs.symlinkSync(externalManifestPath, manifestPath, "file");
  assert.strictEqual(
    readManifest(fixtureRoot, "repo").storageStatus,
    "manifest_invalid",
    "a manifest symlink must never adopt external repository identity",
  );
  assert.deepStrictEqual(
    fs.readFileSync(externalManifestPath),
    manifestBytes,
    "symlink rejection must not mutate the external manifest",
  );
  fs.unlinkSync(manifestPath);
  fs.writeFileSync(manifestPath, manifestBytes);

  const externalRaceManifest = path.join(externalRoot, "race-project.json");
  const externalRace = { ...manifest, repo_id: "repo_dddddddddddddddddddddddddddddddd" };
  fs.writeFileSync(externalRaceManifest, JSON.stringify(externalRace), "utf8");
  const originalOpenSync = fs.openSync;
  let barrierRan = false;
  fs.openSync = (file, ...args) => {
    if (file === manifestPath && !barrierRan) {
      barrierRan = true;
      fs.unlinkSync(manifestPath);
      fs.renameSync(externalRaceManifest, manifestPath);
    }
    return originalOpenSync(file, ...args);
  };
  try {
    const raced = readManifest(fixtureRoot, "repo");
    assert.strictEqual(barrierRan, true, "the deterministic pre-open replacement barrier must run");
    assert.strictEqual(raced.storageStatus, "manifest_unreadable");
    assert.notStrictEqual(raced.repoId, externalRace.repo_id, "replacement identity must never be adopted");
  } finally {
    fs.openSync = originalOpenSync;
  }
  assert.strictEqual(fs.existsSync(manifestPath), true, "race replacement must leave a manifest fixture");

  const originalReadFileSync = fs.readFileSync;
  const originalDeniedOpenSync = fs.openSync;
  let deniedDescriptor;
  fs.openSync = (file, ...args) => {
    const descriptor = originalDeniedOpenSync(file, ...args);
    if (file === manifestPath) deniedDescriptor = descriptor;
    return descriptor;
  };
  fs.readFileSync = (file, ...args) => {
    if (file === deniedDescriptor) {
      const error = new Error("access denied");
      error.code = "EACCES";
      throw error;
    }
    return originalReadFileSync(file, ...args);
  };
  try {
    assert.strictEqual(readManifest(fixtureRoot, "repo").storageStatus, "manifest_unreadable");
  } finally {
    fs.openSync = originalDeniedOpenSync;
    fs.readFileSync = originalReadFileSync;
    fs.rmSync(fixtureRoot, { recursive: true, force: true });
    fs.rmSync(externalRoot, { recursive: true, force: true });
  }

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
