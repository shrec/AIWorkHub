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
    deepseek_credentials,
    process_launcher,
    repository_state,
    storage_observability,
    task_store,
)


DEFAULT_TASK_LIMIT = 500
DEFAULT_PROCESS_LIMIT = 200
MAX_PROCESS_LOG_BYTES = 4 * 1024 * 1024
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
    gate = event.get("worker_mcp_gate")
    if (
        not isinstance(context, Mapping)
        and not isinstance(delivery, Mapping)
        and not isinstance(ack, Mapping)
        and not isinstance(gate, Mapping)
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
            estimate = {
                "label": str(raw_estimate.get("label") or "")[:80],
                "raw_context_bytes": int(raw_estimate.get("raw_context_bytes") or 0),
                "bundle_bytes": int(raw_estimate.get("bundle_bytes") or 0),
                "delta_bytes": int(raw_estimate.get("delta_bytes") or 0),
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
            "source_graph_live_calls": int(verification.get("live_source_graph_calls") or 0),
            "source_graph_bytes": int(bytes_by_tool.get("source_graph") or 0)
            if isinstance(bytes_by_tool, Mapping) else 0,
            "source_graph_cache_hits": int(cache_by_tool.get("source_graph") or 0)
            if isinstance(cache_by_tool, Mapping) else 0,
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
        "tool_use": tool_use,
    }


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
        "source_graph_live_tasks": 0,
        "source_graph_injected_only_tasks": 0,
        "source_graph_stale_or_cached_tasks": 0,
        "source_graph_missing_tasks": 0,
        "source_graph_calls": 0,
        "source_graph_live_calls": 0,
        "source_graph_bytes": 0,
        "source_graph_cache_hits": 0,
        "policy_violation_tasks": 0,
        "policy_violations": 0,
        "tampered_ledger_tasks": 0,
        "live_rate": 0.0,
        "any_rate": 0.0,
        "gate_satisfaction_rate": 0.0,
        "by_adapter": {},
    }

    def bucket_for(adapter: str) -> dict[str, int]:
        buckets = totals["by_adapter"]
        if adapter not in buckets:
            buckets[adapter] = {
                "gated_tasks": 0,
                "live_tasks": 0,
                "injected_only_tasks": 0,
                "missing_or_stale_tasks": 0,
                "source_graph_calls": 0,
                "policy_violations": 0,
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
        if tool_use.get("satisfied"):
            totals["satisfied_tasks"] += 1

        calls = int(tool_use.get("source_graph_calls") or 0)
        live_calls = int(tool_use.get("source_graph_live_calls") or 0)
        source_bytes = int(tool_use.get("source_graph_bytes") or 0)
        cache_hits = int(tool_use.get("source_graph_cache_hits") or 0)
        violations = int(tool_use.get("policy_violations") or 0)
        satisfaction = str(tool_use.get("source_graph_satisfaction") or "")
        totals["source_graph_calls"] += calls
        totals["source_graph_live_calls"] += live_calls
        totals["source_graph_bytes"] += source_bytes
        totals["source_graph_cache_hits"] += cache_hits
        totals["policy_violations"] += violations
        bucket["source_graph_calls"] += calls
        bucket["policy_violations"] += violations
        if violations:
            totals["policy_violation_tasks"] += 1
        if int(tool_use.get("entries_tampered") or 0):
            totals["tampered_ledger_tasks"] += 1

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
    if denominator:
        totals["live_rate"] = round(100.0 * totals["source_graph_live_tasks"] / denominator, 1)
        totals["any_rate"] = round(100.0 * totals["source_graph_any_tasks"] / denominator, 1)
        totals["gate_satisfaction_rate"] = round(100.0 * totals["satisfied_tasks"] / denominator, 1)
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
    spawned with (``AIWORKHUB_REPO_ROOT``/cwd ancestor), never a fixed path
    relative to this package's own install location."""
    return repository_state.resolve_repository_root(require_manifest=False)


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
        by_runner: dict[str, dict[str, Any]] = {}
        for row in task_store.list_tasks(self.repo_root, status=None, limit=5000):
            card = task_store.get_task(self.repo_root, str(row.get("task_id") or "")) or {}
            summary = card.get("usage_summary") or {}
            if isinstance(summary, str):
                try:
                    summary = json.loads(summary)
                except json.JSONDecodeError:
                    summary = {}
            source = summary.get("by_runner") if isinstance(summary, Mapping) else {}
            if not isinstance(source, Mapping):
                continue
            for runner, raw_bucket in source.items():
                if not isinstance(raw_bucket, Mapping):
                    continue
                bucket = by_runner.setdefault(
                    str(runner),
                    {"records": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "cost_usd": 0.0},
                )
                for key in ("records", "input_tokens", "output_tokens", "total_tokens"):
                    bucket[key] += int(raw_bucket.get(key) or 0)
                bucket["cost_usd"] += float(raw_bucket.get("cost_usd") or 0.0)
        for bucket in by_runner.values():
            bucket["cost_usd"] = round(float(bucket["cost_usd"]), 6)
        return {
            "schema_id": "aiworkhub.dashboard.canonical_cost_ledger.v1",
            "aggregates": {"by_runner": by_runner},
            "source_status": {"canonical_task_store": "ready"},
        }

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
        return read_process_runs(limit=DEFAULT_PROCESS_LIMIT)

    def get_exact_status_counts(self) -> dict[str, int]:
        """Exact per-status totals across the whole canonical queue (see
        ``exact_status_counts``) -- independent of ``task_limit``, never a
        per-row payload."""
        return exact_status_counts(self.repo_root)

    def get_adapter_readiness(self) -> dict[str, Any]:
        """Read-only worker-adapter readiness (never exposes any secret)."""
        return deepseek_credentials.adapter_readiness(repo=self.repo_root)

    def get_callback_bridge_health(self) -> dict[str, Any]:
        """Read-only, redacted-safe callback bridge health: bound/unbound
        task counts and per-state outbox counts, sourced from the canonical
        AIWorkHub task_store -- never ``AITools/taskdb.py``. Never exposes a
        full origin_thread_id."""
        return task_store.callback_bridge_health(self.repo_root)


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
            "project_context_telemetry": _project_context_telemetry({}),
            "callback_bridge_health": {},
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
            "totals": _cost_totals(ledger),
            "ledger": dict(ledger),
        },
        "collision_report": dict(collision_report),
        "agent_processes": dict(process_report),
        "adapter_readiness": dict(adapter_readiness),
        "source_graph_telemetry": _source_graph_telemetry(process_report),
        "project_context_telemetry": _project_context_telemetry(process_report),
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
    if isinstance(terminal_review, Mapping):
        evidence = terminal_review.get("evidence")
        quality = evidence.get("quality_gate") if isinstance(evidence, Mapping) else None
        if isinstance(quality, Mapping):
            result["task"]["quality_gate"] = {
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
