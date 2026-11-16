"""Hub-measured proof that a reviewer could not read the candidate (NF-2026-02308).

``quality_evidence.reviewer_report_could_not_inspect`` decides blindness only
from what the reviewer's own report says.  A reviewer whose candidate-overlay
reads all failed, but which still filed ordinary findings from the docs, was
therefore classed sighted.  This module measures the one fact the hub can prove
for itself: the reviewer request's own HMAC-authenticated worker MCP audit
ledger, read through :func:`worker_ai_tools_mcp.verify_audit_ledger` (the ledger
reader the completion gate already uses; there is no second parser).

``overlay_unreachable`` holds when at least one ``source_graph`` call was refused
with a candidate-overlay-unavailable error AND no ``body``/``file``/``diff``/
``bodygrep`` call on the candidate succeeded.  A missing, unreadable or
unverifiable ledger is UNKNOWN, never blind -- the same rule a missing
``usage`` field gets.  The fact is derived only from the ledger: nothing a
model writes in a report can set or clear it, and :func:`stamp_report` strips
any model-supplied ``reviewer_request_id`` before the hub writes its own.

Every function here is total: any failure answers "not measured".
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

REVIEWER_REQUEST_ID_KEY = "reviewer_request_id"


def overlay_unreachable_from_verification(verification: Mapping[str, Any]) -> bool:
    """The measured rule, over one ``verify_audit_ledger`` result."""

    if not isinstance(verification, Mapping):
        return False
    if verification.get("reason") in {"audit_key_unreadable", "audit_ledger_unreadable"}:
        return False
    try:
        unavailable = int(verification.get("candidate_overlay_unavailable_calls") or 0)
        reads = int(verification.get("candidate_overlay_read_successes") or 0)
    except (TypeError, ValueError):
        return False
    return unavailable > 0 and reads == 0


def _from_metadata(metadata: Mapping[str, Any], request_id: str) -> bool:
    """Measure one reviewer request from its sealed request metadata."""

    from . import worker_ai_tools_mcp

    worker_mcp = metadata.get("worker_mcp")
    if not isinstance(worker_mcp, Mapping):
        return False
    ledger_path = worker_mcp.get("audit_ledger_path")
    key_path = worker_mcp.get("audit_hmac_key_path")
    if not ledger_path or not key_path:
        return False
    verification = worker_ai_tools_mcp.verify_audit_ledger(
        Path(str(ledger_path)),
        Path(str(key_path)),
        task_id=str(metadata["task_id"]),
        runner=str(metadata["runner"]),
        topic=str(metadata["topic"]),
        request_id=str(request_id or metadata.get("request_id") or "") or None,
    )
    return overlay_unreachable_from_verification(verification)


def _metadata_path(events: Any) -> Any:
    """The newest ``metadata_path`` among a request's events (or one event)."""

    if isinstance(events, Mapping):
        events = [events]
    for event in reversed(list(events)):
        raw = event.get("metadata_path") if isinstance(event, Mapping) else None
        if raw:
            return raw
    return None


def overlay_unreachable(events: Any, request_id: str = "") -> bool:
    """The ONE measurement every caller uses (accept, accept_preview, reuse).

    ``events`` is the reviewer request's event list (a single event mapping, or
    a zero-argument callable returning either, is also accepted so the caller's
    own lookup runs under the same guard).  The metadata path is resolved from
    it here, once, and the bounded metadata read plus the ledger verification
    follow.  Total: any failure -- an events lookup that raises, no metadata,
    an unreadable or oversized file, an unverifiable ledger -- is UNKNOWN,
    which is never blind.
    """

    try:
        from .request_metadata_limits import REQUEST_METADATA_MAX_BYTES

        if callable(events):
            events = events()
        path = _metadata_path(events)
        if not path:
            return False
        target = Path(str(path))
        if (
            target.is_symlink()
            or not target.is_file()
            or target.stat().st_size > REQUEST_METADATA_MAX_BYTES
        ):
            return False
        metadata = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(metadata, Mapping):
            return False
        return _from_metadata(metadata, str(request_id))
    except Exception:  # noqa: BLE001 -- unknown is never blind
        return False


def stamp_report(
    report: Mapping[str, Any], reviewer_request_id: str, overlay_unreachable: bool
) -> dict[str, Any]:
    """A copy of ``report`` carrying the hub's own reviewer request id.

    Any ``reviewer_request_id`` the model wrote is dropped first.  The id is
    added only for a measured-unreachable reviewer, so every other report is
    passed on exactly as it was.
    """

    stamped = {k: v for k, v in report.items() if k != REVIEWER_REQUEST_ID_KEY}
    if overlay_unreachable and reviewer_request_id:
        stamped[REVIEWER_REQUEST_ID_KEY] = str(reviewer_request_id)
    return stamped


def blind_inputs(
    rows: Iterable[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], frozenset[str]]:
    """Stamped reports and the measured set, from ``reviewer_evidence`` rows."""

    reports: list[dict[str, Any]] = []
    unreachable: set[str] = set()
    for row in rows:
        receipt = row.get("receipt")
        report = receipt.get("report") if isinstance(receipt, Mapping) else None
        if not isinstance(report, Mapping):
            continue
        request_id = str(row.get("request_id") or "")
        measured = row.get("overlay_unreachable") is True and bool(request_id)
        if measured:
            unreachable.add(request_id)
        reports.append(stamp_report(report, request_id, measured))
    return reports, frozenset(unreachable)
