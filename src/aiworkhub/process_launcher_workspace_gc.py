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

import os
import pathlib
from typing import Any

from . import process_event_ledger

__all__ = ["GC_FINALIZED_WORKSPACE_SEAM_NAMES", "GcSweepSnapshot", "gc_finalized_workspace"]

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
    "split_lifecycle_tail",
    "superseded_reason",
    "task_engine",
    "terminal_failure_classification",
)


class GcSweepSnapshot:
    """Which requests gained a ledger row since one sweep read its snapshot.

    NF-2026-01730: the sweep reads the full ``_latest_by_request`` projection
    once, and each candidate's under-lock re-check asks this object instead of
    re-reading it.  It folds only the bytes appended to the active ledger and
    the spill segments published since the snapshot's base generation, so a
    re-check never replays the ledger -- which, with a spill segment present
    and other writers appending, cost one full multi-segment reparse per
    candidate (1321.7 s for one sweep).

    The base generation is taken immediately BEFORE the snapshot read, so a
    row landing during that read is folded here as well: at worst a candidate
    is reported changed when it was not, and skipped until the next pass.  A
    change that is not pure growth -- a rotation, a replaced or truncated
    active file, an altered immutable segment -- cannot be described from
    appended bytes, so the snapshot answers ``None`` from then on.
    """

    def __init__(
        self, process_log_path: pathlib.Path,
        generation: tuple[tuple[str, int, int, int, int], ...],
    ) -> None:
        self._path = pathlib.Path(process_log_path)
        self._active = str(process_log_path)
        self._base = {segment[0]: segment for segment in generation}
        base_active = self._base.get(self._active)
        self._identity = base_active[1:3] if base_active else None
        # Resume at the snapshot's last COMPLETE row: a row half-written at the
        # base is re-read whole here rather than skipped.
        self._offset = process_event_ledger._active_complete_offset(
            self._path, size=base_active[3]
        ) if base_active else 0
        self._spills_read: set[str] = set()
        self._touched: set[str] = set()
        self._valid = True

    def request_changed(
        self, generation: tuple[tuple[str, int, int, int, int], ...] | None,
        request_id: str,
    ) -> bool | None:
        """``True``/``False`` for a row since the snapshot; ``None`` when unprovable."""
        if self._valid and (generation is None or not self._fold(generation)):
            self._valid = False
        return (request_id in self._touched) if self._valid else None

    def _fold(self, generation: tuple[tuple[str, int, int, int, int], ...]) -> bool:
        current = {segment[0]: segment for segment in generation}
        if any(name not in current for name in self._base):
            return False  # a segment vanished: a rotation or a deletion
        for name, segment in current.items():
            if name == self._active:
                if self._identity is not None and segment[1:3] != self._identity:
                    return False
                if segment[3] < self._offset or not self._read(segment, self._offset, True):
                    return False
            elif name in self._base:
                if segment != self._base[name]:
                    return False  # an immutable segment changed
            elif not process_event_ledger._is_spill(self._path, pathlib.Path(name)):
                return False  # a new rotation archive
            elif name not in self._spills_read:
                if not self._read(segment, 0, False):
                    return False
                self._spills_read.add(name)
        return True

    def _read(self, segment: tuple[str, int, int, int, int], start: int, active: bool) -> bool:
        size = segment[3]
        if size <= start:
            return True
        try:
            with open(segment[0], "rb") as handle:
                opened = os.fstat(handle.fileno())
                if (int(opened.st_dev), int(opened.st_ino)) != tuple(segment[1:3]):
                    return False
                handle.seek(start)
                payload = handle.read(size - start)
        except OSError:
            return False
        # A spill is one atomically published row; an active tail folds only
        # complete rows and resumes at the first incomplete one next time.
        complete = payload.rfind(b"\n") + 1 if active else len(payload)
        body = payload[:complete] if active or payload.endswith(b"\n") else payload + b"\n"
        for row in process_event_ledger._parse_complete_jsonl(body):
            if isinstance(row.get("request_id"), str):
                self._touched.add(row["request_id"])
        if active:
            self._offset = start + complete
            self._identity = tuple(segment[1:3])
        return True


def gc_finalized_workspace(
    self, request_id: str, event: dict[str, Any], *, snapshot: GcSweepSnapshot | None = None
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
    split_lifecycle_tail = _pl.split_lifecycle_tail
    superseded_reason = _pl.superseded_reason
    task_engine = _pl.task_engine
    terminal_failure_classification = _pl.terminal_failure_classification

    if event.get("state") not in GC_CANDIDATE_PROCESS_STATES:
        return None
    if not event.get("workspace_retained") or event.get("workspace_quarantined"):
        return None
    with self._lock:
        # Liveness is judged per request from that request's own row, so this
        # row alone answers for it -- not a full projection read plus a pid
        # probe of every request in the ledger, once per candidate.
        if request_id in self._active_request_ids({request_id: event}):
            return None
    with self._request_lock(request_id):
        # Re-check ONLY this request under its lock (NF-2026-01730). A sweep's
        # snapshot answers from the bytes appended since it was read: a full
        # projection re-read per candidate made one sweep 1321.7 s, and a
        # per-request replay per candidate 38 min. A direct call without a
        # snapshot reads this one request's own history instead.
        if snapshot is None:
            changed, latest = False, split_lifecycle_tail(self._request_events(request_id))[0]
        else:
            changed, latest = snapshot.request_changed(self._ledger_generation(), request_id), event
            if changed is None:
                # Rotated or rewritten since the snapshot: the next pass decides
                # from a fresh one rather than this pass replaying per candidate.
                return {"request_id": request_id, "gc": False, "reason": "ledger_snapshot_unverifiable"}
        if (
            latest.get("state") not in GC_CANDIDATE_PROCESS_STATES
            or not latest.get("workspace_retained") or latest.get("workspace_quarantined")
        ):
            return None
        versions = (event.get("retention_rule_version"), latest.get("retention_rule_version"))
        if refused := superseded_reason(WORKTREE_RETENTION_RULE_VERSION, *versions):
            return {"request_id": request_id, "gc": False, "reason": refused}
        if changed or any(latest.get(key) != value for key, value in event.items()):
            # The caller's snapshot row is no longer this request's latest: never
            # act on a decision drawn from evidence a newer row has replaced.
            return {"request_id": request_id, "gc": False, "reason": "latest_event_changed_since_snapshot"}

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
