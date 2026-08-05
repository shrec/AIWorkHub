#!/usr/bin/env python3
"""Verify retry/role snapshot arithmetic and its non-causal boundaries."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OBSERVATION = ROOT / "benchmarks" / "retry-role-observation-v1.json"


def _rate(numerator: int, denominator: int) -> float:
    return round(100.0 * numerator / denominator, 1) if denominator else 0.0


def check(path: Path = DEFAULT_OBSERVATION) -> list[str]:
    try:
        doc: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return [f"retry_role_observation_unreadable:{exc}"]

    errors: list[str] = []
    source = doc.get("source") or {}
    retry = doc.get("retry_economics") or {}
    roles = doc.get("role_attribution") or {}
    attempts = int(retry.get("attempt_records") or 0)
    tasks = int(source.get("tasks_with_usage") or 0)
    retries = int(retry.get("retry_records") or 0)
    if attempts != int(source.get("usage_records") or 0):
        errors.append("retry_role_usage_population_mismatch")
    if retries != attempts - tasks:
        errors.append("retry_role_retry_population_mismatch")
    if float(retry.get("retry_record_rate_percent") or 0.0) != _rate(
        retries, attempts
    ):
        errors.append("retry_role_retry_rate_mismatch")
    known_retry = int(retry.get("retry_cost_known_records") or 0)
    unknown_retry = int(retry.get("retry_cost_unknown_records") or 0)
    if known_retry + unknown_retry != retries:
        errors.append("retry_role_retry_cost_population_mismatch")
    retried_tasks = int(retry.get("tasks_with_retries") or 0)
    accepted_retried = int(retry.get("accepted_retried_tasks") or 0)
    if float(
        retry.get("accepted_rate_among_retried_tasks_percent") or 0.0
    ) != _rate(accepted_retried, retried_tasks):
        errors.append("retry_role_retried_acceptance_rate_mismatch")

    worker = roles.get("worker") or {}
    reviewer = roles.get("reviewer") or {}
    role_records = int(worker.get("records") or 0) + int(
        reviewer.get("records") or 0
    )
    if role_records != attempts:
        errors.append("retry_role_role_population_mismatch")
    explicit = int(roles.get("explicit_role_records") or 0)
    inferred = int(roles.get("legacy_inferred_role_records") or 0)
    if explicit + inferred != attempts:
        errors.append("retry_role_role_quality_population_mismatch")

    boundaries = doc.get("claim_boundaries") or {}
    for key in (
        "retry_tokens_are_avoidable_savings",
        "retry_caused_acceptance",
        "role_cost_is_complete",
    ):
        if boundaries.get(key) is not False:
            errors.append(f"retry_role_unsafe_claim:{key}")
    if boundaries.get("association_only") is not True:
        errors.append("retry_role_association_boundary_missing")
    return errors


if __name__ == "__main__":
    failures = check()
    if failures:
        print("retry/role observation check failed:")
        for failure in failures:
            print(f"- {failure}")
        raise SystemExit(1)
    print("retry/role observation check passed")
