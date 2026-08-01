"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const extensionSource = fs.readFileSync(path.join(__dirname, "..", "extension.js"), "utf8");
const appSource = fs.readFileSync(path.join(__dirname, "..", "media", "app.js"), "utf8");
const cssSource = fs.readFileSync(path.join(__dirname, "..", "media", "app.css"), "utf8");

test("dashboard exposes KPI operations view as the default tab", () => {
  assert.match(extensionSource, /id="tab-kpis"[^>]+data-tab="kpis"/);
  assert.match(extensionSource, /id="panel-kpis"[^>]+aria-labelledby="tab-kpis"/);
  assert.match(extensionSource, /id="kpi-dashboard"/);
  assert.match(extensionSource, /aria-selected="false"[^>]+id="tab-topics"/);
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

test("KPI v3 renders Source Graph workflow, generations, call gaps and byte economics", () => {
  assert.match(appSource, /aiworkhub\.kpi\.dashboard\.v3/);
  assert.match(appSource, /Source Graph workflow stages/);
  assert.match(appSource, /Source Graph modes/);
  assert.match(appSource, /Tool-use cohorts/);
  assert.match(appSource, /Context compression/);
  assert.match(appSource, /Source Graph latency p50/);
  assert.match(appSource, /SG call gap p95/);
  assert.match(appSource, /SG evidence rows/);
  assert.match(appSource, /Source Graph index generations/);
  assert.match(appSource, /Byte compression uses declared raw paths/);
});
