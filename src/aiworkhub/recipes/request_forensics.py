#!/usr/bin/env python3
"""What happened to one launched request, and the recurring rows across requests.

Replaces the inline scripts, shell log reads, slices, worktree inspections and
direct SQLite reads a manager spent answering the same few questions about a
worker attempt. For one request it folds the owner recipes that already read
each fact -- the supervisor summary and stderr tail, the liveness measurement,
the validation receipts and the changed paths -- into one object. Across
requests it returns bounded rows for the recurring questions: which requests
are in one state, which failed one validation class, which started in a window.

Nothing here restates a reader. Each section is the owner recipe's own ``main``
run in-process with its stdout captured, so a fix to an owner reaches this
recipe unchanged. A section whose owner refuses is reported as unavailable with
the owner's reason, and absent evidence is never a pass.

``last_heartbeat_epoch`` is the supervisor's own ``heartbeat_at_epoch``: the
supervisor records no separate progress timestamp, so this is the closest
measured signal of the last activity.

Repository rows read the newest ``SCAN_WINDOW`` supervisor files through
process_liveness, which shows at most 100 of them. ``window_incomplete`` is true
when some scanned requests are not visible, so an older match can be missing.

Read-only. Stdlib only.

    python -m aiworkhub.recipes.request_forensics --request-id ID
    python -m aiworkhub.recipes.request_forensics [--state S]
        [--failure-class C] [--since 24h] [--limit N]

``all`` is the value of every filter that is not applied, as in the other
recipes. Exit 0 when the question was answered (including "no rows"); exit 2
when it could not be: an unknown or malformed request id, a conflicting or
malformed argument, or an owner that is unavailable.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import re
import time
from typing import Any, Callable

from . import _common as common
from . import attempt_validation, process_liveness, request_log_tail, worktree_diff

SCHEMA_ID = "aiworkhub.recipe.request_forensics.v1"

DEFAULT_LIMIT = 20
MAX_LIMIT = 200
# The newest supervisor files process_liveness reads: its own default.
SCAN_WINDOW = 200
MAX_CHECKS = 50
MAX_PATHS = 200
STDERR_BYTES = 4096
# The changed set is what is wanted, not diff text: the smallest budget
# worktree_diff accepts keeps its reply to the path list.
PATHS_ONLY_DIFF_BYTES = 1024
TAIL_CHARS = 400
MAX_TEXT_CHARS = 200
MAX_SINCE_SECONDS = 365 * 86_400
_SINCE = re.compile(r"([1-9][0-9]{0,4})([smhd])")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3_600, "d": 86_400}


def _ask(owner: Callable[[list[str]], int], argv: list[str]) -> tuple[int, dict[str, Any]]:
    """Run an owner recipe's own ``main`` and return its exit code and object.

    The owners answer by printing one JSON object through ``common.emit``.
    Running them in-process with stdout captured is the reuse: no reader is
    restated here. A reply that is not a JSON object is an unreadable answer,
    never a success.
    """
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = owner(argv)
    try:
        payload = json.loads(buffer.getvalue())
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        return 2, {
            "ok": False,
            "reason": "owner_output_unreadable",
            "detail": owner.__module__,
        }
    return code, payload


def _reason(payload: dict[str, Any]) -> tuple[str, str]:
    reason = str(payload.get("reason") or "owner_refused")
    detail = str(payload.get("detail") or "")[:MAX_TEXT_CHARS]
    return reason, detail


def _is_epoch(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _validation(request_id: str) -> dict[str, Any]:
    code, owner = _ask(
        attempt_validation.main,
        ["--request-id", request_id, "--tail-chars", str(TAIL_CHARS)],
    )
    if code != 0:
        reason, _ = _reason(owner)
        return {
            "available": False,
            "reason": reason,
            "passed": None,
            "failure_classes": None,
            "checks": [],
        }
    checks = [c for c in owner.get("checks") or [] if isinstance(c, dict)]
    count = int(owner.get("check_count") or 0)
    # A run with no checks has no receipts: absent evidence, not a pass.
    no_evidence = count == 0
    return {
        "available": True,
        "check_count": count,
        "failed_count": int(owner.get("failed_count") or 0),
        "passed": None if no_evidence else bool(owner.get("passed")),
        "failure_classes": None
        if no_evidence
        else list(owner.get("failure_classes") or []),
        "checks": [
            {
                "command": c.get("command"),
                "returncode": c.get("returncode"),
                "failure_class": c.get("failure_class"),
            }
            for c in checks[:MAX_CHECKS]
        ],
        "checks_truncated": len(checks) > MAX_CHECKS,
    }


def _changed_paths(request_id: str) -> dict[str, Any]:
    code, owner = _ask(
        worktree_diff.main,
        [
            "--request-id",
            request_id,
            "--path",
            common.NO_FILTER,
            "--max-bytes",
            str(PATHS_ONLY_DIFF_BYTES),
        ],
    )
    if code != 0:
        reason, _ = _reason(owner)
        return {"available": False, "reason": reason, "count": None, "paths": []}
    files = [f for f in owner.get("files") or [] if isinstance(f, dict)]
    paths = [str(f.get("path")) for f in files]
    return {
        "available": True,
        "count": owner.get("changed_path_count"),
        "paths": paths[:MAX_PATHS],
        "paths_truncated": len(paths) > MAX_PATHS,
    }


def _one_request(request_id: str) -> int:
    code, tail = _ask(
        request_log_tail.main,
        ["--request-id", request_id, "--stream", "stderr", "--bytes", str(STDERR_BYTES)],
    )
    if code != 0:
        reason, detail = _reason(tail)
        return common.fail(SCHEMA_ID, reason, detail)
    supervisor = tail.get("supervisor") if isinstance(tail.get("supervisor"), dict) else {}

    code, live = _ask(process_liveness.main, ["--request-id", request_id])
    processes = live.get("processes") if code == 0 else None
    proc = processes[0] if isinstance(processes, list) and processes else {}

    validation = _validation(request_id)
    return common.emit(
        {
            "schema_id": SCHEMA_ID,
            "mode": "request",
            "request_id": request_id,
            "state": supervisor.get("state"),
            "exit_code": supervisor.get("exit_code"),
            "error": str(supervisor.get("error") or "")[:MAX_TEXT_CHARS],
            "phase": supervisor.get("last_meaningful_phase"),
            "started_at_epoch": supervisor.get("started_at_epoch"),
            "finished_at_epoch": supervisor.get("finished_at_epoch"),
            "last_heartbeat_epoch": supervisor.get("heartbeat_at_epoch"),
            "timeout_seconds": supervisor.get("timeout_seconds"),
            "timeout_enforced": supervisor.get("timeout_enforced"),
            "child_alive": proc.get("child_alive"),
            "supervisor_alive": proc.get("supervisor_alive"),
            "liveness": {
                "available": code == 0,
                "reason": None if code == 0 else _reason(live)[0],
            },
            "failure_classes": validation["failure_classes"],
            "validation": validation,
            "changed_paths": _changed_paths(request_id),
            "stderr_tail": {
                "present": tail.get("log_present"),
                "bytes_budget": STDERR_BYTES,
                "truncated": tail.get("tail_truncated"),
                "read_error": tail.get("read_error"),
                "text": tail.get("tail"),
            },
        }
    )


def _scan(
    filters: dict[str, Any],
    state: str | None,
    failure_class: str | None,
    since_seconds: float | None,
    limit: int,
) -> int:
    code, listing = _ask(
        process_liveness.main,
        ["--request-id", common.NO_FILTER, "--scan", str(SCAN_WINDOW), "--include-dead"],
    )
    if code != 0:
        reason, detail = _reason(listing)
        return common.fail(SCHEMA_ID, reason, detail)

    floor = None if since_seconds is None else time.time() - since_seconds
    rows: list[dict[str, Any]] = []
    more = False
    for proc in listing.get("processes") or []:
        if not isinstance(proc, dict):
            continue
        if state is not None and proc.get("claimed_state") != state:
            continue
        started = proc.get("started_at_epoch")
        if floor is not None and (not _is_epoch(started) or started < floor):
            continue
        failure_classes = None
        if failure_class is not None:
            failure_classes = _validation(str(proc.get("request_id")))["failure_classes"]
            if failure_classes is None or failure_class not in failure_classes:
                continue
        if len(rows) == limit:
            more = True
            break
        rows.append(
            {
                "request_id": proc.get("request_id"),
                "claimed_state": proc.get("claimed_state"),
                "exit_code": proc.get("exit_code"),
                "started_at_epoch": started,
                "last_heartbeat_epoch": proc.get("heartbeat_at_epoch"),
                "child_alive": proc.get("child_alive"),
                "supervisor_alive": proc.get("supervisor_alive"),
                "failure_classes": failure_classes,
            }
        )

    scanned = int(listing.get("scanned_count") or 0)
    visible = int(listing.get("returned_count") or 0)
    return common.emit(
        {
            "schema_id": SCHEMA_ID,
            "mode": "repository",
            "filters": filters,
            "limit": limit,
            "scan_window": SCAN_WINDOW,
            "scanned_count": scanned,
            "rows_visible_count": visible,
            "window_incomplete": visible < scanned,
            "states": listing.get("states"),
            "returned_count": len(rows),
            "truncated": more,
            "rows": rows,
        }
    )


def _since_seconds(text: str) -> float | None:
    if text == common.NO_FILTER:
        return None
    match = _SINCE.fullmatch(text)
    if match is None:
        raise ValueError(text)
    seconds = int(match.group(1)) * _UNIT_SECONDS[match.group(2)]
    if seconds > MAX_SINCE_SECONDS:
        raise ValueError(text)
    return float(seconds)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--request-id",
        default=common.NO_FILTER,
        help="one 32-hex request id, or 'all' for repository rows (default: all)",
    )
    parser.add_argument(
        "--state",
        default=common.NO_FILTER,
        help="exact claimed state of a supervisor file, or 'all' (default: all)",
    )
    parser.add_argument(
        "--failure-class",
        default=common.NO_FILTER,
        help="exact validation failure class, or 'all' (default: all)",
    )
    parser.add_argument(
        "--since",
        default=common.NO_FILTER,
        help="started within <N>s, <N>m, <N>h or <N>d (up to 365d), or 'all'",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"max repository rows, 1..{MAX_LIMIT} (default: {DEFAULT_LIMIT})",
    )
    args = parser.parse_args(argv)

    if args.request_id != common.NO_FILTER:
        if any(
            value != common.NO_FILTER
            for value in (args.state, args.failure_class, args.since)
        ):
            return common.fail(
                SCHEMA_ID,
                "conflicting_arguments",
                "--request-id answers one request; drop --state, "
                "--failure-class and --since",
            )
        request_id = str(args.request_id).strip().lower()
        if not common.is_request_id(request_id):
            return common.fail(
                SCHEMA_ID, "invalid_request_id", "request id must be 32 hex characters"
            )
        return _one_request(request_id)

    try:
        since_seconds = _since_seconds(args.since)
    except ValueError:
        return common.fail(
            SCHEMA_ID,
            "invalid_since",
            "use <N>s, <N>m, <N>h or <N>d up to 365d, or 'all'",
        )
    state = None if args.state == common.NO_FILTER else str(args.state)
    failure_class = (
        None if args.failure_class == common.NO_FILTER else str(args.failure_class)
    )
    filters = {
        "state": state,
        "failure_class": failure_class,
        "since": args.since,
        "since_seconds": since_seconds,
    }
    limit = max(1, min(int(args.limit), MAX_LIMIT))
    return _scan(filters, state, failure_class, since_seconds, limit)


if __name__ == "__main__":
    raise SystemExit(main())
