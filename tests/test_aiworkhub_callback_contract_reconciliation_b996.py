from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest


SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from aiworkhub import callback_store  # noqa: E402


def test_enqueue_callback_rolls_back_owned_transaction_on_commit_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Cursor:
        rowcount = 1

    class _Connection:
        def __init__(self) -> None:
            self.rollbacks = 0

        def execute(self, *_args, **_kwargs):
            return _Cursor()

        def commit(self) -> None:
            raise sqlite3.OperationalError("disk busy")

        def rollback(self) -> None:
            self.rollbacks += 1

    connection = _Connection()
    monkeypatch.setattr(callback_store, "_ensure_callback_outbox_table", lambda _conn: None)
    monkeypatch.setattr(callback_store, "current_claim_episode", lambda _conn, _task: "1")

    with pytest.raises(sqlite3.OperationalError, match="disk busy"):
        callback_store.enqueue_callback(
            connection,  # type: ignore[arg-type]
            "TASK_CALLBACK_ROLLBACK",
            "thread-1",
            "review_ready",
            provider="codex",
        )
    assert connection.rollbacks == 1


def _review_row(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    terminal_substatus: str,
    origin: str = "thread-b996",
) -> None:
    now = callback_store.utc_now()
    card = {
        "task_id": task_id,
        "runner": "codex_b996",
        "topic": "task_mcp",
        "status": "review",
        "worker_status": "future_worker_substatus",
        "terminal_substatus": terminal_substatus,
        "callback_required": False,
        "coordinator_provider": "codex",
        "origin_thread_id": origin,
        "claim_epoch": 4,
    }
    conn.execute(
        """
        INSERT INTO tasks(
          task_id, runner, topic, status, worker_status, priority, objective,
          card_json, created_at, updated_at, origin_thread_id, archived_at
        ) VALUES (?, 'codex_b996', 'task_mcp', 'review', 'future_worker_substatus',
                  'normal', '', ?, ?, ?, ?, '')
        """,
        (task_id, json.dumps(card), now, now, origin),
    )
    conn.commit()


def test_reconciliation_unknown_review_substatus_falls_back_without_weakening_direct_enqueue(
    tmp_path: Path,
) -> None:
    conn = callback_store.open_db(tmp_path / "task_queue.sqlite")
    try:
        callback_store.init_db(conn)
        _review_row(
            conn,
            "UNKNOWN_RECONCILIATION_B996",
            terminal_substatus="future_reconciliation_only_state",
        )

        assert callback_store.seed_missing_review_callbacks(
            conn, provider="codex"
        ) == 1
        row = conn.execute(
            "SELECT transition, state, provider, origin_thread_id, episode_id "
            "FROM callback_outbox WHERE task_id=?",
            ("UNKNOWN_RECONCILIATION_B996",),
        ).fetchone()
        assert tuple(row) == (
            "review_ready",
            "pending",
            "codex",
            "thread-b996",
            "4",
        )
        assert callback_store.seed_missing_review_callbacks(
            conn, provider="codex"
        ) == 0
        assert callback_store.enqueue_callback(
            conn,
            "UNKNOWN_RECONCILIATION_B996",
            "thread-b996",
            "future_reconciliation_only_state",
            provider="codex",
            episode_id="5",
        ) is False
    finally:
        conn.close()


def test_enqueue_copies_to_manager_chat_without_removing_the_codex_row(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    db_path = root / ".aiworkhub" / "tasking" / "task_queue.sqlite"
    session_id = "mls-" + "ab" * 16
    session_dir = root / ".aiworkhub" / "runtime" / "manager_loop" / "sessions"
    session_dir.mkdir(parents=True)
    (session_dir.parent / "selected.json").write_text(
        json.dumps({"session_id": session_id}), encoding="utf-8"
    )
    (session_dir / f"{session_id}.json").write_text(
        json.dumps({"session_id": session_id, "status": "active"}), encoding="utf-8"
    )
    conn = callback_store.open_db(db_path)
    try:
        callback_store.init_db(conn)
        now = callback_store.utc_now()
        card = {
            "task_id": "TASK_DUAL_CALLBACK",
            "origin_thread_id": "11111111-1111-4111-8111-111111111111",
            "manager_chat_session_id": session_id,
            "coordinator_provider": "codex",
        }
        conn.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, card_json, created_at, updated_at, origin_thread_id) "
            "VALUES (?, 'codex', 'task_mcp', 'review', 'review', ?, ?, ?, ?)",
            ("TASK_DUAL_CALLBACK", json.dumps(card), now, now, card["origin_thread_id"]),
        )
        conn.commit()

        assert callback_store.enqueue_callback(
            conn,
            "TASK_DUAL_CALLBACK",
            card["origin_thread_id"],
            "review_ready",
            provider="codex",
            episode_id="1",
        ) is True
        rows = conn.execute(
            "SELECT provider, origin_thread_id, state FROM callback_outbox "
            "WHERE task_id=? ORDER BY provider",
            ("TASK_DUAL_CALLBACK",),
        ).fetchall()
        assert [(row["provider"], row["origin_thread_id"], row["state"]) for row in rows] == [
            ("codex", card["origin_thread_id"], "pending"),
            ("manager_chat", session_id, "pending"),
        ]

        chat_only = "mls-" + "ef" * 16
        assert callback_store.enqueue_callback(
            conn,
            "TASK_DUAL_CALLBACK",
            chat_only,
            "blocked",
            provider="codex",
            episode_id="2",
        ) is True
        retagged = conn.execute(
            "SELECT provider, origin_thread_id FROM callback_outbox "
            "WHERE task_id=? AND episode_id='2'",
            ("TASK_DUAL_CALLBACK",),
        ).fetchone()
        assert retagged["provider"] == "manager_chat"
        assert retagged["origin_thread_id"] == chat_only
        still = conn.execute(
            "SELECT state FROM callback_outbox WHERE provider='codex' AND task_id=?",
            ("TASK_DUAL_CALLBACK",),
        ).fetchone()
        assert still["state"] == "pending"
    finally:
        conn.close()
