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
  'id="open-operations"',
  'id="open-tool-use"',
  'id="open-settings"',
  'id="sessions-dialog"',
  'id="kb-dialog"',
  'id="settings-dialog"',
  'id="header-context-graph"',
  'id="operations-dialog"',
]) {
  assert.ok(extension.includes(marker), `missing context viewer marker: ${marker}`);
}

assert.ok(extension.includes('sessions: "aiworkhub_dashboard_sessions"'));
assert.ok(extension.includes('kb: "aiworkhub_dashboard_kb"'));
assert.ok(extension.includes('settings: "aiworkhub_dashboard_settings"'));
assert.ok(extension.includes('SETTINGS_UPDATE_TOOL = "aiworkhub_dashboard_settings_update"'));
assert.ok(extension.includes('MODEL_SETTINGS_UPDATE_TOOL = "aiworkhub_dashboard_model_settings_update"'));
assert.ok(extension.includes('SOURCE_GRAPH_SETTINGS_UPDATE_TOOL = "aiworkhub_dashboard_source_graph_settings_update"'));
assert.ok(app.includes("function renderSessions(payload)"));
assert.ok(app.includes("function renderKb(payload)"));
assert.ok(app.includes('type: "requestSessions"'));
assert.ok(app.includes('type: "requestKb"'));
assert.ok(app.includes('type: "requestSettings"'));
assert.ok(app.includes('type: "updateFeatureSetting"'));
assert.ok(app.includes('type: "updateModelSetting"'));
assert.ok(app.includes("function renderSettings(payload)"));
assert.ok(app.includes('["context_graph", elements.headerContextGraph'));
assert.ok(app.includes('type: "updateSourceGraphLanguage"'));
assert.ok(app.includes("data-source-graph-language"));
assert.ok(app.includes('state.settingsTab'));
assert.ok(app.includes('dataset.settingsTab'));
assert.ok(app.includes('"retention", "Retention"'));
assert.ok(app.includes('["models", "Models"]'));
assert.ok(app.includes("live VS Code/Copilot model"));
assert.ok(app.includes("discovered in VS Code · no task capability assigned"));
assert.ok(app.includes('"telemetry", "Telemetry"'));
assert.ok(css.includes(".diagnostic-icon-button svg"));
assert.ok(css.includes(".settings-row"));
assert.ok(css.includes(".settings-tabs"));
assert.ok(css.includes(".settings-metric-grid"));
assert.ok(css.includes(".settings-model-provider"));
