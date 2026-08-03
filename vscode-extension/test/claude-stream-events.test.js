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
    for (const node of nodes) this.appendChild(node);
  }

  appendChild(node) {
    if (node && node.__fragment) {
      for (const child of node.children) this.children.push(child);
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
  setAttribute(name, value) { this[name] = String(value); }
  querySelector() { return new FakeElement("div"); }
  querySelectorAll() { return []; }
  focus() {}

  get textContent() {
    const childText = this.children
      .map((child) => child && child.textContent ? child.textContent : "")
      .join("");
    return `${this._textContent}${childText}`;
  }

  set textContent(value) {
    this._textContent = String(value);
    this.children = [];
  }

  get innerHTML() { return this._innerHTML; }

  set innerHTML(value) {
    this._innerHTML = String(value);
    this.value = this._innerHTML
      .replace(/&lt;/g, "<")
      .replace(/&gt;/g, ">")
      .replace(/&amp;/g, "&");
  }
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
  return { api: global.__AIWORKHUB_LIVE_OUTPUT_FORMATTING__, document };
}

const { api, document } = loadFormatter();
assert(api, "formatter test hook is exposed");

function lines(...events) {
  return events.map((event) => JSON.stringify(event)).join("\n");
}

{
  const events = api.timelineEventsFromText(lines(
    { type: "stream_event", event: { type: "content_block_stop", index: 0 } },
    { type: "stream_event", event: { type: "message_stop" } },
  ));
  assert.strictEqual(events.length, 0, "Claude stop protocol events are supported no-ops");
}

{
  const events = api.timelineEventsFromText(lines({
    type: "stream_event",
    event: {
      type: "message_delta",
      delta: { stop_reason: "end_turn", stop_sequence: null },
      usage: { output_tokens: 17 },
    },
    session_id: "session-1",
    parent_tool_use_id: null,
    uuid: "uuid-1",
  }));
  assert.strictEqual(events.length, 1);
  assert.strictEqual(events[0].title, "Message delta");
  assert.strictEqual(events[0].state, "completed");
  assert.strictEqual(events[0].message, "stop end_turn | 17 output tokens");
  assert(!events[0].message.includes("Structured event"));
  assert(!events[0].title.includes("Unrecognized"));
}

{
  const events = api.timelineEventsFromText(lines(
    { type: "stream_event", event: { type: "content_block_delta", delta: { type: "text_delta", text: "kept text" } } },
    { type: "stream_event", event: { type: "content_block_delta", delta: { type: "signature_delta", signature: "SECRET_SIG_PAYLOAD_ZZZZZZZZZZZZZZZZ" } } },
    { type: "stream_event", event: { type: "message_delta", delta: {}, usage: {} } },
    { type: "stream_event", event: { type: "content_block_stop", index: 0 } },
    { type: "stream_event", event: { type: "message_stop" } },
    { type: "stream_event", event: { type: "unknown_inner", payload: { nested: true } } },
  ));
  assert.strictEqual(events.length, 3);
  assert(events.some((event) => event.message === "kept text"));
  assert(events.some((event) => event.title === "Message delta" && event.message === "Message metadata updated"));
  const fallback = events.find((event) => event.message.includes("Structured event"));
  assert(fallback, "unknown inner stream event still uses bounded raw fallback");
  assert(fallback.raw.length <= 2000);
  assert(!JSON.stringify(events).includes("SECRET_SIG_PAYLOAD"));
}

{
  api.renderFormattedLiveOutput(lines(
    { type: "stream_event", event: { type: "content_block_stop", index: 0 } },
    { type: "stream_event", event: { type: "message_stop" } },
    { type: "stream_event", event: { type: "message_delta", delta: { stop_reason: "max_tokens" }, usage: { output_tokens: 3 } } },
  ));
  const rendered = document.querySelector("#detail-live-output-container").textContent;
  assert(rendered.includes("Message delta"));
  assert(rendered.includes("stop max_tokens | 3 output tokens"));
  assert(!rendered.includes("Unrecognized event"));
}

console.log("claude-stream-events.test.js: ok");
