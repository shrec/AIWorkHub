# Automatic wave mini-roadmap

## Intent and authority

The mini-roadmap must describe the current development wave without a manager
rewriting its title, milestone, or task list after every intermediate release.
The installed AIWorkHub version is runtime fact; a wave milestone is a target,
not proof that a release was installed or that its goals were achieved. Goal
completion comes only from canonical accepted task evidence. Each wave goal
declares which numbered Roadmap acceptance criteria it proves; every criterion
must be covered by an accepted task whose card validates the measured claim before
the outcome can auto-complete. The repository's
own `.aiworkhub/` stores all wave and task state. A dashboard read never repairs
that state, and automatic writes require `AIWORKHUB_ALLOW_WRITES=1`.

## Chosen design

Keep the wave's original target milestone as historical evidence. The system
must never move a missed target to the next patch merely because a new release
was installed: that would hide schedule slippage. A read-only current-wave
projection joins the active Roadmap outcome with the verified installed runtime
version. It uses a version-neutral heading, labels the milestone as a target,
and marks a passed target with unfinished goals as overdue. If there is no
unambiguous active wave, it reports UNKNOWN instead of selecting an arbitrary
one. A newly planned wave is a new Roadmap outcome; release installation alone
does not invent goals or close an outcome. When every declared goal has exact
accepted evidence and no unresolved criterion, the write-gated reconciler
transitions that outcome to completed with a durable evidence summary.

Task successors need an explicit, same-repository lineage identity. Add an
optional wave-goal binding to task creation with a goal ID and, when replacing
work, the exact predecessor task ID. The task card stores this durable binding.
An idempotent projection reconciler transfers the active goal's current-task
pointer to the new card only after validating that the predecessor belongs to
that goal. The old card remains in history, but an archived or rejected
predecessor cannot count as goal completion. If a task is created without a
binding, the system must not infer one from a title, version suffix, topic, or
prose; the goal stays open/UNKNOWN until an explicit binding exists. This makes
new cards update the mini-roadmap without a separate manual Roadmap edit.

The dashboard serves one authoritative active-wave projection with installed
version, original target version, overdue state, short goal labels, exact current
task IDs, and per-goal state. The VS Code popup renders that projection and
does not pick the highest semver from a list of independent Roadmap outcomes.
`finished`/accepted evidence may check a goal; `archived`, `blocked`, missing,
ambiguous, or stale evidence cannot. The read path never silently changes
Roadmap, Task, or NeedFix state.

## Boundaries and failure handling

- The goal-binding reconciler is the single writer of current task pointers.
  It uses bounded reads and an atomic Roadmap update with a durable event
  containing the old and new exact task identities. It never rewrites the
  original target milestone.
- Task creation remains the source of task identity. The cross-store binding
  is repairable: a failed Roadmap projection leaves the durable task binding
  intact for the next scan, rather than losing or guessing it.
- With writes disabled, an unapplied successor binding is reported as pending;
  the dashboard shows UNKNOWN rather than a falsely completed goal.
- A release is not a goal verdict. An accepted task is not a full outcome
  verdict if another required current task or measured acceptance is missing.
- An outcome with zero goals, missing task evidence, or an acceptance criterion
  without a mapped task remains UNKNOWN, never auto-completed.
- Existing waves without criterion-to-goal coverage remain visible and open;
  the migration records an explicit mapping once, never guesses from prose.
- Existing RM-2026-00065 and RM-2026-00066 remain historical/canonical data;
  migration is a one-time explicit mapping, not a heuristic over old task
  names.

## Verification

Use the observed 0.11.51 wave on installed 0.11.53 as a fixture. Verify the
popup says installed 0.11.53, target 0.11.51 overdue, and unfinished
Playbook/LSP/delta-review goals remain open. Neither read nor release creates
a replacement outcome or changes that target. New exact successor task binding
must replace the current pointer; re-running reconciliation must not add
another event. An unrelated or foreign-repo task must fail closed. Test
concurrent scans, write-gate-closed behavior, unknown versions, missing
task evidence, archived predecessors, automatic completion only after every
goal's evidence is accepted, and dashboard popup rendering. Validate
with focused Python and VS Code extension tests, Roadmap/Task lifecycle tests,
Ruff, and `git diff --check` before acceptance.
