"""A duty nobody measures is a duty that quietly stops.

Committing a lesson after an accept or a reject is a manager duty. Nothing
gates it: no card fails, no check goes red, and no surface said it had been
skipped. Measured on the day the review loop first closed end to end: 2 lessons
against 198 decided cards, 1.0%.

The skill registry downstream needs m-of-n independent accepted evidence before
it can activate anything, so with three lessons in the whole repository a skill
producer would be a machine with no fuel. Measuring the input is what has to
come first.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import learning_commit_store, task_store  # noqa: E402


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    task_store.initialize_repository(root)
    return root


def _card(task_id: str, *, status: str, topic: str, age_days: float) -> tuple:
    when = (datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat()
    card = {"task_id": task_id, "runner": "claude", "topic": topic, "status": status}
    return (task_id, "claude", topic, status, "done", "claude",
            json.dumps(card), when, when)


def _seed(root: Path, rows: list[tuple], lessons: list[str]) -> None:
    db = task_store.canonical_db_path(root)
    with sqlite3.connect(db) as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO tasks"
            "(task_id, runner, topic, status, worker_status, claimed_by,"
            " card_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            rows,
        )
        if lessons:
            conn.executescript(learning_commit_store._SCHEMA)
        for task_id in lessons:
            conn.execute(
                "INSERT INTO learning_commits(commit_id, idempotency_key, task_id,"
                " request_id, repository_id, repo_area, outcome, payload_json,"
                " payload_sha256, projections_json, state, manager_id,"
                " manager_provider, provenance, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (task_id + "-c", task_id + "-k", task_id, "r", "repo", "src",
                 "accepted", "{}", "0" * 64, "{}", "completed", "m", "claude",
                 "manager_accept_review", "2026-09-02", "2026-09-02"),
            )


def test_coverage_counts_decided_cards_against_lessons(tmp_path: Path):
    root = _repo(tmp_path)
    _seed(root, [
        _card("A", status="finished", topic="coding", age_days=1),
        _card("B", status="finished", topic="coding", age_days=2),
        _card("C", status="blocked", topic="coding", age_days=3),
    ], lessons=["A"])

    result = learning_commit_store.coverage(root)
    assert result["decided_cards"] == 3
    assert result["cards_with_lesson"] == 1
    assert result["cards_without_lesson"] == 2
    assert result["coverage_percent"] == 33.3
    assert set(result["recent_without_lesson"]) == {"B", "C"}


def test_a_reviewer_child_is_not_a_decision_about_code(tmp_path: Path):
    """Left in, reviewer runs dominated the denominator.

    Every one of the five most recent uncommitted cards on this repository was
    a reviewer child, and a lesson drawn from one would be a lesson about
    reviewing rather than about the code.
    """
    root = _repo(tmp_path)
    _seed(root, [
        _card("REAL", status="finished", topic="coding", age_days=1),
        _card("REVIEW_1", status="finished", topic="quality_review", age_days=1),
        _card("REVIEW_2", status="finished", topic="quality_review", age_days=1),
    ], lessons=[])

    result = learning_commit_store.coverage(root)
    assert result["decided_cards"] == 1
    assert result["recent_without_lesson"] == ["REAL"]


def test_cards_older_than_the_window_are_not_counted(tmp_path: Path):
    """History decided before the path existed is not a failure of practice."""
    root = _repo(tmp_path)
    _seed(root, [
        _card("RECENT", status="finished", topic="coding", age_days=1),
        _card("ANCIENT", status="finished", topic="coding", age_days=90),
    ], lessons=[])

    result = learning_commit_store.coverage(root, window_days=14)
    assert result["decided_cards"] == 1
    assert result["recent_without_lesson"] == ["RECENT"]


def test_an_undecided_card_owes_no_lesson(tmp_path: Path):
    root = _repo(tmp_path)
    _seed(root, [
        _card("RUNNING", status="processing", topic="coding", age_days=1),
        _card("WAITING", status="review", topic="coding", age_days=1),
    ], lessons=[])

    result = learning_commit_store.coverage(root)
    assert result["decided_cards"] == 0
    assert result["coverage_percent"] is None, "no denominator means no percentage"


def test_the_uncommitted_sample_is_bounded(tmp_path: Path):
    root = _repo(tmp_path)
    _seed(root, [
        _card(f"T{i}", status="finished", topic="coding", age_days=1)
        for i in range(20)
    ], lessons=[])

    result = learning_commit_store.coverage(root)
    assert result["decided_cards"] == 20
    assert len(result["recent_without_lesson"]) == 5
