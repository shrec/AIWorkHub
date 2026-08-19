"""Focused tests for the quality reviewer launch reservation boundary.

These tests pin the reconciliation of expired pid-null / process-false
``starting`` reservations, the bounded idempotent reviewer receipt, and the
invariant that a live provider is never classified by elapsed or quiet time.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from aiworkhub import process_launcher


RUNNER = "deepseek_v4-pro"
TOPIC = "quality_review"
ADAPTER = "vscode_lm"


def _manager(tmp_path: Path) -> process_launcher.ProcessManager:
    repo = tmp_path / "repo"
    repo.mkdir()
    return process_launcher.ProcessManager(
        repo=repo,
        process_log_path=tmp_path / "proc" / "process_events.jsonl",
        process_dir=tmp_path / "proc",
        isolation_enabled=False,
        collision_guard=lambda **_kwargs: {"returncode": 0},
    )


def _starting(
    *,
    request_id: str,
    task_id: str,
    expires_at: float,
    pid: int = 0,
    pid_start_ticks: int | None = None,
    quality_review_attempt: dict | None = None,
) -> dict[str, object]:
    event: dict[str, object] = {
        "request_id": request_id,
        "task_id": task_id,
        "runner": RUNNER,
        "topic": TOPIC,
        "adapter_id": ADAPTER,
        "state": "starting",
        "reservation_expires_at_epoch": expires_at,
    }
    if pid:
        event["pid"] = pid
        event["pid_start_ticks"] = pid_start_ticks
    if quality_review_attempt is not None:
        event["quality_review_attempt"] = quality_review_attempt
    return event


def _latest(manager: process_launcher.ProcessManager, request_id: str) -> dict:
    return manager._latest_by_request().get(request_id) or {}


def _starting_count(manager: process_launcher.ProcessManager, task_id: str) -> int:
    return sum(
        1
        for event in manager._events()
        if event.get("task_id") == task_id and event.get("state") == "starting"
    )


class _FakeWorkspace:
    def as_metadata(self) -> dict:
        return {
            "path": "/tmp/reviewer-ws",
            "home": "/tmp/reviewer-home",
            "repo": "/tmp/reviewer-repo",
            "request_id": "target-req",
        }


def _prepared_result(worker_adapter_id: str = "independent_adapter") -> dict:
    return {
        "ok": True,
        "prepared": {
            "worker_adapter_id": worker_adapter_id,
            "workspace": _FakeWorkspace(),
            "changed_hashes": {"candidate.py": "a" * 64},
            "packet": {
                "packet_sha256": "b" * 64,
                "target": {"claim_epoch": 1},
            },
        },
    }


REVIEW_READY_TARGET_TASK_IDS = ("TARGET_TASK", "TARGET_A", "TARGET_B")


@pytest.fixture(autouse=True)
def _review_ready_target(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give every launch in this module a target that CAN be reviewed.

    ``launch_quality_reviewer`` now refuses before the reservation when the
    target is not ``review_ready`` (NF-2026-00265/NF-2026-00331): the caller
    learns ``ok:false`` immediately instead of holding a success receipt for a
    reviewer that would resolve its target on the background thread and die.

    These tests pin the reservation boundary, which sits DOWNSTREAM of that
    check, and they never stubbed the target lookup because the old code never
    performed one.  On a fresh empty repo the lookup fails, the launch refuses,
    and the receipt carries no ``request_id`` -- so the fixtures, not the
    contract, are what needs to change.  Anything the tests deliberately drive
    through the refusal path overrides this by stubbing ``_show_task`` itself.
    """

    def _show_review_ready_target(_self, task_id: str) -> dict[str, object]:
        return {
            "returncode": 0,
            "stdout": json.dumps({
                "task_id": task_id,
                "terminal_review": {"substatus": "review_ready"},
            }),
            "stderr": "",
        }

    # ``_show_task`` is an INSTANCE attribute assigned in ``__init__``
    # (``show_task or self._default_show_task``), so the class-level default is
    # what a manager built without an explicit ``show_task`` falls back to.
    monkeypatch.setattr(
        process_launcher.ProcessManager,
        "_default_show_task",
        _show_review_ready_target,
    )


def _running_spy(
    manager: process_launcher.ProcessManager, launch_calls: list,
) -> object:
    """Simulate a successful provider spawn that records one running event."""

    def spy_launch(**kwargs):
        request_id = kwargs["reserved_request_id"]
        manager._append_event({
            "request_id": request_id,
            "task_id": kwargs["task_id"],
            "runner": kwargs["runner"],
            "topic": "quality_review",
            "adapter_id": kwargs["adapter_id"],
            "state": "running",
            "pid": os.getpid(),
            "pid_start_ticks": process_launcher._pid_start_ticks(os.getpid()),
        })
        launch_calls.append(kwargs)
        return {"ok": True, "request_id": request_id, "state": "running"}

    return spy_launch


def test_expired_pid_null_starting_reservation_is_truthfully_terminalized(tmp_path):
    manager = _manager(tmp_path)
    manager._append_event(
        _starting(
            request_id="review-req-1",
            task_id="REVIEWER_1",
            expires_at=time.time() - 1.0,
        )
    )

    assert manager._reconcile_expired_starting_reservations() == 1
    latest = _latest(manager, "review-req-1")
    assert latest["state"] == "blocked"
    assert latest["blocked_reason"] == "reservation_expired"
    # The expired reservation is no longer live: a retry must not return it.
    assert manager._live_reviewer_receipt("REVIEWER_1") is None


def test_unexpired_pid_null_reservation_is_never_stolen(tmp_path):
    manager = _manager(tmp_path)
    manager._append_event(
        _starting(
            request_id="review-req-2",
            task_id="REVIEWER_2",
            expires_at=time.time() + 120.0,
        )
    )

    assert manager._reconcile_expired_starting_reservations() == 0
    assert _latest(manager, "review-req-2")["state"] == "starting"
    receipt = manager._live_reviewer_receipt("REVIEWER_2")
    assert receipt is not None
    assert receipt["ok"] is True
    assert receipt["already_reserved"] is True
    assert receipt["request_id"] == "review-req-2"
    assert receipt["state"] == "starting"


def test_pid_bearing_starting_reservation_process_false_is_reconciled(tmp_path):
    manager = _manager(tmp_path)
    # A live pid with the wrong creation ticks is a proven identity mismatch.
    manager._append_event(
        _starting(
            request_id="review-req-3",
            task_id="REVIEWER_3",
            expires_at=time.time() + 120.0,
            pid=os.getpid(),
            pid_start_ticks=1,
        )
    )

    assert manager._reconcile_expired_starting_reservations() == 1
    latest = _latest(manager, "review-req-3")
    assert latest["state"] == "blocked"
    assert latest["blocked_reason"] == "reservation_process_false"


def test_live_pid_reservation_is_never_classified_by_elapsed_time(tmp_path):
    manager = _manager(tmp_path)
    start_ticks = process_launcher._pid_start_ticks(os.getpid())
    assert start_ticks is not None
    manager._append_event(
        _starting(
            request_id="review-req-4",
            task_id="REVIEWER_4",
            # Epoch already elapsed, but the process is provably alive.
            expires_at=time.time() - 1.0,
            pid=os.getpid(),
            pid_start_ticks=start_ticks,
        )
    )

    assert manager._reconcile_expired_starting_reservations() == 0
    assert _latest(manager, "review-req-4")["state"] == "starting"
    receipt = manager._live_reviewer_receipt("REVIEWER_4")
    assert receipt is not None
    assert receipt["already_reserved"] is True


def test_running_reviewer_returns_bounded_receipt(tmp_path):
    manager = _manager(tmp_path)
    start_ticks = process_launcher._pid_start_ticks(os.getpid())
    manager._append_event({
        "request_id": "review-req-5",
        "task_id": "REVIEWER_5",
        "runner": RUNNER,
        "topic": TOPIC,
        "adapter_id": ADAPTER,
        "state": "running",
        "pid": os.getpid(),
        "pid_start_ticks": start_ticks,
    })

    receipt = manager._live_reviewer_receipt("REVIEWER_5")
    assert receipt is not None
    assert receipt["request_id"] == "review-req-5"
    assert receipt["state"] == "running"
    assert receipt["pid"] == os.getpid()


def test_launch_reservation_atomically_reconciles_before_duplicate_check(tmp_path):
    manager = _manager(tmp_path)
    manager._append_event(
        _starting(
            request_id="review-req-6",
            task_id="REVIEWER_6",
            expires_at=time.time() - 1.0,
        )
    )

    # A retry of the same task must clear the expired reservation, not raise
    # duplicate_reserved_task, and then reserve exactly one fresh slot.
    with manager._launch_reservation({
        "request_id": "review-req-6-retry",
        "task_id": "REVIEWER_6",
        "runner": RUNNER,
        "topic": TOPIC,
        "adapter_id": ADAPTER,
    }):
        pass

    assert _latest(manager, "review-req-6")["state"] == "blocked"
    assert _latest(manager, "review-req-6-retry")["state"] == "starting"


def test_reconciliation_does_not_disturb_unrelated_requests(tmp_path):
    manager = _manager(tmp_path)
    manager._append_event(
        _starting(
            request_id="stale-req",
            task_id="STALE_TASK",
            expires_at=time.time() - 1.0,
        )
    )
    manager._append_event({
        "request_id": "other-req",
        "task_id": "OTHER_TASK",
        "runner": RUNNER,
        "topic": TOPIC,
        "adapter_id": ADAPTER,
        "state": "running",
        "pid": os.getpid(),
        "pid_start_ticks": process_launcher._pid_start_ticks(os.getpid()),
    })

    assert manager._reconcile_expired_starting_reservations() == 1
    assert _latest(manager, "stale-req")["state"] == "blocked"
    # Unrelated running request is untouched and remains live.
    assert _latest(manager, "other-req")["state"] == "running"
    assert manager._live_reviewer_receipt("OTHER_TASK") is not None


def test_launch_quality_reviewer_is_idempotent_for_live_reservation(
    tmp_path, monkeypatch,
):
    manager = _manager(tmp_path)
    manager._append_event(
        _starting(
            request_id="review-req-7",
            task_id="REVIEWER_7",
            expires_at=time.time() + 120.0,
        )
    )

    # The bounded early-return must not reach the (expensive) preparation step.
    monkeypatch.setattr(
        manager,
        "_prepared_quality_review",
        lambda *_args, **_kwargs: pytest.fail("must not prepare a duplicate reviewer"),
    )

    receipt = manager.launch_quality_reviewer(
        target_request_id="target-req",
        target_task_id="TARGET_TASK",
        reviewer_task_id="REVIEWER_7",
        runner=RUNNER,
        adapter_id=ADAPTER,
        lens="correctness",
    )

    assert receipt["ok"] is True
    assert receipt["already_reserved"] is True
    assert receipt["request_id"] == "review-req-7"
    assert _starting_count(manager, "REVIEWER_7") == 1


def test_launch_returns_bounded_receipt_while_preparation_blocks(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    monkeypatch.setattr(
        process_launcher.core, "create_task", lambda **_kwargs: {"ok": True},
    )

    prep_started = threading.Event()
    release = threading.Event()
    launch_calls: list = []

    def blocked_prep(*_args, **_kwargs):
        prep_started.set()
        assert release.wait(timeout=10)
        return _prepared_result()

    monkeypatch.setattr(manager, "_prepared_quality_review", blocked_prep)
    monkeypatch.setattr(manager, "_launch_isolated", _running_spy(manager, launch_calls))

    receipt_box: dict = {}
    handler = threading.Thread(
        target=lambda: receipt_box.update(
            receipt=manager.launch_quality_reviewer(
                target_request_id="target-req",
                target_task_id="TARGET_TASK",
                reviewer_task_id="REVIEWER_NEW",
                runner=RUNNER,
                adapter_id=ADAPTER,
                lens="correctness",
            )
        )
    )
    handler.start()
    handler.join(timeout=5)
    assert not handler.is_alive(), "handler must return before preparation finishes"

    receipt = receipt_box["receipt"]
    assert receipt["ok"] is True
    assert receipt["deferred"] is True
    assert receipt["already_reserved"] is False
    assert receipt["state"] == "starting"
    assert receipt["pid"] == 0
    request_id = receipt["request_id"]

    # Preparation is still blocked, yet unrelated status-style reads complete.
    assert prep_started.wait(timeout=5)
    assert manager._active_count() >= 1
    assert manager._live_reviewer_receipt("SOME_UNRELATED_TASK") is None

    release.set()
    deadline = time.time() + 10
    while not launch_calls and time.time() < deadline:
        time.sleep(0.01)
    assert len(launch_calls) == 1
    assert launch_calls[0]["reserved_request_id"] == request_id
    assert _latest(manager, request_id)["state"] == "running"


def test_preparation_failure_terminalizes_attempt_once(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    launch_calls: list = []
    monkeypatch.setattr(
        manager,
        "_prepared_quality_review",
        lambda *_args, **_kwargs: {
            "ok": False, "error": "quality_review_target_not_review_ready",
        },
    )

    def record_launch(**_kwargs):
        launch_calls.append(_kwargs)

    monkeypatch.setattr(manager, "_launch_isolated", record_launch)

    receipt = manager.launch_quality_reviewer(
        target_request_id="target-req",
        target_task_id="TARGET_TASK",
        reviewer_task_id="REVIEWER_FAIL",
        runner=RUNNER,
        adapter_id=ADAPTER,
        lens="correctness",
    )
    assert receipt["ok"] is True
    assert receipt["deferred"] is True
    request_id = receipt["request_id"]

    deadline = time.time() + 10
    while (
        _latest(manager, request_id).get("state") == "starting"
        and time.time() < deadline
    ):
        time.sleep(0.01)

    latest = _latest(manager, request_id)
    assert latest["state"] == "blocked"
    assert "preparation_failed" in str(latest.get("blocked_reason"))
    blocked = [
        event
        for event in manager._request_events(request_id)
        if event.get("state") == "blocked"
    ]
    assert len(blocked) == 1
    assert launch_calls == []
    assert manager._live_reviewer_receipt("REVIEWER_FAIL") is None


def test_lost_ack_retry_reconciles_same_attempt(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    monkeypatch.setattr(
        process_launcher.core, "create_task", lambda **_kwargs: {"ok": True},
    )

    prep_started = threading.Event()
    release = threading.Event()
    launch_calls: list = []

    def blocked_prep(*_args, **_kwargs):
        prep_started.set()
        assert release.wait(timeout=10)
        return _prepared_result()

    monkeypatch.setattr(manager, "_prepared_quality_review", blocked_prep)
    monkeypatch.setattr(manager, "_launch_isolated", _running_spy(manager, launch_calls))

    first = manager.launch_quality_reviewer(
        target_request_id="target-req",
        target_task_id="TARGET_TASK",
        reviewer_task_id="REVIEWER_LA",
        runner=RUNNER,
        adapter_id=ADAPTER,
        lens="correctness",
    )
    assert first["ok"] is True
    assert first["deferred"] is True
    assert prep_started.wait(timeout=5)

    # The client lost the ack and retries the exact same reviewer task.
    retry = manager.launch_quality_reviewer(
        target_request_id="target-req",
        target_task_id="TARGET_TASK",
        reviewer_task_id="REVIEWER_LA",
        runner=RUNNER,
        adapter_id=ADAPTER,
        lens="correctness",
    )
    assert retry["ok"] is True
    assert retry["already_reserved"] is True
    assert retry["request_id"] == first["request_id"]
    assert _starting_count(manager, "REVIEWER_LA") == 1

    release.set()
    deadline = time.time() + 10
    while not launch_calls and time.time() < deadline:
        time.sleep(0.01)
    assert len(launch_calls) == 1
    assert launch_calls[0]["reserved_request_id"] == first["request_id"]


def test_parallel_same_task_race_yields_single_provider(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    monkeypatch.setattr(
        process_launcher.core, "create_task", lambda **_kwargs: {"ok": True},
    )

    prep_started = threading.Event()
    release = threading.Event()
    launch_calls: list = []
    calls_lock = threading.Lock()

    def blocked_prep(*_args, **_kwargs):
        prep_started.set()
        assert release.wait(timeout=10)
        return _prepared_result()

    monkeypatch.setattr(manager, "_prepared_quality_review", blocked_prep)

    def spy_launch(**kwargs):
        request_id = kwargs["reserved_request_id"]
        manager._append_event({
            "request_id": request_id,
            "task_id": kwargs["task_id"],
            "runner": kwargs["runner"],
            "topic": "quality_review",
            "adapter_id": kwargs["adapter_id"],
            "state": "running",
            "pid": os.getpid(),
            "pid_start_ticks": process_launcher._pid_start_ticks(os.getpid()),
        })
        with calls_lock:
            launch_calls.append(kwargs)
        return {"ok": True, "request_id": request_id, "state": "running"}

    monkeypatch.setattr(manager, "_launch_isolated", spy_launch)

    results: list = []
    results_lock = threading.Lock()
    barrier = threading.Barrier(8)

    def call():
        barrier.wait(timeout=10)
        receipt = manager.launch_quality_reviewer(
            target_request_id="target-req",
            target_task_id="TARGET_TASK",
            reviewer_task_id="REVIEWER_RACE",
            runner=RUNNER,
            adapter_id=ADAPTER,
            lens="correctness",
        )
        with results_lock:
            results.append(receipt)

    threads = [threading.Thread(target=call) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    for thread in threads:
        assert not thread.is_alive()

    assert len(results) == 8
    request_ids = {r["request_id"] for r in results}
    assert len(request_ids) == 1
    deferred = [r for r in results if r.get("deferred")]
    already = [r for r in results if r.get("already_reserved")]
    assert len(deferred) == 1
    assert len(already) == 7

    release.set()
    deadline = time.time() + 10
    while not launch_calls and time.time() < deadline:
        time.sleep(0.01)
    assert len(launch_calls) == 1


def test_three_lenses_share_one_preparation_and_launch_distinctly(
    tmp_path, monkeypatch,
):
    manager = _manager(tmp_path)
    monkeypatch.setattr(
        process_launcher.core, "create_task", lambda **_kwargs: {"ok": True},
    )

    prep_calls: list = []
    prep_started = threading.Event()
    release = threading.Event()

    def blocking_build(*args, **_kwargs):
        prep_calls.append(args)
        prep_started.set()
        assert release.wait(timeout=10)
        return _prepared_result()

    monkeypatch.setattr(manager, "_build_quality_review_packet", blocking_build)

    launch_calls: list = []
    calls_lock = threading.Lock()

    def spy_launch(**kwargs):
        request_id = kwargs["reserved_request_id"]
        manager._append_event({
            "request_id": request_id,
            "task_id": kwargs["task_id"],
            "runner": kwargs["runner"],
            "topic": "quality_review",
            "adapter_id": kwargs["adapter_id"],
            "state": "running",
            "pid": os.getpid(),
            "pid_start_ticks": process_launcher._pid_start_ticks(os.getpid()),
        })
        with calls_lock:
            launch_calls.append(kwargs)
        return {"ok": True, "request_id": request_id, "state": "running"}

    monkeypatch.setattr(manager, "_launch_isolated", spy_launch)

    lenses = ["correctness", "security", "code_quality"]
    receipts = [
        manager.launch_quality_reviewer(
            target_request_id="target-req",
            target_task_id="TARGET_TASK",
            reviewer_task_id=f"REVIEWER_LENS_{idx}",
            runner=RUNNER,
            adapter_id=ADAPTER,
            lens=lens,
        )
        for idx, lens in enumerate(lenses)
    ]
    assert [r["ok"] for r in receipts] == [True, True, True]
    assert [r.get("deferred") for r in receipts] == [True, True, True]
    assert len({r["request_id"] for r in receipts}) == 3

    assert prep_started.wait(timeout=5)
    # While the elected owner is blocked, only one heavy build has been entered;
    # the other two reviewers are bounded waiters that reuse the owner result.
    time.sleep(0.2)
    assert len(prep_calls) == 1

    release.set()
    deadline = time.time() + 10
    while len(launch_calls) < 3 and time.time() < deadline:
        time.sleep(0.01)

    assert len(prep_calls) == 1
    with calls_lock:
        assert len(launch_calls) == 3
    assert {c["task_id"] for c in launch_calls} == {
        "REVIEWER_LENS_0",
        "REVIEWER_LENS_1",
        "REVIEWER_LENS_2",
    }
    assert {c["reserved_request_id"] for c in launch_calls} == {
        r["request_id"] for r in receipts
    }
    assert {
        c["quality_review_binding"]["lens"] for c in launch_calls
    } == set(lenses)


def test_progress_event_preserves_reservation_identity_and_phases(tmp_path):
    manager = _manager(tmp_path)
    expires_at = time.time() + 120.0
    manager._append_event(
        _starting(
            request_id="review-prog-1",
            task_id="REVIEWER_PROG",
            expires_at=expires_at,
        )
    )

    manager._publish_reviewer_progress("review-prog-1", "packet_build_started")
    manager._publish_reviewer_progress(
        "review-prog-1", "scope_audits_started", "heavy"
    )

    latest = _latest(manager, "review-prog-1")
    assert latest["state"] == "starting"
    assert latest["preparation_phase"] == "scope_audits_started"
    assert latest["preparation_detail"] == "heavy"
    assert latest["reservation_expires_at_epoch"] == pytest.approx(
        expires_at, rel=1e-9
    )
    assert latest["preparation_heartbeat_epoch"] > 0

    # Progress never steals a live pid-null reservation.
    assert manager._reconcile_expired_starting_reservations() == 0
    receipt = manager._live_reviewer_receipt("REVIEWER_PROG")
    assert receipt is not None
    assert receipt["already_reserved"] is True
    assert receipt["request_id"] == "review-prog-1"

    # Once terminalized, a stale owner publishes no further progress.
    manager._blocked(
        "REVIEWER_PROG", RUNNER, TOPIC, ADAPTER, "test_done",
        request_id="review-prog-1",
    )
    manager._publish_reviewer_progress("review-prog-1", "zombie_phase")
    terminal = _latest(manager, "review-prog-1")
    assert terminal["state"] == "blocked"
    assert terminal.get("preparation_phase") is None


def test_owner_preparation_timeout_terminalizes_each_reservation_once(
    tmp_path, monkeypatch,
):
    manager = _manager(tmp_path)
    manager._QUALITY_REVIEW_PREP_OWNER_SECONDS = 0.05
    monkeypatch.setattr(
        process_launcher.core, "create_task", lambda **_kwargs: {"ok": True},
    )

    never = threading.Event()

    def hanging_build(*_args, **_kwargs):
        never.wait(timeout=30)
        return _prepared_result()

    monkeypatch.setattr(manager, "_build_quality_review_packet", hanging_build)

    launch_calls: list = []

    def record_launch(**_kwargs):
        launch_calls.append(_kwargs)

    monkeypatch.setattr(manager, "_launch_isolated", record_launch)

    lenses = ["correctness", "security", "code_quality"]
    receipts = [
        manager.launch_quality_reviewer(
            target_request_id="target-req",
            target_task_id="TARGET_TASK",
            reviewer_task_id=f"REVIEWER_TO_{idx}",
            runner=RUNNER,
            adapter_id=ADAPTER,
            lens=lens,
        )
        for idx, lens in enumerate(lenses)
    ]
    assert [r["ok"] for r in receipts] == [True, True, True]
    request_ids = [r["request_id"] for r in receipts]
    assert len(set(request_ids)) == 3

    deadline = time.time() + 10
    while time.time() < deadline:
        states = [_latest(manager, rid).get("state") for rid in request_ids]
        if all(state == "blocked" for state in states):
            break
        time.sleep(0.01)

    for rid in request_ids:
        latest = _latest(manager, rid)
        assert latest["state"] == "blocked"
        assert "preparation_timeout" in str(latest.get("blocked_reason"))
        blocked = [
            e for e in manager._request_events(rid) if e.get("state") == "blocked"
        ]
        assert len(blocked) == 1

    assert launch_calls == []
    for idx in range(3):
        assert manager._live_reviewer_receipt(f"REVIEWER_TO_{idx}") is None
    never.set()


def test_status_read_bounded_and_reports_phase_while_prep_blocks(
    tmp_path, monkeypatch,
):
    manager = _manager(tmp_path)
    monkeypatch.setattr(
        process_launcher.core, "create_task", lambda **_kwargs: {"ok": True},
    )

    prep_started = threading.Event()
    release = threading.Event()

    def blocked_build(*_args, **kwargs):
        progress = kwargs.get("progress")
        if progress:
            progress("scope_audits_started")
        prep_started.set()
        assert release.wait(timeout=10)
        return _prepared_result()

    monkeypatch.setattr(manager, "_build_quality_review_packet", blocked_build)

    launch_calls: list = []
    monkeypatch.setattr(manager, "_launch_isolated", _running_spy(manager, launch_calls))

    receipt = manager.launch_quality_reviewer(
        target_request_id="target-req",
        target_task_id="TARGET_TASK",
        reviewer_task_id="REVIEWER_STATUS",
        runner=RUNNER,
        adapter_id=ADAPTER,
        lens="correctness",
    )
    request_id = receipt["request_id"]
    assert prep_started.wait(timeout=5)

    # The invariant under test belongs to ``status``, not to the launch: a
    # pid-null starting reservation must be described from the reservation
    # itself and never by reading the task card.  The launch now performs ONE
    # deliberate target lookup before reserving (it refuses a target that is not
    # ``review_ready`` rather than handing back a success receipt), so the trap
    # is armed AFTER the launch -- otherwise it fires on a read the contract
    # requires and says nothing about ``status`` at all.
    monkeypatch.setattr(
        manager,
        "_show_task",
        lambda *_a, **_k: pytest.fail(
            "status must not read the task card for a pid-null starting reservation"
        ),
    )

    status = manager.status(request_id)
    assert status["ok"] is True
    assert status["state"] == "starting"
    assert status["preparation_phase"] == "scope_audits_started"
    assert status.get("task_card") is None

    release.set()
    deadline = time.time() + 10
    while not launch_calls and time.time() < deadline:
        time.sleep(0.01)
    assert len(launch_calls) == 1


def test_launch_publishes_phased_progress_under_same_reservation(
    tmp_path, monkeypatch,
):
    manager = _manager(tmp_path)
    monkeypatch.setattr(
        process_launcher.core, "create_task", lambda **_kwargs: {"ok": True},
    )

    def phased_build(*_args, **kwargs):
        progress = kwargs.get("progress")
        for phase in (
            "packet_build_started",
            "scope_audits_started",
            "scope_audits_complete",
            "packet_built",
        ):
            if progress:
                progress(phase)
        return _prepared_result()

    monkeypatch.setattr(manager, "_build_quality_review_packet", phased_build)

    launch_calls: list = []
    monkeypatch.setattr(manager, "_launch_isolated", _running_spy(manager, launch_calls))

    receipt = manager.launch_quality_reviewer(
        target_request_id="target-req",
        target_task_id="TARGET_TASK",
        reviewer_task_id="REVIEWER_PHASE",
        runner=RUNNER,
        adapter_id=ADAPTER,
        lens="correctness",
    )
    request_id = receipt["request_id"]

    deadline = time.time() + 10
    while not launch_calls and time.time() < deadline:
        time.sleep(0.01)
    assert len(launch_calls) == 1

    progress_events = [
        e for e in manager._request_events(request_id)
        if e.get("state") == "starting" and e.get("preparation_phase")
    ]
    # RECORDED REASON for the moved sequence: the reviewer launch path now
    # resolves the independence rung between packet build and packet_prepared and
    # publishes it as "independence_rung_recorded" (with the rung as its detail)
    # under the same reservation, exactly like every sibling preparation
    # milestone.  This is a legitimate new phase, not a loosening -- the
    # assertion is still an exact, ordered equality; the single new phase is
    # inserted in its true position.
    assert [e["preparation_phase"] for e in progress_events] == [
        "packet_build_started",
        "scope_audits_started",
        "scope_audits_complete",
        "packet_built",
        "independence_rung_recorded",
        "packet_prepared",
        "reviewer_task_created",
        "isolated_launch_started",
    ]


def test_provider_spawn_transition_persists_request_task_packet_identity(tmp_path):
    manager = _manager(tmp_path)
    manager._append_event(
        _starting(
            request_id="review-cas-1",
            task_id="REVIEWER_CAS_1",
            expires_at=time.time() + 120.0,
            quality_review_attempt={
                "target_request_id": "target-req",
                "target_task_id": "TARGET_TASK",
                "lens": "correctness",
            },
        )
    )
    binding = {
        "target_request_id": "target-req",
        "target_task_id": "TARGET_TASK",
        "target_claim_epoch": 7,
        "adapter_id": ADAPTER,
        "source_workspace": {"path": "/tmp/reviewer-ws"},
        "candidate_paths": ["candidate.py"],
        "packet": {"packet_sha256": "c" * 64, "target": {"claim_epoch": 7}},
        "lens": "correctness",
    }

    assert manager._reviewer_spawn_transition(
        "review-cas-1", binding=binding
    ) is True

    committed = _latest(manager, "review-cas-1")
    assert committed["state"] == "provider_spawn_committed"
    assert committed["task_id"] == "REVIEWER_CAS_1"
    assert committed["quality_review_attempt"] == {
        "target_request_id": "target-req",
        "target_task_id": "TARGET_TASK",
        "lens": "correctness",
    }
    assert committed["packet"] == binding["packet"]
    assert committed["target_request_id"] == "target-req"
    assert committed["target_task_id"] == "TARGET_TASK"
    assert committed["target_claim_epoch"] == 7
    assert committed["owner_pid"] == os.getpid()
    assert committed["owner_pid_start_ticks"] == (
        process_launcher._pid_start_ticks(os.getpid())
    )
    # The packet binding is never rebound on re-observation of the committed phase.
    assert manager._reviewer_spawn_transition(
        "review-cas-1", binding={**binding, "target_claim_epoch": 99}
    ) is True
    assert _latest(manager, "review-cas-1")["target_claim_epoch"] == 7


def test_provider_identity_attach_cas_is_idempotent_for_identical_identity(tmp_path):
    manager = _manager(tmp_path)
    manager._append_event(
        _starting(
            request_id="review-cas-2",
            task_id="REVIEWER_CAS_2",
            expires_at=time.time() + 120.0,
        )
    )
    assert manager._reviewer_spawn_transition("review-cas-2", binding={}) is True

    ticks = process_launcher._pid_start_ticks(os.getpid())
    assert ticks is not None
    assert manager._reviewer_attach_provider_identity(
        "review-cas-2", pid=os.getpid(), pid_start_ticks=ticks
    ) is True

    committed = _latest(manager, "review-cas-2")
    assert committed["state"] == "provider_spawn_committed"
    assert committed["provider_pid"] == os.getpid()
    assert committed["provider_pid_start_ticks"] == ticks

    events_before = len(manager._request_events("review-cas-2"))
    # Identical (pid, pid_start_ticks) reattachment is idempotent: no new event.
    assert manager._reviewer_attach_provider_identity(
        "review-cas-2", pid=os.getpid(), pid_start_ticks=ticks
    ) is True
    assert len(manager._request_events("review-cas-2")) == events_before
    assert _latest(manager, "review-cas-2")["provider_pid"] == os.getpid()


def test_provider_identity_attach_cas_loses_for_different_identity(tmp_path):
    manager = _manager(tmp_path)
    manager._append_event(
        _starting(
            request_id="review-cas-3",
            task_id="REVIEWER_CAS_3",
            expires_at=time.time() + 120.0,
        )
    )
    assert manager._reviewer_spawn_transition("review-cas-3", binding={}) is True

    ticks = process_launcher._pid_start_ticks(os.getpid())
    assert ticks is not None
    assert manager._reviewer_attach_provider_identity(
        "review-cas-3", pid=os.getpid(), pid_start_ticks=ticks
    ) is True

    # A different PID identity loses the CAS and the winner stays attached.
    assert manager._reviewer_attach_provider_identity(
        "review-cas-3", pid=os.getpid() + 1, pid_start_ticks=ticks
    ) is False
    # The same PID with different start ticks is also a different identity.
    assert manager._reviewer_attach_provider_identity(
        "review-cas-3", pid=os.getpid(), pid_start_ticks=ticks + 1
    ) is False

    committed = _latest(manager, "review-cas-3")
    assert committed["provider_pid"] == os.getpid()
    assert committed["provider_pid_start_ticks"] == ticks


def test_provider_spawn_committed_live_provider_is_never_terminalized(tmp_path):
    manager = _manager(tmp_path)
    # The bounded owner is proven dead, but the provider process is live with
    # exact identity: it must never be terminalized by elapsed/quiet time.
    manager._append_event({
        "request_id": "review-live-1",
        "task_id": "REVIEWER_LIVE_1",
        "runner": RUNNER,
        "topic": TOPIC,
        "adapter_id": ADAPTER,
        "state": "provider_spawn_committed",
        "reservation_expires_at_epoch": time.time() - 1.0,
        "owner_pid": os.getpid(),
        "owner_pid_start_ticks": 1,
        "provider_pid": os.getpid(),
        "provider_pid_start_ticks": process_launcher._pid_start_ticks(os.getpid()),
    })

    assert manager._reconcile_expired_starting_reservations() == 0
    assert _latest(manager, "review-live-1")["state"] == "provider_spawn_committed"
    receipt = manager._live_reviewer_receipt("REVIEWER_LIVE_1")
    assert receipt is not None
    assert receipt["already_reserved"] is True
    assert receipt["request_id"] == "review-live-1"
    assert "review-live-1" in manager._active_request_ids()


def test_provider_spawn_committed_dead_provider_is_terminalized_once(tmp_path):
    manager = _manager(tmp_path)
    # Both the owner and provider identities are proven mismatches: the
    # reservation is terminalized exactly once with a truthful reason.
    manager._append_event({
        "request_id": "review-dead-1",
        "task_id": "REVIEWER_DEAD_1",
        "runner": RUNNER,
        "topic": TOPIC,
        "adapter_id": ADAPTER,
        "state": "provider_spawn_committed",
        "reservation_expires_at_epoch": time.time() + 120.0,
        "owner_pid": os.getpid(),
        "owner_pid_start_ticks": 1,
        "provider_pid": os.getpid(),
        "provider_pid_start_ticks": 1,
    })

    assert manager._reconcile_expired_starting_reservations() == 1
    latest = _latest(manager, "review-dead-1")
    assert latest["state"] == "blocked"
    assert latest["blocked_reason"] == "provider_spawn_committed_provider_dead"
    assert manager._reconcile_expired_starting_reservations() == 0
    assert manager._live_reviewer_receipt("REVIEWER_DEAD_1") is None


def test_claude_cli_and_vscode_lm_share_same_reservation_spawn_truth(tmp_path):
    manager = _manager(tmp_path)
    packet = {"packet_sha256": "d" * 64, "target": {"claim_epoch": 3}}
    attempt = {
        "target_request_id": "target-req",
        "target_task_id": "TARGET_TASK",
        "lens": "correctness",
    }

    # Both reviewer adapter paths flow through the identical reservation + spawn
    # CAS, preserving their own adapter_id and the exact packet binding.
    for request_id, task_id, adapter in (
        ("review-claude-1", "REVIEWER_CLAUDE", "claude_cli"),
        ("review-vscode-1", "REVIEWER_VSCODE", "vscode_lm"),
    ):
        manager._append_event({
            "request_id": request_id,
            "task_id": task_id,
            "runner": RUNNER,
            "topic": TOPIC,
            "adapter_id": adapter,
            "state": "starting",
            "reservation_expires_at_epoch": time.time() + 120.0,
            "quality_review_attempt": attempt,
        })
        assert manager._reviewer_spawn_transition(request_id, binding={
            "target_request_id": "target-req",
            "target_task_id": "TARGET_TASK",
            "target_claim_epoch": 3,
            "packet": packet,
            "lens": "correctness",
        }) is True
        committed = _latest(manager, request_id)
        assert committed["adapter_id"] == adapter
        assert committed["packet"] == packet
        assert committed["quality_review_attempt"] == attempt

    ticks = process_launcher._pid_start_ticks(os.getpid())
    assert ticks is not None
    for request_id in ("review-claude-1", "review-vscode-1"):
        assert manager._reviewer_attach_provider_identity(
            request_id, pid=os.getpid(), pid_start_ticks=ticks
        ) is True
        assert _latest(manager, request_id)["provider_pid"] == os.getpid()

    # A cross-adapter retry of the exact same reviewer task reconciles the same
    # reservation (task identity, not adapter, is the shared truth).
    manager._append_event(
        _starting(
            request_id="review-shared-1",
            task_id="REVIEWER_SHARED",
            expires_at=time.time() + 120.0,
        )
    )
    receipt = manager.launch_quality_reviewer(
        target_request_id="target-req",
        target_task_id="TARGET_TASK",
        reviewer_task_id="REVIEWER_SHARED",
        runner=RUNNER,
        adapter_id="claude_cli",
        lens="correctness",
    )
    assert receipt["ok"] is True
    assert receipt["already_reserved"] is True
    assert receipt["request_id"] == "review-shared-1"
    assert _starting_count(manager, "REVIEWER_SHARED") == 1
