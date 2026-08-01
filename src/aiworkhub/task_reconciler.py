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


def run_scan(
    manager: process_launcher.ProcessManager | None = None,
    *,
    repo: Path | None = None,
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
    result = mgr.reconcile()
    return {
        "ok": True,
        "scanned_at": _utcnow(),
        **result,
    }


class ReconcilerService:
    """Repo-bound in-process reconciler lifecycle for an MCP child."""

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

    def is_running(self) -> bool:
        return bool(self._thread is not None and self._thread.is_alive())

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                result = run_scan(self._manager, repo=self.repo)
                with self._state_lock:
                    self._last_scan = result
                    self._last_error = ""
            except Exception as exc:  # noqa: BLE001 -- lifecycle safety net
                with self._state_lock:
                    self._last_error = f"{type(exc).__name__}:{exc}"[:500]
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
    key = str(Path(repo).resolve())
    with _SERVICES_LOCK:
        service = _SERVICES.get(key)
    if service is None:
        return {
            "ok": False, "running": False, "repo": key,
            "last_scan": {}, "last_error": "reconciler_unregistered",
        }
    return service.health()


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
