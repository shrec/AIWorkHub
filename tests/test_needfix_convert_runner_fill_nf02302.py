"""NF-2026-02302: a prior-only workforce ranking must not fill the cheapest runner."""

from __future__ import annotations

from typing import Any

import pytest

from aiworkhub import manager_ai_tools, server

PRIOR_ONLY = "workforce_rank:prior_only_mid_tier"
TOP1 = "workforce_rank:top1"


FLOOR_REASON = "observed_or_prior_quality_below_floor"


def _candidate(
    runner: str,
    cost: float | None,
    samples: int,
    *,
    excluded: bool = False,
    reasons: list[str] | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "worker_id": runner,
        "adapter_id": f"adapter_{runner}",
        "model": f"model_{runner}",
        "excluded": excluded,
        "execution_runner": runner,
        "score_components": {"sample_count": samples, "estimated_cost_usd": cost},
    }
    if reasons is not None:
        item["exclusion_reasons"] = reasons
    return item


def _reply(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    top = candidates[0]
    return {
        "candidates": candidates,
        "launch_contract": {
            "runner": top["execution_runner"],
            "adapter_id": top["adapter_id"],
            "model": top["model"],
            "task_id": "needfix-NF-X",
            "identity_rule": "use_same_runner_for_task_create_and_agent_launch_task",
        },
    }


@pytest.fixture
def fixed_rank(monkeypatch: pytest.MonkeyPatch):
    def install(reply: dict[str, Any]) -> None:
        monkeypatch.setattr(manager_ai_tools, "workforce_rank", lambda **_kwargs: reply)

    monkeypatch.setattr(server, "_needfix_merged_card", lambda _needfix_id, plan: dict(plan))
    monkeypatch.setattr(server, "_needfix_rank_risk_tier", lambda _card: "medium")
    return install


def _normalize(plan: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    normalized, _normalizations, filled_from = server._normalize_needfix_task_plan("NF-X", plan)
    return normalized, filled_from


def test_prior_only_ranking_skips_cheapest_and_labels_mid_tier(fixed_rank) -> None:
    fixed_rank(
        _reply(
            [
                _candidate("claude_haiku", 0.01, 0),
                _candidate("claude_sonnet", 0.05, 0),
                _candidate("claude_opus", 0.30, 0),
            ]
        )
    )
    plan, filled_from = _normalize({"topic": "t"})
    assert plan["runner"] == "claude_sonnet"
    assert filled_from == {"runner": PRIOR_ONLY}


def test_prior_only_ignores_excluded_and_unknown_cost_candidates(fixed_rank) -> None:
    fixed_rank(
        _reply(
            [
                _candidate("claude_haiku", 0.01, 0),
                _candidate("codex_unknown", None, 0),
                _candidate("claude_sonnet", 0.05, 0, excluded=True),
                _candidate("claude_opus", 0.30, 0),
            ]
        )
    )
    plan, filled_from = _normalize({"topic": "t"})
    assert plan["runner"] == "claude_opus"
    assert filled_from == {"runner": PRIOR_ONLY}


def test_prior_only_without_a_higher_cost_tier_keeps_top1(fixed_rank) -> None:
    fixed_rank(_reply([_candidate("claude_haiku", 0.01, 0), _candidate("other", 0.01, 0)]))
    plan, filled_from = _normalize({"topic": "t"})
    assert plan["runner"] == "claude_haiku"
    assert filled_from == {"runner": TOP1}


def test_ranking_with_samples_keeps_top1(fixed_rank) -> None:
    fixed_rank(
        _reply(
            [
                _candidate("claude_haiku", 0.01, 12),
                _candidate("claude_sonnet", 0.05, 0),
            ]
        )
    )
    plan, filled_from = _normalize({"topic": "t"})
    assert plan["runner"] == "claude_haiku"
    assert filled_from == {"runner": TOP1}


def test_explicit_runner_is_never_touched(fixed_rank) -> None:
    fixed_rank(
        _reply(
            [
                _candidate("claude_haiku", 0.01, 0),
                _candidate("claude_sonnet", 0.05, 0),
            ]
        )
    )
    plan, filled_from = _normalize({"runner": "codex_explicit", "topic": "t"})
    assert plan["runner"] == "codex_explicit"
    assert "runner" not in filled_from


def test_all_candidates_excluded_by_quality_floor_still_fill_mid_tier(fixed_rank) -> None:
    floor = [FLOOR_REASON]
    fixed_rank(
        _reply(
            [
                _candidate("claude_haiku", 0.01, 0, excluded=True, reasons=floor),
                _candidate("claude_sonnet", 0.05, 0, excluded=True, reasons=floor),
                _candidate("claude_opus", 0.30, 0, excluded=True, reasons=floor),
            ]
        )
    )
    plan, filled_from = _normalize({"topic": "t"})
    assert plan["runner"] == "claude_sonnet"
    assert filled_from == {"runner": PRIOR_ONLY}


def test_mid_tier_excluded_for_a_hard_reason_is_skipped(fixed_rank) -> None:
    floor = [FLOOR_REASON]
    fixed_rank(
        _reply(
            [
                _candidate("claude_haiku", 0.01, 0, excluded=True, reasons=floor),
                _candidate(
                    "claude_sonnet", 0.05, 0, excluded=True, reasons=[FLOOR_REASON, "worker_unavailable"]
                ),
                _candidate("claude_opus", 0.30, 0, excluded=True, reasons=floor),
            ]
        )
    )
    plan, filled_from = _normalize({"topic": "t"})
    assert plan["runner"] == "claude_opus"
    assert filled_from == {"runner": PRIOR_ONLY}


def test_any_observed_sample_among_candidates_keeps_top1(fixed_rank) -> None:
    fixed_rank(
        _reply(
            [
                _candidate("claude_haiku", 0.01, 0),
                _candidate("claude_sonnet", 0.05, 3),
                _candidate("claude_opus", 0.30, 0),
            ]
        )
    )
    plan, filled_from = _normalize({"topic": "t"})
    assert plan["runner"] == "claude_haiku"
    assert filled_from == {"runner": TOP1}


def test_mid_tier_equal_to_top1_runner_keeps_top1_label(fixed_rank) -> None:
    fixed_rank(_reply([_candidate("claude_sonnet", 0.05, 0), _candidate("claude_haiku", 0.01, 0)]))
    plan, filled_from = _normalize({"topic": "t"})
    assert plan["runner"] == "claude_sonnet"
    assert filled_from == {"runner": TOP1}
