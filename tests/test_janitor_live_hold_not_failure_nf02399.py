"""NF-2026-02399: a workspace held by a live candidate is not a cleanup failure.

A rework relaunch that still holds its workspace makes the GC report
``cleanup_failed:workspace_cleanup_live_candidate_retained:...``. That hold is
the intended behaviour: a completed sweep whose only failures are holds must
file nothing and resolve an existing ``janitor:worktrees:cleanup_failed``
NeedFix. A real cleanup failure still files, with the holds kept out of its
evidence, and a capped report of holds proves nothing.
"""

from __future__ import annotations

import json
from pathlib import Path

from aiworkhub import needfix_store, process_launcher, storage_retention

CLEANUP_TITLE = "janitor:worktrees:cleanup_failed"
HOLD_MARKER = "workspace_cleanup_live_candidate_retained"
REAL = {"request_id": "req-1", "task_id": "T1", "reason": "cleanup_failed:busy"}
HOLD = {
    "request_id": "req-2",
    "task_id": "T2",
    "reason": f"cleanup_failed:{HOLD_MARKER}:live card T2 holds it",
}


def _sweep(repo: Path, failures: list[dict]) -> dict:
    # ``needfix`` is the list the janitor appends filed ids to; a real sweep starts with it.
    result: dict = {
        "needfix": [],
        "worktrees": {"gc_scanned": len(failures), "failures": failures},
    }
    storage_retention._file_worktree_failure_needfix(repo, result, completed=True)
    return result


def _cleanup_rows(repo: Path) -> list[dict]:
    return [
        row
        for row in needfix_store.list_needfix(repo, limit=500)
        if row["title"] == CLEANUP_TITLE
    ]


def test_a_sweep_whose_only_failure_is_a_live_hold_files_nothing(tmp_path: Path) -> None:
    # Seed the store through its public API so the listing below has a table to read.
    needfix_store.add_needfix(tmp_path, title="unrelated seed", description="seed")

    result = _sweep(tmp_path, [HOLD])

    assert result["needfix"] == []
    assert _cleanup_rows(tmp_path) == []


def test_a_live_hold_only_sweep_resolves_the_existing_cleanup_failed_needfix(
    tmp_path: Path,
) -> None:
    _sweep(tmp_path, [REAL])
    (filed,) = _cleanup_rows(tmp_path)
    assert filed["status"] == "captured"

    second = _sweep(tmp_path, [HOLD])

    assert second["needfix_resolved"] == [filed["id"]]
    (row,) = _cleanup_rows(tmp_path)
    assert row["status"] == "resolved"


def test_a_real_cleanup_failure_still_files_and_keeps_holds_out_of_evidence(
    tmp_path: Path,
) -> None:
    result = _sweep(tmp_path, [HOLD, REAL])

    (row,) = _cleanup_rows(tmp_path)
    assert row["status"] == "captured"
    assert result["needfix"] == [row["id"]]
    recorded = json.dumps(needfix_store.get_needfix(tmp_path, row["id"]))
    assert "busy" in recorded
    assert HOLD_MARKER not in recorded


def test_a_capped_report_of_live_holds_resolves_nothing(tmp_path: Path) -> None:
    _sweep(tmp_path, [REAL])
    capped = [
        dict(HOLD, request_id=f"req-{index}")
        for index in range(process_launcher._GC_FAILURE_REPORT_LIMIT)
    ]

    second = _sweep(tmp_path, capped)

    assert "needfix_resolved" not in second
    (row,) = _cleanup_rows(tmp_path)
    assert row["status"] == "captured"
