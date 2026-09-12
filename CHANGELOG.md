# Changelog

All notable changes to AIWorkHub are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project has
noted by package/extension version and release tag.

## [Unreleased]

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
