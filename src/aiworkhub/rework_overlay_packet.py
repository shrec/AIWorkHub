"""Build and seal retained rework packets for a successor worker request."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any


def materialize_rework_overlay(
    successor_request_id: str,
    successor_task_id: str,
    predecessor_request_id: str,
    predecessor_task_id: str,
    authority_repo: Path,
    file_entries: list[tuple[str, str | None, bytes | None]],
    *,
    request_id_pattern: re.Pattern[str],
    max_files: int,
    max_content_bytes: int,
) -> bytes:
    """Emit a bounded, canonical-digest-bound retained-rework overlay.

    Each file entry is (repo_relative_path, sha256_or_None, content_bytes_or_None).
    sha256=None means delete; content=None with sha256 indicates a hash-only reference.
    Returns JSON bytes with a deterministic canonical_digest over the sorted files payload.
    """
    if not request_id_pattern.fullmatch(successor_request_id):
        raise ValueError(f"invalid successor_request_id: {successor_request_id!r}")
    if not request_id_pattern.fullmatch(predecessor_request_id):
        raise ValueError(f"invalid predecessor_request_id: {predecessor_request_id!r}")
    if successor_request_id == predecessor_request_id:
        raise ValueError("successor and predecessor request_ids must be distinct")
    # Rework intentionally reuses the canonical task ID.  The immutable claim
    # attempt is identified by a new request ID, so requiring a distinct task
    # ID made the packet impossible to wire into the real recovery path.
    if not successor_task_id or not predecessor_task_id:
        raise ValueError("successor and predecessor task_ids are required")
    authority_repo = authority_repo.resolve()
    if not authority_repo.is_dir():
        raise FileNotFoundError(f"authority_repo not found: {authority_repo}")
    if len(file_entries) > max_files:
        raise ValueError("rework overlay file count exceeds limit")
    normalized_files: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    total_content_bytes = 0
    for rel_path, file_sha, content in file_entries:
        normalized_path = PurePosixPath(str(rel_path)).as_posix()
        if (
            not rel_path
            or "\\" in str(rel_path)
            or PurePosixPath(str(rel_path)).is_absolute()
            or any(part in {"", ".", ".."} for part in PurePosixPath(str(rel_path)).parts)
        ):
            raise ValueError(f"invalid repo-relative path: {rel_path!r}")
        if normalized_path in seen_paths:
            raise ValueError(f"duplicate rework overlay path: {normalized_path}")
        seen_paths.add(normalized_path)
        entry: dict[str, Any] = {"path": normalized_path}
        if file_sha is None:
            if content is not None:
                raise ValueError(f"deleted overlay path carries content: {normalized_path}")
            entry["deleted"] = True
        else:
            if not re.fullmatch(r"[0-9a-f]{64}", str(file_sha)):
                raise ValueError(f"invalid rework overlay hash: {normalized_path}")
            entry["sha256"] = file_sha
        if content is not None:
            if hashlib.sha256(content).hexdigest() != file_sha:
                raise ValueError(f"rework overlay content hash mismatch: {normalized_path}")
            total_content_bytes += len(content)
            if total_content_bytes > max_content_bytes:
                raise ValueError("rework overlay content exceeds limit")
            entry["content_base64"] = base64.b64encode(content).decode("ascii")
        normalized_files.append(entry)
    normalized_files.sort(key=lambda e: e["path"])
    payload = {
        "successor_request_id": successor_request_id,
        "successor_task_id": successor_task_id,
        "predecessor_request_id": predecessor_request_id,
        "predecessor_task_id": predecessor_task_id,
        "authority_repo": str(authority_repo),
        "files": normalized_files,
    }
    payload_digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("utf-8")
    ).hexdigest()
    packet = {
        **payload,
        "canonical_digest": payload_digest,
    }
    return json.dumps(packet, indent=2, ensure_ascii=True).encode("utf-8")


def materialize_worker_rework_overlay(
    workspace: Any,
    task_id: str,
    card: Mapping[str, Any],
    materialize: Callable[..., bytes],
    writer: Callable[[Path, dict[str, Any]], None],
    error_type: type[Exception],
) -> tuple[Path | None, dict[str, Any] | None]:
    """Seal verified inherited predecessor bytes into request-private HOME."""
    if not workspace.inherited_rework_paths:
        return None, None
    predecessor = card.get("rework_predecessor")
    if not isinstance(predecessor, Mapping):
        raise error_type("rework_overlay_predecessor_missing")
    predecessor_request_id = str(predecessor.get("request_id") or "").strip()
    predecessor_task_id = str(predecessor.get("task_id") or task_id).strip()
    hashes = predecessor.get("changed_path_hashes")
    if not predecessor_request_id or not predecessor_task_id or not isinstance(hashes, Mapping):
        raise error_type("rework_overlay_predecessor_invalid")

    entries: list[tuple[str, str | None, bytes | None]] = []
    for relative in workspace.inherited_rework_paths:
        if relative not in hashes:
            raise error_type(f"rework_overlay_hash_missing:{relative}")
        expected = hashes.get(relative)
        candidate = workspace.path / relative
        if expected is None:
            if candidate.exists() or candidate.is_symlink():
                raise error_type(f"rework_overlay_deleted_path_present:{relative}")
            entries.append((relative, None, None))
            continue
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise error_type(f"rework_overlay_hash_invalid:{relative}")
        if candidate.is_symlink() or not candidate.is_file():
            raise error_type(f"rework_overlay_file_missing:{relative}")
        content = candidate.read_bytes()
        actual = hashlib.sha256(content).hexdigest()
        # NF-2026-01113: rebase merges bytes; baseline pins "file:<mode>:<sha256>".
        rebased = str((workspace.workspace_baseline or {}).get(relative) or "")
        if actual != expected and not re.fullmatch(rf"file:[0-7]+:{actual}", rebased):
            raise error_type(f"rework_overlay_hash_mismatch:{relative}")
        entries.append((relative, actual, content))

    try:
        packet_bytes = materialize(
            workspace.request_id,
            task_id,
            predecessor_request_id,
            predecessor_task_id,
            workspace.repo,
            entries,
        )
        packet = json.loads(packet_bytes.decode("utf-8"))
    except (ValueError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise error_type(f"rework_overlay_materialization_failed:{exc}") from exc
    path = workspace.home / "task_mcp_worker_runtime" / "rework_overlay.json"
    writer(path, packet)
    return path, packet
