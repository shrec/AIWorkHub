"""NF-2026-00341: ``blocked`` is legal to ENTER (a real terminal outcome) and
illegal to LEAVE via a terminal-review transition. A state legal to enter and
illegal to leave would be a trap, and the manual instrument
``task_store.recover_blocked_rework`` looked like the evidence everyone already
knew it was one.

Resolution chosen: ``blocked`` IS a terminal outcome WITH a defined transition
out. Refusing it as a terminal-review *source* is correct (you cannot
re-terminalize an already-terminal card); the defined exit is
``recover_blocked_rework``, which returns a rework-eligible blocked card to the
legal ``pending`` source status -- from which terminal review is legal again.
These tests prove a card that reaches ``blocked`` is NOT permanently
unreviewable.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aiworkhub import task_fsm, task_store  # noqa: E402


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ready_canonical_db(tmp_path, monkeypatch) -> Path:
    """Create a real canonical task DB with the task_store schema and route
    ``recover_blocked_rework`` at it, without the full repo-bootstrap
    machinery. The recovery logic under test is exercised unchanged against a
    real sqlite database."""
    db_path = tmp_path / "task_queue.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(task_store.SCHEMA)
    conn.commit()
    conn.close()
    readiness = task_store.StorageReadiness(True, "ready", "repo_under_test", str(db_path))
    monkeypatch.setattr(task_store, "_require_ready", lambda root: (readiness, db_path))
    return db_path


def _seed_recoverable_blocked_card(db_path: Path, task_id: str, substatus: str = "validation_failed") -> None:
    """Seed a card that genuinely reached canonical status ``blocked`` with the
    retained terminal-review evidence and residual feedback a real rework
    recovery requires."""
    now = _now()
    card = {
        "schema_id": "aiworkhub.machine_task_card.v1",
        "task_id": task_id,
        "status": "blocked",
        "worker_status": substatus,
        "runner": "r",
        "topic": "task_mcp",
        "claim_epoch": 0,
        "reject_review": {"reason": "needs rework: a validation gate is still red"},
    }
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO tasks (task_id, runner, topic, status, worker_status, card_json, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (task_id, "r", "task_mcp", "blocked", substatus,
         json.dumps(card, ensure_ascii=False, sort_keys=True), now, now),
    )
    conn.execute(
        "INSERT INTO task_events (task_id, event, runner, payload_json, created_at) "
        "VALUES (?, 'terminal_review', ?, ?, ?)",
        (task_id, "r",
         json.dumps({"substatus": substatus, "runner": "r", "recorded_at": now}, sort_keys=True),
         now),
    )
    conn.commit()
    conn.close()


def _canonical_status(db_path: Path, task_id: str) -> str:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT archived_at, status, worker_status FROM tasks WHERE task_id=?", (task_id,)
    ).fetchone()
    conn.close()
    return task_store.canonical_status(dict(row))


def test_blocked_is_a_legal_terminal_outcome_but_an_illegal_terminal_source():
    # Legal to ENTER: it is part of the exhaustive owner vocabulary.
    assert "blocked" in task_fsm.KNOWN_TERMINAL_SUBSTATUSES
    # Illegal to re-terminalize FROM: a terminal-review transition out is
    # refused (this is the two-line contradiction that looked like a trap).
    legal, reason = task_fsm.check_terminal_review_transition("blocked", "review_ready")
    assert legal is False
    assert reason == "illegal_transition:from=blocked:to=review"


def test_recover_blocked_rework_lifts_a_blocked_card_back_to_reviewable(tmp_path, monkeypatch):
    db_path = _ready_canonical_db(tmp_path, monkeypatch)
    task_id = "TASK_" + uuid.uuid4().hex[:12]
    _seed_recoverable_blocked_card(db_path, task_id)

    # The card really is in the terminal ``blocked`` outcome.
    assert _canonical_status(db_path, task_id) == "blocked"

    # The DEFINED transition out works.
    ok, detail = task_store.recover_blocked_rework(tmp_path, task_id, feedback_reason="needs rework")
    assert ok is True, detail

    # It is now back in a LEGAL source status, so terminal review is legal
    # again: the card is NOT permanently unreviewable.
    recovered_status = _canonical_status(db_path, task_id)
    assert recovered_status == "pending"
    assert recovered_status in task_fsm.LEGAL_SOURCE_STATUSES_FOR_TERMINAL_REVIEW
    legal, _reason = task_fsm.check_terminal_review_transition(recovered_status, "review_ready")
    assert legal is True


def test_recovery_target_is_a_legal_terminal_review_source():
    # The escape lands in ``pending`` (see recover_blocked_rework), which is a
    # legal terminal-review source -- the contract that makes the exit real
    # rather than cosmetic.
    assert "pending" in task_fsm.LEGAL_SOURCE_STATUSES_FOR_TERMINAL_REVIEW
