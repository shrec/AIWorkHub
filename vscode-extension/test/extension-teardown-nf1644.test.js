"use strict";

// NF-2026-01644: inside the Windows AppContainer validation sandbox one hung test
// file held the whole extension suite until the 1800 s validation timeout without
// naming itself, and one rejected shutdown step skipped the rest of deactivate().
// (a) deactivate() still disposes the editor-worker MCP client, flushes the debug
//     trace and removes this window's route record when the dispatcher stop rejects.
// (b) runIsolatedTest bounds a child that never exits (ETIMEDOUT) and run-all's
//     reporter names the hung file and exits non-zero, even when a surviving
//     descendant keeps the scratch busy and its removal fails.
// (c) the Windows interpreter preflight never opens stdio pipes (an AppContainer
//     livelocks libuv's named-pipe creation, so spawnSync's timeout never arms),
//     still reports the candidate's exit code and sanitised stderr, and keeps
//     judging the interpreter alone when no capture file can be created.
const assert = require("assert");
const childProcess = require("child_process");
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const Module = require("module");
const isolation = require("./isolated-test-env");

const extensionPath = path.resolve(__dirname, "..", "extension.js");
const fakeVscode = {
  workspace: { workspaceFolders: [], getConfiguration: () => ({ get: (_key, fallback) => fallback }) },
};
const originalLoad = Module._load;
Module._load = function patchedLoad(request, parent, isMain) {
  if (request === "vscode") return fakeVscode;
  return originalLoad.call(this, request, parent, isMain);
};
let extension;
try {
  delete require.cache[extensionPath];
  extension = require(extensionPath);
} finally {
  Module._load = originalLoad;
}
const internals = extension.__testInternals;

async function deactivateSurvivesRejectedDispatcherStop() {
  const root = isolation.createScratch();
  try {
    // Not a real repository id, so shutdown never consults the shared HOME route record.
    const identity = { root, repoId: "repo_nf1644" };
    const dashboard = new internals.McpStdioClient(root, null, identity, "episode_nf1644");
    const worker = new internals.McpStdioClient(root, null, identity, "episode_nf1644",
      { grantWorkerBridgeWrites: true });
    let dispatcherStops = 0;
    dashboard.stopDispatcherThenTerminate = async () => {
      dispatcherStops += 1;
      throw new Error("nf1644_stop_rejected");
    };
    internals.bindVscodeLmProviderBridgeForTest({
      mcpClient: dashboard, workerMcpClient: worker, activeRepoIdentity: identity,
    });
    const routeRecord = internals.windowRouteStatePath(root, internals.constants.WINDOW_SCOPE_ID);
    fs.mkdirSync(path.dirname(routeRecord), { recursive: true });
    fs.writeFileSync(routeRecord, "{}\n");
    const traceFile = path.join(root, "extension-nf1644.jsonl");
    internals.bindDebugTraceFileForTest(traceFile);
    internals.debugTrace("nf1644.before_deactivate");
    assert.ok(internals.debugTraceBufferedLineCount() > 0, "a trace line is buffered before shutdown");

    await extension.deactivate();

    assert.strictEqual(dispatcherStops, 1, "shutdown attempted the dashboard dispatcher stop");
    assert.strictEqual(worker.intentionalStop, true, "worker client disposed although the dispatcher stop rejected");
    assert.strictEqual(internals.debugTraceBufferedLineCount(), 0, "debug trace flushed although the dispatcher stop rejected");
    assert.ok(fs.readFileSync(traceFile, "utf8").includes("nf1644.before_deactivate"));
    assert.strictEqual(fs.existsSync(routeRecord), false, "window route record removed although the dispatcher stop rejected");
    assert.ok(
      internals.systemLogSnapshot().some((entry) => entry.component === "shutdown"
        && entry.message === "dispatcher stop failed: nf1644_stop_rejected"),
      "the failed step is logged by name, never swallowed",
    );
  } finally {
    internals.bindDebugTraceFileForTest("");
    internals.resetVscodeLmWorkerSourceGraphReadinessForTest();
    isolation.cleanupScratch(root);
  }
}

const timeoutEnv = "AIWORKHUB_EXTENSION_TEST_TIMEOUT_MS";
const runnerSource = fs.readFileSync(path.join(__dirname, "run-all.js"), "utf8");

// Execute the real runner with explicit fake discovery, never recursive suite discovery.
function runRunner(env, runIsolatedTest) {
  const runs = [];
  const errors = [];
  let exitCode = null;
  try {
    vm.runInNewContext(runnerSource, {
      __dirname,
      require(name) {
        if (name === "fs") return { readdirSync: () => [
          { name: "hung-probe.test.js", isFile: () => true },
          { name: "package-vsix-scratch.test.js", isFile: () => true },
        ] };
        if (name === "./isolated-test-env") return { runIsolatedTest(args, options) {
          runs.push({ file: path.basename(args[0]), timeoutMs: options && options.timeoutMs });
          return runIsolatedTest(args, options);
        } };
        return require(name);
      },
      process: { execPath: process.execPath, env, exit(code) { exitCode = code; throw new Error("runner exit"); } },
      console: { log() {}, error(line) { errors.push(String(line)); } },
    });
  } catch (error) {
    if (!error || error.message !== "runner exit") throw error;
  }
  return { runs, errors, exitCode };
}

function hungChildIsBoundedAndNamed() {
  const hung = ["-e", "setInterval(() => {}, 1000);"];
  for (const invalid of [0, -1, 1.5, "1000", Number.NaN]) {
    assert.throws(() => isolation.runIsolatedTest(hung, { timeoutMs: invalid }), /test_timeout_ms_invalid/);
  }
  const bounded = isolation.runIsolatedTest(hung, { timeoutMs: 1000, stdio: "ignore" });
  assert.strictEqual(bounded.error && bounded.error.code, "ETIMEDOUT", "a never-exiting child is bounded");
  assert.strictEqual(bounded.status, null);
  assert.ok(!fs.existsSync(bounded.scratchRoot), "timed-out child scratch cleaned");

  const timedOut = runRunner({ [timeoutEnv]: "1000" },
    (_args, options) => isolation.runIsolatedTest(hung, { ...options, stdio: "ignore" }));
  assert.deepStrictEqual(timedOut.runs, [{ file: "hung-probe.test.js", timeoutMs: 1000 }], "the runner stops at the hung file");
  assert.deepStrictEqual(timedOut.errors, ["hung-probe.test.js timed out after 1000ms"]);
  assert.strictEqual(timedOut.exitCode, 1);

  const passed = runRunner({}, () => ({ status: 0 }));
  assert.deepStrictEqual(passed.runs.map((run) => run.timeoutMs), [300000, 300000], "default per-file bound");
  assert.deepStrictEqual(passed.errors, []);
  assert.strictEqual(passed.exitCode, null);

  for (const invalid of ["0", "-5", "1.5", "soon", ""]) {
    const refused = runRunner({ [timeoutEnv]: invalid }, () => { throw new Error("runner must refuse before any test"); });
    assert.deepStrictEqual(refused.runs, [], invalid);
    assert.strictEqual(refused.exitCode, 1, invalid);
    assert.match(refused.errors[0], /^AIWORKHUB_EXTENSION_TEST_TIMEOUT_MS must be a positive integer/, invalid);
  }

  const notStarted = runRunner({}, () => ({ error: Object.assign(new Error("spawn EACCES"), { code: "EACCES" }), status: null }));
  assert.deepStrictEqual(notStarted.errors, ["Failed to start hung-probe.test.js: spawn EACCES"]);
  assert.strictEqual(notStarted.exitCode, 1);

  const notIsolated = runRunner({}, () => { throw new Error("test_scratch_symlink_or_non_directory"); });
  assert.deepStrictEqual(notIsolated.errors,
    ["hung-probe.test.js could not run in isolation: test_scratch_symlink_or_non_directory"]);
  assert.strictEqual(notIsolated.exitCode, 1);

  // A descendant that outlived the killed child keeps the scratch busy, so the removal fails after the
  // spawn. The child's own ETIMEDOUT outcome must still name the file, not a generic isolation failure.
  const cleanupFailure = (outcome) => Object.assign(
    new Error("EBUSY: resource busy or locked"), { code: "EBUSY", result: outcome });
  const lateTimeout = runRunner({ [timeoutEnv]: "1000" }, () => {
    throw cleanupFailure({ error: Object.assign(new Error("spawnSync ETIMEDOUT"), { code: "ETIMEDOUT" }), status: null });
  });
  assert.deepStrictEqual(lateTimeout.errors,
    ["hung-probe.test.js timed out after 1000ms (scratch cleanup failed: EBUSY: resource busy or locked)"]);
  assert.strictEqual(lateTimeout.exitCode, 1);
  const lateExit = runRunner({}, () => { throw cleanupFailure({ status: 0 }); });
  assert.deepStrictEqual(lateExit.errors,
    ["hung-probe.test.js could not run in isolation: EBUSY: resource busy or locked"]);
  assert.strictEqual(lateExit.exitCode, 1);

  const originalRmSync = fs.rmSync;
  let busy = null;
  fs.rmSync = () => { throw Object.assign(new Error("EBUSY: resource busy or locked"), { code: "EBUSY" }); };
  try {
    isolation.runIsolatedTest(hung, { timeoutMs: 1000, stdio: "ignore" });
  } catch (error) {
    busy = error;
  } finally {
    fs.rmSync = originalRmSync;
  }
  assert.ok(busy && busy.code === "EBUSY" && busy.result, "a failed scratch cleanup carries the spawn result");
  try {
    assert.strictEqual(busy.result.error && busy.result.error.code, "ETIMEDOUT", "the child's own outcome is kept");
    assert.strictEqual(busy.result.status, null);
    assert.ok(fs.existsSync(busy.result.scratchRoot), "the busy scratch is still on disk");
  } finally {
    isolation.cleanupScratch(busy.result.scratchRoot);
  }
}

function preflightCapturesStderrWithoutPipes() {
  const root = isolation.createScratch();
  const captureDir = path.join(root, "tmp");
  fs.mkdirSync(captureDir);
  const tempKeys = ["TMPDIR", "TMP", "TEMP"];
  const savedTemp = tempKeys.map((key) => [key, process.env[key]]);
  const spawned = [];
  const originalSpawnSync = childProcess.spawnSync;
  // Records each spawn and delegates to the real spawnSync. A piped spawn is refused,
  // never delegated, so a regression fails here instead of livelocking the sandbox.
  childProcess.spawnSync = (command, args, options) => {
    const stdio = options && options.stdio;
    spawned.push({ stdio, capture: fs.readdirSync(captureDir) });
    if (!Array.isArray(stdio) || !stdio.every((entry) => entry === "ignore" || Number.isInteger(entry))) {
      throw new Error(`preflight_stdio_pipe_refused:${JSON.stringify(stdio)}`);
    }
    return originalSpawnSync(command, args, options);
  };
  try {
    for (const key of tempKeys) process.env[key] = captureDir;
    // node stands in for python: `-e <script> --` keeps the appended `-c "import ..."` as script arguments.
    const failed = internals._preflightPythonCandidate(process.execPath,
      ["-e", "process.stderr.write('nf1644 broken venv ' + 'k'.repeat(40)); process.exit(3);", "--"], root);
    const passed = internals._preflightPythonCandidate(process.execPath, ["-e", "process.exit(0);", "--"], root);
    const missing = internals._preflightPythonCandidate(path.join(root, "missing-python"), [], root);

    assert.strictEqual(spawned.length, 3);
    for (const { stdio, capture } of spawned) {
      assert.deepStrictEqual(stdio.slice(0, 2), ["ignore", "ignore"], "no stdin/stdout pipes");
      assert.ok(Number.isInteger(stdio[2]), "stderr is a capture file descriptor, not a pipe");
      assert.strictEqual(capture.length, 1);
      assert.match(capture[0], /^aiworkhub-preflight-\d+-[0-9a-f]{16}\.stderr$/);
    }
    assert.deepStrictEqual(fs.readdirSync(captureDir), [], "every stderr capture file is removed");
    assert.deepStrictEqual(Object.keys(failed).sort(), ["candidate", "exitCode", "ok", "signal", "stderrTail"]);
    assert.strictEqual(failed.ok, false);
    assert.strictEqual(failed.exitCode, 3);
    assert.strictEqual(failed.signal, null);
    assert.match(failed.stderrTail, /nf1644 broken venv \[REDACTED\]/, "stderr captured and sanitised");
    assert.deepStrictEqual(passed, { ok: true, candidate: process.execPath });
    assert.deepStrictEqual(Object.keys(missing).sort(), ["candidate", "error", "ok"]);
    assert.match(missing.error, /ENOENT/);

    // No capture file: the verdict still depends only on the interpreter, stderr is dropped
    // (never piped), and the diagnostic never names the host temp path.
    for (const key of tempKeys) process.env[key] = path.join(root, "missing-tmp");
    const uncapturedPass = internals._preflightPythonCandidate(process.execPath, ["-e", "process.exit(0);", "--"], root);
    const uncapturedFail = internals._preflightPythonCandidate(process.execPath, ["-e", "process.exit(3);", "--"], root);
    assert.deepStrictEqual(uncapturedPass, { ok: true, candidate: process.execPath });
    assert.deepStrictEqual(Object.keys(uncapturedFail).sort(), ["candidate", "exitCode", "ok", "signal", "stderrTail"]);
    assert.strictEqual(uncapturedFail.ok, false);
    assert.strictEqual(uncapturedFail.exitCode, 3);
    assert.match(uncapturedFail.stderrTail, /no stderr output/);
    assert.ok(!JSON.stringify(uncapturedFail).includes("missing-tmp"), "no host temp path in the diagnostic");
    assert.strictEqual(spawned.length, 5, "the interpreter still runs without a capture file");
    for (const { stdio } of spawned.slice(3)) {
      assert.deepStrictEqual(stdio, ["ignore", "ignore", "ignore"], "stderr is dropped, never piped");
    }
    assert.ok(!fs.existsSync(path.join(root, "missing-tmp")), "no capture file or directory appears");
  } finally {
    childProcess.spawnSync = originalSpawnSync;
    for (const [key, value] of savedTemp) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
    isolation.cleanupScratch(root);
  }
}

async function main() {
  await deactivateSurvivesRejectedDispatcherStop();
  hungChildIsBoundedAndNamed();
  preflightCapturesStderrWithoutPipes();
}

// Fail closed: a promise that never settles must not exit 0 once the event loop drains.
process.exitCode = 1;
main().then(() => {
  process.exitCode = 0;
  console.log("extension teardown NF-2026-01644: ok");
}, (err) => {
  console.error(err);
  process.exitCode = 1;
});
