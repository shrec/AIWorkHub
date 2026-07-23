"use strict";

const assert = require("assert");
const path = require("path");

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
    this._innerHTML = "";
    this.value = "";
  }

  append(...nodes) {
    for (const node of nodes) {
      this.appendChild(node);
    }
  }

  appendChild(node) {
    if (node && node.__fragment) {
      for (const child of node.children) {
        this.children.push(child);
      }
      return node;
    }
    this.children.push(node);
    return node;
  }

  replaceChildren(...nodes) {
    this.children = [];
    this.append(...nodes);
  }

  addEventListener() {}

  setAttribute(name, value) {
    this[name] = String(value);
  }

  querySelector() {
    return new FakeElement("div");
  }

  querySelectorAll() {
    return [];
  }

  focus() {}

  get textContent() {
    const childText = this.children.map((child) => child && child.textContent ? child.textContent : "").join("");
    return `${this._textContent}${childText}`;
  }

  set textContent(value) {
    this._textContent = String(value);
    this.children = [];
  }

  get innerHTML() {
    return this._innerHTML;
  }

  set innerHTML(value) {
    this._innerHTML = String(value);
    this.value = this._innerHTML
      .replace(/&lt;/g, "<")
      .replace(/&gt;/g, ">")
      .replace(/&amp;/g, "&");
  }
}

class FakeDocument {
  constructor() {
    this.elements = new Map();
  }

  createElement(tagName) {
    return new FakeElement(tagName);
  }

  createDocumentFragment() {
    const fragment = new FakeElement("#fragment");
    fragment.__fragment = true;
    return fragment;
  }

  querySelector(selector) {
    if (!this.elements.has(selector)) {
      this.elements.set(selector, new FakeElement("div"));
    }
    return this.elements.get(selector);
  }

  querySelectorAll() {
    return [];
  }

  addEventListener() {}
}

function loadFormatter() {
  const document = new FakeDocument();
  global.document = document;
  global.window = {
    setTimeout: () => 0,
    clearTimeout: () => {},
    addEventListener: () => {},
  };
  global.acquireVsCodeApi = () => ({
    getState: () => ({}),
    setState: () => {},
    postMessage: () => {},
  });
  global.Intl = Intl;
  require(path.join(__dirname, "../media/app.js"));
  return {
    api: global.__AIWORKHUB_LIVE_OUTPUT_FORMATTING__,
    document,
  };
}

const { api, document } = loadFormatter();
assert(api, "formatter test hook is exposed");

function lines(...events) {
  return events.map((event) => typeof event === "string" ? event : JSON.stringify(event)).join("\n");
}

{
  const events = api.timelineEventsFromText(lines(
    { type: "item.started", item: { type: "command_execution", command: "python3 /tmp/worktree/secret/run.py --key sk_test_abcdefghijklmnopqrstuvwxyz" } },
    { type: "item.completed", item: { type: "command_execution", command: "python3 /tmp/worktree/secret/run.py" }, duration_ms: 1530 },
    { type: "result", result: "verdict: pass", duration_ms: 2050, num_turns: 3, usage: { input_tokens: 1000, output_tokens: 250 }, total_cost_usd: 0.0123 },
  ));
  assert.strictEqual(events.length, 3);
  assert.strictEqual(events[0].state, "running");
  assert.strictEqual(events[1].state, "completed");
  assert.strictEqual(events[2].kind, "result");
  assert(events[2].metrics.join(" ").includes("3 turns"));
  assert(!events[0].label.includes("/tmp/worktree"));
  assert(!events[0].label.includes("sk_test"));
}

{
  const events = api.timelineEventsFromText(lines(
    { type: "assistant", message: { content: "starting" } },
    { type: "tool_use", name: "Bash", input: { command: "node test.js" } },
    { type: "tool_result", result: { output: "ok" }, duration_ms: 42 },
    { type: "warning", warning: "stderr: retrying" },
  ));
  assert.strictEqual(events.length, 4);
  assert.strictEqual(events[1].label, "Bash");
  assert.strictEqual(events[2].message, "ok");
  assert.strictEqual(events[3].kind, "warning");
}

{
  const events = api.timelineEventsFromText(lines(
    "{not valid json",
    "plain <img src=x onerror=alert(1)> line",
    { type: "error", error: "failed <script>alert(1)</script>" },
  ));
  assert.strictEqual(events.length, 3);
  assert.strictEqual(events[0].kind, "text");
  assert(events[1].message.includes("<img"));
  assert.strictEqual(events[2].kind, "error");
}

{
  const repeated = JSON.stringify({ type: "item.started", item: { type: "command", command: "echo hi" } });
  const events = api.timelineEventsFromText(`${repeated}\n${repeated}\n`);
  assert.strictEqual(events.length, 1);
}

{
  api.renderFormattedLiveOutput(lines(
    { type: "item.started", item: { type: "command_execution", command: "echo hi" } },
    { type: "result", result: "done", duration_ms: 10, usage: { input_tokens: 1, output_tokens: 2 } },
  ));
  const rendered = document.querySelector("#detail-live-output-container").textContent;
  assert(rendered.includes("item started"));
  assert(rendered.includes("Raw event"));
  assert(rendered.includes("done"));
  assert(rendered.indexOf("done") < rendered.indexOf("echo hi"), "newest event renders first");
}

{
  global.__AIWORKHUB_LIVE_OUTPUT_FORMATTING__.appendLiveOutputText(lines(
    { type: "item.started", item: { type: "command_execution", command: "echo first" } },
  ) + "\n");
  global.__AIWORKHUB_LIVE_OUTPUT_FORMATTING__.appendLiveOutputText(lines(
    { type: "item.completed", item: { type: "command_execution", command: "echo first" }, duration_ms: 100 },
  ) + "\n");
  const rendered = document.querySelector("#detail-live-output-container").textContent;
  assert(rendered.includes("echo first"));
  assert(rendered.includes("completed"));
}

{
  api.startLiveOutputPolling("task-1");
  api.renderLiveOutput({
    ok: true,
    task_id: "task-1",
    output: lines({ type: "warning", warning: "stderr line" }) + "\n",
    stderr_tail: "stderr <b>tail</b>",
    next_cursor: 12,
    liveness_state: "running",
  });
  const stderr = document.querySelector("#detail-live-output-stderr");
  assert.strictEqual(stderr.hidden, false);
  assert(stderr.textContent.includes("stderr <b>tail</b>"));
}

{
  // B896: a Claude CLI stream_event/content_block_delta signature_delta is
  // internal protocol metadata -- it must never appear as a timeline row
  // (not even an "Unrecognized event" placeholder), and its signature
  // payload must never reach the formatted output.
  const events = api.timelineEventsFromText(lines(
    { type: "stream_event", event: { type: "content_block_delta", index: 0, delta: { type: "signature_delta", signature: "SUPER_SECRET_SIGNATURE_PAYLOAD_ABCDEF0123456789" } } },
  ));
  assert.strictEqual(events.length, 0, "signature_delta produces no timeline row");
}

{
  // Adjacent text_delta must still render as readable text.
  const events = api.timelineEventsFromText(lines(
    { type: "stream_event", event: { type: "content_block_delta", index: 0, delta: { type: "text_delta", text: "Hello from the model" } } },
  ));
  assert.strictEqual(events.length, 1);
  assert.strictEqual(events[0].kind, "event");
  assert.strictEqual(events[0].message, "Hello from the model");
  assert.notStrictEqual(events[0].title, "Unrecognized event");
}

{
  // signature_delta interleaved with text_delta, result, and error events:
  // only the signature_delta row is dropped; ordering/newest-first and the
  // other rows' formatting are unaffected.
  const events = api.timelineEventsFromText(lines(
    { type: "stream_event", event: { type: "content_block_delta", index: 0, delta: { type: "text_delta", text: "thinking out loud" } } },
    { type: "stream_event", event: { type: "content_block_delta", index: 0, delta: { type: "signature_delta", signature: "SECRET_SIG_PAYLOAD_ZZZZZZZZZZZZZZZZZZZZZZZZ" } } },
    { type: "error", error: "boom" },
    { type: "result", result: "verdict: pass", duration_ms: 5 },
  ));
  assert.strictEqual(events.length, 3, "only the signature_delta row is omitted");
  assert(events.some((event) => event.message === "thinking out loud"));
  assert(events.some((event) => event.kind === "error" && event.message === "boom"));
  assert(events.some((event) => event.kind === "result"));
  assert(!events.some((event) => event.title === "Unrecognized event"));
  for (const event of events) {
    assert(!JSON.stringify(event).includes("SECRET_SIG_PAYLOAD"), "signature payload never leaks into another row");
  }

  api.renderFormattedLiveOutput(lines(
    { type: "stream_event", event: { type: "content_block_delta", index: 0, delta: { type: "text_delta", text: "thinking out loud" } } },
    { type: "stream_event", event: { type: "content_block_delta", index: 0, delta: { type: "signature_delta", signature: "SECRET_SIG_PAYLOAD_ZZZZZZZZZZZZZZZZZZZZZZZZ" } } },
    { type: "result", result: "verdict: pass", duration_ms: 5 },
  ));
  const rendered = document.querySelector("#detail-live-output-container").textContent;
  assert(rendered.includes("thinking out loud"));
  assert(rendered.includes("verdict: pass"));
  assert(!rendered.includes("SECRET_SIG_PAYLOAD"), "signature payload absent from the formatted feed");
  assert(!rendered.includes("Unrecognized event"));
  // Newest-first: the later "result" row still renders above the earlier text row.
  assert(rendered.indexOf("verdict: pass") < rendered.indexOf("thinking out loud"), "newest event renders first");
}

console.log("live-output-formatting.test.js: ok");
