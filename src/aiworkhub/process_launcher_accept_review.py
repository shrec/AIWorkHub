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

from typing import TYPE_CHECKING, Any, Mapping

from . import manager_skill_tools
from . import quality_evidence

if TYPE_CHECKING:  # names used only in annotations, which are never evaluated
    from .worker_workspace import WorkerWorkspace as _WorkerWorkspaceT

__all__ = [
    "ACCEPT_BLOCKER_KINDS",
    "ACCEPT_PREVIEW_SCHEMA_ID",
    "accept_preview",
    "accept_review",
    "bound_reviewer_request_ids",
    "bound_reviewer_rows",
    "bound_reviewer_task_ids",
    "effective_requested_risk_tier",
    "fold_accept_blockers",
    "reviewer_evidence",
    "server_derived_risk_tier",
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
)


def _blocker(kind: str, detail: str = "", **extra: Any) -> dict[str, Any]:
    """One named blocker, with the exact error string ``accept_review`` returns."""
    return {
        "kind": str(kind),
        "detail": str(detail)[:300],
        "error": (f"{kind}:{detail}"[:400] if detail else str(kind)),
        **extra,
    }


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
        conn = task_store._connect(db_path)
    except Exception:  # noqa: BLE001 -- enumeration failure never accepts anything
        return []
    try:
        rows = conn.execute(
            "SELECT task_id, card_json FROM tasks "
            "WHERE topic='quality_review' AND status NOT IN ('archived')"
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

    Total by construction: a ledger read failure leaves every reviewer with an
    empty request id and ``usable`` False, which blocks acceptance rather than
    granting it.
    """
    rows = bound_reviewer_rows(self.repo, parent_task_id, parent_request_id)
    if not rows:
        return []
    try:
        latest = self._latest_by_request()
    except Exception:  # noqa: BLE001 -- a ledger read failure names no reviewer
        latest = {}
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
        resolved.append(
            {
                **row,
                "request_id": str((chosen or {}).get("request_id") or ""),
                "state": str((chosen or {}).get("state") or ""),
                "usable": bool(usable) and chosen is not None,
                "attempt_count": len(candidates),
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


def _refinement_blockers(reviewers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Findings on ALREADY-VERIFIED reports that will refuse this acceptance.

    Only the receipts a reviewer already sealed are read -- nothing is
    re-verified here and nothing unverified is trusted. ``fold_quality_verdict``
    remains the authority; this names in advance the subset of its verdict a
    manager can act on before paying for a combined tree.
    """
    blockers: list[dict[str, Any]] = []
    for reviewer in reviewers:
        receipt = reviewer.get("receipt")
        report = receipt.get("report") if isinstance(receipt, Mapping) else None
        if not isinstance(report, Mapping):
            continue
        lens = str(report.get("lens") or reviewer.get("lens") or "")
        findings = report.get("findings")
        for finding in findings if isinstance(findings, list) else []:
            if not isinstance(finding, Mapping):
                continue
            if finding.get("disposition") != "defect":
                continue
            finding_id = f"reviewer:{lens}:{str(finding.get('id') or '')}"[:300]
            severity = str(finding.get("severity") or "")
            if severity in quality_evidence.BLOCKING_SEVERITIES:
                blockers.append(
                    _blocker(
                        "refinement_required", finding_id,
                        lens=lens, severity=severity, blocking_severity=True,
                    )
                )
            elif lens in {
                quality_evidence.LENS_CORRECTNESS, quality_evidence.LENS_SECURITY
            }:
                blockers.append(
                    _blocker(
                        "refinement_required", finding_id,
                        lens=lens, severity=severity, blocking_severity=False,
                    )
                )
    return blockers


def fold_accept_blockers(
    *,
    reviewers: list[dict[str, Any]],
    reviewer_request_ids: list[str] | None,
    risk_profile: Mapping[str, Any],
    terminal_substatus: str,
    pending_context_write_intents: int = 0,
    confirm_high_risk: bool = False,
    destructive_blockers: list[str] | None = None,
    confirm_destructive_change: bool = False,
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
    known = {row["request_id"]: row for row in reviewers if row["request_id"]}
    unresolved: list[str] = []
    if selected is None:
        chosen = [row for row in reviewers if row["usable"]]
        source = "server_bound_reviewer_children"
    else:
        chosen = [known[request_id] for request_id in selected if request_id in known]
        unresolved = [request_id for request_id in selected if request_id not in known]
        source = "manager_named"
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
    running_lenses = {row["lens"] for row in reviewers if not row["usable"]}
    if lens_census_complete:
        for lens in required:
            if lens in usable_lenses:
                continue
            blockers.append(
                _blocker(
                    "required_reviewer_missing", lens,
                    lens=lens,
                    # The difference between "launch one" and "wait for the one
                    # already running" -- the manager's next action, named.
                    reviewer_running=lens in running_lenses,
                )
            )
    blockers.extend(_refinement_blockers([row for row in chosen if row["usable"]]))
    if risk_profile.get("explicit_human_approval_required") and not confirm_high_risk:
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
    }


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
    fold = fold_accept_blockers(
        reviewers=reviewer_evidence(self, task_id, request_id),
        reviewer_request_ids=overrides.get("reviewer_request_ids"),
        risk_profile=risk_profile,
        terminal_substatus=str(terminal_review.get("substatus") or ""),
        pending_context_write_intents=intents,
        confirm_high_risk=bool(overrides.get("confirm_high_risk")),
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
    "effective_requested_risk_tier",
    "fold_accept_blockers",
    "_latest_request_identity_event",
    "manager_skill_tools",
    "reviewer_evidence",
    "server_derived_risk_tier",
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
    with acceptance_lock:
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
                return {
                    "ok": False,
                    "error": (
                        "stale_input:dependency_changed:" + ",".join(changed_inputs)
                    )[:500],
                    "request_id": request_id,
                    "task_id": task_id,
                }

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
            acceptance_evidence_record = self._canonical_outcome_evidence(
                request_id,
                attempt_artifact_receipt,
                level=evidence_levels.EvidenceLevel.FIXED_AND_VERIFIED,
                verified_by=core.CODEX_RUNNER,
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
            acceptance_evidence_record = self._canonical_outcome_evidence(
                request_id,
                attempt_artifact_receipt,
                level=evidence_levels.EvidenceLevel.FIXED_AND_VERIFIED,
                verified_by=core.CODEX_RUNNER,
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
            accept_fold = fold_accept_blockers(
                reviewers=reviewers,
                reviewer_request_ids=reviewer_request_ids,
                risk_profile=risk_profile,
                terminal_substatus=str(terminal_review.get("substatus") or ""),
                confirm_high_risk=confirm_high_risk,
            )
            if accept_fold["blockers"]:
                return {
                    "ok": False,
                    "error": str(accept_fold["blockers"][0]["error"]),
                    "request_id": request_id,
                    "task_id": task_id,
                    "accept_blockers": accept_fold["blockers"],
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
                verified_reviewer_tasks.append(
                    (
                        str(reviewer_metadata.get("task_id") or ""),
                        reviewer_workspace,
                        False,
                    )
                )
            quality_gate = quality_evidence.run_completion_quality_gate(
                workspace.path,
                changed_paths=changed,
                requested_risk_tier=effective_requested_tier,
                risk_signals=effective_risk_signals,
                reviewer_reports=verified_reviewer_reports,
                combined_tree_checks=combined_tree_checks,
                worker_provider=str(latest.get("adapter_id") or runner),
                human_approval=confirm_high_risk,
                reachability_inputs=self._candidate_reachability_inputs(
                    workspace, changed
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
            try:
                manager_identity = (
                    core._claude_manager_identity() or core._codex_manager_identity()
                ) or {}
            except Exception:  # noqa: BLE001 -- describing the caller never fails an accept
                manager_identity = {}
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
                "explicit_human_approval": bool(confirm_high_risk),
                "explicit_human_approval_implied_by_caller": False,
                "destructive_change_confirmed_by_manager": bool(
                    confirm_destructive_change
                ),
            }
            if not quality_gate.get("passed"):
                quality_blockers = quality_gate.get("blocking_checks") or []
                if not isinstance(quality_blockers, list):
                    raise WorkspaceError("quality_gate_failed:invalid_blocking_checks")
                reason = quality_gate.get("config_error") or ",".join(str(v) for v in quality_blockers)
                raise WorkspaceError("quality_gate_failed:" + str(reason)[:400])
            quality_gate["destructive_diff_checks"] = destructive_rows
            quality_gate["destructive_diff_blockers"] = destructive_blockers
            quality_gate["destructive_change_confirmed"] = bool(
                confirm_destructive_change and destructive_blockers
            )
            validations, full_validation_snapshot = (
                _run_full_snapshot_validations(workspace, card, latest, changed)
            )
            quality_gate["full_validation_snapshot"] = full_validation_snapshot
            _enforce_behavioral_gate(card, validations, quality_gate)
            current_hashes = _changed_path_hashes(workspace, changed)
            if set(current_hashes) != set(stored_hashes) or any(
                current_hashes[relative] != stored_hashes.get(relative)
                for relative in current_hashes
            ):
                raise WorkspaceError("stored_hash_mismatch")
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

        promoted = self._promote_accepted_candidate(workspace, changed)

        try:
            accepted_outcome_receipt = _accepted_outcome_receipt(
                self.repo,
                task_id=task_id,
                request_id=request_id,
                claim_epoch=int(card.get("claim_epoch") or 0),
                base_oid=str(workspace_meta.get("base_oid") or ""),
                promoted_paths=promoted,
                changed_path_hashes=stored_hashes,
                attempt_artifact_manifest=attempt_artifact_receipt,
            )
        except (OSError, TypeError, ValueError, WorkspaceError) as exc:
            return {
                "ok": False,
                "error": f"accepted_outcome_receipt_failed:{exc}",
                "request_id": request_id,
                "task_id": task_id,
                "promoted_paths": promoted,
            }

        acceptance_evidence_record = self._canonical_outcome_evidence(
            request_id,
            attempt_artifact_receipt,
            level=evidence_levels.EvidenceLevel.FIXED_AND_VERIFIED,
            verified_by=core.CODEX_RUNNER,
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
            changed_path_hashes=stored_hashes,
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
