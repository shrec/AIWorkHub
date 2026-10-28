"""Acceptance of one ``review_ready`` request, extracted from ``ProcessManager``.

``ProcessManager.accept_review`` is a thin delegation to :func:`accept_review`
below.  The move is byte-for-byte: the body is the one that lived in
``process_launcher`` and ``self`` is still the :class:`ProcessManager`
instance, so every ``self.`` collaborator resolves exactly as before.  Module
level collaborators are re-bound from the ``process_launcher`` module object on
entry rather than imported here, because 27 test files monkeypatch that module
and a name captured at import time would leave those patches pointing at code
this function no longer calls -- silently, with the tests still green.
"""

from __future__ import annotations

import contextlib
import errno
import fnmatch
import hashlib
import json
import os
import posixpath
import re
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from . import __version__
from . import manager_skill_tools
from . import quality_evidence
from .platform_io import lock_fd, process_is_alive, unlock_fd
from .process_identity import (
    PidIdentityVerdict,
    _pid_identity_evidence,
    _pid_start_ticks,
)

if TYPE_CHECKING:  # names used only in annotations, which are never evaluated
    from collections.abc import Iterator

    from .worker_workspace import WorkerWorkspace as _WorkerWorkspaceT

__all__ = [
    "ACCEPT_BLOCKER_KINDS",
    "ACCEPT_IN_FLIGHT_EVENT_SCHEMA_ID",
    "ACCEPT_IN_FLIGHT_SCHEMA_ID",
    "ACCEPT_PREVIEW_SCHEMA_ID",
    "MANAGER_CORRECTION_SCHEMA_ID",
    "SUPPLEMENTAL_REVIEW_SCHEMA_ID",
    "ManagerCorrectionError",
    "accept_in_flight",
    "accept_preview",
    "accept_review",
    "bound_reviewer_request_ids",
    "bound_reviewer_rows",
    "bound_reviewer_task_ids",
    "corrected_candidate_texts",
    "create_supplemental_inspection_rounds",
    "dispose_review_finding",
    "effective_requested_risk_tier",
    "fold_accept_blockers",
    "review_finding_dispositions",
    "reviewer_evidence",
    "sealed_review_packet",
    "server_derived_risk_tier",
    "supplemental_rounds_completed",
    "validate_manager_correction",
]

_REVIEWER_USABLE_STATES = ("review_ready", "accepted")

# A reviewer that has not finished is not missing evidence, it is unfinished
# evidence, and the two need different words: 17 of 159 measured accept
# attempts failed with ``quality_reviewer_not_review_ready`` -- after paying for
# a combined-tree materialization and two validation runs.
_REVIEWER_RUNNING_STATES = frozenset(
    {"starting", "running", "processing", "finalizing", "reconcile_pending"}
)

ACCEPT_PREVIEW_SCHEMA_ID = "aiworkhub.accept_preview.v1"

# Every blocker the fold can name, so a caller can branch on a closed set
# instead of matching prose. The measured frequencies are over the 159 accept
# attempts audited on 2026-09-08, of which 49 (31%) failed on one of these.
ACCEPT_BLOCKER_KINDS = (
    "terminal_substatus_not_review_ready",   # 8 of 159
    "context_write_intents_pending",
    "required_reviewer_missing",             # 19 of 159
    "quality_reviewer_not_review_ready",     # 17 of 159
    "refinement_required",
    "explicit_human_approval_missing",       # 5 of 159
    "destructive_diff_requires_manager_confirmation",
    "reviewer_state_unknown",  # ledger unreadable: neither running nor missing
    # A reviewer that finished and reported it could not READ its packet. Named
    # here because the accept fold refuses with it and the preview now predicts
    # it (NF-2026-01158) -- a caller branching on this closed set has to be able
    # to see it without matching prose.
    "reviewer_could_not_inspect",
    # An accept of this exact request is running right now, in this or another
    # server process: wait for it, never start a second one (NF-2026-01626).
    "accept_in_progress",
    # Its in-flight marker exists but cannot be read, so whether it still runs
    # is unknown -- and unknown is never "not running".
    "accept_in_flight_marker_unreadable",
)


def _blocker(kind: str, detail: str = "", **extra: Any) -> dict[str, Any]:
    """One named blocker, with the exact error string ``accept_review`` returns."""
    return {
        "kind": str(kind),
        "detail": str(detail)[:300],
        "error": (f"{kind}:{detail}"[:400] if detail else str(kind)),
        **extra,
    }


def _codex_route_is_live(core: Any, identity: Mapping[str, Any]) -> bool:
    """A Codex identity backed by a live route: a UUID thread on a target that
    is not route-pending and does accept callbacks."""
    return (
        bool(core._UUID_RE.fullmatch(str(identity.get("thread_id") or "")))
        and str(identity.get("callback_supported") or "").strip().lower() != "false"
        and str(identity.get("route_state") or "available") in ("available", "ready")
    )


def _accept_manager_identity(core: Any) -> dict[str, Any]:
    """Resolve the accepting manager's identity once, best-effort.

    The identity is the one the manager MCP surface stamps on every reply
    (``core.stamped_manager_identity``): same-uid local runtime state
    (provider, session, window), never a credential, and never allowed to
    fail an accept. A Codex identity counts only when its route is live --
    a stale Codex route file is not the manager and is never a fallback for
    another provider (RM-2026-00082); unverified is recorded as unverified.
    """
    try:
        identity = core.stamped_manager_identity()
        if str(identity.get("provider") or "") == "codex" and not _codex_route_is_live(core, identity):
            return {}
        return identity or {}
    except Exception:  # noqa: BLE001 -- describing the caller never fails an accept
        return {}


def bound_reviewer_rows(
    repo: Any, parent_task_id: str, parent_request_id: str
) -> list[dict[str, Any]]:
    """One scan over the quality-review children bound to this exact request.

    Returns, per bound reviewer task, the fields every caller here needs: the
    task id, the lens the packet was sealed for, the reviewer's own terminal
    substatus, and the verified receipt if it produced one. One read serves the
    default-reviewer resolution, the cheap blocker fold and the preview, so a
    manager decision costs the store one query rather than one per reviewer.

    The binding read -- ``terminal_review.evidence.quality_review`` with the
    root ``quality_review`` block as the older fallback -- is the SAME one
    ``task_engine.disposition_reviewer_children`` scans to dispose these cards
    at accept and reject. Measured on the live store, 2,219 of 2,219 reviewer
    cards carry it under terminal evidence and none under the root key.

    Read-only and total: any store failure returns an empty list, which can
    only make the caller ask for MORE evidence (a missing required lens blocks
    acceptance), never less.
    """
    from . import task_store

    try:
        _readiness, db_path = task_store._require_ready(repo)
        conn = task_store._connect(db_path, readonly=True)
    except Exception:  # noqa: BLE001 -- enumeration failure never accepts anything
        return []
    try:
        rows = conn.execute(
            "SELECT task_id, card_json FROM tasks "
            "WHERE topic='quality_review'"
        ).fetchall()
    except Exception:  # noqa: BLE001
        return []
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    import json as _json

    bound: list[dict[str, Any]] = []
    for row in rows:
        try:
            card = _json.loads(row["card_json"] or "{}")
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(card, dict):
            continue
        terminal = card.get("terminal_review")
        terminal = terminal if isinstance(terminal, dict) else {}
        terminal_evidence = terminal.get("evidence")
        terminal_evidence = terminal_evidence if isinstance(terminal_evidence, dict) else {}
        terminal_binding = terminal_evidence.get("quality_review")
        root_binding = card.get("quality_review")
        if not isinstance(terminal_binding, dict):
            terminal_binding = {}
        if not isinstance(root_binding, dict):
            root_binding = {}
        if terminal_binding and root_binding and terminal_binding != root_binding:
            # Two durable statements that disagree are not a binding.
            continue
        binding = terminal_binding or root_binding
        if (
            str(binding.get("target_task_id") or "") != str(parent_task_id)
            or str(binding.get("target_request_id") or "") != str(parent_request_id)
        ):
            continue
        task_id = str(row["task_id"] or "")
        if not task_id:
            continue
        receipt = terminal_evidence.get("quality_review_receipt")
        bound.append(
            {
                "task_id": task_id,
                "lens": str(binding.get("lens") or ""),
                "packet_sha256": str(binding.get("packet_sha256") or ""),
                "terminal_substatus": str(terminal.get("substatus") or ""),
                "receipt": receipt if isinstance(receipt, dict) else None,
            }
        )
    return sorted(bound, key=lambda entry: entry["task_id"])


def bound_reviewer_task_ids(
    repo: Any, parent_task_id: str, parent_request_id: str
) -> list[str]:
    """The task ids from :func:`bound_reviewer_rows`, for callers that need only those.

    The server has always known which reviewers belong to a request; only the
    accept surface asked the manager to name them, in 135 of 159 measured
    calls, by copying 32-hex ids out of earlier launch results.
    """
    return [
        row["task_id"]
        for row in bound_reviewer_rows(repo, parent_task_id, parent_request_id)
    ]


def bound_reviewer_request_ids(
    self, parent_task_id: str, parent_request_id: str
) -> list[str]:
    """Return the usable reviewer REQUEST ids bound to this parent request.

    One reviewer task can be relaunched, so a task can own several requests.
    Only ``review_ready``/``accepted`` requests are usable evidence, and where
    a task has more than one the most recently finished wins -- the same
    "latest terminal attempt" rule the accept path applies when the manager
    names an id by hand.
    """
    return [
        row["request_id"]
        for row in reviewer_evidence(self, parent_task_id, parent_request_id)
        if row["usable"]
    ]


def reviewer_evidence(
    self, parent_task_id: str, parent_request_id: str
) -> list[dict[str, Any]]:
    """Resolve, per bound reviewer task, its current attempt and what it proved.

    One reviewer task can be relaunched, so a task can own several requests.
    The latest USABLE (``review_ready``/``accepted``) attempt wins, because that
    is the one carrying evidence; a task with no usable attempt still reports
    its latest attempt so the caller can say "running" instead of "missing" --
    two conditions the accept surface has always conflated and which need
    different answers from a manager.

    ``running`` is liveness, never a state string: it comes only from the
    launcher's per-event rule (``_live_lens_reviewers``) over attempts sealed
    to this exact parent.  A live reviewer whose card carries no binding yet is
    listed from its topic ``quality_review`` ledger event alone (NF-2026-01058).

    Total by construction: any failure of the ledger read or of the liveness
    rule leaves every reviewer with an empty request id, ``usable`` False and
    ``state_unknown`` True, with the exception class name (never its text) in
    ``state_unknown_cause`` -- never a ``running`` False dressed up as
    knowledge -- which blocks acceptance (NF-2026-01164).
    """
    rows = bound_reviewer_rows(self.repo, parent_task_id, parent_request_id)
    live_lens_reviewers = getattr(self, "_live_lens_reviewers", None)
    try:
        latest = self._latest_by_request()
        live = (
            live_lens_reviewers(latest, parent_task_id, parent_request_id)
            if live_lens_reviewers is not None
            else []
        )
    except Exception as exc:  # noqa: BLE001 -- unknown is neither running nor missing
        # The class name only: exception text can carry host paths.
        unknown = {
            "request_id": "", "state": "", "usable": False, "attempt_count": 0,
            "running": False, "running_request_id": "", "state_unknown": True,
            "state_unknown_cause": type(exc).__name__[:80],
        }
        return [{**row, **unknown} for row in rows] or [{
            "task_id": "", "lens": "", "packet_sha256": "",
            "terminal_substatus": "", "receipt": None, **unknown,
        }]
    live_by_task: dict[str, tuple[str, Mapping[str, Any]]] = {}
    for live_request_id, live_event in live:
        live_by_task.setdefault(
            str(live_event.get("task_id") or ""), (live_request_id, live_event)
        )
    bound_task_ids = {row["task_id"] for row in rows}
    for live_task_id, (_live_request_id, live_event) in live_by_task.items():
        if live_task_id and live_task_id not in bound_task_ids:
            # A running reviewer's binding exists only in its ledger event.
            attempt = live_event.get("quality_review_attempt") or {}
            rows.append({
                "task_id": live_task_id, "lens": str(attempt.get("lens") or ""),
                "packet_sha256": "", "terminal_substatus": "", "receipt": None,
            })
    attempts: dict[str, list[dict[str, Any]]] = {}
    for request_id, event in latest.items():
        if not isinstance(event, dict):
            continue
        reviewer_task_id = str(event.get("task_id") or "")
        if not reviewer_task_id:
            continue
        attempts.setdefault(reviewer_task_id, []).append(
            {
                "request_id": str(request_id),
                "state": str(event.get("state") or ""),
                "finished_at": str(event.get("finished_at") or ""),
            }
        )
    resolved: list[dict[str, Any]] = []
    for row in rows:
        candidates = attempts.get(row["task_id"], [])
        usable = [
            attempt
            for attempt in candidates
            if attempt["state"] in _REVIEWER_USABLE_STATES
        ]
        chosen = max(
            usable or candidates,
            key=lambda attempt: (attempt["finished_at"], attempt["request_id"]),
            default=None,
        )
        running = live_by_task.get(row["task_id"])
        resolved.append(
            {
                **row,
                "request_id": str((chosen or {}).get("request_id") or ""),
                "state": str((chosen or {}).get("state") or ""),
                "usable": bool(usable) and chosen is not None,
                "attempt_count": len(candidates),
                "running": running is not None,
                "running_request_id": running[0] if running else "",
                "state_unknown": False,
                "state_unknown_cause": "",
            }
        )
    return sorted(resolved, key=lambda entry: (entry["lens"], entry["task_id"]))


def _latest_request_identity_event(
    events: list[dict[str, Any]], task_id: str
) -> dict[str, Any] | None:
    """Return the newest exact-task event carrying launch identity.

    Request-scoped orchestration may append bookkeeping events after the
    terminal worker event. Those rows intentionally omit runner/topic and
    must not erase the authenticated execution identity used by acceptance.
    """
    return next(
        (
            event
            for event in reversed(events)
            if str(event.get("task_id") or "") == task_id
            and str(event.get("runner") or "")
            and str(event.get("topic") or "")
        ),
        None,
    )


def server_derived_risk_tier(card: Any) -> str:
    """The tier the FINALIZER measured for this candidate, or "" when unrecorded.

    ``run_review_ready_quality_gate`` seals ``review_risk_profile`` on
    ``terminal_review.evidence.quality_gate`` at ``review_ready``, with the same
    ``derive_risk_signals`` -> ``resolve_risk_profile`` pair the accept path
    runs. Reading it back means the manager no longer has to predict and retype
    the tier -- and, where the finalizer saw a signal the accept-time
    re-derivation cannot (a destructive diff against a canonical tree that has
    since moved), it is a floor the accept fold keeps rather than loses.
    """
    if not isinstance(card, Mapping):
        return ""
    terminal = card.get("terminal_review")
    evidence = terminal.get("evidence") if isinstance(terminal, Mapping) else None
    gate = evidence.get("quality_gate") if isinstance(evidence, Mapping) else None
    profile = gate.get("review_risk_profile") if isinstance(gate, Mapping) else None
    if not isinstance(profile, Mapping) or str(profile.get("error") or ""):
        return ""
    tier = str(profile.get("effective_tier") or "")
    return tier if tier in quality_evidence._RISK_RANK else ""


def effective_requested_risk_tier(card: Any, requested_risk_tier: Any) -> str:
    """Fold the manager's requested tier over the server-derived one, upward only.

    ``requested_risk_tier`` is an OVERRIDE, and an override of a safety floor
    may only raise it: a manager may always ask for more review than the
    measurement demands, and may never ask for less by naming a lower tier.
    ``None`` means "whatever the server derived", which is the whole point --
    the tier stops being something a manager has to type correctly.
    """
    derived = server_derived_risk_tier(card)
    requested = (
        str(requested_risk_tier)
        if isinstance(requested_risk_tier, str) and requested_risk_tier
        else ""
    )
    ranks = quality_evidence._RISK_RANK
    if requested and requested not in ranks:
        # Fail closed on a tier nobody defined: ``resolve_risk_profile`` will
        # refuse it, and refusing is the correct outcome.
        return requested
    if not requested:
        return derived or quality_evidence.RISK_LOW
    if derived and ranks[derived] > ranks[requested]:
        return derived
    return requested


def _refinement_blockers(
    reviewers: list[dict[str, Any]],
    *,
    finding_dispositions: list[Mapping[str, Any]] | None = None,
    disposition_rows: list[dict[str, Any]] | None = None,
    manager_correction: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Findings on ALREADY-VERIFIED reports that will refuse this acceptance.

    Only the receipts a reviewer already sealed are read -- nothing is
    re-verified here and nothing unverified is trusted. ``fold_quality_verdict``
    remains the authority; this names in advance the subset of its verdict a
    manager can act on before paying for a combined tree.

    ``finding_dispositions`` are manager disposition receipts ALREADY bound by
    :func:`bound_finding_dispositions`. Each finding they name is appended to
    ``disposition_rows`` with its disposition and receipt digest; a
    ``dismissed`` non-blocking finding is not a blocker (NF-2026-01170), every
    other finding blocks exactly as before.

    ``manager_correction`` is a record ALREADY validated by
    :func:`validate_manager_correction` -- never a caller's claim. Each
    ``confirmed``, non-blocking correctness/security finding it names is lifted
    the same way a dismissal lifts one, and its ``disposition_rows`` entry says
    ``lifted_by`` ``manager_correction`` (NF-2026-01436). A blocking severity is
    never lifted and no other blocker is affected.
    """
    corrected_finding_ids = frozenset(
        str(value)
        for value in ((manager_correction or {}).get("finding_ids") or ())
    )
    by_finding = {
        (
            str(row.get("reviewer_request_id") or ""),
            str(row.get("finding_id") or ""),
        ): row
        for row in (finding_dispositions or ())
        if isinstance(row, Mapping)
    }
    dismissed_by_finding = quality_evidence.dismissed_finding_receipts(
        finding_dispositions
    )
    reports: list[tuple[dict[str, Any], str, list[Any]]] = []
    # Ids are counted across EVERY report given, exactly as the fold counts
    # them: an id that is not unique is never lifted by either surface.
    finding_id_counts: dict[str, int] = {}
    for reviewer in reviewers:
        receipt = reviewer.get("receipt")
        report = receipt.get("report") if isinstance(receipt, Mapping) else None
        if not isinstance(report, Mapping):
            continue
        lens = str(report.get("lens") or reviewer.get("lens") or "")
        findings = report.get("findings")
        rows = findings if isinstance(findings, list) else []
        reports.append((reviewer, lens, rows))
        for finding in rows:
            if isinstance(finding, Mapping):
                counted_id = f"reviewer:{lens}:{str(finding.get('id') or '')}"
                finding_id_counts[counted_id] = finding_id_counts.get(counted_id, 0) + 1
    blockers: list[dict[str, Any]] = []
    for reviewer, lens, rows in reports:
        for finding in rows:
            if not isinstance(finding, Mapping):
                continue
            if finding.get("disposition") != "defect":
                continue
            finding_id = f"reviewer:{lens}:{str(finding.get('id') or '')}"
            severity = str(finding.get("severity") or "")
            stored = by_finding.get((str(reviewer.get("request_id") or ""), finding_id))
            disposition = str(stored.get("disposition") or "") if stored else ""
            lifted = (
                disposition == "dismissed"
                and lens
                in {quality_evidence.LENS_CORRECTNESS, quality_evidence.LENS_SECURITY}
                and quality_evidence.dismissal_lifts_finding(
                    finding_id,
                    severity=severity,
                    finding_id_counts=finding_id_counts,
                    dismissed_by_finding=dismissed_by_finding,
                )
            )
            # NF-2026-01436: a CONFIRMED non-blocking correctness/security
            # finding the manager has actually fixed, with that fix validated
            # before this ran. The uniqueness condition is the dismissal path's
            # own: an id that is not unique across these reports names more than
            # one finding, so correcting it cannot be proven and lifts nothing.
            # A blocking severity falls through to the blocking branch below and
            # is never lifted by either surface.
            corrected = (
                not lifted
                and finding_id in corrected_finding_ids
                and disposition == "confirmed"
                and severity not in quality_evidence.BLOCKING_SEVERITIES
                and lens
                in {quality_evidence.LENS_CORRECTNESS, quality_evidence.LENS_SECURITY}
                and finding_id_counts.get(finding_id) == 1
            )
            lifted = lifted or corrected
            if stored is not None and disposition_rows is not None:
                disposition_rows.append(
                    {
                        "finding_id": finding_id,
                        "reviewer_request_id": str(reviewer.get("request_id") or ""),
                        "lens": lens,
                        "severity": severity,
                        "disposition": disposition,
                        "receipt_sha256": str(stored.get("receipt_sha256") or ""),
                        "blocker_lifted": lifted,
                        # Named only for a manager correction, so a row produced
                        # by any other path keeps exactly the shape it had.
                        **({"lifted_by": "manager_correction"} if corrected else {}),
                    }
                )
            extra: dict[str, Any] = (
                {
                    "disposition": disposition,
                    "disposition_receipt_sha256": str(
                        stored.get("receipt_sha256") or ""
                    ),
                }
                if stored is not None
                else {}
            )
            if severity in quality_evidence.BLOCKING_SEVERITIES:
                blockers.append(
                    _blocker(
                        "refinement_required", finding_id[:300],
                        lens=lens, severity=severity, blocking_severity=True,
                        **extra,
                    )
                )
            elif lens in {
                quality_evidence.LENS_CORRECTNESS, quality_evidence.LENS_SECURITY
            }:
                if lifted:
                    continue
                blockers.append(
                    _blocker(
                        "refinement_required", finding_id[:300],
                        lens=lens, severity=severity, blocking_severity=False,
                        **extra,
                    )
                )
    return blockers


def _receipt_sha256(receipt: Any) -> str:
    """The canonical digest of one reviewer receipt, or "" when unreadable."""
    import json as _json

    from . import review_orchestrator

    if not isinstance(receipt, Mapping):
        return ""
    try:
        return review_orchestrator.canonical_digest(
            _json.loads(_json.dumps(dict(receipt)))
        )
    except (TypeError, ValueError):
        return ""


def _sealed_receipt_sha256(reviewers: list[dict[str, Any]]) -> dict[str, str]:
    """Reviewer request id -> digest of the receipt the server sealed for it."""
    digests: dict[str, str] = {}
    for row in reviewers:
        request_id = str(row.get("request_id") or "")
        digest = _receipt_sha256(row.get("receipt"))
        if request_id and digest and row.get("usable"):
            digests[request_id] = digest
    return digests


def _target_candidate_sha256(card: Any) -> str:
    """The candidate digest sealed on the target's own terminal evidence."""
    from . import review_orchestrator

    return str(
        review_orchestrator.target_identity_from_card(card).get("candidate_sha256")
        or ""
    )


def bound_finding_dispositions(
    self: Any,
    task_id: str,
    request_id: str,
    *,
    candidate_sha256: str,
    receipt_sha256_by_reviewer: Mapping[str, str],
) -> list[dict[str, Any]]:
    """Stored finding dispositions still bound to THIS candidate and receipts.

    A row survives only when its stored candidate digest equals
    ``candidate_sha256`` and its stored reviewer receipt digest equals the
    digest of the receipt verified for that reviewer request here. Anything
    else -- other bytes, another report, an unreadable store -- is ignored, so
    the finding blocks exactly as it did before the disposition existed.
    """
    loader = getattr(self, "review_finding_dispositions", None)
    if not callable(loader) or not candidate_sha256:
        return []
    try:
        rows = loader(task_id, request_id)
    except Exception:  # noqa: BLE001 -- an unreadable store lifts nothing
        return []
    bound: list[dict[str, Any]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, Mapping):
            continue
        expected = str(
            receipt_sha256_by_reviewer.get(str(row.get("reviewer_request_id") or ""))
            or ""
        )
        if (
            str(row.get("candidate_sha256") or "") != candidate_sha256
            or not expected
            or str(row.get("reviewer_receipt_sha256") or "") != expected
        ):
            continue
        bound.append(dict(row))
    return bound


def _selected_reviewer_rows(
    reviewers: list[dict[str, Any]], reviewer_request_ids: list[str] | None
) -> tuple[list[dict[str, Any]], list[str], str]:
    """The reviewer rows one accept acts on, the unresolved ids, and the source.

    ONE definition, read by :func:`fold_accept_blockers` and by
    :func:`validate_manager_correction`: a manager correction may only name a
    finding on a reviewer this accept actually selected, and "selected" has to
    mean the same thing in both places -- otherwise a correction could lift a
    blocker the fold never raised, or fail to lift one it did.
    """
    selected = (
        None if reviewer_request_ids is None else [str(v) for v in reviewer_request_ids]
    )
    if selected is None:
        return (
            [row for row in reviewers if row["usable"]],
            [],
            "server_bound_reviewer_children",
        )
    known = {row["request_id"]: row for row in reviewers if row["request_id"]}
    return (
        [known[request_id] for request_id in selected if request_id in known],
        [request_id for request_id in selected if request_id not in known],
        "manager_named",
    )


# ---------------------------------------------------------------------------
# Manager correction on top of the sealed candidate (NF-2026-01436, flow A).
#
# Measured 2026-10-09: needfix-NF-2026-01334-r1 (request 515c092b) carried ONE
# confirmed LOW correctness finding -- a duplicate test def -- and
# ``accept_review`` still answered ``refinement_required`` with
# ``blocking_severity`` false and ``disposition`` confirmed, because a confirmed
# finding is a real defect and the accept path has no way to say "and here is
# the fix". The only available next step was a full reject plus a rework round
# plus two more high-risk reviewers, and the corrected work was archived instead
# of accepted -- which is why the consumer seat measured ~54% learning-commit
# coverage.
#
# A manager correction is a unified diff applied ON TOP of the sealed candidate
# bytes, naming the confirmed non-blocking findings it repairs. Three rules make
# it safe to accept rather than rework:
#
# * it is decided BEFORE any promotion or state change, and every refusal is the
#   one typed ``manager_correction_invalid:<reason>`` with nothing written;
# * it can only lift a CONFIRMED, non-blocking correctness/security finding --
#   a blocking severity is never lifted and no other blocker is touched; and
# * the reviewer receipts stay bound to the sealed candidate they actually read.
#   The manager's bytes are a separate, separately hashed statement in the
#   accepted-outcome receipt, never a re-keying of what a reviewer saw.
# ---------------------------------------------------------------------------
MANAGER_CORRECTION_SCHEMA_ID = "aiworkhub.manager_correction.v1"

# The only fields a correction may carry; anything else refuses by name rather
# than being silently dropped.
MANAGER_CORRECTION_FIELDS = frozenset({"patch", "finding_ids", "reason"})

# Bounded inputs: a correction is a small repair of an already-reviewed
# candidate, never a second candidate. Each cap refuses by name instead of
# reading further.
MAX_MANAGER_CORRECTION_PATCH_BYTES = 256 * 1024
MAX_MANAGER_CORRECTION_FILES = 16
MAX_MANAGER_CORRECTION_HUNKS = 256
MAX_MANAGER_CORRECTION_TARGET_BYTES = 4 * 1024 * 1024

_MANAGER_CORRECTION_HUNK = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@"
)

# The non-content header lines a ``git diff`` preamble may carry before the
# ``---``/``+++`` pair. Anything else outside a hunk is unparsable, not noise.
_MANAGER_CORRECTION_PREAMBLE = (
    "diff ",
    "index ",
    "old mode ",
    "new mode ",
    "similarity ",
    "dissimilarity ",
    "rename ",
    "copy ",
)


class ManagerCorrectionError(Exception):
    """One named reason a manager correction is refused.

    Carries a reason token only -- never patch text, file content or a digest --
    so the message can be returned to the caller verbatim.
    """


def _manager_correction_path(raw: str) -> str:
    """The repo-relative path one diff header names, or a named refusal."""
    candidate = raw.split("\t", 1)[0].strip()
    if candidate in ("", "/dev/null"):
        raise ManagerCorrectionError("patch_file_create_or_delete_unsupported")
    for prefix in ("a/", "b/"):
        if candidate.startswith(prefix):
            candidate = candidate[len(prefix):]
            break
    normalized = candidate.replace("\\", "/")
    if (
        not normalized
        or normalized.startswith("/")
        or ".." in normalized.split("/")
        or "\x00" in normalized
        or any(char in normalized for char in "*?[")
        or ":" in normalized
    ):
        raise ManagerCorrectionError("patch_path_invalid")
    return normalized


def corrected_candidate_texts(patch: str, read_text: Any) -> dict[str, str]:
    """Apply one unified diff to the sealed candidate, strictly.

    ``read_text(relative)`` supplies the preimage for each path the patch names
    and may itself raise :class:`ManagerCorrectionError`. Every hunk must land
    at the line its own header states and its context and deleted lines must
    match the preimage exactly: there is no fuzz, no offset search and no
    partial application. A patch that does not parse, names a path twice,
    creates or deletes a file, renames one, or disagrees with a single byte of
    the candidate raises and NOTHING is written anywhere -- this function
    returns the corrected text and never touches the filesystem itself.

    Lines are compared with ``\\n`` as the only terminator, which is what the
    reader enforces for the preimage; a ``\\ No newline at end of file`` marker
    refuses by name rather than being guessed at.
    """
    if not isinstance(patch, str) or not patch.strip():
        raise ManagerCorrectionError("patch_empty")
    if len(patch.encode("utf-8")) > MAX_MANAGER_CORRECTION_PATCH_BYTES:
        raise ManagerCorrectionError("patch_too_large")
    lines = patch.replace("\r\n", "\n").split("\n")
    corrected: dict[str, str] = {}
    hunks = 0
    index = 0
    while index < len(lines):
        header = lines[index]
        if not header.startswith("--- "):
            if header.strip() and not header.startswith(_MANAGER_CORRECTION_PREAMBLE):
                raise ManagerCorrectionError("patch_unparsable")
            index += 1
            continue
        if index + 1 >= len(lines) or not lines[index + 1].startswith("+++ "):
            raise ManagerCorrectionError("patch_unparsable")
        old_path = _manager_correction_path(header[4:])
        new_path = _manager_correction_path(lines[index + 1][4:])
        if old_path != new_path:
            raise ManagerCorrectionError("patch_rename_unsupported")
        if new_path in corrected:
            raise ManagerCorrectionError("patch_duplicate_file")
        if len(corrected) >= MAX_MANAGER_CORRECTION_FILES:
            raise ManagerCorrectionError("patch_too_many_files")
        index += 2
        source = read_text(new_path).split("\n")
        trailing_newline = bool(source) and source[-1] == ""
        if trailing_newline:
            source = source[:-1]
        out: list[str] = []
        cursor = 0
        saw_hunk = False
        while index < len(lines):
            match = _MANAGER_CORRECTION_HUNK.match(lines[index])
            if match is None:
                break
            saw_hunk = True
            hunks += 1
            if hunks > MAX_MANAGER_CORRECTION_HUNKS:
                raise ManagerCorrectionError("patch_too_many_hunks")
            old_start = int(match.group(1))
            old_count = 1 if match.group(2) is None else int(match.group(2))
            new_count = 1 if match.group(4) is None else int(match.group(4))
            # An insertion-only hunk states the line BEFORE the insertion point;
            # every other hunk states its own first line, 1-indexed.
            start = old_start - 1 if old_count else old_start
            if start < cursor or start + old_count > len(source):
                raise ManagerCorrectionError("patch_hunk_out_of_range")
            out.extend(source[cursor:start])
            cursor = start
            index += 1
            taken_old = 0
            taken_new = 0
            while taken_old < old_count or taken_new < new_count:
                if index >= len(lines):
                    raise ManagerCorrectionError("patch_hunk_truncated")
                body = lines[index]
                index += 1
                if body.startswith("\\"):
                    raise ManagerCorrectionError(
                        "patch_no_newline_marker_unsupported"
                    )
                # A trimmed empty context line is the one accepted shorthand:
                # the declared counts, not the line shape, end the hunk.
                kind = body[:1] if body else " "
                text = body[1:] if body else ""
                if kind == "+":
                    taken_new += 1
                    out.append(text)
                    continue
                if kind not in (" ", "-"):
                    raise ManagerCorrectionError("patch_unparsable")
                if taken_old >= old_count or source[cursor] != text:
                    raise ManagerCorrectionError("patch_context_mismatch")
                taken_old += 1
                cursor += 1
                if kind == " ":
                    taken_new += 1
                    out.append(text)
        if not saw_hunk:
            raise ManagerCorrectionError("patch_no_hunks")
        out.extend(source[cursor:])
        corrected[new_path] = "\n".join(out) + ("\n" if trailing_newline else "")
    if not corrected:
        raise ManagerCorrectionError("patch_no_file_headers")
    return corrected


def _verified_manager_identity(self: Any, core: Any, path_cls: Any) -> tuple[str, str]:
    """The verified manager actor for THIS repository, or a named refusal.

    The narrow subset :func:`validate_manager_correction` needs: a bootstrap
    that resolves the manager role for this exact repository and names both a
    provider and a session. The write gate is deliberately not re-checked here
    because ``accept_review`` has already refused ``write_gate_closed`` far
    above; :func:`dispose_review_finding` keeps its own four-step ladder and its
    own error strings, which this does not replace or change.
    """
    try:
        route = core.manager_bootstrap()
    except Exception:  # noqa: BLE001 -- an unresolvable route verifies nothing
        return "", "manager_identity_unverified"
    identity = route.get("manager_route") if isinstance(route, dict) else None
    if not isinstance(identity, dict) or route.get("role") != "manager":
        return "", "manager_identity_unverified"
    try:
        repo_matches = bool(route.get("repo")) and (
            path_cls(str(route["repo"])).resolve() == self.repo
        )
    except (OSError, TypeError, ValueError):
        repo_matches = False
    if not repo_matches:
        return "", "manager_repository_mismatch"
    provider = str(identity.get("provider") or route.get("provider") or "").strip()
    session_id = str(
        identity.get("thread_id") or identity.get("session_id") or ""
    ).strip()
    if not provider or not session_id:
        return "", "manager_session_identity_missing"
    return f"{provider}:{session_id}", ""


def validate_manager_correction(
    self: Any,
    correction: Any,
    *,
    card: Mapping[str, Any],
    workspace: Any,
    changed: list[str],
    reviewers: list[dict[str, Any]],
    reviewer_request_ids: list[str] | None,
    finding_dispositions: list[Mapping[str, Any]],
    core: Any,
    path_cls: Any,
) -> tuple[dict[str, Any] | None, dict[str, str], str]:
    """Decide one manager correction BEFORE any promotion or state change.

    Returns ``(record, corrected_texts, "")`` when the correction is valid and
    ``(None, {}, reason)`` otherwise, in which case the caller refuses with
    ``manager_correction_invalid:<reason>`` and changes nothing. Read-only
    throughout: the corrected bytes are RETURNED for the caller to write to the
    accept tree once every other cheap blocker has cleared, so a later refusal
    cannot leave a half-corrected tree behind.

    The ``record`` is this module's internal statement of the correction. Its
    ``patch_sha256``/``finding_ids``/``paths``/``candidate_path_hashes``/
    ``corrected_path_hashes``/``reason`` are the six fields
    ``accepted_outcome_receipt`` binds; ``schema_id`` and ``actor`` stay here,
    on the accept path, and are not part of the receipt identity.
    """
    if not isinstance(correction, Mapping):
        return None, {}, "not_an_object"
    unknown = sorted(set(correction) - MANAGER_CORRECTION_FIELDS)
    if unknown:
        return None, {}, ("unknown_field:" + ",".join(unknown))[:200]
    patch = correction.get("patch")
    raw_ids = correction.get("finding_ids")
    reason = correction.get("reason")
    if not isinstance(patch, str) or not patch.strip():
        return None, {}, "patch_empty"
    if (
        not isinstance(raw_ids, (list, tuple))
        or not raw_ids
        or any(not isinstance(value, str) or not value.strip() for value in raw_ids)
    ):
        return None, {}, "finding_ids_empty"
    if reason is not None and not isinstance(reason, str):
        return None, {}, "reason_invalid"
    actor, identity_reason = _verified_manager_identity(self, core, path_cls)
    if identity_reason:
        return None, {}, identity_reason

    # The findings in play are exactly the ones ``_refinement_blockers`` reads:
    # the ``defect`` findings on the SELECTED, usable reviewers' already-sealed
    # receipts. Counting ids over that same population is what makes an
    # ambiguous id refuse here rather than lift the wrong blocker there.
    selected, _unresolved, _source = _selected_reviewer_rows(
        reviewers, reviewer_request_ids
    )
    findings: dict[str, dict[str, str]] = {}
    counts: dict[str, int] = {}
    for row in selected:
        if not row.get("usable"):
            continue
        receipt = row.get("receipt")
        report = receipt.get("report") if isinstance(receipt, Mapping) else None
        if not isinstance(report, Mapping):
            continue
        lens = str(report.get("lens") or row.get("lens") or "")
        reported = report.get("findings")
        for finding in reported if isinstance(reported, list) else []:
            if not isinstance(finding, Mapping):
                continue
            finding_id = f"reviewer:{lens}:{str(finding.get('id') or '')}"
            counts[finding_id] = counts.get(finding_id, 0) + 1
            if finding.get("disposition") != "defect":
                continue
            findings[finding_id] = {
                "lens": lens,
                "severity": str(finding.get("severity") or ""),
                "reviewer_request_id": str(row.get("request_id") or ""),
            }
    confirmed = {
        (
            str(row.get("reviewer_request_id") or ""),
            str(row.get("finding_id") or ""),
        )
        for row in finding_dispositions
        if isinstance(row, Mapping)
        and str(row.get("disposition") or "") == "confirmed"
    }
    finding_ids = sorted({str(value) for value in raw_ids})
    for finding_id in finding_ids:
        finding = findings.get(finding_id)
        if finding is None:
            return None, {}, f"finding_not_found:{finding_id}"[:200]
        if counts.get(finding_id, 0) != 1:
            return None, {}, f"finding_id_ambiguous:{finding_id}"[:200]
        if finding["severity"] in quality_evidence.BLOCKING_SEVERITIES:
            return None, {}, f"finding_blocking_severity:{finding_id}"[:200]
        if finding["lens"] not in {
            quality_evidence.LENS_CORRECTNESS,
            quality_evidence.LENS_SECURITY,
        }:
            return None, {}, f"finding_lens_not_correctable:{finding_id}"[:200]
        if (finding["reviewer_request_id"], finding_id) not in confirmed:
            return None, {}, f"finding_not_confirmed:{finding_id}"[:200]

    allowed = [
        value for value in (card.get("allowed_writes") or []) if isinstance(value, str)
    ]
    in_candidate = set(changed)
    candidate_bytes: dict[str, bytes] = {}

    def _read_candidate(relative: str) -> str:
        # Scope is decided BEFORE the file is opened: a path outside the card's
        # own ``allowed_writes`` is one this accept must not read, let alone
        # promote, and saying so by name is what makes the refusal legible.
        if not any(
            relative == pattern or fnmatch.fnmatchcase(relative, pattern)
            for pattern in allowed
        ):
            raise ManagerCorrectionError(
                f"path_outside_allowed_writes:{relative}"[:200]
            )
        # A path the sealed candidate never changed is not in ``changed``, so it
        # would be corrected in the accept tree and then never promoted or
        # hash-verified. Refuse rather than promote half a correction.
        if relative not in in_candidate:
            raise ManagerCorrectionError(f"path_not_in_candidate:{relative}"[:200])
        source = workspace.path / relative
        if source.is_symlink() or not source.is_file():
            raise ManagerCorrectionError(f"patch_target_missing:{relative}"[:200])
        if source.stat().st_size > MAX_MANAGER_CORRECTION_TARGET_BYTES:
            raise ManagerCorrectionError(f"patch_target_too_large:{relative}"[:200])
        try:
            raw = source.read_bytes()
            text = raw.decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ManagerCorrectionError(
                f"patch_target_unreadable:{relative}"[:200]
            ) from exc
        if "\r\n" in text:
            raise ManagerCorrectionError(
                f"patch_target_line_endings_unsupported:{relative}"[:200]
            )
        # The exact preimage the hunks were verified against, so the candidate
        # digest below cannot come from a second, later read of the same file.
        candidate_bytes[relative] = raw
        return text

    try:
        corrected_texts = corrected_candidate_texts(patch, _read_candidate)
    except ManagerCorrectionError as exc:
        return None, {}, str(exc)[:200]

    paths = sorted(corrected_texts)
    candidate_hashes: dict[str, str] = {}
    corrected_hashes: dict[str, str] = {}
    for relative in paths:
        before = candidate_bytes[relative]
        after = corrected_texts[relative].encode("utf-8")
        if before == after:
            return None, {}, f"patch_changes_nothing:{relative}"[:200]
        candidate_hashes[relative] = hashlib.sha256(before).hexdigest()
        corrected_hashes[relative] = hashlib.sha256(after).hexdigest()
    record: dict[str, Any] = {
        "schema_id": MANAGER_CORRECTION_SCHEMA_ID,
        "patch_sha256": hashlib.sha256(patch.encode("utf-8")).hexdigest(),
        "finding_ids": finding_ids,
        "paths": paths,
        "candidate_path_hashes": candidate_hashes,
        "corrected_path_hashes": corrected_hashes,
        "reason": str(reason or "")[:2000],
        "actor": actor,
    }
    return record, corrected_texts, ""


def _restore_correction_originals(
    workspace: _WorkerWorkspaceT, originals: Mapping[str, bytes]
) -> list[str]:
    """Write back the pre-correction bytes for every corrected path.

    Called on every exit from ``accept_review`` that does not promote after
    the corrected bytes were already written into the retained worktree
    (NF-2026-01469): a correction applied but never accepted must not leave
    the worktree ahead of what the next operation expects. Every path is
    attempted even when an earlier one fails; the relative paths that could
    not be restored are returned (empty list means all restored).
    """
    failed: list[str] = []
    for relative, original in originals.items():
        try:
            (workspace.path / relative).write_bytes(original)
        except OSError:
            failed.append(relative)
    return failed


def _finalizer_validation_reuse(
    terminal_review: Mapping[str, Any],
    *,
    correction_record: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """The finalizer's recorded combined-tree run, or ``None`` when unusable.

    A manager correction changes the validated bytes, so it never reuses.
    """
    if correction_record is not None:
        return None
    evidence = terminal_review.get("evidence")
    if not isinstance(evidence, Mapping):
        return None
    rows = evidence.get("validation")
    gate = evidence.get("quality_gate")
    snapshot = gate.get("full_validation_snapshot") if isinstance(gate, Mapping) else None
    if (
        not isinstance(rows, (list, tuple))
        or not rows
        or not isinstance(snapshot, Mapping)
        or not isinstance(snapshot.get("tree_identity_sha256"), str)
    ):
        return None
    return {"full_validation_snapshot": snapshot, "validations": rows}


def _blind_reviewer_lenses(
    reviewers: list[dict[str, Any]],
    finding_dispositions: list[Mapping[str, Any]] | None = None,
) -> set[str]:
    """Lenses whose ALREADY-SEALED reports show that nothing inspected them.

    Literally ``quality_evidence.blind_reviewer_lenses`` -- the classifier
    :func:`quality_evidence.fold_quality_verdict` decides acceptance with -- over
    the very receipts the accept path would read. Sharing the function, rather
    than restating the rule, is the whole point: a second hand-written copy of
    "what counts as blind" is how a preview starts promising an acceptance the
    gate then refuses. ``finding_dispositions`` are the ALREADY-bound manager
    receipts, handed to that same function so a dismissed process_limit finding
    stops counting here exactly where the fold stops counting it (NF-2026-01762).

    Only ``receipt.report`` is consulted, and that receipt was verified and
    sealed by the server: nothing here re-verifies anything, and no report a
    model, worker or manager supplied can reach this function.
    """
    return quality_evidence.blind_reviewer_lenses(
        (
            reviewer["receipt"].get("report")
            for reviewer in reviewers
            if isinstance(reviewer.get("receipt"), Mapping)
        ),
        dismissed_by_finding=quality_evidence.dismissed_finding_receipts(
            finding_dispositions
        ),
    )


# ---------------------------------------------------------------------------
# Supplemental inspection, on the production accept path.
#
# A reviewer that returns nothing but ``process_limit`` findings has told us it
# could not READ its packet.  Until now the only thing that could happen next
# was a manager noticing ``reviewer_could_not_inspect`` in a blocker list and
# launching a reviewer by hand -- or, worse, sending the candidate back through
# the implementation worker, whose bytes were never in question.
#
# The three helpers below make the proportionate answer mechanical: recover the
# exact packet the blind reviewer was sealed against, count how many re-reads
# this lens has already had, and execute the ONE bounded round the gate's own
# plan authorized.  None of them can accept anything: every failure path here
# leaves the acceptance exactly as blocked as it already was.
# ---------------------------------------------------------------------------
SUPPLEMENTAL_REVIEW_SCHEMA_ID = "aiworkhub.accept_supplemental_review.v1"

# The reviewer packet cap the worker MCP surface already enforces when it hands
# the packet to a reviewer. Re-stated here because this read is a SECOND reader
# of the same file and must not become the one unbounded path to it.
MAX_SEALED_PACKET_BYTES = 8 * 1024 * 1024


def sealed_review_packet(
    metadata: Mapping[str, Any],
    workspace: Any,
    receipt: Mapping[str, Any],
) -> dict[str, Any] | None:
    """The exact packet THIS reviewer was sealed against, or ``None``.

    Three independent facts have to line up before a packet is returned, and
    each one is checked here rather than assumed from the one before it: the
    file lives inside the reviewer's own home, its body hashes to its own
    ``packet_sha256``, and that digest is the one the already-verified receipt
    is bound to. A packet that fails any of them is not this reviewer's packet.

    Fail-closed by construction, and cheaply so: ``None`` can only REMOVE the
    supplemental round the gate would otherwise plan. There is no return value
    of this function that satisfies a lens, clears a blocker or weakens
    ``reviewer_could_not_inspect``.
    """

    import json as _json
    from pathlib import Path as _Path

    from . import quality_reviewer

    binding = metadata.get("quality_review") if isinstance(metadata, Mapping) else None
    if not isinstance(binding, Mapping) or not isinstance(receipt, Mapping):
        return None
    raw_path = binding.get("packet_path")
    if not isinstance(raw_path, str) or not raw_path:
        return None
    try:
        packet_path = _Path(raw_path).resolve()
        packet_path.relative_to(_Path(workspace.home).resolve())
        if packet_path.is_symlink() or not packet_path.is_file():
            return None
        if packet_path.stat().st_size > MAX_SEALED_PACKET_BYTES:
            return None
        packet = _json.loads(packet_path.read_text(encoding="utf-8"))
    except (AttributeError, OSError, TypeError, ValueError):
        # ValueError covers both the outside-home ``relative_to`` refusal and
        # every decode failure (JSONDecodeError/UnicodeDecodeError subclass it).
        return None
    if not isinstance(packet, dict):
        return None
    sealed = str(packet.get("packet_sha256") or "")
    if not sealed or sealed != str(receipt.get("packet_sha256") or ""):
        return None
    body = {key: value for key, value in packet.items() if key != "packet_sha256"}
    if quality_reviewer._canonical_digest(body) != sealed:
        return None
    return packet


def supplemental_rounds_completed(
    reviewers: list[dict[str, Any]],
) -> dict[str, int]:
    """How many re-reads each lens has already had, counted from the store.

    The first reviewer for a lens is the review itself; every additional bound
    reviewer task for that same lens is a round that already EXISTS. Counting
    them off the reviewer children -- the same rows the accept fold reads --
    means the bound needs no new durable state and cannot drift from what the
    store actually shows.

    "Exists" rather than "finished" is deliberate and errs toward refusing: a
    second reviewer for a lens that is still running already consumed the
    round, and launching another beside it would spend the bound twice on the
    same question.
    """

    counts: dict[str, int] = {}
    for row in reviewers:
        lens = str(row.get("lens") or "")
        if lens:
            counts[lens] = counts.get(lens, 0) + 1
    return {lens: max(0, count - 1) for lens, count in counts.items()}


def _reviewer_launch_route(self, reviewer: Mapping[str, Any]) -> dict[str, str]:
    """The runner/adapter the blind reviewer itself ran on.

    A supplemental round is the same lens on the same route reading the same
    packet: nothing about the review is re-planned, only re-read. An identity
    this cannot recover returns ``{}``, which refuses the round rather than
    guessing a provider.
    """

    request_id = str(reviewer.get("request_id") or "")
    if not request_id:
        return {}
    try:
        events = self._request_events(request_id)
    except Exception:  # noqa: BLE001 -- an unreadable ledger launches nothing
        return {}
    event = _latest_request_identity_event(
        [row for row in (events or []) if isinstance(row, dict)],
        str(reviewer.get("task_id") or ""),
    )
    if event is None:
        return {}
    runner = str(event.get("runner") or "")
    adapter_id = str(event.get("adapter_id") or runner)
    if not runner or not adapter_id:
        return {}
    return {"runner": runner, "adapter_id": adapter_id}


def create_supplemental_inspection_rounds(
    self,
    *,
    request_id: str,
    task_id: str,
    reviewers: list[dict[str, Any]],
    quality_gate: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Execute the bounded re-reads the gate's own plan already authorized.

    This decides nothing. ``run_completion_quality_gate`` computed the plan
    from the sealed packet and the recorded round count; this only launches the
    rounds it marked eligible, on the blind reviewer's own route, against the
    same target request. The implementation worker is never relaunched -- the
    candidate is not what failed -- and no manager has to name a reviewer.

    Every row is reported, launched or not, with the reason it was not: a round
    that could not be created must be visible, because the acceptance stays
    blocked either way and the manager needs to know which of the two states
    they are in.
    """

    plans = quality_gate.get("supplemental_inspection")
    if not isinstance(plans, list) or not plans:
        return []
    launch = getattr(self, "launch_quality_reviewer", None)
    rows: list[dict[str, Any]] = []
    for plan in plans:
        if not isinstance(plan, Mapping):
            continue
        lens = str(plan.get("lens") or "")
        row: dict[str, Any] = {
            "schema_id": SUPPLEMENTAL_REVIEW_SCHEMA_ID,
            "lens": lens,
            "round": int(plan.get("round") or 0),
            "max_rounds": int(plan.get("max_rounds") or 0),
            "packet_sha256": str(plan.get("packet_sha256") or ""),
            "inspection_target_count": len(plan.get("inspection_targets") or []),
            "eligible": bool(plan.get("eligible")),
            "reason": str(plan.get("reason") or "")[:200],
            "created": False,
            "reviewer_task_id": "",
            "reviewer_request_id": "",
            "implementation_worker_relaunched": False,
        }
        if not row["eligible"]:
            rows.append(row)
            continue
        prior = next(
            (
                candidate
                for candidate in reviewers
                if str(candidate.get("lens") or "") == lens
                and str(candidate.get("task_id") or "")
            ),
            None,
        )
        route = _reviewer_launch_route(self, prior) if prior is not None else {}
        if not callable(launch) or not route:
            row["reason"] = "supplemental_route_unavailable"
            rows.append(row)
            continue
        reviewer_task_id = f"{prior['task_id']}-SUPPLEMENTAL-{row['round']}"
        row["reviewer_task_id"] = reviewer_task_id
        try:
            result = launch(
                target_request_id=request_id,
                target_task_id=task_id,
                reviewer_task_id=reviewer_task_id,
                runner=route["runner"],
                adapter_id=route["adapter_id"],
                lens=lens,
            )
        except Exception as exc:  # noqa: BLE001 -- a refused launch never accepts
            row["reason"] = f"supplemental_launch_failed:{type(exc).__name__}"[:200]
            rows.append(row)
            continue
        result = result if isinstance(result, Mapping) else {}
        row["created"] = result.get("ok") is True
        row["reviewer_request_id"] = str(result.get("request_id") or "")
        if not row["created"]:
            row["reason"] = str(
                result.get("error") or "supplemental_launch_refused"
            )[:200]
        rows.append(row)
    return rows


def fold_accept_blockers(
    *,
    reviewers: list[dict[str, Any]],
    reviewer_request_ids: list[str] | None,
    risk_profile: Mapping[str, Any],
    terminal_substatus: str,
    pending_context_write_intents: int = 0,
    confirm_high_risk: bool = False,
    explicit_human_approval: bool = False,
    repository_requires_explicit_human_approval: bool = False,
    destructive_blockers: list[str] | None = None,
    confirm_destructive_change: bool = False,
    finding_dispositions: list[Mapping[str, Any]] | None = None,
    manager_correction: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Every acceptance blocker decidable from persisted evidence alone.

    Measured 2026-09-08 over 159 accept attempts: 49 (31%) failed on exactly
    these conditions -- ``required_reviewer_missing`` 19,
    ``quality_reviewer_not_review_ready`` 17,
    ``terminal_substatus_not_review_ready`` 8,
    ``explicit_human_approval_missing`` 5 -- and every one of them failed AFTER
    a combined-tree workspace had been materialized and validated twice. None
    of these needs a single byte of that tree.

    Two rules keep this honest, and they point in opposite directions:

    * it may never ACCEPT anything and never lowers a bar --
      ``fold_quality_verdict`` still runs in full at acceptance over the same
      profile, so a tier planned too narrowly upstream still fails closed
      there; and
    * it may never INVENT a refusal the real path would not have made. When a
      manager names a reviewer request this fold cannot see -- an id outside
      the bound-children scan, whose lens is therefore unknown -- the missing
      -lens question is left entirely to the authoritative loop, which resolves
      that id by request id and verifies its receipt. Not-listed-here is not
      evidence of not-existing.
    """
    required = [
        str(lens) for lens in (risk_profile.get("required_reviewer_lenses") or ())
    ]
    selected = (
        None if reviewer_request_ids is None else [str(v) for v in reviewer_request_ids]
    )
    chosen, unresolved, source = _selected_reviewer_rows(
        reviewers, reviewer_request_ids
    )
    blockers: list[dict[str, Any]] = []
    if str(terminal_substatus) != "review_ready":
        blockers.append(
            _blocker("terminal_substatus_not_review_ready", str(terminal_substatus))
        )
    if int(pending_context_write_intents or 0) > 0:
        blockers.append(
            _blocker(
                "context_write_intents_pending",
                str(int(pending_context_write_intents)),
            )
        )
    for reviewer in chosen:
        if reviewer["usable"]:
            continue
        blockers.append(
            _blocker(
                "quality_reviewer_not_review_ready",
                reviewer["request_id"] or reviewer["task_id"],
                lens=reviewer["lens"], state=reviewer["state"],
            )
        )
    # A reviewer this fold cannot see, or one whose packet binding carries no
    # lens, makes the lens census incomplete -- and an incomplete census cannot
    # say a lens is missing. The accept fold still can, and does.
    lens_census_complete = not unresolved and all(row["lens"] for row in chosen)
    usable_lenses = {row["lens"] for row in chosen if row["usable"]}
    # Running is the launcher's liveness verdict (``running``), never a state
    # string; an unreadable ledger is ``state_unknown``, never "not running".
    running_rows = {
        row["lens"]: row for row in reviewers if row.get("running") and not row["usable"]
    }
    state_unknown = any(row.get("state_unknown") for row in reviewers)
    state_unknown_cause = next(
        (
            str(row.get("state_unknown_cause"))[:80]
            for row in reviewers
            if row.get("state_unknown_cause")
        ),
        "",
    )
    if lens_census_complete:
        for lens in required:
            if lens in usable_lenses:
                continue
            if state_unknown:
                cause = {"cause": state_unknown_cause} if state_unknown_cause else {}
                blockers.append(
                    _blocker("reviewer_state_unknown", lens, lens=lens, **cause)
                )
                continue
            live = running_rows.get(lens)
            blockers.append(
                _blocker(
                    "required_reviewer_missing", lens,
                    lens=lens,
                    # The difference between "launch one" and "wait for the one
                    # already running" -- the manager's next action, named.
                    reviewer_running=live is not None,
                    **({
                        "reviewer_task_id": live["task_id"],
                        "reviewer_request_id": (
                            live.get("running_request_id") or live["request_id"]
                        ),
                    } if live is not None else {}),
                )
            )
    evidence_rows = [row for row in chosen if row["usable"]]
    disposition_rows: list[dict[str, Any]] = []
    blockers.extend(
        _refinement_blockers(
            evidence_rows,
            finding_dispositions=finding_dispositions,
            disposition_rows=disposition_rows,
            manager_correction=manager_correction,
        )
    )
    # NF-2026-01158: the refusal the accept fold WILL make for a lens nothing
    # inspected, predicted here from the same already-verified receipts with the
    # fold's own classifier -- so a manager never reads a clear preview and then
    # meets ``reviewer_could_not_inspect:<lens>`` after paying for a combined
    # tree and two validation runs.
    #
    # Gated on ``required`` for the same reason ``fold_quality_verdict`` gates it
    # on ``review_active``: at a tier that runs no attributable review an
    # unsolicited blind report is non-blocking noise there, so claiming it blocks
    # here would be a refusal the real path never makes. The lens set itself is
    # deliberately NOT narrowed to ``required`` -- the fold blocks on a blind
    # report for any judgment lens, and predicting less would be the same
    # disagreement in the other direction.
    if required:
        for lens in sorted(
            _blind_reviewer_lenses(
                evidence_rows, finding_dispositions=finding_dispositions
            )
        ):
            blockers.append(_blocker("reviewer_could_not_inspect", lens, lens=lens))
    # NF-2026-01742: the manager's ``confirm_high_risk`` and an owner approval
    # are different facts; the one shared predicate decides which clears this.
    if risk_profile.get(
        "explicit_human_approval_required"
    ) and not quality_evidence.high_risk_approval_satisfied(
        explicit_human_approval=explicit_human_approval,
        manager_high_risk_confirmed=confirm_high_risk,
        repository_requires_explicit_human_approval=(
            repository_requires_explicit_human_approval
        ),
    ):
        blockers.append(_blocker("explicit_human_approval_missing"))
    if not confirm_destructive_change:
        for check_id in destructive_blockers or ():
            blockers.append(
                _blocker(
                    "destructive_diff_requires_manager_confirmation", str(check_id)
                )
            )
    return {
        "blockers": blockers,
        "reviewer_request_ids": (
            [row["request_id"] for row in chosen if row["usable"]]
            if selected is None
            else list(selected)
        ),
        "reviewer_request_id_source": source,
        "required_reviewer_lenses": required,
        "lens_census_complete": lens_census_complete,
        "unresolved_reviewer_request_ids": unresolved,
        "per_lens": sorted(
            (
                {
                    "lens": row["lens"],
                    "task_id": row["task_id"],
                    "request_id": row["request_id"],
                    "state": row["state"],
                    "usable": row["usable"],
                    "running": bool(row.get("running")),
                    "required": row["lens"] in required,
                    "selected": any(
                        chosen_row is row for chosen_row in chosen
                    ),
                    "has_verified_report": isinstance(row.get("receipt"), Mapping),
                }
                for row in reviewers
            ),
            key=lambda entry: (entry["lens"], entry["task_id"]),
        ),
        # NF-2026-01170: each finding's bound manager disposition, shown only
        # when one exists so a fold without dispositions is unchanged.
        **(
            {"finding_dispositions": disposition_rows}
            if disposition_rows
            else {}
        ),
    }


# ---------------------------------------------------------------------------
# NF-2026-01626: one in-flight accept per (task_id, request_id).
#
# Measured 2026-10-20 on needfix-NF-2026-01616: the accept-lane validation ran
# for 15 minutes, the MCP client gave up at 1800 s with no progress, and the
# server recorded the accept about a minute later -- while the manager, holding
# only the timeout, took it for cancelled and nearly started a second accept.
# ``accept_review`` now claims a marker before either acceptance lock, so a
# duplicate answers ``accept_in_progress`` at once instead of queueing behind
# the first one's validation, and ``accept_preview`` says the same to a
# manager whose call timed out.
#
# The marker has two witnesses. A small JSON file, created O_CREAT|O_EXCL in
# the process dir beside the request locks, is what another server process
# sees and judges by its holder's pid identity. An in-process entry is the only
# witness that can tell a thread of THIS process still running the accept from
# a file this process failed to remove: this pid is alive by definition.
# ---------------------------------------------------------------------------

ACCEPT_IN_FLIGHT_SCHEMA_ID = "aiworkhub.accept_in_flight.v1"
ACCEPT_IN_FLIGHT_EVENT_SCHEMA_ID = "aiworkhub.accept_in_flight_event.v1"
_ACCEPT_IN_FLIGHT_DIRNAME = ".accept-in-flight"
_ACCEPT_IN_FLIGHT_LOCK_NAME = ".lock"
# Operator-visible evidence: every stale marker cleared, every failed release.
_ACCEPT_IN_FLIGHT_EVENTS_NAME = "events.jsonl"
_ACCEPT_IN_FLIGHT_MAX_BYTES = 4096
# A marker is created empty and filled by the very next write, so a lock-free
# reader landing between the two reads again before calling it unreadable.
_ACCEPT_IN_FLIGHT_READ_ATTEMPTS = 5
# Windows refuses to unlink a file another process holds open -- a preview
# reading the marker at that instant -- so a removal retries, boundedly.
_ACCEPT_IN_FLIGHT_UNLINK_ATTEMPTS = 20
_ACCEPT_IN_FLIGHT_RETRY_SECONDS = 0.01
_ACCEPT_IN_FLIGHT_CREATE_FLAGS = (
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
)
_ACCEPT_IN_FLIGHT_GUARD = threading.Lock()
# Marker key -> the payload this process holds. An entry lives exactly as long
# as one accept_review call, so the map is bounded by concurrent accepts.
_ACCEPT_IN_FLIGHT_HELD: dict[str, dict[str, Any]] = {}


def _accept_in_flight_key(task_id: str, request_id: str) -> str:
    # The marker key: one file per exact task/request pair.
    return hashlib.sha256("\x00".join((task_id, request_id)).encode("utf-8")).hexdigest()


def _accept_in_flight_path(self: Any, task_id: str, request_id: str) -> Path:
    """The marker for one exact (task_id, request_id), beside the request locks."""
    return (
        Path(self.process_dir)
        / _ACCEPT_IN_FLIGHT_DIRNAME
        / f"{_accept_in_flight_key(task_id, request_id)}.json"
    )


def _forget_accept_in_flight(key: str) -> None:
    with _ACCEPT_IN_FLIGHT_GUARD:
        _ACCEPT_IN_FLIGHT_HELD.pop(key, None)


def _read_accept_marker(path: Path) -> tuple[str, dict[str, Any]]:
    """``("absent" | "present" | "unreadable", marker)`` for one marker file."""
    for attempt in range(_ACCEPT_IN_FLIGHT_READ_ATTEMPTS):
        if attempt:
            time.sleep(_ACCEPT_IN_FLIGHT_RETRY_SECONDS)
        try:
            with path.open("rb") as handle:
                raw = handle.read(_ACCEPT_IN_FLIGHT_MAX_BYTES + 1)
        except (FileNotFoundError, NotADirectoryError):
            # No marker can exist where its directory does not: the claim's
            # own mkdir then names the cause, identically on every platform.
            return "absent", {}
        except OSError:
            continue
        try:
            marker = json.loads(raw.decode("utf-8"))
        except (ValueError, RecursionError):
            # A deeply nested marker exhausts the decoder's recursion limit:
            # it is unreadable like any other bytes that do not parse.
            continue
        pid = marker.get("pid") if isinstance(marker, dict) else None
        if (
            len(raw) <= _ACCEPT_IN_FLIGHT_MAX_BYTES
            and isinstance(pid, int)
            and not isinstance(pid, bool)
            and pid > 0
        ):
            return "present", marker
    return "unreadable", {}


def _accept_marker_holder(marker: Mapping[str, Any], *, held_here: bool) -> str:
    """``live``, ``dead`` or ``unknown`` for the process that wrote ``marker``.

    Fail-closed like every verdict in ``process_identity``: only a holder
    PROVEN gone -- not alive, or a different process now wearing its pid -- is
    ``dead``, and a pid whose identity cannot be read is ``unknown``. A marker
    naming this very process is judged by ``held_here`` alone.
    """
    pid = int(marker["pid"])
    ticks = marker.get("pid_start_ticks")
    if pid == os.getpid() and ticks == _pid_start_ticks(pid):
        return "live" if held_here else "dead"
    if not process_is_alive(pid):
        return "dead"
    verdict = _pid_identity_evidence(pid, ticks).verdict
    if verdict is PidIdentityVerdict.MATCH:
        return "live"
    if verdict is PidIdentityVerdict.MISMATCH:
        return "dead"
    return "unknown"


def _accept_in_progress(
    task_id: str, request_id: str, marker: Mapping[str, Any], *, verified: bool
) -> dict[str, Any]:
    """The one refusal shape for an accept that is still running."""
    return {
        "ok": False,
        "error": "accept_in_progress",
        "request_id": request_id,
        "task_id": task_id,
        "started_at": marker.get("started_at"),
        "holder_pid": marker.get("pid"),
        "holder_server_version": marker.get("server_version"),
        "holder_identity": "verified" if verified else "unverified",
    }


def _accept_in_flight_answer(
    task_id: str,
    request_id: str,
    state: str,
    marker: Mapping[str, Any],
    *,
    held_here: bool,
) -> dict[str, Any] | None:
    """The refusal one marker forces, or None when it is absent or stale."""
    if state == "absent":
        return None
    if state != "present":
        return {
            "ok": False,
            "error": "accept_in_flight_marker_unreadable",
            "request_id": request_id,
            "task_id": task_id,
            "marker": (
                f"{_ACCEPT_IN_FLIGHT_DIRNAME}/"
                f"{_accept_in_flight_key(task_id, request_id)}.json"
            ),
        }
    holder = _accept_marker_holder(marker, held_here=held_here)
    if holder == "dead":
        return None
    return _accept_in_progress(task_id, request_id, marker, verified=holder == "live")


def accept_in_flight(self: Any, task_id: str, request_id: str) -> dict[str, Any] | None:
    """READ-ONLY: the refusal an accept of this exact request gets right now.

    ``accept_in_progress`` -- with the holder's ``started_at`` and
    ``holder_pid`` -- while an accept of the same (task_id, request_id) runs in
    this or any other server process; ``accept_in_flight_marker_unreadable``
    when its marker cannot be read; otherwise None. Takes no lock and writes
    nothing, so a stale marker reads as None here: only ``accept_review``
    clears one.
    """
    state, marker = _read_accept_marker(_accept_in_flight_path(self, task_id, request_id))
    # Consulted AFTER the file, so a thread here that claimed or released in
    # between is judged by what it holds now, not by what it held a moment ago.
    with _ACCEPT_IN_FLIGHT_GUARD:
        held = _ACCEPT_IN_FLIGHT_HELD.get(_accept_in_flight_key(task_id, request_id))
    if held is not None:
        return _accept_in_progress(task_id, request_id, held, verified=True)
    return _accept_in_flight_answer(task_id, request_id, state, marker, held_here=False)


@contextlib.contextmanager
def _accept_marker_dir_lock(directory: Path) -> Iterator[None]:
    """Serialize every marker check-and-create and every removal in one dir.

    Held for one read, one liveness probe and one create or unlink -- never
    across an accept -- so a stale marker two server processes judge at once is
    replaced by exactly one of them.
    """
    fd = os.open(
        directory / _ACCEPT_IN_FLIGHT_LOCK_NAME,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0),
        0o600,
    )
    try:
        lock_fd(fd, blocking=True)
        try:
            yield
        finally:
            unlock_fd(fd)
    finally:
        os.close(fd)


def _append_accept_in_flight_event(directory: Path, record: Mapping[str, Any]) -> None:
    """Append one operator-visible line; raises ``OSError`` when it did not land."""
    from . import process_launcher as _pl

    line = json.dumps(
        {
            "schema_id": ACCEPT_IN_FLIGHT_EVENT_SCHEMA_ID,
            "recorded_at": _pl._utcnow(),
            **record,
        },
        sort_keys=True,
        default=str,
    )
    with (directory / _ACCEPT_IN_FLIGHT_EVENTS_NAME).open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def _unlink_accept_marker(path: Path) -> None:
    """Remove one marker, waiting out the sharing violation a Windows reader causes."""
    for attempt in range(_ACCEPT_IN_FLIGHT_UNLINK_ATTEMPTS):
        try:
            path.unlink()
            return
        except FileNotFoundError:
            return
        except PermissionError:
            if attempt + 1 == _ACCEPT_IN_FLIGHT_UNLINK_ATTEMPTS:
                raise
            time.sleep(_ACCEPT_IN_FLIGHT_RETRY_SECONDS)


def _create_accept_marker(
    path: Path, payload: Mapping[str, Any], *, task_id: str, request_id: str
) -> tuple[dict[str, Any] | None, bytes]:
    """Create ``path`` holding ``payload``: ``(refusal, bytes_written)``.

    Judged twice. First lock-free, so a duplicate of a live accept is refused
    without writing anything -- not even the lock file. Then under the
    directory lock, where a marker whose holder is proven gone is recorded,
    removed and replaced; a stale marker that cannot be recorded stays put.
    """
    state, marker = _read_accept_marker(path)
    refusal = _accept_in_flight_answer(task_id, request_id, state, marker, held_here=False)
    if refusal is not None:
        return refusal, b""
    data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with _accept_marker_dir_lock(path.parent):
        state, marker = _read_accept_marker(path)
        refusal = _accept_in_flight_answer(
            task_id, request_id, state, marker, held_here=False
        )
        if refusal is not None:
            return refusal, b""
        if state == "present":
            _append_accept_in_flight_event(
                path.parent,
                {
                    "event": "stale_marker_cleared",
                    "task_id": task_id,
                    "request_id": request_id,
                    "stale_marker": dict(marker),
                    "cleared_by_pid": payload.get("pid"),
                },
            )
            try:
                _unlink_accept_marker(path)
            except OSError as exc:
                # The cleared line above records the intent; when the removal
                # then fails, the log says so instead of claiming a clear.
                with contextlib.suppress(OSError):
                    _append_accept_in_flight_event(
                        path.parent,
                        {
                            "event": "stale_marker_clear_failed",
                            "task_id": task_id,
                            "request_id": request_id,
                            "error": type(exc).__name__,
                        },
                    )
                raise
        fd = os.open(path, _ACCEPT_IN_FLIGHT_CREATE_FLAGS, 0o600)
        try:
            try:
                if os.write(fd, data) != len(data):
                    raise OSError(errno.EIO, "short write to the accept in-flight marker")
            finally:
                os.close(fd)
        except BaseException:
            with contextlib.suppress(OSError):
                path.unlink()
            raise
    return None, data


def _release_accept_marker(path: Path, data: bytes) -> None:
    """Remove this accept's own marker; never raises past the accept it ends.

    The accept's outcome -- its receipt above all -- is already decided, so a
    failed removal is recorded instead of raised. The marker left behind names
    this pid and its creation ticks: the next accept here clears it as this
    process's own leftover, and any other process does once this one exits.
    A marker holding other bytes belongs to whoever wrote them and stays.
    """
    try:
        with _accept_marker_dir_lock(path.parent):
            try:
                current: bytes | None = path.read_bytes()
            except FileNotFoundError:
                current = None
            if current == data:
                _unlink_accept_marker(path)
    except OSError as exc:
        with contextlib.suppress(OSError):
            _append_accept_in_flight_event(
                path.parent,
                {
                    "event": "release_failed",
                    "marker": path.name,
                    "error": type(exc).__name__,
                },
            )


class _AcceptInFlight:
    """One ``accept_review`` call's claim on its in-flight marker.

    A refused claim holds nothing and carries the ``refusal`` to return.
    Otherwise this is the context manager that releases the marker -- file and
    in-process entry both -- on every exit, success and exception alike.
    """

    def __init__(
        self,
        *,
        refusal: dict[str, Any] | None = None,
        key: str = "",
        path: Path | None = None,
        data: bytes = b"",
    ) -> None:
        self.refusal = refusal
        self._key = key
        self._path = path
        self._data = data

    def __enter__(self) -> _AcceptInFlight:
        return self

    def __exit__(self, *exc_info: object) -> None:
        key, path = self._key, self._path
        self._key, self._path = "", None
        if not key:
            return
        try:
            if path is not None:
                _release_accept_marker(path, self._data)
        finally:
            _forget_accept_in_flight(key)


def _accept_in_flight_marker(self: Any, task_id: str, request_id: str) -> _AcceptInFlight:
    """Claim the in-flight marker for one ``accept_review`` call.

    The in-process entry is reserved first, so a second thread of this server
    is refused without touching the disk; the file follows. A duplicate of a
    live accept is refused without writing anything, and every refusal names
    its own cause.
    """
    from . import process_launcher as _pl

    key = _accept_in_flight_key(task_id, request_id)
    payload: dict[str, Any] = {
        "schema_id": ACCEPT_IN_FLIGHT_SCHEMA_ID,
        "task_id": task_id,
        "request_id": request_id,
        "pid": os.getpid(),
        "pid_start_ticks": _pid_start_ticks(os.getpid()),
        "started_at": _pl._utcnow(),
        "server_version": __version__,
    }
    with _ACCEPT_IN_FLIGHT_GUARD:
        held = _ACCEPT_IN_FLIGHT_HELD.setdefault(key, payload)
    if held is not payload:
        return _AcceptInFlight(
            refusal=_accept_in_progress(task_id, request_id, held, verified=True)
        )
    try:
        path = _accept_in_flight_path(self, task_id, request_id)
        refusal, data = _create_accept_marker(
            path, payload, task_id=task_id, request_id=request_id
        )
    except OSError as exc:
        refusal = {
            "ok": False,
            "error": f"accept_in_flight_marker_unavailable:{type(exc).__name__}",
            "request_id": request_id,
            "task_id": task_id,
        }
    except BaseException:
        _forget_accept_in_flight(key)
        raise
    if refusal is not None:
        _forget_accept_in_flight(key)
        return _AcceptInFlight(refusal=refusal)
    return _AcceptInFlight(key=key, path=path, data=data)


def accept_preview(self, request_id: str, task_id: str, **overrides: Any) -> dict[str, Any]:
    """Read-only: exactly what would block ``accept_review`` right now.

    Same fold, same inputs, no writes, no workspace, no combined tree -- so a
    manager learns the answer before paying for the two validation runs that
    preceded 49 of 159 measured failures. Optional ``overrides`` mirror the
    accept parameters (``requested_risk_tier``, ``reviewer_request_ids``,
    ``confirm_high_risk``, ``confirm_destructive_change``).

    A clear preview is NOT an acceptance and NOT a promise of one: the
    expensive half -- the combined tree, the declared validations, the
    mechanical gate, the reviewer receipt verification by request id -- runs
    only in ``accept_review`` and can still refuse. ``blocked`` False means
    only that nothing cheap is refusing yet.
    """
    from . import process_launcher as _pl

    result: dict[str, Any] = {
        "ok": True,
        "schema_id": ACCEPT_PREVIEW_SCHEMA_ID,
        "request_id": request_id,
        "task_id": task_id,
        "authoritative": False,
        "evaluated": False,
    }
    try:
        card = _pl._parse_card(self._show_task(task_id), task_id)
    except Exception as exc:  # noqa: BLE001 -- a preview never raises at a manager
        return {**result, "ok": False, "error": f"task_lookup_failed:{exc}"[:300]}
    # NF-2026-01626: a manager whose accept call timed out asks here instead of
    # retrying it. While that accept still runs, that is the whole answer -- its
    # holder and start time, under the same closed blocker set as the rest.
    try:
        in_flight = accept_in_flight(self, task_id, request_id)
    except Exception as exc:  # noqa: BLE001 -- a preview never raises at a manager
        return {
            **result,
            "ok": False,
            "error": f"accept_in_flight_unknown:{type(exc).__name__}",
        }
    if in_flight is not None:
        facts = {
            key: value
            for key, value in in_flight.items()
            if key not in {"ok", "error", "request_id", "task_id"}
        }
        answer = {
            **result,
            "blocked": True,
            "canonical_status": _pl._canonical_task_status(card),
            "blockers": [_blocker(str(in_flight["error"]), **facts)],
        }
        if in_flight["error"] == "accept_in_progress":
            answer["accept_in_progress"] = facts
        return answer
    terminal_review = card.get("terminal_review")
    terminal_review = terminal_review if isinstance(terminal_review, Mapping) else {}
    evidence = terminal_review.get("evidence")
    evidence = evidence if isinstance(evidence, Mapping) else {}
    changed = [str(value) for value in (evidence.get("changed_paths") or ())]
    requested = effective_requested_risk_tier(
        card, overrides.get("requested_risk_tier")
    )
    try:
        signals = quality_evidence.derive_risk_signals(card, changed)
        risk_profile = quality_evidence.resolve_risk_profile(requested, signals=signals)
    except Exception as exc:  # noqa: BLE001
        return {
            **result,
            "ok": False,
            "error": f"risk_profile_unavailable:{type(exc).__name__}:{exc}"[:300],
        }
    intents = 0
    try:
        snapshot = self._context_write_intent_snapshot(request_id)
        if snapshot.get("ok"):
            intents = int((snapshot.get("counts") or {}).get("pending") or 0)
    except Exception:  # noqa: BLE001 -- an unreadable snapshot blocks nothing here
        intents = 0
    preview_reviewers = reviewer_evidence(self, task_id, request_id)
    preview_dispositions = bound_finding_dispositions(
        self,
        task_id,
        request_id,
        candidate_sha256=_target_candidate_sha256(card),
        receipt_sha256_by_reviewer=_sealed_receipt_sha256(preview_reviewers),
    )
    # The CANONICAL repository's policy, as accept_review reads it; a repo the
    # preview cannot read fails closed to requiring the owner.
    try:
        owner_approval_required = (
            quality_evidence.repository_requires_explicit_human_approval(self.repo)
        )
    except Exception:  # noqa: BLE001 -- a preview never raises at a manager
        owner_approval_required = True
    fold = fold_accept_blockers(
        reviewers=preview_reviewers,
        reviewer_request_ids=overrides.get("reviewer_request_ids"),
        risk_profile=risk_profile,
        terminal_substatus=str(terminal_review.get("substatus") or ""),
        pending_context_write_intents=intents,
        confirm_high_risk=bool(overrides.get("confirm_high_risk")),
        repository_requires_explicit_human_approval=owner_approval_required,
        **(
            {"finding_dispositions": preview_dispositions}
            if preview_dispositions
            else {}
        ),
    )
    return {
        **result,
        "evaluated": True,
        "blocked": bool(fold["blockers"]),
        "canonical_status": _pl._canonical_task_status(card),
        "terminal_substatus": str(terminal_review.get("substatus") or ""),
        "changed_path_count": len(changed),
        "risk_profile": {
            "requested_tier": risk_profile["requested_tier"],
            "effective_tier": risk_profile["effective_tier"],
            "signals": list(risk_profile["signals"]),
            "required_reviewer_lenses": list(risk_profile["required_reviewer_lenses"]),
            "combined_tree_required": bool(risk_profile["combined_tree_required"]),
            "explicit_human_approval_required": bool(
                risk_profile["explicit_human_approval_required"]
            ),
            "server_derived_tier": server_derived_risk_tier(card),
            "card_declared_risk_tier": str(card.get("risk_tier") or ""),
        },
        "pending_context_write_intents": intents,
        **fold,
    }


def preview_reviewer_reports(
    self: Any,
    task_id: str,
    request_id: str,
    reviewer_request_ids: Any,
) -> list[dict[str, Any]]:
    """Read-only: the sealed reports of the selected usable reviewers.

    The same selection the accept verification loop reads, so a manager's
    packet shows the findings it will be asked to dispose. Never raises: an
    unreadable reviewer ledger yields no reports.
    """
    try:
        selected = {str(value) for value in (reviewer_request_ids or ())}
        reports: list[dict[str, Any]] = []
        for row in reviewer_evidence(self, task_id, request_id):
            reviewer_request_id = str(row.get("request_id") or "")
            receipt = row.get("receipt")
            if (
                not row.get("usable")
                or not reviewer_request_id
                or reviewer_request_id not in selected
                or not isinstance(receipt, Mapping)
                or not isinstance(receipt.get("report"), Mapping)
            ):
                continue
            reports.append(
                {**dict(receipt["report"]), "reviewer_request_id": reviewer_request_id}
            )
        return reports
    except Exception:  # noqa: BLE001 -- a packet read never raises at a manager
        return []


def _stale_inputs_touching_candidate(
    drifted_inputs: list[str], candidate_paths: list[str]
) -> list[str]:
    """The drifted immutable inputs that cover a path the candidate changed.

    A declared input may be a file, a directory whose manifest covers every
    entry below it, or a dependency's write pattern (``allowed_writes``
    semantics), so equality alone would call an edit under a drifted directory
    unrelated. Both sides are compared slash-normalized and case-folded, and an
    input naming the whole tree covers every path: each of those can only add a
    refusal, never remove one (NF-2026-01543).
    """

    def _normalized(value: str) -> str:
        return posixpath.normpath(str(value).replace("\\", "/")).lstrip("/").casefold()

    candidates = {_normalized(path) for path in candidate_paths}
    touching: list[str] = []
    for declared in drifted_inputs:
        root = _normalized(declared)
        if root in {"", "."} or any(
            path == root
            or path.startswith(root + "/")
            or root.startswith(path + "/")
            or fnmatch.fnmatchcase(path, root)
            for path in candidates
        ):
            touching.append(declared)
    return touching


# The seams are runtime names only.  ``Any`` and ``_WorkerWorkspaceT`` appear
# solely in annotations -- which ``from __future__ import annotations`` leaves
# as strings -- so they are resolved from this module's own imports and are
# deliberately NOT re-bound below.  A name that is never evaluated cannot carry
# a monkeypatch, and shadowing it here would only hide it from the type checker.
#
# THIS module's own names are the second group.  They were never in
# ``process_launcher``, so there is no seam to preserve and re-binding them off
# ``_pl`` would raise ``AttributeError``; a test that patches one patches it
# here, where it lives.  Two kinds qualify: helpers defined in this module, and
# a sibling module imported here and nowhere else on the launcher.  They are
# declared all the same, because the invariant the seam test protects is "no
# global enters this body unannounced" -- and a name that belongs to neither
# list is exactly the drift it exists to catch.
ACCEPT_REVIEW_LOCAL_NAMES: tuple[str, ...] = (
    "_accept_in_flight_marker",
    "_finalizer_validation_reuse",
    "_accept_manager_identity",
    "_restore_correction_originals",
    "_receipt_sha256",
    "_sealed_receipt_sha256",
    "_stale_inputs_touching_candidate",
    "_target_candidate_sha256",
    "bound_finding_dispositions",
    "contextlib",
    "create_supplemental_inspection_rounds",
    "effective_requested_risk_tier",
    "fold_accept_blockers",
    "_latest_request_identity_event",
    "manager_skill_tools",
    "reviewer_evidence",
    "sealed_review_packet",
    "server_derived_risk_tier",
    "supplemental_rounds_completed",
    "validate_manager_correction",
)

ACCEPT_REVIEW_SEAM_NAMES: tuple[str, ...] = (
    "GitCommandTimeout",
    "LaunchRejected",
    "Path",
    "WorkerWorkspace",
    "WorkspaceError",
    "_accepted_outcome_receipt",
    "_canonical_task_status",
    "_card_is_readonly_quality_review",
    "_card_is_readonly_research",
    "_changed_path_hashes",
    "_declared_validation_commands",
    "_enforce_behavioral_gate",
    "_finished_acceptance_result",
    "_parse_card",
    "_path_manifest",
    "_readonly_research_result_evidence",
    "_run_declared_validations",
    "_run_full_snapshot_validations",
    "_utcnow",
    "_verified_accepted_quality_review_receipt",
    "_verified_quality_review_receipt",
    "_worker_workspace",
    "assert_gc_safe_workspace_shape",
    "cleanup_workspace",
    "core",
    "create_combined_validation_workspace",
    "enforce_scope",
    "evidence_levels",
    "json",
    "learning_commit",
    "quality_evidence",
    "task_engine",
    "validate_required_outputs",
)


def accept_review(
    self,
    request_id: str,
    task_id: str,
    *,
    confirm_destructive_change: bool = False,
    requested_risk_tier: str | None = None,
    risk_signals: list[str] | None = None,
    reviewer_reports: list[dict[str, Any]] | None = None,
    reviewer_request_ids: list[str] | None = None,
    confirm_high_risk: bool = False,
    manager_correction: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Coordinator/write-gated acceptance of one ``review_ready`` request.

    This is Phase 2 of the review-first lifecycle: ``_finalize_isolated_request``
    already retained the isolated worktree and recorded every check's
    evidence on the canonical card's ``terminal_review`` (changed paths +
    hashes, required outputs, validation, the worker MCP gate, and the
    exact request/task/runner/topic identity). Nothing before this method
    may ever touch the canonical repo for a review-first request.  Every
    precondition below is required; any failure leaves the canonical repo
    untouched and the task in ``review`` with the reason returned as
    ``error``.  A retry after this method already promoted and finished
    the exact same request returns ``already_accepted`` instead of
    re-promoting or re-validating anything.

    ``requested_risk_tier`` and ``risk_signals`` are manager-owned inputs, and
    both are OVERRIDES that may only tighten. ``requested_risk_tier=None`` --
    the default -- means "the tier the finalizer already measured for this
    candidate" (:func:`server_derived_risk_tier`); an explicit tier is folded
    over that with :func:`effective_requested_risk_tier`, which takes the
    higher of the two, so naming a tier can add review and can never remove it.
    Medium-and-higher profiles materialize a fresh combined-tree workspace
    and fail closed without the required read-only reviewer reports.
    High/critical profiles additionally require ``confirm_high_risk``.

    ``reviewer_request_ids=None`` -- the default -- means "every verified
    reviewer child bound to this exact (request_id, task_id)", which the server
    already knows: the manager retyped them in 135 of 159 measured calls. An
    explicit list overrides that set, and an explicit ``[]`` excludes every
    reviewer. Whichever way they are chosen, EVERY id is still verified here by
    request id against its own sealed receipt -- defaulting changes who types
    the ids, never what is proven about them.

    ``manager_correction`` -- ``None`` by default, in which case every path
    below behaves exactly as it did before NF-2026-01436 -- is a unified diff
    the verified manager applies ON TOP of the sealed candidate, naming the
    confirmed non-blocking findings it repairs:
    ``{"patch": <unified diff>, "finding_ids": ["reviewer:<lens>:<id>", ...],
    "reason": str}``. It is validated by :func:`validate_manager_correction`
    BEFORE any promotion or state change and refuses with one typed
    ``manager_correction_invalid:<reason>``; a named confirmed finding's
    ``refinement_required`` blocker is then lifted, the corrected bytes are what
    the declared validations and the mechanical gate judge, and the
    accepted-outcome receipt binds the patch digest together with the candidate
    and corrected hashes. A blocking-severity finding is never lifted, and the
    reviewer receipts stay bound to the sealed candidate they actually read.
    """
    # Every module-level name this function used while it lived in
    # ``process_launcher`` is re-bound here from that exact module object, so a
    # ``monkeypatch.setattr(process_launcher, name, ...)`` seam still reaches the
    # code below.  The list is the function's complete free-variable set and is
    # asserted against it by ``test_process_launcher_accept_review.py``; nothing
    # in the body was rewritten to get here.
    from . import process_launcher as _pl

    GitCommandTimeout = _pl.GitCommandTimeout
    LaunchRejected = _pl.LaunchRejected
    Path = _pl.Path
    WorkerWorkspace = _pl.WorkerWorkspace
    WorkspaceError = _pl.WorkspaceError
    _accepted_outcome_receipt = _pl._accepted_outcome_receipt
    _canonical_task_status = _pl._canonical_task_status
    _card_is_readonly_quality_review = _pl._card_is_readonly_quality_review
    _card_is_readonly_research = _pl._card_is_readonly_research
    _changed_path_hashes = _pl._changed_path_hashes
    _declared_validation_commands = _pl._declared_validation_commands
    _enforce_behavioral_gate = _pl._enforce_behavioral_gate
    _finished_acceptance_result = _pl._finished_acceptance_result
    _parse_card = _pl._parse_card
    _path_manifest = _pl._path_manifest
    _readonly_research_result_evidence = _pl._readonly_research_result_evidence
    _run_declared_validations = _pl._run_declared_validations
    _run_full_snapshot_validations = _pl._run_full_snapshot_validations
    _utcnow = _pl._utcnow
    _verified_accepted_quality_review_receipt = _pl._verified_accepted_quality_review_receipt
    _verified_quality_review_receipt = _pl._verified_quality_review_receipt
    _worker_workspace = _pl._worker_workspace
    assert_gc_safe_workspace_shape = _pl.assert_gc_safe_workspace_shape
    cleanup_workspace = _pl.cleanup_workspace
    core = _pl.core
    create_combined_validation_workspace = _pl.create_combined_validation_workspace
    enforce_scope = _pl.enforce_scope
    evidence_levels = _pl.evidence_levels
    json = _pl.json
    learning_commit = _pl.learning_commit
    quality_evidence = _pl.quality_evidence
    task_engine = _pl.task_engine
    validate_required_outputs = _pl.validate_required_outputs
    # A no-change research/reviewer task never promotes repository bytes.
    # A bounded pre-read selects a per-request lock for that case so it
    # does not wait behind an unrelated writable promotion. Every identity
    # and evidence check is repeated below while the selected lock is held.
    readonly_lock_path = False
    try:
        pre_events = self._request_events(request_id)
        pre_latest = pre_events[-1] if pre_events else {}
        pre_card = _parse_card(self._show_task(task_id), task_id)
        pre_evidence = (pre_card.get("terminal_review") or {}).get("evidence") or {}
        readonly_lock_path = bool(
            str(pre_latest.get("task_id") or "") == task_id
            and _canonical_task_status(pre_card) == "review"
            and (
                _card_is_readonly_quality_review(pre_card)
                or _card_is_readonly_research(pre_card)
            )
            and pre_evidence.get("changed_paths") in ([], None)
        )
    except (LaunchRejected, AttributeError, TypeError):
        readonly_lock_path = False
    acceptance_lock = (
        self._request_lock(request_id)
        if readonly_lock_path
        else self._promotion_lock()
    )
    # NF-2026-01626: claimed before either lock, so a duplicate of an accept
    # that is still validating answers ``accept_in_progress`` at once instead
    # of queueing on those locks for the length of that validation. A refused
    # claim ran nothing and wrote nothing; a granted one is released by the
    # ``with`` below on every exit, success and exception alike.
    in_flight = _accept_in_flight_marker(self, task_id, request_id)
    if in_flight.refusal is not None:
        return in_flight.refusal
    # NF-2026-01515: the writable path must also hold the per-request lock so
    # retention GC (_gc_finalized_workspace) cannot quarantine this workspace
    # mid-correction; the read-only path already holds that lock alone.
    with in_flight, acceptance_lock, (
        contextlib.nullcontext()
        if readonly_lock_path
        else self._request_lock(request_id)
    ):
        if reviewer_reports:
            return {
                "ok": False,
                "error": "unverified_reviewer_reports_forbidden",
                "request_id": request_id,
                "task_id": task_id,
            }
        events = self._request_events(request_id)
        if not events:
            return {"ok": False, "error": "request_not_found", "request_id": request_id}
        latest_observed = events[-1]
        if str(latest_observed.get("task_id") or "") != task_id:
            return {
                "ok": False,
                "error": "request_task_identity_mismatch",
                "request_id": request_id,
                "task_id": task_id,
            }
        latest = _latest_request_identity_event(events, task_id)
        if latest is None:
            return {
                "ok": False,
                "error": "request_identity_incomplete",
                "request_id": request_id,
                "task_id": task_id,
            }
        runner = str(latest.get("runner") or "")
        topic = str(latest.get("topic") or "")

        try:
            card = _parse_card(self._show_task(task_id), task_id)
        except LaunchRejected as exc:
            return {
                "ok": False,
                "error": f"task_lookup_failed:{exc}",
                "request_id": request_id,
                "task_id": task_id,
            }

        finished_result = _finished_acceptance_result(
            self.repo,
            card,
            task_id=task_id,
            request_id=request_id,
            canonical_status=_canonical_task_status,
            close_needfix=self._close_accepted_task_needfix,
        )
        if finished_result is not None:
            return finished_result
        canonical = _canonical_task_status(card)
        if canonical != "review":
            return {
                "ok": False,
                "error": f"task_not_in_review:{canonical}",
                "request_id": request_id,
                "task_id": task_id,
            }
        if card.get("runner") != runner or card.get("topic") != topic:
            return {
                "ok": False,
                "error": "task_identity_mismatch",
                "request_id": request_id,
                "task_id": task_id,
            }
        if card.get("claimed_by") != runner:
            return {
                "ok": False,
                "error": "claim_ownership_mismatch",
                "request_id": request_id,
                "task_id": task_id,
            }

        terminal_review = card.get("terminal_review") or {}
        if str(terminal_review.get("substatus") or "") != "review_ready":
            return {
                "ok": False,
                "error": (
                    "terminal_substatus_not_review_ready:"
                    + str(terminal_review.get("substatus") or "")
                ),
                "request_id": request_id,
                "task_id": task_id,
            }
        deterministic = card.get("deterministic_verification") or {}
        if deterministic:
            expected_epoch = str(card.get("claim_epoch") or 0)
            observed_epoch = str(deterministic.get("claim_epoch") or 0)
            if observed_epoch != expected_epoch:
                return {
                    "ok": False,
                    "error": (
                        "deterministic_verification_claim_epoch_mismatch:"
                        f"expected={expected_epoch}:observed={observed_epoch}"
                    ),
                    "request_id": request_id,
                    "task_id": task_id,
                }
        if deterministic.get("applicable") and not deterministic.get("pass"):
            return {
                "ok": False,
                "error": "deterministic_verification_failed",
                "request_id": request_id,
                "task_id": task_id,
            }

        evidence = terminal_review.get("evidence") or {}
        request_identity = evidence.get("request_identity") or {}
        if (
            str(request_identity.get("request_id") or "") != request_id
            or str(request_identity.get("task_id") or "") != task_id
            or str(request_identity.get("runner") or "") != runner
            or str(request_identity.get("topic") or "") != topic
        ):
            return {
                "ok": False,
                "error": "evidence_request_identity_mismatch",
                "request_id": request_id,
                "task_id": task_id,
            }

        predecessor = card.get("rework_predecessor")
        residual_identities = (
            predecessor.get("residual_identities")
            if isinstance(predecessor, dict)
            else None
        )
        if residual_identities:
            residual_contract = evidence.get("residual_contract")
            if not isinstance(residual_contract, list) or not residual_contract:
                return {
                    "ok": False,
                    "error": "residual_contract_evidence_missing",
                    "request_id": request_id,
                    "task_id": task_id,
                }
            if any(
                not isinstance(row, dict) or row.get("pass") is not True
                for row in residual_contract
            ):
                return {
                    "ok": False,
                    "error": "residual_contract_evidence_failed",
                    "request_id": request_id,
                    "task_id": task_id,
                }

        intent_snapshot = self._context_write_intent_snapshot(request_id)
        if intent_snapshot.get("ok"):
            pending_intents = int((intent_snapshot.get("counts") or {}).get("pending") or 0)
            if pending_intents:
                return {
                    "ok": False,
                    "error": f"context_write_intents_pending:{pending_intents}",
                    "request_id": request_id,
                    "task_id": task_id,
                    "pending_context_write_intents": [
                        {
                            "intent_id": row.get("intent_id"),
                            "component": row.get("component"),
                            "action": row.get("action"),
                        }
                        for row in intent_snapshot.get("intents") or []
                        if row.get("status") == "pending_manager_review"
                    ],
                }

        workspace_meta = evidence.get("workspace")
        if not isinstance(workspace_meta, dict):
            return {
                "ok": False,
                "error": "evidence_workspace_missing",
                "request_id": request_id,
                "task_id": task_id,
            }
        try:
            workspace = WorkerWorkspace.from_metadata(workspace_meta)
        except (KeyError, TypeError, ValueError) as exc:
            return {
                "ok": False,
                "error": f"evidence_workspace_invalid:{exc}",
                "request_id": request_id,
                "task_id": task_id,
            }
        if workspace.repo != self.repo or workspace.request_id != request_id:
            return {
                "ok": False,
                "error": "workspace_identity_mismatch",
                "request_id": request_id,
                "task_id": task_id,
            }
        try:
            assert_gc_safe_workspace_shape(
                request_id, workspace.path, workspace.home, repo=self.repo
            )
        except WorkspaceError as exc:
            return {
                "ok": False,
                "error": f"unsafe_workspace_shape:{exc}",
                "request_id": request_id,
                "task_id": task_id,
            }
        if workspace.path.is_symlink() or not workspace.path.is_dir():
            return {
                "ok": False,
                "error": "workspace_missing",
                "request_id": request_id,
                "task_id": task_id,
            }

        readonly_quality_review = _card_is_readonly_quality_review(card)
        readonly_research = (
            _card_is_readonly_research(card) and not readonly_quality_review
        )
        readonly_no_change = readonly_research or readonly_quality_review
        # A no-change research/quality-review task has no candidate bytes to
        # correct, so a correction aimed at one is refused before any evidence
        # is read rather than silently ignored on the zero-promotion branches.
        if manager_correction is not None and readonly_no_change:
            return {
                "ok": False,
                "error": "manager_correction_invalid:no_change_task",
                "request_id": request_id,
                "task_id": task_id,
            }
        try:
            attempt_artifact_receipt = self._verify_attempt_artifact_receipt(
                request_id,
                evidence.get("attempt_artifact_manifest"),
            )
            terminal_evidence_record = evidence_levels.validate_evidence_record(
                evidence.get("evidence_record")
            )
            minimum_evidence_level = self._minimum_acceptance_evidence_level(
                card,
                readonly_quality_review=readonly_quality_review,
                readonly_research=readonly_research,
            )
            if not evidence_levels.meets_evidence_level(
                terminal_evidence_record.evidence_level,
                minimum_evidence_level,
            ):
                raise WorkspaceError(
                    "evidence_level_below_minimum:"
                    f"observed={terminal_evidence_record}:"
                    f"required={minimum_evidence_level}"
                )
            expected_reference = self._attempt_evidence_reference(
                request_id,
                attempt_artifact_receipt,
            )
            if terminal_evidence_record.reference != expected_reference:
                raise WorkspaceError("evidence_record_reference_mismatch")
        except (
            WorkspaceError,
            evidence_levels.EvidenceValidationError,
            TypeError,
        ) as exc:
            return {
                "ok": False,
                "error": f"evidence_level_gate_failed:{exc}",
                "request_id": request_id,
                "task_id": task_id,
            }
        if readonly_lock_path and not readonly_no_change:
            return {
                "ok": False,
                "error": "review_lock_class_changed_retry",
                "request_id": request_id,
                "task_id": task_id,
            }
        stored_hashes = evidence.get("changed_path_hashes")
        if stored_hashes is None and readonly_no_change:
            stored_hashes = {}
        if not isinstance(stored_hashes, dict) or (
            not stored_hashes and not readonly_no_change
        ):
            return {
                "ok": False,
                "error": "evidence_changed_hashes_missing",
                "request_id": request_id,
                "task_id": task_id,
            }
        if readonly_no_change and stored_hashes:
            return {
                "ok": False,
                "error": (
                    "quality_review_changed_hashes_forbidden"
                    if readonly_quality_review
                    else "research_changed_hashes_forbidden"
                ),
                "request_id": request_id,
                "task_id": task_id,
            }

        if not readonly_no_change:
            # Referential evidence is independently recomputed before the
            # write gate opens and before a single candidate byte is
            # promoted.  Stored prose cannot satisfy this gate.
            from . import evidence_instruments

            evidence_audit = evidence_instruments.review_evidence_audit(
                self.repo,
                workspace.path,
                changed_paths=list(evidence.get("changed_paths") or []),
                stored_hashes=stored_hashes,
                required_outputs=list(evidence.get("required_outputs") or []),
            )
            if evidence_audit.get("blocking"):
                return {
                    "ok": False,
                    "error": "review_evidence_audit_failed:"
                    + ",".join(evidence_audit.get("blockers") or [])[:420],
                    "request_id": request_id,
                    "task_id": task_id,
                    "review_evidence_audit": evidence_audit,
                }

        # B919: fail closed before copying any output whenever a declared
        # immutable/dependency input has drifted since the claim-time
        # snapshot (B914 -- a retained-worktree validation had passed
        # against a stale dependency population). Untouched when a task
        # declares no ``immutable_inputs`` at all.
        declared_immutable_inputs = [
            str(p) for p in (evidence.get("immutable_inputs") or [])
        ]
        stale_input_rebased: list[str] = []
        if declared_immutable_inputs:
            stored_input_manifest = evidence.get("immutable_input_manifest")
            if not isinstance(stored_input_manifest, dict):
                return {
                    "ok": False,
                    "error": "stale_input:dependency_manifest_missing",
                    "request_id": request_id,
                    "task_id": task_id,
                }
            current_input_manifest = _path_manifest(self.repo, declared_immutable_inputs)
            changed_inputs = sorted(
                relative
                for relative in declared_immutable_inputs
                if current_input_manifest.get(relative) != stored_input_manifest.get(relative)
            )
            if changed_inputs:
                # NF-2026-01543: a drift in a frozen input this candidate never
                # changed is rebased in place instead of forcing a full rework
                # round. The full-snapshot revalidation below already runs the
                # card's declared validations on the current canonical tree
                # (HEAD plus its uncommitted delta) with the candidate overlaid
                # last, so that run -- not the claim-time one -- proves the
                # candidate against the drifted bytes, and the accept records
                # the inputs it rebased over. The refusal is unchanged when a
                # drifted input covers a candidate path, when the card declares
                # no validation for that run to execute, and for a no-change
                # task, whose revalidation reads the claim-time worktree.
                try:
                    revalidates_on_head = bool(_declared_validation_commands(card))
                except WorkspaceError:
                    revalidates_on_head = False
                if (
                    readonly_no_change
                    or not revalidates_on_head
                    or _stale_inputs_touching_candidate(
                        changed_inputs,
                        [*(evidence.get("changed_paths") or []), *stored_hashes],
                    )
                ):
                    return {
                        "ok": False,
                        "error": (
                            "stale_input:dependency_changed:" + ",".join(changed_inputs)
                        )[:500],
                        "request_id": request_id,
                        "task_id": task_id,
                    }
                stale_input_rebased = changed_inputs

        if not core.writes_allowed():
            return {
                "ok": False,
                "error": "write_gate_closed",
                "request_id": request_id,
                "task_id": task_id,
            }

        if readonly_quality_review:
            try:
                if enforce_scope(
                    workspace,
                    git_phase="review_acceptance",
                    git_timeout=_worker_workspace.finalization_git_timeout_seconds(),
                ):
                    raise WorkspaceError("quality_review_workspace_mutated")
                if evidence.get("changed_paths") not in ([], None):
                    raise WorkspaceError("quality_review_changed_paths_forbidden")
                required_output_records = validate_required_outputs(
                    workspace,
                    card.get("required_outputs") or [],
                    allow_empty=(),
                    allow_unchanged=(),
                )
                metadata_path = self._metadata_from_events(events)
                if metadata_path is None:
                    raise WorkspaceError("quality_review_metadata_missing")
                if (
                    metadata_path.parent.resolve() != self.process_dir.resolve()
                    or metadata_path.is_symlink()
                    or not metadata_path.is_file()
                    or metadata_path.stat().st_size > 2 * 1024 * 1024
                ):
                    raise WorkspaceError("quality_review_metadata_invalid")
                try:
                    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise WorkspaceError("quality_review_metadata_unreadable") from exc
                if (
                    str(metadata.get("request_id") or "") != request_id
                    or str(metadata.get("task_id") or "") != task_id
                    or str(metadata.get("runner") or "") != runner
                    or str(metadata.get("topic") or "") != topic
                ):
                    raise WorkspaceError("quality_review_metadata_identity_mismatch")
                try:
                    metadata_workspace = WorkerWorkspace.from_metadata(
                        dict(metadata["workspace"])
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    raise WorkspaceError("quality_review_workspace_invalid") from exc
                if metadata_workspace.as_metadata() != workspace.as_metadata():
                    raise WorkspaceError("quality_review_workspace_identity_mismatch")
                verified_receipt = _verified_quality_review_receipt(
                    metadata, workspace, request_id
                )
                stored_receipt = evidence.get("quality_review_receipt")
                if not isinstance(stored_receipt, dict):
                    raise WorkspaceError("quality_review_receipt_missing")
                if verified_receipt != stored_receipt:
                    raise WorkspaceError("quality_review_receipt_mismatch")
                report = verified_receipt.get("report") or {}
                if (
                    report.get("read_only") is not True
                    or report.get("can_mutate_repo") is not False
                ):
                    raise WorkspaceError("quality_review_receipt_not_readonly")
                validations = _run_declared_validations(
                    workspace, card, latest
                )
            except GitCommandTimeout as exc:
                return {
                    "ok": False,
                    "error": str(exc)[:500],
                    "request_id": request_id,
                    "task_id": task_id,
                }
            except WorkspaceError as exc:
                return {
                    "ok": False,
                    "error": f"revalidation_failed:{exc}",
                    "request_id": request_id,
                    "task_id": task_id,
                }

            quality_gate = {
                "schema_id": "aiworkhub.completion_quality_gate.v1",
                "applicable": False,
                "passed": None,
                "reason": "quality_review_no_repository_change",
                "changed_paths": [],
                "checks": [],
                "blocking_checks": [],
            }
            manager_identity = _accept_manager_identity(core)
            acceptance_evidence_record = self._canonical_outcome_evidence(
                request_id,
                attempt_artifact_receipt,
                level=evidence_levels.EvidenceLevel.FIXED_AND_VERIFIED,
                verified_by=(
                    str(manager_identity.get("provider") or "").strip()[:60]
                    or "manager_unverified"
                ),
                message=(
                    "Manager reverified and accepted the sealed quality-review outcome."
                ),
            )
            try:
                accepted_outcome_receipt = _accepted_outcome_receipt(
                    self.repo,
                    task_id=task_id,
                    request_id=request_id,
                    claim_epoch=int(card.get("claim_epoch") or 0),
                    base_oid=str(workspace_meta.get("base_oid") or ""),
                    promoted_paths=[],
                    changed_path_hashes=stored_hashes,
                    attempt_artifact_manifest=attempt_artifact_receipt,
                )
            except (OSError, TypeError, ValueError, WorkspaceError) as exc:
                return {
                    "ok": False,
                    "error": f"accepted_outcome_receipt_failed:{exc}",
                    "request_id": request_id,
                    "task_id": task_id,
                    "promoted_paths": [],
                }
            accept_result = task_engine.accept_review(
                self.repo,
                task_id,
                runner=runner,
                topic=topic,
                request_id=request_id,
                evidence={
                    "promoted_paths": [],
                    "validation": validations,
                    "required_outputs": required_output_records,
                    "quality_gate": quality_gate,
                    "quality_review_receipt": verified_receipt,
                    "source_evidence_record": terminal_evidence_record.to_dict(),
                    "acceptance_evidence_record": acceptance_evidence_record,
                    "attempt_artifact_manifest": attempt_artifact_receipt,
                },
                accepted_outcome_receipt=accepted_outcome_receipt,
            )
            if not accept_result.get("ok"):
                return {
                    "ok": False,
                    "error": (
                        "quality_review_finalize_failed:"
                        + str(
                            accept_result.get("stderr")
                            or accept_result.get("stdout")
                            or ""
                        )
                    )[:500],
                    "request_id": request_id,
                    "task_id": task_id,
                    "promoted_paths": [],
                }
            needfix_closure = self._close_accepted_task_needfix(task_id, request_id)
            cleanup_error = ""
            try:
                cleanup_workspace(workspace.repo, workspace.path, workspace.home)
            except WorkspaceError as exc:
                cleanup_error = str(exc)[:500]
            self._retention_event({
                "request_id": request_id,
                "task_id": task_id,
                "runner": runner,
                "topic": topic,
                "adapter_id": latest.get("adapter_id"),
                "state": "accepted",
                "accepted": True,
                "promoted_paths": [],
                "cleanup_error": cleanup_error,
                "quality_review_receipt": verified_receipt,
                "acceptance_evidence_record": acceptance_evidence_record,
                "accepted_outcome_receipt": accepted_outcome_receipt,
                "reviewer_finalization": [],
                "acceptance_lock_scope": "request",
                "needfix_closure": needfix_closure,
                "finished_at": _utcnow(),
            }, disposition="retained_in_place" if cleanup_error else "removed")
            return {
                "ok": True,
                "request_id": request_id,
                "task_id": task_id,
                "promoted_paths": [],
                "cleanup_error": cleanup_error,
                "quality_review_receipt": verified_receipt,
                "acceptance_evidence_record": acceptance_evidence_record,
                "accepted_outcome_receipt": accepted_outcome_receipt,
                "reviewer_finalization": [],
                "acceptance_lock_scope": "request",
                "needfix_closure": needfix_closure,
            }

        if readonly_research:
            try:
                if enforce_scope(
                    workspace,
                    git_phase="review_acceptance",
                    git_timeout=_worker_workspace.finalization_git_timeout_seconds(),
                ):
                    raise WorkspaceError("research_workspace_mutated")
                if evidence.get("changed_paths") not in ([], None):
                    raise WorkspaceError("research_changed_paths_forbidden")
                required_output_records = validate_required_outputs(
                    workspace,
                    card.get("required_outputs") or [],
                    allow_empty=(),
                    allow_unchanged=(),
                )
                stored_result = evidence.get("research_result")
                if not isinstance(stored_result, dict):
                    raise WorkspaceError("research_result_evidence_missing")
                stdout_raw = latest.get("stdout_path")
                if not stdout_raw:
                    raise WorkspaceError("research_result_path_missing")
                stdout_path = Path(str(stdout_raw))
                try:
                    safe_parent = stdout_path.parent.resolve()
                    expected_parent = self.process_dir.resolve()
                except OSError as exc:
                    raise WorkspaceError("research_result_path_invalid") from exc
                if (
                    safe_parent != expected_parent
                    or stdout_path.name != f"{request_id}.stdout.log"
                ):
                    raise WorkspaceError("research_result_path_identity_mismatch")
                current_result = _readonly_research_result_evidence(stdout_path)
                if not current_result.get("meaningful_output"):
                    raise WorkspaceError(
                        str(
                            current_result.get("reason")
                            or "research_result_missing"
                        )
                    )
                identity_keys = (
                    "schema_id",
                    "meaningful_output",
                    "bytes",
                    "sha256",
                    "result_event_count",
                    "result_chars",
                )
                if any(
                    current_result.get(key) != stored_result.get(key)
                    for key in identity_keys
                ):
                    raise WorkspaceError("research_result_evidence_mismatch")
                worker_mcp_gate = evidence.get("worker_mcp_gate")
                if (
                    isinstance(worker_mcp_gate, dict)
                    and worker_mcp_gate.get("gated")
                    and not worker_mcp_gate.get("satisfied", True)
                ):
                    raise WorkspaceError(
                        "validation_required_aiworkhub_mcp_call_missing:"
                        + str(worker_mcp_gate.get("reason") or "")
                    )
                validations = _run_declared_validations(
                    workspace, card, latest
                )
            except GitCommandTimeout as exc:
                return {
                    "ok": False,
                    "error": str(exc)[:500],
                    "request_id": request_id,
                    "task_id": task_id,
                }
            except WorkspaceError as exc:
                return {
                    "ok": False,
                    "error": f"revalidation_failed:{exc}",
                    "request_id": request_id,
                    "task_id": task_id,
                }

            try:
                accepted_outcome_receipt = _accepted_outcome_receipt(
                    self.repo,
                    task_id=task_id,
                    request_id=request_id,
                    claim_epoch=int(card.get("claim_epoch") or 0),
                    base_oid=str(workspace_meta.get("base_oid") or ""),
                    promoted_paths=[],
                    changed_path_hashes=stored_hashes,
                    attempt_artifact_manifest=attempt_artifact_receipt,
                )
            except (OSError, TypeError, ValueError, WorkspaceError) as exc:
                return {
                    "ok": False,
                    "error": f"accepted_outcome_receipt_failed:{exc}",
                    "request_id": request_id,
                    "task_id": task_id,
                    "promoted_paths": [],
                }

            quality_gate = {
                "schema_id": "aiworkhub.completion_quality_gate.v1",
                "applicable": False,
                "passed": None,
                "reason": "research_no_repository_change",
                "changed_paths": [],
                "checks": [],
                "blocking_checks": [],
            }
            manager_identity = _accept_manager_identity(core)
            acceptance_evidence_record = self._canonical_outcome_evidence(
                request_id,
                attempt_artifact_receipt,
                level=evidence_levels.EvidenceLevel.FIXED_AND_VERIFIED,
                verified_by=(
                    str(manager_identity.get("provider") or "").strip()[:60]
                    or "manager_unverified"
                ),
                message=(
                    "Manager reverified and accepted the sealed research outcome."
                ),
            )
            accept_result = task_engine.accept_review(
                self.repo,
                task_id,
                runner=runner,
                topic=topic,
                request_id=request_id,
                evidence={
                    "promoted_paths": [],
                    "validation": validations,
                    "required_outputs": required_output_records,
                    "quality_gate": quality_gate,
                    "research_result": current_result,
                    "source_evidence_record": terminal_evidence_record.to_dict(),
                    "acceptance_evidence_record": acceptance_evidence_record,
                    "attempt_artifact_manifest": attempt_artifact_receipt,
                },
                accepted_outcome_receipt=accepted_outcome_receipt,
            )
            if not accept_result.get("ok"):
                return {
                    "ok": False,
                    "error": (
                        "research_finalize_failed:"
                        + str(
                            accept_result.get("stderr")
                            or accept_result.get("stdout")
                            or ""
                        )
                    )[:500],
                    "request_id": request_id,
                    "task_id": task_id,
                    "promoted_paths": [],
                }
            needfix_closure = self._close_accepted_task_needfix(task_id, request_id)
            cleanup_error = ""
            try:
                cleanup_workspace(workspace.repo, workspace.path, workspace.home)
            except WorkspaceError as exc:
                cleanup_error = str(exc)[:500]
            self._retention_event({
                "request_id": request_id,
                "task_id": task_id,
                "runner": runner,
                "topic": topic,
                "adapter_id": latest.get("adapter_id"),
                "state": "accepted",
                "accepted": True,
                "promoted_paths": [],
                "cleanup_error": cleanup_error,
                "research_result": current_result,
                "acceptance_evidence_record": acceptance_evidence_record,
                "accepted_outcome_receipt": accepted_outcome_receipt,
                "reviewer_finalization": [],
                "needfix_closure": needfix_closure,
                "finished_at": _utcnow(),
            }, disposition="retained_in_place" if cleanup_error else "removed")
            return {
                "ok": True,
                "request_id": request_id,
                "task_id": task_id,
                "promoted_paths": [],
                "cleanup_error": cleanup_error,
                "research_result": current_result,
                "acceptance_evidence_record": acceptance_evidence_record,
                "accepted_outcome_receipt": accepted_outcome_receipt,
                "reviewer_finalization": [],
                "acceptance_lock_scope": "request",
                "needfix_closure": needfix_closure,
            }

        # Snapshot of the pre-correction bytes for any path a manager
        # correction overwrites below, so a non-promoting exit can restore
        # the retained worktree (NF-2026-01469). Declared here, outside the
        # try, so every except handler can read it regardless of where in
        # the try an exception was raised.
        correction_originals: dict[str, bytes] = {}
        try:
            changed = enforce_scope(
                workspace,
                git_phase="review_acceptance",
                git_timeout=_worker_workspace.finalization_git_timeout_seconds(),
            )
            required_output_records = validate_required_outputs(
                workspace,
                card.get("required_outputs") or [],
                allow_empty=tuple(card.get("allow_empty_required_outputs") or []),
                allow_unchanged=tuple(card.get("allow_unchanged_required_outputs") or []),
            )
            validated_required_paths = {
                rec["path"]
                for rec in required_output_records
                if not rec.get("unchanged_allowed")
            }
            changed = sorted(set(changed) | validated_required_paths)
            if not changed:
                raise WorkspaceError("no_effect")
            worker_mcp_gate = evidence.get("worker_mcp_gate")
            if (
                isinstance(worker_mcp_gate, dict)
                and worker_mcp_gate.get("gated")
                and not worker_mcp_gate.get("satisfied", True)
            ):
                raise WorkspaceError(
                    "validation_required_aiworkhub_mcp_call_missing:"
                    + str(worker_mcp_gate.get("reason") or "")
                )
            destructive_checks = quality_evidence.run_destructive_diff_checks(
                self.repo,
                workspace.path,
                changed_paths=changed,
            )
            destructive_rows = [check.to_dict() for check in destructive_checks]
            destructive_blockers = [
                check.check_id
                for check in destructive_checks
                if check.status == quality_evidence.STATUS_FAILED
            ]
            if destructive_blockers and not confirm_destructive_change:
                raise WorkspaceError(
                    "destructive_diff_requires_manager_confirmation:"
                    + ",".join(destructive_blockers)[:300]
                )
            effective_risk_signals = quality_evidence.derive_risk_signals(
                card,
                changed,
                destructive_checks=destructive_checks,
            )
            effective_risk_signals.extend(risk_signals or [])
            effective_risk_signals = sorted(dict.fromkeys(effective_risk_signals))
            # A candidate that empties or removes its own quality policy must
            # not thereby weaken its own acceptance. The weakening is an
            # observed signal that escalates the tier (medium+ then demands
            # combined-tree and reviewer evidence); it does not silently pass.
            policy_authority = quality_evidence.assess_quality_policy_authority(
                self.repo, workspace.path, changed_paths=changed
            )
            # NF-2026-01742: ``confirm_high_risk`` is a MANAGER confirmation,
            # never an owner approval. No owner-authenticated accept path
            # exists, so a manager call never carries explicit human approval;
            # the manager confirmation clears the approval requirement unless
            # the CANONICAL repository policy (never the candidate's own)
            # declares ``explicit_human_approval_required``.
            owner_human_approval = False
            owner_approval_required = (
                quality_evidence.repository_requires_explicit_human_approval(self.repo)
            )
            high_risk_approval = quality_evidence.high_risk_approval_satisfied(
                explicit_human_approval=owner_human_approval,
                manager_high_risk_confirmed=bool(confirm_high_risk),
                repository_requires_explicit_human_approval=owner_approval_required,
            )
            if policy_authority["weakened"] and policy_authority["escalation_signal"]:
                effective_risk_signals = sorted(
                    dict.fromkeys(
                        [
                            *effective_risk_signals,
                            str(policy_authority["escalation_signal"]),
                        ]
                    )
                )
            # The tier the FINALIZER measured, raised (never lowered) by an
            # explicit manager request. ``None`` -- the default -- means the
            # manager stops predicting and retyping a tier the server already
            # derived from this exact candidate.
            effective_requested_tier = effective_requested_risk_tier(
                card, requested_risk_tier
            )
            risk_profile = quality_evidence.resolve_risk_profile(
                effective_requested_tier,
                signals=effective_risk_signals,
            )
            # ---- the cheap fold, BEFORE anything expensive --------------
            #
            # Measured 2026-09-08: 49 of 159 accept attempts (31%) failed on a
            # parameter or timing condition decidable from persisted evidence
            # alone -- and every one of them failed AFTER a combined-tree
            # workspace had been materialized and validated twice. Nothing
            # below this point is needed to know any of them.
            #
            # It refuses; it never accepts. ``fold_quality_verdict`` still runs
            # in full further down over the same profile, so a tier planned too
            # narrowly upstream still fails closed there. This only says so
            # first, for free.
            #
            # Two of the fold's conditions -- the target substatus and the
            # pending write intents -- were already refused far above, so they
            # cannot fire HERE. They are still in the fold because
            # ``accept_preview`` runs the identical function and is where a
            # manager meets them: the point is that the two surfaces answer
            # from one implementation, not two that drift.
            reviewers = reviewer_evidence(self, task_id, request_id)
            # Bound against the SEALED receipts here; the gate below re-binds
            # against the receipts it re-verifies, so a disposition that no
            # longer matches is dropped there and the finding blocks again.
            sealed_finding_dispositions = bound_finding_dispositions(
                self,
                task_id,
                request_id,
                candidate_sha256=_target_candidate_sha256(card),
                receipt_sha256_by_reviewer=_sealed_receipt_sha256(reviewers),
            )
            # NF-2026-01436: the manager correction is decided HERE -- before
            # the combined tree, before a reviewer receipt is re-verified and
            # before any byte is written anywhere. An invalid correction refuses
            # with one typed error and leaves the accept tree, the canonical
            # repo and the card exactly as they were.
            correction_record: dict[str, Any] | None = None
            corrected_texts: dict[str, str] = {}
            accepted_path_hashes: dict[str, str | None] = stored_hashes
            if manager_correction is not None:
                correction_record, corrected_texts, correction_reason = (
                    validate_manager_correction(
                        self,
                        manager_correction,
                        card=card,
                        workspace=workspace,
                        changed=changed,
                        reviewers=reviewers,
                        reviewer_request_ids=reviewer_request_ids,
                        finding_dispositions=sealed_finding_dispositions,
                        core=core,
                        path_cls=Path,
                    )
                )
                if correction_record is None:
                    return {
                        "ok": False,
                        "error": f"manager_correction_invalid:{correction_reason}"[:500],
                        "request_id": request_id,
                        "task_id": task_id,
                        "promoted_paths": [],
                    }
            accept_fold = fold_accept_blockers(
                reviewers=reviewers,
                reviewer_request_ids=reviewer_request_ids,
                risk_profile=risk_profile,
                terminal_substatus=str(terminal_review.get("substatus") or ""),
                confirm_high_risk=confirm_high_risk,
                explicit_human_approval=owner_human_approval,
                repository_requires_explicit_human_approval=owner_approval_required,
                **(
                    {"finding_dispositions": sealed_finding_dispositions}
                    if sealed_finding_dispositions
                    else {}
                ),
                # Only a VALIDATED correction reaches the fold, so the lift it
                # performs can never rest on a claim nothing checked.
                **(
                    {"manager_correction": correction_record}
                    if correction_record is not None
                    else {}
                ),
            )
            # NF-2026-01158: the blind-lens prediction stays in the fold so
            # ``accept_preview`` reports it, but refusing on it HERE would skip
            # the quality-gate path below that launches the one bounded
            # supplemental round -- ``fold_quality_verdict`` refuses it there
            # with the same classifier, so preview and accept still agree.
            early_blockers = [
                blocker
                for blocker in accept_fold["blockers"]
                if blocker["kind"] != "reviewer_could_not_inspect"
            ]
            if early_blockers:
                return {
                    "ok": False,
                    "error": str(early_blockers[0]["error"]),
                    "request_id": request_id,
                    "task_id": task_id,
                    "accept_blockers": early_blockers,
                    "reviewer_request_ids": accept_fold["reviewer_request_ids"],
                    "reviewer_request_id_source": accept_fold[
                        "reviewer_request_id_source"
                    ],
                    "required_reviewer_lenses": accept_fold["required_reviewer_lenses"],
                    "reviewer_lenses": accept_fold["per_lens"],
                    "effective_risk_tier": str(risk_profile["effective_tier"]),
                    "requested_risk_tier": effective_requested_tier,
                    "combined_tree_materialized": False,
                }
            # Nothing cheap refuses any more, so the manager's already-validated
            # bytes go onto the accept tree HERE -- before the combined tree is
            # materialized, before the declared validations run and before the
            # mechanical gate reads a byte. Every one of them then judges the
            # CORRECTED candidate, and the bytes promoted below are the bytes
            # that passed. A refusal on any earlier line left the tree alone.
            if correction_record is not None:
                for relative in correction_record["paths"]:
                    target_path = workspace.path / relative
                    correction_originals[relative] = target_path.read_bytes()
                    target_path.write_bytes(
                        corrected_texts[relative].encode("utf-8")
                    )
                accepted_path_hashes = {
                    **stored_hashes,
                    **correction_record["corrected_path_hashes"],
                }
            combined_tree: dict[str, Any] | None = None
            combined_tree_checks: list[dict[str, Any]] = []
            inherited_policy = (
                policy_authority.get("candidate_policy_source") == "canonical_unchanged"
                and (
                    policy_authority.get("canonical_declared_checks")
                    or not policy_authority.get("canonical_config_readable")
                )
            )
            # Sparse omission is not policy deletion. Enforce the inherited
            # policy against the complete candidate union, even at low risk;
            # never run full-tree commands against the sparse worker tree.
            if risk_profile.get("combined_tree_required") or inherited_policy:
                union_workspace, combined_tree = create_combined_validation_workspace(
                    workspace,
                    card,
                    changed,
                )
                try:
                    union_validations = _run_declared_validations(
                        union_workspace, card, latest
                    )
                    union_quality = quality_evidence.run_completion_quality_gate(
                        union_workspace.path,
                        changed_paths=changed,
                        requested_risk_tier=effective_requested_tier,
                        risk_signals=effective_risk_signals,
                        combined_tree_scope=True,
                        policy_root=self.repo if inherited_policy else None,
                    )
                    if not union_quality.get("passed"):
                        union_blockers = union_quality.get("blocking_checks") or []
                        failed_checks = [row for row in union_quality.get("checks") or []
                                         if row.get("check_id") in union_blockers][:3]
                        raise WorkspaceError(
                            "combined_tree_quality_failed:"
                            + json.dumps({"blockers": union_blockers,
                                          "failed_checks": failed_checks})[:4000]
                        )
                    combined_tree_checks = [
                        {
                            "check_id": "combined-tree-materialized",
                            "kind": "requirements",
                            "status": quality_evidence.STATUS_PASSED,
                            "provenance": "current canonical tree plus exact candidate delta",
                        },
                        *[
                            {
                                **dict(row),
                                "check_id": "combined-tree:" + str(row.get("check_id") or "check"),
                            }
                            for row in union_quality.get("checks") or []
                        ],
                        *[
                            {
                                "check_id": f"combined-tree:validation:{index}",
                                "kind": "test",
                                "status": quality_evidence.STATUS_PASSED,
                                "provenance": str(row.get("command") or "validation")[:2000],
                            }
                            for index, row in enumerate(union_validations)
                        ],
                    ]
                    combined_tree["validation"] = union_validations
                    combined_tree["quality_gate"] = union_quality
                finally:
                    cleanup_workspace(
                        union_workspace.repo,
                        union_workspace.path,
                        union_workspace.home,
                    )
            # The manager named these in 24 of 159 measured calls and retyped
            # what the server already knew in the other 135. ``None`` now means
            # "the verified reviewer children bound to this exact (request_id,
            # task_id)"; an explicit list still overrides, and an explicit ``[]``
            # still excludes every reviewer. Either way each id is verified
            # below against its own sealed receipt -- this decides who TYPES the
            # ids, never what is proven about them.
            reviewer_ids = (
                list(accept_fold["reviewer_request_ids"])
                if reviewer_request_ids is None
                else list(reviewer_request_ids)
            )
            reviewer_ids_source = accept_fold["reviewer_request_id_source"]
            if len(reviewer_ids) > quality_evidence.MAX_REVIEW_REPORTS:
                raise WorkspaceError("quality_reviewer_request_overflow")
            if len(set(reviewer_ids)) != len(reviewer_ids):
                raise WorkspaceError("quality_reviewer_request_duplicate")
            verified_reviewer_reports: list[dict[str, Any]] = []
            verified_reviewer_tasks: list[
                tuple[str, _WorkerWorkspaceT | None, bool]
            ] = []
            # The sealed packet each verified reviewer actually read, keyed by
            # the lens its own receipt names. An already-accepted reviewer has
            # no live workspace and therefore contributes none -- which is a
            # fact about what can be proven now, not a defect: a lens with no
            # packet here simply gets no supplemental round.
            sealed_review_packets: dict[str, dict[str, Any]] = {}
            # The digest of each reviewer receipt verified in THIS accept, the
            # only value a stored finding disposition may be bound to.
            verified_receipt_sha256: dict[str, str] = {}
            for reviewer_request_id in reviewer_ids:
                reviewer_events = self._request_events(reviewer_request_id)
                if not reviewer_events:
                    raise WorkspaceError(
                        f"quality_reviewer_request_not_found:{reviewer_request_id}"
                    )
                reviewer_latest = reviewer_events[-1]
                reviewer_state = str(reviewer_latest.get("state") or "")
                if reviewer_state == "accepted":
                    reviewer_task_id = str(reviewer_latest.get("task_id") or "")
                    try:
                        reviewer_card = _parse_card(
                            self._show_task(reviewer_task_id), reviewer_task_id
                        )
                        receipt = _verified_accepted_quality_review_receipt(
                            reviewer_latest,
                            reviewer_card,
                            reviewer_request_id,
                            request_id,
                            task_id,
                        )
                    except (LaunchRejected, WorkspaceError) as exc:
                        raise WorkspaceError(
                            f"quality_reviewer_accepted_invalid:{reviewer_request_id}:{exc}"
                        ) from exc
                    verified_reviewer_reports.append(dict(receipt["report"]))
                    verified_receipt_sha256[reviewer_request_id] = _receipt_sha256(
                        receipt
                    )
                    verified_reviewer_tasks.append(
                        (reviewer_task_id, None, True)
                    )
                    continue
                if reviewer_state != "review_ready":
                    raise WorkspaceError(
                        f"quality_reviewer_not_review_ready:{reviewer_request_id}"
                    )
                reviewer_metadata_path = self._metadata_from_events(reviewer_events)
                if reviewer_metadata_path is None:
                    raise WorkspaceError(
                        f"quality_reviewer_metadata_missing:{reviewer_request_id}"
                    )
                if (
                    reviewer_metadata_path.parent.resolve()
                    != self.process_dir.resolve()
                    or reviewer_metadata_path.is_symlink()
                    or not reviewer_metadata_path.is_file()
                    or reviewer_metadata_path.stat().st_size > 2 * 1024 * 1024
                ):
                    raise WorkspaceError(
                        f"quality_reviewer_metadata_invalid:{reviewer_request_id}"
                    )
                try:
                    reviewer_metadata = json.loads(
                        reviewer_metadata_path.read_text(encoding="utf-8")
                    )
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise WorkspaceError(
                        f"quality_reviewer_metadata_unreadable:{reviewer_request_id}"
                    ) from exc
                reviewer_binding = reviewer_metadata.get("quality_review") or {}
                if (
                    str(reviewer_binding.get("target_request_id") or "") != request_id
                    or str(reviewer_binding.get("target_task_id") or "") != task_id
                ):
                    raise WorkspaceError(
                        f"quality_reviewer_target_mismatch:{reviewer_request_id}"
                    )
                reviewer_workspace = WorkerWorkspace.from_metadata(
                    dict(reviewer_metadata["workspace"])
                )
                if enforce_scope(
                    reviewer_workspace,
                    git_phase="review_acceptance",
                    git_timeout=_worker_workspace.finalization_git_timeout_seconds(),
                ):
                    raise WorkspaceError(
                        f"quality_reviewer_workspace_mutated:{reviewer_request_id}"
                    )
                receipt = _verified_quality_review_receipt(
                    reviewer_metadata,
                    reviewer_workspace,
                    reviewer_request_id,
                )
                verified_reviewer_reports.append(dict(receipt["report"]))
                verified_receipt_sha256[reviewer_request_id] = _receipt_sha256(receipt)
                verified_reviewer_tasks.append(
                    (
                        str(reviewer_metadata.get("task_id") or ""),
                        reviewer_workspace,
                        False,
                    )
                )
                # Recovered from the reviewer's own home and bound to the
                # receipt just verified above, so the gate decides the
                # supplemental question from the packet ITSELF rather than from
                # the report's account of it. Unprovable packets return None
                # and are simply absent.
                reviewer_packet = sealed_review_packet(
                    reviewer_metadata, reviewer_workspace, receipt
                )
                reviewer_report_lens = str(
                    (receipt.get("report") or {}).get("lens") or ""
                )
                if reviewer_packet is not None and reviewer_report_lens:
                    sealed_review_packets[reviewer_report_lens] = reviewer_packet
            # NF-2026-01170: only dispositions bound to THIS candidate digest and
            # to the reviewer receipts just verified above reach the fold.
            accepted_finding_dispositions = bound_finding_dispositions(
                self,
                task_id,
                request_id,
                candidate_sha256=_target_candidate_sha256(card),
                receipt_sha256_by_reviewer=verified_receipt_sha256,
            )
            quality_gate = quality_evidence.run_completion_quality_gate(
                workspace.path,
                changed_paths=changed,
                requested_risk_tier=effective_requested_tier,
                risk_signals=effective_risk_signals,
                reviewer_reports=verified_reviewer_reports,
                combined_tree_checks=combined_tree_checks,
                worker_provider=str(latest.get("adapter_id") or runner),
                human_approval=high_risk_approval,
                reachability_inputs=self._candidate_reachability_inputs(
                    workspace, changed
                ),
                # The two inputs the supplemental decision needs, and nothing
                # the gate can use to PASS anything: the packet each lens was
                # sealed against, and how many re-reads that lens already had.
                review_packets=sealed_review_packets,
                supplemental_rounds=supplemental_rounds_completed(reviewers),
                **(
                    {"finding_dispositions": accepted_finding_dispositions}
                    if accepted_finding_dispositions
                    else {}
                ),
                # Only a VALIDATED correction reaches the authoritative gate,
                # so the lift it performs can never rest on a caller's claim
                # nothing checked (NF-2026-01469).
                **(
                    {"corrected_finding_ids": correction_record["finding_ids"]}
                    if correction_record is not None
                    else {}
                ),
            )
            quality_gate["combined_tree"] = combined_tree
            quality_gate["quality_policy_authority"] = policy_authority
            # WHO accepted, WHO chose the reviewer ids, and WHAT the tier was
            # asked to be -- on the record, in the accept event's own payload.
            #
            # ``confirm_high_risk`` stays an EXPLICIT manager input and is NOT
            # implied by the verified call: a high/critical acceptance remains a
            # decision someone made in words, and
            # ``explicit_human_approval_implied_by_caller`` states in the record
            # itself that it was not inferred. Should that ever change, this
            # field is where the audit has to say so.
            #
            # ``requested_risk_tier_source`` distinguishes a tier the manager
            # typed from one the finalizer measured, so a later reader can tell
            # an override from a default.
            #
            # The identity is same-uid local runtime state (provider, session
            # and window), never a credential, and it is best-effort: an
            # unverifiable route is recorded as unverified rather than
            # fabricated, and never blocks a promotion that has already passed
            # every gate above.
            manager_identity = _accept_manager_identity(core)
            quality_gate["accept_manager_identity"] = {
                "verified": bool(manager_identity),
                "provider": str(manager_identity.get("provider") or "")[:60],
                "session_id": str(
                    manager_identity.get("session_id")
                    or manager_identity.get("thread_id")
                    or ""
                )[:120],
                "window_id": str(manager_identity.get("window_id") or "")[:120],
            }
            quality_gate["accept_parameter_provenance"] = {
                "reviewer_request_ids": list(reviewer_ids),
                "reviewer_request_id_source": (
                    reviewer_ids_source
                    if reviewer_request_ids is None
                    else "manager_named"
                ),
                "requested_risk_tier": effective_requested_tier,
                "requested_risk_tier_source": (
                    "manager_override" if requested_risk_tier else "server_derived"
                ),
                "manager_requested_risk_tier": str(requested_risk_tier or ""),
                "server_derived_risk_tier": server_derived_risk_tier(card),
                # True only from an owner-authenticated path; a manager call
                # never has one (NF-2026-01742).
                "explicit_human_approval": owner_human_approval,
                "explicit_human_approval_implied_by_caller": False,
                "explicit_human_approval_required_by_repository_policy": (
                    owner_approval_required
                ),
                "manager_high_risk_confirmation": {
                    "confirmed": bool(confirm_high_risk),
                    "actor": dict(quality_gate["accept_manager_identity"]),
                    # The reviewer receipts this accept re-verified and binds.
                    "evidence_ids": [
                        *(str(value) for value in reviewer_ids),
                        *(
                            "quality_review_receipt_sha256:"
                            + verified_receipt_sha256[str(value)]
                            for value in reviewer_ids
                            if verified_receipt_sha256.get(str(value))
                        ),
                    ],
                    "satisfied_explicit_human_approval_requirement": bool(
                        risk_profile.get("explicit_human_approval_required")
                        and high_risk_approval
                        and not owner_human_approval
                    ),
                },
                "destructive_change_confirmed_by_manager": bool(
                    confirm_destructive_change
                ),
            }
            if not quality_gate.get("passed"):
                quality_blockers = quality_gate.get("blocking_checks") or []
                if not isinstance(quality_blockers, list):
                    raise WorkspaceError("quality_gate_failed:invalid_blocking_checks")
                reason = quality_gate.get("config_error") or ",".join(str(v) for v in quality_blockers)
                # The acceptance fails either way, and with the identical error
                # string it always had. What changes is what has already
                # happened by the time the manager reads it: where the gate's
                # own plan authorized a bounded same-lens re-read of the same
                # sealed packet, that round is launched HERE, not left as an
                # instruction for someone to carry out. The implementation
                # worker is untouched -- its bytes are not what failed.
                supplemental = create_supplemental_inspection_rounds(
                    self,
                    request_id=request_id,
                    task_id=task_id,
                    reviewers=reviewers,
                    quality_gate=quality_gate,
                )
                if supplemental:
                    error_message = (
                        "revalidation_failed:quality_gate_failed:"
                        + str(reason)[:400]
                    )
                    if correction_originals:
                        failed_restores = _restore_correction_originals(
                            workspace, correction_originals
                        )
                        if failed_restores:
                            error_message += (
                                ":correction_restore_failed:"
                                + ",".join(failed_restores)[:200]
                            )
                    return {
                        "ok": False,
                        "error": error_message,
                        "request_id": request_id,
                        "task_id": task_id,
                        "supplemental_inspection": supplemental,
                    }
                raise WorkspaceError("quality_gate_failed:" + str(reason)[:400])
            quality_gate["destructive_diff_checks"] = destructive_rows
            quality_gate["destructive_diff_blockers"] = destructive_blockers
            quality_gate["destructive_change_confirmed"] = bool(
                confirm_destructive_change and destructive_blockers
            )
            validation_reuse = _finalizer_validation_reuse(
                terminal_review, correction_record=correction_record
            )
            validations, full_validation_snapshot = (
                _run_full_snapshot_validations(
                    workspace, card, latest, changed,
                    **({"reuse": validation_reuse} if validation_reuse else {}),
                )
            )
            quality_gate["full_validation_snapshot"] = full_validation_snapshot
            _enforce_behavioral_gate(card, validations, quality_gate)
            # ``accepted_path_hashes`` IS ``stored_hashes`` unless a manager
            # correction was validated and applied above, in which case the
            # corrected paths carry their corrected digests -- so this still
            # proves the tree about to be promoted is the tree just validated.
            current_hashes = _changed_path_hashes(workspace, changed)
            if set(current_hashes) != set(accepted_path_hashes) or any(
                current_hashes[relative] != accepted_path_hashes.get(relative)
                for relative in current_hashes
            ):
                raise WorkspaceError("stored_hash_mismatch")
        except GitCommandTimeout as exc:
            error_message = str(exc)[:500]
            if correction_originals:
                failed_restores = _restore_correction_originals(workspace, correction_originals)
                if failed_restores:
                    error_message += (
                        ":correction_restore_failed:" + ",".join(failed_restores)[:200]
                    )
            return {
                "ok": False,
                "error": error_message,
                "request_id": request_id,
                "task_id": task_id,
            }
        except WorkspaceError as exc:
            error_message = f"revalidation_failed:{exc}"
            if correction_originals:
                failed_restores = _restore_correction_originals(workspace, correction_originals)
                if failed_restores:
                    error_message += (
                        ":correction_restore_failed:" + ",".join(failed_restores)[:200]
                    )
            return {
                "ok": False,
                "error": error_message,
                "request_id": request_id,
                "task_id": task_id,
            }
        except BaseException:
            # Any other exception after the correction write (an OSError from
            # a later read/write on a second corrected path, or a raise from
            # a validation/gate helper) still leaves CORRECTED bytes in the
            # retained worktree unless they are restored before it propagates
            # (NF-2026-01469).
            if correction_originals:
                _restore_correction_originals(workspace, correction_originals)
            raise
        # Promote exactly the merged bytes validated in the combined tree (NF-2026-01381 part A).
        combined_tree = full_validation_snapshot.get("combined_tree") or {}
        try:
            # NF-2026-01469: promotion can raise before writing any byte (a
            # backwards-version refusal) or mid-write; either way CORRECTED
            # bytes must not be left behind in the retained worktree.
            promoted = self._promote_accepted_candidate(
                workspace, changed, merged_hashes=combined_tree.get("merged_paths")
            )
        except BaseException:
            if correction_originals:
                _restore_correction_originals(workspace, correction_originals)
            raise

        # NF-2026-01518: the paths promotion three-way merged, exactly as the
        # combined tree validated them; canonical now holds their MERGED bytes.
        merged_paths = combined_tree.get("merged_paths") or {}
        try:
            accepted_outcome_receipt = _accepted_outcome_receipt(
                self.repo,
                task_id=task_id,
                request_id=request_id,
                claim_epoch=int(card.get("claim_epoch") or 0),
                base_oid=str(workspace_meta.get("base_oid") or ""),
                promoted_paths=promoted,
                changed_path_hashes=accepted_path_hashes,
                attempt_artifact_manifest=attempt_artifact_receipt,
                # Named only when a correction landed, so an accept without one
                # calls exactly the receipt builder it always called.
                **(
                    {"manager_correction": correction_record}
                    if correction_record is not None
                    else {}
                ),
                # Likewise named only when a merge landed: each merge consumed
                # its path's accepted (sealed or corrected) bytes.
                **(
                    {
                        "promotion_merge": {
                            "paths": sorted(merged_paths),
                            "candidate_path_hashes": {
                                relative: accepted_path_hashes.get(relative)
                                for relative in merged_paths
                            },
                            "merged_path_hashes": dict(merged_paths),
                        }
                    }
                    if merged_paths
                    else {}
                ),
            )
        except (OSError, TypeError, ValueError, WorkspaceError) as exc:
            # Canonical keeps the promoted bytes (no rollback), but the retained
            # worktree must not keep CORRECTED bytes, or the next review trips
            # review_workspace_hash_mismatch (NF-2026-01518).
            error_message = f"accepted_outcome_receipt_failed:{exc}"
            if correction_originals:
                failed_restores = _restore_correction_originals(workspace, correction_originals)
                if failed_restores:
                    error_message += (
                        ":correction_restore_failed:" + ",".join(failed_restores)[:200]
                    )
            return {
                "ok": False,
                "error": error_message,
                "request_id": request_id,
                "task_id": task_id,
                "promoted_paths": promoted,
            }

        acceptance_evidence_record = self._canonical_outcome_evidence(
            request_id,
            attempt_artifact_receipt,
            level=evidence_levels.EvidenceLevel.FIXED_AND_VERIFIED,
            verified_by=(
                str(manager_identity.get("provider") or "").strip()[:60]
                or "manager_unverified"
            ),
            message=(
                "Manager revalidated, promoted, and accepted the exact sealed candidate."
            ),
        )

        accept_result = task_engine.accept_review(
            self.repo,
            task_id,
            runner=runner,
            topic=topic,
            request_id=request_id,
            evidence={
                "promoted_paths": promoted,
                "validation": validations,
                "required_outputs": required_output_records,
                "quality_gate": quality_gate,
                "source_evidence_record": terminal_evidence_record.to_dict(),
                "acceptance_evidence_record": acceptance_evidence_record,
                "attempt_artifact_manifest": attempt_artifact_receipt,
                # Named only when a drift in a frozen input the candidate never
                # changed was rebased in place, so an accept without one records
                # exactly what it always recorded (NF-2026-01543).
                **(
                    {"stale_input_rebased": stale_input_rebased}
                    if stale_input_rebased
                    else {}
                ),
            },
            accepted_outcome_receipt=accepted_outcome_receipt,
        )
        if not accept_result.get("ok"):
            return {
                "ok": False,
                "error": (
                    "promotion_finalize_failed:"
                    + str(accept_result.get("stderr") or accept_result.get("stdout") or "")
                )[:500],
                "request_id": request_id,
                "task_id": task_id,
                "promoted_paths": promoted,
            }

        needfix_closure = self._close_accepted_task_needfix(task_id, request_id)

        verified_reviewer_ids = [
            tid for tid, _ws, _accepted in verified_reviewer_tasks
        ]
        disposition_result = task_engine.disposition_reviewer_children(
            self.repo,
            task_id,
            verified_reviewer_task_ids=verified_reviewer_ids,
            parent_request_id=request_id,
            disposition="accepted",
        )
        try:
            disposition_payload = json.loads(
                str(disposition_result.get("stdout") or "{}")
            )
        except (TypeError, json.JSONDecodeError):
            disposition_payload = {}
        finalized_set = set(disposition_payload.get("finalized") or [])
        reviewer_finalization: list[dict[str, Any]] = []
        for reviewer_task_id, reviewer_workspace, already_accepted in verified_reviewer_tasks:
            row = {
                "task_id": reviewer_task_id,
                "finished": already_accepted or reviewer_task_id in finalized_set,
                "cleanup_error": "",
            }
            if reviewer_workspace is None:
                reviewer_finalization.append(row)
                continue
            try:
                cleanup_workspace(
                    reviewer_workspace.repo,
                    reviewer_workspace.path,
                    reviewer_workspace.home,
                )
            except WorkspaceError as exc:
                row["cleanup_error"] = str(exc)[:300]
            reviewer_finalization.append(row)

        # One reply definition, so the cleanup-failed path cannot drift.
        learning_owed = learning_commit.commit_owed(
            task_id=task_id, request_id=request_id, outcome="accepted",
            changed_paths=list(promoted), evidence_reference=str(
                (acceptance_evidence_record or {}).get("reference") or ""),
        )
        # One evidence row per skill this card actually received, with the
        # actor DERIVED from the card's own runner. Measured 2026-09-08: across
        # 3,383 recorded decisions there were 0 evidence rows, because the only
        # path was a manager typing skill_add_evidence with a free-text actor --
        # and the one "active" record had 5 rows from a single canonical actor
        # under two spellings. Deriving the actor is what makes two independent
        # actors mean two. Never raises; activation stays a manager decision.
        manager_skill_tools.record_decision_evidence(
            self.repo, task_id=task_id, request_id=request_id, outcome="accepted"
        )
        # Imported here, not at module scope: the seam guard treats a function
        # -body import as a binding, and neither module is a ``process_launcher``
        # seam a test could patch.
        from . import learning_commit_store as _learning_store
        from . import needfix_store as _needfix_store

        # Findings that SURVIVED ingest but were not acted on. The accept
        # landed, so nothing here blocked it -- and the reviewer card that
        # carries them is finalized in this same call and archived later (1,796
        # so far), which is where 589 accepted-shape findings went. Drafted
        # from the reports this accept actually verified; the manager files one
        # with needfix_add, or lets it land through capture_proposal as
        # captured/unverified. The DESCRIPTION stays the manager's.
        surviving_findings = _needfix_store.draft_from_review_evidence(
            {
                "task_id": task_id,
                "terminal_review": {
                    "evidence": {
                        "request_identity": {
                            "request_id": request_id, "task_id": task_id,
                        },
                        "changed_paths": list(promoted),
                        "attempt_artifact_manifest": attempt_artifact_receipt,
                        "quality_gate": {
                            "quality_verdict": {
                                "reviewer_reports": verified_reviewer_reports,
                            },
                        },
                    },
                },
            },
            request_id=request_id,
        )
        # Measured 2026-09-08: 3,383 accept/reject decisions produced 130
        # session documents, and the injected bundle's session section read
        # evidence_count 0 in 743 of 743 requests -- every mandated session
        # query was empty by construction. One event document per decision,
        # through the same context_writes path the learning-commit projection
        # uses. No model call; learning_commit still owns the lesson text.
        session_decision_event = _learning_store.record_decision_event(
            self.repo,
            task_id=task_id,
            request_id=request_id,
            decision="accepted",
            changed_path_hashes=accepted_path_hashes,
            changed_paths=list(promoted),
            failure_category="",
        )
        accepted_reply = {
            "ok": True, "request_id": request_id, "task_id": task_id,
            "promoted_paths": promoted,
            "reviewer_finalization": reviewer_finalization,
            "acceptance_evidence_record": acceptance_evidence_record,
            "accepted_outcome_receipt": accepted_outcome_receipt,
            "needfix_closure": needfix_closure,
            "learning_commit_owed": learning_owed,
            "needfix_candidates": surviving_findings,
            "session_decision_event": session_decision_event,
            # The same provenance the accept EVENT carries, in the reply, so a
            # caller can see which reviewer ids were used and who supplied them
            # without re-reading the card it just finished.
            "accept_parameter_provenance": quality_gate.get(
                "accept_parameter_provenance", {}
            ),
            "accept_manager_identity": quality_gate.get("accept_manager_identity", {}),
            # The frozen inputs this accept rebased over, as the event records
            # them (NF-2026-01543); absent when nothing drifted.
            **(
                {"stale_input_rebased": stale_input_rebased}
                if stale_input_rebased
                else {}
            ),
        }
        try:
            cleanup_workspace(workspace.repo, workspace.path, workspace.home)
        except WorkspaceError as exc:
            self._retention_event({
                "request_id": request_id,
                "task_id": task_id,
                "runner": runner,
                "topic": topic,
                "adapter_id": latest.get("adapter_id"),
                "state": "accepted",
                "accepted": True,
                "promoted_paths": promoted,
                "cleanup_error": str(exc)[:500],
                "reviewer_finalization": reviewer_finalization,
                "acceptance_evidence_record": acceptance_evidence_record,
                "accepted_outcome_receipt": accepted_outcome_receipt,
                "needfix_closure": needfix_closure,
                "learning_commit_owed": learning_owed,
                "finished_at": _utcnow(),
            }, disposition="retained_in_place")
            return {**accepted_reply, "cleanup_error": str(exc)[:500]}

        self._retention_event({
            "request_id": request_id,
            "task_id": task_id,
            "runner": runner,
            "topic": topic,
            "adapter_id": latest.get("adapter_id"),
            "state": "accepted",
            "accepted": True,
            "promoted_paths": promoted,
            "learning_commit_owed": learning_owed,
            "reviewer_finalization": reviewer_finalization,
            "acceptance_evidence_record": acceptance_evidence_record,
            "accepted_outcome_receipt": accepted_outcome_receipt,
            "needfix_closure": needfix_closure,
            "finished_at": _utcnow(),
        }, disposition="removed")
        return accepted_reply


def dispose_review_finding(
    self: Any,
    *,
    task_id: str,
    request_id: str,
    reviewer_request_id: str,
    finding_id: str,
    disposition: str,
    counter_evidence: str,
    reason: str,
) -> dict[str, Any]:
    """Record one manager disposition of one non-blocking reviewer finding.

    Same manager gate as :meth:`resolve_review_route_hold`. The binding --
    the reviewer receipt and the candidate digest -- is derived here from
    the server-sealed reviewer evidence and the target's terminal evidence,
    never from the caller. It never accepts the target (NF-2026-01170).
    """
    # Re-bound from the ``process_launcher`` module object, as in
    # :func:`accept_review`, so monkeypatches of that module still apply.
    from . import process_launcher as _pl

    Path = _pl.Path
    _parse_card = _pl._parse_card
    core = _pl.core
    review_orchestrator = _pl.review_orchestrator

    route = core.manager_bootstrap()
    identity = route.get("manager_route") if isinstance(route, dict) else None
    if not isinstance(identity, dict) or route.get("role") != "manager":  # route is a dict
        return {"ok": False, "error": "verified_manager_identity_required"}
    if not route.get("repo") or Path(str(route["repo"])).resolve() != self.repo:
        return {"ok": False, "error": "manager_repository_mismatch"}
    if not core.writes_allowed():
        return {"ok": False, "error": "write_gate_closed"}
    provider = str(identity.get("provider") or route.get("provider") or "").strip()
    session_id = str(identity.get("thread_id") or identity.get("session_id") or "").strip()
    if not provider or not session_id:
        return {"ok": False, "error": "manager_session_identity_missing"}
    review_db = review_orchestrator.canonical_review_db(self)
    if review_db is None:
        return {"ok": False, "error": "review_storage_unavailable"}
    try:
        card = _parse_card(self._show_task(task_id), task_id)
    except Exception as exc:  # noqa: BLE001 -- an unreadable target binds nothing
        return {"ok": False, "error": f"task_lookup_failed:{exc}"[:300]}
    # An accepted target's outcome is sealed in its accepted_outcome_receipt;
    # a disposition recorded afterwards could only contradict it
    # (NF-2026-01682). Same finished predicate as the task lifecycle.
    if isinstance(card, dict) and core._lifecycle_state(card) == "finished":
        return {
            "ok": False,
            "error": "review_finding_disposition_frozen_after_accept",
            "detail": {
                "task_status": str(card.get("status") or ""),
                "worker_status": str(card.get("worker_status") or ""),
            },
        }
    bound = next(
        (
            row
            for row in reviewer_evidence(self, task_id, request_id)
            if row.get("usable")
            and str(row.get("request_id") or "") == str(reviewer_request_id)
        ),
        None,
    )
    target_identity = review_orchestrator.target_identity_from_card(card)
    # Terminal evidence that resolves for another request belongs to that
    # request: nothing may be recorded against it (NF-2026-01180).
    request_mismatch = bool(target_identity) and target_identity.get(
        "target_request_id"
    ) != str(request_id)
    result = review_orchestrator.ReviewOrchestrator(
        self, db_path=review_db
    ).dispose_review_finding(
        task_id=task_id,
        request_id=request_id,
        reviewer_request_id=reviewer_request_id,
        finding_id=finding_id,
        disposition=disposition,
        counter_evidence=counter_evidence,
        reason=reason,
        actor=f"{provider}:{session_id}",
        reviewer_receipt=(bound or {}).get("receipt"),
        candidate_sha256=(
            "" if request_mismatch else str(target_identity.get("candidate_sha256") or "")
        ),
    )
    if result.get("error") != "review_finding_disposition_candidate_unavailable":
        return result
    # Names and ids only: never a hash, a path or evidence content.
    named = review_orchestrator._target_identity_values(card) or {}
    return {
        **result,
        "detail": {
            "unresolved": (
                ["request_mismatch"]
                if request_mismatch
                else review_orchestrator.target_identity_unresolved(card)
            ),
            "task_status": str(card.get("status") or ""),
            "task_substatus": str(
                card.get("substatus") or card.get("terminal_substatus") or ""
            ),
            "terminal_request_id": str(named.get("target_request_id") or ""),
            "requested_request_id": str(request_id),
        },
    }


def review_finding_dispositions(
    self: Any, task_id: str, request_id: str
) -> list[dict[str, Any]]:
    """READ-ONLY: stored finding dispositions for one exact target request."""
    from . import process_launcher as _pl

    review_orchestrator = _pl.review_orchestrator

    review_db = review_orchestrator.canonical_review_db(self)
    if review_db is None:
        return []
    return review_orchestrator.ReviewOrchestrator(
        self, db_path=review_db
    ).review_finding_dispositions(task_id=task_id, request_id=request_id)
