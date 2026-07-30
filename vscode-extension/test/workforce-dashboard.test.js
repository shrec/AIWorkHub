"use strict";

const assert = require("assert");
const fs = require("fs");
const path = require("path");

const root = path.resolve(__dirname, "..");
const extension = fs.readFileSync(path.join(root, "extension.js"), "utf8");
const app = fs.readFileSync(path.join(root, "media/app.js"), "utf8");
const css = fs.readFileSync(path.join(root, "media/app.css"), "utf8");

for (const id of ["tab-workforce", "panel-workforce", "workforce-list"]) {
  assert(extension.includes(`id="${id}"`), `missing workforce dashboard element ${id}`);
}
assert(app.includes('workforceList: document.querySelector("#workforce-list")'));
assert(app.includes("function renderWorkforce(snapshot)"));
assert(app.includes("snapshot.workforce_catalog"));
assert(app.includes("Provider quota is never fabricated"));
assert(css.includes(".workforce-row"));
console.log("workforce dashboard contract: ok");
