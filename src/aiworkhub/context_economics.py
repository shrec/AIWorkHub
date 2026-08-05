"""Provider-neutral context economics for AIWorkHub.

Replaces the misleading raw-bytes versus envelope-bytes comparison with an
honest, evidence-backed measurement model.  Defines separate populations,
computes only evidence-backed ratios, and never presents byte estimates as
token or dollar truth.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

SCHEMA_ID = "aiworkhub.task_mcp.context_economics.v1"


# ---------------------------------------------------------------------------
# Single-task measurement
# ---------------------------------------------------------------------------

def measure_context_delivery(
    *,
    project_context_metadata: dict[str, Any] | None = None,
    naive_discover_bytes: int | None = None,
    usage: dict[str, Any] | None = None,
    adapter_id: str = "",
    outcome: str = "",
    repo_id: str = "",
    task_id: str = "",
    task_type: str = "",
    runner: str = "",
    topic: str = "",
) -> dict[str, Any]:
    """Produce one honest task-level measurement record.

    Populations are defined separately: naive discover/read context, selected
    source material, serialized envelope overhead, delivered prompt context,
    cached input, model input/output, and monetary cost.  No field is invented
    when evidence is absent.
    """

    metadata: dict[str, Any] = project_context_metadata or {}
    sections: list[dict[str, Any]] = metadata.get("sections") or []

    # -- Byte populations ---------------------------------------------------
    # Selected source = sum of tool output content bytes (after Source Graph
    # selection).  This is NOT a raw repository estimate — the tool already
    # filtered/summarised.
    selected_source_bytes: int | None = None
    if sections:
        selected_source_bytes = sum(
            int(s.get("bytes") or 0) for s in sections
        )

    delivered_prompt_bytes: int | None = None
    bundle_bytes_val = metadata.get("bundle_bytes")
    if bundle_bytes_val is not None:
        delivered_prompt_bytes = int(bundle_bytes_val)

    encoding = (metadata.get("optimization") or {}).get("prompt_encoding") or {}
    legacy_prompt_bytes: int | None = None
    legacy_value = encoding.get("legacy_v1_bundle_bytes")
    if isinstance(legacy_value, (int, float)) and legacy_value > 0:
        legacy_prompt_bytes = int(legacy_value)
    prompt_encoding_delta_bytes: int | None = None
    if legacy_prompt_bytes is not None and delivered_prompt_bytes is not None:
        prompt_encoding_delta_bytes = delivered_prompt_bytes - legacy_prompt_bytes

    serialized_envelope_bytes: int | None = None
    if (selected_source_bytes is not None
            and delivered_prompt_bytes is not None):
        serialized_envelope_bytes = max(
            0, delivered_prompt_bytes - selected_source_bytes
        )

    # -- Token / cost populations -------------------------------------------
    usage_data: dict[str, Any] = usage or {}

    input_tokens: int | None = _opt_int(usage_data.get("input_tokens"))
    output_tokens: int | None = _opt_int(usage_data.get("output_tokens"))
    cost_usd: float | None = _opt_float(usage_data.get("cost_usd"))

    cache_read: int = int(
        usage_data.get("cache_read_input_tokens")
        or usage_data.get("cached_input_tokens")
        or 0
    )
    cache_create: int = int(usage_data.get("cache_creation_input_tokens") or 0)
    openai_cached: int = int(usage_data.get("cached_input_tokens") or 0)

    explicit_cache_observed = usage_data.get("cache_metrics_observed")
    if isinstance(explicit_cache_observed, bool):
        cache_observed = explicit_cache_observed
    else:
        cache_observed = any(
            key in usage_data
            for key in (
                "cache_read_input_tokens",
                "cached_input_tokens",
                "cache_creation_input_tokens",
            )
        )

    # Claude reports cache fields as DISJOINT (add to input_tokens).
    # OpenAI reports cached_input_tokens as a SUBSET of input_tokens.
    is_claude = (adapter_id == "claude_cli")
    cached_total: int | None = None
    cache_eligible_input_tokens: int | None = None
    cache_metric_valid: bool | None = None
    cache_invalid_reason = ""
    if cache_observed and input_tokens is not None:
        # Cache creation is a write, never a hit. Claude's input/cache fields
        # are disjoint, whereas OpenAI's cached tokens are a subset of input.
        cached_total = cache_read if is_claude else openai_cached
        cache_eligible_input_tokens = (
            input_tokens + cache_read + cache_create
            if is_claude else input_tokens
        )
        cache_metric_valid = (
            cached_total >= 0
            and cache_eligible_input_tokens > 0
            and cached_total <= cache_eligible_input_tokens
        )
        if not cache_metric_valid:
            cache_invalid_reason = "cached_tokens_exceed_eligible_input"

    # -- Ratios (only when both operands measurable) ------------------------
    compression_ratio: float | None = None
    if (naive_discover_bytes is not None
            and selected_source_bytes is not None
            and selected_source_bytes > 0):
        compression_ratio = round(
            naive_discover_bytes / selected_source_bytes, 3
        )

    overhead_ratio: float | None = None
    if (serialized_envelope_bytes is not None
            and delivered_prompt_bytes is not None
            and delivered_prompt_bytes > 0):
        overhead_ratio = round(
            serialized_envelope_bytes / delivered_prompt_bytes, 4
        )

    prompt_encoding_reduction: float | None = None
    if legacy_prompt_bytes is not None and delivered_prompt_bytes is not None:
        prompt_encoding_reduction = round(
            1.0 - delivered_prompt_bytes / legacy_prompt_bytes,
            4,
        )

    cache_hit_rate: float | None = None
    if (cache_metric_valid is True
            and cache_eligible_input_tokens is not None
            and cached_total is not None):
        cache_hit_rate = round(
            cached_total / cache_eligible_input_tokens, 4
        )

    return {
        "schema_id": SCHEMA_ID,
        "task_id": task_id,
        "repo_id": repo_id,
        "runner": runner,
        "topic": topic,
        "task_type": task_type,
        "adapter_id": adapter_id,
        "outcome": outcome,
        "populations": {
            "naive_discover_bytes": naive_discover_bytes,
            "selected_source_bytes": selected_source_bytes,
            "serialized_envelope_bytes": serialized_envelope_bytes,
            "delivered_prompt_bytes": delivered_prompt_bytes,
            "legacy_v1_prompt_bytes": legacy_prompt_bytes,
            "prompt_encoding_delta_bytes": prompt_encoding_delta_bytes,
            "cached_input_tokens": cached_total,
            "cache_creation_input_tokens": (
                cache_create if is_claude and cache_observed else None
            ),
            "cache_eligible_input_tokens": cache_eligible_input_tokens,
            "cache_metric_valid": cache_metric_valid,
            "model_input_tokens": input_tokens,
            "model_output_tokens": output_tokens,
            "cost_usd": cost_usd,
        },
        "cache_semantics": {
            "provider_shape": (
                "claude_disjoint" if is_claude else "openai_subset"
            ),
            "cache_subset_of_input_tokens": not is_claude,
            "cache_creation_counts_as_hit": False,
            "measurement_observed": cache_observed,
            "measurement_valid": cache_metric_valid,
            "invalid_reason": cache_invalid_reason,
        },
        "ratios": {
            "compression_ratio_vs_naive_discover": compression_ratio,
            "envelope_overhead_ratio": overhead_ratio,
            "prompt_encoding_reduction": prompt_encoding_reduction,
            "cache_hit_rate_of_input": cache_hit_rate,
        },
        "notes": {
            "byte_labels": (
                "Byte fields are deterministic measurements, NOT token "
                "or dollar estimates."
            ),
            "null_means_not_measured": True,
            "naive_discover_definition": (
                "Sum of raw file bytes for read_first + allowed_writes paths, "
                "before Source Graph selection.  null when files not "
                "accessible or not measured."
            ),
            "selected_source_definition": (
                "Sum of tool output content bytes (source_graph + session + "
                "kb + ai_memory sections).  This is AFTER tool selection, "
                "NOT a raw repository estimate."
            ),
            "negative_savings_reported_honestly": True,
            "no_fabricated_10x_claims": True,
        },
    }


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def aggregate_context_economics(
    measurements: list[dict[str, Any]],
) -> dict[str, Any]:
    """Deterministic aggregation by repo, task type, adapter, outcome, runner.

    Cost per accepted/review-ready task and cache hit rate are computed
    without double-counting provider token fields.
    """

    by_repo: dict[str, dict[str, Any]] = defaultdict(_agg_zero)
    by_task_type: dict[str, dict[str, Any]] = defaultdict(_agg_zero)
    by_adapter: dict[str, dict[str, Any]] = defaultdict(_agg_zero)
    by_outcome: dict[str, dict[str, Any]] = defaultdict(_agg_zero)
    by_runner: dict[str, dict[str, Any]] = defaultdict(_agg_zero)

    total_tasks = 0
    accepted_tasks = 0
    review_ready_tasks = 0
    total_cost = 0.0
    total_in = 0
    total_out = 0
    total_cached = 0
    total_cache_eligible = 0
    cache_observed_tasks = 0
    cache_invalid_tasks = 0

    for m in measurements:
        pops: dict[str, Any] = m.get("populations") or {}
        repo = str(m.get("repo_id") or "unknown")
        ttype = str(m.get("task_type") or "unknown")
        adapter = str(m.get("adapter_id") or "unknown")
        outcome = str(m.get("outcome") or "unknown")
        runner = str(m.get("runner") or "unknown")

        cost = pops.get("cost_usd")
        inp = pops.get("model_input_tokens")
        out_tok = pops.get("model_output_tokens")
        cached = pops.get("cached_input_tokens")
        cache_eligible = pops.get("cache_eligible_input_tokens")
        cache_valid = pops.get("cache_metric_valid")

        total_tasks += 1
        if isinstance(cost, (int, float)):
            total_cost += float(cost)
        if isinstance(inp, (int, float)):
            total_in += int(inp)
        if isinstance(out_tok, (int, float)):
            total_out += int(out_tok)
        if cache_valid is True:
            cache_observed_tasks += 1
        elif cache_valid is False:
            cache_invalid_tasks += 1
        if cache_valid is True and isinstance(cached, (int, float)):
            total_cached += int(cached)
        if cache_valid is True and isinstance(cache_eligible, (int, float)):
            total_cache_eligible += int(cache_eligible)

        if outcome in ("accepted", "done", "SHIP"):
            accepted_tasks += 1
        if outcome in (
            "accepted", "done", "SHIP", "review_ready", "review-ready",
        ):
            review_ready_tasks += 1

        _agg_merge(by_repo[repo], pops, cost)
        _agg_merge(by_task_type[ttype], pops, cost)
        _agg_merge(by_adapter[adapter], pops, cost)
        _agg_merge(by_outcome[outcome], pops, cost)
        _agg_merge(by_runner[runner], pops, cost)

    return {
        "schema_id": SCHEMA_ID,
        "aggregation_label": (
            "deterministic_byte_and_token_aggregation_not_cost_truth"
        ),
        "task_totals": {
            "total_tasks": total_tasks,
            "accepted_tasks": accepted_tasks,
            "review_ready_tasks": review_ready_tasks,
        },
        "cost_totals": {
            "total_cost_usd": round(total_cost, 6),
            "total_input_tokens": total_in,
            "total_output_tokens": total_out,
            "total_cached_tokens": total_cached,
            "total_cache_eligible_input_tokens": total_cache_eligible,
            "cache_observed_tasks": cache_observed_tasks,
            "cache_invalid_tasks": cache_invalid_tasks,
        },
        "derived": {
            "cost_per_accepted_task_usd": (
                round(total_cost / accepted_tasks, 6)
                if accepted_tasks > 0 else None
            ),
            "cost_per_review_ready_task_usd": (
                round(total_cost / review_ready_tasks, 6)
                if review_ready_tasks > 0 else None
            ),
            "overall_cache_hit_rate": (
                round(total_cached / total_cache_eligible, 4)
                if total_cache_eligible > 0 else None
            ),
        },
        "by_repo": _dict_sorted(by_repo),
        "by_task_type": _dict_sorted(by_task_type),
        "by_adapter": _dict_sorted(by_adapter),
        "by_outcome": _dict_sorted(by_outcome),
        "by_runner": _dict_sorted(by_runner),
    }


# ---------------------------------------------------------------------------
# Before / after comparison
# ---------------------------------------------------------------------------

def compare_policies(
    *,
    label: str,
    before_measurements: list[dict[str, Any]],
    after_measurements: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compare two populations (e.g. old vs new Source Graph policy)."""

    before_agg = aggregate_context_economics(before_measurements)
    after_agg = aggregate_context_economics(after_measurements)

    b_cost: float = float(before_agg["cost_totals"]["total_cost_usd"])
    a_cost: float = float(after_agg["cost_totals"]["total_cost_usd"])
    b_acc: int = int(before_agg["task_totals"]["accepted_tasks"])
    a_acc: int = int(after_agg["task_totals"]["accepted_tasks"])

    cost_delta = round(a_cost - b_cost, 6)
    cost_pct: float | None = None
    if b_cost > 0:
        cost_pct = round(((a_cost - b_cost) / b_cost) * 100, 2)

    return {
        "schema_id": SCHEMA_ID,
        "comparison_label": label,
        "before": before_agg,
        "after": after_agg,
        "delta": {
            "cost_usd_delta": cost_delta,
            "cost_usd_change_pct": cost_pct,
            "accepted_tasks_delta": a_acc - b_acc,
        },
        "notes": {
            "byte_estimates_are_not_token_cost_truth": True,
            "baselines_null_when_not_measured": True,
        },
    }


def context_optimization_report(
    *,
    records: list[dict[str, Any]],
    before_bundle_bytes_field: str = "before_bundle_bytes",
    after_bundle_bytes_field: str = "after_bundle_bytes",
) -> dict[str, Any]:
    """Summarize measured context optimization without token fabrication."""

    comparable = [
        row for row in records
        if isinstance(row.get(before_bundle_bytes_field), (int, float))
        and isinstance(row.get(after_bundle_bytes_field), (int, float))
    ]
    before_bytes = sum(int(row[before_bundle_bytes_field]) for row in comparable)
    after_bytes = sum(int(row[after_bundle_bytes_field]) for row in comparable)
    cache_hits = sum(1 for row in records if row.get("cache_outcome") == "hit")
    cache_known = sum(1 for row in records if row.get("cache_outcome") in {"hit", "miss", "stale", "corrupt"})
    fallback_count = sum(1 for row in records if bool(row.get("discovery_fallback")))
    validation_known = [
        bool(row.get("validation_success"))
        for row in records
        if row.get("validation_success") is not None
    ]
    telemetry_rows = [
        row for row in records
        if any(
            isinstance((row.get("usage") or {}).get(key), (int, float))
            and (row.get("usage") or {}).get(key) > 0
            for key in ("input_tokens", "output_tokens", "cached_input_tokens", "cache_creation_input_tokens")
        )
    ]

    recorded_usage_totals = {
        "input_tokens": _sum_usage(telemetry_rows, "input_tokens"),
        "output_tokens": _sum_usage(telemetry_rows, "output_tokens"),
        "cached_input_tokens": _sum_usage(telemetry_rows, "cached_input_tokens"),
        "cache_creation_input_tokens": _sum_usage(telemetry_rows, "cache_creation_input_tokens"),
    } if telemetry_rows else None

    if not records:
        measurement_status = "inconclusive_no_records"
    elif not comparable:
        measurement_status = "inconclusive_no_comparable_records"
    else:
        measurement_status = "measured"

    return {
        "schema_id": SCHEMA_ID,
        "record_type": "context_optimization_report",
        "measurement_status": measurement_status,
        "record_count": len(records),
        "comparable_record_count": len(comparable),
        "before_bundle_bytes": before_bytes if comparable else None,
        "after_bundle_bytes": after_bytes if comparable else None,
        "bundle_byte_delta": (after_bytes - before_bytes) if comparable else None,
        "bundle_byte_change_ratio": (
            round(after_bytes / before_bytes, 4)
            if before_bytes > 0 else None
        ),
        "cache_hit_rate": (
            round(cache_hits / cache_known, 4)
            if cache_known > 0 else None
        ),
        "discovery_fallback_rate": (
            round(fallback_count / len(records), 4)
            if records else None
        ),
        "validation_success_rate": (
            round(sum(1 for ok in validation_known if ok) / len(validation_known), 4)
            if validation_known else None
        ),
        "recorded_token_telemetry": {
            "provider_telemetry_rows": len(telemetry_rows),
            "tokens": recorded_usage_totals,
            "null_when_provider_telemetry_absent": True,
        },
        "notes": {
            "bundle_bytes_are_deterministic_bytes_not_tokens": True,
            "actual_tokens_reported_only_from_provider_telemetry": True,
            "byte_estimates_are_not_cost_truth": True,
        },
    }


# ---------------------------------------------------------------------------
# Dashboard-ready record
# ---------------------------------------------------------------------------

def dashboard_record(
    measurements: list[dict[str, Any]],
) -> dict[str, Any]:
    """Produce a bounded, dashboard-ready record from measurements."""
    agg = aggregate_context_economics(measurements)
    return {
        "schema_id": SCHEMA_ID,
        "record_type": "dashboard_ready",
        "summary": {
            "total_tasks": agg["task_totals"]["total_tasks"],
            "accepted_tasks": agg["task_totals"]["accepted_tasks"],
            "total_cost_usd": agg["cost_totals"]["total_cost_usd"],
            "cost_per_accepted_usd": (
                agg["derived"]["cost_per_accepted_task_usd"]
            ),
            "cost_per_review_ready_usd": (
                agg["derived"]["cost_per_review_ready_task_usd"]
            ),
            "overall_cache_hit_rate": (
                agg["derived"]["overall_cache_hit_rate"]
            ),
        },
        "by_adapter": agg["by_adapter"],
        "by_task_type": agg["by_task_type"],
        "by_outcome": agg["by_outcome"],
        "authority_flags": {
            "runtime_authority": False,
            "support_authority": False,
            "dashboard_mutation": False,
            "canonical_cost_ledger": False,
        },
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _opt_int(value: Any) -> int | None:
    """Convert to int, return None for falsy/zero-ish unknown."""
    if value is None:
        return None
    try:
        v = int(value)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def _opt_float(value: Any) -> float | None:
    """Convert to float, return None for falsy/zero-ish unknown."""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v > 0.0 else None


def _sum_usage(rows: list[dict[str, Any]], key: str) -> int:
    total = 0
    for row in rows:
        usage = row.get("usage") or {}
        value = usage.get(key)
        if isinstance(value, (int, float)) and value > 0:
            total += int(value)
    return total


def _agg_zero() -> dict[str, Any]:
    return {
        "task_count": 0,
        "total_cost_usd": 0.0,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_cached_tokens": 0,
        "total_cache_eligible_input_tokens": 0,
        "cache_observed_tasks": 0,
        "cache_invalid_tasks": 0,
        "total_delivered_bytes": 0,
        "total_selected_bytes": 0,
        "total_envelope_bytes": 0,
        "encoding_observed_tasks": 0,
        "total_legacy_v1_prompt_bytes": 0,
        "total_nested_v2_prompt_bytes": 0,
        "total_prompt_encoding_delta_bytes": 0,
    }


def _agg_merge(
    bucket: dict[str, Any],
    pops: dict[str, Any],
    cost: float | None,
) -> None:
    bucket["task_count"] += 1
    if cost is not None:
        bucket["total_cost_usd"] = round(
            bucket["total_cost_usd"] + float(cost), 6
        )
    cache_valid = pops.get("cache_metric_valid")
    if cache_valid is True:
        bucket["cache_observed_tasks"] += 1
    elif cache_valid is False:
        bucket["cache_invalid_tasks"] += 1
    for src_key, dst_key in [
        ("model_input_tokens", "total_input_tokens"),
        ("model_output_tokens", "total_output_tokens"),
        ("delivered_prompt_bytes", "total_delivered_bytes"),
        ("selected_source_bytes", "total_selected_bytes"),
        ("serialized_envelope_bytes", "total_envelope_bytes"),
    ]:
        val = pops.get(src_key)
        if isinstance(val, (int, float)):
            bucket[dst_key] += int(val)
    legacy_prompt = pops.get("legacy_v1_prompt_bytes")
    delivered_prompt = pops.get("delivered_prompt_bytes")
    if isinstance(legacy_prompt, (int, float)) and isinstance(
        delivered_prompt, (int, float)
    ):
        bucket["encoding_observed_tasks"] += 1
        bucket["total_legacy_v1_prompt_bytes"] += int(legacy_prompt)
        bucket["total_nested_v2_prompt_bytes"] += int(delivered_prompt)
        bucket["total_prompt_encoding_delta_bytes"] += int(
            delivered_prompt - legacy_prompt
        )
    if cache_valid is True:
        for src_key, dst_key in [
            ("cached_input_tokens", "total_cached_tokens"),
            ("cache_eligible_input_tokens", "total_cache_eligible_input_tokens"),
        ]:
            val = pops.get(src_key)
            if isinstance(val, (int, float)):
                bucket[dst_key] += int(val)


def _dict_sorted(d: dict[str, Any]) -> dict[str, Any]:
    return dict(sorted(d.items()))


__all__ = [
    "SCHEMA_ID",
    "aggregate_context_economics",
    "compare_policies",
    "context_optimization_report",
    "dashboard_record",
    "measure_context_delivery",
]
