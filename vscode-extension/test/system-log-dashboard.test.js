"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");

const fakeVscode = {
  workspace: { workspaceFolders: [], getConfiguration: () => ({ get: (_key, fallback) => fallback }) },
  window: {},
  Uri: {},
  EventEmitter: class {},
  CancellationTokenSource: class {},
  ProgressLocation: { Notification: 15 },
};
const originalLoad = Module._load;
Module._load = function patchedLoad(request, parent, isMain) {
  if (request === "vscode") return fakeVscode;
  return originalLoad.call(this, request, parent, isMain);
};
const extensionPath = path.resolve(__dirname, "..", "extension.js");
delete require.cache[extensionPath];
const { __testInternals } = require(extensionPath);
Module._load = originalLoad;

__testInternals.clearSystemLogs();
__testInternals.recordSystemLog("[runtime] ready");
__testInternals.recordSystemLog("[mcp stderr] warning: opaque_abcdefghijklmnopqrstuvwxyz123456");
__testInternals.recordSystemLog("[mcp stderr] Processing request of type CallToolRequest");
let entries = __testInternals.systemLogSnapshot();
assert.equal(entries.length, 3);
assert.equal(entries[0].component, "mcp stderr");
assert.equal(entries[0].level, "info");
assert.equal(entries[1].level, "warning");
assert.ok(entries[1].message.includes("[REDACTED]"));
assert.equal(entries[2].component, "runtime");

for (let index = 0; index < 240; index += 1) {
  __testInternals.recordSystemLog(`[mcp] event ${index}`);
}
entries = __testInternals.systemLogSnapshot();
assert.equal(entries.length, 243);
assert.equal(entries[0].message, "event 239");

const app = fs.readFileSync(path.resolve(__dirname, "..", "media", "app.js"), "utf8");
const css = fs.readFileSync(path.resolve(__dirname, "..", "media", "app.css"), "utf8");
const extension = fs.readFileSync(extensionPath, "utf8");
assert.ok(app.includes("function renderSystemLogs"));
assert.ok(app.includes('type: "copySystemLogs"'));
assert.ok(app.includes('type: "clearSystemLogs"'));
assert.ok(css.includes(".system-log-terminal"));
assert.ok(extension.includes('id="system-log-dialog"'));
assert.ok(extension.includes('id="last-system-log"'));
assert.ok(extension.includes('id="ai-memory-dialog"'));

console.log("system log dashboard contract: ok");
