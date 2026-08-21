<div align="center">
  <img src="https://raw.githubusercontent.com/shrec/AIWorkHub/main/vscode-extension/media/aiworkhub-hero.png" alt="AIWorkHub" width="100%">
</div>

# AIWorkHub for VS Code

AIWorkHub is a repository-native control plane for multi-model software
development. It gives every repository an isolated task system, Source Graph,
durable project context, worker runtime and evidence-first review loop.

The extension opens as a retained editor tab and runs one repository-scoped
MCP stdio runtime on the workspace host. It does not open a browser, bind a
port, expose a LAN service or require an AIWorkHub cloud account.

## What's new in 0.10.13

- Storage refreshes reuse a bounded append-aware process-event projection;
  warm reads of the canonical 33,000-row ledger fell from 1.12 seconds to
  about 10 ms and complete retention preview to roughly 0.27-0.35 seconds.
- Rotation, spill, replacement, truncation and deletion invalidate the cache
  and replay canonical event order, so the speedup does not weaken evidence.

## What's new in 0.10.12

- Source Graph imported-call resolution tokenizes each source line once,
  cutting the canonical full rebuild from 66.82 seconds to 18.60 seconds.
- Phase-level build telemetry identifies hashing, extraction, merge,
  resolution, Git-metrics and quality costs independently.

## What's new in 0.10.11

- Workers materialize only task-declared files in sparse linked worktrees,
  avoiding whole-repository checkout cost while retaining mechanical scope and
  zero-diff verification.
- Warm process-event lookups no longer reparse the complete ledger, and exact
  append/rotation/replacement identities invalidate cached projections.
- Windows cleanup timeout recovery is request-owned and diagnostics identify
  the real failing command; semantic-edit placeholders fail closed.

## What's new in 0.10.10

- Byte-identical compatibility edits are verified without rewriting files or
  claiming changes, preventing already-satisfied cards from looping through
  required-output validation.

## What's new in 0.10.9

- Windows worker finalization and review acceptance no longer fail solely on
  a hard-coded two-second Git probe. A full creation-time manifest verifies
  zero-diff and changed paths after bounded Git-tree cleanup, while Preflight
  now checks the real isolated finalization path.

## What's new in 0.10.8

- Text-only GLM workers preserve intentional Source Graph query modes and
  targets, so exact indexed file/symbol reads no longer collapse into false
  zero-hit `focus` queries.

## What's new in 0.10.7

- Terminal NeedFix lifecycle now releases stale converted-task storage pins.
- Exact ledger-owned orphan worktrees can be quarantined and restored without
  weakening protection for unknown or active workspaces.

## What's new in 0.10.6

- Storage inventory attributes AIWorkHub-owned worktrees from their exact
  durable request envelope even after Git prunes the linked registration.
- Ownership recovery stays process-free per worktree and never treats a broken
  checkout as safely removable.

## What's new in 0.10.5

- Rework workers see canonical Source Graph results plus exact changed/deleted
  worktree paths without cloning or rebuilding the repository graph.
- Task-store event indexes upgrade safely across older compatible schemas.

## What's new in 0.10.4

- NeedFix active-state reads are roughly seven times faster on the canonical
  repository through one bounded task-card snapshot with exact fallback.
- Indexed review-event chronology removes the dashboard's correlated event-log
  scan and keeps decision/latency aggregates exact.

## What's new in 0.10.3

- Full dashboard payloads are about 62.5% smaller on the canonical repository;
  Storage retains its aggregate policy/count/byte truth without retransmitting
  thousands of protected per-request rows that the UI never renders.

## What's new in 0.10.2

- Storage telemetry survives MCP runtime and VS Code window reloads through an
  atomic repository-bound last-known-good snapshot. On the canonical 19.9 GB
  store, the card restored in 5.6 ms rather than recalculating for 88 seconds.
- Slow background inventory refreshes keep the previous truthful measurement
  visible and cannot override live disk capacity or read-only state.

## What's new in 0.10.1

- Progressive summaries update only their authoritative queue/storage fields,
  so rich dashboard cards never flash back to `No sample` or `Unavailable`.
- Windows zero-diff review acceptance and worker finalization avoid redundant
  `git rev-parse HEAD`; remaining probes are bounded and reap their process tree.
- Finalization telemetry separates scope, validation and transition time.

## What's new in 0.10.0

- Dashboard counters and health render from a bounded summary before heavier
  task, process, KPI, NeedFix and Roadmap sections hydrate.
- Independent full-snapshot reads use a core-derived pool while reserving MCP
  headroom; local full construction fell from 5.1–6.1 seconds to about 3.1.
- Storage calculation starts during health initialization and publishes a
  completed scan in the full response when available, eliminating the stale
  multi-minute `Calculating` state on the measured 19.9 GB repository.

## What's new in 0.9.99

- Manager dashboard summaries now skip full-only workforce, cost, KPI,
  process, task-plan, NeedFix and Roadmap reads, reducing the measured summary
  build from 6.52 seconds to 0.35 seconds.
- Windows workspace verification no longer starts redundant post-create Git
  probes; detached state and base OID are read from bounded worktree metadata,
  removing the reported `git symbolic-ref` provisioning hang.
- Dashboard Storage calculation removes a quarantine-batch N+1 query and uses
  a single lower-syscall tree walk; measured cold latency fell from 25.87
  seconds to 3.44 seconds on the live 19.9 GB store.
- Nested worker bytes are counted exactly once while repository-owned and
  unattributed worktrees remain separately visible.
- AI Memory and Context Graph searches keep their query paths read-only after
  initialization-time schema reconciliation.

## What's new in 0.9.97

- Source Graph incremental refreshes are now scoped to changed Python callers
  and genuinely changed function identities, preserving rename/delete truth
  without a repository-wide edge scan.
- A dedicated qualname index removes the dominant quality-scorecard bottleneck.
- Task-plan snapshots use one bounded SQLite card read instead of an N+1 query
  pattern, keeping large NeedFix/task histories responsive.

## What's new in 0.9.96

- Long-running model work is no longer killed after ten quiet minutes while its
  exact supervisor, child and heartbeat remain live.
- Time since the last visible model output is now diagnostic information only;
  completion, provider error, verified exit or explicit cancellation determines
  the task outcome.

## What's new in 0.9.95

- VS Code LM workers no longer get stuck in the final semantic-edit stage. The
  bridge advertises separate, strict schemas and canonical examples for new-file
  creation and range replacement.
- Late session, memory, KB and Source Graph requests are corrected once without
  being sent to MCP; repeated violations stop with one bounded reason.
- Offline stage/finalize calls now reject missing, hybrid and extra fields while
  preserving exact tool-call identity.

## What's new in 0.9.94

- AIWorkHub could stop responding until you reloaded the window: two operations
  competing for one file left one waiting forever on Linux, and the recovery for
  it only ran on Windows. Reviews that seemed to start and never did were this.
- A running worker could be declared dead and its task closed, because "is this
  process alive?" was answered in three places and answered wrongly on Linux for
  a process we are not permitted to signal.
- Stopping a task could leave a child running — the stop signalled the group but
  checked only the parent.
- A surgical edit could be applied without checking the file was unchanged, in
  silence. Unverified edits are now reported as such.
- The MCP server never started in Copilot: the configuration named the
  interpreter by a path Copilot could not resolve remotely.

## What's new in 0.9.93

- Asking for a function or class by name returned nothing even when it was
  indexed — found, then discarded on the way back. The tool's own suggestions use
  that form, so following its advice led nowhere and work fell back to reading
  whole files.
- AIWorkHub now asks the editor which models are available instead of carrying
  its own list, which knew one model while six were configured and working. A
  newly offered model appears on its own, with nothing to type.
- A request outside a task's permitted files is refused by name, not returned as
  an empty result.

## What's new in 0.9.92

- A single failed internal lookup was enough to reject finished, correct work,
  and a rejected task could only be redone rather than accepted. This is the fix
  that unblocks the loop.
- A task waiting in the queue was counted as one already running, so the launch
  check reported a conflict for practically every launch.
- Starting a review reported success before anything that could refuse it had
  run; eight tasks were found listed as running with nothing behind them, the
  oldest for fifteen hours.
- The review verdict used a status its own list of valid statuses did not
  contain, and an empty verdict read as a pass.

## What's new in 0.9.91

- A reviewer that read nothing could still mark work as passed. At the middle
  risk level two of three reviewers were never checked for whether they had seen
  the code, and a review with no observation counted as a pass. Fixed at every
  risk level.
- The gate reported that the project's own checks had vetted the work when none
  of them had run; it now reports only what executed.
- A task could disarm the checks meant to police it while keeping their number
  unchanged. The comparison now reads what the checks contain.
- Failures could arrive labelled "ready for review"; unrecognised outcomes are
  now treated as blocked.
- A blocked task was locked forever — allowed to enter, not to leave. Leaving is
  now part of the contract.

## What's new in 0.9.90

- AIWorkHub can now tell you why a run died. A provider refusing a task — no
  balance, exhausted quota, rejected key — used to be reported as "the task
  failed", the same message used for broken code, and half of all blocked tasks
  carried it. Refusals are now named for what they are.
- A refusal that arrives without a stated cause says so, instead of telling you
  to re-authenticate a credential that may be perfectly fine.
- Each provider states whether its remaining quota is observable at all, and why
  not, instead of every provider showing the same unhelpful status.

## What's new in 0.9.89

- Work that changes nothing can no longer pass review silently: a task once added
  1,197 lines of correct, tested code that nothing ever called, and all three
  reviewers passed it. Review now checks reachability and names what is not.
- Finishing an old task can no longer undo a newer release. Work started before
  an update and accepted after it used to put the old version number back, which
  stops the extension connecting to its own server. It is now refused, with both
  versions named, before anything is written.

## What's new in 0.9.88

- The dashboard now follows your editor's font size. Most text was hard-coded at
  8-11px and ignored the size you had already chosen in VS Code; every size now
  derives from that setting, with the smallest around 11px at the default.
- AIWorkHub could not start its own server for Claude Code: the config file it
  wrote carried a VS Code placeholder Claude Code cannot expand, and every update
  rewrote it. It now writes a real path and refuses to save an unusable config.
- A working task looked dead — the panel dropped the model's live thinking output
  — and valid output was sometimes labelled "unsupported".
- A failed task can no longer be blocked without a recorded reason, a failed
  check keeps the part of its output that explains the failure, and a missing
  file is no longer reported as a corrupt one.

## What's new in 0.9.87

- AIWorkHub no longer copies its whole code index before a retry. Rework is the
  thing that happens most often, and each one duplicated a 107 MB index first;
  it now indexes only the files that changed — 5 MB here — and that wait no
  longer grows as your project does.
- A cleanup job with no ending was locking the task database. Ten of them
  retried every fifteen seconds against tasks archived hours earlier, so every
  review launch failed with "database is locked" while pointing at the wrong
  database.
- Empty storage batches piled up instead of being cleaned: a Windows install had
  100 batches reporting "empty, 0 bytes" while the folder held 3.68 GB. They are
  collected now, and only when the record and the disk agree.

## What's new in 0.9.86

- Work under review could be deleted. When a task's saved work failed its
  integrity check, AIWorkHub deleted the whole workspace rather than preserving
  it — it happened here this morning and took 264 lines of finished work with it,
  silently. Failing work is now quarantined and every removal is audited.
- A task's own code could run outside its sandbox, with AIWorkHub's permissions,
  through a helper that checked whether the test tools were installed.
- A task could declare its own result: certain exit codes were trusted as "could
  not run here" when a test can produce them deliberately.
- Process identity lost a digit above a certain size, and two screens disagreed
  by one — that number is what tells a live worker from a recycled process id.
- AIWorkHub recorded nothing from Claude into its context history, and reported
  "not configured" instead of failing.
- Rejecting a task left its reviewers in the queue forever, three per rejection.
- Continuous Audit as a Service is now upheld by the system rather than by
  remembering to check, and the audit layer runs its first real pass.

## What's new in 0.9.85

- Starting a quality review took twenty to thirty minutes before anything
  happened; it now takes about half a minute. A reviewer needs its own copy of
  the project index to see the code as the task changed it, and AIWorkHub was
  duplicating the entire 107 MB index for every reviewer in order to change the
  six files the task actually touched.
- AIWorkHub now indexes only the changed files and links that small index to the
  main one. Measured here: 107 MB and ~25 minutes became 5.3 MB and 30 seconds.
- The new cost depends on how much changed, not on how large the project is — a
  project with a 1 GB index pays the same for the same six files.

## What's new in 0.9.84

- The audit trail credited the wrong model for the work: every manager action was
  recorded as if Codex performed it, including the ones Claude performed. Actions
  now name whoever actually held the manager seat, and an action that cannot be
  attributed fails rather than guessing.
- Rejecting a task left its reviewers stuck in the review queue forever, three per
  rejection, until the real work was impossible to find. Rejection now cleans up
  exactly as acceptance does.
- After a window reload, a stale manager session still reported itself verified.
  It now fails loudly instead of quietly answering the wrong question.
- A task could be started with a write scope that did not include the file it had
  to change. The worker sees only permission denied, so it retries in the wrong
  files until its budget is gone. Creating a task now warns by name.
- A database opened "read-only" was writable if its path contained a `#`, and wrote
  to a different file than the one named. Fixed everywhere and verified by sweeping
  the whole repository.

## What's new in 0.9.83

- Work that was finished and correct is no longer thrown away because of where it
  ran. Four checks depended on a home directory, a temporary path, a file
  permission or a nested launch the sandbox does not provide; any task touching
  one of them was marked failed in a way that could not be retried.
- A task that finished and was then blocked now tells you. The notification was
  being suppressed as a duplicate, and the delivery thread could be killed by a
  single unexpected error while health still showed green.
- Asking about an archived task no longer crashes the status tool, a replaced task
  no longer shows as waiting work, and one blocked task at the front of the queue
  no longer stops everything behind it.
- A task can no longer lower the bar it is judged against by changing its own
  quality settings, and a report with zero tests no longer counts as a pass.
- Cleaning up old files can no longer lose them when interrupted, reports the
  bytes it actually freed, and the recovery action the Storage panel recommends
  now exists.
- NeedFix counts fall when the work behind them finishes.

## What's new in 0.9.82

- **Source Graph could stop a search a third of the way through and still report
  it as finished, and could return the wrong lines for a symbol.** A page with
  nothing wrong ended the scan permanently, and reads used the line numbers from
  the last index rather than the file as it is now. Both fixed; every read reports
  its freshness.
- **The bug analyzers reported "clean" for languages they cannot read** - five
  C-family detectors ran against 395 Python files and returned no findings. They
  now say they do not apply.
- **A worker that committed inside its own worktree lost the work**, while the
  task still reported its required outputs as validated.
- **The manager could launch conflicting tasks in parallel**, because the default
  plan view was missing every write-conflict field.
- **A stalled runtime kept advertising itself as available**, and storage
  "Calculating" took eighty seconds on a 17 GB repository. Both bounded now.

See the packaged **Changelog** for the complete release summary.

## What's new in 0.9.81

- **The runtime could report itself unavailable on a large repository.** Startup
  ran a full reconciliation scan of every task and every retained workspace before
  answering the editor, so the editor gave up and showed "MCP runtime unavailable"
  against a healthy runtime. Measured here: a 26.7 second startup, 21.6 seconds of
  it in that scan. It now runs in the background and startup answers in 0.77
  seconds.

See the packaged **Changelog** for the complete release summary.

## What's new in 0.9.80

- **Live Output stopped polling forever the first time it had nothing to show.**
  Selecting a task right after launching it returned "output unavailable" and the
  poll chain was never restarted, so the panel kept showing that error while the
  worker streamed. It now restarts on every outcome, and a late task-detail reply
  no longer resurrects a panel you had already left.
- **An idempotency key could silently swallow another file's edit.** Two semantic
  edits sent with the same key but different targets counted as a repeat: the
  second was never written and still reported success, carrying the first file's
  path. A replay must now match in target as well as key.
- **Storage grew without a working bound.** Quarantined directories no batch
  claimed could never be purged, empty batches had no collector, and worker logs
  had no per-file limit. Each now bounds itself, and an oversized terminal log
  keeps the diagnostic tail the launcher reads.
- **Archived tasks stayed live and held their worktrees.** Archiving is now a
  single guarded write that verifies itself after committing, so a task cannot
  sit half-archived and pin storage forever.

See the packaged **Changelog** for the complete release summary.

## What's new in 0.9.79

- **A card that changed a generated file could never be reviewed.** If a change
  touched an eval artifact or anything else Source Graph deliberately does not
  index, the reviewer never started — the launch reported success and then
  nothing happened. Prewarm now skips such a path and the reviewer still runs
  from the sealed review packet. A genuine indexing failure is still refused
  loudly.
- **The storage retention preview could not finish measuring.** It timed out
  after 90 seconds with nothing measured, so no cleanup candidate was ever
  produced. On a 29 GB repository it now completes in under nine seconds and
  reports what is actually reclaimable.

See the packaged **Changelog** for the complete release summary.

## What's new in 0.9.78

- **On macOS, process identity did not exist.** A pid alone is not an identity —
  the operating system reuses them — so a reused pid could be read as a live
  worker. Darwin now supplies a real process creation time, and the eight
  launcher regressions that failed on every macOS CI run pass rather than being
  skipped. An unknown identity fails closed: a runtime directory whose owner
  cannot be identified is never reclaimed.
- **The manager callback follows whoever holds the manager seat.** Codex keeps its
  existing push path unchanged; a Claude manager is now woken through its own
  channel instead of only receiving work while it happened to be waiting.

See the packaged **Changelog** for the complete release summary.

## What's new in 0.9.77

- **If you have only one model provider installed, finished work could never be
  accepted.** The acceptance gate demanded a review from a different vendor than
  the one that did the work, so on a single-provider setup nothing could pass,
  however complete and green it was.
- Reviewer independence is now a recorded ladder — `cross_provider` degrading to
  `cross_model_same_provider` and then to `same_model_fresh_context` — and the
  level actually achieved is written into the acceptance evidence, so you can see
  exactly how independent each review was.
- A reviewer that cannot read the review packet is still refused for its lens,
  and a review that cannot be attributed to a worker provider is still refused.
  Every other safeguard is unchanged.
- Source Graph no longer reports success for a build that indexed nothing, and a
  duplicate declaration in one PHP file no longer aborts the whole index.
- Restoring a quarantined worktree reinstates its git registration and says so
  when it cannot, rather than leaving the directory attributed to nobody.

See the packaged **Changelog** for the complete release summary.

## What's new in 0.9.74

- A quality lens can no longer be satisfied by a reviewer that could not inspect
  anything. A reviewer whose sandbox gives it no file-read tool cannot open the
  review packet it is handed as a file path — and its empty report still came
  back shaped identically to a real review, so a required lens counted as
  satisfied by an inspection that never happened. Observed four times.
- The gate now marks such a lens `reviewer_could_not_inspect`, but only on a
  positive signal: findings that are all `process_limit`, or usage telemetry
  that is present and records zero activity.
- Missing telemetry deliberately stays "unknown" and keeps satisfying the lens.
  Most honest reviews carry no inspection telemetry, and demanding proof of
  inspection rejects real work — absence of evidence is not evidence of absence.

See the packaged **Changelog** for the complete release summary.

## What's new in 0.9.73

- A read-only SQLite open is now actually read-only. Every such connection was
  built by interpolating the path into a URI string, and in SQLite URI syntax a
  `#` in the path swallows the `?mode=ro` query — so the database opened
  read-write with create-if-missing, at a path truncated at the `#`. Verified
  directly: the old form accepted a write and left a stray file on disk, while
  the corrected form refuses the write and creates nothing.
- All eight affected call sites now route through one shared helper rather than
  eight independent strings that each had to stay correct forever, and the
  helper applies two independent guarantees: percent-encoding so the query
  survives URI parsing, and `PRAGMA query_only = ON` so a write is refused even
  if it did not.
- Every converted call site keeps its exact previous timeout, so behaviour
  under concurrent load is unchanged.

See the packaged **Changelog** for the complete release summary.

## What's new in 0.9.72

- Storage retention actually reclaims. Worktree eligibility reported zero
  candidates while the repository sat at 43.3 GB against a 5 GB cap, because
  every attempt workspace stayed pinned and nothing released a superseded one.
  A superseded attempt whose successor has been sealed is now eligible, and
  exceeding the cap forces reclamation of the oldest superseded lineage.
- Live-worktree protection is keyed on the field the claim path actually writes,
  so a `processing` or `review` card's worktree is protected while it runs. It
  was keyed on a field production only writes after acceptance, leaving every
  in-flight worktree unprotected.
- Protection no longer depends on how large the task table has grown, and the
  planner fails closed when task lineage cannot be read.
- Measured here after the fix: 140 reclaimable worktrees, 17.4 GB, with the two
  live workers correctly protected.

See the packaged **Changelog** for the complete release summary.

## What's new in 0.9.71

- A card owner can now release its own claim. The card-scoped write-action set
  omitted `launch-failed`, so a reconciled reservation could leave its card
  stranded in `processing` forever with no live provider able to finish it.
- Both authority call sites now share one named action set, so they cannot
  drift apart. Codex authority is unchanged.

See the packaged **Changelog** for the complete release summary.

## What's new in 0.9.70

- Coordinator routing resolves from the active verified manager route, so a
  Claude-routed repository no longer sits permanently at `route_pending`
  waiting for a Codex thread that will never appear.
- The dashboard no longer raises a Manager coordination Route warning that no
  operator action can clear, and manager identity and coordinator routing can
  no longer disagree inside the same response.
- Codex routing behaviour is unchanged, and a repository with no verified
  route fails closed rather than defaulting to a provider.

See the packaged **Changelog** for the complete release summary.

## What's new in 0.9.69

- A verified Claude manager now receives worker callbacks in its active session
  instead of waiting in the inbox until it happens to poll, so `review_ready`
  and `worker_failed` transitions reach the manager as they happen.
- The lease and ack contract is unchanged: one verified manager route holds a
  batch, ack stays mandatory, an unacked batch stays redeliverable, and a route
  whose provider, repository or session identity does not match never receives
  it.
- `dispatcher_health` reports a truthful provider-specific state on a Claude
  route instead of flagging an unregistered dispatcher where none is expected.
- Windows and macOS CI jobs run explicit platform-owned regression manifests and
  fail if a manifest entry collects nothing, so CI can no longer pass silently
  on zero collected platform tests.

See the packaged **Changelog** for the complete release summary.

<div align="center">
  <img src="https://raw.githubusercontent.com/shrec/AIWorkHub/main/docs/assets/demo/aiworkhub-task-review-loop.gif" alt="AIWorkHub task, worker, evidence and review loop" width="100%">
  <br>
  <em>Create a bounded task, launch a model worker, inspect its evidence and accept or rework it.</em>
</div>

## Highlights

- Plan and inspect dependency-aware AI tasks from one operational dashboard.
- Delegate to supported local model adapters and track real terminal outcomes.
- Replace repeated raw-source discovery with a repository Source Graph covering
  34 configurable code, data and documentation families.
- Send focused code fragments through staged semantic edits and let the local
  bridge assemble the hash-bound final envelope without model-side full-file
  regeneration.
- Use 31 bounded source-intelligence modes for symbols, calls, tests, impact,
  complexity, ownership, hotspots, gaps and task-shaped context bundles.
- Preserve continuity through Session Manager, AI Memory and KB.
- Review diffs, tests, logs, artifacts, approval history and deterministic
  Quality Evidence before acceptance.
- Run a changed-file Known Bug Scanner across C/C++/CUDA, Python,
  JavaScript/TypeScript, Go, Java/Kotlin and PHP without treating heuristic
  warnings as proven failures.
- Measure whether workers used Source Graph throughout the task through
  authenticated tool-use receipts and continuous-use telemetry.
- Keep repositories isolated in separate `.aiworkhub/` authorities.
- Run on Linux, macOS, native Windows, WSL and Remote-SSH.

## Operational surface

The retained dashboard combines the task DAG, live worker output, Review
Inbox, callback health, model readiness, tool-use statistics, storage
retention, Source Graph coverage and bounded viewers for logs, sessions,
AI Memory and KB. Settings remain repository-local under `.aiworkhub/`, so a
multi-window installation does not share task or context authority between
repositories.

<div align="center">
  <img src="https://raw.githubusercontent.com/shrec/AIWorkHub/main/docs/assets/screenshots/aiworkhub-self-hosted-dashboard.png" alt="AIWorkHub repository dashboard" width="100%">
  <br>
  <em>Tasks, callback health, source coverage, context stores, preflight and evidence in one retained editor tab.</em>
</div>

## Get started

1. Install from the Marketplace (or install a release VSIX) and open a Git
   repository in VS Code.
2. Run **AIWorkHub: Open Dashboard**.
3. Select the repository when using a multi-root workspace.
4. Choose **Initialize AIWorkHub** on first use.
5. Open a new Codex, Claude or MCP-capable chat after registration so the new
   runtime tools are discovered by that chat process.

Initialization is explicit and idempotent. It creates repository-local state
only under `.aiworkhub/` and starts the first Source Graph index in the
background.

For Claude Code, initialization also maintains the repository-local
`.mcp.json` server registration and the bounded AIWorkHub block in `CLAUDE.md`.
Open a **new** Claude chat after initialization or an AIWorkHub upgrade. That
direct chat is instructed to bootstrap as the manager, call manager Source
Graph before broad `Read`/`Grep`/`Glob` discovery, and re-query the graph when
its implementation or validation boundary changes. AIWorkHub-launched task
processes use the separate worker tool surface.

## Run your first task

AIWorkHub is designed for a manager chat that delegates bounded work instead
of letting several models edit one checkout without coordination.

Start a new chat after initialization or upgrade and paste:

```text
Use AIWorkHub as manager for the currently bound repository. Call
aiworkhub_manager_bootstrap first; verify repository identity, manager route,
callback, Source Graph and preflight. Do not edit or launch yet. Report what is
ready and what is degraded.
```

Then describe the desired outcome normally. Ask the manager to create bounded
cards and launch only independent, dependency-ready, non-colliding cards in
parallel. The MCP server also presents this lifecycle as a mandatory contract:
creating a task leaves it `pending`; exact claim plus launch establishes
`processing`; workers stop at `review_ready`; callbacks wake the current
verified manager; and only that manager accepts or rejects verified evidence.

1. Check the dashboard header. Repository, MCP, Source Graph and callback
   state should be ready; **Preflight** explains any unavailable optional
   model adapters.
2. Tell the manager what outcome you want. The manager creates a task card
   with an exact objective, acceptance criteria, allowed writes, validation
   commands and dependencies.
3. The manager selects a ready adapter/model and launches the exact card.
   Workers receive repository-scoped Source Graph, Session, Memory and KB
   context and work in an isolated task workspace.
4. Follow **Live Output** or continue other work. Terminal outcomes are durable
   and the originating manager receives a callback when review is required.
5. Open the task in **Review**. Inspect the bounded diff, tests, logs,
   artifacts, tool-use receipts and independent reviewer evidence.
6. **Accept** promotes the verified change and finalizes the task. **Reject**
   records exact feedback and creates a bounded residual rather than silently
   discarding the previous evidence.

Dependency cards remain pending until their prerequisites finish. Collision
checks prevent two active workers from owning overlapping write paths.

See the [complete first-run and manager manual](https://github.com/shrec/AIWorkHub/blob/main/docs/GETTING_STARTED.md)
for copy/paste planning/review prompts, Remote-SSH behavior and recovery after
an interrupted write acknowledgement.

## Models and authentication

AIWorkHub does not proxy credentials or require an AIWorkHub account. It uses
models already authenticated in the corresponding editor or CLI.

| Runner | Typical adapter | Requirement |
| --- | --- | --- |
| Codex | Codex CLI or VS Code Language Model | Existing Codex login or one-time VS Code consent |
| Claude | Claude Code CLI or VS Code Language Model | Existing Claude subscription login or one-time VS Code consent |
| DeepSeek V4 Pro/Flash | VS Code Language Model or Copilot CLI fallback | Provider visible in VS Code; fallback uses its own stored credential |
| GLM 5.2 | VS Code Language Model or Copilot CLI fallback | Provider visible in VS Code; fallback uses its own stored credential |
| Copilot-hosted models | VS Code Language Model | GitHub sign-in and one-time model consent |

The **Preflight** and **Workforce** views report observed availability,
adapter/model identity, outcomes, latency, token evidence and known cost. An
optional adapter being unavailable does not block otherwise ready models.

## Source Graph and context

Source Graph is an incrementally refreshed structural repository index, not a
remote Sourcegraph service. Managers and workers start with low-token `focus`
and `slice` queries, then use calls, trace, impact, test mapping or typed
bundles only when the task needs them. Operations telemetry shows which modes
were requested and executed, returned evidence, workflow stage, latency,
generation and inter-call gaps.

Session Manager stores current state and handoffs; AI Memory stores durable
lessons; KB stores curated project facts; the optional Manager Context Graph
preserves bounded manager transcript evidence. All are repository-local and
have bounded viewers in the dashboard.

## Commands

- **AIWorkHub: Open Dashboard** — open or reveal the retained editor tab.
- **AIWorkHub: Select Repository** — bind the dashboard in a multi-root window.
- **AIWorkHub: Refresh Dashboard** — refresh the current repository snapshot.
- **AIWorkHub: Restart MCP Connection** — replace only AIWorkHub's selected
  repository MCP child.

## Remote development

AIWorkHub is a workspace extension. In Remote-SSH, install it on the remote
extension host; its packaged Python runtime, MCP child and repository state run
beside the remote checkout. No port forwarding is required.

## If the dashboard is not ready

- **Connecting:** use **AIWorkHub: Restart MCP Connection** once and inspect
  the dashboard's last-log row. The extension restarts only its own child.
- **A model is unavailable:** open Preflight, confirm the provider is installed
  and grant the one-time VS Code model consent when prompted.
- **Source Graph is empty:** initialize the repository, enable the required
  language family in Settings and run a refresh.
- **A chat cannot see tools:** open a new chat after installation or upgrade so
  that client performs MCP discovery against the current runtime.
- **Windows upgraded from an old build:** activation automatically migrates
  legacy source/version `PYTHONPATH` registrations to a host-stable packaged
  runtime; no manual `config.toml` edit is required.

## Trust and privacy

- Local stdio transport; no AIWorkHub network listener.
- Read-only and launch-disabled by default.
- Repository-specific state, route identity and audit trail.
- No AIWorkHub telemetry upload of prompts, source, credentials or memories.
- Explicit manager authority for context writes and task acceptance.

Read the full [Getting Started guide](https://github.com/shrec/AIWorkHub/blob/main/docs/GETTING_STARTED.md),
[Architecture](https://github.com/shrec/AIWorkHub/blob/main/docs/ARCHITECTURE.md),
[Source Graph guide](https://github.com/shrec/AIWorkHub/blob/main/docs/SOURCE_GRAPH.md),
[Manager Context Graph guide](https://github.com/shrec/AIWorkHub/blob/main/docs/CONTEXT_GRAPH.md),
[Security Policy](https://github.com/shrec/AIWorkHub/blob/main/SECURITY.md) and
[Product Roadmap](https://github.com/shrec/AIWorkHub/blob/main/docs/PRODUCT_ROADMAP.md).

## Development build

```bash
npm --prefix vscode-extension install
npm --prefix vscode-extension test
npm --prefix vscode-extension run package
code --install-extension vscode-extension/dist/aiworkhub-*.vsix
```

AIWorkHub is open source under the
[MIT License](https://github.com/shrec/AIWorkHub/blob/main/LICENSE).
