"""Standalone test adapter for the package-local callback store.

The public AIWorkHub checkout intentionally has no parent ``AITools/taskdb.py``.
Callback tests use this small fixture writer while exercising the real
package-local outbox, batching, leasing, recovery, and delivery primitives.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from aiworkhub import callback_store as _store
from aiworkhub.callback_store import *  # noqa: F401,F403


def upsert_card(
    conn: sqlite3.Connection,
    card: dict[str, Any],
    *,
    preserve_lifecycle: bool = True,
) -> None:
    """Seed one canonical task row and its terminal callback for tests."""
    _store.init_db(conn)
    task_id = str(card["task_id"])
    existing = conn.execute(
        "SELECT status, worker_status, claimed_by, claimed_at, started_at, completed_at "
        "FROM tasks WHERE task_id=?",
        (task_id,),
    ).fetchone()
    now = _store.utc_now()
    status = str(card.get("status") or "pending")
    worker_status = str(card.get("worker_status") or "unclaimed")
    lifecycle = {
        "claimed_by": None,
        "claimed_at": None,
        "started_at": None,
        "completed_at": None,
    }
    if preserve_lifecycle and existing is not None:
        lifecycle.update({key: existing[key] for key in lifecycle})
    conn.execute(
        """
        INSERT INTO tasks(
          task_id, runner, topic, mode, status, worker_status, priority,
          objective, card_json, created_at, updated_at, claimed_by,
          claimed_at, started_at, completed_at, origin_thread_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(task_id) DO UPDATE SET
          runner=excluded.runner,
          topic=excluded.topic,
          mode=excluded.mode,
          status=excluded.status,
          worker_status=excluded.worker_status,
          priority=excluded.priority,
          objective=excluded.objective,
          card_json=excluded.card_json,
          updated_at=excluded.updated_at,
          claimed_by=excluded.claimed_by,
          claimed_at=excluded.claimed_at,
          started_at=excluded.started_at,
          completed_at=excluded.completed_at,
          origin_thread_id=excluded.origin_thread_id
        """,
        (
            task_id,
            str(card.get("runner") or ""),
            str(card.get("topic") or ""),
            str(card.get("mode") or ""),
            status,
            worker_status,
            str(card.get("priority") or ""),
            str(card.get("objective") or ""),
            json.dumps(card, ensure_ascii=False, sort_keys=True),
            now,
            now,
            lifecycle["claimed_by"],
            lifecycle["claimed_at"],
            lifecycle["started_at"],
            lifecycle["completed_at"],
            str(card.get("origin_thread_id") or ""),
        ),
    )
    conn.commit()
    _store.append_event(conn, task_id, "card_upserted", str(card.get("runner") or ""), {})
    origin_thread_id = str(card.get("origin_thread_id") or "")
    if origin_thread_id and (
        status in {"review", "ready_for_review", "codex_review", "awaiting_review"}
        or worker_status in {"review", "ready_for_review", "codex_review", "awaiting_review"}
    ):
        _store.enqueue_callback(conn, task_id, origin_thread_id, "review_ready")
