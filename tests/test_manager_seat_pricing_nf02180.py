"""NF-2026-02180: manager_seat pricing for claude_code_sessions.

Prices stored Claude Code session usage per model/token-class with a
declared manager-seat rate table, matching a normalized model id (date and
bracket-suffix stripped) by family prefix, never by exact string equality.
An unpriceable model never gets a guessed price, and the manager_seat total
is never silently merged into worker/reviewer totals.
"""

from __future__ import annotations

import pytest

from aiworkhub import cost_ledger


def _session(
    *,
    session_id: str = "s1",
    input_tokens: int = 100,
    output_tokens: int = 20,
    cache_read_input_tokens: int = 10,
    cache_creation_input_tokens: int = 5,
    models: list[str] | None = None,
    last_at: str = "2026-11-01T00:00:00Z",
) -> dict:
    return {
        "session_id": session_id,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": cache_read_input_tokens,
        "cache_creation_input_tokens": cache_creation_input_tokens,
        "models": models if models is not None else ["claude-sonnet-5"],
        "last_at": last_at,
    }


def test_priced_session_matches_exact_rate_computation() -> None:
    row = _session()
    section = {
        "status": "measured",
        "sessions": [row],
        "totals": {},
    }
    priced = cost_ledger._price_claude_code_sessions(section)
    out_row = priced["sessions"][0]

    rates = cost_ledger._CLAUDE_CODE_MODEL_RATES_USD_PER_MTOK["claude-sonnet-5"]
    expected = (
        100 * rates["input"]
        + 20 * rates["output"]
        + 10 * rates["cache_read"]
        + 5 * rates["cache_write"]
    ) / 1_000_000.0

    assert out_row["usd"] == round(expected, 6)
    assert priced["pricing"]["role"] == "manager_seat"
    assert priced["pricing"]["total_usd"] == round(expected, 6)
    assert priced["pricing"]["partial_coverage"] is False


def test_unknown_model_yields_unknown_usd_and_partial_coverage() -> None:
    known = _session(session_id="known", models=["claude-sonnet-5"])
    unknown = _session(session_id="unknown", models=["some-future-model"])
    section = {"status": "measured", "sessions": [known, unknown], "totals": {}}

    priced = cost_ledger._price_claude_code_sessions(section)
    by_id = {row["session_id"]: row for row in priced["sessions"]}

    assert by_id["unknown"]["usd"] == "UNKNOWN"
    assert isinstance(by_id["known"]["usd"], float)
    assert priced["pricing"]["partial_coverage"] is True
    assert priced["pricing"]["known_sessions"] == 1
    assert priced["pricing"]["unknown_sessions"] == 1


def test_real_shaped_model_ids_are_normalized_and_priced() -> None:
    for model_id in (
        "claude-opus-5-5",
        "claude-sonnet-5-5-20261001",
        "claude-sonnet-5[1m]",
    ):
        row = _session(session_id=model_id, models=[model_id])
        section = {"status": "measured", "sessions": [row], "totals": {}}
        priced = cost_ledger._price_claude_code_sessions(section)
        usd = priced["sessions"][0]["usd"]
        assert isinstance(usd, float), model_id
        assert usd > 0.0, model_id


def _patch_ledger(monkeypatch, sessions: list[dict]) -> None:
    def fake_memoized(repo_root, *, runner, topic, status, include_tasks):
        return {
            "aggregates": {
                "by_role": {
                    "worker": {"cost_usd": 2.0},
                    "reviewer": {"cost_usd": 1.0},
                    "manager": {"cost_usd": 99.0},
                }
            },
        }

    def fake_collect(repo_root):
        return {"status": "measured", "sessions": sessions, "totals": {}}

    monkeypatch.setattr(cost_ledger, "_memoized_cost_ledger", fake_memoized)
    monkeypatch.setattr(
        cost_ledger.claude_code_usage, "collect_claude_code_usage", fake_collect
    )


def test_manager_seat_total_is_labelled_separately_from_worker_reviewer(
    monkeypatch, tmp_path
) -> None:
    _patch_ledger(monkeypatch, [])
    without_sessions = cost_ledger.build_cost_ledger(repo_root=tmp_path)

    _patch_ledger(monkeypatch, [_session()])
    result = cost_ledger.build_cost_ledger(repo_root=tmp_path)

    # Existing ledger aggregates are unchanged by manager-seat sessions.
    assert result["aggregates"] == without_sessions["aggregates"]
    assert without_sessions["cost_totals"]["manager_seat_usd"] == 0.0
    assert without_sessions["cost_totals"]["worker_reviewer_usd"] == 3.0
    assert result["cost_totals"]["worker_reviewer_usd"] == 3.0
    assert result["cost_totals"]["manager_seat_usd"] == (
        result["claude_code_sessions"]["pricing"]["total_usd"]
    )
    assert result["cost_totals"]["manager_seat_usd"] != 0.0
    assert result["cost_totals"]["grand_total_usd"] == round(
        3.0 + result["cost_totals"]["manager_seat_usd"], 6
    )
    assert result["cost_totals"]["manager_seat_separately_labelled"] is True
    assert result["cost_totals"]["priced_complete"] is True
    assert result["cost_totals"]["unknown_sessions"] == 0


def test_grand_total_carries_partial_coverage_flag_when_unknown(
    monkeypatch, tmp_path
) -> None:
    _patch_ledger(
        monkeypatch,
        [
            _session(session_id="known", models=["claude-sonnet-5"]),
            _session(session_id="unknown", models=["some-future-model"]),
        ],
    )
    result = cost_ledger.build_cost_ledger(repo_root=tmp_path)

    assert result["claude_code_sessions"]["pricing"]["partial_coverage"] is True
    assert result["cost_totals"]["priced_complete"] is False
    assert result["cost_totals"]["unknown_sessions"] > 0


@pytest.mark.parametrize(
    "model_id", ["claude-haiku-3", "claude-sonnet-6", "claude-opus-4-1"]
)
def test_unlisted_generations_price_as_unknown(model_id: str) -> None:
    section = {
        "status": "measured",
        "sessions": [_session(session_id=model_id, models=[model_id])],
        "totals": {},
    }
    priced = cost_ledger._price_claude_code_sessions(section)

    assert priced["sessions"][0]["usd"] == "UNKNOWN"
    assert priced["pricing"]["partial_coverage"] is True


def test_top_sessions_and_per_day_series_present() -> None:
    rows = [
        _session(session_id="a", input_tokens=1000, last_at="2026-11-01T00:00:00Z"),
        _session(session_id="b", input_tokens=10, last_at="2026-11-02T00:00:00Z"),
    ]
    section = {"status": "measured", "sessions": rows, "totals": {}}

    priced = cost_ledger._price_claude_code_sessions(section)
    pricing = priced["pricing"]

    assert pricing["top_sessions_by_usd"][0]["session_id"] == "a"
    days = {entry["day"] for entry in pricing["per_day_usd"]}
    assert days == {"2026-11-01", "2026-11-02"}


def test_per_day_series_includes_days_with_only_unpriced_sessions() -> None:
    rows = [
        _session(session_id="a", models=["claude-sonnet-5"], last_at="2026-11-01T00:00:00Z"),
        _session(session_id="b", models=["some-future-model"], last_at="2026-11-02T00:00:00Z"),
    ]
    section = {"status": "measured", "sessions": rows, "totals": {}}

    priced = cost_ledger._price_claude_code_sessions(section)
    by_day = {entry["day"]: entry for entry in priced["pricing"]["per_day_usd"]}

    assert "2026-11-02" in by_day
    assert by_day["2026-11-02"]["usd"] is None
    assert by_day["2026-11-02"]["unknown_sessions"] == 1
