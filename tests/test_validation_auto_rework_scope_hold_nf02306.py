"""NF-2026-02306: a size-ratchet / line-cap failure is held for the manager, never auto-returned."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import launch_replay_guard, task_store, validation_auto_rework as auto  # noqa: E402

_TASK = "TASK_SCOPE_HOLD"
_REQ = "req-scope-0001"
_CMD = "python -m pytest -q tests/test_x.py"
_METADATA = {"task_id": _TASK, "runner": "claude", "topic": "t", "adapter_id": "claude_cli", "model": "claude-opus-5-5"}
_CARD = {"task_id": _TASK, "runner": "claude", "topic": "t", "validation_roles": ["reproduction", "lint"]}


@pytest.fixture(autouse=True)
def history(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auto, "_reject_history", lambda repo, task_id: [])
    monkeypatch.setattr(launch_replay_guard, "counted_candidate_code_rejects", lambda evs, identity: [])
    monkeypatch.setattr(task_store, "get_task", lambda repo, task_id: dict(_CARD))


def _evidence(tail: str) -> dict[str, Any]:
    return {
        "request_id": _REQ,
        "validation": [
            {"declared_command": _CMD, "command": "/abs/" + _CMD, "returncode": 1,
             "stdout_tail": tail, "stderr_tail": ""},
            {"declared_command": "ruff check", "returncode": 0, "stdout_tail": "ok"},
        ],
    }


def _plan(tail: str) -> dict[str, Any]:
    return auto.plan(Path("."), _METADATA, "validation_failed", "validation_failed:x", _evidence(tail))


@pytest.mark.parametrize(
    "message",
    [
        "AssertionError: module_size_ratchet_exceeded for src/aiworkhub/big.py",
        "E   Size Ratchet violated: src/aiworkhub/big.py grew",
        "E   RATCHET_EXCEEDED src/aiworkhub/big.py",
        "E   line cap of 800 exceeded by src/aiworkhub/big.py",
        "E   LINE_CAP: file exceeds allowed lines",
    ],
)
def test_ratchet_marker_failure_is_held_for_the_manager(message: str) -> None:
    keys = _plan(f"FAILED tests/test_x.py::test_a - {message}\n1 failed, 3 passed in 1.23s\n")
    record = keys["validation_auto_rework"]
    assert record["decision"] == "manager"
    assert record["cause"] == "scope_change_needed"
    assert "manager_callback_deferred" not in keys


def test_ordinary_assertion_failure_is_still_auto_returned() -> None:
    keys = _plan("FAILED tests/test_x.py::test_a - AssertionError: assert 1 == 2\n1 failed, 3 passed in 1.23s\n")
    record = keys["validation_auto_rework"]
    assert record["decision"] == "auto_return"
    assert record["cause"] == auto.REASON_CODE
    assert keys["manager_callback_deferred"] == {"reason": "validation_auto_rework"}
