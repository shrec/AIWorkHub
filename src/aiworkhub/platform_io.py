"""Small cross-platform filesystem primitives used by the MCP runtime.

Windows does not provide :mod:`fcntl` or ``os.fchmod``.  Keeping these
differences behind this module lets the rest of AIWorkHub use the same
repository-local locking contract on Linux, macOS, and Windows.
"""

from __future__ import annotations

import os


def chmod_fd(fd: int, mode: int) -> None:
    """Apply a POSIX descriptor mode where the host supports it.

    Windows ACLs are not represented by POSIX mode bits and Python does not
    expose ``os.fchmod`` there, so the secure creation flags remain the
    authority and this operation intentionally becomes a no-op.
    """

    fchmod = getattr(os, "fchmod", None)
    if fchmod is not None:
        fchmod(fd, mode)


def _prepare_windows_lock_byte(fd: int) -> None:
    """Ensure byte zero exists and select it for ``msvcrt.locking``."""

    if os.fstat(fd).st_size == 0:
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, b"0")
    os.lseek(fd, 0, os.SEEK_SET)


def lock_fd(fd: int, *, blocking: bool) -> None:
    """Acquire an exclusive advisory lock for an open file descriptor."""

    if os.name == "nt":
        import msvcrt

        _prepare_windows_lock_byte(fd)
        mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
        msvcrt.locking(fd, mode, 1)
        return

    import fcntl

    operation = fcntl.LOCK_EX
    if not blocking:
        operation |= fcntl.LOCK_NB
    fcntl.flock(fd, operation)


def unlock_fd(fd: int) -> None:
    """Release a lock acquired by :func:`lock_fd`."""

    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(fd, fcntl.LOCK_UN)
