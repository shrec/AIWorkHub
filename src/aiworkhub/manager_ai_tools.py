"""Manager-bound access to AIWorkHub's canonical project intelligence.

The task-scoped worker MCP and the manager MCP deliberately expose separate
tool names, but both call the same bounded in-process implementations and the
same repository-local canonical databases.  A caller cannot select another
repository or manufacture a manager identity.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Callable

from . import core
from . import context_writes
from . import context_importer
from . import context_graph
from . import cost_ledger
from . import feature_settings
from . import learning_commit_store
from . import needfix_ingest
from . import storage_registry
from . import task_decomposition
from . import workforce_catalog
from . import workforce_router
from . import worker_ai_tools_mcp as worker_tools


def _manager_context(*, topic: str = "management", target: str | None = None) -> tuple[worker_tools.WorkerToolContext | None, dict[str, Any]]:
    route = core.manager_bootstrap()
    identity = route.get("manager_route") if isinstance(route, dict) else None
    if route.get("role") != "manager" or not isinstance(identity, dict):
        return None, {"ok": False, "error": "verified_manager_identity_required"}
    session_id = str(identity.get("thread_id") or identity.get("session_id") or "").strip()
    provider = str(identity.get("provider") or route.get("provider") or "manager").strip()
    if not session_id:
        return None, {"ok": False, "error": "manager_session_identity_missing"}
    root = Path(str(route.get("repo") or core.repo_root())).resolve()
    context = worker_tools.WorkerToolContext(
        task_id=f"manager:{session_id}",
        runner=f"{provider}_manager",
        topic=str(topic or "management")[:128],
        request_id=session_id,
        repo=root,
        authority_repo=root,
        # A manager query's ``target`` is a QUERY SCOPE, not a security
        # allowlist, and the two must not be conflated.  Passing it through as
        # ``source_graph_targets`` made the manager's own selector its only
        # permitted path: a ``body``/``function`` target is a QUALNAME, the
        # resolved symbol's FILE never equals it, and the selector enforcement
        # then refused the manager's primary discovery surface with
        # ``symbol_out_of_scope`` -- the same qualname-as-path confusion
        # NF-2026-00348 removed one level down.  The scope still reaches the
        # engine through ``source_graph_query(target=...)``; what it must not do
        # is narrow the authority of a role that has repository-wide authority
        # by definition.  Task-scoped allowlists belong to WORKERS, which is
        # where they are still enforced.
        source_graph_targets=(),
        session_topic=str(topic or "management")[:128],
        audit_ledger_path=None,
        audit_hmac_key_path=None,
    )
    return context, {"provider": provider, "session_id": session_id, "repo": str(root)}


def _invoke(call: Callable[[worker_tools.WorkerToolContext], dict[str, Any]], *, topic: str = "management", target: str | None = None) -> dict[str, Any]:
    context, manager = _manager_context(topic=topic, target=target)
    if context is None:
        return manager
    result = dict(call(context))
    result["manager"] = manager
    result["surface"] = "manager_mcp"
    return result


def _write_invoke(
    call: Callable[[Path, dict[str, str]], dict[str, Any]], *, topic: str = "management",
    task_id: str = "",
) -> dict[str, Any]:
    context, manager = _manager_context(topic=topic)
    if context is None:
        return manager
    if not core.writes_allowed():
        return {"ok": False, "error": "write_gate_closed", "surface": "manager_mcp", "manager": manager}
    actor = {
        "role": "manager",
        "actor_id": manager["session_id"],
        "task_id": str(task_id or "")[:256],
        "provider": manager["provider"],
        "session_id": manager["session_id"],
    }
    try:
        result = dict(call(context.authority_repo, actor))
    except (
        context_writes.ContextWriteError,
        learning_commit_store.LearningCommitStoreError,
        needfix_ingest.NeedFixIngestError,
        workforce_catalog.WorkforceCatalogError,
    ) as exc:
        result = {"ok": False, "error": str(exc)[:240]}
    except sqlite3.IntegrityError as exc:
        result = {
            "ok": False,
            "error": "context_write_integrity_error",
            "sqlite_errorname": str(getattr(exc, "sqlite_errorname", "SQLITE_CONSTRAINT")),
            "constraint": str(exc).split(":", 1)[0][:120],
            "recovery_action": "retry_with_same_idempotency_key_or_use_update",
        }
    except (OSError, sqlite3.Error, storage_registry.StorageRegistryError) as exc:
        result = {"ok": False, "error": f"context_write_failed:{type(exc).__name__}"}
    result["manager"] = manager
    result["surface"] = "manager_mcp"
    return result


def source_graph_query(
    *,
    mode: worker_tools.SourceGraphMode,
    query: str,
    budget: int = 64,
    target: str | None = None,
    cursor: str | None = None,
    continuation_cursor: str | None = None,
    bundle_type: worker_tools.SourceGraphBundleType = "explore",
    workflow_stage: worker_tools.WorkflowStage = "unspecified",
    compact_replay: bool = True,
) -> dict[str, Any]:
    return _invoke(
        lambda ctx: worker_tools.source_graph_query(
            ctx, mode=mode, query=query, budget=budget,
            target=target, cursor=cursor,
            continuation_cursor=continuation_cursor,
            bundle_type=bundle_type, workflow_stage=workflow_stage,
            compact_replay=compact_replay,
        ),
        target=target,
    )


def session_current_state(*, topic: str = "management", limit: int = 12) -> dict[str, Any]:
    return _invoke(
        lambda ctx: worker_tools.session_current_state(ctx, limit=limit),
        topic=topic,
    )


def ai_memory_search(*, query: str, limit: int = 8) -> dict[str, Any]:
    return _invoke(lambda ctx: worker_tools.ai_memory_search(ctx, query=query, limit=limit))


def ai_memory_get(*, key: str) -> dict[str, Any]:
    return _invoke(lambda ctx: worker_tools.ai_memory_get(ctx, key=key))


def ai_memory_related(*, key: str) -> dict[str, Any]:
    return _invoke(lambda ctx: worker_tools.ai_memory_related(ctx, key=key))


def kb_search(*, query: str, limit: int = 8) -> dict[str, Any]:
    return _invoke(lambda ctx: worker_tools.kb_search(ctx, query=query, limit=limit))


def kb_get(*, key: str) -> dict[str, Any]:
    return _invoke(lambda ctx: worker_tools.kb_get(ctx, key=key))


def kb_related(*, key: str) -> dict[str, Any]:
    return _invoke(lambda ctx: worker_tools.kb_related(ctx, key=key))


def context_graph_search(*, query: str, limit: int = 12) -> dict[str, Any]:
    context, manager = _manager_context()
    if context is None:
        return manager
    try:
        result = context_graph.search(context.authority_repo, query, limit=limit)
        context_graph.record_query_telemetry(
            context.authority_repo, operation="search", result=result
        )
    except (context_graph.ContextGraphError, OSError, sqlite3.Error) as exc:
        result = {"ok": False, "error": str(exc)[:240]}
    return {**result, "manager": manager, "surface": "manager_mcp"}


def context_graph_range(
    *, thread_id: str, around_event_id: int, before: int = 5, after: int = 5,
) -> dict[str, Any]:
    context, manager = _manager_context()
    if context is None:
        return manager
    try:
        result = context_graph.get_range(
            context.authority_repo,
            thread_id=thread_id,
            around_event_id=around_event_id,
            before=before,
            after=after,
        )
        context_graph.record_query_telemetry(
            context.authority_repo, operation="range", result=result
        )
    except (context_graph.ContextGraphError, OSError, sqlite3.Error) as exc:
        result = {"ok": False, "error": str(exc)[:240]}
    return {**result, "manager": manager, "surface": "manager_mcp"}


def context_graph_related(*, node_id: str, limit: int = 20) -> dict[str, Any]:
    context, manager = _manager_context()
    if context is None:
        return manager
    try:
        result = context_graph.related(context.authority_repo, node_id=node_id, limit=limit)
        context_graph.record_query_telemetry(
            context.authority_repo, operation="related", result=result
        )
    except (context_graph.ContextGraphError, OSError, sqlite3.Error) as exc:
        result = {"ok": False, "error": str(exc)[:240]}
    return {**result, "manager": manager, "surface": "manager_mcp"}


def context_graph_event_write(
    *, role: str, event_type: str, content: str, source_ref: str,
    idempotency_key: str, task_id: str = "", metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    context, manager = _manager_context()
    if context is None:
        return manager
    if not core.writes_allowed():
        return {"ok": False, "error": "write_gate_closed", "manager": manager, "surface": "manager_mcp"}
    try:
        result = context_graph.append_event(
            context.authority_repo,
            thread_id=manager["session_id"],
            session_id=manager["session_id"],
            provider=manager["provider"],
            role=role,
            event_type=event_type,
            content=content,
            source_ref=source_ref,
            idempotency_key=idempotency_key,
            task_id=task_id,
            metadata=metadata,
        )
    except (context_graph.ContextGraphError, OSError, sqlite3.Error) as exc:
        result = {"ok": False, "error": str(exc)[:240]}
    return {**result, "manager": manager, "surface": "manager_mcp"}


def context_graph_rebuild() -> dict[str, Any]:
    context, manager = _manager_context()
    if context is None:
        return manager
    if not core.writes_allowed():
        return {"ok": False, "error": "write_gate_closed", "manager": manager, "surface": "manager_mcp"}
    try:
        result = context_graph.rebuild_projection(context.authority_repo)
    except (context_graph.ContextGraphError, OSError, sqlite3.Error) as exc:
        result = {"ok": False, "error": str(exc)[:240]}
    return {**result, "manager": manager, "surface": "manager_mcp"}


def _workforce_process_rows(repo: Path) -> list[dict[str, Any]]:
    """Read one authority repository's bounded process ledger without reconcile."""
    # Import lazily: dashboard imports workforce_catalog during server
    # bootstrap. Its reader is intentionally read-only and receives the exact
    # authority repo's process log, avoiding the ambient/default ProcessManager
    # that may belong to another VS Code window.
    from . import dashboard

    report = dashboard.read_process_runs(
        process_log_path=(repo / ".aiworkhub/runtime/process_logs/process_events.jsonl"),
        limit=1000,
    )
    return [dict(item) for item in report.get("processes") or [] if isinstance(item, dict)]


def workforce_catalog_read() -> dict[str, Any]:
    def call(ctx: worker_tools.WorkerToolContext) -> dict[str, Any]:
        ledger = cost_ledger.build_cost_ledger(
            repo_root=ctx.authority_repo, include_tasks=True
        )
        return workforce_catalog.build_catalog(
            ctx.authority_repo,
            process_rows=_workforce_process_rows(ctx.authority_repo),
            usage_rows=ledger.get("tasks") or [],
            cost_per_accepted_outcome=ledger.get("cost_per_accepted_outcome") or {},
        )

    return _invoke(
        call
    )


def workforce_rank(
    *,
    task_id: str,
    kinds: list[str],
    risk: str = "medium",
    context_tokens: int = 0,
    tool_needs: list[str] | None = None,
    quality_floor: float = 0.0,
) -> dict[str, Any]:
    def call(ctx: worker_tools.WorkerToolContext) -> dict[str, Any]:
        task = workforce_router.TaskRequirements.build(
            task_id=task_id,
            repo_id=core.repository_current().get("repo_id") or "unknown",
            kinds=kinds,
            risk=risk,
            context_tokens=context_tokens,
            tool_needs=tool_needs or [],
            quality_floor=quality_floor,
        )
        ledger = cost_ledger.build_cost_ledger(
            repo_root=ctx.authority_repo, include_tasks=True
        )
        snapshot = workforce_catalog.build_catalog(
            ctx.authority_repo,
            process_rows=_workforce_process_rows(ctx.authority_repo),
            usage_rows=ledger.get("tasks") or [],
            cost_per_accepted_outcome=ledger.get("cost_per_accepted_outcome") or {},
        )
        return workforce_catalog.rank_task(ctx.authority_repo, task, catalog=snapshot)

    return _invoke(call)


def task_decomposition_preview(
    *,
    parent_task_id: str,
    objective: str,
    source_graph_receipt: dict[str, Any],
    children: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build a pure Source Graph-grounded child-DAG proposal."""

    def call(ctx: worker_tools.WorkerToolContext) -> dict[str, Any]:
        try:
            return task_decomposition.build_proposal(
                ctx.authority_repo,
                parent_task_id=parent_task_id,
                objective=objective,
                source_graph_receipt=source_graph_receipt,
                children=children,
            )
        except task_decomposition.TaskDecompositionError as exc:
            return {"ok": False, "error": str(exc)[:500]}

    return _invoke(call, topic="task_decomposition")


def workforce_upsert(*, worker: dict[str, Any]) -> dict[str, Any]:
    return _write_invoke(
        lambda repo, actor: workforce_catalog.upsert_worker(repo, worker, actor=actor),
        topic="workforce",
    )


def session_write(
    *, action: context_writes.SessionAction, topic: str, content: str,
    idempotency_key: str, provenance: str,
) -> dict[str, Any]:
    context, manager = _manager_context(topic=topic)
    if context is not None and not feature_settings.enabled(context.authority_repo, "session_manager"):
        return {**feature_settings.disabled_result("session_manager"), "manager": manager, "surface": "manager_mcp"}
    return _write_invoke(
        lambda repo, actor: context_writes.session_write(
            repo, actor=actor, action=action, topic=topic, content=content,
            idempotency_key=idempotency_key, provenance=provenance,
        ),
        topic=topic,
    )


def ai_memory_write(
    *, action: context_writes.MemoryAction, key: str, value: str = "",
    tags: str = "", scope: str = "project", idempotency_key: str,
    provenance: str,
) -> dict[str, Any]:
    context, manager = _manager_context()
    if context is not None and not feature_settings.enabled(context.authority_repo, "ai_memory"):
        return {**feature_settings.disabled_result("ai_memory"), "manager": manager, "surface": "manager_mcp"}
    return _write_invoke(
        lambda repo, actor: context_writes.memory_write(
            repo, actor=actor, action=action, key=key, value=value, tags=tags,
            scope=scope, idempotency_key=idempotency_key, provenance=provenance,
        )
    )


def kb_write(
    *, action: context_writes.KbAction, key: str, title: str = "", body: str = "",
    category: str = "", tags: str = "", source_refs: str = "",
    replacement_key: str = "", idempotency_key: str, provenance: str,
) -> dict[str, Any]:
    context, manager = _manager_context()
    if context is not None and not feature_settings.enabled(context.authority_repo, "knowledge_base"):
        return {**feature_settings.disabled_result("knowledge_base"), "manager": manager, "surface": "manager_mcp"}
    return _write_invoke(
        lambda repo, actor: context_writes.kb_write(
            repo, actor=actor, action=action, key=key, title=title, body=body,
            category=category, tags=tags, source_refs=source_refs,
            replacement_key=replacement_key, idempotency_key=idempotency_key,
            provenance=provenance,
        )
    )


def learning_commit(
    *,
    task_id: str,
    request_id: str,
    repo_area: str,
    outcome: str,
    evidence_ids: list[str],
    idempotency_key: str,
    provenance: str,
    root_cause_candidate: str = "",
    invariant_candidate: str = "",
    lesson_candidate: str = "",
    edge_candidates: list[dict[str, str]] | None = None,
    promote_ai_memory: bool = False,
    promote_context_graph: bool = False,
    promote_kb: bool = False,
) -> dict[str, Any]:
    """Commit one explicit manager learning decision after adjudication."""
    data = {
        "task_id": task_id,
        "repo_area": repo_area,
        "outcome": outcome,
        "evidence_ids": evidence_ids,
        "root_cause_candidate": root_cause_candidate or None,
        "invariant_candidate": invariant_candidate or None,
        "lesson_candidate": lesson_candidate or None,
        "edge_candidates": edge_candidates or [],
        "promotion_eligible_ai_memory": bool(promote_ai_memory),
        "promotion_eligible_context_graph": bool(promote_context_graph),
        "promotion_eligible_kb": bool(promote_kb),
    }
    return _write_invoke(
        lambda repo, actor: learning_commit_store.commit_learning(
            repo,
            actor=actor,
            request_id=request_id,
            data=data,
            idempotency_key=idempotency_key,
            provenance=provenance,
        ),
        topic="learning_commit",
        task_id=task_id,
    )


def needfix_markdown_preview(
    *, source_paths: list[str] | None = None, follow_links: bool = True,
) -> dict[str, Any]:
    def call(context: worker_tools.WorkerToolContext) -> dict[str, Any]:
        try:
            return needfix_ingest.preview(
                context.authority_repo,
                source_paths=source_paths,
                follow_links=follow_links,
            )
        except needfix_ingest.NeedFixIngestError as exc:
            return {"ok": False, "error": str(exc)[:240]}

    return _invoke(call, topic="needfix_markdown_intake")


def needfix_markdown_commit(
    *, source_paths: list[str] | None, preview_id: str, follow_links: bool = True,
) -> dict[str, Any]:
    return _write_invoke(
        lambda repo, _actor: needfix_ingest.commit(
            repo,
            source_paths=source_paths,
            preview_id=preview_id,
            follow_links=follow_links,
        ),
        topic="needfix_markdown_intake",
    )


def context_import(
    *, component: context_importer.Component, operation: context_importer.Operation,
    source_path: str = "", idempotency_key: str = "", import_id: str = "",
    provenance: str = "", limit: int = 10_000,
) -> dict[str, Any]:
    context, manager = _manager_context(topic="context_import")
    if context is None:
        return manager
    if operation != "dry_run" and not core.writes_allowed():
        return {
            "ok": False, "error": "write_gate_closed",
            "surface": "manager_mcp", "manager": manager,
        }
    try:
        result = context_importer.import_context(
            context.authority_repo,
            component=component,
            operation=operation,
            source_path=source_path,
            idempotency_key=idempotency_key,
            import_id=import_id,
            limit=limit,
            actor_id=manager["session_id"],
            provider=manager["provider"],
            provenance=provenance,
        )
    except context_importer.ContextImportError as exc:
        result = {"ok": False, "error": str(exc)[:240]}
    except (OSError, sqlite3.Error, storage_registry.StorageRegistryError) as exc:
        result = {"ok": False, "error": f"context_import_failed:{type(exc).__name__}"}
    result["manager"] = manager
    result["surface"] = "manager_mcp"
    return result


__all__ = [
    "ai_memory_get",
    "ai_memory_related",
    "ai_memory_search",
    "ai_memory_write",
    "context_graph_event_write",
    "context_graph_range",
    "context_graph_rebuild",
    "context_graph_related",
    "context_graph_search",
    "context_import",
    "kb_get",
    "kb_related",
    "kb_search",
    "kb_write",
    "learning_commit",
    "needfix_markdown_commit",
    "needfix_markdown_preview",
    "session_current_state",
    "session_write",
    "source_graph_query",
]
