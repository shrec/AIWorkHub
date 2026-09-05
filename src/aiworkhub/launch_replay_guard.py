"""Identity guard for byte-identical relaunches of terminal failures."""

from __future__ import annotations

import re
from typing import Any, Mapping

from . import core, task_store


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


def validation_only_replay_authorization(
    card: Mapping[str, Any], task_id: str
) -> dict[str, Any] | None:
    """Return one exact provider-free replay grant or fail closed.

    The task store mints this coordinator-only grant while recovering a
    blocked task.  Merely finding a similarly named field must never select
    the deterministic lane: every immutable episode binding is checked before
    a claim, workspace mutation, credential lookup, or provider operation.
    Output bytes are checked again by ``validate_required_outputs`` inside the
    ordinary finalizer, so this routing check cannot authorize stale content.
    """

    raw = card.get("validation_only_replay_authorization")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("validation_only_replay_authorization_invalid")
    if raw.get("one_episode_binding") is not True:
        raise ValueError("validation_only_replay_episode_binding_missing")
    if str(raw.get("task_id") or "") != task_id:
        raise ValueError("validation_only_replay_task_mismatch")
    if str(raw.get("actor") or "") != core.CODEX_RUNNER:
        raise ValueError("validation_only_replay_actor_mismatch")

    predecessor = card.get("rework_predecessor")
    if not isinstance(predecessor, dict):
        raise ValueError("validation_only_replay_predecessor_missing")
    predecessor_request_id = str(predecessor.get("request_id") or "").strip()
    if not predecessor_request_id or str(
        raw.get("predecessor_request_id") or ""
    ) != predecessor_request_id:
        raise ValueError("validation_only_replay_predecessor_mismatch")
    predecessor_hashes = predecessor.get("changed_path_hashes")
    authorized_hashes = raw.get("changed_path_hashes")
    if (
        not isinstance(predecessor_hashes, dict)
        or not predecessor_hashes
        or not isinstance(authorized_hashes, dict)
        or authorized_hashes != predecessor_hashes
    ):
        raise ValueError("validation_only_replay_hash_manifest_mismatch")
    if not all(
        isinstance(path, str)
        and path.strip()
        and isinstance(digest, str)
        and re.fullmatch(r"[a-f0-9]{64}", digest)
        for path, digest in authorized_hashes.items()
    ):
        raise ValueError("validation_only_replay_hash_manifest_invalid")
    try:
        authorized_epoch = int(str(raw.get("next_claim_epoch")))
        claim_epoch = int(str(card.get("claim_epoch")))
    except (TypeError, ValueError):
        raise ValueError("validation_only_replay_claim_epoch_invalid") from None
    if authorized_epoch != claim_epoch:
        raise ValueError("validation_only_replay_claim_epoch_mismatch")
    # A replay may exist solely to re-finalize hash-pinned inherited outputs
    # after an operational finalizer failure. The authenticated predecessor
    # hash manifest above is the replay authority; ``required_outputs`` may be
    # empty when the template authorizes changed paths without declaring every
    # allowed path as a mandatory changed output. An explicitly empty
    # validation contract is also authoritative and must not force executable
    # scratch or a provider call.
    return dict(raw)



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
    # ``recover-blocked-rework`` is itself a coordinator-authorized retry.
    # It deliberately preserves the terminal episode for auditability, so the
    # absence of a separate ``terminal_retry`` envelope must not make the
    # recovered pending card indistinguishable from an unreviewed relaunch.
    recovered_at = str(
        "" if card is None else card.get("recovered_from_blocked_at") or ""
    ).strip()
    if recovered_at and recovered_at > str(failure.get("recorded_at") or ""):
        return ""
    retry = _latest_recorded(None if card is None else card.get("terminal_retry"))
    if retry is not None and _terminal_retry_supersedes(retry, failure):
        return ""
    return ":".join(
        (IDENTICAL_RELAUNCH_BLOCKED_REASON, predecessor_request_id, error_hash)
    )
