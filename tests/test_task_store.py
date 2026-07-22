from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

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
