"use strict";

// Manager console block renderers (spec 2026-09-26 §3). Loaded before app.js;
// every app.js global these use (createElement, state, elements) is read at
// call time, never at load time. Every event payload is untrusted model/tool
// output: strings go through createElement's textContent or
// document.createTextNode, never innerHTML.

function managerChatEventNode(event) {
  const type = String((event && event.type) || "");
  const payload = event && event.payload && typeof event.payload === "object" ? event.payload : {};
  if (type === "command") return managerConsoleCommandNode(payload);
  if (type === "file_change") return managerConsoleFileChangeNode(payload);
  if (type === "assistant_text" || type === "user_message") {
    const bubble = createElement("div", `manager-chat-bubble role-${type === "user_message" ? "user" : "assistant"}`);
    bubble.appendChild(createElement("span", "manager-chat-bubble-label sr-only", type === "user_message" ? "You" : "Manager"));
    bubble.appendChild(type === "assistant_text" ? managerConsoleMarkdown(payload.text) : document.createTextNode(String(payload.text || "")));
    return managerConsoleBlock(type, bubble);
  }
  if (type === "tool_call" || type === "tool_result") {
    const name = String(payload.name || payload.tool || "tool");
    const hint = managerChatToolHint(payload);
    const row = createElement("details", "manager-chat-tool-row " + (type === "tool_result" ? "is-result" : "is-call"));
    row.open = type === "tool_call";
    const summary = type === "tool_result" ? name + " result" : name;
    row.appendChild(createElement("summary", "", hint ? summary + " · " + hint : summary));
    const body = createElement("div", "manager-chat-tool-row-body");
    const rest = Object.assign({}, payload);
    delete rest.name;
    delete rest.tool;
    appendManagerChatToolFields(body, rest, 0);
    row.appendChild(body);
    const task = type === "tool_result" ? managerConsoleTaskOf(payload) : null;
    return task ? managerConsoleTaskNode(task, row) : managerConsoleBlock(type, row);
  }
  if (type === "callback") {
    return managerConsoleBlock(type, createElement("div", "manager-chat-marker", `Automatic wake-up${payload.text ? `: ${limitText(payload.text, 120)}` : ""}`));
  }
  if (type === "error") {
    return managerConsoleBlock(type, createElement("div", "manager-chat-error", String(payload.error || payload.message || "Manager error")));
  }
  if (type === "session_start") {
    return managerConsoleBlock(type, createElement("div", "manager-chat-marker", "Session started"));
  }
  if (type === "handoff_request") {
    return managerConsoleBlock(type, createElement("div", "manager-chat-marker", `Handoff requested${payload.reason ? `: ${limitText(payload.reason, 120)}` : ""}`));
  }
  if (type === "session_close") {
    return managerConsoleBlock(type, createElement("div", "manager-chat-marker", `Session closed${payload.reason ? `: ${limitText(payload.reason, 120)}` : ""}`));
  }
  if (type === "reasoning") {
    const row = createElement("details", "manager-chat-thinking-block");
    row.open = true;
    row.appendChild(createElement("summary", "", managerChatThoughtSummary(event)));
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

function managerChatFormatDuration(ms) {
  const seconds = Math.max(0, Math.round(Number(ms) / 1000));
  if (seconds < 60) return seconds + "s";
  return Math.floor(seconds / 60) + "m " + (seconds % 60) + "s";
}

function managerChatEventTime(event) {
  const parsed = Date.parse(event && event.at || "");
  return Number.isFinite(parsed) ? parsed : 0;
}

function managerChatThoughtSummary(event) {
  const events = Array.isArray(state.managerChatEvents) ? state.managerChatEvents : [];
  const start = managerChatEventTime(event);
  let end = 0;
  const index = events.indexOf(event);
  if (index >= 0) {
    for (let cursor = index + 1; cursor < events.length; cursor += 1) {
      if (events[cursor] && events[cursor].type !== "reasoning") {
        end = managerChatEventTime(events[cursor]);
        break;
      }
    }
  }
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
    if (item.type === "turn_end" || item.type === "error" || item.type === "session_close") finished = true;
  }
  return String(state.managerChatSession || "") + ":" + (finished ? turn + 1 : turn);
}

// Glyph per block kind; the gutter is fixed-width so blocks never shift.
const MANAGER_CONSOLE_GLYPHS = Object.freeze({
  user_message: "›", assistant_text: "●", reasoning: "∴", command: "$", file_change: "±",
  tool_call: "⚙", tool_result: "⚙", task: "▣", callback: "↯", goal: "◎", error: "!",
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
  return managerConsoleBlock("task", body);
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

function managerConsoleTurnTools(turn) {
  if (!Number.isInteger(turn)) return 0;
  const ids = new Set();
  for (const item of state.managerChatEvents || []) {
    if (!item || item.turn !== turn || !["tool_call", "command", "file_change"].includes(item.type)) continue;
    ids.add((item.payload && item.payload.call_id) || "seq:" + item.seq);
  }
  return ids.size;
}

function managerConsoleFooterText(event) {
  const turn = event && event.turn;
  const tools = managerConsoleTurnTools(turn);
  const parts = [Number.isInteger(turn) ? `turn ${turn}` : "turn", `${tools} tool${tools === 1 ? "" : "s"}`];
  const usage = managerConsoleUsage(event && event.payload && event.payload.usage);
  if (usage) parts.push(`in ${managerConsoleCompact(usage.input)}`, `cache ${managerConsoleCompact(usage.cache)}`, `out ${managerConsoleCompact(usage.output)}`);
  const first = Number.isInteger(turn) ? (state.managerChatEvents || []).find((item) => item && item.turn === turn) : null;
  // managerChatEventTime is 0 without an `at`; no duration is better than a made-up one.
  const start = first ? managerChatEventTime(first) : 0;
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

function renderManagerChatEvents() {
  if (!elements.managerChatTranscript) return;
  managerConsoleApplyHairline();
  const box = elements.managerChatTranscript;
  const scrollTop = box.scrollTop;
  const atBottom = box.scrollHeight - scrollTop - (box.clientHeight || 0) < 24;
  let rows = [];
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
  for (const event of managerConsoleMergeCommands(state.managerChatEvents)) {
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
  const partialRows = managerConsolePartialNodes(state.managerChatPartial);
  if (partialRows.length > 0) {
    rows.push(...partialRows);
  } else if (state.managerChatRunning) {
    // The timer belongs to one session and turn: a start left over from an
    // earlier turn or session restarts instead of counting on (the 64m timer).
    const key = managerChatThinkingKey();
    if (!state.managerChatThinkingSince || state.managerChatThinkingKey !== key) {
      state.managerChatThinkingSince = Date.now();
      state.managerChatThinkingKey = key;
    }
    rows.push(managerChatLiveThinkingNode());
  } else if (state.managerChatLastThoughtMs && !state.managerChatEvents.some((item) => item && item.type === "reasoning")) {
    rows.push(createElement("div", "manager-chat-thinking", "Thought for " + managerChatFormatDuration(state.managerChatLastThoughtMs)));
  }
  const limit = state.managerChatRenderLimit || MANAGER_CONSOLE_BLOCK_LIMIT;
  if (rows.length > limit) {
    rows = rows.slice(-limit);
    const earlier = createElement("button", "mc-show-all", "load earlier");
    earlier.type = "button";
    earlier.addEventListener("click", () => {
      state.managerChatRenderLimit = limit + MANAGER_CONSOLE_BLOCK_LIMIT;
      managerConsoleScheduleRender();
    });
    rows.unshift(earlier);
  }
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
  box.scrollTop = atBottom ? box.scrollHeight : scrollTop;
  if (elements.managerChatLatest) elements.managerChatLatest.hidden = atBottom;
  managerConsoleAnnounce(state.managerChatEvents);
}

const MANAGER_CONSOLE_BLOCK_LIMIT = 400;
const MANAGER_CONSOLE_INLINE = /(`[^`]+`|\*\*[^*]+\*\*|\*[^*]+\*|_[^_]+_|\[[^\]]+\]\([^)\s]+\))/;

function managerConsoleInline(parent, text) {
  for (const part of String(text).split(MANAGER_CONSOLE_INLINE)) {
    if (!part) continue;
    if (part.startsWith("`") && part.endsWith("`") && part.length > 1) parent.appendChild(createElement("code", "mc-mono", part.slice(1, -1)));
    else if (part.startsWith("**") && part.endsWith("**") && part.length > 4) parent.appendChild(createElement("strong", "", part.slice(2, -2)));
    else if (/^(\*[^*]+\*|_[^_]+_)$/.test(part)) parent.appendChild(createElement("em", "", part.slice(1, -1)));
    else if (/^\[[^\]]+\]\([^)\s]+\)$/.test(part)) {
      const cut = part.indexOf("](");
      parent.appendChild(document.createTextNode(`${part.slice(1, cut)} (${part.slice(cut + 2, -1)})`));
    } else parent.appendChild(document.createTextNode(part));
  }
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
      fragment.appendChild(createElement("pre", "mc-code", code.join("\n")));
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
  // NF-2026-01230: only turn_end ends a turn; a mid-turn assistant_text already settled its streamed text server side.
  return (state.managerChatEvents || []).some((item) => item && item.turn === turn && item.type === "turn_end");
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
  state.managerChatRenderLimit = MANAGER_CONSOLE_BLOCK_LIMIT;
  state.managerChatAnnouncedSeq = 0;
  state.managerChatThinkingSince = 0;
  state.managerChatLastThoughtMs = 0;
  if (elements.managerChatAnnouncer) elements.managerChatAnnouncer.textContent = "";
  if (elements.managerChatLatest) elements.managerChatLatest.hidden = true;
  if (elements.managerChatTranscript) elements.managerChatTranscript.scrollTop = elements.managerChatTranscript.scrollHeight;
}
