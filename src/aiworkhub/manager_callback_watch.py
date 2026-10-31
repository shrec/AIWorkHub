"""Host-side wake signal for a direct-chat Claude manager (NF-2026-01892).

Run as a background command; it exits as soon as a callback is claimable for
the given route, which re-invokes the model.  It only peeks -- it never claims,
leases or acknowledges, so ``aiworkhub_claude_callback_wait`` still returns the
same batch afterwards.

Exit codes: 0 callback pending, 3 timeout, 2 store error.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from typing import Sequence

from . import callback_store

EXIT_PENDING = 0
EXIT_ERROR = 2
EXIT_TIMEOUT = 3


def _emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    sys.stdout.flush()


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
    conn: sqlite3.Connection | None = None
    try:
        conn = callback_store.open_db(callback_store.resolve_db_path(args.repo))
        while True:
            batch = callback_store.peek_claimable_callback(
                conn,
                provider=args.provider,
                origin_thread_id=args.origin_thread_id,
            )
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
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
