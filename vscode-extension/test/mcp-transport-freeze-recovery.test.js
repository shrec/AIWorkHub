"use strict";

// NF-2026-02223: after an extension-host stall the dashboard stayed on
// "Dashboard snapshot delayed (mcp_request_timeout)". Node runs expired timers
// before it reads stdout, so a reply that already sat in the child's pipe lost
// the race to its own timeout, and the timed-out frame was still written to
// the child afterwards. These tests pin the lag-tolerant timeout, the
// dead-frame skip, the off-thread retention scan and the stall probe.
const assert = require("node:assert/strict");
const { Worker } = require("node:worker_threads");
const fs = require("node:fs");
const Module = require("node:module");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const vscode = {
  workspace: { workspaceFolders: [], getConfiguration: () => ({ get: (_key, fallback) => fallback }) },
  window: { createOutputChannel: () => ({ appendLine: () => {}, dispose: () => {} }) },
  LanguageModelChatToolMode: { Auto: 1, Required: 2 },
  LanguageModelToolResultPart: class { constructor(callId, content) { Object.assign(this, { callId, content }); } },
  LanguageModelChatMessage: {
    User: (content) => ({ role: "user", content }),
    Assistant: (content) => ({ role: "assistant", content }),
  },
};
const originalLoad = Module._load;
Module._load = function (name, parent, isMain) {
  return name === "vscode" ? vscode : originalLoad.call(this, name, parent, isMain);
};
let extension;
try { extension = require("../extension.js"); }
finally { Module._load = originalLoad; }
const internals = extension.__testInternals;
const C = internals.constants;
const retention = require("../runtime-retention");

const traceDir = fs.mkdtempSync(path.join(os.tmpdir(), "aiworkhub-freeze-trace-"));
let traceSeq = 0;

function bindTrace() {
  traceSeq += 1;
  return internals.bindDebugTraceFileForTest(
    path.join(traceDir, `extension-freeze${traceSeq}-${process.pid}-window_${"a".repeat(24)}.jsonl`),
  );
}

function traceEvents(file, event) {
  internals.flushDebugTrace(false);
  if (!fs.existsSync(file)) return [];
  return fs.readFileSync(file, "utf8").split("\n").filter(Boolean)
    .map((line) => JSON.parse(line))
    .filter((record) => record.event === event);
}

function busyWait(ms) {
  const until = Date.now() + ms;
  while (Date.now() < until) { /* deliberate host stall */ }
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function newClient(label) {
  return new internals.McpStdioClient(
    path.join(os.tmpdir(), "freeze-repo"),
    { appendLine: () => {} },
    { uriStr: "file:///freeze-repo", repoId: `repo_${"f".repeat(32)}`, root: path.join(os.tmpdir(), "freeze-repo") },
    `episode_${label}`,
  );
}

// A child whose stdin is recorded instead of written; `release()` completes
// the oldest in-flight write so the single-writer queue advances.
function fakeChild(pid, { holdWrites = false } = {}) {
  const writes = [];
  const callbacks = [];
  const child = {
    pid,
    killed: false,
    kill: () => { child.killed = true; },
    stdin: {
      write(payload, callback) {
        writes.push(String(payload));
        if (holdWrites) callbacks.push(callback);
        else setImmediate(() => callback(null));
        return true;
      },
    },
  };
  return { child, writes, release: () => callbacks.shift()(null) };
}

function adopt(client, child, { initialized, owned = true }) {
  client.child = child;
  if (owned) {
    client.lifecycleChild = child;
    client.lifecyclePid = child.pid;
  }
  client.initialized = initialized;
}

function reply(client, child, id, result) {
  client._onStdout(child, Buffer.from(`${JSON.stringify({ jsonrpc: "2.0", id, result })}\n`, "utf8"));
}

test("a reply already in the stdout pipe wins over a timer delayed by a host stall", async () => {
  // The peer is a worker thread rather than a pipe-stdio process: inside a
  // Windows AppContainer sandbox a pipe-stdio spawn never returns
  // (NF-2026-01644). Its replies reach the host through libuv's poll phase,
  // exactly like stdout data, so an expired timer still runs before them.
  const flag = new Int32Array(new SharedArrayBuffer(4));
  const peer = new Worker([
    "const { parentPort, workerData } = require('worker_threads');",
    "const flag = new Int32Array(workerData.flag);",
    "parentPort.on('message', (frame) => {",
    "  const message = JSON.parse(frame);",
    "  if (message.id === undefined) return;",
    "  parentPort.postMessage(JSON.stringify({ jsonrpc: '2.0', id: message.id, result: { echo: message.method } }) + '\\n');",
    "  Atomics.store(flag, 0, message.id);",
    "});",
    "parentPort.postMessage('ready');",
  ].join("\n"), { eval: true, workerData: { flag: flag.buffer } });
  try {
    await new Promise((resolve, reject) => {
      peer.once("message", resolve);
      peer.once("error", reject);
    });
    const child = {
      pid: 510000,
      killed: false,
      kill: () => { child.killed = true; },
      stdin: {
        write(payload, callback) {
          peer.postMessage(String(payload));
          setImmediate(() => callback(null));
          return true;
        },
      },
    };
    const client = newClient("stall_reply");
    // Not yet initialized: today an expired timer here also drops the child.
    adopt(client, child, { initialized: false, owned: false });
    client.lifecycleChild = child;
    client.lifecyclePid = child.pid;
    // _onStdout only accepts frames from the owned child; ownership is kept
    // while the kill path is fenced so no real pid is ever signalled.
    client._terminateOwnedChild = () => { child.killed = true; return true; };
    peer.on("message", (frame) => client._onStdout(child, Buffer.from(String(frame), "utf8")));
    const timeoutMs = 300;
    const pending = client.request("ping", {}, timeoutMs);
    pending.catch(() => {});
    const id = client.nextId - 1;
    const started = Date.now();
    // Block the host past the timeout AFTER the peer has sent its reply.
    while (!(Atomics.load(flag, 0) === id && Date.now() - started >= timeoutMs + 2000)) {
      if (Date.now() - started > 20000) break;
    }
    assert.equal(Atomics.load(flag, 0), id, "the peer must have sent its reply during the stall");
    const result = await pending;
    assert.deepEqual(result, { echo: "ping" });
    assert.equal(client.child, child, "the child must not be dropped");
    assert.equal(child.killed, false, "the child must not be killed");
    assert.equal(client.pending.size, 0);
  } finally {
    await peer.terminate();
  }
});

test("a request that timed out before it was written never reaches the child", async () => {
  const client = newClient("dead_frame");
  const { child, writes, release } = fakeChild(510001, { holdWrites: true });
  adopt(client, child, { initialized: true });
  const first = client.request("tools/call", { name: "first" }, 10000);
  const doomed = client.request("tools/call", { name: "doomed" }, 30);
  const doomedId = client.nextId - 1;
  client.notify("notifications/progress", { token: "n" });
  const after = client.request("tools/call", { name: "after" }, 10000);
  const afterId = client.nextId - 1;
  assert.equal(writes.length, 1, "only the first frame is in flight");
  await assert.rejects(doomed, /mcp_request_timeout/);
  release();
  assert.equal(writes.length, 2);
  assert.match(writes[1], /notifications\/progress/, "notify frames are always written");
  release();
  assert.equal(writes.length, 3, "the request enqueued after the dead frame is written");
  assert.equal(JSON.parse(writes[2]).id, afterId);
  assert.ok(
    !writes.some((frame) => JSON.parse(frame).id === doomedId),
    "the timed-out frame must never be written",
  );
  reply(client, child, afterId, { ok: "after" });
  reply(client, child, 1, { ok: "first" });
  assert.deepEqual(await after, { ok: "after" });
  assert.deepEqual(await first, { ok: "first" });
});

test("an on-time timeout rejects exactly as before and still kills an uninitialized child", async () => {
  const client = newClient("on_time");
  const { child } = fakeChild(510002);
  adopt(client, child, { initialized: false, owned: false });
  const traceFile = bindTrace();
  const pending = client.request("tools/call", { name: "slow_tool" }, 40);
  const id = client.nextId - 1;
  const err = await pending.then(() => null, (error) => error);
  assert.ok(err, "the request must reject");
  assert.equal(err.message, "mcp_request_timeout");
  assert.equal(err.mcpDiagnostics.phase, "request_wait");
  assert.equal(err.mcpDiagnostics.request_id, id);
  assert.equal(err.mcpDiagnostics.method, "tools/call");
  assert.equal(err.mcpDiagnostics.tool, "slow_tool");
  assert.equal(err.mcpDiagnostics.child_pid, 510002);
  assert.equal(err.mcpDiagnostics.timeout_ms, 40);
  assert.ok(Number.isFinite(err.mcpDiagnostics.lag_ms) && err.mcpDiagnostics.lag_ms <= C.MCP_TIMER_LAG_TOLERANCE_MS);
  assert.equal(client.child, null, "an uninitialized child is still dropped on an on-time timeout");
  assert.equal(client.pending.size, 0);
  const timeouts = traceEvents(traceFile, "mcp.request.timeout");
  assert.equal(timeouts.length, 1);
  assert.ok(Number.isFinite(timeouts[0].lag_ms));
  assert.equal(traceEvents(traceFile, "mcp.request.timeout_deferred").length, 0);
});

test("a stalled timeout with no reply rejects once, after one grace", async () => {
  assert.equal(C.MCP_TIMER_LAG_TOLERANCE_MS, 1000);
  assert.equal(C.MCP_TIMER_LAG_GRACE_MS, 5000);
  const client = newClient("stall_no_reply");
  const { child } = fakeChild(510003);
  adopt(client, child, { initialized: false, owned: false });
  const traceFile = bindTrace();
  let settled = 0;
  const pending = client.request("tools/call", { name: "never" }, 50);
  const outcome = pending.then(() => { settled += 1; return null; }, (error) => { settled += 1; return error; });
  const id = client.nextId - 1;
  busyWait(50 + C.MCP_TIMER_LAG_TOLERANCE_MS + 700);
  const stallEnded = Date.now();
  await sleep(30);
  assert.equal(settled, 0, "a stalled timer must not reject immediately");
  assert.ok(client.pending.has(id));
  assert.equal(client.child, child, "the uninitialized child is kept during the grace");
  const deferred = traceEvents(traceFile, "mcp.request.timeout_deferred");
  assert.equal(deferred.length, 1);
  assert.equal(deferred[0].id, id);
  assert.ok(deferred[0].lag_ms > C.MCP_TIMER_LAG_TOLERANCE_MS);
  const err = await outcome;
  assert.ok(Date.now() - stallEnded >= C.MCP_TIMER_LAG_GRACE_MS - 50, "rejection waits for the grace");
  assert.equal(err.message, "mcp_request_timeout");
  assert.ok(err.mcpDiagnostics.lag_ms > C.MCP_TIMER_LAG_TOLERANCE_MS);
  assert.equal(client.child, null, "the uninitialized child is dropped at the final rejection");
  await sleep(20);
  assert.equal(settled, 1);
  assert.equal(traceEvents(traceFile, "mcp.request.timeout").filter((row) => row.id === id).length, 1);
});

test("a reply during the grace resolves and clears the grace timer", async () => {
  const client = newClient("stall_grace_reply");
  const { child } = fakeChild(510004);
  adopt(client, child, { initialized: true });
  const pending = client.request("tools/call", { name: "late" }, 50);
  pending.catch(() => {});
  const id = client.nextId - 1;
  busyWait(50 + C.MCP_TIMER_LAG_TOLERANCE_MS + 700);
  await sleep(30);
  const record = client.pending.get(id);
  assert.ok(record && record.timer, "a grace timer is armed");
  const graceTimer = record.timer;
  reply(client, child, id, { ok: true });
  assert.deepEqual(await pending, { ok: true });
  assert.equal(graceTimer._destroyed, true, "the grace timer is cleared by the resolve path");
});

test("a child exit during the grace clears the current timer", async () => {
  const client = newClient("stall_grace_exit");
  const { child } = fakeChild(510005);
  adopt(client, child, { initialized: true });
  const pending = client.request("tools/call", { name: "late" }, 50);
  const outcome = pending.then(() => null, (error) => error);
  const id = client.nextId - 1;
  busyWait(50 + C.MCP_TIMER_LAG_TOLERANCE_MS + 700);
  await sleep(30);
  const graceTimer = client.pending.get(id).timer;
  client._failPendingForChild(child, new Error("mcp_child_exited code=1 signal=null"));
  const err = await outcome;
  assert.match(err.message, /mcp_child_exited/);
  assert.equal(graceTimer._destroyed, true, "no timer may fire after the child exit");
  assert.equal(client.pending.size, 0);
});

function snapshotHarness() {
  const view = {
    snapshotRequestSeq: 0,
    snapshotDeltaCache: null,
    fullSnapshotPainted: false,
    messages: [],
    bindClient() {},
    stillBoundTo() { return true; },
    postMessage(message) { this.messages.push(message); },
  };
  const client = {
    repositoryIdentity: { uriStr: "file:///freeze-repo", repoId: `repo_${"f".repeat(32)}` },
    claimEpisode: "episode_snapshot",
    runtimePreflight: null,
    callTool: async () => ({ snapshot_mode: "full", tasks: [] }),
    _convergeBackgroundServices() {},
  };
  return { view, client };
}

function retentionFixture() {
  const storageRoot = fs.mkdtempSync(path.join(os.tmpdir(), "aiworkhub-freeze-retention-"));
  const name = `0.9.0-${"9".repeat(64)}`;
  const root = path.join(storageRoot, "runtime", "generations", name);
  fs.mkdirSync(path.join(root, "runtime"), { recursive: true });
  fs.writeFileSync(path.join(root, "runtime", "server.py"), "x");
  fs.writeFileSync(path.join(root, "manifest.json"), JSON.stringify({
    schema_id: retention.GENERATION_SCHEMA,
    version: "0.9.0",
    fingerprint: "9".repeat(64),
    created_at: new Date().toISOString(),
  }));
  fs.writeFileSync(path.join(storageRoot, "runtime", "current.json"), JSON.stringify({ generation: name }));
  return storageRoot;
}

test("the snapshot post never scans retention on the host thread", async () => {
  const storageRoot = retentionFixture();
  const realScan = retention.scan;
  retention.scan = () => { throw new Error("host-thread scan is forbidden"); };
  try {
    internals.bindStableRuntimeInfoForTest({ storageRoot, generationRoot: "", runtimeDir: "" });
    const { view, client } = snapshotHarness();
    const first = await internals.pushSnapshotNoRetry(view, { client, convergeBackgroundServices: false });
    assert.equal(first.posted, true);
    const firstPost = view.messages.find((message) => message.payload);
    assert.ok(!Object.prototype.hasOwnProperty.call(firstPost.payload, "extension_runtime_storage"),
      "before the first background scan the post has no extension_runtime_storage");
    const inFlight = internals.runtimeRetentionScanForTest();
    assert.ok(inFlight, "the post starts one background scan");
    await inFlight;
    view.messages.length = 0;
    const second = await internals.pushSnapshotNoRetry(view, { client, convergeBackgroundServices: false });
    assert.equal(second.posted, true);
    const storage = view.messages.find((message) => message.payload).payload.extension_runtime_storage;
    assert.equal(storage.ok, true);
    assert.equal(storage.generation_count, 1);
  } finally {
    retention.scan = realScan;
    internals.bindStableRuntimeInfoForTest(null);
    fs.rmSync(storageRoot, { recursive: true, force: true });
  }
});

test("the retention cache runs one scan at a time and holds results for 60 s", async () => {
  const realScanAsync = retention.scanAsync;
  const calls = [];
  retention.scanAsync = (options) => new Promise((resolve, reject) => { calls.push({ options, resolve, reject }); });
  try {
    internals.bindStableRuntimeInfoForTest({ storageRoot: path.join(os.tmpdir(), "freeze-root"), generationRoot: "", runtimeDir: "" });
    assert.equal(internals.runtimeRetentionSnapshot(), undefined);
    assert.equal(internals.runtimeRetentionSnapshot(), undefined);
    assert.equal(calls.length, 1, "at most one background scan runs at a time");
    const payload = { ok: true, candidates: [], quarantine_batches: [] };
    const beforeEnd = Date.now();
    calls[0].resolve(payload);
    await internals.runtimeRetentionScanForTest();
    const cache = internals.runtimeRetentionCacheForTest();
    assert.ok(cache.cached_at >= beforeEnd, "cached_at is stamped when the scan finishes");
    assert.equal(internals.runtimeRetentionSnapshot(), payload);
    assert.equal(calls.length, 1, "a fresh cache does not rescan");
    cache.cached_at -= 61000;
    assert.equal(internals.runtimeRetentionSnapshot(), payload, "a stale cache is still served while it refreshes");
    assert.equal(calls.length, 2);
    calls[1].reject(new Error("scan exploded"));
    await internals.runtimeRetentionScanForTest();
    const failed = internals.runtimeRetentionSnapshot();
    assert.equal(failed.ok, false);
    assert.match(failed.error, /scan exploded/);
    assert.deepEqual(failed.candidates, []);
    assert.deepEqual(failed.quarantine_batches, []);
    assert.equal(calls.length, 2, "a failure is cached for 60 s too");
  } finally {
    retention.scanAsync = realScanAsync;
    internals.bindStableRuntimeInfoForTest(null);
  }
});

test("runtime cleanup awaits a fresh off-thread scan", async () => {
  const realScan = retention.scan;
  const realScanAsync = retention.scanAsync;
  let asyncCalls = 0;
  retention.scan = () => { throw new Error("host-thread scan is forbidden"); };
  retention.scanAsync = async () => {
    asyncCalls += 1;
    return { ok: true, candidate_count: 0, candidates: [], quarantine_batches: [] };
  };
  try {
    internals.bindStableRuntimeInfoForTest({ storageRoot: path.join(os.tmpdir(), "freeze-root"), generationRoot: "", runtimeDir: "" });
    const { view } = snapshotHarness();
    await internals.runRuntimeCleanup(view);
    assert.equal(asyncCalls, 1);
    assert.match(String(view.messages[0].message), /No lease-free obsolete runtime generations/);
  } finally {
    retention.scan = realScan;
    retention.scanAsync = realScanAsync;
    internals.bindStableRuntimeInfoForTest(null);
  }
});

test("an invalidation starts a fresh scan instead of joining the stale one", async () => {
  const realScanAsync = retention.scanAsync;
  const calls = [];
  retention.scanAsync = () => new Promise((resolve) => { calls.push(resolve); });
  try {
    internals.bindStableRuntimeInfoForTest({ storageRoot: path.join(os.tmpdir(), "freeze-root"), generationRoot: "", runtimeDir: "" });
    assert.equal(internals.runtimeRetentionSnapshot(), undefined);
    const stale = internals.runtimeRetentionScanForTest();
    internals.invalidateRuntimeRetentionCache();
    assert.equal(internals.runtimeRetentionSnapshot(), undefined);
    assert.equal(calls.length, 2, "the post-change snapshot starts its own scan");
    const fresh = internals.runtimeRetentionScanForTest();
    assert.notEqual(fresh, stale);
    calls[0]({ ok: true, generation_count: 2, candidates: [], quarantine_batches: [] });
    await stale;
    assert.equal(internals.runtimeRetentionCacheForTest(), null, "a pre-change result is never cached");
    assert.equal(internals.runtimeRetentionScanForTest(), fresh, "the stale scan never clears the fresh one");
    calls[1]({ ok: true, generation_count: 1, candidates: [], quarantine_batches: [] });
    await fresh;
    assert.equal(internals.runtimeRetentionSnapshot().generation_count, 1);
    assert.equal(calls.length, 2);
  } finally {
    retention.scanAsync = realScanAsync;
    internals.bindStableRuntimeInfoForTest(null);
  }
});

test("the stall probe writes one line per stall and nothing on healthy ticks", async () => {
  const traceFile = bindTrace();
  internals.startHostStallProbe();
  try {
    assert.equal(internals.isHostStallProbeActive(), true);
    await sleep(1200);
    assert.equal(traceEvents(traceFile, "host.event_loop.stall").length, 0, "healthy ticks write nothing");
    busyWait(3300);
    await sleep(1300);
    const stalls = traceEvents(traceFile, "host.event_loop.stall");
    assert.equal(stalls.length, 1, "one line per stall");
    assert.ok(stalls[0].stall_ms >= 2000);
    assert.ok(Number.isFinite(stalls[0].cpu_user_ms) && Number.isFinite(stalls[0].cpu_system_ms));
    assert.ok(stalls[0].cpu_user_ms + stalls[0].cpu_system_ms >= 1000, "a busy host stall burns CPU");
  } finally {
    internals.stopHostStallProbe();
  }
  assert.equal(internals.isHostStallProbeActive(), false);
  const source = fs.readFileSync(path.resolve(__dirname, "..", "extension.js"), "utf8");
  const deactivateBody = source.slice(source.indexOf("async function deactivate()"));
  assert.ok(deactivateBody.slice(0, 600).includes("stopHostStallProbe()"), "deactivate clears the probe");
  const activateBody = source.slice(source.indexOf("async function activate(context)"), source.indexOf("async function deactivate()"));
  assert.ok(activateBody.includes("startHostStallProbe()"), "activate starts the probe");
});

test.after(() => {
  internals.bindDebugTraceFileForTest("");
  fs.rmSync(traceDir, { recursive: true, force: true });
});
