"""Reviewer prewarm truth: read-only open, bounded wait, reservation expiry,
and preparation-phase stall visibility (NF-2026-00302).

Each test fails without the corresponding pure decision seam in
``aiworkhub.quality_review``; together they cover the three independent
defects that left six concurrent reviewers wedged in
``reviewer_source_graph_prewarm_started`` with a frozen heartbeat, a past
reservation and a writable-open lock the reviewer never needed.
"""

from __future__ import annotations

from aiworkhub import quality_review


def test_reviewer_prewarm_opens_read_only_never_writable() -> None:
    """A reviewer is read-only by contract, so its prewarm open reflects that."""
    reviewer_card = {"allowed_writes": [], "forbidden": ["repository_write"]}
    plan = quality_review.reviewer_prewarm_open_plan(reviewer_card=reviewer_card)

    assert plan["open_mode"] == quality_review.REVIEWER_PREWARM_OPEN_MODE_READONLY
    assert plan["open_mode"] == "read_only"
    assert plan["writable"] is False
    # The measured wedge was a writer-lock contention (``writable_open_recover``
    # / ``database is locked``); a read-only open must never take that lock.
    assert plan["requires_writer_lock"] is False
    assert plan["contract_consistent"] is True
    assert plan["reason"] == "reviewer_read_only_by_contract"


def test_reviewer_prewarm_open_plan_flags_a_non_read_only_card() -> None:
    plan = quality_review.reviewer_prewarm_open_plan(
        reviewer_card={"allowed_writes": ["src/x.py"], "forbidden": []}
    )
    # The open mode is read-only regardless, but a card that declares writes is
    # not a real reviewer and is reported as inconsistent.
    assert plan["open_mode"] == "read_only"
    assert plan["contract_consistent"] is False


def test_prewarm_that_exceeds_deadline_fails_launch_with_named_reason() -> None:
    """A prewarm that cannot acquire within the bound fails launch, not hangs."""
    verdict = quality_review.evaluate_prewarm_wait(
        elapsed_seconds=30.1,
        acquired=False,
        deadline_seconds=10.0,
        phase="writable_open_recover",
        error="connect:OperationalError:database is locked",
    )
    assert verdict["launch_failed"] is True
    assert verdict["timed_out"] is True
    assert verdict["ok"] is False
    assert verdict["reason"].startswith(
        "reviewer_source_graph_prewarm_deadline_exceeded"
    )
    # The provider-observed failure detail travels in the named reason.
    assert "database is locked" in verdict["reason"]


def test_prewarm_within_deadline_keeps_waiting_and_acquired_succeeds() -> None:
    waiting = quality_review.evaluate_prewarm_wait(
        elapsed_seconds=1.0, acquired=False, deadline_seconds=10.0
    )
    assert waiting["state"] == "waiting"
    assert waiting["launch_failed"] is False
    assert waiting["timed_out"] is False

    acquired = quality_review.evaluate_prewarm_wait(
        elapsed_seconds=1.0, acquired=True, deadline_seconds=10.0
    )
    assert acquired["ok"] is True
    assert acquired["state"] == "acquired"
    assert acquired["launch_failed"] is False


def test_expired_reservation_terminates_attempt_and_frees_slot() -> None:
    """Let a reservation expire: the attempt is terminated and the slot freed."""
    verdict = quality_review.evaluate_reservation(
        reservation_expires_at_epoch=1000.0,
        now_epoch=1030.5,
        slot_id="slot-3",
        attempt_id="attempt-9",
    )
    assert verdict["expired"] is True
    assert verdict["terminate_attempt"] is True
    assert verdict["free_slot"] is True
    assert verdict["slot_id"] == "slot-3"
    assert verdict["reason"].startswith("reviewer_reservation_expired")


def test_unexpired_reservation_stays_active() -> None:
    verdict = quality_review.evaluate_reservation(
        reservation_expires_at_epoch=2000.0, now_epoch=1000.0
    )
    assert verdict["expired"] is False
    assert verdict["terminate_attempt"] is False
    assert verdict["free_slot"] is False


def test_frozen_preparation_heartbeat_is_visible_without_a_process() -> None:
    """A frozen preparation heartbeat is a stall even though no process exists."""
    launch_epoch = 100.0
    verdict = quality_review.detect_preparation_stall(
        phase="reviewer_source_graph_prewarm_started",
        heartbeat_epoch=launch_epoch,
        now_epoch=launch_epoch + 30.0,
        pid=None,
        process_alive=False,
        stall_after_seconds=20.0,
    )
    assert verdict["stalled"] is True
    # The whole point: detection does NOT require a running process.
    assert verdict["in_preparation"] is True
    assert verdict["watches_process"] is False
    assert verdict["pid"] is None
    assert verdict["process_alive"] is False
    assert verdict["reason"].startswith("reviewer_preparation_heartbeat_frozen")


def test_fresh_preparation_heartbeat_is_not_a_stall() -> None:
    verdict = quality_review.detect_preparation_stall(
        phase="reviewer_source_graph_prewarm_started",
        heartbeat_epoch=129.0,
        now_epoch=130.0,
        pid=None,
        process_alive=False,
        stall_after_seconds=20.0,
    )
    assert verdict["stalled"] is False
    assert verdict["reason"] == "reviewer_preparation_heartbeat_fresh"
