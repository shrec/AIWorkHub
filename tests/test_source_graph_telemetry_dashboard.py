from __future__ import annotations

import json

from aiworkhub import dashboard, process_launcher


def _row(task_id: str, adapter: str, tool_use: dict) -> dict:
    return {
        "task_id": task_id,
        "adapter_id": adapter,
        "ai_infra_context": {"tool_use": tool_use},
    }


def _row_with_denials(
    task_id: str, adapter: str, tool_use: dict, provider_tool_denials: dict,
) -> dict:
    row = _row(task_id, adapter, tool_use)
    row["ai_infra_context"]["provider_tool_denials"] = provider_tool_denials
    return row


def test_source_graph_telemetry_separates_live_from_injected_and_stale() -> None:
    report = {
        "processes": [
            _row_with_denials(
                "LIVE",
                "deepseek_copilot_cli",
                {
                    "gated": True,
                    "satisfied": True,
                    "source_graph_satisfaction": "live_worker_call",
                    "source_graph_calls": 3,
                    "source_graph_live_calls": 2,
                    "source_graph_hit_count": 7,
                    "source_graph_zero_hit_calls": 1,
                    "source_graph_failed_calls": 0,
                    "source_graph_bytes": 900,
                    "source_graph_cache_hits": 1,
                    "policy_violations": 0,
                    "entries_tampered": 0,
                },
                {
                    "evidence_observed": True,
                    "permission_denials_total": 2,
                    "raw_discovery_denials": 1,
                    "raw_discovery_labels": ["rg"],
                },
            ),
            _row(
                "INJECTED",
                "claude_cli",
                {
                    "gated": True,
                    "satisfied": True,
                    "source_graph_satisfaction": "injected_receipt",
                    "source_graph_calls": 0,
                    "source_graph_live_calls": 0,
                    "source_graph_hit_count": 0,
                    "source_graph_zero_hit_calls": 0,
                    "source_graph_failed_calls": 0,
                    "policy_violations": 1,
                    "entries_tampered": 0,
                },
            ),
            _row(
                "STALE",
                "claude_cli",
                {
                    "gated": True,
                    "satisfied": False,
                    "source_graph_satisfaction": "stale_or_cached",
                    "source_graph_calls": 1,
                    "source_graph_live_calls": 0,
                    "source_graph_hit_count": 0,
                    "source_graph_zero_hit_calls": 1,
                    "source_graph_failed_calls": 1,
                    "source_graph_cache_hits": 1,
                    "policy_violations": 0,
                    "entries_tampered": 1,
                },
            ),
            # A retry for LIVE is older because the process list is newest first;
            # telemetry counts the task once and keeps the first row.
            _row(
                "LIVE",
                "deepseek_copilot_cli",
                {
                    "gated": True,
                    "satisfied": False,
                    "source_graph_satisfaction": "",
                    "source_graph_calls": 0,
                    "source_graph_live_calls": 0,
                },
            ),
        ]
    }

    result = dashboard._source_graph_telemetry(report)

    assert result["observed_tasks"] == 3
    assert result["gated_tasks"] == 3
    assert result["satisfied_tasks"] == 2
    assert result["source_graph_any_tasks"] == 3
    assert result["source_graph_live_tasks"] == 1
    assert result["source_graph_injected_only_tasks"] == 1
    assert result["source_graph_stale_or_cached_tasks"] == 1
    assert result["source_graph_missing_tasks"] == 0
    assert result["source_graph_calls"] == 4
    assert result["source_graph_live_calls"] == 2
    assert result["source_graph_hit_count"] == 7
    assert result["source_graph_zero_hit_calls"] == 2
    assert result["source_graph_failed_calls"] == 1
    assert result["source_graph_bytes"] == 900
    assert result["source_graph_cache_hits"] == 2
    assert result["policy_violation_tasks"] == 1
    assert result["policy_violations"] == 1
    assert result["tampered_ledger_tasks"] == 1
    assert result["provider_permission_denials"] == 2
    assert result["raw_discovery_denials"] == 1
    assert result["raw_discovery_denial_tasks"] == 1
    assert result["provider_denial_evidence_tasks"] == 1
    assert result["live_rate"] == 33.3
    assert result["any_rate"] == 100.0
    assert result["gate_satisfaction_rate"] == 66.7
    assert result["measurement_label"] == (
        "authenticated_calls_and_returned_bytes_only_no_token_or_cost_claim"
    )
    assert result["blocked_reason_counts"] == {"unspecified": 1}
    assert result["by_adapter"]["claude_cli"]["source_graph_zero_hit_calls"] == 1
    assert result["by_adapter"]["deepseek_copilot_cli"]["live_tasks"] == 1
    assert result["by_adapter"]["deepseek_copilot_cli"]["raw_discovery_denials"] == 1
    assert result["by_adapter"]["claude_cli"]["injected_only_tasks"] == 1


def test_source_graph_telemetry_ignores_ungated_tasks() -> None:
    result = dashboard._source_graph_telemetry(
        {"processes": [_row("DATA", "deepseek_copilot_cli", {"gated": False})]}
    )
    assert result["observed_tasks"] == 1
    assert result["gated_tasks"] == 0
    assert result["live_rate"] == 0.0


def test_provider_denial_parser_counts_without_persisting_raw_payload(tmp_path) -> None:
    output = tmp_path / "worker.jsonl"
    output.write_text(
        json.dumps({
            "type": "result",
            "permission_denials": [
                {"tool": "Bash", "input": {"command": "rg -n secret src"}},
                {"tool": "Write", "path": "/private/secret.txt"},
            ],
        }) + "\n",
        encoding="utf-8",
    )

    result = process_launcher._provider_tool_denials_from_output(output)

    assert result == {
        "schema_id": "aiworkhub.provider_tool_denials.v1",
        "evidence_observed": True,
        "permission_denials_total": 2,
        "raw_discovery_denials": 1,
        "raw_discovery_labels": ["rg"],
    }
    assert "secret" not in json.dumps(result)


def test_provider_denial_parser_reports_unobserved_not_false_zero(tmp_path) -> None:
    output = tmp_path / "worker.jsonl"
    output.write_text(json.dumps({"type": "result", "ok": True}) + "\n", encoding="utf-8")

    result = process_launcher._provider_tool_denials_from_output(output)

    assert result["evidence_observed"] is False
    assert result["permission_denials_total"] == 0
    assert result["raw_discovery_denials"] == 0
