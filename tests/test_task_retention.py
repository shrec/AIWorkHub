from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from aiworkhub import task_retention, task_store


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    assert task_store.initialize_repository(repo)["ok"]
    return repo


def _archived(repo: Path, task_id: str, *, callback_state: str = "delivered") -> None:
    db = task_store.canonical_db_path(repo)
    timestamp = "2025-01-01T00:00:00+00:00"
    connection = sqlite3.connect(db)
    try:
        connection.execute(
            "INSERT INTO tasks(task_id,runner,topic,status,worker_status,card_json,created_at,updated_at,completed_at,origin_thread_id,archived_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                task_id,
                "runner",
                "topic",
                "finished",
                "done",
                json.dumps({"task_id": task_id, "archived_at": timestamp}),
                timestamp,
                timestamp,
                timestamp,
                "thread",
                timestamp,
            ),
        )
        connection.execute(
            "INSERT INTO task_events(task_id,event,runner,payload_json,created_at) VALUES(?,?,?,?,?)",
            (task_id, "archived", "manager", "{}", timestamp),
        )
        connection.execute(
            "INSERT INTO callback_outbox(task_id,origin_thread_id,state,created_at,updated_at) VALUES(?,?,?,?,?)",
            (task_id, "thread", callback_state, timestamp, timestamp),
        )
        connection.commit()
    finally:
        connection.close()


def test_preview_only_selects_old_archived_tasks_with_terminal_callbacks(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _archived(repo, "TASK_READY")
    _archived(repo, "TASK_CALLBACK_PENDING", callback_state="pending")

    result = task_retention.preview(repo, older_than_days=30)

    assert result["candidate_count"] == 1
    assert result["candidates"][0]["task_id"] == "TASK_READY"
    assert result["protected_callback_count"] == 1


def test_quarantine_restore_roundtrip_preserves_task_events_and_callback(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _archived(repo, "TASK_ROUNDTRIP")
    preview = task_retention.preview(repo, older_than_days=30)

    moved = task_retention.quarantine(
        repo,
        preview_digest=preview["preview_digest"],
        older_than_days=30,
        confirm=True,
    )

    assert moved["quarantined"] == 1
    assert task_store.get_task(repo, "TASK_ROUNDTRIP") is None
    batch = task_retention.list_batches(repo)["batches"][0]
    assert batch["task_count"] == 1
    assert batch["purge_eligible"] is False

    restored = task_retention.restore(repo, batch_id=moved["batch_id"], confirm=True)
    assert restored["restored"] == 1
    assert task_store.get_task(repo, "TASK_ROUNDTRIP")["task_id"] == "TASK_ROUNDTRIP"
    assert task_store.get_task_events(repo, "TASK_ROUNDTRIP")[0]["event"] == "archived"
    assert task_retention.list_batches(repo)["batches"][0]["purge_eligible"] is True
    connection = sqlite3.connect(task_store.canonical_db_path(repo))
    try:
        events = [
            row[0]
            for row in connection.execute(
                "SELECT event FROM task_retention_audit WHERE batch_id=? ORDER BY audit_id",
                (moved["batch_id"],),
            )
        ]
    finally:
        connection.close()
    assert events == ["quarantined", "restored"]


def test_quarantine_is_preview_bound_and_purge_respects_undo_window(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _archived(repo, "TASK_SAFE")
    preview = task_retention.preview(repo, older_than_days=30)

    with pytest.raises(task_retention.TaskRetentionError, match="preview_changed"):
        task_retention.quarantine(
            repo,
            preview_digest="0" * 64,
            older_than_days=30,
            confirm=True,
        )

    moved = task_retention.quarantine(
        repo,
        preview_digest=preview["preview_digest"],
        older_than_days=30,
        confirm=True,
    )
    with pytest.raises(task_retention.TaskRetentionError, match="undo_window_active"):
        task_retention.purge(repo, batch_id=moved["batch_id"], confirm=True)


def test_confirmation_and_age_bounds_fail_closed(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _archived(repo, "TASK_BOUNDS")
    with pytest.raises(task_retention.TaskRetentionError, match="days_out_of_range"):
        task_retention.preview(repo, older_than_days=1)
    preview = task_retention.preview(repo, older_than_days=30)
    with pytest.raises(task_retention.TaskRetentionError, match="confirmation_required"):
        task_retention.quarantine(
            repo,
            preview_digest=preview["preview_digest"],
            older_than_days=30,
        )
