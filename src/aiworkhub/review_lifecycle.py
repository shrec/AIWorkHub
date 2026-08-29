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
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping


SCHEMA_ID = "aiworkhub.review_lifecycle.v1"
DESCRIPTOR_SCHEMA_ID = "aiworkhub.review_action_descriptor.v1"
HEX64 = re.compile(r"^[0-9a-f]{64}$")
VALID_STATES = {"pending", "reserved", "completed", "failed"}
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
    (9, "target", "target_accept", ""),
    (10, "target", "target_archive", ""),
    (11, "needfix", "needfix_close", ""),
)

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
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(chain_id) REFERENCES review_chains(chain_id),
  UNIQUE(chain_id, action_index)
);
CREATE INDEX IF NOT EXISTS idx_review_action_outbox_state
  ON review_action_outbox(state, action_id);

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
) -> ReviewChain:
    packet = _canonical_sha256(packet_sha256, "packet_sha256")
    candidate = _canonical_sha256(candidate_sha256, "candidate_sha256")
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
            conn.commit()
            return _hydrate_chain_by_id(conn, chain.chain_id)
        identity_json, identity_sha = _canonical_json_sha(identity)
        cursor = conn.execute(
            "INSERT INTO review_chains("
            "target_task_id,target_request_id,claim_epoch,packet_sha256,candidate_sha256,"
            "chain_identity_json,chain_identity_sha256,created_at"
            ") VALUES(?,?,?,?,?,?,?,?)",
            (
                identity["target_task_id"],
                identity["target_request_id"],
                identity["claim_epoch"],
                packet,
                candidate,
                identity_json,
                identity_sha,
                created_at,
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
        _verify_all_chains(conn)
        rows = conn.execute(
            "SELECT * FROM review_action_outbox ORDER BY action_id"
        ).fetchall()
        for row in rows:
            chain_row = conn.execute(
                "SELECT * FROM review_chains WHERE chain_id=?", (row["chain_id"],)
            ).fetchone()
            if chain_row is None:
                raise ReviewLifecycleError("descriptor_tamper")
            identity = _verify_chain_row(chain_row)
            _verify_action_row(row, identity, chain_row["chain_identity_sha256"])
            state = str(row["state"])
            if state == "pending":
                if not _prior_actions_completed(
                    conn, row, identity, chain_row["chain_identity_sha256"]
                ):
                    continue
            elif state == "reserved":
                lease_expires_at = _parse_utc(str(row["lease_expires_at"]), "lease_expires_at")
                if lease_expires_at > now:
                    continue
                if not _prior_actions_completed(
                    conn, row, identity, chain_row["chain_identity_sha256"]
                ):
                    continue
            else:
                continue
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
        conn.commit()
        return None
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


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
        _verify_action_row(row, identity, chain_row["chain_identity_sha256"])
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
        _verify_action_row(row, identity, str(chain_row["chain_identity_sha256"]))
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
    """Return truthful bounded state counts after authenticating every chain."""
    conn = _connect(db_path)
    try:
        ensure_schema(conn)
        _verify_all_chains(conn)
        counts = {state: 0 for state in sorted(VALID_STATES)}
        for row in conn.execute(
            "SELECT state, COUNT(*) AS count FROM review_action_outbox GROUP BY state"
        ):
            counts[str(row["state"])] = int(row["count"])
        return counts
    finally:
        conn.close()


def _connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5.0)
    conn.execute("PRAGMA busy_timeout=5000")
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
    if state in {"completed", "failed"}:
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
        _verify_action_row(row, identity, identity_sha256)


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
        _verify_action_row(prior, identity, identity_sha256)
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
    conn = _connect(db_path)
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
    conn = _connect(db_path)
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
