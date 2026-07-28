const assert = require("assert");
const fs = require("fs");
const Module = require("module");
const os = require("os");
const path = require("path");

const extensionPath = path.resolve(__dirname, "..", "extension.js");

function loadExtensionHost(repoRoot) {
  delete require.cache[extensionPath];
  const originalLoad = Module._load;
  const fakeVscode = {
    workspace: {
      workspaceFolders: [{ name: path.basename(repoRoot), uri: { fsPath: repoRoot, toString: () => `file://${repoRoot}` } }],
      getConfiguration: () => ({ get: () => "", inspect: () => ({}), update: async () => {} }),
    },
    window: {
      createOutputChannel: () => ({ appendLine: () => {}, dispose: () => {} }),
      setStatusBarMessage: () => {},
      showErrorMessage: () => {},
      showInformationMessage: () => {},
    },
    commands: { registerCommand: () => ({ dispose: () => {} }) },
    Uri: { joinPath: (...parts) => ({ fsPath: parts.map((p) => p.fsPath || p).join(path.sep) }) },
    ViewColumn: { Active: 1 },
    ConfigurationTarget: { Global: 1 },
  };
  Module._load = function patchedLoad(request, parent, isMain) {
    if (request === "vscode") return fakeVscode;
    return originalLoad.call(this, request, parent, isMain);
  };
  try {
    const extension = require(extensionPath);
    const context = {
      extensionUri: { fsPath: path.resolve(__dirname, "..") },
      extension: { packageJSON: { version: "0.6.64" } },
      subscriptions: [],
      workspaceState: { update: () => {}, get: () => undefined },
    };
    return { extension, context };
  } finally {
    Module._load = originalLoad;
  }
}

// Unique, self-contained test repo ids so this test's assertions against
// the machine-wide shared router manifest can never collide with other tests.
const REPO_A_ID = `repo_${"b1017".repeat(7).slice(0, 32)}`;
const REPO_FOREIGN_ID = `repo_${"c1017".repeat(7).slice(0, 32)}`;
const VALID_UUID_1 = "11111111-1111-4111-8111-111111111111";
const VALID_UUID_2 = "22222222-2222-4222-8222-222222222222";

function clearDir(dir) {
  fs.rmSync(dir, { recursive: true, force: true });
  fs.mkdirSync(dir, { recursive: true });
}

function writeInstance(instancesDir, name, overrides) {
  const now = Date.now() / 1000;
  const payload = {
    instance_id: name,
    generation_id: `gen-${name}`,
    repo_id: REPO_A_ID,
    pid: 999999,
    parent_pid: process.pid,
    pid_start_time: 1,
    socket_path: path.join(os.tmpdir(), `${name}.sock`),
    capability_path: path.join(os.tmpdir(), `${name}.cap`),
    owned_thread_ids: [],
    active_thread_id: "",
    active_thread_observed_at: now,
    heartbeat_at: now,
    owner_lease_seconds: 90,
    ready: true,
    ...overrides,
  };
  const file = path.join(instancesDir, `${name}.json`);
  fs.writeFileSync(file, JSON.stringify(payload), { encoding: "utf8", mode: 0o600 });
  try {
    fs.chmodSync(file, 0o600);
  } catch (_err) {
    // Best-effort on platforms/filesystems that do not support POSIX chmod.
  }
  return file;
}

(async () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "aiworkhub-mux-ready-"));
  const repoA = path.join(tmp, "alpha");
  fs.mkdirSync(repoA, { recursive: true });

  const muxDir = path.join(tmp, "mux");
  const instancesDir = path.join(muxDir, "instances");
  fs.mkdirSync(instancesDir, { recursive: true });

  const host = loadExtensionHost(repoA);
  const internals = host.extension.__testInternals;
  const muxEnvName = internals.constants.APP_SERVER_MUX_SIDEBAND_DIR_ENV;
  const originalMuxDirEnv = process.env[muxEnvName];
  process.env[muxEnvName] = muxDir;

  const sharedRepoRouteDir = internals.sharedRepoRouteDir();
  const sharedRecordPath = path.join(sharedRepoRouteDir, `${REPO_A_ID}.json`);
  fs.mkdirSync(sharedRepoRouteDir, { recursive: true });
  fs.rmSync(sharedRecordPath, { force: true });

  const repoInfo = { root: repoA, repoId: REPO_A_ID, repoName: "alpha", label: "alpha" };

  try {
    assert.strictEqual(
      internals.appServerMuxInstancesDir(),
      instancesDir,
      "the mux instances directory must honor the sideband-dir override",
    );

    // ── B1017.1: Initial pre-startup state: route_pending (fail-closed) ──
    clearDir(instancesDir);
    let route = internals.refreshCoordinatorRouteOwnership(repoInfo);
    assert.strictEqual(route.targets.codex.route.thread_id, "", "pre-start must never fabricate a thread id");
    assert.strictEqual(route.targets.codex.capability_state, "route_pending", "pre-start must be route_pending");

    // ── B1017.2: Post-ensureStarted convergence -- a ready mux instance
    //    must immediately publish capability_state=available without
    //    waiting for the 4-minute renewal tick ──
    clearDir(instancesDir);
    writeInstance(instancesDir, "one", {
      active_thread_id: VALID_UUID_1,
      owned_thread_ids: [VALID_UUID_1],
    });
    route = internals.refreshCoordinatorRouteOwnership(repoInfo);
    assert.strictEqual(route.targets.codex.route.thread_id, VALID_UUID_1,
      "post-ensureStarted with a ready mux must immediately publish the verified thread id");
    assert.strictEqual(route.targets.codex.route.session_id, VALID_UUID_1);
    assert.strictEqual(route.targets.codex.capability_state, "available",
      "post-ensureStarted with a ready mux must immediately set capability_state=available");
    assert.deepStrictEqual(route.targets.codex.wake, {
      mode: "app_server_sideband",
      supported: true,
    });

    // Persisted to repo-local coordinator target.
    const onDisk = JSON.parse(fs.readFileSync(internals.routeStatePath(repoA), "utf8"));
    assert.strictEqual(onDisk.targets.codex.route.thread_id, VALID_UUID_1,
      "immediate post-ready route publication must persist the verified thread");

    // Published into shared router manifest.
    const sharedVerified = internals.readSharedRepoRouteRecord(REPO_A_ID);
    assert.strictEqual(sharedVerified.targets.codex.route.thread_id, VALID_UUID_1,
      "immediate post-ready route publication must publish into the shared manifest");

    // ── B1017.3: Negative ownership cases remain route_pending ──
    const negativeCases = [
      ["empty active_thread_id", { active_thread_id: "", owned_thread_ids: [] }],
      ["synthetic codex:window id", { active_thread_id: "codex:window_abc", owned_thread_ids: ["codex:window_abc"] }],
      ["wrong repo_id", { repo_id: REPO_FOREIGN_ID, active_thread_id: VALID_UUID_1, owned_thread_ids: [VALID_UUID_1] }],
      ["wrong parent_pid", { parent_pid: process.pid + 1, active_thread_id: VALID_UUID_1, owned_thread_ids: [VALID_UUID_1] }],
      ["stale heartbeat", { active_thread_id: VALID_UUID_1, owned_thread_ids: [VALID_UUID_1], heartbeat_at: Date.now() / 1000 - 500, owner_lease_seconds: 90 }],
      ["not ready", { ready: false, active_thread_id: VALID_UUID_1, owned_thread_ids: [VALID_UUID_1] }],
    ];
    for (const [label, overrides] of negativeCases) {
      clearDir(instancesDir);
      writeInstance(instancesDir, "one", overrides);
      route = internals.refreshCoordinatorRouteOwnership(repoInfo);
      assert.strictEqual(route.targets.codex.route.thread_id, "", `${label} must remain route_pending post-ensureStarted`);
      assert.strictEqual(route.targets.codex.capability_state, "route_pending", `${label} must remain route_pending post-ensureStarted`);
    }

    // ── B1017.4: Ambiguous (two instances) remains route_pending ──
    clearDir(instancesDir);
    writeInstance(instancesDir, "one", { active_thread_id: VALID_UUID_1, owned_thread_ids: [VALID_UUID_1] });
    writeInstance(instancesDir, "two", { active_thread_id: VALID_UUID_2, owned_thread_ids: [VALID_UUID_2] });
    route = internals.refreshCoordinatorRouteOwnership(repoInfo);
    assert.strictEqual(route.targets.codex.route.thread_id, "", "ambiguous owners must remain route_pending post-ensureStarted");
    assert.strictEqual(route.targets.codex.capability_state, "route_pending", "ambiguous owners must remain route_pending post-ensureStarted");

    // ── B1017.5: Convergence back to pending when mux disappears ──
    clearDir(instancesDir);
    route = internals.refreshCoordinatorRouteOwnership(repoInfo);
    assert.strictEqual(route.targets.codex.route.thread_id, "", "route must not remain verified after mux disappears");
    assert.strictEqual(route.targets.codex.capability_state, "route_pending");

    // ── B1017.6: Renewal timer still calls the same function, so the
    //    4-minute tick renewal path is covered by the same convergence ──
    clearDir(instancesDir);
    writeInstance(instancesDir, "renewal", {
      active_thread_id: VALID_UUID_2,
      owned_thread_ids: [VALID_UUID_2],
    });
    route = internals.refreshCoordinatorRouteOwnership(repoInfo);
    assert.strictEqual(route.targets.codex.route.thread_id, VALID_UUID_2, "renewal-tick convergence must also publish immediately");
    assert.strictEqual(route.targets.codex.capability_state, "available");
  } finally {
    if (originalMuxDirEnv === undefined) {
      delete process.env[muxEnvName];
    } else {
      process.env[muxEnvName] = originalMuxDirEnv;
    }
    fs.rmSync(sharedRecordPath, { force: true });
    fs.rmSync(tmp, { recursive: true, force: true });
  }

  console.log("AIWorkHub Codex route publication post-mux-ready B1017 regression passed");
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
