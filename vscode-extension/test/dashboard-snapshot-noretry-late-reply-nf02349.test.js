"use strict";

// NF-2026-02349: a no-retry full snapshot that joins a slow in-flight read must
// paint that read's late reply, like pushSnapshotOnce does, without making its
// callers wait for it. Fake client and fake Webview only: no provider CLI, no
// MCP server, no VS Code.

const test = require("node:test");
const assert = require("node:assert");
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

const internals = extension.__testInternals;
const TIMEOUT_REASON = internals.constants.DASHBOARD_REQUEST_TIMEOUT_REASON;

// Drains promise continuations; the mocked clock is not advanced.
async function flush() {
  for (let i = 0; i < 20; i += 1) await new Promise((resolve) => setImmediate(resolve));
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

// The timeout a dashboard request throws when it missed its soft budget and
// stays pending in the reader; `late` settles with the reply or the abandonment.
function timeoutWithLate(late) {
  const err = new Error(TIMEOUT_REASON);
  err.lateReply = late;
  return err;
}

function makeClient(callTool, repoId = "repo_late_reply") {
  const client = {
    repositoryIdentity: { uriStr: "file:///work/repo", repoId },
    claimEpisode: `episode_${repoId}`,
    recovery: { open: false },
    converges: 0,
    callTool,
    _convergeBackgroundServices() {
      client.converges += 1;
    },
  };
  return client;
}

function makeView(client, painted = false) {
  const messages = [];
  const view = new internals.ViewState((message) => messages.push(message));
  view.bindClient(client);
  view.fullSnapshotPainted = painted;
  return { view, messages };
}

const types = (messages) => messages.map((message) => message.type);
const snapshots = (messages) => messages.filter((message) => message.type === "snapshot");

test("case 1: a timed-out no-retry snapshot is painted from its late reply", async () => {
  const late = deferred();
  const client = makeClient(async () => {
    throw timeoutWithLate(late.promise);
  });
  const { view, messages } = makeView(client);

  const result = await internals.pushSnapshotNoRetry(view, { client });
  assert.deepStrictEqual(result, { posted: false, payload: null }, "callers must not wait for the late reply");
  assert.deepStrictEqual(types(messages), ["snapshotDelayed"]);

  late.resolve({ snapshot_mode: "full", late_marker: "late-paint" });
  await flush();

  assert.deepStrictEqual(types(messages), ["snapshotDelayed", "snapshot"]);
  assert.strictEqual(messages[1].payload.late_marker, "late-paint");
  assert.strictEqual(view.fullSnapshotPainted, true);
});

test("case 2: a no-retry call joining a pushSnapshotOnce flight paints exactly one snapshot", async () => {
  const late = deferred();
  let calls = 0;
  const client = makeClient(async () => {
    calls += 1;
    throw timeoutWithLate(late.promise);
  });
  // Painted already, so the bounded summary is skipped and only the full read is in play.
  const { view, messages } = makeView(client, true);

  const once = internals.pushSnapshotOnce(view, { client });
  await flush();
  assert.strictEqual(view.fullSnapshotFlight && view.fullSnapshotFlight.late, true, "the once call must be awaiting its late reply");

  const noRetry = await internals.pushSnapshotNoRetry(view, { client, authoritative: false });
  assert.deepStrictEqual(noRetry, { posted: false, payload: null });
  assert.strictEqual(calls, 1, "the no-retry call must join the in-flight read, not write a second frame");

  late.resolve({ snapshot_mode: "full", late_marker: "joined-paint" });
  await once;
  await flush();

  assert.strictEqual(snapshots(messages).length, 1, "exactly one snapshot is posted when the late reply resolves");
  assert.strictEqual(snapshots(messages)[0].payload.late_marker, "joined-paint");
  assert.strictEqual(view.fullSnapshotPainted, true);
});

test("case 3a: a late reply after the view rebound to another client posts no snapshot", async () => {
  const late = deferred();
  const client = makeClient(async () => {
    throw timeoutWithLate(late.promise);
  });
  const other = makeClient(async () => {
    throw new Error("unused");
  }, "repo_other");
  const { view, messages } = makeView(client);

  await internals.pushSnapshotNoRetry(view, { client });
  view.bindClient(other);
  late.resolve({ snapshot_mode: "full", late_marker: "stale-paint" });
  await flush();

  assert.deepStrictEqual(types(messages), ["snapshotDelayed"]);
  assert.strictEqual(view.fullSnapshotPainted, false);
});

test("case 3b: a late reply that rejects posts nothing further and raises no unhandled rejection", async () => {
  const unhandled = [];
  const onUnhandled = (reason) => unhandled.push(reason);
  process.on("unhandledRejection", onUnhandled);
  try {
    const late = deferred();
    const client = makeClient(async () => {
      throw timeoutWithLate(late.promise);
    });
    const { view, messages } = makeView(client);

    await internals.pushSnapshotNoRetry(view, { client });
    late.reject(new Error("dashboard_request_abandoned"));
    await flush();
    await new Promise((resolve) => setImmediate(resolve));

    assert.deepStrictEqual(types(messages), ["snapshotDelayed"]);
    assert.strictEqual(view.fullSnapshotPainted, false);
    assert.deepStrictEqual(unhandled, []);
  } finally {
    process.removeListener("unhandledRejection", onUnhandled);
  }
});

test("case 4: convergeBackgroundServices:false is honoured on the late paint", async () => {
  const lateOff = deferred();
  const clientOff = makeClient(async () => {
    throw timeoutWithLate(lateOff.promise);
  });
  const offView = makeView(clientOff).view;
  await internals.pushSnapshotNoRetry(offView, { client: clientOff, convergeBackgroundServices: false });
  lateOff.resolve({ snapshot_mode: "full", late_marker: "no-converge" });
  await flush();
  assert.strictEqual(clientOff.converges, 0, "_convergeBackgroundServices must not run on the late paint");

  const lateOn = deferred();
  const clientOn = makeClient(async () => {
    throw timeoutWithLate(lateOn.promise);
  }, "repo_late_reply_default");
  const onView = makeView(clientOn).view;
  await internals.pushSnapshotNoRetry(onView, { client: clientOn });
  lateOn.resolve({ snapshot_mode: "full", late_marker: "converge" });
  await flush();
  assert.strictEqual(clientOn.converges, 1, "the default paints the late reply and converges once");
});

test("a timed-out no-retry call returns promptly and keeps the delayed banner", { timeout: 5000 }, async () => {
  const late = deferred();
  const client = makeClient(async () => {
    throw timeoutWithLate(late.promise);
  });
  const { view, messages } = makeView(client);

  const result = await internals.pushSnapshotNoRetry(view, { client, authoritative: true });
  assert.deepStrictEqual(result, { posted: false, payload: null });
  assert.strictEqual(messages[0].type, "snapshotDelayed");
  assert.strictEqual(messages[0].reason, TIMEOUT_REASON);
  late.resolve({ snapshot_mode: "full", late_marker: "authoritative" });
  await flush();
  assert.deepStrictEqual(types(messages), ["snapshotDelayed", "snapshot"]);
});

test("case 5: a late paint never forces authoritative, so a joined request posts one snapshot", async () => {
  const late = deferred();
  let calls = 0;
  const client = makeClient(async () => {
    calls += 1;
    throw timeoutWithLate(late.promise);
  });
  const { view, messages } = makeView(client, true);

  await internals.pushSnapshotNoRetry(view, { client, authoritative: true });
  const once = internals.pushSnapshotOnce(view, { client });
  await flush();
  assert.strictEqual(calls, 1, "the once call must join the no-retry flight, not write a second frame");

  late.resolve({ snapshot_mode: "full", late_marker: "single" });
  await once;
  await flush();

  assert.strictEqual(snapshots(messages).length, 1, "exactly one snapshot is posted for the shared late reply");
  assert.strictEqual(snapshots(messages)[0].payload.late_marker, "single");
  assert.strictEqual(client.converges, 1, "background services converge once, on the paint that posted");
});

test("case 6: a late paint whose converge throws is traced, not left as an unhandled rejection", async () => {
  const unhandled = [];
  const onUnhandled = (reason) => unhandled.push(reason);
  process.on("unhandledRejection", onUnhandled);
  try {
    const late = deferred();
    const client = makeClient(async () => {
      throw timeoutWithLate(late.promise);
    });
    client._convergeBackgroundServices = () => {
      throw new Error("converge_failed");
    };
    const { view, messages } = makeView(client);

    await internals.pushSnapshotNoRetry(view, { client });
    late.resolve({ snapshot_mode: "full", late_marker: "converge-throws" });
    await flush();
    await new Promise((resolve) => setImmediate(resolve));

    assert.deepStrictEqual(types(messages), ["snapshotDelayed", "snapshot"]);
    assert.deepStrictEqual(unhandled, [], "a throw after the late paint must be caught and traced");
  } finally {
    process.removeListener("unhandledRejection", onUnhandled);
  }
});
