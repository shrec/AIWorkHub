#!/usr/bin/env python3
"""Verify the checked-in semantic-edit pilot without inventing claims."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LEDGER = ROOT / "benchmarks" / "semantic-edit-pilot-v1.json"
METRICS = ("input_tokens", "cached_input_tokens", "output_tokens", "total_tokens")


def _reduction(focused: float, baseline: float) -> float:
    if baseline <= 0:
        raise ValueError("benchmark_baseline_must_be_positive")
    return round(100.0 * (1.0 - focused / baseline), 3)


def check(path: Path = DEFAULT_LEDGER) -> list[str]:
    errors: list[str] = []
    try:
        document: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"benchmark_unreadable:{exc}"]

    pairs = document.get("pairs")
    if not isinstance(pairs, list) or not pairs:
        return ["benchmark_pairs_missing"]
    design = document.get("design") or {}
    if int(design.get("pair_count") or 0) != len(pairs):
        errors.append("benchmark_pair_count_mismatch")

    calculated = {
        variant: {metric: 0 for metric in (*METRICS, "duration_seconds")}
        for variant in ("focused", "baseline")
    }
    request_ids: set[str] = set()
    for pair in pairs:
        if not isinstance(pair, dict):
            errors.append("benchmark_pair_not_object")
            continue
        for variant in ("focused", "baseline"):
            run = pair.get(variant)
            if not isinstance(run, dict):
                errors.append(f"benchmark_{variant}_run_missing")
                continue
            request_id = str(run.get("request_id") or "")
            if not request_id or request_id in request_ids:
                errors.append("benchmark_request_id_missing_or_duplicate")
            request_ids.add(request_id)
            if run.get("terminal_worker_state") != "review_ready":
                errors.append("benchmark_run_not_review_ready")
            if int(run.get("total_tokens") or 0) != (
                int(run.get("input_tokens") or 0)
                + int(run.get("output_tokens") or 0)
            ):
                errors.append("benchmark_run_token_total_mismatch")
            for metric in METRICS:
                calculated[variant][metric] += int(run.get(metric) or 0)
            calculated[variant]["duration_seconds"] += float(
                run.get("duration_seconds") or 0.0
            )

    aggregate = document.get("aggregate") or {}
    for variant in ("focused", "baseline"):
        recorded = aggregate.get(variant) or {}
        for metric in METRICS:
            if int(recorded.get(metric) or 0) != calculated[variant][metric]:
                errors.append(f"benchmark_aggregate_{variant}_{metric}_mismatch")
        uncached = calculated[variant]["input_tokens"] - calculated[variant][
            "cached_input_tokens"
        ]
        if int(recorded.get("uncached_input_tokens") or 0) != uncached:
            errors.append(f"benchmark_aggregate_{variant}_uncached_mismatch")
        if abs(
            float(recorded.get("duration_seconds") or 0.0)
            - calculated[variant]["duration_seconds"]
        ) > 0.000001:
            errors.append(f"benchmark_aggregate_{variant}_duration_mismatch")

    reductions = aggregate.get("observed_reduction_percent") or {}
    for metric in ("input_tokens", "output_tokens", "total_tokens", "duration_seconds"):
        expected = _reduction(
            calculated["focused"][metric], calculated["baseline"][metric]
        )
        if abs(float(reductions.get(metric) or 0.0) - expected) > 0.001:
            errors.append(f"benchmark_reduction_{metric}_mismatch")

    focused_uncached = (
        calculated["focused"]["input_tokens"]
        - calculated["focused"]["cached_input_tokens"]
    )
    baseline_uncached = (
        calculated["baseline"]["input_tokens"]
        - calculated["baseline"]["cached_input_tokens"]
    )
    uncached_change = round(
        100.0 * (focused_uncached / baseline_uncached - 1.0), 3
    )
    if abs(
        float(aggregate.get("uncached_input_change_percent") or 0.0)
        - uncached_change
    ) > 0.001:
        errors.append("benchmark_uncached_input_change_mismatch")

    claim_status = document.get("claim_status") or {}
    if claim_status.get("public_claim_eligible") is not False:
        errors.append("benchmark_small_pilot_must_not_be_public_claim_eligible")
    required_reasons = {
        "sample_size_two_pairs",
        "cache_mix_differs_between_variants",
        "manager_acceptance_not_observed_in_ledger",
    }
    if not required_reasons.issubset(set(claim_status.get("reason_codes") or [])):
        errors.append("benchmark_claim_limit_reasons_missing")
    return errors


def main() -> int:
    errors = check()
    if errors:
        print("semantic-edit benchmark check failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print("semantic-edit benchmark check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
