"""NF-2026-01859: claude_callback_wait drops stale callbacks and names the review
cards it cannot deliver.

Field bugs: (1) the first batch of a fresh session belonged to a card that was
already cancelled; (2) a card sat review_ready while the wait returned
``timeout_no_callback`` with an empty liveness list.  Delivery scope is unchanged
(a session never claims another origin's callback); review_waiting is visibility.

Run: python -m pytest -q tests/test_callback_stale_and_review_waiting_nf01859.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aiworkhub import callback_store, core, task_store  # noqa: E402

SESSION = "SESSION-NF01859"
OTHER = "SESSION-OTHER-NF01859"


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


def _insert_task(
    task_id, *, status="review", worker_status=None, substatus="review_ready",
    origin=SESSION, archived_at="", extra_card=None,
):
    now = datetime.now(timezone.utc).isoformat()
    card = {
        "schema_id": "aiworkhub.machine_task_card.v1",
        "task_id": task_id, "status": status,
        "worker_status": worker_status or status,
        "runner": "r", "topic": "t", "origin_thread_id": origin,
        "coordinator_provider": "claude", "terminal_substatus": substatus,
        "claim_epoch": 1, **(extra_card or {}),
    }
    conn = core._canonical_connect()
    try:
        callback_store._ensure_tasks_origin_thread_column(conn)
        callback_store.init_db(conn)
        conn.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, priority, "
            "objective, card_json, created_at, updated_at, origin_thread_id, archived_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                task_id, "r", "t", status, worker_status or status, "high", "",
                json.dumps(card, sort_keys=True), now, now, origin, archived_at,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_outbox(task_id, origin, state, *, transition="review_ready", recovery_count=0):
    now = datetime.now(timezone.utc).isoformat()
    conn = core._canonical_connect()
    try:
        callback_store.init_db(conn)
        conn.execute(
            "INSERT INTO callback_outbox(task_id,provider,origin_thread_id,transition,episode_id,"
            "state,created_at,updated_at,batch_id,recovery_count) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (task_id, "claude", origin, transition, "1", state, now, now, "", recovery_count),
        )
        conn.commit()
    finally:
        conn.close()


def _scalar(sql, params=()):
    conn = core._canonical_connect()
    try:
        return conn.execute(sql, params).fetchone()[0]
    finally:
        conn.close()


def test_batch_of_only_a_cancelled_card_is_acknowledged_skipped_and_reported(repo):
    _insert_task("T-CANCELLED", status="blocked", substatus="cancelled")

    reply = core.claude_callback_wait(timeout_seconds=1)

    assert reply["status"] == "timeout_no_callback"
    assert [
        (row["task_id"], row["stale_reason"]) for row in reply["skipped_stale"]
    ] == [("T-CANCELLED", "task_cancelled")]
    assert reply["skipped_stale"][0]["batch_id"]
    assert _scalar("SELECT state FROM callback_batches WHERE batch_id=?", (reply["skipped_stale"][0]["batch_id"],)) == "delivered"
    assert _scalar("SELECT state FROM callback_outbox WHERE task_id='T-CANCELLED'") == "delivered"


def test_mixed_batch_returns_both_with_stale_member_labelled(repo):
    _insert_task("T-STALE", status="blocked", substatus="cancelled")
    _insert_task("T-LIVE", status="review", substatus="review_ready")

    reply = core.claude_callback_wait(timeout_seconds=1)

    assert reply["status"] == "callback_ready"
    by_task = {member["task_id"]: member for member in reply["members"]}
    assert set(by_task) == {"T-STALE", "T-LIVE"}
    assert by_task["T-STALE"]["actionable"] is False
    assert by_task["T-STALE"]["stale_reason"] == "task_cancelled"
    assert "stale_reason" not in by_task["T-LIVE"]
    assert by_task["T-LIVE"].get("actionable", True) is True
    assert "skipped_stale" not in reply


@pytest.mark.parametrize(
    ("kwargs", "transition", "expected"),
    [
        ({"status": "archived"}, "review_ready", "task_archived"),
        ({"status": "review", "archived_at": "2026-10-01T00:00:00+00:00"}, "review_ready", "task_archived"),
        ({"status": "superseded"}, "review_ready", "task_superseded"),
        ({"status": "finished"}, "review_ready", "task_finished"),
        ({"status": "pending", "worker_status": "done"}, "review_ready", "task_finished"),
        ({"status": "cancelled"}, "review_ready", "task_cancelled"),
        ({"status": "pending", "worker_status": "unclaimed"}, "review_ready", "task_pending"),
        ({"status": "processing", "worker_status": "claimed"}, "review_ready", "task_processing"),
        ({"status": "blocked", "substatus": "blocked"}, "review_ready", "task_transition_changed"),
        ({"status": "review"}, "review_ready", ""),
    ],
)
def test_member_stale_reason_classification(repo, kwargs, transition, expected):
    _insert_task("T-X", **kwargs)
    conn = core._canonical_connect(readonly=True)
    try:
        member = {"task_id": "T-X", "transition": transition, "episode_id": "1"}
        assert core._callback_member_stale_reason(conn, member) == expected
    finally:
        conn.close()


def test_review_ready_card_delivered_to_another_origin_is_named_in_review_waiting(repo):
    """Reproduced root cause of the empty-liveness timeout: the card's callback
    was already delivered to a different origin, so this session's seed skips it
    (NF-2026-01548) and nothing this session launched exists to list."""
    _insert_task("T-OTHER", status="review", origin=OTHER)
    _insert_outbox("T-OTHER", OTHER, "delivered")

    reply = core.claude_callback_wait(timeout_seconds=1)

    assert reply["status"] == "timeout_no_callback"
    assert reply["liveness"]["requests"] == []
    assert reply["review_waiting"] == [
        {
            "task_id": "T-OTHER", "status": "review",
            "terminal_substatus": "review_ready", "reason": "other_origin",
        }
    ]
    # Visibility only: the current session did not claim or re-seed it.
    assert _scalar("SELECT COUNT(*) FROM callback_batches") == 0
    assert _scalar("SELECT COUNT(*) FROM callback_outbox WHERE task_id='T-OTHER'") == 1


def test_young_deferred_review_chain_is_named(repo):
    _insert_task(
        "T-DEFERRED", status="review", origin=SESSION,
        extra_card={
            "terminal_review": {
                "recorded_at": datetime.now(timezone.utc).isoformat(),
                "evidence": {"manager_callback_deferred": {"reason": "quality_review_chain"}},
            }
        },
    )

    reply = core.claude_callback_wait(timeout_seconds=1)

    assert reply["status"] == "timeout_no_callback"
    assert [(row["task_id"], row["reason"]) for row in reply["review_waiting"]] == [
        ("T-DEFERRED", "deferred_review_chain")
    ]


def test_review_card_without_a_deliverable_row_is_named_no_callback_row(repo):
    _insert_task("T-NOROW", status="review", origin=SESSION)
    _insert_outbox("T-NOROW", SESSION, "dead_letter", recovery_count=1)

    reply = core.claude_callback_wait(timeout_seconds=1)

    assert [(row["task_id"], row["reason"]) for row in reply["review_waiting"]] == [
        ("T-NOROW", "no_callback_row")
    ]


def test_other_providers_cards_are_not_listed(repo):
    _insert_task("T-CODEX", status="review", origin=OTHER, extra_card={"coordinator_provider": "codex"})

    assert core.claude_callback_wait(timeout_seconds=1)["review_waiting"] == []


def test_review_waiting_is_bounded_to_twenty_rows(repo):
    for index in range(25):
        task_id = f"T-BULK-{index:02d}"
        _insert_task(task_id, status="review", origin=OTHER)
        _insert_outbox(task_id, OTHER, "delivered")

    reply = core.claude_callback_wait(timeout_seconds=1)

    assert len(reply["review_waiting"]) == core.CALLBACK_WAIT_REVIEW_WAITING_LIMIT == 20
    listed = [row["task_id"] for row in reply["review_waiting"]]
    assert listed[0] == "T-BULK-24"
    assert "T-BULK-00" not in listed


def test_watch_changed_carries_review_waiting(repo, monkeypatch):
    _insert_task("T-OTHER-W", status="review", origin=OTHER)
    _insert_outbox("T-OTHER-W", OTHER, "delivered")
    request_id = "req_alpha_0001"
    snapshots = [
        [{"request_id": request_id, "task_id": "T-W", "state": "running",
          "preparation_phase": None, "exit_code": None, "timestamp": "2026-10-28T10:00:00+00:00"}],
        [{"request_id": request_id, "task_id": "T-W", "state": "exited",
          "preparation_phase": None, "exit_code": 0, "timestamp": "2026-10-28T10:00:05+00:00"}],
    ]
    calls = {"n": 0}

    def rows(_session_id, _watch):
        index = min(calls["n"], len(snapshots) - 1)
        calls["n"] += 1
        return [dict(row) for row in snapshots[index]]

    monkeypatch.setattr(core, "_claude_session_process_rows", rows)

    reply = core.claude_callback_wait(timeout_seconds=5, watch_request_ids=[request_id])

    assert reply["status"] == "watch_changed"
    assert [(row["task_id"], row["reason"]) for row in reply["review_waiting"]] == [
        ("T-OTHER-W", "other_origin")
    ]
