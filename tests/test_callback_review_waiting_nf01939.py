"""NF-2026-01939: review_waiting drops dead QUALITY_REVIEW_* cards and shows why
a failed reviewer failed.

Field report (EntryLink, 0.12.83): every callback_wait reply listed ~20 old
QUALITY_REVIEW_* entries (other_origin; worker_failed / launch_failed /
cancelled / timed_out) and a fresh reviewer's worker_failed cause was invisible.

Run: python -m pytest -n 0 -q tests/test_callback_review_waiting_nf01939.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aiworkhub import callback_store, core, manager_callback_wait, task_store  # noqa: E402

SESSION = "SESSION-NF01939"
OTHER = "SESSION-OTHER-NF01939"


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
    task_id, *, status="blocked", substatus="worker_failed", origin=OTHER,
    worker_status=None, extra_card=None,
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
                json.dumps(card, sort_keys=True), now, now, origin, "",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_outbox(task_id, origin, state, transition):
    now = datetime.now(timezone.utc).isoformat()
    # A dead_letter row is only left alone once it has been recovered once.
    recovery_count = 1 if state == "dead_letter" else 0
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


def _dead_reviewer(task_id, substatus="worker_failed", origin=OTHER, **card):
    _insert_task(task_id, substatus=substatus, origin=origin, extra_card=card or None)
    _insert_outbox(task_id, origin, "delivered", substatus)


def test_terminal_other_origin_reviewer_is_not_in_review_waiting(repo):
    for index, substatus in enumerate(
        ("worker_failed", "launch_failed", "cancelled", "timed_out")
    ):
        _dead_reviewer(f"QUALITY_REVIEW_DEAD{index}", substatus)
    _insert_task("T-LIVE-OTHER", status="review", substatus="review_ready")
    _insert_outbox("T-LIVE-OTHER", OTHER, "delivered", "review_ready")

    # NF-2026-02255: foreign items are listed only on explicit opt-in.
    reply = core.claude_callback_wait(timeout_seconds=1, include_other_origin=True)

    listed = [row["task_id"] for row in reply["review_waiting"]]
    assert not any(task_id.startswith("QUALITY_REVIEW_") for task_id in listed)
    assert listed == ["T-LIVE-OTHER"]
    assert reply["review_waiting_hidden_terminal"] == 4


def test_dead_reviewers_do_not_consume_the_review_waiting_limit(repo):
    for index in range(25):
        _dead_reviewer(f"QUALITY_REVIEW_BULK{index:02d}")
    _insert_task("T-OLD-LIVE", status="review", substatus="review_ready")
    _insert_outbox("T-OLD-LIVE", OTHER, "delivered", "review_ready")

    # NF-2026-02255: foreign items are listed only on explicit opt-in.
    reply = core.claude_callback_wait(timeout_seconds=1, include_other_origin=True)

    assert [row["task_id"] for row in reply["review_waiting"]] == ["T-OLD-LIVE"]
    assert reply["review_waiting_hidden_terminal"] == 25


def test_non_reviewer_terminal_other_origin_card_stays_listed(repo):
    _dead_reviewer("T-WORKER-FAILED")

    # NF-2026-02255: foreign items are listed only on explicit opt-in.
    reply = core.claude_callback_wait(timeout_seconds=1, include_other_origin=True)

    assert [(row["task_id"], row["reason"]) for row in reply["review_waiting"]] == [
        ("T-WORKER-FAILED", "other_origin")
    ]
    assert "review_waiting_hidden_terminal" not in reply


def test_reviewer_with_a_finished_target_drops_out(repo):
    _insert_task("T-TARGET", status="finished", worker_status="done", substatus="review_ready")
    _insert_task(
        "QUALITY_REVIEW_FINISHEDTARGET", origin=SESSION,
        extra_card={"review_target_task_id": "T-TARGET"},
    )
    _insert_outbox("QUALITY_REVIEW_FINISHEDTARGET", SESSION, "dead_letter", "worker_failed")

    reply = core.claude_callback_wait(timeout_seconds=1)

    assert reply["review_waiting"] == []
    assert reply["review_waiting_hidden_terminal"] == 1


def test_own_failed_reviewer_with_open_target_stays_and_carries_its_cause(repo):
    _insert_task("T-OPEN", status="review", origin=SESSION, substatus="review_ready")
    _insert_outbox("T-OPEN", OTHER, "delivered", "review_ready")
    _insert_task(
        "QUALITY_REVIEW_FRESH", origin=SESSION,
        extra_card={
            "review_target_task_id": "T-OPEN",
            "terminal_reason": {"code": "quality_review_claim_binding_failed", "missing_cause": False},
            "terminal_review": {"evidence": {"first_error": "AuthError: token expired"}},
        },
    )
    _insert_outbox("QUALITY_REVIEW_FRESH", SESSION, "dead_letter", "worker_failed")

    reply = core.claude_callback_wait(timeout_seconds=1)

    by_task = {row["task_id"]: row for row in reply["review_waiting"]}
    assert by_task["QUALITY_REVIEW_FRESH"] == {
        "task_id": "QUALITY_REVIEW_FRESH", "status": "blocked",
        "terminal_substatus": "worker_failed", "reason": "no_callback_row",
        "first_error": "AuthError: token expired",
        "terminal_reason": "quality_review_claim_binding_failed",
    }
    assert "first_error" not in by_task["T-OPEN"]
    assert "review_waiting_hidden_terminal" not in reply


def test_failed_entry_without_recorded_cause_has_no_invented_cause(repo):
    _insert_task("QUALITY_REVIEW_NOCAUSE", origin=SESSION)
    _insert_outbox("QUALITY_REVIEW_NOCAUSE", SESSION, "dead_letter", "worker_failed")

    reply = core.claude_callback_wait(timeout_seconds=1)

    assert reply["review_waiting"] == [
        {
            "task_id": "QUALITY_REVIEW_NOCAUSE", "status": "blocked",
            "terminal_substatus": "worker_failed", "reason": "no_callback_row",
        }
    ]


def test_cause_text_is_bounded(repo):
    _insert_task(
        "QUALITY_REVIEW_LONG", origin=SESSION, extra_card={"first_error": "x" * 5000}
    )
    _insert_outbox("QUALITY_REVIEW_LONG", SESSION, "dead_letter", "worker_failed")

    entry = core.claude_callback_wait(timeout_seconds=1)["review_waiting"][0]

    assert len(entry["first_error"]) == manager_callback_wait.CALLBACK_WAIT_REVIEW_CAUSE_MAX_CHARS == 200
