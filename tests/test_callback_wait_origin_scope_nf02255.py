"""NF-2026-02255: callback wait lists only this origin's review_waiting items.

Every ``claude_callback_wait`` used to return blocked cards owned by other
manager origins (QR-gate-repair-*, audit-mobile-*, ...), so a manager could not
tell what its own session owned.  By default other-origin items are now folded
into ``other_origin_count``; ``include_other_origin=True`` lists them again, and
an item this session owns is never hidden.

Run: python -m pytest -q -p no:cacheprovider tests/test_callback_wait_origin_scope_nf02255.py
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from aiworkhub import callback_store, core, manager_callback_wait, task_store

SESSION = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"


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


def _insert_blocked(task_id: str, *, task_origin: str, callback_origin: str) -> None:
    """A blocked worker_failed card whose callback row belongs to
    ``callback_origin`` and is neither re-seeded nor claimed by this wait: an
    own row is an already-recovered dead_letter, a foreign row was delivered
    (a foreign dead_letter would be retargeted to this waiting seat)."""
    state = "dead_letter" if callback_origin == SESSION else "delivered"
    now = datetime.now(timezone.utc).isoformat()
    card = {
        "schema_id": "aiworkhub.machine_task_card.v1",
        "task_id": task_id, "status": "blocked", "worker_status": "blocked",
        "runner": "r", "topic": "t", "origin_thread_id": task_origin,
        "coordinator_provider": "claude", "terminal_substatus": "worker_failed",
        "claim_epoch": 1,
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
                task_id, "r", "t", "blocked", "blocked", "high", "",
                json.dumps(card, sort_keys=True), now, now, task_origin, "",
            ),
        )
        conn.execute(
            "INSERT INTO callback_outbox(task_id,provider,origin_thread_id,transition,episode_id,"
            "state,created_at,updated_at,batch_id,recovery_count) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (task_id, "claude", callback_origin, "worker_failed", "1", state, now, now, "", 1),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_mixed_origins() -> None:
    _insert_blocked("OWN-CARD", task_origin=SESSION, callback_origin=SESSION)
    # Owned only through its callback row: the task records no origin.
    _insert_blocked("OWN-CALLBACK", task_origin="", callback_origin=SESSION)
    _insert_blocked("QR-gate-repair-1", task_origin=OTHER, callback_origin=OTHER)
    _insert_blocked("audit-mobile-1", task_origin=OTHER, callback_origin=OTHER)


def _listed(reply) -> set[str]:
    return {row["task_id"] for row in reply["review_waiting"]}


def test_callback_wait_lists_only_own_origin_and_counts_the_rest(repo):
    _seed_mixed_origins()

    reply = core.claude_callback_wait(timeout_seconds=1)

    assert reply["status"] == "timeout_no_callback"
    assert _listed(reply) == {"OWN-CARD", "OWN-CALLBACK"}
    assert reply["other_origin_count"] == 2
    assert reply["review_waiting_scope"] == "own_origin"


def test_callback_wait_opt_in_lists_other_origin_items(repo):
    _seed_mixed_origins()

    reply = core.claude_callback_wait(timeout_seconds=1, include_other_origin=True)

    assert _listed(reply) == {"OWN-CARD", "OWN-CALLBACK", "QR-gate-repair-1", "audit-mobile-1"}
    reasons = {row["task_id"]: row["reason"] for row in reply["review_waiting"]}
    assert reasons["QR-gate-repair-1"] == "other_origin"
    assert reply["other_origin_count"] == 2
    assert reply["review_waiting_scope"] == "all_origins"


def test_callback_wait_never_hides_own_origin_when_only_own_items_exist(repo):
    _insert_blocked("OWN-CARD", task_origin=SESSION, callback_origin=SESSION)

    reply = core.claude_callback_wait(timeout_seconds=1)

    assert _listed(reply) == {"OWN-CARD"}
    assert reply["other_origin_count"] == 0


def test_foreign_dead_reviewer_counts_as_other_origin_not_hidden_terminal(repo):
    _insert_blocked("QUALITY_REVIEW_FOREIGN", task_origin=OTHER, callback_origin=OTHER)

    reply = core.claude_callback_wait(timeout_seconds=1)

    assert _listed(reply) == set()
    assert reply["other_origin_count"] == 1
    assert "review_waiting_hidden_terminal" not in reply


def test_review_waiting_error_reply_carries_scope_keys(repo, monkeypatch):
    def _boom(*_args, **_kwargs):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(core, "_canonical_connect", _boom)

    reply = manager_callback_wait._callback_review_waiting("claude", SESSION)

    assert reply["review_waiting"] == []
    assert reply["review_waiting_error"] == "RuntimeError"
    assert reply["review_waiting_scope"] == "own_origin"
    assert reply["other_origin_count"] == 0
