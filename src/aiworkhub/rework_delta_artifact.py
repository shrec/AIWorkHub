"""Sealed rework-delta artifacts: sidecar blobs, sealing, verification, materialization.

Moved verbatim out of ``worker_workspace`` (module size ratchet).  Every public
and underscore name here is re-exported by ``worker_workspace`` so existing
``worker_workspace.<name>`` callers are unchanged.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Iterable, Protocol

if TYPE_CHECKING:
    from .promotion_merge import ReworkRebase
    from .worker_workspace import WorkerWorkspace

_REWORK_BLOB_CHUNK_BYTES = 1024 * 1024


def _write_rework_path(destination: Path, relative: str, content: bytes | None) -> None:
    if content is None:
        if destination.is_symlink() or destination.is_file():
            destination.unlink()
        elif destination.exists():
            raise WorkspaceError(f"rework_predecessor_delete_non_file:{relative}")
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)


def stream_sha256(stream: Any, sink: Any = None) -> tuple[str, int]:
    """Hash (and optionally copy) a binary stream in bounded chunks."""
    digest, size = hashlib.sha256(), 0
    while chunk := stream.read(_REWORK_BLOB_CHUNK_BYTES):
        digest.update(chunk)
        size += len(chunk)
        if sink is not None:
            sink.write(chunk)
    return digest.hexdigest(), size


class ReworkBlobSource(Protocol):
    """Over-inline-cap content the sealer streams into a sidecar blob.

    ``copy_into`` writes the exact bytes to ``sink`` and returns the
    ``(sha256, size)`` it observed while copying; the sealer refuses the blob
    unless both still equal the recorded ``sha256`` and ``size``.
    """

    sha256: str
    size: int

    def copy_into(self, sink: Any) -> tuple[str, int]: ...


@dataclass(frozen=True)
class ReworkDeltaBlob:
    """One stream-verified sealed sidecar blob: exact bytes at ``path``."""

    path: Path
    sha256: str
    size: int

    def copy_into(self, sink: Any) -> tuple[str, int]:
        with self.path.open("rb") as handle:
            return stream_sha256(handle, sink)


def _rework_blob_source(relative: str, content: Any) -> ReworkBlobSource:
    sha, size = getattr(content, "sha256", None), getattr(content, "size", None)
    if (
        not isinstance(sha, str)
        or not re.fullmatch(r"[0-9a-f]{64}", sha)
        or type(size) is not int
        or size < 0
        or not callable(getattr(content, "copy_into", None))
    ):
        raise WorkspaceError(f"rework_delta_content_invalid:{relative}")
    return content


def _link_like(info: os.stat_result) -> bool:
    """A symlink, or any Windows reparse point (junction, mount point)."""
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & reparse)


def _rework_blob_dir(artifact_dir: Path) -> Path:
    blob_dir = artifact_dir / REWORK_DELTA_BLOB_DIRNAME
    info = os.lstat(blob_dir)
    if _link_like(info) or not stat.S_ISDIR(info.st_mode):
        raise WorkspaceError("rework_delta_blob_dir_unsafe")
    return blob_dir


def _rework_delta_blob_present(artifact_dir: Path, blob_sha: str, size: int) -> bool:
    """Cheap retention-fence check: the blob exists, unlinked, at its size."""
    try:
        info = os.lstat(_rework_blob_dir(artifact_dir) / blob_sha)
    except (OSError, WorkspaceError):
        return False
    return not _link_like(info) and stat.S_ISREG(info.st_mode) and info.st_size == size


def rework_delta_blob_refs(encoded: bytes) -> dict[str, int]:
    """Name every sidecar blob one sealed delta JSON references (sha256 -> size).

    Raises ``ValueError``/``WorkspaceError`` for an unparsable artifact or a
    malformed reference, so a caller deciding retention can fail closed.
    """
    if b'"blob_sha256"' not in encoded:
        return {}
    packet = json.loads(encoded.decode("utf-8"))
    files = packet.get("files") if isinstance(packet, dict) else None
    if not isinstance(files, list):
        raise WorkspaceError("rework_delta_artifact_invalid")
    refs: dict[str, int] = {}
    for entry in files:
        if not isinstance(entry, dict) or "blob_sha256" not in entry:
            continue
        blob_sha, size = entry.get("blob_sha256"), entry.get("size")
        if (
            not isinstance(blob_sha, str)
            or not re.fullmatch(r"[0-9a-f]{64}", blob_sha)
            or type(size) is not int
            or size < 0
        ):
            raise WorkspaceError("rework_delta_blob_invalid")
        refs[blob_sha] = size
    return refs


def verified_rework_delta_blob(artifact_dir: Path, blob_sha: str, size: int) -> ReworkDeltaBlob:
    """Stream-verify one sealed blob beside its delta artifact; fail closed.

    The blob must sit directly in the resolved ``blobs`` directory (itself no
    link or junction), be a regular non-link file of exactly ``size`` bytes,
    and stream to exactly ``blob_sha``.
    """
    if not re.fullmatch(r"[0-9a-f]{64}", str(blob_sha)) or type(size) is not int or size < 0:
        raise WorkspaceError("rework_delta_blob_invalid")
    try:
        blob_dir = _rework_blob_dir(artifact_dir)
        path = blob_dir / blob_sha
        info = os.lstat(path)
        if (
            _link_like(info)
            or not stat.S_ISREG(info.st_mode)
            or path.resolve(strict=True).parent != blob_dir.resolve(strict=True)
        ):
            raise WorkspaceError(f"rework_delta_blob_unsafe:{blob_sha}")
        with path.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode) or opened.st_size != size:
                raise WorkspaceError(f"rework_delta_blob_size_mismatch:{blob_sha}")
            observed, observed_size = stream_sha256(handle)
    except OSError as exc:
        raise WorkspaceError(f"rework_delta_blob_missing:{blob_sha}") from exc
    if observed_size != size or observed != blob_sha:
        raise WorkspaceError(f"rework_delta_blob_hash_mismatch:{blob_sha}")
    return ReworkDeltaBlob(path, blob_sha, size)


def _seal_rework_delta_blob(artifact_dir: Path, source: ReworkBlobSource) -> None:
    """Stream one file into ``blobs/<sha256>``: temp, fsync, verify, rename.

    An intact blob with the same digest is reused (its mtime refreshed so the
    janitor's age fence restarts); an absent, tampered or unrefreshable one is
    replaced by a fresh verified copy.
    """
    blob_dir = artifact_dir / REWORK_DELTA_BLOB_DIRNAME
    blob_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    _rework_blob_dir(artifact_dir)
    target = blob_dir / source.sha256
    try:
        verified_rework_delta_blob(artifact_dir, source.sha256, source.size)
        os.utime(target)
        return
    except (OSError, WorkspaceError):
        pass
    fd, temp_name = tempfile.mkstemp(dir=blob_dir, prefix=".rework-blob-", suffix=".tmp")
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            digest, size = source.copy_into(handle)
            handle.flush()
            os.fsync(handle.fileno())
        if digest != source.sha256 or size != source.size:
            raise WorkspaceError(f"rework_delta_blob_source_changed:{source.sha256}")
        chmod_path(temp_path, 0o600)
        atomic_replace(temp_path, target)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def _write_rework_blob(destination: Path, relative: str, blob: ReworkDeltaBlob) -> None:
    """Stream-copy one verified blob into place via a sibling temp file."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.is_symlink() and destination.exists() and not destination.is_file():
        raise WorkspaceError(f"rework_delta_blob_destination_invalid:{relative}")
    mode = (
        stat.S_IMODE(destination.stat().st_mode)
        if destination.is_file() and not destination.is_symlink() else 0o644
    )
    fd, temp_name = tempfile.mkstemp(
        dir=destination.parent, prefix=".rework-blob-", suffix=".tmp"
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            digest, size = blob.copy_into(handle)
            handle.flush()
            os.fsync(handle.fileno())
        if digest != blob.sha256 or size != blob.size:
            raise WorkspaceError(f"rework_delta_content_hash_mismatch:{relative}")
        chmod_path(temp_path, mode)
        atomic_replace(temp_path, destination)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def _rework_hash_mismatch(
    relative: str, source: str, expected: Any, content: bytes | None,
) -> WorkspaceError:
    """Name WHICH read disagreed, keeping the typed reason prefix intact.

    NF-2026-01199: the retained worktree and the sealed artifact raised one
    indistinguishable reason.  ``terminal_failure_classification`` types only
    the ``rework_predecessor_hash_mismatch`` prefix, so the diagnosis rides
    immediately after ``:<relative>`` -- ahead of the ``[:300]``/``[:500]``
    bounds this reason passes through on its way to the card -- and a pure
    CR/LF difference in retained worktree bytes is named as exactly that.

    Field order keeps compact verdicts first and both 64-character digests
    last, so a long ``relative`` cannot push ``observed_bytes``/
    ``line_endings_only`` out of the ``[:300]`` card bound; ``expected`` is
    last since the card already publishes it separately as
    ``changed_path_hashes``.  It renders only when it is actually a
    64-character hex digest, so a forged string cannot inject fake tail
    fields into the reason.
    """
    observed = None if content is None else hashlib.sha256(content).hexdigest()
    digest = (
        isinstance(expected, str)
        and re.fullmatch(r"[0-9a-f]{64}", expected) is not None
    )
    tail = f" source={source}"
    if source == "worktree":
        unix = b"" if content is None else content.replace(b"\r\n", b"\n")
        only_endings = content is not None and digest and any(
            hashlib.sha256(variant).hexdigest() == expected
            for variant in (unix, unix.replace(b"\n", b"\r\n"))
        )
        tail += f" line_endings_only={'true' if only_endings else 'false'}"
    tail += f" observed_bytes={0 if content is None else len(content)}"
    tail += f" observed={observed or 'absent'}"
    if digest:
        tail += f" expected={expected}"
    elif isinstance(expected, str):
        tail += " expected=invalid"
    else:
        tail += " expected=absent"
    return WorkspaceError(f"rework_predecessor_hash_mismatch:{relative}{tail}")


def _materialize_rework_predecessor_from_worktree(
    worktree: Path,
    source_workspace: WorkerWorkspace,
    hashes: dict[str, Any],
    allowed_writes: tuple[str, ...],
    rebase: ReworkRebase | None = None,
) -> list[str]:
    """Copy the hash-pinned predecessor delta from the retained worktree."""

    planned: list[tuple[str, bytes | None]] = []
    for raw_relative, raw_expected in sorted(hashes.items()):
        relative = _relative_repo_path(str(raw_relative))
        if not _matches(relative, allowed_writes):
            raise WorkspaceError(f"rework_predecessor_outside_scope:{relative}")
        if raw_expected is not None and (
            not isinstance(raw_expected, str)
            or not re.fullmatch(r"[0-9a-f]{64}", raw_expected)
        ):
            raise WorkspaceError(f"rework_predecessor_hash_invalid:{relative}")
        source = source_workspace.path / relative
        _require_beneath(source_workspace.path, source)
        _require_beneath(worktree, worktree / relative)
        regular = source.is_file() and not source.is_symlink()
        content = source.read_bytes() if regular else None
        observed = None if content is None else hashlib.sha256(content).hexdigest()
        if observed != raw_expected:
            raise _rework_hash_mismatch(relative, "worktree", raw_expected, content)
        planned.append((relative, content if rebase is not None or content is None else b""))
    merged = {} if rebase is None else rebase(planned)
    for relative, content in planned:
        if content is not None:  # _copy_one carries the mode; merged bytes replace content.
            _copy_one(source_workspace.path / relative, worktree / relative)
        if content is None or relative in merged:
            _write_rework_path(worktree / relative, relative, merged.get(relative, content))
    return [relative for relative, _ in planned]


REWORK_DELTA_ARTIFACT_SCHEMA_ID = "aiworkhub.rework_delta_artifact.v2"


def _rework_delta_canonical_digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("utf-8")
    ).hexdigest()


def _rework_delta_normalize_path(raw_path: object) -> str:
    if not isinstance(raw_path, str) or not raw_path:
        raise WorkspaceError(f"rework_delta_invalid_path:{raw_path!r}")
    relative = _relative_repo_path(raw_path)
    posix = PurePosixPath(relative)
    if (
        not relative
        or posix.is_absolute()
        or any(part in {"", ".", ".."} for part in posix.parts)
        or posix.parts[0] == ".git"
    ):
        raise WorkspaceError(f"rework_delta_invalid_path:{raw_path!r}")
    return relative


def seal_rework_delta_artifact(
    authority_repo: Path,
    task_id: str,
    request_id: str,
    claim_epoch: int,
    file_entries: Iterable[tuple[str, bytes | ReworkBlobSource | None]],
    artifact_dir: Path,
) -> dict[str, str]:
    """Atomically seal exact predecessor bytes as one bounded delta artifact.

    ``bytes`` are base64-inlined under ``MAX_REWORK_OVERLAY_CONTENT_BYTES``.
    A ``ReworkBlobSource`` (an over-inline-cap file) is streamed into the
    sidecar ``blobs/<sha256>`` beside the artifact BEFORE the artifact is
    published, and its entry carries ``blob_sha256`` + ``size`` instead.
    """
    if not _REQUEST_ID_RE.fullmatch(request_id):
        raise WorkspaceError(f"rework_delta_invalid_request_id:{request_id!r}")
    if not task_id or type(claim_epoch) is not int or claim_epoch < 1:
        raise WorkspaceError("rework_delta_identity_missing")
    entries = list(file_entries)
    if not entries:
        raise WorkspaceError("rework_delta_artifact_empty")
    if len(entries) > MAX_REWORK_OVERLAY_FILES:
        raise WorkspaceError("rework_delta_file_count_exceeds_limit")
    files: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    total_content_bytes = 0
    carried_bytes = 0
    blob_sources: list[ReworkBlobSource] = []
    for raw_path, content in entries:
        relative = _rework_delta_normalize_path(raw_path)
        if relative in seen_paths:
            raise WorkspaceError(f"rework_delta_duplicate_path:{relative}")
        seen_paths.add(relative)
        entry: dict[str, Any] = {"path": relative}
        if content is None:
            entry["deleted"] = True
        elif isinstance(content, (bytes, bytearray)):
            total_content_bytes += len(content)
            if total_content_bytes > MAX_REWORK_OVERLAY_CONTENT_BYTES:
                raise WorkspaceError("rework_delta_content_exceeds_limit")
            entry["sha256"] = hashlib.sha256(content).hexdigest()
            entry["content_base64"] = base64.b64encode(content).decode("ascii")
        else:
            source = _rework_blob_source(relative, content)
            carried_bytes += source.size
            if carried_bytes > MAX_REWORK_CARRIED_BYTES:
                raise WorkspaceError(
                    f"rework_delta_carried_bytes_exceeds_limit:{carried_bytes}"
                    f">{MAX_REWORK_CARRIED_BYTES}"
                )
            entry.update(
                {"sha256": source.sha256, "blob_sha256": source.sha256, "size": source.size}
            )
            blob_sources.append(source)
        files.append(entry)
    files.sort(key=lambda item: item["path"])
    payload = {
        "schema_id": REWORK_DELTA_ARTIFACT_SCHEMA_ID,
        "authority_repo": str(authority_repo.resolve(strict=False)),
        "task_id": task_id,
        "request_id": request_id,
        "claim_epoch": claim_epoch,
        "files": files,
    }
    packet = {**payload, "canonical_digest": _rework_delta_canonical_digest(payload)}
    encoded = json.dumps(packet, indent=2, ensure_ascii=True).encode("utf-8")
    artifact_digest = hashlib.sha256(encoded).hexdigest()
    artifact_dir = artifact_dir.resolve(strict=False)
    artifact_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    chmod_path(artifact_dir, 0o700)
    for source in blob_sources:
        _seal_rework_delta_blob(artifact_dir, source)
    artifact_path = artifact_dir / f"{artifact_digest}.json"
    fd, temp_name = tempfile.mkstemp(
        dir=artifact_dir, prefix=".rework-delta-", suffix=".tmp"
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        chmod_path(temp_path, 0o600)
        atomic_replace(temp_path, artifact_path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise
    return {"path": str(artifact_path), "digest": artifact_digest}


def validated_rework_delta(
    raw: Any,
    *,
    authority_repo: Path,
    task_id: str,
    expected_request_id: str,
    expected_claim_epoch: int | None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Authenticate one ``rework_delta`` descriptor: ``(descriptor, None)``.

    ``reject_review`` pins a predecessor's sealed delta only through this
    check, and validation-only replay recovery rebuilds a collected
    predecessor only behind it (NF-2026-01655).  ``(None, None)`` means no
    delta is claimed; a malformed, misidentified, misplaced, oversized,
    unreadable or tampered descriptor returns its typed
    ``rework_delta_descriptor_*`` reason.
    """
    if raw is None:
        return None, None
    if not isinstance(raw, dict) or set(raw) != {
        "schema_id",
        "sealed",
        "authority_repo",
        "task_id",
        "request_id",
        "claim_epoch",
        "artifact_path",
        "artifact_sha256",
    }:
        return None, "rework_delta_descriptor_invalid"
    claim_epoch = raw.get("claim_epoch")
    digest = raw.get("artifact_sha256")
    if (
        raw.get("schema_id") != "aiworkhub.rework_delta_descriptor.v1"
        or raw.get("sealed") is not True
        or str(raw.get("authority_repo") or "")
        != str(Path(authority_repo).resolve(strict=False))
        or str(raw.get("task_id") or "") != task_id
        or str(raw.get("request_id") or "") != expected_request_id
        or type(claim_epoch) is not int
        or claim_epoch < 1
        or (
            expected_claim_epoch is not None
            and claim_epoch != expected_claim_epoch
        )
        or not isinstance(digest, str)
        or len(digest) != 64
        or any(ch not in "0123456789abcdef" for ch in digest)
    ):
        return None, "rework_delta_descriptor_identity_mismatch"
    artifact_path = Path(str(raw.get("artifact_path") or ""))
    artifact_root = (configured_runtime_root(authority_repo) / "rework_deltas").resolve(
        strict=False
    )
    resolved_artifact = artifact_path.resolve(strict=False)
    if resolved_artifact.parent != artifact_root or not resolved_artifact.is_file():
        return None, "rework_delta_descriptor_path_invalid"
    try:
        if resolved_artifact.stat().st_size > 64 * 1024 * 1024:
            return None, "rework_delta_descriptor_too_large"
        observed_digest = hashlib.sha256(resolved_artifact.read_bytes()).hexdigest()
    except OSError:
        return None, "rework_delta_descriptor_unreadable"
    if observed_digest != digest:
        return None, "rework_delta_descriptor_tampered"
    return dict(raw), None


def has_verified_rework_delta(
    predecessor: Any,
    *,
    authority_repo: Path,
) -> bool:
    """Return whether a predecessor has a durable, identity-bound delta.

    This is the retention fence shared by process GC and the storage planner.
    A worktree may be released only after the exact descriptor projected by
    ``reject_review`` still names an intact content-addressed artifact beneath
    this repository's runtime root.  Any malformed, missing, moved, symlinked,
    oversized, or tampered artifact fails closed and keeps the worktree pinned.
    """
    if not isinstance(predecessor, dict):
        return False
    descriptor = predecessor.get("rework_delta")
    artifact = predecessor.get("delta_artifact")
    if (
        not isinstance(descriptor, dict)
        or set(descriptor) != {
            "schema_id",
            "sealed",
            "authority_repo",
            "task_id",
            "request_id",
            "claim_epoch",
            "artifact_path",
            "artifact_sha256",
        }
        or not isinstance(artifact, dict)
        or set(artifact) != {"path", "digest"}
    ):
        return False
    authority_repo = authority_repo.resolve(strict=False)
    request_id = str(predecessor.get("request_id") or "").strip()
    task_id = str(predecessor.get("task_id") or "").strip()
    claim_epoch = predecessor.get("claim_epoch")
    digest = descriptor.get("artifact_sha256")
    raw_path = descriptor.get("artifact_path")
    if (
        descriptor.get("schema_id") != "aiworkhub.rework_delta_descriptor.v1"
        or descriptor.get("sealed") is not True
        or str(descriptor.get("authority_repo") or "") != str(authority_repo)
        or str(descriptor.get("request_id") or "") != request_id
        or str(descriptor.get("task_id") or "") != task_id
        or type(claim_epoch) is not int
        or claim_epoch < 1
        or descriptor.get("claim_epoch") != claim_epoch
        or not _REQUEST_ID_RE.fullmatch(request_id)
        or not task_id
        or not isinstance(digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", digest)
        or artifact != {"path": raw_path, "digest": digest}
    ):
        return False
    artifact_path = Path(str(raw_path or ""))
    artifact_root = (configured_runtime_root(authority_repo) / "rework_deltas").resolve(
        strict=False
    )
    resolved = artifact_path.resolve(strict=False)
    if (
        resolved.parent != artifact_root
        or resolved.name != f"{digest}.json"
        or artifact_path.is_symlink()
        or not resolved.is_file()
    ):
        return False
    try:
        if resolved.stat().st_size > 64 * 1024 * 1024:
            return False
        encoded = resolved.read_bytes()
        if hashlib.sha256(encoded).hexdigest() != digest:
            return False
        # The worktree may be released only when every sidecar blob the delta
        # names is present at its sealed size; materialization re-hashes it.
        return all(
            _rework_delta_blob_present(resolved.parent, blob_sha, size)
            for blob_sha, size in rework_delta_blob_refs(encoded).items()
        )
    except (OSError, ValueError, WorkspaceError):
        return False


def verified_rework_delta_file_hashes(
    predecessor: Any,
    *,
    authority_repo: Path,
) -> dict[str, str] | None:
    """Return sealed predecessor path digests, or None when none is claimed.

    A predecessor that does not claim a sealed delta returns ``None`` so the
    existing inherited-rework carve-out still applies. A claimed but
    unverified, identity-mismatched, or unreadable artifact returns an empty
    mapping so inheritance fails closed.
    """
    if not isinstance(predecessor, dict):
        return None
    if predecessor.get("rework_delta") is None and predecessor.get("delta_artifact") is None:
        return None
    if not has_verified_rework_delta(predecessor, authority_repo=authority_repo):
        return {}
    artifact_path = Path(str((predecessor.get("delta_artifact") or {}).get("path") or ""))
    try:
        packet = json.loads(artifact_path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return {}
    if not isinstance(packet, dict):
        return {}
    canonical_digest = str(packet.pop("canonical_digest", "") or "")
    if (
        packet.get("schema_id") != REWORK_DELTA_ARTIFACT_SCHEMA_ID
        or not re.fullmatch(r"[0-9a-f]{64}", canonical_digest)
        or _rework_delta_canonical_digest(packet) != canonical_digest
        or str(packet.get("authority_repo")) != str(authority_repo.resolve(strict=False))
        or str(packet.get("request_id")) != str(predecessor.get("request_id") or "").strip()
        or str(packet.get("task_id")) != str(predecessor.get("task_id") or "").strip()
        or packet.get("claim_epoch") != predecessor.get("claim_epoch")
    ):
        return {}
    files = packet.get("files")
    if not isinstance(files, list) or not files:
        return {}
    hashes: dict[str, str] = {}
    for entry in files:
        if not isinstance(entry, dict):
            return {}
        try:
            relative = _rework_delta_normalize_path(entry.get("path"))
        except WorkspaceError:
            return {}
        if entry.get("deleted") is True:
            continue
        file_sha = str(entry.get("sha256") or "")
        if not re.fullmatch(r"[0-9a-f]{64}", file_sha):
            return {}
        hashes[relative] = file_sha
    return hashes


def verify_rework_delta_artifact(
    artifact: Any,
    authority_repo: Path,
    request_id: str,
    task_id: str,
    claim_epoch: int,
    expected_path_hashes: dict[str, Any],
    allowed_writes: tuple[str, ...],
    *,
    worktree: Path | None = None,
) -> list[tuple[str, bytes | ReworkDeltaBlob | None]]:
    """Verify one sealed changed/deleted-file delta and return its exact plan.

    Materialization passes its destination ``worktree`` so every planned path
    is also proven beneath it.  Blocked-rework recovery omits it to
    authenticate a collected candidate with this same verifier, writing
    nothing.  A blob-carried entry is planned as a ``ReworkDeltaBlob`` whose
    sidecar was stream-verified (location, link, type, size and sha256).
    """
    if not isinstance(artifact, dict):
        raise WorkspaceError("rework_delta_artifact_invalid")
    raw_path = str(artifact.get("path") or "")
    digest = str(artifact.get("digest") or "")
    if not raw_path or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise WorkspaceError("rework_delta_artifact_invalid")
    artifact_path = Path(raw_path)
    if (
        artifact_path.name != f"{digest}.json"
        or artifact_path.is_symlink()
        or not artifact_path.is_file()
    ):
        raise WorkspaceError("rework_delta_artifact_missing")
    encoded = artifact_path.read_bytes()
    if hashlib.sha256(encoded).hexdigest() != digest:
        raise WorkspaceError("rework_delta_artifact_tampered")
    try:
        packet = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise WorkspaceError("rework_delta_artifact_invalid") from exc
    if not isinstance(packet, dict):
        raise WorkspaceError("rework_delta_artifact_invalid")
    canonical_digest = str(packet.pop("canonical_digest", "") or "")
    if (
        packet.get("schema_id") != REWORK_DELTA_ARTIFACT_SCHEMA_ID
        or not re.fullmatch(r"[0-9a-f]{64}", canonical_digest)
        or _rework_delta_canonical_digest(packet) != canonical_digest
    ):
        raise WorkspaceError("rework_delta_artifact_tampered")
    if (
        str(packet.get("authority_repo")) != str(authority_repo.resolve(strict=False))
        or str(packet.get("request_id")) != request_id
        or str(packet.get("task_id")) != task_id
        or type(packet.get("claim_epoch")) is not int
        or packet.get("claim_epoch") != claim_epoch
    ):
        raise WorkspaceError("rework_delta_identity_mismatch")
    files = packet.get("files")
    if not isinstance(files, list) or not files or len(files) > MAX_REWORK_OVERLAY_FILES:
        raise WorkspaceError("rework_delta_artifact_incomplete")
    expected = {
        _relative_repo_path(str(key)): value
        for key, value in expected_path_hashes.items()
    }
    planned: list[tuple[str, bytes | ReworkDeltaBlob | None]] = []
    seen_paths: set[str] = set()
    total_content_bytes = 0
    carried_bytes = 0
    # Sidecar blobs live beside the very artifact whose digest was checked.
    artifact_dir = artifact_path.resolve(strict=True).parent
    for entry in files:
        if not isinstance(entry, dict):
            raise WorkspaceError("rework_delta_artifact_invalid")
        relative = _rework_delta_normalize_path(entry.get("path"))
        if relative in seen_paths:
            raise WorkspaceError(f"rework_delta_duplicate_path:{relative}")
        seen_paths.add(relative)
        if relative not in expected:
            raise WorkspaceError(f"rework_delta_unexpected_path:{relative}")
        raw_expected = expected[relative]
        if raw_expected is not None and (
            not isinstance(raw_expected, str)
            or not re.fullmatch(r"[0-9a-f]{64}", raw_expected)
        ):
            raise WorkspaceError(f"rework_predecessor_hash_invalid:{relative}")
        if not _matches(relative, allowed_writes):
            raise WorkspaceError(f"rework_predecessor_outside_scope:{relative}")
        if worktree is not None:
            _require_beneath(worktree, worktree / relative)
        if entry.get("deleted") is True:
            if (
                raw_expected is not None
                or entry.get("sha256") is not None
                or entry.get("content_base64") is not None
                or entry.get("blob_sha256") is not None
            ):
                raise WorkspaceError(f"rework_delta_delete_conflict:{relative}")
            planned.append((relative, None))
            continue
        file_sha = str(entry.get("sha256") or "")
        content_base64 = entry.get("content_base64")
        if "blob_sha256" in entry:
            size = entry.get("size")
            if (
                not re.fullmatch(r"[0-9a-f]{64}", file_sha)
                or entry.get("blob_sha256") != file_sha
                or content_base64 is not None
                or type(size) is not int
                or size < 0
            ):
                raise WorkspaceError(f"rework_delta_artifact_incomplete:{relative}")
            if file_sha != raw_expected:
                raise WorkspaceError(
                    f"rework_predecessor_hash_mismatch:{relative} source=artifact"
                    f" observed_bytes={size} observed={file_sha}"
                )
            carried_bytes += size
            if carried_bytes > MAX_REWORK_CARRIED_BYTES:
                raise WorkspaceError("rework_delta_carried_bytes_exceeds_limit")
            planned.append(
                (relative, verified_rework_delta_blob(artifact_dir, file_sha, size))
            )
            continue
        if not re.fullmatch(r"[0-9a-f]{64}", file_sha) or not isinstance(
            content_base64, str
        ):
            raise WorkspaceError(f"rework_delta_artifact_incomplete:{relative}")
        try:
            content = base64.b64decode(content_base64, validate=True)
        except (ValueError, TypeError) as exc:
            raise WorkspaceError(f"rework_delta_artifact_invalid:{relative}") from exc
        if hashlib.sha256(content).hexdigest() != file_sha:
            raise WorkspaceError(f"rework_delta_content_hash_mismatch:{relative}")
        if file_sha != raw_expected:
            raise _rework_hash_mismatch(relative, "artifact", raw_expected, content)
        total_content_bytes += len(content)
        if total_content_bytes > MAX_REWORK_OVERLAY_CONTENT_BYTES:
            raise WorkspaceError("rework_delta_content_exceeds_limit")
        planned.append((relative, content))
    if seen_paths != set(expected):
        raise WorkspaceError("rework_delta_artifact_incomplete")
    return planned


def materialize_rework_delta_artifact(
    artifact: Any,
    authority_repo: Path,
    request_id: str,
    task_id: str,
    claim_epoch: int,
    worktree: Path,
    expected_path_hashes: dict[str, Any],
    allowed_writes: tuple[str, ...],
    rebase: ReworkRebase | None = None,
) -> list[str]:
    """Verify and materialize one sealed changed/deleted-file delta.

    Blob-carried paths are stream-copied from their verified sidecar blob and
    never handed to ``rebase``; base drift on one fails closed instead.
    """
    planned = verify_rework_delta_artifact(
        artifact,
        authority_repo,
        request_id,
        task_id,
        claim_epoch,
        expected_path_hashes,
        allowed_writes,
        worktree=worktree,
    )
    blobs = {
        relative: content for relative, content in planned
        if isinstance(content, ReworkDeltaBlob)
    }
    inline = [(relative, content) for relative, content in planned if relative not in blobs]
    merged = {} if rebase is None else rebase(inline)
    if rebase is not None:
        _refuse_blob_rebase(rebase, sorted(blobs))
    for relative, content in planned:
        if relative in blobs:
            _write_rework_blob(worktree / relative, relative, blobs[relative])
            continue
        content = merged.get(relative, content)
        _write_rework_path(worktree / relative, relative, content)
    return sorted(relative for relative, _ in planned)


def _refuse_blob_rebase(rebase: ReworkRebase, blob_paths: list[str]) -> None:
    """Fail closed when base drift would have to merge a blob-carried path.

    The three-way merge works on whole in-memory bytes, so a sidecar blob is
    never merged.  Each blob path is probed alone as a byte-free deletion
    marker: the rebase returns (or conflicts on) exactly the drifted paths.
    """
    for relative in blob_paths:
        try:
            merged = rebase([(relative, None)])
        except WorkspaceError as exc:
            if not str(exc).startswith("rework_base_drift:"):
                raise
            merged = {relative: None}
        if relative in merged:
            raise WorkspaceError(f"rework_delta_blob_rebase_conflict:{relative}")


# Import-cycle resolution: ``WorkspaceError`` (and the helpers below) must stay
# defined in ``worker_workspace`` so every ``except``/``isinstance`` keeps
# matching one class object.  Nothing above needs them at definition time, only
# when a function runs, so this import sits LAST: whichever module is imported
# first has finished every definition before the other one reads its names.
from .worker_workspace import (  # noqa: E402
    MAX_REWORK_CARRIED_BYTES,
    MAX_REWORK_OVERLAY_CONTENT_BYTES,
    MAX_REWORK_OVERLAY_FILES,
    REWORK_DELTA_BLOB_DIRNAME,
    WorkspaceError,
    _REQUEST_ID_RE,
    _copy_one,
    _matches,
    _relative_repo_path,
    _require_beneath,
    atomic_replace,
    chmod_path,
    configured_runtime_root,
)