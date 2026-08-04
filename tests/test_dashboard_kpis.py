from aiworkhub.dashboard_kpis import build_kpi_snapshot
from aiworkhub.dashboard import _compact_ai_infra, _merge_ai_infra


def _run(
    task_id,
    state,
    timestamp,
    adapter="deepseek",
    calls=0,
    live_calls=0,
    stages=None,
    raw_context_bytes=0,
    optimized_context_bytes=None,
    bundle_bytes=0,
    prompt_bytes=0,
    prompt_budget_bytes=0,
    prompt_mode="initial",
    usage=None,
):
    estimate = {
        "raw_context_bytes": raw_context_bytes,
        "bundle_bytes": bundle_bytes,
    }
    if optimized_context_bytes is not None:
        estimate["optimized_section_bytes"] = optimized_context_bytes
    return {
        "task_id": task_id,
        "state": state,
        "finished_at": timestamp,
        "adapter_id": adapter,
        "ai_infra_context": {
            "tool_use": {
                "source_graph_calls": calls,
                "source_graph_live_calls": live_calls,
                "source_graph_stage_counts": stages or {},
                "policy_violations": 0,
            },
            "estimate": estimate,
            "prompt_budget": {
                "mode": prompt_mode,
                "total_bytes": prompt_bytes,
                "max_bytes": prompt_budget_bytes,
                "delta_rework": prompt_mode == "rework_delta",
            },
            "usage": usage or {},
        },
    }


def _build(processes, *, total_requests=None, manager_decisions=None):
    return build_kpi_snapshot(
        process_report={
            "processes": processes,
            "total_requests": len(processes) if total_requests is None else total_requests,
            "truncated": False,
        },
        source_graph={
            "source_graph_calls": 10,
            "source_graph_failed_calls": 2,
            "source_graph_mode_attributed_calls": 8,
            "source_graph_stage_attributed_calls": 7,
            "source_graph_mode_counts": {"focus": 4, "slice": 2, "impact": 2},
            "source_graph_stage_counts": {"orientation": 3, "implementation": 2, "validation": 2, "unspecified": 3},
            "source_graph_latency": {"count": 10, "p50_ms": 4.5, "p95_ms": 18.0},
            "source_graph_call_gaps": {
                "count": 9,
                "p50_seconds": 12.0,
                "p95_seconds": 45.0,
                "long_gap_threshold_seconds": 900,
                "long_gap_count": 2,
                "long_gap_rate": 22.2,
                "interpretation": "observed_inter_call_gap_not_model_inactivity",
            },
            "source_graph_evidence_rows": {
                "entity_rows": 20,
                "edge_rows": 12,
                "file_rows": 8,
            },
            "source_graph_index_revision_counts": {
                "aiworkhub.source_graph.semantic.v5": 10,
            },
            "live_rate": 75.0,
            "gate_satisfaction_rate": 80.0,
        },
        project_context={
            "session_current_state": {"requested_tasks": 4, "executed_tasks": 3, "hit_count": 2},
            "ai_memory": {"requested_tasks": 2, "executed_tasks": 2, "hit_count": 1},
            "kb": {"requested_tasks": 0, "executed_tasks": 0},
        },
        callback_health={
            "by_state": {"delivered": 9, "dead_letter": 1},
            "backlog_count": 2,
            "retry_count": 3,
        },
        cost_totals={"total_tokens": 1234, "cost_usd": 0.75},
        process_limit=50,
        manager_decision_totals=manager_decisions,
    )


def test_kpis_use_latest_run_per_task_and_truthful_terminal_denominator():
    result = _build(
        [
            _run("A", "review_ready", "2026-08-01T12:00:00Z", calls=4, live_calls=4),
            _run("A", "validation_failed", "2026-07-31T12:00:00Z", calls=1),
            _run("B", "validation_failed", "2026-08-01T13:00:00Z", adapter="claude"),
            _run("C", "processing", "2026-08-01T14:00:00Z"),
        ]
    )

    assert result["window"]["observed_runs"] == 3
    assert result["headline"]["terminal_runs"] == 2
    assert result["headline"]["review_ready_runs"] == 1
    assert result["headline"]["review_ready_rate"] == 50.0
    assert result["headline"]["validation_failed_rate"] == 50.0
    assert result["data_quality"]["acceptance_rate_available"] is False
    assert "manager_decisions" in result["data_quality"]["acceptance_rate_reason"]


def test_kpis_report_only_explicit_manager_acceptance_decisions():
    result = _build(
        [
            _run("A", "accepted", "2026-08-01T12:00:00Z"),
            _run("B", "accepted", "2026-08-01T13:00:00Z"),
            _run("C", "rejected", "2026-08-01T14:00:00Z"),
            _run("D", "review_ready", "2026-08-01T15:00:00Z"),
        ]
    )

    assert result["headline"]["manager_decisions"] == 3
    assert result["headline"]["manager_acceptance_rate"] == 66.7
    assert result["data_quality"]["acceptance_rate_available"] is True


def test_kpis_prefer_exact_canonical_manager_decisions_over_process_states():
    result = _build(
        [_run("A", "accepted", "2026-08-01T12:00:00Z")],
        manager_decisions={"accepted": 2, "rejected": 3, "total": 5},
    )

    assert result["headline"]["manager_decisions"] == 5
    assert result["headline"]["accepted_runs"] == 2
    assert result["headline"]["rejected_runs"] == 3
    assert result["headline"]["manager_acceptance_rate"] == 40.0
    assert "canonical_task_event_ledger" in result["data_quality"]["acceptance_rate_reason"]


def test_kpis_calculate_callback_tool_and_context_denominators():
    result = _build([_run("A", "review_ready", "2026-08-01T12:00:00Z")])

    assert result["schema_id"] == "aiworkhub.kpi.dashboard.v4"
    assert result["headline"]["callback_delivery_rate"] == 90.0
    assert result["headline"]["source_graph_useful_call_rate"] == 80.0
    assert result["headline"]["source_graph_mode_attribution_rate"] == 80.0
    assert result["headline"]["source_graph_stage_attribution_rate"] == 70.0
    assert result["headline"]["source_graph_latency_p95_ms"] == 18.0
    assert result["headline"]["source_graph_call_gap_p50_seconds"] == 12.0
    assert result["headline"]["source_graph_call_gap_p95_seconds"] == 45.0
    assert result["headline"]["source_graph_long_call_gap_count"] == 2
    assert result["headline"]["source_graph_long_call_gap_rate"] == 22.2
    assert result["headline"]["source_graph_entity_rows"] == 20
    assert result["headline"]["source_graph_edge_rows"] == 12
    assert result["headline"]["source_graph_file_rows"] == 8
    assert result["headline"]["source_graph_index_revisions"] == 1
    assert result["source_graph_index_revisions"] == [
        {"name": "aiworkhub.source_graph.semantic.v5", "calls": 10},
    ]
    assert result["data_quality"]["source_graph_call_gap_samples"] == 9
    assert result["data_quality"]["source_graph_long_call_gap_threshold_seconds"] == 900
    assert result["data_quality"]["source_graph_call_gap_interpretation"] == (
        "observed_inter_call_gap_not_model_inactivity"
    )
    assert result["context"][0]["execution_rate"] == 75.0
    assert result["context"][2]["execution_rate"] is None


def test_kpis_order_and_bound_daily_buckets_and_report_truncation():
    processes = [
        _run(f"T{day}", "review_ready", f"2026-07-{day:02d}T12:00:00Z")
        for day in range(1, 32)
    ]
    processes.extend([
        _run("T32", "review_ready", "2026-08-01T12:00:00Z"),
        _run("T33", "review_ready", "2026-08-02T12:00:00Z"),
        _run("T34", "review_ready", "2026-08-03T12:00:00Z"),
        _run("T35", "review_ready", "2026-08-04T12:00:00Z"),
    ])
    result = _build(processes, total_requests=70)

    assert len(result["daily"]) == 30
    assert result["daily"][0]["date"] == "2026-07-06"
    assert result["daily"][-1]["date"] == "2026-08-04"
    assert result["window"]["truncated"] is True
    assert result["data_quality"]["process_window_truncated"] is True


def test_kpis_handle_empty_and_invalid_inputs_without_false_percentages():
    result = _build(
        [_run("A", "processing", "not-a-timestamp")],
    )

    assert result["headline"]["review_ready_rate"] is None
    assert result["daily"] == []
    assert result["data_quality"]["invalid_timestamp_rows"] == 1


def test_kpis_aggregate_semantic_edit_bytes_without_claiming_token_savings():
    focused = _run("A", "review_ready", "2026-08-04T18:00:00Z")
    focused["ai_infra_context"]["semantic_edit"] = {
        "schema_id": "aiworkhub.semantic_edit_runtime_evidence.v1",
        "observed": True,
        "file_count": 1,
        "range_count": 2,
        "file_bytes": 10_000,
        "old_region_bytes": 400,
        "replacement_bytes": 250,
        "model_reemitted_old_bytes": 0,
    }
    legacy = _run("B", "review_ready", "2026-08-04T18:01:00Z")
    legacy["ai_infra_context"]["semantic_edit"] = {
        "schema_id": "aiworkhub.semantic_edit_runtime_evidence.v0",
        "observed": True,
        "file_bytes": 999_999,
        "replacement_bytes": 1,
    }

    result = _build([focused, legacy])
    semantic = result["semantic_edit"]

    assert semantic["bounded_runs"] == 2
    assert semantic["evidence_observed_runs"] == 1
    assert semantic["evidence_unobserved_runs"] == 1
    assert semantic["file_bytes"] == 10_000
    assert semantic["old_region_bytes"] == 400
    assert semantic["replacement_bytes"] == 250
    assert semantic["replacement_to_file_byte_rate"] == 2.5
    assert semantic["structural_bytes_not_reemitted"] == 9_750
    assert semantic["structural_byte_ratio"] == 40.0
    assert semantic["token_savings_available"] is False
    assert semantic["cost_savings_available"] is False
    assert result["headline"]["semantic_edit_observed_runs"] == 1


def test_kpis_report_truthful_stage_cohorts_and_byte_economics_without_token_claims():
    result = _build([
        _run(
            "A", "review_ready", "2026-08-01T12:00:00Z",
            calls=3, live_calls=3,
            stages={"orientation": 1, "implementation": 1, "validation": 1},
            raw_context_bytes=10_000, bundle_bytes=2_500,
            prompt_bytes=40_000, prompt_budget_bytes=160_000,
        ),
        _run(
            "B", "validation_failed", "2026-08-01T13:00:00Z",
            calls=1, live_calls=1, stages={"orientation": 1},
            raw_context_bytes=2_000, bundle_bytes=3_000,
            prompt_bytes=20_000, prompt_budget_bytes=112_000,
            prompt_mode="rework_delta",
        ),
    ])

    cohorts = {row["name"]: row for row in result["tool_use_cohorts"]}
    assert cohorts["continuous_use"]["review_ready_rate"] == 100.0
    assert cohorts["live_single_stage"]["review_ready_rate"] == 0.0
    assert result["economics"]["measured_tasks"] == 2
    assert result["economics"]["pre_optimization_section_bytes"] == 12_000
    assert result["economics"]["raw_context_bytes"] == 12_000
    assert result["economics"]["delivered_bundle_bytes"] == 5_500
    assert result["economics"]["net_context_bytes_delta"] == 6_500
    assert result["economics"]["estimated_context_bytes_avoided"] == 6_500
    assert result["economics"]["estimated_context_bytes_added"] == 0
    assert result["economics"]["gross_context_bytes_avoided"] == 7_500
    assert result["economics"]["gross_context_bytes_added"] == 1_000
    assert result["economics"]["compressed_tasks"] == 1
    assert result["economics"]["expanded_tasks"] == 1
    assert result["economics"]["context_delivery_direction"] == "compressed"
    assert result["economics"]["context_delta_rate"] == 54.2
    assert result["economics"]["context_compression_rate"] == 54.2
    assert result["economics"]["context_expansion_rate"] is None
    assert result["economics"]["raw_file_counterfactual_available"] is False
    assert result["economics"]["source_selection_savings_available"] is False
    assert result["economics"]["source_selection_savings_reason"] == (
        "no_raw_file_counterfactual_baseline"
    )
    assert result["economics"]["prompt_measured_tasks"] == 2
    assert result["economics"]["initial_prompt_tasks"] == 1
    assert result["economics"]["rework_prompt_tasks"] == 1
    assert result["economics"]["average_prompt_bytes"] == 30_000.0
    assert result["economics"]["prompt_budget_utilization_rate"] == 22.1
    assert result["headline"]["average_prompt_bytes"] == 30_000.0
    assert result["economics"]["token_savings_available"] is False


def test_kpis_report_context_expansion_instead_of_false_compression() -> None:
    result = _build([
        _run(
            "A",
            "review_ready",
            "2026-08-04T17:51:09Z",
            raw_context_bytes=4_285,
            bundle_bytes=5_136,
        ),
    ])

    economics = result["economics"]
    headline = result["headline"]
    assert economics["context_delivery_direction"] == "expanded"
    assert economics["net_context_bytes_delta"] == -851
    assert economics["estimated_context_bytes_avoided"] == 0
    assert economics["estimated_context_bytes_added"] == 851
    assert economics["gross_context_bytes_added"] == 851
    assert economics["expanded_tasks"] == 1
    assert economics["context_delta_rate"] == -19.9
    assert economics["context_compression_rate"] is None
    assert economics["context_expansion_rate"] == 19.9
    assert headline["context_delivery_direction"] == "expanded"
    assert headline["estimated_context_bytes_added"] == 851


def test_kpis_separate_optional_suppression_from_envelope_overhead() -> None:
    result = _build([
        _run(
            "A",
            "review_ready",
            "2026-08-04T17:51:09Z",
            raw_context_bytes=2_000,
            optimized_context_bytes=1_200,
            bundle_bytes=1_500,
        ),
    ])

    economics = result["economics"]
    headline = result["headline"]
    assert economics["optimization_measured_tasks"] == 1
    assert economics["optimization_bytes_removed"] == 800
    assert economics["optimization_reduction_rate"] == 40.0
    assert economics["envelope_measured_tasks"] == 1
    assert economics["envelope_bytes_added"] == 300
    assert economics["envelope_overhead_rate"] == 25.0
    assert economics["net_context_bytes_delta"] == 500
    assert headline["optimization_bytes_removed"] == 800
    assert headline["envelope_bytes_added"] == 300


def test_kpis_wire_provider_usage_into_context_economics() -> None:
    result = _build([
        _run(
            "A",
            "review_ready",
            "2026-08-01T12:00:00Z",
            adapter="claude_cli",
            raw_context_bytes=10_000,
            bundle_bytes=2_500,
            usage={
                "input_tokens": 8_000,
                "output_tokens": 400,
                "cached_input_tokens": 3_000,
                "cache_creation_input_tokens": 500,
                "cost_usd": 0.12,
                "cost_observed": True,
            },
        )
    ])

    provider = result["economics"]["provider_measurement"]
    assert provider["summary"]["total_tasks"] == 1
    assert provider["summary"]["overall_cache_hit_rate"] == 0.2609
    assert provider["summary"]["cost_per_review_ready_usd"] == 0.12
    assert result["headline"]["provider_cache_hit_rate"] == 26.1
    assert result["headline"]["cost_per_review_ready_usd"] == 0.12


def test_process_event_prompt_budget_survives_bounded_dashboard_projection():
    compact = _compact_ai_infra({
        "prompt_budget": {
            "schema_id": "aiworkhub.worker_prompt_budget.v1",
            "mode": "rework_delta",
            "total_bytes": 24_000,
            "max_bytes": 112_000,
            "remaining_bytes": 88_000,
            "utilization_percent": 21.43,
            "delta_rework": True,
            "sections": {
                "task_contract_bytes": 4_000,
                "project_context_bytes": 12_000,
            },
        },
    })

    assert compact["prompt_budget"] == {
        "schema_id": "aiworkhub.worker_prompt_budget.v1",
        "mode": "rework_delta",
        "total_bytes": 24_000,
        "max_bytes": 112_000,
        "remaining_bytes": 88_000,
        "utilization_percent": 21.43,
        "delta_rework": True,
        "byte_labels_are_token_truth": False,
        "sections": {
            "task_contract_bytes": 4_000,
            "project_context_bytes": 12_000,
        },
    }


def test_terminal_usage_merge_preserves_launch_context_evidence() -> None:
    launch = _compact_ai_infra({
        "project_context": {
            "sections": [{
                "name": "source_graph",
                "requested": True,
                "executed": True,
                "bytes": 2400,
            }],
            "estimated_raw_context_vs_bundle_bytes": {
                "raw_context_bytes": 10_000,
                "bundle_bytes": 2_500,
            },
        },
    })
    terminal = _compact_ai_infra({
        "usage": {
            "input_tokens": 8_000,
            "output_tokens": 400,
            "cached_input_tokens": 3_000,
            "cost_usd": 0.12,
            "cost_observed": True,
        },
        "project_context_acknowledgement": {"acknowledged": True},
    })

    merged = _merge_ai_infra(launch, terminal)

    assert merged["source_graph"]["bytes"] == 2400
    assert merged["estimate"]["pre_optimization_section_bytes"] == 10_000
    assert merged["estimate"]["raw_context_bytes"] == 10_000
    assert merged["estimate"]["optimized_section_bytes_observed"] is False
    assert merged["usage"]["input_tokens"] == 8_000
    assert merged["usage"]["cost_usd"] == 0.12


def test_compact_context_marks_optimized_section_measurement_observed() -> None:
    compact = _compact_ai_infra({
        "project_context": {
            "estimated_raw_context_vs_bundle_bytes": {
                "raw_context_bytes": 2_000,
                "optimized_context_bytes": 1_200,
                "bundle_bytes": 1_500,
            },
        },
    })

    assert compact["estimate"]["pre_optimization_section_bytes"] == 2_000
    assert compact["estimate"]["optimized_section_bytes"] == 1_200
    assert compact["estimate"]["optimized_section_bytes_observed"] is True
