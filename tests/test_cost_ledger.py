from aiworkhub import cost_ledger


def test_cost_per_accepted_outcome_uses_exact_matched_population() -> None:
    usage = [
        {
            "task_id": "accepted",
            "role": "worker",
            "model": "cheap-model",
            "total_tokens": 100,
            "cost_usd": 0.25,
            "cost_known": True,
        },
        {
            "task_id": "accepted",
            "role": "worker",
            "model": "cheap-model",
            "total_tokens": 50,
            "cost_usd": 0.05,
            "cost_known": True,
        },
        {
            "task_id": "rejected",
            "role": "worker",
            "model": "cheap-model",
            "total_tokens": 200,
            "cost_usd": 0.20,
            "cost_known": True,
        },
        {
            "task_id": "accepted",
            "role": "reviewer",
            "model": "review-model",
            "total_tokens": 10,
            "cost_usd": 9.0,
            "cost_known": True,
        },
    ]
    decisions = {
        "accepted": {"decision": "accepted"},
        "rejected": {"decision": "rejected"},
    }
    cards = [
        {
            "task_id": task_id,
            "project_context": {"task_type": "code"},
        }
        for task_id in decisions
    ]

    view = cost_ledger.cost_per_accepted_outcome_view(
        usage, decisions, cards
    )
    row = view["routes"]["cheap-model"]["unknown"]["code"]["unknown"]

    assert row["state"] == "MEASURED"
    assert row["matched_decided_tasks"] == 2
    assert row["accepted_outcomes"] == 1
    assert row["attempt_records"] == 3
    assert row["total_cost_usd"] == 0.5
    assert row["cost_per_accepted_outcome_usd"] == 0.5
    assert row["cost_coverage"] == 1.0
    assert "review-model" not in view["routes"]
    assert view["automatic_routing"] is False


def test_cost_per_accepted_outcome_unknown_and_mixed_models_fail_closed() -> None:
    usage = [
        {
            "task_id": "unknown-cost",
            "role": "worker",
            "model": "model-a",
            "cost_usd": 0.0,
            "cost_known": False,
        },
        {
            "task_id": "mixed",
            "role": "worker",
            "model": "model-a",
            "cost_usd": 0.1,
            "cost_known": True,
        },
        {
            "task_id": "mixed",
            "role": "worker",
            "model": "model-b",
            "cost_usd": 0.1,
            "cost_known": True,
        },
    ]
    decisions = {
        "unknown-cost": {"decision": "accepted"},
        "mixed": {"decision": "accepted"},
    }
    cards = [
        {
            "task_id": task_id,
            "project_context": {"task_type": "research"},
        }
        for task_id in decisions
    ]

    view = cost_ledger.cost_per_accepted_outcome_view(
        usage, decisions, cards
    )
    row = view["routes"]["model-a"]["unknown"]["research"]["unknown"]

    assert row["state"] == "UNKNOWN"
    assert row["cost_per_accepted_outcome_usd"] is None
    assert view["mixed_model_tasks"] == 1


def test_cost_per_accepted_outcome_without_acceptance_is_unmeasured() -> None:
    view = cost_ledger.cost_per_accepted_outcome_view(
        [{
            "task_id": "rejected",
            "role": "worker",
            "model": "model-a",
            "cost_usd": 0.3,
            "cost_known": True,
        }],
        {"rejected": {"decision": "rejected"}},
        [{
            "task_id": "rejected",
            "project_context": {"task_type": "code"},
        }],
    )

    row = view["routes"]["model-a"]["unknown"]["code"]["unknown"]
    assert row["state"] == "UNMEASURED"
    assert row["cost_per_accepted_outcome_usd"] is None


def test_cost_per_accepted_outcome_normalizes_declared_model_aliases() -> None:
    view = cost_ledger.cost_per_accepted_outcome_view(
        [
            {
                "task_id": "accepted",
                "role": "worker",
                "observed_model": (
                    "copilot/claude-sonnet-5/claude-sonnet-5/"
                    "Claude Sonnet 5/claude-sonnet-5"
                ),
                "requested_model": "sonnet",
                "cost_usd": 1.0,
                "cost_known": True,
            },
            {
                "task_id": "accepted",
                "role": "worker",
                "model": "sonnet",
                "cost_usd": 2.0,
                "cost_known": True,
            },
        ],
        {"accepted": {"decision": "accepted"}},
        [{
            "task_id": "accepted",
            "project_context": {"task_type": "code"},
        }],
    )

    assert view["mixed_model_tasks"] == 0
    assert view["routes"]["sonnet"]["unknown"]["code"]["unknown"][
        "cost_per_accepted_outcome_usd"
    ] == 3.0


def test_cost_per_accepted_outcome_partitions_explicit_risk() -> None:
    view = cost_ledger.cost_per_accepted_outcome_view(
        [{
            "task_id": "high-risk",
            "role": "worker",
            "model": "model-a",
            "provider": "codex",
            "cost_usd": 4.0,
            "cost_known": True,
        }],
        {"high-risk": {"decision": "accepted"}},
        [{
            "task_id": "high-risk",
            "risk_tier": "high",
            "project_context": {"task_type": "code"},
        }],
    )

    row = view["routes"]["model-a"]["codex_cli"]["code"]["high"]
    assert row["state"] == "MEASURED"
    assert row["risk_evidence"] == "explicit_task_card"


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
            "cache_write_input_tokens": 7,
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
    assert result["tasks"][0]["cache_write_input_tokens"] == 7
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
            "cache_write_input_tokens": 3,
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
        "cache_write_input_tokens": 6,
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


def test_cache_ratio_excludes_rows_without_observed_cache_metrics() -> None:
    aggregate = cost_ledger._aggregate(
        [
            {
                "runner": "codex",
                "records": 1,
                "input_tokens": 1000,
                "output_tokens": 100,
                "total_tokens": 1100,
                "cached_input_tokens": 900,
                "cache_creation_input_tokens": 0,
                "cache_metrics_observed": True,
                "cost_usd": 0.0,
                "cost_known": False,
            },
            {
                "runner": "codex",
                "records": 1,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "cached_input_tokens": 500,
                "cache_creation_input_tokens": 0,
                "cache_metrics_observed": False,
                "cost_usd": 0.0,
                "cost_known": False,
            },
        ],
        "runner",
    )["codex"]

    assert aggregate["cached_input_tokens"] == 1400
    assert aggregate["cache_eligible_input_tokens"] == 1000
    assert aggregate["cache_hit_ratio"] == 0.9
    assert aggregate["cache_observed_records"] == 1


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
            "telemetry_reason": "provider_api_usage_unavailable",
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
    assert row["telemetry_reason"] == "provider_api_usage_unavailable"
    assert row["cost_known"] is False
    aggregate = result["aggregates"]["by_provider"]["vscode_lm"]
    assert aggregate["records"] == 1
    assert aggregate["usage_observed_records"] == 0
    assert aggregate["usage_unknown_records"] == 1
    assert aggregate["total_tokens"] == 0


def test_process_event_fills_unobserved_placeholder_usage(monkeypatch, tmp_path) -> None:
    ledger = tmp_path / ".aiworkhub/runtime/process_logs/process_events.jsonl"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        '{"task_id":"T1","request_id":"R1","repository_id":"repo-1","role":"worker","input_tokens":100,"output_tokens":20,"total_tokens":120,"cache_metrics_observed":true,"cached_input_tokens":40,"cost_observed":true,"cost_usd":0.25}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        cost_ledger.task_store,
        "list_usage_events",
        lambda _root, limit=10_000: [{
            "task_id": "T1",
            "request_id": "R1",
            "repository_id": "repo-1",
            "role": "reviewer",
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "usage_observed": False,
        }],
    )

    [row] = cost_ledger._canonical_usage_rows(tmp_path)

    assert row["total_tokens"] == 120
    assert row["cached_input_tokens"] == 40
    assert row["cost_usd"] == 0.25
    assert row["usage_observed"] is True
    assert row["role"] == "reviewer"


def test_process_event_preserves_explicit_zero_and_identity_containment(
    monkeypatch, tmp_path
) -> None:
    ledger = tmp_path / ".aiworkhub/runtime/process_logs/process_events.jsonl"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        "\n".join((
            '{"task_id":"T1","request_id":"R1","repository_id":"repo-2","total_tokens":120}',
            '{"task_id":"T2","request_id":"R1","repository_id":"repo-1","total_tokens":120}',
            '{"task_id":"T1","request_id":"R2","repository_id":"repo-1","total_tokens":120}',
            '{"task_id":"T1","request_id":"R1","repository_id":"repo-1","total_tokens":120}',
            '{malformed terminal record',
        )),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        cost_ledger.task_store,
        "list_usage_events",
        lambda _root, limit=10_000: [
            {
                "task_id": "T1",
                "request_id": "R1",
                "repository_id": "repo-1",
                "usage_observed": True,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
            },
            {
                "task_id": "T1",
                "request_id": "R3",
                "repository_id": "repo-1",
                "usage_observed": False,
                "total_tokens": 0,
            },
        ],
    )

    rows = cost_ledger._canonical_usage_rows(tmp_path)

    assert rows[0]["total_tokens"] == 0
    assert rows[0]["usage_observed"] is True
    assert rows[1]["total_tokens"] == 0
    assert rows[1]["usage_observed"] is False


def test_process_event_index_retains_telemetry_across_later_lifecycle_event(
    monkeypatch, tmp_path
) -> None:
    ledger = tmp_path / ".aiworkhub/runtime/process_logs/process_events.jsonl"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        "\n".join((
            '{"task_id":"T1","request_id":"R1","repository_id":"repo-1","input_tokens":100,"output_tokens":20,"total_tokens":120,"usage_observed":true}',
            '{"task_id":"T1","request_id":"R1","repository_id":"repo-1","status":"completed","finished_at":"2026-09-01T01:02:03+00:00"}',
        )),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        cost_ledger.task_store,
        "list_usage_events",
        lambda _root, limit=10_000: [{
            "task_id": "T1",
            "request_id": "R1",
            "repository_id": "repo-1",
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "usage_observed": False,
        }],
    )

    [row] = cost_ledger._canonical_usage_rows(tmp_path)
    [event] = cost_ledger._process_event_index(tmp_path).values()

    assert row["total_tokens"] == 120
    assert row["input_tokens"] == 100
    assert row["usage_observed"] is True
    assert event["status"] == "completed"
    assert event["finished_at"] == "2026-09-01T01:02:03+00:00"
