# ADR 0004: Codex callback compatibility boundary

- Status: Accepted
- Date: 2026-07-30

## Context

VS Code does not currently expose a provider-neutral public API that lets a
repository extension inject a turn into an existing Codex chat. AIWorkHub can
optionally place a transparent executable wrapper between the Codex extension
and its App Server child to observe route identity and deliver a bounded wake.
The setting and App Server wire protocol are not AIWorkHub-owned public APIs.

## Decision

The wrapper is an optional, replaceable compatibility adapter. Durable task
state, review transitions, outbox leases and manager ownership remain in the
repository-native core. Manager-inbox delivery is the portable fallback.

The adapter may only:

- proxy exact stdio bytes and process exit semantics;
- observe the minimum route identity needed for repository/thread ownership;
- accept a small authenticated local sideband method allowlist;
- degrade without terminating Codex, VS Code or a foreign process.

Every supported Codex release must pass the recorded handshake, transparent
passthrough, multi-window isolation, restart and fallback contract suite. An
unknown protocol shape disables sideband delivery and reports the reason; it
does not reinterpret or guess the message.

## Consequences

Push wake-up can work in verified same-host installations, while a Codex
extension change cannot corrupt task authority or lose review evidence. Users
may temporarily receive manager-inbox delivery until a new adapter contract is
qualified. Migration to a future public editor API is limited to the callback
adapter.
