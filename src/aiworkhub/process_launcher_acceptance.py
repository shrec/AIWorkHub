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
) -> dict[str, Any]:
    """Build the sole canonical, post-promotion acceptance identity."""
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
    receipt["receipt_id"] = "sha256:" + digest(receipt)
    return receipt


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
