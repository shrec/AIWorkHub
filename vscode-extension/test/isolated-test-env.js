"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");
const { spawnSync } = require("child_process");

const repoRoot = fs.realpathSync(path.resolve(__dirname, "..", ".."));
const scratchBase = path.join(repoRoot, ".aiworkhub", "runtime", "node-test-scratch");
const marker = "AIWORKHUB_NODE_TEST_SCRATCH";
const fallbackRootMarker = "AIWORKHUB_NODE_TEST_SCRATCH_ROOT";
const fallbackPrefix = "aiworkhub-node-test-";
const fallbackRootSentinel = ".aiworkhub-node-test-root";
const fallbackErrorCodes = new Set(["EPERM", "EACCES", "EROFS"]);
const ownedScratch = new Set();
let fallbackRoot = null;
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

function within(root, target) {
  const relative = path.relative(root, path.resolve(target));
  if (!relative || relative.startsWith(`..${path.sep}`) || relative === ".." || path.isAbsolute(relative)) return null;
  return relative;
}

function verifyFallbackRoot(root) {
  if (!path.isAbsolute(root) || !path.basename(root).startsWith(fallbackPrefix)) throw new Error("test_scratch_root_not_owned");
  const stat = fs.lstatSync(root);
  if (!stat.isDirectory() || stat.isSymbolicLink() || identity(fs.realpathSync(root)) !== identity(root)) {
    throw new Error("test_scratch_symlink_or_non_directory");
  }
  if (!fs.existsSync(path.join(root, fallbackRootSentinel))) throw new Error("test_scratch_root_not_owned");
}

function verifyDirectoryChain(target, root = null) {
  const chainRoot = root || (within(repoRoot, target) ? repoRoot : (fallbackRoot && within(fallbackRoot, target) ? fallbackRoot : null));
  const relative = chainRoot && within(chainRoot, target);
  if (!relative) throw new Error("test_scratch_outside_repository");
  if (chainRoot !== repoRoot) verifyFallbackRoot(chainRoot);
  let current = chainRoot;
  for (const part of relative.split(path.sep)) {
    current = path.join(current, part);
    const stat = fs.lstatSync(current);
    if (!stat.isDirectory() || stat.isSymbolicLink() || identity(fs.realpathSync(current)) !== identity(current)) {
      throw new Error("test_scratch_symlink_or_non_directory");
    }
  }
}

function removeFallbackRoot() {
  if (!fallbackRoot) return;
  const root = fallbackRoot;
  fallbackRoot = null;
  fs.rmSync(root, { recursive: true, force: true });
}

function ensureFallbackRoot() {
  if (fallbackRoot) return fallbackRoot;
  const created = fs.mkdtempSync(path.join(os.tmpdir(), fallbackPrefix));
  try {
    const root = fs.realpathSync(created);
    fs.writeFileSync(path.join(root, fallbackRootSentinel), "owned by one test process\n");
    verifyFallbackRoot(root);
    fallbackRoot = root;
  } catch (error) {
    fs.rmSync(created, { recursive: true, force: true });
    throw error;
  }
  process.once("exit", removeFallbackRoot);
  return fallbackRoot;
}

function registerScratch(scratch, root) {
  verifyDirectoryChain(scratch, root);
  ownedScratch.add(identity(scratch));
  return scratch;
}

function createScratch({ mkdir = fs.mkdirSync, exists = fs.existsSync } = {}) {
  // Check each existing ancestor before creating the next: never follow a junction.
  let current = repoRoot;
  for (const part of path.relative(repoRoot, scratchBase).split(path.sep)) {
    current = path.join(current, part);
    if (!exists(current)) {
      try {
        mkdir(current);
      } catch (error) {
        if (!error || !fallbackErrorCodes.has(error.code)) throw error;
        const root = ensureFallbackRoot();
        return registerScratch(fs.mkdtempSync(path.join(root, "case-")), root);
      }
    }
    verifyDirectoryChain(current, repoRoot);
  }
  return registerScratch(fs.mkdtempSync(path.join(scratchBase, "case-")), repoRoot);
}

function verifyScratch(scratch, declaredRoot = null) {
  const parent = identity(path.dirname(scratch));
  let root = null;
  if (path.basename(scratch).startsWith("case-")) {
    if (parent === identity(scratchBase)) root = repoRoot;
    else if (fallbackRoot && parent === identity(fallbackRoot)) root = fallbackRoot;
    else if (declaredRoot && parent === identity(declaredRoot)) {
      // A declared root is never the repository: it must carry the fallback prefix and sentinel.
      root = path.resolve(declaredRoot);
      verifyFallbackRoot(root);
    }
  }
  if (!root) throw new Error("test_scratch_not_owned_case");
  verifyDirectoryChain(scratch, root);
  return root;
}

function cleanupScratch(scratch) {
  verifyScratch(scratch);
  if (!ownedScratch.has(identity(scratch))) throw new Error("test_scratch_not_created_by_this_process");
  // rm removes contained symlinks themselves; the owned target and its ancestors
  // above were checked against their canonical root before recursive removal.
  fs.rmSync(scratch, { recursive: true, force: true });
  ownedScratch.delete(identity(scratch));
}

function childEnvironment(scratch, inherited = process.env) {
  const root = verifyScratch(scratch);
  const env = { ...inherited, [marker]: scratch };
  const redirected = new Set([...Object.keys(roots), "OPENCODE_CONFIG", marker, fallbackRootMarker].map((key) => key.toUpperCase()));
  for (const key of Object.keys(env)) {
    if (redirected.has(key.toUpperCase())) delete env[key];
  }
  env[marker] = scratch;
  if (root !== repoRoot) env[fallbackRootMarker] = root;
  for (const [key, relative] of Object.entries(roots)) {
    env[key] = path.join(scratch, relative);
    fs.mkdirSync(env[key], { recursive: true });
    verifyDirectoryChain(env[key], root);
  }
  env.OPENCODE_CONFIG = path.join(scratch, "config", "opencode", "opencode.json");
  fs.mkdirSync(path.dirname(env.OPENCODE_CONFIG), { recursive: true });
  return env;
}

function verifyChildEnvironment(env = process.env) {
  const scratch = env[marker];
  if (!scratch) throw new Error("test_scratch_missing");
  const root = verifyScratch(scratch, env[fallbackRootMarker] || null);
  for (const [key, relative] of Object.entries(roots)) {
    if (identity(env[key] || "") !== identity(path.join(scratch, relative))) throw new Error(`test_scratch_env_mismatch:${key}`);
    verifyDirectoryChain(env[key], root);
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
  const result = runIsolatedTest([path.resolve(process.argv[1]), ...process.argv.slice(2)]);
  if (result.error) console.error(result.error.message);
  process.exit(result.status === 0 ? 0 : (result.status || 1));
}

module.exports = {
  scratchBase, marker, fallbackRootMarker, fallbackPrefix, fallbackRootSentinel,
  createScratch, cleanupScratch, childEnvironment, verifyChildEnvironment, runIsolatedTest, enterIsolatedTest,
};
