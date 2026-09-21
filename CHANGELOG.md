# Changelog

All notable changes to AIWorkHub are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project has
noted by package/extension version and release tag.

## [Unreleased]

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
