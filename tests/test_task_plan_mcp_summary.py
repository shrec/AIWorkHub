from __future__ import annotations

from aiworkhub import server, task_plan


def _full_plan() -> dict[str, object]:
    return {
        "ok": True,
        "schema_id": "aiworkhub.task_plan_snapshot.v1",
        "task_ids": ["DONE", "READY", "BLOCKED"],
        "lifecycle": {
            "DONE": "finished",
            "READY": "pending",
            "BLOCKED": "blocked",
        },
        "dependencies": {"DONE": [], "READY": [], "BLOCKED": ["READY"]},
        "dependents": {"DONE": [], "READY": ["BLOCKED"], "BLOCKED": []},
        "blockers": {"BLOCKED": ["READY"]},
        "ready": ["READY"],
        "ready_capacity": 1,
        "active_count": 0,
        "blocked_count": 1,
        "blocked_task_ids": ["BLOCKED"],
        "dependency_blocked_count": 1,
        "dependency_blocked_task_ids": ["BLOCKED"],
        "lifecycle_blocked_count": 1,
        "lifecycle_blocked_task_ids": ["BLOCKED"],
        "operational_blockers": {},
        "operational_blocked_task_ids": [],
        "operational_blocked_count": 0,
        "explicit_retry_task_ids": [],
        "explicit_retry_count": 0,
        "orphaned_processing": [],
        "orphaned_processing_count": 0,
        "invalid_depends_on": [],
        "write_scope_overlaps": {},
        "global_collision_free": False,
        "global_collision_count": 1,
        "global_collision_paths": ["src/shared.py"],
        "global_collision_task_ids": ["READY", "BLOCKED"],
        "global_collision_pairs": [["READY", "BLOCKED"]],
        "card_collision_free": {"DONE": True, "READY": False, "BLOCKED": False},
        "card_collision_task_ids": {"READY": ["BLOCKED"], "BLOCKED": ["READY"]},
        "card_collision_paths": {"READY": ["src/shared.py"], "BLOCKED": ["src/shared.py"]},
        "edge_count": 1,
        "layers": [
            {"index": 0, "task_ids": ["DONE", "READY"]},
            {"index": 1, "task_ids": ["BLOCKED"]},
        ],
        "critical_path": ["READY", "BLOCKED"],
        "critical_path_length": 2,
        "dag_valid": True,
        "cycle_nodes": [],
    }


def test_task_plan_mcp_defaults_to_actionable_summary(monkeypatch):
    monkeypatch.setattr(server.core, "task_plan_snapshot", _full_plan)

    result = server.aiworkhub_task_plan_snapshot()

    assert result["snapshot_mode"] == "summary"
    assert result["full_snapshot_available"] is True
    assert result["task_count"] == 3
    assert result["actionable_task_count"] == 2
    assert result["terminal_task_count"] == 1
    assert result["actionable_lifecycle"] == {
        "READY": "pending",
        "BLOCKED": "blocked",
    }
    assert result["ready"] == ["READY"]
    assert result["blocked_task_ids"] == ["BLOCKED"]
    assert result["critical_path"] == ["READY", "BLOCKED"]
    assert result["layer_count"] == 2
    assert "task_ids" not in result
    assert "lifecycle" not in result
    assert "dependencies" not in result
    assert "dependents" not in result
    assert "layers" not in result


def test_task_plan_mcp_full_mode_preserves_complete_dag(monkeypatch):
    full_plan = _full_plan()
    monkeypatch.setattr(server.core, "task_plan_snapshot", lambda: full_plan)

    result = server.aiworkhub_task_plan_snapshot(full=True)

    for key, value in full_plan.items():
        assert result[key] == value
    assert result["snapshot_mode"] == "full"
    assert result["full_snapshot_available"] is True


def test_task_plan_summary_projection_retains_collision_truth():
    cards = [
        {
            "task_id": "A",
            "status": "pending",
            "worker_status": "unclaimed",
            "allowed_writes": ["src/shared.py"],
            "depends_on": [],
            "created_at": "2026-01-01T00:00:00Z",
            "launch_request_id": "",
        },
        {
            "task_id": "B",
            "status": "pending",
            "worker_status": "unclaimed",
            "allowed_writes": ["src/shared.py"],
            "depends_on": [],
            "created_at": "2026-01-02T00:00:00Z",
            "launch_request_id": "",
        },
    ]
    full = task_plan.build_snapshot(cards)
    summary = task_plan.summarize_task_plan_snapshot(full)

    assert summary["global_collision_free"] is False
    assert summary["global_collision_count"] == 1
    assert summary["global_collision_pairs"] == [["A", "B"]]
    assert summary["card_collision_free"] == {"A": False, "B": False}
    assert summary["card_collision_task_ids"] == {"A": ["B"], "B": ["A"]}
    assert summary["card_collision_paths"] == {"A": ["src/shared.py"], "B": ["src/shared.py"]}

    # The bounded summary keeps current truth but drops the historical DAG.
    assert "actionable_lifecycle" in summary
    assert "dependencies" not in summary
    assert "dependents" not in summary
    assert "layers" not in summary
    assert "lifecycle" not in summary
    assert "task_ids" not in summary


def test_summary_excludes_superseded_reviewer_from_actionable_surfaces():
    cards = [
        {
            "task_id": "QR-superseded",
            "status": "superseded",
            "worker_status": "superseded",
            "quality_review": {"target_task_id": "T-old", "status": "pending"},
            "allowed_writes": ["src/shared.py"],
            "depends_on": [],
            "created_at": "2026-01-01T00:00:00Z",
        }
    ]

    full = task_plan.build_snapshot(cards)
    summary = task_plan.summarize_task_plan_snapshot(full)

    assert summary["actionable_lifecycle"] == {}
    assert summary["ready"] == []
    assert summary["active_count"] == 0
    assert summary["ready_capacity"] == 0
    assert summary["critical_path"] == []


def test_summary_exposes_terminal_artifacts_excluded_and_agrees_with_full():
    cards = [
        {
            "task_id": "T-acc",
            "status": "finished",
            "worker_status": "done",
            "accepted_request_id": "req-acc",
            "accepted_at": "2026-01-01T00:00:00Z",
            "accepted_by": "owner",
            "accept_evidence": {"acceptance_evidence_record": {"reference": "req-acc"}},
            "allowed_writes": [],
            "depends_on": [],
            "created_at": "2026-01-01T00:00:00Z",
            "launch_request_id": "",
        },
        {
            "task_id": "QR-retry-terminal",
            "status": "review",
            "worker_status": "review",
            "allowed_writes": [],
            "depends_on": [],
            "created_at": "2026-01-02T00:00:00Z",
            "launch_request_id": "",
            "quality_review": {
                "target_task_id": "T-acc",
                "target_request_id": "req-acc",
            },
        },
        {
            "task_id": "READY",
            "status": "pending",
            "worker_status": "unclaimed",
            "allowed_writes": [],
            "depends_on": [],
            "created_at": "2026-01-03T00:00:00Z",
            "launch_request_id": "",
        },
    ]
    full = task_plan.build_snapshot(cards)
    summary = task_plan.summarize_task_plan_snapshot(full)
    assert full["terminal_artifacts_excluded_count"] == 1
    assert summary["terminal_artifacts_excluded_count"] == 1
    assert summary["terminal_artifacts_excluded"] == full["terminal_artifacts_excluded"]
    assert summary["terminal_artifacts_excluded"][0]["target_status"] == "accepted"
    assert "QR-retry-terminal" not in full["task_ids"]
    assert "READY" in summary["ready"]


def test_summary_retains_rework_and_unresolved_artifacts():
    cards = [
        {
            "task_id": "T-rework",
            "status": "pending",
            "worker_status": "cancelled",
            "allowed_writes": [],
            "depends_on": [],
            "created_at": "2026-01-01T00:00:00Z",
            "launch_request_id": "",
        },
        {
            "task_id": "T-status-rework",
            "status": "rework",
            "worker_status": "unclaimed",
            "allowed_writes": [],
            "depends_on": [],
            "created_at": "2026-01-01T00:30:00Z",
            "launch_request_id": "",
        },
        {
            "task_id": "T-status-unresolved",
            "status": "unresolved",
            "worker_status": "unclaimed",
            "allowed_writes": [],
            "depends_on": [],
            "created_at": "2026-01-01T00:45:00Z",
            "launch_request_id": "",
        },
        {
            "task_id": "QR-rework",
            "status": "review",
            "worker_status": "review",
            "allowed_writes": [],
            "depends_on": [],
            "created_at": "2026-01-02T00:00:00Z",
            "launch_request_id": "",
            "quality_review": {
                "target_task_id": "T-rework",
                "target_request_id": "req-rework",
            },
        },
        {
            "task_id": "QR-status-rework",
            "status": "review",
            "worker_status": "review",
            "allowed_writes": [],
            "depends_on": [],
            "created_at": "2026-01-02T00:30:00Z",
            "launch_request_id": "",
            "quality_review": {"target_task_id": "T-status-rework"},
        },
        {
            "task_id": "QR-status-unresolved",
            "status": "review",
            "worker_status": "review",
            "allowed_writes": [],
            "depends_on": [],
            "created_at": "2026-01-02T00:45:00Z",
            "launch_request_id": "",
            "quality_review": {"target_task_id": "T-status-unresolved"},
        },
        {
            "task_id": "IMPL-unresolved",
            "status": "pending",
            "worker_status": "unclaimed",
            "allowed_writes": [],
            "depends_on": [],
            "created_at": "2026-01-03T00:00:00Z",
            "launch_request_id": "",
            "implementation": {
                "target_task_id": "T-missing",
            },
        },
    ]
    full = task_plan.build_snapshot(cards)
    summary = task_plan.summarize_task_plan_snapshot(full)
    assert full["terminal_artifacts_excluded_count"] == 0
    assert summary["terminal_artifacts_excluded_count"] == 0
    assert "QR-rework" in full["task_ids"]
    assert "QR-status-rework" in full["task_ids"]
    assert "QR-status-unresolved" in full["task_ids"]
    assert "IMPL-unresolved" in full["task_ids"]
    assert "QR-rework" in summary["actionable_lifecycle"]
    assert "QR-status-rework" in summary["actionable_lifecycle"]
    assert "QR-status-unresolved" in summary["actionable_lifecycle"]


def test_summary_keeps_artifact_when_live_target_conflicts_stale_recorded_terminal():
    cards = [
        {
            "task_id": "T-live-rework",
            "status": "pending",
            "worker_status": "cancelled",
            "allowed_writes": [],
            "depends_on": [],
            "created_at": "2026-01-01T00:00:00Z",
            "launch_request_id": "",
        },
        {
            "task_id": "QR-stale-finished",
            "status": "review",
            "worker_status": "review",
            "allowed_writes": [],
            "depends_on": [],
            "created_at": "2026-01-02T00:00:00Z",
            "launch_request_id": "",
            "quality_review": {
                "target_task_id": "T-live-rework",
                "target_status": "finished",
            },
        },
    ]
    full = task_plan.build_snapshot(cards)
    summary = task_plan.summarize_task_plan_snapshot(full)
    assert full["terminal_artifacts_excluded_count"] == 0
    assert summary["terminal_artifacts_excluded_count"] == 0
    assert summary["terminal_artifacts_excluded"] == []
    assert "QR-stale-finished" in full["task_ids"]
    assert "QR-stale-finished" in summary["actionable_lifecycle"]


def test_summary_agrees_when_dependent_of_excluded_artifact_is_ready():
    cards = [
        {
            "task_id": "T-done",
            "status": "finished",
            "worker_status": "done",
            "allowed_writes": [],
            "depends_on": [],
            "created_at": "2026-01-01T00:00:00Z",
            "launch_request_id": "",
        },
        {
            "task_id": "QR-retry-terminal",
            "status": "review",
            "worker_status": "review",
            "allowed_writes": [],
            "depends_on": [],
            "created_at": "2026-01-02T00:00:00Z",
            "launch_request_id": "",
            "quality_review": {"target_task_id": "T-done"},
        },
        {
            "task_id": "CHILD",
            "status": "pending",
            "worker_status": "unclaimed",
            "allowed_writes": [],
            "depends_on": ["QR-retry-terminal"],
            "created_at": "2026-01-03T00:00:00Z",
            "launch_request_id": "",
        },
    ]
    full = task_plan.build_snapshot(cards)
    summary = task_plan.summarize_task_plan_snapshot(full)
    assert full["terminal_artifacts_excluded_count"] == 1
    assert summary["terminal_artifacts_excluded_count"] == 1
    assert summary["terminal_artifacts_excluded"] == full["terminal_artifacts_excluded"]
    assert full["dependencies"]["CHILD"] == []
    assert "CHILD" in full["ready"]
    assert "CHILD" in summary["ready"]
    assert full["blockers"] == summary.get("blockers", full["blockers"])


def test_summary_keeps_bare_superseded_and_excludes_accepted():
    cards = [
        {
            "task_id": "T-acc",
            "status": "finished",
            "worker_status": "done",
            "accepted_request_id": "req-acc",
            "accepted_at": "2026-01-01T00:00:00Z",
            "accepted_by": "owner",
            "accept_evidence": {"acceptance_evidence_record": {"reference": "req-acc"}},
            "allowed_writes": [],
            "depends_on": [],
            "created_at": "2026-01-01T00:00:00Z",
            "launch_request_id": "",
        },
        {
            "task_id": "T-ghost",
            "status": "finished",
            "worker_status": "done",
            "accepted_request_id": "req-ghost",
            "accepted_at": "2026-01-01T00:00:00Z",
            "accepted_by": "owner",
            "accept_evidence": {"acceptance_evidence_record": {"reference": "req-ghost"}},
            "allowed_writes": [],
            "depends_on": [],
            "created_at": "2026-01-01T00:30:00Z",
            "launch_request_id": "",
        },
        {
            "task_id": "T-open-sup",
            "status": "pending",
            "worker_status": "unclaimed",
            "archive_operation": "superseded",
            "allowed_writes": [],
            "depends_on": [],
            "created_at": "2026-01-01T01:00:00Z",
            "launch_request_id": "",
        },
        {
            "task_id": "QR-acc",
            "status": "review",
            "worker_status": "review",
            "allowed_writes": [],
            "depends_on": [],
            "created_at": "2026-01-02T00:00:00Z",
            "launch_request_id": "",
            "quality_review": {"target_task_id": "T-acc"},
        },
        {
            "task_id": "QR-open-sup",
            "status": "review",
            "worker_status": "review",
            "allowed_writes": [],
            "depends_on": [],
            "created_at": "2026-01-02T01:00:00Z",
            "launch_request_id": "",
            "quality_review": {"target_task_id": "T-open-sup"},
        },
        {
            "task_id": "QR-recorded-acc",
            "status": "review",
            "worker_status": "review",
            "allowed_writes": [],
            "depends_on": [],
            "created_at": "2026-01-02T02:00:00Z",
            "launch_request_id": "",
            "quality_review": {
                "target_task_id": "T-ghost",
                "target_status": "accepted",
            },
        },
    ]
    full = task_plan.build_snapshot(cards)
    summary = task_plan.summarize_task_plan_snapshot(full)
    assert full["terminal_artifacts_excluded_count"] == 2
    assert summary["terminal_artifacts_excluded_count"] == 2
    by_target = {
        row["target_task_id"]: row["target_status"]
        for row in summary["terminal_artifacts_excluded"]
    }
    assert by_target["T-acc"] == "accepted"
    assert by_target["T-ghost"] == "accepted"
    assert "QR-acc" not in full["task_ids"]
    assert "QR-recorded-acc" not in full["task_ids"]
    assert "QR-open-sup" in full["task_ids"]
    assert "QR-open-sup" in summary["actionable_lifecycle"]


def test_summary_archive_reason_multi_hop_excludes_and_keeps_bare():
    cards = [
        {
            "task_id": "T-landed",
            "status": "finished",
            "worker_status": "done",
            "allowed_writes": [],
            "depends_on": [],
            "created_at": "2026-01-01T00:00:00Z",
            "launch_request_id": "",
        },
        {
            "task_id": "T-hop",
            "status": "pending",
            "worker_status": "unclaimed",
            "archive_operation": "superseded",
            "archive_reason": "superseded_by:T-landed",
            "allowed_writes": [],
            "depends_on": [],
            "created_at": "2026-01-01T01:00:00Z",
            "launch_request_id": "",
        },
        {
            "task_id": "T-mid",
            "status": "pending",
            "worker_status": "unclaimed",
            "archive_operation": "superseded",
            "archive_reason": "superseded_by:T-hop",
            "allowed_writes": [],
            "depends_on": [],
            "created_at": "2026-01-01T01:30:00Z",
            "launch_request_id": "",
        },
        {
            "task_id": "T-bare",
            "status": "pending",
            "worker_status": "unclaimed",
            "archive_operation": "superseded",
            "archive_reason": "operator_closed",
            "allowed_writes": [],
            "depends_on": [],
            "created_at": "2026-01-01T02:00:00Z",
            "launch_request_id": "",
        },
        {
            "task_id": "QR-mid",
            "status": "review",
            "worker_status": "review",
            "allowed_writes": [],
            "depends_on": [],
            "created_at": "2026-01-02T00:00:00Z",
            "launch_request_id": "",
            "quality_review": {"target_task_id": "T-mid"},
        },
        {
            "task_id": "QR-bare",
            "status": "review",
            "worker_status": "review",
            "allowed_writes": [],
            "depends_on": [],
            "created_at": "2026-01-02T01:00:00Z",
            "launch_request_id": "",
            "quality_review": {"target_task_id": "T-bare"},
        },
    ]
    full = task_plan.build_snapshot(cards)
    summary = task_plan.summarize_task_plan_snapshot(full)
    assert full["terminal_artifacts_excluded_count"] == 1
    assert summary["terminal_artifacts_excluded_count"] == 1
    assert full["terminal_artifacts_excluded"] == summary["terminal_artifacts_excluded"]
    assert "QR-mid" not in full["task_ids"]
    assert "QR-bare" in full["task_ids"]
    assert "QR-bare" in summary["actionable_lifecycle"]
    assert full["terminal_artifacts_excluded"][0]["target_status"] == "superseded"


def test_summary_excludes_every_terminal_artifact_beyond_published_200_cap():
    cards = [
        {
            "task_id": "T-done",
            "status": "finished",
            "worker_status": "done",
            "allowed_writes": [],
            "depends_on": [],
            "created_at": "2026-01-01T00:00:00Z",
            "launch_request_id": "",
        },
        {
            "task_id": "LIVE-READY",
            "status": "pending",
            "worker_status": "unclaimed",
            "allowed_writes": [],
            "depends_on": [],
            "created_at": "2026-01-03T00:00:00Z",
            "launch_request_id": "",
        },
        {
            "task_id": "T-rework",
            "status": "rework",
            "worker_status": "unclaimed",
            "allowed_writes": [],
            "depends_on": [],
            "created_at": "2026-01-01T02:00:00Z",
            "launch_request_id": "",
        },
        {
            "task_id": "QR-rework",
            "status": "review",
            "worker_status": "review",
            "allowed_writes": [],
            "depends_on": [],
            "created_at": "2026-01-02T02:00:00Z",
            "launch_request_id": "",
            "quality_review": {
                "target_task_id": "T-rework",
                "target_status": "rework",
            },
        },
    ]
    cards.extend(
        {
            "task_id": f"QR-{idx:03d}",
            "status": "review",
            "worker_status": "review",
            "allowed_writes": [],
            "depends_on": [],
            "created_at": f"2026-01-02T00:{idx:02d}:00Z",
            "launch_request_id": "",
            "quality_review": {
                "target_task_id": "T-done",
                "target_status": "finished",
            },
        }
        for idx in range(210)
    )
    full = task_plan.build_snapshot(cards)
    summary = task_plan.summarize_task_plan_snapshot(full)
    assert len(full["terminal_artifacts_excluded"]) == 200
    assert full["terminal_artifacts_excluded_count"] == 210
    assert summary["terminal_artifacts_excluded_count"] == 210
    assert summary["terminal_artifacts_excluded"] == full["terminal_artifacts_excluded"]
    assert "QR-209" not in full["task_ids"]
    assert "QR-209" not in summary["actionable_lifecycle"]
    assert "LIVE-READY" in summary["ready"]
    assert "QR-rework" in summary["actionable_lifecycle"]


def test_summary_fail_closed_when_live_target_slot_unusable():
    qr = {
        "task_id": "QR-recorded-acc",
        "status": "review",
        "worker_status": "review",
        "allowed_writes": [],
        "depends_on": [],
        "created_at": "2026-01-02T00:00:00Z",
        "launch_request_id": "",
        "quality_review": {
            "target_task_id": "T-ghost",
            "target_status": "accepted",
        },
    }
    cards = [
        qr,
        {
            "task_id": "READY",
            "status": "pending",
            "worker_status": "unclaimed",
            "allowed_writes": [],
            "depends_on": [],
            "created_at": "2026-01-03T00:00:00Z",
            "launch_request_id": "",
        },
    ]
    full = task_plan.build_snapshot(cards)
    summary = task_plan.summarize_task_plan_snapshot(full)
    assert full["terminal_artifacts_excluded_count"] == 0
    assert summary["terminal_artifacts_excluded_count"] == 0
    assert summary["terminal_artifacts_excluded"] == []
    assert "QR-recorded-acc" in full["task_ids"]
    assert "QR-recorded-acc" in summary["actionable_lifecycle"]
    assert task_plan.evaluate_terminal_artifact(qr, {"T-ghost": None}) is None
    assert task_plan.evaluate_terminal_artifact(qr, {}) is None
    assert "READY" in summary["ready"]


def test_summary_non_superseded_and_invalid_successor_ids_fail_closed():
    cards = [
        {
            "task_id": "T-landed",
            "status": "finished",
            "worker_status": "done",
            "allowed_writes": [],
            "depends_on": [],
            "created_at": "2026-01-01T00:00:00Z",
            "launch_request_id": "",
        },
        {
            "task_id": "T-can",
            "status": "pending",
            "worker_status": "unclaimed",
            "archive_operation": "cancelled",
            "superseded_by": "T-landed",
            "allowed_writes": [],
            "depends_on": [],
            "created_at": "2026-01-01T01:00:00Z",
            "launch_request_id": "",
        },
        {
            "task_id": "T-bad",
            "status": "pending",
            "worker_status": "unclaimed",
            "archive_operation": "superseded",
            "superseded_by": "../etc",
            "allowed_writes": [],
            "depends_on": [],
            "created_at": "2026-01-01T01:30:00Z",
            "launch_request_id": "",
        },
        {
            "task_id": "QR-can",
            "status": "review",
            "worker_status": "review",
            "allowed_writes": [],
            "depends_on": [],
            "created_at": "2026-01-02T00:00:00Z",
            "launch_request_id": "",
            "quality_review": {"target_task_id": "T-can"},
        },
        {
            "task_id": "QR-bad",
            "status": "review",
            "worker_status": "review",
            "allowed_writes": [],
            "depends_on": [],
            "created_at": "2026-01-02T01:00:00Z",
            "launch_request_id": "",
            "quality_review": {"target_task_id": "T-bad"},
        },
    ]
    full = task_plan.build_snapshot(cards)
    summary = task_plan.summarize_task_plan_snapshot(full)
    assert full["terminal_artifacts_excluded_count"] == 0
    assert summary["terminal_artifacts_excluded_count"] == 0
    assert "QR-can" in full["task_ids"]
    assert "QR-bad" in full["task_ids"]
    assert "QR-can" in summary["actionable_lifecycle"]
    assert "QR-bad" in summary["actionable_lifecycle"]
    assert task_plan.successor_task_id(cards[1]) == ""
    assert task_plan.successor_task_id(cards[2]) == ""
