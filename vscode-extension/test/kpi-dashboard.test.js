"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const extensionSource = fs.readFileSync(path.join(__dirname, "..", "extension.js"), "utf8");
const appSource = fs.readFileSync(path.join(__dirname, "..", "media", "app.js"), "utf8");
const cssSource = fs.readFileSync(path.join(__dirname, "..", "media", "app.css"), "utf8");

test("dashboard exposes the complete operations surface in a dedicated popup", () => {
  assert.match(extensionSource, /id="open-operations"[^>]+title="Open repository operations"/);
  assert.match(extensionSource, /class="diagnostic-dialog operations-dialog" id="operations-dialog"/);
  assert.match(extensionSource, /id="kpi-dashboard"/);
  assert.match(extensionSource, /id="tab-kpis"[^>]+data-tab="kpis"/);
  assert.match(extensionSource, /id="panel-kpis"[^>]+aria-labelledby="tab-kpis"/);
  assert.match(extensionSource, /aria-selected="false"[^>]+id="tab-topics"/);
  assert.match(appSource, /elements\.operationsDialog\.showModal\(\)/);
  assert.match(cssSource, /\.diagnostic-dialog\.operations-dialog/);
  assert.match(cssSource, /\.operations-dialog \.tab-panel:not\(\[hidden\]\)/);
});

test("KPI renderer separates worker outcomes from explicit manager decisions", () => {
  assert.match(appSource, /function renderKpis\(snapshot\)/);
  assert.match(appSource, /worker outcomes and explicit manager decisions are separate/i);
  assert.match(appSource, /no token-savings or causal quality claim is inferred/i);
  assert.match(appSource, /renderKpis\(snapshot\)/);
});

test("KPI visualizations include responsive chart and bar primitives", () => {
  assert.match(cssSource, /\.kpi-chart-grid/);
  assert.match(cssSource, /\.kpi-daily-chart/);
  assert.match(cssSource, /\.kpi-bar-track/);
  assert.match(cssSource, /@media \(max-width: 820px\)/);
});

test("KPI v4 renders Source Graph workflow, generations, call gaps and byte economics", () => {
  assert.match(appSource, /aiworkhub\.kpi\.dashboard\.v4/);
  assert.match(appSource, /Source Graph workflow stages/);
  assert.match(appSource, /Source Graph modes/);
  assert.match(appSource, /Tool-use cohorts/);
  assert.match(appSource, /Delivery reduction/);
  assert.match(appSource, /Delivery overhead/);
  assert.match(appSource, /estimated bytes added/);
  assert.match(appSource, /Optional suppression/);
  assert.match(appSource, /Envelope overhead/);
  assert.match(appSource, /serialization bytes added/);
  assert.match(appSource, /Provider cache hit/);
  assert.match(appSource, /Cost \/ review-ready/);
  assert.match(appSource, /Source Graph latency p50/);
  assert.match(appSource, /SG call gap p95/);
  assert.match(appSource, /SG long gaps/);
  assert.match(appSource, /not proof that the model was inactive/);
  assert.match(appSource, /SG evidence rows/);
  assert.match(appSource, /Source Graph index generations/);
  assert.match(appSource, /signed net delta between pre-optimization tool-section payload and delivered bundle bytes/);
  assert.match(appSource, /not raw repository-file, counterfactual read, or token-savings evidence/);
});

test("Operations KPIs render semantic-edit structural evidence without token claims", () => {
  assert.match(appSource, /Focused semantic edits/);
  assert.match(appSource, /Replacement \/ file bytes/);
  assert.match(appSource, /Old bytes re-emitted by model/);
  assert.match(appSource, /byte-shape evidence, not a token, cost, speed, or quality-savings claim/i);
  assert.match(appSource, /Paired baselines are required/);
});

test("Operations KPIs render truthful worker read-efficiency evidence", () => {
  assert.match(appSource, /snapshot\.read_efficiency_telemetry/);
  assert.match(appSource, /Read trace coverage/);
  assert.match(appSource, /Bounded file reads/);
  assert.match(appSource, /Worker read efficiency/);
  assert.match(appSource, /Read evidence by adapter/);
  assert.match(appSource, /legacy excluded/);
  assert.match(appSource, /incompatible legacy task\(s\) excluded/);
  assert.match(appSource, /Provider event\/byte evidence only; no token or savings claim/);
});
