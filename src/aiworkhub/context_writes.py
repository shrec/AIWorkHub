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


def _identity(actor: dict[str, Any]) -> dict[str, str]:
    result = {
        "role": _bounded(actor.get("role", ""), field="actor_role", max_bytes=32),
        "actor_id": _bounded(actor.get("actor_id", ""), field="actor_id", max_bytes=256),
        "task_id": _bounded(actor.get("task_id", ""), field="task_id", max_bytes=256, required=False),
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
        "task_id TEXT NOT NULL,provider TEXT NOT NULL,session_id TEXT NOT NULL,provenance TEXT NOT NULL,"
        "payload_sha256 TEXT NOT NULL,created_at TEXT NOT NULL);"
        "CREATE TABLE IF NOT EXISTS context_entity_state("
        "entity_type TEXT NOT NULL,entity_id INTEGER NOT NULL,status TEXT NOT NULL,"
        "superseded_by INTEGER,updated_at TEXT NOT NULL,PRIMARY KEY(entity_type,entity_id));"
    )
    if db_id == "memory":
        _normalize_memory_schema(con)
    return con


def _ensure_memories_fts(con: sqlite3.Connection) -> dict[str, Any]:
    """Atomic repair: acquire BEGIN IMMEDIATE, re-check, backfill rowid=id.

    Single source of truth for ``memories_fts`` DDL and backfill used by both
    the write-path schema normalizer and the public search-path repair entry
    point.  Returns a bounded outcome dict; callers decide interpretation.
    """
    try:
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
        con.execute("CREATE VIRTUAL TABLE memories_fts USING fts5(key,value,tags,scope)")
        con.execute(
            "INSERT INTO memories_fts(rowid,key,value,tags,scope) "
            "SELECT id,key,value,tags,scope FROM memories"
        )
        con.commit()
        return {"ok": True, "created": True, "reason": "fts_created"}
    except sqlite3.Error as exc:
        try:
            con.rollback()
        except sqlite3.OperationalError:
            pass
        return {"ok": False, "error": "fts_migration_failed", "detail": str(exc)[:160], "sqlite_errorname": str(getattr(exc, "sqlite_errorname", "UNKNOWN"))}


def _normalize_memory_schema(con: sqlite3.Connection) -> None:
    """Repair known legacy ``memories`` schema shapes without losing rows.

    Supersede/archive semantics retain historical rows with the same logical
    key.  Early databases declared ``key UNIQUE``; after an archived row was
    imported, ``remember`` therefore raised an opaque IntegrityError.  Rebuild
    only when an exact unique-key index is observed, preserve ids, and rebuild
    the contentless FTS mirror deterministically via the shared
    ``_ensure_memories_fts`` primitive.  Both paths are idempotent.
    """
    if con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memories'"
    ).fetchone() is None:
        return
    unique_key_index = False
    for row in con.execute("PRAGMA index_list(memories)"):
        # seq, name, unique, origin, partial
        if not bool(row[2]):
            continue
        index_name = str(row[1]).replace('"', '""')
        columns = [
            str(info[2])
            for info in con.execute(f'PRAGMA index_info("{index_name}")')
        ]
        if columns == ["key"]:
            unique_key_index = True
            break
    if unique_key_index:
        con.executescript(
            "DROP TABLE IF EXISTS memories_fts;"
            "ALTER TABLE memories RENAME TO memories_legacy_unique;"
            "CREATE TABLE memories(id INTEGER PRIMARY KEY,key TEXT,value TEXT,tags TEXT,scope TEXT);"
            "INSERT INTO memories(id,key,value,tags,scope) "
            "SELECT id,key,value,tags,scope FROM memories_legacy_unique;"
            "DROP TABLE memories_legacy_unique;"
        )
        con.commit()
    result = _ensure_memories_fts(con)
    if not result.get("ok"):
        raise ContextWriteError(f"fts_normalization_failed:{result.get('error', 'unknown')}")


def ensure_memories_fts(repo: Path) -> dict[str, Any]:
    """Public entry point for search-path FTS repair on the canonical memory DB.

    Opens a fresh writable connection, acquires BEGIN IMMEDIATE, checks
    whether ``memories_fts`` already exists, creates and backfills if absent,
    and returns a bounded outcome dict.  Callers on the read-only search path
    invoke this once before their MATCH query; exact get/related never call
    it.
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
    entity_key: str, actor: dict[str, str], provenance: str, payload: dict[str, Any],
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
                task_id=identity["task_id"],
                occurred_at=timestamp,
            )
        _record(
            con, idempotency_key=idempotency_key, component="session", action=action,
            entity_key=f"document:{doc_id}", actor=identity, provenance=provenance,
            payload={"topic": topic, "content": content},
        )
        con.commit()
        return {"ok": True, "idempotent": False, "action": action, "document_id": doc_id, "timestamp": timestamp}
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


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
        if action == "remember":
            if row is not None and row["status"] == "active":
                raise ContextWriteError("memory_key_exists_use_update")
            cur = con.execute("INSERT INTO memories(key,value,tags,scope) VALUES(?,?,?,?)", (key, value, tags, scope))
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
            con.execute("UPDATE memories SET value=?,tags=?,scope=? WHERE id=?", (value, tags, scope, entity_id))
            con.execute("DELETE FROM memories_fts WHERE rowid=?", (entity_id,))
            con.execute("INSERT INTO memories_fts(rowid,key,value,tags,scope) VALUES(?,?,?,?,?)", (entity_id, key, value, tags, scope))
        else:
            if row is None or row["status"] != "active":
                raise ContextWriteError("memory_active_key_not_found")
            old_id = int(row["id"])
            cur = con.execute("INSERT INTO memories(key,value,tags,scope) VALUES(?,?,?,?)", (key, value, tags, scope))
            entity_id = int(cur.lastrowid)
            con.execute("INSERT INTO memories_fts(rowid,key,value,tags,scope) VALUES(?,?,?,?,?)", (entity_id, key, value, tags, scope))
            con.execute("INSERT OR REPLACE INTO context_entity_state VALUES('memory',?,'superseded',?,?)", (old_id, entity_id, now))
        con.execute("INSERT OR REPLACE INTO context_entity_state VALUES('memory',?,'active',NULL,?)", (entity_id, now)) if action != "archive" else None
        _record(con, idempotency_key=idempotency_key, component="memory", action=action,
                entity_key=f"memory:{entity_id}", actor=identity, provenance=provenance,
                payload={"key": key, "value": value, "tags": tags, "scope": scope})
        con.commit()
        return {"ok": True, "idempotent": False, "action": action, "key": key, "memory_id": entity_id, "status": "archived" if action == "archive" else "active"}
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
            cur = con.execute("INSERT INTO entries(key,title,body,category,tags,source_refs) VALUES(?,?,?,?,?,?)", (new_key, title, body, category, tags, source_refs))
            entity_id = int(cur.lastrowid)
            con.execute("INSERT INTO entries_fts(rowid,key,title,body,category,tags) VALUES(?,?,?,?,?,?)", (entity_id, new_key, title, body, category, tags))
            con.execute("INSERT OR REPLACE INTO context_entity_state VALUES('kb',?,'superseded',?,?)", (old_id, entity_id, now))
            key = new_key
        else:
            if row is None:
                cur = con.execute("INSERT INTO entries(key,title,body,category,tags,source_refs) VALUES(?,?,?,?,?,?)", (key, title, body, category, tags, source_refs))
                entity_id = int(cur.lastrowid)
            else:
                entity_id = int(row["id"])
                con.execute("UPDATE entries SET title=?,body=?,category=?,tags=?,source_refs=? WHERE id=?", (title, body, category, tags, source_refs, entity_id))
                con.execute("DELETE FROM entries_fts WHERE rowid=?", (entity_id,))
            con.execute("INSERT INTO entries_fts(rowid,key,title,body,category,tags) VALUES(?,?,?,?,?,?)", (entity_id, key, title, body, category, tags))
        con.execute("INSERT OR REPLACE INTO context_entity_state VALUES('kb',?,'active',NULL,?)", (entity_id, now)) if action != "archive" else None
        _record(con, idempotency_key=idempotency_key, component="kb", action=action,
                entity_key=f"kb:{entity_id}", actor=identity, provenance=provenance,
                payload={"key": key, "title": title, "body": body, "category": category, "tags": tags, "source_refs": source_refs})
        con.commit()
        return {"ok": True, "idempotent": False, "action": action, "key": key, "entry_id": entity_id, "status": "archived" if action == "archive" else "active"}
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


__all__ = ["ContextWriteError", "KbAction", "MemoryAction", "SessionAction", "ensure_memories_fts", "kb_write", "memory_write", "session_write"]
