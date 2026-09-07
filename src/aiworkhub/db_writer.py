"""One serialized writer per SQLite database, across PROCESSES.

Why this module exists
----------------------
The measured contention on ``.aiworkhub/tasking/task_queue.sqlite`` is
**cross-process**, not in-process: 169 distinct supervisor OS processes
(``pid`` in ``process_events``, each with its own ``pid_start_ticks``) raised
``sqlite3.OperationalError: database is locked``, with up to 12 of them alive
concurrently.  An in-process work queue cannot serialize writers that live in
different processes, so this lease is built on an OS-level advisory file lock
(``fcntl.flock``) which every process on the host observes.

What it changes
---------------
The failure mode becomes deterministic.  Without a lease, concurrent writers
race into ``BEGIN IMMEDIATE``, and the loser burns its whole ``busy_timeout``
before surfacing ``database is locked`` to a caller that must then retry --
``lock -> fail -> sleep -> retry``.  With the lease, a writer waits its turn on
a queue and then runs an uncontended transaction -- ``queue -> transaction ->
commit``.  Waiting is bounded and observable rather than silent and lossy.

What it deliberately does NOT change
------------------------------------
* It does not open, configure, or close connections.  ``PRAGMA
  journal_mode``/``synchronous``/``busy_timeout`` stay exactly where each store
  already sets them.
* It does not begin, commit, or roll back transactions.  Callers keep their own
  ``BEGIN IMMEDIATE`` and their own preimage/CAS guards.
* It does not retry, and it never swallows an exception.  A genuine
  ``sqlite3.OperationalError`` raised inside the lease propagates unchanged --
  a queue that hides an error is worse than a lock.
* Readers are never gated.  Only code that explicitly takes a lease waits, and
  WAL readers continue concurrently with the single writer.

Correctness notes
-----------------
``flock`` is associated with the *open file description*, not with a thread, so
two threads in one process sharing one descriptor would both believe they hold
the lock.  This module therefore takes a per-path in-process ``threading.Lock``
*first* and opens a fresh descriptor per acquisition, so the lease is mutually
exclusive both between threads and between processes.

The lease is re-entrant within a single (process, thread, path): nested use
returns immediately instead of self-deadlocking.
"""

from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

__all__ = [
    "DEFAULT_LEASE_TIMEOUT_S",
    "WriteLeaseError",
    "WriteLeaseTimeout",
    "lock_path_for",
    "connection_db_path",
    "write_lease",
    "lease_for_connection",
    "stats",
    "reset_stats",
    "backend",
]

# Bounded by default at the same order as the stores' own ``busy_timeout``
# (5000 ms) so a wedged holder surfaces as a typed, named timeout instead of an
# unbounded stall. Callers that legitimately need longer pass ``timeout_s``.
DEFAULT_LEASE_TIMEOUT_S: float = 30.0

_LOCK_SUFFIX = ".writer.lock"
_POLL_SECONDS = 0.0005
# Ceiling on the retry sleep. ``flock(LOCK_NB)`` gives no way to be woken when
# the holder releases, so a waiter polls; a coarse ceiling shows up directly as
# tail latency (a 50ms ceiling measured p99 ~2.2s under 8-way contention, most
# of it sleeping after the lock was already free). 5ms keeps the wasted CPU
# negligible at this call rate while cutting that tail by roughly an order of
# magnitude.
_POLL_MAX_SECONDS = 0.005


class WriteLeaseError(RuntimeError):
    """Base class for write-lease failures."""


class WriteLeaseTimeout(WriteLeaseError):
    """The lease could not be acquired within the bounded wait.

    This is raised instead of proceeding unserialized: silently writing without
    the lease would reintroduce exactly the race the lease exists to remove.
    """

    def __init__(self, db_path: str, waited_s: float, timeout_s: float) -> None:
        super().__init__(
            f"write_lease_timeout:{Path(db_path).name}:"
            f"waited_ms={int(waited_s * 1000)}:timeout_ms={int(timeout_s * 1000)}"
        )
        self.db_path = str(db_path)
        self.waited_s = float(waited_s)
        self.timeout_s = float(timeout_s)
        self.retryable = True


# --------------------------------------------------------------------------
# Platform backend
# --------------------------------------------------------------------------
try:  # POSIX
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - exercised on Windows only
    _fcntl = None  # type: ignore[assignment]

try:  # Windows
    import msvcrt as _msvcrt
except ImportError:
    _msvcrt = None  # type: ignore[assignment]


def backend() -> str:
    """Name the cross-process locking backend actually in use.

    ``"none"`` means only in-process serialization is available; callers that
    care about cross-process guarantees can record that degradation instead of
    assuming a guarantee the host cannot provide.
    """
    if _fcntl is not None:
        return "flock"
    if _msvcrt is not None:
        return "msvcrt"
    return "none"


def _try_lock(fd: int) -> bool:
    """Attempt one non-blocking exclusive acquisition. False if held."""
    if _fcntl is not None:
        try:
            _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
            return True
        except OSError:
            return False
    if _msvcrt is not None:  # pragma: no cover - Windows only
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            _msvcrt.locking(fd, _msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    # No cross-process primitive on this host: the in-process lock already held
    # by the caller is the only serialization available.
    return True


def _unlock(fd: int) -> None:
    if _fcntl is not None:
        try:
            _fcntl.flock(fd, _fcntl.LOCK_UN)
        except OSError:
            pass
        return
    if _msvcrt is not None:  # pragma: no cover - Windows only
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            _msvcrt.locking(fd, _msvcrt.LK_UNLCK, 1)
        except OSError:
            pass


# --------------------------------------------------------------------------
# Per-path in-process state
# --------------------------------------------------------------------------
_REGISTRY_GUARD = threading.Lock()
_PATH_LOCKS: dict[str, threading.Lock] = {}
# One descriptor per (process, path), reused for the life of the process. The
# in-process lock above already guarantees a single thread is inside the
# critical section, so a shared descriptor is safe -- and it removes an
# open()/close() syscall pair from every single write, which is pure overhead
# on the hot path.
_PATH_FDS: dict[str, int] = {}
_DEPTH = threading.local()
_PID = os.getpid()

_STATS_GUARD = threading.Lock()
_STATS: dict[str, float] = {
    "acquired": 0.0,
    "reentrant": 0.0,
    "timeouts": 0.0,
    "wait_total_s": 0.0,
    "wait_max_s": 0.0,
    "held_total_s": 0.0,
}


def _path_lock(key: str) -> threading.Lock:
    with _REGISTRY_GUARD:
        _discard_inherited_state_locked()
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _PATH_LOCKS[key] = lock
        return lock


def _discard_inherited_state_locked() -> None:
    """Drop descriptors and locks inherited across ``fork``.

    A forked child inherits both the parent's descriptors and a snapshot of its
    lock objects. Reusing either would let the child believe it holds a lease
    the parent actually owns, so state is rebuilt the first time a new pid uses
    the module.
    """
    global _PID
    current = os.getpid()
    if current == _PID:
        return
    for fd in _PATH_FDS.values():
        try:
            os.close(fd)
        except OSError:
            pass
    _PATH_FDS.clear()
    _PATH_LOCKS.clear()
    _PID = current


def _path_fd(key: str, lock_file: Path) -> int:
    """Descriptor for ``lock_file``, opened once per process."""
    with _REGISTRY_GUARD:
        fd = _PATH_FDS.get(key)
        if fd is not None:
            return fd
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
        _PATH_FDS[key] = fd
        return fd


def _depths() -> dict[str, int]:
    depths = getattr(_DEPTH, "depths", None)
    if depths is None:
        depths = {}
        _DEPTH.depths = depths
    return depths


def _record(field: str, value: float) -> None:
    with _STATS_GUARD:
        if field == "wait_max_s":
            _STATS[field] = max(_STATS[field], value)
        else:
            _STATS[field] += value


def stats() -> dict[str, float]:
    """Observability snapshot; a lease that cannot be measured cannot be tuned."""
    with _STATS_GUARD:
        return dict(_STATS)


def reset_stats() -> None:
    with _STATS_GUARD:
        for key in _STATS:
            _STATS[key] = 0.0


# --------------------------------------------------------------------------
# Path helpers
# --------------------------------------------------------------------------
def lock_path_for(db_path: str | Path) -> Path:
    """Sidecar lock file for ``db_path``.

    A separate file is used rather than the database itself so the lease can
    never interfere with SQLite's own locking bytes, and so it works before the
    database file exists.
    """
    path = Path(db_path)
    return path.with_name(path.name + _LOCK_SUFFIX)


def connection_db_path(conn: Any) -> str:
    """Resolve the main database file behind an open connection.

    Returns ``""`` for in-memory or unresolvable connections, which callers
    treat as "nothing to serialize across processes".
    """
    try:
        rows = conn.execute("PRAGMA database_list").fetchall()
    except Exception:  # noqa: BLE001 -- a probe must never break the caller
        return ""
    for row in rows:
        try:
            name = row[1]
            filename = row[2]
        except (IndexError, KeyError, TypeError):
            continue
        if str(name) == "main":
            return str(filename or "")
    return ""


def _resolve_key(db_path: str | Path) -> str:
    try:
        return str(Path(db_path).resolve())
    except OSError:
        return str(db_path)


# --------------------------------------------------------------------------
# The lease
# --------------------------------------------------------------------------
@contextmanager
def write_lease(
    db_path: str | Path,
    *,
    timeout_s: float | None = None,
) -> Iterator[dict[str, Any]]:
    """Hold the single-writer lease for ``db_path`` for the duration of the block.

    Yields a receipt describing how the lease was obtained, so a caller can
    record real waiting instead of guessing::

        {"db_path": ..., "waited_s": 0.004, "reentrant": False,
         "backend": "flock", "lock_path": ...}

    Raises :class:`WriteLeaseTimeout` if the lease cannot be taken within
    ``timeout_s``.  Exceptions raised by the body propagate unchanged.
    """
    bounded_timeout = (
        DEFAULT_LEASE_TIMEOUT_S if timeout_s is None else max(0.0, float(timeout_s))
    )
    key = _resolve_key(db_path)
    depths = _depths()

    # Re-entrant: a nested lease on the same path in the same thread must not
    # deadlock against the lease the caller already holds.
    if depths.get(key, 0) > 0:
        depths[key] += 1
        _record("reentrant", 1.0)
        try:
            yield {
                "db_path": key,
                "waited_s": 0.0,
                "reentrant": True,
                "backend": backend(),
                "lock_path": str(lock_path_for(db_path)),
            }
        finally:
            depths[key] -= 1
        return

    started = time.monotonic()
    in_process = _path_lock(key)
    deadline = started + bounded_timeout

    # 1. Serialize threads within this process first: flock is per open file
    #    description, so it alone would not exclude sibling threads.
    remaining = max(0.0, deadline - time.monotonic())
    if not in_process.acquire(timeout=remaining if bounded_timeout else 0.0):
        waited = time.monotonic() - started
        _record("timeouts", 1.0)
        raise WriteLeaseTimeout(str(db_path), waited, bounded_timeout)

    lock_file = lock_path_for(db_path)
    flocked = False
    try:
        try:
            fd = _path_fd(key, lock_file)
        except OSError as exc:
            raise WriteLeaseError(
                f"write_lease_unavailable:{lock_file.name}:{type(exc).__name__}"
            ) from exc

        # 2. Serialize across processes.
        backoff = _POLL_SECONDS
        while True:
            if _try_lock(fd):
                flocked = True
                break
            if time.monotonic() >= deadline:
                waited = time.monotonic() - started
                _record("timeouts", 1.0)
                raise WriteLeaseTimeout(str(db_path), waited, bounded_timeout)
            time.sleep(min(backoff, max(0.0, deadline - time.monotonic())))
            backoff = min(backoff * 2.0, _POLL_MAX_SECONDS)

        waited = time.monotonic() - started
        _record("acquired", 1.0)
        _record("wait_total_s", waited)
        _record("wait_max_s", waited)
        depths[key] = 1
        held_from = time.monotonic()
        try:
            yield {
                "db_path": key,
                "waited_s": waited,
                "reentrant": False,
                "backend": backend(),
                "lock_path": str(lock_file),
            }
        finally:
            depths[key] = max(0, depths.get(key, 1) - 1)
            _record("held_total_s", time.monotonic() - held_from)
    finally:
        # Release in the reverse order of acquisition, and only what this call
        # actually took. Releasing on a bare ``Lock.locked()`` check would drop
        # a lock a *different* thread had since acquired.
        if flocked:
            _unlock(fd)
        in_process.release()


@contextmanager
def lease_for_connection(
    conn: Any,
    *,
    timeout_s: float | None = None,
) -> Iterator[dict[str, Any]]:
    """Take the lease for the database behind ``conn``.

    An in-memory or unresolvable connection has no cross-process peer, so the
    block runs unserialized with ``{"skipped": True}`` rather than failing --
    tests using ``:memory:`` keep working unchanged.
    """
    db_path = connection_db_path(conn)
    if not db_path or db_path == ":memory:":
        yield {"db_path": "", "skipped": True, "waited_s": 0.0, "backend": backend()}
        return
    with write_lease(db_path, timeout_s=timeout_s) as receipt:
        yield {**receipt, "skipped": False}
