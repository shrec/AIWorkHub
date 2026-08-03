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
            "provider": "openai",
            "input_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
            "cost_usd": 0.0,
            "created_at": "2026-08-03T01:02:03+00:00",
        }],
    )
    result = cost_ledger.build_cost_ledger(repo_root=tmp_path, include_tasks=True)
    assert result["counts"] == {"usage_rows": 1, "launch_rows": 0, "union_rows": 1}
    assert result["tasks"][0]["model"] == "gpt-5.5"
    assert result["tasks"][0]["provider"] == "openai"
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
