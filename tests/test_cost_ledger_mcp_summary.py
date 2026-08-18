from __future__ import annotations

from aiworkhub import server


def _ledger(**_kwargs):
    return {
        "tool": "aiworkhub_task_cost_ledger",
        "counts": {"union_rows": 2},
        "cost_quality": {"known_records": 2},
        "cache_quality": {"observed_records": 1},
        "aggregates": {
            "by_topic": {"topic-a": {"total_tokens": 10}},
            "by_runner": {"runner-a": {"total_tokens": 10}},
            "by_model": {"model-a": {"total_tokens": 10}},
            "by_provider": {"provider-a": {"total_tokens": 10}},
            "by_role": {"worker": {"total_tokens": 6}, "reviewer": {"total_tokens": 4}},
            "by_day": {"2026-08-04": {"total_tokens": 10}},
        },
        "tasks": [{"task_id": "TASK_A"}] if _kwargs.get("include_tasks") else [],
    }


def test_cost_ledger_mcp_defaults_to_bounded_dimensions(monkeypatch):
    monkeypatch.setattr(server.cost_ledger, "build_cost_ledger", _ledger)

    result = server.aiworkhub_task_cost_ledger()

    assert result["snapshot_mode"] == "summary"
    assert result["full_snapshot_available"] is True
    # Derived from the real aggregate map and sorted, so by_role -- worker vs
    # reviewer spend -- is named instead of vanishing behind a stale literal.
    assert result["omitted_dimensions"] == ["by_role", "by_runner", "by_topic"]
    assert result["detail_request"] == {"full": True}
    assert set(result["aggregates"]) == {"by_model", "by_provider", "by_day"}
    assert result["cost_quality"] == {"known_records": 2}
    assert result["cache_quality"] == {"observed_records": 1}


def test_cost_ledger_mcp_full_and_task_flags_are_independent(monkeypatch):
    monkeypatch.setattr(server.cost_ledger, "build_cost_ledger", _ledger)

    summary_with_tasks = server.aiworkhub_task_cost_ledger(include_tasks=True)
    full_without_tasks = server.aiworkhub_task_cost_ledger(full=True)

    assert summary_with_tasks["tasks"] == [{"task_id": "TASK_A"}]
    assert "by_runner" not in summary_with_tasks["aggregates"]
    assert full_without_tasks["snapshot_mode"] == "full"
    assert full_without_tasks["tasks"] == []
    assert "by_runner" in full_without_tasks["aggregates"]
    assert "by_topic" in full_without_tasks["aggregates"]
    assert "by_role" in full_without_tasks["aggregates"]
