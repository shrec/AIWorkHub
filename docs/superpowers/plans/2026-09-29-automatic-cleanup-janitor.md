# Automatic Cleanup Janitor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a task is decided, AIWorkHub itself removes every artifact that belonged to it: the worktree, the rework delta, terminal logs, process bundles, spill files, callbacks and the task/NeedFix records. Anything that no live task uses is removed too, whatever its outcome was. No model or owner does this by hand. Ships in release 0.12.4.

**Architecture:** `storage_retention._run_repository_cleanup` becomes a six-step janitor. Each step is isolated, and a failure turns into a deduplicated NeedFix instead of stopping the run.

1. Worktree GC (with a rework seal).
2. Rework-delta prune.
3. Logs, bundles and spill.
4. Unattributed directories and legacy batches.
5. Stale worktree registrations.
6. Records (callbacks, tasks, NeedFix).

The existing trigger sites already call `schedule_repository_cleanup` and are unchanged: `process_launcher._retention_event`, `default_manager` startup, `server._enforce_terminal_retention_safely` and deadline wakeups. The `mark_done` path moves onto the same scheduler.

Deletion is direct; quarantine stays only for manual storage batches. The safety guards in spec §4.3 all stay.

**Tech Stack:** Python 3 stdlib (`concurrent.futures`, `sqlite3`, `pathlib`, `threading`), pytest.

**Spec:** `docs/superpowers/specs/2026-09-29-automatic-cleanup-janitor-design.md`

## Execution Order

| Wave | Cards | Why |
|---|---|---|
| 1 (parallel) | Task 1 (C1), Task 2 (C3), Task 3 (C4) | allowed_writes are disjoint |
| 2 | Task 4 (C2) | consumes the interfaces of C1, C3 and C4; shares `tests/test_storage_retention_rework_protection.py` with C1 |
| 3 | Task 5 (manager one-time step) | runs after 0.12.4 is installed, with owner confirmation for the deletions |

## Global Constraints

- No new config keys. `terminal_runs_days` and `worktree_max_bytes` stay parseable. `logs_days` sets the lifetime of orphan logs and spill files.
- The spec §4.3 guards stay:
  - `processing` rows;
  - the `current_review_request`;
  - undecided `finalize_failed` attempts;
  - `_process_proven_dead`;
  - `assert_gc_safe_workspace_shape`;
  - the integrity quarantine.
- Decided task statuses are exactly `{"finished", "archived", "superseded"}`. Each module that needs this set keeps a local `frozenset` with that literal value, so wave-1 cards do not import from one another.
- Nothing is deleted younger than 24 hours unless a task record owns it. This protects a launch that is still in progress.
- Multicore by default:
  - Per-item deletion runs on a `ThreadPoolExecutor` sized `max(1, (os.cpu_count() or 2) - 1)`. It is I/O-bound and `shutil.rmtree`/`unlink` release the GIL.
  - The result dict must be identical to the sequential path; a test pins this.
- The janitor never writes context databases directly:
  - NeedFix is written only through `needfix_store` functions;
  - tasks only through `task_store`;
  - callbacks only through `callback_store`.
- Every existing file is edited with semantic edit (worker `aiworkhub_worker_semantic_edit_prepare`/`_apply`) on the smallest range. New files are whole-file writes.
- Test command: `.venv/Scripts/python.exe -P -m pytest -q <files>`.
- If a test outside a card's allowed_writes breaks, the card stops and reports it with the failing test id. It does not edit that test.

## Review Focus

- **Hint-drop race:**
  - Condition: a second `schedule_repository_cleanup` call arrives while a run is in progress.
  - Expected: one more run starts after the current one finishes; the hint is not lost. No self-scheduling loop starts from `_retention_event` inside the janitor.
  - Test owner: Task 4.
- **Cross-host NeedFix link:**
  - Condition: a NeedFix row, merged from another host by row union, points at a task this host never saw.
  - Expected: the janitor leaves it alone and does not reopen it.
  - Test owner: Task 3.
- **Launch race inside 24 hours:**
  - Condition: a worktree directory with no ledger row yet (a launch in progress), or a worktree root shared outside the repository.
  - Expected: the directory is not deleted.
  - Test owner: Task 4.
- **Tampered manifest:**
  - Condition: an authenticated storage manifest whose `source` was stripped or changed.
  - Expected: authentication fails and the batch is skipped, not purged.
  - Test owner: Task 4.
- **Variable error text:**
  - Condition: the same step fails again with different exception text.
  - Expected: one NeedFix, not a new row per run.
  - Test owner: Task 4.

---

### Task 1 (card C1): Rework-predecessor seal and worktree GC reachability

**Objective:** a pinned rework predecessor is sealed into a rework-delta artifact and its worktree is removed. The GC reaches every terminal process state, and superseded tasks count as disposed.

**Files (allowed_writes):**
- Modify: `src/aiworkhub/process_launcher.py`:
  - `GC_CANDIDATE_PROCESS_STATES` (≈842);
  - `GC_DISPOSED_CANONICAL_STATUSES`;
  - `_gc_finalized_workspaces` (≈10990-11004);
  - `_gc_disposition` (≈11006-11062);
  - `_gc_finalized_workspace` (≈11132-11405).
- Modify: `src/aiworkhub/task_store.py`: add two functions next to `archive_task`.
- Create: `tests/test_janitor_rework_seal.py`
- Modify: `tests/test_finalized_workspace_gc_b512_v1.py`, `tests/test_storage_retention_rework_protection.py`, `tests/test_finalization_result_precedence.py` (only the asserts that encode the old retain-forever behavior).

**Interfaces:**
- Consumes (all existing):
  - `successful_rework_recovery.candidate_entries(workspace: Path, hashes: Mapping[str, Any]) -> list[tuple[str, bytes | None]]`
  - `ProcessManager._terminal_rework_delta_evidence(workspace, metadata, request_id, changed, *, captured_entries=None) -> dict | None`
  - `WorkerWorkspace.from_metadata`
- Produces:
  - `task_store.attach_rework_delta(root: Path, task_id: str, *, predecessor_request_id: str, claim_epoch: int, descriptor: dict) -> tuple[bool, str]`
    - Returns `(True, "attached")`.
    - Otherwise returns `(False, reason)` with reason in `{"task_missing", "rework_predecessor_mismatch", "rework_delta_present", "card_changed"}`.
    - It does a CAS on the `card_json` preimage and records a `rework_delta_sealed` task event.
  - `task_store.referenced_rework_delta_digests(root: Path) -> set[str]`
    - Collects every 64-lowercase-hex value under the keys `artifact_sha256` or `digest` in the `card_json` of tasks whose status is not decided.
  - `ProcessManager._gc_finalized_workspaces() -> dict`
    - Keeps the keys `gc_scanned`, `gc_cleaned` and `gc_skipped`.
    - Adds `failures: list[{"request_id", "task_id", "reason"}]` (at most 50) only when it is non-empty.
    - Reasons start with `rework_seal_failed:` or `cleanup_failed:`.

- [ ] **Step 1: Write the failing store tests** in `tests/test_janitor_rework_seal.py`. Create the repo with `task_store.initialize_repository(tmp_path)` and insert task rows directly, the way `tests/test_task_retention.py::_archived` does. Core assertions:

  ```python
  ok, reason = task_store.attach_rework_delta(
      repo, "T1", predecessor_request_id="req-other", claim_epoch=1, descriptor=DESC
  )
  assert (ok, reason) == (False, "rework_predecessor_mismatch")
  ok, reason = task_store.attach_rework_delta(
      repo, "T1", predecessor_request_id="req-pred", claim_epoch=1, descriptor=DESC
  )
  assert (ok, reason) == (True, "attached")
  assert task_store.attach_rework_delta(
      repo, "T1", predecessor_request_id="req-pred", claim_epoch=1, descriptor=DESC
  ) == (False, "rework_delta_present")
  assert [e["event"] for e in task_store.get_task_events(repo, "T1")][-1] == "rework_delta_sealed"
  assert task_store.referenced_rework_delta_digests(repo) == {DESC["artifact_sha256"]}
  ```

  Here `DESC` is a sealed descriptor dict:
  - `schema_id` is `"aiworkhub.rework_delta_descriptor.v1"` and `sealed` is `True`;
  - `artifact_sha256` is `"a" * 64`;
  - `authority_repo`, `task_id`, `request_id`, `claim_epoch` and `artifact_path` are also set.

  Add a second task with status `finished` whose card references `"b" * 64`, and assert that digest is **not** in the set.

- [ ] **Step 2: Write the failing GC tests** in `tests/test_finalized_workspace_gc_b512_v1.py`, reusing `_build_manager`, `_seed_gc_candidate` and `WORKTREE_ROOT_ENV`.
  - (a) **Seal then remove.**
    - Setup:
      - Seed a pinned predecessor whose card has `rework_predecessor` = `{request_id: rid, claim_epoch: 1, changed_path_hashes: {"out/result.txt": sha256(content)}}`.
      - Write `content` to `path/"out"/"result.txt"`.
      - Monkeypatch `process_launcher.task_store.attach_rework_delta` to store the descriptor into the card that `_show` returns, and return `(True, "attached")`.
    - Assert:
      - the result reason is `"sealed_rework_delta"`;
      - `not path.exists()`;
      - the artifact file named in the descriptor exists with `sha256 == descriptor["artifact_sha256"]`.
  - (b) **Seal failure keeps the worktree.**
    - Setup: same, but with a wrong hash.
    - Assert:
      - reason `"rework_seal_failed:successful_rework_hash_mismatch"`;
      - `path.exists()`;
      - `manager._gc_finalized_workspaces()["failures"][0]["reason"]` starts with `"rework_seal_failed:"`.
  - (c) **Superseded plus finalize_failed.** A `finalize_failed` attempt whose task is `superseded` is removed.
  - (d) **Every terminal state.** For every state in `TERMINAL_PROCESS_STATES`, a finished task's retained workspace is a GC candidate:

    ```python
    assert set(process_launcher.GC_CANDIDATE_PROCESS_STATES) == set(process_launcher.TERMINAL_PROCESS_STATES)
    ```

- [ ] **Step 3: Run** `.venv/Scripts/python.exe -P -m pytest -q tests/test_janitor_rework_seal.py tests/test_finalized_workspace_gc_b512_v1.py`. Expected: the new tests FAIL because `attach_rework_delta`, `failures` and the seal branch do not exist.

- [ ] **Step 4: Implement `task_store`.**
  - `attach_rework_delta` follows `archive_task`'s pattern: `_require_ready`, `_write_connection`, `_begin_immediate`, read `card_json`, CAS `UPDATE … WHERE card_json = ?`, then INSERT into `task_events`.
  - On success it sets these fields and changes nothing else in the card:
    - `card["rework_predecessor"]["task_id"]` and `["claim_epoch"]`;
    - `["delta_artifact"] = {"path": descriptor["artifact_path"], "digest": descriptor["artifact_sha256"]}`;
    - `card["rework_delta"] = descriptor`.
  - Before writing, read `has_verified_rework_delta` in `process_launcher.py` and match the shape it accepts. Step 2(a) proves the match.
  - `referenced_rework_delta_digests` is one read-only `SELECT card_json FROM tasks WHERE status NOT IN ('finished','archived','superseded')` plus a recursive walk of the JSON values.

- [ ] **Step 5: Implement `process_launcher`.**
  - Constant changes:
    - `GC_CANDIDATE_PROCESS_STATES = TERMINAL_PROCESS_STATES`;
    - add `"superseded"` to `GC_DISPOSED_CANONICAL_STATUSES`;
    - in `_gc_disposition`, the `finalize_failed` guard set becomes `{"finished", "archived", "superseded"}`.
  - In `_gc_finalized_workspace`, at `if not eligible:` and only when `disposition == "pinned_rework_predecessor"`:
    1. Require `_process_proven_dead` and `assert_gc_safe_workspace_shape`. On failure, return the existing skip reasons.
    2. Compute `entries = successful_rework_recovery.candidate_entries(path, predecessor["changed_path_hashes"])`. Catch `SuccessfulReworkRecoveryError` and return `rework_seal_failed:<code>`.
    3. Compute `desc = self._terminal_rework_delta_evidence(WorkerWorkspace.from_metadata(workspace_meta), {"task_id": task_id, "claim_epoch": predecessor.get("claim_epoch") or metadata.get("claim_epoch")}, request_id, sorted(predecessor["changed_path_hashes"]), captured_entries=entries)`.
    4. If `not desc or not desc.get("sealed")`, return `rework_seal_failed:<desc reason or "no_changes">` and keep the worktree.
    5. Call `task_store.attach_rework_delta(self.repo, task_id, predecessor_request_id=request_id, claim_epoch=desc["claim_epoch"], descriptor=desc)`. If it returns false, return `rework_seal_failed:<reason>`.
    6. Re-show the card and rerun `_gc_disposition`. Continue only if it is now `"sealed_rework_delta"`.
    7. Fall through to the existing delete path. No second delete implementation.
  - In `_gc_finalized_workspaces`, collect results whose reason starts with `rework_seal_failed:` or `cleanup_failed:` into `failures`, capped at 50.

- [ ] **Step 6: Update the old-behavior asserts:**
  - `tests/test_finalized_workspace_gc_b512_v1.py` ≈760-778 (retained → cleaned);
  - `tests/test_finalization_result_precedence.py` ≈449;
  - the pinned-protection asserts in `tests/test_storage_retention_rework_protection.py`. Keep them for the unsealed/failure case; flip them where a seal now succeeds.

  Name each changed assert in the handoff.

- [ ] **Step 7: Run** `.venv/Scripts/python.exe -P -m pytest -q tests/test_janitor_rework_seal.py tests/test_finalized_workspace_gc_b512_v1.py tests/test_storage_retention_rework_protection.py tests/test_finalization_result_precedence.py tests/test_worker_workspace.py`. Expected: all PASS.

- [ ] **Step 8: Commit** only the allowed_writes: `feat(gc): seal pinned rework predecessors and reach every terminal state`.

**Acceptance:**
- A pinned predecessor's worktree is gone and its delta artifact materializes for the successor. This is the same guarantee as `test_rework_delta_artifact_materializes_after_predecessor_cleanup`.
- A seal failure keeps the worktree and appears in `failures`.

---

### Task 2 (card C3): Log, process-bundle and spill lifetimes

**Objective:** a decided task's terminal logs and aged process bundles are deleted outright. Orphan logs and spill files expire after `logs_days`. Legacy log batches without a source are purged.

**Files (allowed_writes):**
- Modify: `src/aiworkhub/terminal_log_retention.py`:
  - `_candidate_payload` (≈517-660);
  - `enforce_process_log_bounds` (≈1745-1792);
  - `enforce` (≈1829-1915);
  - `quarantine` (the source stamp);
  - batch purge.
- Modify: `src/aiworkhub/output_spill_store.py`:
  - module docstring (≈26-29);
  - `spill_text`;
  - new `prune_expired`.
- Modify tests: `tests/test_terminal_log_retention.py`, `tests/test_terminal_log_retention_empty_batches.py`, `tests/test_process_log_retention_bounds.py`, `tests/test_output_spill_store.py`, `tests/test_retention_recovery_truth.py`, `tests/test_quarantine_unclaimed_reconciliation.py`.

**Interfaces:**
- Consumes (existing):
  - `terminal_log_retention.backfill_usage_capture(repo_root, *, confirm) -> dict`
  - `_logs_days(root) -> int`
  - `_task_status_map`
- Produces:
  - `output_spill_store.prune_expired(repo: Path, *, max_age_days: int, now: float | None = None) -> dict`
    - Returns `{"scanned": int, "removed": int, "bytes_freed": int, "errors": list[str]}`.
    - Removes `<digest>.txt` files and `.<digest>.*.tmp` temp files whose mtime is older than `max_age_days`.
  - `terminal_log_retention.enforce(repo_root) -> dict`
    - Keeps its current keys.
    - Adds `"usage_backfill"` (the backfill result), `"deleted"` (count of logs deleted directly) and `"spill"` (the `prune_expired` result).
  - Log quarantine manifests written by `quarantine()` carry `"source": "manual"`.

- [ ] **Step 1: Write the failing tests.**
  - In `tests/test_terminal_log_retention.py`:
    - (a) **Decided log deleted at once.**
      - Setup: a terminal request whose task is `finished` and whose usage is captured.
      - Assert:
        - the log is deleted on the first `enforce` (no age wait, no keep-last);
        - no new quarantine batch appears.
    - (b) **Undecided log kept.** The same request with its task `review` is kept.
    - (c) **Backfill before delete.**
      - Setup: a request with usage not yet captured.
      - Assert: after `enforce`, the usage receipt is recorded and the log is gone.
    - (d) **Orphan backstop.**
      - Setup: an orphan log (no request row).
      - Assert:
        - kept when its mtime is younger than `logs_days`;
        - deleted when older;
        - a file under the legacy `logs/` tree is untouched.
    - (e) **Legacy batch purge.**
      - Setup: a log batch manifest without `source`, whose deadline is in the future.
      - Assert:
        - it is purged;
        - a batch with `"source": "manual"` and a future deadline is kept;
        - `quarantine()` writes `"source": "manual"`.
    - (f) **Parallel equals sequential.** Monkeypatch the pool-size helper to `1` and compare it with the default size:

      ```python
      assert result_parallel["deleted"] == result_sequential["deleted"]
      ```

  - In `tests/test_process_log_retention_bounds.py`: an aged process bundle is deleted, not moved into a quarantine batch.
  - In `tests/test_output_spill_store.py`:

    ```python
    receipt = output_spill_store.spill_text("x" * 5000, repo=repo)
    target = repo / ".aiworkhub" / "spill" / f"{receipt.digest}.txt"
    old = time.time() - 10 * 86400
    os.utime(target, (old, old))
    output_spill_store.spill_text("x" * 5000, repo=repo)          # repeat spill refreshes mtime
    assert output_spill_store.prune_expired(repo, max_age_days=7)["removed"] == 0
    os.utime(target, (old, old))
    assert output_spill_store.prune_expired(repo, max_age_days=7)["removed"] == 1
    assert not target.exists()
    ```

    Also assert that an 8-day-old `.<digest>.abc.tmp` file is removed. Use whatever field name `SpillReceipt` actually exposes for the digest.

- [ ] **Step 2: Run** `.venv/Scripts/python.exe -P -m pytest -q tests/test_terminal_log_retention.py tests/test_process_log_retention_bounds.py tests/test_output_spill_store.py`. Expected: the new tests FAIL.

- [ ] **Step 3: Implement.**
  - **`enforce`:**
    - First, `usage_backfill = backfill_usage_capture(root, confirm=True)`.
    - Then purge batches: legacy batches without `source` are purged regardless of deadline; `"source": "manual"` batches keep their deadline.
    - Then delete candidates directly through a `ThreadPoolExecutor` (size `max(1, (os.cpu_count() or 2) - 1)`), with each unlink isolated and each error appended to `errors`.
    - Then `_dead_owner_temp_gc` and `enforce_process_log_bounds`.
    - Then `spill = output_spill_store.prune_expired(root, max_age_days=_logs_days(root))`.
  - **`_candidate_payload`:**
    - A terminal request whose task is decided and whose usage is captured is a candidate, with no `KEEP_LAST_PER_TASK` and no age cutoff.
    - An orphan older than `logs_days` is a candidate. Replace the NF-2026-00286 comment with one line saying the `logs_days` backstop supersedes it.
    - The legacy `logs/` tree stays protected.
    - The digest covers the new candidate set.
  - **`enforce_process_log_bounds`:** aged bundles are removed with `shutil.rmtree` instead of `_stage_reconcile_batch`.
  - **`output_spill_store`:**
    - When `spill_text` finds that the target already exists, it calls `os.utime(target)` after the digest verification.
    - Add `prune_expired`.
    - The docstring states the `logs_days` lifetime and that a missing digest returns `output_spill_store_missing`.

- [ ] **Step 4: Update the old quarantine-flow asserts** in `tests/test_terminal_log_retention_empty_batches.py`, `tests/test_retention_recovery_truth.py` and `tests/test_quarantine_unclaimed_reconciliation.py`, but only where they encode "decided logs go to quarantine". Name each one in the handoff.

- [ ] **Step 5: Run** all six test files in allowed_writes. Expected: PASS.

- [ ] **Step 6: Commit** only the allowed_writes: `feat(retention): delete decided logs, bundles and expired spill directly`.

**Acceptance:**
- After one `enforce`, a decided task has zero terminal logs.
- A spill older than `logs_days` is gone, and `retrieve_text` reports `output_spill_store_missing` for it.

---

### Task 3 (card C4): Records janitor — callbacks, tasks, NeedFix

**Objective:**
- Callbacks of decided tasks are superseded.
- Decided tasks are archived on TTL, whether or not they belong to a family.
- A NeedFix whose task was accepted leaves the open list.
- A NeedFix with a dead local link is reopened.
- Decided-outcome metrics survive the archiving.

**Files (allowed_writes):**
- Modify: `src/aiworkhub/callback_store.py`:
  - new `supersede_decided_task_callbacks`;
  - new helper `_supersede_emptied_batches`, which `prune_stale_pending_callbacks` (≈1642-1727) now shares.
- Modify: `src/aiworkhub/task_retention.py`:
  - `_candidate_ids` (≈965-988);
  - `_final_archive_fence` (≈1031-1118);
  - new `DECIDED_TASK_STATUSES` and `run_records_janitor`.
- Modify: `src/aiworkhub/needfix_store.py`:
  - `reopen_superseded_task_link` (≈3508-3626);
  - new `janitor_bookkeeping`.
- Create: `tests/test_callback_decided_supersede.py`
- Modify tests: `tests/test_task_retention.py`, `tests/test_needfix_store.py`, `tests/test_sdlc_outcome_metrics.py`.

**Interfaces:**
- Consumes (existing):
  - `task_store.get_task`, `task_store.canonical_status`, `task_store.get_task_events(root, task_id, *, limit=100)`, `task_store.archive_task`;
  - `needfix_store.archive_needfix(repo_root, needfix_id, *, reason=None)`;
  - `callback_store.append_event`;
  - `run_automatic_hygiene(root, *, now=None)`.
- Produces:
  - `callback_store.supersede_decided_task_callbacks(conn) -> dict[str, int]`
    - Keys: `scanned`, `superseded`, `batches_superseded`.
    - Handles `pending` and `dead_letter` rows only.
    - Sets `last_error = "task_decided"`.
    - A task that is missing counts as decided.
  - `task_retention.DECIDED_TASK_STATUSES = frozenset({"finished", "archived", "superseded"})`
  - `task_retention.run_records_janitor(root: Path, *, now: float | None = None) -> dict`
    - Keys: `callbacks`, `tasks`, `needfix`.
    - Each value is the step's result, or `{"ok": False, "error": "<ExcClass>: <msg>"}` when that step raised.
    - The steps run in that order.
  - `needfix_store.reopen_superseded_task_link(..., missing_task_has_local_history_fn: Callable[[str], bool] | None = None)`
  - `needfix_store.janitor_bookkeeping(repo_root, *, get_task_fn, canonical_status_fn, task_has_local_history_fn) -> dict`
    - Keys: `scanned`, `archived`, `reopened`, `skipped`.

- [ ] **Step 1: Write the failing callback test** in `tests/test_callback_decided_supersede.py`.
  - Setup:
    - a `finished` task with one `pending` and one `dead_letter` callback in the same batch;
    - a `review` task with a `pending` callback.
  - Core assertion:

    ```python
    result = callback_store.supersede_decided_task_callbacks(conn)
    assert result == {"scanned": 2, "superseded": 2, "batches_superseded": 1}
    states = dict(conn.execute("SELECT task_id, state FROM callback_outbox WHERE task_id='LIVE'").fetchall())
    assert states == {"LIVE": "pending"}
    ```

- [ ] **Step 2: Write the failing task-retention tests** in `tests/test_task_retention.py`, using `_repo` and `_archived`-style inserts.
  - (a) **Standalone decided rows.** A standalone `finished` row past the TTL, and a `superseded` row past the TTL, are both archived by `run_automatic_hygiene`.
  - (b) **Stale ledger ignored.** A `superseded` row whose ledger is stale is archived; `ledger_stale` no longer fences a decided status.
  - (c) **Live dependent.**
    - Setup: a `finished` task that a `pending` task lists in `card_json["depends_on"]`.
    - Assert: `_final_archive_fence` returns `"dependency_live"`, because `core._card_dependencies_have_accepted_outcomes` needs the dependency's status to stay `finished`.
  - (d) **Callback order.**
    - Setup: a decided task with a `pending` callback.
    - Assert: `run_records_janitor` supersedes the callback first, so the same run archives the task.
  - (e) **Step isolation.**
    - Setup: monkeypatch `callback_store.supersede_decided_task_callbacks` to raise `RuntimeError("boom")`.
    - Assert: `result["callbacks"] == {"ok": False, "error": "RuntimeError: boom"}`, and `tasks` and `needfix` still ran.
  - Update the family test at ≈213-281 if its expected archived list grows because standalone rows are now candidates, and name that change in the handoff.

- [ ] **Step 3: Write the failing NeedFix tests** in `tests/test_needfix_store.py`, using the `init` fixture and the `_archived_get_task`/`_superseded_get_task`/`_reopen_canonical_status` helpers.
  - (a) **Accepted task.** A `task_created` NeedFix whose task is `finished` is archived by `janitor_bookkeeping`.
  - (b) **Archived with acceptance.** The same outcome holds when the task is `archived` with `accepted_at`.
  - (c) **Superseded without acceptance.** When the task is `superseded` without `accepted_at`, the NeedFix is reopened to `accepted` and `reopen_generation` increments.
  - (d) **Missing task with local history.** When `get_task_fn` returns `None` and `task_has_local_history_fn` returns `True`, the NeedFix is reopened with event `missing_task_link_reopened`.
  - (e) **Missing task without local history.** When `get_task_fn` returns `None` and `task_has_local_history_fn` returns `False` (a cross-host link):

    ```python
    assert result["skipped"] == 1
    assert needfix_store.get_needfix(repo_root, nf_id)["status"] == "task_created"
    ```

    Use the store's actual getter name.
  - (f) **Existing reopen behavior.** Calling `reopen_superseded_task_link` without the new kwarg behaves exactly as it does today for a missing task: it raises `NeedFixValidationError`.

- [ ] **Step 4: Write the metrics pin test** in `tests/test_sdlc_outcome_metrics.py`.
  - Setup: a task with an `accept_review` event, then an `archived` event, and status `archived`.
  - Assert: it is still in `read_decided_task_cohort(conn)[1].selected`.
  - This test passes today. It pins that archiving never drops a decided outcome.

- [ ] **Step 5: Run** `.venv/Scripts/python.exe -P -m pytest -q tests/test_callback_decided_supersede.py tests/test_task_retention.py tests/test_needfix_store.py tests/test_sdlc_outcome_metrics.py`. Expected: the new tests from Steps 1-3 FAIL.

- [ ] **Step 6: Implement.**
  - **`supersede_decided_task_callbacks`:**
    - mirrors `prune_stale_pending_callbacks`: ensure tables, `BEGIN IMMEDIATE`, UPDATE, then `append_event(conn, task_id, "callback_superseded", "", {"reason": "task_decided"})`;
    - moves the emptied-batch update into `_supersede_emptied_batches(conn, batch_ids, now)`, which both functions call.
  - **`_candidate_ids`:** also selects any row whose status is in `{"finished", "superseded"}` and whose TTL has expired.
  - **`_final_archive_fence`:**
    - After `task_reserved`, return `"dependency_live"` when any task whose status is not decided lists this id in `card_json["depends_on"]`.
    - Skip the `ledger_stale` check when the task status is decided.
  - **`run_records_janitor`:** runs three steps in this order, each in `try/except Exception`:
    1. callbacks, on a connection opened as the caller of `prune_stale_pending_callbacks` at `core.py:2809` opens it;
    2. `run_automatic_hygiene(root, now=now)`;
    3. `needfix_store.janitor_bookkeeping(root, get_task_fn=lambda t: task_store.get_task(root, t), canonical_status_fn=task_store.canonical_status, task_has_local_history_fn=lambda t: bool(task_store.get_task_events(root, t, limit=1)))`.
  - **`reopen_superseded_task_link`:**
    - A `None` task with `missing_task_has_local_history_fn` returning `True` reopens with event `missing_task_link_reopened`.
    - Canonical status `superseded` without `accepted_at` is also reopenable.
  - **`janitor_bookkeeping`:** iterates `task_created` rows with a `converted_task_id`:
    - accepted (finished, or archived with `accepted_at`) → `archive_needfix(..., reason="janitor:task_accepted")`;
    - dead link → `reopen_superseded_task_link(...)`;
    - anything else → skipped.

- [ ] **Step 7: Run** the Step 5 command. Expected: PASS.

- [ ] **Step 8: Commit** only the allowed_writes: `feat(records): janitor supersedes decided callbacks, archives decided tasks, retires accepted NeedFix`.

**Acceptance:** after `run_records_janitor`:
- no decided task has a `pending` callback;
- decided rows past the TTL are archived unless a live dependent or retained evidence fences them;
- no NeedFix points at an accepted task.

---

### Task 4 (card C2): Janitor orchestration

**Objective:** `_run_repository_cleanup` runs the six isolated steps. A failing step becomes a deduplicated NeedFix. Scheduling is loss-free and cannot loop. `mark_done` uses the janitor. Lane C and the private GC thread in `core.py` are deleted.

**Depends on:** Tasks 1, 2 and 3 accepted into the tree.

**Files (allowed_writes):**
- Modify: `src/aiworkhub/storage_retention.py`:
  - `_purge_batch` (≈2027-2057);
  - `_run_repository_cleanup` (≈3305-3395);
  - `schedule_repository_cleanup` (≈3436-3489);
  - delete Lane C: `cleanup_accepted_artifacts` (≈3240), its private helpers (≈2633-3291) and its `__all__` entry (≈3498).
- Modify: `src/aiworkhub/core.py`:
  - `_reconcile_retained_workspaces` (≈5610-5658);
  - delete `_WORKSPACE_GC_JOBS_LOCK`/`_WORKSPACE_GC_JOBS` (≈94-95).
- Create: `tests/test_janitor.py`
- Modify tests: `tests/test_storage_retention.py`, `tests/test_storage_retention_reclaim.py`, `tests/test_storage_retention_autohygiene.py`, `tests/test_storage_retention_measurement_completes.py`, `tests/test_storage_retention_rework_protection.py`.

**Interfaces:**
- Consumes:
  - `ProcessManager._gc_finalized_workspaces()["failures"]` and `task_store.referenced_rework_delta_digests` (Task 1);
  - `terminal_log_retention.enforce` (Task 2);
  - `task_retention.run_records_janitor` (Task 3);
  - existing `scan_worktree_registrations`, `prune_stale_registrations`, `cleanup_workspace`, `_latest_by_request`, `needfix_store.add_needfix`.
- Produces:
  - `_run_repository_cleanup(repo_root, *, base=None, now) -> dict`
    - Keys: `worktrees`, `rework_deltas`, `logs`, `unattributed`, `registrations`, `records`, `needfix`.
    - `needfix` is the list of NeedFix ids created or deduplicated in this run.
  - `_purge_batch(..., ignore_deadline: bool = False)`
  - `schedule_repository_cleanup(repo_root, *, base=None) -> bool`
    - Returns `False` when a run is active; in that case it sets the rerun flag.
    - Returns `False` without scheduling when called from inside the janitor.
  - `result["workspace_retention"] == {"ok": True, "queued": <bool>, "mode": "janitor"}` from `mark_done`.

- [ ] **Step 1: Write the failing tests** in `tests/test_janitor.py`. Stub every step dependency with monkeypatch so each test targets one behavior.
  - (a) **Step order and isolation.**
    - Setup: step 3 raises `OSError("disk")`.
    - Assert:
      - steps 4-6 still ran;
      - `result["logs"]["ok"] is False`;
      - exactly one NeedFix titled `janitor:logs:OSError` exists.
    - Then run again with `OSError("other text")` and assert there is still exactly one row: the title and description are fixed; the evidence carries the text.
  - (b) **Seal failures become NeedFix.** GC `failures` produce one NeedFix titled `janitor:worktrees:rework_seal_failed`, with the failure list as evidence.
  - (c) **Rework-delta prune.**
    - Setup: `rework_deltas/<d>.json` unreferenced and 25 hours old; one referenced; one unreferenced and 1 hour old; a `.rework-delta-x.tmp` that is 25 hours old.
    - Assert: only the old unreferenced file and the old temp file are removed.
  - (d) **Unattributed directories.**
    - Setup: under a worktree base inside the repo:
      - a directory with no ledger row, 25 hours old;
      - one with no ledger row, 1 hour old;
      - one whose latest row is terminal with `workspace_retained=False`, 25 hours old;
      - one whose latest row is `processing`;
      - the `QUARANTINE_DIRNAME` directory;
      - `.hidden`.
    - Assert: only the two 25-hour-old candidates are removed via `cleanup_workspace`.
    - With a base outside the repo, assert `result["unattributed"] == {"skipped": "shared_worktree_root"}`.
  - (e) **Manifest source and tampering.**
    - A source-less authenticated storage batch with a future deadline is purged with `ignore_deadline=True`.
    - A `"source": "manual"` batch with a future deadline is kept.
    - A manifest whose `source` was altered after signing fails `_authenticated_manifest` and is skipped.
  - (f) **No lost hint.**
    - Setup: while a run holds `_AUTO_HYGIENE_RUNNING`, call `schedule_repository_cleanup`.
    - Assert: it returns `False` and exactly one extra run follows.
  - (g) **No self-scheduling loop.** Called from inside a janitor step (the thread-local `_JANITOR_ACTIVE` is set), it schedules nothing.
  - (h) **`mark_done` goes through the janitor.**

    ```python
    assert result["workspace_retention"]["mode"] == "janitor"
    assert not hasattr(core, "_WORKSPACE_GC_JOBS")
    ```

- [ ] **Step 2: Run** `.venv/Scripts/python.exe -P -m pytest -q tests/test_janitor.py`. Expected: FAIL.

- [ ] **Step 3: Implement.**
  - **`_janitor_step(name, fn, result, needfix_ids)`:**
    - runs `fn()`;
    - on an exception, stores `{"ok": False, "error": f"{type(exc).__name__}: {exc}"}`;
    - calls `needfix_store.add_needfix(root, title=f"janitor:{name}:{type(exc).__name__}", description=f"Automatic cleanup step '{name}' failed.", evidence=<the step result>)`;
    - collects the returned id (dedupe returns the existing row).
  - **The six steps** run in order under `_JANITOR_ACTIVE`:
    1. **Worktrees:** lazy-import `ProcessManager(repo=root)._gc_finalized_workspaces()`. A non-empty `failures` produces the NeedFix from test (b).
    2. **Rework deltas:** `_prune_decided_rework_deltas(root, now)`.
    3. **Logs:** `terminal_log_retention.enforce(root)`.
    4. **Unattributed:** the unattributed-directory rule from test (d), plus source-less storage batches purged with `ignore_deadline=True`.
    5. **Registrations:** `prune_stale_registrations(root, preview_digest=scan_worktree_registrations(root, base)["preview_digest"], confirm=True)` only when the preview reports `safe_to_prune`.
    6. **Records:** `task_retention.run_records_janitor(root)`.
  - **Deletions** in steps 2 and 4 use the pool size from Global Constraints.
  - **`_manifest_authentication`** adds `"source"` to the signed fields only when the manifest has it, so legacy manifests still authenticate.
  - **`quarantine()`** in `storage_retention` stamps `"source": "manual"`.
  - **`schedule_repository_cleanup`:**
    - return `False` when `_JANITOR_ACTIVE` is set on this thread;
    - when a run is active, add the repo to `_AUTO_HYGIENE_RERUN` and return `False`;
    - the run thread's `finally` block pops the flag and runs once more.
  - **`core._reconcile_retained_workspaces`:** when ok, call `storage_retention.schedule_repository_cleanup(root)` and set `workspace_retention = {"ok": True, "queued": queued, "mode": "janitor"}`. Delete the private thread, its bare `except`, and `_WORKSPACE_GC_JOBS*`.
  - **Lane C:** delete it. If a test outside allowed_writes imports `cleanup_accepted_artifacts`, stop and report its id.

- [ ] **Step 4: Update the old-behavior asserts** in the five `tests/test_storage_retention*.py` files, and name each one in the handoff.

- [ ] **Step 5: Run** `.venv/Scripts/python.exe -P -m pytest -q tests/test_janitor.py tests/test_storage_retention.py tests/test_storage_retention_reclaim.py tests/test_storage_retention_autohygiene.py tests/test_storage_retention_measurement_completes.py tests/test_storage_retention_rework_protection.py tests/test_janitor_rework_seal.py tests/test_terminal_log_retention.py tests/test_task_retention.py`. Expected: PASS.

- [ ] **Step 6: Commit** only the allowed_writes: `feat(janitor): six-step automatic cleanup with deduplicated failure NeedFix`.

**Acceptance:** the spec §6 acceptance tests 1-10 each map to a passing test in Tasks 1-4, and the manager checks that mapping at review.

---

### Task 5 (manager, one-time): residue that has no creator

Not a card. The manager does this after 0.12.4 is installed and the first janitor run is measured.

- [ ] **Step 1: Measure.** Run `aiworkhub_dashboard_storage_retention_preview` and the terminal-log preview. Compare them with the baseline table in spec §2: 28 worktree directories (8.3 GB), 148 MB of rework deltas, 632 MB of process logs, 47 MB of spill.
- [ ] **Step 2: Ask the owner once** to confirm deleting `.aiworkhub/runtime/tmphziap7b7`, `.aiworkhub/runtime/tmptqfiwls6` and `.aiworkhub/runtime/diagnostics/`. No code creates them any more.
- [ ] **Step 3: Archive** `AIWORKHUB_COMPLETION_INBOX_WORKER_FAILED_FIX_0956_CODEX_20260814_V1` (`request_missing`, so the janitor cannot fence-check it) with `aiworkhub_manager_task_archive`.
- [ ] **Step 4: Record** the before/after numbers on the board item `mech-automation`.

---

## Self-Review

**Spec coverage:**

| Spec section | Task |
|---|---|
| §3 Lane A (unreachable GC) | 1 |
| §3 Lane B (logs quarantined forever) | 2 |
| §3 Lane C (retired) | 4 |
| §4.1 six steps and triggers | 4 (triggers already call the scheduler; no change needed at those sites) |
| §4.2 lifetimes | 1, 2, 4 |
| §4.3 guards | kept in 1 and 4 |
| §4.4 delete-not-quarantine, 24h guard, stray directories | 2, 4, 5 |
| §4.5 records | 3 |
| §4.6 NeedFix dedupe | 4 |
| §4.7 thread pool | 2, 4 |
| §5 no new config | Global Constraints |
| §6 acceptance 1-10 | Tasks 1-4 |

**Type consistency:** these names are identical wherever they are defined and consumed:
- `attach_rework_delta`, `referenced_rework_delta_digests`;
- `failures`;
- `prune_expired`;
- `supersede_decided_task_callbacks`, `DECIDED_TASK_STATUSES`;
- `run_records_janitor`, `janitor_bookkeeping`, `missing_task_has_local_history_fn`;
- `_purge_batch(ignore_deadline=)`;
- `_janitor_step`, `_JANITOR_ACTIVE`, `_AUTO_HYGIENE_RERUN`.

**Deliberate deviations from the spec** (these need the owner's eye):
1. **Cross-host NeedFix links are not reopened.** A missing task is reopened only when this host has local task events for it. A link that arrived by row union from another host stays untouched. This is narrower than spec criterion 3.
2. **New `dependency_live` fence.** A decided task that an undecided task depends on is not archived, because the dependent requires the status `finished`.
3. **Orphan-log backstop.** Orphan logs older than `logs_days` are now deleted. This overrides the NF-2026-00286 "always protect orphans" rule.
4. **Legacy batches purged.** Legacy storage and log batches without `source` are purged regardless of deadline; only `"source": "manual"` batches keep their undo window.
5. **Unsigned log-manifest source.** Log manifests have no HMAC, so their `source` field is not signed; storage manifests sign it.
6. **Stricter log deletion.** A log is deleted only when the task is decided and its usage is captured (the backfill runs first). This is stricter than the spec's wording.
7. **Things left unchanged:**
   - undecided `finalize_failed` attempts stay protected;
   - the existing blocked non-head family archive is kept;
   - `plan_worktree_reclaim` is not touched.
8. **Ledger churn.** 138 ledger rows are marked retained but their directories are gone. They are handled by step 1's existing path; no separate migration.

**Placeholder scan:** none. Each "read X before writing" line names an exact existing symbol, and the step's test pins the result.
