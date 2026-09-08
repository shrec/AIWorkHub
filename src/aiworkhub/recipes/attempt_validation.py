#!/usr/bin/env python3
"""One request's validation result, folded to what a reviewer actually reads.

Replaces the heredoc a manager types to answer "why did this card fail
validation": open ``attempt-artifacts/<id>/validation.json`` and dig the
failing check out of it. That file is large -- 405 KB on one measured request,
mostly interpreter-authority receipts and full stdout -- so reading it whole
costs more than the answer is worth.

The on-disk shape is verified against this repository's own artifacts:
``validation.json`` is ``{"checks": [...], "quality_gate": ..., "schema_id":
..., "worker_mcp_gate": {...}}``, and each check carries ``argv``,
``returncode``, ``stdout_tail``/``stderr_tail``, and -- only when it FAILED --
a ``failure_receipt`` holding ``failure_class``, ``diagnostic_tail`` and
``timed_out``. There is no top-level ``failure_class``; a check that passed has
none, and this script reports ``null`` rather than inventing one. ``diagnostic_
source`` names where each tail came from, so a fallback is never read as the
launcher's own classification.

Read-only.

    python -m aiworkhub.recipes.attempt_validation --request-id ID [--tail-chars N]
"""

from __future__ import annotations

import argparse
import sys

from . import _common as common

SCHEMA_ID = "aiworkhub.recipe.attempt_validation.v1"


def _fold_check(check: dict, tail_chars: int) -> dict:
    receipt = check.get("failure_receipt")
    receipt = receipt if isinstance(receipt, dict) else {}
    if receipt.get("diagnostic_tail"):
        diagnostic = str(receipt["diagnostic_tail"])
        source = "failure_receipt.diagnostic_tail"
    elif check.get("stderr_tail"):
        diagnostic = str(check["stderr_tail"])
        source = "check.stderr_tail"
    elif check.get("stdout_tail"):
        diagnostic = str(check["stdout_tail"])
        source = "check.stdout_tail"
    else:
        diagnostic = ""
        source = "none"
    tail, truncated = common.bounded_text(diagnostic, tail_chars)
    argv = check.get("argv")
    declared = check.get("declared_argv")
    return {
        "command": check.get("command"),
        "argv": argv if isinstance(argv, list) else None,
        "declared_argv": declared if isinstance(declared, list) else None,
        "returncode": check.get("returncode"),
        "duration_seconds": check.get("duration_seconds"),
        "timeout_seconds": check.get("timeout_seconds"),
        "timed_out": receipt.get("timed_out"),
        "behavioral_role": check.get("behavioral_role"),
        "execution_boundary": check.get("execution_boundary"),
        "sandbox_backend": check.get("sandbox_backend"),
        "failure_class": receipt.get("failure_class"),
        "diagnostic_source": source,
        "diagnostic_tail": tail,
        "diagnostic_truncated": truncated,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--request-id", required=True, help="32-hex launch request id")
    parser.add_argument(
        "--tail-chars",
        type=int,
        default=2000,
        help="per-check diagnostic tail size (default: 2000)",
    )
    args = parser.parse_args(argv)

    request_id = str(args.request_id).strip().lower()
    if not common.is_request_id(request_id):
        return common.fail(
            SCHEMA_ID, "invalid_request_id", "request id must be 32 hex characters"
        )
    tail_chars = max(0, min(int(args.tail_chars), 20_000))

    directory = common.ATTEMPT_ARTIFACTS_DIR / request_id
    path = directory / "validation.json"
    if not path.is_file():
        return common.fail(
            SCHEMA_ID, "validation_artifact_missing", f"no validation.json at {path}"
        )
    payload = common.load_json(path)
    if not isinstance(payload, dict):
        return common.fail(
            SCHEMA_ID, "validation_artifact_unreadable", f"cannot parse {path}"
        )

    raw_checks = payload.get("checks")
    checks = [
        _fold_check(check, tail_chars)
        for check in (raw_checks if isinstance(raw_checks, list) else [])
        if isinstance(check, dict)
    ]
    failed = [c for c in checks if c["returncode"] not in (0, None)]
    gate = payload.get("worker_mcp_gate")
    quality = payload.get("quality_gate")

    return common.emit(
        {
            "schema_id": SCHEMA_ID,
            "request_id": request_id,
            "artifact_path": str(path),
            "artifact_bytes": path.stat().st_size,
            "artifact_schema_id": payload.get("schema_id"),
            "check_count": len(checks),
            "failed_count": len(failed),
            "passed": not failed,
            "failure_classes": sorted(
                {c["failure_class"] for c in failed if c["failure_class"]}
            ),
            "checks": checks,
            "worker_mcp_gate": (
                {
                    k: gate.get(k)
                    for k in ("ok", "passed", "reason", "required_tools", "missing_tools")
                    if k in gate
                }
                if isinstance(gate, dict)
                else None
            ),
            "quality_gate": quality if isinstance(quality, (dict, str)) else None,
        }
    )


if __name__ == "__main__":
    sys.exit(main())
