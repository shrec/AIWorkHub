"""NF-2026-01967: a probe still running at the deadline is slow, not unavailable."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import doctor  # noqa: E402


def _only(report: dict, name: str) -> dict:
    return next(entry for entry in report["probes"] if entry["name"] == name)


def _sleeper(seconds: float):
    def probe() -> dict:
        time.sleep(seconds)
        return {"ok": True}

    return probe


def _raises() -> dict:
    raise RuntimeError("down")


def test_only_slow_probe_yields_degraded_with_timeout_recorded() -> None:
    # Fast probes first so a single-worker host never queues them behind the sleeper.
    report = doctor.diagnose(
        {
            "a": lambda: {"ok": True},
            "b": lambda: {"ok": True},
            "aiworkhub_task_health": _sleeper(1.0),
        },
        timeout_seconds=0.2,
    )
    assert report["schema_id"] == doctor.SCHEMA_ID
    assert report["verdict"] == "degraded"
    entry = _only(report, "aiworkhub_task_health")
    assert entry["state"] == doctor.STATE_SLOW == "slow"
    assert entry["timeout_seconds"] == 0.2
    assert entry["summary"] == "timeout"
    assert [e["name"] for e in report["problems"]] == ["aiworkhub_task_health"]
    assert _only(report, "a")["state"] == "ok"


def test_raising_probe_still_unavailable_and_wins_over_slow() -> None:
    report = doctor.diagnose({"r": _raises, "s": _sleeper(1.0)}, timeout_seconds=0.2)
    assert _only(report, "r")["state"] == "unavailable"
    assert _only(report, "r")["summary"] == "RuntimeError"
    assert _only(report, "s")["state"] == "slow"
    assert report["verdict"] == "unavailable"


def test_problem_result_still_wins_over_slow() -> None:
    report = doctor.diagnose(
        {"p": lambda: {"ok": False, "error": "broken"}, "s": _sleeper(1.0)},
        timeout_seconds=0.2,
    )
    assert _only(report, "p")["state"] == "problem"
    assert report["verdict"] == "problem"


def test_cancelled_probe_stays_unavailable_not_started(monkeypatch) -> None:
    monkeypatch.setattr(doctor.os, "cpu_count", lambda: 1)
    release = threading.Event()

    def hung() -> dict:
        release.wait(2.0)
        return {"ok": True}

    try:
        report = doctor.diagnose({"a": hung, "b": lambda: {"ok": True}}, timeout_seconds=0.2)
    finally:
        release.set()
    assert _only(report, "a")["state"] == "slow"
    b = _only(report, "b")
    assert b["state"] == "unavailable"
    assert b["summary"] == "not_started"
    assert "timeout_seconds" not in b
    assert report["verdict"] == "unavailable"


def test_all_ok_still_ok() -> None:
    report = doctor.diagnose({"a": lambda: {"ok": True}})
    assert report["verdict"] == "ok"
    assert "timeout_seconds" not in _only(report, "a")
