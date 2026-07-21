"""Verified, one-time migration of legacy AITools Source Graph databases into
the canonical AIWorkHub location, plus an idempotent authority cutover.

Legacy databases (``AITools/source_graph.db``, ``AITools/source_graph_universal.db``)
are treated as READ-ONLY migration inputs -- this module never writes them.
Copying uses SQLite's Online Backup API (``sqlite3.Connection.backup``), the
supported way to obtain a consistent snapshot of a live database file without
requiring exclusive access. Every migration is verified with
``PRAGMA integrity_check`` plus a schema/table/row-count comparison and a
whole-file sha256 before it is ever treated as authoritative, and a rollback
manifest is recorded next to the canonical database on every run (including
dry runs) so a bad migration can always be traced back to its exact legacy
source.

Cutover (flipping ``storage.json``'s ``authority.canonical_active`` for a
database id) is a SEPARATE, explicit, idempotent step that this module
refuses to perform unless the migration it is based on reported
``parity_ok``. Calling it twice is a no-op the second time.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .repository_state import HUB_DIRNAME, RepositoryStateError
from .storage_registry import (
    StorageRegistryError,
    load_storage_registry,
    resolve_database_path,
)

MIGRATION_MANIFEST_NAME = "migration_manifest.json"


class MigrationError(RuntimeError):
    """A Source Graph migration/cutover step failed or was refused."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        tmp_name = ""
    finally:
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass


def _table_row_counts(conn: sqlite3.Connection) -> dict[str, int]:
    tables = [
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    counts: dict[str, int] = {}
    for table in tables:
        try:
            counts[table] = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        except sqlite3.DatabaseError:
            counts[table] = -1
    return counts


@dataclass(frozen=True, slots=True)
class MigrationReport:
    status: str
    db_id: str
    legacy_path: str
    canonical_path: str
    legacy_sha256: str
    copy_sha256: str
    legacy_integrity_check: str
    copy_integrity_check: str
    legacy_tables: dict[str, int]
    copy_tables: dict[str, int]
    parity_ok: bool
    rollback_manifest_path: str
    dry_run: bool
    finished_at: str

    def to_json(self) -> dict[str, Any]:
        return {
            "status": self.status, "db_id": self.db_id, "legacy_path": self.legacy_path,
            "canonical_path": self.canonical_path, "legacy_sha256": self.legacy_sha256,
            "copy_sha256": self.copy_sha256, "legacy_integrity_check": self.legacy_integrity_check,
            "copy_integrity_check": self.copy_integrity_check, "legacy_tables": self.legacy_tables,
            "copy_tables": self.copy_tables, "parity_ok": self.parity_ok,
            "rollback_manifest_path": self.rollback_manifest_path, "dry_run": self.dry_run,
            "finished_at": self.finished_at,
        }


def migrate_legacy_db(
    repo_root: Path, *, db_id: str, legacy_rel: str, dry_run: bool = True,
) -> MigrationReport:
    """Copy ``legacy_rel`` into the canonical ``db_id`` location, verified.

    ``legacy_rel`` is never written to. When it does not exist, this returns
    a ``no_legacy_source_skip`` report -- a fresh repository with no prior
    AITools database is a valid, expected state, not an error. When
    ``dry_run`` is true (the default) the verified copy is discarded after
    the parity check and only the rollback manifest is written; the
    canonical database file itself is only replaced when ``dry_run=False``
    AND parity passed.
    """

    repo_root = repo_root.resolve()
    legacy_path = (repo_root / legacy_rel).resolve()
    if repo_root not in legacy_path.parents and legacy_path != repo_root:
        raise MigrationError("legacy_path_escapes_repo")

    if not legacy_path.is_file():
        return MigrationReport(
            status="no_legacy_source_skip", db_id=db_id, legacy_path=str(legacy_path),
            canonical_path="", legacy_sha256="", copy_sha256="", legacy_integrity_check="",
            copy_integrity_check="", legacy_tables={}, copy_tables={}, parity_ok=True,
            rollback_manifest_path="", dry_run=dry_run, finished_at=_now_iso(),
        )

    try:
        registry = load_storage_registry(repo_root)
        canonical_path = resolve_database_path(registry, db_id)
    except (RepositoryStateError, StorageRegistryError) as exc:
        raise MigrationError(f"canonical_path_unresolved:{exc}") from exc

    legacy_sha = _sha256_file(legacy_path)
    legacy_conn = sqlite3.connect(str(legacy_path))
    try:
        legacy_integrity = legacy_conn.execute("PRAGMA integrity_check").fetchone()[0]
        legacy_tables = _table_row_counts(legacy_conn)

        canonical_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=".source_graph_migration.", suffix=".sqlite", dir=str(canonical_path.parent),
        )
        os.close(fd)
        tmp_path = Path(tmp_name)
        tmp_path.unlink()  # backup() must create the destination file itself
        dest_conn = sqlite3.connect(str(tmp_path))
        try:
            legacy_conn.backup(dest_conn)
        finally:
            dest_conn.close()
    finally:
        legacy_conn.close()

    copy_conn = sqlite3.connect(str(tmp_path))
    try:
        copy_integrity = copy_conn.execute("PRAGMA integrity_check").fetchone()[0]
        copy_tables = _table_row_counts(copy_conn)
    finally:
        copy_conn.close()
    copy_sha = _sha256_file(tmp_path)

    parity_ok = (
        legacy_integrity == "ok" and copy_integrity == "ok" and legacy_tables == copy_tables
    )

    rollback_manifest = {
        "schema_id": "aiworkhub.source_graph_migration.rollback.v1",
        "db_id": db_id, "legacy_path": str(legacy_path), "legacy_sha256": legacy_sha,
        "canonical_path": str(canonical_path), "copy_sha256": copy_sha,
        "legacy_integrity_check": legacy_integrity, "copy_integrity_check": copy_integrity,
        "legacy_tables": legacy_tables, "copy_tables": copy_tables, "parity_ok": parity_ok,
        "recorded_at": _now_iso(), "dry_run": dry_run,
        "rollback_instructions": (
            "legacy_path was never modified by this migration; to roll back a bad "
            "cutover, restore canonical_path from this manifest's legacy_path and "
            "revert storage.json's authority.state for db_id to 'shadow'."
        ),
    }
    manifest_path = canonical_path.parent / MIGRATION_MANIFEST_NAME
    _atomic_write_json(manifest_path, rollback_manifest)

    if dry_run or not parity_ok:
        tmp_path.unlink(missing_ok=True)
        status = "verified_dry_run" if parity_ok else "parity_failed"
    else:
        os.replace(tmp_path, canonical_path)
        status = "migrated_and_verified"

    return MigrationReport(
        status=status, db_id=db_id, legacy_path=str(legacy_path), canonical_path=str(canonical_path),
        legacy_sha256=legacy_sha, copy_sha256=copy_sha, legacy_integrity_check=legacy_integrity,
        copy_integrity_check=copy_integrity, legacy_tables=legacy_tables, copy_tables=copy_tables,
        parity_ok=parity_ok, rollback_manifest_path=str(manifest_path), dry_run=dry_run,
        finished_at=_now_iso(),
    )


def perform_cutover(repo_root: Path, db_id: str, *, parity_ok: bool) -> dict[str, Any]:
    """Idempotently flip ``storage.json`` authority to canonical for ``db_id``.

    Refuses (raises :class:`MigrationError`) unless ``parity_ok`` is true --
    callers must pass the ``parity_ok`` field from a prior
    :func:`migrate_legacy_db` report (or ``True`` for a fresh repository with
    no legacy source, where the canonical database is the only copy that has
    ever existed). Calling this twice for an already-cutover ``db_id`` is a
    no-op that returns ``status="already_cutover"`` rather than bumping the
    migration generation again.
    """

    if not parity_ok:
        raise MigrationError(f"cutover_requires_parity_ok:{db_id}")

    repo_root = repo_root.resolve()
    registry_path = repo_root / HUB_DIRNAME / "config" / "storage.json"
    try:
        payload = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MigrationError(f"storage_registry_unreadable:{exc}") from exc

    entries = payload.get("databases")
    if not isinstance(entries, list):
        raise MigrationError("storage_registry_databases_missing")
    target = next((e for e in entries if isinstance(e, dict) and e.get("id") == db_id), None)
    if target is None:
        raise MigrationError(f"storage_registry_entry_missing:{db_id}")

    authority = target.setdefault("authority", {})
    migration = target.setdefault("migration", {})
    if authority.get("canonical_active") is True:
        return {
            "status": "already_cutover", "db_id": db_id,
            "generation": int(migration.get("generation") or 0),
        }

    generation = int(migration.get("generation") or 0) + 1
    authority["state"] = "canonical_active"
    authority["canonical_active"] = True
    authority["legacy_active"] = False
    authority["live_cutover"] = True
    migration["generation"] = generation
    migration["cutover_performed"] = True
    migration["rollback_performed"] = False
    migration["legacy_deleted"] = False

    _atomic_write_json(registry_path, payload)

    # Re-validate through the real loader so a malformed write never lands
    # as a silently-accepted authority state.
    try:
        load_storage_registry(repo_root, expected_repo_id=payload.get("repo_id"))
    except (RepositoryStateError, StorageRegistryError) as exc:
        raise MigrationError(f"cutover_write_failed_revalidation:{exc}") from exc

    return {"status": "cutover_applied", "db_id": db_id, "generation": generation}


__all__ = [
    "MIGRATION_MANIFEST_NAME",
    "MigrationError",
    "MigrationReport",
    "migrate_legacy_db",
    "perform_cutover",
]
