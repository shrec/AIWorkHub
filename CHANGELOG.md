# Changelog

All notable changes to AIWorkHub are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project has
noted by package/extension version and release tag.

## [Unreleased]

## [0.12.0] - 2026-09-08

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

