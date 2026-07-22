from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import task_engine, task_store  # noqa: E402


def _repo_with_task(tmp_path: Path, *, status: str = "processing") -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_store.initialize_repository(repo)
    _readiness, db_path = task_store._require_ready(repo)
    now = "2026-07-22T00:00:00+00:00"
    card = {
        "task_id": "TASK_B891",
        "runner": "codex_worker_b891",
        "topic": "task_mcp",
        "allowed_writes": ["out.txt"],
    }
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, priority, "
            "objective, card_json, created_at, updated_at, claimed_by, claimed_at, started_at) "
            "VALUES (?, ?, ?, ?, ?, '', '', ?, ?, ?, ?, ?, ?)",
            (
                "TASK_B891",
                "codex_worker_b891",
                "task_mcp",
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
    return repo


@pytest.mark.parametrize(
    "substatus",
    [
        "worker_failed",
        "validation_failed",
        "required_output_unchanged",
        "blocked",
        "cancelled",
        "launch_failed",
        "liveness_lost",
    ],
)
def test_terminal_outcomes_route_to_review_with_exact_substatus(tmp_path: Path, substatus: str) -> None:
    repo = _repo_with_task(tmp_path)
    result = task_engine.mark_terminal_review(
        repo,
        "TASK_B891",
        "codex_worker_b891",
        substatus,
        evidence={"error": substatus, "request_id": "req-b891"},
    )
    assert result["ok"] is True

    card = task_store.get_task(repo, "TASK_B891")
    assert card is not None
    assert card["status"] == "review"
    assert card["worker_status"] == "review"
    assert card["terminal_substatus"] == substatus
    assert card["terminal_review"]["evidence"]["error"] == substatus
    events = task_store.get_task_events(repo, "TASK_B891")
    assert events[0]["event"] == "terminal_review"


def _repo_with_review_ready_task(tmp_path: Path, *, request_id: str = "req-accept-b1") -> Path:
    repo = _repo_with_task(tmp_path, status="review")
    _readiness, db_path = task_store._require_ready(repo)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE tasks SET claimed_by='codex_worker_b891' WHERE task_id=?",
            ("TASK_B891",),
        )
        card = json.loads(
            conn.execute(
                "SELECT card_json FROM tasks WHERE task_id=?", ("TASK_B891",)
            ).fetchone()[0]
        )
        card["terminal_review"] = {
            "substatus": "review_ready",
            "evidence": {
                "changed_paths": ["out.txt"],
                "changed_path_hashes": {"out.txt": "deadbeef"},
                "request_identity": {
                    "request_id": request_id,
                    "task_id": "TASK_B891",
                    "runner": "codex_worker_b891",
                    "topic": "task_mcp",
                },
            },
        }
        conn.execute(
            "UPDATE tasks SET status='review', worker_status='review', card_json=? "
            "WHERE task_id=?",
            (json.dumps(card), "TASK_B891"),
        )
        conn.commit()
    finally:
        conn.close()
    return repo


def test_accept_review_promotes_and_finishes(tmp_path: Path) -> None:
    repo = _repo_with_review_ready_task(tmp_path)
    result = task_engine.accept_review(
        repo,
        "TASK_B891",
        runner="codex_worker_b891",
        topic="task_mcp",
        request_id="req-accept-b1",
        evidence={"promoted_paths": ["out.txt"]},
    )
    assert result["ok"] is True

    card = task_store.get_task(repo, "TASK_B891")
    assert card is not None
    assert card["status"] == "finished"
    assert card["worker_status"] == "done"
    assert card["accepted_request_id"] == "req-accept-b1"
    events = task_store.get_task_events(repo, "TASK_B891")
    assert events[0]["event"] == "accept_review"


def test_accept_review_idempotent_retry_returns_already_accepted(tmp_path: Path) -> None:
    repo = _repo_with_review_ready_task(tmp_path)
    first = task_engine.accept_review(
        repo, "TASK_B891", runner="codex_worker_b891", topic="task_mcp",
        request_id="req-accept-b1",
    )
    assert first["ok"] is True
    retry = task_engine.accept_review(
        repo, "TASK_B891", runner="codex_worker_b891", topic="task_mcp",
        request_id="req-accept-b1",
    )
    assert retry["ok"] is True
    assert json.loads(retry["stdout"])["already_accepted"] is True


def test_accept_review_rejects_different_request_after_finish(tmp_path: Path) -> None:
    repo = _repo_with_review_ready_task(tmp_path)
    first = task_engine.accept_review(
        repo, "TASK_B891", runner="codex_worker_b891", topic="task_mcp",
        request_id="req-accept-b1",
    )
    assert first["ok"] is True
    other = task_engine.accept_review(
        repo, "TASK_B891", runner="codex_worker_b891", topic="task_mcp",
        request_id="req-accept-other",
    )
    assert other["ok"] is False
    assert "already_finished" in other["stderr"]


def test_accept_review_rejects_non_review_ready_substatus(tmp_path: Path) -> None:
    repo = _repo_with_review_ready_task(tmp_path)
    _readiness, db_path = task_store._require_ready(repo)
    conn = sqlite3.connect(db_path)
    try:
        card = json.loads(
            conn.execute(
                "SELECT card_json FROM tasks WHERE task_id=?", ("TASK_B891",)
            ).fetchone()[0]
        )
        card["terminal_review"]["substatus"] = "worker_failed"
        conn.execute(
            "UPDATE tasks SET card_json=? WHERE task_id=?",
            (json.dumps(card), "TASK_B891"),
        )
        conn.commit()
    finally:
        conn.close()
    result = task_engine.accept_review(
        repo, "TASK_B891", runner="codex_worker_b891", topic="task_mcp",
        request_id="req-accept-b1",
    )
    assert result["ok"] is False
    assert "terminal_substatus_not_review_ready" in result["stderr"]
    card = task_store.get_task(repo, "TASK_B891")
    assert card is not None
    assert card["status"] == "review"
