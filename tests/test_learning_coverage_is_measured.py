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


# --------------------------------------------------------------------------- #
# the head run
#
# A percentage says what the practice has been; it moves about a point per
# lesson, so no single decision can be held to it. The run of most-recently
# decided cards that recorded no lesson can be: filing one lesson for the newest
# decision resets it to zero. That is what `declared_invariants` enforces, and
# these pin the measurement it enforces on.
# --------------------------------------------------------------------------- #


def _aged(task_id: str, days: float) -> tuple:
    return _card(task_id, status="finished", topic="coding", age_days=days)


def test_the_head_run_counts_only_the_newest_decisions(tmp_path: Path):
    """The 38-decision run behind a lesson is history, not the run in progress."""
    root = _repo(tmp_path)
    _seed(root, [
        _aged("NEW_2", 1), _aged("NEW_1", 2),
        _aged("COMMITTED", 3),
        _aged("OLD_1", 4), _aged("OLD_2", 5), _aged("OLD_3", 6),
    ], lessons=["COMMITTED"])

    result = learning_commit_store.coverage(root)

    assert result["consecutive_recent_without_lesson"] == 2
    assert result["cards_without_lesson"] == 5, "the aggregate still counts them all"


def test_one_lesson_for_the_newest_decision_resets_the_head_run(tmp_path: Path):
    root = _repo(tmp_path)
    rows = [_aged(f"T{i}", 1 + i) for i in range(10)]

    _seed(root, rows, lessons=[])
    assert learning_commit_store.coverage(root)["consecutive_recent_without_lesson"] == 10

    _seed(root, rows, lessons=["T0"])
    assert learning_commit_store.coverage(root)["consecutive_recent_without_lesson"] == 0


def test_a_repository_that_never_recorded_a_lesson_has_no_store_and_a_true_run(
    tmp_path: Path,
):
    """The table is created lazily, so "no table" must measure, not raise."""
    root = _repo(tmp_path)
    _seed(root, [_aged("A", 1), _aged("B", 2)], lessons=[])

    result = learning_commit_store.coverage(root)

    assert result["consecutive_recent_without_lesson"] == 2
    assert result["consecutive_recent_without_lesson_capped"] is False


def test_a_run_reported_at_the_scan_limit_says_it_is_a_floor(tmp_path: Path, monkeypatch):
    """A bound must never read as a total."""
    monkeypatch.setattr(learning_commit_store, "RECENT_DECISION_SCAN_LIMIT", 3)
    root = _repo(tmp_path)
    _seed(root, [_aged(f"T{i}", 1 + i) for i in range(5)], lessons=[])

    result = learning_commit_store.coverage(root)

    assert result["consecutive_recent_without_lesson"] == 3
    assert result["consecutive_recent_without_lesson_capped"] is True


def test_an_undecided_repository_reports_no_run_and_no_percentage(tmp_path: Path):
    root = _repo(tmp_path)
    _seed(root, [_card("RUNNING", status="processing", topic="coding", age_days=1)], [])

    result = learning_commit_store.coverage(root)

    assert result["decided_cards"] == 0
    assert result["consecutive_recent_without_lesson"] == 0
    assert result["coverage_percent"] is None
