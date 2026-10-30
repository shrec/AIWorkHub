"""Launch admission: wait for capacity instead of failing the card.

A launch refused for host memory or the concurrency cap says nothing about the
card.  A deferred reviewer thread therefore waits here, in a process-wide
priority queue (reviewers before workers, first come first served within a
rank), and a direct worker launch stamps its still-pending card so the
transient-retry reconciler relaunches it; the MCP server thread never waits.
"""

from __future__ import annotations

import itertools
import json
import math
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from . import task_store

WAIT_SECONDS_ENV = "AIWORKHUB_LAUNCH_ADMISSION_WAIT_SECONDS"
DEFAULT_WAIT_SECONDS = 1800
DEFAULT_POLL_SECONDS = 5
# Cadence at which a deferred card is offered back to the reconciler; the wait
# itself ends when capacity frees, not after a fixed number of tries.
DEFERRED_RETRY_AFTER_SECONDS = 30

MEMORY_REFUSAL_PREFIX = "memory_launch_capacity_denied:"
CONCURRENCY_REFUSAL = "concurrency_limit_reached"
EXPIRED_REASON = "admission_wait_expired"
OUTRANKED_REASON = "admission_outranked_by_queued_reviewer"

_RANKS = {"reviewer": 0, "worker": 1}

_lock = threading.Lock()
_waiters: list["_Waiter"] = []
_sequence = itertools.count()


class _Waiter:
    __slots__ = ("rank", "sequence")

    def __init__(self, rank: int) -> None:
        self.rank = rank
        self.sequence = next(_sequence)

    def order(self) -> tuple[int, int]:
        return (self.rank, self.sequence)


def configured_wait_seconds() -> float:
    raw = os.environ.get(WAIT_SECONDS_ENV, "").strip()
    if not raw:
        return float(DEFAULT_WAIT_SECONDS)
    try:
        value = float(raw)
    except ValueError:
        return float(DEFAULT_WAIT_SECONDS)
    # inf would park a reviewer thread for good; nan compares false to everything.
    return max(0.0, value) if math.isfinite(value) else float(DEFAULT_WAIT_SECONDS)


def await_memory_admission(
    *,
    kind: str,
    probe: Callable[[], Mapping[str, Any]],
    wait_seconds: float,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Block until ``probe`` admits this waiter or ``wait_seconds`` elapses.

    Only the head waiter (lowest rank, then earliest arrival) may take an
    admission, so a freed slot goes to the highest-priority waiter.  Every
    return path, including an exception from ``probe``, removes the waiter.
    ``wait_seconds <= 0`` is exactly one probe, refused unprobed while a
    higher-ranked waiter is queued so a direct launch cannot take its slot.
    """
    if kind not in _RANKS:
        raise ValueError(f"unknown_admission_kind:{kind}")
    if wait_seconds <= 0:
        with _lock:
            outranked = any(w.rank < _RANKS[kind] for w in _waiters)
        if outranked:
            return {
                "admit": False,
                "retryable": True,
                "reason": OUTRANKED_REASON,
                "waited_seconds": 0.0,
            }
        return {**probe(), "waited_seconds": 0.0}
    waiter = _Waiter(_RANKS[kind])
    started = monotonic()
    with _lock:
        _waiters.append(waiter)
    last: Mapping[str, Any] | None = None
    try:
        while True:
            with _lock:
                is_head = min(_waiters, key=_Waiter.order) is waiter
            if is_head:
                last = probe()
                if last.get("admit") is True:
                    return {**last, "waited_seconds": round(monotonic() - started, 3)}
            waited = monotonic() - started
            if waited >= wait_seconds:
                return {
                    **(last or {}),
                    "admit": False,
                    "retryable": True,
                    "reason": EXPIRED_REASON,
                    "waited_seconds": round(waited, 3),
                }
            sleep(max(0.0, min(poll_seconds, wait_seconds - waited)))
    finally:
        with _lock:
            _waiters.remove(waiter)


def is_admission_refusal(reason: str) -> bool:
    return reason == CONCURRENCY_REFUSAL or reason.startswith(MEMORY_REFUSAL_PREFIX)


def admission_reason(reason: str) -> str:
    if not reason.startswith(MEMORY_REFUSAL_PREFIX):
        return reason
    try:
        detail = json.loads(reason[len(MEMORY_REFUSAL_PREFIX):])
    except ValueError:
        return "memory_capacity_insufficient"
    named = detail.get("reason") if isinstance(detail, dict) else None
    return str(named) if named else "memory_capacity_insufficient"


def defer_card(
    repo: Path | str, task_id: str, runner: str, reason: str
) -> tuple[bool, str]:
    """Stamp the pending card for reconciler relaunch; never raises."""
    try:
        return task_store.mark_admission_deferred(
            repo,
            task_id,
            runner=runner,
            reason=admission_reason(reason),
            retry_after_seconds=DEFERRED_RETRY_AFTER_SECONDS,
        )
    except (task_store.TaskStoreError, sqlite3.Error, OSError, ValueError) as exc:
        return False, f"admission_deferral_failed:{type(exc).__name__}"


def queued_receipt_fields(reason: str) -> dict[str, Any]:
    return {
        "retryable": True,
        "retry_after_seconds": DEFERRED_RETRY_AFTER_SECONDS,
        "admission_reason": admission_reason(reason),
    }
