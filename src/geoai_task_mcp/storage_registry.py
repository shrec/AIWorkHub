"""AIWorkingHub repository-local storage registry.

The registry starts as a shadow migration contract and may promote one store
through an explicit, verified cutover. Loading and resolution are manifest/
repo-id bound and fail closed on malformed authority, traversal, symlink, or
cross-repository reuse.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .repository_state import HUB_DIRNAME, PathEscapeError, RepositoryState, inspect_repository


STORAGE_REGISTRY_SCHEMA_ID = "aiworkinghub.storage_registry.v1"
STORAGE_REGISTRY_VERSION = 1
STORAGE_REGISTRY_REL = Path(HUB_DIRNAME) / "config" / "storage.json"

CANONICAL_DATABASES: tuple[dict[str, str], ...] = (
    {
        "id": "task_queue",
        "component": "task_mcp",
        "canonical_path": "tasking/task_queue.sqlite",
        "legacy_source": "bitnnv2/data/tasking/task_queue_v1.sqlite",
        "sha256": "23ecc369f601c3339a802a383d6f15e147df129771e016c05419b2fcb95fb2dd",
    },
    {
        "id": "source_graph",
        "component": "source_graph",
        "canonical_path": "source_graph/source_graph.sqlite",
        "legacy_source": "AITools/source_graph.db",
        "sha256": "6d645a92301727613473eb670982fe56b3a2664a5e43881083eae84b03ac0417",
    },
    {
        "id": "universal",
        "component": "source_graph",
        "canonical_path": "source_graph/universal.sqlite",
        "legacy_source": "AITools/source_graph_universal.db",
        "sha256": "d1bea7d3e8be1f9e59516c4f7ad0b193cc18f33e7a9af14fe3f4880a42597e3e",
    },
    {
        "id": "session",
        "component": "sessions",
        "canonical_path": "sessions/sessions.sqlite",
        "legacy_source": "AITools/session.db",
        "sha256": "34a702b54f17b30b36c16b3e602241c18d35cbc232b11dd99f50e18b7c07b66b",
    },
    {
        "id": "transcript",
        "component": "sessions",
        "canonical_path": "sessions/transcript_graph.sqlite",
        "legacy_source": "AITools/transcript_graph.db",
        "sha256": "773eab9a8cf5a075edaf2962a1250937a68e71bd8bb305ecbb3f7b8d54789196",
    },
    {
        "id": "memory",
        "component": "memory",
        "canonical_path": "memory/memory.sqlite",
        "legacy_source": "AITools/ai_memory/ai_memory.db",
        "sha256": "8fe74da44c6c0dae5cc54a6e062959c531edd9b9a9a591f96fbc6cffb6c17883",
    },
    {
        "id": "kb",
        "component": "kb",
        "canonical_path": "kb/knowledge.sqlite",
        "legacy_source": "AITools/kb.db",
        "sha256": "5188b5e8ea9d7afe53bf2e3a2440619c00d3463943b9ca10aa5ffeabacf19886",
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
    legacy_source: str
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
    """Return the portable registry payload for the known canonical inventory."""

    databases: list[dict[str, Any]] = []
    for item in CANONICAL_DATABASES:
        databases.append({
            "id": item["id"],
            "component": item["component"],
            "schema_version": 1,
            "canonical_durable_path": item["canonical_path"],
            "legacy_source": item["legacy_source"],
            "integrity": {
                "state": "canonical_inventory_hash_supplied",
                "rollback_source_sha256": item["sha256"],
                "sparse_worktree_missing_is_not_absence": True,
            },
            "authority": {
                "state": "shadow",
                "canonical_active": False,
                "legacy_active": False,
                "live_cutover": False,
            },
            "migration": {
                "generation": 0,
                "source_read_only": True,
                "rollback_source_hash": item["sha256"],
                "cutover_performed": False,
                "legacy_deleted": False,
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
    legacy_source = str(item.get("legacy_source") or "")
    if not db_id or not component or not canonical_path or not legacy_source:
        raise StorageRegistryInvalidError("database_required_field_missing")
    _ensure_relative_safe(repo_root / HUB_DIRNAME, canonical_path, f"{db_id}.canonical_durable_path")
    _ensure_relative_safe(repo_root, legacy_source, f"{db_id}.legacy_source")

    integrity = item.get("integrity")
    authority = item.get("authority")
    migration = item.get("migration")
    if not isinstance(integrity, Mapping) or not isinstance(authority, Mapping) or not isinstance(migration, Mapping):
        raise StorageRegistryInvalidError(f"database_state_missing:{db_id}")
    state = str(authority.get("state") or "")
    canonical_active = authority.get("canonical_active") is True
    legacy_active = authority.get("legacy_active") is True
    live_cutover = authority.get("live_cutover") is True
    cutover_performed = migration.get("cutover_performed") is True
    rollback_performed = migration.get("rollback_performed") is True
    legacy_deleted = migration.get("legacy_deleted") is True
    generation = int(migration.get("generation") or 0)
    shadow = (
        state == "shadow" and not canonical_active and not legacy_active
        and not live_cutover and not cutover_performed
    )
    verified_task_cutover = (
        db_id == "task_queue" and state == "canonical_active"
        and canonical_active and not legacy_active and live_cutover
        and cutover_performed and not rollback_performed and generation >= 1
    )
    verified_task_rollback = (
        db_id == "task_queue" and state == "legacy_active_restored"
        and not canonical_active and legacy_active and not live_cutover
        and not cutover_performed and rollback_performed and generation >= 1
    )
    if legacy_deleted or not (shadow or verified_task_cutover or verified_task_rollback):
        raise StorageRegistryInvalidError(f"authority_state_invalid:{db_id}")

    rollback_hash = str(integrity.get("rollback_source_sha256") or "")
    migration_hash = str(migration.get("rollback_source_hash") or "")
    if len(rollback_hash) != 64 or rollback_hash != migration_hash:
        raise StorageRegistryInvalidError(f"rollback_hash_invalid:{db_id}")

    return StorageDatabase(
        db_id=db_id,
        component=component,
        canonical_path=canonical_path,
        legacy_source=legacy_source,
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
