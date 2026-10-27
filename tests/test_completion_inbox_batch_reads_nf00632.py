"""NF-2026-00632: the completion inbox reads each bucket in one task_store snapshot."""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import completion_inbox  # noqa: E402
from aiworkhub import core  # noqa: E402
from aiworkhub import task_store  # noqa: E402

_EPOCH = datetime(2026, 9, 1, tzinfo=timezone.utc)
_BUCKETS = ("pending", "processing", "review", "blocked")

# (task_id, topic column, status, worker_status, card_json, archived_at)
_TASKS: tuple[tuple[str, str, str, str, Any, str], ...] = (
    ("T_PENDING_1", "alpha", "pending", "", {"validation_status": "passed", "note": "p1"}, ""),
    ("T_PENDING_2", "beta", "pending", "", {"allowed_writes": ["a.py"]}, ""),
    ("T_PENDING_CARD_TOPIC", "", "pending", "", {"topic": "alpha", "priority_note": 3}, ""),
    ("T_PROCESSING_1", "alpha", "in_progress", "", {"request_id": "r1"}, ""),
    ("T_PROCESSING_CLAIMED", "beta", "pending", "claimed", {}, ""),
    ("T_REVIEW_1", "alpha", "review", "", {"terminal_substatus": "review_ready"}, ""),
    ("T_REVIEW_WORKER", "alpha", "pending", "ready_for_review", {"nested": {"k": [1, 2]}}, ""),
    ("T_BLOCKED_1", "beta", "blocked_on_dependency", "", {"blocker_reason": "dep"}, ""),
    ("T_BLOCKED_DEFERRED", "alpha", "pending", "deferred_manual", {}, ""),
    ("T_FINISHED_1", "alpha", "finished", "done", {}, ""),
    ("T_SUPERSEDED_1", "alpha", "superseded", "", {}, ""),
    ("T_ARCHIVED_1", "alpha", "review", "", {}, "2026-09-02T00:00:00+00:00"),
    ("T_PENDING_3", "alpha", "pending", "", {}, ""),
)

_EXPECTED_BUCKETS = {
    "pending": {"T_PENDING_1", "T_PENDING_2", "T_PENDING_CARD_TOPIC", "T_PENDING_3"},
    "processing": {"T_PROCESSING_1", "T_PROCESSING_CLAIMED"},
    "review": {"T_REVIEW_1", "T_REVIEW_WORKER"},
    "blocked": {"T_BLOCKED_1", "T_BLOCKED_DEFERRED"},
}


def _at(offset_seconds: int) -> str:
    return (_EPOCH + timedelta(seconds=offset_seconds)).isoformat()


def _seed(root: Path, tasks=_TASKS) -> None:
    task_store.initialize_repository(root)
    _readiness, db_path = task_store._require_ready(root)
    conn = sqlite3.connect(db_path)
    try:
        for index, (task_id, topic, status, worker_status, card, archived_at) in enumerate(tasks):
            conn.execute(
                "INSERT INTO tasks(task_id, runner, topic, status, worker_status, priority, "
                "objective, card_json, created_at, updated_at, archived_at) "
                "VALUES (?, 'codex_worker', ?, ?, ?, '', 'objective', ?, ?, ?, ?)",
                (
                    task_id,
                    topic,
                    status,
                    worker_status,
                    card if isinstance(card, str) else json.dumps(card),
                    _at(index),
                    _at(index),
                    archived_at,
                ),
            )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    _seed(tmp_path)
    monkeypatch.setattr(core, "repo_root", lambda: tmp_path)
    return tmp_path


def _forbid_per_card_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args, **_kwargs):
        raise AssertionError("per-card taskctl read on the production path")

    monkeypatch.setattr(core, "show_task", refuse)
    monkeypatch.setattr(core, "list_tasks", refuse)


def _ids_by_bucket(result: dict[str, Any]) -> dict[str, set[str]]:
    by_bucket: dict[str, set[str]] = {bucket: set() for bucket in _BUCKETS}
    for fact in result["latest_validation_facts"]:
        by_bucket[fact["lifecycle_state"]].add(fact["task_id"])
    return by_bucket


def test_production_path_issues_no_show_task(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    real = task_store.list_task_cards

    def spy(root, **kwargs):
        calls.append(dict(kwargs))
        return real(root, **kwargs)

    monkeypatch.setattr(task_store, "list_task_cards", spy)
    _forbid_per_card_reads(monkeypatch)

    result = completion_inbox.build_completion_inbox(limit=200)

    assert result["read_errors"] == []
    assert result["fetch_errors"] == []
    assert _ids_by_bucket(result) == _EXPECTED_BUCKETS
    assert [call["status"] for call in calls] == list(_BUCKETS)
    assert all(call["topic"] is None and call["limit"] == 200 for call in calls)
    assert result["counts"]["pending_scanned"] == 4


def test_topic_filter_on_production_path(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_per_card_reads(monkeypatch)
    result = completion_inbox.build_completion_inbox(topic="alpha", limit=200)
    assert result["read_errors"] == []
    assert _ids_by_bucket(result) == {
        "pending": {"T_PENDING_1", "T_PENDING_CARD_TOPIC", "T_PENDING_3"},
        "processing": {"T_PROCESSING_1"},
        "review": {"T_REVIEW_1", "T_REVIEW_WORKER"},
        "blocked": {"T_BLOCKED_DEFERRED"},
    }


@pytest.mark.parametrize("status", (*_BUCKETS, "finished", "superseded", "archived", "all"))
def test_batched_cards_equal_show_task(repo: Path, status: str) -> None:
    cards = task_store.list_task_cards(repo, status=status, limit=200, decode_errors=[])
    assert cards
    for card in cards:
        shown, error = completion_inbox._show_one_card(core.show_task, card["task_id"])
        assert error is None
        assert shown == card


@pytest.mark.parametrize("status", (*_BUCKETS, "finished", "superseded", "archived", "all"))
@pytest.mark.parametrize("topic", (None, "alpha", "beta", "missing"))
@pytest.mark.parametrize("limit", (1, 2, 200))
def test_selection_matches_core_list_tasks(repo: Path, status: str, topic: str | None, limit: int) -> None:
    listed = core.list_tasks(status=status, topic=topic, limit=limit)
    expected = [row["task_id"] for row in completion_inbox._parse_list_task_ids(listed["stdout"])]
    cards = task_store.list_task_cards(repo, status=status, topic=topic, limit=limit)
    assert [card["task_id"] for card in cards] == expected
    updated = [card["updated_at"] for card in cards]
    assert updated == sorted(updated, reverse=True)


def test_malformed_card_is_one_bounded_show_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed(tmp_path, (*_TASKS, ("T_BAD_CARD", "alpha", "pending", "", "{not json" + "x" * 500, "")))
    monkeypatch.setattr(core, "repo_root", lambda: tmp_path)
    _forbid_per_card_reads(monkeypatch)

    result = completion_inbox.build_completion_inbox(limit=200)

    assert len(result["read_errors"]) == 1
    error = result["read_errors"][0]
    assert error["scope"] == "show"
    assert error["task_id"] == "T_BAD_CARD"
    assert error["error_kind"] == "json_parse_error"
    assert len(error["error_message"]) <= 200
    assert [entry["task_id"] for entry in result["fetch_errors"]] == ["T_BAD_CARD"]
    assert _ids_by_bucket(result) == _EXPECTED_BUCKETS


def test_non_object_card_is_invalid_card(tmp_path: Path) -> None:
    _seed(tmp_path, (*_TASKS, ("T_LIST_CARD", "alpha", "pending", "", [1, 2], "")))
    errors: list[dict[str, Any]] = []
    cards = task_store.list_task_cards(tmp_path, status="pending", decode_errors=errors)
    assert [error["task_id"] for error in errors] == ["T_LIST_CARD"]
    assert errors[0]["error_kind"] == "invalid_card"
    assert {card["task_id"] for card in cards} == _EXPECTED_BUCKETS["pending"]


def test_raising_batch_read_is_a_list_error(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*_args, **_kwargs):
        raise task_store.TaskStoreError("canonical database unavailable")

    monkeypatch.setattr(task_store, "list_task_cards", broken)
    _forbid_per_card_reads(monkeypatch)

    result = completion_inbox.build_completion_inbox(limit=200)

    assert [(e["scope"], e["status"], e["error_kind"]) for e in result["read_errors"]] == [
        ("list", bucket, "exception") for bucket in _BUCKETS
    ]
    assert "canonical database unavailable" in result["read_errors"][0]["error_message"]
    assert result["latest_validation_facts"] == []


def test_injected_seams_keep_list_then_show(repo: Path) -> None:
    shown: list[str] = []

    def stub_list(status, topic, limit):
        return {"returncode": 0, "stdout": "[pending] [alpha] [codex_worker] T_STUB" if status == "pending" else ""}

    def stub_show(task_id):
        shown.append(task_id)
        return {"returncode": 0, "stdout": json.dumps({"task_id": task_id, "status": "pending"})}

    result = completion_inbox.build_completion_inbox(limit=200, _list_tasks=stub_list, _show_task=stub_show)
    assert shown == ["T_STUB"]
    assert _ids_by_bucket(result)["pending"] == {"T_STUB"}


def test_unfiltered_list_task_cards_is_unchanged(tmp_path: Path) -> None:
    _seed(tmp_path, (*_TASKS, ("T_BAD_CARD", "alpha", "pending", "", "{not json", "")))
    assert task_store._LIST_TASK_CARDS_SQL == (
        "SELECT t.* FROM (SELECT rowid AS rid, updated_at AS u FROM tasks "
        "ORDER BY updated_at DESC LIMIT ?) s JOIN tasks t ON t.rowid = s.rid "
        "ORDER BY s.u DESC"
    )
    _readiness, db_path = task_store._require_ready(tmp_path)
    for limit in (1, 5, 500):
        conn = task_store._connect(db_path, readonly=True)
        try:
            rows = conn.execute(task_store._LIST_TASK_CARDS_SQL, (limit,)).fetchall()
        finally:
            conn.close()
        oracle = [task_store._decode_task_card(row) for row in rows]
        assert task_store.list_task_cards(tmp_path, limit=limit) == oracle
        assert task_store.list_task_cards(tmp_path, limit=limit, status="all") == oracle
