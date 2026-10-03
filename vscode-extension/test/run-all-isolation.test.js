"use strict";

const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const isolation = require("./isolated-test-env");

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
  const first = isolation.runIsolatedTest(["-e", source, "0"], { env: hostile, stdio: "pipe" });
  assert.strictEqual(first.status, 0, first.stderr);
  const evidence = JSON.parse(first.stdout);
  assert.notStrictEqual(evidence.scratch, sentinel);
  assert.ok(!fs.existsSync(first.scratchRoot), "successful child scratch cleaned");
  const failed = isolation.runIsolatedTest(["-e", source, "7"], { env: hostile, stdio: "pipe" });
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
        return isolation.runIsolatedTest(["-e", source, "0"], {env: hostile, stdio: "pipe"});
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
  try {
    assert.throws(() => isolation.cleanupScratch(linkCase), /symlink_or_non_directory/);
    assert.strictEqual(fs.readFileSync(sentinelFile, "utf8"), "owner state\n");
  } finally {
    fs.unlinkSync(linkCase);
    fs.mkdirSync(linkCase);
    isolation.cleanupScratch(linkCase);
  }
} finally {
  isolation.cleanupScratch(sentinel);
}
console.log("AIWorkHub child test scratch isolation regression passed");
