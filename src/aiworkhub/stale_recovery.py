from __future__ import annotations

import functools
import re
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aiworkhub import (
    completion_inbox,
    launch_queue_persist,
    needfix_store,
    process_launcher,
    task_store,
)


ACTION_RANK = {
    "kill_zombie": 0,
    "requeue": 1,
    "mark_done": 2,
    "escalate": 3,
}

ORPHAN_CLAIM_GRACE_LEASES = 3


def _claim_age_seconds(claimed_at: str | None, now: datetime) -> float | None:
    """Age of a claim, or None when the timestamp cannot prove one."""
    try:
        claimed = datetime.fromisoformat(str(claimed_at or ""))
    except ValueError:
        return None
    if claimed.tzinfo is None:
        return None
    return (now - claimed).total_seconds()


def reconcile_dead_processing_claim(
    *, root: str, task_id: str, request_id: str, claim_epoch: int,
    process_status: dict[str, Any], actor: str,
    claimed_at: str | None = None, now: datetime | None = None,
) -> tuple[bool, str]:
    """Validate exact authenticated launcher evidence and reconcile its claim."""
    if process_status.get("state") == "not_found":
        grace = ORPHAN_CLAIM_GRACE_LEASES * process_launcher.heartbeat_lease_seconds()
        age = _claim_age_seconds(claimed_at, now or datetime.now(timezone.utc))
        if age is None or age < grace:
            return False, "orphan_claim_within_grace"
        return task_store.reconcile_dead_processing_claim(
            root, task_id, request_id=request_id, claim_epoch=claim_epoch,
            terminal_evidence={
                "state": "not_found", "reason": NEVER_STARTED_REASON,
                "request_id": request_id, "task_id": task_id,
                "claimed_at": claimed_at, "grace_seconds": grace,
            },
            actor=actor,
        )
    if not process_status.get("ok"):
        return False, "process_evidence_missing_or_unauthenticated"
    if str(process_status.get("request_id") or "") != request_id:
        return False, "process_request_id_mismatch"
    if str(process_status.get("task_id") or "") != task_id:
        return False, "process_task_id_mismatch"
    if process_status.get("process_alive") is not False:
        return False, "process_not_proven_dead"
    if str(process_status.get("state") or "") != "blocked":
        return False, "process_not_terminal_blocked"
    latest = process_status.get("latest_event")
    if not isinstance(latest, dict):
        return False, "process_latest_event_invalid"
    if str(latest.get("request_id") or "") != request_id:
        return False, "process_latest_event_request_id_mismatch"
    if str(latest.get("task_id") or "") != task_id:
        return False, "process_latest_event_task_id_mismatch"
    if str(latest.get("state") or "") != "blocked":
        return False, "process_latest_event_not_terminal_blocked"
    evidence = {
        key: process_status.get(key)
        for key in (
            "request_id", "task_id", "state", "process_alive", "exit_code",
            "runner", "topic", "error",
        )
    }
    evidence["latest_event"] = latest
    return task_store.reconcile_dead_processing_claim(
        root, task_id, request_id=request_id, claim_epoch=claim_epoch,
        terminal_evidence=evidence, actor=actor,
    )


def _open_entries_by_task(launch_log: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    entries = launch_log.get("last_entries", []) or []
    out: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        task_id = entry.get("task_id")
        if not task_id:
            continue
        if entry.get("decision") == "rolled_back":
            continue
        if entry.get("to_state") not in {"queued", "planned", "blocked_launch_disabled"}:
            continue
        out.setdefault(str(task_id), []).append(entry)
    return out


def classify_stale_row(row: dict[str, Any], open_entries: list[dict[str, Any]]) -> dict[str, Any]:
    task_id = str(row.get("task_id") or "")
    validation_status = str(row.get("validation_status") or row.get("latest_validation_status") or "").upper()
    has_artifact = bool(row.get("has_artifact") or row.get("artifact_present") or row.get("artifacts_existing_count"))
    zombie_hint = bool(row.get("zombie_hint") or row.get("process_state") == "zombie")

    if zombie_hint:
        action_type = "kill_zombie"
        confidence = "low" if not row.get("pid") else "medium"
        reason = "stale row has zombie hint; pid-aware kill remains low-confidence until real pid tracking is available"
    elif open_entries:
        action_type = "requeue"
        confidence = "medium"
        reason = "stale processing row has an open launch-queue request; recommend requeue/owner recovery before a fresh request"
    elif validation_status == "PASS" or has_artifact:
        action_type = "mark_done"
        confidence = "medium"
        reason = "stale row appears to have successful validation or artifacts; recommend Codex review/finalization"
    else:
        action_type = "escalate"
        confidence = "low"
        reason = "stale row lacks launch-log evidence, validation success, artifacts, or pid data"

    return {
        "task_id": task_id,
        "runner": row.get("runner") or row.get("claimed_by"),
        "topic": row.get("topic"),
        "action_type": action_type,
        "confidence": confidence,
        "reason": reason,
        "rank": ACTION_RANK[action_type],
        "readonly": True,
    }


def build_recovery_actions(
    *,
    inbox: dict[str, Any] | None = None,
    launch_log: dict[str, Any] | None = None,
    topic: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """Read-only stale recovery recommendations.

    No taskctl write commands are issued here. When ``inbox``/``launch_log``
    are omitted, this reads the existing completion inbox and launch audit log.
    """
    inbox = inbox if inbox is not None else completion_inbox.build_completion_inbox(topic=topic, limit=limit)
    launch_log = launch_log if launch_log is not None else launch_queue_persist.read_persisted_log(max_entries=limit)
    open_by_task = _open_entries_by_task(launch_log)
    stale_rows = inbox.get("stale_processing", []) or []
    actions = [
        classify_stale_row(row, open_by_task.get(str(row.get("task_id") or ""), []))
        for row in stale_rows
    ]
    actions.sort(key=lambda row: (row["rank"], row.get("task_id") or ""))
    return {
        "tool": "aiworkhub_task_stale_recovery_recommend",
        "contract": "B287_v1_readonly_recommendations",
        "readonly": True,
        "topic": topic,
        "recovery_actions": actions,
        "counts": {
            "stale_processing": len(stale_rows),
            "recovery_actions": len(actions),
            "by_action": {
                action: sum(1 for row in actions if row["action_type"] == action)
                for action in ACTION_RANK
            },
        },
        "authority_flags": _authority_flags(),
    }


def _authority_flags() -> dict[str, bool]:
    return {
        "runtime_authority": False,
        "support_authority": False,
        "queue_write": False,
        "audit_write": False,
        "process_launch": False,
        "agent_launch": False,
    }


SCOPES = ("processing", "blocked")

TRIAGE_CONTRACT = "NF-2026-01673_blocked_triage_v1"

# Rule order: the first rule that matches a card wins, and rows sort by this rank.
RECOMMENDATION_RANK = {
    "archive": 0,
    "supersede": 1,
    "replay": 2,
    "relaunch": 3,
    "keep": 4,
}

# Operational run failures; a named subset of task_fsm.KNOWN_TERMINAL_SUBSTATUSES, pinned by test.
RELAUNCH_SUBSTATUSES = frozenset({
    "worker_failed",
    "liveness_lost",
    "launch_failed",
    "timed_out",
    "cancelled",
})

# NF-2026-01777: the one dead_process_reconciled episode that is purely operational.
NEVER_STARTED_SUBSTATUS = "dead_process_reconciled"
NEVER_STARTED_REASON = "launch_never_started"


def never_started_request_id(card: Mapping[str, Any]) -> str:
    """The launch request id of an episode whose launch never started, else ``""``.

    Qualifies only the exact evidence ``reconcile_dead_processing_claim`` records
    for a claim with no process record: terminal_substatus
    ``dead_process_reconciled`` whose ``dead_process_reconciliation`` evidence is
    ``state=not_found`` and ``reason=launch_never_started`` for the same request
    and task.  A worker that ran and then died is never this episode.
    """
    if str(card.get("terminal_substatus") or "") != NEVER_STARTED_SUBSTATUS:
        return ""
    recorded = card.get("dead_process_reconciliation")
    evidence = recorded.get("evidence") if isinstance(recorded, Mapping) else None
    if not isinstance(evidence, Mapping):
        return ""
    request_id = str(recorded.get("request_id") or "").strip()
    if (
        evidence.get("reason") != NEVER_STARTED_REASON
        or evidence.get("state") != "not_found"
        or not request_id
        or str(evidence.get("request_id") or "") != request_id
        or str(evidence.get("task_id") or "") != str(card.get("task_id") or "")
    ):
        return ""
    return request_id

# A named subset of needfix_store.NEEDFIX_TERMINAL_STATUSES, pinned to it by test.
NEEDFIX_CLOSED_STATUSES = frozenset({"resolved", "archived"})

_SIBLING_DONE_STATUSES = ("finished", "archived")
_SIBLING_LIVE_STATUSES = ("pending", "processing", "review")
_STORE_SCAN_CAP = 5000
_NEEDFIX_CARD_ID = re.compile(r"needfix-(NF-[0-9]{4}-[0-9]+)(?:-r[0-9]+)?")
_READ_FAILURE = "_read_failure"

# (created_at, task_id, canonical status) of one same-topic card.
_Sibling = tuple[datetime | None, str, str]


def _attempt(label: str, read: Callable[..., Any], *args: Any, **kwargs: Any) -> tuple[Any, str]:
    """Run one evidence read; a failure becomes a named reason instead of a raise."""
    try:
        return read(*args, **kwargs), ""
    except Exception as exc:
        return None, f"{label}:{type(exc).__name__}"


def _created_at(card: Mapping[str, Any]) -> datetime | None:
    try:
        return datetime.fromisoformat(str(card.get("created_at") or ""))
    except ValueError:
        return None


def _is_later(candidate: datetime | None, base: datetime | None) -> bool:
    if candidate is None or base is None:
        return False
    if (candidate.tzinfo is None) != (base.tzinfo is None):
        return False
    return candidate > base


def _sibling_index(cards: Iterable[Mapping[str, Any]]) -> dict[str, list[_Sibling]]:
    index: dict[str, list[_Sibling]] = {}
    for card in cards:
        topic = str(card.get("topic") or "")
        status = task_store.canonical_status(card)
        if topic and status in _SIBLING_DONE_STATUSES + _SIBLING_LIVE_STATUSES:
            index.setdefault(topic, []).append(
                (_created_at(card), str(card.get("task_id") or ""), status)
            )
    return index


def _earliest_later_sibling(
    index: Mapping[str, list[_Sibling]], card: Mapping[str, Any], statuses: tuple[str, ...],
) -> _Sibling | None:
    task_id = str(card.get("task_id") or "")
    created = _created_at(card)
    later = [
        sibling for sibling in index.get(str(card.get("topic") or ""), ())
        if sibling[1] != task_id and sibling[2] in statuses and _is_later(sibling[0], created)
    ]
    return min(later, key=lambda sibling: (sibling[0], sibling[1]), default=None)


def _needfix_id(task_id: str) -> str | None:
    match = _NEEDFIX_CARD_ID.fullmatch(task_id)
    return match.group(1) if match else None


def _store_needfix_status(root: Path) -> Callable[[str], str | None]:
    db_file = root.joinpath(*needfix_store.NEEDFIX_DB_REL)

    @functools.cache  # per triage call: rework cards share one NeedFix; raises are not cached
    def read(needfix_id: str) -> str | None:
        if not db_file.is_file():  # get_needfix would create a missing database
            return None
        try:
            record = needfix_store.get_needfix(root, needfix_id)
        except needfix_store.NeedFixNotFoundError:
            return None
        return str(record.get("status") or "") or None

    return read


def _sealed_delta(root: Path, predecessor: Any) -> bool:
    # worker_workspace also runs as a bare script; core and task_store import it lazily too.
    from aiworkhub import worker_workspace

    return worker_workspace.has_verified_rework_delta(predecessor, authority_repo=root)


def _read_card(
    root: Path, readiness: task_store.StorageReadiness, row: Mapping[str, Any],
) -> Mapping[str, Any]:
    card, failure = _attempt(
        "card_unreadable", task_store.get_task, root, str(row.get("task_id") or ""),
        readiness=readiness,
    )
    return card if card is not None else {**row, _READ_FAILURE: failure or "card_missing"}


def _read_store(
    root: Path, topic: str | None, limit: int,
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]], int, list[str]]:
    readiness = task_store.storage_readiness(root)
    listed = task_store.list_tasks(root, status="blocked", limit=_STORE_SCAN_CAP)
    capped = ["blocked"] if len(listed) >= _STORE_SCAN_CAP else []
    if topic:
        listed = [row for row in listed if str(row.get("topic") or "") == topic]
    chosen = listed[:limit]
    blocked = [_read_card(root, readiness, row) for row in chosen]
    topics = {str(row.get("topic") or "") for row in chosen} - {""}
    others: list[Mapping[str, Any]] = []
    for status in (_SIBLING_DONE_STATUSES + _SIBLING_LIVE_STATUSES if topics else ()):
        rows = task_store.list_tasks(root, status=status, limit=_STORE_SCAN_CAP)
        if len(rows) >= _STORE_SCAN_CAP:
            capped.append(status)
        others.extend(row for row in rows if str(row.get("topic") or "") in topics)
    return blocked, others, len(listed), capped


def _split_injected(
    cards: Iterable[Mapping[str, Any]], topic: str | None, limit: int,
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]], int]:
    blocked: list[Mapping[str, Any]] = []
    others: list[Mapping[str, Any]] = []
    for card in cards:
        (blocked if task_store.canonical_status(card) == "blocked" else others).append(card)
    if topic:
        blocked = [card for card in blocked if str(card.get("topic") or "") == topic]
    return blocked[:limit], others, len(blocked)


def _classify_blocked(
    card: Mapping[str, Any],
    *,
    root: Path,
    siblings: Mapping[str, list[_Sibling]],
    needfix_status: Callable[[str], str | None],
) -> dict[str, Any]:
    # core imports this module, so the owner of the failure-class read is imported lazily.
    from aiworkhub import core

    task_id = str(card.get("task_id") or "")
    substatus = str(card.get("terminal_substatus") or "").strip() or None
    row: dict[str, Any] = {
        "task_id": task_id,
        "topic": str(card.get("topic") or ""),
        "terminal_substatus": substatus,
        "failure_class": core.recorded_failure_class(card) or None,
    }

    def decide(recommendation: str, reason: str, sibling: str | None = None) -> dict[str, Any]:
        row.update(recommendation=recommendation, reason=reason)
        if sibling:
            row["sibling_task_id"] = sibling
        return row

    if card.get(_READ_FAILURE):
        return decide("keep", f"Card could not be read ({card[_READ_FAILURE]}).")

    needfix_id = _needfix_id(task_id)
    closed, needfix_failure = (
        _attempt("needfix_status_unreadable", needfix_status, needfix_id)
        if needfix_id else (None, "")
    )
    if closed in NEEDFIX_CLOSED_STATUSES:
        return decide("archive", f"NeedFix {needfix_id} is {closed}.")
    done = _earliest_later_sibling(siblings, card, _SIBLING_DONE_STATUSES)
    if done:
        return decide("archive", f"Later same-topic card {done[1]} is {done[2]}.", done[1])
    if needfix_failure:
        return decide("keep", f"NeedFix status could not be read ({needfix_failure}).")
    live = _earliest_later_sibling(siblings, card, _SIBLING_LIVE_STATUSES)
    if live:
        return decide("supersede", f"Later same-topic card {live[1]} is {live[2]}.", live[1])
    if card.get("rework_predecessor"):
        verified, delta_failure = _attempt(
            "rework_delta_unreadable", _sealed_delta, root, card["rework_predecessor"],
        )
        if verified:
            return decide("replay", "Rework predecessor has a verified sealed delta.")
        if delta_failure:
            return decide("keep", f"Rework delta could not be verified ({delta_failure}).")
    if substatus in RELAUNCH_SUBSTATUSES:
        return decide("relaunch", f"terminal_substatus {substatus} is an operational failure.")
    if never_started_request_id(card):
        return decide(
            "relaunch",
            f"terminal_substatus {substatus} records {NEVER_STARTED_REASON}: no worker ever ran, "
            "so the episode is operational and retry_terminal requeues it.",
        )
    if substatus:
        return decide(
            "keep", f"terminal_substatus {substatus} is not operational and no closing evidence exists.",
        )
    return decide("keep", "No terminal_substatus is recorded and no closing evidence exists.")


def build_blocked_triage(
    *,
    root: str | Path,
    topic: str | None = None,
    limit: int = 500,
    sample: int = 10,
    cards: Iterable[Mapping[str, Any]] | None = None,
    needfix_status: Callable[[str], str | None] | None = None,
) -> dict[str, Any]:
    """Read-only blocked-card triage: one recommendation per card, never an action."""
    # Test seams: `cards` (non-blocked entries are sibling evidence) and `needfix_status`.
    root = Path(root)
    limit = max(1, min(int(limit), _STORE_SCAN_CAP))
    sample = max(0, int(sample))
    envelope = {
        "tool": "aiworkhub_task_stale_recovery_recommend",
        "contract": TRIAGE_CONTRACT,
        "readonly": True,
        "scope": "blocked",
        "topic": topic,
    }
    try:
        if cards is None:
            blocked, others, matching, capped = _read_store(root, topic, limit)
        else:
            blocked, others, matching = _split_injected(cards, topic, limit)
            capped = []
    except (task_store.TaskStoreError, sqlite3.Error, OSError) as exc:
        return {
            **envelope,
            "ok": False,
            "error": "blocked_queue_unreadable",
            "detail": f"{type(exc).__name__}: {exc}"[:200],
            "authority_flags": _authority_flags(),
        }
    siblings = _sibling_index(others)
    lookup = needfix_status if needfix_status is not None else _store_needfix_status(root)
    # Sequential: in-memory classification, so process-pool overhead exceeds the work.
    rows = [
        _classify_blocked(card, root=root, siblings=siblings, needfix_status=lookup)
        for card in blocked
    ]
    rows.sort(key=lambda row: (RECOMMENDATION_RANK[row["recommendation"]], row["task_id"]))
    by_recommendation = {name: 0 for name in RECOMMENDATION_RANK}
    by_substatus: dict[str, int] = {}
    groups: dict[str, list[dict[str, Any]]] = {name: [] for name in RECOMMENDATION_RANK}
    for row in rows:
        name = row["recommendation"]
        by_recommendation[name] += 1
        if len(groups[name]) < sample:
            groups[name].append(row)
        substatus = row["terminal_substatus"] or "unrecorded"
        by_substatus[substatus] = by_substatus.get(substatus, 0) + 1
    return {
        **envelope,
        "counts": {
            "blocked": len(rows),
            "by_recommendation": by_recommendation,
            "by_substatus": dict(sorted(by_substatus.items())),
        },
        "groups": groups,
        "bounds": {
            "limit": limit,
            "sample": sample,
            "blocked_matching": matching,
            "truncated": matching > len(rows),
            "scan_capped": capped,
        },
        "authority_flags": _authority_flags(),
    }
