"use strict";

const assert = require("assert");
const fs = require("fs");
const path = require("path");

const root = path.resolve(__dirname, "..");
const extension = fs.readFileSync(path.join(root, "extension.js"), "utf8");
const app = fs.readFileSync(path.join(root, "media", "app.js"), "utf8");
const css = fs.readFileSync(path.join(root, "media", "app.css"), "utf8");

for (const marker of ['id="tab-plan"', 'id="panel-plan"', 'id="plan-dag"']) {
  assert.ok(extension.includes(marker), `missing Plan DAG marker: ${marker}`);
}
for (const marker of [
  "function renderPlanDag(snapshot)",
  "ready_capacity",
  "critical_path",
  "cycle_nodes",
  "blocked:",
  "Current work",
  "All history",
  "state.planScope",
]) {
  assert.ok(app.includes(marker), `missing Plan DAG renderer contract: ${marker}`);
}
for (const marker of [".plan-dag-grid", ".plan-node.critical", ".plan-node.blocked", ".plan-toolbar", ".plan-search"]) {
  assert.ok(css.includes(marker), `missing Plan DAG style: ${marker}`);
}

console.log("visual Plan DAG dashboard contract: ok");
