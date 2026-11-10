"use strict";

// NF-2026-02221: a valid provider event split across 8 KiB server pages must render as one normal
// row (never "Unparsed provider line"), a running log must catch up at a usable speed, and host-path
// redaction must keep the JSON the panel shows parseable. Runs the real media/app.js in a vm with the
// FakeElement harness, and the real extension.js sanitizer through the Module._load vscode stub.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const test = require("node:test");
const Module = require("node:module");

const vscode = {
  workspace: { workspaceFolders: [], getConfiguration: () => ({ get: (_key, fallback) => fallback }) },
  LanguageModelChatToolMode: { Auto: 1, Required: 2 },
  LanguageModelToolResultPart: class { constructor(callId, content) { Object.assign(this, { callId, content }); } },
  LanguageModelChatMessage: {
    User: (content) => ({ role: "user", content }),
    Assistant: (content) => ({ role: "assistant", content }),
  },
};
const originalLoad = Module._load;
Module._load = function (name, parent, isMain) {
  return name === "vscode" ? vscode : originalLoad.call(this, name, parent, isMain);
};
let bridge;
try { bridge = require("../extension.js").__testInternals; }
finally { Module._load = originalLoad; }

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
    this.classList = { add() {}, remove() {}, toggle() {}, contains() { return false; } };
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
    // A browser textarea decodes every entity the server escapes; mirror that for decodeLiveOutput.
    this.value = this._innerHTML
      .replace(/&lt;/g, "<")
      .replace(/&gt;/g, ">")
      .replace(/&quot;/g, "\"")
      .replace(/&#x27;/g, "'")
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

// Captured window timers: the single-owner poll invariant is checked against this map.
const timers = new Map();
let nextHandle = 1;
const posted = [];

function loadFormatter() {
  const document = new FakeDocument();
  global.document = document;
  global.window = {
    setTimeout: (fn, delay) => {
      const handle = nextHandle;
      nextHandle += 1;
      timers.set(handle, { fn, delay });
      return handle;
    },
    clearTimeout: (handle) => { timers.delete(handle); },
    addEventListener: () => {},
  };
  global.acquireVsCodeApi = () => ({
    getState: () => ({}),
    setState: () => {},
    postMessage: (message) => { posted.push(message); },
  });
  global.Intl = Intl;
  for (const name of ["manager_console.js", "app.js"]) {
    const file = path.join(__dirname, "../media", name);
    vm.runInThisContext(fs.readFileSync(file, "utf8"), { filename: file });
  }
  return { api: global.__AIWORKHUB_LIVE_OUTPUT_FORMATTING__, document };
}

const { api, document } = loadFormatter();
const panel = () => document.querySelector("#detail-live-output-container").textContent;
const countOf = (text, needle) => text.split(needle).length - 1;

// The server HTML-escapes the output text, then the webview decodes it again.
function serverEscape(text) {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#x27;");
}

function serverUnescape(text) {
  return text
    .replace(/&quot;/g, "\"")
    .replace(/&#x27;/g, "'")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&amp;/g, "&");
}

function pagesOf(text, size) {
  const out = [];
  for (let at = 0; at < text.length; at += size) {
    out.push(text.slice(at, at + size));
  }
  return out;
}

test("a valid provider line split into 8 KiB pages renders once and never as Unparsed", () => {
  const line = JSON.stringify({
    type: "user",
    message: { role: "user", content: [{ type: "tool_result", tool_use_id: "toolu_big", content: "x".repeat(70 * 1024) }] },
  });
  const escaped = serverEscape(`${line}\n`);
  assert.ok(escaped.length > 64 * 1024, "the escaped line is larger than the old 64 KiB client bound");
  api.startLiveOutputPolling("big-line-task");
  const pieces = pagesOf(escaped, 8192);
  let cursor = 0;
  let sawNeutralRow = false;
  pieces.forEach((page, index) => {
    cursor += page.length;
    const last = index === pieces.length - 1;
    api.renderLiveOutput({
      ok: true, task_id: "big-line-task", output: page, next_cursor: cursor,
      liveness_state: "running", truncated: !last,
    });
    const rendered = panel();
    assert.ok(!rendered.includes("Unparsed provider line"), `page ${index} must not render as Unparsed`);
    if (!last && rendered.includes("Receiving large event...")) {
      sawNeutralRow = true;
    }
  });
  assert.ok(sawNeutralRow, "while the line is still arriving it shows the neutral row");
  const rendered = panel();
  assert.ok(rendered.includes("Tool result"), "the completed line renders as a normal tool result");
  assert.ok(!rendered.includes("Receiving large event..."), "the neutral row disappears when the line completes");
});

test("one-argument calls keep today's strict behaviour; live-tail calls get the neutral row", () => {
  const open = "{\"type\":\"x\", \"v\": ";
  const strict = api.timelineEventsFromText(open);
  assert.equal(strict.length, 1);
  assert.equal(strict[0].title, "Unparsed provider line");
  assert.equal(strict[0].kind, "text");

  const live = api.timelineEventsFromText(open, { liveTail: true });
  assert.equal(live.length, 1);
  assert.equal(live[0].title, "Receiving large event...");
  assert.ok(/KB received$/.test(live[0].message), "the neutral row states its size in KB");
  assert.ok(!JSON.stringify(live[0]).includes("Unparsed"));

  const parses = api.timelineEventsFromText("{\"type\":\"warning\",\"warning\":\"w\"}", { liveTail: true });
  assert.equal(parses[0].kind, "warning", "a last segment that already parses renders as today");

  const plain = api.timelineEventsFromText("plain partial", { liveTail: true });
  assert.equal(plain[0].title, "Output", "plain text renders as today");
});

test("a complete line that is not JSON still shows Unparsed, even in live mode", () => {
  const events = api.timelineEventsFromText("{not valid json\n", { liveTail: true });
  assert.equal(events.length, 1);
  assert.equal(events[0].title, "Unparsed provider line");
});

test("a finished process parses its remaining tail normally and shows a really unparsable tail as Unparsed", () => {
  api.startLiveOutputPolling("finished-task");
  api.renderLiveOutput({
    ok: true, task_id: "finished-task", output: "{\"type\":\"x\", \"v\": ",
    next_cursor: 20, liveness_state: "exited", exit_code: 0,
  });
  assert.ok(panel().includes("Unparsed provider line"));
});

test("a finished process with an empty final page re-renders its open tail once", () => {
  api.startLiveOutputPolling("empty-final-task");
  api.renderLiveOutput({
    ok: true, task_id: "empty-final-task", output: "{\"type\":\"x\", \"v\": ",
    next_cursor: 20, liveness_state: "running",
  });
  assert.ok(panel().includes("Receiving large event..."));

  const exited = { ok: true, task_id: "empty-final-task", output: "", next_cursor: 20, liveness_state: "exited", exit_code: 0 };
  api.renderLiveOutput(exited);
  assert.ok(panel().includes("Unparsed provider line"));
  assert.ok(!panel().includes("Receiving large event..."));

  // The poll keeps running after exit: a later empty page must not rebuild the panel.
  const container = document.querySelector("#detail-live-output-container");
  const rows = container.children;
  api.renderLiveOutput(exited);
  assert.equal(container.children, rows);
});

test("trimming drops only the oldest whole lines and keeps the newest complete line", () => {
  api.startLiveOutputPolling("trim-task");
  const older = Array.from({ length: 200 }, (_value, index) => `L${index}:${"y".repeat(1000)}`);
  api.renderLiveOutput({
    ok: true, task_id: "trim-task", output: `${older.join("\n")}\n`, next_cursor: 1, liveness_state: "running",
  });
  let rendered = panel();
  assert.ok(rendered.includes("L199:"), "the newest line is shown");
  assert.ok(!rendered.includes("L0:"), "the oldest whole line was trimmed");

  // A complete line larger than the 64 KiB bound is never cut and always survives the trim.
  api.renderLiveOutput({
    ok: true, task_id: "trim-task", output: `N:${"q".repeat(100 * 1024)}\n`, next_cursor: 2, liveness_state: "running",
  });
  // The panel shows a redacted preview (a 100 KiB run of one character is masked as a token), so the
  // kept buffer is checked in the raw view, which holds the full decoded text.
  const raw = document.querySelector("#detail-live-output-raw-content").textContent;
  assert.equal(raw, `N:${"q".repeat(100 * 1024)}\n`, "the newest complete line stays intact even when it alone exceeds the bound");
  rendered = panel();
  assert.ok(rendered.includes("N:"), "the newest complete line still renders as a row");
});

test("an unfinished tail longer than 64 KiB is kept whole until its newline arrives", () => {
  api.startLiveOutputPolling("tail-task");
  const head = "{\"type\":\"x\",\"b\":\"";
  api.renderLiveOutput({
    ok: true, task_id: "tail-task", output: `${head}${"z".repeat(100 * 1024)}`,
    next_cursor: 1, liveness_state: "running", truncated: true,
  });
  let rendered = panel();
  assert.ok(rendered.includes("Receiving large event..."), "the open tail shows the neutral row");
  assert.ok(!rendered.includes("Unparsed provider line"), "the head of the open tail is not cut away");

  api.renderLiveOutput({
    ok: true, task_id: "tail-task", output: "\"}\n", next_cursor: 2, liveness_state: "running", truncated: false,
  });
  rendered = panel();
  assert.ok(!rendered.includes("Receiving large event..."), "the neutral row disappears on completion");
  assert.ok(!rendered.includes("Unparsed provider line"));
});

test("a line longer than 1 MiB becomes exactly one size row and is never retained", () => {
  api.startLiveOutputPolling("oversize-task");
  const body = `{"type":"x","b":"${"z".repeat(1024 * 1024 + 4096)}`;
  let cursor = 0;
  for (const page of pagesOf(body, 64 * 1024)) {
    cursor += page.length;
    api.renderLiveOutput({
      ok: true, task_id: "oversize-task", output: page, next_cursor: cursor,
      liveness_state: "running", truncated: true,
    });
  }
  let rendered = panel();
  assert.equal(countOf(rendered, "characters not shown"), 1, "exactly one size row while the line is arriving");
  assert.ok(!rendered.includes("Unparsed provider line"));

  api.renderLiveOutput({
    ok: true, task_id: "oversize-task", output: "\"}\n", next_cursor: cursor + 3,
    liveness_state: "running", truncated: false,
  });
  rendered = panel();
  assert.equal(countOf(rendered, "characters not shown"), 1, "still exactly one size row after the newline");
  assert.ok(!rendered.includes("Receiving large event..."));
});

test("a running log re-polls at 150 ms while truncated, at 4 s otherwise, with one timer at a time", () => {
  api.startLiveOutputPolling("pace-task");
  timers.clear();
  api.renderLiveOutput({
    ok: true, task_id: "pace-task", output: "a\n", next_cursor: 2, liveness_state: "running", truncated: true,
  });
  assert.equal(timers.size, 1);
  assert.deepEqual([...timers.values()].map((timer) => timer.delay), [150]);

  api.renderLiveOutput({
    ok: true, task_id: "pace-task", output: "b\n", next_cursor: 4, liveness_state: "running", truncated: false,
  });
  assert.equal(timers.size, 1, "still exactly one poll owner");
  assert.deepEqual([...timers.values()].map((timer) => timer.delay), [4000]);
});

test("sanitizeLiveOutputPayload redacts host paths in the escaped JSON and keeps it parseable", () => {
  const line = JSON.stringify({
    values: [
      "see D:\\proj\\x\\y.py now",
      "D:\\proj\\x",
      JSON.stringify({ p: "D:\\proj\\x" }),
      "query(\"Y:\\\")",
      "open C:\\temp\\new.txt",
    ],
  });
  const sanitized = bridge.sanitizeLiveOutputPayload({
    ok: true, task_id: "t", output: `${serverEscape(line)}\n`, stderr_tail: "C:\\temp\\err.txt",
  });
  assert.equal(sanitized.task_id, "t", "fields other than output keep sanitizeWebviewPayload behaviour");
  assert.ok(!sanitized.stderr_tail.includes("temp"), "stderr_tail is still redacted by sanitizeWebviewPayload");

  const restored = serverUnescape(sanitized.output).trim();
  const parsed = JSON.parse(restored);
  assert.ok(!restored.includes("proj"), "no host path fragment remains");
  assert.ok(!restored.includes("temp"), "no host path fragment remains");
  assert.ok(parsed.values[0].includes("<redacted-host-path>"));
});

test("sanitizeLiveOutputPayload keeps a lone trailing backslash of a page", () => {
  assert.equal(bridge.sanitizeLiveOutputPayload({ output: "tail \\" }).output, "tail \\");
  assert.equal(bridge.sanitizeLiveOutputPayload({ output: "a\\" }).output, "a\\");
});
