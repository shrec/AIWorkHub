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

from typing import TYPE_CHECKING, Any

from . import quality_evidence

if TYPE_CHECKING:  # names used only in annotations, which are never evaluated
    from .worker_workspace import WorkerWorkspace as _WorkerWorkspaceT

__all__ = ["accept_review"]

# The seams are runtime names only.  ``Any`` and ``_WorkerWorkspaceT`` appear
# solely in annotations -- which ``from __future__ import annotations`` leaves
# as strings -- so they are resolved from this module's own imports and are
# deliberately NOT re-bound below.  A name that is never evaluated cannot carry
# a monkeypatch, and shadowing it here would only hide it from the type checker.
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
    requested_risk_tier: str = quality_evidence.RISK_LOW,
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

    ``requested_risk_tier`` and ``risk_signals`` are manager-owned inputs.
    Medium-and-higher profiles materialize a fresh combined-tree workspace
    and fail closed without the required read-only reviewer reports.
    High/critical profiles additionally require ``confirm_high_risk``.
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
        latest = events[-1]
        if str(latest.get("task_id") or "") != task_id:
            return {
                "ok": False,
                "error": "request_task_identity_mismatch",
                "request_id": request_id,
                "task_id": task_id,
            }
        runner = str(latest.get("runner") or "")
        topic = str(latest.get("topic") or "")
        if not runner or not topic:
            return {
                "ok": False,
                "error": "request_identity_incomplete",
                "request_id": request_id,
                "task_id": task_id,
            }

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
            risk_profile = quality_evidence.resolve_risk_profile(
                requested_risk_tier,
                signals=effective_risk_signals,
            )
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
                        requested_risk_tier=requested_risk_tier,
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
            reviewer_ids = list(reviewer_request_ids or [])
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
                requested_risk_tier=requested_risk_tier,
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
        accepted_reply = {
            "ok": True, "request_id": request_id, "task_id": task_id,
            "promoted_paths": promoted,
            "reviewer_finalization": reviewer_finalization,
            "acceptance_evidence_record": acceptance_evidence_record,
            "accepted_outcome_receipt": accepted_outcome_receipt,
            "needfix_closure": needfix_closure,
            "learning_commit_owed": learning_owed,
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
