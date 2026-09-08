# AIWorkHub for VS Code — Changelog

## 0.12.1 — 2026-09-08

### Fixed

- Release qualification could not run at all: it installed pytest without the
  parallel plugin the project requires, so every platform job failed in under
  half a minute without running a test.
- A seeding test asserted the exact recipes withheld on the machine that ran
  it, which made it pass locally and fail on CI.

## 0.12.0 — 2026-09-08

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

