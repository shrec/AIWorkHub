"""NF-2026-02129: do not relaunch a candidate byte-identical to the predecessor that just failed."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import (  # noqa: E402
    launch_replay_guard,
    task_store,
    validation_auto_rework as auto,
)

_TASK = "TASK_AUTO"
_REQ = "req-auto-0001"
_CMD = "python -m pytest -q tests/test_x.py"


@pytest.fixture(autouse=True)
def pinned_reject(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        auto, "_reject_pinned",
        lambda manager, task_id, reason, request_id: manager.reject_review(
            task_id, reason, to="pending", predecessor_request_id=request_id),
    )


def _tail(msg: str = "assert 1 == 2") -> str:
    return (
        f"FAILED tests/test_x.py::test_a - AssertionError: {msg}\n"
        f"E   file /tmp/pytest-of-u/pytest-7/t0/data.json missing (pid=4242)\n"
        "1 failed, 3 passed in 1.23s\n"
    )


def _evidence(tail: str | None = None, *, changed_path_hashes: dict | None = None, **extra: Any) -> dict[str, Any]:
    payload = {
        "request_id": _REQ,
        "validation": [
            {"declared_command": _CMD, "command": "/abs/" + _CMD, "returncode": 1,
             "stdout_tail": tail if tail is not None else _tail(), "stderr_tail": ""},
            {"declared_command": "ruff check", "returncode": 0, "stdout_tail": "ok"},
        ],
        **extra,
    }
    if changed_path_hashes is not None:
        payload["changed_path_hashes"] = changed_path_hashes
    return payload


_METADATA = {"task_id": _TASK, "runner": "claude", "topic": "t", "adapter_id": "claude_cli", "model": "claude-opus-5-5"}


def _card(rework_predecessor: Any = None) -> dict[str, Any]:
    card = {"task_id": _TASK, "runner": "claude", "topic": "t", "validation_roles": ["reproduction", "lint"]}
    if rework_predecessor is not None:
        card["rework_predecessor"] = rework_predecessor
    return card


@pytest.fixture
def history(monkeypatch: pytest.MonkeyPatch):
    events: list[dict[str, Any]] = []
    monkeypatch.setattr(auto, "_reject_history", lambda repo, task_id: list(events))
    monkeypatch.setattr(launch_replay_guard, "counted_candidate_code_rejects", lambda evs, identity: [])
    return events


def _plan(evidence: dict[str, Any], card: dict[str, Any], monkeypatch: pytest.MonkeyPatch, *,
          state: str = "validation_failed", error: str = "validation_failed:x") -> dict[str, Any]:
    monkeypatch.setattr(task_store, "get_task", lambda repo, task_id: dict(card))
    return auto.plan(Path("."), _METADATA, state, error, evidence)


def test_identical_changed_path_hashes_stop_relaunch(monkeypatch: pytest.MonkeyPatch, history: list) -> None:
    hashes = {"src/a.py": "deadbeef", "src/b.py": "cafef00d"}
    card = _card({"changed_path_hashes": dict(hashes)})
    evidence = _evidence(changed_path_hashes=dict(hashes))

    keys = _plan(evidence, card, monkeypatch)
    record = keys["validation_auto_rework"]

    assert record["decision"] == "manager"
    assert record["cause"] == "no_progress"
    assert record["signature"]
    assert "manager_callback_deferred" not in keys


def test_differing_changed_path_hash_still_auto_returns(monkeypatch: pytest.MonkeyPatch, history: list) -> None:
    predecessor_hashes = {"src/a.py": "deadbeef", "src/b.py": "cafef00d"}
    current_hashes = {"src/a.py": "deadbeef", "src/b.py": "00000000"}
    card = _card({"changed_path_hashes": dict(predecessor_hashes)})
    evidence = _evidence(changed_path_hashes=dict(current_hashes))

    keys = _plan(evidence, card, monkeypatch)
    record = keys["validation_auto_rework"]

    assert record["decision"] == "auto_return"
    assert record["prior_auto_returns"] == 0
    assert keys["manager_callback_deferred"] == {"reason": "validation_auto_rework"}


def test_missing_rework_predecessor_still_auto_returns(monkeypatch: pytest.MonkeyPatch, history: list) -> None:
    card = _card(None)
    evidence = _evidence(changed_path_hashes={"src/a.py": "deadbeef"})

    keys = _plan(evidence, card, monkeypatch)
    record = keys["validation_auto_rework"]

    assert record["decision"] == "auto_return"
    assert keys["manager_callback_deferred"] == {"reason": "validation_auto_rework"}


def test_empty_changed_path_hashes_still_auto_returns(monkeypatch: pytest.MonkeyPatch, history: list) -> None:
    card = _card({"changed_path_hashes": {}})
    evidence = _evidence(changed_path_hashes={})

    keys = _plan(evidence, card, monkeypatch)
    record = keys["validation_auto_rework"]

    assert record["decision"] == "auto_return"
    assert keys["manager_callback_deferred"] == {"reason": "validation_auto_rework"}
