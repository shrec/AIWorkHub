"use strict";

const assert = require("assert");
const { api } = require("./opencode-kilo-stream-events.test.js");

const lines = (...events) => events.map((event) => JSON.stringify(event)).join("\n");
const activity = (fields) => ({ type: "aiworkhub_tool_activity", schema_id: "aiworkhub.vscode_lm.activity.v1",
  request_id: "a".repeat(32), repo_id: "repo_test", ...fields });
const heartbeat = (sequence) => ({ type: "aiworkhub_progress", phase: "provider_response", sequence,
  updated_at: `2026-10-10T12:56:${String(sequence).padStart(2, "0")}.000Z` });
const toolRequest = JSON.stringify({ schema_id: "aiworkhub.vscode_lm.tool_request.v1",
  name: "aiworkhub_worker_source_graph_query", input: { mode: "body", query: "_merge_file" } });

// Shape captured from DeepSeek/GLM `*_vscode_lm` workers: hundreds of reasoning chunks and
// provider_response heartbeats interleaved, then an emulated tool call.
const stream = lines(
  { type: "aiworkhub_progress", phase: "request_accepted", sequence: 1, updated_at: "2026-10-10T12:56:13.878Z" },
  activity({ kind: "status", sequence: 1, capture_status: "available", dropped_events: 0, capture_end: false }),
  activity({ kind: "reasoning", sequence: 2, text: "Let me analyze " }),
  heartbeat(3),
  activity({ kind: "reasoning", sequence: 4, text: "the merge path " }),
  heartbeat(5),
  activity({ kind: "reasoning", sequence: 6, text: "before editing." }),
  heartbeat(7),
  activity({ kind: "assistant_text", sequence: 8, text: toolRequest }),
  activity({ kind: "tool", sequence: 9, call_id: "pci_1", tool_name: "aiworkhub_worker_source_graph_query",
    tool_state: "started", tool_transport: "emulated", input_preview: "{\"mode\":\"body\"}", output_preview: "" }),
  { type: "aiworkhub_progress", phase: "tool_turn", sequence: 10, tool_name: "aiworkhub_worker_source_graph_query",
    tool_state: "completed", elapsed_ms: 1530, error_code: "", timeout_ms: 60000, timeout_phase: "" },
  activity({ kind: "tool", sequence: 11, call_id: "pci_1", tool_name: "aiworkhub_worker_source_graph_query",
    tool_state: "completed", tool_transport: "emulated", input_preview: "{\"mode\":\"body\"}", output_preview: "def _merge_file(...)" }),
  heartbeat(12),
  activity({ kind: "reasoning", sequence: 13, text: "Now the fix is clear." }),
  { type: "result", subtype: "success", is_error: false, result: "Applied one bounded edit." },
);

{
  const events = api.timelineEventsFromText(stream);
  const titles = events.map((event) => event.title);

  const heartbeats = events.filter((event) => event.title === "Model responding");
  assert.strictEqual(heartbeats.length, 1, "provider_response heartbeats collapse into one row");
  assert.deepStrictEqual(heartbeats[0].metrics, ["4 updates"]);
  assert(heartbeats[0].message.includes("12:56:12"), "collapsed heartbeat shows the latest update");

  const reasoning = events.filter((event) => event.title === "Reasoning");
  assert.strictEqual(reasoning.length, 2, "consecutive chunks merge; a tool call starts a new block");
  assert.strictEqual(reasoning[0].activityOutput, "Let me analyze the merge path before editing.");
  assert.deepStrictEqual(reasoning[0].metrics, ["3 chunks"]);
  assert.strictEqual(reasoning[1].message, "Now the fix is clear.");
  assert.deepStrictEqual(reasoning[1].metrics, []);

  const request = events.find((event) => event.title === "Tool request | aiworkhub_worker_source_graph_query");
  assert(request, "emulated tool request envelope is shown as a named tool request");
  assert(request.activityInput.includes("_merge_file"));
  assert(!events.some((event) => event.title === "Assistant"), "tool-request JSON is not shown as assistant prose");

  const turn = events.find((event) => event.title === "Tool turn");
  assert.strictEqual(turn.state, "completed");
  assert.strictEqual(turn.label, "aiworkhub_worker_source_graph_query");
  assert.deepStrictEqual(turn.metrics, ["duration 1.5 s"]);

  assert.strictEqual(titles.filter((title) => title === "Tool activity").length, 3, "status + started + completed rows keep their contract");
  assert.strictEqual(titles[0], "Request accepted");
  assert.strictEqual(titles[titles.length - 1], "Result");
  assert(!events.some((event) => /structured event/i.test(event.message)));
}

{
  const events = api.timelineEventsFromText(lines(
    { type: "aiworkhub_progress", phase: "tool_turn", sequence: 1, tool_name: "read", tool_state: "failed",
      elapsed_ms: 60000, error_code: "tool_timeout", timeout_phase: "execute" },
  ));
  assert.strictEqual(events[0].kind, "error");
  assert(events[0].message.includes("tool_timeout") && events[0].message.includes("execute"));
}

{
  const events = api.timelineEventsFromText(lines(
    activity({ kind: "assistant_text", sequence: 1, text: "Plain final explanation." }),
    activity({ kind: "assistant_text", sequence: 2, text: "{\"schema_id\": \"aiworkhub.vscode_lm.tool_request.v1\", broken" }),
  ));
  assert.deepStrictEqual(events.map((event) => event.title), ["Assistant", "Assistant"],
    "prose and malformed envelopes stay assistant text");
}

console.log("vscode-lm-stream-events.test.js: ok");
