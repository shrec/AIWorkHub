"""Per-repository automatic wake consumer for the manager loop.

While a manager loop session is active, this claims callback batches through
the existing lease API -- the same claim/ack primitives behind
``aiworkhub_claude_callback_wait``/``aiworkhub_claude_callback_ack``
(``callback_store.claim_pending_callback_batch`` and
``callback_store.acknowledge_callback_batch``) -- and turns each member into
a manager turn dispatched through the SAME non-blocking turn lock
``manager_loop_service.send`` uses. At most one member's turn is in flight.
A batch is acked only once every one of its members' turns has FINISHED and
was delivered; only a delivered turn counts toward the hourly cap. A member
that could not start (the lock was busy, or the hourly automatic-turn cap was
reached) stays queued in memory; a member whose turn failed goes back to the
front of the queue and is retried after a backoff. Either way its batch stays
un-acked, so a crash before ack re-delivers it.

No new callback system: :class:`WakeConsumer` never touches a database or an
orchestrator directly -- ``claim``/``ack``/``dispatch`` are injected, which is
what keeps it unit-testable with fakes. :func:`default_callback_source` is
the real wiring ``manager_loop_service`` installs in production.
"""

from __future__ import annotations

import math
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, time as day_time, timedelta, timezone, tzinfo
from functools import partial
from typing import Any, Callable, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

ClaimFn = Callable[[], Optional[Mapping[str, Any]]]
AckFn = Callable[[str, str], bool]
DispatchFn = Callable[[Mapping[str, Any], Callable[..., None]], bool]

DEFAULT_CAP_PER_HOUR = 12
DEFAULT_IDLE_POLL_SECONDS = 1.0
DEFAULT_RETRY_POLL_SECONDS = 0.2
DEFAULT_FAILED_TURN_BACKOFF_SECONDS = 300.0
DEFAULT_MAX_FAILED_TURN_BACKOFF_SECONDS = 3600.0
_HOUR_SECONDS = 3600.0
_MAX_BACKOFF_DOUBLINGS = 32

PARKED_PROVIDER_LIMIT = "provider_limit"
PARKED_PROVIDER_AUTH = "provider_auth"
PARKED_BACKOFF = "backoff"

_LIMIT_MARKERS = ("session limit", "usage limit", "rate limit", "rate_limit")
_AUTH_MARKERS = (
    "failed to authenticate",
    "oauth session expired",
    "/login",
    "invalid api key",
    "invalid_api_key",
)
_RESET_PATTERN = re.compile(
    r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?(?:\s*\(([^)]+)\))?",
    re.IGNORECASE,
)
_UTC_NAMES = frozenset({"utc", "gmt", "z"})


def failure_text(errors: Sequence[Any]) -> str:
    """The provider error strings of one failed turn as a single searchable text."""

    parts: list[str] = []
    for error in errors or ():
        if isinstance(error, Mapping):
            parts.extend(str(value) for value in error.values())
        else:
            parts.append(str(error))
    return " ".join(parts)


def classify_failure(errors: Sequence[Any]) -> str | None:
    """``provider_limit``, ``provider_auth`` or None (a generic failure) for a failed turn."""

    text = failure_text(errors).lower()
    if any(marker in text for marker in _LIMIT_MARKERS):
        return PARKED_PROVIDER_LIMIT
    if any(marker in text for marker in _AUTH_MARKERS):
        return PARKED_PROVIDER_AUTH
    return None


def _zone(name: str) -> tzinfo | None:
    if name.lower() in _UTC_NAMES:
        return timezone.utc
    try:
        return ZoneInfo(name)
    except (KeyError, ValueError, OSError):
        return None


def parse_reset_delay_seconds(text: str, now: datetime) -> float | None:
    """Seconds from ``now`` until the next ``resets 4am (Asia/Tbilisi)`` moment, else None.

    ``now`` must be timezone-aware. A reset without a zone name is read in
    ``now``'s own zone; an unknown zone or no ``resets`` clause yields None.
    """

    match = _RESET_PATTERN.search(text)
    if match is None or now.tzinfo is None:
        return None
    hour, minute = int(match.group(1)), int(match.group(2) or 0)
    if not 1 <= hour <= 12 or minute > 59:
        return None
    hour = hour % 12 + (12 if match.group(3).lower() == "p" else 0)
    zone_name = (match.group(4) or "").strip()
    zone = _zone(zone_name) if zone_name else now.tzinfo
    if zone is None:
        return None
    local_now = now.astimezone(zone)
    target = datetime.combine(local_now.date(), day_time(hour, minute), tzinfo=zone)
    if target <= local_now:
        target = datetime.combine(local_now.date() + timedelta(days=1), day_time(hour, minute), tzinfo=zone)
    return (target.astimezone(timezone.utc) - now.astimezone(timezone.utc)).total_seconds()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class _QueuedMember:
    member: dict[str, Any]
    batch_id: str


@dataclass
class _BatchState:
    lease_id: str
    remaining: int


@dataclass
class _Turn:
    queued: _QueuedMember
    finished: bool = False
    dequeued: bool = False


class WakeConsumer:
    """One repository's automatic-wake background thread.

    ``claim`` and ``ack`` stand in for the callback lease API (a fake in
    tests, the real outbox in production -- see :func:`default_callback_source`).
    ``dispatch(member, done)`` attempts to start one member's turn through the
    caller's own non-blocking turn lock and returns whether the turn STARTED.
    For a started turn the dispatcher calls ``done(delivered, errors)`` once,
    when the turn finishes (``errors`` are the failed turn's provider error
    strings and may be omitted); if dispatch returns False or raises, ``done``
    is not expected and the member stays queued. A delivered turn counts toward
    the cap and settles its batch; a failed one is requeued at the front,
    un-acked and uncounted, and the consumer parks before retrying it:

    * usage/session-limit text parks until the reset time the text names (read
      on ``wall_clock``, never earlier), else on the escalating backoff;
    * authentication-failure text parks with no automatic retry until
      :meth:`resume`;
    * any other failure backs off ``failed_turn_backoff_seconds`` x 2^(n-1) for
      the n-th consecutive failure, capped at ``max_failed_turn_backoff_seconds``.

    A delivered turn resets n to 0. A callback is never dropped because the
    provider is down: its batch stays queued and un-acked throughout.
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
        failed_turn_backoff_seconds: float = DEFAULT_FAILED_TURN_BACKOFF_SECONDS,
        max_failed_turn_backoff_seconds: float = DEFAULT_MAX_FAILED_TURN_BACKOFF_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._claim = claim
        self._ack = ack
        self._dispatch = dispatch
        self._cap_per_hour = max(0, int(cap_per_hour))
        self._idle_poll_seconds = max(0.0, float(idle_poll_seconds))
        self._retry_poll_seconds = max(0.0, float(retry_poll_seconds))
        self._failed_turn_backoff_seconds = max(0.0, float(failed_turn_backoff_seconds))
        self._max_failed_turn_backoff_seconds = max(
            self._failed_turn_backoff_seconds, float(max_failed_turn_backoff_seconds)
        )
        self._clock = clock
        self._wall_clock = wall_clock

        self._state_lock = threading.Lock()
        self._queue: list[_QueuedMember] = []
        self._batches: dict[str, _BatchState] = {}
        self._turn_times: list[float] = []
        self._in_flight: _Turn | None = None
        self._failed_turns = 0
        self._retry_at = float("-inf")
        self._parked_reason: str | None = None

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
        """Stop the background thread. Any batch not yet fully delivered stays un-acked."""

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
            paused = self._paused_locked()
            return {
                "running": running,
                "queued": len(self._queue),
                "turns_this_hour": len(self._turn_times),
                "cap": self._cap_per_hour,
                "in_flight": 0 if self._in_flight is None else 1,
                "failed_turns": self._failed_turns,
                "parked_reason": self._parked_reason if paused else None,
                "retry_in_seconds": (
                    None if self._retry_at == math.inf else max(0.0, float(self._retry_at - self._clock()))
                ),
            }

    def resume(self) -> bool:
        """Un-park an authentication failure so the queued callback is retried now.

        The owner's next send/start/continue calls this. A limit or backoff park
        runs its course instead; returns whether anything was un-parked.
        """

        with self._state_lock:
            if self._parked_reason != PARKED_PROVIDER_AUTH:
                return False
            self._parked_reason = None
            self._retry_at = float("-inf")
            return True

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
            self._stop.wait(self._next_wait_seconds())

    def _next_wait_seconds(self) -> float:
        # A paused consumer (a turn just failed) waits the idle poll, not the
        # retry poll: nothing can start before the backoff elapses anyway.
        with self._state_lock:
            if self._queue and not self._paused_locked():
                return self._retry_poll_seconds
            return self._idle_poll_seconds

    def _paused_locked(self) -> bool:
        return self._clock() < self._retry_at

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
        """Start the next queued member, in order, while nothing is in flight.

        Returns True if a turn started. The started member leaves the queue and
        becomes the single in-flight member; nothing is counted or acked until
        its ``done`` reports the outcome (see :meth:`_finish_turn`). A member
        whose dispatch did not start stays at the front of the queue throughout.
        """

        started_any = False
        while True:
            with self._state_lock:
                if (
                    self._in_flight is not None
                    or not self._queue
                    or self._paused_locked()
                    or not self._under_cap_locked()
                ):
                    break
                queued = self._queue[0]
                turn = _Turn(queued=queued)
                self._in_flight = turn
            try:
                started = bool(self._dispatch(queued.member, partial(self._finish_turn, turn)))
            except Exception:  # noqa: BLE001 -- a dispatch failure must never kill the consumer
                started = False
            with self._state_lock:
                if not started:
                    # The turn never started: no done() is expected, so the
                    # member simply stays queued.
                    if not turn.finished:
                        turn.finished = True
                        if self._in_flight is turn:
                            self._in_flight = None
                    break
                self._leave_queue_locked(turn)
            started_any = True
        return started_any

    def _leave_queue_locked(self, turn: _Turn) -> None:
        if not turn.dequeued:
            turn.dequeued = True
            self._queue = [queued for queued in self._queue if queued is not turn.queued]

    def _backoff_seconds_locked(self) -> float:
        doublings = min(max(self._failed_turns - 1, 0), _MAX_BACKOFF_DOUBLINGS)
        return min(
            self._failed_turn_backoff_seconds * (2.0**doublings), self._max_failed_turn_backoff_seconds
        )

    def _park_after_failure_locked(self, errors: Sequence[Any]) -> None:
        kind = classify_failure(errors)
        if kind == PARKED_PROVIDER_AUTH:
            self._parked_reason = kind
            self._retry_at = math.inf
            return
        delay = self._backoff_seconds_locked()
        if kind == PARKED_PROVIDER_LIMIT:
            try:
                reset_delay = parse_reset_delay_seconds(failure_text(errors), self._wall_clock())
            except Exception:  # noqa: BLE001 -- an unreadable reset falls back to the escalating backoff
                reset_delay = None
            if reset_delay is not None:
                delay = max(0.0, reset_delay)
        self._parked_reason = kind or PARKED_BACKOFF
        self._retry_at = self._clock() + delay

    def _finish_turn(self, turn: _Turn, delivered: bool, errors: Sequence[Any] = ()) -> None:
        """``done`` for one started turn: thread-safe, idempotent, never raises.

        A delivered turn counts toward the hourly cap and settles its batch. A
        failed one goes back to the front of the queue, un-acked and uncounted,
        and parks the consumer according to its provider ``errors`` (see the
        class docstring).
        """

        try:
            with self._state_lock:
                if turn.finished:
                    return
                turn.finished = True
                if self._in_flight is turn:
                    self._in_flight = None
                self._leave_queue_locked(turn)
                if not delivered:
                    self._queue.insert(0, turn.queued)
                    self._failed_turns += 1
                    self._park_after_failure_locked(errors)
                    return
                self._turn_times.append(self._clock())
                self._failed_turns = 0
                self._parked_reason = None
            self._settle_batch(turn.queued.batch_id)
        except Exception:  # noqa: BLE001 -- done runs on the turn thread and must never raise
            pass

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
    *, session_id: str | Callable[[], str], provider: str | None = None, lease_seconds: int = 120
) -> tuple[ClaimFn, AckFn]:
    """The real claim/ack wiring: the same lease API behind the legacy MCP poll tools.

    ``provider=None`` is the Manager Chat seat. It copies still-pending
    Codex/Claude rows onto the active session and claims only that copy.
    It does not rebind those original rows, so the existing mux can still
    deliver them. An explicit provider keeps the older single-family claim.
    The ack resolves each batch's own provider/origin from its row.
    """

    def origin() -> str:
        current = session_id() if callable(session_id) else session_id
        return str(current or "").strip()

    def claim() -> Optional[Mapping[str, Any]]:
        from . import callback_store
        from .core import _canonical_connect

        active_id = origin()
        if not active_id:
            return None
        conn = _canonical_connect()
        try:
            if provider is None:
                callback_store.mirror_pending_to_manager_chat(conn, active_id)
                family = callback_store.MANAGER_CHAT_PROVIDER
                callback_store.rebind_pending_callbacks(
                    conn, provider=family, origin_thread_id=active_id,
                )
                batch = callback_store.claim_pending_callback_batch(
                    conn,
                    lease_seconds=lease_seconds,
                    provider=family,
                    origin_thread_id=active_id,
                )
                claimed_family = family if batch else ""
            else:
                family = str(provider).strip().lower()
                claimed_family = ""
                batch = None
                if family:
                    callback_store.rebind_pending_callbacks(
                        conn, provider=family, origin_thread_id=active_id,
                    )
                    batch = callback_store.claim_pending_callback_batch(
                        conn,
                        lease_seconds=lease_seconds,
                        provider=family,
                        origin_thread_id=active_id,
                    )
                    if batch:
                        claimed_family = family
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
