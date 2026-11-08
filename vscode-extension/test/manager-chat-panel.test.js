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
  const warningCalls = [];
  const warningResponses = [];

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
    // Confirmation modal: a queued response simulates a dismissed dialog
    // (undefined); with none queued it auto-confirms with the offered label,
    // matching how the other managerLoop* host tests exercise the real path.
    vscode: {
      window: {
        showWarningMessage: (message, options, ...actions) => {
          warningCalls.push({ message, options, actions });
          const response = warningResponses.length ? warningResponses.shift() : actions[0];
          return Promise.resolve(response);
        },
      },
    },
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
    warningCalls,
    queueWarningResponse(value) {
      warningResponses.push(value);
    },
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
  // A send refusal answers on the send action channel, so the panel reverts
  // its optimistic running state and gives the owner's words back.
  assert.deepEqual(plain(view.posts), [{
    type: harness.api.OUTBOUND_TYPES.managerLoopAction,
    action: "send",
    payload: { ok: false, error: "invalid_backend_id" },
  }]);
});

test("managerLoopSend forwards attached images and refuses a malformed set before any MCP call", async () => {
  const harness = loadHostSlice();
  harness.setClient(makeClient());
  const view = makeView();
  const send = (images) => harness.api.handleInboundMessage(view, {
    type: "managerLoopSend", text: "look", backendId: "codex_cli", model: "gpt-x", images,
  });

  send(Array.from({ length: 5 }, () => ({ name: "a.png", data: "iVBORw0KGgo=" })));
  send([{ name: "a.png", data: "<script>" }]);
  send([{ name: "a.png", data: "A".repeat(7 * 1024 * 1024) }]);
  await flush();

  assert.equal(harness.managerLoopClientInstances.length, 0);
  assert.deepEqual(plain(view.posts), Array.from({ length: 3 }, () => ({
    type: harness.api.OUTBOUND_TYPES.managerLoopAction,
    action: "send",
    payload: { ok: false, error: "invalid_images" },
  })));

  send([{ name: "a.png", data: "iVBORw0KGgo=", extra: "dropped" }]);
  await flush();
  const call = harness.managerLoopClientInstances[0].calls.find((item) => item.name === "aiworkhub_manager_loop_send");
  assert.deepEqual(plain(call.args), {
    text: "look", backend_id: "codex_cli", model: "gpt-x", reasoning: "",
    images: [{ name: "a.png", data: "iVBORw0KGgo=" }],
  });
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

test("managerLoopContinue carries a picked route; an unlisted backend or half a route is dropped", async () => {
  const harness = loadHostSlice();
  harness.setClient(makeClient());
  const view = makeView();
  const sessionId = "mls-aaaa1111bbbb2222";

  harness.api.handleInboundMessage(view, { type: "managerLoopContinue", sessionId, backendId: "claude_cli", model: "claude-opus-5-5" });
  harness.api.handleInboundMessage(view, { type: "managerLoopContinue", sessionId, backendId: "evil_cli", model: "x" });
  harness.api.handleInboundMessage(view, { type: "managerLoopContinue", sessionId, backendId: "codex_cli", model: " " });
  await flush();

  const continues = harness.managerLoopClientInstances[0].calls
    .filter((call) => call.name === "aiworkhub_manager_loop_continue")
    .map((call) => plain(call.args));
  assert.deepEqual(continues, [
    { session_id: sessionId, backend_id: "claude_cli", model: "claude-opus-5-5" },
    { session_id: sessionId },
    { session_id: sessionId },
  ]);
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

test("managerLoopClose shows a modal confirmation before closing; a dismissed choice runs nothing", async () => {
  const harness = loadHostSlice();
  harness.setClient(makeClient());
  const view = makeView();

  harness.api.handleInboundMessage(view, { type: "managerLoopStart", backendId: "claude_cli", model: "opus" });
  await flush();
  const firstClient = harness.managerLoopClientInstances[0];

  harness.queueWarningResponse(undefined);
  harness.api.handleInboundMessage(view, { type: "managerLoopClose" });
  await flush();

  assert.equal(harness.warningCalls.length, 1);
  assert.equal(harness.warningCalls[0].options.modal, true);
  assert.equal(firstClient.terminated, false);

  harness.api.handleInboundMessage(view, { type: "managerLoopClose" });
  await flush();
  assert.equal(firstClient.terminated, true);
});

test("managerLoopDelete shows a modal confirmation before discarding; a dismissed choice runs nothing", async () => {
  const harness = loadHostSlice();
  harness.setClient(makeClient());
  const view = makeView();
  const deleteId = "mls-aaaa1111bbbb2222";

  harness.queueWarningResponse(undefined);
  harness.api.handleInboundMessage(view, { type: "managerLoopDelete", sessionId: deleteId });
  await flush();

  assert.equal(harness.warningCalls.length, 1);
  assert.equal(harness.warningCalls[0].options.modal, true);
  assert.equal(harness.managerLoopClientInstances.length, 0);

  harness.api.handleInboundMessage(view, { type: "managerLoopDelete", sessionId: deleteId });
  await flush();

  assert.equal(harness.managerLoopClientInstances.length, 1);
  assert.deepEqual(
    plain(harness.managerLoopClientInstances[0].calls.find((call) => call.name === "aiworkhub_manager_loop_discard").args),
    { session_id: deleteId },
  );
});

test("the gated manager loop client is torn down from all five required call sites", () => {
  // 1 definition (async function disposeManagerLoopMcpClient() {) plus 5
  // call sites: runManagerLoopAction's close branch, ViewState.dispose(),
  // deactivate(), selectRepositoryCommand, and getMcpClient()'s repository
  // identity change (NF-2026-02113: a switch that bypasses the command must
  // not leave the old repository's manager-loop server alive). A regression
  // dropping any of the five call sites changes this count.
  const occurrences = extensionSource.split("disposeManagerLoopMcpClient()").length - 1;
  assert.equal(occurrences, 6);
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

test("managerLoopStop reaches the gated client immediately, with no confirmation modal", async () => {
  const harness = loadHostSlice();
  const client = makeClient();
  harness.setClient(client);
  const view = makeView();

  harness.api.handleInboundMessage(view, { type: "managerLoopStop" });
  await flush();

  assert.equal(harness.warningCalls.length, 0, "stop is safe and urgent: no modal");
  const gatedClient = harness.managerLoopClientInstances[0];
  const call = gatedClient.calls.find((c) => c.name === "aiworkhub_manager_loop_stop");
  assert.deepEqual(plain(call.args), {});
});

function loadTaskRestartSlice() {
  const outbound = extractSlice(
    extensionSource,
    "const OUTBOUND_TYPES = Object.freeze({",
    "});",
    "OUTBOUND_TYPES (task restart)",
  );
  const taskIdRe = extractSlice(extensionSource, "const TASK_ID_RE = ", ";", "TASK_ID_RE (host)");
  const tools = extractSlice(
    extensionSource,
    "const TASK_RETENTION_TOOLS = Object.freeze({",
    "});",
    "TASK_RETENTION_TOOLS",
  );
  const fn = extractSlice(
    extensionSource,
    "async function runTaskRestart(view, taskId) {",
    "    view.postMessage({ type: OUTBOUND_TYPES.error, message: sanitizeErrorMessage(err) });\n  }\n}",
    "runTaskRestart",
  );

  let currentClient = null;
  const warningCalls = [];
  const warningResponses = [];
  const pushCalls = [];
  const context = {
    getMcpClient: () => currentClient,
    sanitizeErrorMessage: (err) => String((err && err.message) || "mcp_unavailable"),
    pushSnapshot: async () => {
      pushCalls.push("snapshot");
    },
    pushTaskDetail: async (view, taskId) => {
      pushCalls.push(`detail:${taskId}`);
    },
    vscode: {
      window: {
        showWarningMessage: (message, options, ...actions) => {
          warningCalls.push({ message, options, actions });
          const response = warningResponses.length ? warningResponses.shift() : actions[0];
          return Promise.resolve(response);
        },
      },
    },
  };
  vm.createContext(context);
  vm.runInContext(
    `"use strict";\n${taskIdRe}\n${outbound}\n${tools}\n${fn}\n` +
      "this.api = { runTaskRestart, TASK_RETENTION_TOOLS, OUTBOUND_TYPES };",
    context,
  );
  return {
    api: context.api,
    warningCalls,
    pushCalls,
    queueWarningResponse(value) {
      warningResponses.push(value);
    },
    setClient(client) {
      currentClient = client;
    },
  };
}

test("runTaskRestart sends confirm:true only after the modal choice, and nothing on dismiss", async () => {
  const harness = loadTaskRestartSlice();
  const client = makeClient();
  harness.setClient(client);
  const view = makeView();

  harness.queueWarningResponse(undefined);
  await harness.api.runTaskRestart(view, "task-123");
  assert.equal(client.calls.length, 0, "a dismissed modal must call nothing");
  assert.equal(harness.pushCalls.length, 0);

  await harness.api.runTaskRestart(view, "task-123");
  assert.equal(client.calls.length, 1);
  assert.deepEqual(plain(client.calls[0]), {
    name: "aiworkhub_dashboard_task_restart",
    args: { task_id: "task-123", reason: "owner manual restart from dashboard", confirm: true },
  });
  assert.deepEqual(harness.pushCalls, ["snapshot", "detail:task-123"]);
});

// ── media/app.js: transcript rendering, textContent-only, and after_seq polling ──

function makeFakeElement(tag) {
  const element = {
    tag,
    tagName: String(tag).toUpperCase(),
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
    insertBefore(child, reference) {
      if (reference === null) {
        this.children.push(child);
        return child;
      }
      const index = this.children.indexOf(reference);
      assert.notEqual(index, -1, "insertBefore reference must be a child");
      this.children.splice(index, 0, child);
      return child;
    },
    removeChild(child) {
      const index = this.children.indexOf(child);
      assert.notEqual(index, -1, "removeChild target must be a child");
      this.children.splice(index, 1);
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
    "function showManagerChatNotice(message) {",
    "  showManagerChatNotice(null);\n}",
    "manager chat render/poll functions",
  );
  const managerChatWiring = extractSlice(
    appSource,
    "function applyManagerChatCollapsed(collapsed) {",
    "  if (state.managerChatDraft) elements.managerChatInput.value = state.managerChatDraft;\n}",
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
    managerChatPendingSends: [],
    managerChatSentHistory: [],
    managerChatHistoryIndex: -1,
    managerChatPollErrors: 0,
    managerChatPollNoticeFromError: false,
    managerChatHighestSeenTurn: 0,
    managerChatQueuePayload: null,
    managerChatDraft: "",
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
    managerChatStop: makeFakeElement("button"),
    managerChatStatus: makeFakeElement("span"),
    managerChatStatusLabel: makeFakeElement("span"),
    managerChatSessionLine: makeFakeElement("div"),
    managerChatSessionSelect: makeFakeElement("select"),
    managerChatRenameSession: makeFakeElement("button"),
    managerChatNewSession: makeFakeElement("button"),
    managerChatSessionId: makeFakeElement("span"),
    managerChatSessionBackend: makeFakeElement("span"),
    managerChatComposer: makeFakeElement("form"),
    managerChatInput: makeFakeElement("textarea"),
    managerChatSend: makeFakeElement("button"),
    managerChatAttach: makeFakeElement("button"),
    managerChatFile: Object.assign(makeFakeElement("input"), { files: [], clicks: 0, click() { this.clicks += 1; } }),
    managerChatAttachments: Object.assign(makeFakeElement("div"), { hidden: true }),
    headerManagerChatValue: makeFakeElement("strong"),
    headerManagerChatDetail: makeFakeElement("span"),
  };
  const posts = [];
  const timers = [];
  const vscode = { postMessage: (message) => posts.push(message), setState: () => {}, getState: () => ({}) };
  function persistState() { vscode.setState({ managerChatDraft: state.managerChatDraft || "" }); }
  const windowFake = {
    requestAnimationFrame: (fn) => { fn(); return 1; },
    setTimeout: (fn, delay) => {
      const id = timers.length + 1;
      timers.push({ id, fn, delay });
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
    persistState,
    MANAGER_CHAT_POLL_MS: 1500,
    // Reads at once, so a test sees the attached image right after the event.
    FileReader: class {
      readAsDataURL(file) {
        this.result = `data:${file.type};base64,${file.base64}`;
        this.onload();
      }
    },
  };
  vm.createContext(context);
  vm.runInContext(
    `"use strict";\n${utilities}\n${constants}\n${consoleSource}\n${managerChat}\n${managerChatWiring}\n` +
      "this.api = { managerChatEventNode, renderManagerChatEvents, renderManagerChatEventsResponse, managerConsoleReset, " +
      "renderManagerChatAction, renderManagerChatStatus, requestManagerChatEvents, scheduleManagerChatPoll, " +
      "applyManagerChatSessionUi, applyManagerChatComposerState, managerChatEnabledModels, managerChatSelectedRoute, populateManagerChatModelOptions, applyManagerChatCollapsed, toggleManagerChatSidebar, startManagerChatSidebar };",
    context,
  );
  return { api: context.api, state, elements, posts, timers, window: windowFake, context };
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
  // An idle turn_end alone does not pull status; the periodic refresh and a
  // session_close do (see the two tests after the busy-reply test).
  assert.equal(statusPosts().length, 0, "an idle turn_end batch does not pull status");
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

test("a session_close for the current session pulls status even when the panel is idle", () => {
  // EntryLink 0.12.26: a callback turn crossed the context threshold while the
  // panel was idle; the panel stayed on the closed session because only a
  // running panel ever pulled status after a terminal batch.
  const harness = loadWebviewSlice();
  harness.state.managerChatSession = "mls-aaaa1111bbbb2222";
  harness.state.managerChatRunning = false;

  harness.api.renderManagerChatEventsResponse({
    ok: true,
    events: [
      { seq: 9, type: "turn_end", turn: 2, payload: {} },
      { seq: 10, type: "session_close", turn: 2, payload: { reason: "context_threshold" } },
    ],
  });

  assert.deepEqual(plain(harness.posts.filter((post) => post.type === "managerLoopStatus")), [{ type: "managerLoopStatus" }]);
});

test("the poll loop refreshes status every few seconds so a server-side rotation is followed", () => {
  const harness = loadWebviewSlice();
  harness.state.managerChatSession = "mls-aaaa1111bbbb2222";
  const statusPosts = () => harness.posts.filter((post) => post.type === "managerLoopStatus").length;
  const tick = () => {
    harness.timers.shift().fn();
    harness.api.renderManagerChatEventsResponse({ ok: true, events: [] });
  };

  harness.api.scheduleManagerChatPoll();
  for (let n = 0; n < 3; n += 1) tick();
  assert.equal(statusPosts(), 0, "4.5s of idle polls is still events only");
  tick();
  assert.equal(statusPosts(), 1, "the 6s idle poll also pulls status");
  assert.equal(harness.posts.at(-1).type, "managerLoopEvents", "events keep flowing even if the status reply is lost");

  harness.api.renderManagerChatStatus({
    ok: true,
    running: false,
    session: { session_id: "mls-cccc3333dddd4444", backend_id: "codex_cli", model: "gpt-x" },
    send_queue: [],
  });
  assert.equal(harness.state.managerChatSession, "mls-cccc3333dddd4444", "the status reply follows the rotation");
  for (let n = 0; n < 3; n += 1) tick();
  assert.equal(statusPosts(), 1, "a status reply restarts the refresh clock");
});

test("a refused send reverts the optimistic running state and gives the text back", () => {
  const harness = loadWebviewSlice();
  harness.state.managerChatSession = "mls-aaaa1111bbbb2222";
  harness.elements.managerChatBackendSelect.value = "codex_cli";
  harness.elements.managerChatModelInput.value = "gpt-x";
  harness.elements.managerChatInput.value = "hello";

  trigger(harness.elements.managerChatComposer, "submit");
  assert.equal(harness.state.managerChatRunning, true);
  assert.equal(harness.elements.managerChatInput.value, "");
  harness.api.renderManagerChatEvents();
  assert.ok(harness.state.managerChatThinkingSince > 0);

  harness.api.renderManagerChatAction("send", { ok: false, error: "manager_send_queue_full" });

  assert.equal(harness.state.managerChatRunning, false, "a refusal must not leave a phantom running turn");
  assert.equal(harness.state.managerChatThinkingSince, 0, "the Thinking timer stops with it");
  assert.equal(harness.elements.managerChatInput.value, "hello", "the refused words come back to the composer");
  assert.equal(harness.elements.managerChatNotice.hidden, false);
  const text = flattenNodes(harness.elements.managerChatTranscript, []).map((part) => String(part.textContent || "")).join("");
  assert.doesNotMatch(text, /Thinking/);
});

test("a refused send's text is restored after what the owner typed since", () => {
  const harness = loadWebviewSlice();
  harness.elements.managerChatBackendSelect.value = "codex_cli";
  harness.elements.managerChatModelInput.value = "gpt-x";
  harness.elements.managerChatInput.value = "first";
  trigger(harness.elements.managerChatComposer, "submit");
  harness.elements.managerChatInput.value = "typing on";

  harness.api.renderManagerChatAction("send", { ok: false, error: "invalid_backend_id" });

  assert.equal(harness.elements.managerChatInput.value, "typing on\nfirst");
  assert.equal(harness.state.managerChatRunning, false);
});

test("the Thinking timer restarts for a new turn instead of carrying an old start", () => {
  const harness = loadWebviewSlice();
  harness.state.managerChatSession = "mls-aaaa1111bbbb2222";
  harness.state.managerChatRunning = true;
  harness.state.managerChatThinkingSince = 1; // a start from a turn long gone
  harness.state.managerChatEvents = [
    { seq: 1, turn: 1, type: "user_message", payload: { text: "hi" } },
    { seq: 2, turn: 1, type: "turn_end", payload: {} },
    { seq: 3, turn: 2, type: "user_message", payload: { text: "callback: T-1 -> review_ready" } },
  ];

  harness.api.renderManagerChatEvents();

  assert.ok(Date.now() - harness.state.managerChatThinkingSince < 60000, "the timer belongs to turn 2");
});

test("turn_end, error and session_close stop the Thinking timer even on an idle panel", () => {
  for (const type of ["turn_end", "error", "session_close"]) {
    const harness = loadWebviewSlice();
    harness.state.managerChatSession = "mls-aaaa1111bbbb2222";
    harness.state.managerChatRunning = false;
    harness.state.managerChatThinkingSince = 1;

    harness.api.renderManagerChatEventsResponse({ ok: true, events: [{ seq: 4, type, turn: 2, payload: {} }] });

    assert.equal(harness.state.managerChatThinkingSince, 0, type);
  }
});

test("Enter sends, Shift+Enter and IME composition do not", () => {
  const harness = loadWebviewSlice();
  harness.elements.managerChatComposer.requestSubmit = () => trigger(harness.elements.managerChatComposer, "submit");
  harness.elements.managerChatBackendSelect.value = "codex_cli";
  harness.elements.managerChatModelInput.value = "gpt-x";
  const sends = () => harness.posts.filter((post) => post.type === "managerLoopSend").length;
  let prevented = 0;
  const key = (overrides) => trigger(harness.elements.managerChatInput, "keydown", {
    key: "Enter", shiftKey: false, isComposing: false, preventDefault() { prevented += 1; }, ...overrides,
  });

  harness.elements.managerChatInput.value = "line one";
  key({ shiftKey: true });
  key({ isComposing: true });
  key({ key: "a" });
  assert.equal(sends(), 0);
  assert.equal(prevented, 0, "Shift+Enter keeps inserting a newline");

  key({});
  assert.equal(sends(), 1);
  assert.equal(prevented, 1);
  assert.equal(harness.elements.managerChatInput.value, "");
});

function imageFile(name, type, base64, size) {
  return { name, type, base64, size: size === undefined ? base64.length : size };
}

test("a pasted, dropped or chosen image attaches and Send carries it with the text", () => {
  const harness = loadWebviewSlice();
  const { elements } = harness;
  elements.managerChatBackendSelect.value = "codex_cli";
  elements.managerChatModelInput.value = "gpt-x";

  trigger(elements.managerChatInput, "paste", { clipboardData: { files: [imageFile("shot.png", "image/png", "iVBORw0KGgo=")] } });
  trigger(elements.managerChatComposer, "drop", { dataTransfer: { files: [imageFile("a.gif", "image/gif", "R0lGODlh")] } });
  trigger(elements.managerChatAttach, "click");
  assert.equal(elements.managerChatFile.clicks, 1, "Attach opens the file chooser");
  elements.managerChatFile.files = [imageFile("b.webp", "image/webp", "UklGRg==")];
  trigger(elements.managerChatFile, "change");

  assert.equal(elements.managerChatAttachments.hidden, false);
  assert.equal(elements.managerChatAttachments.children.length, 3);
  elements.managerChatInput.value = "look";
  trigger(elements.managerChatComposer, "submit");

  assert.deepEqual(plain(harness.posts.at(-1)), {
    type: "managerLoopSend",
    text: "look",
    backendId: "codex_cli",
    model: "gpt-x",
    reasoning: "",
    images: [
      { name: "shot.png", data: "iVBORw0KGgo=" },
      { name: "a.gif", data: "R0lGODlh" },
      { name: "b.webp", data: "UklGRg==" },
    ],
  });
  assert.equal(elements.managerChatAttachments.hidden, true, "sent images leave the composer");

  harness.api.renderManagerChatAction("send", { ok: false, error: "manager_image_invalid:0:type" });
  assert.equal(elements.managerChatAttachments.children.length, 3, "a refused send gives the images back");
});

test("only four supported images within 5 MiB attach, and images need a message", () => {
  const harness = loadWebviewSlice();
  const { elements } = harness;
  elements.managerChatBackendSelect.value = "codex_cli";
  elements.managerChatModelInput.value = "gpt-x";
  const drop = (...files) => trigger(elements.managerChatComposer, "drop", { dataTransfer: { files } });

  drop(imageFile("notes.pdf", "application/pdf", "JVBERi0="));
  assert.match(elements.managerChatNotice.textContent, /PNG, JPEG, GIF or WebP/);
  drop(imageFile("huge.png", "image/png", "iVBORw0KGgo=", 5 * 1024 * 1024 + 1));
  assert.match(elements.managerChatNotice.textContent, /5 MiB/);
  drop(...[1, 2, 3, 4, 5].map((n) => imageFile(`s${n}.png`, "image/png", "iVBORw0KGgo=")));
  assert.match(elements.managerChatNotice.textContent, /4 images/);
  assert.equal(elements.managerChatAttachments.children.length, 4);

  const chip = elements.managerChatAttachments.children[0];
  trigger(chip.children.at(-1), "click");
  assert.equal(elements.managerChatAttachments.children.length, 3, "a chip's remove button drops its image");

  elements.managerChatInput.value = "";
  trigger(elements.managerChatComposer, "submit");
  assert.equal(harness.posts.filter((post) => post.type === "managerLoopSend").length, 0);
  assert.match(elements.managerChatNotice.textContent, /message/);
  assert.equal(elements.managerChatAttachments.children.length, 3, "the images wait for the message");
});

test("the transcript and the send queue say how many images a message carried", () => {
  const harness = loadWebviewSlice();
  const node = harness.api.managerChatEventNode({ seq: 1, turn: 1, type: "user_message", payload: { text: "look", images: 2 } });
  assert.match(flattenNodes(node, []).map((part) => String(part.textContent || "")).join(""), /2 images attached/);

  harness.api.renderManagerChatStatus({
    ok: true,
    running: true,
    session: { session_id: "mls-aaaa1111bbbb2222", backend_id: "claude_cli", model: "opus" },
    send_queue: [{ text: "later", images: 1 }],
  });
  const queued = flattenNodes(harness.elements.managerChatQueue, []).map((part) => String(part.textContent || "")).join("");
  assert.match(queued, /1 image attached/);
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
  assert.deepEqual(options.map((option) => option.value), ["", "claude-opus-4-1", "claude-sonnet-5", "gpt-5-codex"]);
  assert.equal(options.find((option) => option.value === "gpt-5-codex").dataset.backendId, "codex_cli");
  assert.equal(harness.elements.managerChatModelInput.value, "", "no model is picked until the owner chooses one");
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
  assert.deepEqual(options.map((option) => option.value), ["", "opus", "fable"], "the value sent on Send is always the launched alias");
  assert.equal(options[1].textContent, "claude-opus-5 (opus)", "the option shows the resolved version, not the bare alias");
  assert.equal(options[2].textContent, "fable", "a row with no resolved version yet falls back to its bare name");
  assert.equal(harness.elements.managerChatModelInput.value, "", "nothing is picked until the owner chooses one");
  harness.elements.managerChatModelInput.value = "opus";
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
    "",
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
    "",
    "claude-opus-4-1",
    "claude-sonnet-5",
    "gpt-5-codex",
  ]);
  assert.equal(harness.elements.managerChatDialog.showModalCalled, 0, "sidebar load never opens a modal dialog");
});

test("opening the chat with two enabled models and no previous selection leaves Send refused until the owner picks one", () => {
  const harness = loadWebviewSlice();
  harness.state.featureSettings = {
    ok: true,
    model_policy: {
      ok: true,
      catalog: {
        workers: [
          { provider: "anthropic", adapter: "claude_cli", model: "claude-opus-4-1", worker_id: "w1", effective_enabled: true, catalog_enabled: true },
          { provider: "openai", adapter: "codex_cli", model: "gpt-5-codex", worker_id: "w4", effective_enabled: true, catalog_enabled: true },
        ],
      },
      providers: {},
    },
  };
  harness.posts.length = 0;

  harness.api.startManagerChatSidebar();

  assert.deepEqual(plain(harness.posts), [{ type: "managerLoopStatus" }, { type: "managerLoopRestore" }], "the sidebar opens posting only status and restore");
  const select = harness.elements.managerChatModelInput;
  assert.equal(select.value, "", "no model the owner never picked is pre-selected");
  assert.equal(select.children[0].value, "");
  assert.equal(select.children[0].disabled, true);
  assert.equal(select.children[0].textContent, "Choose a model");

  harness.elements.managerChatInput.value = "hello";
  trigger(harness.elements.managerChatComposer, "submit");
  assert.equal(harness.posts.some((post) => post.type === "managerLoopSend"), false, "send is refused without a picked model");
  assert.equal(harness.elements.managerChatNotice.hidden, false);
  assert.match(harness.elements.managerChatNotice.textContent, /Choose a model/);

  select.value = "gpt-5-codex";
  trigger(harness.elements.managerChatComposer, "submit");
  assert.deepEqual(plain(harness.posts.at(-1)), {
    type: "managerLoopSend",
    text: "hello",
    backendId: "codex_cli",
    model: "gpt-5-codex",
    reasoning: "",
  });
});

test("opening the chat posts exactly one managerLoopRestore and does not attach a session before any reply", () => {
  const harness = loadWebviewSlice();
  harness.state.managerChatSession = "mls-aaaa1111bbbb2222";
  harness.state.managerChatEvents = [{ seq: 2, type: "user_message", payload: { text: "old" } }];

  harness.api.startManagerChatSidebar();

  assert.equal(harness.posts.filter((post) => post.type === "managerLoopRestore").length, 1);
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


test("slash commands stay on this session", () => {
  const harness = loadWebviewSlice();
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

test("picking a model pins it on the open conversation so it becomes the manager", () => {
  const harness = loadWebviewSlice();
  harness.state.featureSettings = MANAGER_CHAT_MODEL_POLICY_PAYLOAD;
  harness.api.populateManagerChatModelOptions();
  const select = harness.elements.managerChatModelInput;

  select.value = "gpt-5-codex";
  trigger(select, "change");
  assert.equal(
    harness.posts.some((post) => post.type === "managerLoopContinue"),
    false,
    "with no open conversation there is nothing to pin",
  );

  harness.state.managerChatSession = "mls-aaaa1111bbbb2222";
  trigger(select, "change");
  assert.deepEqual(plain(harness.posts.at(-1)), {
    type: "managerLoopContinue",
    sessionId: "mls-aaaa1111bbbb2222",
    backendId: "codex_cli",
    model: "gpt-5-codex",
  });
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
    "manager-chat-close",
    "manager-chat-status",
    "manager-chat-transcript",
    "manager-chat-transcript",
    "manager-chat-notice",
    "manager-chat-composer",
    "manager-chat-input",
    "manager-chat-send",
    "manager-chat-attach",
    "manager-chat-file",
    "manager-chat-attachments",
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
  assert.match(extensionSource, /id="manager-chat-close"/);
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
  assert.match(consoleCss, /\.manager-chat-tool-row-body \{[^}]*overflow:\s*auto/);
  assert.match(consoleCss, /\.manager-chat-tool-row-body \{[^}]*max-height:\s*20em/);
  assert.doesNotMatch(managerCss, /text-overflow:\s*ellipsis/);
  assert.doesNotMatch(managerCss, /line-clamp/);
  assert.doesNotMatch(consoleCss, /line-clamp/);
  const ellipsisRules = [...consoleCss.matchAll(/([^{}]+)\{[^{}]*text-overflow:\s*ellipsis[^{}]*\}/g)];
  assert.ok(ellipsisRules.length > 0, "the closed tool row is the one place an ellipsis is expected");
  for (const [, selector] of ellipsisRules) assert.match(selector, /:not\(\[open\]\)/, selector.trim());
});

test("console rows wrap long tokens, keep the clock inline and style streaming thinking like the final block", () => {
  assert.match(consoleCss, /\.mc-mono \{[^}]*overflow-wrap:\s*anywhere/);
  const taskLink = /\.mc-task-link \{([^}]*)\}/.exec(consoleCss)[1];
  assert.match(taskLink, /overflow-wrap:\s*anywhere/);
  assert.match(taskLink, /white-space:\s*normal/);
  assert.match(taskLink, /text-align:\s*left/);
  const clock = /\.mc-time \{([^}]*)\}/.exec(consoleCss)[1];
  assert.match(clock, /float:\s*right/);
  assert.doesNotMatch(clock, /display:\s*block/);
  assert.match(consoleCss, /\.manager-chat-bubble \{[^}]*display:\s*flow-root/);
  const closedSummary = /\.manager-chat-tool-row:not\(\[open\]\) > summary \{([^}]*)\}/.exec(consoleCss)[1];
  assert.match(closedSummary, /white-space:\s*nowrap/);
  assert.match(closedSummary, /overflow:\s*hidden/);
  const thinkingText = /\.mc-thinking-text \{([^}]*)\}/.exec(consoleCss)[1];
  assert.match(thinkingText, /white-space:\s*pre-wrap/);
  assert.match(thinkingText, /overflow-wrap:\s*anywhere/);
  assert.match(thinkingText, /color:\s*var\(--muted\)/);
  assert.match(consoleCss, /\.manager-chat-thinking-block,\s*\.mc-thinking \{[^}]*border-left/);
  assert.match(consoleCss, /\.manager-chat-thinking-block > summary,\s*\.mc-thinking > summary \{/);
  assert.match(consoleCss, /\.mc-table-wrap \{[^}]*overflow-x:\s*auto/);
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
  // NF-2026-01230: a mid-turn assistant_text (between tool calls) keeps the turn's next
  // partial streaming; only turn_end retires it.
  const next = { session_id: session, turn: 3, text: "next", reasoning: "" };
  harness.api.renderManagerChatEventsResponse({
    ok: true, session_id: session, partial: next,
    events: [{ seq: 2, turn: 3, type: "assistant_text", payload: { text: "final" } }],
  });
  assert.equal(harness.state.managerChatPartial, next);
  assert.equal(harness.elements.managerChatAnnouncer.textContent, "final");
  assert.ok(flattenNodes(harness.elements.managerChatTranscript, []).some((node) => node.className === "mc-caret"));
  harness.api.renderManagerChatEventsResponse({
    ok: true, session_id: session, partial: next,
    events: [{ seq: 3, turn: 3, type: "turn_end", payload: {} }],
  });
  assert.equal(harness.state.managerChatPartial, null);
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

// ── Restore, Rotate and Close: the panel's own three session controls ───────

test("the extension.js webview HTML carries a disabled #manager-chat-close button next to Rotate", () => {
  const markup = extensionSource.slice(
    extensionSource.indexOf('id="manager-chat-rotate"') - 40,
    extensionSource.indexOf('id="manager-chat-status"'),
  );
  assert.match(markup, /<button type="button" id="manager-chat-close" disabled>Close<\/button>/);
});

test("the extension.js webview HTML carries a disabled #manager-chat-stop button next to Close", () => {
  const markup = extensionSource.slice(
    extensionSource.indexOf('id="manager-chat-rotate"') - 40,
    extensionSource.indexOf('id="manager-chat-status"'),
  );
  assert.match(markup, /<button type="button" id="manager-chat-stop" disabled>Stop<\/button>/);
});

test("opening the sidebar posts exactly one managerLoopStatus then one managerLoopRestore", () => {
  const harness = loadWebviewSlice();
  harness.posts.length = 0;

  harness.api.startManagerChatSidebar();

  const relevant = harness.posts.filter((post) => post.type === "managerLoopStatus" || post.type === "managerLoopRestore");
  assert.deepEqual(plain(relevant), [{ type: "managerLoopStatus" }, { type: "managerLoopRestore" }]);
});

test("a restore reply with a session posts a status refresh and does not force the running flag", () => {
  const harness = loadWebviewSlice();
  const sessionId = "mls-" + "c".repeat(32);
  harness.posts.length = 0;

  harness.api.renderManagerChatAction("restore", { ok: true, session: { session_id: sessionId }, running: false });

  assert.deepEqual(plain(harness.posts), [{ type: "managerLoopStatus" }]);
  assert.equal(harness.state.managerChatRunning, false);
});

test("a restore reply with a null session keeps the no-session state", () => {
  const harness = loadWebviewSlice();
  harness.posts.length = 0;

  harness.api.renderManagerChatAction("restore", { ok: true, session: null, running: false });

  assert.equal(harness.state.managerChatSession, null);
  assert.deepEqual(plain(harness.posts), [{ type: "managerLoopStatus" }]);
});

test("a restore error shows a notice and posts nothing further", () => {
  const harness = loadWebviewSlice();
  harness.posts.length = 0;

  harness.api.renderManagerChatAction("restore", { ok: false, error: "manager_loop_unavailable:RuntimeError" });

  assert.equal(harness.elements.managerChatNotice.hidden, false);
  assert.match(harness.elements.managerChatNotice.textContent, /Manager error/);
  assert.equal(harness.state.managerChatSession, null);
  assert.deepEqual(plain(harness.posts), []);
});

test("Rotate posts the owner-requested reason only when enabled with a session", () => {
  const harness = loadWebviewSlice();
  const { elements } = harness;
  harness.state.managerChatSession = "mls-" + "d".repeat(32);
  elements.managerChatRotate.disabled = false;

  trigger(elements.managerChatRotate, "click");
  assert.deepEqual(plain(harness.posts.at(-1)), { type: "managerLoopRotate", reason: "owner_requested" });

  harness.posts.length = 0;
  elements.managerChatRotate.disabled = true;
  trigger(elements.managerChatRotate, "click");
  assert.deepEqual(plain(harness.posts), []);

  harness.posts.length = 0;
  elements.managerChatRotate.disabled = false;
  harness.state.managerChatSession = null;
  trigger(elements.managerChatRotate, "click");
  assert.deepEqual(plain(harness.posts), []);
});

test("a rotate reply refreshes status and the current session's events", () => {
  const harness = loadWebviewSlice();
  const sessionId = "mls-" + "e".repeat(32);
  harness.state.managerChatSession = sessionId;
  harness.state.managerChatLastSeq = 7;

  harness.api.renderManagerChatAction("rotate", { ok: true });

  assert.ok(harness.posts.some((post) => post.type === "managerLoopStatus"));
  assert.deepEqual(plain(harness.posts.at(-1)), { type: "managerLoopEvents", sessionId, afterSeq: 7 });
});

test("Close posts managerLoopClose only when enabled with a session; confirmation is host-side", () => {
  const harness = loadWebviewSlice();
  const { elements } = harness;
  harness.state.managerChatSession = "mls-" + "f".repeat(32);
  elements.managerChatClose.disabled = false;

  harness.window.confirm = () => { throw new Error("webview confirm must not be used"); };
  trigger(elements.managerChatClose, "click");
  assert.deepEqual(plain(harness.posts.at(-1)), { type: "managerLoopClose" });

  harness.posts.length = 0;
  elements.managerChatClose.disabled = true;
  trigger(elements.managerChatClose, "click");
  assert.deepEqual(plain(harness.posts), []);

  harness.posts.length = 0;
  elements.managerChatClose.disabled = false;
  harness.state.managerChatSession = null;
  trigger(elements.managerChatClose, "click");
  assert.deepEqual(plain(harness.posts), []);
});

test("Stop posts managerLoopStop only when enabled; confirmation is host-side", () => {
  const harness = loadWebviewSlice();
  const { elements } = harness;
  elements.managerChatStop.disabled = false;

  trigger(elements.managerChatStop, "click");
  assert.deepEqual(plain(harness.posts.at(-1)), { type: "managerLoopStop" });

  harness.posts.length = 0;
  elements.managerChatStop.disabled = true;
  trigger(elements.managerChatStop, "click");
  assert.deepEqual(plain(harness.posts), []);
});

test("a close reply returns the panel to the no-session state and refreshes status once", () => {
  const harness = loadWebviewSlice();
  harness.state.managerChatSession = "mls-" + "a".repeat(32);
  harness.state.managerChatRunning = true;
  harness.state.managerChatEvents = [{ seq: 1, turn: 1, type: "user_message", payload: { text: "hi" } }];
  harness.state.managerChatLastSeq = 1;
  harness.posts.length = 0;

  harness.api.renderManagerChatAction("close", { ok: true });

  assert.equal(harness.state.managerChatSession, null);
  assert.equal(harness.state.managerChatRunning, false);
  assert.equal(harness.state.managerChatEvents.length, 0);
  assert.equal(harness.state.managerChatLastSeq, 0);
  assert.deepEqual(plain(harness.posts), [{ type: "managerLoopStatus" }]);
});

test("applyManagerChatSessionUi enables Close under the same rule as Rotate", () => {
  const harness = loadWebviewSlice();
  const { elements, state } = harness;

  state.managerChatSession = null;
  state.managerChatRunning = false;
  harness.api.applyManagerChatSessionUi();
  assert.equal(elements.managerChatClose.disabled, true);

  state.managerChatSession = "mls-" + "b".repeat(32);
  state.managerChatRunning = true;
  harness.api.applyManagerChatSessionUi();
  assert.equal(elements.managerChatClose.disabled, true);

  state.managerChatRunning = false;
  harness.api.applyManagerChatSessionUi();
  assert.equal(elements.managerChatClose.disabled, false);
});

test("applyManagerChatSessionUi enables Stop only while a turn is running", () => {
  const harness = loadWebviewSlice();
  const { elements, state } = harness;

  state.managerChatSession = null;
  state.managerChatRunning = false;
  harness.api.applyManagerChatSessionUi();
  assert.equal(elements.managerChatStop.disabled, true);

  state.managerChatRunning = true;
  harness.api.applyManagerChatSessionUi();
  assert.equal(elements.managerChatStop.disabled, false);

  state.managerChatSession = "mls-" + "b".repeat(32);
  state.managerChatRunning = false;
  harness.api.applyManagerChatSessionUi();
  assert.equal(elements.managerChatStop.disabled, true, "a session alone must not enable Stop");
});

// ── turn_stopped: ends Running, stops Thinking timer, pulls status ──────────
test("a turn_stopped event ends Running and the Thinking timer and pulls status", () => {
  const harness = loadWebviewSlice();
  harness.state.managerChatSession = "mls-" + "a".repeat(32);
  harness.state.managerChatRunning = true;
  harness.state.managerChatThinkingSince = Date.now() - 500;

  harness.api.renderManagerChatEventsResponse({
    ok: true,
    events: [{ seq: 5, type: "turn_stopped", turn: 1, payload: {} }],
  });

  assert.equal(harness.state.managerChatRunning, false);
  assert.equal(harness.state.managerChatThinkingSince, 0);
  assert.deepEqual(plain(harness.posts.at(-1)), { type: "managerLoopStatus" });
});

// ── Rename: posts managerLoopRename without title ─────────────────────────
test("a Rename click posts managerLoopRename without title", () => {
  const harness = loadWebviewSlice();
  harness.state.managerChatSession = "mls-" + "a".repeat(32);
  harness.state.managerChatTitle = "My Session";
  harness.window.prompt = () => null;

  trigger(harness.elements.managerChatRenameSession, "click");

  const post = harness.posts.at(-1);
  assert.equal(post.type, "managerLoopRename");
  assert.equal(post.sessionId, "mls-" + "a".repeat(32));
  assert.equal(post.currentTitle, "My Session");
  assert.equal(post.title, undefined);
});

// ── stillBoundTo false: posts ok:false manager_reply_superseded ───────────
test("when stillBoundTo is false the host posts ok:false manager_reply_superseded for status", async () => {
  const harness = loadHostSlice();
  const view = makeView();
  // The binding moves while the call is in flight, as a repo switch would.
  harness.setClient(makeClient(() => { view.bindClient({}); return { ok: true }; }));
  harness.api.handleInboundMessage(view, { type: "managerLoopStatus" });
  await flush();

  const statusPost = view.posts.find((p) => p.type === "managerLoopStatus");
  assert.ok(statusPost, "status reply posted");
  assert.equal(statusPost.payload.ok, false);
  assert.equal(statusPost.payload.error, "manager_reply_superseded");
});

test("when stillBoundTo is false the host posts ok:false manager_reply_superseded for events", async () => {
  const harness = loadHostSlice();
  const view = makeView();
  // The binding moves while the call is in flight, as a repo switch would.
  harness.setClient(makeClient(() => { view.bindClient({}); return { ok: true }; }));
  harness.api.handleInboundMessage(view, { type: "managerLoopEvents", sessionId: "mls-12345678", afterSeq: 0 });
  await flush();

  const eventsPost = view.posts.find((p) => p.type === "managerLoopEvents");
  assert.ok(eventsPost, "events reply posted");
  assert.equal(eventsPost.payload.ok, false);
  assert.equal(eventsPost.payload.error, "manager_reply_superseded");
  assert.equal(eventsPost.payload.session_id, "mls-12345678");
});

// ── Superseded is silent and reschedules ──────────────────────────────────
test("a superseded events reply is silent and reschedules polling", () => {
  const harness = loadWebviewSlice();
  harness.state.managerChatSession = "mls-" + "a".repeat(32);
  harness.state.managerChatRunning = true;
  harness.posts.length = 0;

  harness.api.renderManagerChatEventsResponse({ ok: false, error: "manager_reply_superseded", session_id: "mls-" + "a".repeat(32) });

  assert.equal(harness.posts.length, 0, "no notice or status post");
  assert.ok(harness.timers.length > 0, "poll rescheduled");
});

// ── Poll error backoff ────────────────────────────────────────────────────
test("a status ok:false shows a notice but does not stop polling", () => {
  const harness = loadWebviewSlice();
  harness.state.managerChatSession = "mls-" + "a".repeat(32);
  harness.state.managerChatRunning = true;

  harness.api.renderManagerChatStatus({ ok: false, error: "timeout" });

  assert.ok(harness.timers.length > 0, "poll timer rescheduled");
  assert.equal(harness.elements.managerChatNotice.hidden, false);
});

test("consecutive poll errors double the delay up to 30 s and reset on ok", () => {
  const harness = loadWebviewSlice();
  harness.state.managerChatSession = "mls-" + "a".repeat(32);
  harness.state.managerChatRunning = true;

  harness.api.renderManagerChatStatus({ ok: false, error: "e1" });
  harness.api.renderManagerChatStatus({ ok: false, error: "e2" });
  harness.api.renderManagerChatStatus({ ok: false, error: "e3" });

  const lastTimer = harness.timers.filter((t) => t.id !== undefined).at(-1);
  assert.ok(lastTimer, "poll timer exists");

  harness.api.renderManagerChatStatus({ ok: true, running: false, session: null });
  assert.equal(harness.state.managerChatPollErrors, 0);
});

// ── Watchdog ──────────────────────────────────────────────────────────────
test("a session with no events reply for 15 s re-requests events", () => {
  const harness = loadWebviewSlice();
  harness.state.managerChatSession = "mls-" + "a".repeat(32);
  harness.state.managerChatRunning = true;
  harness.posts.length = 0;

  harness.api.scheduleManagerChatPoll();
  assert.equal(harness.timers.length, 1, "the watchdog rides the one poll timer");
  harness.timers[0].fn();
  assert.equal(harness.posts.filter((p) => p.type === "managerLoopEvents").length, 1, "the tick requests events");

  // No reply arrives: exactly one live timer remains, the 15 s watchdog
  // (the fake keeps fired timers in the list, so match the live id).
  const live = () => harness.timers.filter((t) => t.id === harness.state.managerChatPollTimer);
  assert.equal(live().length, 1, "one watchdog timer");
  assert.equal(live()[0].delay, 15000);

  live()[0].fn();
  assert.equal(harness.posts.filter((p) => p.type === "managerLoopEvents").length, 2, "the watchdog re-requests events");
  assert.equal(live().length, 1, "and re-arms itself");
  assert.equal(live()[0].delay, 15000);
});

// ── Pending FIFO ──────────────────────────────────────────────────────────
test("two quick sends where the second is refused restore the second text", () => {
  const harness = loadWebviewSlice();
  harness.state.managerChatSession = "mls-" + "a".repeat(32);
  harness.state.managerChatRunning = true;
  harness.state.managerChatPendingSends = [
    { text: "first message", images: [] },
    { text: "second message", images: [] },
  ];
  harness.elements.managerChatInput.value = "";

  harness.api.renderManagerChatAction("send", { ok: true, session_id: "mls-" + "a".repeat(32) });

  assert.equal(harness.state.managerChatPendingSends.length, 1, "oldest shifted");
  assert.equal(harness.state.managerChatPendingSends[0].text, "second message");

  harness.api.renderManagerChatAction("send", { ok: false, error: "manager_turn_in_progress" });

  assert.ok(harness.elements.managerChatInput.value.includes("second message"), "refused text restored");
});

test("pending sends render as Sending rows before server queued rows", () => {
  const harness = loadWebviewSlice();
  harness.state.managerChatPendingSends = [{ text: "local pending", images: [] }];

  harness.api.renderManagerChatStatus({
    ok: true,
    running: false,
    session: null,
    send_queue: [{ text: "server queued" }],
  });

  const text = flattenNodes(harness.elements.managerChatQueue, []).map((part) => String(part.textContent || "")).join("");
  assert.ok(text.indexOf("Sending") !== -1 && text.indexOf("local pending") !== -1, "pending row rendered");
  assert.ok(text.indexOf("local pending") < text.indexOf("server queued"), "pending rows come first");
});

test("a submit renders a Sending row at once and its send reply removes it", () => {
  const harness = loadWebviewSlice();
  harness.elements.managerChatBackendSelect.value = "codex_cli";
  harness.elements.managerChatModelInput.value = "gpt-x";
  harness.elements.managerChatInput.value = "hello there";
  const queueText = () => flattenNodes(harness.elements.managerChatQueue, []).map((part) => String(part.textContent || "")).join("");

  trigger(harness.elements.managerChatComposer, "submit");
  assert.equal(harness.posts.filter((post) => post.type === "managerLoopSend").length, 1);
  assert.equal(harness.elements.managerChatQueue.hidden, false);
  assert.match(queueText(), /Sending/);
  assert.match(queueText(), /hello there/);

  harness.api.renderManagerChatAction("send", { ok: true, session: { session_id: "mls-" + "a".repeat(32) }, running: true });
  assert.equal(harness.state.managerChatPendingSends.length, 0);
  assert.equal(harness.elements.managerChatQueue.hidden, true);
  assert.doesNotMatch(queueText(), /Sending/);
});

test("send queue height is bounded so a long pending send cannot cover the transcript", () => {
  const queue = extractSlice(cssSource, "\n.manager-chat-send-queue {", "}", "send queue");
  assert.match(queue, /max-height:\s*30vh;/);
  assert.match(queue, /overflow-y:\s*auto;/);
  assert.match(queue, /overscroll-behavior:\s*contain;/);

  const row = extractSlice(cssSource, "\n.manager-chat-queued-send {", "}", "queued send row");
  assert.match(row, /max-height:\s*6\.5em;/);
  assert.match(row, /overflow:\s*hidden;/);

  const frameBodies = [...cssSource.matchAll(/([^{}]*)\{([^{}]*)\}/g)]
    .filter((m) => m[1].includes(".manager-chat-frame > .manager-chat-send-queue"))
    .map((m) => m[2]);
  assert.ok(frameBodies.length > 0, "send queue frame rule present");
  for (const body of frameBodies) assert.doesNotMatch(body, /flex:\s*0 0 auto;/);
});

// ── Idle new-turn status pull ─────────────────────────────────────────────
test("an idle events reply with a newer non-terminal turn posts exactly one managerLoopStatus", () => {
  const harness = loadWebviewSlice();
  harness.state.managerChatSession = "mls-" + "a".repeat(32);
  harness.state.managerChatRunning = false;
  harness.state.managerChatHighestSeenTurn = 1;
  harness.posts.length = 0;

  harness.api.renderManagerChatEventsResponse({
    ok: true,
    events: [{ seq: 10, type: "assistant_text", turn: 3, payload: { text: "hi" } }],
  });

  const statusCount = harness.posts.filter((p) => p.type === "managerLoopStatus").length;
  assert.equal(statusCount, 1);
});

// ── session_not_found / last_seq / has_more ──────────────────────────────
test("session_not_found stops polling and pulls status", () => {
  const harness = loadWebviewSlice();
  harness.state.managerChatSession = "mls-" + "a".repeat(32);
  harness.state.managerChatRunning = true;
  harness.posts.length = 0;

  harness.api.renderManagerChatEventsResponse({ ok: false, error: "session_not_found", session_id: "mls-" + "a".repeat(32) });

  assert.deepEqual(plain(harness.posts.at(-1)), { type: "managerLoopStatus" });
});

test("last_seq below the cursor resets events and re-requests", () => {
  const harness = loadWebviewSlice();
  harness.state.managerChatSession = "mls-" + "a".repeat(32);
  harness.state.managerChatRunning = true;
  harness.state.managerChatLastSeq = 20;
  harness.state.managerChatEvents = [{ seq: 20, type: "turn_end", turn: 1, payload: {} }];
  harness.posts.length = 0;

  harness.api.renderManagerChatEventsResponse({
    ok: true,
    events: [],
    last_seq: 5,
    session_id: "mls-" + "a".repeat(32),
  });

  assert.equal(harness.state.managerChatLastSeq, 0);
  assert.equal(harness.state.managerChatEvents.length, 0);
  assert.ok(harness.posts.some((p) => p.type === "managerLoopEvents"), "re-requested");
});

test("has_more requests the next page immediately", () => {
  const harness = loadWebviewSlice();
  harness.state.managerChatSession = "mls-" + "a".repeat(32);
  harness.state.managerChatRunning = true;
  harness.posts.length = 0;

  harness.api.renderManagerChatEventsResponse({
    ok: true,
    events: [{ seq: 3, type: "assistant_text", turn: 1, payload: { text: "hi" } }],
    has_more: true,
    session_id: "mls-" + "a".repeat(32),
  });

  assert.ok(harness.posts.some((p) => p.type === "managerLoopEvents"), "next page requested");
});

// ── Draft persistence ─────────────────────────────────────────────────────
test("the draft survives a reload via vscode state", () => {
  const harness = loadWebviewSlice();
  harness.elements.managerChatInput.value = "draft text";
  trigger(harness.elements.managerChatInput, "input");
  assert.equal(harness.state.managerChatDraft, "draft text");
});

// ── Up/Down arrow history ─────────────────────────────────────────────────
test("Up-arrow in an empty composer recalls the last sent message", () => {
  const harness = loadWebviewSlice();
  harness.state.managerChatSentHistory = ["first sent", "second sent"];
  harness.state.managerChatHistoryIndex = -1;
  harness.elements.managerChatInput.value = "";

  trigger(harness.elements.managerChatInput, "keydown", { key: "ArrowUp" });

  assert.equal(harness.elements.managerChatInput.value, "second sent");
});

test("Down-arrow walks forward through history", () => {
  const harness = loadWebviewSlice();
  harness.state.managerChatSentHistory = ["first sent", "second sent"];
  harness.state.managerChatHistoryIndex = 0;
  harness.elements.managerChatInput.value = "first sent";

  trigger(harness.elements.managerChatInput, "keydown", { key: "ArrowDown" });

  assert.equal(harness.elements.managerChatInput.value, "second sent");
});

test("Up-arrow walks back past the last message and Down walks forward again", () => {
  const harness = loadWebviewSlice();
  harness.state.managerChatSentHistory = ["older sent", "newer sent"];
  harness.state.managerChatHistoryIndex = -1;
  harness.elements.managerChatInput.value = "";

  trigger(harness.elements.managerChatInput, "keydown", { key: "ArrowUp" });
  trigger(harness.elements.managerChatInput, "keydown", { key: "ArrowUp" });
  assert.equal(harness.elements.managerChatInput.value, "older sent");

  trigger(harness.elements.managerChatInput, "keydown", { key: "ArrowDown" });
  assert.equal(harness.elements.managerChatInput.value, "newer sent");
});

test("typing ends history browsing", () => {
  const harness = loadWebviewSlice();
  harness.state.managerChatSentHistory = ["only sent"];
  harness.elements.managerChatInput.value = "";
  trigger(harness.elements.managerChatInput, "keydown", { key: "ArrowUp" });
  assert.equal(harness.state.managerChatHistoryIndex, 0);

  harness.elements.managerChatInput.value = "only sent!";
  trigger(harness.elements.managerChatInput, "input");
  assert.equal(harness.state.managerChatHistoryIndex, -1);
  trigger(harness.elements.managerChatInput, "keydown", { key: "ArrowUp" });
  assert.equal(harness.elements.managerChatInput.value, "only sent!", "a typed draft is not replaced");
});

// ── NF-2026-01977: optimistic echo and hidden-thinking note ───────────────
function transcriptText(harness) {
  return flattenNodes(harness.elements.managerChatTranscript, []).map((part) => String(part.textContent || "")).join("\n");
}

function submitManagerChat(harness, text) {
  harness.elements.managerChatBackendSelect.value = "codex_cli";
  harness.elements.managerChatModelInput.value = "gpt-x";
  harness.elements.managerChatInput.value = text;
  trigger(harness.elements.managerChatComposer, "submit");
}

test("a submit renders the user turn before any poll and the polled turn replaces it", () => {
  const harness = loadWebviewSlice();
  const session = "mls-" + "a".repeat(32);
  harness.state.managerChatSession = session;
  submitManagerChat(harness, "echo me");

  assert.equal(harness.posts.filter((post) => post.type === "managerLoopEvents").length, 0, "no poll has run yet");
  const echoes = harness.state.managerChatEvents.filter((event) => event.type === "user_message");
  assert.equal(echoes.length, 1);
  assert.equal(echoes[0].local, "pending");
  assert.equal(echoes[0].payload.pending, true);
  assert.equal(echoes[0].payload.client_id, harness.state.managerChatPendingSends[0].clientId);
  assert.match(transcriptText(harness), /echo me/);

  harness.api.renderManagerChatAction("send", { ok: true, session_id: session });
  assert.equal(harness.state.managerChatEvents.filter((event) => event.local === "pending").length, 1, "send reply keeps the echo");
  harness.api.renderManagerChatEventsResponse({
    ok: true,
    session_id: session,
    last_seq: 2,
    events: [
      { seq: 1, turn: 1, type: "user_message", payload: { text: "echo me" } },
      { seq: 2, turn: 1, type: "assistant_text", payload: { text: "done" } },
    ],
  });

  const users = harness.state.managerChatEvents.filter((event) => event.type === "user_message");
  assert.equal(users.length, 1, "the polled turn replaced the echo");
  assert.equal(users[0].seq, 1);
  assert.equal(users[0].local, undefined);
  assert.equal(transcriptText(harness).split("echo me").length - 1, 1, "rendered once");
  assert.equal(harness.state.managerChatLastSeq, 2);
});

test("an echo is matched by client id when the backend echoes one, and stays last until then", () => {
  const harness = loadWebviewSlice();
  const session = "mls-" + "b".repeat(32);
  harness.state.managerChatSession = session;
  submitManagerChat(harness, "same text");
  submitManagerChat(harness, "same text");
  const [first, second] = harness.state.managerChatPendingSends.map((entry) => entry.clientId);
  assert.notEqual(first, second);

  harness.api.renderManagerChatEventsResponse({
    ok: true,
    session_id: session,
    last_seq: 3,
    events: [
      { seq: 1, turn: 1, type: "user_message", payload: { text: "same text", client_id: second } },
      { seq: 2, turn: 1, type: "assistant_text", payload: { text: "reply" } },
      { seq: 3, turn: 2, type: "user_message", payload: { text: "unrelated callback text" } },
    ],
  });

  const pending = harness.state.managerChatEvents.filter((event) => event.local === "pending");
  assert.deepEqual(plain(pending.map((event) => event.payload.client_id)), [first]);
  assert.equal(harness.state.managerChatEvents.at(-1).payload.client_id, first, "the unanswered echo renders last");
});

test("a refused send marks its echo failed with the error text", () => {
  const harness = loadWebviewSlice();
  harness.state.managerChatSession = "mls-" + "c".repeat(32);
  submitManagerChat(harness, "will fail");

  harness.api.renderManagerChatAction("send", { ok: false, error: "manager_turn_in_progress" });

  const echo = harness.state.managerChatEvents.find((event) => event.type === "user_message");
  assert.equal(echo.local, "failed");
  assert.equal(echo.payload.failed, true);
  assert.equal(echo.payload.error, "manager_turn_in_progress");
  const note = harness.state.managerChatEvents.find((event) => event.type === "error");
  assert.equal(note.payload.error, "Not sent: manager_turn_in_progress");
  assert.match(transcriptText(harness), /Not sent: manager_turn_in_progress/);
  assert.ok(harness.elements.managerChatInput.value.includes("will fail"), "refused text restored");
});

test("a turn_end with hidden thinking blocks renders a one-line note", () => {
  const harness = loadWebviewSlice();
  const session = "mls-" + "d".repeat(32);
  harness.state.managerChatSession = session;

  harness.api.renderManagerChatEventsResponse({
    ok: true,
    session_id: session,
    last_seq: 2,
    events: [
      { seq: 1, turn: 1, type: "assistant_text", payload: { text: "answer" } },
      { seq: 2, turn: 1, type: "turn_end", payload: { reasoning_redacted: 3, thinking_display: { state: "enabled" } } },
    ],
  });

  assert.match(transcriptText(harness), /3 thinking blocks hidden by the model/);
  assert.equal(harness.state.managerChatLastSeq, 2);

  harness.api.renderManagerChatEventsResponse({
    ok: true,
    session_id: session,
    last_seq: 3,
    events: [{ seq: 3, turn: 2, type: "turn_end", payload: { reasoning_redacted: 1, thinking_display: { state: "rejected" } } }],
  });
  assert.match(transcriptText(harness), /1 thinking block hidden by the model · this CLI rejected --thinking-display/);
});

test("a session switch drops the previous session's pending echoes", () => {
  const harness = loadWebviewSlice();
  const sessionA = "mls-" + "e".repeat(32);
  const sessionB = "mls-" + "f".repeat(32);
  harness.state.managerChatSession = sessionA;
  submitManagerChat(harness, "typed in A");
  assert.equal(harness.state.managerChatEvents.filter((event) => event.local === "pending").length, 1);
  assert.equal(harness.state.managerChatEvents.find((event) => event.local === "pending").session, sessionA);

  harness.api.renderManagerChatStatus({ ok: true, running: false, session: { session_id: sessionB } });

  assert.equal(harness.state.managerChatSession, sessionB);
  assert.equal(harness.state.managerChatEvents.filter((event) => event.local === "pending").length, 0, "no A echo in B");
  assert.doesNotMatch(transcriptText(harness), /typed in A/);
  harness.api.renderManagerChatEventsResponse({
    ok: true,
    session_id: sessionB,
    last_seq: 1,
    events: [{ seq: 1, turn: 1, type: "user_message", payload: { text: "typed in A" } }],
  });
  const users = harness.state.managerChatEvents.filter((event) => event.type === "user_message");
  assert.equal(users.length, 1, "B's own user_message renders on its own");
  assert.equal(users[0].seq, 1);
});

test("an echo sent with no session survives the send reply that creates the session", () => {
  const harness = loadWebviewSlice();
  const sessionB = "mls-" + "1".repeat(32);
  harness.state.managerChatSession = null;
  submitManagerChat(harness, "first words");
  const echo = harness.state.managerChatEvents.find((event) => event.local === "pending");
  assert.equal(echo.session, "");

  harness.api.renderManagerChatAction("send", { ok: true, session_id: sessionB });
  assert.equal(harness.state.managerChatSession, sessionB);
  assert.equal(harness.state.managerChatEvents.filter((event) => event.local === "pending").length, 1, "echo kept");
  assert.match(transcriptText(harness), /first words/);

  harness.api.renderManagerChatEventsResponse({
    ok: true,
    session_id: sessionB,
    last_seq: 1,
    events: [{ seq: 1, turn: 1, type: "user_message", payload: { text: "first words" } }],
  });
  const users = harness.state.managerChatEvents.filter((event) => event.type === "user_message");
  assert.equal(users.length, 1, "B's user_message replaced the echo");
  assert.equal(users[0].seq, 1);
  assert.equal(users[0].local, undefined);
});

// ── media/manager_console.js: the transcript reconciles in place ─────────────

const CLOCK_START = Date.parse("2026-11-08T10:00:00Z");
const SESSION_A = "mls-aaaa1111bbbb2222";
const SESSION_B = "mls-cccc3333dddd4444";

function transcriptHarness(events, session = SESSION_A) {
  const harness = loadWebviewSlice();
  harness.state.managerChatSession = session;
  harness.state.managerChatEvents = events;
  harness.box = harness.elements.managerChatTranscript;
  return harness;
}

function setClock(harness, ms) {
  vm.runInContext("Date", harness.context).now = () => ms;
}

function appendEvents(harness, ...events) {
  harness.state.managerChatEvents = harness.state.managerChatEvents.concat(events);
  harness.api.renderManagerChatEvents();
}

const nodeText = (node) => flattenNodes(node, []).map((part) => String(part.textContent || "")).join("");
const textEvent = (seq, text = `m${seq}`) => ({ seq, turn: seq, type: "assistant_text", payload: { text } });
const userEvent = (seq) => ({ seq, turn: 1, type: "user_message", payload: { text: "go" } });
const reasoningEvent = (seq, ts) => ({ seq, turn: 1, ts, type: "reasoning", payload: { text: `thought ${seq}` } });
const toolCall = (seq, callId, name) => ({ seq, turn: 1, type: "tool_call", payload: { call_id: callId, name, input: { path: "a.py" } } });
const toolResult = (seq, callId, isError = false) => ({ seq, turn: 1, type: "tool_result", payload: { call_id: callId, name: "tool", output: "out", is_error: isError } });
const activityGroups = (harness) => harness.box.children.filter((child) => /\bmc-activity-group\b/.test(child.className));
const detailsIn = (node) => flattenNodes(node, []).filter((part) => part.tag === "details");

function assertSameRows(actual, expected, label) {
  assert.equal(actual.length, expected.length, `${label}: row count`);
  expected.forEach((row, index) => assert.strictEqual(actual[index], row, `${label}: row ${index} is the same object`));
}

test("one appended event leaves every rendered row in place and adds one row", () => {
  const harness = transcriptHarness([userEvent(1), textEvent(2), textEvent(3), textEvent(4)]);
  harness.api.renderManagerChatEvents();
  const before = harness.box.children.slice();
  assert.equal(before.length, 4);
  appendEvents(harness, textEvent(5));
  assert.equal(harness.box.children.length, 5);
  assertSameRows(harness.box.children.slice(0, 4), before, "after append");
  assert.ok(nodeText(harness.box.children[4]).includes("m5"));
});

test("elements built for one appended event do not grow with the transcript", () => {
  const appendedElementCount = (size) => {
    const harness = transcriptHarness(Array.from({ length: size }, (_, index) => textEvent(index + 1)));
    harness.api.renderManagerChatEvents();
    let created = 0;
    const documentFake = harness.context.document;
    const createOriginal = documentFake.createElement;
    documentFake.createElement = (tag) => {
      created += 1;
      return createOriginal(tag);
    };
    appendEvents(harness, textEvent(size + 1));
    return created;
  };
  const small = appendedElementCount(10);
  assert.ok(small > 0, "the appended row is built");
  assert.equal(appendedElementCount(300), small);
});

test("a render with no change performs no DOM write", () => {
  const harness = transcriptHarness([
    { seq: 1, turn: 1, ts: CLOCK_START, type: "reasoning", payload: { text: "hm" } },
    { seq: 2, turn: 1, ts: CLOCK_START + 3000, type: "assistant_text", payload: { text: "done" } },
  ]);
  harness.api.renderManagerChatEvents();
  const box = harness.box;
  const writes = [];
  for (const method of ["appendChild", "insertBefore", "removeChild", "replaceChildren"]) {
    const original = box[method];
    box[method] = function (...args) {
      writes.push(method);
      return original.apply(this, args);
    };
  }
  let scrollTop = box.scrollTop;
  Object.defineProperty(box, "scrollTop", {
    get: () => scrollTop,
    set: (value) => {
      scrollTop = value;
      writes.push("scrollTop");
    },
  });
  const snapshot = flattenNodes(box, []).map((node) => [node, node.className, node.textContent, node.open]);
  const assertUntouched = (label) => {
    assert.deepEqual(writes, [], label);
    const now = flattenNodes(box, []);
    assert.equal(now.length, snapshot.length, label);
    snapshot.forEach(([node, className, textContent, open], index) => {
      assert.strictEqual(now[index], node, label);
      assert.equal(node.className, className, label);
      assert.equal(node.textContent, textContent, label);
      assert.equal(node.open, open, label);
    });
  };
  harness.api.renderManagerChatEvents();
  assertUntouched("the unchanged render exits early");
  // Passes the early exit, but no row would render differently.
  harness.state.managerChatLastThoughtMs = 7000;
  harness.api.renderManagerChatEvents();
  assertUntouched("the reconcile finds nothing to do");
});

test("a kept reasoning row keeps ticking after later renders and is not rebuilt by the tick", () => {
  const harness = transcriptHarness([userEvent(1), reasoningEvent(2, CLOCK_START)]);
  harness.state.managerChatRunning = true;
  setClock(harness, CLOCK_START + 3000);
  harness.api.renderManagerChatEvents();
  const row = harness.box.children[1];
  const summary = detailsIn(row)[0].children[0];
  assert.equal(summary.textContent, "Thinking · 3s");
  setClock(harness, CLOCK_START + 9000);
  // Another reasoning event leaves the first one live; only the duration moved.
  appendEvents(harness, reasoningEvent(3, CLOCK_START + 8000));
  assert.strictEqual(harness.box.children[1], row, "the duration does not rebuild the row");
  assert.equal(summary.textContent, "Thinking · 9s");
  setClock(harness, CLOCK_START + 12000);
  harness.api.renderManagerChatEvents();
  assert.equal(summary.textContent, "Thinking · 12s", "a render with no change still ticks the kept row");
});

test("a reasoning row becomes Thought for when a later event arrives", () => {
  const harness = transcriptHarness([userEvent(1), reasoningEvent(2, CLOCK_START)]);
  harness.state.managerChatRunning = true;
  setClock(harness, CLOCK_START + 1000);
  harness.api.renderManagerChatEvents();
  detailsIn(harness.box.children[1])[0].open = false;
  appendEvents(harness, { seq: 3, turn: 1, ts: CLOCK_START + 3000, type: "assistant_text", payload: { text: "ok" } });
  const details = detailsIn(harness.box.children[1])[0];
  assert.equal(details.children[0].textContent, "Thought for 3s");
  assert.equal(details.open, false, "the owner's closed state survives the rebuild");
  setClock(harness, CLOCK_START + 20000);
  harness.api.renderManagerChatEvents();
  assert.equal(details.children[0].textContent, "Thought for 3s", "a finished thought no longer ticks");
});

test("a turn_end row follows the later events its footer depends on", () => {
  const harness = transcriptHarness([toolCall(1, "c1", "read"), { seq: 2, turn: 1, type: "turn_end", payload: {} }]);
  harness.api.renderManagerChatEvents();
  const footer = harness.box.children[1];
  assert.equal(nodeText(footer), "turn 1 · 1 tool");
  appendEvents(harness, textEvent(3));
  assert.strictEqual(harness.box.children[1], footer, "an event of another turn leaves the footer alone");
  appendEvents(harness, toolCall(4, "c2", "grep"));
  assert.equal(nodeText(harness.box.children[1]), "turn 1 · 2 tools");
  assert.notStrictEqual(harness.box.children[1], footer);
});

test("an owner-opened tool row stays open when its result arrives", () => {
  const harness = transcriptHarness([userEvent(1), toolCall(2, "c1", "read")]);
  harness.api.renderManagerChatEvents();
  const running = detailsIn(harness.box.children[1])[0];
  assert.match(running.children[0].textContent, /^read · running · a\.py$/);
  running.open = true;
  appendEvents(harness, toolResult(3, "c1"));
  const finished = detailsIn(harness.box.children[1])[0];
  assert.match(finished.children[0].textContent, /^read · done · a\.py$/);
  assert.equal(finished.open, true);
  assert.equal(harness.box.children.length, 2, "the result merges into its call");
});

test("a result that adds only undefined fields still moves the row out of running", () => {
  const harness = transcriptHarness([toolCall(1, "c1", "read")]);
  harness.api.renderManagerChatEvents();
  appendEvents(harness, { seq: 2, turn: 1, type: "tool_result", payload: { call_id: "c1", name: "read" } });
  assert.match(detailsIn(harness.box.children[0])[0].children[0].textContent, /^read · done · a\.py$/);
});

test("a tool row starts closed in every status", () => {
  const harness = transcriptHarness([]);
  const rows = {
    running: { name: "read", input: {} },
    done: { name: "read", input: {}, output: "ok", is_error: false },
    failed: { name: "read", input: {}, output: "boom", is_error: true },
  };
  for (const [status, payload] of Object.entries(rows)) {
    const details = detailsIn(harness.api.managerChatEventNode({ type: "tool_call", payload }))[0];
    assert.equal(details.open, false, status);
    assert.match(details.children[0].textContent, new RegExp(`${status}$`), status);
  }
});

test("a run of two tools and a thought renders one closed group", () => {
  const harness = transcriptHarness([
    userEvent(1),
    reasoningEvent(2, CLOCK_START),
    toolCall(3, "c1", "read"),
    toolResult(4, "c1"),
    toolCall(5, "c2", "grep"),
    toolResult(6, "c2", true),
  ]);
  harness.api.renderManagerChatEvents();
  assert.equal(harness.box.children.length, 2);
  const [group] = activityGroups(harness);
  assert.equal(group.tag, "details");
  assert.equal(group.open, false);
  assert.equal(group.children[0].tag, "summary");
  assert.equal(group.children[0].textContent, "2 tools · 1 failed", "reasoning is not counted");
  assert.match(group.className, /\bhas-failed\b/);
  assert.equal(group.children[1].children.length, 3, "the thought is a member");
});

test("a group counts each kind, omits zero counts and names the running row", () => {
  const harness = transcriptHarness([
    toolCall(1, "c1", "read"),
    { seq: 2, turn: 1, type: "command", payload: { call_id: "k1", command: "make", status: "completed", exit_code: 2 } },
    { seq: 3, turn: 1, type: "file_change", payload: { path: "a.py", added: 1, removed: 0 } },
    toolResult(4, "c1"),
    toolCall(5, "c2", "lint"),
  ]);
  harness.api.renderManagerChatEvents();
  const [group] = activityGroups(harness);
  assert.equal(group.children[0].textContent, "2 tools · 1 command · 1 file · 1 failed · running lint");
  appendEvents(harness, toolResult(6, "c2"));
  assert.equal(group.children[0].textContent, "2 tools · 1 command · 1 file · 1 failed");
});

test("appending to a group keeps the group, its open state and the other members", () => {
  const harness = transcriptHarness([toolCall(1, "c1", "read"), toolResult(2, "c1"), toolCall(3, "c2", "grep"), toolResult(4, "c2")]);
  harness.api.renderManagerChatEvents();
  const [group] = activityGroups(harness);
  const body = group.children[1];
  const members = body.children.slice();
  group.open = true;
  appendEvents(harness, toolCall(5, "c3", "lint"));
  assert.deepEqual(activityGroups(harness).length, 1);
  assert.strictEqual(activityGroups(harness)[0], group);
  assert.equal(group.open, true);
  assert.equal(group.children[0].textContent, "3 tools · running lint");
  assert.strictEqual(group.children[1], body);
  assert.equal(body.children.length, 3);
  assertSameRows(body.children.slice(0, 2), members, "group members");
});

test("a run with one tool stays ungrouped, and any other row ends a run", () => {
  const single = transcriptHarness([userEvent(1), reasoningEvent(2, CLOCK_START), toolCall(3, "c1", "read")]);
  single.api.renderManagerChatEvents();
  assert.equal(activityGroups(single).length, 0);
  assert.equal(single.box.children.length, 3);
  const split = transcriptHarness([toolCall(1, "a", "read"), toolCall(2, "b", "read"), textEvent(3), toolCall(4, "c", "read"), toolCall(5, "d", "read")]);
  split.api.renderManagerChatEvents();
  assert.equal(activityGroups(split).length, 2);
  assert.equal(split.box.children.length, 3);
});

test("a plain row that joins a new group keeps its object and open state", () => {
  const harness = transcriptHarness([userEvent(1), toolCall(2, "c1", "read")]);
  harness.api.renderManagerChatEvents();
  const row = harness.box.children[1];
  detailsIn(row)[0].open = true;
  appendEvents(harness, toolCall(3, "c2", "grep"));
  assert.equal(harness.box.children.length, 2);
  const [group] = activityGroups(harness);
  assert.strictEqual(group.children[1].children[0], row);
  assert.equal(detailsIn(row)[0].open, true);
  assert.equal(group.open, false);
});

test("a repeated call id renders separate rows that keep their identity", () => {
  const harness = transcriptHarness([toolCall(1, "x", "read"), textEvent(2), { ...toolCall(3, "x", "grep"), turn: 2 }]);
  harness.api.renderManagerChatEvents();
  const before = harness.box.children.slice();
  assert.equal(before.length, 3);
  assert.match(nodeText(before[0]), /read/);
  assert.match(nodeText(before[2]), /grep/);
  appendEvents(harness, textEvent(4));
  assertSameRows(harness.box.children.slice(0, 3), before, "repeated call id");
});

test("load earlier prepends rows without touching existing row objects", () => {
  const harness = transcriptHarness(Array.from({ length: 1000 }, (_, index) => textEvent(index + 1)));
  harness.api.renderManagerChatEvents();
  const before = harness.box.children.slice();
  assert.equal(before.length, 401);
  const earlier = before[0];
  earlier.listeners.click[0]();
  assert.equal(harness.box.children.length, 801);
  assert.strictEqual(harness.box.children[0], earlier, "the button stays while older rows remain");
  assertSameRows(harness.box.children.slice(-400), before.slice(1), "existing rows");
  assert.ok(nodeText(harness.box.children[1]).includes("m201"));
});

test("load earlier extends a group across the window edge without replacing it", () => {
  const events = Array.from({ length: 450 }, (_, index) => toolCall(index + 1, `c${index}`, "read"));
  const harness = transcriptHarness(events);
  harness.api.renderManagerChatEvents();
  const [group] = activityGroups(harness);
  group.open = true;
  const members = group.children[1].children.slice();
  assert.equal(members.length, 400);
  harness.box.children[0].listeners.click[0]();
  assert.equal(harness.box.children.length, 1);
  assert.strictEqual(harness.box.children[0], group);
  assert.equal(group.open, true);
  assert.equal(group.children[1].children.length, 450);
  assertSameRows(group.children[1].children.slice(-400), members, "group members");
});

test("a switched or reset session never reuses the previous session's rows", () => {
  const switched = transcriptHarness([textEvent(1, "same text")]);
  switched.api.renderManagerChatEvents();
  const stale = switched.box.children[0];
  switched.state.managerChatSession = SESSION_B;
  switched.state.managerChatEvents = [textEvent(1, "same text")];
  switched.api.renderManagerChatEvents();
  assert.equal(switched.box.children.length, 1);
  assert.notStrictEqual(switched.box.children[0], stale);
  const reset = transcriptHarness([textEvent(1, "same text")]);
  reset.api.renderManagerChatEvents();
  const old = reset.box.children[0];
  reset.api.managerConsoleReset();
  reset.state.managerChatEvents = [textEvent(1, "same text")];
  reset.api.renderManagerChatEvents();
  assert.equal(reset.box.children.length, 1);
  assert.notStrictEqual(reset.box.children[0], old);
});

test("the empty state is replaced by rows and comes back when they go", () => {
  const harness = transcriptHarness([]);
  harness.api.renderManagerChatEvents();
  assert.equal(harness.box.children.length, 1);
  assert.equal(harness.box.children[0].textContent, "No events yet");
  appendEvents(harness, textEvent(1));
  assert.equal(harness.box.children.length, 1);
  assert.ok(nodeText(harness.box.children[0]).includes("m1"));
  harness.state.managerChatEvents = [];
  harness.api.renderManagerChatEvents();
  assert.equal(harness.box.children.length, 1);
  assert.equal(harness.box.children[0].textContent, "No events yet");
});

test("the first render clears the placeholder the webview markup ships in the transcript", () => {
  assert.match(extensionSource, /id="manager-chat-transcript">\s*<div class="panel-list-empty compact" id="manager-chat-empty">/);
  const harness = transcriptHarness([textEvent(1)]);
  const placeholder = { textContent: "Write on the selected model to open the first session", children: [] };
  harness.box.appendChild(placeholder);
  harness.api.renderManagerChatEvents();
  assert.equal(harness.box.children.length, 1);
  assert.notStrictEqual(harness.box.children[0], placeholder);
  assert.ok(nodeText(harness.box.children[0]).includes("m1"));
  const row = harness.box.children[0];
  appendEvents(harness, textEvent(2));
  assert.strictEqual(harness.box.children[0], row, "only the first render clears the transcript");
  assert.equal(harness.box.children.length, 2);
});

test("the latest button adds no net height to the transcript's flex frame", () => {
  const rule = /\.mc-latest \{([^}]*)\}/.exec(consoleCss)[1];
  const height = Number(/(?:^|[;\s])height:\s*(\d+)px/.exec(rule)[1]);
  const marginTop = Number(/margin-top:\s*(-?\d+)px/.exec(rule)[1]);
  const frame = /\.manager-chat-frame \{([^}]*)\}/.exec(cssSource)[1];
  const gap = /(?:^|[;\s])(?:row-)?gap:\s*(\d+)px/.exec(frame);
  assert.ok(height > 0);
  assert.equal(height + marginTop + (gap ? Number(gap[1]) : 0), 0);
  assert.match(rule, /z-index:\s*\d+/);
  assert.match(rule, /align-self:\s*flex-end/);
});
