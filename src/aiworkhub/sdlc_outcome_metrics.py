"""Bounded, read-only SDLC outcome metrics from canonical event evidence.

Complete decided-task histories are joined to exact durable reasoning/context attempt receipts
and compared only inside matched route, model, task-family and risk-tier cohorts.
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple

from . import needfix_store, process_event_ledger, runtime_adapters, task_store, vscode_lm_worker
from .sqlite_readonly import connect_readonly

SCHEMA_ID = "aiworkhub.sdlc_outcome_metrics.v1"
COMPARISON_SCHEMA_ID = "aiworkhub.reasoning_context_outcome_comparison.v1"
ATTEMPT_EVENT_SCHEMA_ID = "aiworkhub.reasoning_context_attempt_event.v1"
DEFAULT_LIMIT = 500
MAX_LIMIT = 2000
MAX_EXCLUDED_LISTED = 25
MAX_COHORTS_LISTED = 50
MAX_LEDGER_BYTES = 128 * 1024 * 1024
MAX_LEDGER_ROW_BYTES = 4 * 1024 * 1024
MAX_LEDGER_ROWS = 20_000
PROCESS_LEDGER_REL = (".aiworkhub", "runtime", "process_logs", "process_events.jsonl")
SEVERE_SEVERITIES = frozenset({"critical", "high"})
CLAIM_BOUNDARY = (
    "Observational association within matched route, model, task-family and risk-tier "
    "cohorts only. A sent option is not evidence of provider-internal reasoning, context "
    "capacity is capacity and not consumption, and unobserved cost or time is UNKNOWN and "
    "never extrapolated. No causal quality claim follows."
)
_REJECTION_EVENTS = frozenset({"reject_review", "review_rejected"})
_AUTH_STATUSES = frozenset({401, 403})
_EFFORT_FIELDS = ("requested_profile", "option_status", "option_key", "option_value")
_IDENTITY_NAMES = ("adapter_id", "model")
_FAMILY_BY_TASK_TYPE = {"code": "code", "research": "research", "data_classification": "linguistic"}
_RISK_TIERS = frozenset({"low", "medium", "high", "critical"})
_STATUS_TOKEN = re.compile(r"http_status=([0-9]{3})")
_LABEL = re.compile(r"[a-z][a-z0-9_]{0,63}")
_ATTEMPT_MARKER = b'"reasoning_context_attempt"'
_ROW_FIELDS = (
    "request_id", "task_id", "adapter_id", "model", "timestamp", "failure_kind", "diagnostic",
)
_ROW_MAPPINGS = (
    ("usage", ("role", "cost_observed", "cost_usd", "total_tokens_observed", "total_tokens")),
    ("provider_error", ("owner", "sealed", "http_status")),
    ("terminal_reason", ("code",)),
)
# These cuts stop before whole segments older than every scanned one, so they cannot hide a
# newer row of a request the scan joined; every other scan cause can.
_OLDER_SEGMENT_CUTS = frozenset({"byte_bound", "row_bound"})
_CARD_CHUNK = 500
# A malformed card extracts NULL rather than raising, so it is typed apart from a store error.
_CARD_SQL = (
    "SELECT task_id, CASE WHEN json_valid(card_json) THEN json_extract(card_json, '$.topic', "
    "'$.risk_tier', '$.project_context.task_type') END FROM tasks WHERE task_id IN ({})"
)

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


@dataclass(frozen=True)
class AttemptEvidence:
    """A bounded read of the durable attempt ledger; ``available`` is False when none was read.

    ``truncation_causes`` types every reason ``truncated`` is set; see ``_OLDER_SEGMENT_CUTS``.
    """

    rows: tuple[Mapping[str, Any], ...] = ()
    available: bool = False
    truncated: bool = False
    segments_total: int = 0
    segments_scanned: int = 0
    bytes_scanned: int = 0
    byte_bound: int = MAX_LEDGER_BYTES
    oversized_rows: int = 0
    truncation_causes: tuple[str, ...] = ()


@dataclass(frozen=True)
class TaskCardEvidence:
    """Decided tasks' cards; ``available`` is False with a typed ``failure`` when none was read."""

    cards: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    available: bool = False
    failure: str | None = None


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


def _seconds_between(start: Any, end: Any) -> float | None:
    """Seconds from ``start`` to ``end``; None when either is missing, unparseable or reversed."""

    try:
        first, last = (
            datetime.fromisoformat(str(value).replace("Z", "+00:00")) for value in (start, end)
        )
        seconds = (
            last.replace(tzinfo=last.tzinfo or timezone.utc)
            - first.replace(tzinfo=first.tzinfo or timezone.utc)
        ).total_seconds()
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


class _Acceptance(NamedTuple):
    """The first verified acceptance of one whole history and what preceded it."""

    rejections: int
    request_id: str
    claim_epoch: int
    identity_key: str
    elapsed_seconds: float | None


def _first_verified_acceptance(
    history: Sequence[Mapping[str, Any]], repository_id: str
) -> _Acceptance | None:
    """The first verified acceptance and its rejections, or None when none is verified."""

    ordered = sorted(history, key=_event_order)
    for index, event in enumerate(ordered):
        identity = accepted_outcome_identity(event, repository_id)
        key = _verified_identity_key(identity, repository_id) if identity else None
        if identity is None or key is None:
            continue
        return _Acceptance(
            sum(str(prior.get("event") or "") in _REJECTION_EVENTS for prior in ordered[:index]),
            identity["request_id"],
            identity["accepted_outcome_receipt"]["claim_epoch"],
            key,
            _seconds_between(ordered[0].get("created_at"), event.get("created_at")),
        )
    return None


class _Joined(NamedTuple):
    """One accepted attempt joined to its exact, verified receipt."""

    route: str
    model: str
    effort: tuple[str | None, ...]
    capacity: tuple[int | None, str]
    cost_usd: float | None
    total_tokens: int | None
    family: str = ""
    risk: str = ""


class _Verdict(NamedTuple):
    """Why one decided task did or did not join an exact attempt receipt."""

    reason: str = ""
    detail: str | None = None
    joined: _Joined | None = None


class _Task(NamedTuple):
    acceptance: _Acceptance
    joined: _Joined
    severe: int
    unknown_severity: int


def _label(value: Any) -> str | None:
    return value if isinstance(value, str) and _LABEL.fullmatch(value) else None


def _observed_cost(usage: Any) -> float | None:
    """Observed cost in USD; an unobserved or unusable price is None, never zero."""

    if not isinstance(usage, Mapping) or usage.get("cost_observed") is not True:
        return None
    cost = usage.get("cost_usd")
    if isinstance(cost, bool) or not isinstance(cost, (int, float)):
        return None
    try:
        value = float(cost)
    except OverflowError:
        return None
    return value if math.isfinite(value) and value >= 0 else None


def _observed_tokens(usage: Any) -> int | None:
    """Observed total tokens; a zero the provider did not report as observed is None."""

    if not isinstance(usage, Mapping) or usage.get("total_tokens_observed") is not True:
        return None
    tokens = usage.get("total_tokens")
    return tokens if type(tokens) is int and tokens >= 0 else None


def _provider_status(row: Mapping[str, Any]) -> int | None:
    """HTTP status from the sealed provider error, else the launcher's own diagnostic token."""

    sealed = row.get("provider_error")
    if (
        isinstance(sealed, Mapping)
        and str(sealed.get("owner") or "").strip().casefold() == "provider"
        and sealed.get("sealed") is True
        and type(sealed.get("http_status")) is int
    ):
        return sealed["http_status"]
    diagnostic = row.get("diagnostic")
    for token in (diagnostic.split(":") if isinstance(diagnostic, str) else ()):
        match = _STATUS_TOKEN.fullmatch(token)
        if match:
            return int(match.group(1))
    return None


def _failure_verdict(row: Mapping[str, Any]) -> _Verdict | None:
    """Type a failed attempt from typed evidence only; None for an attempt that ran to a candidate."""

    if not row.get("failure_kind") and not isinstance(row.get("terminal_reason"), Mapping):
        return None
    status = _provider_status(row)
    if status in runtime_adapters.PROVIDER_REFUSAL_STATUSES:
        reason = "provider_auth_refusal" if status in _AUTH_STATUSES else "provider_quota_refusal"
        return _Verdict(reason, f"http_{status}")
    return _Verdict("attempt_failed_non_provider", _label(row.get("failure_kind")))


def _identity_reason(
    task_id: str,
    acceptance: _Acceptance,
    row: Mapping[str, Any],
    identity: Mapping[str, Any],
    repository_id: str,
) -> str:
    """Empty when the attempt is the accepted claim's own.

    ``foreign`` is any repository, task, request or adapter that is not the accepted one;
    ``stale`` is a claim epoch that is not the accepted epoch, including an unrecorded one.
    """

    pinned = {"repo_id": repository_id, "task_id": task_id, "request_id": acceptance.request_id}
    if (
        any(identity.get(key) != value for key, value in pinned.items())
        or row.get("task_id") != task_id
        or row.get("request_id") != acceptance.request_id
        or row.get("adapter_id") not in (None, identity["adapter_id"])
    ):
        return "attempt_identity_foreign"
    epoch = identity.get("claim_epoch")
    if type(epoch) is not int or epoch != acceptance.claim_epoch:
        return "attempt_identity_stale"
    return ""


def _receipt_verdict(
    acceptance: _Acceptance,
    row: Mapping[str, Any],
    identity: Mapping[str, Any],
    receipt: Mapping[str, Any],
    repository_id: str,
) -> _Verdict:
    if receipt.get("send_state") == "unknown":
        return _Verdict("attempt_receipt_unknown", _label(receipt.get("unknown_reason")))
    # The worker's own validator decides what a valid receipt is, against the pinned identity.
    spec = {
        "request_id": acceptance.request_id, "repo_id": repository_id, "model": identity["model"],
    }
    verified = vscode_lm_worker._reasoning_context_attempt_result(
        {"reasoning_context_attempt": receipt}, spec
    )
    if verified["send_state"] != "sent":
        if verified["unknown_reason"] == "receipt_identity_mismatch":
            return _Verdict("attempt_identity_foreign")
        return _Verdict("attempt_receipt_malformed", _label(verified["unknown_reason"]))
    if verified["option_status"] == "unknown":
        # The host sent the request but could not name the option: not an effort setting.
        return _Verdict("attempt_receipt_unknown", _label(verified["unknown_reason"]))
    usage = row.get("usage")
    return _Verdict(joined=_Joined(
        identity["adapter_id"],
        identity["model"],
        tuple(verified[field] for field in _EFFORT_FIELDS),
        (verified["context_capacity_tokens"], verified["context_capacity_source"]),
        _observed_cost(usage),
        _observed_tokens(usage),
    ))


def _attempt_verdict(
    task_id: str, acceptance: _Acceptance, row: Mapping[str, Any], repository_id: str
) -> _Verdict:
    """Join the accepted request to its durable attempt row, or type why it cannot be joined."""

    attempt = row.get("reasoning_context_attempt")
    if not isinstance(attempt, Mapping):
        return _Verdict("attempt_receipt_missing")
    identity, receipt = attempt.get("identity"), attempt.get("receipt")
    if (
        attempt.get("schema_id") != ATTEMPT_EVENT_SCHEMA_ID
        or not isinstance(receipt, Mapping)
        or not isinstance(identity, Mapping)
        or not all(isinstance(identity.get(key), str) and identity[key] for key in _IDENTITY_NAMES)
    ):
        return _Verdict("attempt_receipt_malformed")
    reason = _identity_reason(task_id, acceptance, row, identity, repository_id)
    if reason:
        return _Verdict(reason)
    return _failure_verdict(row) or _receipt_verdict(
        acceptance, row, identity, receipt, repository_id
    )


def _task_family(card: Mapping[str, Any]) -> str:
    """Routing family of a card; mirrors the cost ledger's partition (pinned by a test)."""

    if str(card.get("topic") or "").strip() == "quality_review":
        return "review"
    context = card.get("project_context")
    task_type = context.get("task_type") if isinstance(context, Mapping) else None
    return _FAMILY_BY_TASK_TYPE.get(str(task_type or "").strip().lower(), "unknown")


def _task_risk(card: Mapping[str, Any]) -> str:
    risk = str(card.get("risk_tier") or "").strip().lower()
    return risk if risk in _RISK_TIERS else "unknown"


def _task_verdict(
    task_id: str,
    acceptance: _Acceptance,
    indexed: Mapping[str, Mapping[str, Any]],
    attempts: AttemptEvidence,
    cards: TaskCardEvidence,
    repository_id: str,
) -> _Verdict:
    """Type one decided task: joined to its exact attempt and matchable, or excluded."""

    if not attempts.available:
        return _Verdict("attempt_ledger_unavailable")
    row = indexed.get(acceptance.request_id)
    if row is None:
        # Only a complete scan can call an absent row missing; a cut scan can only say UNKNOWN.
        return _Verdict(
            "attempt_receipt_outside_scan_bound" if attempts.truncated else "attempt_receipt_missing"
        )
    verdict = _attempt_verdict(task_id, acceptance, row, repository_id)
    if verdict.joined is None:
        return verdict
    # An unread card store says nothing about family or risk; only a read card can lack them.
    if not cards.available:
        return _Verdict("task_card_unavailable", cards.failure)
    card = cards.cards.get(task_id)
    if card is None:
        return _Verdict("task_card_missing")
    family, risk = _task_family(card), _task_risk(card)
    if family == "unknown":
        return _Verdict("task_family_unknown")
    if risk == "unknown":
        return _Verdict("risk_tier_unknown")
    return _Verdict(joined=verdict.joined._replace(family=family, risk=risk))


def _card_evidence(
    task_cards: TaskCardEvidence | Mapping[str, Mapping[str, Any]] | None,
) -> TaskCardEvidence:
    if isinstance(task_cards, TaskCardEvidence):
        return task_cards
    if task_cards is None:
        return TaskCardEvidence(failure="not_supplied")
    return TaskCardEvidence(task_cards, available=True)


def _index_attempts(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Mapping[str, Any]], int]:
    """Newest row per request id, and how many rows a newer one superseded."""

    newest: dict[str, Mapping[str, Any]] = {}
    seen = 0
    for row in rows:
        request_id = row.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            continue
        seen += 1
        held = newest.get(request_id)
        if held is None or str(row.get("timestamp") or "") >= str(held.get("timestamp") or ""):
            newest[request_id] = row
    return newest, seen - len(newest)


def _severity_by_identity(
    needfix_rows: Sequence[Mapping[str, Any]], repository_id: str
) -> dict[str, tuple[int, int]]:
    """(severe, unknown-severity) NeedFix counts for each exact accepted-outcome identity."""

    counts: dict[str, list[int]] = {}
    for row in needfix_rows:
        key = _verified_identity_key(row.get("caused_by"), repository_id)
        if key is None:
            continue
        severity = row.get("severity")
        tally = counts.setdefault(key, [0, 0])
        if isinstance(severity, str) and severity in SEVERE_SEVERITIES:
            tally[0] += 1
        elif not isinstance(severity, str) or severity not in needfix_store.SEVERITIES:
            tally[1] += 1
    return {key: (severe, unknown) for key, (severe, unknown) in counts.items()}


def _arm_id(effort: Sequence[str | None]) -> str:
    return "|".join(part or "-" for part in effort)


def _metric(
    numerator: float | int | None,
    denominator: int,
    covered: int,
    total: int,
    truncated: bool,
    **extra: Any,
) -> dict[str, Any]:
    """One metric with its sample size, denominator, coverage, UNKNOWN count and truncation."""

    return {
        "numerator": numerator,
        "denominator": denominator,
        "sample_size": covered,
        "evidence_covered": covered,
        "evidence_total": total,
        "unknown": total - covered,
        "truncated": truncated,
        **extra,
    }


def _observed_metric(
    values: Sequence[float | None], truncated: bool, digits: int | None = None
) -> dict[str, Any]:
    """A sum over the observed values only: UNKNOWN (None) when none was observed, never zero."""

    observed = [value for value in values if value is not None]
    numerator = None
    if observed:
        numerator = sum(observed) if digits is None else round(sum(observed), digits)
    return _metric(numerator, len(observed), len(observed), len(values), truncated)


def _arm_metrics(tasks: Sequence[_Task], cut: bool, needfix_cut: bool) -> dict[str, Any]:
    count = len(tasks)
    capacity = [task.joined.capacity for task in tasks]
    sizes = [size for size, _ in capacity if size is not None]
    return {
        "first_pass_acceptance": _metric(
            sum(task.acceptance.rejections == 0 for task in tasks), count, count, count, cut
        ),
        "review_rounds_per_accepted_task": _metric(
            sum(task.acceptance.rejections + 1 for task in tasks), count, count, count, cut
        ),
        "severe_findings": _metric(
            sum(task.severe for task in tasks),
            count,
            0 if needfix_cut else count,
            count,
            cut or needfix_cut,
            unknown_severity=sum(task.unknown_severity for task in tasks),
        ),
        "elapsed_seconds": _observed_metric(
            [task.acceptance.elapsed_seconds for task in tasks], cut, 3
        ),
        "accepted_attempt_total_tokens": _observed_metric(
            [task.joined.total_tokens for task in tasks], cut
        ),
        "accepted_attempt_cost_usd": _observed_metric(
            [task.joined.cost_usd for task in tasks], cut, 6
        ),
        "context_capacity": {
            **_metric(None, len(sizes), len(sizes), count, cut),
            "sources": dict(sorted(Counter(source for _, source in capacity).items())),
            "tokens_min": min(sizes, default=None),
            "tokens_max": max(sizes, default=None),
            "basis": "model_capacity_not_consumption",
        },
    }


def _cohort_rows(
    cells: Mapping[tuple[str, ...], Mapping[tuple[str | None, ...], Sequence[_Task]]],
    cut: bool,
    needfix_cut: bool,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for cell in sorted(cells):
        arms = cells[cell]
        for effort in sorted(arms, key=_arm_id):
            rows.append({
                "route": cell[0],
                "model": cell[1],
                "task_family": cell[2],
                "risk_tier": cell[3],
                "arm_id": _arm_id(effort),
                "effort_setting": dict(zip(_EFFORT_FIELDS, effort)),
                "comparable": len(arms) > 1,
                "cell_effort_settings": len(arms),
                "sample_size": len(arms[effort]),
                "metrics": _arm_metrics(arms[effort], cut, needfix_cut),
            })
    return rows


def _ledger_summary(
    attempts: AttemptEvidence,
    indexed: Mapping[str, Mapping[str, Any]],
    superseded: int,
    accepted: Mapping[str, _Acceptance],
) -> dict[str, Any]:
    matched = {task.request_id for task in accepted.values()}.intersection(indexed)
    return {
        "available": attempts.available,
        "truncated": attempts.truncated,
        "truncation_reasons": list(attempts.truncation_causes),
        "segments_total": attempts.segments_total,
        "segments_scanned": attempts.segments_scanned,
        "bytes_scanned": attempts.bytes_scanned,
        "byte_bound": attempts.byte_bound,
        "oversized_rows_skipped": attempts.oversized_rows,
        "receipt_rows": len(indexed),
        "receipt_rows_superseded": superseded,
        "receipt_rows_unmatched": len(indexed) - len(matched),
    }


def _compare_matched_outcomes(
    selected: Sequence[str],
    excluded: Sequence[Mapping[str, str]],
    accepted: Mapping[str, _Acceptance],
    attempts: AttemptEvidence,
    cards: TaskCardEvidence,
    needfix_rows: Sequence[Mapping[str, Any]],
    *,
    repository_id: str,
    population_cut: bool,
    needfix_cut: bool,
) -> dict[str, Any]:
    """Join complete decided tasks to exact attempt receipts; compare matched cohorts only."""

    unjoined = {entry["task_id"]: entry["reason"] for entry in excluded}
    indexed, superseded = _index_attempts(attempts.rows)
    severity = _severity_by_identity(needfix_rows, repository_id)
    reasons: Counter[str] = Counter()
    statuses: Counter[str] = Counter()
    listed: list[dict[str, str]] = []
    cells: dict[tuple[str, ...], dict[tuple[str | None, ...], list[_Task]]] = {}
    for task_id in selected:
        if task_id in accepted:
            verdict = _task_verdict(
                task_id, accepted[task_id], indexed, attempts, cards, repository_id
            )
        else:
            verdict = _Verdict(unjoined[task_id])
        joined = verdict.joined
        if joined is None:
            reasons[verdict.reason] += 1
            detail = {"detail": verdict.detail} if verdict.detail else {}
            listed.append({"task_id": task_id, "reason": verdict.reason, **detail})
            continue
        acceptance = accepted[task_id]
        statuses[joined.effort[1]] += 1
        cell = (joined.route, joined.model, joined.family, joined.risk)
        cells.setdefault(cell, {}).setdefault(joined.effort, []).append(
            _Task(acceptance, joined, *severity.get(acceptance.identity_key, (0, 0)))
        )

    joined_count = sum(len(tasks) for arms in cells.values() for tasks in arms.values())
    comparable_cells = [cell for cell, arms in cells.items() if len(arms) > 1]
    comparable = sum(len(tasks) for cell in comparable_cells for tasks in cells[cell].values())
    unmatched = joined_count - comparable
    # A cut that only dropped older whole segments matters only if it left a decided task
    # unjoined; any other (or untyped) cause may hide a newer row of an already joined request.
    older_only = bool(attempts.truncation_causes) and set(attempts.truncation_causes) <= (
        _OLDER_SEGMENT_CUTS
    )
    scan_cut = attempts.truncated and (
        not older_only or bool(reasons["attempt_receipt_outside_scan_bound"])
    )
    cohorts = _cohort_rows(cells, population_cut or scan_cut, needfix_cut)
    if comparable:
        state = "MATCHED_OBSERVATIONAL"
    else:
        state = "NO_MATCHED_COHORT" if joined_count else "UNKNOWN"
    return {
        "schema_id": COMPARISON_SCHEMA_ID,
        "state": state,
        "claim_boundary": CLAIM_BOUNDARY,
        "attempt_ledger": _ledger_summary(attempts, indexed, superseded, accepted),
        "population": {
            "selected": len(selected),
            "joined": joined_count,
            "comparable": comparable,
            "unmatched": unmatched,
            "unmatched_reasons": {"single_effort_arm": unmatched} if unmatched else {},
            "option_status_counts": dict(sorted(statuses.items())),
            "excluded": dict(sorted(reasons.items())),
            "excluded_listed": sorted(listed, key=lambda entry: entry["task_id"])[
                :MAX_EXCLUDED_LISTED
            ],
            "excluded_truncated": len(listed) > MAX_EXCLUDED_LISTED,
            "truncated": population_cut,
        },
        "cells": {"total": len(cells), "comparable": len(comparable_cells)},
        "cohorts": cohorts[:MAX_COHORTS_LISTED],
        "cohorts_truncated": len(cohorts) > MAX_COHORTS_LISTED,
    }


def aggregate(
    task_events: Sequence[Mapping[str, Any]],
    needfix_rows: Sequence[Mapping[str, Any]],
    *,
    repository_id: str,
    limit: int = DEFAULT_LIMIT,
    cohort: DecidedTaskCohort | None = None,
    attempts: AttemptEvidence | None = None,
    task_cards: TaskCardEvidence | Mapping[str, Mapping[str, Any]] | None = None,
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
    accepted: dict[str, _Acceptance] = {}
    for task_id in selected:
        if task_id not in complete:
            incomplete_tasks += 1
            excluded.append({"task_id": task_id, "reason": "history_incomplete"})
            continue
        acceptance = _first_verified_acceptance(per_task[task_id], repository_id)
        if acceptance is None:
            unknown_tasks += 1
            excluded.append({"task_id": task_id, "reason": "accepted_outcome_unverified"})
            continue
        accepted[task_id] = acceptance
        accepted_tasks += 1
        first_pass += int(acceptance.rejections == 0)
        review_rounds += acceptance.rejections + 1

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
        "reasoning_context_outcome_comparison": _compare_matched_outcomes(
            selected,
            excluded,
            accepted,
            attempts if attempts is not None else AttemptEvidence(),
            _card_evidence(task_cards),
            rows,
            repository_id=repository_id,
            population_cut=events_truncated,
            needfix_cut=len(needfix_rows) > cap,
        ),
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


def read_task_cards(conn: sqlite3.Connection, task_ids: Sequence[str]) -> TaskCardEvidence:
    """Family and risk fields of the decided tasks' cards in bounded chunks.

    An unreadable store or any malformed card fails the whole read closed with a typed failure
    rather than biasing the subset; a task the readable store holds no card for is just absent.
    """

    ids = list(dict.fromkeys(task_ids))
    cards: dict[str, dict[str, Any]] = {}
    try:
        for start in range(0, len(ids), _CARD_CHUNK):
            chunk = ids[start:start + _CARD_CHUNK]
            for task_id, fields in conn.execute(
                _CARD_SQL.format(",".join("?" * len(chunk))), chunk
            ):
                decoded = json.loads(fields) if isinstance(fields, str) else None
                if not isinstance(decoded, list) or len(decoded) != 3:
                    return TaskCardEvidence(failure="card_malformed")
                topic, risk_tier, task_type = decoded
                cards[task_id] = {
                    "topic": topic,
                    "risk_tier": risk_tier,
                    "project_context": {"task_type": task_type},
                }
    except sqlite3.Error:
        return TaskCardEvidence(failure="store_error")
    except ValueError:
        return TaskCardEvidence(failure="card_malformed")
    return TaskCardEvidence(cards, available=True)


def _project_attempt_row(row: Mapping[str, Any]) -> dict[str, Any]:
    projected = {key: row.get(key) for key in _ROW_FIELDS}
    projected["reasoning_context_attempt"] = row["reasoning_context_attempt"]
    for name, fields in _ROW_MAPPINGS:
        value = row.get(name)
        if isinstance(value, Mapping):
            projected[name] = {key: value.get(key) for key in fields}
    return projected


def _collect_attempt_row(line: bytes, rows: list[dict[str, Any]]) -> None:
    try:
        row = json.loads(line)
    except (ValueError, RecursionError):
        return
    if (
        isinstance(row, dict)
        and isinstance(row.get("request_id"), str)
        and row["request_id"]
        and isinstance(row.get("reasoning_context_attempt"), Mapping)
    ):
        rows.append(_project_attempt_row(row))


def _scan_segment(
    path: Path, size: int, rows: list[dict[str, Any]]
) -> tuple[int, int, bool]:
    """Collect projected receipt rows from the first ``size`` bytes of one ledger file.

    Returns (bytes read, oversized rows, incomplete). No read passes the lstat snapshot, so a
    live append can neither overrun the byte bound nor keep an oversized drain going; only
    newline-terminated lines are admitted, so a record the snapshot cuts never parses as a
    receipt. ``incomplete`` reports that the file grew or shrank since the snapshot, or that an
    unterminated tail was discarded even at a stable EOF, i.e. a row exists that this scan did
    not admit.
    """

    limit = MAX_LEDGER_ROW_BYTES
    consumed = oversized = 0
    discarded_tail = False
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as handle:
        while consumed < size:
            line = handle.readline(min(limit + 1, size - consumed))
            if not line:
                break
            consumed += len(line)
            if not line.endswith(b"\n"):
                if len(line) > limit:
                    oversized += 1
                    while consumed < size:
                        line = handle.readline(min(1 << 20, size - consumed))
                        if not line:
                            break
                        consumed += len(line)
                        if line.endswith(b"\n"):
                            break
                else:
                    # An unterminated tail is never a receipt, but it is an excluded record.
                    discarded_tail = True
            elif _ATTEMPT_MARKER in line:
                _collect_attempt_row(line, rows)
        incomplete = discarded_tail or consumed != size or bool(handle.read(1))
    return consumed, oversized, incomplete


def read_attempt_evidence(
    process_log: str | Path, *, byte_bound: int = MAX_LEDGER_BYTES
) -> AttemptEvidence:
    """Newest-first, byte-bounded, read-only scan of the process ledger for attempt receipts."""

    segments = process_event_ledger.ledger_paths(Path(process_log))
    rows: list[dict[str, Any]] = []
    scanned = bytes_scanned = oversized = 0
    causes: set[str] = set()
    for segment in reversed(segments):
        try:
            size = segment.lstat().st_size
            if bytes_scanned + size > byte_bound:
                causes.add("byte_bound")
                break
            consumed, skipped, changed = _scan_segment(segment, size, rows)
        except OSError:
            # Older segments are still read, so the skipped rows may be any request's newest.
            causes.add("segment_unreadable")
            continue
        scanned += 1
        bytes_scanned += consumed
        oversized += skipped
        if changed:
            causes.add("segment_incomplete")
        if skipped:
            causes.add("oversized_row")
        # A segment is read whole, so the newest rows are never the ones a row bound drops.
        if len(rows) > MAX_LEDGER_ROWS:
            causes.add("row_bound")
            break
    return AttemptEvidence(
        rows=tuple(rows),
        available=bool(segments),
        truncated=bool(causes),
        segments_total=len(segments),
        segments_scanned=scanned,
        bytes_scanned=bytes_scanned,
        byte_bound=byte_bound,
        oversized_rows=oversized,
        truncation_causes=tuple(sorted(causes)),
    )


def read_repository_metrics(
    repo_root: str | Path,
    *,
    repository_id: str,
    limit: int = DEFAULT_LIMIT,
    process_log_path: str | Path | None = None,
    ledger_byte_bound: int = MAX_LEDGER_BYTES,
) -> dict[str, Any]:
    """Read the stores and process ledger read-only; events are the newest decided tasks' histories."""

    cap = max(1, min(int(limit), MAX_LIMIT))
    readiness = task_store.storage_readiness(Path(repo_root))
    if not readiness.ready:
        raise task_store.StorageNotReadyError(readiness.reason)
    task_conn = connect_readonly(readiness.canonical_db)
    try:
        event_rows, cohort = read_decided_task_cohort(task_conn, cap)
        cards = read_task_cards(task_conn, cohort.selected)
    finally:
        task_conn.close()
    attempts = AttemptEvidence(byte_bound=ledger_byte_bound)
    if cohort.selected:
        attempts = read_attempt_evidence(
            process_log_path or Path(repo_root).joinpath(*PROCESS_LEDGER_REL),
            byte_bound=ledger_byte_bound,
        )
    needfix_path = Path(repo_root).joinpath(*needfix_store.NEEDFIX_DB_REL)
    needfix_rows: list[dict[str, Any]] = []
    if needfix_path.is_file():
        nf_conn = connect_readonly(needfix_path)
        nf_conn.row_factory = sqlite3.Row
        try:
            needfix_rows = [
                {
                    "id": row["id"],
                    "severity": row["severity"] if "severity" in row.keys() else None,
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
        event_rows,
        needfix_rows,
        repository_id=repository_id,
        limit=cap,
        cohort=cohort,
        attempts=attempts,
        task_cards=cards,
    )
