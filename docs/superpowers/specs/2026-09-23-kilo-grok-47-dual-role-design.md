# Grok 4.7 via Kilo CLI: Manager and Task MCP Worker

## Decision and scope

The owner wants Grok 4.7 in AIWorkHub for both Manager Chat and Task MCP
implementation work. For the current development wave, use the authenticated
Kilo CLI `xai` provider through the owner's OAuth/subscription. Do not
substitute OpenCode, Copilot, an xAI API key, or another Grok model silently.
Keep the Kilo-specific transport behind a provider boundary so a later
AIWorkHub-owned Grok route can replace it without changing the conversation,
task, review, or repository contracts.

This spec is for the Kilo route and its live acceptance. A Kilo-independent
route is a separate future design: the official Grok Build CLI offers
subscription login and headless output; the direct xAI Responses API offers
`grok-4.7` through an API key and separate API billing. Neither future
route is implemented or enabled as a side effect of this work.

## Measured starting point

- On 2026-09-23, after the owner connected Kilo's `xai` provider, the
  repository-bound preflight changed from `access_unavailable` to
  `ready_unverified`. This proves a locally readable auth source, not a model
  round trip. The workforce catalog marks the existing `grok_kilo_cli`
  `xai/grok-4.6` row as manager and implementation worker, launch-eligible,
  but with unknown round-trip truth.
- Current source accepts only `xai/grok-4.6` in
  `runtime_adapters.GROK_KILO_SUPPORTED_MODELS`, and its tests explicitly
  reject other model IDs. The Manager Chat backend factory supports only its
  three existing CLI families, not `grok_kilo_cli`. Grok 4.7 therefore cannot
  be claimed operational in either role from preflight alone.
- Kilo's model catalog and CLI may use different provider prefixes in
  different account routes. The Kilo model page identifies a hosted
  `x-ai/grok-4.7` route; the existing AIWorkHub direct-`xai` route uses
  `xai/grok-4.6`. Discover the exact authenticated CLI identity before
  declaring the 4.7 route; no prefix conversion or fallback is implicit.
- AIWorkHub currently projects only the `xai` auth record into a
  per-request isolated Kilo HOME and leaves the source untouched. Kilo's
  documentation says its OAuth refresh token rotates on use and concurrent
  Kilo processes can invalidate one another. Whether the current projection
  can preserve the owner's subscription login across repeated turns is not
  yet measured. Treat this as a credential-lifecycle risk, not a proven
  failure or a reason to copy token values into logs or repository storage.
- The connected MCP runtime reports 0.11.66 while the source tree is
  0.11.68. Live acceptance requires installing and verifying a matching
  release after source changes.

## Route and authority contract

The repository's exact policy-enabled Kilo/xAI/Grok-4.7 identity is selected
per manager turn or per task card. Provider listing, auth-source presence,
policy eligibility, executable reachability, model access, and a completed
round trip are reported separately. An unverified or rejected model is shown
with a reason, not launched under 4.6 or another provider's identity.

Manager Chat uses the existing verified repository conversation, automatic
passive session, per-turn route choice, event timeline, and callback identity
from the Manager Chat design. The Kilo backend must enforce manager-scoped
AIWorkHub tool authority and produce normalized text, tool, usage, error,
and terminal events. A Kilo process that cannot establish the manager tool
boundary fails closed; it never borrows a worker receipt or raw write access.

The Task MCP worker uses the canonical English card, enters `processing`
through exact claim/launch, acknowledges its injected repository context,
uses worker Source Graph and role-scoped tools, and stops at `review_ready`.
Existing-file edits use semantic-edit prepare/apply. The manager reviews
mechanical gates and source independently and closes every reviewed card in
the same turn. No worker may promote its own change or reach another repo.

## OAuth and isolation contract

Never expose OAuth tokens, refresh tokens, or the full auth document in
prompts, tool output, telemetry, commits, or `.aiworkhub` task/context
storage. The AppContainer worker retains only the minimum request-local
capability it needs; no unsandboxed fallback, broad drive grant, or global
credential-file write permission is introduced for convenience.

Before a live Kilo code task, use synthetic rotating-token fixtures to trace
the auth source, isolated copy, refresh, process exit, and next launch.
Before that live probe, warn the owner that token rotation may interrupt the
current Kilo login and obtain approval for that specific risk. Confirm that
the owner's existing Kilo login remains usable after two sequential AIWorkHub
turns. Until that is demonstrated, serialize Kilo OAuth
turns sharing the same credential and do not launch a manager turn and worker
concurrently on it. If safe refresh reconciliation would require AIWorkHub
to write the owner's Kilo auth source or create a dedicated persistent
credential profile, stop for an explicit owner decision on that boundary.
Treat logout/re-login as a recovery path, never as normal task completion.

## Diagnosis, implementation, and acceptance sequence

1. In the MCP host environment, discover the exact Grok 4.7 ID with Kilo's
   model-list command, recording only model metadata, CLI identity, version,
   and sanitized route status. Verify the candidate belongs to the connected
   `xai` provider rather than the Kilo Gateway.
2. Prove the OAuth lifecycle with fixtures and a bounded, sequential
   read-only probe before writing product code. Record whether the isolated
   auth record changes, whether the source remains valid, and whether the
   next request succeeds. If this fails, diagnose and design the credential
   owner boundary before any live implementation worker is used.
3. Extend exact model resolution, repository model policy, workforce catalog,
   and preflight without treating discovery as access proof. Wire Kilo to
   Manager Chat's provider-neutral turn interface and Task MCP's existing
   contained worker path. Give each production boundary and its tests to
   an English worker card with a complete non-overlapping write scope.
4. After mechanical gates and manager review, build/install the matching
   release. Run one bounded Manager Chat read-only turn and one Task MCP
   read-only worker card on the exact Grok 4.7 route. Then run one isolated,
   low-risk code card with tests and review. Check another sequential Kilo
   turn afterward for refresh regressions; concurrency stays disabled until
   its own safety test passes.

Acceptance requires exact model and repository receipts, truthful policy and
preflight states, no leaked secrets, manager events rendered and terminated,
worker-tool receipts, `processing` then genuine `review_ready`, no unexpected
writes, correct review/inbox state, and no retained process/lock/grant leak.
Auth, model-not-found, quota, launch, sandbox, timeout, and tool-protocol
failures must remain distinct. Unit tests or a visible model entry alone do
not establish success.

## Future Kilo-independent boundary

Keep provider identity and transport separate. A later design can add a
Grok Build CLI adapter that uses a qualifying xAI subscription login,
or a direct xAI Responses API adapter with an explicitly supplied API key
and separate billing policy. Both must pass the same repository binding,
role-scoped tool, sandbox, telemetry, cancellation, and review contracts.
Neither should reuse Kilo OAuth tokens or claim that a SuperGrok subscription
automatically authorizes the direct developer API.

## Primary references

- [Kilo CLI model-list command](https://kilo.ai/docs/code-with-ai/platforms/cli-reference)
  and [Kilo xAI OAuth/token-rotation behavior](https://kilo.ai/docs/ai-providers/xai).
- [Kilo Grok 4.7 model identity](https://kilo.ai/models/x-ai-grok-4-7).
- [Grok Build CLI and subscription login](https://docs.x.ai/build/overview)
  and [direct xAI API authentication](https://docs.x.ai/developers/quickstart).
