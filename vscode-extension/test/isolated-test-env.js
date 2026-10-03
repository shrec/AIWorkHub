"use strict";

const fs = require("fs");
const path = require("path");
const { spawnSync } = require("child_process");

const repoRoot = fs.realpathSync(path.resolve(__dirname, "..", ".."));
const scratchBase = path.join(repoRoot, ".aiworkhub", "runtime", "node-test-scratch");
const marker = "AIWORKHUB_NODE_TEST_SCRATCH";
const ownedScratch = new Set();
const roots = {
  HOME: "home", USERPROFILE: "home", CODEX_HOME: "codex",
  XDG_CONFIG_HOME: "config", APPDATA: "appdata", LOCALAPPDATA: "localappdata",
  TEMP: "tmp", TMP: "tmp", TMPDIR: "tmp",
  AIWORKHUB_APP_SERVER_MUX_SIDEBAND_DIR: "mux",
  AIWORKHUB_VSCODE_LM_BRIDGE_ROOT: "bridge",
};

function identity(value) {
  const resolved = path.resolve(value);
  return process.platform === "win32" ? resolved.toLowerCase() : resolved;
}

function verifyDirectoryChain(target) {
  const relative = path.relative(repoRoot, path.resolve(target));
  if (!relative || relative.startsWith(`..${path.sep}`) || relative === ".." || path.isAbsolute(relative)) {
    throw new Error("test_scratch_outside_repository");
  }
  let current = repoRoot;
  for (const part of relative.split(path.sep)) {
    current = path.join(current, part);
    const stat = fs.lstatSync(current);
    if (!stat.isDirectory() || stat.isSymbolicLink() || identity(fs.realpathSync(current)) !== identity(current)) {
      throw new Error("test_scratch_symlink_or_non_directory");
    }
  }
}

function createScratch() {
  // Check each existing ancestor before creating the next: never follow a junction.
  let current = repoRoot;
  for (const part of path.relative(repoRoot, scratchBase).split(path.sep)) {
    current = path.join(current, part);
    if (!fs.existsSync(current)) fs.mkdirSync(current);
    verifyDirectoryChain(current);
  }
  const scratch = fs.mkdtempSync(path.join(scratchBase, "case-"));
  verifyDirectoryChain(scratch);
  ownedScratch.add(identity(scratch));
  return scratch;
}

function verifyScratch(scratch) {
  if (identity(path.dirname(scratch)) !== identity(scratchBase) || !path.basename(scratch).startsWith("case-")) {
    throw new Error("test_scratch_not_owned_case");
  }
  verifyDirectoryChain(scratch);
}

function cleanupScratch(scratch) {
  verifyScratch(scratch);
  if (!ownedScratch.has(identity(scratch))) throw new Error("test_scratch_not_created_by_this_process");
  // rm removes contained symlinks themselves; the owned target and its ancestors
  // above were checked against the canonical repository before recursive removal.
  fs.rmSync(scratch, { recursive: true, force: true });
  ownedScratch.delete(identity(scratch));
}

function childEnvironment(scratch, inherited = process.env) {
  verifyScratch(scratch);
  const env = { ...inherited, [marker]: scratch };
  const redirected = new Set([...Object.keys(roots), "OPENCODE_CONFIG", marker].map((key) => key.toUpperCase()));
  for (const key of Object.keys(env)) {
    if (redirected.has(key.toUpperCase())) delete env[key];
  }
  env[marker] = scratch;
  for (const [key, relative] of Object.entries(roots)) {
    env[key] = path.join(scratch, relative);
    fs.mkdirSync(env[key], { recursive: true });
    verifyDirectoryChain(env[key]);
  }
  env.OPENCODE_CONFIG = path.join(scratch, "config", "opencode", "opencode.json");
  fs.mkdirSync(path.dirname(env.OPENCODE_CONFIG), { recursive: true });
  return env;
}

function verifyChildEnvironment(env = process.env) {
  const scratch = env[marker];
  if (!scratch) throw new Error("test_scratch_missing");
  verifyScratch(scratch);
  for (const [key, relative] of Object.entries(roots)) {
    if (identity(env[key] || "") !== identity(path.join(scratch, relative))) throw new Error(`test_scratch_env_mismatch:${key}`);
    verifyDirectoryChain(env[key]);
  }
  if (identity(env.OPENCODE_CONFIG || "") !== identity(path.join(scratch, "config", "opencode", "opencode.json"))) {
    throw new Error("test_scratch_env_mismatch:OPENCODE_CONFIG");
  }
  return scratch;
}

function runIsolatedTest(args, options = {}) {
  const scratch = createScratch();
  try {
    const result = spawnSync(process.execPath, args, {
      cwd: path.resolve(__dirname, ".."),
      env: childEnvironment(scratch, options.env || process.env),
      stdio: options.stdio || "inherit", encoding: "utf8", shell: false,
      windowsHide: true,
    });
    return { ...result, scratchRoot: scratch };
  } finally {
    cleanupScratch(scratch);
  }
}

function enterIsolatedTest() {
  if (process.env[marker]) {
    verifyChildEnvironment();
    return;
  }
  const result = runIsolatedTest([process.argv[1], ...process.argv.slice(2)]);
  if (result.error) console.error(result.error.message);
  process.exit(result.status === 0 ? 0 : (result.status || 1));
}

module.exports = { scratchBase, marker, createScratch, cleanupScratch, childEnvironment, verifyChildEnvironment, runIsolatedTest, enterIsolatedTest };
