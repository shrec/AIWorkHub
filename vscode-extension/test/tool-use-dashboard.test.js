"use strict";

const assert = require("assert");
const fs = require("fs");
const path = require("path");

const root = path.resolve(__dirname, "..");
const extension = fs.readFileSync(path.join(root, "extension.js"), "utf8");
const app = fs.readFileSync(path.join(root, "media", "app.js"), "utf8");
const css = fs.readFileSync(path.join(root, "media", "app.css"), "utf8");

for (const marker of [
  'id="header-source-graph"',
  'id="tab-tool-use"',
  'id="panel-tool-use"',
  'id="tool-use-list"',
]) {
  assert.ok(extension.includes(marker), `missing dashboard marker: ${marker}`);
}

assert.ok(app.includes("function renderToolUse(snapshot)"));
assert.ok(app.includes("source_graph_telemetry"));
assert.ok(app.includes("source_graph_injected_only_tasks"));
assert.ok(app.includes("policy_violations"));
assert.ok(css.includes(".header-tool-use"));
