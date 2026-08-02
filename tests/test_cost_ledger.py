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
