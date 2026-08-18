"""NF-2026-00249: reject_review must finalize the reviewers it strands.

``aiworkhub_agent_accept_review`` finalizes the quality-reviewer cards it
consumes and returns a ``reviewer_finalization`` receipt naming each.
``aiworkhub_task_reject_review`` historically did not: every reviewer whose
findings drove a rejection stayed ``review_ready`` forever, bound to a request
id that no longer existed.  On a three-lens card that stranded three reviewer
cards per rejection, which is how the review queue became 93% noise.

These tests exercise the real canonical task store (no mocks of the
disposition primitive) so the review-queue count before and after a rejection
is an observed fact, not a claim.
"""
from __future__ import annotations

import json
import os
import sqlite3
import stat
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from aiworkhub import core, task_store  # noqa: E402

NOW = "2026-08-18T00:00:00+00:00"

# The exact receipt row shape accept_review returns (see
# ProcessManager.accept_review's ``reviewer_finalization`` and its assertion in
# tests/test_aiworkhub_accept_review_input_hash_guard_b919.py). A manager
# reading either the accept or the reject receipt must see the same keys.
ACCEPT_ROW_KEYS = {"task_id", "finished", "cleanup_error"}


def _init_repo(tmp_path: Path, name: str = "repo") -> Path:
    root = tmp_path / name
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    return root


def _db(root: Path) -> Path:
    return task_store.storage_readiness(root).canonical_db


def _insert(
    root: Path,
    task_id: str,
    *,
    worker_status: str = "review",
    status: str = "review",
    topic: str = "coding",
    card: dict | None = None,
) -> None:
    conn = sqlite3.connect(_db(root))
    try:
        conn.execute(
            "INSERT INTO tasks (task_id,runner,topic,mode,status,worker_status,priority,"
            "objective,card_json,created_at,updated_at,claimed_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, "claude_coding", topic, "solo", status, worker_status, "normal",
             "obj", json.dumps(card or {}), NOW, NOW, "claude_coding"),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_parent(root: Path, task_id: str, request_id: str) -> None:
    """A card in review whose terminal_review pins the request being rejected."""
    _insert(root, task_id, card={
        "terminal_review": {
            "substatus": "review_ready",
            "evidence": {
                "request_identity": {
                    "request_id": request_id,
                    "task_id": task_id,
                    "runner": "claude_coding",
                    "topic": "coding",
                },
            },
        },
    })


def _insert_reviewer(
    root: Path,
    task_id: str,
    target_task: str,
    target_request: str,
    *,
    status: str = "review",
    worker_status: str = "review",
) -> None:
    """A quality-review child bound to one exact target request/claim-epoch."""
    _insert(root, task_id, topic="quality_review", status=status,
            worker_status=worker_status, card={
                "quality_review": {
                    "target_task_id": target_task,
                    "target_request_id": target_request,
                    "target_claim_epoch": 1,
                },
            })


def _status(root: Path, task_id: str) -> tuple[str, str]:
    conn = sqlite3.connect(_db(root))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT status, worker_status FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    return row["status"], row["worker_status"]


def _review_queue(root: Path) -> set[str]:
    """Reviewer cards still awaiting a manager -- the manager's work list.

    A reviewer leaves the queue once it reaches any terminal disposition;
    while stranded it stays here (this is exactly the 93%-noise backlog).
    """
    conn = sqlite3.connect(_db(root))
    try:
        rows = conn.execute(
            "SELECT task_id FROM tasks WHERE topic='quality_review' "
            "AND status NOT IN ('finished','done','archived','superseded')"
        ).fetchall()
    finally:
        conn.close()
    return {row[0] for row in rows}


@pytest.fixture
def coord(tmp_path, monkeypatch):
    root = _init_repo(tmp_path)
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    tok = tmp_path / "coordinator.token"
    tok.write_text("coord-token\n", encoding="utf-8")
    os.chmod(tok, stat.S_IRUSR | stat.S_IWUSR)
    monkeypatch.setenv("BITNN_TASKCTL_COORDINATOR_TOKEN_FILE", str(tok))
    monkeypatch.setenv("BITNN_TASKCTL_COORDINATOR_TOKEN", "coord-token")
    return root


def test_reject_finalizes_every_bound_reviewer(coord):
    """A card with three bound reviewers: the queue is 3 before, 0 after, and
    the reject receipt names all three."""
    _insert_parent(coord, "T_MAIN", "REQ_A")
    for reviewer in ("R_LENS1", "R_LENS2", "R_LENS3"):
        _insert_reviewer(coord, reviewer, "T_MAIN", "REQ_A")

    queue_before = _review_queue(coord)
    assert queue_before == {"R_LENS1", "R_LENS2", "R_LENS3"}

    res = core.reject_review("T_MAIN", "quality gate failed", to="pending")
    assert res["ok"] is True, res

    queue_after = _review_queue(coord)
    assert queue_after == set(), (
        f"reviewers stranded in queue: before={queue_before} after={queue_after}"
    )

    receipt = res["reviewer_finalization"]
    assert {row["task_id"] for row in receipt} == {"R_LENS1", "R_LENS2", "R_LENS3"}
    assert all(row["finished"] is True for row in receipt)
    for reviewer in ("R_LENS1", "R_LENS2", "R_LENS3"):
        assert _status(coord, reviewer) == ("superseded", "superseded")


def test_reviewer_bound_to_a_different_request_survives(coord):
    """A rework relaunches with a new request id; its reviewer must not be
    finalized when the predecessor request is rejected."""
    _insert_parent(coord, "T_MAIN", "REQ_A")
    _insert_reviewer(coord, "R_OLD", "T_MAIN", "REQ_A")
    _insert_reviewer(coord, "R_REWORK", "T_MAIN", "REQ_B")  # different request

    res = core.reject_review("T_MAIN", "rework", to="pending")
    assert res["ok"] is True, res

    named = {row["task_id"] for row in res["reviewer_finalization"]}
    assert named == {"R_OLD"}
    assert _status(coord, "R_OLD") == ("superseded", "superseded")
    # The rework's reviewer is untouched -- still in the queue, still review.
    assert _status(coord, "R_REWORK") == ("review", "review")
    assert "R_REWORK" in _review_queue(coord)


def test_reject_receipt_shape_matches_accept_receipt(coord):
    """Each reject row carries exactly the keys accept_review returns."""
    _insert_parent(coord, "T_MAIN", "REQ_A")
    _insert_reviewer(coord, "R_ONE", "T_MAIN", "REQ_A")

    res = core.reject_review("T_MAIN", "no good", to="pending")
    assert res["ok"] is True, res

    receipt = res["reviewer_finalization"]
    assert receipt, "expected a non-empty reviewer_finalization receipt"
    for row in receipt:
        assert set(row) == ACCEPT_ROW_KEYS
        assert isinstance(row["task_id"], str)
        assert isinstance(row["finished"], bool)
        assert isinstance(row["cleanup_error"], str)


def test_still_running_reviewer_is_superseded_on_reject(coord):
    """Deliberate choice: a reviewer still running when the rejection lands is
    superseded, not left. Its verdict would target a candidate that has just
    been rejected, so leaving it review_ready would recreate the exact noise
    NF-2026-00249 removes."""
    _insert_parent(coord, "T_MAIN", "REQ_A")
    _insert_reviewer(
        coord, "R_RUNNING", "T_MAIN", "REQ_A",
        status="processing", worker_status="claimed",
    )

    assert "R_RUNNING" in _review_queue(coord)

    res = core.reject_review("T_MAIN", "abandon candidate", to="pending")
    assert res["ok"] is True, res

    assert _status(coord, "R_RUNNING") == ("superseded", "superseded")
    assert "R_RUNNING" not in _review_queue(coord)
    named = {row["task_id"]: row for row in res["reviewer_finalization"]}
    assert named["R_RUNNING"]["finished"] is True
