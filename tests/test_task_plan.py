from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import task_plan  # noqa: E402


def _card(task_id, *, status="pending", worker_status="unclaimed", allowed_writes=None, depends_on=None, created_at="2026-01-01T00:00:00Z"):
    return {
        "task_id": task_id,
        "status": status,
        "worker_status": worker_status,
        "allowed_writes": allowed_writes or [],
        "depends_on": depends_on or [],
        "created_at": created_at,
    }


def test_normalize_depends_on_dedupes_and_bounds():
    assert task_plan.normalize_depends_on(None) == []
    assert task_plan.normalize_depends_on(["a", "a", "b"]) == ["a", "b"]
    with pytest.raises(task_plan.TaskPlanError):
        task_plan.normalize_depends_on(["../etc"])
    with pytest.raises(task_plan.TaskPlanError):
        task_plan.normalize_depends_on(list(range(65)))


def test_validate_new_dependency_edge_rejects_self_dependency():
    with pytest.raises(task_plan.TaskPlanError, match="self_dependency_forbidden"):
        task_plan.validate_new_dependency_edge("t1", ["t1"], {"t1": []})


def test_validate_new_dependency_edge_rejects_missing_dependency():
    with pytest.raises(task_plan.TaskPlanError, match="dependency_not_found"):
        task_plan.validate_new_dependency_edge("t2", ["ghost"], {"t1": []})


def test_validate_new_dependency_edge_rejects_cycle():
    # t1 -> t2 already exists; adding t2 -> t1 would cycle.
    existing_edges = {"t1": ["t2"], "t2": []}
    with pytest.raises(task_plan.TaskPlanError, match="dependency_cycle_detected"):
        task_plan.validate_new_dependency_edge("t2", ["t1"], existing_edges)


def test_validate_new_dependency_edge_accepts_valid_dag_edge():
    existing_edges = {"t1": [], "t2": []}
    task_plan.validate_new_dependency_edge("t3", ["t1", "t2"], existing_edges)


def test_snapshot_blocks_pending_task_on_unfinished_dependency():
    cards = [
        _card("t1", status="pending"),
        _card("t2", status="pending", depends_on=["t1"]),
    ]
    snap = task_plan.build_snapshot(cards)
    assert snap["blockers"]["t2"] == ["t1"]
    assert "t2" not in snap["ready"]
    assert "t1" in snap["ready"]


def test_snapshot_unblocks_once_dependency_finished():
    cards = [
        _card("t1", status="finished"),
        _card("t2", status="pending", depends_on=["t1"]),
    ]
    snap = task_plan.build_snapshot(cards)
    assert "t2" not in snap["blockers"]
    assert "t2" in snap["ready"]


def test_snapshot_reports_write_scope_overlap_with_retained_processing_card():
    cards = [
        _card("t1", status="processing", worker_status="claimed", allowed_writes=["src/a.py"]),
        _card("t2", status="pending", allowed_writes=["src/a.py", "src/b.py"]),
    ]
    snap = task_plan.build_snapshot(cards)
    assert snap["write_scope_overlaps"]["t2"] == ["src/a.py"]
    assert "t2" not in snap["ready"]


def test_snapshot_claims_only_one_of_two_overlapping_pending_tasks_in_queue_order():
    cards = [
        _card("t1", allowed_writes=["src/a.py"], created_at="2026-01-01T00:00:00Z"),
        _card("t2", allowed_writes=["src/a.py"], created_at="2026-01-02T00:00:00Z"),
    ]
    snap = task_plan.build_snapshot(cards)
    assert snap["ready"] == ["t1"]
    assert snap["write_scope_overlaps"]["t2"] == ["src/a.py"]


def test_snapshot_disjoint_writes_are_all_ready_in_parallel():
    cards = [
        _card("t1", allowed_writes=["src/a.py"]),
        _card("t2", allowed_writes=["src/b.py"]),
        _card("t3", allowed_writes=["src/c.py"]),
    ]
    snap = task_plan.build_snapshot(cards)
    assert set(snap["ready"]) == {"t1", "t2", "t3"}
    assert snap["write_scope_overlaps"] == {}


def test_snapshot_existing_cards_with_no_depends_on_behave_identically():
    cards = [_card("t1"), _card("t2")]
    snap = task_plan.build_snapshot(cards)
    assert snap["blockers"] == {}
    assert set(snap["ready"]) == {"t1", "t2"}


def test_paths_conflict_exact_parent_child_and_glob():
    assert task_plan.paths_conflict("src/a.py", "src/a.py")
    assert task_plan.paths_conflict("src", "src/x.py")
    assert task_plan.paths_conflict("src/", "src/x.py")
    assert task_plan.paths_conflict("src/**", "src/x.py")
    assert task_plan.paths_conflict("out/*.json", "out/a.json")
    assert not task_plan.paths_conflict("src/a.py", "src/b.py")
    assert not task_plan.paths_conflict("out/*.json", "out/a.txt")


def test_snapshot_write_scope_overlap_detects_glob_conflict():
    cards = [
        _card("t1", status="processing", worker_status="claimed", allowed_writes=["src/**"]),
        _card("t2", status="pending", allowed_writes=["src/x.py"]),
    ]
    snap = task_plan.build_snapshot(cards)
    assert snap["write_scope_overlaps"]["t2"] == ["src/x.py"]
    assert "t2" not in snap["ready"]


def test_snapshot_write_scope_overlap_detects_parent_child_conflict():
    cards = [
        _card("t1", status="processing", worker_status="claimed", allowed_writes=["out"]),
        _card("t2", status="pending", allowed_writes=["out/a.json"]),
    ]
    snap = task_plan.build_snapshot(cards)
    assert snap["write_scope_overlaps"]["t2"] == ["out/a.json"]


def test_snapshot_disjoint_glob_writes_do_not_conflict():
    cards = [
        _card("t1", status="processing", worker_status="claimed", allowed_writes=["src/**"]),
        _card("t2", status="pending", allowed_writes=["docs/x.md"]),
    ]
    snap = task_plan.build_snapshot(cards)
    assert snap["write_scope_overlaps"] == {}
    assert "t2" in snap["ready"]


def test_snapshot_reports_invalid_depends_on_as_blocked_not_ready():
    cards = [
        _card("t1", status="pending", depends_on=["../etc"]),
    ]
    snap = task_plan.build_snapshot(cards)
    assert "t1" in snap["invalid_depends_on"]
    assert snap["blockers"]["t1"] == ["__invalid_depends_on__"]
    assert "t1" not in snap["ready"]


def test_existing_edges_from_cards_flags_invalid_legacy_card():
    cards = {
        "t1": {"depends_on": ["not a valid id!!"]},
        "t2": {"depends_on": []},
    }
    edges, invalid_ids = task_plan.existing_edges_from_cards(cards)
    assert invalid_ids == {"t1"}
    assert edges["t1"] == []


def test_validate_new_dependency_edge_rejects_dependency_with_invalid_depends_on():
    existing_edges = {"t1": [], "t2": []}
    with pytest.raises(task_plan.TaskPlanError, match="dependency_has_invalid_depends_on"):
        task_plan.validate_new_dependency_edge(
            "t3", ["t1"], existing_edges, invalid_ids={"t1"}
        )


def test_validate_new_dependency_edge_rejects_transitive_invalid_depends_on():
    # t3 depends on t2, and t2 depends on t1, but t1's own depends_on is
    # malformed -- fail closed even though t1 isn't a direct dependency.
    existing_edges = {"t1": [], "t2": ["t1"]}
    with pytest.raises(task_plan.TaskPlanError, match="dependency_has_invalid_depends_on"):
        task_plan.validate_new_dependency_edge(
            "t3", ["t2"], existing_edges, invalid_ids={"t1"}
        )


def test_filter_claimable_matches_runner_and_topic_within_ready_set():
    cards = [
        {**_card("t1"), "runner": "codex_a", "topic": "coding"},
        {**_card("t2"), "runner": "codex_b", "topic": "coding"},
    ]
    snap = task_plan.build_snapshot(cards)
    claimable = task_plan.filter_claimable(snap, cards, runner="codex_a", topic="coding")
    assert [c["task_id"] for c in claimable] == ["t1"]
