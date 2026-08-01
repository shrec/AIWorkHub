"""Repository-scoped content-addressed cache for trusted context bundles.

This module is intentionally isolated from project_context/process_launcher.
It stores only immutable, coordinator-authorized context evidence and reuse
metadata.  It does not claim provider prompt-cache billing savings.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


KEY_SCHEMA_ID = "aiworkhub.task_mcp.context_cache_key.v1"
ENTRY_SCHEMA_ID = "aiworkhub.task_mcp.context_cache_entry.v1"
REUSE_SCHEMA_ID = "aiworkhub.task_mcp.context_cache_reuse.v1"
QUERY_REUSE_SCHEMA_ID = "aiworkhub.task_mcp.context_query_reuse_key.v1"
EMPTY_INPUT_SHARD_SHA256 = hashlib.sha256(b"").hexdigest()
MAX_POLICY_BYTES = 16 * 1024
MAX_METADATA_BYTES = 8 * 1024
DEFAULT_MAX_ENTRY_BYTES = 256 * 1024
DEFAULT_MAX_TOTAL_BYTES = 32 * 1024 * 1024
DEFAULT_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
OUTCOME_HIT = "hit"
OUTCOME_MISS = "miss"
OUTCOME_STALE = "stale"
OUTCOME_CORRUPT = "corrupt"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_REPO_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SENSITIVE = re.compile(
    r"(authorization|api[_-]?key|credential|password|secret|token)", re.IGNORECASE
)
_FORBIDDEN_STRUCTURED_FIELDS = {
    "claimed_by",
    "coordinator_claim",
    "lifecycle_state",
    "owner_prompt",
    "prompt",
    "prompt_text",
    "review_requested_by",
    "stderr",
    "stdout",
    "task_id",
    "worker_output",
    "worker_status",
}


class ContextCacheError(RuntimeError):
    """Unsafe cache configuration, key material, or payload."""


@dataclass(frozen=True, slots=True)
class ContextCacheKey:
    repo_id: str
    source_revision: str
    context_policy: dict[str, Any]
    tool_schema_id: str
    tool_version: str
    immutable_input_shard_sha256: str = EMPTY_INPUT_SHARD_SHA256


@dataclass(frozen=True, slots=True)
class ContextQueryKey:
    repo_id: str
    query: str
    source: str
    source_generation: str
    structural_sha256: str = EMPTY_INPUT_SHARD_SHA256


@dataclass(frozen=True, slots=True)
class CacheReadResult:
    outcome: str
    key_sha256: str
    payload: str = ""
    payload_sha256: str = ""
    metadata: dict[str, Any] | None = None
    reason: str = ""


@dataclass(frozen=True, slots=True)
class CacheWriteResult:
    stored: bool
    key_sha256: str
    payload_sha256: str
    bytes_written: int
    evicted_entries: int


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_json(value: Any, *, max_bytes: int, field: str) -> str:
    _reject_sensitive(value, field)
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ContextCacheError(f"{field}_not_canonical_json") from exc
    size = len(encoded.encode("utf-8"))
    if size > max_bytes:
        raise ContextCacheError(f"{field}_too_large:{size}>{max_bytes}")
    return encoded


def _reject_sensitive(value: Any, field: str) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key)
            if _SENSITIVE.search(key_text):
                raise ContextCacheError(f"{field}_contains_sensitive_field:{key}")
            if key_text.lower() in _FORBIDDEN_STRUCTURED_FIELDS:
                raise ContextCacheError(f"{field}_contains_forbidden_field:{key}")
            _reject_sensitive(item, field)
    elif isinstance(value, list):
        for item in value:
            _reject_sensitive(item, field)


def _reject_sensitive_payload(payload: str) -> None:
    start_candidates = [idx for idx in (payload.find("{"), payload.find("[")) if idx >= 0]
    if not start_candidates:
        return
    start = min(start_candidates)
    try:
        value = json.loads(payload[start:])
    except json.JSONDecodeError:
        return
    _reject_sensitive(value, "payload")


def _require_text(value: str, field: str, *, max_bytes: int = 512) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContextCacheError(f"{field}_required")
    if "\x00" in value:
        raise ContextCacheError(f"{field}_contains_nul")
    cleaned = value.strip()
    if len(cleaned.encode("utf-8")) > max_bytes:
        raise ContextCacheError(f"{field}_too_large")
    return cleaned


def _require_repo_id(repo_id: str) -> str:
    cleaned = _require_text(repo_id, "repo_id", max_bytes=128)
    if not _REPO_ID.fullmatch(cleaned) or "/" in cleaned or "\\" in cleaned or ".." in cleaned:
        raise ContextCacheError("repo_id_invalid")
    return cleaned


def _require_sha256(value: str, field: str) -> str:
    cleaned = _require_text(value, field, max_bytes=64).lower()
    if not _HEX64.fullmatch(cleaned):
        raise ContextCacheError(f"{field}_must_be_sha256")
    return cleaned


def _chmod_owner_only(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except PermissionError:
        pass
    # Windows' stat().st_mode exposes DOS read/write bits, not the inherited
    # DACL that enforces per-user access.  Treating 0666/0777 as POSIX
    # group/world permissions makes every cache path unusable on Windows.
    if os.name == "nt":
        return
    try:
        current = stat.S_IMODE(path.stat().st_mode)
    except OSError as exc:
        raise ContextCacheError(f"cache_permission_stat_failed:{exc}") from exc
    if current & 0o077:
        raise ContextCacheError(f"cache_path_permissions_too_open:{path}")


def key_material(key: ContextCacheKey) -> dict[str, Any]:
    repo_id = _require_repo_id(key.repo_id)
    source_revision = _require_text(key.source_revision, "source_revision")
    tool_schema_id = _require_text(key.tool_schema_id, "tool_schema_id")
    tool_version = _require_text(key.tool_version, "tool_version")
    shard_sha = _require_sha256(
        key.immutable_input_shard_sha256, "immutable_input_shard_sha256"
    )
    policy_json = _canonical_json(
        key.context_policy, max_bytes=MAX_POLICY_BYTES, field="context_policy"
    )
    return {
        "schema_id": KEY_SCHEMA_ID,
        "repo_id": repo_id,
        "source_revision": source_revision,
        "context_policy": json.loads(policy_json),
        "context_policy_sha256": _sha256_bytes(policy_json.encode("utf-8")),
        "tool_schema_id": tool_schema_id,
        "tool_version": tool_version,
        "immutable_input_shard_sha256": shard_sha,
    }


def cache_key_sha256(key: ContextCacheKey) -> str:
    material = key_material(key)
    encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _sha256_bytes(encoded.encode("utf-8"))


def query_key_material(key: ContextQueryKey) -> dict[str, Any]:
    """Material for repo+query+source-generation reuse.

    ``source_generation`` must change whenever the relevant repository
    structure changes. ``structural_sha256`` gives callers an exact invalidator
    for known target/baseline manifests without claiming provider cache savings.
    """

    repo_id = _require_repo_id(key.repo_id)
    query = _require_text(key.query, "query", max_bytes=512)
    source = _require_text(key.source, "source", max_bytes=64)
    source_generation = _require_text(
        key.source_generation, "source_generation", max_bytes=128
    )
    structural_sha256 = _require_sha256(key.structural_sha256, "structural_sha256")
    return {
        "schema_id": QUERY_REUSE_SCHEMA_ID,
        "repo_id": repo_id,
        "query": query,
        "query_sha256": _sha256_bytes(query.encode("utf-8")),
        "source": source,
        "source_generation": source_generation,
        "structural_sha256": structural_sha256,
    }


def query_cache_key_sha256(key: ContextQueryKey) -> str:
    material = query_key_material(key)
    encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _sha256_bytes(encoded.encode("utf-8"))


def reuse_metadata(*, cache_key: str, outcome: str) -> dict[str, Any]:
    """Return local-reuse metadata without provider billing claims."""

    return {
        "schema_id": REUSE_SCHEMA_ID,
        "cache_key_sha256": _require_sha256(cache_key, "cache_key_sha256"),
        "outcome": outcome,
        "local_recomputation_avoided": outcome == OUTCOME_HIT,
        "future_exact_bundle_reuse_candidate": outcome == OUTCOME_HIT,
        "provider_prompt_cache_billing_savings_claimed": False,
    }


class ContextCache:
    def __init__(
        self,
        *,
        cache_root: Path,
        repo_id: str,
        max_entry_bytes: int = DEFAULT_MAX_ENTRY_BYTES,
        max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
        max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
    ) -> None:
        self.repo_id = _require_repo_id(repo_id)
        self.root = self._prepare_root(cache_root)
        self.repo_root = self._prepare_repo_root(self.root / self.repo_id)
        self.max_entry_bytes = max(1, int(max_entry_bytes))
        self.max_total_bytes = max(self.max_entry_bytes, int(max_total_bytes))
        self.max_age_seconds = max(1, int(max_age_seconds))

    @staticmethod
    def _prepare_root(cache_root: Path) -> Path:
        if cache_root is None:
            raise ContextCacheError("cache_root_required")
        root = Path(cache_root)
        if root.exists() and root.lstat().st_mode & 0o170000 == 0o120000:
            raise ContextCacheError("cache_root_symlink")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        resolved = root.resolve(strict=True)
        if not resolved.is_dir() or resolved.is_symlink():
            raise ContextCacheError("cache_root_invalid")
        _chmod_owner_only(resolved, 0o700)
        return resolved

    @staticmethod
    def _prepare_repo_root(path: Path) -> Path:
        if path.exists() and path.lstat().st_mode & 0o170000 == 0o120000:
            raise ContextCacheError("repo_cache_root_symlink")
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        resolved = path.resolve(strict=True)
        if resolved.is_symlink() or not resolved.is_dir():
            raise ContextCacheError("repo_cache_root_invalid")
        _chmod_owner_only(resolved, 0o700)
        return resolved

    def _entry_path(self, key_sha: str) -> Path:
        key_sha = _require_sha256(key_sha, "cache_key_sha256")
        directory = self.repo_root / key_sha[:2]
        if directory.exists() and directory.lstat().st_mode & 0o170000 == 0o120000:
            raise ContextCacheError("cache_bucket_symlink")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        resolved_dir = directory.resolve(strict=True)
        if self.repo_root not in resolved_dir.parents and resolved_dir != self.repo_root:
            raise ContextCacheError("cache_path_escape")
        return resolved_dir / f"{key_sha}.json"

    def read(self, key: ContextCacheKey) -> CacheReadResult:
        material = key_material(key)
        key_sha = cache_key_sha256(key)
        path = self._entry_path(key_sha)
        if path.is_symlink():
            self._unlink_regular(path)
            return CacheReadResult(
                outcome=OUTCOME_CORRUPT, key_sha256=key_sha, reason="entry_symlink"
            )
        if not path.exists():
            return CacheReadResult(outcome=OUTCOME_MISS, key_sha256=key_sha)
        now = time.time()
        try:
            entry = self._read_entry_file(path)
            self._verify_entry(entry, material, key_sha)
        except ContextCacheError as exc:
            self._unlink_regular(path)
            return CacheReadResult(
                outcome=OUTCOME_CORRUPT, key_sha256=key_sha, reason=str(exc)[:200]
            )
        age = now - float(entry.get("created_at_epoch") or 0)
        if age > self.max_age_seconds:
            self._unlink_regular(path)
            return CacheReadResult(
                outcome=OUTCOME_STALE, key_sha256=key_sha, reason="entry_age_limit"
            )
        entry["accessed_at_epoch"] = now
        self._atomic_write_json(path, entry)
        payload = str(entry["payload"])
        return CacheReadResult(
            outcome=OUTCOME_HIT,
            key_sha256=key_sha,
            payload=payload,
            payload_sha256=str(entry["payload_sha256"]),
            metadata=dict(entry.get("metadata") or {}),
        )

    def write(
        self,
        key: ContextCacheKey,
        payload: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> CacheWriteResult:
        material = key_material(key)
        if material["repo_id"] != self.repo_id:
            raise ContextCacheError("repo_id_cache_mismatch")
        if not isinstance(payload, str) or "\x00" in payload:
            raise ContextCacheError("payload_invalid")
        _reject_sensitive_payload(payload)
        payload_bytes = payload.encode("utf-8")
        if len(payload_bytes) > self.max_entry_bytes:
            raise ContextCacheError(
                f"payload_too_large:{len(payload_bytes)}>{self.max_entry_bytes}"
            )
        meta = metadata or {}
        metadata_json = _canonical_json(meta, max_bytes=MAX_METADATA_BYTES, field="metadata")
        key_sha = cache_key_sha256(key)
        now = time.time()
        entry = {
            "schema_id": ENTRY_SCHEMA_ID,
            "key_sha256": key_sha,
            "key_material": material,
            "payload": payload,
            "payload_sha256": _sha256_bytes(payload_bytes),
            "payload_bytes": len(payload_bytes),
            "metadata": json.loads(metadata_json),
            "created_at_epoch": now,
            "accessed_at_epoch": now,
        }
        path = self._entry_path(key_sha)
        self._atomic_write_json(path, entry)
        evicted = self.prune()
        return CacheWriteResult(
            stored=True,
            key_sha256=key_sha,
            payload_sha256=entry["payload_sha256"],
            bytes_written=path.stat().st_size if path.exists() else 0,
            evicted_entries=evicted,
        )

    def prune(self) -> int:
        entries = []
        now = time.time()
        for path in self.repo_root.glob("*/*.json"):
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                data = self._read_entry_file(path)
                accessed = float(data.get("accessed_at_epoch") or 0)
                created = float(data.get("created_at_epoch") or 0)
                size = path.stat().st_size
            except (ContextCacheError, OSError, ValueError, TypeError):
                self._unlink_regular(path)
                continue
            if now - created > self.max_age_seconds:
                self._unlink_regular(path)
                entries.append((0.0, path, 0))
                continue
            entries.append((accessed, path, size))
        total = sum(size for _accessed, _path, size in entries)
        evicted = 0
        for _accessed, path, size in sorted(entries, key=lambda row: row[0]):
            if total <= self.max_total_bytes:
                break
            if self._unlink_regular(path):
                total -= size
                evicted += 1
        return evicted

    @staticmethod
    def _read_entry_file(path: Path) -> dict[str, Any]:
        if path.is_symlink():
            raise ContextCacheError("entry_symlink")
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(path, flags)
        except OSError as exc:
            raise ContextCacheError(f"entry_open_failed:{exc}") from exc
        try:
            with os.fdopen(fd, "r", encoding="utf-8") as fh:
                value = json.loads(fh.read())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ContextCacheError(f"entry_read_failed:{exc}") from exc
        if not isinstance(value, dict):
            raise ContextCacheError("entry_not_object")
        return value

    @staticmethod
    def _verify_entry(entry: dict[str, Any], material: dict[str, Any], key_sha: str) -> None:
        if entry.get("schema_id") != ENTRY_SCHEMA_ID:
            raise ContextCacheError("entry_schema_mismatch")
        if entry.get("key_sha256") != key_sha:
            raise ContextCacheError("entry_key_hash_mismatch")
        if entry.get("key_material") != material:
            raise ContextCacheError("entry_key_material_mismatch")
        payload = entry.get("payload")
        if not isinstance(payload, str):
            raise ContextCacheError("entry_payload_invalid")
        payload_sha = _sha256_bytes(payload.encode("utf-8"))
        if entry.get("payload_sha256") != payload_sha:
            raise ContextCacheError("entry_payload_hash_mismatch")

    @staticmethod
    def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
        if path.exists() and path.is_symlink():
            raise ContextCacheError("entry_symlink")
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(tmp, flags, 0o600)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(encoded)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
            _chmod_owner_only(path, 0o600)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass

    @staticmethod
    def _unlink_regular(path: Path) -> bool:
        try:
            if path.is_symlink() or not path.is_file():
                return False
            path.unlink()
            return True
        except OSError:
            return False


__all__ = [
    "CacheReadResult",
    "CacheWriteResult",
    "ContextCache",
    "ContextCacheError",
    "ContextCacheKey",
    "ContextQueryKey",
    "EMPTY_INPUT_SHARD_SHA256",
    "OUTCOME_CORRUPT",
    "OUTCOME_HIT",
    "OUTCOME_MISS",
    "OUTCOME_STALE",
    "cache_key_sha256",
    "key_material",
    "query_cache_key_sha256",
    "query_key_material",
    "reuse_metadata",
]
