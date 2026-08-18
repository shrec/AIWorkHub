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

# Canonical callback payload states. Unknown values remain ineligible.
CALLBACK_ELIGIBLE_TRANSITIONS: frozenset[str] = frozenset({
    "review_ready",
    "blocked",
    "launch_failed",
    "worker_failed",
    "validation_failed",
    "scope_rejected",
    "timed_out",
    "token_budget_exceeded",
    "output_budget_exceeded",
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

    Dedup scope is per CLAIM/REVIEW EPISODE, not per task lifetime, using
    (task_id, transition, origin_thread_id, episode_id, request_id). Legacy
    databases may also carry the equivalent table-level UNIQUE constraint;
    fresh databases enforce the same identity in ``enqueue_callback``'s
    atomic INSERT...SELECT predicate."""
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
          recovery_count INTEGER NOT NULL DEFAULT 0,
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
        ("recovery_count", "INTEGER NOT NULL DEFAULT 0"),
        ("last_error", "TEXT NOT NULL DEFAULT ''"),
        ("lease_id", "TEXT NOT NULL DEFAULT ''"),
        ("lease_expires_at", "TEXT NOT NULL DEFAULT ''"),
    ):
        if not _column_exists(conn, "callback_outbox", column):
            try:
                conn.execute(f"ALTER TABLE callback_outbox ADD COLUMN {column} {ddl}")
            except sqlite3.OperationalError as exc:
                # Two extension/dispatcher clients may initialize the same
                # repo immediately after reload. Both can observe a missing
                # column before one wins ALTER TABLE. The loser's duplicate
                # is success only after the column is independently visible;
                # every other schema/database error remains fail-closed.
                if "duplicate column name" not in str(exc).lower() or not _column_exists(
                    conn, "callback_outbox", column
                ):
                    raise
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
            try:
                conn.execute(f"ALTER TABLE callback_batches ADD COLUMN {column} {ddl}")
            except sqlite3.OperationalError as exc:
                # Reload/activation can initialize the same repository from
                # the extension child and dispatcher thread concurrently.
                # Both may observe the pre-migration schema before one wins
                # ALTER TABLE.  The loser's duplicate-column error is a
                # successful convergence only when the column is now really
                # visible; every other database error remains fail-closed.
                if "duplicate column name" not in str(exc).lower() or not _column_exists(
                    conn, "callback_batches", column
                ):
                    raise
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
    """Enqueue one durable outbox entry, exactly once per route, transition,
    and episode. Two DIFFERENT eligible transitions for the same task/route
    within a single claim episode (e.g. review_ready, then blocked when the
    reconciler damages the review workspace without bumping claim_epoch) are
    each their own wake and must each enqueue -- the dedup keys on transition
    too, never only (task, provider, route, episode)."""
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
        cur = conn.execute(
            """
            INSERT INTO callback_outbox(
              task_id, provider, origin_thread_id, transition, episode_id, event_id,
              request_id, state, created_at, updated_at
            )
            SELECT ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?
             WHERE NOT EXISTS (
               SELECT 1 FROM callback_outbox
                WHERE task_id=? AND provider=? AND origin_thread_id=?
                  AND transition=? AND episode_id=?
             )
            """,
            (
                task_id, validated_provider, validated_thread, transition,
                resolved_episode, event_id, request_id, now, now,
                task_id, validated_provider, validated_thread, transition, resolved_episode,
            ),
        )
        if cur.rowcount != 1:
            conn.rollback()
            return False
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        return False
    except sqlite3.Error:
        # The caller owns the connection, but this function owns the
        # transaction it started. Never return/raise with that shared
        # connection still holding an aborted or open write transaction.
        conn.rollback()
        raise
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
    # B921: ``task_store.mark_terminal_review`` writes the authoritative
    # substatus to ``card["terminal_substatus"]`` -- there has never been a
    # ``card["terminal_outcome"]`` field written anywhere in this codebase,
    # so reading that name here always fell through to the generic
    # ``current_status`` bucket ("review"), which coincidentally always
    # normalizes to "review_ready". That silently matched only a
    # (formerly always-hardcoded) "review_ready" transition; once the
    # enqueue side derives the REAL transition from the substatus, this
    # recheck must derive its expectation the exact same way or every
    # non-review_ready callback would mismatch here and be superseded
    # (silently dropped) instead of delivered.
    terminal_substatus = str(card.get("terminal_substatus") or "").strip()
    if terminal_substatus:
        # An explicit substatus is the authoritative signal for exactly
        # which transition the task is currently in.
        if resolve_callback_transition(terminal_substatus) != transition:
            return False
    elif resolve_callback_transition(current_status) != transition and current_status != "blocked":
        # No substatus recorded (e.g. a worker-crash path that only ever
        # set the coarse status/worker_status columns): a "blocked"
        # status alone is ambiguous between the literal "blocked"
        # transition and every other terminal-failure transition that
        # also lands the task in "blocked" -- defer to the status-based
        # checks below instead of rejecting on this coarse mismatch.
        return False
    if transition == "blocked":
        return current_status in ("review", "blocked")
    if current_status == "blocked":
        return transition in {
            "launch_failed",
            "worker_failed",
            "timed_out",
            "token_budget_exceeded",
            "output_budget_exceeded",
            "cancelled",
        }
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
    # dependency_blocked / liveness_lost do not start with "blocked", so without
    # an explicit mapping they used to fall through to the review_ready fallback
    # (B921 substatus-truth: a blocked outcome must never be delivered as
    # review_ready). Map them to their real failure class.
    "dependency_blocked": "blocked",
    "liveness_lost": "blocked",
    "launch_failed": "launch_failed",
    # A provider/adapter that never starts is launch_failed.  A started worker
    # whose supervisor exits unsuccessfully is a distinct worker_failed
    # outcome and must remain distinct in callbacks/UI diagnostics.
    "worker_failed": "worker_failed",
    "exited_without_review": "launch_failed",
    "validation_failed": "validation_failed",
    "required_output_unchanged": "validation_failed",
    "scope_rejected": "scope_rejected",
    "timed_out": "timed_out",
    "token_budget_exceeded": "token_budget_exceeded",
    "output_budget_exceeded": "output_budget_exceeded",
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


def normalize_callback_transition(raw_state: str | None) -> str | None:
    """Public wrapper: map an authoritative terminal status/substatus string
    (e.g. ``card["terminal_substatus"]``) to its canonical callback
    transition, or ``None`` if unrecognized. The sole source of truth for
    this mapping (``_CALLBACK_TRANSITION_MAP``) so every enqueue/supersede
    call site derives the SAME transition from the SAME authoritative
    substatus -- never a caller-hardcoded literal that can drift from it
    (B917: a hardcoded ``"review_ready"`` enqueue produced a compact callback
    reading ``review_ready`` for a task whose retained ``terminal_review``
    event and ``terminal_substatus`` were ``validation_failed``)."""
    return _normalize_callback_transition(raw_state)


def resolve_callback_transition(raw_state: str | None) -> str:
    """``normalize_callback_transition`` with the one fallback every real
    caller needs: a substatus with no explicit failure-class mapping (e.g.
    a plain successful ``"exited"``, which was never added to
    ``_CALLBACK_TRANSITION_MAP``) resolves to ``"review_ready"`` rather than
    ``None``. This is the single source of truth BOTH the enqueue side
    (``task_engine.mark_terminal_review``) and the delivery-eligibility
    recheck (``_task_still_in_matching_terminal_state``) must call so they
    can never diverge again the way B921 found them diverged."""
    return _normalize_callback_transition(raw_state) or "review_ready"


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


def recover_incompatible_long_inflight_batches(
    conn: sqlite3.Connection,
    *,
    provider: str,
    max_lease_span_seconds: float,
) -> int:
    """Atomically recover inflight batches created by an incompatible,
    longer-lived transport generation.

    The VS Code callback owner moved from a separate App Server subprocess
    (35-minute lease) to the bounded local sideband mux (90-second lease).
    After an abrupt extension reload the old owner no longer exists, but its
    durable lease can otherwise block the new transport for over half an
    hour.  Only batches whose persisted claim-time-to-expiry span exceeds the
    current transport's declared maximum are recovered; current-generation
    inflight work is never touched.
    """
    _ensure_callback_outbox_table(conn)
    _ensure_callback_batches_table(conn)
    normalized_provider = str(provider or "").strip().lower()
    if not normalized_provider or max_lease_span_seconds <= 0:
        return 0

    recovered = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        rows = conn.execute(
            "SELECT batch_id,updated_at,lease_expires_at FROM callback_batches "
            "WHERE state='inflight' AND provider=? AND lease_expires_at<>''",
            (normalized_provider,),
        ).fetchall()
        now = utc_now()
        for row in rows:
            try:
                claimed_at = datetime.fromisoformat(str(row["updated_at"]).replace("Z", "+00:00"))
                expires_at = datetime.fromisoformat(str(row["lease_expires_at"]).replace("Z", "+00:00"))
                lease_span = (expires_at - claimed_at).total_seconds()
            except (TypeError, ValueError):
                continue
            if lease_span <= float(max_lease_span_seconds):
                continue
            batch_id = str(row["batch_id"])
            conn.execute(
                "UPDATE callback_outbox SET state='pending', updated_at=?, "
                "last_error='legacy_long_lease_recovered_for_sideband' "
                "WHERE batch_id=? AND state='inflight'",
                (now, batch_id),
            )
            conn.execute(
                "UPDATE callback_batches SET state='pending', lease_id='', "
                "lease_expires_at='', not_before_at='', updated_at=?, "
                "last_error='legacy_long_lease_recovered_for_sideband', "
                "last_failure_kind='transport_upgrade_recovery' "
                "WHERE batch_id=? AND state='inflight'",
                (now, batch_id),
            )
            recovered += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return recovered


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
    origin_thread_id: str = "",
) -> dict | None:
    """Atomically claim one delivery batch, or None if nothing is claimable.

    Prefers resuming an existing 'pending' batch over forming a new one --
    FIFO by creation order. A new batch coalesces every currently-
    unassigned pending row for the same origin_thread_id (bounded to
    ``max_members``). Never two inflight batches for the same thread.
    Non-blocking scheduled retry: a 'pending' batch whose ``not_before_at``
    is still in the future is skipped, never blocking other threads.

    ``origin_thread_id``, when non-empty, scopes claiming to exactly that
    route's own batches -- a caller that knows its own verified route's
    bound session/thread identity never claims (and is never handed) a
    different route's callback, even when both share the same provider.
    Default '' preserves the existing provider-wide claim behavior."""
    _ensure_callback_outbox_table(conn)
    _ensure_callback_batches_table(conn)
    provider = str(provider or "").strip().lower()
    origin_thread_id = str(origin_thread_id or "").strip()
    provider_where = " AND provider=?" if provider else ""
    thread_where = " AND origin_thread_id=?" if origin_thread_id else ""
    scope_params = tuple(p for p in (provider, origin_thread_id) if p)
    while True:
        conn.execute("BEGIN IMMEDIATE")
        try:
            now_iso = utc_now()
            _reclaim_expired_batches(conn, now_iso)

            inflight_threads = {
                row["origin_thread_id"]
                for row in conn.execute(
                    "SELECT DISTINCT origin_thread_id FROM callback_batches WHERE state='inflight'" + provider_where + thread_where,
                    scope_params,
                ).fetchall()
            }

            due_pending_batch = None
            for row in conn.execute(
                "SELECT * FROM callback_batches WHERE state='pending' " + provider_where + thread_where +
                " ORDER BY created_at ASC, batch_id ASC",
                scope_params,
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
                        "SELECT DISTINCT origin_thread_id FROM callback_batches WHERE state='pending'" + provider_where + thread_where,
                        scope_params,
                    ).fetchall()
                }
                oldest_unassigned = None
                for row in conn.execute(
                    "SELECT origin_thread_id FROM callback_outbox "
                    "WHERE state='pending' AND batch_id='' " + provider_where + thread_where +
                    " ORDER BY created_at ASC, outbox_id ASC",
                    scope_params,
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


def has_deliverable_callback(
    conn: sqlite3.Connection, *, provider: str, origin_thread_id: str,
) -> bool:
    """Non-mutating peek: is there a pending, currently-due callback for
    this exact verified route right now?

    Never claims, leases, or mutates anything -- a caller deciding whether
    a prompt delivery attempt is worthwhile (e.g. a push-delivery channel
    surfacing a wake to a verified Claude manager) can check this without
    consuming a claim cycle or racing the real ``claim_pending_callback_batch``
    caller for the lease."""
    _ensure_callback_outbox_table(conn)
    _ensure_callback_batches_table(conn)
    provider = str(provider or "").strip().lower()
    origin_thread_id = str(origin_thread_id or "").strip()
    if not provider or not origin_thread_id:
        return False
    now_iso = utc_now()
    row = conn.execute(
        """
        SELECT o.outbox_id FROM callback_outbox o
        LEFT JOIN callback_batches b ON b.batch_id = o.batch_id AND o.batch_id != ''
        WHERE o.state='pending' AND o.provider=? AND o.origin_thread_id=?
          AND (b.batch_id IS NULL OR (b.state='pending' AND (b.not_before_at='' OR b.not_before_at<=?)))
        LIMIT 1
        """,
        (provider, origin_thread_id, now_iso),
    ).fetchone()
    return row is not None


def _require_lease_id(lease_id: object) -> str:
    """Validate a caller-provided lease token before any mutating finalizer.

    Fail closed: ``lease_id`` must be a non-empty ``str`` -- never ``None``,
    a boolean, an int, or any other silently-coercible identity -- and is
    returned stripped. Missing/invalid tokens raise ``ValueError`` before any
    batch or outbox row is read or written; the current database row's lease
    is never consulted to substitute a missing token.
    """
    if not isinstance(lease_id, str):
        raise ValueError("lease_id is required and must be a non-empty string")
    stripped = lease_id.strip()
    if not stripped:
        raise ValueError("lease_id is required and must be a non-empty string")
    return stripped


def mark_batch_delivered(conn: sqlite3.Connection, batch_id: str, lease_id: str) -> bool:
    """Mark every still-inflight member of a delivered batch delivered,
    together, only after the matching turn/completed.

    Requires the exact caller-provided lease: the batch must still be the
    caller's inflight claim. A stale or mismatched lease fails closed --
    nothing is mutated and ``False`` is returned.
    """
    lease_id = _require_lease_id(lease_id)
    _ensure_callback_batches_table(conn)
    now = utc_now()
    updated = conn.execute(
        """
        UPDATE callback_batches
        SET state='delivered', updated_at=?
        WHERE batch_id=? AND lease_id=? AND state='inflight'
        """,
        (now, batch_id, lease_id),
    )
    if updated.rowcount != 1:
        conn.rollback()
        return False
    conn.execute(
        "UPDATE callback_outbox SET state='delivered', updated_at=? WHERE batch_id=? AND state='inflight'",
        (now, batch_id),
    )
    conn.commit()
    return True


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
    return mark_batch_delivered(conn, batch_id, lease_id)


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


def seed_missing_review_callbacks(
    conn: sqlite3.Connection,
    *,
    provider: str,
    origin_thread_id: str | None = None,
) -> int:
    """Backfill durable callbacks for current review/terminal-failure tasks.

    Normal terminal transitions enqueue callbacks immediately.  A reload,
    stale runtime, or earlier route bug can leave a task already in the
    canonical review bucket without a pending/inflight/delivered outbox row.
    When a verified manager route is available again, seed only those missing
    rows so the dispatcher/manager-inbox can replay the review wake.

    Safety boundaries:
    - Never fabricates a callback provider: only rows owned by ``provider`` are
      considered.
    - Never invents a thread from nothing: either the caller supplies the
      verified current route ``origin_thread_id`` or the task already carries a
      persisted origin.
    - With no explicit verified route, never duplicates or resurrects a task
      that already has any callback row for the same provider/claim episode.
      An explicit verified route remains route-scoped so reload retargeting can
      seed the new origin exactly once.
    - A verified route may recover one matching dead-letter row or one row
      incorrectly superseded by an older eligibility bug, but only while the
      task is still in that exact terminal episode. ``recovery_count`` prevents
      an endlessly failing transport from being resurrected on every poll.
    """
    _ensure_tasks_origin_thread_column(conn)
    _ensure_callback_outbox_table(conn)
    provider = str(provider or "").strip().lower()
    route_origin = str(origin_thread_id or "").strip()
    if provider not in ("codex", "claude", "copilot"):
        return 0
    rows = conn.execute(
        """
        SELECT task_id, status, card_json, origin_thread_id
          FROM tasks
         WHERE status IN ('review', 'blocked')
           AND (archived_at IS NULL OR archived_at='')
         ORDER BY updated_at ASC, task_id ASC
        """
    ).fetchall()
    seeded = 0
    for row in rows:
        task_id = str(row["task_id"] or "")
        try:
            card = json.loads(row["card_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            card = {}
        if not isinstance(card, dict):
            card = {}
        row_provider = str(card.get("coordinator_provider") or provider).strip().lower()
        if row_provider != provider:
            continue
        episode_id = str(card.get("claim_epoch") or 0)
        current_status = str(row["status"] or "").strip()
        raw_terminal = str(card.get("terminal_substatus") or "").strip()
        # Reconciliation starts from an already-canonical review row. Preserve
        # known compact terminal states, but recover an unfamiliar persisted
        # substatus as review_ready so a future/legacy spelling cannot strand
        # the review. Direct enqueue_callback callers remain fail-closed.
        if current_status == "blocked":
            transition = normalize_callback_transition(raw_terminal)
            if transition not in CALLBACK_ELIGIBLE_TRANSITIONS:
                continue
        else:
            transition = resolve_callback_transition(raw_terminal or "review_ready")
        callback_origin = route_origin or str(row["origin_thread_id"] or card.get("origin_thread_id") or "").strip()
        if not callback_origin:
            continue
        def _recover_terminal_row(existing_row) -> None:
            # Recovery re-points the row at the transition the task is in
            # RIGHT NOW (``transition``), never leaving the old one. A row
            # recovered with a stale transition is superseded again by the
            # very next claim (its transition no longer matches the task's
            # live terminal state) and, its single recovery budget already
            # burned here, could then never be recovered again -- the wake
            # would be lost forever on an otherwise stable manager route.
            batch_id = str(existing_row["batch_id"] or "")
            now = utc_now()
            prior_state = str(existing_row["state"] or "")
            if batch_id:
                conn.execute(
                    "UPDATE callback_batches SET state='superseded', updated_at=? "
                    "WHERE batch_id=? AND state IN ('dead_letter','superseded')",
                    (now, batch_id),
                )
            conn.execute(
                "UPDATE callback_outbox SET state='pending', batch_id='', "
                "lease_id='', lease_expires_at='', attempts=0, "
                "transition=?, last_error=?, "
                "recovery_count=recovery_count+1, updated_at=? "
                "WHERE outbox_id=? AND state=? AND recovery_count=0",
                (
                    transition,
                    f"verified_route_{prior_state}_recovery",
                    now,
                    existing_row["outbox_id"],
                    prior_state,
                ),
            )
            conn.commit()
            append_event(
                conn,
                task_id,
                "callback_terminal_recovered_on_verified_route",
                "",
                {
                    "transition": transition,
                    "episode_id": episode_id,
                    "prior_state": prior_state,
                },
            )

        # The dedup lookup keys on ``transition`` too: a task that already
        # has a review_ready row but is now (same episode) blocked has NO
        # wake for its CURRENT transition, and a transition-blind lookup
        # would wrongly treat the review_ready row as "already handled" and
        # skip the gap -- the exact way the direct enqueue predicate did.
        if route_origin:
            existing = conn.execute(
                "SELECT outbox_id,batch_id,state,recovery_count FROM callback_outbox WHERE task_id=? AND provider=? "
                "AND origin_thread_id=? AND transition=? AND episode_id=? LIMIT 1",
                (task_id, provider, callback_origin, transition, episode_id),
            ).fetchone()
        else:
            existing = conn.execute(
                "SELECT outbox_id,batch_id,state,recovery_count FROM callback_outbox WHERE task_id=? AND provider=? "
                "AND transition=? AND episode_id=? LIMIT 1",
                (task_id, provider, transition, episode_id),
            ).fetchone()
        if existing is not None:
            if (
                route_origin
                and str(existing["state"] or "") in {"dead_letter", "superseded"}
                and int(existing["recovery_count"] or 0) == 0
                and _task_still_in_matching_terminal_state(
                    conn, task_id, transition, episode_id
                )
            ):
                _recover_terminal_row(existing)
                seeded += 1
            continue
        # No wake exists for the transition the task is in right now. On a
        # verified route, prefer recovering a dead-letter/superseded row this
        # task stranded under a DIFFERENT transition in the SAME episode and
        # re-pointing that single recoverable row at the current transition,
        # over leaving it stranded and never producing the owed wake. (A
        # review_ready row superseded when the same card was moved to blocked
        # in-episode is exactly this case.)
        if route_origin:
            stranded = conn.execute(
                "SELECT outbox_id,batch_id,state,recovery_count FROM callback_outbox "
                "WHERE task_id=? AND provider=? AND origin_thread_id=? AND episode_id=? "
                "AND state IN ('dead_letter','superseded') AND recovery_count=0 LIMIT 1",
                (task_id, provider, callback_origin, episode_id),
            ).fetchone()
            if stranded is not None and _task_still_in_matching_terminal_state(
                conn, task_id, transition, episode_id
            ):
                _recover_terminal_row(stranded)
                seeded += 1
                continue
        if enqueue_callback(
            conn,
            task_id,
            callback_origin,
            transition,
            provider=provider,
            episode_id=episode_id,
        ):
            seeded += 1
    return seeded


def _iso_plus_seconds(now_iso: str, delay_seconds: float) -> str:
    if delay_seconds <= 0:
        return ""
    return (datetime.fromisoformat(now_iso) + timedelta(seconds=delay_seconds)).isoformat()


def defer_batch_busy(
    conn: sqlite3.Connection,
    batch_id: str,
    error: str,
    lease_id: str,
    *,
    delay_seconds: float = 0.0,
) -> bool:
    """Return an inflight batch (and every member) to pending after a
    BUSY/active-thread deferral. Non-blocking: schedules the next eligible
    claim time via ``not_before_at``. Never increments
    ``hard_failure_count`` and never dead-letters.

    Requires the exact caller-provided lease. A stale or mismatched lease
    fails closed (``False``) without mutating the batch or its members.
    """
    lease_id = _require_lease_id(lease_id)
    _ensure_callback_batches_table(conn)
    now = utc_now()
    not_before = _iso_plus_seconds(now, delay_seconds)
    bounded_error = str(error)[:500]
    updated = conn.execute(
        """
        UPDATE callback_batches
        SET state='pending', lease_id='', lease_expires_at='', last_error=?,
            updated_at=?, not_before_at=?, last_failure_kind='busy'
        WHERE batch_id=? AND lease_id=? AND state='inflight'
        """,
        (bounded_error, now, not_before, batch_id, lease_id),
    )
    if updated.rowcount != 1:
        conn.rollback()
        return False
    conn.execute(
        "UPDATE callback_outbox SET state='pending', updated_at=? WHERE batch_id=? AND state='inflight'",
        (now, batch_id),
    )
    conn.commit()
    return True


def fail_batch_transient(
    conn: sqlite3.Connection,
    batch_id: str,
    error: str,
    lease_id: str,
    *,
    max_retries: int,
    delay_seconds: float = 0.0,
) -> str:
    """Handle one GENUINE (non-busy) delivery failure for a batch. Bumps
    ``hard_failure_count`` and either reschedules the batch non-blockingly
    or dead-letters it once the budget is exhausted.

    Requires the exact caller-provided lease. Returns ``"requeued"``,
    ``"dead_letter"``, or ``"lease_rejected"`` (fail closed: no mutation).
    """
    lease_id = _require_lease_id(lease_id)
    _ensure_callback_batches_table(conn)
    row = conn.execute(
        "SELECT hard_failure_count FROM callback_batches "
        "WHERE batch_id=? AND lease_id=? AND state='inflight'",
        (batch_id, lease_id),
    ).fetchone()
    if row is None:
        return "lease_rejected"
    new_count = int(row["hard_failure_count"]) + 1
    now = utc_now()
    bounded_error = str(error)[:500]
    if new_count >= max(1, int(max_retries)):
        updated = conn.execute(
            """
            UPDATE callback_batches
            SET state='dead_letter', last_error=?, updated_at=?,
                hard_failure_count=?, last_failure_kind='transient_error'
            WHERE batch_id=? AND lease_id=? AND state='inflight'
            """,
            (bounded_error, now, new_count, batch_id, lease_id),
        )
        if updated.rowcount != 1:
            conn.rollback()
            return "lease_rejected"
        conn.execute(
            "UPDATE callback_outbox SET state='dead_letter', last_error=?, updated_at=? "
            "WHERE batch_id=? AND state='inflight'",
            (bounded_error, now, batch_id),
        )
        conn.commit()
        return "dead_letter"
    not_before = _iso_plus_seconds(now, delay_seconds)
    updated = conn.execute(
        """
        UPDATE callback_batches
        SET state='pending', lease_id='', lease_expires_at='', last_error=?,
            updated_at=?, hard_failure_count=?, not_before_at=?,
            last_failure_kind='transient_error'
        WHERE batch_id=? AND lease_id=? AND state='inflight'
        """,
        (bounded_error, now, new_count, not_before, batch_id, lease_id),
    )
    if updated.rowcount != 1:
        conn.rollback()
        return "lease_rejected"
    conn.execute(
        "UPDATE callback_outbox SET state='pending', updated_at=? WHERE batch_id=? AND state='inflight'",
        (now, batch_id),
    )
    conn.commit()
    return "requeued"


def mark_batch_dead_letter(
    conn: sqlite3.Connection,
    batch_id: str,
    error: str,
    lease_id: str,
) -> bool:
    """Dead-letter every still-inflight member of a batch together.

    Requires the exact caller-provided lease. A stale or mismatched lease
    fails closed (``False``) without mutating the batch or its members.
    """
    lease_id = _require_lease_id(lease_id)
    _ensure_callback_batches_table(conn)
    now = utc_now()
    bounded_error = str(error)[:500]
    updated = conn.execute(
        """
        UPDATE callback_batches
        SET state='dead_letter', last_error=?, updated_at=?
        WHERE batch_id=? AND lease_id=? AND state='inflight'
        """,
        (bounded_error, now, batch_id, lease_id),
    )
    if updated.rowcount != 1:
        conn.rollback()
        return False
    conn.execute(
        "UPDATE callback_outbox SET state='dead_letter', last_error=?, updated_at=? "
        "WHERE batch_id=? AND state='inflight'",
        (bounded_error, now, batch_id),
    )
    conn.commit()
    return True


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
    "has_deliverable_callback",
    "recover_incompatible_long_inflight_batches",
    "mark_batch_delivered",
    "defer_batch_busy",
    "fail_batch_transient",
    "mark_batch_dead_letter",
    "recover_dead_letter_row",
    "callback_batch_stats",
    "callback_outbox_stats",
]
