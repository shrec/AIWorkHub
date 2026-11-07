"use strict";

const assert = require("assert");
const Module = require("module");
const fs = require("fs");
const path = require("path");

const extensionPath = path.resolve(__dirname, "..", "extension.js");
const originalLoad = Module._load;
const fakeVscode = {
  workspace: {
    workspaceFolders: [],
    getConfiguration: () => ({ get: () => 10000, inspect: () => ({}) }),
  },
  window: {
    createOutputChannel: () => ({ appendLine: () => {}, dispose: () => {} }),
  },
  Uri: { joinPath: (...parts) => ({ fsPath: parts.map((part) => part.fsPath || part).join("/") }) },
  ViewColumn: { Active: 1 },
  ConfigurationTarget: { Global: 1 },
};

Module._load = function patchedLoad(request, parent, isMain) {
  if (request === "vscode") return fakeVscode;
  return originalLoad.call(this, request, parent, isMain);
};

let extension;
try {
  extension = require(extensionPath);
} finally {
  Module._load = originalLoad;
}

(async () => {
  const internals = extension.__testInternals;
  assert.ok(
    internals.constants.MCP_DASHBOARD_SNAPSHOT_TIMEOUT_MS
      > internals.constants.MCP_REQUEST_TIMEOUT_MS,
    "full snapshots need a distinct aggregation budget",
  );

  const messages = [];
  let calls = 0;
  const timeoutClient = {
    repositoryIdentity: { uriStr: "file:///work/repo", repoId: "repo_test" },
    claimEpisode: "episode_snapshot_timeout",
    callTool: async (_name, _args, timeoutMs) => {
      calls += 1;
      assert.strictEqual(timeoutMs, internals.constants.MCP_DASHBOARD_SNAPSHOT_TIMEOUT_MS);
      throw new Error("mcp_request_timeout");
    },
  };
  const delayedView = new internals.ViewState((message) => messages.push(message));
  await internals.pushSnapshotNoRetry(delayedView, {
    client: timeoutClient,
    authoritative: true,
  });

  assert.strictEqual(calls, 1, "a delayed full snapshot must not create a retry storm");
  assert.deepStrictEqual(messages.map((message) => message.type), ["snapshotDelayed"]);
  assert.strictEqual(messages[0].reason, "mcp_request_timeout");

  const source = fs.readFileSync(extensionPath, "utf8");
  const snapshotStart = source.indexOf("async function pushSnapshotOnce(view)");
  const snapshotEnd = source.indexOf("async function pushSnapshotNoRetry", snapshotStart);
  const snapshotBody = source.slice(snapshotStart, snapshotEnd);
  assert.ok(
    snapshotBody.includes('if (snapshotRecovery.reason === "mcp_request_timeout") break;'),
    "the auto-refresh path must stop retrying a delayed full snapshot",
  );
  assert.ok(snapshotBody.includes("OUTBOUND_TYPES.snapshotDelayed"));

  const offlineMessages = [];
  const failedClient = {
    repositoryIdentity: { uriStr: "file:///work/repo", repoId: "repo_test" },
    claimEpisode: "episode_snapshot_failure",
    callTool: async () => { throw new Error("mcp_child_exited"); },
  };
  const failedView = new internals.ViewState((message) => offlineMessages.push(message));
  await internals.pushSnapshotNoRetry(failedView, {
    client: failedClient,
    authoritative: true,
  });
  assert.deepStrictEqual(offlineMessages.map((message) => message.type), ["offline"]);

  const extensionHtmlSource = fs.readFileSync(extensionPath, "utf8");
  assert.ok(
    extensionHtmlSource.includes('<strong id="offline-alert-title">MCP connection unavailable</strong>'),
    "the offline alert heading needs an id so the webview can retitle it",
  );

  // Drive the real webview alert functions against a stub DOM, so the heading
  // is asserted through the same code paths the dashboard uses.
  const appSource = fs.readFileSync(path.resolve(__dirname, "..", "media", "app.js"), "utf8");
  const extractFunction = (name) => {
    const start = appSource.indexOf(`function ${name}(`);
    assert.ok(start >= 0, `${name} must exist in media/app.js`);
    let depth = 0;
    for (let i = appSource.indexOf("{", start); i < appSource.length; i += 1) {
      if (appSource[i] === "{") depth += 1;
      else if (appSource[i] === "}") {
        depth -= 1;
        if (depth === 0) return appSource.slice(start, i + 1);
      }
    }
    throw new Error(`unterminated function ${name}`);
  };
  const stubElement = () => ({ hidden: true, textContent: "" });
  const alertContext = {
    elements: {
      offlineAlert: stubElement(),
      offlineAlertTitle: stubElement(),
      offlineAlertMessage: stubElement(),
      tableLoading: stubElement(),
      tableEmpty: stubElement(),
    },
    state: { snapshot: null },
    setConnection: () => {},
  };
  require("vm").createContext(alertContext);
  require("vm").runInContext(
    `${extractFunction("showOffline")}\n${extractFunction("showSnapshotDelayed")}`,
    alertContext,
  );
  const alert = alertContext.elements.offlineAlertTitle;

  require("vm").runInContext('showSnapshotDelayed("mcp_request_timeout")', alertContext);
  assert.strictEqual(alert.textContent, "Dashboard snapshot delayed");
  require("vm").runInContext("showOffline()", alertContext);
  assert.strictEqual(alert.textContent, "MCP connection unavailable");
  require("vm").runInContext('showSnapshotDelayed("mcp_request_timeout")', alertContext);
  assert.strictEqual(alert.textContent, "Dashboard snapshot delayed");
  require("vm").runInContext("showOffline()", alertContext);
  assert.strictEqual(alert.textContent, "MCP connection unavailable", "delayed -> offline must restore the offline heading");

  console.log("dashboard snapshot timeout classification: ok");
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
