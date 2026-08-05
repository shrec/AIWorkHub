"""Verify the checked-in provider-routing observation and claim boundaries."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OBSERVATION = ROOT / "benchmarks" / "provider-routing-observation-v1.json"


def _close(actual: float, expected: float, tolerance: float = 0.001) -> bool:
    return abs(float(actual) - float(expected)) <= tolerance


def check(path: Path = DEFAULT_OBSERVATION) -> list[str]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return [f"provider_routing_observation_unreadable:{exc}"]

    errors: list[str] = []
    source = doc.get("source") or {}
    if int(source.get("parser_matches") or 0) + int(
        source.get("reference_extractor_mismatches") or 0
    ) != int(source.get("total_runs") or 0):
        errors.append("provider_routing_population_mismatch")
    if int(source.get("parser_mismatches") or 0) != 0:
        errors.append("provider_parser_mismatch_claim_not_supported")

    models = doc.get("cache_and_cost") or {}
    claude_names = ("claude-sonnet-5", "claude-opus-4-8", "claude-haiku-4-5")
    if any(name not in models for name in claude_names):
        errors.append("provider_routing_claude_models_missing")
        return errors
    claude_runs = sum(int(models[name].get("runs") or 0) for name in claude_names)
    claude_cost = round(
        sum(float(models[name].get("observed_cost_usd") or 0.0) for name in claude_names),
        2,
    )
    totals = doc.get("claude_observed_totals") or {}
    if claude_runs != int(totals.get("runs") or 0):
        errors.append("provider_routing_claude_run_total_mismatch")
    if not _close(claude_cost, float(totals.get("cost_usd") or 0.0), 0.005):
        errors.append("provider_routing_claude_cost_total_mismatch")

    routing = doc.get("routing_economics") or {}
    opus_rate = float(models["claude-opus-4-8"].get("cost_usd_per_million_tokens") or 0.0)
    sonnet_rate = float(models["claude-sonnet-5"].get("cost_usd_per_million_tokens") or 0.0)
    haiku_rate = float(models["claude-haiku-4-5"].get("cost_usd_per_million_tokens") or 0.0)
    if not _close(routing.get("opus_cost_per_token_vs_sonnet", 0.0), opus_rate / sonnet_rate):
        errors.append("provider_routing_opus_sonnet_ratio_mismatch")
    if not _close(routing.get("opus_cost_per_token_vs_haiku", 0.0), opus_rate / haiku_rate):
        errors.append("provider_routing_opus_haiku_ratio_mismatch")
    counterfactual = float(routing.get("same_opus_volume_at_sonnet_price_usd") or 0.0)
    delta = round(float(models["claude-opus-4-8"]["observed_cost_usd"]) - counterfactual, 2)
    if not _close(delta, routing.get("potential_cost_delta_usd", 0.0), 0.005):
        errors.append("provider_routing_counterfactual_delta_mismatch")
    reduction = round(100.0 * delta / claude_cost, 1) if claude_cost else 0.0
    if not _close(reduction, routing.get("potential_total_cost_reduction_percent", 0.0), 0.05):
        errors.append("provider_routing_counterfactual_percent_mismatch")

    boundaries = doc.get("claim_boundaries") or {}
    if routing.get("status") != "unrealized_counterfactual":
        errors.append("provider_routing_counterfactual_status_missing")
    if boundaries.get("realized_cost_savings_claimed") is not False:
        errors.append("provider_routing_false_realized_savings_claim")
    if boundaries.get("universal_token_multiplier") is not False:
        errors.append("provider_routing_false_universal_multiplier_claim")
    return errors


if __name__ == "__main__":
    failures = check()
    if failures:
        print("provider-routing observation check failed:")
        for failure in failures:
            print(f"- {failure}")
        raise SystemExit(1)
    print("provider-routing observation check passed")
