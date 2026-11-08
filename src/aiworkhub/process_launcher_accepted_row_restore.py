"""Accepted-row restore and prior reviewer receipts, extracted from ``ProcessManager``.

``ProcessManager._accepted_attempt_row``, ``ProcessManager._restore_buried_accepted_row``
and ``ProcessManager._prior_reviewer_receipts`` are thin delegations to the
functions below (NF-2026-01812: ``process_launcher.py`` sat one line under its
module size ratchet).  The move is byte-for-byte apart from the four-space
dedent a method-to-function move forces; ``self`` is still the
:class:`ProcessManager` instance, so every ``self.`` collaborator and the two
``_PRIOR_REVIEW_MAX_*`` class constants resolve exactly as before.  Module
level collaborators are re-bound from the ``process_launcher`` module object on
entry rather than imported here, because the test suite monkeypatches that
module and a name captured at import time would leave those patches pointing
at code these functions no longer call -- silently, with the tests still green.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "ACCEPTED_ATTEMPT_ROW_SEAM_NAMES",
    "ACCEPTED_ROW_RESTORE_SEAM_NAMES",
    "PRIOR_REVIEWER_RECEIPTS_SEAM_NAMES",
    "accepted_attempt_row",
    "prior_reviewer_receipts",
    "restore_buried_accepted_row",
]

# Each function's complete runtime free-variable set on ``process_launcher``.
# ``Any`` and an annotation-only ``Mapping`` are never evaluated, so they are
# resolved from this module's own imports and are deliberately NOT re-bound.
ACCEPTED_ATTEMPT_ROW_SEAM_NAMES: tuple[str, ...] = (
    "_PidIdentityUnknownDeferred",
    "_parse_card",
    "task_store",
)

ACCEPTED_ROW_RESTORE_SEAM_NAMES: tuple[str, ...] = (
    "_parse_card",
    "_utcnow",
)

PRIOR_REVIEWER_RECEIPTS_SEAM_NAMES: tuple[str, ...] = (
    "Mapping",
    "Path",
    "json",
    "quality_reviewer",
    "sqlite3",
    "task_store",
)


def accepted_attempt_row(
    self,
    events: list[Mapping[str, Any]],
    metadata: Mapping[str, Any],
    request_id: str,
) -> Mapping[str, Any] | None:
    """NF-2026-01625/01706: this attempt's ``accepted`` row, only if the card's
    seal and receipt for this request and epoch confirm it (archived or not).

    NF-2026-02186: with an accepted row in the ledger, a card that cannot be
    read is neither sealed nor unsealed, so this raises the finalizer's
    deferral instead of answering ``None`` -- a readable card without the seal
    still answers ``None``.
    """
    # Every module-level name this function used while it lived in
    # ``process_launcher`` is re-bound here from that exact module object, so a
    # ``monkeypatch.setattr(process_launcher, name, ...)`` seam still reaches the
    # code below.  The list is ``ACCEPTED_ATTEMPT_ROW_SEAM_NAMES`` and is
    # asserted against the body by
    # ``test_process_launcher_extraction_seams_nf01729.py``.
    from . import process_launcher as _pl

    _PidIdentityUnknownDeferred = _pl._PidIdentityUnknownDeferred
    _parse_card = _pl._parse_card
    task_store = _pl.task_store

    accepted = next((r for r in reversed(events) if r.get("state") == "accepted"
                     and r.get("accepted") is True), None)
    if accepted is None:
        return None
    task_id = str(metadata.get("task_id") or "")
    try:
        card = _parse_card(self._show_task(task_id), task_id)
    except Exception as exc:  # noqa: BLE001 - any read failure proves nothing about the seal
        raise _PidIdentityUnknownDeferred("accepted_card_unreadable") from exc
    sealed = task_store.accepted_this_attempt(
        card, request_id=request_id, claim_epoch=metadata.get("claim_epoch")
    )
    return accepted if sealed else None


def restore_buried_accepted_row(
    self,
    tail: Mapping[str, Any],
    accepted: Mapping[str, Any],
    metadata: Mapping[str, Any],
) -> Mapping[str, Any]:
    """NF-2026-01800: re-append a sealed ``accepted`` row a later row buried.

    A failed bridge cancel published after acceptance left
    ``reconcile_pending`` as the tail, so every tail reader -- accept
    preview included -- saw an unfinished reviewer forever. A reviewer row
    is restored only while its receipt is the one acceptance verified,
    identical in the event and both card copies; anything else is returned
    as recorded, unrestored (fail closed). A second finalize pass's
    ``finalizing`` row buries the accepted row the same way (NF-2026-02186).
    """
    # Seam re-binding as in :func:`accepted_attempt_row`; the list is
    # ``ACCEPTED_ROW_RESTORE_SEAM_NAMES``.
    from . import process_launcher as _pl

    _parse_card = _pl._parse_card
    _utcnow = _pl._utcnow

    disposition = str(accepted.get("workspace_disposition") or "")
    # NF-2026-01800's reconcile_pending and NF-2026-02186's finalizing only:
    # NF-2026-01706 appends nothing over release_pending.
    if tail.get("state") not in {"reconcile_pending", "finalizing"} or disposition not in {
        "retained_in_place", "quarantined", "removed",
    }:
        return accepted
    task_id = str(metadata.get("task_id") or "")
    if str(accepted.get("topic") or metadata.get("topic") or "") == "quality_review":
        try:
            card = _parse_card(self._show_task(task_id), task_id)
        except Exception:  # noqa: BLE001 - unreadable card keeps the ledger
            return accepted
        receipt = accepted.get("quality_review_receipt")
        terminal = (card.get("terminal_review") or {}).get("evidence") or {}
        accept_evidence = card.get("accept_evidence") or {}
        if not isinstance(receipt, dict) or not (
            receipt == terminal.get("quality_review_receipt")
            == accept_evidence.get("quality_review_receipt")
        ):
            return accepted
    return self._retention_event({
        **accepted,
        "timestamp": _utcnow(),
        "restored_over_state": str(tail.get("state") or ""),
        "restored_over_reason": str(tail.get("reconciliation_deferred") or "")[:200],
    }, disposition=disposition)


def prior_reviewer_receipts(
    self, target_task_id: str, exclude_request_id: str
) -> tuple[list[dict[str, Any]], dict[str, dict[str, str | None]]]:
    """Read EARLIER reviewer receipts for this task, and the bytes they judged.

    Two bounded reads against the canonical task store, both keyed by one
    task id:

    * the reviewer child cards (``topic='quality_review'``) whose sealed
      ``quality_review`` binding names this target task at some request
      other than the one being packaged now; and
    * this target's own ``terminal_review`` events, which are the only
      durable record of what ``changed_path_hashes`` each earlier request
      actually produced -- the card retains the latest attempt only.

    Sequential by design, and the reason is the shape of the work, not an
    oversight: this is two indexed single-key SQLite reads inside a
    connection that is already open, and the whole call is inside the
    single-flight that prepares one packet.  There is nothing to fan out
    across cores, and a pool here would only compete with the interactive
    MCP server for the same file lock.

    Never raises: a store that cannot be read yields no prior findings,
    which costs a round of re-derivation and breaks nothing.
    """
    # Seam re-binding as in :func:`accepted_attempt_row`; the list is
    # ``PRIOR_REVIEWER_RECEIPTS_SEAM_NAMES``.
    from . import process_launcher as _pl

    Mapping = _pl.Mapping
    Path = _pl.Path
    json = _pl.json
    quality_reviewer = _pl.quality_reviewer
    sqlite3 = _pl.sqlite3
    task_store = _pl.task_store

    reports: list[dict[str, Any]] = []
    hashes_by_request: dict[str, dict[str, str | None]] = {}
    conn: sqlite3.Connection | None = None
    try:
        db_path = task_store.canonical_db_path(self.repo)
        # The canonical store's own read-only opener, not a second way of
        # connecting to it: it applies this repository's busy timeout,
        # ``query_only`` and row factory, so a prior-findings read can never
        # write and never diverge from how everything else reads.
        conn = task_store._connect(Path(db_path), readonly=True)
        rows = conn.execute(
            "SELECT task_id, card_json FROM tasks "
            "WHERE topic='quality_review' AND instr(card_json, ?) > 0 "
            "ORDER BY created_at DESC, task_id DESC LIMIT ?",
            (target_task_id, self._PRIOR_REVIEW_MAX_REVIEWER_CARDS),
        ).fetchall()
        events = conn.execute(
            "SELECT payload_json FROM task_events "
            "WHERE task_id=? AND event='terminal_review' "
            "ORDER BY event_id DESC LIMIT ?",
            (target_task_id, self._PRIOR_REVIEW_MAX_TERMINAL_EVENTS),
        ).fetchall()
    except (sqlite3.Error, task_store.TaskStoreError, OSError, ValueError):
        return [], {}
    finally:
        if conn is not None:
            conn.close()

    for event in events:
        try:
            payload = json.loads(event["payload_json"] or "{}")
        except (TypeError, ValueError):
            continue
        evidence = payload.get("evidence")
        if not isinstance(evidence, Mapping):
            evidence = payload if isinstance(payload, Mapping) else {}
        request_id = str(evidence.get("request_id") or payload.get("request_id") or "")
        changed = evidence.get("changed_path_hashes")
        if not request_id or not isinstance(changed, Mapping):
            continue
        hashes_by_request.setdefault(
            request_id,
            {
                str(key): (None if value is None else str(value))
                for key, value in changed.items()
            },
        )

    for row in rows:
        try:
            card = json.loads(row["card_json"] or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(card, Mapping):
            continue
        terminal = card.get("terminal_review")
        evidence = terminal.get("evidence") if isinstance(terminal, Mapping) else None
        if not isinstance(evidence, Mapping):
            continue
        binding = evidence.get("quality_review")
        receipt = evidence.get("quality_review_receipt")
        if not isinstance(binding, Mapping) or not isinstance(receipt, Mapping):
            continue
        # Identity, checked here rather than trusted: this receipt must name
        # THIS target task, some OTHER request of it, and a real lens.
        if str(binding.get("target_task_id") or "") != target_task_id:
            continue
        prior_request_id = str(binding.get("target_request_id") or "")
        if not prior_request_id or prior_request_id == exclude_request_id:
            continue
        lens = str(binding.get("lens") or "")
        if lens not in quality_reviewer.REVIEWER_LENSES:
            continue
        report = receipt.get("report")
        target = receipt.get("target")
        if not isinstance(report, Mapping) or not isinstance(target, Mapping):
            continue
        if str(target.get("task_id") or "") != target_task_id:
            continue
        findings = report.get("findings")
        if not isinstance(findings, list):
            continue
        reports.append(
            {
                "lens": lens,
                "reviewer_task_id": str(row["task_id"] or ""),
                "reviewer_request_id": str(
                    (receipt.get("reviewer") or {}).get("request_id") or ""
                ),
                "reviewer_provider": str(report.get("provider") or ""),
                "packet_sha256": str(binding.get("packet_sha256") or ""),
                "target_request_id": prior_request_id,
                "findings": [f for f in findings if isinstance(f, Mapping)],
            }
        )
    return reports, hashes_by_request
