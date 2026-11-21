"use strict";

// NF-2026-02325: a live, claimed Claude Code session must read ✓ Route ✓ Callback
// with provider claude. Evaluates the real renderManagerIdentity slice of app.js.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const appSource = fs.readFileSync(path.join(__dirname, "..", "media", "app.js"), "utf8");

function identitySlice() {
  const start = appSource.indexOf("function setIdentityCheck(element, ready, label)");
  const end = appSource.indexOf("// ── wave-mini-roadmap-helpers-begin", start);
  assert.notEqual(start, -1);
  assert.notEqual(end, -1);
  const slice = appSource.slice(start, end);
  assert.ok(slice.includes("function renderManagerIdentity(snapshot)"));
  assert.ok(slice.includes("function setIdentityDetailLabel(valueNode, label)"));
  return slice;
}

function makeElement() {
  const classes = new Set();
  return {
    hidden: true,
    textContent: "",
    attrs: {},
    previousElementSibling: { textContent: "" },
    classList: {
      toggle(name, on) {
        if (on) classes.add(name);
        else classes.delete(name);
      },
      contains(name) {
        return classes.has(name);
      },
    },
    setAttribute(name, value) {
      this.attrs[name] = String(value);
    },
  };
}

const ELEMENT_NAMES = [
  "identityAlert",
  "identityAlertTitle",
  "identityManagerCheck",
  "identityRouteCheck",
  "identityCallbackCheck",
  "identityRoleBadge",
  "identityProviderBadge",
  "identityWindowId",
  "identityThreadId",
  "identitySessionId",
  "identityRouteState",
  "identityCallbackState",
  "identityRepoId",
  "identityDiagnostics",
];

function render(snapshot) {
  const elements = Object.fromEntries(ELEMENT_NAMES.map((name) => [name, makeElement()]));
  const context = { elements, snapshot };
  vm.createContext(context);
  vm.runInContext(`${identitySlice()}\nrenderManagerIdentity(snapshot);`, context);
  return elements;
}

const CLAUDE_SESSION = "0b6d1c62-3f4e-4a8b-9c1d-2e3f4a5b6c7d";

function claudeSnapshot(target) {
  return {
    manager_identity: {
      role: "manager",
      provider: "codex",
      manager_route: { session_id: CLAUDE_SESSION, window_id: "claude_vscode_4242" },
    },
    callback_delivery: { status: "dispatcher_not_running", healthy: false, problems: [] },
    manager_identity_target: target,
  };
}

test("a live claimed Claude target reads ✓ Route ✓ Callback with provider claude", () => {
  const elements = render(claudeSnapshot({
    selected_provider: "claude",
    capability_state: "available",
    reason: "",
    callback_lane: "claude_callback_wait",
    session_id: CLAUDE_SESSION,
  }));

  assert.equal(elements.identityRouteCheck.textContent, "✓ Route");
  assert.equal(elements.identityCallbackCheck.textContent, "✓ Callback");
  assert.equal(elements.identityManagerCheck.textContent, "✓ Manager");
  assert.equal(elements.identityProviderBadge.textContent, "provider: claude");
  assert.equal(elements.identityCallbackState.textContent, "claude_callback_wait");
  assert.equal(elements.identityRouteState.textContent, "available");
  assert.equal(elements.identityAlert.classList.contains("identity-ok"), true);
  assert.equal(elements.identityDiagnostics.textContent, "none");
});

test("a Claude target that is not running keeps ! Route and ! Callback", () => {
  const elements = render(claudeSnapshot({
    selected_provider: "claude",
    capability_state: "callback_required",
    reason: "claude_session_not_running",
  }));

  assert.equal(elements.identityRouteCheck.textContent, "! Route");
  assert.equal(elements.identityCallbackCheck.textContent, "! Callback");
  assert.equal(elements.identityProviderBadge.textContent, "provider: claude");
  assert.equal(elements.identityCallbackState.textContent, "dispatcher_not_running");
  assert.match(elements.identityDiagnostics.textContent, /claude_session_not_running/);
  assert.equal(elements.identityAlert.classList.contains("identity-warn"), true);
});

test("an available Claude target without the callback lane does not grant Callback", () => {
  const elements = render(claudeSnapshot({
    selected_provider: "claude",
    capability_state: "available",
    reason: "",
  }));

  assert.equal(elements.identityRouteCheck.textContent, "✓ Route");
  assert.equal(elements.identityCallbackCheck.textContent, "! Callback");
});

test("an active Manager Chat seat is unchanged and still wins", () => {
  const chatSession = `mls-${"a".repeat(32)}`;
  const elements = render({
    manager_identity: {
      role: "manager",
      provider: "codex",
      manager_route: {
        manager_chat: { session_id: chatSession, backend_id: "claude_cli", model: "claude-opus-5-5" },
      },
    },
    callback_delivery: { status: "unknown", healthy: false },
    manager_identity_target: { selected_provider: "claude", capability_state: "callback_required", reason: "claude_session_not_running" },
  });

  assert.equal(elements.identityRouteCheck.textContent, "✓ Route");
  assert.equal(elements.identityCallbackCheck.textContent, "✓ Callback");
  assert.equal(elements.identityProviderBadge.textContent, "provider: claude_cli");
  assert.equal(elements.identityCallbackState.textContent, "manager_chat");
  assert.equal(elements.identityRouteState.textContent, "ready");
  assert.equal(elements.identityWindowId.textContent, chatSession);
});

test("the Codex dispatcher readiness is unchanged", () => {
  const thread = "019f5097-6dbe-7172-870a-945afc5f3bfa";
  const ready = render({
    manager_identity: { role: "manager", provider: "codex", manager_route: { thread_id: thread } },
    callback_delivery: {
      status: "dispatcher_running",
      dispatch_expected: true,
      healthy: true,
      dispatcher_running: true,
      registered: true,
      repo_id: "repo_a",
      dispatcher_repo_id: "repo_a",
    },
    manager_identity_target: { selected_provider: "codex", capability_state: "available", reason: "" },
  });

  assert.equal(ready.identityRouteCheck.textContent, "✓ Route");
  assert.equal(ready.identityCallbackCheck.textContent, "✓ Callback");
  assert.equal(ready.identityProviderBadge.textContent, "provider: codex");
  assert.equal(ready.identityCallbackState.textContent, "dispatcher_running");

  const pending = render({
    manager_identity: { role: "manager", provider: "codex", manager_route: { thread_id: thread } },
    callback_delivery: { status: "dispatcher_not_running", healthy: false, dispatch_expected: true },
    manager_identity_target: {
      selected_provider: "codex",
      capability_state: "route_pending",
      reason: "codex_route_not_live:claim_with_aiworkhub_manager_callback_lane_claim",
    },
  });

  assert.equal(pending.identityRouteCheck.textContent, "! Route");
  assert.equal(pending.identityCallbackCheck.textContent, "! Callback");
  assert.match(pending.identityDiagnostics.textContent, /codex_route_not_live:claim_with_aiworkhub_manager_callback_lane_claim/);
});

test("a target without a selected provider falls back to the identity provider", () => {
  const elements = render({
    manager_identity: { role: "manager", provider: "codex", manager_route: { thread_id: "t" } },
    callback_delivery: {},
    manager_identity_target: {},
  });

  assert.equal(elements.identityProviderBadge.textContent, "provider: codex");
});
