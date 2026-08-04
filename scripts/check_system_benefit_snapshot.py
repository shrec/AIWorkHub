#!/usr/bin/env python3
"""Validate the public system-benefit snapshot and its claim boundaries."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SNAPSHOT = ROOT / "benchmarks" / "system-benefit-snapshot-v1.json"
DEFAULT_SEMANTIC_EDIT_PILOT = ROOT / "benchmarks" / "semantic-edit-pilot-v1.json"


def _rate(numerator: float, denominator: float, digits: int = 1) -> float:
    if denominator <= 0:
        raise ValueError("benchmark_denominator_must_be_positive")
    return round(100.0 * numerator / denominator, digits)


def _close(actual: Any, expected: float, tolerance: float = 0.05) -> bool:
    return abs(float(actual) - expected) <= tolerance


def check(
    path: Path = DEFAULT_SNAPSHOT,
    semantic_edit_path: Path = DEFAULT_SEMANTIC_EDIT_PILOT,
) -> list[str]:
    try:
        doc: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"system_benchmark_unreadable:{exc}"]

    errors: list[str] = []
    outcomes = doc.get("task_outcomes") or {}
    expected = _rate(
        float(outcomes.get("review_ready_runs") or 0),
        float(outcomes.get("terminal_runs") or 0),
    )
    if not _close(outcomes.get("review_ready_rate_percent"), expected):
        errors.append("review_ready_rate_mismatch")
    expected = _rate(
        float(outcomes.get("validation_failed_runs") or 0),
        float(outcomes.get("terminal_runs") or 0),
    )
    if not _close(outcomes.get("validation_failed_rate_percent"), expected):
        errors.append("validation_failed_rate_mismatch")
    expected = _rate(
        float(outcomes.get("manager_accepted") or 0),
        float(outcomes.get("manager_decisions") or 0),
    )
    if not _close(outcomes.get("manager_acceptance_rate_percent"), expected):
        errors.append("manager_acceptance_rate_mismatch")

    for cohort in doc.get("tool_use_cohorts") or []:
        expected = _rate(
            float(cohort.get("review_ready_runs") or 0),
            float(cohort.get("terminal_runs") or 0),
        )
        if not _close(cohort.get("review_ready_rate_percent"), expected):
            errors.append(f"tool_cohort_rate_mismatch:{cohort.get('name')}")

    context = doc.get("context_delivery") or {}
    pre = int(context.get("pre_optimization_tool_section_bytes") or 0)
    optimized = int(context.get("optimized_section_bytes") or 0)
    delivered = int(context.get("delivered_bundle_bytes") or 0)
    envelope = int(context.get("envelope_bytes_added") or 0)
    if pre - optimized != int(context.get("optimization_bytes_removed") or 0):
        errors.append("context_optimization_delta_mismatch")
    if optimized + envelope != delivered:
        errors.append("context_delivery_sum_mismatch")
    net_added = delivered - pre
    if net_added != int(context.get("net_bytes_added") or 0):
        errors.append("context_net_delta_mismatch")
    if not _close(
        context.get("context_expansion_rate_percent"), _rate(net_added, pre)
    ):
        errors.append("context_expansion_rate_mismatch")

    edits = doc.get("semantic_edit_structural") or {}
    file_bytes = int(edits.get("file_bytes") or 0)
    replacement = int(edits.get("replacement_bytes") or 0)
    if not _close(
        edits.get("replacement_to_file_byte_rate_percent"),
        _rate(replacement, file_bytes, 2),
        0.01,
    ):
        errors.append("semantic_edit_replacement_rate_mismatch")
    ratio = round(file_bytes / replacement, 2) if replacement else 0.0
    if not _close(edits.get("structural_file_to_replacement_ratio"), ratio, 0.01):
        errors.append("semantic_edit_structural_ratio_mismatch")

    paired = doc.get("semantic_edit_paired_pilot") or {}
    try:
        paired_doc: dict[str, Any] = json.loads(
            semantic_edit_path.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"semantic_edit_paired_artifact_unreadable:{exc}")
        paired_doc = {}
    paired_aggregate = paired_doc.get("aggregate") or {}
    paired_reductions = paired_aggregate.get("observed_reduction_percent") or {}
    paired_design = paired_doc.get("design") or {}
    paired_claim = paired_doc.get("claim_status") or {}
    pair_caps: list[tuple[Any, Any]] = []
    for pair in paired_doc.get("pairs") or []:
        if not isinstance(pair, dict):
            continue
        focused = pair.get("focused") or {}
        baseline = pair.get("baseline") or {}
        pair_caps.append(
            (
                focused.get("task_card_max_live_tokens"),
                baseline.get("task_card_max_live_tokens"),
            )
        )
    natural_uncapped = bool(pair_caps) and all(
        focused_cap is None and baseline_cap is None
        for focused_cap, baseline_cap in pair_caps
    )
    token_budget_pair_parity = bool(pair_caps) and all(
        focused_cap == baseline_cap for focused_cap, baseline_cap in pair_caps
    )
    if paired_design.get("natural_uncapped") != natural_uncapped:
        errors.append("semantic_edit_natural_uncapped_mismatch")
    if paired_design.get("token_budget_pair_parity") != token_budget_pair_parity:
        errors.append("semantic_edit_token_budget_pair_parity_mismatch")
    reason_codes = set(paired_claim.get("reason_codes") or [])
    if not natural_uncapped:
        for reason in ("explicit_token_caps_present", "not_natural_uncapped_workload"):
            if reason not in reason_codes:
                errors.append(f"semantic_edit_missing_reason:{reason}")
    if not token_budget_pair_parity and "pair_token_budget_mismatch" not in reason_codes:
        errors.append("semantic_edit_missing_reason:pair_token_budget_mismatch")
    if (not natural_uncapped or not token_budget_pair_parity) and paired_claim.get(
        "public_claim_eligible"
    ) is not False:
        errors.append("semantic_edit_unsafe_budget_claim")
    paired_expected = {
        "schema_id": paired_doc.get("schema_id"),
        "pair_count": paired_design.get("pair_count"),
        "model": paired_design.get("model"),
        "adapter_id": paired_design.get("adapter_id"),
        "total_token_reduction_percent": paired_reductions.get("total_tokens"),
        "output_token_reduction_percent": paired_reductions.get("output_tokens"),
        "elapsed_reduction_percent": paired_reductions.get("duration_seconds"),
        "uncached_input_change_percent": paired_aggregate.get(
            "uncached_input_change_percent"
        ),
        "manager_acceptance_observed": paired_design.get(
            "manager_acceptance_observed"
        ),
        "natural_uncapped": natural_uncapped,
        "token_budget_pair_parity": token_budget_pair_parity,
        "public_claim_eligible": paired_claim.get("public_claim_eligible"),
    }
    for key, expected_value in paired_expected.items():
        actual_value = paired.get(key)
        if isinstance(expected_value, float):
            if not _close(actual_value, expected_value, 0.001):
                errors.append(f"semantic_edit_paired_snapshot_mismatch:{key}")
        elif actual_value != expected_value:
            errors.append(f"semantic_edit_paired_snapshot_mismatch:{key}")
    try:
        expected_artifact = semantic_edit_path.relative_to(ROOT).as_posix()
    except ValueError:
        expected_artifact = semantic_edit_path.as_posix()
    if paired.get("artifact") != expected_artifact:
        errors.append("semantic_edit_paired_artifact_path_mismatch")

    callbacks = doc.get("callback_reliability") or {}
    resolved = int(callbacks.get("delivered") or 0) + int(
        callbacks.get("superseded") or 0
    )
    if resolved + int(callbacks.get("dead_letter") or 0) != int(
        callbacks.get("events_total") or 0
    ):
        errors.append("callback_population_mismatch")
    if not _close(
        callbacks.get("resolved_without_dead_letter_rate_percent"),
        _rate(resolved, int(callbacks.get("events_total") or 0)),
    ):
        errors.append("callback_resolution_rate_mismatch")

    claims = doc.get("claim_status") or {}
    required_limits = {
        "system_wide_token_savings": "unmeasured",
        "source_graph_end_to_end_token_multiplier": "unmeasured",
        "system_wide_cost_savings": "unmeasured",
        "tool_use_quality_causality": "observational_only",
        "semantic_edit_paired_result": "pilot_only",
    }
    for key, expected_value in required_limits.items():
        if claims.get(key) != expected_value:
            errors.append(f"unsafe_claim_status:{key}")
    return errors


def main() -> int:
    errors = check()
    if errors:
        print("system-benefit benchmark check failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print("system-benefit benchmark check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
