# AIWorkHub for VS Code — Changelog

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
