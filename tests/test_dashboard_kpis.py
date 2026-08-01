from aiworkhub.dashboard_kpis import build_kpi_snapshot


def _run(task_id, state, timestamp, adapter="deepseek", calls=0, live_calls=0):
    return {
        "task_id": task_id,
        "state": state,
        "finished_at": timestamp,
        "adapter_id": adapter,
        "ai_infra_context": {
            "tool_use": {
                "source_graph_calls": calls,
                "source_graph_live_calls": live_calls,
                "policy_violations": 0,
            }
        },
    }


def _build(processes, *, total_requests=None):
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


def test_kpis_calculate_callback_tool_and_context_denominators():
    result = _build([_run("A", "review_ready", "2026-08-01T12:00:00Z")])

    assert result["headline"]["callback_delivery_rate"] == 90.0
    assert result["headline"]["source_graph_useful_call_rate"] == 80.0
    assert result["headline"]["source_graph_mode_attribution_rate"] == 80.0
    assert result["context"][0]["execution_rate"] == 75.0
    assert result["context"][2]["execution_rate"] is None


def test_kpis_order_and_bound_daily_buckets_and_report_truncation():
    processes = [
        _run(f"T{day}", "review_ready", f"2026-07-{day:02d}T12:00:00Z")
        for day in range(1, 21)
    ]
    result = _build(processes, total_requests=70)

    assert len(result["daily"]) == 14
    assert result["daily"][0]["date"] == "2026-07-07"
    assert result["daily"][-1]["date"] == "2026-07-20"
    assert result["window"]["truncated"] is True
    assert result["data_quality"]["process_window_truncated"] is True


def test_kpis_handle_empty_and_invalid_inputs_without_false_percentages():
    result = _build(
        [_run("A", "processing", "not-a-timestamp")],
    )

    assert result["headline"]["review_ready_rate"] is None
    assert result["daily"] == []
    assert result["data_quality"]["invalid_timestamp_rows"] == 1
