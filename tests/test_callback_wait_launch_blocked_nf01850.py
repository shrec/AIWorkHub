"""NF-2026-01850: claude_callback_wait wakes on a pre-claim launch blocker.

A pending, unclaimed card that gets ``operational_blocker`` emits no callback
(the outbox cannot carry a non-terminal state), so the wait names it itself.

Run: python -m pytest -q tests/test_callback_wait_launch_blocked_nf01850.py
"""

from __future__ import annotations

import json
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aiworkhub import callback_store, core, manager_callback_wait, task_store  # noqa: E402

SESSION = "SESSION-NF01850"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.setattr(core, "repo_root", lambda: root)
    monkeypatch.setattr(core, "CALLBACK_WAIT_WATCH_POLL_SECONDS", 0.05)
    monkeypatch.delenv(core.CALLBACK_WAIT_HOST_TIMEOUT_MS_ENV, raising=False)
    monkeypatch.delenv(core.CALLBACK_WAIT_MAX_SECONDS_ENV, raising=False)
    monkeypatch.setattr(
        core, "_claude_manager_identity",
        lambda: {"provider": "claude", "session_id": SESSION},
    )
    return root


def _blocker(observed_at="2026-10-30T10:00:00+00:00", **extra):
    return {
        "kind": "launch_blocked", "adapter_id": "claude_cli",
        "reason": "quota_exhausted", "observed_at": observed_at, **extra,
    }


def _card(task_id, *, status, blocker=None, provider="claude"):
    card = {
        "schema_id": "aiworkhub.machine_task_card.v1",
        "task_id": task_id, "status": status, "worker_status": "unclaimed",
        "runner": "r", "topic": "t", "origin_thread_id": SESSION,
        "coordinator_provider": provider, "claim_epoch": 0,
    }
    if blocker is not None:
        card["operational_blocker"] = blocker
    return card


def _insert_task(task_id, *, status="pending", blocker=None, provider="claude", archived_at=""):
    now = datetime.now(timezone.utc).isoformat()
    card = _card(task_id, status=status, blocker=blocker, provider=provider)
    conn = core._canonical_connect()
    try:
        callback_store._ensure_tasks_origin_thread_column(conn)
        callback_store.init_db(conn)
        conn.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, priority, "
            "objective, card_json, created_at, updated_at, origin_thread_id, archived_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                task_id, "r", "t", status, "unclaimed", "high", "",
                json.dumps(card, sort_keys=True), now, now, SESSION, archived_at,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _set_blocker(task_id, blocker):
    conn = core._canonical_connect()
    try:
        row = conn.execute("SELECT card_json FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        card = json.loads(row["card_json"])
        card["operational_blocker"] = blocker
        conn.execute(
            "UPDATE tasks SET card_json=? WHERE task_id=?",
            (json.dumps(card, sort_keys=True), task_id),
        )
        conn.commit()
    finally:
        conn.close()


def _blocker_later(task_id, blocker, delay=0.3):
    thread = threading.Thread(
        target=lambda: (time.sleep(delay), _set_blocker(task_id, blocker)), daemon=True
    )
    thread.start()
    return thread


def test_blocker_appearing_during_wait_wakes_with_launch_blocked(repo):
    _insert_task("T-PEND")
    thread = _blocker_later("T-PEND", _blocker(request_id="req-1"))

    started = time.monotonic()
    reply = core.claude_callback_wait(timeout_seconds=20)
    thread.join()

    assert time.monotonic() - started < 15
    assert reply["ok"] is True
    assert reply["status"] == "launch_blocked"
    assert reply["provider"] == "claude"
    assert reply["new_launch_blocked"] == [
        {
            "task_id": "T-PEND", "kind": "launch_blocked", "reason": "quota_exhausted",
            "adapter_id": "claude_cli", "observed_at": "2026-10-30T10:00:00+00:00",
            "request_id": "req-1",
        }
    ]
    assert reply["launch_blocked"] == reply["new_launch_blocked"]
    for key in ("ack", "liveness", "review_waiting"):
        assert key in reply


def test_wakes_without_watch_ids_and_on_a_changed_observed_at(repo):
    _insert_task("T-REPEAT", blocker=_blocker("2026-10-30T10:00:00+00:00"))
    thread = _blocker_later("T-REPEAT", _blocker("2026-10-30T10:05:00+00:00", kind="quota_circuit_reroute"))

    reply = core.claude_callback_wait(timeout_seconds=20)
    thread.join()

    assert reply["status"] == "launch_blocked"
    assert [(row["task_id"], row["kind"], row["observed_at"]) for row in reply["new_launch_blocked"]] == [
        ("T-REPEAT", "quota_circuit_reroute", "2026-10-30T10:05:00+00:00")
    ]


def test_blocker_present_at_wait_start_does_not_wake_but_is_listed_on_timeout(repo):
    _insert_task("T-OLD", blocker=_blocker())

    reply = core.claude_callback_wait(timeout_seconds=1)

    assert reply["status"] == "timeout_no_callback"
    assert [row["task_id"] for row in reply["launch_blocked"]] == ["T-OLD"]
    assert "new_launch_blocked" not in reply


def test_blocker_between_waits_is_listed_in_watch_changed(repo, monkeypatch):
    _insert_task("T-BETWEEN", blocker=_blocker())
    request_id = "req_alpha_0001"
    snapshots = [
        [{"request_id": request_id, "task_id": "T-W", "state": "running",
          "preparation_phase": None, "exit_code": None, "timestamp": "2026-10-30T10:00:00+00:00"}],
        [{"request_id": request_id, "task_id": "T-W", "state": "exited",
          "preparation_phase": None, "exit_code": 0, "timestamp": "2026-10-30T10:00:05+00:00"}],
    ]
    calls = {"n": 0}

    def rows(_session_id, _watch):
        index = min(calls["n"], len(snapshots) - 1)
        calls["n"] += 1
        return [dict(row) for row in snapshots[index]]

    monkeypatch.setattr(core, "_claude_session_process_rows", rows)

    reply = core.claude_callback_wait(timeout_seconds=5, watch_request_ids=[request_id])

    assert reply["status"] == "watch_changed"
    assert [row["task_id"] for row in reply["launch_blocked"]] == ["T-BETWEEN"]


def test_other_provider_non_pending_and_archived_cards_are_not_listed(repo):
    _insert_task("T-CODEX", blocker=_blocker(), provider="codex")
    _insert_task("T-PROCESSING", status="processing", blocker=_blocker())
    _insert_task("T-ARCHIVED", blocker=_blocker(), archived_at="2026-10-01T00:00:00+00:00")
    _insert_task("T-NOBLOCK")
    _insert_task("T-BADBLOCK", blocker=_blocker())
    _set_blocker("T-BADBLOCK", "not-a-dict")

    assert core.claude_callback_wait(timeout_seconds=1)["launch_blocked"] == []


def test_launch_blocked_listing_is_bounded_to_review_waiting_limit(repo):
    for index in range(25):
        _insert_task(f"T-BULK-{index:02d}", blocker=_blocker())

    reply = core.claude_callback_wait(timeout_seconds=1)

    assert len(reply["launch_blocked"]) == core.CALLBACK_WAIT_REVIEW_WAITING_LIMIT == 20


def test_helper_reports_error_name_instead_of_raising(repo, monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("db gone")

    monkeypatch.setattr(core, "_canonical_connect", boom)

    assert manager_callback_wait._callback_launch_blocked("claude") == {
        "launch_blocked": [], "launch_blocked_error": "RuntimeError",
    }
