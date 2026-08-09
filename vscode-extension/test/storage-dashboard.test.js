"use strict";

const assert = require("assert");
const fs = require("fs");
const path = require("path");

const root = path.resolve(__dirname, "..");
const extension = fs.readFileSync(path.join(root, "extension.js"), "utf8");
const app = fs.readFileSync(path.join(root, "media/app.js"), "utf8");
const css = fs.readFileSync(path.join(root, "media/app.css"), "utf8");

for (const token of [
  'class="header-insights"',
  'id="tab-storage"',
  'id="panel-storage"',
  'id="storage-list"',
  'id="header-storage"',
  'id="header-storage-managed"',
  'id="header-storage-free"',
  'id="header-session-manager"',
  'id="header-ai-memory"',
  'id="header-kb"',
  'id="header-preflight"',
  'id="storage-cleanup-preview"',
  'id="storage-registration-prune"',
  'id="terminal-log-cleanup-preview"',
  'id="runtime-cleanup-preview"',
  'id="reload-alert-title"',
]) {
  assert.ok(extension.includes(token), `missing dashboard storage UI token: ${token}`);
}
assert.ok(
  extension.indexOf('class="header-actions"') < extension.indexOf('class="header-insights"'),
  "storage and Source Graph cards must live in a dedicated row below the main header controls"
);
for (const token of [
  "function formatBytes(value)",
  "function renderStorage(snapshot)",
  "snapshot.storage_usage",
  "Safe reclaimable",
  "renderStorage(snapshot);",
  "elements.headerStorageManaged.textContent",
  "snapshot.project_context_telemetry",
  "elements.headerSessionManagerValue",
  "elements.headerAiMemoryValue",
  "elements.headerKbValue",
  "elements.headerPreflightValue",
  "snapshot.environment_preflight",
  'coverageStatus === "degraded"',
  '"Degraded"',
  "secure routes",
  "unavailable",
  "provider_summary",
  "Repository components",
  "Retention dry run",
  "This repository only",
  "requestStorageCleanup",
  "requestStorageRegistrationPrune",
  "Stale registrations",
  "requestStorageRestore",
  "requestStoragePurge",
  "Quarantine batches",
  "Terminal log retention",
  "requestTerminalLogCleanup",
  "requestTerminalLogRestore",
  "requestTerminalLogPurge",
  "Extension runtime cache",
  "requestRuntimeCleanup",
  "requestRuntimeRestore",
  "requestRuntimePurge",
  'elements.reloadAlertTitle.textContent = payload.repairAttempted',
  '"Runtime repair failed"',
]) {
  assert.ok(app.includes(token), `missing storage renderer token: ${token}`);
}
assert.ok(
  extension.includes('await pushRuntimeInfo(view);') && extension.includes('await pushSnapshot(view);'),
  "dashboard Retry must advance runtime repair and refresh the snapshot",
);
assert.ok(css.includes(".storage-row"), "storage dashboard styling is missing");
assert.ok(css.includes(".storage-section-title"), "storage section styling is missing");
assert.ok(css.includes(".storage-batch-row"), "storage quarantine styling is missing");
assert.ok(css.includes("grid-template-columns: repeat(6"), "AI infrastructure cards must share one full-width row");
assert.ok(css.includes("align-items: center"), "AI infrastructure cards must be centered");
console.log("storage dashboard contract: ok");
