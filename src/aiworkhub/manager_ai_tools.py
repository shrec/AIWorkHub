"""Manager-bound access to AIWorkHub's canonical project intelligence.

The task-scoped worker MCP and the manager MCP deliberately expose separate
tool names, but both call the same bounded in-process implementations and the
same repository-local canonical databases.  A caller cannot select another
repository or manufacture a manager identity.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from . import core
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


def source_graph_query(*, mode: str, query: str, budget: int = 64, target: str | None = None, bundle_type: str = "explore") -> dict[str, Any]:
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


def kb_search(*, query: str, limit: int = 8) -> dict[str, Any]:
    return _invoke(lambda ctx: worker_tools.kb_search(ctx, query=query, limit=limit))


def kb_get(*, key: str) -> dict[str, Any]:
    return _invoke(lambda ctx: worker_tools.kb_get(ctx, key=key))


def kb_related(*, key: str) -> dict[str, Any]:
    return _invoke(lambda ctx: worker_tools.kb_related(ctx, key=key))


__all__ = [
    "ai_memory_search",
    "kb_get",
    "kb_related",
    "kb_search",
    "session_current_state",
    "source_graph_query",
]
