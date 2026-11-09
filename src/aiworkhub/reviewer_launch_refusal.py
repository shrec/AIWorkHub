"""Refusal shapes for a reviewer launch that cannot bind its card (NF-2026-01993).

``ProcessManager._ensure_quality_reviewer_card_bound`` names its reserved
request, reviewer card, target request/task and adapter in every non-ok
reply.  The identity builder and the shared refusal shapes moved here
unchanged under the ``process_launcher`` module size ratchet (NF-2026-02036);
``process_launcher`` imports this module and owns no second copy.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from . import vscode_lm_activity

RETAINED_RECOVERY_STATE = "starting_reservation_retained"
DETAIL_LIMIT = 500


def refusal_identity(
    *,
    request_id: str,
    task_id: str,
    target_request_id: str,
    target_task_id: str,
    adapter_id: str,
) -> dict[str, str]:
    """The identity keys every refused reviewer launch carries by value."""

    return {
        "request_id": request_id,
        "task_id": task_id,
        "target_request_id": target_request_id,
        "target_task_id": target_task_id,
        "adapter_id": adapter_id,
    }


def refusal(ident: Mapping[str, str], error: str) -> dict[str, Any]:
    """A plain fail-closed refusal: ``ok``/``error`` plus the launch identity."""

    return {"ok": False, "error": error, **ident}


def retained_refusal(
    ident: Mapping[str, str],
    error: str,
    *,
    claim_recovery_bound: bool,
    **evidence: Any,
) -> dict[str, Any]:
    """A refusal whose starting reservation is retained for reconciliation."""

    return {
        "ok": False,
        **ident,
        "error": error,
        "recovery_state": RETAINED_RECOVERY_STATE,
        "claim_recovery_bound": claim_recovery_bound,
        **evidence,
    }


def reply_detail(reply: Mapping[str, Any]) -> str:
    """The bounded stderr/stdout/error text of a task-engine reply."""

    return str(
        reply.get("stderr") or reply.get("stdout") or reply.get("error") or ""
    )[:DETAIL_LIMIT]


def create_receipt_structured(created: Mapping[str, Any]) -> bool:
    """A non-ok create reply that still proves the identical card exists."""

    return (
        created.get("reconciled") is True
        or str(created.get("receipt_state") or "") == "existing_identical"
    )


def claim_failure_reason(claim: Mapping[str, Any]) -> str:
    """The refusal reason of a reviewer claim the task engine did not grant."""

    return "quality_review_claim_failed:" + str(
        claim.get("stderr") or claim.get("stdout") or ""
    )[:DETAIL_LIMIT]


def binding_failure_reason(exc: BaseException) -> str:
    """The refusal reason of a claim-epoch binding that raised."""

    return (
        f"quality_review_claim_binding_failed:{type(exc).__name__}:{exc}"
    )[:DETAIL_LIMIT]


def card_identity_mismatch(
    card: Mapping[str, Any],
    *,
    runner: str,
    request_id: str,
    require_launch_request: bool,
) -> str:
    """The identity-mismatch refusal of a durable reviewer card, or ``""``."""

    allowed_writes = card.get("allowed_writes")
    mismatches: list[str] = []
    if card.get("read_only") is not True:
        mismatches.append("read_only")
    if not isinstance(allowed_writes, list) or list(allowed_writes) != []:
        mismatches.append("allowed_writes")
    if card.get("topic") != "quality_review":
        mismatches.append("topic")
    if card.get("runner") != runner:
        mismatches.append("runner")
    if require_launch_request and card.get("launch_request_id") != request_id:
        mismatches.append("launch_request_id")
    if not mismatches:
        return ""
    return "quality_review_card_identity_mismatch:" + ",".join(mismatches)


# --- launch-class reviewer start failures (NF-2026-02086) -----------------
#
# A reviewer START that failed because this sandbox cannot run the adapter at
# all: the spawn was denied, the executable is missing, or a Windows
# AppContainer refused it (WinError 5 / WinError 2; errno 13 / errno 2 on
# POSIX).  No provider ran and the candidate was never read, so it says
# nothing about the candidate, and another start on the same adapter fails
# the same way.  ``review_orchestrator._reviewer_failure_class`` -- the one
# reviewer failure classifier -- names it, the lens fails over to the next
# ranked adapter, and the ``workforce_catalog`` route circuit keeps the route
# skipped until a later start of it succeeds.
#
# Only a failed START qualifies.  A reviewer that ran and then failed on the
# candidate or its own review content is never launch-class, whatever its text
# says, and no model can produce a ``launch_failed`` state: it never ran.
LAUNCH_FAILURE_CLASS = "launch_denied"
LAUNCH_FAILURE_STATES = frozenset({"launch_failed"})
_LAUNCH_FAILURE_MARKER = re.compile(
    r"winerror\s*[25]\b|\[errno (?:2|13)\]|access[\s_]+(?:is[\s_]+)?denied"
    r"|spawn[\s_]+denied|executable[\s_]+not[\s_]+found|appcontainer"
    r"|cannot find the file specified"
)
_SKIP_FIELD_UNSAFE = re.compile(r"[^A-Za-z0-9_.\-]")
SKIP_FIELD_LIMIT = 128


def launch_class_signal(
    status: Mapping[str, Any], card: Mapping[str, Any], signal: str,
) -> bool:
    """Whether a terminal reviewer status is a launch-class START failure.

    ``signal`` is the bounded failure text the caller already joined.  The
    state is resolved the way ``mechanical_reviewer_failure_reason`` resolves
    it: a ``blocked`` card reads its own terminal substatus, and a synchronous
    ``ok: False`` receipt carrying no state is a failed launch.
    """

    state = str(status.get("state") or "").strip().lower()
    if state in {"", "blocked"}:
        state = str(
            card.get("terminal_substatus") or card.get("worker_status") or ""
        ).strip().lower() or ("launch_failed" if status.get("ok") is False else "")
    return (
        state in LAUNCH_FAILURE_STATES
        and _LAUNCH_FAILURE_MARKER.search(str(signal or "").lower()) is not None
    )


def launch_class_retirement(failure_reason: Any) -> bool:
    """Whether a retired route attempt's reason is a launch-class failure."""

    parts = str(failure_reason or "").split(":", 2)
    return (
        len(parts) >= 2
        and parts[0] in LAUNCH_FAILURE_STATES
        and parts[1] == LAUNCH_FAILURE_CLASS
    )


def launch_failure_code(failure_reason: Any) -> str:
    """``LAUNCH_FAILURE_CLASS`` for a launch-class reason, else ``""``."""

    return LAUNCH_FAILURE_CLASS if launch_class_retirement(failure_reason) else ""


LAUNCH_EXCEPTION_PREFIX = "launch_exception"
_EXCEPTION_MESSAGE_UNSAFE = re.compile(r"[^A-Za-z0-9_.\- ]+")
EXCEPTION_MESSAGE_LIMIT = 160


def launch_exception_reason(exc: BaseException) -> str:
    """The bounded fault a reviewer launch that raised leaves on its attempt.

    NF-2026-02107. Its class and message, reduced to a closed character set so
    the ``:``-separated reason stays parseable and no message can spell the
    route-hold marker. Recognized secrets are redacted first, because the
    filter strips the ``=`` and ``:`` the redaction patterns need.
    """

    redacted = vscode_lm_activity.redact_text(str(exc), parse_json=False)
    message = _EXCEPTION_MESSAGE_UNSAFE.sub(" ", redacted).strip()
    return (
        f"{LAUNCH_EXCEPTION_PREFIX}:{type(exc).__name__}"
        f":{message[:EXCEPTION_MESSAGE_LIMIT]}"
    )


def launch_failure_skip_reason(
    route_failure: str, receipt: Mapping[str, Any], adapter_id: str,
) -> str:
    """The retirement reason a launch-class failure records on its lens.

    It names the adapter that could not start and the reviewer request the
    launcher reserved for it, beside the failure class; any other failure is
    returned unchanged.  Both fields are reduced to a closed character set so
    the ``:``-separated reason stays parseable.
    """

    if not launch_class_retirement(route_failure):
        return route_failure

    def _field(value: Any) -> str:
        return _SKIP_FIELD_UNSAFE.sub("", str(value or ""))[:SKIP_FIELD_LIMIT]

    return (
        f"{route_failure}:adapter={_field(adapter_id) or 'unknown'}"
        f":reviewer_request={_field(receipt.get('request_id')) or 'unallocated'}"
    )
