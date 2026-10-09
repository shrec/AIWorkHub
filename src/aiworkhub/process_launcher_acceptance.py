"""Accepted-outcome identity and finished-card retry reconciliation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from . import task_engine, task_store
from .worker_workspace import WorkerWorkspace, WorkspaceError


def changed_path_hashes(
    workspace: WorkerWorkspace, changed: list[str]
) -> dict[str, str | None]:
    """Hash each declared-changed path in the isolated workspace."""
    hashes: dict[str, str | None] = {}
    for relative in changed:
        source = workspace.path / relative
        if source.is_symlink() or not source.is_file():
            hashes[relative] = None
            continue
        hashes[relative] = hashlib.sha256(source.read_bytes()).hexdigest()
    return hashes


def accepted_outcome_receipt(
    repo: Path,
    *,
    task_id: str,
    request_id: str,
    claim_epoch: int,
    base_oid: str,
    promoted_paths: list[str],
    changed_path_hashes: dict[str, str | None],
    attempt_artifact_manifest: dict[str, Any],
    manager_correction: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the sole canonical, post-promotion acceptance identity.

    ``manager_correction`` -- ``None`` unless a manager correction was validated
    and applied on top of the sealed candidate (NF-2026-01436) -- adds one
    optional object naming the patch digest, the findings it repairs, the paths
    it touched and those paths' candidate and corrected digests.
    ``changed_path_hashes`` are then the CORRECTED hashes, which is what the
    canonical tree holds and what ``repository_revision`` is derived from, so a
    reader can recover both the bytes a reviewer judged and the bytes that
    landed. The optional object is covered by ``receipt_id``.
    """
    if not base_oid or not isinstance(attempt_artifact_manifest, dict):
        raise WorkspaceError("accepted_outcome_identity_incomplete")
    paths = sorted(promoted_paths)
    canonical_hashes: dict[str, str | None] = {}
    for relative in paths:
        path = repo / relative
        canonical_hashes[relative] = (
            hashlib.sha256(path.read_bytes()).hexdigest()
            if path.is_file() and not path.is_symlink()
            else None
        )
    if paths != sorted(changed_path_hashes) or canonical_hashes != changed_path_hashes:
        raise WorkspaceError("post_promotion_candidate_mismatch")
    digest = lambda value: hashlib.sha256(  # noqa: E731
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    receipt: dict[str, Any] = {
        "schema_id": task_engine.ACCEPTED_OUTCOME_RECEIPT_SCHEMA,
        "task_id": task_id,
        "request_id": request_id,
        "claim_epoch": int(claim_epoch),
        "base_oid": base_oid,
        "promoted_paths": paths,
        "changed_path_hashes": canonical_hashes,
        "attempt_artifact_manifest_id": digest(attempt_artifact_manifest),
        "repository_revision": "sha256:"
        + digest({"base_oid": base_oid, "changed_path_hashes": canonical_hashes}),
    }
    if manager_correction is not None:
        receipt["manager_correction"] = _correction_identity(
            manager_correction, canonical_hashes
        )
    receipt["receipt_id"] = "sha256:" + digest(receipt)
    return receipt


def _correction_identity(
    manager_correction: dict[str, Any], canonical_hashes: dict[str, str | None]
) -> dict[str, Any]:
    """The six receipt fields of one applied manager correction.

    Projected from the accept path's own validated record, so the receipt never
    carries an internal field and never invents one. The correction is checked
    against the bytes that ACTUALLY landed: every corrected path must be a
    promoted path whose canonical digest is the corrected digest, and must
    differ from the candidate digest it replaced. A wiring mistake fails closed
    here, before the card is finalized, rather than sealing a receipt that
    describes bytes nobody promoted.
    """
    paths = sorted({str(value) for value in (manager_correction.get("paths") or ())})
    candidate = {
        str(key): str(value)
        for key, value in (manager_correction.get("candidate_path_hashes") or {}).items()
    }
    corrected = {
        str(key): str(value)
        for key, value in (manager_correction.get("corrected_path_hashes") or {}).items()
    }
    finding_ids = sorted(
        {str(value) for value in (manager_correction.get("finding_ids") or ())}
    )
    patch_sha256 = str(manager_correction.get("patch_sha256") or "")
    if (
        not paths
        or not finding_ids
        or len(patch_sha256) != 64
        or paths != sorted(candidate)
        or paths != sorted(corrected)
        or any(
            corrected[relative] != canonical_hashes.get(relative)
            or candidate[relative] == corrected[relative]
            for relative in paths
        )
    ):
        raise WorkspaceError("manager_correction_receipt_mismatch")
    return {
        "patch_sha256": patch_sha256,
        "finding_ids": finding_ids,
        "paths": paths,
        "candidate_path_hashes": candidate,
        "corrected_path_hashes": corrected,
        "reason": str(manager_correction.get("reason") or ""),
    }


def finished_acceptance_result(
    repo: Path,
    card: dict[str, Any],
    *,
    task_id: str,
    request_id: str,
    canonical_status: Callable[[dict[str, Any]], str],
    close_needfix: Callable[[str, str], dict[str, Any]],
) -> dict[str, Any] | None:
    """Return the idempotent response when the durable task is finished."""
    canonical = canonical_status(card)
    if canonical != "finished":
        try:
            stored_card = task_store.get_task(repo, task_id)
        except (task_store.TaskStoreError, OSError, TypeError, ValueError):
            stored_card = None
        if isinstance(stored_card, dict) and canonical_status(stored_card) == "finished":
            card = stored_card
            canonical = "finished"
    if canonical != "finished":
        return None

    already = str(card.get("accepted_request_id") or "") == request_id
    closure = (
        close_needfix(task_id, request_id)
        if already
        else {"state": "not_attempted"}
    )
    return {
        "ok": already,
        "already_accepted": already,
        "request_id": request_id,
        "task_id": task_id,
        "error": "" if already else "task_already_finished_by_other_request",
        "accepted_outcome_receipt": (
            (card.get("accept_evidence") or {}).get("accepted_outcome_receipt")
            if already
            else None
        ),
        "needfix_closure": closure,
    }
