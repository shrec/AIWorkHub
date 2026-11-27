"""Bounded retry of a transiently locked terminal review transition (NF-2026-00856).

Measured root cause: 82% of runtime lock events are
``review_transition_failed:database is locked`` raised out of
``task_store.mark_terminal_review_with_callback``. ``process_launcher`` then
downgrades a completed, already-paid worker run to ``review_pending`` and the
run is relaunched. A SQLite lock is transient, so the finalizer re-invokes the
exact transition for a bounded wall-clock budget before giving up. The exact
transition is idempotent (``idempotent_noop``), so a retry after a lost success
is safe.

Only a failed result whose text names a SQLite lock/busy condition is retried;
every other failure, and every success, is returned at once and unchanged.
"""

from __future__ import annotations

import random
import time
from typing import Any, Callable, Mapping

#: Case-insensitive markers of a transient SQLite writer conflict.
TRANSIENT_LOCK_MARKERS: tuple[str, ...] = ("database is locked", "database is busy")
#: Wall-clock budget for all retries of one transition, in seconds.
DEFAULT_BUDGET_S = 30.0
#: First backoff ceiling; doubles per retry up to ``MAX_DELAY_S`` (full jitter).
BASE_DELAY_S = 0.05
MAX_DELAY_S = 2.0
#: Floor for each sleep so a retry always yields the writer lease to its owner.
MIN_DELAY_S = 0.01
#: Safety ceiling only: a clock that never advances must not spin forever. The
#: budget, not this count, is the operative bound under a real clock.
MAX_ATTEMPTS_CEILING = 1000


def is_transient_lock_failure(result: Any) -> bool:
    """True when ``result`` is a failed transition naming a SQLite lock/busy."""

    if not isinstance(result, Mapping) or result.get("ok"):
        return False
    text = (str(result.get("stderr") or "") + "\n" + str(result.get("stdout") or "")).lower()
    return any(marker in text for marker in TRANSIENT_LOCK_MARKERS)


def retry_transient_transition(
    call: Callable[[], Any],
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    budget_s: float = DEFAULT_BUDGET_S,
    rng: Callable[[], float] = random.random,
) -> Any:
    """Invoke ``call`` and re-invoke it only while it fails on a SQLite lock.

    Retries use full-jitter exponential backoff, each sleep clipped to the
    remaining wall-clock budget. The last result is returned unchanged, so a
    caller's existing ``review_pending``/``review_transition_failed`` handling
    still applies when the budget is exhausted.
    """

    deadline = clock() + max(0.0, float(budget_s))
    result = call()
    attempt = 1
    while is_transient_lock_failure(result) and attempt < MAX_ATTEMPTS_CEILING:
        remaining = deadline - clock()
        if remaining <= 0:
            break
        ceiling = min(MAX_DELAY_S, BASE_DELAY_S * (2 ** min(attempt - 1, 16)))
        delay = min(remaining, max(MIN_DELAY_S, ceiling * rng()))
        sleep(delay)
        result = call()
        attempt += 1
    return result
