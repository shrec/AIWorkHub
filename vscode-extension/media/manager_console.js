"use strict";

// Manager console block renderers (spec 2026-09-26 §3). Loaded before app.js;
// every app.js global these use (createElement, state, elements) is read at
// call time, never at load time. Every event payload is untrusted model/tool
// output: strings go through createElement's textContent or
// document.createTextNode, never innerHTML.

function managerChatImageCount(count) {
  return `${count} image${count === 1 ? "" : "s"} attached`;
}

function managerConsoleErrorText(payload) {
  return String(payload.error || payload.message || "Manager error");
}

function managerConsolePayload(event) {
  return event && event.payload && typeof event.payload === "object" ? event.payload : {};
}

function managerConsoleHighestTerminalSeq(events) {
  let highest = 0;
  for (const event of events) {
    if (event && MANAGER_CONSOLE_TERMINAL_TYPES.has(event.type)) highest = Math.max(highest, numberValue(event.seq));
  }
  return highest;
}

// A call with no result is live only while no terminal event follows it in the log. `chat` is the
// worker chat's own state (renderWorkerChatEvents); without it the manager console's state applies.
function managerConsoleUnfinishedStatus(event, chat = null) {
  const terminalSeq = chat ? chat.terminalSeq : managerConsoleTerminalSeq;
  return numberValue(event && event.seq) < terminalSeq ? "interrupted" : "running";
}

function managerConsoleToolStatus(payload, event, chat = null) {
  if (!("output" in payload || "is_error" in payload)) return managerConsoleUnfinishedStatus(event, chat);
  return payload.is_error ? "failed" : "done";
}

function managerChatEventNode(event, chat = null) {
  const type = String((event && event.type) || "");
  if (!MANAGER_CONSOLE_ROW_TYPES.has(type)) return null;
  const payload = managerConsolePayload(event);
  if (type === "command") return managerConsoleCommandNode(payload, event, chat);
  if (type === "file_change") return managerConsoleFileChangeNode(payload);
  if (type === "timeline_row") return managerConsoleTimelineRowNode(payload);
  if (type === "assistant_text" || type === "user_message") {
    const bubble = createElement("div", `manager-chat-bubble role-${type === "user_message" ? "user" : "assistant"}`);
    // The worker chat names its runner/model visibly; the manager console's label is for screen readers.
    const speaker = type === "user_message" ? "You" : chat ? chat.assistantLabel : "Manager";
    bubble.appendChild(createElement("span", chat ? "manager-chat-bubble-label worker-chat-label" : "manager-chat-bubble-label sr-only", speaker));
    const clock = managerChatClockText(event);
    if (clock) bubble.appendChild(createElement("span", "mc-time", clock));
    bubble.appendChild(type === "assistant_text" ? managerConsoleMarkdown(payload.text) : document.createTextNode(String(payload.text || "")));
    // The log records only how many images the owner attached, never the image.
    const images = type === "user_message" ? numberValue(payload.images) : 0;
    if (images > 0) bubble.appendChild(createElement("span", "manager-chat-bubble-images", managerChatImageCount(images)));
    const block = managerConsoleBlock(type, bubble);
    block.appendChild(managerConsoleCopyButton(() => String(payload.text || "")));
    return block;
  }
  if (type === "tool_call") {
    const name = String(payload.name || payload.tool || "tool");
    const hint = managerChatToolHint(payload);
    const status = managerConsoleToolStatus(payload, event, chat);
    const hasResult = status === "done" || status === "failed";
    const row = createElement("details", "manager-chat-tool-row is-" + status);
    row.open = false;
    row.appendChild(managerConsoleToolSummary(name, (shown) => [shown, status, hint].filter(Boolean).join(" · ")));
    const body = createElement("div", "manager-chat-tool-row-body");
    const task = hasResult ? managerConsoleTaskOf(payload) : null;
    if (task) body.appendChild(managerConsoleTaskHead(task));
    if ("input" in payload) {
      body.appendChild(createElement("div", "manager-chat-tool-section", "input"));
      appendManagerChatToolFields(body, payload.input, 0);
    }
    if (hasResult) {
      body.appendChild(createElement("div", "manager-chat-tool-section", "output"));
      appendManagerChatToolFields(body, payload.output, 0);
    }
    row.appendChild(body);
    return task ? managerConsoleTaskNode(task, row) : managerConsoleBlock("tool_call", row);
  }
  if (type === "tool_result") {
    const name = String(payload.name || payload.tool || "tool");
    const hint = managerChatToolHint(payload);
    const row = createElement("details", "manager-chat-tool-row is-result");
    row.open = false;
    row.appendChild(managerConsoleToolSummary(name, (shown) => [shown + " result", hint].filter(Boolean).join(" · ")));
    const body = createElement("div", "manager-chat-tool-row-body");
    const task = managerConsoleTaskOf(payload);
    if (task) body.appendChild(managerConsoleTaskHead(task));
    const rest = Object.assign({}, payload);
    delete rest.name;
    delete rest.tool;
    appendManagerChatToolFields(body, rest, 0);
    row.appendChild(body);
    return task ? managerConsoleTaskNode(task, row) : managerConsoleBlock(type, row);
  }
  if (type === "callback") {
    return managerConsoleBlock(type, createElement("div", "manager-chat-marker", `Automatic wake-up${payload.text ? `: ${limitText(payload.text, 120)}` : ""}`));
  }
  if (type === "error") {
    const text = managerConsoleErrorText(payload);
    return managerConsoleBlock(type, createElement("div", "manager-chat-error", payload.repeat_count > 1 ? `${text} x ${payload.repeat_count}` : text));
  }
  if (type === "session_start") {
    const continuation = managerConsoleContinuation(payload);
    if (!continuation) return managerConsoleBlock(type, createElement("div", "manager-chat-marker", "Session started"));
    if (!continuation.summary) return managerConsoleBlock(type, createElement("div", "manager-chat-marker", "Continued from previous session"));
    const row = createElement("details", "manager-chat-tool-row is-continuation");
    row.appendChild(createElement("summary", "", "Continued from previous session"));
    const body = createElement("div", "manager-chat-tool-row-body");
    body.appendChild(document.createTextNode(continuation.summary));
    row.appendChild(body);
    return managerConsoleBlock(type, row);
  }
  if (type === "handoff_request") {
    return managerConsoleBlock(type, createElement("div", "manager-chat-marker", `Handoff requested${payload.reason ? `: ${limitText(payload.reason, 120)}` : ""}`));
  }
  if (type === "session_close") {
    return managerConsoleBlock(type, createElement("div", "manager-chat-marker", `Session closed${payload.reason ? `: ${limitText(payload.reason, 120)}` : ""}`));
  }
  if (type === "turn_stopped") {
    return managerConsoleBlock(type, createElement("div", "manager-chat-marker mc-stopped", "Stopped"));
  }
  if (type === "queue_dropped") {
    const count = Math.max(0, numberValue([payload.dropped_queued, payload.count, payload.dropped, payload.n].find((value) => value !== undefined && value !== null)));
    return managerConsoleBlock(type, createElement("div", "manager-chat-marker", `${count} queued message${count === 1 ? "" : "s"} dropped`));
  }
  if (type === "reasoning") {
    const row = createElement("details", "manager-chat-thinking-block");
    // A worker's reasoning arrives whole in its log, so it carries no live thinking timer.
    const label = chat ? "Reasoning" : managerChatThoughtSummary(event);
    const summary = createElement("summary", "", label);
    if (!chat && managerConsoleActiveIndex && label.startsWith(MANAGER_CONSOLE_LIVE_THOUGHT_PREFIX)) managerConsoleLiveSummaries.push({ summary, event });
    row.appendChild(summary);
    const body = createElement("div", "manager-chat-tool-row-body");
    body.appendChild(document.createTextNode(String(payload.text || "")));
    row.appendChild(body);
    return managerConsoleBlock(type, row);
  }
  if (type === "turn_end") {
    return managerConsoleBlock("turn_end", createElement("div", "mc-footer", managerConsoleFooterText(event, chat)));
  }
  return null;
}

function managerConsoleTimelineSection(parent, title, content) {
  if (content === undefined || content === null || content === "") return;
  const section = createElement("section", "live-output-section");
  section.appendChild(createElement("h4", "", title));
  section.appendChild(createElement("div", "live-output-section-body", String(content)));
  parent.appendChild(section);
}

// The compact row the old Live Output timeline built for one line (timelineEventFromObject or the
// raw-text branch), kept for a provider line no chat translator recognises. Every field is text.
function managerConsoleTimelineRowNode(row) {
  const kind = String(row.kind || "event").replace(/[^\w-]/g, "");
  const card = createElement("div", `mc-body live-output-row is-${kind}`);
  const head = createElement("div", "live-output-row-head");
  head.appendChild(createElement("strong", "live-output-row-title", String(row.title || "")));
  head.appendChild(createElement("span", "live-output-row-state", String(row.state || "")));
  card.appendChild(head);
  if (row.label) card.appendChild(createElement("div", "live-output-row-label", String(row.label)));
  card.appendChild(createElement("div", "live-output-row-message", String(row.message || "No message")));
  const metrics = Array.isArray(row.metrics) ? row.metrics : [];
  if (metrics.length) card.appendChild(createElement("div", "live-output-row-meta", metrics.join(" | ")));
  managerConsoleTimelineSection(card, "Tool input (recognized credentials redacted)", row.activityInput);
  managerConsoleTimelineSection(card, "Tool result (recognized credentials redacted)", row.activityOutput);
  if (row.raw) {
    const raw = createElement("details", "live-output-row-raw");
    raw.open = false;
    raw.appendChild(createElement("summary", "", "Raw event"));
    raw.appendChild(createElement("pre", "", String(row.raw)));
    card.appendChild(raw);
  }
  return managerConsoleBlock("timeline_row", card);
}

// A session_start that names a predecessor or carries a handoff is a continuation, not a fresh start.
function managerConsoleContinuation(payload) {
  const predecessor = payload.predecessor || payload.predecessor_id || payload.predecessor_session_id || payload.previous_session_id
    || payload.continued_from || payload.handoff_from;
  const handoff = payload.handoff;
  const summary = [payload.handoff_summary, payload.summary, typeof handoff === "string" ? handoff : handoff && handoff.summary]
    .find((value) => typeof value === "string" && value.trim());
  if (!predecessor && !handoff && !summary) return null;
  return { summary: summary ? summary.trim() : "" };
}

function managerChatFormatDuration(ms) {
  const seconds = Math.max(0, Math.round(Number(ms) / 1000));
  if (seconds < 60) return seconds + "s";
  return Math.floor(seconds / 60) + "m " + (seconds % 60) + "s";
}

// An event may carry its time as ISO text (`at`, `created_at`) or epoch seconds/ms (`ts`); 0 when absent or unparseable.
function managerChatEventTime(event) {
  if (!event) return 0;
  for (const key of ["at", "ts", "created_at"]) {
    const value = event[key];
    let parsed = NaN;
    if (typeof value === "string" && value) parsed = Date.parse(value);
    else if (typeof value === "number" && value > 0) parsed = value < 1e12 ? value * 1000 : value;
    if (Number.isFinite(parsed) && parsed > 0) return parsed;
  }
  return 0;
}

function managerChatClockText(event) {
  const time = managerChatEventTime(event);
  if (!time) return "";
  const date = new Date(time);
  return String(date.getHours()).padStart(2, "0") + ":" + String(date.getMinutes()).padStart(2, "0");
}

function managerChatThoughtSummary(event) {
  const start = managerChatEventTime(event);
  const index = managerConsoleCurrentIndex();
  const end = index.reasoningEnd.get(event) || 0;
  if (start && end >= start) {
    // Claude CLI delivers a reasoning block whole: the time spent is the gap before it, not after.
    const after = end - start;
    if (after >= 1000) return "Thought for " + managerChatFormatDuration(after);
    const before = index.reasoningStart.get(event) || 0;
    if (before > 0 && start >= before && start - before >= 1000) return "Thought for " + managerChatFormatDuration(start - before);
    return "Thought";
  }
  if (state.managerChatRunning && start) return "Thinking · " + managerChatFormatDuration(Date.now() - start);
  if (state.managerChatLastThoughtMs) return "Thought for " + managerChatFormatDuration(state.managerChatLastThoughtMs);
  return state.managerChatRunning ? "Thinking" : "Thought";
}

function managerChatToolHint(payload) {
  const input = payload && payload.input && typeof payload.input === "object" ? payload.input : {};
  const candidates = [input.path, input.file_path, input.file, input.command, input.cmd, input.query, input.url, input.pattern, payload.path, payload.command];
  for (const item of candidates) {
    if (typeof item === "string" && item.trim()) return item.trim();
  }
  return "";
}

// The status follows the name directly, so the one-line ellipsis can only ever eat the hint.
function managerConsoleToolSummary(name, render) {
  const summary = createElement("summary", "", render(name.replace(/^mcp__.+?__(?=.)/, "")));
  summary.setAttribute("title", render(name));
  return summary;
}

function managerConsoleMcpText(value) {
  if (!Array.isArray(value) || !value.length) return null;
  if (!value.every((item) => item && item.type === "text" && typeof item.text === "string")) return null;
  return value.map((item) => item.text).join("\n");
}

function managerConsoleToolText(value) {
  const text = value == null ? "" : String(value);
  if ((text[0] !== "{" && text[0] !== "[") || text.length >= MANAGER_CONSOLE_PRETTY_JSON_LIMIT) return text;
  let parsed;
  try { parsed = JSON.parse(text); } catch (_error) { return text; }
  const mcp = managerConsoleMcpText(parsed);
  if (mcp !== null) return managerConsoleToolText(mcp);
  // JSON.parse rounds integers past 2^53, and this panel must never show digits the tool did not print.
  return /\d{16,}/.test(text) ? text : JSON.stringify(parsed, null, 2);
}

function managerConsoleToolValue(parent, text) {
  const node = createElement("div", "manager-chat-tool-value");
  node.appendChild(document.createTextNode(text));
  parent.appendChild(node);
}

function appendManagerChatToolFields(parent, value, depth) {
  if (!parent) return;
  const mcp = managerConsoleMcpText(value);
  if (mcp !== null) return managerConsoleToolValue(parent, managerConsoleToolText(mcp));
  if (value == null || typeof value !== "object") return managerConsoleToolValue(parent, managerConsoleToolText(value));
  if (depth > 4) return managerConsoleToolValue(parent, JSON.stringify(value, null, 2));
  const entries = Array.isArray(value) ? value.map((item, index) => [String(index), item]) : Object.keys(value).map((key) => [key, value[key]]);
  for (const pair of entries) {
    const field = createElement("div", "manager-chat-tool-field");
    field.appendChild(createElement("span", "manager-chat-tool-label", pair[0]));
    appendManagerChatToolFields(field, pair[1], depth + 1);
    parent.appendChild(field);
  }
}

function managerChatThinkingKey() {
  // session:turn the running panel waits on -- the latest turn, or the next
  // one once the latest has ended (an optimistic send before its user_message).
  let turn = 0;
  let finished = false;
  for (const item of state.managerChatEvents || []) {
    if (!item || !Number.isInteger(item.turn) || item.turn < turn) continue;
    if (item.turn > turn) {
      turn = item.turn;
      finished = false;
    }
    if (MANAGER_CONSOLE_TERMINAL_TYPES.has(item.type)) finished = true;
  }
  return String(state.managerChatSession || "") + ":" + (finished ? turn + 1 : turn);
}

// Glyph per block kind; the gutter is fixed-width so blocks never shift.
const MANAGER_CONSOLE_GLYPHS = Object.freeze({
  user_message: "›", assistant_text: "●", reasoning: "∴", command: "$", file_change: "±",
  tool_call: "⚙", tool_result: "⚙", task: "▣", callback: "↯", goal: "◎", error: "!", turn_stopped: "■",
  timeline_row: "·",
});
// Every event type that renders a row; managerChatEventNode returns null for anything else.
// timeline_row is the worker chat's compact row for a provider line no translator recognises.
const MANAGER_CONSOLE_ROW_TYPES = new Set([
  "command", "file_change", "assistant_text", "user_message", "tool_call", "tool_result", "callback", "error",
  "session_start", "handoff_request", "session_close", "turn_stopped", "queue_dropped", "reasoning", "turn_end",
  "timeline_row",
]);
const MANAGER_CONSOLE_ACTIVITY_TYPES = new Set(["tool_call", "tool_result", "command", "file_change", "reasoning"]);
// Every event type after which nothing earlier in the log can still be running.
const MANAGER_CONSOLE_TERMINAL_TYPES = new Set(["turn_end", "turn_stopped", "error", "session_close"]);
const MANAGER_CONSOLE_LIVE_THOUGHT_PREFIX = "Thinking ·";
const MANAGER_CONSOLE_COMMAND_LINES = 20;
const MANAGER_CONSOLE_DIFF_LINES = 40;
const MANAGER_CONSOLE_PRETTY_JSON_LIMIT = 64 * 1024;

function managerConsoleBlock(kind, body) {
  const block = createElement("div", "mc-block mc-" + kind.replace(/_/g, "-"));
  const gutter = createElement("span", "mc-gutter", MANAGER_CONSOLE_GLYPHS[kind] || "");
  gutter.setAttribute("aria-hidden", "true");
  block.appendChild(gutter);
  block.appendChild(body);
  return block;
}

function managerConsoleShowAll(label, onClick) {
  const button = createElement("button", "mc-show-all", label);
  button.type = "button";
  button.addEventListener("click", () => { onClick(); button.remove(); });
  return button;
}

function managerConsoleCopyButton(getText) {
  const button = createElement("button", "mc-copy", "Copy");
  button.type = "button";
  button.setAttribute("aria-label", "Copy");
  button.setAttribute("title", "Copy");
  let resetHandle = null;
  button.addEventListener("click", () => {
    const text = String(getText() || "");
    vscode.postMessage({ type: "copyText", text });
    button.textContent = "Copied";
    if (resetHandle) window.clearTimeout(resetHandle);
    resetHandle = window.setTimeout(() => { button.textContent = "Copy"; }, 1500);
  });
  return button;
}

function managerConsoleMergeCommands(events) {
  const latest = new Map();
  for (const event of events) {
    const id = event && event.type === "command" && event.payload && event.payload.call_id;
    if (id) latest.set(id, event);
  }
  const shown = new Set();
  const merged = [];
  for (const event of events) {
    const id = event && event.type === "command" && event.payload && event.payload.call_id;
    if (!id) { merged.push(event); continue; }
    if (shown.has(id)) continue;
    shown.add(id);
    merged.push(latest.get(id));
  }
  return merged;
}

// FIFO per call_id; a result with no earlier unpaired call, or an empty call_id, stays standalone.
function managerConsoleMergeTools(events) {
  const pending = new Map();
  const merged = [];
  for (const event of events) {
    const payload = event && event.payload;
    const id = payload && payload.call_id;
    if (id && event.type === "tool_call") {
      const queue = pending.get(id) || [];
      queue.push(merged.length);
      pending.set(id, queue);
    } else if (id && event.type === "tool_result" && pending.has(id)) {
      const queue = pending.get(id);
      const callIndex = queue.shift();
      if (!queue.length) pending.delete(id);
      const call = merged[callIndex];
      merged[callIndex] = { ...call, payload: { ...call.payload, output: payload.output, is_error: payload.is_error } };
      continue;
    }
    merged.push(event);
  }
  return merged;
}

function managerConsoleCommandStatus(payload, event, chat = null) {
  if (payload.status === "running") return managerConsoleUnfinishedStatus(event, chat);
  const code = Number.isInteger(payload.exit_code) ? payload.exit_code : null;
  return payload.status === "failed" || (code !== null && code !== 0) ? "failed" : "done";
}

function managerConsoleCommandChip(payload, event, chat = null) {
  const status = managerConsoleCommandStatus(payload, event, chat);
  if (status === "running" || status === "interrupted") return createElement("span", "mc-chip is-" + status, status);
  const code = Number.isInteger(payload.exit_code) ? payload.exit_code : null;
  const failed = status === "failed";
  const label = code !== null ? "exit " + code : failed ? "failed" : "done";
  return createElement("span", failed ? "mc-chip is-failed" : "mc-chip is-ok", label);
}

function managerConsoleCommandNode(payload, event, chat = null) {
  const body = createElement("div", "mc-body");
  const head = createElement("div", "mc-head");
  head.appendChild(createElement("code", "mc-mono", String(payload.command || "")));
  head.appendChild(managerConsoleCommandChip(payload, event, chat));
  body.appendChild(head);
  const lines = String(payload.output_tail || "").split("\n");
  if (payload.output_tail) {
    const pre = createElement("pre", "mc-output", lines.slice(-MANAGER_CONSOLE_COMMAND_LINES).join("\n"));
    if (lines.length > MANAGER_CONSOLE_COMMAND_LINES) {
      body.appendChild(managerConsoleShowAll(`show all (${lines.length} lines)`, () => { pre.textContent = lines.join("\n"); }));
    }
    body.appendChild(pre);
  }
  if (payload.truncated) body.appendChild(createElement("div", "mc-note", `output cut, ${numberValue(payload.original_bytes)} bytes in total`));
  return managerConsoleBlock("command", body);
}

function managerConsoleDiffNode(diff) {
  const lines = String(diff || "").split("\n");
  const pre = createElement("pre", "mc-diff");
  const paint = (count) => {
    const fragment = document.createDocumentFragment();
    for (const line of lines.slice(0, count)) {
      const kind = line.startsWith("@@") ? "mc-hunk"
        : line.startsWith("+") && !line.startsWith("+++") ? "mc-add"
        : line.startsWith("-") && !line.startsWith("---") ? "mc-del" : "mc-ctx";
      fragment.appendChild(createElement("span", kind, line + "\n"));
    }
    pre.replaceChildren(fragment);
  };
  paint(MANAGER_CONSOLE_DIFF_LINES);
  if (lines.length <= MANAGER_CONSOLE_DIFF_LINES) return pre;
  const wrap = createElement("div", "mc-collapsible");
  wrap.appendChild(managerConsoleShowAll(`show all (${lines.length} lines)`, () => paint(lines.length)));
  wrap.appendChild(pre);
  return wrap;
}

function managerConsoleFileChangeNode(payload) {
  const body = createElement("div", "mc-body");
  const head = createElement("div", "mc-head");
  head.appendChild(createElement("code", "mc-mono", String(payload.path || "")));
  head.appendChild(createElement("span", "mc-counts", ` (+${numberValue(payload.added)} −${numberValue(payload.removed)})`));
  body.appendChild(head);
  if (payload.diff) body.appendChild(managerConsoleDiffNode(payload.diff));
  if (payload.truncated) body.appendChild(createElement("div", "mc-note", `diff cut, ${numberValue(payload.original_bytes)} bytes in total`));
  return managerConsoleBlock("file_change", body);
}

function managerConsoleTaskFields(value) {
  let data = value;
  if (Array.isArray(data)) data = data.map((block) => (block && typeof block.text === "string" ? block.text : "")).join("");
  if (typeof data === "string") {
    if (data.length > 16384) return null;
    try { data = JSON.parse(data); } catch (_error) { return null; }
  }
  if (!data || typeof data !== "object") return null;
  const task = data.task && typeof data.task === "object" ? data.task : data;
  const id = task.task_id;
  if (typeof id !== "string" || !TASK_ID_RE.test(id)) return null;
  return { id, status: typeof task.status === "string" ? task.status : "", title: typeof task.title === "string" ? task.title : "" };
}

function managerConsoleTaskOf(payload) {
  if (!payload) return null;
  return managerConsoleTaskFields(payload.output) || managerConsoleTaskFields(payload.input);
}

// Goes first in the tool row's body, so a task adds no line above the collapsed row.
function managerConsoleTaskHead(task) {
  const head = createElement("div", "mc-head");
  const open = createElement("button", "mc-task-link mc-mono", task.id);
  open.type = "button";
  open.addEventListener("click", () => requestTaskDetail(task.id));
  head.appendChild(open);
  if (task.status) head.appendChild(createElement("span", "status-badge " + task.status, task.status));
  if (task.title) head.appendChild(createElement("span", "mc-task-title", task.title));
  return head;
}

function managerConsoleTaskNode(task, row) {
  const block = managerConsoleBlock("task", row);
  block.appendChild(managerConsoleCopyButton(() => {
    let text = String(task.id || "");
    if (task.title) text += " — " + task.title;
    if (task.status) text += " (" + task.status + ")";
    return text;
  }));
  return block;
}

function managerConsoleCompact(value) {
  const n = numberValue(value);
  if (n < 1000) return String(n);
  if (n < 1000000) return (n / 1000).toFixed(1).replace(/\.0$/, "") + "k";
  return (n / 1000000).toFixed(1).replace(/\.0$/, "") + "M";
}

function managerConsoleCount(value) {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
}

// Normalized v3 usage, or the legacy provider keys; a hostile or empty usage gives no counts at all.
function managerConsoleUsage(usage) {
  if (!usage || typeof usage !== "object" || Array.isArray(usage)) return null;
  const legacy = !("input" in usage || "output" in usage);
  const pick = (key, legacyKey) => managerConsoleCount(legacy ? usage[legacyKey] : usage[key]);
  const input = pick("input", "input_tokens");
  const output = pick("output", "output_tokens");
  if (input === null && output === null) return null;
  const cache = (pick("cache_read", "cache_read_input_tokens") || 0) + (pick("cache_write", "cache_creation_input_tokens") || 0);
  return { input: input || 0, cache, output: output || 0 };
}

// One pass over the raw events: per-turn first event and tool ids, the end time of each reasoning
// event (the next non-reasoning event) and its start (the time of the immediately preceding event of
// the same turn: Claude CLI delivers a reasoning block whole, so the time spent is the gap before it).
// A render builds it once so no row rescans the whole log.
function managerConsoleBuildIndex(events) {
  const turns = new Map();
  const reasoningEnd = new Map();
  const reasoningStart = new Map();
  let nextEnd = 0;
  for (let cursor = events.length - 1; cursor >= 0; cursor -= 1) {
    const item = events[cursor];
    if (!item) continue;
    if (item.type === "reasoning") {
      reasoningEnd.set(item, nextEnd);
      // A predecessor of another turn is owner idle time, not thinking time.
      const previous = cursor > 0 ? events[cursor - 1] : null;
      if (previous && previous.turn === item.turn) reasoningStart.set(item, managerChatEventTime(previous));
    } else nextEnd = managerChatEventTime(item);
  }
  for (const item of events) {
    if (!item || !Number.isInteger(item.turn)) continue;
    let info = turns.get(item.turn);
    if (!info) {
      info = { first: item, tools: new Set() };
      turns.set(item.turn, info);
    }
    if (item.type === "tool_call" || item.type === "command" || item.type === "file_change") {
      info.tools.add((item.payload && item.payload.call_id) || "seq:" + item.seq);
    }
  }
  return { turns, reasoningEnd, reasoningStart };
}

function managerConsoleCurrentIndex() {
  return managerConsoleActiveIndex || managerConsoleBuildIndex(Array.isArray(state.managerChatEvents) ? state.managerChatEvents : []);
}

function managerConsoleFooterText(event, chat = null) {
  const turn = event && event.turn;
  const index = chat ? chat.index : managerConsoleCurrentIndex();
  const info = Number.isInteger(turn) ? index.turns.get(turn) : null;
  const tools = info ? info.tools.size : 0;
  const parts = [Number.isInteger(turn) ? `turn ${turn}` : "turn", `${tools} tool${tools === 1 ? "" : "s"}`];
  const usage = managerConsoleUsage(event && event.payload && event.payload.usage);
  if (usage) parts.push(`in ${managerConsoleCompact(usage.input)}`, `cache ${managerConsoleCompact(usage.cache)}`, `out ${managerConsoleCompact(usage.output)}`);
  // managerChatEventTime is 0 without a timestamp; no duration is better than a made-up one.
  const start = info ? managerChatEventTime(info.first) : 0;
  const end = managerChatEventTime(event);
  if (start > 0 && end > 0 && end >= start) parts.push(managerChatFormatDuration(end - start));
  return parts.join(" · ");
}

function managerConsoleContextFill(events) {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index];
    if (!event || event.type !== "turn_end") continue;
    const usage = event.payload && event.payload.usage;
    return usage && typeof usage.context_fill === "number" && Number.isFinite(usage.context_fill) ? usage.context_fill : null;
  }
  return null;
}

function managerConsoleApplyHairline() {
  const line = elements.managerChatHairline;
  if (!line) return;
  const fill = managerConsoleContextFill(state.managerChatEvents || []);
  line.hidden = fill === null;
  if (elements.managerChatContext) elements.managerChatContext.textContent = fill === null ? "" : Math.round(fill * 100) + "%";
  if (!(line.firstElementChild || line.children[0])) return;
  if (fill === null) {
    const bar = line.firstElementChild || line.children[0];
    if (bar) bar.style.width = "0%";
    line.setAttribute("aria-valuenow", "0");
    line.classList.toggle("is-stale", false);
    line.classList.toggle("is-blocked", false);
    return;
  }
  const percent = Math.min(100, Math.round(fill * 100));
  const bar = line.firstElementChild || line.children[0]; // the DOM has firstElementChild, the test fake has children
  bar.style.width = percent + "%";
  line.setAttribute("aria-valuenow", String(percent));
  line.classList.toggle("is-stale", fill >= 0.6 && fill < 0.75);
  line.classList.toggle("is-blocked", fill >= 0.75);
}

let managerConsoleLastSignature = null;
let managerConsoleRenderedLimit = null;
let managerConsoleActiveIndex = null;
let managerConsoleLiveSummaries = [];
// When the streamed text last grew: partial text carries no time of its own.
let managerConsoleStripPartialKey = "";
let managerConsoleStripPartialAt = 0;
// Highest seq of a terminal event in the loaded log. Kept between renders because a group's
// member rows can be built after the render that described them.
let managerConsoleTerminalSeq = 0;
// Reconcile state: the rows now in the transcript, the session they belong to, and whether the
// transcript also holds children no entry owns (the empty placeholder or a dropped session's rows).
// It starts true because the webview markup ships a placeholder inside the transcript.
let managerConsoleRows = [];
let managerConsoleRowsSession = null;
let managerConsoleStaleChildren = true;

// Cheap stand-in for "the transcript would come out the same": a poll that
// brings nothing new must not rebuild the DOM (selection, open <details>, Copied).
function managerConsoleRenderSignature(events, limit) {
  const first = events.length ? events[0] : null;
  const last = events.length ? events[events.length - 1] : null;
  const lastText = last && last.payload && typeof last.payload.text === "string" ? last.payload.text.length : 0;
  const partial = state.managerChatPartial;
  const partialKey = partial
    ? [partial.turn, partial.session_id || "", String(partial.text || "").length, String(partial.reasoning || "").length].join(":")
    : "-";
  return [
    state.managerChatSession || "", events.length, first ? first.seq : "", last ? last.seq : "", last ? last.type : "", lastText,
    partialKey, state.managerChatRunning ? 1 : 0, state.managerChatThinkingSince ? 1 : 0, state.managerChatLastThoughtMs || 0, limit,
  ].join("|");
}

function managerConsoleSetText(node, text) {
  if (node.textContent !== text) node.textContent = text;
}

// The strip above the composer. Every field is text written in place, so a tick never touches a transcript row.
function managerConsoleUpdateStrip() {
  const strip = elements.managerChatStrip;
  if (!strip) return;
  const running = Boolean(state.managerChatRunning);
  const queue = state.managerChatQueuePayload && state.managerChatQueuePayload.send_queue;
  const queued = Array.isArray(queue) ? queue.length : 0;
  const active = running || queued > 0;
  if (strip.hidden === active) strip.hidden = !active;
  if (!active) return;
  const now = Date.now();
  const partial = managerConsolePartialIsCurrent(state.managerChatPartial) ? state.managerChatPartial : null;
  const partialKey = partial ? [partial.turn, String(partial.text || "").length, String(partial.reasoning || "").length].join(":") : "";
  if (partialKey !== managerConsoleStripPartialKey) {
    managerConsoleStripPartialKey = partialKey;
    managerConsoleStripPartialAt = partialKey ? now : 0;
  }
  const since = state.managerChatThinkingSince || 0;
  const events = Array.isArray(state.managerChatEvents) ? state.managerChatEvents : [];
  // Anchored on the turn start, so an event from an earlier turn cannot age the new one.
  const lastAt = Math.max(since, managerChatEventTime(events[events.length - 1]), managerConsoleStripPartialAt);
  managerConsoleSetText(elements.managerChatStripPhase, running ? (partial && partial.text ? "Writing" : "Thinking") + " · " + managerChatFormatDuration(since ? now - since : 0) : "");
  managerConsoleSetText(elements.managerChatStripAge, running && lastAt ? "last event " + managerChatFormatDuration(now - lastAt) : "");
  managerConsoleSetText(elements.managerChatStripQueued, queued > 0 ? queued + " queued" : "");
}

// Only the live Thinking summaries and the strip change between identical renders; touch just those nodes.
function managerConsoleTickThinking() {
  for (const entry of managerConsoleLiveSummaries) {
    const start = managerChatEventTime(entry.event);
    if (start) managerConsoleSetText(entry.summary, "Thinking · " + managerChatFormatDuration(Date.now() - start));
  }
  managerConsoleUpdateStrip();
}

function managerConsoleExtraRows() {
  // Armed before the partial rows return, so the strip's elapsed keeps counting while text streams.
  if (state.managerChatRunning) {
    // The timer belongs to one session and turn: a start left over from an
    // earlier turn or session restarts instead of counting on (the 64m timer).
    const key = managerChatThinkingKey();
    if (!state.managerChatThinkingSince || state.managerChatThinkingKey !== key) {
      state.managerChatThinkingSince = Date.now();
      state.managerChatThinkingKey = key;
    }
  }
  const partialRows = managerConsolePartialRows(state.managerChatPartial);
  if (partialRows.length > 0) return partialRows;
  if (state.managerChatLastThoughtMs && !state.managerChatEvents.some((item) => item && item.type === "reasoning")) {
    const label = "Thought for " + managerChatFormatDuration(state.managerChatLastThoughtMs);
    return [{ key: "extra:last-thought", signature: label, build: () => createElement("div", "manager-chat-thinking", label) }];
  }
  return [];
}

function managerConsoleLoadEarlierRow() {
  return {
    key: "load-earlier",
    signature: "load-earlier",
    build: () => {
      const earlier = createElement("button", "mc-show-all", "load earlier");
      earlier.type = "button";
      // A kept button must not remember the limit it was built under.
      earlier.addEventListener("click", () => {
        state.managerChatRenderLimit = (state.managerChatRenderLimit || MANAGER_CONSOLE_BLOCK_LIMIT) + MANAGER_CONSOLE_BLOCK_LIMIT;
        managerConsoleScheduleRender();
      });
      return earlier;
    },
  };
}

function managerConsoleRowKey(event) {
  const id = event.payload && event.payload.call_id;
  if (id && (event.type === "tool_call" || event.type === "command")) return `${event.type}:${id}`;
  if (event.type === "turn_end") return `turn_end:${event.turn}`;
  return `seq:${event.seq}`;
}

// Providers reuse call ids across turns and a turn can log separated turn_end rows, so a repeat
// takes its occurrence number. Counting the whole merged list, not the visible window, keeps a
// row's key when the window moves.
function managerConsoleRowKeys(merged) {
  const keys = new Map();
  const occurrences = new Map();
  let previous = null;
  for (const event of merged) {
    if (event) {
      const base = managerConsoleRowKey(event);
      const count = occurrences.get(base) || 0;
      // Adjacent same-turn markers collapse into one row, which keeps one key as the turn grows.
      const collapsed = event.type === "turn_end" && previous && previous.type === "turn_end" && previous.turn === event.turn;
      if (!collapsed) occurrences.set(base, count + 1);
      const repeat = collapsed ? count - 1 : count;
      keys.set(event, repeat > 0 ? `${base}#${repeat}` : base);
    }
    previous = event;
  }
  return keys;
}

// Computed without DOM, and while the index is active: footers and reasoning labels read it.
// `chat` (the worker chat's own state, see renderWorkerChatEvents) replaces every manager-only read.
function managerConsoleRowSignature(event, chat = null) {
  const type = String(event.type);
  const payload = managerConsolePayload(event);
  if (type === "turn_end") return JSON.stringify([type, managerConsoleFooterText(event, chat)]);
  if (type === "reasoning") {
    if (chat) return JSON.stringify([type, String(payload.text || "")]);
    // A live label's duration is ticked in place; only its change to "Thought for" rebuilds the row.
    const label = managerChatThoughtSummary(event);
    return JSON.stringify([type, label.startsWith(MANAGER_CONSOLE_LIVE_THOUGHT_PREFIX) ? MANAGER_CONSOLE_LIVE_THOUGHT_PREFIX : label, String(payload.text || "")]);
  }
  if (type === "command") {
    return JSON.stringify([type, managerConsoleCommandStatus(payload, event, chat), payload.command, payload.status, payload.exit_code, payload.output_tail, payload.truncated, payload.original_bytes]);
  }
  // The status is explicit because a result can add only undefined keys, which JSON drops,
  // and because a terminal event changes it without touching the payload.
  if (type === "tool_call") return JSON.stringify([type, managerConsoleToolStatus(payload, event, chat), payload]);
  const clock = type === "assistant_text" || type === "user_message" ? managerChatClockText(event) : "";
  return JSON.stringify([type, clock, payload]);
}

function managerConsoleEventRows(merged, visible, chat = null) {
  const keys = managerConsoleRowKeys(merged);
  const rows = [];
  const pushRow = (event) => {
    if (!MANAGER_CONSOLE_ROW_TYPES.has(String((event && event.type) || ""))) return;
    rows.push({ key: keys.get(event), signature: managerConsoleRowSignature(event, chat), event, build: () => managerChatEventNode(event, chat) });
  };
  let lastTurnEndTurn = null;
  const pending = [];
  const flushPendingTurnEnd = () => {
    if (pending.length > 0) {
      pushRow(pending[pending.length - 1]);
      pending.length = 0;
    }
    lastTurnEndTurn = null;
  };
  for (const event of visible) {
    // Opencode closes every agent step with step_finish, which the backend
    // records as turn_end: collapse consecutive same-turn markers so one
    // turn renders one completion row (the last, with final usage).
    if (event && event.type === "turn_end") {
      if (lastTurnEndTurn !== null && lastTurnEndTurn !== event.turn) flushPendingTurnEnd();
      lastTurnEndTurn = event.turn;
      pending.push(event);
      continue;
    }
    flushPendingTurnEnd();
    const previousRow = rows.length > 0 ? rows[rows.length - 1] : null;
    // A repeated stop records a second marker; the transcript says Stopped once.
    if (event && event.type === "turn_stopped" && previousRow && previousRow.event.type === "turn_stopped") continue;
    // A handoff stops its own turn: the Handoff requested row already says so.
    if (event && event.type === "turn_stopped" && previousRow && previousRow.event.type === "handoff_request"
      && previousRow.event.turn === event.turn) continue;
    // A handoff closes its session for the same reason: one row says so.
    if (event && event.type === "session_close" && previousRow && previousRow.event.type === "handoff_request"
      && String(managerConsolePayload(event).reason || "") === String(managerConsolePayload(previousRow.event).reason || "")) continue;
    // A failing provider repeats one error: the first row keeps its key and counts the repeats.
    if (event && event.type === "error" && previousRow && previousRow.event.type === "error"
      && managerConsoleErrorText(managerConsolePayload(event)) === managerConsoleErrorText(managerConsolePayload(previousRow.event))) {
      const previousPayload = managerConsolePayload(previousRow.event);
      const repeated = { ...previousRow.event, payload: { ...previousPayload, repeat_count: (previousPayload.repeat_count || 1) + 1 } };
      previousRow.event = repeated;
      previousRow.signature = managerConsoleRowSignature(repeated, chat);
      previousRow.build = () => managerChatEventNode(repeated, chat);
      continue;
    }
    pushRow(event);
  }
  flushPendingTurnEnd();
  return rows;
}

function managerConsoleCountLabel(count, noun) {
  return `${count} ${noun}${count === 1 ? "" : "s"}`;
}

function managerConsoleActivitySummary(members, chat = null) {
  let tools = 0;
  let commands = 0;
  let files = 0;
  let failed = 0;
  let interrupted = 0;
  let running = "";
  for (const { event } of members) {
    const payload = managerConsolePayload(event);
    if (event.type === "tool_call" || event.type === "tool_result") {
      tools += 1;
      const status = event.type === "tool_call" ? managerConsoleToolStatus(payload, event, chat) : payload.is_error ? "failed" : "done";
      if (status === "failed") failed += 1;
      if (status === "interrupted") interrupted += 1;
      if (status === "running") running = String(payload.name || payload.tool || "tool");
    } else if (event.type === "command") {
      commands += 1;
      const status = managerConsoleCommandStatus(payload, event, chat);
      if (status === "failed") failed += 1;
      if (status === "interrupted") interrupted += 1;
      if (status === "running") running = limitText(String(payload.command || "command"), 60);
    } else if (event.type === "file_change") {
      files += 1;
    }
  }
  const parts = [];
  if (tools) parts.push(managerConsoleCountLabel(tools, "tool"));
  if (commands) parts.push(managerConsoleCountLabel(commands, "command"));
  if (files) parts.push(managerConsoleCountLabel(files, "file"));
  if (failed) parts.push(`${failed} failed`);
  if (interrupted) parts.push(`${interrupted} interrupted`);
  if (running) parts.push(`running ${running}`);
  return { text: parts.join(" · "), failed: failed > 0 };
}

function managerConsoleGroupRow(members, chat = null) {
  const summary = managerConsoleActivitySummary(members, chat);
  const className = summary.failed ? "mc-activity-group has-failed" : "mc-activity-group";
  return {
    // Namespaced so a plain row and the group that grew out of it are never "the same row".
    key: `group:${members[0].key}`,
    signature: JSON.stringify([summary.text, className]),
    summaryText: summary.text,
    className,
    members,
    build: () => {
      const details = createElement("details", className);
      details.open = false;
      details.appendChild(createElement("summary", "mc-activity-summary", summary.text));
      details.appendChild(createElement("div", "mc-activity-body"));
      return details;
    },
  };
}

function managerConsoleGroupRuns(rows, chat = null) {
  const grouped = [];
  let run = [];
  const flushRun = () => {
    if (run.filter((row) => row.event.type !== "reasoning").length >= 2) grouped.push(managerConsoleGroupRow(run, chat));
    else grouped.push(...run);
    run = [];
  };
  for (const row of rows) {
    if (MANAGER_CONSOLE_ACTIVITY_TYPES.has(row.event.type)) {
      run.push(row);
    } else {
      flushRun();
      grouped.push(row);
    }
  }
  flushRun();
  return grouped;
}

// A row can change list when a run becomes a group or a window move splits one, so entries are
// found by key across the whole previous transcript, not only among their old siblings.
function managerConsoleRowPool(entries) {
  const pool = { rows: new Map(), groups: new Map(), memberGroups: new Map(), claimedGroups: new Set(), writes: 0 };
  for (const entry of entries) {
    if (!entry.members) {
      pool.rows.set(entry.key, entry);
      continue;
    }
    pool.groups.set(entry.key, entry);
    for (const member of entry.members) {
      pool.rows.set(member.key, member);
      pool.memberGroups.set(member.key, entry);
    }
  }
  return pool;
}

// A group whose first member changed (the window moved) is still that group if a member survives.
function managerConsoleClaim(pool, descriptor) {
  if (!descriptor.members) return pool.rows.get(descriptor.key) || null;
  let entry = pool.groups.get(descriptor.key) || null;
  for (let index = 0; !entry && index < descriptor.members.length; index += 1) {
    const candidate = pool.memberGroups.get(descriptor.members[index].key);
    if (candidate && !pool.claimedGroups.has(candidate)) entry = candidate;
  }
  if (entry) pool.claimedGroups.add(entry);
  return entry;
}

// Anything a build registers for ticking belongs to that row and leaves with it.
function managerConsoleBuildRow(descriptor) {
  const liveStart = managerConsoleLiveSummaries.length;
  const node = descriptor.build();
  return { node, live: managerConsoleLiveSummaries.splice(liveStart) };
}

function managerConsoleNewEntry(descriptor) {
  const built = managerConsoleBuildRow(descriptor);
  const entry = { key: descriptor.key, signature: descriptor.signature, node: built.node, live: built.live, parent: null };
  if (descriptor.members) {
    entry.summary = built.node.children[0];
    entry.body = built.node.children[1];
    entry.members = [];
  }
  return entry;
}

function managerConsoleDetach(entry, pool) {
  if (!entry.parent) return;
  entry.parent.removeChild(entry.node);
  entry.parent = null;
  pool.writes += 1;
}

function managerConsoleDetailsIn(node, found = []) {
  if (node.tagName === "DETAILS") found.push(node);
  for (const child of node.children || []) managerConsoleDetailsIn(child, found);
  return found;
}

function managerConsoleRebuildRow(entry, descriptor, pool) {
  const built = managerConsoleBuildRow(descriptor);
  const previous = managerConsoleDetailsIn(entry.node);
  managerConsoleDetailsIn(built.node).forEach((details, index) => {
    if (index < previous.length) details.open = previous[index].open;
  });
  entry.parent.insertBefore(built.node, entry.node);
  entry.parent.removeChild(entry.node);
  entry.node = built.node;
  entry.live = built.live;
  entry.signature = descriptor.signature;
  pool.writes += 1;
}

function managerConsoleSyncGroup(entry, descriptor, pool) {
  entry.key = descriptor.key;
  if (entry.signature !== descriptor.signature) {
    managerConsoleSetText(entry.summary, descriptor.summaryText);
    if (entry.node.className !== descriptor.className) entry.node.className = descriptor.className;
    entry.signature = descriptor.signature;
    pool.writes += 1;
  }
  entry.members = managerConsoleReconcile(entry.body, entry.members, descriptor.members, pool);
}

// One list under one parent, the only code that changes the transcript's rows. A row with the same
// key and signature is never touched, so its open state, selection and Copied label survive.
function managerConsoleReconcile(parent, previous, descriptors, pool) {
  const claimed = descriptors.map((descriptor) => managerConsoleClaim(pool, descriptor));
  const resident = new Set(claimed.filter((entry) => entry && entry.parent === parent));
  for (const entry of previous) {
    if (entry.parent === parent && !resident.has(entry)) managerConsoleDetach(entry, pool);
  }
  const anchors = previous.filter((entry) => resident.has(entry));
  const placed = new Set();
  let cursor = 0;
  return descriptors.map((descriptor, index) => {
    while (cursor < anchors.length && placed.has(anchors[cursor])) cursor += 1;
    const anchor = cursor < anchors.length ? anchors[cursor] : null;
    const entry = claimed[index] || managerConsoleNewEntry(descriptor);
    if (entry === anchor) {
      cursor += 1;
    } else {
      managerConsoleDetach(entry, pool);
      parent.insertBefore(entry.node, anchor ? anchor.node : null);
      entry.parent = parent;
      pool.writes += 1;
    }
    placed.add(entry);
    if (descriptor.members) managerConsoleSyncGroup(entry, descriptor, pool);
    else if (entry.signature !== descriptor.signature) managerConsoleRebuildRow(entry, descriptor, pool);
    return entry;
  });
}

function managerConsoleCollectLive(entries, found = []) {
  for (const entry of entries) {
    found.push(...entry.live);
    if (entry.members) managerConsoleCollectLive(entry.members, found);
  }
  return found;
}

function managerConsoleForgetRows() {
  if (managerConsoleRows.length > 0) managerConsoleStaleChildren = true;
  managerConsoleRows = [];
  managerConsoleLiveSummaries = [];
}

function renderManagerChatEvents() {
  if (!elements.managerChatTranscript) return;
  managerConsoleApplyHairline();
  const events = Array.isArray(state.managerChatEvents) ? state.managerChatEvents : [];
  managerConsoleTerminalSeq = managerConsoleHighestTerminalSeq(events);
  const limit = state.managerChatRenderLimit || MANAGER_CONSOLE_BLOCK_LIMIT;
  if (managerConsoleRenderSignature(events, limit) === managerConsoleLastSignature) {
    managerConsoleTickThinking();
    return;
  }
  const box = elements.managerChatTranscript;
  const scrollTop = box.scrollTop;
  const scrollHeight = box.scrollHeight;
  const atBottom = scrollHeight - scrollTop - (box.clientHeight || 0) < 24;
  const prepending = managerConsoleRenderedLimit !== null && limit > managerConsoleRenderedLimit;
  const session = String(state.managerChatSession || "");
  if (session !== managerConsoleRowsSession) {
    managerConsoleForgetRows();
    managerConsoleRowsSession = session;
  }
  const extras = managerConsoleExtraRows();
  const merged = managerConsoleMergeTools(managerConsoleMergeCommands(events));
  const eventLimit = Math.max(0, limit - extras.length);
  const hasEarlier = merged.length > eventLimit;
  const visible = hasEarlier ? merged.slice(merged.length - eventLimit) : merged;
  const pool = managerConsoleRowPool(managerConsoleRows);
  managerConsoleActiveIndex = managerConsoleBuildIndex(events);
  try {
    const descriptors = managerConsoleGroupRuns(managerConsoleEventRows(merged, visible));
    descriptors.push(...extras);
    if (hasEarlier) descriptors.unshift(managerConsoleLoadEarlierRow());
    if (descriptors.length === 0) {
      managerConsoleForgetRows();
      managerConsoleStaleChildren = true;
      managerConsoleRenderedLimit = limit;
      managerConsoleLastSignature = managerConsoleRenderSignature(events, limit);
      box.replaceChildren(
        createElement("div", "panel-list-empty compact", state.managerChatSession ? "No events yet" : "No open session"),
      );
      if (elements.managerChatLatest) elements.managerChatLatest.hidden = true;
      return;
    }
    if (managerConsoleStaleChildren) {
      box.replaceChildren();
      managerConsoleStaleChildren = false;
    }
    managerConsoleRows = managerConsoleReconcile(box, managerConsoleRows, descriptors, pool);
  } catch (error) {
    // A half-applied reconcile leaves children no entry describes: start clean on the next render.
    managerConsoleForgetRows();
    managerConsoleStaleChildren = true;
    managerConsoleLastSignature = null;
    throw error;
  } finally {
    managerConsoleActiveIndex = null;
  }
  managerConsoleRenderedLimit = limit;
  managerConsoleLastSignature = managerConsoleRenderSignature(events, limit);
  managerConsoleLiveSummaries = managerConsoleCollectLive(managerConsoleRows);
  managerConsoleTickThinking();
  if (pool.writes > 0) {
    if (atBottom) box.scrollTop = box.scrollHeight;
    else box.scrollTop = prepending ? scrollTop + (box.scrollHeight - scrollHeight) : scrollTop;
  }
  if (elements.managerChatLatest && elements.managerChatLatest.hidden !== atBottom) elements.managerChatLatest.hidden = atBottom;
  managerConsoleAnnounce(events);
}

const MANAGER_CONSOLE_BLOCK_LIMIT = 400;
// Emphasis needs non-space just inside the delimiters and no word or delimiter character just outside
// them, so snake_case_name, "a * b * c" and 2**10 stay verbatim.
const MANAGER_CONSOLE_INLINE = /`([^`]+)`|(?<![\w*])\*\*(?=[^\s*])([^*]*[^\s*])\*\*(?![\w*])|(?<![\w*])\*(?=[^\s*])([^*]*[^\s*])\*(?![\w*])|(?<![\w])_(?=[^\s_])([^_]*[^\s_])_(?!\w)|\[([^\]]+)\]\(([^)\s]+)\)/g;

function managerConsoleInline(parent, text) {
  const source = String(text);
  let cursor = 0;
  for (const match of source.matchAll(MANAGER_CONSOLE_INLINE)) {
    if (match.index > cursor) parent.appendChild(document.createTextNode(source.slice(cursor, match.index)));
    cursor = match.index + match[0].length;
    if (match[1] !== undefined) parent.appendChild(createElement("code", "mc-mono", match[1]));
    else if (match[2] !== undefined) parent.appendChild(createElement("strong", "", match[2]));
    else if (match[3] !== undefined || match[4] !== undefined) parent.appendChild(createElement("em", "", match[3] !== undefined ? match[3] : match[4]));
    else if (/^https?:\/\//i.test(match[6])) {
      const link = createElement("a", "mc-link", match[5]);
      link.setAttribute("href", match[6]);
      parent.appendChild(link);
    } else parent.appendChild(document.createTextNode(`${match[5]} (${match[6]})`));
  }
  if (cursor < source.length) parent.appendChild(document.createTextNode(source.slice(cursor)));
}

function managerConsoleCodeNode(code) {
  const full = code.join("\n");
  const folded = code.length > MANAGER_CONSOLE_DIFF_LINES;
  const wrap = createElement("div", "mc-code-block");
  wrap.appendChild(managerConsoleCopyButton(() => full));
  const pre = createElement("pre", "mc-code", folded ? code.slice(0, MANAGER_CONSOLE_DIFF_LINES).join("\n") : full);
  if (folded) wrap.appendChild(managerConsoleShowAll(`show all (${code.length} lines)`, () => { pre.textContent = full; }));
  wrap.appendChild(pre);
  return wrap;
}

const MANAGER_CONSOLE_FENCE = /^(\s*)(`{3,})/;
const MANAGER_CONSOLE_HEADING = /^ {0,3}(#{1,6})\s+(\S.*)$/;
const MANAGER_CONSOLE_HR = /^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$/;
const MANAGER_CONSOLE_QUOTE = /^ {0,3}> ?/;
const MANAGER_CONSOLE_LIST_ITEM = /^(\s*)([-*+]|\d+\.)\s+(.*)$/;
// Each quote level recurses once, so a line of ">" cannot run the stack out.
const MANAGER_CONSOLE_QUOTE_DEPTH = 8;

function managerConsoleFenceCloses(line, fence) {
  const trimmed = line.trim();
  return trimmed.startsWith(fence) && /^`+$/.test(trimmed);
}

function managerConsoleFence(parent, lines, start, open) {
  const indent = new RegExp(`^\\s{0,${open[1].length}}`);
  const code = [];
  let index = start + 1;
  while (index < lines.length && !managerConsoleFenceCloses(lines[index], open[2])) code.push(lines[index++].replace(indent, ""));
  parent.appendChild(managerConsoleCodeNode(code));
  return index + 1;
}

function managerConsoleTableCells(line) {
  let row = line.trim();
  if (row.startsWith("|")) row = row.slice(1);
  if (row.endsWith("|") && !row.endsWith("\\|")) row = row.slice(0, -1);
  return row.split(/(?<!\\)\|/).map((cell) => cell.trim().replace(/\\\|/g, "|"));
}

function managerConsoleIsTableStart(lines, index) {
  if (index + 1 >= lines.length || !lines[index].includes("|") || !lines[index + 1].includes("|")) return false;
  const delimiter = managerConsoleTableCells(lines[index + 1]);
  return delimiter.length === managerConsoleTableCells(lines[index]).length && delimiter.every((cell) => /^:?-+:?$/.test(cell));
}

function managerConsoleIsBlockStart(lines, index) {
  const line = lines[index];
  return MANAGER_CONSOLE_FENCE.test(line) || MANAGER_CONSOLE_HEADING.test(line) || MANAGER_CONSOLE_HR.test(line)
    || MANAGER_CONSOLE_QUOTE.test(line) || MANAGER_CONSOLE_LIST_ITEM.test(line) || managerConsoleIsTableStart(lines, index);
}

function managerConsoleTableRow(section, tag, columns, cells) {
  const row = createElement("tr", "");
  for (let column = 0; column < columns; column += 1) {
    const cell = createElement(tag, "");
    managerConsoleInline(cell, cells[column] || "");
    row.appendChild(cell);
  }
  section.appendChild(row);
}

function managerConsoleTable(parent, lines, start) {
  const header = managerConsoleTableCells(lines[start]);
  const wrap = createElement("div", "mc-table-wrap");
  const table = createElement("table", "mc-table");
  const head = createElement("thead", "");
  const body = createElement("tbody", "");
  managerConsoleTableRow(head, "th", header.length, header);
  let index = start + 2;
  while (index < lines.length && lines[index].trim() && lines[index].includes("|") && !managerConsoleIsBlockStart(lines, index)) {
    managerConsoleTableRow(body, "td", header.length, managerConsoleTableCells(lines[index++]));
  }
  table.appendChild(head);
  table.appendChild(body);
  wrap.appendChild(table);
  parent.appendChild(wrap);
  return index;
}

function managerConsoleListLevel(container, indent, ordered, marker) {
  const list = createElement(ordered ? "ol" : "ul", "mc-list");
  const start = ordered ? Number.parseInt(marker, 10) : 1;
  if (start !== 1) list.setAttribute("start", String(start));
  container.appendChild(list);
  return { container, indent, ordered, list, item: null };
}

function managerConsoleListItem(parent, levels, match) {
  const indent = match[1].replace(/\t/g, "    ").length;
  const ordered = /\d/.test(match[2]);
  while (levels.length > 1 && indent < levels[levels.length - 1].indent) levels.pop();
  let level = levels[levels.length - 1];
  if (!level) {
    level = managerConsoleListLevel(parent, indent, ordered, match[2]);
    levels.push(level);
  } else if (indent >= level.indent + 2) {
    level = managerConsoleListLevel(level.item, indent, ordered, match[2]);
    levels.push(level);
  } else if (level.ordered !== ordered) {
    level = managerConsoleListLevel(level.container, level.indent, ordered, match[2]);
    levels[levels.length - 1] = level;
  }
  level.item = createElement("li", "");
  managerConsoleInline(level.item, match[3]);
  level.list.appendChild(level.item);
}

function managerConsoleListItemAt(lines, index) {
  if (index >= lines.length || MANAGER_CONSOLE_HR.test(lines[index])) return null;
  return MANAGER_CONSOLE_LIST_ITEM.exec(lines[index]);
}

function managerConsoleList(parent, lines, start) {
  const levels = [];
  let index = start;
  while (index < lines.length) {
    // A blank line between items does not end the list; only what follows it can.
    let next = index;
    while (next < lines.length && !lines[next].trim()) next += 1;
    const match = managerConsoleListItemAt(lines, next);
    if (match) {
      managerConsoleListItem(parent, levels, match);
      index = next + 1;
      continue;
    }
    if (next !== index || !/^\s{2,}\S/.test(lines[index]) || managerConsoleIsBlockStart(lines, index)) break;
    managerConsoleInline(levels[levels.length - 1].item, `\n${lines[index].trim()}`);
    index += 1;
  }
  return index;
}

function managerConsoleMarkdown(text, depth = 0) {
  const fragment = document.createDocumentFragment();
  const lines = String(text || "").split(/\r?\n/);
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    // Fences come first: nothing below may ever see a line inside one.
    const fence = MANAGER_CONSOLE_FENCE.exec(line);
    if (fence) { index = managerConsoleFence(fragment, lines, index, fence); continue; }
    if (!line.trim()) { index += 1; continue; }
    const heading = MANAGER_CONSOLE_HEADING.exec(line);
    if (heading) {
      const node = createElement(`h${Math.min(heading[1].length + 2, 6)}`, "mc-h");
      managerConsoleInline(node, heading[2].trimEnd());
      fragment.appendChild(node);
      index += 1;
      continue;
    }
    if (MANAGER_CONSOLE_HR.test(line)) {
      fragment.appendChild(createElement("hr", "mc-hr"));
      index += 1;
      continue;
    }
    if (depth < MANAGER_CONSOLE_QUOTE_DEPTH && MANAGER_CONSOLE_QUOTE.test(line)) {
      const quoted = [];
      while (index < lines.length && MANAGER_CONSOLE_QUOTE.test(lines[index])) quoted.push(lines[index++].replace(MANAGER_CONSOLE_QUOTE, ""));
      const quote = createElement("blockquote", "mc-quote");
      quote.appendChild(managerConsoleMarkdown(quoted.join("\n"), depth + 1));
      fragment.appendChild(quote);
      continue;
    }
    if (managerConsoleIsTableStart(lines, index)) { index = managerConsoleTable(fragment, lines, index); continue; }
    if (MANAGER_CONSOLE_LIST_ITEM.test(line)) { index = managerConsoleList(fragment, lines, index); continue; }
    const paragraph = createElement("p", "mc-p");
    const words = [lines[index++]];
    while (index < lines.length && lines[index].trim() && !managerConsoleIsBlockStart(lines, index)) words.push(lines[index++]);
    managerConsoleInline(paragraph, words.join("\n"));
    fragment.appendChild(paragraph);
  }
  return fragment;
}

function managerConsoleTurnFinished(turn) {
  // NF-2026-01230: only turn_end (or an owner stop) ends a turn; a mid-turn assistant_text already settled its streamed text server side.
  return (state.managerChatEvents || []).some((item) => item && item.turn === turn && (item.type === "turn_end" || item.type === "turn_stopped"));
}

function managerConsolePartialIsCurrent(partial) {
  if (!partial || !state.managerChatSession || !Number.isInteger(partial.turn) || managerConsoleTurnFinished(partial.turn)) return false;
  if (partial.session_id && partial.session_id !== state.managerChatSession) return false;
  const latestTurn = (state.managerChatEvents || []).reduce((latest, item) => item && Number.isInteger(item.turn) ? Math.max(latest, item.turn) : latest, 0);
  return partial.turn >= latestTurn;
}

function managerConsolePartialRows(partial) {
  if (!managerConsolePartialIsCurrent(partial)) return [];
  return managerConsoleStreamingRows(partial);
}

// The streamed-but-unsettled text and reasoning rows; shared by the manager console and the worker chat.
function managerConsoleStreamingRows(partial) {
  const rows = [];
  if (partial.reasoning) {
    rows.push({
      key: "extra:partial-reasoning",
      signature: String(partial.reasoning),
      build: () => {
        const details = createElement("details", "mc-thinking");
        details.open = true;
        details.appendChild(createElement("summary", "", "Thinking"));
        details.appendChild(createElement("div", "mc-thinking-text", partial.reasoning));
        return managerConsoleBlock("reasoning", details);
      },
    });
  }
  if (partial.text) {
    rows.push({
      key: "extra:partial-text",
      signature: String(partial.text),
      build: () => {
        const body = createElement("div", "mc-body mc-streaming");
        body.appendChild(managerConsoleMarkdown(partial.text));
        const caret = createElement("span", "mc-caret");
        caret.setAttribute("aria-hidden", "true");
        body.appendChild(caret);
        return managerConsoleBlock("assistant_text", body);
      },
    });
  }
  return rows;
}

function managerConsolePartialNodes(partial) {
  return managerConsolePartialRows(partial).map((row) => row.build());
}

let managerConsoleFramePending = false;

function managerConsoleScheduleRender() {
  if (managerConsoleFramePending) return;
  managerConsoleFramePending = true;
  window.requestAnimationFrame(() => {
    managerConsoleFramePending = false;
    renderManagerChatEvents();
  });
}

function managerConsoleAnnounce(events) {
  if (!elements.managerChatAnnouncer) return;
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index];
    if (!event || event.type !== "assistant_text") continue;
    if (event.seq > (state.managerChatAnnouncedSeq || 0)) {
      state.managerChatAnnouncedSeq = event.seq;
      elements.managerChatAnnouncer.textContent = String((event.payload && event.payload.text) || "");
    }
    return;
  }
}

function managerConsoleReset() {
  state.managerChatPartial = null;
  managerConsoleLastSignature = null;
  managerConsoleRenderedLimit = null;
  state.managerChatRenderLimit = MANAGER_CONSOLE_BLOCK_LIMIT;
  state.managerChatAnnouncedSeq = 0;
  state.managerChatThinkingSince = 0;
  state.managerChatLastThoughtMs = 0;
  managerConsoleForgetRows();
  if (elements.managerChatAnnouncer) elements.managerChatAnnouncer.textContent = "";
  if (elements.managerChatLatest) elements.managerChatLatest.hidden = true;
  if (elements.managerChatTranscript) elements.managerChatTranscript.scrollTop = elements.managerChatTranscript.scrollHeight;
}

// ── Worker Live Output as a chat (NF-2026-02222) ──────────────────────────────
// A port of aiworkhub.manager_loop_backends.translate/flush (claude_cli, codex_cli, opencode_cli),
// behaviour-identical to the Python and checked against tests/fixtures/manager_streams by
// live-output-chat-view-nf02222.test.js. Pure: it reads no DOM and no module state. The py*
// helpers spell out the Python semantics the port depends on (truthiness, str(), strip()).

const LIVE_OUTPUT_OUTPUT_TAIL_BYTES = 8 * 1024;
const LIVE_OUTPUT_MAX_ERROR_DETAIL_CHARS = 500;
const LIVE_OUTPUT_CLAUDE_COMMAND_TOOLS = new Set(["Bash"]);
const LIVE_OUTPUT_CLAUDE_EDIT_TOOLS = new Set(["Edit", "MultiEdit", "Write"]);
const LIVE_OUTPUT_CODEX_TOOL_ITEMS = new Set(["command_execution", "mcp_tool_call", "file_change", "web_search"]);
// str.strip() whitespace and str.splitlines() boundaries, as code points.
const LIVE_OUTPUT_PY_SPACE = new Set([
  9, 10, 11, 12, 13, 28, 29, 30, 31, 32, 133, 160, 5760, 8192, 8193, 8194, 8195, 8196, 8197, 8198, 8199,
  8200, 8201, 8202, 8232, 8233, 8239, 8287, 12288,
]);
const LIVE_OUTPUT_PY_LINE_BREAKS = new Set([10, 11, 12, 13, 28, 29, 30, 133, 8232, 8233]);

function liveOutputIsMapping(value) {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function liveOutputMapping(value) {
  return liveOutputIsMapping(value) ? value : {};
}

function liveOutputHas(mapping, key) {
  return Object.prototype.hasOwnProperty.call(mapping, key);
}

// mapping.get(key): None (null) when the key is absent.
function liveOutputGet(mapping, key) {
  return liveOutputHas(mapping, key) && mapping[key] !== undefined ? mapping[key] : null;
}

function liveOutputPyTruthy(value) {
  if (value === null || value === undefined || value === false || value === 0 || value === "") return false;
  if (Array.isArray(value)) return value.length > 0;
  if (typeof value === "object") return Object.keys(value).length > 0;
  return true;
}

// Python `a or b or c`: the first truthy value, else the last one.
function liveOutputPyOr(...values) {
  for (const value of values) {
    if (liveOutputPyTruthy(value)) return value;
  }
  const last = values[values.length - 1];
  return last === undefined ? null : last;
}

// json.dumps(value) with its default separators and ensure_ascii.
function liveOutputPyJson(value) {
  if (value === null || value === undefined) return "null";
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "number") return Number.isFinite(value) ? String(value) : Number.isNaN(value) ? "NaN" : value > 0 ? "Infinity" : "-Infinity";
  if (typeof value === "string") {
    return JSON.stringify(value).replace(/[^\x00-\x7f]/g, (char) => "\\u" + char.charCodeAt(0).toString(16).padStart(4, "0"));
  }
  if (Array.isArray(value)) return "[" + value.map(liveOutputPyJson).join(", ") + "]";
  return "{" + Object.keys(value).map((key) => liveOutputPyJson(key) + ": " + liveOutputPyJson(value[key])).join(", ") + "}";
}

function liveOutputPyStr(value) {
  if (value === null || value === undefined) return "None";
  if (typeof value === "string") return value;
  if (typeof value === "boolean") return value ? "True" : "False";
  if (typeof value === "number") return String(value);
  return liveOutputPyJson(value);
}

function liveOutputPyStrip(value) {
  const text = String(value);
  let start = 0;
  let end = text.length;
  while (start < end && LIVE_OUTPUT_PY_SPACE.has(text.charCodeAt(start))) start += 1;
  while (end > start && LIVE_OUTPUT_PY_SPACE.has(text.charCodeAt(end - 1))) end -= 1;
  return text.slice(start, end);
}

// str.splitlines(): every Python line boundary, "\r\n" as one, no trailing empty line.
function liveOutputPySplitLines(value) {
  const text = String(value || "");
  const lines = [];
  let start = 0;
  for (let index = 0; index < text.length; index += 1) {
    const code = text.charCodeAt(index);
    if (!LIVE_OUTPUT_PY_LINE_BREAKS.has(code)) continue;
    lines.push(text.slice(start, index));
    if (code === 13 && text.charCodeAt(index + 1) === 10) index += 1;
    start = index + 1;
  }
  if (start < text.length) lines.push(text.slice(start));
  return lines;
}

function liveOutputPyInt(value) {
  return Number.isInteger(value) || typeof value === "boolean";
}

function liveOutputUtf8(text) {
  return new TextEncoder().encode(String(text));
}

// manager_loop.keep_tail: the last `limit` UTF-8 bytes; a cut leading character is dropped.
function liveOutputKeepTail(text, limit) {
  const data = liveOutputUtf8(text);
  if (data.length <= limit) return String(text);
  let tail = data.slice(data.length - limit);
  let skip = 0;
  while (skip < tail.length && (tail[skip] & 0xc0) === 0x80) skip += 1;
  tail = tail.slice(skip);
  return new TextDecoder("utf-8", { fatal: false }).decode(tail);
}

function liveOutputCount(value) {
  if (typeof value === "boolean") return value ? 1 : 0;
  return typeof value === "number" && value > 0 ? Math.trunc(value) : 0;
}

function liveOutputTurnContext() {
  return { calls: new Map(), last_usage: {}, model: "", steps: {}, reasoning_redacted: 0, backend: "" };
}

function liveOutputUsage(input, cacheRead, cacheWrite, output, window, raw, fill = null) {
  const size = liveOutputCount(window);
  const fillRatio = size && fill !== null && fill !== undefined ? Number((fill / size).toFixed(4)) : null;
  return {
    input: liveOutputCount(input), cache_read: liveOutputCount(cacheRead), cache_write: liveOutputCount(cacheWrite),
    output: liveOutputCount(output), context_window: size || null, context_fill: fillRatio, raw: { ...raw },
  };
}

function liveOutputTurnError(source, detail) {
  return { type: "error", payload: { source, error: Array.from(liveOutputPyStr(detail)).slice(0, LIVE_OUTPUT_MAX_ERROR_DETAIL_CHARS).join("") } };
}

function liveOutputResultText(content) {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content.filter((block) => liveOutputIsMapping(block) && block.type === "text")
      .map((block) => liveOutputPyStr(liveOutputPyOr(liveOutputGet(block, "text"), ""))).join("\n");
  }
  return content === null || content === undefined ? "" : liveOutputPyJson(content);
}

function liveOutputCommand(callId, command, status, exitCode = null, output = "", cwd = "") {
  return { type: "command", payload: {
    call_id: callId, command, cwd, status, exit_code: liveOutputPyInt(exitCode) ? exitCode : null,
    output_tail: liveOutputKeepTail(output, LIVE_OUTPUT_OUTPUT_TAIL_BYTES), output_bytes: liveOutputUtf8(output).length,
  } };
}

// difflib.SequenceMatcher(None, a, b) with its default autojunk, as its matching blocks.
function liveOutputMatchingBlocks(a, b) {
  const b2j = new Map();
  b.forEach((line, index) => {
    if (!b2j.has(line)) b2j.set(line, []);
    b2j.get(line).push(index);
  });
  if (b.length >= 200) {
    const ntest = Math.floor(b.length / 100) + 1;
    for (const [line, indices] of [...b2j]) if (indices.length > ntest) b2j.delete(line);
  }
  const longest = (alo, ahi, blo, bhi) => {
    let besti = alo;
    let bestj = blo;
    let bestsize = 0;
    let j2len = new Map();
    for (let i = alo; i < ahi; i += 1) {
      const next = new Map();
      for (const j of b2j.get(a[i]) || []) {
        if (j < blo) continue;
        if (j >= bhi) break;
        const k = (j2len.get(j - 1) || 0) + 1;
        next.set(j, k);
        if (k > bestsize) {
          besti = i - k + 1;
          bestj = j - k + 1;
          bestsize = k;
        }
      }
      j2len = next;
    }
    while (besti > alo && bestj > blo && a[besti - 1] === b[bestj - 1]) {
      besti -= 1;
      bestj -= 1;
      bestsize += 1;
    }
    while (besti + bestsize < ahi && bestj + bestsize < bhi && a[besti + bestsize] === b[bestj + bestsize]) bestsize += 1;
    return [besti, bestj, bestsize];
  };
  const queue = [[0, a.length, 0, b.length]];
  const blocks = [];
  while (queue.length) {
    const [alo, ahi, blo, bhi] = queue.pop();
    const match = longest(alo, ahi, blo, bhi);
    const [i, j, k] = match;
    if (k) {
      blocks.push(match);
      if (alo < i && blo < j) queue.push([alo, i, blo, j]);
      if (i + k < ahi && j + k < bhi) queue.push([i + k, ahi, j + k, bhi]);
    }
  }
  blocks.sort((x, y) => x[0] - y[0] || x[1] - y[1] || x[2] - y[2]);
  const joined = [];
  let [i1, j1, k1] = [0, 0, 0];
  for (const [i2, j2, k2] of blocks) {
    if (i1 + k1 === i2 && j1 + k1 === j2) {
      k1 += k2;
    } else {
      if (k1) joined.push([i1, j1, k1]);
      [i1, j1, k1] = [i2, j2, k2];
    }
  }
  if (k1) joined.push([i1, j1, k1]);
  joined.push([a.length, b.length, 0]);
  return joined;
}

// SequenceMatcher.get_grouped_opcodes(n).
function liveOutputGroupedOpcodes(a, b, n = 3) {
  let codes = [];
  let i = 0;
  let j = 0;
  for (const [ai, bj, size] of liveOutputMatchingBlocks(a, b)) {
    const tag = i < ai && j < bj ? "replace" : i < ai ? "delete" : j < bj ? "insert" : "";
    if (tag) codes.push([tag, i, ai, j, bj]);
    i = ai + size;
    j = bj + size;
    if (size) codes.push(["equal", ai, i, bj, j]);
  }
  if (!codes.length) codes = [["equal", 0, 1, 0, 1]];
  if (codes[0][0] === "equal") {
    const [tag, c1, c2, d1, d2] = codes[0];
    codes[0] = [tag, Math.max(c1, c2 - n), c2, Math.max(d1, d2 - n), d2];
  }
  if (codes[codes.length - 1][0] === "equal") {
    const [tag, c1, c2, d1, d2] = codes[codes.length - 1];
    codes[codes.length - 1] = [tag, c1, Math.min(c2, c1 + n), d1, Math.min(d2, d1 + n)];
  }
  const groups = [];
  let group = [];
  for (const code of codes) {
    let [tag, c1, c2, d1, d2] = code;
    if (tag === "equal" && c2 - c1 > n + n) {
      group.push([tag, c1, Math.min(c2, c1 + n), d1, Math.min(d2, d1 + n)]);
      groups.push(group);
      group = [];
      c1 = Math.max(c1, c2 - n);
      d1 = Math.max(d1, d2 - n);
    }
    group.push([tag, c1, c2, d1, d2]);
  }
  if (group.length && !(group.length === 1 && group[0][0] === "equal")) groups.push(group);
  return groups;
}

function liveOutputUnifiedRange(start, stop) {
  let beginning = start + 1;
  const length = stop - start;
  if (length === 1) return String(beginning);
  if (!length) beginning -= 1;
  return `${beginning},${length}`;
}

// difflib.unified_diff(a, b, fromfile, tofile, lineterm="").
function liveOutputUnifiedDiff(a, b, fromFile, toFile) {
  const out = [];
  for (const group of liveOutputGroupedOpcodes(a, b)) {
    if (!out.length) out.push(`--- ${fromFile}`, `+++ ${toFile}`);
    const first = group[0];
    const last = group[group.length - 1];
    out.push(`@@ -${liveOutputUnifiedRange(first[1], last[2])} +${liveOutputUnifiedRange(first[3], last[4])} @@`);
    for (const [tag, i1, i2, j1, j2] of group) {
      if (tag === "equal") {
        for (const line of a.slice(i1, i2)) out.push(" " + line);
        continue;
      }
      if (tag === "replace" || tag === "delete") for (const line of a.slice(i1, i2)) out.push("-" + line);
      if (tag === "replace" || tag === "insert") for (const line of b.slice(j1, j2)) out.push("+" + line);
    }
  }
  return out;
}

// One file change; each (before, after) pair becomes one hunk of a unified diff.
function liveOutputFileChange(callId, path, pairs, kind) {
  const lines = [];
  let added = 0;
  let removed = 0;
  for (const [before, after] of pairs) {
    const hunk = liveOutputUnifiedDiff(liveOutputPySplitLines(before), liveOutputPySplitLines(after), `a/${path}`, `b/${path}`);
    const body = hunk.slice(2);
    added += body.filter((line) => line.startsWith("+")).length;
    removed += body.filter((line) => line.startsWith("-")).length;
    lines.push(...(lines.length ? body : hunk));
  }
  return { type: "file_change", payload: { call_id: callId, path, kind, diff: lines.join("\n"), added, removed } };
}

function liveOutputToolCall(callId, name, request) {
  return { type: "tool_call", payload: { call_id: callId, name, input: request === undefined ? null : request } };
}

function liveOutputToolResult(callId, name, output, failed) {
  return { type: "tool_result", payload: { call_id: callId, name, output: output === undefined ? null : output, is_error: failed } };
}

function liveOutputTurnEnd(usage, context = null) {
  const payload = liveOutputPyTruthy(usage) ? { usage: { ...usage } } : {};
  if (context && context.reasoning_redacted) payload.reasoning_redacted = context.reasoning_redacted;
  return { type: "turn_end", payload };
}

function liveOutputAssistantText(value) {
  const text = liveOutputPyStrip(liveOutputPyStr(liveOutputPyOr(value, "")));
  return text ? [{ type: "assistant_text", payload: { text } }] : [];
}

function liveOutputReasoning(value, context = null) {
  const text = liveOutputPyStrip(liveOutputPyStr(liveOutputPyOr(value, "")));
  if (!text && context) context.reasoning_redacted += 1;
  return text ? [{ type: "reasoning", payload: { text } }] : [];
}

function liveOutputDelta(kind, value) {
  const text = liveOutputPyStr(liveOutputPyOr(value, ""));
  return text ? [{ type: "delta", payload: { kind, text } }] : [];
}

// context_capture._message_text: the completed text blocks of a Claude message.
function liveOutputClaudeMessageText(content) {
  if (typeof content === "string") return liveOutputPyStrip(content);
  if (!Array.isArray(content)) return "";
  const parts = content.filter((part) => liveOutputIsMapping(part) && part.type === "text")
    .map((part) => liveOutputPyStr(liveOutputPyOr(liveOutputGet(part, "text"), "")));
  return liveOutputPyStrip(parts.filter(Boolean).join("\n"));
}

function liveOutputClaudeUsage(event, context) {
  const usage = liveOutputMapping(event.usage);
  if (!liveOutputPyTruthy(usage)) return null;
  const modelUsage = liveOutputMapping(event.modelUsage);
  let window;
  if (context.model && liveOutputHas(modelUsage, context.model)) {
    window = liveOutputMapping(modelUsage[context.model]).contextWindow;
  } else {
    window = Object.values(modelUsage).reduce((best, entry) => Math.max(best, liveOutputCount(liveOutputMapping(entry).contextWindow)), 0);
  }
  const last = context.last_usage;
  let fill = null;
  if (liveOutputPyTruthy(last)) {
    fill = liveOutputCount(last.input_tokens) + liveOutputCount(last.cache_read_input_tokens) + liveOutputCount(last.cache_creation_input_tokens);
  }
  return liveOutputUsage(usage.input_tokens, usage.cache_read_input_tokens, usage.cache_creation_input_tokens, usage.output_tokens, window, usage, fill);
}

function liveOutputClaudeToolUse(block, context) {
  const callId = liveOutputPyStr(liveOutputPyOr(liveOutputGet(block, "id"), ""));
  const name = liveOutputPyStr(liveOutputPyOr(liveOutputGet(block, "name"), ""));
  const request = liveOutputGet(block, "input");
  context.calls.set(callId, { name, input: request });
  if (LIVE_OUTPUT_CLAUDE_COMMAND_TOOLS.has(name)) {
    return [liveOutputCommand(callId, liveOutputPyStr(liveOutputPyOr(liveOutputGet(liveOutputMapping(request), "command"), "")), "running")];
  }
  if (LIVE_OUTPUT_CLAUDE_EDIT_TOOLS.has(name)) return [];
  return [liveOutputToolCall(callId, name, request)];
}

function liveOutputClaudeFileChanges(callId, name, request, output) {
  const path = liveOutputPyStr(liveOutputPyOr(liveOutputGet(request, "file_path"), ""));
  if (name === "Write") {
    const kind = output.startsWith("File created") ? "add" : "update";
    return [liveOutputFileChange(callId, path, [["", liveOutputPyStr(liveOutputPyOr(liveOutputGet(request, "content"), ""))]], kind)];
  }
  const edits = name === "MultiEdit" ? liveOutputGet(request, "edits") : [request];
  const pairs = (Array.isArray(edits) ? edits : []).filter(liveOutputIsMapping).map((edit) => [
    liveOutputPyStr(liveOutputPyOr(liveOutputGet(edit, "old_string"), "")),
    liveOutputPyStr(liveOutputPyOr(liveOutputGet(edit, "new_string"), "")),
  ]);
  return [liveOutputFileChange(callId, path, pairs, "update")];
}

function liveOutputClaudeToolResult(block, context) {
  const callId = liveOutputPyStr(liveOutputPyOr(liveOutputGet(block, "tool_use_id"), ""));
  const call = context.calls.has(callId) ? context.calls.get(callId) : { name: "", input: null };
  context.calls.delete(callId);
  const name = call.name;
  const request = liveOutputMapping(call.input);
  const failed = liveOutputPyTruthy(liveOutputGet(block, "is_error"));
  const output = liveOutputResultText(liveOutputGet(block, "content"));
  if (LIVE_OUTPUT_CLAUDE_COMMAND_TOOLS.has(name)) {
    return [liveOutputCommand(callId, liveOutputPyStr(liveOutputPyOr(liveOutputGet(request, "command"), "")), failed ? "failed" : "completed", null, output)];
  }
  if (LIVE_OUTPUT_CLAUDE_EDIT_TOOLS.has(name) && !failed) return liveOutputClaudeFileChanges(callId, name, request, output);
  const shown = LIVE_OUTPUT_CLAUDE_EDIT_TOOLS.has(name) ? [liveOutputToolCall(callId, name, call.input)] : [];
  return [...shown, liveOutputToolResult(callId, name, liveOutputGet(block, "content"), failed)];
}

function liveOutputClaudeDelta(event) {
  const inner = liveOutputMapping(event.event);
  if (inner.type !== "content_block_delta") return [];
  const delta = liveOutputMapping(inner.delta);
  if (delta.type === "text_delta") return liveOutputDelta("text", liveOutputGet(delta, "text"));
  if (delta.type === "thinking_delta") return liveOutputDelta("reasoning", liveOutputGet(delta, "thinking"));
  return [];
}

function liveOutputClaudeEvents(event, context) {
  const kind = liveOutputPyStr(liveOutputPyOr(liveOutputGet(event, "type"), ""));
  if (kind === "stream_event") return liveOutputClaudeDelta(event);
  if (kind === "result") return [liveOutputTurnEnd(liveOutputClaudeUsage(event, context), context)];
  const message = liveOutputMapping(event.message);
  if (kind === "assistant") {
    context.model = liveOutputPyStr(liveOutputPyOr(liveOutputGet(message, "model"), context.model));
    const usage = liveOutputMapping(message.usage);
    if (liveOutputPyTruthy(usage)) context.last_usage = { ...usage };
  }
  const content = liveOutputGet(message, "content");
  const events = liveOutputAssistantText(liveOutputClaudeMessageText(content));
  for (const block of Array.isArray(content) ? content : []) {
    if (!liveOutputIsMapping(block)) continue;
    if (block.type === "tool_use") events.push(...liveOutputClaudeToolUse(block, context));
    else if (block.type === "tool_result") events.push(...liveOutputClaudeToolResult(block, context));
    else if (block.type === "thinking") events.push(...liveOutputReasoning(liveOutputGet(block, "thinking"), context));
    else if (block.type === "redacted_thinking") events.push(...liveOutputReasoning("", context));
  }
  return events;
}

function liveOutputCodexUsage(value) {
  const usage = liveOutputMapping(value);
  if (!liveOutputPyTruthy(usage)) return null;
  const cached = liveOutputCount(usage.cached_input_tokens);
  return liveOutputUsage(Math.max(liveOutputCount(usage.input_tokens) - cached, 0), cached, usage.cache_write_input_tokens, usage.output_tokens, 0, usage);
}

function liveOutputCodexEvents(event, context) {
  const kind = liveOutputPyStr(liveOutputPyOr(liveOutputGet(event, "type"), ""));
  if (kind === "turn.completed") return [liveOutputTurnEnd(liveOutputCodexUsage(event.usage), context)];
  if (kind === "turn.failed") {
    return [liveOutputTurnError("provider", liveOutputPyStr(liveOutputPyOr(liveOutputGet(liveOutputMapping(event.error), "message"), "turn_failed")))];
  }
  const item = liveOutputMapping(event.item);
  const itemType = liveOutputPyStr(liveOutputPyOr(liveOutputGet(item, "type"), ""));
  if (!kind.startsWith("item.") || !itemType) return [];
  if (itemType === "agent_message" || itemType === "agentMessage") return liveOutputAssistantText(liveOutputGet(item, "text"));
  const callId = liveOutputPyStr(liveOutputPyOr(liveOutputGet(item, "id"), ""));
  const done = kind === "item.completed";
  if (itemType === "command_execution") {
    const code = done ? liveOutputGet(item, "exit_code") : null;
    const status = !done ? "running" : code === 0 || code === false ? "completed" : "failed";
    return [liveOutputCommand(callId, liveOutputPyStr(liveOutputPyOr(liveOutputGet(item, "command"), "")), status, code,
      liveOutputPyStr(liveOutputPyOr(liveOutputGet(item, "aggregated_output"), "")))];
  }
  if (itemType === "file_change") {
    const changes = done ? liveOutputGet(item, "changes") : null;
    return (Array.isArray(changes) ? changes : []).filter(liveOutputIsMapping).map((change) => ({ type: "file_change", payload: {
      call_id: callId, path: liveOutputPyStr(liveOutputPyOr(liveOutputGet(change, "path"), "")),
      kind: liveOutputPyStr(liveOutputPyOr(liveOutputGet(change, "kind"), "update")), diff: "", added: 0, removed: 0,
    } }));
  }
  if (!LIVE_OUTPUT_CODEX_TOOL_ITEMS.has(itemType)) return [];
  if (done) {
    const output = liveOutputPyOr(liveOutputGet(item, "aggregated_output"), liveOutputGet(item, "output"), liveOutputGet(item, "result"));
    return [liveOutputToolResult(callId, itemType, output, liveOutputPyStr(liveOutputPyOr(liveOutputGet(item, "status"), "")) === "failed")];
  }
  return [liveOutputToolCall(callId, itemType, liveOutputGet(item, "arguments"))];
}

function liveOutputOpencodeCallId(part) {
  return liveOutputPyStr(liveOutputPyOr(liveOutputGet(part, "id"), liveOutputGet(part, "callID"), liveOutputGet(part, "partID"), ""));
}

function liveOutputOpencodeCommand(callId, request, state, status) {
  const metadata = liveOutputMapping(state.metadata);
  const nested = liveOutputMapping(metadata.metadata);
  let code = liveOutputHas(nested, "exit") ? liveOutputGet(nested, "exit") : liveOutputGet(metadata, "exit");
  code = liveOutputPyInt(code) ? code : null;
  let shown = "completed";
  if (status === "error") shown = "failed";
  else if (status !== "completed") shown = "running";
  else if (code !== null && code !== 0 && code !== false) shown = "failed";
  return liveOutputCommand(callId, liveOutputPyStr(liveOutputPyOr(liveOutputGet(request, "command"), "")), shown, code,
    liveOutputPyStr(liveOutputPyOr(liveOutputGet(state, "output"), "")));
}

function liveOutputOpencodeTool(part, context) {
  const state = liveOutputMapping(part.state);
  const name = liveOutputPyStr(liveOutputPyOr(liveOutputGet(part, "tool"), liveOutputGet(part, "name"), liveOutputGet(state, "title"), "tool"));
  const callId = liveOutputOpencodeCallId(part);
  const status = liveOutputPyStr(liveOutputPyOr(liveOutputGet(state, "status"), ""));
  const done = status === "completed" || status === "error";
  const failed = status === "error";
  const request = liveOutputMapping(liveOutputPyOr(liveOutputGet(state, "input"), liveOutputGet(part, "input")));
  if (name === "shell" || name === "bash") return [liveOutputOpencodeCommand(callId, request, state, status)];
  if ((name === "write" || name === "edit") && status === "completed") {
    const path = liveOutputPyStr(liveOutputPyOr(liveOutputGet(request, "path"), liveOutputGet(request, "filePath"), ""));
    if (name === "write") {
      const output = liveOutputPyStr(liveOutputPyOr(liveOutputGet(state, "output"), ""));
      return [liveOutputFileChange(callId, path, [["", liveOutputPyStr(liveOutputPyOr(liveOutputGet(request, "content"), ""))]], output.startsWith("Created") ? "add" : "update")];
    }
    const pairs = [[liveOutputPyStr(liveOutputPyOr(liveOutputGet(request, "oldString"), "")), liveOutputPyStr(liveOutputPyOr(liveOutputGet(request, "newString"), ""))]];
    return [liveOutputFileChange(callId, path, pairs, "update")];
  }
  if (!done) {
    context.calls.set(callId, { name });
    return [liveOutputToolCall(callId, name, liveOutputPyOr(request, liveOutputGet(part, "input")))];
  }
  const seenRunning = context.calls.delete(callId);
  const result = liveOutputToolResult(callId, name, liveOutputGet(state, "output"), failed);
  return seenRunning ? [result] : [liveOutputToolCall(callId, name, liveOutputPyOr(request, liveOutputGet(part, "input"))), result];
}

function liveOutputOpencodeKind(event) {
  let kind = liveOutputPyStrip(liveOutputPyStr(liveOutputPyOr(liveOutputGet(event, "type"), ""))).toLowerCase();
  const part = liveOutputMapping(event.part);
  const partType = liveOutputPyStrip(liveOutputPyStr(liveOutputPyOr(liveOutputGet(part, "type"), ""))).toLowerCase().replace(/-/g, "_");
  if (kind.endsWith("part.updated") || kind.endsWith("part_updated")) kind = partType;
  else if (partType && !["text", "reasoning", "tool", "step_finish", "step_start"].includes(kind)) kind = partType;
  return kind;
}

function liveOutputOpencodeEvents(event, context) {
  const kind = liveOutputOpencodeKind(event);
  const part = liveOutputMapping(event.part);
  if (kind === "step_finish" || kind === "stepfinish") {
    const tokens = liveOutputMapping(liveOutputPyOr(liveOutputGet(part, "tokens"), liveOutputGet(event, "tokens")));
    if (liveOutputPyTruthy(tokens)) {
      const steps = context.steps;
      if (!liveOutputPyTruthy(steps)) Object.assign(steps, { count: 0, input: 0, cache_read: 0, cache_write: 0, output: 0 });
      const cache = liveOutputMapping(tokens.cache);
      steps.count += 1;
      steps.input += liveOutputCount(tokens.input);
      steps.cache_read += liveOutputCount(cache.read);
      steps.cache_write += liveOutputCount(cache.write);
      steps.output += liveOutputCount(tokens.output);
      steps.last = { ...tokens };
    }
    return [];
  }
  if (kind === "text") return liveOutputAssistantText(liveOutputPyOr(liveOutputGet(part, "text"), liveOutputGet(event, "text")));
  if (kind === "reasoning" || kind === "thinking") {
    return liveOutputReasoning(liveOutputPyOr(liveOutputGet(part, "text"), liveOutputGet(part, "thinking"), liveOutputGet(event, "text")), context);
  }
  return kind === "tool" ? liveOutputOpencodeTool(part, context) : [];
}

const LIVE_OUTPUT_TRANSLATORS = Object.freeze({
  claude_cli: liveOutputClaudeEvents,
  codex_cli: liveOutputCodexEvents,
  opencode_cli: liveOutputOpencodeEvents,
});

function liveOutputProviderError(event) {
  if (liveOutputPyStr(liveOutputPyOr(liveOutputGet(event, "type"), "")) !== "error" && !liveOutputPyTruthy(liveOutputGet(event, "is_error"))) return null;
  const reported = liveOutputGet(event, "error");
  let detail;
  if (liveOutputIsMapping(reported)) {
    const said = liveOutputPyOr(liveOutputGet(reported, "message"), liveOutputGet(liveOutputMapping(reported.data), "message"));
    detail = [liveOutputGet(reported, "name"), said].filter(liveOutputPyTruthy).map(liveOutputPyStr).join(": ") || "provider_error";
  } else {
    const text = ["error", "result", "message"].map((key) => liveOutputGet(event, key))
      .find((value) => typeof value === "string" && liveOutputPyStrip(value));
    detail = text === undefined ? null : text;
    const subtype = liveOutputGet(event, "subtype");
    if (detail === null && typeof subtype === "string" && liveOutputPyStrip(subtype)) detail = subtype;
    if (detail === null || liveOutputPyStrip(detail).toLowerCase() === "success") detail = "provider_error";
  }
  return liveOutputTurnError("provider", liveOutputPyStr(detail));
}

// manager_loop_backends.translate: one provider event as loop events; an unrecognized one yields nothing.
function liveOutputTranslate(backendId, event, context = null) {
  if (!liveOutputIsMapping(event)) return [];
  const failure = liveOutputProviderError(event);
  if (failure !== null) return [failure];
  const translator = Object.prototype.hasOwnProperty.call(LIVE_OUTPUT_TRANSLATORS, backendId) ? LIVE_OUTPUT_TRANSLATORS[backendId] : null;
  return translator ? translator(event, context || liveOutputTurnContext()) : [];
}

// manager_loop_backends.flush: only OpenCode sums its step usage into the one closing turn_end.
function liveOutputFlush(backendId, context) {
  if (backendId !== "opencode_cli") return [];
  const steps = context.steps;
  if (!liveOutputPyTruthy(steps)) return [liveOutputTurnEnd(null, context)];
  const usage = liveOutputUsage(steps.input, steps.cache_read, steps.cache_write, steps.output, 0,
    { steps: steps.count, last: liveOutputPyOr(steps.last, {}) });
  return [liveOutputTurnEnd(usage, context)];
}

// A runner/adapter name from the live-output payload, as one translator id ("" when none applies).
function liveOutputBackendId(value) {
  const name = String(value || "").toLowerCase();
  if (name.includes("claude")) return "claude_cli";
  if (name.includes("codex")) return "codex_cli";
  if (name.includes("opencode") || name.includes("kilo")) return "opencode_cli";
  return "";
}

// The provider a line comes from, by its shape alone; "" for a shape no translator owns.
function liveOutputDetectBackend(event) {
  const type = typeof event.type === "string" ? event.type : "";
  if (type.startsWith("aiworkhub_")) return "";
  if (type === "stream_event" || ((type === "assistant" || type === "user") && liveOutputIsMapping(event.message))) return "claude_cli";
  if ((type === "system" || type === "result") && typeof event.subtype === "string" && typeof event.session_id === "string") return "claude_cli";
  if (["thread.started", "turn.started", "turn.completed", "turn.failed"].includes(type)
    || (type.startsWith("item.") && liveOutputIsMapping(event.item))) return "codex_cli";
  if (type && liveOutputIsMapping(event.part) && typeof event.part.type === "string") return "opencode_cli";
  return "";
}

// A line its translator handled even when it yielded nothing yet: a Claude message whose Edit/Write
// call is shown once its result arrives or whose thinking was redacted, an OpenCode step or empty
// reasoning part (counted into the turn_end), a Codex item still in progress. Such a line is part of
// the chat, never a separate timeline row.
function liveOutputTranslatorOwns(backendId, event) {
  if (!liveOutputIsMapping(event)) return false;
  const type = typeof event.type === "string" ? event.type : "";
  if (backendId === "claude_cli") {
    return type === "result" || ((type === "assistant" || type === "user") && liveOutputIsMapping(event.message));
  }
  if (backendId === "codex_cli") {
    const itemType = liveOutputMapping(event.item).type;
    return type.startsWith("item.") && (itemType === "agent_message" || itemType === "agentMessage"
      || LIVE_OUTPUT_CODEX_TOOL_ITEMS.has(itemType));
  }
  if (backendId === "opencode_cli") {
    return ["step_finish", "stepfinish", "step_start", "text", "reasoning", "thinking", "tool"].includes(liveOutputOpencodeKind(event));
  }
  return false;
}

function liveOutputTurnHasText(events) {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    if (events[index].type === "turn_end") return false;
    if (events[index].type === "assistant_text") return true;
  }
  return false;
}

// Every live-output line as chat events, oldest first. `backendId` is the payload's runner/adapter
// (may be ""); a line without one is attributed by its shape, then to the stream's last provider.
// A JSON object no translator turns into an event becomes one timeline_row built exactly as the old
// timeline built it (timelineEventFromObject), a string (a non-JSON line) one row of the raw-text
// branch; nothing is dropped and a valid object is never "Unparsed provider line". Deltas stay in the
// list; the caller decides how to show them.
function liveOutputChatEvents(backendId, lineObjects, options = {}) {
  const context = options.context || liveOutputTurnContext();
  const explicit = liveOutputBackendId(backendId);
  const items = Array.isArray(lineObjects) ? lineObjects : [];
  const events = [];
  const seenRows = new Set();
  let rows = [];
  const flushRows = () => {
    for (const row of rows.length ? coalesceTimelineEvents(rows) : []) events.push({ type: "timeline_row", payload: row });
    rows = [];
  };
  let sticky = explicit;
  items.forEach((line, index) => {
    if (typeof line === "string") {
      rows.push(timelineEventFromTextLine(line, Boolean(options.openTail) && index === items.length - 1));
      return;
    }
    if (!line || typeof line !== "object") return;
    const detected = explicit || (Array.isArray(line) ? "" : liveOutputDetectBackend(line));
    if (detected) sticky = detected;
    const internal = typeof line.type === "string" && line.type.startsWith("aiworkhub_");
    const backend = internal ? "" : detected || sticky;
    const translated = backend ? liveOutputTranslate(backend, line, context) : [];
    const consumed = translated.length > 0 || (Boolean(backend) && liveOutputTranslatorOwns(backend, line));
    if (!consumed) {
      const key = JSON.stringify(line);
      if (seenRows.has(key)) return;
      seenRows.add(key);
      const row = timelineEventFromObject(line, line);
      if (row) rows.push(row);
      return;
    }
    flushRows();
    context.backend = backend;
    // A Claude result holds the turn's final answer; it is shown only when no assistant text did.
    const answer = backend === "claude_cli" && line.type === "result" && typeof line.result === "string" ? liveOutputPyStrip(line.result) : "";
    if (answer && !liveOutputTurnHasText(events)) events.push({ type: "assistant_text", payload: { text: answer } });
    events.push(...translated);
    if (translated.some((event) => event.type === "turn_end")) {
      Object.assign(context, { calls: new Map(), last_usage: {}, steps: {}, reasoning_redacted: 0 });
    }
  });
  flushRows();
  const flushBackend = explicit || context.backend;
  // A live tail is never closed here: a closing turn_end after the last step would mark every
  // still-running call "interrupted". The step usage closes the turn once the run has finished.
  if (flushBackend && options.flush !== false) events.push(...liveOutputFlush(flushBackend, context));
  return events;
}

// ── Worker chat render: the manager console's row builders with the worker's own state ──
const WORKER_CHAT_EVENT_LIMIT = 200;

function workerChatViewState() {
  return { entries: [], stale: true };
}

// Deltas are display-only: a completed message that carries the streamed text supersedes it; any
// other event first settles the streamed text as its own row, and what is still streaming at the end
// shows as the live partial rows.
function workerChatDisplayEvents(events) {
  const shown = [];
  let text = "";
  let reasoning = "";
  for (const event of events) {
    if (!event) continue;
    const payload = managerConsolePayload(event);
    if (event.type === "delta") {
      if (payload.kind === "reasoning") reasoning += String(payload.text || "");
      else text += String(payload.text || "");
      continue;
    }
    if (event.type === "assistant_text" && String(payload.text || "").includes(text.trim())) text = "";
    if (event.type === "reasoning" && String(payload.text || "").includes(reasoning.trim())) reasoning = "";
    if (reasoning.trim()) shown.push({ type: "reasoning", payload: { text: reasoning.trim() } });
    if (text.trim()) shown.push({ type: "assistant_text", payload: { text: text.trim() } });
    text = "";
    reasoning = "";
    shown.push(event);
  }
  return { events: shown, partial: { text: text.trim(), reasoning: reasoning.trim() } };
}

// One list under one parent. A row with the same key and signature keeps its node (so its open
// <details>, selection and Copied label survive); a changed row is rebuilt with its open states.
// Rows only appended are appended; any other change re-places the list's nodes in order.
function workerChatReconcile(parent, previous, descriptors, pool) {
  // The nodes now under `parent`, read before a changed row swaps its entry's node.
  const before = previous.map((entry) => entry.node);
  const entries = descriptors.map((descriptor) => {
    let entry = managerConsoleClaim(pool, descriptor);
    if (!entry) {
      const node = descriptor.build();
      entry = { key: descriptor.key, signature: descriptor.signature, node, live: [], parent: null };
      if (descriptor.members) Object.assign(entry, { summary: node.children[0], body: node.children[1], members: [] });
      pool.writes += 1;
    }
    if (descriptor.members) {
      entry.key = descriptor.key;
      if (entry.signature !== descriptor.signature) {
        managerConsoleSetText(entry.summary, descriptor.summaryText);
        if (entry.node.className !== descriptor.className) entry.node.className = descriptor.className;
        entry.signature = descriptor.signature;
        pool.writes += 1;
      }
      entry.members = workerChatReconcile(entry.body, entry.members, descriptor.members, pool);
    } else if (entry.signature !== descriptor.signature) {
      const node = descriptor.build();
      const opened = managerConsoleDetailsIn(entry.node);
      managerConsoleDetailsIn(node).forEach((details, index) => {
        if (index < opened.length) details.open = opened[index].open;
      });
      entry.node = node;
      entry.signature = descriptor.signature;
      pool.writes += 1;
    }
    return entry;
  });
  const after = entries.map((entry) => entry.node);
  if (before.length <= after.length && before.every((node, index) => node === after[index])) {
    for (let index = before.length; index < after.length; index += 1) parent.appendChild(after[index]);
  } else {
    parent.replaceChildren(...after);
    pool.writes += 1;
  }
  for (const entry of entries) entry.parent = parent;
  return entries;
}

// Renders translated worker events (liveOutputChatEvents) into `container` as a chat, oldest at the
// top. Reads and writes only `options.state` (workerChatViewState()), never the manager console's
// terminal seq, index, signature, strip or announcer. Returns false when there is nothing to show.
function renderWorkerChatEvents(container, events, options = {}) {
  const view = options.state || workerChatViewState();
  const display = workerChatDisplayEvents(Array.isArray(events) ? events : []);
  let turn = 1;
  const list = display.events.map((event, index) => {
    const item = { type: event.type, payload: event.payload, seq: index + 1, turn };
    if (event.type === "turn_end") turn += 1;
    return item;
  });
  const chat = {
    terminalSeq: options.finished ? Number.POSITIVE_INFINITY : managerConsoleHighestTerminalSeq(list),
    assistantLabel: String(options.label || "Worker"),
    index: managerConsoleBuildIndex(list),
  };
  const merged = managerConsoleMergeTools(managerConsoleMergeCommands(list));
  const limit = options.limit || WORKER_CHAT_EVENT_LIMIT;
  const visible = merged.length > limit ? merged.slice(merged.length - limit) : merged;
  const descriptors = managerConsoleGroupRuns(managerConsoleEventRows(merged, visible, chat), chat);
  descriptors.push(...managerConsoleStreamingRows(display.partial));
  if (!descriptors.length) {
    view.entries = [];
    view.stale = true;
    return false;
  }
  const atBottom = numberValue(container.scrollHeight) - numberValue(container.scrollTop) - numberValue(container.clientHeight) < 24;
  if (view.stale) {
    container.replaceChildren();
    view.entries = [];
    view.stale = false;
  }
  const pool = managerConsoleRowPool(view.entries);
  try {
    view.entries = workerChatReconcile(container, view.entries, descriptors, pool);
  } catch (error) {
    view.entries = [];
    view.stale = true;
    throw error;
  }
  if (pool.writes > 0 && atBottom) container.scrollTop = container.scrollHeight;
  return true;
}
