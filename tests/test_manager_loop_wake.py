from __future__ import annotations

import json
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import callback_store  # noqa: E402
from aiworkhub import core as aiworkhub_core  # noqa: E402
from aiworkhub import manager_loop  # noqa: E402
from aiworkhub import manager_loop_service  # noqa: E402
from aiworkhub import manager_loop_wake  # noqa: E402
from aiworkhub.manager_loop_wake import WakeConsumer  # noqa: E402

from test_manager_loop_service import _install_fakes  # noqa: E402


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


def test_a_real_callback_with_an_arbitrary_origin_thread_id_is_claimed_and_woken_while_a_session_is_active(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """No fake claim/ack here: a real ``callback_store`` against a tmp sqlite
    file proves ``default_callback_source`` actually sees a callback whose
    ``origin_thread_id`` is whoever created the task -- never this manager
    loop session's own id -- the gap a fake claim/ack pair can never expose.
    """
    backends = _install_fakes(monkeypatch)
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
        conn.execute(
            "INSERT INTO tasks (task_id, status, card_json, created_at, updated_at) "
            "VALUES (?, 'review', ?, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')",
            ("CARD_X", json.dumps({"claim_epoch": 0})),
        )
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

    assert manager_loop_service.start(tmp_path, "fake", "model-a")["ok"] is True
    backend = backends[0]

    assert backend.entered.wait(timeout=5)
    assert manager_loop_service.wait_for_idle(tmp_path, timeout=5) is True
    assert backend.messages == ["callback: CARD_X -> review_ready"]

    status = manager_loop_service.status(tmp_path)
    assert status["last_turn"]["ok"] is True
    assert status["wake"]["turns_this_hour"] == 1

    assert manager_loop_service.close(tmp_path)["ok"] is True


def test_wake_claims_callbacks_from_every_provider_family_not_just_claude(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """A worker terminal under another family (e.g. codex) must still wake
    an opencode/claude seat: the default source iterates every pending
    family instead of assuming one provider."""
    backends = _install_fakes(monkeypatch)
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
        for card, family in (("CARD_C", "claude"), ("CARD_X", "codex")):
            conn.execute(
                "INSERT INTO tasks (task_id, status, card_json, created_at, updated_at) "
                "VALUES (?, 'review', ?, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')",
                (card, json.dumps({"claim_epoch": 0})),
            )
            assert callback_store.enqueue_callback(
                conn,
                card,
                "some-other-route-thread-id",
                "review_ready",
                provider=family,
                episode_id="0",
            )
        assert sorted(callback_store.pending_callback_providers(conn)) == ["claude", "codex"]
    finally:
        conn.close()

    assert manager_loop_service.start(tmp_path, "fake", "model-a")["ok"] is True

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


def test_a_turn_whose_only_errors_arose_after_delivery_acks_its_batch(
    monkeypatch: Any, tmp_path: Path
) -> None:
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
        assert ack_calls == [("b1", "l1")]
        status = manager_loop_service.status(tmp_path)
        assert status["wake"]["turns_this_hour"] == 1
        assert status["wake"]["failed_turns"] == 0
    finally:
        assert manager_loop_service.close(tmp_path)["ok"] is True
