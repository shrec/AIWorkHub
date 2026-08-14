"""Focused tests for the quality reviewer launch reservation boundary.

These tests pin the reconciliation of expired pid-null / process-false
``starting`` reservations, the bounded idempotent reviewer receipt, and the
invariant that a live provider is never classified by elapsed or quiet time.
"""

from __future__ import annotations

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
