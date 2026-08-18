"""SCAN-E3FF: supervisor_loop_status must never raise on a lifecycle the system
itself produces.

Defect ONE: ``LIFECYCLE_TO_SUPERVISOR_STATE`` had no ``archived`` entry, so
``supervisor_loop_status`` raised ``KeyError('archived')`` for any archived card
at the clean-mapping step -- and archiving is the only closure for a card wedged
in a non-operational terminal substatus, so archived cards are common. The fix
maps the terminal states and degrades an unmapped lifecycle to a named unknown
instead of raising on a read-only surface.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from aiworkhub import core, task_store  # noqa: E402


def _init_repo(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    task_store.initialize_repository(root)
    return root


def _insert_card(task_id, *, status, worker_status, card, archived_at=""):
    now = datetime.now(timezone.utc).isoformat()
    conn = core._canonical_connect()
    try:
        conn.execute(
            "INSERT INTO tasks(task_id,runner,topic,mode,status,worker_status,priority,objective,"
            "card_json,created_at,updated_at,origin_thread_id,archived_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                task_id,
                card.get("runner", ""),
                card.get("topic", ""),
                "",
                status,
                worker_status,
                card.get("priority", "normal"),
                card.get("objective", ""),
                json.dumps(card),
                now,
                now,
                card.get("origin_thread_id", ""),
                archived_at,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _base_card(task_id, status):
    return {
        "task_id": task_id,
        "runner": "R",
        "topic": "TP",
        "status": status,
        "read_first": [],
        "allowed_writes": [],
    }


def test_archived_card_returns_status_instead_of_raising(tmp_path, monkeypatch):
    """Before the fix this raised KeyError('archived'); after it returns a
    named supervisor_state for the archived card."""
    root = _init_repo(tmp_path)
    monkeypatch.setattr(core, "repo_root", lambda: root)
    now = datetime.now(timezone.utc).isoformat()
    _insert_card(
        "T-ARCHIVED-1",
        status="archived",
        worker_status="unclaimed",
        card=_base_card("T-ARCHIVED-1", "archived"),
        archived_at=now,
    )

    result = core.supervisor_loop_status("T-ARCHIVED-1")

    assert result["supervisor_state"] == "archived"
    assert result["error"] is None
    assert result["state"] == "planned"


def test_pending_card_still_maps_to_planned(tmp_path, monkeypatch):
    """The pre-existing mappings are unchanged: a clean pending card still
    derives supervisor_state 'planned'."""
    root = _init_repo(tmp_path)
    monkeypatch.setattr(core, "repo_root", lambda: root)
    _insert_card(
        "T-PENDING-1",
        status="pending",
        worker_status="unclaimed",
        card=_base_card("T-PENDING-1", "pending"),
    )

    result = core.supervisor_loop_status("T-PENDING-1")

    assert result["supervisor_state"] == "planned"
    assert result["error"] is None


def test_unmapped_lifecycle_degrades_to_named_unknown(tmp_path, monkeypatch):
    """An unmapped lifecycle degrades to a named unknown rather than raising."""
    root = _init_repo(tmp_path)
    monkeypatch.setattr(core, "repo_root", lambda: root)
    _insert_card(
        "T-PENDING-2",
        status="pending",
        worker_status="unclaimed",
        card=_base_card("T-PENDING-2", "pending"),
    )
    monkeypatch.setattr(core, "_lifecycle_state", lambda _card: "brand_new_state")

    result = core.supervisor_loop_status("T-PENDING-2")

    assert result["supervisor_state"] == "unknown:brand_new_state"
    assert result["error"] is None
