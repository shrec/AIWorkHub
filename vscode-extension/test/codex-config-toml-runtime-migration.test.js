"use strict";

// B894a: selective Codex config.toml PYTHONPATH runtime migration. Exercises
// __testInternals.migrateCodexConfigTomlText directly (pure text transform,
// no real VS Code, no real filesystem) across the real-world layouts this
// task must handle: AIWorkHub and AIWorkHub_Ultrafast tables side by side,
// mixed-case table names, Linux/macOS/Windows runtime path forms, CRLF line
// endings, and custom/unrelated blocks that must stay fully byte-identical.

const assert = require("assert");
const Module = require("module");
const path = require("path");

const extensionPath = path.resolve(__dirname, "..", "extension.js");

// extension.js requires the real VS Code host API at module scope; stub it
// (same technique as reloadless-runtime-repair.test.js) so this file's pure
// text-transform functions can be loaded and exercised under plain Node.
const fakeVscode = {
  workspace: {
    workspaceFolders: [],
    getConfiguration: () => ({ get: () => "", inspect: () => ({}), update: async () => {} }),
    registerWebviewPanelSerializer: () => ({ dispose: () => {} }),
    registerWebviewViewProvider: () => ({ dispose: () => {} }),
  },
  window: {
    createOutputChannel: () => ({ appendLine: () => {}, dispose: () => {} }),
    setStatusBarMessage: () => {},
    showErrorMessage: () => {},
    showInformationMessage: () => {},
    showQuickPick: async () => undefined,
  },
  commands: { registerCommand: () => ({ dispose: () => {} }) },
  Uri: { joinPath: (...parts) => ({ fsPath: parts.map((p) => p.fsPath || p).join("/") }) },
  ViewColumn: { Active: 1 },
  ConfigurationTarget: { Global: 1 },
};
const originalLoad = Module._load;
Module._load = function patchedLoad(request, parent, isMain) {
  if (request === "vscode") return fakeVscode;
  return originalLoad.call(this, request, parent, isMain);
};
let extensionModule;
try {
  delete require.cache[extensionPath];
  extensionModule = require(extensionPath);
} finally {
  Module._load = originalLoad;
}
const { __testInternals } = extensionModule;
const {
  repairCodexConfigTomlText,
  migrateCodexConfigTomlText,
  splitCodexPythonPathValue,
  classifyImmediateCodexChildren,
} = __testInternals;

{
  const ps = [
    " 101  10 /usr/bin/node /opt/codex -c x app-server --analytics-default-enabled",
    " 102  11 python3 /runtime/bin/aiworkhub-app-server-mux app-server",
    " 103  10 python3 -m aiworkhub.server",
    " 104  12 /usr/bin/node /opt/codex app-server",
  ].join("\n");
  const children = classifyImmediateCodexChildren(ps, 10);
  assert.deepStrictEqual(children.direct.map((x) => x.pid), [101]);
  assert.deepStrictEqual(children.mux, []);

  const muxChildren = classifyImmediateCodexChildren(
    " 201  10 python3 /runtime/bin/aiworkhub-app-server-mux -c x app-server\n",
    10,
  );
  assert.deepStrictEqual(muxChildren.direct, []);
  assert.deepStrictEqual(muxChildren.mux.map((x) => x.pid), [201]);
}

const NEW_RUNTIME_LINUX = "/home/dev/.vscode-server/extensions/publisher.aiworkhub-0.6.20/runtime";
const OLD_RUNTIME_LINUX = "/home/dev/.vscode-server/extensions/publisher.aiworkhub-0.6.19/runtime";
const OLD_RUNTIME_MACOS = "/Users/dev/.vscode/extensions/publisher.aiworkhub-0.6.19/runtime";
const NEW_RUNTIME_MACOS = "/Users/dev/.vscode/extensions/publisher.aiworkhub-0.6.20/runtime";
const OLD_RUNTIME_WINDOWS = "C:\\\\Users\\\\dev\\\\.vscode\\\\extensions\\\\publisher.aiworkhub-0.6.19\\\\runtime";
const NEW_RUNTIME_WINDOWS = "C:\\Users\\dev\\.vscode\\extensions\\publisher.aiworkhub-0.6.20\\runtime";

function assertUnchangedOutsideTargetLine(original, migrated, changedLineSubstrings) {
  const originalLines = original.split(/\r\n|\r|\n/);
  const migratedLines = migrated.split(/\r\n|\r|\n/);
  assert.strictEqual(migratedLines.length, originalLines.length, "line count must be preserved");
  for (let i = 0; i < originalLines.length; i += 1) {
    if (originalLines[i] === migratedLines[i]) continue;
    const touchesTarget = changedLineSubstrings.some((needle) => originalLines[i].includes(needle));
    assert.ok(touchesTarget, `unexpected line drift at line ${i}: ${JSON.stringify(originalLines[i])} -> ${JSON.stringify(migratedLines[i])}`);
  }
}

// Application-global Codex MCP configuration must never retain a workspace
// repository binding. Each Codex chat resolves its own cwd; extension-local
// dashboard children receive their explicit workspace binding at spawn time.
{
  const oldOwnedRuntime = "/home/dev/.vscode-server/extensions/shrec.aiworkhub-0.6.19/runtime";
  const newOwnedRuntime = "/home/dev/.vscode-server/extensions/shrec.aiworkhub-0.6.20/runtime";
  const original = [
    "[mcp_servers.AIWorkHub]",
    'command = "python3"',
    'args = ["-m", "aiworkhub.server"]',
    "",
    "[mcp_servers.AIWorkHub.env]",
    `PYTHONPATH = "${oldOwnedRuntime}"`,
    'AIWORKHUB_REPO = "/repo/last-window-wins"',
    'AIWORKHUB_REPO_ROOT = "/repo/last-window-wins"',
    'AIWORKHUB_ALLOW_WRITES = "1"',
    "",
  ].join("\n");
  const result = repairCodexConfigTomlText(original, newOwnedRuntime);
  assert.strictEqual(result.changed, true);
  assert.ok(!result.text.includes("AIWORKHUB_REPO ="));
  assert.ok(!result.text.includes("AIWORKHUB_REPO_ROOT ="));
  assert.ok(result.text.includes('AIWORKHUB_ALLOW_WRITES = "1"'));
  assert.ok(result.text.includes(`PYTHONPATH = "${newOwnedRuntime}"`));
}

// ── 1. Real AIWorkHub and AIWorkHub_Ultrafast blocks both migrate, distinct
//      repo env (AIWORKHUB_REPO/ROOT/ID) retained verbatim per block. ───────
{
  const original = [
    "[mcp_servers.AIWorkHub]",
    'command = "python3"',
    'args = ["-m", "aiworkhub.server"]',
    "",
    "[mcp_servers.AIWorkHub.env]",
    `PYTHONPATH = "${OLD_RUNTIME_LINUX}"`,
    'AIWORKHUB_REPO = "/repo/one"',
    'AIWORKHUB_REPO_ROOT = "/repo/one"',
    'AIWORKHUB_REPO_ID = "repo_aaaa"',
    "",
    "[mcp_servers.AIWorkHub_Ultrafast]",
    'command = "python3"',
    'args = ["-m", "aiworkhub.server"]',
    "",
    "[mcp_servers.AIWorkHub_Ultrafast.env]",
    `PYTHONPATH = "${OLD_RUNTIME_LINUX}"`,
    'AIWORKHUB_REPO = "/repo/two"',
    'AIWORKHUB_REPO_ROOT = "/repo/two"',
    'AIWORKHUB_REPO_ID = "repo_bbbb"',
    "",
  ].join("\n");

  const result = migrateCodexConfigTomlText(original, NEW_RUNTIME_LINUX);
  assert.strictEqual(result.changed, true);
  assert.deepStrictEqual(result.migrated.sort(), ["AIWorkHub", "AIWorkHub_Ultrafast"].sort());
  assert.ok(result.content.includes(`PYTHONPATH = "${NEW_RUNTIME_LINUX}"`));
  assert.ok(!result.content.includes(OLD_RUNTIME_LINUX));
  assert.ok(result.content.includes('AIWORKHUB_REPO = "/repo/one"'));
  assert.ok(result.content.includes('AIWORKHUB_REPO_ROOT = "/repo/one"'));
  assert.ok(result.content.includes('AIWORKHUB_REPO_ID = "repo_aaaa"'));
  assert.ok(result.content.includes('AIWORKHUB_REPO = "/repo/two"'));
  assert.ok(result.content.includes('AIWORKHUB_REPO_ID = "repo_bbbb"'));
  assertUnchangedOutsideTargetLine(original, result.content, ["PYTHONPATH ="]);
}

// ── 2. Mixed-case table name is preserved exactly; never renamed/lowercased.
{
  const original = [
    "[mcp_servers.MyAiWorkHubCustom]",
    'command = "python3"',
    'args = ["-m", "aiworkhub.server"]',
    "",
    "[mcp_servers.MyAiWorkHubCustom.env]",
    `PYTHONPATH = "${OLD_RUNTIME_MACOS}"`,
    "",
  ].join("\n");
  const result = migrateCodexConfigTomlText(original, NEW_RUNTIME_MACOS);
  assert.deepStrictEqual(result.migrated, ["MyAiWorkHubCustom"]);
  assert.ok(result.content.includes("[mcp_servers.MyAiWorkHubCustom]"));
  assert.ok(result.content.includes("[mcp_servers.MyAiWorkHubCustom.env]"));
  assert.ok(result.content.includes(`PYTHONPATH = "${NEW_RUNTIME_MACOS}"`));
}

// ── 3. Windows path form + CRLF line endings. ───────────────────────────────
{
  const original = [
    "[mcp_servers.AIWorkHub]",
    'command = "py"',
    'args = ["-m", "aiworkhub.server"]',
    "",
    "[mcp_servers.AIWorkHub.env]",
    `PYTHONPATH = "${OLD_RUNTIME_WINDOWS}"`,
    'AIWORKHUB_REPO = "C:\\\\repo"',
    "",
  ].join("\r\n");
  assert.ok(original.includes("\r\n"), "fixture must actually contain CRLF");

  const result = migrateCodexConfigTomlText(original, NEW_RUNTIME_WINDOWS);
  assert.strictEqual(result.changed, true);
  assert.deepStrictEqual(result.migrated, ["AIWorkHub"]);
  // Every original line ending is preserved byte-for-byte (CRLF throughout).
  const crlfCount = (result.content.match(/\r\n/g) || []).length;
  const originalCrlfCount = (original.match(/\r\n/g) || []).length;
  assert.strictEqual(crlfCount, originalCrlfCount, "CRLF line endings must be preserved exactly");
  assert.ok(!result.content.includes("\n\n\n"), "no line-ending corruption introduced");
  assert.ok(result.content.includes('AIWORKHUB_REPO = "C:\\\\repo"'));
}

// ── 4. Custom / unrelated / hand-written MCP blocks stay byte-identical --
//      wrong args (not this server), and a block whose PYTHONPATH is a
//      user's own custom (non-versioned-extension) path, are both left
//      completely untouched. ────────────────────────────────────────────────
{
  const customBlock = [
    "[mcp_servers.SomeOtherTool]",
    'command = "node"',
    'args = ["-y", "some-other-mcp-server"]',
    "",
    "[mcp_servers.SomeOtherTool.env]",
    'PYTHONPATH = "/opt/my/custom/pythonpath"',
    "",
  ].join("\n");
  const notInstalledYet = [
    "[mcp_servers.AIWorkHub]",
    'command = "python3"',
    'args = ["-m", "aiworkhub.server"]',
    "",
    "[mcp_servers.AIWorkHub.env]",
    'PYTHONPATH = "/opt/my/custom/pythonpath"',
    "",
  ].join("\n");

  const customResult = migrateCodexConfigTomlText(customBlock, NEW_RUNTIME_LINUX);
  assert.strictEqual(customResult.changed, false);
  assert.strictEqual(customResult.content, customBlock);

  const notOwnedResult = migrateCodexConfigTomlText(notInstalledYet, NEW_RUNTIME_LINUX);
  assert.strictEqual(notOwnedResult.changed, false);
  assert.strictEqual(notOwnedResult.content, notInstalledYet);
}

// ── 5. A file mixing an owned AIWorkHub block and an unrelated hand-written
//      block: only the owned block's PYTHONPATH byte-range changes. ────────
{
  const mixed = [
    "# hand-written custom entry -- never touched",
    "[mcp_servers.Handwritten]",
    'command = "/usr/local/bin/my-tool"',
    'args = ["--flag"]',
    "",
    "[mcp_servers.Handwritten.env]",
    'PYTHONPATH = "/opt/my/custom/pythonpath"',
    'SOME_OTHER_VAR = "keep-me"',
    "",
    "[mcp_servers.AIWorkHub]",
    'command = "python3"',
    'args = ["-m", "aiworkhub.server"]',
    "",
    "[mcp_servers.AIWorkHub.env]",
    `PYTHONPATH = "${OLD_RUNTIME_LINUX}"`,
    'AIWORKHUB_REPO_ID = "repo_cccc"',
    "",
  ].join("\n");

  const result = migrateCodexConfigTomlText(mixed, NEW_RUNTIME_LINUX);
  assert.deepStrictEqual(result.migrated, ["AIWorkHub"]);
  assert.ok(result.content.includes('PYTHONPATH = "/opt/my/custom/pythonpath"'));
  assert.ok(result.content.includes('SOME_OTHER_VAR = "keep-me"'));
  assert.ok(result.content.includes("# hand-written custom entry -- never touched"));
  assert.ok(result.content.includes(`PYTHONPATH = "${NEW_RUNTIME_LINUX}"`));
  assertUnchangedOutsideTargetLine(mixed, result.content, ['PYTHONPATH = "' + OLD_RUNTIME_LINUX]);
}

// ── 6. Idempotency: migrating already-current content is a no-op (changed
//      is false, byte-identical output). ───────────────────────────────────
{
  const alreadyCurrent = [
    "[mcp_servers.AIWorkHub]",
    'command = "python3"',
    'args = ["-m", "aiworkhub.server"]',
    "",
    "[mcp_servers.AIWorkHub.env]",
    `PYTHONPATH = "${NEW_RUNTIME_LINUX}"`,
    "",
  ].join("\n");
  const result = migrateCodexConfigTomlText(alreadyCurrent, NEW_RUNTIME_LINUX);
  assert.strictEqual(result.changed, false);
  assert.strictEqual(result.content, alreadyCurrent);
}

// ── 7. splitCodexPythonPathValue never mis-splits a Windows drive letter's
//      own colon when no ';' delimiter is present. ─────────────────────────
{
  assert.deepStrictEqual(splitCodexPythonPathValue("C:\\Users\\dev\\runtime"), ["C:\\Users\\dev\\runtime"]);
  assert.deepStrictEqual(splitCodexPythonPathValue("C:\\a\\runtime;C:\\b\\extra"), ["C:\\a\\runtime", "C:\\b\\extra"]);
  assert.deepStrictEqual(splitCodexPythonPathValue("/a/runtime:/b/extra"), ["/a/runtime", "/b/extra"]);
}

// ── 8. resolveExtensionRuntimeDir: prefer the packaged runtime/, fall back to
//      the dev-checkout src/, and never return a path missing the aiworkhub
//      package. Writing a non-existent runtime/ into the Codex config is what
//      made `python -m aiworkhub.server` fail (ModuleNotFoundError) and stopped
//      the Codex chat from launching. ───────────────────────────────────────
{
  const fs = require("fs");
  const os = require("os");
  const { resolveExtensionRuntimeDir } = __testInternals;

  const base = fs.mkdtempSync(path.join(os.tmpdir(), "awh-rt-"));

  // (a) packaged VSIX layout: <ext>/runtime/aiworkhub/__init__.py exists.
  const packagedExt = path.join(base, "shrec.aiworkhub-0.6.31");
  fs.mkdirSync(path.join(packagedExt, "runtime", "aiworkhub"), { recursive: true });
  fs.writeFileSync(path.join(packagedExt, "runtime", "aiworkhub", "__init__.py"), "");
  assert.strictEqual(resolveExtensionRuntimeDir(packagedExt), path.join(packagedExt, "runtime"),
    "packaged VSIX must resolve to its runtime/ dir");

  // (b) dev checkout: <ext>=<repo>/vscode-extension with NO runtime/, but the
  //     repo's ../src/aiworkhub exists (runtime/ is only built at package time).
  const repo = path.join(base, "AIWorkHub");
  const devExt = path.join(repo, "vscode-extension");
  fs.mkdirSync(devExt, { recursive: true });
  fs.mkdirSync(path.join(repo, "src", "aiworkhub"), { recursive: true });
  fs.writeFileSync(path.join(repo, "src", "aiworkhub", "__init__.py"), "");
  assert.strictEqual(resolveExtensionRuntimeDir(devExt), path.join(repo, "src"),
    "dev checkout with no runtime/ must fall back to the repo src/ dir");

  // (c) neither candidate exists: best-effort packaged path so the existing
  //     repair still runs exactly as before this change.
  const bareExt = path.join(base, "bare");
  fs.mkdirSync(bareExt, { recursive: true });
  assert.strictEqual(resolveExtensionRuntimeDir(bareExt), path.join(bareExt, "runtime"),
    "with neither candidate present, fall back to the packaged runtime/ path");

  // (d) regression for the dev-mode dead-path bug: a config PYTHONPATH pointing
  //     at a non-existent runtime/ is now HEALED to the resolved (existing)
  //     src/ dir instead of being left dead (previously changed=false).
  const brokenConfig = [
    "[mcp_servers.AIWorkHub.env]",
    `PYTHONPATH = "${path.join(devExt, "runtime")}"`,
  ].join("\n");
  const healed = repairCodexConfigTomlText(brokenConfig, resolveExtensionRuntimeDir(devExt));
  assert.strictEqual(healed.changed, true, "dead runtime/ PYTHONPATH must be healed, not left in place");
  assert.ok(healed.text.includes(`PYTHONPATH = "${path.join(repo, "src")}"`),
    "healed PYTHONPATH must point at the resolved src/ dir");

  fs.rmSync(base, { recursive: true, force: true });
}

// ── 9. bindCodexSidebandEnvironment: the OpenAI Codex extension shares this
//      workspace host's process.env, so binding the repo identity there is how
//      the mux (which fails closed without AIWORKHUB_REPO_ID, B925) receives it.
//      A real repo_id publishes the binding; an unbound/sentinel identity clears
//      it (do no harm: Codex then launches directly), and the write/launch gates
//      are never published. ──────────────────────────────────────────────────
{
  const { bindCodexSidebandEnvironment } = __testInternals;
  const saved = {};
  const keys = ["AIWORKHUB_REPO_ID", "AIWORKHUB_REPO_ROOT", "AIWORKHUB_REPO",
                "AIWORKHUB_CALLBACK_TRANSPORT", "AIWORKHUB_ALLOW_WRITES", "AIWORKHUB_ALLOW_LAUNCH"];
  for (const k of keys) saved[k] = process.env[k];
  try {
    const realRepoId = "repo_" + "a".repeat(32);

    // real repo_id -> publishes the identity, marks sideband transport.
    process.env.AIWORKHUB_ALLOW_WRITES = "1"; // must be scrubbed
    assert.strictEqual(bindCodexSidebandEnvironment({ repoId: realRepoId, root: "/tmp/repo" }), true);
    assert.strictEqual(process.env.AIWORKHUB_REPO_ID, realRepoId);
    assert.strictEqual(process.env.AIWORKHUB_REPO_ROOT, "/tmp/repo");
    assert.strictEqual(process.env.AIWORKHUB_CALLBACK_TRANSPORT, "sideband");
    assert.strictEqual(process.env.AIWORKHUB_ALLOW_WRITES, undefined, "write gate must never be published");

    // sentinel/uninitialized repo_id -> clears the binding (do no harm).
    assert.strictEqual(bindCodexSidebandEnvironment({ repoId: "manifest-missing" }), false);
    assert.strictEqual(process.env.AIWORKHUB_REPO_ID, undefined);

    // no identity at all -> also cleared.
    process.env.AIWORKHUB_REPO_ID = realRepoId;
    assert.strictEqual(bindCodexSidebandEnvironment(null), false);
    assert.strictEqual(process.env.AIWORKHUB_REPO_ID, undefined);
  } finally {
    for (const k of keys) {
      if (saved[k] === undefined) delete process.env[k];
      else process.env[k] = saved[k];
    }
  }
}

console.log("AIWorkHub Codex config.toml runtime migration contract checks passed");
