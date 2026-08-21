# AIWorkHub for VS Code — Changelog

## 0.10.34 — 2026-08-21

### Fixed

- Sparse pytest and retained validation-only replays now import the exact
  candidate package rather than resolving through the parent canonical tree.

## 0.10.33 — 2026-08-21

### Fixed

- Rework launch/sealing uses the committed claim epoch, and sparse Python
  validation imports candidate package bytes rather than canonical bytes.

## 0.10.32 — 2026-08-21

### Fixed

- Quarantine paths accept only canonical request IDs before any filesystem
  mutation, and validation preflight detects hidden chained full-suite pytest
  commands including adjacent operators.
- Bare-string reasoning deltas render as reasoning without shadowing result or
  nested tool-result payloads that contain auxiliary reasoning metadata.

## 0.10.31 — 2026-08-21

### Fixed

- Read-only zero-diff validation failures keep their original authenticated
  MCP-gate reason instead of being reclassified as a retained-workspace hash
  failure during reconciliation.

## 0.10.30 — 2026-08-21

### Fixed

- Provider infrastructure failures no longer count as model-quality failures
  in workforce ranking.
- Repository-disabled routes no longer degrade aggregate Preflight coverage.
- Reload rebinding supersedes stale callback wakes instead of preserving a
  historical pending backlog.

## 0.10.29 — 2026-08-21

### Fixed

- Required research-card MCP tools now fail closed on missing authenticated
  receipts instead of trusting provider text.
- Explicit zero-diff rework selection matches automatic predecessor
  resolution, and cold Windows finalization probes no longer flash a false
  Blocked state.

## 0.10.28 — 2026-08-21

### Fixed

- Repository switching transfers the verified foreground Codex manager route
  with an atomic epoch fence instead of requiring the target window to claim
  the chat in advance.
- Truncated quality-evidence provenance is explicitly marked and consistent
  across serializers.

## 0.10.27 — 2026-08-21

### Fixed

- Validation-failed review tasks can be safely returned for clean rework when
  their retained delta descriptor is missing or invalid, instead of remaining
  stuck in the review queue.

## 0.10.26 — 2026-08-21

### Fixed

- Repository Settings uses one responsive scroll surface; its heading,
  footnote and sticky tabs remain stable while model switches rerender.
- Plan DAG defaults to current work, adds history/search controls, uses compact
  cards and opens full canonical Task Detail when a card is selected.

## 0.10.25 — 2026-08-21

### Added

- The Models tab lists every model discovered from the active VS Code/Copilot
  host and provides an exact repository-local switch for each model.

### Fixed

- Copilot routes are grouped and gated separately from native provider routes,
  with one provider switch covering all editor-hosted transports.

## 0.10.24 — 2026-08-21

### Added

- Repository Settings exposes provider/model enablement controls that directly
  gate workforce routing for the active repository.
- A Context Graph header card reports durable manager query usage and graph
  size without adding a separate dashboard round trip.

## 0.10.23 — 2026-08-21

### Fixed

- Failed Windows worktree creation no longer enters a second blocking Git
  cleanup/prune path; exact partial state is cleaned without another process.

## 0.10.22 — 2026-08-21

### Fixed

- Exact workspace cleanup is process-free and no longer depends on slow or
  wedged Windows `git worktree remove`/`prune` subprocesses.
- Windows finalization Preflight now returns immediately while one coalesced,
  HEAD-bound background canary establishes the real Ready or Blocked result.

## 0.10.21 — 2026-08-21

### Fixed

- Candidate Python and npm validation now execute from the retained sparse
  workspace with deterministic source/config authority.
- Windows-facing Git preflight is noninteractive, HEAD-bound and process-free
  for cache identity, preventing failed probes from being cached as Ready.

## 0.10.20 — 2026-08-21

### Fixed

- Non-rework review dispositions can retire malformed retained candidates
  without weakening the authenticated contract used by pending rework.

## 0.10.19 — 2026-08-21

### Performance

- Roadmap task-state joins share the dashboard refresh's bounded task-card
  projection. Complete snapshots avoid redundant routed point lookups, while
  partial snapshots keep the exact canonical fallback.

## 0.10.18 — 2026-08-21

### Performance

- NeedFix, Plan, Workforce and Collision now share one bounded task-card
  projection during a dashboard refresh. Complete snapshots also avoid
  redundant point lookups for historical links to absent tasks, while partial
  snapshots preserve fail-safe canonical fallback behavior.

## 0.10.17 — 2026-08-21

### Performance

- Review-decision latency is projected with one set-based SQLite query instead
  of one correlated lookup per decision row.
- Workforce and Preflight share one bounded readiness probe per dashboard
  refresh, reducing duplicate repository work without cross-refresh caching.

## 0.10.16 — 2026-08-21

### Fixed

- Snapshot input sharing is now strictly bounded to one dashboard read set;
  reused provider instances cannot carry task or cost projections into a later
  refresh, while independent dashboard instances remain parallel.

## 0.10.15 — 2026-08-21

### Performance

- Parallel dashboard readers now share one snapshot-scoped task-card and
  cost-ledger projection instead of repeating the same SQLite checks and JSON
  decoding. The canonical full refresh improved by roughly 19% without a
  cross-refresh cache or stale-state window.

## 0.10.14 — 2026-08-21

### Fixed

- Archived/finished task worktrees with broken Git registration can now enter
  reversible quarantine only when the exact request ledger and canonical task
  lifecycle independently prove terminal ownership. Unknown, foreign and live
  worktrees stay protected.
- Cleanup reads terminal owner identities once per batch, avoiding repeated
  task-table scans for large retention waves.

## 0.10.13 — 2026-08-21

### Performance

- Storage retention reuses a bounded append-aware process-event projection
  rather than decoding the entire multi-gigabyte historical ledger on each
  refresh. Warm ledger reads now complete in about 10 ms on the canonical
  repository and complete retention previews in roughly 0.27-0.35 seconds.
- All non-append lifecycle changes invalidate the projection and replay the
  canonical ledger, preserving spill ordering and fail-closed evidence.

## 0.10.12 — 2026-08-21

### Performance

- Source Graph full rebuilds avoid millions of repeated Python regular-
  expression compilations during imported-call resolution. The canonical
  823-file build fell from 66.82 seconds to 18.60 seconds with identical
  deterministic indexing semantics.
- Build health now includes phase-level timing evidence for the complete
  Source Graph pipeline.

## 0.10.11 — 2026-08-21

### Performance

- Windows and Linux workers now materialize declared sparse worktrees instead
  of checking out an entire repository for every task. Finalization preflight
  remains mechanically verified and completes in milliseconds on the
  canonical repository.
- Request-scoped event lookup uses an identity-safe warm projection rather
  than reparsing the complete process ledger for every worker tool call.

### Fixed

- Timed-out Windows worktree removal has a bounded request-owned cleanup/prune
  fallback and reports the exact failing command.
- Literal `<code>` and `<implementation>` semantic-edit placeholders are
  rejected before file mutation.

## 0.10.10 — 2026-08-20

### Fixed

- Byte-identical VS Code LM edits are now verified no-ops instead of false
  changed paths, eliminating stale required-output and retry loops.

## 0.10.9 — 2026-08-20

### Fixed

- Windows worker finalization and review acceptance now use a configurable,
  bounded Git probe with exact process-tree cleanup and a complete mechanical
  worktree-manifest fallback. Windows Preflight exercises the same isolated
  zero-diff path and reports phase timings instead of returning a false Ready.

## 0.10.8 — 2026-08-20

### Fixed

- Text-only GLM workers now preserve exact Source Graph `file`/`body` modes and
  targets instead of reusing the broad orientation `focus` example.

## 0.10.7 — 2026-08-20

### Fixed

- Closed NeedFix records no longer leave stale task lineage pinning retained
  worktrees indefinitely. Ledger-owned broken checkouts are reclaimable only
  through reversible quarantine with terminal ownership revalidation.

## 0.10.6 — 2026-08-20

### Fixed

- Storage ownership survives pruned Git worktree registrations through exact,
  fail-closed request-ledger binding, without restoring per-worktree process
  spawn overhead or weakening quarantine safety.

## 0.10.5 — 2026-08-20

### Fixed

- Worker Source Graph rework overlays are composed from one canonical read and
  a bounded in-memory worktree delta, with deterministic shadow/tombstone
  semantics and no query-time index build or database copy.
- Older compatible task stores no longer fail startup while review-event
  performance indexes are installed.

## 0.10.4 — 2026-08-20

### Performance

- NeedFix dashboard derivation reuses one bounded task-card snapshot instead of
  repeating point reads for list and count.
- Review decision/latency telemetry uses indexed task-event chronology instead
  of correlated full-history scans.

## 0.10.3 — 2026-08-20

### Performance

- Full dashboard refreshes omit the large per-request protected-log detail that
  the UI does not consume, reducing the measured payload by about 62.5% while
  preserving aggregate retention truth.

## 0.10.2 — 2026-08-20

### Performance

- The Storage card restores a repository-bound last-known-good inventory across
  runtime/window reloads instead of returning to `Calculating` while the full
  multi-gigabyte scan repeats.
- Background storage refreshes are less frequent and preserve stale values until
  the next atomically completed measurement is ready.

## 0.10.1 — 2026-08-20

### Fixed

- Fast summary refreshes no longer replace a full dashboard snapshot with
  `No sample` / `Unavailable` placeholders while the full read is in flight.
- Windows review acceptance and finalization use bounded Git probes with exact
  process-tree cleanup and phase-specific errors instead of a 120-second
  `git rev-parse HEAD` hang.

## 0.10.0 — 2026-08-20

### Performance

- The dashboard posts its bounded summary first, so counters and health cards
  appear in roughly two seconds instead of waiting 20–30 seconds for the full
  payload.
- Full snapshot reads hydrate concurrently with a core-derived worker count;
  measured local construction fell to about three seconds.
- Storage inventory begins during the health handshake and a completed scan is
  folded into the same full response, preventing a stale `Calculating` card.

## 0.9.99 — 2026-08-20

### Performance

- The manager dashboard summary no longer builds and discards the full
  workforce, cost, KPI, process, task-plan, NeedFix and Roadmap payload. Measured
  summary construction fell from 6.52 seconds to 0.35 seconds.

### Fixed

- Windows worker workspace verification now reads bounded Git administrative
  metadata instead of launching `git symbolic-ref` and two `git rev-parse`
  children after checkout, removing the exact 120-second provisioning hang
  reported on AIWorkHub 0.9.98.

## 0.9.98 — 2026-08-20

### Performance

- The Storage card now avoids an N+1 repository-identity lookup across hundreds
  of terminal-log quarantine batches. Measured cold calculation on the live
  19.9 GB store fell from 25.87 seconds to 3.44 seconds.
- Storage directory sizing uses a lower-syscall traversal and never re-walks or
  double-counts the nested worker tree.
- Context Graph and AI Memory searches no longer run schema or FTS repair on
  every read.

## 0.9.97 — 2026-08-20

### Performance

- Incremental Source Graph refreshes now resolve only changed Python callers
  and added, removed or renamed function identities instead of scanning the
  repository-wide call graph.
- The Source Graph quality joins now use a dedicated qualname index. Measured
  refresh time on the AIWorkHub repository fell from roughly 55 seconds to
  roughly 5 seconds.
- Task-plan and collision snapshots now decode all bounded cards in one SQLite
  read instead of reopening the database once per task.

## 0.9.96 — 2026-08-20

### Fixed

- Long-running VS Code LM workers are no longer cancelled merely because the
  model has not emitted a new visible response for ten minutes. A fresh exact
  process identity and heartbeat keep the task alive; elapsed quiet time is
  reported only as observability and never as terminal evidence.
- Explicit provider errors, verified process exit and owner cancellation retain
  their existing terminal behavior.

## 0.9.95 — 2026-08-20

### Fixed

- GLM and other VS Code LM workers no longer get trapped at the final semantic
  edit stage. Create and replace-range operations now have separate strict
  schemas and concrete request examples, so valid edits are accepted without
  guessing operation names.
- A late session, memory, KB or Source Graph call during forced staging is
  corrected once without reaching MCP. A repeated protocol violation stops with
  one bounded, explicit reason instead of cascading through malformed JSON and
  `tool_not_allowed` failures.
- Stage and finalize remain offline bridge operations, with strict rejection of
  missing, hybrid and unexpected fields.

## 0.9.94 — 2026-08-19

### Fixed

- **AIWorkHub could stop responding until you reloaded the window.** Two
  operations competing for the same file could leave one waiting forever on
  Linux, with no timeout and no way back — the recovery for it existed but only
  ran on Windows. Reviews that appeared to start and then never did were this.
- **A running worker could be declared dead and its task closed**, because the
  check for "is this process alive?" answered differently in three places, and
  the answer for "running but not ours to signal" was wrong on Linux.
- **Stopping a task could leave a child process running.** The stop signalled the
  whole group but only checked the parent, so the forceful second stage never
  ran.
- **A surgical edit could be applied without checking the file had not changed**,
  and nothing said so. Unverified edits are now reported as unverified. Files
  using classic Mac line endings also lost a line break on edit.
- **The MCP server never started in Copilot.** The configuration written for it
  named the interpreter by a path Copilot could not resolve remotely, failing
  every time while the same program started fine elsewhere.

## 0.9.93 — 2026-08-19

### Fixed

- **Asking for a specific function or class by name returned nothing**, even when
  AIWorkHub had it indexed. It found the symbol and then discarded it on the way
  back. Because the tool's own suggestions use exactly that form, following its
  advice led nowhere and the work fell back to reading whole files — which is the
  cost this feature exists to avoid.
- A request for something outside a task's permitted files now says so by name,
  instead of returning an empty result that reads as "no such symbol".

### Added

- **AIWorkHub now asks the editor which models are available instead of carrying
  its own list.** That list had been written out in four places and knew one
  model while six were configured and working. A model your endpoint starts
  offering now appears on its own — no update, nothing to type. Names the editor
  does not report are still refused, and by name.

## 0.9.92 — 2026-08-19

Work that was finished and correct could not be accepted. This release fixes that
and three related faults.

### Fixed

- **A single failed internal lookup was enough to reject finished work.** The
  check that guards a task's records compared two counts that had drifted apart
  for a reason that had nothing to do with the work itself, and once rejected a
  task could only be redone, never accepted. Around ten tasks were lost to it
  before it was found.
- **A task waiting in the queue was counted as one already running**, so the
  check that decides whether new work can start reported a conflict for
  practically every launch — while the other check, looking at the same tasks,
  reported none.
- **Starting a review reported success before anything that could refuse it had
  run.** Eight tasks were found listed as running with nothing behind them, the
  oldest for fifteen hours. The refusal now happens before the task is committed
  to, and a review that was genuinely under way is no longer at risk of being
  discarded.
- **The review verdict used a status its own list of valid statuses did not
  contain** — introduced in the previous release while fixing a reviewer that
  could pass without reading anything. Related: an empty verdict no longer reads
  as a pass, and how independent a review was is now recorded from that review
  rather than from another one.

## 0.9.91 — 2026-08-19

Six fixes to AIWorkHub's own safety checks, found by an independent audit that
ran the code rather than reading it.

### Fixed

- **A reviewer that read nothing could still mark work as passed.** At the middle
  risk level two of the three reviewers were never checked for whether they had
  actually seen the code — and a review that produced no observation at all was
  counted as a pass. The strongest verdict the system gives could be assembled
  from reviewers that never looked. Now a reviewer that saw nothing never passes,
  at any risk level.
- **The gate said the project's own checks had vetted the work when none of them
  had run.** It now reports only what actually executed.
- **A task could quietly disarm the checks meant to police it** — keeping the
  same number of checks while replacing them with commands that do nothing. The
  comparison now reads what the checks contain, not how many there are.
- **Failures could arrive labelled "ready for review".** Any outcome the system
  did not recognise was delivered as if the work were finished and waiting. It is
  now treated as blocked.
- **A blocked task was locked forever**: reaching that state was allowed, leaving
  it was not, and a separate manual tool existed only to undo it. Leaving is now
  part of the contract.
- **The list of possible outcomes was written out six times in six places and
  three of the copies disagreed** — the underlying cause of the above. There is
  now one list, and a test that fails if any copy drifts from it.

## 0.9.90 — 2026-08-19

### Fixed

- **AIWorkHub can now tell you why a run died.** When a provider refused a task —
  no balance, an exhausted quota, a rejected key — AIWorkHub reported it as
  "the task failed", the same message it uses for broken code. Half of all
  blocked tasks carried that one message, so a problem you could fix in a minute
  looked identical to a bug. Refusals are now named for what they are.
- **A rejected key is no longer assumed.** A refusal that arrives without a
  stated cause now says the provider refused and did not say why, instead of
  telling you to re-authenticate a credential that may be perfectly fine.
- **Each provider now says whether its remaining quota can be seen at all**, and
  why not, rather than every provider showing the same unhelpful status.

### Notes

This release also publishes 0.9.87, 0.9.88 and 0.9.89 — the dashboard font
scale, the MCP config Claude Code could not read, the reasoning output that made
a working task look dead, the failure reasons that were missing or cut off, and
the review checks that now catch unreachable code and a stale version. They were
built and verified but never tagged, so no release was produced for them.

## 0.9.89 — 2026-08-19

Two fixes to AIWorkHub's own safety checks.

### Fixed

- **Work that changes nothing can no longer pass review silently.** A task once
  added 1,197 lines of correct, fully tested code that nothing in the running
  system ever called — and all three reviewers passed it, because tests call new
  code directly and that is what makes them green. The review now checks whether
  new code is actually reachable and names anything that is not.
- **Finishing an old task can no longer undo a newer release.** If a task was
  started before an update and finished after it, accepting the work quietly put
  the old version number back, which stops the extension from connecting to its
  own server. That has now happened twice and was caught by hand both times.
  Accepting such work is now refused, with the file and both version numbers
  named, before anything is written.

## 0.9.88 — 2026-08-19

Eight fixes. Two of them had been broken since 0.9.43 and 0.9.44.

### Fixed

- **The panel now follows your editor's font size.** Most of the dashboard text
  was hard-coded at 8-11px and ignored the font size you had already chosen in
  VS Code — raising it changed nothing. Every size now derives from your setting:
  the smallest text is about 11px at the default and grows with you.
- **AIWorkHub could not start its own server for Claude Code.** The config file
  Claude Code reads was written with a VS Code placeholder in it, which Claude
  Code cannot expand, so the connection failed with a message naming a token
  instead of a folder — and every update rewrote the file and broke it again.
  It now writes a real path, and refuses to save a config a reader cannot use.
- **A working task looked dead.** The panel dropped the model's live "thinking"
  output, so a task producing output normally showed an empty screen. People
  killed healthy runs because of it.
- **Valid output was labelled "unsupported".** An unfamiliar event now shows as
  readable raw output with a reason, never a blank panel and never an
  "unsupported" tag on output that was perfectly valid. Reselecting a task also
  can no longer leave two update loops running at once.
- **A failed task often could not tell you why.** Twenty of fifty-two blocked
  tasks carried no reason at all, so recovery was guesswork for more than a third
  of failures. A task can no longer be blocked without a recorded cause.
- **A failed check cut off its own explanation.** Test output was trimmed from
  the wrong end, keeping the passing lines and discarding the failing one, with
  nothing to say it had been trimmed. Failures now keep the part that explains
  them.
- **A missing file was reported as a corrupt file**, sending you looking for
  damage in something that was simply not there.
- **A task that hung before it ever started was invisible** to the health check,
  because that check looked for a running process and there wasn't one yet.

## 0.9.87 — 2026-08-19

Four fixes. Two of them are about waiting for something that should never have
been slow.

### Fixed

- **AIWorkHub no longer copies its whole code index before a retry.** Every time
  a task was sent back for rework, the system duplicated its entire 107 MB index
  of your repository first — and rework is the thing that happens most often. It
  now builds a small index of just the files that changed. On this repository
  that is 5 MB instead of 107 MB, and the wait no longer grows as your project
  does: a project with a 1 GB index costs exactly the same as one with 100 MB.
- **A cleanup job with no ending was locking the task database.** Ten of them
  were retrying every fifteen seconds against tasks that had been archived hours
  earlier, each holding the database long enough that no new review could start.
  Every review launch failed with "database is locked" — pointing at the wrong
  database. Cleanup for an archived task now stops, with the reason recorded.
- **Empty storage batches piled up instead of being cleaned.** A Windows install
  had 100 batches reporting "empty, 0 bytes" while the folder actually held
  3.68 GB. Empty batches are now collected when they appear, and only when the
  record and the disk agree that there is nothing there — so nothing is deleted
  on a stale claim.
- Reported and left visible rather than quietly assumed safe: three smaller
  issues found while accepting this work are written down in the project's fix
  list instead of being folded into these notes.

## 0.9.86 — 2026-08-18

Nineteen fixes across five accepted tasks.

### Fixed

- **Work under review could be deleted.** When a task's saved work failed its
  integrity check, AIWorkHub deleted the whole workspace. That is the worst
  possible answer to "this might have been tampered with" - it destroys the only
  thing anyone could look at to find out. It happened here this morning and took
  264 lines of finished, verified work with it, silently. Failing work is now
  quarantined, the task is marked blocked with the reason, and every removal is
  written to the audit log.
- **A task's code could run outside its sandbox.** A helper that checks whether
  the test tools are installed ran on the host machine with the task's own files
  on the import path, so a task could have placed a file there and had it
  executed with AIWorkHub's own permissions. Closed two independent ways.
- **A task could declare its own result.** The system trusted certain exit codes
  as proof that a command "could not run here" rather than "failed" - but a test
  can produce those exit codes deliberately, so a genuine failure could present
  itself as a recoverable environment problem.
- **Process identity lost a digit**, and two screens disagreed about it by one.
  That number is what distinguishes a live worker from a recycled process id, so
  a running worker could be judged foreign or a dead one accepted as alive.
  Reported from a Windows install.
- **AIWorkHub recorded nothing from Claude.** On a Claude-only setup the context
  history was empty, and the status simply said "not configured" instead of
  failing - so every conversation recovery had nothing to recover from.
- **Rejecting a task left its reviewers in the queue forever**, three at a time,
  until the real work was impossible to find among them.

### Added

- Continuous Audit as a Service is now upheld by the system itself rather than by
  remembering to check, and the audit layer runs its first real pass: one narrow
  scope, read-only, with findings recorded and traceable to the pass that found
  them.

### Stated, not hidden

Four known limits are written into the release notes rather than left to be
discovered: which compliance properties are actually observed versus
self-reported, a command-line check that only handles one chaining operator, a
counter that conflates two different situations, and a task cut before a release
that can silently carry an old version number forward.

## 0.9.85 — 2026-08-18

### Changed

- **Starting a quality review used to take twenty to thirty minutes before
  anything happened.** It now takes about half a minute.

  A reviewer has to see the code as the task changed it, not as it was, so it
  needs its own copy of the project index. AIWorkHub was making that copy by
  duplicating the entire index — 107 MB here — for every single reviewer, to
  change the six files the task actually touched. Ninety-nine percent of the
  copy was identical to what it was copied from.

  AIWorkHub now indexes just the changed files, separately, and links that small
  index to the main one when answering questions. Measured on this project:
  **107 MB and about 25 minutes became 5.3 MB and 30 seconds.**

  The part that matters for bigger projects: the new cost depends on how much
  changed, not on how large the project is. A project with a 1 GB index pays the
  same 5 MB and 30 seconds for the same six files. The old design would have
  meant hours of waiting before a single review could begin.

### Known limitations, stated rather than left to be found

- Retrying a task still makes the old full copy; that path is tracked separately.
- The link between the small index and the main one is checked by size and
  timestamp, not by content hash.

## 0.9.84 — 2026-08-18

### Fixed

- **The audit trail credited the wrong model for the work.** Every manager action
  was recorded as if Codex performed it, including the ones Claude performed, because
  the runner name was hardcoded rather than taken from whoever actually held the
  manager seat. If you read the ledger to find out who accepted a change, it told you
  the wrong thing. Actions now carry the real manager, and an action that cannot be
  attributed fails instead of guessing.
- **Rejecting a card left its reviewers stuck in the review queue forever.**
  Accepting cleaned them up; rejecting did not. Since a quality-gated pipeline rejects
  by design, the queue filled with three dead entries per rejection until the real
  work was impossible to find. Fourteen piled up here in one day.
- **After a window reload, a stale manager session still reported itself as
  verified.** The identity it named no longer existed, but nothing said so. It now
  fails loudly instead of quietly answering the wrong question.
- **A task could be started with a write scope that did not include the file it
  needed to change.** The worker cannot see this - it only sees permission denied -
  so it retries variations in the wrong files until its budget runs out. Creating a
  card now warns, by name, when a file the card's own evidence points at is missing
  from its scope.
- **Listing a folder in a task's write scope was accepted, then refused at the
  end**, discarding a completed run's output. Both ends agree now, and the problem
  surfaces when it costs nothing.
- **A database opened "read-only" was writable if its path contained a `#`.** The
  character silently truncated the read-only flag, so the connection could write -
  and wrote to a different file than the one named. Fixed everywhere in the package
  and verified by sweeping the whole repository, not by looking.
- Three repair tools for half-archived task rows existed but nothing could call
  them. They work now.

## 0.9.83 — 2026-08-18

### Fixed

- **Work that was finished and correct kept being thrown away for reasons that had
  nothing to do with the code.** Four tests depended on where they happened to be
  run - a home directory the sandbox does not have, a temporary path that changes
  how pytest names tests, a file permission the sandbox refuses, and a nested
  launch it cannot complete. Any card whose checks touched one of them was marked
  failed, and that particular failure cannot be retried, so the only way out was
  to throw the card away and start again. All four are fixed and each was proved
  in both directions before being changed.

- **A task that finished and was then blocked never told anyone.** The
  notification was suppressed as a duplicate of the earlier one, so the task sat
  blocked with nobody informed. Recovery of a lost notification also poisoned
  itself and could only ever be attempted once. The delivery thread had no
  protection: a single unexpected error killed it for the whole repository while
  the health panel still showed green.

- **Asking about an archived task crashed the status tool.** Archiving is often
  the only way to close a stuck task, so this was not a rare corner. A replaced
  task also still showed as waiting work, creating a task that already existed
  handed back a dead one as if it were usable, and one blocked task at the front
  of the queue stopped every ready task behind it from starting.

- **A task could lower the bar it was being judged against.** Emptying its own
  quality settings weakened its own review; a repository asking for a stricter
  minimum produced a block that could never be cleared; a test report with zero
  tests counted as a pass on one path and a failure on the other; and diagnostics
  sent to a retry were being mangled in a way nothing could detect.

- **Cleaning up old files could lose them.** If the cleanup was interrupted
  part-way, files were moved with no record of where they came from and nothing
  could put them back. Cleanup also reported "0 bytes freed" for exactly the case
  holding gigabytes, and the recovery action the Storage panel tells you to run
  had no button anywhere.

- **NeedFix counts did not fall when the work behind them was finished.** The code
  that links a NeedFix to the task fixing it was never given what it needed, so
  that path never ran outside tests.

## 0.9.82 — 2026-08-18

### Fixed

- **Source Graph could answer a search as finished when it had stopped a third of
  the way through, and could return the wrong lines for a symbol.** A page with
  nothing wrong ended the scan permanently, so a clean result could mean "nothing
  found" or "we stopped early" with no way to tell. Reading a symbol used the line
  numbers from the last index rather than the file as it is now, so edits made
  after indexing could return the wrong code. Both are fixed, and every read now
  says how fresh it is.

- **The bug analyzers reported "clean" for languages they cannot read.** Five
  detectors are C-family only, ran against Python anyway, and reported no findings
  across 395 files - which reads as checked and clean. They now say plainly that
  they do not apply.

- **A worker that committed inside its own worktree lost the work.** The change
  set was compared against a moving reference, so committed work disappeared and
  the task still reported its required outputs as validated. Nothing is lost now,
  and an unexplained move fails loudly.

- **The manager could launch conflicting tasks in parallel.** The default plan
  view was missing every write-conflict field, so it showed a task as safe while
  the full view reported real conflicts.

- **A stalled runtime kept advertising itself as available.** One transient write
  error - a full disk is enough - could wedge the heartbeat permanently and remove
  a healthy runtime from routing, while dropped requests waited for a timeout.

- **Storage "Calculating" took eighty seconds** on a 17 GB repository, fifty-five
  of them in a single scan. It is bounded now, and a measurement that is cut short
  says so instead of looking complete.

## 0.9.81 — 2026-08-17

### Fixed

- **The AIWorkHub runtime could report itself unavailable on a large repository.**
  Startup ran a full reconciliation scan of every task and every retained
  workspace before answering the editor, which took long enough that the editor
  gave up and showed "MCP runtime unavailable" even though the runtime was
  healthy. That scan now runs in the background: startup answers in well under a
  second and reconciliation converges behind it.

## 0.9.80 — 2026-08-17

### Fixed

- **Live Output stopped polling forever the first time it had nothing to show.**
  Selecting a task right after launching it — exactly when you want to watch it —
  returned "output unavailable", and the poll chain was never restarted. Seconds
  later the worker was streaming and the panel still showed the error, permanently,
  until you selected the task again by hand. The chain now restarts on every
  outcome, and a not-there-yet response reads as a transient state that says what it
  is waiting for.

- **A late task-detail reply could resurrect a panel you had already left.** If a
  task was archived in another window while its details were still loading, the
  panel came back when the reply landed and the Archive and Restore buttons bound to
  a task that was no longer in the table. A reply that lost its race is now dropped.

- **An idempotency key could silently swallow another file's edit.** Two semantic
  edits sent with the same key but different targets counted as a repeat: the second
  was never written and still reported success, carrying the first file's path.
  Workers that derive the key from a task id lost every edit after the first.

- **Storage grew without a working bound.** Quarantined directories that no batch
  claimed could never be purged, empty batches had no collector, and worker logs and
  attempt bundles had no per-file limit. Each of those now bounds itself, an
  oversized terminal log keeps the diagnostic tail the launcher reads, and a live run
  is never touched.

- **Archived tasks stayed live and held their worktrees.** A row could carry an
  archive timestamp while its status stayed open, so the task never disappeared and
  its worktree could never be reclaimed. Archiving is now a single guarded write that
  verifies itself after committing.

- **The NeedFix list showed records that were already handled.** A rejected entry
  reappeared and a converted one read as open, because the panel and the tools read
  the raw store instead of the derived state. They now derive at read time, and say
  so when they cannot.

## 0.9.79 — 2026-08-17

### Fixed

- **A card that changed a generated file could never be reviewed.** If a change
  touched an eval artifact, a fixture, or anything else Source Graph deliberately
  does not index, the reviewer never started — the launch reported success and
  then nothing happened, so the work could never be accepted however complete it
  was. Prewarm now skips a deliberately excluded path and the reviewer still
  runs, working from the sealed review packet, which already carries the
  candidate content. A genuine indexing failure on a file that should be
  indexable is still refused loudly.
- **The storage retention preview could not finish measuring.** It timed out
  after 90 seconds with nothing measured, so no cleanup candidate was ever
  produced and the footprint only grew. On a 29 GB repository it now completes in
  under nine seconds and reports what is actually reclaimable. A preview that
  does hit its deadline now shows the candidates it did establish, marked
  partial, instead of an empty list that looked like a clean repository.

## 0.9.78 — 2026-08-16

### Fixed

- **On macOS, process identity did not exist.** A pid alone is not an identity —
  the operating system reuses pids — so the launcher could mistake a reused pid
  for a live worker. Darwin now supplies a real process creation time, and the
  eight launcher regressions that failed on every macOS CI run now pass rather
  than being skipped. Identity now lives in one place shared by the launcher, the
  supervisor and the temp collector, and an unknown identity fails closed: a
  runtime directory whose owner cannot be identified is never reclaimed.
- **The manager callback now follows whoever holds the manager seat.** Codex kept
  its existing push path unchanged; a Claude manager is now woken through its own
  channel instead of only receiving work while it happened to be waiting. A
  provider with no push transport says so plainly rather than silently leaving the
  manager to poll.

## 0.9.77 — 2026-08-16

### Fixed

- **If you have only one model provider installed, finished work could never be
  accepted.** The acceptance gate demanded a review from a different vendor than
  the one that did the work, so on a single-provider setup nothing could pass,
  however complete and green it was. Reviewer independence is now a recorded
  ladder that degrades to a fresh-context review by the same model, and the
  achieved level is written into the acceptance evidence so you can see exactly
  how independent each review was. A reviewer that cannot actually read the
  review packet is still refused, and every other safeguard is unchanged.

## 0.9.76 — 2026-08-16

### Fixed

- **0.9.75 shipped without its bundled Python runtime and the dashboard could
  not start.** That build was packaged with a raw `vsce package` call instead of
  the repository's own packager, which is what stages the runtime and the mux
  launcher into the extension. A reloaded window reported
  `bundled_mux_runtime_missing` and the dashboard never came up. If you are on
  0.9.75, install this build. The symptom only appeared after a window reload,
  because until then the extension host was still running the previous build.
- The storage retention preview no longer hangs, and retention can no longer
  reclaim a worktree that a task is still working in.
- The workforce catalog no longer claims a worker is `ready` when it has never
  seen that worker's quota; it says `ready_unverified` and gives the reason.

## 0.9.75 — 2026-08-16

### Fixed

- The context audit trail no longer disagrees with itself about whether a task
  is required. Knowledge Base and AI Memory writes made by a manager outside any
  task genuinely have no task, and they were being stored with an empty-string
  `task_id` because the code called the field optional while the schema declared
  it mandatory. Absence is now stored as `NULL`, and the code and the schema
  state the same contract.
- A context write that fails an integrity constraint now names the offending
  column, instead of reporting only `SQLITE_CONSTRAINT_NOTNULL` and leaving the
  caller to guess which of twelve columns was at fault.
- The `context_mutations` migration is atomic: the rebuild runs in a single
  transaction and verifies the copied row count before committing, so a partial
  failure can no longer strand audit rows while reporting success.

## 0.9.74 — 2026-08-16

### Security

- A quality lens can no longer be satisfied by a reviewer that could not inspect
  anything. A reviewer whose sandbox gives it no file-read tool cannot open the
  review packet it is handed, yet its empty report came back shaped identically
  to a real review — so a required lens was satisfied by an inspection that
  never happened.
- The gate now marks such a lens `reviewer_could_not_inspect` on a positive
  signal only: findings that are all `process_limit`, or usage telemetry that is
  present and records zero activity.
- Missing telemetry stays "unknown" and keeps satisfying the lens, because most
  honest reviews carry none; demanding proof of inspection rejects real work.

## 0.9.73 — 2026-08-16

### Security

- A read-only SQLite open is now actually read-only. Every such connection was
  built by string interpolation, and in SQLite URI syntax a `#` in the path
  swallows the `?mode=ro` query — so the database opened read-write with
  create-if-missing, at a path truncated at the `#`. Verified: the old form
  accepted a write and left a stray file behind; the new form refuses both.
- All eight affected call sites now go through one shared helper with two
  independent guarantees: the path is percent-encoded so the query survives,
  and `PRAGMA query_only = ON` refuses writes even if URI parsing were
  bypassed.
- Each converted call site keeps its exact previous timeout, so behaviour
  under concurrent load is unchanged.

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
