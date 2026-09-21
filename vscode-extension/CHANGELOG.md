# AIWorkHub for VS Code — Changelog

## 0.11.55 — 2026-09-21

### Added

- Bundled runtime: Roadmap list and detail views carry a server-side
  `current_wave` projection: the one in-progress wave with declared goals and
  the highest target version, with each goal checked only when all of its exact
  tasks are finished. Truncated, ambiguous, unversioned, goal-less or malformed
  Roadmap evidence yields a typed `UNKNOWN` with its reason instead of a guess,
  and a wave whose target the installed version has passed while goals are
  still unchecked is flagged overdue.
- Bundled runtime: `aiworkhub_task_create` and
  `aiworkhub_task_create_from_template` accept an optional exact
  `wave_goal_binding` (`roadmap_id`, `goal_id`, `predecessor_task_id`) that
  makes the new card that goal's current task in place of its predecessor. The
  binding is stored on the card, refused up front when it cannot apply, applied
  to the Roadmap once, and repaired by the reconciler when writes are enabled
  and the Roadmap write was interrupted. It is never inferred from a title,
  topic, version suffix or prose.
- Bundled runtime: the reconciler completes an in-progress wave only when every
  numbered acceptance criterion is mapped to a goal and every exact current
  task of every goal is canonically accepted with its own verifier receipt.
  Pending work leaves the wave in progress; missing, archived, blocked,
  superseded or unverified evidence yields a typed `unknown`. The single
  transition is write-gated, refused if the wave's goals or criteria changed
  after the verdict, records the accepted receipts, and never reads an
  installed or released version.

### Changed

- The wave mini-roadmap popup takes its wave, target and per-goal verdict from
  the bundled runtime's `current_wave` projection instead of ranking Roadmap
  rows itself. It shows the installed version and the wave's target separately
  (marking a passed target overdue), never checks a goal the runtime did not,
  and never counts an archived or stale task row as completion.

### Fixed

- Bundled runtime: worker validation sandboxes now seed a repository file that
  a declared pytest module locates by a literal `Path(__file__)`-relative path
  without importing it, plus a JavaScript asset's tracked local `require`
  targets, so such a test no longer fails on a missing asset in a sparse
  worktree (NF-2026-00551). Only git-tracked, non-dot-prefixed files inside the
  repository are seeded, as validation support rather than allowed writes:
  private state and untracked files stay out, a path that escapes the
  repository or crosses a link fails closed, and paths no filesystem can hold
  are declined instead of raising an untyped OS error.

### Not in this release

- Inferred successor progression (a successor takes a predecessor's place in a
  wave goal only through an exact binding that a task declares), the full
  stage-gated Playbook, LSP index integration, causal reasoning-quality
  measurement, and Muse/OpenCode worker qualification remain incomplete.

## 0.11.54 — 2026-09-21

### Fixed

- The bundled Source Graph reader stays in standby when another authenticated
  MCP process owns the index build; a fresh canonical index no longer appears
  degraded solely from normal builder contention (NF-2026-00933).

### Not in this release

- Automatic mini-roadmap progression, full Playbook stage gates, LSP index
  integration, causal reasoning-quality measurement, and Muse/OpenCode worker
  qualification remain incomplete.

## 0.11.53 — 2026-09-21

### Added

- Bundled worker-attempt receipts persist the selected reasoning option and
  reported model context capacity, without claiming provider-internal effort.

### Fixed

- Bundled semantic-review prompts request hash-matched candidate overlays for
  omitted hunks in truncated packets and still fail closed on missing or stale
  overlay evidence (NF-2026-00931).
- Bundled task creation checks required outputs; terminal events distinguish
  validation failure from provider timeout and record typed rate limits.
- Dashboard snapshot quarantine detail is bounded.

### Not in this release

- Full Playbook stage gates, LSP index integration, causal reasoning-quality
  measurement, and Muse/OpenCode worker qualification remain incomplete.

## 0.11.52 — 2026-09-20

### Added

- The wave mini-roadmap now shows the current wave's goals as a live checklist
  joined to each goal's task states, rather than a static list.
- Bundled runtime: semantic review scope is bounded to a candidate's exact
  changed segments, leading with the authenticated changed hunks and only the
  graph-connected callers and tests in the scoped audit, and failing closed on
  missing or stale changed-segment evidence.
- Bundled runtime: Source Graph ships a bounded LSP transport with fail-closed
  definition classification over a private workspace. LSP index integration is
  not included.

### Fixed

- Bundled runtime: blocked-rework recovery lets a strictly later terminal
  failure supersede a stale retained predecessor, re-deriving the predecessor
  from the failure's sealed delta (NF-2026-00515).

### Not in this release

- LSP index integration, the full stage-gated Playbook, reasoning matched to
  an accepted outcome, and Muse/OpenCode worker qualification remain
  incomplete.

## 0.11.51 — 2026-09-20

### Added

- Bundled SDLC case receipts and MCP surfaces link stages to exact canonical
  task identities. Full stage-gated Playbook transitions remain in progress.
- VS Code LM records the reasoning option actually handed to `sendRequest`
  and the model-reported context capacity, with provider-internal state kept
  unknown. Accepted-task metrics distinguish complete, incomplete and
  unverified event histories.

### Fixed

- OpenCode worker MCP registration uses the bounded `awh` alias without a
  duplicate legacy alias. VS Code LM worker requests retain required outputs
  and recover from oversized tool input.
- Manager cost-ledger summaries report bounded coverage; Source Graph retains
  JS/TS call-site coordinates for later LSP qualification. The LSP resolver
  and Semantic Review delta scope are not shipped as complete features.

## 0.11.50 — 2026-09-19

### Added

- The identity strip includes a wave mini-roadmap info popup for the
  current in-progress, current or active Roadmap wave.
- VS Code language-model requests apply a declared effort option only
  when the selected model exposes selectable keys that honor the
  canonical profile; otherwise the host records unsupported,
  provider-default, unverifiable or capability-ceiling and does not
  claim an applied value. Model context capacity is recorded from
  `maxInputTokens` and is never used to pad the prompt. The bundled
  runtime also applies the same verified effort and context receipts on
  CLI worker launches.

### Fixed

- The bundled reviewer retained stream also compacts
  `assistant.reasoning_delta` events so a long thinking turn does not
  refuse a completed review.
- Sparse-worktree VS Code raster fixture seeding and the sandbox-safe
  OpenCode config check are repository-only test/manager work and are
  not packaged as VSIX features.

## 0.11.49 — 2026-09-19

### Added

- Outcome-linked NeedFix metrics are included in the bundled runtime; the
  source repository release includes a receipt-authenticated accepted-task
  evaluation corpus, which is not packaged in the VSIX.

### Fixed

- OpenCode's global MCP registration uses the short `awh` alias and is not
  pinned to the AIWorkHub repository.
- The bundled SDLC metrics reader uses the shared read-only SQLite connector
  for paths containing URI-significant characters such as `#`.
- The Windows and OpenCode fixes in the 0.11.45-0.11.48 development notes below
  ship together here; those intermediate numbers were not separately tagged.

## 0.11.48 — 2026-09-16

### Fixed

- OpenCode's model list in Settings was still missing many installed models
  (nemotron and several others among them) even after last release's cache
  fix -- the compact list shared its row budget unfairly across providers, so
  a provider you had individually toggled models for many times before could
  crowd out one you had barely touched yet. The budget is now large enough
  that today's full catalog fits without crowding anyone out.

## 0.11.47 — 2026-09-16

### Fixed

- OpenCode's model list in Settings now stays current regardless of which
  panel you opened first -- it previously only showed installed models after
  the Workforce view had loaded at least once in the same session.

## 0.11.46 — 2026-09-16

### Fixed

- Windows: worker launches no longer fail with an unexplained authority-key
  error. A freshly created key could still be refused by last release's
  security check because nothing had hardened its permissions to match; it
  now is, before the key is ever used.
- Windows: OpenCode now appears in model settings when it's actually
  installed, instead of being refused outright before it was ever looked for.

## 0.11.45 — 2026-09-16

### Fixed

- Windows: Claude Code can now hold the manager seat. The venv launcher
  interposes a redirector process between the MCP server and `claude.exe`;
  identity verification now walks past that one known hop instead of refusing
  because the direct parent process wasn't `claude.exe`.
- Windows: native-CLI sandboxing works on capable hosts again. A required
  security API was being looked up in the wrong system library, so every
  Windows 11 host reported AppContainer confinement as unavailable even when
  it wasn't.

## 0.11.44 — 2026-09-15

### Fixed

- Windows: creating a task no longer stalls. A child process that inherited the
  MCP server's JSON-RPC stdin pipe hung before running its own first
  instruction, so every `git` the coordinator ran burned its whole timeout and
  `aiworkhub_task_create` appeared frozen — it now answers in 0.09 s instead of
  120 s.
- Windows: installed tools are measured again. Node and Ruff reported empty
  version facts, so a card requiring `node>=20.0.0` and `ruff>=0.12` was refused
  as unwinnable on a machine that had Node v22.16.0 and Ruff 0.16.1.
- Windows: the authority key is protected properly. It no longer follows a
  symlink, and its owner and ACL are checked against the security descriptor of
  the open file rather than trusted unconditionally.
- Windows: the reconciler heartbeat is readable again, so health reports stop
  showing a durable status that is present as missing.
- Windows: reviewer cards no longer stay stuck in `processing` — every terminal
  intent read failed, so no reservation could ever be completed.
- Windows: connecting a repository works again. A valid project manifest was
  read as unreadable because Node reports no device for a path it can open.

## 0.11.43 — 2026-09-15

### Added

- The manager can now make small, hash-bound range edits directly through the
  MCP semantic-edit tools workers already use, instead of only reading and
  reasoning about code.
- A new read-only `aiworkhub_dashboard_skills` MCP surface reports measured
  skill-selection coverage: how many recorded receipts actually selected and
  injected a skill, and the streak of recent receipts that injected nothing.
  An unreadable skill store reports "not measured" with a reason instead of a
  healthy-looking zero.
- A new read-only attempt-trajectory export composes one request's audit
  history, process lifecycle, artifacts and usage into a single canonical
  JSON document, with unmeasured fields reported as `UNKNOWN` rather than
  guessed.

### Fixed

- Source Graph's `calls` mode now resolves a query to the one symbol that
  actually defines the queried name before returning call edges, instead of
  also matching every import, decorator or annotation that merely mentions
  it.
- The Windows sandbox report now names the exact measured cause native CLI
  execution was refused — AppContainer APIs unavailable, the execution path
  not wired to them, or the platform is not Windows — instead of one fixed
  blocker code. This does not change, and does not claim, which Windows
  routes are launchable.
- Oversized Source Graph analytic-mode results from the worker AI-tools MCP
  Source Graph route are now spilled to a repository-scoped store before
  being trimmed for display, so the full original stays retrievable instead
  of being discarded the moment it is truncated. The store has no eviction,
  TTL or size cap yet.
- A duplicate launch request for a task already attached to a live worker now
  returns the existing claim instead of recording a new blocked episode that
  could overwrite the original worker's ownership.
- The compact-counter formatter keeps its extraction seam self-contained for
  the test harness; the four-significant-digit rendering shipped in 0.11.42
  is unchanged.

## 0.11.42 — 2026-09-15

### Fixed

- The Models view keeps every provider visible and keeps the routes you
  explicitly configured, even when a bounded catalog read comes back short.
  Rows are selected per provider after all providers have been seen, and routes
  named in `.aiworkhub/config/models.json` are reserved under both the identity
  they were written with and the canonical policy identity the catalog row
  carries, so a vendor-keyed OpenCode decision still pins its own row. Past the
  hard ceiling, pins that do not fit are counted as refused rather than dropped
  silently.
- Model counts no longer present an upstream-truncated list as an exact total.
  Each row is labelled by the bound that produced it — `declared, not
  discovered`, `configured, origin unknown past the host bound`, `configured,
  past the source bound` — and the view names the host that cut the tail rather
  than attributing the editor host's cap to OpenCode rows.
- Compact counters keep four significant digits, so 1000 reads as `1k` and 1001
  as `1.001k` instead of collapsing to one label; a mantissa rounding up to 1000
  promotes its tier, so 999999999 reads as `1B`. The decimal separator follows
  your locale.

## 0.11.41 — 2026-09-14

### Fixed

- Windows native-CLI workers now launch inside the repo-scoped AppContainer
  profile and its kill-on-close Job Object: the launcher declares that backend
  with the canonical repository identity and refuses to spawn without it, and
  the supervisor accepts no other spelling. The confinement report states the
  boundary actually in force rather than a fixed answer, so a host that does
  not qualify is still reported as bounded by process-tree lifetime only.
  Proved by fake-Windows behaviour tests over the real launch path; no
  live-Windows execution evidence is claimed yet.
- A Windows extension host resuming from idle no longer loses repository
  discovery to one transient handle, sharing or lock fault. The manifest read
  now retries exactly once, only for an authenticated transient cause, on a
  brand-new descriptor that repeats every symlink, regular-file and identity
  check. A missing, malformed, foreign or otherwise invalid manifest still
  fails closed on the first attempt, with no sleep and no second retry.

## 0.11.40 — 2026-09-14

### Fixed

- Rejected validation-only candidates can no longer loop through provider-free
  replay; the next rework claim must run the selected worker.
- Parent rejection no longer finalizes or cancels unrelated reviewer tasks.

## 0.11.39 — 2026-09-14

### Fixed

- Automatic review recovery now advances the pending reservation cursor through
  the action actually selected. A deferred first chain no longer loops back to
  itself and prevents later ready review chains from running in the same pass.

## 0.11.38 — 2026-09-14

### Fixed

- Automatic review recovery now advances up to six reservable lifecycle
  actions per reconciler pass and continues across deferred chains, eliminating
  the one-action starvation loop while retaining bounded interactive headroom.
- Task MCP writer serialization, validation replay authority, reviewer process
  cleanup and callback initialization fixes reduce recurring mechanical
  failures and SQLite lock contention.

## 0.11.37 — 2026-09-14

### Fixed

- Reconciler review recovery advances one durable lifecycle action per scan so
  stalled-review repair no longer blocks unrelated Task MCP operations for
  several minutes.

## 0.11.36 — 2026-09-13

### Fixed

- Toolchain receipt production and validation now share the same bounded file
  fingerprint, so large real-world executables no longer trigger false identity
  drift during provider-free replay.

## 0.11.35 — 2026-09-13

### Fixed

- Durable toolchain snapshots are executable-identity checked before reuse;
  stale cache facts are re-derived rather than poisoning provider-free
  validation replay before its first declared command.

## 0.11.34 — 2026-09-13

### Fixed

- Validation-only replay preserves non-empty `read_first` requirements in the
  signed toolchain identity, avoiding false cache-identity failures before
  validation command 1.

## 0.11.33 — 2026-09-13

### Fixed

- Repeated validation-only replay retains the exact authenticated worker MCP
  gate through mechanical validation failures, with fail-closed identity and
  retained-path binding.

## 0.11.32 — 2026-09-13

### Fixed

- Validation-only replay accepts the exact authenticated retained delta when
  the canonical parent has advanced, without widening ordinary unchanged-file
  allowances.

## 0.11.31 — 2026-09-13

### Fixed

- Retained-candidate validation replay carries the full signed task identity,
  eliminating false `toolchain_authority_receipt_card_identity_mismatch`
  failures while keeping receipts request-bound.

## 0.11.30 — 2026-09-13

### Fixed

- Reconciliation materializes missing automatic-review children for sealed
  review-ready candidates, closing the zero-child review stall without manual
  reviewer launches.
- Mechanical review parks, retained-delta reroutes, pending launch failures and
  blocked-review learning identities preserve their authoritative state across
  retries.
- Read-only analysis and research complete without mutation-only validation or
  reviewer requirements.
- OpenCode isolated workers receive authentication, support classic Snap under
  Landlock, and record capacity refusals as explicit provider evidence.
- VS Code LM finalization preserves valid partial finals while failing closed
  on contradictory or unauthenticated terminal evidence.

## 0.11.29 — 2026-09-13

### Added

- Repository Settings displays discovered OpenCode identities as exact model
  children under a collapsible OpenCode family instead of flattening every
  provider and model into one level.

### Fixed

- The Models snapshot reuses cached environment-preflight discovery, avoiding
  a duplicate `opencode models` process on each Settings refresh.
- VS Code LM quality reviewers now have one request-bound terminal submission
  contract, removing the conflicting printed-JSON instruction that caused
  provider-independent review failures.
- Reconciliation restores missing automatic-review chains for sealed
  `review_ready` candidates and no longer lets stale manager-ready projections
  starve current work.
- Every worker backend now enforces the same monotonic hard timeout, and the
  validation sandbox preserves explicit pytest project imports without
  weakening trusted-runtime ordering.

## 0.11.28 — 2026-09-12

### Fixed

- Completed automatic-review receipts remain discoverable after reviewer-card
  archival, preventing already-finished correctness and security work from
  being reported as missing at manager acceptance.

## 0.11.27 — 2026-09-12

### Fixed

- Automatic quality review can fail over to another eligible reviewer route
  after a mechanical route failure and can recover authenticated chains that
  an older runtime stopped only because no route was available.
- OpenCode JSON/SSE event capture now separates top-level terminal evidence
  from child sessions and deduplicates token, cache, and cost totals.

### Added

- A fail-closed OpenCode CLI runtime foundation with exact provider/model
  identities, Linux executable resolution, JSON command construction, and a
  request-local default-deny worker tool policy.

### Limitations

- OpenCode workforce discovery, task-route wiring, dashboard model selection,
  and a live canary remain pending; the adapter is not production-selectable
  in this release.
- Cross-process SQLite single-writer ownership remains open.

## 0.11.26 — 2026-09-11

### Fixed

- `record_launch_blocker` tolerates an unready/absent storage manifest
  instead of a fail-closed storage error masking the real launch-rejection
  reason.
- Workforce catalog rows now carry explicit manager/implementation-worker/
  reviewer role booleans with safe legacy defaults; Codex routes are always
  manager-only.
- The AppContainer-supervisor-identity launch seam and its platform
  dependency are declared in both governance gates that track this.

### Added

- Research: a provenance-pinned Ponytail adoption contract added to the
  universal-development-skills artifact.

### Known issues

- Four pre-existing regressions remain open (tracked as NF-2026-00796 and
  NF-2026-00798): automatic quality-review launch, a toolchain-cache
  finalization check, MCP write-gate visibility, and Source Graph bootstrap
  ordering in `server.main()`.

### Validation

- Full suite: 10,188 passed, 7 failed (the known issues above), 45 skipped.

## 0.11.25 — 2026-09-10

### Fixed

- Worker and reviewer finalization now defer manager notification until the
  system-owned correctness, security, and code-quality chain is complete.
- One authenticated manager-ready aggregate binds the exact candidate and all
  reviewer evidence, then publishes one atomic callback for manager action.
- Review automation no longer accepts the target, closes its NeedFix, or blocks
  later chains while the verified manager decides.
- Launch-time output validation now uses the persisted expanded-template
  provenance instead of misclassifying valid task contracts.
- Legacy review receipts remain compatible and are excluded from the new
  manager-ready queue.

### Validation

- The complete Python suite passes: 10,200 passed and 44 skipped.

## 0.11.24 — 2026-09-10

### Fixed

- Windows Source Graph refresh safely recovers a stale retained identity only
  after a non-signalling PID absence proof; live, recycled, and unprovable PIDs
  remain fenced and are never signalled.
- Shutdown mutates retained ownership only for the exact locally owned process,
  preventing reload from persisting a foreign `stopping` fence.
- Stopped/fenced refresh is now reported as non-refreshable and cannot pass code
  preflight merely because an older generation remains readable.

### Validation

- The Windows lifecycle simulations and focused Source Graph/preflight suite
  pass on Linux; owner-machine Windows live qualification remains pending.

## 0.11.23 — 2026-09-10

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

## 0.11.22 — 2026-09-10

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

## 0.11.21 — 2026-09-10

### Fixed

- Authenticated parse-broken repair launch now accepts request-scoped
  overlay evidence at prefetch, hides stale canonical symbols, and fails
  closed on identity, hash, or scope mismatch.
- Dashboard Tool Recipes, Skills, and Semantic Edit telemetry classify
  unavailable evidence separately from a measured zero (NF722).
- Compatibility repair (NF757).
- Release CI provenance requires a completed successful push run for the
  exact tag commit.

## 0.11.20 — 2026-09-10

### Fixed

- Source Graph `deadmethods` now reports exact entrypoint truth (NF568).
- Nested quality-review findings now normalize to the canonical finding
  schema (NF747).
- Quality review now delivers a single reviewer packet (NF748).

## 0.11.19 — 2026-09-09

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

## 0.11.18 — 2026-09-09

### Fixed

- Reviewer launch and background packet preparation now agree on canonical
  review-ready state. State-less wait telemetry no longer blocks a valid
  quality review before provider start, and stale targets remain rejected.

## 0.11.17 — 2026-09-09

### Fixed

- Repeated identical missing required-create responses now stop with the typed
  `vscode_lm_finalization_nonprogress` result after one exact `v3_create`
  correction. If the rejected path changes, the new identity still gets one
  repair turn and can complete normally.

## 0.11.16 — 2026-09-09

### Fixed

- Python module validations no longer fail solely because the exact running
  interpreter comes from a hosted toolcache with permissive file mode; the
  trust exception remains identity-bound and other world-writable targets are
  still rejected.

## 0.11.15 — 2026-09-09

### Fixed

- Code workers with no required-output delta are now cancelled at the bounded,
  timeout-derived deadline with an exact terminal reason, preventing a warning
  from being followed by the remainder of a long runaway provider session.

## 0.11.14 — 2026-09-09

### Fixed

- Retained rework cards no longer fail before worker launch when review
  evidence names an already-landed system prerequisite outside the card's
  write scope; the prerequisite remains explicit in the sealed contract.

## 0.11.13 — 2026-09-09

### Fixed

- Editor-hosted reviewers submit durable verdicts through the bridge, and the
  workforce capability row reports that dispatch surface truth.
- Sandbox validation distinguishes unavailable Python semaphore support from
  candidate failure before running multiprocessing-dependent checks.
- Kilo/Grok usage captures nested cache counters and additive per-call cost
  without inflating snapshot token totals.
- Claude-only deferred-schema guidance stays out of every other worker prompt.

## 0.11.12 — 2026-09-09

### Fixed

- Workforce rows distinguish launch readiness from historical and current
  route execution evidence, including terminal provider failures.
- Text and native VS Code LM workers continue bounded semantic staging until
  every required edit/create is present and identify the exact missing path and
  action after each correction.
- Fully staged output finalizes locally without another model turn; repeated
  refusal ends with a stable semantic-stage error rather than a broad limit.

## 0.11.11 — 2026-09-09

### Fixed

- Native Codex workers receive an explicit role-scoped MCP `enabled_tools`
  contract, including Source Graph, semantic editing and validation.
- Codex launch now fails closed on a missing code-worker tool contract, while
  Claude-only deferred-schema guidance stays limited to the Claude CLI.

## 0.11.10 — 2026-09-09

### Fixed

- A pending retry with a retained rework delta can be rerouted to an available
  workforce route without discarding the candidate.
- Bare Python validation commands resolve to the trusted canonical interpreter
  before the worker sandbox runs them, matching coordinator finalization.

## 0.11.9 — 2026-09-09

### Added

- Workers are now told which AIWorkHub tool replaces each thing they are told
  not to do, instead of only being told not to do it.
- A worker that genuinely has to make a raw edit can declare why, and the
  declaration is recorded. Taking one of the three legitimate exceptions no
  longer looks the same as ignoring the rule.
- How much of each run's editing went through the semantic editor is measured
  and attached to the result. It is a measurement, not a gate.
- A relaunch that cannot come out differently is refused, and the refusal says
  what to do instead.
- A reviewer sees the earlier rounds' findings for the same task, with line
  numbers only where the file has not changed since.

### Fixed

- Six of the nine model transports were told their tools were blocked when
  nothing blocked them, and three of those were told it while their tool
  surface refuses more completely than any flag could. Each transport is now
  told what is actually true for it.
- Workers no longer receive the raw file editor. The semantic editor does the
  same job with a hash check, and the raw one was being reached for first in
  95% of edits. Creating a new file is unaffected.
- Eleven contract and smoke gates that had never run now run.
- An accept or a reject is written into the session store, so a rework worker
  starts with its predecessor's decision instead of nothing.

## 0.11.8 — 2026-09-08

### Fixed

- The validation sandbox could not install its seccomp filter on a
  position-independent interpreter, which is what most systems ship. The
  process died without a message instead, so validation runs failed with no
  explanation on those machines.

## 0.11.7 — 2026-09-08

### Fixed

- Manager startup left a background thread running on every call, which made
  validation runs that fork unstable on smaller machines. The sweep now runs
  when someone asks for it, and the reconciler owns it otherwise.

## 0.11.6 — 2026-09-08

### Added

- A tool-recipe run surface with usage evidence: the Tool Recipes panel now
  separates recipes that have run from the ones nobody has used, and reports
  who ran each. Seven operator recipes ship as package modules, so they work in
  every repository AIWorkHub manages instead of only in its own checkout.
- An accept preview that answers what would block an acceptance before any
  candidate tree is built.
- Skill usage evidence: a decision now records which skills the card received
  and the actor that produced the outcome, so a skill's activation evidence is
  measured rather than typed.

### Changed

- Manager bootstrap sends its contract once per session and the live identity
  every time, and folds in the repository and task-health facts that used to
  need two more calls.
- Task, NeedFix and review tools answer with receipts instead of echoing back
  the text the caller just sent.
- Reviewers receive the diff and the validation output the review packet always
  promised, for the one lens they were launched for.

### Fixed

- A rework worker now receives the measured failure from the attempt that
  preceded it instead of rediscovering it.
- The automatic reviewer launch driver works again; reviewers no longer have to
  be launched by hand.
- Task usage records carry their topic, so cost and routing views stop
  reporting a third of all work as unknown.
- The operator recipe scripts are shipped with the package; they were never
  committed and could not run outside a developer's own working copy.

### Fixed (release plumbing)

- Release qualification could not run at all: it installed pytest without the
  parallel plugin the project requires, so every platform job failed in under
  half a minute without running a test.
- Two seeding tests measured the toolchain of the machine that ran them, so
  the release qualified locally and failed on CI.

## 0.11.5 — 2026-09-08

### Fixed

- Read-only research and quality-review tasks can finish after successful
  verification. Both acceptance paths now retain the required outcome receipt
  and continue to reject missing or mismatched evidence.

## 0.11.4 — 2026-09-08

### Fixed

- Validation commands can update timestamps on their authenticated scratch files
  through the metadata broker, including Python and Node file-descriptor calls.
  File ownership and path restrictions remain enforced.
- Windows job and file metadata structures now share canonical declarations
  across process supervision, file operations and temporary-file handling.
- Successful task rework preserves the authenticated candidate across review
  rejection and verifies retained artifacts before restoring the same attempt.

## 0.11.3 — 2026-09-07

### Fixed

- Skills are mined from the repository's own correction record instead of
  written by hand, and a candidate must recur across three separate cards
  before it is offered.
- A skill declared at a lower risk tier now applies to higher-risk work, and
  cards can finally declare which skills they want on the path they are
  actually created on.
- A transient provider failure, an expired credential and a real defect are
  told apart. Work is no longer discarded when a credential expires at the end
  of a run.
- A model the account cannot use is switched off after it fails, instead of
  taking another hundred launches.
- The dashboard cards say loading before they say nothing.
