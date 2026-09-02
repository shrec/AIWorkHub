"""Durable, repository-native persistence for versioned skill records.

``skill_registry`` owns a complete, tested in-memory lifecycle -- propose,
add_evidence, activate, promote, retire, select, build_runtime_packet -- but had
no way to keep a record between two calls, so evidence could never accumulate
toward the ``min_accepted_evidence`` activation threshold and the dashboard
skills panel was structurally empty forever.

This module is that missing durable layer, following the same repository-native
SQLite shape as the other stores in this package (see ``roadmap_store``):

* Canonical, additive schema: :func:`ensure_schema` only ever issues
  ``CREATE TABLE IF NOT EXISTS`` / ``CREATE ... INDEX IF NOT EXISTS`` so opening
  an older database never rewrites or drops an existing row.
* Exact-identity, immutable rows: ``(identity, version)`` is the primary key, so
  a version once written can never be overwritten; two versions of one identity
  both persist side by side.
* A digest that can never be rebound: the content digest carries a ``UNIQUE``
  index, so the same digest can never be bound to a second identity/version.
* Fail-closed reads: a stored record whose recomputed content digest does not
  match its persisted digest is rejected on read and never silently repaired.
* What the digests do and do not provide: ``skill_registry.skill_digest``
  hashes only the content fields and excludes the runtime authorization state
  (evidence, ``lifecycle_state``, ``accepted_count``, ``negative_count``), so a
  content digest alone cannot see a tamper that only rewrites runtime state. A
  second ``state_digest`` computed over the *full* persisted payload covers
  every field, so rewriting any one column without the digest that spans it is
  caught. Both digests are unkeyed SHA-256 values living in the SAME row as the
  payload they cover, so they are DETECTION, not authentication: they catch
  accidental corruption, a truncated or partial write, and naive hand-editing of
  a single column. They do NOT resist an adversary who can already write the
  row -- such a writer sets ``state_digest =
  sha256(canonical_payload_json(forged_record))`` and both checks pass, so a
  forged ``active`` record with forged evidence loads as authoritative. No keyed
  MAC is added to shut that door: the key would have to live in the same
  repository as the data, so it would buy nothing here.

Persistence preserves the full record -- content fields plus runtime state
(evidence, lifecycle, counters) -- through :func:`skill_registry.normalize`, so a
record round-trips byte-for-byte with both digests unchanged.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import skill_registry
from .skill_registry import SkillRecord, SkillRegistry
from .sqlite_readonly import connect_readonly

SCHEMA_ID = "aiworkhub.skill_registry_store.v1"
SKILLS_DB_REL = (".aiworkhub", "tasking", "skills.sqlite")

# Bounded reads: never materialize an unbounded registry from disk.
MAX_LOAD_LIMIT = 1000
DEFAULT_LOAD_LIMIT = MAX_LOAD_LIMIT


class SkillStoreError(Exception):
    """Base error for the durable skill registry store."""


class SkillStoreConflictError(SkillStoreError):
    """An immutable-identity or digest-rebinding write was rejected."""


class SkillStoreIntegrityError(SkillStoreError):
    """A stored record's digest does not match its recomputed content digest."""


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS skill_records (
    identity TEXT NOT NULL,
    version TEXT NOT NULL,
    digest TEXT NOT NULL,
    state_digest TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (identity, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_skill_records_digest
    ON skill_records(digest);
"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db_path(repo_root: str | Path) -> Path:
    return Path(repo_root).joinpath(*SKILLS_DB_REL)


def _connect(repo_root: str | Path) -> sqlite3.Connection:
    """Open the canonical skills database read-write, creating it if absent."""
    path = _db_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the skill_records table and digest index if they do not exist.

    Additive only: existing rows are never rewritten, so an older database is
    upgraded in place without data loss. A pre-existing table that predates the
    ``state_digest`` column gains it as an empty-default column; such legacy rows
    then fail closed on read until rewritten, never serving unverified state.
    """
    conn.executescript(_SCHEMA_SQL)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(skill_records)")}
    if "state_digest" not in columns:
        conn.execute(
            "ALTER TABLE skill_records ADD COLUMN state_digest TEXT NOT NULL DEFAULT ''"
        )


def initialize_repository(repo_root: str | Path) -> dict[str, Any]:
    """Idempotently ensure the canonical skills store exists for ``repo_root``."""
    conn = _connect(repo_root)
    try:
        ensure_schema(conn)
        count = int(conn.execute("SELECT COUNT(*) FROM skill_records").fetchone()[0])
        return {
            "schema_id": SCHEMA_ID,
            "initialized": True,
            "db_path": str(_db_path(repo_root)),
            "existing_count": count,
        }
    finally:
        conn.close()


def _record_payload(record: SkillRecord) -> dict[str, Any]:
    """Project a validated record to a JSON-safe mapping ``normalize`` accepts.

    Every field -- content and runtime state alike -- is preserved so the record
    reconstructs unchanged; enums are stored by their canonical string value.
    """
    return {
        "identity": record.identity,
        "version": record.version,
        "scope": record.scope.value,
        "task_family": record.task_family,
        "path_or_symbol": record.path_or_symbol,
        "risk": record.risk.value,
        "stage": record.stage,
        "triggers": list(record.triggers),
        "confidence": record.confidence,
        "applicability": list(record.applicability),
        "procedure_steps": list(record.procedure_steps),
        "avoid_rules": list(record.avoid_rules),
        "preferred_tools": list(record.preferred_tools),
        "evidence": [
            {
                "source": item.source,
                "outcome": item.outcome.value,
                "authority": item.authority.value,
                "actor_id": item.actor_id,
                "resolved": item.resolved,
                "note": item.note,
            }
            for item in record.evidence
        ],
        "lifecycle_state": record.lifecycle_state.value,
        "accepted_count": record.accepted_count,
        "negative_count": record.negative_count,
    }


def _canonical_payload_json(record: SkillRecord) -> str:
    """Return the canonical, sorted JSON payload bytes for one record."""
    return json.dumps(
        _record_payload(record),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def _state_digest(record: SkillRecord) -> str:
    """SHA-256 over the *full* persisted payload -- content and runtime state.

    :func:`skill_registry.skill_digest` hashes only the content fields, so a
    tamper that rewrites ``lifecycle_state``, ``accepted_count`` or ``evidence``
    leaves it unchanged. This digest covers every persisted field, so any change
    to the persisted authorization state is detected on read.
    """
    return hashlib.sha256(
        _canonical_payload_json(record).encode("utf-8")
    ).hexdigest()


def _serialize(record: SkillRecord) -> tuple[str, str, str, str, str]:
    """Return ``(identity, version, digest, state_digest, payload_json)``.

    Both digests are computed from the record as it will be reconstructed from
    the persisted JSON, so the write-time digests and every later read-time
    digest derive from the exact same canonical bytes.
    """
    validated = skill_registry.validate_record(record)
    payload_json = _canonical_payload_json(validated)
    reconstructed = SkillRecord.from_mapping(json.loads(payload_json))
    digest = skill_registry.skill_digest(reconstructed)
    state_digest = _state_digest(reconstructed)
    return (
        reconstructed.identity,
        reconstructed.version,
        digest,
        state_digest,
        payload_json,
    )


def _row_to_record(row: sqlite3.Row) -> SkillRecord:
    """Reconstruct and verify one persisted row, failing closed on tampering.

    Verifies both the content digest and the full-payload ``state_digest``, so a
    tamper that leaves the content fields intact but forges the runtime
    authorization state (lifecycle, evidence, counters) is still rejected.
    """
    record = SkillRecord.from_mapping(json.loads(row["payload_json"]))
    recomputed = skill_registry.skill_digest(record)
    if recomputed != row["digest"]:
        raise SkillStoreIntegrityError(
            f"stored digest for {row['identity']!r}@{row['version']!r} does not "
            "match its recomputed content digest"
        )
    recomputed_state = _state_digest(record)
    if recomputed_state != row["state_digest"]:
        raise SkillStoreIntegrityError(
            f"stored state digest for {row['identity']!r}@{row['version']!r} does "
            "not match its recomputed full-payload digest"
        )
    return record


def put_record(
    repo_root: str | Path,
    record: SkillRecord,
    *,
    _connection: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Persist one validated skill record with exact-identity immutability.

    Rejects a second write to the same ``(identity, version)`` and rejects
    binding an already-stored content digest to a different identity/version.
    """
    identity, version, digest, state_digest, payload_json = _serialize(record)
    conn = _connection or _connect(repo_root)
    try:
        ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute(
            "SELECT 1 FROM skill_records WHERE identity=? AND version=?",
            (identity, version),
        ).fetchone():
            raise SkillStoreConflictError(
                f"skill {identity!r}@{version!r} is already stored and immutable"
            )
        owner = conn.execute(
            "SELECT identity,version FROM skill_records WHERE digest=?",
            (digest,),
        ).fetchone()
        if owner is not None:
            raise SkillStoreConflictError(
                f"digest {digest} is already bound to "
                f"{owner['identity']!r}@{owner['version']!r} and cannot be rebound"
            )
        conn.execute(
            "INSERT INTO skill_records "
            "(identity,version,digest,state_digest,payload_json,created_at) "
            "VALUES (?,?,?,?,?,?)",
            (identity, version, digest, state_digest, payload_json, _utcnow()),
        )
        conn.commit()
        return {
            "identity": identity,
            "version": version,
            "digest": digest,
            "state_digest": state_digest,
        }
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        if _connection is None:
            conn.close()


def state_digest(record: SkillRecord) -> str:
    """Return the compare-and-swap token for a runtime advance of ``record``.

    This is the full-payload ``state_digest`` a caller reads from the record it
    loaded and passes back to :func:`advance_record` as ``expected_state_digest``.
    A read-modify-write lifecycle step (``add_evidence``/``activate``) is not
    serialized across processes, so a stale reader could otherwise advance from an
    out-of-date runtime state and silently overwrite a newer advance -- losing,
    for example, one of two independent accepted evidence entries. Binding the
    token the caller actually read turns that lost update into an explicit
    refusal instead.
    """
    _identity, _version, _digest, sd, _payload_json = _serialize(record)
    return sd


def advance_record(
    repo_root: str | Path,
    record: SkillRecord,
    *,
    expected_state_digest: str | None = None,
    _connection: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Persist a runtime-state advance of an already-stored ``(identity, version)``.

    The lifecycle -- ``add_evidence``, ``activate``, ``retire`` -- evolves a
    record's evidence, counters and ``lifecycle_state`` while leaving every
    content field, and therefore the content digest, unchanged. A row is
    immutable per ``(identity, version)``, so an advance cannot be a delete plus a
    re-``put_record``: that path commits the delete, then raises in ``put_record``
    validation, and the record is lost rather than left at its prior state.

    This updates ONLY the two runtime columns -- ``payload_json`` and the
    full-payload ``state_digest`` -- of the SAME row, inside one transaction, and
    refuses unless the stored row's ``identity``, ``version`` AND immutable
    content ``digest`` all match the advanced record. Content immutability and
    the unique digest binding are therefore never touched.

    ``expected_state_digest`` is an optional compare-and-swap precondition: when
    supplied (via :func:`state_digest` of the record the caller loaded), the
    advance is refused unless the stored row's runtime ``state_digest`` still
    equals it. The content digest alone cannot catch this, because a runtime
    advance leaves it unchanged, so a stale read-modify-write would pass the
    content check and overwrite a newer advance. The token is the cross-process
    authority a per-server lock cannot provide. Any refusal raises a
    :class:`SkillStoreConflictError` and leaves the stored row exactly as it was.
    """
    identity, version, digest, state_digest_value, payload_json = _serialize(record)
    conn = _connection or _connect(repo_root)
    try:
        ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT digest, state_digest FROM skill_records WHERE identity=? AND version=?",
            (identity, version),
        ).fetchone()
        if row is None:
            raise SkillStoreConflictError(
                f"skill {identity!r}@{version!r} is not stored and cannot be advanced"
            )
        if row["digest"] != digest:
            raise SkillStoreConflictError(
                f"content digest for {identity!r}@{version!r} does not match the stored "
                "row; an advance may evolve only runtime state, never content"
            )
        if expected_state_digest is not None and row["state_digest"] != expected_state_digest:
            raise SkillStoreConflictError(
                f"runtime state for {identity!r}@{version!r} changed under this advance; "
                "the compare-and-swap precondition no longer holds and a stale advance "
                "may not overwrite a newer one"
            )
        conn.execute(
            "UPDATE skill_records SET state_digest=?, payload_json=? "
            "WHERE identity=? AND version=?",
            (state_digest_value, payload_json, identity, version),
        )
        conn.commit()
        return {
            "identity": identity,
            "version": version,
            "digest": digest,
            "state_digest": state_digest_value,
        }
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        if _connection is None:
            conn.close()


def get_record(
    repo_root: str | Path, identity: str, version: str
) -> SkillRecord | None:
    """Return one persisted record, or ``None`` when it is absent.

    Fails closed with :class:`SkillStoreIntegrityError` if the stored digest does
    not match the recomputed content digest. Never creates the database.
    """
    path = _db_path(repo_root)
    if not path.exists():
        return None
    conn = connect_readonly(path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM skill_records WHERE identity=? AND version=?",
            (identity, version),
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    if row is None:
        return None
    return _row_to_record(row)


def list_records(
    repo_root: str | Path, *, limit: int = DEFAULT_LOAD_LIMIT, offset: int = 0
) -> list[SkillRecord]:
    """Return persisted records, bounded and fail-closed. Never creates the DB."""
    path = _db_path(repo_root)
    if not path.exists():
        return []
    bounded = max(1, min(int(limit), MAX_LOAD_LIMIT))
    conn = connect_readonly(path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM skill_records ORDER BY identity ASC, version ASC "
            "LIMIT ? OFFSET ?",
            (bounded, max(0, int(offset))),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    return [_row_to_record(row) for row in rows]


def load_registry(
    repo_root: str | Path,
    *,
    min_accepted_evidence: int = 2,
    limit: int = DEFAULT_LOAD_LIMIT,
) -> SkillRegistry:
    """Load persisted records into a :class:`SkillRegistry`.

    An absent or unreadable store yields an empty registry rather than an error,
    so the dashboard never fails on a repository that has no skills. A stored
    record whose digest is tampered still fails closed (via :func:`list_records`)
    -- it is dropped from no registry, it aborts the whole load.
    """
    registry = SkillRegistry(min_accepted_evidence=min_accepted_evidence)
    for record in list_records(repo_root, limit=limit):
        # These records were reconstructed and digest-verified on read; adopt them
        # through the public API rather than ``propose`` (which admits only
        # evidence-free proposed records and would reject a persisted
        # active/evidenced version).
        registry.adopt(record)
    return registry
