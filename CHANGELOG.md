# Changelog

All notable changes to AIWorkHub are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project has
noted by package/extension version and release tag.

## [Unreleased]

### Added

- Added the repository-isolated Workspace Build Hygiene foundation ported from
  the proven donor design: external scratch slots, quota reservations,
  cryptographic lease release, real byte accounting, digest-bound explicit
  cleanup, rogue in-repository build-tree reporting, CLI access and bounded
  environment-preflight observability.

### Fixed

- Source Graph build reports now count the unique edge rows actually persisted
  after writer deduplication instead of the larger pre-dedup extractor
  population. Dashboard/index statistics therefore reconcile with SQLite.

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
