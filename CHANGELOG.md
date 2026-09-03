# Changelog

All notable changes to AIWorkHub are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project has
noted by package/extension version and release tag.

## [Unreleased]

## [0.10.84] - 2026-09-03

This release closes the Python Source Graph attribute and member-identity blind
spot while keeping exact destinations honest and bounded.

### Fixed

- Python attributes, member writes, reads, calls, decorators and annotations
  now retain stable receiver and source-position identity, including distinct
  accesses on the same line.
- Exact member and imported-call resolution now respects lexical scope,
  shadowing, evaluation order and direct entity existence; unsupported
  inheritance remains honestly unresolved instead of fabricating a target.
- Imported-member reparsing uses authenticated indexed bytes plus pre-parse
  character/token ceilings, post-parse node/depth ceilings and linear
  control-flow traversal.

## [0.10.83] - 2026-09-03

This release makes validation capability recovery and rework retry authority
system-owned, removing another class of self-hosting task loops.

### Fixed

- A central system toolchain authority resolves declared interpreters and
  tools once and records the capability receipt used by validation.
- The coordinator can structurally replay authenticated hardlink,
  deleted-descriptor and operating-system denial cases without broadening the
  worker sandbox or duplicating a completed replay.
- A coordinator-authorized blocked-rework recovery now supersedes the exact
  failed launch episode in the identical-relaunch guard. The audited terminal
  record remains intact while the recovered card can actually run again.

## [0.10.82] - 2026-09-03

This release removes another self-hosting validation loop before returning to
the broader capability-admission work.

### Fixed

- Worker launch preflight verifies the declared interpreter, modules, working
  directory and repository inputs before model time is spent, so absent tools
  and sparse-workspace omissions become structural launch evidence.
- Landlock metadata-broker denials travel over a private, non-inheritable
  structural channel. Only measured hardlink, deleted-descriptor and OS-level
  metadata restrictions can terminate a validation as environment-blocked;
  expected policy denials still reach the validator as ordinary `EPERM`.
- Task creation reports the complete validation-role contract and rejects
  invalid priority values with the canonical enum instead of emitting partial
  or misleading guidance.
- NeedFix conversion preserves the quality contract needed by executable cards
  rather than discarding it during normalization.

## [0.10.81] - 2026-09-03

This release makes queue state, terminal outcomes and validation authority
measurable rather than inferred, while closing four recurring self-hosting
failure paths.

### Fixed

- The dashboard separates the current queue from canonical manager decisions
  and terminal task outcomes. Accepted, rejected, archived, superseded and
  finished are now reported as distinct authoritative counts, with bounded
  effectiveness rates instead of one ambiguous "Finished" number.
- An archived NeedFix whose linked task never produced an accepted outcome can
  reopen atomically. Historical archive state remains audited, while unfinished
  work no longer disappears behind a stale task link.
- Declared Python validator commands resolve through one interpreter authority.
  The executed argv and the authority receipt now agree, module validators use
  the safe `-P -m` form, escaping runtime symlinks fail closed, and Windows,
  macOS and Linux share the same tested contract.
- App-server mux shutdown no longer depends on raw platform probes or a blocked
  stdin reader. Cooperative wake-up and shutdown checks now go through the
  platform facade.
- Retention preserves adjudicated outcomes, and workforce routing scores routes
  from measured accepted results rather than ungrounded availability alone.

## [0.10.80] - 2026-09-02

The release where the surfaces stopped saying "unknown" about things they had
never looked at. Five defects landed in one wave, and four of them are the same
shape: a fact existed, and the reader looked somewhere else.

### Fixed

- The cost ledger reports the repository it is bound to. `aiworkhub_task_cost_ledger`
  was the only caller of `build_cost_ledger` that omitted `repo_root`, so it
  parsed the human-readable usage report -- whose lines carry no model, no
  provider and no timestamp -- and every dimension its docstring promises read
  `unknown`. Measured on the same data at the same moment: 590 rows / 1 model /
  1 provider / 1 day / 0 usage-observed / 0 routes, against 3,509 / 28 / 8 / 36 /
  2,267 / 13. Nothing had to be built; the attribution already existed and was
  unreachable from the one tool that needs it. Routing by cost and difficulty
  now has a measurement behind it.
- A rejection that asks for rework can be learned from. `_request_matches_candidate`
  read only `accepted_request_id` and `terminal_review`, and `reject_review --to
  pending` stamps neither -- so only a rejection that *terminated* a card was
  ever committable, and the common case, feedback to a worker, was not. The
  adjudicated request id was on the card twice, in `rework_predecessor` (pinned
  with the predecessor's changed-path hashes) and in `review_feedback` (carried
  with the reason's sha256). Both are written by `reject_review` itself, never
  supplied by a model, so the lesson stays bound to exactly the request that was
  judged.
- Process status names why it did not read a task card. During a pid-null
  starting reservation the read is deliberately skipped to avoid contending
  with the preparation owner -- correct -- but "not read", "read and absent"
  and "read raised" all reported as `task_state: unknown` with an empty card.
  `task_card_read` now says which, and carries through `collect()`, the surface
  a manager actually calls.
- Workspace GC keeps the workspace `retry_finalization` exists to use, bounded
  by whether a retry can still act. A `finalize_failed` process state retains a
  `blocked` or `pending` card's workspace -- `blocked` is the measured race the
  rule exists for, nine seconds between a failed finalization and the sweep that
  removed its only recovery path -- but a `finished` or `archived` card
  completed by another route, so its workspace is collected. The first form of
  this rule ignored the card entirely and contradicted a canonical test that had
  asserted the opposite since B863; CI caught the pair on the 0.10.79 tag.

### Added

- The skill registry has durable storage and a lifecycle: `skill_registry_store`
  persists proposed and active skills with a compare-and-swap advance, and the
  manager tools can propose, add evidence and activate. Activation requires
  independent accepted evidence from distinct identities, so no single actor can
  certify its own skill.


## [0.10.78] - 2026-09-02

The release that made the task loop close by itself. A card now goes from
worker to accepted without a human driving each step, and the nine tests that
stood red in canonical are green.

### Fixed

- A worker that went quiet could never be finalized. The launcher appends an
  advisory runtime notice after ten minutes without output, and that row
  carries `pid` but no `pid_start_ticks`; both finalizers read PID identity
  from the last row, so identity became UNKNOWN and UNKNOWN defers. The longer
  a worker worked quietly, the more permanent its deferral. Identity now comes
  from the merged request history. Cancellation had the same defect and the
  same fix -- and there the tail row was about to receive SIGTERM.
- The reconciler is the only component that finalizes an exited worker, and its
  health lived in one process's memory behind a silent `except: pass`. A
  reconciler that never started and one working normally were indistinguishable
  from every surface. Each scan now writes a durable record, on success and on
  failure, announced before the pass runs and reported stale when too old.
- Usage recording required the card to still be `processing`, but the finalizer
  moves a successful worker to `review` first -- so every worker that SUCCEEDED
  had its cost measured and discarded. The ledger held failures only, and every
  by-model and by-provider dimension read "unknown". Spend is now accountable
  to the claim, not to the card's current status.
- The review orchestrator attempted and failed one lifecycle action per pass
  for cards that had already left review, and a failed action parks every later
  action in its chain: 129 chains and 1,389 actions permanently unreservable.
  An action whose target is decided now retires as finished, because there was
  nothing to do.
- `append_event` canonicalised a failure-terminal row on a private copy and
  returned nothing, so a finalization and its own replay disagreed about why a
  worker died.

### Added

- `declared_invariants` executes four rules `development_rules.json` only
  stated in prose -- one owner for the terminal vocabulary, one predicate per
  policy, bounded module caches, sqlite context managers that close. Run
  against this session's starting commit it reports 11 real violations; against
  the current tree, zero. Wired into `quality.json`.
- A durable skill registry store. The lifecycle was complete and tested and had
  nowhere to keep a record, so evidence could never accumulate toward
  activation and the dashboard panel was structurally empty. Records persist
  with immutable `(identity, version)`, a content digest that cannot be
  rebound, and a state digest over the runtime fields the content digest
  deliberately excludes.
- `accept_review` and `reject_review` return `learning_commit_owed`, naming the
  lesson a decision owes with the arguments the commit tool requires. Coverage
  is reported on `aiworkhub_task_health`: it read 1 percent.
- Card templates derive the repository gates a package change trips, instead of
  asking an author to recall them -- and exclude the one gate a worker's sparse
  checkout cannot execute.

### Performance

- Retained-workspace GC stopped replaying a 558 MB ledger once per candidate to
  read a single row: 38.2 minutes to 13.5 seconds. Finalization and that sweep
  now run on separate cadences, so a finished card's time-to-review no longer
  waits on garbage collection.
- Storage readiness caches its integrity scan and never its authority: 94 calls
  fell from 11.0 s to 0.219 s.
- The 90-day git walk left the Source Graph index write transaction, where it
  had been holding the writer lock for 93.8 percent of a build.

### Notes

- Source Graph resolution rose from 33.3 to 41.8 percent, and `gaps` stopped
  reporting Python builtins as missing repository work.
- Two detectors were measuring the wrong thing and now do not: the
  OS-dependency boundary counted prose (12 of 153 matches were docstrings,
  including the file that declares the rule), and five test fixtures patched
  `os.read`/`os.replace` process-wide while counting calls, racing every
  background thread.
- `process_launcher` shed exact process identity into its own module,
  14,675 to 14,413 lines; both ratchets descend with it.


## [0.10.77] - 2026-09-01

### Added

- Terminal ledger failure records persist a bounded fixed-key terminal_reason
  and preserve the caller-supplied value in a bounded raw side field, so no
  terminal failure is mute and no unbounded payload is persisted.
- Named sandbox-impossibility causes classify as environment-unsupported,
  machine-separable from real candidate failures; generic permission prose is
  never promoted.
- One canonical runner-id grammar folds variant runner spellings onto the
  registered identity at workforce upsert and rejects conflicting variants
  with a named reason.

### Fixed

- The launcher canonical workforce now covers every enabled claude-family
  route, ending workforce_route_absent kills on pinned-model launches; the
  catalog-launcher parity regression is strict instead of skipping
  route-absent workers.
- The cost ledger fills placeholder usage rows from durable process-event
  evidence matched on exact identity, never overwriting observed telemetry.
- The storage-retention preview overlaps its three I/O components on a
  measured worker count under the existing single-flight fence.
- Process-group launch and termination consolidate under platform_io,
  reducing the direct OS dependency boundary from 153 to 149 calls.

### Notes

- 0.10.76 was prepared but never tagged; its changes ship in this release.
- Open audit lines remaining: NF-548 zero-delta tripwire and
  identical-relaunch guard, NF-547 orchestrator readiness gate (in flight),
  reviewer report in-run validation, rework-replay receipt preflight.

## [0.10.76] - 2026-09-01

### Added

- Quality-review Source Graph partition readiness is honored before review work runs.
- Canonical platform OS primitives are available, with OS dependency ratchets
  covering the full source tree.

### Fixed

- Credential permission calls and remaining chmod facades now use central
  consolidation points.
- Agent process-list responses are bounded, stale terminal task families archive
  safely, and live usage binds to exact task claims.

### Notes

- Remaining process-callsite interface migration, full consolidation, B5 preview
  measurement, NF546 and other pending work are not complete in this release.

## [0.10.75] - 2026-09-01

### Added

- Platform process launching now runs through a facade with direct-import parity.
- Windows authorization has a byte-exact ACL snapshot foundation.
- Source Graph publishes the mode catalog with retained overlay digest authority.

### Fixed

- Retained review and validation candidates are recovered consistently.
- Stale-recovery paths preserve CAS truth when reconciling retained state.
- Validation package-support inputs are materialized for release assurance.

### Notes

- This release does not claim completion of Windows ACL mutation/restore,
  DevRules ratchet replay, validation-tool authority, broker-boundary work or
  remaining staged work.

## [0.10.74] - 2026-08-31

### Added

- Reviewers can read their authenticated, evidence-bound input packets through a
  dedicated packet-read authority.
- Windows read-only authorization uses ACL snapshots with bounded ACE and SID
  parsing.

### Documentation

- The generated Source Graph mode catalog is freshness-checked and documents the
  canonical query-mode surface.

### Notes

- This release does not claim completion of remaining Source Graph work, Windows
  platform work or broader platform consolidation and mechanical standards work.

## [0.10.73] - 2026-08-31

### Fixed

- Baseline diagnostics are compared with parity across validation paths, so
  unchanged accepted findings remain distinct from newly introduced regressions.
- Bugfix templates enforce every required output before completion, preventing
  partially materialized fixes from satisfying the template contract.

### Notes

- This release does not claim completion of remaining Source Graph
  retrieval/evaluation work or Windows ACL/AppContainer integration.

## [0.10.72] - 2026-08-31

### Added

- Source Graph persists authenticated accepted-outcome receipts, and release
  qualification exercises the same receipt contract used by accepted outcomes.

### Fixed

- Dead processing claims are reconciled only within their bound task scope, with
  explicit authority for the reconciliation path.
- Windows child operations derive authority relative to the parent, keep
  directory authority on native handles, and bind child disposition to the
  authorized handle.
- Native Windows disposition failures preserve their platform error behavior.
- Process-launcher acceptance helpers now live behind an extracted boundary, and
  the launcher size ratchet is restored.

### Notes

- This release does not claim completion of remaining Source Graph
  retrieval/evaluation work or Windows ACL/AppContainer integration.

## [0.10.71] - 2026-08-30

### Added

- Task updates may preserve an unchanged residual scope, allowing lifecycle
  progress without manufacturing a scope change.
- Expected file bytes are extracted only from authenticated evidence, and outer
  continuation pagination is bounded by signed cursors with exact item counts
  and deterministic page reassembly.

### Fixed

- Source Graph recovery can discard a locked prior-build probe while preserving
  the readable generation, rather than destroying usable graph evidence.
- Continuation cursors reject tampering before pagination resumes.

### Documentation

- Public Source Graph documentation now reports the correct 48 query modes.

### Tests

- CI exercises the same pagination contract as production, including exact
  counts, multi-page reassembly and cursor-tamper authentication.

## [0.10.70] - 2026-08-30

### Fixed

- Reviewer launches now materialize immutable input packets before execution, so
  every reviewer receives the exact evidence-bound inputs selected by the
  supervisor.
- Repository discovery preserves the authority of an existing valid
  `.aiworkhub/project.json` and no longer falsely reports initialized
  repositories as uninitialized on Windows, Linux or remote workspace hosts.
- The dashboard reports degraded or unavailable storage as an operational
  failure rather than a missing repository. A canonical but older schema is
  identified separately and offers an explicit project-schema upgrade action.

### Tests

- Extension manifest-discovery fixtures now exhaustively mirror production
  behavior for valid, missing, malformed and host-specific repository manifests.

## [0.10.69] - 2026-08-30

### Added

- Supervisors now own authenticated context receipts and enforce the live Source
  Graph gate before worker completion, with provider receipt envelopes binding
  authority, request identity and evidence to their authenticated payloads.
- Workspace and task input preflight is a mechanical, deterministic step, and
  mypy validation uses only the canonical trusted interpreter selected for the
  repository.

### Fixed

- Validation compares diagnostics with the accepted baseline so unchanged
  findings remain distinguishable from regressions.
- Review-finding ingress normalizes identities before an idempotent write, and
  accepted targets close exactly linked NeedFix findings without duplicating
  lifecycle effects.
- Reserved reviewer launch failures now reconcile to a terminal lifecycle result
  instead of leaving leased review work stranded.
- Process-launcher validation was extracted into a focused module with a size
  ratchet that keeps the launcher boundary from growing back.

### Tests

- Coverage exercises context and Source Graph enforcement, baseline diagnostics,
  deterministic preflight, trusted mypy selection, normalized finding replay,
  linked NeedFix closure, launcher extraction, reviewer launch reconciliation
  and authenticated provider receipt envelopes.

## [0.10.68] - 2026-08-29

### Added

- The canonical task database now owns a durable review-chain outbox covering
  three sequential review lenses, target acceptance, archival and linked
  NeedFix closure with leased, replayable action receipts.
- Review orchestration selects each reviewer from the live workforce contract
  instead of a hard-coded vendor or runner and resumes pending work after a
  process restart without creating a second lifecycle database.

### Fixed

- Reviewer models no longer have to remember or invoke the mandatory report
  submission tool. The supervisor ingests their structured terminal report,
  canonicalizes its evidence and persists the authenticated receipt itself.
- Automatic review waits return their lease immediately, verify exact target,
  reviewer, lens, packet, provider and submission bindings, reject actionable
  findings before acceptance, and archive every accepted reviewer and target.
- Accepted and archived targets now resolve every exactly linked
  `task_created` NeedFix as the final durable lifecycle action; tasks without a
  linked finding complete the same step idempotently.
- Provider readiness distinguishes launch eligibility from authenticated,
  observed route success, so zero-history DeepSeek and GLM routes are not
  advertised as available or selected prematurely.

### Tests

- Windows workforce and LM-discovery parity tests now assert the same observed
  provider truth as the canonical workforce catalog.
- Review lifecycle coverage exercises restart replay, route selection,
  terminal receipt authentication, finding rejection, ordered archival and
  exact linked-NeedFix cleanup.

## [0.10.67] - 2026-08-29

### Added

- Worker capacity now adapts to the process CPU and affinity budget, reserves
  capacity on multi-CPU hosts, honors explicit and soft caps, and reports the
  applied ceiling while keeping nested pools constrained.
- Decomposition previews now produce approval-required proposals with a stable
  boundary identifier and the large, low-confidence action class; a closed
  action-class enum establishes the foundation for other high-impact cases.
- A provider-response contract foundation normalizes untrusted responses into
  immutable detached events, preserves unknown types, rejects non-JSON and
  non-finite data, and produces canonical bytes, digests and error categories.

### Fixed

- Nested Landlock validation accepts outer authority only from an authenticated
  request-owned worktree locator and rejects ambient scratch-directory reuse;
  metadata-broker hardlink no-ops remain bound to authenticated descriptors.
- Windows AppContainer supervision owns the exact native process and job
  handles, preserves terminal results, terminates process trees, and closes
  handles and pipes idempotently without inventing a return code.
- Callback-store readiness and WAL-setup failures no longer expose SQLite,
  filesystem or payload details; callers receive bounded categories for WAL
  operations and exhausted lock retries, and cleanup preserves the primary
  database-open failure.
- Manager archive and supersede operations attribute their actor to the
  verified manager provider while retaining write-gate enforcement.

### Changed

- Provider read-efficiency parsing and reporting now live in a focused launcher
  module with parity coverage and a descending module-size ratchet.

### Tests

- Windows lock-timeout coverage isolates the target descriptor attempts so
  unrelated lock calls introduced by Python 3.14 do not cause false failures.
- Source Graph capacity coverage now asserts the applied worker ceiling.

## [0.10.66] - 2026-08-29

### Added

- Bootstrap now publishes a responsibility matrix and construction templates,
  so manager and worker roles begin from explicit, bounded authority.
- Scoped audit coverage records the relevant tool and change boundaries, while
  Source Graph traversal fails closed when it cannot establish that authority.
- Pending context intents retain Session, Memory and KB work that cannot yet be
  resolved, instead of silently discarding it.

### Fixed

- Reviewer preparation enforces its bounds and preserves replay-context
  evidence; recovery reconciles CAS state, raw-terminal outcomes, launch-ledger
  records and retained-terminal work without fabricating a result.
- VS Code repairs malformed repository configuration and revives a retained
  dashboard after a recoverable runtime interruption.
- The runtime uses the trusted interpreter, exposes discovered model settings,
  and binds CaaS corrections to their verified evidence.

## [0.10.65] - 2026-08-28

### Added

- A fail-closed Windows AppContainer lifecycle foundation now provides
  deterministic container identity, shell-free process launch, job ownership,
  structured lifecycle results and cleanup evidence. It remains behind the
  existing runtime gate and is not yet wired into native worker launch.

### Fixed

- Quality-review packet construction separates authenticated narrative evidence
  from explicit read-only filesystem authority, so prose is never materialized
  as a missing path and pre-provider failures terminalize cleanly.
- Reviewer scope is constructed before immutable prose evidence is added,
  keeping changed-path evidence ordering deterministic.
- Immutable prose evidence is bound into the sealed reviewer contract and the
  canonical packet digest is refreshed before persistence.
- Legacy Source Graph migrations open their source SQLite database through a
  read-only URI, preventing source mutation and WAL/SHM sidecar creation.
- Source Graph daemon documentation now states the tested refresh serialization
  invariant without implying that concurrent callers join an in-flight build.
- The Models settings modal preserves its visible shell, pending identity and
  prior rendered state while repository settings are loading.
- Authenticated task-template validation and role overrides are sealed into the
  template contract, preserving their exact authority through validation.
- The native Repository Settings dialog no longer opens as a blank frame:
  content-addressed webview assets and an intrinsic layout keep the wrapped
  footer visible while only the settings list scrolls.

### Known limitations

- `AIWORKHUB_01065_NF453_GLM_VSCODE_LM_TOOL_LOOP_V5` remains unresolved.
  Its candidate was not promoted, and this release makes no claim that the GLM
  VS Code LM tool loop is fixed.

## [0.10.64] - 2026-08-27

### Fixed

- Repository model settings keep a stable modal body, dimensions and rendered
  state while provider/model switches change, avoiding blank or black redraws.
- Quality-review sparse workspaces now materialize declared read-only inputs,
  so reviewers can execute candidate regressions against immutable runtime
  dependencies instead of reporting missing-file false positives.

### Changed

- The manager now freezes scope and cuts an intermediate release after several
  important blocker fixes land while the owner is actively present, then
  continues development on the next version.

### Known limitations

- NF-2026-00467, NF-2026-00474, NF-2026-00475, NF-2026-00478 and the final
  NF-2026-00482 review remain explicitly deferred to 0.10.65; their unaccepted
  isolated-workspace deltas are not included in this release.

## [0.10.63] - 2026-08-27

### Added

- Native Codex capability probing now observes authenticated model catalogs
  without inference calls and keeps repository routing aligned with enabled
  provider policy.
- Production failure learning classifies provider, runtime, validation and
  candidate-code failures into durable evidence for later routing decisions.
- Daily worker outcomes expose the complete terminal category set instead of
  collapsing activity into three broad buckets.

### Fixed

- The model-settings selector keeps a stable responsive layout and visual
  state while provider and model toggles change.
- Native Codex quality reviews are admitted through the canonical runner/topic
  policy, and explicit audited launch-identity reroutes can recover a terminal
  retry without replacing its task history.
- Context capture distinguishes a disabled feature from a real enabled skip,
  preserving truthful counters and generator consumption.
- Runtime owner-manifest reads revalidate file-descriptor and path identity
  after bounded reads, closing replacement races before parsed data is used.
- Hash-pinned validation replays and required-output checks preserve their
  exact scope while allowing provider-free deterministic verification.
- Source Graph documentation now derives its advertised mode count from the
  canonical 37-mode contract.

### Known limitations

- Nested Windows validation metadata handling and the dependent native Windows
  AppContainer runtime remain excluded from this release while NF-2026-00448
  stays open; no completed AppContainer isolation claim is made for 0.10.63.

## [0.10.62] - 2026-08-27

### Fixed

- Required-output validation now reports missing artifacts, unchanged mandatory
  outputs, scope violations and passing records together, preventing one-error
  rework loops in the production worker finalization path.
- Sparse JavaScript validation follows bounded local CommonJS dependencies,
  including explicit and extensionless JSON modules, while bare Python
  validators use the trusted coordinator interpreter.
- Validation scratch failures are classified as refused metadata operations
  only when every candidate carries that measured restriction; mixed failures
  remain fail-closed under their original error.
- A measured self-hosting break-glass rule now authorizes the manager to apply,
  validate, package and install only the smallest Task MCP/plugin recovery fix,
  then requires an immediate return to canonical task flow.

## [0.10.61] - 2026-08-26

### Fixed

- The production dashboard now loads, validates and projects the repository's
  bounded `.aiworkhub/config/development_rules.json` manifest instead of
  reporting `No sample` while valid rules already exist.
- Development Rules telemetry now exposes the canonical manifest version,
  digest, declared rule count and resolved default-context count.

## [0.10.60] - 2026-08-26

### Added

- Repository development-rule manifests now give workers measurable hot-path,
  multicore, allocation, caching and validation constraints.

### Fixed

- Claude subscription workers recover one exact structured stale-credential
  401 by atomically refreshing only the request-local credential projection
  and retrying the same request once; repeated or non-auth failures remain
  fail-closed and secrets are never persisted in evidence.
- Same-request Claude recovery keeps the replacement supervisor visible and
  cancellable instead of allowing the previous monitor to delete its live
  process registration.
- C, C++ and CUDA sparse workspaces resolve tracked local headers through
  declared include roots while rejecting absolute paths, traversal and
  symlink escapes.
- Hosted rework and nested validation use current canonical dependency
  authority, and supervisor progress clocks remain monotonic.

## [0.10.59] - 2026-08-26

### Fixed

- Quality-review launch now keeps one exact request from durable reservation
  through provider spawn, skips the duplicate claim, verifies the persisted
  read-only reviewer card and rejects target/lens identity reuse.
- Sparse candidate worktrees resolve the trusted worker supervisor from the
  installed host package instead of failing before the provider can start.

## [0.10.58] - 2026-08-25

### Fixed

- Quality-review launch acknowledgement now follows durable task creation,
  exact claim and request binding, so deferred reviewer launches cannot leave
  invisible `starting` requests or pending/unclaimed ghost cards.
- Same-ID reviewer retries reconcile the exact bound request, while foreign
  request IDs fail closed and live uncapped providers remain untouched.

## [0.10.57] - 2026-08-25

### Fixed

- Worker launch now provisions each request-owned temporary authority before
  composing Landlock rules, so Claude, Grok and other provider runtimes can
  create arbitrary private temp subdirectories without host-temp access or
  provider-specific sandbox exceptions.

## [0.10.56] - 2026-08-25

### Fixed

- Worker and validation subprocesses now use request-owned temporary roots and
  a verified nested sandbox/broker boundary, eliminating host-temp leakage and
  repeated Landlock validation loops while preserving cleanup evidence.
- Source Graph daemon lifecycle reporting now retries stale query connections
  only when ownership is provably dead, keeps live-holder failures fail-closed,
  and makes health, startup and shutdown agree on terminal process truth.

## [0.10.55] - 2026-08-25

### Fixed

- Task templates now classify validation inputs by language and file type, so
  non-Python fixtures are not accidentally passed to Python-only linters.
- Source Graph audit telemetry separates authenticated live worker calls from
  injected prefetch evidence across call, repeat, zero-hit and discipline
  metrics without weakening the required-tool gate.
- Provider quota and authentication failures are derived only from sealed
  provider-owned evidence and open an exact route-local circuit, preventing
  known-unavailable routes from being selected repeatedly while leaving
  sibling routes and the MCP control plane available.

## [0.10.54] - 2026-08-25

### Fixed

- Model workers are uncapped by default end to end: supervisor and process
  finalization no longer mint new token-budget terminal outcomes from legacy
  metadata, while historical evidence remains readable.
- Reviewer launch and terminal settlement preserve exactly-once intent across
  ledger churn, stale reservations, callback recovery and interrupted manager
  sessions instead of leaving review work stranded.
- Explicit predecessor recovery now verifies true zero-diff retained
  workspaces mechanically, allowing validation-failed rework without fabricated
  hashes while retaining bounded fail-closed checks for drift and malformed
  evidence.
- Sparse validation workspaces seed current canonical Python dependencies and
  carry nested-validator authority coherently, preventing stale-HEAD imports
  from turning valid candidates into repeat validation loops.

## [0.10.53] - 2026-08-24

### Added

- Dashboard coding-foundation cards expose repository rules, skills and typed
  tool-recipe evidence without replacing truthful unavailable states.

### Fixed

- Source Graph `bodygrep` now filters on bytes before decoding and streams
  matching lines, preventing repeated 32 MiB scan bursts from permanently
  inflating the long-lived MCP server heap.
- Source Graph query caching is bounded and evicts obsolete index generations
  instead of retaining unreachable results after each daemon refresh.
- Source Graph daemon shutdown terminates and reaps the exact spawned process
  tree, including abnormal and interrupted lifecycle paths.

## [0.10.52] - 2026-08-24

### Fixed

- Skill Runtime Packet credential filtering now rejects every Unicode format
  character, preventing ZWJ, ZWNJ, word-joiner and BOM key-splitting bypasses
  across all emitted instruction fields.

## [0.10.51] - 2026-08-24

### Added

- Repository Skills now support deterministic, bounded task-aware selection
  receipts with exact identity, version, digest and reason binding.
- Selected ACTIVE skills can be resolved into canonical bounded runtime
  packets without filesystem, database, network or execution side effects.

### Fixed

- Runtime-packet credential filtering now rejects Unicode whitespace and
  fullwidth assignment delimiters in addition to fused, camel, Pascal,
  snake, dotted and dashed secret-key families.

## [0.10.50] - 2026-08-24

### Added

- Repository-local Development Rules, Skill Registry and Typed Tool Recipe
  foundations provide deterministic, versioned coding contracts without
  repeating model-authored boilerplate.
- Dashboard snapshots expose bounded, truthful evidence for rules, skills and
  recipes while preserving unknown, unavailable and unmeasured states.

### Fixed

- Coding-foundation projections now derive structured-field and free-text
  credential redaction from one taxonomy, including compound and
  `secret_key` variants without hiding benign cache keys.
- Bounded iterable projections distinguish an exact limit from truncation with
  one extra probe and ignore untrusted length claims.

## [0.10.49] - 2026-08-23

### Fixed

- Provider and quality-review launch timeouts are now explicitly retained as
  non-enforcing compatibility metadata; an exact live process is never killed
  or terminalized merely because elapsed or quiet time crossed a legacy bound.
- Legacy VS Code LM timeout text and unauthenticated supervisor `timed_out`
  state can no longer override exact process/result evidence.
- Nested validation replay now uses an authenticated coordinator-planted
  authority marker, while GitHub-hosted Landlock limitations remain isolated
  from real local sandbox regressions across the Python matrix.

## [0.10.48] - 2026-08-23

### Fixed

- Worker supervision no longer treats an elapsed legacy timeout as provider
  death; live workers continue until an authenticated terminal result, exact
  process exit, explicit cancellation or an enforceable token-budget event.
- Nested validation helpers now inherit the exact request-owned scratch root,
  preventing false Landlock denials for temporary Git repositories while
  canonical repository writes remain forbidden.
- Switching a pending rework from validation-only replay to an ordinary worker
  consumes the stale one-episode replay authorization atomically instead of
  routing the next launch back into the completed replay lane.

## [0.10.47] - 2026-08-23

### Fixed

- Worker MCP audit evidence now binds every accepted provider tool call to an
  authenticated provider-call identity and explicit live provenance across
  the Python and VS Code execution paths.
- Concurrent duplicate tool events are sealed once through deterministic
  single-flight accounting, while prefetch, cache and live calls remain
  distinct in audit metrics and required-tool gates.
- HMAC-valid rows with malformed identity or provenance now fail closed before
  aggregation and cannot inflate tool-use evidence.

## [0.10.46] - 2026-08-23

### Added

- Template-first task creation now persists the exact template identity and
  normalized contract provenance atomically with the canonical task card.

### Fixed

- Template-derived validation, role and required-output contracts are bounded
  and language-correct before task insertion, eliminating partial cards and
  mechanical cross-language validation failures.

## [0.10.45] - 2026-08-22

### Fixed

- Explicit rework rejection now binds a supplied predecessor request to the
  supplied task identity instead of falsely reporting a missing `task_id`.
- NeedFix, the Task DAG and completion inbox now share one bounded,
  identity-checked terminal-artifact projection, automatically hiding landed
  reviewer and implementation retries while retaining unresolved work.

## [0.10.44] - 2026-08-22

### Fixed

- Accepted task artifacts now close through one crash-safe retention lifecycle:
  retained predecessor pins are released only after canonical acceptance, and
  protected review, replay and quarantine workspaces remain fail-closed.

## [0.10.43] - 2026-08-22

### Added

- Task creation now resolves deterministic contract templates through the
  canonical registry, including exact read/write scope, required outputs and
  validation roles.

### Fixed

- Explicit rework predecessors now bind to strict typed paths, hashes and the
  current claim epoch before a retained delta can be consumed.
- The first worker Source Graph call performs one bounded MCP-readiness wait
  without replaying or duplicating the requested tool execution.
- Reviewer findings accept canonical objects or one bounded JSON-object string;
  rejected intents are HMAC-authenticated, never become verified payloads, and
  a rejected finding cannot be replaced by an empty successful submission.

## [0.10.42] - 2026-08-22

### Added

- Added a deterministic task-template registry for common task classes, so
  read/write scope, required outputs and validation contracts are normalized
  before a worker is launched.
- VS Code staged edits now carry exact required-output progress and finalize
  only as one complete atomic envelope after every required path is staged.

### Fixed

- Python validation commands now resolve only recognized repository virtualenv
  interpreter spellings across POSIX and Windows, with explicit interpreter
  authority recorded in validation receipts.
- Incomplete staged edits retain one bounded correction opportunity, reject
  extra paths, and fail closed after the correction is exhausted.

## [0.10.41] - 2026-08-21

### Fixed

- Repeated provider-free validation replays now bind their one-episode
  authorization to the latest retained predecessor and claim epoch. A prior
  recovery event can no longer strand a pending rework card in a permanent
  `validation_only_replay_predecessor_mismatch` loop.

## [0.10.40] - 2026-08-21

### Fixed

- Sparse Python validation worktrees now include the bounded transitive closure
  of repository-local static imports. Candidate tests no longer fail during
  collection because package modules such as `core.py` were omitted, while
  unrelated modules and the rest of the repository remain unmaterialized.

## [0.10.39] - 2026-08-21

### Fixed

- Kilo's exact JSONL final-text event is now recognized as meaningful
  read-only research output. Successful Grok workers no longer end in a false
  `research_result_missing` validation failure, while tool chatter, reasoning,
  malformed envelopes and arbitrary nested prose remain fail-closed.

## [0.10.38] - 2026-08-21

### Added

- Added the native `grok_kilo_cli` route for the exact `xai/grok-4.6` model,
  including deterministic Kilo executable discovery, repository-local model
  controls, workforce routing and truthful preflight observability.
- Added an xAI-only credential projection into each request's isolated Kilo
  HOME and generated a private Kilo MCP configuration for worker tools.

### Security

- Grok workers use request-scoped HOME and XDG data/config/cache roots; no
  ambient Kilo providers, credentials or caches enter the sandbox.
- Launch evidence contains only bounded, secret-free xAI projection metadata.

## [0.10.37] - 2026-08-21

### Fixed

- Authenticated, fresh Source Graph calls against a request-scoped rework
  worktree now satisfy the live code-task gate instead of being misclassified
  as injected-only context and forcing a false `validation_failed` loop.

## [0.10.36] - 2026-08-21

### Fixed

- Release validation now recognizes the exact pytest runtime already loaded by
  the active interpreter even when an ephemeral CI toolcache exposes its
  `site-packages` root with permissive mode bits. Arbitrary writable roots
  remain rejected and the admitted runtime is bound read-only.

## [0.10.35] - 2026-08-21

### Fixed

- Pytest validation now falls back from an absent configured user site to the
  exact trusted pytest package root of the active virtualenv, keeping sparse
  candidate validation portable across GitHub Actions Python 3.12–3.14.

## [0.10.34] - 2026-08-21

### Fixed

- Sparse Python validation now carries its own `pyproject.toml`, preventing
  pytest from walking into the parent canonical repository and importing the
  wrong `src` tree.
- Provider-free validation replays include hash-pinned inherited Python files
  in candidate import authority by comparing them with the canonical parent
  baseline instead of the already-overlaid workspace baseline.

## [0.10.33] - 2026-08-21

### Fixed

- Worker launch metadata and rework-delta seals now use the exact task card
  committed by `claim_start_exact`, with bool-safe epoch and request identity
  validation instead of the stale pre-claim card.
- Sparse Python validation workspaces seed the AIWorkHub package anchor for
  every scoped package file and put candidate `src` bytes ahead of the
  canonical editable install without allowing a candidate pytest shadow.

## [0.10.32] - 2026-08-21

### Fixed

- Retained-workspace quarantine now validates an exact canonical lowercase
  hexadecimal request ID before creating a destination or moving any bytes,
  preventing path-component escape and ambiguous quarantine names.
- Validation-card preflight now recognizes spaced and adjacent shell-chain
  operators (`;`, `&&`, `||` and operator runs) without executing a shell, so
  an unbounded pytest command cannot hide in another command segment.
- Live output renders bare-string reasoning deltas while preserving real result
  and nested tool-result payloads when they also carry auxiliary reasoning
  metadata.

## [0.10.31] - 2026-08-21

### Fixed

- Retention reconciliation now recognizes a mechanically verified read-only
  zero-diff `validation_failed` review without requiring a non-existent hash
  map. The original MCP gate failure remains authoritative instead of being
  replaced by `retained_workspace_quarantined:review_workspace_hashes_missing`.
- Writable and review-ready candidates still require sealed path hashes and
  retain the existing fail-closed quarantine behavior on ambiguity.

## [0.10.30] - 2026-08-21

### Fixed

- Workforce quality evidence now excludes launch, transport, timeout and
  finalization infrastructure failures while retaining validation failures as
  model-quality evidence. High-assurance routing is no longer poisoned by
  control-plane incidents.
- Repository-disabled model routes are excluded from aggregate Preflight
  coverage as well as workforce selection, so an intentionally disabled CLI
  route cannot make an otherwise healthy repository appear degraded.
- Manager reload rebinding now prunes callbacks whose task is no longer in the
  matching terminal episode instead of repeatedly carrying historical pending
  wakes into the active manager backlog.
- The query-only reviewer Source Graph regression now scopes its build spy to
  the candidate overlay, preventing an unrelated concurrent background index
  from causing a false suite failure.

## [0.10.29] - 2026-08-21

### Fixed

- Required project-context tools are now a blocking authenticated MCP gate for
  read-only/research cards as well as code cards; provider prose cannot stand
  in for missing Session Manager, AI Memory or KB ledger receipts.
- Explicit rejection of a current zero-diff review request now resolves the
  same canonical predecessor evidence as automatic selection.
- A cold Windows finalization probe gets one bounded warm-up interval and
  reports `probing` instead of a transient false `Blocked` state.

## [0.10.28] - 2026-08-21

### Fixed

- A verified foreground Codex chat can atomically transfer its manager route
  from the current live repository to another live repository without the
  target already claiming that thread. A shared ownership fence uses
  monotonic epochs, rejects stale/foreign and concurrent losers, and rolls
  back exactly if target services fail to converge.
- Quality-evidence provenance now uses the same explicit bounded-truncation
  serializer in both canonical representations; short provenance remains
  byte-for-byte unchanged.

## [0.10.27] - 2026-08-21

### Fixed

- Rejecting a validation-failed review no longer gets trapped by a malformed,
  missing, unsealed or tampered rework-delta descriptor. Untrusted delta bytes
  are discarded, the task returns to pending from canonical HEAD, and the
  disposition reports the exact no-reuse reason.
- Valid sealed rework deltas retain their existing authenticated reuse path;
  malformed evidence cannot weaken workspace, hash or allowed-write checks.

## [0.10.26] - 2026-08-21

### Fixed

- Repository Settings now has one bounded scrolling body with a fixed dialog
  header/footer, sticky tabs, responsive model rows and stable scroll position
  after a model switch updates the policy revision.
- Plan DAG defaults to current actionable work instead of rendering all task
  history at once. A searchable All history view preserves audit access, while
  compact cards open the canonical full Task Detail on selection.

## [0.10.25] - 2026-08-21

### Added

- Repository Settings now projects the live VS Code/Copilot model catalog as
  individual model switches instead of showing only statically configured
  workforce rows.

### Fixed

- Copilot is a distinct repository policy owner from native model vendors.
  Disabling Copilot now excludes every editor-hosted route while native
  Codex, Claude and provider routes remain independently controllable.
- Exact Copilot model switches use one normalized `vscode_lm` policy identity,
  so disabling one discovered model does not disable its siblings.

## [0.10.24] - 2026-08-21

### Added

- Repository Settings now includes a Models tab with provider and exact-model
  switches stored in `.aiworkhub/config/models.json`. Provider and adapter
  disables are hard gates, and workforce ranking cannot select a disabled
  route.
- The dashboard header now reports durable Manager Context Graph query calls,
  hits and returned bytes alongside graph event/node counts.

### Fixed

- Model-policy updates use revision compare-and-swap under a cross-process
  advisory lock, preventing concurrent VS Code windows from losing changes.

## [0.10.23] - 2026-08-21

### Fixed

- A timed-out or failed `git worktree add` no longer launches a second Git
  subprocess for global pruning. Partial exact registrations and request-owned
  directories use the same process-free cleanup authority as normal teardown.
- Detached/manual validation workspaces without Git registration remain
  safely cleanable, while mismatched reciprocal registrations still fail
  closed and preserve evidence.

## [0.10.22] - 2026-08-21

### Fixed

- Workspace cleanup no longer launches blocking `git worktree remove` or
  global `git worktree prune` subprocesses. It verifies the exact reciprocal
  worktree registration and removes only the request-owned workspace and Git
  administrative directory.
- Windows finalization Preflight is nonblocking and coalesced per repository,
  adapter and HEAD. Concurrent dashboard/MCP refreshes share one background
  probe; successful and failed results are published with bounded caches and a
  running probe can never be reported as Ready.

### Performance

- The release-host live workspace canary created its isolated worktree in
  about 18 ms and cleaned it in under 1 ms without a cleanup Git subprocess.

## [0.10.21] - 2026-08-21

### Fixed

- Sparse Python validation now imports and measures the retained candidate
  package instead of the canonical editable install, with deterministic
  path/state/bytes authority retained in every validation receipt.
- Declared `npm --prefix` validation receives immutable package and test
  support from the detached candidate base; no dependency tree is copied or
  downloaded, and an unbound dependency tree fails closed before execution.
- Git lifecycle subprocesses no longer inherit ambient `GIT_*` redirection or
  interactive prompt configuration. Preflight resolves HEAD directly from
  bounded Git metadata and caches only successful, exact HEAD-bound receipts.

### Performance

- The full VS Code test suite now runs from a sparse candidate workspace
  without copying `node_modules`; the live 40-file discovery canary completed
  in about five seconds on the release host.

## [0.10.20] - 2026-08-21

### Fixed

- Review rejection to `blocked`, `archived`, or `superseded` no longer
  validates or inherits a malformed retained rework delta. Pending rework
  remains fail-closed and still requires the authenticated delta contract.
- Terminal candidates with invalid rework metadata can now leave the review
  queue without direct database edits, breaking a self-hosting retry loop.

## [0.10.19] - 2026-08-21

### Performance

- Dashboard Roadmap joins now reuse the exact bounded task-card snapshot
  already read for the same refresh. The canonical 25-outcome join fell from
  13 routed point lookups and 0.383 seconds to a 0.0079-second median after
  the shared card read (about 48 times faster).
- Standalone Roadmap calls and capped/partial caller snapshots retain exact
  canonical point lookups for task IDs whose absence cannot be proven.
- The canonical full dashboard snapshot measured a 1.13-second median after
  this change, down from roughly 1.20 seconds in 0.10.18.

## [0.10.18] - 2026-08-21

### Performance

- Dashboard NeedFix derivation now reuses the exact task-card projection
  already loaded for the same bounded refresh instead of querying and decoding
  up to 5,000 cards a second time.
- A complete, under-limit task-card snapshot now proves that a linked task is
  absent without dozens of repository-routed point lookups. Capped or
  caller-declared partial snapshots retain the canonical lookup fallback.
- The canonical full dashboard snapshot measured about 1.20 seconds median,
  down from 1.54 seconds in 0.10.17, with standalone NeedFix reads remaining
  fresh and derived by default.

## [0.10.17] - 2026-08-21

### Performance

- Replaced the dashboard review-latency correlated SQLite lookup with an
  equivalent set-based projection. On the canonical repository the measured
  query fell from roughly 0.95–1.00 seconds to a 0.10-second median.
- Dashboard Workforce and Preflight readers now share one repository probe
  inside the same bounded refresh. Direct calls and later refreshes remain
  fresh; the full dashboard snapshot measured a 1.54-second median after both
  changes.

## [0.10.16] - 2026-08-21

### Fixed

- Dashboard single-flight state is now activated only for one bounded read
  set and is cleared on every exit. Reusing a `DashboardProvider` across
  refreshes therefore cannot reuse task or cost data from the prior refresh.
- Concurrent snapshots that intentionally share a provider are serialized at
  that provider boundary; unrelated provider instances remain fully parallel.

## [0.10.15] - 2026-08-21

### Performance

- A full dashboard refresh now single-flights its shared canonical task-card
  and cost-ledger inputs across the parallel plan, workforce, collision and
  cost consumers. The cache exists only for one snapshot, so later refreshes
  always read fresh repository state.
- On the canonical repository, median full-snapshot wall time fell from about
  2.48 seconds to 2.02 seconds (roughly 19%), while Python call volume fell by
  about 21%. Dashboard semantics and output remain unchanged.

## [0.10.14] - 2026-08-21

### Fixed

- Request-ledger-owned orphan worktrees now become reversible retention
  candidates when their exact canonical owner task is independently finished,
  archived or superseded. Live, nonterminal, unknown and foreign ownership
  remains fail-closed, and quarantine plus restore revalidate both identities.
- Terminal task identities are read once per quarantine/restore batch instead
  of once per worktree, avoiding repeated canonical task-table scans during
  large cleanup operations.

### Storage

- Archiving 19 historical blocked attempts and applying the exact owner rule
  released 25 stale worktrees (4.03 GB) to reversible quarantine on the
  canonical repository. Active worktree storage fell from 4.26 GB to 232 MB;
  two current unresolved predecessor worktrees remain protected.

## [0.10.13] - 2026-08-21

### Performance

- Terminal-log retention now keeps a bounded append-aware latest-event
  projection instead of reparsing the complete process-event ledger on every
  Storage refresh. On the canonical 33,000-row ledger, warm projection time
  fell from 1.12 seconds to about 0.01 seconds and the complete retention
  preview fell to roughly 0.27-0.35 seconds.
- Ordinary appends parse only newly completed JSONL rows. Rotation, spill,
  replacement, truncation, deletion and immutable-segment changes invalidate
  the projection and replay canonical event ordering, so caching never changes
  retention authority.

## [0.10.12] - 2026-08-21

### Performance

- Python imported-call resolution now tokenizes each source line once instead
  of compiling an alias-specific regular expression for every unresolved edge.
  On the canonical 823-file graph this reduced the resolver from 54.17 seconds
  to 5.85 seconds and the full 8-worker rebuild from 66.82 to 18.60 seconds.
- Source Graph build receipts now expose bounded phase timings for hashing,
  extraction, merge, cross-file resolution, Git metrics, quality and total
  wall time so future regressions identify the exact bottleneck.

### Fixed

- Clean-host launcher tests restore their injected provider-auth fixture,
  keeping Linux, Windows, macOS and Python 3.12-3.14 CI qualification aligned.

## [0.10.11] - 2026-08-21

### Performance

- Worker isolation now registers a no-checkout worktree and materializes only
  task-card-declared files with Git sparse-checkout. The canonical fresh
  finalization preflight fell to about 24 ms while mechanical modified, added,
  deleted and renamed-path verification remains intact.
- Repeated request-event lookups reuse an identity-safe ledger projection;
  measured warm lookup time fell from about 1.24 seconds to below 0.2 ms while
  append, rotation, truncation and replacement invalidate the cache.

### Fixed

- Windows cleanup recovers from an exact `git worktree remove` timeout by
  deleting only the request-owned tree and pruning its missing registration;
  failures now report the actual cleanup command instead of an earlier diff.
- Semantic edits containing literal angle placeholders such as `<code>` or
  `<implementation>` are rejected before they can replace valid source.

## [0.10.10] - 2026-08-20

### Fixed

- VS Code LM workers now treat byte-identical compatibility edits as verified
  no-ops: they neither rewrite the target nor report a false changed path. This
  prevents already-satisfied task cards from entering required-output and
  retry loops while retaining the mechanically verified final hash.

## [0.10.9] - 2026-08-20

### Fixed

- Windows worker finalization no longer depends on a hard-coded two-second
  `git diff` subprocess. Timed-out Git trees are reaped and a complete,
  creation-time worktree manifest mechanically verifies zero-diff and all
  modified, added, deleted or renamed paths; incomplete fallback evidence
  fails closed. Windows Preflight now exercises the same isolated finalization
  path and reports phase-level provisioning and cleanup timings.

## [0.10.8] - 2026-08-20

### Fixed

- The text-only GLM bridge no longer contradicts Source Graph guidance by
  forcing every follow-up lookup to `mode=focus`. Exact file and symbol reads
  now preserve their intentional `file`/`body` mode, target and workflow stage,
  so indexed worker scopes do not fail as false zero-hit results.

## [0.10.7] - 2026-08-20

### Fixed

- Storage retention now releases stale task/worktree pins when the owning
  NeedFix is explicitly terminal, while nonterminal task lineage remains
  protected. Exact request-ledger ownership and terminal NeedFix state are both
  rechecked immediately before quarantine.
- Broken Git metadata no longer strands terminal NeedFix worktrees forever.
  Their files can be moved to reversible quarantine and restored truthfully to
  the same ledger-owned orphan state; unknown or active orphans remain
  fail-closed.

## [0.10.6] - 2026-08-20

### Fixed

- Repository storage inventory now uses the exact durable worker request
  envelope as a secondary ownership authority when Git has pruned a linked
  worktree registration. Repo/request/path/HOME bindings must all match; broken
  checkouts remain fail-closed and are never presented as safely removable.
- Stale registered worktree HEADs are recovered from repository-owned Git admin
  metadata without adding per-worktree Git subprocesses. On the canonical
  repository this reduced falsely unattributed storage from 5.27 GB to 2.3 KB
  while keeping the warm preview under one second.

## [0.10.5] - 2026-08-20

### Fixed

- Rework workers now query the canonical Source Graph once and compose only
  packet-bound changed/deleted worktree paths in memory. Runtime queries no
  longer build a private index or copy/write the canonical SQLite database;
  changed files carry exact worktree hashes and deleted files are tombstoned.
- Review-event indexes are installed only after the compatible `task_events`
  schema is present, so additive startup against older task stores no longer
  fails on a missing `event_id` column.

## [0.10.4] - 2026-08-20

### Performance

- NeedFix active-state derivation now loads one bounded canonical task-card
  snapshot and reuses it for listing and exact counting. Live dashboard latency
  fell from 2.88 seconds to about 0.40 seconds while preserving exact fallback
  beyond the bounded snapshot.
- Canonical task events now index both event chronology and task/event
  chronology. The review-decision aggregate no longer performs thousands of
  correlated primary-key scans; measured latency fell from roughly 2.4 seconds
  to under 0.9 seconds.

## [0.10.3] - 2026-08-20

### Performance

- Full dashboard snapshots no longer transport the per-request protected
  terminal-log list that the Storage UI never renders. The canonical live
  payload fell from 2.16 MB to 811 KB, while the Storage projection fell from
  1.39 MB to 38.6 KB and retains every aggregate count and byte total.

## [0.10.2] - 2026-08-20

### Performance

- Storage telemetry now persists one repository-bound, atomically published
  last-known-good snapshot across MCP runtime and VS Code window reloads. The
  canonical 19.9 GB dashboard restored its Storage card in 5.6 ms instead of
  returning to an 88-second `Calculating` scan.
- Managed-storage refreshes use a five-minute cache lifetime and remain
  single-flight in the background, avoiding near-continuous disk traversal on
  repositories whose cold inventory takes longer than the former one-minute TTL.

### Fixed

- Stale Storage values remain visible while refresh runs instead of being
  replaced by zeroes. Foreign, malformed, oversized, expired or symlinked cache
  data is ignored, and persisted telemetry cannot override live disk capacity or
  dashboard read-only authority.

## [0.10.1] - 2026-08-20

### Fixed

- Progressive dashboard summaries now update only queue and storage counters;
  their deliberate empty placeholders can no longer erase the last full Source
  Graph, context, NeedFix, Roadmap, preflight or task snapshot between polls.
- Windows review acceptance and worker finalization no longer launch the
  redundant `git rev-parse HEAD` probe. Remaining Git probes are phase-bounded,
  reap their exact process tree and return structured timeout taxonomy.

### Observability

- Finalization events now report separate workspace-scope, validation and
  evidence/transition wall-clock durations.

## [0.10.0] - 2026-08-20

### Performance

- The native dashboard now renders its bounded operational summary before the
  full observability payload, removing the 20–30 second blank-panel wait.
- Independent read-only dashboard sources run on a core-derived thread pool
  that leaves two cores free for MCP. On the canonical repository, full
  snapshot construction fell from 5.1–6.1 seconds to about 3.1 seconds.
- Storage inventory starts during the cheap health handshake and the full
  response publishes a scan that finishes during hydration. The measured
  summary-to-complete path now reports the exact 19.9 GB managed total in about
  5.9 seconds instead of leaving the card on `Calculating` until a later poll.

### Fixed

- Concurrent snapshot failures retain deterministic source/error ordering;
  parallelism changes latency only, never the returned dashboard contract.

## [0.9.99] - 2026-08-20

### Performance

- Manager dashboard summaries now read only the queue counts, inbox and
  collision fields they actually return. On the live repository, summary
  construction fell from 6.52 seconds to 0.35 seconds; the full Webview
  snapshot remains available unchanged.

### Fixed

- Windows workspace creation no longer spawns the redundant post-create
  `git symbolic-ref` and `git rev-parse` probes that could hang for 120 seconds
  inside the launcher. Detached state, repository ownership and the pinned base
  OID are verified directly from bounded Git worktree metadata instead.
- The storage accounting regression fixture now explicitly configures its
  nested worker root, removing host-environment dependence across Python
  3.12–3.14 CI.

## [0.9.98] - 2026-08-20

### Performance

- **Dashboard storage inventory no longer resolves repository identity once per
  quarantine batch.** On the live 740-batch, 19.9 GB store, the measured cold
  scan fell from 25.87 seconds to 3.44 seconds.
- Storage sizing uses a single `scandir` traversal, prunes the separately
  inventoried worktree root, and stops after the public 100-batch bound instead
  of measuring hundreds of rows that are discarded.
- AI Memory and Context Graph query paths remain read-only after repository
  initialization; schema/FTS reconciliation moved off the query hot path.

### Fixed

- Managed storage no longer counts nested worktrees once as repository data and
  again as worker data. Global, repository-owned and unattributed worktree bytes
  retain their separate truthful projections.

## [0.9.97] - 2026-08-20

### Performance

- **Incremental Source Graph refreshes no longer re-scan the repository-wide
  Python call graph for a local edit.** Cross-file resolution is bounded to
  changed callers and function identities that were actually added, removed or
  renamed. Rename/delete invalidation remains fail-closed.
- Added the missing `entities.qualname` SQLite index used by edge resolution
  and index-quality joins. On the 818-file AIWorkHub repository, the measured
  incremental refresh fell from about 55 seconds to about 5 seconds and the
  quality scorecard from 29.1 seconds to 0.22 seconds.
- **Task-plan snapshots no longer perform one SQLite read per task.** The plan
  now consumes one bounded decoded-card snapshot; the measured 601-task plan
  fell from over 90 seconds to about 0.31 seconds without changing DAG or
  collision semantics.

### Fixed

- Source Graph readers remain serviceable during the shortened incremental
  refresh window; the canonical base plus request-local partition model stays
  intact and no full database copy is introduced.

## [0.9.96] - 2026-08-20

### Fixed

- **A live provider worker was cancelled after ten minutes without a new
  model-output event.** Two independent GLM 5.3 workers reproduced the same
  failure on 0.9.95: their exact supervisor and child processes were alive,
  heartbeats were fresh and stderr was empty, yet the reconciler terminated
  both as `worker_stalled:no_meaningful_activity` at roughly 618–634 seconds.
  Meaningful-output age is now observability only. A live exact process and
  heartbeat remain processing indefinitely; terminal state still requires an
  authenticated provider result/error, verified process exit or explicit owner
  cancellation. The obsolete quiet-time death configuration was removed.

## [0.9.95] - 2026-08-20

### Fixed

- **VS Code LM workers could enter an unrecoverable force-stage loop.** The
  advertised semantic-edit schema mixed mutually exclusive create and
  replace-range fields, while its outer `additionalProperties: false` rejected
  every valid branch. GLM consequently guessed operation names, received
  `operation_invalid`, retried malformed payloads and finally stopped with
  `vscode_lm_semantic_edit_stage_required`. The tool now exposes two exact,
  executable schema branches, supplies canonical offline examples in native and
  text protocols, and rejects hybrid or extra-field payloads deterministically.
- **A late non-stage tool request was treated differently by tool name.** During
  bounded staging, any non-stage request is now corrected once without invoking
  MCP; a repeated violation terminates once with a structured reason. Valid
  stage and finalize calls remain offline and retain exact call/result identity.

## [0.9.94] - 2026-08-19

The OS boundary. Every defect below was measured by running the code, and the
first one had been dressed up as a rule of thumb for weeks.

### Fixed

- **A blocking lock on Linux waited forever, and its recovery was written for an
  OS we do not run on.** `platform_io` promised "the same repository-local
  locking contract on Linux, macOS, and Windows"; Windows had a deadline and
  raised `AdvisoryLockTimeout`, POSIX called `flock(LOCK_EX)` and never returned.
  The one place that catches that exception has a careful deferral - and on Linux
  it was unreachable, so contention parked a monitor thread instead. This is the
  defect behind the operating note "reviewer launches must be serialised; only an
  MCP server restart clears it". It was never a law of the system. POSIX now
  shares the bound and the exception. (NF-2026-00350)
- **Liveness had three answers to one question.** Measured against PID 1, which
  is unambiguously alive: `os.kill(1, 0)` raises EPERM, and the probes answered
  False, False and None. Two adjacent lines of one function disagreed - the
  Windows branch read access-denied as alive, the POSIX branch as dead - so on
  Linux a worker running under another uid was declared dead and its live card
  terminalized. One function now, and EPERM means alive.
- **The group terminator signalled a process group and then waited only on its
  leader**, so a wrapper exiting while its worker ran satisfied the check and the
  SIGKILL escalation never fired.
- **The semantic-edit preimage check was optional and silent.** A range edit
  without a fragment hash was applied with nothing recorded; the module docstring
  meanwhile promised a verified complete-file preimage that this layer never had
  a parameter for. Unverified edits are still possible and are now reported.
  Separately, a bare-CR file lost its line terminator - lines 1 and 2 were
  silently glued - because the preserving branches knew `\r\n` and `\n` and not
  `\r`. (NF-2026-00351)
- **Copilot's MCP server never started.** The extension wrote a repo-local
  absolute interpreter path into root `.mcp.json`, and VS Code's remote spawn -
  which resolves on the server side - failed with ENOENT on it, three times over
  three days, while the extension host spawned the same binary successfully in
  the same minute. Both configs now carry the bare interpreter name, which
  satisfies Claude Code (not a variable) and the remote spawn (resolves via
  PATH); an interpreter configured outside the repository is left alone. This
  reinstates, on evidence, the change backed out of 0.9.92.
  (NF-2026-00352, NF-2026-00347)

### Notes

Promoting the platform fix turned canonical red with 16 failures:
`worker_supervisor` runs as a direct script, and a new relative import broke its
fallback chain one level deeper - in the one process whose job is to stop workers
being orphaned. Corrected here. The defect also blocked its own review three
times, which is the clearest evidence for it in this changelog.

Canonical: 4763 passed, 38 skipped, ruff clean, npm test 39 files.

## [0.9.93] - 2026-08-19

Source Graph, and the models the editor already had.

### Fixed

- **A symbol target was filtered as a path prefix, so `body` and `function`
  returned nothing for symbols the index holds.** The engine resolved the symbol
  and the wrapper then dropped it, because a file path never starts with a
  qualname. `freshness: "fresh"` with zero matches is impossible from the engine
  - the scalar survived while the rows were removed, which is the fingerprint of
  a filter applied after retrieval. `focus` emits qualnames in its own
  `recommended_next_steps`, so a caller following the tool's advice got nothing
  and fell back to reading files: the ~60x token saving abandoned on every such
  call. `slice` had been exempted years earlier with a comment stating exactly
  this reasoning; `body` and `function` never got it. The distinction is now
  named once and drives both the engine call and the filter. A symbol outside the
  task allowlist is refused BY NAME rather than returned as an empty result that
  reads as "no such symbol". (NF-2026-00348)
- **A security lens closed the last piece**: two boundary paths answered the same
  question oppositely - one fail-open, one fail-closed - on a match with no
  attributable file. Not reachable through the current schema, and closed anyway.

### Added

- **AIWorkHub asks VS Code which models exist instead of keeping a list nobody
  maintains.** The same list had been written out four times and every copy knew
  one model, while the owner's endpoint exposed six. `vscode.lm.selectChatModels`
  is the authority now: the catalog fans out one row per discovered model, and a
  model the endpoint starts offering is usable with no code change and no hand
  edit. Gating is the provider family plus the shared name regex - nothing in the
  running path enumerates a model name. The filters that exclude entries which
  measurably fail are unchanged, and a name the editor never reported is refused
  by name.

### Notes

Two audits of Source Graph were accepted alongside this. One, run on GLM-5.2
through the very path being fixed, independently found the `function`-mode defect
above as its own F2 and added a `focus` ranking finding. The other found that a
payload preview labelled `structure_aware_priority_preserving` truncates lists
positionally - so a high-priority row at index 5 is dropped - and that the cache
replay branch returns a `content_sha256` that does not hash its own `content`.
Both are recorded, neither is fixed here.

Canonical: 4730 passed, 38 skipped, ruff clean, npm test 38 files.

## [0.9.92] - 2026-08-19

The release that unblocks the loop. One failed Source Graph call was enough to
make a green card permanently unacceptable, and around ten cards died on it
before it was measured rather than read.

### Fixed

- **A single failed Source Graph call refused the card.** `receipt_conformance`
  compares the lengths of two sequences that describe the same calls; the stage
  was recorded for every call and the mode only when it was recognised, and a
  failed call carries no mode. Measured across three cards refused the same day,
  the difference equalled the failed-call count exactly - 5/5, 1/1, 6/6. The
  refusal then ends the card in `validation_failed`, which is non-operational, so
  correct work could only be relaunched and never accepted. Fixed by the manager
  rather than a card, because the gate blocked its own repair. The check is
  aligned, not weakened: a genuine length difference still blocks. What remains
  open is the blocker's NAME, which describes a workflow_stage discipline failure
  and tests arithmetic - it cost two rounds of correct work being returned with
  the wrong instruction. (NF-2026-00346)
- **A pending card was counted as a held claim.** The launch preflight and the
  live guard gave opposite answers about one card list at one moment - 19 claims
  against collision_free - because membership in a status bucket stood in for a
  held claim. Twelve orphan reviewer cards were enough to report a collision for
  every launch on the primary runner. Counting follows `claimed_by` now, and the
  two surfaces stop sharing one word for two questions: `condition_kind` and
  `runner_busy` distinguish a busy runner from a write-scope collision.
  (NF-2026-00335, AWH-OBS-012b)
- **A reviewer launch returned success before anything that could refuse it had
  run**, so every later failure was invisible at the call site. Eight cards were
  found sitting in processing with no process alive, the oldest for 15.1 hours,
  each reserved and marked processing and never spawned. The refusal now happens
  before the reservation and the thread. The disposal guard keys on the
  reservation rather than on `started_at`, which is written minutes later at
  claim time - a DeepSeek reviewer found that trade. (NF-2026-00265,
  NF-2026-00331, NF-2026-00330)
- **The verdict wrote a lens status its own validity set rejected** - a
  regression 0.9.91 introduced while correctly stopping a blind reviewer from
  reading `passed`. The contract now publishes the status vocabulary,
  `evidence_verdict([], [])` no longer reports `passed` on a vacuum, and the
  independence rung is derived from the report it describes instead of the first
  match. The combined-tree exemption matches by reason kind - narrowed after
  promotion so a check that SHOULD have run still blocks, and only a minimum no
  tier can reach is exempt. (NF-2026-00339..344 residue)

### Notes

Twenty-one ghost cards were cleared by hand: eight processing with no process,
thirteen orphan pending reviewers. Nothing in the system would have cleared them
- `stale_processing` reported 0, seven of the eight had no process record at all,
and `cancel` returned success while the card never moved. Recorded as
NF-2026-00345.

Canonical: 4712 passed, 38 skipped, ruff clean.

## [0.9.91] - 2026-08-19

Six defects from an independent empirical audit of 0.9.90, plus a blocking
contract drift. Every number below was produced by RUNNING the code; the auditor
changed nothing. The six share one root: terminal outcome, risk tier and policy
strictness are each one concept and none of them was one TYPE anywhere.

### Fixed

- **A terminal outcome was one concept written out six times, and three copies
  disagreed.** The launcher emitted five substatuses the state machine called
  illegal - `token_budget_exceeded` among them - and nothing failed only because
  that path happened to call one function rather than another. A barrier holding
  by routing accident is not a barrier. One module now owns the vocabulary, the
  five other sites import it, and a contract test fails when any copy drifts.
  (NF-2026-00339)
- **Any unknown substatus became `review_ready`.** Nineteen lines above that
  fallback the same file documented B921 - "a blocked outcome must never be
  delivered as review_ready" - and its own comment admitted two outcomes had
  already fallen into it. Those two were patched in and the fail-open default was
  left, so every future failure repeated the bug. Unknown now resolves to
  blocked. (NF-2026-00340)
- **`blocked` was a legal outcome and an illegal source at once**, so reaching it
  locked a card forever, and a manual recovery tool existed solely to undo that.
  It is now terminal with a defined exit, proven by a test. (NF-2026-00341)
- **A blind reviewer passed.** At medium tier the blindness check ran only on the
  tier's required lens, and a report's mere existence lifted a lens from skipped
  to passed. Measured: security and code_quality both read `passed` while those
  reviewers reported `usage_observed: false` and zero tokens - they said plainly
  they had read nothing - and the verdict read `verified` with no blockers.
  Blindness is now a property of the report, not of the tier, and never passes at
  any tier. (NF-2026-00344)
- **The gate claimed `repository_policy` scope when every declared check was
  skipped.** Scope now derives from what EXECUTED. (NF-2026-00342)
- **The self-weakening detector counted checks without reading them**, so a
  candidate could keep both checks, replace their commands with `true`, scope
  them to `docs/**`, raise their minimum risk to critical, and be reported as
  unchanged. It now compares command identity, path scope and minimum risk - and
  an unreadable canonical config yields `unable_to_compare` rather than the "no
  weakening" it used to report. (NF-2026-00343)

### Notes

`contract_consistency_check` was failing and blocking: all three managed
instruction carriers had drifted from their canonical projection. They are
reprojected and it passes. The clauses that were missing are worth naming,
because they are exactly the mistakes made while they were absent - "launch in
parallel only cards whose allowed_writes do not overlap", and "a card's
allowed_writes must include the tests that assert the contract it changes and the
production call sites it must wire, or correct work is unwinnable."

Canonical: 4687 passed, 38 skipped, ruff clean, with both audited behaviours
re-measured on the canonical tree after promotion.

## [0.9.90] - 2026-08-19

One card, three NeedFix records, on the layer that tells an operator why a run
died.

### Fixed

- **A dead balance, an exhausted quota and broken code are no longer the same
  error.** A provider refusal arrived as `worker_failed:exit_code=1`; twenty-six
  of fifty-two blocked cards carry exactly that string, so a dead balance, an
  exhausted weekly limit, a revoked key and a genuine crash were indistinguishable
  from each other. One such refusal discarded 2.6M tokens of completed work while
  the record pointed at the worker. The classifier that separates them existed
  and had ZERO production callers - it was on the orphan list measured under
  NF-2026-00304. It is now wired at the launch-failure detector, which classifies
  where the provider's own response is still in hand rather than downstream where
  only an exit code survives, and it is handed the provider's machine fields
  only - never the result prose, which an agent could author. (NF-2026-00275,
  first of three defects; the attempt-accounting and circuit-breaker halves stay
  open on that record.)
- **A bare 401 no longer claims the credential is bad.** Nine launches succeeded
  on one credential, three failed with `provider_authentication_failed`, and it
  cleared by itself ten minutes later - a quota condition wearing an
  authentication label, and the two demand opposite responses. A refusal whose
  body names no cause now records
  `provider_refused:http_status=NNN:cause_not_distinguished_by_response`.
  (NF-2026-00326)
- **Every route now states whether its quota is observable, and why not.**
  `provider_observability_report` was also an orphan; it is wired into the
  preflight. `ready_unverified` remains the honest answer for every route because
  no adapter exposes quota - but it is now a stated conclusion per route rather
  than one generic string shared by all of them. (NF-2026-00270, partial)

### Notes

Four rejection rounds, each on a live defect in this card's own central
invariant, each found by a lens before the manager and each measured by running
the code: both entry points shipped unwired and reported as "already exists and
is correct"; a 402 PAYMENT REQUIRED collapsed to `worker_failed`; bare
`"exhausted"` pulled a dead balance into the quota family and marked it
RECOVERABLE, telling an operator to wait for a condition that only clears when
someone adds credit; and a status-less `unauthorized` was recorded as
`no_provider_refusal_signal` - a refusal the detector itself had matched.

The classifier is now verified across twelve bodies and the detector across five
events. Balance beats status. 402 is never recoverable at the STATUS level,
independent of token membership, so no future vocabulary edit can reintroduce
the class.

This release also carries 0.9.87, 0.9.88 and 0.9.89, which were committed and
CI-verified but never tagged, so no GitHub Release was ever produced for them.
Their entries below stand; this is the tag that publishes them.

Recorded rather than fixed: NF-2026-00332 (`_provider_status_code` reads any
400-599 number anywhere in provider text as an HTTP status, so a crash traceback
containing "line 429" would classify as a recoverable rate limit - latent behind
today's only caller, which builds a controlled body).

## [0.9.89] - 2026-08-19

One card, two NeedFix records, both on the gate that is supposed to be the last
thing standing between a candidate and canonical.

### Fixed

- **The gate now sees code nothing calls.** All three quality lenses had passed a
  candidate whose new code had no caller anywhere - 1,197 insertions across three
  production modules, every line correct, every behaviour tested, and nothing
  changed at runtime. Green tests do not prove reachability: a test can call a
  function directly and pass while no production path ever does. The gate walks
  call and reference edges transitively from the entry points the repository
  already recognises, names each unreachable addition individually, and reports
  rather than refuses - some additions are legitimately unreferenced, and a gate
  that cries wolf gets disabled. (NF-2026-00304)
- **Promotion refuses a write that would undo a release.** A candidate cut before
  a release still carries the old version constant, so promoting it silently
  reverts `_version.py` and every projection derived from it. It happened twice:
  one candidate carried `EXPECTED_MCP_PACKAGE_VERSION` 0.9.82 against a canonical
  at 0.9.84, its successor 0.9.85 against 0.9.87 - which would have broken every
  extension preflight. Both were caught by hand. Promotion now refuses a backwards
  version write with a named reason carrying the file and both values, at the sole
  promotion write seam and before a single byte is written. Equal is silent, ahead
  is allowed, unparseable fails closed. (NF-2026-00315)

### Notes

The card was rejected once for reproducing the exact defect it exists to detect:
round one built both checks correctly and shipped them unreachable - each had one
reference in the whole tree, its own definition - while its tests called them
directly and passed. The report said so plainly: "not threaded into
accept_review's write loop to keep the change minimal." Naming an omission does
not wire it, and a guard that is not on the path guards nothing while making the
next reader believe the boundary is covered.

Recorded rather than fixed: NF-2026-00328 (the reachability seam labels every
symbol in a changed file "modified" and can surface an untouched neighbour as an
unreachable addition - two lenses found it independently), NF-2026-00329 (the
refusal exception escapes `accept_review` past its only handler, so the guard's
success case arrives as an unhandled error rather than a structured refusal).

## [0.9.88] - 2026-08-19

Eight NeedFix records across three accepted cards, plus one regression the
manager caused and caught before pushing. Two of the eight had been reproducing
since 0.9.43 and 0.9.44.

### Fixed

- **The extension wrote a VS Code variable into the config Claude Code reads.**
  `${workspaceFolder}` reached `.mcp.json`, which Claude Code parses directly and
  cannot expand, so the MCP server failed to start naming a token where a
  directory belongs - and every version bump regenerated the file and re-broke
  it. `.mcp.json` now carries the resolved absolute command, and a check REFUSES
  to persist a config a non-VS-Code consumer cannot resolve: a failed check skips
  the write with a named reason rather than writing it and logging. The two files
  differ deliberately and both are asserted - `.vscode/mcp.json` keeps the bare
  interpreter name, because VS Code does not substitute variables in `command`
  and its remote spawn is ENOENT-prone on repo-local absolute paths.
  (NF-2026-00243)
- **A healthy worker looked dead.** Live reasoning deltas were dropped by the
  renderer, so a worker producing output normally showed an empty panel; an
  operator watching that kills the run. Measured on the original report:
  heartbeat age 4s, 11.3 MB of stdout, and nothing on screen for 33 minutes.
  (NF-2026-00182)
- **Valid provider JSON was labelled unsupported.** An unrecognised shape now
  degrades to a readable raw rendering with a named reason, never a blank panel
  and never an "unsupported" label on valid JSON. Live-output generation
  ownership is established with a test rather than assumed, and the poll chain
  clears any pending timer before arming so reselecting a task cannot leave two
  loops alive. (NF-2026-00183, NF-2026-00224)
- **The dashboard now follows the editor's font size instead of overriding it.**
  89 of 112 font-size rules were 11px or smaller and 38 were 9px or smaller,
  while exactly ONE rule read `var(--vscode-font-size)`. That is the defect: the
  user had already told VS Code what size they read at and the panel discarded
  the answer everywhere. A type scale rooted at that variable now drives all 112
  declarations with zero pixel literals remaining; the smallest rendered size is
  11.05px at the 13px default and 13.6px at 16px. Larger text was absorbed with
  layout, never by shrinking back. (NF-2026-00311)
- **Twenty of fifty-two blocked cards carried no terminal reason at all** - 38
  percent of failures where recovery starts blind. A single bounding boundary now
  carries the reason and all four blocked/terminal writers route through it;
  `mark_launch_failed` was the path persisting the empty reason. A path that
  cannot determine the cause records `cause_undetermined:<path>`, naming the path
  rather than hiding behind a placeholder. The backfill of the existing twenty
  rows is not done and that record stays open. (NF-2026-00307)
- **A failing quality-gate check cut off its own explanation.** The summary
  truncated at exactly 2000 characters, head-first, while `error` was empty -
  and a test runner prints failures AFTER the passing output, so the capture kept
  twenty passing lines and discarded the one that said what broke. Failing checks
  now keep the TAIL with a marked elision; every cut says it was cut. The cap was
  not raised: what was wrong is which end it kept. (NF-2026-00321)
- **An absent artifact reported `artifact_invalid`**, sending an operator to hunt
  for corruption in a file that does not exist, while a sibling surface already
  said "missing" for the same condition. Absent and malformed are now different
  reason codes. (NF-2026-00322, external AWH-OBS-015)
- **A frozen preparation heartbeat was invisible to stall detection**, because
  the watchdog read liveness from a pid and a launch that never spawned has none
   - so the one case that most needed a watchdog was the one it could not see.
  Six reviewer launches sat twelve minutes with `pid: null` and a frozen
  heartbeat and no stall was ever reported. Detection is now pid-free.
  (NF-2026-00320)

### Notes

One regression here was the manager's, caught before pushing rather than by CI: a
contract test asserted that the "unsupported event shape" label was still present
in `app.js`, which is precisely the defect NF-2026-00183 removed, and the card's
write scope did not include that file so the worker could not have fixed it. The
same shape appeared a second time on the absent-vs-invalid split. Both guards
were updated to assert the new contract rather than deleted.

Recorded rather than fixed: NF-2026-00323 (the reasoning shape predicate is too
narrow and too eager at once), NF-2026-00324 (a comment tells the next reader to
write the variable NF-2026-00243 removes), NF-2026-00325 (the typography guard
reads the `font-size` longhand only), NF-2026-00326 (every reviewer launch
returned `provider_authentication_failed:http_status=401` minutes after identical
workers succeeded, which is almost certainly a quota condition wearing an
authentication label), NF-2026-00327 (`provenance` truncates without a marker
while `summary` beside it is marked).

## [0.9.87] - 2026-08-19

Three NeedFix records and one of the owner's Windows observability items. Two of
these are about a cost that scales with the repository rather than with the
change, and about a retry that had no ending - both self-inflicted limits this
project has been paying every day.

### Fixed

- **Nothing in AIWorkHub copies the Source Graph index any more.** 954306c
  removed the full-index clone from the reviewer prewarm but left the identical
  `source_conn.backup` on the rework overlay path, so every rework attempt still
  copied 107,130,880 bytes - and reworks are the most frequent operation in this
  pipeline. `build_partition` is now the only path at both call sites, and
  `source_conn.backup` no longer exists in the codebase. The cost of preparing a
  worker's view is proportional to the changed set, not to repository size: an
  index at 1 GB costs the same as one at 100 MB for the same six files.
  (NF-2026-00313)
- **A finalizer whose card was archived retried forever, and the retry storm was
  the database lock.** Ten reviewer requests sat permanently in
  `reconcile_pending`, each re-running its finalizer every fifteen seconds and
  each failing identically with `terminal_transition_failed:not_processing`.
  "Retries exhausted" was not terminal - archiving a card does not stop its
  finalizer, so it kept trying to move a row with no processing state and
  rescheduled itself. Ten writers each holding `task_queue.sqlite` for fourteen
  to seventeen seconds left no window for any launch to read its target card,
  which is why every reviewer launch in that window failed with a bare "database
  is locked" that named the wrong database entirely. A finalizer whose card is
  archived or not processing now terminates with a named reason, and a
  target-card read starved by finalization writers says what actually contended.
  (NF-2026-00305)
- **Empty quarantine batches stopped accumulating at the source.** Measured on
  the owner's Windows 11 install: 100 batches reporting status empty and 0 bytes
  while the directory held 3.68 GB, one batch holding 3.66 GB of it. `quarantine`
  now reaps an empty batch at the moment it opens one and `purge_empty_batches`
  collects any that predate that - both guarded by a record-AND-disk check, so a
  manifest that disagrees with what is physically on disk keeps its full undo
  window rather than being removed on a stale "empty" claim. (AWH-OBS-013)

### Notes

Both fixed cards were first rejected by the MCP receipt-conformance gate rather
than by any test: their code was green - 4548 and 4551 passing - and the gate
refused on `source_graph_mode_stage_sequence_mismatch`. That field had declared
`blocking:true` for months while acceptance ignored it, until NF-2026-00247
closed the hole in 0.9.86. The first two cards it stopped were the manager's own,
and both were reworked rather than routed around.

Recorded rather than fixed, so the next reader inherits the measurement:
NF-2026-00318 (finalizer termination depends on an unversioned stderr string),
NF-2026-00319 (`quarantine_review_workspace` builds its destination from an
unvalidated `request_id`), NF-2026-00320 (a frozen preparation heartbeat is
invisible to stall detection, because the watchdog only watches launches that
already spawned).

## [0.9.86] - 2026-08-18

Nineteen NeedFix records across five accepted cards. Two of the defects below
were introduced by candidates during this work and caught by review rather than
by tests; both are recorded here because that is the point.

### Fixed

- **A review workspace failing its integrity check was DELETED.** Detection was
  correct; the response was the worst available one. At 06:29:27 today a hash
  mismatch was detected on this repository and the worktree went with it, taking
  264 insertions of verified work - no warning, no quarantine, and no entry in
  the retention audit. A failing workspace is now quarantined with the card
  marked blocked and the reason named, every removal is recorded with request id
  and reason, and a workspace whose card is still in review cannot be reclaimed
  by a background sweep. (NF-2026-00300)
- **A validator probe could execute candidate code on the host.** It ran
  `subprocess.run` outside the sandbox with the candidate's own worktree on
  `PYTHONPATH` and no `-S`/`-I`/`-E`; CPython imports `sitecustomize` from any
  `sys.path` entry, so a candidate writing `sitecustomize.py` into its worktree
  would have run arbitrary code as the coordinator. Closed twice over.
- **A candidate could forge its own terminal state.** The classifier trusted
  returncodes 126 and 127 as structural, assuming pytest never exits with them -
  but `os._exit(126)` in a test body does exactly that, so a real gate failure
  could relabel itself recoverable. Only the exception type captured at spawn
  decides now. (NF-2026-00271, NF-2026-00298, NF-2026-00299, NF-2026-00267)
- **Process identity lost its last digit above 2^53**, and the two status
  surfaces disagreed by one tick - so a live worker could be judged foreign, or a
  reused pid accepted as the original. Carried as strings now, from one shared
  surface. Reported from Windows as AWH-OBS-011. (NF-2026-00254)
- **Audit-ledger decoding raised out of its own fail-closed boundary.**
  `str.isdigit()` is not the predicate "int() will succeed": `'--5'` and `'²'`
  pass it and `int()` then raises, while non-ASCII digits were silently accepted
  from an authenticated ledger. (NF-2026-00219)
- **The Context Graph never captured the Claude manager.** On a Claude-only
  installation it recorded nothing from the seat holding the manager role, and
  reported `not_configured` rather than failing - so every continuation and
  compaction recovery was empty and nobody noticed. (NF-2026-00256)
- **Rejecting a card left its quality reviewers in the queue forever.** Three per
  rejection; the queue reached 29 entries of which 27 were consumed reviewers.
  (NF-2026-00249)
- Also: malformed callback rows dead-lettered rather than enqueued
  (NF-2026-00220), VS Code LM claims cancelled before their workspace is deleted
  (NF-2026-00221), timed-out worker deltas retained as rework predecessors
  (NF-2026-00138), rework attempts no longer discarding green work over receipts
  the rework path never made (NF-2026-00246), read-only diagnostics naming the
  real reason instead of `unknown_adapter` (NF-2026-00274), CI uploading the
  Windows and macOS junit reports it was already writing (NF-2026-00242), and
  eight orphan symbols decided rather than commented (NF-2026-00173).

### Added

- **CAAS is enforced by the lifecycle**, not by a step someone remembers, and the
  canonical expansion - Continuous Audit as a Service - is corrected wherever
  this repository controls the wording. The **AuditSystem** lands as a running
  vertical slice: one narrow scope, one read-only pass, structured findings
  written to NeedFix with provenance. (NF-2026-00253, NF-2026-00251)

### Known and recorded rather than claimed fixed

- Only CAAS-P1 is independently observed; P2-P6 read self-reported booleans, so
  "by construction" means something weaker for those five (NF-2026-00317).
- The sandbox-unrunnable preflight splits only on a bare `&&`, so a full suite
  chained with `;` or `||` still escapes it (NF-2026-00316).
- The Context Graph `skipped` counter cannot distinguish "not a manager message"
  from "a manager message we failed to record" (NF-2026-00314).
- A candidate cut before a release silently reverts the version constant when
  promoted, and nothing refuses it (NF-2026-00315).

## [0.9.85] - 2026-08-18

### Changed

- **Preparing a quality reviewer no longer copies the whole Source Graph index.**
  A reviewer must query an index that reflects the candidate rather than
  canonical - querying canonical would show it the old structure of exactly the
  files under review. That was achieved by cloning the entire canonical database
  and re-indexing only the changed files: 107,130,880 bytes, 15,104 entities,
  165,012 edges and 759 files copied per reviewer, per lens, for a candidate
  that typically changes six files. A reviewer took twenty to thirty minutes to
  *start*.

  Source Graph now indexes a bounded scope separately and links it to the rest.
  A **partition** indexes an explicitly declared set of files into its own
  database and never reads, copies or opens the base. A **composed view** binds
  one read-only base plus its partitions and answers as a single index, with
  precedence per file: a file in a partition hides the base rows for that file,
  an absent file resolves from the base, and a deleted file resolves to nothing.
  Edges resolve across the boundary in both directions, and the two cases a
  lexical resolver cannot decide are returned as named limitations rather than
  guessed.

  Measured against the live index for six changed files: **107,130,880 bytes and
  ~25 minutes became 5,300,224 bytes and 30.4 seconds** - twenty times fewer
  bytes, forty times less wall clock. More importantly the dependence on
  repository size is severed rather than reduced: an index of 1 GB costs the
  same for the same six files. The regression asserts this on row counts, not
  bytes, and fails the build if a full rebuild happens at all. (NF-2026-00302)

### Known and not closed by this release

- The same full-index clone still exists on the rework overlay path, so every
  rework attempt still pays it (NF-2026-00313).
- A composed view's base fingerprint is size plus mtime_ns with no content hash,
  so an in-place base replacement preserving both is not detected as a shift.
  Documented in the module rather than left to be rediscovered.
- The reservation-expiry and stall-watchdog halves of NF-2026-00302 are separate
  from the copy cost and remain open.

## [0.9.84] - 2026-08-18

### Fixed

- **Every manager action in this repository's audit trail said `codex` performed it,
  including the ones a Claude manager performed.** `reject_review`, recovery and the
  archive/supersede paths all ran with a hardcoded `--runner codex` regardless of who
  held the verified manager seat. Actions are now attributed to the verified manager
  route, and an action that cannot be attributed fails rather than defaulting to a
  name. (NF-2026-00244)
- **`reject_review` left the quality reviewers it consumed in the review queue
  forever.** Only `accept_review` finalized them, so each rejection on a high-risk
  card added three dead entries pointing at a superseded request. Fourteen
  accumulated in this repository's own queue in a single day before anyone noticed.
  (NF-2026-00249)
- **A manager receipt kept reading as verified after the extension host restart that
  invalidated it.** An identity check that cannot prove it is current now fails
  closed and names the staleness. Reported from a Windows 11 install as AWH-OBS-017.
  (NF-2026-00272)
- **Three archive-repair exits had no caller anywhere**, so a half-archived row could
  be prevented but never found or repaired. They are reachable now, and the mutating
  one is gated by the same coordinator capability check as its siblings.
  (NF-2026-00280)
- **A card could be launched with a write scope that did not contain the file holding
  its bug**, which no worker can detect - it sees only a write denial and retries
  inside the wrong files until its budget is gone. Card creation now warns by name
  when a path the card's own evidence references is absent from `allowed_writes`.
  (NF-2026-00258)
- **A directory in `allowed_writes` was accepted at creation and refused at
  promotion**, so a full worker run could produce output that could not be promoted
  at all. The two ends agree now, and the disagreement is caught when the fix is
  free. (NF-2026-00266)
- **A read-only SQLite open was not read-only if the repository path contained
  `#`.** In URI syntax `#` starts a fragment, which swallowed `?mode=ro` entirely:
  the database opened read-write with create-if-missing, and against a different
  file than the caller named. Every read-only open in the package now builds its URI
  through `Path.resolve().as_uri()`. Verified by a repository-wide sweep, not by
  inspection. (NF-2026-00261)

### Added

- Launch-truth policy for the reviewer prewarm, provider-refusal classification,
  workforce admission and per-adapter observability - defined and tested in
  `quality_review`, `runtime_adapters` and `repo_policy`. **This changes nothing at
  runtime yet:** the call sites live in `process_launcher.py`, which was outside the
  card's write scope, so none of NF-2026-00302, 00275, 00270, 00265 or 00262 is
  closed by this release. The wiring is tracked separately, as is the gate defect
  that let an unwired candidate pass three lenses (NF-2026-00304).

## [0.9.83] - 2026-08-18

### Fixed

- **Four tests made the sandbox report failure on green code, and each one cost a
  worker round.** None of it was flakiness; every one was deterministic and only
  looked random because it depended on where the run happened.
  `test_validation_pythonpath_override_is_scoped_to_one_subprocess` required
  `site.getusersitepackages()` to exist, and under a worker sandbox `HOME` points
  at a throwaway workspace home where it does not.
  `test_suite_profile_collects_per_test_metrics` asserted an exact pytest nodeid,
  and pytest resolves its rootdir to the repository when `tmp_path` sits inside
  it, so the nodeid arrives prefixed. `test_review_evidence_audit_recomputes_hash_and_size`
  ran an unguarded setuid `chmod` that Landlock refuses with EPERM.
  `test_isolated_launch_claims_and_passes_key_through_sandbox` launches a real
  worker under Landlock and waits for it, which a nested ruleset cannot satisfy —
  the file already skipped it on GitHub runners for that exact reason and simply
  never covered our own sandbox. Each was reproduced in both directions before
  being changed.

  This mattered out of proportion to its size: `validation_failed` is a
  non-operational terminal substatus, so `accept_review`, `mark_done` and
  `retry_terminal` all refuse it and the only way out is to archive the card and
  reissue it. Green work was repeatedly thrown away for an environment it never
  touched.

- **A card that reached review and then blocked in one episode never woke
  anybody.** The outbox idempotency predicate keyed on task, provider, route and
  episode while omitting the transition, so the second enqueue matched the first
  row and returned False — the card sat blocked and no callback was delivered.
  `seed_missing_review_callbacks` could not rescue it because its lookup was
  transition-blind in the same way. Terminal recovery also left the old
  transition on the row it recovered, so the next claim superseded it again and
  its single recovery budget was already spent, losing the wake permanently on a
  stable route. The delivery daemon called `_process_batch` bare while that
  method caught only two exception types, so anything else killed the
  repository's one delivery thread and left the batch inflight for a full lease —
  while dispatcher health still reported `ok`.

- **The supervisor surface raised `KeyError` on every archived card.** The
  lifecycle map had no `archived` entry although `_lifecycle_state` returns it,
  and the index was unguarded — so a read-only status tool died on the one state
  the control plane most needs to report. Archiving is also the only closure
  available for a card wedged in a non-operational terminal substatus, which
  makes archived cards common rather than exotic. A superseded card additionally
  read as `pending`, so replaced work kept appearing as still waiting;
  `create_task` handed back finished, archived and superseded cards as if they
  were usable; and `auto_pickup` stopped at the first colliding candidate, so one
  blocked card at the head of the queue starved every ready card behind it.

- **The quality gate could be weakened by the candidate it was judging.** A
  candidate that emptied or removed its own quality policy lowered its own
  acceptance bar; the combined-tree fold ran at low tier, so a repository
  declaring `minimum_risk: medium` produced a permanent `risk_below_minimum`
  blocker that no rework could clear; a zero-test JUnit report satisfied a
  mandatory check on one fold path while the other blocked on the same line; the
  ratchet failed open on an unreadable baseline, indistinguishable from never
  configured; `runtime_coverage_for_paths` raised on a shape-invalid document and
  reported `available` on zero matches; stall recovery was unreachable while the
  supervisor process still existed; and the crash-retry packet ran an HTML
  sanitiser over diagnostics that are embedded as JSON, hashing the unsanitised
  bytes so the corruption could not be detected.

- **An interrupted quarantine left moved files with no record of where they came
  from.** The stage-and-move batch wrote its manifest after the moves, so a crash
  between the first move and the write left files relocated and unrecoverable —
  while the sibling `quarantine()` in the same module writes its manifest first,
  which is what shows this was an omission rather than a design. `purge` also
  reported zero bytes freed for exactly the stranded shape that holds gigabytes,
  because the accounting walked the record instead of the disk; two permanently
  dead recovery paths read as coverage that did not exist; the stranded-worktree
  recovery the dashboard tells operators to run had no runnable entry point
  anywhere; and a restored review task whose retained workspace was gone reported
  hash drift, sending the operator looking for tampering instead of for a swept
  directory.

- **The NeedFix explicit-reference link path could never run in production.**
  `reconcile_unlinked_needfix` accepts a card lister so it can link a NeedFix to a
  card that names it, and two branches depend on that argument — but the only
  production caller never passed it, so those branches were dead everywhere
  except in tests that supplied it directly. That is why counts on the NeedFix
  surfaces did not fall when the work behind them finished.

## [0.9.82] - 2026-08-18

### Fixed

- **Source Graph answered with more confidence than it had earned.** It is the
  discovery surface every agent is told to use before reading files, and seven
  defects made it assert things it had not established. A clean page ended
  pagination forever, because the cursor was conditioned on findings returned
  rather than rows processed: measured with 129 eligible symbols and a budget of
  40, the first clean page produced no cursor and the remaining 89 became
  unreachable, while the answer looked like a completed clean scan. `body`,
  `function`, `class` and `context` sliced the live file with the line numbers of
  the indexed generation and no freshness check, so prepending twenty comment
  lines made the tool return those comments as the symbol's code. The analytics
  corpus was silently truncated while its capped flag stayed false; indexing one
  file destroyed resolved cross-file edges that the next incremental build
  skipped repairing; a renamed target left callers pointing at an entity that no
  longer exists, still labelled as extracted evidence; repository-wide `bodygrep`
  stopped a third of the way through and reported no hits; and `coverage.scanned`
  claimed the whole corpus when only one page had been examined.

- **The analyzers reported clean on languages they cannot read.** Five C-family
  detectors ran against Python with no language filter, so they could never fire
  and still returned "available" with no findings across 395 files, which reads
  as analysed and clean. They now return `not_applicable` with a reason. The
  division detector matched every path separator, so a string like
  `"src/aiworkhub/service"` produced four bogus findings; guard detection required
  parentheses so a Python guard was never recognised; duplicate detection ate the
  rest of a symbol after a line comment and merged different functions; the gaps
  query was saturated by builtins and scoped after its limit; and an oversized
  file was silently counted as considered.

- **A worker that committed inside its own worktree lost the work silently.** The
  change set was diffed against the symbolic HEAD, so a commit moved HEAD and
  every change vanished - never scope-checked, never promoted, destroyed with the
  worktree - while `required_outputs` still validated because it checks the file
  on disk. The diff is now taken against the OID pinned at workspace creation and
  fails closed when HEAD moves unexplained. A staged rename no longer hides the
  deleted source path, a single `*` in `allowed_writes` no longer crosses a
  directory separator, and a failed worktree creation no longer leaves a checkout
  behind.

- **The manager MCP surface dropped the collision evidence it exists to provide.**
  The default plan snapshot kept a private field list that had drifted from the
  canonical one, so all eight write-scope collision fields were missing: the
  default receipt showed a card as collision-free while the full one reported two
  collisions including that card. The omission receipt is now derived from the
  projection instead of a hardcoded list that under-declared twelve dropped
  fields, the cost ledger no longer loses its per-role split, and an id-less
  `tools/call` on the stdio fallback no longer launches a worker and blocks the
  read loop.

- **A dead mux kept advertising itself as ready.** One transient write error
  wedged the registry heartbeat permanently, because the failed write left its
  temp file behind under a fixed name and every later write failed silently - so
  a full disk was enough to remove a healthy instance from routing. The sideband
  accept loop died on any error while nothing cleared `ready`, so requests were
  accepted into the backlog and lost. The request deadline was a per-recv timeout
  on a loop, allowing a connection to hold a thread and descriptor for weeks.

- **Storage "Calculating" took eighty seconds.** Measured on a 17 GB repository:
  80.21 s for the full measurement, 54.77 s of it inside one quarantine scan,
  against 3.09 s for worktree retention and 0.18 s for sizing all 53 worktree
  directories. The scan is bounded now, and a measurement cut short reports
  itself partial instead of looking complete.

## [0.9.81] - 2026-08-17

### Fixed

- **The MCP runtime failed to start on a large repository, reporting a timeout
  against a server that was perfectly healthy.** `main()` called
  `task_reconciler.ensure_started()` synchronously before serving, and that scan
  walks every task row and every retained workspace — measured here at 21.6 s of a
  26.7 s `initialize` round-trip. Any client whose request deadline is shorter than
  the scan reported `mcp_initialize_failed:mcp_request_timeout`, so the extension
  declared the runtime unavailable while nothing was wrong with it. Reconciliation
  now starts on a daemon thread, for the same reason retention already did.
  Measured after the change: `initialize` answers in 0.77 s.

## [0.9.80] - 2026-08-17

### Fixed

- **An idempotency key could silently swallow another file's edit.** The semantic
  edit replay cache was indexed on the key alone, so a second apply with the same
  key but a different target — different file, different range, different content —
  counted as a replay: nothing was written, `ok: true` came back, and the receipt
  carried the first file's path. A worker deriving its key from a task id or a step
  counter lost every edit after the first, under a success receipt, on the primitive
  the project relies on to keep edits cheap. A replay must now match in target as
  well as key; a same-key different-target call is refused explicitly. Also in this
  area: apply preserves the original file mode instead of leaving every edited file
  at `0600`, a failed audit-ledger append fails closed as `quality_review_submit` in
  the same module already did, and the bounded preview cursor is derived from the
  rows actually returned so paging cannot skip rows.

- **Four storage accumulations had no working bound at all.** The quarantine
  directory held gigabytes that no batch claimed — the records said nothing was
  quarantined while hundreds of subdirectories sat on disk, so nothing would ever
  purge them. That path is closed, unclaimed directories can be reconciled back into
  the normal expiry path, the empty purge-eligible batches have a named collector,
  and `process_logs` and attempt-artifacts each have a stated bound. An oversized
  terminal log is tail-capped to the exact window the launcher itself reads to
  diagnose a failure; a live or non-terminal run is never touched. Ownership is
  proved by location rather than inferred from a name or an age, and the destructive
  paths re-verify emptiness in both record and disk before removing anything.

- **A half-archived row stayed live and pinned its worktree forever.** Rows carried
  `archived_at` while their status stayed non-terminal, so every archive tool
  answered `already_archived`, the collision guard still counted them as live, and
  the worktrees they owned could never be reclaimed. `archive_task` now writes
  `archived_at` and the terminal status in one preimage-guarded UPDATE with a
  rowcount guard, and re-reads after commit so a write that did not land reports
  `archive_not_persisted` instead of success.

- **The NeedFix operator surfaces showed a stale list nobody could trust.** Read-time
  derivation existed but nothing called it, so the MCP `needfix_list` tool,
  `needfix_count` and the dashboard panel still went through the raw store: a
  rejected record resurfaced and a converted one read as open. All three now obtain
  their rows through the derived entry points, and `derived` / `underived_reason` are
  carried out to each surface so a caller is told when derivation could not run
  instead of being shown stale rows that look authoritative.

- **Live Output stopped polling forever the first time it had nothing to show.**
  Selecting a task right after launch returned `output_unavailable` and the poll
  chain was never re-armed, so the panel showed that error permanently while the
  worker streamed. The chain now re-arms on every outcome and a not-there-yet
  response reads as a transient state. A `taskDetail` reply that lost its race no
  longer resurrects a cleared panel.

### Changed

- **The multicore principle is stated where models and humans both read it.**
  Everything that can use more than one core should, and the rule now renders into
  `AGENTS.md`, `CLAUDE.md` and `.github/copilot-instructions.md` through the shared
  policy, and is recorded in `docs/ARCHITECTURE.md`.

## [0.9.79] - 2026-08-17

### Fixed

- **A card that changed a file Source Graph deliberately does not index could
  never be reviewed, and therefore never accepted.** The reviewer's candidate
  prewarm walked every changed path and raised when one was excluded by the index
  glob, so the reviewer process never launched — the launch returned `ok: true`
  and then nothing happened. Any change touching an eval artifact, a fixture or
  any generated file became permanently unreviewable however complete it was.

  An excluded file is not an error. Prewarm is an optimisation that makes the
  reviewer's queries fast; the sealed review packet already carries the candidate
  content. Prewarm now skips a deliberately excluded path, records the skip and
  its reason, and the reviewer still launches. When every changed path is
  excluded the reviewer still launches and records that it worked from the packet
  alone.

  A genuine indexing failure on a file that should be indexable is still refused
  loudly, and the classifier that separates the two reads the structured error
  rather than prose. The message is `<prefix>:<type_name>:<code>:<detail>` and the
  verdict is anchored to those fields: tolerance requires the type slot to be
  exactly `SourceGraphError` and the code slot to equal an allowlisted exclusion
  code. Everything after the second colon is the worker-influenced detail — it can
  embed the candidate path, including a literal `SourceGraphError:…excluded_glob`
  — and is never consulted, so it cannot forge a tolerated verdict. Both forgery
  directions are pinned by regression tests.

- **Retention could not finish measuring what to reclaim.** The storage retention
  preview returned `measurement_deadline_exceeded` after 90 seconds with
  candidates, footprint and protected all unmeasured. It was not missing: it
  could not finish, so no candidate was ever produced, so nothing was ever
  quarantined, so the footprint grew and the next measurement was slower still.
  Measured on a live 162-worktree, 29 GB tree, the preview went from a 90-second
  deadline overrun to **8.9 seconds complete**, returning **36 candidates holding
  4.9 GB** with 13 protected. The deadline was not raised.

  A partial measurement no longer reads as a clean repository: a preview that
  hits its deadline returns the candidates it did establish, marked partial with
  what was not covered — and that now holds for every caller of a shared
  single-flight measurement, not only the one that started the walk.

## [0.9.78] - 2026-08-16

### Fixed

- **macOS had no process identity at all, so eight launcher regressions failed on
  every CI run.** `_pid_start_ticks` read field 22 of `/proc/<pid>/stat`, which
  Darwin does not have, so it returned `None` on every call while its callers
  treated the result as an integer. That single `None` produced a `TypeError`
  adding `None` to an `int`, a prewarm liveness that could never be true, a
  reconciliation that counted two where one was expected, and ordering
  assertions that collected nothing.

  Process identity exists so a reused pid cannot be mistaken for a live worker;
  on macOS that protection was simply absent. Darwin now supplies a real process
  creation time through `libc` `sysctl` with mib
  `[CTL_KERN, KERN_PROC, KERN_PROC_PID, pid]`, decoding `p_starttime` from the
  leading union member of `struct extern_proc`, with no third-party dependency.
  None of the eight tests is skipped or marked `xfail` on Darwin. Cross-platform
  identity now lives in exactly one place, `runtime_temp.process_start_ticks`, so
  the launcher, the standalone supervisor and the temp-owner collector cannot
  disagree about what identifies a process. An absent identity is treated as
  "identity unknown" and fails closed: an owner manifest carrying no pid still
  answers alive, so a runtime directory whose owner cannot be identified is never
  reclaimed.

- **A manager could only be woken if it happened to be waiting.** The Codex
  callback wakes an already-open coordinator thread through the extension-owned
  App Server sideband mux; the Claude route had a durable inbox with no trigger,
  so a Claude manager either blocked on a synchronous wait or was told by hand.
  The push transport is now selected from the verified manager route, so whoever
  holds the manager seat gets their own callback activated without configuration:
  Codex keeps the existing sideband path unchanged, Claude gets the channel. A
  provider with no push transport reports a named, manager-visible degraded state
  instead of silently leaving the manager to poll.

- The extension README now names the shipped version, which the packaged static
  contract check has required since 0.9.75.

## [0.9.77] - 2026-08-16

### Fixed

- **An installation with a single model provider could never accept any
  high-risk or critical change.** The acceptance fold in
  `src/aiworkhub/quality_evidence.py` required at least one reviewer report
  whose provider differed from the worker's, and both the `high` and `critical`
  risk profiles set that requirement. Since the worker provider is the worker's
  adapter id and a reviewer receipt carries the reviewer's own adapter id, the
  condition could not be satisfied when only one sighted adapter was installed,
  and every change failed acceptance with `independent_reviewer_missing` no
  matter how complete it was. Both escapes were closed at once: a same-provider
  reviewer could read the review packet but failed the vendor comparison, while
  a different-provider adapter that could not read the packet failed
  `reviewer_could_not_inspect` instead.

  Reviewer independence is now the recorded ladder that the launch and
  submission paths already used — `cross_provider` degrading to
  `cross_model_same_provider` and then to `same_model_fresh_context` — and
  acceptance blocks only when no rung applies. The achieved rung is resolved per
  lens, recorded on the lens row and in the verdict, and written into the
  acceptance evidence, so an accepted change states exactly how independent each
  review was. Multi-model routing exists to allocate work by cost and
  difficulty; it was never a requirement that one vendor review another.

  Every property that actually produces independence is unchanged: the
  anti-anchored packet, the sealed candidate, the separate read-only reviewer
  process, the authenticated `packet_sha256`-bound submission, and the discarding
  of any reviewer-supplied verdict in favour of the deterministic fold. A
  reviewer that cannot inspect the packet is still refused for its lens, and a
  review that cannot be attributed to a worker provider is still refused.

## [0.9.76] - 2026-08-16

### Fixed

- **The 0.9.75 VSIX shipped without its bundled Python runtime and the dashboard
  could not start.** The extension was packaged with a raw `vsce package`
  invocation instead of the repository's own packager,
  `vscode-extension/test/package-vsix.js`, which is the single source of truth
  for staging `src/aiworkhub` into the extension's `runtime/` directory and the
  mux launcher into `bin/`. Both directories were absent from the 0.9.75
  package, so a reloaded window reported
  `bundled_mux_runtime_missing:.../ivanechkheidze.aiworkhub-0.9.75/runtime` and
  the dashboard never initialised. The symptom appeared only after a window
  reload, because until then the extension host was still running the previous
  build. 0.9.76 is built through the canonical packager, which additionally
  asserts that `server.py`, `callback_store.py` and
  `dashboard_static/index.html` are present in the staged runtime.
- `dashboard_storage_retention_preview` no longer hangs. The caller's wait is
  bounded by a validated wall-clock deadline, the footprint walk is single-flight
  so a second caller in the finish window does not duplicate it, and an aborted
  measurement can no longer report partial success. Rework-predecessor worktrees
  are pinned as live, so retention can no longer reclaim work that is still in
  flight — the failure that destroyed an in-progress card's predecessor earlier
  in this release series.
- The workforce catalog no longer reports a worker `ready` when it has never
  observed that worker's quota. It reports `ready_unverified` with the reason
  attached, while a worker with observed, healthy quota still reports `ready`.
  Routing on unverified readiness cost three cards their attempt history when a
  provider's quota turned out to be exhausted; `available` stays boolean and the
  distinction now lives in `readiness_status`.

## [0.9.75] - 2026-08-16

### Fixed

- The context audit trail no longer disagrees with itself about whether a task
  is required. `context_writes` treated `task_id` as optional while the
  `context_mutations` schema declared it `NOT NULL` with no default; the two
  only coexisted because the bounds helper always returned a string, so an
  absent task was stored as the empty string. A KB or AI Memory write made by a
  manager outside any task genuinely has no task, and an empty string is a lie
  shaped like data — indistinguishable from a task whose id is blank. `task_id`
  is now nullable, absence is stored as `NULL`, and the code and the schema
  state the same contract.
- A context write that fails an integrity constraint now names the offending
  column. Previously the caller saw only `SQLITE_CONSTRAINT_NOTNULL` and had to
  guess which of twelve `NOT NULL` columns was at fault. The error now carries
  the component, the action and the exact column, turning a future occurrence
  from a guess into a fact.
- The `context_mutations` migration is atomic. The table rebuild runs inside a
  single transaction and verifies that the copied row count matches the source
  before committing; on mismatch it rolls back and leaves the original table
  untouched. A partial failure could previously strand audit rows while
  reporting success — destroying, silently, the very evidence the table exists
  to hold.

## [0.9.74] - 2026-08-16

### Security

- A quality lens can no longer be satisfied by a reviewer that could not inspect
  anything. Observed four times: a reviewer on the `vscode_lm_in_process`
  sandbox is given no file-read tool while its review packet is handed to it as
  a file path, so it cannot open the candidate at all — and its report still
  came back `terminal_state: review_ready`, `audit_verified: true`, shaped
  identically to a real one. On the identical packet
  (`packet_sha256: 440f1eb1…`) a sighted reviewer produced exact file/line
  evidence and found a real defect.
- The gate now marks a lens `reviewer_could_not_inspect` on either POSITIVE
  signal of blindness: a report whose findings are all `disposition:
  process_limit` — the reviewer itself saying it was prevented from inspecting —
  or `usage` telemetry that is PRESENT and records zero activity
  (`usage_observed: false` with `input_tokens: 0` and `output_tokens: 0`).
  Both are exactly what the blind reviewer produced.
- Missing telemetry is deliberately treated as unknown rather than as
  blindness, and keeps satisfying the lens. An earlier attempt required proof
  of inspection and broke five legitimate tests, because most honest reviews
  carry no inspection telemetry at all. Absence of evidence is not evidence of
  absence, and a gate that pretends otherwise rejects real work.
- The residual gap is named rather than papered over: a reviewer that inspects
  nothing and emits no telemetry is still indistinguishable from a good one.
  Closing it belongs at the harness, which must record inspection evidence for
  every attempt including zero-activity ones, so that absence becomes a fact
  about the reviewer instead of a gap in the instrumentation.

### Validation

- `tests/test_quality_gate_blind_reviewer.py` plus the declared quality-gate
  regression set (`test_completion_quality_gate.py`, `test_quality_verdict_v2.py`,
  `test_combined_tree_workspace.py`): 92 passed, 4 skipped.
- Full repository suite: 3909 passed, 36 skipped, 0 failed — 21 new tests over
  the 0.9.73 baseline with no regression.
- Three independent cross-provider reviewer lenses: security 0 findings,
  correctness 0 findings, code_quality 1 non-actionable observation.

## [0.9.73] - 2026-08-16

### Security

- A read-only SQLite open is now actually read-only. Every such connection was
  built as `sqlite3.connect(f"file:{path}?mode=ro", uri=True)`. In SQLite URI
  syntax `#` begins a fragment, so any path containing `#` swallowed the whole
  `?mode=ro` query: the database opened READ-WRITE with create-if-missing, and
  because the fragment was stripped it opened a *different file than the caller
  named*. Reproduced directly — a control path refused the write with "attempt
  to write a readonly database", while a `db#frag.db` target accepted a
  `CREATE TABLE` and left an 8192-byte file named `db` on disk. Those are two
  independent failures: writes were permitted, and the write landed on a path
  nobody requested.
- All eight affected call sites now route through one shared helper,
  `aiworkhub.sqlite_readonly.connect_readonly`, instead of eight independent
  f-strings that each had to stay correct forever: `task_store` (three sites),
  `task_retention`, `source_graph`, `context_importer`, `context_write_intents`
  and `worker_ai_tools_mcp`.
- The helper applies two independent guarantees rather than one: the path is
  percent-encoded through `Path.resolve().as_uri()` so `#` becomes `%23` and
  the query survives URI parsing, and `PRAGMA query_only = ON` is issued after
  connecting so a write is refused even if URI parsing were bypassed.
- Every converted call site keeps its exact previous `timeout` and row-factory
  behaviour — `source_graph` 30.0s, `task_store` 5.0s, `context_importer` and
  `context_write_intents` 5s, and `worker_ai_tools_mcp`'s own constant — so
  concurrency behaviour under load is unchanged.

### Fixed

- The worker MCP portability fixture copied a hand-listed subset of package
  modules into its synthetic layouts and did not include the new shared
  helper, so it failed on a missing file rather than on the import-root
  behaviour it exists to prove.

### Validation

- `tests/test_sqlite_readonly_boundary.py`: a path containing `#` refuses
  writes AND creates no file, asserted on the filesystem after the attempt
  rather than only on the raised exception, because those are separate
  failures.
- Full suite: 3885 passed, 36 skipped. Three pre-existing
  `test_storage_observability_dashboard` failures are unchanged from 0.9.72 and
  are unrelated to this release; they are tracked separately.
- Passed three independent cross-provider reviewer lenses with zero defects
  before promotion.

## [0.9.72] - 2026-08-15

### Fixed

- Storage retention now actually reclaims. Worktree eligibility previously
  returned `candidate_count: 0` with `projected_bytes` byte-identical to
  `current_bytes` while the repository held 43.3 GB against a 5 GB cap, because
  every attempt workspace stayed pinned through `rework_predecessor` and
  nothing released a superseded one. A superseded attempt whose successor has
  been sealed is now an eligible candidate, and exceeding the cap forces
  reclamation of the oldest superseded lineage instead of reporting nothing to
  do.
- Live-worktree protection is keyed on `launch_request_id`, the field the claim
  path actually writes. It was keyed on `accepted_request_id`, which production
  only writes once a review is accepted and the card has flipped to finished —
  so every `processing` and `review` card had it empty and its live worktree was
  unprotected.
- Protection is no longer recency-bounded. It previously read only the most
  recent rows, so past that window a live card silently lost protection; any
  fixed row cap carries the same defect under a different number. Liveness is
  now resolved from one unbounded read of the canonical `tasks` lifecycle
  columns, whose only bound is the exact size of the table.
- Retention age is an injected input rather than an observed filesystem
  property, so eligibility is deterministic and testable without mutating file
  mtimes.
- When task lineage cannot be read at all the planner fails closed rather than
  treating an unreadable attempt as safe to reclaim.

### Validation

- `tests/test_storage_retention.py` and `tests/test_storage_retention_reclaim.py`:
  13 passed, covering the superseded-lineage case, the over-cap forcing case,
  the protected-live-worktree case, live protection independent of table size,
  and preview/executor agreement.
- Measured on this repository after promotion: `candidate_count` 0 → 140,
  `candidate_bytes` 0 → 17.4 GB, `projected_bytes` 43.3 GB → 25.9 GB, with the
  two live worker worktrees correctly protected.
- Passed an independent cross-provider correctness reviewer lens before
  promotion.

## [0.9.71] - 2026-08-15

### Fixed

- Allow a card owner to release its own claim. The card-scoped write-action set
  omitted `launch-failed`, so `release_stale_reservation_claim` was refused with
  `card_scoped_action_not_allowed:launch-failed` even with writes enabled. The
  owner that legally created a claim therefore had no legal way to release it,
  and any reconciled reservation stranded its card in `processing`/`claimed`
  with `launch_request_id` still attached — permanently, against a later claim's
  `launch_request_conflict`.
- The action set is now a named `_CARD_SCOPED_ACTIONS` frozenset used by both
  `_task_id_from_write_args` and `_check_card_scoped_write_authority`, so the
  two call sites can no longer drift apart. Codex authority is unchanged and
  still restricted to `launch-blocked`.

### Validation

- 142 passed across the process-launcher, Windows launch-lock and task-engine
  suites; 199 passed across the completion quality-gate, quality-verdict,
  combined-tree and worker-workspace suites; `ruff check src/aiworkhub/core.py`
  clean.

## [0.9.70] - 2026-08-15

### Fixed

- Resolve coordinator routing from the active verified manager route instead of
  pinning it to Codex. A repository whose verified manager route is Claude no
  longer reports `automatic: codex`, `route_pending` and
  `codex_thread_id_not_observed` while `manager_identity.provider` says
  `claude` in the same response, and no longer raises a coordination Route
  warning that no operator action can clear.
- Codex coordinator routing, its thread observation and every reported reason
  string are unchanged when the active verified route really is Codex, and a
  repository with no verified route still fails closed rather than defaulting
  to either provider.

### Validation

- `tests/test_coordinator_routing_provider_parity.py`: 10 passed, covering the
  Claude route resolving, the Codex route staying byte-identical, and the
  no-verified-route case failing closed.
- Combined-tree run with the 0.9.69 changes: 143 passed; `ruff check
  src/aiworkhub scripts tests` clean.
- Passed an independent cross-provider correctness reviewer lens before
  promotion.

## [0.9.69] - 2026-08-15

### Added

- Deliver worker callbacks to a verified Claude manager without manual polling,
  so `review_ready` and `worker_failed` transitions reach the manager's active
  MCP session instead of waiting in the inbox until it happens to poll. The
  existing lease/ack contract is unchanged: one verified manager route holds a
  batch, ack stays mandatory, an unacked batch stays redeliverable, and a route
  whose provider, repo_id or session identity does not match never receives it.
- Report a truthful provider-specific state from `dispatcher_health` on a Claude
  route rather than `problems=[dispatcher_unregistered]` with
  `selected_provider=codex`, which read as a broken dispatcher where none is
  expected.

### Changed

- Run explicit platform-owned regression manifests on the Windows and macOS CI
  jobs, covering native process/lock/temp/validation and Darwin
  identity/launch/PATH/RSS/cleanup behaviour. Each OS step preflights with
  `pytest --collect-only` and fails if a manifest entry collects nothing, so CI
  can no longer pass silently on zero collected platform tests. Linux coverage
  and the existing install/VSIX checks are unchanged.
- Pin the three-OS CI contract with `tests/test_cross_platform_ci_contract.py`,
  which fails if an OS manifest, a collection guard, or the full Linux suite is
  removed.

### Validation

- Combined-tree run of the promoted changes: 133 passed; `ruff check
  src/aiworkhub scripts tests` clean.
- Each change additionally passed three independent cross-provider quality
  reviewer lenses (correctness, security, code quality) before promotion.

## [0.9.68] - 2026-08-15

### Fixed

- Bind every new VS Code LM request to the exact fresh editor window selected
  by readiness, preventing another or stale window from stealing the request.
- Enforce the target-window identity again after the atomic claim and publish
  claimant window, process, extension version and bridge capabilities in live
  progress and terminal response receipts.
- Preserve rolling-upgrade compatibility for legacy untargeted requests while
  keeping malformed requests bounded and fail-closed.

### Validation

- VS Code LM bridge Python suite: 46 passed; full extension suite and
  changed-file Ruff/diff checks passed.

## [0.9.67] - 2026-08-15

### Fixed

- Keep the VS Code LM forced semantic-edit stage fully offline: the stage tool
  never traverses MCP, and only that tool is advertised during the phase.
- Treat a role-correct Source Graph request during forced staging as one bounded
  corrective phase violation; repeated requests fail structurally without
  poisoning manager/worker authority or executing the disallowed call.
- Preserve exact native/text provider tool-call history while closing the
  `mcp_unavailable` → `tool_not_allowed` self-dogfood loop.
- Include the already accepted route-identity, task-store migration and
  candidate Source Graph follow-up fixes accumulated after 0.9.66.

### Validation

- Full VS Code extension suite passed; focused Python regression suite: 290
  passed, 1 skipped; changed-file Ruff and diff checks passed.

## [0.9.66] - 2026-08-15

### Fixed

- Clone the verified canonical Source Graph generation for quality reviewers
  instead of running a full repository rebuild before every reviewer launch.
- Reconcile only the review packet's exact changed paths, publish each
  candidate database atomically and keep runtime reviewer queries read-only.
- Fail closed before provider registration when Source Graph authority,
  repository identity, schema or build revision cannot be verified exactly.
- Keep three concurrent reviewer overlays isolated while an independent
  canonical coordinator query remains responsive.
- Remove the remaining mypy errors from the touched reviewer launch and worker
  Source Graph boundaries without suppressing type checks.

### Validation

- Candidate Source Graph suite: 18 passed; focused reviewer launch suite:
  15 passed; Ruff, mypy, extension tests and diff checks passed.

## [0.9.65] - 2026-08-15

### Fixed

- Copy quality-review overlay files by content without preserving host metadata,
  avoiding cross-platform chmod and ownership failures in isolated workspaces.
- Keep VS Code LM reviewer tool history structurally valid through bounded
  stage/final corrections and enforce request-scoped tool authority before
  invocation.
- Persist monotonic NeedFix reopen generations and mint deterministic `-rN`
  successor task IDs without reusing archived task identities.
- Fail soft when untrusted provider usage output contains pathologically deep,
  malformed or undecodable JSON instead of terminating a healthy worker.

### Validation

- Independent correctness, security and code-quality reviews passed for the
  accepted candidates; focused Python and extension suites, Ruff, mypy and
  diff checks passed before packaging.

## [0.9.64] - 2026-08-15

### Fixed

- Build distinct packet-bound reviewer Source Graph databases concurrently
  instead of serializing every reviewer behind one process-global lock.
- Single-flight only callers targeting the same candidate database and publish
  verified temporary indexes atomically before reviewer queries can use them.
- Keep long-running reviewer prewarm reservations alive only while the exact
  owner PID/start identity matches; unknown, dead or recycled identities fail
  closed.

### Validation

- 122 reviewer-launch and candidate Source Graph tests pass (1 skipped), with
  independent correctness, security and code-quality review plus Ruff and
  diff checks.

## [0.9.63] - 2026-08-14

### Fixed

- Publish one canonical quality-review finding contract across reviewer
  instructions, the callable MCP JSON schema, normalization and sealed
  receipts.
- Require `severity`, `summary` and `evidence` in the generated reviewer
  tool schema, reject undocumented aliases and report missing keys exactly.
- Preserve exactly-once review submission counts for both clean reports and
  authenticated non-empty findings.

### Validation

- 143 reviewer contract/launcher tests and 64 quality-evidence tests pass;
  changed-file Ruff and diff checks pass.

## [0.9.62] - 2026-08-14

### Fixed

- Bind reviewer-spawn ownership to an exact PID/start-ticks compare-and-swap so
  a recycled or stale process identity is never accepted as the live reviewer
  launch.
- Recover lost-ack and extension-host reload handoff for reviewer spawns with
  idempotent re-binding instead of leaking a reservation or double-launching.
- Terminalize live providers only on process/terminal evidence; elapsed or
  quiet time alone never kills a running model.

### Validation

- Release metadata check for v0.9.62 passes; extension and release-metadata
  suites pass.

## [0.9.61] - 2026-08-14

### Fixed

- Bound pre-provider quality-review packet preparation independently from
  provider runtime, so a silent or wedged packet build terminalizes every
  pid-null reservation truthfully without imposing a model timeout.
- Publish exact preparation phases and heartbeats under the existing reviewer
  request identity while keeping status reads bounded and non-blocking.
- Preserve single-flight packet reuse, distinct reviewer launches and
  exactly-once failure propagation across all three review lenses.

### Validation

- Reviewer reservation and related launcher/reviewer suites: 47 passed.
- Ruff, mypy and whitespace checks pass.

## [0.9.60] - 2026-08-14

### Fixed

- Prepare one immutable quality-review packet per target with a per-target
  single-flight, so correctness, security and code-quality lenses reuse one
  heavy preparation while retaining distinct reviewer launches.
- Propagate the preparation owner's exact success or failure to concurrent
  reviewer waiters without applying elapsed-time death rules to providers.
- Make the Linux seccomp metadata-broker listener handoff bounded and
  observable across success, EOF, protocol violation, child error and timeout
  paths while preserving fail-closed sandbox authority.

### Validation

- Reviewer reservation/process-launch regression suite: 134 passed, 1 skipped.
- Validation sandbox portability suite: 80 passed.
- Changed-file Ruff and whitespace checks pass.

## [0.9.59] - 2026-08-14

### Fixed

- Run all extension-owned and stable Codex/Claude/Copilot MCP stdio lanes on
  the bounded parallel backend instead of the SDK lane that could remain
  poisoned after a bare `-32602 Invalid request parameters("")` response.
- Keep long-running tool calls isolated with per-request correlation and a
  locked response writer, while malformed parameters fail before dispatch and
  produce a bounded repository-local protocol alert.
- Recognize the exact live empty-detail `Invalid request parameters("")`
  shape in the dashboard client's owned-child recovery path.

### Validation

- MCP backend, packaged-runtime and protocol tests: 30 passed.
- Workspace/Codex configuration and reloadless recovery Node tests pass.
- Changed-file Ruff and whitespace checks pass.

## [0.9.58] - 2026-08-14

### Fixed

- Reject empty-string and non-object JSON-RPC parameters before child or
  sideband dispatch with a stable, non-empty structured reason.
- Record one bounded, redacted, repository-local protocol alert without raw
  request parameters, credentials, prompts, tokens or external host paths.
- Keep valid empty-object calls serviceable and isolate malformed requests so
  concurrent valid MCP clients are not globally poisoned.
- Bind app-server sideband sockets from a short CWD-relative name while
  retaining the exact repository-owned endpoint, owner-only permissions,
  per-repository isolation and cleanup on long retained workspace paths.

### Validation

- MCP stdio, app-server mux and dashboard protocol suite: 115 passed.
- Changed-file Ruff and whitespace checks pass.

## [0.9.57] - 2026-08-14

### Fixed

- Publish truthful Source Graph refresh lifecycle and generation metadata
  without retaining a stale canonical generation after a successful build.
- Make unchanged-file detection content-hash authoritative, including
  same-size/same-mtime mutations, while preserving deterministic incremental
  publication and bounded single-writer behavior.
- Keep scoped analytics, pagination, coverage metadata and repository
  aggregates within the exact requested path boundary.
- Resolve exact same-repository JavaScript/TypeScript import bindings without
  overstating ambiguous or external targets.
- Replace repeated git-metrics list membership with an order-preserving set
  hot path; the deterministic comparison receipt improves from 79,972 to 8.

### Performance

- Use host-adaptive parallel content hashing: 15 workers on a 16-CPU host in
  the release canary, with 691 unchanged files reconciled in about 43 ms.
- Reuse unchanged index-quality evidence instead of repeating graph-wide SQL
  on a no-op refresh.

### Validation

- Combined Source Graph, daemon, MCP serialization and git-metrics suite:
  213 passed; changed-file Ruff and whitespace checks pass.

## [0.9.56] - 2026-08-14

### Fixed

- Fail closed on empty relative Python import module targets instead of
  calling `Path.with_suffix()` on `/` and aborting the entire Source Graph
  refresh before canonical generation publication.

### Validation

- Source Graph regression suite passes: 106 passed.
- A repository-sized private full-index build exercises the repaired resolver
  without touching the canonical generation.

## [0.9.55] - 2026-08-14

### Fixed

- Run synchronous FastMCP tools outside the SDK event loop so slow provider,
  reviewer and finalization calls no longer block bootstrap, status or dashboard
  requests on the shared MCP runtime.
- Dispatch fallback stdio JSON-RPC requests through a bounded adaptive worker
  pool while preserving exact request-id correlation and serialized writes.

### Validation

- MCP server, runtime wiring, schema, lifecycle serialization, app-server mux
  and control-plane concurrency regressions pass: 75 passed.
- Changed-file Ruff and whitespace validation pass.

## [0.9.54] - 2026-08-14

### Fixed

- Count authenticated, fresh Source Graph zero-hit queries as real worker
  invocations while keeping evidence usefulness, cache and authority checks
  separate and fail-closed.
- Bind Codex app-server sideband correlation to one exact child generation,
  coalesce concurrent identical calls and consume late sideband responses
  instead of leaking them into the extension client.

### Validation

- Worker MCP gate/audit regressions pass: 199 passed and 1 skipped.
- App-server mux regressions pass: 55 passed, including concurrent distinct,
  duplicate and late-response correlation cases.

## [0.9.53] - 2026-08-14

### Added

- Resolve Python cross-file function calls only when exact import alias,
  source-line call syntax and one canonical Python target agree.
- Expand balanced Rust `use` trees, including nested braces, visibility,
  aliases, `self` and glob leaves while retaining lexical authority.

### Safety

- Unrelated Python receivers, ambiguous targets and malformed Rust use trees
  remain explicitly unresolved instead of receiving guessed graph edges.

### Validation

- The combined Source Graph, adaptive parallelism and validation-portability
  regression family passes: 314 passed and 5 skipped.

## [0.9.52] - 2026-08-13

### Fixed

- Give full dashboard aggregation its own bounded request budget instead of
  the lightweight control-plane RPC budget.
- Classify a delayed dashboard snapshot as degraded while preserving the last
  truthful data and the live MCP connection, rather than reporting Offline.
- Stop immediate heavy-snapshot retries after a request deadline to prevent a
  retry storm from amplifying repository-load latency.

### Validation

- The complete VS Code extension suite passes, including dynamic timeout
  classification, transport/runtime failure separation and no-retry evidence.

## [0.9.51] - 2026-08-13

### Fixed

- Project `source_graph_ensure_started` and `source_graph_health` from one
  canonical committed-generation snapshot instead of mixing process-local and
  database metadata (NF148/NF149).
- Give every coalesced Source Graph refresh wave a durable repository-local job
  ID and observable queued/running/succeeded/failed receipt with bounded report
  or error evidence.
- Fail closed when the refresh receipt cannot be persisted, eliminating silent
  `queued=true` plus stale/empty-diagnostic states.

### Validation

- Source Graph daemon lifecycle and indexing regressions pass: 130 tests plus
  changed-file Ruff and `git diff --check`.

## [0.9.50] - 2026-08-13

### Fixed

- Serialize every concurrent MCP request and notification through one
  per-child FIFO writer so complete newline-delimited JSON-RPC frames preserve
  their exact request ID and non-empty parameters under load (NF199).
- Correlate out-of-order replies independently while fencing late write
  callbacks from a replaced child, so one request cannot resume or corrupt the
  replacement runtime's queue.
- Detect repeated bare `-32602 Invalid request parameters` responses only after
  a valid response and reloadlessly replace that exact poisoned child; detailed
  caller validation errors remain non-fatal.

### Validation

- The shared-stdio regression proves three simultaneous parameterized requests
  are framed FIFO and resolve correctly from out-of-order responses; the full
  VS Code extension suite passes.

## [0.9.49] - 2026-08-13

### Fixed

- Accept full `stat().st_mode` values for chmod/fchmod only inside the exact
  request-owned validation boundary by stripping recognised file-type bits,
  while continuing to reject setuid, setgid, sticky and out-of-scope metadata
  mutations (NF155).
- Size Source Graph extraction from process-visible CPU affinity with bounded
  reserve and ceiling, keep nested pools serial, and preserve deterministic
  single-writer SQLite merge ordering (NF154).
- Expose truthful extraction selection and fallback telemetry in every Source
  Graph build receipt (NF154).

### Validation

- The combined portability, parallelism and Source Graph suite passes: 185
  tests, plus changed-file Ruff and `git diff --check`.

## [0.9.48] - 2026-08-13

### Fixed

- Add a single repository-owned temporary-data authority under
  `.aiworkhub/temp`, with request-scoped worker and validation directories,
  exact owner identity, bounded retention and fail-closed path handling
  (NF201).
- Keep validation `HOME`, `TMPDIR`, Python caches and executable scratch
  repository-local and isolated between concurrent repositories and requests
  (NF201).
- Preserve review/rework candidates until explicit disposition while exposing
  protected retention rows and safely cleaning disposable or exact dead-owner
  artifacts through the existing retention authority (NF201).

### Validation

- The complete NF201 focused suite passes from the checkpoint tree: 226 tests,
  plus changed-file Ruff and `git diff --check`.

## [0.9.47] - 2026-08-13

### Fixed

- Retain the authenticated sealed read-only reviewer receipt after standalone
  acceptance removes the reviewer's read-only workspace, so target acceptance
  reuses it only when the immutable process event and both task-card receipt
  copies agree exactly (NF131).
- Bind each sealed receipt to the exact target, reviewer, provider, claim epoch
  and deterministic lowercase 64-hex submission identity, and pin the retained
  quality-review envelope to the reviewed-parent claim epoch and adapter
  identity, rejecting epoch- or provider-mismatched reuse (NF131).
- Fail closed on malformed, unverified, duplicate, wrong-type/bool or
  identity-mismatched receipts through exact schema enforcement instead of
  falling through to generic empty-hash equality, while preserving the writable
  changed-path hash fallback for non-reviewer targets (NF131).

### Validation

- Extended the sealed accepted-reviewer receipt and reviewer-contract fixtures
  to cover retention, exact binding, fail-closed schema rejection and the
  preserved writable hash fallback (NF131).

## [0.9.46] - 2026-08-12

### Fixed

- Guard initialization-time task topic migration with `json_valid`, so one
  malformed legacy `card_json` row cannot abort repository startup while
  valid topic backfill remains unchanged (NF166).
- Separate global active-card collision health from exact per-card launch
  eligibility, keeping unrelated collision-free work runnable and dashboard
  summaries truthful (NF187).
- Normalize substantive GLM correctness-review findings into the mandatory
  quality-review submission path, preserve provider-valid tool-call/result
  history, and keep manager/worker tool authority fail-closed (NF168, NF169).
- Redact complete Bearer credentials from portable evidence text and paths
  before dashboard serialization (NF167, PR #25).

## [0.9.45] - 2026-08-12

### Fixed

- Added `superseded` to the canonical exact-status taxonomy so persisted
  superseded tasks remain visible as a separate non-active bucket instead of
  raising `KeyError('superseded')` and degrading the dashboard snapshot
  (NF159, NF170, PR #23).

## [0.9.44] - 2026-08-12

### Fixed

- Windows Claude Code VS Code manager identity now verifies the exact direct
  parent through a native Toolhelp snapshot plus process-token SIDs and the
  per-PID `~/.claude/sessions/<pid>.json` descriptor instead of reading
  `/proc`, which does not exist on Windows. POSIX verification, canonical
  repository/UUID/process checks, and Codex identity behavior remain
  fail-closed (#17).
- Recognized Windows advisory-lock contention from a duplicate finalizer now
  defers reconciliation without terminalizing the task; unexpected lock and
  finalization errors retain the existing fail-closed path (#18, PR #19).
- Deterministic validation now gives every request isolated writable mypy,
  temporary and Ruff cache state, keeps stdin deterministic, and preserves a
  bounded non-secret diagnostic packet for mypy internal errors (NF180,
  PR #21).

### Thanks

- Giorgi Khaburdzania ([@Ba1u1994](https://github.com/Ba1u1994)) for reporting
  #17 and #18 and contributing the #18 fix in PR #19.

## [0.9.43] - 2026-08-11

### Fixed

- VS Code LM worker and quality-review tool calls are exposed under
  worker-scoped names and dispatched through the authenticated worker bridge,
  never through manager authority (NF118).
- Read-only reviewer completion is sealed into a durable receipt; result and
  retry paths converge on the same verified receipt instead of diverging
  (NF131).
- Reviewer quality-evidence packets above the bounded argv threshold use file
  transport, avoiding E2BIG on platforms with small argument limits while
  preserving the packet-size contract (NF134).
- The large-packet regression fixture stays within production packet limits
  and exercises the file-transport path that it was meant to validate (NF134).

## [0.9.42] - 2026-08-10

### Fixed

- Versioned the local staged-edit bridge generation after live evidence showed
  that an older in-memory Extension Host could keep routing semantic-edit stage
  calls through MCP even though the installed 0.9.41 files already contained
  the offline collector. Host receipts now expose exact extension version and
  tool transport so the same stale-runtime condition fails visibly.

## [0.9.41] - 2026-08-10

### Added

- Text-only and native VS Code LM workers can stage one bounded replacement or
  create at a time and finish with a summary-only tool call. AIWorkHub keeps
  the hash-bound fragments in memory and assembles the final semantic-edit
  envelope offline, avoiding fragile full-response regeneration.
- Added bounded, gap-aware dashboard event-stream primitives with deterministic
  overflow and authoritative-resync behavior. Server and webview wiring remain
  an explicitly tracked follow-up rather than an implied completed claim.

### Changed

- Extracted runner/topic allowlist authority from the core task module into a
  dedicated policy module while preserving public imports and exact decisions.

### Fixed

### Fixed

- Integrated PR #16: authenticated VS Code LM read-only finalization now
  exercises its durable token-bound cancel decision instead of bypassing the
  bridge contract, while required-create range edits are revalidated as
  assembled create content and report truthful create/whole-file metrics.
- Coordinator-authorized validation-only replay now skips VS Code bridge
  cancellation only when durable metadata proves that no provider was
  launched; ordinary editor-hosted finalization remains fail-closed.

### Tests

- Added required-create placeholder, `.pyi` ellipsis, bridge-lifecycle and
  provider-free replay regressions, plus staged-edit parity checks for
  text-only and native tool-calling providers and atomic rejection of
  overlapping staged ranges.

## [0.9.40] - 2026-08-10

### Fixed

- Semantic-edit fidelity now rejects literal ellipsis placeholders when a
  required file is created through a range edit and when retained rework
  attempts to preserve an existing placeholder unchanged.
- Repository quality checks declared as `{python} -m ruff` or
  `{python} -m mypy` now use their trusted PATH entrypoint when the active MCP
  interpreter cannot import that module. Receipts preserve both the declared
  command and the exact executable that actually ran.
- Quality-verdict lens aggregation now has an explicit typed mutable shape,
  keeping strict mypy validation green as structured observation evidence is
  accumulated.
- Task supersession now fails before archiving when its replacement task ID
  does not exist in the same repository, preventing a transient broken
  replacement edge in the Plan DAG.

### Tests

- Added focused placeholder-fidelity and quality-tool-resolution regressions.
  Local qualification passes 3,450 Python tests with 35 platform-dependent
  skips, the complete extension suite, Ruff and strict mypy.

## [0.9.39] - 2026-08-09

### Added

- Added strict evidence-level contracts, scoped audit packets and
  manager-gated learning commits so assurance claims, bounded review scope
  and durable lessons have explicit provider-neutral schemas.
- Added a bounded attempt-artifact manifest with deterministic serialization,
  exact hash/size coverage, required/optional presence truth, duplicate-key
  rejection and fail-closed path, identifier and media-type validation.

### Fixed

- Windows MCP runtime routing now binds to the active workspace route instead
  of retaining stale repository authority across editor windows.
- Windows validation no longer treats POSIX world-writable mode bits as ACL
  evidence, and Ruff/mypy quality gates run through the selected trusted
  Python runtime.
- Canonical blocked `finalize_failed` tasks now appear exactly once in the
  completion inbox and are enriched from the exact process request without
  losing a truthful `workspace_retained=false` value.
- Windows runtime regression probes remain executable and portable on Linux,
  preventing platform simulation from constructing unsupported `WindowsPath`
  objects.

### Tests

- Added adversarial artifact-manifest coverage and Windows runtime,
  finalization and completion-inbox regressions. The full Python suite passes
  3,125 tests with 28 platform-dependent skips; the 12-test release
  qualification matrix, extension suite, Ruff, mypy and diff checks pass.

## [0.9.38] - 2026-08-09

### Fixed

- Blocked-rework recovery now has an explicit coordinator-only clean-root
  path when retention has already removed the exact predecessor workspace.
  The recovery preserves the predecessor request identity, changed-path
  hashes, feedback and audit history while removing only the stale workspace
  materialization authority.
- Clean-root recovery fails closed when the predecessor workspace still
  exists, repository/request identity is inconsistent, paths or hashes are
  malformed, or validation-only replay is requested. Exact replay continues
  to require the original retained bytes.
- A task already returned to `pending` by ordinary recovery can receive the
  same one-episode clean-root authorization without creating a replacement
  task or duplicating the earlier recovery event.
- The product roadmap now contains a current `0.9.37` evidence checkpoint that
  separates five real pending foundations from 79 historical lifecycle
  attempts and records the current Windows/Source Graph qualification gates.

### Tests

- Added missing-predecessor, already-recovered and validation-only replay
  regressions. Full Python qualification passes 2,641 tests with 28 skipped.

## [0.9.37] - 2026-08-09

### Fixed

- Dashboard runtime-repair failures now report the real bounded attempt count
  instead of remaining at the misleading `0/3` state.
- The dashboard `Retry` action now advances both MCP runtime repair and the
  snapshot request. A recovered child therefore clears the stale repair
  banner without requiring a window reload.
- A partially spent repair episode survives explicit retry so attempts
  progress truthfully through `1/3`, `2/3`, and `3/3`; only an exhausted or
  blocked episode starts a fresh bounded budget.
- Failed repair is rendered as `Runtime repair failed`, rather than the
  inaccurate `Runtime repair in progress`, and transport-health failures keep
  their exact bounded reason.
- A stale Windows process-event append lock no longer blocks status, cancel,
  finalization recovery, and unrelated launches together. Timed-out writers
  atomically publish a unique immutable ledger segment; readers merge those
  segments by canonical event time, preserving append-only audit truth without
  deleting or stealing another process's lock.

### Tests

- Added repair-attempt telemetry, explicit-retry budget, Webview title,
  runtime-plus-snapshot retry, immutable ledger fallback, chronological merge,
  and fail-closed non-timeout regressions.

## [0.9.36] - 2026-08-09

### Fixed

- Provider-free `validation_only_replay` now permits an explicitly empty
  validation contract. Hash-pinned required-output verification remains
  mandatory, while Windows executable-scratch provisioning is skipped.
- `release_pending` is now an explicit retained-workspace finalization-retry
  state. The retry does not relaunch a provider or require a synthetic
  `finalize_failed` task transition.
- Request status/collection no longer waits on a concurrently owned Windows
  finalizer lock. It returns the current durable snapshot with
  `reconciliation_deferred=request_lock_busy` instead of a 20-second MCP
  transport timeout.
- Writable task creation and NeedFix conversion now reject missing
  `required_outputs` before persistence, matching the launcher contract.
  NeedFix conversion additionally requires the exact workforce `runner` and
  `topic`, and the Webview carries the previewed plan and digest into commit.
- Accepted, fully evidenced NeedFix items can use the separate
  `resolve_verified` manager action without fabricating a task; ordinary
  task-backed resolution remains `task_created -> resolved`.

### Tests

- Added Windows release-pending lock contention, provider-free replay,
  manager-verified NeedFix closure and fail-before-create conversion
  regressions. Full Python qualification passes 2,634 tests with 28 skipped.

## [0.9.35] - 2026-08-09

### Fixed

- Empty validation contracts now short-circuit in the finalizer before route
  resolution, executable-scratch provisioning, or child-process dispatch.
  This makes `validation=[]` authoritative for ordinary workers, retained
  finalization retries and provider-free validation-only replay on Windows.
- Retryable `validation_exec_scratch_unavailable` infrastructure failures now
  use the canonical operational-failure transition instead of masquerading as
  quality-review work. Task review queue, completion inbox and reviewer
  eligibility therefore agree on the exact request state while the retained
  workspace remains available for provider-free retry.
- Blank or non-list validation contracts fail explicitly instead of reaching
  sandbox setup with ambiguous execution intent.

### Tests

- Added finalizer-level empty-validation route/scratch denial and operational
  scratch classification regressions; the focused lifecycle matrix passes 69
  tests.

## [0.9.34] - 2026-08-09

### Fixed

- Unified preflight now distinguishes usable-but-reduced provider coverage
  from full readiness. A host with some unavailable routes reports
  `status=degraded`, exposes the exact unavailable adapter reasons, and the
  dashboard renders `Degraded` plus secure-route coverage instead of the
  misleading `Ready · 0 blockers`. Zero launchable routes are now a global
  preflight blocker.
- Coordinator-authorized `validation_only_replay` accepts its truthful
  `deterministic_validation` execution-lane receipt without comparing it to
  the predecessor provider sandbox. The original adapter identity still
  selects the validation safety boundary, no provider is relaunched, and the
  exception remains forbidden for ordinary worker execution.

### Tests

- Added partial/zero route-coverage preflight regressions, dashboard truth
  contract coverage, and positive/negative validation-only replay backend
  identity tests.

## [0.9.33] - 2026-08-09

### Fixed

- Retained finalization retry now recovers the narrowly operational
  `validation_exec_scratch_unavailable` failure after a successful worker exit,
  without relaunching or charging the provider. Ordinary failing tests and
  product validation errors remain non-retryable and require normal rework.
- Completion inbox classification keeps retryable validation-scratch failures
  out of the ordinary quality-review queue and exposes them as operational
  failures, eliminating the transient `review` /
  `quality_review_target_not_review_ready` split-brain.

### Tests

- Added positive provider-free scratch-recovery tests and negative product-test
  retry denial at both process and durable task-store boundaries, plus inbox
  classification coverage.

## [0.9.32] - 2026-08-09

### Fixed

- Windows validation scratch probing is now platform-aware. Editor-hosted
  worker finalization prefers the request-private retained workspace, executes
  a native probe copy from the candidate directory, and verifies Windows
  metadata with atomic replace instead of requiring POSIX shebang/chmod
  semantics. Enterprise-denied global `%TEMP%` no longer masks a usable
  repository-owned runtime boundary as `noexec`.
- Tasks with no validation commands are regression-protected from provisioning
  executable scratch, so a successful no-validation worker can proceed to
  canonical review without an unrelated scratch capability gate.

### Tests

- Added Windows-simulated native execution, atomic metadata, private scratch
  priority and empty-validation regressions. The focused finalization and
  validation matrix passes with 167 tests; the full repository suite passes
  with 2,615 tests and 28 platform skips.

## [0.9.31] - 2026-08-09

### Fixed

- VS Code LM in-process workers now carry their exact launch backend through
  worker-exit validation and manager acceptance. Windows finalization no longer
  rediscovers the native CLI/AppContainer backend after an editor-hosted worker
  has exited successfully; native CLI routes remain fail-closed without their
  required OS sandbox.
- Finalizer implementation failures now enter the canonical blocked
  `finalize_failed` state instead of fabricating an ordinary review candidate.
  Completion surfaces separate legacy finalization failures from actionable
  review entries, and `review_ready` is derived from the exact terminal
  substatus and request state.
- Added manager-authorized retained-workspace finalization retry. It verifies
  the exact request, successful supervisor receipt and workspace identity, then
  re-runs deterministic finalization without launching or charging the provider
  again; legacy 0.9.30 `review/finalize_failed` rows are also recoverable.

### Tests

- Added editor-route validation and backend-drift regressions, native-adapter
  bypass denial, retained finalization retry/legacy recovery coverage, MCP
  wiring checks and review-inbox split-brain fixtures. Focused regression tests
  pass with 252 tests; the full repository suite passes with 2,611 tests and 28
  platform skips.

## [0.9.30] - 2026-08-09

### Fixed

- Windows isolated-workspace staging now routes policy and dependency seed
  publication through the shared bounded `atomic_replace` helper and closes its
  own temporary writer handle before rename. Neither that handle nor a transient
  destination reader (observed on read-only `AGENTS.md`) now turns workspace
  preparation into `WinError 32`; source bytes and policy files remain unchanged
  and symlink/hardlink protections stay fail-closed.
- VS Code LM request publication now tolerates one repo-spool cleanup race and
  recognizes an immediate owner-only `.claim-*` move by the trusted extension
  host instead of reporting a false missing-request launch failure. Permission,
  parent-identity, ownership and mode failures are never retried.

### Tests

- Added transient Windows sharing-violation coverage, exact `AGENTS.md` source
  immutability and destination-byte checks, bounded spool reprovisioning, and
  immediate secure-claim publication regressions.

## [0.9.29] - 2026-08-09

### Fixed

- Manager-authorized validation-only rework now takes a deterministic,
  provider-free launch lane. Exact task, coordinator actor, predecessor
  request, hash manifest and claim epoch bindings fail closed before claim;
  inherited bytes are reverified by the ordinary finalizer before review.
- Provider credentials, prompts, adapter plans, worker MCP bootstrap and
  provider/supervisor processes are no longer created for validation-only
  replay. Sandboxed validations finalize asynchronously so long-running checks
  do not hold the initiating MCP request open.
- Replay accounting now records an explicit deterministic execution mode and
  `provider_launched=false`, retaining the requested model as provenance while
  avoiding fabricated provider-usage observation.

### Tests

- Added stale-epoch/forged-actor fail-closed coverage, a no-provider launch
  regression that forbids credential, adapter and process calls, and truthful
  zero-provider accounting assertions. The focused launcher/recovery suite
  passes with 120 tests; the repository suite passes with 2,481 tests.

## [0.9.28] - 2026-08-09

### Fixed

- `allow_unchanged_required_outputs` now means an output may remain identical;
  a valid changed output is no longer rejected merely because the same path is
  also permitted to remain unchanged.
- Isolated-workspace seed copying now uses descriptor-bound, no-follow source
  verification and atomic destination replacement, preserving file modes while
  rejecting symlink/non-regular races.
- VS Code LM semantic edits now reject known placeholder, omission and TODO
  payloads plus punctuation-only destructive shrink before any mutation across
  V1 files, V2 replacements/creates and V3 ranges/creates. Valid concise code,
  generics, comparisons, JSX/XML, prose ellipses and explicit V3 deletion remain
  supported.

### Tests

- Added exact allow-unchanged contract and descriptor-safe seed-copy regression
  coverage; the focused NF96/NF98 suite passes with 94 tests.
- Added historical placeholder, false-positive, V1/V2/V3 integration and
  no-partial-write fidelity regressions; the focused VS Code LM suite passes
  with 97 tests.

## [0.9.27] - 2026-08-08

### Fixed

- Source Graph single-file index and remove operations now advance an atomic,
  repository-scoped mutation generation, so cached zero-hit queries cannot
  survive a successful targeted reindex or removal.
- VS Code LM `provider_response` heartbeats now prove transport liveness only;
  they no longer reset the meaningful-progress stall clock without a tool turn,
  final edit, terminal error, usage advance, or other auditable work.
- Supervisor receipts preserve both the latest observed progress sequence and
  the latest meaningful sequence even when a short worker exits after newer
  liveness-only events, with backward-compatible reading of older status
  artifacts.

### Tests

- Added atomic rollback, cross-repository isolation, legacy-marker and
  index/remove cache-generation regressions for Source Graph.
- Added all-adapter provider-response loop and old-status compatibility
  regressions, including a terminal-tail race fixture; the focused worker
  liveness suite passes with 51 tests.

## [0.9.26] - 2026-08-08

### Fixed

- Reviewer-child disposition now resolves the authenticated target binding
  from current `terminal_review.evidence.quality_review` receipts while
  retaining legacy root-card compatibility; conflicting durable bindings fail
  closed instead of leaving accepted reviewers silently stranded in Review.
- VS Code LM strict-JSON prompts explicitly distinguish missing native tool
  calling from the callable AIWorkHub bridge, preventing workers from falsely
  reporting that Source Graph and edit tools are unavailable.
- Supervisor failures retain bounded child return-code/stdout/stderr evidence,
  including a structured stderr fallback when the status artifact itself
  cannot be persisted inside a validation sandbox.

### Tests

- Added current/legacy/conflicting reviewer-binding regressions, supervisor
  diagnostic salvage coverage, and strict-JSON tool-availability prompt checks.

## [0.9.25] - 2026-08-08

### Fixed

- VS Code LM semantic edits preserve exact output bytes on Windows instead of
  allowing newline translation to invalidate the provider-side SHA-256 check.
- DeepSeek and GLM workforce entries truthfully advertise the callable
  repository tools exposed by their strict JSON bridge.
- Owner-private VS Code LM progress receipts are created with mode `0600`
  before atomic rename on POSIX, removing the rename-before-chmod observation
  window while retaining Windows portability.
- Repeated, byte-equivalent authenticated quality-review submissions are
  treated as one logical receipt after an ambiguous acknowledgement; any
  conflicting retry still fails closed.

### Tests

- Added deterministic CRLF/LF byte-identity, workforce capability,
  pre-rename owner-mode, and duplicate/conflicting reviewer-receipt
  regressions.

## [0.9.24] - 2026-08-08

### Fixed

- Windows VS Code LM launches no longer apply POSIX `chmod` operations to
  ACL-governed runtime paths, removing the pre-claim `WinError 5` failure
  boundary while preserving owner-only POSIX modes on Linux and macOS.
- The outer supervisor and inner worker now launch from the verified isolated
  worktree on Windows instead of the drive root; POSIX sandbox cwd behavior is
  unchanged.
- Supervisor spawn failures now identify `child_spawn` versus
  `job_assignment` without weakening mandatory Windows Job Object assignment.
- VS Code LM bridge and worker runtime JSON writers use the same platform-aware
  filesystem contract, including Unicode payloads.
- Source Graph first-build completion now hands control to waiters only after
  releasing the build guard, so an immediate refresh cannot return a stale
  generation while a newly created file is still awaiting extraction.

### Tests

- Added deterministic Windows ACL/cwd/spawn-phase and Source Graph build-handoff
  regressions. The focused launcher suite passes with 71 tests, Ruff passes,
  and the full Python suite passes with 2,536 tests and 28 environment-specific
  skips.

## [0.9.23] - 2026-08-08

### Fixed

- Workforce ranking now returns one directly executable `launch_contract`
  containing the exact worker runner, adapter and model, so managers no longer
  reuse the reserved `codex` coordinator identity for worker cards.
- Task creation rejects the exact coordinator runner before provider or
  workspace work begins, and legacy misuse now fails with a focused launcher
  diagnostic instead of the lower-level `card_scoped_codex_forbidden` error.
- Pre-claim launch blockers are persisted through coordinator authority while
  retaining exact card runner/topic checks; an invalid worker identity can no
  longer suppress its own operational blocker receipt.
- Source Graph retrieval evaluation now distinguishes a missing registry
  (`not_configured`) from a present malformed registry
  (`configuration_invalid`) and returns an actionable repair hint.

### Tests

- Added coordinator/worker identity, executable workforce receipt, legacy
  blocker persistence and retrieval-registry diagnostics regressions. The full
  Python suite passes with 2,523 tests and 28 environment-specific skips; the
  VS Code extension suite also passes.

## [0.9.22] - 2026-08-08

### Fixed

- Quality-review child cards now leave actionable Review when their parent is
  accepted: authenticated successful receipts finalize, redundant sibling
  attempts become durably superseded, and immutable task/event history remains
  available for audit.
- Reviewer-child disposition now binds both the exact target task and target
  request, preventing a receipt for an older candidate from being finalized by
  a later parent decision.
- Canonical task status preserves the durable `superseded` lifecycle instead of
  projecting it back to `pending`.
- VS Code LM multi-range semantic edits retain exact range fidelity and no
  longer corrupt unrelated file regions during a focused replacement.
- Dashboard telemetry separates actionable implementation reviews from
  quality-review receipt rows while retaining the total review-ready count.

### Tests

- Added focused lifecycle, idempotence, request-binding, dashboard KPI, and
  multi-range semantic-edit regressions; the full suite passes with 2,518 tests
  and 28 environment-specific skips.

## [0.9.21] - 2026-08-08

### Fixed

- Quality-review source evidence now enforces the per-file byte budget across
  all changed hunks cumulatively, preventing multi-hunk candidates from
  overflowing the reviewer packet while preserving explicit omission counts.
- Textual hunk headers now JSON-escape repository paths so control characters
  cannot create ambiguous reviewer instructions; the structured path remains
  exact and authoritative.

### Tests

- Added regressions for an NF3-shaped many-hunk candidate and for repository
  paths containing newline and control characters.

## [0.9.20] - 2026-08-08

### Fixed

- Quality-review packets now center bounded source evidence on the candidate's
  actual changed hunks instead of taking only each file's first 4,000 bytes,
  so late-file implementation and test changes remain reviewable without
  unbounded full-file payloads.
- Reviewer source-evidence rows now preserve exact candidate hashes, bounded
  segment metadata and explicit omission reasons for deleted, non-file and
  non-UTF-8 candidates while failing closed on malformed evidence.
- NeedFix status-validation errors now return the allowed lifecycle values and
  actionable schema guidance instead of an opaque rejection.

### Tests

- Added a regression proving that a changed symbol after a 4,000-byte
  unchanged prefix is visible with only the configured adjacent context.
- Added reviewer-packet segment and deleted-candidate contract coverage plus
  expanded NeedFix status-guidance tests.

## [0.9.19] - 2026-08-08

### Fixed

- Linux validation sandboxes now permit `chmod`/`fchmod` on process-owned
  directories strictly beneath the exact request scratch while retaining
  kernel-resolved path confinement, inode revalidation, ownership checks and
  fail-closed denial for the scratch root, symlinks, foreign targets and
  special files.
- Cost-ledger cache-hit ratios now use only cache-observed rows in both the
  numerator and denominator, preventing legacy or unobserved cache-token rows
  from inflating the ratio above its measurable population.
- The worker MCP module now finishes defining its rework-overlay constants and
  validators before entering the standalone server, preventing a startup
  `NameError` and closed stdio connection during real module launch.

### Tests

- Added focused directory metadata broker coverage for path and descriptor
  operations plus a live JSON-writer validation integration.
- Added a mixed observed/unobserved cache-metrics regression for cost-ledger
  aggregation.

## [0.9.18] - 2026-08-07

### Fixed

- Invalid emulated VS Code LM tool input now produces a bounded, redacted and
  actionable terminal diagnostic containing the known tool name, parser
  reason, protocol preview and recent turn trace instead of empty diagnostic
  fields.
- The structured invalid-input receipt is preserved through the outer bridge
  response without weakening fail-closed tool-input validation or exposing
  credential-shaped values.

### Tests

- Added a Node-backed regression that verifies real invalid-branch wiring,
  preview and trace bounds, secret redaction, and outer response propagation.

## [0.9.17] - 2026-08-07

### Added

- NeedFix conversion now produces a manager-confirmed, executable task card
  with durable task linkage, strict preview binding and lost-ack-safe
  idempotency instead of falling through to an unusable empty write scope.
- Retained rework workspaces now expose a request-scoped Source Graph overlay
  whose packet is bound to successor/predecessor identities, canonical digest,
  repo-relative paths and file hashes while the canonical index remains the
  immutable fallback for unshadowed files.

### Fixed

- Malformed NeedFix conversion cards now fail with typed contract errors for
  missing `task_id`, `title` or `objective` fields instead of leaking raw
  `KeyError`, and the conversion path is split into smaller auditable helpers.
- Source Graph rework overlays now validate request identity, avoid mutating
  frozen worker context, preserve thread isolation and clean request-private
  artifacts deterministically.
- The read-only Source Graph integration fixture now reports an explicit skip
  when a validation sandbox forbids the required `chmod`; environments that
  support the capability still execute the complete integration assertion.

## [0.9.16] - 2026-08-07

### Fixed

- An exact, manager-authorized validation-only replay now reaches review when
  the hash-pinned inherited output is byte- and mode-identical to canonical;
  the ordinary `no_effect` gate remains fail-closed for every other code task.
- Finalization regression coverage now exercises a truly zero-delta replay,
  including stale/missing authorization failures and structured replay evidence.

## [0.9.15] - 2026-08-07

### Added

- Worker finalization now consumes the exact one-episode validation-only
  replay authorization snapshotted at launch and attaches structured replay
  evidence to required-output and terminal review receipts.

### Fixed

- Hash-pinned, unchanged inherited predecessor outputs can be revalidated
  without fabricating an edit, while task, actor, predecessor, path/hash and
  claim-epoch mismatches continue to fail closed.

## [0.9.14] - 2026-08-07

### Added

- Blocked rework recovery now supports an explicit, one-episode
  `validation_only_replay` authorization bound to retained predecessor request
  identity, changed-path hashes and the next claim epoch.

### Fixed

- Linux Landlock validators now broker only verified `chmod`, `fchmod` and
  `fchmodat`-family operations on stable, request-owned regular-file targets
  beneath the exact validation scratch, without globally allowing metadata
  syscalls or using seccomp notification continuation.
- The metadata broker now rejects symlink scratch roots, requires libseccomp
  notification API level 5, closes the fork/parent-death race, preserves child
  exit status across listener teardown and bounds listener handshakes so a
  failed setup cannot hang validation.
- A live `run_validations` integration proves that unmodified `git init` works
  inside the secured validation scratch while traversal, symlink, hardlink,
  ownership, mode, identity and malformed-notification cases remain denied.

## [0.9.13] - 2026-08-07

### Fixed

- Validation scratch selection now rejects filesystems that execute files but
  cannot preserve the chmod metadata semantics required by Git and audit
  receipts, falling back to a compatible private scratch root.
- Approved repository-relative virtualenv validators such as
  `.venv/bin/ruff` now resolve to a trusted absolute canonical executable
  while receipts retain both the declared and actually executed argv.
- Focused integration coverage proves the portable scratch, Git initialization
  and declared-versus-executed validation evidence contract end to end.

## [0.9.12] - 2026-08-07

### Added

- Source Graph now exposes bounded single-file indexing and removal so newly
  created, changed or deleted files can update the readable generation without
  waiting for a full repository refresh.

### Fixed

- The MCP server now binds the canonical repository-scoped NeedFix store at
  startup, keeping dashboard and direct MCP reads on the same durable registry.
- Validation executable and temporary-path handling is now portable across
  isolated worktrees and restricted temporary filesystems, preserving declared
  versus executed command evidence without relying on an editable-worktree
  `.venv` or `/dev/shm` metadata operations.

## [0.9.11] - 2026-08-07

### Added

- NeedFix provenance, evidence, task-conversion previews and audit events now
  render as expandable structured trees, typed key/value rows and timeline
  cards instead of raw JSON text blocks.

### Fixed

- NeedFix primary, secondary and destructive actions now use explicit
  high-contrast foregrounds, backgrounds, borders, hover states and keyboard
  focus indicators across dark and light VS Code themes.

## [0.9.10] - 2026-08-07

### Fixed

- Existing repositories now receive the additive NeedFix schema during MCP
  startup, before the first read-only dashboard/list request; fresh repository
  initialization creates the same store in its canonical initialization step.
- The durable NeedFix store now accepts every intake kind exposed by the
  dashboard, including ideas, technical debt, optimizations, benchmark gaps,
  documentation drift, security risks, investigations and roadmap candidates,
  while preserving the original compatibility values.

## [0.9.9] - 2026-08-07

### Added

- A durable repository-scoped NeedFix registry now captures bugs, improvements,
  ideas, technical debt, optimization work, benchmark gaps, documentation
  drift, security risks, investigations and roadmap candidates without
  prematurely creating task cards.
- The native dashboard exposes NeedFix as a top-level status card and dedicated
  popup with bounded search, filters, detail, provenance, evidence, audit
  events and explicit lifecycle actions.
- NeedFix-to-task conversion is a two-step preview and confirmation flow. It
  creates a task card but never launches a worker automatically.

### Fixed

- NeedFix dashboard reads and writes now use bounded MCP tools instead of
  exposing storage paths or arbitrary tool execution to the Webview.
- Destructive archive, purge and lifecycle transitions require explicit user
  confirmation, while task conversion retains its preview until confirmed.

## [0.9.8] - 2026-08-07

### Added

- Coordinators can recover an explicitly blocked rework card through the
  public MCP surface while preserving its task identity, predecessor evidence,
  topic authority and exact task-store transaction boundary.

### Fixed

- VS Code LM bridge JSON writes now pin and revalidate the parent directory,
  reject symlink replacement, verify the final regular file through a bound
  descriptor, and remain portable on platforms without `os.getuid`.
- Atomic JSON regression coverage now protects parent-identity drift,
  symlink-decoy replacement and portable UTF-8 `0600` writes.

## [0.9.7] - 2026-08-06

### Fixed

- Worker launch collision checks are now scoped to the exact candidate task:
  unrelated planned collisions no longer freeze the whole queue,
  dependency-blocked pending cards do not claim write authority early, and
  ready overlapping launches select one deterministic winner.
- New claim and rework episodes clear stale terminal card fields while
  preserving predecessor evidence in append-only events and pinned rework
  receipts.

## [0.9.6] - 2026-08-06

### Fixed

- Coordinator review rejection can explicitly select and durably pin an exact
  same-repository, same-task retained predecessor after verifying workspace
  containment and current changed-path hashes.
- Omitting predecessor selection now defaults to the current reviewed request,
  while empty, foreign, missing, garbage-collected and hash-mismatched
  selections fail closed before state change or workspace cleanup.
- Rework materialization no longer silently falls back to an older candidate
  when a newer independently verified request ended with an unrelated
  infrastructure validation failure.

## [0.9.5] - 2026-08-06

### Added

- A strict SARIF 2.1.0 contract foundation normalizes deterministic findings
  without weakening evidence or path authority.
- Repository assessments are preserved as durable roadmap inputs instead of
  remaining transient chat context.

### Fixed

- Bare `mypy` validation commands now resolve to the trusted canonical
  repository virtual-environment executable while preserving distinct
  declared and executed argv receipts and fail-closed root/symlink checks.
- AI Memory FTS access now repairs legacy public-path schema state safely.
- Source Graph recovery now survives interrupted generations without making
  manager operations wait on a blocking rebuild.

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
