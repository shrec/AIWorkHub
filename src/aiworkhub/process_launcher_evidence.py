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


# NF-2026-01370 (a). One constant, minted here beside ``is_rework_attempt``
# because this is the module that already owns what "a rework attempt" means.
REWORK_NO_DELTA = "rework_no_delta"

# How much of the refused path list travels with the reason. Bounded: the
# refusal names which paths were reproduced, it is not a place for an
# unbounded manifest.
MAX_REWORK_NO_DELTA_REASON_PATH_CHARS = 400

# NF-2026-01405. The one recorded predecessor terminal for which reproducing
# the predecessor's exact bytes IS the recovery rather than ignored reject
# findings: a predecessor whose terminal was ``validation_failed`` never had a
# passing validation, so re-running those bytes through validation is the only
# thing that can decide them. Every other terminal -- ``review_ready`` (an
# ordinary manager reject), ``worker_failed``, ``finalize_failed``, anything
# else -- keeps the refusal, and so does an absent or non-matching field:
# unknown is never an exemption.
REWORK_NO_DELTA_EXEMPT_PREDECESSOR_TERMINAL = "validation_failed"


def rework_no_delta_refusal(
    workspace: WorkerWorkspace,
    metadata: Mapping[str, Any],
    changed: list[str],
    *,
    validation_only_replay: bool,
) -> str:
    """Name a rework attempt that reproduced its predecessor byte for byte.

    NF-2026-01370 (a). ``worker_workspace.validate_required_outputs`` counts
    the SEALED inherited predecessor delta as a change. That is correct for a
    ``validation_only_replay`` -- no provider ran, so there is nothing new to
    require -- and a false green for a provider-relaunched rework: an attempt
    that edited nothing still arrives at the finalizer with the predecessor's
    paths in ``changed`` and reaches ``review_ready``.

    The refusal is minted only when the attempt's changed path SET and every
    per-path SHA-256 equal the rejected predecessor's ``changed_path_hashes``
    -- the sealed inherited delta, re-measured through the same
    ``changed_path_hashes`` owner the retention path seals with, so the two
    sides can never be two different measurements. One
    changed byte, one added path or one removed path is a real delta and is
    never refused. An empty, absent or non-string-digest predecessor record
    proves no identity to compare against, so it mints nothing and leaves the
    ordinary mandatory-output and quality gates to decide.

    NF-2026-01405. The refusal stops a rework that ignored the reject findings
    from reaching review on the predecessor's bytes -- which presumes those
    bytes already passed validation once. When the predecessor's recorded
    terminal substatus was ``validation_failed`` they never did, so re-running
    them IS the recovery and nothing is minted: the ordinary validation,
    quality and required-output gates then decide, a pass reaching
    ``review_ready`` and a failure ending ``validation_failed`` exactly as
    today. The substatus is read from the same ``rework_predecessor`` record
    that publishes the sealed identity, so the exemption and the bytes it
    exempts can never describe two different predecessors.
    """
    if validation_only_replay or not is_rework_attempt(metadata):
        return ""
    predecessor = metadata.get("rework_predecessor") or {}
    if (
        predecessor.get("terminal_substatus")
        == REWORK_NO_DELTA_EXEMPT_PREDECESSOR_TERMINAL
    ):
        return ""
    sealed = predecessor.get("changed_path_hashes")
    if not isinstance(sealed, dict) or not sealed:
        return ""
    inherited = {str(path): digest for path, digest in sealed.items()}
    if any(not isinstance(digest, str) or not digest for digest in inherited.values()):
        return ""
    if set(inherited) != set(changed):
        return ""
    current = changed_path_hashes(workspace, list(changed))
    if any(current.get(path) != digest for path, digest in inherited.items()):
        return ""
    reproduced = ",".join(sorted(inherited))
    return (
        f"{REWORK_NO_DELTA}:"
        + reproduced[:MAX_REWORK_NO_DELTA_REASON_PATH_CHARS]
    )


# NF-2026-01370 (b). The typed marker that says WHY source_graph is reported
# missing, so the gate evidence explains itself instead of only naming the
# tool.
SOURCE_GRAPH_LIVE_CALLS_ALL_FAILED = "source_graph_live_calls_all_failed"


def source_graph_orientation_voided(
    failed_source_graph_calls: int | None,
    *,
    live_calls: int,
    successful_calls: int,
) -> bool:
    """Say whether injected orientation may no longer satisfy ``source_graph``.

    NF-2026-01370 (b). Supervisor-injected orientation records the
    COORDINATOR's own pre-launch query. It is evidence that the initial call
    was already executed, and it says nothing at all about a worker that
    issued live Source Graph calls and had EVERY one of them fail: crediting
    it there turns a worker that discovered nothing into a satisfied gate.

    ``failed_source_graph_calls`` is the already-decoded authenticated count.
    ``None`` is a malformed count from the authenticated ledger and voids
    orientation (an unreadable failure record is never a zero); an ABSENT
    count decodes to ``0`` and voids nothing, so a verifier that never
    reported the field behaves exactly as before.
    """
    if live_calls > 0 or successful_calls > 0:
        return False
    if failed_source_graph_calls is None:
        return True
    return failed_source_graph_calls >= 1


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
