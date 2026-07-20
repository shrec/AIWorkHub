"""Local, read-only HTTP dashboard for the GeoAI task queue.

The server intentionally exposes only GET/HEAD routes. Production data is
collected through the package's existing read helpers; no task mutation helper
is imported or called from this module.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import socket
import time
import webbrowser
from collections import defaultdict
from datetime import datetime, timezone
from functools import partial
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, unquote, urlsplit

from geoai_task_mcp import (
    completion_inbox,
    core,
    cost_ledger,
    deepseek_credentials,
    process_launcher,
)


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_TASK_LIMIT = 500
DEFAULT_PROCESS_LIMIT = 200
MAX_PROCESS_LOG_BYTES = 4 * 1024 * 1024
ACTIVE_STATUSES = ("pending", "processing", "review")
# The full canonical-status taxonomy (AITools.taskdb.canonical_status), used
# for exact whole-queue totals -- independent of any bounded row limit.
ALL_CANONICAL_STATUSES = ("pending", "processing", "review", "blocked", "finished", "archived")
STATIC_DIR = Path(__file__).resolve().with_name("dashboard_static")
# Minimal frame-ancestors allowlist for the explicit VS Code embed document
# only: the desktop Electron webview scheme, and the web/Remote webview
# resource-domain VS Code (vscode.dev / github.dev / tunneled Remote-SSH)
# uses. Every other route keeps frame-ancestors 'none'.
VSCODE_EMBED_FRAME_ANCESTORS = "vscode-webview: https://*.vscode-cdn.net"
VSCODE_EMBED_PATH = "/embed/vscode"
STATIC_ASSETS = {
    "index.html": "text/html; charset=utf-8",
    "dashboard.css": "text/css; charset=utf-8",
    "dashboard.js": "text/javascript; charset=utf-8",
}

_LIST_LINE_RE = re.compile(
    r"^\s*\[(?P<status>[^\]]+)\]\s*"
    r"\[(?P<topic>[^\]]+)\]\s*"
    r"\[(?P<runner>[^\]]+)\]\s*(?P<task_id>\S+)"
    r"(?:\s+model=(?P<model>\S+))?"
    r"(?:\s+outcome=(?P<outcome>\S+))?\s*$"
)
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
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


def _compact_ai_infra(event: Mapping[str, Any]) -> dict[str, Any]:
    context = event.get("project_context")
    delivery = event.get("project_context_delivery")
    ack = event.get("project_context_acknowledgement")
    if not isinstance(context, Mapping) and not isinstance(delivery, Mapping) and not isinstance(ack, Mapping):
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
            estimate = {
                "label": str(raw_estimate.get("label") or "")[:80],
                "raw_context_bytes": int(raw_estimate.get("raw_context_bytes") or 0),
                "bundle_bytes": int(raw_estimate.get("bundle_bytes") or 0),
                "delta_bytes": int(raw_estimate.get("delta_bytes") or 0),
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
    }


class DashboardReadError(RuntimeError):
    """A bounded failure from an existing read-only provider."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bounded_text(value: Any, limit: int = 300) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit] or "unknown read failure"


def _secure_compare(a: str, b: str) -> bool:
    """Constant-time string comparison for coordinator token validation."""
    if len(a) != len(b):
        return False
    result = 0
    for x, y in zip(a, b):
        result |= ord(x) ^ ord(y)
    return result == 0


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
            str(core.repo_root() / "tools/geoai-task-mcp/logs/process_events.jsonl"),
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
            compact["ai_infra_context"] = ai_infra
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
        status_path_raw = status_paths.get(str(row.get("request_id") or ""))
        if status_path_raw:
            liveness_fields = _process_liveness_fields(row, status_path_raw)
            if liveness_fields:
                row.update(liveness_fields)
        if row.get("state") not in process_launcher.ACTIVE_PROCESS_STATES:
            continue
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


def exact_status_counts(db_path: Path | None = None) -> dict[str, int]:
    """Exact per-canonical-status totals across the whole task queue.

    Reads only the narrow ``(archived_at, status, worker_status)`` columns
    from the authoritative SQLite task table -- never ``card_json``, and
    never a row/list payload -- so a bucket (e.g. ``finished``) holding many
    thousands of cards is still counted exactly instead of being capped at
    ``DEFAULT_TASK_LIMIT``. Applies the same ``canonical_status`` precedence
    taskctl/dashboard.js already use, so this never invents a second status
    taxonomy. A pure ``SELECT``: never inserts, updates, or deletes.
    """
    from AITools.taskdb import DEFAULT_DB, canonical_status, init_db, open_db

    counts = {status: 0 for status in ALL_CANONICAL_STATUSES}
    with open_db(db_path or DEFAULT_DB) as conn:
        init_db(conn)
        rows = conn.execute(
            "SELECT archived_at, status, worker_status FROM tasks"
        ).fetchall()
    for row in rows:
        counts[canonical_status(dict(row))] += 1
    return counts


class DashboardProvider:
    """Adapter over the package's existing read-only queue providers."""

    def __init__(
        self,
        *,
        task_limit: int = DEFAULT_TASK_LIMIT,
        stale_processing_hours: float = completion_inbox.DEFAULT_STALE_PROCESSING_HOURS,
    ) -> None:
        self.task_limit = max(1, min(int(task_limit), completion_inbox.MAX_LIMIT))
        self.stale_processing_hours = max(0.0, float(stale_processing_hours))

    def list_tasks(self, status: str) -> list[dict[str, Any]]:
        result = core.list_tasks(status=status, limit=self.task_limit)
        return parse_task_list(_taskctl_stdout(result, f"task list ({status})"), status)

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        result = core.show_task(task_id)
        stdout = _taskctl_stdout(result, f"task show ({task_id})").strip()
        if not stdout or "Task not found:" in stdout:
            return None
        try:
            card = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise DashboardReadError(f"task show ({task_id}) returned invalid JSON") from exc
        if not isinstance(card, dict):
            raise DashboardReadError(f"task show ({task_id}) returned a non-object")
        return card

    def get_completion_inbox(self) -> dict[str, Any]:
        return completion_inbox.build_completion_inbox(
            limit=self.task_limit,
            stale_processing_hours=self.stale_processing_hours,
        )

    def get_cost_ledger(self) -> dict[str, Any]:
        return cost_ledger.build_cost_ledger(include_tasks=False)

    def get_collision_report(self) -> dict[str, Any]:
        result = core.collision_guard(print_json=True)
        if not isinstance(result, Mapping):
            raise DashboardReadError("collision guard returned an invalid result")
        stdout = str(result.get("stdout") or "")
        report = _extract_json_object(stdout)
        if report is not None:
            return report
        if "No cards to scan." in stdout and result.get("returncode") in (0, None):
            return {
                "collision_free": True,
                "active_cards": 0,
                "collision_count": 0,
                "file_collisions": [],
            }
        _taskctl_stdout(result, "collision guard")
        raise DashboardReadError("collision guard returned no JSON report")

    def get_agent_processes(self) -> dict[str, Any]:
        return read_process_runs(limit=DEFAULT_PROCESS_LIMIT)

    def get_exact_status_counts(self) -> dict[str, int]:
        """Exact per-status totals across the whole queue (see
        ``exact_status_counts``) -- independent of ``task_limit``, never a
        per-row payload."""
        return exact_status_counts()

    def get_adapter_readiness(self) -> dict[str, Any]:
        """Read-only worker-adapter readiness (never exposes any secret)."""
        return deepseek_credentials.adapter_readiness(repo=core.repo_root())

    def get_callback_bridge_health(self) -> dict[str, Any]:
        """Read-only, redacted-safe callback bridge health: bound/unbound
        task counts, pending/inflight/delivered/dead-letter outbox counts,
        last delivery, and a non-secret blocker. Never exposes a full
        origin_thread_id -- ``callback_outbox_status`` sources this from
        ``AITools/taskdb.py::callback_outbox_stats``, which redacts at the
        source."""
        result = core.callback_outbox_status()
        stdout = _taskctl_stdout(result, "callback outbox status")
        report = _extract_json_object(stdout)
        if report is None:
            raise DashboardReadError("callback outbox status returned no JSON report")
        return report


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
    totals["cost_usd"] = round(totals["cost_usd"], 6)
    return totals


def build_snapshot(provider: Any | None = None) -> dict[str, Any]:
    """Build one dashboard snapshot while isolating every provider failure."""
    data_provider = provider or DashboardProvider()
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

    return {
        "schema_version": 1,
        "generated_at": _utc_now(),
        "readonly": True,
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
            "totals": _cost_totals(ledger),
            "ledger": dict(ledger),
        },
        "collision_report": dict(collision_report),
        "agent_processes": dict(process_report),
        "adapter_readiness": dict(adapter_readiness),
        "callback_bridge_health": dict(callback_bridge_health),
        "warnings": {
            "stale": stale_tasks,
            "collisions": collision_warnings,
            "runner_mismatches": mismatch_warnings,
        },
        "errors": errors,
    }


def build_task_detail(task_id: str, provider: Any | None = None) -> dict[str, Any] | None:
    data_provider = provider or DashboardProvider()
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
    process_reader = getattr(
        data_provider,
        "get_agent_processes",
        lambda: {"ok": True, "processes": [], "total_requests": 0},
    )
    try:
        process_report = process_reader()
    except Exception:
        process_report = {}
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
            break
    return result


def is_loopback_host(host: str) -> bool:
    candidate = str(host or "").strip()
    if candidate.lower() == "localhost":
        return True
    if candidate.startswith("[") and candidate.endswith("]"):
        candidate = candidate[1:-1]
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


def parse_loopback_authority(value: str) -> tuple[str, int | None] | None:
    """Parse a Host-style authority without resolving attacker-controlled DNS."""
    candidate = str(value or "").strip()
    if not candidate or any(character.isspace() for character in candidate):
        return None
    try:
        parsed = urlsplit(f"//{candidate}")
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if (
        not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        return None

    # Reject ambiguous authorities such as ``localhost:`` that urlsplit accepts.
    serialized_host = f"[{hostname}]" if ":" in hostname else hostname
    serialized = serialized_host if port is None else f"{serialized_host}:{port}"
    if candidate.lower() != serialized.lower():
        return None

    if hostname.lower() == "localhost":
        normalized_host = "localhost"
    else:
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            return None
        if not address.is_loopback:
            return None
        normalized_host = address.compressed
    return normalized_host, port


def parse_dashboard_authority(value: str) -> tuple[str, int | None] | None:
    """Parse a loopback authority or one exact operator-allowlisted IP.

    The server still binds loopback only.  This narrow exception supports a
    read-only LAN reverse proxy without accepting arbitrary Host headers.
    """
    loopback = parse_loopback_authority(value)
    if loopback is not None:
        return loopback

    candidate = str(value or "").strip()
    if not candidate or any(character.isspace() for character in candidate):
        return None
    try:
        parsed = urlsplit(f"//{candidate}")
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if (
        not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        return None
    try:
        normalized_host = ipaddress.ip_address(hostname).compressed
    except ValueError:
        return None
    serialized = f"[{normalized_host}]" if ":" in normalized_host else normalized_host
    if port is not None:
        serialized = f"{serialized}:{port}"
    if candidate.lower() != serialized.lower():
        return None

    allowed = {
        item.strip()
        for item in os.environ.get("GEOAI_TASK_DASHBOARD_ALLOWED_HOSTS", "").split(",")
        if item.strip()
    }
    return (normalized_host, port) if normalized_host in allowed else None


def origin_matches_authority(origin: str, authority: tuple[str, int | None]) -> bool:
    """Return whether an Origin header is the dashboard's exact HTTP origin."""
    candidate = str(origin or "").strip()
    if not candidate or any(character.isspace() for character in candidate):
        return False
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return False
    if (
        parsed.scheme.lower() != "http"
        or not parsed.netloc
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        return False
    origin_authority = parse_dashboard_authority(parsed.netloc)
    if origin_authority is None:
        return False
    host_name, host_port = authority
    origin_name, origin_port = origin_authority
    return (host_name, host_port or 80) == (origin_name, origin_port or 80)


def resolve_static_path(url_path: str, static_dir: Path = STATIC_DIR) -> tuple[Path, str] | None:
    """Resolve one allowlisted asset without permitting path traversal."""
    try:
        decoded = unquote(url_path, errors="strict")
    except UnicodeError:
        return None
    if "\x00" in decoded or "\\" in decoded:
        return None

    route = decoded
    if route == "/":
        relative = "index.html"
    elif route.startswith("/static/"):
        relative = route.removeprefix("/static/")
    else:
        relative = route.removeprefix("/")
    if relative not in STATIC_ASSETS:
        return None

    root = static_dir.resolve()
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate, STATIC_ASSETS[relative]


class DashboardHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class DashboardHTTPServerV6(DashboardHTTPServer):
    address_family = socket.AF_INET6


class DashboardRequestHandler(BaseHTTPRequestHandler):
    server_version = "GeoAITaskDashboard/1.0"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def version_string(self) -> str:
        return self.server_version

    def _send_bytes(
        self,
        status: int,
        body: bytes,
        content_type: str,
        *,
        send_body: bool,
        extra_headers: Mapping[str, str] | None = None,
        vscode_embed: bool = False,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        # Every ordinary page keeps the hard X-Frame-Options DENY. Only the
        # one explicit VS Code embed document (see VSCODE_EMBED_PATH) omits
        # it, relying solely on the narrowly scoped CSP frame-ancestors
        # below -- X-Frame-Options has no syntax for a multi-origin
        # allowlist, so setting DENY there too would simply break the
        # feature this route exists for.
        if not vscode_embed:
            self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Permitted-Cross-Domain-Policies", "none")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header(
            "Permissions-Policy",
            "accelerometer=(), camera=(), geolocation=(), microphone=(), payment=(), usb=()",
        )
        self.send_header("Cache-Control", "no-store")
        frame_ancestors = VSCODE_EMBED_FRAME_ANCESTORS if vscode_embed else "'none'"
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; connect-src 'self'; img-src 'self' data:; "
            "style-src 'self'; script-src 'self'; object-src 'none'; base-uri 'none'; "
            f"form-action 'none'; frame-ancestors {frame_ancestors}",
        )
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    def _send_json(
        self,
        status: int,
        payload: Mapping[str, Any],
        *,
        send_body: bool,
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        body = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        self._send_bytes(
            status,
            body,
            "application/json; charset=utf-8",
            send_body=send_body,
            extra_headers=extra_headers,
        )

    def _bad_request(self, message: str, *, send_body: bool) -> None:
        self._send_json(
            HTTPStatus.BAD_REQUEST,
            {"ok": False, "error": "bad_request", "message": message},
            send_body=send_body,
        )

    def _request_context_is_valid(self, *, send_body: bool) -> bool:
        host_headers = self.headers.get_all("Host", [])
        if len(host_headers) != 1:
            self._send_json(
                HTTPStatus.MISDIRECTED_REQUEST,
                {
                    "ok": False,
                    "error": "invalid_host",
                    "message": "exactly one loopback Host header is required",
                },
                send_body=send_body,
            )
            return False
        authority = parse_dashboard_authority(host_headers[0])
        if authority is None:
            self._send_json(
                HTTPStatus.MISDIRECTED_REQUEST,
                {
                    "ok": False,
                    "error": "invalid_host",
                    "message": "Host must be loopback or an explicitly allowlisted proxy address",
                },
                send_body=send_body,
            )
            return False

        origin_headers = self.headers.get_all("Origin", [])
        if len(origin_headers) > 1 or (
            origin_headers and not origin_matches_authority(origin_headers[0], authority)
        ):
            self._send_json(
                HTTPStatus.FORBIDDEN,
                {
                    "ok": False,
                    "error": "invalid_origin",
                    "message": "Origin must match the dashboard origin",
                },
                send_body=send_body,
            )
            return False
        return True

    def _serve_static(self, path: str, *, send_body: bool, vscode_embed: bool = False) -> None:
        resolved = resolve_static_path(path, self.server.static_dir)  # type: ignore[attr-defined]
        if resolved is None:
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"ok": False, "error": "not_found"},
                send_body=send_body,
            )
            return
        asset_path, content_type = resolved
        try:
            body = asset_path.read_bytes()
        except FileNotFoundError:
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"ok": False, "error": "not_found"},
                send_body=send_body,
            )
            return
        except OSError:
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"ok": False, "error": "static_asset_unavailable"},
                send_body=send_body,
            )
            return
        self._send_bytes(
            HTTPStatus.OK,
            body,
            content_type,
            send_body=send_body,
            vscode_embed=vscode_embed,
        )

    def _dispatch(self, *, send_body: bool) -> None:
        if not self._request_context_is_valid(send_body=send_body):
            return
        if len(self.path) > 2048:
            self._send_json(
                HTTPStatus.REQUEST_URI_TOO_LONG,
                {"ok": False, "error": "request_uri_too_long"},
                send_body=send_body,
            )
            return
        try:
            parsed = urlsplit(self.path)
        except ValueError:
            self._bad_request("invalid request target", send_body=send_body)
            return

        path = parsed.path
        if path == "/healthz":
            if parsed.query:
                self._bad_request("healthz does not accept query parameters", send_body=send_body)
                return
            self._send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "service": "geoai-task-dashboard",
                    "readonly": True,
                    "time": _utc_now(),
                },
                send_body=send_body,
            )
            return

        if path == "/api/snapshot":
            if parsed.query:
                self._bad_request("snapshot does not accept query parameters", send_body=send_body)
                return
            snapshot = build_snapshot(self.server.provider)  # type: ignore[attr-defined]
            self._send_json(HTTPStatus.OK, snapshot, send_body=send_body)
            return

        if path == "/api/health-identity":
            if parsed.query:
                self._bad_request(
                    "health identity does not accept query parameters",
                    send_body=send_body,
                )
                return
            try:
                from AITools.taskdb import db_identity_fingerprint, open_db, init_db, DEFAULT_DB
                with open_db(DEFAULT_DB) as conn:
                    init_db(conn)
                    identity = db_identity_fingerprint(conn)
                self._send_json(
                    HTTPStatus.OK,
                    {"ok": True, **identity},
                    send_body=send_body,
                )
            except Exception as exc:
                self._send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"ok": False, "error": "identity_unavailable", "message": _bounded_text(exc)},
                    send_body=send_body,
                )
            return

        if path == "/api/task":
            try:
                query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=4)
            except ValueError:
                self._bad_request("invalid task query", send_body=send_body)
                return
            if set(query) != {"id"} or len(query["id"]) != 1:
                self._bad_request("exactly one task id is required", send_body=send_body)
                return
            task_id = query["id"][0].strip()
            if not _TASK_ID_RE.fullmatch(task_id):
                self._bad_request("task id contains unsupported characters", send_body=send_body)
                return
            try:
                detail = build_task_detail(task_id, self.server.provider)  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001 - bounded provider failure
                self._send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {
                        "ok": False,
                        "error": "task_provider_unavailable",
                        "message": _bounded_text(exc),
                    },
                    send_body=send_body,
                )
                return
            if detail is None:
                self._send_json(
                    HTTPStatus.NOT_FOUND,
                    {"ok": False, "error": "task_not_found", "task_id": task_id},
                    send_body=send_body,
                )
                return
            self._send_json(HTTPStatus.OK, detail, send_body=send_body)
            return

        if path == VSCODE_EMBED_PATH:
            # Explicit, exact-path, no-query VS Code embed document. Same
            # index.html body as "/" -- dashboard.js/index.html are
            # unmodified and keep sourcing /api/* by absolute path -- but
            # served with the narrowly scoped embed frame policy instead of
            # the hard DENY every other route keeps. Query strings, other
            # paths, and Origin values never select this policy; only this
            # exact path does.
            if parsed.query:
                self._bad_request(
                    "vscode embed endpoint does not accept query parameters",
                    send_body=send_body,
                )
                return
            self._serve_static("/", send_body=send_body, vscode_embed=True)
            return

        self._serve_static(path, send_body=send_body)

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        self._dispatch(send_body=True)

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib handler API
        self._dispatch(send_body=False)

    def _verify_coordinator_token(self) -> str:
        """Read X-Coordinator-Token from request headers and validate.

        Returns empty string on success (token matched), or an error message
        on failure. Never logs or stores the token.
        """
        token_header = (
            (self.headers.get("X-Coordinator-Token") or "").strip()
        )
        if not token_header:
            return "missing X-Coordinator-Token header"

        try:
            from geoai_task_mcp import coordinator_config

            configured_token, configured_path = coordinator_config()
        except Exception:
            configured_token, configured_path = "", ""

        token_path = (
            Path(str(configured_path)).expanduser()
            if configured_path
            else Path.home() / ".config" / "geoai" / "taskctl_coordinator.token"
        ).resolve()
        try:
            file_token = token_path.read_text(encoding="utf-8").strip()
        except OSError:
            file_token = ""

        valid_token = file_token or configured_token
        if not valid_token:
            return "coordinator token not configured on server"

        # Constant-time comparison
        if not _secure_compare(token_header, valid_token):
            return "invalid coordinator token"

        return ""

    def _handle_archive(self, task_id: str, reason: str) -> tuple[int, dict]:
        """Run archive via taskctl and return (http_status, response_body)."""
        try:
            from AITools.taskdb import archive_task, open_db, init_db, DEFAULT_DB, is_archived
        except Exception as e:
            return HTTPStatus.SERVICE_UNAVAILABLE, {
                "ok": False, "error": "taskdb_unavailable", "message": _bounded_text(e),
            }
        try:
            with open_db(DEFAULT_DB) as conn:
                init_db(conn)
                ok, msg = archive_task(conn, task_id, actor="dashboard", reason=reason)
        except Exception as e:
            return HTTPStatus.INTERNAL_SERVER_ERROR, {
                "ok": False, "error": "archive_failed", "message": _bounded_text(e),
            }
        if ok:
            return HTTPStatus.OK, {"ok": True, "task_id": task_id, "message": msg}
        else:
            # Distinguish refused errors from not-found
            if msg == "task_not_found":
                return HTTPStatus.NOT_FOUND, {"ok": False, "error": msg, "task_id": task_id}
            return HTTPStatus.CONFLICT, {"ok": False, "error": msg, "task_id": task_id}

    def _handle_restore(self, task_id: str, reason: str) -> tuple[int, dict]:
        """Run restore via taskctl and return (http_status, response_body)."""
        try:
            from AITools.taskdb import restore_task, open_db, init_db, DEFAULT_DB
        except Exception as e:
            return HTTPStatus.SERVICE_UNAVAILABLE, {
                "ok": False, "error": "taskdb_unavailable", "message": _bounded_text(e),
            }
        try:
            with open_db(DEFAULT_DB) as conn:
                init_db(conn)
                ok, msg = restore_task(conn, task_id, actor="dashboard", reason=reason)
        except Exception as e:
            return HTTPStatus.INTERNAL_SERVER_ERROR, {
                "ok": False, "error": "restore_failed", "message": _bounded_text(e),
            }
        if ok:
            return HTTPStatus.OK, {"ok": True, "task_id": task_id, "message": msg}
        else:
            if msg == "task_not_found":
                return HTTPStatus.NOT_FOUND, {"ok": False, "error": msg, "task_id": task_id}
            return HTTPStatus.CONFLICT, {"ok": False, "error": msg, "task_id": task_id}

    def _dispatch_post(self, send_body: bool) -> None:
        """Handle POST requests for archive, restore, and health-identity."""
        if not self._request_context_is_valid(send_body=send_body):
            return
        if len(self.path) > 2048:
            self._send_json(
                HTTPStatus.REQUEST_URI_TOO_LONG,
                {"ok": False, "error": "request_uri_too_long"},
                send_body=send_body,
            )
            return
        try:
            parsed = urlsplit(self.path)
        except ValueError:
            self._bad_request("invalid request target", send_body=send_body)
            return

        path = parsed.path

        # ── archive/restore POST endpoints ──
        if path in ("/api/archive", "/api/restore"):
            token_error = self._verify_coordinator_token()
            if token_error:
                self._send_json(
                    HTTPStatus.UNAUTHORIZED,
                    {"ok": False, "error": token_error},
                    send_body=send_body,
                )
                return

            # Read JSON body
            content_length = 0
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
            except (TypeError, ValueError):
                pass
            if content_length <= 0 or content_length > 4096:
                self._bad_request("missing or oversized request body", send_body=send_body)
                return

            try:
                body_raw = self.rfile.read(content_length)
                body = json.loads(body_raw)
            except (json.JSONDecodeError, Exception):
                self._bad_request("invalid JSON body", send_body=send_body)
                return

            if not isinstance(body, dict):
                self._bad_request("body must be a JSON object", send_body=send_body)
                return

            task_id = str(body.get("task_id") or "").strip()
            if not _TASK_ID_RE.fullmatch(task_id):
                self._bad_request("invalid task_id", send_body=send_body)
                return
            reason = str(body.get("reason") or "")[:200]

            if path == "/api/archive":
                status_code, resp = self._handle_archive(task_id, reason)
            else:
                status_code, resp = self._handle_restore(task_id, reason)

            self._send_json(status_code, resp, send_body=send_body)
            return

        # ── health-identity (no auth required) ──
        if path == "/api/health-identity":
            try:
                from AITools.taskdb import db_identity_fingerprint, open_db, init_db, DEFAULT_DB
                with open_db(DEFAULT_DB) as conn:
                    init_db(conn)
                    identity = db_identity_fingerprint(conn)
                self._send_json(HTTPStatus.OK, {"ok": True, **identity}, send_body=send_body)
            except Exception as e:
                self._send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"ok": False, "error": "identity_unavailable", "message": _bounded_text(e)},
                    send_body=send_body,
                )
            return

        # Preserve the historical mutation guard for every non-allowlisted
        # POST route. Close the connection because an ignored request body
        # must never be parsed as a second HTTP request.
        self.close_connection = True
        self._send_json(
            HTTPStatus.METHOD_NOT_ALLOWED,
            {
                "ok": False,
                "error": "method_not_allowed",
                "message": "dashboard is read-only",
            },
            send_body=send_body,
            extra_headers={"Allow": "GET, HEAD", "Connection": "close"},
        )

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        self._dispatch_post(send_body=True)

    def _reject_mutation(self) -> None:
        self.close_connection = True
        if not self._request_context_is_valid(send_body=True):
            return
        allow = (
            "GET, HEAD, POST"
            if urlsplit(self.path).path in ("/api/archive", "/api/restore")
            else "GET, HEAD"
        )
        message = (
            "dashboard is read-only except archive/restore"
            if allow.endswith("POST")
            else "dashboard is read-only"
        )
        self._send_json(
            HTTPStatus.METHOD_NOT_ALLOWED,
            {
                "ok": False,
                "error": "method_not_allowed",
                "message": message,
            },
            send_body=True,
            extra_headers={"Allow": allow, "Connection": "close"},
        )

    do_PUT = _reject_mutation
    do_PATCH = _reject_mutation
    do_DELETE = _reject_mutation
    do_OPTIONS = _reject_mutation
    do_TRACE = _reject_mutation
    do_CONNECT = _reject_mutation

    def __getattr__(self, name: str) -> Any:
        if name.startswith("do_"):
            return self._reject_mutation
        raise AttributeError(name)


def create_server(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    provider: Any | None = None,
    static_dir: Path | None = None,
) -> DashboardHTTPServer:
    if not is_loopback_host(host):
        raise ValueError("dashboard host must be a loopback address (127.0.0.1, ::1, or localhost)")
    if not isinstance(port, int) or isinstance(port, bool) or not 0 <= port <= 65535:
        raise ValueError("dashboard port must be between 0 and 65535")

    normalized_host = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    server_class: type[DashboardHTTPServer]
    try:
        is_ipv6 = ipaddress.ip_address(normalized_host).version == 6
    except ValueError:
        is_ipv6 = False
    server_class = DashboardHTTPServerV6 if is_ipv6 else DashboardHTTPServer
    server = server_class((normalized_host, port), DashboardRequestHandler)
    server.provider = provider or DashboardProvider()  # type: ignore[attr-defined]
    server.static_dir = (static_dir or STATIC_DIR).resolve()  # type: ignore[attr-defined]
    return server


def _port_argument(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 0 and 65535")
    return port


def _local_url(host: str, port: int) -> str:
    display_host = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    if ":" in display_host:
        display_host = f"[{display_host}]"
    return f"http://{display_host}:{port}/"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Serve the local read-only GeoAI task dashboard")
    parser.add_argument("--host", default=DEFAULT_HOST, help="loopback host (default: 127.0.0.1)")
    parser.add_argument("--port", type=_port_argument, default=DEFAULT_PORT, help="local port")
    parser.add_argument("--no-open", action="store_true", help="do not open the system browser")
    args = parser.parse_args(argv)

    try:
        server = create_server(args.host, args.port)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))

    actual_port = int(server.server_address[1])
    url = _local_url(args.host, actual_port)
    print(f"GeoAI task dashboard (read-only): {url}", flush=True)
    if not args.no_open:
        webbrowser.open(url, new=2)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
