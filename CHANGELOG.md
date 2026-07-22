# Changelog

All notable changes to AIWorkHub are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project has
noted by package/extension version and release tag.

## [0.6.22] - 2026-07-22

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
  (`vscode-extension/package.json`) versions aligned at `0.6.22`.
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
