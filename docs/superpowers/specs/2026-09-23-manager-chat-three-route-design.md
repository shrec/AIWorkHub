# Manager Chat: Automatic Session and Three-Route Vertical Slice

## Purpose and scope

The owner wants Manager Chat to be a usable project-bound conversation, not a
manual CLI launcher. Opening the panel must attach to or create the repository's
manager conversation without a Start button. Enabled GLM 5.3, Claude, and
Codex manager routes must each complete a manager turn and display its result.
The provider starts only when a turn is sent.
The architecture must admit further policy-enabled models without a hardcoded
three-backend picker. OpenCode Muse 1.3 is a Task MCP implementation worker,
not a Manager Chat acceptance route; its activation is specified separately
in `2026-09-23-opencode-muse-worker-activation-design.md`.

This is the first vertical slice of a larger agent-loop effort. It covers the
session, model route, safe provider invocation, turn event, and visible-result
boundaries needed to make those three routes work. A later spec covers durable
message/callback scheduling, tool execution concurrency, interruption recovery,
and context compaction. No code or implementation text is copied from
`E:\claude-code-main`; only high-level behavior is used as design input.

## Measured starting point

- The panel currently requires Start and filters a worker catalog to only
  `claude_cli`, `codex_cli`, and `opencode_cli` (`vscode-extension/media/app.js`).
  Its composer refuses input while `running`, and event polling does not refresh
  status after a turn completes.
- `ManagerSession` currently pins one backend and model for its whole lifetime;
  the manager backend factory supports only those three CLIs
  (`manager_loop.py`, `manager_loop_backends.py`). GLM 5.3 is available through
  `vscode_lm`, but there is no Manager Chat adapter for that host route.
  OpenCode's separate worker path must not be promoted to manager eligibility
  to solve this.
- The current focused Python baseline passes 70/70 tests and the webview
  baseline passes 19/19 tests. These tests do not prove the requested UX:
  webview cases explicitly require Start and a backend picker, while no
  auto-ensure, per-turn route, or live end-to-end test exists in that set.
- Source tree release is 0.11.68 while the connected MCP server reports 0.11.66.
  Source-level tests do not prove installed-runtime behavior until a matching
  package is installed and checked.

## Session and route contract

The repository identity verified by the MCP manager route is authoritative.
Panel open calls a bounded `ensure` operation: return the live repository
conversation if present, otherwise persist a passive one. Reopening the panel
or changing windows never silently creates a second active conversation or
binds to another repository. A passive session consumes no model request and
does not grant task launch authority merely because the panel is visible.
Session creation still respects `AIWORKHUB_ALLOW_WRITES=1`; a closed write gate
shows a clear disabled state instead of pretending to create a session.

Backend/model identity belongs to each turn, not to the conversation record.
The selected route is stored as an exact `(adapter_id, provider/model)` pair,
with the observed model recorded on each turn. An already-running turn retains
its selected route; changing the picker affects only the next turn. Existing
CLI conversation IDs remain route-local and are resumed only with the same
adapter/model. Switching routes rehydrates from the bounded, canonical
conversation history rather than passing one provider's session ID to another.
Existing pinned-session records must remain readable and migrate without
cross-repository data movement: retire the old provider-pinned record with its
existing handoff, then attach the successor conversation to that handoff. Do
not reinterpret an old provider conversation ID as a new route's ID.

Manager host identity, the selected model identity, and worker provider
identity remain separate. In an existing Copilot chat, its active selected
model remains that conversation's manager; the new panel's own selector
controls only the panel conversation and must not rewrite the Copilot route.

The picker is a projection of canonical model policy plus current preflight,
not a separate model registry. It must distinguish discovery, policy
enablement, executable reachability, provider access, and verified round trip.
Only an exact enabled route may be sent. A discovered but disabled route may be
shown with a reason, never launched. Worker role fields such as
`implementation_worker`, `manager`, `reviewer`, `max_risk`, and `inventory_only`
are not rewritten to make Manager Chat work; manager-conversation eligibility
is a separate, explicit decision.

## Provider boundary

One provider-neutral turn interface accepts the verified repo identity,
session/turn IDs, exact route, bounded prompt/history, cancellation signal,
and manager tool policy. It emits normalized events and a terminal outcome.
The three acceptance routes are:

1. `vscode_lm` / `glm-5.3`: use the existing VS Code LM host transport with a
   manager-specific request identity and role-scoped tool protocol. The
   manager turn must not impersonate a worker request or bypass the host's
   model-access and cancellation receipts.
2. An enabled `claude_cli` model: retain its existing streaming adapter and
   conversation resume behavior, but feed it the same route/turn event
   contract and explicit manager tool authority.
3. An enabled `codex_cli` model: retain its existing CLI adapter and
   conversation resume behavior, subject to the same route/turn event
   contract and explicit manager tool authority. An unavailable subscription
   or route remains unavailable; it is not silently replaced with a worker.

Any adapter unable to provide the safe manager tool boundary must fail closed
with a precise route/phase reason. Provider listing alone is not proof that a
model can answer, use tools, or access its subscription. Authentication,
quota, launch, timeout, and tool-protocol failures are distinct outcomes.

## Conversation events and panel

Each turn has stable `session_id`, `turn_id`, route identity, and monotonic
event sequence. Persist bounded user text, assistant-visible text, tool call
and result pairs with exact tool IDs, canonical task references, usage when
observed, error phase, and one terminal outcome. Do not expose chain-of-thought,
secrets, raw credentials, or unbounded provider payloads. Model/tool text is
rendered as inert DOM text, not HTML.

The panel opens directly into the conversation. It shows a route selector,
composer, and a turn-grouped timeline: user message, model badge, streaming
assistant text, collapsible tool progress/results, canonical task state, and
an explicit completed/failed/cancelled marker. It updates status along with
events, so `Running` clears after a terminal turn and the next message is
sendable. If a send is refused, the unsent draft remains in the composer.
Poll responses are session- and turn-bound, deduplicated by sequence, and a
stale response from a prior repository/session cannot repaint the current one.

## Errors, verification, and rollout

Focused tests must cover auto-ensure/reopen, repository and window isolation,
write-gate refusal, per-turn route switching, exact policy rejection, the
three provider adapters, role-scoped manager permissions, terminal status
refresh, duplicate/out-of-order events, inert rendering, send failure draft
retention, and provider errors without a stuck session. A source-only adapter
test is insufficient: after implementation and independent manager review,
build/install a matching VSIX, verify the live MCP version, and run one bounded
read-only round trip on each of GLM 5.3, Claude, and Codex when each exact route
is available. Record observed access/cost/usage truth; do not label a route
fully working from preflight or unit tests alone.

The manager delegates implementation through canonical Task MCP cards in
English, each with production call sites and contract tests in its allowed
write scope. The manager reviews tests and source, then accepts, returns, or
blocks each review in the same turn. Unrelated working-tree changes remain
untouched. No production change from this spec may weaken worker sandbox or
role permissions to make Manager Chat pass.
