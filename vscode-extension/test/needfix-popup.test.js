"use strict";

const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const root = path.resolve(__dirname, "..");
const extension = fs.readFileSync(path.join(root, "extension.js"), "utf8");
const app = fs.readFileSync(path.join(root, "media", "app.js"), "utf8");
const css = fs.readFileSync(path.join(root, "media", "app.css"), "utf8");

for (const marker of [
  'id="header-needfix"',
  'id="open-needfix"',
  'id="needfix-dialog"',
  'id="needfix-capture-form"',
  'id="needfix-list"',
  'id="needfix-detail"',
  'id="needfix-sort"',
]) {
  assert.ok(extension.includes(marker), `missing NeedFix UI marker: ${marker}`);
}

for (const tool of [
  "aiworkhub_dashboard_needfix_list",
  "aiworkhub_dashboard_needfix_detail",
  "aiworkhub_dashboard_needfix_capture",
  "aiworkhub_dashboard_needfix_update",
  "aiworkhub_dashboard_needfix_transition",
  "aiworkhub_dashboard_needfix_archive",
  "aiworkhub_dashboard_needfix_restore",
  "aiworkhub_dashboard_needfix_purge",
  "aiworkhub_dashboard_needfix_convert_preview",
  "aiworkhub_dashboard_needfix_convert_commit",
]) {
  assert.ok(extension.includes(tool), `missing bounded NeedFix bridge tool: ${tool}`);
}

assert.ok(app.includes("snapshot.needfix"), "header must use canonical NeedFix snapshot");
assert.ok(app.includes("function renderNeedfix(payload)"));
assert.ok(app.includes("function renderNeedfixDetail(payload)"));
assert.ok(app.includes("function needfixDifficulty(entry)"));
assert.ok(app.includes("function compareNeedfixEntries(left, right, sortMode)"));
assert.ok(app.includes("difficulty.label"));
assert.ok(app.includes("Severity remains impact, not effort."));
assert.ok(app.includes("function appendJsonNode(parent, key, rawValue, depth = 0)"));
assert.ok(app.includes("function appendNeedfixEvents(parent, events)"));
assert.ok(app.includes("createElement(\"div\", \"json-visualizer\")"));
assert.ok(app.includes('type: "requestNeedfix"'));
assert.ok(app.includes('type: "needfixCapture"'));
assert.ok(app.includes('type: "needfixConvertPreview"'));
assert.ok(app.includes('type: "needfixConvertCommit"'));
assert.ok(app.includes('"resolve_verified"'), "NeedFix popup must expose evidenced direct resolution");
assert.ok(app.includes("planDigest: state.needfixConversionPreview.plan_digest"), "NeedFix commit must reuse the preview digest");
assert.ok(app.includes("taskPlan: state.needfixConversionPreview.task_plan"), "NeedFix commit must reuse the previewed workforce plan");
assert.ok(app.includes("window.confirm"), "destructive writes require explicit confirmation");
assert.ok(app.includes("window.prompt"), "purge/reject reasons must be explicit");
assert.ok(!app.includes("needfix.sqlite"), "Webview must not access NeedFix storage directly");
assert.ok(!app.includes("agent_launch_task"), "NeedFix popup must never launch a worker");
assert.ok(css.includes(".needfix-dialog"));
assert.ok(css.includes(".needfix-workspace"));
assert.ok(css.includes(".json-visualizer"));
assert.ok(css.includes(".json-field-row"));
assert.ok(css.includes(".needfix-event-timeline"));
assert.ok(css.includes("var(--vscode-button-secondaryForeground, #ffffff)"));
assert.ok(css.includes(".needfix-controls .danger-button"));
assert.ok(css.includes("repeat(4, minmax(115px, auto))"), "NeedFix toolbar must allocate all four selects");

const operationsStart = extension.indexOf('id="operations-dialog"');
const operationsEnd = extension.indexOf("</dialog>", operationsStart);
const needfixDialog = extension.indexOf('id="needfix-dialog"');
assert.ok(needfixDialog > operationsEnd, "NeedFix must be a dedicated top-level dialog, not an Operations tab");

const difficultyStart = app.indexOf("const NEEDFIX_CLOSED_STATUSES");
const difficultyEnd = app.indexOf("function renderNeedfixList()", difficultyStart);
assert.ok(difficultyStart >= 0 && difficultyEnd > difficultyStart, "difficulty helpers must be extractable");
const sandbox = {
  asArray(value) { return Array.isArray(value) ? value : []; },
  numberValue(value) { const parsed = Number(value); return Number.isFinite(parsed) ? parsed : 0; },
};
vm.runInNewContext(
  `${app.slice(difficultyStart, difficultyEnd)}\n` +
  "globalThis.__difficulty = { needfixDifficulty, compareNeedfixEntries };",
  sandbox,
);
const { needfixDifficulty, compareNeedfixEntries } = sandbox.__difficulty;
const easy = { id: "NF-EASY", kind: "docs", readiness_score: 100, evidence_refs: ["test:pass"], status: "captured" };
const hard = { id: "NF-HARD", kind: "refactor", readiness_score: 20, scope_files: ["a", "b", "c"], status: "captured" };
assert.ok(needfixDifficulty(easy).score < needfixDifficulty(hard).score, "easy work must sort before hard work");
assert.ok(compareNeedfixEntries(easy, hard, "difficulty") < 0, "difficulty sort must be ascending");
assert.strictEqual(
  needfixDifficulty({ ...hard, tags: ["difficulty:easy"] }).label,
  "easy",
  "manager difficulty tags must override the estimate",
);
