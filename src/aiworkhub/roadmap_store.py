"""Durable repository-native Roadmap registry.

Roadmap is the manager-approved layer between inexpensive NeedFix intake and
the executable Task DAG.  It has its own repository-local state and audit
events; neither capturing a NeedFix nor creating a Roadmap item launches a
worker or mutates task lifecycle.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

SCHEMA_ID = "aiworkhub.roadmap_store.v1"
ROADMAP_DB_REL = (".aiworkhub", "tasking", "roadmap.sqlite")
ROADMAP_ID_RE = re.compile(r"^RM-\d{4}-\d{5}$")
NEEDFIX_ID_RE = re.compile(r"^NF-\d{4}-\d{5}$")
TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
MAX_LIST_LIMIT = 500

STATUSES = (
    "proposed",
    "approved",
    "in_progress",
    "blocked",
    "deferred",
    "completed",
    "archived",
)
PRIORITIES = ("critical", "high", "medium", "low")
VALID_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "proposed": ("approved", "deferred", "archived"),
    "approved": ("in_progress", "deferred", "archived"),
    "in_progress": ("blocked", "completed", "deferred", "archived"),
    "blocked": ("in_progress", "deferred", "archived"),
    "deferred": ("approved", "archived"),
    "completed": ("archived",),
    "archived": (),
}


class RoadmapError(Exception):
    """Base Roadmap error."""


class RoadmapNotFoundError(RoadmapError):
    pass


class RoadmapValidationError(RoadmapError):
    pass


class RoadmapConflictError(RoadmapError):
    pass


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db_path(repo_root: str | Path) -> Path:
    return Path(repo_root).joinpath(*ROADMAP_DB_REL)


def _connect(repo_root: str | Path) -> sqlite3.Connection:
    path = _db_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS roadmap_items (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    outcome TEXT NOT NULL,
    status TEXT NOT NULL,
    priority TEXT NOT NULL,
    milestone TEXT NOT NULL DEFAULT '',
    acceptance_json TEXT NOT NULL DEFAULT '[]',
    needfix_ids_json TEXT NOT NULL DEFAULT '[]',
    task_ids_json TEXT NOT NULL DEFAULT '[]',
    depends_on_json TEXT NOT NULL DEFAULT '[]',
    provenance_json TEXT NOT NULL DEFAULT '{}',
    evidence_refs_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    archived_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_roadmap_status ON roadmap_items(status);
CREATE INDEX IF NOT EXISTS idx_roadmap_priority ON roadmap_items(priority);

CREATE TABLE IF NOT EXISTS roadmap_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    roadmap_id TEXT NOT NULL,
    event TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_roadmap_events_id ON roadmap_events(roadmap_id);
"""


def initialize_repository(repo_root: str | Path) -> dict[str, Any]:
    conn = _connect(repo_root)
    try:
        conn.executescript(_SCHEMA_SQL)
        count = int(conn.execute("SELECT COUNT(*) FROM roadmap_items").fetchone()[0])
        return {
            "schema_id": SCHEMA_ID,
            "initialized": True,
            "db_path": str(_db_path(repo_root)),
            "existing_count": count,
        }
    finally:
        conn.close()


def _json_list(value: str) -> list[str]:
    loaded = json.loads(value or "[]")
    return [str(item) for item in loaded] if isinstance(loaded, list) else []


def _row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "title": row["title"],
        "outcome": row["outcome"],
        "status": row["status"],
        "priority": row["priority"],
        "milestone": row["milestone"],
        "acceptance": _json_list(row["acceptance_json"]),
        "needfix_ids": _json_list(row["needfix_ids_json"]),
        "task_ids": _json_list(row["task_ids_json"]),
        "depends_on": _json_list(row["depends_on_json"]),
        "provenance": json.loads(row["provenance_json"] or "{}"),
        "evidence_refs": _json_list(row["evidence_refs_json"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "completed_at": row["completed_at"],
        "archived_at": row["archived_at"],
    }


def _event(
    conn: sqlite3.Connection,
    roadmap_id: str,
    event: str,
    detail: Mapping[str, Any] | None = None,
) -> None:
    conn.execute(
        "INSERT INTO roadmap_events (roadmap_id,event,detail_json,created_at) "
        "VALUES (?,?,?,?)",
        (roadmap_id, event, json.dumps(dict(detail or {})), _utcnow()),
    )


def _next_id(conn: sqlite3.Connection) -> str:
    year = datetime.now(timezone.utc).strftime("%Y")
    row = conn.execute(
        "SELECT id FROM roadmap_items WHERE id LIKE ? ORDER BY id DESC LIMIT 1",
        (f"RM-{year}-%",),
    ).fetchone()
    sequence = int(row["id"].rsplit("-", 1)[1]) + 1 if row else 1
    if sequence > 99999:
        raise RoadmapConflictError(f"roadmap id sequence exhausted for {year}")
    return f"RM-{year}-{sequence:05d}"


def _bounded_strings(
    name: str,
    values: Sequence[str] | None,
    *,
    pattern: re.Pattern[str] | None = None,
    limit: int = 100,
) -> list[str]:
    result = list(dict.fromkeys(str(value).strip() for value in (values or ())))
    if len(result) > limit or any(not value or len(value) > 1000 for value in result):
        raise RoadmapValidationError(f"{name} exceeds bounded contract")
    if pattern and any(not pattern.fullmatch(value) for value in result):
        raise RoadmapValidationError(f"{name} contains malformed identity")
    return result


def _validate_dependencies(
    conn: sqlite3.Connection, roadmap_id: str | None, depends_on: Sequence[str]
) -> None:
    graph: dict[str, list[str]] = {}
    rows = conn.execute("SELECT id,depends_on_json FROM roadmap_items").fetchall()
    for row in rows:
        graph[str(row["id"])] = _json_list(row["depends_on_json"])
    for dependency in depends_on:
        if dependency not in graph:
            raise RoadmapNotFoundError(f"roadmap dependency not found: {dependency}")
    if roadmap_id is None:
        return
    graph[roadmap_id] = list(depends_on)
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visiting:
            raise RoadmapConflictError(f"roadmap dependency cycle: {node}")
        if node in visited:
            return
        visiting.add(node)
        for dependency in graph.get(node, []):
            visit(dependency)
        visiting.remove(node)
        visited.add(node)

    visit(roadmap_id)


def add_item(
    repo_root: str | Path,
    *,
    title: str,
    outcome: str,
    priority: str = "medium",
    milestone: str = "",
    acceptance: Sequence[str] | None = None,
    needfix_ids: Sequence[str] | None = None,
    depends_on: Sequence[str] | None = None,
    provenance: Mapping[str, Any] | None = None,
    evidence_refs: Sequence[str] | None = None,
) -> dict[str, Any]:
    title = str(title or "").strip()
    outcome = str(outcome or "").strip()
    if not title or len(title.encode()) > 1000:
        raise RoadmapValidationError("title is required and bounded")
    if not outcome or len(outcome.encode()) > 100_000:
        raise RoadmapValidationError("outcome is required and bounded")
    if priority not in PRIORITIES:
        raise RoadmapValidationError(f"invalid priority: {priority!r}")
    normalized_acceptance = _bounded_strings("acceptance", acceptance, limit=100)
    normalized_needfix = _bounded_strings(
        "needfix_ids", needfix_ids, pattern=NEEDFIX_ID_RE
    )
    normalized_dependencies = _bounded_strings(
        "depends_on", depends_on, pattern=ROADMAP_ID_RE
    )
    normalized_refs = _bounded_strings("evidence_refs", evidence_refs, limit=200)
    conn = _connect(repo_root)
    try:
        conn.executescript(_SCHEMA_SQL)
        conn.execute("BEGIN IMMEDIATE")
        _validate_dependencies(conn, None, normalized_dependencies)
        roadmap_id = _next_id(conn)
        now = _utcnow()
        conn.execute(
            "INSERT INTO roadmap_items "
            "(id,title,outcome,status,priority,milestone,acceptance_json,"
            "needfix_ids_json,task_ids_json,depends_on_json,provenance_json,"
            "evidence_refs_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                roadmap_id,
                title,
                outcome,
                "proposed",
                priority,
                str(milestone or "")[:500],
                json.dumps(normalized_acceptance),
                json.dumps(normalized_needfix),
                "[]",
                json.dumps(normalized_dependencies),
                json.dumps(dict(provenance or {})),
                json.dumps(normalized_refs),
                now,
                now,
            ),
        )
        _event(conn, roadmap_id, "created", {"status": "proposed"})
        result = get_item(repo_root, roadmap_id, _connection=conn)
        conn.commit()
        return result
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def get_item(
    repo_root: str | Path,
    roadmap_id: str,
    *,
    _connection: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    if not ROADMAP_ID_RE.fullmatch(str(roadmap_id or "")):
        raise RoadmapValidationError("malformed roadmap id")
    conn = _connection or _connect(repo_root)
    try:
        row = conn.execute(
            "SELECT * FROM roadmap_items WHERE id=?", (roadmap_id,)
        ).fetchone()
        if row is None:
            raise RoadmapNotFoundError(roadmap_id)
        return _row(row)
    finally:
        if _connection is None:
            conn.close()


def list_items(
    repo_root: str | Path,
    *,
    status: str | None = None,
    include_archived: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    if status is not None and status not in STATUSES:
        raise RoadmapValidationError(f"invalid status: {status!r}")
    clauses: list[str] = []
    params: list[Any] = []
    if status:
        clauses.append("status=?")
        params.append(status)
    elif not include_archived:
        clauses.append("status!='archived'")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    conn = _connect(repo_root)
    try:
        rows = conn.execute(
            f"SELECT * FROM roadmap_items {where} "
            "ORDER BY CASE priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
            "WHEN 'medium' THEN 2 ELSE 3 END, created_at ASC LIMIT ? OFFSET ?",
            (*params, max(1, min(int(limit), MAX_LIST_LIMIT)), max(0, int(offset))),
        ).fetchall()
        return [_row(row) for row in rows]
    finally:
        conn.close()


def count_items_by_status(
    repo_root: str | Path, *, include_archived: bool = False
) -> dict[str, int]:
    """Return unbounded aggregate truth without loading Roadmap payloads."""
    conn = _connect(repo_root)
    try:
        clauses = "" if include_archived else "WHERE status!='archived'"
        rows = conn.execute(
            f"SELECT status,COUNT(*) AS count FROM roadmap_items {clauses} "
            "GROUP BY status"
        ).fetchall()
        counts = {status: 0 for status in STATUSES}
        for row in rows:
            counts[str(row["status"])] = int(row["count"])
        return counts
    finally:
        conn.close()


def transition_item(
    repo_root: str | Path,
    roadmap_id: str,
    target_status: str,
    *,
    reason: str,
) -> dict[str, Any]:
    reason = str(reason or "").strip()
    if not reason or len(reason.encode()) > 4000:
        raise RoadmapValidationError("bounded transition reason is required")
    if target_status not in STATUSES:
        raise RoadmapValidationError(f"invalid status: {target_status!r}")
    conn = _connect(repo_root)
    try:
        conn.execute("BEGIN IMMEDIATE")
        current = get_item(repo_root, roadmap_id, _connection=conn)
        if current["status"] == target_status:
            conn.commit()
            return current
        allowed = VALID_TRANSITIONS[current["status"]]
        if target_status not in allowed:
            raise RoadmapConflictError(
                f"invalid roadmap transition: current={current['status']!r} "
                f"target={target_status!r} allowed={list(allowed)!r}"
            )
        if target_status in {"approved", "in_progress", "completed"}:
            blockers = [
                dependency
                for dependency in current["depends_on"]
                if get_item(repo_root, dependency, _connection=conn)["status"]
                != "completed"
            ]
            if blockers:
                raise RoadmapConflictError(
                    "roadmap dependencies incomplete: " + ",".join(blockers)
                )
        now = _utcnow()
        cursor = conn.execute(
            "UPDATE roadmap_items SET status=?,updated_at=?,completed_at=?,archived_at=? "
            "WHERE id=? AND status=?",
            (
                target_status,
                now,
                now if target_status == "completed" else current["completed_at"],
                now if target_status == "archived" else current["archived_at"],
                roadmap_id,
                current["status"],
            ),
        )
        if cursor.rowcount != 1:
            raise RoadmapConflictError("roadmap transition lost atomic status race")
        _event(
            conn,
            roadmap_id,
            "transitioned",
            {"from": current["status"], "to": target_status, "reason": reason},
        )
        result = get_item(repo_root, roadmap_id, _connection=conn)
        conn.commit()
        return result
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def link_task(
    repo_root: str | Path, roadmap_id: str, task_id: str
) -> dict[str, Any]:
    if not TASK_ID_RE.fullmatch(str(task_id or "")):
        raise RoadmapValidationError("malformed task id")
    conn = _connect(repo_root)
    try:
        conn.execute("BEGIN IMMEDIATE")
        current = get_item(repo_root, roadmap_id, _connection=conn)
        task_ids = list(dict.fromkeys([*current["task_ids"], task_id]))
        conn.execute(
            "UPDATE roadmap_items SET task_ids_json=?,updated_at=? WHERE id=?",
            (json.dumps(task_ids), _utcnow(), roadmap_id),
        )
        if task_id not in current["task_ids"]:
            _event(conn, roadmap_id, "task_linked", {"task_id": task_id})
        result = get_item(repo_root, roadmap_id, _connection=conn)
        conn.commit()
        return result
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def list_events(
    repo_root: str | Path, roadmap_id: str, *, limit: int = 100
) -> list[dict[str, Any]]:
    get_item(repo_root, roadmap_id)
    conn = _connect(repo_root)
    try:
        rows = conn.execute(
            "SELECT seq,event,detail_json,created_at FROM roadmap_events "
            "WHERE roadmap_id=? ORDER BY seq DESC LIMIT ?",
            (roadmap_id, max(1, min(int(limit), 500))),
        ).fetchall()
        return [
            {
                "seq": int(row["seq"]),
                "event": row["event"],
                "detail": json.loads(row["detail_json"] or "{}"),
                "created_at": row["created_at"],
            }
            for row in rows
        ]
    finally:
        conn.close()
