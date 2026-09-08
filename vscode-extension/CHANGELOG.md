# AIWorkHub for VS Code — Changelog

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

