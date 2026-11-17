"""NF-2026-02283: task_amend_pending keys on the current state, not launch history.

A card launched once whose request was rejected (``to=pending``) or superseded
is pending and unclaimed again; with every launched request terminal, it is
amendable. A card with a still-live launch, or a claimed card, still refuses.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import task_store  # noqa: E402

STARTED = "2026-01-02T00:00:00+00:00"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    task_store.initialize_repository(root)
    return root


def _rejected_card(**overrides: Any) -> dict[str, Any]:
    """A card launched once (req-1), rejected back to pending and unclaimed."""
    card: dict[str, Any] = {
        "task_id": "T_REJ",
        "runner": "claude-worker",
        "topic": "coding",
        "status": "pending",
        "worker_status": "unclaimed",
        "objective": "do the thing",
        "allowed_writes": ["src/widget.py"],
        "read_first": [],
        "forbidden": [],
        "validation": ["pytest -q tests/test_widget.py"],
        "validation_roles": ["generic"],
        "work_kind": "generic",
        "claim_epoch": 1,
        "launch_request_id": "req-1",
        "rejection_disposition": {
            "schema_id": "aiworkhub.rejection_disposition.v1",
            "to": "pending",
            "request_id": "req-1",
        },
        "history": [
            {"event": "created"},
            {"event": "claimed", "request_id": "req-1"},
            {"event": "reject_review", "request_id": "req-1", "to": "pending"},
        ],
    }
    card.update(overrides)
    return card


def _insert(repo: Path, card: dict[str, Any], *, started_at: str | None = STARTED,
            claimed_by: str | None = None) -> None:
    _readiness, db_path = task_store._require_ready(repo)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, priority, "
            "objective, card_json, created_at, updated_at, archived_at, started_at, claimed_by) "
            "VALUES (?, ?, ?, ?, ?, '', '', ?, ?, ?, '', ?, ?)",
            (
                card["task_id"], card["runner"], card["topic"], card["status"],
                card["worker_status"], json.dumps(card), STARTED, STARTED,
                started_at, claimed_by,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _row(card: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    row = {
        "status": card["status"], "worker_status": card["worker_status"],
        "archived_at": "", "claimed_by": "", "started_at": STARTED,
    }
    row.update(overrides)
    return row


# ------------------------------------------------------------- reproduction


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        # A task_supersede continue_capped successor: pending, unclaimed, a
        # predecessor recorded, no live launch of its own.
        {"launch_request_id": None, "rework_predecessor": {"request_id": "req-1"}},
    ],
)
def test_reproduction_rejected_pending_unclaimed_card_is_amendable(
    repo: Path, overrides: dict[str, Any]
) -> None:
    card = _rejected_card(**overrides)
    assert not task_store.is_pristine_pending_card(card)
    assert task_store.unlaunched_pending_refusal(_row(card), card) == ""

    _insert(repo, card)
    ok, reason, detail = task_store.amend_pending_card(
        repo, "T_REJ", actor="claude",
        allowed_writes=["tests/test_widget.py"],
        validation=["pytest -q tests/test_widget.py", "pytest -q tests/test_widget.py -k ok"],
        validation_roles=["generic", "generic"],
    )

    assert (ok, reason) == (True, "amended"), detail
    stored = task_store.get_task(repo, "T_REJ")
    assert isinstance(stored, dict)
    # Add-only: the existing entry stays; the history entry is still written.
    assert stored["allowed_writes"] == ["src/widget.py", "tests/test_widget.py"]
    assert stored["validation"][-1] == "pytest -q tests/test_widget.py -k ok"
    assert stored["history"][-1]["event"] == task_store.CARD_AMENDED_PENDING_EVENT
    assert stored["history"][-1]["fields"] == ["allowed_writes", "validation"]


# --------------------------------------------------------------- regression


def test_regression_card_whose_request_is_still_running_refuses(repo: Path) -> None:
    # Running: the launch claimed the card.
    running = _rejected_card(status="processing", worker_status="claimed")
    assert task_store.unlaunched_pending_refusal(
        _row(running, claimed_by="claude-worker"), running
    ) != ""
    # Reserved for a relaunch (req-2 attached, not yet started): still live.
    reserved = _rejected_card(launch_request_id="req-2")
    assert task_store.unlaunched_pending_refusal(
        _row(reserved, started_at=None), reserved
    ) == "card_already_launched"
    # Relaunched (req-2, non-terminal) but started_at carried from req-1: live.
    assert task_store.unlaunched_pending_refusal(
        _row(reserved, started_at=STARTED), reserved
    ) == "card_already_launched"

    _insert(repo, reserved, started_at=None)
    before = task_store.get_task(repo, "T_REJ")
    ok, reason, detail = task_store.amend_pending_card(
        repo, "T_REJ", actor="claude", allowed_writes=["tests/test_widget.py"]
    )
    assert (ok, reason, detail) == (False, "card_already_launched", {})
    assert task_store.get_task(repo, "T_REJ") == before


def test_regression_claimed_card_still_refuses_card_claimed(repo: Path) -> None:
    card = _rejected_card()
    assert task_store.unlaunched_pending_refusal(
        _row(card, claimed_by="claude-worker"), card
    ) == "card_claimed"

    _insert(repo, card, claimed_by="claude-worker")
    ok, reason, detail = task_store.amend_pending_card(
        repo, "T_REJ", actor="claude", allowed_writes=["tests/test_widget.py"]
    )
    assert (ok, reason, detail) == (False, "card_claimed", {})
