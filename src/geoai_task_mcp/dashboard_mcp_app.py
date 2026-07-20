"""Narrow, read-only MCP tool surface for the native VS Code Task Operations app.

B615 replaces the iframe-embedded HTTP dashboard with a native VS Code
Webview whose extension host talks to the Task MCP server over stdio. These
three tools are the ENTIRE data surface the Webview needs: they call
``dashboard.build_snapshot()`` / ``dashboard.build_task_detail()`` -- the
same canonical builders the existing HTTP dashboard's ``/api/snapshot`` and
``/api/task`` routes already use -- and add nothing except a defensive
transport-size bound and task_id validation. No SQLite/taskctl read is
duplicated here; no code path in this module can write queue/audit state or
launch a process.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from geoai_task_mcp import __version__, core, dashboard


# Hard bound on the serialized tool response so a very large queue (many
# thousands of process-log rows, a big cost ledger) can never balloon a
# single MCP stdio response. This defends the transport only -- it never
# changes what build_snapshot()/build_task_detail() compute, only how much of
# it survives one tool call. Every drop is reported, never silent.
MAX_SNAPSHOT_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_TASK_DETAIL_RESPONSE_BYTES = 1 * 1024 * 1024

# Largest / least essential first. status_counts, tasks, and row_counts are
# never trimmed by this list -- they are what the summary strip and task
# table need on every refresh.
_SNAPSHOT_TRIM_ORDER: tuple[str, ...] = (
    "agent_processes",
    "cost_usage",
    "completion_inbox",
    "collision_report",
    "callback_bridge_health",
    "summaries",
    "warnings",
)

# Detail fields trimmed, largest first, only if the single-task payload is
# still over budget (a huge validation_output/result blob on one card).
_DETAIL_TRIM_FIELDS: tuple[str, ...] = (
    "result",
    "worker_result",
    "completion_summary",
    "review_summary",
    "review_notes",
    "validation_output",
    "ai_infra_context",
)


def _readonly_authority_flags() -> dict[str, bool]:
    return {
        "readonly": True,
        "queue_write": False,
        "audit_write": False,
        "process_launch": False,
        "agent_launch": False,
        "shell_invocation": False,
    }


def _byte_len(payload: Mapping[str, Any]) -> int:
    return len(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
    )


def _bound_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Trim the largest secondary sections until the snapshot fits the bound.

    Never mutates the input; returns a shallow copy with 0+ fields replaced
    by ``{"transport_truncated": true}`` and every trimmed field name
    reported in ``transport_truncated_fields``.
    """
    result = dict(snapshot)
    truncated: list[str] = []
    for field in _SNAPSHOT_TRIM_ORDER:
        if _byte_len(result) <= MAX_SNAPSHOT_RESPONSE_BYTES:
            break
        if result.get(field):
            result[field] = {"transport_truncated": True}
            truncated.append(field)
    if truncated:
        result["transport_truncated_fields"] = truncated
    return result


def _bound_task_detail(detail: Mapping[str, Any]) -> dict[str, Any]:
    """Trim the largest task fields until one task's detail fits the bound."""
    result = dict(detail)
    if _byte_len(result) <= MAX_TASK_DETAIL_RESPONSE_BYTES:
        return result
    task = dict(result.get("task") or {})
    truncated: list[str] = []
    for field in _DETAIL_TRIM_FIELDS:
        if _byte_len({**result, "task": task}) <= MAX_TASK_DETAIL_RESPONSE_BYTES:
            break
        if field in task:
            task[field] = "(transport_truncated)"
            truncated.append(field)
    result["task"] = task
    if truncated:
        result["transport_truncated_fields"] = truncated
    return result


def snapshot_view() -> dict[str, Any]:
    """READ-ONLY: canonical dashboard snapshot for the native Webview.

    Calls ``dashboard.build_snapshot()`` -- the SAME builder the HTTP
    dashboard's ``/api/snapshot`` route uses -- and returns the identical
    ``status_counts``, ``tasks``, ``row_counts``, ``summaries``,
    ``cost_usage``, ``agent_processes``, and ``warnings`` shape the existing
    dashboard.js already renders. Adds no second SQLite/taskctl read; only
    applies the defensive transport bound documented on
    ``MAX_SNAPSHOT_RESPONSE_BYTES``.
    """
    snapshot = dict(dashboard.build_snapshot())
    snapshot["server_tool"] = "geoai_dashboard_snapshot"
    snapshot["authority_flags"] = _readonly_authority_flags()
    return _bound_snapshot(snapshot)


def task_detail_view(task_id: str) -> dict[str, Any]:
    """READ-ONLY: canonical detail for exactly one bounded task_id.

    Validates ``task_id`` with the identical pattern the HTTP dashboard's
    ``/api/task`` route enforces (``dashboard._TASK_ID_RE``) BEFORE ever
    calling the provider, then calls ``dashboard.build_task_detail()`` -- the
    same canonical builder ``/api/task`` uses. An invalid or unknown task_id
    returns a bounded ``ok: false`` object; this never raises and never
    reaches a write path.
    """
    candidate = str(task_id or "").strip()
    if not dashboard._TASK_ID_RE.fullmatch(candidate):
        return {
            "ok": False,
            "error": "invalid_task_id",
            "server_tool": "geoai_dashboard_task_detail",
            "authority_flags": _readonly_authority_flags(),
        }
    detail = dashboard.build_task_detail(candidate)
    if detail is None:
        return {
            "ok": False,
            "error": "task_not_found",
            "task_id": candidate,
            "server_tool": "geoai_dashboard_task_detail",
            "authority_flags": _readonly_authority_flags(),
        }
    response = dict(detail)
    response["ok"] = True
    response["server_tool"] = "geoai_dashboard_task_detail"
    response["authority_flags"] = _readonly_authority_flags()
    return _bound_task_detail(response)


def health_view() -> dict[str, Any]:
    """READ-ONLY: cheap liveness check for the Webview's connection banner.

    Delegates to ``core.health()`` -- the same read-only check
    ``geoai_task_health`` already exposes -- and adds only the package
    version and this tool's own authority flags. Never calls
    ``dashboard.build_snapshot()``: polling this tool for a connection
    banner must stay cheap and must never pull the full queue/cost/process
    payload.
    """
    result = core.health()
    result["server_version"] = __version__
    result["server_tool"] = "geoai_dashboard_health"
    result["authority_flags"] = _readonly_authority_flags()
    return result


READONLY_TOOL_NAMES: tuple[str, ...] = (
    "geoai_dashboard_snapshot",
    "geoai_dashboard_task_detail",
    "geoai_dashboard_health",
)

# Stable tool_name -> callable map for server wiring / introspection.
READONLY_TOOLS: dict[str, Any] = {
    "geoai_dashboard_snapshot": snapshot_view,
    "geoai_dashboard_task_detail": task_detail_view,
    "geoai_dashboard_health": health_view,
}


def register(mcp: Any) -> tuple[str, ...]:
    """Register the three narrow dashboard MCP tools; return their names.

    Accepts any object exposing a FastMCP-style ``tool(name=...)`` decorator
    factory (mirrors ``cli_adapter_readonly_tool.register``). Every
    registered tool is read-only: none writes queue/audit state and none
    launches a process.
    """
    for name, fn in READONLY_TOOLS.items():
        mcp.tool(name=name)(fn)
    return READONLY_TOOL_NAMES
