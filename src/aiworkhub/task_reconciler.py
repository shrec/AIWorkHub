"""Idempotent, provider-neutral durable worker reconciler (B412).

Closes the measured B411 launcher-loss gap: isolated worker request
``d6b6e8ee4080420fb555692e741452bf`` exited successfully at
2026-07-15T14:21:41Z but stayed ``processing`` until a coordinator manually
invoked reconciliation an hour later. The gap was structural, not a logic
bug: ``process_launcher.py::ProcessManager`` only re-scans persisted
requests when a NEW ``ProcessManager`` happens to be constructed (MCP
server start, or an explicit ``status``/``list_processes``/``reconcile``
call) -- if nothing calls it, an exited worker sits unfinalized forever.

This module adds no second task queue and no duplicate promotion logic. It
only repeatedly drives the EXISTING
``ProcessManager._reconcile_persisted_requests`` / ``_finalize_isolated_request``
path (scope validation, validation commands, promotion, ``taskctl review``
for a clean exit; review/blocked routing with a normalized outcome for
lost/timed-out/cancelled/failed work -- never pending, never automatic
retry/relaunch) on a bounded scan interval, from a process that survives
every MCP/VS Code/launcher restart. A single-instance advisory lock keeps
one reconciler running per repo; the reconciliation work itself is already
interprocess-safe via ``ProcessManager._registry_lock`` (flock), so a
duplicate scan -- from two lock holders racing, or the same holder scanning
twice -- is always a no-op: an already-terminal request is left alone, and
a still actively-supervised request is left alone.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import signal
import stat
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import core
from . import process_launcher
from .platform_io import (
    chmod_fd,
    is_windows,
    lock_fd,
    stat_owned_by_current_user,
    unlock_fd,
)


DEFAULT_SCAN_INTERVAL_SECONDS = 30.0
MIN_SCAN_INTERVAL_SECONDS = 5.0
MAX_SCAN_INTERVAL_SECONDS = 3600.0
SCAN_INTERVAL_ENV = "AIWORKHUB_RECONCILER_SCAN_INTERVAL_SECONDS"
# Repository-local, non-durable runtime tree (never the historical
# any package-install/monorepo lock path): .aiworkhub/runtime/locks/.
LOCK_REL_PATH = Path(".aiworkhub/runtime/locks/task_reconciler.lock")
# The reconciler is the only thing that finalizes an exited worker, and its
# health used to live in one process's memory: if the thread never started or
# died, every surface still answered "fine" and cards sat in `processing`
# forever. The scan record is written to disk so any process -- a manager chat,
# the dashboard, a later server -- can ask when the loop last closed and get an
# answer that outlives the process that produced it.
STATUS_REL_PATH = Path(".aiworkhub/runtime/task_reconciler_status.json")
# A heartbeat is only evidence while it is recent. Past this many missed
# intervals the record is reported stale rather than healthy, because a scan
# that last ran hours ago is indistinguishable from no reconciler at all.
STALE_SCAN_INTERVALS = 4.0
MIN_STALE_SCAN_SECONDS = 300.0
# Finalizing exited workers is correctness and runs every pass (0.01 s
# measured). Sweeping retained workspaces is housekeeping whose cost is
# dominated by re-proving that pinned rework predecessors are still pinned --
# ~100 of them at ~3 s each on this repository. Running both on one cadence
# made a finished card's time-to-review hostage to garbage collection, so the
# sweep runs on every Nth pass instead.
GC_SCAN_EVERY_N_PASSES = 20
AUTHORITY_RETRY_SECONDS = 0.25


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _scan_interval_seconds() -> float:
    try:
        value = float(os.environ.get(SCAN_INTERVAL_ENV, str(DEFAULT_SCAN_INTERVAL_SECONDS)))
    except (TypeError, ValueError):
        value = DEFAULT_SCAN_INTERVAL_SECONDS
    return max(MIN_SCAN_INTERVAL_SECONDS, min(value, MAX_SCAN_INTERVAL_SECONDS))


def _process_identity() -> dict[str, Any]:
    """Return the strongest process identity this platform can prove."""

    return {
        "owner_pid": os.getpid(),
        "owner_pid_start_ticks": process_launcher._pid_start_ticks(os.getpid()),
    }


class ReconcilerLockHeld(RuntimeError):
    """Another reconciler instance already holds the single-instance lock."""


class ReconcilerLockUnsafe(RuntimeError):
    """The authority path cannot safely identify an ordinary lock file."""


def _lock_metadata_unsafe(metadata: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or bool(getattr(metadata, "st_file_attributes", 0) & reparse_flag)
        or not stat_owned_by_current_user(metadata)
    )


def _lock_path_metadata(lock_path: Path) -> os.stat_result | None:
    try:
        return os.stat(lock_path, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ReconcilerLockUnsafe(f"reconciler_lock_unsafe:{lock_path}") from exc


def _directory_identity(path: Path) -> tuple[int, int]:
    try:
        metadata = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise ReconcilerLockUnsafe(f"reconciler_lock_unsafe:{path}") from exc
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or bool(getattr(metadata, "st_file_attributes", 0) & reparse_flag)
        or not stat_owned_by_current_user(metadata)
    ):
        raise ReconcilerLockUnsafe(f"reconciler_lock_unsafe:{path}")
    return metadata.st_dev, metadata.st_ino


@contextlib.contextmanager
def single_instance_lock(lock_path: Path):
    """Bounded, non-blocking advisory single-instance lock.

    The repository descriptor is a stable guard for the canonical lock-parent
    chain on POSIX.  The lock itself is opened relative to a bound parent
    descriptor, whose pathname identity is revalidated after acquisition.
    """
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent_identity = _directory_identity(lock_path.parent)
    directory_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        directory_flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW

    repo_fd: int | None = None
    repo_locked = False
    rel_parts = LOCK_REL_PATH.parts
    is_canonical_path = (
        len(lock_path.parts) > len(rel_parts)
        and lock_path.parts[-len(rel_parts):] == rel_parts
    )
    if not is_windows() and is_canonical_path:
        repo_path = lock_path.parents[len(rel_parts) - 1]
        try:
            repo_fd = os.open(repo_path, directory_flags)
            lock_fd(repo_fd, blocking=False)
            repo_locked = True
        except OSError as exc:
            if repo_fd is not None:
                os.close(repo_fd)
            raise ReconcilerLockHeld(f"reconciler_lock_held:{lock_path}") from exc

    try:
        parent_fd = os.open(lock_path.parent, directory_flags)
    except OSError as exc:
        if repo_locked and repo_fd is not None:
            with contextlib.suppress(OSError):
                unlock_fd(repo_fd)
            os.close(repo_fd)
        raise ReconcilerLockUnsafe(f"reconciler_lock_unsafe:{lock_path.parent}") from exc
    fd: int | None = None
    try:
        parent_metadata = os.fstat(parent_fd)
        if (parent_metadata.st_dev, parent_metadata.st_ino) != parent_identity:
            raise ReconcilerLockUnsafe(f"reconciler_lock_unsafe:{lock_path.parent}")
        before = _lock_path_metadata(lock_path)
        if before is not None and _lock_metadata_unsafe(before):
            raise ReconcilerLockUnsafe(f"reconciler_lock_unsafe:{lock_path}")
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            if os.open in os.supports_dir_fd:
                fd = os.open(lock_path.name, flags, 0o600, dir_fd=parent_fd)
            else:
                fd = os.open(lock_path, flags, 0o600)
        except OSError as exc:
            raise ReconcilerLockUnsafe(f"reconciler_lock_unsafe:{lock_path}") from exc
        metadata = os.fstat(fd)
        after = _lock_path_metadata(lock_path)
        identity = (metadata.st_dev, metadata.st_ino)
        unsafe = (
            _lock_metadata_unsafe(metadata)
            or after is None
            or _lock_metadata_unsafe(after)
            or (after.st_dev, after.st_ino) != identity
            or (before is not None and (before.st_dev, before.st_ino) != identity)
        )
        if unsafe:
            raise ReconcilerLockUnsafe(f"reconciler_lock_unsafe:{lock_path}")
        with contextlib.suppress(OSError):
            chmod_fd(fd, 0o600)
        try:
            lock_fd(fd, blocking=False)
        except OSError as exc:
            raise ReconcilerLockHeld(f"reconciler_lock_held:{lock_path}") from exc
        locked_path = _lock_path_metadata(lock_path)
        if (
            _directory_identity(lock_path.parent) != parent_identity
            or locked_path is None
            or _lock_metadata_unsafe(locked_path)
            or (locked_path.st_dev, locked_path.st_ino) != identity
        ):
            raise ReconcilerLockUnsafe(f"reconciler_lock_unsafe:{lock_path}")
        try:
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()} {_utcnow()}\n".encode("utf-8"))
        except OSError:
            pass
        yield _process_identity()
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                unlock_fd(fd)
            os.close(fd)
        os.close(parent_fd)
        if repo_locked and repo_fd is not None:
            with contextlib.suppress(OSError):
                unlock_fd(repo_fd)
            os.close(repo_fd)


def status_path(repo: Path | str) -> Path:
    return Path(repo).resolve() / STATUS_REL_PATH


def write_status(repo: Path | str, payload: dict[str, Any]) -> None:
    """Record the scan outcome durably; never let bookkeeping break the loop."""

    target = status_path(repo)
    record = {"schema_id": "aiworkhub.task_reconciler_status.v1", **payload}
    tmp: str | None = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, tmp = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
        )
        try:
            fd_stat = os.fstat(fd)
            path_stat = os.stat(tmp, follow_symlinks=False)
            if (
                not stat.S_ISREG(fd_stat.st_mode)
                or fd_stat.st_nlink != 1
                or (fd_stat.st_dev, fd_stat.st_ino)
                != (path_stat.st_dev, path_stat.st_ino)
            ):
                raise OSError("unsafe reconciler status temporary file")
            with contextlib.suppress(OSError):
                chmod_fd(fd, 0o600)
            os.write(fd, json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        finally:
            os.close(fd)
        os.replace(tmp, target)
        tmp = None
    except OSError:
        # A reconciler that cannot write its own heartbeat must still
        # reconcile; the missing record is itself reported as unknown health.
        return
    finally:
        if tmp is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp)


def read_status(repo: Path | str) -> dict[str, Any]:
    target = status_path(repo)
    try:
        before = _lock_path_metadata(target)
    except ReconcilerLockUnsafe:
        return {}
    if before is None or _lock_metadata_unsafe(before):
        return {}
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(target, flags)
    except OSError:
        return {}
    try:
        metadata = os.fstat(fd)
        after = _lock_path_metadata(target)
        identity = (metadata.st_dev, metadata.st_ino)
        if (
            _lock_metadata_unsafe(metadata)
            or after is None
            or _lock_metadata_unsafe(after)
            or (after.st_dev, after.st_ino) != identity
            or (before.st_dev, before.st_ino) != identity
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            return {}
        with os.fdopen(fd, encoding="utf-8") as stream:
            fd = -1
            record = json.load(stream)
    except (OSError, UnicodeError, json.JSONDecodeError, ReconcilerLockUnsafe):
        return {}
    finally:
        if fd >= 0:
            os.close(fd)
    return record if isinstance(record, dict) else {}


def lock_is_held(lock_path: Path) -> bool:
    """Probe an existing authority lock without creating any path."""

    flags = os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(lock_path, flags)
    except OSError:
        return False
    try:
        try:
            lock_fd(fd, blocking=False)
        except OSError:
            return True
        unlock_fd(fd)
        return False
    finally:
        os.close(fd)


def _staleness(record: dict[str, Any]) -> tuple[bool, float | None]:
    """Return (stale, age_seconds) for a durable scan record.

    A pass that is still running is measured from when it STARTED: it is
    evidence of a live reconciler, and calling it stale the moment it begins
    would report every long sweep as a dead loop.
    """

    at = record.get("scan_finished_epoch")
    if record.get("scan_in_progress") and not isinstance(at, (int, float)):
        at = record.get("scan_started_epoch")
    if not isinstance(at, (int, float)) or isinstance(at, bool):
        return True, None
    age = max(0.0, time.time() - float(at))
    interval = record.get("scan_interval_seconds")
    interval = float(interval) if isinstance(interval, (int, float)) and not isinstance(interval, bool) else DEFAULT_SCAN_INTERVAL_SECONDS
    budget = max(MIN_STALE_SCAN_SECONDS, interval * STALE_SCAN_INTERVALS)
    return age > budget, age


def run_scan(
    manager: process_launcher.ProcessManager | None = None,
    *,
    repo: Path | None = None,
    include_gc: bool = True,
) -> dict[str, Any]:
    """Run one idempotent, bounded reconciliation scan.

    Reuses ProcessManager.reconcile() for both correctness paths: expired
    pid-null reviewer reservations are terminalized and their durable intents
    settled through the canonical task-store API, then exited workers follow
    the existing finalize path. Exact PID/start-tick and spawn-commit evidence
    keeps live or ambiguous reservations untouched. Repeated scans see the
    terminal event and perform no second retirement transition. Never touches
    AITools/taskdb.py directly and never invokes a model/chat endpoint.
    """
    mgr = manager or process_launcher.ProcessManager(repo=repo)
    result = mgr.reconcile(include_gc=include_gc)
    return {
        "ok": True,
        "scanned_at": _utcnow(),
        "gc_included": bool(include_gc),
        **result,
    }


class ReconcilerService:
    """Repo-bound in-process reconciler lifecycle for an MCP child."""

    # Class-level default so the counter exists on any instance, including one
    # built for a test without running __init__.
    _pass_index = 0

    def __init__(self, repo: Path, *, scan_interval_seconds: float | None = None) -> None:
        self.repo = repo.resolve()
        raw_interval = scan_interval_seconds if scan_interval_seconds is not None else _scan_interval_seconds()
        self.scan_interval_seconds = max(
            MIN_SCAN_INTERVAL_SECONDS, min(float(raw_interval), MAX_SCAN_INTERVAL_SECONDS)
        )
        self._manager = process_launcher.ProcessManager(repo=self.repo)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._state_lock = threading.Lock()
        self._last_scan: dict[str, Any] = {}
        self._last_error = ""
        self._pass_index = 0
        self._authority_state = "acquiring"
        self._authority_identity: dict[str, Any] = {}
        self._acquisition_attempts = 0
        self._last_acquisition_error = ""

    def is_running(self) -> bool:
        return bool(self._thread is not None and self._thread.is_alive())

    def _loop(self) -> None:
        lock_path = self.repo / LOCK_REL_PATH
        while not self._stop_event.is_set():
            with self._state_lock:
                self._authority_state = "acquiring"
                self._acquisition_attempts = getattr(self, "_acquisition_attempts", 0) + 1
            try:
                authority = single_instance_lock(lock_path)
                with authority as identity:
                    with self._state_lock:
                        self._authority_state = "active_owner"
                        self._authority_identity = dict(identity)
                        self._last_acquisition_error = ""
                    self._run_as_owner()
            except ReconcilerLockHeld as exc:
                with self._state_lock:
                    self._authority_state = "standby"
                    self._authority_identity = {}
                    self._last_acquisition_error = str(exc)
                self._stop_event.wait(AUTHORITY_RETRY_SECONDS)
            except ReconcilerLockUnsafe as exc:
                with self._state_lock:
                    self._authority_state = "acquisition_failed"
                    self._authority_identity = {}
                    self._last_acquisition_error = str(exc)
                self._stop_event.wait(AUTHORITY_RETRY_SECONDS)
            finally:
                with self._state_lock:
                    if self._authority_state == "active_owner":
                        self._authority_state = "released"
                        self._authority_identity = {}

    def _run_as_owner(
        self,
        *,
        max_iterations: int | None = None,
        on_scan: Any = None,
        stop_requested: Any = None,
    ) -> None:
        iterations = 0
        while not self._stop_event.is_set():
            if stop_requested is not None and stop_requested():
                break
            started = time.time()
            # The first pass sweeps, so a freshly started reconciler still
            # reclaims immediately; after that housekeeping is periodic.
            include_gc = self._pass_index % GC_SCAN_EVERY_N_PASSES == 0
            self._pass_index += 1
            # Announce the pass BEFORE running it. The record is the only way to
            # tell "no reconciler" from "a reconciler mid-pass", and a sweep can
            # run for minutes -- writing only on completion left the loop
            # invisible for exactly as long as it was busiest.
            previous = read_status(self.repo)
            owner = _process_identity()
            write_status(self.repo, {
                "pid": owner["owner_pid"],
                **owner,
                "repo": str(self.repo),
                "authority_state": "active_owner",
                "acquisition_state": "held",
                "scan_started_epoch": started,
                "scan_finished_epoch": None,
                "scan_in_progress": True,
                "scan_interval_seconds": self.scan_interval_seconds,
                "gc_included": include_gc,
                "last_error": "",
                "last_completed_scan": previous.get("last_completed_scan", previous if previous.get("scan_finished_epoch") else {}),
            })
            try:
                result = run_scan(self._manager, repo=self.repo, include_gc=include_gc)
                with self._state_lock:
                    self._last_scan = result
                    self._last_error = ""
                error = ""
            except Exception as exc:  # noqa: BLE001 -- lifecycle safety net
                result = {}
                error = f"{type(exc).__name__}:{exc}"[:500]
                with self._state_lock:
                    self._last_error = error
            # Written on success AND on failure: a loop that is running but
            # failing every scan must not look the same as one that is working.
            completed = {
                "scan_finished_epoch": time.time(),
                "scan_duration_seconds": round(time.time() - started, 3),
                "last_error": error,
                "finalized": result.get("finalized", 0),
                "watched": result.get("watched", 0),
                "gc_included": include_gc,
                "gc_cleaned": result.get("gc_cleaned", 0),
                "scanned_at": result.get("scanned_at", ""),
            }
            write_status(self.repo, {
                "pid": owner["owner_pid"],
                **owner,
                "repo": str(self.repo),
                "authority_state": "active_owner",
                "acquisition_state": "held",
                "scan_started_epoch": started,
                "scan_in_progress": False,
                "scan_interval_seconds": self.scan_interval_seconds,
                **completed,
                "last_completed_scan": completed,
            })
            if on_scan is not None:
                on_scan(result)
            iterations += 1
            if max_iterations is not None and iterations >= max_iterations:
                break
            if self._stop_event.wait(self.scan_interval_seconds):
                break

    def start(self) -> None:
        with self._state_lock:
            if self.is_running():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._loop,
                name=f"aiworkhub-task-reconciler:{self.repo.name}",
                daemon=True,
            )
            thread = self._thread
        thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
        with self._state_lock:
            if self._thread is thread and not thread.is_alive():
                self._thread = None

    def health(self) -> dict[str, Any]:
        with self._state_lock:
            authority_state = getattr(self, "_authority_state", "unknown")
            return {
                "ok": (
                    self.is_running()
                    and not self._last_error
                    and authority_state != "acquisition_failed"
                ),
                "running": self.is_running(),
                "repo": str(self.repo),
                "authority_state": authority_state,
                "active_owner": authority_state == "active_owner",
                "standby": authority_state in {"acquiring", "standby"},
                "authority_identity": dict(getattr(self, "_authority_identity", {})),
                "acquisition_attempts": getattr(self, "_acquisition_attempts", 0),
                "last_acquisition_error": getattr(self, "_last_acquisition_error", ""),
                "scan_interval_seconds": self.scan_interval_seconds,
                "last_scan": dict(self._last_scan),
                "last_error": self._last_error,
            }


_SERVICES: dict[str, ReconcilerService] = {}
_SERVICES_LOCK = threading.Lock()


def ensure_started(repo: Path | str) -> ReconcilerService:
    """Start exactly one reconciliation service per canonical repository."""

    root = Path(repo).resolve()
    key = str(root)
    with _SERVICES_LOCK:
        service = _SERVICES.get(key)
        if service is None:
            service = ReconcilerService(root)
            _SERVICES[key] = service
        service.start()
        return service


def stop_reconciler(repo: Path | str) -> bool:
    key = str(Path(repo).resolve())
    with _SERVICES_LOCK:
        service = _SERVICES.pop(key, None)
    if service is None:
        return False
    service.stop()
    return True


def reconciler_health(repo: Path | str) -> dict[str, Any]:
    """Report reconciler health, in-process first and durably otherwise.

    "This process has no reconciler registered" is not the same claim as "no
    reconciler has run against this repository". A manager chat asking whether
    exited workers are being finalized needs the second answer, so the durable
    record answers when the in-process service is absent -- and a record too old
    to still be evidence is reported stale rather than healthy.
    """

    key = str(Path(repo).resolve())
    with _SERVICES_LOCK:
        service = _SERVICES.get(key)
    record = read_status(key)
    stale, age = _staleness(record) if record else (True, None)
    recorded_error = str(record.get("last_error") or "")
    claims_owner = (
        record.get("authority_state") == "active_owner"
        or record.get("acquisition_state") == "held"
    )
    owner_pid = record.get("owner_pid")
    owner_ticks = record.get("owner_pid_start_ticks")
    owner_identity_live = (
        isinstance(owner_pid, int)
        and not isinstance(owner_pid, bool)
        and (owner_ticks is None or isinstance(owner_ticks, int))
        and lock_is_held(Path(key) / LOCK_REL_PATH)
        and process_launcher._pid_matches(owner_pid, owner_ticks)
    )
    durable_authority_live = not claims_owner or owner_identity_live
    durable = {
        "durable_status_present": bool(record),
        "durable_scan_stale": stale,
        "durable_scan_age_seconds": round(age, 1) if age is not None else None,
        "durable_authority_live": durable_authority_live,
        "durable_last_scan": record,
    }
    if service is None:
        healthy = (
            bool(record)
            and not stale
            and durable_authority_live
            and not recorded_error
        )
        return {
            "ok": healthy,
            "running": False,
            "repo": key,
            "last_scan": record,
            "last_error": (
                ""
                if healthy
                else (
                    recorded_error
                    or (
                        "reconciler_recorded_owner_not_live"
                        if record and not stale and claims_owner
                        else "reconciler_unregistered_and_no_recent_scan"
                    )
                )
            ),
            "startup_error": str(record.get("startup_error") or ""),
            **durable,
        }
    return {**service.health(), **durable}


def _install_stop_handler(
    stop_flag: dict[str, bool], wake_event: threading.Event | None = None
) -> dict[int, Any]:
    def _stop(_signum: int, _frame: Any) -> None:
        stop_flag["stop"] = True
        if wake_event is not None:
            wake_event.set()

    previous = {
        signal.SIGTERM: signal.getsignal(signal.SIGTERM),
        signal.SIGINT: signal.getsignal(signal.SIGINT),
    }
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    return previous


def run_daemon(
    *,
    repo: Path | None = None,
    scan_interval_seconds: float | None = None,
    max_iterations: int | None = None,
    on_scan: Any = None,
) -> int:
    """Continuous bounded reconciliation loop with passive lock failover.

    Builds exactly ONE ``ProcessManager`` for the whole daemon lifetime (not
    one per iteration). A contended daemon remains alive in standby and retries
    the non-blocking repository authority until the owner releases it or this
    process receives a stop signal.
    """
    interval = scan_interval_seconds if scan_interval_seconds is not None else _scan_interval_seconds()
    interval = max(MIN_SCAN_INTERVAL_SECONDS, min(interval, MAX_SCAN_INTERVAL_SECONDS))
    stop_flag = {"stop": False}

    root = Path(repo).resolve() if repo is not None else core.repo_root()
    service = ReconcilerService(root, scan_interval_seconds=interval)
    previous_handlers = _install_stop_handler(stop_flag, service._stop_event)
    emit = on_scan or (
        lambda result: print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
    )
    lock_path = root / LOCK_REL_PATH
    try:
        while not service._stop_event.is_set():
            with service._state_lock:
                service._authority_state = "acquiring"
                service._acquisition_attempts += 1
            try:
                with single_instance_lock(lock_path) as identity:
                    with service._state_lock:
                        service._authority_state = "active_owner"
                        service._authority_identity = dict(identity)
                        service._last_acquisition_error = ""
                    service._run_as_owner(
                        max_iterations=max_iterations,
                        on_scan=emit,
                        stop_requested=lambda: stop_flag["stop"],
                    )
                    return 0
            except ReconcilerLockHeld as exc:
                with service._state_lock:
                    service._authority_state = "standby"
                    service._authority_identity = {}
                    service._last_acquisition_error = str(exc)
                service._stop_event.wait(AUTHORITY_RETRY_SECONDS)
            except ReconcilerLockUnsafe as exc:
                error = str(exc)
                with service._state_lock:
                    service._authority_state = "acquisition_failed"
                    service._authority_identity = {}
                    service._last_acquisition_error = error
                write_status(root, {
                    "authority_state": "acquisition_failed",
                    "acquisition_state": "failed",
                    "acquisition_error": error,
                    "owner_pid": None,
                    "owner_pid_start_ticks": None,
                    "scan_in_progress": False,
                    "scan_started_epoch": None,
                    "scan_finished_epoch": None,
                    "scan_interval_seconds": interval,
                    "last_error": error,
                })
                return 4
        return 0
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="aiworkhub-reconciler",
        description=(
            "Idempotent durable reconciler for AIWorkHub MCP isolated "
            "workers -- token-free, filesystem/process/SQLite lifecycle "
            "checks only; never calls Claude, Codex, DeepSeek, or any chat "
            "endpoint."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_once = sub.add_parser("run-once", help="Run exactly one bounded reconciliation scan")
    run_once.add_argument("--repo", default=None)

    daemon = sub.add_parser("daemon", help="Run continuous bounded reconciliation scans")
    daemon.add_argument("--repo", default=None)
    daemon.add_argument("--scan-interval-seconds", type=float, default=None)
    daemon.add_argument("--max-iterations", type=int, default=None)

    status = sub.add_parser("status", help="Report single-instance lock presence (read-only)")
    status.add_argument("--repo", default=None)

    args = parser.parse_args(argv)
    repo = Path(args.repo).expanduser().resolve() if args.repo else core.repo_root()
    lock_path = repo / LOCK_REL_PATH

    if args.command == "status":
        lock_present = lock_path.is_file()
        lock_held = lock_is_held(lock_path) if lock_present else False
        record = read_status(repo) if lock_held else {}
        print(json.dumps({
            "ok": True,
            "lock_path": str(lock_path),
            "lock_present": lock_present,
            "lock_held": lock_held,
            "owner_pid": record.get("owner_pid"),
            "owner_pid_start_ticks": record.get("owner_pid_start_ticks"),
        }, sort_keys=True))
        return 0

    try:
        if args.command == "run-once":
            with single_instance_lock(lock_path):
                result = run_scan(repo=repo)
                print(json.dumps(result, ensure_ascii=False, sort_keys=True))
                return 0
        return run_daemon(
            repo=repo,
            scan_interval_seconds=args.scan_interval_seconds,
            max_iterations=args.max_iterations,
        )
    except ReconcilerLockHeld as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
