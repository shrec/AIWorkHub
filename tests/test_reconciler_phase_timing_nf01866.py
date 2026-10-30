"""A reconciler scan says which phase it is in and how long each phase took.

Measured 2026-10-30: one scan ran ~20 minutes and nothing recorded WHICH of
its nine sequential phases took the time.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import core, manager_callback_wait  # noqa: E402
from aiworkhub import task_reconciler as tr  # noqa: E402

GC_PHASES = ["callback_prune", "task_hygiene"]
ALL_PHASES = [
    "reconcile",
    "callback_prune",
    "task_hygiene",
    "review_recovery",
    "wave_goal_bindings",
    "wave_completion",
    "transient_retry_relaunch",
    "orphan_claim_release",
    "sdlc_sync",
]


class _Mgr:
    def __init__(self, repo: Path) -> None:
        self.repo = repo

    def reconcile(self, *, include_gc: bool = True) -> dict:
        return {"finalized": 0, "watched": 0, "gc_included": include_gc}


def _patch_phases(monkeypatch, on_call=None):
    def _wrap(name, value):
        def _fn(*_a, **_k):
            if on_call is not None:
                on_call(name)
            return value

        return _fn

    monkeypatch.setattr(core, "_prune_stale_callbacks", _wrap("callback_prune", {"state": "ok"}))
    monkeypatch.setattr(core, "_schedule_task_hygiene", _wrap("task_hygiene", {"state": "ok"}))
    monkeypatch.setattr(tr, "_scan_review_ready_recovery", _wrap("review_recovery", {}))
    monkeypatch.setattr(tr, "_scan_wave_goal_bindings", _wrap("wave_goal_bindings", {}))
    monkeypatch.setattr(tr, "_scan_wave_completion", _wrap("wave_completion", {}))
    monkeypatch.setattr(tr, "_scan_transient_retry_relaunch", _wrap("transient_retry_relaunch", {}))
    monkeypatch.setattr(tr, "_scan_orphan_claims", _wrap("orphan_claim_release", {}))
    monkeypatch.setattr(
        tr.repository_state,
        "inspect_repository",
        lambda _root: SimpleNamespace(manifest=SimpleNamespace(repo_id="repo_x")),
    )
    monkeypatch.setattr(tr.sdlc_sync, "sync_once", _wrap("sdlc_sync", {"state": "ok"}))


def test_durations_cover_all_nine_phases_with_gc(monkeypatch, tmp_path):
    _patch_phases(monkeypatch)
    result = tr.run_scan(_Mgr(tmp_path), include_gc=True)
    assert result["ok"] is True
    assert set(result["phase_durations_ms"]) == set(ALL_PHASES)
    assert all(isinstance(v, int) and v >= 0 for v in result["phase_durations_ms"].values())


def test_durations_omit_gc_phases_without_gc(monkeypatch, tmp_path):
    _patch_phases(monkeypatch)
    result = tr.run_scan(_Mgr(tmp_path), include_gc=False)
    assert set(result["phase_durations_ms"]) == set(ALL_PHASES) - set(GC_PHASES)


def test_on_phase_receives_names_in_order(monkeypatch, tmp_path):
    _patch_phases(monkeypatch)
    seen: list[str] = []
    tr.run_scan(_Mgr(tmp_path), include_gc=True, on_phase=seen.append)
    assert seen == ALL_PHASES
    seen.clear()
    tr.run_scan(_Mgr(tmp_path), include_gc=False, on_phase=seen.append)
    assert seen == [p for p in ALL_PHASES if p not in GC_PHASES]


def test_on_phase_is_called_before_its_phase_runs(monkeypatch, tmp_path):
    events: list[str] = []
    _patch_phases(monkeypatch, on_call=lambda name: events.append(f"run:{name}"))
    tr.run_scan(_Mgr(tmp_path), include_gc=True, on_phase=lambda n: events.append(f"enter:{n}"))
    assert events.index("enter:wave_completion") < events.index("run:wave_completion")
    assert events.index("enter:sdlc_sync") < events.index("run:sdlc_sync")


def test_a_raising_on_phase_never_fails_the_scan(monkeypatch, tmp_path):
    _patch_phases(monkeypatch)
    calls: list[str] = []

    def _boom(name: str) -> None:
        calls.append(name)
        raise RuntimeError("status write failed")

    result = tr.run_scan(_Mgr(tmp_path), include_gc=True, on_phase=_boom)
    assert result["ok"] is True
    assert calls == ALL_PHASES
    assert set(result["phase_durations_ms"]) == set(ALL_PHASES)


def test_a_slow_phase_shows_the_larger_duration(monkeypatch, tmp_path):
    clock = {"now": 1000.0}
    monkeypatch.setattr(tr.time, "monotonic", lambda: clock["now"])

    def _advance(name):
        if name == "wave_completion":
            clock["now"] += 12.5

    _patch_phases(monkeypatch, on_call=_advance)
    result = tr.run_scan(_Mgr(tmp_path), include_gc=True)
    durations = result["phase_durations_ms"]
    assert durations["wave_completion"] == 12500
    assert all(v == 0 for k, v in durations.items() if k != "wave_completion")


def test_real_sleep_is_measured(monkeypatch, tmp_path):
    _patch_phases(monkeypatch, on_call=lambda name: time.sleep(0.05) if name == "sdlc_sync" else None)
    durations = tr.run_scan(_Mgr(tmp_path), include_gc=False)["phase_durations_ms"]
    assert durations["sdlc_sync"] >= 40


def test_loop_publishes_phase_and_completed_record_carries_durations(monkeypatch, tmp_path):
    svc = tr.ReconcilerService.__new__(tr.ReconcilerService)
    svc.repo = tmp_path
    svc.scan_interval_seconds = tr.MIN_SCAN_INTERVAL_SECONDS
    svc._manager = None
    svc._stop_event = threading.Event()
    svc._thread = None
    svc._state_lock = threading.Lock()
    svc._last_scan = {}
    svc._last_error = ""
    during: list[dict] = []

    def _scan(_mgr=None, *, repo=None, include_gc=True, on_phase=None):
        on_phase("review_recovery")
        during.append(tr.read_status(tmp_path))
        svc._stop_event.set()
        return {"ok": True, "phase_durations_ms": {"review_recovery": 7}}

    monkeypatch.setattr(tr, "run_scan", _scan)
    svc._loop()

    (mid,) = during
    assert mid["scan_in_progress"] is True
    assert mid["scan_phase"] == "review_recovery"
    assert isinstance(mid["scan_phase_started_epoch"], float)
    after = tr.read_status(tmp_path)
    assert after["scan_in_progress"] is False
    assert after["scan_phase"] == ""
    assert after["phase_durations_ms"] == {"review_recovery": 7}
    assert after["last_completed_scan"]["phase_durations_ms"] == {"review_recovery": 7}


def _liveness_reconciler(monkeypatch, record: dict) -> dict:
    monkeypatch.setattr(tr, "read_status", lambda _repo: record)
    monkeypatch.setattr(core, "_claude_session_process_rows", lambda *_a, **_k: [])
    snapshot = manager_callback_wait._claude_session_liveness_snapshot({"session_id": "s"}, [])
    return snapshot["reconciler"]


def test_liveness_exposes_running_phase_and_age(monkeypatch):
    reconciler = _liveness_reconciler(monkeypatch, {
        "scan_in_progress": True,
        "scan_started_epoch": time.time() - 100,
        "scan_finished_epoch": None,
        "scan_phase": "wave_completion",
        "scan_phase_started_epoch": time.time() - 30,
        "scan_interval_seconds": 30.0,
    })
    assert reconciler["scan_phase"] == "wave_completion"
    assert isinstance(reconciler["scan_phase_age_s"], float)
    assert 29 <= reconciler["scan_phase_age_s"] <= 60


def test_liveness_shows_empty_phase_for_a_completed_record(monkeypatch):
    reconciler = _liveness_reconciler(monkeypatch, {
        "scan_in_progress": False,
        "scan_started_epoch": time.time() - 100,
        "scan_finished_epoch": time.time() - 10,
        "scan_phase": "",
        "scan_phase_started_epoch": time.time() - 30,
        "scan_interval_seconds": 30.0,
    })
    assert reconciler["scan_phase"] == ""
    assert reconciler["scan_phase_age_s"] is None
