"""NF-2026-02171: what a failed accept proves about the reject that follows it.

A manager reject after a failed ``accept_review`` was recorded as
``candidate_code`` even when the candidate was not at fault: canonical had moved
under it, or the failing command was already red on its base commit.
``classify_terminal_disposition`` never reads prose and the accept's failure
reply was written nowhere durable, so no structured evidence reached the reject.

This module owns both halves. :func:`record_accept_failures` wraps
``accept_review`` and appends one ``task_events`` row per failed attempt,
after accept has returned and released the promotion lock.
:func:`classify_reject` reads the newest such row back for ``reject_review``.
Only structured data decides: the accept's machine error code, its validation
rows and their base-commit triage. A manager's reason text is never consulted.

Everything here is advisory to the two lifecycle calls: a read, triage or write
failure is a warning, never a change to accept's reply or to the reject, and any
doubt leaves the reject classified exactly as it was before this module existed.
"""

from __future__ import annotations

import functools
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Mapping, TypeVar, cast

from . import base_drift_proof as base_drift_proof_module
from . import task_store

logger = logging.getLogger(__name__)

EVENT = "accept_review_failed"
SCHEMA_ID = "aiworkhub.accept_review_failed.v1"
ACTOR = "accept_failure_cause"
FAILURE_CATEGORY_SOURCE = "accept_failure_record"
# Carries a WorkspaceError's validation rows from accept to the wrapper, which
# pops it: the reply a caller receives never contains this key.
PRIVATE_RESULTS_KEY = "_accept_failure_results"

_PROMOTION_CONFLICT_PREFIX = "promotion_merge_conflict:"
_REVALIDATION_PREFIX = "revalidation_failed:"
_COMBINED_TREE_QUALITY_PREFIX = "combined_tree_quality_failed:"
_MAX_ERROR_CHARS = 2_000
_MAX_COMMANDS = 32
_MAX_PAYLOAD_BYTES = 64 * 1024
# Another accept holds this request, or its marker is unreadable: this call
# attempted nothing and must write nothing (NF-2026-01626), so it gets no row.
_NOT_A_FAILED_ACCEPT = frozenset({"accept_in_progress", "accept_in_flight_marker_unreadable"})

_AcceptFn = TypeVar("_AcceptFn", bound=Callable[..., dict[str, Any]])


def record_accept_failures(accept: _AcceptFn) -> _AcceptFn:
    """Wrap ``accept_review``: pop the private key, record a failed attempt's ``ok: False``."""

    @functools.wraps(accept)
    def wrapper(self: Any, request_id: str, task_id: str, **kwargs: Any) -> dict[str, Any]:
        result = accept(self, request_id, task_id, **kwargs)
        if not isinstance(result, dict):
            return result
        results = result.pop(PRIVATE_RESULTS_KEY, None)
        if result.get("ok") is False and result.get("error") not in _NOT_A_FAILED_ACCEPT:
            try:
                _record_failure(self, request_id, task_id, str(result.get("error") or ""), results)
            except Exception as exc:  # noqa: BLE001 -- recording never changes accept's reply
                logger.warning(
                    "accept_failure_cause: no %s row for request %s: %s",
                    EVENT, request_id, type(exc).__name__,
                )
        return result

    return cast(_AcceptFn, wrapper)


def _validation_rows(results: Any) -> list[dict[str, Any]]:
    if not isinstance(results, (list, tuple)):
        return []
    return [dict(row) for row in results if isinstance(row, Mapping)]


def _triage_may_apply(error: str) -> bool:
    """Neighbour and combined-tree refusals judge the candidate's own effect."""
    from .accept_neighbour_tests import NEIGHBOUR_FAILURE_PREFIX

    code = error.removeprefix(_REVALIDATION_PREFIX)
    return not code.startswith((NEIGHBOUR_FAILURE_PREFIX, _COMBINED_TREE_QUALITY_PREFIX))


def _failing_commands(
    request_id: str, task_id: str, rows: list[dict[str, Any]]
) -> list[str]:
    """The failing commands, by the packet's own definition of a failing row."""
    from . import validation_failed_packet

    packet = validation_failed_packet.packet_from_evidence(
        task_id=task_id, request_id=request_id, evidence={"validation": rows}, roles=None
    )
    commands = [
        str(entry.get("command"))
        for entry in packet.get("failing") or []
        if isinstance(entry, Mapping) and entry.get("command")
    ]
    return commands


def _base_triage(
    manager: Any,
    request_id: str,
    task_id: str,
    commands: list[str],
    rows: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """The base-commit triage of ``commands``, or ``None`` when it has no answer."""
    from . import validation_auto_rework, validation_base_triage

    card = task_store.get_task(manager.repo, task_id)
    if not isinstance(card, Mapping):
        return None
    review = card.get("terminal_review")
    evidence = review.get("evidence") if isinstance(review, Mapping) else None
    events = manager._request_events(request_id)
    triage = validation_base_triage.triage(
        manager.repo,
        card=card,
        base_oid=validation_auto_rework._evidence_base_oid(
            evidence if isinstance(evidence, Mapping) else {}
        ),
        adapter_id=str((events[-1] if events else {}).get("adapter_id") or ""),
        commands=commands,
        # The last row per declared command wins, as in validation_auto_rework.
        candidate_rows={
            str(row.get("declared_command") or row.get("command") or ""): row for row in rows
        },
    )
    if not isinstance(triage, Mapping) or triage.get("outcome") not in validation_base_triage.OUTCOMES:
        return None
    return validation_auto_rework._triage_summary(triage)


def _bounded_payload(payload: Mapping[str, Any]) -> str:
    """The row's JSON, keeping the verdict when the facts overflow the bound."""
    text = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, default=str)
    if len(text.encode("utf-8")) <= _MAX_PAYLOAD_BYTES:
        return text
    slim = {key: payload[key] for key in ("schema_id", "request_id", "error") if key in payload}
    triage = payload.get("triage")
    if isinstance(triage, Mapping):
        slim["triage"] = {
            key: str(triage.get(key) or "")[:200] for key in ("schema_id", "outcome", "reason")
        }
    slim["truncated"] = True
    return json.dumps(slim, ensure_ascii=False, sort_keys=True, default=str)


def _record_failure(
    manager: Any, request_id: str, task_id: str, error: str, results: Any
) -> None:
    # Checked first so an unready store costs nothing, least of all a base run.
    _readiness, db_path = task_store._require_ready(manager.repo)
    rows = _validation_rows(results)
    commands: list[str] = []
    triage: dict[str, Any] | None = None
    if rows:
        try:
            commands = _failing_commands(request_id, task_id, rows)
            if _triage_may_apply(error) and len(commands) <= _MAX_COMMANDS:
                triage = _base_triage(manager, request_id, task_id, commands, rows)
        except Exception as exc:  # noqa: BLE001 -- the failure itself is still recorded
            logger.warning(
                "accept_failure_cause: no triage for request %s: %s",
                request_id, type(exc).__name__,
            )
    payload: dict[str, Any] = {
        "schema_id": SCHEMA_ID,
        "request_id": str(request_id),
        "error": error.partition(":stdout=")[0].partition(":stderr=")[0][:_MAX_ERROR_CHARS],
        "commands": commands[:_MAX_COMMANDS],
    }
    if triage is not None:
        payload["triage"] = triage
    conn = task_store._connect(db_path)
    try:
        conn.execute(
            "INSERT INTO task_events(task_id, event, runner, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                str(task_id), EVENT, ACTOR, _bounded_payload(payload),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _newest_failure_row(repo: Path, task_id: str) -> dict[str, Any] | None:
    _readiness, db_path = task_store._require_ready(repo)
    conn = task_store._connect(db_path, readonly=True)
    try:
        row = conn.execute(
            "SELECT payload_json FROM task_events WHERE task_id=? AND event=? "
            "ORDER BY event_id DESC LIMIT 1",
            (str(task_id), EVENT),
        ).fetchone()
    finally:
        conn.close()
    payload = json.loads(row[0]) if row else None
    return payload if isinstance(payload, dict) else None


def _has_confirmed_finding(repo: Path, task_id: str, request_id: str) -> bool:
    """True when a reviewer confirmed a defect, or when that cannot be answered."""
    from . import process_launcher_accept_review

    try:
        rows = process_launcher_accept_review.review_finding_dispositions(
            SimpleNamespace(repo=repo), task_id, request_id
        )
    except Exception as exc:  # noqa: BLE001 -- an unanswered question excuses nothing
        logger.warning(
            "accept_failure_cause: dispositions unreadable for request %s: %s",
            request_id, type(exc).__name__,
        )
        return True
    if not isinstance(rows, list):
        return True
    return any(
        isinstance(row, Mapping) and str(row.get("disposition") or "") == "confirmed"
        for row in rows
    )


def _classify_reject(
    card: Mapping[str, Any],
    task_id: str,
    current_request_id: str,
    repo_root: Callable[[], Path],
) -> tuple[str | None, dict[str, Any] | None]:
    from . import validation_base_triage
    from .learning_commit import FailureCategory

    if not current_request_id:
        return None, None
    repo = repo_root()
    row = _newest_failure_row(repo, task_id)
    if row is None or str(row.get("request_id") or "").strip() != current_request_id:
        return None, None
    error = str(row.get("error") or "")
    triage = row.get("triage")
    conflict = error.startswith(_PROMOTION_CONFLICT_PREFIX)
    base_failed = (
        isinstance(triage, Mapping)
        and triage.get("outcome") == validation_base_triage.BASE_FAILED
        and _triage_may_apply(error)
    )
    if not (conflict or base_failed):
        return None, None
    if _has_confirmed_finding(repo, task_id, current_request_id):
        return None, None
    if not conflict:
        return FailureCategory.VALIDATION_ENVIRONMENT.value, None
    proof, _reason = base_drift_proof_module.base_drift_proof(card, repo_root)
    if proof is None:
        return None, None
    return FailureCategory.BASE_DRIFT.value, proof


def classify_reject(
    card: Mapping[str, Any],
    task_id: str,
    current_request_id: str,
    repo_root: Callable[[], Path],
) -> tuple[str | None, dict[str, Any] | None]:
    """``(failure_category, base_drift_proof)`` for this reject. Never raises.

    ``(None, None)`` means the accept record proves nothing here and the reject
    keeps its existing classification.
    """
    try:
        return _classify_reject(card, task_id, current_request_id, repo_root)
    except Exception as exc:  # noqa: BLE001 -- unknown leaves the reject unchanged
        logger.warning(
            "accept_failure_cause: no classification for task %s: %s",
            task_id, type(exc).__name__,
        )
        return None, None
