# AIWorkHub for VS Code — Changelog

## 0.9.53 — 2026-08-14

### Added

- Bind Python imported-function calls only from exact, unambiguous evidence.
- Expand nested Rust `use` trees without overstating lexical parser authority.

## 0.9.52 — 2026-08-13

### Fixed

- Keep a slow full dashboard refresh in a truthful **Snapshot delayed** state
  without marking the healthy MCP runtime Offline.
- Use a dedicated full-snapshot aggregation budget and avoid immediate retry
  storms while retaining the last successfully rendered dashboard data.

## 0.9.51 — 2026-08-13

### Fixed

- Show one canonical Source Graph generation identity across startup and health
  instead of contradictory process-local metadata.
- Track refresh requests with durable job IDs and explicit terminal outcomes,
  so queued work cannot remain silently stale.

## 0.9.50 — 2026-08-13

### Fixed

- Preserve exact JSON-RPC request IDs and non-empty parameters by serializing
  concurrent MCP request/notification frames through one child-owned FIFO.
- Keep out-of-order response correlation independent and fence late callbacks
  when the repository-scoped MCP child is replaced.
- Repair a repeatedly poisoned bare `-32602 Invalid request parameters`
  transport by replacing only the exact child without reloading the window or
  changing repository/manager identity.

## 0.9.49 — 2026-08-13

### Fixed

- Keep chmod/fchmod metadata restoration inside the exact request-owned
  validation boundary while accepting ordinary permission bits from full
  `stat().st_mode` values.
- Use process-visible CPU capacity for bounded Source Graph extraction,
  prevent nested oversubscription and retain deterministic single-writer
  database merges with explicit selection/fallback telemetry.

## 0.9.48 — 2026-08-13

### Fixed

- Move request-scoped worker and validation temporary data under the current
  repository's `.aiworkhub/temp` authority.
- Keep concurrent repositories isolated and make retention distinguish pinned
  review candidates from disposable or exact dead-owner artifacts.
- Preserve fail-closed validation setup while making temporary HOME, caches
  and executable scratch deterministic.

## 0.9.47 — 2026-08-13

### Fixed

- Retain authenticated sealed read-only reviewer receipts after standalone
  acceptance removes the reviewer workspace, and reuse them only when the
  immutable process event and both task-card receipt copies agree exactly.
- Bind receipts to the exact target, reviewer, provider, claim epoch and
  deterministic lowercase 64-hex submission identity, and fail closed on
  malformed, unverified, duplicate or identity-mismatched receipts while
  preserving the writable changed-path hash fallback.

### Validation

- Added regression fixtures for sealed receipt retention, exact binding and
  fail-closed schema enforcement (NF131).

## 0.9.46 — 2026-08-12

### Fixed

- Keep malformed legacy task JSON from aborting repository initialization.
- Show global collision health separately from each task's exact launch
  eligibility.
- Preserve provider-valid GLM reviewer history and normalize substantive
  correctness findings into the required durable review submission.
- Remove complete Bearer credentials from portable dashboard evidence.

## 0.9.45 — 2026-08-12

### Fixed

- Keep persisted superseded tasks in a separate exact dashboard count instead
  of degrading the snapshot with `KeyError('superseded')` (NF159, NF170,
  PR #23).

## 0.9.44 — 2026-08-12

### Fixed

- Verify the exact Claude Code manager process and session descriptor through
  Windows-native process identity instead of relying on `/proc` (#17).
- Defer duplicate finalizers on recognized Windows request-lock contention
  without terminalizing a healthy owner (#18, PR #19).
- Isolate deterministic mypy, temporary and Ruff cache state per validation
  request and retain bounded diagnostics for internal validator failures
  (NF180, PR #21).

## 0.9.43 — 2026-08-11

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

## 0.9.42 — 2026-08-10

### Fixed

- Give the staged-edit bridge a fresh extension/runtime generation and expose
  exact loaded-version plus tool-transport receipts, preventing a stale
  in-memory Extension Host from being accepted as live proof of the newly
  installed offline staging path.

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
