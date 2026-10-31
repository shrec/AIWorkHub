"""NF-2026-01899: a live owner no longer keeps a stopped reservation alive.

A pid-null ``starting`` reviewer reservation is owned by the long-lived MCP
server.  Once its lease has expired and its preparation heartbeat has gone
quiet, that owner's liveness is no evidence of progress, so the reservation
stops counting as a live lens and is terminalized.
"""

from __future__ import annotations

import os
import time

import pytest

from aiworkhub import process_launcher
from test_quality_reviewer_launch_reservation import (
    _LENS_BINDING,
    _latest,
    _lens_refusal,
    _manager,
    _reserve_lens,
    _starting,
)


def _owned(request_id: str, expires_at: float, **extra: object) -> dict:
    return {
        **_starting(
            request_id=request_id,
            task_id="REVIEWER_STALE",
            expires_at=expires_at,
            quality_review_attempt=dict(_LENS_BINDING),
        ),
        "owner_pid": os.getpid(),
        "owner_pid_start_ticks": process_launcher._pid_start_ticks(os.getpid()),
        "reviewer_claim_epoch": 1,
        **extra,
    }


def test_stale_constant_covers_the_reservation_window():
    assert (
        process_launcher.QUALITY_REVIEW_PREPARATION_STALE_SECONDS
        >= process_launcher.QUALITY_REVIEW_ATTEMPT_RESERVATION_SECONDS
    )


def test_expired_progress_less_reservation_of_a_live_owner_is_not_live(tmp_path):
    manager = _manager(tmp_path)
    event = _owned("stale-req", time.time() - 1.0)
    manager._append_event(event)

    assert manager._reviewer_event_live(event) is False
    assert manager.live_reviewer_for_lens(
        "TARGET_TASK", "target-req", "correctness"
    ) is None

    receipt = _reserve_lens(manager, "REVIEWER_NEW")

    assert receipt["ok"] is True
    assert receipt["already_reserved"] is False
    retired = _latest(manager, "stale-req")
    assert retired["state"] == "blocked"
    assert retired["blocked_reason"] == "reservation_expired"
    # The original owner resuming cannot commit a provider for it.
    assert manager._reviewer_spawn_transition("stale-req") is False
    assert _latest(manager, "stale-req")["state"] == "blocked"


def test_expired_reservation_with_a_stale_heartbeat_is_not_live(tmp_path):
    manager = _manager(tmp_path)
    stale = time.time() - process_launcher.QUALITY_REVIEW_PREPARATION_STALE_SECONDS - 60.0
    event = _owned(
        "stale-beat", time.time() - 1.0,
        preparation_phase="worktree", preparation_heartbeat_epoch=stale,
    )
    manager._append_event(event)

    assert manager._reviewer_event_live(event) is False
    assert _reserve_lens(manager, "REVIEWER_NEW")["ok"] is True
    assert _latest(manager, "stale-beat")["state"] == "blocked"


def test_fresh_preparation_heartbeat_keeps_an_expired_lease_live(tmp_path):
    manager = _manager(tmp_path)
    event = _owned(
        "beating-req", time.time() - 1.0,
        preparation_phase="worktree", preparation_heartbeat_epoch=time.time() - 5.0,
    )
    manager._append_event(event)

    assert manager._reviewer_event_live(event) is True
    assert _reserve_lens(manager, "REVIEWER_NEW") == _lens_refusal(
        "REVIEWER_STALE", "beating-req"
    )
    assert _latest(manager, "beating-req")["state"] == "starting"


def test_unexpired_lease_of_a_live_owner_is_live(tmp_path):
    manager = _manager(tmp_path)
    event = _owned("leased-req", time.time() + 120.0)
    manager._append_event(event)

    assert manager._reviewer_event_live(event) is True
    assert _reserve_lens(manager, "REVIEWER_NEW") == _lens_refusal(
        "REVIEWER_STALE", "leased-req"
    )


@pytest.mark.parametrize("beat", ["not-a-number", float("nan")])
def test_malformed_heartbeat_evidence_stays_live(tmp_path, beat):
    manager = _manager(tmp_path)
    event = _owned(
        "odd-beat", time.time() - 1.0, preparation_heartbeat_epoch=beat,
    )
    assert manager._reviewer_event_live(event) is True


@pytest.mark.parametrize("lease_offset", [-1.0, 600.0], ids=["expired", "future"])
def test_dead_owner_is_not_kept_live_by_its_lease(tmp_path, monkeypatch, lease_offset):
    # NF-2026-01763 is unchanged: a proven-dead owner is never kept by its lease.
    manager = _manager(tmp_path)
    dead_owner_pid = 999_999_999
    event = {
        **_starting(
            request_id="dead-owner", task_id="REVIEWER_DEAD",
            expires_at=time.time() + lease_offset,
            quality_review_attempt=dict(_LENS_BINDING),
        ),
        "owner_pid": dead_owner_pid, "owner_pid_start_ticks": 1,
        "reviewer_claim_epoch": 1,
    }
    manager._append_event(event)
    real_evidence = process_launcher._pid_identity_evidence
    mismatch = type(
        "MismatchEvidence", (),
        {"verdict": process_launcher.PidIdentityVerdict.MISMATCH},
    )()
    monkeypatch.setattr(
        process_launcher,
        "_pid_identity_evidence",
        lambda pid, ticks: (
            mismatch if pid == dead_owner_pid else real_evidence(pid, ticks)
        ),
    )

    assert manager._reviewer_event_live(event) is False
    receipt = _reserve_lens(manager, "REVIEWER_NEW")
    assert receipt["ok"] is True
    assert _latest(manager, "dead-owner")["state"] == "blocked"
