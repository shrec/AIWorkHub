# AIWorkHub for VS Code — Changelog

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
