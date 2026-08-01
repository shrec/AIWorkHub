"""Truthful, bounded KPI aggregation for the repository dashboard.

The dashboard process feed is intentionally bounded.  These helpers therefore
describe worker terminal outcomes observed in that window; they never promote
``review_ready`` into a manager acceptance claim and never infer token savings.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from typing import Any, Mapping


TERMINAL_STATES = frozenset(
    {
        "review_ready",
        "validation_failed",
        "launch_failed",
        "timed_out",
        "cancelled",
        "worker_failed",
        "scope_rejected",
        "blocked",
        "exited",
    }
)
NON_GREEN_STATES = TERMINAL_STATES - {"review_ready"}
MAX_DAILY_BUCKETS = 30


def _count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _rate(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round(100.0 * numerator / denominator, 1)


def _date_bucket(row: Mapping[str, Any]) -> str | None:
    raw = row.get("finished_at") or row.get("timestamp") or row.get("started_at")
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.date().isoformat()


def _latest_task_runs(process_report: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    latest: dict[str, Mapping[str, Any]] = {}
    anonymous: list[Mapping[str, Any]] = []
    for row in process_report.get("processes") or []:
        if not isinstance(row, Mapping):
            continue
        task_id = str(row.get("task_id") or "").strip()
        if task_id:
            if task_id not in latest:
                latest[task_id] = row
        else:
            anonymous.append(row)
    return [*latest.values(), *anonymous]


def build_kpi_snapshot(
    *,
    process_report: Mapping[str, Any],
    source_graph: Mapping[str, Any],
    project_context: Mapping[str, Any],
    callback_health: Mapping[str, Any],
    cost_totals: Mapping[str, Any],
    process_limit: int,
) -> dict[str, Any]:
    """Return bounded charts and denominators from canonical snapshot inputs."""
    runs = _latest_task_runs(process_report)
    outcome_counts = Counter(
        str(row.get("state") or "unknown") for row in runs
    )
    accepted = outcome_counts["accepted"]
    rejected = outcome_counts["rejected"]
    manager_decisions = accepted + rejected
    terminal_runs = sum(outcome_counts[state] for state in TERMINAL_STATES)
    review_ready = outcome_counts["review_ready"]
    validation_failed = outcome_counts["validation_failed"]
    non_green = sum(outcome_counts[state] for state in NON_GREEN_STATES)

    daily: dict[str, dict[str, int]] = defaultdict(
        lambda: {
            "runs": 0,
            "terminal": 0,
            "review_ready": 0,
            "validation_failed": 0,
            "other_non_green": 0,
            "source_graph_live_tasks": 0,
            "source_graph_calls": 0,
            "policy_violations": 0,
        }
    )
    invalid_timestamps = 0
    adapters: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "runs": 0,
            "terminal_runs": 0,
            "review_ready_runs": 0,
            "validation_failed_runs": 0,
            "other_non_green_runs": 0,
            "source_graph_live_tasks": 0,
            "source_graph_calls": 0,
        }
    )
    topics: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"runs": 0, "terminal_runs": 0, "review_ready_runs": 0,
                 "validation_failed_runs": 0, "source_graph_live_tasks": 0}
    )
    usage_cohorts: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"runs": 0, "terminal_runs": 0, "review_ready_runs": 0,
                 "validation_failed_runs": 0}
    )
    economics = {
        "measured_tasks": 0,
        "raw_context_bytes": 0,
        "delivered_bundle_bytes": 0,
        "estimated_context_bytes_avoided": 0,
    }
    for row in runs:
        state = str(row.get("state") or "unknown")
        adapter = str(row.get("adapter_id") or "unknown")[:120]
        adapter_row = adapters[adapter]
        adapter_row["runs"] += 1
        if state in TERMINAL_STATES:
            adapter_row["terminal_runs"] += 1
        if state == "review_ready":
            adapter_row["review_ready_runs"] += 1
        elif state == "validation_failed":
            adapter_row["validation_failed_runs"] += 1
        elif state in NON_GREEN_STATES:
            adapter_row["other_non_green_runs"] += 1

        infra = row.get("ai_infra_context")
        tool_use = infra.get("tool_use") if isinstance(infra, Mapping) else None
        live_calls = (
            _count(tool_use.get("source_graph_live_calls"))
            if isinstance(tool_use, Mapping)
            else 0
        )
        calls = _count(tool_use.get("source_graph_calls")) if isinstance(tool_use, Mapping) else 0
        violations = (
            _count(tool_use.get("policy_violations"))
            if isinstance(tool_use, Mapping)
            else 0
        )
        adapter_row["source_graph_calls"] += calls
        if live_calls:
            adapter_row["source_graph_live_tasks"] += 1

        topic = str(row.get("topic") or "unknown")[:120]
        topic_row = topics[topic]
        topic_row["runs"] += 1
        if state in TERMINAL_STATES:
            topic_row["terminal_runs"] += 1
        if state == "review_ready":
            topic_row["review_ready_runs"] += 1
        elif state == "validation_failed":
            topic_row["validation_failed_runs"] += 1
        if live_calls:
            topic_row["source_graph_live_tasks"] += 1

        stage_counts = tool_use.get("source_graph_stage_counts") if isinstance(tool_use, Mapping) else {}
        attributed_stages = {
            str(stage) for stage, count in (stage_counts.items() if isinstance(stage_counts, Mapping) else [])
            if stage != "unspecified" and _count(count) > 0
        }
        satisfaction = str(tool_use.get("source_graph_satisfaction") or "") if isinstance(tool_use, Mapping) else ""
        if live_calls and len(attributed_stages) >= 2:
            cohort_name = "continuous_use"
        elif live_calls:
            cohort_name = "live_single_stage"
        elif satisfaction == "injected_receipt":
            cohort_name = "injected_only"
        elif calls:
            cohort_name = "stale_cached_or_zero_hit"
        else:
            cohort_name = "missing"
        cohort = usage_cohorts[cohort_name]
        cohort["runs"] += 1
        if state in TERMINAL_STATES:
            cohort["terminal_runs"] += 1
        if state == "review_ready":
            cohort["review_ready_runs"] += 1
        elif state == "validation_failed":
            cohort["validation_failed_runs"] += 1

        estimate = infra.get("estimate") if isinstance(infra, Mapping) else None
        if isinstance(estimate, Mapping):
            raw_bytes = _count(estimate.get("raw_context_bytes"))
            bundle_bytes = _count(estimate.get("bundle_bytes"))
            if raw_bytes > 0:
                economics["measured_tasks"] += 1
                economics["raw_context_bytes"] += raw_bytes
                economics["delivered_bundle_bytes"] += bundle_bytes
                economics["estimated_context_bytes_avoided"] += max(
                    0, raw_bytes - bundle_bytes
                )

        day = _date_bucket(row)
        if day is None:
            invalid_timestamps += 1
            continue
        bucket = daily[day]
        bucket["runs"] += 1
        if state in TERMINAL_STATES:
            bucket["terminal"] += 1
        if state == "review_ready":
            bucket["review_ready"] += 1
        elif state == "validation_failed":
            bucket["validation_failed"] += 1
        elif state in NON_GREEN_STATES:
            bucket["other_non_green"] += 1
        if live_calls:
            bucket["source_graph_live_tasks"] += 1
        bucket["source_graph_calls"] += calls
        bucket["policy_violations"] += violations

    adapter_rows = []
    for name, values in adapters.items():
        row = {"name": name, **values}
        row["review_ready_rate"] = _rate(
            values["review_ready_runs"], values["terminal_runs"]
        )
        row["source_graph_live_rate"] = _rate(
            values["source_graph_live_tasks"], values["runs"]
        )
        adapter_rows.append(row)
    adapter_rows.sort(key=lambda row: (-row["runs"], row["name"].lower()))

    topic_rows = []
    for name, values in topics.items():
        row = {"name": name, **values}
        row["review_ready_rate"] = _rate(values["review_ready_runs"], values["terminal_runs"])
        row["source_graph_live_rate"] = _rate(values["source_graph_live_tasks"], values["runs"])
        topic_rows.append(row)
    topic_rows.sort(key=lambda row: (-row["runs"], row["name"].lower()))

    cohort_rows = []
    for name, values in usage_cohorts.items():
        row = {"name": name, **values}
        row["review_ready_rate"] = _rate(values["review_ready_runs"], values["terminal_runs"])
        cohort_rows.append(row)
    cohort_rows.sort(key=lambda row: (-row["runs"], row["name"]))

    callback_states = callback_health.get("by_state")
    callback_states = callback_states if isinstance(callback_states, Mapping) else {}
    delivered = _count(callback_states.get("delivered"))
    dead_letter = _count(callback_states.get("dead_letter"))
    callback_terminal = delivered + dead_letter

    source_calls = _count(source_graph.get("source_graph_calls"))
    source_failed = _count(source_graph.get("source_graph_failed_calls"))
    source_successful = max(0, source_calls - source_failed)
    mode_attributed = _count(source_graph.get("source_graph_mode_attributed_calls"))
    stage_attributed = _count(source_graph.get("source_graph_stage_attributed_calls"))
    latency = source_graph.get("source_graph_latency")
    latency = latency if isinstance(latency, Mapping) else {}

    mode_rows = [
        {"name": str(name), "calls": _count(count)}
        for name, count in (source_graph.get("source_graph_mode_counts") or {}).items()
        if _count(count) > 0
    ] if isinstance(source_graph.get("source_graph_mode_counts"), Mapping) else []
    mode_rows.sort(key=lambda row: (-row["calls"], row["name"]))
    stage_rows = [
        {"name": str(name), "calls": _count(count)}
        for name, count in (source_graph.get("source_graph_stage_counts") or {}).items()
        if _count(count) > 0
    ] if isinstance(source_graph.get("source_graph_stage_counts"), Mapping) else []
    stage_rows.sort(key=lambda row: (-row["calls"], row["name"]))

    context_rows = []
    for key, label in (
        ("session_current_state", "Session Manager"),
        ("ai_memory", "AI Memory"),
        ("kb", "Knowledge Base"),
    ):
        source = project_context.get(key)
        source = source if isinstance(source, Mapping) else {}
        requested = _count(source.get("requested_tasks"))
        executed = _count(source.get("executed_tasks"))
        context_rows.append(
            {
                "key": key,
                "label": label,
                "requested_tasks": requested,
                "executed_tasks": executed,
                "execution_rate": _rate(executed, requested),
                "hit_count": _count(source.get("hit_count")),
                "bytes": _count(source.get("bytes")),
                "degraded_tasks": _count(source.get("degraded_tasks")),
            }
        )

    observed = len(runs)
    total_requests = _count(process_report.get("total_requests"))
    truncated = bool(process_report.get("truncated")) or total_requests > observed
    ordered_days = sorted(daily)[-MAX_DAILY_BUCKETS:]
    daily_rows = [{"date": day, **daily[day]} for day in ordered_days]

    raw_context_bytes = economics["raw_context_bytes"]
    context_compression_rate = (
        round(
            100.0 * economics["estimated_context_bytes_avoided"] / raw_context_bytes,
            1,
        )
        if raw_context_bytes > 0 else None
    )
    economics.update({
        "context_compression_rate": context_compression_rate,
        "measurement_label": "declared_raw_context_paths_vs_delivered_project_context_bundle_bytes",
        "token_savings_available": False,
        "token_savings_reason": "no_tokenizer_bound_counterfactual_baseline",
    })

    return {
        "schema_id": "aiworkhub.kpi.dashboard.v2",
        "measurement": "bounded_worker_outcomes_and_explicit_manager_decisions",
        "window": {
            "label": f"latest {process_limit} process runs",
            "limit": process_limit,
            "observed_runs": observed,
            "total_requests": total_requests,
            "truncated": truncated,
            "daily_bucket_limit": MAX_DAILY_BUCKETS,
        },
        "headline": {
            "manager_decisions": manager_decisions,
            "accepted_runs": accepted,
            "rejected_runs": rejected,
            "manager_acceptance_rate": _rate(accepted, manager_decisions),
            "terminal_runs": terminal_runs,
            "review_ready_runs": review_ready,
            "review_ready_rate": _rate(review_ready, terminal_runs),
            "validation_failed_runs": validation_failed,
            "validation_failed_rate": _rate(validation_failed, terminal_runs),
            "other_non_green_runs": max(0, non_green - validation_failed),
            "source_graph_live_rate": source_graph.get("live_rate"),
            "source_graph_gate_satisfaction_rate": source_graph.get("gate_satisfaction_rate"),
            "source_graph_useful_call_rate": _rate(source_successful, source_calls),
            "source_graph_mode_attribution_rate": _rate(mode_attributed, source_calls),
            "source_graph_stage_attribution_rate": _rate(stage_attributed, source_calls),
            "source_graph_latency_p50_ms": latency.get("p50_ms"),
            "source_graph_latency_p95_ms": latency.get("p95_ms"),
            "context_compression_rate": context_compression_rate,
            "estimated_context_bytes_avoided": economics["estimated_context_bytes_avoided"],
            "callback_delivery_rate": _rate(delivered, callback_terminal),
            "callback_backlog": _count(callback_health.get("backlog_count")),
            "callback_retries": _count(callback_health.get("retry_count")),
            "callback_dead_letters": dead_letter,
            "total_tokens": _count(cost_totals.get("total_tokens")),
            "cost_usd": round(float(cost_totals.get("cost_usd") or 0.0), 6),
        },
        "outcome_mix": [
            {"state": state, "count": count}
            for state, count in sorted(
                outcome_counts.items(), key=lambda item: (-item[1], item[0])
            )
        ],
        "daily": daily_rows,
        "adapters": adapter_rows,
        "topics": topic_rows[:12],
        "source_graph_modes": mode_rows,
        "source_graph_stages": stage_rows,
        "tool_use_cohorts": cohort_rows,
        "economics": economics,
        "context": context_rows,
        "data_quality": {
            "acceptance_rate_available": manager_decisions > 0,
            "acceptance_rate_reason": (
                "bounded_explicit_manager_decisions_only"
                if manager_decisions
                else "no_explicit_manager_decisions_in_bounded_snapshot"
            ),
            "process_window_truncated": truncated,
            "invalid_timestamp_rows": invalid_timestamps,
            "source_graph_unattributed_calls": max(0, source_calls - mode_attributed),
            "source_graph_stage_unattributed_calls": max(0, source_calls - stage_attributed),
            "source_graph_latency_samples": _count(latency.get("count")),
            "source_graph_latency_samples_truncated": bool(latency.get("samples_truncated")),
            "sample_size": observed,
        },
    }
