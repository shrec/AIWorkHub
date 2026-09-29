"""NF-2026-01124: a reconciler owner yields to a newer installed generation."""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import runtime_generation, task_reconciler  # noqa: E402

INTERVAL = 0.2


def _install(storage: Path, generation: str) -> Path:
    module = (
        storage / "runtime" / "generations" / generation / "runtime" / "aiworkhub"
        / "task_reconciler.py"
    )
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("", encoding="utf-8")
    return module


def _point(storage: Path, generation: str, **extra) -> None:
    record = {
        "schema_id": runtime_generation.CURRENT_SCHEMA_ID,
        "generation": generation,
        "version": "0.0.0",
        **extra,
    }
    (storage / "runtime" / "current.json").write_text(json.dumps(record), encoding="utf-8")


def _wait_for(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _service(repo: Path, module: Path) -> task_reconciler.ReconcilerService:
    service = task_reconciler.ReconcilerService(repo, scan_interval_seconds=5)
    # The constructor clamps to the production minimum; the tests shrink the
    # interval afterwards so three free intervals take well under a second.
    service.scan_interval_seconds = INTERVAL
    service._runtime_path = module
    return service


def test_own_generation_only_for_an_installed_layout(tmp_path):
    module = _install(tmp_path / "gs", "G1")
    assert runtime_generation.own_generation(module) == "G1"
    dev = tmp_path / "checkout" / "src" / "aiworkhub" / "task_reconciler.py"
    assert runtime_generation.own_generation(dev) is None
    # This test's own checkout is a dev layout and is never superseded.
    assert runtime_generation.own_generation() is None
    assert runtime_generation.superseded() is False


def test_current_generation_reads_a_valid_pointer_and_fails_closed(tmp_path):
    storage = tmp_path / "gs"
    module = _install(storage, "G1")
    assert runtime_generation.current_generation(module) is None
    assert runtime_generation.superseded(module) is False

    _point(storage, "G1")
    assert runtime_generation.current_generation(module) == "G1"
    assert runtime_generation.superseded(module) is False

    _point(storage, "G2")
    assert runtime_generation.generation_pair(module) == ("G1", "G2")
    assert runtime_generation.superseded(module) is True

    current = storage / "runtime" / "current.json"
    for invalid in (
        "{not json",
        json.dumps({"schema_id": "other.v1", "generation": "G2"}),
        json.dumps({"schema_id": runtime_generation.CURRENT_SCHEMA_ID}),
        json.dumps({"schema_id": runtime_generation.CURRENT_SCHEMA_ID, "generation": "../x"}),
        json.dumps(["G2"]),
    ):
        current.write_text(invalid, encoding="utf-8")
        assert runtime_generation.current_generation(module) is None
        assert runtime_generation.superseded(module) is False

    _point(storage, "G2", padding="x" * (runtime_generation.MAX_CURRENT_BYTES + 1))
    assert runtime_generation.current_generation(module) is None
    assert runtime_generation.superseded(module) is False


def test_a_dev_checkout_is_never_superseded_even_beside_a_pointer(tmp_path):
    storage = tmp_path / "gs"
    _install(storage, "G1")
    _point(storage, "G2")
    dev = storage / "runtime" / "aiworkhub" / "task_reconciler.py"
    assert runtime_generation.superseded(dev) is False


def test_owner_hands_the_lock_to_a_newer_generation(tmp_path, monkeypatch):
    storage = tmp_path / "gs"
    old_module = _install(storage, "G1")
    new_module = _install(storage, "G2")
    _point(storage, "G1")
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(
        task_reconciler, "run_scan", lambda *_a, **_k: {"ok": True, "finalized": 0}
    )
    old = _service(repo, old_module)
    new = _service(repo, new_module)
    try:
        old.start()
        assert _wait_for(lambda: old.health()["authority_state"] == "active_owner")
        assert _wait_for(
            lambda: task_reconciler.read_status(repo).get("runtime_generation") == "G1"
        )

        _point(storage, "G2")
        new.start()
        assert _wait_for(lambda: new.health()["authority_state"] == "active_owner")
        health = old.health()
        assert health["authority_state"] == "standby"
        assert health["last_acquisition_error"] == "superseded_runtime_generation:G1->G2"

        assert _wait_for(
            lambda: task_reconciler.read_status(repo).get("runtime_generation") == "G2"
        )
        record = task_reconciler.read_status(repo)
        assert record["server_version"] == task_reconciler.SERVER_VERSION
        durable = task_reconciler.reconciler_health(repo)
        assert durable["owner_runtime_generation"] == "G2"
        # No service is registered for this repo, so "own" is this checkout.
        assert durable["own_runtime_generation"] is None

        # The fresh owner keeps the lock: the superseded server stays standby.
        time.sleep(INTERVAL * 6)
        assert new.health()["authority_state"] == "active_owner"
        assert old.health()["authority_state"] == "standby"
    finally:
        new.stop()
        old.stop()


def test_superseded_standby_waits_for_three_free_intervals(tmp_path, monkeypatch):
    storage = tmp_path / "gs"
    module = _install(storage, "G1")
    _point(storage, "G2")
    repo = tmp_path / "repo"
    repo.mkdir()
    scans: list[float] = []
    scanned = threading.Event()

    def _scan(*_a, **_k):
        scans.append(time.monotonic())
        scanned.set()
        return {"ok": True, "finalized": 0}

    monkeypatch.setattr(task_reconciler, "run_scan", _scan)
    lock_path = repo / task_reconciler.LOCK_REL_PATH
    service = _service(repo, module)
    try:
        # A fresh owner holds the lock: the superseded server never takes it.
        with task_reconciler.single_instance_lock(lock_path):
            service.start()
            time.sleep(INTERVAL * 5)
            assert scans == []
            health = service.health()
            assert health["authority_state"] == "standby"
            assert health["last_acquisition_error"] == "superseded_runtime_generation:G1->G2"
        released = time.monotonic()

        # ...nor right after it releases; only once the lock has stayed free
        # for three intervals, so the reconciler never goes dark.
        assert scanned.wait(10.0)
        assert scans[0] - released >= task_reconciler.SUPERSEDED_FREE_INTERVALS * INTERVAL
    finally:
        service.stop()


def test_an_unsuperseded_service_reports_its_own_generation(tmp_path, monkeypatch):
    storage = tmp_path / "gs"
    module = _install(storage, "G1")
    _point(storage, "G1")
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(
        task_reconciler, "run_scan", lambda *_a, **_k: {"ok": True, "finalized": 0}
    )
    service = _service(repo, module)
    with task_reconciler.single_instance_lock(repo / task_reconciler.LOCK_REL_PATH) as i:
        service._authority_identity = dict(i)
        service._authority_state = "active_owner"
        service._run_as_owner(max_iterations=1, stop_requested=lambda: False)
    assert service._last_acquisition_error == ""
    monkeypatch.setitem(task_reconciler._SERVICES, str(repo.resolve()), service)
    health = task_reconciler.reconciler_health(repo)
    assert health["owner_runtime_generation"] == "G1"
    assert health["own_runtime_generation"] == "G1"
