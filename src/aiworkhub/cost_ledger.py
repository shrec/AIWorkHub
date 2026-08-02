from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from aiworkhub import core, launch_queue_persist


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
            "cost_usd": float(entry.get("cost_usd") or 0.0),
            "cost_known": not (
                int(entry.get("usage_total_tokens") or 0) > 0
                and float(entry.get("cost_usd") or 0.0) == 0.0
            ),
            "day": day,
        })
    return rows


def _aggregate(rows: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "records": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
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
        out[bucket]["cost_usd"] = round(out[bucket]["cost_usd"] + float(row.get("cost_usd") or 0.0), 6)
        if row.get("cost_known"):
            out[bucket]["cost_known_records"] += int(row.get("records") or 0)
        else:
            out[bucket]["cost_unknown_records"] += int(row.get("records") or 0)
            out[bucket]["tokens_with_unknown_cost"] += int(row.get("total_tokens") or 0)
    return dict(sorted(out.items()))


def build_cost_ledger(
    *,
    runner: str | None = None,
    topic: str | None = None,
    status: str | None = None,
    include_tasks: bool = False,
) -> dict[str, Any]:
    usage = core.usage_report(runner=runner, topic=topic, status=status)
    usage_rows = _parse_usage_stdout(usage.get("stdout", ""))
    launch_summary = launch_queue_persist.read_persisted_log(max_entries=10_000)
    launch_rows = _launch_rows(launch_summary)

    seen: set[tuple[str, str, str]] = set()
    union_rows: list[dict[str, Any]] = []
    for row in usage_rows + launch_rows:
        ident = (str(row.get("task_id") or ""), str(row.get("runner") or ""), str(row.get("model") or ""))
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
        "aggregates": {
            "by_topic": _aggregate(union_rows, "topic"),
            "by_runner": _aggregate(union_rows, "runner"),
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
