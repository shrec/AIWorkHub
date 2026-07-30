# AIWorkHub Product Roadmap

Status: canonical product direction after the 0.7.8 repository, routing,
dashboard, CI, and tool-use audit. This roadmap separates shipped capability
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

## Current baseline (0.7.8)

### Shipped foundations

- Repository-local `.aiworkhub` identity and durable task authority.
- Native VS Code editor-tab dashboard and repository-scoped stdio MCP child.
- Explicit repository initialization and automatic incremental Source Graph
  lifecycle.
- Python structural indexing, conservative PHP structural indexing, and
  JavaScript/TypeScript file evidence.
- Isolated worker launch, dependency-aware task planning, review lifecycle,
  callback outbox, and coordinator acceptance boundary.
- HMAC-authenticated worker tool-use ledger and Source Graph telemetry v1:
  live calls, cached calls, missing/stale use, bytes returned, policy
  violations, tamper detection, and per-adapter summaries.
- Quality Evidence Engine foundations and evidence-aware task review.
- Storage observability, bounded system logs, and local dashboard views for
  task operations and context systems.
- Ubuntu, Windows, and macOS CI/release matrices plus VSIX and Python package
  generation.

### Partial or not yet production-closed

- A dashboard can select a repository, but an already-running Codex chat MCP
  process can retain its old cwd/repository. Manager route and MCP authority can
  therefore enter a split-brain state.
- Callback delivery is durable, but route convergence and post-switch/reload
  delivery still need a complete behavioral qualification matrix.
- Session Manager, AI Memory, and KB do not yet expose the complete canonical
  manager/worker read-write lifecycle required for durable context ownership.
- Source Graph telemetry proves calls, but does not yet quantify workflow-stage
  coverage, raw-discovery fallback, historical trends, or attributable context
  savings.
- Storage cleanup has a safety design, but deletion/quarantine execution is not
  a production feature.
- The VS Code authenticated model broker is only partially qualified across
  model families and authentication surfaces.
- Branding, GitHub discovery metadata, visual product explanation, and public
  documentation are below product-launch quality.

### Current unreleased implementation slice

- Dashboard context controls are now a single icon-based strip for Logs,
  Session Manager, AI Memory, and KB.
- Session/KB viewers use bounded, repository-registry-resolved read-only MCP
  calls; the Memory viewer accepts both fresh minimal and migrated schemas.
- Manager MCP exposes audited, idempotent, soft-delete-only writes for Session
  Manager, AI Memory, and KB; AI Memory now also has exact get and related
  reads on both manager and worker surfaces.
- The packaged stdio MCP schema exposes Source Graph `mode` and `bundle_type`
  enums, and invalid calls return allowed values plus a valid bounded example.
- Repo-neutral Codex MCP processes resolve an exact live thread route instead
  of trusting stale cwd, while explicitly repo-bound extension/worker children
  remain immutable. Bounded `repo_list`, `repo_current`, and manager-only
  `repo_switch(repo_id)` operations fail closed on ambiguous or foreign routes.
- Cooperative manager-inbox callback startup now rebinds pending same-repo
  events to the current verified manager and seeds missing review callbacks;
  original thread identity remains audit provenance.

These changes remain unreleased until the relevant extension, Python, packaged
runtime, reload, and repository-isolation gates pass together.

## P0 — Stable multi-repository product (0.7.9)

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
  returned bytes, entities/edges returned, latency, and index generation.
- Calls by workflow stage: orientation, implementation, validation, review,
  and rework. A task with only the initial receipt is `injected-only`, never
  `continuous-use`.
- Time and model-turn gaps between Source Graph calls; highlight long code-work
  intervals with no graph evidence.
- Raw discovery fallback count and reason (`unsupported`, `unindexed`, exact
  known path, or policy violation). Distinguish allowed bounded fallback from
  forbidden broad discovery.
- Session Manager, AI Memory, and KB usage in the same stage timeline.
- Acceptance outcome, retries, validation failures, elapsed time, model tokens,
  and cost beside tool use.

### Self-describing Source Graph contract

- Publish the supported query modes (`bundle`, `slice`, and `focus`) and bundle
  types as MCP input enums instead of accepting an opaque string.
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

## P1 — Orchestration and evidence (0.8.x)

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
