# Changelog

All notable changes to AIWorkHub are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project has
noted by package/extension version and release tag.

## [Unreleased]

## [0.12.37] - 2026-10-08

### Added

- vscode_lm: the task live output previews the worker's text and reasoning while it runs (NF-2026-01380).
- OpenCode: the manager seat declares and runs a readable-reasoning model variant, so its thinking streams into Manager Chat (NF-2026-01384).
- Promotion three-way merges a candidate whose canonical parent changed since launch instead of refusing it (NF-2026-01381 part A).

### Fixed

- Manager Chat: the model picked in Manager Chat becomes the manager seat identity, and accept records that seat first (NF-2026-01383).
- Task plan: a launched card no longer reserves its write scope; only an in-place canonical writer blocks an overlapping launch (NF-2026-01381 part B).
- vscode_lm worker bridge: a forced review offers only submit, and staged ranges after the first refund a free-phase turn (NF-2026-01388, NF-2026-01385).
- vscode_lm worker bridge: a bottom-up range ending above the first line-count change keeps its coordinates; finalize never drops a stage rejected for fresh-pair recovery, and an outstanding recovery counts toward the bounded finalize escape (NF-2026-01404).
- Skills: an ordinary card derives its skill selection context, so skills are selected and earn usage evidence (NF-2026-01397).
- Task FSM: a declared but unrun validation is never a measured pass (NF-2026-01392).
- Worker workspace: a validator's `__pycache__/*.pyc` is shell residue, never a scope violation (NF-2026-01390); an untracked nested repository or worktree is never canonical delta (NF-2026-01391).
- Promotion merge: `git merge-file` runs outside the caller repository, and a git die exit is never a conflict count (NF-2026-01401).
- `retry_finalization` records the verified calling manager, never Codex (NF-2026-01394).
- Context-ack evidence reports the coordinator-bound acknowledgement only for an absent receipt; a found but unverifiable receipt keeps its specific reason (NF-2026-01395).
- A launch that names runner, topic and adapter but no model pins the runner's canonical model, so a `claude_cli` worker no longer runs on the CLI account default (NF-2026-01407).

### Qualification

- Each fix has a regression that fails before and passes after; the touched suites and the full suite pass. Installation and live replay are separate evidence.

## [0.12.36] - 2026-10-06

### Fixed

- vscode_lm worker bridge: one staged edit per required output is readiness, not completion. The bridge no longer finalizes offline the moment every required output has a staged edit; it finalizes on the worker's own `aiworkhub_manager_semantic_edit_finalize`, under forced staging, or after a turn with no Source Graph/stage progress, so a worker can land a second edit in an already-staged output. A Source Graph no-progress stop with every required output staged now finalizes that work instead of failing the run (NF-2026-01378).
- Manager Chat: a Claude turn whose CLI rejects `--thinking-display` retries once without the flag and remembers the rejection for the process, instead of failing the turn with `unknown option` (NF-2026-01353).
- OpenCode: a statusless `provider.no-route` error is sealed as a typed route failure instead of an unclassified terminal (NF-2026-01374).

### Qualification

- Each fix has a regression that fails before and passes after; the touched suites and the full suite pass. Installation and live replay are separate evidence.

## [0.12.35] - 2026-10-06

### Fixed

- vscode_lm worker bridge: a `aiworkhub_worker_semantic_edit_apply` call that omits `idempotency_key` gets one derived from the exact (`target_id`, `new`) pair, so the fresh prepare/apply pair that follows a coordinate-shifting stage no longer fails `semantic_edit_apply_input_invalid` for every emulated-transport model; a model-supplied key is unchanged and the native MCP contract stays strict (NF-2026-01373).
- .NET worker seed: with `CentralPackageTransitivePinningEnabled=true`, the synthetic restore also seeds the `Directory.Packages.props` `PackageVersion` rows that pin transitive packages, through the same identity/version allowlist gates, when no direct or global reference already declares the identity (NF-2026-01366).

### Qualification

- Each fix has a regression that fails before and passes after; the touched suites and the full suite pass. Installation and live replay are separate evidence.

## [0.12.34] - 2026-10-06

### Fixed

- Manager Chat seat: the owner's explicit pick (continue, restore, a picked model, binding a route for a turn) stamps `ManagerSession.updated_at` and renews the manager seat lease, so a conversation idle past `MANAGER_CHAT_SEAT_LEASE` becomes the manager again; opening the panel never does. `aiworkhub_manager_loop_continue` accepts `backend_id`/`model`, authorized like a send's route and pinned without starting the backend, and the webview sends the picked model with it. The dashboard target and the repository/coordinator lines show `manager_chat <backend>/<model>` instead of the window route (NF-2026-01376).
- OpenCode workers: the generated config uses the native `mcp.servers.awh` shape with `codemode: false`, so OpenCode 2.0.16 no longer migrates it into Code Mode and hides the direct `awh_*` tools; workers launch `opencode run --standalone` instead of sharing the managed-service port 55552.
- A refused `aiworkhub_worker_semantic_edit_apply` names each invalid field (`invalid_fields`) and the exact `next_call` shape instead of one undifferentiated `semantic_edit_apply_input_invalid` (NF-2026-01373).
- vscode_lm bridge: the create obligation follows the published `required_create_paths` (create paths ∩ required outputs); a chosen optional create stays fidelity-checked, and absent metadata keeps every authorized create mandatory (NF-2026-01330).

### Qualification

- Each fix has regressions for its new contract; the touched suites and the full suite pass. Installation and live replay are separate evidence.

## [0.12.33] - 2026-10-06

### Fixed

- Worker Source Graph rework overlay: an inherited path whose extraction status is `unsupported_fail_closed` (for example `CMakeLists.txt`) is digest-bound `file_evidence_only` evidence instead of failing every worker Source Graph call; an unknown status still fails closed (NF-2026-01369).
- A provider-relaunched rework whose changed paths and sha256 equal the predecessor's sealed `changed_path_hashes` terminates `worker_failed` with `rework_no_delta`, classified as candidate code, instead of reaching `review_ready`; `validation_only_replay` is unaffected. Supervisor-injected orientation no longer satisfies the worker Source Graph gate when every live Source Graph call the worker made failed (`source_graph_live_calls_all_failed`); a malformed or non-dict failure record fails closed (NF-2026-01370).
- The Release workflow's exact-commit CI provenance check waits for the push CI run at the release SHA (rescans every 30 s, up to 3000 s) instead of checking once; main and the tag are pushed together, so the single check always ran while CI was still in progress and rejected every release. A completed non-success run fails at once, and the deadline fails closed (NF-2026-01372).

### Qualification

- Each fix has regressions for its new contract; the touched suites and the full suite pass. Installation and live replay are separate evidence.

## [0.12.32] - 2026-10-05

### Fixed

- AppContainer .NET prerestore: `.slnx` and `.slnf` targets, the canonical solution selected over a sibling, and projects added by the card restored through a literal-XML synthetic seed (`AIWorkHubRestoreSeed.csproj`); a supplementary seed behind a canonical restore never blocks on a deleted or unsupported candidate project, while a strict seed still names a project its solution lists but lacks (NF-2026-01366).
- `ProcessManager._build_adapter` selects the repo-owned executable registration from the canonical repository for every launch path; the isolated launch had passed its worktree, so the registry was never read and the registered OpenCode compat binary was ignored. A malformed or SHA-mismatched registration still fails the launch closed (NF-2026-01250).

### Qualification

- Each fix has a regression that fails before and passes after; the touched suites and the full suite pass. Native OpenCode/Muse provider and MCP qualification inside the AppContainer is separate live evidence.

## [0.12.31] - 2026-10-05

### Fixed

- New `windows_build_env`: the derived MSVC/Windows Kit build environment (vswhere, or a probe of the Program Files install tree where vswhere answers nothing inside an AppContainer), registry `PATH` appended behind the caller's own, `CMAKE_GENERATOR=NMake Makefiles` and `_CL_=/Z7` defaults in an AppContainer, each only where the caller declared none. It is applied to `worker_launch_env` for the `windows_appcontainer` backend and to every AppContainer validation command; `ninja: fatal: CreateNamedPipe: Access is denied` terminates as `validation_unsupported_in_sandbox` (NF-2026-01337).
- AppContainer validation adapts `dotnet build/test/restore/publish` (derived Program Files, per-request NuGet and artifacts paths, a host-cache restore source) (NF-2026-01366).
- `aiworkhub/__init__.py` makes the MSVC CRT default file mode binary through capability detection on `os.O_BINARY`; it covers the audited bare `os.open` sites without per-call edits (NF-2026-01365).
- `validation_route_backend_mismatch` is a control-plane terminal cause (NF-2026-01346).
- The host-git spec tests skip inside an AppContainer lane, whose own temp is container-writable by design.
- The NF-2026-01113 rework rebase reads EOL attributes from the successor base (`--attr-source`) for both its clean and smudge filters: a sparse worktree holds no `.gitattributes`, and git 2.47 then converted nothing, so an `eol=crlf` repository received LF merged bytes.
- `output_spill_store.retrieve_text` treats `NotADirectoryError` like a missing file, so a repository spill root occupied by a file falls through to the worker-writable root on POSIX (NF-2026-01162).
- `windows_appcontainer.is_python_executable` parses its argument as a Windows path on every host.
- Linux/macOS CI: fake CLIs and owner-only token/status files get their mode at `os.open` creation, Windows-only AppContainer tests skip elsewhere, the live probe imports `msvcrt` only on Windows, the NF-2026-01107 listing test allows the signal-0 liveness probe, and the dashboard snapshot test no longer reads the host checkout's context graph.

### Qualification

- Each fix has regressions that fail before and pass after; the touched suites and the full suite pass, and the live clang-cl and CMake/NMake/ctest builds pass inside the AppContainer validation lane. Installation and live replay are separate evidence.

## [0.12.30] - 2026-10-05

### Fixed

- `platform_io.durable_atomic_replace` opens the Windows fsync descriptor with `O_BINARY`; the CRT text-mode `O_RDWR` open stripped a trailing Ctrl-Z (0x1A), truncating a published SQLite partition by one byte and failing Source Graph prewarm for quality reviewers (NF-2026-01365).
- `bodygrep` scans first-party files before vendored directories (`third_party`, `third-party`, `thirdparty`, `3rdparty`, `vendor`, `vendored`) with deterministic resume, so vendored literal matches cannot starve first-party hits (NF-2026-01363).
- `supervisor_spawn_failure_cause` accepts the launcher-minted `worker_prompt_not_delivered:<detail>` shape; terminal log retention owns `<rid>.prompt`, `<rid>.relaunch-spec.json` and `<rid>.attempt1.*`; its enforcement lock is per repository (NF-2026-01360, NF-2026-01362).
- The `list_processes` summary view includes `concurrency_limit` and `workforce_cap` (NF-2026-01359).
- The AppContainer `sitecustomize` docstring no longer prints a `SyntaxWarning` in every validation run.

### Qualification

- Each fix has regressions that fail before and pass after; the touched suites and the full suite pass. Installation and live replay are separate evidence.

## [0.12.29] - 2026-10-05

### Fixed

- Reviewer and combined-validation workspaces skip live `.aiworkhub` runtime files (`*.sqlite-wal`/`-shm`, `*.db-wal`/`-shm`, `*.lock`, `*.sock`, `credentials/`, `worktrees/`, `backups/`) so a byte-range-locked file no longer fails a concurrent reviewer launch with a bare `[Errno 13]`; a filename-less launch `OSError` now records its `launch_phase` and aiworkhub raising frame (NF-2026-01358).
- The Claude auth-retry relaunch replays the persisted prompt and launch spec after verifying the prompt SHA-256, the declared stdin byte count and argv; otherwise it fails closed as `spawn_failed`/126 with `worker_prompt_not_delivered`. The first attempt's logs are kept as `<rid>.attempt1.*` (NF-2026-01354).
- `process_launcher` admits launches through `repo_policy.resolve_workforce_cap` (ceiling 32), and `manager_bootstrap` and `list_processes` report `workforce_cap` (NF-2026-01359).
- Enabling a model in Settings lifts its own disabled adapter gate and pins that adapter's other models off (NF-2026-01355); a legacy AITools `entries_fts` is rebuilt to the canonical index so manager KB upsert stops failing (NF-2026-01356).
- `manager_bootstrap` and `repo_current` report `runtime_generation` with a warning when the serving generation is stale, and the extension points the Claude Code `.mcp.json` at the stable `aiworkhub-mcp-server.py` launcher, keeping other env such as `AIWORKHUB_MAX_PROCESSES` (NF-2026-01357).

### Qualification

- Each fix has regressions that fail before and pass after; the touched suites and the full suite pass. Installation and live replay are separate evidence.

## [0.12.28] - 2026-10-05

### Fixed

- Manager Chat event log is bounded by bytes and polls read only its tail (NF-2026-01233, NF-2026-01244); a failed CLI turn is one error with the provider's own text (NF-2026-01232); a first Send binds the persisted conversation (NF-2026-00989); rotation uses the provider's measured context fill (NF-2026-01238); a streamed partial survives a mid-turn assistant message (NF-2026-01230).
- Manager Chat panel: Enter sends, Shift+Enter keeps a newline; a server-side rotation is followed (a `session_close` and a periodic poll pull status); the Thinking timer belongs to one session and turn; a refused send reverts Running and restores the text.
- `aiworkhub_manager_loop_send` queues an owner message behind a running turn (bounded, in memory, ahead of the next callback wake) and `aiworkhub_manager_loop_status` returns `send_queue`.
- Claude manager turns request summarized thinking, so reasoning reaches the panel.
- `_claude_windows_manager_identity` counts `py.exe`/`pyw.exe` as a re-exec hop (same-user SID per hop, hop limit unchanged) (NF-2026-01005); a system quality reviewer inherits its target's callback route and a reviewer-create failure releases the deferred review wake (NF-2026-01095).
- Callback outbox: seeding looks up the provider a row is stored under, a rebind supersedes duplicate wakes on one route, a verified manager bootstrap adopts other providers' pending wakes, the Claude lane refusal names the owning route, and `callback_outbox_stats` reports `pending_by_provider` (NF-2026-00843, NF-2026-00972).

### Added

- Manager Chat image attachments: up to 4 PNG/JPEG/GIF/WebP images of at most 5 MiB per message, checked by magic bytes and stored once by content hash under the session store; only paths reach the backend and the log records only the count.

### Qualification

- Each fix has regressions that fail before and pass after; the touched suites pass. Installation and live manager replay are separate checks.

## [0.12.27] - 2026-10-05

### Fixed

- `agent_retry_finalization` re-mints a toolchain authority receipt that fails only with a drift code (card/cache identity, PATH, registry, repository fingerprint, executable identity); tamper failures stay fail-closed before any transition (NF-2026-01349).
- `recover_blocked_rework(scope_rejection_resolved=true)` admits a scope-rejected card that retention blocked as `finalize_failed` for the same launch request, so the two recovery gates no longer contradict each other (NF-2026-01350).
- Attempt-artifact re-reads ride out transient Windows read denials, and a filename-less `[Errno 13]` in finalization names its raising frame (NF-2026-01351).
- Rework feedback has one 8000-byte cap: `reject_review` and `recover_blocked_rework` store text within it whole and refuse longer text (`reject_reason_too_large` / `feedback_reason_too_large`) instead of truncating it (NF-2026-01352).

### Qualification

- Each fix has regressions that fail before and pass after; the touched suites pass. Installation and live worker replay are separate checks.

## [0.12.26] - 2026-10-04

### Fixed

- A manager-recovered blocked card is no longer rerouted as a review rejection because an earlier rejection's feedback outlived it (NF-2026-01344).
- Shell residue (an empty `$null`/`nul` file, any `*.stackdump`) is neither scope-checked nor promoted, and `agent_retry_finalization` accepts `scope_rejected` without relaunching the provider (NF-2026-01345).
- A validation-only replay of a card with dependencies carries a toolchain receipt bound to the identity the finalizer checks; receipt refusals are typed diagnostics instead of `unclassified` (NF-2026-01346).
- The crash-retry packet stays within its cap: an oversized inherited path list becomes a count, digest and rework-overlay pointer, and a refusal names the measured size (NF-2026-01347).
- Runtime JSON writes ride out transient Windows sharing denials with a bounded retry; exhaustion names the operation and path (NF-2026-01348).

### Qualification

- Each fix has regressions that fail before and pass after; the touched suites pass. Installation and live worker replay are separate checks.

## [0.12.25] - 2026-10-04

### Fixed

- Windows AppContainer validation runs every command with the request's own temp directory. CreateProcess into the container replaced TEMP/TMP with the adapter-shared `...\AC\Temp` (stale build caches, failing `git init`); the exec scratch now lives under the request home, and a non-Python command starts under a Python trampoline whose site shim puts TEMP/TMP back on the request scratch (NF-2026-01341).
- The recorded validation command stays the declared argv; exit codes, including unsigned Windows NTSTATUS values, are preserved.
- Includes 0.12.24: authenticated same-range editor corrections are recovered (NF-2026-01322).

### Qualification

- AppContainer launcher, exec-scratch, interpreter-parity and worker-workspace regressions pass; an opt-in live AppContainer probe shows a nested child sees the request scratch instead of `AC\Temp`. Installation and live worker replay are separate checks.

## [0.12.23] - 2026-10-03

### Fixed

- Windows Source Graph writer contention uses a verified process-creation identity through the platform interface; PID-only or unknown identity stays fenced.
- Foreign-process signalling, private ownership, atomic publication and existing safety gates are unchanged.
- Canonical OpenCode executable binding and AST closure caching are included. Full sandboxed Muse/MCP startup and Manager Chat end-to-end qualification remain unclaimed.

### Qualification

- Independent Windows creation-identity, daemon, platform and invariant regressions pass. Installation, activation and live worker replay are separate checks.

## [0.12.22] - 2026-10-03

### Fixed

- Editor workers may stage an explicitly allowed output outside the mandatory-output set. Mandatory completion is not a second filesystem authorization scope.
- Exact allowed-write/action contracts, semantic hashes and ranges, substantive-content checks and required-output completion remain enforced.

### Qualification

- An old-source red regression and repaired production collector/text transport pass; independent bridge, console and Python checks pass.
- This is a Task MCP restoration, not completed Manager Chat, sandboxed OpenCode/Muse, context-policy or Skills integration. Installation and live worker replay require separate evidence.

## [0.12.21] - 2026-10-03

### Fixed

- Authorized complete-create editor-worker payloads use an independent 255 KiB bound, restoring measured 16,988- and 19,503-byte stages rejected by the general 16 KiB guard.
- Reads, range edits, malformed operations and requests outside the card scope retain 16 KiB. Payload fidelity, semantic authorization and mandatory-output gates are unchanged.
- Oversize results and traces report the actual selected bound.

### Qualification

- Discriminating old-source regressions, the full Node bridge suite and 100 Python bridge tests passed independently. Packaging, installation and live activation are separate checks.
- Full Manager Chat, shared all-seat context policy, sandboxed OpenCode/Muse and Skills completion are not claimed.

## [0.12.20] - 2026-10-03

### Fixed

- Editor language-model workers use a dedicated, explicitly write-gated MCP connection instead of the read-only dashboard connection. Source Graph, semantic prepare and apply share one authenticated worker session.
- The worker connection cannot launch processes; dashboard gates remain closed and server task/claim/scope/HMAC checks remain authoritative.
- Closing Manager Chat or the dashboard preserves worker edit targets; repository changes and extension deactivation dispose the worker connection.

### Qualification

- Independent full extension qualification passed across 64 test files; 16 Python worker authorization regressions passed. The Python runtime is unchanged from the separately qualified 0.12.19 source apart from the release version literal.
- Packaging, installation and the genuine live existing-file edit canary remain to be verified. Manager Chat completion, sandboxed OpenCode/Muse startup and Skills invocation coverage are not claimed.

## [0.12.19] - 2026-10-03

### Fixed

- Windows worker validation prefers repository-local temporary storage over inherited host temporary paths.
- VS Code language-model workers continue on verified Source Graph progress while retaining duplicate and no-progress safeguards.
- Zero-diff disposition fixtures enable Git long paths and use owned cleanup for read-only Git objects.

### Qualification

- Restoration candidate only: focused independent checks passed; full final Python qualification is pending after a previous run reported 35 failures. Installation and live activation are not yet verified.
- This release does not establish completion of Manager Chat, sandboxed OpenCode/Muse startup or Skills invocation coverage.

## [0.12.18] - 2026-10-03

### Added

- Manager Chat renders glyph blocks, merged command output, expandable diffs, task links, a measured usage footer and context hairline.
- Safe DOM Markdown, transient live partial text, frame-coalesced rendering, 400-block pagination, scroll control and final-only screen-reader announcements are wired through the actual chat panel.
- Both dashboards display main and subagent Claude transcript usage separately as unpriced repository history, without adding it to canonical task costs.

### Fixed

- Empty-output workers await explicit completion instead of being finalized after their first test-only stage.
- Source Graph progress is recognized from returned source evidence and verified chained pages; duplicate and no-progress protection remain enabled.
- Pristine pending cards can be explicitly rerouted to an eligible implementation worker with audited provenance and atomic concurrent-claim protection.
- Skills adoption totals use the measured registry rather than eight rendered rows; accepted learning decisions now feed the existing bootstrap evidence path with exact native acceptance and actor binding.
- Declared worker contexts include digest-bound, task-scoped Development Rules with deduplicated path applicability; cards without that context contract retain their existing behavior.
- Cost headers distinguish unavailable cost from observed zero and label partially priced totals with measured coverage.
- Extension tests isolate child homes, configuration and temporary files inside the repository, protecting live manager routes.
- Windows callback registry snapshots use shared, no-follow reads and pinned-parent publication so atomic updates do not transiently hide a live instance; default platform rename behavior is unchanged.

### Qualification

- Full canonical Python qualification passed (15,614 tests, 444 skips, two warnings and three subtests); all 64 extension test files passed. Live activation remains pending; sandboxed OpenCode/Muse startup is not claimed fixed.

## [0.12.17] - 2026-10-03

### Added

- A developer tool, `scripts/capture_manager_stream.py`, records one real manager turn as a redacted provider-stream fixture: repository, working-directory and home paths in every spelling (including nested JSON and Claude's project-slug form), the user name, e-mail addresses, UUIDs and OpenCode session ids become fixed placeholders, also when a value is split across Claude deltas. Recorded Claude, Codex and OpenCode manager streams are committed as test fixtures (NF-2026-01235, NF-2026-01237).

### Fixed

- Transient Source Graph recovery contention is retried on a short bounded backoff with a per-job budget instead of failing the refresh job, `refresh_now()` passes the recovery gate, and a stale retryable recovery error is cleared (NF-2026-01218).
- A deferred review wake is no longer pre-empted by the seeded `review_ready` row, and the manager-ready wake is always announced by re-arming an already delivered row of the same episode (NF-2026-01220).
- Semantic-edit coverage excludes inherited-unchanged rework paths, derives new files from the empty placeholder baseline and labels no-change runs `no_changed_paths`, so a rework attempt is no longer reported as raw edits it never made (NF-2026-01222).
- A Manager Chat model switch or backend re-bind restores the session's own conversation in the new backend's brief (NF-2026-01225).
- A Claude failure result carries its own text as the Manager Chat provider error instead of the subtype `success` (NF-2026-01226).
- A Manager Chat wake callback is acknowledged only after its turn finished and was delivered; a failed turn keeps it and is retried on a backoff (NF-2026-01227).
- The Manager Chat event log appends one line per event and bounds payload fields on their own, so a long reply keeps its text (NF-2026-01228).
- The Manager Chat seat check reads a 512 KiB tail of the event log, so a session whose newest event is a long reply still counts as active (NF-2026-01244).
- A manager seat child's `PWD` names the directory it was started in, so an OpenCode seat works in the manager repository (NF-2026-01236).

## [0.12.16] - 2026-10-02

### Fixed

- Source Graph recovery rolls a hot journal back on a private staged copy and publishes it through the staged-generation path instead of opening the canonical index writable. A reader holding the index no longer turns recovery into a 33 s raw `database is locked` under the writer lease: contention is a typed, retryable standby (`publish:sqlite_busy`, or `connect:snapshot_changed` when another opener rolled the index back between the two copies) reported in about a second (NF-2026-01186).
- A failed `vscode_lm` tool turn reports its real reason instead of `mcp_unavailable`, and the sanitized message still never carries raw child output, environment values or filesystem paths (NF-2026-01201).
- Only consecutive malformed `vscode_lm` replies exhaust the invalid-JSON budget: the counter resets after every parsed envelope (NF-2026-01200).

## [0.12.15] - 2026-10-02

### Added

- PlatformIO owns a repo-local worker sandbox plan and a CPU-derived slot lease: worker HOME, TEMP, cache, state, config, workspace and log storage is planned under `<repo>/.aiworkhub/runtime/sandboxes/slots/<slot-id>`, with no admin, no drive letter and no path derived from the user profile or the system temp directory. Nothing calls it yet; the launcher wiring is a later change (01173 part A).
- A Windows restricted-token launch primitive starts a child under a restricted primary token at Low integrity inside a kill-on-close Job and writes one DACL on the slot root, refusing a UNC, device, reparse-point or out-of-sandbox root before any descriptor write. It needs no elevation, touches no ancestor ACL and is not yet wired into the worker launch path (01173 part D).

### Fixed

- A launch that fails before a worker runs leaves the card recoverable on the same task ID: the exact claim persists the request linkage before the fallible preflight, the failure records its request id on the blocker, and terminal retry and dead-processing reconciliation accept an unattached card while still refusing a contradicting request id. A legacy blocked card without linkage is recovered through its task-event trail as `legacy_unlinked` (NF-2026-01192, NF-2026-01202).
- An advisory runtime notice no longer masks a worker's lifecycle state and liveness: process readers take the last lifecycle row, and `status()` returns the latest advisory separately as `runtime_notice` (NF-2026-01194).
- A validation-only replay that fails before any declared command runs terminalises `finalize_failed`, not `validation_failed`; a replay whose declared command ran and failed keeps `validation_failed` with its receipt (NF-2026-01195).
- A blocked-rework recovery that keeps the rejected candidate seals a rebind the reroute can authenticate, so the sequence no longer fails with `reroute_manager_rejection_identity_mismatch` (NF-2026-01196).
- A rejected learning commit requires a manager rejection that actually landed for that exact request (NF-2026-01197).
- A failed promotion write reports `promotion_write_failed:<relative>:<op>:errno=<n>:winerror=<n|none>` instead of a bare errno, and a transient sharing violation on replace or unlink is retried with a bounded backoff (NF-2026-01198).
- A retained rework predecessor's path hashes and sealed delta come from one capture, so a write between two reads can no longer publish a pair that fails authentication (NF-2026-01199).
- A worker prompt lost before the supervisor reads it rejects the launch instead of starting the worker on empty stdin (NF-2026-01159).
- Tests take the toolchain-authority test key from one conftest fixture, so a contained validation lane never needs the on-disk key (NF-2026-01204, NF-2026-01213), and three timing-dependent tests wait on the observed state instead of a fixed budget (NF-2026-01191).

## [0.12.14] - 2026-10-01

### Added

- Validation evidence and the review packet carry pytest outcome counts and the first failure block for each pytest validation row, and the packet reports `validation_skipped_total`, so a reviewer sees what a command measured instead of only its exit code (NF-2026-01155).

### Fixed

- A rework launch whose predecessor paths were cleanly rebased onto a drifted main no longer fails with `rework_overlay_hash_mismatch`: the overlay seals the rebased bytes when they match the post-seed workspace baseline, and foreign bytes are still refused (NF-2026-01187).
- Windows child termination is a bounded ladder that ends in the kill-on-close Job and never reports a live child as stopped (NF-2026-01166).
- The VSIX packaging test fixture builds into the validation scratch root and leaves the release `dist` untouched (NF-2026-01183).
- A Codex manager that switched to another repository can switch back: the shared-route identity takes the owner window from a coherent ownership-ledger projection, so the reverse transfer no longer fails with `route_ownership_epoch_conflict` (NF-2026-01188).

## [0.12.13] - 2026-10-01

### Added

- A verified manager can record an audited disposition of one non-blocking reviewer finding: `aiworkhub_manager_review_finding_dispose` stores an append-only receipt bound to the candidate digest and the verified reviewer receipt; a dismissed LOW/MEDIUM finding with counter-evidence lifts only its own refinement blocker, and a high/critical finding cannot be disposed (NF-2026-01170).
- The task store can resolve a `scope_rejected` card on the same task ID: `recover_blocked_rework(scope_rejection_resolved=True)` is always clean-root, requires feedback, verifies the rejected request identity and replays idempotently (NF-2026-01169).

### Fixed

- `review_finding_disposition_candidate_unavailable` carries a `detail` object naming the unresolved identity fields, the task status and the request ids, and terminal evidence that belongs to another request is never bound (NF-2026-01180).
- `retained_terminal_candidate_identity_invalid` names the first failing check as a `:<check>` suffix from a closed vocabulary; `:no_candidate` is the case that needs `clean_root_if_predecessor_missing` (NF-2026-01181).
- A role-bearing pytest validation that passed zero tests is unmeasured, not passed: the behavioral gate fails with `behavioral_evidence_unmeasured:<roles>` when such a row exits 0 with only skips (NF-2026-01165).
- A malformed FTS5 index met at savepoint release or at commit is repaired or reported as a corrupt-index error instead of failing every Source Graph build (NF-2026-01172).
- A lost Windows identity-slot race no longer overwrites a real Source Graph build error; daemon health keeps the earlier `last_error` and records the race in `last_slot_contention_at` (NF-2026-01175).
- Git pipes in the worker workspace are decoded as UTF-8 with replacement instead of the locale codec, so one unmapped byte on a cp1251 host no longer returns an empty stream with rc=0 (NF-2026-01178).
- A freshly provisioned worktree no longer shows seeded files as modified (NF-2026-01168).
- A causeless workspace-GC row no longer overwrites the recorded terminal reason, and a typed credential category classifies as retryable `credential_expired` (NF-2026-01174).
- `reviewer_state_unknown` carries `state_unknown_cause`, the exception class name behind it (NF-2026-01164).

### Changed

- The finding-disposition entry points moved into `process_launcher_accept_review.py`, so `process_launcher.py` is back under its size ratchet; the OS-dependency boundary and four stale test expectations hold again (NF-2026-01176, NF-2026-01179).

## [0.12.12] - 2026-10-01

### Fixed

- The NeedFix header card reports the store's real active and stored counts instead of the length of the 200-row snapshot page; a new additive `stored` field feeds the "stored" label, and `truncated` means only that the items page is bounded (NF-2026-01171).
- A terminal launch failure that changed nothing is recoverable on a clean root: `recover_blocked_rework` proves the request's own worktree untouched against its workspace baseline, refuses untracked files the baseline does not cover, and re-checks a stat fingerprint of the tree under the writer lease without spawning (NF-2026-01160).

## [0.12.11] - 2026-10-01

### Added

- `reject_review` and `recover_blocked_rework` accept an optional `validation_amendment`: validated commands are appended to the card's validation in the same transaction, so a reject reason that demands new evidence is runnable by the next attempt's terminal validation; an over-cap amendment is refused on every branch, including an idempotent replay (NF-2026-01151).

### Changed

- The C/C++ quoted-include seeding moved into `worker_workspace_include_seed.py`, so `worker_workspace.py` is back under the module-size ratchet without raising the limit (NF-2026-01156).

## [0.12.10] - 2026-10-01

### Added

- Converted NeedFix rows get SZZ-style escaped-defect attribution: the lines a fix deleted or modified are blamed back to the accepted card whose sealed receipt promoted them, and tied, mixed or ambiguous blame stays `unknown`; `scripts/needfix_attribution_backfill.py` backfills existing rows (dry-run by default) (RM-2026-00076).

### Fixed

- Concurrent reviewer launches on Windows share one AppContainer profile: `derive_identity` retries a raced `CreateAppContainerProfile` (bounded, with jitter) and falls back to deriving the winner's SID (NF-2026-01096).
- Tests inside the worker sandbox run for real: the symlink capability guard raises an `OSError` instead of `pytest.skip`, so `tmp_path` tests no longer skip silently, and only a real symlink denial is reported as `sandbox_capability_denied:symlink` (NF-2026-01150, NF-2026-01163).
- A read-only worker seat degrades an oversized Context MCP reply to truncated inline content with a typed `spill_unavailable` instead of failing the query; collision and tamper failures stay fail-closed (NF-2026-01162).
- A review lens whose only reviewer hit a process limit gets a fresh reviewer instead of reusing the blind one, and `accept_preview` predicts `reviewer_could_not_inspect:<lens>` with the same classifier `accept_review` uses (NF-2026-01158).
- KB and AI Memory writes fill `created_at`/`updated_at` on legacy schemas with NOT NULL timestamp columns instead of failing the insert; upserts keep `created_at` and refresh `updated_at` (NF-2026-01161).

## [0.12.9] - 2026-09-30

### Fixed

- Code tasks on C/C++ repositories seed the project headers named by angle-bracket `#include <x.h>` directives, resolved against the declared include roots only (NF-2026-01149).
- A Claude CLI launch on the haiku route passes the Claude Code model spelling `claude-haiku-4-5` on `--model`, so it no longer fails with `model_not_found`; every other subsystem keeps the canonical `claude-haiku-4.5` id (AIWORKHUB_01182).
- OpenCode workers can no longer reach the write/patch/multiedit/todo/list builtins, and the `opencode/*-free` route is reported as `opencode_free_tier_client_restricted` instead of disappearing from discovery (NF-2026-01081).
- Rejecting a review also cancels the live auto quality reviewers bound to the rejected request (NF-2026-01087).
- Tests that need symlinks or named pipes carry `requires_symlink` / `requires_named_pipe` markers and skip with `sandbox_capability_denied:<capability>` only where the worker sandbox denies that capability (NF-2026-01138).
- The header manager-chat insight card and its dead dialog path are removed; the sidebar control is the single entry point (AIWORKHUB_01180).

## [0.12.8] - 2026-09-30

### Fixed

- A running reviewer stays visible to lens exclusivity and reuse for its whole life, not only the spawn window (NF-2026-01131).
- A rework launch seeds from the recorded, digest-bound delta artifact first; an emptied predecessor worktree no longer blocks it (NF-2026-01044).
- A supervisor spawn failure reaches the terminal event as the transient `sandbox_spawn_failed` (NF-2026-01136).
- A ranged `read_first` entry (`path:N-M`) no longer fails launch as an unknown path (NF-2026-01137).
- A route whose every recent launch died in the sandbox is marked not launchable, so auto reviewer routing stops picking it (NF-2026-01140).
- Write-path normalization keeps the dot in `.aiworkhub/...` paths, and `reject_review` refuses residual paths that are unsafe or outside `allowed_writes` (NF-2026-00535).
- The forbidden-vs-`allowed_writes` guard no longer fails open on a `.//` or leading-`/` spelling, and a file-valued `allowed_writes` entry no longer covers nested paths (NF-2026-01144).

## [0.12.7] - 2026-09-30

### Fixed

- The SDLC sync opens no case for quality-reviewer cards or for withdrawn tasks that were never accepted, and reports them as skipped (NF-2026-01133).
- Escaped-defect attribution counts only converted NeedFixes and trusts the store-verified `caused_by` regardless of the event window (NF-2026-01134).
- The test stage falls back to the historical accepted-outcome authority when only the live promoted bytes changed since accept (NF-2026-01132).
- A card that task hygiene archives after it was accepted is no longer treated as withdrawn, so its build, test and deploy proofs verify (NF-2026-01135).

## [0.12.6] - 2026-09-29

### Added

- SDLC sweep: the task reconciler records every stage it can prove (plan, design, build, test) for each card, with no manual call, and runs escaped-defect attribution and the control bands once per sweep (RM-2026-00076).
- Deploy and maintain proofs: a card reaches deploy `ready` once a confirmed release receipt's commit holds every file it promoted, and maintain `ready` when deploy is ready, no control band is breached and no open NeedFix names it as the cause (RM-2026-00076).

### Fixed

- A rework overlay rebases the previous attempt's changes onto newer canonical lines instead of reverting them (NF-2026-01113).
- Rejecting a card to `blocked` keeps its sealed rework delta, so a recovered rework no longer fails `required_output_unchanged` (NF-2026-01112).
- A launch without an adapter derives the runner's registered adapter instead of parking the card in `blocked` (NF-2026-01125).
- Supervisor finalization re-reads the on-disk record, and sandbox-denied anchored reads are skipped instead of failing (NF-2026-01107).
- A reconciler lock held by a stale runtime generation hands over to the current runtime (NF-2026-01124).
- Deferred reviewer reuse matches the sealed model and attempt identity (NF-2026-01123).
- Rework cards sort ahead of fresh cards within the same priority (NF-2026-01097).
- The manager semantic edit reads its stdin replacement as strict UTF-8 (NF-2026-01121).
- The SDLC sweep opens its state database through the shared WAL writer instead of a raw connection, and the symlink boundary tests skip only where the OS refuses symlinks.
- The AppContainer persistent-grant test pins the full WRITE_DAC message regardless of how deep the test directory is (NF-2026-01128).

## [0.12.5] - 2026-09-29

### Added

- SDLC control bands: a deterministic detector compares the last 20 decided cards with the 100 before them (first-pass acceptance, review rounds, validation-failed rate) and files a medium NeedFix at 2σ and a high one at 3σ, one per metric, with no model in the loop (RM-2026-00076).
- Escaped-defect attribution: a NeedFix whose fix card was accepted is traced by blame to the accepted card that introduced the defect and linked through a validated, immutable `caused_by` (RM-2026-00076).
- Release receipts: `scripts/release_receipt.py` records each built VSIX (commit, digest, rollback target) and its installed confirmation in the tracked `.aiworkhub/releases.jsonl` (RM-2026-00076).

### Fixed

- A worker contained in an AppContainer can run its card validation in-loop: `validation_run` executes in a separate validation AppContainer instead of refusing, so workers no longer loop blind until post-exit validation (NF-2026-01120).
- The reconciler heartbeat no longer reads as stale: its status temp file is hardened to an owner-only DACL when the inherited one is untrusted, so the durable status is written (NF-2026-01119).


## [0.12.4] - 2026-09-29

### Added

- Automatic cleanup janitor: every done, reject or archive decision queues one per-repository sweep on a background thread. The sweep removes finalized worker worktrees (sealing each rework predecessor's delta first), unreferenced rework deltas, decided attempts' terminal logs, aged process bundles and expired spill payloads. It retires callbacks of decided tasks, archives decided tasks past their TTL, and closes NeedFix whose linked task was accepted. Hints coalesce, and the sweep wakes itself at the next restore deadline.
- `aiworkhub_task_create` takes a declared difficulty (bounded / standard / complex), and the worker's reasoning effort follows it instead of always running at maximum (NF-2026-01101).

### Changed

- The janitor is the single deletion path: the separate accepted-artifact cleanup lane is retired. A step that fails, or an item it cannot delete, makes the sweep report `ok: false` and files one deduplicated NeedFix instead of failing silently.
- A reviewer packet now carries what the reviewer used to fetch turn by turn, so a review is one bounded read pass (NF-2026-01108).

### Fixed

- The automatic review chain reuses a usable or running reviewer for the same target, claim epoch and lens instead of launching a duplicate (NF-2026-01064).
- The retention test fixtures no longer need local git transport, so validation runs inside the worker sandbox (NF-2026-01116).

## [0.12.3] - 2026-09-28

### Changed

- A rework round is reviewed on the hunks that changed since the predecessor round: the candidate carries a sealed rework delta, and a lens that already judged the predecessor reviews only those hunks instead of the whole candidate. An unreadable or partial delta falls back to the full review surface with a recorded reason (NF-2026-01093).

### Fixed

- The manager's rework amendment is sealed into every reviewer lens packet, so the reviewer judges the candidate against the amended contract instead of the original card (NF-2026-01086).

## [0.12.2] - 2026-09-28

### Fixed

- Manager AI Memory search and writes repair a missing `memories_fts` index in place without losing rows, so a legacy or half-migrated store no longer fails the worker's mandatory AI Memory call with `fts_unavailable` (NF-2026-01083).
- A stale Manager Console session no longer holds the manager seat: bootstrap reports the calling chat's own route, task creation stops stamping the expired session as callback origin, and wake never starts on it (NF-2026-01078).
- Semantic edit numbers lines on `\n` only, so a range from Source Graph, git or an editor selects the same text in files holding bare CR, U+0085 or U+2028; a junction path component is refused like a symlink (NF-2026-01077).
- A reviewer spends a submit attempt only on its own rejected submission, never on a supervisor persistence fault (NF-2026-01074).
- Source Graph directory diff reports edits that differ only in invalid UTF-8 bytes as binary and names the correct side of a skipped file (NF-2026-01079, NF-2026-01085).
- The runtime symlink escape test skips only when the symlink privilege is missing, so the AppContainer validation lane no longer fails it (NF-2026-01071).

## [0.12.1] - 2026-09-28

### Added

- Source Graph directory diff core: compares two directory trees, classifies each file as added, removed, modified or renamed, and emits bounded unified-diff hunks plus per-symbol body changes (RM-2026-00071).
- A language-neutral `custom_validation` card template and host-toolchain trust for every Source Graph language family, so cards can be created and validated in all of them (NF-2026-01072 parts A and C).
- Reviewer ingest resolves findings through the index-proven symbol resolver; overbuild findings bind to indexed symbols and the reviewer prompt covers every lens item in one pass (NF-2026-01065, NF-2026-01057 part 1).

### Fixed

- Reviewer findings that contain U+2028, U+2029 or U+0085 are durable: the audit ledger splits on the line-feed character only, and rework overlays count lines the way the AST does (NF-2026-01076).
- One live reviewer per lens, with liveness taken from the launcher's per-event rule (NF-2026-01058, NF-2026-01069).
- Rework sealing reports `RuntimeTempError` as a recovery error, never a raw temp failure (NF-2026-01070).
- Validation records carry a bounded candidate-authority reference (NF-2026-01060 part A).
- Relinking a NeedFix with a verified integrated commit resolves it (NF-2026-01062).
- The GLM bridge over VS Code LM orders no-progress events correctly and rejects oversized tool input cleanly.
- The extension webview suite passes again, and `*.css` is pinned to LF so fresh worktrees match the suite's markers.
- AppContainer-lane symlink and launcher tests skip only on the measured missing capability (NF-2026-01071).

## [0.12.0] - 2026-09-27

### Fixed

- The Windows AppContainer git helper temp directory no longer uses 8.3-shaped path components, so in-container `git init` stops failing with rc=128 on the traverse-only sandbox root (NF-2026-01053).

## [0.11.99] - 2026-09-27

### Fixed

- Reading NeedFix reconciles explicit `Resolves: NF-YYYY-NNNNN` trailers on HEAD-reachable commits into verified resolution, so hand-integrated fixes close without manual transitions; a git timeout falls back to a bounded newest window and a held database write keeps the watermark for the next read (NF-2026-01048).
- Linking a NeedFix to an existing task with a verified integrated commit resolves it and reports `resolved: true` on the first link; active NeedFix state is derived from the linked card (NF-2026-01048, NF-2026-01049).
- Source Graph `bodygrep` searches the inner text of a quoted phrase instead of falling back to a token scan (NF-2026-01051).

### Performance

- The declared-invariants gate parses each module once and shares one scan per check, cutting a card validation from about 111 s to about 33 s (NF-2026-01052).

## [0.11.98] - 2026-09-27

### Fixed

- Contained Claude workers on Windows get the PowerShell tool instead of a Git Bash that cannot start inside the AppContainer; every Bash deny has a PowerShell mirror and recursive PowerShell discovery stays denied (NF-2026-01043, NF-2026-01046).
- Contained validation reads a host-owned tree-sitter grammar mirror under `.aiworkhub/runtime/tree-sitter-cache` instead of the per-user cache on C:, with a read-only grant scoped to that mirror (NF-2026-01042).

## [0.11.97] - 2026-09-27

### Fixed

- A VS Code LM credit-limit refusal opens that route's circuit until its reset, so unpinned launches stop re-picking an exhausted route (NF-2026-01036).
- Accept review's seam guard is green again: `_accept_manager_identity` is declared as an accept-review local name.
- A template card created with a validation override keeps the repository package gate (NF-2026-01041).
- Provider-output failure classifiers move out of `process_launcher` into their own module; the module size ratchet is back to 14148 (NF-2026-01041).
- Test suites follow their contracts: AppContainer sandbox-root grants (NF-2026-01039), the paged review packet, the codex stdin prompt, and LSP symlink tests that skip only when the symlink privilege is missing (NF-2026-01042).

## [0.11.96] - 2026-09-27

### Fixed

- The Windows AppContainer validation lane keeps `git` on `PATH`; only the interactive agent lane drops Git Bash, so candidate tests that spawn `git` stop failing in-container (NF-2026-01037).
- A NeedFix can link a superseded card when its fix is a commit verified on `HEAD` (`integrated_commit`), closing NeedFixes whose fix was integrated by hand (NF-2026-01030).
- Accept evidence `verified_by` names the resolved manager instead of a hardcoded provider (NF-2026-01038).
- Sparse worker checkouts follow `importlib` dynamic imports and module-file siblings (NF-2026-01024).
- Supersede/archive receipts are bounded, the quality-review packet is paged under the host inline limit, semantic edit refuses drive-qualified and UNC paths, and policy files are pinned to LF so autocrlf worktrees stay byte-exact (NF-2026-01028/01029/01033/01034).

## [0.11.95] - 2026-09-27

### Fixed

- Upgrade garbage collection accepts worker workspaces left under the pre-0.11.94 repository-namespaced `%TEMP%` root, so rework relaunches and the legacy drain are no longer refused with `gc_workspace_shape_mismatch`.
- Source Graph repairs a damaged full-text index in place instead of staying stale.
- Terminal failures keep their measured launcher and sandbox classes instead of collapsing into `runtime_error`.
- OpenCode configuration, TOML quoting and protocol line coercion each have one owner again, clearing the copied-helper drift on main.

## [0.11.94] - 2026-09-27

### Fixed

- Windows AppContainer worker sandboxes live inside the repository under `.aiworkhub/runtime/worktrees` and are reached through a per-logon drive letter, so a worker launch no longer writes permissions on the user profile or `%TEMP%` and needs no elevation.

## [0.11.93] - 2026-09-27

### Fixed

- A Windows AppContainer worker launch and close no longer walk the whole user profile. The traverse permission on each parent folder is written to that folder alone and keeps its protection and inheritance state, so a close takes milliseconds instead of minutes to hours.

## [0.11.92] - 2026-09-26

### Fixed

- The manager route panel shows the active Manager Chat session, model and backend instead of a pending Codex thread.

## [0.11.91] - 2026-09-26

### Fixed

- Callbacks are delivered to both the Codex or Claude thread and the active Manager Chat session. A chat session alone can own a callback task.

## [0.11.90] - 2026-09-26

### Added

- Manager chat shows thinking and tool calls while a turn is running, and has a reasoning-depth control.

## [0.11.89] - 2026-09-26

### Fixed

- A callback-required task can be created from Manager Chat when an active mls- session is selected. A Codex thread UUID is no longer required for that seat.

## [0.11.88] - 2026-09-26

### Fixed

- Enabling an OpenCode model lifts the hidden vendor-adapter gate that snapped the toggle back off. A late settings snapshot no longer redraws a newer toggle.

## [0.11.87] - 2026-09-26

### Fixed

- Manager chat binds the selected model to its own CLI. Opening the chat no longer attaches an old session.

## [0.11.86] - 2026-09-26

### Changed

- Manager chat lists models, not providers. Choosing a model binds that session to the CLI that can run it.

## [0.11.85] - 2026-09-26

### Fixed

- Manager chat provider and model selection is no longer overwritten by the active session.

## [0.11.84] - 2026-09-26

### Changed

- Manager sessions are not owned by a provider. Any selected model attaches to the same session.

## [0.11.83] - 2026-09-26

### Changed

- Manager chat drops the in-chat task board. Sessions are filtered to the selected backend, and an unwanted session can be deleted.

## [0.11.82] - 2026-09-26

### Added

- Manager chat restores the last session, starts the first session from the selected model, and keeps callbacks and Context Graph turns on the active session.

## [0.11.81] - 2026-09-26

### Fixed

- A dotted or relative Python import binds to the one indexed module or symbol it names. Bare imports such as os and ast stay unbound.
- A Python annotation binds to the one same-file type or the one imported symbol of that name. Builtin names such as str stay unbound.

## [0.11.80] - 2026-09-26

### Fixed

- JavaScript member calls such as document.createElement no longer bind to a same-file helper just because the name matches.
- A JavaScript callTool of an exact aiworkhub_ tool name binds to the one Python function that tool registry names.

## [0.11.79] - 2026-09-26

### Fixed

- Source Graph health names a Python/JavaScript call boundary that has edges on both sides and none between them, instead of reporting that gap as healthy silence.

## [0.11.78] - 2026-09-26

### Fixed

- An AppContainer overlay base is copy-pinned when a hardlink fails with EXDEV, so a later canonical republish does not kill the overlay.
- AppContainer launches drop Git bash from PATH and set the shell to PowerShell, because msys cannot create objects in \\BaseNamedObjects.

## [0.11.77] - 2026-09-26

### Fixed

- The VS Code LM edit bridge accepts a one-line range given as a decimal string, and `action`/`path` aliases, instead of rejecting the stage as `range_invalid` before the worker runs.

## [0.11.76] - 2026-09-26

### Fixed

- Manager Chat ships as a persistent sidebar beside the dashboard.
- One-line semantic-edit ranges and decimal string line numbers are accepted.
- Windows validation-only replay accepts the configured temp worktree root.
- AppContainer launch does not write a DACL on C:\Users.
- A recovered pending card can be rerouted without a review-rejection receipt.
- A truncated bodygrep resumes from its own next_cursor.
- Impact keeps a symbol's recorded callers when the file sample is full.
- Zero-precision Python crashes findings stay advisory and cannot gate acceptance.
- A too-long validation temp is rebound so the nested LSP helper cwd fits CreateProcess.

## [0.11.75] - 2026-09-26

### Fixed

- VS Code LM workers fail closed after six Source Graph queries without an
  edit, even when each query is different. A successful worker semantic edit
  resets that budget.

## [0.11.74] - 2026-09-25

### Fixed

- VS Code LM workers no longer burn the full agent turn budget on repeated
  line-1 semantic-edit pins or invalid JSON. A third line-1 pin, or a second
  invalid JSON reply, fails closed. Worker semantic edit prepare/apply stays
  on the worker editor and is not rewritten onto the manager tools.


### Fixed

- Node validations inside the Windows AppContainer no longer hang or fail on
  ancestor access: every `node` command gets `--preserve-symlinks
  --preserve-symlinks-main` (realpath otherwise lstat's `C:\Users` and fails
  EPERM), and `node --test` runs in-process (`--test-isolation=none` on
  >=23.6, `--experimental-test-isolation=none` on 22.8-23.5) because libuv
  spins forever when `CreateNamedPipe` is denied (NF-2026-01009).

## [0.11.72] - 2026-09-25

### Fixed

- Kilo/Grok AppContainer launches provision the request-local XDG state leaf
  before startup and grant only bounded, revocable access below the trusted
  user-temp authority.
- Request-scoped AppContainer traversal no longer reaches repository, profile,
  drive-root, or other protected ancestors, and symlink/junction escapes are
  rejected before directory creation.

## [0.11.71] - 2026-09-24

### Fixed

- VS Code LM workers stop repeated unchanged Source Graph discovery before the
  broad turn limit, reuse one compact typed duplicate receipt, and reset only
  after a real edit or material query-boundary change.

## [0.11.70] - 2026-09-24

### Fixed

- Native OpenCode 2 workers route Bun temporary files into the request-local
  AppContainer temp authority, avoiding package-temp `lstat EPERM` failures.
- The canonical module-size invariant passes at its existing threshold.

## [0.11.69] - 2026-09-24

### Added

- Manager Chat starts on first send by selecting one configured, policy-authorized
  manager route; explicit Start remains authoritative and concurrent sends stay
  single-flight.
- Source Graph's canonical LSP acceptance path and focused delta review are
  wired into the reviewed runtime.

### Fixed

- Windows AppContainer workers use request-local user temp worktrees and Kilo's
  exact XDG state leaf is created and granted before launch, removing the
  pre-provider `EPERM realpath` failure.
- Development Rules reports context-applicable rules as applicable instead of
  mislabeling them as resolved work.

## [0.11.68] - 2026-09-23

### Fixed

- A quoted `#include` never refuses a C/C++ card launch. The seeding preflight
  refused every card whose headers live under a nested include root (for
  example `src/cpu/include`) with `local_quoted_include_unresolved`, although
  the compiler resolves those includes itself. Headers are now found through
  the repository's tracked files at `<dir>/<target>`, and an include that
  resolves nowhere (generated, system or build-provided) is skipped;
  untracked files, absolute and `..` targets and symlinks are still never
  seeded.

## [0.11.67] - 2026-09-23

### Fixed

- The Manager panel runs on Codex. Start refused every model the picker
  discovers from the Codex CLI; a second message could not resume the
  conversation (`--ephemeral` kept no session, and `codex exec resume` refuses
  `-s` and `-C`). A model the CLI itself offers is accepted, the session is
  kept, and the resume command uses only the flags `resume` accepts
  (RM-2026-00067).

## [0.11.66] - 2026-09-23

### Changed

- CLI model lists are discovered instead of hardcoded. Codex models come from
  the Codex CLI's own model cache, so a model Codex adds appears by itself.
  Claude models are the CLI's own aliases (opus, sonnet, haiku, fable),
  labelled with the exact version each alias ran on last, while the alias
  itself stays the launched value so a CLI update moves to the newest model
  on its own. The Manager panel shows these labels (RM-2026-00067).

### Fixed

- The manager-loop gate tests no longer start a real manager CLI session when
  the suite runs under a verified manager identity.

## [0.11.65] - 2026-09-23

### Fixed

- The Manager panel's claude_cli session can use the AIWorkHub manager tools.
  It was refused `aiworkhub_manager_bootstrap` ("permission denied: don't ask
  mode") because the manager seat reused the worker's tool allowlist; it now
  allows Read, Bash and the AIWorkHub MCP server and keeps the raw-discovery
  and raw-editor denies (RM-2026-00067).
- The Manager transcript no longer shows a message twice when the status reply
  and the poll timer fetch the same events.

## [0.11.64] - 2026-09-23

### Added

- The Manager panel's model list fills itself from the repository's enabled
  models for the chosen backend; pick one instead of typing it.

### Fixed

- The Manager panel's loop can now write and launch. The dashboard's MCP child
  is read-only by design, so every loop write (Context Graph turn event,
  rotation handoff) failed with `write_gate_closed`. The loop now runs in its
  own MCP child with both gates enabled, spawned only when the owner presses
  Start and stopped on Close; the dashboard child stays read-only.
  `aiworkhub_manager_loop_start/send/rotate` refuse without the launch and
  write gates (RM-2026-00067).

## [0.11.63] - 2026-09-23

### Added

- **Manager chat panel.** The dashboard has a Manager panel: pick a backend
  (`claude_cli`, `codex_cli`, `opencode_cli`) and a model, start a session and
  talk to the manager; the transcript streams assistant text, tool calls,
  callback wake-ups and errors, rendered as text only. It runs on six new
  verified-manager MCP tools (`aiworkhub_manager_loop_start/send/rotate/status/events/close`);
  a turn runs in the background and a second message while one runs is
  refused, never queued (RM-2026-00067).
- **Callbacks wake the manager by themselves.** While a manager loop session
  is active it becomes the repository's callback consumer: each review or
  terminal callback starts a turn through the same lease/ack outbox, acked
  only after the turn has started, with an hourly cap on automatic turns.
- The cost ledger reports Claude Code's own manager and subagent usage from
  its transcripts (usage numbers only, never message content), in a bounded
  `claude_code_sessions` section.
- Compact defaults for `environment_preflight`, `task_show`,
  `agent_task_status` and `manager_workforce_rank`: on this repository the
  preflight summary went from 37.5 KB to 7.3 KB and the workforce rank from
  192 KB to 10.7 KB; the exact payload stays one `detail=` call away.

### Fixed

- A validation-only replay now judges mandatory outputs by the predecessor's
  verified hashes, so a card whose validation failed only for an environment
  reason can be recovered without re-running the model (NF-2026-00966).
- Source Graph's LSP enrichment no longer holds the index open across a
  language-server round trip, which blocked publishing the next generation on
  Windows (NF-2026-00038).
- A GLM reviewer that answers in prose gets one corrective turn instead of
  failing the required correctness review outright (NF-2026-00968).
- Preflight reports the declared and the resolved provider binary apart
  (NF-2026-00030).

## [0.11.62] - 2026-09-23

### Fixed

- **Rework launches work again on Windows** (NF-2026-00042). The worker prompt
  rode the command line (`claude -p <prompt>`, a trailing positional prompt for
  `codex exec`), and `CreateProcessW` accepts at most 32767 characters. Initial
  prompts fit; a rework prompt, which adds the review feedback and crash-retry
  evidence, did not, so every rework died before the model ran — measured as
  `command_line_too_long:41773`. `claude_cli` and `codex_cli` now read the
  prompt from stdin: the launcher hands it to the supervisor on the
  supervisor's own stdin, never through a file or a metadata record, and the
  supervisor writes it to the worker and closes it. A 40,000-character prompt
  now needs a 1,491-character command line. The manager CLI backend delivers
  its turns the same way.

## [0.11.61] - 2026-09-23

### Added

- **The manager seat can launch on Windows.** `runtime_adapters.build_manager_command`
  is the host-side launch plan for the owner's own manager seat: the same argv,
  model and stream format as a worker's plan, without the Windows refusal that
  keeps a worker's native CLI inside AppContainer. The manager CLI backend
  plans every turn through it, and an AST test pins it to that one caller so no
  worker path can reach it (NF-2026-00963).

### Fixed

- Windows: an AppContainer launch failure now names its cause. The error
  carries the Win32 code and its symbolic name (for example
  `ERROR_INVALID_PARAMETER`), the executable, the command-line and
  environment-block lengths, the argument count and the working directory — as
  text and as attributes, and never an argument or environment value, which
  carry the prompt and credentials. A command line longer than the 32767
  characters `CreateProcessW` accepts is refused as `command_line_too_long:<n>`
  instead of failing opaquely (NF-2026-00042, diagnosis step).
- Windows: the AppContainer validation lane no longer fails every card that
  runs `tests/test_windows_appcontainer.py`. Seven tests need host Win32
  privileges the container does not have; they now skip there with a named
  reason, detected from the process token (`TokenIsAppContainer`), and stay
  mandatory on a host run (NF-2026-00964).
- Source Graph no longer indexes nested linked git worktrees (a directory whose
  `.git` file points at `…/worktrees/<name>`) as source, which had doubled every
  result in a repository holding another tool's worktree. Submodules, and a
  root that is itself a linked worktree, stay indexed.

## [0.11.60] - 2026-09-22

### Added

- **Manager agent loop core** (`src/aiworkhub/manager_loop.py`), the
  provider-neutral half of the AIWorkHub-owned manager loop: a persisted
  `ManagerSession` with a bounded JSONL event log, the `ManagerBackend`
  protocol a provider adapter implements, a bounded and deterministic
  rehydration `BriefBuilder` (handoff, open cards, state, context, rules), and
  a `ManagerOrchestrator` that keeps one active session per repository and runs
  one turn at a time — both refused with a named error, never queued. Rotation
  asks the model for a handoff and falls back to a mechanical one built from
  the event log; the backend and model may change between sessions because all
  state lives in the stores. Nothing imports the module yet: the server
  surface, callback consumption and the dashboard chat panel are the next
  cards.

### Fixed

- A validation-only replay grant is now honoured from **any** verified manager
  route, not only `codex`. `launch_replay_guard`, `process_launcher` and
  `task_engine` share `core.VERIFIED_MANAGER_ACTORS`, so a Claude manager's
  recovery of a blocked card no longer fails with
  `validation_only_replay_actor_mismatch`.

### Changed

- The shared development assets — roadmap, NeedFix backlog, tool recipes,
  skills, KB, AI memory and the repository config — are tracked in git, so a
  clone carries them. Because each install allocates ids independently, a host
  that already has records merges them row by row (colliding ids are
  renumbered) instead of checking the databases out over its live ones.

## [0.11.59] - 2026-09-22

### Fixed

- Windows: native `claude_cli` workers now run inside their repo-scoped
  AppContainer with their worker tools, measured on a real Windows 11 host
  (NF-2026-00025, NF-2026-00033, NF-2026-00034).
  - **Process creation.** `CreateProcessW` failed with `ERROR_ENVVAR_NOT_FOUND`
    (203), because the sanitized child environment had no `LOCALAPPDATA`.
    `launch_appcontainer` now supplies it at the one chokepoint that every
    AppContainer launch shares.
  - **Filesystem access.** The container SID gets explicit grants and nothing
    broader:
    - read/execute on the provider CLI install, as a persistent and idempotent
      grant;
    - modify on the per-request worktree, home and temp, including the
      protected directories AIWorkHub creates inside them. These are revoked on
      close by removing only this SID's entries.
    - A grant is refused for UNC or device paths, reparse points, drive roots,
      the user profile and AppData roots, the user temp directory, and anything
      inside the Windows or Program Files trees.
    - An npm `.cmd` shim runs through its native `.exe` target, which must lie
      strictly inside the shim's `node_modules`.
  - **Network.** Worker launches get outbound internet only (`internetClient`),
    with no inbound listening and no private network. Validation launches get
    no network. `windows_confinement_report()` states this split.
  - **Worker tools.** The worker MCP server runs on the host, reached through a
    per-request named pipe:
    - Only this container's SID can open the pipe, and it serves only a process
      in this launch's own job.
    - Inside the container, a System32 PowerShell shim relays stdio to the pipe.
    - The host server starts from a directory the container cannot write. It
      runs with `-P -s` and `PYTHONSAFEPATH`, inherits no `PYTHON*` variables,
      and is refused if a probe finds a container-writable directory on its
      `sys.path`.
    - Every file it reads for authority lives in a directory withheld from the
      container.
  - **Validation.** Validation commands run inside the container, read-only and
    offline, with Python from the canonical repository's venv. An interpreter
    or `pyvenv.cfg` the container can write is refused. A persistent read grant
    counts as satisfied when an ALL APPLICATION PACKAGES allow ACE already
    covers it, with deny ACEs honored in order. AIWorkHub never adds that ACE.
  - **Mid-turn validation.** For a contained worker, the host-side server
    refuses `aiworkhub_worker_validation_run`, because running candidate code
    there would bypass the sandbox. Post-exit validation still runs inside the
    container (NF-2026-00035).
- Windows AppContainer validation now runs a card's declared commands
  (NF-2026-00040). On the first real `claude_cli` card, every validation had
  failed inside the container.
  - A lane-derived `sitecustomize` answers the container's denied stats, and
    the `0o700` mkdir that CPython 3.12.4 creates with a protected DACL.
  - The lane grants the venv's `Scripts` directory read-only, so tools such as
    ruff resolve.
  - `git diff --check` runs on the host, from a verified worktree admin record,
    with every execution path disabled: no hooks, fsmonitor, textconv,
    ext-diff, global config or pager, and attributes read from `HEAD`.
  - Before git starts, the host holds and walks the candidate worktree and
    refuses any junction, symlink or hard link, so the candidate cannot
    redirect git to read outside it. File-content echo is dropped from git's
    output.
- Semantic edits hold the whole directory chain open while they read and write,
  so a junction planted in the worktree cannot redirect an edit outside it.
- C/C++ repositories: quoted includes resolve against the conventional
  `include/` and `src/` roots. A header that is genuinely missing still fails
  closed. `cmake` and `ctest` are trusted validation executables.
- `aiworkhub_task_recover_blocked_rework` works on Windows.
  - It used to probe worker pids with `os.kill(pid, 0)`. On Windows that raises
    `[WinError 87]` for an exited pid, and without a console it terminates the
    process.
  - Recovery, the Source Graph LSP child check and the recipe helper now use the
    shared liveness probe, which sends no signal.
  - An OS error from recovery now names its operation and path
    (NF-2026-00031).
- Quality-reviewer prewarm can no longer wedge the launch queue until a server
  restart (NF-2026-00027).
  - A started prewarm expires after the preparation stall ceiling.
  - A failed build always publishes a terminal phase.
  - Concurrent prewarms queue behind a capacity derived from the core count.
  - On Windows, a reviewer terminal intent can no longer settle twice.
- `aiworkhub_dispatcher_health` reports `manager_inbox_no_live_delivery`, with
  the backlog count, when callbacks wait in a manager inbox that nothing
  delivers. It gates nothing (NF-2026-00029).
- The reconciler backs off standby lock retries to a 5 s cap (NF-2026-00028).
- VS Code LM workers:
  - A worker can read its declared writable files during forced staging
    (NF-2026-00023).
  - A tool name that is not allowlisted gets one correction instead of failing
    the request (NF-2026-00032).

### Known issues

- Python validation inside the AppContainer needs the base interpreter to be
  readable by ALL APPLICATION PACKAGES. Most installs need nothing:
  - A per-user install, uv, pyenv-win or conda under the profile is granted
    automatically.
  - An install under Program Files, including the Microsoft Store Python, is
    readable by default and gets no grant at all.
  - Only an admin-owned directory outside Program Files, such as
    `C:\Python312`, needs a one-time elevated
    `icacls "C:\Python312" /grant "*S-1-15-2-1:(OI)(CI)(RX)" /T`. The launch
    error now names that exact command. A card still shows only
    `worker_failed`, though (NF-2026-00039).
- Only `claude_cli` is bridged. `opencode_cli`, `codex_cli` and the Copilot CLI
  adapters still start their worker MCP server inside the container, where it
  cannot read the state it needs.
- Host-side reads of worktree files outside semantic edit are checked for
  containment only when the path is resolved. Hard links are not yet refused
  (NF-2026-00036).
- On Windows, two `test_source_graph_lsp_integration.py` concurrency tests fail
  with `PermissionError` (NF-2026-00038). The npm-prefix seeding test
  (NF-2026-00024) and the preflight cooldown test (NF-2026-00037, timing) are
  also known Windows gaps.

## [0.11.58] - 2026-09-21

### Changed

- An SDLC case stage can no longer be recorded `ready` on a caller's say-so
  (NF-2026-00945). `ready` for Plan, Design, Build and Test is accepted only
  when the server proves it, read-only and bounded, from this repository's own
  canonical receipts, and the resolved evidence is stored with the stage
  receipt. Plan needs the case's bound task to exist, not be withdrawn and carry
  an objective. Design needs that task's contract to be falsifiable
  (acceptance criteria, validation commands and, unless read-only, a write
  scope); the task's content identity becomes the versioned design. Build needs
  a `review_ready` candidate sealed by the current claim against exactly that
  design, with its attempt bundle, terminal process event, semantic-edit ledger
  and effort/context receipt verifying or verifiably not owed. Test needs the
  sealed validation evidence to re-derive to the stored passing verdict and the
  coordinator's accepted-outcome receipt for that same candidate to validate and
  re-hash its promoted paths. The payload supplies only the Plan and Design
  content and, for Build and Test, `task_id`, `request_id` and `claim_epoch`
  pointers that must equal what the stores say; verdict-shaped keys such as
  `passed`, `verdict` or `sha256` are refused. Whatever the server cannot prove
  is refused with a typed `stage_evidence_refused:<stage>:<code>` reason and a
  next action.
- A recorded `ready` receipt is re-proven on every read. A receipt written
  before this gate, one whose stored evidence no longer hashes or re-derives,
  and one whose predecessor stage is no longer proven stay visible for audit but
  read as `unknown` with a reason. A case's `cycle` is `complete` only when all
  six stages are proven now.
- Deploy and Maintain remain explicit refusals rather than evidence gates. The
  SDLC Deploy and Maintain gates do not yet consume canonical deploy target
  allowlist, release/build provenance receipt, install receipt, rollback
  receipt, deploy approval policy, deployed release identity, observed outcome
  metrics or control-limit policy, so `ready` for either stage is refused with
  each missing producer named and is never inferred. `not_applicable` is
  refused the same way until a canonical policy registry exists, and a
  previously recorded `not_applicable` receipt reads as `unknown`. Until the
  gates consume that proof a case's `cycle` cannot report `complete`.

### Fixed

- The isolated launch now puts the OpenCode worker's request-local `awh` MCP
  config into the worker's own process environment (NF-2026-00919).
  `launch_isolated` derives it from the worker MCP runtime already generated for
  the request, spells it for the selected sandbox (mount aliases under
  bubblewrap, real host paths under Landlock and Windows AppContainer) and sets
  it as `OPENCODE_CONFIG_CONTENT`, with project-level OpenCode config disabled
  (`OPENCODE_DISABLE_PROJECT_CONFIG=1`); nothing is written to disk and no
  global OpenCode config is touched. The contract fails closed: a config that
  does not meet it refuses the launch before any supervisor or worker process
  spawns and records the launch failure with a typed
  `opencode_worker_mcp_config_<cause>` reason, an infrastructure fault rather
  than a model-quality failure. The config must be exactly the `awh` server plus
  the worker permission contract (deny by default, only the `awh` worker tools
  allowed); its `environment` may carry only the request's own binding
  variables, never a provider credential or another inherited variable; it is
  bounded to 16 KiB of ASCII JSON; and the generated source it is read from must
  be a regular, non-symlinked, current-user-owned file beneath the request HOME
  whose request, repository and audit paths match the launch. Under AppContainer
  the launch argv passes through only when the confinement is reported available
  (otherwise the launch is refused, never run unconfined), and a
  validation-shaped request is refused there. This is launch wiring only:
  whether a live OpenCode/Muse worker then connects to `awh` is not measured in
  this release (below).

### Not in this release

- Live OpenCode/Muse worker qualification and Windows runtime qualification.
  The launch wiring above is covered by unit and integration tests that run the
  real `launch_isolated`, worker config provisioner and `sandbox_argv`, with the
  task store, git and the supervisor spawn replaced by stand-ins. The Windows
  AppContainer cases fake the host platform and the Win32 probe, and a fake
  Win32 API checks that the AppContainer launcher passes the environment to the
  child unchanged. No live OpenCode/Muse worker was run against the delivered
  config and nothing was run on a real Windows host, so both remain unmeasured.
- Evidence gates for Deploy and Maintain: the SDLC gates do not yet consume the
  canonical deploy, release and outcome proof those stages would need, so only
  the first four SDLC stages are gated.
- LSP index integration, the OpenCode manager callback, the full stage-gated
  Playbook lifecycle, and portable `.aiworkhub` data are pending and not shipped
  here. No reasoning-quality improvement is claimed: causal reasoning-quality
  measurement remains incomplete, and inferred successor progression is still
  pending.

## [0.11.57] - 2026-09-21

### Fixed

- A reviewer or rework Source Graph overlay partition now pins the exact base
  index generation it was built against (NF-2026-00946). The canonical base is
  published by atomic replacement, so a marker that named only the canonical
  path let every ordinary publication break every in-flight reviewer/rework
  overlay. The marker now records the base generation's device, inode, size and
  `mtime_ns` and pins that generation by hard link beside the partition (never
  a copy or a content hash); reads compose with the pinned generation and verify
  its identity, so a newer canonical generation never leaks into a sealed
  review and a replaced or mutated pin fails closed. Where hard links are
  unsupported the marker records that, and a later base shift fails with an
  explicit `composed_base_shifted_unpinned` reason. Pins no partition
  references any more are pruned on the next marker write, and the partition
  build report carries `base_pin` and `pin_seconds`.
- A manager can reroute a retained candidate after a zero-delta launch failure
  that `recover_blocked_rework` already moved back to pending (NF-2026-00778).
  In that shape (a rejected sealed candidate, then a claim that failed at
  launch, e.g. on provider authentication, before any model work) the recovery
  drops the launch reservation and writes no transient retry, so the
  authenticated reroute was previously lost. Authority is the newest task-bound
  canonical `claim_start -> launch_failed -> blocked_rework_recovery` chain,
  matched field for field against the card's recovery and retained-predecessor
  identity, plus the process ledger proving the failed request ended
  `launch_failed` with zero changed paths on this runner. Card fields alone are
  never authority, and any later lineage event, including the reroute itself,
  makes the authorization stale, so it is one-shot.

### Not in this release

- LSP index integration, OpenCode manager callback and OpenCode/Muse worker
  qualification, the full stage-gated Playbook lifecycle, and portable
  `.aiworkhub` data are pending and not shipped here. No reasoning-quality
  improvement is claimed: causal reasoning-quality measurement remains
  incomplete, and inferred successor progression is still pending.

## [0.11.56] - 2026-09-21

### Changed

- The sealed-delta verifier that rework materialization already used is now the
  write-free `verify_rework_delta_artifact`, which authenticates a sealed delta
  and returns its exact plan; `materialize_rework_delta_artifact` delegates to
  it. Recovery therefore authenticates a collected candidate with the same
  verifier a successor materializes through.

### Fixed

- Explicit manager recovery of a blocked task (`recover_blocked_rework`) can now
  recover a timed-out candidate whose worktree retention already collected,
  from the delta sealed when the attempt terminated (NF-2026-00594). Only a
  truly absent worktree lets that delta stand in for it, and only when its
  descriptor binds this exact repository, task, request and claim epoch to an
  intact, non-symlinked artifact directly beneath the runtime's `rework_deltas`
  directory whose packet holds exactly the terminal's hash-pinned changed
  paths. Recovery then pins the descriptor on the successor's rework
  predecessor, so the existing materializer restores the sealed bytes instead
  of regenerating them. A present, dangling, symlinked or foreign worktree path
  keeps every retained-worktree check; a tampered, missing, foreign or
  mismatched delta fails closed with a typed `retained_terminal_candidate_*`
  reason and leaves the task unchanged; and the clean-root escape refuses
  (`clean_root_rework_sealed_delta_available`) rather than discard authenticated
  sealed bytes.

### Not in this release

- LSP index integration, the OpenCode manager callback, provider-neutral
  completion of the stage-gated Playbook, Muse worker qualification, and
  portable `.aiworkhub` data are pending and not shipped here. Inferred
  successor progression and causal reasoning-quality measurement also remain
  incomplete.

## [0.11.55] - 2026-09-21

### Added

- The Roadmap list and detail dashboard views carry a server-side `current_wave`
  projection: the one in-progress wave with declared goals and the highest
  target version, with each goal checked only when all of its exact tasks are
  finished. Truncated, ambiguous, unversioned, goal-less or malformed Roadmap
  evidence yields a typed `UNKNOWN` with its reason instead of a guess, and the
  projection flags a wave whose target the installed version has passed while
  goals are still unchecked.
- `aiworkhub_task_create` and `aiworkhub_task_create_from_template` accept an
  optional exact `wave_goal_binding` (`roadmap_id`, `goal_id`,
  `predecessor_task_id`) that makes the new card that goal's current task in
  place of its predecessor. The binding is stored on the card, refused up front
  when it cannot apply, applied to the Roadmap once, and repaired by the
  reconciler when writes are enabled and the Roadmap write was interrupted. It
  is never inferred from a title, topic, version suffix or prose.
- The reconciler completes an in-progress wave only when every numbered
  acceptance criterion is mapped to a goal and every exact current task of
  every goal is canonically accepted with its own verifier receipt. Pending
  work leaves the wave in progress; missing, archived, blocked, superseded or
  unverified evidence yields a typed `unknown`. The single transition is
  write-gated, refused if the wave's goals or criteria changed after the
  verdict, records the accepted receipts, and never reads an installed or
  released version.

### Changed

- The dashboard wave mini-roadmap popup takes its wave, target and per-goal
  verdict from the server's `current_wave` projection instead of ranking
  Roadmap rows itself. It shows the installed version and the wave's target
  separately (marking a passed target overdue), never checks a goal the server
  did not, and never counts an archived or stale task row as completion.

### Fixed

- Worker validation sandboxes now seed a repository file that a declared pytest
  module locates by a literal `Path(__file__)`-relative path without importing
  it, plus a JavaScript asset's tracked local `require` targets, so such a test
  no longer fails on a missing asset in a sparse worktree (NF-2026-00551). Only
  git-tracked, non-dot-prefixed files inside the repository are seeded, as
  validation support rather than allowed writes: private state and untracked
  files stay out, a path that escapes the repository or crosses a link fails
  closed, and paths no filesystem can hold are declined instead of raising an
  untyped OS error.

### Not in this release

- Inferred successor progression (a successor takes a predecessor's place in a
  wave goal only through an exact binding that a task declares), the full
  stage-gated Playbook, LSP index integration, causal reasoning-quality
  measurement, and Muse/OpenCode worker qualification remain incomplete.

## [0.11.54] - 2026-09-21

### Fixed

- Authenticated concurrent Source Graph builders now leave the second MCP
  process in standby instead of degrading a fresh canonical index. Retained
  build records preserve the semantic `running` state rather than Linux's
  one-letter process state (NF-2026-00933).

### Not in this release

- Automatic mini-roadmap progression, full Playbook stage gates, LSP index
  integration, causal reasoning-quality measurement, and Muse/OpenCode worker
  qualification remain incomplete.

## [0.11.53] - 2026-09-21

### Added

- Verified worker-attempt receipts now persist the selected reasoning option and
  reported model context capacity. They do not establish provider-internal
  reasoning effort or a causal quality improvement.

### Fixed

- Truncated semantic-review packets tell reviewers to inspect hash-matched
  candidate overlays for omitted hunks, without treating a genuine missing or
  stale overlay as verified evidence (NF-2026-00931).
- Required-output contracts are checked at task creation; terminal failures
  distinguish validation failure from provider timeout, and rate limits have
  a typed event.
- Dashboard snapshots bound their quarantine detail instead of returning
  an unbounded list.

### Not in this release

- The full stage-gated Playbook, LSP index integration, causal reasoning-quality
  measurement, and Muse/OpenCode worker qualification remain incomplete.

## [0.11.52] - 2026-09-20

### Added

- Semantic review scope is bounded to a candidate's exact changed segments:
  reviewer prompts lead with the authenticated changed hunks, then only the
  graph-connected callers and tests the scoped audit lists, then its explicit
  known unknowns. Unchanged, previously-reviewed paths are recognized as
  context rather than new review surface, and missing or stale changed-segment
  evidence fails closed instead of supporting a clean result.
- Source Graph adds a bounded LSP transport with fail-closed definition
  classification (repo-internal, stdlib, dependency, unresolved, ambiguous,
  server-unavailable) over a private workspace. This is the transport
  foundation only; LSP index integration is not included.
- The dashboard wave mini-roadmap renders the current wave's goals as a live
  checklist joined to each goal's task states, instead of a static list.

### Fixed

- Blocked-rework recovery lets a strictly later terminal failure (a newer
  claim epoch) supersede a stale retained predecessor, re-deriving the
  predecessor from the failure's sealed delta instead of inheriting the
  earlier episode's candidate (NF-2026-00515). A predecessor without a
  trustworthy claim epoch is never superseded by failure history.

### Not in this release

- LSP index integration, the full stage-gated Playbook, reasoning matched to
  an accepted outcome, and Muse/OpenCode worker qualification remain
  incomplete. VS Code LM still records only the reasoning option sent and the
  reported context capacity; a sent option is not proof of matched internal
  reasoning effort.

## [0.11.51] - 2026-09-20

### Added

- Repository-bound SDLC cases now have durable stage receipts and MCP read/write
  surfaces, with exact canonical task binding. This is the case protocol
  foundation, not the completed Plan-to-Maintain gate.
- VS Code LM workers now record what reasoning option was actually passed to
  `sendRequest`, the selected model's reported context capacity, and explicit
  unknown/provider-internal state. Durable process-attempt comparison is still
  pending; a sent option is not proof of internal reasoning effort.
- Accepted-task outcome metrics read complete histories for a bounded recent
  cohort and report incomplete and unverified histories separately instead of
  treating a raw event cap as complete evidence.

### Fixed

- OpenCode worker MCP registration uses the bounded `awh` alias and no longer
  leaves a duplicate legacy alias. VS Code LM bridge requests preserve required
  outputs and recover from oversized tool input without falsely losing the
  worker attempt.
- Manager cost-ledger summaries report bounded coverage and unknown-cost truth.
  Source Graph preserves JavaScript/TypeScript call-site byte coordinates for
  subsequent LSP qualification; the LSP resolver itself is not shipped yet.

## [0.11.50] - 2026-09-19

### Added

- CLI worker launches apply the verified reasoning-effort decision and
  record the verified provider/model context window. Effort-control argv
  tokens are emitted only when that decision is APPLIED; context capacity
  is never used to pad the prompt.
- The VS Code LM bridge now receives the authenticated card and publishes
  a reasoning decision plus a model-context receipt. The VSIX host applies
  a declared effort option only when the selected model exposes selectable
  keys that honor the canonical profile; otherwise it records
  unsupported, provider-default, unverifiable or capability-ceiling and
  does not claim an applied value.
- The dashboard identity strip includes a wave mini-roadmap info popup
  for the current in-progress, current or active Roadmap wave.

### Fixed

- Native reviewer retained-stream compaction now also drops
  `assistant.reasoning_delta` ticks, so a long thinking turn no longer
  fails a completed review as `provider_events_oversized`. The live
  stream a human watches is unchanged.
- Sparse worker validation worktrees seed the four committed VS Code
  raster fixtures that extension-static tests require. That manager/test
  support is not a VSIX UI feature. The unreadable OpenCode config check
  remains repository-only test work.

## [0.11.49] - 2026-09-19

### Added

- The accepted-task evaluation corpus is resealed against 49 current-byte,
  receipt-authenticated examples, with a live provenance check.
- Outcome-linked NeedFix and SDLC metrics, provider reasoning policy, typed
  learning dispositions and review-chain recovery are included from this wave.

### Fixed

- SDLC metrics use the shared read-only SQLite connector, so repository paths
  containing `#` are encoded correctly rather than opening the wrong database.
- OpenCode's global AIWorkHub MCP registration uses the short `awh` alias and
  remains repository-neutral; reviewer tools and worker adapter identity are
  wired for OpenCode routes.
- Windows and OpenCode fixes documented in the staged 0.11.45-0.11.48 sections
  below are included in this release. Those numbers were written as development
  notes but were never published as Git tags or separate VSIX releases.

## [0.11.48] - 2026-09-16

### Fixed

- OpenCode's Settings model list still dropped newly-discovered models (the
  `nemotron`, `ling`, `mimo` and `muse-spark` variants among them) even after
  0.11.47 fixed the discovery cache handoff: the compact catalog row budget
  (64) was shared unfairly across providers, because every model a repository
  owner had ever individually toggled in `.aiworkhub/config/models.json` was
  treated as a reserved, priority row before any per-provider fair share ran.
  Copilot's long history of individually-toggled `vscode_lm` models consumed
  nearly the whole budget, starving OpenCode's largely-undeclared catalog down
  to a fraction of its real size. `MAX_MODEL_POLICY_CATALOG_ROWS` is raised
  from 64 to 128, comfortably inside the existing 256 ceiling, so today's
  catalog fits without truncation.

## [0.11.47] - 2026-09-16

### Fixed

- OpenCode's model settings still under-reported what was actually installed,
  even after 0.11.46 fixed executable resolution: `remember_preflight_snapshot`
  -- the write side of the cache Settings reuses so it never spawns a second
  `opencode models` probe -- was only ever called from the Workforce catalog
  builder, which the Settings read path does not itself invoke. A Settings
  read taken before the Workforce view had run once therefore always saw an
  empty cache and reported zero OpenCode models, regardless of how many were
  actually installed. `build_preflight` now warms that cache itself, so any
  preflight read -- Settings, Workforce, or the `environment_preflight` tool
  -- keeps it current regardless of call order.

## [0.11.46] - 2026-09-16

### Fixed

- Windows: a fresh, correctly-created terminal-authority key could still be
  refused by the read-side trust check landed in 0.11.44, because the create
  path never hardened the key's DACL and it kept inheriting whatever the
  parent runtime directory already granted. Measured live on this host: the
  create path reproduced the identical refusal after deleting and recreating
  the key, blocking every worker launch. The create path now applies a
  protected, owner-only DACL (granting the token USER and token OWNER SIDs,
  so an elevation change never locks the same account out) before the key is
  ever readable, and a refusal for an existing key now names the specific
  reason instead of an unexplained dead end.
- Windows: OpenCode never appeared as a model-settings route, even when
  correctly installed, because executable resolution refused it outright on
  every Windows host before ever attempting `shutil.which` -- including when
  an administrator supplied an explicit executable override. OpenCode now
  resolves through the exact same path already trusted for `codex_cli` and
  the other Windows-supported adapters.

## [0.11.45] - 2026-09-16

### Fixed

- Windows: Claude Code could never hold the repository's manager seat. The
  verification read only the MCP server's direct parent process, but a Windows
  venv's `Scripts\python.exe` is a redirector that re-executes the base
  interpreter as a separate process, so the server's real parent was always
  that stub rather than `claude.exe`. Measured on this host: server pid 2652
  &lt;- venv-stub pid 38272 &lt;- pid 29612 (`claude.exe`, whose session descriptor
  validated cleanly). The check now walks the full ancestry through one native
  Toolhelp snapshot, skipping only this interpreter's own re-exec hop under the
  same user, and still requires one exact `claude` ancestor with a valid,
  repository-bound session descriptor.
- Windows: native-CLI sandboxing (AppContainer confinement for `claude_cli`,
  `codex_cli` and the other native adapters) reported
  `windows_appcontainer_sandbox_unavailable` on every capable Windows 11 host.
  `DeriveCapabilitySidsFromName` is a security-base export that `kernel32.dll`
  does not forward, and the probe was looking it up there; it is now resolved
  from `kernelbase.dll` (falling back through the documented API sets), where
  Windows actually publishes it.

## [0.11.44] - 2026-09-15

### Fixed

- Windows: a child process that inherited the MCP server's JSON-RPC stdin pipe
  hung before executing its own first instruction, so every `git` the
  coordinator ran burned its whole timeout. `git ls-files -z` inside
  `repository_tracked_paths` spent its full 120 s budget and
  `aiworkhub_task_create` looked like it had stalled, while the create path
  itself answers in 0.16 s. The server now detaches descriptor 0 to the null
  device at startup and keeps a private, non-inheritable reader for the
  protocol stream: task creation went from 120.08 s to 0.09 s. This also closes
  a platform-independent hazard, since a child holding the request pipe could
  consume JSON-RPC bytes addressed to the server.
- Windows: the toolchain authority recorded an empty version fact for every
  installed tool, because no secure sandbox lane exists there to probe through.
  A card declaring `node>=20.0.0` and `ruff>=0.12` was refused as
  `task_contract_unwinnable` on a host carrying Node v22.16.0 and Ruff 0.16.1.
  Version facts are now measured with a shell-free, path-bound, time-limited
  probe, and `python -m <validator>` reports the validator's version instead of
  the interpreter's.
- Windows: the terminal-authority HMAC key followed a symlink and skipped the
  owner check entirely, because `O_NOFOLLOW` does not exist there. The key is
  now refused when it is a reparse point, its identity is re-verified on the
  open descriptor, and its owner and DACL are read from the security
  descriptor, refusing any Everyone/Users/Authenticated Users grant.
- Windows: the reconciler discarded the heartbeat it had just written, because
  the POSIX `mode & 0o077` privacy test is always true against the synthetic
  `0o666` Windows reports. `durable_status_present` now reads true.
- Windows: every reviewer terminal-intent read failed, so no reservation could
  be terminalized and reviewer cards stayed in `processing` with nothing left
  to finish the transition.
- Windows: `python -m <validator>` was not recognised as a validator invocation
  at all, because the interpreter path was split on `/` only and the name
  pattern did not accept `python.exe`.
- Multi-repo binding on Windows: Node reports `lstat().dev` as 0 while
  `fstat().dev` carries the real volume serial, so every valid manifest was
  read as `manifest-unreadable` and no repository could bind.

## [0.11.43] - 2026-09-15

### Added

- The manager MCP surface now exposes `aiworkhub_manager_semantic_edit_prepare`
  and `aiworkhub_manager_semantic_edit_apply`, the same hash-bound range-edit
  tools workers use, and `aiworkhub_manager_bootstrap` reports whether semantic
  edit is available and whether the write gate is open -- so the manager can
  make small, verified range edits instead of a whole-file rewrite.
- A new read-only `aiworkhub_dashboard_skills` surface reports measured skill-
  selection coverage: totals by lifecycle, how many recent receipts selected
  and injected, and the consecutive run of newest receipts that injected
  nothing. An absent or unreadable skill store reports `measured: False` with a
  reason instead of a zero that reads as "healthy and empty."
- A new deterministic, read-only attempt-trajectory export composes a card's
  audit history, process lifecycle ledger, attempt artifacts and recorded
  usage into one canonical JSON document per `request_id`. Every field is
  either measured evidence or an explicit `UNKNOWN`; the accepted-outcome
  signal is only ever granted by the existing sealed acceptance authority,
  never self-declared.
- A fixed, checked-in four-profile external-repository qualification corpus
  (`llvm/llvm-project`, `microsoft/vscode`, `apache/airflow`, `grpc/grpc`,
  each pinned at a release tag's exact commit) and its manifest/run-artifact
  contracts are added as foundation only. Nothing in this change clones,
  builds or executes an external repository, and no performance, cost or
  token claim is established by it -- that stays `UNKNOWN` until a later
  execution phase produces receipt-backed artifacts.

### Fixed

- Source Graph's `calls` mode now resolves a query to the one entity that
  actually *defines* the named symbol before returning call edges, instead of
  matching every entity sharing that name, including imports, decorators and
  annotations. An imported function no longer reads as an ambiguous query, and
  an edge whose callee is recorded by name only is attributed to a definition
  solely when that name is unique across the repository.
- The Windows sandbox/AppContainer route report now names the exact measured
  cause native CLI execution was refused -- host AppContainer APIs
  unavailable, the execution path not wired to them, or the platform is not
  Windows -- from a closed, membership-checked vocabulary, instead of
  publishing one stable blocker code that discarded which of the three
  applied. This changes only the reported reason a route selection failed; it
  does not change, and does not claim, which Windows routes are launchable.
- Oversized Source Graph analytic-mode results returned by the worker AI-tools
  MCP Source Graph route are now spilled in full to a repository-scoped,
  content-addressed store (`.aiworkhub/spill/`) before the bounded preview a
  model sees is built. The truncated wrapper carries a `spill_locator` and
  retrieval hint so the original text stays retrievable and digest-verified
  instead of being discarded the moment it is trimmed. The store has no
  eviction, TTL or size cap yet; that remains out of scope.
- `failure_disposition` now also returns a typed cause/action/retry-scope
  projection derived from the same resolved evidence as its existing legacy
  `failure_class`/`evidence` fields, so the two can never disagree. When a
  bounded log tail names no cause, the classifier now also reads the reason
  or terminal state this repository itself recorded, instead of falling back
  to a blind relaunch.
- The outer validation authority document and the nested Landlock authority
  locator now receive their final file mode (owner-private, and read-only
  0o444 respectively) at creation time, via `O_CREAT|O_EXCL` with a pinned
  umask, instead of a separate chmod-after-write step that silently no-opped
  on `PermissionError`. This closes a window in which the nested locator's
  hardlinked, shared inode could be rewritten in place by the sandboxed
  validator whose own nesting authority that locator establishes.
- A duplicate manager launch request for a task already attached to a live
  worker now returns an idempotent `already_attached` observation of the
  existing claim instead of recording a new blocked-launch episode, which
  previously could overwrite the original worker's processing ownership and
  make its later successful finalization fail closed.
- Three AppContainer-identity launch-denial reasons (platform mismatch,
  invalid identity, repository identity unavailable) are now classified as
  transient and retryable rather than deterministic card defects, closing a
  release-consistency drift between two separate reads of the launch
  platform within the same call.
- The Plan-DAG summary MCP projection now bounds every sampled ID array and
  per-card collision map to 50 entries, with exact `total_count` and
  `truncated` metadata carried alongside each sample, instead of returning
  some of those fields unbounded; the collision-map sample is ordered
  colliding-cards-first so a late colliding card can never be hidden behind
  older collision-free rows.
- The VS Code Webview compact-counter formatter keeps its single-argument
  extraction seam self-contained by moving the locale-aware implementation
  below the pinned declaration line a test harness extracts verbatim. The
  four-significant-digit rendering behavior shipped in 0.11.42 is unchanged.

### Validation

- Sandbox nested-listener test coverage was tightened to stop asserting an
  impossible nested-stacking state, and the NF841 CI fixture no longer
  depends on the ambient umask.
- The release-metadata projection check is clean for tag v0.11.43, and the
  VSIX version gate and scratch-containment gate pass. Release assurance,
  the release evidence pack and VSIX packaging are verified from the
  canonical tree. This change does not push, tag, publish or install
  anything.

## [0.11.42] - 2026-09-15

### Fixed

- The Models view no longer loses whole providers, or routes the repository
  owner explicitly configured, when a bounded catalog read comes back short.
  Ingestion is bounded separately from the compact render bound, the rows that
  survive are chosen per provider only after every provider has been seen, and
  routes named in `.aiworkhub/config/models.json` are reserved under both the
  identity they were written with and the canonical policy identity the catalog
  row carries -- so a vendor-keyed OpenCode decision pins the row it was written
  for. Both bounds stop at a hard ceiling, and pins that do not fit past it are
  counted as refused rather than dropped in silence.
- Counts that a bounded source truncated upstream are no longer published as
  exact totals. The OpenCode producer's row cap and the editor bridge's model
  slice are read as evidence that an upstream bound already truncated the list,
  never to re-impose one, and the payload carries per-provider
  total/returned/truncated counts beside each source's ingestion loss. The
  Webview labels a row by the bound that produced it -- `declared, not
  discovered`, `configured, origin unknown past the host bound`, `configured,
  past the source bound` -- and names the host that cut the tail instead of
  attributing the editor's cap to OpenCode rows.
- Compact counters in the web dashboard and in the VS Code Webview keep four
  significant digits, so every integer in a decade stays distinct: 1000 renders
  as `1k` and 1001 as `1.001k` instead of collapsing to the same label. A
  mantissa that rounding carries to 1000 promotes its tier, so 999999999 reads
  as `1B` rather than a grouped `1,000M`, and the decimal separator follows the
  reader's locale through `navigator.language`.

### Changed

- The repository-local `.kilo/` directory is ignored, keeping local Kilo state
  out of the canonical tree.

### Validation

- The bounded Models payload and its Webview projection are covered by the
  dashboard MCP app and KPI dashboard regression suites added with the fix; the
  compact counters are covered by the dashboard and Webview counter-precision
  suites, which pin one deterministic locale rather than asserting against the
  host's.
- The release-metadata projection check is clean for tag v0.11.42, and the VSIX
  version gate and scratch-containment gate pass. Release assurance, the release
  evidence pack and VSIX packaging are verified from the canonical tree. This
  change does not push, tag, publish or install anything.

## [0.11.41] - 2026-09-14

### Fixed

- Windows native-CLI workers now actually launch inside the repo-scoped
  AppContainer profile and its kill-on-close Job Object. The launcher writes the
  execution backend together with the canonical `repo_id` and the normalized
  `worker_kind`, and refuses the launch before spawn when that identity cannot
  be established; the supervisor dispatches on that exact backend token and
  refuses any other spelling instead of falling through to a plain subprocess.
- The Windows confinement report now derives the boundary in force from the
  three facts it measures -- platform, host AppContainer APIs and launch-path
  wiring -- rather than returning a constant, so a host that does not qualify is
  still described as bounded by worker process-tree lifetime only.
- A Windows extension host resuming from idle no longer loses repository
  discovery to a single transient fault. The manifest read now takes at most one
  immediate retry, authorized only for a transient cause (Win32
  `ERROR_INVALID_HANDLE`, `ERROR_SHARING_VIOLATION`, `ERROR_LOCK_VIOLATION`, or
  POSIX `EINTR`), and that retry is a whole new attempt on a brand-new
  descriptor which repeats the symlink, regular-file and dev/ino identity
  checks. A missing manifest, invalid UTF-8/JSON, a non-object payload, a
  foreign repository and every identity or security rejection stay fail-closed
  on the first attempt; there is no sleep, no backoff and no second retry.
- Shared-router repository discovery now reads identity through that one
  validated `repository_state` manifest reader instead of a second local JSON
  parser, inheriting the same checks and the same single bounded recovery while
  still degrading to an empty id rather than raising.

### Validation

- The Windows AppContainer wiring is proved by deterministic fake-Windows
  behaviour tests that drive the real launcher and supervisor call path on
  Linux. Those seams cannot prove a Win32 syscall: NF-2026-00452 stays open
  until a real Windows read-only canary runs after this release, and this
  release claims no live-Windows execution evidence.
- The bounded manifest recovery is covered by repository-state and shared-router
  regressions asserting that exactly one retry is authorized, that the retry
  re-runs every identity check on a fresh descriptor, and that non-transient
  causes are never retried into acceptance.

## [0.11.40] - 2026-09-14

### Fixed

- Rejecting a validation-only replay now invalidates that episode's replay
  grant, so the next rework claim invokes a provider instead of rerunning the
  rejected candidate bytes indefinitely.
- Rejecting one parent now cancels only its bound reviewer children; foreign
  review tasks are skipped without being reported as finalized or terminated.

### Validation

- Rework/rejection lifecycle coverage passes with 121 tests, including a
  regression proving a rejected replay grant cannot survive into the successor
  claim. The reviewer-cleanup regression suite passes with 99 focused tests.

## [0.11.39] - 2026-09-14

### Fixed

- Review lifecycle reservation now advances its persistent pending cursor only
  through the action actually selected. Returning a deferred head to pending no
  longer wraps the cursor immediately and starves later ready review chains.
- An exhausted pending round performs at most one bounded rollover scan, so
  blocked descendants do not regress the existing one-call progress guarantee.

### Validation

- The review lifecycle, orchestrator, replay, task-store, single-writer and
  reconciler suites pass with 274 tests. A regression test proves a deferred
  first chain cannot prevent a later ready chain from being reserved.

## [0.11.38] - 2026-09-14

### Fixed

- Automatic quality-review recovery now drains a bounded batch of reservable
  lifecycle actions on every reconciler pass. A deferred first action can no
  longer hold the remaining review queue behind one multi-minute scan.
- Task MCP writes share one serialized writer boundary and reusable lock
  descriptor, reducing SQLite writer contention and lock churn.
- Validation-only replay preserves workspace/toolchain authority, while
  disposed reviewer processes and callback schema initialization are handled
  deterministically instead of creating repeated mechanical failures.
- Foreign reviewer children no longer generate disposition-event floods, and
  replayable review progress is compacted before it reaches model context.

### Validation

- The reconciler, liveness and review-orchestrator suite passes with 191 tests.
  A production-shaped 35-action backlog proves a newly seeded target is reached
  in six bounded passes without duplicate execution.

## [0.11.36] - 2026-09-13

### Fixed

- Validation now checks executable authority receipts with the same bounded
  fingerprint primitive that creates them. Real binaries larger than 1 MiB no
  longer fail provider-free replay before the first declared validation command.

### Validation

- A production-shaped executable larger than 1 MiB proves receipt creation and
  validation share one identity contract. The authority, validation and replay
  suite passes with 565 tests and 2 skips.

## [0.11.35] - 2026-09-13

### Fixed

- Toolchain authority now verifies executable identities before reusing a
  snapshot loaded from the durable cache. A stale or cross-namespace snapshot
  is re-derived instead of being signed into a validation request that must
  immediately fail with `validation_toolchain_authority_executable_identity_drift`.

### Validation

- Regression coverage replaces a persisted executable and proves that a new
  authority instance rejects the stale disk snapshot and measures the current
  toolchain. The full Python suite passes before packaging.

## [0.11.34] - 2026-09-13

### Fixed

- Provider-free validation replay now carries the full `read_first` contract
  into its isolated request, preserving the HMAC-bound toolchain cache
  identity before declared validations run.

### Validation

- Regression coverage uses a non-empty `read_first` contract and verifies the
  replay request against the original toolchain authority receipt.

## [0.11.33] - 2026-09-13

### Fixed

- Repeated provider-free validation replay now preserves authenticated worker
  MCP evidence across a mechanically failed replay instead of stopping with
  `validation_only_replay_predecessor_worker_mcp_gate_missing`.

### Security

- Inherited replay evidence is accepted only when the coordinator-owned
  request packet matches the exact task, request, repository, claim epoch,
  predecessor and retained path hashes; mismatches continue to fail closed.

### Validation

- Regression coverage proves both successful two-hop inheritance and rejection
  of altered provider-launch, request-identity and claim-epoch fields.

## [0.11.32] - 2026-09-13

### Fixed

- Authenticated validation-only replay now accepts an exact retained candidate
  whose bytes differ from the current canonical parent, while continuing to
  bind the replay to task, actor, predecessor request, claim epoch, path and
  SHA-256.

### Validation

- Regression coverage exercises a retained predecessor delta against a newer
  parent and proves that the same unchanged delta still fails closed without
  the one-episode replay authorization.

## [0.11.31] - 2026-09-13

### Fixed

- Provider-free validation-only replay now preserves the complete signed task
  card identity in finalization metadata, so retained candidates can rerun
  their declared gates without weakening cross-request receipt protection.

### Validation

- Regression coverage verifies the replay metadata against the canonical
  HMAC-bound toolchain receipt; the affected launcher, blocked-rework and
  executable-resolution suites pass before packaging.

## [0.11.30] - 2026-09-13

### Fixed

- Reconciliation now materializes missing automatic-review children for sealed
  review-ready candidates and recovers zero-child review chains without manual
  reviewer launches.
- Mechanical review parks, blocked-review learning identities, retained-delta
  reroutes, and pending launch failures now preserve their authoritative
  lifecycle and failure classification across retries.
- Read-only analysis and research complete without code-validation or reviewer
  requirements that cannot add assurance to a mutation-free result.
- OpenCode workers receive isolated authentication, support classic Snap under
  Landlock, and seal capacity refusals as provider evidence instead of leaving
  ambiguous failed attempts.
- VS Code LM finalization preserves valid partial finals while continuing to
  reject contradictory or unauthenticated terminal evidence.

### Validation

- The release activates the already-reviewed zero-child recovery and related
  mechanical-failure regressions now present in the canonical tree. Python,
  extension, release-metadata, package and fresh-install smoke gates are run
  before tagging.

## [0.11.29] - 2026-09-13

### Added

- Repository Settings now projects discovered OpenCode identities into a
  bounded, collapsible provider-to-model tree. Exact model children remain
  distinct from installation, policy enablement, launchability, access, and
  observed round-trip evidence.

### Fixed

- Settings reuses the workforce catalog's cached environment-preflight
  snapshot instead of spawning a second `opencode models` probe during the
  same refresh.
- Automatic VS Code LM quality reviewers now receive one unambiguous terminal
  contract: submit the authenticated report exactly once through the
  request-bound review tool, eliminating the prior printed-JSON/tool-call
  contradiction.
- The reconciler now reconstructs missing automatic-review chains for sealed
  `review_ready` candidates, while permanently stale manager-ready projections
  are quarantined instead of starving newer review work.
- Worker timeout is a monotonic hard wall on every execution backend; output,
  heartbeat, progress and usage events cannot extend it.
- Validation retains trusted pytest runtime roots and explicit sandboxed
  project imports, accepts only verified no-op metadata requests on hardlinks,
  and recognizes candidate bytes that are already canonical.
- Required-output validation counts an inherited rework file only when its
  request identity and digest match an authenticated sealed predecessor.

### Validation

- OpenCode model projection and tree rendering are covered by focused Python
  dashboard/catalog tests and Node Webview tests. The complete release passed
  10,357 Python tests and the full 50-file extension suite, plus Ruff, release
  metadata and diff checks.

## [0.11.28] - 2026-09-12

### Fixed

- Manager acceptance now retains authenticated automatic-review receipts after
  their reviewer cards are archived, so a completed correctness/security chain
  remains visible to the server-bound reviewer census and does not trigger
  duplicate reviewer work.

### Validation

- Regression coverage exercises archived reviewer enumeration through the
  production accept-preview path, alongside the existing authenticated receipt
  verification suite.

## [0.11.27] - 2026-09-12

### Fixed

- Automatic quality review can retry an alternate eligible reviewer route
  after a mechanical route failure, while preserving the manager as the sole
  authority for accepting or returning the implementation target.
- Review reconciliation recovers authenticated chains that an older runtime
  terminalized only because no reviewer route was available, without treating
  transient route availability as a verdict on candidate code.
- OpenCode JSON/SSE terminal and usage events now distinguish top-level
  completion from child-session activity and deduplicate cumulative token,
  cache, and cost evidence.

### Added

- A fail-closed OpenCode CLI runtime foundation: exact `provider/model`
  identities, Linux executable resolution, native JSON command construction,
  and a request-local permission contract that denies built-in tools by
  default and allows only the bounded AIWorkHub worker MCP surface.

### Limitations

- OpenCode is staged but is not yet workforce-eligible or selectable in the
  dashboard. Model discovery, task-route wiring, UI settings, and a live
  end-to-end canary remain required before production use.
- Cross-process SQLite single-writer ownership remains open; this release does
  not claim that all `database is locked` paths are eliminated.

### Validation

- Release qualification covers the full Python and VS Code extension suites,
  Ruff, metadata consistency, VSIX packaging, and packaged-runtime smoke tests.

## [0.11.26] - 2026-09-11

### Fixed

- `record_launch_blocker` tolerates an unready/absent storage manifest instead
  of letting `StorageNotReadyError` mask the real launch-rejection reason,
  restoring 6 tests broken by the prior release's storage fail-closed change.
- Workforce catalog rows carry explicit `manager`, `implementation_worker`,
  and `reviewer` booleans with tested safe defaults for legacy rows; `codex`
  and `codex_gpt*` routes are always forced manager-only regardless of
  declared values.
- The AppContainer-supervisor-identity launch seam and its `sys.platform`
  dependency are now declared in both the launch-isolation seam registry and
  the OS-dependency boundary baseline, closing a gap left by the prior
  release.

### Added

- Research: a provenance-pinned Ponytail adoption contract extending the
  universal-development-skills artifact (upstream `DietrichGebert/ponytail`,
  MIT), ranking the minimal-solution ladder and related concepts against the
  existing skill registry, recipes, and semantic-edit evidence.

### Known issues

- Four pre-existing regressions remain open and are not fixed in this
  release: automatic quality-review launch failing to complete the
  correctness/security chain for two related cards, a toolchain-cache
  finalization receipt check, MCP stdio server write-gate visibility with no
  `ALLOW_WRITES` set, and `server.main()`'s Source Graph bootstrap ordering.
  Tracked as NF-2026-00796 (clusters C/D/G) and NF-2026-00798 (recovery
  lineage fail-closed gap).

### Validation

- Full suite: 10,188 passed, 7 failed (the four pre-existing issues above),
  45 skipped.

## [0.11.25] - 2026-09-10

### Fixed

- Candidate and quality-review finalization no longer wake the manager before
  the system-owned correctness, security, and code-quality chain completes.
- The completed chain seals an authenticated manager-ready aggregate bound to
  the exact candidate, claim, packet, reviewer requests, reports, receipts,
  and submissions, then atomically emits exactly one manager callback.
- Manager-owned acceptance, rejection, archival, and linked-NeedFix closure
  remain outside review automation without head-of-line blocking later chains.
- Persisted template provenance is bound before required-output validation, so
  launch-time expansion cannot be mistaken for an unclassified task contract.
- Historical `target_accept` receipts remain readable but cannot repopulate the
  new manager queue or block review-orchestrator startup.

### Validation

- The complete Python suite passes: 10,200 passed and 44 skipped.

## [0.11.24] - 2026-09-10

### Fixed

- Windows Source Graph refresh now clears a retained build identity only when
  the non-signalling PID probe definitively proves that process absent; a live,
  recycled, malformed, or unprovable identity remains fenced.
- Daemon shutdown rewrites a retained identity only for its exact locally owned
  process handle, so reload cannot turn a foreign owner into a permanent
  `build_start_fenced` state.
- Source Graph health reports stopped writers as stopped, and code preflight no
  longer treats an old readable generation as ready when refresh is stopped,
  degraded, stale, fenced, or its latest refresh job failed.

### Validation

- Windows lifecycle simulations and the focused Source Graph/preflight suite
  pass on Linux. Owner-machine Windows live refresh qualification remains open.

## [0.11.23] - 2026-09-10

### Fixed

- Automatic quality-review orchestration no longer waits on a Source Graph
  partition receipt that can only be created by the reviewer launch itself;
  launch-owned prewarm still fails closed before provider execution.
- Dashboard foundation telemetry stays compact and opens its full Skills,
  Tool Recipes, and Semantic Edit evidence in an accessible popup.

### Added

- A source-audited design for universal, provider-neutral development skills
  and their future A/B evaluation is documented.

### Limitations

- Manager-ready notification still needs to be delayed until the automatic
  reviewer chain has completed (NF769).
- Source Graph durable single-owner (NF761) and the secure native
  SemLock-capable validation lane (NF690) remain open.

## [0.11.22] - 2026-09-10

### Fixed

- Dashboard full-snapshot hydration now includes Skills, Tool Recipes, and
  Semantic Edit.
- Canonical `task_queue.sqlite` mutation paths now take a single
  cross-process writer lease while concurrent readonly access remains
  available.
- SemLock preflight denial now emits exact per-command validation receipt
  cardinality.

### Limitations

- Source Graph durable single-owner (NF761) remains open.
- The secure native SemLock-capable validation lane (NF690) remains open.

## [0.11.21] - 2026-09-10

### Fixed

- Authenticated parse-broken repair launch now accepts request-scoped
  overlay evidence at prefetch, hides stale canonical symbols, and fails
  closed on identity, hash, or scope mismatch.
- Dashboard Tool Recipes, Skills, and Semantic Edit telemetry classify
  unavailable evidence separately from a measured zero (NF722).
- Compatibility repair (NF757).
- Release CI provenance requires a completed successful push run for the
  exact tag commit.

## [0.11.20] - 2026-09-10

### Fixed

- Source Graph `deadmethods` now reports exact entrypoint truth (NF568).
- Nested quality-review findings now normalize to the canonical finding
  schema (NF747).
- Quality review now delivers a single reviewer packet (NF748).

## [0.11.19] - 2026-09-09

### Fixed

- Worker, review and rework stages now preserve the authenticated request
  identity so later stages stay on the same request.
- Authenticated parse-broken rework prefetch (NF736) loads the retained
  candidate instead of asking the worker to rediscover it.
- Bounded missing-create finalization (NF737) stops after one exact
  correction when the same required create is still missing.
- VSIX packaging validation stays scratch-contained (NF745) and does not
  write outside the bounded workspace.
- The Marketplace landing page is restored as a complete page with the
  repository screenshot and architecture assets.
- Root generated `data/` JSONL hygiene (NF573) classifies root `data/` as
  artifacts and skips root `data/*.jsonl` in untargeted bodygrep before
  content/result-budget consumption, while targeted queries and nested
  package data remain available.

## [0.11.18] - 2026-09-09

### Fixed

- Quality-review launch now reconciles the canonical
  `review/review/review_ready` card state across both admission and background
  packet preparation. Later state-less orchestration events no longer reject a
  valid target before provider start, while a target that has left review still
  fails closed.

## [0.11.17] - 2026-09-09

### Fixed

- Editor-hosted finalization now stops after the same missing required-create
  rejection repeats, returning the typed `vscode_lm_finalization_nonprogress`
  failure instead of spending additional provider turns. A changed rejected
  path still receives its own exact `v3_create` correction and may complete.

## [0.11.16] - 2026-09-09

### Fixed

- Module-form validation now accepts the exact interpreter already running
  AIWorkHub when a hosted toolcache exposes that same endpoint as
  world-writable. Arbitrary world-writable executables remain refused, and
  failed validation rows now retain the resolver reason for CI diagnosis.

## [0.11.15] - 2026-09-09

### Fixed

- Code-worker launches that reach their timeout-derived zero-required-output
  deadline now cancel through the canonical lifecycle with a distinct reason,
  instead of emitting a warning and burning the rest of a long provider run.
  Read-only cards, explicit unchanged-output contracts and real write deltas
  remain exempt.

## [0.11.14] - 2026-09-09

### Fixed

- Retained rework now distinguishes worker-editable residual artifacts from
  out-of-scope system prerequisites. Prerequisites remain in the sealed audit
  packet, while an actual out-of-scope predecessor change still fails closed.

## [0.11.13] - 2026-09-09

### Fixed

- Editor-hosted quality reviewers can submit durable verdicts through the
  bridge, and route capability truth now follows the dispatch surface instead
  of leaving `reviewer_submit` unknown.
- Validation preflights Python multiprocessing semaphore support inside the
  actual sandbox and reports a stable unsupported capability when the host
  denies it, instead of repeatedly failing valid candidates with `PermissionError`.
- Kilo/Grok `step-finish` usage now records nested token/cache counters and
  provider-reported cost. Token counters retain snapshot/max semantics while
  each distinct direct `part.cost` event is accumulated exactly once.
- Worker-policy tests preserve Claude's one-shot deferred-schema instruction
  without adding those prompt bytes to Codex, Grok or other transports.

## [0.11.12] - 2026-09-09

### Fixed

- Workforce telemetry now separates route startability, access probes,
  historical evidence, current round-trip observation and provider outcome;
  installation alone can no longer read as a successful live execution.
- A real terminal provider failure now proves that a route was observed without
  falsely reporting the route as available or successful.
- VS Code LM workers keep semantic staging open until every immutable required
  output is staged, name the exact next path/action, and terminate repeated
  refusal with a bounded stage-specific error instead of a broad turn limit.
- Complete staged edit/create envelopes finalize offline without an extra
  provider turn on both text and native tool-calling routes.

## [0.11.11] - 2026-09-09

### Fixed

- Generated Codex worker configuration now declares the exact enabled MCP tool
  set instead of allowing the host to expose only `exit_preflight`.
- Code-worker launch fails closed unless Source Graph and both semantic-edit
  operations are enabled; reviewer-bound Codex runs retain their review tools.
- Claude's deferred-schema `ToolSearch` instruction is now rendered only for
  the Claude CLI and is never sent to Codex or other transports.

## [0.11.10] - 2026-09-09

### Fixed

- Pending retries can be rerouted through a workforce-catalog route even when the
  task carries a retained rework delta; the sealed candidate remains preserved.
- Worker-side validation now resolves bare `python` and `python -m` commands to
  the same trusted canonical interpreter as finalization, including isolated
  `-P -m` module execution.

## [0.11.9] - 2026-09-09

### Added

- The instruction the models actually receive now names the AIWorkHub tool for
  every surface it forbids. The policy forbade raw search and a whole-file
  rewrite in prose and never said what to use instead, so a model with no named
  substitute reached for the next available thing. The worker runtime policy
  carries a derived substitution table -- raw search to Source Graph, a whole
  file rewrite to semantic edit prepare/apply, a retyped validation command to
  the bounded validation runner, an unbounded read to one Source Graph preview,
  and calling your own work finished to the exit rehearsal. Both sides are
  derived from the tuples that define them, so a renamed tool cannot leave a
  dangling instruction behind.
- A worker can declare a semantic-edit exception and have it recorded.
  `aiworkhub_worker_semantic_edit_exception_declare` writes the exception, the
  path and the reason into the HMAC-authenticated audit ledger, and the
  coverage record moves that path out of `undeclared_raw_only`. The policy
  always had three legitimate exceptions -- a new file, a change spanning most
  of a file, an adapter without the tools -- and until now taking one was
  indistinguishable from ignoring the rule.
- Semantic edit coverage is measured per attempt and attached to the terminal
  event: which changed paths were reached by an apply, which were raw only,
  which exceptions were declared or derived, and five named reasons a run could
  not be measured rather than a false zero. It is measurement, not a gate; a
  test asserts no acceptance module reads it.
- The manager seat has the same semantic-edit pair over MCP, with its own
  audit ledger, so a manager correction is recorded the way a worker's is.
- A relaunch that cannot produce a different outcome is refused, and the
  refusal names the two legal moves: reroute the launch identity, or authorize
  the repeat with a reason.
- A reviewer receives the findings from earlier rounds on the same task, with
  line numbers carried only where the cited file is byte-identical and withheld
  where it is not; a stale line number is worse than none.

### Fixed

- Six of the nine supported adapters were told their tools were
  "provider-blocked" when nothing blocked them. Three of those six were told it
  while their tool surface refuses more completely than any flag: AIWorkHub is
  the tool server for the in-process bridge, and the twenty tools it offers are
  every one `aiworkhub_*`, with no raw search and no raw editor among them. The
  notice now renders only for the three transports that genuinely have no
  launch-time lever, and enforcement is read from both mechanisms rather than
  from the argv flag alone.
- The validation line claimed pytest, ruff and mypy were provider-blocked. On
  six adapters nothing blocked them, and on the three that do, prefix matching
  means `Bash(pytest *)` never matches `<python> -m pytest`, the spelling this
  repository actually uses. It now states the reason that is true everywhere: a
  hand-typed run is unreceipted, so it does not count.
- A build worker is denied the raw editor at launch. `Edit` sat on the granted
  tool list beside semantic edit prepare/apply and was denied nowhere, which is
  why 1,500 of 2,648 verified attempts that changed a file made zero semantic
  applies. Measured before the change: of 547 `Edit` calls, 523 hit a file the
  run never prepared at all -- the semantic path was not weighed and rejected,
  it was never entered. `Write` is kept, because 82% of its use authors a new
  file and no tool substitutes for that. The deny is launch-only and never
  reaches the repository's tracked `.claude/settings.json`.
- Eleven MCP contract and smoke gates had never run: pytest collects `test_*.py`
  and they are named `mcp_*.py`. Eight now run under a driver that also fails if
  a new gate file is neither driven nor declared unrun with a reason. The
  contract drift they had accumulated was not an SDK change but this project's
  own `geoai_task_*` to `aiworkhub_task_*` rename, which FastMCP writes into
  every schema title; substituting the old prefix reproduces the old fingerprint
  byte-exactly.
- An accept or a reject now writes the decision, the changed paths with their
  hashes and the review-feedback digest into the session store, so a rework
  worker's injected context carries its predecessor's decision instead of
  nothing.
- A citation that named a line range in a twelve-thousand-line file moved three
  times in one day on unrelated edits. It names the function now, and the test
  refuses a line-range citation outright.
## [0.11.8] - 2026-09-08

### Fixed

- The validation sandbox's seccomp filter could not be installed on any
  position-independent interpreter, which is every distribution build and every
  `actions/setup-python` runtime. `seccomp_rule_add` was bound without
  `argtypes`, so the filter context -- a pointer that `seccomp_init` returns as
  a Python int -- was converted to a C `int` and silently truncated to its low
  32 bits. On a non-PIE interpreter the heap sits below 4 GiB and the
  truncation is invisible, which is why it passed here for months; on a PIE
  interpreter libseccomp dereferenced a wild pointer and the process died of
  SIGSEGV with no output, taking the whole metadata filter with it. The
  boundary never widened: the wrapper crashed rather than allowing anything.
- The two existing end-to-end broker tests skip when the capability probe
  reports "unsupported" -- and this defect is what made that probe report
  unsupported, so they had been silently skipping on every CI run. The
  timestamp broker test now measures which filter the host actually installed
  and asserts that path: brokered means the timestamps are applied, a landlock
  filter without user notification means the syscall is refused with EPERM. It
  no longer skips.

## [0.11.7] - 2026-09-08

### Fixed

- Manager bootstrap started a daemon thread on every call to run task hygiene
  off the request path. Bootstrap is also the route gate for every manager
  tool, so a long-lived manager process was almost never single-threaded --
  and the validation sandbox's metadata broker forks. A fork from a
  multi-threaded process killed the broker's child on SIGSEGV with no output
  on all three CI Python versions, while passing on a 16-core developer
  machine. Hygiene now runs on the caller's thread and only when someone
  offers: the bootstrap tool does, the route gate does not, and the
  reconciler's GC pass owns the repositories nobody bootstraps. The measured
  saving stands, because it was the ~470 gate calls per session and not the 53
  bootstraps that were paying 2.77s each.

## [0.11.6] - 2026-09-08

A token-burn audit measured where the models actually spend context, and this
release moves the mechanical half of that work into AIWorkHub. The measurements
are from 6,587 ledger attempt records (6.06B input tokens), 780 worker runs,
1,141 reviewer runs and 27 manager sessions.

The audit's first finding was that the received wisdom was wrong: strict
ceremony is 0.1% of worker tool-result bytes and the worker prompt is 10 KB,
6% of its cap. The burn is the tool loop -- validation output 43% of bytes,
discovery 39% -- and every relay turn re-reads a context that grows from 36K to
139K tokens. So this release optimizes turns and reply shape, not prompt text.

### Added

- `aiworkhub_manager_recipe_run` executes a registered tool recipe and persists
  a receipt, and `aiworkhub_manager_recipe_usage` reports per recipe: runs,
  distinct actors, last run and exit distribution. The receipt's actor is
  derived from the verified manager route; no tool exposes a parameter that
  could name one.
- `aiworkhub.recipes` ships seven operator recipes as package modules --
  task events, usage rollup, request log tail, attempt validation, worktree
  diff, process liveness, repo test subset -- replacing the ad-hoc Python
  heredocs that were 48% of manager Bash calls.
- `aiworkhub_agent_accept_preview` returns exactly what would block an accept
  before any combined tree is materialized. 49 of 159 measured accept attempts
  failed on a blocker that was knowable in advance, after two validation runs
  had been paid for.
- `aiworkhub_manager_skill_usage` reports per skill: proposals, evidence by
  outcome, distinct actors and the exact reason a skill is not injectable.
- Skill evidence is now produced by the decision itself, one row per skill the
  card received, with the actor derived from the card's runner. Across 3,383
  recorded decisions there were 0 evidence rows, so no skill could ever reach
  the two distinct actors activation requires.

### Changed

- `manager_bootstrap` returns the identity block on every call and the contract
  prose once per verified session: 9,372 B to 1,699 B on the second call. It
  also folds in `repository_current` and `task_health`, so the start sequence is
  one call, and hygiene runs off the request path.
- Mutation tools answer with receipts instead of echoes. `reject_review` was
  17.8 KB of which ~80% was the manager's own reason, unchanged card fields and
  hash baselines; `task_create` echoed 85% of its own input.
- Source Graph fits an oversized focus reply to the cap in one plain-JSON page
  instead of paging it as base64 that no caller decoded, folds the duplicate
  `ranked_symbols`/`hot_symbols` lists into the match rows, and infers
  `workflow_stage` from the ledger.
- `semantic_edit_prepare` returns a hash-only receipt for a range this server
  already delivered. 83% of prepares were never applied, carrying 24.9 MB of
  text the model already held.
- The reviewer packet carries only the lens being reviewed, so it fits inline
  instead of forcing a tool round trip; it now also carries the complete diff
  hunks and the validation output tails the prompt already promised.
- The tool-use policy makes the semantic editor mandatory for changing an
  existing file, with the exceptions named so the rule is followable.
- `launch` derives runner, topic and adapter from the card; `accept_review`
  derives its reviewer ids and risk tier.
- Card creation reports test-scope gaps: 225 of 1,624 writable cards name a
  test in a validation command they cannot write, and 548 leave an existing
  same-stem test outside scope.

### Fixed

- The rework failure delta never reached a rework worker: the crash-retry
  packet was gated on a non-zero exit and every validation_failed predecessor
  exits 0. 999 reworks re-discovered a failure the finalizer had measured, and
  66% failed validation again.
- The automatic review driver was dead. It read the target identity from card
  keys no card carries, so 0 of 627 chains resolved and 504 launch actions
  failed on identity; the manager launched 570 reviewers by hand.
- A reviewer receipt's packet digest was compared against the chain's
  attempt-artifact manifest digest -- two digests of different objects, equal in
  0 of 103 real comparisons, so the branch could only ever raise.
- Usage records dropped `topic`, leaving 1.92B input tokens (31.6%)
  unattributed although the launcher computed it and the card stores it.
- Backfilled usage rows were stamped with the backfill instant, so the day
  buckets and the retry ordering were wrong for a quarter of all records.
- The injected worker orientation was empty in 680 of 680 bundles: the hit
  counter counted the echoed query tokens as hits, so the emptiness check could
  never fire.
- Stale pending callbacks fenced task hygiene until an optional manager call
  happened to prune them; the reconciler's GC pass now owns it.
- Read-only reviewers inherited the build worker's Grep/Glob deny from the
  repository settings, so 25% of their Bash calls were refused and they
  substituted subagents and whole-file reads.
- The operator recipe scripts were never tracked by git, so they were
  unrunnable in CI, in a fresh clone and in every worker worktree.
- A read-only queue connection built its SQLite URI by string interpolation, so
  a repository path containing `#` opened a different file read-write.

### Fixed (release plumbing found while shipping this)

- Release qualification installed pytest without pytest-xdist while the
  project's addopts pin `-n auto --dist loadfile`, so every platform job
  exited in under 25 seconds on `unrecognized arguments: -n --dist` without
  collecting a test. It had been failing that way on the previous tag too, so
  no release had actually qualified.
- The per-project seeding tests asserted the toolchain of the machine that ran
  them. Seeding requires a project to declare a tool AND the host to have it,
  so a test that declared ruff and then asked the machine whether ruff exists
  was testing the machine. The decision is now exercised against stated
  evidence, verified in a virtualenv built to match the CI job and under a
  simulation where nothing resolves at all.

## [0.11.5] - 2026-09-08

### Fixed

- Read-only research and quality-review tasks now bind the required outcome
  receipt when accepted, so successful verification can finish the task.
  Missing or mismatched receipts remain rejected.

## [0.11.4] - 2026-09-08

### Fixed

- Validation commands can update timestamps on their authenticated scratch files
  through the metadata broker, including Python and Node file-descriptor calls.
  File ownership and path restrictions remain enforced.
- Windows job and file metadata structures now share canonical declarations
  across process supervision, file operations and temporary-file handling.
- Successful task rework preserves the authenticated candidate across review
  rejection and verifies retained artifacts before restoring the same attempt.

## [0.11.3] - 2026-09-07

### Fixed

- The correction record is mined into skill candidates. 669 statements over
  585 cards cluster by RULE rather than by file -- the miner strips backticked
  code, quoted literals, paths, identifiers, card ids, digits and the card's
  own write set before any similarity is computed, so what survives is what a
  statement asserts rather than what it is about. A candidate needs three
  distinct cards in three distinct files; 596 single incidents are refused, and
  the real record yields five. The learning ledger's 49 commits come from only
  28 cards -- one card wrote seven differently-worded invariants in an
  afternoon -- so counting statements would have manufactured seven
  confirmations from one incident.
- A skill declared at a lower risk tier now applies upward. It matched exactly,
  so a rule written for medium never reached the high-risk card that needed it
  more. The relation is deliberately asymmetric: a critical-only precaution is
  not owed by low-risk work.
- The path cards are actually created on can declare the skill vocabulary.
  `core.create_task` had carried all five selection dimensions for some time
  and neither MCP surface exposed them, which is why 0 of 4,628 stored cards
  carry them. `skill_task_family` now defaults to the family the card's own
  template declares.
- A transient provider hiccup, an expired credential and a real defect stop
  dying the same way. Nothing is classified from prose: each class rests on a
  typed field, a status the provider returned, or a refusal kind the boundary
  already establishes. Transient returns the card to pending with its workspace
  intact; credential never sweeps the workspace, which is what was discarding
  finished work when a credential expired at the end of a run; defect is the
  only class that earns `blocked` and is never inferred from an exit code.
  Everything else stays unknown and behaves as before.
- A route is extinguished by its outcomes rather than its registration. The
  failure circuit was computed once per catalog row, so a route with no row
  belonged to no circuit and its counter stayed at zero through 105 launches of
  a model the account cannot use. Replayed against the real sequence, those 105
  launches become 1.
- A blocked transition without a reason is closed. 14 of 15 reasonless blocked
  cards were manager rejections that HAD a reason -- it went into the review
  feedback and the event payload while the field an operator reads stayed
  empty.
- An unattended retry asks the disposition instead of re-deriving it, and
  refuses a class nobody reasoned about rather than treating silence as
  permission.
- `_launch_isolated` is extracted: 13,580 lines to 12,572, 1,043 moved with 8
  altered, and all 69 injection seams re-bound from the live module at call
  time. Freezing one of them turns five tests red, which is the silent failure
  the guard exists for.
- The dashboard's coding-foundation cards say loading before they say nothing.
  They asserted "No sample / No evidence" at first paint while the default
  snapshot had simply not sent the field yet.
