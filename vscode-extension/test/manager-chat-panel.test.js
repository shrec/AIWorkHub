"use strict";

const assert = require("node:assert/strict");
// Objects built inside the vm-loaded extension and webview carry that realm's
// Object.prototype, so strict deep equality compares plain copies of them.
const plain = (value) => JSON.parse(JSON.stringify(value));
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const extensionSource = fs.readFileSync(path.join(__dirname, "..", "extension.js"), "utf8").replace(/\r\n/g, "\n");
const appSource = fs.readFileSync(path.join(__dirname, "..", "media", "app.js"), "utf8");
const cssSource = fs.readFileSync(path.join(__dirname, "..", "media", "app.css"), "utf8");
const consoleSource = fs.readFileSync(path.join(__dirname, "..", "media", "manager_console.js"), "utf8");
const consoleCss = fs.readFileSync(path.join(__dirname, "..", "media", "manager_console.css"), "utf8");

// Extracts an exact shipped block (verbatim, not reimplemented) so assertions
// exercise the real production code path, following the pattern already used
// by kpi-dashboard.test.js / dashboard-panel-dispose-identity.test.js.
function extractSlice(source, startMarker, endMarker, label) {
  const start = source.indexOf(startMarker);
  assert.notEqual(start, -1, `${label}: start marker not found`);
  const end = source.indexOf(endMarker, start);
  assert.notEqual(end, -1, `${label}: end marker not found`);
  return source.slice(start, end + endMarker.length);
}

function flush() {
  return new Promise((resolve) => setImmediate(resolve));
}

// ── extension.js: the seven new message types routed to their MCP tools ──────

function makeClient(responder) {
  const calls = [];
  return {
    calls,
    async callTool(name, args) {
      calls.push({ name, args });
      return responder ? responder(name, args) : { ok: true };
    },
  };
}

function makeView() {
  const posts = [];
  return {
    posts,
    boundClient: null,
    bindClient(client) {
      this.boundClient = client;
    },
    stillBoundTo(client) {
      return this.boundClient === client;
    },
    postMessage(message) {
      posts.push(message);
    },
  };
}

function loadHostSlice() {
  const allowed = extractSlice(
    extensionSource,
    "const ALLOWED_INBOUND_MESSAGE_TYPES = new Set([",
    "]);",
    "ALLOWED_INBOUND_MESSAGE_TYPES",
  );
  const outbound = extractSlice(
    extensionSource,
    "const OUTBOUND_TYPES = Object.freeze({",
    "});",
    "OUTBOUND_TYPES",
  );
  const tools = extractSlice(
    extensionSource,
    "const MANAGER_LOOP_TOOLS = Object.freeze({",
    "const MANAGER_SESSION_ID_RE = /^[A-Za-z0-9][A-Za-z0-9_-]{7,63}$/;",
    "MANAGER_LOOP_TOOLS/MANAGER_LOOP_BACKENDS/MANAGER_SESSION_ID_RE",
  );
  const managerLoopClient = extractSlice(
    extensionSource,
    "let managerLoopMcpClient = null;",
    "    // Best-effort shutdown -- the reference above is already cleared.\n  }\n}",
    "getManagerLoopMcpClient/disposeManagerLoopMcpClient",
  );
  const helpers = extractSlice(
    extensionSource,
    "async function pushManagerLoopStatus(view) {",
    "      payload: { ok: false, error: sanitizeErrorMessage(err) },\n    });\n  }\n}",
    "manager loop host functions",
  );
  const handler = extractSlice(
    extensionSource,
    "function handleInboundMessage(view, message) {",
    '      pushManagerLoopEvents(view, sessionId, Number.isFinite(afterSeq) && afterSeq >= 0 ? afterSeq : 0);\n      break;\n    }\n    default:\n      break;\n  }\n}',
    "handleInboundMessage",
  );

  let currentClient = null;
  // Every gated child this test run spawns, in creation order -- lets tests
  // assert lazy creation, reuse and re-creation-after-dispose by identity.
  const managerLoopClientInstances = [];

  class FakeMcpStdioClient {
    constructor(repositoryRoot, outputChannel, repositoryIdentity, claimEpisode, options) {
      this.repositoryRoot = repositoryRoot;
      this.outputChannel = outputChannel;
      this.repositoryIdentity = repositoryIdentity;
      this.claimEpisode = claimEpisode;
      this.options = options || {};
      this.calls = [];
      this.terminated = false;
      managerLoopClientInstances.push(this);
    }
    async callTool(name, args) {
      this.calls.push({ name, args });
      return { ok: true };
    }
    async stopDispatcherThenTerminate() {
      this.terminated = true;
    }
  }

  const context = {
    getMcpClient: () => currentClient,
    outputChannel: { appendLine: () => {} },
    McpStdioClient: FakeMcpStdioClient,
    sanitizeWebviewPayload: (value) => value,
    sanitizeErrorMessage: (err) => String((err && err.message) || "mcp_unavailable"),
  };
  vm.createContext(context);
  vm.runInContext(
    `"use strict";\n${allowed}\n${outbound}\n${tools}\n${managerLoopClient}\n${helpers}\n${handler}\n` +
      "this.api = { handleInboundMessage, OUTBOUND_TYPES, MANAGER_LOOP_TOOLS, MANAGER_LOOP_BACKENDS, getManagerLoopMcpClient, disposeManagerLoopMcpClient };",
    context,
  );
  return {
    api: context.api,
    managerLoopClientInstances,
    setClient(client) {
      currentClient = client;
    },
  };
}

test("managerLoopStart/Send/Rotate/Status/Events reach one lazily-created client spawned with both gates set, and never the gate-free client", async () => {
  const harness = loadHostSlice();
  const client = makeClient();
  harness.setClient(client);
  const view = makeView();

  assert.equal(harness.managerLoopClientInstances.length, 0);

  harness.api.handleInboundMessage(view, { type: "managerLoopStart", backendId: "claude_cli", model: "opus" });
  harness.api.handleInboundMessage(view, { type: "managerLoopSend", text: "hello manager", backendId: "codex_cli", model: "gpt-x" });
  harness.api.handleInboundMessage(view, { type: "managerLoopRotate", reason: "context threshold" });
  harness.api.handleInboundMessage(view, { type: "managerLoopStatus" });
  harness.api.handleInboundMessage(view, {
    type: "managerLoopEvents",
    sessionId: "mls-aaaa1111bbbb2222",
    afterSeq: 5,
  });
  await flush();

  // One gated child spawned lazily on the first managerLoop* message, then
  // reused -- never re-created -- for every message after it.
  assert.equal(harness.managerLoopClientInstances.length, 1);
  const gatedClient = harness.managerLoopClientInstances[0];
  assert.equal(gatedClient.options.grantManagerLoopGates, true);

  const find = (name) => gatedClient.calls.find((call) => call.name === name);
  assert.deepEqual(plain(find("aiworkhub_manager_loop_start").args), { backend_id: "claude_cli", model: "opus" });
  assert.deepEqual(plain(find("aiworkhub_manager_loop_send").args), { text: "hello manager", backend_id: "codex_cli", model: "gpt-x", reasoning: "" });
  assert.deepEqual(plain(find("aiworkhub_manager_loop_rotate").args), { reason: "context threshold" });
  assert.deepEqual(plain(find("aiworkhub_manager_loop_events").args), { session_id: "mls-aaaa1111bbbb2222", after_seq: 5 });
  // Status is called both explicitly and as the authoritative refresh after
  // every mutating action, so it must have been reached at least once.
  assert.ok(gatedClient.calls.some((call) => call.name === "aiworkhub_manager_loop_status"));

  // None of the seven tools ever reach the dashboard's own read-only client.
  assert.equal(client.calls.length, 0);
});

test("managerLoopSend with an unlisted backend is refused before any MCP call", async () => {
  const harness = loadHostSlice();
  harness.setClient(makeClient());
  const view = makeView();

  harness.api.handleInboundMessage(view, { type: "managerLoopSend", text: "hi", backendId: "not_a_backend", model: "m" });
  await flush();

  assert.equal(harness.managerLoopClientInstances.length, 0);
  assert.deepEqual(plain(view.posts), [{ type: harness.api.OUTBOUND_TYPES.error, message: "invalid_backend_id" }]);
});

test("managerLoopEnsure reaches the gated client as aiworkhub_manager_loop_ensure with no args", async () => {
  const harness = loadHostSlice();
  harness.setClient(makeClient());
  const view = makeView();

  harness.api.handleInboundMessage(view, { type: "managerLoopEnsure" });
  await flush();

  assert.equal(harness.managerLoopClientInstances.length, 1);
  const gatedClient = harness.managerLoopClientInstances[0];
  assert.deepEqual(plain(gatedClient.calls.find((call) => call.name === "aiworkhub_manager_loop_ensure").args), {});
});

test("managerLoopContinue and managerLoopNew reach the gated client; a short id does not", async () => {
  const harness = loadHostSlice();
  harness.setClient(makeClient());
  const view = makeView();

  harness.api.handleInboundMessage(view, { type: "managerLoopContinue", sessionId: "mls-aaaa1111bbbb2222" });
  harness.api.handleInboundMessage(view, { type: "managerLoopNew" });
  harness.api.handleInboundMessage(view, { type: "managerLoopContinue", sessionId: "nope" });
  await flush();

  assert.equal(harness.managerLoopClientInstances.length, 1);
  const gatedClient = harness.managerLoopClientInstances[0];
  assert.deepEqual(
    plain(gatedClient.calls.find((call) => call.name === "aiworkhub_manager_loop_continue").args),
    { session_id: "mls-aaaa1111bbbb2222" },
  );
  assert.deepEqual(plain(gatedClient.calls.find((call) => call.name === "aiworkhub_manager_loop_new").args), {});
  assert.equal(gatedClient.calls.filter((call) => call.name === "aiworkhub_manager_loop_continue").length, 1);
});

test("managerLoopRestore and managerLoopRename reach the gated client", async () => {
  const harness = loadHostSlice();
  harness.setClient(makeClient());
  const view = makeView();

  harness.api.handleInboundMessage(view, { type: "managerLoopRestore" });
  harness.api.handleInboundMessage(view, { type: "managerLoopRename", sessionId: "mls-aaaa1111bbbb2222", title: "Morning" });
  harness.api.handleInboundMessage(view, { type: "managerLoopRename", sessionId: "nope", title: "Morning" });
  await flush();

  const gatedClient = harness.managerLoopClientInstances[0];
  assert.deepEqual(plain(gatedClient.calls.find((call) => call.name === "aiworkhub_manager_loop_restore").args), {});
  assert.deepEqual(plain(gatedClient.calls.find((call) => call.name === "aiworkhub_manager_loop_rename").args), {
    session_id: "mls-aaaa1111bbbb2222",
    title: "Morning",
  });
  assert.equal(gatedClient.calls.filter((call) => call.name === "aiworkhub_manager_loop_rename").length, 1);
});

test("managerLoopClose disposes the gated client; the next managerLoopStart spawns a fresh one", async () => {
  const harness = loadHostSlice();
  harness.setClient(makeClient());
  const view = makeView();

  harness.api.handleInboundMessage(view, { type: "managerLoopStart", backendId: "claude_cli", model: "opus" });
  await flush();
  assert.equal(harness.managerLoopClientInstances.length, 1);
  const firstClient = harness.managerLoopClientInstances[0];
  assert.equal(firstClient.terminated, false);

  harness.api.handleInboundMessage(view, { type: "managerLoopClose" });
  await flush();
  assert.equal(firstClient.terminated, true);

  harness.api.handleInboundMessage(view, { type: "managerLoopStart", backendId: "claude_cli", model: "opus" });
  await flush();
  assert.equal(harness.managerLoopClientInstances.length, 2);
  assert.notEqual(harness.managerLoopClientInstances[1], firstClient);
});

test("the gated manager loop client is torn down from all three required call sites", () => {
  // 1 definition (async function disposeManagerLoopMcpClient() {) plus 3
  // call sites: runManagerLoopAction's close branch, ViewState.dispose(),
  // and deactivate(). A regression dropping any of the three call sites
  // changes this count.
  const occurrences = extensionSource.split("disposeManagerLoopMcpClient()").length - 1;
  assert.equal(occurrences, 4);
});

test("managerLoopStart refuses an unlisted backend without ever calling an MCP tool", async () => {
  const harness = loadHostSlice();
  const client = makeClient();
  harness.setClient(client);
  const view = makeView();

  harness.api.handleInboundMessage(view, { type: "managerLoopStart", backendId: "not_a_real_backend", model: "m" });
  await flush();

  assert.equal(client.calls.length, 0);
  assert.deepEqual(plain(view.posts), [{ type: harness.api.OUTBOUND_TYPES.error, message: "invalid_backend_id" }]);
});

test("managerLoopEvents with a malformed session id is dropped before any MCP call", async () => {
  const harness = loadHostSlice();
  const client = makeClient();
  harness.setClient(client);
  const view = makeView();

  harness.api.handleInboundMessage(view, { type: "managerLoopEvents", sessionId: "../../etc/passwd", afterSeq: 0 });
  await flush();

  assert.equal(client.calls.length, 0);
  assert.deepEqual(plain(view.posts), []);
});

// ── media/app.js: transcript rendering, textContent-only, and after_seq polling ──

function makeFakeElement(tag) {
  const element = {
    tag,
    className: "",
    textContent: "",
    hidden: false,
    disabled: false,
    value: "",
    open: false,
    showModalCalled: 0,
    attrs: {},
    children: [],
    listeners: {},
    classList: { toggle() {} },
    scrollTop: 0,
    scrollHeight: 0,
    setAttribute(name, value) {
      this.attrs[name] = String(value);
    },
    appendChild(child) {
      this.children.push(child);
      return child;
    },
    append(...nodes) {
      this.children.push(...nodes);
    },
    replaceChildren(...nodes) {
      if (nodes.length === 1 && nodes[0] && nodes[0].__isFragment) {
        this.children = nodes[0].children.slice();
      } else {
        this.children = nodes;
      }
    },
    addEventListener(type, handler) {
      (this.listeners[type] = this.listeners[type] || []).push(handler);
    },
    show() {
      this.open = true;
    },
    showModal() {
      this.open = true;
      this.showModalCalled += 1;
    },
    close() {
      this.open = false;
      const handlers = this.listeners.close || [];
      for (const handler of handlers) handler({});
    },
  };
  return element;
}

// Simulates a DOM event dispatch against the fake element harness: runs every
// handler registered via addEventListener(type, ...), in registration order.
function trigger(element, type, eventOverrides = {}) {
  const handlers = element.listeners[type] || [];
  const event = { preventDefault() {}, target: element, ...eventOverrides };
  for (const handler of handlers) handler(event);
}

function flattenNodes(node, out) {
  out.push(node);
  for (const child of node.children || []) {
    flattenNodes(child, out);
  }
  return out;
}

function loadWebviewSlice() {
  const utilities =
    extractSlice(appSource, "function createElement(tag, className, text) {", "return element;\n}", "createElement") +
    "\n" +
    extractSlice(appSource, "function asArray(value) {", "return Array.isArray(value) ? value : [];\n}", "asArray") +
    "\n" +
    extractSlice(
      appSource,
      "function numberValue(value) {",
      "return Number.isFinite(parsed) ? parsed : 0;\n}",
      "numberValue",
    ) +
    "\n" +
    extractSlice(
      appSource,
      "function limitText(value, maxLength = 120) {",
      "return `${text.slice(0, Math.max(0, maxLength - 1)).trimEnd()}...`;\n}",
      "limitText",
    );
  const constants = extractSlice(
    appSource,
    "const MANAGER_CHAT_BACKENDS = new Set([",
    "]);",
    "MANAGER_CHAT_BACKENDS",
  );
  const managerChat = extractSlice(
    appSource,
    "function managerChatTaskStatus(task) {",
    "  showManagerChatNotice(null);\n}",
    "manager chat render/poll functions",
  );
  const managerChatWiring = extractSlice(
    appSource,
    "function applyManagerChatCollapsed(collapsed) {",
    "  state.managerChatRunning = true;\n  applyManagerChatSessionUi();\n});",
    "manager chat sidebar wiring",
  );

  const state = {
    featureSettings: null,
    managerChatSession: null,
    managerChatBackend: null,
    managerChatModel: null,
    managerChatModelByBackend: {},
    managerChatRunning: false,
    managerChatEvents: [],
    managerChatLastSeq: 0,
    managerChatPollTimer: null,
  };
  const elements = {
    headerManagerChat: makeFakeElement("button"),
    managerChatDialog: Object.assign(makeFakeElement("dialog"), { open: true }),
    managerChatTranscript: makeFakeElement("div"),
    managerChatLatest: makeFakeElement("button"),
    managerChatAnnouncer: makeFakeElement("div"),
    managerChatNotice: makeFakeElement("div"),
    managerChatSummary: makeFakeElement("span"),
    managerChatBackendSelect: makeFakeElement("select"),
    managerChatModelInput: makeFakeElement("select"),
    managerChatStart: makeFakeElement("button"),
    managerChatRotate: makeFakeElement("button"),
    managerChatClose: makeFakeElement("button"),
    managerChatStatus: makeFakeElement("span"),
    managerChatStatusLabel: makeFakeElement("span"),
    managerChatSessionLine: makeFakeElement("div"),
    managerChatSessionSelect: makeFakeElement("select"),
    managerChatRenameSession: makeFakeElement("button"),
    managerChatNewSession: makeFakeElement("button"),
    managerChatSessionId: makeFakeElement("span"),
    managerChatSessionBackend: makeFakeElement("span"),
    managerChatComposer: makeFakeElement("form"),
    managerChatTasksHead: makeFakeElement("div"),
    managerChatTasksList: makeFakeElement("div"),
    managerChatTaskFilter: Object.assign(makeFakeElement("select"), { value: "open" }),
    managerChatInput: makeFakeElement("textarea"),
    managerChatSend: makeFakeElement("button"),
    headerManagerChatValue: makeFakeElement("strong"),
    headerManagerChatDetail: makeFakeElement("span"),
  };
  const posts = [];
  const timers = [];
  const vscode = { postMessage: (message) => posts.push(message) };
  const windowFake = {
    requestAnimationFrame: (fn) => { fn(); return 1; },
    setTimeout: (fn) => {
      const id = timers.length + 1;
      timers.push({ id, fn });
      return id;
    },
    clearTimeout: (id) => {
      const index = timers.findIndex((timer) => timer.id === id);
      if (index !== -1) timers.splice(index, 1);
    },
    prompt: () => "",
    confirm: () => true,
  };

  const context = {
    document: {
      createElement: (tag) => makeFakeElement(tag),
      createTextNode: (text) => ({ nodeType: 3, textContent: String(text) }),
      createDocumentFragment: () => ({ __isFragment: true, children: [], appendChild(child) { this.children.push(child); return child; } }),
    },
    window: windowFake,
    vscode,
    state,
    elements,
    MANAGER_CHAT_POLL_MS: 1500,
  };
  vm.createContext(context);
  vm.runInContext(
    `"use strict";\n${utilities}\n${constants}\n${consoleSource}\n${managerChat}\n${managerChatWiring}\n` +
      "this.api = { managerChatEventNode, renderManagerChatEvents, renderManagerChatEventsResponse, " +
      "renderManagerChatAction, renderManagerChatStatus, requestManagerChatEvents, scheduleManagerChatPoll, " +
      "applyManagerChatSessionUi, applyManagerChatComposerState, managerChatEnabledModels, managerChatSelectedRoute, populateManagerChatModelOptions, applyManagerChatCollapsed, toggleManagerChatSidebar, startManagerChatSidebar, renderManagerChatTaskBoard, driveManagerChatTask };",
    context,
  );
  return { api: context.api, state, elements, posts, timers, window: windowFake };
}

test("manager chat turn_end shows reported token counts", () => {
  const view = loadWebviewSlice();
  const node = view.api.managerChatEventNode({
    type: "turn_end",
    payload: { usage: { input_tokens: 1234, output_tokens: 567 } },
  });
  assert.ok(node, "turn_end should render a node");
  const text = flattenNodes(node, []).map((part) => String(part.textContent || "")).join("");
  assert.match(text, /^turn · 0 tools/);
  assert.match(text, /in 1\.2k/);
  assert.match(text, /out 567/);
});

test("manager chat turn_end without usage renders the neutral marker", () => {
  const view = loadWebviewSlice();
  const node = view.api.managerChatEventNode({ type: "turn_end", payload: {} });
  assert.ok(node, "turn_end without usage should still render");
  const text = flattenNodes(node, []).map((part) => String(part.textContent || "")).join("");
  assert.equal(text, "turn · 0 tools");
});

test("manager chat turn_end names its turn and tool-call count", () => {
  const harness = loadWebviewSlice();
  harness.state.managerChatEvents = [
    { seq: 1, turn: 4, type: "tool_call", payload: { name: "read" } },
    { seq: 2, turn: 4, type: "tool_call", payload: { name: "grep" } },
    { seq: 3, turn: 3, type: "tool_call", payload: { name: "old" } },
  ];
  const node = harness.api.managerChatEventNode({ type: "turn_end", turn: 4, payload: {} });
  const text = flattenNodes(node, []).map((part) => String(part.textContent || "")).join("");
  assert.equal(text, "turn 4 · 2 tools");
});

test("manager chat reasoning renders as inert collapsible text", () => {
  const harness = loadWebviewSlice();
  const hostile = "thinking: <img src=x onerror=alert(1)>";
  const node = harness.api.managerChatEventNode({ type: "reasoning", payload: { text: hostile } });
  assert.ok(node, "reasoning should render a node");
  const allNodes = flattenNodes(node, []);
  assert.ok(!allNodes.some((item) => item.tag === "img"), "no element from payload");
  const text = allNodes.map((item) => String(item.textContent || "")).join("");
  assert.ok(text.includes("thinking:"), "reasoning text preserved");
  assert.ok(text.includes("<img"), "payload kept verbatim as inert text");
  assert.ok(allNodes.some((item) => item.tag === "details"), "reasoning collapses like tool rows");
});

test("manager chat turn_end hostile usage payload stays neutral", () => {
  const view = loadWebviewSlice();
  const node = view.api.managerChatEventNode({
    type: "turn_end",
    payload: {
      usage: {
        input_tokens: "<img src=x onerror=alert(1)>",
        output_tokens: Number.NaN,
        total_tokens: -12,
        cache_read_input_tokens: "34",
        cache_creation_input_tokens: { evil: true },
      },
    },
  });
  assert.ok(node, "hostile usage should still render a node");
  const text = flattenNodes(node, []).map((part) => String(part.textContent || "")).join("");
  assert.equal(text, "turn · 0 tools");
  assert.ok(!text.includes("<img"), "hostile markup must never reach text content");
  assert.ok(!text.includes("Tokens:"), "no synthetic counts may be reported");
});

test("a hostile event payload renders as inert text, never as a parsed element", () => {
  const harness = loadWebviewSlice();
  const hostile = '<img src=x onerror=alert(1)>';
  const node = harness.api.managerChatEventNode({ type: "assistant_text", turn: 1, payload: { text: hostile } });

  const allNodes = flattenNodes(node, []);
  assert.ok(!allNodes.some((item) => item.tag === "img"), "no <img> element was ever created from the payload");
  const textNode = allNodes.find((item) => item.nodeType === 3);
  assert.ok(textNode, "the hostile string must be carried by a text node, never assigned via innerHTML");
  assert.equal(textNode.textContent, hostile, "the payload is preserved verbatim as inert text");
});

test("the transcript renders assistant text, tool rows and callback wake-ups, and accumulates after_seq", () => {
  const harness = loadWebviewSlice();
  harness.state.managerChatSession = "mls-aaaa1111bbbb2222";

  harness.api.renderManagerChatEventsResponse({
    ok: true,
    events: [
      { seq: 1, type: "session_start", turn: 0, payload: {} },
      { seq: 2, type: "assistant_text", turn: 1, payload: { text: "hi there" } },
      { seq: 3, type: "tool_call", turn: 1, payload: { name: "read_file", input: { path: "a.py" } } },
      { seq: 4, type: "callback", turn: 2, payload: { text: "callback: T-1 -> review", task_id: "T-1" } },
    ],
  });

  assert.equal(harness.state.managerChatLastSeq, 4);
  const rendered = harness.elements.managerChatTranscript.children;
  assert.equal(rendered.length, 4);
  assert.match(flattenNodes(rendered[1], []).map((c) => c.textContent || "").join(""), /hi there/);
  assert.match(rendered[2].children[1].children[0].textContent, /read_file/);
  assert.match(rendered[3].children[1].textContent, /Automatic wake-up/);
});

test("a terminal batch unlocks the composer immediately, status confirms after", () => {
  const harness = loadWebviewSlice();
  harness.state.managerChatSession = "mls-aaaa1111bbbb2222";
  harness.state.managerChatRunning = true;
  harness.elements.managerChatSend.disabled = true;

  harness.api.renderManagerChatEventsResponse({
    ok: true,
    events: [{ seq: 9, type: "turn_end", turn: 2, payload: {} }],
  });

  assert.equal(harness.state.managerChatRunning, false);
  assert.equal(harness.elements.managerChatSend.disabled, false);
  assert.ok(harness.posts.some((post) => post.type === "managerLoopStatus"));
});

test("polling requests the next batch using the after_seq of the last rendered event", () => {
  const harness = loadWebviewSlice();
  harness.state.managerChatSession = "mls-aaaa1111bbbb2222";

  harness.api.renderManagerChatEventsResponse({
    ok: true,
    events: [{ seq: 7, type: "assistant_text", turn: 1, payload: { text: "ok" } }],
  });

  assert.equal(harness.timers.length, 1, "one poll timer is armed after a successful events response");
  harness.timers[0].fn();
  assert.deepEqual(plain(harness.posts.at(-1)), {
    type: "managerLoopEvents",
    sessionId: "mls-aaaa1111bbbb2222",
    afterSeq: 7,
  });
});

test("a turn_end batch while running pulls one status refresh to unlock the composer", () => {
  const harness = loadWebviewSlice();
  harness.state.managerChatSession = "mls-aaaa1111bbbb2222";
  harness.state.managerChatRunning = true;

  harness.api.renderManagerChatEventsResponse({
    ok: true,
    events: [{ seq: 9, type: "turn_end", turn: 2, payload: {} }],
  });

  assert.deepEqual(plain(harness.posts.at(-1)), { type: "managerLoopStatus" });
});

test("a failed turn without turn_end still pulls one status refresh", () => {
  const harness = loadWebviewSlice();
  harness.state.managerChatSession = "mls-aaaa1111bbbb2222";
  harness.state.managerChatRunning = true;

  harness.api.renderManagerChatEventsResponse({
    ok: true,
    events: [{ seq: 9, type: "error", turn: 2, payload: { error: "x" } }],
  });

  assert.deepEqual(plain(harness.posts.at(-1)), { type: "managerLoopStatus" });
});

test("no status pull when idle or when the batch has no terminal event", () => {
  const harness = loadWebviewSlice();
  harness.state.managerChatSession = "mls-aaaa1111bbbb2222";
  harness.state.managerChatRunning = false;
  const statusPosts = () => harness.posts.filter((post) => post.type === "managerLoopStatus");

  harness.api.renderManagerChatEventsResponse({
    ok: true,
    events: [{ seq: 9, type: "turn_end", turn: 2, payload: {} }],
  });
  assert.equal(statusPosts().length, 0, "idle panel never polls status");
  harness.state.managerChatRunning = true;
  harness.api.renderManagerChatEventsResponse({
    ok: true,
    events: [{ seq: 10, type: "assistant_text", turn: 3, payload: { text: "hi" } }],
  });

  assert.equal(statusPosts().length, 0, "mid-turn batches never poll status");
});

test("consecutive same-turn turn_end markers collapse to one completion row", () => {
  const harness = loadWebviewSlice();
  harness.state.managerChatSession = "mls-aaaa1111bbbb2222";
  harness.state.managerChatEvents = [
    { seq: 1, turn: 1, type: "user_message", payload: { text: "hi" } },
    { seq: 2, turn: 1, type: "turn_end", payload: {} },
    { seq: 3, turn: 1, type: "turn_end", payload: { usage: { input_tokens: 10 } } },
    { seq: 4, turn: 1, type: "turn_end", payload: {} },
  ];

  harness.api.renderManagerChatEvents();

  const markers = harness.elements.managerChatTranscript.children.filter(
    (child) => flattenNodes(child, []).some((node) => node.className === "mc-footer")
  );
  assert.equal(markers.length, 1);
});

test("an events reply that overlaps one already rendered appends nothing twice", () => {
  const harness = loadWebviewSlice();
  harness.state.managerChatSession = "mls-aaaa1111bbbb2222";
  const reply = {
    ok: true,
    events: [{ seq: 1, type: "user_message", turn: 1, payload: { text: "hello" } }],
  };

  harness.api.renderManagerChatEventsResponse(reply);
  harness.api.renderManagerChatEventsResponse(reply);

  assert.equal(harness.state.managerChatEvents.length, 1);
  assert.equal(harness.elements.managerChatTranscript.children.length, 1);
});

test("a manager_turn_in_progress reply shows a notice and sends nothing else", () => {
  const harness = loadWebviewSlice();

  harness.api.renderManagerChatAction("send", { ok: false, error: "manager_turn_in_progress" });

  assert.equal(harness.elements.managerChatNotice.hidden, false);
  assert.match(harness.elements.managerChatNotice.textContent, /already running/i);
  assert.equal(harness.posts.length, 0, "the busy reply must never trigger a resend");
});

test("the composer stays enabled with no session and while a turn is running", () => {
  const harness = loadWebviewSlice();
  assert.equal(harness.state.managerChatSession, null);

  harness.api.applyManagerChatComposerState();

  assert.equal(harness.elements.managerChatInput.disabled, false);
  assert.equal(harness.elements.managerChatSend.disabled, false);

  harness.state.managerChatRunning = true;
  harness.api.applyManagerChatComposerState();

  assert.equal(harness.elements.managerChatInput.disabled, false, "a running turn must not lock the composer");
  assert.equal(harness.elements.managerChatSend.disabled, false);
});

test("submitting the composer posts the picker route with the text", () => {
  const harness = loadWebviewSlice();
  harness.elements.managerChatBackendSelect.value = "codex_cli";
  harness.elements.managerChatModelInput.value = "gpt-x";
  harness.elements.managerChatInput.value = "hello";
  harness.elements.managerChatSend.disabled = false;

  trigger(harness.elements.managerChatComposer, "submit");

  assert.deepEqual(plain(harness.posts.at(-1)), {
    type: "managerLoopSend",
    text: "hello",
    backendId: "codex_cli",
    model: "gpt-x",
    reasoning: "",
  });
});

test("submitting while a turn is running still sends and leaves the composer enabled", () => {
  const harness = loadWebviewSlice();
  harness.state.managerChatRunning = true;
  harness.api.applyManagerChatComposerState();
  harness.elements.managerChatBackendSelect.value = "codex_cli";
  harness.elements.managerChatModelInput.value = "gpt-x";
  harness.elements.managerChatInput.value = "next";

  trigger(harness.elements.managerChatComposer, "submit");
  assert.deepEqual(plain(harness.posts.at(-1)), {
    type: "managerLoopSend",
    text: "next",
    backendId: "codex_cli",
    model: "gpt-x",
    reasoning: "",
  });
  assert.equal(harness.elements.managerChatInput.disabled, false);
  assert.equal(harness.elements.managerChatSend.disabled, false);
});

test("queued sends render from status.send_queue and disappear when drained", () => {
  const harness = loadWebviewSlice();
  const hostile = "<img src=x onerror=alert(1)>";
  const longModel = "claude-opus-4-1-very-long-model-name-that-must-not-be-clipped";
  harness.api.renderManagerChatStatus({
    ok: true,
    running: true,
    session: { session_id: "mls-aaaa1111bbbb2222", backend_id: "claude_cli", model: longModel },
    send_queue: [
      { text: hostile, model: longModel },
      "second send",
    ],
  });

  const host = harness.elements.managerChatQueue;
  assert.ok(host, "queue host is created from the status payload");
  assert.equal(host.hidden, false);
  assert.equal(host.children.length, 2);
  const nodes = flattenNodes(host, []);
  const text = nodes.map((part) => String(part.textContent || "")).join("");
  assert.ok(text.includes(hostile), "queued text is preserved");
  assert.ok(text.includes(longModel), "queued model text is not clipped");
  assert.ok(text.includes("second send"));
  assert.ok(!nodes.some((item) => item.tag === "img"), "hostile queue HTML must not become an element");
  assert.ok(
    nodes.some((item) => item.nodeType === 3 && item.textContent === hostile),
    "hostile queue text stays a text node",
  );

  harness.api.renderManagerChatStatus({
    ok: true,
    running: false,
    session: { session_id: "mls-aaaa1111bbbb2222", backend_id: "claude_cli", model: longModel },
    send_queue: [],
  });

  assert.equal(host.hidden, true);
  assert.equal(host.children.length, 0, "a drained send_queue removes the rows");
});

test("manager chat tool and reasoning text is not clipped and hostile HTML stays literal", () => {
  const harness = loadWebviewSlice();
  const toolBody = "x".repeat(400);
  const reasoning = `${"y".repeat(800)}<img src=x onerror=alert(1)>`;
  const toolNode = harness.api.managerChatEventNode({
    type: "tool_call",
    payload: { name: "read_file", input: toolBody },
  });
  const toolText = flattenNodes(toolNode, []).map((part) => String(part.textContent || "")).join("");
  assert.ok(toolText.includes(toolBody), "tool payload is not truncated");
  assert.equal(toolText.includes("..."), false, "tool payload must not be ellipsized");
  const reasonNodes = flattenNodes(harness.api.managerChatEventNode({
    type: "reasoning",
    payload: { text: reasoning },
  }), []);
  const reasonText = reasonNodes.map((part) => String(part.textContent || "")).join("");
  assert.ok(reasonText.includes(reasoning), "model reasoning is not truncated");
  assert.ok(!reasonNodes.some((item) => item.tag === "img"));
  assert.ok(reasonNodes.some((item) => item.nodeType === 3 && String(item.textContent || "").includes("<img")));
});

test("a running turn names the latest streamed action on the status line", () => {
  const harness = loadWebviewSlice();
  harness.state.managerChatSession = "mls-aaaa1111bbbb2222";
  harness.state.managerChatRunning = true;
  harness.state.managerChatEvents = [
    { seq: 1, type: "tool_call", payload: { name: "read" } },
  ];

  harness.api.applyManagerChatSessionUi();

  assert.match(harness.elements.managerChatStatusLabel.textContent, /Running · read/);
});

test("tool calls render as fields for every model, not as a JSON blob", () => {
  const harness = loadWebviewSlice();
  const node = harness.api.managerChatEventNode({
    type: "tool_call",
    payload: { name: "read_file", input: { path: "a.py" } },
  });
  const text = flattenNodes(node, []).map((part) => String(part.textContent || "")).join("\n");
  assert.match(text, /read_file/);
  assert.match(text, /a\.py/);
  assert.equal(text.includes('{\"input\"'), false);
});

test("a running turn shows a thinking timer for any model", () => {
  const harness = loadWebviewSlice();
  harness.state.managerChatSession = "mls-aaaa1111bbbb2222";
  harness.state.managerChatRunning = true;
  harness.state.managerChatEvents = [];
  harness.api.renderManagerChatEvents();
  const text = flattenNodes(harness.elements.managerChatTranscript, []).map((part) => String(part.textContent || "")).join("");
  assert.match(text, /Thinking/);
});

test("polling continues when the manager dialog is closed", () => {
  const harness = loadWebviewSlice();
  harness.state.managerChatSession = "mls-aaaa1111bbbb2222";
  harness.elements.managerChatDialog.open = false;

  harness.api.renderManagerChatEventsResponse({
    ok: true,
    events: [{ seq: 3, type: "assistant_text", turn: 1, payload: { text: "ok" } }],
  });

  assert.equal(harness.timers.length, 1, "a closed dialog still arms the session poll");
  harness.timers[0].fn();
  assert.deepEqual(plain(harness.posts.at(-1)), {
    type: "managerLoopEvents",
    sessionId: "mls-aaaa1111bbbb2222",
    afterSeq: 3,
  });
});

test("a status reply requests events even when the dialog is closed", () => {
  const harness = loadWebviewSlice();
  harness.elements.managerChatDialog.open = false;

  harness.api.renderManagerChatStatus({
    ok: true,
    running: false,
    session: { session_id: "mls-aaaa1111bbbb2222", backend_id: "codex_cli", model: "gpt-x" },
    send_queue: [],
  });

  assert.deepEqual(plain(harness.posts.find((post) => post.type === "managerLoopEvents")), {
    type: "managerLoopEvents",
    sessionId: "mls-aaaa1111bbbb2222",
    afterSeq: 0,
  });
});

test("closing the manager dialog does not stop session polling", () => {
  const harness = loadWebviewSlice();
  harness.state.managerChatSession = "mls-aaaa1111bbbb2222";
  harness.elements.managerChatDialog.open = true;
  harness.api.scheduleManagerChatPoll();
  assert.equal(harness.timers.length, 1);

  harness.elements.managerChatDialog.close();

  assert.equal(harness.elements.managerChatDialog.open, false);
  assert.equal(harness.timers.length, 1, "close reschedules instead of dropping the chain");
  harness.timers[0].fn();
  assert.deepEqual(plain(harness.posts.at(-1)), {
    type: "managerLoopEvents",
    sessionId: "mls-aaaa1111bbbb2222",
    afterSeq: 0,
  });
});

test("the picker stays on the owner's choice and is not overwritten by the session route", () => {
  const harness = loadWebviewSlice();
  harness.state.featureSettings = MANAGER_CHAT_MODEL_POLICY_PAYLOAD;
  harness.state.managerChatSession = "mls-aaaa1111bbbb2222";
  harness.state.managerChatBackend = "codex_cli";
  harness.state.managerChatModel = "gpt-5-codex";
  for (const backend of ["claude_cli", "codex_cli"]) {
    harness.elements.managerChatBackendSelect.appendChild({ value: backend, textContent: backend });
  }
  harness.elements.managerChatBackendSelect.value = "claude_cli";
  harness.elements.managerChatModelInput.value = "claude-sonnet-5";

  harness.api.applyManagerChatSessionUi();

  assert.equal(harness.elements.managerChatBackendSelect.disabled, false);
  assert.equal(harness.elements.managerChatModelInput.disabled, false);
  assert.equal(harness.elements.managerChatBackendSelect.value, "claude_cli");
  assert.equal(harness.elements.managerChatModelInput.value, "claude-sonnet-5");
});

// ── media/app.js: model picker sourced from the repository's enabled models ─

const MANAGER_CHAT_MODEL_POLICY_PAYLOAD = {
  ok: true,
  revision: 3,
  model_policy: {
    ok: true,
    revision: 3,
    catalog: {
      workers: [
        { provider: "anthropic", adapter: "claude_cli", model: "claude-opus-4-1", worker_id: "w1", effective_enabled: true, catalog_enabled: true },
        { provider: "anthropic", adapter: "claude_cli", model: "claude-sonnet-5", worker_id: "w2", effective_enabled: true, catalog_enabled: true },
        { provider: "anthropic", adapter: "claude_cli", model: "claude-haiku-4-5", worker_id: "w3", effective_enabled: false, catalog_enabled: true },
        { provider: "openai", adapter: "codex_cli", model: "gpt-5-codex", worker_id: "w4", effective_enabled: true, catalog_enabled: true },
      ],
    },
    providers: {},
  },
};

test("the model select lists every enabled model and remembers which transport reaches it", () => {
  const harness = loadWebviewSlice();
  harness.state.featureSettings = MANAGER_CHAT_MODEL_POLICY_PAYLOAD;

  harness.api.populateManagerChatModelOptions();

  const options = harness.elements.managerChatModelInput.children;
  assert.deepEqual(options.map((option) => option.value), ["claude-opus-4-1", "claude-sonnet-5", "gpt-5-codex"]);
  assert.equal(options.find((option) => option.value === "gpt-5-codex").dataset.backendId, "codex_cli");
  assert.equal(harness.elements.managerChatModelInput.value, "claude-opus-4-1");
});

test("a discovered row's label is shown as the option text while the value stays the model that gets launched", () => {
  const harness = loadWebviewSlice();
  harness.state.featureSettings = {
    ok: true,
    revision: 1,
    model_policy: {
      ok: true,
      revision: 1,
      catalog: {
        workers: [
          { provider: "anthropic", adapter: "claude_cli", model: "opus", worker_id: "claude-opus-5", effective_enabled: true, catalog_enabled: true, label: "claude-opus-5 (opus)" },
          { provider: "anthropic", adapter: "claude_cli", model: "fable", worker_id: "", effective_enabled: true, catalog_enabled: true, inventory_only: true, discovered_from_cli: true, label: "fable" },
        ],
      },
      providers: {},
    },
  };
  harness.elements.managerChatBackendSelect.value = "claude_cli";

  harness.api.populateManagerChatModelOptions();

  const options = harness.elements.managerChatModelInput.children;
  assert.deepEqual(options.map((option) => option.value), ["opus", "fable"], "the value sent on Send is always the launched alias");
  assert.equal(options[0].textContent, "claude-opus-5 (opus)", "the option shows the resolved version, not the bare alias");
  assert.equal(options[1].textContent, "fable", "a row with no resolved version yet falls back to its bare name");
  assert.equal(harness.elements.managerChatModelInput.value, "opus");
  harness.elements.managerChatInput.value = "continue";
  trigger(harness.elements.managerChatComposer, "submit");
  assert.deepEqual(plain(harness.posts.at(-1)), {
    type: "managerLoopSend",
    text: "continue",
    backendId: "claude_cli",
    model: "opus",
    reasoning: "",
  });
});

test("choosing a model keeps the full list and resolves that model's transport", () => {
  const harness = loadWebviewSlice();
  harness.state.featureSettings = MANAGER_CHAT_MODEL_POLICY_PAYLOAD;
  harness.api.populateManagerChatModelOptions();
  harness.elements.managerChatModelInput.value = "gpt-5-codex";

  const route = harness.api.managerChatSelectedRoute();

  assert.deepEqual(harness.elements.managerChatModelInput.children.map((option) => option.value), [
    "claude-opus-4-1",
    "claude-sonnet-5",
    "gpt-5-codex",
  ]);
  assert.equal(route.backendId, "codex_cli");
  assert.equal(route.model, "gpt-5-codex");
});

test("no enabled models shows a disabled hint and disables the model combo", () => {
  const harness = loadWebviewSlice();
  harness.state.featureSettings = {
    ok: true,
    model_policy: { ok: true, catalog: { workers: [] } },
  };

  harness.api.populateManagerChatModelOptions();

  const options = harness.elements.managerChatModelInput.children;
  assert.equal(options.length, 1);
  assert.equal(options[0].disabled, true);
  assert.match(options[0].textContent, /No enabled models/);
  assert.equal(harness.elements.managerChatModelInput.disabled, true);
});

test("Send continues the saved session with the exact backend and model chosen in the picker", () => {
  const harness = loadWebviewSlice();
  harness.state.featureSettings = MANAGER_CHAT_MODEL_POLICY_PAYLOAD;
  harness.elements.managerChatBackendSelect.value = "claude_cli";
  harness.api.populateManagerChatModelOptions();
  harness.elements.managerChatModelInput.value = "claude-sonnet-5";
  harness.elements.managerChatInput.value = "continue";
  harness.elements.managerChatInput.value = "continue";

  trigger(harness.elements.managerChatComposer, "submit");

  assert.deepEqual(plain(harness.posts.at(-1)), {
    type: "managerLoopSend",
    text: "continue",
    backendId: "claude_cli",
    model: "claude-sonnet-5",
    reasoning: "",
  });
});
test("Send is refused when no model is available", () => {
  const harness = loadWebviewSlice();
  harness.state.featureSettings = {
    ok: true,
    model_policy: { ok: true, catalog: { workers: [] } },
  };
  harness.api.populateManagerChatModelOptions();
  harness.elements.managerChatInput.value = "continue";

  trigger(harness.elements.managerChatComposer, "submit");

  assert.equal(harness.posts.some((post) => post.type === "managerLoopSend"), false, "no send is posted without a real model");
  assert.equal(harness.elements.managerChatNotice.hidden, false);
});

test("loading the sidebar lists every enabled model without opening a dialog", () => {
  const harness = loadWebviewSlice();
  harness.state.featureSettings = MANAGER_CHAT_MODEL_POLICY_PAYLOAD;

  harness.api.startManagerChatSidebar();

  assert.deepEqual(harness.elements.managerChatModelInput.children.map((option) => option.value), [
    "claude-opus-4-1",
    "claude-sonnet-5",
    "gpt-5-codex",
  ]);
  assert.equal(harness.elements.managerChatDialog.showModalCalled, 0, "sidebar load never opens a modal dialog");
});

test("opening the chat does not attach a saved session or create one", () => {
  const harness = loadWebviewSlice();
  harness.state.managerChatSession = "mls-aaaa1111bbbb2222";
  harness.state.managerChatEvents = [{ seq: 2, type: "user_message", payload: { text: "old" } }];

  harness.api.startManagerChatSidebar();

  assert.equal(harness.posts.some((post) => post.type === "managerLoopRestore"), false);
  assert.equal(harness.posts.some((post) => post.type === "managerLoopEnsure"), false);
  assert.deepEqual(plain(harness.posts.find((post) => post.type === "managerLoopStatus")), { type: "managerLoopStatus" });
  assert.equal(harness.state.managerChatSession, null);
  assert.equal(harness.state.managerChatEvents.length, 0);
  assert.equal(harness.elements.managerChatDialog.showModalCalled, 0);
});

test("a non-array option list still binds the selected model to its transport", () => {
  const harness = loadWebviewSlice();
  harness.state.featureSettings = MANAGER_CHAT_MODEL_POLICY_PAYLOAD;
  harness.api.populateManagerChatModelOptions();
  const select = harness.elements.managerChatModelInput;
  const listed = select.children.slice();
  const collection = { length: listed.length };
  listed.forEach((option, index) => {
    collection[index] = option;
  });
  select.children = collection;
  select.options = collection;
  select.selectedOptions = undefined;
  select.value = "gpt-5-codex";
  harness.elements.managerChatBackendSelect.value = "claude_cli";

  const route = harness.api.managerChatSelectedRoute();

  assert.equal(Array.isArray(select.children), false);
  assert.equal(route.backendId, "codex_cli");
  assert.equal(route.model, "gpt-5-codex");
});

test("the collapse toggle flips sidebar state and never opens a dialog", () => {
  const harness = loadWebviewSlice();
  harness.state.managerChatCollapsed = false;

  harness.api.toggleManagerChatSidebar();

  assert.equal(harness.state.managerChatCollapsed, true);
  assert.equal(harness.elements.managerChatDialog.showModalCalled, 0, "never modal: the dashboard stays usable");
  harness.api.toggleManagerChatSidebar();

  assert.equal(harness.state.managerChatCollapsed, false);
  assert.equal(harness.elements.managerChatDialog.open, true, "collapse does not drive dialog.open");
});

test("the manager session shows canonical tasks and drives one from the chosen model", () => {
  const harness = loadWebviewSlice();
  harness.state.featureSettings = MANAGER_CHAT_MODEL_POLICY_PAYLOAD;
  harness.api.populateManagerChatModelOptions();
  harness.elements.managerChatModelInput.value = "gpt-5-codex";
  harness.state.tasks = [
    { task_id: "TASK_PENDING", status: "pending", objective: "wait" },
    { task_id: "TASK_RUN", status: "processing", title: "ship <img src=x>" },
  ];

  harness.api.renderManagerChatTaskBoard();

  const buttons = harness.elements.managerChatTasksList.children.filter((node) => node.tag === "button");
  assert.deepEqual(buttons.map((button) => button.dataset.taskId), ["TASK_RUN", "TASK_PENDING"]);
  const text = flattenNodes(harness.elements.managerChatTasksList, []).map((node) => String(node.textContent || "")).join("");
  assert.match(harness.elements.managerChatTasksHead.textContent, /2 · 1 running/);
  assert.ok(text.includes("<img"), "task text stays literal");
  assert.ok(!flattenNodes(harness.elements.managerChatTasksList, []).some((node) => node.tag === "img"));

  harness.api.driveManagerChatTask("TASK_RUN");

  assert.equal(harness.posts.at(-1).type, "managerLoopSend");
  assert.equal(harness.posts.at(-1).backendId, "codex_cli");
  assert.equal(harness.posts.at(-1).model, "gpt-5-codex");
  assert.match(harness.posts.at(-1).text, /TASK_RUN \(processing\)/);
});

test("the task filter hides closed cards and slash commands stay on this session", () => {
  const harness = loadWebviewSlice();
  harness.state.featureSettings = MANAGER_CHAT_MODEL_POLICY_PAYLOAD;
  harness.elements.managerChatBackendSelect.value = "codex_cli";
  harness.api.populateManagerChatModelOptions();
  harness.state.tasks = [
    { task_id: "TASK_RUN", status: "processing", title: "ship" },
    { task_id: "TASK_DONE", status: "accepted", title: "landed" },
  ];

  harness.api.renderManagerChatTaskBoard();
  let buttons = harness.elements.managerChatTasksList.children.filter((node) => node.tag === "button");
  assert.deepEqual(buttons.map((button) => button.dataset.taskId), ["TASK_RUN"]);
  assert.equal(buttons[0].children[0].textContent, "inspect");

  harness.elements.managerChatTaskFilter.value = "all";
  trigger(harness.elements.managerChatTaskFilter, "change");
  buttons = harness.elements.managerChatTasksList.children.filter((node) => node.tag === "button");
  assert.deepEqual(buttons.map((button) => button.dataset.taskId), ["TASK_RUN", "TASK_DONE"]);

  harness.elements.managerChatInput.value = "/task TASK_DONE";
  trigger(harness.elements.managerChatComposer, "submit");
  assert.match(harness.posts.at(-1).text, /drive canonical task TASK_DONE \(accepted\)/);
  assert.equal(harness.elements.managerChatInput.value, "");

  harness.elements.managerChatInput.value = "/new";
  trigger(harness.elements.managerChatComposer, "submit");
  assert.deepEqual(plain(harness.posts.at(-1)), { type: "managerLoopNew" });
});

test("the session picker lists saved conversations and continue/new stay on this session", () => {
  const harness = loadWebviewSlice();
  const hostile = "<img src=x onerror=alert(1)>";
  harness.api.renderManagerChatStatus({
    ok: true,
    running: false,
    session: { session_id: "mls-aaaa1111bbbb2222", backend_id: "codex_cli", model: "gpt-5-codex" },
    sessions: [
      { session_id: "mls-aaaa1111bbbb2222", status: "active", backend_id: "codex_cli", model: "gpt-5-codex", turn_count: 2 },
      { session_id: hostile, status: "closed", backend_id: "", model: "", turn_count: 1 },
      { session_id: "mls-bbbb2222cccc3333", status: "closed", backend_id: "", model: "", turn_count: 4 },
    ],
  });

  const select = harness.elements.managerChatSessionSelect;
  assert.equal(select.value, "mls-aaaa1111bbbb2222");
  assert.equal(harness.elements.managerChatSessionLine.hidden, false);
  const labels = select.children.map((option) => option.textContent).join("");
  assert.ok(labels.includes("<img"), "a long hostile id stays literal text, not an element");
  assert.ok(!select.children.some((option) => option.tag === "img"));

  select.value = "mls-bbbb2222cccc3333";
  trigger(select, "change");
  assert.deepEqual(plain(harness.posts.at(-1)), {
    type: "managerLoopContinue",
    sessionId: "mls-bbbb2222cccc3333",
  });

  trigger(harness.elements.managerChatNewSession, "click");
  assert.deepEqual(plain(harness.posts.at(-1)), { type: "managerLoopNew" });
});

test("the model field is a <select>, not a free-text input", () => {
  assert.match(extensionSource, /<select id="manager-chat-model" class="compact-select" aria-label="Model for this session"><\/select>/);
  assert.doesNotMatch(extensionSource, /<input id="manager-chat-model"/);
});

// ── Structural reuse: sidebar beside the dashboard, no covering dialog ──

test("the Manager panel is a persistent sidebar and reuses existing theme tokens", () => {
  assert.doesNotMatch(extensionSource, /id="header-manager-chat/);
  assert.match(extensionSource, /id="manager-chat-collapse"[^>]*aria-controls="manager-chat-sidebar"/);
  assert.match(extensionSource, /<div class="dashboard-shell" id="dashboard-shell">/);
  assert.match(extensionSource, /<div class="dashboard-column" id="dashboard-column">/);
  assert.match(extensionSource, /<aside class="manager-chat-sidebar" id="manager-chat-sidebar" aria-label="Manager chat">/);
  assert.match(extensionSource, /id="manager-chat-collapse"/);
  assert.doesNotMatch(extensionSource, /<dialog[^>]+id="manager-chat-dialog"/);
  assert.doesNotMatch(extensionSource, /data-close-dialog="manager-chat-dialog"/);
  assert.match(extensionSource, /<div class="needfix-toolbar">\s*<select id="manager-chat-backend"/);
  for (const id of [
    "manager-chat-summary",
    "manager-chat-session",
    "manager-chat-rename-session",
    "manager-chat-delete-session",
    "manager-chat-new-session",
    "manager-chat-backend",
    "manager-chat-model",
    "manager-chat-rotate",
    "manager-chat-status",
    "manager-chat-transcript",
    "manager-chat-transcript",
    "manager-chat-notice",
    "manager-chat-composer",
    "manager-chat-input",
    "manager-chat-send",
  ]) {
    assert.match(extensionSource, new RegExp(`id="${id}"`), `${id} must stay`);
  }
  assert.doesNotMatch(extensionSource, /id="manager-chat-tasks"/);
  assert.doesNotMatch(extensionSource, /id="manager-chat-task-filter"/);
  const shellAt = extensionSource.indexOf('id="dashboard-shell"');
  const columnAt = extensionSource.indexOf('id="dashboard-column"');
  const mainEnd = extensionSource.indexOf("</main>");
  const asideAt = extensionSource.indexOf('id="manager-chat-sidebar"');
  assert.ok(shellAt !== -1 && shellAt < columnAt && columnAt < mainEnd && mainEnd < asideAt, "sidebar sits beside the dashboard column");
  assert.doesNotMatch(appSource, /managerChatDialog\.show\(/);
  assert.doesNotMatch(extensionSource, /id="manager-chat-start"/);
  assert.doesNotMatch(extensionSource, /id="manager-chat-close"/);
  assert.doesNotMatch(appSource, /managerChatDialog\.showModal\(/, "the sidebar never opens a modal dialog");
  assert.match(appSource, /function startManagerChatSidebar\(/);
  assert.match(appSource, /function toggleManagerChatSidebar\(/);
  assert.match(cssSource, /\.dashboard-shell\s*\{[^}]*grid-template-columns:\s*minmax\(0,\s*1fr\)\s*clamp\(320px,\s*30vw,\s*520px\)/);
  assert.match(cssSource, /#manager-chat-sidebar\s*\{[^}]*position:\s*sticky/);
  assert.doesNotMatch(cssSource, /#manager-chat-sidebar\s*\{[^}]*position:\s*fixed/);
  assert.doesNotMatch(cssSource, /manager-chat-dialog/);
  assert.doesNotMatch(cssSource, /\.diagnostic-dialog\.manager-chat-dialog\s*\{[^}]*position:\s*fixed/);
  const stackStart = cssSource.indexOf("@media (max-width: 900px)");
  const stack = cssSource.slice(stackStart, stackStart + 900);
  assert.match(stack, /position:\s*static/);
  assert.match(stack, /flex-direction:\s*column/);
  assert.doesNotMatch(stack, /position:\s*fixed/);

  const managerCss = extractSlice(
    cssSource,
    ".manager-chat-frame {",
    ".manager-chat-composer .primary-button {\n  flex: 0 0 auto;\n  align-self: flex-end;\n}",
    "manager chat css block",
  );
  assert.doesNotMatch(
    managerCss,
    /#[0-9a-fA-F]{3,8}\b/,
    "manager chat CSS must reuse existing theme tokens, never a new colour literal",
  );
  assert.doesNotMatch(
    consoleCss,
    /#[0-9a-fA-F]{3,8}\b/,
    "manager console CSS must reuse existing theme tokens, never a new colour literal",
  );
});

test("manager chat model and tool text is not clipped by ellipsis or a fixed height", () => {
  const managerCss = extractSlice(
    cssSource,
    ".manager-chat-frame {",
    ".manager-chat-composer .primary-button {\n  flex: 0 0 auto;\n  align-self: flex-end;\n}",
    "manager chat css block",
  );
  assert.match(managerCss, /#manager-chat-model \{[^}]*field-sizing:\s*content/);
  assert.match(consoleCss, /\.manager-chat-tool-row > summary \{[^}]*overflow-wrap:\s*anywhere/);
  assert.match(consoleCss, /\.manager-chat-tool-row-body \{[^}]*overflow:\s*visible/);
  assert.match(consoleCss, /\.manager-chat-tool-row-body \{[^}]*max-height:\s*none/);
  for (const css of [managerCss, consoleCss]) {
    assert.doesNotMatch(css, /text-overflow:\s*ellipsis/);
    assert.doesNotMatch(css, /line-clamp/);
  }
});

test("manager console assets load before app.js and ship in the VSIX", () => {
  const html = extensionSource.slice(extensionSource.indexOf("function getHtmlForWebview("));
  const consoleScript = html.indexOf('src="${consoleScriptUri}"');
  const appScript = html.indexOf('src="${scriptUri}"');
  assert.ok(consoleScript !== -1 && consoleScript < appScript, "console script must load before app.js");
  assert.ok(html.indexOf('href="${styleUri}"') < html.indexOf('href="${consoleStyleUri}"'), "console css loads after app.css");
  const packager = fs.readFileSync(path.join(__dirname, "package-vsix.js"), "utf8");
  assert.match(packager, /"media\/manager_console\.js"/);
  assert.match(packager, /"media\/manager_console\.css"/);
  assert.doesNotMatch(appSource, /function managerChatEventNode\(/);
  assert.doesNotMatch(cssSource, /\.manager-chat-bubble \{/);
});

test("partial-only provider responses render without persisting or announcing deltas", () => {
  const harness = loadWebviewSlice();
  const session = "mls-" + "a".repeat(32);
  harness.state.managerChatSession = session;
  harness.state.managerChatEvents = [{ seq: 1, turn: 3, type: "user_message", payload: { text: "go" } }];
  harness.state.managerChatLastSeq = 1;
  const partial = { session_id: session, turn: 3, text: "Hel **world**", reasoning: "hm" };
  harness.api.renderManagerChatEventsResponse({ ok: true, session_id: session, events: [], partial });
  assert.equal(harness.state.managerChatPartial, partial);
  assert.equal(harness.state.managerChatEvents.length, 1);
  assert.equal(harness.state.managerChatLastSeq, 1);
  assert.equal(harness.elements.managerChatAnnouncer.textContent, "");
  const nodes = flattenNodes(harness.elements.managerChatTranscript, []);
  assert.ok(nodes.some((node) => node.className === "mc-caret"));
  assert.ok(nodes.some((node) => node.tag === "strong" && node.textContent === "world"));
  harness.api.renderManagerChatEventsResponse({
    ok: true, session_id: session, partial,
    events: [{ seq: 2, turn: 3, type: "assistant_text", payload: { text: "final" } }],
  });
  assert.equal(harness.state.managerChatPartial, null);
  assert.equal(harness.elements.managerChatAnnouncer.textContent, "final");
  assert.ok(!flattenNodes(harness.elements.managerChatTranscript, []).some((node) => node.className === "mc-caret"));
  harness.api.renderManagerChatEventsResponse({ ok: true, session_id: session, events: [], partial: null });
  assert.equal(harness.state.managerChatPartial, null);
});

test("session resets clear partial, render limit and announcements through actual call sites", () => {
  for (const reset of ["status-switch", "status-close", "action-switch", "sidebar"]) {
    const harness = loadWebviewSlice();
    harness.state.managerChatSession = "mls-" + "a".repeat(32);
    harness.state.managerChatPartial = { session_id: harness.state.managerChatSession, turn: 9, text: "old" };
    harness.state.managerChatEvents = [{ seq: 999, turn: 9, type: "assistant_text", payload: { text: "old" } }];
    harness.state.managerChatRenderLimit = 800;
    harness.state.managerChatAnnouncedSeq = 999;
    harness.elements.managerChatAnnouncer.textContent = "old";
    harness.api.renderManagerChatEvents();
    if (reset === "sidebar") harness.api.startManagerChatSidebar();
    else if (reset === "action-switch") harness.api.renderManagerChatAction("new", { ok: true, session_id: "mls-" + "b".repeat(32) });
    else harness.api.renderManagerChatStatus({ ok: true, running: false, session: reset === "status-close" ? null : { session_id: "mls-" + "b".repeat(32) } });
    assert.equal(harness.state.managerChatPartial, null, reset);
    assert.equal(harness.state.managerChatEvents.length, 0, reset);
    assert.equal(harness.state.managerChatRenderLimit, 400, reset);
    assert.equal(harness.state.managerChatAnnouncedSeq, 0, reset);
    assert.equal(harness.elements.managerChatAnnouncer.textContent, "", reset);
    assert.equal(harness.elements.managerChatLatest.hidden, true, reset);
    assert.ok(!flattenNodes(harness.elements.managerChatTranscript, []).some((node) => node.textContent === "old"), reset + " clears the old transcript immediately");
  }
});

test("stale provider replies cannot replace the active session partial or events", () => {
  const harness = loadWebviewSlice();
  const session = "mls-" + "b".repeat(32);
  harness.state.managerChatSession = session;
  harness.state.managerChatEvents = [{ seq: 1, turn: 4, type: "user_message", payload: { text: "go" } }];
  harness.state.managerChatLastSeq = 1;
  harness.api.renderManagerChatEventsResponse({ ok: true, session_id: session, events: [], partial: { session_id: session, turn: 4, text: "current" } });
  const current = harness.state.managerChatPartial;
  harness.api.renderManagerChatEventsResponse({ ok: true, session_id: "mls-" + "a".repeat(32), events: [{ seq: 9, turn: 1, type: "assistant_text", payload: { text: "old" } }], partial: { turn: 1, text: "old" } });
  assert.equal(harness.state.managerChatPartial, current);
  assert.equal(harness.state.managerChatLastSeq, 1);
  assert.equal(harness.state.managerChatEvents.length, 1);
  harness.api.renderManagerChatEventsResponse({ ok: true, session_id: session, events: [], partial: { session_id: session, turn: 3, text: "older turn" } });
  assert.equal(harness.state.managerChatPartial, null);
});

test("latest button resumes following and only the final announcer has aria-live", () => {
  const harness = loadWebviewSlice();
  const box = harness.elements.managerChatTranscript;
  box.scrollHeight = 2000;
  box.scrollTop = 100;
  harness.elements.managerChatLatest.hidden = false;
  harness.elements.managerChatLatest.listeners.click[0]();
  assert.equal(box.scrollTop, 2000);
  assert.equal(harness.elements.managerChatLatest.hidden, true);
  const markup = extensionSource.slice(extensionSource.indexOf('id="manager-chat-transcript"') - 40, extensionSource.indexOf('id="manager-chat-notice"'));
  assert.doesNotMatch(markup.split("</div>")[0], /aria-live/);
  assert.match(markup, /id="manager-chat-announcer" aria-live="polite"/);
  const persisted = appSource.slice(appSource.indexOf("function persistState()"), appSource.indexOf("function stopReadyRetry()"));
  assert.doesNotMatch(persisted, /managerChatPartial/);
});

test("the host binds successful and failed event replies to their requested session", async () => {
  const sessionId = "mls-" + "a".repeat(32);
  for (const failed of [false, true]) {
    const harness = loadHostSlice();
    harness.setClient(makeClient(() => { if (failed) throw new Error("failed"); return { ok: true, events: [], partial: null }; }));
    const view = makeView();
    harness.api.handleInboundMessage(view, { type: "managerLoopEvents", sessionId, afterSeq: 0 });
    await flush();
    assert.equal(view.posts.length, 1);
    assert.equal(view.posts[0].payload.session_id, sessionId);
    assert.equal(view.posts[0].payload.ok, !failed);
  }
});

test("five partial provider batches coalesce and render the latest text once", () => {
  const harness = loadWebviewSlice();
  const frames = [];
  harness.window.requestAnimationFrame = (fn) => { frames.push(fn); return frames.length; };
  harness.state.managerChatSession = "mls-" + "a".repeat(32);
  let writes = 0;
  const box = harness.elements.managerChatTranscript;
  const original = box.replaceChildren;
  box.replaceChildren = function (...nodes) { writes += 1; return original.apply(this, nodes); };
  for (let i = 0; i < 5; i += 1) {
    harness.api.renderManagerChatEventsResponse({ ok: true, session_id: harness.state.managerChatSession, events: [], partial: { session_id: harness.state.managerChatSession, turn: 1, text: "partial " + i } });
  }
  assert.equal(frames.length, 1);
  assert.equal(writes, 0);
  frames[0]();
  assert.equal(writes, 1);
  assert.ok(flattenNodes(box, []).some((node) => node.textContent === "partial 4"));
});
