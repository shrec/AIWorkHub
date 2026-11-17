"use strict";

// NF-2026-02317: the first call to a freshly spawned dashboard reader must get a
// cold-start budget; after the child answers once, the caller's timeout applies.
// Source-inspection test in the style of dashboard-snapshot-timeout-classification.test.js.

const assert = require("assert");
const fs = require("fs");
const path = require("path");

const extensionPath = path.resolve(__dirname, "..", "extension.js");
const source = fs.readFileSync(extensionPath, "utf8");

function classBody(name) {
  const start = source.indexOf(`class ${name} `);
  assert.ok(start >= 0, `class ${name} must exist in extension.js`);
  const end = source.indexOf("\n}\n", start);
  assert.ok(end > start, `class ${name} must terminate`);
  return source.slice(start, end);
}

function methodBody(classSource, name) {
  const start = classSource.indexOf(`\n  ${name}(`);
  const asyncStart = classSource.indexOf(`\n  async ${name}(`);
  const begin = start >= 0 ? start : asyncStart;
  assert.ok(begin >= 0, `${name} must exist on the class`);
  const end = classSource.indexOf("\n  }\n", begin);
  assert.ok(end > begin, `${name} must terminate`);
  return classSource.slice(begin, end + "\n  }\n".length);
}

// The cold-start constant exists next to the other DASHBOARD_PROCESS_* constants.
const constantMatch = source.match(/^const DASHBOARD_PROCESS_COLD_START_TIMEOUT_MS = 90000;$/m);
assert.ok(constantMatch, "DASHBOARD_PROCESS_COLD_START_TIMEOUT_MS = 90000 must be declared");
assert.ok(
  source.indexOf("const DASHBOARD_PROCESS_MAX_PENDING_REQUESTS") < constantMatch.index
    && constantMatch.index < source.indexOf("const DASHBOARD_REQUEST_TIMEOUT_REASON"),
  "the cold-start constant must sit with the DASHBOARD_PROCESS_* constants",
);

const client = classBody("DashboardProcessClient");

// Spawn resets the answered flag, so every new child starts on the cold budget.
const spawn = methodBody(client, "_spawn");
assert.ok(
  spawn.includes("this.childAnswered = false;"),
  "_spawn must reset childAnswered on every new child",
);

// The response handler marks the child answered only once it resolves a pending record.
const onStdout = methodBody(client, "_onStdout");
const recordGuard = onStdout.indexOf("if (!record) continue;");
const answeredMark = onStdout.indexOf("this.childAnswered = true;");
assert.ok(recordGuard >= 0, "_onStdout must look up the pending record");
assert.ok(
  answeredMark > recordGuard,
  "_onStdout must set childAnswered = true only after a pending record is found",
);

// callTool: before the first answer the timer uses max(caller timeout, cold budget);
// after the first answer it uses the caller's timeoutMs unchanged.
const callTool = methodBody(client, "callTool");
assert.ok(
  callTool.includes(
    "const coldBudgetMs = this.childAnswered ? timeoutMs : Math.max(timeoutMs, DASHBOARD_PROCESS_COLD_START_TIMEOUT_MS);",
  ),
  "callTool must select the cold budget until the child has answered",
);
assert.ok(
  callTool.includes("fail(new Error(DASHBOARD_REQUEST_TIMEOUT_REASON)), coldBudgetMs);"),
  "callTool's request timer must use the selected budget",
);
assert.ok(
  !/setTimeout\([^)]*\), timeoutMs\);/.test(callTool),
  "callTool must not arm its timer with the raw caller timeout",
);

// The MCP default and the hidden-stop interval stay as they were.
assert.ok(source.includes("const MCP_REQUEST_TIMEOUT_MS = 20000;"), "MCP_REQUEST_TIMEOUT_MS must stay 20000");
assert.ok(
  source.includes("const DASHBOARD_PROCESS_HIDDEN_STOP_MS = 60 * 1000;"),
  "DASHBOARD_PROCESS_HIDDEN_STOP_MS must stay 60 s",
);

process.stdout.write("dashboard-reader-cold-start-budget: ok\n");
