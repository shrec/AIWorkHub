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

// ── extension.js: the six new message types routed to their MCP tools ──────

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
  const context = {
    getMcpClient: () => currentClient,
    sanitizeWebviewPayload: (value) => value,
    sanitizeErrorMessage: (err) => String((err && err.message) || "mcp_unavailable"),
  };
  vm.createContext(context);
  vm.runInContext(
    `"use strict";\n${allowed}\n${outbound}\n${tools}\n${helpers}\n${handler}\n` +
      "this.api = { handleInboundMessage, OUTBOUND_TYPES, MANAGER_LOOP_TOOLS, MANAGER_LOOP_BACKENDS };",
    context,
  );
  return {
    api: context.api,
    setClient(client) {
      currentClient = client;
    },
  };
}

test("extension.js routes the six manager loop message types to their exact MCP tools", async () => {
  const harness = loadHostSlice();
  const client = makeClient(() => ({ ok: true, session: null, running: false, last_turn: null, events: [] }));
  harness.setClient(client);
  const view = makeView();

  harness.api.handleInboundMessage(view, { type: "managerLoopStart", backendId: "claude_cli", model: "opus" });
  harness.api.handleInboundMessage(view, { type: "managerLoopSend", text: "hello manager" });
  harness.api.handleInboundMessage(view, { type: "managerLoopRotate", reason: "context threshold" });
  harness.api.handleInboundMessage(view, { type: "managerLoopClose" });
  harness.api.handleInboundMessage(view, { type: "managerLoopStatus" });
  harness.api.handleInboundMessage(view, {
    type: "managerLoopEvents",
    sessionId: "mls-aaaa1111bbbb2222",
    afterSeq: 5,
  });
  await flush();

  const find = (name) => client.calls.find((call) => call.name === name);
  assert.deepEqual(plain(find("aiworkhub_manager_loop_start").args), { backend_id: "claude_cli", model: "opus" });
  assert.deepEqual(plain(find("aiworkhub_manager_loop_send").args), { text: "hello manager" });
  assert.deepEqual(plain(find("aiworkhub_manager_loop_rotate").args), { reason: "context threshold" });
  assert.deepEqual(plain(find("aiworkhub_manager_loop_close").args), {});
  assert.deepEqual(plain(find("aiworkhub_manager_loop_events").args), { session_id: "mls-aaaa1111bbbb2222", after_seq: 5 });
  // Status is called both explicitly and as the authoritative refresh after
  // every mutating action, so it must have been reached at least once.
  assert.ok(client.calls.some((call) => call.name === "aiworkhub_manager_loop_status"));
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
  return {
    tag,
    className: "",
    textContent: "",
    hidden: false,
    disabled: false,
    value: "",
    open: false,
    attrs: {},
    children: [],
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
  };
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
  const managerChat = extractSlice(
    appSource,
    "function managerChatEventNode(event) {",
    "  showManagerChatNotice(null);\n}",
    "manager chat render/poll functions",
  );

  const state = {
    managerChatSession: null,
    managerChatBackend: null,
    managerChatModel: null,
    managerChatRunning: false,
    managerChatEvents: [],
    managerChatLastSeq: 0,
    managerChatPollTimer: null,
  };
  const elements = {
    managerChatDialog: Object.assign(makeFakeElement("dialog"), { open: true }),
    managerChatTranscript: makeFakeElement("div"),
    managerChatNotice: makeFakeElement("div"),
    managerChatSummary: makeFakeElement("span"),
    managerChatBackendSelect: makeFakeElement("select"),
    managerChatModelInput: makeFakeElement("input"),
    managerChatStart: makeFakeElement("button"),
    managerChatRotate: makeFakeElement("button"),
    managerChatClose: makeFakeElement("button"),
    managerChatStatus: makeFakeElement("span"),
    managerChatStatusLabel: makeFakeElement("span"),
    managerChatSessionLine: makeFakeElement("div"),
    managerChatSessionId: makeFakeElement("span"),
    managerChatSessionBackend: makeFakeElement("span"),
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
    `"use strict";\n${utilities}\n${managerChat}\n` +
      "this.api = { managerChatEventNode, renderManagerChatEvents, renderManagerChatEventsResponse, " +
      "renderManagerChatAction, requestManagerChatEvents, scheduleManagerChatPoll };",
    context,
  );
  return { api: context.api, state, elements, posts, timers };
}

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

test("a manager_turn_in_progress reply shows a notice and sends nothing else", () => {
  const harness = loadWebviewSlice();

  harness.api.renderManagerChatAction("send", { ok: false, error: "manager_turn_in_progress" });

  assert.equal(harness.elements.managerChatNotice.hidden, false);
  assert.match(harness.elements.managerChatNotice.textContent, /already running/i);
  assert.equal(harness.posts.length, 0, "the busy reply must never trigger a resend");
});

// ── Structural reuse: existing dialog/theme classes, no new colour literals ──

test("the Manager panel reuses the dashboard's existing dialog chrome and theme tokens", () => {
  assert.match(extensionSource, /id="header-manager-chat"[^>]+title="Open the Manager chat loop"/);
  assert.match(extensionSource, /<dialog class="diagnostic-dialog manager-chat-dialog" id="manager-chat-dialog">/);
  assert.match(extensionSource, /<div class="needfix-toolbar">\s*<select id="manager-chat-backend"/);
  assert.match(appSource, /elements\.managerChatDialog\.showModal\(\)/);
  assert.match(appSource, /elements\.managerChatDialog\.addEventListener\("close"/);
  assert.match(cssSource, /#manager-chat-dialog \.needfix-toolbar/);

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
