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

- **Codex:** manager inbox is the portable baseline. A same-host VS Code setup
  may additionally use the optional App Server sideband transport to wake the
  exact visible thread.
- **Claude:** cooperative manager inbox and resumable CLI delivery share the
  same durable outbox. Claude-native channels can provide push delivery when
  the installed Claude client supports them.
- **Other clients:** the repository review inbox remains authoritative even
  when a client has no push-wake API.

Dashboard route status is evidence, not optimism: `available`, `pending` and
`degraded` describe the live manager tuple, transport, backlog, retry state and
last delivery.

## Codex compatibility boundary

The optional sideband implementation wraps the Codex executable selected by VS
Code and transparently proxies its stdio App Server stream. It does not provide
a stable public integration contract: the App Server protocol and the
`chatgpt.cliExecutable` setting are owned by the Codex extension and may change.

AIWorkHub therefore treats sideband as an adapter, not repository authority:

1. Task state and callback events remain durable without it.
2. Unsupported hosts or protocol drift fall back to manager-inbox delivery.
3. The wrapper must preserve argv, stdio and exit behavior and must never kill
   or reconfigure a foreign process.
4. Contract tests qualify supported Codex versions and fail closed on unknown
   message shapes.
5. A future documented editor callback API can replace the adapter without a
   task-store migration.

The architectural decision and upgrade requirements are recorded in
[ADR 0004](adr/0004-codex-callback-compatibility-boundary.md).
