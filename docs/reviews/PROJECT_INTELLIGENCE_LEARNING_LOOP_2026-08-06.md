# Project intelligence learning loop

Source: owner-supplied design review, attachment
`3d21dd3d-601a-4c94-83e7-7c9f47996a12`, received 2026-08-06.

## Authority separation

- Source Graph answers: what is true in current code?
- Session Manager answers: what are we doing now, what was tested, and why?
- AI Memory answers: what verified lesson did the project learn?
- Context Graph answers: how are tasks, causes, fixes, evidence, decisions, and
  later regressions related?
- KB answers: what canonical rules and contracts govern work?

These surfaces must remain separate authorities while participating in one
closed learning loop.

## Learning commit protocol

After a manager accepts or rejects a task, create a bounded distillation record
containing task identity, repository area, outcome, verified root cause,
affected invariant, discriminating validation, reusable lesson, and proposed
Context Graph edges. Only an evidence-gated manager outcome may promote a
lesson into AI Memory or durable causal relations into Context Graph.

Worker prose is never sufficient authority for a memory write. Rejected or
inconclusive lessons remain candidates, not canonical project knowledge.

### Implemented locally (2026-08-10)

The manager surface now exposes one explicit `aiworkhub_manager_learning_commit`
operation. It is not called automatically from worker output. For an accepted
outcome it verifies the exact canonical task/request identity and the manager's
sealed `fixed_and_verified` acceptance receipt before any promotion is allowed.

The task database stores the canonical Learning Commit receipt and a resumable
per-authority outbox. Session Manager, Context Graph, AI Memory and KB retain
their separate databases and authority rules; each projection uses a derived
idempotency key. A partial failure remains visibly `partial`, and retrying the
same commit resumes only the failed projection. Context Graph learning edges
are deterministically reconstructed from immutable event metadata during a
projection rebuild.

Local acceptance evidence: 3,245 Python tests passed (35 skipped), including
focused idempotency, partial-retry, cross-request rejection and graph-rebuild
coverage. This is local proof and is not a published-release claim.

## Routing use

Future route advice should learn from the joined tuple of task class,
repository area, model/adapter, context delivered, validation outcome, manager
decision, rework count, tokens, and observed cost. It must preserve unknown
telemetry as unknown and start in advisory mode.
