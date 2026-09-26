# Manager Chat Automatic Conversation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Opening Manager Chat attaches to a passive repository conversation, and each sent turn uses an explicitly selected, policy-authorized manager model while its result is visible in the panel.

**Architecture:** Keep the existing repository-owned `ManagerSession` and event store, but remove backend/model ownership from the conversation lifecycle. Resolve an exact route at each turn through a separate manager policy projection, then normalize provider events into the existing bounded event stream. The webview keeps its catalog-backed model list and callback wake machinery; it stops requiring Start and renders turn-bound output.

**Tech Stack:** Python manager loop and MCP service, VS Code extension JavaScript/webview, pytest, Node test runner.

**Spec:** `docs/superpowers/specs/2026-09-23-manager-chat-three-route-design.md`

## Global Constraints

- Verified repository identity is authoritative; no chat may attach to another repository after a window switch.
- A passive session makes no provider call. Creation respects `AIWORKHUB_ALLOW_WRITES=1`; a closed gate produces a visible refusal.
- Route identity belongs to the turn. Exact adapter and model are recorded; no silent model substitution or cross-provider conversation-ID reuse.
- Existing pinned-session records remain readable and migrate by a durable handoff, not by reinterpreting their provider session IDs.
- Only enabled manager routes may send. Worker eligibility must not imply manager eligibility.
- Text and tool output are bounded, redacted where required, and rendered as inert DOM text. No chain-of-thought or credentials are persisted.
- Use AIWorkHub Task MCP cards in English for implementation, semantic edits for existing files, and manager review before acceptance. Do not copy code from `E:\claude-code-main`.

## Review Focus

- Two windows open the same repository simultaneously: exactly one durable active conversation, with no duplicate provider request.
- A window changes repository while a status/events response is in flight: the old response cannot repaint the new repository's panel.
- A user changes the model while a turn runs: the running turn keeps its original route and only the next turn sees the new selection.
- A discovered model is disabled, inaccessible, or quota-exhausted: the draft remains and the UI reports the exact policy/access phase; no fallback model runs.
- A legacy pinned session is present: its provider conversation ID never migrates to another adapter, and its handoff remains readable.

---

### Task 1: Passive repository conversation and legacy handoff

**Files:**
- Modify: `src/aiworkhub/manager_loop.py` (`ManagerSession`, `ManagerOrchestrator.start`, session persistence and rotation)
- Test: `tests/test_manager_loop.py`

**Interfaces:**
- Consumes: verified repository identity from `ManagerOrchestrator.for_repository`.
- Produces: `ManagerOrchestrator.ensure() -> ManagerSession`, idempotent and provider-free; persisted `session_id` and `repo_id` remain stable on repeated calls.

- [ ] **Step 1: Write failing tests** for two simultaneous `ensure()` calls, reopen after reconstructing the orchestrator, closed write gate, and a pinned legacy session. The core assertion is:

  ```python
  first = orchestrator.ensure()
  second = orchestrator.ensure()
  assert first.session_id == second.session_id
  assert first.repo_id == second.repo_id
  assert backend.started == []
  ```

  In the legacy case, assert that a successor session has a handoff reference and no inherited provider conversation ID.

- [ ] **Step 2: Run** `.venv/Scripts/python.exe -P -m pytest -q tests/test_manager_loop.py`; observe the new tests fail because `ensure` does not exist.
- [ ] **Step 3: Implement** `ensure()` under the repository's existing exclusive session lock. Return an active session unchanged when it is already valid; otherwise persist one passive session without invoking the backend. Keep `ManagerSession.from_json` compatible with `aiworkhub.manager_loop.v1` records and retire a pinned predecessor through the existing handoff path. Record route identity only on turn events, not in the passive session's authority decision.
- [ ] **Step 4: Run** the exact suite from Step 2 and verify both the new assertions and existing rotation tests pass. Review the session file and event schema for boundedness and cross-repository rejection.
- [ ] **Step 5: Commit** only `src/aiworkhub/manager_loop.py` and `tests/test_manager_loop.py` with a narrow message such as `feat: ensure passive manager conversation`.

### Task 2: MCP ensure boundary and write-gate truth

**Files:**
- Modify: `src/aiworkhub/manager_loop_service.py` (`start`, `_entry_for`, `status`)
- Modify: `src/aiworkhub/server.py` (manager-loop tool registration beside `aiworkhub_manager_loop_start`)
- Test: `tests/test_manager_loop_service.py`, `tests/test_server.py`

**Interfaces:**
- Consumes: `ManagerOrchestrator.ensure()` from Task 1.
- Produces: `manager_loop_service.ensure(repo) -> {ok, session, running}` and `aiworkhub_manager_loop_ensure()` using the verified manager repo route.

- [ ] **Step 1: Write failing tests** proving the MCP tool resolves the verified repository, refuses a closed write gate, reuses the same `session_id` across calls, and does not invoke a provider. Assert the shape explicitly:

  ```python
  first = manager_loop_service.ensure(repo)
  second = manager_loop_service.ensure(repo)
  assert first["ok"] is True
  assert first["session"]["session_id"] == second["session"]["session_id"]
  assert first["running"] is False
  ```

- [ ] **Step 2: Run** `.venv/Scripts/python.exe -P -m pytest -q tests/test_manager_loop_service.py tests/test_server.py`; verify the new tests fail at the missing ensure boundary.
- [ ] **Step 3: Implement** a bounded ensure service call and MCP wrapper. Reuse `_manager_loop_repo_root` and the existing write-gate check; do not accept a caller-supplied repo path or treat panel visibility as task-launch authority. Preserve `start` temporarily for old clients, but do not call it from the new panel.
- [ ] **Step 4: Run** the same suites; inspect the MCP tool descriptor and test that `AIWORKHUB_ALLOW_WRITES=0` yields an explicit error and no session file.
- [ ] **Step 5: Commit** only the four files in this task with `feat: expose verified manager chat ensure`.

### Task 3: Per-turn manager route and provider-neutral result

**Files:**
- Modify: `src/aiworkhub/manager_loop.py` (turn dispatch/event identity)
- Modify: `src/aiworkhub/manager_loop_service.py` (`send`)
- Modify: `src/aiworkhub/manager_loop_backends.py` (route-local CLI sessions and VS Code LM manager adapter)
- Modify: `src/aiworkhub/server.py` (send and route-policy tool boundary)
- Test: `tests/test_manager_loop.py`, `tests/test_manager_loop_service.py`, `tests/test_manager_loop_backends.py`, `tests/test_server.py`

**Interfaces:**
- Consumes: a passive `session_id`, verified repo, and exact `{adapter_id, model}` selected for one turn.
- Produces: one terminal turn with immutable route identity and normalized bounded `assistant_text`, `tool_call`, `tool_result`, `usage` (only when observed), `error`, and `turn_end` records.

- [ ] **Step 1: Write failing tests** for switching `claude_cli -> codex_cli -> glm_vscode_lm` across three turns without switching `session_id`; an in-flight turn keeps its own route; a disabled route is rejected before provider invocation; a returned model differing from the request is rejected; provider auth/quota/timeout errors leave the session idle and the next turn sendable. Use fake adapters so tests do not spend provider credits.
- [ ] **Step 2: Run** `.venv/Scripts/python.exe -P -m pytest -q tests/test_manager_loop.py tests/test_manager_loop_service.py tests/test_manager_loop_backends.py tests/test_server.py`; verify these new cases fail for the current pinned-session route.
- [ ] **Step 3: Implement** exact-route selection at send time, with a separate manager-eligibility check over canonical model policy and preflight. Keep CLI conversation IDs keyed by exact adapter/model; on a route switch, build a bounded history brief rather than passing the predecessor's provider ID. Add a manager-specific VS Code LM request identity and enforce manager-only tool policy. Append normalized events with `session_id`, `turn_id`, monotonic `seq`, and exact route.
- [ ] **Step 4: Run** the suites from Step 2 and verify the role/gate tests remain green. A missing model-access receipt must be `unverified` or an explicit error, never a fabricated success.
- [ ] **Step 5: Commit** only the production and test files in this task with `feat: route manager chat turns per model`.

### Task 4: Automatic panel attach and visible turn timeline

**Files:**
- Modify: `vscode-extension/extension.js` (Manager Chat host messages, MCP calls, and inline webview markup that currently contains Start)
- Modify: `vscode-extension/media/app.js` (`managerChatEnabledModels`, `applyManagerChatSessionUi`, `renderManagerChatStatus`, `renderManagerChatEventsResponse`)
- Test: `vscode-extension/test/manager-chat-panel.test.js`

**Interfaces:**
- Consumes: `aiworkhub_manager_loop_ensure`, manager policy projection, turn-bound status/events, and send from Tasks 2-3.
- Produces: a repository-bound passive conversation on panel open, unchanged unsent draft on refusal, and a turn-grouped inert DOM timeline.

- [ ] **Step 1: Write failing webview/host tests** for panel open auto-ensure, dynamic model list from canonical catalog (do not replace the existing list mechanism), disabled manager route, no Start button, text/tool/usage/error rendering, terminal status refresh, duplicate/out-of-order sequence, stale repository response, and draft retention after failed send. The DOM safety assertion is:

  ```js
  assert.equal(messageNode.textContent, '<img src=x onerror=alert(1)>');
  assert.equal(messageNode.querySelector('img'), null);
  ```

- [ ] **Step 2: Run** `node vscode-extension/test/manager-chat-panel.test.js`; verify the new cases fail on the old Start/pinned-session behavior.
- [ ] **Step 3: Implement** ensure on panel open/rebind, scope every response by repo/session/turn, and keep `managerChatEnabledModels` as the existing catalog projection while filtering by manager eligibility rather than worker eligibility. Render model text with `textContent`; show tool call/result and usage only from canonical events. Clear `Running` after a terminal event, and preserve composer input on refusal. Do not create a second active conversation on reload.
- [ ] **Step 4: Run** `node vscode-extension/test/manager-chat-panel.test.js` and `npm --prefix vscode-extension test`; verify no old Start assertion remains and no unrelated extension test regresses.
- [ ] **Step 5: Commit** only the files in this task with `feat: open manager chat automatically`.

### Task 5: Installed Windows acceptance

**Files:**
- Test: `tests/test_manager_loop.py`, `tests/test_manager_loop_service.py`, `tests/test_manager_loop_backends.py`, `vscode-extension/test/manager-chat-panel.test.js`
- Artifact: a version-matched local VSIX and three exact route receipts, recorded through canonical AIWorkHub Task MCP review evidence.

**Interfaces:**
- Consumes: reviewed Tasks 1-4; produces an observed route result, not a source-only claim.

- [ ] **Step 1: Run** the focused Python suites and `npm --prefix vscode-extension test`; record counts and failures verbatim.
- [ ] **Step 2: Build and install** the package matching the source version. Confirm the connected MCP server reports that same version after reload before any live claim.
- [ ] **Step 3: Open Manager Chat** twice on the verified Windows repository and confirm the same passive `session_id`, no Start control, and no provider launch before send.
- [ ] **Step 4: Send one bounded read-only turn** on each *available and explicitly manager-enabled* `glm_vscode_lm/glm-5.3`, `claude_cli/<exact enabled model>`, and `codex_cli/<exact enabled model>` route. Record request/model identity, text/tool/usage/error events and terminal state. If a route lacks access, record the exact denial; do not substitute another route or label it passed.
- [ ] **Step 5: Reload and change repository**, verify old responses do not repaint the new panel, then close the acceptance card by manager review with all remaining UNKNOWN items named.

## Self-review boundary

This plan covers the Manager Chat vertical slice only. OpenCode Muse worker activation, Source Graph LSP, semantic delta review, and dynamic provider discovery are separate workstreams in RM-2026-00068. The already-dynamic model-list rendering is preserved; policy eligibility and exact per-turn routing are the new work. No production implementation is authorized by this plan alone before its card scopes and tests are reviewed.
