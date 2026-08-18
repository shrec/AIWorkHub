"""SCAN-E3FF: auto_pickup must not stop at the first colliding candidate.

Defect FOUR: the claim scan only ever attempted ``eligible[0]``; if that atomic
claim lost the unclaimed+pending guard (already claimed, blocked, or retired
since the snapshot), it returned ``claim_race_lost`` immediately -- so one
blocked card at the head of the queue starved every ready card behind it. The
fix continues the scan, claims the first candidate that survives its guard, and
reports what was shadowed and why.
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


def _insert_card(task_id, *, status, worker_status, runner="R", topic="TP"):
    now = datetime.now(timezone.utc).isoformat()
    card = {"task_id": task_id, "runner": runner, "topic": topic, "status": status}
    conn = core._canonical_connect()
    try:
        conn.execute(
            "INSERT INTO tasks(task_id,runner,topic,mode,status,worker_status,priority,objective,"
            "card_json,created_at,updated_at,origin_thread_id,archived_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                task_id, runner, topic, "", status, worker_status, "normal", "",
                json.dumps(card), now, now, "", "",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def test_blocked_head_of_queue_does_not_starve_ready_card(tmp_path, monkeypatch):
    """A blocked card at the head of the eligible scan is skipped; the ready
    card behind it is claimed. Without the fix auto_pickup returned
    claim_race_lost on the head and never reached the ready card."""
    root = _init_repo(tmp_path)
    monkeypatch.setattr(core, "repo_root", lambda: root)
    monkeypatch.setattr(core, "_canonical_write_gate", lambda *a, **k: None)

    _insert_card("A-BLOCKED", status="blocked", worker_status="blocked")
    _insert_card("B-READY", status="pending", worker_status="unclaimed")

    # A real Plan-DAG snapshot would already exclude the blocked head; forcing
    # it into the eligible list proves the CLAIM loop itself continues past a
    # colliding candidate rather than returning at the first conflict.
    monkeypatch.setattr(
        core,
        "eligible_dryrun_candidates",
        lambda *a, **k: [{"task_id": "A-BLOCKED"}, {"task_id": "B-READY"}],
    )

    result = core.auto_pickup("R", "TP")

    assert result["ok"] is True
    claimed = json.loads(result["stdout"])
    assert claimed["task_id"] == "B-READY"
    skipped_ids = [s["task_id"] for s in result["skipped_candidates"]]
    assert skipped_ids == ["A-BLOCKED"]
    assert result["skipped_candidates"][0]["reason"] == "claim_conflict"

    # The ready card is now really claimed in the canonical store.
    claimed_card = task_store.get_task(root, "B-READY")
    assert claimed_card["worker_status"] == "claimed"
    assert claimed_card["claimed_by"] == "R"


def test_all_candidates_colliding_reports_skips(tmp_path, monkeypatch):
    """When every candidate collides, auto_pickup fails with a named reason and
    still reports which candidates were shadowed and why."""
    root = _init_repo(tmp_path)
    monkeypatch.setattr(core, "repo_root", lambda: root)
    monkeypatch.setattr(core, "_canonical_write_gate", lambda *a, **k: None)

    _insert_card("A-BLOCKED", status="blocked", worker_status="blocked")
    monkeypatch.setattr(
        core,
        "eligible_dryrun_candidates",
        lambda *a, **k: [{"task_id": "A-BLOCKED"}],
    )

    result = core.auto_pickup("R", "TP")

    assert result["ok"] is False
    assert result["stderr"].startswith("no_claimable_task:")
    assert [s["task_id"] for s in result["skipped_candidates"]] == ["A-BLOCKED"]
