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
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import core
from . import process_launcher
from .platform_io import chmod_fd, lock_fd, unlock_fd


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


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _scan_interval_seconds() -> float:
    try:
        value = float(os.environ.get(SCAN_INTERVAL_ENV, str(DEFAULT_SCAN_INTERVAL_SECONDS)))
    except (TypeError, ValueError):
        value = DEFAULT_SCAN_INTERVAL_SECONDS
    return max(MIN_SCAN_INTERVAL_SECONDS, min(value, MAX_SCAN_INTERVAL_SECONDS))


class ReconcilerLockHeld(RuntimeError):
    """Another reconciler instance already holds the single-instance lock."""


@contextlib.contextmanager
def single_instance_lock(lock_path: Path):
    """Bounded, non-blocking advisory single-instance lock.

    Deliberately separate from ``ProcessManager._registry_lock`` (which
    already makes every individual finalize/claim operation interprocess-
    safe): this lock only prevents redundant reconciler DAEMONS from running
    concurrently against the same repo, it is not itself the correctness
    boundary for finalization.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(lock_path, flags, 0o600)
    chmod_fd(fd, 0o600)
    try:
        try:
            lock_fd(fd, blocking=False)
        except OSError as exc:
            raise ReconcilerLockHeld(f"reconciler_lock_held:{lock_path}") from exc
        try:
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()} {_utcnow()}\n".encode("utf-8"))
        except OSError:
            pass
        yield
    finally:
        with contextlib.suppress(OSError):
            unlock_fd(fd)
        os.close(fd)


def status_path(repo: Path | str) -> Path:
    return Path(repo).resolve() / STATUS_REL_PATH


def write_status(repo: Path | str, payload: dict[str, Any]) -> None:
    """Record the scan outcome durably; never let bookkeeping break the loop."""

    target = status_path(repo)
    record = {"schema_id": "aiworkhub.task_reconciler_status.v1", **payload}
    try:
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(record, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )
        os.replace(tmp, target)
    except OSError:
        # A reconciler that cannot write its own heartbeat must still
        # reconcile; the missing record is itself reported as unknown health.
        return


def read_status(repo: Path | str) -> dict[str, Any]:
    try:
        raw = status_path(repo).read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        record = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return record if isinstance(record, dict) else {}


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

    Reuses ``ProcessManager.reconcile()`` (== ``_reconcile_persisted_requests``)
    unchanged -- the SAME finalize path the isolated launcher already runs on
    its own startup/status/cancel calls. A request whose supervisor is still
    actively heartbeating is left untouched; a request whose supervisor
    already produced a terminal status (or whose exact PID+start-tick
    identity no longer exists) is finalized exactly once through the
    existing scope/validate/promote/review-or-release path. Never touches
    ``AITools/taskdb.py`` directly and never invokes any Claude/Codex/
    DeepSeek/chat endpoint.
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

    def is_running(self) -> bool:
        return bool(self._thread is not None and self._thread.is_alive())

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            started = time.time()
            # The first pass sweeps, so a freshly started reconciler still
            # reclaims immediately; after that housekeeping is periodic.
            include_gc = self._pass_index % GC_SCAN_EVERY_N_PASSES == 0
            self._pass_index += 1
            # Announce the pass BEFORE running it. The record is the only way to
            # tell "no reconciler" from "a reconciler mid-pass", and a sweep can
            # run for minutes -- writing only on completion left the loop
            # invisible for exactly as long as it was busiest.
            write_status(self.repo, {
                "pid": os.getpid(),
                "repo": str(self.repo),
                "scan_started_epoch": started,
                "scan_finished_epoch": None,
                "scan_in_progress": True,
                "scan_interval_seconds": self.scan_interval_seconds,
                "gc_included": include_gc,
                "last_error": "",
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
            write_status(self.repo, {
                "pid": os.getpid(),
                "repo": str(self.repo),
                "scan_started_epoch": started,
                "scan_finished_epoch": time.time(),
                "scan_in_progress": False,
                "scan_duration_seconds": round(time.time() - started, 3),
                "scan_interval_seconds": self.scan_interval_seconds,
                "last_error": error,
                "finalized": result.get("finalized", 0),
                "watched": result.get("watched", 0),
                "gc_included": bool(result.get("gc_included")),
                "gc_cleaned": result.get("gc_cleaned", 0),
                "scanned_at": result.get("scanned_at", ""),
            })
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
            return {
                "ok": self.is_running() and not self._last_error,
                "running": self.is_running(),
                "repo": str(self.repo),
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
    durable = {
        "durable_status_present": bool(record),
        "durable_scan_stale": stale,
        "durable_scan_age_seconds": round(age, 1) if age is not None else None,
        "durable_last_scan": record,
    }
    if service is None:
        return {
            "ok": bool(record) and not stale,
            "running": False,
            "repo": key,
            "last_scan": record,
            "last_error": (
                "" if record and not stale
                else "reconciler_unregistered_and_no_recent_scan"
            ),
            "startup_error": str(record.get("startup_error") or ""),
            **durable,
        }
    return {**service.health(), **durable}


def _install_stop_handler(stop_flag: dict[str, bool]) -> None:
    def _stop(_signum: int, _frame: Any) -> None:
        stop_flag["stop"] = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)


def run_daemon(
    *,
    repo: Path | None = None,
    scan_interval_seconds: float | None = None,
    max_iterations: int | None = None,
    on_scan: Any = None,
) -> int:
    """Continuous bounded reconciliation loop.

    Builds exactly ONE ``ProcessManager`` for the whole daemon lifetime (not
    one per iteration) so a request already being watched by that manager's
    own background watcher thread is never double-watched across
    iterations; ``run_scan``/``reconcile()`` remain idempotent regardless.
    """
    interval = scan_interval_seconds if scan_interval_seconds is not None else _scan_interval_seconds()
    interval = max(MIN_SCAN_INTERVAL_SECONDS, min(interval, MAX_SCAN_INTERVAL_SECONDS))
    manager = process_launcher.ProcessManager(repo=repo)
    stop_flag = {"stop": False}
    _install_stop_handler(stop_flag)

    iterations = 0
    while not stop_flag["stop"]:
        result = run_scan(manager, repo=repo)
        if on_scan is not None:
            on_scan(result)
        else:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
        iterations += 1
        if max_iterations is not None and iterations >= max_iterations:
            break
        if stop_flag["stop"]:
            break
        time.sleep(interval)
    return 0


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
        print(json.dumps({
            "ok": True,
            "lock_path": str(lock_path),
            "lock_present": lock_path.is_file(),
        }, sort_keys=True))
        return 0

    try:
        with single_instance_lock(lock_path):
            if args.command == "run-once":
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
