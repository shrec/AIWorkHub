from __future__ import annotations

import json
import sqlite3
import sys
import uuid
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from aiworkhub import callback_store  # noqa: E402

TASK_ID = "CROSS_ORIGIN_NF01548"


def _insert_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    status: str = "review",
    worker_status: str = "review",
    terminal_substatus: str = "",
    episode: int = 1,
) -> None:
    now = callback_store.utc_now()
    card = {
        "task_id": task_id,
        "runner": "worker_nf01548",
        "topic": "task_mcp",
        "status": status,
        "worker_status": worker_status,
        "callback_required": True,
        "coordinator_provider": "claude",
        "origin_thread_id": "",
        "claim_epoch": episode,
    }
    if terminal_substatus:
        card["terminal_substatus"] = terminal_substatus
    conn.execute(
        """
        INSERT INTO tasks(
          task_id, runner, topic, status, worker_status, priority, objective,
          card_json, created_at, updated_at, origin_thread_id, archived_at
        ) VALUES (?, 'worker_nf01548', 'task_mcp', ?, ?, 'normal', '', ?, ?, ?, '', '')
        """,
        (task_id, status, worker_status, json.dumps(card), now, now),
    )
    conn.commit()


def _retarget_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    status: str,
    terminal_substatus: str = "",
    episode: int,
) -> None:
    card = json.loads(
        conn.execute("SELECT card_json FROM tasks WHERE task_id=?", (task_id,)).fetchone()[0]
    )
    card["status"] = status
    card["worker_status"] = status
    card["claim_epoch"] = episode
    if terminal_substatus:
        card["terminal_substatus"] = terminal_substatus
    conn.execute(
        "UPDATE tasks SET status=?, worker_status=?, card_json=? WHERE task_id=?",
        (status, status, json.dumps(card), task_id),
    )
    conn.commit()


def _enqueue(conn, origin: str, transition: str, episode: str, *, delivered: bool) -> None:
    assert callback_store.enqueue_callback(
        conn, TASK_ID, origin, transition, provider="claude", episode_id=episode
    )
    if delivered:
        conn.execute(
            "UPDATE callback_outbox SET state='delivered' WHERE task_id=? AND origin_thread_id=?",
            (TASK_ID, origin),
        )
        conn.commit()


def _rows(conn, origin: str | None = None):
    sql = "SELECT origin_thread_id, transition, episode_id, state FROM callback_outbox WHERE task_id=?"
    params: tuple = (TASK_ID,)
    if origin is not None:
        sql += " AND origin_thread_id=?"
        params += (origin,)
    return conn.execute(sql + " ORDER BY outbox_id", params).fetchall()


def _seed(conn, route: str) -> int:
    return callback_store.seed_missing_review_callbacks(
        conn, provider="claude", origin_thread_id=route
    )


def _db(tmp_path):
    conn = callback_store.open_db(tmp_path / "task_queue.sqlite")
    callback_store.init_db(conn)
    return conn


def test_delivered_episode_is_not_seeded_to_new_route_origin(tmp_path):
    conn = _db(tmp_path)
    try:
        origin_a, origin_b = str(uuid.uuid4()), str(uuid.uuid4())
        _insert_task(conn, TASK_ID, episode=2)
        _enqueue(conn, origin_a, "review_ready", "2", delivered=True)

        assert _seed(conn, origin_b) == 0
        assert _rows(conn, origin_b) == []
        assert [(r["origin_thread_id"], r["state"]) for r in _rows(conn)] == [(origin_a, "delivered")]
    finally:
        conn.close()


def test_undelivered_wake_still_retargets_to_new_route_origin_once(tmp_path):
    conn = _db(tmp_path)
    try:
        origin_a, origin_b = str(uuid.uuid4()), str(uuid.uuid4())
        _insert_task(conn, TASK_ID, episode=1)
        _enqueue(conn, origin_a, "review_ready", "1", delivered=False)

        assert _seed(conn, origin_b) == 1
        assert len(_rows(conn, origin_b)) == 1
    finally:
        conn.close()


def test_new_episode_is_seeded_after_earlier_episode_was_delivered(tmp_path):
    conn = _db(tmp_path)
    try:
        origin_a, origin_b = str(uuid.uuid4()), str(uuid.uuid4())
        _insert_task(conn, TASK_ID, episode=1)
        _enqueue(conn, origin_a, "review_ready", "1", delivered=True)
        _retarget_task(conn, TASK_ID, status="review", episode=2)

        assert _seed(conn, origin_b) == 1
        rows = _rows(conn, origin_b)
        assert [(r["transition"], r["episode_id"]) for r in rows] == [("review_ready", "2")]
    finally:
        conn.close()


def test_new_transition_is_seeded_after_other_transition_was_delivered(tmp_path):
    conn = _db(tmp_path)
    try:
        origin_a, origin_b = str(uuid.uuid4()), str(uuid.uuid4())
        _insert_task(conn, TASK_ID, episode=1)
        _enqueue(conn, origin_a, "review_ready", "1", delivered=True)
        _retarget_task(conn, TASK_ID, status="blocked", terminal_substatus="worker_failed", episode=1)

        assert _seed(conn, origin_b) == 1
        rows = _rows(conn, origin_b)
        assert [(r["transition"], r["episode_id"]) for r in rows] == [("worker_failed", "1")]
    finally:
        conn.close()


def test_seed_with_new_route_origin_is_idempotent(tmp_path):
    conn = _db(tmp_path)
    try:
        origin_a, origin_b = str(uuid.uuid4()), str(uuid.uuid4())
        _insert_task(conn, TASK_ID, episode=1)
        _enqueue(conn, origin_a, "review_ready", "1", delivered=False)

        assert _seed(conn, origin_b) == 1
        assert _seed(conn, origin_b) == 0
        assert len(_rows(conn, origin_b)) == 1
    finally:
        conn.close()
