"""Refusal shapes for a reviewer launch that cannot bind its card (NF-2026-01993).

``ProcessManager._ensure_quality_reviewer_card_bound`` names its reserved
request, reviewer card, target request/task and adapter in every non-ok
reply.  The identity builder and the shared refusal shapes moved here
unchanged under the ``process_launcher`` module size ratchet (NF-2026-02036);
``process_launcher`` imports this module and owns no second copy.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

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
