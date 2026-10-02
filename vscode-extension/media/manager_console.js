"use strict";

// Manager console block renderers (spec 2026-09-26 §3). Loaded before app.js;
// every app.js global these use (createElement, state, elements) is read at
// call time, never at load time. Every event payload is untrusted model/tool
// output: strings go through createElement's textContent or
// document.createTextNode, never innerHTML.

function managerChatEventNode(event) {
  const type = String((event && event.type) || "");
  const payload = event && event.payload && typeof event.payload === "object" ? event.payload : {};
  if (type === "assistant_text" || type === "user_message") {
    const bubble = createElement("div", `manager-chat-bubble role-${type === "user_message" ? "user" : "assistant"}`);
    bubble.appendChild(createElement("span", "manager-chat-bubble-label", type === "user_message" ? "You" : "Manager"));
    bubble.appendChild(document.createTextNode(String(payload.text || "")));
    return bubble;
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
    return row;
  }
  if (type === "callback") {
    return createElement("div", "manager-chat-marker", `Automatic wake-up${payload.text ? `: ${limitText(payload.text, 120)}` : ""}`);
  }
  if (type === "error") {
    return createElement("div", "manager-chat-error", String(payload.error || payload.message || "Manager error"));
  }
  if (type === "session_start") {
    return createElement("div", "manager-chat-marker", "Session started");
  }
  if (type === "handoff_request") {
    return createElement("div", "manager-chat-marker", `Handoff requested${payload.reason ? `: ${limitText(payload.reason, 120)}` : ""}`);
  }
  if (type === "session_close") {
    return createElement("div", "manager-chat-marker", `Session closed${payload.reason ? `: ${limitText(payload.reason, 120)}` : ""}`);
  }
  if (type === "reasoning") {
    const row = createElement("details", "manager-chat-thinking-block");
    row.open = true;
    row.appendChild(createElement("summary", "", managerChatThoughtSummary(event)));
    const body = createElement("div", "manager-chat-tool-row-body");
    body.appendChild(document.createTextNode(String(payload.text || "")));
    row.appendChild(body);
    return row;
  }
  if (type === "turn_end") {
    return managerChatTurnEndNode(payload, event);
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

function managerChatTurnCallCount(turn) {
  if (!Array.isArray(state.managerChatEvents)) return 0;
  return state.managerChatEvents.filter((item) => item && item.type === "tool_call" && item.turn === turn).length;
}

function managerChatTurnEndNode(payload, event) {
  const usage = payload && payload.usage;
  const counts = [];
  if (usage && typeof usage === "object" && !Array.isArray(usage)) {
    const fields = [
      ["input_tokens", "in"],
      ["output_tokens", "out"],
      ["total_tokens", "total"],
      ["cache_read_input_tokens", "cache read"],
      ["cache_creation_input_tokens", "cache write"]
    ];
    for (const field of fields) {
      const value = usage[field[0]];
      if (typeof value === "number" && Number.isFinite(value) && value >= 0) {
        counts.push(`${field[1]} ${value.toLocaleString("en-US")}`);
      }
    }
  }
  const turn = event && Number.isFinite(event.turn) ? ` ${event.turn}` : "";
  const calls = managerChatTurnCallCount(event && event.turn);
  const head = `Turn${turn} completed · ${calls} tool call${calls === 1 ? "" : "s"}`;
  const text = counts.length > 0 ? `${head} (Tokens: ${counts.join(", ")})` : head;
  return createElement("div", "manager-chat-marker", text);
}

function renderManagerChatEvents() {
  if (!elements.managerChatTranscript) return;
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
  for (const event of state.managerChatEvents) {
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
  if (state.managerChatRunning) {
    if (!state.managerChatThinkingSince) state.managerChatThinkingSince = Date.now();
    rows.push(managerChatLiveThinkingNode());
  } else if (state.managerChatLastThoughtMs && !state.managerChatEvents.some((item) => item && item.type === "reasoning")) {
    rows.push(createElement("div", "manager-chat-thinking", "Thought for " + managerChatFormatDuration(state.managerChatLastThoughtMs)));
  }
  if (rows.length === 0) {
    elements.managerChatTranscript.replaceChildren(
      createElement("div", "panel-list-empty compact", state.managerChatSession ? "No events yet" : "No open session"),
    );
    return;
  }
  const fragment = document.createDocumentFragment();
  for (const row of rows) fragment.appendChild(row);
  elements.managerChatTranscript.replaceChildren(fragment);
  elements.managerChatTranscript.scrollTop = elements.managerChatTranscript.scrollHeight;
}
