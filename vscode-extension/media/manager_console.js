"use strict";

// Manager console block renderers (spec 2026-09-26 §3). Loaded before app.js;
// every app.js global these use (createElement, state, elements) is read at
// call time, never at load time. Every event payload is untrusted model/tool
// output: strings go through createElement's textContent or
// document.createTextNode, never innerHTML.

function managerChatImageCount(count) {
  return `${count} image${count === 1 ? "" : "s"} attached`;
}

function managerChatEventNode(event) {
  const type = String((event && event.type) || "");
  const payload = event && event.payload && typeof event.payload === "object" ? event.payload : {};
  if (type === "command") return managerConsoleCommandNode(payload);
  if (type === "file_change") return managerConsoleFileChangeNode(payload);
  if (type === "assistant_text" || type === "user_message") {
    const bubble = createElement("div", `manager-chat-bubble role-${type === "user_message" ? "user" : "assistant"}`);
    bubble.appendChild(createElement("span", "manager-chat-bubble-label sr-only", type === "user_message" ? "You" : "Manager"));
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
    const hasResult = "output" in payload || "is_error" in payload;
    const status = !hasResult ? "running" : payload.is_error ? "failed" : "done";
    const row = createElement("details", "manager-chat-tool-row is-" + status);
    row.open = status !== "done";
    row.appendChild(createElement("summary", "", [name, hint, status].filter(Boolean).join(" · ")));
    const body = createElement("div", "manager-chat-tool-row-body");
    if ("input" in payload) {
      body.appendChild(createElement("div", "manager-chat-tool-section", "input"));
      appendManagerChatToolFields(body, payload.input, 0);
    }
    if (hasResult) {
      body.appendChild(createElement("div", "manager-chat-tool-section", "output"));
      appendManagerChatToolFields(body, payload.output, 0);
    }
    row.appendChild(body);
    const task = hasResult ? managerConsoleTaskOf(payload) : null;
    return task ? managerConsoleTaskNode(task, row) : managerConsoleBlock("tool_call", row);
  }
  if (type === "tool_result") {
    const name = String(payload.name || payload.tool || "tool");
    const hint = managerChatToolHint(payload);
    const row = createElement("details", "manager-chat-tool-row is-result");
    row.open = false;
    const summary = name + " result";
    row.appendChild(createElement("summary", "", hint ? summary + " · " + hint : summary));
    const body = createElement("div", "manager-chat-tool-row-body");
    const rest = Object.assign({}, payload);
    delete rest.name;
    delete rest.tool;
    appendManagerChatToolFields(body, rest, 0);
    row.appendChild(body);
    const task = managerConsoleTaskOf(payload);
    return task ? managerConsoleTaskNode(task, row) : managerConsoleBlock(type, row);
  }
  if (type === "callback") {
    return managerConsoleBlock(type, createElement("div", "manager-chat-marker", `Automatic wake-up${payload.text ? `: ${limitText(payload.text, 120)}` : ""}`));
  }
  if (type === "error") {
    return managerConsoleBlock(type, createElement("div", "manager-chat-error", String(payload.error || payload.message || "Manager error")));
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
    row.open = true;
    const label = managerChatThoughtSummary(event);
    const summary = createElement("summary", "", label);
    if (managerConsoleActiveIndex && label.startsWith("Thinking ·")) managerConsoleLiveSummaries.push({ summary, event });
    row.appendChild(summary);
    const body = createElement("div", "manager-chat-tool-row-body");
    body.appendChild(document.createTextNode(String(payload.text || "")));
    row.appendChild(body);
    return managerConsoleBlock(type, row);
  }
  if (type === "turn_end") {
    return managerConsoleBlock("turn_end", createElement("div", "mc-footer", managerConsoleFooterText(event)));
  }
  return null;
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
  const end = managerConsoleCurrentIndex().reasoningEnd.get(event) || 0;
  if (start && end >= start && end) return "Thought for " + managerChatFormatDuration(end - start);
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

function appendManagerChatToolFields(parent, value, depth) {
  if (!parent || depth > 4) return;
  if (value == null || typeof value !== "object") {
    const text = createElement("div", "manager-chat-tool-value");
    text.appendChild(document.createTextNode(value == null ? "" : String(value)));
    parent.appendChild(text);
    return;
  }
  const entries = Array.isArray(value) ? value.map((item, index) => [String(index), item]) : Object.keys(value).map((key) => [key, value[key]]);
  for (const pair of entries) {
    const field = createElement("div", "manager-chat-tool-field");
    field.appendChild(createElement("span", "manager-chat-tool-label", pair[0]));
    if (pair[1] && typeof pair[1] === "object") appendManagerChatToolFields(field, pair[1], depth + 1);
    else {
      const text = createElement("div", "manager-chat-tool-value");
      text.appendChild(document.createTextNode(pair[1] == null ? "" : String(pair[1])));
      field.appendChild(text);
    }
    parent.appendChild(field);
  }
}

function managerChatLiveThinkingNode() {
  const elapsed = state.managerChatThinkingSince ? Date.now() - state.managerChatThinkingSince : 0;
  const row = createElement("div", "manager-chat-thinking is-live");
  const dots = createElement("span", "manager-chat-thinking-dots");
  dots.setAttribute("aria-hidden", "true");
  dots.appendChild(createElement("span", ""));
  dots.appendChild(createElement("span", ""));
  dots.appendChild(createElement("span", ""));
  row.appendChild(dots);
  const label = createElement("span", "manager-chat-thinking-label", "Thinking · " + managerChatFormatDuration(elapsed));
  label.id = "manager-chat-thinking-elapsed";
  managerConsoleLiveLabel = label;
  row.appendChild(label);
  return row;
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
    if (item.type === "turn_end" || item.type === "turn_stopped" || item.type === "error" || item.type === "session_close") finished = true;
  }
  return String(state.managerChatSession || "") + ":" + (finished ? turn + 1 : turn);
}

// Glyph per block kind; the gutter is fixed-width so blocks never shift.
const MANAGER_CONSOLE_GLYPHS = Object.freeze({
  user_message: "›", assistant_text: "●", reasoning: "∴", command: "$", file_change: "±",
  tool_call: "⚙", tool_result: "⚙", task: "▣", callback: "↯", goal: "◎", error: "!", turn_stopped: "■",
});
const MANAGER_CONSOLE_COMMAND_LINES = 20;
const MANAGER_CONSOLE_DIFF_LINES = 40;

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

function managerConsoleCommandChip(payload) {
  if (payload.status === "running") return createElement("span", "mc-chip is-running", "running");
  const code = Number.isInteger(payload.exit_code) ? payload.exit_code : null;
  const failed = payload.status === "failed" || (code !== null && code !== 0);
  const label = code !== null ? "exit " + code : failed ? "failed" : "done";
  return createElement("span", failed ? "mc-chip is-failed" : "mc-chip is-ok", label);
}

function managerConsoleCommandNode(payload) {
  const body = createElement("div", "mc-body");
  const head = createElement("div", "mc-head");
  head.appendChild(createElement("code", "mc-mono", String(payload.command || "")));
  head.appendChild(managerConsoleCommandChip(payload));
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

function managerConsoleTaskNode(task, detail) {
  const body = createElement("div", "mc-body");
  const open = createElement("button", "mc-task-link mc-mono", task.id);
  open.type = "button";
  open.addEventListener("click", () => requestTaskDetail(task.id));
  body.appendChild(open);
  if (task.status) body.appendChild(createElement("span", "status-badge " + task.status, task.status));
  if (task.title) body.appendChild(createElement("span", "mc-task-title", task.title));
  if (detail) body.appendChild(detail);
  const block = managerConsoleBlock("task", body);
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

// One pass over the raw events: per-turn first event and tool ids, and the end time of each reasoning
// event (the next non-reasoning event). A render builds it once so no row rescans the whole log.
function managerConsoleBuildIndex(events) {
  const turns = new Map();
  const reasoningEnd = new Map();
  let nextEnd = 0;
  for (let cursor = events.length - 1; cursor >= 0; cursor -= 1) {
    const item = events[cursor];
    if (!item) continue;
    if (item.type === "reasoning") reasoningEnd.set(item, nextEnd);
    else nextEnd = managerChatEventTime(item);
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
  return { turns, reasoningEnd };
}

function managerConsoleCurrentIndex() {
  return managerConsoleActiveIndex || managerConsoleBuildIndex(Array.isArray(state.managerChatEvents) ? state.managerChatEvents : []);
}

function managerConsoleFooterText(event) {
  const turn = event && event.turn;
  const info = Number.isInteger(turn) ? managerConsoleCurrentIndex().turns.get(turn) : null;
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
let managerConsoleLiveLabel = null;
let managerConsoleLiveSummaries = [];

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

// Only the live Thinking labels change between identical renders; touch just those nodes.
function managerConsoleTickThinking() {
  if (managerConsoleLiveLabel && state.managerChatThinkingSince) {
    managerConsoleLiveLabel.textContent = "Thinking · " + managerChatFormatDuration(Date.now() - state.managerChatThinkingSince);
  }
  for (const entry of managerConsoleLiveSummaries) {
    const start = managerChatEventTime(entry.event);
    if (start) entry.summary.textContent = "Thinking · " + managerChatFormatDuration(Date.now() - start);
  }
}

function managerConsoleExtraRows() {
  const partialRows = managerConsolePartialNodes(state.managerChatPartial);
  if (partialRows.length > 0) return partialRows;
  if (state.managerChatRunning) {
    // The timer belongs to one session and turn: a start left over from an
    // earlier turn or session restarts instead of counting on (the 64m timer).
    const key = managerChatThinkingKey();
    if (!state.managerChatThinkingSince || state.managerChatThinkingKey !== key) {
      state.managerChatThinkingSince = Date.now();
      state.managerChatThinkingKey = key;
    }
    return [managerChatLiveThinkingNode()];
  }
  if (state.managerChatLastThoughtMs && !state.managerChatEvents.some((item) => item && item.type === "reasoning")) {
    return [createElement("div", "manager-chat-thinking", "Thought for " + managerChatFormatDuration(state.managerChatLastThoughtMs))];
  }
  return [];
}

function renderManagerChatEvents() {
  if (!elements.managerChatTranscript) return;
  managerConsoleApplyHairline();
  const events = Array.isArray(state.managerChatEvents) ? state.managerChatEvents : [];
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
  managerConsoleLiveLabel = null;
  managerConsoleLiveSummaries = [];
  const extras = managerConsoleExtraRows();
  const merged = managerConsoleMergeTools(managerConsoleMergeCommands(events));
  const eventLimit = Math.max(0, limit - extras.length);
  const hasEarlier = merged.length > eventLimit;
  const visible = hasEarlier ? merged.slice(merged.length - eventLimit) : merged;
  const rows = [];
  let lastTurnEndTurn = null;
  const pending = [];
  const flushPendingTurnEnd = () => {
    if (pending.length > 0) {
      const node = managerChatEventNode(pending[pending.length - 1]);
      if (node) rows.push(node);
      pending.length = 0;
    }
    lastTurnEndTurn = null;
  };
  managerConsoleActiveIndex = managerConsoleBuildIndex(events);
  try {
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
      const node = managerChatEventNode(event);
      if (node) rows.push(node);
    }
    flushPendingTurnEnd();
  } finally {
    managerConsoleActiveIndex = null;
  }
  rows.push(...extras);
  if (hasEarlier) {
    const earlier = createElement("button", "mc-show-all", "load earlier");
    earlier.type = "button";
    earlier.addEventListener("click", () => {
      state.managerChatRenderLimit = limit + MANAGER_CONSOLE_BLOCK_LIMIT;
      managerConsoleScheduleRender();
    });
    rows.unshift(earlier);
  }
  managerConsoleRenderedLimit = limit;
  managerConsoleLastSignature = managerConsoleRenderSignature(events, limit);
  if (rows.length === 0) {
    elements.managerChatTranscript.replaceChildren(
      createElement("div", "panel-list-empty compact", state.managerChatSession ? "No events yet" : "No open session"),
    );
    if (elements.managerChatLatest) elements.managerChatLatest.hidden = true;
    return;
  }
  const fragment = document.createDocumentFragment();
  for (const row of rows) fragment.appendChild(row);
  elements.managerChatTranscript.replaceChildren(fragment);
  if (atBottom) box.scrollTop = box.scrollHeight;
  else box.scrollTop = prepending ? scrollTop + (box.scrollHeight - scrollHeight) : scrollTop;
  if (elements.managerChatLatest) elements.managerChatLatest.hidden = atBottom;
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
    else parent.appendChild(document.createTextNode(`${match[5]} (${match[6]})`));
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

function managerConsoleMarkdown(text) {
  const fragment = document.createDocumentFragment();
  const lines = String(text || "").split("\n");
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    if (line.startsWith("```")) {
      const code = [];
      index += 1;
      while (index < lines.length && !lines[index].startsWith("```")) code.push(lines[index++]);
      index += 1;
      fragment.appendChild(managerConsoleCodeNode(code));
      continue;
    }
    const bullet = /^\s*([-*]|\d+\.)\s+/;
    if (bullet.test(line)) {
      const ordered = /^\s*\d+\./.test(line);
      const list = createElement(ordered ? "ol" : "ul", "mc-list");
      while (index < lines.length && bullet.test(lines[index])) {
        const item = createElement("li", "");
        managerConsoleInline(item, lines[index++].replace(bullet, ""));
        list.appendChild(item);
      }
      fragment.appendChild(list);
      continue;
    }
    if (!line.trim()) { index += 1; continue; }
    const paragraph = createElement("p", "mc-p");
    const words = [];
    while (index < lines.length && lines[index].trim() && !lines[index].startsWith("```") && !bullet.test(lines[index])) words.push(lines[index++]);
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

function managerConsolePartialNodes(partial) {
  if (!managerConsolePartialIsCurrent(partial)) return [];
  const nodes = [];
  if (partial.reasoning) {
    const details = createElement("details", "mc-thinking");
    details.open = true;
    details.appendChild(createElement("summary", "", "Thinking"));
    details.appendChild(createElement("div", "mc-thinking-text", partial.reasoning));
    nodes.push(managerConsoleBlock("reasoning", details));
  }
  if (partial.text) {
    const body = createElement("div", "mc-body mc-streaming");
    body.appendChild(managerConsoleMarkdown(partial.text));
    const caret = createElement("span", "mc-caret");
    caret.setAttribute("aria-hidden", "true");
    body.appendChild(caret);
    nodes.push(managerConsoleBlock("assistant_text", body));
  }
  return nodes;
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
  if (elements.managerChatAnnouncer) elements.managerChatAnnouncer.textContent = "";
  if (elements.managerChatLatest) elements.managerChatLatest.hidden = true;
  if (elements.managerChatTranscript) elements.managerChatTranscript.scrollTop = elements.managerChatTranscript.scrollHeight;
}
