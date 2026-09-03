# AIWorkHub for VS Code — Changelog

## 0.10.82 — 2026-09-03

### Fixed

- Worker launch preflight checks interpreter, module, working-directory and
  repository-input capabilities before provider work starts.
- Metadata-broker denials use a private non-inheritable structural channel;
  measured hardlink/deleted-fd environment restrictions are separated from
  normal policy denials without widening Landlock or seccomp.
- Task creation returns complete validation-role and priority diagnostics, and
  NeedFix conversion preserves its quality contract.

## 0.10.81 — 2026-09-03

### Fixed

- The dashboard now separates the live queue from canonical decisions and task
  outcomes. Accepted, rejected, archived, superseded and finished counts are
  shown independently, alongside bounded acceptance, review-readiness,
  validation-failure, callback-delivery and skill-lifecycle measurements.
- Archived NeedFix entries can reopen atomically when their linked task never
  reached an accepted outcome, so unfinished work is not hidden by stale links.
- Python validator declarations now resolve through one auditable interpreter
  authority. The receipt matches the executed argv, module validators retain
  the safe `-P -m` form, and cross-platform regressions cover Linux, macOS and
  Windows.
- App-server mux shutdown uses the platform facade and cooperatively wakes a
  blocked stdin reader instead of leaving shutdown work stranded.
- Retention keeps adjudicated outcome truth, and routing scores providers from
  measured accepted outcomes.

## 0.10.80 — 2026-09-02

### Fixed

- The cost panel reports the repository it is bound to. The cost ledger tool was
  the only caller that did not pass a repository root, so it fell back to parsing
  a text report whose lines carry no model, no provider and no date -- and every
  breakdown read "unknown". Measured on the same data at the same moment: 590
  rows, 1 model, 1 provider, 1 day and no routes, against 3,509 rows, 28 models,
  8 providers, 36 days and 13 cost-per-accepted-outcome routes. The attribution
  already existed; only the tool that needs it could not see it.
- A rejection that sends work back for rework can now be learned from. Until now
  only a rejection that closed a card could be committed as a lesson, so the
  feedback the loop gives a worker -- the common case -- was never learned from.
- Process status says why it did not read a task card. A card that was never
  read no longer looks identical to a card that does not exist, or to a read
  that failed.
- Workspace cleanup keeps the workspace that finalization retry exists to use,
  for exactly as long as a retry can still act on it: a blocked or pending
  card's workspace is kept, a finished or archived one is collected.

### Added

- Skills are durable. The registry persists proposed and active skills and the
  manager can propose one, attach evidence and activate it. Activation needs
  independent accepted evidence from distinct identities, so no single actor can
  certify its own skill.

## 0.10.78 — 2026-09-02

### Fixed

- The task loop closes by itself. A worker that went quiet could never be
  finalized: the launcher appends an advisory notice after ten minutes without
  output, that row carries a pid but no start ticks, and both finalizers read
  identity from the last row -- so identity became UNKNOWN, and UNKNOWN defers.
  The longer a worker worked quietly, the more permanent its deferral.
  Cancellation had the same defect, where the tail row's pid was about to
  receive SIGTERM.
- The reconciler reports whether it is running. Its health lived in one
  process's memory behind a silent exception handler, so a reconciler that
  never started and one working normally were indistinguishable from every
  surface a manager can reach.
- Provider spend is recorded for work that SUCCEEDED. Usage recording required
  the card to still be processing while the finalizer moves a successful worker
  to review first, so every success was measured and discarded and the ledger
  accumulated failures only.
- The review orchestrator retires actions whose target card is already decided
  instead of failing them; a failed action parks every later action in its
  chain.

### Added

- Repository invariants that had only ever been prose now execute as a quality
  gate, and card templates derive the gates a package change trips instead of
  asking an author to recall them.
- A durable skill registry store: immutable (identity, version), a content
  digest that can never be rebound, and a state digest over the runtime fields
  the content digest deliberately excludes.
- Accept and reject name the lesson the decision owes, with the arguments the
  commit tool requires.

### Performance

- Retained-workspace GC no longer replays a 558 MB ledger once per candidate to
  read a single row: 38.2 minutes to 13.5 seconds, and finalization runs on its
  own cadence so a finished card does not wait on garbage collection.
- The 90-day git walk left the Source Graph index write transaction, where it
  had held the writer lock for 93.8 percent of a build.

## 0.10.77 — 2026-09-01

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

## 0.10.76 — 2026-09-01

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

## 0.10.75 — 2026-09-01

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

## 0.10.74 — 2026-08-31

### Added

- Reviewers can read authenticated, evidence-bound input packets through a
  dedicated packet-read authority.
- Windows read-only authorization uses ACL snapshots with bounded ACE and SID
  parsing.

### Documentation

- The generated Source Graph mode catalog is freshness-checked and documents the
  canonical query-mode surface.

### Notes

- Remaining Source Graph work, Windows platform work, platform consolidation and
  mechanical standards work are not represented as complete in this release.

## 0.10.73 — 2026-08-31

### Fixed

- Baseline diagnostics are compared consistently across validation paths, keeping
  unchanged accepted findings distinct from newly introduced regressions.
- Bugfix templates require every declared output before completion, so a partial
  fix cannot satisfy the template contract.

### Notes

- Remaining Source Graph retrieval/evaluation work and Windows ACL/AppContainer
  integration are not represented as complete in this release.

## 0.10.72 — 2026-08-31

### Added

- Authenticated Source Graph accepted-outcome receipts are persisted, with
  release-qualification coverage enforcing the same receipt contract.

### Fixed

- Dead processing claims reconcile within their bound task scope through an
  explicitly authorized reconciliation path.
- Windows child authority is parent-relative, directory authority remains native
  to its handle, and child disposition is bound to that authorized handle.
- Native Windows disposition failures retain platform error parity.
- Process-launcher acceptance helpers were extracted and the launcher size
  ratchet was restored.

### Notes

- Remaining Source Graph retrieval/evaluation work and Windows ACL/AppContainer
  integration are not represented as complete in this release.

## 0.10.71 — 2026-08-30

### Added

- Unchanged residual task scopes are accepted when an update advances lifecycle
  state without changing the remaining work.
- Authenticated evidence supplies expected file bytes, while signed, bounded
  outer continuation cursors provide exact counts and deterministic reassembly.

### Fixed

- Source Graph recovery removes a locked prior-build probe without destroying
  the readable generation it was probing.
- Tampered pagination cursors are rejected before continuation.

### Documentation

- Public Source Graph material now states the correct total of 48 query modes.

### Tests

- CI mirrors production pagination checks for exact counts, complete multi-page
  reassembly and cursor-tamper authentication.

## 0.10.70 — 2026-08-30

### Fixed

- Reviewer inputs are materialized as immutable evidence-bound packets before a
  reviewer starts.
- Existing valid `.aiworkhub/project.json` files retain canonical repository
  authority, repairing false uninitialized detection on Windows, Linux and
  remote workspace hosts.
- Degraded or unavailable storage is shown as an operational failure, distinct
  from a canonical schema that needs upgrading; the latter exposes an explicit
  project-schema upgrade action.

### Tests

- Manifest-discovery fixtures exhaustively match production behavior across
  valid, missing, malformed and host-specific cases.

## 0.10.69 — 2026-08-30

### Added

- Supervisor-owned context receipts now carry authenticated provider envelopes,
  and the worker completion path enforces its live Source Graph gate.
- Mechanical workspace and input preflight rejects invalid launch inputs before
  execution, while mypy runs through the repository's canonical trusted Python
  interpreter.

### Fixed

- Diagnostic validation compares results with the accepted baseline to identify
  regressions without relabeling existing diagnostics.
- Normalized review findings enter the lifecycle idempotently, and acceptance
  closes exactly linked NeedFix findings without repeated side effects.
- Reserved reviewer launch failures reconcile to a terminal result, and
  process-launcher validation lives in a focused module protected by a size
  ratchet.

### Tests

- Regression coverage binds receipt authentication and Source Graph enforcement
  to baseline comparison, preflight, interpreter trust, finding replay, linked
  closure, launcher extraction and reviewer launch-failure reconciliation.

## 0.10.68 — 2026-08-29

### Added

- A durable canonical review outbox now drives correctness, security and code
  quality reviews in sequence, then accepts and archives the target and closes
  its exactly linked NeedFix records.
- Reviewer routes come from the live workforce policy and pending lifecycle
  actions survive process restarts with authenticated receipts and leases.

### Fixed

- The supervisor now owns mandatory reviewer-report ingestion and structured
  evidence canonicalization, so successful review no longer depends on a model
  remembering to call the submission tool.
- Review waits release their lease immediately; terminal receipts are bound to
  the exact target, lens, packet, provider and single submission; actionable
  findings stop acceptance and completed reviewer cards are archived.
- Zero-history DeepSeek and GLM routes remain launch-eligible for a canary but
  unavailable and unselectable until authenticated success is observed.

### Tests

- Windows provider and LM-discovery parity coverage follows the canonical
  observed-readiness contract, and lifecycle tests cover restart replay,
  archive order and linked-NeedFix cleanup.

## 0.10.67 — 2026-08-29

### Added

- Adaptive worker capacity uses the effective CPU and affinity budget, reserves
  room on multi-CPU hosts, honors caps, reports the applied ceiling, and keeps
  nested pools bounded.
- Decomposition previews now return approval-required proposals with stable
  boundary identifiers and the large, low-confidence action class.
- A provider-response contract foundation supplies immutable normalized events,
  preserved unknown types, strict JSON data, canonical bytes, digests and
  stable error categories.

### Fixed

- Nested Landlock validation trusts outer authority only through an
  authenticated request-owned worktree locator; hardlink no-ops stay bound to
  authenticated descriptors instead of ambient paths.
- Windows AppContainer supervision owns the native process and job lifecycle,
  preserves terminal results, terminates process trees, and closes handles and
  pipes idempotently.
- Callback-store readiness and WAL-setup failures expose bounded categories
  rather than SQLite, path or payload details; cleanup preserves the primary
  database-open error.
- Archive and supersede actions now record the verified manager provider actor
  while preserving manager write-gate enforcement.

### Changed

- Launcher read-efficiency parsing and reporting moved into a focused module,
  backed by parity tests and a descending module-size ratchet.

### Tests

- Python 3.14 lock-timeout tests isolate the intended descriptor attempts, and
  Source Graph capacity tests assert the effective worker ceiling.

## 0.10.66 — 2026-08-29

### Added

- Bootstrap responsibility matrices and construction templates make the
  manager/worker handoff explicit, while pending context intents preserve
  unresolved Session, Memory and KB work for a later bounded resolution.
- Scoped audit coverage and fail-closed Source Graph traversal keep discovery
  within verified authority instead of continuing from an untrusted path.

### Fixed

- Reviewer preparation now observes strict bounds and carries replay-context
  evidence; CAS, raw-terminal, launch-ledger and retained-terminal recovery
  reconcile interrupted work deterministically.
- Repository configuration repair revives the retained VS Code dashboard, and
  model settings reflect discovered capabilities through the trusted
  interpreter. CaaS corrections remain bound to verified evidence.

## 0.10.65 — 2026-08-28

### Added

- A fail-closed Windows AppContainer lifecycle foundation covers deterministic
  identity, shell-free launch, job ownership and cleanup evidence. It remains
  gated and is not yet wired into native worker launch.

### Fixed

- Quality-review packets distinguish narrative evidence from explicit
  read-only files and close pre-provider preparation failures cleanly.
- Reviewer scope is built before immutable prose is added, preserving
  changed-path evidence ordering.
- Immutable prose is sealed into the reviewer contract and bound by the
  refreshed canonical packet digest before persistence.
- Legacy Source Graph database migration is read-only at the source boundary.
- Source Graph refresh documentation matches the serialized runtime invariant.
- The Models settings modal preserves its shell, pending identity and prior
  rendered state while repository settings load.
- Authenticated task-template validation and role overrides are sealed into the
  template contract, preserving their exact authority through validation.
- The native Repository Settings dialog no longer opens as a blank frame:
  content-addressed webview assets and an intrinsic layout keep the wrapped
  footer visible while only the settings list scrolls.

### Known limitations

- `AIWORKHUB_01065_NF453_GLM_VSCODE_LM_TOOL_LOOP_V5` remains unresolved.
  Its candidate was not promoted, and 0.10.65 does not claim the GLM VS Code LM
  tool loop is fixed.

## 0.10.64 — 2026-08-27

### Fixed

- Model settings remain visible and dimensionally stable while toggles update.
- Quality reviewers receive declared immutable runtime dependencies in their
  sparse workspaces, eliminating missing-input false positives.

### Changed

- Development waves now cut and install an intermediate release after several
  blocker fixes land while the owner is actively present.

## 0.10.63 — 2026-08-27

### Added

- Native Codex model capability observation and audited task launch-identity
  rerouting keep recovery on canonical provider routes.
- Failure-learning telemetry and complete worker outcome categories make task
  failures and active states diagnosable without collapsing distinct causes.

### Fixed

- The model selector remains responsive and dimensionally stable while model
  and provider toggles change.
- Native Codex reviewers can run through the canonical quality-review topic.
- Context capture reports disabled and skipped states truthfully.
- Owner-manifest reads reject post-read path replacement races.
- Deterministic validation replay and Source Graph mode documentation stay
  aligned with their canonical contracts.

### Known limitations

- NF-2026-00448 and its dependent Windows AppContainer runtime are explicitly
  excluded and remain open; this release makes no completed AppContainer claim.

## 0.10.62 — 2026-08-27

### Fixed

- Task finalization reports every required-output mismatch in one structured
  result instead of sending rework through a one-error-at-a-time loop.
- Sparse JavaScript validation retains bounded local CommonJS JavaScript and
  JSON dependencies, and Python validation resolves to the trusted runtime.
- Mixed exec-scratch failures no longer masquerade as a metadata-only sandbox
  restriction.
- The manager has a measured, tightly scoped self-hosting recovery path for
  replacing a Task MCP/plugin build that blocks canonical development.

## 0.10.61 — 2026-08-26

### Fixed

- The Development Rules dashboard card now reads the validated repository
  manifest and reports its measured version, digest and rule counts instead
  of a false `No sample` state.

## 0.10.60 — 2026-08-26

### Added

- Repository development rules expose explicit performance, multicore,
  allocation, caching and validation constraints to coding workers.

### Fixed

- Claude subscription workers refresh a stale request-local credential once
  on an exact authenticated 401 and retry the identical request without
  losing cancellation or process-liveness authority.
- Sparse C/C++/CUDA workspaces resolve quoted headers through declared include
  roots with traversal and symlink-escape protection.
- Hosted rework, nested validation dependencies and supervisor progress
  accounting use current deterministic authority.

## 0.10.59 — 2026-08-26

### Fixed

- Reviewer launch now preserves the exact reserved request through isolated
  provider spawn, validates the durable read-only card and avoids a second
  claim.
- Sparse self-development candidates locate the existing host supervisor
  script deterministically, so reviewer launches no longer fail before start.

## 0.10.58 — 2026-08-25

### Fixed

- Reviewer launches now create, claim and bind the exact task before returning
  an acknowledgement, eliminating pending/unclaimed ghost reviewers and making
  same-ID retry reconciliation deterministic.

## 0.10.57 — 2026-08-25

### Fixed

- Request-owned provider temp directories are provisioned before Landlock is
  composed, preventing Claude/Grok launch failures while keeping the sandbox
  restricted to the exact worker request.

## 0.10.56 — 2026-08-25

### Fixed

- Worker and validator temporary files are request-owned and cleaned through a
  verified nested sandbox boundary instead of leaking into host temp storage.
- Source Graph daemon health and recovery distinguish stale query connections
  from live lock holders and report consistent process-lifecycle truth.

## 0.10.55 — 2026-08-25

### Fixed

- Task-template validation is language aware, preventing data fixtures from
  being sent to Python-only tools.
- Worker Source Graph metrics distinguish live calls from injected prefetch
  evidence while preserving fail-closed receipt authentication.
- Sealed provider quota and authentication failures trip only the affected
  route circuit, avoiding repeated selection of a known-unavailable model.

## 0.10.54 — 2026-08-25

### Fixed

- Worker execution is uncapped by default across supervision and terminal
  finalization; legacy token-budget records remain diagnostic only.
- Reviewer terminal intents settle durably across stale reservations, ledger
  churn and callback recovery instead of accumulating stranded review work.
- Zero-diff validation rework is mechanically reconstructed from retained
  workspace evidence, and sparse validators use current canonical Python
  dependencies instead of stale Git-HEAD modules.

## 0.10.53 — 2026-08-24

### Added

- Coding-foundation status cards expose repository rules, skills and typed
  tool-recipe evidence in the dashboard.

### Fixed

- Source Graph body search no longer leaves repeated scan-sized heap growth in
  the long-lived MCP process.
- Source Graph caches evict obsolete generations and daemon shutdown reaps the
  exact spawned process tree.

## 0.10.52 — 2026-08-24

### Fixed

- Skill packet secret filtering rejects all Unicode format characters before
  any instruction can reach a worker prompt.
