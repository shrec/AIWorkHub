"use strict";

// NF-2026-00643 contract tests.
//
// Two independently measured defects, two independently failing assertions:
//   1. activation awaited the VS Code language-model catalog (n=40, median
//      6.870 s, p90 12.893 s, max 31.619 s) BEFORE registering any command or
//      webview provider, in an extension host shared with every other
//      extension in the window;
//   2. every trace line cost open(O_APPEND)+write+fsync+close, and nothing
//      ever pruned the directory: 1,102 files / 2,235,024,325 bytes measured.

const assert = require("assert");
const fs = require("fs");
const Module = require("module");
const os = require("os");
const path = require("path");

const extensionPath = path.resolve(__dirname, "..", "extension.js");
const originalLoad = Module._load;

// The catalog call is the thing under test, so it is the thing the fake
// controls: `catalogDelayMs` is how long vscode.lm.selectChatModels takes.
const lmState = { catalogDelayMs: 0, calls: 0 };
const fakeVscode = {
  workspace: {
    workspaceFolders: [],
    getConfiguration: () => ({ get: (_key, fallback) => fallback, inspect: () => ({}) }),
  },
  window: {
    createOutputChannel: () => ({ appendLine: () => {}, dispose: () => {} }),
    showInformationMessage: () => Promise.resolve(undefined),
    setStatusBarMessage: () => {},
  },
  lm: {
    selectChatModels: () => {
      lmState.calls += 1;
      // Deliberately NOT unref'd: this timer is the only thing holding the
      // event loop open across the awaited catalog, and an unref'd one lets
      // node exit silently mid-test instead of failing.
      return new Promise((resolve) => { setTimeout(() => resolve([]), lmState.catalogDelayMs); });
    },
  },
  Uri: { joinPath: (...parts) => ({ fsPath: parts.map((part) => part.fsPath || part).join("/") }) },
  ViewColumn: { Active: 1 },
  ConfigurationTarget: { Global: 1 },
};

const bridgeRoot = fs.mkdtempSync(path.join(os.tmpdir(), "aiworkhub-lm-bridge-"));
process.env.AIWORKHUB_VSCODE_LM_BRIDGE_ROOT = bridgeRoot;

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
const {
  debugTrace,
  debugTraceAppend,
  flushDebugTrace,
  pruneDebugTraces,
  debugTraceBufferedLineCount,
  bindDebugTraceFileForTest,
  currentDebugTraceFileForTest,
  VscodeLmBridgeHost,
} = internals;
const C = internals.constants;

// `fs.fsyncSync(fd)` is a property lookup on the same module object the
// extension holds, so counting it here counts the real call the extension
// makes. Each extension test file is spawned as its own process by
// test/run-all.js, so this patch cannot race another test.
const realFsync = fs.fsyncSync;
let fsyncCalls = 0;
fs.fsyncSync = function countedFsync(fd) {
  fsyncCalls += 1;
  return realFsync.call(fs, fd);
};

const traceDir = fs.mkdtempSync(path.join(os.tmpdir(), "aiworkhub-trace-"));
const traceFile = path.join(traceDir, `extension-stamp-${process.pid}-window_${"a".repeat(24)}.jsonl`);

function readLines(file) {
  if (!fs.existsSync(file)) return [];
  return fs.readFileSync(file, "utf8").split("\n").filter(Boolean);
}

// ── 1. the steady-state trace path buffers and never fsyncs ────────────────
bindDebugTraceFileForTest(traceFile);
fsyncCalls = 0;
for (let index = 0; index < 200; index += 1) {
  debugTrace("dashboard.tick", { index });
}
assert.strictEqual(
  fsyncCalls,
  0,
  `200 buffered trace lines must cost zero fsync, got ${fsyncCalls}`,
);
assert.ok(
  debugTraceBufferedLineCount() > 0,
  "trace lines must be batched in memory, not written one syscall at a time",
);
assert.strictEqual(
  readLines(traceFile).length,
  0,
  "nothing may reach the trace file before a flush",
);
flushDebugTrace(false);
assert.strictEqual(readLines(traceFile).length, 200, "a flush must write every buffered line");
assert.strictEqual(fsyncCalls, 0, "a non-durable flush must not fsync");
assert.strictEqual(debugTraceBufferedLineCount(), 0, "a flush must drain the buffer");
const firstRecord = JSON.parse(readLines(traceFile)[0]);
assert.strictEqual(firstRecord.schema_id, "aiworkhub.extension_debug_trace.v1");
assert.strictEqual(firstRecord.event, "dashboard.tick");
assert.strictEqual(firstRecord.index, 0);

// ── 2. a dying host still writes durably ──────────────────────────────────
for (const event of ["host.uncaught_exception", "host.unhandled_rejection", "host.exit", "host.signal"]) {
  assert.ok(C.DEBUG_TRACE_DURABLE_EVENTS.has(event), `${event} must stay durable`);
}
bindDebugTraceFileForTest(traceFile);
fsyncCalls = 0;
debugTrace("host.exit", { code: 1 });
assert.strictEqual(
  debugTraceBufferedLineCount(),
  0,
  "a terminating host must not leave its last trace line in a buffer",
);
assert.strictEqual(fsyncCalls, 1, `a durable trace line must fsync exactly once, got ${fsyncCalls}`);
const durableLines = readLines(traceFile);
assert.strictEqual(JSON.parse(durableLines[durableLines.length - 1]).event, "host.exit");

// ── 3. the file rotates at the byte cap, and rotation is the only fsync ────
const rotateFile = path.join(traceDir, `extension-rot-${process.pid}-window_${"b".repeat(24)}.jsonl`);
bindDebugTraceFileForTest(rotateFile);
fsyncCalls = 0;
const halfCap = `${"x".repeat(Math.floor(C.DEBUG_TRACE_MAX_FILE_BYTES * 0.6))}\n`;
debugTraceAppend(halfCap, false);
assert.strictEqual(currentDebugTraceFileForTest(), rotateFile, "the first chunk must not rotate");
assert.strictEqual(fsyncCalls, 0, "writing under the cap must not fsync");
debugTraceAppend(halfCap, false);
const rotated = currentDebugTraceFileForTest();
assert.strictEqual(
  rotated,
  rotateFile.replace(/\.jsonl$/, ".1.jsonl"),
  `crossing ${C.DEBUG_TRACE_MAX_FILE_BYTES} bytes must open the next part, got ${rotated}`,
);
assert.strictEqual(fsyncCalls, 1, `rotation must fsync the sealed file exactly once, got ${fsyncCalls}`);
assert.ok(fs.statSync(rotateFile).size <= C.DEBUG_TRACE_MAX_FILE_BYTES, "a sealed part must respect the cap");
assert.ok(fs.existsSync(rotated), "the new part must exist");
fs.fsyncSync = realFsync;

// ── 4. retention bounds the directory by age, count and total bytes ────────
const pruneDir = fs.mkdtempSync(path.join(os.tmpdir(), "aiworkhub-prune-"));
const now = Date.now();
const deadPidName = (label, ageMs, kind = "extension") =>
  `${kind}-${label}-4294967295-window_${"c".repeat(24)}.jsonl`;
const write = (name, bytes, ageMs) => {
  const full = path.join(pruneDir, name);
  fs.writeFileSync(full, "z".repeat(bytes));
  const when = new Date(now - ageMs);
  fs.utimesSync(full, when, when);
  return full;
};

const ancient = write(deadPidName("old", 0), 128, C.DEBUG_TRACE_MAX_AGE_MS + 60_000);
const mcpAncient = write(deadPidName("oldmcp", 0, "mcp"), 128, C.DEBUG_TRACE_MAX_AGE_MS + 60_000);
const unrelated = write("notes.txt", 128, C.DEBUG_TRACE_MAX_AGE_MS + 60_000);
// This window's own pid, written just now: an actively-written file is never a
// retention candidate however many files sit around it.
const live = write(`extension-live-${process.pid}-window_${"d".repeat(24)}.jsonl`, 128, 0);
const recent = [];
for (let index = 0; index < C.DEBUG_TRACE_KEEP_FILES + 8; index += 1) {
  recent.push(write(deadPidName(`recent${index}`, 0), 128, (index + 1) * 1000));
}

const removed = pruneDebugTraces(pruneDir, now);
assert.ok(!fs.existsSync(ancient), "an over-age extension trace must be removed");
assert.ok(!fs.existsSync(mcpAncient), "retention must also cover the MCP child's trace files");
assert.ok(fs.existsSync(unrelated), "retention must not touch a file it does not own");
assert.ok(fs.existsSync(live), "retention must never delete a live window's own trace file");
const survivors = fs.readdirSync(pruneDir).filter((name) => /^(?:extension|mcp)-.+\.jsonl$/.test(name));
assert.ok(
  survivors.length <= C.DEBUG_TRACE_KEEP_FILES + 1,
  `retention must bound the directory to ${C.DEBUG_TRACE_KEEP_FILES} inactive files plus the live one, got ${survivors.length}`,
);
assert.ok(removed.length >= 9, `retention must report what it removed, got ${removed.length}`);
// The newest inactive files are the ones kept.
assert.ok(fs.existsSync(recent[0]), "the newest inactive trace must survive");
assert.ok(!fs.existsSync(recent[recent.length - 1]), "the oldest inactive trace must not survive");

const byteDir = fs.mkdtempSync(path.join(os.tmpdir(), "aiworkhub-prune-bytes-"));
const big = Math.ceil(C.DEBUG_TRACE_MAX_TOTAL_BYTES / 3) + 1024;
const bigFiles = [];
for (let index = 0; index < 4; index += 1) {
  const full = path.join(byteDir, deadPidName(`big${index}`, 0));
  fs.writeFileSync(full, Buffer.alloc(big));
  const when = new Date(now - (index + 1) * 1000);
  fs.utimesSync(full, when, when);
  bigFiles.push(full);
}
pruneDebugTraces(byteDir, now);
const remainingBytes = fs.readdirSync(byteDir)
  .map((name) => fs.statSync(path.join(byteDir, name)).size)
  .reduce((total, size) => total + size, 0);
assert.ok(
  remainingBytes <= C.DEBUG_TRACE_MAX_TOTAL_BYTES,
  `retention must hold the directory under ${C.DEBUG_TRACE_MAX_TOTAL_BYTES} bytes, got ${remainingBytes}`,
);
assert.ok(!fs.existsSync(bigFiles[3]), "the oldest file over the byte budget must go first");

// ── 4b. initialization actually wires retention up ────────────────────────
// pruneDebugTraces being correct is worth nothing if nothing calls it.
const storageRoot = fs.mkdtempSync(path.join(os.tmpdir(), "aiworkhub-globalstorage-"));
const initTraceDir = path.join(storageRoot, "debug");
fs.mkdirSync(initTraceDir, { recursive: true });
const staleName = `extension-stale-4294967295-window_${"f".repeat(24)}.jsonl`;
const stalePath = path.join(initTraceDir, staleName);
fs.writeFileSync(stalePath, "stale\n");
const stale = new Date(Date.now() - C.DEBUG_TRACE_MAX_AGE_MS - 60_000);
fs.utimesSync(stalePath, stale, stale);

internals.initializeDebugTracing({ globalStorageUri: { fsPath: storageRoot } });
const initialised = currentDebugTraceFileForTest();
assert.ok(
  initialised.startsWith(initTraceDir),
  `initialization must bind a trace file under globalStorage, got ${initialised}`,
);
flushDebugTrace(false);
assert.strictEqual(
  JSON.parse(readLines(initialised)[0]).event,
  "trace.initialized",
  "initialization must still record that tracing started",
);

// ── 5. activation ordering is a source-level contract ─────────────────────
const source = fs.readFileSync(extensionPath, "utf8");
const configRepairEnd = source.indexOf('debugTrace("activation.config_repair.end")');
const providersRegistered = source.indexOf('debugTrace("activation.providers_registered")');
const bridgeDeferred = source.indexOf('debugTrace("activation.vscode_lm_bridge.deferred")');
assert.ok(configRepairEnd > 0 && providersRegistered > 0 && bridgeDeferred > 0, "activation markers must exist");
assert.ok(
  providersRegistered < bridgeDeferred,
  "commands and the webview provider must be registered before the language-model bridge is started",
);
const measuredWindow = source.slice(configRepairEnd, providersRegistered);
assert.ok(
  !measuredWindow.includes("vscodeLmBridgeHost.start"),
  "the measured activation window must contain no language-model bridge start",
);
assert.ok(
  !/await\s+vscodeLmBridgeHost\.start\(activeRepoIdentity\)[\s\S]{0,400}activation\.providers_registered/.test(source),
  "activation must not await the bridge before registering providers",
);

// ── 6. start() returns without waiting for the catalog ────────────────────
(async () => {
  // Retention is scheduled off the activation path, so it lands a tick later.
  await new Promise((resolve) => setTimeout(resolve, 50));
  assert.ok(
    !fs.existsSync(stalePath),
    "initializing tracing must schedule the retention sweep, not just define it",
  );
  fs.rmSync(storageRoot, { recursive: true, force: true });

  // Buffering is only safe if something drains it without being asked. A
  // sub-threshold batch must reach disk on the flush timer alone -- otherwise
  // a quiet window's traces sit in memory until the host dies.
  const timerFile = path.join(traceDir, `extension-timer-${process.pid}-window_${"e".repeat(24)}.jsonl`);
  bindDebugTraceFileForTest(timerFile);
  debugTrace("dashboard.tick", { via: "timer" });
  assert.strictEqual(readLines(timerFile).length, 0, "a sub-threshold line must be buffered first");
  await new Promise((resolve) => setTimeout(resolve, C.DEBUG_TRACE_FLUSH_MS + 750));
  assert.strictEqual(
    readLines(timerFile).length,
    1,
    `the flush timer must drain the buffer within ${C.DEBUG_TRACE_FLUSH_MS} ms without an explicit flush`,
  );

  const repoInfo = { repoId: `repo_${"e".repeat(32)}`, root: "/tmp/repo" };
  const context = { globalState: { get: () => false, update: async () => {} }, extension: { packageJSON: { version: "0.0.0" } } };

  lmState.catalogDelayMs = 1500;
  lmState.calls = 0;
  const host = new VscodeLmBridgeHost(context);
  const started = Date.now();
  await host.start(repoInfo);
  const elapsed = Date.now() - started;

  assert.ok(
    elapsed < 400,
    `start() must not wait for the language-model catalog: returned after ${elapsed} ms with a 1500 ms catalog`,
  );
  assert.ok(host.pollTimer, "start() must arm the request poll before the catalog resolves");
  assert.ok(host.heartbeatTimer, "start() must arm the heartbeat timer before the catalog resolves");
  assert.strictEqual(lmState.calls, 1, "start() must still ask for the catalog, in the background");

  const hostPath = path.join(bridgeRoot, "hosts", repoInfo.repoId);
  assert.ok(
    !fs.existsSync(hostPath) || fs.readdirSync(hostPath).length === 0,
    "the heartbeat cannot have been published yet -- the catalog has not resolved",
  );

  await host.initialHeartbeat;
  const published = fs.readdirSync(hostPath);
  assert.strictEqual(published.length, 1, "the background heartbeat must publish exactly one host record");
  const record = JSON.parse(fs.readFileSync(path.join(hostPath, published[0]), "utf8"));
  assert.strictEqual(record.repo_id, repoInfo.repoId);
  host.dispose();

  // A disposed host must not publish over the record of whoever came after it.
  const late = new VscodeLmBridgeHost(context);
  lmState.catalogDelayMs = 200;
  await late.start(repoInfo);
  late.dispose();
  await late.initialHeartbeat;

  fs.rmSync(traceDir, { recursive: true, force: true });
  fs.rmSync(pruneDir, { recursive: true, force: true });
  fs.rmSync(byteDir, { recursive: true, force: true });
  fs.rmSync(bridgeRoot, { recursive: true, force: true });
  console.log("activation-latency-and-trace-cost: ok");
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
