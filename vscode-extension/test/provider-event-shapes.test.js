"use strict";

// NF-2026-00183: the general JSON/model-response visualizer must degrade an
// unrecognised provider event shape to a readable raw rendering with a named
// reason -- never a blank panel, and never an "unsupported" label on valid
// JSON. This extends (does not revert) the 0.9.80 fix NF-2026-00282, which
// stopped valid provider JSON from being rendered as an unsupported event
// shape: valid JSON objects are parsed and summarized by timelineEventFromObject
// instead of hitting the raw-text branch, and the raw-text branch itself no
// longer emits an "unsupported" placeholder.

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
    const childText = this.children.map((child) => (child && child.textContent ? child.textContent : "")).join("");
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
  return { api: global.__AIWORKHUB_LIVE_OUTPUT_FORMATTING__, document };
}

const { api, document } = loadFormatter();
assert(api, "formatter test hook is exposed");

function noUnsupportedLabel(event) {
  const blob = `${event.title} ${event.label} ${event.message}`.toLowerCase();
  assert(!blob.includes("unsupported"), `no "unsupported" label: ${JSON.stringify(event)}`);
  assert(event.title !== "Unrecognized event", `no "Unrecognized event" title: ${JSON.stringify(event)}`);
}

function nonBlank(event) {
  assert(event.message && event.message.trim().length > 0, `readable, non-blank message: ${JSON.stringify(event)}`);
}

// Unrecognised shape #1: a valid JSON object with an unknown `type`. It must
// render as a readable structured summary, never "unsupported"/"Unrecognized".
{
  const events = api.timelineEventsFromText(JSON.stringify(
    { type: "provider_custom_telemetry", phase: "warmup", detail: "loading model weights" },
  ));
  assert.strictEqual(events.length, 1);
  assert.strictEqual(events[0].kind, "event", "valid JSON is an event row, not a raw-text fallback");
  nonBlank(events[0]);
  noUnsupportedLabel(events[0]);
  assert(events[0].raw && events[0].raw.length > 0, "raw event retained for detail");
}

// Unrecognised shape #2: a valid JSON object with NO `type` and novel keys.
{
  const events = api.timelineEventsFromText(JSON.stringify(
    { novel_shape: true, payload: { a: 1, b: 2 }, note: "unknown provider frame" },
  ));
  assert.strictEqual(events.length, 1);
  assert.strictEqual(events[0].kind, "event");
  nonBlank(events[0]);
  noUnsupportedLabel(events[0]);
  assert(events[0].message.includes("novel_shape") || events[0].message.includes("Structured event"),
    "unknown valid shape is summarized readably");
}

// Unrecognised shape #3: a structured-looking but MALFORMED (unparseable) line.
// It must degrade to a readable raw rendering with a named reason (the row
// title), showing the actual content -- never blank, never "unsupported".
{
  const malformed = '{"type": "weird_frame", "value": oops-not-json';
  const events = api.timelineEventsFromText(malformed);
  assert.strictEqual(events.length, 1);
  assert.strictEqual(events[0].kind, "text");
  assert.strictEqual(events[0].title, "Unparsed provider line", "named reason in the row title");
  nonBlank(events[0]);
  noUnsupportedLabel(events[0]);
  assert(events[0].message.includes("weird_frame"), "the raw content is rendered, not hidden behind a placeholder");
}

// The formatted panel is never blank for these unrecognised shapes, and never
// prints an "unsupported" label.
{
  const decoded = [
    JSON.stringify({ type: "provider_custom_telemetry", phase: "warmup", detail: "loading model weights" }),
    JSON.stringify({ novel_shape: true, payload: { a: 1 }, note: "unknown provider frame" }),
    '{"type": "weird_frame", "value": oops-not-json',
  ].join("\n");
  api.renderFormattedLiveOutput(decoded);
  const rendered = document.querySelector("#detail-live-output-container").textContent;
  assert(rendered.trim().length > 0, "panel is not blank");
  assert(!rendered.toLowerCase().includes("unsupported"), "no unsupported label in the rendered panel");
  assert(!rendered.includes("Waiting for model output"), "unrecognised shapes still produce visible rows");
  assert(rendered.includes("weird_frame"), "malformed content rendered readably");
}

console.log("provider-event-shapes.test.js: ok");
