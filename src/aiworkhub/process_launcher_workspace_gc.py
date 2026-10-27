"""Finalized-workspace GC for one request, extracted from ``ProcessManager``.

``ProcessManager._gc_finalized_workspace`` is a thin delegation to
:func:`gc_finalized_workspace` below (NF-2026-01729: ``process_launcher.py``
sat exactly on the module size ratchet).  The move is byte-for-byte apart
from the four-space dedent a method-to-function move forces; ``self`` is still the
:class:`ProcessManager` instance, so every ``self.`` collaborator resolves
exactly as before.  Module level collaborators are re-bound from the
``process_launcher`` module object on entry rather than imported here, because
the test suite monkeypatches that module and a name captured at import time
would leave those patches pointing at code this function no longer calls --
silently, with the tests still green.
"""

from __future__ import annotations

from typing import Any

__all__ = ["GC_FINALIZED_WORKSPACE_SEAM_NAMES", "gc_finalized_workspace"]

# The function's complete runtime free-variable set on ``process_launcher``.
# ``Any`` appears only in annotations, which are never evaluated, so it is
# resolved from this module's own import and is deliberately NOT re-bound.
GC_FINALIZED_WORKSPACE_SEAM_NAMES: tuple[str, ...] = (
    "GC_CANDIDATE_PROCESS_STATES",
    "Path",
    "WORKTREE_RETENTION_RULE_VERSION",
    "WorkerWorkspace",
    "WorkspaceError",
    "_canonical_task_status",
    "_parse_card",
    "_process_proven_dead",
    "_utcnow",
    "assert_gc_safe_workspace_shape",
    "cleanup_workspace",
    "gc_husk_without_metadata",
    "json",
    "quarantine_review_workspace",
    "record_review_workspace_retention_audit",
    "superseded_reason",
    "task_engine",
    "terminal_failure_classification",
)


def gc_finalized_workspace(
    self, request_id: str, event: dict[str, Any]
) -> dict[str, Any] | None:
    """Delete only a finalized task's retained, proven-dead workspace."""
    # Every module-level name this function used while it lived in
    # ``process_launcher`` is re-bound here from that exact module object, so a
    # ``monkeypatch.setattr(process_launcher, name, ...)`` seam still reaches the
    # code below.  The list is ``GC_FINALIZED_WORKSPACE_SEAM_NAMES`` and is
    # asserted against the body by
    # ``test_process_launcher_extraction_seams_nf01729.py``.
    from . import process_launcher as _pl

    GC_CANDIDATE_PROCESS_STATES = _pl.GC_CANDIDATE_PROCESS_STATES
    Path = _pl.Path
    WORKTREE_RETENTION_RULE_VERSION = _pl.WORKTREE_RETENTION_RULE_VERSION
    WorkerWorkspace = _pl.WorkerWorkspace
    WorkspaceError = _pl.WorkspaceError
    _canonical_task_status = _pl._canonical_task_status
    _parse_card = _pl._parse_card
    _process_proven_dead = _pl._process_proven_dead
    _utcnow = _pl._utcnow
    assert_gc_safe_workspace_shape = _pl.assert_gc_safe_workspace_shape
    cleanup_workspace = _pl.cleanup_workspace
    gc_husk_without_metadata = _pl.gc_husk_without_metadata
    json = _pl.json
    quarantine_review_workspace = _pl.quarantine_review_workspace
    record_review_workspace_retention_audit = _pl.record_review_workspace_retention_audit
    superseded_reason = _pl.superseded_reason
    task_engine = _pl.task_engine
    terminal_failure_classification = _pl.terminal_failure_classification

    if event.get("state") not in GC_CANDIDATE_PROCESS_STATES:
        return None
    if not event.get("workspace_retained") or event.get("workspace_quarantined"):
        return None
    with self._lock:
        if request_id in self._active_request_ids():
            return None
    with self._request_lock(request_id):
        # Re-read under the lock through the SAME projection the prefilter used; a full _request_events replay per request is a 38-min sweep.
        latest = self._latest_by_request().get(request_id)
        if latest is None:
            return None
        if (
            latest.get("state") not in GC_CANDIDATE_PROCESS_STATES
            or not latest.get("workspace_retained") or latest.get("workspace_quarantined")
        ):
            return None
        versions = (event.get("retention_rule_version"), latest.get("retention_rule_version"))
        if refused := superseded_reason(WORKTREE_RETENTION_RULE_VERSION, *versions):
            return {"request_id": request_id, "gc": False, "reason": refused}

        metadata_raw = latest.get("metadata_path")
        if not metadata_raw:
            return gc_husk_without_metadata(self, request_id, latest, "metadata_path_missing")
        metadata_path = Path(str(metadata_raw))
        if not metadata_path.is_file():
            return gc_husk_without_metadata(self, request_id, latest, "metadata_missing")
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            workspace_meta = metadata["workspace"]
            repo = Path(str(workspace_meta["repo"]))
            path = Path(str(workspace_meta["path"]))
            home = Path(str(workspace_meta["home"]))
            meta_request_id = str(workspace_meta["request_id"])
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return {"request_id": request_id, "gc": False, "reason": "metadata_invalid"}
        if meta_request_id != request_id or repo.resolve() != self.repo:
            return {"request_id": request_id, "gc": False, "reason": "workspace_identity_mismatch"}

        task_id = str(latest.get("task_id") or "")
        runner = str(latest.get("runner") or "")
        if not task_id or not runner:
            return {"request_id": request_id, "gc": False, "reason": "task_identity_missing"}
        try:
            card = _parse_card(self._show_task(task_id), task_id)
        except Exception as exc:
            return {
                "request_id": request_id,
                "gc": False,
                "reason": f"task_lookup_failed:{exc}"[:200],
            }
        eligible, disposition = self._gc_disposition(
            card, request_id, repo=self.repo, process_state=str(latest.get("state") or ""))
        if not eligible and disposition == "pinned_rework_predecessor":
            # Seal the pinned bytes into a verified delta; the delta then
            # stands in for the worktree and the ordinary path below
            # releases it. Any failure keeps the worktree.
            sealed_card, disposition = self._seal_pinned_rework_predecessor(
                card, task_id, request_id, latest,
                workspace_meta=workspace_meta, path=path, home=home, repo=repo,
            )
            if sealed_card is None:
                return {"request_id": request_id, "gc": False, "reason": disposition}
            card, eligible = sealed_card, True
        if not eligible:
            if disposition != "current_review_request":
                return {
                    "request_id": request_id,
                    "gc": False,
                    "reason": disposition,
                }
            pid = int(latest.get("pid") or 0)
            ticks = latest.get("pid_start_ticks")
            if not _process_proven_dead(pid, ticks):
                return {
                    "request_id": request_id,
                    "gc": False,
                    "reason": "process_not_proven_dead",
                }
            try:
                workspace = WorkerWorkspace.from_metadata(dict(workspace_meta))
                assert_gc_safe_workspace_shape(
                    meta_request_id, path, home, repo=repo
                )
            except (KeyError, TypeError, ValueError, WorkspaceError) as exc:
                return {
                    "request_id": request_id,
                    "gc": False,
                    "reason": f"unsafe_workspace_shape:{exc}"[:200],
                }
            intact, integrity_reason = self._retained_review_workspace_integrity(
                card, request_id, workspace
            )
            if intact:
                return {
                    "request_id": request_id,
                    "gc": False,
                    "reason": disposition,
                }
            transition = task_engine.mark_review_workspace_missing(
                self.repo,
                task_id,
                runner,
                request_id,
                reason=integrity_reason,
            )
            if not transition.get("ok"):
                return {
                    "request_id": request_id,
                    "gc": False,
                    "reason": (
                        "review_workspace_reconcile_failed:"
                        + str(transition.get("stderr") or "unknown")
                    )[:200],
                }
            prior_state = str(latest.get("state") or "")
            retention_state = "finalize_failed" if prior_state in {"review_ready", "exited", "exited_without_review"} else prior_state
            # The "still in review" guard protects bytes that EXIST; bytes already gone have nothing to preserve, so reclaim the retained record through the ordinary idempotent cleanup path (audited).
            if integrity_reason == "review_workspace_missing":
                try:
                    cleanup_workspace(repo, path, home)
                except WorkspaceError as exc:
                    self._retention_event({
                        "request_id": request_id,
                        "task_id": task_id,
                        "runner": runner,
                        "topic": latest.get("topic"),
                        "adapter_id": latest.get("adapter_id"),
                        "state": retention_state,
                        "error": f"retained_workspace_cleanup_failed:{exc}"[:500],
                        "workspace_gc": False,
                        "workspace_gc_at": _utcnow(),
                        "workspace_gc_reason": integrity_reason,
                        "review_transition_ok": True,
                        "callback_enqueued": bool(
                            transition.get("callback_enqueued")
                        ),
                    }, disposition="retained_in_place")
                    return {
                        "request_id": request_id,
                        "gc": False,
                        "reason": f"retained_workspace_cleanup_failed:{exc}"[:200],
                    }
                record_review_workspace_retention_audit(
                    self.process_log_path,
                    request_id=request_id,
                    task_id=task_id,
                    card_status=_canonical_task_status(card),
                    reason=integrity_reason,
                    action="purge",
                )
                # Bytes already gone: this branch removes (disposition "removed").  GC runs only on a card already in a terminal GC-candidate state, so an adjudicated outcome always exists here -- keep it like the quarantine branch, never mask it.
                self._retention_event({
                    "request_id": request_id,
                    "task_id": task_id,
                    "runner": runner,
                    "topic": latest.get("topic"),
                    "adapter_id": latest.get("adapter_id"),
                    "state": retention_state,
                    "error": f"retained_workspace_missing_reclaimed:{integrity_reason}"[:500],
                    "workspace_gc": True,
                    "workspace_gc_at": _utcnow(),
                    "workspace_gc_reason": integrity_reason,
                    "review_transition_ok": True,
                    "callback_enqueued": bool(
                        transition.get("callback_enqueued")
                    ),
                }, disposition="removed")
                return {
                    "request_id": request_id,
                    "gc": True,
                    "reason": integrity_reason,
                }
            # A failed integrity check on bytes that still EXIST is not authority to destroy work: quarantine them for a manager (audited).
            try:
                quarantine_dir = quarantine_review_workspace(
                    self.process_log_path,
                    request_id=request_id,
                    path=path,
                    home=home,
                )
            except (OSError, WorkspaceError) as exc:
                self._retention_event({
                    "request_id": request_id,
                    "task_id": task_id,
                    "runner": runner,
                    "topic": latest.get("topic"),
                    "adapter_id": latest.get("adapter_id"),
                    "state": "finalize_failed",
                    "workspace_gc": False,
                    "workspace_gc_at": _utcnow(),
                    "workspace_gc_reason": integrity_reason,
                    "review_transition_ok": True,
                    "callback_enqueued": bool(
                        transition.get("callback_enqueued")
                    ),
                    # ``review_workspace_quarantine_failed`` is now a named control-plane reason, so this no longer degrades to ``finalize_failed:runtime_error``; the ``{exc}`` tail stays caller text on the sanitised channel.
                    **terminal_failure_classification.terminal_event_authority(state="finalize_failed", exit_code=None, error=f"review_workspace_quarantine_failed:{exc}"[:500]),
                }, disposition="retained_in_place")
                return {
                    "request_id": request_id,
                    "gc": False,
                    "reason": f"quarantine_failed:{exc}"[:200],
                }
            record_review_workspace_retention_audit(
                self.process_log_path,
                request_id=request_id,
                task_id=task_id,
                card_status=_canonical_task_status(card),
                reason=integrity_reason,
                action="quarantine",
                moved_to=str(quarantine_dir),
            )
            self._retention_event({
                "request_id": request_id,
                "task_id": task_id,
                "runner": runner,
                "topic": latest.get("topic"),
                "adapter_id": latest.get("adapter_id"),
                "state": retention_state,
                "error": f"retained_workspace_quarantined:{integrity_reason}"[:500],
                "workspace_gc": False,
                "workspace_quarantined": True,
                "workspace_quarantine_path": str(quarantine_dir),
                "workspace_gc_at": _utcnow(),
                "workspace_gc_reason": integrity_reason,
                "review_transition_ok": True,
                "callback_enqueued": bool(transition.get("callback_enqueued")),
            }, disposition="quarantined")
            return {
                "request_id": request_id,
                "gc": False,
                "quarantined": True,
                "reason": integrity_reason,
            }

        pid = int(latest.get("pid") or 0)
        ticks = latest.get("pid_start_ticks")
        if not _process_proven_dead(pid, ticks):
            return {"request_id": request_id, "gc": False, "reason": "process_not_proven_dead"}
        try:
            assert_gc_safe_workspace_shape(meta_request_id, path, home, repo=repo)
        except WorkspaceError as exc:
            return {
                "request_id": request_id,
                "gc": False,
                "reason": f"unsafe_workspace_shape:{exc}"[:200],
            }
        try:
            cleanup_workspace(repo, path, home)
        except WorkspaceError as exc:
            self._retention_event({
                "request_id": request_id, "task_id": task_id,
                "runner": runner, "topic": latest.get("topic"),
                "adapter_id": latest.get("adapter_id"), "state": latest.get("state"),
                "error": f"cleanup_failed:{exc}"[:500], "workspace_gc": False,
                "workspace_gc_at": _utcnow(), "workspace_gc_reason": disposition,
            }, disposition="retained_in_place")
            return {
                "request_id": request_id,
                "gc": False,
                "reason": f"cleanup_failed:{exc}"[:200],
            }

        record_review_workspace_retention_audit(
            self.process_log_path,
            request_id=request_id,
            task_id=task_id,
            card_status=_canonical_task_status(card),
            reason=disposition,
            action="purge",
        )
        self._retention_event({
            "request_id": request_id,
            "task_id": task_id,
            "runner": runner,
            "topic": latest.get("topic"),
            "adapter_id": latest.get("adapter_id"),
            "state": latest.get("state"),
            "workspace_gc": True,
            "workspace_gc_at": _utcnow(),
            "workspace_gc_reason": disposition,
        }, disposition="removed")
        return {"request_id": request_id, "gc": True, "reason": disposition}
