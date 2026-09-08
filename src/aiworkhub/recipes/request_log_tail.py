#!/usr/bin/env python3
"""Bounded tail of one launched request's captured output, plus its supervisor.

Replaces the ``tail``/``sed``/heredoc a manager types to see what a worker
actually printed. 589 of 4,962 measured Bash calls targeted ``.aiworkhub``
runtime logs by hand.

The layout is verified against this repository's own runtime directory:
``.aiworkhub/runtime/process_logs/processes/<request_id>.<stream>.log`` beside
``<request_id>.supervisor.json`` and ``<request_id>.request.json``.

Read-only.

    python -m aiworkhub.recipes.request_log_tail --request-id ID
        [--stream stdout|stderr] [--bytes N]

The tail is the LAST ``--bytes`` bytes, because a failure's diagnosis is at the
end. The supervisor summary is included in full because it is small and fixed:
it carries the exit code, the timeout the run was given and whether it was
enforced, the byte budget, and the pids -- which is usually the answer without
reading the log at all.
"""

from __future__ import annotations

import argparse
import sys

from . import _common as common

SCHEMA_ID = "aiworkhub.recipe.request_log_tail.v1"

# The fixed, small subset of ``<id>.supervisor.json`` that answers "what
# happened to this run". The whole file also carries a per-sample token budget
# event list that grows with the run and is not a summary.
_SUPERVISOR_FIELDS = (
    "state",
    "exit_code",
    "error",
    "child_pid",
    "supervisor_pid",
    "started_at_epoch",
    "finished_at_epoch",
    "heartbeat_at_epoch",
    "heartbeat_seq",
    "timeout_seconds",
    "timeout_enforced",
    "deadline_epoch",
    "last_meaningful_phase",
    "stdout_bytes",
    "stderr_bytes",
    "stdout_dropped_bytes",
    "stderr_dropped_bytes",
    "capture_errors",
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--request-id", required=True, help="32-hex launch request id")
    parser.add_argument("--stream", default="stdout", choices=("stdout", "stderr"))
    parser.add_argument(
        "--bytes", type=int, default=8192, help="tail size in bytes (default: 8192)"
    )
    args = parser.parse_args(argv)

    request_id = str(args.request_id).strip().lower()
    if not common.is_request_id(request_id):
        return common.fail(
            SCHEMA_ID, "invalid_request_id", "request id must be 32 hex characters"
        )
    tail_bytes = max(256, min(int(args.bytes), 131_072))

    log_path = common.PROCESS_LOGS_DIR / f"{request_id}.{args.stream}.log"
    supervisor_path = common.PROCESS_LOGS_DIR / f"{request_id}.supervisor.json"
    if not log_path.exists() and not supervisor_path.exists():
        return common.fail(
            SCHEMA_ID,
            "request_not_found",
            f"no process log or supervisor file for {request_id}",
        )

    tail = ""
    log_bytes = 0
    truncated = False
    read_error = ""
    if log_path.exists():
        try:
            log_bytes = log_path.stat().st_size
            with log_path.open("rb") as handle:
                if log_bytes > tail_bytes:
                    handle.seek(log_bytes - tail_bytes)
                    truncated = True
                tail = handle.read().decode("utf-8", errors="replace")
        except OSError as exc:
            read_error = f"{type(exc).__name__}: {exc}"

    supervisor_raw = common.load_json(supervisor_path)
    supervisor = None
    if isinstance(supervisor_raw, dict):
        supervisor = {k: supervisor_raw.get(k) for k in _SUPERVISOR_FIELDS}
        budget = supervisor_raw.get("output_budget")
        if isinstance(budget, dict):
            supervisor["output_budget"] = {
                k: budget.get(k)
                for k in (
                    "cap_bytes",
                    "observed_bytes",
                    "stdout_received_bytes",
                    "stderr_received_bytes",
                )
            }
        token_budget = supervisor_raw.get("token_budget")
        if isinstance(token_budget, dict):
            supervisor["token_budget"] = {
                k: token_budget.get(k)
                for k in (
                    "accepted_total_tokens",
                    "cap_tokens",
                    "cap_enforceable",
                    "enforcing",
                    "cost_usd",
                )
            }

    request_raw = common.load_json(common.PROCESS_LOGS_DIR / f"{request_id}.request.json")
    request = None
    if isinstance(request_raw, dict):
        request = {
            k: request_raw.get(k)
            for k in ("adapter_id", "model", "task_id", "runner", "role", "topic")
        }

    return common.emit(
        {
            "schema_id": SCHEMA_ID,
            "request_id": request_id,
            "stream": args.stream,
            "log_path": str(log_path),
            "log_present": log_path.exists(),
            "log_bytes": log_bytes,
            "tail_bytes": tail_bytes,
            "tail_truncated": truncated,
            "read_error": read_error,
            "tail": tail,
            "supervisor": supervisor,
            "request": request,
        }
    )


if __name__ == "__main__":
    sys.exit(main())
