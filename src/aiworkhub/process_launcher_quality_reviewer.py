"""Quality-reviewer launch and prior-findings cluster, extracted from ``ProcessManager``.

``ProcessManager._quality_review_prior_findings``,
``ProcessManager._prepared_quality_review``,
``ProcessManager._ensure_quality_reviewer_card_bound``,
``ProcessManager.launch_quality_reviewer`` and
``ProcessManager.cancel_disposed_reviewer_processes`` are thin delegations to
the functions below (NF-2026-02379: ``process_launcher.py`` sat 9 lines under
its module size ratchet).  The move is byte-for-byte apart from the four-space
dedent a method-to-function move forces; ``self`` is still the
:class:`ProcessManager` instance, so every ``self.`` collaborator and class
constant resolves exactly as before.  Module level collaborators are re-bound
from the ``process_launcher`` module object on entry rather than imported here,
because the test suite monkeypatches that module and a name captured at import
time would leave those patches pointing at code these functions no longer call.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "CANCEL_DISPOSED_REVIEWER_PROCESSES_SEAM_NAMES",
    "ENSURE_QUALITY_REVIEWER_CARD_BOUND_SEAM_NAMES",
    "LAUNCH_QUALITY_REVIEWER_SEAM_NAMES",
    "PREPARED_QUALITY_REVIEW_SEAM_NAMES",
    "QUALITY_REVIEW_PRIOR_FINDINGS_SEAM_NAMES",
    "cancel_disposed_reviewer_processes",
    "ensure_quality_reviewer_card_bound",
    "launch_quality_reviewer",
    "prepared_quality_review",
    "quality_review_prior_findings",
]

# Each function's complete runtime free-variable set on ``process_launcher``.
# ``Any`` and an annotation-only ``Mapping`` are never evaluated, so they are
# resolved from this module's own imports and are deliberately NOT re-bound.
QUALITY_REVIEW_PRIOR_FINDINGS_SEAM_NAMES: tuple[str, ...] = (
    "Mapping",
    "quality_reviewer",
    "re",
)

PREPARED_QUALITY_REVIEW_SEAM_NAMES: tuple[str, ...] = (
    "_QualityReviewPrepFlight",
    "threading",
    "time",
)

ENSURE_QUALITY_REVIEWER_CARD_BOUND_SEAM_NAMES: tuple[str, ...] = (
    "LaunchRejected",
    "_committed_claim_card",
    "_parse_card",
    "_refusal",
    "core",
    "json",
    "quality_review",
    "quality_reviewer",
    "runtime_adapters",
    "task_engine",
)

LAUNCH_QUALITY_REVIEWER_SEAM_NAMES: tuple[str, ...] = (
    "LAUNCH_IMPLEMENTED",
    "LaunchRejected",
    "Mapping",
    "_launch_isolated_mod",
    "_legacy_timeout_fields",
    "_parse_card",
    "quality_evidence",
    "quality_review",
    "threading",
)

CANCEL_DISPOSED_REVIEWER_PROCESSES_SEAM_NAMES: tuple[str, ...] = (
    "TERMINAL_PROCESS_STATES",
    "_launch_isolated_mod",
    "_oserror_within",
    "cleanup_deferral",
    "task_store",
)


def quality_review_prior_findings(
    self,
    *,
    target_task_id: str,
    target_request_id: str,
    current_hashes: Mapping[str, str | None],
    source_evidence: Mapping[str, Mapping[str, Any]],
    predecessor_request_id: str,
) -> dict[str, Any] | None:
    """Carry earlier lens findings forward, with a MECHANICAL line status.

    Measured 2026-09-08: 916 of 1,141 reviewer launches target a successor
    candidate, 34.6% of their changed paths are byte-identical to the
    predecessor, and 45 of 73 accepted targets produced zero findings across
    every lens run they ever paid for.  Every one of those rounds re-derived
    judgments the previous round already made and this repository already
    paid for, because the packet said nothing about them.

    The status attached to each finding is decided by comparing the sha256
    of the cited path in THIS candidate against the sha256 of the same path
    in the candidate that finding was written against -- the exact bytes,
    recovered from that request's own ``terminal_review`` event.  Identical
    bytes mean the cited lines still say exactly what the earlier reviewer
    read, so the line numbers carry over unchanged and the finding is the
    strongest available pointer to where to look first.  Different bytes
    mean the line numbers are not transferable and the status says so.

    Nothing here concludes that a finding was FIXED.  ``lines_changed``
    says the bytes moved, not that the defect went away; ``lines_unchanged``
    says they did not move, not that the finding was right.  Both are
    sha256 comparisons, and the judgment stays with the reviewer.

    Returns ``None`` when no earlier receipt for this task can be read --
    the packet then simply carries no prior-review section, exactly as
    before.
    """
    # Every module-level name this function used while it lived in
    # ``process_launcher`` is re-bound here from that exact module object, so a
    # ``monkeypatch.setattr(process_launcher, name, ...)`` seam still reaches the
    # code below.  The list is ``QUALITY_REVIEW_PRIOR_FINDINGS_SEAM_NAMES``.
    from . import process_launcher as _pl

    Mapping = _pl.Mapping
    quality_reviewer = _pl.quality_reviewer
    re = _pl.re

    reports, hashes_by_request = self._prior_reviewer_receipts(
        target_task_id, target_request_id
    )
    if not reports:
        return None

    changed_paths = set(current_hashes)

    def hunk_overlap(path: str, start: int | None, end: int | None) -> bool | None:
        """Does the cited range fall inside a hunk THIS candidate changed?"""
        if start is None:
            return None
        row = source_evidence.get(path)
        segments = row.get("segments") if isinstance(row, Mapping) else None
        if not isinstance(segments, list):
            return None
        last = end if end is not None and end >= start else start
        for segment in segments:
            if not isinstance(segment, Mapping):
                continue
            low = segment.get("changed_start_line")
            high = segment.get("changed_end_line")
            if not isinstance(low, int) or not isinstance(high, int):
                continue
            if start <= high and last >= low:
                return True
        return False

    lenses: dict[str, dict[str, list[dict[str, Any]]]] = {}
    omitted = 0
    for report in reports:
        lens = report["lens"]
        section = lenses.setdefault(lens, {"reports": [], "findings": []})
        if len(section["reports"]) >= quality_reviewer.MAX_PRIOR_REPORTS_PER_LENS:
            omitted += 1
            continue
        prior_hashes = hashes_by_request.get(report["target_request_id"])
        reviewer_request_id = report["reviewer_request_id"]
        if not reviewer_request_id:
            omitted += 1
            continue
        rows: list[dict[str, Any]] = []
        for finding in report["findings"]:
            raw_path = finding.get("path")
            path = str(raw_path) if isinstance(raw_path, str) and raw_path else None
            if path is not None and path not in changed_paths:
                # The packet binds every path-bearing row to this
                # candidate's changed set. A citation outside it is carried
                # without the path rather than dropped, so the judgment
                # still reaches the reviewer and nothing is silently lost.
                path = None
            unchanged = (
                path is not None
                and isinstance(prior_hashes, Mapping)
                and path in prior_hashes
                and prior_hashes[path] is not None
                and prior_hashes[path] == current_hashes.get(path)
            )

            # Line numbers are carried ONLY when the bytes are identical.
            # On changed bytes they are not merely stale, they are
            # misleading -- a number that points at a line the earlier
            # reviewer never read -- so they are withheld outright.
            cited: list[int | None] = []
            for field in ("line_start", "line_end"):
                value = finding.get(field)
                cited.append(
                    value
                    if unchanged
                    and isinstance(value, int)
                    and not isinstance(value, bool)
                    and value >= 1
                    else None
                )
            line_start, line_end = cited
            rows.append(
                {
                    "reviewer_request_id": reviewer_request_id,
                    "finding_id": str(finding.get("id") or ""),
                    "severity": str(finding.get("severity") or "low"),
                    "disposition": str(finding.get("disposition") or "observation"),
                    "actionable": finding.get("actionable") is True,
                    "summary": str(finding.get("summary") or ""),
                    "path": path,
                    "line_start": line_start,
                    "line_end": line_end,
                    "status": "lines_unchanged" if unchanged else "lines_changed",
                    "line_mapping": "identity" if unchanged else "unavailable",
                    "path_in_candidate": path is not None,
                    "overlaps_current_hunk": (
                        hunk_overlap(path, line_start, line_end)
                        if unchanged and path is not None
                        else None
                    ),
                }
            )
        section["reports"].append(
            {
                "reviewer_request_id": reviewer_request_id,
                "reviewer_task_id": report["reviewer_task_id"],
                "reviewer_provider": report["reviewer_provider"],
                "target_request_id": report["target_request_id"],
                "packet_sha256": (
                    report["packet_sha256"]
                    if re.fullmatch(r"[0-9a-f]{64}", report["packet_sha256"])
                    else None
                ),
                "finding_count": len(rows),
            }
        )
        section["findings"].extend(rows)

    for lens, section in lenses.items():
        # A finding on bytes that did not move is the strongest pointer the
        # packet can carry, so it is kept first when the cap bites; an
        # actionable defect outranks an observation after that.
        section["findings"].sort(
            key=lambda row: (
                row["status"] != "lines_unchanged",
                not row["actionable"],
                {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(
                    row["severity"], 4
                ),
                row["finding_id"],
            )
        )
        cap = quality_reviewer.MAX_PRIOR_FINDINGS_PER_LENS
        if len(section["findings"]) > cap:
            omitted += len(section["findings"]) - cap
            section["findings"] = section["findings"][:cap]
        # ``finding_count`` stays the report's TRUE count and is never
        # rewritten to what survived the cap: ``clean`` is derived from it,
        # and a truncated report that reported "clean" would be a lie that
        # tells a reviewer to look away.
    if not any(section["reports"] for section in lenses.values()):
        return None
    return {
        "predecessor_request_id": predecessor_request_id,
        "lenses": lenses,
        "omitted": omitted,
    }

def prepared_quality_review(
    self,
    target_request_id: str,
    target_task_id: str,
    progress: Any | None = None,
) -> dict[str, Any]:
    """Prepare the packet once per exact target, single-flight across lenses.

    Concurrent correctness/security/code_quality reviewers for one target
    previously each observed the same cache miss and rebuilt the heavy
    packet. A per-target single-flight now elects exactly one owner that
    runs ``_build_quality_review_packet``; every other caller waits on a
    bounded condition and reuses the owner's result. The owner's success
    *and* failure propagate truthfully, so a waiter never masks a real
    preparation error with an independent rebuild.

    Each caller's wait is bounded. A caller timeout is truthful but never
    retires a still-running builder: that builder remains authoritative,
    publishes its eventual result, and releases its flight and capacity.
    """
    # Every module-level name this function used while it lived in
    # ``process_launcher`` is re-bound here from that exact module object, so a
    # ``monkeypatch.setattr(process_launcher, name, ...)`` seam still reaches the
    # code below.  The list is ``PREPARED_QUALITY_REVIEW_SEAM_NAMES``.
    from . import process_launcher as _pl

    _QualityReviewPrepFlight = _pl._QualityReviewPrepFlight
    threading = _pl.threading
    time = _pl.time

    key = (target_request_id, target_task_id)
    capacity_deadline = time.monotonic() + self._QUALITY_REVIEW_PREP_WAIT_SECONDS
    with self._QUALITY_REVIEW_PREP_CAPACITY:
        while True:
            cache = self.__dict__.setdefault("_quality_review_prepared", {})
            prepared = cache.get(key)
            if prepared is not None:
                return {"ok": True, "prepared": prepared}
            flights = self.__dict__.setdefault("_quality_review_flights", {})
            flight = flights.get(key)
            if flight is not None:
                owner = False
                break
            if (
                self._QUALITY_REVIEW_PREP_ACTIVE_BUILDERS
                < self._QUALITY_REVIEW_PREP_MAX
            ):
                flight = _QualityReviewPrepFlight()
                flights[key] = flight
                type(self)._QUALITY_REVIEW_PREP_ACTIVE_BUILDERS += 1
                owner = True
                break
            remaining = capacity_deadline - time.monotonic()
            if remaining <= 0:
                return {
                    "ok": False,
                    "error": "quality_review_preparation_timeout",
                }
            self._QUALITY_REVIEW_PREP_CAPACITY.wait(timeout=remaining)

    if not owner:
        with flight.condition:
            deadline = time.monotonic() + self._QUALITY_REVIEW_PREP_WAIT_SECONDS
            while not flight.done:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                flight.condition.wait(timeout=remaining)
            result = flight.result if flight.done else None
        if result is None:
            return {"ok": False, "error": "quality_review_preparation_timeout"}
        return result

    def _run_owner_build() -> None:
        try:
            result = self._build_quality_review_packet(
                target_request_id, target_task_id, progress=progress
            )
        except Exception as exc:  # noqa: BLE001 -- propagate owner failure truthfully
            result = {
                "ok": False,
                "error": f"quality_review_target_invalid:{exc}",
            }
        with self._QUALITY_REVIEW_PREP_CAPACITY:
            if result.get("ok"):
                cache = self.__dict__.setdefault("_quality_review_prepared", {})
                cache[key] = result["prepared"]
                while len(cache) > self._QUALITY_REVIEW_PREP_MAX:
                    cache.pop(next(iter(cache)))
            with flight.condition:
                flight.result = result
                flight.done = True
                flight.condition.notify_all()
            flights = self.__dict__.setdefault("_quality_review_flights", {})
            if flights.get(key) is flight:
                flights.pop(key)
                type(self)._QUALITY_REVIEW_PREP_ACTIVE_BUILDERS -= 1
                self._QUALITY_REVIEW_PREP_CAPACITY.notify_all()

    builder = threading.Thread(
        target=_run_owner_build,
        name=f"aiworkhub-reviewer-prep-{target_request_id[:8]}",
        daemon=True,
    )
    try:
        builder.start()
    except Exception as exc:  # noqa: BLE001 -- thread launch can fail at runtime
        result = {
            "ok": False,
            "error": f"quality_review_target_invalid:{exc}",
        }
        with self._QUALITY_REVIEW_PREP_CAPACITY:
            with flight.condition:
                flight.result = result
                flight.done = True
                flight.condition.notify_all()
            flights = self.__dict__.setdefault("_quality_review_flights", {})
            if flights.get(key) is flight:
                flights.pop(key)
                type(self)._QUALITY_REVIEW_PREP_ACTIVE_BUILDERS -= 1
                self._QUALITY_REVIEW_PREP_CAPACITY.notify_all()
        return result
    with flight.condition:
        deadline = time.monotonic() + self._QUALITY_REVIEW_PREP_OWNER_SECONDS
        while not flight.done:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            flight.condition.wait(timeout=remaining)
        result = flight.result if flight.done else None
    if result is None:
        return {"ok": False, "error": "quality_review_preparation_timeout"}
    return result

def ensure_quality_reviewer_card_bound(
    self,
    *,
    request_id: str,
    target_request_id: str,
    target_task_id: str,
    reviewer_task_id: str,

    runner: str,
    adapter_id: str,
    lens: str,
    target_card: dict[str, Any] | None,
    terminalize_on_failure: bool,
) -> dict[str, Any]:
    """Create, claim and bind the exact reviewer card before acknowledgement."""
    # Every module-level name this function used while it lived in
    # ``process_launcher`` is re-bound here from that exact module object, so a
    # ``monkeypatch.setattr(process_launcher, name, ...)`` seam still reaches the
    # code below.  The list is ``ENSURE_QUALITY_REVIEWER_CARD_BOUND_SEAM_NAMES``.
    from . import process_launcher as _pl

    LaunchRejected = _pl.LaunchRejected
    _committed_claim_card = _pl._committed_claim_card
    _parse_card = _pl._parse_card
    _refusal = _pl._refusal
    core = _pl.core
    json = _pl.json
    quality_review = _pl.quality_review
    quality_reviewer = _pl.quality_reviewer
    runtime_adapters = _pl.runtime_adapters
    task_engine = _pl.task_engine

    worker_adapter_id = str((target_card or {}).get("adapter_id") or "")
    independence = quality_review.resolve_independence_rung(
        worker_provider=runtime_adapters.provider_for_adapter(
            worker_adapter_id
        ),
        reviewer_provider=runtime_adapters.provider_for_adapter(adapter_id),
        worker_model=worker_adapter_id,
        reviewer_model=str(adapter_id or ""),
    )
    created = core.create_task(
        task_id=reviewer_task_id,
        title=quality_reviewer.reviewer_card_title(lens=lens, target_task_id=target_task_id),
        runner=runner,
        topic="quality_review",
        objective=(
            "Review the exact anti-anchored candidate packet and submit "
            f"{lens} findings through the bound reviewer MCP tool."
        ),
        acceptance=[
            "Exactly one authenticated quality_review_submit receipt",
            "No repository mutation",
            quality_review.independence_acceptance_line(independence),
        ],
        allowed_writes=[],
        forbidden=[
            "repository_write",
            "worker_rationale_as_evidence",
            "model_supplied_provider_identity",
        ],
        required_outputs=[],
        validation=[],
        priority="high",
        callback_required=True,
        task_type="research",
        read_only=True,
        review_target_task_id=target_task_id,  # NF-2026-01095: target's route
    )
    create_ok = created.get("ok") is True
    create_structured = _refusal.create_receipt_structured(created)
    create_detail = _refusal.reply_detail(created)
    # NF-2026-01993: every non-ok reply names its request, card and adapter.
    ident = _refusal.refusal_identity(
        request_id=request_id, task_id=reviewer_task_id,
        target_request_id=target_request_id,
        target_task_id=target_task_id, adapter_id=adapter_id,
    )

    def _fail(reason: str) -> dict[str, Any]:
        if terminalize_on_failure:
            self._terminalize_reviewer_attempt(
                request_id, reviewer_task_id, runner, adapter_id, reason=reason
            )
        return _refusal.refusal(ident, reason)

    if not create_ok and not create_structured:  # NF-2026-02234: relaunch a requeued card
        failed = quality_reviewer.existing_reviewer_card_failure(
            created, create_detail, _fail, lambda t: _parse_card(self._show_task(t), t),
            (LaunchRejected,), reviewer_task_id, target_task_id, lens=lens, runner=runner)
        if failed is not None:
            core.release_review_wake(target_task_id, failed["error"])  # NF-2026-01095
            return failed

    def _verify_durable(*, require_launch_request: bool) -> dict[str, Any] | None:
        try:
            card = _parse_card(
                self._show_task(reviewer_task_id), reviewer_task_id
            )
        except LaunchRejected as exc:
            return _fail(f"quality_review_card_unreadable:{exc}"[:500])
        mismatch = _refusal.card_identity_mismatch(
            card, runner=runner, request_id=request_id,
            require_launch_request=require_launch_request,
        )
        return _fail(mismatch) if mismatch else None

    durable_error = _verify_durable(require_launch_request=False)
    if durable_error is not None:
        return durable_error
    claim = task_engine.claim_start_exact(
        self.repo,
        reviewer_task_id,
        runner,
        "quality_review",
        request_id=request_id,
    )
    if not claim.get("ok"):
        reason = _refusal.claim_failure_reason(claim)
        contention = task_engine.trusted_claim_contention(
            claim, task_id=reviewer_task_id, request_id=request_id
        )
        if contention is None:
            return _fail(reason)
        # NF-2026-00804: contention defers under the same request identity.
        try:
            retained = self._retain_ambiguous_reviewer_claim_for_reconciliation(
                request_id, reviewer_task_id, runner,
                reason="quality_review_claim_write_contention",
                recovery_state=task_engine.CLAIM_CONTENTION_RECOVERY_STATE,
            )
        except Exception:  # noqa: BLE001 -- an unretained deferral fails closed
            retained = False
        if not retained:
            return _fail(reason)
        return _refusal.retained_refusal(
            ident, reason, claim_recovery_bound=True,
            task_queue_contention=contention,
        )
    try:
        claimed_card = _committed_claim_card(
            claim,
            request_id=request_id,
            task_id=reviewer_task_id,
            runner=runner,
            topic="quality_review",
        )
    except LaunchRejected as receipt_exc:
        try:
            canonical = _parse_card(
                self._show_task(reviewer_task_id), reviewer_task_id
            )
            claimed_card = _committed_claim_card(
                {
                    "ok": True,
                    "returncode": 0,
                    "stdout": json.dumps(canonical),
                },
                request_id=request_id,
                task_id=reviewer_task_id,
                runner=runner,
                topic="quality_review",
            )
        except LaunchRejected as recovery_exc:
            recovery_retained = False
            try:
                recovery_retained = (
                    self._retain_ambiguous_reviewer_claim_for_reconciliation(
                        request_id, reviewer_task_id, runner
                    )
                )
            except Exception:  # noqa: BLE001 -- return stable retry evidence
                recovery_retained = False
            return _refusal.retained_refusal(
                ident, f"quality_review_claim_failed:{receipt_exc}"[:500],
                claim_recovery_bound=recovery_retained,
                claim_recovery_error=str(recovery_exc)[:500],
            )
    claim_epoch = claimed_card.get("claim_epoch")
    binding_persisted = False
    bind_reason = "quality_review_claim_binding_failed:binding_not_persisted"
    if type(claim_epoch) is int and claim_epoch >= 1:
        try:
            binding_persisted = self._bind_reviewer_claim_epoch(
                request_id, claim_epoch
            )
        except Exception as exc:  # noqa: BLE001 -- claim must never be stranded
            binding_persisted = False
            bind_reason = _refusal.binding_failure_reason(exc)
    if not binding_persisted:
        # Terminalize the reservation only after the canonical task release
        # succeeds.  If that CAS/store operation fails, preserving the
        # starting reservation keeps it eligible for the periodic reaper.
        released = task_engine.mark_launch_failed(
            self.repo,
            reviewer_task_id,
            runner,
            reason=bind_reason,
            request_id=request_id,
        )
        if released.get("ok") is not True:
            recovery_bound = False
            if type(claim_epoch) is int and claim_epoch >= 1:
                try:
                    recovery_bound = self._retain_reviewer_claim_for_reconciliation(
                        request_id, reviewer_task_id, claim_epoch
                    )
                except Exception:  # noqa: BLE001 -- report retryable evidence
                    recovery_bound = False
            return _refusal.retained_refusal(
                ident, bind_reason, claim_recovery_bound=recovery_bound,
                release_error=_refusal.reply_detail(released),
            )
        return _fail(bind_reason)
    durable_error = _verify_durable(require_launch_request=True)
    if durable_error is not None:
        return durable_error
    return {
        "ok": True,
        "request_id": request_id,
        "task_id": reviewer_task_id,
        "target_request_id": target_request_id,
        "launch_request_id": request_id,
    }

def launch_quality_reviewer(
    self,
    *,
    target_request_id: str,
    target_task_id: str,
    reviewer_task_id: str,
    runner: str,
    adapter_id: str,
    lens: str,
    model: str | None = None,
    timeout_seconds: int = 1800,
    refuse_live_lens: bool = False,
    target_claim_epoch: str = "",
    hedge_incumbent_request_id: str = "",
) -> dict[str, Any]:
    """Create and launch one independent packet-bound reviewer task.

    Acknowledgement is returned only after the exact reviewer card,
    launch_request_id binding and reservation are durably reconcilable.
    Expensive preparation and provider start still run under one background
    owner so a live uncapped provider is never cancelled by caller timeout.
    Retried calls reconcile the same attempt instead of launching a duplicate.
    """
    # Every module-level name this function used while it lived in
    # ``process_launcher`` is re-bound here from that exact module object, so a
    # ``monkeypatch.setattr(process_launcher, name, ...)`` seam still reaches the
    # code below.  The list is ``LAUNCH_QUALITY_REVIEWER_SEAM_NAMES``.
    from . import process_launcher as _pl

    LAUNCH_IMPLEMENTED = _pl.LAUNCH_IMPLEMENTED
    LaunchRejected = _pl.LaunchRejected
    Mapping = _pl.Mapping
    _launch_isolated_mod = _pl._launch_isolated_mod
    _legacy_timeout_fields = _pl._legacy_timeout_fields
    _parse_card = _pl._parse_card
    quality_evidence = _pl.quality_evidence
    quality_review = _pl.quality_review
    threading = _pl.threading

    if lens not in quality_evidence.JUDGMENT_LENSES:
        return {"ok": False, "error": "quality_review_lens_invalid"}
    try:
        _target_card: dict[str, Any] | None = _parse_card(
            self._show_task(target_task_id), target_task_id
        )
    except LaunchRejected:
        _target_card = None
    _target_verdict = quality_review.assess_reviewer_launch_target(
        target_card=_target_card
    )
    # NF-2026-01845: only the explicit manager launch (the tool passes
    # ``refuse_live_lens=True``; the orchestrator's automatic launch never
    # does) may review a validation_failed candidate, so a correction can
    # be judged. Automatic launches keep requiring ``review_ready``.
    if (
        refuse_live_lens
        and isinstance(_target_card, Mapping)
        and str(_target_card.get("status") or "") == "review"
        and str(_target_card.get("worker_status") or "") == "review"
        and str(_target_card.get("terminal_substatus") or "") == "validation_failed"
    ):
        _target_verdict = {
            **_target_verdict,
            "can_launch": True,
            "fails_at_launch": False,
            "reason": "",
            "target_substatus": "validation_failed",
        }

    def _bind_visible_card(
        request_id: str, *, terminalize_on_failure: bool
    ) -> dict[str, Any]:
        return self._ensure_quality_reviewer_card_bound(
            request_id=request_id,
            target_request_id=target_request_id,
            target_task_id=target_task_id,
            reviewer_task_id=reviewer_task_id,
            runner=runner,
            adapter_id=adapter_id,
            lens=lens,
            target_card=_target_card,
            terminalize_on_failure=terminalize_on_failure,
        )

    existing = self._live_reviewer_receipt(
        reviewer_task_id,
        target_request_id=target_request_id,
        target_task_id=target_task_id,
        lens=lens,
    )
    if existing is not None:
        if existing.get("ok") is not True:
            return existing
        bound = _bind_visible_card(
            str(existing.get("request_id") or ""),
            terminalize_on_failure=False,
        )
        if bound.get("ok") is not True:
            return bound
        return existing
    if not _target_verdict.get("can_launch"):
        return {
            "ok": False,
            "error": "quality_review_target_not_review_ready",
            "reason": _target_verdict.get("reason"),
            "target_substatus": _target_verdict.get("target_substatus"),
            "fails_at_launch": True, "request_created": False,
        }
    # The named request must be an exact ledger request of the target task
    # BEFORE anything is reserved: the packet build re-checks it, but only
    # on the background owner after an attempt already exists, and a
    # prefix must never reach a reviewer (NF-2026-01748).
    target_refusal = self._exact_quality_review_target_refusal(
        target_request_id, target_task_id
    )
    if target_refusal is not None:
        return {**target_refusal, "request_created": False}
    stale_refusal = _launch_isolated_mod.stale_reviewer_target_refusal(  # NF-2026-02357
        self, target_card=_target_card, target_request_id=target_request_id,
        target_task_id=target_task_id, reviewer_task_id=reviewer_task_id,
    )
    if stale_refusal is not None:
        return stale_refusal
    # The target claim epoch is sealed into the reservation so reuse of a
    # live reviewer is keyed to this exact claim, not merely its request.
    # The caller's epoch is the one that commissioned this review; the
    # card's is only a fallback for callers that name none, because the
    # card can be re-claimed between commissioning and launch.
    caller_epoch = "" if target_claim_epoch is None else str(target_claim_epoch)
    if caller_epoch:
        target_claim_epoch = caller_epoch
    else:
        card_epoch = (
            _target_card.get("claim_epoch")
            if isinstance(_target_card, Mapping) else None
        )
        target_claim_epoch = "" if card_epoch is None else str(card_epoch)
    reservation = self._reserve_quality_reviewer_attempt(
        reviewer_task_id=reviewer_task_id,
        runner=runner,
        adapter_id=adapter_id,
        target_request_id=target_request_id,
        target_task_id=target_task_id,
        lens=lens,
        model=model,
        timeout_seconds=timeout_seconds,
        refuse_live_lens=refuse_live_lens,
        **({"target_claim_epoch": target_claim_epoch} if target_claim_epoch else {}),
        **({"hedge_incumbent_request_id": hedge_incumbent_request_id}
           if hedge_incumbent_request_id else {}),
    )
    if reservation.get("ok") is not True:
        return reservation
    if reservation.get("reused_existing_reviewer") is True:
        # Another reviewer already owns this exact target and lens; its own
        # card is already bound. Binding THIS reviewer task id to that
        # request would corrupt both, so the reused receipt is returned
        # untouched and no provider is started. Whether one exists at all
        # is decided in exactly one place -- the reservation's own
        # ``existing_lens_reviewer`` call -- so a lens whose only reviewer
        # went blind arrives here with nothing to return (NF-2026-01158).
        return reservation
    request_id = str(reservation["request_id"])
    bound = _bind_visible_card(
        request_id,
        terminalize_on_failure=not bool(reservation.get("already_reserved")),
    )
    if bound.get("ok") is not True:
        return bound
    if reservation.get("already_reserved"):
        return reservation
    threading.Thread(
        target=self._launch_reserved_quality_reviewer,
        kwargs={

            "request_id": request_id,
            "target_request_id": target_request_id,
            "target_task_id": target_task_id,
            "reviewer_task_id": reviewer_task_id,
            "runner": runner,
            "adapter_id": adapter_id,
            "lens": lens,
            "model": model,
            "timeout_seconds": timeout_seconds,
        },
        name=f"aiworkhub-reviewer-{request_id[:8]}",
        daemon=True,
    ).start()
    return {
        "ok": True,
        "launch_implemented": LAUNCH_IMPLEMENTED,
        "launch_enabled": True,
        "request_id": request_id,
        "task_id": reviewer_task_id,
        "runner": runner,
        "topic": "quality_review",
        "adapter_id": adapter_id,
        "model": model,
        "state": "starting",
        "pid": 0,
        "deferred": True,
        "already_reserved": False,
        "launch_request_id": request_id,
        "shell": False,
        **_legacy_timeout_fields(timeout_seconds),
    }

def cancel_disposed_reviewer_processes(
    self,
    reviewer_finalization: Any,
    *,
    rejected_request_id: str = "",
    reason: str = "parent_candidate_rejected",
) -> dict[str, Any]:
    """Cancel every live request bound to a rejected review, exact and stable.

    Only task ids in ``core.reject_review``'s durable ``reviewer_finalization``
    receipt are matched, plus (given ``rejected_request_id``) every other
    non-terminal ledger request whose quality-review binding targets that
    exact request -- a server-launched auto reviewer is never a card child.
    A stable ledger snapshot is required so a racing append cannot hide a
    newly launched reviewer.  NF-2026-01764: the parent reject has already
    committed, so an ``OSError`` from one cancel is never raised or retried;
    it lands in ``cleanup_deferred`` with a ``stage``.  Only
    ``workspace_cleanup`` -- a path under that request's own worktree root,
    e.g. WinError 5 on a held handle -- is left for the storage janitor /
    stranded-worktree recovery.  Any other failure (termination, ledger
    append) is ``reviewer_cancel_failed``, which no janitor recovers.
    """
    # Every module-level name this function used while it lived in
    # ``process_launcher`` is re-bound here from that exact module object, so a
    # ``monkeypatch.setattr(process_launcher, name, ...)`` seam still reaches the
    # code below.  The list is ``CANCEL_DISPOSED_REVIEWER_PROCESSES_SEAM_NAMES``.
    from . import process_launcher as _pl

    TERMINAL_PROCESS_STATES = _pl.TERMINAL_PROCESS_STATES
    _launch_isolated_mod = _pl._launch_isolated_mod
    _oserror_within = _pl._oserror_within
    cleanup_deferral = _pl.cleanup_deferral
    task_store = _pl.task_store

    rows = reviewer_finalization if isinstance(reviewer_finalization, list) else []
    reviewer_task_ids = sorted({
        str(row.get("task_id") or "").strip()
        for row in rows
        if isinstance(row, dict) and str(row.get("task_id") or "").strip()
    })
    target_request_id = str(rejected_request_id or "").strip()
    if not reviewer_task_ids and not target_request_id:
        return {
            "schema_id": "aiworkhub.reviewer_process_cancellation.v1",
            "ok": True,
            "state": "no_bound_reviewers",
            "reviewer_task_ids": [],
            "cancelled": [],
        }

    latest, generation = self._latest_by_request_stable()
    if generation is None:
        return {
            "schema_id": "aiworkhub.reviewer_process_cancellation.v1",
            "ok": False,
            "state": "reconcile_pending",
            "blocked_reason": "process_ledger_snapshot_unproven",
            "reviewer_task_ids": reviewer_task_ids,
            "cancelled": [],
        }

    exact_ids = set(reviewer_task_ids)
    candidates: list[tuple[str, dict[str, Any], str]] = []
    for request_id, event in sorted(latest.items(), key=lambda item: item[0]):
        if not isinstance(event, dict) or str(event.get("state") or "") in TERMINAL_PROCESS_STATES:
            continue
        if str(event.get("task_id") or "") in exact_ids:
            candidates.append((str(request_id), event, "bound_child"))
        elif target_request_id and self._reviewer_launch_target_request_id(
            str(request_id), event
        ) == target_request_id:
            candidates.append((str(request_id), event, "target_request"))

    cancelled: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    for request_id, event, match in candidates:
        try:
            outcome = self.cancel(request_id, reason=reason)
        except OSError as exc:
            workspace_root = task_store._request_worktree_path(self.repo, request_id).parent
            if _oserror_within(exc, workspace_root):
                stage, outcome = "workspace_cleanup", {
                    "state": "cleanup_deferred", "blocked_reason": "cleanup_deferred",
                }
            else:
                stage, outcome = "reviewer_cancel_failed", {
                    "state": "cancel_failed", "blocked_reason": "reviewer_cancel_failed",
                }
            deferred.append(cleanup_deferral(exc, f"request:{request_id}", stage=stage))
        cancelled.append({
            "task_id": str(event.get("task_id") or ""),
            "request_id": request_id,
            "match": match,
            "ok": bool(outcome.get("ok")) if isinstance(outcome, dict) else False,
            "state": str(outcome.get("state") or "") if isinstance(outcome, dict) else "",
            "blocked_reason": (
                str(outcome.get("blocked_reason") or "")
                if isinstance(outcome, dict)
                else "cancel_result_invalid"
            ),
        })
    superseded_cards = _launch_isolated_mod.supersede_queued_reviewers(  # NF-2026-02357
        self, latest=latest, candidates=candidates,
        target_request_id=target_request_id, reason=reason,
    )
    return {
        "schema_id": "aiworkhub.reviewer_process_cancellation.v1",
        "ok": all(row["ok"] for row in cancelled)
        and all(row["ok"] for row in superseded_cards),
        "state": "completed",
        "reviewer_task_ids": reviewer_task_ids,
        "cancelled": cancelled,
        **({"superseded_cards": superseded_cards} if superseded_cards else {}),
        **({"cleanup_deferred": deferred} if deferred else {}),
    }
