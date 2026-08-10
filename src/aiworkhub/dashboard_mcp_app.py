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
import os
import re
import sqlite3
import time
from typing import Any, Mapping

from aiworkhub import (
    __version__,
    core,
    context_graph,
    dashboard,
    feature_settings,
    needfix_store,
    process_launcher,
    roadmap_store,
    repo_policy,
    repository_bootstrap,
    shared_router,
    source_graph,
    storage_observability,
    storage_registry,
    storage_retention,
    task_retention,
    task_store,
    terminal_log_retention,
)


# Hard bound on the serialized tool response so a very large queue (many
# thousands of process-log rows, a big cost ledger) can never balloon a
# single MCP stdio response. This defends the transport only -- it never
# changes what build_snapshot()/build_task_detail() compute, only how much of
# it survives one tool call. Every drop is reported, never silent.
# A dashboard refresh is a control-plane heartbeat, not a history export.
# Keep it comfortably below common MCP/Webview message thresholds; detailed
# task, log, memory, session and KB data already have dedicated bounded tools.
MAX_SNAPSHOT_RESPONSE_BYTES = 512 * 1024
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

_COMPACT_SNAPSHOT_FIELDS: tuple[str, ...] = (
    "schema_version",
    "generated_at",
    "readonly",
    "storage",
    "storage_usage",
    "health",
    "status_counts",
    "row_counts",
    "warnings",
    "errors",
    "manager_identity",
    "callback_delivery",
    "manager_identity_target",
    "known_repositories",
    "server_tool",
    "authority_flags",
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

# Bound on each incremental Live Output raw-text read. The cursor advances by
# exactly the delivered raw bytes, so a caller can fetch the next chunk
# without loss. Keep this materially below the general log-tail bound: JSONL
# provider streams can represent each token fragment as a full event object,
# and HTML escaping can expand the serialized response beyond the raw size.
MAX_LIVE_OUTPUT_BYTES = 8 * 1024
MAX_MEMORY_ROWS = 200
MAX_MEMORY_VALUE_CHARS = 4000
MAX_SESSION_ROWS = 200
MAX_KB_ROWS = 200
MAX_CONTEXT_VALUE_CHARS = 4000

# The ONE genuine "repository has never been initialized" reason prefix
# ``task_store.storage_readiness`` can produce: ``inspect_repository`` raised
# ``ManifestMissingError`` because no ``.aiworkhub/project.json`` has ever
# been written. Every other ``storage_readiness`` reason (a corrupt/
# mismatched storage registry, a missing/corrupt canonical DB, a schema or
# quick_check failure, a manifest with the wrong schema/layout version) is a
# REAL failure, not "never initialized" -- ``is_not_initialized_reason`` must
# return False for those so the dashboard never tells a user with a corrupt
# repository to "just click Init Repo" as though nothing has happened yet.
_NOT_INITIALIZED_REASON_PREFIXES: tuple[str, ...] = ("manifest_invalid:manifest_missing",)


def _debug_trace(event: str, **fields: Any) -> None:
    trace_file = os.environ.get("AIWORKHUB_DEBUG_TRACE_FILE", "").strip()
    if not trace_file:
        return
    payload = {
        "schema_id": "aiworkhub.mcp_debug_trace.v1",
        "timestamp_epoch": time.time(),
        "monotonic": time.monotonic(),
        "process": "mcp-server",
        "pid": os.getpid(),
        "event": str(event)[:120],
        **fields,
    }
    data = (json.dumps(payload, ensure_ascii=True, default=str) + "\n").encode("utf-8")
    try:
        os.makedirs(os.path.dirname(trace_file), exist_ok=True)
        fd = os.open(trace_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        return


def _debug_stage(name: str, operation: Any) -> Any:
    started = time.perf_counter()
    _debug_trace("snapshot.stage.begin", stage=name)
    try:
        result = operation()
    except Exception as exc:
        _debug_trace(
            "snapshot.stage.error",
            stage=name,
            duration_ms=round((time.perf_counter() - started) * 1000, 3),
            error_type=type(exc).__name__,
        )
        raise
    _debug_trace(
        "snapshot.stage.end",
        stage=name,
        duration_ms=round((time.perf_counter() - started) * 1000, 3),
    )
    return result


def is_not_initialized_reason(reason: str) -> bool:
    """True only for the genuine "never initialized" storage_readiness
    reason -- never for a corrupt/mismatched registry, a missing/corrupt
    canonical DB, or a schema/quick_check failure. Callers (health_view,
    snapshot_view) use this to decide whether "Initialize AIWorkHub" is the
    correct recommended action, so a real corruption is never masked as
    "just click init"."""
    text = str(reason or "")
    return any(text.startswith(prefix) for prefix in _NOT_INITIALIZED_REASON_PREFIXES)


def _readonly_authority_flags() -> dict[str, bool]:
    return {
        "readonly": True,
        "queue_write": False,
        "audit_write": False,
        "process_launch": False,
        "agent_launch": False,
        "shell_invocation": False,
    }


def _storage_write_authority_flags() -> dict[str, bool]:
    return {
        "readonly": False,
        "queue_write": False,
        "audit_write": True,
        "storage_write": True,
        "process_launch": False,
        "agent_launch": False,
        "shell_invocation": False,
    }


def settings_view() -> dict[str, Any]:
    """READ-ONLY: repository-local feature switches and capabilities."""
    try:
        root = core.repo_root()
        result = feature_settings.load(root)
        result["context_graph_runtime"] = context_graph.status(root)
        result["source_graph_policy"] = source_graph.source_graph_policy_view(root)
        policy = repo_policy.load_policy(root)
        result["retention_policy"] = dict(policy.get("retention") or {})
    except (
        context_graph.ContextGraphError,
        feature_settings.FeatureSettingsError,
        repo_policy.RepoPolicyError,
        OSError,
        sqlite3.Error,
    ) as exc:
        result = {"ok": False, "error": str(exc)[:240]}
    result["server_tool"] = "aiworkhub_dashboard_settings"
    result["authority_flags"] = _readonly_authority_flags()
    return result


def source_graph_settings_update_view(
    language_changes: dict[str, bool], expected_revision: int,
) -> dict[str, Any]:
    """USER WRITE: atomically update repository Source Graph languages."""

    root = core.repo_root()
    try:
        result = source_graph.update_language_policy(
            root,
            language_changes=language_changes,
            expected_revision=expected_revision,
        )
        if feature_settings.enabled(root, "source_graph"):
            result["source_graph_refresh"] = core.source_graph_refresh_now()
    except (
        feature_settings.FeatureSettingsError,
        source_graph.SourceGraphError,
        OSError,
        sqlite3.Error,
        ValueError,
    ) as exc:
        result = {"ok": False, "error": str(exc)[:240]}
    result["server_tool"] = "aiworkhub_dashboard_source_graph_settings_update"
    result["authority_flags"] = _storage_write_authority_flags()
    return result


def settings_update_view(changes: dict[str, bool], expected_revision: int) -> dict[str, Any]:
    """USER WRITE: atomically update bounded repository feature switches."""
    root = core.repo_root()
    try:
        result = feature_settings.update(
            root,
            changes=changes,
            expected_revision=expected_revision,
        )
        if "source_graph" in changes:
            lifecycle = (
                core.source_graph_ensure_started()
                if changes["source_graph"]
                else core.source_graph_stop()
            )
            result["source_graph_lifecycle"] = lifecycle
        if changes.get("context_graph") is True:
            result["context_graph_runtime"] = context_graph.ensure_schema(root)
    except (
        context_graph.ContextGraphError,
        feature_settings.FeatureSettingsError,
        OSError,
        sqlite3.Error,
        ValueError,
    ) as exc:
        result = {"ok": False, "error": str(exc)[:240]}
    result["server_tool"] = "aiworkhub_dashboard_settings_update"
    result["authority_flags"] = _storage_write_authority_flags()
    return result


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


def _compact_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Return the bounded manager-facing operational snapshot.

    The native Webview explicitly requests the full shape. Model callers get
    queue counts, health, warnings and route truth by default without pulling
    repeated task rows, process evidence, cost ledgers, workforce history or
    analytics into their context. Dedicated bounded tools own those details.
    """

    result = {
        key: snapshot[key]
        for key in _COMPACT_SNAPSHOT_FIELDS
        if key in snapshot
    }
    omitted = sorted(key for key in snapshot if key not in result)
    result.update({
        "snapshot_mode": "summary",
        "full_snapshot_available": True,
        "omitted_fields": omitted,
    })
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


def snapshot_view(full: bool = False) -> dict[str, Any]:
    """READ-ONLY: canonical dashboard snapshot for the native Webview.

    Calls ``dashboard.build_snapshot()`` -- the SAME builder the HTTP
    dashboard's ``/api/snapshot`` route uses -- and returns the identical
    ``status_counts``, ``tasks``, ``row_counts``, ``summaries``,
    ``cost_usage``, ``agent_processes``, and ``warnings`` shape the existing
    dashboard.js already renders when ``full=true``. The default manager call
    is a bounded operational summary; the native Webview requests full mode
    explicitly. Adds no second SQLite/taskctl read.
    """
    started = time.perf_counter()
    _debug_trace("snapshot.begin")
    snapshot = dict(_debug_stage("dashboard.build_snapshot", dashboard.build_snapshot))
    storage = snapshot.get("storage")
    if isinstance(storage, dict) and not storage.get("ready", True):
        storage = dict(storage)
        storage["not_initialized"] = is_not_initialized_reason(storage.get("reason", ""))
        snapshot["storage"] = storage
    try:
        manager = _debug_stage("core.manager_bootstrap", core.manager_bootstrap)
    except Exception as exc:  # noqa: BLE001 -- dashboard diagnostics must not break the queue view
        manager = {
            "ok": False,
            "role": "unknown",
            "provider": "unknown",
            "manager_route": {},
            "reason": f"manager_bootstrap_failed:{type(exc).__name__}",
        }
    snapshot["manager_identity"] = manager
    # Identity authority and callback delivery are deliberately separate
    # contracts.  A valid manager route does not prove that the extension-
    # owned dispatcher is registered/running, so surface the live dispatcher
    # health from this exact repo-bound MCP child in every snapshot.
    try:
        snapshot["callback_delivery"] = _debug_stage("core.dispatcher_health", core.dispatcher_health)
    except Exception as exc:  # noqa: BLE001 -- callback diagnostics must not break the queue view
        snapshot["callback_delivery"] = {
            "ok": False,
            "healthy": False,
            "status": "unknown",
            "dispatcher_running": False,
            "registered": False,
            "problems": [f"dispatcher_health_failed:{type(exc).__name__}"],
        }
    try:
        router_root = _debug_stage("core.repo_root.router", core.repo_root)
        snapshot["known_repositories"] = _debug_stage(
            "shared_router.list_known_repositories",
            lambda: shared_router.list_known_repositories(current_root=router_root, limit=32),
        )
    except Exception as exc:  # noqa: BLE001 -- shared router diagnostics must not break the active repo view
        snapshot["known_repositories"] = {
            "ok": False,
            "schema_id": shared_router.SCHEMA_ID,
            "error": f"shared_router_failed:{type(exc).__name__}",
            "repositories": [],
            "rejects": [],
        }
    try:
        target_root = _debug_stage("core.repo_root.target", core.repo_root)
        targets = _debug_stage(
            "core.read_selected_coordinator_target",
            lambda: core.read_selected_coordinator_target(target_root),
        )
        selected = str(targets.get("selected_provider") or "")
        selected_target = targets.get("targets", {}).get(selected, {}) if isinstance(targets.get("targets"), dict) else {}
        wake = selected_target.get("wake", {}) if isinstance(selected_target, dict) else {}
        snapshot["manager_identity_target"] = {
            "selected_provider": selected,
            "capability_state": selected_target.get("capability_state") if isinstance(selected_target, dict) else "",
            "reason": wake.get("reason") or wake.get("action") if isinstance(wake, dict) else "",
        }
    except Exception:  # noqa: BLE001 -- route diagnostics are optional
        snapshot["manager_identity_target"] = {}
    snapshot["server_tool"] = "aiworkhub_dashboard_snapshot"
    snapshot["authority_flags"] = _readonly_authority_flags()
    if full:
        snapshot["snapshot_mode"] = "full"
        result = _debug_stage("bound_snapshot", lambda: _bound_snapshot(snapshot))
    else:
        result = _debug_stage(
            "compact_snapshot",
            lambda: _bound_snapshot(_compact_snapshot(snapshot)),
        )
    _debug_trace(
        "snapshot.end",
        duration_ms=round((time.perf_counter() - started) * 1000, 3),
        response_bytes=_byte_len(result),
    )
    return result


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
            "server_tool": "aiworkhub_dashboard_task_detail",
            "authority_flags": _readonly_authority_flags(),
        }
    detail = dashboard.build_task_detail(candidate)
    if detail is None:
        return {
            "ok": False,
            "error": "task_not_found",
            "task_id": candidate,
            "server_tool": "aiworkhub_dashboard_task_detail",
            "authority_flags": _readonly_authority_flags(),
        }
    response = dict(detail)
    response["ok"] = True
    response["server_tool"] = "aiworkhub_dashboard_task_detail"
    response["authority_flags"] = _readonly_authority_flags()
    return _bound_task_detail(response)


def memory_view(limit: int = 100) -> dict[str, Any]:
    """READ-ONLY: newest canonical AI Memory rows for this repository.

    The database path is resolved exclusively through the repo-bound storage
    registry and opened read-only. Browsing never mutates access counters.
    """

    try:
        safe_limit = max(1, min(int(limit), MAX_MEMORY_ROWS))
    except (TypeError, ValueError):
        safe_limit = 100
    try:
        root = core.repo_root()
        registry = storage_registry.load_storage_registry(root)
        db_path = storage_registry.resolve_database_path(registry, "memory")
        connection = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(memories)").fetchall()
            }
            required = {"id", "key", "value", "tags", "scope"}
            if not required.issubset(columns):
                raise sqlite3.OperationalError("memory_schema_missing_required_columns")
            project_expr = "m.project AS project" if "project" in columns else "'' AS project"
            created_expr = "m.created_at AS created_at" if "created_at" in columns else "'' AS created_at"
            updated_expr = "m.updated_at AS updated_at" if "updated_at" in columns else "'' AS updated_at"
            order_column = "updated_at" if "updated_at" in columns else (
                "created_at" if "created_at" in columns else "id"
            )
            has_state = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='context_entity_state'"
            ).fetchone() is not None
            state_join = (
                "LEFT JOIN context_entity_state s ON s.entity_type='memory' AND s.entity_id=m.id "
                if has_state else ""
            )
            state_expr = "COALESCE(s.status,'active') AS status" if has_state else "'active' AS status"
            total = int(connection.execute("SELECT COUNT(*) FROM memories").fetchone()[0])
            rows = connection.execute(
                f"SELECT m.id,m.key,m.value,m.tags,m.scope,{project_expr},{created_expr},"
                f"{updated_expr},{state_expr} FROM memories m {state_join}"
                f"ORDER BY m.{order_column} DESC,m.id DESC LIMIT ?",
                (safe_limit,),
            ).fetchall()
        finally:
            connection.close()
    except (OSError, sqlite3.Error, storage_registry.StorageRegistryError) as exc:
        return {
            "ok": False,
            "error": f"memory_unavailable:{type(exc).__name__}",
            "entries": [],
            "total": 0,
            "authority_flags": _readonly_authority_flags(),
        }
    entries = [{
        "id": int(row["id"]),
        "key": str(row["key"] or "")[:300],
        "value": str(row["value"] or "")[:MAX_MEMORY_VALUE_CHARS],
        "tags": str(row["tags"] or "")[:500],
        "scope": str(row["scope"] or "")[:40],
        "project": str(row["project"] or "")[:200],
        "created_at": str(row["created_at"] or "")[:80],
        "updated_at": str(row["updated_at"] or "")[:80],
        "status": str(row["status"] or "active")[:32],
    } for row in rows]
    return {
        "ok": True,
        "server_tool": "aiworkhub_dashboard_memory",
        "entries": entries,
        "count": len(entries),
        "total": total,
        "truncated": total > len(entries),
        "authority_flags": _readonly_authority_flags(),
    }


def session_view(limit: int = 100) -> dict[str, Any]:
    """READ-ONLY: newest canonical Session Manager transcript evidence.

    Session continuity is represented by the canonical transcript graph's
    bounded ``documents`` rows. The dashboard never opens a legacy session
    file and never mutates access/checkpoint state while browsing.
    """

    try:
        safe_limit = max(1, min(int(limit), MAX_SESSION_ROWS))
    except (TypeError, ValueError):
        safe_limit = 100
    try:
        root = core.repo_root()
        registry = storage_registry.load_storage_registry(root)
        db_path = storage_registry.resolve_database_path(registry, "transcript")
        connection = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            total = int(connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0])
            rows = connection.execute(
                "SELECT doc_id, source_id, timestamp, kind, content FROM documents "
                "ORDER BY timestamp DESC, doc_id DESC LIMIT ?",
                (safe_limit,),
            ).fetchall()
        finally:
            connection.close()
    except (OSError, sqlite3.Error, storage_registry.StorageRegistryError) as exc:
        return {
            "ok": False,
            "error": f"sessions_unavailable:{type(exc).__name__}",
            "entries": [],
            "total": 0,
            "authority_flags": _readonly_authority_flags(),
        }
    entries = [{
        "id": int(row["doc_id"]),
        "source_id": str(row["source_id"] or "")[:300],
        "timestamp": str(row["timestamp"] or "")[:80],
        "kind": str(row["kind"] or "")[:80],
        "content": str(row["content"] or "")[:MAX_CONTEXT_VALUE_CHARS],
    } for row in rows]
    return {
        "ok": True,
        "server_tool": "aiworkhub_dashboard_sessions",
        "entries": entries,
        "count": len(entries),
        "total": total,
        "truncated": total > len(entries),
        "authority_flags": _readonly_authority_flags(),
    }


def kb_view(limit: int = 100) -> dict[str, Any]:
    """READ-ONLY: newest canonical repository KB entries."""

    try:
        safe_limit = max(1, min(int(limit), MAX_KB_ROWS))
    except (TypeError, ValueError):
        safe_limit = 100
    try:
        root = core.repo_root()
        registry = storage_registry.load_storage_registry(root)
        db_path = storage_registry.resolve_database_path(registry, "kb")
        connection = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            total = int(connection.execute("SELECT COUNT(*) FROM entries").fetchone()[0])
            has_state = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='context_entity_state'"
            ).fetchone() is not None
            if has_state:
                rows = connection.execute(
                    "SELECT e.id,e.key,e.title,e.body,e.category,e.tags,e.source_refs,"
                    "COALESCE(s.status,'active') status FROM entries e "
                    "LEFT JOIN context_entity_state s ON s.entity_type='kb' AND s.entity_id=e.id "
                    "ORDER BY e.id DESC LIMIT ?", (safe_limit,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT id,key,title,body,category,tags,source_refs,'active' status "
                    "FROM entries ORDER BY id DESC LIMIT ?", (safe_limit,),
                ).fetchall()
        finally:
            connection.close()
    except (OSError, sqlite3.Error, storage_registry.StorageRegistryError) as exc:
        return {
            "ok": False,
            "error": f"kb_unavailable:{type(exc).__name__}",
            "entries": [],
            "total": 0,
            "authority_flags": _readonly_authority_flags(),
        }
    entries = [{
        "id": int(row["id"]),
        "key": str(row["key"] or "")[:300],
        "title": str(row["title"] or "")[:500],
        "body": str(row["body"] or "")[:MAX_CONTEXT_VALUE_CHARS],
        "category": str(row["category"] or "")[:120],
        "tags": str(row["tags"] or "")[:500],
        "source_refs": str(row["source_refs"] or "")[:1000],
        "status": str(row["status"] or "active")[:32],
    } for row in rows]
    return {
        "ok": True,
        "server_tool": "aiworkhub_dashboard_kb",
        "entries": entries,
        "count": len(entries),
        "total": total,
        "truncated": total > len(entries),
        "authority_flags": _readonly_authority_flags(),
    }


def storage_retention_preview_view() -> dict[str, Any]:
    """READ-ONLY: fresh repository-scoped cleanup preview and batch list."""
    try:
        root = core.repo_root()
        response = storage_retention.preview(root)
        response["quarantine"] = storage_retention.list_batches(root)
    except storage_retention.StorageRetentionError as exc:
        response = {"ok": False, "error": str(exc)[:240]}
    response["server_tool"] = "aiworkhub_dashboard_storage_retention_preview"
    response["authority_flags"] = _readonly_authority_flags()
    return response


def storage_quarantine_view(preview_digest: str, confirm: bool = False) -> dict[str, Any]:
    """USER WRITE: quarantine one still-current preview after explicit consent."""
    try:
        root = core.repo_root()
        response = storage_retention.quarantine(
            root,
            preview_digest=str(preview_digest or "")[:128],
            confirm=confirm is True,
        )
        storage_observability.invalidate(root)
    except storage_retention.StorageRetentionError as exc:
        response = {"ok": False, "error": str(exc)[:240]}
    response["server_tool"] = "aiworkhub_dashboard_storage_quarantine"
    response["authority_flags"] = _storage_write_authority_flags()
    return response


def storage_registration_prune_view(
    preview_digest: str,
    confirm: bool = False,
) -> dict[str, Any]:
    """USER WRITE: prune exact stale AIWorkHub Git registrations."""
    try:
        root = core.repo_root()
        response = storage_retention.prune_stale_registrations(
            root,
            preview_digest=str(preview_digest or "")[:128],
            confirm=confirm is True,
        )
        storage_observability.invalidate(root)
    except storage_retention.StorageRetentionError as exc:
        response = {"ok": False, "error": str(exc)[:240]}
    response["server_tool"] = "aiworkhub_dashboard_storage_registration_prune"
    response["authority_flags"] = _storage_write_authority_flags()
    return response


def storage_restore_view(batch_id: str, confirm: bool = False) -> dict[str, Any]:
    """USER WRITE: restore one repository-owned quarantine batch."""
    try:
        root = core.repo_root()
        response = storage_retention.restore(
            root,
            batch_id=str(batch_id or "")[:128],
            confirm=confirm is True,
        )
        storage_observability.invalidate(root)
    except storage_retention.StorageRetentionError as exc:
        response = {"ok": False, "error": str(exc)[:240]}
    response["server_tool"] = "aiworkhub_dashboard_storage_restore"
    response["authority_flags"] = _storage_write_authority_flags()
    return response


def storage_purge_view(batch_id: str, confirm: bool = False) -> dict[str, Any]:
    """USER WRITE: permanently purge one expired quarantine batch."""
    try:
        root = core.repo_root()
        response = storage_retention.purge(
            root,
            batch_id=str(batch_id or "")[:128],
            confirm=confirm is True,
        )
        storage_observability.invalidate(root)
    except storage_retention.StorageRetentionError as exc:
        response = {"ok": False, "error": str(exc)[:240]}
    response["server_tool"] = "aiworkhub_dashboard_storage_purge"
    response["authority_flags"] = _storage_write_authority_flags()
    return response


def terminal_log_retention_preview_view(
    cursor: int = 0,
    limit: int = terminal_log_retention.DEFAULT_PREVIEW_LIMIT,
) -> dict[str, Any]:
    """READ-ONLY: terminal-run log preview; the canonical ledger is excluded."""
    try:
        root = core.repo_root()
        response = terminal_log_retention.preview(root, cursor=cursor, limit=limit)
        response["quarantine"] = terminal_log_retention.list_batches(root)
    except terminal_log_retention.TerminalLogRetentionError as exc:
        response = {"ok": False, "error": str(exc)[:240]}
    response["server_tool"] = "aiworkhub_dashboard_terminal_log_retention_preview"
    response["authority_flags"] = _readonly_authority_flags()
    return response


def terminal_log_quarantine_view(preview_digest: str, confirm: bool = False) -> dict[str, Any]:
    """USER WRITE: move one exact set of terminal files to repo-local quarantine."""
    try:
        root = core.repo_root()
        response = terminal_log_retention.quarantine(
            root, preview_digest=str(preview_digest or "")[:128], confirm=confirm is True
        )
        storage_observability.invalidate(root)
    except terminal_log_retention.TerminalLogRetentionError as exc:
        response = {"ok": False, "error": str(exc)[:240]}
    response["server_tool"] = "aiworkhub_dashboard_terminal_log_quarantine"
    response["authority_flags"] = _storage_write_authority_flags()
    return response


def terminal_log_usage_backfill_view(confirm: bool = False) -> dict[str, Any]:
    """USER WRITE: recover canonical usage receipts before log retention."""

    try:
        root = core.repo_root()
        response = terminal_log_retention.backfill_usage_capture(
            root, confirm=confirm is True
        )
    except terminal_log_retention.TerminalLogRetentionError as exc:
        response = {"ok": False, "error": str(exc)[:240]}
    response["server_tool"] = "aiworkhub_dashboard_terminal_log_usage_backfill"
    response["authority_flags"] = _storage_write_authority_flags()
    return response


def terminal_log_restore_view(batch_id: str, confirm: bool = False) -> dict[str, Any]:
    """USER WRITE: restore one terminal-log quarantine batch without overwrite."""
    try:
        root = core.repo_root()
        response = terminal_log_retention.restore(
            root, batch_id=str(batch_id or "")[:128], confirm=confirm is True
        )
        storage_observability.invalidate(root)
    except terminal_log_retention.TerminalLogRetentionError as exc:
        response = {"ok": False, "error": str(exc)[:240]}
    response["server_tool"] = "aiworkhub_dashboard_terminal_log_restore"
    response["authority_flags"] = _storage_write_authority_flags()
    return response


def terminal_log_purge_view(batch_id: str, confirm: bool = False) -> dict[str, Any]:
    """USER WRITE: permanently purge one expired terminal-log batch."""
    try:
        root = core.repo_root()
        response = terminal_log_retention.purge(
            root, batch_id=str(batch_id or "")[:128], confirm=confirm is True
        )
        storage_observability.invalidate(root)
    except terminal_log_retention.TerminalLogRetentionError as exc:
        response = {"ok": False, "error": str(exc)[:240]}
    response["server_tool"] = "aiworkhub_dashboard_terminal_log_purge"
    response["authority_flags"] = _storage_write_authority_flags()
    return response


def task_retention_preview_view(older_than_days: int | None = None) -> dict[str, Any]:
    """READ-ONLY: old archived-task cleanup preview and undo batches."""
    try:
        root = core.repo_root()
        response = task_retention.preview(root, older_than_days=older_than_days)
        response["quarantine"] = task_retention.list_batches(root)
    except (task_retention.TaskRetentionError, ValueError, TypeError) as exc:
        response = {"ok": False, "error": str(exc)[:240]}
    response["server_tool"] = "aiworkhub_dashboard_task_retention_preview"
    response["authority_flags"] = _readonly_authority_flags()
    return response


def task_archive_view(task_id: str, reason: str = "", confirm: bool = False) -> dict[str, Any]:
    """USER WRITE: archive one non-active task without deleting its evidence."""
    candidate = str(task_id or "")
    if confirm is not True:
        response = {"ok": False, "error": "task_archive_confirmation_required"}
    elif not dashboard._TASK_ID_RE.fullmatch(candidate):
        response = {"ok": False, "error": "invalid_task_id"}
    else:
        try:
            root = core.repo_root()
            task = task_store.get_task(root, candidate)
            if task is None:
                response = {"ok": False, "error": "task_not_found"}
            elif task_store.canonical_status(task) in {"processing", "review"}:
                response = {"ok": False, "error": "task_archive_active_or_review_forbidden"}
            else:
                ok, status = task_store.archive_task(
                    root,
                    candidate,
                    actor="dashboard_user",
                    reason=str(reason or "")[:200],
                )
                response = {"ok": ok, "task_id": candidate, "status": status}
                storage_observability.invalidate(root)
        except task_store.TaskStoreError as exc:
            response = {"ok": False, "error": str(exc)[:240]}
    response["server_tool"] = "aiworkhub_dashboard_task_archive"
    response["authority_flags"] = _storage_write_authority_flags()
    return response


def task_restore_view(task_id: str, reason: str = "", confirm: bool = False) -> dict[str, Any]:
    """USER WRITE: restore one still-live archived task to its prior state."""
    candidate = str(task_id or "")
    if confirm is not True:
        response = {"ok": False, "error": "task_restore_confirmation_required"}
    elif not dashboard._TASK_ID_RE.fullmatch(candidate):
        response = {"ok": False, "error": "invalid_task_id"}
    else:
        try:
            root = core.repo_root()
            ok, status = task_store.restore_task(
                root,
                candidate,
                actor="dashboard_user",
                reason=str(reason or "")[:200],
            )
            response = {"ok": ok, "task_id": candidate, "status": status}
            storage_observability.invalidate(root)
        except task_store.TaskStoreError as exc:
            response = {"ok": False, "error": str(exc)[:240]}
    response["server_tool"] = "aiworkhub_dashboard_task_restore"
    response["authority_flags"] = _storage_write_authority_flags()
    return response


def _needfix_response(response: Mapping[str, Any], tool: str, *, write: bool = False) -> dict[str, Any]:
    """Attach dashboard authority metadata to one bounded NeedFix response."""
    result = dict(response)
    result["server_tool"] = tool
    result["authority_flags"] = (
        _storage_write_authority_flags() if write else _readonly_authority_flags()
    )
    return result


def _bounded_needfix_row(row: Mapping[str, Any], *, include_detail: bool = False) -> dict[str, Any]:
    """Return only bounded fields safe for the native Webview."""
    result: dict[str, Any] = {
        "id": str(row.get("id") or "")[:32],
        "title": str(row.get("title") or "")[:240],
        "status": str(row.get("status") or "")[:40],
        "kind": str(row.get("kind") or "")[:60],
        "severity": str(row.get("severity") or "")[:24],
        "readiness_score": max(0, min(100, int(row.get("readiness_score") or 0))),
        "duplicate_parent_id": str(row.get("duplicate_parent_id") or "")[:32] or None,
        "converted_task_id": str(row.get("converted_task_id") or "")[:200] or None,
        "created_at": str(row.get("created_at") or "")[:64],
        "updated_at": str(row.get("updated_at") or "")[:64],
        "archived_at": str(row.get("archived_at") or "")[:64] or None,
        "tags": [str(value)[:80] for value in list(row.get("tags") or [])[:24]],
        "scope_files": [str(value)[:500] for value in list(row.get("scope_files") or [])[:48]],
        "scope_symbols": [str(value)[:300] for value in list(row.get("scope_symbols") or [])[:48]],
        "evidence_refs": [str(value)[:500] for value in list(row.get("evidence_refs") or [])[:48]],
    }
    if include_detail:
        result.update({
            "description": str(row.get("description") or "")[:8000],
            "scope": str(row.get("scope") or "")[:4000] or None,
            "provenance": dict(list((row.get("provenance") or {}).items())[:32])
            if isinstance(row.get("provenance"), Mapping) else {},
            "evidence": dict(list((row.get("evidence") or {}).items())[:32])
            if isinstance(row.get("evidence"), Mapping) else {},
        })
    return result


def needfix_list_view(
    status: str | None = None,
    kind: str | None = None,
    severity: str | None = None,
    include_archived: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """READ-ONLY: bounded canonical NeedFix inbox for the native Webview."""
    bounded_limit = max(1, min(int(limit or 100), 200))
    bounded_offset = max(0, int(offset or 0))
    try:
        rows = core.needfix_list(
            status=str(status)[:40] if status else None,
            kind=str(kind)[:60] if kind else None,
            severity=str(severity)[:24] if severity else None,
            include_archived=include_archived is True,
            limit=bounded_limit,
            offset=bounded_offset,
        )
        response = {
            "ok": True,
            "entries": [_bounded_needfix_row(row) for row in rows],
            "count": len(rows),
            "limit": bounded_limit,
            "offset": bounded_offset,
            "truncated": len(rows) >= bounded_limit,
        }
    except (needfix_store.NeedFixError, OSError, sqlite3.Error, TypeError, ValueError) as exc:
        response = {"ok": False, "error": str(exc)[:240], "entries": []}
    return _needfix_response(response, "aiworkhub_dashboard_needfix_list")


def needfix_detail_view(needfix_id: str, event_limit: int = 50) -> dict[str, Any]:
    """READ-ONLY: one bounded NeedFix row plus its audit events."""
    candidate = str(needfix_id or "")
    if not needfix_store.NF_ID_RE.fullmatch(candidate):
        return _needfix_response(
            {"ok": False, "error": "invalid_needfix_id"},
            "aiworkhub_dashboard_needfix_detail",
        )
    try:
        row = core.needfix_show(candidate)
        events = core.needfix_events(candidate, limit=max(1, min(int(event_limit or 50), 100)))
        response = {
            "ok": True,
            "item": _bounded_needfix_row(row, include_detail=True),
            "events": [dict(list(event.items())[:24]) for event in events[:100]],
        }
    except (needfix_store.NeedFixError, OSError, sqlite3.Error, TypeError, ValueError) as exc:
        response = {"ok": False, "error": str(exc)[:240]}
    return _needfix_response(response, "aiworkhub_dashboard_needfix_detail")


def _bounded_roadmap_row(
    row: Mapping[str, Any], *, include_detail: bool = False
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": str(row.get("id") or "")[:32],
        "title": str(row.get("title") or "")[:240],
        "status": str(row.get("status") or "")[:40],
        "priority": str(row.get("priority") or "")[:24],
        "milestone": str(row.get("milestone") or "")[:500],
        "needfix_ids": [str(value)[:32] for value in list(row.get("needfix_ids") or [])[:100]],
        "task_ids": [str(value)[:256] for value in list(row.get("task_ids") or [])[:100]],
        "depends_on": [str(value)[:32] for value in list(row.get("depends_on") or [])[:100]],
        "dependency_blockers": [
            str(value)[:32] for value in list(row.get("dependency_blockers") or [])[:100]
        ],
        "dependency_ready": bool(row.get("dependency_ready", True)),
        "created_at": str(row.get("created_at") or "")[:64],
        "updated_at": str(row.get("updated_at") or "")[:64],
    }
    if include_detail:
        result.update(
            {
                "outcome": str(row.get("outcome") or "")[:100_000],
                "acceptance": [
                    str(value)[:1000] for value in list(row.get("acceptance") or [])[:100]
                ],
                "provenance": dict(list((row.get("provenance") or {}).items())[:32])
                if isinstance(row.get("provenance"), Mapping)
                else {},
                "evidence_refs": [
                    str(value)[:1000] for value in list(row.get("evidence_refs") or [])[:200]
                ],
                "tasks": [dict(list(task.items())[:8]) for task in list(row.get("tasks") or [])[:100]],
            }
        )
    return result


def roadmap_list_view(
    status: str | None = None,
    include_archived: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """READ-ONLY: bounded Roadmap outcomes joined to Task-DAG truth."""
    try:
        snapshot = core.roadmap_snapshot(
            limit=max(1, min(int(limit or 100), 200)),
            include_archived=include_archived is True,
        )
        rows = list(snapshot.get("items") or [])
        if status:
            rows = [row for row in rows if row.get("status") == str(status)[:40]]
        if not include_archived:
            rows = [row for row in rows if row.get("status") != "archived"]
        bounded_offset = max(0, int(offset or 0))
        rows = rows[bounded_offset : bounded_offset + max(1, min(int(limit or 100), 200))]
        response = {
            "ok": True,
            "entries": [_bounded_roadmap_row(row) for row in rows],
            "count": len(rows),
            "active": int(snapshot.get("active") or 0),
            "total": int(snapshot.get("total") or 0),
            "status_counts": dict(snapshot.get("status_counts") or {}),
            "truncated": bool(snapshot.get("truncated")),
        }
    except (roadmap_store.RoadmapError, OSError, sqlite3.Error, TypeError, ValueError) as exc:
        response = {"ok": False, "error": str(exc)[:240], "entries": []}
    return _needfix_response(response, "aiworkhub_dashboard_roadmap_list")


def roadmap_detail_view(roadmap_id: str, event_limit: int = 50) -> dict[str, Any]:
    """READ-ONLY: one Roadmap outcome with task/dependency and audit truth."""
    candidate = str(roadmap_id or "")
    if not roadmap_store.ROADMAP_ID_RE.fullmatch(candidate):
        return _needfix_response(
            {"ok": False, "error": "invalid_roadmap_id"},
            "aiworkhub_dashboard_roadmap_detail",
        )
    try:
        snapshot = core.roadmap_snapshot(limit=200)
        row = next(
            (item for item in snapshot.get("items") or [] if item.get("id") == candidate),
            None,
        )
        if row is None:
            row = core.roadmap_show(candidate)
        response = {
            "ok": True,
            "item": _bounded_roadmap_row(row, include_detail=True),
            "events": [
                dict(list(event.items())[:24])
                for event in core.roadmap_events(
                    candidate, limit=max(1, min(int(event_limit or 50), 100))
                )[:100]
            ],
        }
    except (roadmap_store.RoadmapError, OSError, sqlite3.Error, TypeError, ValueError) as exc:
        response = {"ok": False, "error": str(exc)[:240]}
    return _needfix_response(response, "aiworkhub_dashboard_roadmap_detail")


def needfix_capture_view(
    title: str,
    description: str,
    kind: str = "other",
    severity: str = "medium",
    scope: str | None = None,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    """USER WRITE: explicitly capture one dashboard-authored proposal."""
    try:
        row = core.needfix_capture(
            title=str(title or "")[:240],
            description=str(description or "")[:8000],
            kind=str(kind or "other")[:60],
            severity=str(severity or "medium")[:24],
            scope=str(scope or "")[:4000] or None,
            tags=[str(value)[:80] for value in list(tags or [])[:24]],
            provenance={"source": "dashboard_user"},
        )
        response = {"ok": True, "item": _bounded_needfix_row(row, include_detail=True)}
    except (needfix_store.NeedFixError, OSError, sqlite3.Error, TypeError, ValueError) as exc:
        response = {"ok": False, "error": str(exc)[:240]}
    return _needfix_response(response, "aiworkhub_dashboard_needfix_capture", write=True)


def needfix_update_view(
    needfix_id: str,
    title: str | None = None,
    description: str | None = None,
    scope: str | None = None,
    kind: str | None = None,
    severity: str | None = None,
    tags: list[str] | None = None,
    readiness_score: int | None = None,
) -> dict[str, Any]:
    """USER WRITE: update bounded mutable NeedFix fields."""
    candidate = str(needfix_id or "")
    try:
        row = core.needfix_update(
            candidate,
            title=str(title)[:240] if title is not None else None,
            description=str(description)[:8000] if description is not None else None,
            scope=str(scope)[:4000] if scope is not None else None,
            kind=str(kind)[:60] if kind is not None else None,
            severity=str(severity)[:24] if severity is not None else None,
            tags=[str(value)[:80] for value in list(tags)[:24]] if tags is not None else None,
            readiness_score=max(0, min(100, int(readiness_score))) if readiness_score is not None else None,
        )
        response = {"ok": True, "item": _bounded_needfix_row(row, include_detail=True)}
    except (needfix_store.NeedFixError, OSError, sqlite3.Error, TypeError, ValueError) as exc:
        response = {"ok": False, "error": str(exc)[:240]}
    return _needfix_response(response, "aiworkhub_dashboard_needfix_update", write=True)


def needfix_transition_view(
    needfix_id: str,
    action: str,
    reason: str = "",
    readiness_score: int | None = None,
    duplicate_parent_id: str = "",
    confirm: bool = False,
) -> dict[str, Any]:
    """USER WRITE: one explicit, confirmed NeedFix lifecycle transition."""
    candidate = str(needfix_id or "")
    selected = str(action or "").strip().lower()
    if confirm is not True:
        response = {"ok": False, "error": "needfix_transition_confirmation_required"}
    else:
        try:
            if selected == "triage":
                row = core.needfix_triage(candidate, readiness_score=readiness_score, triage_note=str(reason)[:1000] or None)
            elif selected == "accept":
                row = core.needfix_accept(candidate, readiness_score=readiness_score)
            elif selected == "reject":
                row = core.needfix_reject(candidate, reason=str(reason)[:1000])
            elif selected == "duplicate":
                row = core.needfix_mark_duplicate(candidate, str(duplicate_parent_id)[:32], reason=str(reason)[:1000] or None)
            elif selected == "defer":
                row = core.needfix_defer(candidate, reason=str(reason)[:1000] or None)
            elif selected == "task_planned":
                row = core.needfix_mark_task_planned(candidate)
            elif selected == "resolve":
                row = core.needfix_resolve(candidate, resolution_note=str(reason)[:1000] or None)
            elif selected == "resolve_verified":
                row = core.needfix_resolve_verified(
                    candidate, resolution_note=str(reason)[:1000]
                )
            else:
                return _needfix_response(
                    {"ok": False, "error": "invalid_needfix_transition"},
                    "aiworkhub_dashboard_needfix_transition",
                    write=True,
                )
            response = {"ok": True, "item": _bounded_needfix_row(row, include_detail=True)}
        except (needfix_store.NeedFixError, OSError, sqlite3.Error, TypeError, ValueError) as exc:
            response = {"ok": False, "error": str(exc)[:240]}
    return _needfix_response(response, "aiworkhub_dashboard_needfix_transition", write=True)


def needfix_archive_view(needfix_id: str, reason: str = "", confirm: bool = False) -> dict[str, Any]:
    if confirm is not True:
        response = {"ok": False, "error": "needfix_archive_confirmation_required"}
    else:
        try:
            row = core.needfix_archive(str(needfix_id or ""), reason=str(reason)[:1000] or None)
            response = {"ok": True, "item": _bounded_needfix_row(row, include_detail=True)}
        except (needfix_store.NeedFixError, OSError, sqlite3.Error, TypeError, ValueError) as exc:
            response = {"ok": False, "error": str(exc)[:240]}
    return _needfix_response(response, "aiworkhub_dashboard_needfix_archive", write=True)


def needfix_restore_view(needfix_id: str, target_status: str = "captured", confirm: bool = False) -> dict[str, Any]:
    if confirm is not True:
        response = {"ok": False, "error": "needfix_restore_confirmation_required"}
    else:
        try:
            row = core.needfix_restore(str(needfix_id or ""), target_status=str(target_status or "captured")[:40])
            response = {"ok": True, "item": _bounded_needfix_row(row, include_detail=True)}
        except (needfix_store.NeedFixError, OSError, sqlite3.Error, TypeError, ValueError) as exc:
            response = {"ok": False, "error": str(exc)[:240]}
    return _needfix_response(response, "aiworkhub_dashboard_needfix_restore", write=True)


def needfix_purge_view(needfix_id: str, audit_reason: str, confirm: bool = False) -> dict[str, Any]:
    if confirm is not True:
        response = {"ok": False, "error": "needfix_purge_confirmation_required"}
    else:
        try:
            response = dict(core.needfix_purge(str(needfix_id or ""), str(audit_reason or "")[:1000]))
            response.setdefault("ok", True)
        except (needfix_store.NeedFixError, OSError, sqlite3.Error, TypeError, ValueError) as exc:
            response = {"ok": False, "error": str(exc)[:240]}
    return _needfix_response(response, "aiworkhub_dashboard_needfix_purge", write=True)


def needfix_convert_preview_view(
    needfix_id: str, task_plan: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """READ-ONLY: normalized executable conversion card plus its plan_digest."""
    try:
        response = dict(
            core.needfix_preview_convert(str(needfix_id or ""), task_plan=task_plan)
        )
        response.setdefault("ok", True)
    except (needfix_store.NeedFixError, OSError, sqlite3.Error, TypeError, ValueError) as exc:
        response = {"ok": False, "error": str(exc)[:240]}
    return _needfix_response(response, "aiworkhub_dashboard_needfix_convert_preview")


def needfix_convert_commit_view(
    needfix_id: str,
    confirm: bool = False,
    task_plan: Mapping[str, Any] | None = None,
    plan_digest: str | None = None,
) -> dict[str, Any]:
    """USER WRITE: explicitly create a task card; never launch a worker.

    Every confirmed conversion must carry the exact ``plan_digest`` the
    matching preview returned, binding the commit to what the manager
    actually inspected. The rule is identical for an explicit
    ``task_plan`` and for the default scope-derived card: a missing
    digest, or a digest that no longer matches the freshly normalized
    card, fails closed instead of silently converting a plan nobody
    confirmed. Core's digest check runs only on the create path, so
    already-task_created retries keep short-circuiting without forcing a
    new create.
    """
    if confirm is not True:
        response = {"ok": False, "error": "needfix_conversion_confirmation_required"}
    elif not plan_digest:
        response = {"ok": False, "error": "needfix_conversion_plan_digest_required"}
    else:
        try:
            response = dict(
                core.needfix_convert(
                    str(needfix_id or ""),
                    task_plan=task_plan,
                    plan_digest=str(plan_digest) if plan_digest else None,
                )
            )
            response.setdefault("ok", True)
        except (needfix_store.NeedFixError, OSError, sqlite3.Error, TypeError, ValueError) as exc:
            response = {"ok": False, "error": str(exc)[:240]}
    return _needfix_response(response, "aiworkhub_dashboard_needfix_convert_commit", write=True)


def needfix_link_existing_task_view(
    needfix_id: str, existing_task_id: str, confirm: bool = False
) -> dict[str, Any]:
    """USER WRITE: manager-only, explicitly link a NeedFix to an existing, finished, accepted task."""
    if confirm is not True:
        response = {"ok": False, "error": "needfix_link_existing_task_confirmation_required"}
    else:
        try:
            response = dict(
                core.needfix_link_existing_task(str(needfix_id or ""), str(existing_task_id or ""))
            )
            response.setdefault("ok", True)
        except (needfix_store.NeedFixError, OSError, sqlite3.Error, TypeError, ValueError) as exc:
            response = {"ok": False, "error": str(exc)[:240]}
    return _needfix_response(response, "aiworkhub_dashboard_needfix_link_existing_task", write=True)


def needfix_reopen_superseded_task_link_view(
    needfix_id: str, reason: str, confirm: bool = False
) -> dict[str, Any]:
    """USER WRITE: reopen only an exact archived/superseded converted-task link."""
    if confirm is not True:
        response = {
            "ok": False,
            "error": "needfix_reopen_superseded_task_link_confirmation_required",
        }
    else:
        try:
            response = dict(
                core.needfix_reopen_superseded_task_link(
                    str(needfix_id or ""), str(reason or "")
                )
            )
            response.setdefault("ok", True)
        except (needfix_store.NeedFixError, OSError, sqlite3.Error, TypeError, ValueError) as exc:
            response = {"ok": False, "error": str(exc)[:240]}
    return _needfix_response(
        response,
        "aiworkhub_dashboard_needfix_reopen_superseded_task_link",
        write=True,
    )


def task_quarantine_view(
    preview_digest: str,
    older_than_days: int | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    """USER WRITE: quarantine a still-current old archived-task preview."""
    try:
        root = core.repo_root()
        response = task_retention.quarantine(
            root,
            preview_digest=str(preview_digest or "")[:128],
            older_than_days=older_than_days,
            confirm=confirm is True,
        )
        storage_observability.invalidate(root)
    except (task_retention.TaskRetentionError, ValueError, TypeError) as exc:
        response = {"ok": False, "error": str(exc)[:240]}
    response["server_tool"] = "aiworkhub_dashboard_task_quarantine"
    response["authority_flags"] = _storage_write_authority_flags()
    return response


def task_quarantine_restore_view(batch_id: str, confirm: bool = False) -> dict[str, Any]:
    """USER WRITE: restore one archived-task quarantine batch."""
    try:
        root = core.repo_root()
        response = task_retention.restore(
            root, batch_id=str(batch_id or "")[:128], confirm=confirm is True
        )
        storage_observability.invalidate(root)
    except task_retention.TaskRetentionError as exc:
        response = {"ok": False, "error": str(exc)[:240]}
    response["server_tool"] = "aiworkhub_dashboard_task_quarantine_restore"
    response["authority_flags"] = _storage_write_authority_flags()
    return response


def task_quarantine_purge_view(batch_id: str, confirm: bool = False) -> dict[str, Any]:
    """USER WRITE: permanently purge one expired archived-task batch."""
    try:
        root = core.repo_root()
        response = task_retention.purge(
            root, batch_id=str(batch_id or "")[:128], confirm=confirm is True
        )
        storage_observability.invalidate(root)
    except task_retention.TaskRetentionError as exc:
        response = {"ok": False, "error": str(exc)[:240]}
    response["server_tool"] = "aiworkhub_dashboard_task_quarantine_purge"
    response["authority_flags"] = _storage_write_authority_flags()
    return response


def health_view() -> dict[str, Any]:
    """READ-ONLY: cheap liveness check for the Webview's connection banner.

    Reads canonical repo-local storage directly.  The native dashboard must
    not depend on a repository shipping ``AITools/taskctl.py`` merely to
    render its connection banner.  Never calls ``dashboard.build_snapshot``:
    polling stays cheap and cannot pull the queue/cost/process payload.
    """
    # Use the same canonical resolver as every manager/task tool.  The VS Code
    # dashboard child carries explicit AIWORKHUB_REPO* bindings, while the
    # application-global Codex MCP registration intentionally omits them so
    # multiple repository windows cannot overwrite one another; that child is
    # repository-bound by its own process cwd.  Reading only env vars here made
    # dashboard_health falsely report repo_root_not_selected even though
    # manager_bootstrap and task_create were correctly bound to the current
    # repository.
    try:
        root_raw = str(core.repo_root())
    except (OSError, RuntimeError, task_store.TaskStoreError):
        root_raw = ""
    if not root_raw:
        result: dict[str, Any] = {
            "ok": False,
            "repo": "",
            "storage": {
                "ready": False,
                "reason": "repo_root_not_selected",
                "not_initialized": is_not_initialized_reason("repo_root_not_selected"),
            },
            "error": "repo_root_not_selected",
        }
    else:
        readiness = task_store.storage_readiness(root_raw)
        storage = readiness.as_dict()
        if not readiness.ready:
            storage["not_initialized"] = is_not_initialized_reason(readiness.reason)
        result = {
            "ok": bool(readiness.ready),
            "repo": root_raw,
            "storage": storage,
        }
    result["server_version"] = __version__
    result["server_tool"] = "aiworkhub_dashboard_health"
    result["authority_flags"] = _readonly_authority_flags()
    try:
        result["manager_identity"] = core.manager_bootstrap()
    except Exception as exc:  # noqa: BLE001 -- health surface must never raise
        result["manager_identity"] = {
            "ok": False,
            "role": "unknown",
            "provider": "unknown",
            "manager_route": {},
            "reason": f"manager_bootstrap_failed:{type(exc).__name__}",
        }
    # B857: best-effort dispatcher health -- never lets a dispatcher-side
    # failure break this cheap connection-banner check. An uninitialized
    # repository reports dispatcher status "uninitialized", not an error.
    try:
        result["dispatcher"] = core.dispatcher_health()
    except Exception as exc:  # noqa: BLE001 -- health surface must never raise
        result["dispatcher"] = {"ok": False, "status": "error", "error": f"dispatcher_health_failed:{type(exc).__name__}"}
    return result


def _initialize_authority_flags() -> dict[str, bool]:
    return {
        "readonly": False,
        "queue_write": False,
        "audit_write": False,
        "process_launch": False,
        "agent_launch": False,
        "shell_invocation": False,
        "repository_bootstrap_write": True,
    }


# Only a repo_id shaped like the extension's own generator output
# (``repo_<32 lowercase hex>``) is honored as an identity expectation. Any
# other value (e.g. the extension's "manifest-missing"/"manifest-invalid"
# placeholder labels for a never-initialized repository) is treated as "no
# expectation yet" so a legitimate first-time init is never refused by
# accidentally adopting a placeholder string as the permanent repo_id.
_REAL_REPO_ID_RE = re.compile(r"^repo_[a-f0-9]{32}$")


def initialize_view(repo_id: str = "") -> dict[str, Any]:
    """WRITE (bootstrap-only): the one bounded, idempotent, fail-closed
    initialization action the Webview's "Initialize AIWorkHub" button
    invokes.

    Bound to the active repository via ``AIWORKHUB_REPO_ROOT`` (the same env
    var the extension host spawns this child process with -- never a fixed
    path relative to this package's own install location) and, when the
    caller supplies one, an expected ``repo_id`` that must match any existing
    manifest exactly. Creates/repairs the repository-local canonical stores.
    Fresh repositories receive empty compatible databases; an older registry
    may migrate only its explicitly declared repo-local legacy SQLite files
    with a consistent backup. Legacy files are never deleted.
    """
    root = str(
        os.environ.get("AIWORKHUB_REPO_ROOT")
        or os.environ.get("AIWORKHUB_REPO")
        or ""
    ).strip()
    if not root:
        return {
            "ok": False,
            "error": "initialization_refused",
            "message": "repository_root_not_bound",
            "server_tool": "aiworkhub_dashboard_initialize",
            "authority_flags": _initialize_authority_flags(),
        }
    candidate = str(repo_id or "").strip()
    expected = candidate if _REAL_REPO_ID_RE.match(candidate) else None
    try:
        # Routes through repository_bootstrap.initialize_repository_full so
        # the same bounded action ALSO provisions the Source Graph directory
        # (never a second, separate init step) -- see repository_bootstrap.py.
        result = repository_bootstrap.initialize_repository_full(root, expected_repo_id=expected)
        # A pre-init snapshot can cache usage/readiness observations for this
        # process. Invalidate them before the caller immediately asks for the
        # authoritative post-init snapshot.
        storage_observability.invalidate(root)
    except task_store.InitializationRefusedError as exc:
        return {
            "ok": False,
            "error": "initialization_refused",
            "message": str(exc)[:300],
            "server_tool": "aiworkhub_dashboard_initialize",
            "authority_flags": _initialize_authority_flags(),
        }
    except task_store.TaskStoreError as exc:
        return {
            "ok": False,
            "error": "initialization_failed",
            "message": str(exc)[:300],
            "server_tool": "aiworkhub_dashboard_initialize",
            "authority_flags": _initialize_authority_flags(),
        }
    response = dict(result)
    response["server_tool"] = "aiworkhub_dashboard_initialize"
    response["authority_flags"] = _initialize_authority_flags()
    return response


def task_live_output_view(task_id: str, cursor: int = 0) -> dict[str, Any]:
    """READ-ONLY: bounded, single-task Live Output read for the dashboard's
    selected-task panel.

    Validates ``task_id`` with the same pattern every other task-scoped tool
    enforces (``dashboard._TASK_ID_RE``) BEFORE calling
    ``process_launcher.read_live_output_for_task`` -- the ONLY task this call
    ever reads process-log/stdout/stderr evidence for; there is no
    dashboard-wide fan-out across other tasks. An invalid task_id, or any
    unexpected failure resolving the active repository, returns a bounded
    ``ok: false`` object; this never raises.
    """
    candidate = str(task_id or "").strip()
    if not dashboard._TASK_ID_RE.fullmatch(candidate):
        return {
            "ok": False,
            "error": "invalid_task_id",
            "server_tool": "aiworkhub_dashboard_task_live_output",
            "authority_flags": _readonly_authority_flags(),
        }
    try:
        safe_cursor = max(0, int(cursor))
    except (TypeError, ValueError):
        safe_cursor = 0

    try:
        repo_root = dashboard._default_repo_root()
        result = process_launcher.read_live_output_for_task(
            candidate,
            repo=repo_root,
            cursor=safe_cursor,
            max_bytes=MAX_LIVE_OUTPUT_BYTES,
        )
    except Exception as exc:  # noqa: BLE001 - fail closed, one task's read never crashes the tool call
        result = {
            "ok": False,
            "task_id": candidate,
            "error": "output_unavailable",
            "reason": f"repository_unresolved:{type(exc).__name__}",
            "cursor": safe_cursor,
            "next_cursor": safe_cursor,
            "truncated": False,
            "output": "",
            "stderr_tail": "",
        }
    response = dict(result)
    response["server_tool"] = "aiworkhub_dashboard_task_live_output"
    response["authority_flags"] = _readonly_authority_flags()
    return response


READONLY_TOOL_NAMES: tuple[str, ...] = (
    "aiworkhub_dashboard_snapshot",
    "aiworkhub_dashboard_task_detail",
    "aiworkhub_dashboard_health",
)

# Stable tool_name -> callable map for server wiring / introspection. Kept
# to this exact set of three (unchanged since B615/B853) so any existing
# consumer asserting this precise tuple keeps passing; the newer
# task-live-output tool is additive and lives in LIVE_OUTPUT_TOOLS below.
READONLY_TOOLS: dict[str, Any] = {
    "aiworkhub_dashboard_snapshot": snapshot_view,
    "aiworkhub_dashboard_task_detail": task_detail_view,
    "aiworkhub_dashboard_health": health_view,
}

# The one bounded write-capable tool: repository initialization. Kept
# separate from READONLY_TOOLS/READONLY_TOOL_NAMES so its non-read-only
# authority is never accidentally treated as part of the read-only surface.
INITIALIZE_TOOL_NAME = "aiworkhub_dashboard_initialize"
INITIALIZE_TOOLS: dict[str, Any] = {INITIALIZE_TOOL_NAME: initialize_view}

# B855: the single-task Live Output tool. Read-only (no queue/audit write,
# no process launch) exactly like READONLY_TOOLS, but kept in its own
# name/dict pair -- additive to the historical READONLY_TOOL_NAMES/
# READONLY_TOOLS tuple/dict so an existing exact-tuple assertion on those two
# names (see tests/test_aiworkhub_vscode_release_b853.py) keeps passing
# unchanged.
LIVE_OUTPUT_TOOL_NAME = "aiworkhub_dashboard_task_live_output"
LIVE_OUTPUT_TOOLS: dict[str, Any] = {LIVE_OUTPUT_TOOL_NAME: task_live_output_view}
MEMORY_TOOL_NAME = "aiworkhub_dashboard_memory"
MEMORY_TOOLS: dict[str, Any] = {MEMORY_TOOL_NAME: memory_view}
SESSION_TOOL_NAME = "aiworkhub_dashboard_sessions"
SESSION_TOOLS: dict[str, Any] = {SESSION_TOOL_NAME: session_view}
KB_TOOL_NAME = "aiworkhub_dashboard_kb"
KB_TOOLS: dict[str, Any] = {KB_TOOL_NAME: kb_view}
NEEDFIX_READ_TOOLS: dict[str, Any] = {
    "aiworkhub_dashboard_needfix_list": needfix_list_view,
    "aiworkhub_dashboard_needfix_detail": needfix_detail_view,
    "aiworkhub_dashboard_needfix_convert_preview": needfix_convert_preview_view,
}
NEEDFIX_WRITE_TOOLS: dict[str, Any] = {
    "aiworkhub_dashboard_needfix_capture": needfix_capture_view,
    "aiworkhub_dashboard_needfix_update": needfix_update_view,
    "aiworkhub_dashboard_needfix_transition": needfix_transition_view,
    "aiworkhub_dashboard_needfix_archive": needfix_archive_view,
    "aiworkhub_dashboard_needfix_restore": needfix_restore_view,
    "aiworkhub_dashboard_needfix_purge": needfix_purge_view,
    "aiworkhub_dashboard_needfix_convert_commit": needfix_convert_commit_view,
    "aiworkhub_dashboard_needfix_link_existing_task": needfix_link_existing_task_view,
    "aiworkhub_dashboard_needfix_reopen_superseded_task_link": needfix_reopen_superseded_task_link_view,
}
ROADMAP_READ_TOOLS: dict[str, Any] = {
    "aiworkhub_dashboard_roadmap_list": roadmap_list_view,
    "aiworkhub_dashboard_roadmap_detail": roadmap_detail_view,
}
SETTINGS_TOOL_NAME = "aiworkhub_dashboard_settings"
SETTINGS_TOOLS: dict[str, Any] = {SETTINGS_TOOL_NAME: settings_view}
SETTINGS_UPDATE_TOOL_NAME = "aiworkhub_dashboard_settings_update"
SETTINGS_UPDATE_TOOLS: dict[str, Any] = {SETTINGS_UPDATE_TOOL_NAME: settings_update_view}
SOURCE_GRAPH_SETTINGS_UPDATE_TOOL_NAME = "aiworkhub_dashboard_source_graph_settings_update"
SOURCE_GRAPH_SETTINGS_UPDATE_TOOLS: dict[str, Any] = {
    SOURCE_GRAPH_SETTINGS_UPDATE_TOOL_NAME: source_graph_settings_update_view,
}
STORAGE_RETENTION_PREVIEW_TOOL_NAME = "aiworkhub_dashboard_storage_retention_preview"
STORAGE_RETENTION_READ_TOOLS: dict[str, Any] = {
    STORAGE_RETENTION_PREVIEW_TOOL_NAME: storage_retention_preview_view,
}
STORAGE_RETENTION_WRITE_TOOLS: dict[str, Any] = {
    "aiworkhub_dashboard_storage_quarantine": storage_quarantine_view,
    "aiworkhub_dashboard_storage_registration_prune": storage_registration_prune_view,
    "aiworkhub_dashboard_storage_restore": storage_restore_view,
    "aiworkhub_dashboard_storage_purge": storage_purge_view,
}
TERMINAL_LOG_RETENTION_PREVIEW_TOOL_NAME = "aiworkhub_dashboard_terminal_log_retention_preview"
TERMINAL_LOG_RETENTION_READ_TOOLS: dict[str, Any] = {
    TERMINAL_LOG_RETENTION_PREVIEW_TOOL_NAME: terminal_log_retention_preview_view,
}
TERMINAL_LOG_RETENTION_WRITE_TOOLS: dict[str, Any] = {
    "aiworkhub_dashboard_terminal_log_usage_backfill": terminal_log_usage_backfill_view,
    "aiworkhub_dashboard_terminal_log_quarantine": terminal_log_quarantine_view,
    "aiworkhub_dashboard_terminal_log_restore": terminal_log_restore_view,
    "aiworkhub_dashboard_terminal_log_purge": terminal_log_purge_view,
}
TASK_RETENTION_PREVIEW_TOOL_NAME = "aiworkhub_dashboard_task_retention_preview"
TASK_RETENTION_READ_TOOLS: dict[str, Any] = {
    TASK_RETENTION_PREVIEW_TOOL_NAME: task_retention_preview_view,
}
TASK_RETENTION_WRITE_TOOLS: dict[str, Any] = {
    "aiworkhub_dashboard_task_archive": task_archive_view,
    "aiworkhub_dashboard_task_restore": task_restore_view,
    "aiworkhub_dashboard_task_quarantine": task_quarantine_view,
    "aiworkhub_dashboard_task_quarantine_restore": task_quarantine_restore_view,
    "aiworkhub_dashboard_task_quarantine_purge": task_quarantine_purge_view,
}


def register(mcp: Any) -> tuple[str, ...]:
    """Register the dashboard MCP tools; return their names.

    Accepts any object exposing a FastMCP-style ``tool(name=...)`` decorator
    factory (mirrors ``cli_adapter_readonly_tool.register``). Every tool in
    ``READONLY_TOOLS`` plus ``LIVE_OUTPUT_TOOLS`` (snapshot, task detail,
    health, task live output) is read-only: none writes queue/audit state
    and none launches a process. ``aiworkhub_dashboard_initialize`` is the
    sole bounded exception -- it only ever creates/repairs the repository's
    own ``.aiworkhub`` manifest, storage registry, canonical task DB, and
    Source Graph directory (see
    ``repository_bootstrap.initialize_repository_full``).
    """
    for name, fn in READONLY_TOOLS.items():
        mcp.tool(name=name)(fn)
    for name, fn in LIVE_OUTPUT_TOOLS.items():
        mcp.tool(name=name)(fn)
    for name, fn in MEMORY_TOOLS.items():
        mcp.tool(name=name)(fn)
    for name, fn in SESSION_TOOLS.items():
        mcp.tool(name=name)(fn)
    for name, fn in KB_TOOLS.items():
        mcp.tool(name=name)(fn)
    for name, fn in NEEDFIX_READ_TOOLS.items():
        mcp.tool(name=name)(fn)
    for name, fn in NEEDFIX_WRITE_TOOLS.items():
        mcp.tool(name=name)(fn)
    for name, fn in ROADMAP_READ_TOOLS.items():
        mcp.tool(name=name)(fn)
    for name, fn in SETTINGS_TOOLS.items():
        mcp.tool(name=name)(fn)
    for name, fn in SETTINGS_UPDATE_TOOLS.items():
        mcp.tool(name=name)(fn)
    for name, fn in SOURCE_GRAPH_SETTINGS_UPDATE_TOOLS.items():
        mcp.tool(name=name)(fn)
    for name, fn in STORAGE_RETENTION_READ_TOOLS.items():
        mcp.tool(name=name)(fn)
    for name, fn in STORAGE_RETENTION_WRITE_TOOLS.items():
        mcp.tool(name=name)(fn)
    for name, fn in TERMINAL_LOG_RETENTION_READ_TOOLS.items():
        mcp.tool(name=name)(fn)
    for name, fn in TERMINAL_LOG_RETENTION_WRITE_TOOLS.items():
        mcp.tool(name=name)(fn)
    for name, fn in TASK_RETENTION_READ_TOOLS.items():
        mcp.tool(name=name)(fn)
    for name, fn in TASK_RETENTION_WRITE_TOOLS.items():
        mcp.tool(name=name)(fn)
    for name, fn in INITIALIZE_TOOLS.items():
        mcp.tool(name=name)(fn)
    return READONLY_TOOL_NAMES + (
        LIVE_OUTPUT_TOOL_NAME,
        MEMORY_TOOL_NAME,
        SESSION_TOOL_NAME,
        KB_TOOL_NAME,
        SETTINGS_TOOL_NAME,
        STORAGE_RETENTION_PREVIEW_TOOL_NAME,
        TERMINAL_LOG_RETENTION_PREVIEW_TOOL_NAME,
        TASK_RETENTION_PREVIEW_TOOL_NAME,
    ) + (
        tuple(STORAGE_RETENTION_WRITE_TOOLS)
        + tuple(TERMINAL_LOG_RETENTION_WRITE_TOOLS)
        + tuple(TASK_RETENTION_WRITE_TOOLS)
        + tuple(NEEDFIX_READ_TOOLS)
        + tuple(NEEDFIX_WRITE_TOOLS)
        + tuple(ROADMAP_READ_TOOLS)
        + tuple(SETTINGS_UPDATE_TOOLS)
        + tuple(SOURCE_GRAPH_SETTINGS_UPDATE_TOOLS)
        + (INITIALIZE_TOOL_NAME,)
    )
