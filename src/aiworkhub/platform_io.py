"""Small cross-platform filesystem primitives used by the MCP runtime.

Windows does not provide :mod:`fcntl` or ``os.fchmod``.  Keeping these
differences behind this module lets the rest of AIWorkHub use the same
repository-local locking contract on Linux, macOS, and Windows.
"""

from __future__ import annotations

import contextlib
import ctypes
import errno
import importlib
import json
import math
import ntpath
import os
import posixpath
import select
import signal
import stat
import subprocess
import sys
import time
import unicodedata
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, Protocol, TypedDict, cast

if TYPE_CHECKING:
    from .windows_appcontainer import AppContainerProbe
    from .windows_mxc import MxcReadiness


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

try:
    from .windows_file_structures import FILE_ID_INFO
except ImportError:  # direct-file loading used by platform regression tests
    from windows_file_structures import FILE_ID_INFO  # type: ignore[no-redef]


def _normalized_platform(platform_name: str | None = None) -> str:
    """Return the canonical ``windows``, ``linux`` or ``macos`` platform name."""

    if platform_name is None and os.name == "nt":
        name = "nt"
    else:
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


def current_user_uid(platform_name: str | None = None) -> int | None:
    """Return the current POSIX uid, or ``None`` on Windows.

    Windows ownership is expressed by ACLs rather than ``stat().st_uid`` and
    does not expose :func:`os.getuid`.  On every non-Windows platform this
    authority fails closed instead of letting callers silently skip an owner
    check when the runtime cannot provide a valid uid.
    """

    if is_windows(platform_name):
        return None
    getuid = getattr(os, "getuid", None)
    if not callable(getuid):
        raise OSError(errno.ENOSYS, "current_user_uid_unavailable")
    try:
        uid = int(getuid())
    except (OSError, TypeError, ValueError) as exc:
        raise OSError(errno.EIO, "current_user_uid_unavailable") from exc
    if uid < 0:
        raise OSError(errno.EIO, "current_user_uid_invalid")
    return uid


def stat_owned_by_current_user(
    metadata: os.stat_result, platform_name: str | None = None
) -> bool:
    """Apply the host ownership model to one already-opened file identity.

    Windows callers rely on their separately authenticated per-user ACL/path
    boundary.  POSIX callers require an exact uid match and fail closed when
    either the uid authority or stat metadata is unavailable.
    """

    if is_windows(platform_name):
        return True
    try:
        uid = current_user_uid(platform_name)
        owner_uid = int(metadata.st_uid)
    except (AttributeError, OSError, TypeError, ValueError):
        return False
    return uid is not None and owner_uid == uid


# Windows secret-file trust. POSIX answers "may only the owner read this key"
# with st_uid plus mode 0o600. Windows has neither: os.chmod(..., 0o600) reads
# back as 0o666, and st_uid is always 0. The equivalent question there is asked
# of the security descriptor on an ALREADY-OPEN handle -- never of a pathname,
# which a symlink or a swap can redirect between the check and the read.
_OWNER_SECURITY_INFORMATION = 0x00000001
_DACL_SECURITY_INFORMATION = 0x00000004
_SE_FILE_OBJECT = 1
_TOKEN_QUERY = 0x0008
_TOKEN_USER_CLASS = 1
_TOKEN_OWNER_CLASS = 4
_ACL_SIZE_INFORMATION = 2
_ACCESS_ALLOWED_ACE_TYPE = 0x00
_ACCESS_ALLOWED_OBJECT_ACE_TYPE = 0x05

# Principals whose presence in a key file's DACL means the secret is readable
# beyond its owner. SYSTEM and BUILTIN\Administrators are deliberately NOT in
# this set: they are inherited by essentially every file on a Windows host and
# already hold the privilege to read anything, so refusing them would refuse
# every legitimately created key rather than describe a real exposure.
WINDOWS_UNTRUSTED_SECRET_SIDS = frozenset(
    {
        "S-1-1-0",  # Everyone
        "S-1-5-7",  # Anonymous Logon
        "S-1-5-11",  # Authenticated Users
        "S-1-5-32-545",  # BUILTIN\\Users
        "S-1-5-32-546",  # BUILTIN\\Guests
        "S-1-5-113",  # Local account
        "S-1-5-114",  # Local account and member of Administrators group
    }
)


def _windows_sid_string(advapi32: Any, kernel32: Any, sid: Any) -> str:
    """Render one PSID as its canonical ``S-1-...`` string, or "" on failure."""

    convert = advapi32.ConvertSidToStringSidW
    convert.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p))
    convert.restype = ctypes.c_int
    out = ctypes.c_wchar_p()
    if not convert(sid, ctypes.byref(out)):
        return ""
    try:
        return str(out.value or "")
    finally:
        kernel32.LocalFree(ctypes.c_void_p(ctypes.cast(out, ctypes.c_void_p).value))


def _windows_token_sids(advapi32: Any, kernel32: Any) -> frozenset[str]:
    """The SIDs this process legitimately creates files as.

    Both the token USER and the token OWNER are accepted: an elevated session
    creates files owned by BUILTIN\\Administrators rather than by the user, so
    checking only the user SID would refuse the key the host itself just wrote.
    """

    get_current_process = kernel32.GetCurrentProcess
    get_current_process.restype = ctypes.c_void_p
    open_process_token = advapi32.OpenProcessToken
    open_process_token.argtypes = (
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_void_p),
    )
    open_process_token.restype = ctypes.c_int
    get_token_information = advapi32.GetTokenInformation
    get_token_information.argtypes = (
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
    )
    get_token_information.restype = ctypes.c_int

    token = ctypes.c_void_p()
    if not open_process_token(
        get_current_process(), _TOKEN_QUERY, ctypes.byref(token)
    ):
        return frozenset()
    try:
        sids: set[str] = set()
        for token_class in (_TOKEN_USER_CLASS, _TOKEN_OWNER_CLASS):
            needed = ctypes.c_uint32(0)
            get_token_information(token, token_class, None, 0, ctypes.byref(needed))
            if not needed.value:
                continue
            buffer = ctypes.create_string_buffer(needed.value)
            if not get_token_information(
                token, token_class, buffer, needed.value, ctypes.byref(needed)
            ):
                continue
            # TOKEN_USER and TOKEN_OWNER both begin with one PSID field.
            sid = ctypes.c_void_p.from_buffer(buffer)
            rendered = _windows_sid_string(advapi32, kernel32, sid)
            if rendered:
                sids.add(rendered)
        return frozenset(sids)
    finally:
        kernel32.CloseHandle(token)


class _AclSizeInformation(ctypes.Structure):
    _fields_ = (
        ("AceCount", ctypes.c_uint32),
        ("AclBytesInUse", ctypes.c_uint32),
        ("AclBytesFree", ctypes.c_uint32),
    )


class _AceHeader(ctypes.Structure):
    _fields_ = (
        ("AceType", ctypes.c_ubyte),
        ("AceFlags", ctypes.c_ubyte),
        ("AceSize", ctypes.c_uint16),
    )


class _AccessAllowedAce(ctypes.Structure):
    _fields_ = (
        ("Header", _AceHeader),
        ("Mask", ctypes.c_uint32),
        ("SidStart", ctypes.c_uint32),
    )


def windows_descriptor_secret_trust(fd: int) -> tuple[bool, str]:
    """Answer the Windows form of "only the owner can read this key".

    Reads the OWNER and the DACL from the security descriptor of THIS open
    descriptor's handle, so neither answer can be redirected by a pathname race.
    Returns ``(trusted, reason)`` and fails closed: every error path answers
    ``False`` with a named reason rather than defaulting to trusted.
    """

    if os.name != "nt":
        return False, "not_a_windows_host"
    try:
        import msvcrt

        handle = msvcrt.get_osfhandle(fd)
    except (ImportError, OSError, ValueError):
        return False, "handle_unavailable"
    try:
        win_dll = _windows_dll_loader()
        advapi32 = win_dll("advapi32", use_last_error=True)
        kernel32 = win_dll("kernel32", use_last_error=True)
        kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
        kernel32.LocalFree.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel32.CloseHandle.restype = ctypes.c_int

        get_security_info = advapi32.GetSecurityInfo
        get_security_info.argtypes = (
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
        )
        get_security_info.restype = ctypes.c_uint32

        owner = ctypes.c_void_p()
        dacl = ctypes.c_void_p()
        descriptor = ctypes.c_void_p()
        status = int(
            get_security_info(
                ctypes.c_void_p(handle),
                _SE_FILE_OBJECT,
                _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,
                ctypes.byref(owner),
                None,
                ctypes.byref(dacl),
                None,
                ctypes.byref(descriptor),
            )
        )
        if status != 0:
            return False, f"security_descriptor_unreadable:{status}"
        try:
            trusted_sids = _windows_token_sids(advapi32, kernel32)
            if not trusted_sids:
                return False, "process_token_unreadable"
            owner_sid = _windows_sid_string(advapi32, kernel32, owner)
            if not owner_sid:
                return False, "owner_unreadable"
            if owner_sid not in trusted_sids:
                return False, f"foreign_owner:{owner_sid}"
            if not dacl.value:
                # A NULL DACL grants everyone full control.
                return False, "null_dacl_grants_everyone"
            get_acl_information = advapi32.GetAclInformation
            get_acl_information.argtypes = (
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.c_int,
            )
            get_acl_information.restype = ctypes.c_int
            get_ace = advapi32.GetAce
            get_ace.argtypes = (
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.POINTER(ctypes.c_void_p),
            )
            get_ace.restype = ctypes.c_int

            size_info = _AclSizeInformation()
            if not get_acl_information(
                dacl,
                ctypes.byref(size_info),
                ctypes.sizeof(size_info),
                _ACL_SIZE_INFORMATION,
            ):
                return False, "dacl_unreadable"
            for index in range(size_info.AceCount):
                ace = ctypes.c_void_p()
                if not get_ace(dacl, index, ctypes.byref(ace)):
                    return False, f"ace_unreadable:{index}"
                header = _AceHeader.from_address(int(ace.value or 0))
                if header.AceType not in (
                    _ACCESS_ALLOWED_ACE_TYPE,
                    _ACCESS_ALLOWED_OBJECT_ACE_TYPE,
                ):
                    continue
                sid_address = int(ace.value or 0) + _AccessAllowedAce.SidStart.offset
                granted = _windows_sid_string(
                    advapi32, kernel32, ctypes.c_void_p(sid_address)
                )
                if not granted:
                    return False, f"ace_sid_unreadable:{index}"
                if granted in WINDOWS_UNTRUSTED_SECRET_SIDS:
                    return False, f"world_readable_ace:{granted}"
            return True, "owner_bound_dacl"
        finally:
            if descriptor.value:
                kernel32.LocalFree(descriptor)
    except (AttributeError, OSError, ValueError) as exc:
        return False, f"windows_security_api_unavailable:{exc}"


# Directory-privacy vocabulary. "Is this directory readable only by its owner"
# is a POSIX MODE question on POSIX and an ACL question on Windows; this module
# reads the first and does not yet read the second, so it must be able to say
# so rather than return a boolean it cannot justify.
DIRECTORY_PRIVACY_BACKEND_POSIX_MODE = "posix_mode_bits"
DIRECTORY_PRIVACY_BACKEND_NONE = "none"


def republish_standard_input_handle() -> bool:
    """Make the PROCESS std input handle agree with descriptor 0 again.

    ``os.dup2`` rebinds the C runtime's descriptor 0, but a child is handed the
    PROCESS-level ``STD_INPUT_HANDLE``. Windows is the only host where the two
    can disagree, and a child that inherits the stale one keeps whatever the
    descriptor no longer points at. Publishing the current handle keeps the two
    answers identical. Returns whether the handle was republished; every other
    host answers ``False`` because it has nothing to republish.
    """

    if not is_windows():
        return False
    try:
        import msvcrt

        kernel32 = _windows_dll_loader()("kernel32", use_last_error=True)
        kernel32.SetStdHandle.argtypes = (ctypes.c_uint32, ctypes.c_void_p)
        kernel32.SetStdHandle.restype = ctypes.c_int
        return bool(
            kernel32.SetStdHandle(
                ctypes.c_uint32(0xFFFFFFF6),  # STD_INPUT_HANDLE (-10) as DWORD
                ctypes.c_void_p(msvcrt.get_osfhandle(0)),
            )
        )
    except (AttributeError, ImportError, OSError, ValueError):
        return False


_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
_ACL_REVISION = 2
_FILE_ALL_ACCESS = 0x1F01FF


_GENERIC_WRITE_DAC = 0x00040000  # WRITE_DAC
_GENERIC_READ_CONTROL = 0x00020000  # READ_CONTROL
# Measured: SetSecurityInfo(..., DACL_SECURITY_INFORMATION, ...) on a handle
# opened with WRITE_DAC alone fails ACCESS_DENIED (5); it also reads the
# existing security descriptor as part of applying the new DACL, so the
# handle needs READ_CONTROL too. WRITE_DAC|READ_CONTROL together succeeded.
_DACL_WRITE_ACCESS = _GENERIC_WRITE_DAC | _GENERIC_READ_CONTROL
_FILE_SHARE_ALL = 0x00000007  # READ | WRITE | DELETE
_OPEN_EXISTING = 3


def windows_harden_owner_only_key_dacl(path: "Path | str") -> tuple[bool, str]:
    """Replace a key file's DACL with a protected, owner-only grant.

    NF-2026-00011 follow-up, found live: a key created BEFORE this module's
    read-side trust check existed inherits its parent directory's DACL, which
    on this host granted Authenticated Users (S-1-5-11) read access.
    ``windows_descriptor_secret_trust`` then correctly refuses it -- which is
    exactly right for a key someone else can read -- but nothing had ever
    hardened the WRITE side to stop producing one. Measured: deleting and
    recreating the key through the unpatched create path reproduced the
    identical refusal, so this is not a one-off bad file, it is every key this
    process creates under a permissive parent, forever, until the create path
    is fixed.

    Takes a PATH rather than an already-open descriptor and opens its own
    handle requesting ``WRITE_DAC | READ_CONTROL``: measured on this host, a
    CRT descriptor from ``os.open`` (``O_RDWR``/``O_WRONLY``) never carries
    those rights -- ``SetSecurityInfo`` on it failed ``ACCESS_DENIED`` even
    though this process owns the file, because Windows grants a right only
    when the handle's own open request asked for it. ``WRITE_DAC`` alone is
    also not enough: ``SetSecurityInfo`` reads the existing security
    descriptor as part of applying the new one, so it needs ``READ_CONTROL``
    too -- measured directly, a ``WRITE_DAC``-only handle still failed
    ``ACCESS_DENIED`` on this same call. The caller has already pinned the
    file's identity (no symlink, matching ``(st_dev, st_ino)``) immediately
    before calling this, on the SAME repo-local, non-adversarial path; this
    function does not repeat that check.

    Grants FILE_ALL_ACCESS to both the process token's USER SID and its OWNER
    SID -- an elevated session creates files owned by BUILTIN\\Administrators,
    and granting only one would lock the same account out of its own key
    across an elevation change -- and marks the DACL PROTECTED so it stops
    inheriting from the parent directory, closing the exact channel that
    caused this. Returns ``(ok, reason)`` and never raises; a caller that gets
    ``False`` must fail closed rather than keep the (unprotected, inherited)
    DACL the file already had.
    """

    if not is_windows():
        return False, "not_a_windows_host"
    try:
        win_dll = _windows_dll_loader()
        advapi32 = win_dll("advapi32", use_last_error=True)
        kernel32 = win_dll("kernel32", use_last_error=True)
        kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
        kernel32.LocalFree.restype = ctypes.c_void_p
        create_file = kernel32.CreateFileW
        create_file.argtypes = (
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
        )
        create_file.restype = ctypes.c_void_p
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (ctypes.c_void_p,)
        close_handle.restype = ctypes.c_int

        handle = create_file(
            str(path),
            _DACL_WRITE_ACCESS,
            _FILE_SHARE_ALL,
            None,
            _OPEN_EXISTING,
            0,
            None,
        )
        if not handle or handle == _INVALID_HANDLE_VALUE:
            return False, f"create_file_write_dac_failed:{ctypes.get_last_error()}"
        try:
            trusted_sids = _windows_token_sids(advapi32, kernel32)
            if not trusted_sids:
                return False, "process_token_unreadable"

            convert = advapi32.ConvertStringSidToSidW
            convert.argtypes = (ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_void_p))
            convert.restype = ctypes.c_int
            initialize_acl = advapi32.InitializeAcl
            initialize_acl.argtypes = (ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32)
            initialize_acl.restype = ctypes.c_int
            add_ace = advapi32.AddAccessAllowedAce
            add_ace.argtypes = (
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.c_void_p,
            )
            add_ace.restype = ctypes.c_int
            set_security_info = advapi32.SetSecurityInfo
            set_security_info.argtypes = (
                ctypes.c_void_p,
                ctypes.c_int,
                ctypes.c_uint32,
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_void_p,
            )
            set_security_info.restype = ctypes.c_uint32

            # 1024 bytes comfortably holds a handful of ACEs; InitializeAcl
            # takes the BUFFER capacity, not the bytes actually used, so
            # over-allocating is harmless and avoids hand-computing exact
            # ACE/header sizes.
            acl_buffer = ctypes.create_string_buffer(1024)
            if not initialize_acl(acl_buffer, len(acl_buffer), _ACL_REVISION):
                return False, f"initialize_acl_failed:{ctypes.get_last_error()}"

            psids: list[ctypes.c_void_p] = []
            try:
                for sid_string in sorted(trusted_sids):
                    psid = ctypes.c_void_p()
                    if not convert(sid_string, ctypes.byref(psid)):
                        return False, f"convert_sid_failed:{sid_string}"
                    psids.append(psid)
                    if not add_ace(acl_buffer, _ACL_REVISION, _FILE_ALL_ACCESS, psid):
                        return False, f"add_access_allowed_ace_failed:{ctypes.get_last_error()}"

                status = set_security_info(
                    ctypes.c_void_p(handle),
                    _SE_FILE_OBJECT,
                    _DACL_SECURITY_INFORMATION | _PROTECTED_DACL_SECURITY_INFORMATION,
                    None,
                    None,
                    acl_buffer,
                    None,
                )
                if status != 0:
                    return False, f"set_security_info_failed:{status}"
                return True, "owner_only_dacl_applied"
            finally:
                for psid in psids:
                    if psid.value:
                        kernel32.LocalFree(psid)
        finally:
            close_handle(ctypes.c_void_p(handle))
    except (AttributeError, OSError, ValueError) as exc:
        return False, f"windows_security_api_unavailable:{exc}"


def directory_privacy_backend(platform_name: str | None = None) -> str:
    """Name the primitive this host offers for proving directory privacy."""

    if is_windows(platform_name):
        return DIRECTORY_PRIVACY_BACKEND_NONE
    return DIRECTORY_PRIVACY_BACKEND_POSIX_MODE


def directory_is_private_to_current_user(
    metadata: os.stat_result, platform_name: str | None = None
) -> bool | None:
    """Is this directory closed to group and other?  ``None`` means unmeasured.

    On POSIX the mode bits answer directly: no group or other bit set.

    Windows returns ``None``, and the arithmetic is why.  Windows has no POSIX
    mode bits at all; Python synthesizes ``st_mode`` there from the read-only
    attribute alone, so a directory is ``0o777`` when writable and ``0o555``
    when read-only.  ``0o777 & 0o077 == 0o077`` and ``0o555 & 0o077 == 0o055``
    -- both non-zero -- so a mode test does not report "not private" on
    Windows, it reports NOTHING, and reports it as a failure for every
    directory on the host.  ``os.chmod`` cannot move the answer either: on
    Windows it only toggles the read-only attribute.

    ``None`` is deliberately not ``True``.  A caller that treats an unmeasured
    verdict as a pass hands out a directory nobody proved was private, which is
    worse than the hard failure it replaces; the contract is that ``None`` must
    be RECORDED as a reduced guarantee at the call site.
    """

    if directory_privacy_backend(platform_name) == DIRECTORY_PRIVACY_BACKEND_NONE:
        return None
    return not bool(stat.S_IMODE(metadata.st_mode) & 0o077)


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
    """Return the named Windows process-group flag through a typed boundary.

    ``subprocess.CREATE_NEW_PROCESS_GROUP`` only exists in the ``subprocess``
    module when Python itself is built for Windows, so on Linux/macOS
    ``getattr`` cannot see it. This function must describe what Windows
    WOULD use even when queried from a non-Windows host (``platform_name``
    lets a caller ask for the Windows answer while running elsewhere), so the
    fallback is the documented, stable literal (0x00000200) rather than 0.
    """

    return cast(
        int, getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
    )


def process_group_launch_kwargs(
    platform_name: str | None = None,
) -> ProcessGroupLaunchKwargs:
    """Return subprocess kwargs that create a separately signalable process group."""

    if is_windows(platform_name):
        return {"creationflags": _windows_creation_flag()}
    return {"start_new_session": True}


def windows_system_taskkill_path() -> str | None:
    """Resolve taskkill from the kernel-reported system directory, never PATH."""

    try:
        system_directory = ctypes.create_unicode_buffer(32768)
        length = ctypes.windll.kernel32.GetSystemDirectoryW(
            system_directory, len(system_directory)
        )
        if length <= 0 or length >= len(system_directory):
            return None
        return system_directory.value.rstrip("\\/") + "\\taskkill.exe"
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def linux_proc_identity(pid: int) -> dict[str, Any] | None:
    """Return Linux procfs identity without using a liveness-only PID probe."""

    if is_windows() or pid <= 0:
        return None
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        fields = raw[raw.rfind(")") + 2 :].split()
        if fields[0] == "Z":
            return None
        return {
            "pid": pid,
            "state": fields[0],
            "pgid": int(fields[2]),
            "session_id": int(fields[3]),
            "start_ticks": int(fields[19]),
        }
    except (OSError, ValueError, IndexError):
        return None


def cross_instance_process_identity_supported() -> bool:
    """Whether Linux procfs exposes the retained process identity contract."""

    return is_linux() and Path("/proc/self/stat").is_file()


def process_environment_contains(pid: int, marker: bytes) -> bool:
    try:
        return marker in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    except OSError:
        return False


def owned_process_group_members(owner_marker: bytes) -> list[dict[str, Any]]:
    """Find token-bearing procfs members with PID/start identity evidence."""

    members: list[dict[str, Any]] = []
    try:
        proc_entries = list(Path("/proc").iterdir())
    except OSError:
        return members
    for entry in proc_entries:
        if not entry.name.isdigit():
            continue
        identity = linux_proc_identity(int(entry.name))
        if identity is None:
            continue
        if process_environment_contains(int(entry.name), owner_marker):
            members.append(identity)
    return members


def signal_process_group(identity: int, *, graceful: bool) -> None:
    """Signal one POSIX process group through the platform authority."""

    if not _valid_process_identity(identity):
        raise ProcessLookupError(identity)
    posix_killpg = _resolve_posix_killpg(None)
    if posix_killpg is None:
        raise OSError(errno.ENOSYS, "process_group_signal_unavailable")
    posix_killpg(identity, signal.SIGTERM if graceful else _POSIX_SIGKILL)


def pipe_write_end_still_open(streams: object) -> bool:
    """Return whether any inherited pipe write-end may still be open."""

    if is_windows() or not hasattr(select, "poll"):
        return True
    poller = select.poll()
    registered = 0
    for stream in streams if isinstance(streams, tuple) else ():
        if stream is None:
            continue
        try:
            if stream.closed:
                continue
            fd = stream.fileno()
        except (ValueError, OSError):
            continue
        poller.register(fd, select.POLLIN)
        registered += 1
    if registered == 0:
        return False
    hangups = 0
    for _fd, event in poller.poll(0):
        if event & (select.POLLHUP | select.POLLERR | select.POLLNVAL):
            hangups += 1
    return hangups < registered


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


# Windows sandbox policy.  windows_mxc discovers the pinned MXC runtime and
# windows_appcontainer owns AppContainer capability and child-environment
# policy; this facade only decides whether the platform may use them and folds
# their answers into typed, fail-closed records.  Both are imported on first
# use, never at module import, so a host that never sandboxes on Windows keeps
# the import graph and behaviour it had before.
WINDOWS_SANDBOX_CAUSE_PLATFORM_NOT_WINDOWS = "platform_not_windows"
WINDOWS_SANDBOX_CAUSE_APPCONTAINER_UNAVAILABLE = "win32_appcontainer_unavailable"
WINDOWS_SANDBOX_CAUSE_MXC_NOT_READY = "mxc_runtime_not_ready"

_SANDBOX_PROBE_ERRORS = (
    ImportError,
    AttributeError,
    OSError,
    RuntimeError,
    TypeError,
    ValueError,
)


class WindowsSandboxReadiness(NamedTuple):
    """Fail-closed verdict on whether a Windows sandbox launch may start here.

    ``ready`` needs BOTH the AppContainer host probe and the pinned MXC runtime.
    ``causes`` names every reason it is not (empty when ready).  ``appcontainer``
    and ``mxc`` are the primitives' own evidence, ``None`` when that primitive
    was not consulted or could not run.
    """

    ready: bool
    causes: tuple[str, ...]
    appcontainer: AppContainerProbe | None
    mxc: MxcReadiness | None

    @property
    def runtime_path(self) -> Path | None:
        """The architecture-correct MXC executable, only once the whole stack is ready."""

        return self.mxc.runtime_path if self.ready and self.mxc is not None else None


class WindowsSandboxLaunchDecision(NamedTuple):
    """Typed answer to whether a launch may use the Windows sandbox, and with what.

    ``allowed`` guarantees ``executable`` is the resolved MXC runtime and
    ``environment`` is the child environment the AppContainer needs.  A denial
    carries no executable and hands back the caller's ``environment`` untouched.
    """

    allowed: bool
    causes: tuple[str, ...]
    executable: Path | None
    environment: Mapping[str, str] | None
    readiness: WindowsSandboxReadiness


def _appcontainer_module() -> Any:
    try:
        from . import windows_appcontainer
    except ImportError:  # direct-script entrypoint
        import windows_appcontainer  # type: ignore[no-redef]
    return windows_appcontainer


def _mxc_module() -> Any:
    try:
        from . import windows_mxc
    except ImportError:  # direct-script entrypoint
        import windows_mxc  # type: ignore[no-redef]
    return windows_mxc


def windows_sandbox_readiness(
    package_root: str | os.PathLike[str],
    *,
    platform_name: str | None = None,
    host_machine: str | None = None,
) -> WindowsSandboxReadiness:
    """Probe the Windows sandbox stack; anything short of fully ready is refused.

    Off Windows nothing is imported, called or read.  On Windows the AppContainer
    host probe and the pinned MXC runtime probe both run, and a primitive that
    cannot load or that raises counts as not ready.
    """

    if not is_windows(platform_name):
        return WindowsSandboxReadiness(
            False, (WINDOWS_SANDBOX_CAUSE_PLATFORM_NOT_WINDOWS,), None, None
        )
    try:
        appcontainer = _appcontainer_module().probe()
    except _SANDBOX_PROBE_ERRORS:
        appcontainer = None
    try:
        mxc = _mxc_module().probe_mxc_runtime(package_root, host_machine)
    except _SANDBOX_PROBE_ERRORS:
        mxc = None
    causes: list[str] = []
    if getattr(appcontainer, "available", False) is not True:
        causes.append(WINDOWS_SANDBOX_CAUSE_APPCONTAINER_UNAVAILABLE)
    if getattr(mxc, "ready", False) is not True or getattr(mxc, "wxc_path", None) is None:
        causes.append(WINDOWS_SANDBOX_CAUSE_MXC_NOT_READY)
    return WindowsSandboxReadiness(not causes, tuple(causes), appcontainer, mxc)


def windows_sandbox_environment(
    environment: Mapping[str, str] | None, *, platform_name: str | None = None
) -> Mapping[str, str] | None:
    """Return the child environment a sandboxed launch needs on this platform.

    Windows delegates to the AppContainer policy, which decides what the child
    block must carry; a failure there raises rather than falling back to the
    unadjusted environment.  Every other platform gets the caller's mapping back.
    """

    if not is_windows(platform_name):
        return environment
    return _appcontainer_module().appcontainer_child_environment(environment)


def windows_sandbox_launch_decision(
    package_root: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
    *,
    platform_name: str | None = None,
    host_machine: str | None = None,
) -> WindowsSandboxLaunchDecision:
    """Decide whether a launch may use the Windows sandbox, and with what.

    Allowed only when :func:`windows_sandbox_readiness` is ready.  The executable
    and the child environment are resolved solely in that case, so a denial can
    never leak either.
    """

    readiness = windows_sandbox_readiness(
        package_root, platform_name=platform_name, host_machine=host_machine
    )
    if not readiness.ready:
        return WindowsSandboxLaunchDecision(
            False, readiness.causes, None, environment, readiness
        )
    child_environment = windows_sandbox_environment(
        environment, platform_name=platform_name
    )
    return WindowsSandboxLaunchDecision(
        True, (), readiness.runtime_path, child_environment, readiness
    )


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
_FILE_CREATE = 0x2
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


class _FileDispositionInfo(ctypes.Structure):
    _fields_ = [("DeleteFile", ctypes.c_ubyte)]


class _FileDispositionInfoEx(ctypes.Structure):
    _fields_ = [("Flags", ctypes.c_uint32)]


_FILE_ATTRIBUTE_TAG_INFO = 9
_FILE_ID_INFO = 18
_FILE_DISPOSITION_INFO = 4
_FILE_DISPOSITION_INFO_EX = 21
# Native NtSetInformationFile FILE_INFORMATION_CLASS values (a different
# numbering from the FILE_INFO_BY_HANDLE_CLASS values just above, which are
# Win32's SetFileInformationByHandle enum and only used for disposition).
_FILE_RENAME_INFO = 10
_FILE_RENAME_INFO_EX = 65
_FILE_LINK_INFO = 11
_FILE_LINK_INFO_EX = 72
_FILE_DISPOSITION_FLAG_DELETE = 0x00000001
_FILE_DISPOSITION_FLAG_POSIX_SEMANTICS = 0x00000002
# CreateFileW-only flags (distinct namespace from the NtCreateFile
# CreateOptions constants above, though FILE_FLAG_OPEN_REPARSE_POINT
# happens to share _FILE_OPEN_REPARSE_POINT's bit value).
_WINDOWS_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
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

    def detach(self) -> int:
        """Transfer this HANDLE to another owner without closing it."""
        value = self.value
        self._value = None
        return value

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
    disposition: int = _FILE_OPEN,
) -> tuple[OwnedWindowsHandle, Any, Callable[[], int]]:
    """Open (or, with ``disposition=_FILE_CREATE``, exclusively create) one
    exact child segment beneath a borrowed parent HANDLE.

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
            disposition,
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
        if tag_info.ReparseTag != 0:
            # A distinct errno (matching POSIX ELOOP) lets every caller reuse
            # the same "refuse a symlink/junction without following it" branch
            # it already has for ``os.open(..., O_NOFOLLOW)``.
            raise OSError(errno.ELOOP, "opened child is a reparse point")
        if not tag_info.FileAttributes & _FILE_ATTRIBUTE_DIRECTORY:
            raise OSError(errno.ENOTDIR, "opened child is not a directory")
        file_id = FILE_ID_INFO()
        if not get_file_information(
            owned.value,
            _FILE_ID_INFO,
            ctypes.byref(file_id),
            ctypes.sizeof(file_id),
        ):
            raise _windows_error(get_last_error())
        if file_id.volume_serial_number == 0 or not any(file_id.file_id):
            raise OSError(errno.EINVAL, "opened child has no stable volume/file identity")
    except BaseException:
        owned.close()
        raise
    return owned


def open_windows_relative_regular_file_descriptor(parent_handle: int, child_name: str) -> int:
    """Open one non-reparse file beneath a pinned parent; caller owns the FD.

    NtCreateFile resolves exactly one child relative to the borrowed HANDLE.
    Converting that same HANDLE to a CRT descriptor never reopens its pathname.
    """
    owned, information, last_error = _open_windows_relative_child_handle(
        parent_handle, child_name,
        0x00100081,  # FILE_READ_DATA | FILE_READ_ATTRIBUTES | SYNCHRONIZE
        0x7,  # share READ | WRITE | DELETE; identity remains pinned by the HANDLE
        _FILE_SYNCHRONOUS_IO_NONALERT | _FILE_OPEN_REPARSE_POINT | 0x40,  # NON_DIRECTORY_FILE
    )
    try:
        attributes = _FileAttributeTagInfo()
        identity = FILE_ID_INFO()
        for info_class, value in ((_FILE_ATTRIBUTE_TAG_INFO, attributes), (_FILE_ID_INFO, identity)):
            if not information(owned.value, info_class, ctypes.byref(value), ctypes.sizeof(value)):
                raise _windows_error(last_error())
        if attributes.ReparseTag:
            # A distinct errno (matching POSIX ELOOP) lets every caller reuse
            # the same "refuse a symlink/junction without following it" branch
            # it already has for ``os.open(..., O_NOFOLLOW)``.
            raise OSError(errno.ELOOP, "opened child is a reparse point")
        if attributes.FileAttributes & (_FILE_ATTRIBUTE_DIRECTORY | _FILE_ATTRIBUTE_DEVICE | 0x400):
            raise OSError(errno.EISDIR, "opened child is not a regular file")
        if identity.volume_serial_number == 0 or not any(identity.file_id):
            raise OSError(errno.EINVAL, "opened child has no stable volume/file identity")
        import msvcrt

        descriptor = msvcrt.open_osfhandle(owned.value, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        owned.detach()  # the CRT descriptor now owns the original HANDLE
        return descriptor
    except BaseException:
        owned.close()
        raise


# NtCreateFile failures raise a bare ``OSError(winerror, ...)`` (see
# ``_open_windows_relative_child_handle``): a test pins that ``.errno`` stays
# the *raw* DOS error rather than the POSIX-mapped one, so a caller that wants
# a proper ``FileExistsError`` has to translate these specific codes itself.
_WINDOWS_ALREADY_EXISTS_ERRNOS = frozenset({80, 183})  # ERROR_FILE_EXISTS, ERROR_ALREADY_EXISTS


def create_windows_relative_regular_file_descriptor(parent_handle: int, child_name: str) -> int:
    """Exclusively create one new regular file beneath a borrowed parent HANDLE.

    Mirrors ``os.open(name, O_WRONLY | O_CREAT | O_EXCL, dir_fd=parent_fd)``:
    NtCreateFile's ``FILE_CREATE`` disposition fails if anything at all --  a
    regular file, a directory, or a symlink/reparse point -- already exists at
    that exact name, so this can never silently follow or replace an existing
    object the way a plain ``CREATE_ALWAYS`` open could. The returned CRT
    descriptor owns the HANDLE.
    """
    try:
        owned, _information, _last_error = _open_windows_relative_child_handle(
            parent_handle,
            child_name,
            0x00100182,  # FILE_WRITE_DATA | FILE_WRITE_ATTRIBUTES | FILE_READ_ATTRIBUTES | SYNCHRONIZE
            0x0,  # no sharing: this name is meant to be exclusively ours until closed
            _FILE_SYNCHRONOUS_IO_NONALERT | 0x40,  # NON_DIRECTORY_FILE
            disposition=_FILE_CREATE,
        )
    except OSError as exc:
        if exc.errno in _WINDOWS_ALREADY_EXISTS_ERRNOS:
            raise FileExistsError(errno.EEXIST, "opened child already exists") from exc
        raise
    try:
        import msvcrt

        descriptor = msvcrt.open_osfhandle(owned.value, os.O_WRONLY | getattr(os, "O_BINARY", 0))
        owned.detach()  # the CRT descriptor now owns the original HANDLE
        return descriptor
    except BaseException:
        owned.close()
        raise


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
            # A distinct errno (matching POSIX ELOOP) lets every caller reuse
            # the same "refuse a symlink/junction without following it" branch
            # it already has for ``os.open(..., O_NOFOLLOW)``.
            raise OSError(errno.ELOOP, "opened child is a reparse point")
        if tag_info.FileAttributes & _FILE_ATTRIBUTE_DEVICE:
            raise OSError(errno.EINVAL, "opened child has an unexpected device type")
        is_directory = bool(tag_info.FileAttributes & _FILE_ATTRIBUTE_DIRECTORY)
        file_id = FILE_ID_INFO()
        if not get_file_information(
            owned.value,
            _FILE_ID_INFO,
            ctypes.byref(file_id),
            ctypes.sizeof(file_id),
        ):
            raise _windows_error(get_last_error())
        if file_id.volume_serial_number == 0 or not any(file_id.file_id):
            raise OSError(errno.EINVAL, "opened child has no stable volume/file identity")
    except BaseException:
        owned.close()
        raise
    return WindowsRelativeChildAuthority(
        owned,
        is_directory=is_directory,
        volume_serial_number=int(file_id.volume_serial_number),
        file_id=bytes(file_id.file_id),
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


def open_windows_root_directory_handle(path: Path | str) -> OwnedWindowsHandle:
    """Open one absolute directory path as an authenticated, non-reparse HANDLE.

    Every other primitive above resolves a single path *segment* relative to an
    already-open parent HANDLE, but that chain has to start somewhere: from a
    pathname. ``CreateFileW`` with ``FILE_FLAG_OPEN_REPARSE_POINT`` opens the
    reparse point itself instead of following it -- exactly like
    ``NtCreateFile`` does for a relative child in
    :func:`_open_windows_relative_child_handle` -- and the same
    attribute/reparse-tag/volume-serial/FileId checks
    :func:`open_windows_relative_child_directory` applies to a child are applied
    here, so a symlinked or junctioned root is rejected exactly like a
    symlinked intermediate component would be. ``FILE_SHARE_DELETE`` is
    included (via ``_WINDOWS_DIRECTORY_SHARE_ACCESS``) so this open never blocks
    directory enumeration or cleanup elsewhere.
    """

    from ctypes import wintypes

    win_dll = _windows_dll_loader()
    kernel32 = win_dll("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (ctypes.c_void_p,)
    close_handle.restype = ctypes.c_int
    get_last_error = _windows_last_error_getter()

    handle_value = create_file(
        str(path),
        _WINDOWS_DIRECTORY_DESIRED_ACCESS,
        _WINDOWS_DIRECTORY_SHARE_ACCESS,
        None,
        _WINDOWS_OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if handle_value in (None, 0, _INVALID_HANDLE_VALUE):
        raise _windows_error(get_last_error())

    owned = OwnedWindowsHandle(int(handle_value), close_handle, get_last_error)
    try:
        get_file_information = kernel32.GetFileInformationByHandleEx
        get_file_information.argtypes = (
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
        )
        get_file_information.restype = ctypes.c_int
        tag_info = _FileAttributeTagInfo()
        if not get_file_information(
            owned.value,
            _FILE_ATTRIBUTE_TAG_INFO,
            ctypes.byref(tag_info),
            ctypes.sizeof(tag_info),
        ):
            raise _windows_error(get_last_error())
        if tag_info.ReparseTag != 0:
            raise OSError(errno.ELOOP, "root directory is a reparse point")
        if not tag_info.FileAttributes & _FILE_ATTRIBUTE_DIRECTORY:
            raise OSError(errno.ENOTDIR, "root path is not a directory")
        file_id = FILE_ID_INFO()
        if not get_file_information(
            owned.value,
            _FILE_ID_INFO,
            ctypes.byref(file_id),
            ctypes.sizeof(file_id),
        ):
            raise _windows_error(get_last_error())
        if file_id.volume_serial_number == 0 or not any(file_id.file_id):
            raise OSError(errno.EINVAL, "root directory has no stable volume/file identity")
    except BaseException:
        owned.close()
        raise
    return owned


class _FileRenameOrLinkInfoHeader(ctypes.Structure):
    """The layout ``FILE_RENAME_INFO(_EX)`` and ``FILE_LINK_INFO(_EX)`` share.

    All four structures are, physically, the same three fixed fields (a
    4-byte flags/boolean, a HANDLE, a length) followed by the inline
    ``WCHAR`` name -- only the *meaning* of the first field (a bare
    ``BOOLEAN ReplaceIfExists`` for the legacy classes, a ``ULONG Flags`` for
    the ``_Ex`` ones) differs, and a bit-0 "replace if exists" reads correctly
    either way. One header, reused for rename and hard-link creation alike.
    """

    _fields_ = [
        ("Flags", ctypes.c_uint32),
        ("RootDirectory", ctypes.c_void_p),
        ("FileNameLength", ctypes.c_uint32),
    ]


def _set_file_relative_name_information(
    handle_value: int,
    root_directory_handle: int,
    new_name: str,
    *,
    ex_info_class: int,
    legacy_info_class: int,
    replace_if_exists: bool,
    posix_semantics: bool = False,
) -> None:
    """Point one already-open, authenticated HANDLE at a new name/parent.

    ``handle_value`` is the exact child HANDLE authenticated by
    :func:`open_windows_relative_child_disposition` -- never a reopened
    pathname -- and ``root_directory_handle`` names the new parent directory by
    HANDLE, not by path, so the destination is exactly the borrowed parent the
    caller already authenticated. This goes through ``NtSetInformationFile``
    (ntdll) rather than the Win32 ``SetFileInformationByHandle`` wrapper: the
    Win32 wrapper's documented contract has never guaranteed honoring a
    non-NULL ``RootDirectory`` for a relative rename/link, while the native
    call -- the same one ``_open_windows_relative_child_handle`` already uses
    for relative opens -- is the documented way every other HANDLE-relative
    filesystem operation in Windows (Chromium, .NET, Sysinternals) implements
    a POSIX-style ``renameat``/``linkat``. The ``_Ex`` information class is
    tried first (it alone can request atomic replace-if-exists semantics); a
    pre-Windows-10 1709 filesystem rejects it with ``STATUS_INVALID_PARAMETER``
    -- the one documented "unsupported" signal, matching
    :func:`mark_windows_relative_child_disposition` -- at which point the
    legacy, boolean-only class is retried.
    """

    new_name, byte_length = _canonical_windows_child_segment(new_name)
    win_dll = _windows_dll_loader()
    ntdll = win_dll("ntdll", use_last_error=True)
    nt_set_information_file = ntdll.NtSetInformationFile
    nt_set_information_file.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(_IoStatusBlock),
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
    )
    nt_set_information_file.restype = ctypes.c_int32
    rtl_status_to_dos_error = ntdll.RtlNtStatusToDosError
    rtl_status_to_dos_error.argtypes = (ctypes.c_int32,)
    rtl_status_to_dos_error.restype = ctypes.c_uint32

    # Not ``ctypes.sizeof(_FileRenameOrLinkInfoHeader)``: ctypes pads a
    # standalone struct's overall size up to its largest member's alignment
    # (8, for the HANDLE), but the real ``FILE_RENAME_INFORMATION`` layout has
    # no such trailing pad -- ``FileName`` immediately follows
    # ``FileNameLength`` at its natural 4-byte-aligned offset. Using the padded
    # ``sizeof`` here inserted 4 bytes of garbage before the name on 64-bit and
    # the kernel rejected it as ``ERROR_INVALID_NAME``. The field's own
    # ``.offset`` is exact regardless of trailing struct padding.
    header_size = (
        _FileRenameOrLinkInfoHeader.FileNameLength.offset + ctypes.sizeof(ctypes.c_uint32)
    )
    name_buffer = ctypes.create_unicode_buffer(new_name)

    def build_buffer(flags: int) -> ctypes.Array:
        buffer = ctypes.create_string_buffer(header_size + byte_length + 2)
        header = ctypes.cast(buffer, ctypes.POINTER(_FileRenameOrLinkInfoHeader)).contents
        header.Flags = flags
        header.RootDirectory = root_directory_handle
        header.FileNameLength = byte_length
        ctypes.memmove(ctypes.byref(buffer, header_size), name_buffer, byte_length)
        return buffer

    def set_information(info_class: int, buffer: ctypes.Array) -> int:
        io_status = _IoStatusBlock()
        return int(
            nt_set_information_file(
                ctypes.c_void_p(handle_value),
                ctypes.byref(io_status),
                buffer,
                len(buffer),
                info_class,
            )
        )

    def raise_for_status(status: int) -> None:
        winerror = int(rtl_status_to_dos_error(status))
        if winerror in _WINDOWS_ALREADY_EXISTS_ERRNOS:
            raise FileExistsError(errno.EEXIST, f"{new_name!r} already exists")
        raise OSError(winerror, f"NtSetInformationFile failed for {new_name!r}")

    status = set_information(ex_info_class, build_buffer((0x1 if replace_if_exists else 0x0) | (0x2 if posix_semantics else 0x0)))
    if status >= 0:
        return
    winerror = int(rtl_status_to_dos_error(status))
    if winerror not in _WINDOWS_DISPOSITION_UNSUPPORTED_ERRNOS:
        raise_for_status(status)
    if posix_semantics:
        raise OSError(errno.ENOTSUP, "required POSIX rename semantics unavailable")
    status = set_information(legacy_info_class, build_buffer(1 if replace_if_exists else 0))
    if status < 0:
        raise_for_status(status)


def rename_windows_relative_child(
    src_parent_handle: int,
    old_name: str,
    dst_parent_handle: int,
    new_name: str,
    *,
    replace_if_exists: bool = False,
    posix_semantics: bool = False,
) -> None:
    """Rename one exact child from beneath one borrowed parent HANDLE to another.

    Mirrors ``os.replace(old, new, src_dir_fd=..., dst_dir_fd=...)``: the child
    named ``old_name`` beneath ``src_parent_handle`` is opened and authenticated
    (non-reparse, stable identity) by
    :func:`open_windows_relative_child_disposition` first, so a symlink swapped
    in under the old name is refused with the same ``ELOOP``-style errno every
    other primitive here uses, and the rename never re-resolves a pathname from
    scratch. ``src_parent_handle`` and ``dst_parent_handle`` may be the same
    HANDLE (a sibling rename, the shape this script always uses) or different
    ones.
    """

    authority = open_windows_relative_child_disposition(src_parent_handle, old_name)
    try:
        _set_file_relative_name_information(
            authority.handle.value,
            dst_parent_handle,
            new_name,
            ex_info_class=_FILE_RENAME_INFO_EX,
            legacy_info_class=_FILE_RENAME_INFO,
            replace_if_exists=replace_if_exists,
            posix_semantics=posix_semantics,
        )
    finally:
        authority.close()


def link_windows_relative_child(
    src_parent_handle: int,
    existing_name: str,
    dst_parent_handle: int,
    new_name: str,
) -> None:
    """Create a hard link ``new_name`` for ``existing_name``, HANDLE to HANDLE.

    Mirrors ``os.link(old, new, src_dir_fd=..., dst_dir_fd=...)`` and
    :func:`rename_windows_relative_child`'s shape: the existing child is opened
    and authenticated first, so a symlink swapped in under ``existing_name`` is
    refused rather than linked.
    """

    authority = open_windows_relative_child_disposition(src_parent_handle, existing_name)
    try:
        _set_file_relative_name_information(
            authority.handle.value,
            dst_parent_handle,
            new_name,
            ex_info_class=_FILE_LINK_INFO_EX,
            legacy_info_class=_FILE_LINK_INFO,
            replace_if_exists=False,
        )
    finally:
        authority.close()


def delete_windows_relative_child(parent_handle: int, child_name: str) -> None:
    """Authenticate and delete one exact child beneath a borrowed parent HANDLE.

    Composes :func:`open_windows_relative_child_disposition` and
    :func:`mark_windows_relative_child_disposition` -- the two-step shape every
    other disposition caller already uses -- into the one-call ``os.unlink``
    shape the sync script's cleanup paths want.
    """

    authority = open_windows_relative_child_disposition(parent_handle, child_name)
    try:
        mark_windows_relative_child_disposition(authority)
    finally:
        authority.close()


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


def windows_process_tree() -> dict[int, tuple[int, str]] | None:
    """One Windows PID -> (parent PID, image name) snapshot, or None.

    POSIX answers ancestry from ``/proc``; Windows needs a Toolhelp snapshot,
    which is exactly the kind of platform difference this module exists to own.
    A single snapshot answers parent AND image for every PID, so a caller
    walking an ancestry chain reads one consistent view of the process table
    instead of racing a second enumeration between hops. Fails closed: any
    error, and every non-Windows host, answers None rather than a partial tree.
    """

    if not is_windows():
        return None
    try:
        from ctypes import wintypes

        class _ProcessEntry32W(ctypes.Structure):
            _fields_ = (
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260),
            )

        kernel32 = _windows_dll_loader()("kernel32", use_last_error=True)
        create_snapshot = kernel32.CreateToolhelp32Snapshot
        create_snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
        create_snapshot.restype = wintypes.HANDLE
        process_first = kernel32.Process32FirstW
        process_first.argtypes = (wintypes.HANDLE, ctypes.POINTER(_ProcessEntry32W))
        process_first.restype = wintypes.BOOL
        process_next = kernel32.Process32NextW
        process_next.argtypes = (wintypes.HANDLE, ctypes.POINTER(_ProcessEntry32W))
        process_next.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL

        snapshot = create_snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
        if snapshot == wintypes.HANDLE(-1).value:
            return None
        tree: dict[int, tuple[int, str]] = {}
        try:
            entry = _ProcessEntry32W()
            entry.dwSize = ctypes.sizeof(entry)
            if not process_first(snapshot, ctypes.byref(entry)):
                return None
            while True:
                tree[int(entry.th32ProcessID)] = (
                    int(entry.th32ParentProcessID),
                    str(entry.szExeFile),
                )
                if not process_next(snapshot, ctypes.byref(entry)):
                    break
        finally:
            close_handle(snapshot)
        return tree
    except (AttributeError, OSError, TypeError, ValueError):
        return None


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


class PublicationDurabilityError(OSError):
    """The replacement committed, but its directory entry may not be durable."""

    replacement_committed = True
    published = True


class IdentityBoundPublicationError(OSError):
    """Publication could not be bound to the verified source-file identity."""

    replacement_committed = False
    published = False


_AT_EMPTY_PATH = 0x1000
_AT_FDCWD = -100
_AT_SYMLINK_FOLLOW = 0x400


def _link_open_file(
    source_fd: int, destination_directory_fd: int, destination_name: str
) -> None:
    """Create a hard link to an open Linux file without resolving its pathname."""

    try:
        libc = ctypes.CDLL(None, use_errno=True)
        linkat = libc.linkat
    except (AttributeError, OSError) as exc:
        raise IdentityBoundPublicationError(
            errno.ENOTSUP, "identity_bound_publication_not_supported"
        ) from exc
    linkat.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
    )
    linkat.restype = ctypes.c_int
    encoded_name = os.fsencode(destination_name)
    result = linkat(source_fd, b"", destination_directory_fd, encoded_name, _AT_EMPTY_PATH)
    if result and ctypes.get_errno() in {errno.ENOENT, errno.EPERM}:
        # AT_EMPTY_PATH can require CAP_DAC_READ_SEARCH. procfs exposes the
        # already-open descriptor as an unswappable kernel-owned symlink.
        descriptor_path = os.fsencode(f"/proc/self/fd/{source_fd}")
        result = linkat(
            _AT_FDCWD,
            descriptor_path,
            destination_directory_fd,
            encoded_name,
            _AT_SYMLINK_FOLLOW,
        )
    if result:
        error_number = ctypes.get_errno()
        if error_number in {
            errno.EXDEV,
            errno.EINVAL,
            errno.ENOENT,
            errno.ENOSYS,
            errno.ENOTSUP,
            errno.EPERM,
        }:
            raise IdentityBoundPublicationError(
                errno.ENOTSUP, "identity_bound_publication_not_supported"
            )
        raise OSError(error_number, os.strerror(error_number), destination_name)


def _same_regular_file_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        stat.S_ISREG(left.st_mode)
        and stat.S_ISREG(right.st_mode)
        and left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
    )


def _link_verified_source_into_staging(
    verified_identity: os.stat_result,
    source_directory_fd: int,
    source_name: str,
    staging_directory_fd: int,
    staging_name: str,
) -> None:
    """Stage the verified source by name, then re-prove its exact identity.

    macOS exposes neither ``AT_EMPTY_PATH`` nor a procfs descriptor path, so a
    staging hard link cannot be bound to the open descriptor the way Linux binds
    it. The link is instead created by name relative to the source directory
    descriptor and the linked object is re-stat'd and compared against the
    already-verified identity. The caller keeps the source descriptor open for
    the whole publication, so that identity pins the verified inode: a mismatch
    is a source substitution and fails closed with ``ESTALE`` before anything is
    published. A namespace operation this module cannot perform (cross-device
    link, unsupported ``linkat``) is reported as unsupported, never published.
    """

    try:
        os.link(
            source_name,
            staging_name,
            src_dir_fd=source_directory_fd,
            dst_dir_fd=staging_directory_fd,
            follow_symlinks=False,
        )
    except (NotImplementedError, TypeError) as exc:
        raise IdentityBoundPublicationError(
            errno.ENOTSUP, "identity_bound_publication_not_supported"
        ) from exc
    except OSError as exc:
        if exc.errno in {
            errno.EXDEV,
            errno.EINVAL,
            errno.EMLINK,
            errno.ENOENT,
            errno.ENOSYS,
            errno.ENOTSUP,
            errno.EPERM,
        }:
            raise IdentityBoundPublicationError(
                errno.ENOTSUP, "identity_bound_publication_not_supported"
            ) from exc
        raise
    staged_identity = os.stat(
        staging_name, dir_fd=staging_directory_fd, follow_symlinks=False
    )
    if not _same_regular_file_identity(verified_identity, staged_identity):
        raise IdentityBoundPublicationError(
            errno.ESTALE, "publication_source_identity_changed"
        )


def identity_bound_durable_atomic_replace(
    source: str | os.PathLike[str], destination: str | os.PathLike[str]
) -> None:
    """Durably publish the exact verified regular source-file identity.

    Linux binds a staging hard link directly to the open source descriptor.
    macOS has no descriptor-empty-path or procfs link, so it stages the source
    by name relative to the source directory descriptor and re-proves the linked
    object's identity against the still-open verified descriptor: that open
    descriptor pins the verified inode for the whole call, so a matching
    device/inode proves the exact verified file was staged and rejects any
    source substitution before publication.
    Destination authority linearizes to the parent directory object opened at
    call start: the descriptor-relative commit follows that object across a
    concurrent rename instead of resolving a substituted public parent path.
    Other platforms fail closed before changing the destination because this
    module cannot guarantee an equivalent descriptor-bound namespace operation.
    """

    source_path = os.fspath(source)
    destination_path = os.fspath(destination)
    if not (is_linux() or is_macos()):
        raise IdentityBoundPublicationError(
            errno.ENOTSUP, "identity_bound_publication_not_supported"
        )

    source_directory = os.path.dirname(os.path.abspath(source_path))
    destination_directory = os.path.dirname(os.path.abspath(destination_path))
    source_name = os.path.basename(source_path)
    destination_name = os.path.basename(destination_path)
    nonce = f"{os.getpid()}-{os.urandom(12).hex()}"
    staging_directory_name = f".{destination_name}.publish-{nonce}"
    staging_name = "source"
    quarantine_name = f".{source_name}.publish-{nonce}"

    source_fd = os.open(
        source_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    )
    source_directory_fd = -1
    destination_directory_fd = -1
    staging_directory_fd = -1
    staging_directory_created = False
    source_quarantined = False
    destination_committed = False
    try:
        verified_identity = os.fstat(source_fd)
        if not stat.S_ISREG(verified_identity.st_mode):
            raise IdentityBoundPublicationError(
                errno.EINVAL, "publication_source_is_not_a_regular_file"
            )
        source_directory_fd = os.open(
            source_directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        )
        destination_directory_fd = os.open(
            destination_directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        )
        try:
            os.mkdir(staging_directory_name, 0o700, dir_fd=destination_directory_fd)
            staging_directory_created = True
            staging_directory_fd = os.open(
                staging_directory_name,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
                dir_fd=destination_directory_fd,
            )
        except (NotImplementedError, TypeError) as exc:
            raise IdentityBoundPublicationError(
                errno.ENOTSUP, "identity_bound_publication_not_supported"
            ) from exc
        if is_linux():
            _link_open_file(source_fd, staging_directory_fd, staging_name)
        else:
            _link_verified_source_into_staging(
                verified_identity,
                source_directory_fd,
                source_name,
                staging_directory_fd,
                staging_name,
            )

        # Moving the pathname aside is intentionally followed by an fd-identity
        # check. A substitute is restored and can never reach the destination.
        os.replace(
            source_name,
            quarantine_name,
            src_dir_fd=source_directory_fd,
            dst_dir_fd=source_directory_fd,
        )
        source_quarantined = True
        if not _same_regular_file_identity(
            verified_identity,
            os.stat(
                quarantine_name,
                dir_fd=source_directory_fd,
                follow_symlinks=False,
            ),
        ):
            os.replace(
                quarantine_name,
                source_name,
                src_dir_fd=source_directory_fd,
                dst_dir_fd=source_directory_fd,
            )
            source_quarantined = False
            raise IdentityBoundPublicationError(
                errno.ESTALE, "publication_source_identity_changed"
            )

        os.fsync(source_fd)
        try:
            os.replace(
                staging_name,
                destination_name,
                src_dir_fd=staging_directory_fd,
                dst_dir_fd=destination_directory_fd,
            )
        except (NotImplementedError, TypeError) as exc:
            raise IdentityBoundPublicationError(
                errno.ENOTSUP, "identity_bound_publication_not_supported"
            ) from exc
        destination_committed = True
        os.unlink(quarantine_name, dir_fd=source_directory_fd)
        source_quarantined = False
        os.fsync(destination_directory_fd)
        if source_directory_fd != destination_directory_fd:
            os.fsync(source_directory_fd)
    except OSError as exc:
        if destination_committed and not isinstance(exc, PublicationDurabilityError):
            raise PublicationDurabilityError(
                exc.errno,
                f"publication_committed_but_sync_failed:{destination_path}",
                destination_path,
            ) from exc
        raise
    finally:
        if source_quarantined and not destination_committed:
            try:
                os.replace(
                    quarantine_name,
                    source_name,
                    src_dir_fd=source_directory_fd,
                    dst_dir_fd=source_directory_fd,
                )
            except OSError:
                pass
        if staging_directory_fd >= 0:
            try:
                os.unlink(staging_name, dir_fd=staging_directory_fd)
            except FileNotFoundError:
                pass
            os.close(staging_directory_fd)
        if staging_directory_created:
            try:
                os.rmdir(staging_directory_name, dir_fd=destination_directory_fd)
            except OSError:
                pass
        if destination_directory_fd >= 0:
            os.close(destination_directory_fd)
        if source_directory_fd >= 0:
            os.close(source_directory_fd)
        if source_fd >= 0:
            os.close(source_fd)


def durable_atomic_replace(
    source: str | os.PathLike[str], destination: str | os.PathLike[str]
) -> None:
    """Durably publish a completed file at ``destination`` where supported.

    The source contents reach stable storage before the atomic namespace
    change.  POSIX additionally syncs the parent directory after replacement;
    platforms which cannot open directories truthfully stop after the durable
    file sync and atomic replacement.
    """

    source_path = os.fspath(source)
    destination_path = os.fspath(destination)
    directory_descriptor = -1
    if os.name != "nt":
        directory_descriptor = os.open(
            os.path.dirname(os.path.abspath(destination_path)),
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
    try:
        # CPython's Windows ``os.fsync`` rejects a read-only CRT descriptor
        # with ``EBADF`` even though POSIX accepts it.  Publication candidates
        # are coordinator-created writable staging files, so open the Windows
        # descriptor read/write solely for the durability flush.
        source_flags = os.O_RDWR if os.name == "nt" else os.O_RDONLY
        descriptor = os.open(source_path, source_flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        atomic_replace(source_path, destination_path)
        if directory_descriptor >= 0:
            try:
                os.fsync(directory_descriptor)
            except OSError as exc:
                raise PublicationDurabilityError(
                    exc.errno,
                    f"publication_committed_but_directory_sync_failed:{destination_path}",
                    destination_path,
                ) from exc
    finally:
        if directory_descriptor >= 0:
            os.close(directory_descriptor)


class _MsvcrtLocking(Protocol):
    LK_NBLCK: int
    LK_UNLCK: int

    def locking(self, fd: int, mode: int, nbytes: int) -> None: ...


def advisory_lock_backend() -> str:
    """Name the cross-process advisory-lock primitive this host provides.

    ``"none"`` means the host offers no cross-process lock, so a caller can
    record that degradation instead of assuming a guarantee it does not have.
    """

    if os.name == "nt":
        return "msvcrt"
    return "flock"


# Directory-authority vocabulary. Naming what the host can actually provide is
# the same contract :func:`advisory_lock_backend` states one function above:
# ``"none"`` means the host offers no such primitive, so a caller records the
# degradation instead of assuming a guarantee it does not hold.
DIRECTORY_DESCRIPTOR_BACKEND_POSIX = "posix_directory_descriptor"
DIRECTORY_DESCRIPTOR_BACKEND_NONE = "none"


def directory_descriptor_backend(platform_name: str | None = None) -> str:
    """Name the primitive this host offers for pinning an open directory.

    Windows is ``"none"``, and every part of that is load-bearing: it defines
    neither ``O_DIRECTORY`` nor ``O_NOFOLLOW``, ``os.open`` on a directory
    raises ``PermissionError`` there, and ``os.supports_dir_fd`` is empty so a
    ``dir_fd=`` open was never reachable either.  A caller that assembles its
    own mask with ``hasattr(os, "O_DIRECTORY")`` therefore does not degrade
    gracefully -- it degrades to a bare ``O_RDONLY`` open of a directory, which
    fails on EVERY Windows attempt and can never succeed.

    ``platform_name`` selects the branch explicitly, so the Windows answer is
    observable from a POSIX host instead of being asserted untested.
    """

    if is_windows(platform_name):
        return DIRECTORY_DESCRIPTOR_BACKEND_NONE
    return DIRECTORY_DESCRIPTOR_BACKEND_POSIX


def nofollow_open_flag() -> int:
    """``O_NOFOLLOW`` where the host defines it, else ``0``.

    Read at call time and never captured at import, so a caller that removes
    the constant to exercise the degraded branch is observed by every reader.
    """

    return int(getattr(os, "O_NOFOLLOW", 0))


def directory_open_flags() -> int:
    """The ``O_`` mask for opening a directory without following a symlink."""

    return int(os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow_open_flag())


def lock_file_open_flags(*, nofollow: bool = False) -> int:
    """The ``O_`` mask for opening a lock file read/write.

    ``O_CLOEXEC`` is POSIX-only and ``O_BINARY`` is Windows-only; each resolves
    to 0 where the platform does not define it, and 0 is a no-op inside the
    mask, so one call serves every platform. CI caught the inline version of
    this on Windows: ``os.O_CLOEXEC`` simply does not exist there.  ``O_BINARY``
    matters as much: without it Windows opens the descriptor in text mode and
    rewrites ``\\n`` on the way out, so a lock file's own bytes differ by host.

    ``nofollow`` adds ``O_NOFOLLOW`` for a caller that must refuse a symlinked
    lock path outright rather than detect the substitution afterwards.
    """

    return int(
        os.O_CREAT
        | os.O_RDWR
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_BINARY", 0)
        | (nofollow_open_flag() if nofollow else 0)
    )


def open_directory_descriptor(
    path: Path | str, platform_name: str | None = None
) -> int | None:
    """Open a pinned, non-following descriptor on one existing directory.

    Returns ``None`` -- never a descriptor built from a silently degraded flag
    mask -- where :func:`directory_descriptor_backend` reports ``"none"``, so
    the caller chooses an honest weaker check instead of issuing an open that
    cannot succeed on that host.
    """

    if (
        directory_descriptor_backend(platform_name)
        == DIRECTORY_DESCRIPTOR_BACKEND_NONE
    ):
        return None
    return os.open(str(path), directory_open_flags())


def close_directory_descriptor(descriptor: int | None) -> None:
    """Close what :func:`open_directory_descriptor` returned, ``None`` included."""

    if descriptor is not None:
        os.close(descriptor)


def open_lock_file(path: Path) -> int:
    """Open (creating if needed) a lock file and return its descriptor.

    The flag set is platform knowledge and belongs here, not at a call site;
    :func:`lock_file_open_flags` is the one place that mask is written.

    The descriptor is opened for WRITING because :func:`lock_fd` needs a
    writable fd on Windows -- ``msvcrt.locking`` locks a byte range that has to
    exist, and the read-only descriptor POSIX would accept fails with EBADF.

    On Windows this goes through :func:`_open_windows_lock_file` instead of
    ``os.open``: a caller (``db_writer.py``) deliberately keeps this
    descriptor open for the life of the process to save a syscall pair per
    write, and the CRT open ``os.open`` performs has no way to ask for
    ``FILE_SHARE_DELETE`` -- so any later ``os.unlink``/``shutil.rmtree`` of
    this exact path, from this or another process, is refused for as long as
    the process runs. Only a native ``CreateFileW`` call can request that
    share bit; the CRT's ``_wsopen_s`` (what ``os.open`` uses under the hood)
    does not expose it at all.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    if is_windows():
        return _open_windows_lock_file(path)
    return os.open(str(path), lock_file_open_flags(), 0o600)


def _open_windows_lock_file(path: Path) -> int:
    """``open_lock_file``'s Windows path: a writable fd shared for delete too."""

    from ctypes import wintypes

    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    FILE_SHARE_READ_WRITE_DELETE = 0x7
    OPEN_ALWAYS = 4
    FILE_ATTRIBUTE_NORMAL = 0x80

    kernel32 = _windows_dll_loader()("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE

    handle = create_file(
        str(path),
        GENERIC_READ | GENERIC_WRITE,
        FILE_SHARE_READ_WRITE_DELETE,
        None,
        OPEN_ALWAYS,
        FILE_ATTRIBUTE_NORMAL,
        None,
    )
    if handle in (None, 0, _INVALID_HANDLE_VALUE):
        raise _windows_error(ctypes.get_last_error())
    import msvcrt

    return msvcrt.open_osfhandle(handle, os.O_RDWR | getattr(os, "O_BINARY", 0))


def open_readonly_shared(path: Path, flags: int, *, nofollow: bool = False) -> int:
    """Open an existing file for reading without ever blocking its deletion.

    POSIX ``os.open`` never stands in the way of an ``unlink``, so ``flags``
    pass straight through there.  On Windows the CRT open behind ``os.open``
    shares only READ | WRITE, so while any such handle is open a concurrent
    ``os.unlink`` of the same path fails with a sharing violation.  A reader of
    a claim ticket must never veto the lock holder that retires it: measured,
    one settler's unlocked read of a reviewer terminal intent made the winner's
    retire fail silently, and a queued settler then settled the same intent a
    second time.  So Windows opens through ``CreateFileW`` with
    FILE_SHARE_DELETE, exactly as :func:`open_lock_file` does; the descriptor
    reads the same bytes in the same CRT mode ``os.open(path, O_RDONLY)`` gave.
    """

    if not is_windows():
        if nofollow:
            nofollow_flag = getattr(os, "O_NOFOLLOW", 0)
            if not nofollow_flag:
                raise OSError(errno.ENOTSUP, "nofollow read unavailable")
            flags |= nofollow_flag
        return os.open(str(path), flags)
    from ctypes import wintypes

    GENERIC_READ = 0x80000000
    kernel32 = _windows_dll_loader()("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        str(path), GENERIC_READ, _FILE_SHARE_ALL, None, _OPEN_EXISTING,
        _FILE_FLAG_OPEN_REPARSE_POINT if nofollow else 0, None
    )
    if handle in (None, 0, _INVALID_HANDLE_VALUE):
        raise _windows_error(ctypes.get_last_error())
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    owned = OwnedWindowsHandle(handle, close_handle, ctypes.get_last_error)
    try:
        if nofollow:
            information = kernel32.GetFileInformationByHandleEx
            information.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
            information.restype = wintypes.BOOL
            attributes = _FileAttributeTagInfo()
            if not information(handle, _FILE_ATTRIBUTE_TAG_INFO, ctypes.byref(attributes), ctypes.sizeof(attributes)):
                raise _windows_error(ctypes.get_last_error())
            if attributes.ReparseTag or attributes.FileAttributes & 0x400:
                raise OSError(errno.ELOOP, "opened file is a reparse point")
            if attributes.FileAttributes & (_FILE_ATTRIBUTE_DIRECTORY | _FILE_ATTRIBUTE_DEVICE):
                raise OSError(errno.EISDIR, "opened file is not regular")
        import msvcrt

        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | (getattr(os, "O_BINARY", 0) if nofollow else 0))
        owned.detach()
        return descriptor
    except BaseException:
        owned.close()
        raise


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


class ReparsePointRefused(OSError):
    """A path held by :func:`pinned_paths` is, or passes through, a link."""


@contextlib.contextmanager
def pinned_paths(paths: "list[Path]") -> "Iterator[None]":
    """Hold ``paths`` -- ancestors first -- so that, until the block exits,
    none of them is a reparse point and none can be renamed, deleted or
    swapped for one (NF-2026-00034).

    A host process that edits a tree a sandboxed worker can write must not
    have a path redirected under it: a worker with modify rights can replace
    ``src\\pkg`` with a junction to anywhere between a check and a write.  On
    Windows each path is opened with FILE_FLAG_OPEN_REPARSE_POINT (so a link
    is seen, not followed), refused if it carries the reparse attribute, and
    held open WITHOUT FILE_SHARE_DELETE -- so no other process can open it for
    the DELETE that renaming or removing it takes.  Entries can still be
    created inside a held directory.  Because each path is held before the
    next one is opened, a path used inside the block resolves only through
    held, verified directories.  POSIX: an lstat check per path; there the
    worker's own sandbox is what keeps the tree honest.
    """

    if not is_windows():
        for path in paths:
            if stat.S_ISLNK(os.lstat(path).st_mode):
                raise ReparsePointRefused(errno.ELOOP, "symlink refused", str(path))
        yield
        return
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
    kernel32.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, wintypes.LPVOID]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    invalid = wintypes.HANDLE(-1).value
    handles: list[Any] = []
    try:
        for path in paths:
            handle = kernel32.CreateFileW(
                str(path),
                0x0001 | 0x0080,  # FILE_READ_DATA (joins share checks) | FILE_READ_ATTRIBUTES
                0x0001 | 0x0002,  # FILE_SHARE_READ | FILE_SHARE_WRITE, never DELETE
                None,
                3,  # OPEN_EXISTING
                0x02000000 | 0x00200000,  # BACKUP_SEMANTICS | OPEN_REPARSE_POINT
                None,
            )
            if not handle or handle == invalid:
                error = ctypes.get_last_error()  # type: ignore[attr-defined]
                raise OSError(errno.EACCES, f"cannot hold path (winerror {error})", str(path))
            handles.append(handle)
            info = (wintypes.DWORD * 13)()  # BY_HANDLE_FILE_INFORMATION
            if not kernel32.GetFileInformationByHandle(handle, info):
                raise OSError(errno.EIO, "cannot read path attributes", str(path))
            if info[0] & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise ReparsePointRefused(errno.ELOOP, "reparse point refused", str(path))
        yield
    finally:
        for handle in reversed(handles):
            kernel32.CloseHandle(handle)


# ---------------------------------------------------------------------------
# AIWORKHUB_SANDBOX_SLOT_SECTION -- the repo-local worker sandbox model.
#
# Every byte a worker writes -- home, temp, cache, state, config, workspace and
# logs -- lives under <repo>/.aiworkhub/runtime/sandboxes/slots/<slot-id>.  No
# path below is derived from the user profile, the local app-data root, the
# interpreter's own home or temp defaults, the system drive or a hardcoded
# drive letter: the repository may live on any volume, and a request whose
# containment cannot be proved does not start rather than falling back.  No
# elevation, no drive-letter mapping and no ACL is written here -- the Windows
# AppContainer backend is a separate authority.  Holder liveness goes through
# process_is_alive, which on Windows asks the kernel for the exit code and
# never touches the process.
# ---------------------------------------------------------------------------

# The seven storage conventions a worker can reach, in the order they are
# created and cleaned.  Card 01173B/C consume this tuple, so its order and
# spelling are part of the contract.
SANDBOX_SLOT_DIRS: tuple[str, ...] = (
    "home",
    "temp",
    "state",
    "cache",
    "config",
    "workspace",
    "logs",
)

SANDBOX_REASON_SLOTS_EXHAUSTED = "sandbox_slots_exhausted"
SANDBOX_REASON_PATH_ESCAPES_ROOT = "sandbox_path_escapes_root"
SANDBOX_REASON_REPARSE_POINT = "sandbox_reparse_point"
SANDBOX_REASON_UNC_PATH = "sandbox_unc_path"
SANDBOX_REASON_CASE_ALIAS = "sandbox_case_alias"
SANDBOX_REASON_LEASE_CONFLICT = "sandbox_lease_conflict"

# The whole typed refusal vocabulary.  A caller branches on these codes, never
# on a message, and there is no seventh "it probably worked" outcome.
SANDBOX_REASONS: tuple[str, ...] = (
    SANDBOX_REASON_SLOTS_EXHAUSTED,
    SANDBOX_REASON_PATH_ESCAPES_ROOT,
    SANDBOX_REASON_REPARSE_POINT,
    SANDBOX_REASON_UNC_PATH,
    SANDBOX_REASON_CASE_ALIAS,
    SANDBOX_REASON_LEASE_CONFLICT,
)

# Relative, so the model is volume-agnostic: the tree is always resolved from
# the repository root the caller supplies.
_SANDBOX_TREE_RELATIVE = (".aiworkhub", "runtime", "sandboxes")
_SANDBOX_XDG_DATA_RELATIVE = (".local", "share")

# The slot tree is owner-only.  Windows ignores POSIX mode bits (see
# chmod_path, which is a no-op there) and this module writes no ACL, so on
# Windows containment is carried by the path checks below, not by a mode.
_SANDBOX_DIR_MODE = 0o700
_SANDBOX_LEASE_MODE = 0o600

_SANDBOX_REPARSE_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_SANDBOX_WIN32_EXTENDED_PREFIX = "\\\\?\\"


class SandboxUnavailable(RuntimeError):
    """No sandbox slot could be handed out, with the typed reason why.

    ``reason`` is one of :data:`SANDBOX_REASONS` and is the only thing callers
    branch on.  Raising is the whole contract: there is no unsandboxed or
    unverified path to fall back to, because a worker that escapes its slot
    writes into the repository the slot exists to protect.
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


def _sandbox_normalized_key(path: str | os.PathLike[str]) -> str:
    """One comparison key for containment: case-folded by host semantics.

    ``os.path.normcase`` is the host's own answer to whether two spellings name
    one path, which is what containment must be measured with.  A Windows
    extended-length prefix is dropped first so a long resolved child and its
    shorter resolved root stay comparable; anything the strip does not
    recognise keeps its prefix and therefore fails the containment test.
    """

    text = os.fspath(path)
    if text.startswith(_SANDBOX_WIN32_EXTENDED_PREFIX) and not text.startswith(
        _SANDBOX_WIN32_EXTENDED_PREFIX + "UNC\\"
    ):
        text = text[len(_SANDBOX_WIN32_EXTENDED_PREFIX) :]
    return os.path.normcase(text)


def _sandbox_resolved_key(path: str | os.PathLike[str]) -> str:
    """Resolve ``path`` strictly and return its containment key.

    Strict resolution is the point: a path that does not exist, or whose
    resolution fails, has not been proved contained and so is refused.
    """

    try:
        return _sandbox_normalized_key(os.path.realpath(path, strict=True))
    except OSError as exc:
        raise SandboxUnavailable(SANDBOX_REASON_PATH_ESCAPES_ROOT, os.fspath(path)) from exc


def _sandbox_assert_strictly_inside(path: str | os.PathLike[str], root_key: str) -> None:
    """Refuse ``path`` unless it resolves strictly inside ``root_key``."""

    if not _sandbox_resolved_key(path).startswith(root_key + os.sep):
        raise SandboxUnavailable(SANDBOX_REASON_PATH_ESCAPES_ROOT, os.fspath(path))


def _sandbox_assert_reparse_free(path: Path) -> None:
    """Refuse a symlink, junction or any other reparse point at ``path``.

    ``lstat`` answers about the link itself, never its target, which is the
    only way to see a junction planted where the sandbox is about to write or
    delete.  POSIX reports a symlink through ``S_ISLNK``; Windows does NOT set
    ``S_IFLNK`` for a junction but does set FILE_ATTRIBUTE_REPARSE_POINT in
    ``st_file_attributes``, so both answers are consulted.  A path that does
    not exist yet is not a reparse point and is left to the caller.
    """

    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise SandboxUnavailable(SANDBOX_REASON_REPARSE_POINT, str(path)) from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise SandboxUnavailable(SANDBOX_REASON_REPARSE_POINT, str(path))
    if getattr(metadata, "st_file_attributes", 0) & _SANDBOX_REPARSE_ATTRIBUTE:
        raise SandboxUnavailable(SANDBOX_REASON_REPARSE_POINT, str(path))


def _sandbox_reject_device_or_unc(text: str) -> None:
    """Refuse a UNC share or a ``\\\\?\\`` / ``\\\\.\\`` device-prefixed root.

    Neither spelling can anchor this containment model: a UNC root has no
    volume to compare against, and a device prefix suppresses exactly the
    normalisation every later comparison assumes.  Forward slashes are folded
    first so ``//server/share`` is refused the same way ``\\\\server\\share``
    is, on every platform -- a Windows-only refusal would leave the POSIX path
    accepting a root it cannot contain either.
    """

    if text.replace("/", "\\").startswith("\\\\"):
        raise SandboxUnavailable(SANDBOX_REASON_UNC_PATH, text)


def _sandbox_reject_case_alias(path: Path) -> None:
    """Refuse a spelling that only case-folds onto the real directory entry.

    A case-insensitive volume accepts ``...\\REPO`` for a directory stored as
    ``repo``: every later normcase comparison then passes while the audit trail
    names a path that was never created.  The real entry is the only accepted
    spelling, and the check reads the parent's entries rather than asking the
    host whether it folds case -- so a case-sensitive host refuses the same
    alias a case-insensitive one would silently accept.
    """

    name = path.name
    parent = path.parent
    if not name or parent == path:
        return
    try:
        entries = os.listdir(parent)
    except OSError:
        return
    if name in entries:
        return
    folded = name.casefold()
    if any(entry.casefold() == folded for entry in entries):
        raise SandboxUnavailable(SANDBOX_REASON_CASE_ALIAS, str(path))


def _sandbox_repo_root(repo_root: str | os.PathLike[str]) -> Path:
    """Canonicalise the repository root the whole slot tree hangs off.

    Every refusal here happens before anything is created, so a rejected root
    leaves the target directory exactly as it was.  Resolving the root first is
    also what makes the reparse checks below meaningful on a host whose temp or
    home tree is itself reached through a link.
    """

    text = os.fspath(repo_root)
    if not text:
        raise SandboxUnavailable(SANDBOX_REASON_PATH_ESCAPES_ROOT, "repo_root_not_configured")
    _sandbox_reject_device_or_unc(text)
    absolute = Path(os.path.abspath(text))
    _sandbox_reject_device_or_unc(str(absolute))
    _sandbox_reject_case_alias(absolute)
    resolved = Path(os.path.realpath(absolute, strict=False))
    _sandbox_reject_device_or_unc(str(resolved))
    if not os.path.isdir(resolved):
        raise SandboxUnavailable(SANDBOX_REASON_PATH_ESCAPES_ROOT, str(resolved))
    return resolved


def sandbox_root(repo_root: str | os.PathLike[str]) -> Path:
    """The one repo-local root every worker sandbox slot lives under.

    ``realpath(repo_root)/.aiworkhub/runtime/sandboxes`` -- repository-relative
    on purpose, so the answer is the same on any volume and never borrows the
    user profile or the system drive.
    """

    return _sandbox_repo_root(repo_root).joinpath(*_SANDBOX_TREE_RELATIVE)


def sandbox_slot_count(cpu_count: int | None = None) -> int:
    """Derive how many slots this host may lease, from its observed core count.

    The count is the observed cores minus one, so the interactive MCP server
    keeps a core of its own while every slot is leased; a single-core host
    still gets one slot rather than none.  The only input is the measurement
    (``os.cpu_count()`` when the caller supplies nothing) -- there is no pinned
    worker constant here, because the right number is a property of the machine
    and cannot be decided in advance.
    """

    observed = os.cpu_count() if cpu_count is None else cpu_count
    cores = max(1, int(observed or 1))
    return max(1, cores - 1)


def _sandbox_slot_id(index: int) -> str:
    """The on-disk name of one slot: stable, sortable, and never a path."""

    return f"slot-{index:02d}"


@dataclass(frozen=True)
class SandboxPlan:
    """Where one leased worker may write, and nowhere else.

    The seven storage paths and every value :meth:`environment` exports resolve
    strictly inside ``slot_root``; ``repo_root``, ``sandbox_root`` and
    ``slot_root`` are the anchors that containment is measured against.  The
    record is frozen so a launcher cannot retarget a path after containment was
    proved for it.
    """

    repo_root: Path
    sandbox_root: Path
    slot_id: str
    slot_root: Path
    home: Path
    temp: Path
    state: Path
    cache: Path
    config: Path
    workspace: Path
    logs: Path
    request_id: str

    @property
    def xdg_data_home(self) -> Path:
        """``home/.local/share`` -- the XDG data root, still inside the slot."""

        return self.home.joinpath(*_SANDBOX_XDG_DATA_RELATIVE)

    def environment(self) -> dict[str, str]:
        """The child environment that keeps every storage convention in-slot.

        POSIX tools read HOME and the XDG_* variables, Windows tools read TEMP,
        TMP and USERPROFILE, and portable ones read TMPDIR; all of them are
        answered from this slot, so a worker never writes to the real user
        profile, the real %TEMP% or the system drive.  USERPROFILE is exported
        on Windows only: inventing it on POSIX would hand tools a variable the
        host does not have.
        """

        values = {
            "HOME": str(self.home),
            "TEMP": str(self.temp),
            "TMP": str(self.temp),
            "TMPDIR": str(self.temp),
            "XDG_CONFIG_HOME": str(self.config),
            "XDG_CACHE_HOME": str(self.cache),
            "XDG_STATE_HOME": str(self.state),
            "XDG_DATA_HOME": str(self.xdg_data_home),
        }
        if is_windows():
            values["USERPROFILE"] = str(self.home)
        return values


def _sandbox_make_owner_only_dir(path: Path) -> None:
    """Create one sandbox-owned directory, owner-only, never through a link.

    The reparse check runs before AND after creation: before, because
    ``exist_ok=True`` would happily accept a junction someone planted at the
    path; after, because the entry we now hold is the one we are about to write
    and delete inside.
    """

    _sandbox_assert_reparse_free(path)
    os.makedirs(path, mode=_SANDBOX_DIR_MODE, exist_ok=True)
    _sandbox_assert_reparse_free(path)
    chmod_path(path, _SANDBOX_DIR_MODE)


def _sandbox_remove_entry(path: Path, slot_key: str) -> None:
    """Remove one entry that is proved to live strictly inside the slot."""

    _sandbox_assert_reparse_free(path)
    _sandbox_assert_strictly_inside(path, slot_key)
    if os.path.isdir(path):
        _sandbox_clear_children(path, slot_key)
        os.rmdir(path)
    else:
        os.unlink(path)


def _sandbox_clear_children(path: Path, slot_key: str) -> None:
    """Empty ``path`` in place, keeping ``path`` itself.

    Nothing outside the slot is reachable from here: ``path`` must resolve at
    or inside ``slot_key``, every child must resolve strictly inside it, and a
    reparse point refuses the whole wipe instead of being followed.  Following
    one is exactly how a junction planted in a scratch tree turns a scratch
    wipe into a wipe of the canonical ``.aiworkhub`` config, databases or
    worktrees it points at.
    """

    key = _sandbox_resolved_key(path)
    if key != slot_key and not key.startswith(slot_key + os.sep):
        raise SandboxUnavailable(SANDBOX_REASON_PATH_ESCAPES_ROOT, str(path))
    with os.scandir(path) as entries:
        children = [Path(entry.path) for entry in entries]
    for child in children:
        _sandbox_remove_entry(child, slot_key)


def _sandbox_empty_slot_dirs(plan: SandboxPlan) -> None:
    """Empty the seven slot directories, keeping the directories themselves."""

    slot_key = _sandbox_resolved_key(plan.slot_root)
    for name in SANDBOX_SLOT_DIRS:
        directory = plan.slot_root / name
        if not os.path.lexists(directory):
            continue
        _sandbox_assert_reparse_free(directory)
        _sandbox_assert_strictly_inside(directory, slot_key)
        _sandbox_clear_children(directory, slot_key)


def _sandbox_prepare_slot(repo: Path, root: Path, slot_id: str, request_id: str) -> SandboxPlan:
    """Wipe one slot, rebuild it, and prove every plan path lives inside it.

    The wipe is what makes a crashed, timed-out or cancelled predecessor
    harmless: its residue is removed on the next lease of the same slot rather
    than leaking into the next request.
    """

    slots_key = _sandbox_resolved_key(root / "slots")
    slot_root = root / "slots" / slot_id
    _sandbox_make_owner_only_dir(slot_root)
    slot_key = _sandbox_resolved_key(slot_root)
    if not slot_key.startswith(slots_key + os.sep):
        raise SandboxUnavailable(SANDBOX_REASON_PATH_ESCAPES_ROOT, str(slot_root))
    _sandbox_clear_children(slot_root, slot_key)
    directories = {name: slot_root / name for name in SANDBOX_SLOT_DIRS}
    for directory in directories.values():
        _sandbox_make_owner_only_dir(directory)
    # XDG_DATA_HOME is conventionally home/.local/share, so each level is
    # created explicitly rather than left to makedirs: an intermediate created
    # as a side effect would keep the process umask instead of 0o700.
    xdg_data = directories["home"]
    for part in _SANDBOX_XDG_DATA_RELATIVE:
        xdg_data = xdg_data / part
        _sandbox_make_owner_only_dir(xdg_data)
    for directory in (*directories.values(), xdg_data):
        _sandbox_assert_strictly_inside(directory, slot_key)
    plan = SandboxPlan(
        repo_root=repo,
        sandbox_root=root,
        slot_id=slot_id,
        slot_root=slot_root,
        request_id=request_id,
        **directories,
    )
    for value in plan.environment().values():
        _sandbox_assert_strictly_inside(value, slot_key)
    return plan


def _sandbox_lease_record(request_id: str, slot_id: str) -> bytes:
    """The bytes one lease file holds: who claimed the slot, and from where."""

    return json.dumps(
        {
            "claimed_at_epoch": time.time(),
            "pid": os.getpid(),
            "request_id": request_id,
            "slot_id": slot_id,
        },
        sort_keys=True,
    ).encode("utf-8")


def _sandbox_lease_holder(lease_path: Path) -> tuple[str, int] | None:
    """The (request id, holder pid) a lease names, or None when it names nobody.

    ``None`` means the file exists but does not prove who holds it -- foreign,
    truncated or unreadable.  That is deliberately not the same answer as "the
    holder is dead": see :func:`_sandbox_lease_is_reclaimable`.
    """

    try:
        raw = lease_path.read_bytes()
    except OSError:
        return None
    try:
        record = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    pid = record.get("pid")
    request_id = record.get("request_id")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    if not isinstance(request_id, str):
        return None
    return request_id, pid


def _sandbox_lease_is_reclaimable(lease_path: Path) -> bool:
    """Whether a lease may be removed: only once its holder is proven dead.

    A lease that has already vanished is reclaimable -- there is nothing left
    holding the slot.  A lease that names no usable pid is NOT: it proves
    nothing, and proving nothing must never be the reason two live requests are
    handed one slot.  Liveness is :func:`process_is_alive`, which reads the
    Windows exit code instead of raising anything at the process, and which
    answers ALIVE for every ambiguity so a running holder is never evicted.
    """

    if not os.path.lexists(lease_path):
        return True
    holder = _sandbox_lease_holder(lease_path)
    if holder is None:
        return False
    return not process_is_alive(holder[1])


def _sandbox_try_claim(lease_path: Path, request_id: str, slot_id: str) -> bool:
    """Claim one slot's lease file with an exclusive create, or report it held.

    The exclusive create IS the mutual exclusion for an UNHELD slot: two
    callers -- threads or processes -- cannot both create the same path.  It is
    not the mutual exclusion for reclaiming a dead holder, because that is a
    check followed by an unlink: a second claimer that had already read the
    same dead lease would unlink the LIVE lease the first claimer just created,
    and one slot would be handed to two live requests -- the second of which
    then wipes the first one's workspace.  So the check, the unlink and the
    exclusive create all run under one advisory lock per slot, taken on a
    sibling ``<slot_id>.claim`` file.  That file is never deleted: the OS lock
    dies with its holder, so it cannot go stale, while unlinking it would let
    two claimers lock two different inodes under one name and reopen the race.

    A lease whose holder is provably dead is removed and the create retried
    exactly once; a lease held by a live process, or one that names nobody, is
    left untouched and the caller moves on.
    """

    claim_path = lease_path.with_name(f"{slot_id}.claim")
    _sandbox_assert_reparse_free(claim_path)
    guard = os.open(claim_path, lock_file_open_flags(nofollow=True), _SANDBOX_LEASE_MODE)
    try:
        try:
            lock_fd(guard, blocking=True)
        except AdvisoryLockTimeout as exc:
            raise SandboxUnavailable(SANDBOX_REASON_LEASE_CONFLICT, str(claim_path)) from exc
        try:
            for attempt in (0, 1):
                try:
                    descriptor = os.open(
                        lease_path,
                        os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0),
                        _SANDBOX_LEASE_MODE,
                    )
                except FileExistsError:
                    if attempt or not _sandbox_lease_is_reclaimable(lease_path):
                        return False
                    try:
                        os.unlink(lease_path)
                    except FileNotFoundError:
                        pass
                    except OSError:
                        return False
                    continue
                try:
                    os.write(descriptor, _sandbox_lease_record(request_id, slot_id))
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                if _sandbox_lease_holder(lease_path) != (request_id, os.getpid()):
                    raise SandboxUnavailable(SANDBOX_REASON_LEASE_CONFLICT, str(lease_path))
                return True
            return False
        finally:
            unlock_fd(guard)
    finally:
        os.close(guard)


class SandboxSlotLease:
    """One live claim on one slot: the plan it hands out, and its release.

    The lease file is the claim, so releasing it is what frees the slot.
    ``release`` empties the seven directories and then removes that file even
    if the wipe failed, because a lease nobody can release is a slot nobody can
    ever use again.
    """

    def __init__(self, plan: SandboxPlan, lease_path: Path) -> None:
        self.plan = plan
        self.lease_path = lease_path
        self._released = False

    @property
    def released(self) -> bool:
        return self._released

    def release(self) -> None:
        """Empty the seven slot directories and drop the claim, exactly once."""

        if self._released:
            return
        self._released = True
        try:
            _sandbox_empty_slot_dirs(self.plan)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(self.lease_path)

    def __enter__(self) -> SandboxSlotLease:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.release()


def lease_sandbox_slot(
    repo_root: str | os.PathLike[str],
    request_id: str,
    *,
    slot_count: int | None = None,
) -> SandboxSlotLease:
    """Lease one isolated, cleaned, repo-local slot for one request.

    One live request per slot: the claim is an exclusive-create lease file at
    ``sandbox_root/leases/<slot_id>.lease`` naming the request and the holder
    pid.  The slot is wiped and rebuilt before the plan is returned, so a
    predecessor that crashed, timed out or was cancelled leaves no residue, and
    slots are mutually isolated because no path in one resolves inside another.

    Every containment failure raises :class:`SandboxUnavailable` with a typed
    reason and changes nothing outside the slot; there is no unsandboxed or
    unverified path to fall back to.
    """

    identity = str(request_id)
    if not identity:
        raise SandboxUnavailable(SANDBOX_REASON_LEASE_CONFLICT, "request_id_not_configured")
    repo = _sandbox_repo_root(repo_root)
    root = repo.joinpath(*_SANDBOX_TREE_RELATIVE)
    # The ancestors above sandbox_root are canonical repository data: they are
    # verified against a planted link and created if absent, but never
    # re-permissioned and never cleaned.
    ancestor = repo
    for part in _SANDBOX_TREE_RELATIVE[:-1]:
        ancestor = ancestor / part
        _sandbox_assert_reparse_free(ancestor)
        os.makedirs(ancestor, exist_ok=True)
        _sandbox_assert_reparse_free(ancestor)
    leases_root = root / "leases"
    for directory in (root, root / "slots", leases_root):
        _sandbox_make_owner_only_dir(directory)
    count = sandbox_slot_count() if slot_count is None else max(1, int(slot_count))
    for index in range(count):
        slot_id = _sandbox_slot_id(index)
        lease_path = leases_root / f"{slot_id}.lease"
        if not _sandbox_try_claim(lease_path, identity, slot_id):
            continue
        try:
            plan = _sandbox_prepare_slot(repo, root, slot_id, identity)
        except BaseException:
            # The claim is ours and the slot is unusable: drop it rather than
            # leave a lease that blocks the slot for the rest of the host's life.
            with contextlib.suppress(FileNotFoundError):
                os.unlink(lease_path)
            raise
        return SandboxSlotLease(plan, lease_path)
    raise SandboxUnavailable(SANDBOX_REASON_SLOTS_EXHAUSTED, f"{count}_slots_all_held")
