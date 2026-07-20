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
import fcntl
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import core
from . import process_launcher


DEFAULT_SCAN_INTERVAL_SECONDS = 30.0
MIN_SCAN_INTERVAL_SECONDS = 5.0
MAX_SCAN_INTERVAL_SECONDS = 3600.0
SCAN_INTERVAL_ENV = "GEOAI_TASK_MCP_RECONCILER_SCAN_INTERVAL_SECONDS"
LOCK_REL_PATH = Path("tools/geoai-task-mcp/logs/task_reconciler.lock")


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
    os.fchmod(fd, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
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
            fcntl.flock(fd, fcntl.LOCK_UN)
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
        prog="geoai-task-reconciler",
        description=(
            "Idempotent durable reconciler for GeoAI Task MCP isolated "
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
