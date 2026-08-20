"""Append-only conversation ledger and deterministic operational graph.

The ledger and its rebuildable projection share the canonical repository
``transcript`` database.  The ledger is the source of truth; graph nodes and
edges are derived state and may always be rebuilt without model inference.
No caller supplies a database path or repository identity.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from . import capture_adapters, feature_settings, storage_registry
from .sqlite_readonly import connect_readonly


SCHEMA_ID = "aiworkhub.context_graph.v1"
MAX_CONTENT_BYTES = 64 * 1024
MAX_METADATA_BYTES = 16 * 1024
MAX_SOURCE_REF_BYTES = 2048
MAX_RESULTS = 50
MAX_RANGE_SIDE = 50
_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,191}$")
ROLES = frozenset({"user", "assistant", "tool", "system", "manager", "worker"})

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversation_events(
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_uid TEXT UNIQUE NOT NULL,
    repo_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    task_id TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL,
    role TEXT NOT NULL,
    event_type TEXT NOT NULL,
    content TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    source_ref TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    content_sha256 TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conversation_events_thread
ON conversation_events(thread_id,event_id);
CREATE INDEX IF NOT EXISTS idx_conversation_events_task
ON conversation_events(task_id,event_id);
CREATE INDEX IF NOT EXISTS idx_conversation_events_time
ON conversation_events(occurred_at,event_id);
CREATE VIRTUAL TABLE IF NOT EXISTS conversation_events_fts
USING fts5(event_uid UNINDEXED,content,source_ref);

CREATE TABLE IF NOT EXISTS context_nodes(
    node_id TEXT PRIMARY KEY,
    node_type TEXT NOT NULL,
    label TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active',
    source_event_id INTEGER NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_context_nodes_type ON context_nodes(node_type);

CREATE TABLE IF NOT EXISTS context_edges(
    edge_id TEXT PRIMARY KEY,
    src_node_id TEXT NOT NULL,
    relation TEXT NOT NULL,
    dst_node_id TEXT NOT NULL,
    source_event_id INTEGER NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_context_edges_src ON context_edges(src_node_id,relation);
CREATE INDEX IF NOT EXISTS idx_context_edges_dst ON context_edges(dst_node_id,relation);

CREATE TABLE IF NOT EXISTS context_projection_meta(
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class ContextGraphError(RuntimeError):
    """Invalid input, unavailable authority or storage failure."""


def _bounded(value: Any, field: str, maximum: int, *, required: bool = True) -> str:
    if not isinstance(value, str):
        raise ContextGraphError(f"invalid_{field}")
    text = value.strip()
    if (required and not text) or "\x00" in text or len(text.encode("utf-8")) > maximum:
        raise ContextGraphError(f"invalid_{field}")
    return text


def _token(value: Any, field: str, *, required: bool = True) -> str:
    text = _bounded(value, field, 256, required=required)
    if text and not _TOKEN_RE.fullmatch(text):
        raise ContextGraphError(f"invalid_{field}")
    return text


def is_valid_token(value: Any) -> bool:
    """Return True when ``value`` satisfies :func:`append_event`'s token rule.

    A caller that batches many records can pre-filter each identifier with this
    predicate, so a single record whose id violates the strict token charset is
    skipped rather than raising :class:`ContextGraphError` mid-batch and
    discarding every good record captured alongside it.
    """
    try:
        _token(value, "token")
    except ContextGraphError:
        return False
    return True


def _metadata(value: Mapping[str, Any] | None) -> str:
    if value is None:
        return "{}"
    if not isinstance(value, Mapping):
        raise ContextGraphError("invalid_metadata")
    try:
        encoded = json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ContextGraphError("invalid_metadata") from exc
    if len(encoded.encode("utf-8")) > MAX_METADATA_BYTES:
        raise ContextGraphError("metadata_too_large")
    return encoded


def _connect(repo: Path | str, *, read_only: bool = False) -> tuple[sqlite3.Connection, str]:
    root = Path(repo).resolve()
    registry = storage_registry.load_storage_registry(root)
    path = storage_registry.resolve_database_path(registry, "transcript")
    if not path.is_file() or path.is_symlink():
        raise ContextGraphError("canonical_transcript_unavailable")
    if read_only:
        con = connect_readonly(path, timeout=5)
    else:
        con = sqlite3.connect(str(path), timeout=5)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=5000")
    return con, registry.repo.manifest.repo_id


def ensure_schema(repo: Path | str) -> dict[str, Any]:
    con, repo_id = _connect(repo)
    try:
        con.executescript(SCHEMA)
        con.execute(
            "INSERT OR REPLACE INTO context_projection_meta(key,value) VALUES('schema_id',?)",
            (SCHEMA_ID,),
        )
        con.commit()
    finally:
        con.close()
    return {"ok": True, "schema_id": SCHEMA_ID, "repo_id": repo_id, "ready": True}


def status(repo: Path | str) -> dict[str, Any]:
    """Return a bounded read-only runtime summary for Settings/dashboard UI."""
    if not feature_settings.enabled(repo, "context_graph"):
        return feature_settings.disabled_result("context_graph")
    con, repo_id = _connect(repo, read_only=True)
    try:
        tables = {
            str(row["name"])
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
                "('conversation_events','context_nodes','context_edges','context_projection_meta')"
            ).fetchall()
        }
        if "conversation_events" not in tables:
            return {
                "ok": False,
                "ready": False,
                "error": "context_graph_schema_missing",
                "repo_id": repo_id,
            }
        counts = {
            name: int(con.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0])
            for name in ("conversation_events", "context_nodes", "context_edges")
        }
        row = con.execute(
            "SELECT value FROM context_projection_meta WHERE key='last_event_id'"
        ).fetchone()
        return {
            "ok": True,
            "ready": True,
            "schema_id": SCHEMA_ID,
            "repo_id": repo_id,
            "capture_scope": capture_adapters.CAPTURE_SCOPE,
            "capture_adapters": capture_adapters.status_map(),
            "events": counts["conversation_events"],
            "nodes": counts["context_nodes"],
            "edges": counts["context_edges"],
            "last_event_id": int(row["value"]) if row is not None else 0,
        }
    except sqlite3.Error as exc:
        raise ContextGraphError(f"context_graph_status_failed:{type(exc).__name__}") from exc
    finally:
        con.close()


def _node(con: sqlite3.Connection, node_id: str, node_type: str, label: str,
          event_id: int, timestamp: str, metadata: str = "{}") -> None:
    con.execute(
        "INSERT INTO context_nodes(node_id,node_type,label,state,source_event_id,metadata_json,updated_at) "
        "VALUES(?,?,?,'active',?,?,?) ON CONFLICT(node_id) DO UPDATE SET "
        "label=excluded.label,state='active',source_event_id=excluded.source_event_id,"
        "metadata_json=excluded.metadata_json,updated_at=excluded.updated_at",
        (node_id, node_type, label[:500], event_id, metadata, timestamp),
    )


def _edge(con: sqlite3.Connection, src: str, relation: str, dst: str,
          event_id: int, timestamp: str) -> None:
    edge_id = hashlib.sha256(f"{src}\0{relation}\0{dst}\0{event_id}".encode()).hexdigest()
    con.execute(
        "INSERT OR IGNORE INTO context_edges(edge_id,src_node_id,relation,dst_node_id,"
        "source_event_id,metadata_json,created_at) VALUES(?,?,?,?,?,'{}',?)",
        (edge_id, src, relation, dst, event_id, timestamp),
    )


def _project_row(con: sqlite3.Connection, row: sqlite3.Row) -> None:
    event_id = int(row["event_id"])
    event_node = f"event:{row['event_uid']}"
    timestamp = str(row["occurred_at"])
    _node(con, event_node, "event", str(row["content"])[:500] or str(row["event_type"]),
          event_id, timestamp, str(row["metadata_json"]))
    identities = (
        (f"repo:{row['repo_id']}", "repository", str(row["repo_id"]), "IN_REPOSITORY"),
        (f"thread:{row['thread_id']}", "thread", str(row["thread_id"]), "IN_THREAD"),
        (f"session:{row['session_id']}", "session", str(row["session_id"]), "IN_SESSION"),
        (f"actor:{row['provider']}:{row['role']}", "actor", f"{row['provider']}:{row['role']}", "PRODUCED_BY"),
    )
    for node_id, node_type, label, relation in identities:
        _node(con, node_id, node_type, label, event_id, timestamp)
        _edge(con, event_node, relation, node_id, event_id, timestamp)
    if row["task_id"]:
        task_node = f"task:{row['task_id']}"
        _node(con, task_node, "task", str(row["task_id"]), event_id, timestamp)
        _edge(con, event_node, "FOR_TASK", task_node, event_id, timestamp)
    if row["event_type"] == "learning_commit":
        try:
            learning_metadata = json.loads(str(row["metadata_json"] or "{}"))
        except (TypeError, json.JSONDecodeError):
            learning_metadata = {}
        learning_edges = learning_metadata.get("learning_edges")
        if isinstance(learning_edges, list):
            for edge in learning_edges[:16]:
                if not isinstance(edge, dict):
                    continue
                source = str(edge.get("source") or "").strip()
                target = str(edge.get("target") or "").strip()
                relation = str(edge.get("relation") or "").strip()
                if not source or not target or not relation or source == target:
                    continue
                source_node = "learning:" + hashlib.sha256(
                    source.encode("utf-8")
                ).hexdigest()[:32]
                target_node = "learning:" + hashlib.sha256(
                    target.encode("utf-8")
                ).hexdigest()[:32]
                node_metadata = _metadata({"learning_commit": True})
                _node(
                    con, source_node, "learning_concept", source,
                    event_id, timestamp, node_metadata,
                )
                _node(
                    con, target_node, "learning_concept", target,
                    event_id, timestamp, node_metadata,
                )
                _edge(con, event_node, "ASSERTS_RELATION", source_node, event_id, timestamp)
                _edge(con, source_node, relation, target_node, event_id, timestamp)
    previous = con.execute(
        "SELECT event_uid FROM conversation_events WHERE thread_id=? AND event_id<? "
        "ORDER BY event_id DESC LIMIT 1",
        (row["thread_id"], event_id),
    ).fetchone()
    if previous is not None:
        _edge(con, f"event:{previous['event_uid']}", "PRECEDES", event_node, event_id, timestamp)


def append_event(
    repo: Path | str,
    *,
    thread_id: str,
    session_id: str,
    provider: str,
    role: str,
    event_type: str,
    content: str,
    source_ref: str,
    idempotency_key: str,
    task_id: str = "",
    metadata: Mapping[str, Any] | None = None,
    occurred_at: str = "",
) -> dict[str, Any]:
    if not feature_settings.enabled(repo, "context_graph"):
        return feature_settings.disabled_result("context_graph")
    thread_id = _token(thread_id, "thread_id")
    session_id = _token(session_id, "session_id")
    provider = _token(provider, "provider")
    role = _token(role, "role")
    if role not in ROLES:
        raise ContextGraphError("invalid_role")
    event_type = _token(event_type, "event_type")
    task_id = _token(task_id, "task_id", required=False)
    content = _bounded(content, "content", MAX_CONTENT_BYTES, required=False)
    source_ref = _bounded(source_ref, "source_ref", MAX_SOURCE_REF_BYTES)
    if not _IDEMPOTENCY_RE.fullmatch(idempotency_key):
        raise ContextGraphError("invalid_idempotency_key")
    metadata_json = _metadata(metadata)
    occurred_at = occurred_at.strip() or datetime.now(timezone.utc).isoformat()
    occurred_at = _bounded(occurred_at, "occurred_at", 80)
    ingested_at = datetime.now(timezone.utc).isoformat()
    content_sha = hashlib.sha256(content.encode("utf-8")).hexdigest()

    con, repo_id = _connect(repo)
    try:
        con.executescript(SCHEMA)
        event_uid = hashlib.sha256(f"{repo_id}\0{idempotency_key}".encode()).hexdigest()
        con.execute("BEGIN IMMEDIATE")
        existing = con.execute(
            "SELECT event_id,event_uid,occurred_at FROM conversation_events WHERE event_uid=?",
            (event_uid,),
        ).fetchone()
        if existing is not None:
            con.rollback()
            return {
                "ok": True,
                "idempotent": True,
                "event_id": int(existing["event_id"]),
                "event_uid": str(existing["event_uid"]),
                "occurred_at": str(existing["occurred_at"]),
            }
        cur = con.execute(
            "INSERT INTO conversation_events(event_uid,repo_id,thread_id,session_id,task_id,provider,"
            "role,event_type,content,metadata_json,source_ref,occurred_at,ingested_at,content_sha256) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (event_uid, repo_id, thread_id, session_id, task_id, provider, role, event_type,
             content, metadata_json, source_ref, occurred_at, ingested_at, content_sha),
        )
        if cur.lastrowid is None:
            raise ContextGraphError("context_graph_event_id_missing")
        event_id = int(cur.lastrowid)
        con.execute(
            "INSERT INTO conversation_events_fts(rowid,event_uid,content,source_ref) VALUES(?,?,?,?)",
            (event_id, event_uid, content, source_ref),
        )
        row = con.execute("SELECT * FROM conversation_events WHERE event_id=?", (event_id,)).fetchone()
        assert row is not None
        _project_row(con, row)
        con.execute(
            "INSERT OR REPLACE INTO context_projection_meta(key,value) VALUES('last_event_id',?)",
            (str(event_id),),
        )
        con.commit()
        return {
            "ok": True,
            "idempotent": False,
            "schema_id": SCHEMA_ID,
            "event_id": event_id,
            "event_uid": event_uid,
            "occurred_at": occurred_at,
            "content_sha256": content_sha,
        }
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def append_session_document_in_transaction(
    con: sqlite3.Connection,
    *,
    repo_id: str,
    thread_id: str,
    session_id: str,
    provider: str,
    role: str,
    action: str,
    topic: str,
    content: str,
    document_id: int,
    task_id: str = "",
    occurred_at: str,
) -> dict[str, Any]:
    """Project one canonical Session document in its existing transaction."""
    source_ref = f"session_document:{document_id}"
    idempotency_key = f"session-document-{repo_id}-{document_id}"
    event_uid = hashlib.sha256(f"{repo_id}\0{idempotency_key}".encode()).hexdigest()
    existing = con.execute(
        "SELECT event_id,event_uid FROM conversation_events WHERE event_uid=?", (event_uid,)
    ).fetchone()
    if existing is not None:
        return {"ok": True, "idempotent": True, "event_id": int(existing["event_id"])}
    metadata_json = _metadata({"topic": topic, "document_id": document_id})
    content_sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
    cur = con.execute(
        "INSERT INTO conversation_events(event_uid,repo_id,thread_id,session_id,task_id,provider,"
        "role,event_type,content,metadata_json,source_ref,occurred_at,ingested_at,content_sha256) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (event_uid, repo_id, thread_id, session_id, task_id, provider, role,
         f"session_{action}", content, metadata_json, source_ref, occurred_at,
         datetime.now(timezone.utc).isoformat(), content_sha),
    )
    if cur.lastrowid is None:
        raise ContextGraphError("context_graph_event_id_missing")
    event_id = int(cur.lastrowid)
    con.execute(
        "INSERT INTO conversation_events_fts(rowid,event_uid,content,source_ref) VALUES(?,?,?,?)",
        (event_id, event_uid, content, source_ref),
    )
    row = con.execute("SELECT * FROM conversation_events WHERE event_id=?", (event_id,)).fetchone()
    assert row is not None
    _project_row(con, row)
    con.execute(
        "INSERT OR REPLACE INTO context_projection_meta(key,value) VALUES('last_event_id',?)",
        (str(event_id),),
    )
    return {"ok": True, "idempotent": False, "event_id": event_id, "event_uid": event_uid}


def rebuild_projection(repo: Path | str) -> dict[str, Any]:
    if not feature_settings.enabled(repo, "context_graph"):
        return feature_settings.disabled_result("context_graph")
    con, _repo_id = _connect(repo)
    try:
        con.executescript(SCHEMA)
        con.execute("BEGIN IMMEDIATE")
        con.execute("DELETE FROM context_edges")
        con.execute("DELETE FROM context_nodes")
        rows = con.execute("SELECT * FROM conversation_events ORDER BY event_id").fetchall()
        for row in rows:
            _project_row(con, row)
        last_id = int(rows[-1]["event_id"]) if rows else 0
        con.execute(
            "INSERT OR REPLACE INTO context_projection_meta(key,value) VALUES('last_event_id',?)",
            (str(last_id),),
        )
        con.commit()
        return {"ok": True, "events_projected": len(rows), "last_event_id": last_id}
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def search(repo: Path | str, query: str, *, limit: int = 12) -> dict[str, Any]:
    if not feature_settings.enabled(repo, "context_graph"):
        return feature_settings.disabled_result("context_graph")
    query = _bounded(query, "query", 2048)
    try:
        limit = max(1, min(int(limit), MAX_RESULTS))
    except (TypeError, ValueError):
        raise ContextGraphError("invalid_limit") from None
    con, _repo_id = _connect(repo, read_only=True)
    try:
        terms = [term for term in re.findall(r"[\w.-]+", query, re.UNICODE) if term]
        rows: list[sqlite3.Row] = []
        if terms:
            expression = " AND ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms[:12])
            try:
                rows = con.execute(
                    "SELECT e.* FROM conversation_events_fts f JOIN conversation_events e ON e.event_id=f.rowid "
                    "WHERE conversation_events_fts MATCH ? ORDER BY bm25(conversation_events_fts),e.event_id DESC LIMIT ?",
                    (expression, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []
        if not rows:
            rows = con.execute(
                "SELECT * FROM conversation_events WHERE content LIKE ? OR source_ref LIKE ? "
                "ORDER BY event_id DESC LIMIT ?",
                (f"%{query}%", f"%{query}%", limit),
            ).fetchall()
        results = [_event_result(row) for row in rows]
        return {"ok": True, "schema_id": SCHEMA_ID, "query": query, "count": len(results), "results": results}
    except sqlite3.Error as exc:
        raise ContextGraphError(f"context_graph_search_failed:{type(exc).__name__}") from exc
    finally:
        con.close()


def _event_result(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "event_id": int(row["event_id"]),
        "event_uid": str(row["event_uid"]),
        "thread_id": str(row["thread_id"]),
        "session_id": str(row["session_id"]),
        "task_id": str(row["task_id"]),
        "provider": str(row["provider"]),
        "role": str(row["role"]),
        "event_type": str(row["event_type"]),
        "content": str(row["content"]),
        "source_ref": str(row["source_ref"]),
        "occurred_at": str(row["occurred_at"]),
        "content_sha256": str(row["content_sha256"]),
    }


def get_range(
    repo: Path | str, *, thread_id: str, around_event_id: int,
    before: int = 5, after: int = 5,
) -> dict[str, Any]:
    if not feature_settings.enabled(repo, "context_graph"):
        return feature_settings.disabled_result("context_graph")
    thread_id = _token(thread_id, "thread_id")
    if isinstance(around_event_id, bool) or not isinstance(around_event_id, int) or around_event_id < 1:
        raise ContextGraphError("invalid_around_event_id")
    before = max(0, min(int(before), MAX_RANGE_SIDE))
    after = max(0, min(int(after), MAX_RANGE_SIDE))
    con, _repo_id = _connect(repo, read_only=True)
    try:
        center = con.execute(
            "SELECT * FROM conversation_events WHERE event_id=? AND thread_id=?",
            (around_event_id, thread_id),
        ).fetchone()
        if center is None:
            return {"ok": False, "error": "event_not_found", "events": []}
        prior = con.execute(
            "SELECT * FROM conversation_events WHERE thread_id=? AND event_id<? "
            "ORDER BY event_id DESC LIMIT ?", (thread_id, around_event_id, before),
        ).fetchall()[::-1]
        following = con.execute(
            "SELECT * FROM conversation_events WHERE thread_id=? AND event_id>? "
            "ORDER BY event_id LIMIT ?", (thread_id, around_event_id, after),
        ).fetchall()
        events = [_event_result(row) for row in (*prior, center, *following)]
        return {"ok": True, "thread_id": thread_id, "center_event_id": around_event_id, "count": len(events), "events": events}
    finally:
        con.close()


def related(repo: Path | str, *, node_id: str, limit: int = 20) -> dict[str, Any]:
    if not feature_settings.enabled(repo, "context_graph"):
        return feature_settings.disabled_result("context_graph")
    node_id = _bounded(node_id, "node_id", 512)
    limit = max(1, min(int(limit), MAX_RESULTS))
    con, _repo_id = _connect(repo, read_only=True)
    try:
        rows = con.execute(
            "SELECT e.src_node_id,e.relation,e.dst_node_id,e.source_event_id,"
            "s.node_type AS src_type,s.label AS src_label,d.node_type AS dst_type,d.label AS dst_label "
            "FROM context_edges e LEFT JOIN context_nodes s ON s.node_id=e.src_node_id "
            "LEFT JOIN context_nodes d ON d.node_id=e.dst_node_id "
            "WHERE e.src_node_id=? OR e.dst_node_id=? ORDER BY e.source_event_id DESC LIMIT ?",
            (node_id, node_id, limit),
        ).fetchall()
        return {"ok": True, "node_id": node_id, "count": len(rows), "relations": [dict(row) for row in rows]}
    finally:
        con.close()


__all__ = [
    "ContextGraphError", "SCHEMA", "SCHEMA_ID", "append_event",
    "append_session_document_in_transaction", "ensure_schema", "get_range",
    "is_valid_token", "rebuild_projection", "related", "search", "status",
]
