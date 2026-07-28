"use strict";

const assert = require("assert");
const fs = require("fs");
const path = require("path");

const root = path.resolve(__dirname, "..");
const extension = fs.readFileSync(path.join(root, "extension.js"), "utf8");
const app = fs.readFileSync(path.join(root, "media/app.js"), "utf8");
const css = fs.readFileSync(path.join(root, "media/app.css"), "utf8");

for (const token of [
  'id="tab-storage"',
  'id="panel-storage"',
  'id="storage-list"',
  'id="header-storage"',
  'id="header-storage-managed"',
  'id="header-storage-free"',
]) {
  assert.ok(extension.includes(token), `missing dashboard storage UI token: ${token}`);
}
for (const token of [
  "function formatBytes(value)",
  "function renderStorage(snapshot)",
  "snapshot.storage_usage",
  "Safe reclaimable",
  "renderStorage(snapshot);",
  "elements.headerStorageManaged.textContent",
]) {
  assert.ok(app.includes(token), `missing storage renderer token: ${token}`);
}
assert.ok(css.includes(".storage-row"), "storage dashboard styling is missing");
console.log("storage dashboard contract: ok");
