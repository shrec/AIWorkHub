"""NF-2026-00240: Claude-route delivery contract at the callback_store layer.

Covers the store-level primitives a Claude-route push-delivery channel
relies on: a batch claim can be scoped to exactly one verified route's own
session (never a different route's callback, even for the same provider),
a non-mutating peek can check deliverability without consuming a claim, the
existing lease/ack contract stays intact (ack requires an exact route/lease
match, an unacked batch stays redeliverable), and a full claude
enqueue->claim->ack cycle behaves as documented.
"""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aiworkhub import callback_store  # noqa: E402


def _make_db(tmp_path):
    conn = callback_store.open_db(tmp_path / "task_queue.sqlite")
    callback_store.init_db(conn)
    return conn


def _seed_review_task(conn, task_id: str, session_id: str, *, provider: str = "claude") -> None:
    now = callback_store.utc_now()
    card = {
        "schema_id": "aiworkhub.machine_task_card.v1",
        "task_id": task_id,
        "status": "review",
        "worker_status": "review",
        "runner": "r",
        "topic": "task_mcp",
        "priority": "high",
        "objective": "claude-route-delivery",
        "origin_thread_id": session_id,
        "claim_epoch": 0,
    }
    conn.execute(
        """
        INSERT INTO tasks (
          task_id, runner, topic, status, worker_status, priority, objective,
          card_json, created_at, updated_at, origin_thread_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task_id, "r", "task_mcp", "review", "review", "high",
            card["objective"], json.dumps(card, ensure_ascii=False, sort_keys=True),
            now, now, session_id,
        ),
    )
    conn.commit()
    assert callback_store.enqueue_callback(
        conn, task_id, session_id, "review_ready", provider=provider, episode_id="0",
    )


def _task_id() -> str:
    return f"TASK_{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# claim_pending_callback_batch: session-scoped claiming (route-mismatch
# rejection at the claim layer, defense-in-depth beneath route_identity).
# ---------------------------------------------------------------------------

def test_claim_scoped_to_origin_thread_id_never_returns_other_route_batch(tmp_path):
    conn = _make_db(tmp_path)
    session_a = str(uuid.uuid4())
    session_b = str(uuid.uuid4())
    _seed_review_task(conn, _task_id(), session_a)
    _seed_review_task(conn, _task_id(), session_b)

    claimed = callback_store.claim_pending_callback_batch(
        conn, lease_seconds=30, provider="claude", origin_thread_id=session_a,
    )
    assert claimed is not None
    assert claimed["origin_thread_id"] == session_a
    assert all(m["origin_thread_id"] == session_a for m in claimed["members"])

    # Session B's callback is untouched -- still claimable on its own route,
    # never silently swept up by session A's scoped claim.
    still_pending = conn.execute(
        "SELECT state FROM callback_outbox WHERE origin_thread_id=?", (session_b,)
    ).fetchone()
    assert still_pending["state"] == "pending"

    claimed_b = callback_store.claim_pending_callback_batch(
        conn, lease_seconds=30, provider="claude", origin_thread_id=session_b,
    )
    assert claimed_b is not None
    assert claimed_b["origin_thread_id"] == session_b


def test_claim_scoped_to_wrong_origin_thread_id_returns_none(tmp_path):
    conn = _make_db(tmp_path)
    session_a = str(uuid.uuid4())
    unrelated = str(uuid.uuid4())
    _seed_review_task(conn, _task_id(), session_a)

    claimed = callback_store.claim_pending_callback_batch(
        conn, lease_seconds=30, provider="claude", origin_thread_id=unrelated,
    )
    assert claimed is None
    # The real pending row for session_a is untouched by the mismatched claim.
    row = conn.execute(
        "SELECT state FROM callback_outbox WHERE origin_thread_id=?", (session_a,)
    ).fetchone()
    assert row["state"] == "pending"


def test_claim_without_origin_thread_id_is_unscoped_backward_compatible(tmp_path):
    conn = _make_db(tmp_path)
    session_a = str(uuid.uuid4())
    _seed_review_task(conn, _task_id(), session_a)

    claimed = callback_store.claim_pending_callback_batch(conn, lease_seconds=30, provider="claude")
    assert claimed is not None
    assert claimed["origin_thread_id"] == session_a


def test_rebind_prunes_stale_pending_callback_instead_of_retargeting_it(tmp_path):
    conn = _make_db(tmp_path)
    old_session = str(uuid.uuid4())
    new_session = str(uuid.uuid4())
    task_id = _task_id()
    _seed_review_task(conn, task_id, old_session, provider="codex")
    card = json.loads(
        conn.execute(
            "SELECT card_json FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()["card_json"]
    )
    card["status"] = "finished"
    conn.execute(
        "UPDATE tasks SET status='finished', worker_status='finished', card_json=? "
        "WHERE task_id=?",
        (json.dumps(card, sort_keys=True), task_id),
    )
    conn.commit()

    rebound = callback_store.rebind_pending_callbacks(
        conn, provider="codex", origin_thread_id=new_session
    )
    row = conn.execute(
        "SELECT state, origin_thread_id, last_error FROM callback_outbox "
        "WHERE task_id=?",
        (task_id,),
    ).fetchone()

    assert rebound == 0
    assert row["state"] == "superseded"
    assert row["origin_thread_id"] == old_session
    assert row["last_error"] == "task_no_longer_in_matching_terminal_state_or_episode"


# ---------------------------------------------------------------------------
# has_deliverable_callback: non-mutating peek.
# ---------------------------------------------------------------------------

def test_has_deliverable_callback_true_then_false_after_claim(tmp_path):
    conn = _make_db(tmp_path)
    session_a = str(uuid.uuid4())
    _seed_review_task(conn, _task_id(), session_a)

    assert callback_store.has_deliverable_callback(
        conn, provider="claude", origin_thread_id=session_a,
    ) is True
    # A peek never mutates state -- the row is still claimable afterward.
    row = conn.execute(
        "SELECT state FROM callback_outbox WHERE origin_thread_id=?", (session_a,)
    ).fetchone()
    assert row["state"] == "pending"

    claimed = callback_store.claim_pending_callback_batch(
        conn, lease_seconds=30, provider="claude", origin_thread_id=session_a,
    )
    assert claimed is not None
    assert callback_store.has_deliverable_callback(
        conn, provider="claude", origin_thread_id=session_a,
    ) is False


def test_has_deliverable_callback_false_for_other_route(tmp_path):
    conn = _make_db(tmp_path)
    session_a = str(uuid.uuid4())
    session_b = str(uuid.uuid4())
    _seed_review_task(conn, _task_id(), session_a)

    assert callback_store.has_deliverable_callback(
        conn, provider="claude", origin_thread_id=session_b,
    ) is False
    assert callback_store.has_deliverable_callback(
        conn, provider="codex", origin_thread_id=session_a,
    ) is False


def test_has_deliverable_callback_requires_provider_and_thread(tmp_path):
    conn = _make_db(tmp_path)
    assert callback_store.has_deliverable_callback(conn, provider="", origin_thread_id="x") is False
    assert callback_store.has_deliverable_callback(conn, provider="claude", origin_thread_id="") is False


# ---------------------------------------------------------------------------
# Lease/ack contract: exactly one verified route holds a batch, ack is
# mandatory, an unacked batch stays redeliverable, and a mismatched route
# is rejected.
# ---------------------------------------------------------------------------

def test_acknowledge_requires_exact_provider_and_thread_match(tmp_path):
    conn = _make_db(tmp_path)
    session_a = str(uuid.uuid4())
    _seed_review_task(conn, _task_id(), session_a)
    claim = callback_store.claim_pending_callback_batch(
        conn, lease_seconds=30, provider="claude", origin_thread_id=session_a,
    )
    assert claim is not None
    batch_id, lease_id = claim["batch_id"], claim["lease_id"]

    # Wrong provider, wrong thread, wrong lease: every mismatch fails
    # closed without acknowledging (and without mutating) the batch.
    assert callback_store.acknowledge_callback_batch(
        conn, batch_id, lease_id, provider="codex", origin_thread_id=session_a,
    ) is False
    assert callback_store.acknowledge_callback_batch(
        conn, batch_id, lease_id, provider="claude", origin_thread_id=str(uuid.uuid4()),
    ) is False
    assert callback_store.acknowledge_callback_batch(
        conn, batch_id, uuid.uuid4().hex, provider="claude", origin_thread_id=session_a,
    ) is False
    assert conn.execute(
        "SELECT state FROM callback_batches WHERE batch_id=?", (batch_id,)
    ).fetchone()["state"] == "inflight"

    # The exact matching route acknowledges successfully, exactly once.
    assert callback_store.acknowledge_callback_batch(
        conn, batch_id, lease_id, provider="claude", origin_thread_id=session_a,
    ) is True
    assert callback_store.acknowledge_callback_batch(
        conn, batch_id, lease_id, provider="claude", origin_thread_id=session_a,
    ) is False
    assert conn.execute(
        "SELECT state FROM callback_batches WHERE batch_id=?", (batch_id,)
    ).fetchone()["state"] == "delivered"


def test_unacked_batch_stays_redeliverable_after_lease_expiry(tmp_path):
    conn = _make_db(tmp_path)
    session_a = str(uuid.uuid4())
    _seed_review_task(conn, _task_id(), session_a)
    claim = callback_store.claim_pending_callback_batch(
        conn, lease_seconds=30, provider="claude", origin_thread_id=session_a,
    )
    assert claim is not None
    batch_id = claim["batch_id"]

    # Never acknowledged. Force the lease to have already expired (a
    # dropped tool response / crashed manager never leaves the batch
    # stranded forever).
    conn.execute(
        "UPDATE callback_batches SET lease_expires_at=? WHERE batch_id=?",
        ("2000-01-01T00:00:00+00:00", batch_id),
    )
    conn.commit()

    reclaimed = callback_store.claim_pending_callback_batch(
        conn, lease_seconds=30, provider="claude", origin_thread_id=session_a,
    )
    assert reclaimed is not None
    assert reclaimed["batch_id"] == batch_id
    assert reclaimed["lease_id"] != claim["lease_id"]

    # The stale first lease can no longer acknowledge the reclaimed batch.
    assert callback_store.acknowledge_callback_batch(
        conn, batch_id, claim["lease_id"], provider="claude", origin_thread_id=session_a,
    ) is False
    # Only the new (redelivered) lease can.
    assert callback_store.acknowledge_callback_batch(
        conn, batch_id, reclaimed["lease_id"], provider="claude", origin_thread_id=session_a,
    ) is True


def test_only_one_route_holds_a_batch_at_a_time(tmp_path):
    """A second claim for the SAME route while the first is still inflight
    (unacked) must never hand out a second concurrent lease for it."""
    conn = _make_db(tmp_path)
    session_a = str(uuid.uuid4())
    _seed_review_task(conn, _task_id(), session_a)
    first = callback_store.claim_pending_callback_batch(
        conn, lease_seconds=30, provider="claude", origin_thread_id=session_a,
    )
    assert first is not None

    second = callback_store.claim_pending_callback_batch(
        conn, lease_seconds=30, provider="claude", origin_thread_id=session_a,
    )
    assert second is None


# ---------------------------------------------------------------------------
# Full claude-route enqueue -> claim -> ack cycle.
# ---------------------------------------------------------------------------

def test_full_claude_route_delivery_cycle(tmp_path):
    conn = _make_db(tmp_path)
    session_id = str(uuid.uuid4())
    task_id = _task_id()
    _seed_review_task(conn, task_id, session_id)

    assert callback_store.has_deliverable_callback(
        conn, provider="claude", origin_thread_id=session_id,
    ) is True

    claim = callback_store.claim_pending_callback_batch(
        conn, lease_seconds=30, provider="claude", origin_thread_id=session_id,
    )
    assert claim is not None
    assert claim["members"][0]["task_id"] == task_id
    assert claim["members"][0]["transition"] == "review_ready"

    ok = callback_store.acknowledge_callback_batch(
        conn, claim["batch_id"], claim["lease_id"],
        provider="claude", origin_thread_id=session_id,
    )
    assert ok is True
    outbox_row = conn.execute(
        "SELECT state FROM callback_outbox WHERE task_id=?", (task_id,)
    ).fetchone()
    assert outbox_row["state"] == "delivered"


def test_worker_failed_transition_delivers_through_same_claude_route_cycle(tmp_path):
    conn = _make_db(tmp_path)
    session_id = str(uuid.uuid4())
    task_id = _task_id()
    now = callback_store.utc_now()
    conn.execute(
        """
        INSERT INTO tasks (
          task_id, runner, topic, status, worker_status, priority, objective,
          card_json, created_at, updated_at, origin_thread_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task_id, "r", "task_mcp", "blocked", "blocked", "high", "worker-failed",
            json.dumps({
                "schema_id": "aiworkhub.machine_task_card.v1",
                "task_id": task_id,
                "status": "blocked",
                "worker_status": "blocked",
                "origin_thread_id": session_id,
                "claim_epoch": 0,
            }, ensure_ascii=False, sort_keys=True),
            now, now, session_id,
        ),
    )
    conn.commit()
    assert callback_store.enqueue_callback(
        conn, task_id, session_id, "worker_failed", provider="claude", episode_id="0",
    )

    claim = callback_store.claim_pending_callback_batch(
        conn, lease_seconds=30, provider="claude", origin_thread_id=session_id,
    )
    assert claim is not None
    assert claim["members"][0]["transition"] == "worker_failed"
    assert callback_store.acknowledge_callback_batch(
        conn, claim["batch_id"], claim["lease_id"],
        provider="claude", origin_thread_id=session_id,
    ) is True
