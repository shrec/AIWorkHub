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


def test_cost_ledger_mcp_binds_the_repository_it_reports_on(monkeypatch):
    """The tool must hand ``build_cost_ledger`` a root, not the global fallback.

    Without ``repo_root`` the builder parses the text usage report, whose lines
    carry no model, provider or day, so every aggregate the docstring promises
    reads ``unknown`` while looking authoritative.  The stub above answers the
    same either way, which is exactly why nothing caught it (NF-2026-00542).
    """

    seen: dict[str, object] = {}

    def _capture(**kwargs):
        seen.update(kwargs)
        return _ledger(**kwargs)

    monkeypatch.setattr(server.cost_ledger, "build_cost_ledger", _capture)
    monkeypatch.setattr(server.core, "repo_root", lambda: "/repo/under/test")

    server.aiworkhub_task_cost_ledger()

    assert seen["repo_root"] == "/repo/under/test"


def _grown_ledger_factory(
    models: int = 24, days: int = 28, route_models: int = 16
):
    """History-shaped ledger: more models/days/routes than the bound keeps."""

    def _grown(**_kwargs):
        by_model = {
            f"model-{index:02d}": {"total_tokens": index}
            for index in range(models)
        }
        # Ties model-22 at magnitude 22 so the key tie-break is exercised.
        by_model["model-tie"] = {"total_tokens": 22}
        by_day = {
            f"2026-{1 + index // 28:02d}-{1 + index % 28:02d}": {
                "total_tokens": index
            }
            for index in range(days)
        }
        routes = {
            f"route-model-{index:02d}": {
                "route-a": {
                    "family-x": {
                        "unknown": {
                            "total_tokens": 5,
                            "state": "UNKNOWN",
                        },
                        "measured": {
                            "total_tokens": (index + 1) * 100,
                            "state": "MEASURED",
                        },
                    },
                },
            }
            for index in range(route_models)
        }
        return {
            "tool": "aiworkhub_task_cost_ledger",
            "counts": {"union_rows": 99},
            "cost_quality": {"known_records": 90},
            "cache_quality": {"observed_records": 9},
            "aggregates": {
                "by_topic": {"topic-a": {"total_tokens": 10}},
                "by_runner": {"runner-a": {"total_tokens": 10}},
                "by_model": by_model,
                "by_provider": {"provider-a": {"total_tokens": 10}},
                "by_role": {
                    "worker": {"total_tokens": 6},
                    "reviewer": {"total_tokens": 4},
                },
                "by_day": by_day,
            },
            "cost_per_accepted_outcome": {
                "schema_id": "aiworkhub.cost_per_accepted_outcome.v1",
                "mode": "advisory_only",
                "routes": routes,
                "unmatched_decisions": 3,
                "unknown_model_tasks": 2,
                "mixed_model_tasks": 1,
            },
            "tasks": (
                [{"task_id": "TASK_A"}] if _kwargs.get("include_tasks") else []
            ),
        }

    return _grown


_grown_ledger = _grown_ledger_factory()


def test_cost_ledger_mcp_summary_bounds_rows_deterministically(monkeypatch):
    monkeypatch.setattr(server.cost_ledger, "build_cost_ledger", _grown_ledger)

    summary = server.aiworkhub_task_cost_ledger()
    again = server.aiworkhub_task_cost_ledger()

    assert summary == again
    assert summary["summary_row_bounds"] == {
        "by_model": {
            "total_count": 25,
            "returned_count": 10,
            "truncated": True,
            "ranked_by": "observed_cost_usd_then_unpriced_activity",
        },
        "by_day": {
            "total_count": 28,
            "returned_count": 10,
            "truncated": True,
            "ranked_by": "most_recent_day_then_undated",
        },
        "cost_per_accepted_outcome_routes": {
            "total_count": 16,
            "returned_count": 10,
            "truncated": True,
            "ranked_by": "summed_matched_attempt_token_magnitude",
        },
    }
    # Top-N by descending token magnitude, key ascending on ties: model-tie
    # shares magnitude 22 with model-22 and both survive the cut.
    assert list(summary["aggregates"]["by_model"]) == [
        "model-23",
        "model-22",
        "model-tie",
        "model-21",
        "model-20",
        "model-19",
        "model-18",
        "model-17",
        "model-16",
        "model-15",
    ]
    # Days are most-recent-first.
    assert list(summary["aggregates"]["by_day"]) == [
        f"2026-01-{day:02d}" for day in range(28, 18, -1)
    ]
    # Route models rank by summed matched magnitude; route-model-15 is the
    # largest and survives, its UNKNOWN row intact.
    assert set(summary["cost_per_accepted_outcome"]["routes"]) == {
        f"route-model-{index:02d}" for index in range(6, 16)
    }
    kept = summary["cost_per_accepted_outcome"]["routes"]["route-model-15"]
    assert kept["route-a"]["family-x"]["unknown"] == {
        "total_tokens": 5,
        "state": "UNKNOWN",
    }
    # Aggregate totals, cost coverage truth and summary-client keys survive.
    assert summary["counts"] == {"union_rows": 99}
    assert summary["cost_quality"] == {"known_records": 90}
    assert summary["cache_quality"] == {"observed_records": 9}
    assert summary["aggregates"]["by_provider"] == {
        "provider-a": {"total_tokens": 10}
    }
    assert summary["omitted_dimensions"] == ["by_role", "by_runner", "by_topic"]
    assert summary["detail_request"] == {"full": True}


def test_cost_ledger_mcp_default_summary_bytes_stay_bounded(monkeypatch):
    import json

    monkeypatch.setattr(
        server.cost_ledger,
        "build_cost_ledger",
        _grown_ledger_factory(models=96, days=112, route_models=64),
    )

    summary_size = len(json.dumps(server.aiworkhub_task_cost_ledger()))
    full_size = len(json.dumps(server.aiworkhub_task_cost_ledger(full=True)))

    assert summary_size < 12_000 < full_size

    # Doubling every growth dimension pins returned rows at the top-N, so
    # the serialized default cannot grow with history.
    monkeypatch.setattr(
        server.cost_ledger,
        "build_cost_ledger",
        _grown_ledger_factory(models=192, days=224, route_models=128),
    )
    doubled = server.aiworkhub_task_cost_ledger()
    bounds = doubled["summary_row_bounds"]
    assert bounds["by_model"]["returned_count"] == 10
    assert bounds["by_day"]["returned_count"] == 10
    assert bounds["cost_per_accepted_outcome_routes"]["returned_count"] == 10
    assert len(json.dumps(doubled)) < 12_000


def test_cost_ledger_mcp_full_returns_every_row_unchanged(monkeypatch):
    monkeypatch.setattr(server.cost_ledger, "build_cost_ledger", _grown_ledger)

    full = server.aiworkhub_task_cost_ledger(full=True)

    assert full["snapshot_mode"] == "full"
    assert full["full_snapshot_available"] is True
    assert set(full["aggregates"]) == {
        "by_topic",
        "by_runner",
        "by_model",
        "by_provider",
        "by_role",
        "by_day",
    }
    assert len(full["aggregates"]["by_model"]) == 25
    assert len(full["aggregates"]["by_day"]) == 28
    assert len(full["cost_per_accepted_outcome"]["routes"]) == 16
    assert "summary_row_bounds" not in full
    assert full["counts"] == {"union_rows": 99}


def test_cost_ledger_mcp_forwards_filters_before_bounding(monkeypatch):
    seen: dict[str, object] = {}

    def _capture(**kwargs):
        seen.update(kwargs)
        return _grown_ledger(**kwargs)

    monkeypatch.setattr(server.cost_ledger, "build_cost_ledger", _capture)

    summary = server.aiworkhub_task_cost_ledger(
        runner="runner-a", topic="topic-a", status="accepted"
    )

    assert seen["runner"] == "runner-a"
    assert seen["topic"] == "topic-a"
    assert seen["status"] == "accepted"
    assert "repo_root" in seen
    assert summary["summary_row_bounds"]["by_model"]["truncated"] is True


def test_cost_ledger_mcp_registers_public_tool_only():
    # Regression: only the public tool may carry the @mcp.tool() decorator; the
    # private row-magnitude helper must stay an unregistered plain function.
    registered = set(server.mcp._tool_manager._tools)

    assert "aiworkhub_task_cost_ledger" in registered
    assert "_ledger_row_magnitude" not in registered


def test_cost_ledger_change_preserves_task_bound_sdlc_endpoints(monkeypatch):
    """The ledger bound must leave its module neighbours alone.

    An earlier candidate for this card was cut against a stale baseline, so its
    diff silently deleted the two task-bound SDLC endpoints that live a few
    hundred lines below ``aiworkhub_task_cost_ledger`` in the same module.
    Every cost-ledger test stayed green, because none of them looked at a tool
    other than their own.  Presence alone is a weak guard, so each endpoint is
    also called: it must still delegate to its exact ``core`` function by
    keyword, which a stub that merely re-declared the name would not do.
    """

    registered = set(server.mcp._tool_manager._tools)

    assert "aiworkhub_manager_sdlc_case_create_for_task" in registered
    assert "aiworkhub_manager_sdlc_case_for_task" in registered

    seen: list[dict[str, object]] = []

    def _create(**kwargs):
        seen.append({"core_fn": "sdlc_case_create_for_task", **kwargs})
        return {"ok": True, "case_id": "CASE_A"}

    def _read(**kwargs):
        seen.append({"core_fn": "sdlc_case_for_task", **kwargs})
        return {"ok": True, "case_id": "CASE_A"}

    monkeypatch.setattr(server.core, "sdlc_case_create_for_task", _create)
    monkeypatch.setattr(server.core, "sdlc_case_for_task", _read)

    created = server.aiworkhub_manager_sdlc_case_create_for_task(
        task_id="TASK_A", request_id="REQ_A"
    )
    read = server.aiworkhub_manager_sdlc_case_for_task(task_id="TASK_A")

    assert seen == [
        {
            "core_fn": "sdlc_case_create_for_task",
            "task_id": "TASK_A",
            "request_id": "REQ_A",
        },
        {"core_fn": "sdlc_case_for_task", "task_id": "TASK_A"},
    ]
    assert created == {"ok": True, "case_id": "CASE_A"}
    assert read == {"ok": True, "case_id": "CASE_A"}


def _cost_ranked_ledger(**_kwargs):
    """Models whose dollar rank and token rank disagree, plus unpriced ones."""

    by_model = {
        f"cheap-bulk-{index:02d}": {
            "records": 10,
            "total_tokens": 900_000 + index,
            "cost_usd": 0.05,
            "cost_known_records": 10,
            "cost_unknown_records": 0,
            "tokens_with_unknown_cost": 0,
        }
        for index in range(12)
    }
    by_model["expensive-terse"] = {
        "records": 2,
        "total_tokens": 1_200,
        "cost_usd": 411.5,
        "cost_known_records": 2,
        "cost_unknown_records": 0,
        "tokens_with_unknown_cost": 0,
    }
    # No provider price was ever reported for these two.  cost_usd 0.0 is
    # absence, not evidence of free, so they rank on activity instead.
    by_model["unpriced-heavy"] = {
        "records": 7,
        "total_tokens": 5_000_000,
        "cost_usd": 0.0,
        "cost_known_records": 0,
        "cost_unknown_records": 7,
        "tokens_with_unknown_cost": 5_000_000,
    }
    by_model["unpriced-light"] = {
        "records": 1,
        "total_tokens": 10,
        "cost_usd": 0.0,
        "cost_known_records": 0,
        "cost_unknown_records": 1,
        "tokens_with_unknown_cost": 10,
    }
    return {
        "tool": "aiworkhub_task_cost_ledger",
        "counts": {"union_rows": 99},
        "cost_quality": {
            "known_records": 92,
            "unknown_records": 8,
            "zero_cost_is_free": False,
            "reason": "provider_cost_absence_is_unknown_not_zero",
        },
        "aggregates": {
            "by_model": by_model,
            "by_provider": {"provider-a": {"records": 99}},
            "by_day": {"2026-08-04": {"records": 99}},
        },
        "tasks": [],
    }


def test_cost_ledger_mcp_summary_ranks_models_by_observed_cost(monkeypatch):
    """The expensive low-token model must outrank cheap bulk ones.

    Ranking on tokens alone kept twelve 900k-token models worth $0.05 each
    and cut the $411.50 model that spent 1,200 tokens -- the inversion of
    the one question a cost summary is read for.  A model whose provider
    reported no price is not $0 either: it ranks on its activity rather
    than being cut as though it were free.
    """

    monkeypatch.setattr(
        server.cost_ledger, "build_cost_ledger", _cost_ranked_ledger
    )

    summary = server.aiworkhub_task_cost_ledger()
    again = server.aiworkhub_task_cost_ledger()
    kept = summary["aggregates"]["by_model"]

    assert summary == again
    # Dollars order the priced models, activity orders the unpriced ones,
    # and the bound is filled from both so neither starves the other.
    assert list(kept) == [
        "expensive-terse",
        "unpriced-heavy",
        "cheap-bulk-00",
        "unpriced-light",
        "cheap-bulk-01",
        "cheap-bulk-02",
        "cheap-bulk-03",
        "cheap-bulk-04",
        "cheap-bulk-05",
        "cheap-bulk-06",
    ]
    # The token-ranked leader is exactly what the bound now cuts.
    assert "cheap-bulk-11" not in kept
    # Unknown-cost accounting travels with the kept row, unchanged.
    assert kept["unpriced-heavy"]["tokens_with_unknown_cost"] == 5_000_000
    assert kept["unpriced-heavy"]["cost_known_records"] == 0
    assert summary["summary_row_bounds"]["by_model"] == {
        "total_count": 15,
        "returned_count": 10,
        "truncated": True,
        "ranked_by": "observed_cost_usd_then_unpriced_activity",
    }
    # Totals and cost-coverage truth are untouched by the bound.
    assert summary["counts"] == {"union_rows": 99}
    assert summary["cost_quality"]["unknown_records"] == 8
    assert summary["cost_quality"]["zero_cost_is_free"] is False


def test_cost_ledger_mcp_summary_keeps_recent_days_beside_undated(monkeypatch):
    """``unknown`` is not a day and must not evict one.

    ``_aggregate`` files every timestamp-less record under ``unknown``, and
    a reverse-lexical order ranks that string above every ISO date -- so
    the bound spent a slot on a bucket that is not a day at all and dropped
    a real recent one.
    """

    def _dated(**_kwargs):
        by_day = {
            f"2026-03-{1 + index:02d}": {"total_tokens": 100 + index}
            for index in range(14)
        }
        by_day["unknown"] = {"total_tokens": 999_999}
        return {
            "tool": "aiworkhub_task_cost_ledger",
            "counts": {"union_rows": 15},
            "aggregates": {
                "by_model": {"model-a": {"total_tokens": 10}},
                "by_provider": {"provider-a": {"total_tokens": 10}},
                "by_day": by_day,
            },
            "tasks": [],
        }

    monkeypatch.setattr(server.cost_ledger, "build_cost_ledger", _dated)

    summary = server.aiworkhub_task_cost_ledger()
    days = summary["aggregates"]["by_day"]

    # The ten most recent real days survive -- including 2026-03-05, the
    # one a reverse-lexical order handed to ``unknown``.
    assert list(days)[:10] == [
        f"2026-03-{day:02d}" for day in range(14, 4, -1)
    ]
    # Undated spend is retained beside them, never silently dropped.
    assert days["unknown"] == {"total_tokens": 999_999}
    assert summary["summary_row_bounds"]["by_day"] == {
        "total_count": 15,
        "returned_count": 11,
        "truncated": True,
        "ranked_by": "most_recent_day_then_undated",
    }


def _outcome_ledger(**_kwargs):
    """A ledger whose ``model_outcomes.models`` grew with the catalog."""

    models = {
        f"outcome-model-{index:02d}": {
            "decided_tasks": 4,
            "accepted": 3,
            "rejected": 1,
            "acceptance_rate_percent": 75.0,
            "usage_observed_tasks": 4,
            "cost_observed_tasks": 4 if index % 2 else 0,
            "total_tokens": 1_000 * (index + 1),
            "cost_usd": 0.5 * (index + 1) if index % 2 else 0.0,
        }
        for index in range(24)
    }
    return {
        "tool": "aiworkhub_task_cost_ledger",
        "counts": {"union_rows": 96},
        "aggregates": {
            "by_model": {"model-a": {"records": 96}},
            "by_provider": {"provider-a": {"records": 96}},
            "by_day": {"2026-08-04": {"records": 96}},
        },
        "model_outcomes": {
            "schema_id": "aiworkhub.model_outcome_matrix.v1",
            "association_only": True,
            "attribution": (
                "latest_usage_attempt_at_or_before_latest_manager_decision"
            ),
            "models": models,
            "unmatched_decisions": 11,
        },
        "tasks": [],
    }


def test_cost_ledger_mcp_summary_bounds_model_outcome_models(monkeypatch):
    """``model_outcomes.models`` grows with history and must be bounded too.

    ``by_model`` was bounded while this second model-keyed map was passed
    through whole, so the default response still grew with every model the
    repository ever ran.  Only the row map is cut: the totals beside it
    describe the whole population and stay exact.
    """

    monkeypatch.setattr(
        server.cost_ledger, "build_cost_ledger", _outcome_ledger
    )

    summary = server.aiworkhub_task_cost_ledger()
    full = server.aiworkhub_task_cost_ledger(full=True)
    outcomes = summary["model_outcomes"]

    assert len(outcomes["models"]) == 10
    assert summary["summary_row_bounds"]["model_outcomes_models"] == {
        "total_count": 24,
        "returned_count": 10,
        "truncated": True,
        "ranked_by": "observed_cost_usd_then_unpriced_activity",
    }
    # The priciest priced model and the heaviest unpriced one both survive.
    assert "outcome-model-23" in outcomes["models"]
    assert "outcome-model-22" in outcomes["models"]
    assert outcomes["models"]["outcome-model-22"]["cost_observed_tasks"] == 0
    # Population totals beside the row map are not bounded truths.
    assert outcomes["unmatched_decisions"] == 11
    assert outcomes["schema_id"] == "aiworkhub.model_outcome_matrix.v1"
    assert outcomes["association_only"] is True
    assert outcomes["attribution"] == (
        "latest_usage_attempt_at_or_before_latest_manager_decision"
    )
    # full=true is unchanged: every row, and no bound receipt.
    assert len(full["model_outcomes"]["models"]) == 24
    assert "summary_row_bounds" not in full


def _production_aggregate_row(*, priced: bool) -> dict[str, object]:
    """One bucket in the exact shape ``cost_ledger._aggregate`` emits."""

    return {
        "records": 10,
        "input_tokens": 1000,
        "output_tokens": 1000,
        "visible_output_tokens": 900,
        "reasoning_output_tokens": 100,
        "total_tokens": 2000,
        "cached_input_tokens": 500,
        "cache_creation_input_tokens": 100,
        "cache_write_input_tokens": 100,
        "usage_observed_records": 10,
        "usage_unknown_records": 0,
        "cache_observed_records": 10,
        "cache_eligible_input_tokens": 1000,
        "cost_usd": 0.25 if priced else 0.0,
        "cost_known_records": 10 if priced else 0,
        "cost_unknown_records": 0 if priced else 10,
        "tokens_with_unknown_cost": 0 if priced else 2000,
        "cache_hit_ratio": 0.5,
    }


def _production_route_row(*, measured: bool) -> dict[str, object]:
    """One leaf in the shape ``cost_per_accepted_outcome_view`` emits."""

    return {
        "matched_decided_tasks": 4,
        "accepted_outcomes": 3,
        "rejected_outcomes": 1,
        "attempt_records": 5,
        "cost_known_attempts": 5 if measured else 0,
        "cost_unknown_attempts": 0 if measured else 5,
        "total_cost_usd": 1.5 if measured else 0.0,
        "total_tokens": 9000,
        "state": "MEASURED" if measured else "UNKNOWN",
        "reason": (
            "complete_matched_cost_and_acceptance_population"
            if measured
            else "one_or_more_matched_attempt_costs_unknown"
        ),
        "cost_coverage": 1.0 if measured else 0.0,
        "acceptance_rate": 0.75,
        "cost_per_accepted_outcome_usd": 0.5 if measured else None,
        "risk_partition": "high",
        "risk_evidence": "explicit_task_card",
    }


def _production_ledger_factory(*, models, days, routes, outcome_models):
    """The full production ledger shape, parameterised by history size.

    Every row value is held constant so that comparing two histories
    measures how many rows the default returns, never how wide one row
    happened to render.
    """

    def _build(**kwargs):
        by_day = {
            f"2026-{1 + index // 28:02d}-{1 + index % 28:02d}": (
                _production_aggregate_row(priced=True)
            )
            for index in range(days)
        }
        by_day["unknown"] = _production_aggregate_row(priced=False)
        return {
            "tool": "aiworkhub_task_cost_ledger",
            "contract": "B288_v1_readonly_cost_ledger",
            "readonly": True,
            "filters": {"runner": None, "topic": None, "status": None},
            "counts": {
                "usage_rows": 400,
                "launch_rows": 0,
                "union_rows": 400,
            },
            "cost_quality": {
                "known_records": 300,
                "unknown_records": 100,
                "tokens_with_unknown_cost": 200_000,
                "zero_cost_is_free": False,
                "reason": "provider_cost_absence_is_unknown_not_zero",
            },
            "cache_quality": {
                "observed_records": 280,
                "cached_input_tokens": 90_000,
                "cache_creation_input_tokens": 1000,
                "cache_write_input_tokens": 1000,
                "absent_metrics_are_unknown_not_zero": True,
            },
            "role_quality": {
                "explicit_records": 400,
                "legacy_inferred_records": 0,
                "legacy_inference": (
                    "quality_review_topic_is_reviewer_otherwise_worker"
                ),
            },
            "topic_quality": {
                "records_by_source": {"usage_event": 400},
                "joined_topic_changes_attribution_not_tokens": True,
            },
            "aggregates": {
                "by_topic": {
                    "topic-a": _production_aggregate_row(priced=True)
                },
                "by_runner": {
                    "runner-a": _production_aggregate_row(priced=True)
                },
                "by_model": {
                    f"model-{index:03d}": _production_aggregate_row(
                        priced=index % 3 != 0
                    )
                    for index in range(models)
                },
                "by_provider": {
                    f"provider-{index}": _production_aggregate_row(
                        priced=True
                    )
                    for index in range(6)
                },
                "by_role": {
                    "worker": _production_aggregate_row(priced=True),
                    "reviewer": _production_aggregate_row(priced=True),
                },
                "by_day": by_day,
            },
            "model_outcomes": {
                "schema_id": "aiworkhub.model_outcome_matrix.v1",
                "association_only": True,
                "attribution": (
                    "latest_usage_attempt_at_or_before_"
                    "latest_manager_decision"
                ),
                "models": {
                    f"outcome-model-{index:03d}": {
                        "decided_tasks": 6,
                        "accepted": 4,
                        "rejected": 2,
                        "acceptance_rate_percent": 66.7,
                        "usage_observed_tasks": 6,
                        "cost_observed_tasks": 6 if index % 3 else 0,
                        "total_tokens": 12_000,
                        "cost_usd": 3.5 if index % 3 else 0.0,
                    }
                    for index in range(outcome_models)
                },
                "unmatched_decisions": 7,
            },
            "cost_per_accepted_outcome": {
                "schema_id": "aiworkhub.cost_per_accepted_outcome.v1",
                "mode": "advisory_only",
                "automatic_routing": False,
                "attribution": (
                    "single_worker_model_per_task_all_attempt_costs"
                ),
                "routes": {
                    f"route-model-{index:03d}": {
                        "route-a": {
                            "code": {
                                "high": _production_route_row(measured=True),
                                "unknown": _production_route_row(
                                    measured=False
                                ),
                            },
                        },
                    }
                    for index in range(routes)
                },
                "unmatched_decisions": 5,
                "unknown_model_tasks": 3,
                "mixed_model_tasks": 2,
                "claim_boundary": "measured rows are associations only",
            },
            "retry_economics": {
                "schema_id": "aiworkhub.retry_economics.v1",
                "tasks_with_retries": 12,
                "retry_records": 30,
                "retry_tokens": 60_000,
                "retry_cost_usd": 4.5,
                "retry_cost_unknown_records": 3,
            },
            "tasks": (
                [{"task_id": "TASK_A"}] if kwargs.get("include_tasks") else []
            ),
            "authority_flags": {"runtime_authority": False},
            "source_status": {"usage_report_ok": True, "launch_log_ok": True},
        }

    return _build


def test_cost_ledger_mcp_production_shaped_default_stays_byte_bounded(
    monkeypatch,
):
    """The real response shape, not a thin fixture, must stay bounded.

    The earlier byte test used rows of one field and omitted
    ``model_outcomes`` entirely, so it passed while the shipped default
    still grew with the model catalog.  This drives every dimension
    ``build_cost_ledger`` really returns.
    """

    import json

    small = _production_ledger_factory(
        models=32, days=40, routes=24, outcome_models=32
    )
    large = _production_ledger_factory(
        models=128, days=160, routes=96, outcome_models=128
    )

    monkeypatch.setattr(server.cost_ledger, "build_cost_ledger", small)
    small_summary = server.aiworkhub_task_cost_ledger()
    small_bytes = len(json.dumps(small_summary))

    monkeypatch.setattr(server.cost_ledger, "build_cost_ledger", large)
    large_summary = server.aiworkhub_task_cost_ledger()
    large_bytes = len(json.dumps(large_summary))
    large_full_bytes = len(json.dumps(server.aiworkhub_task_cost_ledger(
        full=True
    )))

    assert large_bytes < 40_000
    # Quadrupling every history dimension changes the default response only
    # by the digits of the totals it declares; the row payload itself is
    # pinned at the bound.
    assert 0 <= large_bytes - small_bytes <= 8
    assert large_full_bytes > 5 * large_bytes

    bounds = large_summary["summary_row_bounds"]
    assert bounds["by_model"]["returned_count"] == 10
    assert bounds["by_model"]["total_count"] == 128
    # Ten dated buckets plus the retained undated one.
    assert bounds["by_day"]["returned_count"] == 11
    assert bounds["by_day"]["total_count"] == 161
    assert bounds["cost_per_accepted_outcome_routes"]["returned_count"] == 10
    assert bounds["model_outcomes_models"]["returned_count"] == 10
    assert bounds["model_outcomes_models"]["total_count"] == 128

    # Totals, cost coverage and UNKNOWN semantics are identical whatever
    # the history size, and the UNKNOWN route row of a kept model is intact.
    assert large_summary["counts"] == small_summary["counts"]
    assert large_summary["cost_quality"] == small_summary["cost_quality"]
    assert large_summary["cost_quality"]["zero_cost_is_free"] is False
    assert large_summary["cache_quality"] == small_summary["cache_quality"]
    assert large_summary["retry_economics"] == small_summary[
        "retry_economics"
    ]
    kept_route = large_summary["cost_per_accepted_outcome"]["routes"]
    leaf = kept_route["route-model-000"]["route-a"]["code"]
    assert leaf["unknown"]["state"] == "UNKNOWN"
    assert leaf["unknown"]["cost_per_accepted_outcome_usd"] is None
    assert large_summary["cost_per_accepted_outcome"]["unknown_model_tasks"] == 3
    assert large_summary["omitted_dimensions"] == [
        "by_role",
        "by_runner",
        "by_topic",
    ]


def _partial_coverage_ledger(*, priced_records: int):
    """Twenty fully priced models beside one mostly-unpriced heavy bucket.

    ``priced_records`` is the only thing that varies: 0 leaves the heavy
    bucket entirely unpriced, 1 gives it a single cheap priced record out
    of 500.  The magnitude the provider never priced is essentially the
    same either way, so the bound must keep the bucket in both worlds.
    """

    def _build(**_kwargs):
        by_model = {
            f"priced-{index:02d}": {
                "records": 10,
                "total_tokens": 1_000,
                "cost_usd": float(20 - index),
                "cost_known_records": 10,
                "cost_unknown_records": 0,
                "tokens_with_unknown_cost": 0,
            }
            for index in range(20)
        }
        by_model["partial-heavy"] = {
            "records": 500,
            "total_tokens": 5_000_000,
            # One cheap priced record; the other 499 were never priced.
            "cost_usd": 0.004 if priced_records else 0.0,
            "cost_known_records": priced_records,
            "cost_unknown_records": 500 - priced_records,
            "tokens_with_unknown_cost": 5_000_000 - 10 * priced_records,
        }
        return {
            "tool": "aiworkhub_task_cost_ledger",
            "counts": {"union_rows": 700},
            "cost_quality": {
                "known_records": 200 + priced_records,
                "unknown_records": 500 - priced_records,
                "zero_cost_is_free": False,
                "reason": "provider_cost_absence_is_unknown_not_zero",
            },
            "aggregates": {
                "by_model": by_model,
                "by_provider": {"provider-a": {"records": 700}},
                "by_day": {"2026-08-04": {"records": 700}},
            },
            "tasks": [],
        }

    return _build


def test_cost_ledger_partial_cost_coverage_keeps_unpriced_magnitude(
    monkeypatch,
):
    """One cheap priced record must not delete 499 unpriced ones.

    ``cost_known_records > 0`` was read as full coverage, so a bucket
    holding a single $0.004 record among 500 ranked on that amount, landed
    last of twenty-one priced models and was cut -- while the identical
    bucket with zero priced records survived on its magnitude.  Pricing
    one record made five million unpriced tokens vanish from the summary.
    """

    monkeypatch.setattr(
        server.cost_ledger,
        "build_cost_ledger",
        _partial_coverage_ledger(priced_records=1),
    )
    partial = server.aiworkhub_task_cost_ledger()

    monkeypatch.setattr(
        server.cost_ledger,
        "build_cost_ledger",
        _partial_coverage_ledger(priced_records=0),
    )
    unpriced = server.aiworkhub_task_cost_ledger()

    # The bucket survives whether or not one of its 500 records is priced.
    assert "partial-heavy" in partial["aggregates"]["by_model"]
    assert "partial-heavy" in unpriced["aggregates"]["by_model"]
    # It wins its slot on unpriced magnitude rather than on $0.004, so it
    # ranks second either way -- behind the priciest fully covered model.
    assert list(partial["aggregates"]["by_model"])[:2] == [
        "priced-00",
        "partial-heavy",
    ]
    assert list(unpriced["aggregates"]["by_model"])[:2] == [
        "priced-00",
        "partial-heavy",
    ]
    # Partial coverage stays visible on the kept row, unrounded.
    kept = partial["aggregates"]["by_model"]["partial-heavy"]
    assert kept["cost_known_records"] == 1
    assert kept["cost_unknown_records"] == 499
    assert kept["tokens_with_unknown_cost"] == 4_999_990
    assert kept["cost_usd"] == 0.004
    # The bound itself is unchanged: still ten of twenty-one rows.
    assert partial["summary_row_bounds"]["by_model"] == {
        "total_count": 21,
        "returned_count": 10,
        "truncated": True,
        "ranked_by": "observed_cost_usd_then_unpriced_activity",
    }
    assert partial["cost_quality"]["zero_cost_is_free"] is False


def _usage_unobserved_outcome_ledger(**_kwargs):
    """``model_outcomes`` rows whose usage attempts were never observed.

    ``_model_outcome_matrix`` still counts the decision, so such a row
    reads ``total_tokens`` 0 beside a real ``decided_tasks`` count.
    """

    models = {
        f"unobserved-model-{index:02d}": {
            "decided_tasks": index + 1,
            "accepted": index,
            "rejected": 1,
            "acceptance_rate_percent": 50.0,
            "usage_observed_tasks": 0,
            "cost_observed_tasks": 0,
            "total_tokens": 0,
            "cost_usd": 0.0,
        }
        for index in range(24)
    }
    return {
        "tool": "aiworkhub_task_cost_ledger",
        "counts": {"union_rows": 300},
        "aggregates": {
            "by_model": {"model-a": {"records": 300}},
            "by_provider": {"provider-a": {"records": 300}},
            "by_day": {"2026-08-04": {"records": 300}},
        },
        "model_outcomes": {
            "schema_id": "aiworkhub.model_outcome_matrix.v1",
            "association_only": True,
            "models": models,
            "unmatched_decisions": 4,
        },
        "tasks": [],
    }


def test_cost_ledger_ranks_usage_unobserved_rows_by_decided_tasks(monkeypatch):
    """A zero ``total_tokens`` is absence, not a magnitude of zero.

    The magnitude helper stopped at the first field merely present, so a
    usage-unobserved outcome row reported 0.0 however many tasks it
    decided.  Every row then tied at zero and the key tie-break ordered
    them alphabetically: the bound kept the first ten names and cut the
    model that had decided the most tasks.
    """

    monkeypatch.setattr(
        server.cost_ledger,
        "build_cost_ledger",
        _usage_unobserved_outcome_ledger,
    )

    summary = server.aiworkhub_task_cost_ledger()
    kept = summary["model_outcomes"]["models"]

    # Ranked by decisions, most first -- not by name.
    assert list(kept) == [
        f"unobserved-model-{index:02d}" for index in range(23, 13, -1)
    ]
    assert "unobserved-model-00" not in kept
    assert kept["unobserved-model-23"]["decided_tasks"] == 24
    # Coverage truth on the kept rows is untouched.
    assert kept["unobserved-model-23"]["usage_observed_tasks"] == 0
    assert kept["unobserved-model-23"]["cost_observed_tasks"] == 0
    assert summary["summary_row_bounds"]["model_outcomes_models"] == {
        "total_count": 24,
        "returned_count": 10,
        "truncated": True,
        "ranked_by": "observed_cost_usd_then_unpriced_activity",
    }
    assert summary["model_outcomes"]["unmatched_decisions"] == 4


def _day_heavy_ledger(*, dated: int, undated: int):
    """More dated days and more undated buckets than either group keeps."""

    def _build(**_kwargs):
        by_day = {
            f"2026-{1 + index // 28:02d}-{1 + index % 28:02d}": {
                "records": 3,
                "total_tokens": 100 + index,
            }
            for index in range(dated)
        }
        by_day.update({
            f"unknown-{index:02d}": {"records": 2, "total_tokens": 7}
            for index in range(undated)
        })
        return {
            "tool": "aiworkhub_task_cost_ledger",
            "counts": {"union_rows": 500},
            "aggregates": {
                "by_model": {
                    f"model-{index:02d}": {"records": 5, "total_tokens": index}
                    for index in range(30)
                },
                "by_provider": {"provider-a": {"records": 500}},
                "by_day": by_day,
            },
            "tasks": [],
        }

    return _build


def test_cost_ledger_by_day_ceiling_is_two_top_n_and_documented(monkeypatch):
    """``by_day`` holds two capped groups, so its ceiling is 2N, not N.

    The tool docstring claimed every bounded dimension returned at most
    ``_COST_LEDGER_SUMMARY_TOP_N`` rows while ``by_day`` could return ten
    dated buckets plus ten undated ones.  The documented contract has to
    state the bound the code actually applies.
    """

    top_n = server._COST_LEDGER_SUMMARY_TOP_N
    monkeypatch.setattr(
        server.cost_ledger,
        "build_cost_ledger",
        _day_heavy_ledger(dated=40, undated=14),
    )

    summary = server.aiworkhub_task_cost_ledger()
    days = summary["aggregates"]["by_day"]
    dated_keys = [key for key in days if key.startswith("2026-")]
    undated_keys = [key for key in days if not key.startswith("2026-")]

    # Each group is capped, and the dimension's real ceiling is their sum.
    assert len(dated_keys) == top_n
    assert len(undated_keys) == top_n
    assert summary["summary_row_bounds"]["by_day"] == {
        "total_count": 54,
        "returned_count": 2 * top_n,
        "truncated": True,
        "ranked_by": "most_recent_day_then_undated",
    }
    # Every other bounded dimension really is capped at N.
    assert len(summary["aggregates"]["by_model"]) == top_n

    # The docstring states that exact ceiling rather than a flat top-N.
    doc = server.aiworkhub_task_cost_ledger.__doc__ or ""
    assert "by_day" in doc
    assert "2 * _COST_LEDGER_SUMMARY_TOP_N" in doc
