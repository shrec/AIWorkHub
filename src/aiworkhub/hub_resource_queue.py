"""Hub-managed named resources with one persisted FIFO queue (NF-2026-02240).

Every holder and waiter of a named resource such as ``cpu_heavy`` is one row
of the runtime-DB table ``hub_resource_queue``. The head of a resource's queue
is its waiting row with the highest priority, oldest enqueue id first, so equal
priorities are strict FIFO; the head takes the resource only while no row holds
it. A row whose process is provably dead is removed by the next acquirer that
looks, so a crashed holder never wedges the queue. Liveness is the runtime's
single answer, ``platform_io.process_is_alive`` (alive unless proven dead).

Writes go through ``task_store``'s one cross-process writer lease and are short
``BEGIN IMMEDIATE`` transactions; nothing waits while holding the database.
"""

from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import task_store

STATUS_SCHEMA_ID = "aiworkhub.hub_resource_status.v1"
CPU_HEAVY = "cpu_heavy"
KNOWN_RESOURCES: frozenset[str] = frozenset({CPU_HEAVY})
POLL_SECONDS = 0.5
MAX_OWNER_CHARS = 200
MAX_STATUS_ROWS = 200
MIN_PRIORITY = -100
MAX_PRIORITY = 100

_TABLE_STATEMENTS = (
    "CREATE TABLE IF NOT EXISTS hub_resource_queue ("
    " entry_id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " resource TEXT NOT NULL,"
    " owner TEXT NOT NULL,"
    " priority INTEGER NOT NULL DEFAULT 0,"
    " state TEXT NOT NULL CHECK (state IN ('waiting', 'holding')),"
    " pid INTEGER NOT NULL,"
    " enqueued_at REAL NOT NULL,"
    " acquired_at REAL)",
    "CREATE INDEX IF NOT EXISTS idx_hub_resource_queue_order"
    " ON hub_resource_queue(resource, state, priority, entry_id)",
)
# Holder first, then the waiters in the exact order they will be served.
_QUEUE_ORDER = "CASE state WHEN 'holding' THEN 0 ELSE 1 END, priority DESC, entry_id ASC"


class ResourceQueueError(RuntimeError):
    """A refused queue operation; ``str(exc)`` is a stable ``cause:detail`` code."""


class ResourceWaitTimeout(ResourceQueueError):
    """The bounded wait for a resource elapsed; the waiter row was withdrawn."""


@dataclass(frozen=True)
class ResourceLease:
    db_path: Path
    resource: str
    owner: str
    entry_id: int
    priority: int
    waited_seconds: float

    def as_record(self) -> dict[str, Any]:
        return {
            "resource": self.resource,
            "owner": self.owner,
            "entry_id": self.entry_id,
            "priority": self.priority,
            "waited_seconds": self.waited_seconds,
        }


def default_db_path(repo_root: str | os.PathLike[str]) -> Path:
    """The verified canonical runtime database of one repository."""
    return task_store.canonical_db_path(repo_root)


def status_reply(repo_root: str | os.PathLike[str], resource: str = "") -> dict[str, Any]:
    """Manager tool reply: the queue view, or a named refusal (never an empty pass)."""
    try:
        return status(default_db_path(repo_root), resource)
    except (ResourceQueueError, task_store.TaskStoreError, sqlite3.Error, OSError) as exc:
        return {
            "schema_id": STATUS_SCHEMA_ID,
            "ok": False,
            "readonly": True,
            "error": f"{type(exc).__name__}:{exc}"[:300],
        }


def _resource(name: object) -> str:
    if not isinstance(name, str) or name not in KNOWN_RESOURCES:
        raise ResourceQueueError(f"resource_unknown:{str(name)[:64]}")
    return name


def _owner(owner: object) -> str:
    text = str(owner or "").strip()
    if not text or len(text) > MAX_OWNER_CHARS:
        raise ResourceQueueError("resource_owner_invalid")
    return text


def _priority(priority: object) -> int:
    if type(priority) is not int or not MIN_PRIORITY <= priority <= MAX_PRIORITY:
        raise ResourceQueueError(f"resource_priority_invalid:{priority!r}"[:80])
    return priority


def _ensure_table(conn: sqlite3.Connection) -> None:
    for statement in _TABLE_STATEMENTS:
        conn.execute(statement)


def _process_is_alive(pid: int) -> bool:
    from . import platform_io

    return platform_io.process_is_alive(pid)


def enqueue(
    db_path: str | os.PathLike[str],
    resource: str,
    owner: str,
    *,
    priority: int = 0,
    pid: int | None = None,
    clock: Callable[[], float] = time.time,
) -> int:
    """Append one waiter and return its enqueue id."""
    name, who, rank = _resource(resource), _owner(owner), _priority(priority)
    with task_store._write_connection(Path(db_path)) as conn:
        task_store._begin_immediate(conn)
        _ensure_table(conn)
        cursor = conn.execute(
            "INSERT INTO hub_resource_queue"
            " (resource, owner, priority, state, pid, enqueued_at)"
            " VALUES (?, ?, ?, 'waiting', ?, ?)",
            (name, who, rank, int(os.getpid() if pid is None else pid), float(clock())),
        )
        conn.commit()
        return int(cursor.lastrowid or 0)


def try_take(
    db_path: str | os.PathLike[str],
    entry_id: int,
    *,
    is_alive: Callable[[int], bool] | None = None,
    clock: Callable[[], float] = time.time,
) -> bool:
    """Make ``entry_id`` the holder if it heads a free queue; idempotent."""
    alive = is_alive or _process_is_alive
    with task_store._write_connection(Path(db_path)) as conn:
        task_store._begin_immediate(conn)
        _ensure_table(conn)
        row = conn.execute(
            "SELECT resource, state FROM hub_resource_queue WHERE entry_id = ?",
            (int(entry_id),),
        ).fetchone()
        if row is None:
            conn.rollback()
            raise ResourceQueueError(f"resource_queue_entry_missing:{entry_id}")
        if row["state"] == "holding":
            conn.rollback()
            return True
        rows = conn.execute(
            "SELECT entry_id, state, pid FROM hub_resource_queue WHERE resource = ?"
            f" ORDER BY {_QUEUE_ORDER}",
            (row["resource"],),
        ).fetchall()
        dead = {
            int(other["entry_id"])
            for other in rows
            if int(other["entry_id"]) != int(entry_id) and not alive(int(other["pid"]))
        }
        if dead:
            conn.executemany(
                "DELETE FROM hub_resource_queue WHERE entry_id = ?",
                [(value,) for value in sorted(dead)],
            )
        live = [other for other in rows if int(other["entry_id"]) not in dead]
        head = live[0] if live else None
        if head is None or head["state"] == "holding" or int(head["entry_id"]) != int(entry_id):
            conn.commit()
            return False
        conn.execute(
            "UPDATE hub_resource_queue SET state = 'holding', acquired_at = ?"
            " WHERE entry_id = ?",
            (float(clock()), int(entry_id)),
        )
        conn.commit()
        return True


def release(db_path: str | os.PathLike[str], entry_id: int) -> bool:
    """Remove one holder or waiter row; ``False`` when it was already gone."""
    with task_store._write_connection(Path(db_path)) as conn:
        task_store._begin_immediate(conn)
        _ensure_table(conn)
        cursor = conn.execute(
            "DELETE FROM hub_resource_queue WHERE entry_id = ?", (int(entry_id),)
        )
        conn.commit()
        return cursor.rowcount > 0


def acquire(
    db_path: str | os.PathLike[str],
    resource: str,
    owner: str,
    *,
    max_wait_seconds: float,
    priority: int = 0,
    poll_seconds: float = POLL_SECONDS,
    sleep: Callable[[float], object] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    wall_clock: Callable[[], float] = time.time,
    is_alive: Callable[[int], bool] | None = None,
    pid: int | None = None,
) -> ResourceLease:
    """Wait in FIFO order for ``resource``; the waiter row never outlives a failure."""
    limit = float(max_wait_seconds)
    if not limit > 0:
        raise ResourceQueueError(f"resource_wait_limit_invalid:{max_wait_seconds!r}"[:80])
    entry_id = enqueue(
        db_path, resource, owner, priority=priority, pid=pid, clock=wall_clock
    )
    started = clock()
    try:
        while True:
            if try_take(db_path, entry_id, is_alive=is_alive, clock=wall_clock):
                return ResourceLease(
                    db_path=Path(db_path),
                    resource=resource,
                    owner=_owner(owner),
                    entry_id=entry_id,
                    priority=priority,
                    waited_seconds=round(max(0.0, clock() - started), 3),
                )
            waited = clock() - started
            if waited >= limit:
                raise ResourceWaitTimeout(f"resource_wait_timeout:{resource}:{limit:g}s")
            sleep(max(0.0, min(float(poll_seconds), limit - waited)))
    except BaseException:
        release(db_path, entry_id)
        raise


@contextmanager
def held(
    db_path: str | os.PathLike[str],
    resource: str,
    owner: str,
    **kwargs: Any,
) -> Iterator[ResourceLease]:
    """Hold ``resource`` for the block; released on every exit path."""
    lease = acquire(db_path, resource, owner, **kwargs)
    try:
        yield lease
    finally:
        release(db_path, lease.entry_id)


def _read_rows(path: Path, names: list[str], limit: int) -> list[sqlite3.Row]:
    if not path.exists():
        return []
    conn = task_store._connect(path, readonly=True)
    try:
        placeholders = ",".join("?" for _ in names)
        return conn.execute(
            "SELECT entry_id, resource, owner, priority, state, pid, enqueued_at,"
            f" acquired_at FROM hub_resource_queue WHERE resource IN ({placeholders})"
            f" ORDER BY resource, {_QUEUE_ORDER} LIMIT ?",
            (*names, int(limit)),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return []
        raise
    finally:
        conn.close()


def status(
    db_path: str | os.PathLike[str],
    resource: str = "",
    *,
    clock: Callable[[], float] = time.time,
    limit: int = MAX_STATUS_ROWS,
) -> dict[str, Any]:
    """Read-only queue view: holder, ordered waiters, priority and wait time."""
    names = [_resource(resource)] if resource else sorted(KNOWN_RESOURCES)
    bound = max(1, min(int(limit), MAX_STATUS_ROWS))
    rows = _read_rows(Path(db_path), names, bound + 1)
    now = float(clock())
    views: dict[str, dict[str, Any]] = {
        name: {"resource": name, "holder": None, "waiters": [], "waiter_count": 0}
        for name in names
    }
    for row in rows[:bound]:
        view = views[str(row["resource"])]
        enqueued = float(row["enqueued_at"])
        acquired = row["acquired_at"]
        entry: dict[str, Any] = {
            "entry_id": int(row["entry_id"]),
            "owner": str(row["owner"]),
            "priority": int(row["priority"]),
            "pid": int(row["pid"]),
            "enqueued_at": enqueued,
            "wait_seconds": round(
                max(0.0, (float(acquired) if acquired is not None else now) - enqueued), 3
            ),
        }
        if row["state"] == "holding":
            entry["acquired_at"] = float(acquired) if acquired is not None else None
            entry["held_seconds"] = round(
                max(0.0, now - (float(acquired) if acquired is not None else now)), 3
            )
            view["holder"] = entry
        else:
            entry["position"] = len(view["waiters"]) + 1
            view["waiters"].append(entry)
            view["waiter_count"] = len(view["waiters"])
    return {
        "schema_id": STATUS_SCHEMA_ID,
        "ok": True,
        "readonly": True,
        "resources": [views[name] for name in names],
        "truncated": len(rows) > bound,
    }
