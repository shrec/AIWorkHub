"""Authenticate retained successful episodes before granting rework authority."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

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
def _regular_descriptor(path: Path, root: Path):
    """Traverse from the trusted containment root and retain every parent handle.

    ``root`` is the root ``_contained_path`` already proved link-free, so no
    directory above it is opened (restricted tokens may not open the drive root).
    On Windows the root is opened with ``open_windows_root_directory_handle``, not
    ``WindowsDirectoryAuthority``: the latter's final-path query is denied
    (winerror=5) under the AppContainer token, so root identity is authenticated
    by its reparse tag and FILE_ID instead.
    """
    names = path.relative_to(root).parts
    if not names:
        raise SuccessfulReworkRecoveryError("successful_rework_path_unsafe")
    with ExitStack() as handles:
        if platform_io.is_windows():
            parent = handles.enter_context(platform_io.open_windows_root_directory_handle(root)).value
            for name in names[:-1]:
                parent = handles.enter_context(
                    platform_io.open_windows_relative_child_directory(parent, name)
                ).value
            descriptor = platform_io.open_windows_relative_regular_file_descriptor(parent, path.name)
        else:
            flags = platform_io.directory_open_flags()
            parent = os.open(root, flags)
            handles.callback(os.close, parent)
            for name in names[:-1]:
                parent = os.open(name, flags, dir_fd=parent)
                handles.callback(os.close, parent)
            descriptor = os.open(
                path.name, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0),
                dir_fd=parent,
            )
        handles.callback(os.close, descriptor)
        yield descriptor


# NF-2026-01739. ``MAX_REWORK_OVERLAY_CONTENT_BYTES`` bounds what
# ``seal_rework_delta_artifact`` base64-inlines into ONE JSON artifact (and so
# the bytes held in memory for it). It is an inline-capture cap, not a limit on
# a candidate: a regular file over the remaining inline budget is carried by
# sha256 + size + repo-relative path, hashed in bounded chunks, and the sealer
# streams its exact bytes into a sidecar blob beside the delta artifact
# (re-hashed while copying, so a file changed since capture fails closed).


@dataclass(frozen=True)
class DigestCarriedContent:
    """A captured regular file too large to inline, identified by digest.

    It is a ``worker_workspace.ReworkBlobSource``: ``copy_into`` re-streams
    the retained file through the same anchored descriptor walk. ``len`` is
    refused by name, so an inline-only consumer refuses it instead of
    dropping or truncating it.
    """

    path: str
    sha256: str
    size: int
    workspace: Path

    def __bool__(self) -> bool:
        return True

    def __len__(self) -> int:
        raise SuccessfulReworkRecoveryError("successful_rework_content_digest_carried")

    def copy_into(self, sink) -> tuple[str, int]:
        _require_safe_relative(self.path)
        return _stream_regular(self.workspace / self.path, self.workspace, sink)


def _require_unchanged(before: os.stat_result, after: os.stat_result, observed: int) -> None:
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_nlink) != (
        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_nlink
    ) or observed != after.st_size:
        raise SuccessfulReworkRecoveryError("successful_rework_content_changed")


def _digest_open_regular(fd: int, stream, before: os.stat_result, sink=None) -> tuple[str, int]:
    """Hash (and optionally copy) an open regular file in bounded chunks."""
    digest, size = worker_workspace.stream_sha256(stream, sink)
    _require_unchanged(before, os.fstat(fd), size)
    return digest, size


def _stream_regular(path: Path, root: Path, sink) -> tuple[str, int]:
    """Copy one regular file through anchored parents, returning its digest."""
    _contained_path(path, root)
    try:
        with _regular_descriptor(path, root) as fd, os.fdopen(fd, "rb", closefd=False) as stream:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise SuccessfulReworkRecoveryError("successful_rework_path_unsafe")
            return _digest_open_regular(fd, stream, before, sink)
    except FileNotFoundError as exc:
        raise SuccessfulReworkRecoveryError("successful_rework_read_failed") from exc
    except (OSError, runtime_temp.RuntimeTempError) as exc:
        raise SuccessfulReworkRecoveryError("successful_rework_read_failed") from exc


def _read_regular(
    path: Path, root: Path, limit: int, *, missing_ok: bool = False, carry_as: str | None = None,
) -> bytes | DigestCarriedContent | None:
    """Read once through anchored parents; authenticate the descriptor read.

    With ``carry_as`` a regular file over ``limit`` is streamed through the
    same descriptor into a ``DigestCarriedContent`` instead of being refused.
    """
    _contained_path(path, root)
    try:
        with _regular_descriptor(path, root) as fd, os.fdopen(fd, "rb", closefd=False) as stream:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise SuccessfulReworkRecoveryError("successful_rework_path_unsafe")
            if limit < 0 or (before.st_size > limit and carry_as is None):
                raise SuccessfulReworkRecoveryError("successful_rework_content_too_large")
            if before.st_size > limit:
                digest, size = _digest_open_regular(fd, stream, before)
                return DigestCarriedContent(carry_as, digest, size, root)
            data = stream.read(limit + 1)
            after = os.fstat(fd)
            if len(data) > limit:
                raise SuccessfulReworkRecoveryError("successful_rework_content_too_large")
            _require_unchanged(before, after, len(data))
            return data
    except FileNotFoundError as exc:
        if missing_ok:
            return None
        raise SuccessfulReworkRecoveryError("successful_rework_read_failed") from exc
    except (OSError, runtime_temp.RuntimeTempError) as exc:
        # The anchored walk can fail to open a parent (e.g. a drive root a
        # restricted token cannot list); fail closed as a ValueError so no
        # RuntimeError escapes callers that catch OSError/ValueError.
        raise SuccessfulReworkRecoveryError("successful_rework_read_failed") from exc


def _require_safe_relative(relative: str) -> None:
    if (
        not relative or relative.startswith("/") or "\\" in relative
        or any(part in {"", ".", ".."} for part in relative.split("/"))
    ):
        raise SuccessfulReworkRecoveryError("successful_rework_hashes_invalid")


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


CapturedContent = bytes | DigestCarriedContent | None


def capture_candidate_paths(workspace: Path, paths) -> list[tuple[str, CapturedContent]]:
    """Capture once, rejecting unsafe paths and bounding aggregate inline content.

    A regular file over the remaining inline budget is carried by digest
    (``DigestCarriedContent``); it never consumes inline budget, and all
    carried bytes together stay under ``MAX_REWORK_CARRIED_BYTES``.
    """
    if any(not isinstance(path, str) for path in paths):
        raise SuccessfulReworkRecoveryError("successful_rework_hashes_invalid")
    entries = []
    remaining = worker_workspace.MAX_REWORK_OVERLAY_CONTENT_BYTES
    carried = 0
    for relative in sorted(set(paths)):
        _require_safe_relative(relative)
        source = workspace / relative
        data = _read_regular(source, workspace, remaining, missing_ok=True, carry_as=relative)
        if isinstance(data, DigestCarriedContent):
            carried += data.size
            if carried > worker_workspace.MAX_REWORK_CARRIED_BYTES:
                raise SuccessfulReworkRecoveryError(
                    f"successful_rework_carried_bytes_exceeds_limit:{carried}"
                    f">{worker_workspace.MAX_REWORK_CARRIED_BYTES}"
                )
        elif data is not None:
            remaining -= len(data)
        entries.append((relative, data))
    return entries


def _captured_digest(data: CapturedContent) -> str | None:
    if isinstance(data, DigestCarriedContent):
        return data.sha256
    return hashlib.sha256(data).hexdigest() if data is not None else None


def captured_path_hashes(
    entries: Sequence[tuple[str, CapturedContent]]
) -> dict[str, str | None]:
    """Hash exactly the captured bytes, with ``None`` for a deletion marker.

    NF-2026-01199: the one operation that makes a published
    ``changed_path_hashes`` and a sealed delta artifact agree is deriving both
    from the SAME ``capture_candidate_paths`` result.  The success path and
    both terminal failure paths share this helper rather than each re-reading
    the worktree, which is what let a concurrent writer split the pair.
    A digest-carried file contributes the digest streamed at capture.
    """
    return {relative: _captured_digest(data) for relative, data in entries}


def candidate_entries(workspace: Path, hashes: Mapping[str, Any]) -> list[tuple[str, CapturedContent]]:
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
        if _captured_digest(data) != hashes[relative]:
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
    hashes = captured_path_hashes(entries)
    # Over-inline-cap files are sealed as sidecar blobs from the same capture.
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
