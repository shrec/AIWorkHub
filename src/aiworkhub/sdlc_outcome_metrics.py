"""Bounded, read-only SDLC outcome metrics from canonical event evidence."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from . import needfix_store, task_store

SCHEMA_ID = "aiworkhub.sdlc_outcome_metrics.v1"
DEFAULT_LIMIT = 500
MAX_LIMIT = 2000


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

    payload = _payload(event)
    if str(event.get("event") or "") != "accept_review":
        return None
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


def aggregate(
    task_events: Sequence[Mapping[str, Any]],
    needfix_rows: Sequence[Mapping[str, Any]],
    *,
    repository_id: str,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Compute bounded aggregates without consulting prose or mutable status."""

    cap = max(1, min(int(limit), MAX_LIMIT))
    events = list(task_events)[:cap]
    rows = list(needfix_rows)[:cap]
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

    accepted_tasks = 0
    first_pass = 0
    review_rounds = 0

    def event_order(item: Mapping[str, Any]) -> tuple[int, str]:
        raw = item.get("event_id") or item.get("seq") or 0
        try:
            sequence = int(raw)
        except (TypeError, ValueError):
            sequence = 0
        return sequence, str(item.get("created_at") or "")

    for history in per_task.values():
        ordered = sorted(
            history,
            key=event_order,
        )
        accepted_indexes = [
            index for index, event in enumerate(ordered)
            if accepted_outcome_identity(event, repository_id) is not None
        ]
        if not accepted_indexes:
            continue
        accepted_tasks += 1
        prior = ordered[: accepted_indexes[0]]
        rejections = sum(
            str(event.get("event") or "") in {"reject_review", "review_rejected"}
            for event in prior
        )
        first_pass += int(rejections == 0)
        review_rounds += rejections + 1

    attributed = 0
    unknown = 0
    outside_event_bound = 0
    events_truncated = len(task_events) > cap
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
        "first_pass_acceptance": {
            "numerator": first_pass,
            "denominator": accepted_tasks,
            "evidence_covered": accepted_tasks if len(task_events) <= cap else 0,
            "evidence_total": len(per_task),
        },
        "review_rounds_per_accepted_task": {
            "numerator": review_rounds,
            "denominator": accepted_tasks,
            "evidence_covered": accepted_tasks if len(task_events) <= cap else 0,
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


def read_repository_metrics(
    repo_root: str | Path, *, repository_id: str, limit: int = DEFAULT_LIMIT
) -> dict[str, Any]:
    """Read both stores in SQLite read-only mode and compute one snapshot."""

    cap = max(1, min(int(limit), MAX_LIMIT))
    readiness = task_store.storage_readiness(Path(repo_root))
    if not readiness.ready:
        raise task_store.StorageNotReadyError(readiness.reason)
    task_conn = sqlite3.connect(f"file:{readiness.canonical_db}?mode=ro", uri=True)
    task_conn.row_factory = sqlite3.Row
    try:
        event_rows = [
            dict(row) for row in task_conn.execute(
                "SELECT event_id, task_id, event, payload_json, created_at "
                "FROM task_events ORDER BY event_id DESC LIMIT ?", (cap + 1,)
            ).fetchall()
        ]
    finally:
        task_conn.close()
    needfix_path = Path(repo_root).joinpath(*needfix_store.NEEDFIX_DB_REL)
    needfix_rows: list[dict[str, Any]] = []
    if needfix_path.is_file():
        nf_conn = sqlite3.connect(f"file:{needfix_path}?mode=ro", uri=True)
        nf_conn.row_factory = sqlite3.Row
        try:
            needfix_rows = [
                {
                    "id": row["id"],
                    "caused_by": json.loads(row["caused_by_json"])
                    if "caused_by_json" in row.keys() and row["caused_by_json"] else None,
                }
                for row in nf_conn.execute(
                    "SELECT * FROM needfix ORDER BY created_at DESC LIMIT ?", (cap + 1,)
                ).fetchall()
            ]
        finally:
            nf_conn.close()
    return aggregate(event_rows, needfix_rows, repository_id=repository_id, limit=cap)
