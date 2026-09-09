"""Identity guard for byte-identical relaunches of terminal failures."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import core, sqlite_readonly, task_store


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

# --- NF-2026-00548 second axis: the repeated OUTCOME ----------------------
#
# The first axis above compares the card. It cannot fire on the rework path at
# all, for two independent reasons measured on this repository's canonical
# store: every rejection writes a new ``review_feedback`` (so
# ``review_feedback_identity`` always differs), and ``reject_review`` runs
# ``begin_claim_episode``, which pops ``terminal_review`` off the card
# entirely -- so the relaunched card carries no terminal record to compare.
#
# This axis therefore reads the durable ``task_events`` terminal rows instead
# of the card, and compares OUTCOMES: the same error identity AND the same
# candidate bytes, produced by the same launch identity. Both must match --
# a different candidate that happens to fail the same way is a real new
# attempt, and blocking it would be a false refusal.
IDENTICAL_OUTCOME_RELAUNCH_BLOCKED_REASON = "identical_outcome_relaunch_blocked"
IDENTICAL_OUTCOME_OVERRIDE_SCHEMA_ID = "aiworkhub.identical_outcome_override.v1"
IDENTICAL_OUTCOME_REFUSAL_SCHEMA_ID = "aiworkhub.identical_outcome_refusal.v1"
# A refusal must never strand a card: these are the two moves that remain
# legal, and the refusal reason names them.
IDENTICAL_OUTCOME_NEXT_MOVES = ("reroute_launch_identity", "identical_outcome_override")
# How many consecutive terminals must carry the SAME outcome before the next
# relaunch is refused.  Measured by replaying this rule over the canonical
# store (task_queue.sqlite, mode=ro immutable=1), 2026-09-08:
#
#   threshold 2 (repeat once):  66 refusals over all history, of which 17 were
#                               followed by a different outcome and 6 by a
#                               ``review_ready`` -- i.e. 6 refusals would have
#                               blocked an attempt that went on to succeed.
#                               On the only fully instrumented window
#                               (>=2026-09-02, when ``error_hash`` and
#                               ``adapter_id`` started being recorded) it fires
#                               exactly once, and that one attempt succeeded.
#   threshold 3 (repeat twice):  8 refusals over all history, 0 followed by a
#                               success; 7 of the 8 tasks never produced
#                               another terminal at all.  0 wrong refusals.
#
# Two identical outcomes are therefore NOT evidence that the third attempt is
# wasted; three are.  The threshold is the measurement, not a preference.
IDENTICAL_OUTCOME_REPEAT_THRESHOLD = 3
_TERMINAL_OUTCOME_EVENTS = ("terminal_review", "terminal_failure")
# One bounded read per launch: only the rows the threshold can consume.
_TERMINAL_OUTCOME_SCAN_LIMIT = 8


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


def _identical_card_relaunch_refusal(
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
    error_hash = terminal_outcome_error_hash(failure)
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


def terminal_outcome_error_hash(record: Mapping[str, Any]) -> str:
    """The bounded error identity of one recorded terminal outcome.

    The recorder pins ``error_hash`` at the top level (since 2026-09-02); the
    error text itself lives under ``evidence.error``, never at the top level,
    so the historical top-level fallback could never produce a hash for a
    record written before that.  Both are read here so one implementation
    answers for every recorded terminal, however old.
    """

    recorded = str(record.get("error_hash") or "").strip().lower()
    if recorded:
        return recorded
    error = record.get("error")
    if not error:
        evidence = record.get("evidence")
        error = evidence.get("error") if isinstance(evidence, Mapping) else None
    return bounded_error_hash(error)


def terminal_outcome_candidate_identity(record: Mapping[str, Any]) -> str:
    """The exact candidate bytes one terminal outcome was produced from.

    ``changed_path_hashes`` is the per-path content manifest the finalizer
    recorded, so an equal manifest means the worker wrote byte-for-byte the
    same candidate.  Absent or empty is NOT an identity: it fails open.
    """

    evidence = record.get("evidence")
    hashes = (
        evidence.get("changed_path_hashes") if isinstance(evidence, Mapping) else None
    )
    if not isinstance(hashes, Mapping):
        hashes = record.get("changed_path_hashes")
    if not isinstance(hashes, Mapping) or not hashes:
        return ""
    return json.dumps(
        {str(path): str(digest) for path, digest in hashes.items()},
        ensure_ascii=False,
        sort_keys=True,
    )


def recorded_terminal_outcomes(
    repo: str | Path | None,
    task_id: str,
    *,
    limit: int = IDENTICAL_OUTCOME_REPEAT_THRESHOLD,
) -> list[dict[str, Any]]:
    """The newest recorded terminal outcomes for one task, newest first.

    Read from ``task_events`` because that is where they survive: the card's
    ``terminal_review`` is a ``CURRENT_EPISODE_CARD_FIELDS`` member and is
    erased by ``begin_claim_episode`` on the very transition that sends a
    rejected card back to ``pending``.  Read-only, bounded, and fails open --
    a store that cannot be read yields no history and therefore no refusal.
    """

    task_id = str(task_id or "").strip()
    if not repo or not task_id:
        return []
    bounded = max(1, min(int(limit), _TERMINAL_OUTCOME_SCAN_LIMIT))
    try:
        db_path = task_store.canonical_db_path(repo)
    except Exception:
        return []
    try:
        conn = sqlite_readonly.connect_readonly(db_path)
    except Exception:
        return []
    try:
        placeholders = ",".join("?" for _ in _TERMINAL_OUTCOME_EVENTS)
        rows = conn.execute(
            "SELECT payload_json FROM task_events WHERE task_id=? "
            f"AND event IN ({placeholders}) ORDER BY event_id DESC LIMIT ?",
            (task_id, *_TERMINAL_OUTCOME_EVENTS, bounded),
        ).fetchall()
    except Exception:
        return []
    finally:
        conn.close()
    outcomes: list[dict[str, Any]] = []
    for row in rows:
        try:
            record = json.loads(str(row[0] or "{}"))
        except (TypeError, ValueError):
            record = None
        if not isinstance(record, dict):
            # An unreadable row breaks the run: the outcomes on either side of
            # it are not consecutive any more, so nothing past it may count.
            break
        outcomes.append(record)
    return outcomes


def identical_outcome_run(
    records: Sequence[Mapping[str, Any]],
) -> tuple[int, dict[str, Any] | None]:
    """How many newest terminals share one outcome, and that outcome's record.

    ``records`` is newest-first.  The run is broken by any change of substatus
    class, error identity, candidate identity, runner or adapter -- so a new
    candidate, a new failure, or a different provider all end it.
    """

    if not records:
        return 0, None
    latest = dict(records[0])
    if str(latest.get("substatus") or "") not in _RELAUNCH_GUARD_FAILURE_SUBSTATUSES:
        return 0, None
    error_hash = terminal_outcome_error_hash(latest)
    candidate = terminal_outcome_candidate_identity(latest)
    if not error_hash or not candidate:
        return 0, None
    runner = str(latest.get("runner") or "")
    adapter_id = str(latest.get("adapter_id") or "")
    run = 1
    for record in records[1:]:
        if str(record.get("substatus") or "") != str(latest.get("substatus") or ""):
            break
        if terminal_outcome_error_hash(record) != error_hash:
            break
        if terminal_outcome_candidate_identity(record) != candidate:
            break
        if str(record.get("runner") or "") != runner:
            break
        if str(record.get("adapter_id") or "") != adapter_id:
            break
        run += 1
    return run, latest


def identical_outcome_override(
    card: Mapping[str, Any] | None, *, request_id: str, error_hash: str
) -> dict[str, Any] | None:
    """The manager's explicit, recorded permission to relaunch anyway.

    It authorizes ONE named outcome: the receipt has to name the exact
    predecessor request and the exact error identity it is overriding, so a
    stale override cannot silently cover a later, different repeat.
    """

    raw = None if card is None else card.get("identical_outcome_override")
    entries = raw if isinstance(raw, list) else [raw]
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        if str(entry.get("schema_id") or "") != IDENTICAL_OUTCOME_OVERRIDE_SCHEMA_ID:
            continue
        if str(entry.get("request_id") or "").strip() != str(request_id):
            continue
        if str(entry.get("error_hash") or "").strip().lower() != str(error_hash):
            continue
        return dict(entry)
    return None


def identical_outcome_refusal(
    card: Mapping[str, Any] | None,
    *,
    runner: str,
    adapter_id: str,
    repo: str | Path | None = None,
    records: Sequence[Mapping[str, Any]] | None = None,
) -> str:
    """Refuse a relaunch that has already produced this outcome twice over.

    Fails open on every missing input: no history, no pinned error identity,
    no candidate manifest, a changed contract, a changed launch identity, a
    coordinator retry/recovery, or an explicit override all permit the launch.
    """

    task_id = str((card or {}).get("task_id") or "")
    if records is None:
        records = recorded_terminal_outcomes(
            repo, task_id, limit=IDENTICAL_OUTCOME_REPEAT_THRESHOLD
        )
    run, latest = identical_outcome_run(list(records or []))
    if latest is None or run < IDENTICAL_OUTCOME_REPEAT_THRESHOLD:
        return ""
    if str(latest.get("runner") or "") != str(runner):
        return ""
    if str(latest.get("adapter_id") or "") != str(adapter_id):
        return ""
    request_id = str(latest.get("request_id") or "").strip()
    if not request_id or len(request_id) > 120:
        return ""
    error_hash = terminal_outcome_error_hash(latest)
    # The contract itself is the other thing that can change between attempts.
    # A record that pins one must still match; a record written before the
    # recorder pinned it cannot be checked, and is not treated as a mismatch.
    recorded_card_identity = str(latest.get("card_content_sha256") or "").strip()
    if recorded_card_identity and recorded_card_identity != card_content_identity(card):
        return ""
    recovered_at = str(
        "" if card is None else card.get("recovered_from_blocked_at") or ""
    ).strip()
    if recovered_at and recovered_at > str(latest.get("recorded_at") or ""):
        return ""
    retry = _latest_recorded(None if card is None else card.get("terminal_retry"))
    if retry is not None and _terminal_retry_supersedes(retry, latest):
        return ""
    if identical_outcome_override(card, request_id=request_id, error_hash=error_hash):
        return ""
    return ":".join(
        (
            IDENTICAL_OUTCOME_RELAUNCH_BLOCKED_REASON,
            request_id,
            error_hash,
            f"repeats={run}",
            "next=" + "|".join(IDENTICAL_OUTCOME_NEXT_MOVES),
        )
    )


def identical_outcome_refusal_receipt(
    card: Mapping[str, Any] | None,
    *,
    repo: str | Path | None = None,
    records: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """The refusal this card meets if relaunched on its own launch identity.

    Derived, never stored: the manager cannot forge it, and it stops being
    true the moment the card's runner changes -- which is exactly what
    ``reroute_launch_identity`` does with it.
    """

    task_id = str((card or {}).get("task_id") or "")
    if records is None:
        records = recorded_terminal_outcomes(
            repo, task_id, limit=IDENTICAL_OUTCOME_REPEAT_THRESHOLD
        )
    records = list(records or [])
    run, latest = identical_outcome_run(records)
    if latest is None:
        return None
    runner = str(latest.get("runner") or "")
    if not runner or runner != str((card or {}).get("runner") or ""):
        return None
    adapter_id = str(latest.get("adapter_id") or "")
    reason = identical_outcome_refusal(
        card, runner=runner, adapter_id=adapter_id, records=records
    )
    if not reason:
        return None
    return {
        "schema_id": IDENTICAL_OUTCOME_REFUSAL_SCHEMA_ID,
        "reason": reason,
        "request_id": str(latest.get("request_id") or "").strip(),
        "error_hash": terminal_outcome_error_hash(latest),
        "terminal_substatus": str(latest.get("substatus") or ""),
        "runner": runner,
        "adapter_id": adapter_id,
        "repeats": run,
        "recorded_at": str(latest.get("recorded_at") or ""),
    }


def identical_relaunch_refusal(
    card: Mapping[str, Any] | None,
    *,
    runner: str,
    adapter_id: str,
    repo: str | Path | None = None,
    records: Sequence[Mapping[str, Any]] | None = None,
) -> str:
    """Why this exact relaunch must not spend another attempt, else ``""``.

    Two independent axes, checked in order of strength.  The first refuses a
    byte-identical relaunch of the SAME CARD.  The second refuses a relaunch
    whose recorded OUTCOMES have already repeated ``IDENTICAL_OUTCOME_REPEAT_
    THRESHOLD`` times on this launch identity, regardless of review feedback --
    the case the first axis structurally cannot see.
    """

    exact = _identical_card_relaunch_refusal(
        card, runner=runner, adapter_id=adapter_id
    )
    if exact:
        return exact
    return identical_outcome_refusal(
        card, runner=runner, adapter_id=adapter_id, repo=repo, records=records
    )
