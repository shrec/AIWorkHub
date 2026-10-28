"""NF-2026-01776: a dependency on a superseded card follows its successor.

Measured: after needfix-NF-2026-01765 was superseded by needfix-NF-2026-01765-r1
and the -r1 card succeeded, reconcile still reported the dependent
needfix-NF-2026-01769 as ``missing_dependencies`` on the archived predecessor,
so it (and everything depending on it) never autolaunched.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path


_B905_PATH = Path(__file__).resolve().parent / "test_aiworkhub_dependency_autolaunch_b905_v7.py"
_spec = importlib.util.spec_from_file_location("_b905_dependency_autolaunch_helpers", _B905_PATH)
assert _spec is not None and _spec.loader is not None
b905 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(b905)
autolaunch = b905.autolaunch


def _add_superseded(repo: Path, task_id: str, by: str = "", *, operation: str = "superseded") -> None:
    """Insert an archived row exactly as ``task_store.archive_task`` leaves it."""
    card = {
        "task_id": task_id,
        "runner": f"runner_{task_id.lower()}",
        "topic": "task_mcp",
        "status": operation,
        "worker_status": "unclaimed",
        "depends_on": [],
        "archived_at": "2026-10-28T00:00:00+00:00",
        "archive_operation": operation,
        "archive_reason": (f"superseded_by:{by}; replaced" if by else "replaced"),
    }
    if by and operation == "superseded":
        card["superseded_by"] = by
    conn = sqlite3.connect(repo / ".aiworkhub" / "tasking" / "task_queue.sqlite")
    conn.execute(
        "INSERT INTO tasks(task_id, runner, topic, status, worker_status, card_json, archived_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (task_id, card["runner"], "task_mcp", operation, "unclaimed", json.dumps(card), card["archived_at"]),
    )
    conn.commit()
    conn.close()


def _missing(outcome, task_id):
    return [
        r for r in outcome["delayed"]
        if r["task_id"] == task_id and r["reason"] == "missing_dependencies"
    ]


def test_successor_success_satisfies_dependency_and_child_autolaunches(tmp_path):
    repo = b905._init_repo(tmp_path)
    _add_superseded(repo, "A", "A-r1")
    b905._add(repo, "A-r1", status="finished", worker="done")
    b905._add(repo, "CHILD", deps=["A"])
    calls: list[str] = []
    outcome = autolaunch.reconcile_after_accept(repo, "A-r1", b905._canonical_claim_start(repo, calls))
    assert [r["task_id"] for r in outcome["launched"]] == ["CHILD"]
    assert calls == ["CHILD"]
    assert _missing(outcome, "CHILD") == []
    assert outcome["superseded_resolved"] == [
        {"task_id": "CHILD", "dependencies": {"A": {"successor": "A-r1", "chain": ["A"]}}}
    ]
    assert b905._state(repo, "CHILD")[:2] == ("processing", "claimed")


def test_successor_failure_blocks_child(tmp_path):
    repo = b905._init_repo(tmp_path)
    _add_superseded(repo, "A", "A-r1")
    b905._add(repo, "A-r1", status="failed", worker="worker_failed")
    b905._add(repo, "CHILD", deps=["A"])
    calls: list[str] = []
    outcome = autolaunch.reconcile_after_accept(repo, "A-r1", b905._canonical_claim_start(repo, calls))
    assert outcome["blocked"] == [{"task_id": "CHILD", "blocked_by": ["A-r1"]}]
    assert calls == []
    status, worker, card = b905._state(repo, "CHILD")
    assert (status, worker, card["substatus"]) == ("review", "review", "dependency_blocked")
    assert card["dependency_blocked_by"] == ["A-r1"]


def test_pending_successor_waits_instead_of_missing(tmp_path):
    repo = b905._init_repo(tmp_path)
    _add_superseded(repo, "A", "A-r1")
    b905._add(repo, "A-r1")
    b905._add(repo, "CHILD", deps=["A"])
    outcome = autolaunch.reconcile_startup(repo, b905._canonical_claim_start(repo, []))
    assert {
        "task_id": "CHILD", "reason": "waiting_dependencies", "dependencies": ["A-r1"]
    } in outcome["delayed"]
    assert _missing(outcome, "CHILD") == []


def test_multi_hop_chain_resolves_to_live_terminal_successor(tmp_path):
    repo = b905._init_repo(tmp_path)
    _add_superseded(repo, "A", "A-r1")
    _add_superseded(repo, "A-r1", "A-r2")
    b905._add(repo, "A-r2", status="finished", worker="done")
    b905._add(repo, "CHILD", deps=["A"])
    calls: list[str] = []
    outcome = autolaunch.reconcile_startup(repo, b905._canonical_claim_start(repo, calls))
    assert calls == ["CHILD"]
    assert outcome["superseded_resolved"] == [
        {"task_id": "CHILD", "dependencies": {"A": {"successor": "A-r2", "chain": ["A", "A-r1"]}}}
    ]


def test_chain_without_live_successor_stays_missing(tmp_path):
    repo = b905._init_repo(tmp_path)
    # Superseded with no recorded replacement.
    _add_superseded(repo, "NOBY")
    # Superseded onto a card that was itself archived, but not superseded.
    _add_superseded(repo, "TODONE", "DONE")
    _add_superseded(repo, "DONE", operation="completed")
    # Superseded onto an id that does not exist at all.
    _add_superseded(repo, "GHOSTED", "GHOST")
    # Archived but never superseded: no successor edge to follow.
    _add_superseded(repo, "CANCELLED", "X", operation="cancelled")
    b905._add(repo, "X", status="finished", worker="done")
    for dep in ("NOBY", "TODONE", "GHOSTED", "CANCELLED"):
        b905._add(repo, f"CHILD_{dep}", deps=[dep])
    calls: list[str] = []
    outcome = autolaunch.reconcile_startup(repo, b905._canonical_claim_start(repo, calls))
    assert calls == []
    for dep in ("NOBY", "TODONE", "GHOSTED", "CANCELLED"):
        assert _missing(outcome, f"CHILD_{dep}") == [
            {"task_id": f"CHILD_{dep}", "reason": "missing_dependencies", "dependencies": [dep]}
        ]
    assert outcome["superseded_resolved"] == []


def test_supersede_cycle_terminates_as_missing(tmp_path):
    repo = b905._init_repo(tmp_path)
    _add_superseded(repo, "A", "B")
    _add_superseded(repo, "B", "A")
    b905._add(repo, "CHILD", deps=["A"])
    outcome = autolaunch.reconcile_startup(repo, b905._canonical_claim_start(repo, []))
    assert _missing(outcome, "CHILD") == [
        {"task_id": "CHILD", "reason": "missing_dependencies", "dependencies": ["A"]}
    ]


def test_supersede_chain_walk_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(autolaunch, "MAX_SUPERSEDE_CHAIN_HOPS", 2)
    repo = b905._init_repo(tmp_path)
    # Two hops: within the bound.
    _add_superseded(repo, "N0", "N1")
    _add_superseded(repo, "N1", "N2")
    b905._add(repo, "N2", status="finished", worker="done")
    # Three hops: beyond the bound.
    _add_superseded(repo, "F0", "F1")
    _add_superseded(repo, "F1", "F2")
    _add_superseded(repo, "F2", "F3")
    b905._add(repo, "F3", status="finished", worker="done")
    b905._add(repo, "NEAR", deps=["N0"])
    b905._add(repo, "FAR", deps=["F0"])
    calls: list[str] = []
    outcome = autolaunch.reconcile_startup(repo, b905._canonical_claim_start(repo, calls))
    assert calls == ["NEAR"]
    assert _missing(outcome, "FAR") == [
        {"task_id": "FAR", "reason": "missing_dependencies", "dependencies": ["F0"]}
    ]


def test_unreadable_archived_card_is_not_read_as_end_of_chain(tmp_path, monkeypatch):
    repo = b905._init_repo(tmp_path)
    _add_superseded(repo, "A", "A-r1")
    b905._add(repo, "A-r1", status="finished", worker="done")
    b905._add(repo, "CHILD", deps=["A"])

    def unreadable(db, task_id):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(autolaunch, "_read_archived_card", unreadable)
    calls: list[str] = []
    outcome = autolaunch.reconcile_startup(repo, b905._canonical_claim_start(repo, calls))
    assert calls == []
    assert _missing(outcome, "CHILD") == []
    assert [r["reason"] for r in outcome["delayed"] if r["task_id"] == "CHILD"] == [
        "dependency_resolution_unreadable"
    ]
    monkeypatch.undo()
    again = autolaunch.reconcile_startup(repo, b905._canonical_claim_start(repo, calls))
    assert [r["task_id"] for r in again["launched"]] == ["CHILD"]
