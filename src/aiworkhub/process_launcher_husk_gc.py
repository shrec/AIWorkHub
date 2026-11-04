"""Reclaim of metadata-less worktree husks for ``ProcessManager``.

Moved out of ``process_launcher`` unchanged under the module size ratchet.
``process_launcher`` names are resolved lazily at call time (import cycle).
"""

from __future__ import annotations

from typing import Any

from . import worker_workspace as _worker_workspace


DISPOSED_WORKSPACE_RECLAIMED_REASON = "disposed_workspace_reclaimed"


def gc_husk_without_metadata(
    manager: Any, request_id: str, latest: dict[str, Any], missing_reason: str
) -> dict[str, Any]:
    """Reclaim a metadata-less workspace, but only once its card is disposed.

    A workspace whose shape is not exactly this request's keeps
    ``missing_reason``. Otherwise it goes only when the disposition gate calls
    its card finished, archived or superseded and its process is proven dead
    -- never because a restart, timeout or lost metadata orphaned it. A husk is
    audited ``husk_reclaimed``; a populated worktree or home (a Source Graph
    base-pin hard link, worker state) of a disposed card has no remaining use
    and is audited ``disposed_workspace_reclaimed`` (NF-2026-02063). Every keep
    reason of ``_gc_disposition`` still wins.
    """
    from . import process_launcher as pl

    kept = {"request_id": request_id, "gc": False, "reason": missing_reason}
    try:
        root = _worker_workspace.configured_worktree_root(manager.repo)
        path, home = root / request_id / "worktree", root / request_id / "home"
        pl.assert_gc_safe_workspace_shape(request_id, path, home, repo=manager.repo)
    except pl.WorkspaceError:
        return kept
    reason = (
        "husk_reclaimed" if _worker_workspace.is_workspace_husk(path, home)
        else DISPOSED_WORKSPACE_RECLAIMED_REASON
    )
    task_id = str(latest.get("task_id") or "")
    runner = str(latest.get("runner") or "")
    if not task_id or not runner:
        return {"request_id": request_id, "gc": False, "reason": "task_identity_missing"}
    try:
        card = pl._parse_card(manager._show_task(task_id), task_id)
    except Exception as exc:
        return {
            "request_id": request_id,
            "gc": False,
            "reason": f"task_lookup_failed:{exc}"[:200],
        }
    eligible, disposition = manager._gc_disposition(
        card, request_id, repo=manager.repo, process_state=str(latest.get("state") or ""))
    status = pl._canonical_task_status(card)
    if not eligible or status not in pl.GC_DISPOSED_CANONICAL_STATUSES:
        return {
            "request_id": request_id,
            "gc": False,
            "reason": f"task_not_disposed:{status}" if eligible else disposition,
        }
    if not pl._process_proven_dead(int(latest.get("pid") or 0), latest.get("pid_start_ticks")):
        return {"request_id": request_id, "gc": False, "reason": "process_not_proven_dead"}
    event = {
        "request_id": request_id,
        "task_id": task_id,
        "runner": runner,
        "topic": latest.get("topic"),
        "adapter_id": latest.get("adapter_id"),
        "state": latest.get("state"),
    }
    try:
        pl.cleanup_workspace(manager.repo, path, home)
    except pl.WorkspaceError as exc:
        manager._retention_event({
            **event, "error": f"cleanup_failed:{exc}"[:500], "workspace_gc": False,
            "workspace_gc_at": pl._utcnow(), "workspace_gc_reason": disposition,
        }, disposition="retained_in_place")
        return {"request_id": request_id, "gc": False, "reason": f"cleanup_failed:{exc}"[:200]}
    pl.record_review_workspace_retention_audit(
        manager.process_log_path,
        request_id=request_id,
        task_id=task_id,
        card_status=status,
        reason=reason,
        action="purge",
    )
    manager._retention_event({
        **event, "workspace_gc": True, "workspace_gc_at": pl._utcnow(),
        "workspace_gc_reason": reason,
    }, disposition="removed")
    return {"request_id": request_id, "gc": True, "reason": reason}
