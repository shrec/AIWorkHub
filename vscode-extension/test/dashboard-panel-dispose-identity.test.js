const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

function dashboardFunctions() {
  const source = fs.readFileSync(path.join(__dirname, "..", "extension.js"), "utf8");
  const start = source.indexOf("async function openDashboardCommand(");
  const end = source.indexOf("\nasync function restartMcpConnectionCommand(", start);
  assert.notEqual(start, -1);
  assert.notEqual(end, -1);
  return source.slice(start, end);
}

function makePanel(name) {
  const disposeListeners = [];
  const viewStateListeners = [];
  const messageListeners = [];
  return {
    name,
    visible: true,
    revealCount: 0,
    webview: {
      html: "",
      posts: [],
      postMessage(message) { this.posts.push(message); },
      onDidReceiveMessage(listener) { messageListeners.push(listener); },
    },
    onDidDispose(listener) { disposeListeners.push(listener); },
    onDidChangeViewState(listener) { viewStateListeners.push(listener); },
    reveal() { this.revealCount += 1; },
    dispose() { for (const listener of disposeListeners) listener(); },
    receive(message) { for (const listener of messageListeners) listener(message); },
    show() { for (const listener of viewStateListeners) listener(); },
  };
}

test("stale dashboard disposal preserves the revived panel's authority and routing", async () => {
  const created = makePanel("created");
  const revived = makePanel("revived");
  const background = [];

  class FakeViewState {
    constructor(post) { this.post = post; this.disposeCount = 0; this.client = null; }
    bindClient(client) { this.client = client; }
    setVisible(visible) { this.visible = visible; }
    dispose() { this.disposeCount += 1; }
    emit(message) { this.post(message); }
  }

  const context = {
    panel: null,
    ViewState: FakeViewState,
    PANEL_VIEW_TYPE: "aiworkhub.dashboard",
    activeRepoIdentity: {},
    vscode: {
      ViewColumn: { Active: 1 },
      window: { createWebviewPanel: () => created },
    },
    applyWebviewOptions() {},
    getHtmlForWebview: () => "dashboard",
    recordSystemLog() {},
    getMcpClient: () => ({ id: "client" }),
    handleInboundMessage(view, message) { view.emit({ routed: message.type }); },
    pushRepositoryInfo() {},
    pushRuntimeInfo() {},
    pushCoordinatorTargets() {},
    pushSnapshot(view) { view.emit({ type: "snapshot" }); },
    runBackgroundTask(_label, operation) { background.push(operation); operation(); },
  };
  vm.createContext(context);
  vm.runInContext(`${dashboardFunctions()}\nthis.api = { openDashboardCommand, reviveDashboardPanel, refreshDashboardCommand, currentPanel: () => panel };`, context);

  await context.api.openDashboardCommand({});
  const createdView = created.__aiworkhubViewState;
  context.api.reviveDashboardPanel(revived, {}, {});
  const revivedView = revived.__aiworkhubViewState;
  assert.equal(createdView.disposeCount, 1, "adoption tears down the stale controller once");

  created.dispose();
  background[0]();
  assert.equal(createdView.disposeCount, 1, "stale disposal does not tear down twice");
  assert.equal(context.api.currentPanel(), revived);

  context.api.refreshDashboardCommand();
  revived.receive({ type: "refresh" });
  await context.api.openDashboardCommand({});
  assert.equal(revived.revealCount, 1);
  assert.deepEqual(created.webview.posts, [{ type: "snapshot" }]);
  assert.deepEqual(revived.webview.posts.slice(-2), [{ type: "snapshot" }, { routed: "refresh" }]);

  revived.dispose();
  assert.equal(revivedView.disposeCount, 1);
  assert.equal(revived.__aiworkhubViewState, null);
  assert.equal(context.api.currentPanel(), null);
  assert.ok(background.length >= 3);
});

test("same-panel dashboard revival disposes the replaced ViewState exactly once", () => {
  const revived = makePanel("revived");

  class FakeViewState {
    constructor(post) { this.post = post; this.disposeCount = 0; }
    bindClient() {}
    setVisible() {}
    dispose() { this.disposeCount += 1; }
    emit(message) { this.post(message); }
  }

  const context = {
    panel: null,
    ViewState: FakeViewState,
    PANEL_VIEW_TYPE: "aiworkhub.dashboard",
    activeRepoIdentity: {},
    vscode: { ViewColumn: { Active: 1 } },
    applyWebviewOptions() {},
    getHtmlForWebview: () => "dashboard",
    recordSystemLog() {},
    getMcpClient: () => ({ id: "client" }),
    handleInboundMessage() {},
    pushRepositoryInfo() {},
    pushRuntimeInfo() {},
    pushCoordinatorTargets() {},
    pushSnapshot() {},
    runBackgroundTask(_label, operation) { operation(); },
  };
  vm.createContext(context);
  vm.runInContext(`${dashboardFunctions()}\nthis.api = { reviveDashboardPanel, currentPanel: () => panel };`, context);

  context.api.reviveDashboardPanel(revived, {}, {});
  const firstView = revived.__aiworkhubViewState;
  context.api.reviveDashboardPanel(revived, {}, {});
  const replacementView = revived.__aiworkhubViewState;

  assert.equal(firstView.disposeCount, 1, "replacement disposes the original controller first");
  assert.notEqual(replacementView, firstView);
  assert.equal(context.api.currentPanel(), revived);
  revived.dispose();
  assert.equal(firstView.disposeCount, 1, "original controller is not disposed twice");
  assert.equal(replacementView.disposeCount, 1, "replacement controller is disposed once");
  assert.equal(revived.__aiworkhubViewState, null);
  assert.equal(context.api.currentPanel(), null);
});
