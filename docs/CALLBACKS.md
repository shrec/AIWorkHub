# Callback delivery

AIWorkHub sends every claimed task that reaches review to the verified manager
for the repository that owns it. The task's terminal substatus does not filter
delivery: successful, blocked, failed, timed-out, cancelled and rejected worker
outcomes all enter the same review inbox with their truthful disposition.

## Durable lifecycle

Terminal transitions enter a repository-local outbox. Events are deduplicated
by task, transition, manager route and claim episode, then leased in bounded
per-manager batches. Temporary busy-manager conditions are parked with backoff;
transport failures use an independent retry budget. Expired leases are
reclaimed after restart, and activation reconciles review rows with missing
outbox entries.

Callbacks never contain worker output, prompts, credentials or source. The wake
message carries only validated task identifiers and terminal transitions; the
manager reads the full evidence through MCP.

## Manager transports

Which manager chat is woken automatically depends on the client:

- **Built-in Manager (dashboard Manager chat):** the supported fully automatic
  path for every backend (`codex_cli`, `claude_cli`, `opencode_cli`). The seat
  bootstraps as `provider=manager_chat` with `route_state=ready` and
  `callback_supported=true`. Its wake consumer claims each callback batch and
  runs it as a manager turn, so reviews continue without an owner prompt. Use
  it to run Codex models with automatic callbacks, including on Windows.
- **Claude Code chat:** the chat waits on `aiworkhub_claude_callback_wait` and
  acknowledges with `aiworkhub_claude_callback_ack` (a two-phase MCP
  long-poll). A background
  `python -m aiworkhub.manager_callback_watch --provider claude ...` exits when
  a callback is pending, which re-invokes the model.
- **Codex VS Code sidebar chat:** manager inbox only. The App Server mux that
  used to push-wake a Codex thread was retired in 0.12.85 and Codex has no
  callback-wait tool, so the sidebar chat receives no push. It can still manage
  the repository; it polls `aiworkhub_task_review_queue` or
  `aiworkhub_completion_inbox` itself.
- **Other MCP clients:** the repository review inbox remains authoritative even
  when a client has no push-wake API.

Dashboard route status is evidence, not optimism: `available`, `pending` and
`degraded` describe the live manager tuple, transport, backlog, retry state and
last delivery.

## Codex sidebar route states

A Codex sidebar chat bootstraps as a verified manager. Its callback route is a
separate question:

- When Codex passes `CODEX_THREAD_ID` (with
  `CODEX_INTERNAL_ORIGINATOR_OVERRIDE=codex_vscode`) into the MCP server
  environment, the route binds to that thread and callbacks land in its inbox.
- Otherwise bootstrap reports `route_pending` with wake reason
  `codex_thread_id_not_observed`. Since 0.12.85 this is the expected state, not
  a broken install. `aiworkhub_task_create` then refuses callback-required
  cards with `callback_route_pending:codex_thread_id_not_observed`, unless a
  built-in Manager seat is active for the same repository; that seat then owns
  the callbacks.
- `callback_adoption` with reason `callback_capable_manager_route_required` in
  the bootstrap reply is informational: this chat did not take over another
  provider's pending wakes. It does not gate launches.

## Retired Codex sideband

Up to 0.12.84 an optional adapter wrapped the Codex executable selected by VS
Code and proxied its stdio App Server stream to wake the exact visible thread.
The App Server protocol and the `chatgpt.cliExecutable` setting are owned by the
Codex extension and are not a stable public contract, so 0.12.85 retired it:
activation no longer installs the launcher and restores a
`chatgpt.cliExecutable` that still points at it. Task state and callback events
never depended on the adapter. The original decision is recorded in
[ADR 0004](adr/0004-codex-callback-compatibility-boundary.md); a documented
editor callback API could add a push path for the sidebar again.
