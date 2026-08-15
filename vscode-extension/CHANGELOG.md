# AIWorkHub for VS Code — Changelog

## 0.9.72 — 2026-08-15

### Fixed

- Storage retention actually reclaims now. Worktree eligibility returned zero
  candidates while the repository sat far over its cap, because every attempt
  workspace stayed pinned and nothing released a superseded one.
- Live-worktree protection is keyed on the field the claim path writes
  (`launch_request_id`), not on one that only exists after a review is
  accepted — previously every processing/review card's live worktree was
  unprotected.
- Protection is no longer limited to the most recent rows, so a live card can
  no longer lose protection just because the task table grew.
- Retention age is injected rather than read from file mtimes, and the planner
  fails closed when task lineage cannot be read.

## 0.9.71 — 2026-08-15

### Fixed

- A card owner can now release its own claim. The card-scoped write-action set
  omitted `launch-failed`, so releasing a stale reservation claim was always
  refused and any reconciled reservation left its card stranded in
  `processing`/`claimed` forever.
- The action set is now one named frozenset shared by both authority call
  sites, so they cannot drift apart. Codex authority is unchanged.

## 0.9.70 — 2026-08-15

### Fixed

- Coordinator routing now resolves from the active verified manager route. A
  Claude-routed repository no longer shows `automatic: codex`,
  `route_pending` and `codex_thread_id_not_observed` in the dashboard while
  manager identity reports `claude`, and no longer raises a Manager
  coordination Route warning that no action can clear.
- Codex routing, thread observation and reason strings are unchanged when the
  active verified route is Codex; a repository with no verified route fails
  closed instead of defaulting to either provider.

## 0.9.69 — 2026-08-15

### Added

- Deliver worker callbacks to a verified Claude manager without manual polling,
  so `review_ready` and `worker_failed` transitions reach the manager's active
  session instead of waiting in the inbox until it happens to poll.
- Keep the lease and ack contract unchanged: one verified manager route holds a
  batch, ack stays mandatory, an unacked batch stays redeliverable, and a route
  whose provider, repository or session identity does not match never receives
  it.

### Fixed

- Report a truthful provider-specific state from `dispatcher_health` on a Claude
  route instead of an unregistered-dispatcher problem where no dispatcher is
  expected.

### Changed

- Run explicit platform-owned regression manifests on the Windows and macOS CI
  jobs, each preflighted with a collection guard that fails when a manifest
  entry collects nothing, so CI can no longer pass silently on zero collected
  platform tests. Linux coverage and the existing install/VSIX checks are
  unchanged.

## 0.9.68 — 2026-08-15

### Fixed

- Route each VS Code LM worker request to the exact fresh editor window chosen
  by preflight instead of allowing another or stale window to claim it.
- Record claimant window, extension-host PID, extension version and bridge
  capabilities in progress and terminal receipts for direct diagnosis.
- Keep legacy untargeted requests compatible during rolling upgrades.

## 0.9.67 — 2026-08-15

### Fixed

- Keep bounded semantic staging offline and advertise only the offline stage
  tool once the VS Code LM worker enters that phase.
- Recover one role-correct Source Graph phase violation without invoking it,
  then fail repeated violations with a bounded structured error.
- Prevent the `mcp_unavailable` followed by `tool_not_allowed` loop that could
  terminate otherwise healthy self-development workers before edits.

## 0.9.66 — 2026-08-15

### Fixed

- Prepare reviewer Source Graph overlays by cloning the verified canonical
  SQLite generation and indexing only packet-changed files, with no full
  repository rebuild in reviewer prewarm.
- Verify canonical authority, repository binding, schema and generation before
  reviewer runtime or provider registration begins.
- Keep concurrent reviewer overlays isolated and coordinator Source Graph
  reads serviceable while candidate databases are published atomically.

## 0.9.65 — 2026-08-15

### Fixed

- Preserve exact VS Code LM tool-call/result history across bounded reviewer
  stage and final corrections.
- Reject cross-role tool calls before callbacks or invocation and recover the
  reviewer submit phase without widening manager/worker authority.
- Use content-only review overlay copies for portable Linux, macOS and Windows
  isolated workspaces.
- Keep reopened NeedFix generations and malformed provider usage evidence
  fail-closed without killing otherwise healthy workers.

## 0.9.64 — 2026-08-15

### Fixed

- Run independent reviewer Source Graph prewarms concurrently while retaining
  exact per-candidate single-flight and atomic verified publication.
- Prevent elapsed reservation expiry from terminating a live exact-owned
  prewarm; unknown, dead and recycled PID identities remain fail-closed.
- Keep reviewer runtime queries read-only and free of lazy index builds.

## 0.9.63 — 2026-08-14

### Fixed

- Unify reviewer prompts, callable MCP JSON schema, validation and durable
  receipts around one canonical finding object.
- Require `severity`, `summary` and `evidence`, reject undocumented aliases
  and return exact missing-key errors instead of a guessing loop.
- Keep clean and non-empty reviewer reports on the same exactly-once
  finalization path.

## 0.9.62 — 2026-08-14

### Fixed

- Bind reviewer-spawn ownership to an exact PID/start-ticks compare-and-swap so
  a recycled process identity is never accepted as the live reviewer launch.
- Recover lost-ack and extension-host reload handoff idempotently without
  leaking a reservation or double-launching a reviewer.
- Terminalize live providers only on process/terminal evidence, never on
  elapsed or quiet time.

## 0.9.61 — 2026-08-14

### Fixed

- Surface live reviewer preparation phases without blocking independent status
  reads while an expensive packet is built.
- Bound only the pid-null pre-provider preparation stage and preserve the
  no-elapsed-timeout contract once a model process exists.
- Terminalize all single-flight waiters exactly once when packet preparation
  cannot complete.

## 0.9.60 — 2026-08-14

### Fixed

- Coalesce concurrent correctness, security and code-quality reviewer packet
  preparation per exact target while keeping three distinct review launches.
- Keep reviewer launch handlers responsive and propagate the single
  preparation owner's truthful result to every waiting lens.
- Harden the Linux validation metadata-broker listener handoff with bounded,
  fail-closed diagnostics for hosted CI and local secure sandboxes.

## 0.9.59 — 2026-08-14

### Fixed

- Use the bounded parallel stdio backend for dashboard, stable Codex and
  repaired Claude/Copilot MCP registrations so one long request cannot block
  independent status/read calls.
- Reject malformed empty-string parameters before tool dispatch with durable
  redacted telemetry, avoiding the persistent bare `-32602` connection state.
- Recover the exact owned dashboard MCP child after the live
  `Invalid request parameters("")` poison signature repeats.

## 0.9.58 — 2026-08-14

### Fixed

- Fail closed before dispatch when an MCP request carries empty-string or
  non-object parameters, with a bounded redacted repository-local alert.
- Preserve valid `{}` requests and keep parallel healthy clients serviceable
  when one malformed request is rejected.
- Use the repository-owned sideband endpoint through a short relative socket
  name so long retained workspace paths do not exceed AF_UNIX limits.

## 0.9.57 — 2026-08-14

### Fixed

- Publish fresh Source Graph generations with truthful refresh health and
  generation metadata.
- Reindex by content hash instead of trusting size/mtime hints, while using
  bounded host-adaptive parallel hashing for unchanged files.
- Keep scoped analytics, pagination and aggregate counts inside the requested
  path boundary and resolve exact local JavaScript/TypeScript import bindings.
- Use an O(1) membership hot path for bounded git metrics without changing
  persisted or returned ordering.

## 0.9.56 — 2026-08-14

### Fixed

- Prevent an empty relative Python import module from aborting Source Graph
  refresh with `PosixPath('/') has an empty name` before publication.

## 0.9.55 — 2026-08-14

### Fixed

- Keep bootstrap, status and dashboard calls responsive while long-running
  provider, reviewer or finalization tools execute on the shared MCP runtime.
- Correlate concurrent fallback stdio responses by exact JSON-RPC request ID
  with bounded host-adaptive dispatch and serialized output writes.

## 0.9.54 — 2026-08-14

### Fixed

- Treat a fresh authenticated Source Graph zero-hit result as a real worker
  invocation without weakening cache, identity or authority gates.
- Keep concurrent Codex app-server sideband requests exactly correlated and
  prevent late sideband replies from poisoning the extension client.

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
