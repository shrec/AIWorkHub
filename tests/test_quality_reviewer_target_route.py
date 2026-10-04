"""NF-2026-01095: a system quality reviewer is bound to its target's manager.

Measured: needfix-NF-2026-01093-r1 reached review_ready under a verified Claude
manager; 42 s later the AUTOMATIC reviewer launch, running in a background
reconciler, resolved the live route as a pending Codex route with no thread, so
create_task refused the reviewer card with
``callback_route_pending:codex_thread_id_not_observed``. The target's own
review_ready wake was deferred to the review chain, so nothing woke the manager.

Run: python -m pytest -q tests/test_quality_reviewer_target_route.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from aiworkhub import callback_store, core, task_store  # noqa: E402

_CLAUDE_SESSION = "9ea55703-f15e-4c35-8253-4e0c96516781"


def _pending_codex_route() -> dict[str, str]:
    return {
        "provider": "codex",
        "session_id": "episode_pending",
        "thread_id": "",
        "window_id": "window_01095",
        "callback_supported": "false",
        "route_state": "route_pending",
    }


def _insert_target(
    root: Path,
    task_id: str,
    *,
    origin: str = _CLAUDE_SESSION,
    provider: str = "claude",
    status: str = "review",
    worker_status: str = "review",
    deferred: bool = True,
) -> None:
    now = callback_store.utc_now()
    card = {
        "task_id": task_id,
        "runner": "worker_01095",
        "topic": "task_mcp",
        "status": status,
        "worker_status": worker_status,
        "callback_required": True,
        "coordinator_provider": provider,
        "origin_thread_id": origin,
        "manager_chat_session_id": "",
        "manager_route_state": "",
        "claim_epoch": 3,
        "terminal_substatus": "review_ready" if status == "review" else "",
        "terminal_review": {
            "recorded_at": now,
            "evidence": (
                {"manager_callback_deferred": {"reason": "system_quality_review_pending"}}
                if deferred else {}
            ),
        },
    }
    _readiness, db_path = task_store._require_ready(root)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, priority, "
            "objective, card_json, created_at, updated_at, origin_thread_id, archived_at) "
            "VALUES (?, 'worker_01095', 'task_mcp', ?, ?, 'normal', '', ?, ?, ?, ?, '')",
            (task_id, status, worker_status, json.dumps(card), now, now, origin),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    monkeypatch.setattr(core, "_claude_manager_identity", lambda: None)
    monkeypatch.setattr(core, "_codex_manager_identity", _pending_codex_route)
    monkeypatch.setattr(core, "_verify_coordinator_capability", lambda _runner: (True, "ok"))
    return root


def _create_reviewer(task_id: str, target: str | None) -> dict:
    return core.create_task(
        task_id=task_id,
        title=f"Independent correctness review for {target}",
        runner="worker_01095",
        topic="quality_review",
        objective="Review the exact candidate packet.",
        acceptance=["Exactly one authenticated quality_review_submit receipt"],
        allowed_writes=[],
        required_outputs=[],
        validation=[],
        priority="high",
        callback_required=True,
        task_type="research",
        read_only=True,
        review_target_task_id=target,
    )


def test_reviewer_card_inherits_the_target_route_under_a_pending_codex_route(repo):
    _insert_target(repo, "TARGET_01095")

    result = _create_reviewer("QUALITY_REVIEW_01095", "TARGET_01095")

    assert result["ok"] is True, result.get("stderr")
    card = task_store.get_task(repo, "QUALITY_REVIEW_01095")
    assert card["origin_thread_id"] == _CLAUDE_SESSION
    assert card["coordinator_provider"] == "claude"
    assert card["callback_supported"] is True


def test_reviewer_without_a_target_still_fails_closed_on_a_pending_route(repo):
    result = _create_reviewer("QUALITY_REVIEW_01095_NO_TARGET", None)

    assert result["ok"] is False
    assert result["stderr"] == "callback_route_pending:codex_thread_id_not_observed"


@pytest.mark.parametrize("origin", ["", "claude:window_1", "episode_pending"])
def test_a_target_without_a_callback_origin_grants_nothing(repo, origin):
    _insert_target(repo, "TARGET_01095_NO_ORIGIN", origin=origin)

    result = _create_reviewer("QUALITY_REVIEW_01095_NO_ORIGIN", "TARGET_01095_NO_ORIGIN")

    assert result["ok"] is False
    assert result["stderr"] == "callback_route_pending:codex_thread_id_not_observed"


def test_a_review_target_never_replaces_the_manager_identity_gate(repo, monkeypatch):
    _insert_target(repo, "TARGET_01095")
    monkeypatch.setattr(core, "_codex_manager_identity", lambda: None)

    result = _create_reviewer("QUALITY_REVIEW_01095_NO_MANAGER", "TARGET_01095")

    assert result["ok"] is False
    assert result["stderr"] == "manager_identity_required:task_create"


def _outbox(root: Path, task_id: str) -> list[dict]:
    _readiness, db_path = task_store._require_ready(root)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [
            dict(row) for row in conn.execute(
                "SELECT provider, origin_thread_id, transition, episode_id, state "
                "FROM callback_outbox WHERE task_id=?", (task_id,),
            )
        ]
    finally:
        conn.close()


def test_a_reviewer_create_failure_releases_the_deferred_manager_wake_once(repo):
    _insert_target(repo, "TARGET_01095")
    assert _outbox(repo, "TARGET_01095") == []

    reason = "quality_review_task_create_failed:callback_route_pending:x"
    assert core.release_review_wake("TARGET_01095", reason) is True
    assert core.release_review_wake("TARGET_01095", reason) is False

    assert _outbox(repo, "TARGET_01095") == [{
        "provider": "claude",
        "origin_thread_id": _CLAUDE_SESSION,
        "transition": "review_ready",
        "episode_id": "3",
        "state": "pending",
    }]
    events = [
        e for e in task_store.get_task_events(repo, "TARGET_01095")
        if e.get("event") == "review_wake_released"
    ]
    assert len(events) == 1


def test_no_wake_is_released_for_a_card_that_is_not_review_ready(repo):
    _insert_target(
        repo, "TARGET_01095_RUNNING", status="processing", worker_status="claimed",
    )

    assert core.release_review_wake("TARGET_01095_RUNNING", "reason") is False
    assert core.release_review_wake("TASK_THAT_DOES_NOT_EXIST", "reason") is False
    assert _outbox(repo, "TARGET_01095_RUNNING") == []
