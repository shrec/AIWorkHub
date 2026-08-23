"""SCAN-E3FF: lifecycle-truth defects on the task lifecycle in core.py.

Covers:
  * Defect TWO -- ``_lifecycle_state`` folded ``superseded`` into ``pending``,
    so a card explicitly replaced by a successor read as work still waiting on
    planning surfaces. It now mirrors ``task_store.canonical_status`` and
    reports ``superseded`` as its own closed state.
  * Defect THREE -- ``create_task`` reconciled onto ANY existing row and handed
    it back. Reconciling onto a LIVE row is the lost-response recovery contract
    and must keep working; reconciling onto a finished/archived/superseded row
    now returns a receipt naming that terminal state instead of a dead card.
  * Defect FIVE -- the long-poll completion inbox claimed at provider level, so
    a second verified manager on the same repository could lease and park a
    batch belonging to another route. It now claims at route level.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from aiworkhub import callback_store, core, task_store  # noqa: E402


def _init_repo(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    task_store.initialize_repository(root)
    return root


# ---------------------------------------------------------------------------
# Defect TWO: superseded is a closed state, not pending.
# ---------------------------------------------------------------------------

def test_lifecycle_state_superseded_is_its_own_state():
    assert core._lifecycle_state({"status": "superseded"}) == "superseded"
    assert core._lifecycle_state({"worker_status": "superseded"}) == "superseded"


def test_lifecycle_state_other_states_unchanged():
    assert core._lifecycle_state({"status": "pending"}) == "pending"
    assert core._lifecycle_state({}) == "pending"
    assert core._lifecycle_state({"status": "archived"}) == "archived"
    assert core._lifecycle_state({"status": "finished"}) == "finished"
    assert core._lifecycle_state({"worker_status": "done"}) == "finished"
    assert core._lifecycle_state({"status": "blocked"}) == "blocked"
    assert core._lifecycle_state({"worker_status": "claimed"}) == "processing"
    assert core._lifecycle_state({"status": "review"}) == "review"


def test_superseded_card_is_not_offered_for_pickup():
    """A superseded card must not read as claimable-pending on the auto_pickup
    planning surface. Before the fix _lifecycle_state returned 'pending', so the
    card was offered as ready work."""
    rows = [
        {
            "task_id": "S1",
            "worker_status": "unclaimed",
            "status": "superseded",
            "runner": "R",
            "topic": "TP",
        }
    ]
    assert core.eligible_dryrun_candidates(rows, "R", "TP") == []


# ---------------------------------------------------------------------------
# Defect THREE: create_task reconcile vs terminal rows.
# ---------------------------------------------------------------------------

def _create_args(task_id):
    return dict(
        task_id=task_id,
        title="lifecycle truth reconcile",
        runner="claude-worker",
        topic="core_lifecycle_test",
        objective="verify reconcile onto live vs terminal rows",
        acceptance=["reconcile terminal rows to a named receipt"],
        allowed_writes=["src/aiworkhub/example_target.py"],
        required_outputs=["src/aiworkhub/example_target.py"],
        validation=["pytest -q tests/test_example_target.py"],
        custom_template_escape="audited_custom_unclassified",
        callback_required=False,
    )


def _patch_manager(monkeypatch, root):
    monkeypatch.setattr(core, "repo_root", lambda: root)
    monkeypatch.setattr(
        core,
        "_claude_manager_identity",
        lambda: {"provider": "claude", "session_id": "SESSION-A", "route_state": "verified"},
    )
    monkeypatch.setattr(core, "_canonical_write_gate", lambda *a, **k: None)
    monkeypatch.setattr(core, "_verify_coordinator_capability", lambda _runner: (True, "test_route"))


def _force_card_status(task_id, status):
    conn = core._canonical_connect()
    try:
        row = conn.execute("SELECT card_json FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        card = json.loads(row["card_json"])
        card["status"] = status
        conn.execute("UPDATE tasks SET card_json=? WHERE task_id=?", (json.dumps(card), task_id))
        conn.commit()
    finally:
        conn.close()


def test_create_task_reconciles_live_row_lost_response(tmp_path, monkeypatch):
    """The lost-response recovery contract: retrying an identical create with
    the same task_id onto a LIVE row still reconciles and returns that card."""
    root = _init_repo(tmp_path)
    _patch_manager(monkeypatch, root)
    args = _create_args("SCAN-RECON-LIVE-1")

    first = core.create_task(**args)
    assert first["created"] is True
    assert first["receipt_state"] == "created"

    second = core.create_task(**args)
    assert second["ok"] is True
    assert second["created"] is False
    assert second["reconciled"] is True
    assert second["receipt_state"] == "existing_identical"


@pytest.mark.parametrize("terminal", ["finished", "archived", "superseded"])
def test_create_task_terminal_row_returns_named_receipt(tmp_path, monkeypatch, terminal):
    """Reconciling onto a finished/archived/superseded row returns a receipt
    naming that terminal state rather than a card the caller cannot use."""
    root = _init_repo(tmp_path)
    _patch_manager(monkeypatch, root)
    args = _create_args(f"SCAN-RECON-{terminal.upper()}-1")

    assert core.create_task(**args)["created"] is True
    _force_card_status(args["task_id"], terminal)

    retry = core.create_task(**args)
    assert retry["ok"] is False
    assert retry["created"] is False
    assert retry["reconciled"] is False
    assert retry["receipt_state"] == "existing_terminal"
    assert retry["terminal_state"] == terminal
    assert f"task_terminal:{terminal}" in retry["stderr"]


# ---------------------------------------------------------------------------
# Defect FIVE: long-poll completion inbox claims at route level.
# ---------------------------------------------------------------------------

def _seed_callback(task_id, origin_thread_id):
    now = datetime.now(timezone.utc).isoformat()
    conn = core._canonical_connect()
    try:
        callback_store.init_db(conn)
        conn.execute(
            "INSERT INTO callback_outbox(task_id,provider,origin_thread_id,transition,episode_id,"
            "state,created_at,updated_at,batch_id) VALUES(?,?,?,?,?,?,?,?,?)",
            (task_id, "claude", origin_thread_id, "review_ready", "1", "pending", now, now, ""),
        )
        conn.commit()
    finally:
        conn.close()


def _outbox_state(task_id):
    conn = core._canonical_connect()
    try:
        row = conn.execute(
            "SELECT state, batch_id FROM callback_outbox WHERE task_id=?", (task_id,)
        ).fetchone()
    finally:
        conn.close()
    return (row["state"], row["batch_id"])


def _batch_count():
    conn = core._canonical_connect()
    try:
        row = conn.execute("SELECT COUNT(*) AS n FROM callback_batches").fetchone()
    finally:
        conn.close()
    return int(row["n"])


def test_callback_wait_claims_at_route_level(tmp_path, monkeypatch):
    """A second verified manager on the same repository (session B) cannot lease
    and park a batch belonging to another route (session A). Without the fix the
    provider-wide claim leased A's batch and parked it for 30s, incrementing
    attempts and making A miss its wake-up."""
    root = _init_repo(tmp_path)
    monkeypatch.setattr(core, "repo_root", lambda: root)
    # A seeded callback with no backing task row would be pruned as stale by the
    # claim's terminal-state recheck; hold that recheck True so the test targets
    # the route-scoping behavior under change (same pattern as
    # test_aiworkhub_dispatcher_recovery_b953.py).
    monkeypatch.setattr(
        callback_store, "_task_still_in_matching_terminal_state", lambda *a, **k: True
    )
    _seed_callback("T-CB-A", origin_thread_id="SESSION-A")

    # Session B: must not touch A's batch.
    monkeypatch.setattr(
        core, "_claude_manager_identity",
        lambda: {"provider": "claude", "session_id": "SESSION-B"},
    )
    res_b = core.claude_callback_wait(timeout_seconds=1)
    assert res_b["status"] == "timeout_no_callback"
    assert _outbox_state("T-CB-A") == ("pending", "")
    assert _batch_count() == 0

    # Session A: the rightful manager still receives its own batch.
    monkeypatch.setattr(
        core, "_claude_manager_identity",
        lambda: {"provider": "claude", "session_id": "SESSION-A"},
    )
    res_a = core.claude_callback_wait(timeout_seconds=1)
    assert res_a["status"] == "callback_ready"
    assert res_a["origin_thread_id"] == "SESSION-A"
    assert "T-CB-A" in [m["task_id"] for m in res_a["members"]]
