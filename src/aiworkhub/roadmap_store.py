"""Durable repository-native Roadmap registry.

Roadmap is the manager-approved layer between inexpensive NeedFix intake and
the executable Task DAG.  It has its own repository-local state and audit
events; neither capturing a NeedFix nor creating a Roadmap item launches a
worker or mutates task lifecycle.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import sqlite_readonly

SCHEMA_ID = "aiworkhub.roadmap_store.v1"
ROADMAP_DB_REL = (".aiworkhub", "tasking", "roadmap.sqlite")
ROADMAP_ID_RE = re.compile(r"^RM-\d{4}-\d{5}$")
NEEDFIX_ID_RE = re.compile(r"^NF-\d{4}-\d{5}$")
TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
WAVE_GOAL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
WAVE_GOAL_SUCCESSOR_EVENT = "wave_goal_successor_bound"
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


def item_revision(item: Mapping[str, Any]) -> str:
    """Digest of the Roadmap fields a completion verdict was decided from."""
    payload = {
        key: item.get(key) for key in ("id", "status", "acceptance", "provenance")
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def transition_item(
    repo_root: str | Path,
    roadmap_id: str,
    target_status: str,
    *,
    reason: str,
    expected_revision: str | None = None,
    evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Transition one item atomically; an already-reached target writes nothing.

    ``expected_revision`` refuses the move when the item's goals, acceptance or
    status changed after the caller decided it (see :func:`item_revision`).
    ``evidence`` is recorded in the same durable ``transitioned`` event.
    """
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
        if expected_revision is not None and item_revision(current) != expected_revision:
            raise RoadmapConflictError("roadmap_revision_changed")
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
        detail: dict[str, Any] = {
            "from": current["status"], "to": target_status, "reason": reason,
        }
        if evidence is not None:
            detail["evidence"] = dict(evidence)
        _event(conn, roadmap_id, "transitioned", detail)
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


# --- Retired-task outcomes ---------------------------------------------------
#
# Retention deletes task rows, so a linked task's final status would otherwise
# vanish. The outcome is kept as ``task_retired`` events in the existing events
# table (no schema change: the Roadmap DB is synced by row union). A live task
# row always wins over a recorded event when the snapshot resolves a status.

TASK_RETIRED_EVENT = "task_retired"
_RETIRED_STATUS_MAX_BYTES = 64
_RETIRED_LOOKUP_CHUNK = 500


def record_tasks_retired(
    repo_root: str | Path, statuses: Mapping[str, str]
) -> int:
    """Append ``task_retired`` to every item linking a task; return items touched.

    A repository with no Roadmap DB (or no items) is a no-op returning 0.
    All events are written in one transaction.
    """
    clean: dict[str, str] = {}
    for task_id, status in statuses.items():
        task_id, status = str(task_id), str(status or "").strip()
        if not TASK_ID_RE.fullmatch(task_id):
            continue  # link_task rejects such ids, so nothing can link them
        if not status or len(status.encode()) > _RETIRED_STATUS_MAX_BYTES:
            raise RoadmapValidationError("malformed retired task status")
        clean[task_id] = status
    if not clean or not _db_path(repo_root).exists():
        return 0

    def linking_items(conn: sqlite3.Connection) -> list[tuple[str, list[str]]]:
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='roadmap_items'"
        ).fetchone():
            return []
        found: list[tuple[str, list[str]]] = []
        for row in conn.execute("SELECT id,task_ids_json FROM roadmap_items").fetchall():
            linked = [t for t in _json_list(row["task_ids_json"]) if t in clean]
            if linked:
                found.append((row["id"], linked))
        return found

    conn = _connect(repo_root)
    try:
        # A plain read decides first: a batch no item links must not contend
        # for the Roadmap write lock. The linking set is re-read under the lock.
        if not linking_items(conn):
            return 0
        conn.execute("BEGIN IMMEDIATE")
        touched = 0
        for roadmap_id, linked in linking_items(conn):
            for task_id in linked:
                _event(
                    conn,
                    roadmap_id,
                    TASK_RETIRED_EVENT,
                    {"task_id": task_id, "status": clean[task_id]},
                )
            touched += 1
        conn.commit()
        return touched
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def record_task_retired(repo_root: str | Path, task_id: str, status: str) -> int:
    return record_tasks_retired(repo_root, {task_id: status})


def retired_task_statuses(
    repo_root: str | Path, task_ids: Sequence[str]
) -> dict[str, str]:
    """Return ``{task_id: latest recorded retired status}`` for the given ids.

    A repository with no Roadmap DB yields ``{}`` without creating one.
    """
    ids = sorted({str(task_id) for task_id in task_ids if task_id})
    if not ids or not _db_path(repo_root).exists():
        return {}
    conn = _connect(repo_root)
    try:
        found: dict[str, str] = {}
        for start in range(0, len(ids), _RETIRED_LOOKUP_CHUNK):
            chunk = ids[start : start + _RETIRED_LOOKUP_CHUNK]
            marks = ",".join("?" for _ in chunk)
            rows = conn.execute(
                "SELECT json_extract(detail_json,'$.task_id') AS task_id,"
                "json_extract(detail_json,'$.status') AS status,MAX(seq) AS seq "
                "FROM roadmap_events WHERE event=? "
                f"AND json_extract(detail_json,'$.task_id') IN ({marks}) "
                "GROUP BY task_id",
                (TASK_RETIRED_EVENT, *chunk),
            ).fetchall()
            found.update(
                {str(row["task_id"]): str(row["status"]) for row in rows if row["status"]}
            )
        return found
    finally:
        conn.close()


_SUPERSEDE_MAX_HOPS = 16


def _superseded_successor_outcome(
    start: Mapping[str, Any],
    start_id: str,
    get_card: Callable[[str], Mapping[str, Any] | None],
    canonical_status: Callable[[Mapping[str, Any]], str],
    retired: Callable[[str], str | None],
) -> tuple[str | None, str]:
    """Follow ``superseded_by`` from ``start``; return ``(outcome, chain_text)``.

    ``outcome`` is ``"finished"`` only when the chain ends at a finished
    successor; any unfinished, missing, cyclic or over-long chain yields
    ``None``.  ``chain_text`` names every hop followed and why it stopped.
    """
    seen = {start_id}
    hops: list[str] = []
    current: Mapping[str, Any] | None = start
    while current is not None:
        nxt = str(current.get("superseded_by") or "").strip()
        if not nxt:
            break
        hops.append(nxt)
        if nxt in seen:
            return None, "->".join(["superseded", *hops]) + ":cycle"
        if len(hops) > _SUPERSEDE_MAX_HOPS:
            return None, "->".join(["superseded", *hops]) + ":too_long"
        seen.add(nxt)
        current = get_card(nxt)
        if current is None:
            outcome = retired(nxt) or "missing"
            return (
                "finished" if outcome == "finished" else None,
                "->".join(["superseded", *hops]) + f":{outcome}",
            )
        outcome = canonical_status({**current, "archived_at": ""})
        if outcome == "finished":
            return "finished", "->".join(["superseded", *hops]) + ":finished"
    last = canonical_status({**current, "archived_at": ""}) if current else "missing"
    return None, "->".join(["superseded", *hops]) + f":{last}"


def linked_task_outcomes(
    repo_root: str | Path,
    task_ids: Sequence[str],
    cards: Mapping[str, Mapping[str, Any] | None],
    canonical_status: Callable[[Mapping[str, Any]], str],
    get_card: Callable[[str], Mapping[str, Any] | None] | None = None,
) -> list[tuple[str, str, str | None]]:
    """Resolve each linked id to ``(task_id, display_status, outcome_status)``.

    A live card displays under the archive overlay while its outcome is read
    with ``archived_at`` cleared, so an accepted-then-archived card stays
    ``finished``. An id with no live card falls back to its latest retired
    record (``purged:<status>``); with neither it is ``missing`` and has no
    outcome. One retired lookup covers every id without a live card.

    With ``get_card``, an unfinished live card carrying ``superseded_by`` is
    followed (cycle-safe, bounded) to its successor; a chain ending at a
    finished outcome resolves ``finished`` with a display status naming the
    chain (``superseded->r2:finished``). Any other chain keeps the original
    outcome and appends the followed chain to the display status.
    """
    missing = [task_id for task_id in task_ids if cards.get(task_id) is None]
    retired = retired_task_statuses(repo_root, missing) if missing else {}
    resolved: list[tuple[str, str, str | None]] = []
    for task_id in task_ids:
        card = cards.get(task_id)
        if card is not None:
            display = canonical_status(card)
            outcome = canonical_status({**card, "archived_at": ""})
            if (
                get_card is not None
                and outcome != "finished"
                and str(card.get("superseded_by") or "").strip()
            ):
                chain_outcome, chain = _superseded_successor_outcome(
                    card,
                    task_id,
                    get_card,
                    canonical_status,
                    lambda tid: retired_task_statuses(repo_root, [tid]).get(tid),
                )
                if chain_outcome == "finished":
                    display, outcome = chain, "finished"
                else:
                    display = f"{display} {chain}"
            resolved.append((task_id, display, outcome))
            continue
        outcome = retired.get(task_id)
        status = f"purged:{outcome}" if outcome else "missing"
        resolved.append((task_id, status, outcome))
    return resolved


# --- Exact wave-goal successor binding ---------------------------------------
#
# A wave outcome's ``provenance.wave_goals[{id,label,task_ids}]`` names each
# goal's CURRENT tasks. A successor never takes a predecessor's place by title,
# topic, version suffix or prose: only a successor card that declares the exact
# ``{roadmap_id, goal_id, predecessor_task_id}`` binding can, and only while the
# predecessor is that goal's single current occurrence.

_SUCCESSOR_EVENT_SQL = (
    "SELECT seq FROM roadmap_events WHERE roadmap_id=? AND event=? AND "
    "CASE WHEN json_valid(detail_json) THEN "
    "json_extract(detail_json, '$.goal_id')=? AND json_extract(detail_json, '$.from')=? "
    "AND json_extract(detail_json, '$.to')=? ELSE 0 END LIMIT 1"
)


def _successor_event_detail(
    binding: Mapping[str, str], successor_task_id: str
) -> dict[str, str]:
    return {
        "goal_id": binding["goal_id"],
        "from": binding["predecessor_task_id"],
        "to": successor_task_id,
    }


def _binding_identity_refusal(
    repo_root: str | Path,
    binding: Mapping[str, Any],
    successor_task_id: str,
    *,
    successor_exists: bool,
) -> str:
    """Name the first malformed or foreign identity of one exact binding."""

    identities = (
        binding.get("roadmap_id"),
        binding.get("goal_id"),
        binding.get("predecessor_task_id"),
        successor_task_id,
    )
    patterns = (ROADMAP_ID_RE, WAVE_GOAL_ID_RE, TASK_ID_RE, TASK_ID_RE)
    if not all(
        isinstance(value, str) and pattern.fullmatch(value)
        for value, pattern in zip(identities, patterns)
    ):
        return "malformed_binding"
    if binding["predecessor_task_id"] == successor_task_id:
        return "self_succession"
    # The same repository root as this Roadmap store: a task of any other
    # repository is simply not found here.
    from . import task_store

    if task_store.get_task(repo_root, binding["predecessor_task_id"]) is None:
        return "predecessor_not_in_repository"
    if not successor_exists:
        return ""
    successor = task_store.get_task(repo_root, successor_task_id)
    if successor is None:
        return "successor_not_in_repository"
    if successor.get("wave_goal_binding") != dict(binding):
        return "successor_binding_mismatch"
    return ""


def _successor_verdict(
    conn: sqlite3.Connection,
    repo_root: str | Path,
    binding: Mapping[str, str],
    successor_task_id: str,
) -> tuple[str, str, dict[str, Any] | None, dict[str, Any] | None]:
    """Decide one exact binding as ``(state, reason, wave, goal)``; never writes.

    The exact prior event is consulted first, so a retry converges to
    ``already_applied`` even after the goal moved on or the wave closed.
    """

    try:
        wave = get_item(repo_root, binding["roadmap_id"], _connection=conn)
    except RoadmapNotFoundError:
        return "refused", "roadmap_not_found", None, None
    detail = _successor_event_detail(binding, successor_task_id)
    if conn.execute(
        _SUCCESSOR_EVENT_SQL,
        (wave["id"], WAVE_GOAL_SUCCESSOR_EVENT, detail["goal_id"], detail["from"], detail["to"]),
    ).fetchone():
        return "already_applied", "", wave, None
    if wave["status"] != "in_progress":
        return "refused", f"wave_not_active:{wave['status']}", wave, None
    provenance = wave["provenance"]
    goals = provenance.get("wave_goals") if isinstance(provenance, dict) else None
    matching = [
        goal
        for goal in (goals if isinstance(goals, list) else [])
        if isinstance(goal, dict) and goal.get("id") == binding["goal_id"]
    ]
    if len(matching) != 1:
        return "refused", "goal_ambiguous" if matching else "goal_missing", wave, None
    current = matching[0].get("task_ids")
    if not isinstance(current, list) or not all(isinstance(value, str) for value in current):
        return "refused", "goal_task_ids_malformed", wave, None
    occurrences = current.count(binding["predecessor_task_id"])
    if occurrences != 1:
        reason = "predecessor_ambiguous" if occurrences else "predecessor_not_current"
        return "refused", reason, wave, None
    if successor_task_id in current:
        return "refused", "successor_already_current", wave, None
    return "ready", "", wave, matching[0]


def goal_successor_preflight(
    repo_root: str | Path,
    *,
    roadmap_id: str,
    goal_id: str,
    predecessor_task_id: str,
    successor_task_id: str,
) -> dict[str, Any]:
    """Read-only verdict for a binding whose successor card is not written yet.

    Returns ``ready``, ``already_applied`` or ``refused`` with a typed reason.
    The Roadmap store is opened read-only and is never created by this check.
    """

    binding = {
        "roadmap_id": roadmap_id,
        "goal_id": goal_id,
        "predecessor_task_id": predecessor_task_id,
    }
    reason = _binding_identity_refusal(
        repo_root, binding, successor_task_id, successor_exists=False
    )
    if reason:
        return {"state": "refused", "reason": reason}
    path = _db_path(repo_root)
    if not path.is_file():
        return {"state": "refused", "reason": "roadmap_not_found"}
    try:
        conn = sqlite_readonly.connect_readonly(path)
    except sqlite3.Error as exc:
        return {"state": "refused", "reason": f"roadmap_unreadable:{type(exc).__name__}"}
    try:
        conn.row_factory = sqlite3.Row
        state, reason, _wave, _goal = _successor_verdict(
            conn, repo_root, binding, successor_task_id
        )
    except sqlite3.Error as exc:
        state, reason = "refused", f"roadmap_unreadable:{type(exc).__name__}"
    finally:
        conn.close()
    return {"state": state, "reason": reason}


def bind_goal_successor(
    repo_root: str | Path,
    *,
    roadmap_id: str,
    goal_id: str,
    predecessor_task_id: str,
    successor_task_id: str,
) -> dict[str, Any]:
    """Move one active wave goal's current task to its exact declared successor.

    Both tasks must be records of this repository's task store, the
    successor's canonical card must declare exactly this binding, and the
    predecessor must be the named goal's single current task. Only that one
    occurrence is replaced; the goal's other tasks, every other goal, and the
    target milestone are untouched, and the outcome-wide ``task_ids`` history
    keeps the predecessor. The goal update and one durable
    ``wave_goal_successor_bound`` event commit in one transaction. The same
    binding again is ``already_applied`` and writes nothing; every other
    verdict is ``refused`` with a typed reason and writes nothing.
    """

    binding = {
        "roadmap_id": roadmap_id,
        "goal_id": goal_id,
        "predecessor_task_id": predecessor_task_id,
    }
    receipt: dict[str, Any] = {**binding, "successor_task_id": successor_task_id}
    reason = _binding_identity_refusal(
        repo_root, binding, successor_task_id, successor_exists=True
    )
    if reason:
        return {**receipt, "state": "refused", "reason": reason}
    if not _db_path(repo_root).is_file():
        return {**receipt, "state": "refused", "reason": "roadmap_not_found"}
    conn = _connect(repo_root)
    try:
        conn.executescript(_SCHEMA_SQL)
        conn.execute("BEGIN IMMEDIATE")
        state, reason, wave, goal = _successor_verdict(
            conn, repo_root, binding, successor_task_id
        )
        if wave is None or goal is None:
            conn.rollback()
            return {**receipt, "state": state, "reason": reason, "wave": wave}
        goal["task_ids"] = [
            successor_task_id if value == predecessor_task_id else value
            for value in goal["task_ids"]
        ]
        history = list(
            dict.fromkeys([*wave["task_ids"], predecessor_task_id, successor_task_id])
        )
        conn.execute(
            "UPDATE roadmap_items SET provenance_json=?,task_ids_json=?,updated_at=? "
            "WHERE id=?",
            (json.dumps(wave["provenance"]), json.dumps(history), _utcnow(), roadmap_id),
        )
        _event(
            conn,
            roadmap_id,
            WAVE_GOAL_SUCCESSOR_EVENT,
            _successor_event_detail(binding, successor_task_id),
        )
        result = get_item(repo_root, roadmap_id, _connection=conn)
        conn.commit()
        return {**receipt, "state": "applied", "reason": "", "wave": result}
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


# --- Evidence-gated automatic wave completion --------------------------------
#
# The verdict is ``wave_roadmap.wave_completion_verdict`` (pure). This store
# only gathers each exact current task's canonical evidence and, for a
# ``completed`` verdict, performs the one guarded transition. No installed or
# released version is consulted anywhere on this path.

WAVE_COMPLETION_EVIDENCE_SCHEMA = "aiworkhub.wave_completion_evidence.v1"
_ACTIVE_WAVE_WHERE = (
    "WHERE status='in_progress' AND "
    "CASE WHEN json_valid(provenance_json) THEN "
    "json_type(provenance_json, '$.wave_goals') IS NOT NULL ELSE 0 END "
    "ORDER BY id LIMIT ?"
)
_ACTIVE_WAVE_SQL = f"SELECT id FROM roadmap_items {_ACTIVE_WAVE_WHERE}"
_ACTIVE_WAVE_ROWS_SQL = f"SELECT * FROM roadmap_items {_ACTIVE_WAVE_WHERE}"
# open_wave's rival and retry checks cover every live wave: a blocked wave is
# still its milestone's wave (it stays supersedable, see _WAVE_SUPERSEDABLE).
_LIVE_WAVE_ROWS_SQL = _ACTIVE_WAVE_ROWS_SQL.replace(
    "status='in_progress'", "status IN ('in_progress','blocked')"
)


def active_wave_ids(repo_root: str | Path, *, limit: int = 8) -> dict[str, Any]:
    """Bounded ids of in-progress outcomes declaring wave goals; read-only.

    A repository without a Roadmap store has no waves, and this read never
    creates one.
    """
    path = _db_path(repo_root)
    if not path.is_file():
        return {"wave_ids": [], "truncated": False}
    bound = max(1, min(int(limit), MAX_LIST_LIMIT))
    conn = sqlite_readonly.connect_readonly(path)
    try:
        rows = conn.execute(_ACTIVE_WAVE_SQL, (bound + 1,)).fetchall()
    finally:
        conn.close()
    ids = [str(row[0]) for row in rows]
    return {"wave_ids": ids[:bound], "truncated": len(ids) > bound}


def _unknown_completion(wave_id: object, reason: str) -> dict[str, Any]:
    return {
        "wave_id": str(wave_id or "")[:32],
        "criteria": 0,
        "goals": [],
        "task_ids": [],
        "state": "unknown",
        "reason": reason,
    }


def _task_evidence(
    repo_root: str | Path, task_ids: Sequence[str]
) -> tuple[dict[str, str], dict[str, dict[str, str]], str]:
    """Each exact task's completion evidence and verifier receipt; never writes.

    Returns ``(evidence, receipts, failure)``; a non-empty ``failure`` names
    the unreadable task store, which is never evidence of acceptance.
    """

    from . import task_store, wave_roadmap

    evidence: dict[str, str] = {}
    receipts: dict[str, dict[str, str]] = {}
    for task_id in dict.fromkeys(task_ids):
        try:
            card = task_store.get_task(repo_root, task_id)
        except Exception as exc:  # noqa: BLE001 -- an unreadable store is never acceptance
            return {}, {}, f"task_store_unavailable:{type(exc).__name__}"[:80]
        status = task_store.canonical_status(card) if isinstance(card, Mapping) else None
        evidence[task_id] = wave_roadmap.task_evidence(task_id, card, status)
        receipt = wave_roadmap.accepted_receipt(task_id, card)
        if evidence[task_id] == wave_roadmap.TASK_ACCEPTED and receipt is not None:
            receipts[task_id] = receipt
    return evidence, receipts, ""


def reconcile_wave_completion(repo_root: str | Path, wave_id: str) -> dict[str, Any]:
    """Complete one wave only from mapped, canonically accepted exact evidence.

    Returns the verdict -- ``completed``, ``pending_evidence`` or ``unknown``
    with a typed reason. Only a ``completed`` verdict on an in-progress wave
    writes, through one ``transition_item`` guarded by the revision the verdict
    was decided from, so a goal rebound or a criterion edited meanwhile refuses
    the move. A repeated or concurrent call finds the wave already completed
    and writes no second event.
    """

    from . import wave_roadmap

    if not ROADMAP_ID_RE.fullmatch(str(wave_id or "")):
        return _unknown_completion(wave_id, "malformed_roadmap_id")
    if not _db_path(repo_root).is_file():
        return _unknown_completion(wave_id, "roadmap_not_found")
    try:
        wave = get_item(repo_root, wave_id)
    except RoadmapNotFoundError:
        return _unknown_completion(wave_id, "roadmap_not_found")
    verdict, receipts = _completion_verdict(repo_root, wave)
    if (
        verdict["state"] != wave_roadmap.COMPLETION_COMPLETED
        or wave["status"] != "in_progress"
    ):
        return verdict
    record = {
        "schema_id": WAVE_COMPLETION_EVIDENCE_SCHEMA,
        "criteria": verdict["criteria"],
        "goals": [
            {
                "id": goal["id"],
                "acceptance_indices": goal["acceptance_indices"],
                "accepted": {
                    task["task_id"]: receipts[task["task_id"]] for task in goal["tasks"]
                },
            }
            for goal in verdict["goals"]
        ],
    }
    reason = (
        f"wave evidence complete: {verdict['criteria']} acceptance criteria mapped to "
        f"{len(verdict['goals'])} goals; {len(receipts)} exact tasks accepted "
        "with verifier receipts"
    )
    try:
        completed = transition_item(
            repo_root,
            wave_id,
            "completed",
            reason=reason,
            expected_revision=item_revision(wave),
            evidence=record,
        )
    except RoadmapConflictError as exc:
        return {**verdict, "state": "unknown", "reason": f"transition_refused:{exc}"[:200]}
    return {**verdict, "wave": completed}


# --- Wave authoring (NF-2026-00987) -------------------------------------------
#
# Manager-only writes that open a wave, edit its goals, attach a goal's first
# tasks and roll a wave over to the next milestone. Validation is pure
# (``wave_roadmap``); each write here is one short BEGIN IMMEDIATE transaction
# that commits the row change with its durable event. Every call returns a
# receipt ``{state: applied | already_applied | refused, reason, wave,
# item_revision}`` and a retry with the same arguments converges on
# ``already_applied`` without a second row or event. Nothing here can set
# ``completed``: ``reconcile_wave_completion`` stays the only path to it.

WAVE_STATUS_SCHEMA = "aiworkhub.wave_status.v1"
WAVE_OPENED_EVENT = "wave_opened"
WAVE_SUPERSEDED_EVENT = "superseded_by"
WAVE_GOAL_TASK_ATTACHED_EVENT = "wave_goal_task_attached"
WAVE_GOAL_UPSERTED_EVENT = "wave_goal_upserted"
WAVE_GOAL_REMOVED_EVENT = "wave_goal_removed"
_WAVE_SUPERSEDABLE = ("in_progress", "blocked")
_WAVE_STATUS_LIMIT = 32
_MAX_REASON_BYTES = 4000
_ITEM_BY_ID_SQL = "SELECT * FROM roadmap_items WHERE id=?"
_WAVE_OPEN_DIGEST_SQL = (
    "SELECT * FROM roadmap_items WHERE status IN ('in_progress','blocked') AND "
    "CASE WHEN json_valid(provenance_json) THEN "
    "json_extract(provenance_json, '$.wave_open.digest')=? ELSE 0 END LIMIT 1"
)


def _wave_receipt(
    state: str, reason: str, wave: dict[str, Any] | None = None, **extra: Any
) -> dict[str, Any]:
    return {
        **extra,
        "state": state,
        "reason": reason,
        "wave": wave,
        "item_revision": item_revision(wave) if wave is not None else None,
    }


def _refused(reason: str, **extra: Any) -> dict[str, Any]:
    return _wave_receipt("refused", reason, None, **extra)


def _event_recorded(
    conn: sqlite3.Connection, roadmap_id: str, event: str, fields: Mapping[str, str]
) -> bool:
    # ``fields`` keys are this module's own detail keys, never caller text.
    clauses = " AND ".join(f"json_extract(detail_json, '$.{key}')=?" for key in fields)
    row = conn.execute(
        "SELECT seq FROM roadmap_events WHERE roadmap_id=? AND event=? AND "
        f"CASE WHEN json_valid(detail_json) THEN {clauses} ELSE 0 END LIMIT 1",
        (roadmap_id, event, *fields.values()),
    ).fetchone()
    return row is not None


def _write_goals(
    conn: sqlite3.Connection,
    wave: Mapping[str, Any],
    goals: Sequence[Mapping[str, Any]],
    history: Sequence[str] | None = None,
) -> None:
    provenance = dict(wave["provenance"])
    provenance["wave_goals"] = [dict(goal) for goal in goals]
    conn.execute(
        "UPDATE roadmap_items SET provenance_json=?,task_ids_json=?,updated_at=? WHERE id=?",
        (
            json.dumps(provenance),
            json.dumps(list(wave["task_ids"] if history is None else history)),
            _utcnow(),
            wave["id"],
        ),
    )


def _read_only_items(
    repo_root: str | Path, sql: str, params: Sequence[Any]
) -> list[dict[str, Any]] | None:
    """Rows of one read on a read-only connection; ``None`` when no store exists."""
    path = _db_path(repo_root)
    if not path.is_file():
        return None
    conn = sqlite_readonly.connect_readonly(path)
    try:
        conn.row_factory = sqlite3.Row
        return [_row(row) for row in conn.execute(sql, tuple(params)).fetchall()]
    finally:
        conn.close()


def _completion_verdict(
    repo_root: str | Path, wave: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, dict[str, str]]]:
    """The pure completion verdict for one wave row plus its receipts; never writes."""
    from . import wave_roadmap

    goals, _reason = wave_roadmap.completion_goals(wave)
    task_ids = [task_id for goal in goals for task_id in goal["task_ids"]]
    evidence, receipts, failure = _task_evidence(
        repo_root, task_ids if wave.get("status") == "in_progress" else ()
    )
    if failure:
        return _unknown_completion(wave.get("id"), failure), {}
    return wave_roadmap.wave_completion_verdict(wave, evidence), receipts


def _predecessor_refusal(wave: Mapping[str, Any]) -> str:
    if wave["status"] not in _WAVE_SUPERSEDABLE:
        return f"predecessor_not_active:{wave['status']}"
    provenance = wave["provenance"]
    if not isinstance(provenance, dict) or "wave_goals" not in provenance:
        return "predecessor_not_wave"
    return ""


def open_wave(
    repo_root: str | Path,
    *,
    milestone: str,
    title: str,
    goals: Sequence[Mapping[str, Any]] | None = None,
    acceptance: Sequence[str] | None = None,
    supersedes: str = "",
    outcome: str = "",
    priority: str = "medium",
) -> dict[str, Any]:
    """Open one ``in_progress`` wave targeting ``milestone`` with its goals.

    Refuses a second active wave for the same milestone. With ``supersedes``
    the predecessor's goals that are not yet proven by accepted tasks move into
    the new wave with their current ``task_ids``, and the predecessor is
    archived (its rows and events kept) with a ``superseded_by`` event, all in
    the transaction that creates the new wave. The same arguments again return
    the wave they opened (``already_applied``).
    """

    from . import wave_roadmap

    milestone = str(milestone or "").strip()
    title = str(title or "").strip()
    supersedes = str(supersedes or "").strip()
    outcome = str(outcome or "").strip() or f"Wave {milestone}: {title}"
    base: dict[str, Any] = {"milestone": milestone, "supersedes": supersedes or None}
    reason = wave_roadmap.milestone_refusal(milestone)
    if not reason and (not title or len(title.encode()) > 1000):
        reason = "title_invalid"
    if not reason and len(outcome.encode()) > 100_000:
        reason = "outcome_invalid"
    if not reason and priority not in PRIORITIES:
        reason = "priority_invalid"
    if not reason and supersedes and not ROADMAP_ID_RE.fullmatch(supersedes):
        reason = "malformed_supersedes"
    if reason:
        return _refused(reason, **base)
    try:
        criteria = _bounded_strings("acceptance", acceptance, limit=100)
    except RoadmapValidationError:
        return _refused("acceptance_invalid", **base)
    if not criteria:
        return _refused("acceptance_missing", **base)
    new_goals, reason = wave_roadmap.goal_definitions(goals, len(criteria))
    if reason:
        return _refused(reason, **base)
    digest = hashlib.sha256(
        json.dumps(
            {
                "milestone": milestone,
                "title": title,
                "outcome": outcome,
                "priority": priority,
                "acceptance": criteria,
                "goals": new_goals,
                "supersedes": supersedes,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    conn = _connect(repo_root)
    try:
        conn.executescript(_SCHEMA_SQL)
        existing = conn.execute(_WAVE_OPEN_DIGEST_SQL, (digest,)).fetchone()
        if existing is not None:
            return _wave_receipt("already_applied", "", _row(existing), **base)
        # Task evidence is read before the write lock is taken; the predecessor's
        # revision is re-checked under it, so a goal edited meanwhile refuses.
        predecessor: dict[str, Any] | None = None
        carried: list[dict[str, Any]] = []
        if supersedes:
            row = conn.execute(_ITEM_BY_ID_SQL, (supersedes,)).fetchone()
            if row is None:
                return _refused("predecessor_not_found", **base)
            predecessor = _row(row)
            reason = _predecessor_refusal(predecessor)
            if reason:
                return _refused(reason, **base)
            evidence, _receipts, failure = _task_evidence(
                repo_root, wave_roadmap.goal_task_ids(predecessor)
            )
            if failure:
                return _refused(failure, **base)
            carried, reason = wave_roadmap.carried_goals(predecessor, evidence, criteria)
            if reason:
                return _refused(reason, **base)
        wave_goals, reason = wave_roadmap.composed_goals(carried, new_goals)
        if reason:
            return _refused(reason, **base)
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(_WAVE_OPEN_DIGEST_SQL, (digest,)).fetchone()
        if existing is not None:
            conn.rollback()
            return _wave_receipt("already_applied", "", _row(existing), **base)
        if predecessor is not None:
            current = get_item(repo_root, supersedes, _connection=conn)
            if item_revision(current) != item_revision(predecessor):
                conn.rollback()
                return _refused("predecessor_revision_changed", **base)
        active = conn.execute(_LIVE_WAVE_ROWS_SQL, (MAX_LIST_LIMIT + 1,)).fetchall()
        if len(active) > MAX_LIST_LIMIT:
            conn.rollback()
            return _refused("active_waves_truncated", **base)
        rivals = [
            str(row["id"])
            for row in active
            if row["id"] != supersedes and wave_roadmap.same_milestone(row["milestone"], milestone)
        ]
        if rivals:
            conn.rollback()
            return _refused(f"active_wave_exists:{rivals[0]}", **base)
        wave_id = _next_id(conn)
        now = _utcnow()
        history = list(dict.fromkeys(t for goal in wave_goals for t in goal["task_ids"]))
        provenance = {
            "origin": "wave_open",
            "verified": True,
            "wave_goals": wave_goals,
            "wave_open": {"digest": digest, "supersedes": supersedes or None},
        }
        conn.execute(
            "INSERT INTO roadmap_items "
            "(id,title,outcome,status,priority,milestone,acceptance_json,"
            "needfix_ids_json,task_ids_json,depends_on_json,provenance_json,"
            "evidence_refs_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                wave_id, title, outcome, "in_progress", priority, milestone,
                json.dumps(criteria), "[]", json.dumps(history), "[]",
                json.dumps(provenance), "[]", now, now,
            ),
        )
        carried_ids = [goal["id"] for goal in carried]
        _event(conn, wave_id, "created", {"status": "in_progress", "origin": "wave_open"})
        _event(
            conn,
            wave_id,
            WAVE_OPENED_EVENT,
            {
                "milestone": milestone,
                "goal_ids": [goal["id"] for goal in wave_goals],
                "carried_goal_ids": carried_ids,
                "supersedes": supersedes or None,
            },
        )
        superseded: dict[str, Any] | None = None
        if predecessor is not None:
            cursor = conn.execute(
                "UPDATE roadmap_items SET status='archived',archived_at=?,updated_at=? "
                "WHERE id=? AND status=?",
                (now, now, supersedes, predecessor["status"]),
            )
            if cursor.rowcount != 1:
                raise RoadmapConflictError("wave supersession lost atomic status race")
            _event(
                conn,
                supersedes,
                "transitioned",
                {
                    "from": predecessor["status"],
                    "to": "archived",
                    "reason": f"superseded by wave {wave_id} targeting {milestone}",
                },
            )
            left = [
                goal["id"]
                for goal in wave_roadmap.declared_goals(predecessor)[0]
                if goal["id"] not in carried_ids
            ]
            _event(
                conn,
                supersedes,
                WAVE_SUPERSEDED_EVENT,
                {
                    "superseded_by": wave_id,
                    "milestone": milestone,
                    "carried_goal_ids": carried_ids,
                    "accepted_goal_ids": left,
                },
            )
            superseded = get_item(repo_root, supersedes, _connection=conn)
        result = get_item(repo_root, wave_id, _connection=conn)
        conn.commit()
        return _wave_receipt(
            "applied", "", result, **base, superseded=superseded, carried_goal_ids=carried_ids
        )
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def _converted_task_id(repo_root: str | Path, needfix_id: str) -> tuple[str, str]:
    """A NeedFix's ``converted_task_id``, or ``("", refusal)``."""
    if not NEEDFIX_ID_RE.fullmatch(needfix_id):
        return "", "malformed_needfix_id"
    from . import needfix_store

    try:
        item = needfix_store.get_needfix(repo_root, needfix_id)
    except needfix_store.NeedFixNotFoundError:
        return "", "needfix_not_found"
    except Exception as exc:  # noqa: BLE001 -- an unreadable store resolves nothing
        return "", f"needfix_store_unavailable:{type(exc).__name__}"[:80]
    converted = str(item.get("converted_task_id") or "").strip()
    return (converted, "") if converted else ("", "needfix_not_converted")


def attach_goal_task(
    repo_root: str | Path,
    *,
    roadmap_id: str,
    goal_id: str,
    task_id: str = "",
    needfix_id: str = "",
) -> dict[str, Any]:
    """Attach one existing task to an active wave goal, with no predecessor.

    ``needfix_id`` resolves to that NeedFix's ``converted_task_id``. The goal's
    ``task_ids`` and the outcome's ``task_ids`` history gain the task, with one
    ``wave_goal_task_attached`` event, in one transaction. The same attach
    again is ``already_applied``; a task already in any goal of the wave, a
    full goal (``MAX_GOAL_TASKS``) or an inactive wave is refused.
    """

    from . import task_store, wave_roadmap

    roadmap_id = str(roadmap_id or "").strip()
    goal_id = str(goal_id or "").strip()
    task_id = str(task_id or "").strip()
    needfix_id = str(needfix_id or "").strip()
    base: dict[str, Any] = {
        "roadmap_id": roadmap_id,
        "goal_id": goal_id,
        "task_id": task_id or None,
        "needfix_id": needfix_id or None,
    }
    if not ROADMAP_ID_RE.fullmatch(roadmap_id):
        return _refused("malformed_roadmap_id", **base)
    reason = wave_roadmap.goal_id_refusal(goal_id)
    if reason:
        return _refused(reason, **base)
    if bool(task_id) == bool(needfix_id):
        return _refused(
            "task_and_needfix_exclusive" if task_id else "task_or_needfix_required", **base
        )
    if needfix_id:
        task_id, reason = _converted_task_id(repo_root, needfix_id)
        if reason:
            return _refused(reason, **base)
        base["task_id"] = task_id
    if not TASK_ID_RE.fullmatch(task_id):
        return _refused("malformed_task_id", **base)
    try:
        card = task_store.get_task(repo_root, task_id)
    except Exception as exc:  # noqa: BLE001 -- an unreadable store proves no task
        return _refused(f"task_store_unavailable:{type(exc).__name__}"[:80], **base)
    if card is None:
        return _refused("task_not_found", **base)
    if not _db_path(repo_root).is_file():
        return _refused("roadmap_not_found", **base)
    conn = _connect(repo_root)
    try:
        conn.executescript(_SCHEMA_SQL)
        conn.execute("BEGIN IMMEDIATE")
        try:
            wave = get_item(repo_root, roadmap_id, _connection=conn)
        except RoadmapNotFoundError:
            conn.rollback()
            return _refused("roadmap_not_found", **base)
        goals, reason = wave_roadmap.declared_goals(wave)
        target = next((goal for goal in goals if goal["id"] == goal_id), None)
        detail = {"goal_id": goal_id, "task_id": task_id}
        if (
            target is not None
            and task_id in target["task_ids"]
            and _event_recorded(conn, roadmap_id, WAVE_GOAL_TASK_ATTACHED_EVENT, detail)
        ):
            conn.rollback()
            return _wave_receipt("already_applied", "", wave, **base)
        if wave["status"] != "in_progress":
            reason = f"wave_not_active:{wave['status']}"
        elif not reason and target is None:
            reason = "goal_missing"
        if not reason:
            holder = next((goal["id"] for goal in goals if task_id in goal["task_ids"]), None)
            if holder is not None:
                reason = f"task_already_attached:{holder}"
            elif target is not None and len(target["task_ids"]) >= wave_roadmap.MAX_GOAL_TASKS:
                reason = f"goal_task_limit:{wave_roadmap.MAX_GOAL_TASKS}"
        if reason or target is None:
            conn.rollback()
            return _refused(reason or "goal_missing", **base)
        target["task_ids"] = [*target["task_ids"], task_id]
        _write_goals(conn, wave, goals, list(dict.fromkeys([*wave["task_ids"], task_id])))
        _event(
            conn,
            roadmap_id,
            WAVE_GOAL_TASK_ATTACHED_EVENT,
            {**detail, **({"needfix_id": needfix_id} if needfix_id else {})},
        )
        result = get_item(repo_root, roadmap_id, _connection=conn)
        conn.commit()
        return _wave_receipt("applied", "", result, **base)
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def upsert_goal(
    repo_root: str | Path,
    *,
    roadmap_id: str,
    goal_id: str,
    label: str,
    expected_revision: str,
    acceptance_indices: Sequence[int] | None = None,
) -> dict[str, Any]:
    """Create or relabel one goal of an active wave, guarded by ``item_revision``.

    ``acceptance_indices`` (1-based criteria) replaces the goal's mapping;
    ``None`` keeps it. A goal that already reads exactly so is
    ``already_applied`` whatever the revision; any other edit decided from a
    stale revision is refused.
    """

    from . import wave_roadmap

    roadmap_id = str(roadmap_id or "").strip()
    goal_id = str(goal_id or "").strip()
    expected = str(expected_revision or "").strip()
    base: dict[str, Any] = {"roadmap_id": roadmap_id, "goal_id": goal_id}
    if not ROADMAP_ID_RE.fullmatch(roadmap_id):
        return _refused("malformed_roadmap_id", **base)
    if not expected:
        return _refused("expected_revision_required", **base)
    if not _db_path(repo_root).is_file():
        return _refused("roadmap_not_found", **base)
    conn = _connect(repo_root)
    try:
        conn.executescript(_SCHEMA_SQL)
        conn.execute("BEGIN IMMEDIATE")
        try:
            wave = get_item(repo_root, roadmap_id, _connection=conn)
        except RoadmapNotFoundError:
            conn.rollback()
            return _refused("roadmap_not_found", **base)
        goals, outcome, reason = wave_roadmap.upserted_goals(
            wave, goal_id, label, acceptance_indices
        )
        if not reason and outcome == "unchanged":
            conn.rollback()
            return _wave_receipt("already_applied", "", wave, **base, outcome=outcome)
        if not reason and wave["status"] != "in_progress":
            reason = f"wave_not_active:{wave['status']}"
        current_revision = item_revision(wave)
        if not reason and current_revision != expected:
            reason = "stale_revision"
        if reason:
            conn.rollback()
            return _refused(reason, **base, current_revision=current_revision)
        _write_goals(conn, wave, goals)
        written = next(goal for goal in goals if goal["id"] == goal_id)
        _event(
            conn,
            roadmap_id,
            WAVE_GOAL_UPSERTED_EVENT,
            {
                "goal_id": goal_id,
                "outcome": outcome,
                "label": written["label"],
                "acceptance_indices": written.get("acceptance_indices"),
            },
        )
        result = get_item(repo_root, roadmap_id, _connection=conn)
        conn.commit()
        return _wave_receipt("applied", "", result, **base, outcome=outcome)
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def remove_goal(
    repo_root: str | Path,
    *,
    roadmap_id: str,
    goal_id: str,
    expected_revision: str,
    force: bool = False,
    reason: str = "",
) -> dict[str, Any]:
    """Remove one goal of an active wave, guarded by ``item_revision``.

    A goal still holding any task that is not canonically accepted is removed
    only with ``force=True`` and a non-empty ``reason``; both are recorded in
    the ``wave_goal_removed`` event. The outcome's ``task_ids`` history keeps
    the goal's tasks. Removing an already-removed goal is ``already_applied``.
    """

    from . import wave_roadmap

    roadmap_id = str(roadmap_id or "").strip()
    goal_id = str(goal_id or "").strip()
    expected = str(expected_revision or "").strip()
    note = str(reason or "").strip()
    base: dict[str, Any] = {"roadmap_id": roadmap_id, "goal_id": goal_id}
    if not ROADMAP_ID_RE.fullmatch(roadmap_id):
        return _refused("malformed_roadmap_id", **base)
    refusal = wave_roadmap.goal_id_refusal(goal_id)
    if refusal:
        return _refused(refusal, **base)
    if not expected:
        return _refused("expected_revision_required", **base)
    if len(note.encode()) > _MAX_REASON_BYTES:
        return _refused("reason_too_long", **base)
    if not _db_path(repo_root).is_file():
        return _refused("roadmap_not_found", **base)
    conn = _connect(repo_root)
    try:
        conn.executescript(_SCHEMA_SQL)
        try:
            wave = get_item(repo_root, roadmap_id, _connection=conn)
        except RoadmapNotFoundError:
            return _refused("roadmap_not_found", **base)
        remaining, removed, refusal = wave_roadmap.removed_goals(wave, goal_id)
        if refusal == "goal_missing" and _event_recorded(
            conn, roadmap_id, WAVE_GOAL_REMOVED_EVENT, {"goal_id": goal_id}
        ):
            return _wave_receipt("already_applied", "", wave, **base)
        if not refusal and wave["status"] != "in_progress":
            refusal = f"wave_not_active:{wave['status']}"
        current_revision = item_revision(wave)
        if not refusal and current_revision != expected:
            refusal = "stale_revision"
        if refusal or removed is None:
            return _refused(
                refusal or "goal_missing", **base, current_revision=current_revision
            )
        evidence, _receipts, failure = _task_evidence(repo_root, removed["task_ids"])
        if failure:
            return _refused(failure, **base)
        unaccepted = wave_roadmap.unaccepted_task_ids(removed["task_ids"], evidence)
        if unaccepted and not force:
            return _refused(
                "goal_has_unaccepted_tasks", **base, unaccepted_task_ids=unaccepted
            )
        if unaccepted and not note:
            return _refused("force_reason_required", **base, unaccepted_task_ids=unaccepted)
        conn.execute("BEGIN IMMEDIATE")
        current = get_item(repo_root, roadmap_id, _connection=conn)
        if item_revision(current) != expected:
            conn.rollback()
            return _refused(
                "stale_revision", **base, current_revision=item_revision(current)
            )
        _write_goals(conn, current, remaining)
        _event(
            conn,
            roadmap_id,
            WAVE_GOAL_REMOVED_EVENT,
            {
                "goal_id": goal_id,
                "label": removed["label"],
                "task_ids": removed["task_ids"],
                "unaccepted_task_ids": unaccepted,
                "forced": bool(unaccepted),
                "reason": note,
            },
        )
        result = get_item(repo_root, roadmap_id, _connection=conn)
        conn.commit()
        return _wave_receipt("applied", "", result, **base, unaccepted_task_ids=unaccepted)
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def _wave_summary(wave: Mapping[str, Any]) -> dict[str, Any]:
    from . import wave_roadmap

    goals, reason = wave_roadmap.declared_goals(wave)
    keep = ("id", "label", "task_ids", "acceptance_indices", "carried_from")
    acceptance = wave.get("acceptance")
    return {
        "id": str(wave.get("id") or "")[:32],
        "title": str(wave.get("title") or "")[:200],
        "milestone": str(wave.get("milestone") or "")[:64],
        "status": wave.get("status"),
        "item_revision": item_revision(wave),
        "criteria": len(acceptance) if isinstance(acceptance, list) else 0,
        "goals_reason": reason,
        "goals": [{key: goal[key] for key in keep if key in goal} for goal in goals],
    }


def wave_status(
    repo_root: str | Path, *, installed_version: str, wave_id: str = ""
) -> dict[str, Any]:
    """Read-only: the current-wave projection plus one wave's completion verdict.

    ``wave_id`` names the wave whose verdict is reported; empty means the
    projected current wave. Nothing is written and no store is created; the
    verdict is the one ``reconcile_wave_completion`` would act on. Output is
    bounded: at most ``_WAVE_STATUS_LIMIT`` active waves are read (more makes
    the projection ``truncated_roadmap``), each with at most ``MAX_GOALS``
    goals of ``MAX_GOAL_TASKS`` tasks.
    """

    from . import task_store, wave_roadmap

    wave_id = str(wave_id or "").strip()
    base: dict[str, Any] = {"schema_id": WAVE_STATUS_SCHEMA, "wave_id": wave_id or None}
    if wave_id and not ROADMAP_ID_RE.fullmatch(wave_id):
        return {**base, "state": "refused", "reason": "malformed_roadmap_id"}
    try:
        rows = _read_only_items(repo_root, _ACTIVE_WAVE_ROWS_SQL, (_WAVE_STATUS_LIMIT + 1,))
        named = _read_only_items(repo_root, _ITEM_BY_ID_SQL, (wave_id,)) if wave_id else []
    except sqlite3.Error as exc:
        return {**base, "state": "refused", "reason": f"roadmap_unreadable:{type(exc).__name__}"}
    if wave_id and not named:
        return {**base, "state": "refused", "reason": "roadmap_not_found"}
    active = rows or []
    truncated = len(active) > _WAVE_STATUS_LIMIT
    active = active[:_WAVE_STATUS_LIMIT]
    subjects = [*active, *(named or [])]
    task_ids = list(
        dict.fromkeys(t for row in subjects for t in wave_roadmap.goal_task_ids(row))
    )
    try:
        cards = {task_id: task_store.get_task(repo_root, task_id) for task_id in task_ids}
        outcomes = linked_task_outcomes(
            repo_root,
            task_ids,
            cards,
            task_store.canonical_status,
            get_card=lambda tid: task_store.get_task(repo_root, tid),
        )
    except Exception as exc:  # noqa: BLE001 -- an unreadable store is never progress
        return {
            **base,
            "state": "refused",
            "reason": f"task_store_unavailable:{type(exc).__name__}"[:80],
        }
    display = {task_id: status for task_id, status, _outcome in outcomes}
    joined = [
        {
            **row,
            "tasks": [
                {"task_id": t, "status": display.get(t, "missing")}
                for t in wave_roadmap.goal_task_ids(row)
            ],
        }
        for row in active
    ]
    projection = wave_roadmap.project_current_wave(
        joined, installed_version, truncated=truncated
    )
    target = (
        named[0]
        if named
        else next((row for row in active if row["id"] == projection.get("wave_id")), None)
    )
    completion = (
        _completion_verdict(repo_root, target)[0]
        if target is not None
        else _unknown_completion("", f"no_current_wave:{projection['selection_reason']}")
    )
    return {
        **base,
        "state": "ok",
        "reason": "",
        "current_wave": projection,
        "wave": _wave_summary(target) if target is not None else None,
        "completion": completion,
    }


def rollover_wave(
    repo_root: str | Path,
    *,
    next_milestone: str,
    title: str = "",
    goals: Sequence[Mapping[str, Any]] | None = None,
    acceptance: Sequence[str] | None = None,
    outcome: str = "",
) -> dict[str, Any]:
    """``open_wave(milestone=next_milestone, supersedes=<current active wave>)``.

    The current wave is the one ``wave_roadmap.select_current_wave`` picks.
    ``acceptance`` defaults to the current wave's criteria, so carried goals
    keep their criterion mapping; ``title`` defaults to ``Wave <milestone>``.
    When the current wave already targets ``next_milestone`` the call is
    ``already_applied``; an earlier milestone is refused.
    """

    from . import wave_roadmap

    next_milestone = str(next_milestone or "").strip()
    base: dict[str, Any] = {"milestone": next_milestone, "supersedes": None}
    reason = wave_roadmap.milestone_refusal(next_milestone)
    if reason:
        return _refused(reason, **base)
    try:
        rows = _read_only_items(repo_root, _ACTIVE_WAVE_ROWS_SQL, (MAX_LIST_LIMIT + 1,))
    except sqlite3.Error as exc:
        return _refused(f"roadmap_unreadable:{type(exc).__name__}", **base)
    active = rows or []
    if len(active) > MAX_LIST_LIMIT:
        return _refused("active_waves_truncated", **base)
    current, why = wave_roadmap.select_current_wave(active)
    if current is None:
        return _refused(f"no_current_wave:{why}", **base)
    current_wave = dict(current)
    if wave_roadmap.same_milestone(current_wave["milestone"], next_milestone):
        return _wave_receipt(
            "already_applied", "current_wave_targets_milestone", current_wave, **base
        )
    if not wave_roadmap.milestone_after(next_milestone, current_wave["milestone"]):
        return _refused(
            "milestone_not_after_current", **base, current_wave_id=current_wave["id"]
        )
    return open_wave(
        repo_root,
        milestone=next_milestone,
        title=str(title or "").strip() or f"Wave {next_milestone}",
        goals=goals,
        acceptance=current_wave["acceptance"] if acceptance is None else acceptance,
        supersedes=current_wave["id"],
        outcome=outcome,
        priority=str(current_wave.get("priority") or "medium"),
    )


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
