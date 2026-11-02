"""NF-2026-01975: agent_task_status summary carries a bounded first_failure block."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import mcp_summary_folds, server  # noqa: E402

_KEYS = {"command", "index", "exit_code", "failure_class", "tail", "tail_source"}


def _status(rows: list[dict], card: dict | None = None) -> dict:
    return {
        "ok": True,
        "request_id": "req-1",
        "task_id": "T-1",
        "state": "finished",
        "task_card": {"task_id": "T-1", "title": "t", "status": "finished", **(card or {})},
        "latest_event": {"event": "finished", "validation": rows},
    }


def _call(monkeypatch, payload: dict, detail: str = "summary") -> dict:
    class FakeManager:
        def status(self, request_id: str) -> dict:
            return copy.deepcopy(payload)

    monkeypatch.setattr(server.process_launcher, "default_manager", lambda: FakeManager())
    return server.aiworkhub_agent_task_status("req-1", detail=detail)


def _size(value: dict) -> int:
    return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def test_first_of_two_failures_is_reported(monkeypatch) -> None:
    rows = [
        {"command": "ruff check .", "returncode": 0, "stdout_tail": "ok"},
        {"command": "pytest -q a", "returncode": 1, "failure_class": "test_failed",
         "timed_out": False, "stderr_tail": "E boom\n1 failed", "stdout_tail": "out"},
        {"command": "pytest -q b", "returncode": 2, "failure_class": "collection_error",
         "stderr_tail": "later"},
    ]
    result = _call(monkeypatch, _status(rows))
    first = result["first_failure"]
    assert first["command"] == "pytest -q a"
    assert first["index"] == 1
    assert first["exit_code"] == 1
    assert first["failure_class"] == "test_failed"
    assert first["tail"] == "E boom\n1 failed"
    assert first["tail_source"] == "stderr"
    assert first["timed_out"] is False


def test_tail_falls_back_to_stdout_and_argv() -> None:
    rows = [{"argv": ["python", "-m", "pytest"], "returncode": 1, "stderr_tail": "  \n", "stdout_tail": "only out"}]
    first = mcp_summary_folds.first_failure_from_status({"validation": rows}, {})
    assert first["command"] == "python -m pytest"
    assert first["tail"] == "only out"
    assert first["tail_source"] == "stdout"
    assert "timed_out" not in first


def test_tail_is_line_and_byte_bounded_and_utf8_safe() -> None:
    many = "\n".join(f"line {i}" for i in range(100))
    first = mcp_summary_folds.first_failure_from_status(
        {"validation": [{"command": "x", "returncode": 1, "stderr_tail": many}]}, {}
    )
    assert first["tail"].splitlines() == [f"line {i}" for i in range(80, 100)]

    wide = "€" * 3000
    first = mcp_summary_folds.first_failure_from_status(
        {"validation": [{"command": "x", "returncode": 1, "stderr_tail": "a" + wide}]}, {}
    )
    encoded = first["tail"].encode("utf-8")
    assert len(encoded) <= mcp_summary_folds.FIRST_FAILURE_TAIL_BYTES
    assert len(encoded) >= mcp_summary_folds.FIRST_FAILURE_TAIL_BYTES - 3
    assert set(first["tail"]) == {"€"}


def test_whole_block_stays_under_fixed_cap() -> None:
    row = {
        "command": "c" * 5000, "returncode": 1, "failure_class": "f" * 5000, "timed_out": True,
        "stderr_tail": "€" * 20000,
    }
    first = mcp_summary_folds.first_failure_from_status({"validation": [row]}, {})
    assert len(first["command"]) == mcp_summary_folds.FIRST_FAILURE_COMMAND_CHARS
    assert _size(first) <= mcp_summary_folds.FIRST_FAILURE_MAX_BYTES

    wide_command = dict(row, command="€" * 5000)
    first = mcp_summary_folds.first_failure_from_status({"validation": [wide_command]}, {})
    assert _size(first) <= mcp_summary_folds.FIRST_FAILURE_MAX_BYTES


def test_terminal_failure_is_the_fallback() -> None:
    card = {"terminal_failure": {
        "substatus": "worker_failed", "reason": "boom",
        "evidence": {"exit_code": 3, "stdout_tail": "worker said no"},
    }}
    first = mcp_summary_folds.first_failure_from_status({"validation": []}, card)
    assert first["failure_class"] == "worker_failed"
    assert first["exit_code"] == 3
    assert first["tail"] == "worker said no"
    assert first["tail_source"] == "stdout"
    assert first["index"] is None


def test_passing_and_running_have_no_first_failure(monkeypatch) -> None:
    passing = _status([{"command": "pytest", "returncode": 0, "stdout_tail": "ok"}])
    running = _status([])
    running["state"] = "running"
    running["latest_event"] = {"event": "progress"}
    for payload in (passing, running):
        assert "first_failure" not in _call(monkeypatch, payload)


def test_evidence_and_full_are_unchanged(monkeypatch) -> None:
    payload = _status([{"command": "pytest", "returncode": 1, "stderr_tail": "bad"}])
    assert "first_failure" in _call(monkeypatch, payload)
    assert "first_failure" not in _call(monkeypatch, payload, detail="evidence")
    full = _call(monkeypatch, payload, detail="full")
    assert full == payload
