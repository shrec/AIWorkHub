"""Per-card phase timeline for decided tasks, read-only and bounded.

Every duration is derived from timestamps and timings the task system already records:

* ``queue``: ``tasks.created_at`` -> ``tasks.claimed_at``.
* ``worker``: ``tasks.started_at`` -> ``terminal_review.evidence.finished_at`` (worker done).
* ``validation``: declared validation rows (``duration_seconds``, a row reused from an earlier
  run costs nothing here) + finalization neighbour tests (``full_validation_snapshot.
  neighbour_tests``) + workspace ``provisioning_timings_ms.total``.
* ``finalize_other``: the rest of finalization, worker done -> ``terminal_review.recorded_at``
  (review ready) minus ``validation``.
* ``reviewers``: review ready -> the last reviewer receipt (card ``reviewer_receipts[].
  recorded_at`` or a ``reviewer`` attempt row of the process ledger for the same task).
* ``manager_wait``: ``manager_ready_at`` -> the accept decision (``accept_evidence.started_at``).
* ``accept``: the accept decision -> ``accepted_at``.

A missing, unparsable or inverted input makes that phase UNKNOWN for that card with a named
cause; it is never reported as zero, and unknowns are counted apart from the known values the
median and p90 are taken over.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

SCHEMA_ID = "aiworkhub.sdlc_phase_timeline.v1"
UNKNOWN = "UNKNOWN"
PHASES = (
    "queue", "worker", "validation", "finalize_other", "reviewers", "manager_wait", "accept",
)
VALIDATION_PARTS = ("declared", "neighbour", "provisioning")
MAX_SLOWEST_LISTED = 10
MAX_CARD_TIMELINE_BYTES = 1024 * 1024
# A finalization remainder this far below zero is rounding between two clocks, not a gap.
_REMAINDER_TOLERANCE_SECONDS = 0.5
PERCENTILE_METHOD = "median: midpoint of the two middle values; p90: nearest rank"

_EVIDENCE = "$.terminal_review.evidence"
_SNAPSHOT = _EVIDENCE + ".full_validation_snapshot"
_NEIGHBOURS = _SNAPSHOT + ".neighbour_tests"
# Order is the field order of the decoded json_extract array.
_CARD_PATHS: tuple[tuple[str, str], ...] = (
    ("review_ready_at", "$.terminal_review.recorded_at"),
    ("worker_done_at", _EVIDENCE + ".finished_at"),
    ("validations", _EVIDENCE + ".validations"),
    ("validation", _EVIDENCE + ".validation"),
    ("snapshot_schema", _SNAPSHOT + ".schema_id"),
    ("neighbour_seconds", _NEIGHBOURS + ".duration_seconds"),
    ("neighbour_rerun_seconds", _NEIGHBOURS + ".neighbour_flaky_rerun.duration_seconds"),
    ("workspace_provisioning_ms", _EVIDENCE + ".workspace.provisioning_timings_ms.total"),
    ("provisioning_ms", _EVIDENCE + ".provisioning_timings_ms.total"),
    ("reviewer_receipts", "$.reviewer_receipts"),
    ("manager_ready_at", "$.manager_ready_at"),
    ("accept_started_at", "$.accept_evidence.started_at"),
    ("accepted_at", "$.accepted_at"),
)
_TIMELINE_SQL = (
    "SELECT task_id, created_at, claimed_at, started_at, json_valid(card_json), "
    "CASE WHEN json_valid(card_json) THEN json_extract(card_json, "
    + ", ".join(f"'{path}'" for _, path in _CARD_PATHS)
    + ") END, "
    f"CASE WHEN json_valid(card_json) THEN json_type(card_json, '{_NEIGHBOURS}') END "
    "FROM tasks WHERE task_id IN ({})"
)


@dataclass(frozen=True)
class PhaseEvidence:
    """Decided tasks' timeline inputs; ``available`` is False with a typed ``failure``."""

    records: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    available: bool = False
    failure: str | None = None


def read_phase_evidence(
    conn: sqlite3.Connection, task_ids: Sequence[str], *, chunk: int
) -> PhaseEvidence:
    """Timeline inputs of ``task_ids`` in bounded chunks, extracting only the named paths.

    An unreadable store fails the whole read closed; a malformed or oversized card only makes
    its own card-derived phases unknown, and a task without a card row is simply absent.
    """

    ids = list(dict.fromkeys(task_ids))
    records: dict[str, dict[str, Any]] = {}
    step = max(1, int(chunk))
    try:
        for start in range(0, len(ids), step):
            part = ids[start:start + step]
            sql = _TIMELINE_SQL.format(",".join("?" * len(part)))
            for task_id, created, claimed, started, valid, fields, neighbours in conn.execute(
                sql, part
            ):
                record: dict[str, Any] = {
                    "created_at": created, "claimed_at": claimed, "started_at": started,
                    "card_failure": None,
                }
                if not valid:
                    record["card_failure"] = "card_malformed"
                elif isinstance(fields, str) and len(fields) > MAX_CARD_TIMELINE_BYTES:
                    record["card_failure"] = "card_evidence_oversized"
                else:
                    decoded = json.loads(fields) if isinstance(fields, str) else None
                    if not isinstance(decoded, list) or len(decoded) != len(_CARD_PATHS):
                        record["card_failure"] = "card_malformed"
                    else:
                        record.update(zip((name for name, _ in _CARD_PATHS), decoded))
                        record["neighbour_present"] = neighbours not in (None, "null")
                records[str(task_id)] = record
    except sqlite3.Error:
        return PhaseEvidence(failure="store_error")
    except (ValueError, RecursionError):
        return PhaseEvidence(failure="card_malformed")
    return PhaseEvidence(records, available=True)


def _instant(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _seconds(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and number >= 0 else None


def _span(record: Mapping[str, Any], start: str, end: str) -> tuple[float | None, str | None]:
    moments: list[datetime] = []
    for name in (start, end):
        value = record.get(name)
        if value is None or value == "":
            return None, f"{name}_missing"
        moment = _instant(value)
        if moment is None:
            return None, f"{name}_unparsable"
        moments.append(moment)
    seconds = (moments[1] - moments[0]).total_seconds()
    if seconds < 0:
        return None, "negative_interval"
    return round(seconds, 3), None


def _declared_seconds(record: Mapping[str, Any]) -> float | None:
    rows = record.get("validations")
    if not isinstance(rows, list):
        rows = record.get("validation")
    if not isinstance(rows, list) or not rows:
        return None
    total = 0.0
    for row in rows:
        if not isinstance(row, Mapping):
            return None
        if row.get("reused_from"):
            continue  # reused from an earlier run: finalization spent no time on it
        seconds = _seconds(row.get("duration_seconds"))
        if seconds is None:
            return None
        total += seconds
    return total


def _neighbour_seconds(record: Mapping[str, Any]) -> float | None:
    if not record.get("snapshot_schema"):
        return None
    if not record.get("neighbour_present"):
        return 0.0  # the finalization snapshot selected no neighbours
    seconds = _seconds(record.get("neighbour_seconds"))
    rerun = record.get("neighbour_rerun_seconds")
    if seconds is None or (rerun is not None and _seconds(rerun) is None):
        return None
    return seconds + (_seconds(rerun) or 0.0)


def _provisioning_seconds(record: Mapping[str, Any]) -> float | None:
    for name in ("workspace_provisioning_ms", "provisioning_ms"):
        millis = _seconds(record.get(name))
        if millis is not None:
            return millis / 1000.0
    return None


def _validation_parts(record: Mapping[str, Any]) -> dict[str, float | None]:
    return {
        "declared": _declared_seconds(record),
        "neighbour": _neighbour_seconds(record),
        "provisioning": _provisioning_seconds(record),
    }


def _last_reviewer_receipt(
    record: Mapping[str, Any], reviewer_times: Iterable[Any]
) -> str | None:
    ready = _instant(record.get("review_ready_at"))
    receipts = record.get("reviewer_receipts")
    stamps = [
        receipt.get("recorded_at") for receipt in receipts if isinstance(receipt, Mapping)
    ] if isinstance(receipts, list) else []
    best: tuple[datetime, str] | None = None
    for stamp in (*stamps, *reviewer_times):
        moment = _instant(stamp)
        if moment is None or (ready is not None and moment < ready):
            continue  # an earlier round's receipt is not this attempt's review
        if best is None or moment > best[0]:
            best = (moment, str(stamp))
    return best[1] if best else None


def derive_card_phases(
    record: Mapping[str, Any] | None, reviewer_times: Iterable[Any] = ()
) -> dict[str, Any]:
    """Durations (seconds) per phase for one card; an unknown phase is None with its cause."""

    if record is None:
        return {
            "phases": dict.fromkeys(PHASES),
            "causes": dict.fromkeys(PHASES, "task_card_missing"),
            "validation_parts": dict.fromkeys(VALIDATION_PARTS), "total_seconds": None,
        }
    phases: dict[str, float | None] = {}
    causes: dict[str, str] = {}

    def put(name: str, value: tuple[float | None, str | None]) -> None:
        phases[name] = value[0]
        if value[0] is None:
            causes[name] = value[1] or "unknown"

    put("queue", _span(record, "created_at", "claimed_at"))
    failure = record.get("card_failure")
    parts: dict[str, float | None] = dict.fromkeys(VALIDATION_PARTS)
    if failure:
        for name in PHASES[1:]:
            put(name, (None, str(failure)))
        total: tuple[float | None, str | None] = (None, str(failure))
    else:
        put("worker", _span(record, "started_at", "worker_done_at"))
        parts = _validation_parts(record)
        missing = [name for name in VALIDATION_PARTS if parts[name] is None]
        measured = [value for value in parts.values() if value is not None]
        validation = None if missing else round(sum(measured), 3)
        put("validation", (validation, f"{missing[0]}_timing_missing" if missing else None))
        finalization, cause = _span(record, "worker_done_at", "review_ready_at")
        if finalization is not None and validation is None:
            cause = "validation_unknown"
        remainder = (
            None if finalization is None or validation is None
            else round(finalization - validation, 3)
        )
        if remainder is not None and remainder < 0:
            if remainder < -_REMAINDER_TOLERANCE_SECONDS:
                remainder, cause = None, "validation_exceeds_finalization"
            else:
                remainder = 0.0
        put("finalize_other", (remainder, cause))
        last = _last_reviewer_receipt(record, reviewer_times)
        put("reviewers", _span({**record, "last_reviewer_at": last}, "review_ready_at",
                               "last_reviewer_at"))
        put("manager_wait", _span(record, "manager_ready_at", "accept_started_at"))
        put("accept", _span(record, "accept_started_at", "accepted_at"))
        total = _span(record, "created_at", "accepted_at")
    return {
        "phases": phases,
        "causes": causes,
        "validation_parts": {
            name: None if value is None else round(value, 3) for name, value in parts.items()
        },
        "total_seconds": total[0],
    }


def _median(values: Sequence[float]) -> float | None:
    ordered = sorted(values)
    count = len(ordered)
    if not count:
        return None
    middle = count // 2
    value = ordered[middle] if count % 2 else (ordered[middle - 1] + ordered[middle]) / 2
    return round(value, 3)


def _p90(values: Sequence[float]) -> float | None:
    ordered = sorted(values)
    if not ordered:
        return None
    return round(ordered[max(0, math.ceil(0.9 * len(ordered)) - 1)], 3)


def _reviewer_times(rows: Iterable[Mapping[str, Any]]) -> dict[str, list[Any]]:
    """Reviewer attempt-row timestamps from the process ledger, keyed by their task."""

    times: dict[str, list[Any]] = {}
    for row in rows:
        usage = row.get("usage")
        if isinstance(usage, Mapping) and usage.get("role") == "reviewer":
            task_id = str(row.get("task_id") or "")
            if task_id:
                times.setdefault(task_id, []).append(row.get("timestamp"))
    return times


def summarize(
    evidence: PhaseEvidence | None,
    task_ids: Sequence[str],
    attempt_rows: Iterable[Mapping[str, Any]] = (),
    *,
    slowest_limit: int = MAX_SLOWEST_LISTED,
) -> dict[str, Any]:
    """Median/p90 per phase over ``task_ids`` plus a bounded slowest-cards list."""

    evidence = evidence if evidence is not None else PhaseEvidence(failure="phase_evidence_unread")
    ids = list(dict.fromkeys(task_ids))
    reviewers = _reviewer_times(attempt_rows) if evidence.available else {}
    known: dict[str, list[float]] = {name: [] for name in PHASES}
    causes: dict[str, Counter[str]] = {name: Counter() for name in PHASES}
    with_unknown = 0
    ranked: list[tuple[float, str, dict[str, Any]]] = []
    for task_id in ids:
        if evidence.available:
            derived = derive_card_phases(
                evidence.records.get(task_id), reviewers.get(task_id, ())
            )
        else:
            derived = derive_card_phases(None)
            derived["causes"] = dict.fromkeys(PHASES, evidence.failure or "phase_evidence_unread")
        for name in PHASES:
            value = derived["phases"][name]
            if value is None:
                causes[name][derived["causes"][name]] += 1
            else:
                known[name].append(value)
        with_unknown += int(bool(derived["causes"]))
        if derived["total_seconds"] is not None:
            ranked.append((derived["total_seconds"], task_id, derived))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    cap = max(0, int(slowest_limit))
    return {
        "schema_id": SCHEMA_ID,
        "available": evidence.available,
        "unknown_reason": None if evidence.available else evidence.failure,
        "cards": len(ids),
        "percentile_method": PERCENTILE_METHOD,
        "phases": {
            name: {
                "known": len(known[name]),
                "unknown": sum(causes[name].values()),
                "median_seconds": _median(known[name]),
                "p90_seconds": _p90(known[name]),
                "unknown_causes": dict(sorted(causes[name].items())),
            }
            for name in PHASES
        },
        "unknowns": {
            "cards_with_unknown_phase": with_unknown,
            "phase_values_unknown": sum(sum(counter.values()) for counter in causes.values()),
        },
        "slowest_cards": [
            {
                "task_id": task_id,
                "total_seconds": total,
                "phases": {
                    name: UNKNOWN if derived["phases"][name] is None else derived["phases"][name]
                    for name in PHASES
                },
                "validation_parts": {
                    name: UNKNOWN if value is None else value
                    for name, value in derived["validation_parts"].items()
                },
            }
            for total, task_id, derived in ranked[:cap]
        ],
        "slowest_limit": cap,
        "slowest_truncated": len(ranked) > cap,
        "unranked_total_unknown": len(ids) - len(ranked),
    }
