# Changelog

All notable changes to AIWorkHub are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project has
noted by package/extension version and release tag.

## [Unreleased]

## [0.9.4] - 2026-08-06

### Fixed

- VS Code LM bridge requests now carry an explicit `quality_review` kind, so
  packet-bound reviewers may submit their authenticated findings immediately
  without an unrelated mandatory Source Graph pre-turn.
- Both native tool-calling and text-envelope model protocols preserve the
  ordinary worker Source Graph gate while allowing the reviewer-only submit
  tool from the first turn.

## [0.9.3] - 2026-08-06

### Fixed

- Independent quality reviewers now skip the generic Session/Memory/KB
  project-context envelope and duplicate VS Code Source Graph prefetch because
  their exact hash-bound review packet is already the injected authority.
- Live, on-demand reviewer Source Graph access remains available against the
  candidate overlay while launch receipts no longer wait on unrelated context
  bootstrap work.

## [0.9.2] - 2026-08-06

### Fixed

- Independent reviewer lenses now reuse one bounded, hash-bound candidate
  packet instead of repeating target-event, task-card, hash and source-excerpt
  preparation for every lens.
- Exact candidate source excerpts are included in the packet delivered to the
  reviewer prompt, with path/hash identity, truncation metadata and bounded
  fail-closed reads.
- Focused regression coverage now protects reviewer packet delivery, reuse,
  identity/hash drift and truncation behavior.

## [0.9.1] - 2026-08-06

### Added

- VS Code LM requests now publish owner-private, request-bound monotonic
  progress receipts and stream their validated phase into supervisor evidence.
- Active workers now expose the last meaningful progress time and phase,
  distinct from the supervisor-owned heartbeat lease.

### Fixed

- Heartbeat-only workers that exceed the configurable meaningful-activity
  grace are finalized as `worker_stalled:no_meaningful_activity` instead of
  remaining indefinitely active; exact PID and process start ticks are
  reverified immediately before terminating the owned process tree.
- Stall callbacks and terminal evidence preserve idle duration, phase,
  progress sequence, output-byte counters and exact process identity.
- Review evidence now combines terminal validation with candidate-tree truth,
  and only the exact changed-paths-not-applicable skip is non-blocking.
- VS Code LM progress validation remains fail-closed for unsafe or mismatched
  sidecars while a missing receipt stays backward-compatible.

## [0.9.0] - 2026-08-05

### Fixed

- Source Graph refresh requests that collide with an active build are now
  coalesced into one follow-up generation instead of leaving readiness stale.
- VS Code LM edit envelopes can repair a missing or malformed final hash only
  from a trusted launch-time path contract, with action and line-range bounds
  enforced before the edit is accepted.
- Delta rework preserves explicitly inherited predecessor outputs as
  promotable changes while byte-identical placeholders remain fail-closed.
- Finalization records an explicit non-provider `finalizing` phase and timing
  evidence without overwriting a durable manager cancellation decision.
- Atomic runtime writes avoid redundant `chmod` calls when a newly-created
  file already has the required owner-only mode, preventing false failures in
  restricted validation sandboxes.
- The stdlib fallback MCP writer is regression-tested against locale-sensitive
  Windows stdout with Georgian text, the `→` character, exact ASCII framing
  and structured broken-pipe shutdown evidence.

## [0.8.99] - 2026-08-05

### Added

- Dependency-free MCP dispatchers now return bounded recovery guidance and up
  to three close registered names after a hallucinated tool call, without
  aliasing or executing the suggestion automatically.
- Known Bug Scanner findings now carry a line-movement-stable root-cause
  fingerprint and deterministic duplicate summary alongside the existing
  location-sensitive identity and static/runtime evidence boundary.
- Added a source-pinned Strix capability review documenting adopted,
  pre-existing, deferred and domain-specific concepts without importing code
  or creating parallel authorities.

## [0.8.98] - 2026-08-05

### Fixed

- Review evidence accepts the canonical `file:<mode>:<sha256>` token emitted
  by required-output manifests while continuing to reject malformed tokens or
  content drift before promotion.
- Manager contracts now make the verified AIWorkHub repository route
  authoritative over stale host cwd/workspace/environment hints and fail
  closed before inspecting a mismatched repository.
- README and public benchmark surfaces now expose the semantic-edit pilot's
  mismatched `20k`/`200k` token ceilings and forbid presenting its historical
  `27.5%` observation as a causal or product-savings claim.

## [0.8.97] - 2026-08-05

### Added

- The diff-scoped Known Bug Scanner can emit deterministic SARIF 2.1.0 for CI
  and code-scanning ingestion, including stable fingerprints and CWE metadata.
- Native and SARIF security findings explicitly distinguish a static source
  candidate from runtime-validated reproduction evidence.

## [0.8.96] - 2026-08-05

### Added

- The evidence-instrumentation matrix now has executable gates for eval
  artifacts, receipt conformance, review references, Source Graph retrieval,
  provider-instruction consistency, worker tool discipline, session usage,
  test-suite resources, runtime coverage, risk-mode precision, quality
  ratcheting and paired prompt/bundle experiments.
- Provider-usage receipts can be backfilled from retained raw streams without
  estimating missing usage. Terminal-log retention protects a run until an
  exact observed-or-unavailable capture receipt exists.
- Operations → Tool Use now shows compact-replay bytes, conformance failures
  and observational tool-discipline evidence while keeping byte ratios and
  provider-token claims separate.
- Runtime coverage can be previewed and write-gated into Source Graph, where
  coverage and test-oriented modes expose it with explicit missing-evidence
  semantics.

### Changed

- Review acceptance independently recomputes candidate hashes, sizes and
  required-output references before promotion; retained legacy receipts that
  stored the path set in `changed_path_hashes` remain verifiable.
- Provider instruction files and worker runtime guidance now derive from one
  canonical contract, including focused semantic edits and authenticated
  receipts.
- Workforce ranking uses observed tool discipline only as a non-causal
  tie-breaker after outcome and cost evidence.

### Fixed

- Storage telemetry remains available before the repository task store has
  been initialized, even with usage-aware terminal-log retention enabled.
- Evaluation PASS claims with zero eligible rows or inconsistent aggregates
  now fail closed through the registered artifact contract.

## [0.8.95] - 2026-08-05

### Added

- Source Graph now stores a generation-bound index-quality scorecard with
  resolved-edge ratio, cross-language bindings, artifact share, per-language
  density, database/freelist measurements and a bounded 100-generation trend.
- Every completed Source Graph refresh replays sampled `focus` guidance and
  candidate files through the production manager/worker MCP wrapper. Health
  reports the resolvability ratio and attributes misses to the wrapper or the
  engine/emitter layer.
- Operations → Tool Use displays recommendation resolvability and structural
  index-quality metrics without converting them into token or quality claims.

## [0.8.94] - 2026-08-05

### Changed

- Source Graph `slice` is now exact-symbol scoped: call evidence is selected
  by the resolved qualname instead of widening to every unrelated function in
  the containing file.
- Exact qualnames outrank incidental FTS/path matches, with explicit FTS column
  weights favoring symbol identity over signatures and filenames.
- `deps` now reports partitioned call/import/inheritance dependencies and
  dependents instead of retransmitting the byte-identical `trace` response.
- The checked noisy-file slice fixture records 100 legacy file-level edge rows
  (`21,921` bytes) versus one exact-symbol edge (`277` bytes), a 98.736%
  structural reduction. It explicitly makes no provider-token or quality claim.

### Fixed

- Exact body lookup is deterministic for duplicate short names and accepts the
  exact qualname emitted by Source Graph discovery.
- Conservative cross-file resolution no longer binds JavaScript/TypeScript,
  C/C++, Java, C#, Go or Rust lexical calls to same-named authorities from an
  incompatible language family.
- Focus TODO evidence now requires an observed comment marker, avoiding false
  work items from identifiers and ordinary string literals.

## [0.8.93] - 2026-08-05

### Changed

- VS Code-hosted workers receive their mandatory initial Source Graph result
  from a launcher-side, worker-scoped HMAC/audit call. This removes the shared
  coordinator MCP round-trip from concurrent bootstrap while preserving live
  MCP re-queries for implementation, validation and review.
- Cross-process launch locking now reserves only the exact task and capacity
  slot. Worktree creation, runtime provisioning, context construction and
  provider startup proceed independently instead of serializing unrelated
  workers behind one global lock.
- Unmeasured VS Code Language Model usage is persisted with
  `provider_api_usage_unavailable`, distinguishing an API limitation from a
  parser failure or fabricated zero token/cost usage.

### Fixed

- Concurrent Claude, DeepSeek and GLM editor-model workers no longer queue
  their initial Source Graph bootstrap on the coordinator's single MCP stdio
  transport.
- A bounded launch reservation prevents duplicate task starts before a PID
  exists and expires after a crashed provisioner, preserving concurrency and
  lifecycle truth.
- C++ exact-symbol body lookup now has an explicit manager/worker parity
  regression for `DBAccountStatus`-shaped source.

## [0.8.92] - 2026-08-05

### Fixed

- Incremental Source Graph stat caching now verifies the active extractor
  capability before skipping a file, so installing or restoring the optional
  Tree-sitter backend upgrades unchanged JavaScript/TypeScript files from
  lexical to semantic evidence on the next refresh.

## [0.8.91] - 2026-08-05

### Changed

- Repeated worker Source Graph calls return a SHA-bound cache receipt when it
  is smaller than retransmitting the prior content; small results retain their
  full payload when that is cheaper.
- New repository Source Graph policies exclude generated JSON/JSONL
  measurement artifacts under `eval/` while keeping JSON/XML languages and
  ordinary configuration data enabled and user-configurable.
- Focus results represent hot symbols as compact references to their canonical
  ranked rows instead of repeating full symbol evidence.
- Incremental Source Graph refreshes persist file size and nanosecond mtime,
  skip AST extraction for unchanged files, and avoid cross-file edge relinking
  when no indexed file changed or disappeared.

### Fixed

- Worker `slice` accepts the exact qualname targets emitted by
  `recommended_next_steps` instead of treating them as file-prefix filters.
- Manager project-context queries preserve the declared query and execute the
  requested Source Graph mode instead of silently substituting the first
  target or falling back to `focus`.
- Source Graph byte fitting preserves query/target receipt metadata, trims
  optional evidence in convergent chunks, and no longer loops on a one-item
  list.
- Truncated symbol previews keep semantic identity and ranking fields ahead of
  alphabetic noise.
- Empty context-optimization evidence is reported as `INCONCLUSIVE`; its test
  writes only to an isolated temporary directory and can no longer overwrite
  the tracked benchmark artifacts with a vacuous `PASS`.

## [0.8.90] - 2026-08-05

### Changed

- Read-only research and quality-review acceptance uses a request-scoped lock
  instead of waiting behind unrelated canonical file promotions.
- VS Code LM Source Graph calls carry an explicit workflow stage from the
  initial request through private tool calls, cache identity and receipts.
- `worker_failed` remains distinct from `launch_failed` in callback and UI
  state, preserving whether a provider failed before or after worker start.

### Fixed

- Semantic edit can fill a declared zero-byte required-output placeholder at
  its single hash-bound virtual `1:1` insertion point without relaxing any
  other line-range, path, scope or stale-hash gate.
- Exact worker Source Graph body lookup can recover a symbol from another
  coordinator-declared target and handles Windows path casing without
  broadening access beyond the immutable task scope.
- `task_mark_done` is idempotent after `agent_accept_review` already promoted
  and finished the exact candidate.
- Windows reconciliation watches a live supervisor even when the OS cannot
  provide start ticks, rather than prematurely emitting a finalizer failure;
  destructive PID actions remain start-time verified.
- Dashboard task liveness uses the newest request attempt and no longer lets
  an older failed retry overwrite a current successful or running attempt.

## [0.8.89] - 2026-08-05

### Changed

- Source Graph refreshes parse files before opening their bounded write
  transaction and defer exclusive `VACUUM` maintenance while the live
  generation is serving manager and worker queries.
- Task-created project context now prioritizes declared files and concrete
  code entities over generic project/provider words.
- Benchmark documentation uses the durable `Evidence matrix` heading instead
  of presenting an old release number as the current product version.

### Fixed

- Preflight hydrates the committed Source Graph generation during indexing,
  standby and restart states, preserving truthful readability during a
  transient SQLite health-probe lock.
- Optional project context converts SQLite/query failures into bounded
  degraded evidence instead of aborting unrelated worker launches.
- Successful Windows VS Code LM workers with no validation commands no longer
  fail finalization by resolving an unavailable native AppContainer sandbox.
- Generic `task_mark_done` rejects failed terminal reviews and cannot bypass
  the isolated candidate revalidation/promotion path owned by
  `agent_accept_review`.
- VS Code LM failures now preserve the failing phase, bounded cause, initial
  Source Graph request and MCP timeout identity instead of returning empty
  diagnostics.
- Policy-warning telemetry remains consistent for observation-only tasks, and
  dashboard usage renders unavailable provider counters as unavailable rather
  than as measured zero.

## [0.8.88] - 2026-08-05

### Added

- Claude Code direct chats receive an explicit manager startup contract through
  both the managed `CLAUDE.md` block and MCP bootstrap/tool descriptions. New
  chats must bootstrap AIWorkHub and use manager Source Graph discovery before
  broad built-in filesystem tools, then re-query as the working boundary
  changes.
- Durable usage evidence distinguishes worker and reviewer activity and exposes
  retry economics without presenting historical role inference as directly
  observed fact.
- A checked retry/role observation artifact and CI verifier preserve the
  evidence behind the public benchmark narrative.

### Changed

- Unknown provider cost remains unknown during workforce routing. Candidates
  are no longer assigned a fabricated `$9,900` estimate, and mixed
  known/unknown usage cannot dilute an observed effective token price.
- The token-economy audit now separates verified provider accounting from
  unmeasured tokenizer, cache and compaction hypotheses and requires controlled
  benchmarks before public savings claims.

### Fixed

- Repository re-initialization refreshes Claude's managed AIWorkHub policy
  while preserving owner-authored text outside the managed markers.
- Usage timestamps survive canonical ledger normalization, keeping attempt
  order and model-to-manager-outcome association stable.

## [0.8.87] - 2026-08-04

### Added

- Durable usage rows now preserve requested and observed model identities,
  visible output, reasoning output and cache-write telemetry. The cost ledger
  exposes an association-only model-by-manager-outcome matrix using the latest
  usage attempt at or before the manager decision.
- Validation receipts now retain the exact declared command/argv beside the
  normalized argv that actually executed, including an explicit rewrite flag.
- A checked 65-run provider-routing observation documents near-saturated
  provider caching and identifies model routing as the next measurable cost
  lever without presenting the $20.83 counterfactual as realized savings.

### Changed

- No-write/no-output tasks must declare `read_only: true`; an empty write scope
  is no longer treated as implicit read-only intent. This prevents accidental
  code cards from consuming provider tokens without promotable outputs.

### Fixed

- Codex `reasoning_output_tokens` are included in billed output and total-token
  accounting instead of being silently omitted. `cache_write_input_tokens` is
  recognized and normalized into durable cache-creation evidence.

## [0.8.86] - 2026-08-04

### Changed

- Manager and task-creation contracts now state that tasks are uncapped by
  default and prohibit inferred or automatically assigned token ceilings.
  Explicit owner- or repository-policy budgets remain available, while normal
  efficiency work targets focused context, bounded reads, minimal edits,
  retries, and validation rather than truncating useful work.

### Fixed

- The semantic-edit pilot ledger now exposes its historical explicit token
  caps and the `20k` versus `200k` first-pair mismatch. The benchmark checker
  rejects hidden cap-policy drift and prevents capped evidence from being
  presented as a natural uncapped result.

## [0.8.85] - 2026-08-04

### Fixed

- Successful no-write/no-output code inspections now use the authenticated
  read-only result lifecycle instead of failing terminal reconciliation with
  `no_effect`.
- Read-only code tasks preserve and revalidate a satisfied worker MCP gate at
  manager acceptance; an unsatisfied required Source Graph/tool receipt still
  fails closed.

## [0.8.84] - 2026-08-04

### Fixed

- Live Claude token ceilings now sum completed per-turn `message_delta`
  usage until the terminal request aggregate arrives. Multi-turn workers can
  no longer evade a request-wide cap merely because every individual turn is
  below it.
- POSIX validation normalizes the portable bare `python` spelling to
  `python3`, avoiding false `rc=126` failures on hosts without `/bin/python`.
- Explicit no-write/no-output cards can launch regardless of task type, so
  evidence-only audits no longer require dummy repository artifacts.

## [0.8.83] - 2026-08-04

### Changed

- Provider-reported usage now passes through one bounded recursive normalizer
  shared by live token-budget enforcement and durable process accounting.
  Nested Claude, Codex and OpenAI-compatible usage/cache fields are interpreted
  consistently, while missing telemetry remains unknown rather than a false
  zero.
- Durable process evidence retains the requested model, an observed provider
  model identity when emitted, and up to 64 ordered provider usage snapshots;
  snapshots are explicitly labeled as provider-reported rather than assumed
  deltas.
- Worker prompts place 3,014 bytes of invariant runtime/tool policy before the
  first task-specific byte, making that prefix cacheable across tasks. No cache
  or token savings are claimed until fresh provider telemetry measures them.

### Fixed

- Live token caps and terminal cost accounting no longer use divergent usage
  parsers, preventing nested stream events from being enforceable live but
  absent from the canonical ledger (or vice versa).

## [0.8.82] - 2026-08-04

### Changed

- Project Context Bundle v2 embeds canonical JSON evidence as nested objects
  instead of escaped JSON strings, removes card-duplicated mode/section
  wrapper fields, and uses compact outer serialization. The exact same
  representative evidence fixture shrank from 849 to 600 bytes (29.329%);
  this is deterministic structural byte evidence, not a token-savings claim.
- Context-economics telemetry now records the per-task legacy-v1 versus
  nested-v2 byte counterfactual while preserving token and cost fields as
  unknown unless the provider reports them.
- The system-benefit checker now cross-validates the public semantic-edit
  pilot deltas against its separate pair-level machine-readable ledger.

### Fixed

- Windows worker launch no longer recurses before supervisor spawn when an
  existing terminal-authority key has ACL-backed Windows permissions that do
  not round-trip as POSIX `0600`; create races are now bounded and invalid
  keys fail closed with structured launch-phase diagnostics.
- Completion Inbox adapter launchability now consumes the same route-aware
  preflight authority as the Preflight card, removing contradictory native
  CLI readiness on Windows.
- Worker context receipts count delivered evidence correctly for both legacy
  v1 section lists and v2 evidence maps.
- Benchmark documentation now uses an evidence-snapshot label and clarifies
  the provider-trace/read-operation denominators.

## [0.8.81] - 2026-08-04

### Added

- Added a machine-checked paired semantic-edit pilot ledger with exact task and
  request identities, provider token/time observations, contrary uncached-
  input evidence and an enforced `public_claim_eligible=false` status while
  the sample remains small, non-randomized and cache-confounded.
- Added a public benchmark page and documentation that distinguish structural
  byte ratios from token, cost, latency and accepted-quality measurements.
- Added a Product Hunt launch pack plus three canonical 1270x760 gallery
  compositions using the existing AIWorkHub brand assets.
- Added benchmark-evidence recomputation to the static CI quality job.
- Added a machine-checked full-system benefit snapshot covering Source Graph
  enforcement/latency, tool-use cohorts, read behavior, signed context
  expansion, semantic-edit shape, callback durability, task outcomes and
  incomplete cost coverage. The public comparison page now separates
  AIWorkHub's integrated control-plane differentiation from the documented
  strengths of Graphify, Serena, Aider and Cline.

## [0.8.80] - 2026-08-04

### Added

- Operations KPI analytics now retain authenticated, path-free semantic-edit
  receipts and visualize focused-edit runs, edited ranges, source-file bytes,
  selected old-region bytes, replacement bytes and model-reemitted old bytes.
- Added an explicit structural replacement/file byte ratio and bytes-not-
  reemitted counter. These measurements are labeled as byte-shape evidence and
  never presented as token, cost, speed or quality savings without a paired
  provider baseline.

## [0.8.79] - 2026-08-04

### Fixed

- Terminal semantic-edit telemetry now consumes authenticated CLI
  `semantic_edit_apply` receipts as well as VS Code LM response metrics, while
  exporting only bounded byte counts and never replacement text, paths, hashes
  or idempotency keys.
- Read-efficiency telemetry now recognizes the exact side-effect-free
  `wc -l <path> && sed -n <range> <same-path>` shape emitted by Codex and
  excludes the line-count prefix from measured file bytes. Other compound
  shell commands remain deliberately unclassified.

## [0.8.78] - 2026-08-04

### Fixed

- Normalized the text-only VS Code LM semantic-edit prepare request so the
  final-envelope `path` alias cannot turn a valid repository-relative target
  into `semantic_edit_path_invalid`. The worker prompt now shows the exact
  `file_path` tool input shape, while all existing scope and hash checks remain
  fail-closed.

## [0.8.77] - 2026-08-04

### Added

- Added a replacement-only semantic edit protocol for existing files. Source
  Graph-selected line ranges are bound to full-file and fragment hashes;
  workers return only new code and a deterministic local Python applier enforces
  scope, freshness, overlap, symlink and atomic-write guards.
- Added byte-level semantic-edit receipts to terminal evidence. They distinguish
  full file size, selected old region and model replacement output while
  explicitly making no token-savings claim; provider-token A/B measurement
  remains the authority for economy claims.

### Changed

- VS Code LM workers now request `semantic_edit_response.v3` by default while
  retaining v1/v2 parsing as compatibility fallbacks. Existing files no longer
  require old-code echo or complete-file regeneration in the normal path.

## [0.8.76] - 2026-08-04

### Fixed

- Corrected Context economics population names: the existing baseline is
  pre-optimization tool-section payload, not raw repository files or a
  counterfactual model read. Dashboard provider economics no longer feeds
  that population into the naive-discovery compression ratio.
- Split context delivery into optional-section suppression, serialization
  envelope overhead and signed net delivery delta. Source-selection and token
  savings remain explicitly unavailable until a controlled raw-file A/B
  counterfactual exists.

## [0.8.75] - 2026-08-04

### Fixed

- Replaced one-sided Context compression accounting with a signed net byte
  delta. Mixed samples now subtract bundle expansion from gross compression,
  and Operations renders expansion explicitly instead of reporting false
  bytes avoided. The metric remains a deterministic declared-byte comparison,
  never a token-savings claim.

## [0.8.74] - 2026-08-04

### Fixed

- Versioned the corrected provider read-efficiency measurement and exclude
  incompatible legacy summaries from current KPI totals while reporting their
  count explicitly; historical false rows can no longer pollute the corrected
  dashboard.

## [0.8.73] - 2026-08-04

### Fixed

- Deduplicated Codex `item.started`/`item.completed` command pairs so a single
  bounded file read is no longer counted twice or misclassified as an unknown
  repetition. The regression is covered by a real provider-event-shaped test.
- Stopped instructing workers to repeat fresh, non-degraded Session Manager,
  AI Memory and KB queries already authenticated in their injected context.
  Live re-query remains available for absent/degraded sections or new facts,
  removing ceremonial tool cycles without weakening the Source Graph code gate.

### Added

- Added truthful read-efficiency visuals to Operations KPIs: provider trace
  coverage, bounded/unbounded reads, exact/overlapping rereads, observed bytes
  and per-adapter evidence coverage. The UI explicitly labels these as
  provider event/byte measurements, never inferred token or savings claims.

## [0.8.72] - 2026-08-04

### Added

- Connected the previously standalone read-efficiency analyzer to canonical
  worker finalization. High-confidence Claude/Codex read events now produce a
  path-free process summary with bounded/unbounded reads, exact/overlapping
  rereads, observed response bytes and Source Graph correlation. Missing
  provider evidence remains explicitly unobserved rather than a false zero.
- Added repository/dashboard aggregation by adapter and worker instructions
  that prefer Source Graph body/file previews plus bounded, non-repeated exact
  reads. Measurements explicitly make no token or cost-savings claim.

## [0.8.71] - 2026-08-04

### Fixed

- Made Source Graph's advertised low-token workflow truthful: `focus` and
  `slice` responses now use an 8 KiB content ceiling, analysis modes use a
  12 KiB ceiling and content-rich modes retain 16 KiB. Truncation remains
  structure-aware, exposes its applied cap and preserves full pre-truncation
  hit/evidence counts for telemetry and reproducible benchmarks.

## [0.8.70] - 2026-08-04

### Fixed

- Exposed the existing fail-closed required-output exception contracts through
  canonical MCP task creation. Managers can now explicitly declare valid
  unchanged or deliberately empty required files; both lists are validated,
  persisted and included in idempotent create reconciliation instead of
  forcing clean-root successor tasks.

## [0.8.69] - 2026-08-04

### Fixed

- Bounded the manager-facing cost ledger to provider, model and day summaries
  by default. Per-runner/per-topic maps and raw task rows remain independently
  available through explicit `full=true` and `include_tasks=true` requests.

## [0.8.68] - 2026-08-04

### Fixed

- Made the manager-facing Plan-DAG snapshot actionable and bounded by default.
  Ready work, live blockers, collisions, orphaned processing and DAG validity
  remain visible, while repeated finished-card lifecycle and dependency maps
  move behind explicit `full=true` inspection.

## [0.8.67] - 2026-08-04

### Fixed

- Made the manager-facing dashboard snapshot bounded by default while the
  native Webview explicitly requests the unchanged full shape. Model calls no
  longer pull task rows, process evidence, ledgers, workforce history and KPI
  analytics when only health, queue counts, warnings and route truth are
  needed.

## [0.8.66] - 2026-08-04

### Fixed

- Replaced Completion Inbox's embedded full process-event payloads with
  bounded operational summaries. Manager polling keeps lifecycle, error and
  measured-usage facts while omitting repeated project-context bundles,
  receipts, validation arrays and other evidence available through exact
  process inspection.

## [0.8.65] - 2026-08-04

### Fixed

- Unified the completion-inbox compatibility readiness view with canonical
  Claude live-auth evidence. A provider-observed 401/403 can no longer appear
  blocked in Environment Preflight but launchable in Completion Inbox.

## [0.8.64] - 2026-08-04

### Fixed

- Preserved the bounded Claude live-authentication circuit across MCP and
  extension reloads using owner-only, non-secret metadata. A recent
  authoritative 401/403 now continues to block stale `auth status` readiness
  without storing the executable path, OAuth token, or provider credentials.

## [0.8.63] - 2026-08-04

### Fixed

- Excluded the first-party `claude-code` extension's internal model entries
  from the background VS Code LM worker broker after repeated text-first and
  stream-first live canaries proved they return no public response parts.
  Copilot-hosted Claude remains a separate explicit editor route; first-party
  Claude subscription workers remain bound to `claude_cli`.

## [0.8.62] - 2026-08-04

### Fixed

- Read VS Code language-model responses from the authoritative typed
  `response.stream` before the derived text-only view. This preserves
  provider text/tool parts that `response.text` may filter and consume, while
  retaining a bounded compatibility fallback for legacy responses that omit
  the typed stream.

## [0.8.61] - 2026-08-04

### Fixed

- Added bounded, content-free VS Code LM response-part diagnostics so an
  actually empty contributed provider stream is no longer confused with an
  unsupported JSON event shape.
- Trip a short Claude authentication circuit breaker after an authoritative
  live 401/403, preventing stale `claude auth status` cache entries from
  advertising the expired subscription route as ready and repeatedly burning
  failed task launches.
- Kept first-party Claude subscription workers on `claude_cli`; they no longer
  fall back silently to the separate VS Code/Copilot authorization and billing
  surface when the subscription CLI is unavailable.

## [0.8.60] - 2026-08-04

### Fixed

- Made the VS Code LM text bridge fall back to `response.stream` when a
  contributed provider exposes an iterable `response.text` channel but emits
  no content through it, preventing false empty-response finalization loops.
- Rejected VS Code/Codium launchers at the Claude subscription preflight
  boundary so a stale executable override can never run `code auth status`
  and repeatedly open empty `auth`/`status` editor buffers.

## [0.8.59] - 2026-08-04

### Added

- Added an optional `source-graph-semantic` backend for parser-backed
  JavaScript/TypeScript declarations, imports, inheritance and calls while
  retaining the dependency-free lexical fallback and truthful capability
  receipts.
- Added cross-platform lock, topic-grammar and large-tree semantic regression
  coverage, including the semantic extra on Linux, Windows and macOS CI.

### Fixed

- Split long worker finalization and canonical review promotion away from the
  short global launch registry lock, preventing unrelated Windows launches
  from timing out behind completed-worker reconciliation or review work.
- Unified task-create and launch topic identity grammar so valid dotted,
  dashed and colon-delimited topics remain launchable.
- Made preflight sandbox telemetry route-aware, separating native CLI sandbox
  capability from safe in-process VS Code LM routes.
- Corrected cost-ledger duplicate aggregation, provider cache accounting and
  Source Graph freshness denominators without presenting unknown usage as
  zero cost or zero tokens.
- Improved Source Graph query normalization, exact phrase/identifier handling
  and cross-file JavaScript/TypeScript import resolution; large native parse
  trees now derive line numbers from stable byte offsets instead of unstable
  parser point accessors.

## [0.8.58] - 2026-08-04

### Fixed

- Rejected descriptive prose and out-of-scope patterns in `required_outputs`
  before launching a provider, with explicit guidance to place human-readable
  outcome requirements in `acceptance`.
- Retried completed-worker reconciliation across transient filesystem or
  SQLite races and converted exhausted finalizer failures into a durable,
  callback-emitting `finalize_failed` state instead of leaving tasks stranded
  in `processing`.
- Made `finalize_failed` a retryable operational terminal outcome while
  retaining the isolated workspace for diagnosis.

## [0.8.57] - 2026-08-04

### Fixed

- Retained every completed provider attempt in the canonical usage ledger even
  when VS Code LM exposes no token or price counters, while reporting missing
  measurements as `unknown` instead of fabricated zero-token or zero-cost
  values.
- Added observed-versus-unknown usage counters to usage reports, cost-ledger
  aggregates, and compact dashboard process telemetry without changing the
  existing measured-token accounting path.

## [0.8.56] - 2026-08-04

### Fixed

- Added a repository-confined, byte-bounded source preview to exact Source
  Graph `file` results, allowing constant-only and file-level authorities to
  be read without repeated zero-hit symbol-body queries or unbounded reads.

## [0.8.55] - 2026-08-04

### Fixed

- Reported injected project-context acknowledgement from the actual receipt
  check even for evidence-only tasks that do not require the worker MCP gate.
- Preserved observational Source Graph/tool-use telemetry for ungated research
  tasks and kept unobserved provider cost explicitly unknown instead of
  presenting a fabricated zero-dollar measurement.
- Made Source Graph `file` mode honor an indexed exact target even when the
  query is a semantic description, while retaining query-path fallback for
  directory-scoped requests.

## [0.8.54] - 2026-08-04

### Added

- Added an exact operational-terminal retry flow that preserves task identity,
  prior evidence and claim history while requiring the manager to name the
  matching request and terminal substatus.
- Added a deterministic worker read-efficiency analyzer for measuring bounded
  versus unbounded reads, repeated file reads and estimated input waste without
  inventing token savings.
- Added hash-pinned VS Code LM source edits with bounded mismatch diagnostics so
  stale replacements fail closed without retaining raw model output.

### Fixed

- Stopped runaway workers after 8 MiB of combined stdout/stderr, retained exact
  byte evidence without labelling it token truth, and propagated the distinct
  `output_budget_exceeded` state through task storage, callbacks and retry.
- Bounded live dashboard output to cursor-based 8 KiB chunks and exposed
  explicitly retryable operational blockers in Plan-DAG telemetry.
- Prevented isolated workers from spending time installing or unpacking missing
  validation dependencies; canonical validation remains a supervisor concern.
- Accepted authenticated evidence-only reviews with no write scope, retained
  code-task residual rework contracts, and preserved terminal callback truth.

## [0.8.53] - 2026-08-03

### Fixed

- Replaced the VS Code LM bridge's global single-flight worker queue with a
  bounded three-request scheduler, so independent editor-model tasks no longer
  consume their execution deadlines while waiting behind another provider
  call.
- Pinned every in-flight editor-model request to the repository identity under
  which it was atomically claimed and cancel active provider calls on bridge
  stop, preventing repository switches or reloads from contaminating response
  routing.
- Published active and maximum editor-model request counts in the bridge
  heartbeat for truthful capacity diagnostics.

## [0.8.52] - 2026-08-03

### Fixed

- Rejected validation commands at task creation when the worker's own
  fail-closed parser cannot execute them, returning the failing command index
  and safe checked-in-script examples before any provider tokens are spent.
- Classified the VS Code LM bridge's structured response-deadline event as
  `timed_out` instead of the generic `worker_failed`, preserving truthful
  callback, KPI and retry evidence.
- Routed Ruff's disposable cache into each request's writable validation
  scratch directory so read-only worker worktrees no longer produce false
  permission failures.

## [0.8.51] - 2026-08-03

### Fixed

- Excluded VS Code's internal `copilot-utility*` picker entries from worker
  model selection and ranked concrete model IDs ahead of mutable display
  names, preventing `Unknown tokenizer: undefined` failures for DeepSeek V4
  Flash when its real editor model is available.
- Counted only pending, processing and review tasks as active in Plan-DAG
  telemetry; terminal blocked tasks no longer consume active capacity or
  appear as the current critical path.

## [0.8.50] - 2026-08-03

### Added

- Wired provider-reported input, output, cache and cost observations into the
  bounded Context Economics KPI surface, including provider cache-hit and
  cost-per-review-ready measurements without fabricating token savings.
- Allowed declared quality commands to normalize bounded SARIF, JUnit XML,
  coverage JSON, benchmark JSON and AI-finding report artifacts into the
  canonical completion verdict.

### Fixed

- Delivered `timed_out`, `worker_failed`, cancellation and token-budget
  terminal callbacks while their canonical task remains in the blocked
  lifecycle bucket, instead of incorrectly superseding the durable wake-up.
- Recovered one matching callback that an older eligibility check incorrectly
  superseded when the verified manager route reloads.
- Preserved the hash-pinned predecessor candidate after a rework successor
  fails or times out, and retained explicit terminal failure reasons instead
  of empty error evidence.

- Preserved launch-time project-context evidence when a later terminal event
  adds provider usage, instead of replacing the whole per-request telemetry
  record.
- Exposed AI Memory exact-get and related-record tools to Claude workers, in
  parity with the registered worker MCP surface.
- Enforced the repository's `session_memory_kb_required_for_nontrivial` policy
  switch in the completion gate and failed closed on malformed policy state.
- Labeled absent provider usage explicitly as `telemetry_unavailable` in
  token-budget supervisor evidence.
- Reported `record_launch_blocker` write-gate denials with the exact
  `launch-blocked` command instead of the underlying claim authority name.
- Made the cross-plugin snapshot regression fail clearly when either function
  marker is missing or reordered, rather than slicing from an invalid index.

## [0.8.49] - 2026-08-03

### Added

- Wired the provider-neutral token-budget kernel into the detached worker
  supervisor. Tasks may set an explicit `max_live_tokens`; structured usage
  observed while the provider is running is enforced immediately, while
  terminal-only telemetry is truthfully retained as posthoc-only evidence.

### Fixed

- Routed VS Code LM adapters through their editor-host execution boundary on
  every platform, while retaining AppContainer/OS sandbox enforcement for
  native CLI adapters.
- Persisted explicit per-model editor consent before the provider turn and
  isolated broker/snapshot failures from the manager MCP recovery circuit.
- Hydrated Source Graph daemon/preflight truth from the canonical readable
  generation and added exact file provenance to context entities and edges.
- Recorded pre-claim launch failures as retryable operational blockers without
  fabricating processing/review states, and clarified that auto-pickup is
  optional while launch is the required worker-start operation.
- Moved timeout, cancellation and worker-crash outcomes out of the actionable
  review queue while preserving callbacks, worktree evidence and original
  validation/output denominators.
- Retained structured evidence for every failed validation command, including
  return code, duration, bounded stream heads/tails and truncation markers.
- Reported native authenticated/credential-backed routes independently from
  editor consent telemetry, preventing healthy CLI adapters from appearing
  access-unavailable.
- Drained provider pipes as available chunks instead of waiting for a 64 KiB
  read or EOF, restoring genuinely live output and usage telemetry.

## [0.8.48] - 2026-08-03

### Added

- Added a provider-neutral token-budget kernel with authoritative-live,
  posthoc-only and unavailable-telemetry states, immutable report identities,
  cumulative/delta deduplication and truthful cap-crossing evidence.

### Fixed

- Preserved secure Windows execution truth: editor-visible models route through
  the bounded VS Code LM broker, requested aliases resolve to the exact model
  observed by the editor, and unconstrained native CLI routes fail closed
  instead of claiming sandbox readiness.
- Rewrote declared `python -m ruff` validation commands to the trusted
  repository-runtime Ruff executable, closing false `validation_failed`
  results in isolated workers.
- Classified structured provider 401/403 authentication failures as blocked
  launch failures rather than empty review candidates, without persisting
  provider error bodies or secrets.
- Stopped terminal process rows from being mislabeled `liveness=lost` and
  excluded blocked cards from active write-collision ownership.

## [0.8.47] - 2026-08-03

### Fixed

- Removed a Python 3.14-only procfs race from the abrupt-supervisor-loss
  regression: a worker disappearing between the existence probe and
  `/proc/<pid>/stat` read is now correctly treated as successful termination.

## [0.8.46] - 2026-08-03

### Fixed

- Normalized declared `pytest` validation commands to the trusted running
  Python interpreter before entering the secure sandbox. Packaged workers no
  longer fail by trying to execute the absent `/bin/pytest` console script.
- Resolved approved bare `ruff` validations from the selected repository or
  active trusted virtual environment, with owner/mode/symlink checks and an
  explicit read-only sandbox bind instead of trusting `PATH`.
- Preserved high-value semantic fields in bounded Source Graph/context JSON
  previews instead of returning an arbitrary alphabetic prefix.
- Made automatic review risk signals monotonic and derived from the task card
  and candidate diff, while requiring explicit validation for mutating code
  tasks and exposing Claude's quality-review submission tool.
- Corrected token/cache accounting across retries and surfaced truthful
  unknown-cost evidence instead of reporting unpriced work as free.
- Preserved hash-pinned predecessor identity after failed validation and made
  strict read-only research tasks reach review through bounded, hash-verified
  provider evidence without weakening the empty-diff rule for code tasks.
- Indexed the exact isolated candidate tree for independent quality reviewers,
  so Source Graph review queries inspect proposed code rather than stale HEAD.
- Recognized Claude `message_delta` and terminal stream events in the dashboard
  instead of rendering valid provider JSON as an unsupported event shape.

### Added

- Added dashboard KPI evidence for context delivery, tool use, validation and
  provider/runtime outcomes, backed by focused regression coverage.
- Added bounded initial/rework prompt envelopes with per-section byte evidence
  and compact residual feedback, keeping token claims separate from byte data.

## [0.8.45] - 2026-08-03

### Added

- Added a mandatory MCP server-level Manager Contract banner, visible during
  protocol initialization in both FastMCP and the packaged stdlib fallback.
  It defines repository authority, startup order, truthful task transitions,
  safe parallel launch, callback/review ownership and lost-ack recovery.
- Expanded the public first-run documentation with copy/paste manager prompts,
  the exact pending/processing/review lifecycle and evidence-first acceptance.

## [0.8.44] - 2026-08-03

### Fixed

- Made the automatic VS Code model broker fail-open during extension
  activation. Some provider catalogs can transiently return null/malformed
  model entries or reject discovery; those entries are now ignored and a
  bounded degraded heartbeat log is recorded instead of taking down the
  AIWorkHub dashboard and MCP runtime.

## [0.8.43] - 2026-08-03

### Added

- Made the credential-free VS Code Language Model broker active by default.
  It discovers only models already authorized in the current editor window,
  requests consent only when an exact queued task first invokes a model, and
  carries the same model catalog into Remote-SSH repository workers.
- Added bounded broker observability to preflight: live/stale host counts,
  freshest heartbeat age, exact visible model identities and a dashboard
  summary that separates editor models from redundant execution routes.

### Fixed

- Distinguished an expired editor heartbeat from a live host that genuinely
  cannot see a requested model. Reloads now report `vscode_lm_host_stale`
  instead of the misleading `vscode_lm_model_not_visible` entitlement error.
- Added editor-broker fallback for Claude and Codex workers as well as
  DeepSeek and GLM, with exact per-model visibility checks. Workforce ranking
  now returns the resolved effective adapter rather than the unavailable
  declared adapter, closing the rank-success/launch-failure split.
- Applied VS Code LM bridge setting changes without requiring another window
  reload.

## [0.8.42] - 2026-08-03

### Fixed

- Made manager and worker fallback MCP responses explicit binary UTF-8 rather
  than locale-encoded text. Georgian and other Unicode task content can no
  longer raise `UnicodeEncodeError` on Windows `cp1251` stdout after a task
  mutation has committed, so `task_create` and the following `task_show` stay
  on the same live transport.
- Added a deterministic locale-hostile JSON-RPC regression covering Georgian
  `task_create -> task_show`, plus the equivalent worker stdio response path.

## [0.8.41] - 2026-08-03

### Added

- Added durable create reconciliation: identical retries of a committed task
  now return `created:false` with the existing canonical receipt, while a
  same-id/different-payload request remains a field-described conflict.
- Added empty MCP resource discovery surfaces for fallback runtimes, removing
  `resources/list` and `resources/templates/list` compatibility warnings.

### Fixed

- Replaced versioned/source-checkout Codex MCP registrations with a
  host-stable launcher. Marketplace activation atomically migrates legacy
  `PYTHONPATH` entries once, preserves tool approvals, and future upgrades
  advance only the immutable runtime pointer instead of closing live stdio.
- Serialized task create/claim/launch/review/finalize mutations inside each MCP
  server and added an immediate SQLite transaction boundary, preventing
  concurrent shared-transport writes from racing.
- Made fallback stdio shutdown BrokenPipe-safe with bounded structured stderr
  diagnostics, and restored Windows child lifetime ownership through Job
  Objects for both muxed and passthrough Codex processes.
- Repaired AI Memory FTS migration, repository-authoritative dashboard and
  preflight truth, terminal liveness, targeted Markdown/body Source Graph
  search, DeepSeek/GLM adapter fallback, and repo-bound usage/cost provenance.
- Allowed read-only research reviewers without output files, isolated pytest
  cache writes in worker sandboxes, and returned actionable residual-identity
  schema guidance during review rejection.

## [0.8.40] - 2026-08-02

### Fixed

- Removed the Codex-active reload race by starting the real App Server before
  repository-route discovery and attaching the exact repo-scoped sideband in
  the background. A restored Codex editor no longer needs a second reload,
  while callback authority remains unpublished until the current extension
  host's unique repository route is verified.
- Isolated callback-mux tests from the real host launcher and executable pin.
  Test fixtures can no longer rewrite `~/.local/bin/aiworkhub-app-server-mux`
  to a deleted temporary directory and make the next active Codex reload exit
  with code 127 before AIWorkHub activation can repair it.

## [0.8.39] - 2026-08-02

### Added

- Added canonical manager accept/reject and rejection-latency KPIs, explicit
  known-versus-unknown provider cost accounting, telemetry-capable Source
  Graph denominators, and actionable terminal-failure guidance in task rows.
- Added bounded, paginated terminal-retention previews and automatic
  repository startup enforcement with an undo quarantine window.

### Fixed

- Removed recursively persisted `card_json` envelopes from task generations
  and worker prompts, eliminating the observed 343K–615K token amplification.
- Made isolated read-only Source Graph queries compatible with SQLite by using
  DELETE journaling, and enabled JSON Lines/NDJSON language recognition.
- Preserved `claimed_by` in bounded lifecycle projections and made startup
  retention failures incapable of terminating or polluting MCP stdio.
- Stabilized GLM 5.2 and DeepSeek VS Code model discovery through canonical
  aliases, and published a strict item enum for MCP `risk_signals` arrays.

## [0.8.38] - 2026-08-02

### Added

- Moved the complete Operations surface into a dedicated Dashboard dialog,
  with KPIs as the default tab and direct entry points for Tool Use and
  Storage. The selected-task inspector now uses the full available width.

### Fixed

- Replaced recursive `collect_result` task/event documents with bounded
  projections, stable hashes, explicit truncation metadata and a retrieval
  cursor. Large nested review evidence can no longer inflate a bounded collect
  response to tens of thousands of tokens.
- Preserved rejected-review predecessor artifacts through residual rework,
  materialized declared JSON/JSONL inputs safely, enforced typed residual and
  contradictory path contracts, and moved broad workspace cleanup out of the
  synchronous review transition.
- Hardened canonical context writes against legacy AI Memory schemas and
  integrity failures while keeping write failures explicit and auditable.

## [0.8.37] - 2026-08-01

### Added

- Added a bounded 15-minute informational Source Graph inter-call-gap alert to
  KPI telemetry and the dashboard. Counts, rates, thresholds and sample
  denominators remain visible, and the UI explicitly avoids interpreting an
  observed gap as proof that a model was inactive.

## [0.8.36] - 2026-08-01

### Added

- Added KPI v3 Source Graph observability: authenticated inter-call gap
  distributions, returned structural entity/edge/file counts and canonical
  index-generation attribution in the repository dashboard.

### Fixed

- Bound worker Source Graph query caches to the canonical successful index
  generation, preventing an incremental refresh from returning stale cached
  results.

## [0.8.35] - 2026-08-01

### Added

- Added Source Graph workflow-stage and latency telemetry backed by the
  authenticated worker ledger, plus mode/stage/cohort KPI visualizations over
  an aggregate-only bounded history of up to 1,000 process runs.
- Added truthful context-byte economics from declared raw repository paths
  versus the delivered project-context bundle. Token savings remain explicitly
  unavailable without a tokenizer-bound counterfactual baseline.
- Added Markdown and MDX as a configurable documentation
  family, bringing Source Graph coverage to 34 code/data/documentation
  families and making repository roadmaps and contracts searchable.

## [0.8.34] - 2026-08-01

### Added

- Added a repository-local KPI Dashboard with responsive charts for explicit
  manager decisions, bounded worker outcomes, validation failures, Source
  Graph use, callback delivery, adapter effectiveness and Session/Memory/KB
  execution.
- Added visible sample sizes, denominators, truncation and attribution quality
  disclosures. The dashboard keeps manager acceptance separate from
  `review_ready` and does not infer token savings or causal quality gains.

## [0.8.33] - 2026-08-01

### Fixed

- Made model tool-use telemetry discoverable without searching below the task
  inspector: the top diagnostics strip now has a `Telemetry` action, and the
  `Source Graph` summary card opens and scrolls directly to the `Tool Use` tab.

## [0.8.32] - 2026-08-01

### Added

- Made Source Graph mode telemetry permanently visible in the Dashboard Tool
  Use view. It now reports authenticated mode attribution, legacy/unattributed
  calls, distinct modes, the recent mode path, core `focus`/`slice`/`context`/
  `calls`/`trace`/`impact`/`testmap`/`coverage`/`bundle` counters and a bounded
  per-runner mode breakdown instead of hiding the section when old ledgers do
  not contain mode metadata.

### Fixed

- Replaced the Marketplace README's package-relative hero image with its public
  HTTPS asset and added a release documentation gate that prevents relative
  Marketplace HTML image URLs from returning.

## [0.8.31] - 2026-08-01

### Fixed

- Made process-launcher lifecycle tests independent of a host-installed Claude
  subscription while retaining focused fail-closed first-party authentication
  coverage. Clean Python 3.12, 3.13 and 3.14 CI runners now test the injected
  adapter lifecycle rather than local account availability.

## [0.8.30] - 2026-08-01

### Added

- Added reversible archived-task retention: age-based preview, protection for
  pending callback delivery, digest-bound quarantine, seven-day undo,
  collision-safe restore, separately confirmed purge and a durable compact
  audit trail. Dashboard task details now expose archive/restore actions and
  Storage exposes cleanup and quarantine controls.
- Expanded authenticated tool-use accounting beyond Source Graph with
  per-tool calls, successful calls, bounded bytes and cache hits in the
  dashboard.
- Added bounded first-party Claude CLI subscription preflight using the CLI's
  own redacted auth-status command. Claude subscription auth remains distinct
  from Copilot/VS Code model consent and no credential is copied.

### Fixed

- Preserved exact Source Graph file/context results under mature-repository
  output budgets instead of allowing an oversized nested context to erase the
  entire match.
- Made terminal-log expiration follow configured age limits without retaining
  an unbounded per-task tail after completion; active and review evidence
  remains protected.
- Added a bounded liveness reconciler so abandoned processing rows reach
  truthful review dispositions instead of silently accumulating.

## [0.8.29] - 2026-08-01

### Fixed

- Added the canonical repository ID to every Task MCP Project Context receipt,
  so workers can report and validate the exact repository identity instead of
  inferring it from a filesystem path or leaving it unresolved.
- Applied Source Graph target scoping before bounded-output truncation and
  enforced path-component boundaries. Large analytics responses can no longer
  preserve out-of-scope preview data or treat a sibling such as `eval2` as
  belonging to the requested `eval` scope.
- Excluded repository runtime `logs/` from Source Graph indexing by default,
  preventing generated task events from dominating language/file statistics
  and broad architectural queries.
- Split workforce attribution diagnostics into missing-model and unknown
  adapter/model populations, making historical unattributed process rows
  explainable without misreporting current worker launches.

### Validation

- Verified the Marketplace-installed 0.8.28 callback route end to end with a
  live Codex Spark canary; the current manager received the review callback,
  independently rejected incomplete evidence, and left the review queue empty.
- Passed 1,628 Python tests with 22 skips, all VS Code extension regressions,
  and focused Source Graph, Project Context and workforce tests.

### Changed

- Raised the supported Python baseline from 3.10 to 3.12 and moved the full CI
  matrix to Python 3.12, 3.13 and 3.14. This removes security-only legacy
  branches from the declared product surface and keeps `tomllib` available as
  a standard-library contract throughout the supported range.

## [0.8.28] - 2026-08-01

### Added

- Expanded the Marketplace and VS Code extension description with the shipped
  33-family/31-mode Source Graph, continuous-use telemetry, repository-local
  context viewers, deterministic Quality Evidence and multi-language Known Bug
  Scanner capabilities.
- Added a dedicated, opaque 256×256 Marketplace icon source and packaged PNG
  with a new asset path so Marketplace/CDN caches cannot retain the old
  transparent or missing presentation asset.

### Fixed

- Replaced the Marketplace README's obsolete SVG hero path with the packaged
  raster presentation already required by VS Code's extension-details view.
  The previous public Marketplace version was still 0.8.10 and therefore
  exposed the old SVG reference even though newer GitHub releases contained
  the corrected PNG documentation.
- Made optional Marketplace and Open VSX workflow jobs visibly skip when their
  repository switches are disabled, and fail when enabled without a token,
  instead of producing a successful job containing only skipped publish steps.

### Documentation

- Documented registry enable switches, public-version verification and the
  distinct Marketplace, dashboard and editable logo assets.

## [0.8.27] - 2026-08-01

### Added

- Added the repository-isolated Workspace Build Hygiene foundation ported from
  the proven donor design: external scratch slots, quota reservations,
  cryptographic lease release, real byte accounting, digest-bound explicit
  cleanup, rogue in-repository build-tree reporting, CLI access and bounded
  environment-preflight observability.
- Added the first diff-scoped Known Bug Scanner rule packs for C/C++/CUDA,
  cryptographic misuse, Python, JavaScript/TypeScript, Go, Java/Kotlin and PHP.
  High-confidence findings block Quality Evidence; heuristic warnings remain
  visible validation evidence without producing false proof of failure.
- Completed the repository-neutral Source Graph capability port with 31
  manager/worker MCP modes. Dedicated bounded views now cover tags, symbols,
  calls, test maps, hotspots, complexity, bottlenecks, churn, ownership,
  review queues, TODO/gap discovery, pipeline planning and non-blocking
  leak/null/raw-pointer/cast/crash/loop/dead/duplicate risk candidates.
- Expanded the Known Bug Scanner with changed-path rules for disabled TLS
  verification, literal divide-by-zero, unsafe process-shell boundaries,
  unsafe deserialization/temp-file APIs, permissive certificate callbacks and
  bounded C/C++ release-lifetime candidates. Python literals/comments and
  JavaScript RegExp `.exec()` are masked from false matches.

### Fixed

- Source Graph build reports now count the unique edge rows actually persisted
  after writer deduplication instead of the larger pre-dedup extractor
  population. Dashboard/index statistics therefore reconcile with SQLite.
- Structural test relationships are no longer presented as execution
  coverage: line/branch coverage remains explicitly `not_available` until
  genuine runtime evidence is imported.

### Documentation

- Published the complete Source Graph mode/risk contract, Known Bug Scanner
  severity boundary, donor-capability disposition and current Marketplace plus
  GitHub Release installation channels.

## [0.8.26] - 2026-08-01

### Added

- Ported the repository-neutral intelligence layer from the proven
  UltrafastSecp256k1 Source Graph: bounded `focus`, `slice`, `context`,
  `impact`, `trace` and task-shaped `bundle` queries now include ranked
  symbols, bidirectional calls, related tests, TODO/risk signals and
  index-time 90-day churn/ownership evidence.
- Added conservative semantic adapters for C/C++/CUDA/OpenCL/Metal,
  JavaScript/TypeScript, Rust, Go, Java and C#. Together with the existing
  Python and PHP adapters, these produce declarations, imports, functions or
  methods, inheritance and observed calls while ambiguous targets remain
  explicitly unresolved. All other registered families retain truthful
  file-level evidence.
- Added the public Source Graph guide covering the 33 language families,
  evidence labels, six MCP query modes, continuous-use accounting and
  repository isolation.

### Fixed

- Made compact Source Graph payloads strictly byte-bounded, deduplicated call
  evidence, and prevented commented-out imports/includes from becoming graph
  facts.
- Preserved the imported package root when the automatic indexing daemon
  starts its dedicated child process, so source-checkout/editor test runtimes
  no longer fail with `ModuleNotFoundError` while packaged installs remain
  unchanged.

## [0.8.25] - 2026-08-01

### Fixed

- Fixed macOS import failure in the Windows byte-range lock compatibility
  layer: Darwin exposes the deadlock errno as `EDEADLK`, while Windows may
  expose `EDEADLOCK`. The runtime now resolves either spelling without
  weakening lock contention handling on any platform.

## [0.8.24] - 2026-08-01

### Added

- Added one canonical 33-family Source Graph language registry. C/C++/CUDA,
  JSON, XML and the other registered families now receive exact file-level
  path/hash/size evidence when no semantic parser is available; Python and
  PHP retain their stronger AST/lexical extraction tiers.
- Added repository-local Source Graph language switches to Dashboard Settings.
  Changes are optimistic-lock protected, stored in
  `.aiworkhub/config/source_graph.json`, and trigger incremental reindexing so
  disabled families are removed and re-enabled families return automatically.

### Fixed

- Replaced the four-language discovery allowlist that silently skipped C++ and
  structured data files, while preserving build/cache/archive exclusions and
  backward-compatible migration from the v1 ignore-only policy.
- Preserved deterministic validation evidence after a recoverable denied MCP
  tool request. A denied request remains visible as policy-warning telemetry,
  but no longer discards later valid Source Graph/Session/Memory/KB receipts;
  missing required canonical evidence still fails closed.

## [0.8.23] - 2026-08-01

### Fixed

- Fixed a Windows activation-order race where the OpenAI extension launched its bundled `codex.exe` before AIWorkHub could add the callback mux command to the extension-host `PATH`. Windows now persists the exact extension-owned native mux executable path after the same-host and explicit-opt-in gates succeed.
- Kept the existing POSIX command and `PATH` behavior unchanged, continued excluding `chatgpt.cliExecutable` from Settings Sync, and added a regression for upgrading the unresolved bare Windows command to the stable native launcher.

## [0.8.22] - 2026-08-01

### Fixed

- Replaced Windows `os.kill(pid, 0)` route and mux liveness probes with non-signalling `OpenProcess` plus `GetExitCodeProcess` checks. On Windows, the old probe could terminate the VS Code extension host with exit code 0 while the dashboard was enumerating routes, leaving the Webview stuck on `Connecting`.
- Preserved the existing POSIX liveness and `/proc` PID-reuse checks unchanged, and added a native Windows regression test proving that probing a live process does not terminate it.

## [0.8.21] - 2026-08-01

### Fixed

- Removed three unguarded `os.getuid()` call sites in
  `terminal_authority.load_or_create_key`, `terminal_authority.read_grant`
  and `worker_workspace.resolve_trusted_pytest_runtime_root`. Windows
  exposes no `os.getuid` (the per-user profile is ACL-protected), so each
  call raised `AttributeError` on import-resolution paths and crashed the
  whole MCP surface there. All three now gate owner-equivalence behind
  `os.name != "nt"` while keeping the strict POSIX mode-bit contract intact.
- Made `process_event_ledger._rotate` use the cross-platform
  `atomic_replace` instead of a bare `os.replace`. Dashboard and review
  readers briefly hold the active ledger open; on Windows that sharing lock
  turned a normal concurrent read into a `WinError 32` write failure that
  dropped audit events. The bounded retry in `atomic_replace` tolerates the
  transient without weakening the existing lock-held exclusion of other
  writers.
- Stopped worker MCP test helpers from hard-coding `os.fchmod`. 28
  `test_aiworkhub_dynamic_worker_mcp_*` tests failed at setup on Windows
  because `monkeypatch.setattr(os, "fchmod", ...)` raises `AttributeError`
  where the attribute is absent. The helpers now guard the patch with
  `hasattr(os, "fchmod")`, matching the no-op `chmod_fd` contract
  `platform_io` already applies on Windows.
- Stopped the bubblewrap sandbox-alias provisioning from collapse to a
  Windows drive-relative path. `provision_worker_mcp_runtime` now builds
  `/workspace`, `/authority-repo`, `/aiworkhub-package-root` and the
  home alias as `PurePosixPath` for the bubblewrap backend, so their
  string form stays POSIX-shaped even on a Windows host (previously
  `str(Path("/aiworkhub-package-root"))` became `\\aiworkhub-package-root`
  and the worker MCP env pointed at a non-existent drive-relative root).
- Made the Codex worker-MCP config assertions platform-independent by
  parsing the rendered TOML with `tomllib.loads` instead of a raw substring
  search. TOML escapes backslashes in Windows paths, so the previous
  `str(path) in toml_text` assertion failed even though the deserialised
  value was correct.

## [0.8.20] - 2026-08-01

### Fixed

- Stopped the extension host from issuing `taskkill /PID <pid> /T /F` for a
  child that had already exited. A pid identifies our child only while that
  child runs; afterwards Windows may hand the same pid to any process, and
  `/T` kills the target *plus every descendant*. If the recycled pid landed
  above this extension host, the tree kill took the host down — and with it
  every other extension in the window (Codex, Copilot, Claude), abruptly and
  with no chance to log anything. Node still owns the process handle, so
  `exitCode`/`signalCode` now gate the tree kill. POSIX was unaffected: it
  uses the exact-child `kill()` path instead.
- Made `lock_fd(blocking=True)` actually block on Windows. `msvcrt.LK_LOCK`
  is not the counterpart of `flock(LOCK_EX)`: it retries ten times at
  one-second intervals and then raises `OSError`, so a lock held longer than
  ten seconds became a hard failure on Windows while POSIX callers simply
  waited. It now polls the non-blocking primitive, bounded by
  `WINDOWS_LOCK_MAX_WAIT_SECONDS`: unlike `flock`, a Windows byte-range lock
  can be blocked by this same process holding another handle, which waiting
  can never clear, so an unbounded wait would freeze the caller outright.
- Made system-log pruning linear. It runs on every recorded line — every
  `[mcp stderr]` chunk and every tool call — and re-serialized the entire
  retained array on each iteration of a pop loop. Once the retained set
  crossed the 1 MiB cap that cost ~89 ms per logged line, so ~100 lines
  blocked the extension-host thread for ~9 s. A host that stops answering
  VS Code's ping is terminated, taking every other extension in the window
  (Codex, Copilot, Claude) with it. Measured 88.83 ms -> 2.10 ms per line
  with byte-identical output.
- Restored the README hero image on the extension details page. It pointed at
  an SVG, which VS Code refuses as an image source ("SVGs are not a valid
  image source") and the Marketplace strips, so the page rendered a broken
  image. The same artwork now ships as `media/aiworkhub-hero.png`, rasterized
  from the unchanged `.svg` master, and the packaging allowlist bundles it.

### Changed

- Replaced the placeholder `Other` Marketplace category with `AI`, so the
  extension is listed as `AI` + `Visualization` rather than falling into the
  catch-all bucket.

## [0.8.19] - 2026-08-01

### Fixed

- Stopped worker finalization from terminating a process whose identity was
  never verified. `_pid_matches` reports a match for any live PID when the
  supervisor status recorded no `child_pid_start_ticks`, and the child branch
  of `_finalize_isolated_request` trusted it while the sibling supervisor
  branch did not. Termination now goes through `_identity_verified_pid`, which
  requires the recorded process creation timestamp. On Linux the terminator is
  `os.killpg`, which fails closed on a non-leader PID, so the mistake was
  nearly inert; on Windows there is no `killpg` and the same call becomes
  `taskkill /PID <pid> /T`, which kills that PID *and every descendant* — so a
  recycled PID could destroy an unrelated process tree.

### Added

- Passive extension-host crash diagnostics behind the existing
  `aiworkhub.debugTracing` setting. Uncaught exceptions, unhandled rejections,
  exit codes and signals are now recorded to the fsynced trace file. Every
  extension in a window shares one extension-host process, so a fatal error
  reached while opening the dashboard also takes Codex, Copilot and Claude
  down; previously that death left no post-mortem at all. The listeners
  observe only and add no steady-state cost.

## [0.8.18] - 2026-07-31

### Fixed

- Removed eager `*`/startup activation and the package-level
  `chatgpt.cliExecutable` override. Installing AIWorkHub no longer starts an
  MCP child, activates VS Code model providers, or changes Codex before the
  user opens the dashboard.
- Made the Codex callback mux and VS Code language-model worker bridge
  explicit opt-ins. Safe mode also removes legacy AIWorkHub-owned Codex
  overrides while preserving unrelated custom executables.
- Kept the first dashboard snapshot available during transient Windows route
  file contention and added regressions for zero eager children, lazy startup,
  and callback override cleanup.

## [0.8.17] - 2026-07-31

### Fixed

- Prevented transient Windows routing-file `EPERM`/`EBUSY` failures from
  blocking the first dashboard snapshot and leaving the Webview permanently
  on `Connecting`. Routing publication is now best-effort at the snapshot
  boundary while repository reads continue through the bound MCP child.
- Made routing JSON temporary names collision-safe for concurrent dashboard,
  lease-renewal, and startup-convergence writes, with failed temporary files
  cleaned up. Added a Windows contention regression that proves the dashboard
  still reaches a live snapshot.

## [0.8.16] - 2026-07-31

### Fixed

- Fixed Windows manager verification and the dashboard's permanent
  `Connecting` state by replacing the unavailable `/proc` ancestry check with
  a bounded native Toolhelp process-tree snapshot and same-user token SID
  validation. The existing Linux `/proc` verification path is unchanged.
- Fixed Windows Codex identity dispatch so it reaches the verified
  extension-owned route instead of returning at the first missing `/proc`
  read. A callback-pending route remains a valid repository-local manager
  while continuing to fail closed for direct callback delivery.

## [0.8.15] - 2026-07-31

### Fixed

- Fixed first-time repository activation on Windows by publishing the
  authoritative storage-ready snapshot from the MCP process that performed
  initialization before changing the manifest identity and rebinding.
- Added a bounded MCP shutdown/startup handoff and exact owned-process-tree
  termination for Windows `py.exe`/`python.exe` launch chains, preventing the
  replacement runtime from colliding with lingering SQLite and repository
  handles. Linux and macOS retain their existing exact-child shutdown path.
- Added Windows init/rebind regression coverage and durable pre-rebind
  readiness logging for future runtime diagnosis.

## [0.8.11] - 2026-07-31

### Fixed

- Restored canonical Session Manager writes and explicit session imports for
  repositories that adopted the richer legacy transcript schema. The shared
  adapter now supplies provenance fields and updates either standalone or
  external-content FTS indexes without replacing historical databases.
- Added regression coverage for both fresh minimal transcript stores and
  migrated rich transcript stores, including audited writes, imports, search
  indexing and rollback-compatible entity ownership.

## [0.8.10] - 2026-07-31

### Fixed

- Replaced three per-task N+1 dashboard scans with one canonical batch-card
  read. On the live GeoAI repository the snapshot build fell from 23.5s to
  0.83s and from 764KB to 310KB, keeping it inside the extension request
  deadline instead of opening the recovery circuit.
- Limited the refresh snapshot to 50 recent process rows and a 512KB transport
  budget. Full task details, live output, logs, memory, sessions and KB remain
  available through their dedicated bounded tools.

## [0.8.9] - 2026-07-31

### Fixed

- Moved CPU-heavy Source Graph builds out of every MCP stdio process and
  into a dedicated, cancellable indexing subprocess. Large repository
  indexing can no longer starve dashboard snapshots, callback delivery or
  health requests behind the Python GIL.
- Coalesced dispatcher startup, route promotion and watchdog convergence on
  one repository-bound operation, preventing concurrent `ensure_started`
  calls from blocking the MCP channel and leaving the dashboard on
  `Connecting` / `mcp_recovery_circuit_open`.
- Added production subprocess-indexing coverage while retaining deterministic
  in-process test injection for Source Graph lifecycle unit tests.

## [0.8.8] - 2026-07-31

### Fixed

- Prevented the dashboard runtime status check from racing the asynchronous
  MCP startup sequence. Runtime version and dashboard capabilities are now
  verified once during the child handshake and reused for that exact child
  lifecycle, so a healthy repository-bound runtime is never restarted into a
  false `mcp_version_mismatch_after_repair` / `mcp_recovery_circuit_open`
  state while callback and Source Graph services converge in the background.
- Extended the multi-repository, reloadless-repair and route-lease regression
  harnesses to exercise the same handshake capability contract as the live
  extension.

## [0.8.7] - 2026-07-31

### Changed

- Set the canonical VS Code Marketplace publisher to `IvaneChkheidze`; release
  qualification now fails if future extension manifests drift to another
  publisher identity.

### Documentation

- Added English and Georgian launch articles, platform-specific publication
  copy and a truth-preserving publishing checklist.

## [0.8.6] - 2026-07-31

### Added

- Added public documentation for the distinct Source Graph and manager-only
  Context Graph authorities, including capture scope, retrieval operations,
  privacy boundaries, relationships to Session/Memory/KB and measurable
  recovery outcomes.

### Fixed

- Packaged stdlib MCP schemas now emit an `items` contract for every array,
  including the `aiworkhub_agent_accept_review` reviewer fields required by
  Copilot's MCP validator.
- Rebuilt the public workflow GIF from five complete opaque frames, removing
  the corrupt/missing-frame sequence that flashed black on GitHub.

## [0.8.5] - 2026-07-31

### Added

- Generated manager instructions now bind the repository Context Graph's
  bounded search, range and related-evidence tools into the normal manager
  workflow while keeping workers outside the manager transcript graph.

### Fixed

- VS Code Language Model workers can now update an explicitly allowed
  repository-root file under Landlock without requesting broad write access to
  the repository root. Nested outputs retain atomic replacement and `.git`
  metadata remains protected.
- Tightened output-path symlink checks before model edits are materialized.
- Corrected the public acknowledgement so Context Graph is not attributed to
  another project.

## [0.8.4] - 2026-07-31

### Added

- Added passive Codex manager transcript capture from authoritative App Server
  `item/completed` user/assistant messages into the enabled repository Manager
  Context Graph.
- Added a bounded background capture queue, exact repo/thread route
  verification, deterministic idempotency and capture health counters so
  transcript persistence never blocks the visible chat transport.

### Security

- Capture remains repository opt-in and manager-only. Reasoning, tool output,
  commands, approvals, streaming deltas and worker traffic are excluded.
- Failed or ambiguous repository/thread routes fail closed without writing
  content or scraping another extension's private storage.

## [0.8.3] - 2026-07-31

### Fixed

- Restricted automatic Context Graph capture to verified manager Session
  writes. Worker Session/Memory/KB activity remains available through its
  canonical tools but never enters the Manager Context Graph.
- Renamed the Settings surface to `Manager Context Graph` and documented the
  manager-only boundary explicitly.

## [0.8.2] - 2026-07-31

### Added

- Added an opt-in, append-only repository conversation ledger in the canonical
  transcript database with deterministic Context Graph nodes and relations.
- Added bounded manager MCP operations for Context Graph search, exact
  transcript ranges, related-node retrieval, event ingestion and projection
  rebuild.
- Canonical Session Manager writes now feed the Context Graph atomically when
  the repository feature is enabled.
- Settings now reports exact repository-local Context Graph event, node and
  edge counts.

### Changed

- Context Graph is now a real repository runtime behind the existing
  revision-guarded feature switch instead of a dormant configuration entry.
- Fresh repository initialization provisions the rebuildable Context Graph
  schema without enabling transcript capture by default.

## [0.8.1] - 2026-07-30

### Added

- Added a repository-local Settings dialog for Source Graph, Session Manager,
  AI Memory, Knowledge Base and the upcoming Context Graph runtime.
- Added an optimistic revision-guarded `.aiworkhub/config/features.json`
  contract so concurrent VS Code windows cannot silently overwrite settings.

### Changed

- Source Graph settings now control the real repo-bound daemon lifecycle;
  disabling stops indexing and tool calls fail explicitly until re-enabled.
- Session Manager, AI Memory and Knowledge Base model tools now honor their
  repository feature switches while task orchestration and callback routing
  remain protected core services.

### Added

- Added a tracked repository quality policy with portable `{python}` command
  resolution and explicit configured/unverified policy status.
- Added deterministic extension test discovery so every `*.test.js` file is
  executed in CI without maintaining a filename chain.
- Added complete retention footprint accounting for canonical runtime, legacy
  logs, orphan request files and repository/shared worktree populations.
- Added exact AIWorkHub stale-worktree registration attribution and a
  digest-bound, explicitly confirmed dashboard prune action that fails closed
  when any stale foreign registration is present.
- Added reversible quarantine, restore and delayed purge for the aged legacy
  root `logs/` store, guarded by an exact preview identity and confirmation.
- Added immutable process-ledger rotation at 48 MiB with streaming readers and
  complete rotated-segment storage accounting.
- Added 16 MiB per-stream worker output bounds that retain the newest stdout
  and stderr tail and record dropped-byte evidence in supervisor status.
- Added claim-epoch binding for deterministic verification and terminal review
  evidence; stale verdicts are cleared on reject/re-claim and cannot authorize
  acceptance from another execution episode.
- Split Plan DAG blockage observability into total, dependency-blocked and
  lifecycle-blocked counts and exact task-ID populations.
- Added reproducible Ruff correctness and mypy typed-kernel gates to repository
  policy, pull-request CI and tag releases through a declared `dev` extra.
- Added a read-only 26-case Quality Gate calibration report with false-green,
  false-red and expected-blocker metrics, required across the platform CI
  matrix; excess reviewer reports now fail closed instead of silent truncation.
- Release metadata verification now fails when the canonical version has no
  corresponding changelog section.
- Replaced the implementation-history README with an outcome-first public
  product page and added a CI contract against broken links, internal task IDs,
  legacy host paths and completion-tool naming drift.
- Documented the optional Codex App Server adapter as a replaceable
  compatibility boundary with manager-inbox fallback, and added optional PyPI
  Trusted Publishing to the tag release workflow.
- Modernized package licensing metadata to SPDX/PEP 639 form and made Twine
  metadata/rendering validation a release gate before registry publication.
- Added the canonical AIWorkHub brand system, repository hero, positioning,
  public support guide, Marketplace-ready extension page and package discovery
  metadata.
- Added a single canonical release-version authority, deterministic projection
  sync/check tooling, repeated-build VSIX reproducibility gates and published
  SHA-256 checksums for release artifacts.
- Added the Quality Gate 2.0 contract and ADR: six falsifiable lenses,
  deterministic verdict ownership, risk-proportional review, combined-tree
  verification and positive/negative gate calibration.
- Added the first Quality Gate 2.0 runtime foundation: a pure six-lens verdict
  fold, monotonic risk profiles, strict read-only reviewer evidence schema,
  initial positive/negative fixtures and bounded dashboard verdict/lens status.
- Added manager-accept combined-tree validation for medium-and-higher risk:
  current canonical deltas and deletions are overlaid with the exact retained
  candidate in a fresh worktree before promotion. High/critical profiles now
  fail closed without explicit human approval.
- Added project acknowledgements with attribution to `kimi-atlas` for the
  quality-gate ideas that informed this direction.

### Changed

- Missing or empty `.aiworkhub/quality.json` can no longer produce an empty
  `ok: true`; evidence surfaces identify builtin-only versus repository-policy
  verification explicitly.
- Storage previews now report the measured total footprint and name unmanaged
  legacy/unattributed populations instead of presenting a partial total as
  repository health.
- Reframed the repository landing page around user outcomes and the current
  product architecture instead of historical implementation notes.
- Corrected the security and issue-reporting guidance to reflect the native
  stdio dashboard and repository-local runtime.

## [0.8.0] - 2026-07-30

### Added

- Added manager-reviewed context-write intents and safe legacy Session/Memory/
  KB import into canonical repository storage.
- Added a four-platform fresh-install qualification matrix and reproducible
  VSIX/package release checks.

### Fixed

- Made repository handoff and worker relaunch atomic across manager changes.
- Preserved archived terminal lifecycle truth and blocked destructive review
  false-greens before promotion.
- Stabilized Linux, Windows, macOS and Remote-SSH release qualification.

## [0.7.9] - 2026-07-30

### Added

- Added bounded `repo_list`, `repo_current`, and manager-only
  `repo_switch(repo_id)` operations for exact live multi-repository routing.
- Added audited and idempotent manager writes for Session Manager, AI Memory,
  and KB, plus exact/related AI Memory reads on manager and worker surfaces.
- Added bounded Session Manager and KB dashboard viewers beside Logs and AI
  Memory, with repository-registry-resolved canonical storage only.
- Added self-describing Source Graph enum schemas and bounded valid examples
  for invalid mode requests.

### Fixed

- Repo-neutral Codex MCP processes now resolve the exact live thread route;
  explicitly repo-bound extension and worker children remain immutable.
- Cooperative callback startup rebinds pending same-repository events to the
  current verified manager and recreates missing review callbacks after
  reload, while retaining the originating thread as audit provenance.
- AI Memory dashboard/read queries now tolerate fresh minimal schemas and
  exclude archived or superseded entries when lifecycle state is available.

## [0.6.75] - 2026-07-28

### Fixed

- Callback delivery now uses the already-installed, repository-bound Codex
  App Server mux sideband instead of spawning a competing second App Server.
- Sideband delivery has a bounded 45-second local round-trip timeout and
  90-second recoverable lease, so an abrupt reload cannot block later events
  behind the subprocess transport's former 35-minute inflight lease.
- Idle callback polling is capped at two seconds and its wait is interruptible,
  providing prompt terminal-event delivery and clean extension reloads.

## [0.6.74] - 2026-07-28

### Fixed

- Added a bounded post-startup route convergence loop so the exact Codex
  thread observed just after mux startup is published within seconds instead
  of waiting for the four-minute lease-renewal tick.
- Every dashboard refresh now re-evaluates the live mux-owned route before
  building the snapshot, making the route banner self-healing without reload.

## [0.6.73] - 2026-07-28

### Fixed

- Reload recovery now treats a shared callback route as live only while both
  its lease is fresh and its owning extension-host PID exists. A dead previous
  window can no longer block the replacement window from publishing its route,
  which previously made the Codex mux time out and left callbacks permanently
  at `codex_thread_id_not_observed`.
- Added the pending-to-verified and post-mux-ready callback regressions to the
  extension's normal release test suite.

## [0.6.72] - 2026-07-28

### Fixed

- Moved the Codex mux launcher out of the versioned VSIX directory into the
  extension's stable global-storage path. Future extension upgrades update
  an immutable runtime pointer without requiring a second reload to repair a
  stale `chatgpt.cliExecutable` path.
- Classifies routine MCP stderr transport messages as informational unless
  their content actually contains a warning, degradation, failure, or error.

## [0.6.71] - 2026-07-28

### Fixed

- Closed the VS Code parallel-activation race: the Codex app-server mux now
  waits up to ten seconds for the exact parent extension-host repository
  route before safely falling back to transparent passthrough.

### Added

- Added an always-visible one-line latest-system-event strip with a full log
  popup. Logs are repository-isolated, capped at 1 MiB, and retained for at
  most seven days under the repo-local runtime tree.
- Added a read-only AI Memory popup backed by the current repository's
  canonical storage-registry database, with local filtering and no access
  counter mutation.

## [0.6.70] - 2026-07-28

### Fixed

- Automatically configures the packaged, cross-platform Codex App Server mux
  when `chatgpt.cliExecutable` is unset or already AIWorkHub-owned. This is
  the missing thread-observation source required to recover callback routes
  after reload; unrelated custom executables are preserved.
- Removed duplicate `route`/`route_reason` fields from the manager banner.
- Added a bounded, newest-first System Log terminal with formatted levels and
  components plus Copy/Clear controls. Its 200-entry ring buffer stays in
  memory and creates no additional disk ledger.

## [0.6.69] - 2026-07-28

### Fixed

- Stopped the dashboard from showing a green fully-verified manager banner
  while its coordinator route is still `route_pending`. Identity, route, and
  dispatcher health are now evaluated and labelled independently.

## [0.6.68] - 2026-07-28

### Changed

- Promoted managed storage and free-disk telemetry into the always-visible
  dashboard header. Clicking it opens the detailed Storage operations tab.

## [0.6.67] - 2026-07-28

### Added

- Added a read-only **Storage** dashboard tab showing repository-local
  `.aiworkhub` data, retained worker worktrees, safely reclaimable bytes, and
  current filesystem capacity/free space in human-readable units.
- Added cached background storage measurement, so expensive worktree sizing
  never blocks the dashboard refresh path even with hundreds of retained
  task worktrees.

## [0.6.66] - 2026-07-28

### Fixed

- Callback delivery now acknowledges the successful synchronous
  `turn/start` response. A later cancelled/interrupted terminal notification
  can no longer retry an already-injected callback five times and then move it
  to `dead_letter`.
- A verified route may recover a matching dead-letter callback exactly once,
  so review work stranded during a reload is replayed without an infinite
  resurrection loop.
- Retained worker worktrees are collected after their exact attempt is
  finished, archived, rejected to pending, blocked, or superseded by a newer
  review request. The current review request, live processes, malformed
  authority, and unsafe paths still fail closed and remain untouched.
- Coordinator lifecycle actions trigger the same safe retention sweep
  immediately; the periodic reconciler remains the durable fallback.
- Codex callback ownership remains extension-scoped. A verified headless MCP
  client no longer becomes a competing dispatcher owner, and AIWorkHub does
  not silently rewrite another extension's CLI configuration.

## [0.6.65] - 2026-07-27

### Fixed

- B1021: Verified non-route_pending Codex manager identity now makes
  `dispatch_expected` true in `dispatcher_health` and exposes `window_id`
  in `dispatcher_ensure_started` even without `AIWORKHUB_WINDOW_ID`, so a
  Codex-attached dashboard without the env window ID can still dispatch
  callbacks.
- B1017: Immediate Codex route publication post-mux-ready convergence: a
  ready App Server mux instance publishes `capability_state=available` and
  its verified `thread_id` without waiting for the 4-minute renewal tick,
  and negative ownership cases correctly remain `route_pending`.

## [0.6.64] - 2026-07-27

### Fixed

- Exact reload and live-mux Codex route publication with shared-manifest
  split-brain protection prevents stale route tables across concurrent windows.
- Durable callback outbox rebind, seed, and replay after route restoration
  ensures no callback is dropped when the transport reconnects.
- Safe nested `user → message → tool_result` Live Output rendering guards
  against malformed message envelopes in the dashboard timeline.

## [0.6.52] - 2026-07-25

### Fixed

- Made packaged MCP runtimes immutable and content-addressed in VS Code global
  storage, so installing a newer extension cannot delete the runtime beneath
  already-running Codex/Claude windows.
- Repaired exact repository/thread callback routing, callback replay after
  reconnect, terminal substatus preservation, and repository-isolated shared
  route discovery.
- Restored Claude manager task creation by normalizing its verified
  `session_id` with Codex `thread_id` into the canonical origin route.
- Preserved immutable declared-input hashes through review acceptance and
  failed closed when a dependency changes before promotion.
- Repaired workspace MCP configuration migration to the stable packaged
  runtime on Linux, Windows, Remote-SSH, and multi-repository windows.
- Improved dashboard manager/routing diagnostics and live-output formatting.

### Changed

- Release verification now requires all four version authorities
  (`pyproject.toml`, Python `__version__`, extension manifest, and extension
  runtime constant) to match the release tag.

## [0.6.26] - 2026-07-22

### Fixed

- Aligned the repository-binding runtime-version regression contract with the
  packaged release so CI and tag-driven VSIX publication validate the same
  canonical version.

## [0.6.25] - 2026-07-22

### Added

- Deterministic task lifecycle finite-state machine: every status transition
  is now explicit and exhaustively enumerated, so a non-transition is
  provably rejected rather than falling through a status-string comparison.
- Plan-DAG task dependencies: `depends_on` edges, readiness computation, and
  write-overlap blocker detection so a task cannot become claimable while a
  dependency is outstanding or while its `allowed_writes` collides with an
  in-flight dependency's.
- Review-before-promotion retained workspaces: a worker's isolated worktree
  is retained through `review` and only reclaimed after a confirmed
  coordinator disposition, instead of being torn down at worker exit.
- Independent coordinator accept path: the coordinator re-validates and
  hash-gates a worker's changed files against its own rerun before
  promotion, independent of the worker's self-reported validation.

### Fixed

- Safe validation cwd compatibility: validation subprocess working directory
  resolution stays compatible with the worker's own worktree across the
  supported cwd/PYTHONPATH combinations.
- Cross-platform Codex runtime migration compatibility: the bundled/embedded
  MCP runtime migration path launches consistently across the supported
  platforms.

### Changed

- Python package (`aiworkhub`), MCP runtime, and VS Code extension
  (`vscode-extension/package.json`) versions aligned at `0.6.25`.
- README dispatch/verification section now documents Plan-DAG `depends_on`
  dependencies and the plan snapshot alongside the existing three-layer
  validation/acceptance model.
- README Kimi-Atlas-inspired roadmap section now distinguishes implemented
  concepts (deterministic lifecycle FSM, Plan-DAG dependencies/readiness,
  deterministic verification lenses, independent coordinator accept) from
  concepts still on the roadmap (combined-tree differential gate, read-time
  context graph, SAFE untrusted-output wrapper, forward-recovery expansion).

## [0.6.24] - 2026-07-22

### Fixed

- Route every non-exited terminal supervisor state to `review` and never back
  to `pending`, enqueueing exactly one release
  (`test_non_exited_terminal_states_route_to_review_never_pending_and_enqueue_one_release`).
- Treat a detached, shell-free worker process that exits cleanly as reaching
  `review_ready` on its own exact terminal authority, and make workspace GC
  wait for a confirmed canonical terminal status before reclaiming a
  finalized worktree (`test_real_shell_free_process_reaches_review_ready`,
  `test_gc_still_waits_for_confirmed_canonical_terminal_status_after_retain`).
- Resolve the validation-time `PYTHONPATH`/cwd strictly beneath the worker's
  own worktree and scope any override to the single validation subprocess it
  was requested for, instead of leaking a broader or parent-repo path
  (`resolve_validation_pythonpath`,
  `test_validation_pythonpath_resolution_is_beneath_worktree`,
  `test_validation_pythonpath_override_is_scoped_to_one_subprocess`).
- Repair the embedded/bundled MCP runtime and the VS Code dashboard panel
  controller without requiring a window reload: the bundled runtime spawns
  cleanly on its own, and panel revival disposes the stale controller before
  adopting the new one (`_spawn_bundled_runtime`,
  `test_revive_dashboard_panel_disposes_stale_controller_first`).
- Migrate the Copilot and Codex worker MCP configs to launch the packaged
  runtime as a Python module with a dedicated, portability-safe `PYTHONPATH`
  alias, selectively (not a blanket rewrite of every adapter config)
  (`test_worker_mcp_server_copilot_and_codex_configs_also_launch_as_module`,
  `test_pythonpath_uses_dedicated_bubblewrap_package_alias_not_authority_repo`).
- Confirmed the cost/usage surfaces (`cost_ledger.py`'s `build_cost_ledger`,
  `aiworkhub_task_cost_ledger`, `aiworkhub_task_usage_report`) remain
  read-only telemetry: no per-model or per-task cost ceiling gates a launch
  or a review transition anywhere in the launch/callback path.

### Changed

- Python package (`aiworkhub`), MCP runtime, and VS Code extension
  (`vscode-extension/package.json`) versions aligned at `0.6.24`.
- README documents the three-layer dispatch/validation/acceptance model
  (worker self-validation, independent coordinator re-validation, and the
  audit-ledger acceptance gate) and records Kimi-Atlas-inspired roadmap
  concepts as explicitly unimplemented design ideas.

## [0.6.10] - 2026-07-22

### Fixed

- Make every task created through the public manager MCP carry a required,
  task-type-aware project-context contract. Code tasks now fail closed when
  Source Graph evidence is empty; Session Manager is mandatory and AI Memory
  plus KB are requested adaptively for every new task.

## [0.6.9] - 2026-07-22

### Added

- Render selected-task Live Output as readable result, status, timing, token,
  cache, model and cost sections; retain the full provider JSON only inside a
  collapsed diagnostic disclosure.

### Fixed

- Start the task-scoped worker AI-tools MCP inside the self-contained VSIX
  runtime even when the optional Python `mcp` package is unavailable, using a
  bounded standard-library JSON-RPC fallback with complete tool schemas.

## [0.6.8] - 2026-07-22

### Fixed

- Remove legacy Claude `PreToolUse` hooks and permission allow entries that
  redirected models to retired `AITools/source_graph.py` or `AITools/cgraph.py`
  interfaces; unrelated owner hooks and permissions remain untouched.

## [0.6.7] - 2026-07-22

### Added

- `Initialize AIWorkHub` now idempotently installs the canonical AGENTS,
  Claude and Copilot tool-use projections in every initialized repository.
- Repository initialization safely merges Claude project settings with native
  denies for raw discovery tools while preserving owner settings and failing
  closed on malformed JSON.

## [0.6.6] - 2026-07-22

### Added

- Exposed the canonical Source Graph, Session Manager, AI Memory and KB as
  role-bound manager MCP tools as well as isolated worker MCP tools.
- Added provider-native raw-discovery denies for Claude and Copilot workers;
  `Grep`/`Glob` and shell `grep`/`rg`/`find`/`tree` cannot replace Source Graph.
- Extended the authenticated completion gate to require Session Manager and
  every requested Memory/KB surface in addition to a fresh non-empty Source
  Graph lookup for code tasks.

### Changed

- Generated agent instructions are role-aware and permit raw discovery only
  through a new exact coordinator-authorized fallback card after Source Graph
  reports a target unsupported or unindexed.

## [0.6.5] - 2026-07-21

### Fixed

- Persist manager-derived `origin_thread_id` in both the canonical task row
  and immutable card JSON so review transitions enqueue callbacks reliably.
- Preserve card origin identity when reading older rows whose denormalized
  origin column is empty.
- Make concurrent callback schema upgrades tolerate only a verified
  duplicate-column winner, preventing reload-time dispatcher thread loss.

## [0.6.4] - 2026-07-21

### Fixed

- Accept RFC 9562 UUIDv7/v8 manager session identities so current Codex
  origin threads survive mux ownership validation and `aiworkhub_task_create`
  can bind callbacks to the real originating chat.
- Treat `AIWORKHUB_REPO` as the manager-mux equivalent of the VS Code
  dashboard child's `AIWORKHUB_REPO_ROOT`, preventing false degraded health
  when both surfaces address the same canonical repository.

## [0.6.3] - 2026-07-21

### Fixed

- Made the injected AIWorkHub worker MCP the explicit mandatory interface in
  generated AGENTS, Claude and Copilot instructions; legacy `AITools` scripts,
  databases and raw repository discovery are no longer valid worker fallbacks.
- Added one-time, non-destructive migration of explicitly registered legacy
  Source Graph, Session Manager, AI Memory and KB SQLite stores into each
  repository's canonical `.aiworkhub` storage and activated their authority.
- Added manager bootstrap/task-create MCP operations and verified independent
  Codex and Claude callback lanes with repository-scoped coordinator identity.
- Hardened callback SQLite startup against transient locks and garbage-collected
  stale App Server mux runtime descriptors without touching live sessions.

## [0.6.2] - 2026-07-21

### Fixed

- Route VS Code Codex callbacks through the extension-owned App Server
  sideband mux, bundle its executable launcher in the VSIX, repair stale
  launcher settings, and start the dispatcher at VS Code startup.
- Load the current owner-only coordinator token into the MCP child instead
  of inheriting a stale parent token.
- Increment and persist `claim_epoch` on every native exact claim and
  auto-pickup so callback deduplication distinguishes requeued task episodes.
- Interrupt active sideband reads during dispatcher shutdown so VS Code
  reload cannot strand a callback batch behind a long orphaned lease.
- Discard out-of-order dashboard snapshot responses so an older overlapping
  refresh can never overwrite a newer canonical queue state.
- Treat dispatcher startup from headless worker MCP processes as a normal
  worker-role boundary; only the VS Code extension child owns coordinator
  callback dispatch and its required window identity.
- Recognize a same-repository interactive Claude Code VS Code parent as the
  selected Claude manager, derive its live window/session identity from
  same-uid runtime metadata, and grant coordinator transitions only while
  the repository route explicitly selects Claude.
- Partition callback outbox/batches by the task's originating coordinator
  provider so Codex and Claude managers operate automatically and in
  parallel without a repository-global provider toggle.
- Replace the misleading dashboard coordinator toggle with an automatic
  per-task routing status indicator.
- Native `mark_review` now durably enqueues the repository-bound callback in
  the same canonical task database.
- Callback eligibility now treats live lifecycle columns as authoritative
  over stale task-card snapshot fields, preventing genuine review events
  from being incorrectly superseded.
- Exact claim-start repairs empty denormalized runner/topic columns from the
  immutable task-card identity, preserving migrated queue compatibility.

## [0.6.1] - 2026-07-21

### Changed

- Removed the runtime dependency on a repository-local `AITools/taskctl.py`:
  the packaged MCP runtime now dispatches task operations directly against
  the selected repository's `.aiworkhub/tasking/task_queue.sqlite` store.
- Added native task verification, review-queue, lifecycle, export, collision,
  callback-outbox, and usage-report compatibility operations.
- Made the VSIX self-contained for initialized repositories and aligned the
  Python package, MCP runtime, and VS Code extension versions at `0.6.1`.
- Added regression coverage proving the installed runtime works without an
  `AITools/` directory or a subprocess call to the legacy task controller.

## [0.6.0] - Unreleased (public release closure)

### Changed

- Removed the manual Model capabilities / GLM canary diagnostics surface
  from the dashboard editor tab (no `vscode.lm.selectChatModels` discovery
  action, no credit-consuming canary prompt) and its obsolete extension-only
  and Python static tests; the real autonomous worker adapters, model
  routing, task launch, and callback behavior are unaffected.
- Python package (`aiworkhub`, `pyproject.toml`) and VS Code extension
  (`vscode-extension/package.json`) versions aligned at `0.6.0`, including
  the extension's own `EXPECTED_MCP_PACKAGE_VERSION` runtime-compatibility
  check.
- README rewritten to open with what AIWorkHub is and a five-minute
  VS Code install/Init Repo/use quickstart, reflecting the current
  repository-local `.aiworkhub/` canonical task-store authority instead of
  the historical `AITools/taskctl.py` / `bitnnv2/data/tasking/*` parent-repo
  wrapper design.
- `docs/ARCHITECTURE.md` and `docs/GETTING_STARTED.md` added.
- `.gitignore` extended to exclude `node_modules/`, `*.vsix`, `*.sqlite`,
  `*.sqlite3`, `*.db`, and repository-local `.aiworkhub/` state.
- `SECURITY.md` and `vscode-extension/package.json` GitHub URLs corrected to
  this repository's actual current remote (`shrec/AIWorkHub`); the
  product/package/CLI identity itself stays canonical `AIWorkHub`/`aiworkhub`.

### Notes

Earlier phases (local stdio MVP, safe local automation, agent launcher,
project-switch readiness, the Task MCP -> Codex callback bridge, VS
Code-owned App Server mux/sideband transport, and the canonical
`aiworkhub` naming cutover) are tracked in detail in `MVP_ROADMAP.md`
rather than duplicated here.

[0.6.1]: https://github.com/shrec/AIWorkHub/releases/tag/v0.6.1
[0.6.2]: https://github.com/shrec/AIWorkHub/releases/tag/v0.6.2
[0.6.0]: https://github.com/shrec/AIWorkHub/compare/v0.5.0...v0.6.0
