"use strict";

const assert = require("assert");
const fs = require("fs");
const path = require("path");

const root = path.resolve(__dirname, "..");
const extension = fs.readFileSync(path.join(root, "extension.js"), "utf8");
const app = fs.readFileSync(path.join(root, "media", "app.js"), "utf8");
const css = fs.readFileSync(path.join(root, "media", "app.css"), "utf8");

for (const marker of [
  'id="open-system-log"',
  'id="open-sessions"',
  'id="open-ai-memory"',
  'id="open-kb"',
  'id="sessions-dialog"',
  'id="kb-dialog"',
]) {
  assert.ok(extension.includes(marker), `missing context viewer marker: ${marker}`);
}

assert.ok(extension.includes('sessions: "aiworkhub_dashboard_sessions"'));
assert.ok(extension.includes('kb: "aiworkhub_dashboard_kb"'));
assert.ok(app.includes("function renderSessions(payload)"));
assert.ok(app.includes("function renderKb(payload)"));
assert.ok(app.includes('type: "requestSessions"'));
assert.ok(app.includes('type: "requestKb"'));
assert.ok(css.includes(".diagnostic-icon-button svg"));
