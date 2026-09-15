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


def test_summary_bounds_historical_blocked_ids_and_keeps_exact_counts(monkeypatch):
    hist = [f"HIST-{i:03d}" for i in range(400)]
    plan = _full_plan()
    plan.update(
        {
            "task_ids": ["DONE", "READY", "BLOCKED"] + hist,
            "lifecycle": {
                "DONE": "finished",
                "READY": "pending",
                "BLOCKED": "blocked",
                **{task_id: "rework" for task_id in hist},
            },
            "blocked_count": 401,
            "blocked_task_ids": ["BLOCKED"] + hist,
            "dependency_blocked_count": 1,
            "dependency_blocked_task_ids": ["BLOCKED"],
            "lifecycle_blocked_count": 400,
            "lifecycle_blocked_task_ids": hist,
        }
    )
    monkeypatch.setattr(server.core, "task_plan_snapshot", lambda: plan)

    summary = server.aiworkhub_task_plan_snapshot()

    assert summary["snapshot_mode"] == "summary"
    assert summary["full_snapshot_available"] is True
    assert summary["task_count"] == 403
    assert summary["actionable_task_count"] == 402
    assert summary["terminal_task_count"] == 1
    assert summary["ready_count"] == 1
    assert summary["blocked_count"] == 401
    assert summary["lifecycle_blocked_count"] == 400
    assert summary["global_collision_count"] == 1

    fields = summary["sample_bounds"]["fields"]
    cap = summary["sample_bounds"]["max_sample_count"]
    assert 0 < cap < 400

    blocked = summary["blocked_task_ids"]
    assert len(blocked) == cap
    assert blocked == (["BLOCKED"] + hist)[:cap]
    assert fields["blocked_task_ids"] == {
        "total_count": 401,
        "returned_count": cap,
        "truncated": True,
    }

    lifecycle_blocked = summary["lifecycle_blocked_task_ids"]
    assert len(lifecycle_blocked) == cap
    assert lifecycle_blocked == hist[:cap]
    assert fields["lifecycle_blocked_task_ids"] == {
        "total_count": 400,
        "returned_count": cap,
        "truncated": True,
    }

    actionable = summary["actionable_lifecycle"]
    assert len(actionable) == cap
    assert list(actionable) == (["READY", "BLOCKED"] + hist)[:cap]
    assert fields["actionable_lifecycle"] == {
        "total_count": 402,
        "returned_count": cap,
        "truncated": True,
    }

    assert summary["ready"] == ["READY"]
    assert fields["ready"]["truncated"] is False

    assert "lifecycle" not in summary
    assert "task_ids" not in summary
    assert "dependencies" not in summary
    assert "layers" not in summary
    assert {"lifecycle", "task_ids", "dependencies", "layers"} <= set(
        summary["omitted_fields"]
    )


def test_full_mode_preserves_complete_blocked_history_authority(monkeypatch):
    hist = [f"HIST-{i:03d}" for i in range(400)]
    plan = _full_plan()
    plan.update(
        {
            "task_ids": ["DONE", "READY", "BLOCKED"] + hist,
            "lifecycle": {
                "DONE": "finished",
                "READY": "pending",
                "BLOCKED": "blocked",
                **{task_id: "rework" for task_id in hist},
            },
            "blocked_count": 401,
            "blocked_task_ids": ["BLOCKED"] + hist,
            "dependency_blocked_count": 1,
            "dependency_blocked_task_ids": ["BLOCKED"],
            "lifecycle_blocked_count": 400,
            "lifecycle_blocked_task_ids": hist,
        }
    )
    monkeypatch.setattr(server.core, "task_plan_snapshot", lambda: plan)

    summary = server.aiworkhub_task_plan_snapshot()
    full = server.aiworkhub_task_plan_snapshot(full=True)

    assert full["snapshot_mode"] == "full"
    assert full["full_snapshot_available"] is True
    assert full["task_ids"] == ["DONE", "READY", "BLOCKED"] + hist
    assert full["lifecycle"] == plan["lifecycle"]
    assert full["blocked_task_ids"] == ["BLOCKED"] + hist
    assert full["lifecycle_blocked_task_ids"] == hist
    assert "sample_bounds" not in full

    assert summary["task_count"] == len(full["task_ids"])
    assert summary["actionable_task_count"] == 402
    assert summary["blocked_count"] == full["blocked_count"]
    assert summary["lifecycle_blocked_count"] == len(
        full["lifecycle_blocked_task_ids"]
    )
    assert len(summary["lifecycle_blocked_task_ids"]) < len(
        full["lifecycle_blocked_task_ids"]
    )


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


def _pending_card(task_id: str, path: str, minute: int) -> dict[str, object]:
    return {
        "task_id": task_id,
        "status": "pending",
        "worker_status": "unclaimed",
        "allowed_writes": [path],
        "depends_on": [],
        "created_at": f"2026-01-01T00:{minute % 60:02d}:00Z",
        "launch_request_id": "",
    }


def test_summary_collision_sample_keeps_late_active_colliding_card():
    # 58 collision-free cards created first, then the only colliding pair.
    # Both the insertion order and the sorted order of the dense
    # ``card_collision_free`` map therefore put the colliding cards last.
    cards = [
        _pending_card(f"OLD-{idx:03d}", f"src/old_{idx:03d}.py", idx)
        for idx in range(58)
    ]
    cards.append(_pending_card("ZLATE-A", "src/late_shared.py", 58))
    cards.append(_pending_card("ZLATE-B", "src/late_shared.py", 59))

    full = task_plan.build_snapshot(cards)
    summary = task_plan.summarize_task_plan_snapshot(full)
    cap = summary["sample_bounds"]["max_sample_count"]

    assert len(cards) == 60
    assert cap < len(cards)
    assert len(full["card_collision_free"]) == 60
    assert full["card_collision_free"]["ZLATE-A"] is False
    assert full["card_collision_free"]["ZLATE-B"] is False

    # A plain insertion-order head sample of the dense source map is all-True
    # here, i.e. exactly the all-clear the bounded payload must never report
    # while ``global_collision_count`` is non-zero.
    head = list(full["card_collision_free"].items())[:cap]
    assert all(free for _, free in head)

    flags = summary["card_collision_free"]
    assert summary["global_collision_free"] is False
    assert summary["global_collision_count"] == 1
    assert len(flags) == cap
    assert flags["ZLATE-A"] is False
    assert flags["ZLATE-B"] is False
    assert sorted(tid for tid, free in flags.items() if not free) == [
        "ZLATE-A",
        "ZLATE-B",
    ]
    assert summary["sample_bounds"]["fields"]["card_collision_free"] == {
        "total_count": 60,
        "returned_count": cap,
        "truncated": True,
        "colliding_total_count": 2,
        "colliding_returned_count": 2,
        "colliding_omitted_count": 0,
    }

    # The peer/path maps name the same cards the flags sample reports False.
    assert summary["card_collision_task_ids"] == {
        "ZLATE-A": ["ZLATE-B"],
        "ZLATE-B": ["ZLATE-A"],
    }
    assert summary["card_collision_paths"]["ZLATE-A"] == ["src/late_shared.py"]

    # full=true keeps the complete per-card authority unchanged.
    assert full["global_collision_pairs"] == [["ZLATE-A", "ZLATE-B"]]
    assert "sample_bounds" not in full


def test_summary_collision_sample_bounds_colliding_cards_and_peer_maps():
    cards = []
    for idx in range(60):
        path = f"src/pair_{idx:03d}.py"
        cards.append(_pending_card(f"P-{idx:03d}-A", path, idx))
        cards.append(_pending_card(f"P-{idx:03d}-B", path, idx))

    full = task_plan.build_snapshot(cards)
    summary = task_plan.summarize_task_plan_snapshot(full)
    cap = summary["sample_bounds"]["max_sample_count"]
    fields = summary["sample_bounds"]["fields"]

    assert len(full["card_collision_free"]) == 120
    assert full["global_collision_count"] == 60
    assert len(full["card_collision_task_ids"]) == 120
    assert len(full["card_collision_paths"]) == 120

    flags = summary["card_collision_free"]
    assert summary["global_collision_count"] == 60
    assert len(flags) == cap
    # Every slot is spent on a colliding card, and the omitted colliding
    # remainder is stated rather than implied by the generic truncation flag.
    assert all(free is False for free in flags.values())
    assert fields["card_collision_free"] == {
        "total_count": 120,
        "returned_count": cap,
        "truncated": True,
        "colliding_total_count": 120,
        "colliding_returned_count": cap,
        "colliding_omitted_count": 120 - cap,
    }

    peers = summary["card_collision_task_ids"]
    paths = summary["card_collision_paths"]
    assert list(peers) == sorted(flags)
    assert list(paths) == sorted(flags)
    assert peers["P-000-A"] == ["P-000-B"]
    assert paths["P-000-B"] == ["src/pair_000.py"]
    assert fields["card_collision_task_ids"] == {
        "total_count": 120,
        "returned_count": cap,
        "truncated": True,
    }
    assert fields["card_collision_paths"] == fields["card_collision_task_ids"]

    assert len(summary["global_collision_task_ids"]) == cap
    assert fields["global_collision_task_ids"]["total_count"] == 120
    assert len(summary["global_collision_pairs"]) == cap
    assert fields["global_collision_pairs"]["total_count"] == 60


def test_summary_bounds_operational_maps_and_keeps_exact_counts(monkeypatch):
    ops = [f"OPS-{idx:03d}" for idx in range(120)]
    overlapping = [f"OVL-{idx:03d}" for idx in range(80)]
    plan = _full_plan()
    plan.update(
        {
            "task_ids": ["DONE", "READY", "BLOCKED"] + ops + overlapping,
            "lifecycle": {
                "DONE": "finished",
                "READY": "pending",
                "BLOCKED": "blocked",
                **{task_id: "pending" for task_id in ops + overlapping},
            },
            "operational_blockers": {
                task_id: "processing_without_launch_request" for task_id in ops
            },
            "operational_blocked_task_ids": ops,
            "operational_blocked_count": 120,
            "explicit_retry_task_ids": ops,
            "explicit_retry_count": 120,
            "write_scope_overlaps": {
                task_id: ["READY"] for task_id in overlapping
            },
        }
    )
    monkeypatch.setattr(server.core, "task_plan_snapshot", lambda: plan)

    summary = server.aiworkhub_task_plan_snapshot()
    full = server.aiworkhub_task_plan_snapshot(full=True)
    cap = summary["sample_bounds"]["max_sample_count"]
    fields = summary["sample_bounds"]["fields"]

    # Exact aggregates survive the bound.
    assert summary["operational_blocked_count"] == 120
    assert summary["explicit_retry_count"] == 120

    assert list(summary["operational_blockers"]) == ops[:cap]
    assert fields["operational_blockers"] == {
        "total_count": 120,
        "returned_count": cap,
        "truncated": True,
    }
    assert summary["operational_blocked_task_ids"] == ops[:cap]
    assert fields["operational_blocked_task_ids"]["total_count"] == 120
    assert summary["explicit_retry_task_ids"] == ops[:cap]
    assert fields["explicit_retry_task_ids"]["total_count"] == 120

    assert list(summary["write_scope_overlaps"]) == overlapping[:cap]
    assert fields["write_scope_overlaps"] == {
        "total_count": 80,
        "returned_count": cap,
        "truncated": True,
    }

    # full=true remains the complete, unsampled authority.
    assert len(full["operational_blockers"]) == 120
    assert len(full["write_scope_overlaps"]) == 80
    assert full["operational_blocked_task_ids"] == ops
    assert "sample_bounds" not in full
