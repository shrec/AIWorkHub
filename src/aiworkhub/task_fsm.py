"""Pure canonical task-status transition legality for AIWorkHub (v0.6.20).

One source of truth for which ``task_store.canonical_status`` values a
launched/claimed processing task may legally transition *from* when a
terminal outcome (success, validation_failed, worker_failed, launch_failed,
scope_rejected, cancelled, stale/process_lost) is recorded. Every such
terminal outcome routes to ``status=review`` with a terminal substatus and
evidence -- never directly to ``pending`` or ``finished``. Only the
coordinator's own accept/reject/archive APIs (``core.mark_done``,
``core.reject_review``, ``task_store.archive_task``/``restore_task``) may
move a task out of ``review``; nothing in this module grants that authority.

This module is intentionally pure: no I/O, no mutation, no imports beyond
the standard library. ``task_store.mark_terminal_review`` is the sole
caller that turns a rejected transition into a fail-closed refusal plus an
auditable ``task_events`` row.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

# A terminal-review transition is legal only from a task that is still
# "in flight" from the coordinator's perspective: freshly claimed/launched
# (``processing``) or claimed-but-not-yet-committed (``pending`` -- e.g. a
# launch failure recorded before ``claim_start_exact`` ever ran). Recording
# a new terminal outcome against a task already in ``review``, ``finished``,
# ``archived``, or ``blocked`` would silently clobber a coordinator decision
# (or another worker's terminal record) and must fail closed instead.
#
# ``blocked`` is a real, legal TERMINAL outcome (see ``KNOWN_TERMINAL_SUBSTATUSES``),
# not a trap (NF-2026-00341). Refusing it as a *source* here does not strand a
# card: re-terminalizing an already-terminal card is never the way out of
# ``blocked``. The defined exit is the coordinator's own recovery instrument
# ``task_store.recover_blocked_rework`` (rework-eligible outcomes -> back to a
# legal ``pending`` source status, from which terminal review is legal again)
# or the accept/reject/archive APIs. So ``blocked`` is legal to enter AND has a
# defined transition out; it is simply a different transition than the one this
# pure function guards.
# The lifecycle states one claim can legitimately be observed in AFTER its
# attempt has run. Provider spend is a fact about an attempt that already
# happened, so the ledger must still accept it once the card has advanced --
# refusing on "status != processing" silently discarded the cost of every
# worker that succeeded, leaving a ledger that recorded only failures.
# Forgery is prevented by claim identity (launch_request_id plus claim_epoch,
# which a re-claim increments), never by the card's current status.
CLAIM_ATTEMPT_ACCOUNTABLE_STATUSES: frozenset[str] = frozenset({
    "processing",
    "review",
    "blocked",
    "finished",
})


LEGAL_SOURCE_STATUSES_FOR_TERMINAL_REVIEW: frozenset[str] = frozenset({"pending", "processing"})

# The single, authoritative terminal-substatus vocabulary for AIWorkHub. This
# module OWNS it; every other site (``task_store``, ``callback_store``,
# ``process_launcher``) imports the names below rather than restating a literal
# copy. Three hand-copied duplicates had already drifted (NF-2026-00339)
# precisely because agreement was maintained by comments instead of by import.
#
# It is exhaustive and fail-closed: an empty/oversized/malformed or otherwise
# unrecognized substatus is always illegal, and ``check_terminal_review_transition``
# refuses it regardless of source status. A NEW terminal outcome is added HERE
# (and, when it is a producible launcher state, to ``LAUNCHER_TERMINAL_SUBSTATUSES``
# below) -- never invented at the site that produces it, which is exactly how
# outcomes ended up validated in one place and produced in another.
KNOWN_TERMINAL_SUBSTATUSES: frozenset[str] = frozenset(
    {
        "exited",
        "review_ready",
        "validation_failed",
        "worker_failed",
        "launch_failed",
        "scope_rejected",
        "cancelled",
        "timed_out",
        "promotion_conflict",
        "finalize_failed",
        "stale",
        "process_lost",
        "required_output_unchanged",
        "blocked",
        "dependency_blocked",
        "liveness_lost",
        # Launcher-produced outcomes (``process_launcher.TERMINAL_PROCESS_STATES``).
        # These were emitted by the launcher but omitted from this vocabulary,
        # so ``check_terminal_review_transition`` called them illegal even though
        # the callback layer supports them end to end (NF-2026-00339). The
        # barrier now holds by contract, not by which routing call happens to
        # run for them.
        "exited_without_review",
        "finalize_abandoned",
        "monitor_error",
        "token_budget_exceeded",
        "output_budget_exceeded",
    }
)

# The exact subset of ``KNOWN_TERMINAL_SUBSTATUSES`` that ``process_launcher``
# can actually emit as a terminal process state. A named subset of the owner
# vocabulary, not a second copy: ``process_launcher.TERMINAL_PROCESS_STATES``
# IS this object.
LAUNCHER_TERMINAL_SUBSTATUSES: frozenset[str] = frozenset(
    {
        "review_ready",
        "exited",
        "exited_without_review",
        "timed_out",
        "token_budget_exceeded",
        "output_budget_exceeded",
        "cancelled",
        "launch_failed",
        "worker_failed",
        "scope_rejected",
        "validation_failed",
        "promotion_conflict",
        "finalize_failed",
        "finalize_abandoned",
        "monitor_error",
        "blocked",
    }
)

# The subset of terminal substatuses ``task_store.mark_terminal_failure``
# accepts as post-launch failures routed to the canonical ``blocked`` bucket.
# A named policy subset of the owner vocabulary; ``task_store`` references this
# object instead of restating it.
MARK_TERMINAL_FAILURE_SUBSTATUSES: frozenset[str] = frozenset(
    {
        "timed_out",
        "token_budget_exceeded",
        "output_budget_exceeded",
        "worker_failed",
        "finalize_failed",
        "cancelled",
        "liveness_lost",
    }
)

# The canonical callback-delivery transition classes. Every terminal substatus
# collapses onto exactly one of these coarse classes for delivery/eligibility:
# ``callback_store.CALLBACK_ELIGIBLE_TRANSITIONS`` and
# ``task_store._ATOMIC_CALLBACK_TRANSITIONS`` ARE this object. Every class is
# itself a member of ``KNOWN_TERMINAL_SUBSTATUSES``.
TERMINAL_CALLBACK_CLASSES: frozenset[str] = frozenset(
    {
        "review_ready",
        "blocked",
        "launch_failed",
        "worker_failed",
        "validation_failed",
        "scope_rejected",
        "timed_out",
        "token_budget_exceeded",
        "output_budget_exceeded",
        "cancelled",
    }
)

# The authoritative map from a terminal substatus to its canonical callback
# class. ``callback_store`` layers only its own input-spelling aliases (e.g.
# ``"review"``, ``"deferred"``) on top of this; the substatus->class truth lives
# HERE so a blocked-class outcome can never be delivered as ``review_ready``
# (B921 / NF-2026-00340). Every key is a ``KNOWN_TERMINAL_SUBSTATUSES`` member
# and every value is a ``TERMINAL_CALLBACK_CLASSES`` member.
TERMINAL_SUBSTATUS_TO_CALLBACK_CLASS: Mapping[str, str] = MappingProxyType(
    {
        "review_ready": "review_ready",
        "exited": "review_ready",
        "blocked": "blocked",
        "dependency_blocked": "blocked",
        "liveness_lost": "blocked",
        "promotion_conflict": "blocked",
        "finalize_failed": "blocked",
        "finalize_abandoned": "blocked",
        "monitor_error": "blocked",
        "process_lost": "blocked",
        "stale": "blocked",
        "launch_failed": "launch_failed",
        "exited_without_review": "launch_failed",
        "worker_failed": "worker_failed",
        "validation_failed": "validation_failed",
        "required_output_unchanged": "validation_failed",
        "scope_rejected": "scope_rejected",
        "timed_out": "timed_out",
        "token_budget_exceeded": "token_budget_exceeded",
        "output_budget_exceeded": "output_budget_exceeded",
        "cancelled": "cancelled",
    }
)


def check_terminal_review_transition(
    current_canonical_status: str, substatus: str
) -> tuple[bool, str]:
    """Pure legality check for one ``-> review`` terminal transition.

    Returns ``(True, "ok")`` when legal, else ``(False, reason)``. Never
    raises, never touches storage. Fail-closed on the substatus vocabulary
    itself: an unrecognized terminal substatus is illegal regardless of the
    source status, so a typo'd or spoofed outcome can never be audited in as
    if it were one of the known terminal classes.
    """
    substatus = (substatus or "").strip()
    if not substatus:
        return False, "illegal_transition:empty_substatus"
    if substatus not in KNOWN_TERMINAL_SUBSTATUSES:
        return False, f"illegal_transition:unknown_substatus={substatus}"
    if current_canonical_status not in LEGAL_SOURCE_STATUSES_FOR_TERMINAL_REVIEW:
        return False, f"illegal_transition:from={current_canonical_status}:to=review"
    return True, "ok"


def evidence_verdict(
    validations: list[Mapping[str, object]] | None,
    required_output_records: list[Mapping[str, object]] | None,
) -> dict[str, object]:
    """Deterministic pass/fail predicate over already-recorded evidence rows.

    Consumes only the bounded, machine-produced records ``run_validations``
    and ``validate_required_outputs`` already append to a terminal-review
    event -- a validation command's exit code, a required output's recorded
    sha256/size. It never inspects free-text worker output and never grants
    promotion authority: the coordinator's own accept (``core.mark_done``)
    remains the sole authority that finalizes a task, regardless of this
    verdict. This function only records what already passed or failed so
    that decision does not depend on re-parsing an LLM's own claims.
    """
    validations = list(validations or [])
    required_output_records = list(required_output_records or [])

    def _validation_failed(v: object) -> bool:
        if not isinstance(v, Mapping):
            return True
        returncode = v.get("returncode")
        # A missing or ``None`` returncode is an unproven validation, not a
        # proven pass -- never coerce it to 0 via ``or 0`` (that previously
        # let an unrecorded/aborted validation run silently count as green).
        if isinstance(returncode, bool) or not isinstance(returncode, int):
            return True
        return returncode != 0

    def _required_output_missing(r: object) -> bool:
        if not isinstance(r, Mapping):
            return True
        sha256 = r.get("sha256")
        path = r.get("path")
        size = r.get("bytes")
        if not path or not isinstance(sha256, str) or not sha256:
            return True
        # ``validate_required_outputs`` already enforces allow-empty
        # semantics before a record is ever produced, so by this point a
        # recorded size must be a non-negative integer -- never negative,
        # never a non-numeric placeholder.
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            return True
        return False

    failed_validations = [v for v in validations if _validation_failed(v)]
    missing_required_outputs = [
        r for r in required_output_records if _required_output_missing(r)
    ]

    # Nothing measured is not the same as nothing wrong. With no validation and
    # no required-output evidence there is nothing that proves a pass, so an
    # empty verdict must not read as passed: ``passed`` stays False and
    # ``nothing_measured`` says which of the two -- a vacuum, or a clean run --
    # this is. deterministic_verification's no_gates_recorded guard is correct
    # and unchanged; this fixes the exported field that is otherwise copied,
    # green, straight into a terminal record as evidence_verdict.passed.
    nothing_measured = not validations and not required_output_records
    passed = (
        not nothing_measured
        and not failed_validations
        and not missing_required_outputs
    )
    return {
        "passed": passed,
        "nothing_measured": nothing_measured,
        "validation_count": len(validations),
        "failed_validation_count": len(failed_validations),
        "required_output_count": len(required_output_records),
        "missing_required_output_count": len(missing_required_outputs),
    }


# Terminal substatuses that represent a recorded success outcome eligible for
# a meaningful deterministic pass. Every other known terminal substatus is a
# failure class: it can only ever deterministically fail, never pass.
_SUCCESS_TERMINAL_SUBSTATUSES: frozenset[str] = frozenset({"review_ready", "exited"})


def deterministic_verification(
    substatus: str,
    validations: list[Mapping[str, object]] | None,
    required_output_records: list[Mapping[str, object]] | None,
    *,
    claim_epoch: int = 0,
) -> dict[str, object]:
    """Deterministic, fail-closed pass/fail record for one terminal outcome.

    Never grants promotion authority (see ``evidence_verdict``); this is
    additionally substatus-aware so a known failure outcome can never be
    reported as passing regardless of what evidence happens to be attached,
    and a success outcome with no recorded gates is reported as
    inapplicable rather than a hollow, meaningless pass.
    """
    substatus = (substatus or "").strip()
    validations = list(validations or [])
    required_output_records = list(required_output_records or [])
    verdict = evidence_verdict(validations, required_output_records)
    try:
        epoch = max(0, int(claim_epoch))
    except (TypeError, ValueError):
        epoch = 0

    if substatus not in KNOWN_TERMINAL_SUBSTATUSES:
        return {
            "applicable": False,
            "pass": False,
            "substatus": substatus,
            "reason": "unknown_substatus",
            "claim_epoch": epoch,
            "evidence_verdict": verdict,
        }

    if substatus not in _SUCCESS_TERMINAL_SUBSTATUSES:
        return {
            "applicable": True,
            "pass": False,
            "substatus": substatus,
            "reason": "known_failure_substatus",
            "claim_epoch": epoch,
            "evidence_verdict": verdict,
        }

    if not validations and not required_output_records:
        return {
            "applicable": False,
            "pass": False,
            "substatus": substatus,
            "reason": "no_gates_recorded",
            "claim_epoch": epoch,
            "evidence_verdict": verdict,
        }

    return {
        "applicable": True,
        "pass": bool(verdict["passed"]),
        "substatus": substatus,
        "reason": "evidence_verdict_passed" if verdict["passed"] else "evidence_verdict_failed",
        "claim_epoch": epoch,
        "evidence_verdict": verdict,
    }


__all__ = [
    "KNOWN_TERMINAL_SUBSTATUSES",
    "LAUNCHER_TERMINAL_SUBSTATUSES",
    "LEGAL_SOURCE_STATUSES_FOR_TERMINAL_REVIEW",
    "MARK_TERMINAL_FAILURE_SUBSTATUSES",
    "TERMINAL_CALLBACK_CLASSES",
    "TERMINAL_SUBSTATUS_TO_CALLBACK_CLASS",
    "check_terminal_review_transition",
    "deterministic_verification",
    "evidence_verdict",
]
