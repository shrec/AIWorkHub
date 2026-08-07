"""Durable, repo-scoped NeedFix intake registry.

This module is additive and self-contained: it never mutates or weakens
existing task lifecycle, blocked-rework, predecessor-selection, validation
or manager gate behavior in ``task_store.py`` / ``core.py`` / ``server.py``.
NeedFix is deliberately a *separate* state space from canonical task state.

Storage: one bounded SQLite database per repository at
``<repo_root>/.aiworkhub/tasking/needfix.sqlite`` (mirrors the canonical
task store's repo-local isolation pattern). A worker may only ever place a
NeedFix into ``captured`` (unverified proposal) state; only an explicit
manager-side call may ``add`` with elevated authority fields or invoke
``convert``. The two authority paths are functionally distinct:

* ``capture_proposal`` -- worker/unverified entry point. Always lands in
  status ``captured`` regardless of caller-supplied status.
* ``add_needfix`` -- manager mutation entry point. May set richer initial
  fields but still defaults to ``captured`` unless explicitly elevated by
  a caller that has independently established manager authority.

Full lifecycle:
  captured -> triaged -> accepted|duplicate|rejected|deferred
  accepted -> task_planned -> task_created -> resolved
  [archived] available at any non-transient state; restore back to
  captured/triaged/accepted via explicit manager action.

Conversion (``convert_needfix``) is explicit only: nothing here ever
implicitly creates or launches a task. It first atomically claims exactly
one NeedFix row (``accepted`` or ``task_planned`` -> ``converting``) using a
conditional ``UPDATE ... WHERE status = ?`` so concurrent callers cannot
both win the claim (fail-closed on 0 rows updated). ``captured`` and
``triaged`` are *not* claimable -- the captured/unverified guard ensures
only manager-affirmed NeedFixes can convert. It then invokes the
caller-supplied ``create_task_fn`` (bound to the existing authoritative
task store's ``create_task``) to create one schema-valid canonical task.
On success the NeedFix transitions to ``task_created`` with the resulting
``converted_task_id`` recorded, atomically and durably. On failure the
claim is compensated back to its pre-claim status so the NeedFix is never
stranded as ``task_created`` without a real task. A retried conversion for
a NeedFix already in ``task_created`` is idempotent: it does not attempt a
second task creation and simply returns the previously recorded
``converted_task_id`` (lost-ack-safe reconciliation).

Duplicate-parent cycle guard: ``mark_duplicate`` validates that the
proposed ``duplicate_parent_id`` does not itself point (directly or
transitively) to the current NeedFix, preventing infinite cycles.

IDs are stable, repo-local NF-YYYY-NNNNN format (e.g. NF-2026-00001).
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

SCHEMA_ID = "aiworkhub.needfix_store.v2"

HUB_DIRNAME = ".aiworkhub"
NEEDFIX_DB_REL = (HUB_DIRNAME, "tasking", "needfix.sqlite")

# Bounded payload guards -- no unbounded payloads, no artificial token cap
# beyond a generous, documented ceiling that protects the durable store
# from pathological input rather than throttling legitimate work.
MAX_DESCRIPTION_BYTES = 200_000
MAX_EVIDENCE_BYTES = 500_000
MAX_LIST_LIMIT = 500
DEFAULT_LIST_LIMIT = 100

STATUSES: tuple[str, ...] = (
    "captured",      # unverified worker proposal, entry state
    "triaged",       # initial manager review completed
    "accepted",      # confirmed valid, ready for task planning
    "duplicate",     # marked as duplicate of another needfix
    "rejected",      # explicitly rejected with reason
    "deferred",      # postponed for later consideration
    "task_planned",  # task creation planned/queued
    "task_created",  # converted; converted_task_id recorded
    "resolved",      # underlying issue resolved
    "archived",      # soft-deleted, audited, restorable
)

CLAIMABLE_STATUSES: tuple[str, ...] = ("accepted", "task_planned")

TERMINAL_STATUSES: tuple[str, ...] = ("duplicate", "rejected", "resolved")

VALID_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "captured": ("triaged", "duplicate", "rejected", "archived"),
    "triaged": ("accepted", "duplicate", "rejected", "deferred", "archived"),
    "accepted": ("task_planned", "duplicate", "rejected", "deferred", "archived"),
    "duplicate": ("archived",),
    "rejected": ("archived",),
    "deferred": ("triaged", "accepted", "rejected", "archived"),
    "task_planned": ("task_created", "accepted", "archived"),
    "task_created": ("resolved", "archived"),
    "resolved": ("archived",),
    "archived": (),  # restore handles archive specially
}

SEVERITIES: tuple[str, ...] = ("critical", "high", "medium", "low", "info")

KINDS: tuple[str, ...] = (
    "bug",
    "feature",
    "improvement",
    "idea",
    "technical_debt",
    "optimization",
    "benchmark_gap",
    "documentation_drift",
    "security_risk",
    "investigation",
    "roadmap_candidate",
    # Preserve the foundation's original public values for compatibility.
    "refactor",
    "security",
    "docs",
    "other",
)

NF_ID_RE = re.compile(r'^NF-\d{4}-\d{5}$')
MAX_ID_SEQUENCE = 99999


class NeedFixError(Exception):
    """Base error for NeedFix registry operations."""


class NeedFixNotFoundError(NeedFixError):
    pass


class NeedFixConflictError(NeedFixError):
    """Raised when a concurrency guard or lifecycle guard fails closed."""


class NeedFixValidationError(NeedFixError):
    pass


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db_path(repo_root: str | Path) -> Path:
    root = Path(repo_root)
    return root.joinpath(*NEEDFIX_DB_REL)


def _connect(repo_root: str | Path) -> sqlite3.Connection:
    path = _db_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def _generate_nf_id(conn: sqlite3.Connection) -> str:
    """Generate a stable NF-YYYY-NNNNN ID scoped to this repository.

    Sequence resets per year and is repo-isolated via the single database.
    """
    year = datetime.now(timezone.utc).strftime("%Y")
    row = conn.execute(
        "SELECT id FROM needfix WHERE id LIKE ? ORDER BY id DESC LIMIT 1",
        (f"NF-{year}-%",),
    ).fetchone()
    if row is not None:
        last_num = int(row["id"].split("-")[-1])
        next_num = last_num + 1
    else:
        next_num = 1
    if next_num > MAX_ID_SEQUENCE:
        raise NeedFixError(f"NF ID sequence exhausted for year {year}")
    return f"NF-{year}-{next_num:05d}"


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS needfix (
    id TEXT PRIMARY KEY,
    dedupe_key TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'other',
    severity TEXT NOT NULL DEFAULT 'medium',
    tags_json TEXT NOT NULL DEFAULT '[]',
    scope TEXT,
    scope_files_json TEXT DEFAULT '[]',
    scope_symbols_json TEXT DEFAULT '[]',
    provenance_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    evidence_refs_json TEXT NOT NULL DEFAULT '[]',
    readiness_score INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    duplicate_parent_id TEXT,
    converted_task_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    task_planned_at TEXT,
    resolved_at TEXT,
    archived_at TEXT,
    UNIQUE(dedupe_key, status) ON CONFLICT IGNORE
);

CREATE INDEX IF NOT EXISTS idx_needfix_dedupe ON needfix(dedupe_key);
CREATE INDEX IF NOT EXISTS idx_needfix_status ON needfix(status);
CREATE INDEX IF NOT EXISTS idx_needfix_kind ON needfix(kind);
CREATE INDEX IF NOT EXISTS idx_needfix_severity ON needfix(severity);
CREATE INDEX IF NOT EXISTS idx_needfix_duplicate_parent ON needfix(duplicate_parent_id);

CREATE TABLE IF NOT EXISTS needfix_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    needfix_id TEXT NOT NULL,
    event TEXT NOT NULL,
    detail_json TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_needfix_events_id ON needfix_events(needfix_id);
"""


def initialize_repository(repo_root: str | Path) -> dict[str, Any]:
    """Idempotently ensure the NeedFix store exists for ``repo_root``.

    Safe to call against a fresh repository or an already-initialized one;
    preserves any existing rows/authority. Never touches canonical task
    store files.
    """

    conn = _connect(repo_root)
    try:
        conn.executescript(_SCHEMA_SQL)
        row = conn.execute("SELECT COUNT(*) AS n FROM needfix").fetchone()
        return {
            "schema_id": SCHEMA_ID,
            "initialized": True,
            "db_path": str(_db_path(repo_root)),
            "existing_count": int(row["n"]),
        }
    finally:
        conn.close()


def _dedupe_key(title: str, description: str, scope: str | None) -> str:
    payload = json.dumps(
        {"title": title.strip(), "description": description.strip(), "scope": scope or ""},
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _validate_payload(
    title: str,
    description: str,
    evidence: Any,
    kind: str = "other",
    severity: str = "medium",
    tags: Sequence[str] | None = None,
    scope_files: Sequence[str] | None = None,
    scope_symbols: Sequence[str] | None = None,
    readiness_score: int = 0,
) -> None:
    if not title or not isinstance(title, str):
        raise NeedFixValidationError("title is required")
    if not description or not isinstance(description, str):
        raise NeedFixValidationError("description is required")
    if len(description.encode("utf-8")) > MAX_DESCRIPTION_BYTES:
        raise NeedFixValidationError("description exceeds bounded size")
    evidence_bytes = len(json.dumps(evidence or {}).encode("utf-8"))
    if evidence_bytes > MAX_EVIDENCE_BYTES:
        raise NeedFixValidationError("evidence payload exceeds bounded size")
    if kind not in KINDS:
        raise NeedFixValidationError(f"invalid kind: {kind!r}; valid: {KINDS}")
    if severity not in SEVERITIES:
        raise NeedFixValidationError(f"invalid severity: {severity!r}; valid: {SEVERITIES}")
    if readiness_score < 0 or readiness_score > 100:
        raise NeedFixValidationError("readiness_score must be 0-100")


def _record_event(
    conn: sqlite3.Connection,
    needfix_id: str,
    event: str,
    detail: Mapping[str, Any] | None = None,
) -> None:
    conn.execute(
        "INSERT INTO needfix_events (needfix_id, event, detail_json, created_at) VALUES (?, ?, ?, ?)",
        (needfix_id, event, json.dumps(detail or {}), _utcnow_iso()),
    )


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "dedupe_key": row["dedupe_key"],
        "title": row["title"],
        "description": row["description"],
        "kind": row["kind"],
        "severity": row["severity"],
        "tags": json.loads(row["tags_json"]),
        "scope": row["scope"],
        "scope_files": json.loads(row["scope_files_json"]),
        "scope_symbols": json.loads(row["scope_symbols_json"]),
        "provenance": json.loads(row["provenance_json"]),
        "evidence": json.loads(row["evidence_json"]),
        "evidence_refs": json.loads(row["evidence_refs_json"]),
        "readiness_score": row["readiness_score"],
        "status": row["status"],
        "duplicate_parent_id": row["duplicate_parent_id"],
        "converted_task_id": row["converted_task_id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "task_planned_at": row["task_planned_at"],
        "resolved_at": row["resolved_at"],
        "archived_at": row["archived_at"],
    }


def _check_duplicate_cycle(
    conn: sqlite3.Connection, needfix_id: str, proposed_parent_id: str
) -> None:
    """Fail closed if ``proposed_parent_id`` (transitively) points to ``needfix_id``."""
    if proposed_parent_id == needfix_id:
        raise NeedFixValidationError("cannot mark a needfix as a duplicate of itself")
    visited: set[str] = set()
    current = proposed_parent_id
    while current:
        if current in visited:
            raise NeedFixValidationError(
                f"duplicate-parent cycle detected involving {current}"
            )
        if current == needfix_id:
            raise NeedFixValidationError(
                f"duplicate-parent cycle: {proposed_parent_id} transitively points to {needfix_id}"
            )
        visited.add(current)
        row = conn.execute(
            "SELECT duplicate_parent_id FROM needfix WHERE id = ?", (current,)
        ).fetchone()
        current = row["duplicate_parent_id"] if (row and row["duplicate_parent_id"]) else ""


def _validate_transition(current_status: str, target_status: str) -> None:
    """Lifecycle guard: fail closed on invalid transitions."""
    if current_status == target_status:
        return  # idempotent no-op
    allowed = VALID_TRANSITIONS.get(current_status, ())
    if target_status not in allowed:
        raise NeedFixConflictError(
            f"invalid transition: {current_status!r} -> {target_status!r}; "
            f"allowed: {allowed}"
        )


def _insert(
    repo_root: str | Path,
    *,
    title: str,
    description: str,
    scope: str | None,
    provenance: Mapping[str, Any],
    evidence: Mapping[str, Any] | None,
    status: str,
    kind: str = "other",
    severity: str = "medium",
    tags: Sequence[str] | None = None,
    scope_files: Sequence[str] | None = None,
    scope_symbols: Sequence[str] | None = None,
    evidence_refs: Sequence[str] | None = None,
    readiness_score: int = 0,
) -> dict[str, Any]:
    _validate_payload(
        title,
        description,
        evidence,
        kind=kind,
        severity=severity,
        tags=tags,
        scope_files=scope_files,
        scope_symbols=scope_symbols,
        readiness_score=readiness_score,
    )
    dedupe_key = _dedupe_key(title, description, scope)
    conn = _connect(repo_root)
    try:
        conn.executescript(_SCHEMA_SQL)
        existing = conn.execute(
            "SELECT * FROM needfix WHERE dedupe_key = ? AND status != 'archived' LIMIT 1",
            (dedupe_key,),
        ).fetchone()
        if existing is not None:
            _record_event(conn, existing["id"], "dedupe_hit", {"dedupe_key": dedupe_key})
            return _row_to_dict(existing)

        needfix_id = _generate_nf_id(conn)
        now = _utcnow_iso()
        conn.execute(
            """INSERT INTO needfix
            (id, dedupe_key, title, description, kind, severity,
             tags_json, scope, scope_files_json, scope_symbols_json,
             provenance_json, evidence_json, evidence_refs_json,
             readiness_score, status, duplicate_parent_id,
             converted_task_id, created_at, updated_at,
             task_planned_at, resolved_at, archived_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, NULL, NULL, NULL)""",
            (
                needfix_id,
                dedupe_key,
                title,
                description,
                kind,
                severity,
                json.dumps(list(tags or [])),
                scope,
                json.dumps(list(scope_files or [])),
                json.dumps(list(scope_symbols or [])),
                json.dumps(dict(provenance or {})),
                json.dumps(dict(evidence or {})),
                json.dumps(list(evidence_refs or [])),
                readiness_score,
                status,
                now,
                now,
            ),
        )
        _record_event(conn, needfix_id, "created", {"status": status})
        row = conn.execute("SELECT * FROM needfix WHERE id = ?", (needfix_id,)).fetchone()
        return _row_to_dict(row)
    finally:
        conn.close()


def capture_proposal(
    repo_root: str | Path,
    *,
    title: str,
    description: str,
    scope: str | None = None,
    provenance: Mapping[str, Any] | None = None,
    evidence: Mapping[str, Any] | None = None,
    kind: str = "other",
    severity: str = "medium",
    tags: Sequence[str] | None = None,
    scope_files: Sequence[str] | None = None,
    scope_symbols: Sequence[str] | None = None,
    evidence_refs: Sequence[str] | None = None,
    readiness_score: int = 0,
) -> dict[str, Any]:
    """Worker/unverified entry point. Always lands as ``captured``.

    Workers never have manager mutation authority here: status is fixed
    regardless of any caller-supplied value. Provenance is stamped
    unverified. Conversion is blocked until a manager explicitly promotes
    the NeedFix past ``captured``/``triaged`` to ``accepted``.
    """

    prov = dict(provenance or {})
    prov.setdefault("origin", "worker_proposal")
    prov["verified"] = False
    return _insert(
        repo_root,
        title=title,
        description=description,
        scope=scope,
        provenance=prov,
        evidence=evidence,
        status="captured",
        kind=kind,
        severity=severity,
        tags=tags,
        scope_files=scope_files,
        scope_symbols=scope_symbols,
        evidence_refs=evidence_refs,
        readiness_score=readiness_score,
    )


def add_needfix(
    repo_root: str | Path,
    *,
    title: str,
    description: str,
    scope: str | None = None,
    provenance: Mapping[str, Any] | None = None,
    evidence: Mapping[str, Any] | None = None,
    status: str = "captured",
    kind: str = "other",
    severity: str = "medium",
    tags: Sequence[str] | None = None,
    scope_files: Sequence[str] | None = None,
    scope_symbols: Sequence[str] | None = None,
    evidence_refs: Sequence[str] | None = None,
    readiness_score: int = 0,
) -> dict[str, Any]:
    """Manager mutation entry point with explicit, distinct authority.

    Unlike ``capture_proposal`` this may set an initial status beyond
    ``captured`` because it represents an already manager-mediated call,
    not raw unverified worker input. Provenance is stamped as manager_add.
    """

    if status not in STATUSES:
        raise NeedFixValidationError(f"invalid status: {status!r}")
    prov = dict(provenance or {})
    prov.setdefault("origin", "manager_add")
    prov["verified"] = True
    return _insert(
        repo_root,
        title=title,
        description=description,
        scope=scope,
        provenance=prov,
        evidence=evidence,
        status=status,
        kind=kind,
        severity=severity,
        tags=tags,
        scope_files=scope_files,
        scope_symbols=scope_symbols,
        evidence_refs=evidence_refs,
        readiness_score=readiness_score,
    )

def get_needfix(repo_root: str | Path, needfix_id: str) -> dict[str, Any]:
    conn = _connect(repo_root)
    try:
        row = conn.execute("SELECT * FROM needfix WHERE id = ?", (needfix_id,)).fetchone()
        if row is None:
            raise NeedFixNotFoundError(needfix_id)
        return _row_to_dict(row)
    finally:
        conn.close()


def list_needfix(
    repo_root: str | Path,
    *,
    status: str | None = None,
    kind: str | None = None,
    severity: str | None = None,
    include_archived: bool = False,
    limit: int = DEFAULT_LIST_LIMIT,
    offset: int = 0,
    order_by: str = "created_at",
    order_dir: str = "DESC",
) -> list[dict[str, Any]]:
    """Bounded list with filters and pagination.

    Filters: status, kind, severity, include_archived.
    Pagination: limit (1-500), offset (>= 0).
    Ordering: created_at or updated_at, ASC or DESC.
    """
    bounded_limit = max(1, min(int(limit), MAX_LIST_LIMIT))
    if order_by not in ("created_at", "updated_at"):
        order_by = "created_at"
    if order_dir.upper() not in ("ASC", "DESC"):
        order_dir = "DESC"
    conn = _connect(repo_root)
    try:
        clauses: list[str] = []
        params: list[Any] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        elif not include_archived:
            clauses.append("status != 'archived'")
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        if severity is not None:
            clauses.append("severity = ?")
            params.append(severity)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = conn.execute(
            f"SELECT * FROM needfix {where} ORDER BY {order_by} {order_dir} LIMIT ? OFFSET ?",
            (*params, bounded_limit, max(0, int(offset))),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def _apply_transition(
    conn: sqlite3.Connection,
    needfix_id: str,
    target_status: str,
    event: str,
    detail: dict[str, Any] | None = None,
    extra_sets: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Atomically apply a guarded status transition."""
    row = conn.execute("SELECT * FROM needfix WHERE id = ?", (needfix_id,)).fetchone()
    if row is None:
        raise NeedFixNotFoundError(needfix_id)
    current = row["status"]
    _validate_transition(current, target_status)
    sets = ["status = ?", "updated_at = ?"]
    params: list[Any] = [target_status, _utcnow_iso()]
    if extra_sets:
        for col, val in extra_sets.items():
            sets.append(f"{col} = ?")
            params.append(val)
    params.append(needfix_id)
    conn.execute(f"UPDATE needfix SET {', '.join(sets)} WHERE id = ?", params)
    _record_event(conn, needfix_id, event, dict(detail or {}, prior_status=current))
    updated = conn.execute("SELECT * FROM needfix WHERE id = ?", (needfix_id,)).fetchone()
    return _row_to_dict(updated)


def triage_needfix(
    repo_root: str | Path,
    needfix_id: str,
    *,
    readiness_score: int | None = None,
    triage_note: str | None = None,
) -> dict[str, Any]:
    """Manager action: promote from captured -> triaged."""
    conn = _connect(repo_root)
    try:
        extra: dict[str, Any] = {}
        if readiness_score is not None:
            extra["readiness_score"] = readiness_score
        return _apply_transition(
            conn, needfix_id, "triaged", "triaged",
            detail={"triage_note": triage_note, "readiness_score": readiness_score} if (triage_note or readiness_score is not None) else None,
            extra_sets=extra if extra else None,
        )
    finally:
        conn.close()


def accept_needfix(
    repo_root: str | Path,
    needfix_id: str,
    *,
    readiness_score: int | None = None,
) -> dict[str, Any]:
    """Manager action: accept a needfix as valid (triaged|deferred -> accepted)."""
    conn = _connect(repo_root)
    try:
        extra: dict[str, Any] = {}
        if readiness_score is not None:
            extra["readiness_score"] = readiness_score
        return _apply_transition(
            conn, needfix_id, "accepted", "accepted",
            extra_sets=extra if extra else None,
        )
    finally:
        conn.close()


def reject_needfix(
    repo_root: str | Path,
    needfix_id: str,
    *,
    reason: str,
) -> dict[str, Any]:
    """Manager action: reject a needfix with mandatory reason."""
    if not reason.strip():
        raise NeedFixValidationError("reason is required for rejection")
    conn = _connect(repo_root)
    try:
        return _apply_transition(
            conn, needfix_id, "rejected", "rejected",
            detail={"reason": reason},
        )
    finally:
        conn.close()


def mark_duplicate(
    repo_root: str | Path,
    needfix_id: str,
    duplicate_parent_id: str,
    *,
    reason: str | None = None,
) -> dict[str, Any]:
    """Manager action: mark as duplicate of another needfix with cycle guard.

    The duplicate-parent cycle guard traverses the duplicate_parent_id chain
    from ``duplicate_parent_id`` to ensure it does not point (directly or
    transitively) back to ``needfix_id``, failing closed on any cycle.
    """
    conn = _connect(repo_root)
    try:
        parent = conn.execute(
            "SELECT id FROM needfix WHERE id = ? AND status != 'archived'",
            (duplicate_parent_id,),
        ).fetchone()
        if parent is None:
            raise NeedFixNotFoundError(
                f"duplicate parent {duplicate_parent_id} not found or archived"
            )
        _check_duplicate_cycle(conn, needfix_id, duplicate_parent_id)
        return _apply_transition(
            conn, needfix_id, "duplicate", "marked_duplicate",
            detail={"duplicate_parent_id": duplicate_parent_id, "reason": reason},
            extra_sets={"duplicate_parent_id": duplicate_parent_id},
        )
    finally:
        conn.close()


def defer_needfix(
    repo_root: str | Path,
    needfix_id: str,
    *,
    reason: str | None = None,
) -> dict[str, Any]:
    """Manager action: defer a needfix for later consideration."""
    conn = _connect(repo_root)
    try:
        return _apply_transition(
            conn, needfix_id, "deferred", "deferred",
            detail={"reason": reason},
        )
    finally:
        conn.close()


def mark_task_planned(
    repo_root: str | Path,
    needfix_id: str,
) -> dict[str, Any]:
    """Manager action: mark as task planned (accepted -> task_planned)."""
    conn = _connect(repo_root)
    try:
        return _apply_transition(
            conn, needfix_id, "task_planned", "task_planned",
            extra_sets={"task_planned_at": _utcnow_iso()},
        )
    finally:
        conn.close()


def resolve_needfix(
    repo_root: str | Path,
    needfix_id: str,
    *,
    resolution_note: str | None = None,
) -> dict[str, Any]:
    """Manager action: mark as resolved (task_created -> resolved)."""
    conn = _connect(repo_root)
    try:
        return _apply_transition(
            conn, needfix_id, "resolved", "resolved",
            detail={"resolution_note": resolution_note},
            extra_sets={"resolved_at": _utcnow_iso()},
        )
    finally:
        conn.close()


def update_needfix(
    repo_root: str | Path,
    needfix_id: str,
    *,
    title: str | None = None,
    description: str | None = None,
    scope: str | None = None,
    kind: str | None = None,
    severity: str | None = None,
    tags: Sequence[str] | None = None,
    scope_files: Sequence[str] | None = None,
    scope_symbols: Sequence[str] | None = None,
    evidence: Mapping[str, Any] | None = None,
    evidence_refs: Sequence[str] | None = None,
    readiness_score: int | None = None,
) -> dict[str, Any]:
    """Update mutable fields. Blocked for transient/terminal statuses."""
    conn = _connect(repo_root)
    try:
        row = conn.execute("SELECT * FROM needfix WHERE id = ?", (needfix_id,)).fetchone()
        if row is None:
            raise NeedFixNotFoundError(needfix_id)
        if row["status"] in ("converting",):
            raise NeedFixConflictError(
                f"cannot update needfix {needfix_id} in transient status {row['status']!r}"
            )
        if kind is not None and kind not in KINDS:
            raise NeedFixValidationError(f"invalid kind: {kind!r}")
        if severity is not None and severity not in SEVERITIES:
            raise NeedFixValidationError(f"invalid severity: {severity!r}")
        if readiness_score is not None and (readiness_score < 0 or readiness_score > 100):
            raise NeedFixValidationError("readiness_score must be 0-100")

        new_title = title if title is not None else row["title"]
        new_description = description if description is not None else row["description"]
        new_kind = kind if kind is not None else row["kind"]
        new_severity = severity if severity is not None else row["severity"]
        new_tags = json.dumps(list(tags)) if tags is not None else row["tags_json"]
        new_scope = scope if scope is not None else row["scope"]
        new_scope_files = json.dumps(list(scope_files)) if scope_files is not None else row["scope_files_json"]
        new_scope_symbols = json.dumps(list(scope_symbols)) if scope_symbols is not None else row["scope_symbols_json"]
        new_evidence = json.dumps(dict(evidence)) if evidence is not None else row["evidence_json"]
        new_evidence_refs = json.dumps(list(evidence_refs)) if evidence_refs is not None else row["evidence_refs_json"]
        new_readiness = readiness_score if readiness_score is not None else row["readiness_score"]

        conn.execute(
            """UPDATE needfix SET title = ?, description = ?, kind = ?, severity = ?,
            tags_json = ?, scope = ?, scope_files_json = ?, scope_symbols_json = ?,
            evidence_json = ?, evidence_refs_json = ?, readiness_score = ?,
            updated_at = ? WHERE id = ?""",
            (
                new_title, new_description, new_kind, new_severity,
                new_tags, new_scope, new_scope_files, new_scope_symbols,
                new_evidence, new_evidence_refs, new_readiness,
                _utcnow_iso(), needfix_id,
            ),
        )
        _record_event(conn, needfix_id, "updated", {"fields_updated": True})
        updated = conn.execute("SELECT * FROM needfix WHERE id = ?", (needfix_id,)).fetchone()
        return _row_to_dict(updated)
    finally:
        conn.close()

def archive_needfix(
    repo_root: str | Path, needfix_id: str, *, reason: str | None = None
) -> dict[str, Any]:
    conn = _connect(repo_root)
    try:
        row = conn.execute("SELECT * FROM needfix WHERE id = ?", (needfix_id,)).fetchone()
        if row is None:
            raise NeedFixNotFoundError(needfix_id)
        if row["status"] == "converting":
            raise NeedFixConflictError(f"cannot archive needfix {needfix_id} while converting")
        if row["status"] == "archived":
            return _row_to_dict(row)
        prior = row["status"]
        conn.execute(
            "UPDATE needfix SET status = 'archived', archived_at = ?, updated_at = ? WHERE id = ?",
            (_utcnow_iso(), _utcnow_iso(), needfix_id),
        )
        _record_event(conn, needfix_id, "archived", {"reason": reason, "prior_status": prior})
        updated = conn.execute("SELECT * FROM needfix WHERE id = ?", (needfix_id,)).fetchone()
        return _row_to_dict(updated)
    finally:
        conn.close()


def restore_needfix(
    repo_root: str | Path,
    needfix_id: str,
    *,
    target_status: str = "captured",
) -> dict[str, Any]:
    """Restore an archived NeedFix to an active non-terminal status."""
    if target_status not in ("captured", "triaged", "accepted", "deferred"):
        raise NeedFixValidationError(
            f"restore target_status must be captured|triaged|accepted|deferred, got {target_status!r}"
        )
    conn = _connect(repo_root)
    try:
        row = conn.execute("SELECT * FROM needfix WHERE id = ?", (needfix_id,)).fetchone()
        if row is None:
            raise NeedFixNotFoundError(needfix_id)
        if row["status"] != "archived":
            raise NeedFixConflictError(f"needfix {needfix_id} is not archived (currently {row['status']!r})")
        conn.execute(
            "UPDATE needfix SET status = ?, archived_at = NULL, updated_at = ? WHERE id = ?",
            (target_status, _utcnow_iso(), needfix_id),
        )
        _record_event(conn, needfix_id, "restored", {"target_status": target_status})
        updated = conn.execute("SELECT * FROM needfix WHERE id = ?", (needfix_id,)).fetchone()
        return _row_to_dict(updated)
    finally:
        conn.close()


def purge_needfix(
    repo_root: str | Path, needfix_id: str, *, audit_reason: str
) -> dict[str, Any]:
    """Hard delete -- only ever audited and only for already-archived rows."""

    if not audit_reason:
        raise NeedFixValidationError("audit_reason is required for purge")
    conn = _connect(repo_root)
    try:
        row = conn.execute("SELECT * FROM needfix WHERE id = ?", (needfix_id,)).fetchone()
        if row is None:
            raise NeedFixNotFoundError(needfix_id)
        if row["status"] != "archived":
            raise NeedFixConflictError("only archived needfix rows may be purged")
        snapshot = _row_to_dict(row)
        _record_event(conn, needfix_id, "purged", {"audit_reason": audit_reason, "snapshot": snapshot})
        conn.execute("DELETE FROM needfix WHERE id = ?", (needfix_id,))
        return {"id": needfix_id, "purged": True, "audit_snapshot": snapshot}
    finally:
        conn.close()


def count_needfix(
    repo_root: str | Path,
    *,
    status: str | None = None,
    kind: str | None = None,
    severity: str | None = None,
) -> int:
    conn = _connect(repo_root)
    try:
        clauses: list[str] = []
        params: list[Any] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        else:
            clauses.append("status != 'archived'")
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        if severity is not None:
            clauses.append("severity = ?")
            params.append(severity)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        row = conn.execute(f"SELECT COUNT(*) AS n FROM needfix {where}", params).fetchone()
        return int(row["n"])
    finally:
        conn.close()


def list_events(
    repo_root: str | Path, needfix_id: str, *, limit: int = DEFAULT_LIST_LIMIT
) -> list[dict[str, Any]]:
    bounded_limit = max(1, min(int(limit), MAX_LIST_LIMIT))
    conn = _connect(repo_root)
    try:
        rows = conn.execute(
            "SELECT * FROM needfix_events WHERE needfix_id = ? ORDER BY seq DESC LIMIT ?",
            (needfix_id, bounded_limit),
        ).fetchall()
        return [
            {
                "seq": r["seq"],
                "needfix_id": r["needfix_id"],
                "event": r["event"],
                "detail": json.loads(r["detail_json"] or "{}"),
                "created_at": r["created_at"],
            }
            for r in rows
        ]
    finally:
        conn.close()


def preview_convert(repo_root: str | Path, needfix_id: str) -> dict[str, Any]:
    """Read-only preview of what conversion would do. Never mutates state."""

    row = get_needfix(repo_root, needfix_id)
    if row["status"] == "task_created":
        return {
            "needfix_id": needfix_id,
            "already_converted": True,
            "converted_task_id": row["converted_task_id"],
        }
    if row["status"] == "resolved":
        return {
            "needfix_id": needfix_id,
            "already_converted": True,
            "converted_task_id": row["converted_task_id"],
            "resolved": True,
        }
    claimable = row["status"] in CLAIMABLE_STATUSES
    return {
        "needfix_id": needfix_id,
        "already_converted": False,
        "claimable": claimable,
        "current_status": row["status"],
        "unverified": row["status"] in ("captured",),
        "triaged": row["status"] == "triaged",
        "proposed_task_title": row["title"],
        "proposed_task_objective": row["description"],
    }


def convert_needfix(
    repo_root: str | Path,
    needfix_id: str,
    create_task_fn: Callable[[dict[str, Any]], Mapping[str, Any]],
    *,
    task_card_builder: Callable[[dict[str, Any]], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Explicit, schema-valid, atomic, idempotent, concurrency-safe convert.

    ``create_task_fn`` must be the existing authoritative task-store/core
    ``create_task`` callable (or an adapter around it); this module never
    creates tasks itself.

    Guards:
    - captured/unverified cannot convert (must be accepted or task_planned)
    - Idempotent short-circuit: if already ``task_created`` return existing
      ``converted_task_id`` without re-creating a task (lost-ack-safe).
    - Atomic claim via conditional UPDATE from a claimable status to
      ``converting``; 0 rows updated means another caller already won the
      race or the row is not claimable -- fails closed.
    - On any task-creation failure the claim is compensated back to its
      original status so the NeedFix is never stranded as ``task_created``
      without a real task.
    - On success, atomically record ``task_created`` + ``converted_task_id``.
    """

    conn = _connect(repo_root)
    try:
        row = conn.execute("SELECT * FROM needfix WHERE id = ?", (needfix_id,)).fetchone()
        if row is None:
            raise NeedFixNotFoundError(needfix_id)

        if row["status"] == "task_created":
            return {
                "needfix_id": needfix_id,
                "converted_task_id": row["converted_task_id"],
                "already_converted": True,
            }

        if row["status"] == "resolved":
            return {
                "needfix_id": needfix_id,
                "converted_task_id": row["converted_task_id"],
                "already_converted": True,
                "resolved": True,
            }

        if row["status"] in ("captured", "triaged"):
            raise NeedFixConflictError(
                f"needfix {needfix_id} is {row['status']!r} -- "
                f"captured/unverified NeedFixes cannot be converted. "
                f"A manager must first accept this NeedFix."
            )

        prior_status = row["status"]
        cur = conn.execute(
            "UPDATE needfix SET status = 'converting', updated_at = ? "
            "WHERE id = ? AND status IN ('accepted', 'task_planned')",
            (_utcnow_iso(), needfix_id),
        )
        if cur.rowcount != 1:
            raise NeedFixConflictError(
                f"needfix {needfix_id} could not be claimed for conversion "
                f"(current status is {prior_status!r}, must be accepted or task_planned)"
            )
        _record_event(conn, needfix_id, "conversion_claimed", {"prior_status": prior_status})
        needfix_snapshot = _row_to_dict(
            conn.execute("SELECT * FROM needfix WHERE id = ?", (needfix_id,)).fetchone()
        )
    finally:
        conn.close()

    try:
        if task_card_builder is not None:
            card = dict(task_card_builder(needfix_snapshot))
        else:
            card = {
                "task_id": f"needfix-{needfix_id}",
                "title": needfix_snapshot["title"],
                "objective": needfix_snapshot["description"],
            }
        result = create_task_fn(card)
        task_id = None
        if isinstance(result, Mapping):
            task_id = result.get("task_id") or result.get("id")
        if not task_id:
            raise NeedFixError("create_task_fn did not return a task_id")
    except Exception:
        conn = _connect(repo_root)
        try:
            conn.execute(
                "UPDATE needfix SET status = ?, updated_at = ? "
                "WHERE id = ? AND status = 'converting'",
                (prior_status, _utcnow_iso(), needfix_id),
            )
            _record_event(
                conn,
                needfix_id,
                "conversion_compensated",
                {"restored_status": prior_status},
            )
        finally:
            conn.close()
        raise

    conn = _connect(repo_root)
    try:
        conn.execute(
            "UPDATE needfix SET status = 'task_created', converted_task_id = ?, "
            "updated_at = ? WHERE id = ? AND status = 'converting'",
            (task_id, _utcnow_iso(), needfix_id),
        )
        _record_event(conn, needfix_id, "conversion_committed", {"converted_task_id": task_id})
        return {
            "needfix_id": needfix_id,
            "converted_task_id": task_id,
            "already_converted": False,
        }
    finally:
        conn.close()


def link_existing_task(
    repo_root: str | Path,
    needfix_id: str,
    existing_task_id: str,
    get_task_fn: Callable[[str], Mapping[str, Any] | None],
    canonical_status_fn: Callable[[Mapping[str, Any]], str],
) -> dict[str, Any]:
    """Explicit, manager-only, atomic, idempotent link to an already-existing task.

    ``get_task_fn`` and ``canonical_status_fn`` must be the existing
    authoritative task-store ``get_task`` / ``canonical_status`` callables
    (or adapters around them) bound to this repository; this module never
    reads or mutates the task store itself.

    Guards mirror ``convert_needfix``:
    - captured/unverified cannot link (must be accepted or task_planned)
    - Idempotent short-circuit: an identical retry (same ``needfix_id`` and
      ``existing_task_id``) on an already ``task_created``/``resolved``
      NeedFix returns the existing link without reclaiming (lost-ack-safe).
      A retry with a *different* ``existing_task_id`` fails closed as a
      conflict instead of silently repointing the link.
    - Atomic claim via conditional UPDATE from a claimable status to
      ``converting``; 0 rows updated means another caller already won the
      race or the row is not claimable -- fails closed.
    - The candidate task identity is verified only after the claim, against
      a stable snapshot: missing/foreign/fabricated ids (``get_task_fn``
      returns ``None``) and ids that are not manager-accepted-and-finished
      (``canonical_status_fn`` result != ``"finished"``) both fail closed.
    - On any verification failure the claim is compensated back to its
      original status so the NeedFix is never stranded as ``task_created``
      without a real linked task.
    - On success, atomically records ``task_created`` + ``converted_task_id``
      plus audited provenance of the link.
    """

    existing_task_id = str(existing_task_id or "").strip()
    if not existing_task_id:
        raise NeedFixValidationError("existing_task_id is required")

    conn = _connect(repo_root)
    try:
        row = conn.execute("SELECT * FROM needfix WHERE id = ?", (needfix_id,)).fetchone()
        if row is None:
            raise NeedFixNotFoundError(needfix_id)

        if row["status"] == "task_created":
            if row["converted_task_id"] == existing_task_id:
                return {
                    "needfix_id": needfix_id,
                    "converted_task_id": row["converted_task_id"],
                    "already_converted": True,
                }
            raise NeedFixConflictError(
                f"needfix {needfix_id} is already linked to "
                f"{row['converted_task_id']!r}, not {existing_task_id!r}"
            )

        if row["status"] == "resolved":
            if row["converted_task_id"] == existing_task_id:
                return {
                    "needfix_id": needfix_id,
                    "converted_task_id": row["converted_task_id"],
                    "already_converted": True,
                    "resolved": True,
                }
            raise NeedFixConflictError(
                f"needfix {needfix_id} is already resolved and linked to "
                f"{row['converted_task_id']!r}, not {existing_task_id!r}"
            )

        if row["status"] in ("captured", "triaged"):
            raise NeedFixConflictError(
                f"needfix {needfix_id} is {row['status']!r} -- "
                f"captured/unverified NeedFixes cannot be linked. "
                f"A manager must first accept this NeedFix."
            )

        prior_status = row["status"]
        cur = conn.execute(
            "UPDATE needfix SET status = 'converting', updated_at = ? "
            "WHERE id = ? AND status IN ('accepted', 'task_planned')",
            (_utcnow_iso(), needfix_id),
        )
        if cur.rowcount != 1:
            raise NeedFixConflictError(
                f"needfix {needfix_id} could not be claimed for linking "
                f"(current status is {prior_status!r}, must be accepted or task_planned)"
            )
        _record_event(
            conn, needfix_id, "existing_task_link_claimed",
            {"prior_status": prior_status, "existing_task_id": existing_task_id},
        )
    finally:
        conn.close()

    try:
        task = get_task_fn(existing_task_id)
        if task is None:
            raise NeedFixValidationError(
                f"existing task {existing_task_id!r} not found in this repository"
            )
        task_status = canonical_status_fn(task)
        if task_status != "finished":
            raise NeedFixConflictError(
                f"existing task {existing_task_id!r} is not manager-accepted "
                f"and finished (canonical status is {task_status!r})"
            )
    except Exception:
        conn = _connect(repo_root)
        try:
            conn.execute(
                "UPDATE needfix SET status = ?, updated_at = ? "
                "WHERE id = ? AND status = 'converting'",
                (prior_status, _utcnow_iso(), needfix_id),
            )
            _record_event(
                conn, needfix_id, "existing_task_link_compensated",
                {"restored_status": prior_status},
            )
        finally:
            conn.close()
        raise

    conn = _connect(repo_root)
    try:
        conn.execute(
            "UPDATE needfix SET status = 'task_created', converted_task_id = ?, "
            "updated_at = ? WHERE id = ? AND status = 'converting'",
            (existing_task_id, _utcnow_iso(), needfix_id),
        )
        _record_event(
            conn, needfix_id, "existing_task_linked",
            {"converted_task_id": existing_task_id},
        )
        return {
            "needfix_id": needfix_id,
            "converted_task_id": existing_task_id,
            "already_converted": False,
        }
    finally:
        conn.close()
