from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import doctor, server  # noqa: E402


def _only(report: dict, name: str) -> dict:
    return next(entry for entry in report["probes"] if entry["name"] == name)


def test_ok_probe_is_ok() -> None:
    report = doctor.diagnose({"aiworkhub_task_health": lambda: {"ok": True}})
    assert report["schema_id"] == "aiworkhub.doctor_diagnose.v1"
    entry = _only(report, "aiworkhub_task_health")
    assert entry["state"] == "ok"
    assert entry["next_tool"] == "aiworkhub_task_health"
    assert set(entry) == {"name", "state", "summary", "next_tool"}


def test_ok_false_is_problem_with_bounded_excerpt() -> None:
    report = doctor.diagnose({"p": lambda: {"ok": False, "error": "x" * 500}})
    entry = _only(report, "p")
    assert entry["state"] == "problem"
    assert entry["summary"] == "x" * doctor.SUMMARY_MAX_CHARS


def test_ok_false_falls_back_through_error_fields() -> None:
    assert _only(doctor.diagnose({"p": lambda: {"ok": False, "last_error": "boom"}}), "p")[
        "summary"
    ] == "boom"
    assert _only(doctor.diagnose({"p": lambda: {"ok": False, "reason": "why"}}), "p")[
        "summary"
    ] == "why"


def test_nested_reconciler_ok_false_is_problem() -> None:
    result = {"ok": True, "reconciler": {"ok": False, "last_error": "scan_stalled"}}
    entry = _only(doctor.diagnose({"p": lambda: result}), "p")
    assert entry["state"] == "problem"
    assert "scan_stalled" in entry["summary"]
    assert "reconciler" in entry["summary"]


def test_nested_learning_coverage_ok_false_is_problem() -> None:
    result = {"ok": True, "learning_coverage": {"ok": False, "reason": "uncovered"}}
    entry = _only(doctor.diagnose({"p": lambda: result}), "p")
    assert entry["state"] == "problem"
    assert "learning_coverage" in entry["summary"]


def test_raising_probe_is_unavailable_with_exception_type() -> None:
    def boom() -> dict:
        raise RuntimeError("secret detail")

    entry = _only(doctor.diagnose({"p": boom}), "p")
    assert entry["state"] == "unavailable"
    assert entry["summary"] == "RuntimeError"


def test_non_dict_probe_is_unavailable() -> None:
    entry = _only(doctor.diagnose({"p": lambda: ["not", "a", "dict"]}), "p")  # type: ignore[dict-item,return-value]
    assert entry["state"] == "unavailable"
    assert "list" in entry["summary"]


def test_timeout_probe_is_unavailable() -> None:
    def slow() -> dict:
        time.sleep(1.0)
        return {"ok": True}

    started = time.monotonic()
    report = doctor.diagnose({"p": slow}, timeout_seconds=0.2)
    assert time.monotonic() - started < 0.9
    entry = _only(report, "p")
    assert entry["state"] == "unavailable"
    assert entry["summary"] == "timeout"


def test_probe_queued_behind_a_hung_probe_is_not_started(monkeypatch) -> None:
    monkeypatch.setattr(doctor.os, "cpu_count", lambda: 1)

    def slow() -> dict:
        time.sleep(1.0)
        return {"ok": True}

    report = doctor.diagnose({"a": slow, "b": lambda: {"ok": True}}, timeout_seconds=0.2)
    assert _only(report, "a")["summary"] == "timeout"
    assert _only(report, "b")["summary"] == "not_started"
    assert report["verdict"] == "unavailable"


def test_verdict_ok_only_when_every_probe_ok() -> None:
    report = doctor.diagnose({"a": lambda: {"ok": True}, "b": lambda: {}})
    assert report["verdict"] == "ok"
    assert report["problems"] == []


def test_verdict_unavailable_when_unavailable_and_no_problem() -> None:
    def boom() -> dict:
        raise ValueError

    report = doctor.diagnose({"a": lambda: {"ok": True}, "b": boom})
    assert report["verdict"] == "unavailable"
    assert [entry["name"] for entry in report["problems"]] == ["b"]


def test_verdict_problem_wins_over_unavailable() -> None:
    def boom() -> dict:
        raise ValueError

    report = doctor.diagnose(
        {"a": lambda: {"ok": True}, "b": boom, "c": lambda: {"ok": False, "error": "e"}}
    )
    assert report["verdict"] == "problem"
    assert [entry["name"] for entry in report["problems"]] == ["b", "c"]


def test_concurrent_results_match_sequential_calls_in_probe_order() -> None:
    probes = {
        f"probe_{index}": (lambda i=index: {"ok": i % 2 == 0, "error": f"e{i}"})
        for index in range(6)
    }
    report = doctor.diagnose(probes)
    expected = [doctor._classify(name, probe()) for name, probe in probes.items()]
    assert report["probes"] == expected


def test_reply_stays_under_eight_kib_by_dropping_summaries_first() -> None:
    probes = {
        f"probe_with_a_long_name_{index:02d}": (lambda: {"ok": False, "error": "y" * 400})
        for index in range(20)
    }
    report = doctor.diagnose(probes)
    assert len(json.dumps(report).encode("utf-8")) < 8 * 1024
    assert report["verdict"] == "problem"
    assert all(entry["summary"] == "" for entry in report["probes"])
    assert len(report["problems"]) == 20


def test_empty_probe_set_is_refused() -> None:
    try:
        doctor.diagnose({})
    except ValueError as exc:
        assert "at_least_one_probe" in str(exc)
    else:
        raise AssertionError("empty probe set must not report a verdict")


def test_server_registers_doctor_diagnose_tool() -> None:
    mcp = server.mcp
    if hasattr(mcp, "registered_tools"):
        names = list(mcp.registered_tools)
    else:
        names = [tool.name for tool in asyncio.run(mcp.list_tools())]
    assert "aiworkhub_doctor_diagnose" in names
    assert (server.aiworkhub_doctor_diagnose.__doc__ or "").lstrip().startswith("READ-ONLY:")


def test_server_tool_composes_the_three_health_probes(monkeypatch) -> None:
    monkeypatch.setattr(server, "aiworkhub_task_health", lambda: {"ok": True})
    monkeypatch.setattr(server.core, "dispatcher_health", lambda: {"ok": False, "error": "stopped"})
    monkeypatch.setattr(server.core, "source_graph_health", lambda: {"ok": True})
    report = server.aiworkhub_doctor_diagnose()
    assert report["verdict"] == "problem"
    assert [entry["name"] for entry in report["probes"]] == [
        "aiworkhub_task_health",
        "aiworkhub_dispatcher_health",
        "aiworkhub_source_graph_health",
    ]
    assert [entry["name"] for entry in report["problems"]] == ["aiworkhub_dispatcher_health"]
