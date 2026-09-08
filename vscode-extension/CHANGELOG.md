# AIWorkHub for VS Code — Changelog

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

