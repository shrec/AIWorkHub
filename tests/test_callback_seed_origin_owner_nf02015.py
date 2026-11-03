from __future__ import annotations

import json
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from aiworkhub import callback_store  # noqa: E402

TASK_ID = "ORIGIN_OWNER_NF02015"
LAUNCH_REQUEST_ID = "req-nf02015-launch"


def _db(tmp_path):
    conn = callback_store.open_db(tmp_path / "task_queue.sqlite")
    callback_store.init_db(conn)
    return conn


def _insert_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    origin: str,
    card_extra: dict | None = None,
) -> None:
    now = callback_store.utc_now()
    card = {
        "task_id": task_id,
        "runner": "worker_nf02015",
        "topic": "task_mcp",
        "status": "review",
        "worker_status": "review",
        "callback_required": True,
        "coordinator_provider": "claude",
        "origin_thread_id": origin,
        "claim_epoch": 1,
    }
    card.update(card_extra or {})
    conn.execute(
        """
        INSERT INTO tasks(
          task_id, runner, topic, status, worker_status, priority, objective,
          card_json, created_at, updated_at, origin_thread_id, archived_at
        ) VALUES (?, 'worker_nf02015', 'task_mcp', 'review', 'review', 'normal', '', ?, ?, ?, ?, '')
        """,
        (task_id, json.dumps(card), now, now, origin),
    )
    conn.commit()


def _seed(conn, route: str) -> int:
    return callback_store.seed_missing_review_callbacks(
        conn, provider="claude", origin_thread_id=route
    )


def _rows(conn, origin: str):
    return conn.execute(
        "SELECT origin_thread_id, request_id, state FROM callback_outbox "
        "WHERE task_id=? AND provider='claude' AND origin_thread_id=? ORDER BY outbox_id",
        (TASK_ID, origin),
    ).fetchall()


def _stamp(conn, origin: str, seen_at: datetime) -> None:
    callback_store._ensure_callback_route_seen_table(conn)
    conn.execute(
        "INSERT OR REPLACE INTO callback_route_seen(provider, origin_thread_id, last_seen_at) "
        "VALUES ('claude', ?, ?)",
        (origin, seen_at.isoformat()),
    )
    conn.commit()


def test_live_owner_keeps_review_callback_in_its_own_lane(tmp_path):
    conn = _db(tmp_path)
    try:
        origin_a, origin_b = str(uuid.uuid4()), str(uuid.uuid4())
        _insert_task(conn, TASK_ID, origin=origin_a, card_extra={"launch_request_id": LAUNCH_REQUEST_ID})
        # Seat A waits (records its stamp; its own task's row lands in A).
        _seed(conn, origin_a)
        conn.execute("DELETE FROM callback_outbox")
        conn.commit()

        # Seat B, concurrently live, waits next: it must not steal A's card.
        assert _seed(conn, origin_b) == 1
        assert _rows(conn, origin_b) == []
        rows_a = _rows(conn, origin_a)
        assert len(rows_a) == 1
        assert rows_a[0]["request_id"] == LAUNCH_REQUEST_ID
        # Re-polling B stays idempotent and still never lands in B.
        assert _seed(conn, origin_b) == 0
        assert _rows(conn, origin_b) == []
    finally:
        conn.close()


def test_stale_owner_review_callback_retargets_to_waiting_seat(tmp_path):
    conn = _db(tmp_path)
    try:
        origin_a, origin_b = str(uuid.uuid4()), str(uuid.uuid4())
        _insert_task(conn, TASK_ID, origin=origin_a, card_extra={"launch_request_id": LAUNCH_REQUEST_ID})
        stale = datetime.now(timezone.utc) - callback_store.ROUTE_LIVE_WINDOW * 2
        _stamp(conn, origin_a, stale)

        assert _seed(conn, origin_b) == 1
        assert _rows(conn, origin_a) == []
        rows_b = _rows(conn, origin_b)
        assert len(rows_b) == 1
        assert rows_b[0]["request_id"] == LAUNCH_REQUEST_ID
    finally:
        conn.close()


def test_unseen_owner_review_callback_retargets_to_waiting_seat(tmp_path):
    conn = _db(tmp_path)
    try:
        origin_a, origin_b = str(uuid.uuid4()), str(uuid.uuid4())
        # A launched card (it carries a request id) with no stamp for its
        # owner: the absent-stamp branch of the liveness gate authorizes it.
        _insert_task(conn, TASK_ID, origin=origin_a, card_extra={"launch_request_id": LAUNCH_REQUEST_ID})

        assert _seed(conn, origin_b) == 1
        assert _rows(conn, origin_a) == []
        rows_b = _rows(conn, origin_b)
        assert len(rows_b) == 1
        assert rows_b[0]["request_id"] == LAUNCH_REQUEST_ID
    finally:
        conn.close()


def test_card_without_launch_identity_keeps_legacy_retarget(tmp_path):
    # b985 contract: with no request id there is no seat-specific member to
    # protect, so a second verified route still gets its own copy once.
    conn = _db(tmp_path)
    try:
        origin_a, origin_b = str(uuid.uuid4()), str(uuid.uuid4())
        _insert_task(conn, TASK_ID, origin=origin_a)
        assert _seed(conn, origin_a) == 1
        assert _seed(conn, origin_b) == 1
        assert len(_rows(conn, origin_b)) == 1
        assert _rows(conn, origin_b)[0]["request_id"] == ""
    finally:
        conn.close()


def test_empty_origin_task_still_seeds_to_waiting_seat(tmp_path):
    conn = _db(tmp_path)
    try:
        origin_b = str(uuid.uuid4())
        _insert_task(conn, TASK_ID, origin="")

        assert _seed(conn, origin_b) == 1
        assert len(_rows(conn, origin_b)) == 1
    finally:
        conn.close()


def test_request_id_falls_back_to_terminal_evidence(tmp_path):
    conn = _db(tmp_path)
    try:
        origin_b = str(uuid.uuid4())
        _insert_task(
            conn,
            TASK_ID,
            origin="",
            card_extra={"terminal_review": {"evidence": {"request_id": "req-from-evidence"}}},
        )

        assert _seed(conn, origin_b) == 1
        assert _rows(conn, origin_b)[0]["request_id"] == "req-from-evidence"
    finally:
        conn.close()


def test_waiting_seat_records_its_last_seen_stamp(tmp_path):
    conn = _db(tmp_path)
    try:
        origin_b = str(uuid.uuid4())
        _seed(conn, origin_b)
        row = conn.execute(
            "SELECT last_seen_at FROM callback_route_seen WHERE provider='claude' AND origin_thread_id=?",
            (origin_b,),
        ).fetchone()
        assert row is not None
        assert callback_store._route_origin_seen_recently(conn, "claude", origin_b, {})
    finally:
        conn.close()
