"""Focused tests for the quality reviewer launch reservation boundary.

These tests pin the reconciliation of expired pid-null / process-false
``starting`` reservations, the bounded idempotent reviewer receipt, and the
invariant that a live provider is never classified by elapsed or quiet time.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from aiworkhub import process_launcher


RUNNER = "deepseek_v4-pro"
TOPIC = "quality_review"
ADAPTER = "vscode_lm"


def _manager(tmp_path: Path) -> process_launcher.ProcessManager:
    # ``exist_ok`` so a test can build a SECOND manager over the same durable
    # process directory -- the exact shape of a crash-and-restart.
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
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
    event["quality_review_attempt"] = quality_review_attempt or {
        "target_request_id": "target-req",
        "target_task_id": "TARGET_TASK",
        "lens": "correctness",
    }
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

    reviewer_cards: dict[str, dict[str, object]] = {}

    def _show_review_ready_target(_self, task_id: str) -> dict[str, object]:
        reviewer = reviewer_cards.get(task_id)
        if reviewer is not None:
            return {
                "returncode": 0,
                "stdout": json.dumps(reviewer),
                "stderr": "",
            }
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

    # Reservation tests isolate the process-ledger boundary. The production
    # launch now durably creates and claims the reviewer card before returning
    # its acknowledgement, so provide the mechanically valid task-engine
    # receipts that this focused fixture previously never needed.
    def _create(**kwargs: object) -> dict[str, object]:
        task_id = str(kwargs["task_id"])
        reviewer_cards[task_id] = {
            "task_id": task_id,
            "runner": kwargs["runner"],
            "topic": kwargs["topic"],
            "read_only": kwargs.get("read_only") is True,
            "allowed_writes": list(kwargs.get("allowed_writes") or []),
            "status": "pending",
            "worker_status": "unclaimed",
        }
        return {
            "ok": True,
            "created": True,
            "task_id": task_id,
        }

    monkeypatch.setattr(process_launcher.core, "create_task", _create)

    def _claim(
        _repo: Path,
        task_id: str,
        runner: str,
        topic: str,
        *,
        request_id: str,
    ) -> dict[str, object]:
        existing = reviewer_cards.get(task_id) or {}
        card = {
            "task_id": task_id,
            "runner": runner,
            "topic": topic,
            "launch_request_id": request_id,
            "claim_epoch": 1,
            "status": "processing",
            "worker_status": "claimed",
            "claimed_by": runner,
            "read_only": existing.get("read_only") is True,
            "allowed_writes": list(existing.get("allowed_writes") or []),
        }
        reviewer_cards[task_id] = card
        return {
            "ok": True,
            "returncode": 0,
            "stdout": json.dumps(card),
            "stderr": "",
        }

    monkeypatch.setattr(
        process_launcher.task_engine,
        "claim_start_exact",
        _claim,
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
    manager._QUALITY_REVIEW_PREP_WAIT_SECONDS = 0.05

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
    deadline = time.time() + 10
    while time.time() < deadline:
        flights = manager.__dict__.get("_quality_review_flights", {})
        active_builders = type(manager)._QUALITY_REVIEW_PREP_ACTIVE_BUILDERS
        if flights == {} and active_builders == 0:
            break
        time.sleep(0.01)

    assert manager.__dict__.get("_quality_review_flights", {}) == {}
    assert type(manager)._QUALITY_REVIEW_PREP_ACTIVE_BUILDERS == 0


def test_status_read_bounded_and_reports_phase_while_prep_blocks(
    tmp_path, monkeypatch,
):
    manager = _manager(tmp_path)

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
    # reservation is terminalized exactly once with a truthful reason.  The
    # committed phase carries the reviewer claim epoch that
    # ``_reviewer_spawn_transition`` always records, which is what makes the
    # terminal intent bindable and therefore the ledger event legal.
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
        "reviewer_claim_epoch": 1,
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


# ---------------------------------------------------------------------------
# NF-2026-00401: crash-recoverable terminal intent for owner/provider-dead
# committed reviewer reservations.
#
# Terminalizing such a reservation spans TWO independent durable stores: the
# process ledger and the task store.  The registry lock deliberately covers
# only the first, so the pair needs its own recovery story rather than a wider
# lock.  These tests pin that story: intent is durable before the ledger event,
# settlement happens with the lock released, the transition is bound to the
# exact task/request/claim epoch, and nothing that is merely *unknown* is ever
# terminalized or signalled.
# ---------------------------------------------------------------------------


def _committed(
    *,
    request_id: str,
    task_id: str,
    reviewer_claim_epoch: int | None = 7,
    owner_pid: int | None = None,
    owner_pid_start_ticks: object = 1,
    provider_pid: int | None = None,
    provider_pid_start_ticks: object = 1,
    expires_at: float | None = None,
) -> dict[str, object]:
    """Build a ``provider_spawn_committed`` reservation event.

    Defaults describe a committed attempt whose owner identity is a proven
    MISMATCH (a live pid with the wrong creation ticks) and whose provider was
    never attached -- the owner-dead shape.  Pass ``provider_pid`` to describe
    the provider-dead shape instead.
    """

    event: dict[str, object] = {
        "request_id": request_id,
        "task_id": task_id,
        "runner": RUNNER,
        "topic": TOPIC,
        "adapter_id": ADAPTER,
        "state": "provider_spawn_committed",
        "reservation_expires_at_epoch": (
            time.time() + 120.0 if expires_at is None else expires_at
        ),
        "owner_pid": os.getpid() if owner_pid is None else owner_pid,
        "owner_pid_start_ticks": owner_pid_start_ticks,
    }
    if provider_pid is not None:
        event["provider_pid"] = provider_pid
        event["provider_pid_start_ticks"] = provider_pid_start_ticks
    if reviewer_claim_epoch is not None:
        event["reviewer_claim_epoch"] = reviewer_claim_epoch
    return event


class _TerminalSpy:
    """Stand in for ``task_store.mark_terminal_failure`` and record identity."""

    def __init__(self, result: tuple[bool, str] = (True, "blocked")) -> None:
        self.calls: list[dict] = []
        self.result = result
        self.entered = threading.Event()
        self.gate: threading.Event | None = None

    def __call__(self, root, task_id, **kwargs):
        self.calls.append({"root": root, "task_id": task_id, **kwargs})
        self.entered.set()
        if self.gate is not None:
            assert self.gate.wait(timeout=15)
        return self.result


def _capture_callbacks(monkeypatch) -> list[dict]:
    """Capture the store's single terminal-callback authority.

    Settlement owns no private wrapper around it, so this is the one seam a
    test has to watch to know exactly what the manager was signalled about.
    """

    calls: list[dict] = []

    def record(root, task_id, **kwargs) -> bool:
        calls.append({"root": root, "task_id": task_id, **kwargs})
        return True

    monkeypatch.setattr(
        process_launcher.task_store, "enqueue_terminal_callback", record
    )
    return calls


def _identity_spy(monkeypatch) -> list[tuple]:
    """Record every PID identity evaluation without changing its verdict."""

    evaluated: list[tuple] = []
    exact = process_launcher._pid_identity_evidence

    def observed(pid, expected_start_ticks):
        evidence = exact(pid, expected_start_ticks)
        evaluated.append((pid, expected_start_ticks, evidence.verdict))
        return evidence

    monkeypatch.setattr(process_launcher, "_pid_identity_evidence", observed)
    return evaluated


def test_provider_dead_reservation_settles_terminal_intent_exactly_once(
    tmp_path, monkeypatch,
):
    manager = _manager(tmp_path)
    manager._append_event(_committed(
        request_id="review-intent-1",
        task_id="REVIEWER_INTENT_1",
        reviewer_claim_epoch=4,
        provider_pid=os.getpid(),
        provider_pid_start_ticks=1,
    ))

    assert manager._reconcile_expired_starting_reservations() == 1
    latest = _latest(manager, "review-intent-1")
    assert latest["state"] == "blocked"
    assert latest["blocked_reason"] == "provider_spawn_committed_provider_dead"
    assert latest["terminal_intent"] == "recorded"

    intent_path = manager._reviewer_terminal_intent_path("review-intent-1")
    assert intent_path.is_file()
    payload = json.loads(intent_path.read_text(encoding="utf-8"))
    assert payload["schema_id"] == process_launcher.REVIEWER_TERMINAL_INTENT_SCHEMA_ID
    assert payload["task_id"] == "REVIEWER_INTENT_1"
    assert payload["request_id"] == "review-intent-1"
    assert payload["reviewer_claim_epoch"] == 4
    assert payload["runner"] == RUNNER

    spy = _TerminalSpy()
    monkeypatch.setattr(process_launcher.task_store, "mark_terminal_failure", spy)
    callbacks = _capture_callbacks(monkeypatch)

    assert manager._settle_reviewer_terminal_intents() == 1
    assert len(spy.calls) == 1
    call = spy.calls[0]
    assert call["task_id"] == "REVIEWER_INTENT_1"
    assert call["request_id"] == "review-intent-1"
    assert call["claim_epoch"] == 4
    assert call["runner"] == RUNNER
    assert call["substatus"] == "liveness_lost"
    assert not intent_path.exists()
    assert len(callbacks) == 1

    # Repeated settlement, and a repeated reconcile of the already-terminal
    # reservation, are both no-ops: exactly one transition, one callback.
    assert manager._settle_reviewer_terminal_intents() == 0
    assert manager._reconcile_expired_starting_reservations() == 0
    assert len(spy.calls) == 1
    assert len(callbacks) == 1


def test_concurrent_settlement_emits_one_terminal_callback(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    manager._append_event(_committed(
        request_id="review-conc-1",
        task_id="REVIEWER_CONC_1",
        reviewer_claim_epoch=2,
        provider_pid=os.getpid(),
        provider_pid_start_ticks=1,
    ))
    assert manager._reconcile_expired_starting_reservations() == 1

    spy = _TerminalSpy()
    monkeypatch.setattr(process_launcher.task_store, "mark_terminal_failure", spy)
    callbacks = _capture_callbacks(monkeypatch)

    barrier = threading.Barrier(6)
    settled: list[int] = []
    settled_lock = threading.Lock()

    def settle() -> None:
        barrier.wait(timeout=10)
        count = manager._settle_reviewer_terminal_intents()
        with settled_lock:
            settled.append(count)

    threads = [threading.Thread(target=settle) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
    for thread in threads:
        assert not thread.is_alive()

    assert sum(settled) == 1
    assert len(spy.calls) == 1
    assert len(callbacks) == 1


def test_terminal_intent_survives_crash_before_task_store_transition(
    tmp_path, monkeypatch,
):
    manager = _manager(tmp_path)
    manager._append_event(_committed(
        request_id="review-crash-1",
        task_id="REVIEWER_CRASH_1",
        reviewer_claim_epoch=11,
        provider_pid=os.getpid(),
        provider_pid_start_ticks=1,
    ))

    def crash(*_args, **_kwargs):
        raise RuntimeError("process died between the two durable stores")

    monkeypatch.setattr(process_launcher.task_store, "mark_terminal_failure", crash)
    assert manager._reconcile_expired_starting_reservations() == 1
    with pytest.raises(RuntimeError):
        manager._settle_reviewer_terminal_intents()

    intent_path = manager._reviewer_terminal_intent_path("review-crash-1")
    assert intent_path.is_file(), "the intent must outlive the failed transition"

    # Restart: a fresh ProcessManager over the same durable process directory
    # finishes the exact same transition instead of stranding the claim.
    spy = _TerminalSpy()
    monkeypatch.setattr(process_launcher.task_store, "mark_terminal_failure", spy)
    recovered = _manager(tmp_path)
    callbacks = _capture_callbacks(monkeypatch)

    assert recovered._settle_reviewer_terminal_intents() == 1
    assert spy.calls[0]["task_id"] == "REVIEWER_CRASH_1"
    assert spy.calls[0]["request_id"] == "review-crash-1"
    assert spy.calls[0]["claim_epoch"] == 11
    assert len(callbacks) == 1
    assert not intent_path.exists()


def test_recycled_claim_epoch_is_never_terminalized(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    manager._append_event(_committed(
        request_id="review-recycled-1",
        task_id="REVIEWER_RECYCLED_1",
        reviewer_claim_epoch=9,
    ))

    assert manager._reconcile_expired_starting_reservations() == 1
    latest = _latest(manager, "review-recycled-1")
    assert latest["blocked_reason"] == "provider_spawn_committed_owner_dead"
    assert latest["terminal_intent"] == "recorded"

    # The card was released and re-claimed while the intent was pending: the
    # store refuses the mismatched epoch and no callback is ever emitted.
    spy = _TerminalSpy(result=(False, "claim_epoch_mismatch:expected=9:current=10"))
    monkeypatch.setattr(process_launcher.task_store, "mark_terminal_failure", spy)
    # The refusal STRING never proves the transition did not land -- the same
    # refusal is what a settler meets after its own transition when the card
    # was re-claimed underneath it.  The card is the authority, and this one
    # records episode 10's outcome rather than this settler's episode 9, which
    # is exactly what a recycled epoch means.
    monkeypatch.setattr(
        process_launcher.task_store,
        "get_task",
        lambda *_args, **_kwargs: {
            "claim_epoch": 10,
            "terminal_failure": {
                "runner": RUNNER,
                "substatus": "liveness_lost",
                "claim_epoch": 10,
                "evidence": {"request_id": "review-recycled-1"},
            },
        },
    )
    callbacks = _capture_callbacks(monkeypatch)

    assert manager._settle_reviewer_terminal_intents() == 0
    assert spy.calls[0]["claim_epoch"] == 9
    assert callbacks == []
    # A mismatch against a card that records somebody ELSE's episode is final
    # for this claim, so the spent intent is retired rather than retried
    # forever against a card it may never legally move.
    assert not manager._reviewer_terminal_intent_path("review-recycled-1").exists()


def test_transient_store_failure_keeps_terminal_intent_for_retry(
    tmp_path, monkeypatch,
):
    manager = _manager(tmp_path)
    manager._append_event(_committed(
        request_id="review-retry-1",
        task_id="REVIEWER_RETRY_1",
        reviewer_claim_epoch=3,
    ))
    assert manager._reconcile_expired_starting_reservations() == 1

    spy = _TerminalSpy(result=(False, "terminal_failure_transition_conflict"))
    monkeypatch.setattr(process_launcher.task_store, "mark_terminal_failure", spy)
    callbacks = _capture_callbacks(monkeypatch)

    intent_path = manager._reviewer_terminal_intent_path("review-retry-1")
    assert manager._settle_reviewer_terminal_intents() == 0
    assert callbacks == []
    assert intent_path.is_file(), "a transient conflict must not drop the intent"

    spy.result = (True, "blocked")
    assert manager._settle_reviewer_terminal_intents() == 1
    assert len(callbacks) == 1
    assert not intent_path.exists()


def test_unknown_pid_identity_is_never_terminalized_or_signalled(
    tmp_path, monkeypatch,
):
    manager = _manager(tmp_path)
    # A real pid with no recorded creation ticks yields UNKNOWN identity
    # evidence, and the reservation epoch has already elapsed.  Neither fact is
    # terminal authority.
    manager._append_event(_committed(
        request_id="review-unknown-1",
        task_id="REVIEWER_UNKNOWN_1",
        reviewer_claim_epoch=5,
        provider_pid=os.getpid(),
        provider_pid_start_ticks=None,
        expires_at=time.time() - 1.0,
    ))

    evaluated = _identity_spy(monkeypatch)
    signals: list[int] = []
    exact_kill = os.kill

    def guarded_kill(pid, sig):
        signals.append(sig)
        assert sig == 0, "reconciliation must never signal a reviewer process"
        return exact_kill(pid, sig)

    monkeypatch.setattr(process_launcher.os, "kill", guarded_kill)

    assert manager._reconcile_expired_starting_reservations() == 0
    assert (
        os.getpid(),
        None,
        process_launcher.PidIdentityVerdict.UNKNOWN,
    ) in evaluated, "the provider pid must reach PID identity evaluation"
    assert _latest(manager, "review-unknown-1")["state"] == "provider_spawn_committed"
    assert not manager._reviewer_terminal_intent_path("review-unknown-1").exists()
    assert all(sig == 0 for sig in signals)


def test_unrelated_live_reviewer_is_never_terminalized(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    ticks = process_launcher._pid_start_ticks(os.getpid())
    assert ticks is not None
    manager._append_event(_committed(
        request_id="review-live-2",
        task_id="REVIEWER_LIVE_2",
        reviewer_claim_epoch=3,
        provider_pid=os.getpid(),
        provider_pid_start_ticks=ticks,
        expires_at=time.time() - 1.0,
    ))
    manager._append_event(_committed(
        request_id="review-dead-2",
        task_id="REVIEWER_DEAD_2",
        reviewer_claim_epoch=8,
        provider_pid=os.getpid(),
        provider_pid_start_ticks=1,
    ))

    evaluated = _identity_spy(monkeypatch)

    assert manager._reconcile_expired_starting_reservations() == 1
    assert (
        os.getpid(),
        ticks,
        process_launcher.PidIdentityVerdict.MATCH,
    ) in evaluated, "the live reviewer must reach PID identity evaluation"
    assert _latest(manager, "review-live-2")["state"] == "provider_spawn_committed"
    assert not manager._reviewer_terminal_intent_path("review-live-2").exists()
    assert manager._reviewer_terminal_intent_path("review-dead-2").is_file()
    receipt = manager._live_reviewer_receipt("REVIEWER_LIVE_2")
    assert receipt is not None
    assert receipt["request_id"] == "review-live-2"


def test_missing_reviewer_claim_epoch_terminalizes_nothing(tmp_path):
    # Without a claim epoch the intent is unbindable, so nothing durable can
    # ever finish the task-store half.  Appending the ``blocked`` event anyway
    # would release the reservation and strand the reviewer card in
    # ``processing`` forever, so the committed row is left exactly as it is.
    manager = _manager(tmp_path)
    manager._append_event(_committed(
        request_id="review-noepoch-1",
        task_id="REVIEWER_NOEPOCH_1",
        reviewer_claim_epoch=None,
        provider_pid=os.getpid(),
        provider_pid_start_ticks=1,
    ))

    assert manager._reconcile_expired_starting_reservations() == 0
    assert _latest(manager, "review-noepoch-1")["state"] == "provider_spawn_committed"
    assert not any(
        event.get("request_id") == "review-noepoch-1"
        and event.get("state") == "blocked"
        for event in manager._events()
    )
    assert not manager._reviewer_terminal_intent_path("review-noepoch-1").exists()
    assert manager._settle_reviewer_terminal_intents() == 0


def test_unwritable_terminal_intent_defers_the_ledger_event_for_retry(
    tmp_path, monkeypatch,
):
    # A failed intent write must leave the committed row intact: that row is
    # the only thing that brings the next reconcile pass back to this
    # reservation, so terminalizing the ledger here would lose the transition
    # permanently rather than deferring it.
    manager = _manager(tmp_path)
    manager._append_event(_committed(
        request_id="review-unwritable-1",
        task_id="REVIEWER_UNWRITABLE_1",
        reviewer_claim_epoch=4,
        provider_pid=os.getpid(),
        provider_pid_start_ticks=1,
    ))

    def refuse(*_args, **_kwargs):
        raise OSError("intent store is full")

    monkeypatch.setattr(process_launcher, "write_json_0600", refuse)

    assert manager._reconcile_expired_starting_reservations() == 0
    assert _latest(manager, "review-unwritable-1")["state"] == "provider_spawn_committed"
    assert not manager._reviewer_terminal_intent_path("review-unwritable-1").exists()

    # The disk recovers and the very next pass completes the same transition.
    monkeypatch.undo()
    assert manager._reconcile_expired_starting_reservations() == 1
    latest = _latest(manager, "review-unwritable-1")
    assert latest["state"] == "blocked"
    assert latest["terminal_intent"] == "recorded"
    assert manager._reviewer_terminal_intent_path("review-unwritable-1").is_file()


def test_provider_identity_attachment_preserves_the_reviewer_claim_epoch(tmp_path):
    # Attaching the provider PID re-states the committed phase.  If the epoch
    # did not travel with it, every reservation that actually spawned a
    # provider would become unbindable and could never be recovered.
    manager = _manager(tmp_path)
    manager._append_event(
        _starting(
            request_id="review-attach-1",
            task_id="REVIEWER_ATTACH_1",
            expires_at=time.time() + 120.0,
        )
    )
    assert manager._reviewer_spawn_transition(
        "review-attach-1", binding={}, reviewer_claim_epoch=15
    ) is True
    assert manager._reviewer_attach_provider_identity(
        "review-attach-1", pid=os.getpid(), pid_start_ticks=1
    ) is True

    attached = _latest(manager, "review-attach-1")
    assert attached["provider_pid"] == os.getpid()
    assert attached["reviewer_claim_epoch"] == 15

    assert manager._reconcile_expired_starting_reservations() == 1
    payload = json.loads(
        manager._reviewer_terminal_intent_path("review-attach-1").read_text(
            encoding="utf-8"
        )
    )
    assert payload["reviewer_claim_epoch"] == 15


def test_settlement_failure_never_masks_the_launch_outcome(tmp_path, monkeypatch):
    # Settlement runs from a bare ``finally``.  A store failure there belongs
    # to a wholly unrelated dead reservation and must not replace the caller's
    # own LaunchRejected -- or its successful receipt -- with that error.
    manager = _manager(tmp_path)
    manager._append_event(_committed(
        request_id="review-mask-1",
        task_id="REVIEWER_MASK_1",
        reviewer_claim_epoch=2,
        provider_pid=os.getpid(),
        provider_pid_start_ticks=1,
    ))
    assert manager._reconcile_expired_starting_reservations() == 1

    def explode(*_args, **_kwargs):
        # Not a store/filesystem/lock failure, so the per-intent containment
        # does not absorb it: only the contained wrapper stands between this
        # and the caller.
        raise RuntimeError("the task store adapter blew up")

    monkeypatch.setattr(process_launcher.task_store, "mark_terminal_failure", explode)

    with pytest.raises(RuntimeError):
        manager._settle_reviewer_terminal_intents()
    assert manager._settle_reviewer_terminal_intents_contained() == 0

    # The successful launch receipt survives the failing settlement...
    with manager._launch_reservation({
        "request_id": "mask-ok-req",
        "task_id": "MASK_OK_TASK",
        "runner": RUNNER,
        "topic": TOPIC,
        "adapter_id": ADAPTER,
    }):
        pass
    assert _latest(manager, "mask-ok-req")["state"] == "starting"

    # ...and so does a genuine rejection, which must not become an OSError.
    monkeypatch.setattr(process_launcher, "_configured_limit", lambda: 1)
    with pytest.raises(process_launcher.LaunchRejected) as rejected:
        with manager._launch_reservation({
            "request_id": "mask-reject-req",
            "task_id": "MASK_REJECT_TASK",
            "runner": RUNNER,
            "topic": TOPIC,
            "adapter_id": ADAPTER,
        }):
            pass
    assert "concurrency_limit_reached" in str(rejected.value)

    # The intent outlived every failure and is still there to be settled.
    assert manager._reviewer_terminal_intent_path("review-mask-1").is_file()


@pytest.mark.parametrize(
    "failure",
    [
        process_launcher.task_store.TaskStoreError("database is locked"),
        OSError("the task store volume went away"),
        sqlite3.OperationalError("database is locked"),
    ],
    ids=["task_store_error", "oserror", "sqlite_locked"],
)
def test_settlement_store_failure_keeps_intent_and_emits_no_callback(
    tmp_path, monkeypatch, failure,
):
    # A store that is briefly unavailable is not evidence about the claim.
    # Nothing is signalled and the intent survives for the next pass, so the
    # reviewer card is never left in ``processing`` with the transition lost.
    manager = _manager(tmp_path)
    manager._append_event(_committed(
        request_id="review-storeerr-1",
        task_id="REVIEWER_STOREERR_1",
        reviewer_claim_epoch=6,
    ))
    assert manager._reconcile_expired_starting_reservations() == 1

    def unavailable(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(
        process_launcher.task_store, "mark_terminal_failure", unavailable
    )
    callbacks = _capture_callbacks(monkeypatch)

    assert manager._settle_reviewer_terminal_intents() == 0
    assert callbacks == []
    assert manager._reviewer_terminal_intent_path("review-storeerr-1").is_file()

    # Once the store recovers, that exact transition still completes.
    monkeypatch.setattr(
        process_launcher.task_store, "mark_terminal_failure", _TerminalSpy()
    )
    assert manager._settle_reviewer_terminal_intents() == 1
    assert len(callbacks) == 1
    assert not manager._reviewer_terminal_intent_path("review-storeerr-1").exists()


def test_one_locked_card_still_settles_every_other_intent(tmp_path, monkeypatch):
    # A single contended card is not a reason to abandon the reservations
    # queued beside it.  Containment is per intent, so the locked one is kept
    # for the next pass while the rest of the pass completes normally.
    manager = _manager(tmp_path)
    for request_id, task_id, epoch in (
        ("review-locked-1", "REVIEWER_LOCKED_1", 4),
        ("review-open-1", "REVIEWER_OPEN_1", 5),
    ):
        manager._append_event(_committed(
            request_id=request_id, task_id=task_id, reviewer_claim_epoch=epoch,
        ))
    assert manager._reconcile_expired_starting_reservations() == 2

    locked = {"REVIEWER_LOCKED_1"}
    attempted: list[str] = []

    def contended(_root, task_id, **_kwargs):
        attempted.append(task_id)
        if task_id in locked:
            raise sqlite3.OperationalError("database is locked")
        return True, "blocked"

    monkeypatch.setattr(
        process_launcher.task_store, "mark_terminal_failure", contended
    )
    callbacks = _capture_callbacks(monkeypatch)

    # Both intents are reached -- the locked one does not abort the pass --
    # regardless of which order the intent files happen to be visited in.
    assert manager._settle_reviewer_terminal_intents() == 1
    assert sorted(attempted) == ["REVIEWER_LOCKED_1", "REVIEWER_OPEN_1"]
    assert [call["task_id"] for call in callbacks] == ["REVIEWER_OPEN_1"]
    assert manager._reviewer_terminal_intent_path("review-locked-1").is_file()
    assert not manager._reviewer_terminal_intent_path("review-open-1").exists()

    # The contention clears and the deferred transition completes exactly once.
    locked.clear()
    assert manager._settle_reviewer_terminal_intents() == 1
    assert [call["task_id"] for call in callbacks] == [
        "REVIEWER_OPEN_1",
        "REVIEWER_LOCKED_1",
    ]
    assert not manager._reviewer_terminal_intent_path("review-locked-1").exists()


def test_owed_callback_survives_a_failed_enqueue_after_the_transition(
    tmp_path, monkeypatch,
):
    # The transition lands and the callback does not.  Retiring the intent
    # there would drop the manager signal permanently, because the intent is
    # the only thing that brings a later pass back to this claim.
    manager = _manager(tmp_path)
    manager._append_event(_committed(
        request_id="review-owed-1",
        task_id="REVIEWER_OWED_1",
        reviewer_claim_epoch=7,
    ))
    assert manager._reconcile_expired_starting_reservations() == 1
    intent_path = manager._reviewer_terminal_intent_path("review-owed-1")

    applied: list[str] = []

    def transition(_root, task_id, **_kwargs):
        if applied:
            # The retry sees the card the first attempt already blocked, which
            # is the exact refusal a crash-recovered settler has to interpret.
            return False, "not_processing:current=blocked"
        applied.append(task_id)
        return True, "blocked"

    monkeypatch.setattr(
        process_launcher.task_store, "mark_terminal_failure", transition
    )

    def unwritable(*_args, **_kwargs):
        raise OSError("the callback volume went away")

    monkeypatch.setattr(
        process_launcher.task_store, "enqueue_terminal_callback", unwritable
    )

    assert manager._settle_reviewer_terminal_intents() == 0
    assert intent_path.is_file(), "an owed callback must not retire its intent"

    # Recovery: the store proves from the card that this exact intent's own
    # transition landed, so the same one callback is still owed and emitted.
    applied_probes: list[tuple] = []

    def already_applied(_root, task_id, state, **kwargs):
        applied_probes.append((task_id, state, kwargs["claim_epoch"]))
        return True

    monkeypatch.setattr(
        process_launcher.task_store,
        "terminal_failure_already_applied",
        already_applied,
    )
    callbacks = _capture_callbacks(monkeypatch)

    assert manager._settle_reviewer_terminal_intents() == 1
    assert applied_probes == [
        ("REVIEWER_OWED_1", "not_processing:current=blocked", 7)
    ]
    assert len(callbacks) == 1
    assert callbacks[0]["task_id"] == "REVIEWER_OWED_1"
    assert callbacks[0]["claim_epoch"] == 7
    assert callbacks[0]["substatus"] == "liveness_lost"
    assert not intent_path.exists()

    # Exactly one transition and exactly one callback across both passes, and
    # a further pass has nothing left to settle.
    assert applied == ["REVIEWER_OWED_1"]
    assert manager._settle_reviewer_terminal_intents() == 0
    assert len(callbacks) == 1


def test_a_contained_enqueue_failure_keeps_the_intent_for_retry(
    tmp_path, monkeypatch,
):
    # ``enqueue_terminal_callback`` contains a locked store and answers False.
    # That is "not written yet", never "nothing is owed", so the ticket stays.
    manager = _manager(tmp_path)
    manager._append_event(_committed(
        request_id="review-notyet-1",
        task_id="REVIEWER_NOTYET_1",
        reviewer_claim_epoch=12,
    ))
    assert manager._reconcile_expired_starting_reservations() == 1
    intent_path = manager._reviewer_terminal_intent_path("review-notyet-1")

    monkeypatch.setattr(
        process_launcher.task_store, "mark_terminal_failure", _TerminalSpy()
    )
    attempts: list[dict] = []
    written = {"ok": False}

    def contained(root, task_id, **kwargs):
        attempts.append({"root": root, "task_id": task_id, **kwargs})
        return written["ok"]

    monkeypatch.setattr(
        process_launcher.task_store, "enqueue_terminal_callback", contained
    )

    assert manager._settle_reviewer_terminal_intents() == 0
    assert len(attempts) == 1
    assert intent_path.is_file()

    written["ok"] = True
    assert manager._settle_reviewer_terminal_intents() == 1
    assert len(attempts) == 2
    assert attempts[-1]["claim_epoch"] == 12
    assert not intent_path.exists()


def test_an_already_durable_callback_is_not_read_as_not_written_yet(
    tmp_path, monkeypatch,
):
    # The sibling above pins the OTHER world behind the same ``False``: the
    # enqueue also answers False when the row it owes is already there, which
    # is what a crash between a successful enqueue and this retire leaves
    # behind.  Retrying that forever strands the intent and the processing
    # claim behind it, so the settler has to separate the two worlds with the
    # store's own proof -- and it must ask about the exact identity this
    # intent bound, never about the card in general.
    manager = _manager(tmp_path)
    manager._append_event(_committed(
        request_id="review-durable-1",
        task_id="REVIEWER_DURABLE_1",
        reviewer_claim_epoch=14,
    ))
    assert manager._reconcile_expired_starting_reservations() == 1
    intent_path = manager._reviewer_terminal_intent_path("review-durable-1")

    monkeypatch.setattr(
        process_launcher.task_store, "mark_terminal_failure", _TerminalSpy()
    )
    monkeypatch.setattr(
        process_launcher.task_store,
        "enqueue_terminal_callback",
        lambda *_a, **_k: False,
    )
    probes: list[dict] = []
    durable = {"ok": False}

    def already_durable(root, task_id, **kwargs):
        probes.append({"root": root, "task_id": task_id, **kwargs})
        return durable["ok"]

    monkeypatch.setattr(
        process_launcher.task_store,
        "terminal_callback_already_durable",
        already_durable,
    )

    # Unproven is still "not written yet": the ticket is kept for a pass that
    # can prove it, never retired on a refusal nobody could account for.
    assert manager._settle_reviewer_terminal_intents() == 0
    assert intent_path.is_file()
    assert probes == [{
        "root": manager.repo,
        "task_id": "REVIEWER_DURABLE_1",
        "substatus": "liveness_lost",
        "request_id": "review-durable-1",
        "claim_epoch": 14,
    }]

    durable["ok"] = True
    assert manager._settle_reviewer_terminal_intents() == 1
    assert not intent_path.exists()
    assert len(probes) == 2

    # And the retired ticket brings no later pass back to this claim.
    assert manager._settle_reviewer_terminal_intents() == 0
    assert len(probes) == 2


def test_settled_callback_names_the_exact_bound_episode(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    manager._append_event(_committed(
        request_id="review-episode-1",
        task_id="REVIEWER_EPISODE_1",
        reviewer_claim_epoch=13,
    ))
    assert manager._reconcile_expired_starting_reservations() == 1

    monkeypatch.setattr(
        process_launcher.task_store, "mark_terminal_failure", _TerminalSpy()
    )
    enqueued: list[dict] = []

    def record(root, task_id, **kwargs):
        enqueued.append({"root": root, "task_id": task_id, **kwargs})
        return True

    monkeypatch.setattr(
        process_launcher.task_store, "enqueue_terminal_callback", record
    )

    assert manager._settle_reviewer_terminal_intents() == 1
    assert len(enqueued) == 1
    assert enqueued[0]["task_id"] == "REVIEWER_EPISODE_1"
    assert enqueued[0]["request_id"] == "review-episode-1"
    # The episode is the epoch the transition was bound to -- not whatever the
    # card happens to carry by the time the callback is built.
    assert enqueued[0]["claim_epoch"] == 13
    assert enqueued[0]["substatus"] == "liveness_lost"

    # The intent is retired, so a repeated pass enqueues nothing further.
    assert manager._settle_reviewer_terminal_intents() == 0
    assert len(enqueued) == 1


def test_terminal_callback_fails_closed_on_a_non_integer_episode(tmp_path):
    # ``True`` is an ``int`` subclass; accepting it would signal the manager
    # about episode 1.  The guard rejects before any store access, so the
    # uninitialised repository is never even opened.
    for bad in (True, False, "13", 13.0):
        assert process_launcher.task_store.enqueue_terminal_callback(
            tmp_path,
            "REVIEWER_EPISODE_1",
            substatus="liveness_lost",
            request_id="review-episode-1",
            claim_epoch=bad,
        ) is False


def test_latest_by_request_lookup_stays_bounded_for_hot_path_callers(tmp_path):
    # The generation bracketing is what any caller a hidden append could
    # falsify has to pay for -- terminalizing passes and the admission CAS.
    # The plain read stays plain for the reporting callers that are left,
    # where being one append behind is a stale number and not a false verdict.
    manager = _manager(tmp_path)
    manager._append_event(
        _starting(
            request_id="review-bounded-1",
            task_id="REVIEWER_BOUNDED_1",
            expires_at=time.time() + 120.0,
        )
    )

    generations = 0
    scans = 0
    exact_generation = manager._ledger_generation
    exact_events = manager._events

    def counted_generation():
        nonlocal generations
        generations += 1
        return exact_generation()

    def counted_events():
        nonlocal scans
        scans += 1
        return exact_events()

    manager._ledger_generation = counted_generation
    manager._events = counted_events

    assert "review-bounded-1" in manager._latest_by_request()
    assert generations == 0, "the hot path must not describe the ledger at all"
    assert scans == 1, "the hot path must read the ledger exactly once"

    latest, generation = manager._latest_by_request_stable()
    assert "review-bounded-1" in latest
    assert generation is not None
    assert generations == 2, "a terminalizing read is bracketed before and after"
    assert scans == 2


def test_blocked_card_read_never_delays_a_disjoint_reservation(
    tmp_path, monkeypatch,
):
    manager = _manager(tmp_path)
    manager._append_event(_committed(
        request_id="review-block-1",
        task_id="REVIEWER_BLOCK_1",
        reviewer_claim_epoch=6,
        provider_pid=os.getpid(),
        provider_pid_start_ticks=1,
    ))
    assert manager._reconcile_expired_starting_reservations() == 1

    spy = _TerminalSpy()
    spy.gate = threading.Event()
    monkeypatch.setattr(process_launcher.task_store, "mark_terminal_failure", spy)
    _capture_callbacks(monkeypatch)

    settler = threading.Thread(target=manager._settle_reviewer_terminal_intents)
    settler.start()
    try:
        assert spy.entered.wait(timeout=5), "the card transition must be in flight"

        # A wholly unrelated task reserves its slot while that store call is
        # still blocked.  If any task-store read happened under the outer
        # registry lock, this acknowledgement would wait behind it.
        started = time.monotonic()
        with manager._launch_reservation({
            "request_id": "disjoint-req",
            "task_id": "DISJOINT_TASK",
            "runner": RUNNER,
            "topic": TOPIC,
            "adapter_id": ADAPTER,
        }):
            pass
        elapsed = time.monotonic() - started
    finally:
        spy.gate.set()
        settler.join(timeout=15)

    assert not settler.is_alive()
    assert elapsed < 2.0, f"disjoint reservation took {elapsed:.3f}s"
    assert _latest(manager, "disjoint-req")["state"] == "starting"
    assert len(spy.calls) == 1


def _archive_segment(
    manager: process_launcher.ProcessManager, index: int, request_id: str
) -> Path:
    """Write one immutable rotated segment beside the active ledger file."""

    active = manager.process_log_path
    segment = active.with_name(f"{active.stem}.{index:04d}{active.suffix}")
    segment.parent.mkdir(parents=True, exist_ok=True)
    segment.write_text(
        json.dumps({
            "schema_id": "aiworkhub.task_mcp.process_event.v1",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "request_id": request_id,
            "task_id": request_id.upper(),
            "runner": RUNNER,
            "topic": TOPIC,
            "adapter_id": ADAPTER,
            "state": "exited",
        }) + "\n",
        encoding="utf-8",
    )
    return segment


class _LedgerWork:
    """Count ledger parses and sweeps, split by whether the lock is held.

    A full parse reads every segment; a generation sweep lstats every segment.
    Both scale with the ledger, so where they happen relative to the
    cross-process registry lock is the whole performance contract.
    """

    def __init__(self, manager: process_launcher.ProcessManager) -> None:
        self.manager = manager
        self.depth = 0
        self.parses = 0
        self.parses_locked = 0
        self.sweeps = 0
        self.sweeps_locked = 0
        self.on_parse = None
        exact_events = manager._events
        exact_generation = manager._ledger_generation
        exact_registry_lock = manager._registry_lock

        @contextlib.contextmanager
        def counting_registry_lock():
            with exact_registry_lock():
                self.depth += 1
                try:
                    yield
                finally:
                    self.depth -= 1

        def counted_events():
            self.parses += 1
            if self.depth:
                self.parses_locked += 1
            result = exact_events()
            # Fired for locked reads too, deliberately: an appender that only
            # ever perturbs the UNLOCKED read can never induce a replay inside
            # the lock, and the regression would pass against the very shape it
            # exists to forbid.
            if self.on_parse is not None:
                self.on_parse()
            return result

        def counted_generation():
            self.sweeps += 1
            if self.depth:
                self.sweeps_locked += 1
            return exact_generation()

        manager._registry_lock = counting_registry_lock
        manager._events = counted_events
        manager._ledger_generation = counted_generation

    def reset(self) -> None:
        self.parses = self.parses_locked = 0
        self.sweeps = self.sweeps_locked = 0


def test_concurrent_appends_add_no_ledger_work_under_the_registry_lock(tmp_path):
    # ``_latest_by_request_stable`` replays until its bracketing generations
    # agree, so a busy multi-segment ledger costs up to
    # ``_LEDGER_SNAPSHOT_MAX_ATTEMPTS`` full parses and twice as many sweeps.
    # Paying that under the cross-process registry lock made every unrelated
    # reservation acknowledgement queue behind it.  The snapshot now happens
    # with the lock released and is re-proved under it by ONE sweep, so a
    # concurrent appender can lengthen the unlocked read as much as it likes
    # without adding a single byte of work to the locked region.
    manager = _manager(tmp_path)
    manager._append_event(
        _starting(
            request_id="review-seg-active",
            task_id="REVIEWER_SEG_ACTIVE",
            expires_at=time.time() + 600.0,
        )
    )
    for index in (1, 2):
        _archive_segment(manager, index, f"review-seg-{index}")
    segments = process_launcher.process_event_ledger.ledger_paths(
        manager.process_log_path
    )
    assert len(segments) == 3, "the regression needs a real multi-segment ledger"

    work = _LedgerWork(manager)

    # Baseline: nothing appending underneath, so the snapshot stabilizes on its
    # first attempt.  This is the irreducible work one reservation owes.
    with manager._launch_reservation({
        "request_id": "quiet-req",
        "task_id": "QUIET_TASK",
        "runner": RUNNER,
        "topic": TOPIC,
        "adapter_id": ADAPTER,
    }):
        pass
    quiet_parses = work.parses
    quiet_parses_locked = work.parses_locked
    quiet_sweeps_locked = work.sweeps_locked
    assert _latest(manager, "quiet-req")["state"] == "starting"

    # Now a second ProcessManager over the same durable process directory
    # appends underneath every unlocked read -- the exact shape that forces the
    # stable snapshot to replay.  The rows are terminal ones for unrelated
    # requests, so the appender perturbs only the ledger generation and never
    # the concurrency accounting this reservation is about to do.
    appender = _manager(tmp_path)
    pending = [3]

    def append_underneath():
        if not pending[0]:
            return
        pending[0] -= 1
        appender._append_event({
            "request_id": f"review-appender-{pending[0]}",
            "task_id": f"REVIEWER_APPENDER_{pending[0]}",
            "runner": RUNNER,
            "topic": TOPIC,
            "adapter_id": ADAPTER,
            "state": "exited",
            "returncode": 0,
        })

    work.reset()
    work.on_parse = append_underneath
    with manager._launch_reservation({
        "request_id": "busy-req",
        "task_id": "BUSY_TASK",
        "runner": RUNNER,
        "topic": TOPIC,
        "adapter_id": ADAPTER,
    }):
        pass
    work.on_parse = None

    assert pending[0] == 0, "the concurrent appender must actually have run"
    assert work.parses > quiet_parses, "the replays must really have happened"
    # ...and every one of those extra parses happened with the lock released.
    assert work.parses_locked == quiet_parses_locked
    assert work.sweeps_locked == quiet_sweeps_locked
    assert work.sweeps_locked <= 1, (
        "re-proving the handed-in snapshot is one sweep, not a replay loop"
    )
    # The acknowledgement still lands, and the pass is still exact.
    assert _latest(manager, "busy-req")["state"] == "starting"
    assert _latest(manager, "review-seg-active")["state"] == "starting"


def test_stale_handed_in_snapshot_terminalizes_nothing(tmp_path):
    # Moving the snapshot outside the lock must not weaken its authority: a
    # snapshot that no longer describes the ledger may hide a row this pass
    # would contradict, so the pass defers exactly as an unstable bracketed
    # read does.
    manager = _manager(tmp_path)
    manager._append_event(
        _starting(
            request_id="review-stale-1",
            task_id="REVIEWER_STALE_1",
            expires_at=time.time() - 1.0,
        )
    )
    snapshot = manager._latest_by_request_stable()
    assert snapshot[1] is not None

    # A concurrent appender moves the ledger between the snapshot and the lock.
    _manager(tmp_path)._append_event(
        _starting(
            request_id="review-stale-2",
            task_id="REVIEWER_STALE_2",
            expires_at=time.time() + 600.0,
        )
    )

    assert manager._reconcile_expired_starting_reservations(snapshot) == 0
    assert _latest(manager, "review-stale-1")["state"] == "starting"

    # A snapshot that was never proved stable is refused the same way.
    assert manager._reconcile_expired_starting_reservations((snapshot[0], None)) == 0
    assert _latest(manager, "review-stale-1")["state"] == "starting"

    # Re-taken against the current ledger, the very same pass terminalizes it.
    assert manager._reconcile_expired_starting_reservations() == 1
    assert _latest(manager, "review-stale-1")["state"] == "blocked"


def test_unstable_ledger_generation_terminalizes_nothing(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    manager._append_event(
        _starting(
            request_id="review-unstable-1",
            task_id="REVIEWER_UNSTABLE_1",
            expires_at=time.time() - 1.0,
        )
    )
    stable_latest, stable_generation = manager._latest_by_request_stable()
    assert stable_generation is not None
    assert "review-unstable-1" in stable_latest

    # Every bracketing read observes a different generation, which is exactly
    # what a concurrent append looks like -- the snapshot may hide a row.
    moving = iter(range(10_000))
    monkeypatch.setattr(
        manager, "_ledger_generation", lambda: (("moving", next(moving)),)
    )

    latest, generation = manager._latest_by_request_stable()
    assert generation is None
    assert "review-unstable-1" in latest
    assert manager._reconcile_expired_starting_reservations() == 0
    monkeypatch.undo()
    assert _latest(manager, "review-unstable-1")["state"] == "starting"


def test_an_append_between_the_snapshot_and_the_cas_is_never_hidden(tmp_path):
    # ``_append_event`` does not take the registry lock, so holding that lock
    # excludes other lock TAKERS and not appenders: a supervisor publishing a
    # ``running`` row, or any second ProcessManager, can land a write between
    # the pre-lock snapshot and the admission CAS.  Deciding admission from a
    # plain parse there can simply not see that row -- and the row it misses
    # is the live duplicate of the very task being launched.  The one-sweep
    # re-proof sees the ledger moved, and the bounded fresh snapshot exposes
    # the exact duplicate without admitting on the stale read.
    manager = _manager(tmp_path)
    appender = _manager(tmp_path)
    exact_registry_lock = manager._registry_lock
    raced: list[bool] = []

    rival = _starting(
        request_id="rival-req",
        task_id="RACED_TASK",
        expires_at=time.time() + 600.0,
    )

    @contextlib.contextmanager
    def racing_registry_lock():
        with exact_registry_lock():
            # Inside the lock and before the CAS reads anything: exactly the
            # window an appender that never takes this lock can hit.
            if not raced:
                raced.append(True)
                appender._append_event(rival)
            yield

    manager._registry_lock = racing_registry_lock

    launch = {
        "request_id": "racer-req",
        "task_id": "RACED_TASK",
        "runner": RUNNER,
        "topic": TOPIC,
        "adapter_id": ADAPTER,
    }
    with pytest.raises(process_launcher.LaunchRejected) as duplicate:
        with manager._launch_reservation(dict(launch)):
            pass

    assert raced == [True], "the interleaving append must actually have run"
    assert str(duplicate.value) == "duplicate_reserved_task:rival-req"
    assert _latest(manager, "racer-req") == {}
    assert _latest(manager, "rival-req")["state"] == "starting"


def test_a_reservation_this_pass_retired_no_longer_blocks_its_own_relaunch(
    tmp_path,
):
    # Reconciliation and the admission CAS now share ONE proven snapshot, so
    # the rows this pass retires have to be mirrored back into it.  Without
    # that mirror the CAS would still read the reservation it had just
    # terminalized as live, and refuse the relaunch of the very task it freed.
    manager = _manager(tmp_path)
    manager._append_event({
        "request_id": "stalled-req",
        "task_id": "STALLED_TASK",
        "runner": RUNNER,
        "topic": TOPIC,
        "adapter_id": ADAPTER,
        "state": "starting",
        # Deliberately UNEXPIRED: only the frozen preparation heartbeat makes
        # this row terminal, so a stale snapshot would show the duplicate
        # guard a perfectly live reservation rather than an elapsed one.
        "reservation_expires_at_epoch": time.time() + 600.0,
        "preparation_phase": "worktree_create",
        "preparation_heartbeat_epoch": time.time() - 100_000.0,
    })

    with manager._launch_reservation({
        "request_id": "relaunch-req",
        "task_id": "STALLED_TASK",
        "runner": RUNNER,
        "topic": TOPIC,
        "adapter_id": ADAPTER,
    }):
        pass

    # The stalled reservation really was terminalized by this same pass...
    stalled = _latest(manager, "stalled-req")
    assert stalled["state"] == "blocked"
    assert str(stalled["blocked_reason"]).startswith(
        "preparation_heartbeat_stalled:"
    )
    # ...and the relaunch it freed was admitted, not refused as a duplicate of
    # a reservation that no longer exists.
    assert _latest(manager, "relaunch-req")["state"] == "starting"


def test_the_admission_cas_never_reads_an_unproven_ledger(tmp_path, monkeypatch):
    # The companion to the interleaving case: whatever the reason a stable
    # generation cannot be shown -- a moving ledger or a segment that can
    # never be described -- admission fails closed rather than deciding the
    # concurrency limit and the duplicate guard from a parse that may be one
    # append behind.
    manager = _manager(tmp_path)
    manager._append_event(
        _starting(
            request_id="held-req",
            task_id="HELD_TASK",
            expires_at=time.time() + 600.0,
        )
    )

    moving = iter(range(10_000))
    monkeypatch.setattr(
        manager, "_ledger_generation", lambda: (("moving", next(moving)),)
    )
    with pytest.raises(process_launcher.LaunchRejected) as deferred:
        with manager._launch_reservation({
            "request_id": "unproven-req",
            "task_id": "UNPROVEN_TASK",
            "runner": RUNNER,
            "topic": TOPIC,
            "adapter_id": ADAPTER,
        }):
            pass
    assert str(deferred.value) == "ledger_snapshot_unproven"
    monkeypatch.undo()

    # No reservation was written, and the unrelated row this pass could not
    # account for is untouched.
    assert _latest(manager, "unproven-req") == {}
    assert _latest(manager, "held-req")["state"] == "starting"

    # With the ledger describable again the same launch is admitted.
    with manager._launch_reservation({
        "request_id": "unproven-req",
        "task_id": "UNPROVEN_TASK",
        "runner": RUNNER,
        "topic": TOPIC,
        "adapter_id": ADAPTER,
    }):
        pass
    assert _latest(manager, "unproven-req")["state"] == "starting"


def test_the_reviewer_reservation_never_admits_on_an_unproven_ledger(
    tmp_path, monkeypatch,
):
    # Reserving a reviewer attempt reconciles, checks for an already-live
    # reviewer and applies the concurrency ceiling.  Every one of those is
    # falsified by a single hidden append -- the live reviewer it must not
    # duplicate is exactly the row a stale parse cannot see -- so when no
    # stable generation can be shown the reservation defers instead of minting
    # a second provider.
    manager = _manager(tmp_path)
    exact_generation = manager._ledger_generation
    moving = iter(range(10_000))
    monkeypatch.setattr(
        manager, "_ledger_generation", lambda: (("moving", next(moving)),)
    )

    deferred = manager._reserve_quality_reviewer_attempt(
        reviewer_task_id="REVIEWER_UNPROVEN",
        runner=RUNNER,
        adapter_id=ADAPTER,
        target_request_id="target-req",
        target_task_id="TARGET_TASK",
        lens="correctness",
        model=None,
        timeout_seconds=60,
    )
    assert deferred == {"ok": False, "error": "ledger_snapshot_unproven"}
    # Nothing was reserved on the unproven read.
    assert _starting_count(manager, "REVIEWER_UNPROVEN") == 0

    monkeypatch.setattr(manager, "_ledger_generation", exact_generation)
    reserved = manager._reserve_quality_reviewer_attempt(
        reviewer_task_id="REVIEWER_UNPROVEN",
        runner=RUNNER,
        adapter_id=ADAPTER,
        target_request_id="target-req",
        target_task_id="TARGET_TASK",
        lens="correctness",
        model=None,
        timeout_seconds=60,
    )
    assert reserved["ok"] is True
    assert reserved["already_reserved"] is False
    assert _starting_count(manager, "REVIEWER_UNPROVEN") == 1


def test_the_reviewer_cas_paths_fail_closed_on_an_unprovable_ledger(
    tmp_path, monkeypatch,
):
    # The spawn commit, the provider attach and the terminalization each decide
    # from the exact latest row for one request, and each is contradicted by a
    # row a hidden append could be adding right now.  A plain parse under the
    # registry lock proves nothing -- ``_append_event`` never takes that lock --
    # so with no stable generation all three must act on nothing at all.
    manager = _manager(tmp_path)
    manager._append_event(
        _starting(
            request_id="review-unprovable-cas",
            task_id="REVIEWER_UNPROVABLE_CAS",
            expires_at=time.time() + 600.0,
        )
    )
    before = list(manager._events())
    exact_generation = manager._ledger_generation

    moving = iter(range(10_000))
    monkeypatch.setattr(
        manager, "_ledger_generation", lambda: (("moving", next(moving)),)
    )
    assert manager._reviewer_spawn_transition(
        "review-unprovable-cas", reviewer_claim_epoch=5
    ) is False
    assert manager._reviewer_attach_provider_identity(
        "review-unprovable-cas",
        pid=os.getpid(),
        pid_start_ticks=process_launcher._pid_start_ticks(os.getpid()),
    ) is False
    manager._terminalize_reviewer_attempt(
        "review-unprovable-cas",
        "REVIEWER_UNPROVABLE_CAS",
        RUNNER,
        ADAPTER,
        reason="owner_preparation_timeout",
    )
    assert list(manager._events()) == before, (
        "no reviewer CAS may write against an unprovable ledger"
    )

    # With the ledger describable again the very same commit is taken.
    monkeypatch.setattr(manager, "_ledger_generation", exact_generation)
    assert manager._reviewer_spawn_transition(
        "review-unprovable-cas", reviewer_claim_epoch=5
    ) is True
    committed = _latest(manager, "review-unprovable-cas")
    assert committed["state"] == "provider_spawn_committed"
    assert committed["reviewer_claim_epoch"] == 5


def test_a_hidden_append_is_seen_by_the_reviewer_spawn_cas(tmp_path):
    # The snapshot each reviewer CAS hands itself is taken with the registry
    # lock RELEASED, so a rival owner can append while it waits.  The stale
    # hand-off must not be read as "no authority": a spawn CAS that quietly
    # does nothing makes its caller terminate a provider for a reservation
    # somebody else has already committed.  The lost hand-off falls back to a
    # fresh bracketed read and reports the committed phase it really finds.
    manager = _manager(tmp_path)
    rival = _manager(tmp_path)
    manager._append_event(
        _starting(
            request_id="review-raced-cas",
            task_id="REVIEWER_RACED_CAS",
            expires_at=time.time() + 600.0,
        )
    )
    exact_registry_lock = manager._registry_lock
    raced: list[bool] = []

    @contextlib.contextmanager
    def racing_registry_lock():
        with exact_registry_lock():
            # Inside the lock and before the CAS reads anything: exactly the
            # window an appender that never takes this lock can hit.
            if not raced:
                raced.append(True)
                rival._append_event(
                    _committed(
                        request_id="review-raced-cas",
                        task_id="REVIEWER_RACED_CAS",
                        reviewer_claim_epoch=5,
                        provider_pid=os.getpid(),
                        provider_pid_start_ticks=(
                            process_launcher._pid_start_ticks(os.getpid())
                        ),
                    )
                )
            yield

    manager._registry_lock = racing_registry_lock
    assert manager._reviewer_spawn_transition(
        "review-raced-cas", reviewer_claim_epoch=5
    ) is True
    assert raced == [True], "the interleaving append must actually have run"
    manager._registry_lock = exact_registry_lock

    commits = [
        event
        for event in manager._events()
        if event.get("request_id") == "review-raced-cas"
        and event.get("state") == "provider_spawn_committed"
    ]
    assert len(commits) == 1, "the rival commit must not be duplicated"
    assert commits[0]["provider_pid"] == os.getpid()


def test_a_stale_snapshot_still_terminalizes_an_abandoned_reservation(tmp_path):
    # The mirror image.  A DISJOINT reservation is appended while this owner
    # waits for the lock, so its hand-off snapshot is stale through no fault of
    # the request it is about to terminalize.  Deferring there would leave the
    # abandoned reservation ``starting`` until its epoch elapsed -- an elapsed
    # -time outcome by the back door -- so the fresh bracketed read settles it
    # now, and the unrelated row stays exactly as its own owner left it.
    manager = _manager(tmp_path)
    rival = _manager(tmp_path)
    manager._append_event(
        _starting(
            request_id="review-raced-term",
            task_id="REVIEWER_RACED_TERM",
            expires_at=time.time() + 600.0,
        )
    )
    exact_registry_lock = manager._registry_lock
    raced: list[bool] = []

    @contextlib.contextmanager
    def racing_registry_lock():
        with exact_registry_lock():
            if not raced:
                raced.append(True)
                rival._append_event(
                    _starting(
                        request_id="review-unrelated",
                        task_id="REVIEWER_UNRELATED",
                        expires_at=time.time() + 600.0,
                    )
                )
            yield

    manager._registry_lock = racing_registry_lock
    manager._terminalize_reviewer_attempt(
        "review-raced-term",
        "REVIEWER_RACED_TERM",
        RUNNER,
        ADAPTER,
        reason="owner_preparation_timeout",
    )
    assert raced == [True], "the interleaving append must actually have run"
    manager._registry_lock = exact_registry_lock

    terminal = _latest(manager, "review-raced-term")
    assert terminal["state"] == "blocked"
    assert terminal["blocked_reason"] == "owner_preparation_timeout"
    assert _latest(manager, "review-unrelated")["state"] == "starting"


def _ledger_diagnostics(manager: process_launcher.ProcessManager) -> list[dict]:
    """Every operator-visible line this manager has recorded."""

    path = process_launcher.reviewer_terminal_intent_diagnostic_path(
        manager.process_log_path
    )
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_standing_unprovable_ledger_generation_stops_replaying(tmp_path, monkeypatch):
    # ``_ledger_generation`` refuses a segment that is a symlink or not a
    # regular file, and that refusal does not go away by asking again.  The
    # stable snapshot used to spend its whole ``_LEDGER_SNAPSHOT_MAX_ATTEMPTS``
    # budget of full parses -- and twice as many sweeps -- re-proving that on
    # every single launch, and said nothing to anyone while doing it.
    manager = _manager(tmp_path)
    manager._append_event(
        _starting(
            request_id="review-unprovable-active",
            task_id="REVIEWER_UNPROVABLE_ACTIVE",
            expires_at=time.time() + 600.0,
        )
    )
    segment = _archive_segment(manager, 1, "review-unprovable-1")
    # A real symlink on disk, with the bytes moved outside the ledger
    # directory so nothing else can still find them.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    real = elsewhere / segment.name
    segment.rename(real)
    segment.symlink_to(real)

    # ``ledger_paths`` drops a symlinked archive on the floor, so hand the
    # segment straight to ``_ledger_generation`` -- the way a segment swapped
    # under a live rotation reaches it -- and let the real method refuse it.
    exact_ledger_paths = process_launcher.process_event_ledger.ledger_paths
    monkeypatch.setattr(
        process_launcher.process_event_ledger,
        "ledger_paths",
        lambda path: [segment, path],
    )
    assert manager._ledger_generation() is None

    work = _LedgerWork(manager)
    launches = 4
    for _ in range(launches):
        latest, generation = manager._latest_by_request_stable()
        # Fail-closed authority is untouched: an unproven generation still
        # means no pass may terminalize anything from this snapshot.
        assert generation is None
        assert latest == {}

    attempts = process_launcher.ProcessManager._LEDGER_SNAPSHOT_MAX_ATTEMPTS
    assert work.parses == 0, (
        "a generation that can never be proved must not buy a single parse"
    )
    assert work.sweeps == 2 * launches, (
        f"two sweeps prove the failure standing; {2 * attempts} sweeps and "
        f"{attempts} parses per launch is the loop this forbids"
    )

    # ...and the unusable segment is named exactly once across every launch.
    diagnostics = _ledger_diagnostics(manager)
    assert len(diagnostics) == 1
    assert diagnostics[0]["reason"] == "ledger_generation_unprovable"
    assert diagnostics[0]["intent_file"] == segment.name
    assert diagnostics[0]["schema_id"] == (
        process_launcher.REVIEWER_TERMINAL_INTENT_DIAGNOSTIC_SCHEMA_ID
    )

    # A ONE-OFF undescribable read is the opposite case: a rotation landing
    # between the two bracketing sweeps.  It must still replay, and must never
    # be reported, or the attempt budget would have no purpose left.
    patient_root = tmp_path / "patient"
    patient_root.mkdir()
    patient = _manager(patient_root)
    patient._append_event(
        _starting(
            request_id="review-patient",
            task_id="REVIEWER_PATIENT",
            expires_at=time.time() + 600.0,
        )
    )
    flaky = [segment]

    def flaky_ledger_paths(path):
        if flaky:
            flaky.pop()
            return [segment, path]
        return exact_ledger_paths(path)

    monkeypatch.setattr(
        process_launcher.process_event_ledger, "ledger_paths", flaky_ledger_paths
    )
    latest, generation = patient._latest_by_request_stable()
    assert not flaky, "the one-off refusal must actually have been served"
    assert generation is not None
    assert "review-patient" in latest
    assert _ledger_diagnostics(patient) == []


def test_ledger_generation_changes_on_every_append(tmp_path):
    manager = _manager(tmp_path)
    manager._append_event(
        _starting(
            request_id="review-gen-1",
            task_id="REVIEWER_GEN_1",
            expires_at=time.time() + 120.0,
        )
    )
    before = manager._ledger_generation()
    assert before is not None
    manager._append_event(
        _starting(
            request_id="review-gen-2",
            task_id="REVIEWER_GEN_2",
            expires_at=time.time() + 120.0,
        )
    )
    assert manager._ledger_generation() != before


def test_reviewer_spawn_transition_persists_reviewer_claim_epoch(tmp_path):
    manager = _manager(tmp_path)
    manager._append_event(
        _starting(
            request_id="review-epoch-1",
            task_id="REVIEWER_EPOCH_1",
            expires_at=time.time() + 120.0,
        )
    )
    assert manager._reviewer_spawn_transition(
        "review-epoch-1", binding={}, reviewer_claim_epoch=12
    ) is True
    committed = _latest(manager, "review-epoch-1")
    assert committed["state"] == "provider_spawn_committed"
    assert committed["reviewer_claim_epoch"] == 12

    # A bool is not an epoch, and an unknown epoch is never fabricated: the
    # committed phase simply carries none, and reconciliation fails closed.
    manager._append_event(
        _starting(
            request_id="review-epoch-2",
            task_id="REVIEWER_EPOCH_2",
            expires_at=time.time() + 120.0,
        )
    )
    assert manager._reviewer_spawn_transition(
        "review-epoch-2", binding={}, reviewer_claim_epoch=True
    ) is True
    assert "reviewer_claim_epoch" not in _latest(manager, "review-epoch-2")


def _terminalized_after_preflight(
    manager: process_launcher.ProcessManager,
    monkeypatch: pytest.MonkeyPatch,
    lifecycle: dict[str, str],
    failures: list[dict],
) -> None:
    """Drive a real launch to the terminalized-reservation return.

    The reservation is still held when the launch starts, the preflight card
    decides whether this launch holds the canonical claim, and the bounded
    owner terminalizes the reservation immediately afterwards -- the exact
    window in which a claimed card can be abandoned.
    """

    monkeypatch.setattr(process_launcher, "launch_gates_open", lambda: True)
    monkeypatch.setattr(
        process_launcher.ProcessManager,
        "_reviewer_reservation_still_held",
        lambda _self, _request_id: True,
    )
    monkeypatch.setattr(
        process_launcher.ProcessManager,
        "_preflight_card",
        lambda _self, task_id, *_a, **_k: {
            "task_id": task_id,
            "runner": RUNNER,
            "topic": TOPIC,
            "status": lifecycle["status"],
            "worker_status": lifecycle["worker_status"],
            "claimed_by": RUNNER,
        },
    )

    def terminalized(_self, _card):
        raise process_launcher._ReviewerReservationTerminalized("bounded-owner")

    monkeypatch.setattr(
        process_launcher.ProcessManager, "_with_dependency_inputs", terminalized
    )

    def mark_launch_failed(repo, task_id, runner, *, reason, request_id):
        failures.append({
            "repo": repo,
            "task_id": task_id,
            "runner": runner,
            "reason": reason,
            "request_id": request_id,
        })
        return {"ok": True}

    monkeypatch.setattr(
        process_launcher.task_engine, "mark_launch_failed", mark_launch_failed
    )


def _abandoned_receipt(
    manager: process_launcher.ProcessManager, request_id: str
) -> dict:
    return manager._launch_isolated(
        task_id="TARGET_TASK",
        runner=RUNNER,
        topic=TOPIC,
        adapter_id=ADAPTER,
        model=None,
        owner_prompt="",
        timeout_seconds=600,
        quality_review_binding={"lens": "correctness"},
        reserved_request_id=request_id,
    )


def test_a_claimed_card_reaches_one_truthful_terminal_state_when_abandoned(
    tmp_path, monkeypatch,
):
    # A reviewer launch that already holds the canonical claim can still lose
    # its reservation to the bounded owner.  Returning the abandon receipt
    # without moving that claim leaves the exact card ``processing`` under an
    # owner that is gone: the reservation is already terminal, so no later
    # reconciliation pass ever comes back to it.
    manager = _manager(tmp_path)
    lifecycle = {"status": "processing", "worker_status": "claimed"}
    failures: list[dict] = []
    _terminalized_after_preflight(manager, monkeypatch, lifecycle, failures)

    receipt = _abandoned_receipt(manager, "review-claimed-1")

    assert receipt["ok"] is False
    assert receipt["blocked_reason"] == "quality_review_reservation_terminalized"
    assert receipt["claimed_task_transition"] == "launch_failed"
    assert failures == [{
        "repo": manager.repo,
        "task_id": "TARGET_TASK",
        "runner": RUNNER,
        "reason": "quality_review_reservation_terminalized",
        "request_id": "review-claimed-1",
    }]
    # Exactly one truthful terminal state for the claimed card, and the
    # terminalized reservation is never given a second terminal event.
    assert _latest(manager, "review-claimed-1") == {}

    # A launch that never took the claim must fabricate no transition at all:
    # that pending card is still somebody else's to pick up.
    lifecycle.update(status="pending", worker_status="unclaimed")
    failures.clear()
    receipt = _abandoned_receipt(manager, "review-claimed-2")
    assert receipt["claimed_task_transition"] == "not_claimed"
    assert failures == []
    assert _latest(manager, "review-claimed-2") == {}


def test_an_abandoned_claim_names_a_refused_terminal_transition(
    tmp_path, monkeypatch,
):
    # The transition can itself be refused, and a bounded blocked receipt that
    # said nothing about it would read as proof the claim was released.
    manager = _manager(tmp_path)
    lifecycle = {"status": "processing", "worker_status": "claimed"}
    failures: list[dict] = []
    _terminalized_after_preflight(manager, monkeypatch, lifecycle, failures)
    monkeypatch.setattr(
        process_launcher.task_engine,
        "mark_launch_failed",
        lambda *_a, **_k: {"ok": False, "stderr": "claim_owner_mismatch"},
    )

    receipt = _abandoned_receipt(manager, "review-claimed-3")

    assert receipt["ok"] is False
    assert receipt["blocked_reason"] == "quality_review_reservation_terminalized"
    assert receipt["claimed_task_transition"] == (
        "launch_failure_transition_failed:claim_owner_mismatch"
    )


def test_claimed_pre_provider_exception_uses_exact_reserved_request(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    request_id = "review-stale-editor-host"
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    process_launcher.task_store.initialize_repository(manager.repo)
    _readiness, db_path = process_launcher.task_store._require_ready(manager.repo)
    now = "2026-08-30T00:00:00+00:00"
    card = {
        "task_id": "TARGET_TASK",
        "runner": RUNNER,
        "topic": TOPIC,
        "status": "processing",
        "worker_status": "claimed",
        "claimed_by": RUNNER,
        "launch_request_id": request_id,
        "allowed_writes": ["candidate.py"],
    }
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, "
            "priority, objective, card_json, created_at, updated_at, claimed_by, "
            "claimed_at, started_at) VALUES (?, ?, ?, 'processing', 'claimed', "
            "'', '', ?, ?, ?, ?, ?, '')",
            (
                "TARGET_TASK",
                RUNNER,
                TOPIC,
                json.dumps(card),
                now,
                now,
                RUNNER,
                now,
            ),
        )

    monkeypatch.setattr(process_launcher, "launch_gates_open", lambda: True)
    monkeypatch.setattr(
        process_launcher.ProcessManager,
        "_preflight_card",
        lambda _self, *_args, **_kwargs: card,
    )
    monkeypatch.setattr(
        process_launcher.ProcessManager,
        "_reviewer_reservation_still_held",
        lambda _self, candidate: candidate == request_id,
    )

    def stale_editor_host(_self, _card):
        raise RuntimeError("stale_editor_host_before_provider")

    monkeypatch.setattr(
        process_launcher.ProcessManager, "_with_dependency_inputs", stale_editor_host
    )

    receipt = _abandoned_receipt(manager, request_id)
    assert receipt["ok"] is False
    assert "launch_request_id_required" not in receipt["blocked_reason"]

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT status, worker_status, completed_at, card_json "
            "FROM tasks WHERE task_id='TARGET_TASK'"
        ).fetchone()
    assert row is not None
    assert row["status"] == "blocked"
    assert row["worker_status"] == "launch_failed"
    assert row["completed_at"]
    failed_card = json.loads(row["card_json"])
    assert failed_card["launch_request_id"] == request_id
    terminal_snapshot = tuple(row)

    # Retry is idempotent, while exact request/owner CAS guards later claims.
    retry = process_launcher.task_engine.mark_launch_failed(
        manager.repo,
        "TARGET_TASK",
        RUNNER,
        reason="retry",
        request_id=request_id,
    )
    assert retry["ok"] is False
    request_mismatch = process_launcher.task_engine.mark_launch_failed(
        manager.repo,
        "TARGET_TASK",
        RUNNER,
        reason="must not steal",
        request_id="different-request",
    )
    assert request_mismatch["ok"] is False
    owner_mismatch = process_launcher.task_engine.mark_launch_failed(
        manager.repo,
        "TARGET_TASK",
        "different-owner",
        reason="must not steal",
        request_id=request_id,
    )
    assert owner_mismatch["ok"] is False
    with sqlite3.connect(db_path) as conn:
        terminal_retry = conn.execute(
            "SELECT status, worker_status, completed_at, card_json "
            "FROM tasks WHERE task_id='TARGET_TASK'"
        ).fetchone()
    assert terminal_retry == terminal_snapshot

    archived, archive_reason = process_launcher.task_store.archive_task(
        manager.repo, "TARGET_TASK", reason="terminal launch failure"
    )
    assert archived is True, archive_reason
