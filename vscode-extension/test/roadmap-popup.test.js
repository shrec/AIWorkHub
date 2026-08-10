"use strict";

const assert = require("assert");
const fs = require("fs");
const path = require("path");

const root = path.resolve(__dirname, "..");
const extension = fs.readFileSync(path.join(root, "extension.js"), "utf8");
const app = fs.readFileSync(path.join(root, "media", "app.js"), "utf8");
const css = fs.readFileSync(path.join(root, "media", "app.css"), "utf8");

for (const marker of [
  'id="header-roadmap"',
  'id="open-roadmap"',
  'id="roadmap-dialog"',
  'id="roadmap-list"',
  'id="roadmap-detail-panel"',
]) assert.ok(extension.includes(marker), `missing Roadmap UI marker: ${marker}`);

for (const tool of [
  "aiworkhub_dashboard_roadmap_list",
  "aiworkhub_dashboard_roadmap_detail",
]) assert.ok(extension.includes(tool), `missing bounded Roadmap bridge tool: ${tool}`);

assert.ok(extension.includes('"requestRoadmap"'));
assert.ok(extension.includes('"requestRoadmapDetail"'));
assert.ok(extension.includes("ROADMAP_ID_RE.test(roadmapId)"));
assert.ok(app.includes("snapshot.roadmap"), "header must use canonical Roadmap snapshot");
assert.ok(app.includes('type: "requestRoadmap"'));
assert.ok(app.includes('type: "requestRoadmapDetail"'));
assert.ok(app.includes("dependency_blockers"));
assert.ok(css.includes("#header-roadmap:hover"));
assert.ok(!app.includes("roadmap.sqlite"), "Webview must not access Roadmap storage directly");
assert.ok(!app.includes("roadmapTransition"), "read-only popup must not mutate Roadmap state");

console.log("Roadmap popup contract verified");
