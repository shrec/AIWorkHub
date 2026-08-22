from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import task_plan  # noqa: E402


def _card(task_id, *, status="pending", worker_status="unclaimed", allowed_writes=None, depends_on=None, created_at="2026-01-01T00:00:00Z", launch_request_id=None, **extra):
    return {
        "task_id": task_id,
        "status": status,
        "worker_status": worker_status,
        "allowed_writes": allowed_writes or [],
        "depends_on": depends_on or [],
        "created_at": created_at,
        "launch_request_id": (
            "request-existing"
            if launch_request_id is None and status == "processing"
            else (launch_request_id or "")
        ),
        **extra,
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


def test_snapshot_rewrites_superseded_dependency_to_finished_replacement():
    cards = [
        _card(
            "old",
            status="archived",
            archived_at="2026-01-02T00:00:00Z",
            archive_operation="superseded",
            superseded_by="replacement",
        ),
        _card("replacement", status="finished", worker_status="done"),
        _card("successor", depends_on=["old"]),
    ]

    snap = task_plan.build_snapshot(cards)

    assert snap["dependencies"]["successor"] == ["replacement"]
    assert snap["original_dependencies"]["successor"] == ["old"]
    assert snap["dependency_replacements"]["successor"]["old"] == {
        "chain": ["old"],
        "resolved_to": "replacement",
    }
    assert "successor" in snap["ready"]
    assert snap["dag_valid"] is True


def test_snapshot_blocks_until_superseded_replacement_finishes():
    cards = [
        _card(
            "old",
            status="archived",
            archived_at="2026-01-02T00:00:00Z",
            archive_operation="superseded",
            superseded_by="replacement",
        ),
        _card("replacement", status="pending"),
        _card("successor", depends_on=["old"]),
    ]

    snap = task_plan.build_snapshot(cards)

    assert snap["blockers"]["successor"] == ["replacement"]
    assert "successor" not in snap["ready"]


@pytest.mark.parametrize(
    ("archived_cards", "expected_error"),
    [
        (
            [
                _card(
                    "old",
                    status="archived",
                    archived_at="2026-01-02T00:00:00Z",
                    archive_operation="superseded",
                    superseded_by="missing",
                )
            ],
            "__superseded_replacement_not_found__:missing",
        ),
        (
            [
                _card(
                    "old",
                    status="archived",
                    archived_at="2026-01-02T00:00:00Z",
                    archive_operation="superseded",
                    superseded_by="next",
                ),
                _card(
                    "next",
                    status="archived",
                    archived_at="2026-01-03T00:00:00Z",
                    archive_operation="superseded",
                    superseded_by="old",
                ),
            ],
            "__superseded_replacement_cycle__:old",
        ),
        (
            [
                _card(
                    "old",
                    status="archived",
                    archived_at="2026-01-02T00:00:00Z",
                    archive_operation="archived",
                )
            ],
            "__archived_dependency_not_superseded__:old",
        ),
        (
            [
                _card(
                    "old",
                    status="archived",
                    archived_at="2026-01-02T00:00:00Z",
                    archive_operation="cancelled",
                    superseded_by="replacement",
                )
            ],
            "__archived_dependency_not_superseded__:old",
        ),
        (
            [
                _card(
                    "old",
                    status="archived",
                    archived_at="2026-01-02T00:00:00Z",
                    archive_operation="superseded",
                    superseded_by="../etc",
                )
            ],
            "__invalid_superseded_replacement__:old",
        ),
    ],
)
def test_snapshot_fails_closed_for_invalid_replacement_chain(
    archived_cards, expected_error
):
    snap = task_plan.build_snapshot(
        [*archived_cards, _card("successor", depends_on=["old"])]
    )

    assert snap["blockers"]["successor"] == [expected_error]
    assert snap["dependency_resolution_errors"]["successor"] == [expected_error]
    assert "successor" not in snap["ready"]
    assert snap["dag_valid"] is False


def test_snapshot_detects_cycle_created_by_superseded_dependency_rewrite():
    cards = [
        _card(
            "old",
            status="archived",
            archived_at="2026-01-02T00:00:00Z",
            archive_operation="superseded",
            superseded_by="replacement",
        ),
        _card("replacement", depends_on=["successor"]),
        _card("successor", depends_on=["old"]),
    ]

    snap = task_plan.build_snapshot(cards)

    assert snap["cycle_nodes"] == ["replacement", "successor"]
    assert snap["dag_valid"] is False
    assert snap["ready"] == []


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
    assert snap["ready_capacity"] == 3
    assert snap["layers"] == [{"index": 0, "task_ids": ["t1", "t2", "t3"]}]


def test_snapshot_projects_layers_and_current_critical_path():
    cards = [
        _card("root", status="finished"),
        _card("left", depends_on=["root"]),
        _card("right", depends_on=["root"]),
        _card("leaf", depends_on=["left"]),
    ]

    snap = task_plan.build_snapshot(cards)

    assert snap["dag_valid"] is True
    assert snap["edge_count"] == 3
    assert snap["active_count"] == 3
    assert snap["blocked_count"] == 1
    assert snap["dependency_blocked_count"] == 1
    assert snap["lifecycle_blocked_count"] == 0
    assert snap["layers"] == [
        {"index": 0, "task_ids": ["root"]},
        {"index": 1, "task_ids": ["left", "right"]},
        {"index": 2, "task_ids": ["leaf"]},
    ]
    assert snap["critical_path"] == ["root", "left", "leaf"]
    assert snap["critical_path_length"] == 3


def test_snapshot_blocked_count_includes_lifecycle_and_names_dag_subset():
    cards = [
        _card("lifecycle", status="blocked", worker_status="blocked"),
        _card("dependency", depends_on=["unfinished"]),
        _card("unfinished", status="processing", worker_status="claimed"),
    ]

    snap = task_plan.build_snapshot(cards)

    assert snap["blocked_count"] == 2
    assert snap["blocked_task_ids"] == ["dependency", "lifecycle"]
    assert snap["dependency_blocked_count"] == 1
    assert snap["dependency_blocked_task_ids"] == ["dependency"]
    assert snap["lifecycle_blocked_count"] == 1
    assert snap["lifecycle_blocked_task_ids"] == ["lifecycle"]
    assert snap["active_count"] == 2


def test_snapshot_terminal_blocked_task_is_not_active_or_a_critical_path():
    snap = task_plan.build_snapshot([
        _card("terminal", status="blocked", worker_status="worker_failed"),
    ])

    assert snap["active_count"] == 0
    assert snap["critical_path"] == []
    assert snap["critical_path_length"] == 0


def test_snapshot_legacy_cycle_is_visible_and_has_no_fabricated_critical_path():
    cards = [
        _card("a", depends_on=["b"]),
        _card("b", depends_on=["a"]),
    ]

    snap = task_plan.build_snapshot(cards)

    assert snap["dag_valid"] is False
    assert snap["cycle_nodes"] == ["a", "b"]
    assert snap["critical_path"] == []
    assert snap["critical_path_length"] == 0


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


def test_snapshot_global_collision_truth_separates_independent_ready_card():
    cards = [
        _card("collide_a", allowed_writes=["src/shared.py"], created_at="2026-01-01T00:00:00Z"),
        _card("collide_b", allowed_writes=["src/shared.py"], created_at="2026-01-02T00:00:00Z"),
        _card("independent", allowed_writes=["src/own.py"], created_at="2026-01-03T00:00:00Z"),
    ]
    snap = task_plan.build_snapshot(cards)

    assert snap["global_collision_free"] is False
    assert snap["global_collision_count"] == 1
    assert snap["global_collision_paths"] == ["src/shared.py"]
    assert snap["global_collision_task_ids"] == ["collide_a", "collide_b"]
    assert snap["global_collision_pairs"] == [["collide_a", "collide_b"]]

    # Exact per-card truth: each colliding card only sees its own conflict.
    assert snap["card_collision_free"]["collide_a"] is False
    assert snap["card_collision_free"]["collide_b"] is False
    assert snap["card_collision_free"]["independent"] is True
    assert snap["card_collision_task_ids"]["collide_a"] == ["collide_b"]
    assert snap["card_collision_task_ids"]["collide_b"] == ["collide_a"]
    assert snap["card_collision_paths"]["collide_a"] == ["src/shared.py"]
    assert "independent" not in snap["card_collision_task_ids"]
    assert "independent" not in snap["card_collision_paths"]

    # An unrelated global collision must never block the collision-free,
    # dependency-ready card.
    assert "independent" in snap["ready"]
    assert snap["write_scope_overlaps"] == {"collide_b": ["src/shared.py"]}


def test_snapshot_per_card_collision_truth_reports_only_involved_conflicts():
    cards = [
        _card("left", allowed_writes=["src/a.py"]),
        _card("middle", allowed_writes=["src/a.py", "src/b.py"]),
        _card("right", allowed_writes=["src/b.py"]),
    ]
    snap = task_plan.build_snapshot(cards)

    # left <-> middle share src/a.py; middle <-> right share src/b.py;
    # left and right never touch each other.
    assert snap["global_collision_free"] is False
    assert snap["global_collision_count"] == 2
    assert snap["global_collision_pairs"] == [["left", "middle"], ["middle", "right"]]
    assert snap["global_collision_paths"] == ["src/a.py", "src/b.py"]
    assert snap["global_collision_task_ids"] == ["left", "middle", "right"]

    assert snap["card_collision_free"]["left"] is False
    assert snap["card_collision_free"]["middle"] is False
    assert snap["card_collision_free"]["right"] is False
    assert snap["card_collision_task_ids"]["left"] == ["middle"]
    assert snap["card_collision_task_ids"]["middle"] == ["left", "right"]
    assert snap["card_collision_task_ids"]["right"] == ["middle"]
    assert snap["card_collision_paths"]["left"] == ["src/a.py"]
    assert snap["card_collision_paths"]["middle"] == ["src/a.py", "src/b.py"]
    assert snap["card_collision_paths"]["right"] == ["src/b.py"]


def test_snapshot_empty_write_scope_overlaps_does_not_imply_global_collision_free():
    cards = [
        _card("p1", status="processing", worker_status="claimed", allowed_writes=["src/shared.py"]),
        _card("p2", status="processing", worker_status="claimed", allowed_writes=["src/shared.py"]),
    ]
    snap = task_plan.build_snapshot(cards)

    # No pending card is being gated, so the claim-eligibility projection is
    # empty -- but two live workers overlap the same file.
    assert snap["write_scope_overlaps"] == {}
    assert snap["ready"] == []
    assert snap["global_collision_free"] is False
    assert snap["global_collision_count"] == 1
    assert snap["global_collision_pairs"] == [["p1", "p2"]]
    assert snap["card_collision_free"]["p1"] is False
    assert snap["card_collision_free"]["p2"] is False


def test_snapshot_collision_truth_ignores_finished_and_blocked_cards():
    cards = [
        _card("active", allowed_writes=["src/shared.py"]),
        _card("done", status="finished", worker_status="done", allowed_writes=["src/shared.py"]),
        _card("stuck", status="blocked", worker_status="blocked", allowed_writes=["src/shared.py"]),
    ]
    snap = task_plan.build_snapshot(cards)

    assert snap["global_collision_free"] is True
    assert snap["global_collision_count"] == 0
    assert snap["global_collision_paths"] == []
    assert snap["global_collision_task_ids"] == []
    assert snap["global_collision_pairs"] == []
    assert snap["card_collision_free"] == {
        "active": True,
        "done": True,
        "stuck": True,
    }


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


def test_existing_edges_from_cards_projects_superseded_alias_edge():
    cards = {
        "old": _card(
            "old",
            status="archived",
            archived_at="2026-01-02T00:00:00Z",
            archive_operation="superseded",
            archive_reason="superseded_by:replacement; legacy audit",
        ),
        "replacement": _card("replacement"),
    }

    edges, invalid_ids = task_plan.existing_edges_from_cards(cards)

    assert edges["old"] == ["replacement"]
    assert invalid_ids == set()


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


def test_snapshot_excludes_reviewer_retry_when_exact_target_is_terminal():
    cards = [
        _card("T-done", status="finished", worker_status="done"),
        _card(
            "QR-retry-terminal",
            status="review",
            worker_status="review",
            topic="quality_review",
            quality_review={
                "target_task_id": "T-done",
                "target_request_id": "req-done",
            },
        ),
        _card("T-ready"),
    ]
    snap = task_plan.build_snapshot(cards)
    assert "QR-retry-terminal" not in snap["task_ids"]
    assert "QR-retry-terminal" not in snap["ready"]
    assert snap["terminal_artifacts_excluded_count"] == 1
    assert snap["terminal_artifacts_excluded"] == [
        {
            "task_id": "QR-retry-terminal",
            "target_task_id": "T-done",
            "target_status": "finished",
            "artifact_kind": "reviewer",
            "reason": "exact_target_terminal",
        }
    ]
    assert "T-ready" in snap["ready"]


def test_snapshot_retains_reviewer_retry_when_target_is_rework():
    cards = [
        _card("T-rework", worker_status="cancelled"),
        _card("T-status-rework", status="rework"),
        _card("T-status-rework-req", status="rework_required"),
        _card(
            "QR-retry-rework",
            status="review",
            worker_status="review",
            topic="quality_review",
            quality_review={
                "target_task_id": "T-rework",
                "target_request_id": "req-rework",
            },
        ),
        _card(
            "QR-status-rework",
            status="review",
            worker_status="review",
            quality_review={"target_task_id": "T-status-rework"},
        ),
        _card(
            "QR-status-rework-req",
            status="review",
            worker_status="review",
            quality_review={"target_task_id": "T-status-rework-req"},
        ),
    ]
    snap = task_plan.build_snapshot(cards)
    assert "QR-retry-rework" in snap["task_ids"]
    assert "QR-status-rework" in snap["task_ids"]
    assert "QR-status-rework-req" in snap["task_ids"]
    assert snap["terminal_artifacts_excluded_count"] == 0
    assert snap["terminal_artifacts_excluded"] == []


def test_snapshot_retains_unresolved_implementation_artifact():
    cards = [
        _card("T-status-unresolved", status="unresolved"),
        _card(
            "IMPL-retry-unresolved",
            implementation={
                "target_task_id": "T-missing",
                "target_request_id": "req-missing",
            },
        ),
        _card(
            "QR-status-unresolved",
            status="review",
            worker_status="review",
            quality_review={"target_task_id": "T-status-unresolved"},
        ),
    ]
    snap = task_plan.build_snapshot(cards)
    assert "IMPL-retry-unresolved" in snap["task_ids"]
    assert "IMPL-retry-unresolved" in snap["ready"]
    assert "QR-status-unresolved" in snap["task_ids"]
    assert snap["terminal_artifacts_excluded_count"] == 0


def test_snapshot_keeps_artifact_when_live_target_conflicts_stale_recorded_terminal():
    cards = [
        _card("T-live-rework", worker_status="cancelled"),
        _card(
            "QR-stale-accepted",
            status="review",
            worker_status="review",
            topic="quality_review",
            quality_review={
                "target_task_id": "T-live-rework",
                "target_status": "accepted",
            },
        ),
        _card(
            "IMPL-stale-finished",
            implementation={
                "target_task_id": "T-missing",
            },
        ),
    ]
    snap = task_plan.build_snapshot(cards)
    assert "QR-stale-accepted" in snap["task_ids"]
    assert "IMPL-stale-finished" in snap["task_ids"]
    assert "IMPL-stale-finished" in snap["ready"]
    assert snap["terminal_artifacts_excluded_count"] == 0
    assert snap["terminal_artifacts_excluded"] == []


def test_card_status_evidence_normalizes_worker_status_done():
    card = _card("T-done-only", status="pending", worker_status="done")
    assert task_plan.card_status_evidence(card) == "finished"
    qr = _card(
        "QR-done-lookup",
        status="review",
        worker_status="review",
        quality_review={"target_task_id": "T-done-only"},
    )
    snap = task_plan.build_snapshot([card, qr])
    assert "QR-done-lookup" not in snap["task_ids"]
    assert snap["terminal_artifacts_excluded"][0]["target_status"] == "finished"


def test_snapshot_excludes_reviewer_without_target_status_when_live_finished():
    cards = [
        _card("T-done", status="finished", worker_status="done"),
        _card(
            "QR-no-recorded-status",
            status="review",
            worker_status="review",
            quality_review={"target_task_id": "T-done", "target_request_id": "req-done"},
        ),
    ]
    snap = task_plan.build_snapshot(cards)
    assert "QR-no-recorded-status" not in snap["task_ids"]
    assert snap["terminal_artifacts_excluded_count"] == 1
    assert snap["terminal_artifacts_excluded"][0]["target_status"] == "finished"


def test_snapshot_dependent_of_excluded_artifact_is_not_phantom_blocked():
    cards = [
        _card("T-done", status="finished", worker_status="done"),
        _card(
            "QR-retry-terminal",
            status="review",
            worker_status="review",
            quality_review={"target_task_id": "T-done"},
        ),
        _card("CHILD", depends_on=["QR-retry-terminal"]),
    ]
    snap = task_plan.build_snapshot(cards)
    assert "QR-retry-terminal" not in snap["task_ids"]
    assert snap["dependencies"]["CHILD"] == []
    assert "CHILD" not in snap["blockers"]
    assert "CHILD" in snap["ready"]
    assert snap["dependency_blocked_task_ids"] == []


def test_snapshot_keeps_superseded_without_successor_and_excludes_landed():
    cards = [
        _card("T-landed", status="finished", worker_status="done"),
        _card("T-open-sup", archive_operation="superseded"),
        _card("T-closed-sup", archive_operation="superseded", superseded_by="T-landed"),
        _card("T-open-can", archive_operation="cancelled"),
        _card(
            "QR-open-sup",
            status="review",
            worker_status="review",
            quality_review={"target_task_id": "T-open-sup"},
        ),
        _card(
            "QR-closed-sup",
            status="review",
            worker_status="review",
            quality_review={"target_task_id": "T-closed-sup"},
        ),
        _card(
            "QR-open-can",
            status="review",
            worker_status="review",
            quality_review={"target_task_id": "T-open-can"},
        ),
    ]
    snap = task_plan.build_snapshot(cards)
    assert "QR-open-sup" in snap["task_ids"]
    assert "QR-open-can" in snap["task_ids"]
    assert "QR-closed-sup" not in snap["task_ids"]
    assert snap["terminal_artifacts_excluded_count"] == 1
    assert snap["terminal_artifacts_excluded"][0]["task_id"] == "QR-closed-sup"


def test_snapshot_excludes_reviewer_without_target_status_when_live_accepted():
    cards = [
        _card(
            "T-acc",
            status="finished",
            worker_status="done",
            accepted_request_id="req-acc",
            accepted_at="2026-01-01T00:00:00Z",
            accepted_by="owner",
            accept_evidence={"acceptance_evidence_record": {"reference": "req-acc"}},
        ),
        _card(
            "QR-accepted",
            status="review",
            worker_status="review",
            quality_review={"target_task_id": "T-acc", "target_request_id": "req-acc"},
        ),
    ]
    snap = task_plan.build_snapshot(cards)
    assert "QR-accepted" not in snap["task_ids"]
    assert snap["terminal_artifacts_excluded_count"] == 1
    assert snap["terminal_artifacts_excluded"][0]["target_status"] == "accepted"


def test_evaluate_terminal_artifact_ignores_recorded_accepted_when_live_target_absent():
    assert "superseded" not in task_plan.TERMINAL_TARGET_STATUSES
    assert task_plan.is_terminal_target_status("accepted")
    assert task_plan.is_terminal_target_status("finished")
    assert not task_plan.is_terminal_target_status("superseded")
    qr = _card(
        "QR-recorded-acc",
        status="review",
        worker_status="review",
        quality_review={"target_task_id": "T-ghost", "target_status": "accepted"},
    )
    row = task_plan.evaluate_terminal_artifact(qr, {})
    assert row is None
    snap = task_plan.build_snapshot([qr])
    assert "QR-recorded-acc" in snap["task_ids"]
    assert snap["terminal_artifacts_excluded_count"] == 0
    assert snap["terminal_artifacts_excluded"] == []


def test_collect_terminal_artifacts_sorts_then_bounds_over_200_permutations():
    target = _card("T-done", status="finished", worker_status="done")
    artifacts = [
        _card(
            f"QR-{idx:03d}",
            status="review",
            worker_status="review",
            quality_review={
                "target_task_id": "T-done",
                "target_status": "finished",
            },
        )
        for idx in range(210)
    ]
    expected = [f"QR-{idx:03d}" for idx in range(task_plan.MAX_TERMINAL_ARTIFACT_ROWS)]
    reversed_cards = [target, *reversed(artifacts)]
    reversed_rows = task_plan.collect_terminal_artifacts(reversed_cards)
    assert len(reversed_rows) == task_plan.MAX_TERMINAL_ARTIFACT_ROWS
    assert [row["task_id"] for row in reversed_rows] == expected
    stride = artifacts[0::2] + artifacts[1::2]
    stride_rows = task_plan.collect_terminal_artifacts([target, *stride])
    assert [row["task_id"] for row in stride_rows] == expected
    assert stride_rows[0]["target_status"] == "finished"
    assert stride_rows[0]["artifact_kind"] == "reviewer"


def test_has_landed_successor_archive_reason_multi_hop_and_no_snapshot():
    landed = _card("T-landed", status="finished", worker_status="done")
    hop = _card(
        "T-hop",
        archive_operation="superseded",
        archive_reason="superseded_by:T-landed",
    )
    mid = _card(
        "T-mid",
        archive_operation="superseded",
        archive_reason="superseded_by:T-hop",
    )
    bare = _card(
        "T-bare",
        archive_operation="superseded",
        archive_reason="operator_closed",
    )
    by_id = {
        "T-landed": landed,
        "T-hop": hop,
        "T-mid": mid,
        "T-bare": bare,
    }
    assert task_plan.has_landed_successor(mid, by_id)
    assert task_plan.has_landed_successor(hop, by_id)
    assert not task_plan.has_landed_successor(bare, by_id)
    assert not task_plan.has_landed_successor(mid, None)
    assert not task_plan.has_landed_successor(mid, {})
    qr = _card(
        "QR-mid",
        status="review",
        worker_status="review",
        quality_review={"target_task_id": "T-mid"},
    )
    qr_bare = _card(
        "QR-bare",
        status="review",
        worker_status="review",
        quality_review={"target_task_id": "T-bare"},
    )
    assert task_plan.evaluate_terminal_artifact(qr, None) is None
    assert task_plan.evaluate_terminal_artifact(qr, {}) is None
    assert task_plan.evaluate_terminal_artifact(qr, by_id) == {
        "task_id": "QR-mid",
        "target_task_id": "T-mid",
        "target_status": "superseded",
        "artifact_kind": "reviewer",
        "reason": "exact_target_terminal",
    }
    assert task_plan.evaluate_terminal_artifact(qr_bare, by_id) is None
    snap = task_plan.build_snapshot([landed, hop, mid, bare, qr, qr_bare])
    assert "QR-mid" not in snap["task_ids"]
    assert "QR-bare" in snap["task_ids"]
    assert snap["terminal_artifacts_excluded_count"] == 1


def test_card_status_evidence_rework_unresolved_positive_and_negative():
    assert task_plan.card_status_evidence(None) == ""
    assert task_plan.card_status_evidence("not-a-card") == ""
    assert task_plan.card_status_evidence(_card("t-rework", status="rework")) == "rework"
    assert task_plan.card_status_evidence(_card("t-unresolved", status="unresolved")) == "unresolved"
    assert task_plan.card_status_evidence(
        _card("t-rework-req", status="rework_required")
    ) == "rework_required"
    assert task_plan.card_status_evidence(
        _card("t-worker-rework", status="pending", worker_status="rework")
    ) == "rework"
    accepted = _card(
        "t-acc",
        status="finished",
        accepted_request_id="req-acc",
        accepted_by="owner",
        accepted_at="2026-01-01T00:00:00Z",
        accept_evidence={"acceptance_evidence_record": {"reference": "req-acc"}},
    )
    assert task_plan.card_status_evidence(accepted) == "accepted"
    assert task_plan.card_status_evidence(
        _card("t-fin", status="finished", worker_status="done")
    ) == "finished"
    assert task_plan.card_status_evidence(_card("t-done", status="done")) == "finished"
    assert task_plan.card_status_evidence(_card("t-completed", status="completed")) == "finished"
    assert task_plan.card_status_evidence(
        _card("t-stale", status="stale_already_done")
    ) == "finished"
    assert task_plan.card_status_evidence(_card("t-pending", status="pending")) == "pending"
    assert task_plan.is_rework_or_unresolved_status("rework")
    assert task_plan.is_rework_or_unresolved_status("unresolved")
    assert task_plan.is_rework_or_unresolved_status("rework_required")
    assert not task_plan.is_rework_or_unresolved_status("finished")
    assert not task_plan.is_rework_or_unresolved_status("accepted")


def test_dag_excludes_every_terminal_artifact_beyond_published_200_cap():
    target = _card("T-done", status="finished", worker_status="done")
    live = _card("LIVE-READY", status="pending")
    rework_target = _card("T-rework", status="rework")
    rework = _card(
        "QR-rework",
        status="review",
        worker_status="review",
        quality_review={"target_task_id": "T-rework", "target_status": "rework"},
    )
    artifacts = [
        _card(
            f"QR-{idx:03d}",
            status="review",
            worker_status="review",
            quality_review={
                "target_task_id": "T-done",
                "target_status": "finished",
            },
        )
        for idx in range(210)
    ]
    cards = [target, live, rework_target, rework, *reversed(artifacts)]
    snap = task_plan.build_snapshot(cards)
    published = snap["terminal_artifacts_excluded"]
    rows, excluded_ids = task_plan.terminal_artifact_projection(cards)
    assert published == rows
    assert excluded_ids == task_plan.terminal_artifact_exclusion_ids(cards)
    assert len(published) == task_plan.MAX_TERMINAL_ARTIFACT_ROWS
    assert snap["terminal_artifacts_excluded_count"] == 210
    assert len(excluded_ids) == 210
    assert "QR-209" in excluded_ids
    assert "QR-209" not in snap["task_ids"]
    assert "QR-000" not in snap["task_ids"]
    assert "LIVE-READY" in snap["task_ids"]
    assert "QR-rework" in snap["task_ids"]
    assert "LIVE-READY" in snap["ready"]
    assert "QR-rework" not in excluded_ids


def test_terminal_artifact_projection_empty_task_id_is_consistent():
    target = _card("T-done", status="finished", worker_status="done")
    empty = {
        "task_id": "",
        "status": "review",
        "worker_status": "review",
        "allowed_writes": [],
        "depends_on": [],
        "created_at": "2026-01-02T00:00:00Z",
        "quality_review": {"target_task_id": "T-done", "target_status": "finished"},
    }
    labeled = _card(
        "QR-done",
        status="review",
        worker_status="review",
        quality_review={"target_task_id": "T-done", "target_status": "finished"},
    )
    missing = _card(
        "QR-ghost",
        status="review",
        worker_status="review",
        quality_review={"target_task_id": "T-ghost", "target_status": "accepted"},
    )
    cards = [target, empty, labeled, missing]
    by_id = {"T-done": target, "QR-done": labeled, "QR-ghost": missing}
    rows, excluded = task_plan.terminal_artifact_projection(cards, by_id)
    assert task_plan.evaluate_terminal_artifact(empty, None) is None
    assert task_plan.evaluate_terminal_artifact(empty, by_id) is not None
    assert any(not row["task_id"] for row in rows)
    assert "" in excluded
    assert "QR-done" in excluded
    assert "QR-ghost" not in excluded
    assert excluded == {str(row.get("task_id") or "") for row in rows}
    assert rows == task_plan.collect_terminal_artifacts(cards, by_id)
    assert excluded == task_plan.terminal_artifact_exclusion_ids(cards, by_id)

def test_is_valid_task_id_and_successor_task_id_public():
    assert task_plan.is_valid_task_id("T-landed")
    assert task_plan.is_valid_task_id("needfix-NF-2026-00387-r1")
    assert not task_plan.is_valid_task_id("")
    assert not task_plan.is_valid_task_id("../etc")
    assert not task_plan.is_valid_task_id("bad id")
    landed = _card("T-landed", status="finished", worker_status="done")
    hop = _card("T-hop", archive_operation="superseded", superseded_by="T-landed")
    via_reason = _card(
        "T-reason",
        archive_operation="superseded",
        archive_reason="superseded_by:T-landed;note=legacy",
    )
    invalid = _card("T-bad", archive_operation="superseded", superseded_by="../etc")
    assert task_plan.successor_task_id(hop) == "T-landed"
    assert task_plan.successor_task_id(via_reason) == "T-landed"
    assert task_plan.successor_task_id(invalid) == ""
    assert task_plan.successor_task_id(None) == ""
    assert task_plan.successor_task_id(landed) == ""

def test_has_landed_successor_iterative_cycle_and_status_aliases():
    loop_a = _card("A", archive_operation="superseded", superseded_by="B")
    loop_b = _card("B", archive_operation="superseded", superseded_by="A")
    assert not task_plan.has_landed_successor(loop_a, {"A": loop_a, "B": loop_b})
    completed = _card("T-completed", status="completed")
    hop = _card("T-hop", archive_operation="superseded", superseded_by="T-completed")
    assert task_plan.has_landed_successor(hop, {"T-completed": completed, "T-hop": hop})
    stale = _card("T-stale", status="stale_already_done")
    cancelled = _card("T-can", archive_operation="cancelled", superseded_by="T-stale")
    assert task_plan.successor_task_id(cancelled) == ""
    assert not task_plan.has_landed_successor(
        cancelled, {"T-stale": stale, "T-can": cancelled}
    )
    open_rework = _card("T-rework", status="rework")
    retry = _card("T-retry", archive_operation="superseded", superseded_by="T-rework")
    assert not task_plan.has_landed_successor(
        retry, {"T-rework": open_rework, "T-retry": retry}
    )
    assert task_plan.is_terminal_target_status("done")
    assert task_plan.is_terminal_target_status("completed")
    assert task_plan.is_terminal_target_status("stale_already_done")
    assert not task_plan.is_terminal_target_status("rework")


def test_evaluate_terminal_artifact_fail_closed_on_unusable_live_slot():
    qr = _card(
        "QR-recorded-acc",
        status="review",
        worker_status="review",
        quality_review={"target_task_id": "T-ghost", "target_status": "accepted"},
    )
    assert task_plan.evaluate_terminal_artifact(qr, {"T-ghost": None}) is None
    assert task_plan.evaluate_terminal_artifact(qr, {"T-ghost": "not-a-card"}) is None
    live_rework = _card("T-ghost", status="pending", worker_status="cancelled")
    assert task_plan.evaluate_terminal_artifact(qr, {"T-ghost": live_rework}) is None
    live_accepted = _card(
        "T-ghost",
        status="finished",
        worker_status="done",
        accepted_request_id="req-ghost",
        accepted_at="2026-01-01T00:00:00Z",
        accepted_by="owner",
        accept_evidence={"acceptance_evidence_record": {"reference": "req-ghost"}},
    )
    assert task_plan.evaluate_terminal_artifact(qr, {"T-ghost": live_accepted}) is not None


def test_non_superseded_and_invalid_successor_ids_fail_closed_on_dag_and_projection():
    landed = _card("T-landed", status="finished", worker_status="done")
    cancelled = _card(
        "T-can",
        archive_operation="cancelled",
        superseded_by="T-landed",
    )
    invalid = _card(
        "T-bad",
        archive_operation="superseded",
        superseded_by="../etc",
    )
    qr_can = _card(
        "QR-can",
        status="review",
        worker_status="review",
        quality_review={"target_task_id": "T-can"},
    )
    qr_bad = _card(
        "QR-bad",
        status="review",
        worker_status="review",
        quality_review={"target_task_id": "T-bad"},
    )
    dependent = _card("T-next", depends_on=["T-can"])
    cards = [landed, cancelled, invalid, qr_can, qr_bad, dependent]
    by_id = {card["task_id"]: card for card in cards}
    assert task_plan.successor_task_id(cancelled) == ""
    assert task_plan.successor_task_id(invalid) == ""
    assert task_plan._superseded_replacement(cancelled) == ""
    assert task_plan._superseded_replacement(invalid) == ""
    assert task_plan.evaluate_terminal_artifact(qr_can, by_id) is None
    assert task_plan.evaluate_terminal_artifact(qr_bad, by_id) is None
    snap = task_plan.build_snapshot(cards)
    assert snap["terminal_artifacts_excluded_count"] == 0
    assert "QR-can" in snap["task_ids"]
    assert "QR-bad" in snap["task_ids"]
    resolved, _chain, error = task_plan.resolve_superseded_dependency("T-can", by_id)
    assert resolved == "T-can"
    assert error is None
    archived_cancelled = _card(
        "old",
        status="archived",
        archived_at="2026-01-02T00:00:00Z",
        archive_operation="cancelled",
        superseded_by="T-landed",
    )
    _resolved, _chain, archived_error = task_plan.resolve_superseded_dependency(
        "old",
        {"old": archived_cancelled, "T-landed": landed},
    )
    assert archived_error == "__archived_dependency_not_superseded__:old"
