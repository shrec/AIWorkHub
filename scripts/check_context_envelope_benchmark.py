#!/usr/bin/env python3
"""Verify the deterministic Project Context Bundle v1/v2 byte benchmark."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BENCHMARK = ROOT / "benchmarks" / "context-envelope-encoding-v2.json"


def check(path: Path = DEFAULT_BENCHMARK) -> list[str]:
    try:
        document: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"context_envelope_benchmark_unreadable:{exc}"]

    errors: list[str] = []
    design = document.get("design") or {}
    measurement = document.get("measurement") or {}
    claim = document.get("claim_status") or {}
    legacy = int(measurement.get("legacy_v1_bundle_bytes") or 0)
    nested = int(measurement.get("nested_v2_bundle_bytes") or 0)
    selected = int(measurement.get("selected_evidence_bytes") or 0)
    envelope = int(measurement.get("nested_v2_envelope_bytes") or 0)
    if legacy <= 0 or nested <= 0 or selected <= 0:
        errors.append("context_envelope_population_missing")
    if nested - legacy != int(measurement.get("delta_bytes") or 0):
        errors.append("context_envelope_delta_mismatch")
    if nested - selected != envelope:
        errors.append("context_envelope_overhead_mismatch")
    expected_reduction = round(100.0 * (1.0 - nested / legacy), 3) if legacy else 0.0
    if abs(float(measurement.get("reduction_percent") or 0.0) - expected_reduction) > 0.001:
        errors.append("context_envelope_reduction_mismatch")
    if design.get("comparison") != "same_evidence_legacy_v1_vs_nested_v2_serialization":
        errors.append("context_envelope_comparison_not_same_evidence")
    for key in (
        "token_savings_available",
        "cost_savings_available",
        "latency_savings_available",
        "quality_improvement_available",
    ):
        if claim.get(key) is not False:
            errors.append(f"unsafe_context_envelope_claim:{key}")
    return errors


def main() -> int:
    errors = check()
    if errors:
        print("context-envelope benchmark check failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print("context-envelope benchmark check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
