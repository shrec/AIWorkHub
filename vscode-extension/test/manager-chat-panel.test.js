"use strict";

const assert = require("node:assert/strict");
// Objects built inside the vm-loaded extension and webview carry that realm's
// Object.prototype, so strict deep equality compares plain copies of them.
const plain = (value) => JSON.parse(JSON.stringify(value));
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const extensionSource = fs.readFileSync(path.join(__dirname, "..", "extension.js"), "utf8");
const appSource = fs.readFileSync(path.join(__dirname, "..", "media", "app.js"), "utf8");
const cssSource = fs.readFileSync(path.join(__dirname, "..", "media", "app.css"), "utf8");

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
  assert.deepEqual(plain(find("aiworkhub_manager_loop_send").args), { text: "hello manager", backend_id: "codex_cli", model: "gpt-x" });
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
    "function managerChatEventNode(event) {",
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
    managerChatSessionId: makeFakeElement("span"),
    managerChatSessionBackend: makeFakeElement("span"),
    managerChatComposer: makeFakeElement("form"),
    managerChatInput: makeFakeElement("textarea"),
    managerChatSend: makeFakeElement("button"),
    headerManagerChatValue: makeFakeElement("strong"),
    headerManagerChatDetail: makeFakeElement("span"),
  };
  const posts = [];
  const timers = [];
  const vscode = { postMessage: (message) => posts.push(message) };
  const windowFake = {
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
    `"use strict";\n${utilities}\n${constants}\n${managerChat}\n${managerChatWiring}\n` +
      "this.api = { managerChatEventNode, renderManagerChatEvents, renderManagerChatEventsResponse, " +
      "renderManagerChatAction, renderManagerChatStatus, requestManagerChatEvents, scheduleManagerChatPoll, " +
      "applyManagerChatSessionUi, applyManagerChatComposerState, managerChatEnabledModels, populateManagerChatModelOptions, applyManagerChatCollapsed, toggleManagerChatSidebar, startManagerChatSidebar, openManagerChatDialog };",
    context,
  );
  return { api: context.api, state, elements, posts, timers };
}

test("manager chat turn_end shows reported token counts", () => {
  const view = loadWebviewSlice();
  const node = view.api.managerChatEventNode({
    type: "turn_end",
    payload: { usage: { input_tokens: 1234, output_tokens: 567 } },
  });
  assert.ok(node, "turn_end should render a node");
  const text = flattenNodes(node, []).map((part) => String(part.textContent || "")).join("");
  assert.match(text, /Turn completed/);
  assert.match(text, /in 1,234/);
  assert.match(text, /out 567/);
});

test("manager chat turn_end without usage renders the neutral marker", () => {
  const view = loadWebviewSlice();
  const node = view.api.managerChatEventNode({ type: "turn_end", payload: {} });
  assert.ok(node, "turn_end without usage should still render");
  const text = flattenNodes(node, []).map((part) => String(part.textContent || "")).join("");
  assert.equal(text, "Turn completed · 0 tool calls");
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
  assert.equal(text, "Turn 4 completed · 2 tool calls");
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
  assert.equal(node.tag, "details", "reasoning collapses like tool rows");
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
  assert.equal(text, "Turn completed · 0 tool calls");
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
  assert.match(rendered[1].children.map((c) => c.textContent || "").join(""), /hi there/);
  assert.match(rendered[2].children[0].textContent, /read_file/);
  assert.match(rendered[3].textContent, /Automatic wake-up/);
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
    (child) => String(child.textContent || "").includes("completed")
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

test("the picker stays live during a session and syncs to the running route", () => {
  const harness = loadWebviewSlice();
  harness.state.featureSettings = MANAGER_CHAT_MODEL_POLICY_PAYLOAD;
  harness.state.managerChatSession = "mls-aaaa1111bbbb2222";
  harness.state.managerChatBackend = "codex_cli";
  harness.state.managerChatModel = "gpt-5-codex";
  for (const backend of ["claude_cli", "codex_cli"]) {
    harness.elements.managerChatBackendSelect.appendChild({ value: backend, textContent: backend });
  }
  harness.elements.managerChatBackendSelect.value = "claude_cli";

  harness.api.applyManagerChatSessionUi();

  assert.equal(harness.elements.managerChatStart.disabled, true);
  assert.equal(harness.elements.managerChatBackendSelect.disabled, false);
  assert.equal(harness.elements.managerChatModelInput.disabled, false);
  assert.equal(harness.elements.managerChatBackendSelect.value, "codex_cli");
  assert.equal(harness.elements.managerChatModelInput.value, "gpt-5-codex");
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

test("the model select is filled from the settings payload's enabled models for the chosen backend", () => {
  const harness = loadWebviewSlice();
  harness.state.featureSettings = MANAGER_CHAT_MODEL_POLICY_PAYLOAD;
  harness.elements.managerChatBackendSelect.value = "claude_cli";

  harness.api.populateManagerChatModelOptions();

  const options = harness.elements.managerChatModelInput.children;
  assert.deepEqual(options.map((option) => option.value), ["claude-opus-4-1", "claude-sonnet-5"]);
  assert.equal(harness.elements.managerChatModelInput.value, "claude-opus-4-1", "the first enabled model is preselected");
  assert.equal(harness.elements.managerChatStart.disabled, false);
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
  assert.deepEqual(options.map((option) => option.value), ["opus", "fable"], "the value sent on Start is always the launched alias");
  assert.equal(options[0].textContent, "claude-opus-5 (opus)", "the option shows the resolved version, not the bare alias");
  assert.equal(options[1].textContent, "fable", "a row with no resolved version yet falls back to its bare name");
  assert.equal(harness.elements.managerChatModelInput.value, "opus");

  trigger(harness.elements.managerChatStart, "click");
  assert.deepEqual(plain(harness.posts.at(-1)), {
    type: "managerLoopStart",
    backendId: "claude_cli",
    model: "opus",
  });
});

test("switching backend repopulates the options and remembers the owner's last choice per backend for the session", () => {
  const harness = loadWebviewSlice();
  harness.state.featureSettings = MANAGER_CHAT_MODEL_POLICY_PAYLOAD;
  harness.elements.managerChatBackendSelect.value = "claude_cli";
  harness.api.populateManagerChatModelOptions();
  harness.elements.managerChatModelInput.value = "claude-sonnet-5";
  trigger(harness.elements.managerChatModelInput, "change");

  harness.elements.managerChatBackendSelect.value = "codex_cli";
  trigger(harness.elements.managerChatBackendSelect, "change");

  assert.deepEqual(harness.elements.managerChatModelInput.children.map((option) => option.value), ["gpt-5-codex"]);
  assert.equal(harness.elements.managerChatModelInput.value, "gpt-5-codex");

  harness.elements.managerChatBackendSelect.value = "claude_cli";
  trigger(harness.elements.managerChatBackendSelect, "change");

  assert.equal(
    harness.elements.managerChatModelInput.value,
    "claude-sonnet-5",
    "the owner's earlier pick for claude_cli is remembered for the webview session",
  );
});

test("a backend with no enabled models shows a disabled hint option and disables Start", () => {
  const harness = loadWebviewSlice();
  harness.state.featureSettings = MANAGER_CHAT_MODEL_POLICY_PAYLOAD;
  harness.elements.managerChatBackendSelect.value = "opencode_cli";

  harness.api.populateManagerChatModelOptions();

  const options = harness.elements.managerChatModelInput.children;
  assert.equal(options.length, 1);
  assert.equal(options[0].disabled, true);
  assert.match(options[0].textContent, /No enabled models/);
  assert.equal(harness.elements.managerChatStart.disabled, true);
});

test("Start sends the exact backend and model chosen in the picker", () => {
  const harness = loadWebviewSlice();
  harness.state.featureSettings = MANAGER_CHAT_MODEL_POLICY_PAYLOAD;
  harness.elements.managerChatBackendSelect.value = "claude_cli";
  harness.api.populateManagerChatModelOptions();
  harness.elements.managerChatModelInput.value = "claude-sonnet-5";

  trigger(harness.elements.managerChatStart, "click");

  assert.deepEqual(plain(harness.posts.at(-1)), {
    type: "managerLoopStart",
    backendId: "claude_cli",
    model: "claude-sonnet-5",
  });
});

test("Start is refused when the current backend has no enabled models", () => {
  const harness = loadWebviewSlice();
  harness.state.featureSettings = MANAGER_CHAT_MODEL_POLICY_PAYLOAD;
  harness.elements.managerChatBackendSelect.value = "opencode_cli";
  harness.api.populateManagerChatModelOptions();

  trigger(harness.elements.managerChatStart, "click");

  assert.equal(harness.posts.length, 0, "no managerLoopStart message is ever sent without a real model");
  assert.equal(harness.elements.managerChatNotice.hidden, false);
});

test("loading the sidebar populates the model picker without opening a dialog", () => {
  const harness = loadWebviewSlice();
  harness.state.featureSettings = MANAGER_CHAT_MODEL_POLICY_PAYLOAD;
  harness.elements.managerChatBackendSelect.value = "codex_cli";

  harness.api.startManagerChatSidebar();

  assert.deepEqual(harness.elements.managerChatModelInput.children.map((option) => option.value), ["gpt-5-codex"]);
  assert.equal(harness.elements.managerChatDialog.showModalCalled, 0, "sidebar load never opens a modal dialog");
});

test("loading the sidebar ensures and requests status when there is no session", () => {
  const harness = loadWebviewSlice();
  assert.equal(harness.state.managerChatSession, null);

  harness.api.startManagerChatSidebar();

  assert.deepEqual(plain(harness.posts.find((post) => post.type === "managerLoopEnsure")), { type: "managerLoopEnsure" });
  assert.deepEqual(plain(harness.posts.find((post) => post.type === "managerLoopStatus")), { type: "managerLoopStatus" });
  assert.equal(harness.elements.managerChatDialog.showModalCalled, 0);
});

test("a sidebar start with a session does not re-ensure", () => {
  const harness = loadWebviewSlice();
  harness.state.managerChatSession = "mls-aaaa1111bbbb2222";

  harness.api.startManagerChatSidebar();

  assert.equal(harness.posts.some((post) => post.type === "managerLoopEnsure"), false);
  assert.equal(harness.posts.some((post) => post.type === "managerLoopStatus"), true);
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

test("the model field is a <select>, not a free-text input", () => {
  assert.match(extensionSource, /<select id="manager-chat-model" class="compact-select" aria-label="Manager model"><\/select>/);
  assert.doesNotMatch(extensionSource, /<input id="manager-chat-model"/);
});

// ── Structural reuse: sidebar beside the dashboard, no covering dialog ──

test("the Manager panel is a persistent sidebar and reuses existing theme tokens", () => {
  assert.match(extensionSource, /id="header-manager-chat"[^>]+title="Collapse or expand Manager chat"/);
  assert.match(extensionSource, /<div class="dashboard-shell" id="dashboard-shell">/);
  assert.match(extensionSource, /<div class="dashboard-column" id="dashboard-column">/);
  assert.match(extensionSource, /<aside class="manager-chat-sidebar" id="manager-chat-sidebar" aria-label="Manager chat">/);
  assert.match(extensionSource, /id="manager-chat-collapse"/);
  assert.doesNotMatch(extensionSource, /<dialog[^>]+id="manager-chat-dialog"/);
  assert.doesNotMatch(extensionSource, /data-close-dialog="manager-chat-dialog"/);
  assert.match(extensionSource, /<div class="needfix-toolbar">\s*<select id="manager-chat-backend"/);
  for (const id of [
    "manager-chat-summary",
    "manager-chat-backend",
    "manager-chat-model",
    "manager-chat-start",
    "manager-chat-rotate",
    "manager-chat-close",
    "manager-chat-status",
    "manager-chat-transcript",
    "manager-chat-notice",
    "manager-chat-composer",
    "manager-chat-input",
    "manager-chat-send",
  ]) {
    assert.match(extensionSource, new RegExp(`id="${id}"`), `${id} must stay`);
  }
  const shellAt = extensionSource.indexOf('id="dashboard-shell"');
  const columnAt = extensionSource.indexOf('id="dashboard-column"');
  const mainEnd = extensionSource.indexOf("</main>");
  const asideAt = extensionSource.indexOf('id="manager-chat-sidebar"');
  assert.ok(shellAt !== -1 && shellAt < columnAt && columnAt < mainEnd && mainEnd < asideAt, "sidebar sits beside the dashboard column");
  assert.doesNotMatch(appSource, /managerChatDialog\.show\(/);
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
});

test("manager chat model and tool text is not clipped by ellipsis or a fixed height", () => {
  const managerCss = extractSlice(
    cssSource,
    ".manager-chat-frame {",
    ".manager-chat-composer .primary-button {\n  flex: 0 0 auto;\n  align-self: flex-end;\n}",
    "manager chat css block",
  );
  assert.match(managerCss, /#manager-chat-model \{[^}]*field-sizing:\s*content/);
  assert.match(managerCss, /\.manager-chat-tool-row > summary \{[^}]*overflow-wrap:\s*anywhere/);
  assert.match(managerCss, /\.manager-chat-tool-row-body \{[^}]*overflow:\s*visible/);
  assert.match(managerCss, /\.manager-chat-tool-row-body \{[^}]*max-height:\s*none/);
  assert.doesNotMatch(managerCss, /text-overflow:\s*ellipsis/);
  assert.doesNotMatch(managerCss, /line-clamp/);
});
