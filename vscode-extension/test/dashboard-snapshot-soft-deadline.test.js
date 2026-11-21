"use strict";

// NF-2026-02323: a slow but live dashboard reader never produces a sticky
// "Dashboard snapshot delayed" banner. Past its soft budget a request stays
// pending; its late reply is painted (replacing the banner); Retry keeps a live
// answered reader; only a dead/failed or hung reader is stopped and respawned;
// and a refresh joins an in-flight full snapshot instead of stacking frames.
// Fake child process and fake Webview only: no Python, no provider CLI.

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
const { constants } = internals;
const SNAPSHOT_TOOL = "aiworkhub_dashboard_snapshot";
const BUDGET_MS = constants.MCP_DASHBOARD_SNAPSHOT_TIMEOUT_MS;
const CAP_MS = constants.DASHBOARD_LATE_REPLY_CAP_MS;
const TIMEOUT_REASON = constants.DASHBOARD_REQUEST_TIMEOUT_REASON;

// setImmediate is not mocked, so this drains promise continuations while the
// mocked setTimeout clock stays frozen.
async function flush() {
  for (let i = 0; i < 20; i += 1) await new Promise((resolve) => setImmediate(resolve));
}

function fakeChild() {
  const child = {
    // No pid: treeKillMcpChild never shells out to taskkill for a fake.
    pid: undefined,
    exitCode: null,
    signalCode: null,
    killed: false,
    kills: 0,
    frames: [],
    stdin: {
      write(frame, callback) {
        child.frames.push(JSON.parse(frame));
        if (callback) callback(null);
        return true;
      },
    },
    kill() {
      child.kills += 1;
      child.killed = true;
      return true;
    },
  };
  return child;
}

function makeClient() {
  const client = new internals.DashboardProcessClient(
    "/work/repo",
    null,
    { uriStr: "file:///work/repo", repoId: "repo_soft_deadline" },
    "episode_soft_deadline",
  );
  const child = fakeChild();
  // A warm reader that has answered before.
  client.child = child;
  client.childAnswered = true;
  client.spawned = [];
  client._spawn = async function spawnFake() {
    const next = fakeChild();
    this.child = next;
    this.spawnCount += 1;
    this.childAnswered = false;
    this.childFailure = "";
    this.spawned.push(next);
  };
  client._convergeBackgroundServices = () => {};
  client.stops = [];
  const realStop = client.stop.bind(client);
  client.stop = (reason) => {
    client.stops.push(reason);
    return realStop(reason);
  };
  return { client, child };
}

function reply(client, child, id, result) {
  client._onStdout(child, Buffer.from(`${JSON.stringify({ id, result })}\n`, "utf8"));
}

// `painted` simulates a refresh after the first full paint; binding first keeps
// bindClient from resetting it on a fresh view.
function makeView(client, painted = false) {
  const messages = [];
  const view = new internals.ViewState((message) => messages.push(message));
  view.bindClient(client);
  view.fullSnapshotPainted = painted;
  return { view, messages };
}

const types = (messages) => messages.map((message) => message.type);

test("budget constants: the hard cap outlasts every soft budget", () => {
  assert.strictEqual(TIMEOUT_REASON, "dashboard_reader_timeout");
  assert.ok(internals.DASHBOARD_PROCESS_TOOLS.has(SNAPSHOT_TOOL));
  assert.ok(CAP_MS > BUDGET_MS && CAP_MS > constants.DASHBOARD_PROCESS_COLD_START_TIMEOUT_MS);
});

test("(a) a reply after the budget is painted, and the banner was posted first", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout", "Date"], now: 1_000_000 });
  const { client, child } = makeClient();
  const { view, messages } = makeView(client, true);

  const pushed = internals.pushSnapshotOnce(view, { client });
  await flush();
  assert.strictEqual(child.frames.length, 1);
  assert.deepStrictEqual(child.frames[0].arguments, { full: true });

  t.mock.timers.tick(BUDGET_MS);
  await flush();
  assert.deepStrictEqual(types(messages), ["snapshotDelayed"]);
  assert.strictEqual(messages[0].reason, TIMEOUT_REASON);
  assert.strictEqual(client.pending.size, 1, "the timed-out request must stay pending");

  reply(client, child, child.frames[0].id, { snapshot_mode: "full", marker: "late-full-snapshot" });
  await pushed;
  assert.deepStrictEqual(types(messages), ["snapshotDelayed", "snapshot"]);
  assert.strictEqual(messages[1].payload.marker, "late-full-snapshot");
  assert.strictEqual(view.fullSnapshotPainted, true);
  assert.ok(view.snapshotDeltaCache, "the late full payload becomes the delta base");
  assert.strictEqual(view.snapshotDeltaCache.payload.marker, "late-full-snapshot");
  assert.strictEqual(view.fullSnapshotFlight, null);
  assert.strictEqual(client.pending.size, 0);
  assert.deepStrictEqual(client.stops, []);
  assert.strictEqual(child.kills, 0, "a slow live reader is never killed by the soft budget");
});

test("(b) Retry on a live answered child keeps the process and joins the snapshot", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout", "Date"], now: 1_000_000 });
  const { client, child } = makeClient();
  const { view, messages } = makeView(client, true);

  const first = internals.pushSnapshotOnce(view, { client });
  await flush();
  t.mock.timers.tick(BUDGET_MS);
  await flush();
  assert.deepStrictEqual(types(messages), ["snapshotDelayed"]);

  client.consecutiveFailures = 2;
  client.retryNotBefore = Date.now() + 5000;
  client.resetForExplicitRetry();
  assert.deepStrictEqual(client.stops, [], "Retry must not stop a live answered reader");
  assert.strictEqual(child.kills, 0, "Retry must not tree-kill a live answered reader");
  assert.strictEqual(client.child, child);
  assert.strictEqual(client.spawnCount, 0, "Retry must not respawn a live answered reader");
  assert.strictEqual(client.pending.size, 1, "Retry keeps the pending snapshot request");
  assert.strictEqual(client.consecutiveFailures, 0);
  assert.strictEqual(client.retryNotBefore, 0);

  const retried = internals.pushSnapshotOnce(view, { client });
  await flush();
  assert.strictEqual(child.frames.length, 1, "the Retry snapshot joins the in-flight request");

  reply(client, child, child.frames[0].id, { snapshot_mode: "full", marker: "after-retry" });
  await Promise.all([first, retried]);
  const painted = messages.filter((message) => message.type === "snapshot");
  assert.strictEqual(painted.length, 1);
  assert.strictEqual(painted[0].payload.marker, "after-retry");
  assert.strictEqual(messages[messages.length - 1].type, "snapshot", "the late paint replaces the banner");
});

test("(c) Retry on a dead or failed child still stops and respawns it", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout", "Date"], now: 1_000_000 });

  // Dead child (exit not yet delivered) holding a request past its budget.
  {
    const { client, child } = makeClient();
    const call = client.callTool(SNAPSHOT_TOOL, { full: true }, 1000);
    await flush();
    t.mock.timers.tick(1000);
    const err = await call.then(() => null, (error) => error);
    assert.ok(err, "the budget must reject the caller");
    assert.strictEqual(err.message, TIMEOUT_REASON);
    assert.ok(err.lateReply && typeof err.lateReply.then === "function");

    child.exitCode = 1;
    client.resetForExplicitRetry();
    assert.deepStrictEqual(client.stops, ["dashboard_process_retry"]);
    assert.strictEqual(client.child, null);
    await assert.rejects(err.lateReply, /dashboard_process_retry/);

    const next = client.callTool(SNAPSHOT_TOOL, { full: true }, 1000);
    await flush();
    assert.strictEqual(client.spawned.length, 1, "the next call respawns the reader");
    const respawned = client.spawned[0];
    assert.strictEqual(respawned.frames.length, 1);
    reply(client, respawned, respawned.frames[0].id, { ok: true });
    assert.deepStrictEqual(await next, { ok: true });
  }

  // Live child whose stdin write failed: a recorded process-level failure.
  {
    const { client, child } = makeClient();
    child.stdin.write = (_frame, callback) => {
      callback(new Error("EPIPE"));
      return false;
    };
    await assert.rejects(client.callTool(SNAPSHOT_TOOL, { full: true }, 1000), /dashboard_process_write_failed/);
    client.resetForExplicitRetry();
    assert.deepStrictEqual(client.stops, ["dashboard_process_retry"]);
    assert.strictEqual(child.kills, 1);
    assert.strictEqual(client.child, null);
  }
});

test("(d) a request past the hard cap marks the reader hung and stops it", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout", "Date"], now: 1_000_000 });
  const { client, child } = makeClient();
  const call = client.callTool(SNAPSHOT_TOOL, { full: true }, 1000);
  await flush();
  t.mock.timers.tick(1000);
  const err = await call.then(() => null, (error) => error);
  assert.ok(err && err.lateReply, "the budget rejection carries the late reply");

  t.mock.timers.tick(CAP_MS - 1000 - 1);
  await flush();
  assert.deepStrictEqual(client.stops, [], "below the cap the reader is only slow");
  assert.strictEqual(child.kills, 0);
  assert.strictEqual(client.child, child);

  t.mock.timers.tick(1);
  await flush();
  assert.deepStrictEqual(client.stops, ["dashboard_process_hung"]);
  assert.strictEqual(child.kills, 1);
  assert.strictEqual(client.child, null);
  await assert.rejects(err.lateReply, /dashboard_process_hung/);

  const next = client.callTool(SNAPSHOT_TOOL, { full: true }, 1000);
  await flush();
  assert.strictEqual(client.spawned.length, 1, "the next call respawns a hung reader");
  reply(client, client.spawned[0], client.spawned[0].frames[0].id, { ok: true });
  assert.deepStrictEqual(await next, { ok: true });
});

test("(e) a refresh during an in-flight full snapshot writes no second frame", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout", "Date"], now: 1_000_000 });
  const { client, child } = makeClient();
  const { view, messages } = makeView(client);

  // First load: bounded summary, then the slow full snapshot.
  const first = internals.pushSnapshotOnce(view, { client });
  await flush();
  assert.strictEqual(child.frames.length, 1);
  assert.deepStrictEqual(child.frames[0].arguments, { full: false });
  reply(client, child, child.frames[0].id, { snapshot_mode: "summary" });
  await flush();
  assert.strictEqual(child.frames.length, 2);
  assert.deepStrictEqual(child.frames[1].arguments, { full: true });

  // A poll while the full snapshot is on time: no summary, no second full.
  const onTimePoll = internals.pushSnapshotOnce(view, { client });
  await flush();
  assert.strictEqual(child.frames.length, 2, "an on-time in-flight full snapshot is joined");

  t.mock.timers.tick(BUDGET_MS);
  await flush();
  assert.ok(types(messages).includes("snapshotDelayed"));

  // A poll while the full snapshot awaits its late reply.
  const latePoll = internals.pushSnapshotOnce(view, { client });
  await flush();
  assert.strictEqual(child.frames.length, 2, "a late in-flight full snapshot is joined");
  assert.strictEqual(client.pending.size, 1);

  reply(client, child, child.frames[1].id, { snapshot_mode: "full", marker: "joined" });
  await Promise.all([first, onTimePoll, latePoll]);
  const painted = messages.filter((message) => message.type === "snapshot");
  assert.strictEqual(painted.length, 1, "one reply, one paint");
  assert.strictEqual(painted[0].payload.marker, "joined");
  assert.strictEqual(messages[messages.length - 1].type, "snapshot");
  assert.strictEqual(child.frames.length, 2);
  assert.strictEqual(view.fullSnapshotFlight, null);
});
