"""Bounded active-file lifecycle for the process event ledger.

The process ledger is append-only evidence.  The active file is rotated before
it can reach the retention reader's historical 64 MiB fail-closed boundary;
rotated files remain immutable and readers stream them in chronological order.
"""

from __future__ import annotations

import json
import os
import stat
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .platform_io import atomic_replace, chmod_fd, lock_fd, unlock_fd


ACTIVE_LEDGER_MAX_BYTES = 48 * 1024 * 1024


def _archive_pattern(path: Path) -> str:
    return f"{path.stem}.*{path.suffix}"


def ledger_paths(path: Path) -> list[Path]:
    """Return immutable rotations oldest-first, followed by the active file."""

    archives: list[Path] = []
    if path.parent.is_dir():
        for candidate in path.parent.glob(_archive_pattern(path)):
            try:
                info = candidate.lstat()
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                archives.append(candidate)
    archives.sort(key=lambda item: item.name)
    try:
        active_info = path.lstat()
    except OSError:
        active_info = None
    if (
        active_info is not None
        and stat.S_ISREG(active_info.st_mode)
        and not stat.S_ISLNK(active_info.st_mode)
    ):
        archives.append(path)
    return archives


@contextmanager
def _append_lock(path: Path) -> Iterator[None]:
    lock_path = Path(f"{path}.append.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_APPEND | os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(lock_path, flags, 0o600)
    chmod_fd(fd, 0o600)
    with os.fdopen(fd, "a+", encoding="utf-8") as handle:
        lock_fd(handle.fileno(), blocking=True)
        try:
            yield
        finally:
            unlock_fd(handle.fileno())


def _rotate(path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    archive = path.with_name(
        f"{path.stem}.{stamp}.{os.getpid()}.{uuid.uuid4().hex[:8]}{path.suffix}"
    )
    # Use cross-platform atomic_replace instead of bare os.replace: on Windows,
    # a concurrent dashboard/reader holding the active ledger open can briefly
    # make os.replace fail with WinError 32 (sharing violation). The bounded
    # retry in atomic_replace tolerates the transient without weakening the
    # lock-held exclusion of other writers.
    atomic_replace(path, archive)
    os.chmod(archive, 0o600)
    return archive


def append_event(
    path: Path,
    event: dict[str, Any],
    *,
    max_active_bytes: int = ACTIVE_LEDGER_MAX_BYTES,
) -> None:
    """Append one JSON row, rotating the active file before the size bound."""

    if max_active_bytes < 1024:
        raise ValueError("process_ledger_max_bytes_too_small")
    payload = (json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    if len(payload) > max_active_bytes:
        raise ValueError("process_event_exceeds_active_ledger_bound")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    with _append_lock(path):
        try:
            info = path.lstat()
        except FileNotFoundError:
            info = None
        if info is not None:
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise OSError("process_event_ledger_invalid")
            if info.st_size and info.st_size + len(payload) > max_active_bytes:
                _rotate(path)
        flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags, 0o600)
        try:
            chmod_fd(fd, 0o600)
            written = os.write(fd, payload)
            if written != len(payload):
                raise OSError("short_process_event_write")
        finally:
            os.close(fd)


def iter_events(path: Path) -> Iterator[dict[str, Any]]:
    """Stream valid object rows across rotations without whole-file reads."""

    for ledger in ledger_paths(path):
        try:
            with ledger.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(row, dict):
                        yield row
        except OSError:
            continue
