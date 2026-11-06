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

import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable

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
