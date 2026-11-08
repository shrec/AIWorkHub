"""NF-2026-02169: freed heap is returned to the OS after each heavy operation."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from aiworkhub import (
    doctor,
    health,
    platform_io,
    task_reconciler,
    task_retention,
    task_store,
    task_store_compaction,
)


@pytest.fixture(autouse=True)
def _fresh_trim_state() -> Any:
    def reset() -> None:
        platform_io._HEAP_TRIM_STATE.update({"probed": False, "handle": None, "last": None})

    reset()
    yield
    reset()


class _FakeLibc:
    def __init__(self, result: int = 1) -> None:
        self.calls: list[int] = []
        self.loads = 0

        def malloc_trim(pad: int) -> int:
            self.calls.append(pad)
            return result

        self.malloc_trim = malloc_trim


def _fake_glibc(monkeypatch: pytest.MonkeyPatch, result: int = 1) -> _FakeLibc:
    libc = _FakeLibc(result)

    def load(name: str) -> _FakeLibc:
        assert name == "libc.so.6"
        libc.loads += 1
        return libc

    monkeypatch.setattr(platform_io, "is_linux", lambda platform_name=None: True)
    monkeypatch.setattr(os, "confstr", lambda name: "glibc 2.35", raising=False)
    monkeypatch.setattr(platform_io.ctypes, "CDLL", load)
    return libc


def _count_trims(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []

    def trim() -> dict[str, Any]:
        calls.append(1)
        return {"available": True, "trimmed": True}

    monkeypatch.setattr(platform_io, "trim_process_heap", trim)
    return calls


def test_off_glibc_is_a_no_op_that_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform_io, "is_linux", lambda platform_name=None: False)
    first = platform_io.trim_process_heap()
    assert first["available"] is False
    assert first["trimmed"] is False
    assert platform_io.trim_process_heap()["available"] is False


def test_musl_and_load_failure_report_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform_io, "is_linux", lambda platform_name=None: True)

    def musl(name: str) -> str:
        raise ValueError("no glibc")

    monkeypatch.setattr(os, "confstr", musl, raising=False)
    assert platform_io.trim_process_heap()["available"] is False

    platform_io._HEAP_TRIM_STATE.update({"probed": False, "handle": None, "last": None})
    monkeypatch.setattr(os, "confstr", lambda name: "glibc 2.35", raising=False)

    def broken(name: str) -> Any:
        raise OSError("libc.so.6 missing")

    monkeypatch.setattr(platform_io.ctypes, "CDLL", broken)
    assert platform_io.trim_process_heap()["available"] is False


def test_trim_failure_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    libc = _fake_glibc(monkeypatch)

    def boom(pad: int) -> int:
        raise RuntimeError("trim exploded")

    libc.malloc_trim = boom  # type: ignore[assignment]
    result = platform_io.trim_process_heap()
    assert result["available"] is False
    assert result["reason"].startswith("trim_failed:")


def test_trim_uses_a_cached_handle_and_is_rate_limited(monkeypatch: pytest.MonkeyPatch) -> None:
    libc = _fake_glibc(monkeypatch)
    clock = {"now": 100.0}
    monkeypatch.setattr(platform_io, "time", SimpleNamespace(monotonic=lambda: clock["now"]))

    first = platform_io.trim_process_heap()
    assert first == {"available": True, "trimmed": True}
    second = platform_io.trim_process_heap()
    assert second["skipped"] == "rate_limited"
    assert second["trimmed"] is False
    assert libc.calls == [0]

    clock["now"] += platform_io.HEAP_TRIM_MIN_INTERVAL_SECONDS + 0.1
    assert platform_io.trim_process_heap()["trimmed"] is True
    assert libc.calls == [0, 0]
    assert libc.loads == 1


def test_records_janitor_trims_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _count_trims(monkeypatch)
    monkeypatch.setattr(task_retention, "_janitor_callbacks", lambda root: {})
    monkeypatch.setattr(task_retention, "_janitor_needfix", lambda root: {})
    monkeypatch.setattr(task_retention, "run_automatic_hygiene", lambda root, now=None: {})
    task_retention.run_records_janitor(".")
    assert calls == [1]


def test_compaction_trims_once_and_when_it_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _count_trims(monkeypatch)
    monkeypatch.setattr(task_store, "canonical_db_path", lambda repo_root: tmp_path / "q.sqlite")
    monkeypatch.setattr(task_store_compaction, "_db_stats", lambda db_path: {})
    monkeypatch.setattr(task_store_compaction, "_candidates", lambda db_path, cutoff: [])
    monkeypatch.setattr(task_store_compaction, "_attempt_artifact_root", lambda repo_root: tmp_path)
    report = task_store_compaction.compact_finished_cards(tmp_path, dry_run=True)
    assert report["ok"] is True
    assert calls == [1]

    with pytest.raises(task_store_compaction.CompactionError):
        task_store_compaction.compact_finished_cards(tmp_path, batch_size=0)
    assert calls == [1, 1]


def _stub_scan_phases(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "_scan_review_ready_recovery",
        "_scan_wave_goal_bindings",
        "_scan_wave_completion",
        "_scan_transient_retry_relaunch",
        "_scan_operational_retry_relaunch",
        "_scan_orphan_claims",
    ):
        monkeypatch.setattr(task_reconciler, name, lambda *args, **kwargs: {})


def test_reconciler_pass_trims_once_and_when_it_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _count_trims(monkeypatch)
    _stub_scan_phases(monkeypatch)
    manager = SimpleNamespace(repo=tmp_path, reconcile=lambda include_gc=True: {})
    assert task_reconciler.run_scan(manager, include_gc=False)["ok"] is True
    assert calls == [1]

    def failing(include_gc: bool = True) -> dict[str, Any]:
        raise RuntimeError("reconcile failed")

    broken = SimpleNamespace(repo=tmp_path, reconcile=failing)
    with pytest.raises(RuntimeError):
        task_reconciler.run_scan(broken, include_gc=False)
    assert calls == [1, 1]


def test_health_scan_trims_once_and_when_it_raises(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _count_trims(monkeypatch)
    monkeypatch.setattr(health, "health", lambda: {"ok": True})
    health.main()
    capsys.readouterr()
    assert calls == [1]

    def failing() -> dict[str, Any]:
        raise RuntimeError("health failed")

    monkeypatch.setattr(health, "health", failing)
    with pytest.raises(RuntimeError):
        health.main()
    assert calls == [1, 1]


def test_doctor_diagnose_trims_once_and_when_it_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _count_trims(monkeypatch)
    monkeypatch.setattr(doctor, "task_db_holders", lambda: {})
    reply = doctor.diagnose({"probe": lambda: {"ok": True}}, timeout_seconds=5.0)
    assert reply["verdict"] == doctor.STATE_OK
    assert calls == [1]

    with pytest.raises(ValueError):
        doctor.diagnose({})
    assert calls == [1, 1]


def test_decorator_preserves_the_wrapped_identity() -> None:
    assert doctor.diagnose.__name__ == "diagnose"
    assert task_reconciler.run_scan.__name__ == "run_scan"
    assert task_retention.run_records_janitor.__wrapped__.__name__ == "run_records_janitor"
