# AIWorkHub for VS Code — Changelog

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
