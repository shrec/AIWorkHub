"""Review-packet view of a launch's ``sandbox_visibility`` receipt (NF-2026-02238).

The launch record (``<request_id>.request.json``) carries the receipt written
by ``linux_read_roots.prepare_launch_visibility``.  This module turns it into
the bounded block the quality-review packet shows, with an explicit verdict
for the ``workspace_prepare`` hook so a reviewer can tell "could not run"
(``spawn_failed`` / ``timed_out`` / ``failed``) from "did not run"
(``not_configured`` / ``not_applicable``) from "ran" (``succeeded``).
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

MAX_LAUNCH_RECORD_BYTES = 16 * 1024 * 1024
MAX_TAIL_CHARS = 1024
_REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_PREPARE_VERDICTS = {
    "succeeded": "ran",
    "failed": "could_not_run",
    "timed_out": "could_not_run",
    "spawn_failed": "could_not_run",
    "not_configured": "did_not_run",
    "not_applicable": "did_not_run",
}


def summarize_sandbox_visibility(receipt: Mapping[str, Any]) -> dict[str, Any]:
    prepare = receipt.get("workspace_prepare")
    prepare = prepare if isinstance(prepare, Mapping) else {}
    status = str(prepare.get("status") or "not_recorded")
    return {
        "backend": str(receipt.get("backend") or "unknown"),
        "read_roots": [str(root) for root in receipt.get("read_roots") or []],
        "read_roots_enforcement": str(receipt.get("read_roots_enforcement") or "unknown"),
        "deny_read": [str(glob) for glob in receipt.get("deny_read") or []],
        "deny_read_enforcement": str(receipt.get("deny_read_enforcement") or "unknown"),
        "denied_file_count": int(receipt.get("denied_file_count") or 0),
        "read_layer_rule_count": int(receipt.get("read_layer_rule_count") or 0),
        "deny_read_refused_count": int(receipt.get("deny_read_refused_count") or 0),
        # Declared limitation: deny_read never hides tracked .git objects.
        "git_object_store_readable": receipt.get("git_object_store_readable") is not False,
        "workspace_prepare": {
            "status": status,
            "verdict": _PREPARE_VERDICTS.get(status, "unknown"),
            "returncode": prepare.get("returncode"),
            "reused": prepare.get("reused") is True,
            "output_tail": str(prepare.get("output_tail") or "")[-MAX_TAIL_CHARS:],
        },
    }


def launch_sandbox_visibility(
    process_dir: str | os.PathLike[str], request_id: str
) -> dict[str, Any] | None:
    """The summarized receipt, ``None`` for a launch record without one.

    A launch record that exists but cannot be read is reported, not skipped.
    """
    if not _REQUEST_ID.fullmatch(request_id):
        return {"status": "launch_record_unreadable", "reason": "request_id_invalid"}
    path = Path(process_dir) / f"{request_id}.request.json"
    if not path.exists():
        return None
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_LAUNCH_RECORD_BYTES + 1)
        if len(raw) > MAX_LAUNCH_RECORD_BYTES:
            return {"status": "launch_record_unreadable", "reason": "too_large"}
        record = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {"status": "launch_record_unreadable", "reason": type(exc).__name__}
    receipt = record.get("sandbox_visibility") if isinstance(record, dict) else None
    if not isinstance(receipt, Mapping):
        return None
    return summarize_sandbox_visibility(receipt)
