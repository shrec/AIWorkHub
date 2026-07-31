# Manager Context Graph

The Manager Context Graph is AIWorkHub's opt-in, repository-local continuity
layer for manager conversations. It preserves exact completed-message evidence
and projects deterministic relations between repositories, threads, sessions,
tasks, actors and events. Its purpose is recovery: after a long session,
compaction or manager handoff, a verified manager can find the relevant fact
and reopen the bounded transcript neighborhood in which it was established.

It is not the Source Graph. The Source Graph indexes code for managers and
workers. The Manager Context Graph indexes manager conversation evidence and
is never exposed to workers.

## What is stored

When the feature is enabled, one canonical append-only event contains bounded:

- repository, provider, thread, session and optional task identity;
- event role and stable event type (passive chat capture uses user/assistant);
- completed message content, occurrence time and source reference;
- deterministic content hash and idempotency identity;
- bounded metadata needed to establish provenance.

The rebuildable projection creates event, repository, thread, session, task and
actor nodes. Relations include repository/thread/session membership, producing
actor, associated task and chronological `PRECEDES` edges. The event ledger is
authority; nodes and edges are derived state and can be rebuilt without model
inference.

## What is not captured

The shipped Codex adapter accepts only authoritative completed user and
assistant message items from an exact verified repository/thread route. It
excludes internal reasoning, streaming deltas, tool results, shell commands,
approval prompts and transient protocol events. Capture uses a bounded
asynchronous queue so repository storage cannot block transparent chat
transport.

Context Graph is not a secret store. Users and models must not place secrets in
conversation content or durable context. Repository isolation, bounded payloads
and exact routing reduce exposure but do not turn transcript text into a
credential vault.

## Manager operations

The canonical manager MCP surface provides:

- `aiworkhub_manager_context_graph_search` — full-text search over exact
  transcript-backed evidence;
- `aiworkhub_manager_context_graph_range` — bounded events before and after one
  known event in the same thread;
- `aiworkhub_manager_context_graph_related` — deterministic graph relations for
  one node;
- `aiworkhub_manager_context_graph_event_write` — explicit idempotent manager
  event ingestion;
- `aiworkhub_manager_context_graph_rebuild` — rebuild derived nodes and edges
  from the immutable event ledger.

Generated agent policy tells verified managers to use search, range and related
when the repository setting is enabled. Workers never query or write this
surface.

## Why the other context systems remain necessary

| Context system | Canonical job |
| --- | --- |
| Session Manager | Current execution state, checkpoints, handoffs and closure |
| AI Memory | Reusable decisions and lessons with lifecycle state |
| KB | Curated project contracts and authoritative documentation |
| Manager Context Graph | Exact conversational evidence and deterministic relationships |
| Source Graph | Bounded structural code intelligence |

A transcript hit can explain why a decision was made. AI Memory should hold the
reusable lesson derived from it; KB should hold the approved contract; Session
Manager should hold what is active now. Keeping these authorities separate
prevents an old conversation from silently becoming current project policy.

## Capture support and controls

- Scope: manager only, repository local, opt-in.
- Current passive adapter: completed Codex user/assistant messages.
- Claude/Copilot passive capture: not configured and not claimed as shipped.
- Storage: the canonical transcript database inside that repository's
  `.aiworkhub/` storage registry.
- Settings: repository-local enable/disable control; disabling capture does not
  fabricate deletion of existing evidence.
- Projection: deterministic and rebuildable from the append-only ledger.

## Measuring whether it helps

Evaluate Context Graph with fixed recovery tasks rather than activity counts:

1. **Recovery success:** did the manager find the required historical fact and
   its exact transcript range?
2. **Recovery precision:** how many returned events were actually relevant?
3. **Context restored:** bounded evidence bytes returned versus the transcript
   span that would otherwise need to be replayed.
4. **Time to evidence:** elapsed time and tool calls from question to verified
   source event.
5. **Continuity quality:** whether a post-compaction or replacement manager
   makes the same evidence-backed decision without a stale-policy regression.
6. **Isolation failures:** cross-repository or wrong-thread retrievals; the
   target is zero.

AIWorkHub must label unmeasured values as unavailable. Byte measurements are
not automatically token or dollar savings, and correlation between Context
Graph use and task acceptance is not proof of causation.

## Current limitations

- Passive capture is not yet qualified across every provider.
- The graph uses deterministic identity and chronology relations; it does not
  claim inferred semantic truth.
- Removing or retaining transcript evidence requires an explicit bounded
  lifecycle policy; disabling the feature alone is not a purge operation.
- Search results remain evidence, not an automatic instruction override.

The implementation contract is in `src/aiworkhub/context_graph.py`; passive
Codex capture is in `src/aiworkhub/manager_transcript_capture.py`.
