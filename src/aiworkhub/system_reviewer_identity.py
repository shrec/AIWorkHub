"""NF-2026-02372: the identity a system quality reviewer create runs under.

The background reconciler commissions a quality reviewer card from a process
whose ancestry is not the manager's, so ``_live_manager_identity()`` is None
there and ``create_task`` refused every automatic reviewer launch with
``manager_identity_required:task_create``. The reviewer's callbacks belong to
the manager that owns the target (``_review_target_callback_binding``), so a
card that is exactly a read-only, no-writes ``quality_review`` card naming a
resolvable target may be created under that target's recorded route. Anything
else gets None and the caller keeps refusing. The write gate and coordinator
capability gate are not this module's concern and still run in the caller.
"""

from __future__ import annotations

from collections.abc import Sequence

QUALITY_REVIEW_TOPIC = "quality_review"


def system_reviewer_identity(
    binding: tuple[str, str, str, str] | None,
    *,
    review_target_task_id: str | None,
    topic: str | None,
    read_only: bool,
    allowed_writes: Sequence[str] | None,
) -> dict[str, str] | None:
    """The derived identity for a system reviewer create, None unless all hold.

    ``binding`` is ``_review_target_callback_binding(review_target_task_id)``:
    ``(origin, manager_chat_session, provider, route_state)``.
    """
    if binding is None or not str(review_target_task_id or "").strip():
        return None
    if str(topic or "").strip() != QUALITY_REVIEW_TOPIC or read_only is not True:
        return None
    if allowed_writes is None:
        writes: list[str] = []
    elif isinstance(allowed_writes, (str, bytes)):
        return None
    else:
        writes = [str(item) for item in allowed_writes]
    if any(item.strip() for item in writes):
        return None
    origin, _chat, provider, route_state = binding
    if not origin or not provider:
        return None
    return {
        "provider": provider,
        "session_id": origin,
        "thread_id": origin,
        "window_id": "",
        "route_state": route_state,
        "callback_supported": "true",
    }
