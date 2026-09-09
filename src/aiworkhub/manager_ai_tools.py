"""Manager-bound access to AIWorkHub's canonical project intelligence.

The task-scoped worker MCP and the manager MCP deliberately expose separate
tool names, but both call the same bounded in-process implementations and the
same repository-local canonical databases.  A caller cannot select another
repository or manufacture a manager identity.
"""

from __future__ import annotations

import dataclasses
import os
import secrets
import sqlite3
import threading
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from . import core
from . import context_writes
from . import context_importer
from . import context_graph
from . import feature_settings
from . import learning_commit_store
from . import needfix_ingest
from . import repository_state
from . import semantic_edit
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


def workforce_catalog_read() -> dict[str, Any]:
    def call(ctx: worker_tools.WorkerToolContext) -> dict[str, Any]:
        return workforce_catalog.build_routing_catalog(ctx.authority_repo)

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
        snapshot = workforce_catalog.build_routing_catalog(ctx.authority_repo)
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


# ---------------------------------------------------------------------------
# Manager-seat semantic editing.
#
# The worker MCP has had ``semantic_edit_prepare``/``_apply`` from the start;
# the manager MCP had no semantic-edit tool at all, so the one seat whose
# charter is "small precise corrections" made them by whole-string rewrite and
# left no ``semantic_edit_apply_receipt`` in any authenticated ledger.  The
# coverage measurement that counts changed paths against apply receipts was
# therefore structurally blind to every edit the manager made: not "0%", but
# unmeasurable.
#
# Nothing here is a second implementation.  ``semantic_edit`` holds the
# hash-bound prepare, ``semantic_edit_applier`` holds the whole-file-preimage
# applier, and ``worker_ai_tools_mcp.WorkerSemanticEditSession`` holds the
# session, idempotency, receipt and ledger contract.  The manager seat gets a
# ``WorkerToolContext`` built from its VERIFIED route with an audit ledger
# bound, and drives that same session.  One definition, two seats, identical
# guarantees.
# ---------------------------------------------------------------------------

MANAGER_SEMANTIC_EDIT_TOPIC = "semantic_edit"

# The manager seat's scope answer.  A worker writes into a sandboxed worktree
# under a card's ``allowed_writes``; the manager writes into the CANONICAL tree
# and has no card, so a copy of the worker's rule would authorize nothing.
#
# The boundary implemented here is: every existing UTF-8 file in the verified
# repository working tree, EXCEPT AIWorkHub's own ``.aiworkhub`` state.  Three
# of the four refusals are already enforced by the shared primitives and are
# inherited rather than restated -- ``normalize_relative_path`` refuses an
# absolute path, a ``..`` escape and ``.git``; ``resolve_existing_file``
# refuses a symlink at any path component and any target that does not resolve
# under the root; ``read_utf8_file`` refuses a non-UTF-8 or oversized file.
#
# The one refusal this seat adds is ``.aiworkhub/**``.  That subtree is not
# source the manager corrects: it is the storage registry, the canonical
# databases, the credential and worktree runtime state, and the audit ledgers
# -- including THIS surface's own ledger -- that the manager's review reads as
# evidence.  A line-range edit there is never a correction; it is a rewrite of
# the evidence a decision is measured against.  It also makes
# "never edit a candidate worktree in place" mechanical, because candidate
# worktrees live under ``.aiworkhub/runtime/worktrees``.
#
# No narrower rule than that is implemented, deliberately.  The manager already
# writes to the canonical tree through ``scripts/manager_semantic_edit.py`` and
# through ordinary file tools, both of which accept any path in the repository;
# a narrower rule here would not remove that authority, it would only push the
# seat back to the unrecorded raw edit this surface exists to replace.  So this
# is strictly NARROWER than what the seat can already do, and widens nothing.
MANAGER_SEMANTIC_EDIT_SCOPE = (
    "canonical_repository_working_tree_excluding_aiworkhub_state"
)

# The three exceptions the repository's semantic-edit rule allows, taken from
# the finalizer's own tuple rather than restated here: the manager's declared
# code and the coverage record's vocabulary must be the same list or a manager
# declaration would read back as ``unknown_exception_code``.
_MANAGER_EDIT_SESSIONS: dict[
    tuple[str, str, str], worker_tools.WorkerSemanticEditSession
] = {}
_MANAGER_EDIT_LOCK = threading.Lock()
_MAX_MANAGER_EDIT_SESSIONS = 16
_MANAGER_SEMANTIC_EDIT_RUNTIME_REL = (
    Path(repository_state.HUB_DIRNAME) / "runtime" / "manager_semantic_edit"
)


def semantic_edit_policy_exceptions() -> tuple[str, ...]:
    """The closed vocabulary a fallback record may name.

    Imported lazily from ``process_launcher`` so this module keeps its light
    import graph, and read from there rather than copied so the declaring side
    and the reading side can never drift apart.  If that module cannot be
    imported the vocabulary is reported as empty, which makes every declared
    code read as unknown -- unmeasured with a named reason, never silently
    accepted.
    """

    try:
        from . import process_launcher
    except Exception:  # pragma: no cover - import-time environment failure
        return ()
    return tuple(
        str(code) for code in getattr(
            process_launcher, "SEMANTIC_EDIT_POLICY_EXCEPTIONS", ()
        )
    )


def _manager_edit_scope_refusal(relative: str) -> str:
    """``""`` when ``relative`` is inside the manager seat's scope."""

    parts = PurePosixPath(relative).parts
    if parts and parts[0] == repository_state.HUB_DIRNAME:
        return f"manager_semantic_edit_path_is_aiworkhub_state:{relative}"
    return ""


def _provision_manager_edit_ledger(root: Path) -> tuple[Path, Path]:
    """This repository's manager-seat HMAC ledger and key (0700 dir, 0600 files).

    Same shape and same appender as the per-request worker ledger, so
    ``verify_audit_ledger`` reads a manager apply receipt with no special case;
    the ledger row's ``runner`` already ends in ``_manager``, which the ledger
    stage inference has always understood.
    """

    runtime_dir = (root / _MANAGER_SEMANTIC_EDIT_RUNTIME_REL).resolve()
    key_path = runtime_dir / "audit_hmac.key"
    ledger_path = runtime_dir / "audit_ledger.jsonl"
    if not key_path.exists() or key_path.stat().st_size < 32:
        worker_tools._touch_0600(key_path)
        os.truncate(key_path, 0)
        with open(key_path, "wb") as handle:
            handle.write(secrets.token_bytes(32))
        os.chmod(key_path, 0o600)
    worker_tools._touch_0600(ledger_path)
    return ledger_path, key_path


def _manager_edit_session() -> tuple[
    worker_tools.WorkerSemanticEditSession | None, dict[str, Any]
]:
    """The verified manager seat's semantic-edit session, or a refusal dict.

    The session is cached per ``(provider, session_id, repo)`` because a
    prepared ``target_id`` must survive until the matching apply, and every
    component of that key comes from the verified route -- never from a caller
    argument.
    """

    context, manager = _manager_context(topic=MANAGER_SEMANTIC_EDIT_TOPIC)
    if context is None:
        return None, manager
    try:
        ledger_path, key_path = _provision_manager_edit_ledger(
            context.authority_repo
        )
    except OSError as exc:
        return None, {
            "ok": False,
            "error": f"manager_semantic_edit_ledger_unavailable:{type(exc).__name__}",
            "surface": "manager_mcp",
            "manager": manager,
        }
    key = (manager["provider"], manager["session_id"], manager["repo"])
    with _MANAGER_EDIT_LOCK:
        session = _MANAGER_EDIT_SESSIONS.get(key)
        if session is None:
            bound = dataclasses.replace(
                context,
                # ``path_is_allowed`` is a pure allowlist and an empty one
                # authorizes nothing, so the seat's scope is expressed as
                # "every repository-relative path" here and the one refusal
                # this seat adds is applied by ``prepare`` below, before any
                # target is ever minted.  ``apply`` can only act on a target
                # ``prepare`` minted, so no unchecked path can reach it.
                allowed_writes=("*",),
                audit_ledger_path=ledger_path,
                audit_hmac_key_path=key_path,
            )
            session = worker_tools.WorkerSemanticEditSession(bound)
            _MANAGER_EDIT_SESSIONS[key] = session
            while len(_MANAGER_EDIT_SESSIONS) > _MAX_MANAGER_EDIT_SESSIONS:
                _MANAGER_EDIT_SESSIONS.pop(next(iter(_MANAGER_EDIT_SESSIONS)))
    return session, manager


def _manager_edit_actor(manager: dict[str, Any]) -> dict[str, str]:
    """The receipt's actor, derived from the verified route and nothing else.

    Identical in shape and in origin to ``_write_invoke``'s actor: every field
    comes from ``core.manager_bootstrap()``'s verified identity, and no tool
    argument can reach it.
    """

    return {
        "role": "manager",
        "actor_id": str(manager["session_id"]),
        "provider": str(manager["provider"]),
        "session_id": str(manager["session_id"]),
        "repo": str(manager["repo"]),
    }


def _manager_edit_result(
    result: dict[str, Any], manager: dict[str, Any]
) -> dict[str, Any]:
    payload = dict(result)
    payload["manager"] = manager
    payload["surface"] = "manager_mcp"
    payload["actor"] = _manager_edit_actor(manager)
    payload["scope"] = MANAGER_SEMANTIC_EDIT_SCOPE
    payload["token_savings_claimed"] = False
    return payload


def semantic_edit_prepare(
    *, file_path: str, start_line: int, end_line: int,
    include_fragment: bool = False,
) -> dict[str, Any]:
    """MANAGER: bind one line range's whole-file and fragment sha256."""

    session, manager = _manager_edit_session()
    if session is None:
        return manager
    tool = "semantic_edit_prepare"
    try:
        relative = semantic_edit.normalize_relative_path(file_path)
    except semantic_edit.SemanticEditError as exc:
        return _manager_edit_result(
            {"ok": False, "tool": tool, "reason": str(exc)[:240]}, manager
        )
    refusal = _manager_edit_scope_refusal(relative)
    if refusal:
        return _manager_edit_result(
            {"ok": False, "tool": tool, "reason": refusal}, manager
        )
    return _manager_edit_result(
        session.prepare(
            file_path=relative,
            start_line=start_line,
            end_line=end_line,
            include_fragment=include_fragment,
        ),
        manager,
    )


def semantic_edit_apply(
    *, target_id: str, new: str, idempotency_key: str
) -> dict[str, Any]:
    """MANAGER: replace exactly the prepared range, refusing a moved file."""

    session, manager = _manager_edit_session()
    if session is None:
        return manager
    if not core.writes_allowed():
        # The same gate every other manager write answers to.  It is a
        # narrowing, not a widening: the seat can already edit this tree by
        # other means with no gate at all.
        return {
            "ok": False,
            "error": "write_gate_closed",
            "tool": "semantic_edit_apply",
            "surface": "manager_mcp",
            "manager": manager,
        }
    return _manager_edit_result(
        session.apply(
            target_id=target_id, new=new, idempotency_key=idempotency_key,
        ),
        manager,
    )


def _manager_edit_file_lines(root: Path, relative: str) -> int | None:
    """Line count of ``relative``, or ``None`` when it cannot be measured."""

    try:
        target = semantic_edit.resolve_existing_file(root, relative)
        _data, text = semantic_edit.read_utf8_file(target, relative)
    except (OSError, semantic_edit.SemanticEditError):
        return None
    return len(text.splitlines())


def semantic_edit_fallback_record(
    *, path: str, reason: str, exception: str = "",
    start_line: int = 0, end_line: int = 0,
) -> dict[str, Any]:
    """MANAGER RECORD: "I did not use semantic edit here, for this reason".

    A RECORD, never a gate.  It refuses no edit, it is written after the fact,
    and nothing in acceptance consults it -- its only consumer is the coverage
    evidence, where a raw edit with no record shows up as exactly that.  So it
    returns ``ok: True`` even for an out-of-vocabulary code or an unusable
    path: a record that can be refused is a record that hides edits.

    What can be derived is derived, from this seat's own evidence, and only the
    genuinely judgemental case needs the caller's word:

    * ``new_file`` -- derived when the path is absent from the canonical tree.
    * ``adapter_without_tools`` -- derived when this seat's own semantic-edit
      surface is unusable (no verified route, or no ledger).
    * ``spans_most_of_file`` -- derived when a supplied ``start_line``/
      ``end_line`` span covers a strict majority of the file's lines.  "Most"
      is taken at its literal meaning; the threshold is the definition of the
      word, not a tuned constant, and it is reported with the counts it was
      computed from.

    A declared code is recorded either way and marked ``corroborated`` or not,
    with the basis named.  Absence of corroboration reads as unmeasured with a
    reason, never as a refutation.
    """

    session, manager = _manager_edit_session()
    tool = "semantic_edit_exception_declare"
    vocabulary = semantic_edit_policy_exceptions()
    declared = str(exception or "").strip()[:64]
    reason_text = str(reason or "").strip()[:200]
    record: dict[str, Any] = {
        "ok": True,
        "tool": tool,
        "schema_id": "aiworkhub.manager_semantic_edit_fallback_record.v1",
        "is_gate": False,
        "allowed_exceptions": list(vocabulary),
        "token_savings_claimed": False,
    }
    if session is None:
        # No verified route or no ledger: the surface itself is unavailable, so
        # the record has nowhere authenticated to go.  Say so, and derive the
        # exception that the unavailability itself justifies.
        record.update({
            "recorded": False,
            "unrecorded_reason": str(manager.get("error") or "manager_route_unverified")[:120],
            "path": str(path or "")[:512],
            "reason": reason_text,
            "exception": declared or "adapter_without_tools",
            "derived": [{
                "path": str(path or "")[:512],
                "exception": "adapter_without_tools",
                "basis": "manager_semantic_edit_surface_unavailable",
                "source": "runtime_derivation",
            }],
            "declared": [],
            "manager": manager,
            "surface": "manager_mcp",
        })
        return record

    root = session.ctx.authority_repo
    try:
        relative = semantic_edit.normalize_relative_path(path)
    except semantic_edit.SemanticEditError as exc:
        record.update({
            "recorded": False,
            "unrecorded_reason": str(exc)[:120],
            "path": str(path or "")[:512],
            "reason": reason_text,
            "exception": declared,
            "derived": [],
            "declared": [],
        })
        return _manager_edit_result(record, manager)

    file_lines = _manager_edit_file_lines(root, relative)
    path_exists = file_lines is not None
    span_lines = 0
    if isinstance(start_line, int) and isinstance(end_line, int):
        if not isinstance(start_line, bool) and not isinstance(end_line, bool):
            if start_line >= 1 and end_line >= start_line:
                span_lines = end_line - start_line + 1

    derived: list[dict[str, Any]] = []
    if not path_exists:
        derived.append({
            "path": relative,
            "exception": "new_file",
            "basis": "path_absent_from_canonical_tree",
            "source": "runtime_derivation",
        })
    elif span_lines and file_lines and span_lines * 2 > file_lines:
        derived.append({
            "path": relative,
            "exception": "spans_most_of_file",
            "basis": f"span_covers_a_strict_majority_of_file_lines:{span_lines}/{file_lines}",
            "source": "runtime_derivation",
        })

    declared_rows: list[dict[str, Any]] = []
    if declared:
        if declared not in vocabulary:
            corroborated, corroboration = False, "unknown_exception_code"
        elif declared == "new_file":
            corroborated = not path_exists
            corroboration = "path_presence_in_canonical_tree"
        elif declared == "adapter_without_tools":
            # The seat answered, so the surface is usable and the code is not
            # corroborated -- stated, not silently dropped.
            corroborated = False
            corroboration = "manager_semantic_edit_surface_is_available"
        elif not span_lines or not file_lines:
            corroborated = False
            corroboration = "no_span_supplied_for_a_span_claim"
        else:
            corroborated = span_lines * 2 > file_lines
            corroboration = f"span_lines_vs_file_lines:{span_lines}/{file_lines}"
        declared_rows.append({
            "path": relative,
            "exception": declared,
            "reason": reason_text,
            "source": "manager_declaration",
            "corroborated": bool(corroborated),
            "corroboration": corroboration,
        })

    effective = declared or (derived[0]["exception"] if derived else "")
    record.update({
        "path": relative,
        "reason": reason_text,
        "exception": effective,
        "declared_exception_valid": bool(declared) and declared in vocabulary,
        "path_exists": path_exists,
        "file_lines": file_lines,
        "span_lines": span_lines,
        "derived": derived,
        "declared": declared_rows,
    })
    # The ledger payload is the shape ``verify_audit_ledger`` already reads for
    # a worker declaration: path, exception, reason.  Nothing else crosses.
    recorded = worker_tools._append_audit(
        session.ctx,
        tool=tool,
        ok=True,
        cache_hit=False,
        hit_count=1,
        bytes_returned=0,
        authority_source="canonical",
        authority_state="manager_declared_exception",
        payload={
            "path": relative,
            "exception": effective,
            "reason": reason_text,
        },
    )
    record["recorded"] = bool(recorded)
    if not recorded:
        record["unrecorded_reason"] = "manager_audit_ledger_append_failed"
    return _manager_edit_result(record, manager)


__all__ = [
    "MANAGER_SEMANTIC_EDIT_SCOPE",
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
    "semantic_edit_apply",
    "semantic_edit_fallback_record",
    "semantic_edit_policy_exceptions",
    "semantic_edit_prepare",
    "session_current_state",
    "session_write",
    "source_graph_query",
]
