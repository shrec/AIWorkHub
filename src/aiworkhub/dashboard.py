"""Canonical read-only builders for the AIWorkHub dashboard.

The native VS Code Webview (see ``vscode-extension/extension.js``) talks to
this package over the repository-local Task MCP stdio session via
``dashboard_mcp_app`` -- there is no HTTP listener in this module. This file
owns only the pure, repository-bound snapshot/detail builders that
``dashboard_mcp_app`` (and any other in-process consumer) calls directly.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections import defaultdict
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any, Callable, Mapping

from aiworkhub import (
    completion_inbox,
    core,
    cost_ledger,
    dashboard_kpis,
    deepseek_credentials,
    process_launcher,
    repo_policy,
    storage_observability,
    task_plan,
    task_store,
    workforce_catalog,
)


DEFAULT_TASK_LIMIT = 500
DEFAULT_PROCESS_LIMIT = 200
DEFAULT_SNAPSHOT_PROCESS_LIMIT = 50
DEFAULT_KPI_HISTORY_PROCESS_LIMIT = 1000
MAX_PROCESS_LOG_BYTES = 4 * 1024 * 1024
MAX_KPI_HISTORY_LOG_BYTES = 16 * 1024 * 1024
# Informational observability boundary, not an inactivity or policy verdict.
# A worker may legitimately spend this long testing or reasoning between two
# authenticated Source Graph calls, so the dashboard always exposes the
# denominator and the interpretation label alongside the alert count.
SOURCE_GRAPH_LONG_CALL_GAP_SECONDS = 15 * 60
ACTIVE_STATUSES = ("pending", "processing", "review")
# The full canonical-status taxonomy (AITools.taskdb.canonical_status), used
# for exact whole-queue totals -- independent of any bounded row limit.
ALL_CANONICAL_STATUSES = ("pending", "processing", "review", "blocked", "finished", "archived")

_LIST_LINE_RE = re.compile(
    r"^\s*\[(?P<status>[^\]]+)\]\s*"
    r"\[(?P<topic>[^\]]+)\]\s*"
    r"\[(?P<runner>[^\]]+)\]\s*(?P<task_id>\S+)"
    r"(?:\s+model=(?P<model>\S+))?"
    r"(?:\s+outcome=(?P<outcome>\S+))?\s*$"
)
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
_PORTABLE_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_HOST_PATH_IN_TEXT_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:[A-Za-z]:[\\/]|/)[^\s\"'`<>]+"
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[_-]?key|authorization|credential|password|secret|token)"
    r"\s*([:=])\s*([^\s,;]+)"
)
_PROCESS_RUN_FIELDS = {
    "request_id",
    "task_id",
    "runner",
    "topic",
    "adapter_id",
    "model",
    "state",
    "pid",
    "pid_start_ticks",
    "started_at",
    "finished_at",
    "timestamp",
    "exit_code",
    "blocked_reason",
    "error",
    "reason",
    "timeout_seconds",
    "workspace_isolated",
    "sandbox_backend",
    "usage_recorded",
    "usage_error",
}


def _portable_text(value: Any, limit: int) -> str:
    """Return bounded review text without host paths or obvious secrets.

    Evidence bundles are intended to move between machines.  They therefore
    retain useful summaries and commands, but never preserve host-local
    absolute paths or credential assignments copied from provider output.
    """
    text = _PORTABLE_CONTROL_RE.sub("", str(value or ""))
    text = _SECRET_ASSIGNMENT_RE.sub(r"\1\2<redacted>", text)
    text = _HOST_PATH_IN_TEXT_RE.sub("<host-path>", text)
    return text[: max(0, int(limit))]


def _portable_path(value: Any, limit: int = 500) -> str:
    text = _PORTABLE_CONTROL_RE.sub("", str(value or "")).strip()
    if text.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", text):
        name = re.split(r"[\\/]", text.rstrip("/\\"))[-1]
        text = f"<host-path>/{name}" if name else "<host-path>"
    return text[: max(0, int(limit))]


def _bounded_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _compact_ai_infra(event: Mapping[str, Any]) -> dict[str, Any]:
    context = event.get("project_context")
    delivery = event.get("project_context_delivery")
    ack = event.get("project_context_acknowledgement")
    gate = event.get("worker_mcp_gate")
    prompt_budget = event.get("prompt_budget")
    provider_denials = event.get("provider_tool_denials")
    read_efficiency = event.get("read_efficiency")
    semantic_edit = event.get("semantic_edit")
    usage = event.get("usage")
    if (
        not isinstance(context, Mapping)
        and not isinstance(delivery, Mapping)
        and not isinstance(ack, Mapping)
        and not isinstance(gate, Mapping)
        and not isinstance(prompt_budget, Mapping)
        and not isinstance(provider_denials, Mapping)
        and not isinstance(read_efficiency, Mapping)
        and not isinstance(semantic_edit, Mapping)
        and not isinstance(usage, Mapping)
    ):
        return {}

    by_name: dict[str, dict[str, Any]] = {}
    if isinstance(context, Mapping):
        for section in context.get("sections") or []:
            if not isinstance(section, Mapping):
                continue
            name = str(section.get("name") or "")[:64]
            if not name:
                continue
            by_name[name] = {
                "requested": bool(section.get("requested")),
                "executed": bool(section.get("executed")),
                "hit_count": int(section.get("hit_count") or 0),
                "bytes": int(section.get("bytes") or 0),
                "sha256": str(section.get("sha256") or "")[:80],
                "truncated": bool(section.get("truncated")),
                "degraded_reason": str(section.get("degraded_reason") or "")[:180],
            }
    estimate = {}
    if isinstance(context, Mapping):
        raw_estimate = context.get("estimated_raw_context_vs_bundle_bytes")
        if isinstance(raw_estimate, Mapping):
            pre_optimization_bytes = raw_estimate.get(
                "pre_optimization_section_bytes"
            )
            if pre_optimization_bytes is None:
                pre_optimization_bytes = raw_estimate.get("raw_context_bytes")
            optimized_section_bytes = raw_estimate.get("optimized_section_bytes")
            if optimized_section_bytes is None:
                optimized_section_bytes = raw_estimate.get("optimized_context_bytes")
            optimized_section_bytes_observed = optimized_section_bytes is not None
            estimate = {
                "label": str(raw_estimate.get("label") or "")[:80],
                "population_definition": str(
                    raw_estimate.get("population_definition") or (
                        "tool_section_payload_before_optional_suppression_not_"
                        "raw_repository_files_or_counterfactual_reads"
                    )
                )[:180],
                "pre_optimization_section_bytes": int(
                    pre_optimization_bytes or 0
                ),
                "optimized_section_bytes": int(optimized_section_bytes or 0),
                "optimized_section_bytes_observed": (
                    optimized_section_bytes_observed
                ),
                # Compatibility alias for historical dashboard consumers.
                "raw_context_bytes": int(pre_optimization_bytes or 0),
                "bundle_bytes": int(raw_estimate.get("bundle_bytes") or 0),
                "optimization_delta_bytes": int(
                    raw_estimate.get("optimization_delta_bytes") or 0
                ),
                "envelope_delta_bytes": int(
                    raw_estimate.get("envelope_delta_bytes") or 0
                ),
                "delta_bytes": int(raw_estimate.get("delta_bytes") or 0),
                "raw_file_counterfactual_available": bool(
                    raw_estimate.get("raw_file_counterfactual_available")
                ),
                "token_savings_available": False,
            }

    tool_use: dict[str, Any] = {}
    if isinstance(gate, Mapping):
        verification = gate.get("verification")
        if not isinstance(verification, Mapping):
            verification = {}
        calls = verification.get("call_count_by_tool")
        successful = verification.get("successful_call_count_by_tool")
        bytes_by_tool = verification.get("bounded_bytes_by_tool")
        cache_by_tool = verification.get("cache_hits_by_tool")
        satisfaction = gate.get("satisfaction_by_tool")
        def bounded_counter(value: Any) -> dict[str, int]:
            if not isinstance(value, Mapping):
                return {}
            return {
                str(name)[:64]: max(0, int(count or 0))
                for name, count in list(value.items())[:32]
            }
        tool_use = {
            "gated": bool(gate.get("gated")),
            "satisfied": bool(gate.get("satisfied", True)),
            "reason": str(gate.get("reason") or "")[:240],
            "source_graph_satisfaction": str(
                satisfaction.get("source_graph") if isinstance(satisfaction, Mapping) else ""
            )[:80],
            "source_graph_calls": int(calls.get("source_graph") or 0)
            if isinstance(calls, Mapping) else 0,
            "source_graph_successful_calls": int(successful.get("source_graph") or 0)
            if isinstance(successful, Mapping) else 0,
            "source_graph_fresh_calls": int(
                verification.get("fresh_source_graph_calls") or 0
            ),
            "source_graph_live_calls": int(verification.get("live_source_graph_calls") or 0),
            "source_graph_hit_count": int(verification.get("source_graph_hit_count") or 0),
            "source_graph_zero_hit_calls": int(
                verification.get("source_graph_zero_hit_calls") or 0
            ),
            "source_graph_failed_calls": int(
                verification.get("source_graph_failed_calls") or 0
            ),
            "source_graph_bytes": int(bytes_by_tool.get("source_graph") or 0)
            if isinstance(bytes_by_tool, Mapping) else 0,
            "source_graph_cache_hits": int(cache_by_tool.get("source_graph") or 0)
            if isinstance(cache_by_tool, Mapping) else 0,
            "source_graph_mode_counts": dict(
                verification.get("source_graph_mode_counts") or {}
            ),
            "source_graph_mode_sequence": list(
                verification.get("source_graph_mode_sequence") or []
            )[:64],
            "source_graph_stage_counts": bounded_counter(
                verification.get("source_graph_stage_counts")
            ),
            "source_graph_stage_sequence": [
                str(value)[:32]
                for value in (verification.get("source_graph_stage_sequence") or [])[:64]
            ],
            "source_graph_mode_stage_counts": {
                str(stage)[:32]: bounded_counter(counts)
                for stage, counts in list(
                    (verification.get("source_graph_mode_stage_counts") or {}).items()
                )[:8]
                if isinstance(counts, Mapping)
            },
            "source_graph_stage_attributed_calls": int(
                verification.get("source_graph_stage_attributed_calls") or 0
            ),
            "source_graph_latency": {
                "count": int((verification.get("source_graph_latency") or {}).get("count") or 0),
                "total_ms": float((verification.get("source_graph_latency") or {}).get("total_ms") or 0.0),
                "samples_ms": [
                    round(float(value), 3)
                    for value in (
                        (verification.get("source_graph_latency") or {}).get("samples_ms") or []
                    )[:64]
                    if isinstance(value, (int, float)) and 0 <= float(value) <= 3_600_000
                ],
                "samples_truncated": bool(
                    (verification.get("source_graph_latency") or {}).get("samples_truncated")
                ),
            },
            "source_graph_call_gaps": {
                "count": int((verification.get("source_graph_call_gaps") or {}).get("count") or 0),
                "total_seconds": float(
                    (verification.get("source_graph_call_gaps") or {}).get("total_seconds") or 0.0
                ),
                "samples_seconds": [
                    round(float(value), 3)
                    for value in (
                        (verification.get("source_graph_call_gaps") or {}).get("samples_seconds") or []
                    )[:64]
                    if isinstance(value, (int, float)) and 0 <= float(value) <= 31_536_000
                ],
                "samples_truncated": bool(
                    (verification.get("source_graph_call_gaps") or {}).get("samples_truncated")
                ),
            },
            "source_graph_evidence_rows": bounded_counter(
                verification.get("source_graph_evidence_rows")
            ),
            "source_graph_index_revision_counts": bounded_counter(
                verification.get("source_graph_index_revision_counts")
            ),
            "source_graph_index_sequence": [
                {
                    "revision": str(value.get("revision") or "")[:96],
                    "finished_at": str(value.get("finished_at") or "")[:64],
                }
                for value in (verification.get("source_graph_index_sequence") or [])[:64]
                if isinstance(value, Mapping)
            ],
            "call_count_by_tool": bounded_counter(calls),
            "successful_call_count_by_tool": bounded_counter(successful),
            "bounded_bytes_by_tool": bounded_counter(bytes_by_tool),
            "cache_hits_by_tool": bounded_counter(cache_by_tool),
            "policy_violations": int(verification.get("policy_violations") or 0),
            "entries_verified": int(verification.get("entries_verified") or 0),
            "entries_tampered": int(verification.get("entries_tampered") or 0),
        }

    return {
        "source_graph": by_name.get("source_graph", {}),
        "session_current_state": by_name.get("session_current_state", {}),
        "ai_memory": by_name.get("ai_memory", {}),
        "kb": by_name.get("kb", {}),
        "injected": bool(isinstance(delivery, Mapping) and delivery.get("injected")),
        "acknowledged": bool(isinstance(ack, Mapping) and ack.get("acknowledged")),
        "delivery": {
            "bundle_sha256": str(delivery.get("bundle_sha256") or "")[:80]
            if isinstance(delivery, Mapping) else "",
            "prompt_sha256": str(delivery.get("prompt_sha256") or "")[:80]
            if isinstance(delivery, Mapping) else "",
            "bundle_bytes": int(delivery.get("bundle_bytes") or 0)
            if isinstance(delivery, Mapping) else 0,
        },
        "acknowledgement": {
            "bundle_sha256": str(ack.get("bundle_sha256") or "")[:80]
            if isinstance(ack, Mapping) else "",
            "prompt_sha256": str(ack.get("prompt_sha256") or "")[:80]
            if isinstance(ack, Mapping) else "",
            "reason": str(ack.get("reason") or "")[:160] if isinstance(ack, Mapping) else "",
        },
        "estimate": estimate,
        "prompt_budget": {
            "schema_id": str(prompt_budget.get("schema_id") or "")[:96],
            "mode": str(prompt_budget.get("mode") or "")[:32],
            "total_bytes": _bounded_int(prompt_budget.get("total_bytes")),
            "max_bytes": _bounded_int(prompt_budget.get("max_bytes")),
            "remaining_bytes": _bounded_int(prompt_budget.get("remaining_bytes")),
            "utilization_percent": float(prompt_budget.get("utilization_percent") or 0.0),
            "delta_rework": bool(prompt_budget.get("delta_rework")),
            "byte_labels_are_token_truth": False,
            "sections": {
                str(name)[:64]: _bounded_int(value)
                for name, value in list(
                    (prompt_budget.get("sections") or {}).items()
                )[:16]
            } if isinstance(prompt_budget.get("sections"), Mapping) else {},
        } if isinstance(prompt_budget, Mapping) else {},
        "provider_tool_denials": {
            "evidence_observed": bool(provider_denials.get("evidence_observed")),
            "permission_denials_total": int(
                provider_denials.get("permission_denials_total") or 0
            ),
            "raw_discovery_denials": int(
                provider_denials.get("raw_discovery_denials") or 0
            ),
            "raw_discovery_labels": [
                str(value)[:32]
                for value in (provider_denials.get("raw_discovery_labels") or [])[:16]
            ],
        } if isinstance(provider_denials, Mapping) else {},
        "read_efficiency": {
            "schema_id": str(read_efficiency.get("schema_id") or "")[:96],
            "evidence_observed": bool(read_efficiency.get("evidence_observed")),
            "provider_records_scanned": _bounded_int(
                read_efficiency.get("provider_records_scanned")
            ),
            "recognized_read_events": _bounded_int(
                read_efficiency.get("recognized_read_events")
            ),
            "recognized_source_graph_events": _bounded_int(
                read_efficiency.get("recognized_source_graph_events")
            ),
            **{
                key: _bounded_int(read_efficiency.get(key))
                for key in (
                    "total_reads", "known_repetitions", "unknown_repetitions",
                    "exact_rereads", "overlap_rereads", "bounded_reads",
                    "unbounded_reads", "explicit_source_graph_associations",
                    "derived_temporal_associations", "read_bytes_observed",
                    "total_read_bytes", "bounded_read_bytes",
                    "unbounded_read_bytes", "exact_reread_bytes",
                    "overlap_reread_bytes", "unknown_repetition_bytes",
                )
            },
            "measurement_label": str(
                read_efficiency.get("measurement_label") or ""
            )[:120],
        } if isinstance(read_efficiency, Mapping) else {},
        "semantic_edit": {
            "schema_id": str(semantic_edit.get("schema_id") or "")[:96],
            "observed": bool(semantic_edit.get("observed")),
            **{
                key: _bounded_int(semantic_edit.get(key))
                for key in (
                    "file_count", "range_count", "file_bytes",
                    "old_region_bytes", "replacement_bytes",
                    "model_reemitted_old_bytes",
                )
            },
            # This receipt is structural byte evidence only. It cannot prove
            # provider-token or monetary savings without a paired baseline.
            "token_savings_claimed": False,
        } if isinstance(semantic_edit, Mapping) else {},
        "usage": {
            "input_tokens": _bounded_int(usage.get("input_tokens")),
            "output_tokens": _bounded_int(usage.get("output_tokens")),
            "cached_input_tokens": _bounded_int(usage.get("cached_input_tokens")),
            "cache_creation_input_tokens": _bounded_int(
                usage.get("cache_creation_input_tokens")
            ),
            "usage_observed": bool(usage.get("usage_observed")),
            "cache_metrics_observed": bool(usage.get("cache_metrics_observed")),
            "cost_usd": max(0.0, float(usage.get("cost_usd") or 0.0)),
            "cost_observed": bool(usage.get("cost_observed")),
        } if isinstance(usage, Mapping) else {},
        "tool_use": tool_use,
    }


def _merge_ai_infra(
    previous: Mapping[str, Any] | None,
    current: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Merge bounded process evidence without erasing prior event sections.

    A terminal usage event usually carries usage/acknowledgement but not the
    launch event's project-context sections.  Replacing the whole compact
    mapping made the latest row lose exactly the context evidence needed for
    truthful economics.  Empty current sections therefore never erase an
    earlier non-empty section from the same request.
    """

    merged = dict(previous or {})
    for key, value in (current or {}).items():
        if isinstance(value, Mapping) and not value:
            continue
        merged[key] = value
    return merged


def _source_graph_telemetry(process_report: Mapping[str, Any]) -> dict[str, Any]:
    """Aggregate authenticated worker Source Graph evidence by latest task run.

    One task is counted once even when it was retried.  ``live`` means a fresh,
    non-empty, non-cached worker call verified by the HMAC ledger.  An injected
    receipt is intentionally reported separately: it proves context delivery,
    not continued Source Graph use during execution.
    """
    latest_by_task: dict[str, Mapping[str, Any]] = {}
    for row in process_report.get("processes") or []:
        if not isinstance(row, Mapping):
            continue
        task_id = str(row.get("task_id") or "").strip()
        if task_id and task_id not in latest_by_task:
            latest_by_task[task_id] = row

    totals: dict[str, Any] = {
        "schema_id": "aiworkhub.source_graph.telemetry.v1",
        "observed_tasks": len(latest_by_task),
        "gated_tasks": 0,
        "satisfied_tasks": 0,
        "source_graph_any_tasks": 0,
        "source_graph_fresh_tasks": 0,
        "source_graph_live_tasks": 0,
        "source_graph_injected_only_tasks": 0,
        "source_graph_stale_or_cached_tasks": 0,
        "source_graph_missing_tasks": 0,
        "source_graph_calls": 0,
        "source_graph_fresh_calls": 0,
        "source_graph_live_calls": 0,
        "source_graph_hit_count": 0,
        "source_graph_zero_hit_calls": 0,
        "source_graph_failed_calls": 0,
        "source_graph_bytes": 0,
        "source_graph_cache_hits": 0,
        "source_graph_mode_counts": {},
        "source_graph_mode_sequence": [],
        "source_graph_mode_attributed_calls": 0,
        "source_graph_mode_eligible_calls": 0,
        "source_graph_mode_legacy_calls": 0,
        "source_graph_mode_unattributed_calls": 0,
        "source_graph_distinct_modes": 0,
        "source_graph_mode_attribution_rate": 0.0,
        "source_graph_stage_counts": {},
        "source_graph_stage_sequence": [],
        "source_graph_mode_stage_counts": {},
        "source_graph_stage_attributed_calls": 0,
        "source_graph_stage_eligible_calls": 0,
        "source_graph_stage_legacy_calls": 0,
        "source_graph_stage_unattributed_calls": 0,
        "source_graph_stage_attribution_rate": 0.0,
        "source_graph_latency": {
            "count": 0, "total_ms": 0.0, "min_ms": None, "max_ms": None,
            "p50_ms": None, "p95_ms": None, "samples_truncated": False,
        },
        "source_graph_call_gaps": {
            "count": 0, "total_seconds": 0.0, "min_seconds": None,
            "max_seconds": None, "p50_seconds": None, "p95_seconds": None,
            "long_gap_threshold_seconds": SOURCE_GRAPH_LONG_CALL_GAP_SECONDS,
            "long_gap_count": 0, "long_gap_rate": None,
            "alert_state": "not_available",
            "interpretation": "observed_inter_call_gap_not_model_inactivity",
            "samples_truncated": False,
        },
        "source_graph_evidence_rows": {
            "entity_rows": 0, "edge_rows": 0, "file_rows": 0,
        },
        "source_graph_index_revision_counts": {},
        "source_graph_index_sequence": [],
        "tool_call_counts": {},
        "tool_success_counts": {},
        "tool_bytes": {},
        "tool_cache_hits": {},
        "policy_violation_tasks": 0,
        "policy_violations": 0,
        "provider_permission_denials": 0,
        "raw_discovery_denials": 0,
        "raw_discovery_denial_tasks": 0,
        "provider_denial_evidence_tasks": 0,
        "tampered_ledger_tasks": 0,
        "blocked_reason_counts": {},
        "measurement_label": "authenticated_calls_and_returned_bytes_only_no_token_or_cost_claim",
        "live_rate": 0.0,
        "fresh_rate": 0.0,
        "any_rate": 0.0,
        "gate_satisfaction_rate": 0.0,
        "by_adapter": {},
    }

    def bucket_for(adapter: str) -> dict[str, Any]:
        buckets = totals["by_adapter"]
        if adapter not in buckets:
            buckets[adapter] = {
                "gated_tasks": 0,
                "live_tasks": 0,
                "injected_only_tasks": 0,
                "missing_or_stale_tasks": 0,
                "source_graph_calls": 0,
                "source_graph_fresh_calls": 0,
                "source_graph_hit_count": 0,
                "source_graph_zero_hit_calls": 0,
                "source_graph_failed_calls": 0,
                "source_graph_mode_counts": {},
                "source_graph_stage_counts": {},
                "source_graph_mode_attributed_calls": 0,
                "source_graph_mode_unattributed_calls": 0,
                "policy_violations": 0,
                "provider_permission_denials": 0,
                "raw_discovery_denials": 0,
                "tool_call_counts": {},
                "tool_success_counts": {},
            }
        return buckets[adapter]

    for row in latest_by_task.values():
        infra = row.get("ai_infra_context")
        tool_use = infra.get("tool_use") if isinstance(infra, Mapping) else None
        if not isinstance(tool_use, Mapping) or not tool_use.get("gated"):
            continue
        totals["gated_tasks"] += 1
        adapter = str(row.get("adapter_id") or "unknown")[:120]
        bucket = bucket_for(adapter)
        bucket["gated_tasks"] += 1
        provider_denials = infra.get("provider_tool_denials")
        if isinstance(provider_denials, Mapping):
            permission_denials = int(
                provider_denials.get("permission_denials_total") or 0
            )
            raw_denials = int(provider_denials.get("raw_discovery_denials") or 0)
            totals["provider_permission_denials"] += permission_denials
            totals["raw_discovery_denials"] += raw_denials
            bucket["provider_permission_denials"] += permission_denials
            bucket["raw_discovery_denials"] += raw_denials
            if provider_denials.get("evidence_observed"):
                totals["provider_denial_evidence_tasks"] += 1
            if raw_denials:
                totals["raw_discovery_denial_tasks"] += 1
        if tool_use.get("satisfied"):
            totals["satisfied_tasks"] += 1

        calls = int(tool_use.get("source_graph_calls") or 0)
        fresh_calls = int(tool_use.get("source_graph_fresh_calls") or 0)
        live_calls = int(tool_use.get("source_graph_live_calls") or 0)
        hit_count = int(tool_use.get("source_graph_hit_count") or 0)
        zero_hits = int(tool_use.get("source_graph_zero_hit_calls") or 0)
        failed_calls = int(tool_use.get("source_graph_failed_calls") or 0)
        source_bytes = int(tool_use.get("source_graph_bytes") or 0)
        cache_hits = int(tool_use.get("source_graph_cache_hits") or 0)
        violations = int(tool_use.get("policy_violations") or 0)
        satisfaction = str(tool_use.get("source_graph_satisfaction") or "")
        totals["source_graph_calls"] += calls
        totals["source_graph_fresh_calls"] += fresh_calls
        if fresh_calls > 0:
            totals["source_graph_fresh_tasks"] += 1
        totals["source_graph_live_calls"] += live_calls
        totals["source_graph_hit_count"] += hit_count
        totals["source_graph_zero_hit_calls"] += zero_hits
        totals["source_graph_failed_calls"] += failed_calls
        totals["source_graph_bytes"] += source_bytes
        totals["source_graph_cache_hits"] += cache_hits
        totals["policy_violations"] += violations
        bucket["source_graph_calls"] += calls
        bucket["source_graph_fresh_calls"] += fresh_calls
        bucket["source_graph_hit_count"] += hit_count
        bucket["source_graph_zero_hit_calls"] += zero_hits
        bucket["source_graph_failed_calls"] += failed_calls
        bucket["policy_violations"] += violations
        for source_key, total_key in (
            ("call_count_by_tool", "tool_call_counts"),
            ("successful_call_count_by_tool", "tool_success_counts"),
            ("bounded_bytes_by_tool", "tool_bytes"),
            ("cache_hits_by_tool", "tool_cache_hits"),
        ):
            values = tool_use.get(source_key)
            if not isinstance(values, Mapping):
                continue
            destination = totals[total_key]
            adapter_destination = bucket.get(total_key)
            for tool_name, value in values.items():
                safe_name = str(tool_name)[:64]
                destination[safe_name] = (
                    int(destination.get(safe_name) or 0) + max(0, int(value or 0))
                )
                if isinstance(adapter_destination, dict):
                    adapter_destination[safe_name] = (
                        int(adapter_destination.get(safe_name) or 0)
                        + max(0, int(value or 0))
                    )
        mode_counts = tool_use.get("source_graph_mode_counts")
        if isinstance(mode_counts, Mapping):
            totals["source_graph_mode_eligible_calls"] += calls
            for mode, count in mode_counts.items():
                safe_mode = str(mode)[:40]
                safe_count = max(0, int(count or 0))
                totals["source_graph_mode_counts"][safe_mode] = (
                    int(totals["source_graph_mode_counts"].get(safe_mode) or 0)
                    + safe_count
                )
                bucket_modes = bucket["source_graph_mode_counts"]
                bucket_modes[safe_mode] = (
                    int(bucket_modes.get(safe_mode) or 0) + safe_count
                )
        sequence = tool_use.get("source_graph_mode_sequence")
        if isinstance(sequence, list):
            for mode in sequence:
                if len(totals["source_graph_mode_sequence"]) >= 128:
                    break
                totals["source_graph_mode_sequence"].append(str(mode)[:40])
        stage_counts = tool_use.get("source_graph_stage_counts")
        if isinstance(stage_counts, Mapping):
            totals["source_graph_stage_eligible_calls"] += calls
            for stage, count in stage_counts.items():
                safe_stage = str(stage)[:32]
                safe_count = max(0, int(count or 0))
                totals["source_graph_stage_counts"][safe_stage] = (
                    int(totals["source_graph_stage_counts"].get(safe_stage) or 0)
                    + safe_count
                )
                bucket_stages = bucket["source_graph_stage_counts"]
                bucket_stages[safe_stage] = int(bucket_stages.get(safe_stage) or 0) + safe_count
        stage_sequence = tool_use.get("source_graph_stage_sequence")
        if isinstance(stage_sequence, list):
            for stage in stage_sequence:
                if len(totals["source_graph_stage_sequence"]) >= 128:
                    break
                totals["source_graph_stage_sequence"].append(str(stage)[:32])
        mode_stage_counts = tool_use.get("source_graph_mode_stage_counts")
        if isinstance(mode_stage_counts, Mapping):
            for stage, modes in mode_stage_counts.items():
                if not isinstance(modes, Mapping):
                    continue
                destination = totals["source_graph_mode_stage_counts"].setdefault(
                    str(stage)[:32], {}
                )
                for mode, count in modes.items():
                    safe_mode = str(mode)[:40]
                    destination[safe_mode] = (
                        int(destination.get(safe_mode) or 0) + max(0, int(count or 0))
                    )
        latency = tool_use.get("source_graph_latency")
        if isinstance(latency, Mapping):
            samples = latency.get("samples_ms")
            if isinstance(samples, list):
                aggregate_samples = totals["source_graph_latency"].setdefault("samples_ms", [])
                for value in samples:
                    if len(aggregate_samples) >= 512:
                        totals["source_graph_latency"]["samples_truncated"] = True
                        break
                    if isinstance(value, (int, float)) and 0 <= float(value) <= 3_600_000:
                        aggregate_samples.append(round(float(value), 3))
            if latency.get("samples_truncated"):
                totals["source_graph_latency"]["samples_truncated"] = True
        call_gaps = tool_use.get("source_graph_call_gaps")
        if isinstance(call_gaps, Mapping):
            samples = call_gaps.get("samples_seconds")
            if isinstance(samples, list):
                aggregate_gaps = totals["source_graph_call_gaps"].setdefault(
                    "samples_seconds", []
                )
                for value in samples:
                    if len(aggregate_gaps) >= 512:
                        totals["source_graph_call_gaps"]["samples_truncated"] = True
                        break
                    if isinstance(value, (int, float)) and 0 <= float(value) <= 31_536_000:
                        aggregate_gaps.append(round(float(value), 3))
            if call_gaps.get("samples_truncated"):
                totals["source_graph_call_gaps"]["samples_truncated"] = True
        evidence_rows = tool_use.get("source_graph_evidence_rows")
        if isinstance(evidence_rows, Mapping):
            for key in ("entity_rows", "edge_rows", "file_rows"):
                totals["source_graph_evidence_rows"][key] += max(
                    0, int(evidence_rows.get(key) or 0)
                )
        revision_counts = tool_use.get("source_graph_index_revision_counts")
        if isinstance(revision_counts, Mapping):
            for revision, count in revision_counts.items():
                safe_revision = str(revision)[:96]
                totals["source_graph_index_revision_counts"][safe_revision] = (
                    int(totals["source_graph_index_revision_counts"].get(safe_revision) or 0)
                    + max(0, int(count or 0))
                )
        index_sequence = tool_use.get("source_graph_index_sequence")
        if isinstance(index_sequence, list):
            for identity in index_sequence:
                if len(totals["source_graph_index_sequence"]) >= 128:
                    break
                if not isinstance(identity, Mapping):
                    continue
                totals["source_graph_index_sequence"].append({
                    "revision": str(identity.get("revision") or "")[:96],
                    "finished_at": str(identity.get("finished_at") or "")[:64],
                })
        if violations:
            totals["policy_violation_tasks"] += 1
        if int(tool_use.get("entries_tampered") or 0):
            totals["tampered_ledger_tasks"] += 1
        if not tool_use.get("satisfied"):
            reason = str(tool_use.get("reason") or "unspecified")[:240]
            reason_counts = totals["blocked_reason_counts"]
            reason_counts[reason] = int(reason_counts.get(reason) or 0) + 1

        if live_calls > 0:
            totals["source_graph_any_tasks"] += 1
            totals["source_graph_live_tasks"] += 1
            bucket["live_tasks"] += 1
        elif satisfaction == "injected_receipt":
            totals["source_graph_any_tasks"] += 1
            totals["source_graph_injected_only_tasks"] += 1
            bucket["injected_only_tasks"] += 1
        elif satisfaction == "stale_or_cached" or calls > 0:
            totals["source_graph_any_tasks"] += 1
            totals["source_graph_stale_or_cached_tasks"] += 1
            bucket["missing_or_stale_tasks"] += 1
        else:
            totals["source_graph_missing_tasks"] += 1
            bucket["missing_or_stale_tasks"] += 1

    denominator = totals["gated_tasks"]
    attributed_calls = sum(
        max(0, int(count or 0))
        for count in totals["source_graph_mode_counts"].values()
    )
    totals["source_graph_mode_attributed_calls"] = attributed_calls
    totals["source_graph_mode_unattributed_calls"] = max(
        0, totals["source_graph_mode_eligible_calls"] - attributed_calls
    )
    totals["source_graph_mode_legacy_calls"] = max(
        0, totals["source_graph_calls"] - totals["source_graph_mode_eligible_calls"]
    )
    stage_attributed = sum(
        max(0, int(count or 0))
        for stage, count in totals["source_graph_stage_counts"].items()
        if stage != "unspecified"
    )
    totals["source_graph_stage_attributed_calls"] = stage_attributed
    totals["source_graph_stage_unattributed_calls"] = max(
        0, totals["source_graph_stage_eligible_calls"] - stage_attributed
    )
    totals["source_graph_stage_legacy_calls"] = max(
        0, totals["source_graph_calls"] - totals["source_graph_stage_eligible_calls"]
    )
    if totals["source_graph_stage_eligible_calls"]:
        totals["source_graph_stage_attribution_rate"] = round(
            100.0 * stage_attributed / totals["source_graph_stage_eligible_calls"], 1
        )
    latency_samples = sorted(totals["source_graph_latency"].pop("samples_ms", []))
    if latency_samples:
        def latency_percentile(fraction: float) -> float:
            index = min(
                len(latency_samples) - 1,
                max(0, int(round((len(latency_samples) - 1) * fraction))),
            )
            return latency_samples[index]

        totals["source_graph_latency"].update({
            "count": len(latency_samples),
            "total_ms": round(sum(latency_samples), 3),
            "min_ms": latency_samples[0],
            "max_ms": latency_samples[-1],
            "p50_ms": latency_percentile(0.50),
            "p95_ms": latency_percentile(0.95),
        })
    gap_samples = sorted(totals["source_graph_call_gaps"].pop("samples_seconds", []))
    if gap_samples:
        def gap_percentile(fraction: float) -> float:
            index = min(
                len(gap_samples) - 1,
                max(0, int(round((len(gap_samples) - 1) * fraction))),
            )
            return gap_samples[index]

        totals["source_graph_call_gaps"].update({
            "count": len(gap_samples),
            "total_seconds": round(sum(gap_samples), 3),
            "min_seconds": gap_samples[0],
            "max_seconds": gap_samples[-1],
            "p50_seconds": gap_percentile(0.50),
            "p95_seconds": gap_percentile(0.95),
            "long_gap_count": sum(
                1
                for value in gap_samples
                if value >= SOURCE_GRAPH_LONG_CALL_GAP_SECONDS
            ),
        })
        long_gap_count = totals["source_graph_call_gaps"]["long_gap_count"]
        totals["source_graph_call_gaps"]["long_gap_rate"] = round(
            100.0 * long_gap_count / len(gap_samples), 1
        )
        totals["source_graph_call_gaps"]["alert_state"] = (
            "observed" if long_gap_count else "clear"
        )
    totals["source_graph_distinct_modes"] = sum(
        1 for count in totals["source_graph_mode_counts"].values() if int(count or 0) > 0
    )
    if totals["source_graph_mode_eligible_calls"]:
        totals["source_graph_mode_attribution_rate"] = round(
            100.0 * attributed_calls / totals["source_graph_mode_eligible_calls"], 1
        )
    for bucket in totals["by_adapter"].values():
        bucket_attributed = sum(
            max(0, int(count or 0))
            for count in bucket["source_graph_mode_counts"].values()
        )
        bucket["source_graph_mode_attributed_calls"] = bucket_attributed
        bucket["source_graph_mode_unattributed_calls"] = max(
            0, bucket["source_graph_calls"] - bucket_attributed
        )
    if denominator:
        totals["live_rate"] = round(100.0 * totals["source_graph_live_tasks"] / denominator, 1)
        totals["fresh_rate"] = round(
            100.0 * totals["source_graph_fresh_tasks"] / denominator, 1
        )
        totals["any_rate"] = round(100.0 * totals["source_graph_any_tasks"] / denominator, 1)
        totals["gate_satisfaction_rate"] = round(100.0 * totals["satisfied_tasks"] / denominator, 1)
    return totals


def _read_efficiency_telemetry(process_report: Mapping[str, Any]) -> dict[str, Any]:
    """Aggregate path-free provider read evidence by latest task run."""

    latest_by_task: dict[str, Mapping[str, Any]] = {}
    for row in process_report.get("processes") or []:
        if not isinstance(row, Mapping):
            continue
        task_id = str(row.get("task_id") or "").strip()
        if task_id and task_id not in latest_by_task:
            latest_by_task[task_id] = row

    count_fields = (
        "provider_records_scanned", "recognized_read_events",
        "recognized_source_graph_events", "total_reads", "known_repetitions",
        "unknown_repetitions", "exact_rereads", "overlap_rereads",
        "bounded_reads", "unbounded_reads", "explicit_source_graph_associations",
        "derived_temporal_associations", "read_bytes_observed",
        "total_read_bytes", "bounded_read_bytes", "unbounded_read_bytes",
        "exact_reread_bytes", "overlap_reread_bytes",
        "unknown_repetition_bytes",
    )
    totals: dict[str, Any] = {
        "schema_id": "aiworkhub.read_efficiency.telemetry.v2",
        "observed_tasks": len(latest_by_task),
        "evidence_observed_tasks": 0,
        "evidence_unobserved_tasks": 0,
        "legacy_evidence_tasks": 0,
        **{field: 0 for field in count_fields},
        "bounded_read_rate": None,
        "exact_reread_rate": None,
        "read_byte_coverage_rate": None,
        "measurement_label": (
            "observed_provider_events_and_bytes_only_no_token_or_cost_claim"
        ),
        "by_adapter": {},
    }

    for row in latest_by_task.values():
        infra = row.get("ai_infra_context")
        evidence = infra.get("read_efficiency") if isinstance(infra, Mapping) else None
        adapter = str(row.get("adapter_id") or "unknown")[:120]
        bucket = totals["by_adapter"].setdefault(
            adapter,
            {
                "tasks": 0,
                "evidence_observed_tasks": 0,
                "legacy_evidence_tasks": 0,
                **{field: 0 for field in count_fields},
            },
        )
        bucket["tasks"] += 1
        if not isinstance(evidence, Mapping) or not evidence.get("evidence_observed"):
            totals["evidence_unobserved_tasks"] += 1
            continue
        if str(evidence.get("schema_id") or "") != "aiworkhub.provider_read_efficiency.v2":
            # v1 Codex summaries counted both item.started and item.completed
            # for one command. Preserve their existence as legacy evidence,
            # but never mix their suspect counts into current KPIs.
            totals["legacy_evidence_tasks"] += 1
            bucket["legacy_evidence_tasks"] += 1
            continue
        totals["evidence_observed_tasks"] += 1
        bucket["evidence_observed_tasks"] += 1
        for field in count_fields:
            value = max(0, _bounded_int(evidence.get(field)))
            totals[field] += value
            bucket[field] += value

    if totals["total_reads"]:
        totals["bounded_read_rate"] = round(
            100.0 * totals["bounded_reads"] / totals["total_reads"], 1
        )
        totals["exact_reread_rate"] = round(
            100.0 * totals["exact_rereads"] / totals["total_reads"], 1
        )
        totals["read_byte_coverage_rate"] = round(
            100.0 * totals["read_bytes_observed"] / totals["total_reads"], 1
        )
    return totals


def _project_context_telemetry(process_report: Mapping[str, Any]) -> dict[str, Any]:
    """Aggregate latest-per-task Session, Memory and KB context evidence."""
    latest_by_task: dict[str, Mapping[str, Any]] = {}
    for row in process_report.get("processes") or []:
        if not isinstance(row, Mapping):
            continue
        task_id = str(row.get("task_id") or "").strip()
        if task_id and task_id not in latest_by_task:
            latest_by_task[task_id] = row

    components = {
        name: {
            "requested_tasks": 0,
            "executed_tasks": 0,
            "hit_count": 0,
            "bytes": 0,
            "degraded_tasks": 0,
        }
        for name in ("session_current_state", "ai_memory", "kb")
    }
    for row in latest_by_task.values():
        infra = row.get("ai_infra_context")
        if not isinstance(infra, Mapping):
            continue
        for name, totals in components.items():
            section = infra.get(name)
            if not isinstance(section, Mapping) or not section:
                continue
            if section.get("requested"):
                totals["requested_tasks"] += 1
            if section.get("executed"):
                totals["executed_tasks"] += 1
            totals["hit_count"] += int(section.get("hit_count") or 0)
            totals["bytes"] += int(section.get("bytes") or 0)
            if section.get("degraded_reason"):
                totals["degraded_tasks"] += 1
    return {
        "schema_id": "aiworkhub.project_context.telemetry.v1",
        "observed_tasks": len(latest_by_task),
        **components,
    }


class DashboardReadError(RuntimeError):
    """A bounded failure from an existing read-only provider."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bounded_text(value: Any, limit: int = 300) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit] or "unknown read failure"


def _taskctl_stdout(result: Any, source: str) -> str:
    if not isinstance(result, Mapping):
        raise DashboardReadError(f"{source} returned an invalid result")

    returncode = result.get("returncode")
    if returncode not in (0, None):
        detail = _bounded_text(result.get("stderr") or result.get("stdout"))
        raise DashboardReadError(f"{source} failed: {detail}")
    if result.get("ok") is False and returncode is None:
        detail = _bounded_text(result.get("stderr") or result.get("error"))
        raise DashboardReadError(f"{source} failed: {detail}")
    return str(result.get("stdout") or "")


def parse_task_list(stdout: str, requested_status: str) -> list[dict[str, Any]]:
    """Parse taskctl's compact list format into JSON-ready task rows."""
    rows: list[dict[str, Any]] = []
    for line in (stdout or "").splitlines():
        match = _LIST_LINE_RE.match(line)
        if not match:
            continue
        row = match.groupdict()
        row["status"] = (row.get("status") or requested_status).strip().lower()
        row["topic"] = (row.get("topic") or "unknown").strip()
        row["runner"] = (row.get("runner") or "unassigned").strip()
        row["task_id"] = row["task_id"].strip()
        # Which concrete model is (or was) executing this task -- "" when
        # unknown (e.g. a legacy card with no recommended_model and no
        # reported usage yet).
        row["model"] = (row.get("model") or "").strip()
        # Coordinator-review-first terminal outcome (e.g. validation_failed/
        # timed_out/blocked) when the row carries one -- "" for every
        # pre-existing row shape (success review, no outcome suffix).
        row["outcome"] = (row.get("outcome") or "").strip()
        rows.append(row)
    return rows


def _extract_json_object(text: str) -> dict[str, Any] | None:
    decoder = json.JSONDecoder()
    for index, character in enumerate(text or ""):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _process_liveness_fields(row: Mapping[str, Any], status_path_raw: str) -> dict[str, Any]:
    """Bounded, read-only liveness derivation for one compact process row.

    Reads the owner-only supervisor heartbeat artifact (fails closed to
    ``{}`` on any symlink/permission/ownership/malformed-JSON problem via
    ``process_launcher.read_supervisor_status``) and derives the honest
    alive/quiet/unresponsive/lost state plus model/runtime/heartbeat-age
    fields -- never the raw status file path itself, and never unbounded
    log content.
    """
    supervisor_status = process_launcher.read_supervisor_status(Path(status_path_raw))
    if not supervisor_status:
        return {}
    try:
        pid = int(row.get("pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    ticks = row.get("pid_start_ticks")
    supervisor_alive = bool(pid and process_launcher._pid_matches(pid, ticks))
    child_pid = int(supervisor_status.get("child_pid") or 0)
    child_ticks = supervisor_status.get("child_pid_start_ticks")
    child_alive = bool(child_pid and process_launcher._pid_matches(child_pid, child_ticks))
    liveness = process_launcher.derive_liveness_state(
        now_epoch=time.time(),
        supervisor_alive=supervisor_alive,
        heartbeat_at_epoch=supervisor_status.get("heartbeat_at_epoch"),
        last_output_change_epoch=supervisor_status.get("last_output_change_epoch"),
    )
    started_epoch = supervisor_status.get("started_at_epoch")
    runtime_seconds = (
        max(0.0, time.time() - float(started_epoch))
        if isinstance(started_epoch, (int, float))
        else None
    )
    last_activity_epoch = supervisor_status.get("last_output_change_epoch") or supervisor_status.get(
        "heartbeat_at_epoch"
    )
    last_activity_at = None
    if isinstance(last_activity_epoch, (int, float)):
        last_activity_at = datetime.fromtimestamp(float(last_activity_epoch), tz=timezone.utc).isoformat()

    result: dict[str, Any] = {
        "liveness_state": liveness["liveness_state"],
        "heartbeat_age_seconds": liveness["heartbeat_age_seconds"],
        "activity_age_seconds": liveness["activity_age_seconds"],
        "supervisor_alive": supervisor_alive,
        "child_alive": child_alive,
        "runtime_seconds": runtime_seconds,
        "heartbeat_seq": supervisor_status.get("heartbeat_seq"),
        "last_activity_at": last_activity_at,
    }
    return {key: value for key, value in result.items() if value is not None}


def _merge_process_liveness_into_tasks(
    task_groups: Mapping[str, list[dict[str, Any]]],
    process_report: Mapping[str, Any],
) -> None:
    """Enrich task rows with bounded, read-only liveness facts from the
    isolated launcher's process log so the dashboard's Activity column
    shows a real timestamp instead of "Unknown" for a live worker, and so
    model/runtime/heartbeat/supervisor-child-alive/derived liveness state
    are visible on the owning task row -- never a write, never inferred
    from bare process existence alone."""
    rows_by_id = {
        row["task_id"]: row
        for rows in task_groups.values()
        for row in rows
        if row.get("task_id")
    }
    for process_row in process_report.get("processes") or []:
        if not isinstance(process_row, Mapping):
            continue
        target = rows_by_id.get(str(process_row.get("task_id") or ""))
        if target is None:
            continue
        if not target.get("last_activity_at") and process_row.get("last_activity_at"):
            target["last_activity_at"] = process_row["last_activity_at"]
        if not target.get("model") and process_row.get("model"):
            target["model"] = process_row["model"]
        for key in (
            "liveness_state",
            "heartbeat_age_seconds",
            "activity_age_seconds",
            "supervisor_alive",
            "child_alive",
            "runtime_seconds",
        ):
            value = process_row.get(key)
            if value is not None:
                target[key] = value
        if process_row.get("ai_infra_context"):
            target["ai_infra_context"] = process_row["ai_infra_context"]
        terminal = _provider_terminal_status(process_row)
        if terminal:
            target["provider_terminal"] = terminal


def _provider_terminal_status(process_row: Mapping[str, Any]) -> dict[str, Any]:
    """Project a bounded provider root cause onto the owning task row.

    Terminal failures already exist in the authenticated process ledger, but
    previously the task UI exposed only a generic substatus. This classifier
    keeps the original bounded message, adds a stable category, and never
    treats an active/healthy run as an error.
    """

    state = str(process_row.get("state") or "")[:80]
    if state in process_launcher.ACTIVE_PROCESS_STATES or state in {"review_ready", "exited"}:
        return {}
    raw_reason = (
        process_row.get("blocked_reason")
        or process_row.get("error")
        or process_row.get("reason")
        or ""
    )
    message = _portable_text(raw_reason, 500).strip()
    if not state and not message:
        return {}
    lowered = f"{state} {message}".lower()
    if any(marker in lowered for marker in (
        "authentication_failed", "unauthorized", "oauth", "token expired", "401",
    )):
        category = "provider_authentication_failed"
        retryable = True
    elif any(marker in lowered for marker in (
        "consent", "model_not_visible", "model unavailable", "unknown model",
    )):
        category = "provider_model_unavailable"
        retryable = True
    elif any(marker in lowered for marker in ("timed_out", "timeout", "deadline")):
        category = "provider_timeout"
        retryable = True
    elif any(marker in lowered for marker in ("enoent", "not found", "executable")):
        category = "provider_executable_unavailable"
        retryable = False
    elif state == "launch_failed":
        category = "provider_launch_failed"
        retryable = True
    else:
        category = "worker_terminal_failure"
        retryable = state not in {"scope_rejected", "cancelled"}
    if state == "validation_failed":
        recommended_action = "reject_review_to_residual_rework"
    elif category == "provider_authentication_failed":
        recommended_action = "repair_provider_authentication_then_retry"
    elif retryable:
        recommended_action = "retry_worker_launch"
    else:
        recommended_action = "inspect_terminal_evidence"
    return {
        "state": state,
        "category": category,
        "message": message,
        "retryable": retryable,
        "recommended_action": recommended_action,
        "adapter_id": str(process_row.get("adapter_id") or "")[:120],
        "model": str(process_row.get("model") or "")[:160],
    }


def read_process_runs(
    *,
    process_log_path: Path | None = None,
    limit: int = DEFAULT_PROCESS_LIMIT,
    max_bytes: int = MAX_PROCESS_LOG_BYTES,
) -> dict[str, Any]:
    """Read recent launcher events without reconciling or mutating process state."""
    safe_limit = max(1, min(int(limit), 1000))
    safe_max_bytes = max(1024, min(int(max_bytes), 16 * 1024 * 1024))
    path = process_log_path or Path(
        os.environ.get(
            process_launcher.PROCESS_LOG_ENV,
            str(core.repo_root() / process_launcher.PROCESS_LOG_DEFAULT_REL),
        )
    )
    base_report = {
        "ok": True,
        "readonly": True,
        "source": "process_event_log",
        "launch_implemented": process_launcher.LAUNCH_IMPLEMENTED,
        "launch_enabled": process_launcher.launch_gates_open(),
        "active_observed": 0,
        "total_requests": 0,
        "truncated": False,
        "invalid_records": 0,
        "processes": [],
    }
    if not path.is_file():
        return base_report

    size = path.stat().st_size
    start = max(0, size - safe_max_bytes)
    with path.open("rb") as handle:
        if start:
            handle.seek(start - 1)
            if handle.read(1) != b"\n":
                handle.readline()
        payload = handle.read(safe_max_bytes)

    latest: dict[str, dict[str, Any]] = {}
    # Bounded server-side-only path used to read the supervisor heartbeat
    # file for liveness derivation -- never included in the compact row
    # returned to the browser (matches the existing policy of never exposing
    # stdout_path/stderr_path either).
    status_paths: dict[str, str] = {}
    invalid_records = 0
    for raw_line in payload.splitlines():
        try:
            event = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            invalid_records += 1
            continue
        if not isinstance(event, Mapping):
            invalid_records += 1
            continue
        request_id = str(event.get("request_id") or "").strip()[:256]
        if not request_id:
            invalid_records += 1
            continue
        compact: dict[str, Any] = {}
        for key in _PROCESS_RUN_FIELDS:
            value = event.get(key)
            if value is None:
                continue
            if isinstance(value, str):
                compact[key] = value[:500]
            elif isinstance(value, (bool, int, float)):
                compact[key] = value
        ai_infra = _compact_ai_infra(event)
        if ai_infra:
            compact["ai_infra_context"] = _merge_ai_infra(
                (latest.get(request_id) or {}).get("ai_infra_context"),
                ai_infra,
            )
        latest[request_id] = {**latest.get(request_id, {}), **compact, "request_id": request_id}
        raw_status_path = event.get("supervisor_status_path")
        if isinstance(raw_status_path, str) and raw_status_path:
            status_paths[request_id] = raw_status_path[:1024]

    rows = sorted(
        latest.values(),
        key=lambda row: str(row.get("timestamp") or row.get("finished_at") or row.get("started_at") or ""),
        reverse=True,
    )[:safe_limit]
    active_observed = 0
    for row in rows:
        # Liveness describes an active process lease.  A clean terminal row
        # (for example ``review_ready``) is expected to have no live PID or
        # heartbeat owner; deriving liveness for it mislabeled successful
        # completion as ``lost`` in the dashboard/workforce telemetry.
        if row.get("state") not in process_launcher.ACTIVE_PROCESS_STATES:
            continue
        status_path_raw = status_paths.get(str(row.get("request_id") or ""))
        if status_path_raw:
            liveness_fields = _process_liveness_fields(row, status_path_raw)
            if liveness_fields:
                row.update(liveness_fields)
        try:
            pid = int(row.get("pid") or 0)
            alive = bool(pid and process_launcher._pid_matches(pid, row.get("pid_start_ticks")))
        except (TypeError, ValueError, OSError):
            alive = False
        row["process_alive"] = alive
        if alive:
            active_observed += 1
        else:
            row["observed_state"] = "not_running"

    return {
        **base_report,
        "active_observed": active_observed,
        "total_requests": len(latest),
        "truncated": bool(start),
        "invalid_records": invalid_records,
        "processes": rows,
    }


def exact_status_counts(repo_root: Path | str | None = None) -> dict[str, int]:
    """Exact per-canonical-status totals across the whole canonical queue.

    Reads only the narrow ``(archived_at, status, worker_status)`` columns
    from the AIWorkHub-owned canonical SQLite task table -- never
    ``card_json``, and never a row/list payload -- so a bucket (e.g.
    ``finished``) holding many thousands of cards is still counted exactly
    instead of being capped at ``DEFAULT_TASK_LIMIT``. Uses
    ``task_store.canonical_status`` -- the same precedence dashboard.js
    renders -- and never imports ``AITools/taskdb.py`` or
    ``AITools/taskctl.py``: this repository's own dev-ops queue is not the
    data source for an arbitrary attached repository. A pure ``SELECT``:
    never inserts, updates, or deletes. Raises
    ``task_store.StorageNotReadyError`` when canonical storage is not yet
    verified-ready; callers must check readiness first and never fall back.
    """
    counts = task_store.exact_status_counts(repo_root or _default_repo_root())
    return {status: int(counts.get(status, 0)) for status in ALL_CANONICAL_STATUSES}


def _default_repo_root() -> Path:
    """Resolve the active repository root the same way the MCP child was
    spawned or switched, never a fixed path relative to this package's own
    install location.

    ``core.repo_root`` is the process-wide repository authority.  In a
    repo-neutral manager child it also honors the atomic in-process switch
    override.  Resolving independently from cwd here made dashboard snapshots
    keep reading the repository from which the process originally started
    after the manager had switched to another repository.
    """
    return core.repo_root()


class DashboardProvider:
    """Adapter over the canonical AIWorkHub task_store plus the package's
    existing read-only process/cost/collision providers."""

    def __init__(
        self,
        *,
        repo_root: Path | str | None = None,
        task_limit: int = DEFAULT_TASK_LIMIT,
        stale_processing_hours: float = completion_inbox.DEFAULT_STALE_PROCESSING_HOURS,
    ) -> None:
        self.repo_root = Path(repo_root) if repo_root is not None else _default_repo_root()
        self.task_limit = max(1, min(int(task_limit), completion_inbox.MAX_LIMIT))
        self.stale_processing_hours = max(0.0, float(stale_processing_hours))

    def get_storage_readiness(self) -> task_store.StorageReadiness:
        """Verified registry/database authority readiness for the active
        repository -- never directory existence alone. Every dashboard read
        path below must check this before touching the canonical DB."""
        return task_store.storage_readiness(self.repo_root)

    def list_tasks(self, status: str) -> list[dict[str, Any]]:
        return task_store.list_tasks(self.repo_root, status=status, limit=self.task_limit)

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        return task_store.get_task(self.repo_root, task_id)

    def get_task_events(self, task_id: str) -> list[dict[str, Any]]:
        return task_store.get_task_events(self.repo_root, task_id)

    def get_completion_inbox(self) -> dict[str, Any]:
        review_rows = task_store.list_tasks(self.repo_root, status="review", limit=self.task_limit)
        processing_rows = task_store.list_tasks(self.repo_root, status="processing", limit=self.task_limit)
        stale_rows: list[dict[str, Any]] = []
        stale_after = self.stale_processing_hours * 3600.0
        now = time.time()
        for row in processing_rows:
            raw = str(row.get("updated_at") or row.get("started_at") or "").replace("Z", "+00:00")
            try:
                age = now - datetime.fromisoformat(raw).timestamp()
            except (TypeError, ValueError):
                continue
            if age >= stale_after:
                stale_rows.append(row)
        return {
            "review_queue": review_rows,
            "stale_processing": stale_rows,
            "runner_mismatch_warnings": [],
            "latest_validation_facts": [],
            "read_errors": [],
        }

    def get_cost_ledger(self) -> dict[str, Any]:
        # Reuse the canonical union ledger exposed through Task MCP. Task
        # cards alone omit retry/terminal usage and can produce a false-zero
        # dashboard while measured launch records exist.
        result = dict(cost_ledger.build_cost_ledger(
            repo_root=self.repo_root,
            include_tasks=False,
        ))
        result["schema_id"] = "aiworkhub.dashboard.canonical_cost_ledger.v1"
        return result

    def get_collision_report(self) -> dict[str, Any]:
        active: list[dict[str, Any]] = []
        for status in ACTIVE_STATUSES:
            active.extend(task_store.list_tasks(self.repo_root, status=status, limit=self.task_limit))
        owners_by_path: dict[str, list[str]] = defaultdict(list)
        for row in active:
            task_id = str(row.get("task_id") or "")
            card = task_store.get_task(self.repo_root, task_id) or {}
            for path in card.get("allowed_writes") or []:
                normalized = str(path).strip()
                if normalized and task_id not in owners_by_path[normalized]:
                    owners_by_path[normalized].append(task_id)
        collisions = [
            {"path": path, "task_ids": owners}
            for path, owners in sorted(owners_by_path.items())
            if len(owners) > 1
        ]
        return {
            "collision_free": not collisions,
            "active_cards": len(active),
            "collision_count": len(collisions),
            "file_collisions": collisions,
        }

    def get_agent_processes(self) -> dict[str, Any]:
        return read_process_runs(limit=DEFAULT_SNAPSHOT_PROCESS_LIMIT)

    def get_kpi_processes(self) -> dict[str, Any]:
        """Larger bounded history used only for aggregate KPI projections.

        Raw rows are never returned as a second browser payload; the snapshot
        exposes only the bounded aggregates and their truncation metadata.
        """
        return read_process_runs(
            limit=DEFAULT_KPI_HISTORY_PROCESS_LIMIT,
            max_bytes=MAX_KPI_HISTORY_LOG_BYTES,
        )

    def get_exact_status_counts(self) -> dict[str, int]:
        """Exact per-status totals across the whole canonical queue (see
        ``exact_status_counts``) -- independent of ``task_limit``, never a
        per-row payload."""
        return exact_status_counts(self.repo_root)

    def get_manager_decision_counts(self) -> dict[str, int]:
        """Exact explicit accept/reject totals from canonical task events."""
        return task_store.manager_decision_counts(self.repo_root)

    def get_adapter_readiness(self) -> dict[str, Any]:
        """Read-only worker-adapter readiness (never exposes any secret)."""
        return deepseek_credentials.adapter_readiness(repo=self.repo_root)

    def get_environment_preflight(self) -> dict[str, Any]:
        """Unified repository, policy, Source Graph and provider readiness."""
        return repo_policy.build_preflight(self.repo_root)

    def get_workforce_catalog(self) -> dict[str, Any]:
        """Configured workforce joined to bounded canonical process evidence."""
        process_report = read_process_runs(limit=DEFAULT_PROCESS_LIMIT)
        return workforce_catalog.build_catalog(
            self.repo_root,
            cards=task_store.list_task_cards(self.repo_root, limit=5000),
            process_rows=process_report.get("processes") or [],
            usage_rows=cost_ledger.build_cost_ledger(
                repo_root=self.repo_root, include_tasks=True
            ).get("tasks") or [],
        )

    def get_callback_bridge_health(self) -> dict[str, Any]:
        """Read-only, redacted-safe callback bridge health: bound/unbound
        task counts and per-state outbox counts, sourced from the canonical
        AIWorkHub task_store -- never ``AITools/taskdb.py``. Never exposes a
        full origin_thread_id."""
        return task_store.callback_bridge_health(self.repo_root)

    def get_task_plan(self) -> dict[str, Any]:
        """Read-only Plan-DAG projection from full canonical cards."""
        cards = task_store.list_task_cards(self.repo_root, limit=5000)
        return {
            "ok": True,
            "schema_id": "aiworkhub.task_plan_snapshot.v1",
            **task_plan.build_snapshot(cards),
        }


def _normalize_task_rows(value: Any, status: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise DashboardReadError(f"task list ({status}) returned a non-list")
    rows: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, Mapping):
            continue
        task_id = str(raw.get("task_id") or "").strip()
        if not task_id:
            continue
        row = dict(raw)
        row["task_id"] = task_id
        row["status"] = str(raw.get("status") or status).strip().lower()
        row["topic"] = str(raw.get("topic") or "unknown").strip()
        row["runner"] = str(raw.get("runner") or "unassigned").strip()
        # Carry archived_at so the dashboard can mark archived rows
        archived_at = str(raw.get("archived_at") or "").strip()
        if archived_at:
            row["archived_at"] = archived_at
        rows.append(row)
    return rows


def _safe_read(
    source: str,
    operation: Callable[[], Any],
    errors: list[dict[str, str]],
    fallback: Any,
) -> Any:
    try:
        return operation()
    except Exception as exc:  # noqa: BLE001 - each provider must degrade independently
        errors.append({
            "source": source,
            "kind": type(exc).__name__,
            "message": _bounded_text(exc),
        })
        return fallback


def _merge_inbox_facts(
    task_groups: dict[str, list[dict[str, Any]]],
    inbox: Mapping[str, Any],
) -> None:
    rows_by_id = {
        row["task_id"]: row
        for rows in task_groups.values()
        for row in rows
        if row.get("task_id")
    }

    enrichments: list[Any] = []
    enrichments.extend(inbox.get("latest_validation_facts") or [])
    enrichments.extend(inbox.get("review_queue") or [])
    for fact in enrichments:
        if not isinstance(fact, Mapping):
            continue
        target = rows_by_id.get(str(fact.get("task_id") or ""))
        if target is None:
            continue
        for key, value in fact.items():
            if key not in {"task_id", "lifecycle_state"} and value not in (None, ""):
                target[key] = value


def _build_dimension_summary(
    task_groups: Mapping[str, list[dict[str, Any]]],
    dimension: str,
    stale_ids: set[str],
) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "name": "",
            "total": 0,
            "pending": 0,
            "processing": 0,
            "review": 0,
            "blocked": 0,
            "stale": 0,
        }
    )
    for status in ACTIVE_STATUSES:
        for task in task_groups.get(status, []):
            name = str(task.get(dimension) or "unknown").strip() or "unknown"
            bucket = buckets[name]
            bucket["name"] = name
            bucket["total"] += 1
            bucket[status] += 1
            if task.get("task_id") in stale_ids:
                bucket["stale"] += 1
    return sorted(buckets.values(), key=lambda item: (-item["total"], item["name"].lower()))


def _cost_totals(ledger: Mapping[str, Any] | None) -> dict[str, Any]:
    totals = {
        "available": bool(ledger),
        "records": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cost_usd": 0.0,
        "cost_known_records": 0,
        "cost_unknown_records": 0,
        "tokens_with_unknown_cost": 0,
        "usage_observed_records": 0,
        "usage_unknown_records": 0,
    }
    if not ledger:
        return totals
    by_runner = (ledger.get("aggregates") or {}).get("by_runner") or {}
    if not isinstance(by_runner, Mapping):
        return totals
    for bucket in by_runner.values():
        if not isinstance(bucket, Mapping):
            continue
        totals["records"] += int(bucket.get("records") or 0)
        totals["input_tokens"] += int(bucket.get("input_tokens") or 0)
        totals["output_tokens"] += int(bucket.get("output_tokens") or 0)
        totals["total_tokens"] += int(bucket.get("total_tokens") or 0)
        totals["cost_usd"] += float(bucket.get("cost_usd") or 0.0)
        totals["cost_known_records"] += int(bucket.get("cost_known_records") or 0)
        totals["cost_unknown_records"] += int(bucket.get("cost_unknown_records") or 0)
        totals["tokens_with_unknown_cost"] += int(bucket.get("tokens_with_unknown_cost") or 0)
        totals["usage_observed_records"] += int(
            bucket.get("usage_observed_records") or 0
        )
        totals["usage_unknown_records"] += int(
            bucket.get("usage_unknown_records") or 0
        )
    totals["cost_usd"] = round(totals["cost_usd"], 6)
    totals["cost_complete"] = totals["cost_unknown_records"] == 0
    totals["usage_complete"] = (
        totals["records"] > 0
        and totals["usage_unknown_records"] == 0
        and totals["usage_observed_records"] == totals["records"]
    )
    return totals


def build_snapshot(provider: Any | None = None) -> dict[str, Any]:
    """Build one dashboard snapshot while isolating every provider failure.

    Fails closed on storage: an uninitialized or degraded repository (no
    manifest, no verified canonical DB, authority not canonical, corrupt
    schema/quick_check) returns an empty UNINITIALIZED/DEGRADED snapshot --
    zero task counts, zero rows -- and never calls any secondary provider,
    so no legacy row can ever leak into the response. There is no fallback
    path: readiness is verified registry/database authority, never
    directory existence alone (see ``task_store.storage_readiness``).
    """
    data_provider = provider or DashboardProvider()

    readiness_reader = getattr(data_provider, "get_storage_readiness", None)
    readiness = readiness_reader() if readiness_reader is not None else None
    storage_state = {
        "ready": bool(getattr(readiness, "ready", True)) if readiness is not None else True,
        "reason": str(getattr(readiness, "reason", "unknown_provider")) if readiness is not None else "unknown_provider",
        "repo_id": str(getattr(readiness, "repo_id", "")) if readiness is not None else "",
    }
    provider_root = getattr(data_provider, "repo_root", None)
    storage_usage = storage_observability.snapshot(provider_root or _default_repo_root())
    if readiness is not None and not storage_state["ready"]:
        zero_counts = {status: 0 for status in ALL_CANONICAL_STATUSES}
        zero_counts["stale"] = 0
        zero_counts["active"] = 0
        empty_tasks = {status: [] for status in (*ACTIVE_STATUSES, "blocked", "finished", "archived", "stale")}
        return {
            "schema_version": 1,
            "generated_at": _utc_now(),
            "readonly": True,
            "storage": storage_state,
            "storage_usage": storage_usage,
            "health": {"ok": False, "degraded": True, "provider_error_count": 0},
            "status_counts": zero_counts,
            "row_counts": {
                status: {"returned": 0, "exact": 0, "truncated": False} for status in ALL_CANONICAL_STATUSES
            },
            "tasks": empty_tasks,
            "summaries": {"topics": [], "runners": []},
            "completion_inbox": {},
            "cost_usage": {"totals": _cost_totals(None), "ledger": {}},
            "collision_report": {},
            "agent_processes": {},
            "adapter_readiness": {},
            "source_graph_telemetry": _source_graph_telemetry({}),
            "read_efficiency_telemetry": _read_efficiency_telemetry({}),
            "project_context_telemetry": _project_context_telemetry({}),
            "kpi_analytics": dashboard_kpis.build_kpi_snapshot(
                process_report={},
                source_graph=_source_graph_telemetry({}),
                project_context=_project_context_telemetry({}),
                callback_health={},
                cost_totals=_cost_totals(None),
                process_limit=DEFAULT_SNAPSHOT_PROCESS_LIMIT,
                manager_decision_totals={},
            ),
            "callback_bridge_health": {},
            "task_plan": {},
            "warnings": {"stale": [], "collisions": [], "runner_mismatches": []},
            "errors": [],
        }

    errors: list[dict[str, str]] = []
    task_groups: dict[str, list[dict[str, Any]]] = {}

    for status in ACTIVE_STATUSES:
        value = _safe_read(
            f"tasks.{status}",
            partial(data_provider.list_tasks, status),
            errors,
            [],
        )
        task_groups[status] = _safe_read(
            f"tasks.{status}.normalize",
            partial(_normalize_task_rows, value, status),
            errors,
            [],
        )

    inbox = _safe_read(
        "completion_inbox",
        data_provider.get_completion_inbox,
        errors,
        {},
    )
    if not isinstance(inbox, Mapping):
        errors.append({
            "source": "completion_inbox",
            "kind": "DashboardReadError",
            "message": "completion inbox returned a non-object",
        })
        inbox = {}

    ledger = _safe_read("cost_ledger", data_provider.get_cost_ledger, errors, {})
    if not isinstance(ledger, Mapping):
        errors.append({
            "source": "cost_ledger",
            "kind": "DashboardReadError",
            "message": "cost ledger returned a non-object",
        })
        ledger = {}

    collision_report = _safe_read(
        "collision_guard",
        data_provider.get_collision_report,
        errors,
        {},
    )
    if not isinstance(collision_report, Mapping):
        errors.append({
            "source": "collision_guard",
            "kind": "DashboardReadError",
            "message": "collision guard returned a non-object",
        })
        collision_report = {}

    process_reader = getattr(
        data_provider,
        "get_agent_processes",
        lambda: {"ok": True, "processes": [], "total_requests": 0},
    )
    process_report = _safe_read("agent_processes", process_reader, errors, {})
    if not isinstance(process_report, Mapping):
        errors.append({
            "source": "agent_processes",
            "kind": "DashboardReadError",
            "message": "agent process provider returned a non-object",
        })
        process_report = {}

    kpi_process_reader = getattr(data_provider, "get_kpi_processes", None)
    if kpi_process_reader is None:
        kpi_process_report = process_report
        kpi_process_limit = DEFAULT_SNAPSHOT_PROCESS_LIMIT
    else:
        kpi_process_report = _safe_read(
            "kpi_process_history", kpi_process_reader, errors, process_report
        )
        if not isinstance(kpi_process_report, Mapping):
            errors.append({
                "source": "kpi_process_history",
                "kind": "DashboardReadError",
                "message": "KPI process history provider returned a non-object",
            })
            kpi_process_report = process_report
        kpi_process_limit = DEFAULT_KPI_HISTORY_PROCESS_LIMIT

    adapter_reader = getattr(
        data_provider,
        "get_adapter_readiness",
        lambda: {"ok": True, "readonly": True, "adapters": []},
    )
    adapter_readiness = _safe_read("adapter_readiness", adapter_reader, errors, {})
    if not isinstance(adapter_readiness, Mapping):
        errors.append({
            "source": "adapter_readiness",
            "kind": "DashboardReadError",
            "message": "adapter readiness provider returned a non-object",
        })
        adapter_readiness = {}

    preflight_reader = getattr(data_provider, "get_environment_preflight", lambda: {})
    environment_preflight = _safe_read(
        "environment_preflight", preflight_reader, errors, {}
    )
    if not isinstance(environment_preflight, Mapping):
        errors.append({
            "source": "environment_preflight",
            "kind": "DashboardReadError",
            "message": "environment preflight provider returned a non-object",
        })
        environment_preflight = {}

    workforce_reader = getattr(data_provider, "get_workforce_catalog", lambda: {})
    workforce = _safe_read("workforce_catalog", workforce_reader, errors, {})
    if not isinstance(workforce, Mapping):
        errors.append({
            "source": "workforce_catalog",
            "kind": "DashboardReadError",
            "message": "workforce catalog provider returned a non-object",
        })
        workforce = {}

    callback_reader = getattr(
        data_provider,
        "get_callback_bridge_health",
        lambda: {"bound_task_count": 0, "unbound_task_count": 0, "by_state": {}},
    )
    callback_bridge_health = _safe_read("callback_bridge_health", callback_reader, errors, {})
    if not isinstance(callback_bridge_health, Mapping):
        errors.append({
            "source": "callback_bridge_health",
            "kind": "DashboardReadError",
            "message": "callback bridge health provider returned a non-object",
        })
        callback_bridge_health = {}

    plan_reader = getattr(data_provider, "get_task_plan", lambda: {})
    task_plan_snapshot = _safe_read("task_plan", plan_reader, errors, {})
    if not isinstance(task_plan_snapshot, Mapping):
        errors.append({
            "source": "task_plan",
            "kind": "DashboardReadError",
            "message": "task plan provider returned a non-object",
        })
        task_plan_snapshot = {}

    _merge_inbox_facts(task_groups, inbox)
    _merge_process_liveness_into_tasks(task_groups, process_report)

    stale_tasks = [dict(item) for item in inbox.get("stale_processing", []) if isinstance(item, Mapping)]
    stale_ids = {str(item.get("task_id")) for item in stale_tasks if item.get("task_id")}
    for row in task_groups["processing"]:
        if row.get("task_id") in stale_ids:
            row["stale"] = True

    for read_error in inbox.get("read_errors", []) or []:
        if isinstance(read_error, Mapping):
            errors.append({
                "source": f"completion_inbox.{read_error.get('scope') or 'read'}",
                "kind": str(read_error.get("error_kind") or "read_error"),
                "message": _bounded_text(read_error.get("error_message")),
            })

    source_status = ledger.get("source_status") or {}
    if isinstance(source_status, Mapping):
        for name, ok in source_status.items():
            if ok is False:
                errors.append({
                    "source": f"cost_ledger.{name}",
                    "kind": "source_unavailable",
                    "message": "cost or usage source reported unavailable",
                })

    collision_warnings = [
        dict(item)
        for item in collision_report.get("file_collisions", []) or []
        if isinstance(item, Mapping)
    ]
    mismatch_warnings = [
        dict(item)
        for item in inbox.get("runner_mismatch_warnings", []) or []
        if isinstance(item, Mapping)
    ]

    # Exact whole-queue totals (see exact_status_counts): a single narrow
    # SQLite aggregate over (archived_at, status, worker_status) only, never
    # a per-row payload, so blocked/finished/archived stay exact past
    # DEFAULT_TASK_LIMIT. Providers without this optional method (e.g. test
    # doubles) fall back to the historical bounded-row-length behavior.
    exact_reader = getattr(data_provider, "get_exact_status_counts", None)
    exact_counts: Mapping[str, Any] | None = None
    if exact_reader is not None:
        exact_counts = _safe_read("exact_status_counts", exact_reader, errors, None)
        if not isinstance(exact_counts, Mapping):
            if exact_counts is not None:
                errors.append({
                    "source": "exact_status_counts",
                    "kind": "DashboardReadError",
                    "message": "exact status counts provider returned a non-object",
                })
            exact_counts = None

    if exact_counts is not None:
        status_counts = {
            status: int(exact_counts.get(status) or 0) for status in ALL_CANONICAL_STATUSES
        }
    else:
        status_counts = {status: len(task_groups[status]) for status in ACTIVE_STATUSES}
        for s in ("blocked", "finished", "archived"):
            value = _safe_read(
                f"tasks.{s}",
                partial(data_provider.list_tasks, s),
                errors,
                [],
            )
            status_counts[s] = len(value)

    status_counts["stale"] = len(stale_tasks)
    # Active = only non-archived pending+processing+review, exact.
    status_counts["active"] = (
        status_counts["pending"] + status_counts["processing"] + status_counts["review"]
    )

    # Bounded-row-list truncation metadata: how many rows the returned
    # snapshot actually carries per status vs. the exact authoritative
    # total, so a consumer can tell "500 shown" apart from "500 total".
    row_counts: dict[str, dict[str, Any]] = {}
    for status in ACTIVE_STATUSES:
        returned = len(task_groups[status])
        exact = status_counts.get(status, returned)
        row_counts[status] = {
            "returned": returned,
            "exact": exact,
            "truncated": returned < exact,
        }
    for status in ("blocked", "finished", "archived"):
        exact = status_counts.get(status, 0)
        row_counts[status] = {"returned": 0, "exact": exact, "truncated": exact > 0}

    source_graph_telemetry = _source_graph_telemetry(process_report)
    read_efficiency_telemetry = _read_efficiency_telemetry(process_report)
    project_context_telemetry = _project_context_telemetry(process_report)
    cost_totals = _cost_totals(ledger)
    kpi_analytics = dashboard_kpis.build_kpi_snapshot(
        process_report=kpi_process_report,
        source_graph=_source_graph_telemetry(kpi_process_report),
        project_context=_project_context_telemetry(kpi_process_report),
        callback_health=callback_bridge_health,
        cost_totals=cost_totals,
        process_limit=kpi_process_limit,
        manager_decision_totals=_safe_read(
            "manager_decision_counts",
            getattr(data_provider, "get_manager_decision_counts", lambda: {}),
            errors,
            {},
        ),
    )

    return {
        "schema_version": 1,
        "generated_at": _utc_now(),
        "readonly": True,
        "storage": storage_state,
        "storage_usage": storage_usage,
        "health": {
            "ok": not errors,
            "degraded": bool(errors),
            "provider_error_count": len(errors),
        },
        "status_counts": status_counts,
        "row_counts": row_counts,
        "tasks": {
            **task_groups,
            "stale": stale_tasks,
        },
        "summaries": {
            "topics": _build_dimension_summary(task_groups, "topic", stale_ids),
            "runners": _build_dimension_summary(task_groups, "runner", stale_ids),
        },
        "completion_inbox": dict(inbox),
        "cost_usage": {
            "totals": cost_totals,
            "ledger": dict(ledger),
        },
        "collision_report": dict(collision_report),
        "agent_processes": dict(process_report),
        "adapter_readiness": dict(adapter_readiness),
        "environment_preflight": dict(environment_preflight),
        "workforce_catalog": dict(workforce),
        "source_graph_telemetry": source_graph_telemetry,
        "read_efficiency_telemetry": read_efficiency_telemetry,
        "project_context_telemetry": project_context_telemetry,
        "kpi_analytics": kpi_analytics,
        "callback_bridge_health": dict(callback_bridge_health),
        "task_plan": dict(task_plan_snapshot),
        "warnings": {
            "stale": stale_tasks,
            "collisions": collision_warnings,
            "runner_mismatches": mismatch_warnings,
        },
        "errors": errors,
    }


def build_task_detail(task_id: str, provider: Any | None = None) -> dict[str, Any] | None:
    data_provider = provider or DashboardProvider()
    readiness_reader = getattr(data_provider, "get_storage_readiness", None)
    if readiness_reader is not None:
        readiness = readiness_reader()
        if not getattr(readiness, "ready", True):
            # Fail closed: never read a task's detail out of an
            # unverified/uninitialized store.
            return None
    card = data_provider.get_task(task_id)
    if card is None:
        return None
    if not isinstance(card, Mapping):
        raise DashboardReadError("task provider returned a non-object")
    result: dict[str, Any] = {
        "generated_at": _utc_now(),
        "readonly": True,
        "task": dict(card),
    }
    # Surface archived_at from the card so the dashboard knows at a glance
    archived_at = str(card.get("archived_at") or "").strip()
    result["task"]["archived_at"] = archived_at
    terminal_review = card.get("terminal_review")
    terminal_evidence: Mapping[str, Any] = {}
    if isinstance(terminal_review, Mapping):
        evidence = terminal_review.get("evidence")
        if isinstance(evidence, Mapping):
            terminal_evidence = evidence
        quality = evidence.get("quality_gate") if isinstance(evidence, Mapping) else None
        if isinstance(quality, Mapping):
            quality_projection: dict[str, Any] = {
                "schema_id": str(quality.get("schema_id") or "")[:100],
                "passed": bool(quality.get("passed")),
                "blocking_checks": [str(v)[:200] for v in (quality.get("blocking_checks") or [])[:40]],
                "config_error": str(quality.get("config_error") or "")[:500],
                "checks": [
                    {
                        "check_id": str(item.get("check_id") or "")[:200],
                        "kind": str(item.get("kind") or "")[:80],
                        "status": str(item.get("status") or "")[:80],
                        "summary": str(item.get("summary") or "")[:500],
                    }
                    for item in (quality.get("checks") or [])[:80]
                    if isinstance(item, Mapping)
                ],
            }
            verdict = quality.get("quality_verdict")
            if isinstance(verdict, Mapping):
                risk = verdict.get("risk_profile")
                quality_projection["quality_verdict"] = {
                    "schema_id": str(verdict.get("schema_id") or "")[:100],
                    "status": str(verdict.get("status") or "unverified")[:80],
                    "passed": bool(verdict.get("passed")),
                    "refine_required": bool(verdict.get("refine_required")),
                    "risk_tier": (
                        str(risk.get("effective_tier") or "unknown")[:40]
                        if isinstance(risk, Mapping)
                        else "unknown"
                    ),
                    "blocking_evidence": [
                        str(value)[:200]
                        for value in (verdict.get("blocking_evidence") or [])[:40]
                    ],
                    "lenses": [
                        {
                            "lens": str(item.get("lens") or "")[:80],
                            "status": str(item.get("status") or "")[:80],
                            "evidence_count": (
                                len(item.get("evidence_ids"))
                                if isinstance(item.get("evidence_ids"), list)
                                else 0
                            ),
                            "finding_count": (
                                len(item.get("finding_ids"))
                                if isinstance(item.get("finding_ids"), list)
                                else 0
                            ),
                        }
                        for item in (verdict.get("lenses") or [])[:12]
                        if isinstance(item, Mapping)
                    ],
                }
            result["task"]["quality_gate"] = quality_projection
    process_reader = getattr(
        data_provider,
        "get_agent_processes",
        lambda: {"ok": True, "processes": [], "total_requests": 0},
    )
    try:
        process_report = process_reader()
    except Exception:
        process_report = {}
    selected_process: Mapping[str, Any] = {}
    if isinstance(process_report, Mapping):
        for row in process_report.get("processes") or []:
            if not isinstance(row, Mapping) or str(row.get("task_id") or "") != task_id:
                continue
            if row.get("model"):
                result["task"]["model"] = str(row["model"])[:120]
            if row.get("adapter_id"):
                result["task"]["adapter_id"] = str(row["adapter_id"])[:120]
            if row.get("ai_infra_context"):
                result["task"]["ai_infra_context"] = row["ai_infra_context"]
            selected_process = row
            break

    event_reader = getattr(data_provider, "get_task_events", lambda _task_id: [])
    try:
        raw_events = event_reader(task_id)
    except Exception:
        raw_events = []
    events = [item for item in raw_events if isinstance(item, Mapping)][-100:]
    changed_hashes = terminal_evidence.get("changed_path_hashes")
    if not isinstance(changed_hashes, Mapping):
        changed_hashes = {}
    validations = terminal_evidence.get("validation")
    if not isinstance(validations, list):
        validations = []
    required_outputs = terminal_evidence.get("required_outputs")
    if not isinstance(required_outputs, list):
        required_outputs = []
    validation_summary = [
        {
            "command": _portable_text(item.get("command") or item.get("check_id"), 500),
            "ok": bool(item.get("ok", item.get("passed", False))),
            "returncode": item.get("returncode"),
            "summary": _portable_text(
                item.get("summary") or item.get("stderr") or item.get("stdout") or "",
                1000,
            ),
        }
        for item in validations[:200]
        if isinstance(item, Mapping)
    ]
    required_output_summary = [
        {
            "path": _portable_path(item.get("path")),
            "sha256": str(item.get("sha256") or "")[:128],
            "bytes": _bounded_int(item.get("bytes") or item.get("size")),
            "unchanged_allowed": bool(item.get("unchanged_allowed")),
        }
        if isinstance(item, Mapping)
        else {"path": _portable_path(item)}
        for item in required_outputs[:200]
    ]
    artifact_values: list[str] = []
    for source in (
        card.get("artifacts"),
        card.get("artifact_paths"),
        card.get("required_outputs"),
        terminal_evidence.get("artifacts"),
        terminal_evidence.get("artifact_paths"),
    ):
        values = source if isinstance(source, list) else [source] if isinstance(source, str) else []
        for value in values:
            if isinstance(value, Mapping):
                value = value.get("path") or value.get("artifact") or ""
            text = _portable_path(value)
            if text and text not in artifact_values:
                artifact_values.append(text)
    approval_history: list[dict[str, Any]] = []
    for item in events:
        event_name = str(item.get("event") or item.get("action") or "").strip()
        if not event_name:
            continue
        payload = item.get("payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                payload = {}
        if not isinstance(payload, Mapping):
            payload = {}
        approval_history.append(
            {
                "event": event_name[:80],
                "timestamp": str(item.get("timestamp") or item.get("created_at") or "")[:80],
                "actor": str(item.get("runner") or payload.get("actor") or "")[:120],
                "from_state": str(item.get("from_state") or payload.get("from_state") or "")[:80],
                "to_state": str(item.get("to_state") or payload.get("to_state") or "")[:80],
                "reason": _portable_text(
                    item.get("reason")
                    or item.get("error")
                    or payload.get("reason")
                    or payload.get("error"),
                    300,
                ),
            }
        )
    result["task"]["review_evidence_bundle"] = {
        "schema_id": "aiworkhub.review_evidence_bundle.v1",
        "task_id": task_id,
        "generated_at": result["generated_at"],
        "terminal": {
            "status": str(card.get("status") or "")[:80],
            "worker_status": str(card.get("worker_status") or "")[:80],
            "substatus": str(
                card.get("terminal_substatus")
                or (terminal_review.get("substatus") if isinstance(terminal_review, Mapping) else "")
                or ""
            )[:120],
            "process_state": str(selected_process.get("state") or "")[:80],
            "exit_code": selected_process.get("exit_code"),
            "error": str(
                _portable_text(
                    terminal_evidence.get("error") or selected_process.get("error"), 500
                )
            ),
        },
        "diff": {
            "changed_paths": [
                _portable_path(value)
                for value in (
                    terminal_evidence.get("changed_paths")
                    if isinstance(terminal_evidence.get("changed_paths"), list)
                    else []
                )[:500]
            ],
            "changed_path_hashes": {
                _portable_path(key): str(value)[:128]
                for key, value in list(changed_hashes.items())[:500]
            },
            "promoted_paths": [
                _portable_path(value)
                for value in (
                    terminal_evidence.get("promoted_paths")
                    if isinstance(terminal_evidence.get("promoted_paths"), list)
                    else []
                )[:500]
            ],
        },
        "tests": validation_summary,
        "required_outputs": required_output_summary,
        "logs": {
            "result_summary": _portable_text(
                card.get("completion_summary") or card.get("review_summary"), 2000
            ),
            "usage": {
                str(key)[:80]: value
                for key, value in list((selected_process.get("usage") or {}).items())[:80]
                if isinstance(value, (bool, int, float))
            }
            if isinstance(selected_process.get("usage"), Mapping)
            else {},
        },
        "artifacts": artifact_values[:500],
        "approvals": approval_history,
    }
    return result
