"""Worktree-retention rule version fence: a stale build must not sweep what a newer build retained."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path

from .platform_io import atomic_replace, lock_fd, open_lock_file, unlock_fd

# Bump whenever the set of statuses or reasons that allow worktree removal changes.
WORKTREE_RETENTION_RULE_VERSION = 1

RULE_MARKER_NAME = "worktree_retention_rule.json"
RULE_MARKER_SCHEMA_ID = "aiworkhub.worktree_retention_rule.v1"
RULE_LOCK_NAME = f"{RULE_MARKER_NAME}.lock"
# Safety backstop only: a valid marker is ~100 bytes, so a larger file is corrupt and is not read whole.
_MARKER_MAX_BYTES = 4096


def read_recorded_rule_version(runtime_dir: Path) -> int:
    """Return the highest rule version recorded under ``runtime_dir``; 0 when absent or invalid."""
    try:
        with (Path(runtime_dir) / RULE_MARKER_NAME).open("rb") as handle:
            payload = json.loads(handle.read(_MARKER_MAX_BYTES).decode("utf-8-sig"))
    except (OSError, ValueError, RecursionError):
        return 0
    version = payload.get("version") if isinstance(payload, dict) else None
    return version if type(version) is int and version > 0 else 0


def record_rule_version(runtime_dir: Path, version: int) -> int:
    """Atomically record ``max(recorded, version)``; a marker I/O failure keeps the previous value."""
    recorded = read_recorded_rule_version(runtime_dir)
    if version <= recorded or not Path(runtime_dir).is_dir():
        return recorded
    try:
        lock = open_lock_file(Path(runtime_dir) / RULE_LOCK_NAME)
    except OSError:
        return recorded
    try:
        # Builds racing past the same stale value serialize here, so a lower version never lands last.
        lock_fd(lock, blocking=True)
        try:
            return _raise_marker(Path(runtime_dir), version)
        finally:
            unlock_fd(lock)
    except OSError:
        return read_recorded_rule_version(runtime_dir)
    finally:
        os.close(lock)


def _raise_marker(runtime_dir: Path, version: int) -> int:
    """Under the lock: re-read the marker and write ``version`` only while it is still higher."""
    recorded = read_recorded_rule_version(runtime_dir)
    if version <= recorded:
        return recorded
    marker = Path(runtime_dir) / RULE_MARKER_NAME
    body = json.dumps({"schema_id": RULE_MARKER_SCHEMA_ID, "version": version}, sort_keys=True)
    temp: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(
            dir=marker.parent, prefix=f"{marker.name}.", suffix=".tmp"
        )
        temp = Path(name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(body)
        atomic_replace(temp, marker)
        return version
    except OSError:
        return recorded
    finally:
        if temp is not None:
            with contextlib.suppress(OSError):
                temp.unlink(missing_ok=True)


def superseded_reason(mine: int, *recorded: object) -> str | None:
    """Return the refusal reason when any recorded rule version is newer than ``mine``."""
    newest = max((value for value in recorded if type(value) is int), default=mine)
    return f"retention_rule_superseded:{mine}<{newest}" if newest > mine else None
