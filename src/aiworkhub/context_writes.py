"""Canonical, repository-bound context mutations for verified MCP callers.

Models never receive a database path.  Callers resolve manager/worker identity
first and pass the already-authorized repository into this module.  Every
mutation is bounded, idempotent, provenance-bearing, audited, and soft-delete
only.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from . import context_graph, feature_settings, storage_registry, transcript_store
from .repository_state import RepositoryStateError


SessionAction = Literal["start", "event", "checkpoint", "state", "handoff", "close"]
MemoryAction = Literal["remember", "update", "supersede", "archive"]
KbAction = Literal["upsert", "ingest", "supersede", "archive"]

MAX_KEY_BYTES = 256
MAX_CONTENT_BYTES = 32 * 1024
MAX_TAGS_BYTES = 1024
MAX_PROVENANCE_BYTES = 2048
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,191}$")


class ContextWriteError(RuntimeError):
    pass


def _bounded(value: Any, *, field: str, max_bytes: int, required: bool = True) -> str:
    if not isinstance(value, str):
        raise ContextWriteError(f"invalid_{field}")
    text = value.strip()
    if (required and not text) or "\x00" in text or len(text.encode("utf-8")) > max_bytes:
        raise ContextWriteError(f"invalid_{field}")
    return text


_INTEGRITY_COLUMN_RE = re.compile(r"failed:\s*([A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)?)")


def _integrity_column(exc: sqlite3.IntegrityError) -> str:
    """Parse the exact offending ``table.column`` from a sqlite integrity error.

    sqlite spells NOT NULL / UNIQUE / CHECK failures as
    ``... constraint failed: <table>.<column>``.  Surfacing that qualified name
    turns every future occurrence from a guess across a dozen NOT NULL columns
    into a fact.  When no column is present, fall back to a *bounded* slice of
    the raw message: the structured component/action/column fields stay the
    primary signal, and the raw text is a capped fallback, never an unbounded
    leak of sqlite internals to the caller.
    """
    match = _INTEGRITY_COLUMN_RE.search(str(exc))
    if match:
        return match.group(1)
    raw = " ".join(str(exc).split())
    return raw[:120] if raw else "unknown"


def _integrity_error(
    exc: sqlite3.IntegrityError, *, component: str, action: str
) -> ContextWriteError:
    """Re-shape a raw sqlite IntegrityError into a column-naming ContextWriteError."""
    column = _integrity_column(exc)
    return ContextWriteError(
        f"context_write_integrity_error:component={component}:action={action}:column={column}"
    )


def _identity(actor: dict[str, Any]) -> dict[str, str | None]:
    # task_id is genuinely optional: a manager write made outside any task has
    # no task, and that absence is stored as NULL (see the nullable task_id
    # column in _open) rather than as an empty-string sentinel that cannot be
    # told apart from a task whose id is blank.  _bounded still rejects a
    # malformed task_id; an absent or blank one collapses to None.
    task_id = _bounded(actor.get("task_id", ""), field="task_id", max_bytes=256, required=False)
    result: dict[str, str | None] = {
        "role": _bounded(actor.get("role", ""), field="actor_role", max_bytes=32),
        "actor_id": _bounded(actor.get("actor_id", ""), field="actor_id", max_bytes=256),
        "task_id": task_id or None,
        "provider": _bounded(actor.get("provider", ""), field="provider", max_bytes=64),
        "session_id": _bounded(actor.get("session_id", ""), field="session_id", max_bytes=256),
    }
    if result["role"] not in {"manager", "worker"}:
        raise ContextWriteError("invalid_actor_role")
    return result


def _open(repo: Path, db_id: str) -> sqlite3.Connection:
    registry = storage_registry.load_storage_registry(repo)
    path = storage_registry.resolve_database_path(registry, db_id)
    if not path.is_file() or path.stat().st_size <= 0:
        raise ContextWriteError(f"canonical_database_unavailable:{db_id}")
    con = sqlite3.connect(str(path), timeout=5)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=5000")
    con.executescript(
        "CREATE TABLE IF NOT EXISTS context_mutations("
        "id INTEGER PRIMARY KEY,idempotency_key TEXT UNIQUE NOT NULL,component TEXT NOT NULL,"
        "action TEXT NOT NULL,entity_key TEXT NOT NULL,actor_role TEXT NOT NULL,actor_id TEXT NOT NULL,"
        "task_id TEXT,provider TEXT NOT NULL,session_id TEXT NOT NULL,provenance TEXT NOT NULL,"
        "payload_sha256 TEXT NOT NULL,created_at TEXT NOT NULL);"
        "CREATE TABLE IF NOT EXISTS context_entity_state("
        "entity_type TEXT NOT NULL,entity_id INTEGER NOT NULL,status TEXT NOT NULL,"
        "superseded_by INTEGER,updated_at TEXT NOT NULL,PRIMARY KEY(entity_type,entity_id));"
    )
    _normalize_context_mutations_schema(con)
    if db_id == "memory":
        _normalize_memory_schema(con)
    elif db_id == "kb":
        _normalize_kb_schema(con)
    return con


def _create_memories_fts(con: sqlite3.Connection) -> None:
    """Create and backfill ``memories_fts`` (rowid=id) in the caller's transaction.

    Single source of truth for the ``memories_fts`` DDL and backfill.  The
    caller owns BEGIN/COMMIT, so the atomic schema rebuild and the standalone
    repair share one definition without either nesting a transaction.
    """
    con.execute("CREATE VIRTUAL TABLE memories_fts USING fts5(key,value,tags,scope)")
    con.execute(
        "INSERT INTO memories_fts(rowid,key,value,tags,scope) "
        "SELECT id,key,value,tags,scope FROM memories"
    )


def _memories_fts_triggers(con: sqlite3.Connection) -> list[str]:
    """Names of every trigger, on any table, whose SQL references ``memories_fts``.

    Deliberately not limited to triggers on ``memories``: with
    ``legacy_alter_table`` off, ``ALTER TABLE ... RENAME`` re-parses the whole
    schema, so a trigger on another table that writes to a dropped
    ``memories_fts`` fails the rename just as the legacy ``memories`` triggers
    do.
    """
    return [
        str(name)
        for name, sql in con.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger'"
        ).fetchall()
        if "memories_fts" in str(sql or "").lower()
    ]


def _drop_memories_fts_triggers(con: sqlite3.Connection) -> None:
    """Drop the legacy triggers that keep ``memories_fts`` in sync behind our back.

    The canonical model maintains ``memories_fts`` explicitly in
    ``memory_write``, so these triggers must not survive: with the index gone
    they fail every statement on their table (``no such table:
    main.memories_fts``), and beside a recreated index they would maintain it a
    second time.  The caller owns the transaction.
    """
    for name in _memories_fts_triggers(con):
        con.execute('DROP TRIGGER IF EXISTS "' + name.replace('"', '""') + '"')


def _ensure_memories_fts(con: sqlite3.Connection) -> dict[str, Any]:
    """Atomic repair: lock-free check, then BEGIN IMMEDIATE, re-check, backfill rowid=id.

    Standalone repair built on ``_create_memories_fts`` and used by both the
    write-path schema normalizer and the public search-path repair entry
    point.  An index that already exists is a no-op decided by a read alone, so
    the hot search path never takes the write lock; only an absent index goes
    on to BEGIN IMMEDIATE, where it is checked again against a concurrent
    repairer.  Creating the index also drops any legacy trigger that would
    maintain it a second time (``_drop_memories_fts_triggers``).  Returns a
    bounded outcome dict; callers decide interpretation.
    """
    try:
        if con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memories_fts'"
        ).fetchone() is not None:
            return {"ok": True, "created": False, "reason": "fts_already_exists"}
        con.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        return {"ok": False, "error": "fts_lock_blocked", "detail": str(exc)[:120]}
    try:
        if con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memories_fts'"
        ).fetchone() is not None:
            con.rollback()
            return {"ok": True, "created": False, "reason": "fts_already_exists"}
        if con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memories'"
        ).fetchone() is None:
            con.rollback()
            return {"ok": True, "created": False, "reason": "memories_table_absent"}
        _drop_memories_fts_triggers(con)
        _create_memories_fts(con)
        con.commit()
        return {"ok": True, "created": True, "reason": "fts_created"}
    except sqlite3.Error as exc:
        try:
            con.rollback()
        except sqlite3.OperationalError:
            pass
        return {"ok": False, "error": "fts_migration_failed", "detail": str(exc)[:160], "sqlite_errorname": str(getattr(exc, "sqlite_errorname", "UNKNOWN"))}


def _has_unique_key_index(con: sqlite3.Connection) -> bool:
    """True when ``memories`` carries a unique index on exactly ``key``."""
    for row in con.execute("PRAGMA index_list(memories)").fetchall():
        # seq, name, unique, origin, partial
        if not bool(row[2]):
            continue
        index_name = str(row[1]).replace('"', '""')
        columns = [
            str(info[2])
            for info in con.execute(f'PRAGMA index_info("{index_name}")').fetchall()
        ]
        if columns == ["key"]:
            return True
    return False


def _migrate_memories_schema(con: sqlite3.Connection) -> None:
    """Atomically clear legacy FTS triggers, rebuild ``memories_fts``, un-``UNIQUE`` ``memories``.

    Every legacy trigger that writes to ``memories_fts`` is dropped, on
    whichever table it sits: the canonical model maintains that index
    explicitly in ``memory_write``.  A surviving trigger double-indexes every
    write, and once ``memories_fts`` is gone it fails every statement on its
    table with ``no such table: main.memories_fts`` -- modern SQLite refuses
    the ``ALTER TABLE ... RENAME`` below for the same reason, because it
    re-parses every trigger in the schema.  ``memories_fts`` is then dropped
    and recreated in the canonical shape, backfilled with rowid=id, whether or
    not the table itself needs rebuilding.  A legacy store keeps an
    external-content index (``content='memories'``) that only the dropped
    triggers maintained, and an existence check by name cannot tell it from
    the canonical one.  Left in place it would keep answering ``MATCH`` from
    superseded text: ``memory_write`` deletes by rowid after the content row
    has already changed, so an external-content index can no longer recover the
    old terms.  When an exact ``key UNIQUE`` index is observed, the table is
    rebuilt without it in between, preserving ids.

    The whole sequence is one ``BEGIN IMMEDIATE`` transaction, like
    ``_normalize_context_mutations_schema``.  The former ``executescript``
    form committed ``DROP TABLE memories_fts`` by itself and only then failed
    the rename, stranding a store with no FTS index, the stale trigger and the
    unique index all still in place.  Here the index drop is rolled back with
    everything else: the copied row count is verified against the source and
    the rebuilt index against ``memories``, and any failure -- a count mismatch
    or any exception -- restores the original tables, triggers and index
    untouched and raises ``ContextWriteError`` naming the step that failed.
    What needs migrating is re-checked under the write lock, so a concurrent
    opener that already migrated the store is not migrated twice.
    """
    step = "begin_immediate"
    try:
        con.execute("BEGIN IMMEDIATE")
        step = "recheck_under_lock"
        rebuild_table = _has_unique_key_index(con)
        if not rebuild_table and not _memories_fts_triggers(con):
            con.rollback()
            return
        step = "drop_fts_triggers"
        _drop_memories_fts_triggers(con)
        step = "drop_memories_fts"
        con.execute("DROP TABLE IF EXISTS memories_fts")
        if rebuild_table:
            step = "count_source_rows"
            source_count = con.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            step = "rename_memories_to_legacy"
            con.execute("ALTER TABLE memories RENAME TO memories_legacy_unique")
            step = "create_memories"
            con.execute(
                "CREATE TABLE memories(id INTEGER PRIMARY KEY,key TEXT,value TEXT,tags TEXT,scope TEXT)"
            )
            step = "copy_rows"
            con.execute(
                "INSERT INTO memories(id,key,value,tags,scope) "
                "SELECT id,key,value,tags,scope FROM memories_legacy_unique"
            )
            step = "verify_row_count"
            copied_count = con.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            if copied_count != source_count:
                raise ContextWriteError(
                    "memories_migration_row_count_mismatch:"
                    f"source={source_count}:copied={copied_count}"
                )
            step = "drop_legacy_table"
            con.execute("DROP TABLE memories_legacy_unique")
        step = "create_memories_fts"
        _create_memories_fts(con)
        step = "verify_fts_row_count"
        memories_count = con.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        indexed_count = con.execute("SELECT COUNT(*) FROM memories_fts").fetchone()[0]
        if indexed_count != memories_count:
            raise ContextWriteError(
                "memories_migration_fts_row_count_mismatch:"
                f"memories={memories_count}:indexed={indexed_count}"
            )
        step = "commit"
        con.commit()
    except Exception as exc:
        try:
            con.rollback()
        except sqlite3.Error:
            pass
        if isinstance(exc, ContextWriteError):
            raise
        raise ContextWriteError(
            f"memories_migration_failed:step={step}:{type(exc).__name__}:{str(exc)[:120]}"
        ) from exc


def _normalize_memory_schema(con: sqlite3.Connection) -> None:
    """Repair known legacy ``memories`` schema shapes without losing rows.

    Supersede/archive semantics retain historical rows with the same logical
    key.  Early databases declared ``key UNIQUE``; after an archived row was
    imported, ``remember`` therefore raised an opaque IntegrityError.  Rebuild
    only when an exact unique-key index is observed, preserve ids, and rebuild
    the FTS mirror deterministically.  Independently of the unique index, any
    legacy trigger that writes to ``memories_fts`` sends the store through
    ``_migrate_memories_schema``, which drops it and always recreates the index
    in the canonical shape.  A database stranded with no ``memories_fts`` and a
    stale trigger therefore repairs itself here whether or not the unique index
    survives, and a legacy external-content index never outlives the triggers
    that maintained it.  The closing ``_ensure_memories_fts`` call is the
    shared primitive that backfills an index that is still absent.  Both paths
    are idempotent.
    """
    if con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memories'"
    ).fetchone() is None:
        return
    if _has_unique_key_index(con) or _memories_fts_triggers(con):
        _migrate_memories_schema(con)
    result = _ensure_memories_fts(con)
    if not result.get("ok"):
        raise ContextWriteError(f"fts_normalization_failed:{result.get('error', 'unknown')}")


def _kb_fts_needs_rebuild(con: sqlite3.Connection) -> list[str] | None:
    """Triggers writing to ``entries_fts`` when the index needs rebuilding, else None."""
    triggers = [
        str(name)
        for name, sql in con.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'").fetchall()
        if "entries_fts" in str(sql or "").lower()
    ]
    row = con.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='entries_fts'").fetchone()
    columns = [str(col[1]) for col in con.execute("PRAGMA table_info(entries_fts)").fetchall()]
    canonical = (
        row is not None
        and columns == ["key", "title", "body", "category", "tags"]
        and "content=" not in "".join(str(row[0] or "").lower().split())
    )
    return None if canonical and not triggers else triggers


def _normalize_kb_schema(con: sqlite3.Connection) -> None:
    """Rebuild a legacy AITools ``entries_fts`` into the canonical shape (NF-2026-01356).

    A KB migrated from AITools keeps ``entries_fts`` as an external-content
    index over ``(key,title,body,tags)`` -- no ``category`` -- maintained by
    triggers on ``entries``.  ``kb_write`` maintains the canonical
    ``(key,title,body,category,tags)`` index explicitly, so every upsert failed
    with ``table entries_fts has no column named category`` (reported to the
    manager as ``context_write_failed:OperationalError``).  Like
    ``_migrate_memories_schema``: drop every trigger that writes to
    ``entries_fts``, recreate the index canonically and backfill rowid=id, in
    one ``BEGIN IMMEDIATE`` transaction re-checked under the lock; any failure
    rolls back untouched.  A canonical index is a read-only no-op.
    """
    if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='entries'").fetchone() is None:
        return
    if _kb_fts_needs_rebuild(con) is None:
        return
    step = "begin_immediate"
    try:
        con.execute("BEGIN IMMEDIATE")
        step = "recheck_under_lock"
        triggers = _kb_fts_needs_rebuild(con)
        if triggers is None:
            con.rollback()
            return
        step = "drop_fts_triggers"
        for name in triggers:
            con.execute('DROP TRIGGER IF EXISTS "' + name.replace('"', '""') + '"')
        step = "drop_entries_fts"
        con.execute("DROP TABLE IF EXISTS entries_fts")
        step = "create_entries_fts"
        con.execute("CREATE VIRTUAL TABLE entries_fts USING fts5(key,title,body,category,tags)")
        con.execute(
            "INSERT INTO entries_fts(rowid,key,title,body,category,tags) "
            "SELECT id,key,title,body,category,tags FROM entries"
        )
        step = "commit"
        con.commit()
    except Exception as exc:
        try:
            con.rollback()
        except sqlite3.Error:
            pass
        raise ContextWriteError(
            f"kb_fts_migration_failed:step={step}:{type(exc).__name__}:{str(exc)[:120]}"
        ) from exc


def _normalize_context_mutations_schema(con: sqlite3.Connection) -> None:
    """Make ``context_mutations.task_id`` nullable on legacy stores.

    Early databases declared ``task_id TEXT NOT NULL`` and recorded every
    task-less manager write as the empty string -- data shaped like a lie,
    indistinguishable from a task whose id is genuinely blank.  When an actual
    NOT NULL constraint on task_id is observed, rebuild the table with a
    nullable task_id, preserve ids, and normalise the historical empty-string
    sentinel to NULL so absence becomes representable and honest.  Every other
    column keeps its NOT NULL guarantee.  Idempotent: once task_id is already
    nullable this is a cheap no-op.

    The rebuild is atomic.  ``context_mutations`` *is* the audit trail; a
    partial rebuild that stranded or dropped rows while reporting success would
    destroy exactly the evidence the table exists to hold, undetectably -- the
    original NF-2026-00268 failure mode, where an empty audit table could not be
    told apart from one that was never written.  So the whole
    rename/create/copy/drop runs inside one ``BEGIN IMMEDIATE`` transaction, the
    copied row count is verified against the source count, and any failure --
    a count mismatch or any exception -- rolls back to leave the original table
    untouched and complete, then re-raises.
    """
    info = con.execute("PRAGMA table_info(context_mutations)").fetchall()
    if not info:
        return
    # PRAGMA table_info columns: cid, name, type, notnull, dflt_value, pk.
    task_id_notnull = any(str(col[1]) == "task_id" and bool(col[3]) for col in info)
    if not task_id_notnull:
        return
    con.execute("BEGIN IMMEDIATE")
    try:
        source_count = con.execute(
            "SELECT COUNT(*) FROM context_mutations"
        ).fetchone()[0]
        con.execute(
            "ALTER TABLE context_mutations RENAME TO context_mutations_legacy_notnull"
        )
        con.execute(
            "CREATE TABLE context_mutations("
            "id INTEGER PRIMARY KEY,idempotency_key TEXT UNIQUE NOT NULL,component TEXT NOT NULL,"
            "action TEXT NOT NULL,entity_key TEXT NOT NULL,actor_role TEXT NOT NULL,actor_id TEXT NOT NULL,"
            "task_id TEXT,provider TEXT NOT NULL,session_id TEXT NOT NULL,provenance TEXT NOT NULL,"
            "payload_sha256 TEXT NOT NULL,created_at TEXT NOT NULL)"
        )
        con.execute(
            "INSERT INTO context_mutations(id,idempotency_key,component,action,entity_key,actor_role,"
            "actor_id,task_id,provider,session_id,provenance,payload_sha256,created_at) "
            "SELECT id,idempotency_key,component,action,entity_key,actor_role,actor_id,"
            "NULLIF(task_id,''),provider,session_id,provenance,payload_sha256,created_at "
            "FROM context_mutations_legacy_notnull"
        )
        copied_count = con.execute(
            "SELECT COUNT(*) FROM context_mutations"
        ).fetchone()[0]
        if copied_count != source_count:
            raise ContextWriteError(
                "context_mutations_migration_row_count_mismatch:"
                f"source={source_count}:copied={copied_count}"
            )
        con.execute("DROP TABLE context_mutations_legacy_notnull")
        con.commit()
    except Exception:
        con.rollback()
        raise


def ensure_memories_fts(repo: Path) -> dict[str, Any]:
    """Public entry point for search-path FTS repair on the canonical memory DB.

    Opens a fresh writable connection, checks whether ``memories_fts`` already
    exists, creates and backfills it if absent, and returns a bounded outcome
    dict.  With the index present this is a no-op that never takes the write
    lock (see ``_ensure_memories_fts``).  The manager search path invokes this
    once before its MATCH query; exact get/related never call it.
    """
    try:
        registry = storage_registry.load_storage_registry(repo)
        path = storage_registry.resolve_database_path(registry, "memory")
    except (storage_registry.StorageRegistryError, RepositoryStateError, OSError) as exc:
        return {"ok": False, "error": "fts_registry_unavailable", "detail": str(exc)[:160]}
    if not path.is_file() or path.stat().st_size <= 0:
        return {"ok": False, "error": "fts_db_absent_or_empty"}
    con: sqlite3.Connection | None = None
    try:
        con = sqlite3.connect(str(path), timeout=5)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA busy_timeout=5000")
        return _ensure_memories_fts(con)
    except sqlite3.Error as exc:
        return {"ok": False, "error": "fts_migration_failed", "detail": str(exc)[:160], "sqlite_errorname": str(getattr(exc, "sqlite_errorname", "UNKNOWN"))}
    except OSError as exc:
        return {"ok": False, "error": "fts_io_failed", "detail": str(exc)[:160]}
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass


def _begin(con: sqlite3.Connection, *, idempotency_key: str) -> sqlite3.Row | None:
    if not _IDEMPOTENCY_RE.fullmatch(idempotency_key):
        raise ContextWriteError("invalid_idempotency_key")
    con.execute("BEGIN IMMEDIATE")
    return con.execute(
        "SELECT id,component,action,entity_key,created_at FROM context_mutations WHERE idempotency_key=?",
        (idempotency_key,),
    ).fetchone()


def _record(
    con: sqlite3.Connection, *, idempotency_key: str, component: str, action: str,
    entity_key: str, actor: dict[str, str | None], provenance: str, payload: dict[str, Any],
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    con.execute(
        "INSERT INTO context_mutations(idempotency_key,component,action,entity_key,actor_role,actor_id,"
        "task_id,provider,session_id,provenance,payload_sha256,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (idempotency_key, component, action, entity_key, actor["role"], actor["actor_id"],
         actor["task_id"], actor["provider"], actor["session_id"], provenance, digest, now),
    )


def _idempotent(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "ok": True, "idempotent": True, "mutation_id": int(row["id"]),
        "component": row["component"], "action": row["action"],
        "entity_key": row["entity_key"], "created_at": row["created_at"],
    }


def session_write(
    repo: Path, *, actor: dict[str, Any], action: SessionAction, topic: str,
    content: str, idempotency_key: str, provenance: str,
) -> dict[str, Any]:
    identity = _identity(actor)
    if action not in {"start", "event", "checkpoint", "state", "handoff", "close"}:
        raise ContextWriteError("invalid_session_action")
    topic = _bounded(topic, field="topic", max_bytes=256)
    content = _bounded(content, field="content", max_bytes=MAX_CONTENT_BYTES)
    provenance = _bounded(provenance, field="provenance", max_bytes=MAX_PROVENANCE_BYTES)
    manager_graph_capture = (
        identity["role"] == "manager"
        and feature_settings.enabled(repo, "context_graph")
    )
    if manager_graph_capture:
        context_graph.ensure_schema(repo)
    con = _open(repo, "transcript")
    try:
        prior = _begin(con, idempotency_key=idempotency_key)
        if prior is not None:
            con.rollback()
            return _idempotent(prior)
        timestamp = datetime.now(timezone.utc).isoformat()
        source_id = f"{identity['role']}:{identity['provider']}:{identity['session_id']}:{topic}"
        doc_id = transcript_store.insert_document(
            con,
            source_id=source_id,
            timestamp=timestamp,
            kind=action,
            content=content,
            source="aiworkhub",
            speaker=identity["role"],
            tags=topic,
        )
        if manager_graph_capture:
            registry = storage_registry.load_storage_registry(repo)
            context_graph.append_session_document_in_transaction(
                con,
                repo_id=registry.repo.manifest.repo_id,
                thread_id=identity["session_id"],
                session_id=identity["session_id"],
                provider=identity["provider"],
                role=identity["role"],
                action=action,
                topic=topic,
                content=content,
                document_id=doc_id,
                # The context_graph projection keeps its own empty-string
                # convention for a task-less write; only the audit trail
                # (context_mutations, via _record) stores NULL.
                task_id=identity["task_id"] or "",
                occurred_at=timestamp,
            )
        _record(
            con, idempotency_key=idempotency_key, component="session", action=action,
            entity_key=f"document:{doc_id}", actor=identity, provenance=provenance,
            payload={"topic": topic, "content": content},
        )
        con.commit()
        return {"ok": True, "idempotent": False, "action": action, "document_id": doc_id, "timestamp": timestamp}
    except sqlite3.IntegrityError as exc:
        con.rollback()
        raise _integrity_error(exc, component="session", action=action) from exc
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def _legacy_timestamp_columns(con: sqlite3.Connection, table: str) -> frozenset[str]:
    """Which of created_at/updated_at ``table`` declares as real columns."""
    names = {str(row[1]) for row in con.execute(f"PRAGMA table_info({table})").fetchall()}
    return frozenset(c for c in ("created_at", "updated_at") if c in names)


def _timestamp_insert_extra(columns: frozenset[str], now: str) -> tuple[str, str, tuple[str, ...]]:
    present = [c for c in ("created_at", "updated_at") if c in columns]
    return (
        "".join(f",{c}" for c in present),
        "".join(",?" for _ in present),
        tuple(now for _ in present),
    )


def _timestamp_update_extra(columns: frozenset[str], now: str) -> tuple[str, tuple[str, ...]]:
    if "updated_at" in columns:
        return ",updated_at=?", (now,)
    return "", ()


def memory_write(
    repo: Path, *, actor: dict[str, Any], action: MemoryAction, key: str,
    value: str = "", tags: str = "", scope: str = "project",
    idempotency_key: str, provenance: str,
) -> dict[str, Any]:
    identity = _identity(actor)
    if action not in {"remember", "update", "supersede", "archive"}:
        raise ContextWriteError("invalid_memory_action")
    key = _bounded(key, field="key", max_bytes=MAX_KEY_BYTES)
    value = _bounded(value, field="value", max_bytes=MAX_CONTENT_BYTES, required=action != "archive")
    tags = _bounded(tags, field="tags", max_bytes=MAX_TAGS_BYTES, required=False)
    scope = _bounded(scope, field="scope", max_bytes=64)
    provenance = _bounded(provenance, field="provenance", max_bytes=MAX_PROVENANCE_BYTES)
    con = _open(repo, "memory")
    try:
        prior = _begin(con, idempotency_key=idempotency_key)
        if prior is not None:
            con.rollback()
            return _idempotent(prior)
        row = con.execute(
            "SELECT m.id,m.value,m.tags,m.scope,COALESCE(s.status,'active') status FROM memories m "
            "LEFT JOIN context_entity_state s ON s.entity_type='memory' AND s.entity_id=m.id "
            "WHERE m.key=? ORDER BY m.id DESC LIMIT 1", (key,),
        ).fetchone()
        now = datetime.now(timezone.utc).isoformat()
        ts_columns = _legacy_timestamp_columns(con, "memories")
        ins_col_extra, ins_ph_extra, ins_val_extra = _timestamp_insert_extra(ts_columns, now)
        upd_set_extra, upd_val_extra = _timestamp_update_extra(ts_columns, now)
        if action == "remember":
            if row is not None and row["status"] == "active":
                raise ContextWriteError("memory_key_exists_use_update")
            cur = con.execute(
                f"INSERT INTO memories(key,value,tags,scope{ins_col_extra}) VALUES(?,?,?,?{ins_ph_extra})",
                (key, value, tags, scope) + ins_val_extra,
            )
            entity_id = int(cur.lastrowid)
            con.execute("INSERT INTO memories_fts(rowid,key,value,tags,scope) VALUES(?,?,?,?,?)", (entity_id, key, value, tags, scope))
        elif action == "archive":
            if row is None or row["status"] != "active":
                raise ContextWriteError("memory_active_key_not_found")
            entity_id = int(row["id"])
            con.execute("INSERT OR REPLACE INTO context_entity_state VALUES('memory',?,'archived',NULL,?)", (entity_id, now))
        elif action == "update":
            if row is None or row["status"] != "active":
                raise ContextWriteError("memory_active_key_not_found")
            entity_id = int(row["id"])
            con.execute(
                f"UPDATE memories SET value=?,tags=?,scope=?{upd_set_extra} WHERE id=?",
                (value, tags, scope) + upd_val_extra + (entity_id,),
            )
            con.execute("DELETE FROM memories_fts WHERE rowid=?", (entity_id,))
            con.execute("INSERT INTO memories_fts(rowid,key,value,tags,scope) VALUES(?,?,?,?,?)", (entity_id, key, value, tags, scope))
        else:
            if row is None or row["status"] != "active":
                raise ContextWriteError("memory_active_key_not_found")
            old_id = int(row["id"])
            cur = con.execute(
                f"INSERT INTO memories(key,value,tags,scope{ins_col_extra}) VALUES(?,?,?,?{ins_ph_extra})",
                (key, value, tags, scope) + ins_val_extra,
            )
            entity_id = int(cur.lastrowid)
            con.execute("INSERT INTO memories_fts(rowid,key,value,tags,scope) VALUES(?,?,?,?,?)", (entity_id, key, value, tags, scope))
            con.execute("INSERT OR REPLACE INTO context_entity_state VALUES('memory',?,'superseded',?,?)", (old_id, entity_id, now))
        con.execute("INSERT OR REPLACE INTO context_entity_state VALUES('memory',?,'active',NULL,?)", (entity_id, now)) if action != "archive" else None
        _record(con, idempotency_key=idempotency_key, component="memory", action=action,
                entity_key=f"memory:{entity_id}", actor=identity, provenance=provenance,
                payload={"key": key, "value": value, "tags": tags, "scope": scope})
        con.commit()
        return {"ok": True, "idempotent": False, "action": action, "key": key, "memory_id": entity_id, "status": "archived" if action == "archive" else "active"}
    except sqlite3.IntegrityError as exc:
        con.rollback()
        raise _integrity_error(exc, component="memory", action=action) from exc
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def kb_write(
    repo: Path, *, actor: dict[str, Any], action: KbAction, key: str,
    title: str = "", body: str = "", category: str = "", tags: str = "",
    source_refs: str = "", replacement_key: str = "", idempotency_key: str,
    provenance: str,
) -> dict[str, Any]:
    identity = _identity(actor)
    if action not in {"upsert", "ingest", "supersede", "archive"}:
        raise ContextWriteError("invalid_kb_action")
    key = _bounded(key, field="key", max_bytes=MAX_KEY_BYTES)
    title = _bounded(title, field="title", max_bytes=1024, required=action != "archive")
    body = _bounded(body, field="body", max_bytes=MAX_CONTENT_BYTES, required=action != "archive")
    category = _bounded(category, field="category", max_bytes=128, required=False)
    tags = _bounded(tags, field="tags", max_bytes=MAX_TAGS_BYTES, required=False)
    source_refs = _bounded(source_refs, field="source_refs", max_bytes=MAX_PROVENANCE_BYTES, required=False)
    provenance = _bounded(provenance, field="provenance", max_bytes=MAX_PROVENANCE_BYTES)
    con = _open(repo, "kb")
    try:
        prior = _begin(con, idempotency_key=idempotency_key)
        if prior is not None:
            con.rollback()
            return _idempotent(prior)
        row = con.execute("SELECT id FROM entries WHERE key=?", (key,)).fetchone()
        now = datetime.now(timezone.utc).isoformat()
        ts_columns = _legacy_timestamp_columns(con, "entries")
        ins_col_extra, ins_ph_extra, ins_val_extra = _timestamp_insert_extra(ts_columns, now)
        upd_set_extra, upd_val_extra = _timestamp_update_extra(ts_columns, now)
        if action == "archive":
            if row is None:
                raise ContextWriteError("kb_key_not_found")
            entity_id = int(row["id"])
            con.execute("INSERT OR REPLACE INTO context_entity_state VALUES('kb',?,'archived',NULL,?)", (entity_id, now))
        elif action == "supersede":
            if row is None:
                raise ContextWriteError("kb_key_not_found")
            new_key = _bounded(replacement_key, field="replacement_key", max_bytes=MAX_KEY_BYTES)
            if con.execute("SELECT 1 FROM entries WHERE key=?", (new_key,)).fetchone() is not None:
                raise ContextWriteError("kb_replacement_key_exists")
            old_id = int(row["id"])
            cur = con.execute(
                f"INSERT INTO entries(key,title,body,category,tags,source_refs{ins_col_extra}) "
                f"VALUES(?,?,?,?,?,?{ins_ph_extra})",
                (new_key, title, body, category, tags, source_refs) + ins_val_extra,
            )
            entity_id = int(cur.lastrowid)
            con.execute("INSERT INTO entries_fts(rowid,key,title,body,category,tags) VALUES(?,?,?,?,?,?)", (entity_id, new_key, title, body, category, tags))
            con.execute("INSERT OR REPLACE INTO context_entity_state VALUES('kb',?,'superseded',?,?)", (old_id, entity_id, now))
            key = new_key
        else:
            if row is None:
                cur = con.execute(
                    f"INSERT INTO entries(key,title,body,category,tags,source_refs{ins_col_extra}) "
                    f"VALUES(?,?,?,?,?,?{ins_ph_extra})",
                    (key, title, body, category, tags, source_refs) + ins_val_extra,
                )
                entity_id = int(cur.lastrowid)
            else:
                entity_id = int(row["id"])
                con.execute(
                    f"UPDATE entries SET title=?,body=?,category=?,tags=?,source_refs=?{upd_set_extra} WHERE id=?",
                    (title, body, category, tags, source_refs) + upd_val_extra + (entity_id,),
                )
                con.execute("DELETE FROM entries_fts WHERE rowid=?", (entity_id,))
            con.execute("INSERT INTO entries_fts(rowid,key,title,body,category,tags) VALUES(?,?,?,?,?,?)", (entity_id, key, title, body, category, tags))
        con.execute("INSERT OR REPLACE INTO context_entity_state VALUES('kb',?,'active',NULL,?)", (entity_id, now)) if action != "archive" else None
        _record(con, idempotency_key=idempotency_key, component="kb", action=action,
                entity_key=f"kb:{entity_id}", actor=identity, provenance=provenance,
                payload={"key": key, "title": title, "body": body, "category": category, "tags": tags, "source_refs": source_refs})
        con.commit()
        return {"ok": True, "idempotent": False, "action": action, "key": key, "entry_id": entity_id, "status": "archived" if action == "archive" else "active"}
    except sqlite3.IntegrityError as exc:
        con.rollback()
        raise _integrity_error(exc, component="kb", action=action) from exc
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


__all__ = ["ContextWriteError", "KbAction", "MemoryAction", "SessionAction", "ensure_memories_fts", "kb_write", "memory_write", "session_write"]
