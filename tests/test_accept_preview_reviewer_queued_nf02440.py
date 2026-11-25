"""NF-2026-02440: a queued auto-review is named on ``required_reviewer_missing``.

Right after ``review_ready`` the auto-review chain has only written its actions
to ``review_action_outbox``; no reviewer card or ledger event exists yet. The
blocker must say the review is queued (and in which outbox state) without
claiming it is running, and must be byte-identical to before when nothing is
queued.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiworkhub import process_launcher_accept_review as accept_review
from aiworkhub import review_lifecycle, review_orchestrator

REQUEST_ID = "req-parent-1"
LENS = "correctness"


def _missing_blocker(**kwargs):
    fold = accept_review.fold_accept_blockers(
        reviewers=[],
        reviewer_request_ids=None,
        risk_profile={"required_reviewer_lenses": [LENS]},
        terminal_substatus="review_ready",
        **kwargs,
    )
    found = [b for b in fold["blockers"] if b["kind"] == "required_reviewer_missing"]
    assert len(found) == 1
    return found[0]


def _insert_action(
    conn: sqlite3.Connection,
    *,
    chain_id: int,
    index: int,
    lens: str,
    state: str,
    request_id: str = REQUEST_ID,
    action_type: str = "launch",
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO review_chains(chain_id,target_task_id,"
        "target_request_id,claim_epoch,packet_sha256,candidate_sha256,"
        "chain_identity_json,chain_identity_sha256,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (chain_id, "task-1", request_id, "e1", "p", "c", "{}", f"id{chain_id}", "t"),
    )
    conn.execute(
        "INSERT INTO review_action_outbox(chain_id,action_index,phase,"
        "action_type,lens,target_task_id,target_request_id,claim_epoch,"
        "descriptor_json,descriptor_sha256,state,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            chain_id, index, lens or "target", action_type, lens, "task-1",
            request_id, "e1", "{}", f"d{chain_id}-{index}", state, "t", "t",
        ),
    )


@pytest.fixture()
def outbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db = tmp_path / "review.db"
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.executescript(review_lifecycle.SCHEMA)
    monkeypatch.setattr(
        review_orchestrator, "canonical_review_db", lambda manager: db
    )
    return db


def test_fold_marks_queued_without_claiming_running() -> None:
    blocker = _missing_blocker(queued_reviewer_actions={LENS: "pending"})
    assert blocker["reviewer_queued"] is True
    assert blocker["queued_action_state"] == "pending"
    assert blocker["reviewer_running"] is False
    assert "reviewer_task_id" not in blocker


def test_fold_output_unchanged_when_nothing_queued() -> None:
    plain = _missing_blocker()
    assert _missing_blocker(queued_reviewer_actions={}) == plain
    # A queued action for a different lens is not this lens's queue.
    assert _missing_blocker(queued_reviewer_actions={"security": "pending"}) == plain
    assert "reviewer_queued" not in plain
    assert "queued_action_state" not in plain
    assert plain["reviewer_running"] is False


@pytest.mark.parametrize("state", ["pending", "reserved"])
def test_outbox_pending_or_leased_action_is_reported(outbox: Path, state: str) -> None:
    with closing(sqlite3.connect(outbox)) as conn, conn:
        _insert_action(conn, chain_id=1, index=0, lens=LENS, state=state)
    queued = accept_review.queued_reviewer_actions(SimpleNamespace(), REQUEST_ID)
    assert queued == {LENS: state}
    blocker = _missing_blocker(queued_reviewer_actions=queued)
    assert blocker["reviewer_queued"] is True
    assert blocker["queued_action_state"] == state


def test_outbox_without_queued_action_adds_nothing(outbox: Path) -> None:
    with closing(sqlite3.connect(outbox)) as conn, conn:
        _insert_action(conn, chain_id=1, index=0, lens=LENS, state="completed")
        _insert_action(conn, chain_id=1, index=1, lens=LENS, state="failed")
        # Queued, but for another request: not this request's review.
        _insert_action(
            conn, chain_id=2, index=0, lens=LENS, state="pending", request_id="other"
        )
        # Queued, but not a lens action.
        _insert_action(
            conn, chain_id=1, index=2, lens="", state="pending",
            action_type="target_accept",
        )
    assert accept_review.queued_reviewer_actions(SimpleNamespace(), REQUEST_ID) == {}
    assert "reviewer_queued" not in _missing_blocker(
        queued_reviewer_actions=accept_review.queued_reviewer_actions(
            SimpleNamespace(), REQUEST_ID
        )
    )


def test_unreadable_store_adds_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        review_orchestrator, "canonical_review_db", lambda manager: None
    )
    assert accept_review.queued_reviewer_actions(SimpleNamespace(), REQUEST_ID) == {}
    assert accept_review.queued_reviewer_actions(SimpleNamespace(), "") == {}
