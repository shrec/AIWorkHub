"use strict";

const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

class FakeElement {
  constructor(tagName) {
    this.tagName = tagName;
    this.className = "";
    this.children = [];
    this.dataset = {};
    this.style = {};
    this.hidden = false;
    this.open = false;
    this._textContent = "";
    this.value = "";
    this.classList = { add() {}, remove() {}, toggle() {}, contains() { return false; } };
  }
  append(...nodes) { for (const node of nodes) this.appendChild(node); }
  appendChild(node) {
    if (node && node.__fragment) {
      for (const child of node.children) this.children.push(child);
      return node;
    }
    this.children.push(node);
    return node;
  }
  replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
  addEventListener() {}
  setAttribute(name, value) { this[name] = String(value); }
  querySelector() { return new FakeElement("div"); }
  querySelectorAll() { return []; }
  focus() {}
  get textContent() {
    return `${this._textContent}${this.children.map((child) => (child && child.textContent) || "").join("")}`;
  }
  set textContent(value) { this._textContent = String(value); this.children = []; }
  get innerHTML() { return ""; }
  set innerHTML(_value) {}
}

class FakeDocument {
  constructor() { this.elements = new Map(); }
  createElement(tagName) { return new FakeElement(tagName); }
  createDocumentFragment() {
    const fragment = new FakeElement("#fragment");
    fragment.__fragment = true;
    return fragment;
  }
  querySelector(selector) {
    if (!this.elements.has(selector)) this.elements.set(selector, new FakeElement("div"));
    return this.elements.get(selector);
  }
  querySelectorAll() { return []; }
  addEventListener() {}
}

const document = new FakeDocument();
global.document = document;
global.window = { setTimeout: () => 0, clearTimeout: () => {}, addEventListener: () => {} };
global.acquireVsCodeApi = () => ({ getState: () => ({}), setState: () => {}, postMessage: () => {} });
for (const name of ["manager_console.js", "app.js"]) {
  const file = path.join(__dirname, "../media", name);
  vm.runInThisContext(fs.readFileSync(file, "utf8"), { filename: file });
}
const api = global.__AIWORKHUB_LIVE_OUTPUT_FORMATTING__;
assert(api, "formatter test hook is exposed");

const lines = (...events) => events.map((event) => JSON.stringify(event)).join("\n");

// OpenCode `run --format json` captured stream.
{
  const fixture = fs.readFileSync(
    path.resolve(__dirname, "../../tests/fixtures/manager_streams/opencode_cli.jsonl"), "utf8");
  const events = api.timelineEventsFromText(fixture);
  const titles = events.map((event) => event.title);
  assert(!titles.some((title) => /step start/i.test(title)), "step_start is a no-op");
  assert.strictEqual(titles.filter((title) => title === "Reasoning").length, 4);
  assert.strictEqual(titles.filter((title) => title === "Step finished").length, 3);

  const shell = events.find((event) => event.title === "Tool | shell");
  assert(shell, "shell tool row is named");
  assert.strictEqual(shell.state, "completed");
  assert.strictEqual(shell.message, "git --version");
  assert(shell.activityInput.includes("git --version"));
  assert(shell.activityOutput.includes("git version 2.45.2"));

  const edit = events.find((event) => event.title === "Tool | edit");
  assert(edit && edit.activityInput.includes("oldString") && edit.activityOutput.includes("1 replacement"));

  const assistant = events.filter((event) => event.title === "Assistant").map((event) => event.message);
  assert(assistant.some((message) => message.includes("git --version")));
  assert(assistant.includes("done"));

  const step = events.find((event) => event.title === "Step finished");
  assert.strictEqual(step.message, "reason tool-calls");
  assert.strictEqual(step.metrics.length, 5, "in/out/reasoning/cached/cost");
  assert(step.metrics[0].endsWith(" in") && step.metrics[1].endsWith(" out"));
  assert(step.metrics[2].endsWith(" reasoning") && step.metrics[3].endsWith(" cached"));
  assert(/0[.,]0164/.test(step.metrics[4]), "step cost is shown");

  assert(!JSON.stringify(events).includes("onWPNFYuk06"), "encrypted reasoning never reaches any row");
  assert(!events.some((event) => /structured event/i.test(event.message)));
}

// Kilo uses hyphenated event types and plain reasoning text.
{
  const events = api.timelineEventsFromText(lines(
    { type: "step-start", sessionID: "ses_k", part: { type: "step-start" } },
    { type: "reasoning", sessionID: "ses_k", part: { type: "reasoning", text: "Inspecting the failing route first." } },
    {
      type: "tool_use", sessionID: "ses_k",
      part: { type: "tool", tool: "read", state: { status: "running", input: { filePath: "src/app.py" } } },
    },
    {
      type: "tool_use", sessionID: "ses_k",
      part: { type: "tool", tool: "bash", state: { status: "error", input: { command: "pytest" }, error: "exit 1" } },
    },
    {
      type: "step-finish", sessionID: "ses_k",
      part: { type: "step-finish", reason: "stop", cost: 0.1372, tokens: { input: 139148, output: 842, reasoning: 210, cache: { read: 128000, write: 416 } } },
    },
  ));
  assert.strictEqual(events.length, 4);
  const reasoning = events.find((event) => event.title === "Reasoning");
  assert.strictEqual(reasoning.message, "Inspecting the failing route first.");
  assert(reasoning.activityOutput.includes("failing route"));

  const read = events.find((event) => event.title === "Tool | read");
  assert.strictEqual(read.state, "running");
  assert.strictEqual(read.message, "src/app.py");

  const bash = events.find((event) => event.title === "Tool | bash");
  assert.strictEqual(bash.kind, "error");
  assert.strictEqual(bash.state, "failed");
  assert(bash.activityOutput.includes("exit 1"));

  const step = events.find((event) => event.title === "Step finished");
  assert.strictEqual(step.message, "reason stop");
  assert(step.metrics.some((metric) => metric.includes("cached")));
}

// OpenCode part-update events resolve through the nested part type.
{
  const events = api.timelineEventsFromText(lines(
    { type: "message.part.updated", part: { type: "reasoning", text: "checking the route" } },
  ));
  assert.strictEqual(events.length, 1);
  assert.strictEqual(events[0].message, "checking the route");
}

// Unrelated provider shapes keep their existing rendering.
{
  const events = api.timelineEventsFromText(lines(
    { type: "stream_event", event: { type: "content_block_delta", delta: { type: "text_delta", text: "kept text" } } },
  ));
  assert.strictEqual(events.length, 1);
  assert.strictEqual(events[0].title, "Text");
}

{
  api.renderFormattedLiveOutput(lines(
    { type: "tool_use", part: { type: "tool", tool: "grep", state: { status: "completed", input: { pattern: "TODO" }, output: "3 matches" } } },
  ));
  const rendered = document.querySelector("#detail-live-output-container").textContent;
  assert(rendered.includes("Tool | grep"));
  assert(rendered.includes("Tool input"));
  assert(rendered.includes("3 matches"));
}

module.exports = { api, document };
console.log("opencode-kilo-stream-events.test.js: ok");
