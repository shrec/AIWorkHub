from __future__ import annotations

import json
import sqlite3
import sys
import uuid
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from aiworkhub import callback_store, core, task_store  # noqa: E402


def _insert_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    provider: str = "codex",
    status: str = "review",
    worker_status: str = "review",
    callback_required: bool = True,
    origin: str = "",
    episode: int = 1,
    archived_at: str = "",
) -> None:
    now = callback_store.utc_now()
    card = {
        "task_id": task_id,
        "runner": "worker_b994",
        "topic": "task_mcp",
        "status": status,
        "worker_status": worker_status,
        "callback_required": callback_required,
        "coordinator_provider": provider,
        "origin_thread_id": origin,
        "claim_epoch": episode,
    }
    conn.execute(
        """
        INSERT INTO tasks(
          task_id, runner, topic, status, worker_status, priority, objective,
          card_json, created_at, updated_at, origin_thread_id, archived_at
        ) VALUES (?, 'worker_b994', 'task_mcp', ?, ?, 'normal', '', ?, ?, ?, ?, ?)
        """,
        (
            task_id,
            status,
            worker_status,
            json.dumps(card),
            now,
            now,
            origin,
            archived_at,
        ),
    )
    conn.commit()


def _manager_identity() -> dict[str, str]:
    return {
        "provider": "codex",
        "session_id": "episode_pending",
        "thread_id": "",
        "window_id": "window_b994",
        "callback_supported": "false",
        "route_state": "route_pending",
    }


def test_polling_only_route_pending_creation_persists_empty_origin(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    monkeypatch.setattr(core, "_claude_manager_identity", lambda: None)
    monkeypatch.setattr(core, "_codex_manager_identity", _manager_identity)
    monkeypatch.setattr(core, "_verify_coordinator_capability", lambda _runner: (True, "ok"))

    result = core.create_task(
        task_id="POLLING_ONLY_B994",
        title="Polling only",
        runner="worker_b994",
        topic="task_mcp",
        objective="Persist without a callback route.",
        acceptance=["Persisted."],
        allowed_writes=[],
        callback_required=False,
    )

    assert result["ok"] is True
    card = json.loads(result["stdout"])
    assert card["origin_thread_id"] == ""
    assert card["callback_required"] is False
    assert card["callback_supported"] is False
    assert card["manager_route_state"] == "route_pending"
    assert task_store.get_task(root, "POLLING_ONLY_B994")["origin_thread_id"] == ""


def test_callback_required_route_pending_still_fails_closed(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    monkeypatch.setattr(core, "_claude_manager_identity", lambda: None)
    monkeypatch.setattr(core, "_codex_manager_identity", _manager_identity)
    monkeypatch.setattr(core, "_verify_coordinator_capability", lambda _runner: (True, "ok"))

    result = core.create_task(
        task_id="CALLBACK_REQUIRED_B994",
        title="Callback required",
        runner="worker_b994",
        topic="task_mcp",
        objective="Fail until the callback route is observed.",
        acceptance=["Fails closed."],
        allowed_writes=[],
        callback_required=True,
    )
    assert result["ok"] is False
    assert result["stderr"] == "callback_route_pending:codex_thread_id_not_observed"
    assert task_store.get_task(root, "CALLBACK_REQUIRED_B994") is None


def test_reconciliation_seeds_full_eligible_review_backlog_and_filters_rows(tmp_path):
    db_path = tmp_path / "task_queue.sqlite"
    conn = callback_store.open_db(db_path)
    try:
        callback_store.init_db(conn)
        route = str(uuid.uuid4())
        for index in range(3):
            _insert_task(
                conn,
                f"ELIGIBLE_{index}_B994",
                origin=str(uuid.uuid4()),
                callback_required=index != 0,
                episode=index + 1,
            )
        _insert_task(conn, "WRONG_PROVIDER_B994", provider="claude", origin=route)
        _insert_task(conn, "NON_REVIEW_B994", status="pending", worker_status="unclaimed", origin=route)
        _insert_task(conn, "ARCHIVED_B994", origin=route, archived_at=callback_store.utc_now())

        assert callback_store.seed_missing_review_callbacks(
            conn, provider="codex", origin_thread_id=route
        ) == 3
        rows = conn.execute(
            "SELECT task_id, provider, origin_thread_id FROM callback_outbox ORDER BY task_id"
        ).fetchall()
        assert [row["task_id"] for row in rows] == [
            "ELIGIBLE_0_B994",
            "ELIGIBLE_1_B994",
            "ELIGIBLE_2_B994",
        ]
        assert all(row["provider"] == "codex" and row["origin_thread_id"] == route for row in rows)

        _insert_task(conn, "INVALID_ORIGIN_B994", origin="")
        assert callback_store.seed_missing_review_callbacks(conn, provider="codex") == 0
    finally:
        conn.close()


def test_implicit_origin_does_not_recreate_delivered_episode(tmp_path):
    db_path = tmp_path / "task_queue.sqlite"
    conn = callback_store.open_db(db_path)
    try:
        callback_store.init_db(conn)
        persisted_origin = str(uuid.uuid4())
        old_origin = str(uuid.uuid4())
        _insert_task(conn, "DELIVERED_B994", origin=persisted_origin, episode=7)
        assert callback_store.enqueue_callback(
            conn,
            "DELIVERED_B994",
            old_origin,
            "review_ready",
            provider="codex",
            episode_id="7",
        )
        conn.execute(
            "UPDATE callback_outbox SET state='delivered' WHERE task_id='DELIVERED_B994'"
        )
        conn.commit()

        assert callback_store.seed_missing_review_callbacks(conn, provider="codex") == 0
        rows = conn.execute(
            "SELECT origin_thread_id, state FROM callback_outbox WHERE task_id='DELIVERED_B994'"
        ).fetchall()
        assert [(row["origin_thread_id"], row["state"]) for row in rows] == [
            (old_origin, "delivered")
        ]
    finally:
        conn.close()
