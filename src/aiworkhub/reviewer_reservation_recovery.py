"""Fail-closed recovery classification for unbound reviewer reservations."""

from __future__ import annotations

import time
import json
import sqlite3
import stat
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Protocol, cast

from . import core, task_store
from .platform_io import AdvisoryLockTimeout


ClaimResolution = tuple[str, int | None]


class ReviewerReservationRecoveryManager(Protocol):
    """Launcher-owned operations required by reviewer reservation recovery."""

    @property
    def repo(self) -> Path: ...
    @property
    def process_dir(self) -> Path: ...
    @property
    def _lock(self) -> AbstractContextManager[Any]: ...
    @property
    def _TERMINAL_INTENT_DIAGNOSTICS_PER_PASS(self) -> int: ...
    @property
    def _REVIEWER_TERMINAL_INTENT_SUFFIX(self) -> str: ...
    @property
    def _REVIEWER_TERMINAL_INTENT_SUBSTATUS(self) -> str: ...
    @property
    def _DURABLE_TERMINAL_INTENT_DISPOSITIONS(self) -> frozenset[str]: ...

    def _registry_lock(self) -> AbstractContextManager[Any]: ...
    def _request_lock(
        self, request_id: str, *, blocking: bool = True
    ) -> AbstractContextManager[Any]: ...
    def _append_event(self, event: dict[str, Any]) -> dict[str, Any]: ...
    def _latest_by_request_stable(
        self,
    ) -> tuple[dict[str, dict[str, Any]], tuple[Any, ...] | None]: ...
    def _reviewer_terminal_intent_path(self, request_id: str) -> Path: ...
    def _resolved_reservation_snapshot(
        self,
        snapshot: tuple[dict[str, dict[str, Any]], tuple[Any, ...] | None] | None,
    ) -> tuple[dict[str, dict[str, Any]], tuple[Any, ...] | None]: ...
    def _diagnose_identity_incomplete_reservation(
        self,
        request_id: str,
        event: Mapping[str, Any],
        blocked_reason: str,
        remaining: int,
    ) -> tuple[bool, int]: ...
    def _terminal_intent_is_resolved(self, state: str) -> bool: ...
    def _terminal_intent_diagnosed_marker(self, path: Path) -> Path: ...
    def _terminal_intent_retired_marker(self, path: Path) -> Path: ...
    def _read_regular_intent(self, path: Path) -> tuple[str, Any]: ...
    def _diagnose_unsettleable_intent(
        self, path: Path, reason: str, remaining: int
    ) -> tuple[bool, int]: ...
    def _diagnose_retired_intent(
        self, path: Path, state: str, remaining: int
    ) -> tuple[bool, int]: ...
    def _diagnose_unroutable_callback(
        self, path: Path, reason: str, remaining: int
    ) -> tuple[bool, int]: ...
    def _reviewer_source_graph_prewarm_live_event(
        self, event: Mapping[str, Any]
    ) -> bool: ...
    def _resolve_unbound_reviewer_claims(
        self, candidate_latest: dict[str, dict[str, Any]]
    ) -> dict[str, ClaimResolution]: ...
    def _record_reviewer_terminal_intent(
        self, request_id: str, event: Mapping[str, Any], blocked_reason: str
    ) -> str: ...
    def _terminalize_committed_reservation(
        self,
        request_id: str,
        event: Mapping[str, Any],
        blocked_reason: str,
        diagnostics_left: int,
    ) -> tuple[bool, int]: ...
    def _reconcile_expired_starting_reservations(
        self,
        snapshot: tuple[dict[str, dict[str, Any]], tuple[Any, ...] | None] | None,
        *,
        resolved: bool = False,
        _admission_recovery_authority: object | None = None,
        _unbound_claim_resolutions: dict[str, ClaimResolution] | None = None,
    ) -> int: ...


def resolve_unbound_reviewer_claims(
    candidate_latest: Mapping[str, Mapping[str, Any]],
    *,
    load_task: Callable[[str], object],
    is_safe_int: Callable[[object], bool],
    now: float | None = None,
) -> dict[str, ClaimResolution]:
    """Classify expired, unbound reviewer claims using canonical task state.

    Missing, malformed, mismatched, or unavailable state remains unresolved so
    callers preserve the reservation.  This module classifies authority only;
    it neither persists task state nor evaluates process liveness.
    """

    resolutions: dict[str, ClaimResolution] = {}
    resolution_now = time.time() if now is None else now
    for request_id, event in candidate_latest.items():
        if (
            event.get("state") != "starting"
            or event.get("topic") != "quality_review"
            or event.get("reviewer_claim_epoch") is not None
        ):
            continue
        try:
            deadline = float(event.get("reservation_expires_at_epoch"))
        except (TypeError, ValueError):
            continue
        if not (0.0 < deadline < float("inf") and deadline < resolution_now):
            continue
        task_id = str(event.get("task_id") or "").strip()
        runner = str(event.get("runner") or "").strip()
        if not task_id or not runner:
            continue
        try:
            card = load_task(task_id)
        except Exception:  # noqa: BLE001 -- store ambiguity fails closed
            continue
        if card is None:
            resolutions[request_id] = ("legacy", None)
            continue
        if not isinstance(card, dict):
            continue
        status = str(card.get("status") or "").strip().lower()
        worker_status = str(card.get("worker_status") or "").strip().lower()
        claimed_by = str(card.get("claimed_by") or "").strip()
        launch_request_id = str(card.get("launch_request_id") or "").strip()
        if status == "pending" and worker_status == "unclaimed" and not claimed_by:
            resolutions[request_id] = ("legacy", None)
            continue
        claim_epoch = card.get("claim_epoch")
        if (
            status == "processing"
            and worker_status == "claimed"
            and claimed_by == runner
            and launch_request_id == request_id
            and is_safe_int(claim_epoch)
            and int(claim_epoch) > 0
        ):
            resolutions[request_id] = ("exact", int(claim_epoch))
    return resolutions


def settle_reviewer_terminal_intents(
    manager: ReviewerReservationRecoveryManager,
    *,
    schema_id: str,
    is_safe_int: Callable[[object], bool],
    unlink_regular: Callable[[Any], None],
) -> int:
    """Complete every durable reviewer terminal intent exactly once.

    This is the half of reconciliation that touches SQLite, so it runs with
    no registry lock held.  Each intent is settled under its own request
    lock and bound to the exact task/request/claim epoch it recorded: a
    card that was released and re-claimed since fails closed and is never
    terminalized.  The intent is removed only once the transition AND the
    one callback it owes are both settled, so a transient store failure --
    a locked store above all -- retries instead of stranding a processing
    card or silently swallowing the callback.

    Every store failure is contained per intent.  One contended card must
    not abort settlement of the unrelated intents sitting beside it on
    disk, which is the only reason they are all reached from one pass.

    An intent that can never be bound at all is neither acted on nor
    deleted; it is reported once to the diagnostic ledger so the stranded
    card is visible to an operator rather than silent.  An intent that is
    bindable but meets a final refusal really is retired -- no pass could
    move it -- but never before that refusal is on the same record.
    """

    settled = 0
    diagnostics_left = manager._TERMINAL_INTENT_DIAGNOSTICS_PER_PASS
    try:
        intents = sorted(
            manager.process_dir.glob("*" + manager._REVIEWER_TERMINAL_INTENT_SUFFIX)
        )
    except OSError:
        return 0
    for path in intents:
        try:
            observed = path.lstat()
            if not stat.S_ISREG(observed.st_mode):
                _recorded, diagnostics_left = manager._diagnose_unsettleable_intent(
                    path, "path_identity_mismatch", diagnostics_left
                )
                continue
            raw, opened = manager._read_regular_intent(path)
            if (opened.st_dev, opened.st_ino) != (observed.st_dev, observed.st_ino):
                _recorded, diagnostics_left = manager._diagnose_unsettleable_intent(
                    path, "path_identity_mismatch", diagnostics_left
                )
                continue
        except FileNotFoundError:
            # The settler that won this intent retired it between the glob
            # and this read.  That is the benign race the whole design
            # expects, and it is not evidence that anything is unusable.
            continue
        except OSError:
            # The intent is still THERE and its bytes cannot be read, so
            # no pass will ever settle it while that lasts.  It is kept --
            # never acted on, never deleted -- and named once, because
            # treating it as the retired-by-the-winner case above would
            # strand the card in silence for as long as the condition
            # holds.
            _recorded, diagnostics_left = manager._diagnose_unsettleable_intent(
                path, "unreadable_bytes", diagnostics_left
            )
            continue
        try:
            payload = json.loads(raw)
        except ValueError:
            _recorded, diagnostics_left = manager._diagnose_unsettleable_intent(
                path, "unparseable_json", diagnostics_left
            )
            continue
        if (
            not isinstance(payload, dict)
            or payload.get("schema_id") != schema_id
        ):
            _recorded, diagnostics_left = manager._diagnose_unsettleable_intent(
                path, "foreign_schema", diagnostics_left
            )
            continue
        request_id = str(payload.get("request_id") or "")
        task_id = str(payload.get("task_id") or "")
        runner = str(payload.get("runner") or "")
        claim_epoch = payload.get("reviewer_claim_epoch")
        if (
            not request_id
            or not task_id
            or not runner
            or not is_safe_int(claim_epoch)
            or int(cast(int, claim_epoch)) < 1
        ):
            # An intent we cannot bind to an exact identity is never acted
            # on and never deleted: it stays as evidence for an operator,
            # and the diagnostic ledger says so out loud exactly once.
            _recorded, diagnostics_left = manager._diagnose_unsettleable_intent(
                path, "identity_unbindable", diagnostics_left
            )
            continue
        if path != manager._reviewer_terminal_intent_path(request_id):
            _recorded, diagnostics_left = manager._diagnose_unsettleable_intent(
                path, "path_identity_mismatch", diagnostics_left
            )
            continue
        substatus = str(
            payload.get("substatus") or manager._REVIEWER_TERMINAL_INTENT_SUBSTATUS
        ).strip()
        if substatus not in task_store.MARK_TERMINAL_FAILURE_SUBSTATUSES:
            # The store would refuse this with a final
            # ``unsupported_terminal_failure`` and the ticket would be
            # retired having moved nothing.  A substatus outside the
            # store's vocabulary is a malformed intent, so it is validated
            # here and takes the same never-acted-on, never-deleted path as
            # an unbindable identity rather than reaching SQLite at all.
            # It earns its OWN reason, though: the identity here bound
            # perfectly and only the declared substatus is unusable, so
            # reporting it as ``identity_unbindable`` would send an
            # operator to repair task/request/claim-epoch fields that are
            # already correct.
            _recorded, diagnostics_left = manager._diagnose_unsettleable_intent(
                path, "substatus_unsupported", diagnostics_left
            )
            continue
        owed = False
        try:
            with manager._request_lock(request_id, blocking=False):
                # The intent file IS the claim ticket, and it is re-read,
                # settled and retired inside this lock.  A settler that
                # queued behind the winner therefore finds the ticket gone
                # and transitions nothing, instead of leaning on the
                # store's CAS to absorb a second attempt.
                try:
                    held_raw, locked_observed = manager._read_regular_intent(path)
                    held = json.loads(held_raw)
                except (OSError, ValueError):
                    continue
                if (
                    held != payload
                    or path != manager._reviewer_terminal_intent_path(request_id)
                    or not stat.S_ISREG(locked_observed.st_mode)
                    or (locked_observed.st_dev, locked_observed.st_ino)
                    != (observed.st_dev, observed.st_ino)
                ):
                    _recorded, diagnostics_left = (
                        manager._diagnose_unsettleable_intent(
                            path, "path_identity_mismatch", diagnostics_left
                        )
                    )
                    continue
                ok, state = task_store.mark_terminal_failure(
                    manager.repo,
                    task_id,
                    runner=runner,
                    substatus=substatus,
                    evidence={
                        "request_id": request_id,
                        "error": str(payload.get("blocked_reason") or ""),
                        "reviewer_claim_epoch": claim_epoch,
                    },
                    request_id=request_id,
                    claim_epoch=claim_epoch,
                )
                if not ok and not manager._terminal_intent_is_resolved(str(state)):
                    # A transient CAS loss a later pass can still win, so
                    # the ticket is kept rather than the transition dropped.
                    continue
                # A final refusal can also mean this intent's OWN
                # transition already landed and only its callback is still
                # owed -- exactly what a crash, or a failed retire below,
                # leaves behind.  The store proves that from the card it
                # wrote rather than the launcher inferring it from a
                # refusal string.  Only lease expiry requests card recovery;
                # every legacy terminal-intent reason retains this exact
                # settlement path without an extra TaskStore dependency.
                blocked_reason = str(payload.get("blocked_reason") or "")
                recovery_requested = (
                    blocked_reason == "reservation_expired"
                    or blocked_reason == "reservation_process_false"
                    or blocked_reason.startswith(
                        "preparation_heartbeat_stalled:"
                    )
                )
                current_card: dict[str, Any] | None = None
                retry_already_applied = False
                if recovery_requested:
                    current_card = task_store.get_task(manager.repo, task_id)
                    prior_retry = (
                        current_card.get("terminal_retry")
                        if isinstance(current_card, dict)
                        else None
                    )
                    retry_already_applied = (
                        isinstance(current_card, dict)
                        and core._lifecycle_state(current_card) == "pending"
                        and current_card.get("worker_status") == "unclaimed"
                        and isinstance(prior_retry, dict)
                        and prior_retry.get("request_id") == request_id
                        and prior_retry.get("terminal_substatus") == substatus
                    )
                owed = (
                    ok
                    or retry_already_applied
                    or task_store.terminal_failure_already_applied(
                        manager.repo,
                        task_id,
                        str(state),
                        runner=runner,
                        substatus=substatus,
                        request_id=request_id,
                        claim_epoch=claim_epoch,
                    )
                )
                if not owed:
                    # A final refusal that moved no card at all.  The
                    # ticket is genuinely spent, but it may only be retired
                    # once the refusal is durable evidence; when the line
                    # cannot be written the intent is retained so a later
                    # pass reports and retires it instead of it vanishing.
                    recorded, diagnostics_left = manager._diagnose_retired_intent(
                        path, str(state), diagnostics_left
                    )
                    if not recorded:
                        continue
                # The store owns the callback vocabulary, the episode
                # binding and the containment of its own unavailability, so
                # this is the single post-transition callback authority
                # rather than a launcher-private copy of the callback
                # database, and ``claim_epoch`` names the episode the
                # transition actually moved.  It runs BEFORE the ticket is
                # retired: the intent is the only thing that brings a later
                # pass back to this claim, so retiring first would lose the
                # callback whenever the enqueue could not be written.
                if owed and not task_store.enqueue_terminal_callback(
                    manager.repo,
                    task_id,
                    substatus=substatus,
                    request_id=request_id,
                    claim_epoch=claim_epoch,
                ) and not task_store.terminal_callback_already_durable(
                    manager.repo,
                    task_id,
                    substatus=substatus,
                    request_id=request_id,
                    claim_epoch=claim_epoch,
                ):
                    # A refused enqueue is three different worlds, and only
                    # the store can tell them apart.  "Not written yet" --
                    # a locked or unreadable store -- keeps the ticket and
                    # retries.  "Already durable" is what a crash between
                    # a successful enqueue and this retire leaves behind;
                    # every later pass would see the same duplicate
                    # refusal, so reading it as "not written yet" would
                    # strand this intent, and the processing claim behind
                    # it, forever.  The proof is authenticated against the
                    # exact task/request/episode/route this intent binds,
                    # so an unknown or mismatched row never retires it.
                    unroutable = (
                        task_store.terminal_callback_identity_unroutable(
                            manager.repo, task_id, substatus=substatus,
                        )
                    )
                    if not unroutable:
                        continue
                    # The third world: the transition IS durable and the
                    # callback identity is one the store will never route,
                    # so no pass could ever write the row and no proof
                    # could ever appear.  Retiring it silently would hide a
                    # manager wake that is genuinely lost, so the truthful
                    # disposition goes on the record first and the ticket
                    # is kept whenever that line cannot be written.
                    recorded, diagnostics_left = (
                        manager._diagnose_unroutable_callback(
                            path, unroutable, diagnostics_left
                        )
                    )
                    if not recorded:
                        continue
                if (
                    recovery_requested
                    and owed
                    and isinstance(current_card, dict)
                    and not retry_already_applied
                ):
                    # Reaper failures are operational launch episodes, not
                    # actionable review evidence. With their exact failure
                    # and callback durable above, route the same request
                    # through canonical retry so the card is claimable.
                    with core._REPOSITORY_SWITCH_LOCK:
                        prior_repo_override = core._PROCESS_REPO_ROOT_OVERRIDE
                        core._PROCESS_REPO_ROOT_OVERRIDE = manager.repo
                        try:
                            retry = core.retry_terminal_task(
                                task_id,
                                request_id,
                                substatus,
                                reason=str(payload.get("blocked_reason") or ""),
                                topic="quality_review",
                            )
                        finally:
                            core._PROCESS_REPO_ROOT_OVERRIDE = prior_repo_override
                    if not retry.get("ok"):
                        # The terminal intent remains the resumable owner.
                        # A later pass observes the already-applied failure
                        # and callback, then retries only this exact episode.
                        continue
                unlink_regular(path)
                # A repaired intent may carry a marker from when it was
                # still unbindable, and a retired one carries the marker
                # that claimed its refusal line; retiring the ticket
                # retires both rather than leaving either behind.
                unlink_regular(manager._terminal_intent_diagnosed_marker(path))
                unlink_regular(manager._terminal_intent_retired_marker(path))
        except (
            AdvisoryLockTimeout,
            OSError,
            sqlite3.Error,
            task_store.TaskStoreError,
        ):
            # Another settler owns this request, or the store is briefly
            # unavailable or locked.  All retry on the next pass, and
            # containing them per intent is what keeps one contended card
            # from aborting settlement of every other intent on disk.
            continue
        if owed:
            settled += 1
    return settled


def record_reviewer_terminal_intent(
    manager: ReviewerReservationRecoveryManager,
    request_id: str,
    event: Mapping[str, Any],
    blocked_reason: str,
    *,
    schema_id: str,
    is_safe_int: Callable[[object], bool],
    utcnow: Callable[[], str],
    write_json: Callable[[Any, Mapping[str, Any]], None],
) -> str:
    """Durably declare a terminal transition before performing it.

    The process ledger and the task store are two independent durable
    stores and the registry lock deliberately does not span the second one.
    Without a recorded intent, a crash between them strands the reviewer
    card in ``processing`` with no evidence of what was meant to happen.
    The intent names the exact task, request and claim epoch, so a later
    pass finishes *that* transition instead of inventing a new one.

    An intent is only recorded when the reservation carries the complete
    identity: an incomplete identity fails closed and is never settled.
    """

    task_id = str(event.get("task_id") or "").strip()
    runner = str(event.get("runner") or "").strip()
    claim_epoch = event.get("reviewer_claim_epoch")
    if not is_safe_int(claim_epoch):
        return "identity_incomplete"
    claim_epoch_int = int(cast(int, claim_epoch))
    if not task_id or not runner or claim_epoch_int < 1:
        return "identity_incomplete"
    path = manager._reviewer_terminal_intent_path(request_id)
    try:
        existing_raw, _opened = manager._read_regular_intent(path)
    except FileNotFoundError:
        existing_raw = None
    except (OSError, UnicodeError):
        return "record_failed"
    if existing_raw is not None:
        try:
            existing = json.loads(existing_raw)
        except (UnicodeError, json.JSONDecodeError):
            return "record_failed"
        expected = {
            "schema_id": schema_id,
            "request_id": str(request_id),
            "task_id": task_id,
            "runner": runner,
            "reviewer_claim_epoch": claim_epoch_int,
            "substatus": manager._REVIEWER_TERMINAL_INTENT_SUBSTATUS,
            "blocked_reason": blocked_reason,
        }
        if not isinstance(existing, dict) or any(
            existing.get(key) != value for key, value in expected.items()
        ):
            return "record_failed"
        if not is_safe_int(existing.get("reviewer_claim_epoch")):
            return "record_failed"
        return "already_recorded"
    try:
        write_json(path, {
            "schema_id": schema_id,
            "request_id": str(request_id),
            "task_id": task_id,
            "runner": runner,
            "reviewer_claim_epoch": claim_epoch_int,
            "substatus": manager._REVIEWER_TERMINAL_INTENT_SUBSTATUS,
            "blocked_reason": blocked_reason,
            "recorded_at": utcnow(),
        })
    except OSError:
        return "record_failed"
    return "recorded"



def terminalize_committed_reservation(
    manager: ReviewerReservationRecoveryManager,
    request_id: str,
    event: Mapping[str, Any],
    blocked_reason: str,
    diagnostics_left: int,
) -> tuple[bool, int]:
    """Terminalize one proven-dead committed reservation, intent first.

    The intent is written before the ledger event so that every point after
    this line is recoverable: a crash leaves an exact, replayable record of
    which claim still has to be released.  No task-store I/O happens here --
    the caller may hold the outer registry lock, and SQLite must never be
    reached from under it.

    Returns ``(terminalized, diagnostics_left)``.  ``terminalized`` states
    whether the reservation was terminalized in the ledger, which happens
    only once the intent is durable.  The ``blocked`` event is what
    releases the reservation: appending it with no intent on disk strands
    the reviewer card in ``processing`` with nothing left to finish the
    transition, and it erases the committed row a later pass would need in
    order to try again.  Declining to append keeps the two durable stores
    agreeing and makes a failed intent write retryable.

    ``diagnostics_left`` is the caller's per-pass diagnostic budget,
    threaded through so one pass over many unbindable rows emits a bounded
    number of lines instead of one per row.
    """

    if (
        event.get("state") == "starting"
        and event.get("topic") == "quality_review"
        and "reviewer_claim_epoch" not in event
    ):
        # Reservations written before claim epochs were persisted cannot
        # be bound to an exact task-store claim.  Preserve their original
        # ledger-only retirement semantics: the blocked row releases the
        # launch reservation, while deliberately creating no settlement
        # intent that could mutate an unrelated or newly reclaimed card.
        manager._append_event({
            "request_id": request_id,
            "task_id": event.get("task_id"),
            "runner": event.get("runner"),
            "topic": event.get("topic"),
            "adapter_id": event.get("adapter_id"),
            "state": "blocked",
            "blocked_reason": blocked_reason,
            "reservation_expires_at_epoch": event.get(
                "reservation_expires_at_epoch"
            ),
        })
        return True, diagnostics_left

    disposition = manager._record_reviewer_terminal_intent(
        request_id, event, blocked_reason
    )
    if disposition not in manager._DURABLE_TERMINAL_INTENT_DISPOSITIONS:
        if disposition == "identity_incomplete":
            # No intent was written, and appending the terminalizing event
            # without one is exactly what the docstring above forbids, so
            # this reservation moves nowhere.  Every later pass re-derives
            # the same refusal from the same unchanged row: a proven-dead
            # reservation that is permanently unterminalizable, in total
            # silence.  One bounded line keyed to this exact request and
            # identity episode is the only evidence an operator gets that
            # the card needs a hand repair.  ``record_failed`` is
            # deliberately NOT reported here -- that write is transient and
            # a later pass is expected to succeed, so naming it would turn
            # a passing retry into permanent operator noise.
            _recorded, diagnostics_left = (
                manager._diagnose_identity_incomplete_reservation(
                    request_id, event, blocked_reason, diagnostics_left
                )
            )
        return False, diagnostics_left
    manager._append_event({
        "request_id": request_id,
        "task_id": event.get("task_id"),
        "runner": event.get("runner"),
        "topic": event.get("topic"),
        "adapter_id": event.get("adapter_id"),
        "state": "blocked",
        "blocked_reason": blocked_reason,
        "reservation_expires_at_epoch": event.get(
            "reservation_expires_at_epoch"
        ),
        "reviewer_claim_epoch": event.get("reviewer_claim_epoch"),
        "terminal_intent": disposition,
    })
    return True, diagnostics_left




def reconcile_expired_starting_reservations(
    manager: ReviewerReservationRecoveryManager,
    snapshot: tuple[
        dict[str, dict[str, Any]], tuple[Any, ...] | None
    ] | None = None,
    *,
    resolved: bool = False,
    _admission_recovery_authority: object | None = None,
    _unbound_claim_resolutions: dict[str, tuple[str, int | None]] | None = None,
    now_epoch: Callable[[], float],
    parse_durable_pid: Callable[[object], tuple[int | None, bool]],
    pid_identity_evidence: Callable[[int, object], Any],
    identity_mismatch: object,
    classify_preparation_stall: Callable[..., Mapping[str, Any]],
) -> int:
    """Truthfully terminalize pid-null starting reservations that expired.

    A ``starting`` reservation with no pid belongs to a provisioner that
    never reached supervisor spawn.  Once its bounded reservation epoch
    elapses it is no longer live, so it is terminalized (``blocked`` with
    ``reservation_expired``) instead of silently expiring.  A reservation
    that already carries a real pid is only terminalized when pid identity
    evidence proves a mismatch.  A durable ``provider_spawn_committed``
    phase is exact spawn authority that outlives its owner process, so it
    is never terminalized by elapsed or quiet time: only a committed owner
    proven dead (and no provider running event) is truthfully terminalized
    once.  A live provider's liveness follows exact process evidence.  A
    pid-null ``starting`` reservation whose exact owner is still live in the
    ``reviewer_source_graph_prewarm_started`` phase is likewise deferred
    rather than terminalized by its elapsed epoch.

    ``snapshot`` is a stable ledger snapshot the caller already took with
    the registry lock RELEASED.  A caller that has not already proved it
    is routed through the registry lock here and the generation is proved
    again only after acquisition.  Thus every terminal intent/event shares
    the exact serialization boundary with spawn commit, even when this
    method is entered directly by the periodic reconciler.

    ``resolved`` states that the caller ALREADY re-proved that snapshot
    with its own sweep, inside this same lock acquisition, and shares the
    dict.  It exists so the admission CAS in ``_launch_reservation`` and
    this pass are proved by ONE sweep between them rather than two: the
    re-proof is a property of the critical section, not of either caller.
    In exchange this pass mirrors every row it terminalizes back into that
    shared dict, so the caller's copy keeps describing the ledger exactly
    without a second parse.

    This pass writes only ledger events and durable terminal intents; the
    task-store half runs in ``_settle_reviewer_terminal_intents`` with no
    registry lock held.  Diagnostics about reservations that can never be
    terminalized share one bounded per-pass budget, so a directory full of
    unbindable rows cannot turn a single pass into an unbounded burst of
    marker and ledger writes.
    """

    if not resolved:
        candidate = snapshot or manager._latest_by_request_stable()
        candidate_latest = candidate[0] if isinstance(candidate, tuple) else candidate
        unbound_claim_resolutions = manager._resolve_unbound_reviewer_claims(
            candidate_latest
        )
        with manager._lock, manager._registry_lock():
            proven = manager._resolved_reservation_snapshot(candidate)
            if proven is None:
                # A newer row may be spawn/process authority for an exact
                # request this pass was about to retire.  Ambiguity always
                # preserves that authority and retries on a later scan.
                return 0
            return manager._reconcile_expired_starting_reservations(
                proven,
                resolved=True,
                _admission_recovery_authority=_admission_recovery_authority,
                _unbound_claim_resolutions=unbound_claim_resolutions,
            )

    now = now_epoch()
    # The exact requests this pass retired, in ledger order.  It is both
    # the return count and the key set mirrored into ``latest`` below.
    # Named apart from the per-row ``terminalized`` flag the loop rebinds.
    retired: list[str] = []
    diagnostics_left = manager._TERMINAL_INTENT_DIAGNOSTICS_PER_PASS
    if resolved and snapshot is not None:
        latest, generation = snapshot
    else:
        latest, generation = manager._resolved_reservation_snapshot(snapshot)
    if generation is None:
        # The ledger changed under every bounded read attempt, so this
        # snapshot may hide a concurrent append.  Terminalizing on it could
        # contradict a row that already exists; defer to the next pass.
        return 0
    for request_id, event in latest.items():
        state = event.get("state")
        if state == "provider_spawn_committed":
            provider_pid, provider_pid_ambiguous = parse_durable_pid(
                event.get("provider_pid")
            )
            if provider_pid_ambiguous:
                continue
            try:
                provider_identity = (
                    pid_identity_evidence(
                        provider_pid, event.get("provider_pid_start_ticks")
                    ).verdict
                    if provider_pid
                    else None
                )
            except (TypeError, ValueError, OverflowError):
                # Malformed persisted identity is ambiguous. Preserve this
                # row and continue the pass so it cannot contain later rows.
                continue
            if provider_pid:
                if provider_identity is not identity_mismatch:
                    # A live provider is never terminalized by elapsed or
                    # quiet time, even when its bounded owner is gone.
                    continue
                terminalized, diagnostics_left = (
                    manager._terminalize_committed_reservation(
                        request_id,
                        event,
                        "provider_spawn_committed_provider_dead",
                        diagnostics_left,
                    )
                )
                if terminalized:
                    retired.append(request_id)
                continue
            owner_pid, owner_pid_ambiguous = parse_durable_pid(
                event.get("owner_pid")
            )
            if owner_pid_ambiguous:
                continue
            try:
                owner_identity = (
                    pid_identity_evidence(
                        owner_pid, event.get("owner_pid_start_ticks")
                    ).verdict
                    if owner_pid
                    else None
                )
            except (TypeError, ValueError, OverflowError):
                continue
            if (
                owner_pid
                and owner_identity is identity_mismatch
            ):
                terminalized, diagnostics_left = (
                    manager._terminalize_committed_reservation(
                        request_id,
                        event,
                        "provider_spawn_committed_owner_dead",
                        diagnostics_left,
                    )
                )
                if terminalized:
                    retired.append(request_id)
            continue
        if state != "starting":
            continue
        try:
            reservation_deadline = float(
                event.get("reservation_expires_at_epoch")
            )
        except (TypeError, ValueError):
            # Missing or malformed lease authority is ambiguous. Preserve
            # the reservation and contain the bad row so other candidates
            # in this pass can still be reconciled.
            continue
        if not (0.0 < reservation_deadline < float("inf")):
            # Zero, negative, NaN, and infinite deadlines cannot authorize
            # retirement. Fail closed instead of treating them as expired.
            continue
        if reservation_deadline >= now:
            # No classification, including a stalled preparation
            # heartbeat or disproved PID, can bypass the exact lease.
            continue
        if (
            event.get("topic") == "quality_review"
            and event.get("reviewer_claim_epoch") is None
        ):
            resolution = (_unbound_claim_resolutions or {}).get(request_id)
            if resolution is None:
                # Canonical state was unavailable, mismatched, newer, or
                # otherwise ambiguous when read outside the registry lock.
                continue
            resolution_kind, resolved_epoch = resolution
            if resolution_kind == "exact" and resolved_epoch is not None:
                bound = dict(event)
                bound["reviewer_claim_epoch"] = resolved_epoch
                bound["claim_binding_state"] = "reviewer_claim_bound"
                manager._append_event(bound)
                latest[request_id] = bound
                event = bound
            elif resolution_kind != "legacy":
                continue
        pid, pid_ambiguous = parse_durable_pid(event.get("pid"))
        if pid_ambiguous:
            continue
        try:
            identity = (
                pid_identity_evidence(
                    pid, event.get("pid_start_ticks")
                ).verdict
                if pid
                else None
            )
        except (TypeError, ValueError, OverflowError):
            # A malformed PID or start-tick value cannot prove absence or
            # mismatch. Preserve this row without aborting the whole pass.
            continue
        if pid:
            if identity is identity_mismatch:
                terminalized, diagnostics_left = (
                    manager._terminalize_committed_reservation(
                        request_id,
                        event,
                        "reservation_process_false",
                        diagnostics_left,
                    )
                )
                if terminalized:
                    retired.append(request_id)
            # Exact PID/start-identity mismatch independently disproves
            # the recorded owner, regardless of its nominal lease. Live
            # and ambiguous identities preserve the reservation.
            continue
        owner_pid, owner_pid_ambiguous = parse_durable_pid(
            event.get("owner_pid")
        )
        if owner_pid_ambiguous:
            continue
        try:
            owner_identity = (
                pid_identity_evidence(
                    owner_pid, event.get("owner_pid_start_ticks")
                ).verdict
                if owner_pid
                else None
            )
        except (TypeError, ValueError, OverflowError):
            # A malformed preparation-owner identity cannot authorize
            # retirement.  Contain the row and fail closed.
            continue
        if owner_pid and owner_identity is not identity_mismatch:
            # Every pid-null preparation phase remains owned while the
            # exact reserving process matches. UNKNOWN is ambiguous and
            # therefore preserves too, even after the nominal lease.
            continue
        if manager._reviewer_source_graph_prewarm_live_event(event):
            # A live owned prewarm keeps extending its own liveness; the
            # bounded reservation epoch alone is never terminal authority
            # while the exact owner is still building the Source Graph.
            continue
        preparation_stall = classify_preparation_stall(
            now_epoch=now,
            preparation_heartbeat_epoch=event.get("preparation_heartbeat_epoch"),
            preparation_phase=event.get("preparation_phase"),
        )
        if preparation_stall["preparation_stalled"]:
            # A frozen preparation heartbeat distinguishes the stable
            # terminal reason once the exact reservation lease has expired.
            terminalized, diagnostics_left = (
                manager._terminalize_committed_reservation(
                    request_id,
                    event,
                    preparation_stall["reason"],
                    diagnostics_left,
                )
            )
            if terminalized:
                retired.append(request_id)
            continue
        terminalized, diagnostics_left = (
            manager._terminalize_committed_reservation(
                request_id,
                event,
                "reservation_expired",
                diagnostics_left,
            )
        )
        if terminalized:
            retired.append(request_id)
    # Mirror this pass's OWN appends into the snapshot it was proved on.
    # A caller sharing the dict (``resolved=True``) decides admission from
    # it with no second parse, so it has to keep describing the ledger
    # exactly -- otherwise a reservation this pass just retired would still
    # read as live and the CAS would refuse a slot that is genuinely free.
    # Only ``state`` is mirrored: it is the sole field admission reads
    # about a retired row, and the appended event above stays the one and
    # only authority for everything else.
    for request_id in retired:
        latest[request_id] = {**latest[request_id], "state": "blocked"}
    return len(retired)
