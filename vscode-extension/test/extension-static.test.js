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
assert.deepStrictEqual(pkg.extensionKind, ["workspace"]);
assert.strictEqual(pkg.icon, "media/aiworkhub-icon.png");

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
assert.ok(ext.includes("retainContextWhenHidden: true"));
assert.ok(ext.includes("getHtmlForNavigatorWebview"));
assert.ok(ext.includes("Open Dashboard"));
assert.ok(ext.includes("Select Repository"));

assert.strictEqual((ext.match(/childProcess\.spawn/g) || []).length, 1);
assert.ok(ext.includes('["-m", "aiworkhub.server"]'));
assert.ok(ext.includes("AIWORKHUB_REPO_ROOT"));
assert.ok(ext.includes("AIWORKHUB_REPO_ID"));
assert.ok(ext.includes("AIWORKHUB_WINDOW_ID"));
assert.ok(ext.includes("AIWORKHUB_CLAIM_EPISODE"));
assert.ok(ext.includes('snapshot: "aiworkhub_dashboard_snapshot"'));
assert.ok(ext.includes("mcpClient.repositoryRoot !== root"));
assert.ok(ext.includes("mcpClient.stop({ restart: false })"));

// B844: `import aiworkhub` must resolve to this extension's own bundled
// runtime/ directory -- never a repository checkout, an editable install, or
// a fixed host-absolute path. Both the PYTHONPATH env var and the spawned
// child's own cwd must be derived from context.extensionUri, and the
// repository path must stay confined to the AIWORKHUB_REPO* data env vars.
assert.ok(ext.includes('path.join(context.extensionUri.fsPath, "runtime")'));
assert.ok(ext.includes("env.PYTHONPATH ="));
assert.ok(ext.includes("cwd: runtimeDir || root"));
assert.ok(!ext.includes('cwd: root,'));

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

assert.ok(ext.includes("defaultCoordinatorTargets"));
assert.ok(ext.includes("selected_provider"));
assert.ok(ext.includes("claim_episode"));
assert.ok(ext.includes("thread_id"));
assert.ok(ext.includes("session_id"));
assert.ok(ext.includes("callback_required"));
assert.ok(ext.includes("notify_and_open_chat"));
assert.ok(app.includes('case "coordinatorTargets"'));
assert.ok(app.includes('type: "selectCoordinatorTarget"'));

assert.ok(fs.existsSync(path.join(root, "media", "aiworkhub-icon.png")));
assert.ok(fs.statSync(path.join(root, "media", "aiworkhub-icon.png")).size > 1000);
assert.ok(fs.existsSync(path.join(root, "media", "aiworkhub-activity.svg")));

// B844: the packaging script must bundle the canonical aiworkhub Python
// package (including dashboard_static assets) into an extension-local
// runtime/ directory, sourced from the one canonical src/aiworkhub tree.
const packageVsix = read("test/package-vsix.js");
assert.ok(packageVsix.includes('path.join(root, "..", "src", "aiworkhub")'));
assert.ok(packageVsix.includes('path.join(extensionDir, "runtime", "aiworkhub")'));
assert.ok(packageVsix.includes("__pycache__"));
assert.ok(packageVsix.includes("dashboard_static"));
assert.ok(fs.existsSync(path.join(root, "..", "src", "aiworkhub", "server.py")));
assert.ok(fs.existsSync(path.join(root, "..", "src", "aiworkhub", "dashboard_static", "index.html")));

console.log("AIWorkHub VS Code extension static contract checks passed");
