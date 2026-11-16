"""Detached dispatch for ``aiworkhub_agent_retry_finalization`` (NF-2026-02099).

The MCP tool used to run the whole retried finalization -- declared
validations, quality gates, cleanup -- inside the tool call. A finalize that
stalled froze the manager seat for 30+ minutes and nothing reported progress or
failure. The contract here:

* Admission (identity, retryability, seal, receipt refresh, card transition and
  the ``finalizing`` row) is still decided before the call returns, so every
  refusal reaches the caller with the request unchanged.
* Once the retry is admitted -- ``mark_admitted`` is called right after the
  ``finalizing`` row is published under a live finalizer heartbeat -- the
  caller gets a receipt. The finalize keeps running on an owned daemon thread;
  its terminal review is the callback and its heartbeat carries the progress.
* A finalize that settles inside ``SETTLE_GRACE_SECONDS`` is answered with its
  terminal outcome instead of a receipt; nothing is ever killed or abandoned
  because of elapsed time.
* At most one retry per request is in flight per process. A duplicate call gets
  a receipt naming the live one rather than queueing behind the request lock.
* A ``finalizing`` row is reclaimed only on positive evidence that its
  finalizer died (``abandoned_finalizer_state``), never by wall-clock waiting.

The retry's admission body stays on ``ProcessManager``; the post-admission
settle (``settle_admitted``), the liveness classification and the shared
``finalize_failed`` recorder (``record_finalizer_failed``) live here so
``process_launcher.py`` keeps its ratchet headroom. They read every
``process_launcher`` name through a lazy module import, so a
``monkeypatch.setattr(process_launcher, ...)`` still reaches them.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

_LOG = logging.getLogger(__name__)

# Ceiling on the synchronous admission phase. Admission is bounded work (the
# request lock is taken non-blocking), so this only guards the MCP call against
# a pathological filesystem; a retry still pending admission is reported by
# receipt, never cancelled.
ADMISSION_WAIT_SECONDS = 30.0
# How long an admitted finalize may take to settle before the caller is
# answered with a receipt instead of the terminal outcome.
SETTLE_GRACE_SECONDS = 2.0
RECEIPT_SCHEMA_ID = "aiworkhub.finalization_retry_receipt.v1"
_ERROR_MAX_CHARS = 500


class _Retry:
    """One dispatched retry: its admission/settlement signals and outcome."""

    __slots__ = ("request_id", "task_id", "admitted", "settled", "done", "result")

    def __init__(self, request_id: str, task_id: str) -> None:
        self.request_id = request_id
        self.task_id = task_id
        self.admitted = False
        self.settled = threading.Event()  # admitted or done
        self.done = threading.Event()
        self.result: dict[str, Any] | None = None


_GUARD = threading.Lock()
# Bounded by the number of concurrently retried requests: an entry lives
# exactly as long as its dispatch thread runs.
_IN_FLIGHT: dict[str, _Retry] = {}
_CURRENT = threading.local()


def in_flight(request_id: str) -> bool:
    """Whether a dispatched retry of ``request_id`` is still running here."""
    with _GUARD:
        return request_id in _IN_FLIGHT


def mark_admitted(request_id: str) -> None:
    """Signal that the running retry published its ``finalizing`` row.

    Only the dispatch thread that owns ``request_id`` can signal; a direct,
    undispatched call of the retry body is a no-op.
    """
    retry = getattr(_CURRENT, "retry", None)
    if retry is not None and retry.request_id == request_id:
        retry.admitted = True
        retry.settled.set()


def receipt(request_id: str, task_id: str, *, admitted: bool, already_in_flight: bool) -> dict[str, Any]:
    """The immediate answer for a retry whose outcome arrives as a callback."""
    return {
        "ok": True,
        "schema_id": RECEIPT_SCHEMA_ID,
        "request_id": request_id,
        "task_id": task_id,
        "state": "finalizing" if admitted else "admission_pending",
        "deferred": True,
        "finalization_retry_dispatched": True,
        "already_in_flight": already_in_flight,
        "provider_relaunched": False,
        "workspace_retained": True,
        # Not yet admitted: a later refusal is only logged, so no callback is
        # promised and the caller polls the task status instead.
        "outcome_delivery": "terminal_review_callback" if admitted else "poll_task_status",
        "error": "",
    }


def _run_detached(retry: _Retry, run: Callable[[], dict[str, Any]]) -> None:
    _CURRENT.retry = retry
    try:
        result = run()
    except Exception as exc:  # noqa: BLE001 -- the owned thread must always settle
        _LOG.exception("finalization retry crashed request_id=%s", retry.request_id)
        result = {
            "ok": False,
            "request_id": retry.request_id,
            "task_id": retry.task_id,
            "error": f"finalization_retry_crashed:{type(exc).__name__}:{exc}"[:_ERROR_MAX_CHARS],
        }
    finally:
        _CURRENT.retry = None
    retry.result = result
    with _GUARD:
        if _IN_FLIGHT.get(retry.request_id) is retry:
            del _IN_FLIGHT[retry.request_id]
    retry.done.set()
    retry.settled.set()
    if not result.get("ok"):
        # Every non-ok outcome is logged, admitted or not: a refusal that lands
        # after the caller already got its receipt must never be dropped.
        _LOG.warning(
            "finalization retry ended request_id=%s admitted=%s state=%s error=%s",
            retry.request_id, retry.admitted, result.get("state"), str(result.get("error") or "")[:200],
        )


def dispatch(request_id: str, task_id: str, run: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Run ``run`` (the synchronous retry body) detached; answer promptly.

    Returns the body's own result when it settles before admission (every
    refusal) or within ``SETTLE_GRACE_SECONDS`` of admission, else a receipt.
    """
    with _GUARD:
        live = _IN_FLIGHT.get(request_id)
        if live is not None:
            if live.task_id != task_id:
                # A request belongs to exactly one task: a duplicate naming a
                # different task is refused, never answered with a receipt
                # that mislabels the live retry.
                return {
                    "ok": False,
                    "request_id": request_id,
                    "task_id": task_id,
                    "error": (
                        f"finalization_retry_task_identity_mismatch:in_flight_task_id={live.task_id}"
                    )[:_ERROR_MAX_CHARS],
                }
            # The live retry's own admission, read under the guard: a duplicate
            # of a retry still pending admission is told exactly that.
            return receipt(
                request_id, live.task_id, admitted=live.admitted, already_in_flight=True
            )
        retry = _Retry(request_id, task_id)
        _IN_FLIGHT[request_id] = retry
    thread = threading.Thread(
        target=_run_detached,
        args=(retry, run),
        name=f"aiworkhub-retry-finalize-{request_id[:8]}",
        daemon=True,
    )
    try:
        thread.start()
    except RuntimeError as exc:
        with _GUARD:
            _IN_FLIGHT.pop(request_id, None)
        return {
            "ok": False,
            "request_id": request_id,
            "task_id": task_id,
            "error": f"finalization_retry_dispatch_failed:{exc}"[:_ERROR_MAX_CHARS],
        }
    retry.settled.wait(ADMISSION_WAIT_SECONDS)
    if retry.admitted:
        retry.done.wait(SETTLE_GRACE_SECONDS)
    if retry.done.is_set() and retry.result is not None:
        return dict(retry.result)
    return receipt(request_id, task_id, admitted=retry.admitted, already_in_flight=False)


def abandoned_finalizer_state(latest: dict[str, Any]) -> str:
    """Classify a ``finalizing`` row's finalizer: ``abandoned``, ``unknown`` or ``live``.

    The admission lock is released before the finalize runs, so holding it
    proves nothing about a ``finalizing`` row. Only positive evidence of death
    reclaims one (``abandoned``): the row's finalizer section names an exact
    pid/start-ticks identity that no longer matches a process. A missing or
    malformed section, or a live finalizer whose heartbeat merely went stale,
    is ``unknown`` and refused with the request unchanged; a live, beating one
    is ``live`` and not retryable. Liveness decides, never elapsed time.
    """
    from . import process_launcher as pl  # lazy: process_launcher imports this module

    status_now = pl.read_supervisor_status(Path(str(latest.get("supervisor_status_path") or "")))
    section = status_now.get(pl.FINALIZER_STATUS_SECTION)
    section = section if isinstance(section, dict) else {}
    finalizer_pid = section.get("pid")
    finalizer_ticks = section.get("pid_start_ticks")
    if (
        # A pid <= 0 names no process at all (0/-1 address process groups),
        # so its mismatch is not evidence of death.
        type(finalizer_pid) is int
        and finalizer_pid > 0
        and type(finalizer_ticks) is int
        and not pl._pid_matches(finalizer_pid, finalizer_ticks)
    ):
        return "abandoned"
    if not pl.finalizer_liveness(latest, status_now):
        return "unknown"
    return "live"


# NF-2026-02275: the explicit reasons a ``finalizing`` row is ended with when
# nothing can settle it any more.
FINALIZER_DEAD_REASON = "finalize_failed:finalizer_dead"
VALIDATION_HUNG_REASON = "validation_failed:validation_hung"
# Every declared validation command is bounded by
# ``worker_workspace.MAX_VALIDATION_SECONDS`` and they run in sequence, so a
# validating phase older than ``total`` such ceilings plus this grace has
# outlived every timeout the validator enforces: it is hung, not slow.
VALIDATION_DEADLINE_GRACE_SECONDS = 300.0


def finalizer_claim_epoch(events: Iterable[Mapping[str, Any]]) -> int:
    """The finalization claim epoch of a request's ledger rows (NF-2026-02275).

    The highest ``finalizer_claim_epoch`` a retry stamped, else the last
    integer ``claim_epoch``, else ``0``.
    """
    stamped: int | None = None
    claim = 0
    for event in events:
        value = event.get("finalizer_claim_epoch")
        if type(value) is int and (stamped is None or value > stamped):
            stamped = value
        value = event.get("claim_epoch")
        if type(value) is int:
            claim = value
    return claim if stamped is None else stamped


def bound_claim_epoch(current: int | None, events: Iterable[Mapping[str, Any]]) -> int:
    """``current`` when a retry already bound a claim epoch, else the ledger's."""
    return current if type(current) is int else finalizer_claim_epoch(events)


def finalizer_fence_path(manager: Any, request_id: str) -> Path:
    identity = hashlib.sha256(str(request_id).encode("utf-8")).hexdigest()
    return Path(manager.process_dir) / ".finalizer-fences" / f"{identity}.json"


def fence_finalizer(manager: Any, request_id: str, reason: str, latest: Mapping[str, Any]) -> None:
    """NF-2026-02275: every later write of this request's current finalizer is ignored.

    The fence names ``{request_id, claim_epoch}``: only a finalizer writing
    under exactly that epoch is dropped, so a retry under a fresh epoch
    (``fresh_finalizer_epoch``) is never fenced by an older end.
    """
    from . import process_launcher as pl  # lazy: process_launcher imports this module

    stamped = latest.get("finalizer_claim_epoch")
    epoch = stamped if type(stamped) is int else finalizer_claim_epoch(
        manager._request_events(request_id)
    )
    path = finalizer_fence_path(manager, request_id)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    pl.write_json_0600(path, {
        "request_id": request_id,
        "claim_epoch": epoch,
        "reason": str(reason)[:300],
        "fenced_at": pl._utcnow(),
    })


def stored_fence_epoch(manager: Any, request_id: str) -> int | None:
    from . import process_launcher as pl  # lazy: process_launcher imports this module

    record = pl.read_supervisor_status(finalizer_fence_path(manager, request_id))
    if not isinstance(record, dict):  # absent/unreadable fence names no epoch
        return None
    epoch = record.get("claim_epoch")
    if record.get("request_id") != request_id or type(epoch) is not int:
        return None
    return epoch


def finalizer_fenced(manager: Any, request_id: str, claim_epoch: int | None) -> bool:
    """True only when the stored fence names this request at exactly ``claim_epoch``."""
    if type(claim_epoch) is not int:
        return False
    return stored_fence_epoch(manager, request_id) == claim_epoch


def fresh_finalizer_epoch(manager: Any, request_id: str) -> int:
    """A claim epoch above every epoch this request's ledger or fence has named."""
    stored = stored_fence_epoch(manager, request_id)
    current = finalizer_claim_epoch(manager._request_events(request_id))
    return max(current, stored if stored is not None else current) + 1


def active_finalizer_fenced(manager: Any, request_id: str) -> bool:
    """This thread's finalizer of ``request_id`` writes under a fenced epoch.

    A fence is checked only inside a finalization of this exact request (its
    heartbeat is this thread's active one), so no other writer -- the
    reconciler, cancel, GC -- ever pays for or trips over it.
    """
    from . import process_launcher as pl  # lazy: process_launcher imports this module

    active = getattr(pl._FinalizerHeartbeat._active, "heartbeat", None)
    if active is None or not request_id or active._request_id != request_id:
        return False
    if getattr(manager, "process_dir", None) is None:  # a manager with no fence store
        return False
    return (
        finalizer_fence_path(manager, request_id).is_file()
        and finalizer_fenced(manager, request_id, active.claim_epoch)
    )


def fenced_transition_refusal(manager: Any, request_id: str) -> dict[str, Any] | None:
    """The card-transition answer for a fenced finalizer, else ``None``."""
    if not active_finalizer_fenced(manager, request_id):
        return None
    return {"ok": False, "finalizer_fenced": True, "stderr": "finalizer_fenced",
            "callback_enqueued": False}


def reconcile_pending_finalization(manager: Any, request_id: str, state: Any) -> int:
    """``1`` when this reconcile pass settled the pending request terminal, else ``0``.

    A finalizer that is provably dead, or whose validation outlived every
    enforced timeout, ends explicitly; anything else finalizes as before.
    """
    from . import process_launcher as pl  # lazy: process_launcher imports this module

    ended = end_if_finalizer_dead(manager, request_id) if state == "finalizing" else None
    event = ended or manager._finalize_after_process_exit(request_id)
    return int(event is not None and event.get("state") in pl.TERMINAL_PROCESS_STATES)


def drop_closed_writers(repo: Any, risks: list[Any]) -> list[Any]:
    """NF-2026-02276: a card closed by cancel or supersede is no writer."""
    from . import task_store

    return [
        risk for risk in risks
        if not (
            isinstance(risk, dict)
            and task_store.task_closed_by_disposal(repo, str(risk.get("task_id") or ""))
        )
    ]


def _identity_gone(pid: Any, ticks: Any) -> bool:
    """True only on positive evidence: an exact pid/start-ticks that no longer matches."""
    from . import process_launcher as pl  # lazy: process_launcher imports this module

    return (
        type(pid) is int and pid > 0 and type(ticks) is int
        and not pl._pid_matches(pid, ticks)
    )


def _finalizer_identity(status_now: Any) -> tuple[int, int] | None:
    from . import process_launcher as pl  # lazy: process_launcher imports this module

    if not isinstance(status_now, dict):  # unreadable/None status names no identity
        return None
    section = status_now.get(pl.FINALIZER_STATUS_SECTION)
    if not isinstance(section, dict):
        return None
    pid, ticks = section.get("pid"), section.get("pid_start_ticks")
    if type(pid) is not int or pid <= 0 or type(ticks) is not int:
        return None
    return pid, ticks


def dead_finalizer_reason(latest: dict[str, Any], *, now_epoch: float | None = None) -> str:
    """Why a ``finalizing`` row can no longer settle itself, else ``""``.

    Positive evidence only; anything unreadable or ambiguous answers ``""``.
    The worker supervisor and its child must both be provably gone (exact
    pid/start-ticks no longer matching). Then:

    * the finalizer section names an exact identity that no longer matches:
      ``finalize_failed:finalizer_dead`` -- however fresh its heartbeat
      timestamp, because a beat written by a process that has since died says
      nothing about the present;
    * the finalizer is alive but its validating phase has outlived every
      enforced validation timeout: ``validation_failed:validation_hung``.

    A hung-but-alive validation command is handled by NF-2026-02269.
    """
    from . import process_launcher as pl  # lazy: process_launcher imports this module
    from .worker_workspace import MAX_VALIDATION_SECONDS

    raw_path = str(latest.get("supervisor_status_path") or "")
    if latest.get("state") != "finalizing" or raw_path in {"", "."}:
        return ""
    status_now = pl.read_supervisor_status(Path(raw_path))
    if not status_now or str(status_now.get("state") or "") in {"starting", "running"}:
        return ""
    if not _identity_gone(latest.get("pid"), latest.get("pid_start_ticks")):
        return ""
    child_pid = status_now.get("child_pid")
    if child_pid not in (None, 0, "") and not _identity_gone(
        child_pid, status_now.get("child_pid_start_ticks")
    ):
        return ""
    identity = _finalizer_identity(status_now)
    if identity is None:
        return ""
    if not pl._pid_matches(*identity):
        return FINALIZER_DEAD_REASON
    section = status_now[pl.FINALIZER_STATUS_SECTION]
    now = time.time() if now_epoch is None else now_epoch
    beat, elapsed, total = (
        section.get(key) for key in ("heartbeat_at_epoch", "elapsed_seconds", "total")
    )
    if (
        section.get("phase") != pl.FINALIZER_VALIDATING_PHASE
        or type(beat) not in (int, float)
        or type(elapsed) not in (int, float)
        or type(total) is not int
        or not 0 <= elapsed <= pl.FINALIZER_PROGRESS_MAX_SECONDS
        or not 0 <= total <= pl.FINALIZER_PROGRESS_MAX_TOTAL
    ):
        return ""
    # ``beat - elapsed`` is the phase start: stable while a hung finalizer's
    # heartbeat thread keeps beating, so a fresh beat cannot extend it.
    deadline = (
        float(beat) - float(elapsed)
        + max(1, total) * MAX_VALIDATION_SECONDS
        + VALIDATION_DEADLINE_GRACE_SECONDS
    )
    return VALIDATION_HUNG_REASON if now > deadline else ""


def _pending_latest(manager: Any, request_id: str) -> dict[str, Any]:
    from . import process_launcher as pl  # lazy: process_launcher imports this module

    events = manager._request_events(request_id)
    if not events:
        return {}
    return {**manager._event_identity(events), **pl.split_lifecycle_tail(events)[0]}


def end_if_finalizer_dead(manager: Any, request_id: str) -> dict[str, Any] | None:
    """End a ``finalizing`` row whose finalizer cannot settle it, else ``None``.

    A dead finalizer released the request lock with its process, so its fence
    is decided under that lock (taken non-blocking; contention means a live
    finalizer owns it and nothing is changed). The lock is then released and
    the candidate the dead finalizer left is re-finalized through the same
    retry path ``agent_retry_finalization`` uses, under a fresh claim epoch:
    a sealed diff that validates reaches review, anything else fails. A hung
    finalizer still holds the lock, so ``validation_hung`` is decided without
    it -- the fence written first makes every later write of that finalizer
    a no-op -- and recorded ``finalize_failed``.
    """
    from . import process_launcher as pl  # lazy: process_launcher imports this module

    latest = _pending_latest(manager, request_id)
    reason = dead_finalizer_reason(latest) if latest else ""
    if not reason:
        return None
    if reason == VALIDATION_HUNG_REASON:
        return _end_finalization(manager, request_id, latest, reason)
    try:
        with manager._request_lock(request_id, blocking=False):
            latest = _pending_latest(manager, request_id)
            reason = dead_finalizer_reason(latest) if latest else ""
            if reason != FINALIZER_DEAD_REASON:
                return None if not reason else _end_finalization(manager, request_id, latest, reason)
            fence_finalizer(manager, request_id, reason, latest)
    except (pl.AdvisoryLockTimeout, BlockingIOError):
        return None
    return _refinalize_dead_finalizer(manager, request_id, latest, reason)


def _refinalize_dead_finalizer(
    manager: Any, request_id: str, latest: dict[str, Any], reason: str
) -> dict[str, Any]:
    """Re-run the retry-finalization path for a fenced dead finalizer.

    Only a refusal (the retry was never admitted) falls back to
    ``finalize_failed``; an admitted retry settles the card itself.
    """
    from . import process_launcher as pl  # lazy: process_launcher imports this module

    task_id = str(latest.get("task_id") or "")
    result = manager.retry_finalization(request_id, task_id) if task_id else {
        "ok": False, "error": "request_identity_missing",
    }
    if result.get("ok") or result.get("already_in_flight"):
        return {**result, "finalization_end_reason": reason}
    events = manager._request_events(request_id)
    tail = pl.split_lifecycle_tail(events)[0] if events else {}
    if tail.get("state") in pl.TERMINAL_PROCESS_STATES:
        return tail
    return _record_end(manager, request_id, latest, events, reason, {
        "finalization_retry_refused": str(result.get("error") or "")[:300],
    })


def _end_finalization(
    manager: Any, request_id: str, latest: dict[str, Any], reason: str
) -> dict[str, Any]:
    from . import process_launcher as pl  # lazy: process_launcher imports this module

    fence_finalizer(manager, request_id, reason, latest)
    events = manager._request_events(request_id)
    tail = pl.split_lifecycle_tail(events)[0]
    if tail.get("state") in pl.TERMINAL_PROCESS_STATES:
        return tail  # the finalizer settled between the verdict and the fence
    return _record_end(manager, request_id, latest, events, reason, {})


def _record_end(
    manager: Any,
    request_id: str,
    latest: dict[str, Any],
    events: list[dict[str, Any]],
    reason: str,
    extra: dict[str, Any],
) -> dict[str, Any]:
    code = latest.get("exit_code")
    return record_finalizer_failed(
        manager,
        request_id,
        code if type(code) is int else None,
        {**manager._event_identity(events), **extra, "finalization_end_reason": reason},
        reason,
        0,
        time.monotonic(),
    )


def _cancel_death_evidence(latest: dict[str, Any]) -> tuple[bool, int | None]:
    """``(provably_dead, live_finalizer_pid)`` for a pending request.

    Positive evidence only: either ``dead_finalizer_reason`` names the end,
    or the supervisor is provably gone, the recorded child (if any) is
    provably gone and the finalizer section is absent or provably gone.
    Unreadable/None status, a missing path, a live or unknown supervisor,
    child or finalizer all answer "not dead".
    """
    from . import process_launcher as pl  # lazy: process_launcher imports this module

    raw_path = str(latest.get("supervisor_status_path") or "")
    if raw_path in {"", "."}:
        return False, None
    status_now = pl.read_supervisor_status(Path(raw_path))
    if not isinstance(status_now, dict) or not status_now:
        return False, None
    identity = _finalizer_identity(status_now)
    if identity is not None and pl._pid_matches(*identity):
        return False, identity[0]
    if dead_finalizer_reason(latest):
        return True, None
    if str(status_now.get("state") or "") in {"starting", "running"}:
        return False, None
    if not _identity_gone(latest.get("pid"), latest.get("pid_start_ticks")):
        return False, None
    child_pid = status_now.get("child_pid")
    if child_pid not in (None, 0, "") and not _identity_gone(
        child_pid, status_now.get("child_pid_start_ticks")
    ):
        return False, None
    if pl.FINALIZER_STATUS_SECTION in status_now and identity is None:
        return False, None  # a present but unreadable section is unknown
    return True, None


def cancel_pending_finalization(manager: Any, request_id: str, reason: str) -> dict[str, Any]:
    """``agent_cancel_task`` for a request in ``FINALIZATION_PENDING_STATES``.

    Proceeds only on positive death evidence (``_cancel_death_evidence``);
    a provably alive finalizer keeps the refusal, naming its pid, and every
    unknown keeps it too. The request lock -- released by its holder's death
    -- is then taken non-blocking; held means a finalizer still owns the
    writer boundary and the request is unchanged. Under it the request is
    fenced, its card released as ``cancelled`` and the cancel recorded. A
    failed release is reported ``ok=False``, never ``cancelled``.
    """
    from . import process_launcher as pl  # lazy: process_launcher imports this module

    def refusal(state: Any, finalizer_pid: int | None) -> dict[str, Any]:
        return {
            "ok": False,
            "request_id": request_id,
            "state": state,
            "blocked_reason": "finalization_pending",
            "finalizer_alive": finalizer_pid is not None,
            "finalizer_pid": finalizer_pid,
        }

    latest = _pending_latest(manager, request_id)
    dead, alive_pid = _cancel_death_evidence(latest) if latest else (False, None)
    if not dead:
        return refusal(latest.get("state"), alive_pid)
    try:
        with manager._request_lock(request_id, blocking=False):
            latest = _pending_latest(manager, request_id)
            state = latest.get("state")
            if state in pl.TERMINAL_PROCESS_STATES:
                return {"ok": True, "request_id": request_id, "state": state, "idempotent_noop": True}
            dead, alive_pid = _cancel_death_evidence(latest)
            if not dead or state not in pl.FINALIZATION_PENDING_STATES:
                return refusal(state, alive_pid)
            cancel_reason = f"finalizer_dead_cancel:{reason}"[:300]
            task_id = str(latest.get("task_id") or "")
            runner = str(latest.get("runner") or "")
            if not task_id or not runner:
                return {"ok": False, "request_id": request_id, "state": state,
                        "release_transition_ok": False, "stderr": "request_identity_missing"}
            fence_finalizer(manager, request_id, cancel_reason, latest)
            release = pl.task_engine.mark_terminal_failure(
                manager.repo, task_id, runner, "cancelled",
                evidence={"request_id": request_id, "reason": cancel_reason,
                          "cancelled_from_state": state},
                request_id=request_id,
            )
            if not isinstance(release, dict) or not release.get("ok"):
                release = release if isinstance(release, dict) else {}
                return {
                    "ok": False,
                    "request_id": request_id,
                    "state": state,
                    "finalizer_fenced": True,
                    "release_transition_ok": False,
                    "stderr": str(
                        release.get("stderr") or release.get("stdout") or "release_transition_failed"
                    )[:500],
                }
            event = manager._retention_event({
                **manager._event_identity(manager._request_events(request_id)),
                "request_id": request_id,
                "state": "cancelled",
                "reason": cancel_reason,
                "cancelled_from_state": state,
                "finished_at": pl._utcnow(),
                "finalizer_fenced": True,
                "release_transition_ok": True,
                "callback_enqueued": bool(release.get("callback_enqueued")),
                **pl.terminal_failure_classification.terminal_event_authority(
                    state="cancelled", exit_code=None, error=cancel_reason
                ),
            }, disposition="retained_in_place")
            return {
                "ok": True,
                "request_id": request_id,
                "state": event.get("state"),
                "cancelled_from_state": state,
                "finalizer_fenced": True,
                "release_transition_ok": True,
            }
    except (pl.AdvisoryLockTimeout, BlockingIOError):
        return refusal(latest.get("state"), None)


def record_finalizer_failed(
    manager: Any,
    request_id: str,
    supervisor_returncode: int | None,
    event_identity: dict[str, Any],
    error: str,
    finalize_attempts: int,
    finalization_started: float,
) -> dict[str, Any]:
    """Decide a finalize that cannot settle itself as ``finalize_failed``.

    Moves the card through ``mark_terminal_failure`` (which enqueues the
    manager callback) and records the matching retained terminal event.
    Shared by the exhausted ``ProcessManager._finalize_after_process_exit``
    and the detached finalization retry (``settle_admitted``).
    """
    from . import process_launcher as pl  # lazy: process_launcher imports this module

    if active_finalizer_fenced(manager, request_id):
        # NF-2026-02275: a fenced (cancelled or ended) finalizer never moves the card.
        return {**event_identity, "request_id": request_id, "finalizer_fenced": True, "recorded": False}
    events = manager._request_events(request_id)
    tail = pl.split_lifecycle_tail(events)[0]
    if tail.get("state") == "review_ready" or (
        tail.get("state") == "accepted" and tail.get("accepted") is True
    ):
        # NF-2026-01680: a request that already SUCCEEDED (e.g. an accepted
        # reviewer) keeps that decision; a late exhausted finalize appends no
        # terminal row over it and moves no card, so no callback is enqueued
        # either. Any other tail (exited, failed, ...) still moves the card.
        return {
            **tail,
            "request_id": request_id,
            "skipped_decided_tail": True,
            "recorded": False,
        }
    task_id = str(event_identity.get("task_id") or "")
    runner = str(event_identity.get("runner") or "")
    release_result: dict[str, Any] = {
        "ok": False,
        "stderr": "request_identity_missing",
        "callback_enqueued": False,
    }
    if task_id and runner:
        release_result = pl.task_engine.mark_terminal_failure(
            manager.repo,
            task_id,
            runner,
            "finalize_failed",
            evidence={
                "request_id": request_id,
                "error": error[:500],
                "supervisor_returncode": supervisor_returncode,
                "finalize_attempts": finalize_attempts,
            },
            request_id=request_id,
        )
    transition_reason = str(
        release_result.get("stderr") or release_result.get("stdout") or ""
    )
    # "retries exhausted" must mean the attempt is over, not that the next
    # reconcile round begins. When the terminal transition fails because the
    # target card is no longer processing -- archived, deleted, or reclaimed
    # -- no future reconcile can move it, so terminalize as
    # ``finalize_abandoned`` (a terminal, non-re-arming state) with a named
    # cause an operator can count. Only a card that may still be processing
    # keeps ``reconcile_pending`` so its legitimate retry survives.
    abandon_cause = (
        None
        if release_result.get("ok")
        else pl._finalizer_card_not_processing(transition_reason)
    )
    if release_result.get("ok"):
        terminal_state = "finalize_failed"
        error_detail = error
        pl.unlink_if_regular(manager._terminal_authority_grant_path(request_id))
    elif abandon_cause is not None:
        terminal_state = "finalize_abandoned"
        error_detail = (
            error + ":finalize_abandoned:" + abandon_cause + ":" + transition_reason
        )
    else:
        terminal_state = "reconcile_pending"
        error_detail = error + ":terminal_transition_failed:" + transition_reason
    return manager._retention_event({
        **event_identity,
        "request_id": request_id,
        "state": terminal_state,
        "worker_terminal_state": (
            "finalize_abandoned"
            if terminal_state == "finalize_abandoned"
            else "finalize_failed"
        ),
        "exit_code": supervisor_returncode,
        "finished_at": pl._utcnow(),
        "finalizer_abandoned": terminal_state == "finalize_abandoned",
        "release_transition_ok": bool(release_result.get("ok")),
        "callback_enqueued": bool(release_result.get("callback_enqueued")),
        "finalization_duration_ms": round(
            (time.monotonic() - finalization_started) * 1000.0, 3
        ),
        **pl.terminal_failure_classification.terminal_event_authority(
            state=terminal_state, exit_code=supervisor_returncode, error=error_detail[:500]
        ),
    }, disposition="retained_in_place")


def settle_admitted(manager: Any, request_id: str, task_id: str) -> dict[str, Any]:
    """Run an admitted retry's finalize and end its ``finalizing`` row truthfully.

    Past admission the finalize is detached, so a raise or a run with no
    terminal event must still end the row truthfully. It is decided
    ``finalize_failed`` (manager callback enqueued) by the same recorder
    ``_finalize_after_process_exit`` uses once exhausted. That wrapper itself
    is not reused: it re-runs the finalize and carries out the auto-return that
    ``retry_finalization`` already owns. Its deferral signals are honoured
    exactly as it honours them: a contended request lock, an unknown pid
    identity or a pending bridge cancellation proves nothing about the
    candidate, so the row is left for the reconciler and never terminalized.
    """
    from . import process_launcher as pl  # lazy: process_launcher imports this module

    finalization_started = time.monotonic()
    deferred_reason = ""
    try:
        event = manager._finalize_isolated_request(request_id, 0)
        failure = "" if event is not None else "finalization_retry_no_terminal_event"
    except pl._BridgeCancellationDeferred:
        event, failure, deferred_reason = None, "", "bridge_cancellation_pending"
    except pl._PidIdentityUnknownDeferred:
        event, failure, deferred_reason = None, "", "pid_identity_unknown"
    except pl.AdvisoryLockTimeout:
        event, failure, deferred_reason = None, "", "request_lock_busy"
    except Exception as exc:  # noqa: BLE001 -- the detached finalize must settle
        event = None
        failure = (
            f"finalization_retry_crashed:{type(exc).__name__}:"
            f"{pl.exception_text_with_site(exc)}"
        )
    if deferred_reason:
        deferred_events = manager._request_events(request_id)
        return {
            "ok": False,
            "request_id": request_id,
            "task_id": task_id,
            "state": (
                str(pl.split_lifecycle_tail(deferred_events)[0].get("state") or "")
                if deferred_events
                else ""
            ),
            "reconciliation_deferred": deferred_reason,
            "provider_relaunched": False,
            "workspace_retained": True,
            "error": f"finalization_retry_deferred:{deferred_reason}",
        }
    if event is None:
        settled = manager._request_events(request_id)
        if not settled or str(pl.split_lifecycle_tail(settled)[0].get("state") or "") != "finalizing":
            return {
                "ok": False,
                "request_id": request_id,
                "task_id": task_id,
                "error": failure[:_ERROR_MAX_CHARS],
            }
        event = record_finalizer_failed(
            manager, request_id, 0, manager._event_identity(settled), failure, 1, finalization_started
        )
    return {
        "ok": str(event.get("state") or "") == "review_ready",
        "request_id": request_id,
        "task_id": task_id,
        "state": event.get("state"),
        "provider_relaunched": False,
        "workspace_retained": event.get("workspace_retained"),
        "error": event.get("error") or failure[:_ERROR_MAX_CHARS],
    }
