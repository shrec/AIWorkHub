# AIWorkHub for VS Code — Changelog

## 0.9.41 — 2026-08-10

### Added

- Stage bounded semantic-edit replacements and creates through small tool
  calls, then assemble the final worker envelope offline from hash-bound
  receipts after a summary-only finalize call.
- Added bounded event-stream primitives with gap, overflow, reconnect and
  authoritative-resync behavior for the next dashboard transport stage.

### Fixed

- Validation-only replay skips editor-bridge cancellation only when durable
  request evidence proves that no provider was launched.
- Required-create range edits are revalidated as complete create content, and
  authenticated read-only finalization follows the normal cancel contract.

### Validation

- Text-only and native provider paths share staged-edit parity tests; rejected
  overlapping ranges cannot mutate the accumulated final envelope.

## 0.9.40 — 2026-08-10

### Fixed

- Reject literal ellipsis placeholders in required semantic-edit creates and
  retained rework.
- Resolve Ruff and mypy through a trusted PATH entrypoint when the active MCP
  interpreter cannot import the declared module, without losing executed-command
  provenance.
- Verify supersession replacement tasks before archiving the original card.
- Keep quality-verdict lens aggregation strictly typed.

### Validation

- Added focused placeholder-fidelity, quality-tool-resolution and replacement
  edge regressions; complete Python and extension qualification remains the
  release gate.

## 0.9.39 — 2026-08-09

### Added

- Strict evidence-level contracts, scoped audit packets and manager-gated
  learning commits.
- A deterministic, bounded attempt-artifact manifest with exact hash/size
  coverage, duplicate-key rejection and fail-closed metadata/path validation.

### Fixed

- Bound Windows MCP runtime routing to the active workspace in multi-window
  sessions.
- Stopped treating POSIX mode bits as Windows ACL evidence during validation.
- Run Ruff and mypy through the selected trusted Python runtime.
- Report canonical blocked finalization failures exactly once in the
  completion inbox, enriched by exact request-scoped process evidence.
- Kept Windows runtime regression tests portable on Linux.

### Validation

- The full Python suite passes 3,125 tests with 28 platform-dependent skips.
- The 12-test release qualification matrix, extension suite, Ruff, mypy and
  diff checks pass.

For full project history, see the
[repository changelog](https://github.com/shrec/AIWorkHub/blob/main/CHANGELOG.md).
