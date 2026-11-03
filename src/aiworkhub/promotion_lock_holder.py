"""Promotion-lock holder record and the typed ``promotion_busy`` refusal payload.

NF-2026-02006: a manager whose ``accept_review`` timed out on
``ProcessManager._promotion_lock`` got only ``windows_advisory_lock_timeout
after 20s`` and could not tell a peer accept that is still validating from a
stale lock. The holder writes a small record beside the lock
(``.promotion.lock.holder.json``, the shared :func:`lock_holder_sidecar_path`
spelling) while it holds the lock; a waiter that times out reads it back and
names the holder instead of the errno.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

from .platform_io import lock_holder_sidecar_path, process_is_alive
from .worker_workspace import write_json_0600

PROMOTION_BUSY = "promotion_busy"
# ponytail: a fixed polite re-poll hint, not a measured ETA -- the holder is
# usually mid combined-tree validation, which runs for minutes, and the lock
# wait bound itself stays ADVISORY_LOCK_MAX_WAIT_SECONDS.
RETRY_AFTER_SECONDS = 30
# A holder record is a handful of short fields; anything larger is not ours.
_MAX_HOLDER_BYTES = 64 * 1024
_HOLDER_FIELDS = ("pid", "task_id", "request_id", "operation", "acquired_at")


def write_promotion_holder(
    lock_path: str | os.PathLike[str],
    *,
    task_id: str,
    request_id: str,
    operation: str,
    acquired_at: str,
) -> None:
    """Record who holds the promotion lock; call only while holding it.

    Diagnostic only: a write failure degrades a later refusal to
    ``holder=None`` and never fails the acquisition it describes.
    """

    record = {
        "pid": os.getpid(),
        "task_id": str(task_id or ""),
        "request_id": str(request_id or ""),
        "operation": str(operation or ""),
        "acquired_at": str(acquired_at),
    }
    try:
        write_json_0600(lock_holder_sidecar_path(lock_path), record)
    except OSError:
        return


def read_promotion_holder(lock_path: str | os.PathLike[str]) -> dict[str, Any] | None:
    """Return the holder record, or ``None`` when missing/unreadable/malformed."""

    try:
        with open(lock_holder_sidecar_path(lock_path), "rb") as handle:
            raw = handle.read(_MAX_HOLDER_BYTES + 1)
        if len(raw) > _MAX_HOLDER_BYTES:
            return None
        record = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    # Echo only the scalar fields write_promotion_holder records, never the raw file.
    return {
        key: record[key]
        for key in _HOLDER_FIELDS
        if isinstance(record.get(key), (str, int)) and not isinstance(record.get(key), bool)
    }


def _holder_stale(record: dict[str, Any]) -> bool | None:
    try:
        pid = int(record["pid"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    # A pid outside pid_t range makes the liveness probe raise
    # (OverflowError / ctypes.ArgumentError) instead of answering.
    if not 0 < pid < 2**31:
        return None
    return not process_is_alive(pid)


def _held_for_s(record: dict[str, Any], now: datetime) -> float | None:
    try:
        acquired = datetime.fromisoformat(str(record["acquired_at"]))
        return max(0.0, round((now - acquired).total_seconds(), 3))
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def promotion_busy_payload(
    lock_path: str | os.PathLike[str], *, now: datetime | None = None
) -> dict[str, Any]:
    """Build the typed refusal for a promotion-lock wait that timed out.

    ``holder_stale`` is ``True`` only when the recorded pid is proven dead by
    the runtime's single liveness answer; a missing record or pid is unknown
    (``None``), never "not stale".
    """

    holder = read_promotion_holder(lock_path)
    current = now or datetime.now(timezone.utc)
    return {
        "error": PROMOTION_BUSY,
        "holder": holder,
        "holder_stale": None if holder is None else _holder_stale(holder),
        "held_for_s": None if holder is None else _held_for_s(holder, current),
        "retry_after_s": RETRY_AFTER_SECONDS,
    }
