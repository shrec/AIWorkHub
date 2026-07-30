"""Explicit, repository-local legacy context importer.

The importer never discovers global paths and never replaces a canonical
database.  A verified manager selects one repository-relative SQLite file and
one component.  Dry-run and apply use the same deterministic plan; apply adds
only new rows and records their exact canonical row ids so rollback can remove
only data created by that import run.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from . import repository_state, storage_registry


Component = Literal["session", "memory", "kb"]
Operation = Literal["dry_run", "apply", "rollback"]

MAX_IMPORT_ROWS = 100_000
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,191}$")
_SPECS: dict[str, dict[str, Any]] = {
    "session": {
        "db_id": "transcript",
        "table": "documents",
        "columns": ("source_id", "timestamp", "kind", "content"),
        "fts": "documents_fts",
    },
    "memory": {
        "db_id": "memory",
        "table": "memories",
        "columns": ("key", "value", "tags", "scope"),
        "fts": "memories_fts",
    },
    "kb": {
        "db_id": "kb",
        "table": "entries",
        "columns": ("key", "title", "body", "category", "tags", "source_refs"),
        "fts": "entries_fts",
    },
}


class ContextImportError(RuntimeError):
    pass


def _source_path(repo: Path, value: str) -> Path:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ContextImportError("source_path_required")
    rel = Path(value.strip())
    if rel.is_absolute() or ".." in rel.parts:
        raise ContextImportError("source_path_must_be_repo_relative")
    cursor = repo
    for part in rel.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ContextImportError("source_path_symlink_forbidden")
    source = (repo / rel).resolve(strict=False)
    hub = (repo / repository_state.HUB_DIRNAME).resolve()
    if source == hub or hub in source.parents:
        raise ContextImportError("canonical_storage_cannot_be_import_source")
    if not source.is_file():
        raise ContextImportError("source_database_missing")
    return source


def _open_source(path: Path) -> sqlite3.Connection:
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        if con.execute("PRAGMA quick_check(1)").fetchone()[0] != "ok":
            raise ContextImportError("source_quick_check_failed")
        return con
    except sqlite3.Error as exc:
        raise ContextImportError(f"source_database_invalid:{type(exc).__name__}") from exc


def _rows(source: sqlite3.Connection, component: str, limit: int) -> list[dict[str, str]]:
    spec = _SPECS[component]
    table = spec["table"]
    columns = tuple(spec["columns"])
    known = {
        str(row["name"])
        for row in source.execute(f"PRAGMA table_info({table})").fetchall()
    }
    missing = [column for column in columns if column not in known]
    if missing:
        raise ContextImportError(f"source_schema_missing:{table}:{','.join(missing)}")
    count = int(source.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    if count > limit:
        raise ContextImportError(f"source_rows_exceed_limit:{count}:{limit}")
    selected = source.execute(
        f"SELECT {','.join(columns)} FROM {table} ORDER BY rowid"
    ).fetchall()
    return [{column: str(row[column] or "") for column in columns} for row in selected]


def _fingerprint(component: str, rows: list[dict[str, str]]) -> str:
    payload = json.dumps(
        {"component": component, "rows": rows},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _ensure_ledger(con: sqlite3.Connection) -> None:
    con.executescript(
        "CREATE TABLE IF NOT EXISTS context_import_runs("
        "import_id TEXT PRIMARY KEY,idempotency_key TEXT UNIQUE NOT NULL,component TEXT NOT NULL,"
        "source_path TEXT NOT NULL,source_fingerprint TEXT NOT NULL,status TEXT NOT NULL,"
        "actor_id TEXT NOT NULL,provider TEXT NOT NULL,provenance TEXT NOT NULL,"
        "report_json TEXT NOT NULL,created_at TEXT NOT NULL,rolled_back_at TEXT NOT NULL DEFAULT '',"
        "rollback_actor_id TEXT NOT NULL DEFAULT '',rollback_provenance TEXT NOT NULL DEFAULT '');"
        "CREATE TABLE IF NOT EXISTS context_import_items("
        "import_id TEXT NOT NULL,entity_id INTEGER NOT NULL,entity_key TEXT NOT NULL,"
        "payload_sha256 TEXT NOT NULL,"
        "PRIMARY KEY(import_id,entity_id));"
        "CREATE TABLE IF NOT EXISTS context_entity_state("
        "entity_type TEXT NOT NULL,entity_id INTEGER NOT NULL,status TEXT NOT NULL,"
        "superseded_by INTEGER,updated_at TEXT NOT NULL,PRIMARY KEY(entity_type,entity_id));"
    )
    item_columns = {
        str(row["name"])
        for row in con.execute("PRAGMA table_info(context_import_items)").fetchall()
    }
    if "payload_sha256" not in item_columns:
        con.execute(
            "ALTER TABLE context_import_items ADD COLUMN payload_sha256 TEXT NOT NULL DEFAULT ''"
        )


def _existing(con: sqlite3.Connection, component: str, row: dict[str, str]) -> tuple[str, int | None]:
    if component == "session":
        found = con.execute(
            "SELECT doc_id FROM documents WHERE source_id=? AND timestamp=? AND kind=? AND content=? LIMIT 1",
            (row["source_id"], row["timestamp"], row["kind"], row["content"]),
        ).fetchone()
        return ("duplicate", int(found[0])) if found else ("new", None)
    table = "memories" if component == "memory" else "entries"
    found = con.execute(f"SELECT * FROM {table} WHERE key=? ORDER BY rowid DESC LIMIT 1", (row["key"],)).fetchone()
    if found is None:
        return "new", None
    columns = _SPECS[component]["columns"]
    same = all(str(found[column] or "") == row[column] for column in columns)
    return ("duplicate" if same else "conflict"), int(found["id"])


def _plan(con: sqlite3.Connection, component: str, rows: list[dict[str, str]]) -> dict[str, Any]:
    planned: list[dict[str, Any]] = []
    counts = {"source": len(rows), "new": 0, "duplicate": 0, "conflict": 0}
    for ordinal, row in enumerate(rows):
        state, entity_id = _existing(con, component, row)
        counts[state] += 1
        planned.append({"ordinal": ordinal, "state": state, "entity_id": entity_id, "row": row})
    return {"counts": counts, "rows": planned}


def _insert(con: sqlite3.Connection, component: str, row: dict[str, str]) -> tuple[int, str]:
    if component == "session":
        cur = con.execute(
            "INSERT INTO documents(source_id,timestamp,kind,content) VALUES(?,?,?,?)",
            (row["source_id"], row["timestamp"], row["kind"], row["content"]),
        )
        entity_id = int(cur.lastrowid)
        con.execute("INSERT INTO documents_fts(rowid,content) VALUES(?,?)", (entity_id, row["content"]))
        return entity_id, f"document:{entity_id}"
    if component == "memory":
        cur = con.execute(
            "INSERT INTO memories(key,value,tags,scope) VALUES(?,?,?,?)",
            (row["key"], row["value"], row["tags"], row["scope"]),
        )
        entity_id = int(cur.lastrowid)
        con.execute(
            "INSERT INTO memories_fts(rowid,key,value,tags,scope) VALUES(?,?,?,?,?)",
            (entity_id, row["key"], row["value"], row["tags"], row["scope"]),
        )
        return entity_id, row["key"]
    cur = con.execute(
        "INSERT INTO entries(key,title,body,category,tags,source_refs) VALUES(?,?,?,?,?,?)",
        (row["key"], row["title"], row["body"], row["category"], row["tags"], row["source_refs"]),
    )
    entity_id = int(cur.lastrowid)
    con.execute(
        "INSERT INTO entries_fts(rowid,key,title,body,category,tags) VALUES(?,?,?,?,?,?)",
        (entity_id, row["key"], row["title"], row["body"], row["category"], row["tags"]),
    )
    return entity_id, row["key"]


def _rollback(
    con: sqlite3.Connection,
    component: str,
    import_id: str,
    *,
    actor_id: str,
    provenance: str,
) -> dict[str, Any]:
    run = con.execute(
        "SELECT status,report_json FROM context_import_runs WHERE import_id=? AND component=?",
        (import_id, component),
    ).fetchone()
    if run is None:
        raise ContextImportError("import_run_not_found")
    if str(run["status"]) == "rolled_back":
        return {"ok": True, "idempotent": True, "operation": "rollback", "import_id": import_id}
    items = con.execute(
        "SELECT entity_id,payload_sha256 FROM context_import_items "
        "WHERE import_id=? ORDER BY entity_id",
        (import_id,),
    ).fetchall()
    table, fts, id_col = {
        "session": ("documents", "documents_fts", "doc_id"),
        "memory": ("memories", "memories_fts", "id"),
        "kb": ("entries", "entries_fts", "id"),
    }[component]
    columns = tuple(_SPECS[component]["columns"])
    # Refuse rollback if a later canonical operation changed an imported row;
    # deleting it would erase post-import work that this run does not own.
    for item in items:
        entity_id = int(item["entity_id"])
        current = con.execute(
            f"SELECT {','.join(columns)} FROM {table} WHERE {id_col}=?",
            (entity_id,),
        ).fetchone()
        if current is None:
            raise ContextImportError(f"rollback_entity_missing:{entity_id}")
        payload = {column: str(current[column] or "") for column in columns}
        if _fingerprint(component, [payload]) != str(item["payload_sha256"]):
            raise ContextImportError(f"rollback_entity_changed:{entity_id}")
    for item in items:
        entity_id = int(item["entity_id"])
        con.execute(f"DELETE FROM {fts} WHERE rowid=?", (entity_id,))
        con.execute(f"DELETE FROM {table} WHERE {id_col}=?", (entity_id,))
        con.execute(
            "DELETE FROM context_entity_state WHERE entity_type=? AND entity_id=?",
            ("memory" if component == "memory" else "kb", entity_id),
        ) if component != "session" else None
    now = datetime.now(timezone.utc).isoformat()
    con.execute(
        "UPDATE context_import_runs SET status='rolled_back',rolled_back_at=?,"
        "rollback_actor_id=?,rollback_provenance=? WHERE import_id=?",
        (now, actor_id, provenance, import_id),
    )
    return {"ok": True, "idempotent": False, "operation": "rollback", "import_id": import_id, "removed": len(items)}


def import_context(
    repo: Path,
    *,
    component: Component,
    operation: Operation,
    source_path: str = "",
    idempotency_key: str = "",
    import_id: str = "",
    limit: int = 10_000,
    actor_id: str = "",
    provider: str = "",
    provenance: str = "",
) -> dict[str, Any]:
    """Plan, apply or rollback one explicit legacy context-store import."""

    if component not in _SPECS:
        raise ContextImportError("component_invalid")
    if operation not in {"dry_run", "apply", "rollback"}:
        raise ContextImportError("operation_invalid")
    limit = max(1, min(int(limit), MAX_IMPORT_ROWS))
    registry = storage_registry.load_storage_registry(repo)
    canonical = storage_registry.resolve_database_path(registry, _SPECS[component]["db_id"])
    con = sqlite3.connect(str(canonical), timeout=10)
    con.row_factory = sqlite3.Row
    try:
        if operation == "rollback":
            _ensure_ledger(con)
            if not _ID_RE.fullmatch(import_id):
                raise ContextImportError("import_id_invalid")
            if not actor_id.strip() or not provenance.strip():
                raise ContextImportError("rollback_provenance_required")
            con.execute("BEGIN IMMEDIATE")
            result = _rollback(
                con,
                component,
                import_id,
                actor_id=actor_id.strip()[:256],
                provenance=provenance.strip()[:2048],
            )
            con.commit()
            return result
        source = _source_path(repo, source_path)
        source_con = _open_source(source)
        try:
            rows = _rows(source_con, component, limit)
        finally:
            source_con.close()
        fingerprint = _fingerprint(component, rows)
        plan = _plan(con, component, rows)
        summary = {
            "ok": True,
            "operation": operation,
            "component": component,
            "source_path": str(source.relative_to(repo)),
            "source_fingerprint": fingerprint,
            **plan["counts"],
        }
        if operation == "dry_run":
            con.rollback()
            return summary
        _ensure_ledger(con)
        if not _ID_RE.fullmatch(idempotency_key):
            raise ContextImportError("idempotency_key_invalid")
        if not actor_id.strip() or not provider.strip() or not provenance.strip():
            raise ContextImportError("import_provenance_required")
        prior = con.execute(
            "SELECT import_id,report_json,status FROM context_import_runs WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        if prior is not None:
            report = json.loads(str(prior["report_json"]))
            return {**report, "ok": True, "idempotent": True, "status": str(prior["status"])}
        run_id = "import_" + hashlib.sha256(
            f"{component}\0{source.relative_to(repo)}\0{fingerprint}\0{idempotency_key}".encode("utf-8")
        ).hexdigest()[:32]
        con.execute("BEGIN IMMEDIATE")
        inserted = 0
        for item in plan["rows"]:
            if item["state"] != "new":
                continue
            entity_id, entity_key = _insert(con, component, item["row"])
            con.execute(
                "INSERT INTO context_import_items(import_id,entity_id,entity_key,payload_sha256) "
                "VALUES(?,?,?,?)",
                (run_id, entity_id, entity_key, _fingerprint(component, [item["row"]])),
            )
            inserted += 1
        report = {**summary, "operation": "apply", "import_id": run_id, "inserted": inserted}
        con.execute(
            "INSERT INTO context_import_runs(import_id,idempotency_key,component,source_path,"
            "source_fingerprint,status,actor_id,provider,provenance,report_json,created_at) "
            "VALUES(?,?,?,?,?,'applied',?,?,?,?,?)",
            (
                run_id,
                idempotency_key,
                component,
                str(source.relative_to(repo)),
                fingerprint,
                actor_id.strip()[:256],
                provider.strip()[:64],
                provenance.strip()[:2048],
                json.dumps(report, ensure_ascii=False, sort_keys=True),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        con.commit()
        return {**report, "idempotent": False, "status": "applied"}
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


__all__ = ["Component", "ContextImportError", "Operation", "import_context"]
