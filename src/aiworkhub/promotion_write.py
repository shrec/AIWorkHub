"""Named, retrying promotion-write primitives for worker_workspace.promote.

NF-2026-01198: a promotion write that failed on Windows used to propagate a
bare OSError with no path, operation or cause, and a transient sharing
violation was never retried. Every write here instead raises
``PromotionWriteError`` named
``promotion_write_failed:<relative>:<operation>:errno=<n>:winerror=<n|none>``
-- never a bare errno, never an absolute path -- and a transient sharing
violation on replace/unlink retries with a small bounded backoff.
"""

from __future__ import annotations

import os
import stat
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, TypeVar

from .platform_io import is_windows

#: Win32 ERROR_SHARING_VIOLATION: Python surfaces it as OSError.winerror == 32
#: when the destination is momentarily open elsewhere without FILE_SHARE_DELETE.
SHARING_VIOLATION_WINERROR = 32

#: Win32 ERROR_ACCESS_DENIED. Measured on Windows 11 (Python 3.12, destination
#: held open via CreateFileW without FILE_SHARE_DELETE; share modes 0, READ and
#: READ|WRITE all gave the same result): os.replace(temp, dest) raises winerror
#: 5 (this constant), while Path.unlink() raises winerror 32 instead. Both come
#: from the same transient holder, so replace must treat winerror 5 as
#: transient too -- the tradeoff is a real, non-transient ACL denial on replace
#: now also spends the bounded retry budget before the named failure surfaces.
ACCESS_DENIED_WINERROR = 5

#: Per-operation transient winerror sets (see the measurement above).
_TRANSIENT_WINERRORS = {
    "replace": frozenset({ACCESS_DENIED_WINERROR, SHARING_VIOLATION_WINERROR}),
    "unlink": frozenset({SHARING_VIOLATION_WINERROR}),
}

#: Bounded retry budget for a transient sharing violation on replace/unlink.
PROMOTION_WRITE_RETRY_ATTEMPTS = 8
PROMOTION_WRITE_RETRY_DELAY_SECONDS = 0.3

T = TypeVar("T")


class PromotionWriteError(RuntimeError):
    """A promotion write step failed; str(self) names the path/op/cause.

    worker_workspace.promote wraps every instance in WorkspaceError so
    callers only ever see one exception hierarchy.
    """


def format_promotion_write_failure(relative: str, operation: str, exc: OSError) -> str:
    winerror = getattr(exc, "winerror", None)
    winerror_text = "none" if winerror is None else str(winerror)
    errno_text = "none" if exc.errno is None else str(exc.errno)
    return (
        f"promotion_write_failed:{relative}:{operation}:"
        f"errno={errno_text}:winerror={winerror_text}"
    )


def _is_transient_sharing_violation(operation: str, exc: OSError) -> bool:
    allowed = _TRANSIENT_WINERRORS.get(operation, frozenset())
    return getattr(exc, "winerror", None) in allowed


def clear_readonly_if_set(path: Path) -> int | None:
    """Clear the Windows read-only attribute of exactly this destination file.

    ``promote`` only ever calls this on a path that has already passed the
    allowed_writes scope check and the parent-hash preflight, so unblocking
    its own authorized overwrite is not a new grant. No-op off Windows and
    when the path has no read-only bit set. Returns the original mode when
    it actually cleared the bit, so a failed write can restore it; returns
    None on every other exit.
    """
    if not is_windows():
        return None
    try:
        mode = path.stat().st_mode
    except OSError:
        return None
    if mode & stat.S_IWRITE:
        return None
    try:
        os.chmod(path, mode | stat.S_IWRITE)
    except OSError:
        return None
    return mode


def make_temp_sibling(destination: Path) -> Path:
    """Create a closed, empty temp file beside ``destination`` for atomic publish."""
    fd, temp_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    os.close(fd)
    return Path(temp_name)


def run_promotion_write(
    relative: str, operation: str, func: Callable[..., T], *args: Any, **kwargs: Any
) -> T:
    """Run one non-retried promotion-write step; name any OSError it raises."""
    try:
        return func(*args, **kwargs)
    except OSError as exc:
        raise PromotionWriteError(
            format_promotion_write_failure(relative, operation, exc)
        ) from exc


def run_promotion_write_retrying(
    relative: str,
    operation: str,
    func: Callable[..., T],
    *args: Any,
    attempts: int = PROMOTION_WRITE_RETRY_ATTEMPTS,
    delay_seconds: float = PROMOTION_WRITE_RETRY_DELAY_SECONDS,
    sleep: Callable[[float], None] | None = None,
    **kwargs: Any,
) -> T:
    """Run a replace/unlink step, retrying only a transient sharing violation."""
    wait = sleep or time.sleep
    for attempt in range(1, attempts + 1):
        try:
            return func(*args, **kwargs)
        except OSError as exc:
            if not _is_transient_sharing_violation(operation, exc) or attempt == attempts:
                raise PromotionWriteError(
                    format_promotion_write_failure(relative, operation, exc)
                ) from exc
            wait(delay_seconds)
    raise AssertionError("unreachable")  # pragma: no cover


def run_destination_write_retrying(
    relative: str,
    operation: str,
    destination: Path,
    func: Callable[..., T],
    *args: Any,
) -> T:
    """Clear a read-only destination for one write, restoring it on failure.

    The read-only bit is part of the parent-hash preflight baseline, so a
    clear that outlives a failed write would make the next promote() reject
    the retry as parent_changed_since_launch. Restore it before the
    PromotionWriteError propagates; leave it cleared on success.
    """
    original = clear_readonly_if_set(destination)
    try:
        return run_promotion_write_retrying(relative, operation, func, *args)
    except PromotionWriteError:
        if original is not None:
            try:
                os.chmod(destination, original)
            except OSError:
                pass
        raise
