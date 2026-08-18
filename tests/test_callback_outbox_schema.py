"""NF-2026-00220: validate the callback outbox schema before enqueue.

A structurally malformed callback outbox row (e.g. a blank task id) is
dead-lettered for audited recovery rather than silently enqueued as pending
work a delivery worker can never route.
"""

from __future__ import annotations

import sqlite3

from aiworkhub import callback_store as cs


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    cs.init_db(conn)
    return conn


def _states(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT task_id, state, last_error FROM callback_outbox ORDER BY outbox_id"
    ).fetchall()


def test_blank_task_id_row_is_dead_lettered_not_enqueued() -> None:
    conn = _conn()
    ok = cs.enqueue_callback(
        conn,
        task_id="   ",
        origin_thread_id="thread-1",
        transition="review_ready",
        provider="codex",
    )
    assert ok is False
    rows = _states(conn)
    assert len(rows) == 1
    assert rows[0]["state"] == "dead_letter"
    assert "task_id_blank" in rows[0]["last_error"]
    # It was NOT written as pending work.
    pending = conn.execute(
        "SELECT COUNT(*) FROM callback_outbox WHERE state='pending'"
    ).fetchone()[0]
    assert pending == 0


def test_control_character_task_id_is_dead_lettered() -> None:
    conn = _conn()
    ok = cs.enqueue_callback(
        conn,
        task_id="T-\x00-1",
        origin_thread_id="thread-1",
        transition="review_ready",
        provider="codex",
    )
    assert ok is False
    rows = _states(conn)
    assert len(rows) == 1
    assert rows[0]["state"] == "dead_letter"
    assert "control_character" in rows[0]["last_error"]


def test_wellformed_row_still_enqueues_as_pending() -> None:
    conn = _conn()
    ok = cs.enqueue_callback(
        conn,
        task_id="T-1",
        origin_thread_id="thread-1",
        transition="review_ready",
        provider="codex",
    )
    assert ok is True
    rows = _states(conn)
    assert len(rows) == 1
    assert rows[0]["state"] == "pending"
    assert rows[0]["last_error"] == ""


class _FailingDeadLetterConn:
    """Wraps a real connection but makes the dead-letter INSERT fail AFTER
    sqlite has opened the implicit write transaction, exactly as a real
    constraint/operational failure mid-statement would leave the shared
    connection: still holding a transaction the next writer would inherit."""

    def __init__(self, real: sqlite3.Connection) -> None:
        self._real = real

    def execute(self, sql: str, *args, **kwargs):
        if "'dead_letter'" in sql:
            # Open a real write transaction on the shared connection, then
            # fail -- the connection is now mid-transaction.
            self._real.execute("INSERT INTO _txnprobe VALUES (1)")
            raise sqlite3.OperationalError("simulated dead-letter insert failure")
        return self._real.execute(sql, *args, **kwargs)

    def __getattr__(self, name: str):
        return getattr(self._real, name)


def test_failed_dead_letter_insert_leaves_no_open_transaction() -> None:
    real = _conn()
    real.execute("CREATE TABLE _txnprobe(x)")
    real.commit()
    conn = _FailingDeadLetterConn(real)

    import pytest

    # A blank task id routes into the dead-letter path; the insert fails.
    with pytest.raises(sqlite3.Error):
        cs.enqueue_callback(
            conn,
            task_id="   ",
            origin_thread_id="thread-1",
            transition="review_ready",
            provider="codex",
        )
    # The dead-letter path owns the transaction it started; on failure it
    # must roll back so the caller's shared connection is not left holding an
    # aborted or open write transaction the next writer would inherit.
    assert real.in_transaction is False
