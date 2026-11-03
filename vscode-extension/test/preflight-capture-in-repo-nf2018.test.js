"use strict";

// NF-2026-02018 (owner rule NF-2026-02004): nothing AIWorkHub needs for a repository
// may live outside that repository folder. The Windows interpreter preflight's child
// stderr capture therefore lives under <root>/.aiworkhub/runtime/tmp/ when a
// repository root is known, never under os.tmpdir(), and is removed after every
// verdict (success, failure, spawn error, spawn timeout). Without a root the
// os.tmpdir() fallback is unchanged and still cleaned up. findPythonCommand passes
// its workspace root to both preflight call sites.
const assert = require("assert");
const childProcess = require("child_process");
const fs = require("fs");
const path = require("path");
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

const CAPTURE_NAME = /^aiworkhub-preflight-\d+-[0-9a-f]{16}\.stderr$/;
const TEMP_KEYS = ["TMPDIR", "TMP", "TEMP"];

function listing(dir) {
  return fs.existsSync(dir) ? fs.readdirSync(dir) : [];
}

// Runs fn with os.tmpdir() redirected to tmpDir and spawnSync recording which
// capture files exist at spawn time. `fake(command)` may return a canned result.
function withRecordedSpawns(tmpDir, repoDir, fake, fn) {
  const savedTemp = TEMP_KEYS.map((key) => [key, process.env[key]]);
  const originalSpawnSync = childProcess.spawnSync;
  const spawned = [];
  childProcess.spawnSync = (command, args, options) => {
    spawned.push({ command, stdio: options && options.stdio, repo: listing(repoDir), tmp: listing(tmpDir) });
    const canned = fake ? fake(command) : null;
    return canned || originalSpawnSync(command, args, options);
  };
  try {
    for (const key of TEMP_KEYS) process.env[key] = tmpDir;
    fn();
    return spawned;
  } finally {
    childProcess.spawnSync = originalSpawnSync;
    for (const [key, value] of savedTemp) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  }
}

function assertInRepoCapture(spawned, count) {
  assert.strictEqual(spawned.length, count);
  for (const { stdio, repo, tmp } of spawned) {
    assert.ok(Number.isInteger(stdio[2]), "stderr is a capture file descriptor");
    assert.strictEqual(repo.length, 1, "the capture file is inside <root>/.aiworkhub/runtime/tmp");
    assert.match(repo[0], CAPTURE_NAME);
    assert.deepStrictEqual(tmp, [], "nothing is created under os.tmpdir()");
  }
}

function captureLivesInRepositoryAndIsRemoved() {
  const root = isolation.createScratch();
  try {
    const tmpDir = path.join(root, "host-tmp");
    fs.mkdirSync(tmpDir);
    const repoDir = path.join(root, ".aiworkhub", "runtime", "tmp");
    const timeoutCommand = path.join(root, "slow-python");
    const results = {};
    const spawned = withRecordedSpawns(tmpDir, repoDir,
      (command) => (command === timeoutCommand ? { error: Object.assign(new Error("spawnSync ETIMEDOUT"), { code: "ETIMEDOUT" }) } : null),
      () => {
        results.failed = internals._preflightPythonCandidate(process.execPath,
          ["-e", "process.stderr.write('nf2018 broken venv'); process.exit(3);", "--"], root, root);
        results.passed = internals._preflightPythonCandidate(process.execPath, ["-e", "process.exit(0);", "--"], root, root);
        results.missing = internals._preflightPythonCandidate(path.join(root, "missing-python"), [], root, root);
        results.timedOut = internals._preflightPythonCandidate(timeoutCommand, [], root, root);
      });
    assertInRepoCapture(spawned, 4);
    assert.deepStrictEqual(listing(repoDir), [], "every in-repo capture file is removed");
    assert.deepStrictEqual(listing(tmpDir), []);
    assert.strictEqual(results.failed.ok, false);
    assert.strictEqual(results.failed.exitCode, 3);
    assert.match(results.failed.stderrTail, /nf2018 broken venv/, "stderr still captured from the in-repo file");
    assert.deepStrictEqual(results.passed, { ok: true, candidate: process.execPath });
    assert.match(results.missing.error, /ENOENT/);
    assert.strictEqual(results.timedOut.timedOut, true);
  } finally {
    isolation.cleanupScratch(root);
  }
}

function noRepositoryFallsBackToTmpdir() {
  const root = isolation.createScratch();
  try {
    const tmpDir = path.join(root, "host-tmp");
    fs.mkdirSync(tmpDir);
    let passed;
    // NO_REPO: no repository root is known, so the host temp dir is the only location.
    const spawned = withRecordedSpawns(tmpDir, path.join(root, ".aiworkhub", "runtime", "tmp"), null, () => {
      passed = internals._preflightPythonCandidate(process.execPath, ["-e", "process.exit(0);", "--"], root);
    });
    assert.strictEqual(spawned.length, 1);
    assert.strictEqual(spawned[0].tmp.length, 1, "without a root the capture uses os.tmpdir()");
    assert.match(spawned[0].tmp[0], CAPTURE_NAME);
    assert.deepStrictEqual(spawned[0].repo, []);
    assert.deepStrictEqual(listing(tmpDir), [], "the tmpdir capture is still removed");
    assert.ok(!fs.existsSync(path.join(root, ".aiworkhub")), "no repository directory is created without a root");
    assert.deepStrictEqual(passed, { ok: true, candidate: process.execPath });
  } finally {
    isolation.cleanupScratch(root);
  }
}

function resolverPassesWorkspaceRoot() {
  const root = isolation.createScratch();
  const platform = Object.getOwnPropertyDescriptor(process, "platform");
  try {
    const tmpDir = path.join(root, "host-tmp");
    fs.mkdirSync(tmpDir);
    const venvPython = path.join(root, ".venv", "Scripts", "python.exe");
    fs.mkdirSync(path.dirname(venvPython), { recursive: true });
    fs.writeFileSync(venvPython, "");
    const repoDir = path.join(root, ".aiworkhub", "runtime", "tmp");
    let resolved;
    Object.defineProperty(process, "platform", { ...platform, value: "win32" });
    const spawned = withRecordedSpawns(tmpDir, repoDir, () => ({ status: 1, signal: null }), () => {
      resolved = internals.findPythonCommand(root);
    });
    // The repo venv candidate, then the py -3 and python fallbacks: all capture in-repo.
    assert.deepStrictEqual(spawned.map((entry) => entry.command), [venvPython, "py", "python"]);
    assertInRepoCapture(spawned, 3);
    assert.deepStrictEqual(listing(repoDir), [], "every resolver capture file is removed");
    assert.strictEqual(resolved.command, venvPython);
  } finally {
    Object.defineProperty(process, "platform", platform);
    isolation.cleanupScratch(root);
  }
}

captureLivesInRepositoryAndIsRemoved();
noRepositoryFallsBackToTmpdir();
resolverPassesWorkspaceRoot();
console.log("preflight capture in repository NF-2026-02018: ok");
