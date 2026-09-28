# Automatic cleanup janitor — design (S1)

Date: 2026-09-29 · Program: Coding Factory, close-pipeline set S1 → S2 → S3 (S1 cleanup, S2 one-step close, S3 same-file parallel merge)
Owner goals served: Goal 1 (bookkeeping happens in WorkHub, not in a model) and Goal 2 (no mechanical duty left on the model).

## 1. Outcome

Owner's rule, verbatim:

- "სამუშაო ხე მანამ გინდა სანამ ხეზე მუშაობას აგრძელებ" — a worktree is needed only while work on it continues.
- "ტასკი დასრულდა მივიღეთ ხეში უნდა ამოიშალოს ნიდ ფიქს ლისტიდან მისი დაგიც უნდა გაიწმინდოს" — once a task is accepted into the tree, its NeedFix leaves the list and its traces are cleaned.

So every artifact lives exactly as long as the work that owns it. Once that work is **decided** (accept, reject, supersede, archive, cancel), WorkHub removes the artifact on its own. No model turn and no human does cleanup.

Success criteria (measured, not asserted):

1. Within 60 s of an accept, reject, supersede or archive, the task's worktree and worker home are gone from disk and from `git worktree list`.
2. After the first sweep on this repository, the retention preview protects only live workers and current review requests; quarantine holds 0 bytes of AIWorkHub-owned items; 0 worktree registrations are prunable.
3. 0 resolved NeedFix outside the archive, and 0 NeedFix in `task_created` whose task is missing, or was archived or superseded without an accept.
4. 0 `pending` or `dead_letter` callbacks belonging to decided tasks.
5. A failing janitor step produces exactly one NeedFix (deduplicated), never silence and never one per sweep.
6. No regression: rework after cleanup still works from the sealed rework delta, and the current review request never loses its worktree.

## 2. Measured baseline (2026-09-29, this repository)

| Item | Now | Why it is still there |
|---|---|---|
| Owned worktrees | 22, 3.50 GB | 9 `rework_predecessor_retained` (pinned by tasks from 09-23/24), 6 `blocked_terminal_candidate_retained`, 6 `under_age` (`terminal_runs_days` = 30), 1 `live_worker` |
| Unattributed worktrees | 6, 45 MB | owner cannot be proven |
| Quarantine | 35 batches, 109 worktrees, 17.7 GB | 7-day undo window; **0 of 109 ever restored**; deadlines 2026-10-04…05 |
| Stale worktree registrations | 109 of 184 prunable | nothing prunes them automatically |
| Task rows | finished 107, superseded 200, blocked 110, review 13, pending 10 | nothing archives decided tasks; 12 review and 4 pending rows date from 2026-08-01…08-10 |
| NeedFix | resolved 151 not archived; `task_created` 83 (81 point to a missing or dead task); `converting` 1 | resolution stops at `resolved`; dead links are never cleared |
| Callback outbox | pending 15, dead_letter 55 | rows of decided tasks are never closed |
| process_logs / rework_deltas | 645 MB / 154 MB | age policy (`logs_days` = 7) plus a 7-day quarantine |

About 22 GB is on disk; the only part still needed is the live worker's tree.

## 3. Root cause

Three cleanup lanes exist, and none of them is keyed to the decision.

- **Lane A — direct GC after a decision.**
  - Path: `core.mark_done` → `_reconcile_retained_workspaces` (core.py:5610) → `ProcessManager._gc_finalized_workspaces` / `_gc_finalized_workspace` (process_launcher.py:10990, 11132).
  - It deletes directly, but `GC_CANDIDATE_PROCESS_STATES = TERMINAL_PROCESS_STATES - {"blocked"}` (process_launcher.py:842) skips every blocked attempt.
  - Its caller runs it in a private thread with `except Exception: pass`, so failures vanish.
- **Lane B — the storage hygiene daemon.**
  - Path: `schedule_repository_cleanup` → `_run_repository_cleanup` (storage_retention.py:3436, 3305).
  - It treats worktrees as a cache: age (`terminal_runs_days`) and size (`worktree_max_bytes`) decide, not the task.
  - Removal goes to a 7-day quarantine.
  - It protects blocked candidates and rework predecessors with no end date.
  - `terminal_log_retention.enforce` (terminal_log_retention.py:1829) applies the same pattern to logs.
- **Lane C — `storage_retention.cleanup_accepted_artifacts`** (storage_retention.py:3240). It is keyed to the accept, but it has no production caller; it appears only in `__all__` and tests.

Two further causes:

- **The rework pin outlives the need.** `_gc_disposition` (process_launcher.py:11007) holds a predecessor's worktree while its task is in `REWORK_RECOVERABLE_STATUSES` (task_fsm.py:61, which includes `blocked` and `pending`). Rework does not need that worktree once a sealed delta exists (`test_rework_delta_artifact_materializes_after_predecessor_cleanup`), and `_gc_disposition` already releases the pin in that case. What is missing is anything that seals the delta before the attempt goes idle.
- **Bookkeeping has tools but no trigger.** Task archive, NeedFix archive, callback closing and registration prune each exist as manual tools. Today each is a manager turn.

**Why quarantine exists.** terminal_log_retention.py:1850 records that "this repository has spent enough of its history letting retention destroy things that were still wanted." This design answers that concern in a different way: first secure the durable facts (receipts, events, usage rows, the sealed delta, and the accepted files in the tree), then delete the bytes. Quarantine stays only for items whose owner cannot be proven.

## 4. Design

### 4.1 One janitor, one trigger path

`_run_repository_cleanup` becomes the janitor. It is already single-flight and repository-locked, and it is already scheduled from four places:

- `ProcessManager._retention_event` (process_launcher.py:5292);
- startup (`default_manager`, process_launcher.py:14050);
- `server._enforce_terminal_retention_safely` (server.py:4959);
- deadline wakeups.

`core._reconcile_retained_workspaces` stops running its own thread and instead calls `storage_retention.schedule_repository_cleanup(root)`, so every decision point reaches the janitor. Lane A's GC becomes step 1 of the janitor.

No pending/reconciled event pair is needed. A decided task that still has a worktree is itself the durable "cleanup pending" marker, so a crash mid-sweep is healed by the next sweep. (This simplifies the chat design, which proposed event pairs at the accept sites; the guarantee is the same.)

The steps run in the order below. Each step is isolated: its failure is recorded in its nested result, and the remaining steps still run.

1. worktrees of decided work (§4.3, §4.4);
2. rework deltas of decided tasks;
3. process logs of terminal requests whose usage is in the DB;
4. the quarantine backlog, plus the unattributed lane (unchanged);
5. prune stale worktree registrations;
6. records: tasks, NeedFix, callbacks (§4.5).

### 4.2 Lifetime table

| Artifact | Lives until | Then |
|---|---|---|
| worktree + worker home | launch until the task is decided, or until a terminal attempt that no recovery path reads | deleted, not quarantined |
| rework delta | the rework chain's task is decided | deleted |
| process log | the request is terminal and its usage row exists | deleted |
| task row | finished or superseded | archived (reversible). **Blocked, review and pending rows are never auto-archived**: they await a manager decision |
| NeedFix | its task is accepted | resolved, then archived. If its task link is dead, the link is cleared and the item returns to its open status |
| callback | delivered, or its task is decided | a `pending` or `dead_letter` row of a decided task moves to `superseded` with `last_error = "task_decided"` |
| worktree registration | its directory exists | pruned |

These are kept as durable history: task events, receipts, usage rows, `accepted_outcome_receipt`, and git history.

### 4.3 Eligibility: one function

`ProcessManager._gc_disposition` becomes the only eligibility authority. For AIWorkHub-owned worktrees, Lane B's preview asks it instead of using its own `protected` reasons.

Changes:

- **Blocked attempts become GC candidates.** Remove `- {"blocked"}` from `GC_CANDIDATE_PROCESS_STATES`; the attempt is over.
- **Seal before release.** A pinned rework predecessor that has no verified sealed delta gets one first, sealed by the janitor with `worker_workspace.seal_rework_delta_artifact` (worker_workspace.py:2965). The existing `sealed_rework_delta` branch then releases the pin. This covers the 9 pinned predecessors from 09-23/24. A timed-out attempt that recovery can promote to predecessor is already sealed by the existing timeout path. If sealing fails, the worktree stays and a NeedFix is filed: no silent retention and no unsafe delete.
- **Age and size stop deciding for owned worktrees.** `terminal_runs_days` and `worktree_max_bytes` remain only as the backstop for unattributed worktrees.
- **These guards stay as they are:**
  - a processing task's worktree is never touched;
  - `current_review_request` is never touched;
  - `finalize_failed` on an undecided task is kept, because `retry_finalization` reads it;
  - `_process_proven_dead`;
  - `assert_gc_safe_workspace_shape`;
  - the integrity quarantine of a current review request (process_launcher.py:11295).

### 4.4 Delete, don't quarantine, what we own

- **Worktrees.** A decided owned worktree goes through `cleanup_workspace` (worktree remove plus home). Unattributed or foreign worktrees keep the existing quarantine and 7-day window, unchanged.
- **Process logs.** Usage backfill runs first; it is the function behind `aiworkhub_dashboard_terminal_log_usage_backfill`. After that, a terminal request's logs are deleted. Logs of live requests are untouched. `logs_days` remains only as the backstop for logs whose request cannot be resolved. If backfill fails, the log is kept.
- **Rework deltas.** They are deleted when their task is decided.
- **Quarantine batches.** An authenticated batch (`_authenticated_manifest`) whose items all belong to decided tasks is purged on the next sweep, without waiting for its deadline. A batch with any unknown item keeps its deadline. After this change no owned item enters quarantine, so in practice this rule clears the 17.7 GB backlog once.
- **Lane C is retired.** `cleanup_accepted_artifacts` and its tests are deleted. One deletion path serves every decision, and the idempotent sweep already gives it resumability.

### 4.5 Records bookkeeping

- **Tasks.** `finished` and `superseded` rows are archived through the existing archive path. Metrics that count accepted work must read the accepted outcome (events or receipt), not the status column. The plan checks `sdlc_outcome_metrics` and the dashboard counters for this.
- **NeedFix, resolved.** Every `resolved` NeedFix whose task is finished moves to `archived`. `_close_accepted_task_needfix` already resolves at accept; the janitor finishes the job.
- **NeedFix, dead link.** A NeedFix in `task_created` whose `converted_task_id` is missing, or points to a task archived or superseded without an accept, gets its link cleared through the existing reopen path (`needfix_reopen_superseded_task_link`). The plan extends that path to missing and archived targets, and the item returns to its open status.
- **Callbacks.** `pending` and `dead_letter` rows of decided tasks move to `superseded` with `last_error = "task_decided"`. Rows of undecided tasks are untouched, because they signal a real delivery problem.
- **Registrations.** The existing registration-prune function (behind `aiworkhub_dashboard_storage_registration_prune`) runs when its preview reports `safe_to_prune`.

### 4.6 Failures are visible

When a janitor step fails, the janitor files one NeedFix through the NeedFix store API. Its `dedupe_key` is `janitor:<step>:<error class>` and its evidence is the step's nested result. A repeating failure therefore stays one item. `core._reconcile_retained_workspaces` loses its bare `except`.

### 4.7 Parallelism

Per-item deletions (worktrees, log files) are IO-bound and run in a thread pool sized from `os.cpu_count()`, leaving headroom for the MCP server. If concurrent `git worktree remove` calls measure contention on the repository's worktree admin directory, the plan records that measurement and keeps that one call sequential, with a comment explaining why.

## 5. Unchanged / out of scope

- Accept authority stays with the manager. The janitor never decides a review, and never archives a blocked, review or pending task.
- One-time manager triage is not janitor work: the 12 review and 4 pending rows from 2026-08, and the 110 blocked tasks. The manager decides these after the janitor lands.
- S2 (digest callback, WorkHub commit, `push_policy`) and S3 (same-file merge) have their own specs.
- RM-2026-00073 forward rollback takes its pre-images from git (S2 commits the accept), not from worktrees; nothing here blocks it.
- No new configuration keys. The existing `repo_policy` retention keys keep only their backstop role.

## 6. Acceptance tests

1. **Call site.**
   - An accept through the real accept path (`process_launcher_accept_review`), not a direct janitor call, ends with the worktree and home removed and no registration left.
   - The same holds for reject, supersede and archive.
2. **Blocked attempt.** Its worktree is removed. When it is a rework predecessor, a verified sealed delta exists before removal, and `recover_blocked_rework` still succeeds afterwards.
3. **Negatives.** These are untouched:
   - a processing task's worktree;
   - the current review request;
   - a live pid;
   - an undecided `finalize_failed` attempt;
   - a failed seal (worktree kept, one NeedFix).
4. **Unattributed.** An unattributed worktree still goes to quarantine with the 7-day window.
5. **Quarantine purge.** A batch whose items all belong to decided tasks is purged on the next sweep; a batch with any unknown item keeps its deadline.
6. **Logs.** A log is deleted only after its usage row exists; a backfill failure keeps the log.
7. **Records.**
   - finished → archived;
   - resolved NeedFix → archived;
   - a dead-link NeedFix → reopened;
   - a `pending` or `dead_letter` callback of a decided task → `superseded`;
   - a blocked task row is untouched.
8. **Failure visibility.** An injected step failure yields exactly one NeedFix across two sweeps, and the other steps still run.
9. **Lane C retired.** Nothing imports `cleanup_accepted_artifacts`.
10. **Measured on this repository after install:**
    - the preview protects only live workers and current review requests;
    - owned quarantine bytes are 0;
    - 0 registrations are prunable;
    - 0 `pending` or `dead_letter` callbacks belong to decided tasks;
    - 0 resolved NeedFix remain outside the archive.
