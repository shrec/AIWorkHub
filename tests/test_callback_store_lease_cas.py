"""NF217 V3: fail-closed end-to-end callback lease CAS contract.

Every mutating callback-batch finalizer must require a non-empty,
caller-provided, exact ``lease_id``. A missing, empty, boolean, coerced,
stale or mismatched lease fails closed without mutating the batch or its
outbox members, and a stale owner can never adopt a successor lease after
expiry/reclaim.
"""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aiworkhub import callback_store  # noqa: E402


def _seed_review_task(conn, task_id: str, session_id: str) -> None:
    now = callback_store.utc_now()
    card = {
        "schema_id": "aiworkhub.machine_task_card.v1",
        "task_id": task_id,
        "status": "review",
        "worker_status": "review",
        "runner": "r",
        "topic": "task_mcp",
        "priority": "high",
        "objective": "lease-cas",
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
            task_id,
            "r",
            "task_mcp",
            "review",
            "review",
            "high",
            card["objective"],
            json.dumps(card, ensure_ascii=False, sort_keys=True),
            now,
            now,
            session_id,
        ),
    )
    conn.commit()
    callback_store.enqueue_callback(conn, task_id, session_id, "review_ready", episode_id="0")


def _batch_row(conn, batch_id: str):
    return conn.execute(
        "SELECT state, lease_id FROM callback_batches WHERE batch_id=?", (batch_id,)
    ).fetchone()


def _outbox_states(conn, batch_id: str) -> list[str]:
    return [
        str(row["state"])
        for row in conn.execute(
            "SELECT state FROM callback_outbox WHERE batch_id=? ORDER BY outbox_id ASC",
            (batch_id,),
        ).fetchall()
    ]


def _make_db(tmp_path):
    conn = callback_store.open_db(tmp_path / "task_queue.sqlite")
    callback_store.init_db(conn)
    return conn


def _claim_one(tmp_path):
    conn = _make_db(tmp_path)
    session_id = str(uuid.uuid4())
    task_id = f"TASK_{uuid.uuid4().hex[:12]}"
    _seed_review_task(conn, task_id, session_id)
    claim = callback_store.claim_pending_callback_batch(conn, lease_seconds=30)
    assert claim is not None
    return conn, claim


@pytest.mark.parametrize(
    "bad",
    [None, "", "   ", True, False, 0, 1, 123, 12.5, [], {}, object()],
)
def test_require_lease_id_rejects_missing_empty_boolean_and_coerced(bad):
    with pytest.raises(ValueError):
        callback_store._require_lease_id(bad)


def test_finalizers_reject_missing_lease_without_mutation(tmp_path):
    conn, claim = _claim_one(tmp_path)
    batch_id = claim["batch_id"]
    before_batch = _batch_row(conn, batch_id)
    before_outbox = _outbox_states(conn, batch_id)

    for finalize in (
        lambda: callback_store.mark_batch_delivered(conn, batch_id, None),
        lambda: callback_store.defer_batch_busy(conn, batch_id, "x", None, delay_seconds=0.0),
        lambda: callback_store.fail_batch_transient(conn, batch_id, "x", None, max_retries=3),
        lambda: callback_store.mark_batch_dead_letter(conn, batch_id, "x", None),
    ):
        with pytest.raises(ValueError):
            finalize()

    assert _batch_row(conn, batch_id)["state"] == before_batch["state"] == "inflight"
    assert _batch_row(conn, batch_id)["lease_id"] == before_batch["lease_id"]
    assert _outbox_states(conn, batch_id) == before_outbox == ["inflight"]


@pytest.mark.parametrize("bad", [True, False, 0, 1, 123])
def test_finalizers_reject_boolean_and_coerced_lease_without_mutation(tmp_path, bad):
    conn, claim = _claim_one(tmp_path)
    batch_id = claim["batch_id"]
    before_batch = _batch_row(conn, batch_id)
    before_outbox = _outbox_states(conn, batch_id)

    for finalize in (
        lambda: callback_store.mark_batch_delivered(conn, batch_id, bad),
        lambda: callback_store.defer_batch_busy(conn, batch_id, "x", bad, delay_seconds=0.0),
        lambda: callback_store.fail_batch_transient(conn, batch_id, "x", bad, max_retries=3),
        lambda: callback_store.mark_batch_dead_letter(conn, batch_id, "x", bad),
    ):
        with pytest.raises(ValueError):
            finalize()

    assert _batch_row(conn, batch_id)["state"] == before_batch["state"] == "inflight"
    assert _batch_row(conn, batch_id)["lease_id"] == before_batch["lease_id"]
    assert _outbox_states(conn, batch_id) == before_outbox == ["inflight"]


def test_stale_and_mismatched_lease_fails_closed_without_mutation(tmp_path):
    conn, claim = _claim_one(tmp_path)
    batch_id = claim["batch_id"]
    wrong = uuid.uuid4().hex
    assert wrong != claim["lease_id"]
    before_batch = _batch_row(conn, batch_id)
    before_outbox = _outbox_states(conn, batch_id)

    assert callback_store.mark_batch_delivered(conn, batch_id, wrong) is False
    assert callback_store.defer_batch_busy(conn, batch_id, "x", wrong, delay_seconds=0.0) is False
    assert (
        callback_store.fail_batch_transient(conn, batch_id, "x", wrong, max_retries=3)
        == "lease_rejected"
    )
    assert callback_store.mark_batch_dead_letter(conn, batch_id, "x", wrong) is False

    assert _batch_row(conn, batch_id)["state"] == before_batch["state"] == "inflight"
    assert _batch_row(conn, batch_id)["lease_id"] == before_batch["lease_id"] == claim["lease_id"]
    assert _outbox_states(conn, batch_id) == before_outbox == ["inflight"]


def test_expired_owner_cannot_mutate_after_reclaim_new_owner_finalizes_exactly_once(tmp_path):
    conn, claim_a = _claim_one(tmp_path)
    batch_id = claim_a["batch_id"]
    lease_a = claim_a["lease_id"]

    # Force owner A's lease to expire so the next claim reclaims the SAME
    # durable batch identity and issues a fresh successor lease.
    conn.execute(
        "UPDATE callback_batches SET lease_expires_at=? WHERE batch_id=?",
        ("2000-01-01T00:00:00+00:00", batch_id),
    )
    conn.commit()

    claim_b = callback_store.claim_pending_callback_batch(conn, lease_seconds=30)
    assert claim_b is not None
    assert claim_b["batch_id"] == batch_id
    lease_b = claim_b["lease_id"]
    assert lease_b != lease_a

    # A is stale: every finalizer fails closed and mutates nothing.
    assert callback_store.mark_batch_delivered(conn, batch_id, lease_a) is False
    assert callback_store.defer_batch_busy(conn, batch_id, "stale", lease_a, delay_seconds=0.0) is False
    assert (
        callback_store.fail_batch_transient(conn, batch_id, "stale", lease_a, max_retries=3)
        == "lease_rejected"
    )
    assert callback_store.mark_batch_dead_letter(conn, batch_id, "stale", lease_a) is False

    row = _batch_row(conn, batch_id)
    assert row["state"] == "inflight"
    assert row["lease_id"] == lease_b
    assert _outbox_states(conn, batch_id) == ["inflight"]

    # B is current and can finalize exactly once.
    assert callback_store.mark_batch_delivered(conn, batch_id, lease_b) is True
    assert callback_store.mark_batch_delivered(conn, batch_id, lease_b) is False
    assert _batch_row(conn, batch_id)["state"] == "delivered"
    assert _outbox_states(conn, batch_id) == ["delivered"]
