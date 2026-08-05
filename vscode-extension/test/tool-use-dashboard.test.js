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
assert.ok(app.includes("function openToolUseTelemetry()"));
assert.ok(app.includes('elements.openToolUse.addEventListener("click", openToolUseTelemetry)'));
assert.ok(app.includes('elements.headerSourceGraph.addEventListener("click", openToolUseTelemetry)'));
assert.ok(app.includes("source_graph_telemetry"));
assert.ok(app.includes("source_graph_index_health"));
assert.ok(app.includes("Guidance resolves"));
assert.ok(app.includes("Resolved edges"));
assert.ok(app.includes("Cross-language"));
assert.ok(app.includes("Artifact entities"));
assert.ok(app.includes("generation-bound structural measurements"));
assert.ok(app.includes("source_graph_injected_only_tasks"));
assert.ok(app.includes("policy_violations"));
assert.ok(app.includes("source_graph_hit_count"));
assert.ok(app.includes("source_graph_zero_hit_calls"));
assert.ok(app.includes("source_graph_failed_calls"));
assert.ok(app.includes("measured return bytes"));
assert.ok(app.includes("raw_discovery_denials"));
assert.ok(app.includes("provider_denial_evidence_tasks"));
assert.ok(app.includes("blocked_reason_counts"));
assert.ok(app.includes("tool_call_counts"));
assert.ok(app.includes("source_graph_mode_counts"));
assert.ok(app.includes("source_graph_mode_attributed_calls"));
assert.ok(app.includes("source_graph_mode_unattributed_calls"));
assert.ok(app.includes("Recent mode path"));
assert.ok(app.includes("calls without mode metadata"));
assert.ok(app.includes("Authenticated MCP tools"));
assert.ok(app.includes("Source Graph modes"));
assert.ok(app.includes("not inferred token or cost savings"));
assert.ok(css.includes(".header-tool-use"));
