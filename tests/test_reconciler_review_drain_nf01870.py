"""NF-2026-01870: review recovery also runs on its own drain thread.

A scan can run for minutes and the next one starts only after it returns, so
reviewer launch must not wait on scan length.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import pytest  # noqa: E402

from aiworkhub import review_orchestrator, runtime_generation, task_reconciler  # noqa: E402

INTERVAL = 0.05
DRAIN_PREFIX = "aiworkhub-review-drain:"


def _drain_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name.startswith(DRAIN_PREFIX) and t.is_alive()]


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


@pytest.fixture
def service(tmp_path, monkeypatch):
    svc = task_reconciler.ReconcilerService.__new__(task_reconciler.ReconcilerService)
    svc.repo = tmp_path
    svc.scan_interval_seconds = INTERVAL
    svc._manager = object()
    svc._stop_event = threading.Event()
    svc._state_lock = threading.Lock()
    svc._last_scan = {}
    svc._last_error = ""
    svc._pass_index = 0
    svc._authority_identity = {}
    svc._last_acquisition_error = ""
    monkeypatch.setattr(task_reconciler, "read_status", lambda repo: {})
    monkeypatch.setattr(task_reconciler, "write_status", lambda repo, record: None)
    monkeypatch.setattr(runtime_generation, "own_generation", lambda path=None: None)
    monkeypatch.setattr(svc, "_superseded_reason", lambda: "")
    monkeypatch.setattr(task_reconciler, "_last_review_recovery_monotonic", 0.0)
    yield svc
    svc._stop_event.set()
    assert _wait_for(lambda: not _drain_threads())


def test_drain_thread_runs_recovery_while_scan_is_blocked(service, monkeypatch):
    release = threading.Event()
    calls: list[str] = []

    def blocked_scan(*_args, **_kwargs):
        release.wait(10)
        return {}

    def recovery(_manager):
        calls.append(threading.current_thread().name)
        return {}

    monkeypatch.setattr(task_reconciler, "run_scan", blocked_scan)
    monkeypatch.setattr(task_reconciler, "_scan_review_ready_recovery", recovery)

    owner = threading.Thread(target=service._run_as_owner, kwargs={"max_iterations": 1})
    owner.start()
    try:
        assert _wait_for(lambda: len(calls) >= 1)
        assert owner.is_alive()
        assert all(name.startswith(DRAIN_PREFIX) for name in calls)
    finally:
        release.set()
        owner.join(5)
    assert not owner.is_alive()


def test_drain_thread_stops_with_owner_and_adds_no_pass(service, monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(task_reconciler, "run_scan", lambda *a, **k: {})
    monkeypatch.setattr(
        task_reconciler, "_scan_review_ready_recovery", lambda manager: calls.append(1) or {}
    )

    service._run_as_owner(max_iterations=1)

    assert not _drain_threads()
    settled = len(calls)
    time.sleep(INTERVAL * 6)
    assert len(calls) == settled
    assert settled == 0


class _Ticks:
    def __init__(self, *results: bool) -> None:
        self._results = iter(results)

    def wait(self, _timeout):
        return next(self._results)


def _run_drain_loop(service, ticks: _Ticks) -> None:
    thread = threading.Thread(target=service._review_drain_loop, args=(ticks,))
    thread.start()
    thread.join(5)
    assert not thread.is_alive()


def test_fresh_stamp_suppresses_the_drain_tick(service, monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(
        task_reconciler, "_scan_review_ready_recovery", lambda manager: calls.append(1) or {}
    )
    monkeypatch.setattr(task_reconciler, "_last_review_recovery_monotonic", time.monotonic())
    service.scan_interval_seconds = 3600.0

    _run_drain_loop(service, _Ticks(False, False, True))

    assert calls == []


def test_stale_stamp_lets_the_drain_tick_run(service, monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(
        task_reconciler, "_scan_review_ready_recovery", lambda manager: calls.append(1) or {}
    )
    monkeypatch.setattr(task_reconciler, "_last_review_recovery_monotonic", 0.0)
    service.scan_interval_seconds = 1.0

    _run_drain_loop(service, _Ticks(False, True))

    assert calls == [1]


def test_recovery_is_single_flight(monkeypatch):
    state = {"current": 0, "max": 0}
    guard = threading.Lock()

    def slow_db(_manager):
        with guard:
            state["current"] += 1
            state["max"] = max(state["max"], state["current"])
        time.sleep(0.05)
        with guard:
            state["current"] -= 1
        return None

    monkeypatch.setattr(review_orchestrator, "canonical_review_db", slow_db)
    before = task_reconciler._last_review_recovery_monotonic
    threads = [
        threading.Thread(target=task_reconciler._scan_review_ready_recovery, args=(object(),))
        for _ in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)

    assert state["max"] == 1
    assert task_reconciler._last_review_recovery_monotonic > before
