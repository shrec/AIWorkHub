"use strict";

const assert = require("assert");
const Module = require("module");
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
  const messages = [];
  const calls = [];
  let convergenceCalls = 0;
  const client = {
    repositoryIdentity: { uriStr: "file:///work/repo", repoId: "repo_test" },
    claimEpisode: "episode_progressive_snapshot",
    recovery: { open: false },
    callTool: async (_name, args, timeoutMs) => {
      calls.push({ args, timeoutMs });
      return args.full
        ? { snapshot_mode: "full", status_counts: { active: 2 }, tasks: { pending: [{ task_id: "T1" }] } }
        : { snapshot_mode: "summary", status_counts: { active: 2 } };
    },
    _convergeBackgroundServices: () => { convergenceCalls += 1; },
  };
  const view = new extension.__testInternals.ViewState((message) => messages.push(message));
  view.bindClient(client);

  await extension.__testInternals.pushSnapshotOnce(view, { client });

  assert.deepStrictEqual(calls.map((call) => call.args), [{ full: false }, { full: true }]);
  assert.strictEqual(
    calls[0].timeoutMs,
    extension.__testInternals.constants.MCP_REQUEST_TIMEOUT_MS,
  );
  assert.strictEqual(
    calls[1].timeoutMs,
    extension.__testInternals.constants.MCP_DASHBOARD_SNAPSHOT_TIMEOUT_MS,
  );
  assert.deepStrictEqual(
    messages
      .filter((message) => message.type === "snapshotSummary" || message.type === "snapshot")
      .map((message) => [message.type, message.payload.snapshot_mode]),
    [["snapshotSummary", "summary"], ["snapshot", "full"]],
  );
  assert.strictEqual(convergenceCalls, 1);

  console.log("dashboard progressive snapshot: ok");
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
