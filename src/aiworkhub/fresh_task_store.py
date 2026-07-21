"""Rollback-safe fresh Task MCP queue cutover helpers.

The coordinator uses this module to archive the legacy SQLite queue, prove a
new schema-only DB is empty, and atomically publish it as the repo-local active
queue.  The default path is dry-run/preflight; mutation requires an explicit
caller gate from taskctl.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


TASK_ID = "CODEX_GPT55_AIWORKHUB_FRESH_TASKDB_CUTOVER_B832_V1"
HUB = ".aiworkhub"
LEGACY_REL = Path("bitnnv2") / "data" / "tasking" / "task_queue_v1.sqlite"
CANONICAL_REL = Path("tasking") / "task_queue.sqlite"
ARCHIVE_REL = Path("tasking") / "archive" / "task_queue_v1_legacy_20260720.sqlite"
EMPTY_TABLES = ("tasks", "task_events", "callback_outbox", "callback_batches")


class FreshTaskStoreError(RuntimeError):
    """Fresh queue cutover failed or was refused."""


class CutoverRefusedError(FreshTaskStoreError):
    """The request did not satisfy the coordinator execute gate."""


@dataclass(frozen=True, slots=True)
class FreshTaskStorePaths:
    repo_root: Path
    repo_id: str
    registry_path: Path
    legacy_db: Path
    archive_db: Path
    canonical_db: Path


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FreshTaskStoreError(f"json_unreadable:{path}") from exc
    if not isinstance(payload, dict):
        raise FreshTaskStoreError(f"json_not_object:{path}")
    return payload


def _safe_join(base: Path, rel_value: str, field: str) -> Path:
    rel = Path(rel_value)
    if rel.is_absolute() or "\x00" in rel.as_posix() or ".." in rel.parts:
        raise FreshTaskStoreError(f"path_escape:{field}")
    path = (base / rel).resolve(strict=False)
    root = base.resolve(strict=False)
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise FreshTaskStoreError(f"path_outside_repo:{field}") from exc
    return path


def resolve_paths(repo_root: str | Path) -> FreshTaskStorePaths:
    root = Path(repo_root).resolve(strict=False)
    manifest = _load_json(root / HUB / "project.json")
    registry = _load_json(root / HUB / "config" / "storage.json")
    repo_id = str(manifest.get("repo_id") or "")
    if not repo_id or registry.get("repo_id") != repo_id:
        raise FreshTaskStoreError("repo_id_mismatch")
    if registry.get("durable_root") != HUB:
        raise FreshTaskStoreError("durable_root_invalid")
    task_record = _task_queue_record(registry)
    return FreshTaskStorePaths(
        repo_root=root,
        repo_id=repo_id,
        registry_path=root / HUB / "config" / "storage.json",
        legacy_db=_safe_join(root, str(task_record.get("legacy_source") or LEGACY_REL.as_posix()), "legacy_source"),
        archive_db=_safe_join(root / HUB, ARCHIVE_REL.as_posix(), "archive"),
        canonical_db=_safe_join(root / HUB, str(task_record.get("canonical_durable_path") or CANONICAL_REL.as_posix()), "canonical"),
    )


def _task_queue_record(registry: Mapping[str, Any]) -> dict[str, Any]:
    for item in registry.get("databases") or []:
        if isinstance(item, dict) and item.get("id") == "task_queue":
            return item
    raise FreshTaskStoreError("task_queue_record_missing")


def _taskdb_module(repo_root: Path):
    aitools = str(repo_root / "AITools")
    if aitools not in sys.path:
        sys.path.insert(0, aitools)
    import taskdb  # type: ignore[import-not-found]

    return taskdb


def _connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    if not readonly:
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    if readonly:
        conn.execute("PRAGMA query_only=ON")
    return conn


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def sqlite_online_backup(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=str(destination.parent))
    os.close(fd)
    try:
        with _connect(source, readonly=True) as src, _connect(Path(tmp_name)) as dst:
            src.backup(dst, pages=128)
            dst.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            dst.commit()
        os.replace(tmp_name, destination)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def quick_check(path: Path) -> str:
    with _connect(path, readonly=True) as conn:
        row = conn.execute("PRAGMA quick_check").fetchone()
    return str(row[0] if row else "")


def schema_fingerprint(path: Path) -> str:
    with _connect(path, readonly=True) as conn:
        rows = [
            dict(row)
            for row in conn.execute(
                """
                SELECT type, name, tbl_name, COALESCE(sql, '') AS sql
                  FROM sqlite_master
                 WHERE type IN ('table', 'index', 'trigger', 'view')
                   AND name NOT LIKE 'sqlite_%'
                 ORDER BY type, name, tbl_name, sql
                """
            ).fetchall()
        ]
    payload = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def table_counts(path: Path) -> dict[str, int]:
    """Logical inventory used to prove that an online archive matches source."""
    with _connect(path, readonly=True) as conn:
        names = [
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]
        return {name: int(conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]) for name in names}


def _table_count(conn: sqlite3.Connection, table: str) -> int:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    if row is None:
        return 0
    return int(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])


def empty_counts(path: Path) -> dict[str, int]:
    with _connect(path, readonly=True) as conn:
        return {table: _table_count(conn, table) for table in EMPTY_TABLES}


def _active_blockers(conn: sqlite3.Connection) -> list[dict[str, str]]:
    if _table_count(conn, "tasks") == 0:
        return []
    rows = conn.execute(
        """
        SELECT task_id, status, worker_status
          FROM tasks
         WHERE task_id != ?
           AND COALESCE(archived_at, '') = ''
           AND (status IN ('processing', 'review') OR worker_status IN ('processing', 'review'))
         ORDER BY task_id
        """,
        (TASK_ID,),
    ).fetchall()
    return [dict(row) for row in rows]


def _inflight_callback_leases(conn: sqlite3.Connection) -> dict[str, list[dict[str, str]]]:
    leases: dict[str, list[dict[str, str]]] = {"callback_outbox": [], "callback_batches": []}
    if _table_count(conn, "callback_outbox"):
        leases["callback_outbox"] = [
            dict(row)
            for row in conn.execute(
                """
                SELECT task_id, state, lease_id, lease_expires_at
                  FROM callback_outbox
                 WHERE state='inflight' AND COALESCE(lease_id, '') != ''
                 ORDER BY task_id, lease_id
                """
            ).fetchall()
            if _lease_is_live(str(row["lease_expires_at"] or ""))
        ]
    if _table_count(conn, "callback_batches"):
        leases["callback_batches"] = [
            dict(row)
            for row in conn.execute(
                """
                SELECT batch_id, state, lease_id, lease_expires_at
                  FROM callback_batches
                 WHERE state='inflight' AND COALESCE(lease_id, '') != ''
                 ORDER BY batch_id, lease_id
                """
            ).fetchall()
            if _lease_is_live(str(row["lease_expires_at"] or ""))
        ]
    return leases


def _lease_is_live(expires_at: str) -> bool:
    """Malformed/missing expiry is live (fail closed); expired leases are stale."""
    if not expires_at:
        return True
    try:
        parsed = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    return parsed > datetime.now(timezone.utc)


def preflight(repo_root: str | Path) -> dict[str, Any]:
    paths = resolve_paths(repo_root)
    if not paths.legacy_db.is_file():
        raise FreshTaskStoreError(f"legacy_db_missing:{paths.legacy_db}")
    with _connect(paths.legacy_db, readonly=True) as conn:
        active_blockers = _active_blockers(conn)
        inflight_leases = _inflight_callback_leases(conn)
    refused = bool(active_blockers or inflight_leases["callback_outbox"] or inflight_leases["callback_batches"])
    return {
        "action": "preflight",
        "ok": not refused,
        "cutover_performed": False,
        "repo_id": paths.repo_id,
        "paths": _paths_json(paths),
        "active_blockers": active_blockers,
        "inflight_callback_leases": inflight_leases,
    }


def _paths_json(paths: FreshTaskStorePaths) -> dict[str, str]:
    return {
        "legacy_db": str(paths.legacy_db),
        "archive_db": str(paths.archive_db),
        "canonical_db": str(paths.canonical_db),
        "registry_path": str(paths.registry_path),
    }


def _initialize_empty_db(repo_root: Path, destination: Path) -> dict[str, Any]:
    taskdb = _taskdb_module(repo_root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{destination.name}.fresh.", suffix=".tmp", dir=str(destination.parent))
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        with taskdb.open_db(tmp) as conn:
            taskdb.init_db(conn)
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.commit()
        qc = quick_check(tmp)
        counts = empty_counts(tmp)
        if qc != "ok" or any(counts.values()):
            raise FreshTaskStoreError(f"fresh_db_not_empty:quick_check={qc}:counts={counts}")
        return {
            "path": str(tmp),
            "quick_check": qc,
            "byte_size": tmp.stat().st_size,
            "schema_fingerprint": schema_fingerprint(tmp),
            "counts": counts,
        }
    except Exception:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def _rebase_registry(paths: FreshTaskStorePaths, archive_hash: str, *, rollback: bool = False) -> dict[str, Any]:
    registry = _load_json(paths.registry_path)
    original = json.loads(json.dumps(registry, sort_keys=True))
    record = _task_queue_record(registry)
    authority = record.setdefault("authority", {})
    migration = record.setdefault("migration", {})
    integrity = record.setdefault("integrity", {})
    generation = int(migration.get("generation") or 0) + 1
    authority.update({
        "state": "canonical_active" if not rollback else "legacy_active_restored",
        "canonical_active": not rollback,
        "legacy_active": rollback,
        "live_cutover": not rollback,
    })
    migration.update({
        "generation": generation,
        "source_read_only": True,
        "rollback_source_hash": archive_hash,
        "archive_path": ARCHIVE_REL.as_posix(),
        "archive_sha256": archive_hash,
        "cutover_performed": not rollback,
        "legacy_deleted": False,
        "rollback_performed": rollback,
    })
    integrity.update({
        "state": "fresh_cutover_archive_verified" if not rollback else "rollback_archive_restored",
        "rollback_source_sha256": archive_hash,
        "archive_sha256": archive_hash,
    })
    policy = registry.get("migration_policy")
    if isinstance(policy, dict):
        policy["no_live_cutover_in_this_registry"] = bool(rollback)
    _atomic_write_json(paths.registry_path, registry)
    return {
        "generation": generation,
        "other_records_unchanged": _other_records_unchanged(original, registry),
        "task_queue": record,
    }


def _other_records_unchanged(before: Mapping[str, Any], after: Mapping[str, Any]) -> bool:
    def without_task_queue(payload: Mapping[str, Any]) -> list[Any]:
        return [item for item in payload.get("databases") or [] if not (isinstance(item, dict) and item.get("id") == "task_queue")]

    return without_task_queue(before) == without_task_queue(after)


def cutover(repo_root: str | Path, *, execute: bool = False) -> dict[str, Any]:
    paths = resolve_paths(repo_root)
    pf = preflight(paths.repo_root)
    if not pf["ok"]:
        raise CutoverRefusedError("preflight_refused")
    if not execute:
        return {**pf, "action": "dry_run", "execute_required": True}

    source = {
        "quick_check": quick_check(paths.legacy_db),
        "byte_size": paths.legacy_db.stat().st_size,
        "schema_fingerprint": schema_fingerprint(paths.legacy_db),
        "table_counts": table_counts(paths.legacy_db),
        "sha256": sha256_file(paths.legacy_db),
    }
    if source["quick_check"] != "ok":
        raise FreshTaskStoreError("legacy_quick_check_failed")
    if paths.archive_db.exists():
        # Archive is immutable: reuse only an already-proven logical match.
        if (
            quick_check(paths.archive_db) != "ok"
            or schema_fingerprint(paths.archive_db) != source["schema_fingerprint"]
            or table_counts(paths.archive_db) != source["table_counts"]
        ):
            raise FreshTaskStoreError("immutable_archive_mismatch")
    else:
        sqlite_online_backup(paths.legacy_db, paths.archive_db)
    archive = {
        "path": str(paths.archive_db),
        "quick_check": quick_check(paths.archive_db),
        "byte_size": paths.archive_db.stat().st_size,
        "schema_fingerprint": schema_fingerprint(paths.archive_db),
        "sha256": sha256_file(paths.archive_db),
        "table_counts": table_counts(paths.archive_db),
    }
    if (
        archive["quick_check"] != "ok"
        or archive["schema_fingerprint"] != source["schema_fingerprint"]
        or archive["table_counts"] != source["table_counts"]
    ):
        raise FreshTaskStoreError("archive_parity_failed")

    fresh = _initialize_empty_db(paths.repo_root, paths.canonical_db)
    fresh_tmp = Path(str(fresh["path"]))
    try:
        os.replace(fresh_tmp, paths.canonical_db)
    finally:
        try:
            fresh_tmp.unlink()
        except FileNotFoundError:
            pass

    registry = _rebase_registry(paths, str(archive["sha256"]), rollback=False)
    return {
        "action": "fresh_cutover_complete",
        "cutover_performed": True,
        "legacy_imported": False,
        "repo_id": paths.repo_id,
        "paths": _paths_json(paths),
        "archive": archive,
        "source": source,
        "fresh": {**fresh, "path": str(paths.canonical_db)},
        "registry": registry,
    }


def rollback(repo_root: str | Path, *, execute: bool = False) -> dict[str, Any]:
    paths = resolve_paths(repo_root)
    if not paths.archive_db.is_file():
        raise FreshTaskStoreError("archive_missing")
    archive_hash = sha256_file(paths.archive_db)
    if not paths.legacy_db.is_file():
        raise FreshTaskStoreError("legacy_db_missing")
    if (
        quick_check(paths.archive_db) != "ok"
        or quick_check(paths.legacy_db) != "ok"
        or schema_fingerprint(paths.archive_db) != schema_fingerprint(paths.legacy_db)
        or table_counts(paths.archive_db) != table_counts(paths.legacy_db)
    ):
        raise FreshTaskStoreError("legacy_archive_parity_failed")
    if not execute:
        return {
            "action": "rollback_dry_run",
            "cutover_performed": False,
            "execute_required": True,
            "archive_sha256": archive_hash,
            "paths": _paths_json(paths),
        }
    registry = _rebase_registry(paths, archive_hash, rollback=True)
    return {
        "action": "rollback_complete",
        "cutover_performed": False,
        "archive_sha256": archive_hash,
        "legacy_sha256": sha256_file(paths.legacy_db),
        "paths": _paths_json(paths),
        "registry": registry,
    }


__all__ = [
    "ARCHIVE_REL",
    "CANONICAL_REL",
    "CutoverRefusedError",
    "FreshTaskStoreError",
    "FreshTaskStorePaths",
    "LEGACY_REL",
    "TASK_ID",
    "cutover",
    "preflight",
    "quick_check",
    "resolve_paths",
    "rollback",
    "schema_fingerprint",
    "sha256_file",
    "sqlite_online_backup",
]
