# AIWorkHub for VS Code — Changelog

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
