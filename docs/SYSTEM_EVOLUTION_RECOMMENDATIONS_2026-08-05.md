# AIWorkHub system-evolution roadmap (2026-08-05)
## Purpose

This document records candidate architecture work after `0.8.99`. It is a
roadmap, not a performance claim. Every benefit remains **unmeasured** until a
checked artifact records the population, denominator, provider usage, elapsed
time, manager decision, and counterfactual.

AIWorkHub already provides repository-bound task state, isolated workspaces,
dependency-aware launch, Source Graph v5, durable context, evidence-gated
review, multi-model routes, callbacks, retention, and operational telemetry.
The work below closes specific gaps in that existing control plane.

## Evidence rules

- Do not convert structural bytes into provider-token or cost savings.
- Do not treat missing model identity, usage, price, or manager decisions as
  zero.
- Evaluate routing and retry changes by **tokens, cost, and elapsed time per
  manager-accepted outcome**, not by task completion alone.
- Keep automatic behavior advisory-only until matched canaries demonstrate no
  quality or risk regression.
- Preserve repository-local `.aiworkhub/` authority; cross-repository
  coordination must exchange signed receipts, never databases.

## Priorities

### P0 — Advisory economic routing with safe denominators

**Already present**

- `workforce_router.py` has capability, context, tool, quality, provider, and
  risk gates.
- `workforce_catalog.py` exposes model/adapter capabilities and joins observed
  evidence without inventing quota.
- `cost_ledger.py` labels unknown cost and exposes an association-only model
  outcome matrix.

**Gap**

Acceptance and cost populations can have different denominators; model
identity is sometimes unknown; no deterministic cost-per-accepted-outcome
advisory or canary state machine exists.

**Smallest safe implementation**

1. Add a matched-denominator `cost_per_accepted_outcome` view to
   `cost_ledger.py`, with explicit `UNKNOWN` and `UNMEASURED` states.
2. Add a pure advisory scorer to `workforce_router.py`. Apply capability and
   risk gates before economic ranking. Unknown cost must never rank as free.
3. Surface the advisory and its evidence coverage through
   `workforce_catalog.py`; do not change manager choice automatically.
4. Add deterministic tie-breaking and an advisory → shadow → bounded-canary
   promotion sequence. Roll back on acceptance, validation, evidence-coverage,
   or risk regression.

**Required evidence before activation**

A matched, uncapped, multi-task benchmark partitioned by task family and risk,
recording provider input/output tokens, cost, elapsed time, retries,
validation, and manager acceptance.

**Implemented local advisory foundation (2026-08-10)**

- The cost ledger now computes a single-model, single-route, matched-decision
  cost per accepted outcome and fails closed as `UNKNOWN` or `UNMEASURED`.
- Workforce ranking exposes a deterministic economic advisory while preserving
  the pre-existing capability/quality/cost selection result.
- Unknown cost never ranks as free; mixed-model tasks are excluded from model
  attribution; reviewer cost is not assigned to the worker model.
- Advisory-to-shadow/canary promotion remains disabled pending the required
  matched, uncapped, family-and-risk parity evidence above.

### P0 — Meaningful research-output validation

The current research gate accepted a literal `"..."` as meaningful output.
That allowed a content-free worker result to reach `review_ready`.

Add a deterministic minimum-information gate for read-only research results:
reject ellipsis/placeholders, require at least one verifiable finding or an
explicit evidence-backed `inconclusive` result, and retain the raw output hash.
This is a correctness fix, not a token-saving claim.

### P1 — Manager-assisted task decomposition

**Already present**

- Canonical task DAGs, dependency blockers, collision checks, disjoint
  `allowed_writes`, and `task_dependency_autolaunch_reconcile` exist.

**Gap**

AIWorkHub does not yet turn one high-level objective into a verified child DAG
by itself.

**Next step**

Add an advisory decomposition proposal built from Source Graph
`impact`/`deps` evidence. The manager must approve the proposed boundaries
before card creation. Auto-spawning is deferred until collision, dependency,
and accepted-outcome benchmarks exist.

Measure wall-clock time, total attempts, collisions, retries, and accepted
outcomes against the same tasks executed sequentially. No throughput
multiplier is claimed today.

**Implemented advisory foundation (2026-08-10)**

- `aiworkhub_manager_task_decomposition_preview` accepts only a canonical,
  hash-verified Source Graph `impact`/`deps` receipt from the active repo.
- Every child has bounded task identity, objective, write/output scope,
  dependency edges and exact evidence refs present in that receipt.
- Cycles, external dependencies, unverified refs and cross-child write
  collisions fail closed.
- The result is a deterministic proposal digest with
  `manager_approval_required=true`; it never creates, claims or launches a
  task. Automatic spawning remains deferred pending the matched benchmark.

### P1 — Diagnostic delta-rework loop

**Already present**

AIWorkHub retains validation evidence, predecessor request identity, changed
paths, review feedback, and explicit terminal retry/rework transitions.

**Gap**

Failed checks do not yet produce a bounded automatic diagnostic rework
proposal with a verified failure class and stop condition.

**Next step**

Normalize executable, exit code, failing target, bounded stderr/stack trace,
and changed-path hashes into a diagnostic receipt. Let the manager enable a
small, repository-configured retry policy per failure class. Do not infer a
universal retry count or token cap.

Measure convergence rate, repeated-failure rate, tokens per accepted outcome,
and false-green repair cost before enabling automatic retries by default.

### P1 — Source Graph retrieval and freshness

**Already present**

Source Graph performs incremental indexing for changed and deleted files and
supports symbol-precise `focus`, `slice`, `body`, `calls`, `trace`, `impact`,
`testmap`, and typed bundles. Exact-symbol slice has a verified structural
fixture; it is not a provider-token claim.

**Gaps**

- The checked retrieval corpus now covers ten non-empty cases and enforces
  recall@k, MRR and success@k minimums, but accepted-task outcome coverage is
  still zero and broad free-form query coverage remains small.
- Freshness should be event-triggered where safe and retain a deterministic
  fallback refresh.
- Vector retrieval has no measured marginal benefit over current structural
  and lexical modes.

**Next step**

Continue adding task-derived checked queries and bind retrieval receipts to
manager-accepted outcomes. The evaluator already records precision@k,
recall@k, MRR, success@k, returned bytes and latency, but those structural
measurements are not causal product savings. Only after accepted-outcome
coverage exists should a small local vector candidate enter a matched A/B
lane. Do not add embeddings or claim vector benefit before that comparison.

### P2 — Optional OCI worker sandbox

Landlock/worktree isolation remains the fast default. A rootless Podman/Docker
driver may be useful for tasks requiring system packages or stronger process,
network, and dependency boundaries.

Before implementation, define capability detection, image provenance,
network policy, secret handling, workspace promotion, cancellation, and
cross-platform behavior. Benchmark startup latency and accepted-outcome cost;
do not claim zero host leakage without an adversarial security test.

### P2 — Federated cross-repository dependencies

Known-repository routing and repository-local authority already exist.
Federated dependency leases do not.

Design signed, expiring receipts that identify the source repository, target
repository, task identity, expected artifact/schema hash, and terminal
decision. Repositories must never read or mutate each other's SQLite stores.
Start as manager-visible advisory state; automatic cross-repository release is
deferred until replay, expiry, and confused-deputy tests pass.

### P2 — Event-driven dashboard transport

The extension currently uses polling in several runtime and webview paths.
Replace only the high-frequency state path with a bounded push channel while
retaining snapshot reconciliation after reload, dropped frames, or extension
host restarts. Streaming stdout/stderr must remain bounded and redacted.

Measure event latency, CPU wakeups, transferred bytes, memory growth, and
recovery correctness against the polling baseline. “Zero latency” is not a
valid target.

## Implementation order

1. Fix meaningful-output validation and make the quality ratchet
   release-blocking.
2. Implement advisory economic routing and its matched benchmark.
3. Add diagnostic delta-rework as opt-in policy.
4. Benchmark manager-assisted decomposition.
5. Expand Source Graph retrieval evidence before considering vectors.
6. Prototype OCI, federation, and push telemetry independently behind
   capability flags.

## Claim matrix

| Area | Current evidence | Public claim allowed now | Next decision metric |
|---|---|---|---|
| Economic routing | Association-only model/outcome fields; incomplete identities and cost coverage | No automatic-routing savings claim | Cost, tokens, and time per accepted outcome |
| Task decomposition | DAG/collision/autolaunch primitives exist | Dependency-safe orchestration exists | Matched sequential vs decomposed outcomes |
| Rework | Durable feedback and terminal retry exist | Evidence-preserving rework exists | Convergence and repeated-failure rates |
| Source Graph | Incremental indexing and checked structural slice fixtures | Structural payload ratios with exact fixture scope | Retrieval quality plus accepted-task outcomes |
| OCI sandbox | Not implemented | None | Security tests and startup/outcome overhead |
| Federation | Known-repository discovery only | Repository-local isolation | Lease replay/expiry/authority correctness |
| Streaming UI | Polling baseline exists | Operational dashboard exists | Latency, CPU, bytes, memory, recovery |
