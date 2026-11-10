"""NF-2026-02167: ``aiworkhub_task_reject_review(relaunch=true)`` launches the retry itself.

Every rework reject was followed by ``aiworkhub_agent_launch_task`` for the same
card and identity, a second manager call that decided nothing.  These tests run
the real ``server.aiworkhub_task_reject_review`` and ``core.reject_review`` over
a tmp repo and replace only ``server._launch_task_internal`` with a recorder.
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

from aiworkhub import (  # noqa: E402
    core,
    feature_settings,
    process_launcher,
    server,
    task_store,
)

NOW = "2026-11-10T00:00:00+00:00"
SESSION_ID = "019f5097-6dbe-7172-870a-945afc5f3bfb"
TASK_ID = "T_MAIN"
REQUEST_ID = "REQ_A"
REASON = "the rework must be launched in the same call"
NO_LESSON = "mechanical_change_only"


def _insert(root: Path, task_id: str, *, topic: str, card: dict[str, Any]) -> None:
    conn = sqlite3.connect(task_store.canonical_db_path(root))
    try:
        conn.execute(
            "INSERT INTO tasks (task_id,runner,topic,mode,status,worker_status,priority,"
            "objective,card_json,created_at,updated_at,claimed_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, "claude_coding", topic, "solo", "review", "review", "normal",
             "obj", json.dumps(card), NOW, NOW, "claude_coding"),
        )
        conn.commit()
    finally:
        conn.close()


def _rows(root: Path, table: str) -> list[dict[str, Any]]:
    conn = sqlite3.connect(task_store.canonical_db_path(root))
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]
    finally:
        conn.close()


def _reject_events(root: Path) -> list[dict[str, Any]]:
    return [row for row in _rows(root, "task_events") if row["event"] == "reject_review"]


class _Launches:
    """Records every ``_launch_task_internal`` call and replies as told."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.reply: Any = {"ok": True, "request_id": "REQ_LAUNCHED"}
        self.raises: Exception | None = None

    def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.raises is not None:
            raise self.raises
        return self.reply


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    feature_settings.update(root, changes={"context_graph": True}, expected_revision=0)
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    monkeypatch.setattr(core, "manager_bootstrap", lambda: {
        "ok": True,
        "role": "manager",
        "provider": "codex",
        "repo": str(root),
        "manager_route": {"provider": "codex", "session_id": SESSION_ID, "thread_id": SESSION_ID},
    })
    monkeypatch.setattr(core, "writes_allowed", lambda: True)
    monkeypatch.setattr(core, "repo_root", lambda: root)
    # The coordinator token file needs chmod 0600, which the sandbox denies;
    # the capability itself is not what this regression is about.
    monkeypatch.setattr(
        core, "_verify_coordinator_capability", lambda _runner: (True, "test_coordinator")
    )
    ledger_dir = root / "process_ledger"
    ledger_dir.mkdir()
    manager = process_launcher.ProcessManager(
        repo=root,
        process_log_path=ledger_dir / "processes.jsonl",
        process_dir=ledger_dir / "requests",
    )
    monkeypatch.setattr(process_launcher, "default_manager", lambda: manager)
    _insert(root, TASK_ID, topic="coding", card={
        "allowed_writes": ["src/aiworkhub/process_launcher.py"],
        "terminal_review": {
            "substatus": "review_ready",
            "evidence": {"request_identity": {
                "request_id": REQUEST_ID, "task_id": TASK_ID,
                "runner": "claude_coding", "topic": "coding",
            }},
        },
    })
    return root


@pytest.fixture
def launches(monkeypatch) -> _Launches:
    recorder = _Launches()
    monkeypatch.setattr(server, "_launch_task_internal", recorder)
    return recorder


def test_relaunch_rejects_then_launches_once_with_the_cards_own_identity(repo, launches):
    reply = server.aiworkhub_task_reject_review(
        task_id=TASK_ID, reason=REASON, relaunch=True, no_new_lesson_reason=NO_LESSON,
    )

    assert reply["ok"] is True, reply
    assert "learning_disposition" in reply
    assert task_store.get_task(repo, TASK_ID)["status"] == "pending"
    assert len(_reject_events(repo)) == 1
    assert launches.calls == [{"task_id": TASK_ID, "adapter_id": None}]
    assert reply["relaunch"] == {"ok": True, "request_id": "REQ_LAUNCHED"}


def test_relaunch_adapter_id_is_passed_through_trimmed(repo, launches):
    reply = server.aiworkhub_task_reject_review(
        task_id=TASK_ID, reason=REASON, relaunch=True, relaunch_adapter_id=" claude_coding ",
    )

    assert reply["ok"] is True, reply
    assert launches.calls == [{"task_id": TASK_ID, "adapter_id": "claude_coding"}]


def test_default_reply_has_no_relaunch_key_and_launches_nothing(repo, launches):
    reply = server.aiworkhub_task_reject_review(task_id=TASK_ID, reason=REASON)

    assert reply["ok"] is True, reply
    assert "relaunch" not in reply
    assert launches.calls == []
    assert task_store.get_task(repo, TASK_ID)["status"] == "pending"


def test_relaunch_adapter_id_alone_does_not_launch(repo, launches):
    reply = server.aiworkhub_task_reject_review(
        task_id=TASK_ID, reason=REASON, relaunch_adapter_id="claude_coding",
    )

    assert reply["ok"] is True, reply
    assert "relaunch" not in reply
    assert launches.calls == []


def _assert_refused_before_anything_changed(repo: Path, launches: _Launches, reply: dict) -> None:
    assert reply["ok"] is False
    assert "relaunch" not in reply
    assert launches.calls == []
    assert _reject_events(repo) == []
    assert task_store.get_task(repo, TASK_ID)["status"] == "review"


@pytest.mark.parametrize("disposition", ["blocked", "archived", "superseded"])
def test_relaunch_is_refused_unless_the_reject_goes_to_pending(repo, launches, disposition):
    reply = server.aiworkhub_task_reject_review(
        task_id=TASK_ID, reason=REASON, to=disposition, relaunch=True,
    )

    assert reply == {"ok": False, "error": "relaunch_requires_pending"}
    _assert_refused_before_anything_changed(repo, launches, reply)


@pytest.mark.parametrize("category", ["base_drift", "validation_environment"])
@pytest.mark.parametrize("disposition", ["pending", "blocked"])
def test_relaunch_is_refused_with_a_category_that_replays_automatically(
    repo, launches, category, disposition
):
    reply = server.aiworkhub_task_reject_review(
        task_id=TASK_ID, reason=REASON, to=disposition, failure_category=category,
        relaunch=True,
    )

    expected = (
        "relaunch_conflicts_with_automatic_replay"
        if disposition == "pending"
        else "relaunch_requires_pending"
    )
    assert reply == {"ok": False, "error": expected}
    _assert_refused_before_anything_changed(repo, launches, reply)


def test_a_launch_replying_not_ok_leaves_the_reject_committed(repo, launches):
    launches.reply = {"ok": False, "error": "launch_blocked"}

    reply = server.aiworkhub_task_reject_review(
        task_id=TASK_ID, reason=REASON, relaunch=True,
    )

    assert reply["ok"] is True, reply
    assert reply["relaunch"] == {"ok": False, "error": "launch_blocked"}
    assert len(launches.calls) == 1
    assert len(_reject_events(repo)) == 1
    assert task_store.get_task(repo, TASK_ID)["status"] == "pending"


def test_a_launch_without_an_ok_key_is_not_reported_as_launched(repo, launches):
    launches.reply = {"request_id": "REQ_LAUNCHED"}

    reply = server.aiworkhub_task_reject_review(
        task_id=TASK_ID, reason=REASON, relaunch=True,
    )

    assert reply["ok"] is True, reply
    assert reply["relaunch"] == {"request_id": "REQ_LAUNCHED", "ok": False}


def test_a_launch_that_raises_leaves_the_reject_committed(repo, launches):
    launches.raises = RuntimeError("provider exploded")

    reply = server.aiworkhub_task_reject_review(
        task_id=TASK_ID, reason=REASON, relaunch=True,
    )

    assert reply["ok"] is True, reply
    assert reply["relaunch"] == {"ok": False, "error": "relaunch_raised:RuntimeError"}
    assert "provider exploded" not in json.dumps(reply)
    assert len(launches.calls) == 1
    assert len(_reject_events(repo)) == 1
    assert task_store.get_task(repo, TASK_ID)["status"] == "pending"


def test_a_non_dict_launch_reply_is_reported_invalid(repo, launches):
    launches.reply = "launched"

    reply = server.aiworkhub_task_reject_review(
        task_id=TASK_ID, reason=REASON, relaunch=True,
    )

    assert reply["ok"] is True, reply
    assert reply["relaunch"] == {"ok": False, "error": "relaunch_reply_invalid"}
    assert task_store.get_task(repo, TASK_ID)["status"] == "pending"
