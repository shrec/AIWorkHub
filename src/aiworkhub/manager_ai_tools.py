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
from . import storage_registry
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
        source_graph_targets=(str(target),) if target else (),
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
) -> dict[str, Any]:
    context, manager = _manager_context(topic=topic)
    if context is None:
        return manager
    if not core.writes_allowed():
        return {"ok": False, "error": "write_gate_closed", "surface": "manager_mcp", "manager": manager}
    actor = {
        "role": "manager",
        "actor_id": manager["session_id"],
        "task_id": "",
        "provider": manager["provider"],
        "session_id": manager["session_id"],
    }
    try:
        result = dict(call(context.authority_repo, actor))
    except context_writes.ContextWriteError as exc:
        result = {"ok": False, "error": str(exc)[:240]}
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
    bundle_type: worker_tools.SourceGraphBundleType = "explore",
) -> dict[str, Any]:
    return _invoke(
        lambda ctx: worker_tools.source_graph_query(
            ctx, mode=mode, query=query, budget=budget,
            target=target, bundle_type=bundle_type,
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


def session_write(
    *, action: context_writes.SessionAction, topic: str, content: str,
    idempotency_key: str, provenance: str,
) -> dict[str, Any]:
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
    return _write_invoke(
        lambda repo, actor: context_writes.kb_write(
            repo, actor=actor, action=action, key=key, title=title, body=body,
            category=category, tags=tags, source_refs=source_refs,
            replacement_key=replacement_key, idempotency_key=idempotency_key,
            provenance=provenance,
        )
    )


__all__ = [
    "ai_memory_get",
    "ai_memory_related",
    "ai_memory_search",
    "ai_memory_write",
    "kb_get",
    "kb_related",
    "kb_search",
    "kb_write",
    "session_current_state",
    "session_write",
    "source_graph_query",
]
