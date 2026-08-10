# AIWorkHub Product Roadmap

Status: canonical product direction after the 0.8.45 repository, orchestration,
storage, CI, and tool-use closure. This roadmap separates shipped capability
from partial capability and planned work. A design document or canary is not a
shipped feature.

## Product position

**AIWorkHub is the repository-native AI engineering control plane for VS Code.**
It coordinates multiple coding models against isolated repository state,
replaces repeated raw source discovery with a Source Graph, preserves project
continuity through Session Manager, AI Memory, and KB, and requires evidence
before a task can be accepted.

The product remains local-first. Repository state belongs to that repository;
model prompts, source text, credentials, and memories are not uploaded by
AIWorkHub telemetry.

## Current delivery checkpoint (local 0.9.40 candidate, 2026-08-10)

This checkpoint is the current operational snapshot; the older baseline below
remains useful history. It does not promote an unchecked roadmap item to
shipped status.

- Release `v0.9.39` is the latest published baseline. Local candidate `0.9.40`
  adds the required-file semantic-edit placeholder guard and truthful
  `{python} -m ruff|mypy` tool-entrypoint resolution. It passes 3,450 Python
  tests (35 skipped), the complete extension suite, Ruff, strict mypy, release
  metadata validation and VSIX packaging. The candidate is installed locally
  but is not a public release claim.
- The canonical queue has four actionable cards and no orphaned processing:
  NF41 is retained at review for provider-free validation replay after the
  `0.9.40` runtime reload; clean-root GLM successors for NF22, NF45 and NF19
  are pending and ready.
- The Plan DAG contains 150 cards: 146 terminal and the four actionable cards
  described above. There is no dependency cycle or write-scope collision.
  The common evidence-level contract, durable attempt-artifact manifest and
  graph-scoped reviewer packet are now implemented and locally validated. The
  meaningful-output/task-type behavioral contract is also implemented:
  specialized cards fail before launch without exact validation roles and the
  same observed receipts are rechecked at finalization and manager acceptance.
  The manager-gated Learning Commit integration, bounded NeedFix Markdown
  intake, atomic terminal-review/callback transition, release-assurance gate,
  fail-closed Windows path-mux staging fallback, and explicit superseded-task
  replacement-edge projection are now locally complete. Plan-DAG readiness now
  follows the audited replacement chain, waits for the replacement to finish,
  and blocks malformed, missing, ordinary-archive, or cyclic chains. A
  repository-native Roadmap registry is now implemented locally as the
  explicit manager-approved layer between NeedFix intake and the executable
  Task DAG. It preserves outcome/dependency/acceptance/task provenance in its
  own audited store, exposes bounded MCP operations, and has a dedicated
  read-only VS Code dashboard popup. Completion is gated on canonical finished
  tasks or explicit task-free evidence.
  Local proof is not promoted to a release claim.
- NeedFix contains 115 durable records after the current closure rebase: 85
  resolved, 3 duplicate, 7 captured, 9 accepted, 1 linked to a created task,
  4 deferred and 6 archived. Captured ideas are not
  accepted roadmap commitments, and accepted findings are not resolved fixes.
- Windows packaged-runtime qualification remains the immediate platform gate.
  The `v0.9.37` dashboard now reports failed repair attempts truthfully, and
  the shared event-ledger lock fallback is implemented, but live confirmation
  is still required for finalization recovery and validation-only replay. One
  VS Code window timed out during MCP child recovery while another window on
  the same machine launched successfully, narrowing the remaining MCP defect
  to window-local child/session/extension-host ownership or recovery routing.
- A fresh Source Graph generation was present, but one roadmap-oriented free-
  form focus query returned zero file/entity evidence and required exact-target
  fallback. The checked retrieval corpus is now ten non-empty cases with
  recall@k `1.0`, MRR `0.95`, success@k `1.0`, mean returned payload
  `3035.2` bytes, and an environment-observed mean/p95 latency of
  `19.328/28.936 ms`. Accepted-outcome coverage is still `0`, so this proves
  the checked structural retrieval contract, not broad-query quality, causal
  task benefit, provider-token savings, or a vector-search advantage.

The next closure order is: reload and live-verify the local `0.9.40` runtime,
provider-free replay/accept NF41, verify the two new fidelity/tool-resolution
guards, then resume NF22, NF45 and NF19 from clean task roots. Windows-only and
matched-benchmark residuals stay open until their stated evidence exists.

## Assessment intake and assurance roadmap (2026-08-06)

Eight owner-supplied product and architecture reviews are preserved under
[`docs/reviews/`](reviews/README.md). They are decision inputs, not shipped
claims. The following checklist is the canonical follow-through so no review is
lost:

- [x] Preserve each review as a separately attributable repository document.
- [x] Add the durable Roadmap layer between NeedFix and Task DAG, including
  dependency and completion gates, canonical task links, MCP tools, audit
  events, dashboard snapshot data, and a dedicated read-only popup.
- [x] Add bounded Markdown review/roadmap intake. Preview follows only safe
  repository-local Markdown links, extracts explicit issue/recommendation/gap
  lists plus unchecked roadmap items, and seals source hashes and stable source
  fingerprints without writing. Exact preview commit creates or refreshes only
  untrusted `captured` NeedFix proposals; it cannot promote or mutate a
  triaged/accepted/terminal item.
- [x] Add the deterministic SARIF contract foundation and focused tests.
- [x] Complete and promote the common evidence-level contract. Worker and
  reviewer finalization now assign canonical outcome levels from observed
  gates, manager acceptance enforces a task-type minimum against the sealed
  attempt manifest, and independent revalidation/promotion emits the durable
  `fixed_and_verified` acceptance receipt.
- [x] Complete the durable attempt-artifact manifest and wire it into worker,
  quality-reviewer, and failed-attempt finalization. Each attempt now seals
  bounded metadata, change-index, validation, provider-usage, and review
  artifacts behind exact byte hashes; successful review evidence and the
  process ledger retain the immediately re-verified manifest receipt.
- [ ] Finish same-path semantic-edit transactions and byte-fidelity regression
  coverage; then run an uncapped, matched whole-file-versus-semantic A/B.
- [x] Add meaningful-output and task-type behavioral anti-collapse gates.
  Read-only research rejects empty/tool-only/placeholder finals and retains an
  exact stdout hash. Writable cards may explicitly select `bugfix`, `refactor`,
  `performance`, `security`, or `data_ml`; task creation then requires aligned
  `validation_roles` for reproduction+regression, parity, numeric
  baseline+delta, negative fixtures, or schema+distribution respectively.
  Finalization and manager acceptance both recompute the deterministic gate;
  performance receipts use bounded `AIWORKHUB_METRIC` JSON and AIWorkHub
  computes direction/tolerance truth instead of trusting model prose.
- [x] Build graph-scoped reviewer packets with risk-selected lenses and
  evidence-bearing normalized findings. Production review preparation now
  resolves changed symbols from exact candidate bytes, joins bounded canonical
  caller/test evidence, carries invariants/forbidden changes/validation and
  explicit unknowns, and seals one deterministic scope per reviewer lens.
  Defect submissions require an in-scope path/line or mechanical check ID;
  model-supplied evidence levels are capped at observed static evidence.
- [x] Implement the manager-gated Learning Commit Protocol across Session,
  Context Graph, AI Memory, KB, and accepted task outcomes. The manager-only
  MCP operation verifies the canonical accepted request and its
  `fixed_and_verified` receipt before promotion. A task-database receipt/outbox
  records exact payload identity and per-authority projection state; retries
  resume only failed projections. Worker prose is never promoted implicitly,
  and accepted causal edges survive deterministic Context Graph rebuilds.
- [x] Add assurance-as-code checks for claim-to-evidence, public tool surfaces,
  policy projections, Source Graph freshness/retrieval goldens, and dormant
  quality capabilities. The repository-owned release-assurance manifest pins
  claim artifacts by SHA-256, checks public caveats and required MCP tools,
  validates non-empty retrieval cases and quality-policy IDs, and requires
  exact policy/freshness/negative-fixture/quality-adapter test selectors. CI
  and both release jobs run the fail-closed checker. This is static release
  assurance, not runtime or causal proof.
- [x] Expand the Source Graph retrieval golden corpus from three to ten checked
  cases and make recall@k, MRR, success@k, returned bytes, latency, and
  accepted-outcome coverage machine-readable. The evaluator bypasses compact
  cache receipts internally so repeated measurements still inspect complete
  ranked paths, while normal model-facing queries retain compact replay. The
  current checked corpus passes its structural quality minimums; accepted-
  outcome coverage remains zero and a vector candidate remains blocked on a
  matched A/B rather than assumed benefit.
- [ ] Normalize validation failure classes and measure diagnostic delta-rework
  against blind retry before enabling automation.
- [x] Produce a replayable release evidence pack, residual-risk register, and
  adapter/route parity matrix. The deterministic pack binds the existing
  release-assurance verdict, evidence-pinned open risks, and explicitly
  environment-scoped Linux/Windows route observations behind one content hash.
  CI and both release jobs replay the join fail-closed. This is not release
  approval, a risk waiver, or a cross-platform runtime guarantee.
- [x] Publish a bounded UltrafastSecp256k1 evidence-first case study. Every
  cited source is pinned to public commit `2d6de776` with a recorded SHA-256;
  the study explicitly excludes independent performance, adoption, causal, and
  external-audit claims. It is a reusable control-design reference, not an
  AIWorkHub benchmark or production-safety claim.
- [ ] Close route-local circuit isolation and measured
  cost-per-accepted-outcome advisory routing. Atomic callback/terminal
  persistence is locally complete: the terminal-review state/event and
  callback outbox/event now commit in one SQLite transaction, with rollback
  regression coverage for a callback-row insertion failure. The local
  advisory foundation now joins exact single-model, single-route usage cost
  to canonical manager decisions, preserves `UNKNOWN`/`UNMEASURED`, and never
  changes the selected worker. Live values are computed from the durable
  ledger rather than copied into this roadmap, so they cannot silently drift.
  They remain association evidence, not a causal savings claim. Shadow/canary
  activation stays open until an uncapped matched benchmark is partitioned by
  task family and persisted risk evidence.
- [ ] Validate manager-assisted task decomposition against matched sequential
  execution. The local preview foundation is implemented: it requires a
  canonical hash-verified Source Graph `impact`/`deps` receipt, validates a
  collision-free child DAG, returns a deterministic proposal digest, and
  performs no task creation or launch. Automatic spawning and throughput
  claims remain blocked on matched accepted-outcome evidence.
- [ ] Measure the session-level context delta protocol on repeated live worker
  queries. The local foundation now returns a hash reference for unchanged
  Session Manager state and a bounded row delta for changed state, scoped by
  exact task/request/repository/topic/limit/authority identity. Full canonical
  state and authenticated audit hashes remain durable, cache state is bounded
  and restart-safe, and deterministic tests cover unchanged, changed, and
  cross-request isolation. Provider-token, latency, cost, and quality claims
  remain blocked on a matched live benchmark.

Evidence policy: ratings and estimated percentages in review records are expert
judgment. Structural byte ratios are not provider-token savings; projected
quality, speed, or cost gains remain hypotheses until a checked benchmark
artifact establishes them.

## Current baseline (0.8.35)

### Shipped foundations

- Repository-local `.aiworkhub` identity and durable task authority.
- Native VS Code editor-tab dashboard and repository-scoped stdio MCP child.
- Explicit repository initialization and automatic incremental Source Graph
  lifecycle.
- Python structural indexing, conservative PHP structural indexing, and
  JavaScript/TypeScript file evidence.
- Isolated worker launch, dependency-aware task planning, review lifecycle,
  callback outbox, and coordinator acceptance boundary.
- HMAC-authenticated worker tool-use ledger and Source Graph telemetry v3:
  live calls, cached calls, missing/stale use, bytes returned, policy
  violations, tamper detection, and per-adapter summaries.
- Quality Evidence Engine foundations and evidence-aware task review.
- Visual Plan DAG, Review Inbox 2.0, repository Policy as Code, environment
  preflight and evidence-backed adaptive workforce scoring.
- Canonical manager/worker Session Manager, AI Memory and KB read/write
  surfaces with provenance, idempotency and audited soft lifecycle operations.
- Opt-in canonical Manager Context Graph foundation: manager-only append-only
  conversation events, deterministic rebuildable repository/thread/session/task
  relations, bounded search and exact transcript-range recovery. Manager
  Session writes are captured atomically when enabled; worker sessions are
  excluded.
- Passive Codex manager transcript capture from authoritative completed
  user/assistant items, with asynchronous storage, exact live route
  verification and deterministic deduplication. Internal reasoning, tools,
  commands, approvals and deltas are excluded.
- Storage observability, bounded system logs, authenticated all-tool usage
  statistics, and local dashboard views for task operations and context
  systems.
- Bounded repository KPI charts for explicit manager decisions, worker
  outcomes, validation failures, Source Graph use, callback delivery,
  adapter effectiveness and context-system execution, with visible sample
  windows, denominators and telemetry-quality disclosures. Source Graph
  statistics include authenticated workflow stages, modes, latency, time gaps,
  structural evidence rows and index generations; cached answers are invalidated
  when the canonical index generation changes.
- Safe repository worktree, terminal-run and extension-runtime retention with
  preview, quarantine, restore and separately confirmed expired purge.
- Repository-aware worker execution now defaults to the git-ignored and
  Source-Graph-excluded `.aiworkhub/runtime` boundary: isolated worktrees and
  request-private homes live under `runtime/worktrees`, validation scratch is
  probed under `runtime/validation`, and the retention/registration tools use
  the same root. Explicit external runtime/worktree/scratch overrides remain
  available. Arbitrary nested checkout paths and symlinked runtime boundaries
  fail closed, while the exact legacy temp layout remains eligible only for
  upgrade-time GC.
- Safe archived-task retention with age-based preview, callback-backlog
  protection, digest-bound quarantine, a seven-day undo window, collision-safe
  restore, separately confirmed purge and a surviving compact audit trail.
- First-party Claude CLI subscription preflight is distinct from VS Code /
  Copilot model consent; an expired Claude session is rejected before task
  claim without reading or copying credentials.
- Ubuntu, Windows, and macOS CI/release matrices plus VSIX and Python package
  generation.
- One canonical Python release-version authority with deterministic extension
  projections, tag/version CI checks, reproducible VSIX qualification and
  sorted release-asset SHA-256 checksums.

### Partial or not yet production-closed

- `core.py`, `process_launcher.py` and the extension entry point remain large
  authority modules and require characterization-first extraction.
- The extension remains JavaScript-first; its incremental TypeScript module
  migration and restored-tab/reload E2E boundary are still open.
- Shared pytest state isolation and test-suite organization are not complete.
  Correctness-critical Ruff coverage spans the Python tree and mypy gates the
  initial typed lifecycle kernel; broader typed coverage remains incremental.
- Quality Evidence Engine v1 now includes deterministic six-lens verdicts,
  negative calibration, independent read-only review and combined-tree
  validation. Shared-worktree registration attribution is exact-layout,
  digest-bound, explicitly confirmed and foreign-stale fail-closed.
- Quality activation is now change-sensitive at the risk-policy boundary:
  mutating code cards require validation, deterministic card/path/diff signals
  raise a monotonic risk floor, and the Claude worker contract includes the
  signed reviewer-submission tool. Mutation/revert-to-red probes and durable
  post-accept escape metrics remain open.
- The VS Code authenticated model broker is not fully qualified across every
  provider's first-party and Copilot authorization surface.
- Task/tool telemetry authenticates generic MCP calls as well as Source Graph
  calls. KPI v3 ships authenticated mode/workflow-stage attribution, bounded
  latency and inter-call-gap distributions, returned entity/edge/file evidence,
  index-generation attribution, an aggregate-only 1,000-run history,
  topic/tool-use cohorts and measured raw-path-versus-delivered-bundle byte economics.
  Durable daily rollups beyond retained process logs, normalized task classes,
  tokenizer-bound counterfactuals and accepted-change-per-dollar remain open.
- Worker prompt budgeting now records exact per-section byte accounting,
  distinguishes initial from residual rework packets, enforces a smaller
  rework ceiling, and persists only bounded review feedback plus immutable
  predecessor/residual references. Tokenizer-bound A/B outcome calibration
  remains open. Canonical usage now preserves provider/model/attempt identity,
  cached/cache-creation token populations and explicit cache/cost observation;
  provider pricing completeness and accepted-change-per-dollar remain open.
- Structure-aware bounded JSON previews preserve ranked symbols, related tests,
  risks, todos and recommended next steps under truncation. A provider-neutral
  token-ceiling kernel is being qualified; live supervisor termination and
  per-task policy/UI configuration remain open and must not be claimed from
  post-hoc usage alone.
- The brand foundation, concise public README and registry workflows are
  present. Registry-owner setup, public screenshots/GIF and long-form launch
  material remain open.
- Codex push callbacks currently include an optional compatibility adapter over
  a non-public App Server boundary. Manager inbox remains the portable fallback;
  each supported Codex release needs a compatibility qualification matrix.
- Context Graph does not yet claim complete cross-provider chat capture. The
  repository ledger/projection/retrieval runtime and Codex final-message
  adapter are shipped; supported, consented Claude/Copilot manager adapters
  remain an explicit follow-up and must not depend on scraping private plugin
  storage.

## P0 — Stable multi-repository product (shipped; continuously qualified)

### 1. Atomic chat repository handoff

Allow the same manager chat to work on repository A, explicitly switch to B,
and later return to A without reusing the wrong task store or callback route.

Required contract:

- Add bounded `repo_list`, `repo_current`, and manager-only `repo_switch`
  operations. Selection uses a verified `repo_id`; a model never supplies an
  arbitrary filesystem path.
- Bind authority to the complete tuple `(repo_id, window_id, provider,
  thread_id, claim_episode)`.
- Treat manager identity, MCP repository authority, callback route, Source
  Graph, Session Manager, AI Memory, and KB as one atomic handoff.
- Stop the old repository dispatcher/indexing lifecycle before activating the
  new one. Do not kill VS Code, Codex, Claude, another extension, or a foreign
  process.
- Keep terminal callbacks durable in their origin repository while it is
  inactive. Delivery ownership follows the repository's current verified
  manager: on a legitimate manager handoff, unfinished and review-ready work
  from an older thread is rebound to the new manager for that same repository.
  Preserve the originating thread as audit provenance, not as a permanent
  delivery destination. Never rebind or deliver a callback across repository
  boundaries.
- Fail closed on ambiguity, missing manifest, stale route, foreign window,
  nested-repository mismatch, or incomplete identity. Show the precise reason
  in the dashboard.

Release gate: two repositories and manager handoffs within each; switch A -> B
-> A, create and finish one task in each, then replace A's manager while one A
task remains active. Verify that its terminal callback reaches A's new verified
manager exactly once, its origin thread remains in audit provenance, and no A
callback reaches B. Also verify disjoint databases, Source Graph indexes,
sessions, memories, KB entries, and logs; zero cross-repository reads/writes.

### 2. Canonical context layer read/write closure

Complete manager/worker MCP APIs for:

- Session Manager: start, event, state update, checkpoint, handoff, close, and
  current-state read.
- AI Memory: remember, get/search/related, update, supersede, and safe archive.
- KB: upsert, bounded document ingest, get/search/related, supersede, and safe
  archive.

Every write requires repository/session/task/provider identity, provenance,
timestamp, idempotency key, duplicate detection, audit event, bounded payload,
and explicit manager/worker capability. Direct model writes to SQLite remain
forbidden. Fix dashboard memory schema migration and `OperationalError` before
release.

### 3. Callback and review lifecycle closure

- Every transition into review creates an outbox event, regardless of terminal
  substatus.
- On activation/reload/switch, reconcile review rows against the durable outbox
  and deliver missing callbacks exactly once.
- Show route tuple, backlog, oldest age, last delivery, retry count, transport,
  and degraded reason.
- Detect stale processing tasks and move them to review with truthful terminal
  disposition; never silently recycle them into pending.

### 4. Cross-platform and Remote-SSH qualification

The matrix must cover native Windows, Linux, macOS, WSL where applicable, and
Remote-SSH split-host behavior. Tests cover installation, Init Repo, Python
selection, MCP registration, dashboard reload, Source Graph first index,
repository handoff, worker launch, cancellation, review callback, cleanup, and
VSIX upgrade. Windows uses native process/IPC behavior; POSIX-only sockets and
paths must have a supported alternative or a truthful degraded mode.

## P1 — Source Graph economics and enforcement (0.8.0)

Source Graph is the core economic feature. The dashboard must show whether it
is used throughout the task, not merely injected at prompt start.

### Tool Use 2.0 dashboard

Report locally, by repository/task/run/model/adapter:

- Source Graph calls, successful hits, zero hits, cached hits, stale calls,
  returned bytes, bounded latency, per-call structural entity/edge/file evidence
  and index-generation attribution are shipped. Index refresh changes the query
  cache generation, so cached results cannot silently outlive their index.
- Calls by authenticated workflow stage (orientation, implementation,
  validation, review and rework) are shipped. A task with only the initial
  receipt is `injected-only`; KPI cohorts require at least two attributed
  stages before labeling a task `continuous_use`.
- Authenticated time gaps between Source Graph calls are shipped with bounded
  p50/p95 distributions and a bounded 15-minute informational long-gap alert.
  The UI explicitly labels an observed interval as non-proof of model
  inactivity. Model-turn correlation remains open.
- Raw discovery fallback count and reason (`unsupported`, `unindexed`, exact
  known path, or policy violation). Distinguish allowed bounded fallback from
  forbidden broad discovery.
- Session Manager, AI Memory, and KB usage in the same stage timeline.
- Acceptance outcome, retries, validation failures, elapsed time, model tokens,
  and cost beside tool use.

### Self-describing Source Graph contract

- Publish discovery, repository analytics and risk modes as MCP input enums
  instead of accepting an opaque string. The canonical list includes the six
  compact discovery modes plus tags/symbols, call/test/coverage maps,
  complexity/hotspots, history/ownership/review queue, bounded risk candidates
  and the composite pipeline packet.
- Invalid-mode responses include the bounded allowed-value list and one valid
  example; agents must never spend calls guessing the tool contract.
- Expose a small read-only capabilities response containing indexer families,
  supported file types, current generation, and bounded limits.
- Keep the schema identical on manager and worker surfaces so a validated
  workflow does not change meaning when delegated.

### Economic metrics

- `context_compression = 1 - graph_bundle_bytes / bounded_raw_context_estimate`
- `estimated_context_bytes_avoided = max(0, raw_estimate - graph_bundle_bytes)`
- tokenizer-specific estimated tokens avoided when the active model tokenizer
  is known; otherwise label the value as a byte estimate.
- cache reuse ratio, useful-hit rate, accepted tasks per million tokens,
  accepted change per dollar, retry cost, and cost of missing/stale graph use.
- compare cohorts (continuous-use vs injected-only vs fallback-heavy) only when
  sample sizes and task classes are shown. Do not claim causation from a raw
  correlation.
- Exact initial/rework prompt bytes, configured byte ceiling, utilization and
  contract/project-context/coordinator-context section breakdown are shipped.
  These remain byte measurements, never token-savings claims. Rework carries
  bounded feedback and residual identities against a hash-pinned retained
  predecessor workspace instead of replaying the previous task/result envelope.
- Canonical cost-ledger rows preserve every retry attempt and aggregate by
  provider and model. Cache-hit ratios use only rows where the provider
  actually exposed cache metrics; missing telemetry is unknown, never zero.

No prompt text, source content, credentials, or memory content enters these
statistics. Retention is bounded and repository-local.

### Continuous-use policy enforcement

- Worker tool wrappers record authenticated calls and raw-discovery attempts.
- Source Graph-required code tasks cannot pass review with only an injected
  receipt; they require task-stage live evidence proportional to the work.
- Broad grep/rg/find/tree is denied while the target is graph-supported.
- A bounded exact-target fallback is allowed only after an authenticated
  unsupported/unindexed result, and the reason is recorded.
- Enforcement is proportional: documentation-only or exact-file tasks do not
  manufacture irrelevant Source Graph calls.

Release gate: a fixed task suite demonstrates lower context bytes without lower
acceptance quality; dashboard numbers reconcile exactly with signed ledgers and
model usage records.

### Evidence instrumentation closure

**Closed in 0.8.96 and regression-verified for 0.8.99:** the 16-item
evidence/tool-economics instrument set is implemented. It includes
Source Graph recommendation roundtrips and retrieval precision, registry-driven
eval truth, attempt-bound provider usage with retention protection, generation
quality, compact replay, authenticated receipt conformance, pre-promotion review
evidence revalidation, generated instruction consistency, tool discipline,
session token decomposition, per-test suite profiling, runtime coverage import,
risk-mode precision, a quality no-net-growth ratchet and preregistered paired
prompt/bundle A/B evaluation.

Implementation closure is not evidence-population closure. Risk precision and
paired A/B stay `not_configured`/`inconclusive` until real adjudicated or matched
rows exist. Tool discipline is only an observational workforce tie-breaker, and
compact replay never converts bytes into provider tokens. The next economic
work is to collect unbiased populations through these shipped contracts rather
than add another measurement framework.

## P1 — Orchestration and evidence (0.8.x)

### Proven donor capability consolidation

Port repository-neutral capabilities from the UltrafastSecp256k1 engineering
toolkit without importing its product semantics or legacy databases. The
canonical disposition and sequencing live in
[`DONOR_CAPABILITY_PORT.md`](DONOR_CAPABILITY_PORT.md).

- Source Graph's polyglot adapters, six compact discovery modes and all
  repository-neutral donor analytics are shipped on one canonical database.
- Workspace Build Hygiene's cross-platform core is implemented: bounded
  external scratch slots, quota/reservation accounting, lease-safe cleanup,
  rogue in-repo build-tree detection and bounded preflight evidence. Dashboard
  preview/apply controls and task-launch allocation remain.
- Then add optional change-sensitive compiler/CUDA checks as Quality Evidence
  adapters, not core dependencies.
- Existing Task, Callback, Session, AI Memory, KB, DAG and collision
  authorities supersede donor scripts with equivalent responsibilities; no
  parallel database or duplicate lifecycle is permitted.

### Review Inbox 2.0

Pagination, task/model/status/time filters, bounded default payloads, recent
terminal summary, stale-processing recovery, bulk-safe review navigation, and
clear evidence completeness.

**0.8.0 closure:** the repository-bound inbox provides bounded pagination and
filters, recent terminal/callback summaries and a portable task-detail evidence
bundle. The bundle derives diff identity, tests, required outputs, artifacts,
numeric usage and approval history from canonical stores while excluding raw
logs and redacting host-local paths and obvious credential assignments.

### Visual DAG and planner

Display dependencies, blocked reasons, write collisions, critical path,
ready-to-launch work, and automatic launch after dependencies finish. The DAG
is advisory for planning but authoritative for launch readiness.

### Evidence bundles and code quality

Each accepted task receives a bounded bundle containing diff identity, allowed
writes, tests, quality checks, SARIF/CodeQL-compatible findings when available,
coverage, logs, artifacts, tool-use receipts, reviewer decision, and rollback
identity. `not_available` is never `passed`.

**Quality Gate 2.0:** evolve the existing engine without creating a parallel
review framework. The canonical contract is documented in
[`QUALITY_CONTROL.md`](QUALITY_CONTROL.md) and ADR 0003.

**Implemented foundation:** the existing engine now emits
`aiworkhub.quality_verdict.v2`. One pure fold owns verdict status across all six
lenses; model-supplied `PASS` fields are discarded. Monotonic risk resolution
raises task requirements from declared/observed signals but never lets a worker
lower them. Read-only reviewer reports use a bounded strict schema, missing
required reviewer/combined-tree evidence fails closed, and the dashboard shows
effective risk, verdict, refinement requirement and bounded lens status. The
canonical calibration matrix now covers every pure-verdict blocker family
with 3 reference positives and 23 targeted negatives. It reports expected
blocker observability, false-green/false-red rates and uncalibrated case IDs;
the same test is required in Linux, Windows, macOS and Remote-SSH CI.

Reviewer recovery preserves the same authority boundary. A blocked
`quality_review` card cannot use the generic rework transition or generic task
launch, because those paths do not carry the immutable target request/task and
packet body. The manager must launch a replacement through the dedicated
quality-review entry point, which rebuilds and verifies the packet against the
retained target before claim. This prevents an unbound read-only model response
from becoming apparent review evidence.

Manager acceptance now materializes a fresh `aiworkhub.combined_tree.v1`
workspace for medium-and-higher risk. It overlays the current canonical
tracked/untracked delta (including deletions) and then the exact retained
candidate, executes validations and the deterministic quality floor there,
and records bounded union evidence before promotion. High/critical profiles
also fail closed without an explicit manager approval bit.

Repository-declared checks may now state bounded repo-relative `paths` and a
monotonic `minimum_risk`. Exact changed-path evidence skips only demonstrably
irrelevant checks; absent selectors and absent delta evidence preserve the
historical conservative always-run behavior. Skips remain explicit evidence,
while every applicable declared check is still mandatory and fail-closed.

The diff-scoped Known Bug Scanner now emits the same bounded findings as native
JSON or SARIF 2.1.0. Every result remains explicitly a `static_candidate` with
`runtime_validated=false` until a targeted test or reproduction supplies that
stronger evidence. This adopts the useful validated-finding/CI handoff pattern
seen in agentic security tools without importing their runtime, code, or a
security-specific orchestration model into AIWorkHub.

The bounded [Strix capability review](STRIX_CAPABILITY_REVIEW.md) added two
repository-neutral refinements without importing its runtime: recoverable
unknown-tool guidance and stable deterministic root-cause fingerprints for
static findings. Agent graph, durable state, notes/todos, SARIF, usage ledgers,
sandboxing and progressive context already map to existing canonical AIWorkHub
authorities, so no parallel lifecycle or storage layer was added.

**Open P1 — task-progress truth:** provider stream activity and meaningful task
progress must be separate leases. Raw `provider_response` events prove only
transport liveness; they must not indefinitely postpone stalled-worker
reconciliation. A progress renewal requires a bounded semantic milestone,
tool/edit receipt, target-hash change, validation transition, or submitted
evidence. Regression coverage must preserve long-running provider liveness
while truthfully surfacing no-op execution and retaining its diagnostics.

- Add one pure deterministic verdict fold over six falsifiable lenses:
  correctness, does-it-run, test adequacy, security, code quality and
  requirements/scope. Models emit findings; no model computes PASS/FAIL.
- Add repository/task risk profiles. The universal floor always runs; medium,
  high and release profiles progressively require independent review,
  change-sensitive coverage, security checks, mutation/revert-to-red probes,
  platform evidence and explicit approval. A worker cannot lower its risk.
- Execute the existing read-only `quality_reviewer` contract with anti-anchored
  evidence packets. Prefer a different provider for high-risk correctness and
  security review; reviewer output is schema-checked and cannot mutate code.
- Add a combined-tree differential before final acceptance: validate the exact
  candidate together with accepted dependencies and concurrent canonical
  changes, then bind the resulting hashes to the acceptance evidence.
- Add a deterministic negative-gate benchmark: one known-good fixture and one
  deliberately broken fixture per blocking predicate. Track false-green,
  false-red, unavailable-evidence and post-accept escape rates.
- Expose lens status, proof source, reviewer disagreement and residual risk in
  Review Inbox 2.0. Missing required evidence fails closed; unavailable
  optional evidence remains visible and never masquerades as passed.

### Workforce catalog and adaptive scoring

Maintain user-configured available models and score them by verified outcomes:
task class, quality, validation pass rate, retry rate, latency, and cost. The
manager chooses the cheapest capable model and updates scores from evidence.
Token budgets are observable/advisory by default, not arbitrary hard caps
assigned before task difficulty is known.

Keep authentication surfaces truthful: VS Code/Copilot models use VS Code
authorization; first-party Claude subscription use remains a distinct adapter
and must never be mislabeled as Copilot Claude.

**0.8.0 closure:** InitRepo provisions a bounded repository-local catalog and
Manager MCP exposes audited upsert, evidence-backed inventory and explainable
ranking. Runtime scoring joins only same-repository canonical task/process
evidence (acceptance, review readiness, validation failure, retry, latency,
tokens and observed cost); absent evidence uses a labeled conservative prior.
Quota remains explicitly unavailable when the provider API does not expose it.
The native dashboard displays each route's exact readiness state, whether model
access was actually observed, outcome sample size and bounded manager score
adjustment. First-party Claude, Codex, DeepSeek VS Code LM and GLM VS Code LM
remain separate adapter identities.

### Environment preflight and policy as code

Before launch, verify runtime, model access, repository identity, Source Graph,
write scope, validation tools, and callback capability. Versioned repository
policy defines tool requirements, forbidden commands, validation, retention,
and quality gates.

**0.8.0 closure:** InitRepo provisions validated, non-executable repository
policy JSON; launch applies its adapter, Source Graph and required-check gates
before claim/start. A unified MCP/dashboard preflight reports repository,
policy, Source Graph, quality and adapter readiness while distinguishing an
installed executable from actually observed provider access. Retention policy
is bounded here and consumed by the separate storage-lifecycle workstream.

## P2 — Storage lifecycle, analytics, and integrations (0.9.x)

**Implemented foundation (0.8.0):** retained worker worktrees now have a
repository-scoped lifecycle backed by exact Git-common-dir ownership. The
dashboard shows `.aiworkhub` component sizes, policy age/size limits and a
read-only cleanup projection. A fresh digest plus explicit local-user gesture
moves only clean, fully-pushed, policy-aged worktrees into same-volume
quarantine; restore remains available for seven days and permanent purge is a
separate confirmation after expiry. Quarantine bytes remain counted as managed
storage and are never mislabeled reclaimed. Rotated logs, superseded Source
Graph generations and obsolete runtime packages remain the next adapters.
Stale Git registrations are separately inventoried and pruned only when every
candidate is attributable to the exact AIWorkHub worktree layout; any stale
foreign registration blocks the repository-wide Git prune operation.

**Locally validated runtime-root closure (2026-08-10):** repository-aware
launch no longer defaults to a global temporary directory. The canonical
default is `.aiworkhub/runtime/worktrees`, with executable validation scratch
under `.aiworkhub/runtime/validation`; Git and Source Graph already exclude the
entire `.aiworkhub` runtime boundary. Storage preview/quarantine/restore/purge
and registration scans resolve that same root. Explicit external overrides are
preserved, non-repository callers retain the historical temporary fallback,
and GC accepts only the exact old temporary layout during migration. This is a
local qualification statement until the next release is published.

- Ship inventory-only storage accounting first, then preview, quarantine,
  restore, and finally opt-in purge after cross-platform adversarial tests.
- Retain only bounded logs, terminal runs, obsolete Source Graph generations,
  and old runtime packages according to repository policy. Never delete active
  tasks, review/callback evidence, canonical context stores, credentials, or
  foreign files.
- Add local historical reliability analytics: completion/retry/failure rate,
  callback latency, model effectiveness, Source Graph economics, and storage
  growth.
- Add GitHub issue/PR synchronization, webhooks, and a public automation API
  only after local lifecycle and isolation are stable.
- Introduce task templates and a plugin/skill catalog after Policy as Code is
  production-closed.

## Maintainability program

This work follows the reliability gates above; it must not destabilize routing
or create a second implementation of a live subsystem.

- Split `core.py` by stable authority boundary (repository binding, manager
  identity, task lifecycle, callback lifecycle, and write policy) while
  retaining one public MCP contract.
- Split `process_launcher.py` into process supervision, review promotion,
  terminal truth, model adapters, and evidence verification. Preserve exact
  task/worktree identity through the extraction.
- Migrate the extension incrementally to TypeScript modules (`mcp-client`,
  `repository-route`, `dashboard-controller`, `model-broker`) behind the
  existing packaged entry point; no flag-day rewrite.
- Organize tests into unit/integration/e2e/platform groups with shared fixtures
  only where they do not hide repository identity. Keep old test paths through
  a bounded migration window so release scripts do not silently skip coverage.
- Add Ruff first for deterministic errors/import hygiene, then progressively
  type-check stable Python modules. Formatting/type adoption is staged and may
  not produce one unreviewable repository-wide rewrite.
- Keep generated caches, bytecode, build output, test worktrees, and old runtime
  generations outside Git and visible to the storage inventory/cleanup policy.

### Test and quality program

- Introduce shared test fixtures only for deterministic repository creation,
  environment restoration, and module-state cleanup. Never hide repo/window/
  thread identity behind a permissive fixture.
- Migrate the flat test suite incrementally into `unit`, `integration`, `e2e`,
  and `platform` groups. During migration, CI enumerates both old and new paths
  so moving a file cannot silently remove coverage.
- Replace fail-fast-only Python CI with a bounded multi-failure report after the
  known state-pollution failures are repaired. Add randomized-order runs as a
  scheduled qualification, not as an unseeded release gate.
- Add Ruff in narrow, deterministic stages (`F`, `E`, import hygiene first),
  then progressively type-check stable modules. Do not enable a repository-wide
  formatter or strict typing in one noisy rewrite.
- Add change-sensitive coverage reporting. Coverage is evidence, not a single
  arbitrary global percentage: critical routing, authority, callback, storage,
  and write-gate modules require branch coverage and behavioral E2E proof.
- Audit broad exception handlers on authority boundaries. A catch-all is valid
  only when it converts the exception into a bounded, observable degraded state
  and never reports success.
- Calibrate the quality gate itself with committed positive/negative fixtures;
  CI must prove known-good remains green and every targeted defect becomes the
  expected non-green verdict.
- Run dependency/concurrency union validations before acceptance so isolated
  green tasks cannot create a false-green integrated tree.

### Release and repository hygiene

- Keep one canonical release version and fail CI when Python package metadata,
  VS Code package metadata, runtime protocol compatibility, changelog, or VSIX
  filename drift. Generated formats may be synchronized by the release script;
  they must not guess the version dynamically at runtime.
- Maintain a current changelog entry for every published version and move old
  MVP finish notes into a clearly historical section.
- Classify tracked `data/` and `eval/` files as canonical fixture, reproducible
  evidence, or disposable runtime output. Keep only the first two in the main
  history; do not move authoritative task/review evidence to LFS merely to hide
  an unresolved retention policy.
- Review broad `activationEvents` only after callback/reload requirements are
  qualified. Narrow activation when possible, but never trade startup latency
  for missed callbacks or an unregistered MCP server.
- Provide a reproducible developer extra/environment, one command for Python
  qualification, one for extension qualification, and one release preflight.

### Architecture and documentation discipline

- Record repository routing, callback delivery, context-write authority,
  Source Graph enforcement, model authentication, and storage retention as
  Architecture Decision Records under `docs/adr/`.
- Generate a bounded MCP API reference from the actual registered tool schemas;
  documentation and runtime inventories must fail CI if they diverge.
- Add Mermaid diagrams for repository handoff, worker launch/review, callback
  delivery, and context persistence. Each diagram names authority boundaries
  and failure/degraded paths, not only the happy path.
- Every large extraction (`core.py`, `process_launcher.py`, extension modules)
  begins with characterization tests and ends with identical public contracts,
  runtime fingerprints, and platform behavior.

## Execution order and measurable outcomes

Work is accepted in this order; later polish cannot mask an earlier authority
failure:

1. **0.7.9 authority closure:** context viewers and read/write tools, atomic
   repository handoff, callback reconciliation, and exact route observability.
2. **0.8.0 economic proof:** continuous Source Graph enforcement and locally
   measured context/cost savings without reduced accepted-task quality.
3. **0.8.x orchestration quality:** Review Inbox 2.0, visual DAG, evidence
   bundles, adaptive model scoring, preflight, and Policy as Code.
4. **0.9.x lifecycle/productization:** safe retention, historical analytics,
   integrations, module extraction, documentation, SEO, and marketplace polish.
5. **1.0 qualification:** the complete clean-install/platform/isolation matrix
   below passes from packaged artifacts, not a source checkout.

Primary product KPIs are: accepted tasks per million tokens, accepted change
per dollar, first-pass validation rate, callback delivery latency/reliability,
cross-repository isolation failures (target: zero), Source Graph continuous-use
rate, raw-discovery violations, stale-task recovery time, and repository-local
storage growth. Activity counts alone are never reported as product progress.

## Assessment reconciliation

The 24 July evaluation and later model reviews are useful historical inputs,
not current authority. The current tree confirms cross-platform CI, extension
tests, canonical storage, and reduced tracked artifact bloat already exist, so
claims that these are wholly absent are rejected. The remaining valid findings
are retained above: large authority modules, flat/duplicated test setup,
fail-fast CI, missing staged lint/type gates, version/changelog drift risk,
incomplete context write lifecycle, opaque Source Graph query schema, and
insufficient public product documentation. Roadmap status changes only after
code, tests, and packaged-runtime evidence agree.

## 1.0 release gate

AIWorkHub 1.0 requires:

1. A clean install works in a new repository on Windows, Linux, macOS, and
   Remote-SSH without a source checkout or manual credential file.
2. A single chat can switch repositories and return with exact isolation and
   durable callbacks.
3. Source Graph auto-indexing and continuous-use enforcement work for the
   declared language support matrix.
4. Session Manager, AI Memory, and KB have complete canonical read/write,
   migration, audit, and retention lifecycles.
5. Review, dependency auto-launch, stale recovery, evidence bundles, and
   quality gates pass behavioral end-to-end tests.
6. No supported workflow requires browser UI, fixed localhost/LAN ports,
   repository source paths, or another plugin's private files.
7. Installation, upgrade, rollback, uninstall, and cleanup are documented and
   tested.
8. Security/isolation review has no unresolved P0/P1 finding.

## Brand and repository discovery

### Canonical message

Name: **AIWorkHub**

Short form: **AWH**

Tagline: **Repository-native orchestration for AI coding agents.**

Long description: **Coordinate multiple coding models in VS Code with
repository-local tasks, source intelligence, durable context, verified review,
and exact callback routing.**

### Repository presentation

- README hero with the canonical logo, one-sentence value proposition, current
  release/status badges, a 90-second workflow, dashboard screenshot/GIF, and a
  small architecture diagram.
- A truthful feature/status matrix that distinguishes shipped, preview, and
  planned capability.
- Dedicated pages for architecture, security/isolation, Source Graph economics,
  model adapters, cross-platform support, troubleshooting, contributing,
  changelog, and release policy.
- GitHub description and topics: `ai-agents`, `vscode-extension`, `mcp-server`,
  `multi-agent`, `developer-tools`, `source-code-graph`, `ai-memory`,
  `task-orchestration`, `local-first`, and `remote-ssh`.
- Marketplace metadata and screenshots use the same name, logo, tagline,
  feature claims, and supported-platform table.

### Article sequence

1. Why AI coding needs a repository-native control plane.
2. Source Graph economics: replacing repeated raw discovery with bounded
   structural context.
3. Reliable human-in-the-loop orchestration: review, evidence, and callbacks.
4. Multi-model workforce routing without sacrificing repository isolation.
5. Durable context without a cloud memory service: Session Manager, AI Memory,
   and KB.
6. Cross-platform lessons from VS Code, Remote-SSH, Windows process models,
   and local MCP.

Every public claim must link to a reproducible test, release artifact, or
measured dashboard field. Roadmap items are labeled as planned until their
release gate passes.
