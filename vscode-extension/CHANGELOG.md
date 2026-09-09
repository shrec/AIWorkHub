# AIWorkHub for VS Code — Changelog

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
