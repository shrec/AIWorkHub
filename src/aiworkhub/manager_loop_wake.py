"""Per-repository automatic wake consumer for the manager loop.

While a manager loop session is active, this claims callback batches through
the existing lease API -- the same claim/ack primitives behind
``aiworkhub_claude_callback_wait``/``aiworkhub_claude_callback_ack``
(``callback_store.claim_pending_callback_batch`` and
``callback_store.acknowledge_callback_batch``) -- and turns each member into
a manager turn dispatched through the SAME non-blocking turn lock
``manager_loop_service.send`` uses. A batch is acked only once every one of
its members' turns has STARTED; a member that could not start (the lock was
busy, or the hourly automatic-turn cap was reached) stays queued in memory
and its batch stays un-acked, so a crash before ack re-delivers it.

No new callback system: :class:`WakeConsumer` never touches a database or an
orchestrator directly -- ``claim``/``ack``/``dispatch`` are injected, which is
what keeps it unit-testable with fakes. :func:`default_callback_source` is
the real wiring ``manager_loop_service`` installs in production.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

ClaimFn = Callable[[], Optional[Mapping[str, Any]]]
AckFn = Callable[[str, str], bool]
DispatchFn = Callable[[Mapping[str, Any]], bool]

DEFAULT_CAP_PER_HOUR = 12
DEFAULT_IDLE_POLL_SECONDS = 1.0
DEFAULT_RETRY_POLL_SECONDS = 0.2
_HOUR_SECONDS = 3600.0


@dataclass
class _QueuedMember:
    member: dict[str, Any]
    batch_id: str


@dataclass
class _BatchState:
    lease_id: str
    remaining: int


class WakeConsumer:
    """One repository's automatic-wake background thread.

    ``claim`` and ``ack`` stand in for the callback lease API (a fake in
    tests, the real outbox in production -- see :func:`default_callback_source`).
    ``dispatch`` attempts to start one member's turn through the caller's own
    non-blocking turn lock and reports only whether the turn STARTED, never
    whether it finished -- that is all acking needs to know.
    """

    def __init__(
        self,
        *,
        claim: ClaimFn,
        ack: AckFn,
        dispatch: DispatchFn,
        cap_per_hour: int = DEFAULT_CAP_PER_HOUR,
        idle_poll_seconds: float = DEFAULT_IDLE_POLL_SECONDS,
        retry_poll_seconds: float = DEFAULT_RETRY_POLL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._claim = claim
        self._ack = ack
        self._dispatch = dispatch
        self._cap_per_hour = max(0, int(cap_per_hour))
        self._idle_poll_seconds = max(0.0, float(idle_poll_seconds))
        self._retry_poll_seconds = max(0.0, float(retry_poll_seconds))
        self._clock = clock

        self._state_lock = threading.Lock()
        self._queue: list[_QueuedMember] = []
        self._batches: dict[str, _BatchState] = {}
        self._turn_times: list[float] = []

        self._run_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        """Start the background thread. A second call, while running, is a no-op."""

        with self._run_lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop.clear()
            thread = threading.Thread(target=self._run, daemon=True)
            self._thread = thread
            thread.start()
            return True

    def stop(self, timeout: float | None = None) -> None:
        """Stop the background thread. Any batch not yet fully started stays un-acked."""

        self._stop.set()
        with self._run_lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def status(self) -> dict[str, Any]:
        with self._run_lock:
            running = self._thread is not None and self._thread.is_alive()
        with self._state_lock:
            self._prune_turn_times_locked()
            return {
                "running": running,
                "queued": len(self._queue),
                "turns_this_hour": len(self._turn_times),
                "cap": self._cap_per_hour,
            }

    def _prune_turn_times_locked(self) -> None:
        floor = self._clock() - _HOUR_SECONDS
        self._turn_times = [when for when in self._turn_times if when > floor]

    def _under_cap_locked(self) -> bool:
        self._prune_turn_times_locked()
        return len(self._turn_times) < self._cap_per_hour

    def _run(self) -> None:
        while not self._stop.is_set():
            self._drain_queue()
            try:
                batch = self._claim()
            except Exception:  # noqa: BLE001 -- a claim failure must never kill the consumer
                batch = None
            if batch:
                self._enqueue_batch(batch)
                self._drain_queue()
                continue
            wait_seconds = self._retry_poll_seconds if self._has_queued() else self._idle_poll_seconds
            self._stop.wait(wait_seconds)

    def _has_queued(self) -> bool:
        with self._state_lock:
            return bool(self._queue)

    def _enqueue_batch(self, batch: Mapping[str, Any]) -> None:
        batch_id = str(batch.get("batch_id") or "")
        lease_id = str(batch.get("lease_id") or "")
        members = [dict(member) for member in (batch.get("members") or [])]
        if not batch_id or not members:
            return
        with self._state_lock:
            existing = self._batches.get(batch_id)
            if existing is not None:
                # Re-delivery of a batch already tracked in memory (its lease
                # lapsed while a member was still queued/in-flight) -- renew
                # the lease so the eventual ack still matches, but never
                # re-enqueue its members: they are already queued or already
                # had their turn started, and re-adding them would wake the
                # same callback twice.
                existing.lease_id = lease_id
                return
            self._batches[batch_id] = _BatchState(lease_id=lease_id, remaining=len(members))
            self._queue.extend(_QueuedMember(member=member, batch_id=batch_id) for member in members)

    def _drain_queue(self) -> bool:
        """Try to start every queued member once, in order. Returns True if any started."""

        started_any = False
        while True:
            with self._state_lock:
                if not self._queue or not self._under_cap_locked():
                    break
                queued = self._queue[0]
            try:
                started = bool(self._dispatch(queued.member))
            except Exception:  # noqa: BLE001 -- a dispatch failure must never kill the consumer
                started = False
            if not started:
                break
            with self._state_lock:
                self._queue.pop(0)
                self._turn_times.append(self._clock())
            started_any = True
            self._settle_batch(queued.batch_id)
        return started_any

    def _settle_batch(self, batch_id: str) -> None:
        with self._state_lock:
            state = self._batches.get(batch_id)
            if state is None:
                return
            state.remaining -= 1
            if state.remaining > 0:
                return
            lease_id = state.lease_id
            del self._batches[batch_id]
        try:
            self._ack(batch_id, lease_id)
        except Exception:  # noqa: BLE001 -- an ack failure leaves the batch for lease-expiry re-delivery
            pass


def default_callback_source(
    *, session_id: str, provider: str | None = None, lease_seconds: int = 120
) -> tuple[ClaimFn, AckFn]:
    """The real claim/ack wiring: the same lease API behind the legacy MCP poll tools.

    A fresh callback's ``origin_thread_id`` is whoever created the task --
    never this manager loop session's own id -- so claiming scoped to
    ``session_id`` alone would never see it. Before every claim, this rebinds
    the repository's durable pending callbacks onto ``session_id``, exactly
    the way the bootstrap dispatcher hands a repository's callbacks to
    whichever route is its current verified manager
    (``callback_store.rebind_pending_callbacks`` -- see ``core.py``'s
    ``manager_inbox`` bootstrap). ``provider=None`` (the Manager Chat wake
    consumer) covers every pending family -- a seat on any backend sees
    worker terminals launched under any other; an explicit provider keeps
    the legacy single-family scope. The ack resolves each batch's own
    provider/origin from its row, so mixed-family batches settle correctly.
    """

    def _families(conn: Any) -> list[str]:
        from . import callback_store as _store

        if provider is not None:
            name = str(provider).strip().lower()
            return [name] if name else []
        return _store.pending_callback_providers(conn)

    def claim() -> Optional[Mapping[str, Any]]:
        from . import callback_store
        from .core import _canonical_connect

        conn = _canonical_connect()
        try:
            families = _families(conn)
            batch = None
            claimed_family = ""
            for family in families:
                callback_store.rebind_pending_callbacks(
                    conn, provider=family, origin_thread_id=session_id,
                )
                batch = callback_store.claim_pending_callback_batch(
                    conn,
                    lease_seconds=lease_seconds,
                    provider=family,
                    origin_thread_id=session_id,
                )
                if batch:
                    claimed_family = family
                    break
        finally:
            conn.close()
        if not batch:
            return batch
        # A claimed member is a raw ``callback_outbox`` row: its own ``state``
        # column is delivery lifecycle ('inflight' at claim time), never the
        # task outcome ``ManagerOrchestrator.wake`` words the turn with. Use
        # the row's ``transition`` (the actual outcome) as the member's state.
        # ``_wake_provider`` routes the later ack to this batch's own family.
        return {
            **batch,
            "_wake_provider": claimed_family,
            "members": [
                {**member, "state": member.get("transition", "")}
                for member in (batch.get("members") or [])
            ],
        }

    def ack(batch_id: str, lease_id: str) -> bool:
        from . import callback_store
        from .core import _canonical_connect

        conn = _canonical_connect()
        try:
            # The batch carries its own family: a mixed-family consumer must
            # ack with the row's provider/origin, not the closure's, or the
            # lease match fails and the batch redelivers forever.
            row = conn.execute(
                "SELECT provider, origin_thread_id FROM callback_batches WHERE batch_id=?",
                (str(batch_id or ""),),
            ).fetchone()
            if row is None:
                return False
            return callback_store.acknowledge_callback_batch(
                conn,
                batch_id,
                lease_id,
                provider=str(row["provider"] or ""),
                origin_thread_id=str(row["origin_thread_id"] or ""),
            )
        finally:
            conn.close()

    return claim, ack
