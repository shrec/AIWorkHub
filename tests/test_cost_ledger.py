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
