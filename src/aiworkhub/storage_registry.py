"""AIWorkHub repository-local storage registry.

A fresh registry is canonical-only from creation: every entry's authority is
already ``canonical_active`` under this repository's own ``.aiworkhub`` tree,
with no fixed legacy source path and no historic-project hash baked in. There
is no automatic legacy discovery, seeding, or fallback here -- an explicit,
caller-selected import tool (see ``source_graph_migration.py``) may still
copy data from a caller-named legacy path into the canonical location, but
this module never reads or requires one. Loading and resolution are
manifest/repo-id bound and fail closed on malformed authority, traversal,
symlink, or cross-repository reuse.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .repository_state import HUB_DIRNAME, PathEscapeError, RepositoryState, inspect_repository


STORAGE_REGISTRY_SCHEMA_ID = "aiworkhub.storage_registry.v1"
STORAGE_REGISTRY_VERSION = 1
STORAGE_REGISTRY_REL = Path(HUB_DIRNAME) / "config" / "storage.json"

CANONICAL_DATABASES: tuple[dict[str, str], ...] = (
    {
        "id": "task_queue",
        "component": "task_mcp",
        "canonical_path": "tasking/task_queue.sqlite",
    },
    {
        "id": "source_graph",
        "component": "source_graph",
        "canonical_path": "source_graph/source_graph.sqlite",
    },
    {
        "id": "universal",
        "component": "source_graph",
        "canonical_path": "source_graph/universal.sqlite",
    },
    {
        "id": "session",
        "component": "sessions",
        "canonical_path": "sessions/sessions.sqlite",
    },
    {
        "id": "transcript",
        "component": "sessions",
        "canonical_path": "sessions/transcript_graph.sqlite",
    },
    {
        "id": "memory",
        "component": "memory",
        "canonical_path": "memory/memory.sqlite",
    },
    {
        "id": "kb",
        "component": "kb",
        "canonical_path": "kb/knowledge.sqlite",
    },
)


class StorageRegistryError(RuntimeError):
    """Base class for storage registry validation failures."""


class StorageRegistryInvalidError(StorageRegistryError):
    """The registry is malformed or incompatible with the repository manifest."""


@dataclass(frozen=True, slots=True)
class StorageDatabase:
    db_id: str
    component: str
    canonical_path: str
    rollback_source_sha256: str
    integrity_state: str
    authority_state: str
    canonical_active: bool
    legacy_active: bool
    live_cutover: bool
    migration_generation: int


@dataclass(frozen=True, slots=True)
class StorageRegistry:
    repo: RepositoryState
    registry_path: Path
    payload: dict[str, Any]
    databases: dict[str, StorageDatabase]


def default_registry_payload(repo_id: str) -> dict[str, Any]:
    """Return the portable, canonical-only registry payload.

    Every entry carries only ``id``, ``component``, ``canonical_durable_path``
    and an ``authority`` block already asserting ``canonical_active`` under
    this repository's own ``.aiworkhub`` tree from the moment the registry is
    created -- there is no shadow/legacy-fallback phase, no fixed
    ``legacy_source`` path, no historic-project hash, and no
    integrity/migration bookkeeping baked into a fresh entry. A repository
    that owns its data from birth has nothing to migrate from and nothing to
    roll back to, so ``_parse_database`` fills in the equivalent
    already-cutover defaults for any entry that omits ``integrity``/
    ``migration`` -- those blocks remain valid, and are still parsed, on any
    older, richer registry produced by an explicit migration/cutover tool.
    """

    databases: list[dict[str, Any]] = []
    for item in CANONICAL_DATABASES:
        databases.append({
            "id": item["id"],
            "component": item["component"],
            "canonical_durable_path": item["canonical_path"],
            "authority": {
                "state": "canonical_active",
                "canonical_active": True,
                "legacy_active": False,
                "live_cutover": True,
            },
        })

    return {
        "schema_id": STORAGE_REGISTRY_SCHEMA_ID,
        "registry_version": STORAGE_REGISTRY_VERSION,
        "repo_id": repo_id,
        "portable": True,
        "host_absolute_paths_allowed": False,
        "durable_root": HUB_DIRNAME,
        "registry_path": "config/storage.json",
        "databases": databases,
        "runtime_policy": {
            "durable": False,
            "ignored": True,
            "path": "runtime",
            "runtime_only_patterns": [
                "*.sqlite-wal",
                "*.sqlite-shm",
                "*.db-wal",
                "*.db-shm",
                "*.lock",
                "*.sock",
                "credentials/*",
                "worktrees/*",
                "backups/*",
            ],
            "sqlite_policy": {
                "main_files_are_durable": True,
                "wal_shm_are_runtime_only": True,
                "backup_payloads_are_runtime_only_until_promoted": True,
            },
        },
        "migration_policy": {
            "no_live_cutover_in_this_registry": True,
            "no_legacy_delete_in_this_registry": True,
            "legacy_locations_are_read_only": True,
        },
    }


def _ensure_relative_safe(repo_root: Path, rel_value: str, field: str) -> Path:
    rel = Path(rel_value)
    if rel.is_absolute() or "\x00" in rel.as_posix() or ".." in rel.parts:
        raise PathEscapeError(f"storage_registry_path_escape:{field}:{rel_value}")
    candidate = repo_root / rel
    cursor = repo_root
    for part in rel.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise PathEscapeError(f"storage_registry_symlink:{field}:{rel_value}")
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise PathEscapeError(f"storage_registry_unresolvable:{field}:{rel_value}") from exc
    if resolved != repo_root and repo_root not in resolved.parents:
        raise PathEscapeError(f"storage_registry_outside_repo:{field}:{rel_value}")
    return resolved


def _reject_absolute_strings(value: Any, path: str = "$") -> None:
    if isinstance(value, str):
        if Path(value).is_absolute() or value.startswith("file:"):
            raise StorageRegistryInvalidError(f"absolute_or_uri_path_forbidden:{path}")
        return
    if isinstance(value, Mapping):
        for key, child in value.items():
            _reject_absolute_strings(child, f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _reject_absolute_strings(child, f"{path}[{index}]")


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StorageRegistryInvalidError(f"storage_registry_unreadable:{path}") from exc
    if not isinstance(payload, dict):
        raise StorageRegistryInvalidError("storage_registry_must_be_object")
    return payload


def _parse_database(item: Mapping[str, Any], repo_root: Path) -> StorageDatabase:
    db_id = str(item.get("id") or "")
    component = str(item.get("component") or "")
    canonical_path = str(item.get("canonical_durable_path") or "")
    if not db_id or not component or not canonical_path:
        raise StorageRegistryInvalidError("database_required_field_missing")
    _ensure_relative_safe(repo_root / HUB_DIRNAME, canonical_path, f"{db_id}.canonical_durable_path")

    authority = item.get("authority")
    if not isinstance(authority, Mapping):
        raise StorageRegistryInvalidError(f"database_state_missing:{db_id}")
    # ``integrity``/``migration`` are optional bookkeeping blocks: a fresh,
    # canonical-only entry (see ``default_registry_payload``) carries neither
    # -- it has nothing to roll back to -- so an absent block defaults to the
    # equivalent already-cutover-generation-1 state rather than being
    # rejected. An older, richer registry produced by an explicit
    # migration/cutover tool still supplies both and is parsed unchanged.
    integrity_raw = item.get("integrity")
    integrity: Mapping[str, Any] = integrity_raw if isinstance(integrity_raw, Mapping) else {}
    migration_raw = item.get("migration")
    migration: Mapping[str, Any] = migration_raw if isinstance(migration_raw, Mapping) else {}
    fresh_canonical_only = not integrity_raw and not migration_raw
    state = str(authority.get("state") or "")
    canonical_active = authority.get("canonical_active") is True
    legacy_active = authority.get("legacy_active") is True
    live_cutover = authority.get("live_cutover") is True
    if fresh_canonical_only and state == "canonical_active" and canonical_active:
        # A fresh, never-migrated entry is, by construction, an implicit
        # generation-1 cutover with nothing rolled back and nothing deleted.
        cutover_performed = True
        rollback_performed = False
        legacy_deleted = False
        generation = 1
    else:
        cutover_performed = migration.get("cutover_performed") is True
        rollback_performed = migration.get("rollback_performed") is True
        legacy_deleted = migration.get("legacy_deleted") is True
        generation = int(migration.get("generation") or 0)
    shadow = (
        state == "shadow" and not canonical_active and not legacy_active
        and not live_cutover and not cutover_performed
    )
    verified_cutover = (
        state == "canonical_active"
        and canonical_active and not legacy_active and live_cutover
        and cutover_performed and not rollback_performed and generation >= 1
    )
    verified_rollback = (
        state == "legacy_active_restored"
        and not canonical_active and legacy_active and not live_cutover
        and not cutover_performed and rollback_performed and generation >= 1
    )
    if legacy_deleted or not (shadow or verified_cutover or verified_rollback):
        raise StorageRegistryInvalidError(f"authority_state_invalid:{db_id}")

    # A rollback hash is only meaningful once an explicit, caller-selected
    # migration has actually copied data in (see source_graph_migration.py);
    # a canonical-only entry with nothing migrated carries neither field.
    rollback_hash = str(integrity.get("rollback_source_sha256") or "")
    migration_hash = str(migration.get("rollback_source_hash") or "")
    if rollback_hash or migration_hash:
        if len(rollback_hash) != 64 or rollback_hash != migration_hash:
            raise StorageRegistryInvalidError(f"rollback_hash_invalid:{db_id}")

    return StorageDatabase(
        db_id=db_id,
        component=component,
        canonical_path=canonical_path,
        rollback_source_sha256=rollback_hash,
        integrity_state=str(integrity.get("state") or ""),
        authority_state=str(authority.get("state") or ""),
        canonical_active=canonical_active,
        legacy_active=legacy_active,
        live_cutover=live_cutover,
        migration_generation=generation,
    )


def load_storage_registry(
    root: str | Path | None = None,
    *,
    expected_repo_id: str | None = None,
) -> StorageRegistry:
    repo = inspect_repository(root)
    if expected_repo_id is not None and repo.manifest.repo_id != expected_repo_id:
        raise StorageRegistryInvalidError("storage_registry_repo_id_mismatch")
    registry_path = _ensure_relative_safe(repo.root, STORAGE_REGISTRY_REL.as_posix(), "registry_path")
    payload = _load_json(registry_path)
    _reject_absolute_strings(payload)

    if payload.get("schema_id") != STORAGE_REGISTRY_SCHEMA_ID:
        raise StorageRegistryInvalidError("storage_registry_schema_id_invalid")
    if payload.get("registry_version") != STORAGE_REGISTRY_VERSION:
        raise StorageRegistryInvalidError("storage_registry_version_invalid")
    if payload.get("repo_id") != repo.manifest.repo_id:
        raise StorageRegistryInvalidError("storage_registry_repo_id_mismatch")
    if payload.get("host_absolute_paths_allowed") is not False:
        raise StorageRegistryInvalidError("host_absolute_paths_must_be_forbidden")
    if payload.get("durable_root") != HUB_DIRNAME:
        raise StorageRegistryInvalidError("durable_root_invalid")

    databases_payload = payload.get("databases")
    if not isinstance(databases_payload, list) or not databases_payload:
        raise StorageRegistryInvalidError("databases_missing")
    databases: dict[str, StorageDatabase] = {}
    for item in databases_payload:
        if not isinstance(item, Mapping):
            raise StorageRegistryInvalidError("database_must_be_object")
        db = _parse_database(item, repo.root)
        if db.db_id in databases:
            raise StorageRegistryInvalidError(f"database_duplicate:{db.db_id}")
        databases[db.db_id] = db
    return StorageRegistry(repo=repo, registry_path=registry_path, payload=payload, databases=databases)


def resolve_database_path(registry: StorageRegistry, db_id: str) -> Path:
    try:
        db = registry.databases[db_id]
    except KeyError as exc:
        raise StorageRegistryInvalidError(f"database_unknown:{db_id}") from exc
    return _ensure_relative_safe(
        registry.repo.root / HUB_DIRNAME,
        db.canonical_path,
        f"{db_id}.canonical_durable_path",
    )


__all__ = [
    "CANONICAL_DATABASES",
    "STORAGE_REGISTRY_REL",
    "STORAGE_REGISTRY_SCHEMA_ID",
    "STORAGE_REGISTRY_VERSION",
    "StorageDatabase",
    "StorageRegistry",
    "StorageRegistryError",
    "StorageRegistryInvalidError",
    "default_registry_payload",
    "load_storage_registry",
    "resolve_database_path",
]
