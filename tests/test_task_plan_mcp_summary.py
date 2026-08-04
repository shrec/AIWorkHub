from __future__ import annotations

from aiworkhub import server


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
