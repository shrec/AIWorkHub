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

  console.log("dashboard snapshot timeout classification: ok");
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
