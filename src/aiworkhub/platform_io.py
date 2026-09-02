"""Small cross-platform filesystem primitives used by the MCP runtime.

Windows does not provide :mod:`fcntl` or ``os.fchmod``.  Keeping these
differences behind this module lets the rest of AIWorkHub use the same
repository-local locking contract on Linux, macOS, and Windows.
"""

from __future__ import annotations

import ctypes
import errno
import importlib
import math
import ntpath
import os
import posixpath
import signal
import subprocess
import sys
import time
import unicodedata
from collections.abc import Callable
from typing import Any, Protocol, TypedDict, cast


class BackgroundProcessLaunchKwargs(TypedDict, total=False):
    """Platform-specific kwargs for a supervised headless subprocess."""

    creationflags: int
    startupinfo: Any
    start_new_session: bool


ProcessGroupLaunchKwargs = BackgroundProcessLaunchKwargs

# ``signal.SIGKILL`` does not exist on Windows, but the POSIX escalation path
# must still be importable (and unit-testable with an injected ``killpg``)
# there; 9 is the invariant POSIX value.
_POSIX_SIGKILL = getattr(signal, "SIGKILL", 9)


class _PlatformProcessBackend(Protocol):
    def background_process_launch_kwargs(
        self, platform_name: str | None = None
    ) -> BackgroundProcessLaunchKwargs: ...

    def windows_process_is_alive(self, pid: int) -> bool: ...

    def process_is_alive(self, pid: int) -> bool: ...


_platform_process = importlib.import_module(
    "._platform_process" if __package__ else "_platform_process",
    __package__ or None,
)
_platform_process_backend = cast(_PlatformProcessBackend, _platform_process)


def _normalized_platform(platform_name: str | None = None) -> str:
    """Return the canonical ``windows``, ``linux`` or ``macos`` platform name."""

    name = (sys.platform if platform_name is None else platform_name).lower()
    if name in {"nt", "windows", "win32", "cygwin", "msys"}:
        return "windows"
    if name.startswith("linux") or name == "posix":
        return "linux"
    if name in {"darwin", "mac", "macos", "osx"}:
        return "macos"
    return name


def is_windows(platform_name: str | None = None) -> bool:
    return _normalized_platform(platform_name) == "windows"


def is_linux(platform_name: str | None = None) -> bool:
    return _normalized_platform(platform_name) == "linux"


def is_macos(platform_name: str | None = None) -> bool:
    return _normalized_platform(platform_name) == "macos"


def available_memory_bytes(platform_name: str | None = None) -> int | None:
    """Return host-available physical memory, or ``None`` when it cannot be measured."""

    if is_linux(platform_name):
        try:
            with open("/proc/meminfo", encoding="ascii") as handle:
                for line in handle:
                    if line.startswith("MemAvailable:"):
                        parts = line.split()
                        if len(parts) == 3 and parts[2] == "kB":
                            value = int(parts[1]) * 1024
                            return value if value >= 0 else None
                        return None
            return None
        except (OSError, UnicodeError, ValueError):
            return None
    if is_windows(platform_name):

        class _MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("length", ctypes.c_ulong),
                ("memory_load", ctypes.c_ulong),
                ("total_physical", ctypes.c_ulonglong),
                ("available_physical", ctypes.c_ulonglong),
                ("total_page_file", ctypes.c_ulonglong),
                ("available_page_file", ctypes.c_ulonglong),
                ("total_virtual", ctypes.c_ulonglong),
                ("available_virtual", ctypes.c_ulonglong),
                ("available_extended_virtual", ctypes.c_ulonglong),
            ]

        try:
            status = _MemoryStatus()
            status.length = ctypes.sizeof(status)
            # argtypes/restype are set explicitly, as every other ctypes entry
            # point in this module does: without them ctypes guesses a 32-bit
            # int for both the pointer argument and the BOOL return.
            global_memory_status = ctypes.windll.kernel32.GlobalMemoryStatusEx
            global_memory_status.argtypes = (ctypes.POINTER(_MemoryStatus),)
            global_memory_status.restype = ctypes.c_int
            if not global_memory_status(ctypes.byref(status)):
                return None
            return int(status.available_physical)
        except (AttributeError, OSError, ValueError):
            return None
    try:
        pages = int(os.sysconf("SC_AVPHYS_PAGES"))
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        available = pages * page_size
        return available if pages >= 0 and page_size > 0 else None
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _windows_creation_flag() -> int:
    """Return the named Windows process-group flag through a typed boundary."""

    return cast(int, getattr(subprocess, "CREATE_NEW_PROCESS_GROUP"))


def process_group_launch_kwargs(
    platform_name: str | None = None,
) -> ProcessGroupLaunchKwargs:
    """Return subprocess kwargs that create a separately signalable process group."""

    if is_windows(platform_name):
        return {"creationflags": _windows_creation_flag()}
    return {"start_new_session": True}


def _valid_process_identity(identity: object) -> bool:
    return (
        isinstance(identity, int)
        and not isinstance(identity, bool)
        and identity > 0
    )


def _resolve_posix_killpg(
    killpg: Callable[[int, int], None] | None,
) -> Callable[[int, int], None] | None:
    if killpg is not None:
        return killpg
    candidate = getattr(os, "killpg", None)
    return candidate if callable(candidate) else None


def probe_process_group(
    identity: int,
    *,
    platform_name: str | None = None,
    killpg: Callable[[int, int], None] | None = None,
    windows_probe: Callable[[int], bool] | None = None,
) -> bool:
    """Probe a process group, failing closed except for permission denial."""

    if not _valid_process_identity(identity):
        return False
    if is_windows(platform_name):
        return (windows_probe or windows_pid_is_alive)(identity)
    posix_killpg = _resolve_posix_killpg(killpg)
    if posix_killpg is None:
        return False
    try:
        posix_killpg(identity, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as error:
        return error.errno == errno.EPERM
    return True


def terminate_process_tree(
    identity: int,
    *,
    platform_name: str | None = None,
    timeout: float = 5.0,
    poll_interval: float = 0.05,
    killpg: Callable[[int, int], None] | None = None,
    probe: Callable[[int], bool] | None = None,
    run: Callable[..., Any] = subprocess.run,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Terminate one process tree, escalating a surviving POSIX group to KILL."""

    if (
        not _valid_process_identity(identity)
        or not math.isfinite(timeout)
        or not math.isfinite(poll_interval)
        or timeout < 0
        or poll_interval < 0
    ):
        return False
    if is_windows(platform_name):
        try:
            run(
                ["taskkill", "/PID", str(identity), "/T"],
                check=False,
                shell=False,
                timeout=timeout,
            )
        except (subprocess.TimeoutExpired, OSError):
            return False
        alive = probe or (lambda pid: windows_pid_is_alive(pid))
        deadline = monotonic() + timeout
        while alive(identity):
            remaining = deadline - monotonic()
            if remaining <= 0:
                break
            sleep(min(poll_interval, remaining))
        else:
            return True
        try:
            completed = run(
                ["taskkill", "/F", "/PID", str(identity), "/T"],
                check=False,
                shell=False,
                timeout=timeout,
            )
        except (subprocess.TimeoutExpired, OSError):
            return False
        return int(completed.returncode) == 0

    posix_killpg = _resolve_posix_killpg(killpg)
    if posix_killpg is None:
        return False
    alive = probe or (
        lambda pgid: probe_process_group(
            pgid, platform_name="posix", killpg=posix_killpg
        )
    )

    def wait_until_gone() -> bool:
        deadline = monotonic() + timeout
        while alive(identity):
            remaining = deadline - monotonic()
            if remaining <= 0:
                return False
            sleep(min(poll_interval, remaining))
        return True

    try:
        posix_killpg(identity, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except (PermissionError, OSError):
        return False
    if wait_until_gone():
        return True
    try:
        posix_killpg(identity, _POSIX_SIGKILL)
    except ProcessLookupError:
        return True
    except (PermissionError, OSError):
        return False
    return wait_until_gone()


def executable_name(name: str, platform_name: str | None = None) -> str:
    """Return a platform-appropriate executable filename."""

    if is_windows(platform_name) and not name.lower().endswith(".exe"):
        return f"{name}.exe"
    return name


def path_key(path: str | os.PathLike[str], platform_name: str | None = None) -> str:
    """Return a lexical path identity key with platform-specific case semantics."""

    value = os.fspath(path)
    if is_windows(platform_name):
        return ntpath.normcase(ntpath.abspath(ntpath.normpath(value)))
    return posixpath.abspath(posixpath.normpath(value))


def paths_equal(
    left: str | os.PathLike[str],
    right: str | os.PathLike[str],
    platform_name: str | None = None,
) -> bool:
    return path_key(left, platform_name) == path_key(right, platform_name)


def background_process_launch_kwargs(
    platform_name: str | None = None,
) -> BackgroundProcessLaunchKwargs:
    """Return only the headless process flags supported by this platform."""

    return _platform_process_backend.background_process_launch_kwargs(platform_name)


_INVALID_HANDLE_VALUE = cast(int, ctypes.c_void_p(-1).value)
# Windows reserves CON/PRN/AUX/NUL, COM1-COM9 and LPT1-LPT9 as device names;
# the superscript-digit spellings COM¹-COM³ and LPT¹-LPT³ are reserved as well.
# The split-below check rejects the bare name and every dotted extension form
# before any native call.
_DOS_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{number}" for number in range(1, 10)}
    | {f"LPT{number}" for number in range(1, 10)}
    | {f"{prefix}{superscript}" for prefix in ("COM", "LPT") for superscript in "¹²³"}
)
_WINDOWS_RESERVED_SEGMENT_CHARACTERS = frozenset('<>:"|?*')

# FILE_ATTRIBUTE_* bits used to classify an authenticated child identity.
_FILE_ATTRIBUTE_DIRECTORY = 0x10
_FILE_ATTRIBUTE_DEVICE = 0x40

# Enumeration authority (unchanged legacy contract): the directory must be
# listable and its attributes readable, and sharing includes FILE_SHARE_DELETE
# so a concurrent cleaner never blocks enumeration of the same directory.
_WINDOWS_DIRECTORY_DESIRED_ACCESS = 0x00100081  # LIST | READ_ATTRIBUTES | SYNCHRONIZE
_WINDOWS_DIRECTORY_SHARE_ACCESS = 0x7  # FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE
# Disposition authority: the open requests DELETE | FILE_READ_ATTRIBUTES |
# SYNCHRONIZE so the very same HANDLE can later be marked for deletion, and
# shares only READ | WRITE -- never FILE_SHARE_DELETE -- so a pending delete mark
# is never silently absorbed by a concurrent opener.
_WINDOWS_CHILD_DESIRED_ACCESS = 0x00110080  # DELETE | FILE_READ_ATTRIBUTES | SYNCHRONIZE
_WINDOWS_CHILD_SHARE_ACCESS = 0x3  # FILE_SHARE_READ | FILE_SHARE_WRITE

_FILE_OPEN = 0x1
_FILE_DIRECTORY_FILE = 0x1
_FILE_SYNCHRONOUS_IO_NONALERT = 0x20
_FILE_OPEN_REPARSE_POINT = 0x00200000
# The enumeration opener constrains the open to directories; the disposition
# opener leaves the type unconstrained so it can target a file or a directory.
_WINDOWS_DIRECTORY_CREATE_OPTIONS = (
    _FILE_DIRECTORY_FILE | _FILE_SYNCHRONOUS_IO_NONALERT | _FILE_OPEN_REPARSE_POINT
)
_WINDOWS_CHILD_CREATE_OPTIONS = (
    _FILE_SYNCHRONOUS_IO_NONALERT | _FILE_OPEN_REPARSE_POINT
)


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


class _FileDispositionInfo(ctypes.Structure):
    _fields_ = [("DeleteFile", ctypes.c_ubyte)]


class _FileDispositionInfoEx(ctypes.Structure):
    _fields_ = [("Flags", ctypes.c_uint32)]


_FILE_ATTRIBUTE_TAG_INFO = 9
_FILE_ID_INFO = 18
_FILE_DISPOSITION_INFO = 4
_FILE_DISPOSITION_INFO_EX = 21
_FILE_DISPOSITION_FLAG_DELETE = 0x00000001
_FILE_DISPOSITION_FLAG_POSIX_SEMANTICS = 0x00000002
# The one documented "unsupported" result: pre-Windows 10 1709 filesystems
# reject FileDispositionInfoEx with ERROR_INVALID_PARAMETER (87) -- the exact
# signal that plain FileDispositionInfo delete semantics is the only fallback.
_WINDOWS_DISPOSITION_UNSUPPORTED_ERRNOS = frozenset({87})


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


class WindowsRelativeChildAuthority:
    """Typed, authenticated relative-child authority for exact-handle disposition.

    Carries the borrowed, identity-authenticated HANDLE together with the type,
    volume serial and FileId proven at open time, so disposition can target the
    very same object without ever reopening a pathname (which could drift to a
    different filesystem object between open and delete).
    """

    def __init__(
        self,
        handle: OwnedWindowsHandle,
        *,
        is_directory: bool,
        volume_serial_number: int,
        file_id: bytes,
    ) -> None:
        self._handle = handle
        self.is_directory = is_directory
        self.volume_serial_number = volume_serial_number
        self.file_id = bytes(file_id)

    @property
    def handle(self) -> OwnedWindowsHandle:
        return self._handle

    @property
    def closed(self) -> bool:
        return self._handle.closed

    def close(self) -> None:
        self._handle.close()

    def __enter__(self) -> WindowsRelativeChildAuthority:
        return self

    def __exit__(self, *_exc: object) -> None:
        self._handle.close()


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


def _open_windows_relative_child_handle(
    parent_handle: int,
    child_name: str,
    desired_access: int,
    share_access: int,
    create_options: int,
) -> tuple[OwnedWindowsHandle, Any, Callable[[], int]]:
    """Open one exact child segment beneath a borrowed parent HANDLE.

    Returns the owned HANDLE together with the bound
    ``GetFileInformationByHandleEx`` and last-error getters so the caller can
    authenticate the child identity on this exact HANDLE before it leaves scope.
    """

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
            desired_access,
            ctypes.byref(attributes),
            ctypes.byref(io_status),
            None,
            0,
            share_access,
            _FILE_OPEN,
            create_options,
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
    return (
        OwnedWindowsHandle(value, close_handle, get_last_error),
        get_file_information,
        get_last_error,
    )


def open_windows_relative_child_directory(
    parent_handle: int,
    child_name: str,
) -> OwnedWindowsHandle:
    """Open and validate one directory directly beneath a borrowed parent HANDLE.

    The enumeration contract is preserved: ``FILE_LIST_DIRECTORY |
    FILE_READ_ATTRIBUTES | SYNCHRONIZE`` with ``FILE_SHARE_DELETE``, so directory
    enumeration is never blocked by a concurrent cleaner and vice versa.
    """

    owned, get_file_information, get_last_error = _open_windows_relative_child_handle(
        parent_handle,
        child_name,
        _WINDOWS_DIRECTORY_DESIRED_ACCESS,
        _WINDOWS_DIRECTORY_SHARE_ACCESS,
        _WINDOWS_DIRECTORY_CREATE_OPTIONS,
    )
    try:
        tag_info = _FileAttributeTagInfo()
        if not get_file_information(
            owned.value,
            _FILE_ATTRIBUTE_TAG_INFO,
            ctypes.byref(tag_info),
            ctypes.sizeof(tag_info),
        ):
            raise _windows_error(get_last_error())
        if (
            not tag_info.FileAttributes & _FILE_ATTRIBUTE_DIRECTORY
            or tag_info.ReparseTag != 0
        ):
            raise OSError("opened child is not a non-reparse directory")
        file_id = _FileIdInfo()
        if not get_file_information(
            owned.value,
            _FILE_ID_INFO,
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


def open_windows_relative_child_disposition(
    parent_handle: int,
    child_name: str,
) -> WindowsRelativeChildAuthority:
    """Open and authenticate one exact child (file or directory) for disposition.

    The open requests ``DELETE | FILE_READ_ATTRIBUTES | SYNCHRONIZE`` and shares
    only ``READ | WRITE``, so the very same HANDLE can later be marked for
    deletion. The object's type (regular file or directory), volume serial and
    FileId are proven here and retained on the returned authority.
    """

    owned, get_file_information, get_last_error = _open_windows_relative_child_handle(
        parent_handle,
        child_name,
        _WINDOWS_CHILD_DESIRED_ACCESS,
        _WINDOWS_CHILD_SHARE_ACCESS,
        _WINDOWS_CHILD_CREATE_OPTIONS,
    )
    try:
        tag_info = _FileAttributeTagInfo()
        if not get_file_information(
            owned.value,
            _FILE_ATTRIBUTE_TAG_INFO,
            ctypes.byref(tag_info),
            ctypes.sizeof(tag_info),
        ):
            raise _windows_error(get_last_error())
        if tag_info.ReparseTag != 0:
            raise OSError("opened child is a reparse point")
        if tag_info.FileAttributes & _FILE_ATTRIBUTE_DEVICE:
            raise OSError("opened child has an unexpected device type")
        is_directory = bool(tag_info.FileAttributes & _FILE_ATTRIBUTE_DIRECTORY)
        file_id = _FileIdInfo()
        if not get_file_information(
            owned.value,
            _FILE_ID_INFO,
            ctypes.byref(file_id),
            ctypes.sizeof(file_id),
        ):
            raise _windows_error(get_last_error())
        if file_id.VolumeSerialNumber == 0 or not any(file_id.FileId):
            raise OSError("opened child has no stable volume/file identity")
    except BaseException:
        owned.close()
        raise
    return WindowsRelativeChildAuthority(
        owned,
        is_directory=is_directory,
        volume_serial_number=int(file_id.VolumeSerialNumber),
        file_id=bytes(file_id.FileId),
    )


def mark_windows_relative_child_disposition(
    authority: WindowsRelativeChildAuthority,
) -> None:
    """Mark the exact authenticated child HANDLE for deletion.

    The child HANDLE was opened and authenticated by
    :func:`open_windows_relative_child_disposition`; this call requests deletion
    of that very HANDLE through ``SetFileInformationByHandle`` and never reopens
    a pathname, so the identity already proven cannot drift to a different
    filesystem object.
    """

    value = authority.handle.value  # raises ValueError once the handle is closed
    win_dll = _windows_dll_loader()
    kernel32 = win_dll("kernel32", use_last_error=True)
    set_file_information = kernel32.SetFileInformationByHandle
    set_file_information.argtypes = (
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
    )
    set_file_information.restype = ctypes.c_int
    get_last_error = _windows_last_error_getter()

    flags = _FILE_DISPOSITION_FLAG_DELETE
    if not authority.is_directory:
        # POSIX delete semantics are only defined for regular files.
        flags |= _FILE_DISPOSITION_FLAG_POSIX_SEMANTICS
    disposition_ex = _FileDispositionInfoEx(flags)
    if not set_file_information(
        ctypes.c_void_p(value),
        _FILE_DISPOSITION_INFO_EX,
        ctypes.byref(disposition_ex),
        ctypes.sizeof(disposition_ex),
    ):
        error = get_last_error()
        if error not in _WINDOWS_DISPOSITION_UNSUPPORTED_ERRNOS:
            raise _windows_error(error)
        if authority.closed:
            raise OSError("child HANDLE was closed before disposition fallback")
        disposition = _FileDispositionInfo(1)
        if not set_file_information(
            ctypes.c_void_p(value),
            _FILE_DISPOSITION_INFO,
            ctypes.byref(disposition),
            ctypes.sizeof(disposition),
        ):
            raise _windows_error(get_last_error())


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
    """A recognized advisory-lock conflict outlived its bounded wait.

    The last errno observed while polling is carried on :attr:`errno` -- a
    structured attribute a caller reads without parsing prose -- and named
    symbolically in the message. ``EACCES`` and the deadlock errno are merged
    in the Windows contended set on purpose: the errno alone cannot decide a
    permission failure from real contention, so the honest outcome is to report
    *which* errno timed out rather than guess. ``errno`` is ``None`` only when
    no contended attempt was ever recorded.
    """

    def __init__(self, message: str, *, errno: int | None = None) -> None:
        super().__init__(message)
        self.errno = errno


def _advisory_lock_errno_symbol(observed_errno: int | None) -> str:
    """Return the symbolic spelling of an errno for a timeout message."""

    if observed_errno is None:
        return "unknown"
    return errno.errorcode.get(observed_errno, str(observed_errno))


def _advisory_lock_timeout(
    platform_label: str, observed_errno: int | None
) -> AdvisoryLockTimeout:
    """Build the one shared timeout shape for both platform lock paths.

    Both the POSIX and Windows paths raise through here, so they expose the same
    ``errno`` attribute and the same message shape; a Windows-only diagnostic
    would recreate the asymmetry NF-2026-00350 just closed.
    """

    return AdvisoryLockTimeout(
        f"{platform_label}_advisory_lock_timeout after "
        f"{ADVISORY_LOCK_MAX_WAIT_SECONDS:g}s "
        f"(last errno {_advisory_lock_errno_symbol(observed_errno)})",
        errno=observed_errno,
    )


def windows_pid_is_alive(pid: int) -> bool:
    """Check Windows process liveness without ever signalling the process.

    ``os.kill(pid, 0)`` is the conventional POSIX probe, but CPython maps
    non-console signals to ``TerminateProcess`` on Windows. In particular a
    zero signal can terminate the target with exit code 0, which previously
    killed the VS Code extension host while the dashboard enumerated routes.
    """

    return _platform_process_backend.windows_process_is_alive(pid)


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

    return _platform_process_backend.process_is_alive(pid)


def chmod_fd(fd: int, mode: int, *, platform_name: str | None = None) -> None:
    """Apply a POSIX descriptor mode where the host supports it.

    Windows ACLs are not represented by POSIX mode bits and Python does not
    expose ``os.fchmod`` there, so the secure creation flags remain the
    authority and this operation intentionally becomes a no-op.

    Applicability is decided by :func:`posix_path_modes_supported` -- the same
    predicate :func:`chmod_path` uses. Deciding it here with a bare
    ``getattr(os, "fchmod")`` capability probe answered one policy question by
    two different rules, and because only ``chmod_path`` accepted a
    ``platform_name`` override the Windows behaviour of this function could not
    be tested at all. The ``fchmod`` absence check survives as a defensive
    fallback for a POSIX-named host that genuinely lacks it.
    """

    if not posix_path_modes_supported(platform_name):
        return
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


class _MsvcrtLocking(Protocol):
    LK_NBLCK: int
    LK_UNLCK: int

    def locking(self, fd: int, mode: int, nbytes: int) -> None: ...


def _prepare_windows_lock_byte(fd: int) -> None:
    """Ensure byte zero exists and select it for ``msvcrt.locking``.

    This REQUIRES A WRITABLE descriptor. ``msvcrt.locking`` locks a byte range
    that has to exist, so an empty lock file gets one byte written into it.
    POSIX ``flock`` needs neither a writable fd nor any file content, so a lock
    file that both branches may open must always be opened for writing -- the
    read-only descriptor POSIX accepts fails here with ``EBADF``.

    Restoring the caller's file offset is not this helper's job; ``lock_fd`` and
    ``unlock_fd`` save and restore it around the whole Windows operation.
    """

    if os.fstat(fd).st_size == 0:
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, b"0")
    os.lseek(fd, 0, os.SEEK_SET)


def _lock_fd_windows(
    fd: int, windows_locking: _MsvcrtLocking, *, blocking: bool
) -> None:
    """Acquire the Windows byte-range lock; the caller restores the offset."""

    _prepare_windows_lock_byte(fd)
    if not blocking:
        windows_locking.locking(fd, windows_locking.LK_NBLCK, 1)
        return
    # ``msvcrt.LK_LOCK`` is NOT the Windows equivalent of ``flock(LOCK_EX)``.
    # It retries ten times at one-second intervals and then raises ``OSError``,
    # so a lock genuinely held by someone else fails on Windows where a POSIX
    # caller would simply wait.  Poll the non-blocking primitive instead, but
    # bound the wait: unlike ``flock``, a Windows byte-range lock can be blocked
    # by this very process holding another handle, which no amount of waiting
    # can clear. Waiting forever there would hang the caller outright.
    deadline = time.monotonic() + ADVISORY_LOCK_MAX_WAIT_SECONDS
    # Track the LAST contended errno so the timeout can report whether the wait
    # ended on the deadlock spelling or a permission-shaped EACCES -- both are
    # retried, but the outcome must say which one it was.
    last_errno: int | None = None
    while True:
        try:
            windows_locking.locking(fd, windows_locking.LK_NBLCK, 1)
            return
        except OSError as exc:
            if exc.errno not in _WINDOWS_LOCK_CONTENDED_ERRNOS:
                raise
            last_errno = exc.errno
            if time.monotonic() >= deadline:
                raise _advisory_lock_timeout("windows", last_errno) from exc
            time.sleep(ADVISORY_LOCK_POLL_SECONDS)
            # locking() leaves the file pointer where it found it, but a failed
            # attempt must still start from the same lock byte.
            os.lseek(fd, 0, os.SEEK_SET)


def lock_fd(fd: int, *, blocking: bool) -> None:
    """Acquire an exclusive advisory lock for an open file descriptor.

    The caller's file offset is preserved on every platform. On Windows the
    lock byte is addressed by seeking, so the offset is saved and restored
    around the operation; POSIX ``flock`` never moves it.
    """

    if os.name == "nt":
        import msvcrt

        windows_locking = cast(_MsvcrtLocking, msvcrt)
        # POSIX ``flock`` leaves the caller's file offset untouched. The Windows
        # primitive locks the byte range at the CURRENT position, so this branch
        # has to seek to zero -- and has to put the offset back, or ``lock_fd``
        # would silently rewind the caller's file on one platform only. Saving
        # and restoring it here makes the observable offset contract identical
        # on both platforms, which is the entire point of this module.
        saved_offset = os.lseek(fd, 0, os.SEEK_CUR)
        try:
            _lock_fd_windows(fd, windows_locking, blocking=blocking)
        finally:
            os.lseek(fd, saved_offset, os.SEEK_SET)
        return
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
    # Track the LAST contended errno so both platforms report the same shape.
    last_errno = None
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as exc:
            if exc.errno not in _POSIX_LOCK_CONTENDED_ERRNOS:
                raise
            last_errno = exc.errno
            if time.monotonic() >= deadline:
                raise _advisory_lock_timeout("posix", last_errno) from exc
            time.sleep(ADVISORY_LOCK_POLL_SECONDS)


def unlock_fd(fd: int) -> None:
    """Release a lock acquired by :func:`lock_fd`.

    Like :func:`lock_fd`, this leaves the caller's file offset exactly where it
    found it on every platform.
    """

    if os.name == "nt":
        import msvcrt

        windows_locking = cast(_MsvcrtLocking, msvcrt)
        saved_offset = os.lseek(fd, 0, os.SEEK_CUR)
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            windows_locking.locking(fd, windows_locking.LK_UNLCK, 1)
        finally:
            os.lseek(fd, saved_offset, os.SEEK_SET)
        return

    import fcntl

    fcntl.flock(fd, fcntl.LOCK_UN)
