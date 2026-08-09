"""Regression tests for blocked terminal_substatus=finalize_failed cards."""

from __future__ import annotations

import json
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import completion_inbox  # noqa: E402


def _entry_by_task_id(entries, task_id):
    return next((e for e in entries if e.get("task_id") == task_id), None)


def test_blocked_finalize_failed_minimal_shape():
    card = {
        "task_id": "BLOCKED_FF_01",
        "runner": "fixture_runner_blk",
        "claimed_by": "fixture_runner_blk",
        "topic": "task_mcp",
        "priority": "normal",
        "objective": "fixture finalize_failed blocked task",
        "allowed_writes": ["src/a.py"],
        "updated_at": "2026-08-09T12:00:00+00:00",
        "terminal_substatus": "finalize_failed",
        "launch_request_id": "req_launch_01",
        "blocker_reason": "finalize step crashed",
        "workspace_retained": False,
    }

    def stub_list_tasks(status="pending", topic=None, limit=80):
        if status != "blocked":
            return {"stdout": "", "returncode": 0}
        return {
            "stdout": f"[blocked] [task_mcp] [fixture_runner_blk] {card['task_id']}",
            "returncode": 0,
        }

    def stub_show_task(task_id):
        return {"stdout": json.dumps(card), "returncode": 0}

    result = completion_inbox.build_completion_inbox(
        topic="task_mcp",
        limit=50,
        _list_tasks=stub_list_tasks,
        _show_task=stub_show_task,
    )

    failures = [
        e for e in result["operational_failures"]
        if e["task_id"] == card["task_id"]
    ]
    assert len(failures) == 1
    entry = failures[0]

    assert entry["terminal_substatus"] == "finalize_failed"
    assert entry["request_id"] == "req_launch_01"
    assert entry["operational_error"] == "finalize step crashed"
    assert entry["launch_request_id"] == "req_launch_01"
    assert entry["workspace_retained"] is False

    assert _entry_by_task_id(result["review_queue"], card["task_id"]) is None
    assert _entry_by_task_id(result["stale_processing"], card["task_id"]) is None

    counts = result["counts"]
    assert counts["blocked_scanned"] == 1
    assert counts["operational_failures"] == 1
    assert counts["review_queue"] == 0
    assert counts["stale_processing"] == 0


def test_blocked_finalize_failed_terminal_failure_evidence():
    card = {
        "task_id": "BLOCKED_FF_02",
        "runner": "fixture_runner_blk2",
        "topic": "task_mcp",
        "updated_at": "2026-08-09T13:00:00+00:00",
        "terminal_substatus": "finalize_failed",
        "launch_request_id": "req_launch_02",
        "terminal_failure": {
            "substatus": "finalize_failed",
            "evidence": {
                "request_id": "req_failure_ev_02",
                "error": "terminal_failure evidence error text",
            },
        },
        "workspace_retained": True,
    }

    def stub_list_tasks(status="pending", topic=None, limit=80):
        if status != "blocked":
            return {"stdout": "", "returncode": 0}
        return {
            "stdout": f"[blocked] [task_mcp] [fixture_runner_blk2] {card['task_id']}",
            "returncode": 0,
        }

    def stub_show_task(task_id):
        return {"stdout": json.dumps(card), "returncode": 0}

    result = completion_inbox.build_completion_inbox(
        topic="task_mcp",
        limit=50,
        _list_tasks=stub_list_tasks,
        _show_task=stub_show_task,
    )

    entry = _entry_by_task_id(result["operational_failures"], card["task_id"])
    assert entry is not None
    assert entry["request_id"] == "req_failure_ev_02"
    assert entry["operational_error"] == "terminal_failure evidence error text"
    assert entry["workspace_retained"] is True


def test_blocked_non_finalize_failed_excluded():
    card = {
        "task_id": "BLOCKED_OTHER_03",
        "runner": "fixture_runner_blk3",
        "topic": "task_mcp",
        "updated_at": "2026-08-09T14:00:00+00:00",
        "terminal_substatus": "blocked_manual",
        "blocker_reason": "manual hold",
    }

    def stub_list_tasks(status="pending", topic=None, limit=80):
        if status != "blocked":
            return {"stdout": "", "returncode": 0}
        return {
            "stdout": f"[blocked] [task_mcp] [fixture_runner_blk3] {card['task_id']}",
            "returncode": 0,
        }

    def stub_show_task(task_id):
        return {"stdout": json.dumps(card), "returncode": 0}

    result = completion_inbox.build_completion_inbox(
        topic="task_mcp",
        limit=50,
        _list_tasks=stub_list_tasks,
        _show_task=stub_show_task,
    )

    assert _entry_by_task_id(result["operational_failures"], card["task_id"]) is None
    assert result["counts"]["blocked_scanned"] == 1
    assert result["counts"]["operational_failures"] == 0
