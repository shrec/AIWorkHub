"""Launch-side bound on in-process reviewer Source Graph prewarms (NF-2026-00027).

A reviewer reservation reads as a live prewarm while its latest phase is
``reviewer_source_graph_prewarm_started`` and its owner -- the long-lived MCP
server -- is alive.  That row is written once and never refreshed while the
build runs, so a hung build, or a launcher that left without retiring the
phase, stayed "live" for the whole server lifetime: its launch owner waited
forever and the row held a launch slot past its reservation deadline.  Three
such rows filled the default queue until a restart changed the owner pid.
:func:`prewarm_outlived` bounds that reading; :func:`wait_for_slot` bounds how
many prewarms one server runs at once.

The bound is measured.  Prewarms run as threads inside the MCP server, so they
are GIL-bound: on the 16-core owner host (12 real repository files per
candidate, k concurrent real ``prewarm_quality_review_source_graph`` threads)
the batch took 7.0 s at k=1, 10.5 s at k=2, 17.9 s at k=3, 20.8 s at k=4 and
30.2 s at k=6.  Throughput stops rising at k=2 (0.14 -> 0.19 builds/s, then
flat); every further concurrent build only stretches each build's wall time
toward the stall ceiling.  So the capacity is that plateau, reduced on a small
host so one core always stays with the interactive server.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable, Mapping

from . import parallelism

CAPACITY_EXHAUSTED = "reviewer_prewarm_capacity_exhausted"
PREWARM_STARTED = "reviewer_source_graph_prewarm_started"
PREWARM_QUEUED = "reviewer_source_graph_prewarm_queued"
# Measured throughput plateau of concurrent in-process prewarms (module docstring).
_MEASURED_PLATEAU = 2
# ponytail: polls the cached ledger projection; a Condition fed by the
# complete/failed publishes would wake sooner if queue latency ever matters.
_POLL_SECONDS = 0.25
# Serializes "count live prewarms, then publish started" so two launchers can
# never both take the last slot.
_SLOT_LOCK = threading.Lock()


def capacity() -> int:
    """Concurrent in-process reviewer prewarms this host admits."""

    return max(1, min(_MEASURED_PLATEAU, parallelism.get_cpu_capacity() - 1))


def prewarm_outlived(event: Mapping[str, Any], ceiling: float) -> bool:
    """True once a started prewarm row is older than ``ceiling`` seconds.

    The started row is never refreshed while the build runs, so its heartbeat
    age is the prewarm's age.  A missing or malformed heartbeat is ambiguous
    and keeps the previous (live) reading.
    """

    heartbeat: Any = event.get("preparation_heartbeat_epoch")
    try:
        return time.time() - float(heartbeat) > ceiling
    except (TypeError, ValueError):
        return False


def wait_for_slot(
    manager: Any,
    request_id: str | None,
    progress: Callable[..., None],
    *,
    ceiling: float,
    owner_ticks: Any,
) -> str | None:
    """Publish the started phase once this server runs fewer than ``capacity()``.

    Queue with a bounded wait, never refuse at the door: the canonical review
    launches its three lenses together, and a refusal would only hand the third
    lens back to the caller to retry, while the measured plateau shows the
    batch finishes no sooner when it runs.  The wait runs in the reviewer's
    background launcher thread, never in the MCP handler.  It is bounded by
    ``ceiling`` because every counted prewarm stops counting within one ceiling
    (it completes, fails, or outlives it); a waiter still without a slot after
    that returns the named ``reviewer_prewarm_capacity_exhausted`` reason.

    Counted rows are this process's live prewarms, read from the durable ledger
    through ``manager._reviewer_source_graph_prewarm_live_event``, so the count
    heals itself and no in-memory slot can leak.  Returns ``None`` once the
    started phase is published, else the refusal reason.
    """

    owner_pid = os.getpid()
    deadline = time.monotonic() + ceiling
    queued = False
    while True:
        with _SLOT_LOCK:
            in_flight = sum(
                1
                for other, event in manager._latest_by_request().items()
                if other != request_id
                and event.get("owner_pid") == owner_pid
                and event.get("owner_pid_start_ticks") == owner_ticks
                and manager._reviewer_source_graph_prewarm_live_event(event)
            )
            limit = capacity()
            if in_flight < limit:
                progress(PREWARM_STARTED)
                return None
        if time.monotonic() >= deadline:
            return (
                f"{CAPACITY_EXHAUSTED}:in_flight={in_flight}:capacity={limit}"
                f":waited={ceiling:.0f}s"
            )
        if not queued:
            queued = True
            progress(PREWARM_QUEUED, f"in_flight={in_flight}:capacity={limit}")
        time.sleep(_POLL_SECONDS)
