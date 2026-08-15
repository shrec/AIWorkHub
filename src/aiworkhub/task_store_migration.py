"""Online SQLite task-store backup, parity, and explicit cutover helpers."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .repository_state import inspect_repository


TASK_QUEUE_FILENAME = "task_queue.sqlite"
LEGACY_TASK_QUEUE_REL = Path("bitnnv2") / "data" / "tasking" / "task_queue_v1.sqlite"
PARITY_TABLES = (
    "tasks",
    "task_events",
    "task_artifacts",
    "callback_outbox",
    "callback_batches",
)


class TaskStoreMigrationError(RuntimeError):
    """Base migration error."""


class ParityMismatchError(TaskStoreMigrationError):
    """Shadow/cutover target does not match the source."""


class CutoverRefusedError(TaskStoreMigrationError):
    """Cutover was requested without the explicit coordinator flag."""


class SourceAbsentError(TaskStoreMigrationError):
    """Migration source database does not exist."""


class ShadowAbsentError(TaskStoreMigrationError):
    """Cutover shadow database does not exist."""


class WalUnsafeError(TaskStoreMigrationError):
    """A live WAL connection prevents a safe checkpoint or cutover."""


class MigrationDatabaseError(TaskStoreMigrationError):
    """A SQLite database operation failed during migration."""


@dataclass(frozen=True, slots=True)
class TaskStorePaths:
    repo_root: Path
    source_db: Path
    canonical_db: Path
    shadow_db: Path
    rollback_db: Path
    repo_id: str


def resolve_task_store_paths(
    repo_root: str | Path,
    *,
    source_db: str | Path | None = None,
    canonical_db: str | Path | None = None,
    shadow_db: str | Path | None = None,
) -> TaskStorePaths:
    """Resolve migration paths from the AIWorkHub manifest.

    The compatibility source remains the legacy queue path by default; callers
    may pass an explicit source for tests.  ``canonical_db`` always prefers the
    manifest's durable tasking directory when present.
    """
    state = inspect_repository(repo_root)
    root = state.root
    src = Path(source_db) if source_db is not None else root / LEGACY_TASK_QUEUE_REL
    if not src.is_absolute():
        src = root / src
    canonical = (
        Path(canonical_db)
        if canonical_db is not None
        else state.durable_paths["tasking"] / TASK_QUEUE_FILENAME
    )
    if not canonical.is_absolute():
        canonical = root / canonical
    shadow = Path(shadow_db) if shadow_db is not None else canonical.with_name(f".{canonical.name}.shadow")
    if not shadow.is_absolute():
        shadow = root / shadow
    return TaskStorePaths(
        repo_root=root,
        source_db=src.resolve(strict=False),
        canonical_db=canonical.resolve(strict=False),
        shadow_db=shadow.resolve(strict=False),
        rollback_db=canonical.with_name(f".{canonical.name}.rollback"),
        repo_id=state.manifest.repo_id,
    )


def _connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    path = Path(path)
    if readonly:
        # Never let a missing database materialise as an empty, all-zero parity
        # target: that is how an absent source silently "matches" an empty
        # shadow and deletes a populated canonical store during cutover.
        if not path.exists():
            raise SourceAbsentError(str(path))
        # Open read-only via SQLite URI mode=ro. A plain path open defaults to
        # read-write and would silently recreate an empty database if another
        # process deletes the file between the existence precheck above and the
        # open below (TOCTOU). mode=ro fails closed instead.
        try:
            conn = sqlite3.connect(f"{path.absolute().as_uri()}?mode=ro", uri=True)
        except sqlite3.OperationalError as exc:
            if not path.exists():
                raise SourceAbsentError(str(path)) from exc
            raise MigrationDatabaseError(f"readonly_open_failed:{path}") from exc
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    if readonly:
        conn.execute("PRAGMA query_only=ON")
    return conn


def sqlite_online_backup(
    source_db: Path,
    destination_db: Path,
    *,
    pages: int = 128,
    progress: Callable[[int, int, int], None] | None = None,
) -> None:
    """Copy a live SQLite DB using sqlite3's online backup API.

    The destination is written to a private temporary file in the destination
    directory and atomically published only after ``Connection.backup`` returns
    successfully.  An interrupted backup therefore leaves the previous shadow
    intact instead of publishing a partial database.
    """
    source_db = Path(source_db)
    destination_db = Path(destination_db)
    destination_db.parent.mkdir(parents=True, exist_ok=True)
    fd = -1
    tmp_name = ""
    try:
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{destination_db.name}.",
            suffix=".tmp",
            dir=str(destination_db.parent),
        )
        os.close(fd)
        fd = -1
        src = _connect(source_db, readonly=True)
        dst = _connect(Path(tmp_name))
        try:
            try:
                src.backup(dst, pages=max(1, pages), progress=progress)
                dst.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                dst.commit()
            except sqlite3.DatabaseError as exc:
                raise MigrationDatabaseError(
                    f"online_backup_failed:{source_db} -> {destination_db}"
                ) from exc
        finally:
            dst.close()
            src.close()
        os.replace(tmp_name, destination_db)
        tmp_name = ""
    finally:
        if fd >= 0:
            os.close(fd)
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass


def _require_source(source_db: Path) -> None:
    """Fail closed when the migration source is missing or not a regular file."""
    if not source_db.is_file():
        raise SourceAbsentError(str(source_db))


def _require_shadow(shadow_db: Path) -> None:
    """Fail closed when there is no shadow to cut over to."""
    if not shadow_db.is_file():
        raise ShadowAbsentError(str(shadow_db))


def _checkpoint_wal(path: Path) -> None:
    """Flush a store's WAL before cutover.

    A busy checkpoint means a live connection still holds the WAL, so cutover
    must fail closed instead of replacing a database that is being written.
    """
    if not path.exists():
        return
    conn = _connect(path)
    try:
        try:
            row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        except sqlite3.DatabaseError as exc:
            raise WalUnsafeError(f"wal_checkpoint_failed:{path.name}") from exc
        if row is not None and int(row["busy"]):
            raise WalUnsafeError(f"wal_checkpoint_busy:{path.name}")
    finally:
        conn.close()


def _next_rollback_path(base: Path) -> Path:
    """Return a rollback path that never overwrites an existing generation."""
    if not base.exists():
        return base
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    candidate = base.with_name(f"{base.name}.{stamp}")
    index = 0
    while candidate.exists():
        index += 1
        candidate = base.with_name(f"{base.name}.{stamp}.{index}")
    return candidate


def _remove_wal_sidecars(db_path: Path) -> None:
    """Drop stale WAL sidecars left by the database being replaced."""
    for suffix in ("-wal", "-shm"):
        try:
            Path(f"{db_path}{suffix}").unlink()
        except FileNotFoundError:
            pass


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _rows(conn: sqlite3.Connection, sql: str, args: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, args).fetchall()]


def schema_fingerprint(conn: sqlite3.Connection) -> str:
    rows = _rows(
        conn,
        """
        SELECT type, name, tbl_name, COALESCE(sql, '') AS sql
          FROM sqlite_master
         WHERE type IN ('table', 'index', 'trigger', 'view')
           AND name NOT LIKE 'sqlite_%'
         ORDER BY type, name, tbl_name, sql
        """,
    )
    payload = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def parity_snapshot(path: str | Path, *, repo_id: str = "") -> dict[str, Any]:
    path = Path(path)
    # ``sqlite3.Connection.__exit__`` commits/rolls back but does not close the
    # handle. An explicit closing context is required before Windows can
    # atomically replace a parity-checked database during cutover.
    try:
        with closing(_connect(path, readonly=True)) as conn:
            counts = {
                table: int(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])
                if _table_exists(conn, table)
                else 0
                for table in PARITY_TABLES
            }
            tasks = (
                _rows(
                    conn,
                    """
                    SELECT task_id, status, worker_status, COALESCE(claimed_by, '') AS claimed_by,
                           COALESCE(claimed_at, '') AS claimed_at, COALESCE(started_at, '') AS started_at,
                           COALESCE(completed_at, '') AS completed_at, COALESCE(archived_at, '') AS archived_at,
                           COALESCE(origin_thread_id, '') AS origin_thread_id
                      FROM tasks
                     ORDER BY task_id
                    """,
                )
                if _table_exists(conn, "tasks")
                else []
            )
            pending_outbox = (
                _rows(
                    conn,
                    """
                    SELECT task_id, origin_thread_id, transition, episode_id, request_id,
                           state, batch_id
                      FROM callback_outbox
                     WHERE state IN ('pending', 'inflight')
                     ORDER BY task_id, origin_thread_id, transition, episode_id, request_id, state, batch_id
                    """,
                )
                if _table_exists(conn, "callback_outbox")
                else []
            )
            pending_batches = (
                _rows(
                    conn,
                    """
                    SELECT batch_id, origin_thread_id, state, member_count
                      FROM callback_batches
                     WHERE state IN ('pending', 'inflight')
                     ORDER BY batch_id, origin_thread_id, state
                    """,
                )
                if _table_exists(conn, "callback_batches")
                else []
            )
            fingerprint = schema_fingerprint(conn)
            return {
                "repo_id": repo_id,
                "db_path": str(path.resolve(strict=False)),
                "schema_fingerprint": fingerprint,
                "counts": counts,
                "task_identities": tasks,
                "pending_delivery_identities": {
                    "callback_outbox": pending_outbox,
                    "callback_batches": pending_batches,
                },
            }
    except sqlite3.DatabaseError as exc:
        raise MigrationDatabaseError(f"parity_snapshot_failed:{path}") from exc


def _comparable(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_fingerprint": snapshot["schema_fingerprint"],
        "counts": snapshot["counts"],
        "task_identities": snapshot["task_identities"],
        "pending_delivery_identities": snapshot["pending_delivery_identities"],
    }


def compare_parity(source_db: str | Path, target_db: str | Path, *, repo_id: str = "") -> dict[str, Any]:
    source = parity_snapshot(source_db, repo_id=repo_id)
    target = parity_snapshot(target_db, repo_id=repo_id)
    ok = _comparable(source) == _comparable(target)
    return {
        "ok": ok,
        "source": source,
        "target": target,
        "mismatched_sections": [
            key for key in _comparable(source) if _comparable(source)[key] != _comparable(target)[key]
        ],
    }


def create_shadow(
    repo_root: str | Path,
    *,
    source_db: str | Path | None = None,
    canonical_db: str | Path | None = None,
    shadow_db: str | Path | None = None,
) -> dict[str, Any]:
    paths = resolve_task_store_paths(
        repo_root,
        source_db=source_db,
        canonical_db=canonical_db,
        shadow_db=shadow_db,
    )
    _require_source(paths.source_db)
    sqlite_online_backup(paths.source_db, paths.shadow_db)
    parity = compare_parity(paths.source_db, paths.shadow_db, repo_id=paths.repo_id)
    if not parity["ok"]:
        raise ParityMismatchError(json.dumps(parity["mismatched_sections"]))
    return {
        "action": "shadow_created",
        "cutover_performed": False,
        "paths": {
            "source_db": str(paths.source_db),
            "canonical_db": str(paths.canonical_db),
            "shadow_db": str(paths.shadow_db),
            "rollback_db": str(paths.rollback_db),
        },
        "parity": parity,
    }


def cutover_shadow(
    repo_root: str | Path,
    *,
    allow_cutover: bool = False,
    source_db: str | Path | None = None,
    canonical_db: str | Path | None = None,
    shadow_db: str | Path | None = None,
) -> dict[str, Any]:
    if not allow_cutover:
        raise CutoverRefusedError("explicit_coordinator_cutover_required")
    paths = resolve_task_store_paths(
        repo_root,
        source_db=source_db,
        canonical_db=canonical_db,
        shadow_db=shadow_db,
    )
    _require_source(paths.source_db)
    _require_shadow(paths.shadow_db)
    parity = compare_parity(paths.source_db, paths.shadow_db, repo_id=paths.repo_id)
    if not parity["ok"]:
        raise ParityMismatchError(json.dumps(parity["mismatched_sections"]))
    paths.canonical_db.parent.mkdir(parents=True, exist_ok=True)
    rollback_path: Path | None = None
    if paths.canonical_db.exists():
        _checkpoint_wal(paths.canonical_db)
        rollback_path = _next_rollback_path(paths.rollback_db)
        sqlite_online_backup(paths.canonical_db, rollback_path)
    os.replace(paths.shadow_db, paths.canonical_db)
    _remove_wal_sidecars(paths.canonical_db)
    post = compare_parity(paths.source_db, paths.canonical_db, repo_id=paths.repo_id)
    if not post["ok"]:
        if rollback_path is not None and rollback_path.exists():
            os.replace(rollback_path, paths.canonical_db)
        raise ParityMismatchError("post_cutover_parity_failed_rollback_restored")
    return {
        "action": "cutover_complete",
        "cutover_performed": True,
        "legacy_db_retained": str(paths.source_db),
        "rollback_db": str(rollback_path) if rollback_path is not None else "",
        "parity": post,
    }
