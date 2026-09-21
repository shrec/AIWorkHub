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
from typing import Any, Mapping, Sequence

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
_ACTIVE_WAVE_SQL = (
    "SELECT id FROM roadmap_items WHERE status='in_progress' AND "
    "CASE WHEN json_valid(provenance_json) THEN "
    "json_type(provenance_json, '$.wave_goals') IS NOT NULL ELSE 0 END "
    "ORDER BY id LIMIT ?"
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


def reconcile_wave_completion(repo_root: str | Path, wave_id: str) -> dict[str, Any]:
    """Complete one wave only from mapped, canonically accepted exact evidence.

    Returns the verdict -- ``completed``, ``pending_evidence`` or ``unknown``
    with a typed reason. Only a ``completed`` verdict on an in-progress wave
    writes, through one ``transition_item`` guarded by the revision the verdict
    was decided from, so a goal rebound or a criterion edited meanwhile refuses
    the move. A repeated or concurrent call finds the wave already completed
    and writes no second event.
    """

    from . import task_store, wave_roadmap

    if not ROADMAP_ID_RE.fullmatch(str(wave_id or "")):
        return _unknown_completion(wave_id, "malformed_roadmap_id")
    if not _db_path(repo_root).is_file():
        return _unknown_completion(wave_id, "roadmap_not_found")
    try:
        wave = get_item(repo_root, wave_id)
    except RoadmapNotFoundError:
        return _unknown_completion(wave_id, "roadmap_not_found")
    goals, _reason = wave_roadmap.completion_goals(wave)
    evidence: dict[str, str] = {}
    receipts: dict[str, dict[str, str]] = {}
    task_ids = dict.fromkeys(task_id for goal in goals for task_id in goal["task_ids"])
    for task_id in task_ids if wave["status"] == "in_progress" else ():
        try:
            card = task_store.get_task(repo_root, task_id)
        except Exception as exc:  # noqa: BLE001 -- an unreadable store is never acceptance
            return _unknown_completion(
                wave_id, f"task_store_unavailable:{type(exc).__name__}"[:80]
            )
        status = task_store.canonical_status(card) if isinstance(card, Mapping) else None
        evidence[task_id] = wave_roadmap.task_evidence(task_id, card, status)
        receipt = wave_roadmap.accepted_receipt(task_id, card)
        if evidence[task_id] == wave_roadmap.TASK_ACCEPTED and receipt is not None:
            receipts[task_id] = receipt
    verdict = wave_roadmap.wave_completion_verdict(wave, evidence)
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
