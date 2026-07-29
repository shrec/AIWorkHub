"""Production-bounded automatic Source Graph indexing lifecycle (0.6.30).

Mirrors ``callback_bridge.py``'s ``CallbackDispatcher``/registry pattern
(B857): exactly ONE background daemon per canonical repository, an
idempotent ``ensure_started``, an explicit ``stop``, and a health surface
that never fabricates a running/ready daemon. The daemon's job is narrower
than the callback dispatcher's: keep ``source_graph.build_index`` for the
active repository converged in the background so ``InitRepo``/repository
reload never block the caller on a full index build.

Lifecycle:
  * First run for a repository with no ``last_build`` meta row (fresh
    ``.aiworkhub/source_graph`` database, or none yet) does ONE full build
    (``incremental=False``). Every run after that is incremental.
  * A periodic loop refreshes on a conservative, configurable interval
    (``DEFAULT_REFRESH_INTERVAL_SECONDS``, floored at
    ``MIN_REFRESH_INTERVAL_SECONDS``) via ``threading.Event.wait`` so
    ``stop()`` interrupts the wait immediately instead of blocking on a
    plain ``time.sleep``.
  * A single ``threading.Lock`` per daemon (``_build_lock``) makes the
    periodic loop and an explicit ``refresh_now()`` call mutually
    exclusive: a build already in flight is never joined by a second one
    against the same repository's database.
  * A failed build is caught, recorded (``degraded`` status + bounded
    ``last_error``), and never re-raised -- one bad build must not crash
    the daemon thread, let alone the parent MCP process.

Registry keyed by the resolved repository root (identical convention to
``callback_bridge._dispatcher_registry_key``) so two repositories attached
to this tool never share or interfere with each other's daemon.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import source_graph

DEFAULT_REFRESH_INTERVAL_SECONDS = 300.0
MIN_REFRESH_INTERVAL_SECONDS = 30.0

STATUS_STOPPED = "stopped"
STATUS_INDEXING = "indexing"
STATUS_READY = "ready"
STATUS_EMPTY = "empty"
STATUS_STANDBY = "standby"
STATUS_DEGRADED = "degraded"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _registry_key(repo_root: Path | str) -> str:
    return str(Path(repo_root).resolve())


class SourceGraphDaemon:
    """Owns exactly one background Source Graph indexing thread for one
    repository. Never constructed directly by callers outside this
    module's registry functions (``ensure_started``/``stop_daemon``)."""

    def __init__(
        self,
        repo_root: Path | str,
        *,
        refresh_interval_seconds: float = DEFAULT_REFRESH_INTERVAL_SECONDS,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.refresh_interval_seconds = max(
            MIN_REFRESH_INTERVAL_SECONDS, float(refresh_interval_seconds)
        )
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        # Non-overlapping guard: the periodic loop and an explicit
        # refresh_now() never build this repository's database concurrently.
        self._build_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._status = STATUS_STOPPED
        self._last_report: dict[str, Any] | None = None
        self._last_error: str = ""
        self._last_run_at: str = ""
        self._started_at: str = ""

    def is_running(self) -> bool:
        thread = self._thread
        return bool(thread is not None and thread.is_alive())

    def _has_prior_build(self) -> bool:
        """True iff this repository's database already recorded a build.

        A fresh repository (no database file yet, or a database with an
        empty ``meta`` table) triggers exactly one full build; every build
        after that is incremental. Any resolution failure is treated as
        "no prior build" -- it fails toward a (safe, if redundant) full
        build rather than silently skipping indexing.
        """
        try:
            db_path = source_graph.resolve_db_path(self.repo_root)
            if not db_path.exists():
                return False
            conn = source_graph.connect(db_path, read_only=True)
            try:
                row = conn.execute(
                    "SELECT value FROM meta WHERE key='last_build'"
                ).fetchone()
                return row is not None
            finally:
                conn.close()
        except source_graph.SourceGraphError:
            return False

    def _run_one_build(self) -> bool:
        """Attempt one bounded, non-overlapping build.

        Returns ``True`` iff this call actually acquired the build lock
        and ran (whether the build itself succeeded or failed); returns
        ``False`` when another build (periodic tick or a concurrent
        ``refresh_now()``) already held the lock, in which case this call
        does nothing and reports no state change.
        """
        if not self._build_lock.acquire(blocking=False):
            return False
        try:
            with self._state_lock:
                self._status = STATUS_INDEXING
            try:
                # Prior-build probing is part of the fallible indexing
                # operation. A transient SQLite/read failure must be recorded
                # as degraded and retried, never escape the daemon thread and
                # strand health at ``indexing`` with ``running=false``.
                incremental = self._has_prior_build()
                report = source_graph.build_index(self.repo_root, incremental=incremental)
                with self._state_lock:
                    # A successful SQLite transaction is not the same thing
                    # as a usable Source Graph.  Keep empty repositories
                    # truthful so code-task gates cannot mistake a zero-row
                    # database for an indexed project.
                    self._status = (
                        STATUS_READY if report.files_seen > 0 else STATUS_EMPTY
                    )
                    self._last_report = report.to_json()
                    self._last_error = ""
                    self._last_run_at = _utcnow()
            except source_graph.SourceGraphBuildInProgressError:
                # Another VS Code/MCP child owns this repository's writer
                # lease. This process remains a healthy reader/standby and
                # retries on the next tick; contention is not degradation.
                with self._state_lock:
                    self._status = STATUS_STANDBY
                    self._last_error = ""
                    self._last_run_at = _utcnow()
            except Exception as exc:  # noqa: BLE001 -- a failed index must never crash the MCP process
                with self._state_lock:
                    self._status = STATUS_DEGRADED
                    self._last_error = f"{type(exc).__name__}:{exc}"[:500]
                    self._last_run_at = _utcnow()
            return True
        finally:
            self._build_lock.release()

    def _loop(self) -> None:
        self._run_one_build()
        while not self._stop_event.wait(self.refresh_interval_seconds):
            self._run_one_build()

    def start(self) -> None:
        """Idempotent: a second call while already running is a no-op.

        Returns immediately -- the (possibly full) first build runs on the
        background thread, never blocking this call's caller.
        """
        with self._state_lock:
            if self.is_running():
                return
            self._stop_event.clear()
            self._started_at = _utcnow()
            thread = threading.Thread(
                target=self._loop,
                name=f"aiworkhub-source-graph-daemon:{self.repo_root.name}",
                daemon=True,
            )
            self._thread = thread
        thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
        with self._state_lock:
            # Never pretend a timed-out build thread has stopped. Keeping the
            # live thread registered prevents a second daemon from starting
            # against the same repository database.
            if self._thread is thread and not thread.is_alive():
                self._thread = None
                self._status = STATUS_STOPPED

    def refresh_now(self) -> dict[str, Any]:
        """Force one bounded build synchronously, respecting the same
        non-overlap lock the periodic loop uses. If a build is already in
        flight (periodic tick or another ``refresh_now()``), this returns
        ``triggered: False`` immediately rather than blocking or starting
        a second overlapping build against the same database.
        """
        triggered = self._run_one_build()
        health = self.health()
        if not triggered:
            return {**health, "ok": True, "triggered": False, "reason": "build_in_progress"}
        return {**health, "triggered": True}

    def health(self) -> dict[str, Any]:
        with self._state_lock:
            return {
                "ok": self._status != STATUS_DEGRADED,
                "status": self._status,
                "running": self.is_running(),
                "repo_root": str(self.repo_root),
                "refresh_interval_seconds": self.refresh_interval_seconds,
                "started_at": self._started_at,
                "last_run_at": self._last_run_at,
                "last_report": self._last_report,
                "last_error": self._last_error,
                "writer_state": "standby" if self._status == STATUS_STANDBY else "active",
            }


_REGISTRY: dict[str, SourceGraphDaemon] = {}
_REGISTRY_LOCK = threading.Lock()


def ensure_started(
    repo_root: Path | str,
    *,
    refresh_interval_seconds: float = DEFAULT_REFRESH_INTERVAL_SECONDS,
) -> SourceGraphDaemon:
    """Idempotently start (or return) the ONE daemon for ``repo_root``.

    Safe to call repeatedly from ``InitRepo``, activation, tab-
    deserialization, and reload alike -- an already-registered daemon for
    this repository is returned unchanged (``refresh_interval_seconds`` on
    a repeat call is ignored, exactly like ``callback_bridge.ensure_dispatcher``
    ignoring a repeat provider-matching call's kwargs).
    """
    key = _registry_key(repo_root)
    with _REGISTRY_LOCK:
        daemon = _REGISTRY.get(key)
        if daemon is None:
            daemon = SourceGraphDaemon(repo_root, refresh_interval_seconds=refresh_interval_seconds)
            _REGISTRY[key] = daemon
    daemon.start()  # no-op if already running
    return daemon


def get_daemon(repo_root: Path | str) -> SourceGraphDaemon | None:
    with _REGISTRY_LOCK:
        return _REGISTRY.get(_registry_key(repo_root))


def stop_daemon(repo_root: Path | str) -> bool:
    """Stop and unregister the daemon for ``repo_root``, if any.

    Returns ``True`` iff a daemon was actually registered (and is now
    stopped/unregistered).
    """
    key = _registry_key(repo_root)
    with _REGISTRY_LOCK:
        daemon = _REGISTRY.get(key)
    if daemon is None:
        return False
    daemon.stop()
    if daemon.is_running():
        return False
    with _REGISTRY_LOCK:
        if _REGISTRY.get(key) is daemon:
            _REGISTRY.pop(key, None)
    return True


def stop_all_daemons() -> int:
    """Stop every registered daemon. Used by full-process teardown/tests."""
    with _REGISTRY_LOCK:
        daemons = list(_REGISTRY.values())
        _REGISTRY.clear()
    for daemon in daemons:
        daemon.stop()
    return len(daemons)


def daemon_health(repo_root: Path | str) -> dict[str, Any]:
    """Read-only health for the repo's daemon, or a not-registered shape
    if none exists yet (never an error -- an unregistered daemon on an
    otherwise-healthy repository is a normal, not-degraded state)."""
    daemon = get_daemon(repo_root)
    if daemon is None:
        return {
            "ok": True,
            "status": STATUS_STOPPED,
            "running": False,
            "repo_root": str(Path(repo_root).resolve()),
            "refresh_interval_seconds": None,
            "started_at": "",
            "last_run_at": "",
            "last_report": None,
            "last_error": "",
            "registered": False,
        }
    out = daemon.health()
    out["registered"] = True
    return out


def refresh_now(repo_root: Path | str) -> dict[str, Any]:
    """Force one bounded build for an already-registered daemon.

    Returns ``ok: False`` (never raises, never starts a daemon implicitly)
    when no daemon is registered for this repository yet -- callers that
    want auto-start-then-refresh call ``ensure_started`` first.
    """
    daemon = get_daemon(repo_root)
    if daemon is None:
        return {
            "ok": False,
            "error": "not_registered",
            "repo_root": str(Path(repo_root).resolve()),
        }
    return daemon.refresh_now()


__all__ = [
    "DEFAULT_REFRESH_INTERVAL_SECONDS",
    "MIN_REFRESH_INTERVAL_SECONDS",
    "STATUS_DEGRADED",
    "STATUS_EMPTY",
    "STATUS_INDEXING",
    "STATUS_READY",
    "STATUS_STANDBY",
    "STATUS_STOPPED",
    "SourceGraphDaemon",
    "daemon_health",
    "ensure_started",
    "get_daemon",
    "refresh_now",
    "stop_all_daemons",
    "stop_daemon",
]
