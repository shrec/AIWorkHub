from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aiworkhub import core, launch_queue_persist, task_store


USAGE_LINE_RE = re.compile(
    r"^(?P<task_id>\S+) \| runner=(?P<runner>[^|]+) \| topic=(?P<topic>[^|]+) "
    r"\| records=(?P<records>\d+) \| tokens=(?P<tokens>\d+) "
    r"\| in=(?P<input_tokens>\d+) out=(?P<output_tokens>\d+) \| cost=\$(?P<cost>[0-9.]+)"
)


def _parse_usage_stdout(stdout: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in (stdout or "").splitlines():
        match = USAGE_LINE_RE.match(line.strip())
        if not match:
            continue
        data = match.groupdict()
        rows.append({
            "source": "taskctl_usage_report",
            "task_id": data["task_id"],
            "runner": data["runner"].strip(),
            "topic": data["topic"].strip(),
            "model": "",
            "records": int(data["records"]),
            "input_tokens": int(data["input_tokens"]),
            "output_tokens": int(data["output_tokens"]),
            "total_tokens": int(data["tokens"]),
            "cached_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_metrics_observed": False,
            "cost_usd": float(data["cost"]),
            "cost_known": not (
                int(data["tokens"]) > 0 and float(data["cost"]) == 0.0
            ),
            "day": "",
        })
    return rows


def _launch_rows(summary: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for entry in summary.get("last_entries", []) or []:
        task_id = entry.get("task_id")
        if not task_id:
            continue
        ts = entry.get("requested_at") or entry.get("ts") or ""
        day = ""
        if isinstance(ts, str) and len(ts) >= 10:
            day = ts[:10]
        elif isinstance(ts, (float, int)):
            day = datetime.fromtimestamp(float(ts), tz=timezone.utc).date().isoformat()
        rows.append({
            "source": "launch_queue_audit",
            "task_id": task_id,
            "runner": entry.get("runner") or "",
            "topic": entry.get("topic") or "",
            "model": entry.get("model") or "",
            "records": 1,
            "input_tokens": int(entry.get("usage_input_tokens") or 0),
            "output_tokens": int(entry.get("usage_output_tokens") or 0),
            "total_tokens": int(entry.get("usage_total_tokens") or 0),
            "cached_input_tokens": int(entry.get("usage_cached_input_tokens") or 0),
            "cache_creation_input_tokens": int(entry.get("usage_cache_creation_input_tokens") or 0),
            "cache_metrics_observed": bool(entry.get("usage_cache_metrics_observed")),
            "cost_usd": float(entry.get("cost_usd") or 0.0),
            "cost_known": not (
                int(entry.get("usage_total_tokens") or 0) > 0
                and float(entry.get("cost_usd") or 0.0) == 0.0
            ),
            "day": day,
        })
    return rows


def _canonical_usage_rows(repo_root: Path | str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for entry in task_store.list_usage_events(repo_root, limit=10_000):
        created_at = str(entry.get("created_at") or "")
        total_tokens = int(entry.get("total_tokens") or 0)
        cost_usd = float(entry.get("cost_usd") or 0.0)
        cost_observed = entry.get("cost_observed")
        note = str(entry.get("note") or "")
        rows.append({
            "source": "canonical_usage_event",
            "task_id": str(entry.get("task_id") or ""),
            "runner": str(entry.get("runner") or ""),
            "topic": str(entry.get("topic") or ""),
            "model": str(entry.get("model") or ""),
            "provider": str(entry.get("provider") or ""),
            "records": int(entry.get("records") or 1),
            "input_tokens": int(entry.get("input_tokens") or 0),
            "output_tokens": int(entry.get("output_tokens") or 0),
            "total_tokens": total_tokens,
            "cached_input_tokens": int(entry.get("cached_input_tokens") or 0),
            "cache_creation_input_tokens": int(entry.get("cache_creation_input_tokens") or 0),
            "cache_metrics_observed": bool(entry.get("cache_metrics_observed")),
            "cost_usd": cost_usd,
            "cost_observed": bool(cost_observed),
            "cost_known": (
                bool(cost_observed)
                if cost_observed is not None
                else not (total_tokens > 0 and cost_usd == 0.0)
            ),
            "source_detail": str(entry.get("source") or ""),
            "note": note,
            "attempt_id": note.removeprefix("task_mcp_request:") if note.startswith("task_mcp_request:") else "",
            "day": created_at[:10] if len(created_at) >= 10 else "",
        })
    return rows


def _aggregate(rows: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "records": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cached_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_observed_records": 0,
        "cache_eligible_input_tokens": 0,
        "cost_usd": 0.0,
        "cost_known_records": 0,
        "cost_unknown_records": 0,
        "tokens_with_unknown_cost": 0,
    })
    for row in rows:
        bucket = str(row.get(key) or "unknown")
        out[bucket]["records"] += int(row.get("records") or 0)
        out[bucket]["input_tokens"] += int(row.get("input_tokens") or 0)
        out[bucket]["output_tokens"] += int(row.get("output_tokens") or 0)
        out[bucket]["total_tokens"] += int(row.get("total_tokens") or 0)
        out[bucket]["cached_input_tokens"] += int(row.get("cached_input_tokens") or 0)
        out[bucket]["cache_creation_input_tokens"] += int(
            row.get("cache_creation_input_tokens") or 0
        )
        if row.get("cache_metrics_observed"):
            out[bucket]["cache_observed_records"] += int(row.get("records") or 0)
            out[bucket]["cache_eligible_input_tokens"] += int(row.get("input_tokens") or 0)
        out[bucket]["cost_usd"] = round(out[bucket]["cost_usd"] + float(row.get("cost_usd") or 0.0), 6)
        if row.get("cost_known"):
            out[bucket]["cost_known_records"] += int(row.get("records") or 0)
        else:
            out[bucket]["cost_unknown_records"] += int(row.get("records") or 0)
            out[bucket]["tokens_with_unknown_cost"] += int(row.get("total_tokens") or 0)
    for aggregate in out.values():
        denominator = int(aggregate["cache_eligible_input_tokens"] or 0)
        aggregate["cache_hit_ratio"] = (
            round(int(aggregate["cached_input_tokens"] or 0) / denominator, 6)
            if denominator > 0
            else None
        )
    return dict(sorted(out.items()))


def build_cost_ledger(
    *,
    repo_root: Path | str | None = None,
    runner: str | None = None,
    topic: str | None = None,
    status: str | None = None,
    include_tasks: bool = False,
) -> dict[str, Any]:
    if repo_root is not None:
        usage_rows = _canonical_usage_rows(repo_root)
        if runner:
            usage_rows = [row for row in usage_rows if row.get("runner") == runner]
        if topic:
            usage_rows = [row for row in usage_rows if row.get("topic") == topic]
        if status:
            usage_rows = [row for row in usage_rows if row.get("status") == status]
        # The persisted launch queue is process-global and carries no complete
        # repository identity.  Never mix it into an explicitly repo-bound
        # catalog; canonical usage events are the sole authority here.
        usage = {"ok": True}
        launch_summary = {"ok": True}
        launch_rows: list[dict[str, Any]] = []
    else:
        usage = core.usage_report(runner=runner, topic=topic, status=status)
        usage_stdout = usage.get("stdout", "")
        usage_rows = _parse_usage_stdout(
            usage_stdout if isinstance(usage_stdout, str) else ""
        )
        launch_summary = launch_queue_persist.read_persisted_log(max_entries=10_000)
        launch_rows = _launch_rows(launch_summary)

    if repo_root is not None:
        # Every canonical usage event is a real attempt. Collapsing on
        # task/runner/model hides retries and under-reports their cost.
        union_rows = list(usage_rows)
    else:
        seen: set[tuple[str, str, str]] = set()
        union_rows = []
        for row in usage_rows + launch_rows:
            ident = (
                str(row.get("task_id") or ""),
                str(row.get("runner") or ""),
                str(row.get("model") or ""),
            )
            if ident in seen:
                continue
            seen.add(ident)
            union_rows.append(row)

    return {
        "tool": "aiworkhub_task_cost_ledger",
        "contract": "B288_v1_readonly_cost_ledger",
        "readonly": True,
        "filters": {"runner": runner, "topic": topic, "status": status},
        "counts": {
            "usage_rows": len(usage_rows),
            "launch_rows": len(launch_rows),
            "union_rows": len(union_rows),
        },
        "cost_quality": {
            "known_records": sum(int(row.get("records") or 0) for row in union_rows if row.get("cost_known")),
            "unknown_records": sum(int(row.get("records") or 0) for row in union_rows if not row.get("cost_known")),
            "tokens_with_unknown_cost": sum(
                int(row.get("total_tokens") or 0)
                for row in union_rows if not row.get("cost_known")
            ),
            "zero_cost_is_free": False,
            "reason": "provider_cost_absence_is_unknown_not_zero",
        },
        "cache_quality": {
            "observed_records": sum(
                int(row.get("records") or 0)
                for row in union_rows
                if row.get("cache_metrics_observed")
            ),
            "cached_input_tokens": sum(
                int(row.get("cached_input_tokens") or 0) for row in union_rows
            ),
            "cache_creation_input_tokens": sum(
                int(row.get("cache_creation_input_tokens") or 0) for row in union_rows
            ),
            "absent_metrics_are_unknown_not_zero": True,
        },
        "aggregates": {
            "by_topic": _aggregate(union_rows, "topic"),
            "by_runner": _aggregate(union_rows, "runner"),
            "by_model": _aggregate(union_rows, "model"),
            "by_provider": _aggregate(union_rows, "provider"),
            "by_day": _aggregate(union_rows, "day"),
        },
        "tasks": union_rows if include_tasks else [],
        "authority_flags": {
            "runtime_authority": False,
            "support_authority": False,
            "queue_write": False,
            "audit_write": False,
            "process_launch": False,
            "agent_launch": False,
        },
        "source_status": {
            "usage_report_ok": bool(usage.get("ok")),
            "launch_log_ok": bool(launch_summary.get("ok")),
        },
    }
