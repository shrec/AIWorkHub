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

import dataclasses
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
SELECTION_RECEIPT_SCHEMA_ID = "aiworkhub.skill_registry_store.selection_receipt.v1"
SKILLS_DB_REL = (".aiworkhub", "tasking", "skills.sqlite")

# Bounded reads: never materialize an unbounded registry from disk.
MAX_LOAD_LIMIT = 1000
DEFAULT_LOAD_LIMIT = MAX_LOAD_LIMIT
# A runtime packet is already capped at skill_registry.MAX_PACKET_SELECTED rows;
# this is the receipt table's own independent cap, so a malformed packet cannot
# write an unbounded row.
MAX_RECEIPT_SKILLS = 32
MAX_RECEIPT_ID_CHARS = 200
MAX_EMPTY_REASON_CHARS = 64
# A count is bounded for the same reason a receipt row list is: the column is
# a measurement, and a measurement larger than the receipt could hold is a bug
# upstream, not a number to persist.
MAX_RECEIPT_COUNT = 10_000

# Stamped on an empty receipt whose caller measured no reason. Distinct from the
# blank default, which means the row predates the measurement columns entirely:
# "recorded empty, reason unreported" and "never measured" are different facts
# and a coverage projection must not add them together.
SELECTION_EMPTY_REASON_NOT_REPORTED = "reason_not_reported"

# The bucket a persisted reason outside the closed set is counted under. Rows
# predate validation or come from a build whose vocabulary has since changed,
# and :func:`skill_coverage` GROUPS BY this column: without a bucket, one stale
# free-text value opens an unbounded key space in a projection whose whole
# promise is bounded counts. It is never written, only read.
SELECTION_EMPTY_REASON_UNRECOGNIZED = "reason_unrecognized"

# The closed set a receipt row's ``empty_reason`` may hold: the selection
# vocabulary this build knows, plus this module's own "recorded empty, reason
# unreported" token. Validated at WRITE time so the grouping key space stays
# closed at the source rather than being repaired on every read.
PERSISTABLE_EMPTY_REASONS: frozenset[str] = frozenset(
    skill_registry.SELECTION_EMPTY_REASONS | {SELECTION_EMPTY_REASON_NOT_REPORTED}
)

# How many of the newest receipts a coverage projection reads. Bounded by the
# same rule as every other read here: a projection reports counts and a streak,
# never an unbounded row set.
MAX_COVERAGE_RECEIPTS = 500


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
CREATE TABLE IF NOT EXISTS skill_selection_receipts (
    task_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    packet_sha256 TEXT NOT NULL,
    selected_json TEXT NOT NULL,
    context_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    selected_count INTEGER NOT NULL DEFAULT 0,
    injected_count INTEGER NOT NULL DEFAULT 0,
    empty_reason TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (task_id, request_id)
);
CREATE INDEX IF NOT EXISTS idx_skill_selection_receipts_task
    ON skill_selection_receipts(task_id);
"""

# Receipt columns added after the table shipped. Listed once, with the exact
# DDL each needs, so the additive upgrade in ``ensure_schema`` stays a loop over
# data rather than a growing block of near-identical ALTER statements.
_RECEIPT_ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("selected_count", "INTEGER NOT NULL DEFAULT 0"),
    ("injected_count", "INTEGER NOT NULL DEFAULT 0"),
    ("empty_reason", "TEXT NOT NULL DEFAULT ''"),
)


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
    """Create the canonical tables, index and added columns if they are absent.

    Additive only: existing rows are never rewritten, so an older database is
    upgraded in place without data loss. A pre-existing table that predates the
    ``state_digest`` column gains it as an empty-default column; such legacy rows
    then fail closed on read until rewritten, never serving unverified state.

    The receipt measurement columns (``selected_count``, ``injected_count``,
    ``empty_reason``) are added the same way. Their defaults are deliberately
    the zero/empty ones: a row written before the columns existed was never
    MEASURED for them, and :func:`skill_coverage` reports such rows as
    unmeasured rather than folding them into a real count of zero.
    """
    conn.executescript(_SCHEMA_SQL)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(skill_records)")}
    if "state_digest" not in columns:
        conn.execute(
            "ALTER TABLE skill_records ADD COLUMN state_digest TEXT NOT NULL DEFAULT ''"
        )
    receipt_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(skill_selection_receipts)")
    }
    for column, ddl in _RECEIPT_ADDED_COLUMNS:
        if column not in receipt_columns:
            conn.execute(
                f"ALTER TABLE skill_selection_receipts ADD COLUMN {column} {ddl}"
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


def _bounded_page(
    repo_root: str | Path, sql: str, *, limit: int, offset: int, strict: bool = False
) -> list[sqlite3.Row]:
    """The ONE owner of a bounded, fail-closed page read of the skills database.

    Both stored tables are paged the same way -- never create the database, cap
    the limit at :data:`MAX_LOAD_LIMIT`, open read-only, and answer an unreadable
    or missing table with an empty page rather than an error, so a repository
    that has no skills never fails a dashboard. Writing that twice would be a
    second implementation of one policy, and the two would drift on the cap.

    ``strict=True`` withdraws only that last clause, and only for a caller whose
    job is to MEASURE. An empty page is a safe answer for a reader that wants
    rows and a false one for :func:`skill_coverage`: a corrupt file or a store
    missing ``skill_records`` would be published as measured zeros with
    ``truncated`` false -- byte-identical to a healthy registry nobody has
    filled yet. A strict caller is expected to catch :class:`sqlite3.Error` and
    report the reading as unmeasured instead of inventing a number.

    ``sql`` must end in ``LIMIT ? OFFSET ?``; those are the only bound values.
    """
    path = _db_path(repo_root)
    if not path.exists():
        return []
    conn = connect_readonly(path)
    conn.row_factory = sqlite3.Row
    try:
        return list(
            conn.execute(
                sql, (max(1, min(int(limit), MAX_LOAD_LIMIT)), max(0, int(offset)))
            ).fetchall()
        )
    except sqlite3.Error:
        if strict:
            raise
        return []
    finally:
        conn.close()


def list_records(
    repo_root: str | Path,
    *,
    limit: int = DEFAULT_LOAD_LIMIT,
    offset: int = 0,
    strict: bool = False,
) -> list[SkillRecord]:
    """Return persisted records, bounded and fail-closed. Never creates the DB.

    ``strict`` is forwarded to :func:`_bounded_page`: a measuring caller wants an
    unreadable store to raise rather than look empty.
    """
    rows = _bounded_page(
        repo_root,
        "SELECT * FROM skill_records ORDER BY identity ASC, version ASC "
        "LIMIT ? OFFSET ?",
        limit=limit,
        offset=offset,
        strict=strict,
    )
    return [_row_to_record(row) for row in rows]


def stored_state_digest(
    repo_root: str | Path, identity: str, version: str
) -> str | None:
    """Return the ``state_digest`` column of one stored row, or ``None``.

    :func:`state_digest` recomputes the token from a record in memory, which is
    the right token only while the loaded record is byte-identical to the stored
    row. :func:`load_registry` may adopt a persisted ``active`` record whose own
    evidence does not support activation as ``proposed`` instead, so the record a
    caller holds is deliberately NOT the stored payload and its recomputed token
    would never match. This reads the token the row actually carries, so a
    compare-and-swap advance still refuses a genuine concurrent write while a
    demoted record stays advanceable.
    """
    path = _db_path(repo_root)
    if not path.exists():
        return None
    conn = connect_readonly(path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT state_digest FROM skill_records WHERE identity=? AND version=?",
            (identity, version),
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    if row is None:
        return None
    return str(row["state_digest"])


def activation_supported(record: SkillRecord, min_accepted_evidence: int = 2) -> bool:
    """Whether a record's OWN evidence still supports an ``active`` lifecycle.

    The stored digests are unkeyed and live beside the payload they cover, so
    they detect corruption and hand-editing, never a forged record (see this
    module's docstring). This check is a different question and one the digests
    cannot answer even in principle: does the evidence the record itself carries
    meet the activation rule that is in force NOW? A record activated under an
    earlier, weaker reading of independence answers no.
    """
    if record.lifecycle_state is not skill_registry.LifecycleState.ACTIVE:
        return True
    if skill_registry.unresolved_negative_evidence(record):
        return False
    count = skill_registry.independent_accepted_evidence_count(record)
    return count >= min_accepted_evidence


def audit_active_records(
    repo_root: str | Path,
    *,
    min_accepted_evidence: int = 2,
    limit: int = DEFAULT_LOAD_LIMIT,
) -> list[dict[str, Any]]:
    """Report every persisted ``active`` record against the activation rule.

    Read-only and non-mutating: it never rewrites a row, so an unverified
    activation is surfaced for a manager to act on rather than repaired behind
    their back. Each entry names the canonical actor identities the activation
    actually rests on, which is the fact a raw ``accepted_count`` hides.

    ``unreachable_dimensions`` is the second thing an activation can be wrong
    about, and the one nothing was asking. A record can rest on two genuinely
    independent accepted actors and still declare free-text prose where a
    vocabulary token belongs, which makes it ACTIVE, verified, and impossible
    for any card context to match. It is listed, never retired here: retirement
    is a manager transition through :meth:`SkillRegistry.retire` and
    :func:`advance_record`, and replacement is a new version through the same
    propose/evidence/activate path. This report names the targets; the canonical
    mechanisms move them.
    """
    report: list[dict[str, Any]] = []
    for record in list_records(repo_root, limit=limit):
        if record.lifecycle_state is not skill_registry.LifecycleState.ACTIVE:
            continue
        actors = skill_registry.independent_accepted_actor_ids(record)
        unreachable = skill_registry.unreachable_selection_dimensions(record)
        report.append(
            {
                "identity": record.identity,
                "version": record.version,
                "accepted_count": record.accepted_count,
                "independent_accepted_actors": len(actors),
                "actor_ids": list(actors),
                "raw_actor_ids": sorted(
                    {
                        item.actor_id
                        for item in record.evidence
                        if item.outcome is skill_registry.EvidenceOutcome.ACCEPTED
                    }
                ),
                "min_accepted_evidence": int(min_accepted_evidence),
                "verified": activation_supported(record, min_accepted_evidence),
                "unreachable_dimensions": list(unreachable),
                "injectable": skill_registry.is_injectable(record),
            }
        )
    return report


def load_registry(
    repo_root: str | Path,
    *,
    min_accepted_evidence: int = 2,
    limit: int = DEFAULT_LOAD_LIMIT,
    strict: bool = False,
    demote_unverified_active: bool = True,
) -> SkillRegistry:
    """Load persisted records into a :class:`SkillRegistry`.

    An absent or unreadable store yields an empty registry rather than an error,
    so the dashboard never fails on a repository that has no skills. A stored
    record whose digest is tampered still fails closed (via :func:`list_records`)
    -- it is dropped from no registry, it aborts the whole load.

    A stored ``active`` record whose own evidence does not meet the activation
    rule in force is adopted as ``proposed`` instead. It is loaded, not lost:
    every content field, evidence entry and counter is preserved and the content
    digest is unchanged, because ``lifecycle_state`` is runtime state and not a
    content field. Only its eligibility changes, and only in the safe direction
    -- :func:`skill_registry.select` serves ACTIVE records exclusively, so a
    record that self-certified under a weaker reading of independence stops
    being injected into worker context while it stays fully available for a
    manager to re-evaluate, re-evidence and legitimately re-activate. The stored
    row is NOT rewritten by this; use :func:`audit_active_records` to see it.

    ``demote_unverified_active=False`` loads the persisted lifecycle verbatim.
    It exists for audit and migration callers that must observe the stored state
    exactly as written, and must not be used to serve runtime selection.

    ``strict=True`` withdraws the "unreadable yields empty" clause of the first
    paragraph, so a caller measuring coverage sees the sqlite failure instead of
    an empty registry it would report as a real zero.
    """
    registry = SkillRegistry(min_accepted_evidence=min_accepted_evidence)
    for record in list_records(repo_root, limit=limit, strict=strict):
        if demote_unverified_active and not activation_supported(
            record, min_accepted_evidence
        ):
            record = dataclasses.replace(
                record, lifecycle_state=skill_registry.LifecycleState.PROPOSED
            )
        # These records were reconstructed and digest-verified on read; adopt them
        # through the public API rather than ``propose`` (which admits only
        # evidence-free proposed records and would reject a persisted
        # active/evidenced version).
        registry.adopt(record)
    return registry


# ---------------------------------------------------------------------------
# Skill SELECTION receipts -- which skills a card actually received.
#
# ``skill_registry.select`` + ``build_runtime_packet`` run at prompt-build time
# and the packet was handed to the worker and then dropped. Nothing on disk
# recorded which skills a card received, so ``skill_retirement_report`` reported
# ``cards_with_persisted_packet=0`` and ``injected_cards=0`` for 4,681 cards --
# every usage question about the skill system answered "unmeasurable", and the
# accept/reject evidence loop had no list of skills to attribute an outcome to.
#
# The receipt closes exactly that gap and nothing else: the identity+version
# list plus the packet's sha256, keyed by the card and the request that received
# it. It stores no procedure bodies (the packet rows carry instruction text; the
# receipt carries only the addresses), so it can never become a second, drifting
# copy of a skill.
# ---------------------------------------------------------------------------


def _bounded_id(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise SkillStoreError(f"{field}_required")
    if len(text) > MAX_RECEIPT_ID_CHARS or "\x00" in text:
        raise SkillStoreError(f"{field}_invalid")
    return text


def _bounded_count(value: Any) -> int:
    """Return a non-negative, bounded receipt count, or refuse."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise SkillStoreError("selection_count_invalid")
    if value < 0 or value > MAX_RECEIPT_COUNT:
        raise SkillStoreError("selection_count_invalid")
    return value



def _packet_rows(packet: Any) -> list[dict[str, str]]:
    """Reduce a packet, a selection receipt, or their mapping to address rows.

    Accepts whatever the selection site holds -- a
    :class:`skill_registry.SkillRuntimePacket`, a
    :class:`skill_registry.SkillSelectionReceipt`, or the ``as_mapping()`` form
    of either -- so the one call a launcher has to add never depends on which
    object happens to be in scope there.
    """
    if hasattr(packet, "as_mapping"):
        packet = packet.as_mapping()
    if not isinstance(packet, dict):
        raise SkillStoreError("selection_packet_invalid")
    raw = packet.get("skills")
    if raw is None:
        raw = packet.get("selected")
    if raw is None:
        raise SkillStoreError("selection_packet_invalid")
    if not isinstance(raw, (list, tuple)):
        raise SkillStoreError("selection_packet_invalid")
    if len(raw) > MAX_RECEIPT_SKILLS:
        raise SkillStoreError(f"selection_packet_too_large:{len(raw)}")
    rows: list[dict[str, str]] = []
    for item in raw:
        if hasattr(item, "as_mapping"):
            item = item.as_mapping()
        if not isinstance(item, dict):
            raise SkillStoreError("selection_packet_invalid")
        rows.append(
            {
                "identity": _bounded_id(item.get("identity"), "selection_identity"),
                "version": _bounded_id(item.get("version"), "selection_version"),
                "digest": str(item.get("digest") or "")[:128],
            }
        )
    rows.sort(key=lambda row: (row["identity"], row["version"]))
    return rows


def selection_packet_sha256(packet: Any) -> str:
    """The sha256 of one runtime packet, over its canonical JSON bytes.

    Computed from the packet as a whole -- instruction text included -- so two
    cards that received the same skill VERSIONS but different procedure bodies
    (a record advanced between the two launches) are distinguishable.
    """
    if hasattr(packet, "as_mapping"):
        packet = packet.as_mapping()
    if not isinstance(packet, dict):
        raise SkillStoreError("selection_packet_invalid")
    canonical = json.dumps(packet, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def record_selection(
    repo_root: str | Path,
    *,
    task_id: str,
    request_id: str = "",
    packet: Any,
    context: Any = None,
    selected_count: int | None = None,
    empty_reason: str = "",
) -> dict[str, Any]:
    """Persist which skills one card+request received, and the packet's sha.

    Idempotent by ``(task_id, request_id)``: re-recording the identical
    measurement is a no-op that reports ``idempotent``. A DIFFERENT packet --
    or the same packet with a different measurement -- replaces the row and
    reports ``replaced``. A relaunch of the same request genuinely rebuilds the
    packet, and silently keeping the stale list would attribute the decision's
    evidence to skills the worker never saw.

    An empty packet is recorded too. "This card declared selection vocabulary
    and matched nothing" is a measurement; leaving no row would make it
    indistinguishable from "selection never ran", which is the exact confusion
    the injection ledger already reports as unavailable.

    ``selected_count`` and ``empty_reason`` are what turn that row from a
    tombstone into evidence. SELECTED and INJECTED are two different numbers --
    :func:`skill_registry.select` decides the first, ``build_runtime_packet``
    bounds the second -- and recording only the packet made a selection that was
    truncated to fit look exactly like a selection that found nothing.
    ``selected_count`` defaults to the injected count, which is correct for the
    common unbounded case and never invents a number the caller did not measure.

    ``empty_reason`` carries one :data:`skill_registry.SELECTION_EMPTY_REASONS`
    token when the packet is empty. An empty packet recorded WITHOUT one is
    stamped :data:`SELECTION_EMPTY_REASON_NOT_REPORTED` rather than left blank,
    so a blank reason keeps its one meaning: this row predates the measurement.
    A token outside :data:`PERSISTABLE_EMPTY_REASONS` is REFUSED rather than
    truncated and stored: :func:`skill_coverage` groups its empty tally by this
    column, so admitting free text would let one caller turn a bounded reason
    histogram into an open-ended one. :func:`record_selection_reported` reports
    that refusal like any other, so a launcher still never raises on it.
    """
    task = _bounded_id(task_id, "task_id")
    request = str(request_id or "").strip()[:MAX_RECEIPT_ID_CHARS]
    rows = _packet_rows(packet)
    packet_sha = selection_packet_sha256(packet)
    selected_json = json.dumps(rows, sort_keys=True, separators=(",", ":"))
    injected = len(rows)
    selected = injected if selected_count is None else _bounded_count(selected_count)
    reason = str(empty_reason or "").strip()[:MAX_EMPTY_REASON_CHARS]
    if reason and reason not in PERSISTABLE_EMPTY_REASONS:
        raise SkillStoreError(
            "empty_reason must be one of "
            f"{sorted(PERSISTABLE_EMPTY_REASONS)}; got {reason!r}"
        )
    if not injected and not reason:
        reason = SELECTION_EMPTY_REASON_NOT_REPORTED
    if injected:
        # A packet that carried skills is not empty, whatever the caller said.
        reason = ""
    if context is not None and hasattr(context, "as_mapping"):
        context = context.as_mapping()
    context_json = json.dumps(
        context if isinstance(context, dict) else {},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )[:4096]
    conn = _connect(repo_root)
    try:
        ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT packet_sha256,selected_count,injected_count,empty_reason "
            "FROM skill_selection_receipts WHERE task_id=? AND request_id=?",
            (task, request),
        ).fetchone()
        idempotent = existing is not None and (
            existing["packet_sha256"],
            int(existing["selected_count"]),
            int(existing["injected_count"]),
            str(existing["empty_reason"] or ""),
        ) == (packet_sha, selected, injected, reason)
        if not idempotent:
            conn.execute(
                "INSERT OR REPLACE INTO skill_selection_receipts "
                "(task_id,request_id,packet_sha256,selected_json,context_json,created_at,"
                "selected_count,injected_count,empty_reason) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    task, request, packet_sha, selected_json, context_json, _utcnow(),
                    selected, injected, reason,
                ),
            )
        conn.commit()
        return {
            "schema_id": SELECTION_RECEIPT_SCHEMA_ID,
            "task_id": task,
            "request_id": request,
            "packet_sha256": packet_sha,
            "skills": rows,
            "selected_count": selected,
            "injected_count": injected,
            "empty_reason": reason,
            "idempotent": bool(idempotent),
            "replaced": bool(existing is not None and not idempotent),
        }
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def record_selection_reported(
    repo_root: str | Path,
    *,
    task_id: str,
    request_id: str = "",
    packet: Any,
    context: Any = None,
    selected_count: int | None = None,
    empty_reason: str = "",
) -> dict[str, Any]:
    """:func:`record_selection` that REPORTS its refusal instead of raising.

    The one call a launcher adds sits on the prompt-build path, where a failure
    to record a receipt must not take the worker's context bundle down with it.
    This is not a silent fallback: nothing is swallowed. A refusal comes back as
    ``{"ok": False, "reason": ...}`` naming the failing type, so the caller can
    record it exactly as it records every other degraded surface.
    """
    try:
        return {"ok": True, **record_selection(
            repo_root, task_id=task_id, request_id=request_id,
            packet=packet, context=context,
            selected_count=selected_count, empty_reason=empty_reason,
        )}
    except (SkillStoreError, sqlite3.Error, OSError, TypeError, ValueError) as exc:
        return {
            "ok": False,
            "reason": f"skill_selection_receipt_not_recorded:{type(exc).__name__}",
            "detail": str(exc)[:200],
        }


def _receipt_row(row: sqlite3.Row) -> dict[str, Any]:
    try:
        skills = json.loads(str(row["selected_json"] or "[]"))
    except (TypeError, ValueError):
        skills = []
    try:
        context = json.loads(str(row["context_json"] or "{}"))
    except (TypeError, ValueError):
        context = {}
    # The measurement columns are read defensively, not because a write path
    # could omit them, but because a READ path can: connect_readonly cannot run
    # the additive ALTER, so a database not yet opened read-write still serves
    # its pre-measurement shape. It reads as unmeasured, never as a zero.
    columns = set(row.keys())
    measured = "injected_count" in columns and "empty_reason" in columns
    injected = int(row["injected_count"]) if measured else 0
    selected = (
        int(row["selected_count"]) if "selected_count" in columns else injected
    )
    reason = str(row["empty_reason"] or "") if measured else ""
    return {
        "schema_id": SELECTION_RECEIPT_SCHEMA_ID,
        "task_id": str(row["task_id"]),
        "request_id": str(row["request_id"]),
        "packet_sha256": str(row["packet_sha256"]),
        "skills": skills if isinstance(skills, list) else [],
        "context": context if isinstance(context, dict) else {},
        "created_at": str(row["created_at"]),
        "selected_count": selected,
        "injected_count": injected,
        "empty_reason": reason,
        # Blank reason on an empty receipt is the ONE signal that separates a
        # row written before these columns existed from a measured empty one.
        "measured": bool(measured and (injected or reason)),
    }


def get_selection(
    repo_root: str | Path, task_id: str, request_id: str = ""
) -> dict[str, Any] | None:
    """Return one card's selection receipt, or ``None``. Never creates the DB.

    An exact ``(task_id, request_id)`` match wins. With no request id, or when
    that exact request has no row, the card's most recent receipt is returned,
    because a decision is adjudicated against the request that ran and the
    launcher may have recorded the packet before the request id was minted.
    """
    path = _db_path(repo_root)
    if not path.exists():
        return None
    conn = connect_readonly(path)
    conn.row_factory = sqlite3.Row
    try:
        request = str(request_id or "").strip()
        row = None
        if request:
            row = conn.execute(
                "SELECT * FROM skill_selection_receipts WHERE task_id=? AND request_id=?",
                (str(task_id), request),
            ).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT * FROM skill_selection_receipts WHERE task_id=? "
                "ORDER BY created_at DESC, request_id DESC LIMIT 1",
                (str(task_id),),
            ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return None if row is None else _receipt_row(row)


def list_selections(
    repo_root: str | Path,
    *,
    limit: int = DEFAULT_LOAD_LIMIT,
    offset: int = 0,
    strict: bool = False,
) -> list[dict[str, Any]]:
    """Return persisted selection receipts, bounded. Never creates the DB.

    ``strict`` is forwarded to :func:`_bounded_page` for measuring callers.
    """
    rows = _bounded_page(
        repo_root,
        "SELECT * FROM skill_selection_receipts "
        "ORDER BY created_at DESC, task_id ASC LIMIT ? OFFSET ?",
        limit=limit,
        strict=strict,
        offset=offset,
    )
    return [_receipt_row(row) for row in rows]


def injection_counts(
    repo_root: str | Path, *, limit: int = DEFAULT_LOAD_LIMIT
) -> dict[str, dict[str, Any]]:
    """Count, per ``identity@version``, the cards and requests it was injected into.

    This is the injection denominator ``skill_miner.measure_retirement`` refused
    to invent. It is now a count of RECORDED receipts, never a replay of
    selection over history: a card with no receipt contributes nothing rather
    than a guess.
    """
    counts: dict[str, dict[str, Any]] = {}
    for receipt in list_selections(repo_root, limit=limit):
        for row in receipt["skills"]:
            if not isinstance(row, dict):
                continue
            key = f"{row.get('identity')}@{row.get('version')}"
            entry = counts.setdefault(key, {"cards": set(), "requests": set()})
            entry["cards"].add(receipt["task_id"])
            if receipt["request_id"]:
                entry["requests"].add(receipt["request_id"])
    return {
        key: {
            "injected_cards": len(entry["cards"]),
            "injected_requests": len(entry["requests"]),
            "card_ids": sorted(entry["cards"])[:40],
        }
        for key, entry in counts.items()
    }


# ---------------------------------------------------------------------------
# Measured coverage -- is the skill system actually reaching workers?
#
# Every number below is counted from rows this repository wrote. When the store
# is absent nothing is counted and ``measured`` is False: an unavailable skill
# surface reported as a row of zeros is the exact confusion that let 24 of 24
# empty selections read as a healthy, quiet system.
# ---------------------------------------------------------------------------

COVERAGE_SCHEMA_ID = "aiworkhub.skill_registry_store.coverage.v1"


def unmeasured_coverage(reason: str) -> dict[str, Any]:
    """The one shape an unavailable coverage reading takes. Never zeros.

    Public because the reading can also fail ABOVE this module -- a dashboard
    view that cannot even reach :func:`skill_coverage` must answer in the same
    shape, or a caller has to branch on which layer failed to learn whether the
    skill surface was measured.
    """
    return {
        "schema_id": COVERAGE_SCHEMA_ID,
        "measured": False,
        "unavailable_reason": reason,
        "skills": {},
        "selection": {},
    }


def _coverage_empty_reason(value: Any) -> str:
    """Return the closed-vocabulary bucket one persisted reason counts under."""
    reason = str(value or "") or SELECTION_EMPTY_REASON_NOT_REPORTED
    if reason not in PERSISTABLE_EMPTY_REASONS:
        return SELECTION_EMPTY_REASON_UNRECOGNIZED
    return reason


def _more_rows_beyond(
    repo_root: str | Path, sql: str, *, offset: int, strict: bool = False
) -> bool:
    """Whether one bounded page left rows behind it. One row is read, never more."""
    return bool(
        _bounded_page(repo_root, sql, limit=1, offset=offset, strict=strict)
    )


def skill_coverage(
    repo_root: str | Path,
    *,
    limit: int = MAX_COVERAGE_RECEIPTS,
    record_limit: int = MAX_LOAD_LIMIT,
    min_accepted_evidence: int = 2,
) -> dict[str, Any]:
    """Return bounded, measured skill selection/injection coverage.

    Four numbers and a streak, no rows:

    * ``skills.total`` / ``skills.by_lifecycle`` -- what the registry holds.
    * ``skills.injectable`` -- ACTIVE records that some card context could
      actually reach (:func:`skill_registry.is_injectable`). A record declaring
      free-text prose where a vocabulary token belongs is ACTIVE and unreachable,
      so counting ACTIVE alone reports readiness the matcher cannot deliver.
    * ``selection.selection_count`` / ``selection.injection_count`` -- receipts
      that SELECTED something and receipts that INJECTED something. They are two
      numbers because a bounded packet can carry fewer skills than select chose.
    * ``selection.consecutive_empty_streak`` -- how many of the newest receipts
      in a row injected nothing, with ``empty_reasons`` naming why. This is the
      number that refuses to stay quiet: a long streak is a broken chain, not a
      calm one.

    Every population read here is bounded, and each block DISCLOSES its own
    bound: ``skills.record_limit`` / ``skills.truncated`` and
    ``selection.receipt_limit`` / ``selection.truncated``. A headline total
    silently computed from an identity-ordered clamp is the same defect this
    projection exists to name -- a number that looks like the whole registry
    and is really the first page of it. ``truncated`` is measured, by reading
    ONE row past the page, not inferred from the page being full.

    ``empty_reasons`` is keyed by :data:`PERSISTABLE_EMPTY_REASONS` plus
    :data:`SELECTION_EMPTY_REASON_UNRECOGNIZED`. Writes are validated, so the
    bucket catches only rows an older build persisted.

    Three store states are three different answers, never one. An ABSENT store
    is ``skill_store_absent``. A store that exists but cannot be read -- corrupt
    bytes, or a file missing the tables this projection groups over -- is
    ``skill_store_unreadable:<ExceptionName>``. Only a store that opened and
    answered is ``measured``, and only then may it report zeros. The middle case
    is the one worth naming: it fails exactly where a healthy empty registry
    succeeds, so a projection that reads it leniently publishes a broken store
    as a calm one.

    Read-only. Never creates the database and never rewrites a row. The registry
    is loaded with the demotion rule in force, so ``injectable`` counts what
    selection would really serve rather than what the rows claim.
    """
    path = _db_path(repo_root)
    if not path.exists():
        return unmeasured_coverage("skill_store_absent")
    record_page = max(1, min(int(record_limit), MAX_LOAD_LIMIT))
    receipt_page = max(1, min(int(limit), MAX_COVERAGE_RECEIPTS))
    # Every read below is strict. The page reader's default is to answer an
    # unreadable store with an empty page, which is right for a caller that
    # wants rows and fatal for this one: a corrupt file or a store missing
    # skill_records would arrive here as zero records, zero receipts and
    # truncated=False, and be published as a measured, healthy, empty registry.
    try:
        records = load_registry(
            repo_root,
            min_accepted_evidence=min_accepted_evidence,
            limit=record_page,
            strict=True,
        ).records()
        receipts = list_selections(repo_root, limit=receipt_page, strict=True)
        records_truncated = _more_rows_beyond(
            repo_root,
            "SELECT identity FROM skill_records "
            "ORDER BY identity ASC, version ASC LIMIT ? OFFSET ?",
            offset=record_page,
            strict=True,
        )
        receipts_truncated = _more_rows_beyond(
            repo_root,
            "SELECT task_id FROM skill_selection_receipts "
            "ORDER BY created_at DESC, task_id ASC LIMIT ? OFFSET ?",
            offset=receipt_page,
            strict=True,
        )
    except (SkillStoreError, sqlite3.Error, OSError, ValueError) as exc:
        return unmeasured_coverage(f"skill_store_unreadable:{type(exc).__name__}")

    by_lifecycle: dict[str, int] = {}
    injectable = 0
    active_unreachable = 0
    for record in records:
        state = record.lifecycle_state.value
        by_lifecycle[state] = by_lifecycle.get(state, 0) + 1
        if record.lifecycle_state is not skill_registry.LifecycleState.ACTIVE:
            continue
        if skill_registry.is_injectable(record):
            injectable += 1
        else:
            active_unreachable += 1

    measured_receipts = [row for row in receipts if row["measured"]]
    empty_reasons: dict[str, int] = {}
    selection_count = 0
    injection_count = 0
    for row in measured_receipts:
        if row["selected_count"]:
            selection_count += 1
        if row["injected_count"]:
            injection_count += 1
        else:
            reason = _coverage_empty_reason(row["empty_reason"])
            empty_reasons[reason] = empty_reasons.get(reason, 0) + 1
    # list_selections orders newest first, so the streak is the leading run.
    streak = 0
    for row in measured_receipts:
        if row["injected_count"]:
            break
        streak += 1

    return {
        "schema_id": COVERAGE_SCHEMA_ID,
        "measured": True,
        "unavailable_reason": "",
        "skills": {
            "total": len(records),
            "by_lifecycle": dict(sorted(by_lifecycle.items())),
            "active": by_lifecycle.get(
                skill_registry.LifecycleState.ACTIVE.value, 0
            ),
            "injectable": injectable,
            "active_unreachable_vocabulary": active_unreachable,
            # The population these totals were counted over, and whether the
            # registry held more than it. Without both, "total" reads as the
            # registry when it is only the first identity-ordered page of it.
            "record_limit": record_page,
            "truncated": records_truncated,
        },
        "selection": {
            "receipts": len(measured_receipts),
            "unmeasured_receipts": len(receipts) - len(measured_receipts),
            "selection_count": selection_count,
            "injection_count": injection_count,
            "empty_receipts": len(measured_receipts) - injection_count,
            "consecutive_empty_streak": streak,
            "all_empty": bool(measured_receipts) and injection_count == 0,
            "empty_reasons": dict(sorted(empty_reasons.items())),
            # The EFFECTIVE bound this call used, not the module ceiling: a
            # caller that asked for 10 receipts was never told it got 10.
            "receipt_limit": receipt_page,
            "truncated": receipts_truncated,
        },
    }
