"use strict";

const assert = require("assert");
const fs = require("fs");
const path = require("path");

const root = path.resolve(__dirname, "..");
const extension = fs.readFileSync(path.join(root, "extension.js"), "utf8");
const app = fs.readFileSync(path.join(root, "media", "app.js"), "utf8");
const css = fs.readFileSync(path.join(root, "media", "app.css"), "utf8");

for (const id of [
  "callback-observability", "callback-backlog", "callback-delivery", "callback-retries", "callback-degraded",
  "return-search", "return-topic", "return-runner", "return-previous", "return-next", "return-page",
  "detail-evidence-block", "detail-evidence",
]) {
  assert(extension.includes(`id="${id}"`), `missing dashboard element ${id}`);
  assert(app.includes(`#${id}`), `missing app binding ${id}`);
}

assert(app.includes("function renderCallbackObservability(snapshot)"));
assert(app.includes("health.current_delivery_error"));
assert(app.includes("historical dead letters; latest terminal delivery recovered"));
assert(app.includes("function renderReviewEvidence(evidence)"));
assert(app.includes("returnPageSize: 20"));
assert(app.includes("filtered.slice(start, start + state.returnPageSize)"));
assert(app.includes("review_evidence_bundle"));
assert(!app.includes("JSON.stringify(evidence, null, 2)"));
assert(css.includes(".callback-observability"));
assert(css.includes(".return-toolbar"));
assert(css.includes(".review-evidence-list"));

console.log("review inbox pagination and callback observability: PASS");
