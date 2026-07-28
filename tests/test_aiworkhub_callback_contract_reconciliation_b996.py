from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path


SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from aiworkhub import callback_store  # noqa: E402


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
