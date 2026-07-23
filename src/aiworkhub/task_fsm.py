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

from typing import Mapping

# A terminal-review transition is legal only from a task that is still
# "in flight" from the coordinator's perspective: freshly claimed/launched
# (``processing``) or claimed-but-not-yet-committed (``pending`` -- e.g. a
# launch failure recorded before ``claim_start_exact`` ever ran). Recording
# a new terminal outcome against a task already in ``review``, ``finished``,
# ``archived``, or ``blocked`` would silently clobber a coordinator decision
# (or another worker's terminal record) and must fail closed instead.
LEGAL_SOURCE_STATUSES_FOR_TERMINAL_REVIEW: frozenset[str] = frozenset({"pending", "processing"})

# Bounded, known terminal substatus vocabulary. Not exhaustive-enforced (new
# adapters may add outcomes), but empty/oversized/malformed substatus is
# always illegal regardless of source status.
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

    passed = not failed_validations and not missing_required_outputs
    return {
        "passed": passed,
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

    if substatus not in KNOWN_TERMINAL_SUBSTATUSES:
        return {
            "applicable": False,
            "pass": False,
            "substatus": substatus,
            "reason": "unknown_substatus",
            "evidence_verdict": verdict,
        }

    if substatus not in _SUCCESS_TERMINAL_SUBSTATUSES:
        return {
            "applicable": True,
            "pass": False,
            "substatus": substatus,
            "reason": "known_failure_substatus",
            "evidence_verdict": verdict,
        }

    if not validations and not required_output_records:
        return {
            "applicable": False,
            "pass": False,
            "substatus": substatus,
            "reason": "no_gates_recorded",
            "evidence_verdict": verdict,
        }

    return {
        "applicable": True,
        "pass": bool(verdict["passed"]),
        "substatus": substatus,
        "reason": "evidence_verdict_passed" if verdict["passed"] else "evidence_verdict_failed",
        "evidence_verdict": verdict,
    }


__all__ = [
    "KNOWN_TERMINAL_SUBSTATUSES",
    "LEGAL_SOURCE_STATUSES_FOR_TERMINAL_REVIEW",
    "check_terminal_review_transition",
    "deterministic_verification",
    "evidence_verdict",
]
