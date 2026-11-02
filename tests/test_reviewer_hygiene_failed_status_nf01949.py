from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from aiworkhub import task_retention, task_store

_OLD = "2026-08-31T00:00:00+00:00"
_CUTOFF = datetime(2026, 9, 1, tzinfo=timezone.utc)
_REVIEWER_ID = "FAMILY_E1_SECURITY_V1"


def _row(task_id: str, worker_status: str, *, topic: str = "quality_review") -> dict[str, Any]:
    return {
        "task_id": task_id,
        "topic": topic,
        "status": "blocked",
        "worker_status": worker_status,
        "updated_at": _OLD,
        "completed_at": _OLD,
        "card": {"task_id": task_id, "topic": topic, "launch_request_id": "req-review"},
    }


@pytest.mark.parametrize(
    "worker_status",
    [
        "timed_out",
        "launch_failed",
        "liveness_lost",
        "cancelled",
        "worker_failed",
        "finalize_failed",
        "blocked",
        "done",
    ],
)
def test_blocked_reviewer_is_candidate_for_every_terminal_worker_status(
    worker_status: str,
) -> None:
    rows = [_row(_REVIEWER_ID, worker_status)]

    assert task_retention._candidate_ids(rows, _CUTOFF) == {_REVIEWER_ID}


def test_non_quality_review_reviewer_shaped_row_is_not_selected() -> None:
    rows = [_row(_REVIEWER_ID, "timed_out", topic="coding")]

    assert task_retention._candidate_ids(rows, _CUTOFF) == set()


@pytest.mark.parametrize("callback_state", ["pending", "inflight"])
def test_failed_reviewer_with_live_callback_is_still_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, callback_state: str
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    assert task_store.initialize_repository(repo)["ok"]
    card = _row(_REVIEWER_ID, "timed_out")["card"] | {
        "status": "blocked",
        "worker_status": "timed_out",
        "updated_at": _OLD,
    }
    connection = sqlite3.connect(task_store.canonical_db_path(repo))
    try:
        connection.execute(
            "INSERT INTO callback_outbox(task_id,origin_thread_id,state,created_at,updated_at) "
            "VALUES(?,?,?,?,?)",
            (_REVIEWER_ID, "thread", callback_state, _OLD, _OLD),
        )
        connection.commit()
    finally:
        connection.close()
    monkeypatch.setattr(task_store, "get_task", lambda _root, _task_id: card)
    monkeypatch.setattr(
        task_retention,
        "_latest_process_row",
        lambda _root, _request: {
            "task_id": _REVIEWER_ID,
            "request_id": "req-review",
            "state": "worker_failed",
            "timestamp": "2026-01-01T00:00:01+00:00",
        },
    )

    fenced, reason = task_retention._final_archive_fence(repo, _REVIEWER_ID, "req-review")

    assert fenced is None
    assert reason == "callback_live"


@pytest.mark.parametrize("state", ["starting", "running"])
def test_failed_reviewer_with_live_ledger_is_still_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    assert task_store.initialize_repository(repo)["ok"]
    card = _row(_REVIEWER_ID, "timed_out")["card"] | {
        "status": "blocked",
        "worker_status": "timed_out",
        "updated_at": _OLD,
    }
    monkeypatch.setattr(task_store, "get_task", lambda _root, _task_id: card)
    monkeypatch.setattr(
        task_retention,
        "_latest_process_row",
        lambda _root, _request: {
            "task_id": _REVIEWER_ID,
            "request_id": "req-review",
            "state": state,
            "timestamp": "2026-01-01T00:00:01+00:00",
        },
    )

    fenced, reason = task_retention._final_archive_fence(repo, _REVIEWER_ID, "req-review")

    assert fenced is None
    assert reason == "ledger_live"
