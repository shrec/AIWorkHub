from aiworkhub import cost_ledger


def test_token_usage_without_provider_price_is_unknown_not_free() -> None:
    rows = cost_ledger._parse_usage_stdout(
        "TASK_A | runner=deepseek | topic=code | records=1 | tokens=120 "
        "| in=100 out=20 | cost=$0.0000"
    )

    assert rows[0]["cost_known"] is False
    aggregate = cost_ledger._aggregate(rows, "runner")["deepseek"]
    assert aggregate["cost_usd"] == 0.0
    assert aggregate["cost_unknown_records"] == 1
    assert aggregate["tokens_with_unknown_cost"] == 120


def test_observed_nonzero_provider_price_is_known() -> None:
    rows = cost_ledger._parse_usage_stdout(
        "TASK_A | runner=claude | topic=code | records=1 | tokens=120 "
        "| in=100 out=20 | cost=$0.1250"
    )

    aggregate = cost_ledger._aggregate(rows, "runner")["claude"]
    assert aggregate["cost_known_records"] == 1
    assert aggregate["cost_unknown_records"] == 0


def test_repo_bound_usage_preserves_model_and_unknown_cost_truth(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        cost_ledger.task_store,
        "list_usage_events",
        lambda _root, limit=10_000: [{
            "task_id": "T1",
            "runner": "codex_runner",
            "topic": "code",
            "model": "gpt-5.5",
            "requested_model": "gpt-5.5",
            "observed_model": "gpt-5.5-codex",
            "model_observed": True,
            "provider": "openai",
            "input_tokens": 100,
            "output_tokens": 20,
            "visible_output_tokens": 15,
            "reasoning_output_tokens": 5,
            "total_tokens": 120,
            "cost_usd": 0.0,
            "created_at": "2026-08-03T01:02:03+00:00",
        }],
    )
    result = cost_ledger.build_cost_ledger(repo_root=tmp_path, include_tasks=True)
    assert result["counts"] == {"usage_rows": 1, "launch_rows": 0, "union_rows": 1}
    assert result["tasks"][0]["model"] == "gpt-5.5"
    assert result["tasks"][0]["requested_model"] == "gpt-5.5"
    assert result["tasks"][0]["observed_model"] == "gpt-5.5-codex"
    assert result["tasks"][0]["reasoning_output_tokens"] == 5
    assert result["tasks"][0]["provider"] == "openai"
    assert result["tasks"][0]["role"] == "worker"
    assert result["tasks"][0]["role_observed"] is False
    assert result["tasks"][0]["created_at"] == "2026-08-03T01:02:03+00:00"
    assert result["tasks"][0]["cost_known"] is False
    assert result["aggregates"]["by_day"]["2026-08-03"]["total_tokens"] == 120


def test_repo_bound_usage_preserves_retries_and_cache_economics(monkeypatch, tmp_path) -> None:
    events = [
        {
            "task_id": "T1",
            "runner": "codex_runner",
            "topic": "code",
            "model": "gpt-5.5",
            "provider": "codex",
            "role": "reviewer",
            "source": "task_mcp_launcher",
            "note": f"task_mcp_request:req-{attempt}",
            "input_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
            "cached_input_tokens": cached,
            "cache_creation_input_tokens": 0,
            "cache_metrics_observed": True,
            "cost_usd": 0.0,
            "cost_observed": True,
            "created_at": f"2026-08-03T01:02:0{attempt}+00:00",
        }
        for attempt, cached in ((1, 40), (2, 60))
    ]
    monkeypatch.setattr(
        cost_ledger.task_store,
        "list_usage_events",
        lambda _root, limit=10_000: events,
    )
    monkeypatch.setattr(
        cost_ledger.task_store,
        "latest_manager_decisions",
        lambda _root: {
            "T1": {
                "decision": "accepted",
                "created_at": "2026-08-03T01:02:02+00:00",
            }
        },
    )

    result = cost_ledger.build_cost_ledger(repo_root=tmp_path, include_tasks=True)

    assert result["counts"] == {"usage_rows": 2, "launch_rows": 0, "union_rows": 2}
    assert {row["attempt_id"] for row in result["tasks"]} == {"req-1", "req-2"}
    assert result["cost_quality"]["known_records"] == 2
    assert result["cache_quality"] == {
        "observed_records": 2,
        "cached_input_tokens": 100,
        "cache_creation_input_tokens": 0,
        "absent_metrics_are_unknown_not_zero": True,
    }
    provider = result["aggregates"]["by_provider"]["codex"]
    assert provider["records"] == 2
    assert provider["total_tokens"] == 240
    assert provider["cache_hit_ratio"] == 0.5
    assert result["aggregates"]["by_model"]["gpt-5.5"]["records"] == 2
    assert result["aggregates"]["by_role"]["reviewer"]["records"] == 2
    assert result["role_quality"] == {
        "explicit_records": 2,
        "legacy_inferred_records": 0,
        "legacy_inference": "quality_review_topic_is_reviewer_otherwise_worker",
    }
    assert result["retry_economics"] == {
        "schema_id": "aiworkhub.retry_economics.v1",
        "association_only": True,
        "tasks_with_usage": 1,
        "attempt_records": 2,
        "tasks_with_retries": 1,
        "retry_records": 1,
        "retry_record_rate_percent": 50.0,
        "retry_tokens": 120,
        "retry_cost_usd": 0.0,
        "retry_cost_unknown_records": 0,
        "accepted_retried_tasks": 1,
        "accepted_rate_among_retried_tasks_percent": 100.0,
        "claim_boundary": (
            "Repeated usage records are measured attempts. They do not prove "
            "that the retry caused acceptance or that its tokens were avoidable."
        ),
    }
    assert result["model_outcomes"]["models"]["gpt-5.5"] == {
        "decided_tasks": 1,
        "accepted": 1,
        "rejected": 0,
        "acceptance_rate_percent": 100.0,
        "usage_observed_tasks": 1,
        "cost_observed_tasks": 1,
        "total_tokens": 120,
        "cost_usd": 0.0,
    }


def test_cache_ratio_is_unknown_when_provider_did_not_report_cache_metrics() -> None:
    aggregate = cost_ledger._aggregate(
        [{
            "runner": "glm",
            "records": 1,
            "input_tokens": 100,
            "output_tokens": 10,
            "total_tokens": 110,
            "cached_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_metrics_observed": False,
            "cost_usd": 0.0,
            "cost_known": False,
        }],
        "runner",
    )["glm"]

    assert aggregate["cache_hit_ratio"] is None
    assert aggregate["cache_observed_records"] == 0


def test_repo_bound_unobserved_attempt_is_counted_but_not_measured(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        cost_ledger.task_store,
        "list_usage_events",
        lambda _root, limit=10_000: [{
            "task_id": "T-UNKNOWN",
            "runner": "glm_worker",
            "topic": "code",
            "model": "glm-5.2",
            "provider": "vscode_lm",
            "usage_observed": False,
            "cost_observed": False,
            "source": "task_mcp_launcher",
            "note": "task_mcp_request:req-unknown",
            "created_at": "2026-08-04T01:02:03+00:00",
        }],
    )

    result = cost_ledger.build_cost_ledger(repo_root=tmp_path, include_tasks=True)

    assert result["counts"]["usage_rows"] == 1
    [row] = result["tasks"]
    assert row["attempt_id"] == "req-unknown"
    assert row["usage_observed"] is False
    assert row["cost_known"] is False
    aggregate = result["aggregates"]["by_provider"]["vscode_lm"]
    assert aggregate["records"] == 1
    assert aggregate["usage_observed_records"] == 0
    assert aggregate["usage_unknown_records"] == 1
    assert aggregate["total_tokens"] == 0
