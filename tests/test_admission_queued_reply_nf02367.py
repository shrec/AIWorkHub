"""NF-2026-02367: an admission-queued launch reply is a non-failure.

``server._launch_task_internal`` runs for real over a tmp repo; only
``process_launcher.default_manager`` is replaced with a fake whose ``launch``
returns a canned reply, so no provider CLI or manager loop ever starts.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from aiworkhub import core, launch_admission, process_launcher, server, task_store  # noqa: E402

NOW = "2026-11-20T00:00:00+00:00"
MEMORY_REASON = 'memory_launch_capacity_denied:{"reason":"memory_capacity_insufficient"}'


def _insert_pending(root: Path, task_id: str, runner: str = "claude_coding") -> None:
    card = {"task_id": task_id, "runner": runner, "topic": "task_mcp"}
    conn = sqlite3.connect(task_store.canonical_db_path(root))
    try:
        conn.execute(
            "INSERT INTO tasks (task_id, runner, topic, status, worker_status, "
            "claimed_by, card_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (task_id, runner, "task_mcp", "pending", "unclaimed", None,
             json.dumps(card), NOW, NOW),
        )
        conn.commit()
    finally:
        conn.close()


def _set_due(root: Path, task_id: str, due: str) -> None:
    conn = sqlite3.connect(task_store.canonical_db_path(root))
    try:
        (raw,) = conn.execute(
            "SELECT card_json FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        card = json.loads(raw)
        card["retry_not_before"] = due
        conn.execute(
            "UPDATE tasks SET card_json=? WHERE task_id=?", (json.dumps(card), task_id)
        )
        conn.commit()
    finally:
        conn.close()


def _queued_reply(task_id: str, reason: str) -> dict[str, Any]:
    return {
        **launch_admission.queued_receipt_fields(reason),
        "ok": False,
        "launch_implemented": True,
        "launch_enabled": True,
        "request_id": f"REQ_{task_id}",
        "task_id": task_id,
        "state": "admission_queued",
        "blocked_reason": reason,
        "shell": False,
    }


class _FakeManager:
    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self.reply: dict[str, Any] = {}
        self.calls = 0

    def launch(self, **_kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        return dict(self.reply)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    return root


@pytest.fixture
def manager(repo: Path, monkeypatch: pytest.MonkeyPatch) -> _FakeManager:
    fake = _FakeManager(repo)
    monkeypatch.setattr(process_launcher, "default_manager", lambda: fake)
    monkeypatch.setattr(core, "scrub_coordinator_capability_from_environment", lambda: None)
    return fake


def _defer(repo: Path, task_id: str, reason: str = MEMORY_REASON) -> None:
    _insert_pending(repo, task_id)
    assert launch_admission.defer_card(repo, task_id, "claude_coding", reason) == (
        True,
        "pending",
    )


def test_queued_reply_is_ok_and_queued(repo, manager):
    _defer(repo, "T1")
    manager.reply = _queued_reply("T1", MEMORY_REASON)
    reply = server._launch_task_internal(task_id="T1")
    assert reply["ok"] is True
    assert reply["queued"] is True
    assert reply["state"] == "admission_queued"
    assert reply["queue_position_basis"] == "retry_not_before_order"
    assert reply["relaunch"] == {
        "automatic": True,
        "by": "transient_retry_reconciler",
        "interval_seconds": 30,
    }
    assert "queue_position_error" not in reply


def test_waits_for_maps_the_admission_resource(repo, manager):
    _defer(repo, "T1")
    manager.reply = _queued_reply("T1", MEMORY_REASON)
    assert server._launch_task_internal(task_id="T1")["waits_for"] == {
        "resource": "host_memory",
        "reason": "memory_capacity_insufficient",
    }
    manager.reply = _queued_reply("T1", "concurrency_limit_reached")
    assert server._launch_task_internal(task_id="T1")["waits_for"] == {
        "resource": "concurrency_slot",
        "reason": "concurrency_limit_reached",
    }


def test_queue_position_orders_by_retry_not_before_then_task_id(repo, manager):
    _defer(repo, "T_B")
    _defer(repo, "T_A")
    _set_due(repo, "T_B", "2026-11-20T00:00:10+00:00")
    _set_due(repo, "T_A", "2026-11-20T00:00:20+00:00")
    assert [e["task_id"] for e in task_store.admission_deferred_queue(repo)] == [
        "T_B",
        "T_A",
    ]
    manager.reply = _queued_reply("T_A", MEMORY_REASON)
    reply = server._launch_task_internal(task_id="T_A")
    assert (reply["queue_position"], reply["queue_length"]) == (2, 2)

    _set_due(repo, "T_A", "2026-11-20T00:00:10+00:00")
    reply = server._launch_task_internal(task_id="T_A")
    assert (reply["queue_position"], reply["queue_length"]) == (1, 2)


def test_start_notification_names_the_status_tool(repo, manager):
    _defer(repo, "T1")
    manager.reply = _queued_reply("T1", MEMORY_REASON)
    reply = server._launch_task_internal(task_id="T1")
    assert reply["start_notification"] == {
        "tool": "aiworkhub_agent_task_status",
        "task_id": "T1",
    }


def test_non_queued_refusal_stays_byte_identical(repo, manager):
    refused = {
        "ok": False,
        "request_id": "REQ_X",
        "task_id": "T1",
        "state": "blocked",
        "blocked_reason": "duplicate_live_task:abc",
        "shell": False,
    }
    manager.reply = refused
    reply = server._launch_task_internal(task_id="T1")
    stall = process_launcher._stall_policy_fields()
    assert reply == {
        **refused,
        "timeout_seconds": 7200,
        "timeout_enforced": False,
        **stall,
    }
    assert reply["ok"] is False
    assert "queued" not in reply and "start_notification" not in reply

    started = {"ok": True, "request_id": "REQ_S", "task_id": "T1", "state": "started"}
    manager.reply = started
    assert server._launch_task_internal(task_id="T1") == {
        **started,
        "timeout_seconds": 7200,
        "timeout_enforced": False,
        **stall,
    }


def test_relaunch_reply_carries_the_queued_fields(repo, manager):
    _defer(repo, "T1")
    manager.reply = _queued_reply("T1", "concurrency_limit_reached")
    reply = server._relaunch_launch_reply("T1", None)
    assert reply["ok"] is True
    assert reply["queued"] is True
    assert reply["waits_for"]["resource"] == "concurrency_slot"
    assert (reply["queue_position"], reply["queue_length"]) == (1, 1)
    assert reply["queue_position_basis"] == "retry_not_before_order"
    assert reply["relaunch"]["by"] == "transient_retry_reconciler"
    assert reply["start_notification"] == {
        "tool": "aiworkhub_agent_task_status",
        "task_id": "T1",
    }


def test_unknown_queue_membership_is_named_not_guessed(repo, manager):
    manager.reply = _queued_reply("T_GONE", MEMORY_REASON)
    reply = server._launch_task_internal(task_id="T_GONE")
    assert reply["queued"] is True
    assert reply["queue_position"] is None
    assert reply["queue_position_error"] == "task_not_in_admission_queue"


def test_waits_for_never_guesses_an_unknown_resource():
    assert launch_admission.waits_for("")["resource"] == "unknown"
    assert launch_admission.waits_for("other_refusal")["resource"] == "unknown"
