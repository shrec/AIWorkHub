"""Bounded PermissionError retry and live-candidate guard for workspace cleanup.

NF-2026-02250 (external Mego report, AIWorkHub 0.12.97): a Windows file lock
made ``worker_workspace.cleanup_workspace`` raise
``workspace_cleanup_remove_failed:<name>:PermissionError``.  Raised from inside
finalization (the full-snapshot validation tears its scratch workspace down
through the same function) that one transient lock turned the card into
``finalize_failed``, and the janitor -- which reaches the very same function --
then deleted the reviewed candidate worktree.

This module owns the policies ``cleanup_workspace`` applies once for every
caller (finalize purge, accept, validation scratch, janitor) through
:func:`remove_request_workspace`:

* :func:`live_candidate_hold` -- a worktree named by any card that is not
  terminal is never deleted, and an AIWorkHub task store that cannot prove
  otherwise (not ready, unreadable) holds it too (fail closed).
* :func:`remove_with_permission_retry` retries a lock-class removal failure
  with a short, bounded backoff and, when a sharing/lock violation outlives
  it, returns a structured *cleanup residue* (exception type, errno/winerror
  and the locking path) instead of raising, logged as a ``cleanup_residue``
  event.  A genuine access denial (Windows ``winerror`` 5) and every other
  failure still raise.
"""

from __future__ import annotations

import errno
import json
import logging
import sqlite3
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable

CLEANUP_RESIDUE_SCHEMA_ID = "aiworkhub.workspace_cleanup_residue.v1"
CLEANUP_RESIDUE_EVENT = "cleanup_residue"
# Delays between attempts; ``len + 1`` attempts in total, < 1 s of waiting.
PERMISSION_RETRY_DELAYS_SECONDS: tuple[float, ...] = (0.05, 0.1, 0.2, 0.4)
# Bounded in-process record of residues left behind (newest last).
RESIDUE_HISTORY_LIMIT = 64
# Windows ERROR_ACCESS_DENIED (5) is retried -- a scanner may hold the file for
# a moment -- but never downgraded: once the retries are spent it propagates.
_RETRY_WINERRORS = frozenset({5, 32, 33})
# Windows ERROR_SHARING_VIOLATION / ERROR_LOCK_VIOLATION: a held file, a residue.
_LOCK_WINERRORS = frozenset({32, 33})
_LOCK_ERRNOS = frozenset({errno.EACCES, errno.EPERM, errno.EBUSY})

_LOG = logging.getLogger(__name__)
_RESIDUES: deque[dict[str, Any]] = deque(maxlen=RESIDUE_HISTORY_LIMIT)
_RESIDUES_LOCK = threading.Lock()
_sleep: Callable[[float], None] = time.sleep


def _os_error_in_chain(exc: BaseException) -> OSError | None:
    """The first ``OSError`` in ``exc``'s cause/context chain (bounded walk)."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen and len(seen) < 16:
        seen.add(id(current))
        if isinstance(current, OSError):
            return current
        current = current.__cause__ or current.__context__
    return None


def is_lock_class_error(exc: BaseException) -> bool:
    """True when a removal may have failed because something holds the file.

    This is the *retry* predicate.  On Windows the ``winerror`` decides
    (sharing/lock violation or access denied); elsewhere a ``PermissionError``
    or ``EACCES``/``EPERM``/``EBUSY``.
    """
    cause = _os_error_in_chain(exc)
    if cause is None:
        return False
    winerror = getattr(cause, "winerror", None)
    if winerror is not None:
        return winerror in _RETRY_WINERRORS
    return isinstance(cause, PermissionError) or cause.errno in _LOCK_ERRNOS


def is_lock_residue(exc: BaseException) -> bool:
    """True when a failure that outlived every retry is a held file.

    Only a held file may become a non-fatal residue: a Windows failure must be
    a sharing (32) or lock (33) violation; a genuine access denial (5) is not.
    """
    cause = _os_error_in_chain(exc)
    winerror = getattr(cause, "winerror", None)
    if winerror is not None:
        return winerror in _LOCK_WINERRORS
    return is_lock_class_error(exc)


def cleanup_residue(
    exc: BaseException, *, root: Path, stage: str, attempts: int
) -> dict[str, Any]:
    """The structured, non-fatal record of a removal a lock outlived."""
    cause = _os_error_in_chain(exc) or exc
    locking = getattr(cause, "filename", None) or getattr(cause, "filename2", None)
    return {
        "schema_id": CLEANUP_RESIDUE_SCHEMA_ID,
        "event": CLEANUP_RESIDUE_EVENT,
        "stage": stage,
        "root": str(root),
        "path": str(locking) if locking else str(root),
        "exception_type": type(cause).__name__,
        "errno": getattr(cause, "errno", None),
        "winerror": getattr(cause, "winerror", None),
        "attempts": attempts,
        "error": str(exc)[:500],
    }


def _record(residue: dict[str, Any]) -> None:
    with _RESIDUES_LOCK:
        _RESIDUES.append(dict(residue))
    _LOG.warning(
        "%s %s",
        CLEANUP_RESIDUE_EVENT,
        json.dumps(residue, sort_keys=True, default=str),
        extra={"event": CLEANUP_RESIDUE_EVENT, "cleanup_residue": dict(residue)},
    )


def recent_residues() -> list[dict[str, Any]]:
    """Copies of the most recent residues, oldest first (bounded)."""
    with _RESIDUES_LOCK:
        return [dict(item) for item in _RESIDUES]


def remove_with_permission_retry(
    remove: Callable[[], None],
    *,
    root: Path,
    stage: str,
    retryable: Callable[[BaseException], bool] = is_lock_class_error,
    delays: tuple[float, ...] = PERMISSION_RETRY_DELAYS_SECONDS,
) -> dict[str, Any] | None:
    """Run ``remove``; retry a lock-class failure, then report it as a residue.

    Returns ``None`` once ``remove`` succeeds and the residue record when a
    held-file failure (:func:`is_lock_residue`) survives every bounded retry.
    A non-retryable failure is re-raised unchanged on the attempt it happens,
    and a retryable one that is not a held file (Windows access denied) is
    re-raised once the retries are spent, so an unknown error is never
    downgraded to a non-fatal residue.
    """
    attempts = 0
    while True:
        attempts += 1
        try:
            remove()
            return None
        except Exception as exc:  # noqa: BLE001 - classified, else re-raised
            if not retryable(exc):
                raise
            if attempts > len(delays):
                if not is_lock_residue(exc):
                    raise
                residue = cleanup_residue(exc, root=root, stage=stage, attempts=attempts)
                _record(residue)
                return residue
            _sleep(delays[attempts - 1])


def live_candidate_hold(repo: Path, request_id: str) -> str:
    """Why ``request_id``'s worktree must be kept, or ``""`` when it may go.

    Held unless terminal: any card whose stored JSON names this request and
    whose canonical status is not terminal -- ``task_store``'s archived /
    superseded / finished set -- holds it, so processing, finalizing,
    review_ready, blocked and pending-with-retained-candidate cards all do.
    Only a directory with no task store at all (no AIWorkHub manifest at
    exactly ``repo``) has no card that could name it.  Once the manifest is
    there the store must prove the worktree free: a store that is not ready
    (missing, corrupt, wrong authority) or cannot be read fails closed.
    """
    from .repository_state import PROJECT_MANIFEST_REL

    request_id = str(request_id or "").strip()
    if not request_id or not (Path(repo) / PROJECT_MANIFEST_REL).is_file():
        return ""
    from . import task_store  # local import: cycle-safe

    try:
        readiness = task_store.storage_readiness(repo)
        if not readiness.ready:
            return f"candidate_hold_unverified:{readiness.reason}"[:300]
        conn = task_store.connect_readonly(Path(readiness.canonical_db), timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT task_id, status, worker_status, archived_at "
                "FROM tasks WHERE instr(card_json, ?) > 0",
                (request_id,),
            ).fetchall()
        finally:
            conn.close()
    except (task_store.TaskStoreError, sqlite3.Error, OSError, ValueError) as exc:
        return f"candidate_hold_unverified:{type(exc).__name__}"

    for row in rows:
        status = task_store.canonical_status(dict(row))
        if status not in task_store._ARCHIVE_TERMINAL_STATUSES:
            return f"non_terminal_candidate:{status}:{row['task_id']}"
    return ""


def remove_request_workspace(
    repo: Path,
    path: Path,
    home: Path,
    *,
    admin_dir: Callable[[], Path | None],
    remove: Callable[[Path], None],
    error: Callable[[str], Exception],
) -> dict[str, Any] | None:
    """Hold-check, then remove one validated request workspace and its registration.

    ``path``/``home`` are the already shape-checked ``<root>/worktree`` and
    ``<root>/home``.  A live candidate raises
    ``workspace_cleanup_live_candidate_retained`` before anything is touched.
    Returns ``None`` for a clean removal and the residue record when a held
    file outlived the bounded retries; after a tree residue the Git
    registration is kept so a later sweep can still resolve the tree.
    """
    hold = live_candidate_hold(repo, path.parent.name)
    if hold:
        raise error(f"workspace_cleanup_live_candidate_retained:{hold}"[:500])
    registration = admin_dir()
    tree = path.parent
    residue = remove_with_permission_retry(
        lambda: remove(tree), root=tree, stage="workspace_tree"
    )
    if residue is not None:
        return residue
    if path.exists() or home.exists() or tree.exists():
        raise error("workspace_cleanup_directory_retained")
    if registration is None:
        return None
    admin: Path = registration
    residue = remove_with_permission_retry(
        lambda: remove(admin), root=admin, stage="worktree_registration"
    )
    if residue is None and admin.exists():
        raise error("workspace_cleanup_registration_retained")
    return residue
