from __future__ import annotations

import importlib
import json
import sqlite3
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

autolaunch = importlib.import_module("aiworkhub.dependency_autolaunch")
launch_admission = importlib.import_module("aiworkhub.launch_admission")
process_launcher = importlib.import_module("aiworkhub.process_launcher")
process_launcher_launch_isolated = importlib.import_module(
    "aiworkhub.process_launcher_launch_isolated"
)
task_store = importlib.import_module("aiworkhub.task_store")


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _denied() -> dict:
    return {"admit": False, "retryable": True, "reason": "memory_capacity_insufficient"}


def _admitted() -> dict:
    return {"admit": True, "retryable": True, "reason": "memory_capacity_available"}


@pytest.fixture(autouse=True)
def _no_leftover_waiters():
    yield
    assert launch_admission._waiters == []


def _await(clock: _Clock, probe, *, kind="worker", wait_seconds=60, poll_seconds=5):
    return launch_admission.await_memory_admission(
        kind=kind,
        probe=probe,
        wait_seconds=wait_seconds,
        poll_seconds=poll_seconds,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )


def test_admits_after_capacity_frees_and_clears_waiter():
    clock = _Clock()
    verdicts = iter([_denied(), _denied(), _admitted()])
    result = _await(clock, lambda: next(verdicts))
    assert result["admit"] is True
    assert result["waited_seconds"] == 10.0
    assert clock.sleeps == [5, 5]


def test_expiry_returns_named_reason_and_clears_waiter():
    clock = _Clock()
    result = _await(clock, _denied, wait_seconds=12, poll_seconds=5)
    assert result["admit"] is False
    assert result["reason"] == "admission_wait_expired"
    assert result["retryable"] is True
    assert result["waited_seconds"] == 12.0


def test_probe_exception_clears_waiter():
    def boom() -> dict:
        raise RuntimeError("probe failed")

    with pytest.raises(RuntimeError):
        _await(_Clock(), boom)


def test_zero_wait_probes_exactly_once():
    calls: list[int] = []

    def probe() -> dict:
        calls.append(1)
        return _denied()

    clock = _Clock()
    result = _await(clock, probe, wait_seconds=0)
    assert len(calls) == 1
    assert clock.sleeps == []
    assert result["admit"] is False
    assert result["reason"] == "memory_capacity_insufficient"
    assert result["waited_seconds"] == 0.0


def test_unknown_kind_is_refused():
    with pytest.raises(ValueError):
        _await(_Clock(), _admitted, kind="operator")


def test_reviewer_is_admitted_before_an_earlier_worker():
    capacity = {"free": False}
    admitted: list[str] = []
    guard = threading.Lock()

    def probe_for(name: str):
        def probe() -> dict:
            with guard:
                if capacity["free"]:
                    capacity["free"] = False
                    admitted.append(name)
                    return _admitted()
            return _denied()

        return probe

    def run(name: str, kind: str) -> None:
        launch_admission.await_memory_admission(
            kind=kind,
            probe=probe_for(name),
            wait_seconds=30,
            poll_seconds=0,
            sleep=lambda _s: threading.Event().wait(0.0005),
        )

    def wait_for_waiters(count: int) -> None:
        for _ in range(20_000):
            with launch_admission._lock:
                if len(launch_admission._waiters) >= count:
                    return
            threading.Event().wait(0.0005)
        raise AssertionError("waiters never registered")

    worker = threading.Thread(target=run, args=("worker", "worker"))
    worker.start()
    wait_for_waiters(1)
    reviewer = threading.Thread(target=run, args=("reviewer", "reviewer"))
    reviewer.start()
    wait_for_waiters(2)

    capacity["free"] = True
    for _ in range(20_000):
        with guard:
            if admitted:
                break
        threading.Event().wait(0.0005)
    assert admitted == ["reviewer"]
    reviewer.join(timeout=10)
    capacity["free"] = True
    worker.join(timeout=10)
    assert admitted == ["reviewer", "worker"]
    assert not worker.is_alive() and not reviewer.is_alive()


def test_configured_wait_seconds_env(monkeypatch):
    monkeypatch.delenv(launch_admission.WAIT_SECONDS_ENV, raising=False)
    assert launch_admission.configured_wait_seconds() == 1800
    monkeypatch.setenv(launch_admission.WAIT_SECONDS_ENV, "0")
    assert launch_admission.configured_wait_seconds() == 0
    monkeypatch.setenv(launch_admission.WAIT_SECONDS_ENV, "not-a-number")
    assert launch_admission.configured_wait_seconds() == 1800
    for raw in ("inf", "nan"):
        monkeypatch.setenv(launch_admission.WAIT_SECONDS_ENV, raw)
        assert launch_admission.configured_wait_seconds() == 1800


def test_zero_wait_worker_is_refused_while_a_reviewer_is_queued():
    reviewer = launch_admission._Waiter(launch_admission._RANKS["reviewer"])
    with launch_admission._lock:
        launch_admission._waiters.append(reviewer)
    try:
        result = _await(_Clock(), _admitted, wait_seconds=0)
    finally:
        with launch_admission._lock:
            launch_admission._waiters.remove(reviewer)
    assert result["admit"] is False
    assert result["reason"] == launch_admission.OUTRANKED_REASON
    assert _await(_Clock(), _admitted, wait_seconds=0)["admit"] is True


def test_deferred_reviewer_raises_only_after_the_wait_expires(monkeypatch):
    clock = _Clock()
    real_await = launch_admission.await_memory_admission
    monkeypatch.setattr(
        launch_admission,
        "await_memory_admission",
        lambda **kw: real_await(**kw, sleep=clock.sleep, monotonic=clock.monotonic),
    )
    monkeypatch.setenv(launch_admission.WAIT_SECONDS_ENV, "20")
    monkeypatch.setattr(process_launcher, "_memory_launch_admission", _denied)
    monkeypatch.setattr(process_launcher, "launch_gates_open", lambda: True)
    monkeypatch.setattr(process_launcher, "_validate_adapter_identity", lambda *a: None)
    monkeypatch.setattr(
        process_launcher, "validate_workforce_identity", lambda *a, **k: "model"
    )
    monkeypatch.setattr(
        process_launcher, "_validation_only_replay_authorization", lambda *a: None
    )
    monkeypatch.setattr(
        process_launcher, "_enforce_quality_review_launch_binding", lambda *a: None
    )
    monkeypatch.setattr(
        process_launcher.task_engine,
        "record_launch_blocker",
        lambda *a, **k: {"ok": True},
    )

    class _Manager:
        repo = Path(".")

        def _reviewer_reservation_still_held(self, _request_id):
            return True

        def _preflight_card(self, *args, **kwargs):
            return {"status": "pending", "worker_status": "unclaimed"}

        def _with_dependency_inputs(self, card):
            return card

        def _await_reviewer_admission(self, probe):
            return process_launcher.ProcessManager._await_reviewer_admission(
                self, probe
            )

        def _blocked(self, task_id, runner, topic, adapter_id, reason, **kwargs):
            return {"state": kwargs.get("state", "blocked"), "blocked_reason": reason}

    result = process_launcher_launch_isolated.launch_isolated(
        _Manager(),
        task_id="TASK_R1",
        runner="runner_r1",
        topic="quality_review",
        adapter_id="claude_cli",
        model=None,
        owner_prompt="",
        timeout_seconds=600,
        reserved_request_id="req1",
    )

    assert clock.sleeps, "the reviewer must wait before it is refused"
    assert clock.now >= 20
    assert result["state"] == "blocked"
    prefix = "memory_launch_capacity_denied:"
    assert result["blocked_reason"].startswith(prefix)
    detail = json.loads(result["blocked_reason"][len(prefix):])
    assert detail["reason"] == "admission_wait_expired"
    assert detail["waited_seconds"] >= 20


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    assert task_store.initialize_repository(repo)["ok"]
    return repo


def _db(root: Path) -> Path:
    return task_store.storage_readiness(root).canonical_db


def _insert_pending_card(root: Path, task_id: str, *, runner: str) -> None:
    now = "2026-10-01T00:00:00+00:00"
    card = {
        "task_id": task_id,
        "runner": runner,
        "topic": "task_mcp",
        "status": "pending",
        "worker_status": "unclaimed",
        "origin_thread_id": f"thread_{task_id.lower()}",
        "coordinator_provider": "codex",
    }
    conn = sqlite3.connect(_db(root))
    try:
        conn.execute(
            "INSERT INTO tasks (task_id, runner, topic, status, worker_status, "
            "claimed_by, card_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (task_id, runner, "task_mcp", "pending", "unclaimed", None,
             json.dumps(card), now, now),
        )
        conn.commit()
    finally:
        conn.close()


def _row(root: Path, task_id: str) -> tuple[str, str, dict]:
    conn = sqlite3.connect(_db(root))
    try:
        status, worker_status, raw = conn.execute(
            "SELECT status, worker_status, card_json FROM tasks WHERE task_id=?",
            (task_id,),
        ).fetchone()
    finally:
        conn.close()
    return status, worker_status, json.loads(raw)


def test_admission_deferred_card_stays_pending_and_is_relaunched_when_due(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(autolaunch, "network_reachable", lambda: True)
    root = _init_repo(tmp_path)
    _insert_pending_card(root, "T1", runner="runner_t1")

    deferred, state = launch_admission.defer_card(
        root, "T1", "runner_t1", 'memory_launch_capacity_denied:{"reason":"x"}'
    )
    assert (deferred, state) == (True, "pending")
    status, worker_status, card = _row(root, "T1")
    assert (status, worker_status) == ("pending", "unclaimed")
    assert card["transient_retry"]["kind"] == "admission_deferred"
    assert card["transient_retry"]["attempts"] >= 1
    assert card["retry_not_before"]
    assert "transient_retry_attempts" not in card

    calls: list[str] = []

    def launch(task_id: str, runner: str, topic: str, request_id: str) -> dict:
        calls.append(task_id)
        return {"ok": True}

    outcome = autolaunch.reconcile_transient_due(root, launch=launch)
    assert calls == []
    assert outcome["delayed"][0]["reason"] == "transient_retry_not_due"

    conn = sqlite3.connect(_db(root))
    try:
        card["retry_not_before"] = "2000-01-01T00:00:00+00:00"
        conn.execute(
            "UPDATE tasks SET card_json=? WHERE task_id='T1'", (json.dumps(card),)
        )
        conn.commit()
    finally:
        conn.close()
    outcome = autolaunch.reconcile_transient_due(root, launch=launch)
    assert calls == ["T1"]
    assert [entry["task_id"] for entry in outcome["launched"]] == ["T1"]


def test_admission_deferral_refuses_a_claimed_or_foreign_card(tmp_path):
    root = _init_repo(tmp_path)
    _insert_pending_card(root, "T2", runner="runner_t2")
    assert task_store.mark_admission_deferred(
        root, "T2", runner="someone_else", reason="r", retry_after_seconds=30
    ) == (False, "runner_mismatch")
    assert task_store.mark_admission_deferred(
        root, "MISSING", runner="runner_t2", reason="r", retry_after_seconds=30
    ) == (False, "task_not_found")


def test_only_capacity_refusals_are_deferred():
    assert launch_admission.is_admission_refusal("concurrency_limit_reached")
    assert launch_admission.is_admission_refusal(
        'memory_launch_capacity_denied:{"reason":"memory_probe_unavailable"}'
    )
    assert not launch_admission.is_admission_refusal("duplicate_live_task:abc")
    assert launch_admission.admission_reason(
        'memory_launch_capacity_denied:{"reason":"memory_probe_unavailable"}'
    ) == "memory_probe_unavailable"
