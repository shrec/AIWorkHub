"use strict";

// NF-2026-02222: worker Live Output renders as a chat, the way the manager console renders a
// manager turn. The webview translates every provider line with a port of
// aiworkhub.manager_loop_backends.translate/flush (manager_console.js) and renders the events with
// the manager console's row builders, keeping its own render state. Runs the real
// media/manager_console.js and media/app.js in this realm with a FakeElement DOM.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const test = require("node:test");

const FIXTURES = path.resolve(__dirname, "..", "..", "tests", "fixtures", "manager_streams");
// The same cases as tests/test_manager_stream_fixtures.py.
const FIXTURE_CASES = [
  ["claude_cli", "claude_cli"],
  ["claude_cli_seat", "claude_cli"],
  ["codex_cli", "codex_cli"],
  ["opencode_cli", "opencode_cli"],
];
const plain = (value) => JSON.parse(JSON.stringify(value));

class FakeElement {
  constructor(tagName) {
    this.tagName = String(tagName).toUpperCase();
    this.className = "";
    this.children = [];
    this.parentNode = null;
    this.dataset = {};
    this.style = {};
    this.attributes = {};
    this.listeners = {};
    this.hidden = false;
    this.open = false;
    this.value = "";
    this.scrollTop = 0;
    this.scrollHeight = 0;
    this.clientHeight = 0;
    this._textContent = "";
    this._innerHTML = "";
    this.classList = { add() {}, remove() {}, toggle() {}, contains() { return false; } };
  }

  append(...nodes) {
    for (const node of nodes) this.appendChild(node);
  }

  // A DOM node has one parent: appending moves it.
  appendChild(node) {
    if (node && node.__fragment) {
      for (const child of [...node.children]) this.appendChild(child);
      node.children = [];
      return node;
    }
    if (node && node.parentNode) node.parentNode.children = node.parentNode.children.filter((child) => child !== node);
    if (node && typeof node === "object") node.parentNode = this;
    this.children.push(node);
    return node;
  }

  replaceChildren(...nodes) {
    for (const child of this.children) if (child && typeof child === "object") child.parentNode = null;
    this.children = [];
    this.append(...nodes);
  }

  remove() {
    if (this.parentNode) this.parentNode.children = this.parentNode.children.filter((child) => child !== this);
    this.parentNode = null;
  }

  addEventListener(type, listener) {
    (this.listeners[type] = this.listeners[type] || []).push(listener);
  }

  setAttribute(name, value) {
    this.attributes[name] = String(value);
  }

  querySelector() {
    return new FakeElement("div");
  }

  querySelectorAll() {
    return [];
  }

  focus() {}

  get textContent() {
    return `${this._textContent}${this.children.map((child) => (child && child.textContent) || "").join("")}`;
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
      .replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, "\"").replace(/&#x27;/g, "'").replace(/&amp;/g, "&");
  }
}

class FakeText {
  constructor(text) {
    this.textContent = String(text);
    this.children = [];
  }
}

class FakeDocument {
  constructor() {
    this.elements = new Map();
  }

  createElement(tagName) {
    return new FakeElement(tagName);
  }

  createTextNode(text) {
    return new FakeText(text);
  }

  createDocumentFragment() {
    const fragment = new FakeElement("#fragment");
    fragment.__fragment = true;
    return fragment;
  }

  querySelector(selector) {
    if (!this.elements.has(selector)) this.elements.set(selector, new FakeElement("div"));
    return this.elements.get(selector);
  }

  querySelectorAll() {
    return [];
  }

  addEventListener() {}
}

function load() {
  const document = new FakeDocument();
  global.document = document;
  global.window = {
    setTimeout: () => 0,
    clearTimeout: () => {},
    addEventListener: () => {},
    requestAnimationFrame: () => 0,
  };
  global.acquireVsCodeApi = () => ({ getState: () => ({}), setState: () => {}, postMessage: () => {} });
  global.Intl = Intl;
  // Two classic scripts in one global scope, in the order the webview loads them.
  for (const name of ["manager_console.js", "app.js"]) {
    const file = path.join(__dirname, "..", "media", name);
    vm.runInThisContext(fs.readFileSync(file, "utf8"), { filename: file });
  }
  return { api: global.__AIWORKHUB_LIVE_OUTPUT_FORMATTING__, document };
}

const { api, document } = load();
const scope = (source) => vm.runInThisContext(source);
const container = () => document.querySelector("#detail-live-output-container");
const lines = (...events) => events.map((event) => (typeof event === "string" ? event : JSON.stringify(event))).join("\n");

function flat(node, found = []) {
  if (!node || typeof node !== "object") return found;
  found.push(node);
  for (const child of node.children || []) flat(child, found);
  return found;
}

const byClass = (root, name) => flat(root).filter((node) => String(node.className || "").split(/\s+/).includes(name));

function fixtureLines(stem) {
  return fs.readFileSync(path.join(FIXTURES, `${stem}.jsonl`), "utf8").split(/\r?\n/).filter((line) => line.trim()).map((line) => JSON.parse(line));
}

function fixtureExpected(stem) {
  return JSON.parse(fs.readFileSync(path.join(FIXTURES, `${stem}.expected.json`), "utf8"));
}

// ── Parity with manager_loop_backends (the reproduction: none of this existed before) ──

test("the webview exposes the translator port and the worker chat renderer", () => {
  for (const name of ["liveOutputTranslate", "liveOutputFlush", "liveOutputChatEvents", "renderWorkerChatEvents", "workerChatViewState"]) {
    assert.equal(scope(`typeof ${name}`), "function", `${name} is defined by manager_console.js`);
  }
});

for (const [stem, backendId] of FIXTURE_CASES) {
  test(`${stem}: translate + flush, deltas excluded, equals the Python expected events`, () => {
    const translate = scope("liveOutputTranslate");
    const flush = scope("liveOutputFlush");
    const context = scope("liveOutputTurnContext")();
    const events = [];
    for (const line of fixtureLines(stem)) events.push(...translate(backendId, line, context).filter((event) => event.type !== "delta"));
    events.push(...flush(backendId, context));
    assert.deepEqual(plain(events), fixtureExpected(stem));
  });

  test(`${stem}: liveOutputChatEvents keeps every translated event and turns only unrecognised lines into rows`, () => {
    const objects = fixtureLines(stem);
    const events = scope("liveOutputChatEvents")(backendId, objects);
    const translated = events.filter((event) => event.type !== "delta" && event.type !== "timeline_row");
    assert.deepEqual(plain(translated), fixtureExpected(stem));
    const translate = scope("liveOutputTranslate");
    const context = scope("liveOutputTurnContext")();
    const silent = objects.filter((line) => translate(backendId, line, context).length === 0);
    const rows = events.filter((event) => event.type === "timeline_row");
    assert.ok(rows.length <= silent.length, "a row only for a line the translator turned into nothing");
    assert.ok(!rows.some((row) => row.payload.title === "Unparsed provider line"), "a valid JSON line is never Unparsed");
  });
}

test("a file change counts content lines that look like headers (difflib port)", () => {
  const change = scope("liveOutputFileChange")("e1", "notes.txt", [["alpha\n", "+++beta\n---gamma\n"]], "update");
  assert.equal(change.payload.added, 2);
  assert.equal(change.payload.removed, 1);
  assert.deepEqual(change.payload.diff.split("\n").slice(0, 2), ["--- a/notes.txt", "+++ b/notes.txt"]);
  const edit = scope("liveOutputFileChange")("e2", "x.py", [["a\nb\nc\nd\ne\nf\ng\nh\ni\n", "a\nb\nc\nd\nE\nf\ng\nh\ni\n"]], "update");
  assert.equal(edit.payload.diff, ["--- a/x.py", "+++ b/x.py", "@@ -2,7 +2,7 @@", " b", " c", " d", "-e", "+E", " f", " g", " h"].join("\n"));
});

test("an unpaired result without context still carries its call id", () => {
  const event = { type: "user", message: { content: [{ type: "tool_result", tool_use_id: "toolu_1", content: "ok" }] } };
  assert.deepEqual(plain(scope("liveOutputTranslate")("claude_cli", event)), [
    { type: "tool_result", payload: { call_id: "toolu_1", name: "", output: "ok", is_error: false } },
  ]);
});

// ── The rendered chat ──

const CLAUDE_TURN = [
  { type: "system", subtype: "init", session_id: "s1", model: "claude-test" },
  { type: "assistant", message: { model: "claude-test", content: [{ type: "text", text: "First I **check** the repo." }] } },
  { type: "assistant", message: { model: "claude-test", content: [{ type: "tool_use", id: "t1", name: "Bash", input: { command: "git status" } }] } },
  { type: "user", message: { role: "user", content: [{ type: "tool_result", tool_use_id: "t1", content: "nothing to commit" }] } },
  { type: "assistant", message: { model: "claude-test", content: [{ type: "tool_use", id: "t2", name: "Read", input: { file_path: "notes.txt" } }] } },
  { type: "user", message: { role: "user", content: [{ type: "tool_result", tool_use_id: "t2", content: "alpha" }] } },
  { type: "assistant", message: { model: "claude-test", content: [{ type: "tool_use", id: "t3", name: "Edit", input: { file_path: "notes.txt", old_string: "alpha", new_string: "beta" } }] } },
  { type: "user", message: { role: "user", content: [{ type: "tool_result", tool_use_id: "t3", content: "The file notes.txt has been updated." }] } },
  { type: "assistant", message: { model: "claude-test", content: [{ type: "text", text: "All done." }] } },
  { type: "result", subtype: "success", session_id: "s1", result: "All done.", usage: { input_tokens: 3100, cache_read_input_tokens: 80000, cache_creation_input_tokens: 8000, output_tokens: 1200 } },
];

test("a Claude worker renders as chat rows, oldest first, with a runner/model label and one footer", () => {
  api.startLiveOutputPolling("claude-task");
  api.renderLiveOutput({ ok: true, task_id: "claude-task", output: `${lines(...CLAUDE_TURN)}\n`, next_cursor: 1, liveness_state: "exited", exit_code: 0 });
  const box = container();
  const text = box.textContent;
  assert.ok(text.indexOf("First I") < text.indexOf("All done."), "chronological: oldest at the top");
  assert.equal(countOccurrences(text, "All done."), 1, "the result repeats the last answer; it is shown once");
  const labels = byClass(box, "worker-chat-label");
  assert.ok(labels.length >= 2 && labels.every((label) => label.textContent === "claude_cli · claude-test"), "assistant bubbles name the runner and model");
  assert.ok(flat(box).some((node) => node.tagName === "STRONG" && node.textContent === "check"), "assistant text is markdown");
  const group = byClass(box, "mc-activity-group");
  assert.equal(group.length, 1, "the tool calls, command and file change collapse into one group");
  assert.equal(group[0].open, false);
  assert.equal(group[0].children[0].textContent, "1 tool · 1 command · 1 file");
  assert.ok(byClass(box, "mc-mono").some((node) => node.textContent === "git status"), "the command row shows its command");
  assert.ok(byClass(box, "mc-add").some((node) => node.textContent === "+beta\n"), "the file change shows its diff");
  const footers = byClass(box, "mc-footer");
  assert.equal(footers.length, 1, "one turn_end usage footer");
  assert.ok(footers[0].textContent.startsWith("turn 1 · 3 tools · in 3.1k · cache 88k · out 1.2k"));
  // Only the init line has no chat translation, so it alone keeps a compact row.
  const rows = byClass(box, "live-output-row");
  assert.equal(rows.length, 1);
  assert.ok(rows[0].textContent.includes("system"));
});

test("codex and opencode workers render commands, tools and file changes as chat rows", () => {
  api.startLiveOutputPolling("codex-task");
  api.renderLiveOutput({ ok: true, task_id: "codex-task", next_cursor: 1, liveness_state: "running", output: `${lines(
    { type: "turn.started" },
    { type: "item.started", item: { id: "c1", type: "command_execution", command: "pytest -q", aggregated_output: "", exit_code: null } },
    { type: "item.completed", item: { id: "c1", type: "command_execution", command: "pytest -q", aggregated_output: "3 passed\n", exit_code: 0 } },
    { type: "item.completed", item: { id: "m1", type: "agent_message", text: "Tests pass." } },
    { type: "turn.completed", usage: { input_tokens: 500, cached_input_tokens: 100, output_tokens: 50 } },
  )}\n` });
  let box = container();
  assert.equal(byClass(box, "mc-command").length, 1, "started and completed merge into one command row");
  assert.ok(byClass(box, "mc-chip").some((chip) => chip.textContent === "exit 0"));
  assert.ok(box.textContent.includes("Tests pass."));
  assert.ok(byClass(box, "worker-chat-label").every((label) => label.textContent === "codex_cli"));
  assert.equal(byClass(box, "mc-footer").length, 1);

  api.startLiveOutputPolling("opencode-task");
  api.renderLiveOutput({ ok: true, task_id: "opencode-task", next_cursor: 1, liveness_state: "exited", exit_code: 0, output: `${lines(
    { type: "step_start", part: { type: "step-start" } },
    { type: "text", part: { type: "text", text: "Looking at the route." } },
    { type: "tool_use", part: { id: "g1", type: "tool", tool: "grep", state: { status: "completed", input: { pattern: "TODO" }, output: "3 matches" } } },
    { type: "tool_use", part: { id: "w1", type: "tool", tool: "write", state: { status: "completed", input: { path: "a.txt", content: "x\n" }, output: "Created a.txt" } } },
    { type: "step_finish", part: { type: "step-finish", reason: "stop", tokens: { input: 10, output: 5, cache: { read: 2, write: 0 } } } },
  )}\n` });
  box = container();
  assert.ok(box.textContent.indexOf("Looking at the route.") < box.textContent.indexOf("3 matches"));
  assert.ok(byClass(box, "manager-chat-tool-row").some((row) => row.children[0].textContent.startsWith("grep · done")));
  assert.ok(byClass(box, "mc-file-change").length === 1 && box.textContent.includes("a.txt (+1 −0)"));
  assert.equal(byClass(box, "mc-footer").length, 1, "the step usage closes the turn with one footer");
  assert.equal(byClass(box, "live-output-row").length, 0, "every opencode line here has a chat translation");
});

test("unrecognised and non-JSON lines render exactly the old timeline row, one each", () => {
  const unrecognised = [
    JSON.stringify({ type: "aiworkhub_tool_activity", kind: "tool", tool_name: "read", tool_state: "completed", tool_transport: "native", call_id: "call-1", sequence: 1, input_preview: "in", output_preview: "out" }),
    JSON.stringify({ type: "aiworkhub_live_output_oversize", chars: 2000000 }),
    JSON.stringify({ type: "provider_custom_telemetry", phase: "warmup", detail: "loading model weights" }),
    "plain worker line",
    "{not valid json",
  ];
  for (const line of unrecognised) {
    const [old] = api.timelineEventsFromText(line);
    api.renderFormattedLiveOutput(line);
    const rows = byClass(container(), "live-output-row");
    assert.equal(rows.length, 1, `one row for ${line.slice(0, 40)}`);
    assert.equal(byClass(rows[0], "live-output-row-title")[0].textContent, old.title);
    assert.equal(byClass(rows[0], "live-output-row-state")[0].textContent, old.state);
    assert.equal(byClass(rows[0], "live-output-row-message")[0].textContent, old.message);
    if (line.startsWith("{\"")) assert.notEqual(old.title, "Unparsed provider line");
  }
  api.renderFormattedLiveOutput(unrecognised.join("\n"));
  assert.equal(byClass(container(), "live-output-row").length, unrecognised.length, "no line is dropped");
});

test("appending a page keeps unchanged rows, open details and the scroll position rules", () => {
  api.startLiveOutputPolling("page-task");
  const box = container();
  const page = (output, cursor) => api.renderLiveOutput({ ok: true, task_id: "page-task", output, next_cursor: cursor, liveness_state: "running" });
  page(`${lines(CLAUDE_TURN[1], CLAUDE_TURN[2], CLAUDE_TURN[3], CLAUDE_TURN[4])}\n`, 1);
  const firstRow = box.children[0];
  const group = byClass(box, "mc-activity-group")[0];
  assert.ok(firstRow && group);
  group.open = true;
  box.scrollHeight = 1000;
  box.clientHeight = 200;
  box.scrollTop = 790;
  page(`${lines(CLAUDE_TURN[5])}\n`, 2);
  assert.equal(box.children[0], firstRow, "the unchanged first row is the same node");
  assert.equal(byClass(box, "mc-activity-group")[0], group, "the group node is kept");
  assert.equal(group.open, true, "an opened <details> stays open");
  assert.equal(box.scrollTop, 1000, "within 24 px of the bottom: follows the new rows");
  box.scrollTop = 100;
  page(`${lines(CLAUDE_TURN[8])}\n`, 3);
  assert.equal(box.children[0], firstRow);
  assert.equal(box.scrollTop, 100, "scrolled up: the position is kept");
  assert.ok(box.textContent.includes("All done."));
});

test("streamed text shows while it arrives and once when its message completes", () => {
  const delta = (text) => ({ type: "stream_event", event: { type: "content_block_delta", delta: { type: "text_delta", text } } });
  api.renderFormattedLiveOutput(lines(delta("Hello "), delta("world")), { liveTail: true });
  assert.ok(byClass(container(), "mc-streaming").length === 1 && container().textContent.includes("Hello world"));
  api.renderFormattedLiveOutput(lines(delta("Hello "), delta("world"), { type: "assistant", message: { content: [{ type: "text", text: "Hello world" }] } }), { liveTail: true });
  assert.equal(countOccurrences(container().textContent, "Hello world"), 1);
  assert.equal(byClass(container(), "mc-streaming").length, 0);
});

test("a worker render leaves the manager console's state alone", () => {
  scope("managerConsoleTerminalSeq = 1000000; managerConsoleLastSignature = 'manager-signature'; managerConsoleStripPartialKey = 'strip';");
  scope("managerConsoleActiveIndex = new Proxy({}, { get() { throw new Error('worker read managerConsoleActiveIndex'); } });");
  const appState = scope("state");
  Object.defineProperty(appState, "managerChatEvents", { configurable: true, get() { throw new Error("worker read the manager events"); } });
  const announcer = scope("elements").managerChatAnnouncer;
  announcer.textContent = "manager announcement";
  try {
    api.renderFormattedLiveOutput(lines(
      { type: "assistant", message: { content: [{ type: "thinking", thinking: "plan" }, { type: "tool_use", id: "r1", name: "Bash", input: { command: "sleep 5" } }] } },
    ), { liveTail: true });
    assert.ok(byClass(container(), "mc-chip").some((chip) => chip.textContent === "running"), "the worker's own terminal seq decides running");
    assert.ok(container().textContent.includes("Reasoning"));
  } finally {
    delete appState.managerChatEvents;
    appState.managerChatEvents = [];
  }
  assert.equal(scope("managerConsoleTerminalSeq"), 1000000);
  assert.equal(scope("managerConsoleLastSignature"), "manager-signature");
  assert.equal(scope("managerConsoleStripPartialKey"), "strip");
  assert.equal(announcer.textContent, "manager announcement");
  scope("managerConsoleActiveIndex = null; managerConsoleTerminalSeq = 0; managerConsoleLastSignature = null; managerConsoleStripPartialKey = '';");
});

test("event text is redacted by the shared rule and reaches the DOM only as text", () => {
  const secret = "sk_live_abcdefghijklmnopqrstuv";
  api.startLiveOutputPolling("secret-task");
  const output = `${lines(
    { type: "assistant", message: { content: [{ type: "text", text: `key ${secret} <img src=x onerror=alert(1)> in <redacted-host-path>` }] } },
    { type: "assistant", message: { content: [{ type: "tool_use", id: "s1", name: "WebFetch", input: { url: "https://x.test", token: secret } }] } },
    { type: "user", message: { role: "user", content: [{ type: "tool_result", tool_use_id: "s1", content: `fetched with ${secret}` }] } },
  )}\n`;
  api.renderLiveOutput({ ok: true, task_id: "secret-task", output, next_cursor: 1, liveness_state: "exited", exit_code: 0 });
  const box = container();
  const text = box.textContent;
  assert.ok(!text.includes(secret), "a credential in assistant text, tool input or tool output is masked");
  assert.equal(countOccurrences(text, "[token]"), 3);
  assert.equal(scope("redactDisplayText")(`key ${secret}`), "key [token]", "the same rule the old rows applied");
  assert.ok(text.includes("<img src=x onerror=alert(1)>"), "markup is shown as text");
  assert.ok(text.includes("<redacted-host-path>"), "the server's host-path mask is shown as is");
  assert.ok(flat(box).every((node) => !node._innerHTML), "no event data goes through innerHTML");
  assert.equal(document.querySelector("#detail-live-output-raw-content").textContent, output, "the Raw panel still shows the decoded text");
});

test("a live OpenCode tail keeps a running call running and closes no turn after a step", () => {
  const stream = lines(
    { type: "step_start", part: { type: "step-start" } },
    { type: "step_finish", part: { type: "step-finish", reason: "tool-calls", tokens: { input: 10, output: 5, cache: { read: 2, write: 0 } } } },
    { type: "step_start", part: { type: "step-start" } },
    { type: "tool_use", part: { id: "g2", type: "tool", tool: "grep", state: { status: "running", input: { pattern: "TODO" } } } },
  );
  api.renderFormattedLiveOutput(stream, { liveTail: true });
  let box = container();
  assert.ok(byClass(box, "manager-chat-tool-row").some((row) => row.children[0].textContent.startsWith("grep · running")));
  assert.ok(!box.textContent.includes("interrupted"), "a step_finish mid-run is not the end of the turn");
  assert.equal(byClass(box, "mc-footer").length, 0, "no usage footer while the run is live");
  api.renderFormattedLiveOutput(stream);
  box = container();
  assert.equal(byClass(box, "mc-footer").length, 1, "the finished run closes the turn with the step usage");
});

function countOccurrences(text, needle) {
  return text.split(needle).length - 1;
}
