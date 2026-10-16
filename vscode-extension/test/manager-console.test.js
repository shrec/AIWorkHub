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
  const frames = [];
  const posted = [];
  elements.managerChatAnnouncer = fakeElement("div");
  elements.managerChatLatest = fakeElement("button");
  const context = {
    document: {
      createElement: (tag) => fakeElement(tag),
      createTextNode: (text) => ({ tag: "#text", textContent: String(text), children: [] }),
      createDocumentFragment: () => ({ __isFragment: true, children: [], appendChild(child) { this.children.push(child); return child; } }),
    },
    state: { managerChatEvents: events, managerChatSession: "mls-fixture", managerChatRunning: false },
    elements,
    requestTaskDetail: (id) => opened.push(id),
    vscode: { postMessage: (message) => posted.push(message) },
    window: {
      requestAnimationFrame: (fn) => { frames.push(fn); return frames.length; },
      setTimeout: () => 0,
      clearTimeout: () => {},
    },
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
  vm.runInContext(`${utilities}\n${consoleSource}\nthis.api = { managerChatEventNode, renderManagerChatEvents, managerConsoleMergeCommands, managerConsoleMergeTools, managerConsoleCompact, managerConsoleFooterText, managerConsoleTaskOf, managerConsoleApplyHairline, managerConsoleMarkdown, managerConsolePartialNodes, managerConsoleScheduleRender };`, context);
  return { api: context.api, document: context.document, elements, opened, frames, posted, state: context.state };
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

test("a tool_call paired with its tool_result by call_id merges into one done row carrying input and output", () => {
  const { api } = load();
  const merged = api.managerConsoleMergeTools([
    { seq: 1, turn: 1, type: "tool_call", payload: { call_id: "a1", name: "Read", input: { path: "x.py" } } },
    { seq: 2, turn: 1, type: "tool_result", payload: { call_id: "a1", name: "Read", output: "contents", is_error: false } },
  ]);
  assert.equal(merged.length, 1);
  assert.equal(merged[0].seq, 1);
  const node = api.managerChatEventNode(merged[0]);
  assert.equal(flat(node).filter((n) => n.tag === "details").length, 1);
  assert.ok(text(node).includes("Read"));
  assert.ok(text(node).includes("done"));
  assert.ok(text(node).includes("x.py"));
  assert.ok(text(node).includes("contents"));
  assert.equal(flat(node).find((n) => n.tag === "details").open, false);
});

test("a tool_call with no result yet renders one running, open row", () => {
  const { api } = load();
  const node = api.managerChatEventNode({ type: "tool_call", payload: { call_id: "b1", name: "Bash", input: { command: "ls" } } });
  assert.equal(flat(node).filter((n) => n.tag === "details").length, 1);
  const row = flat(node).find((n) => n.tag === "details");
  assert.equal(row.open, true);
  assert.ok(text(node).includes("running"));
  assert.ok(!text(node).includes("output"));
});

test("a failed tool_result merges into one failed, open row", () => {
  const { api } = load();
  const merged = api.managerConsoleMergeTools([
    { seq: 1, turn: 1, type: "tool_call", payload: { call_id: "c1", name: "Bash", input: { command: "false" } } },
    { seq: 2, turn: 1, type: "tool_result", payload: { call_id: "c1", name: "Bash", output: "boom", is_error: true } },
  ]);
  assert.equal(merged.length, 1);
  const node = api.managerChatEventNode(merged[0]);
  const row = flat(node).find((n) => n.tag === "details");
  assert.equal(row.open, true);
  assert.ok(text(node).includes("failed"));
  assert.ok(text(node).includes("boom"));
});

test("a tool_result with no matching call, or an empty call_id, stays a standalone row", () => {
  const { api } = load();
  const orphan = api.managerConsoleMergeTools([
    { seq: 1, turn: 1, type: "tool_result", payload: { call_id: "missing", name: "Read", output: "x", is_error: false } },
  ]);
  assert.equal(orphan.length, 1);
  assert.equal(orphan[0].type, "tool_result");
  const node = api.managerChatEventNode(orphan[0]);
  assert.ok(text(node).includes("Read result"));
  const empty = api.managerConsoleMergeTools([
    { seq: 1, turn: 1, type: "tool_call", payload: { call_id: "", name: "Read", input: {} } },
    { seq: 2, turn: 1, type: "tool_result", payload: { call_id: "", name: "Read", output: "x", is_error: false } },
  ]);
  assert.equal(empty.length, 2);
  assert.equal(empty[1].type, "tool_result");
});

test("merging tool calls and results never mutates the original events", () => {
  const { api } = load();
  const call = { seq: 1, turn: 1, type: "tool_call", payload: { call_id: "d1", name: "Read", input: { path: "x" } } };
  const result = { seq: 2, turn: 1, type: "tool_result", payload: { call_id: "d1", name: "Read", output: "y", is_error: false } };
  const events = [call, result];
  api.managerConsoleMergeTools(events);
  assert.equal(call.payload.output, undefined);
  assert.equal(events.length, 2);
  assert.equal(events[0], call);
  assert.equal(events[1], result);
});

test("a repeated call_id pairs results FIFO when providers reuse call ids across turns", () => {
  const { api } = load();
  const merged = api.managerConsoleMergeTools([
    { seq: 1, turn: 1, type: "tool_call", payload: { call_id: "x", name: "Read", input: { path: "a" } } },
    { seq: 2, turn: 1, type: "tool_result", payload: { call_id: "x", name: "Read", output: "a-out", is_error: false } },
    { seq: 3, turn: 2, type: "tool_call", payload: { call_id: "x", name: "Read", input: { path: "b" } } },
    { seq: 4, turn: 2, type: "tool_result", payload: { call_id: "x", name: "Read", output: "b-out", is_error: false } },
  ]);
  assert.equal(merged.length, 2);
  assert.equal(merged[0].payload.output, "a-out");
  assert.equal(merged[1].payload.output, "b-out");
  const rows = merged.map((event) => flat(api.managerChatEventNode(event)).find((n) => n.tag === "details"));
  assert.ok(rows.every((row) => row.open === false));
});

test("two calls sharing a call_id give the earlier one the result while the later stays running", () => {
  const { api } = load();
  const merged = api.managerConsoleMergeTools([
    { seq: 1, turn: 1, type: "tool_call", payload: { call_id: "x", name: "Read", input: { path: "a" } } },
    { seq: 2, turn: 1, type: "tool_call", payload: { call_id: "x", name: "Read", input: { path: "b" } } },
    { seq: 3, turn: 1, type: "tool_result", payload: { call_id: "x", name: "Read", output: "a-out", is_error: false } },
  ]);
  assert.equal(merged.length, 2);
  assert.equal(merged[0].payload.output, "a-out");
  assert.equal(merged[1].payload.output, undefined);
  assert.ok(text(api.managerChatEventNode(merged[0])).includes("done"));
  assert.ok(text(api.managerChatEventNode(merged[1])).includes("running"));
});

test("a merged tool call whose result output carries a task_id renders as a task block", () => {
  const { api, opened } = load();
  const output = [{ type: "text", text: JSON.stringify({ task_id: "T-2026-00099", status: "review", title: "Merged task" }) }];
  const merged = api.managerConsoleMergeTools([
    { seq: 1, turn: 1, type: "tool_call", payload: { call_id: "m1", name: "aiworkhub_task_show", input: {} } },
    { seq: 2, turn: 1, type: "tool_result", payload: { call_id: "m1", name: "aiworkhub_task_show", output, is_error: false } },
  ]);
  assert.equal(merged.length, 1);
  const node = api.managerChatEventNode(merged[0]);
  assert.ok(text(node).includes("T-2026-00099"));
  assert.ok(text(node).includes("Merged task"));
  flat(node).find((n) => n.tag === "button").listeners.click[0]();
  assert.deepEqual(opened, ["T-2026-00099"]);
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

test("markdown subset is built from nodes and never creates links or html", () => {
  const { api } = load();
  const fragment = api.managerConsoleMarkdown("Intro **bold** and `code`\n\n- one\n- two\n\n```\n<img src=x>\n```\n\nsee [docs](https://example.test)");
  const all = flat(fragment);
  assert.ok(all.some((n) => n.tag === "strong" && text(n) === "bold"));
  assert.ok(all.some((n) => n.tag === "code" && text(n) === "code"));
  assert.equal(all.filter((n) => n.tag === "li").length, 2);
  assert.ok(all.some((n) => n.tag === "pre" && text(n).includes("<img src=x>")));
  assert.ok(!all.some((n) => n.tag === "a" || n.tag === "img"));
  assert.ok(text(fragment).includes("docs (https://example.test)"));
});

test("partial renders only for a turn that has not finished", () => {
  const { api, state } = load([{ seq: 1, turn: 3, type: "user_message", payload: { text: "go" } }]);
  const live = api.managerConsolePartialNodes({ turn: 3, text: "Hel", reasoning: "hm", command_output: "" });
  assert.equal(live.length, 2);
  assert.ok(flat(live[1]).some((n) => n.className === "mc-caret"));
  state.managerChatEvents.push({ seq: 2, turn: 3, type: "turn_end", payload: {} });
  assert.deepEqual(Array.from(api.managerConsolePartialNodes({ turn: 3, text: "Hel", reasoning: "", command_output: "" })), []);
});

test("renders coalesce to one DOM write per animation frame", () => {
  const { api, frames, elements } = load([{ seq: 1, turn: 1, type: "assistant_text", payload: { text: "hi" } }]);
  let writes = 0;
  const original = elements.managerChatTranscript.replaceChildren;
  elements.managerChatTranscript.replaceChildren = function (...nodes) { writes += 1; return original.apply(this, nodes); };
  for (let i = 0; i < 5; i += 1) api.managerConsoleScheduleRender();
  assert.equal(frames.length, 1);
  frames[0]();
  assert.equal(writes, 1);
});

test("the transcript keeps the newest 400 blocks behind load earlier", () => {
  const events = Array.from({ length: 450 }, (_, i) => ({ seq: i + 1, turn: 1, type: "assistant_text", payload: { text: `m${i}` } }));
  const { api, elements, state } = load(events);
  api.renderManagerChatEvents();
  const rows = elements.managerChatTranscript.children;
  assert.equal(rows.length, 401);
  assert.equal(rows[0].tag, "button");
  rows[0].listeners.click[0]();
  assert.equal(state.managerChatRenderLimit, 800);
  api.renderManagerChatEvents();
  assert.equal(elements.managerChatTranscript.children.length, 450);
});

test("only a new final message is announced", () => {
  const { api, elements, state } = load([{ seq: 1, turn: 1, type: "assistant_text", payload: { text: "first" } }]);
  api.renderManagerChatEvents();
  assert.equal(elements.managerChatAnnouncer.textContent, "first");
  elements.managerChatAnnouncer.textContent = "";
  state.managerChatPartial = { turn: 2, text: "stream", reasoning: "", command_output: "" };
  api.renderManagerChatEvents();
  assert.equal(elements.managerChatAnnouncer.textContent, "");
});

test("scrolled-up rendering retains position and bottom rendering follows", () => {
  const { api, elements, state } = load([{ seq: 1, turn: 1, type: "assistant_text", payload: { text: "first" } }]);
  const box = elements.managerChatTranscript;
  box.scrollHeight = 1000;
  box.clientHeight = 100;
  box.scrollTop = 120;
  const original = box.replaceChildren;
  box.replaceChildren = function (...nodes) { original.apply(this, nodes); this.scrollHeight += 100; this.scrollTop = 0; };
  api.renderManagerChatEvents();
  assert.equal(box.scrollTop, 120);
  assert.equal(elements.managerChatLatest.hidden, false);
  box.scrollTop = box.scrollHeight - box.clientHeight;
  state.managerChatEvents.push({ seq: 2, turn: 2, type: "assistant_text", payload: { text: "next" } });
  api.renderManagerChatEvents();
  assert.equal(box.scrollTop, box.scrollHeight);
  assert.equal(elements.managerChatLatest.hidden, true);
});

test("partial outlives a mid-turn assistant message and rejects a finished turn and an older session or turn", () => {
  const { api, state } = load([{ seq: 1, turn: 3, type: "user_message", payload: { text: "go" } }]);
  assert.equal(api.managerConsolePartialNodes({ session_id: "mls-old", turn: 3, text: "old" }).length, 0);
  assert.equal(api.managerConsolePartialNodes({ turn: 2, text: "old" }).length, 0);
  // NF-2026-01230: the server settles the streamed text before it logs assistant_text, so
  // the partial that follows is the turn's next message, not a stale copy of this one.
  state.managerChatEvents.push({ seq: 2, turn: 3, type: "assistant_text", payload: { text: "final" } });
  assert.ok(api.managerConsolePartialNodes({ turn: 3, text: "next" }).length > 0);
  state.managerChatEvents.push({ seq: 3, turn: 3, type: "turn_end", payload: {} });
  assert.equal(api.managerConsolePartialNodes({ turn: 3, text: "stale" }).length, 0);
});

test("assistant_text and user_message bubbles each have a copy button that posts the raw text", () => {
  const { api, posted } = load();
  const assistantNode = api.managerChatEventNode({ type: "assistant_text", payload: { text: "**bold** plan" } });
  const assistantCopy = flat(assistantNode).find((n) => n.tag === "button" && n.className === "mc-copy");
  assert.ok(assistantCopy);
  assistantCopy.listeners.click[0]();
  const userNode = api.managerChatEventNode({ type: "user_message", payload: { text: "go now" } });
  const userCopy = flat(userNode).find((n) => n.tag === "button" && n.className === "mc-copy");
  assert.ok(userCopy);
  userCopy.listeners.click[0]();
  assert.deepEqual(JSON.parse(JSON.stringify(posted)), [
    { type: "copyText", text: "**bold** plan" },
    { type: "copyText", text: "go now" },
  ]);
});

test("a task node's copy button posts the id, title and status", () => {
  const { api, posted } = load();
  const output = [{ type: "text", text: JSON.stringify({ task_id: "T-2026-00099", status: "review", title: "Ship it" }) }];
  const node = api.managerChatEventNode({ type: "tool_result", payload: { call_id: "m2", name: "aiworkhub_task_show", output, is_error: false } });
  const copyButton = flat(node).find((n) => n.tag === "button" && n.className === "mc-copy");
  assert.ok(copyButton);
  copyButton.listeners.click[0]();
  assert.deepEqual(JSON.parse(JSON.stringify(posted)), [{ type: "copyText", text: "T-2026-00099 — Ship it (review)" }]);
});

test("extension.js allowlists copyText and its handler refuses a non-string", () => {
  const extensionSource = fs.readFileSync(path.join(__dirname, "..", "extension.js"), "utf8");
  const allowlist = slice(extensionSource, "const ALLOWED_INBOUND_MESSAGE_TYPES = new Set([", "\"requestStorageCleanup\",");
  assert.ok(allowlist.includes("\"copyText\","));
  const handler = slice(extensionSource, "case \"copyText\": {", "break;\n    }");
  assert.ok(handler.includes("typeof text !== \"string\""));
  assert.ok(handler.includes("copy_text_invalid"));
});

function countWrites(elements) {
  const box = elements.managerChatTranscript;
  const original = box.replaceChildren;
  const counter = { writes: 0 };
  box.replaceChildren = function (...nodes) { counter.writes += 1; return original.apply(this, nodes); };
  return counter;
}

test("a poll that changes nothing does not rebuild the transcript, and a new event does", () => {
  const events = [
    { seq: 1, turn: 1, type: "user_message", payload: { text: "go" } },
    { seq: 2, turn: 1, type: "assistant_text", payload: { text: "done" } },
  ];
  const { api, elements, state } = load(events);
  const counter = countWrites(elements);
  api.renderManagerChatEvents();
  assert.equal(counter.writes, 1);
  api.renderManagerChatEvents();
  api.renderManagerChatEvents();
  assert.equal(counter.writes, 1);
  state.managerChatEvents = events.concat([{ seq: 3, turn: 2, type: "assistant_text", payload: { text: "more" } }]);
  api.renderManagerChatEvents();
  assert.equal(counter.writes, 2);
});

test("an unchanged poll while running only ticks the live Thinking label", () => {
  const { api, elements, state } = load([{ seq: 1, turn: 1, type: "user_message", payload: { text: "go" } }]);
  state.managerChatRunning = true;
  const counter = countWrites(elements);
  api.renderManagerChatEvents();
  assert.equal(counter.writes, 1);
  const label = flat(elements.managerChatTranscript).find((n) => n.className === "manager-chat-thinking-label");
  assert.ok(label);
  state.managerChatThinkingSince -= 5000;
  api.renderManagerChatEvents();
  assert.equal(counter.writes, 1);
  assert.equal(label.textContent, "Thinking · 5s");
});

test("1500 events build and attach at most limit + 2 transcript rows", () => {
  const events = Array.from({ length: 1500 }, (_, i) => ({ seq: i + 1, turn: i + 1, type: "assistant_text", payload: { text: `m${i}` } }));
  // Count every element built, so building all 1500 rows and slicing afterwards fails.
  const counted = (harness) => {
    const create = harness.document.createElement;
    harness.created = 0;
    harness.document.createElement = (tag) => { harness.created += 1; return create(tag); };
    return harness;
  };
  const one = counted(load(events.slice(0, 1)));
  one.api.renderManagerChatEvents();
  const perRow = one.created;
  const all = counted(load(events));
  all.api.renderManagerChatEvents();
  assert.ok(all.elements.managerChatTranscript.children.length <= 400 + 2);
  assert.ok(all.created <= (400 + 2) * perRow, `${all.created} elements built for 400 rows`);
  all.created = 0;
  all.state.managerChatRenderLimit = 50;
  all.state.managerChatRunning = true;
  all.api.renderManagerChatEvents();
  assert.ok(all.elements.managerChatTranscript.children.length <= 50 + 2);
  assert.ok(all.created <= (50 + 2) * perRow, `${all.created} elements built for 50 rows`);
  assert.equal(all.elements.managerChatTranscript.children[0].tag, "button");
});

test("a changed Thought-for duration re-renders the transcript", () => {
  const { api, elements, state } = load([
    { seq: 1, turn: 1, type: "user_message", payload: { text: "go" } },
    { seq: 2, turn: 1, type: "turn_end", payload: {} },
  ]);
  state.managerChatLastThoughtMs = 5000;
  api.renderManagerChatEvents();
  assert.ok(text(elements.managerChatTranscript).includes("Thought for 5s"));
  state.managerChatLastThoughtMs = 12000;
  api.renderManagerChatEvents();
  assert.ok(text(elements.managerChatTranscript).includes("Thought for 12s"));
});

test("load earlier keeps the previously first visible row where it was", () => {
  const events = Array.from({ length: 450 }, (_, i) => ({ seq: i + 1, turn: i + 1, type: "assistant_text", payload: { text: `m${i}` } }));
  const { api, elements } = load(events);
  const box = elements.managerChatTranscript;
  box.scrollHeight = 1000;
  box.clientHeight = 100;
  box.scrollTop = 0;
  const original = box.replaceChildren;
  box.replaceChildren = function (...nodes) { original.apply(this, nodes); this.scrollHeight += 600; this.scrollTop = 0; };
  api.renderManagerChatEvents();
  assert.ok(text(box.children[1]).includes("m50"));
  box.scrollTop = 0;
  box.children[0].listeners.click[0]();
  api.renderManagerChatEvents();
  assert.equal(box.children.length, 450);
  assert.ok(text(box.children[50]).includes("m50"));
  assert.equal(box.scrollTop, 600);
});

test("turn_stopped renders a Stopped row and finishes its turn", () => {
  const { api, state } = load([{ seq: 1, turn: 3, type: "user_message", payload: { text: "go" } }]);
  assert.ok(text(api.managerChatEventNode({ type: "turn_stopped", turn: 3, payload: {} })).includes("Stopped"));
  assert.ok(api.managerConsolePartialNodes({ turn: 3, text: "Hel" }).length > 0);
  state.managerChatEvents.push({ seq: 2, turn: 3, type: "turn_stopped", payload: {} });
  assert.equal(api.managerConsolePartialNodes({ turn: 3, text: "Hel" }).length, 0);
  state.managerChatRunning = true;
  api.renderManagerChatEvents();
  assert.equal(state.managerChatThinkingKey, "mls-fixture:4");
});

test("queue_dropped and a continuation session_start render their rows", () => {
  const { api } = load();
  assert.ok(text(api.managerChatEventNode({ type: "queue_dropped", payload: { dropped_queued: 2 } })).includes("2 queued messages dropped"));
  assert.ok(text(api.managerChatEventNode({ type: "queue_dropped", payload: { dropped_queued: 1 } })).includes("1 queued message dropped"));
  assert.ok(text(api.managerChatEventNode({ type: "session_start", payload: {} })).includes("Session started"));
  const plain = api.managerChatEventNode({ type: "session_start", payload: { predecessor: "mls-old" } });
  assert.ok(text(plain).includes("Continued from previous session"));
  const node = api.managerChatEventNode({ type: "session_start", payload: { predecessor: "mls-old", handoff_summary: "Carry on with T-1" } });
  assert.ok(flat(node).some((n) => n.tag === "details"));
  assert.ok(text(node).includes("Continued from previous session"));
  assert.ok(text(node).includes("Carry on with T-1"));
});

test("snake_case_name and a * b * c render verbatim while real emphasis still works", () => {
  const { api } = load();
  for (const source of ["use snake_case_name here", "a * b * c", "2 * 3 * 4 = 24", "__init__ and my_var_name", "2**10 and 2**20", "x**2 + y**2"]) {
    const fragment = api.managerConsoleMarkdown(source);
    assert.ok(!flat(fragment).some((n) => n.tag === "em" || n.tag === "strong"), source);
    assert.equal(text(fragment), source);
  }
  const emphasis = flat(api.managerConsoleMarkdown("*one* and _two_ and **three**"));
  assert.deepEqual(emphasis.filter((n) => n.tag === "em").map(text), ["one", "two"]);
  assert.deepEqual(emphasis.filter((n) => n.tag === "strong").map(text), ["three"]);
});

test("a code block has a Copy button that posts the block text and folds past 40 lines", () => {
  const { api, posted } = load();
  const fragment = api.managerConsoleMarkdown("```\nline 1\nline 2\n```");
  const copy = flat(fragment).find((n) => n.tag === "button" && n.className === "mc-copy");
  assert.ok(copy);
  copy.listeners.click[0]();
  assert.deepEqual(JSON.parse(JSON.stringify(posted)), [{ type: "copyText", text: "line 1\nline 2" }]);
  const long = Array.from({ length: 50 }, (_, i) => `row ${i + 1}`);
  const folded = flat(api.managerConsoleMarkdown("```\n" + long.join("\n") + "\n```"));
  const pre = folded.find((n) => n.tag === "pre");
  assert.equal(pre.textContent.split("\n").length, 40);
  const showAll = folded.find((n) => n.tag === "button" && n.className === "mc-show-all");
  assert.equal(showAll.textContent, "show all (50 lines)");
  folded.find((n) => n.tag === "button" && n.className === "mc-copy").listeners.click[0]();
  assert.equal(JSON.parse(JSON.stringify(posted)).at(-1).text, long.join("\n"));
});

test("a timestamped event shows HH:MM and an untimestamped one shows none", () => {
  const { api } = load();
  const at = "2026-09-27T10:05:00Z";
  const date = new Date(at);
  const expected = String(date.getHours()).padStart(2, "0") + ":" + String(date.getMinutes()).padStart(2, "0");
  const stamped = flat(api.managerChatEventNode({ type: "assistant_text", at, payload: { text: "hi" } })).filter((n) => n.className === "mc-time");
  assert.deepEqual(stamped.map((n) => n.textContent), [expected]);
  const epoch = flat(api.managerChatEventNode({ type: "user_message", ts: date.getTime() / 1000, payload: { text: "hi" } })).filter((n) => n.className === "mc-time");
  assert.deepEqual(epoch.map((n) => n.textContent), [expected]);
  const bare = flat(api.managerChatEventNode({ type: "assistant_text", payload: { text: "hi" } }));
  assert.ok(!bare.some((n) => n.className === "mc-time"));
});
