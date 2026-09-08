#!/usr/bin/env python3
"""Measured liveness for launched requests, plus the reconciler's heartbeat age.

Replaces the heredoc a manager types when a card says ``processing`` and
nothing seems to be happening.

Two things this repository already learned the hard way are structural here:

* **A status string is not a process.** ``state: "running"`` in a supervisor
  file is a claim; ``child_alive`` below is a measurement taken by asking the
  OS about the pid. Both are reported, and when they disagree the disagreement
  is the answer.
* **The reconciler's own liveness is the second question.** The only thing that
  finalizes an exited worker keeps its health in
  ``.aiworkhub/runtime/task_reconciler_status.json``; a stale heartbeat there
  explains a queue full of finished work that nobody closed. Its age is
  reported whether or not anything else looks wrong.

Read-only.

    python -m aiworkhub.recipes.process_liveness [--request-id ID] [--scan N]
        [--include-dead]

With ``--request-id`` it answers about that one request. Without, it scans the
newest ``--scan`` supervisor files and returns the ones whose pids are measured
ALIVE, plus a count of everything scanned grouped by its claimed state.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import _common as common

SCHEMA_ID = "aiworkhub.recipe.process_liveness.v1"


def _entry(path: Path) -> dict | None:
    payload = common.load_json(path)
    if not isinstance(payload, dict):
        return None
    child = payload.get("child_pid")
    supervisor = payload.get("supervisor_pid")
    return {
        "request_id": path.name.split(".")[0],
        "claimed_state": payload.get("state"),
        "exit_code": payload.get("exit_code"),
        "error": str(payload.get("error") or "")[:200],
        "child_pid": child,
        "child_alive": common.pid_is_alive(child),
        "supervisor_pid": supervisor,
        "supervisor_alive": common.pid_is_alive(supervisor),
        "started_at_epoch": payload.get("started_at_epoch"),
        "finished_at_epoch": payload.get("finished_at_epoch"),
        "heartbeat_at_epoch": payload.get("heartbeat_at_epoch"),
        "timeout_seconds": payload.get("timeout_seconds"),
        "timeout_enforced": payload.get("timeout_enforced"),
        "status_mtime_epoch": path.stat().st_mtime,
    }


def _reconciler(now: float) -> dict:
    payload = common.load_json(common.RECONCILER_STATUS)
    if not isinstance(payload, dict):
        return {
            "present": False,
            "path": str(common.RECONCILER_STATUS),
            "detail": "status file absent or unreadable",
        }
    finished = payload.get("scan_finished_epoch")
    age = (
        round(now - float(finished), 3)
        if isinstance(finished, (int, float)) and not isinstance(finished, bool)
        else None
    )
    pid = payload.get("pid")
    return {
        "present": True,
        "path": str(common.RECONCILER_STATUS),
        "pid": pid,
        "pid_alive": common.pid_is_alive(pid),
        "acquisition_state": payload.get("acquisition_state"),
        "authority_state": payload.get("authority_state"),
        "scan_in_progress": payload.get("scan_in_progress"),
        "scan_interval_seconds": payload.get("scan_interval_seconds"),
        "scanned_at": payload.get("scanned_at"),
        "heartbeat_age_seconds": age,
        "last_error": str(payload.get("last_error") or "")[:200],
        "watched": payload.get("watched"),
        "finalized": payload.get("finalized"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--request-id",
        default=common.NO_FILTER,
        help="one 32-hex request id, or 'all' to scan (default: all)",
    )
    parser.add_argument(
        "--scan",
        type=int,
        default=200,
        help="how many newest supervisor files to scan (default: 200)",
    )
    parser.add_argument(
        "--include-dead",
        action="store_true",
        help="return every scanned entry, not only the measurably alive ones",
    )
    args = parser.parse_args(argv)

    now = time.time()
    if not common.PROCESS_LOGS_DIR.is_dir():
        return common.fail(
            SCHEMA_ID,
            "process_logs_unavailable",
            f"no process log directory at {common.PROCESS_LOGS_DIR}",
        )

    if args.request_id != common.NO_FILTER:
        request_id = str(args.request_id).strip().lower()
        if not common.is_request_id(request_id):
            return common.fail(
                SCHEMA_ID, "invalid_request_id", "request id must be 32 hex characters"
            )
        path = common.PROCESS_LOGS_DIR / f"{request_id}.supervisor.json"
        entry = _entry(path) if path.is_file() else None
        if entry is None:
            return common.fail(
                SCHEMA_ID, "request_not_found", f"no supervisor file at {path}"
            )
        return common.emit(
            {
                "schema_id": SCHEMA_ID,
                "request_id": request_id,
                "scanned_count": 1,
                "alive_count": int(bool(entry["child_alive"] or entry["supervisor_alive"])),
                "states": {str(entry["claimed_state"]): 1},
                "processes": [entry],
                "reconciler": _reconciler(now),
            }
        )

    scan = max(1, min(int(args.scan), 2000))
    try:
        candidates = sorted(
            common.PROCESS_LOGS_DIR.glob("*.supervisor.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:scan]
    except OSError as exc:
        return common.fail(SCHEMA_ID, "scan_failed", str(exc))

    states: dict[str, int] = {}
    processes = []
    alive = 0
    for path in candidates:
        entry = _entry(path)
        if entry is None:
            continue
        key = str(entry["claimed_state"])
        states[key] = states.get(key, 0) + 1
        is_alive = bool(entry["child_alive"] or entry["supervisor_alive"])
        if is_alive:
            alive += 1
        if is_alive or args.include_dead:
            processes.append(entry)

    processes.sort(key=lambda e: e["status_mtime_epoch"], reverse=True)
    return common.emit(
        {
            "schema_id": SCHEMA_ID,
            "request_id": common.NO_FILTER,
            "scan_limit": scan,
            "scanned_count": len(candidates),
            "alive_count": alive,
            "states": dict(sorted(states.items())),
            "returned_count": len(processes[:100]),
            "processes": processes[:100],
            "reconciler": _reconciler(now),
        }
    )


if __name__ == "__main__":
    sys.exit(main())
