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
      extension: { packageJSON: { version: "0.6.63" } },
      subscriptions: [],
      workspaceState: { update: () => {}, get: () => undefined },
    };
    return { extension, context };
  } finally {
    Module._load = originalLoad;
  }
}

// Unique, self-contained test repo ids (never shared with other test files'
// "repo_aaaa..."/"repo_bbbb..." fixtures) so this test's assertions against
// the machine-wide shared router manifest can never collide with, or be
// polluted by, leftover state another test file wrote for the same id.
const REPO_A_ID = `repo_${"b1008".repeat(7).slice(0, 32)}`;
const REPO_FOREIGN_ID = `repo_${"c1008".repeat(7).slice(0, 32)}`;
const VALID_UUID_1 = "11111111-1111-4111-8111-111111111111";
const VALID_UUID_2 = "22222222-2222-4222-8222-222222222222";
const FOREIGN_UUID = "33333333-3333-4333-8333-333333333333";
const FOREIGN_WINDOW_ID = "window_foreignb1008foreignb1008";

function clearDir(dir) {
  fs.rmSync(dir, { recursive: true, force: true });
  fs.mkdirSync(dir, { recursive: true });
}

// Mirrors src/aiworkhub/app_server_mux.py's per-instance registry descriptor
// shape (see _write_registry_descriptor / SidebandInstance).
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
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "aiworkhub-mux-route-"));
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

    // ── No mux instance observed yet: the route stays pending, never fabricated ──
    clearDir(instancesDir);
    let route = internals.refreshCoordinatorRouteOwnership(repoInfo);
    assert.strictEqual(route.targets.codex.route.thread_id, "", "no observation must never fabricate a thread id");
    assert.strictEqual(route.targets.codex.capability_state, "route_pending");

    // ── Negative ownership cases: each must leave the route route_pending ──
    const negativeCases = [
      ["empty active_thread_id", { active_thread_id: "", owned_thread_ids: [] }],
      ["synthetic codex:window id (not a UUID)", { active_thread_id: "codex:window_abc", owned_thread_ids: ["codex:window_abc"] }],
      ["wrong repo_id", { repo_id: REPO_FOREIGN_ID, active_thread_id: VALID_UUID_1, owned_thread_ids: [VALID_UUID_1] }],
      ["wrong extension host / window (parent_pid mismatch)", { parent_pid: process.pid + 1, active_thread_id: VALID_UUID_1, owned_thread_ids: [VALID_UUID_1] }],
      ["stale heartbeat beyond owner_lease_seconds", { active_thread_id: VALID_UUID_1, owned_thread_ids: [VALID_UUID_1], heartbeat_at: Date.now() / 1000 - 500, owner_lease_seconds: 90 }],
      ["not ready", { ready: false, active_thread_id: VALID_UUID_1, owned_thread_ids: [VALID_UUID_1] }],
    ];
    for (const [label, overrides] of negativeCases) {
      clearDir(instancesDir);
      writeInstance(instancesDir, "one", overrides);
      route = internals.refreshCoordinatorRouteOwnership(repoInfo);
      assert.strictEqual(route.targets.codex.route.thread_id, "", `${label} must remain route_pending`);
      assert.strictEqual(route.targets.codex.capability_state, "route_pending", `${label} must remain route_pending`);
    }

    // ── Ambiguous: two live/fresh/ready/matching-owner instances -- never guess ──
    clearDir(instancesDir);
    writeInstance(instancesDir, "one", { active_thread_id: VALID_UUID_1, owned_thread_ids: [VALID_UUID_1] });
    writeInstance(instancesDir, "two", { active_thread_id: VALID_UUID_2, owned_thread_ids: [VALID_UUID_2] });
    route = internals.refreshCoordinatorRouteOwnership(repoInfo);
    assert.strictEqual(route.targets.codex.route.thread_id, "", "ambiguous owners must remain route_pending");
    assert.strictEqual(route.targets.codex.capability_state, "route_pending");

    // ── pending -> verified: exactly one fresh, ready, extension-owned mux instance ──
    clearDir(instancesDir);
    writeInstance(instancesDir, "one", { active_thread_id: VALID_UUID_1, owned_thread_ids: [VALID_UUID_1] });
    route = internals.refreshCoordinatorRouteOwnership(repoInfo);
    assert.strictEqual(route.targets.codex.route.thread_id, VALID_UUID_1);
    assert.strictEqual(route.targets.codex.route.session_id, VALID_UUID_1);
    assert.strictEqual(route.targets.codex.capability_state, "available");
    assert.deepStrictEqual(route.targets.codex.wake, { mode: "app_server_sideband", supported: true });

    const onDisk = JSON.parse(fs.readFileSync(internals.routeStatePath(repoA), "utf8"));
    assert.strictEqual(
      onDisk.targets.codex.route.thread_id,
      VALID_UUID_1,
      "the verified thread id must be persisted to the repo-local coordinator target",
    );

    const sharedVerified = internals.readSharedRepoRouteRecord(REPO_A_ID);
    assert.strictEqual(
      sharedVerified.targets.codex.route.thread_id,
      VALID_UUID_1,
      "the verified thread id must be published into the shared router manifest",
    );

    // ── Convergence: the mux instance disappears -> the route reverts to pending ──
    clearDir(instancesDir);
    route = internals.refreshCoordinatorRouteOwnership(repoInfo);
    assert.strictEqual(
      route.targets.codex.route.thread_id,
      "",
      "a route must never remain 'verified' once its live mux evidence is gone",
    );
    assert.strictEqual(route.targets.codex.capability_state, "route_pending");
    const sharedAfterLoss = internals.readSharedRepoRouteRecord(REPO_A_ID);
    assert.strictEqual(
      sharedAfterLoss.targets.codex.route.thread_id,
      "",
      "the shared manifest must converge back to pending for this window's own repo entry",
    );

    // ── Shared-manifest split-brain: a different, still-live window's verified
    //    route must never be downgraded by this window's own pending observation ──
    const now = new Date();
    const foreignRecord = {
      schema_id: "aiworkhub.shared_repo_route.v1",
      repo_id: REPO_A_ID,
      repo_name: "alpha",
      repo_root: repoA,
      window_id: FOREIGN_WINDOW_ID,
      extension_host_pid: process.pid + 5000,
      selected_provider: "codex",
      targets: {
        codex: {
          provider: "codex",
          capability_state: "available",
          route: { repo_id: REPO_A_ID, window_id: FOREIGN_WINDOW_ID, claim_episode: "episode_foreign", thread_id: FOREIGN_UUID, session_id: FOREIGN_UUID },
          wake: { mode: "app_server_sideband", supported: true },
        },
        claude: { provider: "claude", capability_state: "callback_required", route: {}, wake: {} },
      },
      updated_at: now.toISOString(),
      lease_expires_at: new Date(now.getTime() + 15 * 60 * 1000).toISOString(),
    };
    fs.writeFileSync(sharedRecordPath, JSON.stringify(foreignRecord), "utf8");

    clearDir(instancesDir); // this window has no verified mux of its own right now
    route = internals.refreshCoordinatorRouteOwnership(repoInfo);
    assert.strictEqual(route.targets.codex.route.thread_id, "", "this window's own repo-local route stays scoped to its own observation");
    const sharedStillForeign = internals.readSharedRepoRouteRecord(REPO_A_ID);
    assert.strictEqual(
      sharedStillForeign.window_id,
      FOREIGN_WINDOW_ID,
      "a foreign window's fresh verified route must never be overwritten by this window's pending observation",
    );
    assert.strictEqual(sharedStillForeign.targets.codex.route.thread_id, FOREIGN_UUID);

    // Once this window is itself verified, it may take over the shared manifest.
    writeInstance(instancesDir, "one", { active_thread_id: VALID_UUID_1, owned_thread_ids: [VALID_UUID_1] });
    route = internals.refreshCoordinatorRouteOwnership(repoInfo);
    assert.strictEqual(route.targets.codex.route.thread_id, VALID_UUID_1);
    const sharedTakenOver = internals.readSharedRepoRouteRecord(REPO_A_ID);
    assert.notStrictEqual(
      sharedTakenOver.window_id,
      FOREIGN_WINDOW_ID,
      "this window's own newly-verified route may take over the shared manifest",
    );
    assert.strictEqual(sharedTakenOver.targets.codex.route.thread_id, VALID_UUID_1);

    // ── Expired foreign lease: a stale foreign record must not block convergence forever ──
    fs.writeFileSync(
      sharedRecordPath,
      JSON.stringify({ ...foreignRecord, lease_expires_at: new Date(now.getTime() - 1000).toISOString() }),
      "utf8",
    );
    clearDir(instancesDir); // this window is pending again
    route = internals.refreshCoordinatorRouteOwnership(repoInfo);
    const sharedAfterExpiry = internals.readSharedRepoRouteRecord(REPO_A_ID);
    assert.notStrictEqual(
      sharedAfterExpiry.window_id,
      FOREIGN_WINDOW_ID,
      "an expired foreign lease must not block this window's own convergence write",
    );
  } finally {
    if (originalMuxDirEnv === undefined) {
      delete process.env[muxEnvName];
    } else {
      process.env[muxEnvName] = originalMuxDirEnv;
    }
    fs.rmSync(sharedRecordPath, { force: true });
  }

  console.log("AIWorkHub Codex route publication (pending -> verified) regression passed");
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
