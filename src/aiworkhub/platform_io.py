"""Small cross-platform filesystem primitives used by the MCP runtime.

Windows does not provide :mod:`fcntl` or ``os.fchmod``.  Keeping these
differences behind this module lets the rest of AIWorkHub use the same
repository-local locking contract on Linux, macOS, and Windows.
"""

from __future__ import annotations

import errno
import ctypes
import os
import subprocess
import time
import unicodedata
from collections.abc import Callable
from typing import Any, Protocol, TypedDict, cast


class BackgroundProcessLaunchKwargs(TypedDict, total=False):
    """Platform-specific kwargs for a supervised headless subprocess."""

    creationflags: int
    startupinfo: Any
    start_new_session: bool


def background_process_launch_kwargs(
    platform_name: str | None = None,
) -> BackgroundProcessLaunchKwargs:
    """Return only the headless process flags supported by this platform."""

    platform_name = os.name if platform_name is None else platform_name
    if platform_name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        return {
            "creationflags": subprocess.CREATE_NO_WINDOW,
            "startupinfo": startupinfo,
        }
    return {"start_new_session": True}


_INVALID_HANDLE_VALUE = cast(int, ctypes.c_void_p(-1).value)
_DOS_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{number}" for number in range(1, 10)}
    | {f"LPT{number}" for number in range(1, 10)}
)
_WINDOWS_RESERVED_SEGMENT_CHARACTERS = frozenset('<>:"|?*')


class _WindowsLibraryLoader(Protocol):
    def __call__(self, name: str, **kwargs: object) -> Any: ...


class _UnicodeString(ctypes.Structure):
    _fields_ = [
        ("Length", ctypes.c_ushort),
        ("MaximumLength", ctypes.c_ushort),
        ("Buffer", ctypes.c_void_p),
    ]


class _ObjectAttributes(ctypes.Structure):
    _fields_ = [
        ("Length", ctypes.c_uint32),
        ("RootDirectory", ctypes.c_void_p),
        ("ObjectName", ctypes.POINTER(_UnicodeString)),
        ("Attributes", ctypes.c_uint32),
        ("SecurityDescriptor", ctypes.c_void_p),
        ("SecurityQualityOfService", ctypes.c_void_p),
    ]


class _IoStatusBlockUnion(ctypes.Union):
    _fields_ = [("Status", ctypes.c_int32), ("Pointer", ctypes.c_void_p)]


class _IoStatusBlock(ctypes.Structure):
    _anonymous_ = ("result",)
    _fields_ = [("result", _IoStatusBlockUnion), ("Information", ctypes.c_size_t)]


class _FileAttributeTagInfo(ctypes.Structure):
    _fields_ = [("FileAttributes", ctypes.c_uint32), ("ReparseTag", ctypes.c_uint32)]


class _FileIdInfo(ctypes.Structure):
    _fields_ = [
        ("VolumeSerialNumber", ctypes.c_ulonglong),
        ("FileId", ctypes.c_ubyte * 16),
    ]


class OwnedWindowsHandle:
    """Pointer-width-safe Windows handle with checked, retryable ownership release."""

    def __init__(
        self,
        value: int,
        close_handle: Callable[[ctypes.c_void_p], int],
        get_last_error: Callable[[], int],
    ) -> None:
        self._value: int | None = value
        self._close_handle = close_handle
        self._get_last_error = get_last_error

    @property
    def value(self) -> int:
        if self._value is None:
            raise ValueError("Windows handle is closed")
        return self._value

    @property
    def closed(self) -> bool:
        return self._value is None

    def close(self) -> None:
        value = self._value
        if value is None:
            return
        if not self._close_handle(ctypes.c_void_p(value)):
            raise _windows_error(self._get_last_error())
        self._value = None

    def __enter__(self) -> OwnedWindowsHandle:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _canonical_windows_child_segment(child_name: str) -> tuple[str, int]:
    if not isinstance(child_name, str):
        raise TypeError("child_name must be str")
    if (
        not child_name
        or child_name in {".", ".."}
        or "/" in child_name
        or "\\" in child_name
        or "\x00" in child_name
        or any(
            character in _WINDOWS_RESERVED_SEGMENT_CHARACTERS
            or ord(character) < 32
            for character in child_name
        )
        or child_name.endswith((".", " "))
        or unicodedata.normalize("NFC", child_name) != child_name
    ):
        raise ValueError("child_name must be one canonical Windows segment")
    if child_name.split(".", 1)[0].upper() in _DOS_DEVICE_NAMES:
        raise ValueError("child_name is a reserved DOS device name")
    try:
        encoded = child_name.encode("utf-16-le", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError("child_name is not valid UTF-16") from exc
    if len(encoded) > 0xFFFC:
        raise ValueError("child_name exceeds UNICODE_STRING capacity")
    return child_name, len(encoded)


def _windows_error(code: int) -> OSError:
    win_error = getattr(ctypes, "WinError", None)
    if win_error is not None:
        return cast(OSError, win_error(code))
    return OSError(code, f"Windows error {code}")


def _windows_dll_loader() -> _WindowsLibraryLoader:
    return cast(_WindowsLibraryLoader, getattr(ctypes, "WinDLL"))


def _windows_last_error_getter() -> Callable[[], int]:
    get_last_error = getattr(ctypes, "get_last_error", None)
    if get_last_error is None:
        return lambda: 0
    return cast(Callable[[], int], get_last_error)


def _handle_value(handle: object) -> int:
    value = handle.value if isinstance(handle, ctypes.c_void_p) else handle
    if not isinstance(value, int):
        raise OSError("native Windows handle was not an integer")
    return value


def open_windows_relative_child_directory(
    parent_handle: int,
    child_name: str,
) -> OwnedWindowsHandle:
    """Open and validate one directory directly beneath a borrowed parent HANDLE."""

    if isinstance(parent_handle, bool) or not isinstance(parent_handle, int):
        raise TypeError("parent_handle must be int")
    if parent_handle == 0 or parent_handle == _INVALID_HANDLE_VALUE:
        raise ValueError("parent_handle is invalid")
    child_name, byte_length = _canonical_windows_child_segment(child_name)

    win_dll = _windows_dll_loader()
    ntdll = win_dll("ntdll", use_last_error=True)
    kernel32 = win_dll("kernel32", use_last_error=True)
    get_last_error = _windows_last_error_getter()
    nt_create_file = ntdll.NtCreateFile
    nt_create_file.argtypes = (
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_uint32,
        ctypes.POINTER(_ObjectAttributes),
        ctypes.POINTER(_IoStatusBlock),
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
    )
    nt_create_file.restype = ctypes.c_int32
    rtl_status_to_dos_error = ntdll.RtlNtStatusToDosError
    rtl_status_to_dos_error.argtypes = (ctypes.c_int32,)
    rtl_status_to_dos_error.restype = ctypes.c_uint32
    get_file_information = kernel32.GetFileInformationByHandleEx
    get_file_information.argtypes = (
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
    )
    get_file_information.restype = ctypes.c_int
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (ctypes.c_void_p,)
    close_handle.restype = ctypes.c_int

    name_buffer = ctypes.create_unicode_buffer(child_name)
    unicode_name = _UnicodeString(
        byte_length,
        byte_length + 2,
        ctypes.cast(name_buffer, ctypes.c_void_p),
    )
    attributes = _ObjectAttributes(
        ctypes.sizeof(_ObjectAttributes),
        ctypes.c_void_p(parent_handle),
        ctypes.pointer(unicode_name),
        0x40,
        None,
        None,
    )
    result_handle = ctypes.c_void_p()
    io_status = _IoStatusBlock()
    status = int(
        nt_create_file(
            ctypes.byref(result_handle),
            0x00100081,
            ctypes.byref(attributes),
            ctypes.byref(io_status),
            None,
            0,
            0x7,
            0x1,
            0x200021,
            None,
            0,
        )
    )
    if status < 0:
        winerror = int(rtl_status_to_dos_error(status))
        raise OSError(
            winerror,
            f"NtCreateFile failed for relative child {child_name!r}",
        )
    value = result_handle.value
    if not value:
        raise OSError("NtCreateFile succeeded without a handle")
    owned = OwnedWindowsHandle(value, close_handle, get_last_error)
    try:
        tag_info = _FileAttributeTagInfo()
        if not get_file_information(
            owned.value,
            9,
            ctypes.byref(tag_info),
            ctypes.sizeof(tag_info),
        ):
            raise _windows_error(get_last_error())
        if not tag_info.FileAttributes & 0x10 or tag_info.ReparseTag != 0:
            raise OSError("opened child is not a non-reparse directory")
        file_id = _FileIdInfo()
        if not get_file_information(
            owned.value,
            18,
            ctypes.byref(file_id),
            ctypes.sizeof(file_id),
        ):
            raise _windows_error(get_last_error())
        if file_id.VolumeSerialNumber == 0 or not any(file_id.FileId):
            raise OSError("opened child has no stable volume/file identity")
    except BaseException:
        owned.close()
        raise
    return owned


WINDOWS_REPLACE_RETRY_SECONDS = 1.0
# One bounded advisory-lock wait shared by every platform -- not a second
# constant beside a Windows one. A blocking ``lock_fd`` still waits for a
# genuine holder to release, but it stops meaning *forever*: an unbounded wait
# freezes whatever the caller is serving -- a dashboard snapshot, a
# reconciliation monitor -- with no way out, and on Windows a byte-range lock
# this process already holds on another handle can never be satisfied by
# waiting at all. Both platforms poll the non-blocking primitive and, on this
# one shared deadline, raise :class:`AdvisoryLockTimeout`.
ADVISORY_LOCK_POLL_SECONDS = 0.02
ADVISORY_LOCK_MAX_WAIT_SECONDS = 20.0
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
# POSIX ``flock(LOCK_NB)`` reports a lock held elsewhere as EWOULDBLOCK (== EAGAIN
# on Linux). That is contention to wait through, not a real error.
_POSIX_LOCK_CONTENDED_ERRNOS = frozenset({errno.EAGAIN, errno.EWOULDBLOCK})


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


def process_is_alive(pid: int) -> bool:
    """The single liveness answer for the whole runtime: alive unless proven dead.

    Every liveness caller -- the launcher, the shared route registry, and the
    temp-owner GC -- imports this one function so they can never disagree. A
    signal refused with ``EPERM`` proves the process *exists* (we simply may not
    signal it), so it reads as ALIVE, not dead: a worker running under another
    uid is still running. Only an explicit "no such process" is death; every
    other ambiguity fails closed to alive so a live process is never
    terminalized. Windows liveness routes through :func:`windows_pid_is_alive`,
    which never signals the target.
    """

    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if os.name == "nt":
        return windows_pid_is_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # EPERM: the process exists, we just may not signal it
    except OSError:
        return True  # undeterminable -> fail closed to alive
    return True


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
        deadline = time.monotonic() + ADVISORY_LOCK_MAX_WAIT_SECONDS
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
                        f"{ADVISORY_LOCK_MAX_WAIT_SECONDS:g}s"
                    ) from exc
                time.sleep(ADVISORY_LOCK_POLL_SECONDS)
                # locking() leaves the file pointer where it found it, but a
                # failed attempt must still start from the same lock byte.
                os.lseek(fd, 0, os.SEEK_SET)

    import fcntl

    if not blocking:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return
    # ``flock(LOCK_EX)`` blocks forever, which parks a monitor thread with no
    # way out when a holder never releases -- and left the AdvisoryLockTimeout
    # recovery unreachable on the platform we actually run on. Poll the
    # non-blocking primitive on the one shared bound instead: blocking still
    # waits for a real holder, but a genuinely stuck lock raises
    # ``AdvisoryLockTimeout`` -- the same recognized signal the Windows branch
    # raises and the launcher already recovers from.
    deadline = time.monotonic() + ADVISORY_LOCK_MAX_WAIT_SECONDS
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as exc:
            if exc.errno not in _POSIX_LOCK_CONTENDED_ERRNOS:
                raise
            if time.monotonic() >= deadline:
                raise AdvisoryLockTimeout(
                    "posix_advisory_lock_timeout after "
                    f"{ADVISORY_LOCK_MAX_WAIT_SECONDS:g}s"
                ) from exc
            time.sleep(ADVISORY_LOCK_POLL_SECONDS)


def unlock_fd(fd: int) -> None:
    """Release a lock acquired by :func:`lock_fd`."""

    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(fd, fcntl.LOCK_UN)
