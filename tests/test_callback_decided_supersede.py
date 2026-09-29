"""supersede_decided_task_callbacks: retire undeliverable wakes of decided tasks."""

from __future__ import annotations

import json

from aiworkhub import callback_store


def _make_db(tmp_path):
    conn = callback_store.open_db(tmp_path / "task_queue.sqlite")
    callback_store.init_db(conn)
    return conn


def _task(conn, task_id: str, status: str) -> None:
    now = callback_store.utc_now()
    card = {"task_id": task_id, "status": status}
    conn.execute(
        "INSERT INTO tasks (task_id, runner, topic, status, worker_status, priority, "
        "objective, card_json, created_at, updated_at) VALUES (?, 'r', 't', ?, ?, 'high', "
        "'o', ?, ?, ?)",
        (task_id, status, status, json.dumps(card, sort_keys=True), now, now),
    )
    conn.commit()


def _batch(conn, batch_id: str, state: str = "pending") -> None:
    now = callback_store.utc_now()
    conn.execute(
        "INSERT INTO callback_batches (batch_id, provider, origin_thread_id, state, "
        "created_at, updated_at, member_count) VALUES (?, 'codex', 'thread', ?, ?, ?, 0)",
        (batch_id, state, now, now),
    )
    conn.commit()


def _outbox(conn, task_id: str, state: str, *, batch_id: str = "", episode: str = "0") -> int:
    now = callback_store.utc_now()
    cursor = conn.execute(
        "INSERT INTO callback_outbox (task_id, provider, origin_thread_id, transition, "
        "episode_id, state, created_at, updated_at, batch_id) "
        "VALUES (?, 'codex', 'thread', 'review_ready', ?, ?, ?, ?, ?)",
        (task_id, episode, state, now, now, batch_id),
    )
    conn.commit()
    return int(cursor.lastrowid)


def _state(conn, outbox_id: int):
    return conn.execute(
        "SELECT state, last_error FROM callback_outbox WHERE outbox_id=?", (outbox_id,)
    ).fetchone()


def test_decided_task_pending_and_dead_letter_rows_are_superseded(tmp_path):
    conn = _make_db(tmp_path)
    _task(conn, "T_DONE", "finished")
    _task(conn, "T_LIVE", "review")
    _batch(conn, "B1")
    pending = _outbox(conn, "T_DONE", "pending", batch_id="B1")
    dead = _outbox(conn, "T_DONE", "dead_letter", batch_id="B1", episode="1")
    live = _outbox(conn, "T_LIVE", "pending")

    result = callback_store.supersede_decided_task_callbacks(conn)

    assert result == {"scanned": 2, "superseded": 2, "batches_superseded": 1}
    for outbox_id in (pending, dead):
        row = _state(conn, outbox_id)
        assert row["state"] == "superseded"
        assert row["last_error"] == "task_decided"
    assert _state(conn, live)["state"] == "pending"
    batch = conn.execute("SELECT state FROM callback_batches WHERE batch_id='B1'").fetchone()
    assert batch["state"] == "superseded"
    events = conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id='T_DONE' AND event='callback_superseded'"
    ).fetchone()[0]
    assert events == 2
    conn.close()


def test_archived_superseded_and_missing_tasks_count_as_decided(tmp_path):
    conn = _make_db(tmp_path)
    _task(conn, "T_ARCH", "archived")
    _task(conn, "T_SUP", "superseded")
    rows = [
        _outbox(conn, "T_ARCH", "pending"),
        _outbox(conn, "T_SUP", "dead_letter"),
        _outbox(conn, "T_GONE", "pending"),
    ]

    result = callback_store.supersede_decided_task_callbacks(conn)

    assert result == {"scanned": 3, "superseded": 3, "batches_superseded": 0}
    assert all(_state(conn, r)["state"] == "superseded" for r in rows)
    conn.close()


def test_inflight_and_blocked_task_callbacks_are_untouched(tmp_path):
    conn = _make_db(tmp_path)
    _task(conn, "T_DONE", "finished")
    _task(conn, "T_BLOCKED", "blocked")
    _batch(conn, "B2", state="inflight")
    inflight = _outbox(conn, "T_DONE", "inflight", batch_id="B2")
    blocked_pending = _outbox(conn, "T_BLOCKED", "pending")
    blocked_dead = _outbox(conn, "T_BLOCKED", "dead_letter")

    result = callback_store.supersede_decided_task_callbacks(conn)

    assert result == {"scanned": 0, "superseded": 0, "batches_superseded": 0}
    assert _state(conn, inflight)["state"] == "inflight"
    assert _state(conn, blocked_pending)["state"] == "pending"
    assert _state(conn, blocked_dead)["state"] == "dead_letter"
    batch = conn.execute("SELECT state FROM callback_batches WHERE batch_id='B2'").fetchone()
    assert batch["state"] == "inflight"
    conn.close()


def test_batch_with_a_live_member_stays_pending(tmp_path):
    conn = _make_db(tmp_path)
    _task(conn, "T_DONE", "finished")
    _task(conn, "T_LIVE", "review")
    _batch(conn, "B3")
    _outbox(conn, "T_DONE", "pending", batch_id="B3")
    live = _outbox(conn, "T_LIVE", "pending", batch_id="B3")

    result = callback_store.supersede_decided_task_callbacks(conn)

    assert result == {"scanned": 1, "superseded": 1, "batches_superseded": 0}
    assert _state(conn, live)["state"] == "pending"
    batch = conn.execute("SELECT state FROM callback_batches WHERE batch_id='B3'").fetchone()
    assert batch["state"] == "pending"
    conn.close()
