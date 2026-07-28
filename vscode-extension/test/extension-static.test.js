const assert = require("assert");
const fs = require("fs");
const path = require("path");

const root = path.resolve(__dirname, "..");
const read = (rel) => fs.readFileSync(path.join(root, rel), "utf8");
const pkg = JSON.parse(read("package.json"));
const ext = read("extension.js");
const app = read("media/app.js");
const css = read("media/app.css");

function absent(haystack, values, label) {
  const hits = values.filter((value) => haystack.includes(value));
  assert.deepStrictEqual(hits, [], `${label}: ${hits.join(", ")}`);
}

assert.strictEqual(pkg.name, "aiworkhub");
assert.strictEqual(pkg.displayName, "AIWorkHub");
assert.ok(ext.includes(`EXPECTED_MCP_PACKAGE_VERSION = "${pkg.version}"`));
assert.deepStrictEqual(pkg.extensionKind, ["workspace"]);
assert.strictEqual(pkg.icon, "media/aiworkhub-icon.png");
assert.ok(
  pkg.activationEvents.includes("onStartupFinished"),
  "AIWorkHub callbacks must activate after VS Code startup without opening the dashboard"
);

const commands = new Map(pkg.contributes.commands.map((command) => [command.command, command.title]));
assert.deepStrictEqual([...commands.keys()].sort(), [
  "aiworkhub.openDashboard",
  "aiworkhub.refreshDashboard",
  "aiworkhub.restartDashboard",
  "aiworkhub.selectRepository",
]);
for (const title of commands.values()) {
  assert.ok(title.startsWith("AIWorkHub: "), title);
}
assert.strictEqual(pkg.contributes.viewsContainers.activitybar[0].id, "aiworkhub");
assert.strictEqual(pkg.contributes.views.aiworkhub[0].id, "aiworkhub.view");
assert.ok(pkg.contributes.configuration.properties["aiworkhub.refreshIntervalMs"]);

absent(JSON.stringify(pkg) + ext + app + css, [
  "dashboardUrl",
  "openExternal",
  "simpleBrowser",
  "asExternalUri",
  "XMLHttpRequest",
  "fetch(",
  "192.168.",
  "127.0.0.1",
  "0.0.0.0",
  ":8765",
  "primary_sidebar_dashboard",
], "legacy or remote dashboard dependency found");

// B844: the bundled runtime must never depend on a repository checkout, an
// editable install, or a network-time install of the aiworkhub package.
absent(ext, [
  "pip install",
  "pip3 install",
  "--editable",
  "site-packages",
  "npm install",
], "install-anywhere gap: repository/editable-install/network-install dependency found");

assert.ok(ext.includes('const EXT_ID = "aiworkhub"'));
assert.ok(ext.includes('const DISPLAY_NAME = "AIWorkHub"'));
assert.ok(ext.includes('const PANEL_VIEW_TYPE = "aiworkhub.dashboard"'));
assert.strictEqual((ext.match(/createWebviewPanel/g) || []).length, 1);
assert.ok(ext.includes("registerWebviewPanelSerializer(PANEL_VIEW_TYPE"));
assert.ok(ext.includes("startup dispatcher activation failed"));
assert.ok(ext.includes("ensureRepositoryCoordinatorCapability(root)"));
assert.ok(ext.includes('path.join(root, ".aiworkhub", "runtime")'));
assert.ok(!ext.includes('homedir(), ".config", "aiworkhub", "taskctl_coordinator.token"'));
assert.ok(ext.includes("snapshotRequestSeq"));
assert.ok(ext.includes("requestSeq === view.snapshotRequestSeq"));
assert.ok(ext.includes('AIWORKHUB_CALLBACK_TRANSPORT: "sideband"'));
assert.ok(!ext.includes('AIWORKHUB_CALLBACK_TRANSPORT: "repository_inbox_poll"'));
// The only permitted foreign-PID operation is Node's non-destructive signal-0
// existence probe used to reject a dead route owner after reload.  No signal
// that can stop, interrupt, or otherwise control another process is allowed.
assert.strictEqual((ext.match(/process\.kill\(numericPid, 0\)/g) || []).length, 1);
absent(ext, [
  "process.kill(numericPid, \"SIG",
  "process.kill(numericPid, 'SIG",
  "classifyImmediateCodexChildren",
], "destructive foreign process control");
assert.ok(ext.includes('getConfiguration("chatgpt")'));
assert.ok(ext.includes('config.update("cliExecutable", launcher, true)'));
assert.ok(ext.includes("custom_cli_executable_preserved"));
assert.ok(ext.includes("retainContextWhenHidden: true"));
assert.ok(ext.includes("getHtmlForNavigatorWebview"));
assert.ok(ext.includes("Open Dashboard"));
assert.ok(ext.includes("Select Repository"));

assert.strictEqual((ext.match(/childProcess\.spawn\(/g) || []).length, 1);
assert.ok(ext.includes('[...python.argsPrefix, "-m", "aiworkhub.server"]'));
assert.ok(ext.includes("AIWORKHUB_REPO_ROOT"));
assert.ok(ext.includes("AIWORKHUB_REPO_ID"));
assert.ok(ext.includes("AIWORKHUB_WINDOW_ID"));
assert.ok(ext.includes("AIWORKHUB_CLAIM_EPISODE"));
assert.ok(ext.includes('snapshot: "aiworkhub_dashboard_snapshot"'));
assert.ok(ext.includes("mcpClient.repositoryRoot !== root"));
// B865: switching repositories must stop the OLD repo's lifecycle-owned
// dispatcher (never orphaning a nested app-server subprocess) before the
// old MCP child is killed -- a bare mcpClient.stop() would skip that.
assert.ok(ext.includes("mcpClient.stopDispatcherThenTerminate({ restart: false })"));

// B844: `import aiworkhub` must resolve to this extension's own bundled
// runtime/ directory -- never a repository checkout, an editable install, or
// a fixed host-absolute path. Both the PYTHONPATH env var and the spawned
// child's own cwd must be derived from context.extensionUri, and the
// repository path must stay confined to the AIWORKHUB_REPO* data env vars.
// The runtime dir is resolved from context.extensionUri via
// resolveExtensionRuntimeDir, which prefers the packaged runtime/ and only
// falls back to the sibling repo src/ in a development checkout (never a
// fixed host path). Writing a non-existent runtime/ path here is what broke
// Codex's `python -m aiworkhub.server`.
assert.ok(ext.includes("resolveExtensionRuntimeDir(context.extensionUri.fsPath)"));
assert.ok(ext.includes('path.join(extensionFsPath, "runtime")'));

// Cross-plugin failsafe: no foreign extension configuration or shared-host
// environment binding. Repository identity is private to the owned MCP child.
assert.ok(!ext.includes("bindCodexSidebandEnvironment"));
assert.ok(!ext.includes("process.env.AIWORKHUB_REPO_ID ="));
assert.ok(ext.includes("function writeSharedRepoRouteRecord"));
assert.ok(ext.includes("function removeSharedRepoRouteRecord"));
assert.ok(ext.includes("aiworkhub.shared_repo_route.v1"));
assert.ok(ext.includes('path.join(os.homedir(), ".aiworkhub", "router", "repos")'));
assert.ok(ext.includes('id="repo-router"'));
assert.ok(app.includes("function renderKnownRepositories"));
assert.ok(app.includes("known_repositories"));

// Codex callback routing owns a packaged app-server mux, but it must be
// configured through VS Code settings rather than global environment state.
assert.ok(ext.includes("materializeStableMuxLauncher(context)"));
assert.ok(ext.includes("ensureCodexCallbackMuxConfigured(context, stableMuxLauncher)"));
assert.ok(ext.includes('config.update("cliExecutable", launcher, true)'));
assert.ok(ext.includes("custom_cli_executable_preserved"));
assert.ok(ext.includes("env.PYTHONPATH ="));
assert.ok(ext.includes("cwd: runtimeDir || root"));
assert.ok(!ext.includes('cwd: root,'));
assert.ok(ext.includes('path.join(root, ".venv", "Scripts", "python.exe")'));
assert.ok(ext.includes('command: "py"'));
assert.ok(ext.includes('argsPrefix: ["-3"]'));
assert.ok(ext.includes('childProcess.spawn(python.command, [...python.argsPrefix, "-m", "aiworkhub.server"]'));
assert.ok(ext.includes("function repairWorkspaceMcpConfigObject"));
assert.ok(ext.includes("ensureWorkspaceMcpConfigsRepaired(context)"));
assert.ok(ext.includes("PYTHONPATH: runtimeDir"));

// B850: activation and repository selection must never bootstrap/write a
// repository, and storage readiness must never be computed from directory
// existence -- both were the cross-repository dashboard authority bug.
// Initialization is the Webview's one explicit "Initialize AIWorkHub"
// action, which invokes the one bounded aiworkhub_dashboard_initialize MCP
// tool (Python side owns manifest/registry/DB creation).
assert.ok(!ext.includes("function bootstrapRepository"), "activation-time bootstrap must be removed");
assert.ok(!ext.includes(".isDirectory()"), "storage readiness must never be derived from directory existence");
assert.ok(ext.includes('path.join(root, ".aiworkhub", "project.json")'));
assert.ok(ext.includes('INITIALIZE_TOOL = "aiworkhub_dashboard_initialize"'));
assert.ok(ext.includes('"initializeStorage"'));
assert.ok(ext.includes("pushInitializeStorage"));
assert.ok(ext.includes("REAL_REPO_ID_RE"));
assert.ok(ext.includes("no_repository_selected"));
assert.ok(ext.includes("showQuickPick"));
assert.ok(ext.includes("workspaceState.get(WSP_STATE_KEY_REPO_URI"));
assert.ok(ext.includes("workspaceState.update(WSP_STATE_KEY_REPO_URI"));

// Repository binding + exact child lifecycle closure.
assert.ok(ext.includes("async stopDispatcherThenTerminate("));
assert.ok(ext.includes("_terminateOwnedChild"));
assert.ok(ext.includes("candidate === this.lifecycleChild"));
assert.ok(ext.includes("panel.__aiworkhubViewState.dispose()"));
assert.ok(!ext.includes(".aiworkinghub"), "the legacy .aiworkinghub path must never be referenced");
{
  const handshakeStart = ext.indexOf("async _handshake()");
  const handshakeEnd = ext.indexOf("async _assertRuntimeVersionBeforeServices()", handshakeStart);
  const handshakeBody = ext.slice(handshakeStart, handshakeEnd);
  assert.ok(handshakeStart >= 0 && handshakeEnd > handshakeStart);
  assert.ok(!handshakeBody.includes("this.callTool("), "handshake must never recurse through ensureStarted()");
  assert.ok(handshakeBody.includes("this._convergeBackgroundServices()"));
}

const deactivateMarker = "async function deactivate()";
const deactivateBody = ext.slice(ext.indexOf(deactivateMarker), ext.indexOf(deactivateMarker) + 500);
assert.ok(deactivateBody.includes("stopDispatcherThenTerminate"));

assert.ok(ext.includes("defaultCoordinatorTargets"));
assert.ok(ext.includes("selected_provider"));
assert.ok(ext.includes("claim_episode"));
assert.ok(ext.includes("extension_host_pid"));
assert.ok(ext.includes("thread_id"));
assert.ok(ext.includes("session_id"));
assert.ok(ext.includes('capability_state: "route_pending"'));
assert.ok(!ext.includes("thread_id: `codex:${WINDOW_SCOPE_ID}`"), "Codex route must not fabricate a callback thread id");
assert.ok(ext.includes("target.route = { ...defaults.targets[provider].route, ...existingRoute }"));
assert.ok(ext.includes("callback_required"));
assert.ok(ext.includes("notify_and_open_chat"));
assert.ok(app.includes('case "coordinatorTargets"'));
assert.ok(app.includes('type: "selectCoordinatorTarget"'));
assert.ok(app.includes("Manager verified · callback unavailable"));
assert.ok(app.includes("Manager verified · callback route pending"));
assert.ok(app.includes("Manager identity, route and callback verified"));

// v0.6.9: Live Output presents the provider's terminal envelope as readable
// sections and metrics. The opaque JSON is retained only in a collapsed raw
// disclosure for diagnostics.
assert.ok(app.includes("function renderFormattedLiveOutput"));
assert.ok(app.includes('"Result"'));
assert.ok(app.includes('"Model usage"'));
assert.ok(app.includes("detailLiveOutputRawContent"));
assert.ok(ext.includes("Raw provider output"));
assert.ok(css.includes(".live-output-metrics"));

assert.ok(fs.existsSync(path.join(root, "media", "aiworkhub-icon.png")));
assert.ok(fs.statSync(path.join(root, "media", "aiworkhub-icon.png")).size > 1000);
assert.ok(fs.existsSync(path.join(root, "media", "aiworkhub-activity.svg")));

// B844: the packaging script must bundle the canonical aiworkhub Python
// package (including dashboard_static assets) into an extension-local
// runtime/ directory, sourced from the one canonical src/aiworkhub tree.
const packageVsix = read("test/package-vsix.js");
const packageJson = JSON.parse(read("package.json"));
assert.ok(packageJson.activationEvents.includes("*"));
assert.ok(packageVsix.includes('path.join(root, "..", "src", "aiworkhub")'));
assert.ok(packageVsix.includes('path.join(extensionDir, "runtime", "aiworkhub")'));
assert.ok(packageVsix.includes("__pycache__"));
assert.ok(packageVsix.includes("dashboard_static"));
assert.ok(fs.existsSync(path.join(root, "..", "src", "aiworkhub", "server.py")));
assert.ok(fs.existsSync(path.join(root, "..", "src", "aiworkhub", "dashboard_static", "index.html")));

// Codex config.toml runtime-path repair: activation must fix a stale
// AIWorkHub-owned PYTHONPATH entry (an older versioned
// shrec.aiworkhub-*/runtime install dir) without touching custom entries.
assert.ok(ext.includes("codexConfigTomlPath"));
assert.ok(ext.includes("repairCodexConfigTomlText"));
assert.ok(ext.includes("ensureCodexConfigTomlRepaired"));
assert.ok(ext.includes('".codex", "config.toml"'));
assert.ok(ext.includes("ensureCodexConfigTomlRepaired(context)"));

// repairCodexConfigTomlText is a pure string-in/string-out helper, but
// extension.js requires "vscode" at module scope; stub it (same pattern as
// test/multirepo-connecting.test.js) only long enough to load the module and
// grab __testInternals.
const Module = require("module");
const extensionPath = path.join(root, "extension.js");
const { repairCodexConfigTomlText } = (() => {
  delete require.cache[extensionPath];
  const originalLoad = Module._load;
  Module._load = function patchedLoad(request, parent, isMain) {
    if (request === "vscode") return {};
    return originalLoad.call(this, request, parent, isMain);
  };
  try {
    return require(extensionPath).__testInternals;
  } finally {
    Module._load = originalLoad;
  }
})();

{
  // Linux/macOS style old versioned path -> repaired to current runtime dir.
  const currentRuntimeDir = "/home/user/.vscode/extensions/shrec.aiworkhub-0.6.19/runtime";
  const before = [
    "[mcp_servers.aiworkhub]",
    'command = "python3"',
    "[mcp_servers.aiworkhub.env]",
    'PYTHONPATH = "/home/user/.vscode/extensions/shrec.aiworkhub-0.6.17/runtime"',
  ].join("\n");
  const expected = [
    "[mcp_servers.aiworkhub]",
    'command = "python3"',
    "[mcp_servers.aiworkhub.env]",
    `PYTHONPATH = "${currentRuntimeDir}"`,
  ].join("\n");
  const { text, changed } = repairCodexConfigTomlText(before, currentRuntimeDir);
  assert.strictEqual(changed, true);
  assert.strictEqual(text, expected);
}

{
  // Windows-style path with backslashes and a drive letter: the drive
  // prefix must not be duplicated (regression for the "C:C:\..." bug).
  const currentRuntimeDir = "C:\\Users\\dev\\.vscode\\extensions\\shrec.aiworkhub-0.6.19\\runtime";
  const before = [
    "[mcp_servers.aiworkhub.env]",
    'PYTHONPATH = "C:\\Users\\dev\\.vscode\\extensions\\shrec.aiworkhub-0.6.10\\runtime"',
  ].join("\n");
  const expected = [
    "[mcp_servers.aiworkhub.env]",
    `PYTHONPATH = "${currentRuntimeDir}"`,
  ].join("\n");
  const { text, changed } = repairCodexConfigTomlText(before, currentRuntimeDir);
  assert.strictEqual(changed, true);
  assert.strictEqual(text, expected);
  assert.ok(!text.includes("C:C:"));
}

{
  // Windows path with a case-insensitive "AIWorkHub_Ultrafast" section name.
  const currentRuntimeDir = "C:\\Users\\dev\\.vscode\\extensions\\shrec.aiworkhub-0.6.19\\runtime";
  const before = [
    "[mcp_servers.AIWorkHub_Ultrafast.env]",
    'PYTHONPATH = "C:\\Users\\dev\\.vscode\\extensions\\shrec.aiworkhub-0.6.10\\runtime"',
  ].join("\n");
  const expected = [
    "[mcp_servers.AIWorkHub_Ultrafast.env]",
    `PYTHONPATH = "${currentRuntimeDir}"`,
  ].join("\n");
  const { text, changed } = repairCodexConfigTomlText(before, currentRuntimeDir);
  assert.strictEqual(changed, true);
  assert.strictEqual(text, expected);
  assert.ok(!text.includes("C:C:"));
}

{
  // Already current: idempotent, no rewrite.
  const currentRuntimeDir = "/Users/dev/.vscode/extensions/shrec.aiworkhub-0.6.19/runtime";
  const before = [
    "[mcp_servers.aiworkhub.env]",
    `PYTHONPATH = "${currentRuntimeDir}"`,
  ].join("\n");
  const { text, changed } = repairCodexConfigTomlText(before, currentRuntimeDir);
  assert.strictEqual(changed, false);
  assert.strictEqual(text, before);
}

{
  // A custom/non-AIWorkHub MCP entry's PYTHONPATH must never be altered.
  const currentRuntimeDir = "/home/user/.vscode/extensions/shrec.aiworkhub-0.6.19/runtime";
  const before = [
    "[mcp_servers.custom_tool]",
    'command = "node"',
    "[mcp_servers.custom_tool.env]",
    'PYTHONPATH = "/opt/some/other/vendor/runtime"',
  ].join("\n");
  const { text, changed } = repairCodexConfigTomlText(before, currentRuntimeDir);
  assert.strictEqual(changed, false);
  assert.strictEqual(text, before);
}

{
  // Section-aware ownership: a custom section whose PYTHONPATH happens to
  // point at a shrec.aiworkhub-*/runtime shaped path (e.g. pointing at a
  // shrec.aiworkhub runtime for its own reasons) must never be rewritten --
  // only canonical [mcp_servers.aiworkhub...] / [mcp_servers.aiworkhub_ultrafast...]
  // sections are owned.
  const currentRuntimeDir = "/home/user/.vscode/extensions/shrec.aiworkhub-0.6.19/runtime";
  const before = [
    "[mcp_servers.my_custom_aiworkhub_fork.env]",
    'PYTHONPATH = "/home/user/.vscode/extensions/shrec.aiworkhub-0.6.5/runtime"',
  ].join("\n");
  const { text, changed } = repairCodexConfigTomlText(before, currentRuntimeDir);
  assert.strictEqual(changed, false);
  assert.strictEqual(text, before);
}

{
  // Multi-entry PYTHONPATH (POSIX ':' delimiter): only the AIWorkHub segment
  // is replaced, the unrelated sibling entry is preserved untouched.
  const currentRuntimeDir = "/home/user/.vscode/extensions/shrec.aiworkhub-0.6.19/runtime";
  const before = [
    "[mcp_servers.aiworkhub.env]",
    'PYTHONPATH = "/opt/some/other/vendor/runtime:/home/user/.vscode/extensions/shrec.aiworkhub-0.6.5/runtime"',
  ].join("\n");
  const expected = [
    "[mcp_servers.aiworkhub.env]",
    `PYTHONPATH = "/opt/some/other/vendor/runtime:${currentRuntimeDir}"`,
  ].join("\n");
  const { text, changed } = repairCodexConfigTomlText(before, currentRuntimeDir);
  assert.strictEqual(changed, true);
  assert.strictEqual(text, expected);
}

{
  // Windows semicolon-delimited PYTHONPATH list: only the AIWorkHub segment
  // is replaced, the unrelated sibling entry is preserved untouched.
  const currentRuntimeDir = "C:\\Users\\dev\\.vscode\\extensions\\shrec.aiworkhub-0.6.19\\runtime";
  const before = [
    "[mcp_servers.aiworkhub.env]",
    'PYTHONPATH = "C:\\opt\\vendor\\runtime;C:\\Users\\dev\\.vscode\\extensions\\shrec.aiworkhub-0.6.5\\runtime"',
  ].join("\n");
  const expected = [
    "[mcp_servers.aiworkhub.env]",
    `PYTHONPATH = "C:\\opt\\vendor\\runtime;${currentRuntimeDir}"`,
  ].join("\n");
  const { text, changed } = repairCodexConfigTomlText(before, currentRuntimeDir);
  assert.strictEqual(changed, true);
  assert.strictEqual(text, expected);
}

{
  // Non-PYTHONPATH lines referencing a stale path must never be touched.
  const currentRuntimeDir = "/home/user/.vscode/extensions/shrec.aiworkhub-0.6.19/runtime";
  const before = [
    "[mcp_servers.aiworkhub.env]",
    "# see /home/user/.vscode/extensions/shrec.aiworkhub-0.6.5/runtime for details",
  ].join("\n");
  const { text, changed } = repairCodexConfigTomlText(before, currentRuntimeDir);
  assert.strictEqual(changed, false);
  assert.strictEqual(text, before);
}

console.log("AIWorkHub VS Code extension static contract checks passed");
