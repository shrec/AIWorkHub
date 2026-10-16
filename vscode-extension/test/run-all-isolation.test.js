"use strict";

const assert = require("assert");
const fs = require("fs");
const os = require("os");
const path = require("path");
const vm = require("vm");
const { spawnSync } = require("child_process");
const isolation = require("./isolated-test-env");

// File-descriptor capture only: pipe stdio livelocks in the validation sandbox.
function captured(run) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "aiworkhub-isolation-capture-"));
  try {
    const outPath = path.join(dir, "stdout.txt"), errPath = path.join(dir, "stderr.txt");
    const outFd = fs.openSync(outPath, "w");
    let errFd = null;
    let result;
    try {
      errFd = fs.openSync(errPath, "w");
      result = run(["ignore", outFd, errFd]);
    } finally {
      fs.closeSync(outFd);
      if (errFd !== null) fs.closeSync(errFd);
    }
    return { ...result, stdout: fs.readFileSync(outPath, "utf8"), stderr: fs.readFileSync(errPath, "utf8") };
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
}

const parent = { ...process.env };
const sentinel = isolation.createScratch();
try {
  const hostile = isolation.childEnvironment(sentinel);
  const sentinelFile = path.join(sentinel, "owner-sentinel.txt");
  fs.writeFileSync(sentinelFile, "owner state\n");
  const configSentinels = [path.join(hostile.CODEX_HOME, "config.toml"), hostile.OPENCODE_CONFIG,
    path.join(hostile.AIWORKHUB_APP_SERVER_MUX_SIDEBAND_DIR, "owner-state"),
    path.join(hostile.AIWORKHUB_VSCODE_LM_BRIDGE_ROOT, "owner-state")];
  for (const target of configSentinels) fs.writeFileSync(target, "owner config\n");
  const source = `
    const fs = require("fs"), path = require("path"), os = require("os");
    const isolation = require(${JSON.stringify(path.join(__dirname, "isolated-test-env.js"))});
    const scratch = isolation.verifyChildEnvironment();
    const Module = require("module"), original = Module._load;
    Module._load = function (request, ...rest) {
      return request === "vscode" ? {} : original.call(this, request, ...rest);
    };
    const api = require(${JSON.stringify(path.join(__dirname, "..", "extension.js"))}).__testInternals;
    const resolved = [os.homedir(), os.tmpdir(), api.sharedRepoRouteDir(),
      api.resolveCodexConfigTomlPath(process.env), api.resolveOpencodeConfigJsonPath(process.env)];
    for (const target of resolved) {
      const relative = path.relative(scratch, target);
      if (!relative || relative.startsWith("..") || path.isAbsolute(relative)) throw new Error("escaped scratch");
      fs.mkdirSync(path.extname(target) ? path.dirname(target) : target, {recursive:true});
    }
    fs.writeFileSync(path.join(api.sharedRepoRouteDir(), "synthetic.json"), "{}");
    fs.writeFileSync(api.resolveCodexConfigTomlPath(process.env), "fixture codex");
    fs.writeFileSync(api.resolveOpencodeConfigJsonPath(process.env), "fixture opencode");
    for (const key of ["AIWORKHUB_APP_SERVER_MUX_SIDEBAND_DIR", "AIWORKHUB_VSCODE_LM_BRIDGE_ROOT"])
      fs.writeFileSync(path.join(process.env[key], "owner-state"), "fixture state");
    process.stdout.write(JSON.stringify({scratch, resolved}));
    process.exit(Number(process.argv[1]));
  `;
  const first = captured((stdio) => isolation.runIsolatedTest(["-e", source, "0"], { env: hostile, stdio }));
  assert.strictEqual(first.status, 0, first.stderr);
  const evidence = JSON.parse(first.stdout);
  assert.notStrictEqual(evidence.scratch, sentinel);
  assert.ok(!fs.existsSync(first.scratchRoot), "successful child scratch cleaned");
  const failed = captured((stdio) => isolation.runIsolatedTest(["-e", source, "7"], { env: hostile, stdio }));
  assert.strictEqual(failed.status, 7, failed.stderr);
  assert.notStrictEqual(failed.scratchRoot, first.scratchRoot);
  assert.ok(!fs.existsSync(failed.scratchRoot), "failed child scratch cleaned");
  assert.strictEqual(fs.readFileSync(sentinelFile, "utf8"), "owner state\n");
  for (const target of configSentinels) assert.strictEqual(fs.readFileSync(target, "utf8"), "owner config\n");
  assert.ok(!fs.existsSync(path.join(sentinel, "home", ".aiworkhub")), "hostile inherited home untouched");
  assert.deepStrictEqual({ ...process.env }, parent, "parent environment unchanged");
  // Exercise the real runner with explicit fake discovery, never recursive suite discovery.
  const runnerSource = fs.readFileSync(path.join(__dirname, "run-all.js"), "utf8");
  let runnerChildren = 0;
  let runnerExit = null;
  vm.runInNewContext(runnerSource, {
    __dirname,
    require(name) {
      if (name === "fs") return { readdirSync: () => [
        { name: "explicit-probe.test.js", isFile: () => true },
        { name: "package-vsix-scratch.test.js", isFile: () => true },
      ] };
      if (name === "./isolated-test-env") return { runIsolatedTest(args) {
        assert.ok(args[0].endsWith(".test.js"));
        runnerChildren += 1;
        return captured((stdio) => isolation.runIsolatedTest(["-e", source, "0"], { env: hostile, stdio }));
      } };
      if (name === "child_process") return { spawnSync(_command, _args, options) {
        isolation.verifyChildEnvironment(options.env);
        throw new Error("runner bypassed isolated test helper");
      } };
      return require(name);
    },
    process: { execPath: process.execPath, env: hostile, exit(code) { runnerExit = code; throw new Error("runner exit"); } },
    console: { log() {}, error() {} },
  });
  assert.strictEqual(runnerChildren, 2, "all discovered tests run through isolation");
  assert.strictEqual(runnerExit, null);
  assert.throws(() => isolation.cleanupScratch(isolation.scratchBase), /not_owned_case/);
  assert.throws(() => isolation.cleanupScratch(path.dirname(isolation.scratchBase)), /not_owned_case/);
  const linkCase = isolation.createScratch();
  fs.rmdirSync(linkCase); // exact empty directory owned by this test; no recursion
  fs.symlinkSync(sentinel, linkCase, process.platform === "win32" ? "junction" : "dir");
  const observable = fs.lstatSync(linkCase).isSymbolicLink();
  if (!observable) {
    fs.rmdirSync(linkCase); // removes the reparse point only, never its target
    console.log("junction probe skipped: link not observable");
  }
  try {
    if (observable) {
      assert.throws(() => isolation.cleanupScratch(linkCase), /symlink_or_non_directory/);
      assert.strictEqual(fs.readFileSync(sentinelFile, "utf8"), "owner state\n");
    }
  } finally {
    // A consumed case is not recreated, so a wrongly accepted junction surfaces as the
    // assertion above instead of a test_scratch_not_created_by_this_process cleanup error.
    if (fs.lstatSync(linkCase, { throwIfNoEntry: false })) {
      fs.unlinkSync(linkCase);
      fs.mkdirSync(linkCase);
      isolation.cleanupScratch(linkCase);
    }
  }
} finally {
  isolation.cleanupScratch(sentinel);
}

const injected = (code) => Object.assign(new Error(`injected ${code}`), { code });
const sameDirectory = (left, right) => {
  const a = fs.realpathSync(left), b = fs.realpathSync(right);
  return process.platform === "win32" ? a.toLowerCase() === b.toLowerCase() : a === b;
};

// (1) A sandbox-denied repository base falls back to one process-owned OS-temp root.
for (const code of ["EPERM", "EACCES", "EROFS"]) {
  const created = isolation.createScratch({ exists: () => false, mkdir() { throw injected(code); } });
  let second = null;
  try {
    const fallbackRoot = path.dirname(created);
    assert.ok(path.basename(fallbackRoot).startsWith(isolation.fallbackPrefix), code);
    assert.ok(sameDirectory(path.dirname(fallbackRoot), os.tmpdir()), "fallback root lives directly under os.tmpdir()");
    assert.ok(path.basename(created).startsWith("case-"));
    const env = isolation.childEnvironment(created, { ...process.env, [isolation.fallbackRootMarker]: "stale" });
    assert.strictEqual(env[isolation.fallbackRootMarker], fallbackRoot);
    assert.strictEqual(isolation.verifyChildEnvironment(env), created);
    const childSource = `process.stdout.write(require(${JSON.stringify(path.join(__dirname, "isolated-test-env.js"))}).verifyChildEnvironment());`;
    const child = captured((stdio) => spawnSync(process.execPath, ["-e", childSource], {
      env, stdio, encoding: "utf8", shell: false, windowsHide: true,
    }));
    assert.strictEqual(child.status, 0, child.stderr);
    assert.strictEqual(child.stdout, created);
    second = isolation.createScratch({ exists: () => false, mkdir() { throw injected(code); } });
    assert.strictEqual(path.dirname(second), fallbackRoot, "one root per process");
    isolation.cleanupScratch(second);
    second = null;
    assert.throws(() => isolation.cleanupScratch(fallbackRoot), /not_owned_case/);
  } finally {
    if (second) isolation.cleanupScratch(second);
    isolation.cleanupScratch(created);
  }
  assert.ok(!fs.existsSync(created), "fallback scratch cleaned");
}

// (2) Any other creation error still throws.
for (const code of ["ENOENT", "EEXIST", "EIO"]) {
  assert.throws(
    () => isolation.createScratch({ exists: () => false, mkdir() { throw injected(code); } }),
    (error) => error.code === code,
    code,
  );
}

// (3) The normal path still creates under the repository base unless that base is uncreatable here.
{
  const normal = isolation.createScratch();
  try {
    if (fs.existsSync(isolation.scratchBase) && sameDirectory(path.dirname(normal), isolation.scratchBase)) {
      assert.ok(path.basename(normal).startsWith("case-"));
      assert.strictEqual(isolation.childEnvironment(normal)[isolation.fallbackRootMarker], undefined);
    } else {
      assert.ok(path.basename(path.dirname(normal)).startsWith(isolation.fallbackPrefix));
      let code = null;
      try { fs.mkdirSync(isolation.scratchBase, { recursive: true }); } catch (error) { code = error.code; }
      assert.ok(["EPERM", "EACCES", "EROFS"].includes(code), `fell back although the repository base is creatable (${code})`);
    }
  } finally {
    isolation.cleanupScratch(normal);
  }
}

// (4) A declared fallback root that is a symlink/junction, unmarked, or foreign is refused.
{
  const base = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "aiworkhub-isolation-link-")));
  try {
    const forge = (root, caseName) => ({ [isolation.marker]: path.join(root, caseName), [isolation.fallbackRootMarker]: root });
    const realRoot = path.join(base, `${isolation.fallbackPrefix}real`);
    fs.mkdirSync(path.join(realRoot, "case-x"), { recursive: true });
    fs.writeFileSync(path.join(realRoot, isolation.fallbackRootSentinel), "marked\n");
    assert.throws(() => isolation.verifyChildEnvironment(forge(realRoot, "case-x")), /env_mismatch/, "owned root passes ownership checks");
    const bareRoot = path.join(base, `${isolation.fallbackPrefix}bare`);
    fs.mkdirSync(path.join(bareRoot, "case-x"), { recursive: true });
    assert.throws(() => isolation.verifyChildEnvironment(forge(bareRoot, "case-x")), /test_scratch_root_not_owned/);
    const foreignRoot = path.join(base, "foreign");
    fs.mkdirSync(path.join(foreignRoot, "case-x"), { recursive: true });
    fs.writeFileSync(path.join(foreignRoot, isolation.fallbackRootSentinel), "marked\n");
    assert.throws(() => isolation.verifyChildEnvironment(forge(foreignRoot, "case-x")), /test_scratch_root_not_owned/);
    const repoRoot = fs.realpathSync(path.resolve(__dirname, "..", ".."));
    assert.throws(() => isolation.verifyChildEnvironment(forge(repoRoot, "case-x")), /test_scratch_root_not_owned/, "a declared root equal to the repository is refused");
    const link = path.join(base, `${isolation.fallbackPrefix}link`);
    let linked = true;
    try {
      fs.symlinkSync(realRoot, link, process.platform === "win32" ? "junction" : "dir");
    } catch (error) {
      linked = false;
      console.log(`symlink ancestor probe skipped: ${error.code}`);
    }
    if (linked && !fs.lstatSync(link).isSymbolicLink()) {
      fs.rmdirSync(link); // removes the reparse point only, never its target
      linked = false;
      console.log("junction probe skipped: link not observable");
    }
    if (linked) {
      try {
        assert.throws(() => isolation.verifyChildEnvironment(forge(link, "case-x")), /symlink_or_non_directory/);
      } finally {
        if (fs.lstatSync(link, { throwIfNoEntry: false })) fs.unlinkSync(link);
      }
    }
  } finally {
    fs.rmSync(base, { recursive: true, force: true });
  }
}
console.log("AIWorkHub child test scratch isolation regression passed");
