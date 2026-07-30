from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import task_store  # noqa: E402


def _insert_task(repo: Path, task_id: str, *, status: str) -> None:
    task_store.initialize_repository(repo)
    _readiness, db_path = task_store._require_ready(repo)
    now = "2026-07-22T00:00:00+00:00"
    card = {
        "task_id": task_id,
        "runner": "codex_worker_b891",
        "topic": "task_mcp",
        "allowed_writes": ["out.txt"],
    }
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, priority, "
            "objective, card_json, created_at, updated_at, claimed_by, claimed_at, started_at) "
            "VALUES (?, ?, 'task_mcp', ?, ?, '', '', ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                "codex_worker_b891",
                status,
                "claimed" if status == "processing" else "unclaimed",
                json.dumps(card),
                now,
                now,
                "codex_worker_b891" if status == "processing" else "",
                now if status == "processing" else "",
                now if status == "processing" else "",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def test_archive_removes_pending_card_from_active_lists_and_preserves_events(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _insert_task(repo, "TASK_ARCHIVE_B891", status="pending")

    ok, state = task_store.archive_task(
        repo,
        "TASK_ARCHIVE_B891",
        actor="codex",
        reason="reviewed cleanup",
    )
    assert (ok, state) == (True, "archived")
    assert task_store.list_tasks(repo, status="pending") == []
    assert task_store.list_tasks(repo, status="archived")[0]["task_id"] == "TASK_ARCHIVE_B891"
    assert task_store.get_task_events(repo, "TASK_ARCHIVE_B891")[0]["event"] == "archived"


def test_supersede_removes_processing_orphan_without_deleting_audit(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _insert_task(repo, "TASK_SUPERSEDE_B891", status="processing")

    ok, state = task_store.archive_task(
        repo,
        "TASK_SUPERSEDE_B891",
        actor="codex",
        reason="orphaned canary",
        allow_processing=True,
        operation="superseded",
    )
    assert (ok, state) == (True, "superseded")
    assert task_store.list_tasks(repo, status="processing") == []
    archived = task_store.list_tasks(repo, status="archived")
    assert archived[0]["task_id"] == "TASK_SUPERSEDE_B891"
    assert task_store.get_task_events(repo, "TASK_SUPERSEDE_B891")[0]["event"] == "superseded"


@pytest.mark.parametrize(
    "substatus",
    [
        "exited",
        "validation_failed",
        "worker_failed",
        "launch_failed",
        "scope_rejected",
        "cancelled",
        "timed_out",
    ],
)
def test_mark_terminal_review_routes_processing_task_to_review_for_every_terminal_class(
    tmp_path: Path, substatus: str
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = f"TASK_TERMINAL_{substatus.upper()}"
    _insert_task(repo, task_id, status="processing")

    ok, state = task_store.mark_terminal_review(
        repo,
        task_id,
        runner="codex_worker_b891",
        substatus=substatus,
        evidence={"exit_code": 1},
    )

    assert (ok, state) == (True, "review")
    assert task_store.list_tasks(repo, status="processing") == []
    reviewed = task_store.list_tasks(repo, status="review")
    assert reviewed[0]["task_id"] == task_id
    card = task_store.get_task(repo, task_id)
    assert card["terminal_substatus"] == substatus
    events = [e["event"] for e in task_store.get_task_events(repo, task_id)]
    assert "terminal_review" in events


def test_mark_terminal_review_allows_launch_failed_from_pending(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = "TASK_LAUNCH_FAILED_B894"
    _insert_task(repo, task_id, status="pending")

    ok, state = task_store.mark_terminal_review(
        repo, task_id, runner="codex_worker_b891", substatus="launch_failed"
    )
    assert (ok, state) == (True, "review")
    card = task_store.get_task(repo, task_id)
    assert card["status"] == "review"
    assert card["terminal_substatus"] == "launch_failed"


def test_mark_terminal_review_rejects_illegal_regression_to_pending_from_review(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = "TASK_ILLEGAL_REGRESSION_B892"
    _insert_task(repo, task_id, status="processing")

    ok, state = task_store.mark_terminal_review(
        repo, task_id, runner="codex_worker_b891", substatus="exited"
    )
    assert (ok, state) == (True, "review")

    # A second terminal-review attempt against an already-reviewed task must
    # fail closed instead of silently re-recording (or regressing) the task.
    ok2, state2 = task_store.mark_terminal_review(
        repo, task_id, runner="codex_worker_b891", substatus="worker_failed"
    )
    assert ok2 is False
    assert state2.startswith("illegal_transition:from=review")

    # The task must remain exactly in review -- never pending, never finished.
    card = task_store.get_task(repo, task_id)
    assert card["status"] == "review"
    events = [e["event"] for e in task_store.get_task_events(repo, task_id)]
    assert "illegal_transition_rejected" in events


def test_mark_terminal_review_rejects_unknown_substatus(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = "TASK_UNKNOWN_SUBSTATUS_B895"
    _insert_task(repo, task_id, status="processing")

    ok, state = task_store.mark_terminal_review(
        repo, task_id, runner="codex_worker_b891", substatus="totally_made_up_outcome"
    )
    assert ok is False
    assert state == "illegal_transition:unknown_substatus=totally_made_up_outcome"
    card = task_store.get_task(repo, task_id)
    assert card["status"] == "processing"


def test_mark_terminal_review_known_failure_substatus_never_deterministically_passes(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = "TASK_DETERMINISTIC_FAILURE_B896"
    _insert_task(repo, task_id, status="processing")

    ok, state = task_store.mark_terminal_review(
        repo,
        task_id,
        runner="codex_worker_b891",
        substatus="worker_failed",
        evidence={
            "validation": [{"command": "pytest", "returncode": 0}],
            "required_outputs": [{"path": "out.txt", "sha256": "a" * 64, "bytes": 5}],
        },
    )
    assert (ok, state) == (True, "review")
    card = task_store.get_task(repo, task_id)
    verification = card["deterministic_verification"]
    assert verification["applicable"] is True
    assert verification["pass"] is False
    assert verification["reason"] == "known_failure_substatus"


def test_mark_terminal_review_review_ready_with_no_gates_is_not_applicable(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = "TASK_NO_GATES_B897"
    _insert_task(repo, task_id, status="processing")

    ok, state = task_store.mark_terminal_review(
        repo, task_id, runner="codex_worker_b891", substatus="review_ready"
    )
    assert (ok, state) == (True, "review")
    card = task_store.get_task(repo, task_id)
    verification = card["deterministic_verification"]
    assert verification["applicable"] is False
    assert verification["pass"] is False
    assert verification["reason"] == "no_gates_recorded"


def test_mark_terminal_review_review_ready_passes_only_with_clean_evidence(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = "TASK_CLEAN_EVIDENCE_B898"
    _insert_task(repo, task_id, status="processing")

    ok, state = task_store.mark_terminal_review(
        repo,
        task_id,
        runner="codex_worker_b891",
        substatus="review_ready",
        evidence={
            "validation": [{"command": "pytest", "returncode": 0}],
            "required_outputs": [{"path": "out.txt", "sha256": "a" * 64, "bytes": 5}],
        },
    )
    assert (ok, state) == (True, "review")
    card = task_store.get_task(repo, task_id)
    verification = card["deterministic_verification"]
    assert verification["applicable"] is True
    assert verification["pass"] is True
    assert verification["claim_epoch"] == 0
    assert card["terminal_review"]["claim_epoch"] == 0


def test_mark_terminal_review_review_ready_fails_on_none_returncode(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = "TASK_NONE_RETURNCODE_B899"
    _insert_task(repo, task_id, status="processing")

    ok, state = task_store.mark_terminal_review(
        repo,
        task_id,
        runner="codex_worker_b891",
        substatus="exited",
        evidence={
            "validation": [{"command": "pytest", "returncode": None}],
            "required_outputs": [{"path": "out.txt", "sha256": "a" * 64, "bytes": 5}],
        },
    )
    assert (ok, state) == (True, "review")
    card = task_store.get_task(repo, task_id)
    verification = card["deterministic_verification"]
    assert verification["applicable"] is True
    assert verification["pass"] is False
    assert verification["reason"] == "evidence_verdict_failed"


def _insert_review_ready_task(
    repo: Path,
    task_id: str,
    *,
    request_id: str = "req-coord-b1",
    deterministic_verification: dict | None = None,
) -> None:
    _insert_task(repo, task_id, status="review")
    _readiness, db_path = task_store._require_ready(repo)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE tasks SET claimed_by='codex_worker_b891', worker_status='review' WHERE task_id=?",
            (task_id,),
        )
        card = json.loads(
            conn.execute("SELECT card_json FROM tasks WHERE task_id=?", (task_id,)).fetchone()[0]
        )
        terminal_review = {
            "substatus": "review_ready",
            "evidence": {
                "request_identity": {
                    "request_id": request_id,
                    "task_id": task_id,
                    "runner": "codex_worker_b891",
                },
            },
        }
        if deterministic_verification is not None:
            terminal_review["deterministic_verification"] = deterministic_verification
        card["terminal_review"] = terminal_review
        conn.execute("UPDATE tasks SET card_json=? WHERE task_id=?", (json.dumps(card), task_id))
        conn.commit()
    finally:
        conn.close()


def test_mark_terminal_review_rejects_regression_from_archived(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = "TASK_ILLEGAL_FROM_ARCHIVED_B893"
    _insert_task(repo, task_id, status="pending")
    ok, state = task_store.archive_task(repo, task_id, actor="codex", reason="done")
    assert (ok, state) == (True, "archived")

    ok2, state2 = task_store.mark_terminal_review(
        repo, task_id, runner="codex_worker_b891", substatus="worker_failed"
    )
    assert ok2 is False
    assert state2.startswith("illegal_transition:from=archived")
    card = task_store.get_task(repo, task_id)
    assert card["status"] == "archived"
