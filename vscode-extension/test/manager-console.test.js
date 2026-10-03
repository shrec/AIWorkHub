"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const appSource = fs.readFileSync(path.join(__dirname, "..", "media", "app.js"), "utf8");
const consoleSource = fs.readFileSync(path.join(__dirname, "..", "media", "manager_console.js"), "utf8");

function slice(source, start, end) {
  const from = source.indexOf(start);
  const to = source.indexOf(end, from);
  assert.ok(from !== -1 && to !== -1, `slice ${start}`);
  return source.slice(from, to + end.length);
}

function fakeElement(tag) {
  return {
    tag, className: "", textContent: "", children: [], attrs: {}, listeners: {}, style: {}, hidden: false,
    scrollTop: 0, scrollHeight: 0, clientHeight: 0,
    classList: { values: new Set(), toggle(name, on) { on ? this.values.add(name) : this.values.delete(name); } },
    setAttribute(name, value) { this.attrs[name] = String(value); },
    appendChild(child) { this.children.push(child); return child; },
    replaceChildren(...nodes) { this.children = nodes.length === 1 && nodes[0].__isFragment ? nodes[0].children.slice() : nodes; },
    addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); },
    remove() { this.removed = true; },
  };
}

function load(events = []) {
  const elements = { managerChatTranscript: fakeElement("div"), managerChatHairline: fakeElement("div"), managerChatContext: fakeElement("span") };
  elements.managerChatHairline.children.push(fakeElement("span"));
  const opened = [];
  const context = {
    document: {
      createElement: (tag) => fakeElement(tag),
      createTextNode: (text) => ({ tag: "#text", textContent: String(text), children: [] }),
      createDocumentFragment: () => ({ __isFragment: true, children: [], appendChild(child) { this.children.push(child); return child; } }),
    },
    state: { managerChatEvents: events, managerChatSession: "mls-fixture", managerChatRunning: false },
    elements,
    requestTaskDetail: (id) => opened.push(id),
    Date,
  };
  vm.createContext(context);
  // The real app.js utilities and TASK_ID_RE, sliced by their own first and last lines.
  const utilities = [
    slice(appSource, "const TASK_ID_RE = ", ";\n"),
    slice(appSource, "function createElement(tag, className, text) {", "return element;\n}"),
    slice(appSource, "function asArray(value) {", "return Array.isArray(value) ? value : [];\n}"),
    slice(appSource, "function numberValue(value) {", "return Number.isFinite(parsed) ? parsed : 0;\n}"),
    slice(appSource, "function limitText(value, maxLength = 120) {", "return `${text.slice(0, Math.max(0, maxLength - 1)).trimEnd()}...`;\n}"),
  ].join("\n");
  vm.runInContext(`${utilities}\n${consoleSource}\nthis.api = { managerChatEventNode, renderManagerChatEvents, managerConsoleMergeCommands, managerConsoleCompact, managerConsoleFooterText, managerConsoleTaskOf, managerConsoleApplyHairline };`, context);
  return { api: context.api, elements, opened, state: context.state };
}

function flat(node, out = []) {
  if (!node) return out;
  out.push(node);
  for (const child of node.children || []) flat(child, out);
  return out;
}

const text = (node) => flat(node).map((item) => item.children && item.children.length ? "" : String(item.textContent || "")).join("");

test("commands merge by call id at the first position with the last payload", () => {
  const { api } = load();
  const merged = api.managerConsoleMergeCommands([
    { seq: 1, type: "command", payload: { call_id: "c1", command: "git --version", status: "running" } },
    { seq: 2, type: "assistant_text", payload: { text: "hi" } },
    { seq: 3, type: "command", payload: { call_id: "c1", command: "git --version", status: "completed", exit_code: 0, output_tail: "git version 2.46" } },
  ]);
  assert.deepEqual(Array.from(merged, (e) => e.seq), [3, 2]);
  assert.equal(merged[0].payload.status, "completed");
});

test("long command output collapses to its last 20 lines", () => {
  const { api } = load();
  const lines = Array.from({ length: 30 }, (_, i) => `line ${i + 1}`).join("\n");
  const node = api.managerChatEventNode({ type: "command", payload: { call_id: "c", command: "seq 30", status: "completed", exit_code: 0, output_tail: lines } });
  const all = flat(node);
  const pre = all.find((n) => n.tag === "pre");
  assert.ok(pre.textContent.startsWith("line 11") && pre.textContent.endsWith("line 30"));
  const more = all.find((n) => n.tag === "button");
  assert.equal(more.textContent, "show all (30 lines)");
  more.listeners.click[0]();
  assert.ok(pre.textContent.startsWith("line 1\n"));
  const cut = api.managerChatEventNode({ type: "command", payload: { command: "echo cut", output_tail: "cut", truncated: true, original_bytes: 123 } });
  assert.ok(text(cut).includes("output cut, 123 bytes in total"));
});

test("a file change renders a coloured diff and collapses beyond 40 lines", () => {
  const { api } = load();
  const diff = ["--- a/x", "+++ b/x", "@@ -1 +1 @@", "-old", "+new", ...Array.from({ length: 40 }, (_, i) => ` ctx ${i}`)].join("\n");
  const node = api.managerChatEventNode({ type: "file_change", payload: { call_id: "e", path: "src/x.py", kind: "update", diff, added: 1, removed: 1 } });
  const all = flat(node);
  assert.ok(text(node).includes("src/x.py (+1 −1)"));
  assert.ok(all.some((n) => n.className === "mc-add" && n.textContent === "+new\n"));
  assert.ok(all.some((n) => n.className === "mc-del" && n.textContent === "-old\n"));
  assert.equal(all.filter((n) => /^mc-(add|del|hunk|ctx)$/.test(n.className)).length, 40);
  const more = all.find((n) => n.tag === "button");
  assert.equal(more.textContent, "show all (45 lines)");
  more.listeners.click[0]();
  assert.equal(flat(node).filter((n) => /^mc-(add|del|hunk|ctx)$/.test(n.className)).length, 45);
  const cut = api.managerChatEventNode({ type: "file_change", payload: { path: "src/x.py", diff: "+cut", truncated: true, original_bytes: 987 } });
  assert.ok(text(cut).includes("diff cut, 987 bytes in total"));
});

test("footer text handles normalized, legacy and missing usage", () => {
  const events = [
    { seq: 1, turn: 7, at: "2026-09-27T10:00:00Z", type: "user_message", payload: { text: "go" } },
    ...Array.from({ length: 12 }, (_, i) => ({ seq: 2 + i, turn: 7, at: "2026-09-27T10:00:10Z", type: "tool_call", payload: { call_id: `t${i}`, name: "Read" } })),
  ];
  const { api } = load(events);
  const end = (usage) => ({ seq: 99, turn: 7, at: "2026-09-27T10:00:41Z", type: "turn_end", payload: usage ? { usage } : {} });
  assert.equal(api.managerConsoleFooterText(end({ input: 3100, cache_read: 80000, cache_write: 8000, output: 1200 })), "turn 7 · 12 tools · in 3.1k · cache 88k · out 1.2k · 41s");
  assert.equal(api.managerConsoleFooterText(end({ input_tokens: 3100, cache_read_input_tokens: 80000, cache_creation_input_tokens: 8000, output_tokens: 1200 })), "turn 7 · 12 tools · in 3.1k · cache 88k · out 1.2k · 41s");
  assert.equal(api.managerConsoleFooterText(end(null)), "turn 7 · 12 tools · 41s");
  assert.equal(api.managerConsoleCompact(950), "950");
  assert.equal(api.managerConsoleCompact(1_250_000), "1.3M");
});

test("a task id in a tool result becomes a task block that opens the task", () => {
  const { api, opened } = load();
  const output = [{ type: "text", text: JSON.stringify({ task_id: "T-2026-00042", status: "review", title: "Fix <b>it</b>" }) }];
  const node = api.managerChatEventNode({ type: "tool_result", payload: { call_id: "m", name: "aiworkhub_task_show", output, is_error: false } });
  assert.ok(text(node).includes("T-2026-00042"));
  assert.ok(text(node).includes("Fix <b>it</b>"));
  flat(node).find((n) => n.tag === "button").listeners.click[0]();
  assert.deepEqual(opened, ["T-2026-00042"]);
});

test("the hairline follows the last reported context fill", () => {
  const { api, elements, state } = load();
  state.managerChatEvents = [{ seq: 1, turn: 1, type: "turn_end", payload: { usage: { input: 1, cache_read: 0, cache_write: 0, output: 1, context_window: 100, context_fill: 0.8 } } }];
  api.managerConsoleApplyHairline();
  assert.equal(elements.managerChatHairline.children[0].style.width, "80%");
  assert.ok(elements.managerChatHairline.classList.values.has("is-blocked"));
  assert.equal(elements.managerChatContext.textContent, "80%");
  state.managerChatEvents = [];
  api.renderManagerChatEvents();
  assert.equal(elements.managerChatHairline.hidden, true);
  assert.equal(elements.managerChatHairline.children[0].style.width, "0%");
  assert.equal(elements.managerChatContext.textContent, "");
  assert.ok(!elements.managerChatHairline.classList.values.has("is-blocked"));
  assert.ok(!elements.managerChatHairline.classList.values.has("is-stale"));
  state.managerChatEvents = [{ type: "turn_end", payload: { usage: { context_fill: 0.6 } } }];
  api.managerConsoleApplyHairline();
  assert.equal(elements.managerChatHairline.children[0].style.width, "60%");
  assert.ok(elements.managerChatHairline.classList.values.has("is-stale"));
  state.managerChatEvents[0].payload.usage.context_fill = 0.75;
  api.managerConsoleApplyHairline();
  assert.ok(elements.managerChatHairline.classList.values.has("is-blocked"));
  assert.ok(!elements.managerChatHairline.classList.values.has("is-stale"));
  state.managerChatEvents[0].payload.usage.context_fill = null;
  api.managerConsoleApplyHairline();
  assert.equal(elements.managerChatHairline.hidden, true);
  for (const payload of [{ usage: { context_fill: null } }, { usage: {} }, {}]) {
    state.managerChatEvents = [
      { type: "turn_end", payload: { usage: { context_fill: 0.8 } } },
      { type: "assistant_text", payload: { text: "next turn" } },
      { type: "turn_end", payload },
    ];
    api.managerConsoleApplyHairline();
    assert.equal(elements.managerChatHairline.hidden, true, "latest completed turn has unknown context");
    assert.equal(elements.managerChatHairline.children[0].style.width, "0%");
    assert.equal(elements.managerChatContext.textContent, "");
  }
});

test("hostile payloads render as text in every block", () => {
  const { api } = load();
  const evil = "<img src=x onerror=alert(1)>";
  const nodes = [
    { type: "assistant_text", payload: { text: evil } },
    { type: "command", payload: { call_id: "c", command: evil, status: "failed", exit_code: 1, output_tail: evil } },
    { type: "file_change", payload: { call_id: "f", path: evil, kind: "update", diff: `+${evil}`, added: 1, removed: 0 } },
    { type: "error", payload: { source: evil, error: evil } },
    { type: "tool_result", payload: { call_id: "t", name: evil, output: JSON.stringify({ task_id: "T-1", title: evil }), is_error: false } },
  ].map((event) => api.managerChatEventNode(event));
  for (const node of nodes) {
    assert.ok(!flat(node).some((n) => n.tag === "img"), "no element from payload");
    assert.ok(text(node).includes(evil));
  }
});
