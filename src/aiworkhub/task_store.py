"""Canonical, repo-local AIWorkHub task store.

Self-contained SQLite task queue for the AIWorkHub dashboard. This module
never imports or executes ``AITools/taskdb.py`` or ``AITools/taskctl.py``:
every repository AIWorkHub attaches to gets its own canonical
``.aiworkhub/tasking/task_queue.sqlite``, independent of whether that
repository happens to have a host project's own AI tools installed. This is what makes
the dashboard safe to open against an arbitrary third-party repository
instead of silently reading whatever legacy queue happens to be reachable
from the bundled runtime's own install location.

Two responsibilities live here:

* ``storage_readiness`` -- verified registry/database authority readiness.
  Directory existence is never sufficient; manifest identity, storage
  registry repo_id, task_queue canonical_active authority, live cutover
  metadata, canonical DB existence, schema, and an SQLite quick_check must
  all pass.
* ``initialize_repository`` -- the one bounded, idempotent, fail-closed
  initialization action. Fresh repositories get canonical empty stores;
  older registries may migrate only their explicitly declared repo-local
  legacy SQLite sources. Legacy files are retained as rollback artifacts.
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
from typing import Any, Mapping

from .repository_state import (
    HUB_DIRNAME,
    ManifestInvalidError,
    ManifestMissingError,
    RepositoryState,
    RepositoryStateError,
    bootstrap_repository,
    inspect_repository,
)
from .storage_registry import (
    STORAGE_REGISTRY_REL,
    StorageRegistryError,
    StorageRegistryInvalidError,
    default_registry_payload,
    load_storage_registry,
    resolve_database_path,
)


SCHEMA_ID = "aiworkhub.task_store.v1"

CANONICAL_STATUSES: tuple[str, ...] = (
    "pending",
    "processing",
    "review",
    "blocked",
    "finished",
    "archived",
)

REQUIRED_TABLES: tuple[str, ...] = (
    "tasks",
    "task_events",
    "callback_outbox",
    "callback_batches",
)

REQUIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    "tasks": ("task_id", "runner", "topic", "status", "worker_status", "card_json", "origin_thread_id"),
    "callback_outbox": ("task_id", "origin_thread_id", "episode_id", "batch_id", "state"),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  task_id TEXT PRIMARY KEY,
  runner TEXT NOT NULL DEFAULT '',
  topic TEXT NOT NULL DEFAULT '',
  mode TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending',
  worker_status TEXT NOT NULL DEFAULT 'unclaimed',
  priority TEXT NOT NULL DEFAULT '',
  objective TEXT NOT NULL DEFAULT '',
  card_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  claimed_by TEXT,
  claimed_at TEXT,
  started_at TEXT,
  completed_at TEXT,
  origin_thread_id TEXT,
  archived_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS task_events (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL,
  event TEXT NOT NULL,
  runner TEXT NOT NULL DEFAULT '',
  payload_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS callback_outbox (
  outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL,
  origin_thread_id TEXT NOT NULL DEFAULT '',
  episode_id TEXT NOT NULL DEFAULT '',
  event_id TEXT NOT NULL DEFAULT '',
  request_id TEXT NOT NULL DEFAULT '',
  batch_id TEXT NOT NULL DEFAULT '',
  transition TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL DEFAULT 'pending',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  lease_id TEXT NOT NULL DEFAULT '',
  lease_expires_at TEXT NOT NULL DEFAULT '',
  last_error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_task_store_callback_outbox_state ON callback_outbox(state);

CREATE TABLE IF NOT EXISTS callback_batches (
  batch_id TEXT PRIMARY KEY,
  origin_thread_id TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL DEFAULT 'pending',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  lease_id TEXT NOT NULL DEFAULT '',
  lease_expires_at TEXT NOT NULL DEFAULT '',
  member_count INTEGER NOT NULL DEFAULT 0,
  not_before_at TEXT NOT NULL DEFAULT '',
  hard_failure_count INTEGER NOT NULL DEFAULT 0,
  last_failure_kind TEXT NOT NULL DEFAULT '',
  last_error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_task_store_callback_batches_state ON callback_batches(state);
"""


class TaskStoreError(RuntimeError):
    """Base class for canonical task-store failures. Always fail closed."""


class StorageNotReadyError(TaskStoreError):
    """Canonical storage is missing, corrupt, or authority-invalid."""


class InitializationRefusedError(TaskStoreError):
    """Initialization was refused (repo-id/path mismatch, invalid state)."""


@dataclass(frozen=True, slots=True)
class StorageReadiness:
    ready: bool
    reason: str
    repo_id: str
    canonical_db: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "reason": self.reason,
            "repo_id": self.repo_id,
            "canonical_db": self.canonical_db,
        }


def _connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    if not readonly:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path))
    else:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.execute("PRAGMA query_only=ON")
    conn.row_factory = sqlite3.Row
    return conn


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(dict(payload), fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def _atomic_init_schema(path: Path) -> None:
    """Create a fresh, schema-only canonical DB. Never touches an existing file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.fresh.", suffix=".tmp", dir=str(path.parent))
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        conn = sqlite3.connect(str(tmp))
        try:
            conn.executescript(SCHEMA)
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.commit()
        finally:
            conn.close()
        qc = quick_check(tmp)
        if qc != "ok" or not _schema_ok(tmp):
            raise TaskStoreError(f"fresh_schema_init_failed:quick_check={qc}")
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _sqlite_backup(source: Path, destination: Path) -> None:
    """Consistent SQLite backup; never byte-copy a possibly-live WAL DB."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_suffix(destination.suffix + ".migration.tmp")
    if tmp.exists():
        tmp.unlink()
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dst = sqlite3.connect(str(tmp))
    try:
        src.backup(dst)
        if dst.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise InitializationRefusedError(f"legacy_backup_quick_check_failed:{source.name}")
        dst.commit()
    finally:
        dst.close()
        src.close()
    os.replace(tmp, destination)


def _initialize_auxiliary_schema(db_id: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        if db_id == "source_graph":
            from . import source_graph
            conn.executescript(source_graph.SCHEMA)
        elif db_id == "transcript":
            conn.executescript(
                "CREATE TABLE IF NOT EXISTS documents(doc_id INTEGER PRIMARY KEY,source_id TEXT,timestamp TEXT,kind TEXT,content TEXT);"
                "CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(content);"
            )
        elif db_id == "memory":
            conn.executescript(
                "CREATE TABLE IF NOT EXISTS memories(id INTEGER PRIMARY KEY,key TEXT,value TEXT,tags TEXT,scope TEXT);"
                "CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(key,value,tags,scope);"
            )
        elif db_id == "kb":
            conn.executescript(
                "CREATE TABLE IF NOT EXISTS entries(id INTEGER PRIMARY KEY,key TEXT UNIQUE,title TEXT,body TEXT,category TEXT,tags TEXT,source_refs TEXT);"
                "CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5(key,title,body,category,tags);"
                "CREATE TABLE IF NOT EXISTS links(from_key TEXT,to_key TEXT,relation TEXT);"
            )
        else:
            conn.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT)")
        conn.commit()
    finally:
        conn.close()


def _reconcile_auxiliary_databases(repo: RepositoryState, registry_path: Path) -> dict[str, Any]:
    """Migrate shadow AI-tool DBs once, then activate canonical authority."""
    payload = json.loads(registry_path.read_text(encoding="utf-8"))
    rows = payload.get("databases") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise InitializationRefusedError("storage_registry_databases_invalid")
    migrated: list[str] = []
    initialized: list[str] = []
    activated: list[str] = []
    for entry in rows:
        if not isinstance(entry, dict) or entry.get("id") == "task_queue":
            continue
        db_id = str(entry.get("id") or "")
        rel = str(entry.get("canonical_durable_path") or "")
        if not db_id or not rel:
            raise InitializationRefusedError("auxiliary_database_entry_invalid")
        canonical = (repo.root / HUB_DIRNAME / rel).resolve()
        if not canonical.is_relative_to(repo.root):
            raise InitializationRefusedError(f"auxiliary_database_path_escape:{db_id}")
        if not canonical.exists():
            legacy_rel = str(entry.get("legacy_source") or "")
            legacy = (repo.root / legacy_rel).resolve() if legacy_rel else None
            if legacy is not None and legacy.is_relative_to(repo.root) and legacy.is_file():
                _sqlite_backup(legacy, canonical)
                migrated.append(db_id)
            else:
                _initialize_auxiliary_schema(db_id, canonical)
                initialized.append(db_id)
        conn = sqlite3.connect(f"file:{canonical}?mode=ro", uri=True)
        try:
            qc = conn.execute("PRAGMA quick_check(1)").fetchone()[0]
        finally:
            conn.close()
        if qc != "ok":
            raise InitializationRefusedError(f"auxiliary_db_quick_check_failed:{db_id}:{qc}")
        authority = entry.setdefault("authority", {})
        changed = db_id in migrated or db_id in initialized
        if not authority.get("canonical_active") or authority.get("state") != "canonical_active":
            authority.update({
                "state": "canonical_active", "canonical_active": True,
                "legacy_active": False, "live_cutover": True,
            })
            activated.append(db_id)
            changed = True
        integrity = entry.setdefault("integrity", {})
        integrity["state"] = "canonical_quick_check_ok"
        # Hash a database only on its one-time migration/activation.  In
        # particular, do not re-read a multi-gigabyte Source Graph on every
        # idempotent Initialize AIWorkHub call.
        if changed or not integrity.get("canonical_sha256"):
            integrity["canonical_sha256"] = sha256_file(canonical)
        migration = entry.setdefault("migration", {})
        migration.update({
            "generation": max(1, int(migration.get("generation") or 0)),
            "cutover_performed": True,
            "rollback_performed": False,
            "legacy_deleted": False,
            "source_read_only": True,
        })
    if migrated or initialized or activated:
        _atomic_write_json(registry_path, payload)
    return {"migrated": migrated, "initialized": initialized, "activated": activated}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def quick_check(path: Path) -> str:
    conn = _connect(path, readonly=True)
    try:
        row = conn.execute("PRAGMA quick_check").fetchone()
    finally:
        conn.close()
    return str(row[0] if row else "")


def _schema_ok(path: Path) -> bool:
    try:
        conn = _connect(path, readonly=True)
    except sqlite3.DatabaseError:
        return False
    try:
        names = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        if not all(table in names for table in REQUIRED_TABLES):
            return False
        for table, required in REQUIRED_COLUMNS.items():
            columns = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if not set(required).issubset(columns):
                return False
    finally:
        conn.close()
    return True


def _upgrade_compatible_schema(path: Path) -> bool:
    """Apply additive, data-preserving upgrades during explicit initialize."""
    conn = _connect(path)
    changed = False
    try:
        task_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
        if "topic" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN topic TEXT NOT NULL DEFAULT ''")
            changed = True
        if "origin_thread_id" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN origin_thread_id TEXT")
            changed = True
        # Older writers could persist the canonical topic in card_json while
        # leaving the indexed column empty.  Reconcile those rows on every
        # explicit initialize/upgrade; otherwise exact worker launch rejects
        # a valid task with topic_mismatch.
        updated = conn.execute(
            "UPDATE tasks SET topic=COALESCE(json_extract(card_json, '$.topic'), '') "
            "WHERE topic='' AND COALESCE(json_extract(card_json, '$.topic'), '')<>''"
        ).rowcount
        changed = bool(changed or updated)
        outbox_columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(callback_outbox)").fetchall()
        }
        for column in ("episode_id", "batch_id", "event_id", "request_id"):
            if column not in outbox_columns:
                conn.execute(
                    f"ALTER TABLE callback_outbox ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
                )
                changed = True
        if "attempts" not in outbox_columns:
            conn.execute(
                "ALTER TABLE callback_outbox ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0"
            )
            changed = True
        batch_columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(callback_batches)").fetchall()
        }
        for column in ("not_before_at", "last_failure_kind", "last_error"):
            if column not in batch_columns:
                conn.execute(
                    f"ALTER TABLE callback_batches ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
                )
                changed = True
        for column in ("attempts", "member_count", "hard_failure_count"):
            if column not in batch_columns:
                conn.execute(
                    f"ALTER TABLE callback_batches ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0"
                )
                changed = True
        conn.commit()
    finally:
        conn.close()
    return changed


def canonical_status(row: Mapping[str, Any]) -> str:
    """Compact lifecycle state, mirroring AITools/taskdb.py::canonical_status
    but implemented independently -- this module never imports that one."""
    if str(row.get("archived_at") or "").strip():
        return "archived"
    status = str(row.get("status") or "").strip().lower()
    worker_status = str(row.get("worker_status") or "").strip().lower()
    if status in {"finished", "completed", "stale_already_done"} or worker_status == "done":
        return "finished"
    if status.startswith("blocked") or worker_status.startswith(("blocked", "deferred")):
        return "blocked"
    if status in {"review", "ready_for_review", "codex_review", "awaiting_review"} or worker_status in {
        "review",
        "ready_for_review",
        "codex_review",
        "awaiting_review",
    }:
        return "review"
    if status in {"processing", "in_progress"} or worker_status in {"claimed", "in_progress"}:
        return "processing"
    return "pending"


def storage_readiness(root: str | Path) -> StorageReadiness:
    """Verified registry/database authority readiness.

    Directory existence alone is never sufficient. Every one of the
    following must independently pass: manifest identity, storage registry
    repo_id, task_queue canonical_active authority, live cutover metadata,
    canonical DB existence, schema, and an SQLite quick_check.
    """
    try:
        repo = inspect_repository(root)
    except RepositoryStateError as exc:
        return StorageReadiness(False, f"manifest_invalid:{exc}", "", "")

    try:
        registry = load_storage_registry(repo.root, expected_repo_id=repo.manifest.repo_id)
    except StorageRegistryError as exc:
        return StorageReadiness(False, f"registry_invalid:{exc}", repo.manifest.repo_id, "")

    if registry.payload.get("repo_id") != repo.manifest.repo_id:
        return StorageReadiness(False, "registry_repo_id_mismatch", repo.manifest.repo_id, "")

    try:
        db = registry.databases["task_queue"]
    except KeyError:
        return StorageReadiness(False, "task_queue_record_missing", repo.manifest.repo_id, "")

    if not (db.canonical_active and not db.legacy_active and db.live_cutover):
        return StorageReadiness(False, "task_queue_authority_not_canonical", repo.manifest.repo_id, "")

    try:
        canonical_db = resolve_database_path(registry, "task_queue")
    except StorageRegistryError as exc:
        return StorageReadiness(False, f"database_path_invalid:{exc}", repo.manifest.repo_id, "")

    if not canonical_db.is_file():
        return StorageReadiness(False, "canonical_db_missing", repo.manifest.repo_id, str(canonical_db))

    if not _schema_ok(canonical_db):
        return StorageReadiness(False, "canonical_schema_incomplete", repo.manifest.repo_id, str(canonical_db))

    try:
        qc = quick_check(canonical_db)
    except sqlite3.DatabaseError:
        return StorageReadiness(False, "canonical_db_corrupt", repo.manifest.repo_id, str(canonical_db))
    if qc != "ok":
        return StorageReadiness(False, f"quick_check_failed:{qc}", repo.manifest.repo_id, str(canonical_db))

    return StorageReadiness(True, "ready", repo.manifest.repo_id, str(canonical_db))


def _require_ready(root: str | Path) -> tuple[StorageReadiness, Path]:
    readiness = storage_readiness(root)
    if not readiness.ready:
        raise StorageNotReadyError(readiness.reason)
    return readiness, Path(readiness.canonical_db)


def exact_status_counts(root: str | Path) -> dict[str, int]:
    """Exact per-canonical-status totals across the whole canonical queue.

    A pure SELECT over (archived_at, status, worker_status); never a
    per-row payload, never AITools/taskdb.py.
    """
    _readiness, db_path = _require_ready(root)
    counts = {status: 0 for status in CANONICAL_STATUSES}
    conn = _connect(db_path, readonly=True)
    try:
        rows = conn.execute("SELECT archived_at, status, worker_status FROM tasks").fetchall()
    finally:
        conn.close()
    for row in rows:
        counts[canonical_status(dict(row))] += 1
    return counts


def list_tasks(root: str | Path, *, status: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
    """List canonical tasks, optionally filtered to one canonical status."""
    _readiness, db_path = _require_ready(root)
    conn = _connect(db_path, readonly=True)
    try:
        rows = conn.execute(
            "SELECT task_id, runner, "
            "COALESCE(NULLIF(topic, ''), json_extract(card_json, '$.topic'), '') AS topic, "
            "mode, status, worker_status, priority, objective, "
            "created_at, updated_at, claimed_by, claimed_at, started_at, completed_at, archived_at "
            "FROM tasks ORDER BY updated_at DESC"
        ).fetchall()
    finally:
        conn.close()
    bounded_limit = max(1, min(int(limit), 5000))
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["status"] = canonical_status(item)
        if status and status != "all" and item["status"] != status:
            continue
        result.append(item)
        if len(result) >= bounded_limit:
            break
    return result


def get_task(root: str | Path, task_id: str) -> dict[str, Any] | None:
    """Canonical detail for exactly one bounded task_id, or None if unknown."""
    _readiness, db_path = _require_ready(root)
    conn = _connect(db_path, readonly=True)
    try:
        row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    card = dict(row)
    try:
        card_json = json.loads(card.get("card_json") or "{}")
    except json.JSONDecodeError:
        card_json = {}
    if isinstance(card_json, dict):
        card = {**card_json, **card}
        if not card.get("topic") and card_json.get("topic"):
            card["topic"] = card_json["topic"]
        if not card.get("origin_thread_id") and card_json.get("origin_thread_id"):
            card["origin_thread_id"] = card_json["origin_thread_id"]
    card["status"] = canonical_status(card)
    return card


def callback_bridge_health(root: str | Path) -> dict[str, Any]:
    """Redacted-safe callback bridge health: bound/unbound task counts and
    per-state outbox counts. Never exposes a full origin_thread_id."""
    _readiness, db_path = _require_ready(root)
    conn = _connect(db_path, readonly=True)
    try:
        counts = {state: 0 for state in ("pending", "inflight", "delivered", "dead_letter", "superseded")}
        for row in conn.execute("SELECT state, COUNT(*) AS n FROM callback_outbox GROUP BY state"):
            counts[str(row["state"])] = int(row["n"])
        batch_counts = {
            state: 0
            for state in ("pending", "inflight", "delivered", "dead_letter", "superseded")
        }
        for row in conn.execute("SELECT state, COUNT(*) AS n FROM callback_batches GROUP BY state"):
            batch_counts[str(row["state"])] = int(row["n"])
        inflight_batch_member_count = int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM callback_outbox "
                "WHERE batch_id IN (SELECT batch_id FROM callback_batches WHERE state='inflight')"
            ).fetchone()["n"]
        )
        total_tasks = int(conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"])
        bound_task_count = int(
            conn.execute(
                "SELECT COUNT(DISTINCT task_id) AS n FROM callback_outbox WHERE origin_thread_id != ''"
            ).fetchone()["n"]
        )
    finally:
        conn.close()
    return {
        "ok": True,
        "total": sum(counts.values()),
        "by_state": counts,
        "batches": {
            "total": sum(batch_counts.values()),
            "by_state": batch_counts,
            "inflight_batch_member_count": inflight_batch_member_count,
        },
        "bound_task_count": bound_task_count,
        "unbound_task_count": max(0, total_tasks - bound_task_count),
    }


def database_identity(root: str | Path) -> dict[str, Any]:
    """Return bounded, non-secret identity for the bound canonical queue."""
    readiness, db_path = _require_ready(root)
    repo = inspect_repository(root, expected_repo_id=readiness.repo_id)
    conn = _connect(db_path, readonly=True)
    try:
        row = conn.execute("PRAGMA user_version").fetchone()
        user_version = int(row[0] if row else 0)
        schema_rows = conn.execute(
            "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY name"
        ).fetchall()
        schema_sql = "\n".join(str(item[0]) for item in schema_rows if item[0])
    finally:
        conn.close()
    material = "\n".join((str(repo.root), str(db_path.resolve()), str(user_version), schema_sql))
    return {
        "repository_root": str(repo.root),
        "repo_id": repo.manifest.repo_id,
        "db_path": str(db_path.resolve()),
        "db_schema_user_version": user_version,
        "db_identity_fingerprint": hashlib.sha256(material.encode("utf-8")).hexdigest()[:32],
    }


def archive_task(
    root: str | Path,
    task_id: str,
    *,
    actor: str = "dashboard",
    reason: str = "",
) -> tuple[bool, str]:
    """Archive one non-processing task in the bound canonical queue."""
    _readiness, db_path = _require_ready(root)
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT status, worker_status, archived_at, card_json FROM tasks WHERE task_id=?",
            (task_id,),
        ).fetchone()
        if row is None:
            return False, "task_not_found"
        if str(row["archived_at"] or "").strip():
            return True, "already_archived"
        if canonical_status(dict(row)) == "processing":
            return False, "archive_processing_forbidden"
        now = datetime.now(timezone.utc).isoformat()
        try:
            card = json.loads(str(row["card_json"] or "{}"))
        except json.JSONDecodeError:
            card = {}
        if not isinstance(card, dict):
            card = {}
        card["archived_at"] = now
        conn.execute(
            "UPDATE tasks SET archived_at=?, updated_at=?, card_json=? WHERE task_id=?",
            (now, now, json.dumps(card, ensure_ascii=False, sort_keys=True), task_id),
        )
        conn.execute(
            "INSERT INTO task_events(task_id, event, runner, payload_json, created_at) "
            "VALUES (?, 'archived', ?, ?, ?)",
            (task_id, actor, json.dumps({"reason": reason[:200]}, sort_keys=True), now),
        )
        conn.commit()
        return True, "archived"
    finally:
        conn.close()


def restore_task(
    root: str | Path,
    task_id: str,
    *,
    actor: str = "dashboard",
    reason: str = "",
) -> tuple[bool, str]:
    """Restore one archived task in the bound canonical queue."""
    _readiness, db_path = _require_ready(root)
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT archived_at, card_json FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        if row is None:
            return False, "task_not_found"
        if not str(row["archived_at"] or "").strip():
            return True, "not_archived"
        now = datetime.now(timezone.utc).isoformat()
        try:
            card = json.loads(str(row["card_json"] or "{}"))
        except json.JSONDecodeError:
            card = {}
        if not isinstance(card, dict):
            card = {}
        card.pop("archived_at", None)
        conn.execute(
            "UPDATE tasks SET archived_at='', updated_at=?, card_json=? WHERE task_id=?",
            (now, json.dumps(card, ensure_ascii=False, sort_keys=True), task_id),
        )
        conn.execute(
            "INSERT INTO task_events(task_id, event, runner, payload_json, created_at) "
            "VALUES (?, 'restored', ?, ?, ?)",
            (task_id, actor, json.dumps({"reason": reason[:200]}, sort_keys=True), now),
        )
        conn.commit()
        return True, "restored"
    finally:
        conn.close()


def get_task_events(root: str | Path, task_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
    """Return bounded audit events for one task from the canonical queue."""
    _readiness, db_path = _require_ready(root)
    conn = _connect(db_path, readonly=True)
    try:
        rows = conn.execute(
            "SELECT event, runner, payload_json AS payload, created_at FROM task_events "
            "WHERE task_id=? ORDER BY event_id DESC LIMIT ?",
            (task_id, max(1, min(int(limit), 500))),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def _activate_canonical_authority(registry_path: Path, canonical_db: Path) -> dict[str, Any]:
    """Promote task_queue authority to canonical_active for a freshly
    initialized (never-legacy-imported) repository."""
    payload = json.loads(registry_path.read_text(encoding="utf-8"))
    record = None
    for item in payload.get("databases") or []:
        if isinstance(item, dict) and item.get("id") == "task_queue":
            record = item
            break
    if record is None:
        raise InitializationRefusedError("task_queue_record_missing_in_registry")

    db_hash = sha256_file(canonical_db)
    authority = record.setdefault("authority", {})
    migration = record.setdefault("migration", {})
    integrity = record.setdefault("integrity", {})
    generation = int(migration.get("generation") or 0) + 1
    authority.update(
        {
            "state": "canonical_active",
            "canonical_active": True,
            "legacy_active": False,
            "live_cutover": True,
        }
    )
    migration.update(
        {
            "generation": generation,
            "source_read_only": True,
            "rollback_source_hash": db_hash,
            "cutover_performed": True,
            "legacy_deleted": False,
            "rollback_performed": False,
        }
    )
    integrity.update(
        {
            "state": "fresh_repository_initialized",
            "rollback_source_sha256": db_hash,
        }
    )
    _atomic_write_json(registry_path, payload)
    return record


def initialize_repository(
    root: str | Path,
    *,
    expected_repo_id: str | None = None,
) -> dict[str, Any]:
    """The one bounded, idempotent, fail-closed initialization action.

    Creates or validates the manifest, registry and canonical task queue.
    A fresh repository receives empty compatible auxiliary stores.  An older
    AIWorkHub registry that explicitly names a repository-relative read-only
    ``legacy_source`` is migrated once with SQLite's online backup API and
    switched to canonical authority; legacy files are never deleted.
    """
    normalized_expected = str(expected_repo_id or "").strip() or None

    try:
        repo: RepositoryState = inspect_repository(root)
        if normalized_expected is not None and repo.manifest.repo_id != normalized_expected:
            raise InitializationRefusedError("repo_id_path_mismatch")
    except ManifestMissingError:
        repo = bootstrap_repository(root, repo_id=normalized_expected)
    except ManifestInvalidError as exc:
        raise InitializationRefusedError(f"manifest_invalid:{exc}") from exc

    registry_path = repo.root / STORAGE_REGISTRY_REL
    if not registry_path.exists():
        _atomic_write_json(registry_path, default_registry_payload(repo.manifest.repo_id))

    try:
        registry = load_storage_registry(repo.root, expected_repo_id=repo.manifest.repo_id)
    except StorageRegistryError as exc:
        raise InitializationRefusedError(f"storage_registry_invalid:{exc}") from exc

    canonical_db = resolve_database_path(registry, "task_queue")
    created_db = False
    if not canonical_db.is_file():
        _atomic_init_schema(canonical_db)
        created_db = True
    else:
        upgraded_schema = _upgrade_compatible_schema(canonical_db)
        if not _schema_ok(canonical_db):
            raise InitializationRefusedError("canonical_db_present_but_schema_incomplete")
        qc = quick_check(canonical_db)
        if qc != "ok":
            raise InitializationRefusedError(f"canonical_db_quick_check_failed:{qc}")

    db = registry.databases["task_queue"]
    activated = False
    if not (db.canonical_active and not db.legacy_active and db.live_cutover):
        _activate_canonical_authority(registry_path, canonical_db)
        activated = True

    auxiliary = _reconcile_auxiliary_databases(repo, registry_path)

    readiness = storage_readiness(repo.root)
    return {
        "ok": readiness.ready,
        "repo_id": repo.manifest.repo_id,
        "repo_name": repo.manifest.repo_name,
        "created_manifest": normalized_expected is None and created_db,
        "created_canonical_db": created_db,
        "upgraded_canonical_schema": bool(locals().get("upgraded_schema", False)),
        "activated_canonical_authority": activated,
        "legacy_imported": bool(auxiliary["migrated"]),
        "legacy_deleted": False,
        "auxiliary_storage": auxiliary,
        "storage": readiness.as_dict(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


__all__ = [
    "CANONICAL_STATUSES",
    "InitializationRefusedError",
    "REQUIRED_TABLES",
    "SCHEMA",
    "SCHEMA_ID",
    "StorageNotReadyError",
    "StorageReadiness",
    "TaskStoreError",
    "archive_task",
    "callback_bridge_health",
    "canonical_status",
    "database_identity",
    "exact_status_counts",
    "get_task_events",
    "get_task",
    "initialize_repository",
    "list_tasks",
    "quick_check",
    "restore_task",
    "sha256_file",
    "storage_readiness",
]
