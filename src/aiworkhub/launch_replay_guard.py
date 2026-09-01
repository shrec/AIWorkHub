"""Identity guard for byte-identical relaunches of terminal failures."""

from __future__ import annotations

from typing import Any, Mapping

from . import task_store


TASK_CONTRACT_KEYS = task_store.TASK_CONTRACT_KEYS
CARD_CONTENT_IDENTITY_KEYS = task_store.CARD_CONTENT_IDENTITY_KEYS
strip_persistence_envelopes = task_store.strip_persistence_envelopes
IDENTICAL_RELAUNCH_BLOCKED_REASON = "identical_relaunch_blocked"
TERMINAL_ERROR_HASH_HEX_CHARS = task_store.TERMINAL_ERROR_HASH_HEX_CHARS
bounded_error_hash = task_store.bounded_error_hash
card_content_identity = task_store.card_content_identity
review_feedback_identity = task_store.review_feedback_identity

_RELAUNCH_GUARD_FAILURE_SUBSTATUSES = frozenset(
    {"validation_failed"} | set(task_store.MARK_TERMINAL_FAILURE_SUBSTATUSES)
)


def _latest_recorded(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return dict(value)
    if not isinstance(value, list):
        return None
    records = [dict(item) for item in value if isinstance(item, dict)]
    if not records:
        return None
    return max(records, key=lambda record: str(record.get("recorded_at") or ""))


def _terminal_retry_supersedes(
    retry: Mapping[str, Any], failure: Mapping[str, Any]
) -> bool:
    named = str(retry.get("predecessor_request_id") or "").strip()
    if named and named == str(failure.get("request_id") or "").strip():
        return True
    retry_at = str(retry.get("recorded_at") or "").strip()
    if not retry_at:
        return True
    return retry_at > str(failure.get("recorded_at") or "").strip()


def identical_relaunch_refusal(
    card: Mapping[str, Any] | None,
    *,
    runner: str,
    adapter_id: str,
) -> str:
    candidates: list[dict[str, Any]] = []
    for field in ("terminal_failure", "terminal_review"):
        record = _latest_recorded(None if card is None else card.get(field))
        if record is None:
            continue
        if (
            field == "terminal_review"
            and str(record.get("substatus") or "")
            not in _RELAUNCH_GUARD_FAILURE_SUBSTATUSES
        ):
            continue
        candidates.append(record)
    if not candidates:
        return ""
    failure = max(
        candidates, key=lambda record: str(record.get("recorded_at") or "")
    )
    predecessor_request_id = str(failure.get("request_id") or "").strip()
    if not predecessor_request_id:
        return ""
    error_hash = str(failure.get("error_hash") or "").strip().lower()
    if not error_hash:
        error_hash = bounded_error_hash(failure.get("error"))
    if not error_hash:
        return ""
    if str(failure.get("runner") or "") != str(runner):
        return ""
    if str(failure.get("adapter_id") or "") != str(adapter_id):
        return ""
    recorded_card_identity = str(failure.get("card_content_sha256") or "").strip()
    if not recorded_card_identity:
        return ""
    if recorded_card_identity != card_content_identity(card):
        return ""
    recorded_feedback_identity = str(
        failure.get("review_feedback_identity") or ""
    ).strip()
    if not recorded_feedback_identity:
        return ""
    if recorded_feedback_identity != review_feedback_identity(card):
        return ""
    retry = _latest_recorded(None if card is None else card.get("terminal_retry"))
    if retry is not None and _terminal_retry_supersedes(retry, failure):
        return ""
    return ":".join(
        (IDENTICAL_RELAUNCH_BLOCKED_REASON, predecessor_request_id, error_hash)
    )
