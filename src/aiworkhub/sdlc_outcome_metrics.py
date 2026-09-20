"""Bounded, read-only SDLC outcome metrics from canonical event evidence."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import needfix_store, task_store
from .sqlite_readonly import connect_readonly

SCHEMA_ID = "aiworkhub.sdlc_outcome_metrics.v1"
DEFAULT_LIMIT = 500
MAX_LIMIT = 2000
MAX_EXCLUDED_LISTED = 25
_REJECTION_EVENTS = frozenset({"reject_review", "review_rejected"})

# CROSS JOIN keeps the newest-first accept scan outermost, so LIMIT can only cut the last history.
# Only acceptance payloads are ever parsed, so no other event's payload is loaded.
_DECIDED_COHORT_SQL = """
SELECT e.event_id AS event_id, e.task_id AS task_id, e.event AS event,
       CASE WHEN e.event = 'accept_review' THEN e.payload_json END AS payload_json,
       e.created_at AS created_at
FROM task_events AS a CROSS JOIN task_events AS e ON e.task_id = a.task_id
WHERE a.event = 'accept_review' AND a.task_id <> ''
  AND NOT EXISTS (
    SELECT 1 FROM task_events AS later
    WHERE later.task_id = a.task_id AND later.event = 'accept_review'
      AND later.event_id > a.event_id
  )
ORDER BY a.event_id DESC, e.event_id ASC
LIMIT ?
"""


@dataclass(frozen=True)
class DecidedTaskCohort:
    """Decided tasks in read order and which of them had their whole history read."""

    selected: tuple[str, ...]
    complete: frozenset[str]


def _payload(event: Mapping[str, Any]) -> Mapping[str, Any]:
    value = event.get("payload", event.get("payload_json", {}))
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {}
    return value if isinstance(value, Mapping) else {}


def accepted_outcome_identity(
    event: Mapping[str, Any], repository_id: str
) -> dict[str, Any] | None:
    """Extract an accepted identity only when all receipt evidence is present."""

    if str(event.get("event") or "") != "accept_review":
        return None
    payload = _payload(event)
    receipt = payload.get("accepted_outcome_receipt")
    task_id = str(event.get("task_id") or "")
    request_id = str(payload.get("request_id") or "")
    if not needfix_store.accepted_outcome_receipt_is_well_formed(
        receipt, task_id=task_id, request_id=request_id
    ):
        return None
    identity = {
        "schema_id": needfix_store.CAUSED_BY_SCHEMA_ID,
        "repository_id": str(repository_id),
        "task_id": task_id,
        "request_id": request_id,
        "accepted_outcome_receipt": dict(receipt),
    }
    if not all(identity.values()):
        return None
    return identity


def _verified_identity_key(
    identity: Any, repository_id: str
) -> str | None:
    """Canonical key for an exact, locally valid accepted-outcome identity."""

    fields = {
        "schema_id", "repository_id", "task_id", "request_id",
        "accepted_outcome_receipt",
    }
    if not isinstance(identity, Mapping) or set(identity) != fields:
        return None
    if (
        identity.get("schema_id") != needfix_store.CAUSED_BY_SCHEMA_ID
        or identity.get("repository_id") != repository_id
        or not isinstance(identity.get("task_id"), str)
        or not isinstance(identity.get("request_id"), str)
        or not needfix_store.accepted_outcome_receipt_is_well_formed(
            identity.get("accepted_outcome_receipt"),
            task_id=identity.get("task_id", ""),
            request_id=identity.get("request_id", ""),
        )
    ):
        return None
    return json.dumps(dict(identity), sort_keys=True, separators=(",", ":"))


def _event_order(item: Mapping[str, Any]) -> tuple[int, str]:
    raw = item.get("event_id") or item.get("seq") or 0
    try:
        sequence = int(raw)
    except (TypeError, ValueError):
        sequence = 0
    return sequence, str(item.get("created_at") or "")


def _rejections_before_acceptance(
    history: Sequence[Mapping[str, Any]], repository_id: str
) -> int | None:
    """Rejections before the first verified acceptance, or None when none is verified."""

    ordered = sorted(history, key=_event_order)
    for index, event in enumerate(ordered):
        if accepted_outcome_identity(event, repository_id) is not None:
            return sum(
                str(prior.get("event") or "") in _REJECTION_EVENTS for prior in ordered[:index]
            )
    return None


def aggregate(
    task_events: Sequence[Mapping[str, Any]],
    needfix_rows: Sequence[Mapping[str, Any]],
    *,
    repository_id: str,
    limit: int = DEFAULT_LIMIT,
    cohort: DecidedTaskCohort | None = None,
) -> dict[str, Any]:
    """Compute bounded aggregates without consulting prose or mutable status."""

    cap = max(1, min(int(limit), MAX_LIMIT))
    events = list(task_events)[:cap]
    rows = list(needfix_rows)[:cap]
    events_truncated = len(task_events) > cap
    unique: dict[str, Mapping[str, Any]] = {}
    for index, event in enumerate(events):
        event_id = str(event.get("event_id") or event.get("seq") or f"row:{index}")
        unique.setdefault(event_id, event)

    per_task: dict[str, list[Mapping[str, Any]]] = {}
    identities: set[str] = set()
    for event in unique.values():
        task_id = str(event.get("task_id") or "")
        if task_id:
            per_task.setdefault(task_id, []).append(event)
        identity = accepted_outcome_identity(event, repository_id)
        if identity is not None:
            key = _verified_identity_key(identity, repository_id)
            if key is not None:
                identities.add(key)

    # Only decided tasks whose whole history was read enter the outcome metrics.
    if cohort is None:
        decided = sorted(
            task_id for task_id, history in per_task.items()
            if any(str(event.get("event") or "") == "accept_review" for event in history)
        )
        cohort = DecidedTaskCohort(
            tuple(decided), frozenset() if events_truncated else frozenset(decided)
        )
    selected = cohort.selected
    # A selected task with no events in hand can never be vouched for.
    complete = cohort.complete.intersection(per_task)

    accepted_tasks = 0
    first_pass = 0
    review_rounds = 0
    incomplete_tasks = 0
    unknown_tasks = 0
    excluded: list[dict[str, str]] = []
    for task_id in selected:
        if task_id not in complete:
            incomplete_tasks += 1
            excluded.append({"task_id": task_id, "reason": "history_incomplete"})
            continue
        rejections = _rejections_before_acceptance(per_task[task_id], repository_id)
        if rejections is None:
            unknown_tasks += 1
            excluded.append({"task_id": task_id, "reason": "accepted_outcome_unverified"})
            continue
        accepted_tasks += 1
        first_pass += int(rejections == 0)
        review_rounds += rejections + 1

    attributed = 0
    unknown = 0
    outside_event_bound = 0
    for row in rows:
        cause = row.get("caused_by")
        if not isinstance(cause, Mapping):
            unknown += 1
            continue
        key = _verified_identity_key(cause, repository_id)
        if key is None:
            unknown += 1
        elif key in identities:
            attributed += 1
        else:
            unknown += 1
            outside_event_bound += int(events_truncated)

    return {
        "schema_id": SCHEMA_ID,
        "readonly": True,
        "population_bounds": {
            "limit": cap,
            "task_events_scanned": len(events),
            "canonical_events_after_deduplication": len(unique),
            "needfix_rows_scanned": len(rows),
            "task_events_truncated": events_truncated,
            "needfix_rows_truncated": len(needfix_rows) > cap,
        },
        "decided_task_cohort": {
            "selected": len(selected),
            "complete": accepted_tasks,
            "incomplete": incomplete_tasks,
            "unknown": unknown_tasks,
            "truncated": events_truncated,
            "excluded": excluded[:MAX_EXCLUDED_LISTED],
            "excluded_truncated": len(excluded) > MAX_EXCLUDED_LISTED,
        },
        "first_pass_acceptance": {
            "numerator": first_pass,
            "denominator": accepted_tasks,
            "evidence_covered": accepted_tasks,
            "evidence_total": len(per_task),
        },
        "review_rounds_per_accepted_task": {
            "numerator": review_rounds,
            "denominator": accepted_tasks,
            "evidence_covered": accepted_tasks,
            "evidence_total": len(per_task),
        },
        "escaped_defect_attribution": {
            "numerator": attributed,
            "denominator": len(rows),
            "evidence_covered": attributed if not events_truncated else 0,
            "evidence_total": len(rows),
            "unknown_unattributed": unknown,
            "outside_event_bound_unknown": outside_event_bound,
            "task_event_population_complete": not events_truncated,
        },
    }


def read_decided_task_cohort(
    conn: sqlite3.Connection, limit: int = DEFAULT_LIMIT
) -> tuple[list[dict[str, Any]], DecidedTaskCohort]:
    """Whole histories of the newest accepted tasks, read in one bounded statement."""

    cap = max(1, min(int(limit), MAX_LIMIT))
    # The extra row past ``cap`` only proves truncation.
    cursor = conn.execute(_DECIDED_COHORT_SQL, (cap + 1,))
    names = [column[0] for column in cursor.description]
    rows = [dict(zip(names, row)) for row in cursor.fetchall()]
    selected: list[str] = []
    for row in rows[:cap]:
        if not selected or selected[-1] != row["task_id"]:
            selected.append(row["task_id"])
    complete = set(selected)
    # A row past the bound cuts the last history only when it belongs to that same task.
    if len(rows) > cap and rows[cap]["task_id"] == selected[-1]:
        complete.discard(selected[-1])
    return rows, DecidedTaskCohort(tuple(selected), frozenset(complete))


def read_repository_metrics(
    repo_root: str | Path, *, repository_id: str, limit: int = DEFAULT_LIMIT
) -> dict[str, Any]:
    """Read both stores read-only; task events are the newest decided tasks' whole histories."""

    cap = max(1, min(int(limit), MAX_LIMIT))
    readiness = task_store.storage_readiness(Path(repo_root))
    if not readiness.ready:
        raise task_store.StorageNotReadyError(readiness.reason)
    task_conn = connect_readonly(readiness.canonical_db)
    try:
        event_rows, cohort = read_decided_task_cohort(task_conn, cap)
    finally:
        task_conn.close()
    needfix_path = Path(repo_root).joinpath(*needfix_store.NEEDFIX_DB_REL)
    needfix_rows: list[dict[str, Any]] = []
    if needfix_path.is_file():
        nf_conn = connect_readonly(needfix_path)
        nf_conn.row_factory = sqlite3.Row
        try:
            needfix_rows = [
                {
                    "id": row["id"],
                    "caused_by": json.loads(row["caused_by_json"])
                    if "caused_by_json" in row.keys() and row["caused_by_json"] else None,
                }
                for row in nf_conn.execute(
                    "SELECT * FROM needfix ORDER BY created_at DESC, id DESC LIMIT ?",
                    (cap + 1,),
                ).fetchall()
            ]
        finally:
            nf_conn.close()
    return aggregate(
        event_rows, needfix_rows, repository_id=repository_id, limit=cap, cohort=cohort
    )
