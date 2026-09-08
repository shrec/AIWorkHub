# Changelog

All notable changes to AIWorkHub are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project has
noted by package/extension version and release tag.

## [Unreleased]

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

