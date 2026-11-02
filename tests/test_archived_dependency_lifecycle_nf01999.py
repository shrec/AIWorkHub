"""NF-2026-01999: archived dependencies and closing blocked/failed cards.

Gap 1: an archived card with no supersede chain was always an invalid
dependency (``dependency_has_invalid_depends_on``), even when its work had
already been accepted/promoted before it was archived.  A landed archive is now
a satisfied dependency; an abandoned one is refused by its own name.

Gap 2: ``reject_review(to=archived|superseded)`` refused every card whose
worker_status was not ``review``, so a blocked card whose last attempt failed
could not be closed through it.  It now archives such a card with the same
audit event; everything else is still refused, naming the closure tools.
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

from aiworkhub import core, task_plan, task_store  # noqa: E402

NOW = "2026-11-03T00:00:00+00:00"
CLOSURE_TOOLS = (
    "aiworkhub_task_archive",
    "aiworkhub_task_supersede",
    "aiworkhub_task_retry_terminal",
)


# --- pure task_plan contract (create-time validation) ----------------------


def _archived(task_id: str, *, worker_status: str, operation: str = "archived", **extra):
    """An archived card as core.create_task projects it from the tasks row."""
    return {
        "task_id": task_id,
        "status": "archived",
        "worker_status": worker_status,
        "archived_at": NOW,
        "archive_operation": operation,
        "depends_on": [],
        **extra,
    }


def test_landed_archived_card_is_a_satisfied_dependency_at_create():
    cards = {"landed": _archived("landed", worker_status="done")}

    edges, invalid_ids = task_plan.existing_edges_from_cards(cards)

    assert edges["landed"] == []
    assert invalid_ids == set()
    task_plan.validate_new_dependency_edge(
        "new",
        ["landed"],
        edges,
        invalid_ids=invalid_ids,
        archived_unsatisfied=task_plan.archived_unsatisfied_ids(cards),
    )


def test_landed_archived_card_has_no_further_edges_even_with_stale_depends_on():
    # Its own stored depends_on is history: the work already landed.
    cards = {"landed": _archived("landed", worker_status="done", depends_on=["gone"])}
    edges, invalid_ids = task_plan.existing_edges_from_cards(cards)
    assert edges["landed"] == [] and invalid_ids == set()


def test_existing_card_depending_on_landed_archive_stays_valid():
    cards = {
        "landed": _archived("landed", worker_status="done"),
        "mid": {"task_id": "mid", "status": "pending", "worker_status": "unclaimed",
                "depends_on": ["landed"]},
    }
    edges, invalid_ids = task_plan.existing_edges_from_cards(cards)
    assert edges["mid"] == []
    assert invalid_ids == set()
    task_plan.validate_new_dependency_edge("new", ["mid"], edges, invalid_ids=invalid_ids)


@pytest.mark.parametrize("worker_status", ["unclaimed", "review", "blocked", ""])
def test_abandoned_archived_card_is_refused_by_name_at_create(worker_status):
    cards = {"abandoned": _archived("abandoned", worker_status=worker_status)}
    edges, invalid_ids = task_plan.existing_edges_from_cards(cards)

    with pytest.raises(task_plan.TaskPlanError) as excinfo:
        task_plan.validate_new_dependency_edge(
            "new",
            ["abandoned"],
            edges,
            invalid_ids=invalid_ids,
            archived_unsatisfied=task_plan.archived_unsatisfied_ids(cards),
        )
    message = str(excinfo.value)
    assert message.startswith("dependency_archived_unsatisfied:abandoned")
    assert "dependency_has_invalid_depends_on" not in message
    assert "archived without landing work" in message
    assert "aiworkhub_task_supersede" in message


def test_superseded_chain_resolution_is_unchanged():
    cards = {
        "old": _archived(
            "old", worker_status="done", operation="superseded", superseded_by="replacement"
        ),
        "replacement": {"task_id": "replacement", "status": "pending",
                        "worker_status": "unclaimed", "depends_on": []},
    }
    edges, invalid_ids = task_plan.existing_edges_from_cards(cards)
    assert edges["old"] == ["replacement"]
    assert invalid_ids == set()
    assert task_plan.archived_unsatisfied_ids(cards) == set()

    broken = {"old": _archived("old", worker_status="done", operation="superseded")}
    edges, invalid_ids = task_plan.existing_edges_from_cards(broken)
    assert invalid_ids == {"old"}
    with pytest.raises(task_plan.TaskPlanError, match="dependency_has_invalid_depends_on:old"):
        task_plan.validate_new_dependency_edge(
            "new", ["old"], edges, invalid_ids=invalid_ids,
            archived_unsatisfied=task_plan.archived_unsatisfied_ids(broken),
        )


# --- canonical store: launch readiness and reject_review closure -----------


@pytest.fixture
def coord(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    tok = tmp_path / "coordinator.token"
    tok.write_text("coord-token\n", encoding="utf-8")
    os.chmod(tok, stat.S_IRUSR | stat.S_IWUSR)
    monkeypatch.setenv("BITNN_TASKCTL_COORDINATOR_TOKEN_FILE", str(tok))
    monkeypatch.setenv("BITNN_TASKCTL_COORDINATOR_TOKEN", "coord-token")
    return root


def _insert(root: Path, task_id: str, *, status: str, worker_status: str,
            card: dict | None = None, claimed_by: str | None = "claude_coding") -> None:
    readiness = task_store.storage_readiness(root)
    conn = sqlite3.connect(readiness.canonical_db)
    try:
        conn.execute(
            "INSERT INTO tasks (task_id,runner,topic,mode,status,worker_status,priority,"
            "objective,card_json,created_at,updated_at,claimed_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, "claude_coding", "coding", "solo", status, worker_status, "normal",
             "obj", json.dumps(card or {}), NOW, NOW, claimed_by),
        )
        conn.commit()
    finally:
        conn.close()


def _row(root: Path, task_id: str) -> dict:
    readiness = task_store.storage_readiness(root)
    conn = sqlite3.connect(readiness.canonical_db)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    finally:
        conn.close()
    assert row is not None
    return dict(row)


def _events(root: Path, task_id: str) -> list[str]:
    readiness = task_store.storage_readiness(root)
    conn = sqlite3.connect(readiness.canonical_db)
    try:
        rows = conn.execute(
            "SELECT event FROM task_events WHERE task_id=? ORDER BY event_id", (task_id,)
        ).fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows]


def test_landed_archive_satisfies_launch_readiness(coord):
    _insert(coord, "DEP", status="finished", worker_status="done", claimed_by=None)
    ok, state = task_store.archive_task(coord, "DEP", actor="test", reason="cleanup")
    assert ok, state
    _insert(coord, "NEXT", status="pending", worker_status="unclaimed",
            card={"depends_on": ["DEP"], "allowed_writes": ["a.py"]}, claimed_by=None)

    snapshot = task_plan.build_snapshot(core._full_cards_for_plan())

    assert not snapshot["blockers"].get("NEXT")
    assert "NEXT" in snapshot["ready"]


def test_abandoned_archive_still_blocks_launch(coord):
    _insert(coord, "DEP", status="pending", worker_status="unclaimed", claimed_by=None)
    ok, state = task_store.archive_task(coord, "DEP", actor="test", reason="abandoned")
    assert ok, state
    _insert(coord, "NEXT", status="pending", worker_status="unclaimed",
            card={"depends_on": ["DEP"], "allowed_writes": ["a.py"]}, claimed_by=None)

    snapshot = task_plan.build_snapshot(core._full_cards_for_plan())

    assert snapshot["blockers"]["NEXT"] == ["__archived_dependency_not_superseded__:DEP"]
    assert "NEXT" not in snapshot["ready"]


_WORKER_FAILED_CARD = {
    "terminal_review": {"substatus": "worker_failed", "runner": "claude_coding",
                        "recorded_at": NOW, "claim_epoch": 1},
}


@pytest.mark.parametrize("disposition", ["archived", "superseded"])
def test_reject_review_closes_blocked_worker_failed_card(coord, disposition):
    _insert(coord, "T_BLK", status="blocked", worker_status="blocked",
            card=dict(_WORKER_FAILED_CARD), claimed_by=None)

    res = core.reject_review("T_BLK", "worker failed; closing", to=disposition)

    assert res["ok"] is True, res
    row = _row(coord, "T_BLK")
    assert str(row["archived_at"]).strip()
    assert disposition in _events(coord, "T_BLK")


def test_reject_review_closes_failed_card(coord):
    _insert(coord, "T_FAIL", status="failed", worker_status="failed",
            card=dict(_WORKER_FAILED_CARD), claimed_by=None)
    res = core.reject_review("T_FAIL", "worker failed; closing", to="archived")
    assert res["ok"] is True, res
    assert "archived" in _events(coord, "T_FAIL")


def test_reject_review_closure_event_matches_a_reviewed_card(coord):
    _insert(coord, "T_REV", status="review", worker_status="review")
    _insert(coord, "T_BLK", status="blocked", worker_status="blocked",
            card=dict(_WORKER_FAILED_CARD), claimed_by=None)
    assert core.reject_review("T_REV", "done", to="archived")["ok"] is True
    assert core.reject_review("T_BLK", "done", to="archived")["ok"] is True
    assert _events(coord, "T_REV") == _events(coord, "T_BLK") == ["archived"]


@pytest.mark.parametrize("disposition", ["archived", "superseded"])
def test_reject_review_never_closes_a_processing_card(coord, disposition):
    _insert(coord, "T_RUN", status="processing", worker_status="claimed")
    res = core.reject_review("T_RUN", "x", to=disposition)
    assert res["ok"] is False
    stderr = res.get("stderr") or ""
    assert "reject_not_reviewable:task_id=T_RUN" in stderr
    for tool in CLOSURE_TOOLS:
        assert tool in stderr
    assert not str(_row(coord, "T_RUN")["archived_at"] or "").strip()


def test_reject_review_on_pending_card_names_closure_tools(coord):
    _insert(coord, "T_PEND", status="pending", worker_status="unclaimed")
    res = core.reject_review("T_PEND", "x", to="archived")
    assert res["ok"] is False
    stderr = res.get("stderr") or ""
    assert "reject_not_reviewable:task_id=T_PEND" in stderr
    for tool in CLOSURE_TOOLS:
        assert tool in stderr


@pytest.mark.parametrize("disposition", ["pending", "blocked"])
def test_reject_review_rework_disposition_on_blocked_card_is_refused(coord, disposition):
    _insert(coord, "T_BLK", status="blocked", worker_status="blocked",
            card=dict(_WORKER_FAILED_CARD), claimed_by=None)
    res = core.reject_review("T_BLK", "x", to=disposition)
    assert res["ok"] is False
    row = _row(coord, "T_BLK")
    assert row["status"] == "blocked" and row["worker_status"] == "blocked"
    assert not str(row["archived_at"] or "").strip()
