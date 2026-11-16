"""NF-2026-02292: required reviewer lenses run in parallel.

A lens row (phase correctness/security/code_quality) waits only for earlier
rows of its own phase; target/needfix rows still wait for every earlier row.
Failure retirement follows the same gating: a failed lens action retires its
own phase's pending rows plus every non-lens row, never a sibling lens's rows.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import review_lifecycle  # noqa: E402

PACKET = "A" * 64
CANDIDATE = "b" * 64
NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
OWNER = "worker-a"
TOKEN = "lease-a"


def _chain(db: Path) -> review_lifecycle.ReviewChain:
    return review_lifecycle.create_or_replay_chain(
        db,
        target_task_id="TASK_TARGET",
        target_request_id="req-target",
        claim_epoch="7",
        packet_sha256=PACKET,
        candidate_sha256=CANDIDATE,
        now=NOW,
    )


def _reserve(db: Path) -> review_lifecycle.ReviewAction | None:
    return review_lifecycle.reserve_next_action(
        db, owner=OWNER, lease_token=TOKEN, now=NOW, lease_seconds=600
    )


def _complete(db: Path, action: review_lifecycle.ReviewAction) -> None:
    assert review_lifecycle.complete_action(
        db,
        action_id=action.action_id,
        owner=OWNER,
        lease_token=TOKEN,
        receipt={"ok": True, "action_index": action.action_index},
        now=NOW + timedelta(seconds=1),
    )


def _defer(db: Path, action: review_lifecycle.ReviewAction) -> None:
    assert review_lifecycle.defer_action(
        db, action_id=action.action_id, owner=OWNER, lease_token=TOKEN, now=NOW
    )


def _states(db: Path) -> dict[int, str]:
    return {
        int(row["action_index"]): str(row["state"])
        for row in review_lifecycle.rows_for_test(db)
    }


def test_lens_phases_are_derived_from_the_plan() -> None:
    assert review_lifecycle.LENS_PHASES == frozenset(
        {"correctness", "security", "code_quality"}
    )


def test_security_and_code_quality_launch_while_correctness_accept_pending(
    tmp_path: Path,
) -> None:
    db = tmp_path / "task.sqlite"
    _chain(db)

    launch = _reserve(db)
    assert launch is not None
    assert (launch.phase, launch.action_type) == ("correctness", "launch")
    _complete(db, launch)

    # The correctness reviewer is still running: its accept is reserved and
    # deferred back to pending, exactly as the manager does while it waits.
    accept = _reserve(db)
    assert accept is not None
    assert (accept.phase, accept.action_type) == ("correctness", "accept")
    _defer(db, accept)

    security = _reserve(db)
    assert security is not None
    assert (security.phase, security.action_type) == ("security", "launch")
    assert _states(db)[1] == "pending"
    _complete(db, security)

    # Skip past the security accept the same way (its reviewer still runs).
    nxt = _reserve(db)
    assert nxt is not None
    if (nxt.phase, nxt.action_type) == ("security", "accept"):
        _defer(db, nxt)
        nxt = _reserve(db)
    assert nxt is not None
    assert (nxt.phase, nxt.action_type) == ("code_quality", "launch")
    states = _states(db)
    assert states[1] == "pending"
    assert states[4] == "pending"
    # Non-lens rows never start before the lenses finish.
    assert states[9] == states[10] == states[11] == "pending"


def test_target_accept_waits_for_every_lens_row(tmp_path: Path) -> None:
    db = tmp_path / "task.sqlite"
    _chain(db)
    held: review_lifecycle.ReviewAction | None = None
    for _ in range(64):
        action = _reserve(db)
        if action is None:
            break
        assert action.phase in review_lifecycle.LENS_PHASES, action
        if action.action_index == 8:
            # Hold the last code_quality row reserved (not completed).
            held = action
            continue
        _complete(db, action)
    assert held is not None
    states = _states(db)
    assert all(states[index] == "completed" for index in range(8))
    assert states[8] == "reserved"
    assert states[9] == "pending"
    assert _reserve(db) is None

    _complete(db, held)
    target = _reserve(db)
    assert target is not None
    assert (target.phase, target.action_type, target.action_index) == (
        "target",
        "target_accept",
        9,
    )
    assert all(_states(db)[index] == "completed" for index in range(9))


def test_failed_correctness_accept_retires_own_phase_and_non_lens_rows_only(
    tmp_path: Path,
) -> None:
    db = tmp_path / "task.sqlite"
    _chain(db)
    launch = _reserve(db)
    assert launch is not None and launch.action_index == 0
    _complete(db, launch)
    accept = _reserve(db)
    assert accept is not None and accept.action_index == 1
    assert review_lifecycle.fail_action(
        db,
        action_id=accept.action_id,
        owner=OWNER,
        lease_token=TOKEN,
        reason="correctness_accept_failed",
        now=NOW + timedelta(seconds=2),
    )

    result = review_lifecycle.reconcile_dead_chains(db, now=NOW + timedelta(seconds=3))
    assert result["retired"] == 4

    rows = {int(row["action_index"]): row for row in review_lifecycle.rows_for_test(db)}
    for index in (2, 9, 10, 11):
        assert rows[index]["state"] == "retired", index
        assert str(rows[index]["retired_due_to_action_id"]) == str(accept.action_id)
    for index in (3, 4, 5, 6, 7, 8):
        assert rows[index]["state"] == "pending", index

    # Idempotent: a second pass retires nothing more.
    again = review_lifecycle.reconcile_dead_chains(db, now=NOW + timedelta(seconds=4))
    assert again["retired"] == 0

    security = _reserve(db)
    assert security is not None
    assert (security.phase, security.action_type) == ("security", "launch")


def test_failed_non_lens_row_retires_every_later_row(tmp_path: Path) -> None:
    db = tmp_path / "task.sqlite"
    _chain(db)
    target: review_lifecycle.ReviewAction | None = None
    for _ in range(64):
        action = _reserve(db)
        assert action is not None
        if action.action_index == 9:
            target = action
            break
        _complete(db, action)
    assert target is not None
    assert review_lifecycle.fail_action(
        db,
        action_id=target.action_id,
        owner=OWNER,
        lease_token=TOKEN,
        reason="target_failed",
        now=NOW + timedelta(seconds=2),
    )
    result = review_lifecycle.reconcile_dead_chains(db, now=NOW + timedelta(seconds=3))
    assert result["retired"] == 2
    states = _states(db)
    assert states[10] == states[11] == "retired"


def test_cross_phase_prior_rows_are_still_verified(tmp_path: Path) -> None:
    db = tmp_path / "task.sqlite"
    _chain(db)
    launch = _reserve(db)
    assert launch is not None and launch.action_index == 0
    _complete(db, launch)
    accept = _reserve(db)
    assert accept is not None and accept.action_index == 1
    _defer(db, accept)
    security = _reserve(db)
    assert security is not None and security.action_index == 3

    # A tampered correctness row must still fail a security row closed even
    # though the security row no longer waits for it to complete.
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "UPDATE review_action_outbox SET lens='security' WHERE action_index=2"
        )
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(review_lifecycle.ReviewLifecycleError, match="descriptor_tamper"):
        _reserve(db)


def test_missing_prior_row_is_still_descriptor_tamper_for_a_lens_row(
    tmp_path: Path,
) -> None:
    db = tmp_path / "task.sqlite"
    _chain(db)
    conn = sqlite3.connect(db)
    try:
        conn.execute("DELETE FROM review_action_outbox WHERE action_index=0")
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(review_lifecycle.ReviewLifecycleError, match="descriptor_tamper"):
        _reserve(db)
