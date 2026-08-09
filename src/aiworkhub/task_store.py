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
from .provider_tool_guards import ProviderGuardError, apply_repository_guards
from . import task_fsm


SCHEMA_ID = "aiworkhub.task_store.v1"

CANONICAL_STATUSES: tuple[str, ...] = (
    "pending",
    "processing",
    "review",
    "blocked",
    "finished",
    "archived",
)

# Card fields below describe only the *current* execution episode.  A task
# rejected back to ``pending`` may retain them in card_json for review/audit
# display, but the next genuine claim must not inherit that stale terminal
# truth.  Immutable history remains in task_events (including the original
# terminal_review/reject_review rows).  This list deliberately excludes task
# identity, requirements, validation commands and other durable card fields.
CURRENT_EPISODE_CARD_FIELDS: tuple[str, ...] = (
    "terminal_review",
    "terminal_substatus",
    "terminal_outcome",
    "terminal_review_reason",
    "terminal_worker",
    "terminal_review_disposition",
    "deterministic_verification",
    "review_requested_by",
    "review_outcome",
    "review_reason",
    "validation_status",
    "validation_error",
    "validation_output",
    "blocker_reason",
    "launch_error",
    "launch_failed",
    "accepted_request_id",
    "accepted_by",
    "accepted_at",
    "accept_evidence",
)

# ``card_json`` is the durable semantic task payload.  ``get_task`` also
# overlays authoritative SQL lifecycle columns for callers, but that decoded
# projection must never be written back verbatim: doing so embeds the previous
# raw ``card_json`` string inside the next ``card_json`` value and doubles the
# task receipt on every rework episode.
CARD_PERSISTENCE_ENVELOPE_FIELDS: frozenset[str] = frozenset({
    "card_json",
    "created_at",
    "updated_at",
    "claimed_at",
    "started_at",
    "completed_at",
    "archived_at",
})


def persistable_card_payload(card: Mapping[str, Any]) -> dict[str, Any]:
    """Return semantic card fields without the SQLite/read projection.

    This is intentionally shallow.  A historical top-level ``card_json``
    contains the complete recursive predecessor string, so dropping that one
    field removes the whole amplification chain while preserving legitimate
    structured review feedback.
    """

    return {
        str(key): value
        for key, value in card.items()
        if str(key) not in CARD_PERSISTENCE_ENVELOPE_FIELDS
    }


def begin_claim_episode(card: dict[str, Any]) -> dict[str, Any]:
    """Clear stale current-episode metadata and return bounded audit context.

    Callers invoke this only inside the same transaction that successfully
    changes an unclaimed/pending row to processing.  The returned summary may
    be embedded in the new ``claim_start`` event; it never replaces the full,
    append-only terminal events already persisted for the preceding episode.
    """

    prior = {
        key: card.get(key)
        for key in (
            "terminal_substatus",
            "validation_status",
            "validation_error",
            "blocker_reason",
            "launch_error",
        )
        if card.get(key) not in (None, "", [], {})
    }
    for key in CURRENT_EPISODE_CARD_FIELDS:
        card.pop(key, None)
    return prior

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
  recovery_count INTEGER NOT NULL DEFAULT 0,
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

CREATE TABLE IF NOT EXISTS task_retention_batches (
  batch_id TEXT PRIMARY KEY,
  task_count INTEGER NOT NULL DEFAULT 0,
  payload_json TEXT NOT NULL DEFAULT '{}',
  quarantined_at TEXT NOT NULL,
  restore_deadline TEXT NOT NULL,
  restored_at TEXT NOT NULL DEFAULT '',
  purged_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS task_retention_audit (
  audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id TEXT NOT NULL,
  event TEXT NOT NULL,
  task_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
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
            from . import context_graph

            conn.executescript(
                "CREATE TABLE IF NOT EXISTS documents(doc_id INTEGER PRIMARY KEY,source_id TEXT,timestamp TEXT,kind TEXT,content TEXT);"
                "CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(content);"
            )
            conn.executescript(context_graph.SCHEMA)
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
        # Repository-local AI tools are first-class canonical authorities.
        # Leaving these stores in shadow makes Source Graph, Session Manager,
        # AI Memory and KB unusable to both managers and workers.
        if not authority or authority.get("state") != "canonical_active":
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
            db_hash = sha256_file(canonical)
            integrity["canonical_sha256"] = db_hash
        else:
            db_hash = integrity["canonical_sha256"]
        # Keep matching rollback hashes for migrated stores.
        integrity["rollback_source_sha256"] = db_hash
        migration = entry.setdefault("migration", {})
        migration.update({
            "generation": max(1, int(migration.get("generation") or 0)),
            "cutover_performed": True,
            "rollback_performed": False,
            "legacy_deleted": False,
            "source_read_only": True,
            "rollback_source_hash": db_hash,
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
        # v0.8.38 and earlier could serialize ``task_store.get_task``'s full
        # decoded row during reject-review.  That projection included the raw
        # previous card_json string, recursively inflating cards and worker
        # prompts into hundreds of thousands of tokens.  One json_remove is
        # sufficient because the entire older chain lives below this key.
        compacted = conn.execute(
            "UPDATE tasks SET card_json=json_remove(card_json, '$.card_json') "
            "WHERE json_valid(card_json)=1 "
            "AND json_type(card_json, '$.card_json') IS NOT NULL"
        ).rowcount
        changed = bool(changed or compacted)
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
    but implemented independently -- this module never imports that one.

    ``superseded`` is a durable reviewer lifecycle state: a reviewer has
    declared this task displaced by another canonical task.  It must be
    projected as its own public state rather than collapsed back onto
    ``pending`` so reviewer-child disposition truth survives.

    Precedence is intentionally unchanged for the existing public
    archived/finished/blocked/review/processing states; ``superseded``
    slots strictly between ``processing`` and the ``pending`` fallback so
    those five keep their established ordering.
    """
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
    if status == "superseded" or worker_status == "superseded":
        return "superseded"
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


def canonical_db_path(root: str | Path) -> Path:
    """Return the verified canonical task database path for repo-local services."""
    _readiness, path = _require_ready(root)
    return path


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


def _decode_task_card(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
    card = dict(row)
    raw_card_json = card.pop("card_json", "{}")
    try:
        card_json = json.loads(raw_card_json or "{}")
    except json.JSONDecodeError:
        card_json = {}
    if isinstance(card_json, dict):
        # Repair the public/read projection immediately even before the next
        # explicit initialize runs the durable compaction migration.
        card_json.pop("card_json", None)
        card = {**card_json, **card}
        if not card.get("topic") and card_json.get("topic"):
            card["topic"] = card_json["topic"]
        if not card.get("origin_thread_id") and card_json.get("origin_thread_id"):
            card["origin_thread_id"] = card_json["origin_thread_id"]
    card["status"] = canonical_status(card)
    return card


def list_task_cards(root: str | Path, *, limit: int = 500) -> list[dict[str, Any]]:
    """Return bounded canonical task cards with one readiness check/query.

    Dashboard aggregates previously performed ``list_tasks`` followed by one
    ``get_task`` connection and SQLite ``quick_check`` per row. On mature
    repositories that N+1 pattern made every dashboard refresh take tens of
    seconds. This batch reader preserves the identical canonical card decode
    while keeping the operation to one verified database snapshot.
    """
    _readiness, db_path = _require_ready(root)
    bounded_limit = max(1, min(int(limit), 5000))
    conn = _connect(db_path, readonly=True)
    try:
        rows = conn.execute(
            "SELECT * FROM tasks ORDER BY updated_at DESC LIMIT ?",
            (bounded_limit,),
        ).fetchall()
    finally:
        conn.close()
    return [_decode_task_card(row) for row in rows]


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
    return _decode_task_card(row)


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
        attempt_row = conn.execute(
            "SELECT COALESCE(SUM(attempts), 0) AS attempts_total, "
            "COALESCE(SUM(CASE WHEN attempts > 1 THEN attempts - 1 ELSE 0 END), 0) AS retry_count, "
            "COALESCE(MAX(attempts), 0) AS max_attempts FROM callback_outbox"
        ).fetchone()
        last_delivered = conn.execute(
            "SELECT task_id, transition, updated_at FROM callback_outbox "
            "WHERE state='delivered' ORDER BY updated_at DESC, outbox_id DESC LIMIT 1"
        ).fetchone()
        last_dead_letter = conn.execute(
            "SELECT task_id, transition, updated_at, last_error FROM callback_outbox "
            "WHERE state='dead_letter' ORDER BY updated_at DESC, outbox_id DESC LIMIT 1"
        ).fetchone()
        oldest_pending = conn.execute(
            "SELECT MIN(created_at) AS created_at FROM callback_outbox "
            "WHERE state IN ('pending', 'inflight')"
        ).fetchone()
        batch_attempt_row = conn.execute(
            "SELECT COALESCE(SUM(attempts), 0) AS attempts_total, "
            "COALESCE(SUM(CASE WHEN attempts > 1 THEN attempts - 1 ELSE 0 END), 0) AS retry_count, "
            "COALESCE(MAX(attempts), 0) AS max_attempts FROM callback_batches"
        ).fetchone()
        last_dead_batch = conn.execute(
            "SELECT member_count, updated_at, last_error FROM callback_batches "
            "WHERE state='dead_letter' ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    backlog_count = counts["pending"] + counts["inflight"]
    return {
        "ok": True,
        "total": sum(counts.values()),
        "by_state": counts,
        "backlog_count": backlog_count,
        "attempts_total": int(attempt_row["attempts_total"]),
        "retry_count": int(attempt_row["retry_count"]),
        "max_attempts": int(attempt_row["max_attempts"]),
        "oldest_pending_at": str(oldest_pending["created_at"] or ""),
        "last_delivered_task_id": str(last_delivered["task_id"] if last_delivered else ""),
        "last_delivered_transition": str(last_delivered["transition"] if last_delivered else ""),
        "last_delivered_at": str(last_delivered["updated_at"] if last_delivered else ""),
        "last_dead_letter_task_id": str(last_dead_letter["task_id"] if last_dead_letter else ""),
        "last_dead_letter_transition": str(last_dead_letter["transition"] if last_dead_letter else ""),
        "last_dead_letter_at": str(last_dead_letter["updated_at"] if last_dead_letter else ""),
        "last_dead_letter_error": str(last_dead_letter["last_error"] if last_dead_letter else "")[:500],
        "batches": {
            "total": sum(batch_counts.values()),
            "by_state": batch_counts,
            "inflight_batch_member_count": inflight_batch_member_count,
            "attempts_total": int(batch_attempt_row["attempts_total"]),
            "retry_count": int(batch_attempt_row["retry_count"]),
            "max_attempts": int(batch_attempt_row["max_attempts"]),
            "last_dead_letter_batch_member_count": int(last_dead_batch["member_count"] if last_dead_batch else 0),
            "last_dead_letter_batch_at": str(last_dead_batch["updated_at"] if last_dead_batch else ""),
            "last_dead_letter_batch_error": str(last_dead_batch["last_error"] if last_dead_batch else "")[:500],
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
    allow_processing: bool = False,
    operation: str = "archived",
) -> tuple[bool, str]:
    """Archive one task in the bound canonical queue without deleting audit history."""
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
        if canonical_status(dict(row)) == "processing" and not allow_processing:
            return False, "archive_processing_forbidden"
        now = datetime.now(timezone.utc).isoformat()
        try:
            card = json.loads(str(row["card_json"] or "{}"))
        except json.JSONDecodeError:
            card = {}
        if not isinstance(card, dict):
            card = {}
        card["archived_at"] = now
        card["archive_operation"] = operation
        card["archive_reason"] = reason[:200]
        conn.execute(
            "UPDATE tasks SET archived_at=?, updated_at=?, card_json=? WHERE task_id=?",
            (now, now, json.dumps(card, ensure_ascii=False, sort_keys=True), task_id),
        )
        conn.execute(
            "INSERT INTO task_events(task_id, event, runner, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                task_id,
                operation,
                actor,
                json.dumps({"reason": reason[:200]}, sort_keys=True),
                now,
            ),
        )
        conn.commit()
        return True, operation
    finally:
        conn.close()


def mark_terminal_review(
    root: str | Path,
    task_id: str,
    *,
    runner: str,
    substatus: str,
    evidence: Mapping[str, Any] | None = None,
) -> tuple[bool, str]:
    """Route a launched terminal outcome to Codex review, preserving evidence."""
    _readiness, db_path = _require_ready(root)
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT runner, status, worker_status, archived_at, claimed_by, card_json "
            "FROM tasks WHERE task_id=?",
            (task_id,),
        ).fetchone()
        if row is None:
            return False, "task_not_found"
        if str(row["runner"] or "") != runner:
            return False, "runner_mismatch"
        now = datetime.now(timezone.utc).isoformat()
        current_status = canonical_status(dict(row))
        legal, fsm_reason = task_fsm.check_terminal_review_transition(current_status, substatus)
        if not legal:
            conn.execute(
                "INSERT INTO task_events(task_id, event, runner, payload_json, created_at) "
                "VALUES (?, 'illegal_transition_rejected', ?, ?, ?)",
                (
                    task_id,
                    runner,
                    json.dumps(
                        {
                            "attempted_substatus": substatus[:120],
                            "current_status": current_status,
                            "reason": fsm_reason,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    now,
                ),
            )
            conn.commit()
            return False, fsm_reason
        try:
            card = json.loads(str(row["card_json"] or "{}"))
        except json.JSONDecodeError:
            card = {}
        if not isinstance(card, dict):
            card = {}
        evidence_payload = dict(evidence or {})
        try:
            claim_epoch = max(0, int(card.get("claim_epoch") or 0))
        except (TypeError, ValueError):
            claim_epoch = 0
        deterministic_verification = task_fsm.deterministic_verification(
            substatus,
            evidence_payload.get("validation"),
            evidence_payload.get("required_outputs"),
            claim_epoch=claim_epoch,
        )
        terminal = {
            "substatus": substatus[:120],
            "evidence": evidence_payload,
            "deterministic_verification": deterministic_verification,
            "recorded_at": now,
            "runner": runner,
            "claim_epoch": claim_epoch,
        }
        card["terminal_review"] = terminal
        card["terminal_substatus"] = terminal["substatus"]
        card["deterministic_verification"] = deterministic_verification
        card["review_requested_by"] = runner
        conn.execute(
            "UPDATE tasks SET status='review', worker_status='review', claimed_by=?, "
            "completed_at=COALESCE(NULLIF(completed_at, ''), ?), updated_at=?, card_json=? "
            "WHERE task_id=?",
            (runner, now, now, json.dumps(card, ensure_ascii=False, sort_keys=True), task_id),
        )
        conn.execute(
            "INSERT INTO task_events(task_id, event, runner, payload_json, created_at) "
            "VALUES (?, 'terminal_review', ?, ?, ?)",
            (task_id, runner, json.dumps(terminal, ensure_ascii=False, sort_keys=True), now),
        )
        conn.commit()
        return True, "review"
    finally:
        conn.close()


def mark_launch_failed(
    root: str | Path,
    task_id: str,
    *,
    runner: str,
    reason: str = "",
    request_id: str = "",
) -> tuple[bool, str]:
    """Record a failed launch without fabricating worker review evidence.

    A launch failure is an operational blocker, not a completed worker result.
    The transition therefore ends at ``blocked/launch_failed`` and is guarded
    by the exact launch request attached to the claim.  The compare-and-swap
    update prevents a losing concurrent launcher from blocking the request
    that actually acquired the card.
    """
    _readiness, db_path = _require_ready(root)
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT runner, status, worker_status, claimed_by, card_json "
            "FROM tasks WHERE task_id=?",
            (task_id,),
        ).fetchone()
        if row is None:
            return False, "task_not_found"
        if str(row["runner"] or "") != runner:
            return False, "runner_mismatch"
        if canonical_status(dict(row)) != "processing":
            return False, f"not_processing:current={canonical_status(dict(row))}"
        if str(row["worker_status"] or "") != "claimed":
            return False, f"not_claimed:current={row['worker_status']}"
        if str(row["claimed_by"] or "") != runner:
            return False, "claim_owner_mismatch"
        raw_card_json = str(row["card_json"] or "{}")
        try:
            card = json.loads(raw_card_json)
        except json.JSONDecodeError:
            card = {}
        if not isinstance(card, dict):
            card = {}
        attached_request_id = str(card.get("launch_request_id") or "")
        if request_id:
            if attached_request_id != request_id:
                return False, "launch_request_mismatch"
        elif attached_request_id:
            return False, "launch_request_id_required"

        now = datetime.now(timezone.utc).isoformat()
        bounded_reason = reason[:500]
        card.update(
            status="blocked",
            worker_status="launch_failed",
            terminal_substatus="launch_failed",
            launch_failed=True,
            launch_error=bounded_reason,
            blocker_reason=bounded_reason,
            blocked_at=now,
            blocked_by=runner,
        )
        cur = conn.execute(
            "UPDATE tasks SET status='blocked', worker_status='launch_failed', "
            "completed_at=?, updated_at=?, card_json=? "
            "WHERE task_id=? AND status='processing' AND worker_status='claimed' "
            "AND claimed_by=? AND card_json=?",
            (
                now,
                now,
                json.dumps(card, ensure_ascii=False, sort_keys=True),
                task_id,
                runner,
                raw_card_json,
            ),
        )
        if cur.rowcount != 1:
            conn.rollback()
            return False, "launch_failure_transition_conflict"
        event = {
            "reason": bounded_reason,
            "request_id": request_id[:120],
            "transition": "processing->blocked",
            "worker_status": "launch_failed",
            "recorded_at": now,
            "runner": runner,
        }
        conn.execute(
            "INSERT INTO task_events(task_id, event, runner, payload_json, created_at) "
            "VALUES (?, 'launch_failed', ?, ?, ?)",
            (
                task_id,
                runner,
                json.dumps(event, ensure_ascii=False, sort_keys=True),
                now,
            ),
        )
        conn.commit()
        return True, "blocked"
    finally:
        conn.close()


def mark_terminal_failure(
    root: str | Path,
    task_id: str,
    *,
    runner: str,
    substatus: str,
    evidence: Mapping[str, Any] | None = None,
    request_id: str = "",
) -> tuple[bool, str]:
    """Record a post-launch failure outside the actionable review queue.

    A timed-out/cancelled/crashed worker produced no reviewable candidate by
    definition.  Preserve its exact evidence and declared gate denominator,
    emit a truthful terminal substatus, and end in the canonical ``blocked``
    bucket instead of fabricating ``status=review``.
    """
    allowed = {
        "timed_out",
        "token_budget_exceeded",
        "output_budget_exceeded",
        "worker_failed",
        "finalize_failed",
        "cancelled",
        "liveness_lost",
    }
    substatus = str(substatus or "").strip()
    if substatus not in allowed:
        return False, f"unsupported_terminal_failure:{substatus}"
    _readiness, db_path = _require_ready(root)
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT runner, status, worker_status, claimed_by, card_json "
            "FROM tasks WHERE task_id=?",
            (task_id,),
        ).fetchone()
        if row is None:
            return False, "task_not_found"
        if str(row["runner"] or "") != runner:
            return False, "runner_mismatch"
        if canonical_status(dict(row)) != "processing":
            return False, f"not_processing:current={canonical_status(dict(row))}"
        if str(row["claimed_by"] or "") != runner:
            return False, "claim_owner_mismatch"
        raw_card_json = str(row["card_json"] or "{}")
        try:
            card = json.loads(raw_card_json)
        except json.JSONDecodeError:
            card = {}
        if not isinstance(card, dict):
            card = {}
        attached_request_id = str(card.get("launch_request_id") or "")
        if request_id:
            if attached_request_id != request_id:
                return False, "launch_request_mismatch"
        elif attached_request_id:
            return False, "launch_request_id_required"

        evidence_payload = dict(evidence or {})
        try:
            claim_epoch = max(0, int(card.get("claim_epoch") or 0))
        except (TypeError, ValueError):
            claim_epoch = 0
        deterministic_verification = task_fsm.deterministic_verification(
            substatus,
            evidence_payload.get("validation"),
            evidence_payload.get("required_outputs"),
            claim_epoch=claim_epoch,
        )
        now = datetime.now(timezone.utc).isoformat()
        terminal = {
            "substatus": substatus,
            "evidence": evidence_payload,
            "deterministic_verification": deterministic_verification,
            "recorded_at": now,
            "runner": runner,
            "claim_epoch": claim_epoch,
        }
        card.update(
            status="blocked",
            worker_status=substatus,
            terminal_substatus=substatus,
            terminal_failure=terminal,
            deterministic_verification=deterministic_verification,
            blocker_reason=str(evidence_payload.get("error") or substatus)[:500],
            blocked_at=now,
            blocked_by=runner,
        )
        cur = conn.execute(
            "UPDATE tasks SET status='blocked', worker_status=?, completed_at=?, "
            "updated_at=?, card_json=? WHERE task_id=? AND status='processing' "
            "AND claimed_by=? AND card_json=?",
            (
                substatus,
                now,
                now,
                json.dumps(card, ensure_ascii=False, sort_keys=True),
                task_id,
                runner,
                raw_card_json,
            ),
        )
        if cur.rowcount != 1:
            conn.rollback()
            return False, "terminal_failure_transition_conflict"
        conn.execute(
            "INSERT INTO task_events(task_id, event, runner, payload_json, created_at) "
            "VALUES (?, 'terminal_failure', ?, ?, ?)",
            (task_id, runner, json.dumps(terminal, ensure_ascii=False, sort_keys=True), now),
        )
        conn.commit()
        return True, "blocked"
    finally:
        conn.close()


# Terminal substatuses that represent hard dependency/policy/scope blockers
# and must never be silently recovered into pending.  These require external
# resolution (dependency completed, scope redefined, policy changed) before
# any rework transaction is legal.
_HARD_BLOCKER_SUBSTATUSES: frozenset[str] = frozenset(
    {
        "dependency_blocked",
        "scope_rejected",
        "blocked",
    }
)

# Terminal substatuses that are eligible for rework recovery when retained
# predecessor evidence and residual feedback are present.
_REWORK_ELIGIBLE_SUBSTATUSES: frozenset[str] = frozenset(
    {
        "validation_failed",
        "worker_failed",
        "launch_failed",
        "timed_out",
        "token_budget_exceeded",
        "output_budget_exceeded",
        "cancelled",
        "liveness_lost",
        "finalize_failed",
        "process_lost",
        "stale",
        "required_output_unchanged",
        "exited",
        "review_ready",
        "promotion_conflict",
    }
)


def retry_finalize_failed(
    root: str | Path,
    task_id: str,
    *,
    runner: str,
    request_id: str,
    actor: str = "codex",
) -> tuple[bool, str]:
    """Re-enter processing for the same retained finalization attempt only.

    This is deliberately narrower than rework recovery: it never increments
    the claim epoch, creates a new provider attempt, or changes task identity.
    The exact blocked request is restored solely so the trusted coordinator
    can re-run deterministic finalization against its retained workspace.
    Besides ``finalize_failed``, only the explicitly operational
    ``validation_exec_scratch_unavailable`` validation failure is eligible;
    ordinary product/test failures remain terminal and require rework.
    """
    _readiness, db_path = _require_ready(root)
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT runner, status, worker_status, claimed_by, card_json "
            "FROM tasks WHERE task_id=?",
            (task_id,),
        ).fetchone()
        if row is None:
            return False, "task_not_found"
        if str(row["runner"] or "") != runner:
            return False, "runner_mismatch"
        current_status = canonical_status(dict(row))
        if current_status not in {"blocked", "review"}:
            return False, f"not_finalize_failed_terminal:current={current_status}"
        raw_card_json = str(row["card_json"] or "{}")
        try:
            card = json.loads(raw_card_json)
        except json.JSONDecodeError:
            return False, "card_json_invalid"
        if not isinstance(card, dict):
            return False, "card_json_not_dict"
        if str(card.get("launch_request_id") or "") != request_id:
            return False, "launch_request_mismatch"
        terminal_record = (
            card.get("terminal_failure") or card.get("terminal_review")
            if current_status == "blocked"
            else card.get("terminal_review")
        )
        if not isinstance(terminal_record, dict):
            return False, "finalize_failed_terminal_record_missing"
        terminal_substatus = str(terminal_record.get("substatus") or "")
        evidence = terminal_record.get("evidence")
        if not isinstance(evidence, dict):
            return False, "terminal_failure_evidence_missing"
        evidence_error = str(evidence.get("error") or "")
        retryable_validation_failure = (
            terminal_substatus == "validation_failed"
            and (
                evidence_error.startswith("validation_exec_scratch_unavailable:")
                or evidence_error.startswith(
                    "validation_failed:validation_exec_scratch_unavailable:"
                )
            )
        )
        if terminal_substatus != "finalize_failed" and not retryable_validation_failure:
            return False, "terminal_substatus_not_retryable_finalization_failure"
        evidence_request_id = str(
            evidence.get("request_id")
            or (evidence.get("request_identity") or {}).get("request_id")
            or ""
        )
        if evidence_request_id and evidence_request_id != request_id:
            return False, "terminal_failure_request_mismatch"

        now = datetime.now(timezone.utc).isoformat()
        card["status"] = "processing"
        card["worker_status"] = "claimed"
        card["claimed_by"] = runner
        card["finalization_retry"] = {
            "request_id": request_id,
            "actor": actor,
            "authorized_at": now,
            "provider_relaunch": False,
            "source_substatus": terminal_substatus,
        }
        card.pop("blocker_reason", None)
        source_worker_status = str(row["worker_status"] or "")
        cur = conn.execute(
            "UPDATE tasks SET status='processing', worker_status='claimed', "
            "claimed_by=?, completed_at=NULL, updated_at=?, card_json=? "
            "WHERE task_id=? AND status=? AND worker_status=? AND card_json=?",
            (
                runner,
                now,
                json.dumps(card, ensure_ascii=False, sort_keys=True),
                task_id,
                str(row["status"] or ""),
                source_worker_status,
                raw_card_json,
            ),
        )
        if cur.rowcount != 1:
            conn.rollback()
            return False, "finalization_retry_transition_conflict"
        conn.execute(
            "INSERT INTO task_events(task_id, event, runner, payload_json, created_at) "
            "VALUES (?, 'finalization_retry_started', ?, ?, ?)",
            (
                task_id,
                actor,
                json.dumps(
                    {
                        "request_id": request_id,
                        "provider_relaunch": False,
                    },
                    sort_keys=True,
                ),
                now,
            ),
        )
        conn.commit()
        return True, "processing"
    finally:
        conn.close()


def recover_blocked_rework(
    root: str | Path,
    task_id: str,
    *,
    actor: str = "coordinator",
    feedback_reason: str = "",
    validation_only_replay: bool = False,
    clean_root_if_predecessor_missing: bool = False,
) -> tuple[bool, str]:
    """Recover the same blocked task ID for rework when retained predecessor
    evidence and actionable residual feedback are present.

    ``validation_only_replay`` explicitly authorizes a validation-only
    replay recovery.  When True this fails closed unless the retained
    ``rework_predecessor`` carries a non-empty ``request_id`` and
    ``changed_path_hashes``; on success the exact provenance and the newly
    computed claim epoch are persisted on the recovered card as
    ``validation_only_replay_authorization``, scoped to this one episode.
    Default False preserves ordinary recovery behavior unchanged.

    Fails closed on:
      - dependency, policy, or scope blockers (must be resolved first)
      - blocked tasks without retained terminal-review predecessor evidence
      - blocked tasks without residual feedback
      - tasks with a live claim or in-process episode
      - non-blocked tasks
      - explicit validation_only_replay authorization lacking retained
        predecessor request_id or changed_path_hashes

    ``clean_root_if_predecessor_missing`` is an explicit coordinator escape
    hatch for an ordinary rework episode whose hash-pinned predecessor
    workspace has already been removed by retention.  It preserves the
    predecessor identity and hashes as audit evidence, removes only the stale
    materialization authority, and records a clean-root authorization.  It is
    incompatible with validation-only replay, which requires the original
    bytes.

    Idempotent: returns ``(True, "already_recovered")`` when a prior recovery
    already re-queued the same task, without duplicating audit history.  A
    pending recovered task may still receive the explicit clean-root
    authorization once when its predecessor is demonstrably missing.
    """
    _readiness, db_path = _require_ready(root)
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT task_id, runner, status, worker_status, claimed_by, "
            "claimed_at, card_json FROM tasks WHERE task_id=?",
            (task_id,),
        ).fetchone()
        if row is None:
            return False, "task_not_found"

        current_canonical = canonical_status(dict(row))

        try:
            card = json.loads(str(row["card_json"] or "{}"))
        except json.JSONDecodeError:
            return False, "card_json_invalid"
        if not isinstance(card, dict):
            return False, "card_json_not_dict"

        if clean_root_if_predecessor_missing and validation_only_replay:
            return False, "clean_root_incompatible_with_validation_only_replay"

        def missing_predecessor_authority() -> tuple[bool, str, dict[str, Any]]:
            predecessor = card.get("rework_predecessor")
            if not isinstance(predecessor, dict):
                return False, "clean_root_rework_predecessor_invalid", {}
            request_id = str(predecessor.get("request_id") or "").strip()
            hashes = predecessor.get("changed_path_hashes")
            workspace = predecessor.get("workspace")
            if (
                len(request_id) != 32
                or any(ch not in "0123456789abcdef" for ch in request_id)
                or not isinstance(hashes, dict)
                or not hashes
            ):
                return False, "clean_root_rework_predecessor_invalid", {}
            for raw_path, expected_hash in hashes.items():
                if not isinstance(raw_path, str):
                    return False, "clean_root_rework_predecessor_invalid", {}
                path_parts = raw_path.split("/")
                if (
                    not raw_path
                    or raw_path.startswith("/")
                    or "\\" in raw_path
                    or any(part in {"", ".", ".."} for part in path_parts)
                ):
                    return False, "clean_root_rework_predecessor_invalid", {}
                if expected_hash is not None and (
                    not isinstance(expected_hash, str)
                    or len(expected_hash) != 64
                    or any(
                        ch not in "0123456789abcdef" for ch in expected_hash
                    )
                ):
                    return False, "clean_root_rework_predecessor_invalid", {}
            if not isinstance(workspace, dict):
                return False, "clean_root_rework_workspace_invalid", {}
            if str(workspace.get("request_id") or "").strip() != request_id:
                return False, "clean_root_rework_identity_mismatch", {}
            try:
                authority_repo = Path(str(workspace.get("repo") or "")).resolve()
                expected_repo = Path(root).resolve()
                source_workspace = Path(str(workspace.get("path") or ""))
            except (OSError, RuntimeError, ValueError):
                return False, "clean_root_rework_workspace_invalid", {}
            if authority_repo != expected_repo or not source_workspace.is_absolute():
                return False, "clean_root_rework_identity_mismatch", {}
            if source_workspace.is_symlink():
                return False, "clean_root_rework_workspace_invalid", {}
            if source_workspace.exists():
                return False, "clean_root_rework_workspace_still_available", {}
            return True, "missing", {
                "predecessor_request_id": request_id,
                "changed_path_hashes": dict(hashes),
                "missing_workspace": str(source_workspace),
            }

        already = conn.execute(
            "SELECT 1 FROM task_events "
            "WHERE task_id=? AND event='blocked_rework_recovery' LIMIT 1",
            (task_id,),
        ).fetchone()
        if already is not None:
            if current_canonical == "pending":
                if not clean_root_if_predecessor_missing:
                    return True, "already_recovered"
                allowed, reason, clean_root_evidence = missing_predecessor_authority()
                if not allowed:
                    return False, reason
                now = datetime.now(timezone.utc).isoformat()
                card.pop("rework_predecessor", None)
                card["clean_root_recovery_authorization"] = {
                    **clean_root_evidence,
                    "task_id": task_id,
                    "actor": actor,
                    "authorized_at": now,
                    "claim_epoch": card.get("claim_epoch"),
                    "one_episode_binding": True,
                }
                card["recovery_mode"] = "clean_root_missing_predecessor"
                conn.execute(
                    "UPDATE tasks SET updated_at=?, card_json=? WHERE task_id=?",
                    (now, json.dumps(card, ensure_ascii=False, sort_keys=True), task_id),
                )
                conn.execute(
                    "INSERT INTO task_events(task_id, event, runner, payload_json, created_at) "
                    "VALUES (?, 'blocked_rework_clean_root_authorized', ?, ?, ?)",
                    (
                        task_id,
                        actor[:120],
                        json.dumps(
                            {
                                **clean_root_evidence,
                                "claim_epoch": card.get("claim_epoch"),
                                "actor": actor[:120],
                                "recorded_at": now,
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        now,
                    ),
                )
                conn.commit()
                return True, "recovered_clean_root"

        if current_canonical != "blocked":
            return False, f"not_blocked:current={current_canonical}"

        worker_status = str(row["worker_status"] or "").strip()
        claimed_by = str(row["claimed_by"] or "").strip()
        if worker_status in {"claimed", "in_progress"} or (
            claimed_by and worker_status not in {
                "launch_failed", "timed_out", "token_budget_exceeded",
                "output_budget_exceeded", "worker_failed", "finalize_failed",
                "cancelled", "liveness_lost", "process_lost", "stale",
                "validation_failed", "required_output_unchanged",
                "exited", "review_ready", "review", "promotion_conflict",
                "scope_rejected", "dependency_blocked", "blocked",
            }
        ):
            return False, "live_claim_detected"

        terminal_row = conn.execute(
            "SELECT runner, payload_json, created_at FROM task_events "
            "WHERE task_id=? AND event='terminal_review' "
            "ORDER BY rowid DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        if terminal_row is None:
            return False, "no_retained_predecessor_evidence"

        try:
            terminal_review = json.loads(str(terminal_row["payload_json"] or "{}"))
        except json.JSONDecodeError:
            return False, "retained_predecessor_evidence_invalid"
        if not isinstance(terminal_review, dict) or not terminal_review:
            return False, "retained_predecessor_evidence_invalid"

        retained_predecessor = card.get("rework_predecessor")
        if not isinstance(retained_predecessor, dict):
            retained_predecessor = {}

        predecessor_request_id = str(
            retained_predecessor.get("request_id") or ""
        ).strip()
        predecessor_changed_path_hashes = retained_predecessor.get(
            "changed_path_hashes"
        )
        if validation_only_replay:
            if not predecessor_request_id or not predecessor_changed_path_hashes:
                return False, "validation_only_replay_missing_evidence"

        terminal_substatus = str(
            terminal_review.get("substatus") or ""
        ).strip()

        if terminal_substatus in _HARD_BLOCKER_SUBSTATUSES:
            return False, f"hard_blocker:{terminal_substatus}"

        if terminal_substatus and terminal_substatus not in _REWORK_ELIGIBLE_SUBSTATUSES:
            return False, f"unrecognized_terminal_substatus:{terminal_substatus}"

        bounded_feedback = str(feedback_reason or "").strip()
        if not bounded_feedback:
            residual = card.get("reject_review")
            if isinstance(residual, dict):
                bounded_feedback = str(residual.get("reason") or "").strip()
        if not bounded_feedback:
            return False, "no_residual_feedback"

        now = datetime.now(timezone.utc).isoformat()

        try:
            claim_epoch = max(0, int(card.get("claim_epoch") or 0)) + 1
        except (TypeError, ValueError):
            claim_epoch = 1

        predecessor_evidence = {
            "terminal_substatus": terminal_substatus,
            "terminal_runner": str(
                terminal_review.get("runner") or terminal_row["runner"] or ""
            ),
            "terminal_recorded_at": str(
                terminal_review.get("recorded_at") or terminal_row["created_at"] or ""
            ),
            "terminal_claim_epoch": (
                terminal_review.get("claim_epoch")
                if terminal_review.get("claim_epoch") is not None
                else card.get("claim_epoch")
            ),
            "changed_path_hashes": (
                retained_predecessor.get("changed_path_hashes")
                or (
                    terminal_review.get("evidence", {}).get("changed_path_hashes")
                    if isinstance(terminal_review.get("evidence"), dict)
                    else None
                )
            ),
            "request_id": str(retained_predecessor.get("request_id") or ""),
        }

        prior_episode_summary = begin_claim_episode(card)

        card["claim_epoch"] = claim_epoch
        card["recovery_epoch"] = claim_epoch
        card["recovered_from_blocked_at"] = now
        card["recovered_by"] = actor
        card["recovery_predecessor"] = predecessor_evidence
        card["recovery_feedback"] = bounded_feedback[:2000]

        if validation_only_replay:
            card["validation_only_replay_authorization"] = {
                "task_id": task_id,
                "actor": actor,
                "predecessor_request_id": predecessor_request_id,
                "changed_path_hashes": predecessor_changed_path_hashes,
                "authorized_at": now,
                "next_claim_epoch": claim_epoch,
                "one_episode_binding": True,
            }
        else:
            card.pop("validation_only_replay_authorization", None)

        if clean_root_if_predecessor_missing:
            allowed, reason, clean_root_evidence = missing_predecessor_authority()
            if not allowed:
                return False, reason
            card.pop("rework_predecessor", None)
            card["clean_root_recovery_authorization"] = {
                **clean_root_evidence,
                "task_id": task_id,
                "actor": actor,
                "authorized_at": now,
                "claim_epoch": claim_epoch,
                "one_episode_binding": True,
            }
            card["recovery_mode"] = "clean_root_missing_predecessor"
        else:
            card.pop("clean_root_recovery_authorization", None)
            card.pop("recovery_mode", None)

        conn.execute(
            "UPDATE tasks SET status='pending', worker_status='unclaimed', "
            "claimed_by=NULL, claimed_at=NULL, started_at=NULL, "
            "completed_at=NULL, updated_at=?, card_json=? "
            "WHERE task_id=?",
            (
                now,
                json.dumps(card, ensure_ascii=False, sort_keys=True),
                task_id,
            ),
        )

        recovery_payload = {
            "transition": "blocked->pending",
            "terminal_substatus": terminal_substatus,
            "predecessor": predecessor_evidence,
            "feedback": bounded_feedback[:2000],
            "prior_episode": prior_episode_summary,
            "claim_epoch": claim_epoch,
            "recorded_at": now,
            "actor": actor[:120],
            "validation_only_replay": bool(validation_only_replay),
        }
        conn.execute(
            "INSERT INTO task_events(task_id, event, runner, payload_json, created_at) "
            "VALUES (?, 'blocked_rework_recovery', ?, ?, ?)",
            (
                task_id,
                actor[:120],
                json.dumps(recovery_payload, ensure_ascii=False, sort_keys=True),
                now,
            ),
        )
        conn.commit()
        return True, "recovered"
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


def list_usage_events(root: str | Path, *, limit: int = 10_000) -> list[dict[str, Any]]:
    """Return bounded, structured canonical usage events for one repository.

    Unlike the legacy pipe-text report this preserves request-time model and
    provider identity so workforce telemetry can attribute tokens without
    guessing from runner names.
    """
    _readiness, db_path = _require_ready(root)
    conn = _connect(db_path, readonly=True)
    try:
        rows = conn.execute(
            "SELECT task_id, runner, payload_json, created_at FROM task_events "
            "WHERE event='usage_record' ORDER BY event_id DESC LIMIT ?",
            (max(1, min(int(limit), 50_000)),),
        ).fetchall()
    finally:
        conn.close()
    result: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(str(row["payload_json"] or "{}"))
        except json.JSONDecodeError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        result.append({
            **payload,
            "task_id": str(row["task_id"] or ""),
            "runner": str(payload.get("runner") or row["runner"] or ""),
            "created_at": str(row["created_at"] or ""),
        })
    return result


def append_usage_capture_event(
    root: str | Path,
    task_id: str,
    runner: str,
    payload: dict[str, Any],
) -> tuple[bool, str]:
    """Append one idempotent internal provider-usage capture receipt.

    This is deliberately narrower than the public manager write surface.  It
    exists for terminal-log reconciliation after a process exited but before
    its provider usage reached the canonical task event ledger.  Exact task,
    runner and ``task_mcp_request:<request_id>`` identity are mandatory.
    """

    _readiness, db_path = _require_ready(root)
    request_note = str(payload.get("note") or "")
    if not request_note.startswith("task_mcp_request:") or len(request_note) != 49:
        return False, "usage_capture_request_identity_invalid"
    if str(payload.get("source") or "") not in {
        "task_mcp_launcher",
        "terminal_log_backfill",
    }:
        return False, "usage_capture_source_invalid"
    conn = _connect(db_path)
    try:
        task = conn.execute(
            "SELECT runner FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        if task is None:
            return False, "task_not_found"
        if str(task["runner"] or "") != runner:
            return False, "runner_mismatch"
        existing = conn.execute(
            "SELECT 1 FROM task_events WHERE task_id=? AND event='usage_record' "
            "AND json_extract(payload_json, '$.note')=? LIMIT 1",
            (task_id, request_note),
        ).fetchone()
        if existing is not None:
            return True, "already_recorded"
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO task_events(task_id,event,runner,payload_json,created_at) "
            "VALUES (?, 'usage_record', ?, ?, ?)",
            (
                task_id,
                runner,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                now,
            ),
        )
        conn.commit()
        return True, "recorded"
    finally:
        conn.close()


def manager_decision_counts(root: str | Path) -> dict[str, Any]:
    """Return exact review decisions plus bounded review-to-decision latency."""

    _readiness, db_path = _require_ready(root)
    conn = _connect(db_path, readonly=True)
    try:
        row = conn.execute(
            "SELECT "
            "SUM(CASE WHEN event='accept_review' THEN 1 ELSE 0 END) accepted, "
            "SUM(CASE WHEN event='reject_review' THEN 1 "
            "  WHEN event IN ('archived','superseded') "
            "   AND json_extract(payload_json, '$.reason') LIKE 'reject_review:%' "
            "  THEN 1 ELSE 0 END) rejected "
            "FROM task_events"
        ).fetchone()
        accepted = int(row["accepted"] or 0)
        rejected = int(row["rejected"] or 0)
        latency_rows = conn.execute(
            "SELECT d.event, d.payload_json, d.created_at decision_at, "
            " (SELECT r.created_at FROM task_events r "
            "  WHERE r.task_id=d.task_id AND r.event='terminal_review' "
            "    AND r.event_id<d.event_id ORDER BY r.event_id DESC LIMIT 1) review_at "
            "FROM task_events d WHERE d.event IN ('accept_review','reject_review','archived','superseded') "
            "ORDER BY d.event_id DESC LIMIT 5000"
        ).fetchall()
        latency: dict[str, list[float]] = {"accepted": [], "rejected": []}
        for item in latency_rows:
            event = str(item["event"] or "")
            if event in {"archived", "superseded"}:
                try:
                    payload = json.loads(str(item["payload_json"] or "{}"))
                except json.JSONDecodeError:
                    continue
                if not str(payload.get("reason") or "").startswith("reject_review:"):
                    continue
                kind = "rejected"
            else:
                kind = "accepted" if event == "accept_review" else "rejected"
            try:
                decision_at = datetime.fromisoformat(str(item["decision_at"]).replace("Z", "+00:00"))
                review_at = datetime.fromisoformat(str(item["review_at"]).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                continue
            seconds = max(0.0, (decision_at - review_at).total_seconds())
            latency[kind].append(seconds)

        def latency_summary(values: list[float]) -> dict[str, Any]:
            ordered = sorted(values)
            if not ordered:
                return {"count": 0, "p50_seconds": None, "p95_seconds": None}
            percentile = lambda fraction: ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction))]
            return {
                "count": len(ordered),
                "p50_seconds": round(percentile(0.50), 3),
                "p95_seconds": round(percentile(0.95), 3),
            }

        return {
            "accepted": accepted,
            "rejected": rejected,
            "total": accepted + rejected,
            "accepted_latency": latency_summary(latency["accepted"]),
            "rejected_latency": latency_summary(latency["rejected"]),
            "latency_sample_limit": 5000,
        }
    finally:
        conn.close()


def latest_manager_decisions(root: str | Path, *, limit: int = 50_000) -> dict[str, dict[str, str]]:
    """Return the latest explicit manager review decision for each task.

    Archived/superseded rows count as rejection only when their durable reason
    is a ``reject_review:`` disposition. Other lifecycle archival is not a
    quality judgment and is intentionally excluded.
    """

    _readiness, db_path = _require_ready(root)
    conn = _connect(db_path, readonly=True)
    try:
        rows = conn.execute(
            "SELECT task_id, event, payload_json, created_at FROM task_events "
            "WHERE event IN ('accept_review','reject_review','archived','superseded') "
            "ORDER BY event_id DESC LIMIT ?",
            (max(1, min(int(limit), 50_000)),),
        ).fetchall()
    finally:
        conn.close()
    decisions: dict[str, dict[str, str]] = {}
    for row in rows:
        task_id = str(row["task_id"] or "")
        if not task_id or task_id in decisions:
            continue
        event = str(row["event"] or "")
        if event == "accept_review":
            decision = "accepted"
        elif event == "reject_review":
            decision = "rejected"
        else:
            try:
                payload = json.loads(str(row["payload_json"] or "{}"))
            except json.JSONDecodeError:
                continue
            if not str(payload.get("reason") or "").startswith("reject_review:"):
                continue
            decision = "rejected"
        decisions[task_id] = {
            "decision": decision,
            "event": event,
            "created_at": str(row["created_at"] or ""),
        }
    return decisions


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

    try:
        provider_guards = apply_repository_guards(repo.root)
    except ProviderGuardError as exc:
        raise InitializationRefusedError(f"provider_guard_install_failed:{exc}") from exc

    # NeedFix is an additive repo-local auxiliary store. Fresh initialization
    # must create it in the same bounded operation so the dashboard's first
    # read never reports a false missing-table error.
    needfix_storage = ensure_needfix_storage(repo.root)

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
        "needfix_storage": needfix_storage,
        "provider_guards": provider_guards,
        "storage": readiness.as_dict(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


__all__ = [
    "CANONICAL_STATUSES",
    "CURRENT_EPISODE_CARD_FIELDS",
    "InitializationRefusedError",
    "REQUIRED_TABLES",
    "SCHEMA",
    "SCHEMA_ID",
    "StorageNotReadyError",
    "StorageReadiness",
    "storage_readiness",
    "TaskStoreError",
    "archive_task",
    "begin_claim_episode",
    "callback_bridge_health",
    "canonical_db_path",
    "canonical_status",
    "database_identity",
    "exact_status_counts",
    "get_task_events",
    "get_task",
    "initialize_repository",
    "list_tasks",
    "manager_decision_counts",
    "mark_launch_failed",
    "mark_terminal_review",
    "quick_check",
    "recover_blocked_rework",
    "restore_task",
    "sha256_file",
    "ensure_needfix_storage",
]


def ensure_needfix_storage(repo_root) -> dict[str, object]:
    """Narrow, additive: idempotently ensure the repo-scoped NeedFix store
    exists alongside canonical task storage. Never mutates task tables and
    preserves any existing task-store authority/state."""
    from . import needfix_store

    return needfix_store.initialize_repository(repo_root)
