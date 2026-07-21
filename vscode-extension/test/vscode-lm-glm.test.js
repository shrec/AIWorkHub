"use strict";

// B853: bounded VS Code Language Model API discovery + GLM canary contract.
// Static checks (same source-text-reading pattern as extension-static.test.js
// and explicit-init-no-fallback.test.js) proving:
//   1. Model capability discovery and the GLM canary are wired ONLY through
//      the fixed inbound-message enum -- never auto-invoked at activation,
//      on the refresh timer, or during any snapshot poll.
//   2. The canary requires an explicit native modal credit confirmation
//      every run, sends exactly one fixed prompt, never passes `tools`,
//      supports cancellation, and caps kept output.
//   3. This surface is documented as a bounded canary, not an autonomous
//      Task MCP worker, and stays separate from the BYOK subprocess adapters.

const assert = require("assert");
const fs = require("fs");
const path = require("path");

const root = path.resolve(__dirname, "..");
const ext = fs.readFileSync(path.join(root, "extension.js"), "utf8");
const app = fs.readFileSync(path.join(root, "media", "app.js"), "utf8");

function assertPresent(haystack, values, label) {
  const missing = values.filter((value) => !haystack.includes(value));
  assert.deepStrictEqual(missing, [], `${label}: missing required pattern(s) ${missing.join(", ")}`);
}

function assertAbsent(haystack, values, label) {
  const hits = values.filter((value) => haystack.includes(value));
  assert.deepStrictEqual(hits, [], `${label}: found forbidden pattern(s) ${hits.join(", ")}`);
}

// ── 1. Inbound message enum wiring ──────────────────────────────────────
assertPresent(
  ext,
  [
    '"refreshModelCapabilities"',
    '"runGlmCanary"',
    '"cancelGlmCanary"',
    'case "refreshModelCapabilities":',
    'case "runGlmCanary":',
    'case "cancelGlmCanary":',
    "refreshModelCapabilities(view)",
    "runGlmCanary(view)",
    "cancelGlmCanary(view)",
  ],
  "capability discovery / GLM canary message wiring",
);

// ── 2. vscode.lm usage bounded to the two capability functions only ────
const selectChatModelsCallHits = (ext.match(/await vscode\.lm\.selectChatModels\(/g) || []).length;
assert.strictEqual(selectChatModelsCallHits, 2, "vscode.lm.selectChatModels must be called exactly twice (discovery + canary model lookup)");
assertPresent(ext, ["async function refreshModelCapabilities(view)", "async function runGlmCanary(view)"], "discovery/canary function definitions");

// ── 3. Never automatic: absent from activation, the refresh timer, and any
//      poll/snapshot path. ─────────────────────────────────────────────
const rescheduleMatch = ext.match(/reschedule\(\) \{[\s\S]*?\n {2}\}/);
assert.ok(rescheduleMatch, "ViewState.reschedule() body not found");
assertAbsent(
  rescheduleMatch[0],
  ["refreshModelCapabilities", "runGlmCanary", "vscode.lm"],
  "ViewState.reschedule() (the auto-refresh timer) must never trigger model discovery or the GLM canary",
);

const activateMatch = ext.match(/function activate\(context\) \{[\s\S]*?\n}\n/);
assert.ok(activateMatch, "activate() body not found");
assertAbsent(
  activateMatch[0],
  ["refreshModelCapabilities(", "runGlmCanary("],
  "activate() must never auto-invoke model discovery or the GLM canary",
);

const pushSnapshotMatch = ext.match(/async function pushSnapshot\(view\) \{[\s\S]*?\n}\n/);
assert.ok(pushSnapshotMatch, "pushSnapshot() body not found");
assertAbsent(
  pushSnapshotMatch[0],
  ["refreshModelCapabilities(", "runGlmCanary("],
  "pushSnapshot() (every refresh/poll) must never trigger model discovery or the GLM canary",
);

// ── 4. Explicit per-run native modal credit confirmation, one fixed prompt,
//      cancellation, output cap, and no tool execution. ────────────────
const runGlmCanaryMatch = ext.match(/async function runGlmCanary\(view\) \{[\s\S]*?\n}\n/);
assert.ok(runGlmCanaryMatch, "runGlmCanary() body not found");
const canaryBody = runGlmCanaryMatch[0];

assertPresent(
  canaryBody,
  [
    "vscode.window.showWarningMessage(",
    "modal: true",
    '"Send Prompt"',
    'confirmation !== "Send Prompt"',
    "CancellationTokenSource()",
    "GLM_CANARY_FIXED_PROMPT",
    "GLM_CANARY_MAX_OUTPUT_CHARS",
    "isCancellationRequested",
  ],
  "GLM canary run body",
);

// Exactly one sendRequest call, and it must never pass a `tools` option.
const sendRequestHits = (canaryBody.match(/\.sendRequest\(/g) || []).length;
assert.strictEqual(sendRequestHits, 1, "runGlmCanary must call sendRequest exactly once (one fixed prompt)");
assertAbsent(canaryBody, ["tools:", "tools ="], "runGlmCanary must never pass a `tools` option (executes no tools)");

// The one fixed prompt: exactly one LanguageModelChatMessage.User(...) call,
// and it must be the constant, never repository/task/user-interpolated text.
const userMessageHits = (canaryBody.match(/LanguageModelChatMessage\.User\(/g) || []).length;
assert.strictEqual(userMessageHits, 1, "exactly one fixed canary prompt message must be constructed");
assert.ok(canaryBody.includes("LanguageModelChatMessage.User(GLM_CANARY_FIXED_PROMPT)"));

// A second concurrent run must be rejected, never queued/silently dropped.
assertPresent(canaryBody, ['"busy"'], "a second concurrent canary run must report busy, not silently queue");

// ── 5. Sanitized, bounded model metadata only -- no secret/session leakage.
assertPresent(
  ext,
  [
    "function sanitizeModelInfo(model)",
    "vendor:",
    "family:",
    "version:",
    "id:",
    "name:",
    "maxInputTokens:",
  ],
  "sanitizeModelInfo bounded metadata projection",
);
assertPresent(
  ext,
  ['MODEL_CAPABILITY_STATES.available', 'MODEL_CAPABILITY_STATES.notVisible', 'MODEL_CAPABILITY_STATES.accessDenied', 'MODEL_CAPABILITY_STATES.requestFailed'],
  "refreshModelCapabilities must report AVAILABLE/NOT_VISIBLE/ACCESS_DENIED/REQUEST_FAILED",
);
assertPresent(
  ext,
  ['available: "AVAILABLE"', 'notVisible: "NOT_VISIBLE"', 'accessDenied: "ACCESS_DENIED"', 'requestFailed: "REQUEST_FAILED"'],
  "MODEL_CAPABILITY_STATES literal values",
);

// ── 6. Never claims/launches a task; documented as a bounded canary, not an
//      autonomous Task MCP worker; BYOK adapters stay separately labeled. ─
assertAbsent(
  ext,
  ["taskctl claim-start", "aiworkhub_agent_launch_task", "auto-pickup"],
  "the GLM canary/discovery surface must never claim or launch a Task MCP task",
);
assertPresent(
  ext,
  ["not an autonomous Task MCP worker", "deepseek_copilot_cli"],
  "the GLM canary must be documented as bounded (not an autonomous worker) and reference the separately-labeled BYOK adapters",
);

// ── 7. Webview wiring: buttons post the fixed messages; render function
//      names exist in app.js and are reachable only from message handling
//      or explicit click listeners (never from an interval). ─────────────
assertPresent(
  app,
  [
    'vscode.postMessage({ type: "refreshModelCapabilities" });',
    'vscode.postMessage({ type: "runGlmCanary" });',
    'vscode.postMessage({ type: "cancelGlmCanary" });',
    "function renderModelCapabilities(payload)",
    "function renderGlmCanary(payload)",
    'case "modelCapabilities"',
    'case "glmCanary"',
  ],
  "app.js model-capability/GLM-canary Webview wiring",
);
assertAbsent(app, ["setInterval"], "app.js must never run its own timer (host-owned ViewState.reschedule is the only poller)");

console.log("AIWorkHub VS Code LM API discovery + GLM canary contract checks passed");
