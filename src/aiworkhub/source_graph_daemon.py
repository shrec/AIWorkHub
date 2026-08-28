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
  * A single ``threading.Lock`` per daemon (``_build_lock``) serializes
    the periodic loop and an explicit ``refresh_now()`` call: they never
    overlap against the same repository's database.
  * A failed build is caught, recorded (``degraded`` status + bounded
    ``last_error``), and never re-raised -- one bad build must not crash
    the daemon thread, let alone the parent MCP process.

Registry keyed by the resolved repository root (identical convention to
``callback_bridge._dispatcher_registry_key``) so two repositories attached
to this tool never share or interfere with each other's daemon.
"""

from __future__ import annotations

import json
import os
import select
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import source_graph

DEFAULT_REFRESH_INTERVAL_SECONDS = 300.0
MIN_REFRESH_INTERVAL_SECONDS = 30.0
DEFAULT_STALE_MULTIPLIER = 3.0
MIN_STALE_AFTER_SECONDS = 120.0
BUILD_EXECUTION_ENV = "AIWORKHUB_SOURCE_GRAPH_BUILD_EXECUTION"
BUILD_EXECUTION_SUBPROCESS = "subprocess"
BUILD_EXECUTION_THREAD = "thread"

STATUS_STOPPED = "stopped"
STATUS_INDEXING = "indexing"
STATUS_READY = "ready"
STATUS_EMPTY = "empty"
STATUS_STANDBY = "standby"
STATUS_DEGRADED = "degraded"
STATUS_STALE = "stale"
STATUS_RECOVERY = "recovery"

_RECOVERY_PHASE_DETECT = "journal_detect"
_RECOVERY_PHASE_OPEN = "writable_open_recover"
_RECOVERY_PHASE_INTEGRITY = "integrity_check"
_RECOVERY_PHASE_COMMIT = "commit"


def _build_once_payload(repo_root: Path | str, *, incremental: bool) -> dict[str, Any]:
    """Run one index build and return a JSON-safe outcome.

    This function is also the entry point used by the dedicated indexing
    subprocess. Keeping CPU-heavy parsing outside the MCP stdio process is a
    correctness requirement: a large repository build must never starve MCP
    health, dashboard snapshot or callback requests behind the Python GIL.
    """
    try:
        report = source_graph.build_index(Path(repo_root), incremental=incremental)
        return {"kind": "success", "report": report.to_json()}
    except source_graph.SourceGraphBuildInProgressError:
        return {"kind": "standby"}
    except Exception as exc:  # noqa: BLE001 -- serialized for daemon health
        return {"kind": "error", "error": f"{type(exc).__name__}:{exc}"[:500]}


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _registry_key(repo_root: Path | str) -> str:
    return str(Path(repo_root).resolve())


def _repo_has_readable_generation(repo_root: Path | str) -> bool:
    """True iff the canonical database already holds a committed generation.

    Distinguishes a writer that yielded to another process's build lease
    (STANDBY) with a usable prior generation from one where no generation
    has ever been committed -- the former is not a refresh failure, the
    latter genuinely is.
    """
    try:
        db_path = source_graph.resolve_db_path(Path(repo_root).resolve())
        if not db_path.exists():
            return False
        conn = source_graph.connect(db_path, read_only=True)
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = 'last_build'"
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return False
        payload = json.loads(row["value"])
        return bool(
            payload.get("finished_at")
            and payload.get("build_revision")
            and int(payload.get("files_seen") or 0) > 0
        )
    except (
        OSError,
        ValueError,
        TypeError,
        json.JSONDecodeError,
        sqlite3.Error,
        source_graph.SourceGraphError,
    ):
        return False


_STALE_QUERY_CONNECTION_MARKERS = (
    "database schema has changed",
    "attempt to write a readonly database",
)
_STALE_QUERY_CONNECTION_CODES = frozenset(
    code
    for code in (
        getattr(sqlite3, "SQLITE_READONLY", None),
        getattr(sqlite3, "SQLITE_SCHEMA", None),
    )
    if code is not None
)


def _close_query_connection(conn: sqlite3.Connection | None) -> None:
    if conn is None:
        return
    try:
        conn.close()
    except sqlite3.Error:
        pass


def _is_stale_query_connection_error(exc: BaseException) -> bool:
    if not isinstance(exc, sqlite3.Error):
        return False
    code = getattr(exc, "sqlite_errorcode", None)
    if code is not None and code in _STALE_QUERY_CONNECTION_CODES:
        return True
    text = str(exc).casefold()
    return any(marker in text for marker in _STALE_QUERY_CONNECTION_MARKERS)


def _live_index_holder_evidence(repo_root: Path | str) -> str:
    daemon = get_daemon(repo_root)
    if daemon is not None and daemon.is_running():
        with daemon._state_lock:
            status = daemon._status
            build_process = daemon._build_process
        if status in {STATUS_INDEXING, STATUS_RECOVERY} or build_process is not None:
            return f"live_holder:daemon:{status}"
    try:
        with source_graph.index_write_lease(Path(repo_root).resolve()) as acquired:
            if not acquired:
                return "live_holder:index_write_lease"
    except source_graph.SourceGraphError as exc:
        return f"live_holder:{type(exc).__name__}:{exc}"[:300]
    return ""


def _execute_canonical_meta_query(conn: sqlite3.Connection) -> dict[str, str]:
    return {
        str(row["key"]): str(row["value"])
        for row in conn.execute(
            "SELECT key, value FROM meta WHERE key IN "
            "('last_build','index_quality','recommendation_roundtrip')"
        )
    }


def _read_canonical_meta_readonly(
    repo_root: Path | str, db_path: Path
) -> dict[str, str]:
    """Read generation meta on a read-only connection.

    A stale unheld query connection is closed, reopened read-only, and retried
    exactly once. A live holder or any non-stale SQLite error fails closed
    without opening writable.
    """
    conn: sqlite3.Connection | None = None
    try:
        conn = source_graph.connect(db_path, read_only=True)
        return _execute_canonical_meta_query(conn)
    except sqlite3.Error as exc:
        _close_query_connection(conn)
        conn = None
        if not _is_stale_query_connection_error(exc):
            raise
        holder = _live_index_holder_evidence(repo_root)
        if holder:
            raise sqlite3.OperationalError(holder) from exc
        conn = source_graph.connect(db_path, read_only=True)
        return _execute_canonical_meta_query(conn)
    finally:
        _close_query_connection(conn)


class SourceGraphDaemon:
    """Owns exactly one background Source Graph indexing thread for one
    repository. Never constructed directly by callers outside this
    module's registry functions (``ensure_started``/``stop_daemon``)."""

    def __init__(
        self,
        repo_root: Path | str,
        *,
        refresh_interval_seconds: float = DEFAULT_REFRESH_INTERVAL_SECONDS,
        stale_after_seconds: float | None = None,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.refresh_interval_seconds = max(
            MIN_REFRESH_INTERVAL_SECONDS, float(refresh_interval_seconds)
        )
        default_stale_after = max(
            MIN_STALE_AFTER_SECONDS,
            self.refresh_interval_seconds * DEFAULT_STALE_MULTIPLIER,
        )
        self.stale_after_seconds = max(
            self.refresh_interval_seconds,
            (
                float(stale_after_seconds)
                if stale_after_seconds is not None
                else default_stale_after
            ),
        )
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._refresh_event = threading.Event()
        # Non-overlapping guard: the periodic loop and an explicit
        # refresh_now() never build this repository's database concurrently.
        self._build_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._status = STATUS_STOPPED
        self._last_report: dict[str, Any] | None = None
        self._last_error: str = ""
        self._last_run_at: str = ""
        self._last_success_at: str = ""
        self._started_at: str = ""
        self._build_completed = threading.Event()
        configured_execution = os.environ.get(
            BUILD_EXECUTION_ENV, BUILD_EXECUTION_SUBPROCESS
        ).strip().lower()
        self._build_execution = (
            BUILD_EXECUTION_THREAD
            if configured_execution == BUILD_EXECUTION_THREAD
            else BUILD_EXECUTION_SUBPROCESS
        )
        self._process_lock = threading.Lock()
        self._build_process: subprocess.Popen[str] | None = None
        # Exact owned process-group identity of ``_build_process`` (its
        # session/group-leader pid on POSIX; ``None`` on Windows, where the
        # tree is reaped by PID via ``taskkill /T``). Captured while the child
        # is un-reaped so a later PID reuse can never target an unrelated
        # process or group. See ``_terminate_build_process``.
        self._build_pgid: int | None = None
        # Power-loss recovery: single-flight writable recovery before any
        # readonly probe when a non-empty SQLite journal/WAL exists.
        self._recovery_lock = threading.Lock()
        self._recovery_phase: str = ""
        self._recovery_elapsed: float = 0.0
        self._recovery_error: str = ""
        self._last_known_good_generation: dict[str, Any] = {}
        self._recovery_started_at: float = 0.0
        # One durable refresh job per coalesced request wave. The daemon thread
        # is the sole consumer; callers only reserve/observe jobs.
        self._pending_refresh_job: dict[str, Any] | None = None
        self._active_refresh_job: dict[str, Any] | None = None
        self._last_refresh_job: dict[str, Any] | None = None

    def _refresh_job_path(self) -> Path:
        return source_graph.resolve_db_path(self.repo_root).parent / "refresh-job.json"

    def _persist_refresh_job(self, job: dict[str, Any]) -> tuple[bool, str]:
        """Atomically persist one bounded refresh outcome beside the index."""

        try:
            path = self._refresh_job_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
            payload = json.dumps(job, sort_keys=True, separators=(",", ":")) + "\n"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(tmp, path)
            finally:
                try:
                    tmp.unlink()
                except FileNotFoundError:
                    pass
            return True, ""
        except (OSError, source_graph.SourceGraphError) as exc:
            return False, f"refresh_job_persist:{type(exc).__name__}:{exc}"[:300]

    def _begin_refresh_job(self) -> dict[str, Any] | None:
        with self._state_lock:
            if self._pending_refresh_job is None:
                return None
            job = dict(self._pending_refresh_job)
            self._pending_refresh_job = None
            job.update({"state": "running", "started_at": _utcnow()})
            self._active_refresh_job = job
            self._last_refresh_job = job
        ok, error = self._persist_refresh_job(job)
        if not ok:
            self._finish_refresh_job(job, state="failed", error=error)
            return None
        return job

    def _finish_refresh_job(
        self,
        job: dict[str, Any],
        *,
        state: str,
        error: str = "",
    ) -> None:
        with self._state_lock:
            finished = {
                **job,
                "state": state,
                "finished_at": _utcnow(),
                "error": str(error or "")[:500],
                "report": dict(self._last_report) if self._last_report else None,
            }
            if (
                self._active_refresh_job is not None
                and self._active_refresh_job.get("job_id") == job.get("job_id")
            ):
                self._active_refresh_job = None
            self._last_refresh_job = finished
        persisted, persist_error = self._persist_refresh_job(finished)
        if not persisted:
            with self._state_lock:
                self._last_refresh_job = {
                    **finished,
                    "state": "failed",
                    "error": persist_error,
                }

    def _reserve_refresh_job(self) -> tuple[dict[str, Any], bool]:
        """Create (or coalesce into) one pending/active durable refresh job.

        Shared by ``request_refresh()`` and ``refresh_now()`` so both entry
        points expose the same job identity/coalescing contract.
        """
        with self._state_lock:
            existing = self._pending_refresh_job or self._active_refresh_job
            if existing is not None:
                return dict(existing), True
            job = {
                "schema_id": "aiworkhub.source_graph.refresh_job.v1",
                "job_id": uuid.uuid4().hex,
                "repo_root": str(self.repo_root),
                "state": "queued",
                "requested_at": _utcnow(),
                "started_at": "",
                "finished_at": "",
                "error": "",
                "report": None,
            }
            self._pending_refresh_job = job
            self._last_refresh_job = job
            return job, False

    def _refresh_terminal_state(self, status: str, error: str) -> tuple[str, str]:
        """Map a post-build daemon status to a truthful refresh-job state.

        STANDBY means this process yielded its build lease to another
        writer, not that the refresh failed -- if the canonical database
        already holds a readable prior generation, the refresh is truthfully
        ``succeeded``. A STANDBY with no readable generation, or any other
        non-ready status, remains truthfully ``failed``.
        """
        if status in {STATUS_READY, STATUS_EMPTY}:
            return "succeeded", ""
        if status == STATUS_STANDBY and _repo_has_readable_generation(self.repo_root):
            return "succeeded", ""
        return "failed", error or f"refresh_terminal_status:{status}"

    @staticmethod
    def _new_process_group_popen_kwargs() -> dict[str, Any]:
        """Platform-appropriate ``Popen`` kwargs that make the build subprocess
        the leader of a fresh, owned process group/session.

        A dedicated group/session is what lets ``stop()`` reap the ENTIRE build
        tree -- including ``ProcessPoolExecutor`` workers that inherit this
        daemon's stdout/stderr pipes -- as a single unit. Without it, killing
        only the parent leaves those grandchildren holding the pipe write-ends
        open, which would deadlock any subsequent ``communicate()``.
        """
        if os.name == "nt":
            # A new process group is required for a tree ``taskkill /T`` and
            # for CTRL-BREAK signalling. ``CREATE_NEW_PROCESS_GROUP`` only
            # exists on Windows; fall back to ``0`` so the simulated-Windows
            # branch is importable/exercisable on POSIX test hosts.
            return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
        # ``start_new_session`` runs ``setsid()`` in the child, making it a
        # session/group leader whose pgid equals its pid -- the exact owned
        # identity ``os.killpg`` targets.
        return {"start_new_session": True}

    def _signal_process_tree(
        self,
        process: subprocess.Popen[str],
        pgid: int | None,
        *,
        graceful: bool,
    ) -> None:
        """Signal the entire owned build tree, never an unrelated process/group.

        Callers guarantee ``process`` is still live and un-reaped (its PID
        therefore cannot have been reused), so signalling the exact captured
        group is safe. If the owned identity is unknown, this fails closed to
        the single child handle rather than guessing another group.
        """
        if os.name == "nt":
            args = ["taskkill", "/T", "/PID", str(process.pid)]
            if not graceful:
                args.insert(1, "/F")
            try:
                subprocess.run(
                    args,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5.0,
                    check=False,
                )
                return
            except (OSError, subprocess.SubprocessError):
                # taskkill unavailable -- fall back to the exact child handle.
                self._signal_single_child(process, graceful=graceful)
                return
        if pgid is None or pgid <= 0:
            # Unknown owned identity: fail closed to the exact child only.
            self._signal_single_child(process, graceful=graceful)
            return
        sig = signal.SIGTERM if graceful else signal.SIGKILL
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            pass
        except OSError:
            # The group is gone or not ours to signal -- never widen the blast
            # radius; fall back to the exact child handle we still own.
            self._signal_single_child(process, graceful=graceful)

    @staticmethod
    def _signal_single_child(
        process: subprocess.Popen[str], *, graceful: bool
    ) -> None:
        try:
            if graceful:
                process.terminate()
            else:
                process.kill()
        except (ProcessLookupError, OSError):
            pass

    @staticmethod
    def _wait_bounded(process: subprocess.Popen[str], *, timeout: float) -> bool:
        try:
            process.wait(timeout=timeout)
            return True
        except subprocess.TimeoutExpired:
            return False

    @staticmethod
    def _pipe_write_end_still_open(process: subprocess.Popen[str]) -> bool:
        """True iff a live process still holds the write-end of an inherited
        stdout/stderr pipe -- i.e. a same-group descendant we spawned is alive.

        This daemon's build pipes are private: only the build subprocess and
        the descendants that inherited its file descriptors can hold their
        write-ends. Once the group leader has exited, a still-open write-end is
        therefore race-free proof that an owned grandchild survives and keeps
        the owned process group non-empty (which reserves the leader's
        PID/PGID against reuse). ``select.poll`` reports ``POLLHUP`` on the
        read-end exactly when every write-end is closed, so this is
        non-destructive (no bytes are consumed) and never blocks. Windows lacks
        ``select.poll``; there we cannot make this determination and
        conservatively report "open" so no separate reuse guard weakens.
        """
        if os.name == "nt" or not hasattr(select, "poll"):
            return True
        poller = select.poll()
        registered = 0
        for stream in (getattr(process, "stdout", None), getattr(process, "stderr", None)):
            if stream is None:
                continue
            try:
                if stream.closed:
                    continue
                fd = stream.fileno()
            except (ValueError, OSError):
                continue
            poller.register(fd, select.POLLIN)
            registered += 1
        if registered == 0:
            return False
        hangups = 0
        for _fd, event in poller.poll(0):
            if event & (select.POLLHUP | select.POLLERR | select.POLLNVAL):
                hangups += 1
        # A pipe with an open, idle write-end yields no poll event at all; only
        # a fully-closed write-end reports a hangup. If every registered pipe
        # hung up, no owned descendant remains.
        return hangups < registered

    def _wait_owned_group_pipes_closed(
        self, process: subprocess.Popen[str], *, timeout: float
    ) -> bool:
        """Bounded wait until no owned descendant holds an inherited pipe open.

        Used only after the group has been signalled: the SIGKILL closes the
        surviving grandchild's inherited write-ends promptly, so this converges
        near-instantly and is capped by ``timeout`` regardless.
        """
        deadline = time.monotonic() + max(0.0, timeout)
        while self._pipe_write_end_still_open(process):
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.02)
        return True

    def _reap_owned_group_descendants(
        self,
        process: subprocess.Popen[str],
        pgid: int | None,
        *,
        grace_first: bool,
    ) -> None:
        """Reap any owned descendant that outlived the exited group leader.

        The leader's exit is NOT proof the whole tree died: a same-group
        descendant that inherited this daemon's stdout/stderr pipes may still
        be alive. A still-open inherited pipe write-end is race-free proof that
        such a descendant survives, which keeps the owned process group
        non-empty and therefore reserves the leader's PID/PGID against reuse --
        so signalling the exact captured group is still safe. Fails closed
        (signals nothing) when no such descendant survives, the owned identity
        is unknown, or on Windows (no reserved-group guarantee). ``grace_first``
        gives a descendant that has not yet received a group SIGTERM a graceful
        chance before the group SIGKILL; when the group was already SIGTERM'd it
        is ``False`` so a signal-ignoring descendant is escalated immediately.
        """
        if os.name == "nt" or pgid is None or pgid <= 0:
            return
        if not self._pipe_write_end_still_open(process):
            return
        if grace_first:
            self._signal_process_tree(process, pgid, graceful=True)
            if self._wait_owned_group_pipes_closed(process, timeout=2.0):
                return
        self._signal_process_tree(process, pgid, graceful=False)
        self._wait_owned_group_pipes_closed(process, timeout=2.0)

    def _terminate_build_process(self) -> None:
        """Terminate and reap the ENTIRE owned build process tree with bounded
        waits.

        The build subprocess leads its own process group/session (see
        ``_run_build_subprocess``), so a graceful group SIGTERM followed, only
        if needed, by a group SIGKILL kills its ``ProcessPoolExecutor`` workers
        too. Fails closed when no owned identity survives so a reused PID/PGID
        is never targeted.
        """
        with self._process_lock:
            process = self._build_process
            pgid = self._build_pgid
        if process is None:
            return
        if process.poll() is None:
            # The leader is still live and un-reaped, so its PID (== the owned
            # pgid) cannot have been recycled: signal the exact owned group and
            # bound each wait on the leader itself.
            self._signal_process_tree(process, pgid, graceful=True)
            if self._wait_bounded(process, timeout=2.0):
                # The leader exited, but that is not proof the whole tree died:
                # a pipe-inheriting grandchild can ignore the group SIGTERM and
                # outlive it. It already received that group SIGTERM, so escalate
                # any survivor straight to a group SIGKILL and bound the wait on
                # inherited-pipe closure.
                self._reap_owned_group_descendants(process, pgid, grace_first=False)
                return
            self._signal_process_tree(process, pgid, graceful=False)
            self._wait_bounded(process, timeout=2.0)
            # The group SIGKILL should have taken the whole tree, but confirm no
            # pipe-holding descendant outlived it before returning.
            self._reap_owned_group_descendants(process, pgid, grace_first=False)
            return
        # The leader has already exited/been reaped, so its PID -- and thus the
        # owned pgid -- is now eligible for reuse; a blind ``killpg`` could hit
        # an unrelated group. But a same-group descendant that inherited this
        # daemon's stdout/stderr pipes may still be alive, holding the drain
        # open and keeping the owned group non-empty. A non-empty owned group
        # reserves the leader PID/PGID against reuse for exactly as long as
        # that descendant lives, and a still-open inherited pipe write-end is
        # race-free proof that the descendant -- and therefore the exact owned
        # group -- is still ours. The leader never received our group SIGTERM,
        # so give a survivor a graceful chance first; otherwise (no such
        # descendant, unknown identity, or Windows, which has no reserved-group
        # guarantee) fail closed and signal nothing.
        self._reap_owned_group_descendants(process, pgid, grace_first=True)

    def _drain_after_stop(
        self,
        process: subprocess.Popen[str],
        pgid: int | None,
        *,
        timeout: float = 2.0,
    ) -> None:
        """Reap residual stdout/stderr after the tree is terminated, bounded.

        The owned tree is already dead, so its inherited pipe write-ends are
        closed and ``communicate`` reaches EOF promptly. If a writer somehow
        lingers past the timeout, force one final tree kill -- but ONLY while
        the original leader is still exact live and un-reaped, so its PID (==
        the owned pgid) is provably still reserved and the group signal cannot
        land on a recycled PID/PGID. Once the leader has been reaped that
        identity is eligible for reuse, so we never issue a second, stale group
        signal; we fail closed, close the pipes, and give up bounded rather
        than block the daemon thread forever.
        """
        try:
            process.communicate(timeout=timeout)
            return
        except subprocess.TimeoutExpired:
            pass
        # Establish exact live/un-reaped leader identity before force-signalling:
        # a since-reaped leader's PID (== the owned pgid) may already have been
        # recycled onto an unrelated process/group, so signalling it would widen
        # the blast radius. Fail closed and only drain when it cannot be proven
        # live and un-reaped.
        if process.poll() is None:
            self._signal_process_tree(process, pgid, graceful=False)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        self._wait_bounded(process, timeout=timeout)

    def _build_subprocess_command(self, *, incremental: bool) -> list[str]:
        return [
            sys.executable,
            "-m",
            "aiworkhub.source_graph_daemon",
            "--build-once",
            str(self.repo_root),
            "--incremental" if incremental else "--full",
        ]

    def _has_pending_journal(self) -> bool:
        """True when a non-empty SQLite journal or WAL sidecar exists for the
        canonical Source Graph database, signalling an interrupted write that
        must be recovered before any readonly probe.

        ``journal_mode=DELETE`` leaves ``<db>.sqlite-journal`` when a
        transaction was mid-flight at process exit.  A pre-existing WAL
        file (``-wal``) may also survive a crash.  A standalone ``-shm``
        shared-memory index is not a hot journal and does not trigger
        recovery.
        """
        try:
            db_path = source_graph.resolve_db_path(self.repo_root)
        except (source_graph.SourceGraphError, OSError):
            return False
        for suffix in ("-journal", "-wal"):
            candidate = Path(str(db_path) + suffix)
            try:
                if candidate.exists() and candidate.stat().st_size > 0:
                    return True
            except OSError:
                continue
        return False

    def _recover_database(self) -> dict[str, Any]:
        """Single-flight writable recovery of a potentially interrupted SQLite
        database.  Only one caller per daemon acquires the recovery lock;
        subsequent callers receive the coalesced result immediately.

        Returns a JSON-safe dict with ``recovered``: bool | None, ``error``: str,
        and ``phase``: str describing the terminal recovery stage.
        """
        if not self._recovery_lock.acquire(blocking=False):
            # Another thread already owns recovery -- coalesce.
            return {
                "recovered": None,
                "phase": self._recovery_phase,
                "error": self._recovery_error,
                "coalesced": True,
            }
        try:
            self._recovery_started_at = time.monotonic()
            with self._state_lock:
                self._status = STATUS_RECOVERY
                self._recovery_phase = _RECOVERY_PHASE_DETECT
                self._recovery_elapsed = 0.0
                self._recovery_error = ""
                # Snapshot the last-known-good canonical generation *before*
                # recovery mutates the database, so failed recovery can
                # preserve and re-expose this identity.
                if self._last_report:
                    self._last_known_good_generation = {
                        "build_revision": str(self._last_report.get("build_revision") or ""),
                        "finished_at": str(self._last_report.get("finished_at") or ""),
                        "files_seen": int(self._last_report.get("files_seen") or 0),
                    }
            try:
                db_path = source_graph.resolve_db_path(self.repo_root)
            except (source_graph.SourceGraphError, OSError) as exc:
                with self._state_lock:
                    self._recovery_error = f"resolve_path:{type(exc).__name__}:{exc}"[:300]
                    self._recovery_phase = _RECOVERY_PHASE_DETECT
                    self._recovery_elapsed = time.monotonic() - self._recovery_started_at
                    self._recovery_started_at = 0.0
                return {"recovered": False, "phase": self._recovery_phase, "error": self._recovery_error}

            if not db_path.exists():
                # No canonical database present.  A non-empty journal/WAL
                # sidecar surviving alongside a missing canonical DB means
                # the generation was removed mid-write: that is an explicit
                # degraded failure, NOT a clean "nothing to recover"
                # success.  Preserve the last-known-good generation and
                # freeze the elapsed diagnostic; never auto-delete or
                # fabricate readiness.
                if self._has_pending_journal():
                    with self._state_lock:
                        self._recovery_error = "missing_database_with_pending_journal"
                        self._recovery_phase = _RECOVERY_PHASE_DETECT
                        self._recovery_elapsed = (
                            time.monotonic() - self._recovery_started_at
                        )
                        self._recovery_started_at = 0.0
                        self._status = STATUS_DEGRADED
                    return {
                        "recovered": False,
                        "phase": self._recovery_phase,
                        "error": self._recovery_error,
                    }
                # No database yet and no pending journal -- genuinely
                # nothing to recover; this is a normal first-run state.
                with self._state_lock:
                    self._recovery_phase = ""
                    self._recovery_elapsed = (
                        time.monotonic() - self._recovery_started_at
                    )
                    self._recovery_started_at = 0.0
                    self._recovery_error = ""
                    self._status = STATUS_STOPPED
                return {"recovered": True, "phase": "", "error": ""}

            # Phase: writable open -- SQLite auto-recovers the journal/WAL on
            # first connect in read_write mode.  This is the canonical
            # recovery step prescribed by SQLite's hot-journal protocol.
            self._recovery_phase = _RECOVERY_PHASE_OPEN
            self._recovery_elapsed = time.monotonic() - self._recovery_started_at
            try:
                conn = source_graph.connect(db_path, read_only=False)
            except source_graph.SourceGraphBuildInProgressError:
                # Another process holds WAL -- we cannot force recovery here.
                with self._state_lock:
                    self._recovery_error = "wal_held_by_other_process"
                    self._recovery_phase = _RECOVERY_PHASE_OPEN
                    self._recovery_elapsed = time.monotonic() - self._recovery_started_at
                    self._recovery_started_at = 0.0
                return {"recovered": False, "phase": self._recovery_phase, "error": self._recovery_error}
            except (OSError, sqlite3.Error) as exc:
                with self._state_lock:
                    self._recovery_error = f"connect:{type(exc).__name__}:{exc}"[:300]
                    self._recovery_phase = _RECOVERY_PHASE_OPEN
                    self._recovery_elapsed = time.monotonic() - self._recovery_started_at
                    self._recovery_started_at = 0.0
                return {"recovered": False, "phase": self._recovery_phase, "error": self._recovery_error}

            try:
                # Phase: integrity check on the recovered database.
                self._recovery_phase = _RECOVERY_PHASE_INTEGRITY
                self._recovery_elapsed = time.monotonic() - self._recovery_started_at
                try:
                    integrity = conn.execute("PRAGMA integrity_check").fetchone()
                    if integrity and str(integrity[0]).lower() != "ok":
                        raise sqlite3.DatabaseError(f"integrity_check:{integrity[0]}"[:300])
                except sqlite3.Error as exc:
                    # Integrity failure -- do NOT delete the database.
                    # Preserve the canonical index so a human or later retry
                    # can salvage what remains.
                    with self._state_lock:
                        self._recovery_error = f"integrity:{type(exc).__name__}:{exc}"[:300]
                        self._recovery_phase = _RECOVERY_PHASE_INTEGRITY
                        self._status = STATUS_DEGRADED
                        self._recovery_elapsed = time.monotonic() - self._recovery_started_at
                        self._recovery_started_at = 0.0
                    return {"recovered": False, "phase": self._recovery_phase, "error": self._recovery_error}

                # Phase: commit recovery by ensuring journal_mode=DELETE and
                # persisting any recovered state.
                self._recovery_phase = _RECOVERY_PHASE_COMMIT
                self._recovery_elapsed = time.monotonic() - self._recovery_started_at
                conn.commit()

                # Re-read last_build from the recovered database to refresh
                # the daemon's in-memory state so the health surface stays
                try:
                    row = conn.execute(
                        "SELECT value FROM meta WHERE key='last_build'"
                    ).fetchone()
                    if row is None:
                        # A recovered canonical database must carry a
                        # generation identity.  An absent row is not an
                        # acceptable recovered state; fail bounded at commit,
                        # preserve LKG, and never auto-delete.
                        raise KeyError("last_build")
                    payload = json.loads(str(row["value"]))
                    if not isinstance(payload, dict):
                        raise TypeError("last_build payload is not a JSON object")
                    build_revision_raw = payload.get("build_revision")
                    finished_at_raw = payload.get("finished_at")
                    files_seen_raw = payload.get("files_seen")
                    if (
                        not isinstance(build_revision_raw, str)
                        or not build_revision_raw
                        or not isinstance(finished_at_raw, str)
                        or not finished_at_raw
                    ):
                        raise ValueError("missing or empty generation metadata")
                    if (
                        not isinstance(files_seen_raw, int)
                        or isinstance(files_seen_raw, bool)
                        or files_seen_raw < 0
                    ):
                        raise ValueError("files_seen must be a nonnegative integer")
                    build_revision = build_revision_raw
                    finished_at = finished_at_raw
                    files_seen = files_seen_raw
                    with self._state_lock:
                        if not self._last_report:
                            self._last_report = {}
                        self._last_report["build_revision"] = build_revision
                        self._last_report["finished_at"] = finished_at
                        self._last_report["files_seen"] = files_seen
                        self._last_success_at = finished_at
                except (
                    json.JSONDecodeError,
                    TypeError,
                    KeyError,
                    ValueError,
                    AttributeError,
                ) as exc:
                    # Missing or malformed generation metadata is an explicit
                    # bounded degraded failure at commit phase.  Preserve LKG
                    # and frozen elapsed; never auto-delete the canonical DB.
                    with self._state_lock:
                        self._recovery_error = (
                            f"generation_meta:{type(exc).__name__}:{exc}"[:300]
                        )
                        self._recovery_phase = _RECOVERY_PHASE_COMMIT
                        self._status = STATUS_DEGRADED
                        self._recovery_elapsed = (
                            time.monotonic() - self._recovery_started_at
                        )
                        self._recovery_started_at = 0.0
                    return {
                        "recovered": False,
                        "phase": self._recovery_phase,
                        "error": self._recovery_error,
                    }
                except sqlite3.Error as exc:
                    # Meta query error after recovery; preserve LKG, flag
                    # degraded, freeze elapsed, and return the diagnostic.
                    with self._state_lock:
                        self._recovery_error = (
                            f"meta_query:{type(exc).__name__}:{exc}"[:300]
                        )
                        self._recovery_phase = _RECOVERY_PHASE_COMMIT
                        self._status = STATUS_DEGRADED
                        self._recovery_elapsed = (
                            time.monotonic() - self._recovery_started_at
                        )
                        self._recovery_started_at = 0.0
                    return {
                        "recovered": False,
                        "phase": self._recovery_phase,
                        "error": self._recovery_error,
                    }

                with self._state_lock:
                    self._recovery_phase = ""
                    self._recovery_elapsed = time.monotonic() - self._recovery_started_at
                    self._recovery_started_at = 0.0
                    self._recovery_error = ""
                    self._status = STATUS_STOPPED
                return {"recovered": True, "phase": _RECOVERY_PHASE_COMMIT, "error": ""}
            finally:
                conn.close()
        finally:
            self._recovery_lock.release()

    def _run_build_subprocess(self, *, incremental: bool) -> dict[str, Any]:
        command = self._build_subprocess_command(incremental=incremental)
        child_env = os.environ.copy()
        # A source checkout may be imported by pytest/editor tooling without
        # being installed into ``sys.executable``. Preserve the exact package
        # root for the dedicated child; packaged/venv installs simply receive
        # their already-importable site-packages path again. This also keeps
        # the daemon independent of the caller's current working directory.
        package_root = str(Path(__file__).resolve().parent.parent)
        child_env["PYTHONPATH"] = os.pathsep.join(
            value for value in (package_root, child_env.get("PYTHONPATH", "")) if value
        )
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=child_env,
            **self._new_process_group_popen_kwargs(),
        )
        # A session/group-leader's pgid equals its pid. Capture that exact
        # owned identity now, while the child is un-reaped, so termination can
        # never target a recycled PID's unrelated process group.
        pgid = None if os.name == "nt" else process.pid
        with self._process_lock:
            self._build_process = process
            self._build_pgid = pgid
        try:
            while True:
                try:
                    stdout, stderr = process.communicate(timeout=0.25)
                    break
                except subprocess.TimeoutExpired:
                    if self._stop_event.is_set():
                        # Reap the WHOLE owned tree first so no inherited-pipe
                        # grandchild keeps the drain open, then drain residual
                        # output with bounded waits -- never an unbounded
                        # communicate().
                        self._terminate_build_process()
                        self._drain_after_stop(process, pgid)
                        return {"kind": "stopped"}
            if process.returncode != 0:
                detail = (stderr or stdout or f"exit_{process.returncode}").strip()
                return {"kind": "error", "error": f"index_subprocess:{detail}"[:500]}
            try:
                payload = json.loads(stdout)
            except (TypeError, json.JSONDecodeError):
                return {"kind": "error", "error": "index_subprocess:invalid_result"}
            if not isinstance(payload, dict) or payload.get("kind") not in {
                "success", "standby", "error"
            }:
                return {"kind": "error", "error": "index_subprocess:invalid_contract"}
            return payload
        finally:
            with self._process_lock:
                if self._build_process is process:
                    self._build_process = None
                    self._build_pgid = None

    def _execute_build(self, *, incremental: bool) -> dict[str, Any]:
        if self._build_execution == BUILD_EXECUTION_THREAD:
            return _build_once_payload(self.repo_root, incremental=incremental)
        try:
            return self._run_build_subprocess(incremental=incremental)
        except (OSError, subprocess.SubprocessError) as exc:
            return {"kind": "error", "error": f"index_subprocess:{type(exc).__name__}:{exc}"[:500]}

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
                outcome = self._execute_build(incremental=incremental)
                kind = outcome.get("kind")
                if kind == "standby":
                    with self._state_lock:
                        self._status = STATUS_STANDBY
                        self._last_error = ""
                        self._last_run_at = _utcnow()
                    return True
                if kind == "stopped":
                    with self._state_lock:
                        self._status = STATUS_STOPPED
                        self._last_error = ""
                        self._last_run_at = _utcnow()
                    return True
                if kind != "success":
                    raise RuntimeError(str(outcome.get("error") or "index_build_failed"))
                report = dict(outcome["report"])
                files_changed = int(report.get("files_changed") or 0)
                files_removed = int(report.get("files_removed") or 0)
                with self._state_lock:
                    previous_report = self._last_report
                previous_recommendation = (
                    previous_report.get("recommendation_resolvability")
                    if isinstance(previous_report, dict) else None
                )
                reuse_recommendation = bool(
                    files_changed == 0 and files_removed == 0
                    and isinstance(previous_recommendation, dict)
                    and previous_recommendation.get("ok")
                )
                if reuse_recommendation:
                    # A true no-op generation (nothing changed or removed)
                    # leaves the graph identical to the prior generation --
                    # the recommendation round-trip gate already proved
                    # resolvability against that unchanged graph, so
                    # re-running the whole sampled-symbol probe would spend
                    # real work reconfirming a fact that cannot have
                    # changed. Reuse the prior probe's verdict verbatim.
                    recommendation = dict(previous_recommendation)
                    recommendation["reused_from_previous_generation"] = True
                else:
                    # The query engine and MCP wrapper have different failure
                    # surfaces.  Exercise the exact shared manager/worker
                    # wrapper once per committed generation so health can
                    # distinguish an emission/index fault from a wrapper
                    # regression instead of discovering it after a worker
                    # wastes a follow-up call.
                    try:
                        from . import worker_ai_tools_mcp

                        selfcheck_context = worker_ai_tools_mcp.WorkerToolContext(
                            task_id="source-graph:selfcheck",
                            runner="source_graph_daemon",
                            topic="source_graph_health",
                            request_id=f"source-graph:{report.get('finished_at') or _utcnow()}",
                            repo=self.repo_root,
                            authority_repo=self.repo_root,
                            source_graph_targets=(),
                            session_topic="source_graph_health",
                            audit_ledger_path=None,
                            audit_hmac_key_path=None,
                        )
                        recommendation = (
                            worker_ai_tools_mcp.source_graph_recommendation_roundtrip_gate(
                                selfcheck_context,
                            )
                        )
                    except Exception as exc:  # noqa: BLE001 -- telemetry cannot erase a good index
                        recommendation = {
                            "schema_id": "aiworkhub.source_graph.recommendation_roundtrip.v1",
                            "ok": False,
                            "status": "probe_failed",
                            "error": f"{type(exc).__name__}:{exc}"[:300],
                            "sampled_symbols": 0,
                            "emitted": 0,
                            "resolved": 0,
                            "resolvability_ratio": None,
                            "failures": [],
                        }
                    recommendation["reused_from_previous_generation"] = False
                recommendation["build_revision"] = str(
                    report.get("build_revision") or source_graph.BUILD_REVISION
                )
                recommendation["finished_at"] = str(report.get("finished_at") or "")
                try:
                    source_graph.record_recommendation_roundtrip(
                        self.repo_root, recommendation,
                    )
                except (OSError, sqlite3.Error, source_graph.SourceGraphError) as exc:
                    recommendation["persistence_error"] = (
                        f"{type(exc).__name__}:{exc}"[:300]
                    )
                report["recommendation_resolvability"] = recommendation
                with self._state_lock:
                    # A successful SQLite transaction is not the same thing
                    # as a usable Source Graph.  Keep empty repositories
                    # truthful so code-task gates cannot mistake a zero-row
                    # database for an indexed project.
                    self._status = (
                        STATUS_READY if int(report.get("files_seen", 0)) > 0 else STATUS_EMPTY
                    )
                    self._last_report = report
                    self._last_error = ""
                    self._last_run_at = _utcnow()
                    self._last_success_at = self._last_run_at
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
            # Completion is an acquire-ready handoff, not merely a report
            # that parsing stopped.  Wake waiters only after releasing the
            # non-overlap guard so an immediate refresh_now() can perform the
            # requested follow-up build instead of observing a false
            # build_in_progress result and returning the previous generation.
            self._build_completed.set()

    def _loop(self) -> None:
        # Power-loss recovery: writable single-flight recovery must precede
        # any readonly probe when a non-empty journal/WAL exists.
        if self._has_pending_journal():
            result = self._recover_database()
            if not result.get("recovered"):
                # Recovery failed or is still in-progress under another
                # owner.  Retain degraded diagnostics and last-known-good
                # generation; retry recovery on the next cycle before any
                # readonly probe.
                if result.get("recovered") is not None:
                    # Explicit failure (not coalesced): ensure degraded.
                    with self._state_lock:
                        if self._status == STATUS_RECOVERY:
                            self._status = STATUS_DEGRADED
                        if not self._recovery_error:
                            self._recovery_error = (
                                result.get("error") or "recovery_failed"
                            )[:300]
                    # Signal lifecycle completion so callers do not block
                    # indefinitely.  Health remains degraded and build_count
                    # stays at zero; callers MUST inspect health after the wait.
                    self._build_completed.set()
            else:
                self._run_one_build()
        else:
            self._run_one_build()
        while not self._stop_event.is_set():
            self._refresh_event.wait(self.refresh_interval_seconds)
            self._refresh_event.clear()
            if self._stop_event.is_set():
                break
            refresh_job = self._begin_refresh_job()
            # Retry recovery before any probe when journal/WAL persists.
            if self._has_pending_journal():
                result = self._recover_database()
                if not result.get("recovered"):
                    if refresh_job is not None:
                        self._finish_refresh_job(
                            refresh_job,
                            state="failed",
                            error=str(result.get("error") or "recovery_failed"),
                        )
                    continue
            triggered = self._run_one_build()
            if refresh_job is None:
                continue
            if not triggered:
                self._finish_refresh_job(
                    refresh_job, state="failed", error="build_in_progress"
                )
                continue
            with self._state_lock:
                status = self._status
                error = self._last_error
            state, terminal_error = self._refresh_terminal_state(status, error)
            self._finish_refresh_job(refresh_job, state=state, error=terminal_error)

    def start(self) -> None:
        """Idempotent: a second call while already running is a no-op.

        Returns immediately -- the (possibly full) first build runs on the
        background thread, never blocking this call's caller.
        """
        with self._state_lock:
            if self.is_running():
                return
            self._stop_event.clear()
            self._refresh_event.clear()
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
        self._refresh_event.set()
        self._terminate_build_process()
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
        flight (periodic tick or another ``refresh_now()``), this arms the
        binary refresh event so the loop consumes exactly one follow-up
        build after the current build completes. Multiple concurrent
        ``refresh_now()`` calls while one build is in flight coalesce into
        exactly one follow-up build, with no duplicate overlap.

        Also reserves and completes one durable refresh job so callers
        observe the same job identity / terminal-status contract as
        ``request_refresh()``.
        """
        job, coalesced = self._reserve_refresh_job()
        if not coalesced:
            persisted, persist_error = self._persist_refresh_job(job)
            if not persisted:
                with self._state_lock:
                    self._pending_refresh_job = None
                    self._last_refresh_job = {
                        **job,
                        "state": "failed",
                        "finished_at": _utcnow(),
                        "error": persist_error,
                    }
                return {
                    **self.health(),
                    "ok": False,
                    "triggered": False,
                    "job_id": job["job_id"],
                    "refresh_job": dict(self._last_refresh_job),
                    "reason": "refresh_job_persist_failed",
                }
        active_job = self._begin_refresh_job()
        triggered = self._run_one_build()
        if not triggered:
            self._refresh_event.set()
            if active_job is not None:
                self._finish_refresh_job(
                    active_job, state="failed", error="build_in_progress"
                )
            health = self.health()
            return {**health, "ok": True, "triggered": False, "reason": "build_in_progress"}
        if active_job is not None:
            with self._state_lock:
                status = self._status
                error = self._last_error
            state, terminal_error = self._refresh_terminal_state(status, error)
            self._finish_refresh_job(active_job, state=state, error=terminal_error)
        return {**self.health(), "triggered": True}

    def request_refresh(self) -> dict[str, Any]:
        """Queue an immediate refresh without running indexing on the caller.

        MCP stdio request handling must stay responsive while a large source
        tree is parsed. Multiple requests coalesce into one wake-up and the
        existing build lock still guarantees that periodic and requested
        builds never overlap.
        """

        if not self.is_running():
            self.start()
        job, coalesced = self._reserve_refresh_job()
        if not coalesced:
            persisted, persist_error = self._persist_refresh_job(job)
            if not persisted:
                with self._state_lock:
                    self._pending_refresh_job = None
                    self._last_refresh_job = {
                        **job,
                        "state": "failed",
                        "finished_at": _utcnow(),
                        "error": persist_error,
                    }
                return {
                    **self.health(),
                    "ok": False,
                    "triggered": False,
                    "queued": False,
                    "coalesced": False,
                    "job_id": job["job_id"],
                    "refresh_job": dict(self._last_refresh_job),
                    "reason": "refresh_job_persist_failed",
                }
        self._refresh_event.set()
        health = self.health()
        return {
            **health,
            "ok": True,
            "triggered": True,
            "queued": True,
            "coalesced": coalesced,
            "job_id": job["job_id"],
            "refresh_job": job,
            "reason": "refresh_queued",
        }

    def wait_for_first_build(self, *, timeout: float = 10.0) -> bool:
        """Wait for the first build attempt without polling or sleeping.

        This is primarily a deterministic lifecycle synchronization surface
        for release qualification. It is also useful to callers that choose
        to wait for initial indexing after the intentionally non-blocking
        InitRepo call. Repeated calls remain idempotent once the event is set.
        """

        return self._build_completed.wait(timeout=max(0.0, float(timeout)))

    def health(self) -> dict[str, Any]:
        with self._state_lock:
            running = self.is_running()
            status = self._status
            if not running and self._thread is not None:
                status = STATUS_STOPPED
            index_age_seconds: float | None = None
            freshness_anchor = self._last_success_at or self._started_at
            if freshness_anchor:
                try:
                    anchor = datetime.fromisoformat(freshness_anchor)
                    index_age_seconds = max(
                        0.0,
                        (datetime.now(timezone.utc) - anchor).total_seconds(),
                    )
                except ValueError:
                    index_age_seconds = None
            stale = bool(
                running
                and status not in {STATUS_STOPPED, STATUS_DEGRADED, STATUS_RECOVERY}
                and index_age_seconds is not None
                and index_age_seconds > self.stale_after_seconds
            )
            reported_status = STATUS_STALE if stale else status
            last_report = self._last_report or {}
            # Recovery surface: always expose phase, elapsed_seconds, and
            # error so callers receive a stable schema in every state
            # (stopped, healthy, recovery, degraded).  Terminal phase/elapsed
            # and actionable error are retained on failure instead of being
            # dropped when status transitions away from RECOVERY.
            recovery_info: dict[str, Any] = {
                "error": self._recovery_error,
                "phase": self._recovery_phase,
                "elapsed_seconds": (
                    round(time.monotonic() - self._recovery_started_at, 3)
                    if self._recovery_started_at > 0
                    else round(self._recovery_elapsed, 3)
                ),
            }
            lkg = dict(self._last_known_good_generation) if self._last_known_good_generation else {}
            refresh_job = dict(
                self._active_refresh_job
                or self._pending_refresh_job
                or self._last_refresh_job
                or {}
            )
            return {
                "ok": reported_status not in {STATUS_DEGRADED, STATUS_STALE, STATUS_RECOVERY},
                "status": reported_status,
                "running": running,
                "repo_root": str(self.repo_root),
                "refresh_interval_seconds": self.refresh_interval_seconds,
                "stale_after_seconds": self.stale_after_seconds,
                "started_at": self._started_at,
                "last_run_at": self._last_run_at,
                "last_success_at": self._last_success_at,
                "build_revision": str(last_report.get("build_revision") or ""),
                "files_seen": int(last_report.get("files_seen") or 0),
                "hash_workers": int(last_report.get("hash_workers") or 0),
                "hash_candidates": int(last_report.get("hash_candidates") or 0),
                "hash_reused": int(last_report.get("hash_reused") or 0),
                "quality_reused": bool(last_report.get("quality_reused") or False),
                "recommendation_reused": bool(
                    (last_report.get("recommendation_resolvability") or {}).get(
                        "reused_from_previous_generation"
                    )
                    or False
                ),
                "index_age_seconds": index_age_seconds,
                "stale_reason": "last_success_exceeded_threshold" if stale else "",
                "last_report": self._last_report,
                "last_error": self._last_error,
                "writer_state": "standby" if status == STATUS_STANDBY else "active",
                "language_capabilities": dict(source_graph.LANGUAGE_CAPABILITIES),
                "indexed_extensions": list(source_graph.INDEXED_EXTENSIONS),
                "recovery": recovery_info,
                "last_known_good_generation": lkg,
                "refresh_job_id": str(refresh_job.get("job_id") or ""),
                "refresh_job": refresh_job or None,
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
    """Read-only daemon and canonical-generation health for a repository.

    An unregistered daemon is a normal, not-degraded process state.  It does
    not mean that a generation produced by a one-shot or external builder is
    unavailable, so the canonical database is still probed below.
    """
    daemon = get_daemon(repo_root)
    registered = daemon is not None
    if daemon is None:
        out = {
            "ok": True,
            "status": STATUS_STOPPED,
            "running": False,
            "repo_root": str(Path(repo_root).resolve()),
            "refresh_interval_seconds": None,
            "stale_after_seconds": None,
            "started_at": "",
            "last_run_at": "",
            "last_success_at": "",
            "build_revision": "",
            "files_seen": 0,
            "index_age_seconds": None,
            "stale_reason": "",
            "last_report": None,
            "last_error": "",
            "language_capabilities": dict(source_graph.LANGUAGE_CAPABILITIES),
            "indexed_extensions": list(source_graph.INDEXED_EXTENSIONS),
            "index_quality": None,
            "recommendation_resolvability": None,
            "guidance_degraded": False,
            "registered": False,
            "recovery": {"error": "", "phase": "", "elapsed_seconds": 0.0},
            "last_known_good_generation": {},
        }
    else:
        out = daemon.health()
    # Writable single-flight recovery MUST precede every readonly probe
    # when the daemon is actively recovering or a non-empty journal/WAL
    # sidecar still exists.  During recovery, return the bounded process
    # diagnostics (phase/elapsed/error/LKG) without opening any readonly
    # database connection.
    if daemon is not None and (
        out.get("status") == STATUS_RECOVERY or daemon._has_pending_journal()
    ):
        out["registered"] = True
        return out
    # The canonical committed generation remains readable while another
    # process is INDEXING, while this daemon is STANDBY, and after a daemon
    # restart before its first successful refresh.  Health used to hydrate
    # metadata only in one STANDBY branch, producing the split-brain state
    # where live MCP queries succeeded but preflight reported files_seen=0.
    # Reconcile immutable generation identity on every health read.  Writer
    # activity/status remains independently visible in ``status``.
    out["readable_generation"] = bool(
        out.get("last_success_at")
        and out.get("build_revision")
        and int(out.get("files_seen") or 0) > 0
    )
    out["generation_read_error"] = ""
    out["index_quality"] = None
    out["recommendation_resolvability"] = None
    out["guidance_degraded"] = False
    try:
        db_path = source_graph.resolve_db_path(Path(repo_root).resolve())
        if db_path.exists():
            rows = _read_canonical_meta_readonly(repo_root, db_path)
            payload = json.loads(rows.get("last_build", "{}"))
            finished_at = str(payload.get("finished_at") or "")
            build_revision = str(payload.get("build_revision") or "")
            files_seen = int(payload.get("files_seen") or 0)
            if finished_at and build_revision and files_seen > 0:
                out["last_success_at"] = finished_at
                out["build_revision"] = build_revision
                out["files_seen"] = files_seen
                out["readable_generation"] = True
                try:
                    anchor = datetime.fromisoformat(finished_at)
                    age = max(
                        0.0,
                        (datetime.now(timezone.utc) - anchor).total_seconds(),
                    )
                except ValueError:
                    age = None
                out["index_age_seconds"] = age
                stale_after = out.get("stale_after_seconds")
                if age is not None and stale_after is not None:
                    if age > float(stale_after):
                        out["ok"] = False
                        out["stale_reason"] = "last_success_exceeded_threshold"
                    elif out.get("status") == STATUS_STALE:
                        # The stale label was computed from process-local
                        # startup state. A newer canonical generation is the
                        # freshness authority, while current writer activity
                        # remains visible through writer_state/running.
                        out["ok"] = True
                        out["stale_reason"] = ""
            for meta_key, health_key in (
                ("index_quality", "index_quality"),
                ("recommendation_roundtrip", "recommendation_resolvability"),
            ):
                raw = rows.get(meta_key)
                metric = json.loads(raw) if raw else None
                if not isinstance(metric, dict):
                    out[health_key] = None
                    continue
                same_generation = (
                    str(metric.get("build_revision") or "") == build_revision
                    and str(metric.get("finished_at") or "") == finished_at
                )
                out[health_key] = {
                    **metric,
                    "current_generation": same_generation,
                }
            recommendation = out.get("recommendation_resolvability")
            out["guidance_degraded"] = bool(
                isinstance(recommendation, dict)
                and recommendation.get("current_generation")
                and recommendation.get("status") != "ready"
            )
    except (
        OSError,
        ValueError,
        TypeError,
        json.JSONDecodeError,
        sqlite3.Error,
        source_graph.SourceGraphError,
    ) as exc:
        # Preserve a previously known readable generation. A transient health
        # probe failure must not erase valid local identity or contradict a
        # live query; expose the bounded diagnostic separately.
        out["generation_read_error"] = f"{type(exc).__name__}:{exc}"[:300]
    out["registered"] = registered
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
    "DEFAULT_STALE_MULTIPLIER",
    "MIN_REFRESH_INTERVAL_SECONDS",
    "MIN_STALE_AFTER_SECONDS",
    "STATUS_DEGRADED",
    "STATUS_EMPTY",
    "STATUS_INDEXING",
    "STATUS_READY",
    "STATUS_RECOVERY",
    "STATUS_STANDBY",
    "STATUS_STALE",
    "STATUS_STOPPED",
    "SourceGraphDaemon",
    "daemon_health",
    "ensure_started",
    "get_daemon",
    "refresh_now",
    "stop_all_daemons",
    "stop_daemon",
]


def _main() -> int:
    if len(sys.argv) != 4 or sys.argv[1] != "--build-once" or sys.argv[3] not in {
        "--incremental", "--full"
    }:
        return 2
    payload = _build_once_payload(
        Path(sys.argv[2]), incremental=sys.argv[3] == "--incremental"
    )
    sys.stdout.write(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
