"""Host-side wake signal for a direct-chat Claude manager (NF-2026-01892).

Run as a background command; it exits as soon as a callback is claimable for
the given route, which re-invokes the model.  It only peeks -- it never claims,
leases or acknowledges, so ``aiworkhub_claude_callback_wait`` still returns the
same batch afterwards.  Each poll reads through a short-lived read-only
connection and never takes the task_queue write lease, so other writers are
never blocked by a long-running watch.

While it runs it keeps a heartbeat, ``{pid, updated_at, poll_interval}``, at
``tool_guard_hook.manager_watch_heartbeat_path`` -- rewritten by atomic replace
at start and on every poll, removed on every exit -- so the manager's Stop
hook can tell a live watcher from none (NF-2026-02110). The heartbeat is the
watcher's only write and is best-effort: failing to write it never ends the
watch.

Exit codes: 0 callback pending, 3 timeout, 2 store error.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from . import callback_store
from .sqlite_readonly import connect_readonly
from .tool_guard_hook import manager_watch_heartbeat_path

EXIT_PENDING = 0
EXIT_ERROR = 2
EXIT_TIMEOUT = 3


def _emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    sys.stdout.flush()


def _write_heartbeat(path: Path | None, poll_interval: float) -> None:
    """Replace the heartbeat in one step; a reader never sees half a file."""

    if path is None:
        return
    record = {
        "pid": os.getpid(),
        "poll_interval": poll_interval,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    scratch = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        scratch.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
        os.replace(scratch, path)
    except OSError:
        try:
            scratch.unlink()
        except OSError:
            pass


def _remove_heartbeat(path: Path | None) -> None:
    """Remove the heartbeat if this process still owns it."""

    if path is None:
        return
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if isinstance(record, dict) and record.get("pid") == os.getpid():
        try:
            path.unlink()
        except OSError:
            pass


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aiworkhub.manager_callback_watch")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--origin-thread-id", required=True)
    parser.add_argument("--timeout", type=float, default=3600.0)
    parser.add_argument("--poll-interval", type=float, default=2.0)
    args = parser.parse_args(argv)
    poll_interval = max(0.05, args.poll_interval)
    deadline = time.monotonic() + max(0.0, args.timeout)
    try:
        heartbeat = manager_watch_heartbeat_path(Path(args.repo).resolve(), args.origin_thread_id)
    except (OSError, RuntimeError, ValueError):
        heartbeat = None
    try:
        db_path = callback_store.resolve_db_path(args.repo)
        while True:
            _write_heartbeat(heartbeat, poll_interval)
            conn = connect_readonly(db_path)
            try:
                conn.row_factory = sqlite3.Row
                batch = callback_store.peek_claimable_callback(
                    conn,
                    provider=args.provider,
                    origin_thread_id=args.origin_thread_id,
                )
            finally:
                conn.close()
            if batch is not None:
                _emit({"status": "callback_pending", **batch})
                return EXIT_PENDING
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _emit({"status": "timeout"})
                return EXIT_TIMEOUT
            time.sleep(min(poll_interval, remaining))
    except (callback_store.CallbackStoreError, sqlite3.Error, OSError, ValueError) as exc:
        _emit({"status": "error", "reason": f"{type(exc).__name__}: {exc}"})
        return EXIT_ERROR
    finally:
        _remove_heartbeat(heartbeat)


if __name__ == "__main__":
    raise SystemExit(main())
