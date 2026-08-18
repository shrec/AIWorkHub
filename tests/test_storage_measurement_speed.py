"""The terminal-log storage measurement must be fast, exact, and honest.

The dashboard's ``Calculating`` state was dominated by
``terminal_log_retention.preview``: on the canonical repository (8282 process-log
files, 2420 ledger request ids) it spent ~55 s calling ``task_store.get_task``
once per terminal request -- an N+1 query that reopened the 60 MB task DB ~2121
times at ~26 ms each.  The filesystem scan (0.04 s), ledger read (0.8 s) and
usage-capture read (0.03 s) were never the cost.

The fix reads every task's status in one bulk ``list_tasks`` query and joins in
memory, taking the same measurement (verified byte-identical: same candidates,
bytes, ordering and digest) from ~55 s to ~1 s on that repository.  With the N+1
gone the residual cost is the ledger read plus one directory stat of every
per-request file; the stat is IO-bound (``lstat`` releases the GIL) and runs
across a core-derived worker count on a large store.  These tests pin the
properties that must hold together:

1. The bulk join is result-identical to the per-request ``get_task`` path.
2. ``preview`` no longer issues a per-request ``get_task`` (the N+1 is gone).
3. ``partial`` is an honest flag: False for a complete population, True when a
   supplied deadline cut the measurement short -- never the permanent False a
   downstream report found, where a call that measured something still claimed a
   whole (and therefore possibly clean) result.
4. The parallel directory walk is byte-identical to the sequential one (same
   request ids, same per-request file order): parallelism changes only how fast,
   never what is measured.
5. The stat worker count is derived from the observed core count and leaves two
   cores free for the interactive MCP server -- never a constant, never every
   core -- and a small walk stays sequential so no contention is added for a walk
   that is already trivial.
6. A not-ready store (the dashboard measures a repo before it is initialized)
   surfaces as the module's own ``TerminalLogRetentionError``, never as a bare
   ``task_store`` error that turns a clean empty result into a hard scan failure.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aiworkhub import task_store, terminal_log_retention


# A fixed far-future clock so every just-written run is already older than
# ``logs_days`` without mutating filesystem mtimes (os.utime is denied in the
# validation sandbox), matching the sibling retention suites.
_FROZEN_NOW = datetime(2035, 1, 1, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _freeze_retention_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(terminal_log_retention, "now_utc", lambda: _FROZEN_NOW)


# task_id -> (status, worker_status).  A finished or archived task whose run has
# a usage-capture receipt is eligible; an active task is protected; a request
# whose task_id is absent from the store falls to "unknown" and stays protected.
_TASKS = {
    "TASK_FIN": ("finished", "done"),
    "TASK_ARC": ("archived", "done"),
    "TASK_ACT": ("in_progress", "running"),
}


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    assert task_store.initialize_repository(repo)["ok"]
    readiness = task_store.storage_readiness(repo)
    now = "2026-01-01T00:00:00+00:00"
    conn = sqlite3.connect(readiness.canonical_db)
    try:
        for task_id, (status, worker_status) in _TASKS.items():
            conn.execute(
                "INSERT INTO tasks(task_id,runner,topic,status,worker_status,card_json,"
                "created_at,updated_at,completed_at,origin_thread_id) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (task_id, "runner", "topic", status, worker_status, "{}", now, now, now, "thread"),
            )
        conn.commit()
    finally:
        conn.close()
    return repo


def _run(repo: Path, request_id: str, task_id: str, *, with_receipt: bool = True) -> None:
    process_root = repo / terminal_log_retention.PROCESS_FILES_RELATIVE_PATH
    process_root.mkdir(parents=True, exist_ok=True)
    for suffix in terminal_log_retention._OWNED_SUFFIXES:
        (process_root / f"{request_id}{suffix}").write_text(f"{request_id}\n", encoding="utf-8")
    ledger = repo / terminal_log_retention.PROCESS_LOG_RELATIVE_PATH
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "request_id": request_id,
            "task_id": task_id,
            "runner": "runner",
            "topic": "topic",
            "adapter_id": "codex_cli",
            "state": "exited",
            "finished_at": "2026-01-01T00:00:00+00:00",
        }) + "\n")
    if with_receipt:
        ok, reason = task_store.append_usage_capture_event(
            repo, task_id, "runner",
            {
                "source": "task_mcp_launcher",
                "note": f"task_mcp_request:{request_id}",
                "usage_observed": False,
                "telemetry_reason": "provider_usage_report_not_observed",
            },
        )
        assert ok, reason


def _populated_repo(tmp_path: Path) -> Path:
    """A repo whose runs exercise every classification branch."""

    repo = _repo(tmp_path)
    # Two eligible candidates (finished + archived, both with receipts), one run
    # held back by an active task, one held back by a missing receipt, and one
    # whose task id is absent from the store entirely (falls to "unknown").
    _run(repo, f"{1:032x}", "TASK_FIN")
    _run(repo, f"{2:032x}", "TASK_ARC")
    _run(repo, f"{3:032x}", "TASK_ACT")
    _run(repo, f"{4:032x}", "TASK_FIN", with_receipt=False)
    # A request whose task id is absent from the store: a usage receipt cannot be
    # written for a non-existent task, so it has none, and its "unknown" status
    # keeps it protected either way.
    _run(repo, f"{5:032x}", "TASK_MISSING", with_receipt=False)
    return repo


def _sequential_status_map(root: Path) -> dict[str, str]:
    """The pre-fix per-request path: one ``get_task`` per ledger request id."""

    latest = terminal_log_retention._latest_rows(root)
    mapping: dict[str, str] = {}
    for row in latest.values():
        task_id = str(row.get("task_id") or "")
        if task_id and task_id not in mapping:
            task = task_store.get_task(root, task_id)
            mapping[task_id] = str((task or {}).get("status") or "unknown")
    return mapping


def test_bulk_status_join_is_identical_to_per_task_get_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The bulk path may change only how fast, never what is measured: same
    # candidate set, same bytes, same contractual ordering, same digest as the
    # sequential ``get_task`` path.
    repo = _populated_repo(tmp_path)

    bulk = terminal_log_retention.preview(repo, include_candidates=True)

    monkeypatch.setattr(terminal_log_retention, "_task_status_map", _sequential_status_map)
    sequential = terminal_log_retention.preview(repo, include_candidates=True)

    assert bulk["candidates"] == sequential["candidates"]
    assert bulk["candidate_count"] == sequential["candidate_count"]
    assert bulk["candidate_bytes"] == sequential["candidate_bytes"]
    assert bulk["protected"] == sequential["protected"]
    assert bulk["protected_count"] == sequential["protected_count"]
    assert bulk["current_bytes"] == sequential["current_bytes"]
    assert bulk["preview_digest"] == sequential["preview_digest"]
    # The population is real: exactly the two receipted terminal runs are
    # candidates, in the contractual sorted-by-request-id order.
    ids = [item["request_id"] for item in bulk["candidates"]]
    assert ids == [f"{1:032x}", f"{2:032x}"]


def test_preview_issues_no_per_request_get_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression guard for the 55 s N+1: the status join is a single bulk query,
    # so ``preview`` must complete without ever calling ``get_task`` per request.
    repo = _populated_repo(tmp_path)

    def _forbidden_get_task(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("preview must not call get_task per request")

    monkeypatch.setattr(task_store, "get_task", _forbidden_get_task)

    result = terminal_log_retention.preview(repo, include_candidates=True)
    assert result["candidate_count"] == 2


def test_complete_measurement_reports_partial_false(tmp_path: Path) -> None:
    # A measurement that ran to completion is not partial, and it says so.
    repo = _populated_repo(tmp_path)

    result = terminal_log_retention.preview(repo)

    assert result["partial"] is False
    assert result["candidate_count"] == 2


def test_cut_short_measurement_reports_partial_true(tmp_path: Path) -> None:
    # The permanently-false ``partial`` is fixed: a measurement cut short by its
    # deadline reports partial=True rather than presenting an incomplete (and
    # therefore possibly falsely clean) population as a whole one.
    repo = _populated_repo(tmp_path)

    cut = terminal_log_retention.preview(repo, deadline_seconds=0.0)
    assert cut["partial"] is True

    # A generous deadline still completes and is not partial -- the flag tracks
    # the measurement, it is not permanently one value.
    complete = terminal_log_retention.preview(repo, deadline_seconds=60.0)
    assert complete["partial"] is False
    assert complete["candidate_count"] == 2


def test_parallel_directory_walk_is_identical_to_sequential(tmp_path: Path) -> None:
    # Parallelism may change how fast the per-request stat runs, never what it
    # measures: the inventory built across workers must be byte-identical to the
    # single-threaded walk -- same request ids and same per-request file order.
    repo = _repo(tmp_path)
    for index in range(1, 40):
        _run(repo, f"{index:032x}", "TASK_FIN")
    process_root = repo / terminal_log_retention.PROCESS_FILES_RELATIVE_PATH

    sequential = terminal_log_retention._build_inventory(process_root, workers=1)
    parallel = terminal_log_retention._build_inventory(process_root, workers=8)

    assert parallel == sequential
    assert len(sequential) == 39
    # Order within each request's file list is identical, not merely set-equal.
    for request_id, files in sequential.items():
        assert parallel[request_id] == files


def test_preview_is_result_identical_when_the_walk_runs_in_parallel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The whole preview must be identical whether its directory walk ran on one
    # thread or a pool: same candidates, bytes, protected set, ordering and digest.
    repo = _populated_repo(tmp_path)

    baseline = terminal_log_retention.preview(repo, include_candidates=True)

    # Force every walk onto the real thread-pool path.
    monkeypatch.setattr(terminal_log_retention, "_PARALLEL_STAT_THRESHOLD", 1)
    monkeypatch.setattr(
        terminal_log_retention, "_scan_worker_count", lambda count: 4 if count > 1 else 1
    )
    parallel = terminal_log_retention.preview(repo, include_candidates=True)

    assert parallel["candidates"] == baseline["candidates"]
    assert parallel["preview_digest"] == baseline["preview_digest"]
    assert parallel["candidate_bytes"] == baseline["candidate_bytes"]
    assert parallel["protected"] == baseline["protected"]
    assert parallel["current_bytes"] == baseline["current_bytes"]


def test_scan_worker_count_derives_from_cores_and_leaves_headroom(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A small walk stays single-threaded -- a thread pool would only be overhead.
    assert terminal_log_retention._scan_worker_count(0) == 1
    assert (
        terminal_log_retention._scan_worker_count(
            terminal_log_retention._PARALLEL_STAT_THRESHOLD - 1
        )
        == 1
    )
    # A large walk derives its worker count from the observed core count and never
    # takes every core: two are always left free for the interactive MCP server and
    # the dashboard's own request thread. It tracks os.cpu_count(), never a constant.
    monkeypatch.setattr(terminal_log_retention.os, "cpu_count", lambda: 16)
    assert terminal_log_retention._scan_worker_count(10_000) == 14
    monkeypatch.setattr(terminal_log_retention.os, "cpu_count", lambda: 8)
    assert terminal_log_retention._scan_worker_count(10_000) == 6
    # Degenerate core counts fail closed to a single worker, never zero/negative.
    monkeypatch.setattr(terminal_log_retention.os, "cpu_count", lambda: 1)
    assert terminal_log_retention._scan_worker_count(10_000) == 1
    monkeypatch.setattr(terminal_log_retention.os, "cpu_count", lambda: None)
    assert terminal_log_retention._scan_worker_count(10_000) == 1


def test_preview_on_a_not_ready_store_surfaces_the_module_error(tmp_path: Path) -> None:
    # Regression: the bulk status join must not let a task_store StorageNotReadyError
    # (a TaskStoreError) escape preview. A not-ready store is a normal telemetry
    # condition -- the dashboard measures a repo before it is initialized -- and the
    # enclosing measurement only treats TerminalLogRetentionError as a handled
    # telemetry failure. A raw TaskStoreError here turned a not-ready store into a
    # hard background-scan "error"; it must surface as the module's own error type.
    repo = tmp_path / "unready"
    repo.mkdir()

    # StorageNotReadyError is a TaskStoreError, an unrelated hierarchy from
    # TerminalLogRetentionError, so this raises only if the module error is used.
    with pytest.raises(terminal_log_retention.TerminalLogRetentionError):
        terminal_log_retention.preview(repo)
