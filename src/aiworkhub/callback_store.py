"""Package-local canonical callback outbox/batch store.

Self-contained port of the callback outbox/batch primitives ``CallbackBridge``
needs (originally ``AITools/taskdb.py``) into the standalone ``aiworkhub``
package. This module never imports ``AITools/taskdb.py`` or any other
repository-root-relative module: a packaged VSIX runtime with no ``AITools``
directory anywhere on ``sys.path`` still has full callback delivery.

The durable outbox/batch tables live in the SAME canonical, repo-local
``.aiworkhub/tasking/task_queue.sqlite`` that ``task_store.py`` resolves
through the storage registry (``resolve_db_path`` below) -- never a second,
fallback database. Existing ``callback_outbox``/``callback_batches`` rows and
schema (created by a pre-existing ``task_store.SCHEMA`` or a legacy
``AITools/taskdb.py`` install sharing the same file) are upgraded additively
by :func:`init_db`; a pending row is never deleted or rewritten by that
upgrade.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import task_store

SCHEMA_ID = "aiworkhub.callback_store.v1"

# Bounded batch size: coalesces all currently-eligible pending rows for one
# origin_thread_id into a single Codex/Claude turn (see B402 -- eight
# near-simultaneous review_ready events previously produced up to eight
# separate turns/dead letters).
DEFAULT_CALLBACK_BATCH_MAX_MEMBERS = 25

# Terminal callback transitions that are allowed to enqueue an outbox wake.
CALLBACK_ELIGIBLE_TRANSITIONS: frozenset[str] = frozenset({
    "review_ready",
    "blocked",
    "launch_failed",
    "validation_failed",
    "scope_rejected",
    "timed_out",
    "cancelled",
})

CALLBACK_OUTBOX_STATES: tuple[str, ...] = (
    "pending", "inflight", "delivered", "dead_letter", "superseded",
)
CALLBACK_BATCH_STATES: tuple[str, ...] = CALLBACK_OUTBOX_STATES


class CallbackStoreError(RuntimeError):
    """Base class for package-local callback store failures. Fail closed."""


class CallbackStoreNotReadyError(CallbackStoreError):
    """The canonical repository-local task queue is not initialized/ready."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def resolve_db_path(repo: str | Path) -> Path:
    """Resolve the SAME canonical ``.aiworkhub/tasking/task_queue.sqlite``
    ``task_store.py`` uses, through the storage registry. Raises
    :class:`CallbackStoreNotReadyError` (never a silent fallback/second
    database) if the repository is not initialized/ready."""
    readiness = task_store.storage_readiness(repo)
    if not readiness.ready:
        raise CallbackStoreNotReadyError(readiness.reason)
    return Path(readiness.canonical_db)


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    for attempt in range(5):
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            break
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == 4:
                conn.close()
                raise
            time.sleep(0.05 * (attempt + 1))
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row["name"] == column for row in rows)


def _ensure_callback_outbox_table(conn: sqlite3.Connection) -> None:
    """Idempotent, fail-closed migration: create/upgrade callback_outbox.

    Dedup scope is per CLAIM/REVIEW EPISODE, not per task lifetime: the
    UNIQUE constraint covers (task_id, transition, origin_thread_id,
    episode_id, request_id), matching the ``taskctl.py`` claim_epoch
    convention this table is shared with."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS callback_outbox (
          outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
          task_id TEXT NOT NULL,
          provider TEXT NOT NULL DEFAULT '',
          origin_thread_id TEXT NOT NULL DEFAULT '',
          transition TEXT NOT NULL DEFAULT '',
          episode_id TEXT NOT NULL DEFAULT '0',
          event_id TEXT NOT NULL DEFAULT '',
          request_id TEXT NOT NULL DEFAULT '',
          state TEXT NOT NULL DEFAULT 'pending',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          attempts INTEGER NOT NULL DEFAULT 0,
          last_error TEXT NOT NULL DEFAULT '',
          lease_id TEXT NOT NULL DEFAULT '',
          lease_expires_at TEXT NOT NULL DEFAULT '',
          batch_id TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.commit()
    for column, ddl in (
        ("provider", "TEXT NOT NULL DEFAULT ''"),
        ("episode_id", "TEXT NOT NULL DEFAULT '0'"),
        ("event_id", "TEXT NOT NULL DEFAULT ''"),
        ("request_id", "TEXT NOT NULL DEFAULT ''"),
        ("batch_id", "TEXT NOT NULL DEFAULT ''"),
        ("attempts", "INTEGER NOT NULL DEFAULT 0"),
        ("last_error", "TEXT NOT NULL DEFAULT ''"),
        ("lease_id", "TEXT NOT NULL DEFAULT ''"),
        ("lease_expires_at", "TEXT NOT NULL DEFAULT ''"),
    ):
        if not _column_exists(conn, "callback_outbox", column):
            conn.execute(f"ALTER TABLE callback_outbox ADD COLUMN {column} {ddl}")
    conn.commit()
    conn.execute("CREATE INDEX IF NOT EXISTS idx_callback_outbox_state ON callback_outbox(state)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_callback_outbox_batch_id ON callback_outbox(batch_id)")
    conn.commit()


def _ensure_callback_batches_table(conn: sqlite3.Connection) -> None:
    """Idempotent migration: create/upgrade callback_batches.

    One row per coalesced delivery batch: durable batch identity/lease/
    attempts live here so N member outbox rows transition together (bulk
    UPDATE by batch_id) instead of per-row lease bookkeeping."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS callback_batches (
          batch_id TEXT PRIMARY KEY,
          provider TEXT NOT NULL DEFAULT '',
          origin_thread_id TEXT NOT NULL DEFAULT '',
          state TEXT NOT NULL DEFAULT 'pending',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          attempts INTEGER NOT NULL DEFAULT 0,
          last_error TEXT NOT NULL DEFAULT '',
          lease_id TEXT NOT NULL DEFAULT '',
          lease_expires_at TEXT NOT NULL DEFAULT '',
          member_count INTEGER NOT NULL DEFAULT 0,
          not_before_at TEXT NOT NULL DEFAULT '',
          hard_failure_count INTEGER NOT NULL DEFAULT 0,
          last_failure_kind TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.commit()
    for column, ddl in (
        ("provider", "TEXT NOT NULL DEFAULT ''"),
        ("attempts", "INTEGER NOT NULL DEFAULT 0"),
        ("last_error", "TEXT NOT NULL DEFAULT ''"),
        ("lease_id", "TEXT NOT NULL DEFAULT ''"),
        ("lease_expires_at", "TEXT NOT NULL DEFAULT ''"),
        ("member_count", "INTEGER NOT NULL DEFAULT 0"),
        ("not_before_at", "TEXT NOT NULL DEFAULT ''"),
        ("hard_failure_count", "INTEGER NOT NULL DEFAULT 0"),
        ("last_failure_kind", "TEXT NOT NULL DEFAULT ''"),
    ):
        if not _column_exists(conn, "callback_batches", column):
            conn.execute(f"ALTER TABLE callback_batches ADD COLUMN {column} {ddl}")
    conn.commit()
    conn.execute("CREATE INDEX IF NOT EXISTS idx_callback_batches_state ON callback_batches(state)")
    conn.commit()


def _ensure_tasks_origin_thread_column(conn: sqlite3.Connection) -> None:
    if _column_exists(conn, "tasks", "origin_thread_id"):
        return
    try:
        conn.execute("ALTER TABLE tasks ADD COLUMN origin_thread_id TEXT NOT NULL DEFAULT ''")
    except sqlite3.OperationalError:
        # Another repository-bound dispatcher may have completed the same
        # additive migration after our PRAGMA check.  Treat only that exact
        # converged schema as success; every other migration error remains
        # fail-closed.
        if not _column_exists(conn, "tasks", "origin_thread_id"):
            raise
    conn.commit()


def init_db(conn: sqlite3.Connection) -> None:
    """Ensure every table/column this module needs exists, additively.
    Never drops or rewrites an existing row -- see module docstring."""
    conn.executescript(task_store.SCHEMA)
    conn.commit()
    _ensure_tasks_origin_thread_column(conn)
    _ensure_callback_outbox_table(conn)
    _ensure_callback_batches_table(conn)


def append_event(
    conn: sqlite3.Connection, task_id: str, event: str, runner: str, payload: dict | None = None,
) -> None:
    conn.execute(
        "INSERT INTO task_events(task_id, event, runner, payload_json, created_at) VALUES (?, ?, ?, ?, ?)",
        (task_id, event, runner, json.dumps(payload or {}, ensure_ascii=False, sort_keys=True), utc_now()),
    )
    conn.commit()


def task_count(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()
    return int(row["n"])


def origin_thread_bound_count(conn: sqlite3.Connection) -> int:
    _ensure_tasks_origin_thread_column(conn)
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM tasks WHERE origin_thread_id IS NOT NULL AND origin_thread_id != ''"
    ).fetchone()
    return int(row["n"])


def read_origin_thread(conn: sqlite3.Connection, task_id: str) -> str | None:
    _ensure_tasks_origin_thread_column(conn)
    row = conn.execute("SELECT origin_thread_id FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    if row is None:
        return None
    return row["origin_thread_id"] or None


def current_claim_episode(conn: sqlite3.Connection, task_id: str) -> str:
    """The task's current claim/review episode id (``card.claim_epoch``).
    Legacy cards without the field are episode '0'."""
    row = conn.execute("SELECT card_json FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    if row is None:
        return "0"
    try:
        card = json.loads(row["card_json"])
    except (TypeError, json.JSONDecodeError):
        return "0"
    return str(card.get("claim_epoch") or 0)


def enqueue_callback(
    conn: sqlite3.Connection,
    task_id: str,
    origin_thread_id: str,
    transition: str,
    *,
    provider: str = "",
    episode_id: str | None = None,
    event_id: str = "",
    request_id: str = "",
) -> bool:
    """Enqueue one durable outbox entry. Returns True if newly enqueued,
    False if ineligible or already present (at-most-once dedup via the
    UNIQUE constraint, scoped to the current claim episode)."""
    if transition not in CALLBACK_ELIGIBLE_TRANSITIONS:
        return False
    validated_thread = str(origin_thread_id or "").strip()
    validated_provider = str(provider or "").strip().lower()
    if validated_provider not in ("", "codex", "claude", "copilot"):
        return False
    if not validated_thread:
        return False
    _ensure_callback_outbox_table(conn)
    resolved_episode = episode_id if episode_id is not None else current_claim_episode(conn, task_id)
    now = utc_now()
    try:
        conn.execute(
            """
            INSERT INTO callback_outbox(
              task_id, provider, origin_thread_id, transition, episode_id, event_id,
              request_id, state, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (task_id, validated_provider, validated_thread, transition, resolved_episode, event_id, request_id, now, now),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        return False
    append_event(conn, task_id, "callback_enqueued", "", {"transition": transition, "episode_id": resolved_episode, "provider": validated_provider})
    return True


def _task_still_in_matching_terminal_state(
    conn: sqlite3.Connection, task_id: str, transition: str, episode_id: str,
) -> bool:
    """Is ``task_id`` STILL in the eligible review terminal state that
    produced ``transition``, right now? A task that was reject-review'd or
    reclaimed after this entry was enqueued no longer matches, and the
    caller must supersede rather than deliver a stale wake."""
    row = conn.execute("SELECT card_json, status, worker_status, archived_at FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    if row is None:
        return False
    try:
        card = json.loads(row["card_json"]) if row["card_json"] else {}
    except (TypeError, json.JSONDecodeError):
        card = {}
    if not isinstance(card, dict):
        card = {}
    # ``card_json`` is the immutable task-card snapshot while the dedicated
    # lifecycle columns are the live canonical state.  Native task-engine
    # transitions update those columns directly, so a stale card-level
    # ``status``/``worker_status`` must never override the live row and make a
    # genuine review callback look superseded.
    merged = {**card, **dict(row)}
    current_status = task_store.canonical_status(merged)
    current_episode = str(card.get("claim_epoch") or 0)
    if current_episode != str(episode_id):
        return False
    raw_terminal = card.get("terminal_outcome") or current_status
    if _normalize_callback_transition(raw_terminal) != transition:
        return False
    if transition == "blocked":
        return current_status in ("review", "blocked")
    return current_status == "review"


_CALLBACK_TRANSITION_MAP: dict[str, str] = {
    "review": "review_ready",
    "review_ready": "review_ready",
    "ready_for_review": "review_ready",
    "codex_review": "review_ready",
    "awaiting_review": "review_ready",
    "blocked": "blocked",
    "deferred": "blocked",
    "promotion_conflict": "blocked",
    "finalize_failed": "blocked",
    "monitor_error": "blocked",
    "process_lost": "blocked",
    "stale": "blocked",
    "launch_failed": "launch_failed",
    "worker_failed": "launch_failed",
    "exited_without_review": "launch_failed",
    "validation_failed": "validation_failed",
    "scope_rejected": "scope_rejected",
    "timed_out": "timed_out",
    "cancelled": "cancelled",
    "canceled": "cancelled",
}


def _normalize_callback_transition(raw_state: str | None) -> str | None:
    if not raw_state:
        return None
    key = str(raw_state).strip().lower()
    if not key:
        return None
    if key.startswith("blocked"):
        return "blocked"
    return _CALLBACK_TRANSITION_MAP.get(key)


# --- Batched delivery ---------------------------------------------------
#
# A batch coalesces every currently-eligible pending outbox row for ONE
# origin_thread_id into a single leased delivery, so one transport turn
# covers the whole current terminal backlog for that thread instead of one
# turn per task.

def _reclaim_expired_batches(conn: sqlite3.Connection, now_iso: str) -> None:
    """Revert any inflight batch (and its still-inflight member rows) whose
    lease expired back to pending -- a crashed bridge's claim is recoverable
    with the SAME durable batch identity/membership."""
    expired = conn.execute(
        "SELECT batch_id FROM callback_batches "
        "WHERE state='inflight' AND lease_expires_at != '' AND lease_expires_at < ?",
        (now_iso,),
    ).fetchall()
    for row in expired:
        batch_id = row["batch_id"]
        conn.execute(
            "UPDATE callback_batches SET state='pending', lease_id='', lease_expires_at='', "
            "not_before_at='' WHERE batch_id=?",
            (batch_id,),
        )
        conn.execute(
            "UPDATE callback_outbox SET state='pending' WHERE batch_id=? AND state='inflight'",
            (batch_id,),
        )


def _supersede_stale_batch_members(conn: sqlite3.Connection, batch_id: str) -> int:
    """Mark any member of ``batch_id`` whose task is no longer in the
    matching eligible terminal state/episode as superseded. Returns the
    count of members still eligible after pruning."""
    members = conn.execute(
        "SELECT outbox_id, task_id, transition, episode_id FROM callback_outbox "
        "WHERE batch_id=? AND state IN ('pending', 'inflight')",
        (batch_id,),
    ).fetchall()
    remaining = 0
    for member in members:
        if _task_still_in_matching_terminal_state(
            conn, member["task_id"], member["transition"], member["episode_id"],
        ):
            remaining += 1
            continue
        conn.execute(
            """
            UPDATE callback_outbox
            SET state='superseded',
                last_error='task_no_longer_in_matching_terminal_state_or_episode',
                updated_at=?
            WHERE outbox_id=?
            """,
            (utc_now(), member["outbox_id"]),
        )
        append_event(
            conn, member["task_id"], "callback_superseded", "",
            {"transition": member["transition"], "batch_id": batch_id},
        )
    return remaining


def claim_pending_callback_batch(
    conn: sqlite3.Connection,
    lease_seconds: int = 120,
    max_members: int = DEFAULT_CALLBACK_BATCH_MAX_MEMBERS,
    provider: str = "",
) -> dict | None:
    """Atomically claim one delivery batch, or None if nothing is claimable.

    Prefers resuming an existing 'pending' batch over forming a new one --
    FIFO by creation order. A new batch coalesces every currently-
    unassigned pending row for the same origin_thread_id (bounded to
    ``max_members``). Never two inflight batches for the same thread.
    Non-blocking scheduled retry: a 'pending' batch whose ``not_before_at``
    is still in the future is skipped, never blocking other threads."""
    _ensure_callback_outbox_table(conn)
    _ensure_callback_batches_table(conn)
    provider = str(provider or "").strip().lower()
    provider_where = " AND provider=?" if provider else ""
    while True:
        conn.execute("BEGIN IMMEDIATE")
        try:
            now_iso = utc_now()
            _reclaim_expired_batches(conn, now_iso)

            inflight_threads = {
                row["origin_thread_id"]
                for row in conn.execute(
                    "SELECT DISTINCT origin_thread_id FROM callback_batches WHERE state='inflight'" + provider_where,
                    (provider,) if provider else (),
                ).fetchall()
            }

            due_pending_batch = None
            for row in conn.execute(
                "SELECT * FROM callback_batches WHERE state='pending' " + provider_where +
                " ORDER BY created_at ASC, batch_id ASC",
                (provider,) if provider else (),
            ).fetchall():
                if row["origin_thread_id"] in inflight_threads:
                    continue
                not_before = row["not_before_at"] or ""
                if not_before and not_before > now_iso:
                    continue
                due_pending_batch = row
                break

            if due_pending_batch is None:
                reserved_threads = inflight_threads | {
                    row["origin_thread_id"]
                    for row in conn.execute(
                        "SELECT DISTINCT origin_thread_id FROM callback_batches WHERE state='pending'" + provider_where,
                        (provider,) if provider else (),
                    ).fetchall()
                }
                oldest_unassigned = None
                for row in conn.execute(
                    "SELECT origin_thread_id FROM callback_outbox "
                    "WHERE state='pending' AND batch_id='' " + provider_where +
                    " ORDER BY created_at ASC, outbox_id ASC",
                    (provider,) if provider else (),
                ).fetchall():
                    if row["origin_thread_id"] not in reserved_threads:
                        oldest_unassigned = row
                        break
                if oldest_unassigned is None:
                    conn.commit()
                    return None
                thread_id = oldest_unassigned["origin_thread_id"]
                candidates = conn.execute(
                    "SELECT outbox_id FROM callback_outbox "
                    "WHERE state='pending' AND batch_id='' AND origin_thread_id=? " + provider_where +
                    " ORDER BY created_at ASC, outbox_id ASC LIMIT ?",
                    (thread_id, provider, max(1, int(max_members))) if provider else (thread_id, max(1, int(max_members))),
                ).fetchall()
                batch_id = uuid.uuid4().hex
                for candidate in candidates:
                    conn.execute(
                        "UPDATE callback_outbox SET batch_id=? WHERE outbox_id=?",
                        (batch_id, candidate["outbox_id"]),
                    )
                conn.execute(
                    """
                    INSERT INTO callback_batches(
                      batch_id, provider, origin_thread_id, state, created_at, updated_at, member_count
                    ) VALUES (?, ?, ?, 'pending', ?, ?, ?)
                    """,
                    (batch_id, provider, thread_id, now_iso, now_iso, len(candidates)),
                )
            else:
                batch_id = due_pending_batch["batch_id"]

            remaining = _supersede_stale_batch_members(conn, batch_id)
            if remaining == 0:
                conn.execute(
                    "UPDATE callback_batches SET state='superseded', updated_at=? WHERE batch_id=?",
                    (utc_now(), batch_id),
                )
                conn.commit()
                continue

            lease_id = uuid.uuid4().hex
            lease_expires_epoch = datetime.now(timezone.utc).timestamp() + max(1, lease_seconds)
            lease_expires_iso = datetime.fromtimestamp(
                lease_expires_epoch, tz=timezone.utc
            ).isoformat()
            updated = conn.execute(
                """
                UPDATE callback_batches
                SET state='inflight', lease_id=?, lease_expires_at=?,
                    attempts=attempts+1, updated_at=?, not_before_at=''
                WHERE batch_id=? AND state='pending'
                """,
                (lease_id, lease_expires_iso, utc_now(), batch_id),
            )
            if updated.rowcount != 1:
                conn.rollback()
                continue
            conn.execute(
                "UPDATE callback_outbox SET state='inflight', updated_at=? "
                "WHERE batch_id=? AND state='pending'",
                (utc_now(), batch_id),
            )
            batch_row = conn.execute(
                "SELECT * FROM callback_batches WHERE batch_id=?", (batch_id,)
            ).fetchone()
            member_rows = conn.execute(
                "SELECT * FROM callback_outbox WHERE batch_id=? AND state='inflight' "
                "ORDER BY created_at ASC, outbox_id ASC",
                (batch_id,),
            ).fetchall()
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return {
            "batch_id": batch_id,
            "origin_thread_id": batch_row["origin_thread_id"],
            "lease_id": lease_id,
            "lease_expires_at": lease_expires_iso,
            "attempts": int(batch_row["attempts"]),
            "members": [dict(m) for m in member_rows],
        }


def mark_batch_delivered(conn: sqlite3.Connection, batch_id: str) -> None:
    """Mark every still-inflight member of a delivered batch delivered,
    together, only after the matching turn/completed."""
    _ensure_callback_batches_table(conn)
    now = utc_now()
    conn.execute(
        "UPDATE callback_outbox SET state='delivered', updated_at=? WHERE batch_id=? AND state='inflight'",
        (now, batch_id),
    )
    conn.execute(
        "UPDATE callback_batches SET state='delivered', updated_at=? WHERE batch_id=?",
        (now, batch_id),
    )
    conn.commit()


def acknowledge_callback_batch(
    conn: sqlite3.Connection,
    batch_id: str,
    lease_id: str,
    *,
    provider: str,
    origin_thread_id: str,
) -> bool:
    """Acknowledge one exact leased batch after a manager received it.

    The lease, provider and originating thread must all match.  This keeps
    the MCP long-poll path two-phase: a dropped tool response is reclaimed
    after lease expiry instead of being falsely recorded as delivered.
    """
    _ensure_callback_batches_table(conn)
    row = conn.execute(
        "SELECT state, lease_id, provider, origin_thread_id "
        "FROM callback_batches WHERE batch_id=?",
        (batch_id,),
    ).fetchone()
    if row is None:
        return False
    if (
        row["state"] != "inflight"
        or row["lease_id"] != lease_id
        or str(row["provider"] or "").lower() != str(provider or "").lower()
        or row["origin_thread_id"] != origin_thread_id
    ):
        return False
    mark_batch_delivered(conn, batch_id)
    return True


def rebind_pending_callbacks(
    conn: sqlite3.Connection,
    *,
    provider: str,
    origin_thread_id: str,
) -> int:
    """Move only durable, unconsumed callbacks to a verified manager route.

    This is the reload/session-rollover path.  Inflight deliveries are never
    stolen.  Existing pending batches are superseded and their members become
    unbatched so the new manager can claim them immediately.
    """
    _ensure_callback_outbox_table(conn)
    _ensure_callback_batches_table(conn)
    provider = str(provider or "").strip().lower()
    origin_thread_id = str(origin_thread_id or "").strip()
    if not provider or not origin_thread_id:
        raise ValueError("provider and origin_thread_id are required")
    conn.execute("BEGIN IMMEDIATE")
    try:
        rows = conn.execute(
            "SELECT outbox_id, batch_id FROM callback_outbox "
            "WHERE provider=? AND state='pending' AND origin_thread_id<>?",
            (provider, origin_thread_id),
        ).fetchall()
        batch_ids = {str(row["batch_id"] or "") for row in rows if row["batch_id"]}
        now = utc_now()
        for batch_id in batch_ids:
            conn.execute(
                "UPDATE callback_batches SET state='superseded', lease_id='', "
                "lease_expires_at='', updated_at=? WHERE batch_id=? AND state='pending'",
                (now, batch_id),
            )
        if rows:
            conn.execute(
                "UPDATE callback_outbox SET origin_thread_id=?, batch_id='', "
                "lease_id='', lease_expires_at='', last_error='', updated_at=? "
                "WHERE provider=? AND state='pending' AND origin_thread_id<>?",
                (origin_thread_id, now, provider, origin_thread_id),
            )
        conn.commit()
        return len(rows)
    except Exception:
        conn.rollback()
        raise


def _iso_plus_seconds(now_iso: str, delay_seconds: float) -> str:
    if delay_seconds <= 0:
        return ""
    return (datetime.fromisoformat(now_iso) + timedelta(seconds=delay_seconds)).isoformat()


def defer_batch_busy(
    conn: sqlite3.Connection, batch_id: str, error: str = "", *, delay_seconds: float = 0.0,
) -> None:
    """Return an inflight batch (and every member) to pending after a
    BUSY/active-thread deferral. Non-blocking: schedules the next eligible
    claim time via ``not_before_at``. Never increments
    ``hard_failure_count`` and never dead-letters."""
    _ensure_callback_batches_table(conn)
    now = utc_now()
    not_before = _iso_plus_seconds(now, delay_seconds)
    bounded_error = str(error)[:500]
    conn.execute(
        "UPDATE callback_outbox SET state='pending', updated_at=? WHERE batch_id=? AND state='inflight'",
        (now, batch_id),
    )
    conn.execute(
        """
        UPDATE callback_batches
        SET state='pending', lease_id='', lease_expires_at='', last_error=?,
            updated_at=?, not_before_at=?, last_failure_kind='busy'
        WHERE batch_id=?
        """,
        (bounded_error, now, not_before, batch_id),
    )
    conn.commit()


def fail_batch_transient(
    conn: sqlite3.Connection,
    batch_id: str,
    error: str = "",
    *,
    max_retries: int,
    delay_seconds: float = 0.0,
) -> str:
    """Handle one GENUINE (non-busy) delivery failure for a batch. Bumps
    ``hard_failure_count`` and either reschedules the batch non-blockingly
    or dead-letters it once the budget is exhausted. Returns ``"requeued"``
    or ``"dead_letter"``."""
    _ensure_callback_batches_table(conn)
    row = conn.execute(
        "SELECT hard_failure_count FROM callback_batches WHERE batch_id=?", (batch_id,)
    ).fetchone()
    new_count = (int(row["hard_failure_count"]) if row is not None else 0) + 1
    now = utc_now()
    bounded_error = str(error)[:500]
    if new_count >= max(1, int(max_retries)):
        conn.execute(
            "UPDATE callback_outbox SET state='dead_letter', last_error=?, updated_at=? "
            "WHERE batch_id=? AND state='inflight'",
            (bounded_error, now, batch_id),
        )
        conn.execute(
            """
            UPDATE callback_batches
            SET state='dead_letter', last_error=?, updated_at=?,
                hard_failure_count=?, last_failure_kind='transient_error'
            WHERE batch_id=?
            """,
            (bounded_error, now, new_count, batch_id),
        )
        conn.commit()
        return "dead_letter"
    not_before = _iso_plus_seconds(now, delay_seconds)
    conn.execute(
        "UPDATE callback_outbox SET state='pending', updated_at=? WHERE batch_id=? AND state='inflight'",
        (now, batch_id),
    )
    conn.execute(
        """
        UPDATE callback_batches
        SET state='pending', lease_id='', lease_expires_at='', last_error=?,
            updated_at=?, hard_failure_count=?, not_before_at=?,
            last_failure_kind='transient_error'
        WHERE batch_id=?
        """,
        (bounded_error, now, new_count, not_before, batch_id),
    )
    conn.commit()
    return "requeued"


def mark_batch_dead_letter(conn: sqlite3.Connection, batch_id: str, error: str = "") -> None:
    """Dead-letter every still-inflight member of a batch together."""
    _ensure_callback_batches_table(conn)
    now = utc_now()
    bounded_error = str(error)[:500]
    conn.execute(
        "UPDATE callback_outbox SET state='dead_letter', last_error=?, updated_at=? "
        "WHERE batch_id=? AND state='inflight'",
        (bounded_error, now, batch_id),
    )
    conn.execute(
        "UPDATE callback_batches SET state='dead_letter', last_error=?, updated_at=? WHERE batch_id=?",
        (bounded_error, now, batch_id),
    )
    conn.commit()


def recover_dead_letter_row(conn: sqlite3.Connection, outbox_id: int) -> tuple[bool, str]:
    """Coordinator-only audited recovery for one dead-lettered outbox row.
    Requeues it (fresh batch on the next claim) only if the task is STILL
    in the matching eligible terminal state/episode; otherwise supersedes
    it. Returns (ok, action)."""
    _ensure_callback_outbox_table(conn)
    row = conn.execute("SELECT * FROM callback_outbox WHERE outbox_id=?", (outbox_id,)).fetchone()
    if row is None:
        return False, "not_found"
    if row["state"] != "dead_letter":
        return False, "not_dead_letter"
    now = utc_now()
    if _task_still_in_matching_terminal_state(conn, row["task_id"], row["transition"], row["episode_id"]):
        conn.execute(
            """
            UPDATE callback_outbox
            SET state='pending', batch_id='', attempts=0, lease_id='',
                lease_expires_at='', last_error='', updated_at=?
            WHERE outbox_id=?
            """,
            (now, outbox_id),
        )
        conn.commit()
        append_event(
            conn, row["task_id"], "callback_dead_letter_recovered", "",
            {"transition": row["transition"], "outbox_id": outbox_id},
        )
        return True, "requeued"
    conn.execute(
        """
        UPDATE callback_outbox
        SET state='superseded', last_error='task_no_longer_in_matching_terminal_state_or_episode',
            updated_at=?
        WHERE outbox_id=?
        """,
        (now, outbox_id),
    )
    conn.commit()
    append_event(
        conn, row["task_id"], "callback_dead_letter_superseded", "",
        {"transition": row["transition"], "outbox_id": outbox_id},
    )
    return True, "superseded"


def callback_batch_stats(conn: sqlite3.Connection) -> dict:
    """Redacted-safe batch-level health: per-state batch counts, current
    inflight batch size, oldest pending batch age, and a bounded non-secret
    last error -- never a full origin_thread_id or callback payload text."""
    _ensure_callback_batches_table(conn)
    counts = {state: 0 for state in CALLBACK_BATCH_STATES}
    for row in conn.execute("SELECT state, COUNT(*) AS n FROM callback_batches GROUP BY state"):
        if row["state"] in counts:
            counts[row["state"]] = int(row["n"])
    oldest_pending = conn.execute(
        "SELECT created_at FROM callback_batches WHERE state='pending' ORDER BY created_at ASC LIMIT 1"
    ).fetchone()
    oldest_pending_age_seconds = 0.0
    if oldest_pending is not None:
        try:
            created = datetime.fromisoformat(oldest_pending["created_at"])
            oldest_pending_age_seconds = max(0.0, (datetime.now(timezone.utc) - created).total_seconds())
        except (TypeError, ValueError):
            oldest_pending_age_seconds = 0.0
    inflight_batch = conn.execute(
        "SELECT member_count FROM callback_batches WHERE state='inflight' ORDER BY updated_at DESC LIMIT 1"
    ).fetchone()
    last_dead_letter = conn.execute(
        "SELECT batch_id, origin_thread_id, member_count, updated_at, last_error "
        "FROM callback_batches WHERE state='dead_letter' ORDER BY updated_at DESC LIMIT 1"
    ).fetchone()
    waiting_for_thread_idle = conn.execute(
        "SELECT COUNT(*) AS n FROM callback_batches WHERE state='pending' AND last_failure_kind='busy'"
    ).fetchone()
    pending_genuine_failure = conn.execute(
        "SELECT COUNT(*) AS n FROM callback_batches WHERE state='pending' AND last_failure_kind='transient_error'"
    ).fetchone()
    return {
        "total": sum(counts.values()),
        "by_state": counts,
        "inflight_batch_member_count": int(inflight_batch["member_count"]) if inflight_batch else 0,
        "oldest_pending_batch_age_seconds": oldest_pending_age_seconds,
        "waiting_for_thread_idle_batch_count": int(waiting_for_thread_idle["n"]),
        "pending_genuine_failure_batch_count": int(pending_genuine_failure["n"]),
        "last_dead_letter_batch_member_count": int(last_dead_letter["member_count"]) if last_dead_letter else 0,
        "last_dead_letter_batch_at": last_dead_letter["updated_at"] if last_dead_letter else "",
        "last_dead_letter_batch_error": (last_dead_letter["last_error"] or "")[:300] if last_dead_letter else "",
    }


def callback_outbox_stats(conn: sqlite3.Connection) -> dict:
    """Redacted-safe outbox health: bound/unbound task counts, per-state
    outbox counts, last delivery, and last dead-letter -- never a full
    origin_thread_id, only a short trailing suffix elsewhere."""
    _ensure_callback_outbox_table(conn)
    _ensure_tasks_origin_thread_column(conn)
    counts = {state: 0 for state in CALLBACK_OUTBOX_STATES}
    for row in conn.execute("SELECT state, COUNT(*) AS n FROM callback_outbox GROUP BY state"):
        if row["state"] in counts:
            counts[row["state"]] = int(row["n"])
    last_delivered = conn.execute(
        "SELECT task_id, transition, updated_at FROM callback_outbox "
        "WHERE state='delivered' ORDER BY updated_at DESC LIMIT 1"
    ).fetchone()
    last_dead_letter = conn.execute(
        "SELECT task_id, transition, updated_at, last_error FROM callback_outbox "
        "WHERE state='dead_letter' ORDER BY updated_at DESC LIMIT 1"
    ).fetchone()
    bound_task_count = origin_thread_bound_count(conn)
    total_task_count = task_count(conn)
    return {
        "total": sum(counts.values()),
        "by_state": counts,
        "threads_bound": bound_task_count,
        "bound_task_count": bound_task_count,
        "unbound_task_count": max(0, total_task_count - bound_task_count),
        "last_delivered_task_id": last_delivered["task_id"] if last_delivered else "",
        "last_delivered_transition": last_delivered["transition"] if last_delivered else "",
        "last_delivered_at": last_delivered["updated_at"] if last_delivered else "",
        "last_dead_letter_task_id": last_dead_letter["task_id"] if last_dead_letter else "",
        "last_dead_letter_transition": last_dead_letter["transition"] if last_dead_letter else "",
        "last_dead_letter_at": last_dead_letter["updated_at"] if last_dead_letter else "",
        "last_dead_letter_error": (last_dead_letter["last_error"] or "")[:300] if last_dead_letter else "",
        "batches": callback_batch_stats(conn),
    }


__all__ = [
    "SCHEMA_ID",
    "DEFAULT_CALLBACK_BATCH_MAX_MEMBERS",
    "CALLBACK_ELIGIBLE_TRANSITIONS",
    "CALLBACK_OUTBOX_STATES",
    "CALLBACK_BATCH_STATES",
    "CallbackStoreError",
    "CallbackStoreNotReadyError",
    "utc_now",
    "resolve_db_path",
    "open_db",
    "init_db",
    "append_event",
    "task_count",
    "origin_thread_bound_count",
    "read_origin_thread",
    "current_claim_episode",
    "enqueue_callback",
    "claim_pending_callback_batch",
    "mark_batch_delivered",
    "defer_batch_busy",
    "fail_batch_transient",
    "mark_batch_dead_letter",
    "recover_dead_letter_row",
    "callback_batch_stats",
    "callback_outbox_stats",
]
