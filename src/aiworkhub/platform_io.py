"""Small cross-platform filesystem primitives used by the MCP runtime.

Windows does not provide :mod:`fcntl` or ``os.fchmod``.  Keeping these
differences behind this module lets the rest of AIWorkHub use the same
repository-local locking contract on Linux, macOS, and Windows.
"""

from __future__ import annotations

import errno
import os
import time


WINDOWS_REPLACE_RETRY_SECONDS = 1.0
WINDOWS_LOCK_POLL_SECONDS = 0.02
# Windows byte-range locks conflict between two open handles even inside one
# process, so a lock this process already holds on another handle can never be
# satisfied by waiting. POSIX ``flock`` callers can afford to block forever;
# here an unbounded wait would freeze whatever the caller is serving -- a
# dashboard snapshot, for instance -- with no way out. Fail loudly instead.
WINDOWS_LOCK_MAX_WAIT_SECONDS = 20.0
# msvcrt.locking() reports a contended byte range as EDEADLOCK ("resource
# deadlock avoided") and, on some hosts, EACCES. Neither is a real error for
# a caller that asked to wait; anything else is.
def _deadlock_errno(errno_module: object = errno) -> int:
    """Return the host spelling of the POSIX/Windows deadlock errno."""

    value = getattr(errno_module, "EDEADLOCK", None)
    if value is None:
        value = getattr(errno_module, "EDEADLK")
    return int(value)


_WINDOWS_LOCK_CONTENDED_ERRNOS = frozenset({_deadlock_errno(), errno.EACCES})


class AdvisoryLockTimeout(TimeoutError):
    """A recognized advisory-lock conflict outlived its bounded wait."""


def windows_pid_is_alive(pid: int) -> bool:
    """Check Windows process liveness without ever signalling the process.

    ``os.kill(pid, 0)`` is the conventional POSIX probe, but CPython maps
    non-console signals to ``TerminateProcess`` on Windows. In particular a
    zero signal can terminate the target with exit code 0, which previously
    killed the VS Code extension host while the dashboard enumerated routes.
    """

    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
    open_process.restype = ctypes.c_void_p
    get_exit_code = kernel32.GetExitCodeProcess
    get_exit_code.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32))
    get_exit_code.restype = ctypes.c_int
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (ctypes.c_void_p,)
    close_handle.restype = ctypes.c_int

    handle = open_process(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        # Access denied still establishes that a protected process exists.
        return ctypes.get_last_error() == 5
    exit_code = ctypes.c_uint32()
    try:
        if not get_exit_code(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == 259  # STILL_ACTIVE
    finally:
        close_handle(handle)


def chmod_fd(fd: int, mode: int) -> None:
    """Apply a POSIX descriptor mode where the host supports it.

    Windows ACLs are not represented by POSIX mode bits and Python does not
    expose ``os.fchmod`` there, so the secure creation flags remain the
    authority and this operation intentionally becomes a no-op.
    """

    fchmod = getattr(os, "fchmod", None)
    if fchmod is not None:
        fchmod(fd, mode)


def posix_path_modes_supported(platform_name: str | None = None) -> bool:
    """Return whether POSIX path mode bits are an enforceable authority."""

    return (platform_name or os.name) != "nt"


def chmod_path(
    path: str | os.PathLike[str],
    mode: int,
    *,
    platform_name: str | None = None,
) -> None:
    """Apply a POSIX path mode only on hosts where it is meaningful.

    Windows secures these runtime files through creation semantics and the
    containing directory's ACL.  Retrying POSIX ``chmod`` there can instead
    fail with ``WinError 5`` on otherwise valid user-owned paths.
    """

    if posix_path_modes_supported(platform_name):
        os.chmod(path, mode)


def atomic_replace(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
    """Replace a path atomically, tolerating transient Windows sharing locks.

    POSIX permits replacing an open destination, so that path remains a single
    ``os.replace`` call. Windows readers can briefly deny deletion access; a
    short bounded retry prevents a harmless concurrent read from turning a
    durable status write into a supervisor failure.
    """

    if os.name != "nt":
        os.replace(source, destination)
        return
    deadline = time.monotonic() + WINDOWS_REPLACE_RETRY_SECONDS
    while True:
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


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
        if not blocking:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return
        # ``msvcrt.LK_LOCK`` is NOT the Windows equivalent of
        # ``flock(LOCK_EX)``.  It retries ten times at one-second intervals
        # and then raises ``OSError``, so a lock genuinely held by someone
        # else fails on Windows where a POSIX caller would simply wait.  Poll
        # the non-blocking primitive instead, but bound the wait: unlike
        # ``flock``, a Windows byte-range lock can be blocked by this very
        # process holding another handle, which no amount of waiting can
        # clear. Waiting forever there would hang the caller outright.
        deadline = time.monotonic() + WINDOWS_LOCK_MAX_WAIT_SECONDS
        while True:
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return
            except OSError as exc:
                if exc.errno not in _WINDOWS_LOCK_CONTENDED_ERRNOS:
                    raise
                if time.monotonic() >= deadline:
                    raise AdvisoryLockTimeout(
                        "windows_advisory_lock_timeout after "
                        f"{WINDOWS_LOCK_MAX_WAIT_SECONDS:g}s"
                    ) from exc
                time.sleep(WINDOWS_LOCK_POLL_SECONDS)
                # locking() leaves the file pointer where it found it, but a
                # failed attempt must still start from the same lock byte.
                os.lseek(fd, 0, os.SEEK_SET)

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
