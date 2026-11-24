"""NF-2026-02406: a wake for a card that moved on, or one already delivered, is never delivered.

Measured in a manager loop session: a ``worker_failed`` wake was delivered for a
card archived 12 minutes earlier, and one ``review_ready`` wake was delivered
twice, 9 minutes apart, after its card was accepted.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Any, Callable

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import manager_loop_wake  # noqa: E402
from aiworkhub.manager_loop_wake import WakeConsumer  # noqa: E402

from test_manager_loop_wake import (  # noqa: E402
    _insert_review_card,
    _outbox_rows,
    _real_task_db,
)

_SESSION = "mls-" + "ab" * 16
_EXPIRED = "2000-01-01T00:00:00+00:00"


def _card(transition: str) -> dict[str, Any]:
    return {
        "coordinator_provider": "claude",
        "manager_chat_session_id": _SESSION,
        "terminal_substatus": transition,
    }


def _update_task(connect: Callable[[], sqlite3.Connection], task_id: str, **columns: str) -> None:
    conn = connect()
    try:
        assignments = ", ".join(f"{name}=?" for name in columns)
        conn.execute(
            f"UPDATE tasks SET {assignments} WHERE task_id=?", (*columns.values(), task_id)
        )
    finally:
        conn.close()


def _expire_leases(connect: Callable[[], sqlite3.Connection]) -> None:
    conn = connect()
    try:
        conn.execute("UPDATE callback_batches SET lease_expires_at=? WHERE state='inflight'", (_EXPIRED,))
    finally:
        conn.close()


class _Dispatcher:
    """A dispatch that records each member and reports the turn delivered at once."""

    def __init__(self) -> None:
        self.dispatched: list[str] = []

    def __call__(self, member: Any, done: Callable[..., None]) -> bool:
        self.dispatched.append(str(member["task_id"]))
        done(True)
        return True


@pytest.mark.parametrize(
    ("transition", "task_status", "moved_on", "expected_status"),
    [
        ("review_ready", "review", {"status": "finished"}, "finished"),
        ("review_ready", "review", {"archived_at": "2026-11-24T06:30:00Z"}, "archived"),
        ("worker_failed", "blocked", {"archived_at": "2026-11-24T06:30:00Z"}, "archived"),
        ("worker_failed", "blocked", {"status": "finished"}, "finished"),
    ],
)
def test_wake_for_a_card_that_moved_on_while_queued_is_acked_stale_not_delivered(
    monkeypatch: Any,
    tmp_path: Path,
    transition: str,
    task_status: str,
    moved_on: dict[str, str],
    expected_status: str,
) -> None:
    connect = _real_task_db(monkeypatch, tmp_path)
    conn = connect()
    try:
        _insert_review_card(conn, "CARD_S", _card(transition))
        conn.execute("UPDATE tasks SET status=? WHERE task_id='CARD_S'", (task_status,))
    finally:
        conn.close()

    claim, ack = manager_loop_wake.default_callback_source(session_id=_SESSION, holds_seat=False)
    batch = claim()
    assert batch is not None
    assert [member["state"] for member in batch["members"]] == [transition]

    # The wake sits in the consumer's queue while the card is accepted/archived.
    _update_task(connect, "CARD_S", **moved_on)
    dispatcher = _Dispatcher()
    consumer = WakeConsumer(claim=claim, ack=ack, dispatch=dispatcher)
    consumer._enqueue_batch(batch)
    consumer._drain_queue()

    assert dispatcher.dispatched == []
    assert _outbox_rows(connect, "CARD_S") == [
        (
            "manager_chat",
            _SESSION,
            "superseded",
            f"task_no_longer_in_matching_terminal_state:{expected_status}",
        )
    ]
    assert consumer.status()["queued"] == 0
    assert consumer.status()["turns_this_hour"] == 0
    assert claim() is None


def test_wake_for_a_card_still_owed_is_delivered_after_the_stale_check(
    monkeypatch: Any, tmp_path: Path
) -> None:
    connect = _real_task_db(monkeypatch, tmp_path)
    conn = connect()
    try:
        _insert_review_card(conn, "CARD_L", _card("review_ready"))
    finally:
        conn.close()

    claim, ack = manager_loop_wake.default_callback_source(session_id=_SESSION, holds_seat=False)
    batch = claim()
    assert batch is not None
    dispatcher = _Dispatcher()
    consumer = WakeConsumer(claim=claim, ack=ack, dispatch=dispatcher)
    consumer._enqueue_batch(batch)
    consumer._drain_queue()

    assert dispatcher.dispatched == ["CARD_L"]
    assert _outbox_rows(connect, "CARD_L") == [("manager_chat", _SESSION, "delivered", "")]


def test_delivered_wake_is_not_delivered_again_when_its_ack_was_lost(
    monkeypatch: Any, tmp_path: Path
) -> None:
    connect = _real_task_db(monkeypatch, tmp_path)
    conn = connect()
    try:
        _insert_review_card(conn, "CARD_R", _card("review_ready"))
    finally:
        conn.close()

    claim, real_ack = manager_loop_wake.default_callback_source(session_id=_SESSION, holds_seat=False)
    acks: list[str] = []

    def lossy_ack(batch_id: str, lease_id: str) -> bool:
        # The first ack never reaches the store (a crash/timeout after the turn).
        acks.append(batch_id)
        return False if len(acks) == 1 else real_ack(batch_id, lease_id)

    dispatcher = _Dispatcher()
    consumer = WakeConsumer(claim=claim, ack=lossy_ack, dispatch=dispatcher)
    first = claim()
    assert first is not None
    consumer._enqueue_batch(first)
    consumer._drain_queue()
    assert dispatcher.dispatched == ["CARD_R"]
    assert _outbox_rows(connect, "CARD_R") == [("manager_chat", _SESSION, "inflight", "")]

    # The lease lapses without a durable ack, so the store hands the batch out again.
    _expire_leases(connect)
    again = claim()
    assert again is not None
    assert again["batch_id"] == first["batch_id"]
    consumer._enqueue_batch(again)
    consumer._drain_queue()

    assert dispatcher.dispatched == ["CARD_R"]
    assert len(acks) == 2
    assert _outbox_rows(connect, "CARD_R") == [("manager_chat", _SESSION, "delivered", "")]
    assert claim() is None


def test_acked_wake_is_not_claimed_again_even_after_the_lease_window(
    monkeypatch: Any, tmp_path: Path
) -> None:
    connect = _real_task_db(monkeypatch, tmp_path)
    conn = connect()
    try:
        _insert_review_card(conn, "CARD_A", _card("review_ready"))
    finally:
        conn.close()

    claim, ack = manager_loop_wake.default_callback_source(session_id=_SESSION, holds_seat=False)
    batch = claim()
    assert batch is not None
    assert ack(batch["batch_id"], batch["lease_id"]) is True

    _expire_leases(connect)
    assert claim() is None
    assert claim() is None
    assert _outbox_rows(connect, "CARD_A") == [("manager_chat", _SESSION, "delivered", "")]


def test_wake_whose_lease_expired_without_delivery_is_claimed_again(
    monkeypatch: Any, tmp_path: Path
) -> None:
    connect = _real_task_db(monkeypatch, tmp_path)
    conn = connect()
    try:
        _insert_review_card(conn, "CARD_E", _card("review_ready"))
    finally:
        conn.close()

    claim, ack = manager_loop_wake.default_callback_source(session_id=_SESSION, holds_seat=False)
    first = claim()
    assert first is not None
    _expire_leases(connect)
    second = claim()

    assert second is not None
    assert second["batch_id"] == first["batch_id"]
    assert second["lease_id"] != first["lease_id"]
    assert [member["task_id"] for member in second["members"]] == ["CARD_E"]
    assert ack(second["batch_id"], second["lease_id"]) is True


@pytest.mark.parametrize(
    ("settle", "reason"),
    [
        ("UPDATE callback_outbox SET state='delivered' WHERE task_id='CARD_D'", "outbox_already_delivered"),
        ("UPDATE callback_outbox SET state='superseded' WHERE task_id='CARD_D'", "outbox_already_superseded"),
        ("DELETE FROM callback_outbox WHERE task_id='CARD_D'", "outbox_row_missing"),
    ],
)
def test_wake_settled_elsewhere_while_queued_is_not_delivered(
    monkeypatch: Any, tmp_path: Path, settle: str, reason: str
) -> None:
    connect = _real_task_db(monkeypatch, tmp_path)
    conn = connect()
    try:
        _insert_review_card(conn, "CARD_D", _card("review_ready"))
    finally:
        conn.close()

    claim, ack = manager_loop_wake.default_callback_source(session_id=_SESSION, holds_seat=False)
    batch = claim()
    assert batch is not None
    # Another lease acked it, a claim superseded it, or retention purged it
    # while this consumer still held the wake queued.
    conn = connect()
    try:
        conn.execute(settle)
    finally:
        conn.close()
    dispatcher = _Dispatcher()
    consumer = WakeConsumer(claim=claim, ack=ack, dispatch=dispatcher)
    consumer._enqueue_batch(batch)
    consumer._drain_queue()

    assert dispatcher.dispatched == []
    assert consumer.status()["turns_this_hour"] == 0
    assert claim.settle_stale(batch["members"][0]) == reason  # type: ignore[attr-defined]
