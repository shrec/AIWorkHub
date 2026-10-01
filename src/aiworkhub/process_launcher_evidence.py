"""Candidate-retention and immutable-input evidence for process launches."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping

from .process_launcher_acceptance import changed_path_hashes
from .worker_workspace import WorkerWorkspace, WorkspaceError


DELTA_RETAINING_TERMINAL_STATES = frozenset(
    {"validation_failed", "timed_out", "worker_failed"}
)


def retained_candidate_identity_evidence(
    workspace: WorkerWorkspace,
    metadata: dict[str, Any],
    request_id: str,
    changed: list[str],
    claim_state: str,
    *,
    path_hashes: Mapping[str, str | None] | None = None,
) -> dict[str, Any]:
    if not changed:
        return {}
    # NF-2026-01199: a caller that already captured these bytes passes the
    # hashes it derived from them, so the published identity cannot disagree
    # with the artifact sealed from the same capture.
    if path_hashes is None:
        path_hashes = changed_path_hashes(workspace, changed)
    if not path_hashes or set(path_hashes) != set(changed):
        return {}
    workspace_metadata = workspace.as_metadata()
    candidate_authority = {
        "schema_id": "aiworkhub.python_candidate_authority.v1",
        "sources": [
            {
                "path": path,
                "state": (
                    "added" if workspace.parent_baseline.get(path) is None else "modified"
                ),
                "bytes_sha256": path_hashes[path],
            }
            for path in sorted(path_hashes)
        ],
    }
    workspace_metadata["python_candidate_authority"] = dict(candidate_authority)
    return {
        "changed_path_hashes": path_hashes,
        "claim_state": claim_state,
        "python_candidate_authority": dict(candidate_authority),
        "workspace": workspace_metadata,
        "request_identity": {
            "request_id": request_id,
            "task_id": str(metadata["task_id"]),
            "runner": str(metadata["runner"]),
            "topic": str(metadata["topic"]),
            "repo": str(workspace.repo),
            "claim_epoch": metadata.get("claim_epoch"),
            "allowed_writes": list(workspace.allowed_writes),
            "base_oid": workspace.base_oid,
            "parent_baseline": dict(workspace.parent_baseline),
        },
    }


def retained_candidate_seal_evidence(
    workspace: WorkerWorkspace,
    metadata: dict[str, Any],
    request_id: str,
    changed: list[str],
    claim_state: str,
) -> dict[str, Any]:
    """Publish the retained hashes AND the sealed delta from ONE capture.

    NF-2026-01199: both failure paths used to hash the retained worktree and
    then re-read it to seal, so any writer between the two reads (a still
    running validation child, an editor, a normalizer) published a
    ``changed_path_hashes`` the sealed artifact disagreed with, and the
    successor died in ``worker_workspace.verify_rework_delta_artifact``.  This
    is the success path's invariant (``successful_candidate_evidence``): both
    halves are derived from the same bytes.  When that single capture cannot
    be made, the pair is NOT faked from a second read -- the hashes are
    published alone and the seal is refused by name, which is what a failed
    seal already does for a symlink, an unsafe path or an over-limit
    candidate.
    """
    from . import process_launcher
    from .successful_rework_recovery import (
        capture_candidate_paths,
        captured_path_hashes,
    )

    if not changed:
        return {}
    captured: list[tuple[str, bytes | None]] | None = None
    refused: dict[str, Any] | None = None
    try:
        captured = capture_candidate_paths(workspace.path, changed)
    except (OSError, ValueError, WorkspaceError) as exc:
        refused = {
            "schema_id": "aiworkhub.rework_delta_seal.v1",
            "sealed": False,
            "reason": f"rework_delta_capture_failed:{exc}"[:300],
        }
    evidence = retained_candidate_identity_evidence(
        workspace, metadata, request_id, changed, claim_state,
        path_hashes=None if captured is None else captured_path_hashes(captured),
    )
    if not evidence:
        return {}
    delta = refused or process_launcher._terminal_rework_delta_evidence(
        workspace, metadata, request_id, changed, captured_entries=captured,
    )
    if delta is not None:
        evidence["rework_delta"] = delta
    return evidence


def is_rework_attempt(metadata: Mapping[str, Any]) -> bool:
    predecessor = metadata.get("rework_predecessor")
    return isinstance(predecessor, dict) and bool(predecessor)


def retained_rework_candidate_evidence(
    terminal_state: str,
    workspace: WorkerWorkspace,
    metadata: dict[str, Any],
    request_id: str,
    changed: list[str],
    claim_state: str,
) -> dict[str, Any]:
    if terminal_state not in DELTA_RETAINING_TERMINAL_STATES or not changed:
        return {}
    try:
        return retained_candidate_seal_evidence(
            workspace, metadata, request_id, changed, claim_state
        )
    except WorkspaceError:
        return {}


def path_manifest(base: Path, declared: list[str]) -> dict[str, dict[str, Any]]:
    """Return a bounded deterministic manifest for declared relative paths."""

    try:
        base_resolved = base.resolve()
    except OSError:
        base_resolved = base
    manifest: dict[str, dict[str, Any]] = {}
    for relative in declared:
        relative = str(relative)
        target = base / relative
        if target.is_symlink():
            manifest[relative] = {"kind": "missing"}
            continue
        try:
            resolved = target.resolve()
        except OSError:
            manifest[relative] = {"kind": "missing"}
            continue
        if resolved != base_resolved and base_resolved not in resolved.parents:
            manifest[relative] = {"kind": "missing"}
            continue
        if resolved.is_dir():
            try:
                names = sorted(path.name for path in resolved.iterdir())
            except OSError:
                manifest[relative] = {"kind": "missing"}
                continue
            digest = hashlib.sha256()
            for name in names:
                child = resolved / name
                try:
                    size = child.stat().st_size if child.is_file() else -1
                except OSError:
                    size = -1
                digest.update(f"{name}:{size}\n".encode())
            manifest[relative] = {
                "kind": "dir",
                "entry_count": len(names),
                "listing_sha256": digest.hexdigest(),
            }
        elif resolved.is_file():
            try:
                data = resolved.read_bytes()
            except OSError:
                manifest[relative] = {"kind": "missing"}
                continue
            manifest[relative] = {
                "kind": "file",
                "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data),
                "line_count": data.count(b"\n"),
            }
        else:
            manifest[relative] = {"kind": "missing"}
    return manifest
