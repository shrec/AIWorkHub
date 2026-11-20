from __future__ import annotations

import json
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import callback_store  # noqa: E402
from aiworkhub import core as aiworkhub_core  # noqa: E402
from aiworkhub import manager_loop  # noqa: E402
from aiworkhub import manager_loop_service  # noqa: E402
from aiworkhub import manager_loop_wake  # noqa: E402
from aiworkhub.manager_loop_wake import WakeConsumer  # noqa: E402

from test_manager_loop_service import _install_fakes, _seat_record  # noqa: E402


class _FakeClock:
    """A manually-advanced clock so the hourly cap never depends on real time."""

    def __init__(self, start: float = 0.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value


def _noop_claim() -> dict[str, Any] | None:
    return None


def _noop_ack(batch_id: str, lease_id: str) -> bool:
    return True


def _install_fake_wake_source(monkeypatch: Any, claim: Any, ack: Any) -> None:
    monkeypatch.setattr(manager_loop_service, "default_callback_source", lambda **_kwargs: (claim, ack))


# ---------------------------------------------------------------------------
# WakeConsumer unit tests: a fake claim/ack pair stands in for the lease API,
# a fake dispatch stands in for manager_loop_service's own turn_lock dispatch.
# No sleeps anywhere below: every wait is on a threading.Event or a join.
# ---------------------------------------------------------------------------


def test_a_callback_arriving_while_idle_starts_a_turn_and_is_acked_after_it_starts() -> None:
    delivered = {"done": False}
    dispatch_calls: list[dict[str, Any]] = []
    ack_calls: list[tuple[str, str]] = []
    acked = threading.Event()

    def claim() -> dict[str, Any] | None:
        if delivered["done"]:
            return None
        delivered["done"] = True
        return {
            "batch_id": "b1",
            "lease_id": "l1",
            "members": [{"task_id": "CARD_A", "state": "review"}],
        }

    def dispatch(member: dict[str, Any], done: Any) -> bool:
        dispatch_calls.append(member)
        done(True)
        return True

    def ack(batch_id: str, lease_id: str) -> bool:
        ack_calls.append((batch_id, lease_id))
        acked.set()
        return True

    consumer = WakeConsumer(
        claim=claim, ack=ack, dispatch=dispatch, idle_poll_seconds=0.02, retry_poll_seconds=0.02
    )
    consumer.start()
    try:
        assert acked.wait(timeout=5)
    finally:
        consumer.stop()

    assert dispatch_calls == [{"task_id": "CARD_A", "state": "review"}]
    assert ack_calls == [("b1", "l1")]


def test_a_callback_during_a_running_turn_is_queued_then_runs_after_it_ends() -> None:
    delivered = {"done": False}
    turn_busy = {"value": True}
    dispatch_calls: list[dict[str, Any]] = []
    tried_while_busy = threading.Event()
    acked = threading.Event()

    def claim() -> dict[str, Any] | None:
        if delivered["done"]:
            return None
        delivered["done"] = True
        return {
            "batch_id": "b1",
            "lease_id": "l1",
            "members": [{"task_id": "CARD_A", "state": "blocked"}],
        }

    def dispatch(member: dict[str, Any], done: Any) -> bool:
        dispatch_calls.append(member)
        if turn_busy["value"]:
            tried_while_busy.set()
            return False
        done(True)
        return True

    def ack(batch_id: str, lease_id: str) -> bool:
        acked.set()
        return True

    consumer = WakeConsumer(
        claim=claim, ack=ack, dispatch=dispatch, idle_poll_seconds=0.02, retry_poll_seconds=0.02
    )
    consumer.start()
    try:
        assert tried_while_busy.wait(timeout=5)
        # Still queued, never dropped, and not acked while the turn stays busy.
        assert consumer.status()["queued"] == 1
        assert not acked.is_set()

        turn_busy["value"] = False
        assert acked.wait(timeout=5)
    finally:
        consumer.stop()

    assert len(dispatch_calls) >= 2
    assert consumer.status()["queued"] == 0


def test_the_hourly_cap_holds_members_back_and_status_reports_them() -> None:
    clock = _FakeClock(0.0)
    delivered = {"done": False}
    started: list[str] = []
    two_started = threading.Event()
    third_started = threading.Event()
    ack_calls: list[tuple[str, str]] = []

    def claim() -> dict[str, Any] | None:
        if delivered["done"]:
            return None
        delivered["done"] = True
        return {
            "batch_id": "b1",
            "lease_id": "l1",
            "members": [
                {"task_id": "A", "state": "s"},
                {"task_id": "B", "state": "s"},
                {"task_id": "C", "state": "s"},
            ],
        }

    def dispatch(member: dict[str, Any], done: Any) -> bool:
        started.append(member["task_id"])
        done(True)
        if len(started) == 2:
            two_started.set()
        if len(started) == 3:
            third_started.set()
        return True

    def ack(batch_id: str, lease_id: str) -> bool:
        ack_calls.append((batch_id, lease_id))
        return True

    consumer = WakeConsumer(
        claim=claim,
        ack=ack,
        dispatch=dispatch,
        cap_per_hour=2,
        clock=clock,
        idle_poll_seconds=0.02,
        retry_poll_seconds=0.02,
    )
    consumer.start()
    try:
        assert two_started.wait(timeout=5)
        assert not third_started.wait(timeout=0.3)  # the cap holds the third member back
        status = consumer.status()
        assert status["turns_this_hour"] == 2
        assert status["queued"] == 1
        assert status["cap"] == 2
        assert ack_calls == []  # the batch cannot be acked until every member has started

        clock.value += 3601.0  # the hour rolls over
        assert third_started.wait(timeout=5)
        assert ack_calls == [("b1", "l1")]
    finally:
        consumer.stop()


def test_a_crash_before_every_member_starts_leaves_the_batch_unacked() -> None:
    delivered = {"done": False}
    dispatch_calls: list[str] = []
    a_started = threading.Event()
    ack_calls: list[tuple[str, str]] = []

    def claim() -> dict[str, Any] | None:
        if delivered["done"]:
            return None
        delivered["done"] = True
        return {
            "batch_id": "b1",
            "lease_id": "l1",
            "members": [{"task_id": "A", "state": "s"}, {"task_id": "B", "state": "s"}],
        }

    def dispatch(member: dict[str, Any], done: Any) -> bool:
        dispatch_calls.append(member["task_id"])
        if member["task_id"] == "A":
            done(True)
            a_started.set()
            return True
        return False  # B never starts: simulates a turn slot that stays busy

    def ack(batch_id: str, lease_id: str) -> bool:
        ack_calls.append((batch_id, lease_id))
        return True

    consumer = WakeConsumer(
        claim=claim, ack=ack, dispatch=dispatch, idle_poll_seconds=0.02, retry_poll_seconds=0.02
    )
    consumer.start()
    try:
        assert a_started.wait(timeout=5)
        assert consumer.status()["queued"] == 1
    finally:
        consumer.stop()  # the crash: the process goes away before B ever starts

    assert ack_calls == []  # an unstarted member keeps its whole batch un-acked


def test_a_batch_redelivered_after_its_lease_lapses_is_not_enqueued_twice() -> None:
    dispatch_calls: list[str] = []
    turn_busy = {"value": True}
    a_started = threading.Event()
    b_tried_while_busy = threading.Event()
    settled_after_redelivery = threading.Event()
    acked = threading.Event()
    ack_calls: list[tuple[str, str]] = []
    claim_calls = {"n": 0}
    claims_after_redelivery = {"n": 0}

    def claim() -> dict[str, Any] | None:
        claim_calls["n"] += 1
        if claim_calls["n"] == 1:
            return {
                "batch_id": "b1",
                "lease_id": "l1",
                "members": [{"task_id": "A", "state": "s"}, {"task_id": "B", "state": "s"}],
            }
        if b_tried_while_busy.is_set():
            claims_after_redelivery["n"] += 1
            if claims_after_redelivery["n"] == 1:
                # The lease lapsed while B was still stuck in the in-memory
                # queue: the store re-delivers the SAME batch_id, unchanged
                # membership, under a renewed lease.
                return {
                    "batch_id": "b1",
                    "lease_id": "l2",
                    "members": [{"task_id": "A", "state": "s"}, {"task_id": "B", "state": "s"}],
                }
            if claims_after_redelivery["n"] >= 6:
                settled_after_redelivery.set()
        return None

    def dispatch(member: dict[str, Any], done: Any) -> bool:
        task_id = member["task_id"]
        dispatch_calls.append(task_id)
        if task_id == "A":
            done(True)
            a_started.set()
            return True
        if turn_busy["value"]:
            b_tried_while_busy.set()
            return False
        done(True)
        return True

    def ack(batch_id: str, lease_id: str) -> bool:
        ack_calls.append((batch_id, lease_id))
        acked.set()
        return True

    consumer = WakeConsumer(
        claim=claim, ack=ack, dispatch=dispatch, idle_poll_seconds=0.02, retry_poll_seconds=0.02
    )
    consumer.start()
    try:
        assert a_started.wait(timeout=5)
        assert b_tried_while_busy.wait(timeout=5)
        assert settled_after_redelivery.wait(timeout=5)
        # The redelivery above must not have re-enqueued A or a second B.
        assert dispatch_calls.count("A") == 1
        assert consumer.status()["queued"] == 1

        turn_busy["value"] = False
        assert acked.wait(timeout=5)
    finally:
        consumer.stop()

    assert dispatch_calls.count("A") == 1
    assert ack_calls == [("b1", "l2")]  # acked with the RENEWED lease, not the stale one


# ---------------------------------------------------------------------------
# manager_loop_service integration: a real ManagerOrchestrator/FakeManagerBackend
# and a fake callback source injected in place of the lease API.
# ---------------------------------------------------------------------------


def test_no_session_means_no_wake_consumer(monkeypatch: Any, tmp_path: Path) -> None:
    _install_fakes(monkeypatch)

    assert manager_loop_service.status(tmp_path)["wake"] == {
        "running": False,
        "queued": 0,
        "turns_this_hour": 0,
        "cap": manager_loop_wake.DEFAULT_CAP_PER_HOUR,
        "in_flight": 0,
        "failed_turns": 0,
        "parked_reason": None,
        "retry_in_seconds": 0.0,
    }
    entry = manager_loop_service._entry_for(tmp_path)
    assert entry.wake is None


def test_one_wake_consumer_per_repository_and_closing_the_session_stops_it(
    monkeypatch: Any, tmp_path: Path
) -> None:
    _install_fakes(monkeypatch)
    _install_fake_wake_source(monkeypatch, _noop_claim, _noop_ack)

    assert manager_loop_service.start(tmp_path, "fake", "model-a")["ok"] is True
    entry = manager_loop_service._entry_for(tmp_path)
    first = entry.wake
    assert first is not None
    assert manager_loop_service.status(tmp_path)["wake"]["running"] is True

    manager_loop_service._ensure_wake_started(entry, tmp_path)
    assert entry.wake is first  # exactly one consumer per repository; a second start is a no-op

    assert manager_loop_service.close(tmp_path)["ok"] is True
    assert entry.wake is None
    assert manager_loop_service.status(tmp_path)["wake"]["running"] is False


def test_start_can_configure_the_hourly_wake_cap(monkeypatch: Any, tmp_path: Path) -> None:
    _install_fakes(monkeypatch)
    _install_fake_wake_source(monkeypatch, _noop_claim, _noop_ack)

    assert manager_loop_service.start(tmp_path, "fake", "model-a", wake_cap_per_hour=3)["ok"] is True
    assert manager_loop_service.status(tmp_path)["wake"]["cap"] == 3


def test_a_callback_while_idle_starts_a_formatted_wake_turn_and_acks_it(
    monkeypatch: Any, tmp_path: Path
) -> None:
    backends = _install_fakes(monkeypatch)
    monkeypatch.setattr(manager_loop_service, "WAKE_IDLE_POLL_SECONDS", 0.02)
    monkeypatch.setattr(manager_loop_service, "WAKE_RETRY_POLL_SECONDS", 0.02)

    delivered = {"done": False}
    ack_calls: list[tuple[str, str]] = []

    def claim() -> dict[str, Any] | None:
        if delivered["done"]:
            return None
        delivered["done"] = True
        return {
            "batch_id": "b1",
            "lease_id": "l1",
            "members": [{"task_id": "CARD_A", "state": "review"}],
        }

    def ack(batch_id: str, lease_id: str) -> bool:
        ack_calls.append((batch_id, lease_id))
        return True

    _install_fake_wake_source(monkeypatch, claim, ack)

    assert manager_loop_service.start(tmp_path, "fake", "model-a")["ok"] is True
    backend = backends[0]

    assert backend.entered.wait(timeout=5)
    assert manager_loop_service.wait_for_idle(tmp_path, timeout=5) is True
    assert backend.messages == ["callback: CARD_A -> review"]
    assert ack_calls == [("b1", "l1")]

    status = manager_loop_service.status(tmp_path)
    assert status["last_turn"]["ok"] is True
    assert status["wake"]["turns_this_hour"] == 1


def _real_task_db(monkeypatch: Any, tmp_path: Path) -> Any:
    """A real ``callback_store`` on a tmp sqlite file behind ``default_callback_source``.

    Call after ``_install_fakes``: it puts the real claim/ack wiring back in place
    of the no-op source. Returns the connect function the claim itself uses.
    """

    monkeypatch.setattr(
        manager_loop_service, "default_callback_source", manager_loop_wake.default_callback_source
    )
    monkeypatch.setattr(manager_loop_service, "WAKE_IDLE_POLL_SECONDS", 0.02)
    monkeypatch.setattr(manager_loop_service, "WAKE_RETRY_POLL_SECONDS", 0.02)
    db_path = tmp_path / "task_queue.sqlite"

    def fake_canonical_connect(*, readonly: bool = False) -> sqlite3.Connection:
        conn = sqlite3.connect(str(db_path), isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(aiworkhub_core, "_canonical_connect", fake_canonical_connect)
    conn = fake_canonical_connect()
    try:
        callback_store.init_db(conn)
    finally:
        conn.close()
    return fake_canonical_connect


def _insert_review_card(conn: sqlite3.Connection, task_id: str, card: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO tasks (task_id, status, card_json, created_at, updated_at) "
        "VALUES (?, 'review', ?, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')",
        (task_id, json.dumps({"claim_epoch": 0, **card})),
    )


def _outbox_rows(connect: Any, task_id: str) -> list[tuple[str, str, str, str]]:
    conn = connect()
    try:
        return [
            (str(row["provider"]), str(row["origin_thread_id"]), str(row["state"]), str(row["last_error"] or ""))
            for row in conn.execute(
                "SELECT provider, origin_thread_id, state, last_error FROM callback_outbox "
                "WHERE task_id=? ORDER BY outbox_id",
                (task_id,),
            ).fetchall()
        ]
    finally:
        conn.close()


def test_a_pending_lane_row_of_a_card_the_chat_launched_is_moved_onto_it_and_settled(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """No fake claim/ack here: a real ``callback_store`` against a tmp sqlite
    file proves ``default_callback_source`` sees a callback whose lane
    ``origin_thread_id`` is whoever created the task, once the card is this
    chat's own -- and that the original lane row is settled, so exactly one
    consumer handles it.
    """
    backends = _install_fakes(monkeypatch)
    connect = _real_task_db(monkeypatch, tmp_path)
    conn = connect()
    try:
        _insert_review_card(conn, "CARD_X", {})
        assert callback_store.enqueue_callback(
            conn,
            "CARD_X",
            "some-other-route-thread-id-unrelated-to-the-manager-loop-session",
            "review_ready",
            provider="claude",
            episode_id="0",
        )
    finally:
        conn.close()

    started = manager_loop_service.start(tmp_path, "fake", "model-a")
    assert started["ok"] is True
    session_id = started["session"]["session_id"]
    backend = backends[0]
    # The chat launched this card; its lane row predates the stamp.
    conn = connect()
    try:
        conn.execute(
            "UPDATE tasks SET card_json=? WHERE task_id='CARD_X'",
            (json.dumps({"claim_epoch": 0, "manager_chat_session_id": session_id}),),
        )
    finally:
        conn.close()

    assert backend.entered.wait(timeout=10)
    assert manager_loop_service.wait_for_idle(tmp_path, timeout=5) is True
    assert backend.messages == ["callback: CARD_X -> review_ready"]

    status = manager_loop_service.status(tmp_path)
    assert status["last_turn"]["ok"] is True
    assert status["wake"]["turns_this_hour"] == 1
    assert manager_loop_service.close(tmp_path)["ok"] is True
    assert _outbox_rows(connect, "CARD_X") == [
        (
            "claude",
            "some-other-route-thread-id-unrelated-to-the-manager-loop-session",
            "superseded",
            "mirrored_to_manager_chat",
        ),
        ("manager_chat", session_id, "delivered", ""),
    ]


def test_wake_claims_callbacks_from_every_provider_family_not_just_claude(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """A worker terminal under another family (e.g. codex) must still wake
    the chat that launched it: a stamped card and an mls- origin card are each
    filed on the chat session whatever family coordinated them."""
    backends = _install_fakes(monkeypatch)
    connect = _real_task_db(monkeypatch, tmp_path)
    started = manager_loop_service.start(tmp_path, "fake", "model-a")
    assert started["ok"] is True
    session_id = started["session"]["session_id"]

    conn = connect()
    try:
        for card, family, owner in (
            ("CARD_C", "claude", {"manager_chat_session_id": session_id}),
            ("CARD_X", "codex", {"origin_thread_id": session_id}),
        ):
            _insert_review_card(conn, card, {"coordinator_provider": family, **owner})
            assert callback_store.enqueue_callback(
                conn,
                card,
                "some-other-route-thread-id",
                "review_ready",
                provider=family,
                episode_id="0",
            )
        # Filed on the chat directly: no lane manager can claim or rebind them first.
        lane_rows = conn.execute(
            "SELECT COUNT(*) FROM callback_outbox WHERE provider IN ('claude', 'codex')"
        ).fetchone()[0]
        assert lane_rows == 0
    finally:
        conn.close()

    assert backends[0].entered.wait(timeout=10)
    deadline = time.monotonic() + 15
    while len(backends[0].messages) < 2 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert manager_loop_service.wait_for_idle(tmp_path, timeout=10) is True

    assert sorted(backends[0].messages) == [
        "callback: CARD_C -> review_ready",
        "callback: CARD_X -> review_ready",
    ]
    assert manager_loop_service.close(tmp_path)["ok"] is True


def test_claim_does_not_wake_cards_the_chat_did_not_launch(
    monkeypatch: Any, tmp_path: Path
) -> None:
    connect = _real_task_db(monkeypatch, tmp_path)
    session_id = "mls-" + "ab" * 16
    conn = connect()
    try:
        _insert_review_card(conn, "CARD_OTHER", {"coordinator_provider": "claude"})
        _insert_review_card(
            conn, "CARD_MINE", {"coordinator_provider": "claude", "manager_chat_session_id": session_id}
        )
        for card in ("CARD_OTHER", "CARD_MINE"):
            assert callback_store.enqueue_callback(
                conn, card, "claude-thread", "review_ready", provider="claude", episode_id="0"
            )
    finally:
        conn.close()

    claim, ack = manager_loop_wake.default_callback_source(session_id=session_id)
    batch = claim()

    assert batch is not None
    assert [member["task_id"] for member in batch["members"]] == ["CARD_MINE"]
    assert ack(batch["batch_id"], batch["lease_id"]) is True
    assert claim() is None
    # The card the chat did not launch stays with its own lane manager, untouched.
    assert _outbox_rows(connect, "CARD_OTHER") == [("claude", "claude-thread", "pending", "")]
    assert _outbox_rows(connect, "CARD_MINE") == [("manager_chat", session_id, "delivered", "")]


def test_manager_chat_claim_releases_a_deferred_review_wake(
    monkeypatch: Any, tmp_path: Path
) -> None:
    # The review's manager wake was deferred to the quality-review chain, whose
    # grace has run out: the seed releases it, and the chat's own claim must seed
    # it -- even for a session idle past the seat lease, whatever family coordinated it.
    connect = _real_task_db(monkeypatch, tmp_path)
    session_id = "mls-" + "cd" * 16
    deferred_at = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    conn = connect()
    try:
        _insert_review_card(
            conn,
            "CARD_D",
            {
                "coordinator_provider": "claude",
                "manager_chat_session_id": session_id,
                "terminal_substatus": "review_ready",
                "terminal_review": {
                    "recorded_at": deferred_at,
                    "evidence": {"manager_callback_deferred": {"reason": "quality_review_chain"}},
                },
            },
        )
    finally:
        conn.close()

    claim, ack = manager_loop_wake.default_callback_source(session_id=session_id, holds_seat=False)
    batch = claim()

    assert batch is not None
    assert [(member["task_id"], member["state"]) for member in batch["members"]] == [
        ("CARD_D", "review_ready")
    ]
    assert ack(batch["batch_id"], batch["lease_id"]) is True
    assert claim() is None
    assert _outbox_rows(connect, "CARD_D") == [("manager_chat", session_id, "delivered", "")]


def test_publish_manager_ready_row_reaches_the_stamped_chat_session(
    monkeypatch: Any, tmp_path: Path
) -> None:
    # publish_manager_ready files its review_ready row through
    # task_store._enqueue_terminal_callback_row, in the review transaction. It
    # shares enqueue_callback's routing rule: the stamped chat session gets it,
    # and an mls- origin is manager_chat, so no lane manager can take it first.
    from aiworkhub import task_store

    connect = _real_task_db(monkeypatch, tmp_path)
    session_id = "mls-" + "ef" * 16
    conn = connect()
    try:
        _insert_review_card(
            conn, "CARD_P", {"coordinator_provider": "claude", "manager_chat_session_id": session_id}
        )
        _insert_review_card(conn, "CARD_M", {"coordinator_provider": "claude"})
        for card, origin in (("CARD_P", "claude-thread"), ("CARD_M", session_id)):
            assert task_store._enqueue_terminal_callback_row(
                conn,
                task_id=card,
                provider="claude",
                origin_thread_id=origin,
                transition="review_ready",
                episode_id="0",
                request_id="req-" + card,
                now="2026-01-01T00:00:01Z",
            )
        # A lane manager's rebind finds nothing of either card to take.
        assert callback_store.rebind_pending_callbacks(
            conn, provider="claude", origin_thread_id="claude-thread"
        ) == 0
    finally:
        conn.close()

    assert _outbox_rows(connect, "CARD_P") == [("manager_chat", session_id, "pending", "")]
    assert _outbox_rows(connect, "CARD_M") == [("manager_chat", session_id, "pending", "")]
    claim, _ack = manager_loop_wake.default_callback_source(session_id=session_id)
    batch = claim()
    assert batch is not None
    assert sorted(member["task_id"] for member in batch["members"]) == ["CARD_M", "CARD_P"]


def test_a_seated_session_idle_past_the_lease_still_wakes_its_stamped_cards(
    monkeypatch: Any, tmp_path: Path
) -> None:
    backends = _install_fakes(monkeypatch)
    connect = _real_task_db(monkeypatch, tmp_path)
    monkeypatch.setattr(
        manager_loop_service, "authorize_selected_route", lambda repo, backend_id, model: (backend_id, model)
    )
    entry = manager_loop_service._entry_for(tmp_path)
    stale = _seat_record(entry, "mls-" + "3c" * 16, hours_idle=13)
    conn = connect()
    try:
        _insert_review_card(
            conn, "CARD_S", {"coordinator_provider": "claude", "manager_chat_session_id": stale.session_id}
        )
        assert callback_store.enqueue_callback(
            conn, "CARD_S", "claude-thread", "review_ready", provider="claude", episode_id="0"
        )
    finally:
        conn.close()

    # A restart: the panel restores the bound session, idle past the seat lease.
    assert manager_loop_service.restore(tmp_path)["ok"] is True
    try:
        deadline = time.monotonic() + 10
        while not backends and time.monotonic() < deadline:
            time.sleep(0.02)
        assert backends, "the restored session's wake consumer never started a turn"
        assert backends[0].entered.wait(timeout=10)
        assert manager_loop_service.wait_for_idle(tmp_path, timeout=10) is True
        assert backends[0].messages == ["callback: CARD_S -> review_ready"]
    finally:
        assert manager_loop_service.close(tmp_path)["ok"] is True
    assert _outbox_rows(connect, "CARD_S") == [("manager_chat", stale.session_id, "delivered", "")]


def test_a_wake_arriving_during_a_running_turn_is_delivered_after_it(
    monkeypatch: Any, tmp_path: Path
) -> None:
    backends = _install_fakes(monkeypatch)
    assert manager_loop_service.start(tmp_path, "fake", "model-a")["ok"] is True
    backend = backends[0]
    backend.gate = threading.Event()
    outcomes: list[bool] = []
    finished = threading.Event()

    def done(delivered: bool, errors: Any = ()) -> None:
        outcomes.append(delivered)
        finished.set()

    try:
        assert manager_loop_service.send(tmp_path, "one")["ok"] is True
        assert backend.entered.wait(timeout=5)
        assert manager_loop_service.send(tmp_path, "two")["queued"] is True

        # The turn is busy: the wake is queued, not refused, and is not run yet.
        assert manager_loop_service._wake_dispatch(
            tmp_path, {"task_id": "CARD_Q", "state": "review_ready"}, done
        ) is True
        assert not finished.is_set()

        backend.gate.set()
        assert finished.wait(timeout=5)
        assert manager_loop_service.wait_for_idle(tmp_path, timeout=5) is True
        # After the running turn and the queued owner send, never dropped.
        assert backend.messages == ["one", "two", "callback: CARD_Q -> review_ready"]
        assert outcomes == [True]
    finally:
        backend.gate.set()
        assert manager_loop_service.wait_for_idle(tmp_path, timeout=5) is True
        assert manager_loop_service.close(tmp_path)["ok"] is True


def test_a_seated_chat_gets_no_copy_of_a_card_it_did_not_launch(
    monkeypatch: Any, tmp_path: Path
) -> None:
    # Even while a chat session holds the seat, a card no chat launched stays
    # with its lane manager alone: exactly one consumer handles the callback.
    connect = _real_task_db(monkeypatch, tmp_path)
    session_id = "mls-" + "9a" * 16
    monkeypatch.setattr(callback_store, "_active_manager_chat_session_from_conn", lambda conn: session_id)
    conn = connect()
    try:
        _insert_review_card(conn, "CARD_LANE", {"coordinator_provider": "claude"})
        assert callback_store.enqueue_callback(
            conn, "CARD_LANE", "claude-thread", "review_ready", provider="claude", episode_id="0"
        )
    finally:
        conn.close()

    assert _outbox_rows(connect, "CARD_LANE") == [("claude", "claude-thread", "pending", "")]


def test_the_launching_session_wins_over_a_different_mls_origin(
    monkeypatch: Any, tmp_path: Path
) -> None:
    # A rework another chat session launched: the card's origin is the session
    # that created it, its stamp the one that launched it. The row is filed on
    # the launcher, where the seed and the claim look for it.
    connect = _real_task_db(monkeypatch, tmp_path)
    creator, launcher = "mls-" + "1a" * 16, "mls-" + "2b" * 16
    conn = connect()
    try:
        _insert_review_card(
            conn,
            "CARD_RW",
            {"coordinator_provider": "claude", "origin_thread_id": creator, "manager_chat_session_id": launcher},
        )
        assert callback_store.enqueue_callback(
            conn, "CARD_RW", creator, "review_ready", provider="claude", episode_id="0"
        )
    finally:
        conn.close()

    assert _outbox_rows(connect, "CARD_RW") == [("manager_chat", launcher, "pending", "")]


def test_a_wake_queued_behind_an_unrecorded_turn_records_its_own_last_turn(
    monkeypatch: Any, tmp_path: Path
) -> None:
    # rotate dispatches with record_last_turn=False; the wake queued behind it is
    # an ordinary wake turn and reports its outcome in status like any other.
    _install_fakes(monkeypatch)
    assert manager_loop_service.start(tmp_path, "fake", "model-a")["ok"] is True
    entry = manager_loop_service._entry_for(tmp_path)
    entry.last_turn = None
    gate = threading.Event()
    finished = threading.Event()

    def unrecorded(orchestrator: Any) -> dict[str, Any]:
        gate.wait(timeout=5)
        return {"ok": True, "reply": "handoff", "errors": []}

    def done(delivered: bool, errors: Any = ()) -> None:
        finished.set()

    try:
        assert manager_loop_service._dispatch_turn(tmp_path, unrecorded, record_last_turn=False)["ok"] is True
        assert manager_loop_service._wake_dispatch(
            tmp_path, {"task_id": "CARD_R", "state": "review_ready"}, done
        ) is True
        gate.set()
        assert finished.wait(timeout=5)
        assert manager_loop_service.wait_for_idle(tmp_path, timeout=5) is True
        assert entry.last_turn is not None and entry.last_turn["ok"] is True
        assert entry.last_turn["reply"] != "handoff"
    finally:
        gate.set()
        assert manager_loop_service.wait_for_idle(tmp_path, timeout=5) is True
        assert manager_loop_service.close(tmp_path)["ok"] is True


# ---------------------------------------------------------------------------
# Outcome-aware settle (NF-2026-01227): a batch is acked only once its
# members' turns FINISHED and were delivered; a failed turn is retried after a
# backoff measured on the injected clock.
# ---------------------------------------------------------------------------


class _ManualDispatch:
    """A dispatch fake whose started turns finish only when the test calls their ``done``."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.dones: list[Any] = []
        self._cond = threading.Condition()

    def __call__(self, member: dict[str, Any], done: Any = None) -> bool:
        with self._cond:
            self.calls.append(member["task_id"])
            self.dones.append(done)
            self._cond.notify_all()
        return True

    def wait_calls(self, count: int, timeout: float = 5.0) -> bool:
        with self._cond:
            return self._cond.wait_for(lambda: len(self.calls) >= count, timeout)


def _one_batch_claim(task_ids: list[str]) -> Any:
    delivered = {"done": False}

    def claim() -> dict[str, Any] | None:
        if delivered["done"]:
            return None
        delivered["done"] = True
        return {
            "batch_id": "b1",
            "lease_id": "l1",
            "members": [{"task_id": task_id, "state": "s"} for task_id in task_ids],
        }

    return claim


def _manual_consumer(
    task_ids: list[str], clock: _FakeClock
) -> tuple[WakeConsumer, _ManualDispatch, list[tuple[str, str]], threading.Event]:
    """A consumer with the default failed-turn backoff (300 seconds on ``clock``)."""

    dispatch = _ManualDispatch()
    ack_calls: list[tuple[str, str]] = []
    acked = threading.Event()

    def ack(batch_id: str, lease_id: str) -> bool:
        ack_calls.append((batch_id, lease_id))
        acked.set()
        return True

    consumer = WakeConsumer(
        claim=_one_batch_claim(task_ids),
        ack=ack,
        dispatch=dispatch,
        clock=clock,
        idle_poll_seconds=0.02,
        retry_poll_seconds=0.02,
    )
    return consumer, dispatch, ack_calls, acked


def test_a_batch_is_not_acked_while_its_turn_runs_and_is_acked_once_it_is_delivered() -> None:
    consumer, dispatch, ack_calls, acked = _manual_consumer(["A"], _FakeClock())
    consumer.start()
    try:
        assert dispatch.wait_calls(1)
        assert not acked.wait(timeout=0.3)  # the turn only STARTED: nothing is acked yet
        status = consumer.status()
        assert status["turns_this_hour"] == 0
        assert status["in_flight"] == 1

        dispatch.dones[0](True)
        assert ack_calls == [("b1", "l1")]
        status = consumer.status()
        assert status["turns_this_hour"] == 1
        assert status["in_flight"] == 0
    finally:
        consumer.stop()

    assert dispatch.calls == ["A"]
    assert ack_calls == [("b1", "l1")]


def test_a_failed_turn_is_not_acked_or_counted_and_is_retried_only_after_the_backoff() -> None:
    clock = _FakeClock(1000.0)
    consumer, dispatch, ack_calls, acked = _manual_consumer(["A"], clock)
    consumer.start()
    try:
        assert dispatch.wait_calls(1)
        assert not acked.wait(timeout=0.3)  # a started turn is not acked
        dispatch.dones[0](False)

        assert ack_calls == []
        status = consumer.status()
        assert status["turns_this_hour"] == 0
        assert status["queued"] == 1
        assert status["in_flight"] == 0
        assert status["failed_turns"] == 1
        assert status["retry_in_seconds"] == 300.0

        assert not dispatch.wait_calls(2, timeout=0.3)  # paused: no retry yet
        clock.value += 299.0
        assert not dispatch.wait_calls(2, timeout=0.2)  # still inside the backoff
        clock.value += 1.0
        assert dispatch.wait_calls(2)
        assert dispatch.calls == ["A", "A"]
        assert ack_calls == []

        dispatch.dones[1](True)
        assert ack_calls == [("b1", "l1")]
        status = consumer.status()
        assert status["turns_this_hour"] == 1
        assert status["failed_turns"] == 0
        assert status["retry_in_seconds"] == 0.0
    finally:
        consumer.stop()

    assert dispatch.calls == ["A", "A"]
    assert ack_calls == [("b1", "l1")]


def test_a_two_member_batch_whose_first_turn_fails_delivers_each_member_once_in_order() -> None:
    clock = _FakeClock(0.0)
    consumer, dispatch, ack_calls, _acked = _manual_consumer(["A", "B"], clock)
    consumer.start()
    try:
        assert dispatch.wait_calls(1)
        assert consumer.status()["in_flight"] == 1
        assert not dispatch.wait_calls(2, timeout=0.3)  # B waits: at most one member is in flight
        dispatch.dones[0](False)

        assert not dispatch.wait_calls(2, timeout=0.3)  # B is not dispatched during the pause
        assert consumer.status()["queued"] == 2

        clock.value += 300.0
        assert dispatch.wait_calls(2)
        assert dispatch.calls == ["A", "A"]
        dispatch.dones[1](True)
        assert ack_calls == []  # B has not been delivered yet

        assert dispatch.wait_calls(3)
        assert dispatch.calls == ["A", "A", "B"]
        dispatch.dones[2](True)
        assert ack_calls == [("b1", "l1")]
        assert not dispatch.wait_calls(4, timeout=0.2)
    finally:
        consumer.stop()

    assert dispatch.calls == ["A", "A", "B"]
    assert ack_calls == [("b1", "l1")]


def test_a_second_done_for_the_same_turn_settles_nothing_more() -> None:
    consumer, dispatch, ack_calls, _acked = _manual_consumer(["A"], _FakeClock())
    consumer.start()
    try:
        assert dispatch.wait_calls(1)
        done = dispatch.dones[0]
        done(True)
        done(True)
        done(False)

        assert ack_calls == [("b1", "l1")]
        status = consumer.status()
        assert status["turns_this_hour"] == 1
        assert status["failed_turns"] == 0
        assert status["queued"] == 0
        assert status["in_flight"] == 0
        assert not dispatch.wait_calls(2, timeout=0.3)
    finally:
        consumer.stop()

    assert dispatch.calls == ["A"]


def test_status_exposes_in_flight_failed_turns_and_retry_in_seconds() -> None:
    consumer = WakeConsumer(
        claim=_noop_claim, ack=_noop_ack, dispatch=lambda member, done: False, clock=_FakeClock()
    )

    assert consumer.status() == {
        "running": False,
        "queued": 0,
        "turns_this_hour": 0,
        "cap": manager_loop_wake.DEFAULT_CAP_PER_HOUR,
        "in_flight": 0,
        "failed_turns": 0,
        "parked_reason": None,
        "retry_in_seconds": 0.0,
    }


def _run_one_wake_turn(monkeypatch: Any, tmp_path: Path, wake: Any) -> list[tuple[str, str]]:
    """Drive one claimed callback through the real service wiring with ``wake`` as the turn."""

    _install_fakes(monkeypatch)
    monkeypatch.setattr(manager_loop_service, "WAKE_IDLE_POLL_SECONDS", 0.02)
    monkeypatch.setattr(manager_loop_service, "WAKE_RETRY_POLL_SECONDS", 0.02)
    woke = threading.Event()

    def fake_wake(self: Any, member: Any) -> dict[str, Any]:
        woke.set()
        return wake()

    monkeypatch.setattr(manager_loop.ManagerOrchestrator, "wake", fake_wake)
    ack_calls: list[tuple[str, str]] = []

    def ack(batch_id: str, lease_id: str) -> bool:
        ack_calls.append((batch_id, lease_id))
        return True

    _install_fake_wake_source(monkeypatch, _one_batch_claim(["CARD_A"]), ack)

    assert manager_loop_service.start(tmp_path, "fake", "model-a")["ok"] is True
    assert woke.wait(timeout=5)
    assert manager_loop_service.wait_for_idle(tmp_path, timeout=5) is True
    return ack_calls


def test_a_wake_turn_that_raises_leaves_its_batch_unacked(monkeypatch: Any, tmp_path: Path) -> None:
    def wake() -> dict[str, Any]:
        raise RuntimeError("provider down")

    ack_calls = _run_one_wake_turn(monkeypatch, tmp_path, wake)
    try:
        assert ack_calls == []
        status = manager_loop_service.status(tmp_path)
        assert status["last_turn"]["ok"] is False
        assert status["wake"]["turns_this_hour"] == 0
        assert status["wake"]["failed_turns"] == 1
        assert status["wake"]["queued"] == 1
    finally:
        assert manager_loop_service.close(tmp_path)["ok"] is True
    assert ack_calls == []


def test_a_wake_turn_that_is_not_ok_with_an_empty_reply_leaves_its_batch_unacked(
    monkeypatch: Any, tmp_path: Path
) -> None:
    ack_calls = _run_one_wake_turn(
        monkeypatch, tmp_path, lambda: {"ok": False, "reply": "  ", "errors": ["provider_error"]}
    )
    try:
        assert ack_calls == []
        status = manager_loop_service.status(tmp_path)
        assert status["wake"]["turns_this_hour"] == 0
        assert status["wake"]["failed_turns"] == 1
    finally:
        assert manager_loop_service.close(tmp_path)["ok"] is True
    assert ack_calls == []


_AUTH_FAILURE = "Failed to authenticate: OAuth session expired. Please run /login."


def test_a_provider_failure_whose_text_arrived_as_a_reply_leaves_its_batch_unacked(
    monkeypatch: Any, tmp_path: Path
) -> None:
    ack_calls = _run_one_wake_turn(
        monkeypatch,
        tmp_path,
        lambda: {
            "ok": False,
            "reply": _AUTH_FAILURE,
            "errors": [
                {"source": "provider", "error": _AUTH_FAILURE},
                {"source": "worker_failed", "error": "worker_process_failure_no_provider_refusal_signal"},
            ],
        },
    )
    try:
        assert ack_calls == []
        status = manager_loop_service.status(tmp_path)
        assert status["wake"]["turns_this_hour"] == 0
        assert status["wake"]["failed_turns"] == 1
        assert status["wake"]["queued"] == 1
    finally:
        assert manager_loop_service.close(tmp_path)["ok"] is True
    assert ack_calls == []


def test_an_ok_false_turn_does_not_ack_its_callback(
    monkeypatch: Any, tmp_path: Path
) -> None:
    # Even when the model replied, an ok=false turn (an event-cap or Context Graph
    # write error, an empty provider turn) leaves its row for retry on the backoff.
    ack_calls = _run_one_wake_turn(
        monkeypatch,
        tmp_path,
        lambda: {
            "ok": False,
            "reply": "Seen CARD_A.",
            "errors": [
                {"source": "loop", "error": "turn_event_limit"},
                {"source": "context_graph_event_write", "error": "not_ok"},
            ],
        },
    )
    try:
        assert ack_calls == []
        status = manager_loop_service.status(tmp_path)
        assert status["wake"]["turns_this_hour"] == 0
        assert status["wake"]["failed_turns"] == 1
        assert status["wake"]["queued"] == 1
        assert status["wake"]["parked_reason"] == manager_loop_wake.PARKED_BACKOFF
    finally:
        assert manager_loop_service.close(tmp_path)["ok"] is True
    assert ack_calls == []


class _RouteAuthority:
    """Stands in for ``authorize_selected_route``: records each ask, answers ``allowed``."""

    def __init__(self, allowed: bool) -> None:
        self.allowed = allowed
        self.asked: list[tuple[str, str]] = []
        self.called = threading.Event()

    def __call__(self, repo: Any, backend_id: str, model: str) -> tuple[str, str] | None:
        self.asked.append((backend_id, model))
        self.called.set()
        return (backend_id, model) if self.allowed else None


def _claim_one_callback(monkeypatch: Any, authority: _RouteAuthority) -> tuple[list[Any], list[tuple[str, str]]]:
    backends = _install_fakes(monkeypatch)
    monkeypatch.setattr(manager_loop_service, "WAKE_IDLE_POLL_SECONDS", 0.02)
    monkeypatch.setattr(manager_loop_service, "WAKE_RETRY_POLL_SECONDS", 0.02)
    monkeypatch.setattr(manager_loop_service, "authorize_selected_route", authority)
    ack_calls: list[tuple[str, str]] = []

    def ack(batch_id: str, lease_id: str) -> bool:
        ack_calls.append((batch_id, lease_id))
        return True

    _install_fake_wake_source(monkeypatch, _one_batch_claim(["CARD_A"]), ack)
    return backends, ack_calls


def test_a_wake_rebinds_a_restored_session_onto_its_own_authorized_route(
    monkeypatch: Any, tmp_path: Path
) -> None:
    # A session loaded from disk (a server restart, a panel reload) is bound but has
    # no backend yet. Restoring it arms its wake consumer, and its callback must
    # still reach the model on the session's own authorized route.
    authority = _RouteAuthority(allowed=True)
    backends, ack_calls = _claim_one_callback(monkeypatch, authority)
    entry = manager_loop_service._entry_for(tmp_path)
    restored = _seat_record(entry, "mls-" + "0d" * 16)

    assert manager_loop_service.restore(tmp_path)["ok"] is True
    try:
        assert authority.called.wait(timeout=5)
        assert manager_loop_service.wait_for_idle(tmp_path, timeout=5) is True
        assert authority.asked == [("fake", "model-a")]
        assert [(backend.backend_id, backend.model) for backend in backends] == [("fake", "model-a")]
        assert backends[0].messages == ["callback: CARD_A -> s"]
        assert ack_calls == [("b1", "l1")]
        status = manager_loop_service.status(tmp_path)
        assert status["last_turn"]["ok"] is True
        assert status["session"]["session_id"] == restored.session_id
        assert status["wake"]["failed_turns"] == 0
    finally:
        assert manager_loop_service.close(tmp_path)["ok"] is True


def test_a_wake_never_rebinds_a_restored_session_onto_a_route_policy_no_longer_allows(
    monkeypatch: Any, tmp_path: Path
) -> None:
    authority = _RouteAuthority(allowed=False)
    backends, ack_calls = _claim_one_callback(monkeypatch, authority)
    entry = manager_loop_service._entry_for(tmp_path)
    _seat_record(entry, "mls-" + "0e" * 16)

    assert manager_loop_service.restore(tmp_path)["ok"] is True
    try:
        assert authority.called.wait(timeout=5)
        assert manager_loop_service.wait_for_idle(tmp_path, timeout=5) is True
        assert backends == []
        assert ack_calls == []
        status = manager_loop_service.status(tmp_path)
        assert status["last_turn"]["ok"] is False
        assert "manager_backend_unavailable:fake:model-a" in status["last_turn"]["errors"][0]
        assert status["wake"]["failed_turns"] == 1
        assert status["wake"]["queued"] == 1
        assert authority.asked == [("fake", "model-a")]
    finally:
        assert manager_loop_service.close(tmp_path)["ok"] is True
    assert ack_calls == []


def test_a_wake_on_a_session_that_already_has_its_backend_asks_for_no_authorization(
    monkeypatch: Any, tmp_path: Path
) -> None:
    authority = _RouteAuthority(allowed=False)
    backends, ack_calls = _claim_one_callback(monkeypatch, authority)

    assert manager_loop_service.start(tmp_path, "fake", "model-a")["ok"] is True
    try:
        assert backends[0].entered.wait(timeout=5)
        assert manager_loop_service.wait_for_idle(tmp_path, timeout=5) is True
        assert authority.asked == []
        assert len(backends) == 1
        assert backends[0].messages == ["callback: CARD_A -> s"]
        assert ack_calls == [("b1", "l1")]
    finally:
        assert manager_loop_service.close(tmp_path)["ok"] is True


# ---------------------------------------------------------------------------
# Provider-aware parking: limit text parks until its reset, auth text parks
# until the owner acts, anything else backs off exponentially. The callback
# is never dropped: it stays queued and un-acked throughout.
# ---------------------------------------------------------------------------

_LIMIT_TEXT = "You've hit your session limit · resets 4am (UTC)"
_AUTH_TEXT = "Failed to authenticate: OAuth session expired and could not be refreshed"


def _provider_error(text: str) -> list[dict[str, str]]:
    return [{"source": "provider", "error": text}]


def _parking_consumer(
    task_ids: list[str], clock: _FakeClock, wall_now: datetime | None = None
) -> tuple[WakeConsumer, _ManualDispatch, list[tuple[str, str]]]:
    dispatch = _ManualDispatch()
    ack_calls: list[tuple[str, str]] = []

    def ack(batch_id: str, lease_id: str) -> bool:
        ack_calls.append((batch_id, lease_id))
        return True

    consumer = WakeConsumer(
        claim=_one_batch_claim(task_ids),
        ack=ack,
        dispatch=dispatch,
        clock=clock,
        wall_clock=(lambda: wall_now) if wall_now is not None else manager_loop_wake._utc_now,
        idle_poll_seconds=0.02,
        retry_poll_seconds=0.02,
    )
    return consumer, dispatch, ack_calls


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("You've hit your session limit · resets 4am (Asia/Tbilisi)", "provider_limit"),
        ("Claude usage limit reached", "provider_limit"),
        ("429 rate limit exceeded", "provider_limit"),
        (_AUTH_TEXT, "provider_auth"),
        ("Please run /login", "provider_auth"),
        ("Incorrect API key: invalid api key", "provider_auth"),
        ("provider down", None),
        ("", None),
    ],
)
def test_classify_failure_reads_the_provider_error_text(text: str, expected: str | None) -> None:
    assert manager_loop_wake.classify_failure(_provider_error(text)) == expected
    assert manager_loop_wake.classify_failure([text]) == expected


def test_a_reset_time_is_parsed_in_its_own_timezone_and_never_in_the_past() -> None:
    parse = manager_loop_wake.parse_reset_delay_seconds
    utc = timezone.utc

    assert parse(_LIMIT_TEXT, datetime(2026, 10, 14, 23, 5, tzinfo=utc)) == 17700.0  # tomorrow 04:00
    assert parse(_LIMIT_TEXT, datetime(2026, 10, 14, 2, 0, tzinfo=utc)) == 7200.0  # today 04:00
    assert parse(_LIMIT_TEXT, datetime(2026, 10, 14, 4, 0, tzinfo=utc)) == 86400.0  # exactly now -> tomorrow
    assert parse("limit reached, resets 5:30pm (UTC)", datetime(2026, 10, 14, 12, 0, tzinfo=utc)) == 19800.0
    assert parse("resets 12am (UTC)", datetime(2026, 10, 14, 23, 0, tzinfo=utc)) == 3600.0
    plus_four = timezone(timedelta(hours=4))
    assert parse("resets 4am", datetime(2026, 10, 14, 23, 5, tzinfo=plus_four)) == 17700.0  # no zone: now's own
    assert parse("resets 4am (Not/AZone)", datetime(2026, 10, 14, tzinfo=utc)) is None
    assert parse("usage limit reached", datetime(2026, 10, 14, tzinfo=utc)) is None
    assert parse("resets 13am (UTC)", datetime(2026, 10, 14, tzinfo=utc)) is None
    assert parse(_LIMIT_TEXT, datetime(2026, 10, 14, 23, 5)) is None  # naive now: refuse to guess


def test_the_tbilisi_reset_from_the_measured_failure_is_parsed() -> None:
    try:
        ZoneInfo("Asia/Tbilisi")
    except ZoneInfoNotFoundError:
        pytest.skip("no IANA timezone database on this host")
    text = "You've hit your session limit · resets 4am (Asia/Tbilisi)"
    now = datetime(2026, 10, 14, 19, 5, tzinfo=timezone.utc)  # 23:05 in Tbilisi (UTC+4)
    assert manager_loop_wake.parse_reset_delay_seconds(text, now) == 17700.0


def test_failed_turns_back_off_300_600_1200_capped_at_3600_and_reset_on_a_delivered_turn() -> None:
    clock = _FakeClock(0.0)
    consumer, dispatch, ack_calls = _parking_consumer(["A", "B"], clock)
    consumer.start()
    try:
        assert dispatch.wait_calls(1)
        for attempt, expected in enumerate((300.0, 600.0, 1200.0, 2400.0, 3600.0, 3600.0), start=1):
            dispatch.dones[attempt - 1](False, _provider_error("provider down"))
            status = consumer.status()
            assert status["failed_turns"] == attempt
            assert status["parked_reason"] == "backoff"
            assert status["retry_in_seconds"] == expected
            assert status["queued"] == 2  # never dropped

            clock.value += expected - 1.0
            assert not dispatch.wait_calls(attempt + 1, timeout=0.2)  # still parked
            clock.value += 1.0
            assert dispatch.wait_calls(attempt + 1)

        dispatch.dones[6](True)  # A is delivered: the streak ends
        assert dispatch.wait_calls(8)
        assert dispatch.calls == ["A"] * 7 + ["B"]
        dispatch.dones[7](False, _provider_error("provider down"))
        status = consumer.status()
        assert (status["failed_turns"], status["retry_in_seconds"]) == (1, 300.0)
        assert ack_calls == []
    finally:
        consumer.stop()


def test_a_failure_without_errors_still_backs_off_from_the_base_delay() -> None:
    clock = _FakeClock(0.0)
    consumer, dispatch, _ack_calls = _parking_consumer(["A"], clock)
    consumer.start()
    try:
        assert dispatch.wait_calls(1)
        dispatch.dones[0](False)
        status = consumer.status()
        assert (status["parked_reason"], status["retry_in_seconds"]) == ("backoff", 300.0)
    finally:
        consumer.stop()


def test_a_session_limit_parks_until_the_reset_time_and_never_dispatches_earlier() -> None:
    clock = _FakeClock(1000.0)
    wall_now = datetime(2026, 10, 14, 23, 5, tzinfo=timezone.utc)  # reset 4am UTC is 17700s away
    consumer, dispatch, ack_calls = _parking_consumer(["A"], clock, wall_now)
    consumer.start()
    try:
        assert dispatch.wait_calls(1)
        dispatch.dones[0](False, _provider_error(_LIMIT_TEXT))

        status = consumer.status()
        assert status["parked_reason"] == "provider_limit"
        assert status["retry_in_seconds"] == 17700.0
        assert status["queued"] == 1
        assert status["failed_turns"] == 1
        assert consumer.resume() is False  # only an auth park yields to the owner

        clock.value += 300.0  # the generic backoff has long passed; the limit has not
        assert not dispatch.wait_calls(2, timeout=0.3)
        clock.value += 17700.0 - 300.0 - 1.0  # one second before the reset
        assert not dispatch.wait_calls(2, timeout=0.3)
        assert consumer.status()["retry_in_seconds"] == 1.0

        clock.value += 1.0  # the reset moment
        assert dispatch.wait_calls(2)
        assert dispatch.calls == ["A", "A"]
        assert ack_calls == []

        dispatch.dones[1](True)
        assert ack_calls == [("b1", "l1")]
        assert consumer.status()["parked_reason"] is None
    finally:
        consumer.stop()


def test_a_limit_without_a_parsable_reset_falls_back_to_the_escalating_backoff() -> None:
    clock = _FakeClock(0.0)
    consumer, dispatch, _ack_calls = _parking_consumer(["A"], clock)
    consumer.start()
    try:
        assert dispatch.wait_calls(1)
        dispatch.dones[0](False, _provider_error("Claude usage limit reached"))
        status = consumer.status()
        assert (status["parked_reason"], status["retry_in_seconds"]) == ("provider_limit", 300.0)

        clock.value += 300.0
        assert dispatch.wait_calls(2)
        dispatch.dones[1](False, _provider_error("resets 4am (Not/AZone) usage limit"))
        status = consumer.status()
        assert (status["parked_reason"], status["retry_in_seconds"]) == ("provider_limit", 600.0)
    finally:
        consumer.stop()


def test_an_authentication_failure_parks_without_retry_until_resumed() -> None:
    clock = _FakeClock(0.0)
    consumer, dispatch, ack_calls = _parking_consumer(["A"], clock)
    consumer.start()
    try:
        assert dispatch.wait_calls(1)
        dispatch.dones[0](False, _provider_error(_AUTH_TEXT))

        status = consumer.status()
        assert status["parked_reason"] == "provider_auth"
        assert status["retry_in_seconds"] is None  # no automatic retry
        assert status["queued"] == 1
        assert ack_calls == []

        clock.value += 10 * 24 * 3600.0
        assert not dispatch.wait_calls(2, timeout=0.3)  # time alone never un-parks it
        assert consumer.status()["parked_reason"] == "provider_auth"

        assert consumer.resume() is True
        assert consumer.status()["parked_reason"] is None
        assert dispatch.wait_calls(2)
        assert dispatch.calls == ["A", "A"]
        assert consumer.resume() is False  # nothing left to un-park

        dispatch.dones[1](True)
        assert ack_calls == [("b1", "l1")]
    finally:
        consumer.stop()


def test_a_wake_turns_provider_errors_reach_the_consumer_and_park_it(
    monkeypatch: Any, tmp_path: Path
) -> None:
    ack_calls = _run_one_wake_turn(
        monkeypatch,
        tmp_path,
        lambda: {"ok": False, "reply": "", "errors": _provider_error(_LIMIT_TEXT)},
    )
    try:
        assert ack_calls == []
        status = manager_loop_service.status(tmp_path)["wake"]
        assert status["parked_reason"] == "provider_limit"
        assert status["queued"] == 1
        assert status["failed_turns"] == 1
        assert 0.0 < status["retry_in_seconds"] <= 24 * 3600.0
    finally:
        assert manager_loop_service.close(tmp_path)["ok"] is True
    assert ack_calls == []


def test_an_owner_send_unparks_a_wake_parked_on_an_authentication_failure(
    monkeypatch: Any, tmp_path: Path
) -> None:
    calls = {"n": 0}

    def wake() -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] == 1:
            return {"ok": False, "reply": "", "errors": _provider_error(_AUTH_TEXT)}
        return {"ok": True, "reply": "seen", "errors": []}

    ack_calls = _run_one_wake_turn(monkeypatch, tmp_path, wake)
    try:
        status = manager_loop_service.status(tmp_path)["wake"]
        assert (status["parked_reason"], status["retry_in_seconds"]) == ("provider_auth", None)
        assert status["queued"] == 1
        assert not threading.Event().wait(0.3)  # parked: no automatic retry in the meantime
        assert calls["n"] == 1
        assert ack_calls == []

        assert manager_loop_service.send(tmp_path, "i logged in again")["ok"] is True
        assert manager_loop_service.wait_for_idle(tmp_path, timeout=5) is True
        deadline = time.monotonic() + 10
        while not ack_calls and time.monotonic() < deadline:
            time.sleep(0.02)
        assert calls["n"] == 2
        assert ack_calls == [("b1", "l1")]
        status = manager_loop_service.status(tmp_path)["wake"]
        assert (status["parked_reason"], status["queued"], status["failed_turns"]) == (None, 0, 0)
    finally:
        assert manager_loop_service.close(tmp_path)["ok"] is True


def test_an_owner_send_queued_behind_a_failing_wake_turn_still_unparks_it(
    monkeypatch: Any, tmp_path: Path
) -> None:
    # The send's own resume runs while the wake turn is still in flight, before
    # it parks on the auth failure; the queued send must resume again as it runs.
    _install_fakes(monkeypatch)
    monkeypatch.setattr(manager_loop_service, "WAKE_IDLE_POLL_SECONDS", 0.02)
    monkeypatch.setattr(manager_loop_service, "WAKE_RETRY_POLL_SECONDS", 0.02)
    entered, release = threading.Event(), threading.Event()
    calls = {"n": 0}

    def fake_wake(self: Any, member: Any) -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] == 1:
            entered.set()
            release.wait(timeout=5)
            return {"ok": False, "reply": "", "errors": _provider_error(_AUTH_TEXT)}
        return {"ok": True, "reply": "seen", "errors": []}

    monkeypatch.setattr(manager_loop.ManagerOrchestrator, "wake", fake_wake)
    ack_calls: list[tuple[str, str]] = []

    def ack(batch_id: str, lease_id: str) -> bool:
        ack_calls.append((batch_id, lease_id))
        return True

    _install_fake_wake_source(monkeypatch, _one_batch_claim(["CARD_A"]), ack)
    assert manager_loop_service.start(tmp_path, "fake", "model-a")["ok"] is True
    try:
        assert entered.wait(timeout=5)
        assert manager_loop_service.send(tmp_path, "i logged in again")["queued"] is True
        release.set()
        assert manager_loop_service.wait_for_idle(tmp_path, timeout=5) is True
        deadline = time.monotonic() + 10
        while not ack_calls and time.monotonic() < deadline:
            time.sleep(0.02)
        assert calls["n"] == 2
        assert ack_calls == [("b1", "l1")]
    finally:
        assert manager_loop_service.close(tmp_path)["ok"] is True
