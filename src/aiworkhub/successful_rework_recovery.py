"""Authenticate retained successful episodes before granting rework authority."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path
from typing import Any, Mapping

from . import attempt_artifacts, platform_io, runtime_temp, worker_workspace


class SuccessfulReworkRecoveryError(ValueError):
    """A retained successful episode cannot safely become rework authority."""


def _contained_path(path: Path, root: Path) -> None:
    """Reject links through the configured root, including external runtimes."""
    try:
        relative = path.relative_to(root)
        if not path.is_absolute() or any(part in {".", ".."} for part in relative.parts):
            raise ValueError
        for parent in (root, *root.parents):
            if parent.is_symlink():
                raise ValueError
        current = root
        for part in relative.parts:
            current /= part
            if current.is_symlink():
                raise ValueError
    except (OSError, RuntimeError, ValueError) as exc:
        raise SuccessfulReworkRecoveryError("successful_rework_path_unsafe") from exc


@contextmanager
def _regular_descriptor(path: Path):
    """Traverse from a pinned filesystem root and retain every parent handle."""
    with ExitStack() as handles:
        if platform_io.is_windows():
            authority = handles.enter_context(runtime_temp.WindowsDirectoryAuthority(Path(path.anchor)))
            parent = authority.handle
            for name in path.parts[1:-1]:
                parent = handles.enter_context(
                    platform_io.open_windows_relative_child_directory(parent, name)
                ).value
            descriptor = platform_io.open_windows_relative_regular_file_descriptor(parent, path.name)
        else:
            flags = platform_io.directory_open_flags()
            parent = os.open(path.anchor, flags)
            handles.callback(os.close, parent)
            for name in path.parts[1:-1]:
                parent = os.open(name, flags, dir_fd=parent)
                handles.callback(os.close, parent)
            descriptor = os.open(
                path.name, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0),
                dir_fd=parent,
            )
        handles.callback(os.close, descriptor)
        yield descriptor


def _read_regular(path: Path, root: Path, limit: int, *, missing_ok: bool = False) -> bytes | None:
    """Read once through anchored parents; authenticate the descriptor read."""
    _contained_path(path, root)
    try:
        with _regular_descriptor(path) as fd, os.fdopen(fd, "rb", closefd=False) as stream:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise SuccessfulReworkRecoveryError("successful_rework_path_unsafe")
            if before.st_size > limit or limit < 0:
                raise SuccessfulReworkRecoveryError("successful_rework_content_too_large")
            data = stream.read(limit + 1)
            after = os.fstat(fd)
            if len(data) > limit:
                raise SuccessfulReworkRecoveryError("successful_rework_content_too_large")
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_nlink) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_nlink
            ) or len(data) != after.st_size:
                raise SuccessfulReworkRecoveryError("successful_rework_content_changed")
            return data
    except FileNotFoundError as exc:
        if missing_ok:
            return None
        raise SuccessfulReworkRecoveryError("successful_rework_read_failed") from exc
    except OSError as exc:
        raise SuccessfulReworkRecoveryError("successful_rework_read_failed") from exc


def _verified_payloads(runtime: Path, request_id: str, receipt: Mapping[str, Any]) -> dict[str, Any]:
    manifest_path = Path(str(receipt.get("manifest_path") or ""))
    if (
        receipt.get("schema_id") != "aiworkhub.attempt_artifact_bundle_receipt.v1"
        or receipt.get("attempt_id") != request_id
        or receipt.get("verified") is not True
        or manifest_path.name != attempt_artifacts.MANIFEST_FILENAME
        or manifest_path.parent.name != request_id
        or manifest_path.parent.parent.name != "attempt-artifacts"
        or set(receipt.get("roles") or ()) != attempt_artifacts.REQUIRED_BUNDLE_ROLES
    ):
        raise SuccessfulReworkRecoveryError("successful_rework_artifacts_invalid")
    remaining = worker_workspace.MAX_REWORK_OVERLAY_CONTENT_BYTES
    data = _read_regular(manifest_path, runtime, min(remaining, 1024 * 1024))
    if hashlib.sha256(data).hexdigest() != receipt.get("manifest_sha256"):
        raise SuccessfulReworkRecoveryError("successful_rework_manifest_mismatch")
    try:
        manifest = attempt_artifacts.parse_manifest_json(data.decode("utf-8"))
        roles = [entry.role for entry in manifest.artifacts]
        if (
            manifest.attempt_id != request_id
            or set(roles) != attempt_artifacts.REQUIRED_BUNDLE_ROLES
            or len(roles) != len(set(roles))
            or receipt.get("artifact_count") != len(roles)
        ):
            raise ValueError
        payloads = {}
        for entry in manifest.artifacts:
            if entry.byte_count > remaining:
                raise SuccessfulReworkRecoveryError("successful_rework_content_too_large")
            path = manifest_path.parent / entry.path
            if path.parent != manifest_path.parent:
                raise ValueError
            # Parse exactly the bytes whose digest was checked, never reopen a
            # mutable artifact after a separate verify_json_bundle pass.
            data = _read_regular(path, runtime, min(remaining, entry.byte_count))
            remaining -= len(data)
            if len(data) != entry.byte_count or hashlib.sha256(data).hexdigest() != entry.sha256:
                raise ValueError
            payloads[entry.role] = json.loads(data.decode("utf-8"))
        return payloads
    except (UnicodeError, ValueError) as exc:
        if isinstance(exc, SuccessfulReworkRecoveryError):
            raise
        raise SuccessfulReworkRecoveryError("successful_rework_artifacts_invalid") from exc


def capture_candidate_paths(workspace: Path, paths) -> list[tuple[str, bytes | None]]:
    """Capture once, rejecting unsafe paths and bounding aggregate content."""
    if any(not isinstance(path, str) for path in paths):
        raise SuccessfulReworkRecoveryError("successful_rework_hashes_invalid")
    entries = []
    remaining = worker_workspace.MAX_REWORK_OVERLAY_CONTENT_BYTES
    for relative in sorted(set(paths)):
        if (
            not relative or relative.startswith("/") or "\\" in relative
            or any(part in {"", ".", ".."} for part in relative.split("/"))
        ):
            raise SuccessfulReworkRecoveryError("successful_rework_hashes_invalid")
        source = workspace / relative
        data = _read_regular(source, workspace, remaining, missing_ok=True)
        remaining -= len(data) if data is not None else 0
        entries.append((relative, data))
    return entries


def candidate_entries(workspace: Path, hashes: Mapping[str, Any]) -> list[tuple[str, bytes | None]]:
    """Match the complete payload, including authenticated deletion markers."""
    if any(
        value is not None and (
            not isinstance(value, str) or len(value) != 64
            or any(ch not in "0123456789abcdef" for ch in value)
        ) for value in hashes.values()
    ):
        raise SuccessfulReworkRecoveryError("successful_rework_hashes_invalid")
    entries = capture_candidate_paths(workspace, hashes)
    for relative, data in entries:
        observed = hashlib.sha256(data).hexdigest() if data is not None else None
        if observed != hashes[relative]:
            raise SuccessfulReworkRecoveryError("successful_rework_hash_mismatch")
    return entries


def recover_descriptor(
    repo: Path,
    task_id: str,
    request_id: str,
    claim_epoch: int,
    evidence: Mapping[str, Any],
    *,
    terminal_episode: Mapping[str, Any],
) -> dict[str, Any]:
    """Seal only the successful episode authenticated by the caller's snapshot.

    Legacy artifact metadata has no claim epoch. Its canonical terminal event
    supplies that identity; the caller must compare its exact task preimage
    after this filesystem work and before granting the descriptor authority.
    """
    authority_repo = repo.resolve(strict=False)
    identity = evidence.get("request_identity")
    workspace_meta = evidence.get("workspace")
    hashes = evidence.get("changed_path_hashes")
    receipt = evidence.get("attempt_artifact_manifest")
    if not all(isinstance(value, Mapping) for value in (identity, workspace_meta, hashes, receipt, terminal_episode)):
        raise SuccessfulReworkRecoveryError("successful_rework_evidence_invalid")
    if (
        not isinstance(request_id, str) or len(request_id) != 32
        or any(ch not in "0123456789abcdef" for ch in request_id)
        or identity.get("request_id") != request_id or identity.get("task_id") != task_id
        or type(claim_epoch) is not int or claim_epoch < 1
        or terminal_episode.get("claim_epoch") != claim_epoch
        or type(terminal_episode.get("claim_epoch")) is not int
        or terminal_episode.get("request_id") != request_id
        or terminal_episode.get("runner") != identity.get("runner")
        or terminal_episode.get("substatus") != "review_ready"
        or terminal_episode.get("evidence") != evidence
        or ("claim_epoch" in identity and (
            type(identity.get("claim_epoch")) is not int or identity.get("claim_epoch") != claim_epoch
        ))
        or workspace_meta.get("request_id") != request_id
        or ("repo" in identity and Path(str(identity.get("repo") or "")).resolve(strict=False) != authority_repo)
        or Path(str(workspace_meta.get("repo") or "")).resolve(strict=False) != authority_repo
    ):
        raise SuccessfulReworkRecoveryError("successful_rework_identity_mismatch")
    runtime = worker_workspace.configured_runtime_root(authority_repo).absolute()
    workspace = Path(str(workspace_meta.get("path") or ""))
    if workspace != runtime / "worktrees" / request_id / "worktree":
        raise SuccessfulReworkRecoveryError("successful_rework_workspace_unsafe")
    _contained_path(workspace, runtime)
    if not workspace.is_dir():
        raise SuccessfulReworkRecoveryError("successful_rework_workspace_unsafe")
    payloads = _verified_payloads(runtime, request_id, receipt)
    metadata, diff, review = (payloads.get(role) for role in ("metadata", "diff", "review"))
    if (
        not isinstance(metadata, Mapping)
        or metadata.get("schema_id") != "aiworkhub.attempt_metadata.v1"
        or metadata.get("request_identity") != {
            key: identity.get(key) for key in ("request_id", "task_id", "runner", "topic")
        }
        or metadata.get("workspace") != workspace_meta
        or not isinstance(diff, Mapping)
        or diff.get("schema_id") != "aiworkhub.attempt_diff_index.v1"
        or diff.get("changed_path_hashes") != hashes
        or diff.get("changed_paths") != sorted(hashes)
        or not isinstance(review, Mapping)
        or review.get("schema_id") != "aiworkhub.attempt_review.v1"
        or review.get("target_state") != "review_ready"
        or review.get("kind") != "worker_candidate"
        or review.get("error") not in (None, "")
    ):
        raise SuccessfulReworkRecoveryError("successful_rework_artifact_identity_mismatch")
    entries = candidate_entries(workspace, hashes)
    if os.environ.get("AIWORKHUB_ALLOW_WRITES") != "1":
        raise SuccessfulReworkRecoveryError("successful_rework_writes_disabled")
    sealed = worker_workspace.seal_rework_delta_artifact(
        authority_repo, task_id, request_id, claim_epoch, entries, runtime / "rework_deltas",
    )
    return {
        "schema_id": "aiworkhub.rework_delta_descriptor.v1", "sealed": True,
        "authority_repo": str(authority_repo), "task_id": task_id,
        "request_id": request_id, "claim_epoch": claim_epoch,
        "artifact_path": str(sealed["path"]), "artifact_sha256": str(sealed["digest"]),
    }


def prepare_blocked_recovery(
    root: Path, db_path: Path, task_id: str, feedback_reason: str = "",
) -> dict[str, Any] | None:
    """Perform retained I/O without a writer transaction or task mutations."""
    from . import task_store

    with closing(task_store._connect(db_path, readonly=True)) as conn:
        row = conn.execute(
            "SELECT task_id, runner, topic, status, worker_status, claimed_by, "
            "claimed_at, card_json FROM tasks WHERE task_id=?", (task_id,),
        ).fetchone()
        if row is None or task_store.canonical_status(dict(row)) != "blocked":
            return None
        card = json.loads(str(row["card_json"] or "{}"))
        terminal_row = conn.execute(
            "SELECT runner, payload_json, created_at FROM task_events "
            "WHERE task_id=? AND event='terminal_review' ORDER BY rowid DESC LIMIT 1",
            (task_id,),
        ).fetchone()
    if not isinstance(card, dict) or terminal_row is None:
        return None
    feedback = card.get("reject_review")
    if (row["topic"] == "quality_review" or not (
        str(feedback_reason or "").strip()
        or (isinstance(feedback, dict) and str(feedback.get("reason") or "").strip())
    )):
        return None
    terminal = json.loads(str(terminal_row["payload_json"] or "{}"))
    evidence = terminal.get("evidence") if isinstance(terminal, dict) else None
    if (
        not isinstance(evidence, dict) or terminal.get("substatus") != "review_ready"
        or not isinstance(evidence.get("attempt_artifact_manifest"), dict)
        or not evidence.get("changed_path_hashes")
    ):
        return None
    predecessor = card.get("rework_predecessor")
    if isinstance(predecessor, dict) and predecessor.get("rework_delta") is not None:
        return None
    identity = evidence.get("request_identity") or {}
    if (
        identity.get("task_id") != task_id or identity.get("runner") != row["runner"]
        or identity.get("topic") != row["topic"]
        or terminal_row["runner"] != row["runner"]
        or card.get("launch_request_id") != terminal.get("request_id")
        or type(card.get("claim_epoch")) is not int
        or card.get("claim_epoch") != terminal.get("claim_epoch")
        or (isinstance(predecessor, dict) and (
            predecessor.get("request_id") != terminal.get("request_id")
            or predecessor.get("changed_path_hashes") != evidence.get("changed_path_hashes")
            or predecessor.get("workspace") != evidence.get("workspace")
        ))
    ):
        raise SuccessfulReworkRecoveryError("successful_rework_episode_mismatch")
    descriptor = recover_descriptor(
        root, task_id, terminal["request_id"], terminal["claim_epoch"], evidence,
        terminal_episode=terminal,
    )
    pinned = dict(predecessor or {})
    pinned.update({
        "schema_id": "aiworkhub.rework_predecessor.v1", "task_id": task_id,
        "request_id": descriptor["request_id"], "claim_epoch": descriptor["claim_epoch"],
        "workspace": evidence["workspace"], "changed_path_hashes": evidence["changed_path_hashes"],
        "rework_delta": descriptor,
        "delta_artifact": {"path": descriptor["artifact_path"], "digest": descriptor["artifact_sha256"]},
    })
    return {"row": dict(row), "terminal_row": dict(terminal_row), "predecessor": pinned}


def successful_candidate_evidence(workspace, metadata, request_id, changed):
    """Publish a fresh seal for the entire candidate, even on inherited paths."""
    from . import process_launcher

    predecessor_hashes = (metadata.get("rework_predecessor") or {}).get("changed_path_hashes") or {}
    paths = sorted(set(changed) | {path for path in predecessor_hashes if isinstance(path, str) and path})
    entries = capture_candidate_paths(workspace.path, paths)
    hashes = {path: hashlib.sha256(data).hexdigest() if data is not None else None for path, data in entries}
    descriptor = process_launcher._terminal_rework_delta_evidence(
        workspace, metadata, request_id, paths, captured_entries=entries
    )
    if paths and (
        not isinstance(descriptor, dict) or descriptor.get("sealed") is not True
        or descriptor.get("schema_id") != "aiworkhub.rework_delta_descriptor.v1"
        or descriptor.get("request_id") != request_id
        or descriptor.get("task_id") != metadata.get("task_id")
        or descriptor.get("claim_epoch") != metadata.get("claim_epoch")
    ):
        raise worker_workspace.WorkspaceError("successful_rework_delta_missing")
    return paths, hashes, descriptor
