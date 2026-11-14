"""Review-waiting listing for ``claude_callback_wait`` (NF-2026-01859/01939/02255/02262).

NF-2026-02262: the NF-2026-02255 own-origin default folded *every* foreign item
into ``other_origin_count``, which also dropped the review-status cards whose
callback was delivered to another origin -- exactly the cards NF-2026-01859
added ``review_waiting`` to name.  The scope rule is now: a foreign review-status
card stays listed (reason ``other_origin``), while foreign blocked cards and
foreign dead reviewers stay folded unless ``include_other_origin`` is true.
Listing is visibility only; claim and delivery stay origin-scoped.

``core`` binds ``_callback_review_waiting`` to this owner; the policy helpers,
vocabularies and limits are imported from ``manager_callback_wait``.
"""

from __future__ import annotations

import json
from typing import Any

from .manager_callback_wait import (
    _DEAD_REVIEWER_SUBSTATUSES,
    _FAILED_SUBSTATUSES,
    _QUALITY_REVIEW_TASK_PREFIX,
    CALLBACK_WAIT_REVIEW_WAITING_LIMIT,
    CALLBACK_WAIT_REVIEW_WAITING_SCAN_LIMIT,
    _review_waiting_cause,
    _review_waiting_target_finished,
)


def _callback_review_waiting(
    provider: str, session_id: str, *, include_other_origin: bool = False,
) -> dict[str, Any]:
    """Review-bucket cards of ``provider`` this wait cannot deliver, bounded to
    ``CALLBACK_WAIT_REVIEW_WAITING_LIMIT`` rows.  Visibility only: nothing here
    claims, seeds or acknowledges.  ``reason`` is other_origin (a callback row
    for the current episode belongs to another origin), deferred_review_chain
    (the wake is held for the quality-review chain) or no_callback_row.

    A terminal ``QUALITY_REVIEW_*`` card that is other_origin, or whose reviewed
    target is finished, is counted as ``review_waiting_hidden_terminal``.
    Every foreign item is counted in ``other_origin_count``; by default only a
    foreign review-status card (not a dead reviewer) is also listed, the rest
    are listed only when ``include_other_origin`` is true."""
    from . import core  # lazy: core re-exports this module
    scope = "all_origins" if include_other_origin else "own_origin"
    waiting: list[dict[str, str]] = []
    hidden_terminal = 0
    other_origin = 0
    try:
        conn = core._canonical_connect(readonly=True)
        try:
            tasks = conn.execute(
                "SELECT task_id, status, card_json, origin_thread_id FROM tasks "
                "WHERE status IN ('review', 'blocked') AND (archived_at IS NULL OR archived_at='') "
                "ORDER BY updated_at DESC, task_id ASC LIMIT ?",
                (CALLBACK_WAIT_REVIEW_WAITING_SCAN_LIMIT,),
            )
            for task in tasks:
                try:
                    card = json.loads(task["card_json"] or "{}")
                except (TypeError, json.JSONDecodeError):
                    card = {}
                if not isinstance(card, dict):
                    card = {}
                if str(card.get("coordinator_provider") or provider).strip().lower() != provider:
                    continue
                status = str(task["status"] or "")
                substatus = str(card.get("terminal_substatus") or "").strip()
                if status == "blocked" and (
                    core.callback_store.normalize_callback_transition(substatus)
                    not in core.callback_store.CALLBACK_ELIGIBLE_TRANSITIONS
                ):
                    continue
                task_id = str(task["task_id"] or "")
                outbox = conn.execute(
                    "SELECT origin_thread_id, state FROM callback_outbox "
                    "WHERE task_id=? AND provider IN (?, ?) AND episode_id=?",
                    (
                        task_id,
                        provider,
                        core.callback_store.MANAGER_CHAT_PROVIDER,
                        str(card.get("claim_epoch") or 0),
                    ),
                ).fetchall()
                if any(
                    row["origin_thread_id"] == session_id
                    and row["state"] in {"pending", "inflight", "delivered"}
                    for row in outbox
                ):
                    continue
                review = card.get("terminal_review")
                evidence = review.get("evidence") if isinstance(review, dict) else None
                if any(row["origin_thread_id"] != session_id for row in outbox):
                    reason = "other_origin"
                elif (
                    isinstance(evidence, dict)
                    and isinstance(evidence.get("manager_callback_deferred"), dict)
                    and not card.get("manager_ready_receipt")
                ):
                    reason = "deferred_review_chain"
                else:
                    reason = "no_callback_row"
                dead_reviewer = (
                    task_id.startswith(_QUALITY_REVIEW_TASK_PREFIX)
                    and substatus in _DEAD_REVIEWER_SUBSTATUSES
                )
                # Own = the task or any callback row for this episode names this
                # session; an item with no recorded origin is not provably
                # foreign.  The origin check precedes the dead-reviewer branch so
                # a folded foreign dead reviewer never lands in this session's
                # review_waiting_hidden_terminal.
                task_origin = str(
                    task["origin_thread_id"] or card.get("origin_thread_id") or ""
                ).strip()
                own_origin = task_origin == session_id or any(
                    row["origin_thread_id"] == session_id for row in outbox
                )
                if not own_origin and (task_origin or reason == "other_origin"):
                    other_origin += 1
                    # A provably foreign card is other_origin whatever the
                    # outbox-derived reason (e.g. no callback row at all).
                    reason = "other_origin"
                    foreign_listed = status == "review" and not dead_reviewer
                    if not include_other_origin and not foreign_listed:
                        continue
                if dead_reviewer and (
                    reason == "other_origin" or _review_waiting_target_finished(conn, card)
                ):
                    hidden_terminal += 1
                    continue
                if len(waiting) >= CALLBACK_WAIT_REVIEW_WAITING_LIMIT:
                    continue
                entry = {
                    "task_id": task_id,
                    "status": status,
                    "terminal_substatus": substatus,
                    "reason": reason,
                }
                if substatus in _FAILED_SUBSTATUSES:
                    entry.update(_review_waiting_cause(card))
                waiting.append(entry)
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 -- evidence must never break the wait
        # The error reply carries the same scope keys so callers never KeyError.
        return {
            "review_waiting": [],
            "review_waiting_error": type(exc).__name__,
            "review_waiting_scope": scope,
            "other_origin_count": 0,
        }
    result: dict[str, Any] = {
        "review_waiting": waiting,
        "review_waiting_scope": scope,
        "other_origin_count": other_origin,
    }
    if hidden_terminal:
        result["review_waiting_hidden_terminal"] = hidden_terminal
    return result
