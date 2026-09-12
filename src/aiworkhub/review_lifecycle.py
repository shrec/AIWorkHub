"""Authenticated review-chain and action-outbox storage.

This module owns only durable SQLite state. It creates canonical review action
descriptors, reserves them transactionally, and completes them idempotently.
Process launch and other external effects deliberately live elsewhere.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from . import db_writer
from .sqlite_readonly import connect_readonly


SCHEMA_ID = "aiworkhub.review_lifecycle.v1"
DESCRIPTOR_SCHEMA_ID = "aiworkhub.review_action_descriptor.v1"
HEX64 = re.compile(r"^[0-9a-f]{64}$")
VALID_STATES = {"pending", "reserved", "completed", "failed", "retired"}
RETIRED_REASON = "dependency_retired"
ACTION_PREIMAGE_COLUMNS: tuple[str, ...] = (
    "chain_id",
    "action_index",
    "phase",
    "action_type",
    "lens",
    "target_task_id",
    "target_request_id",
    "claim_epoch",
    "descriptor_json",
    "descriptor_sha256",
    "state",
    "owner",
    "lease_token",
    "lease_expires_at",
    "receipt_json",
    "receipt_sha256",
    "receipt_commitment_sha256",
    "completed_at",
    "failure_reason",
    "retired_due_to_action_id",
    "created_at",
    "updated_at",
)

PLAN: tuple[tuple[int, str, str, str], ...] = (
    (0, "correctness", "launch", "correctness"),
    (1, "correctness", "accept", "correctness"),
    (2, "correctness", "archive", "correctness"),
    (3, "security", "launch", "security"),
    (4, "security", "accept", "security"),
    (5, "security", "archive", "security"),
    (6, "code_quality", "launch", "code_quality"),
    (7, "code_quality", "accept", "code_quality"),
    (8, "code_quality", "archive", "code_quality"),
    # The action type is retained for descriptor compatibility with persisted
    # chains.  Its effect is now an authenticated manager-ready boundary; it
    # never accepts the implementation target on the manager's behalf.
    (9, "target", "target_accept", ""),
    (10, "target", "target_archive", ""),
    (11, "needfix", "needfix_close", ""),
)

MANAGER_READY_SCHEMA_ID = "aiworkhub.manager_ready_receipt.v1"
MANAGER_READY_ACTION_TYPES = frozenset({"target_accept"})

SCHEMA = """
CREATE TABLE IF NOT EXISTS review_chains (
  chain_id INTEGER PRIMARY KEY AUTOINCREMENT,
  target_task_id TEXT NOT NULL,
  target_request_id TEXT NOT NULL,
  claim_epoch TEXT NOT NULL,
  packet_sha256 TEXT NOT NULL,
  candidate_sha256 TEXT NOT NULL,
  chain_identity_json TEXT NOT NULL,
  chain_identity_sha256 TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(target_task_id, target_request_id, claim_epoch)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_review_chains_identity
  ON review_chains(chain_identity_sha256);

CREATE TABLE IF NOT EXISTS review_action_outbox (
  action_id INTEGER PRIMARY KEY AUTOINCREMENT,
  chain_id INTEGER NOT NULL,
  action_index INTEGER NOT NULL,
  phase TEXT NOT NULL,
  action_type TEXT NOT NULL,
  lens TEXT NOT NULL DEFAULT '',
  target_task_id TEXT NOT NULL,
  target_request_id TEXT NOT NULL,
  claim_epoch TEXT NOT NULL,
  descriptor_json TEXT NOT NULL,
  descriptor_sha256 TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'pending',
  owner TEXT NOT NULL DEFAULT '',
  lease_token TEXT NOT NULL DEFAULT '',
  lease_expires_at TEXT NOT NULL DEFAULT '',
  receipt_json TEXT NOT NULL DEFAULT '',
  receipt_sha256 TEXT NOT NULL DEFAULT '',
  receipt_commitment_sha256 TEXT NOT NULL DEFAULT '',
  completed_at TEXT NOT NULL DEFAULT '',
  failure_reason TEXT NOT NULL DEFAULT '',
  retired_due_to_action_id TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(chain_id) REFERENCES review_chains(chain_id),
  UNIQUE(chain_id, action_index)
);
CREATE INDEX IF NOT EXISTS idx_review_action_outbox_state
  ON review_action_outbox(state, action_id);
CREATE INDEX IF NOT EXISTS idx_review_action_outbox_state_lease
  ON review_action_outbox(state, lease_expires_at, action_id);

CREATE TABLE IF NOT EXISTS review_reconciliation_state (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  last_failed_action_id INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS review_reservation_state (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  last_pending_action_id INTEGER NOT NULL DEFAULT 0
);

CREATE TRIGGER IF NOT EXISTS trg_review_action_completed_no_update
BEFORE UPDATE ON review_action_outbox
WHEN OLD.state = 'completed'
BEGIN
  SELECT RAISE(ABORT, 'completed_action_immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_review_action_completed_no_delete
BEFORE DELETE ON review_action_outbox
WHEN OLD.state = 'completed'
BEGIN
  SELECT RAISE(ABORT, 'completed_action_immutable');
END;
"""


class ReviewLifecycleError(RuntimeError):
    """Typed fail-closed storage error."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class ReviewAction:
    action_id: int
    chain_id: int
    action_index: int
    phase: str
    action_type: str
    lens: str
    descriptor: dict[str, Any]
    descriptor_sha256: str


@dataclass(frozen=True, slots=True)
class ReviewChain:
    chain_id: int
    chain_identity_sha256: str
    chain_identity: dict[str, str]
    actions: tuple[ReviewAction, ...]


def ensure_schema(conn: sqlite3.Connection) -> bool:
    before_tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    before_triggers = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
    }
    conn.executescript(SCHEMA)
    after_tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    after_triggers = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
    }
    changed = before_tables != after_tables or before_triggers != after_triggers
    action_columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(review_action_outbox)").fetchall()
    }
    if "receipt_commitment_sha256" not in action_columns:
        conn.execute(
            "ALTER TABLE review_action_outbox "
            "ADD COLUMN receipt_commitment_sha256 TEXT NOT NULL DEFAULT ''"
        )
        changed = True
    if "retired_due_to_action_id" not in action_columns:
        conn.execute(
            "ALTER TABLE review_action_outbox "
            "ADD COLUMN retired_due_to_action_id TEXT NOT NULL DEFAULT ''"
        )
        changed = True
    chain_columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(review_chains)").fetchall()
    }
    if "contract_identity_sha256" not in chain_columns:
        # Deliberately NOT part of ``chain_identity``: that object is hashed
        # into every stored chain and action descriptor, and adding a field to
        # it would invalidate all 627 existing rows on the next verification.
        # It is a separate authenticated column, and an empty one means
        # "unknown", which can never match and therefore can never replay.
        conn.execute(
            "ALTER TABLE review_chains "
            "ADD COLUMN contract_identity_sha256 TEXT NOT NULL DEFAULT ''"
        )
        changed = True
    reservation_columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(review_reservation_state)").fetchall()
    }
    if "round_high_watermark" not in reservation_columns:
        conn.execute(
            "ALTER TABLE review_reservation_state "
            "ADD COLUMN round_high_watermark INTEGER NOT NULL DEFAULT 0"
        )
        changed = True
    conn.execute(
        "INSERT OR IGNORE INTO review_reconciliation_state (id, last_failed_action_id) "
        "VALUES (1, 0)"
    )
    conn.execute(
        "INSERT OR IGNORE INTO review_reservation_state (id, last_pending_action_id) "
        "VALUES (1, 0)"
    )
    return changed


def create_or_replay_chain(
    db_path: str | Path,
    *,
    target_task_id: str,
    target_request_id: str,
    claim_epoch: str | int,
    packet_sha256: str,
    candidate_sha256: str,
    now: datetime | None = None,
    contract_identity_sha256: str = "",
) -> ReviewChain:
    packet = _canonical_sha256(packet_sha256, "packet_sha256")
    candidate = _canonical_sha256(candidate_sha256, "candidate_sha256")
    contract_identity = str(contract_identity_sha256 or "")
    if contract_identity and not HEX64.fullmatch(contract_identity):
        raise ReviewLifecycleError("contract_identity_sha256_invalid")
    identity = _chain_identity(
        target_task_id=target_task_id,
        target_request_id=target_request_id,
        claim_epoch=claim_epoch,
        packet_sha256=packet,
        candidate_sha256=candidate,
    )
    created_at = _format_utc(now or datetime.now(timezone.utc))
    conn = _connect(db_path)
    try:
        ensure_schema(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM review_chains WHERE target_task_id=? "
            "AND target_request_id=? AND claim_epoch=?",
            (identity["target_task_id"], identity["target_request_id"], identity["claim_epoch"]),
        ).fetchone()
        if row is not None:
            chain = _hydrate_chain(conn, row)
            if chain.chain_identity != identity:
                raise ReviewLifecycleError("chain_identity_conflict")
            _verify_chain_actions(conn, chain.chain_id, identity, chain.chain_identity_sha256)
            if contract_identity and not str(row["contract_identity_sha256"] or ""):
                # Bind a contract identity only into an empty column, and never
                # over one already recorded: the retained value is what a replay
                # decision was measured against.
                conn.execute(
                    "UPDATE review_chains SET contract_identity_sha256=? "
                    "WHERE chain_id=? AND contract_identity_sha256=''",
                    (contract_identity, chain.chain_id),
                )
            conn.commit()
            return _hydrate_chain_by_id(conn, chain.chain_id)
        identity_json, identity_sha = _canonical_json_sha(identity)
        cursor = conn.execute(
            "INSERT INTO review_chains("
            "target_task_id,target_request_id,claim_epoch,packet_sha256,candidate_sha256,"
            "chain_identity_json,chain_identity_sha256,created_at,contract_identity_sha256"
            ") VALUES(?,?,?,?,?,?,?,?,?)",
            (
                identity["target_task_id"],
                identity["target_request_id"],
                identity["claim_epoch"],
                packet,
                candidate,
                identity_json,
                identity_sha,
                created_at,
                contract_identity,
            ),
        )
        if cursor.rowcount != 1:
            raise ReviewLifecycleError("cas_lost")
        lastrowid = cursor.lastrowid
        if lastrowid is None:
            raise ReviewLifecycleError("cas_lost")
        chain_id = int(lastrowid)
        for action_index, phase, action_type, lens in PLAN:
            descriptor = _descriptor(
                identity=identity,
                identity_sha256=identity_sha,
                phase=phase,
                action_type=action_type,
                lens=lens,
                action_index=action_index,
            )
            descriptor_json, descriptor_sha = _canonical_json_sha(descriptor)
            cursor = conn.execute(
                "INSERT INTO review_action_outbox("
                "chain_id,action_index,phase,action_type,lens,target_task_id,"
                "target_request_id,claim_epoch,descriptor_json,descriptor_sha256,"
                "state,created_at,updated_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    chain_id,
                    action_index,
                    phase,
                    action_type,
                    lens,
                    identity["target_task_id"],
                    identity["target_request_id"],
                    identity["claim_epoch"],
                    descriptor_json,
                    descriptor_sha,
                    "pending",
                    created_at,
                    created_at,
                ),
            )
            if cursor.rowcount != 1:
                raise ReviewLifecycleError("cas_lost")
        _verify_chain_actions(conn, chain_id, identity, identity_sha)
        conn.commit()
        return _hydrate_chain_by_id(conn, chain_id)
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise ReviewLifecycleError("chain_identity_conflict") from exc
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


RESERVE_SCAN_LIMIT = 256


def reserve_next_action(
    db_path: str | Path,
    *,
    owner: str,
    lease_token: str,
    now: datetime,
    lease_seconds: int = 300,
) -> ReviewAction | None:
    if not owner or not lease_token:
        raise ReviewLifecycleError("invalid_owner")
    now_text = _format_utc(now)
    expires_text = _format_utc(now + timedelta(seconds=max(1, int(lease_seconds))))
    conn = _connect(db_path)
    try:
        ensure_schema(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        row = _reservable_candidate(conn, now, reserved_only=True)
        if row is None:
            row = _reservable_candidate(conn, now, reserved_only=False)
        if row is None:
            conn.commit()
            return None
        cursor = conn.execute(
            "UPDATE review_action_outbox SET state='reserved', owner=?, "
            "lease_token=?, lease_expires_at=?, updated_at=? "
            f"WHERE action_id=? AND {_preimage_where_clause(row)}",
            (
                owner,
                lease_token,
                expires_text,
                now_text,
                row["action_id"],
                *_preimage_values(row),
            ),
        )
        if cursor.rowcount != 1:
            raise ReviewLifecycleError("cas_lost")
        conn.commit()
        return _action_from_row(
            conn.execute(
                "SELECT * FROM review_action_outbox WHERE action_id=?",
                (row["action_id"],),
            ).fetchone()
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _reservable_candidate(
    conn: sqlite3.Connection, now: datetime, *, reserved_only: bool
) -> sqlite3.Row | None:
    """Bounded, indexed scan of exactly one state bucket for one candidate.

    Scanning ``reserved`` and ``pending`` through separate indexed queries --
    instead of one scan ordered by ``action_id`` -- keeps an expired lease
    reachable no matter how many pending rows precede it in insertion order: a
    single scan bounded by ``LIMIT`` could fill its whole window with
    unreservable pending rows and never reach the reserved one.

    The reserved bucket itself never scans in ``action_id`` order either. It
    queries ``idx_review_action_outbox_state_lease`` directly for rows whose
    lease has already expired (``lease_expires_at<=now``), ordered by
    ``lease_expires_at`` -- the same composite key the index stores. Live,
    unexpired reservations sort after the query's upper bound and are never
    fetched, no matter how many of them exist or how low their ``action_id``
    is: more than 256 earlier unexpired reservations cannot fill this
    window and hide a later expired one, because they are outside the range
    the index scan ever visits.

    The pending bucket scans one bounded *round* at a time: a persistent
    keyset cursor (``review_reservation_state.last_pending_action_id``) paired
    with a persistent high-water mark (``round_high_watermark``) snapshotted
    once at the start of each round. Every query in a round is clamped to
    ``action_id <= round_high_watermark``, so rows that arrive mid-round never
    extend it. Without that upper bound, a cursor that only advances forward
    and only wraps once a forward scan comes back completely empty is starved
    by sustained arrivals: a steady stream of ever-newer pending rows keeps
    the forward scan non-empty forever, so it never notices the round is done
    and a row `defer_action` reset to a low ``action_id`` is skipped on every
    call, permanently. Clamping the round means it always completes -- and
    wraps to a fresh round starting back at ``action_id`` 0, where the reset
    row sorts first -- within a bounded number of calls set by the backlog
    size at the round's start, independent of how many new rows keep arriving.
    """
    if reserved_only:
        rows = conn.execute(
            "SELECT * FROM review_action_outbox WHERE state='reserved' "
            "AND lease_expires_at<=? ORDER BY lease_expires_at, action_id LIMIT ?",
            (_format_utc(now), RESERVE_SCAN_LIMIT),
        ).fetchall()
    else:
        cursor, watermark = _reservation_cursor(conn)
        if watermark == 0:
            watermark = _pending_high_watermark(conn)
            cursor = 0
        rows = (
            conn.execute(
                "SELECT * FROM review_action_outbox WHERE state='pending' "
                "AND action_id > ? AND action_id <= ? ORDER BY action_id LIMIT ?",
                (cursor, watermark, RESERVE_SCAN_LIMIT),
            ).fetchall()
            if watermark
            else []
        )
        if not rows:
            cursor = 0
            watermark = _pending_high_watermark(conn)
            rows = (
                conn.execute(
                    "SELECT * FROM review_action_outbox WHERE state='pending' "
                    "AND action_id > 0 AND action_id <= ? ORDER BY action_id LIMIT ?",
                    (watermark, RESERVE_SCAN_LIMIT),
                ).fetchall()
                if watermark
                else []
            )
        next_cursor = max((int(r["action_id"]) for r in rows), default=cursor)
        _set_reservation_cursor(conn, next_cursor, watermark)
    for row in rows:
        chain_row = conn.execute(
            "SELECT * FROM review_chains WHERE chain_id=?", (row["chain_id"],)
        ).fetchone()
        if chain_row is None:
            raise ReviewLifecycleError("descriptor_tamper")
        identity = _verify_chain_row(chain_row)
        _verify_action_row(conn, row, identity, chain_row["chain_identity_sha256"])
        state = str(row["state"])
        if state == "reserved":
            lease_expires_at = _parse_utc(str(row["lease_expires_at"]), "lease_expires_at")
            if lease_expires_at > now:
                continue
        elif state != "pending":
            continue
        if not _prior_actions_completed(conn, row, identity, chain_row["chain_identity_sha256"]):
            continue
        return row
    return None


def complete_action(
    db_path: str | Path,
    *,
    action_id: int,
    owner: str,
    lease_token: str,
    receipt: Mapping[str, Any],
    now: datetime,
) -> bool:
    receipt_json, receipt_sha = _canonical_json_sha(dict(receipt))
    now_text = _format_utc(now)
    conn = _connect(db_path)
    try:
        ensure_schema(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM review_action_outbox WHERE action_id=?", (int(action_id),)
        ).fetchone()
        if row is None:
            raise ReviewLifecycleError("action_missing")
        chain_row = conn.execute(
            "SELECT * FROM review_chains WHERE chain_id=?", (row["chain_id"],)
        ).fetchone()
        if chain_row is None:
            raise ReviewLifecycleError("descriptor_tamper")
        identity = _verify_chain_row(chain_row)
        _verify_chain_actions(
            conn,
            int(chain_row["chain_id"]),
            identity,
            str(chain_row["chain_identity_sha256"]),
        )
        _verify_action_row(conn, row, identity, chain_row["chain_identity_sha256"])
        state = str(row["state"])
        if state == "completed":
            if (
                row["owner"] == owner
                and row["lease_token"] == lease_token
                and row["receipt_sha256"] == receipt_sha
                and row["receipt_json"] == receipt_json
            ):
                conn.commit()
                return True
            raise ReviewLifecycleError("completion_conflict")
        if state != "reserved" or row["owner"] != owner or row["lease_token"] != lease_token:
            raise ReviewLifecycleError("completion_conflict")
        if _parse_utc(str(row["lease_expires_at"]), "lease_expires_at") <= now:
            raise ReviewLifecycleError("stale_owner")
        receipt_commitment_sha = _receipt_commitment_sha(row, receipt_json, receipt_sha)
        cursor = conn.execute(
            "UPDATE review_action_outbox SET state='completed', receipt_json=?, "
            "receipt_sha256=?, receipt_commitment_sha256=?, completed_at=?, updated_at=? "
            f"WHERE action_id=? AND {_preimage_where_clause(row)}",
            (
                receipt_json,
                receipt_sha,
                receipt_commitment_sha,
                now_text,
                now_text,
                int(action_id),
                *_preimage_values(row),
            ),
        )
        if cursor.rowcount != 1:
            raise ReviewLifecycleError("cas_lost")
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def fail_action(
    db_path: str | Path,
    *,
    action_id: int,
    owner: str,
    lease_token: str,
    reason: str,
    now: datetime,
) -> bool:
    """Terminally fail the currently leased action using an exact preimage CAS."""
    failure = str(reason or "action_failed")[:500]
    now_text = _format_utc(now)
    conn = _connect(db_path)
    try:
        ensure_schema(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM review_action_outbox WHERE action_id=?", (int(action_id),)
        ).fetchone()
        if row is None:
            raise ReviewLifecycleError("action_missing")
        chain_row = conn.execute(
            "SELECT * FROM review_chains WHERE chain_id=?", (row["chain_id"],)
        ).fetchone()
        if chain_row is None:
            raise ReviewLifecycleError("descriptor_tamper")
        identity = _verify_chain_row(chain_row)
        _verify_chain_actions(
            conn, int(chain_row["chain_id"]), identity,
            str(chain_row["chain_identity_sha256"]),
        )
        _verify_action_row(conn, row, identity, str(chain_row["chain_identity_sha256"]))
        if str(row["state"]) == "failed":
            if row["failure_reason"] == failure:
                conn.commit()
                return True
            raise ReviewLifecycleError("failure_conflict")
        if (
            str(row["state"]) != "reserved"
            or row["owner"] != owner
            or row["lease_token"] != lease_token
        ):
            raise ReviewLifecycleError("failure_conflict")
        cursor = conn.execute(
            "UPDATE review_action_outbox SET state='failed', failure_reason=?, "
            "completed_at=?, updated_at=? "
            f"WHERE action_id=? AND {_preimage_where_clause(row)}",
            (failure, now_text, now_text, int(action_id), *_preimage_values(row)),
        )
        if cursor.rowcount != 1:
            raise ReviewLifecycleError("cas_lost")
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def defer_action(
    db_path: str | Path,
    *,
    action_id: int,
    owner: str,
    lease_token: str,
    now: datetime,
) -> bool:
    """Release one exact lease back to pending without recording an effect."""
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM review_action_outbox WHERE action_id=?", (int(action_id),)
        ).fetchone()
        if row is None:
            raise ReviewLifecycleError("action_missing")
        chain = _hydrate_chain_by_id(conn, int(row["chain_id"]))
        if (
            str(row["state"]) != "reserved"
            or row["owner"] != owner
            or row["lease_token"] != lease_token
        ):
            raise ReviewLifecycleError("defer_conflict")
        cursor = conn.execute(
            "UPDATE review_action_outbox SET state='pending', owner='', lease_token='', "
            "lease_expires_at='', updated_at=? "
            f"WHERE action_id=? AND {_preimage_where_clause(row)}",
            (_format_utc(now), int(action_id), *_preimage_values(row)),
        )
        if cursor.rowcount != 1:
            raise ReviewLifecycleError("cas_lost")
        conn.commit()
        return chain.chain_id == int(row["chain_id"])
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def lifecycle_counts(db_path: str | Path) -> dict[str, int]:
    """Return truthful bounded state counts after authenticating every chain.

    ``pending`` alone is not a queue depth. An action whose chain already has a
    failed action can never be reserved -- every later action in that chain
    waits on it -- so it is parked, not queued. Measured on this repository:
    1,847 pending, of which 1,389 were parked behind 129 failed chains. A
    reader deciding whether the orchestrator has work cannot tell those apart
    from one number, and 'pending' read as a backlog it would never work off.
    """
    conn = _connect(db_path)
    try:
        ensure_schema(conn)
        _verify_all_chains(conn)
        counts = {state: 0 for state in sorted(VALID_STATES)}
        for row in conn.execute(
            "SELECT state, COUNT(*) AS count FROM review_action_outbox GROUP BY state"
        ):
            counts[str(row["state"])] = int(row["count"])
        parked = conn.execute(
            "SELECT COUNT(*) FROM review_action_outbox WHERE state='pending' "
            "AND chain_id IN (SELECT chain_id FROM review_action_outbox "
            "WHERE state='failed')"
        ).fetchone()[0]
        counts["pending_parked"] = int(parked)
        counts["pending_reservable"] = max(0, counts.get("pending", 0) - int(parked))
        return counts
    finally:
        conn.close()


def _connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    stack = ExitStack()
    stack.enter_context(db_writer.write_lease(path))
    try:
        conn = sqlite3.connect(str(path), timeout=5.0, factory=db_writer._LeasedConnection)
        conn.execute("PRAGMA busy_timeout=5000")
        conn.row_factory = sqlite3.Row
        conn._lease_stack = stack  # type: ignore[attr-defined]
        return conn
    except Exception:
        stack.close()
        raise


def _read_connection(db_path: str | Path) -> sqlite3.Connection:
    conn = connect_readonly(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _canonical_sha256(value: str, field: str) -> str:
    text = str(value or "").strip().lower()
    if not HEX64.fullmatch(text):
        raise ReviewLifecycleError(f"invalid_{field}")
    return text


def _format_utc(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ReviewLifecycleError("datetime_not_aware_utc")
    utc = value.astimezone(timezone.utc)
    return utc.isoformat(timespec="microseconds")


def _parse_utc(value: str, field: str) -> datetime:
    if not value or value.endswith("Z"):
        raise ReviewLifecycleError(f"malformed_{field}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ReviewLifecycleError(f"malformed_{field}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ReviewLifecycleError(f"malformed_{field}")
    if parsed.isoformat(timespec="microseconds") != value:
        raise ReviewLifecycleError(f"malformed_{field}")
    return parsed


def _canonical_json_sha(payload: Mapping[str, Any]) -> tuple[str, str]:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _preimage_where_clause(row: sqlite3.Row) -> str:
    del row
    return " AND ".join(f"{column}=?" for column in ACTION_PREIMAGE_COLUMNS)


def _preimage_values(row: sqlite3.Row) -> tuple[Any, ...]:
    return tuple(row[column] for column in ACTION_PREIMAGE_COLUMNS)


def _receipt_commitment_sha(
    row: sqlite3.Row,
    receipt_json: str,
    receipt_sha256: str,
) -> str:
    payload = {
        "schema_id": "aiworkhub.review_completion_receipt_commitment.v1",
        "action_id": str(row["action_id"]),
        "chain_id": str(row["chain_id"]),
        "action_index": str(row["action_index"]),
        "descriptor_sha256": str(row["descriptor_sha256"]),
        "receipt_json": receipt_json,
        "receipt_sha256": receipt_sha256,
    }
    _commitment_json, commitment_sha = _canonical_json_sha(payload)
    return commitment_sha


def _chain_identity(
    *,
    target_task_id: str,
    target_request_id: str,
    claim_epoch: str | int,
    packet_sha256: str,
    candidate_sha256: str,
) -> dict[str, str]:
    task = str(target_task_id or "").strip()
    request = str(target_request_id or "").strip()
    epoch = str(claim_epoch).strip()
    if not task or not request or not epoch:
        raise ReviewLifecycleError("invalid_chain_identity")
    return {
        "schema_id": SCHEMA_ID,
        "target_task_id": task,
        "target_request_id": request,
        "claim_epoch": epoch,
        "packet_sha256": packet_sha256,
        "candidate_sha256": candidate_sha256,
    }


def _descriptor(
    *,
    identity: Mapping[str, str],
    identity_sha256: str,
    phase: str,
    action_type: str,
    lens: str,
    action_index: int,
) -> dict[str, Any]:
    return {
        "schema_id": DESCRIPTOR_SCHEMA_ID,
        "chain_identity_sha256": identity_sha256,
        "chain_identity": dict(identity),
        "action_index": action_index,
        "phase": phase,
        "action_type": action_type,
        "lens": lens,
        "target_task_id": identity["target_task_id"],
        "target_request_id": identity["target_request_id"],
        "claim_epoch": identity["claim_epoch"],
    }


def _stored_sha256_exact(value: object, field: str) -> str:
    text = str(value)
    if not HEX64.fullmatch(text):
        raise ReviewLifecycleError(f"stored_{field}_tamper")
    return text


def _verify_chain_row(row: sqlite3.Row) -> dict[str, str]:
    packet = _stored_sha256_exact(row["packet_sha256"], "packet_sha256")
    candidate = _stored_sha256_exact(row["candidate_sha256"], "candidate_sha256")
    identity = _chain_identity(
        target_task_id=str(row["target_task_id"]),
        target_request_id=str(row["target_request_id"]),
        claim_epoch=str(row["claim_epoch"]),
        packet_sha256=packet,
        candidate_sha256=candidate,
    )
    identity_json, identity_sha = _canonical_json_sha(identity)
    if row["chain_identity_json"] != identity_json or row["chain_identity_sha256"] != identity_sha:
        raise ReviewLifecycleError("chain_identity_conflict")
    return identity


def _verify_action_row(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    identity: Mapping[str, str],
    identity_sha256: str,
) -> dict[str, Any]:
    state = str(row["state"])
    if state not in VALID_STATES:
        raise ReviewLifecycleError("malformed_state")
    _parse_utc(str(row["created_at"]), "created_at")
    _parse_utc(str(row["updated_at"]), "updated_at")
    if state in {"reserved", "completed", "failed"}:
        _parse_utc(str(row["lease_expires_at"]), "lease_expires_at")
    if state in {"completed", "failed", "retired"}:
        _parse_utc(str(row["completed_at"]), "completed_at")
    if state == "pending":
        if any(
            str(row[column])
            for column in (
                "owner",
                "lease_token",
                "lease_expires_at",
                "receipt_json",
                "receipt_sha256",
                "receipt_commitment_sha256",
                "completed_at",
                "failure_reason",
                "retired_due_to_action_id",
            )
        ):
            raise ReviewLifecycleError("descriptor_tamper")
    elif state == "reserved":
        if (
            not str(row["owner"])
            or not str(row["lease_token"])
            or not str(row["lease_expires_at"])
            or any(
                str(row[column])
                for column in (
                    "receipt_json",
                    "receipt_sha256",
                    "receipt_commitment_sha256",
                    "completed_at",
                    "failure_reason",
                    "retired_due_to_action_id",
                )
            )
        ):
            raise ReviewLifecycleError("descriptor_tamper")
    elif state == "completed":
        if (
            not str(row["owner"])
            or not str(row["lease_token"])
            or not str(row["lease_expires_at"])
            or not str(row["receipt_json"])
            or not str(row["receipt_sha256"])
            or not str(row["receipt_commitment_sha256"])
            or not str(row["completed_at"])
            or str(row["failure_reason"])
            or str(row["retired_due_to_action_id"])
        ):
            raise ReviewLifecycleError("descriptor_tamper")
        try:
            receipt = json.loads(str(row["receipt_json"]))
        except json.JSONDecodeError as exc:
            raise ReviewLifecycleError("receipt_tamper") from exc
        if not isinstance(receipt, dict):
            raise ReviewLifecycleError("receipt_tamper")
        receipt_json, receipt_sha = _canonical_json_sha(receipt)
        if row["receipt_json"] != receipt_json or row["receipt_sha256"] != receipt_sha:
            raise ReviewLifecycleError("receipt_tamper")
        if row["receipt_commitment_sha256"] != _receipt_commitment_sha(
            row, receipt_json, receipt_sha
        ):
            raise ReviewLifecycleError("receipt_tamper")
    elif state == "failed":
        if (
            not str(row["owner"])
            or not str(row["lease_token"])
            or not str(row["lease_expires_at"])
            or not str(row["completed_at"])
            or not str(row["failure_reason"])
            or str(row["retired_due_to_action_id"])
            or any(
                str(row[column])
                for column in (
                    "receipt_json",
                    "receipt_sha256",
                    "receipt_commitment_sha256",
                )
            )
        ):
            raise ReviewLifecycleError("descriptor_tamper")
    elif state == "retired":
        if (
            str(row["owner"])
            or str(row["lease_token"])
            or str(row["lease_expires_at"])
            or not str(row["completed_at"])
            or str(row["failure_reason"]) != RETIRED_REASON
            or any(
                str(row[column])
                for column in (
                    "receipt_json",
                    "receipt_sha256",
                    "receipt_commitment_sha256",
                )
            )
        ):
            raise ReviewLifecycleError("descriptor_tamper")
        _verify_retirement_evidence(conn, row)
    action_index = int(row["action_index"])
    if action_index < 0 or action_index >= len(PLAN):
        raise ReviewLifecycleError("descriptor_tamper")
    expected_index, phase, action_type, lens = PLAN[action_index]
    if (
        expected_index != action_index
        or row["phase"] != phase
        or row["action_type"] != action_type
        or row["lens"] != lens
        or row["target_task_id"] != identity["target_task_id"]
        or row["target_request_id"] != identity["target_request_id"]
        or row["claim_epoch"] != identity["claim_epoch"]
    ):
        raise ReviewLifecycleError("descriptor_tamper")
    expected = _descriptor(
        identity=identity,
        identity_sha256=identity_sha256,
        phase=phase,
        action_type=action_type,
        lens=lens,
        action_index=action_index,
    )
    descriptor_json, descriptor_sha = _canonical_json_sha(expected)
    if row["descriptor_json"] != descriptor_json or row["descriptor_sha256"] != descriptor_sha:
        raise ReviewLifecycleError("descriptor_tamper")
    return expected


def _verify_retirement_evidence(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    """Bind a retired row to the exact earlier same-chain failed row it cites.

    Malformed, cross-chain, later, or nonfailed evidence fails closed: a
    retired action's whole authority to skip execution comes from proving one
    real, earlier, same-chain failure caused it.
    """
    raw = str(row["retired_due_to_action_id"])
    if not raw.isdigit():
        raise ReviewLifecycleError("retirement_evidence_invalid")
    cause = conn.execute(
        "SELECT chain_id, action_index, state FROM review_action_outbox WHERE action_id=?",
        (int(raw),),
    ).fetchone()
    if (
        cause is None
        or int(cause["chain_id"]) != int(row["chain_id"])
        or int(cause["action_index"]) >= int(row["action_index"])
        or str(cause["state"]) != "failed"
    ):
        raise ReviewLifecycleError("retirement_evidence_invalid")


def _verify_chain_actions(
    conn: sqlite3.Connection,
    chain_id: int,
    identity: Mapping[str, str],
    identity_sha256: str,
) -> None:
    rows = conn.execute(
        "SELECT * FROM review_action_outbox WHERE chain_id=? ORDER BY action_index",
        (chain_id,),
    ).fetchall()
    if len(rows) != len(PLAN):
        raise ReviewLifecycleError("descriptor_tamper")
    if [int(row["action_index"]) for row in rows] != list(range(len(PLAN))):
        raise ReviewLifecycleError("descriptor_tamper")
    for row in rows:
        _verify_action_row(conn, row, identity, identity_sha256)


def _verify_all_chains(conn: sqlite3.Connection) -> None:
    chain_rows = conn.execute(
        "SELECT * FROM review_chains ORDER BY chain_id"
    ).fetchall()
    for chain_row in chain_rows:
        identity = _verify_chain_row(chain_row)
        _verify_chain_actions(
            conn,
            int(chain_row["chain_id"]),
            identity,
            str(chain_row["chain_identity_sha256"]),
        )


def _prior_actions_completed(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    identity: Mapping[str, str],
    identity_sha256: str,
) -> bool:
    action_index = int(row["action_index"])
    prior_rows = conn.execute(
        "SELECT * FROM review_action_outbox "
        "WHERE chain_id=? AND action_index<? ORDER BY action_index",
        (row["chain_id"], action_index),
    ).fetchall()
    if len(prior_rows) != action_index:
        raise ReviewLifecycleError("descriptor_tamper")
    for prior in prior_rows:
        _verify_action_row(conn, prior, identity, identity_sha256)
        if str(prior["state"]) != "completed":
            return False
    return True


def _hydrate_chain(conn: sqlite3.Connection, row: sqlite3.Row) -> ReviewChain:
    identity = _verify_chain_row(row)
    _verify_chain_actions(conn, int(row["chain_id"]), identity, str(row["chain_identity_sha256"]))
    return ReviewChain(
        chain_id=int(row["chain_id"]),
        chain_identity_sha256=str(row["chain_identity_sha256"]),
        chain_identity=identity,
        actions=tuple(
            _action_from_row(action_row)
            for action_row in conn.execute(
                "SELECT * FROM review_action_outbox WHERE chain_id=? ORDER BY action_index",
                (row["chain_id"],),
            )
        ),
    )


def _hydrate_chain_by_id(conn: sqlite3.Connection, chain_id: int) -> ReviewChain:
    row = conn.execute(
        "SELECT * FROM review_chains WHERE chain_id=?", (chain_id,)
    ).fetchone()
    if row is None:
        raise ReviewLifecycleError("chain_missing")
    return _hydrate_chain(conn, row)


def _action_from_row(row: sqlite3.Row | None) -> ReviewAction:
    if row is None:
        raise ReviewLifecycleError("action_missing")
    try:
        descriptor = json.loads(str(row["descriptor_json"]))
    except json.JSONDecodeError as exc:
        raise ReviewLifecycleError("descriptor_tamper") from exc
    return ReviewAction(
        action_id=int(row["action_id"]),
        chain_id=int(row["chain_id"]),
        action_index=int(row["action_index"]),
        phase=str(row["phase"]),
        action_type=str(row["action_type"]),
        lens=str(row["lens"]),
        descriptor=descriptor,
        descriptor_sha256=str(row["descriptor_sha256"]),
    )


def actions_for_chain(db_path: str | Path, chain_id: int) -> tuple[ReviewAction, ...]:
    conn = _read_connection(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM review_chains WHERE chain_id=?", (int(chain_id),)
        ).fetchone()
        if row is None:
            raise ReviewLifecycleError("chain_missing")
        return _hydrate_chain(conn, row).actions
    finally:
        conn.close()


def completed_receipts_for_chain(
    db_path: str | Path, chain_id: int
) -> tuple[dict[str, Any], ...]:
    """Return completed receipts only after authenticating the whole chain."""
    conn = _read_connection(db_path)
    try:
        _hydrate_chain_by_id(conn, int(chain_id))
        rows = conn.execute(
            "SELECT receipt_json FROM review_action_outbox "
            "WHERE chain_id=? AND state='completed' ORDER BY action_index",
            (int(chain_id),),
        ).fetchall()
        return tuple(json.loads(str(row["receipt_json"])) for row in rows)
    finally:
        conn.close()


def completed_manager_ready_receipts(
    db_path: str | Path,
) -> tuple[dict[str, Any], ...]:
    """Return authenticated manager-ready receipts awaiting a human decision.

    ``target_accept`` is retained as a legacy action identity because action
    descriptors are immutable.  A legacy row is manager-ready only when its
    completed receipt carries the new aggregate schema; old automatic-accept
    receipts never satisfy this predicate.
    """

    conn = _read_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT DISTINCT chain_id FROM review_action_outbox "
            "WHERE action_index=9 AND state='completed' "
            "AND action_type='target_accept' "
            "ORDER BY chain_id"
        ).fetchall()
        receipts: list[dict[str, Any]] = []
        for row in rows:
            chain = _hydrate_chain_by_id(conn, int(row["chain_id"]))
            action_rows = conn.execute(
                "SELECT receipt_json FROM review_action_outbox "
                "WHERE chain_id=? AND action_index=9 AND state='completed' "
                "AND action_type='target_accept'",
                (chain.chain_id,),
            ).fetchall()
            if len(action_rows) != 1:
                raise ReviewLifecycleError("manager_ready_action_ambiguous")
            receipt = json.loads(str(action_rows[0]["receipt_json"]))
            aggregate = receipt.get("manager_ready")
            if aggregate is None:
                # A completed descriptor from releases before the manager-ready
                # boundary is authenticated history, not a pending manager wake.
                continue
            identity = chain.chain_identity
            if (
                not isinstance(aggregate, dict)
                or aggregate.get("schema_id") != MANAGER_READY_SCHEMA_ID
                or aggregate.get("chain_id") != chain.chain_id
                or aggregate.get("chain_identity_sha256")
                != chain.chain_identity_sha256
                or aggregate.get("target_task_id") != identity["target_task_id"]
                or aggregate.get("target_request_id")
                != identity["target_request_id"]
                or str(aggregate.get("claim_epoch")) != identity["claim_epoch"]
                or aggregate.get("packet_sha256") != identity["packet_sha256"]
                or aggregate.get("candidate_sha256")
                != identity["candidate_sha256"]
                or not isinstance(aggregate.get("reviews"), list)
            ):
                raise ReviewLifecycleError("manager_ready_receipt_invalid")
            receipts.append(receipt)
        return tuple(receipts)
    finally:
        conn.close()


def manager_ready_receipt_for_target(
    db_path: str | Path,
    *,
    target_task_id: str,
    target_request_id: str,
    claim_epoch: str | int,
) -> dict[str, Any] | None:
    """Return the one authenticated receipt for an exact target episode."""

    matches = []
    for receipt in completed_manager_ready_receipts(db_path):
        aggregate = receipt["manager_ready"]
        if (
            aggregate["target_task_id"] == str(target_task_id)
            and aggregate["target_request_id"] == str(target_request_id)
            and str(aggregate["claim_epoch"]) == str(claim_epoch)
        ):
            matches.append(receipt)
    if len(matches) > 1:
        raise ReviewLifecycleError("manager_ready_receipt_ambiguous")
    return matches[0] if matches else None


def replay_sources(
    db_path: str | Path,
    *,
    target_task_id: str,
    candidate_sha256: str,
    contract_identity_sha256: str,
    exclude_chain_id: int,
) -> dict[str, dict[str, Any]]:
    """Per lens, an EARLIER chain that already ingested a report for these bytes.

    Measured 2026-09-08 over the live store: 627 chains for 600 distinct
    ``(target_task_id, candidate_sha256)`` pairs -- 27 chains that re-reviewed
    bytes another chain had already been built for, because a chain is keyed by
    ``(target_task_id, target_request_id, claim_epoch)`` and a new request id
    mints a new chain even when every changed file is byte-identical.

    A lens is offered as replayable only when ALL of these hold on the source
    chain, and every one of them is a stored fact rather than an inference:

    * the same target task;
    * the identical ``candidate_sha256`` -- the canonical digest of the whole
      ``changed_path_hashes`` map, so one differing byte in one file is a
      different candidate and no replay is offered;
    * an identical, NON-EMPTY ``contract_identity_sha256``.  Empty means the
      contract was never recorded, and unknown never matches: a chain written
      before this column existed can neither replay nor be replayed from; and
    * the source chain's ``launch`` action for that lens completed with a real
      ``reviewer_request_id`` AND its ``accept`` action completed.  The accept
      action is the proof of INGESTION: it only completes after
      ``_review_receipt`` has authenticated the reviewer's sealed, packet-bound
      report.  A launched-but-never-ingested reviewer offers nothing.

    Everything else -- a missing column, an unreadable receipt, an ambiguous
    duplicate -- yields no entry for that lens, and no entry means a fresh
    reviewer is launched.  This is a fail-closed lookup: it can only ever
    remove a duplicate run, never authorise one that was not already paid for.
    """

    if not HEX64.fullmatch(str(candidate_sha256 or "")) or not HEX64.fullmatch(
        str(contract_identity_sha256 or "")
    ):
        return {}
    conn = _connect(db_path)
    try:
        ensure_schema(conn)
        conn.commit()
        chains = conn.execute(
            "SELECT chain_id, target_request_id, claim_epoch, packet_sha256 "
            "FROM review_chains WHERE target_task_id=? AND candidate_sha256=? "
            "AND contract_identity_sha256=? AND chain_id<>? ORDER BY chain_id",
            (
                str(target_task_id),
                str(candidate_sha256),
                str(contract_identity_sha256),
                int(exclude_chain_id),
            ),
        ).fetchall()
        found: dict[str, dict[str, Any]] = {}
        for chain_row in chains:
            chain_id = int(chain_row["chain_id"])
            actions = conn.execute(
                "SELECT action_type, lens, receipt_json FROM review_action_outbox "
                "WHERE chain_id=? AND state='completed' AND lens<>'' "
                "ORDER BY action_index",
                (chain_id,),
            ).fetchall()
            launches: dict[str, dict[str, Any]] = {}
            ingested: set[str] = set()
            for action in actions:
                try:
                    receipt = json.loads(str(action["receipt_json"] or "{}"))
                except (TypeError, ValueError):
                    continue
                if not isinstance(receipt, dict):
                    continue
                lens = str(action["lens"])
                if str(action["action_type"]) == "launch":
                    reviewer_request_id = str(receipt.get("reviewer_request_id") or "")
                    if reviewer_request_id and not receipt.get("obsolete_reason"):
                        launches[lens] = receipt
                elif str(action["action_type"]) == "accept":
                    if not receipt.get("obsolete_reason"):
                        ingested.add(lens)
            for lens, receipt in launches.items():
                if lens in found or lens not in ingested:
                    continue
                found[lens] = {
                    "source_chain_id": chain_id,
                    "source_target_request_id": str(chain_row["target_request_id"]),
                    "source_claim_epoch": str(chain_row["claim_epoch"]),
                    "source_packet_sha256": str(chain_row["packet_sha256"]),
                    "reviewer_request_id": str(receipt.get("reviewer_request_id") or ""),
                    "reviewer_task_id": str(receipt.get("reviewer_task_id") or ""),
                    "reviewer_route": receipt.get("reviewer_route") or {},
                    "candidate_sha256": str(candidate_sha256),
                    "contract_identity_sha256": str(contract_identity_sha256),
                }
        return found
    finally:
        conn.close()


def rows_for_test(db_path: str | Path) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        ensure_schema(conn)
        return [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM review_action_outbox ORDER BY action_index"
            )
        ]
    finally:
        conn.close()


RECONCILE_BATCH_LIMIT = 256
ROUTE_UNAVAILABLE_FAILURE_PREFIX = "RuntimeError:review_route_unavailable:"


def recover_route_unavailable_chains(
    db_path: str | Path,
    *,
    now: datetime,
    batch_limit: int = RECONCILE_BATCH_LIMIT,
) -> dict[str, int]:
    """Requeue chains terminalized only because no reviewer route existed.

    Older orchestrators failed the launch action when the reviewer catalog had
    no eligible route, then retired every descendant.  Route availability is
    operational state, not a verdict on candidate bytes.  Recover only the
    exact authenticated launch failure and descendants retired by that action;
    the orchestrator will either defer again or complete the ordinary chain.
    """
    limit = max(1, min(int(batch_limit), RECONCILE_BATCH_LIMIT))
    now_text = _format_utc(now)
    conn = _connect(db_path)
    try:
        ensure_schema(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        failed_rows = conn.execute(
            "SELECT * FROM review_action_outbox "
            "WHERE state='failed' AND action_type='launch' "
            "AND failure_reason LIKE ? ORDER BY action_id LIMIT ?",
            (ROUTE_UNAVAILABLE_FAILURE_PREFIX + "%", limit),
        ).fetchall()
        recovered = descendants_requeued = 0
        for failed in failed_rows:
            expected_reason = ROUTE_UNAVAILABLE_FAILURE_PREFIX + str(failed["lens"])
            if str(failed["failure_reason"]) != expected_reason:
                continue
            chain_id = int(failed["chain_id"])
            action_id = int(failed["action_id"])
            chain_row = conn.execute(
                "SELECT * FROM review_chains WHERE chain_id=?", (chain_id,)
            ).fetchone()
            if chain_row is None:
                raise ReviewLifecycleError("descriptor_tamper")
            identity = _verify_chain_row(chain_row)
            chain_identity_sha256 = str(chain_row["chain_identity_sha256"])
            _verify_action_row(conn, failed, identity, chain_identity_sha256)
            descendants = conn.execute(
                "SELECT * FROM review_action_outbox WHERE chain_id=? "
                "AND action_index>? AND state='retired' "
                "AND retired_due_to_action_id=? AND failure_reason=? "
                "ORDER BY action_index",
                (
                    chain_id,
                    int(failed["action_index"]),
                    str(action_id),
                    RETIRED_REASON,
                ),
            ).fetchall()
            for descendant in descendants:
                _verify_action_row(
                    conn, descendant, identity, chain_identity_sha256
                )
            for descendant in descendants:
                updated = conn.execute(
                    "UPDATE review_action_outbox SET state='pending',owner='',"
                    "lease_token='',lease_expires_at='',receipt_json='',"
                    "receipt_sha256='',receipt_commitment_sha256='',"
                    "completed_at='',failure_reason='',retired_due_to_action_id='',"
                    "updated_at=? WHERE action_id=? AND "
                    + _preimage_where_clause(descendant),
                    (
                        now_text,
                        int(descendant["action_id"]),
                        *_preimage_values(descendant),
                    ),
                )
                if updated.rowcount != 1:
                    raise ReviewLifecycleError("cas_lost")
                descendants_requeued += 1
            updated = conn.execute(
                "UPDATE review_action_outbox SET state='pending',owner='',"
                "lease_token='',lease_expires_at='',receipt_json='',"
                "receipt_sha256='',receipt_commitment_sha256='',completed_at='',"
                "failure_reason='',retired_due_to_action_id='',updated_at=? "
                "WHERE action_id=? AND " + _preimage_where_clause(failed),
                (now_text, action_id, *_preimage_values(failed)),
            )
            if updated.rowcount != 1:
                raise ReviewLifecycleError("cas_lost")
            recovered += 1
        conn.commit()
        return {
            "examined": len(failed_rows),
            "recovered": recovered,
            "descendants_requeued": descendants_requeued,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def reconcile_dead_chains(
    db_path: str | Path,
    *,
    now: datetime,
    batch_limit: int = RECONCILE_BATCH_LIMIT,
) -> dict[str, int]:
    """Bounded, idempotent pass that retires dead-chain descendants.

    A failed action permanently blocks every later action in its chain --
    ``_prior_actions_completed`` never sees a failed prior as complete -- so
    those descendants would stay ``pending`` forever without this. Each call
    inspects at most ``batch_limit`` failed actions through an indexed keyset
    cursor that always advances and wraps back to the start once exhausted,
    so a chain that fails after a busy one is never permanently starved.
    Retiring an already-retired descendant is a no-op, so repeated calls
    converge without re-doing work.

    Every failed row and every descendant it retires is authenticated through
    ``_verify_chain_row``/``_verify_action_row`` before this mutates it, the
    same way ``reserve_next_action`` and ``complete_action`` authenticate
    before they act: a row this pass has not verified has no standing to be
    retired, and a tampered descendant descriptor must fail this whole pass
    closed rather than being silently marked retired.
    """
    limit = max(1, min(int(batch_limit), RECONCILE_BATCH_LIMIT))
    now_text = _format_utc(now)
    conn = _connect(db_path)
    try:
        ensure_schema(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        cursor = _reconciliation_cursor(conn)
        failed_rows = conn.execute(
            "SELECT * FROM review_action_outbox "
            "WHERE state='failed' AND action_id > ? ORDER BY action_id LIMIT ?",
            (cursor, limit),
        ).fetchall()
        wrapped = False
        if not failed_rows and cursor != 0:
            wrapped = True
            failed_rows = conn.execute(
                "SELECT * FROM review_action_outbox "
                "WHERE state='failed' ORDER BY action_id LIMIT ?",
                (limit,),
            ).fetchall()
        retired = 0
        for failed in failed_rows:
            chain_id = int(failed["chain_id"])
            action_index = int(failed["action_index"])
            action_id = int(failed["action_id"])
            chain_row = conn.execute(
                "SELECT * FROM review_chains WHERE chain_id=?", (chain_id,)
            ).fetchone()
            if chain_row is None:
                raise ReviewLifecycleError("descriptor_tamper")
            identity = _verify_chain_row(chain_row)
            chain_identity_sha256 = str(chain_row["chain_identity_sha256"])
            _verify_action_row(conn, failed, identity, chain_identity_sha256)
            descendants = conn.execute(
                "SELECT * FROM review_action_outbox WHERE chain_id=? AND action_index>? "
                "AND state='pending' ORDER BY action_index",
                (chain_id, action_index),
            ).fetchall()
            for descendant in descendants:
                _verify_action_row(conn, descendant, identity, chain_identity_sha256)
                updated = conn.execute(
                    "UPDATE review_action_outbox SET state='retired', "
                    "retired_due_to_action_id=?, failure_reason=?, completed_at=?, "
                    "updated_at=? "
                    f"WHERE action_id=? AND {_preimage_where_clause(descendant)}",
                    (
                        str(action_id),
                        RETIRED_REASON,
                        now_text,
                        now_text,
                        int(descendant["action_id"]),
                        *_preimage_values(descendant),
                    ),
                )
                if updated.rowcount == 1:
                    retired += 1
        next_cursor = max(
            (int(row["action_id"]) for row in failed_rows),
            default=0 if wrapped else cursor,
        )
        _set_reconciliation_cursor(conn, next_cursor)
        conn.commit()
        return {
            "examined_failed": len(failed_rows),
            "retired": retired,
            "wrapped": int(wrapped),
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _reconciliation_cursor(conn: sqlite3.Connection) -> int:
    conn.execute(
        "INSERT OR IGNORE INTO review_reconciliation_state (id, last_failed_action_id) "
        "VALUES (1, 0)"
    )
    row = conn.execute(
        "SELECT last_failed_action_id FROM review_reconciliation_state WHERE id=1"
    ).fetchone()
    return int(row[0]) if row is not None else 0


def _set_reconciliation_cursor(conn: sqlite3.Connection, value: int) -> None:
    conn.execute(
        "UPDATE review_reconciliation_state SET last_failed_action_id=? WHERE id=1",
        (int(value),),
    )


def _pending_high_watermark(conn: sqlite3.Connection) -> int:
    """Snapshot the current maximum pending ``action_id``, or 0 if none.

    Taken fresh at the start of every reservation round so the round's upper
    bound is fixed to what existed at that moment -- rows that arrive after
    cannot extend it, which is what keeps a round bounded under sustained
    arrivals.
    """
    row = conn.execute(
        "SELECT COALESCE(MAX(action_id), 0) FROM review_action_outbox WHERE state='pending'"
    ).fetchone()
    return int(row[0]) if row is not None else 0


def _reservation_cursor(conn: sqlite3.Connection) -> tuple[int, int]:
    conn.execute(
        "INSERT OR IGNORE INTO review_reservation_state "
        "(id, last_pending_action_id, round_high_watermark) VALUES (1, 0, 0)"
    )
    row = conn.execute(
        "SELECT last_pending_action_id, round_high_watermark "
        "FROM review_reservation_state WHERE id=1"
    ).fetchone()
    return (int(row[0]), int(row[1])) if row is not None else (0, 0)


def _set_reservation_cursor(conn: sqlite3.Connection, cursor: int, watermark: int) -> None:
    conn.execute(
        "UPDATE review_reservation_state SET last_pending_action_id=?, "
        "round_high_watermark=? WHERE id=1",
        (int(cursor), int(watermark)),
    )
