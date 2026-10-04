"""Loaded first by every Python the Windows AppContainer validation lane runs.

Three CPython calls do not work inside an AppContainer; this module gives each
the host's result, only there.  The lane puts this directory first on
PYTHONPATH for Windows AppContainer children only
(``windows_appcontainer.APPCONTAINER_PYTHON_SITE``); nothing else imports it.
All measured in the validation container on Windows 11 26200, CPython 3.12.4.

``os.mkdir(path, 0o700)``: since 3.12.4 (CVE-2024-4030) it applies an
explicit, protected DACL -- SYSTEM, Administrators, OWNER RIGHTS -- that names
no AppContainer SID, so the container cannot use a directory it just made
(python/cpython#134587, open).  Inside the request's private exec scratch the
mkdir fails outright with WinError 5; in an ordinary granted directory it
succeeds and the next ``listdir`` is denied.  pytest makes its basetemp and
every ``tmp_path`` that way; so does ``tempfile.mkdtemp``.  Here ``0o700``
gets the pre-3.12.4 Windows behaviour: the mode is ignored and the directory
inherits its parent's DACL.  The container can only create directories inside
the request's own granted directories, and the exec scratch pytest's temp
lives in is itself owner-private plus this container's SID, so what a new
directory inherits there is just as private.  CPython ignores every other
mode on Windows already.

``nt._getfinalpathname``: ``GetFinalPathNameByHandleW(VOLUME_NAME_DOS)`` is
denied (WinError 5) for every path -- mapping the volume device to its drive
letter needs the mount manager, which a container cannot query, while the
``VOLUME_NAME_NT`` / ``VOLUME_NAME_NONE`` forms succeed.  So
``Path.resolve(strict=True)`` raised for every existing path.  On that denial
the path is rebuilt from the input's drive letter and the object's
``VOLUME_NAME_NONE`` name -- or, under the per-session drive of a sandbox
root, from that name's tail as long as the input's own path -- and returned
only if it names the very same file (same volume serial and file index);
otherwise the original error stands.

And ``os.stat`` / ``os.lstat`` of the directories above the request's own
directory: the container can stat none of them (WinError 5 on ``D:\\``,
``D:\\Dev`` ...), and no grant can open a drive root, so code that walks a
path from its drive root -- ``repository_state``'s symlink check -- refused
every path.  When such a call is denied, the answer is the host's own
``lstat`` of that exact directory, which the lane passes in
``AIWORKHUB_APPCONTAINER_ANCESTORS`` (``windows_appcontainer.
``ancestor_stat_facts``).  Only those directories, only on a denial.

Not a call but the environment: CreateProcess into the container replaced
TEMP and TMP with the adapter-shared ``...\AC\Temp`` and kept TMPDIR
(NF-2026-01341).  In a validation launch -- the scratch env is set -- both are
put back on TMPDIR, so every child this process starts inherits the request's
own scratch.
"""

import ctypes
import json
import ntpath
import os
from ctypes import wintypes

_mkdir = os.mkdir
_stat = os.stat
_lstat = os.lstat
_getfinalpathname_host = ntpath._getfinalpathname
_ANCESTORS_ENV = "AIWORKHUB_APPCONTAINER_ANCESTORS"
_SCRATCH_ENV = "AIWORKHUB_VALIDATION_EXEC_SCRATCH_ROOT"

_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_VOLUME_NAME_NONE = 0x4
_SHARE_ALL = 0x7
_INVALID_HANDLE = wintypes.HANDLE(-1).value

_kernel32 = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
_kernel32.CreateFileW.restype = wintypes.HANDLE
_kernel32.CreateFileW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
]
_kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
_kernel32.GetFinalPathNameByHandleW.argtypes = [
    wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
]
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


def _mkdir_inheriting(path, mode=0o777, *, dir_fd=None):
    return _mkdir(path, dir_fd=dir_fd)


def _volume_relative_name(path):
    """``path``'s final name without its volume (``\\Dev\\x``), or ``""``."""
    handle = _kernel32.CreateFileW(
        path, 0, _SHARE_ALL, None, _OPEN_EXISTING, _FILE_FLAG_BACKUP_SEMANTICS, None
    )
    if handle in (None, _INVALID_HANDLE):
        return ""
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        length = _kernel32.GetFinalPathNameByHandleW(
            handle, buffer, len(buffer), _VOLUME_NAME_NONE
        )
        return buffer.value if 0 < length < len(buffer) else ""
    finally:
        _kernel32.CloseHandle(handle)


def _getfinalpathname(path):
    try:
        return _getfinalpathname_host(path)
    except PermissionError:
        drive = ntpath.splitdrive(path)[0] if isinstance(path, str) else ""
        relative = _volume_relative_name(path) if len(drive) == 2 else ""
        candidates = ["\\\\?\\" + drive.upper() + relative] if relative else []
        # NF-2026-01027: a sandbox root is a per-session drive letter of its
        # own, and its real directories lead the volume-relative name. The
        # container may not query that mapping (QueryDosDeviceW: WinError 5),
        # so keep only the input's own length of the name's tail.
        tail = ntpath.splitdrive(ntpath.abspath(path))[1].rstrip("\\") if relative else ""
        if relative and relative.lower().endswith(tail.lower()):
            kept = relative[len(relative) - len(tail):] or "\\"
            candidates.append("\\\\?\\" + drive.upper() + kept)
        for candidate in candidates:
            try:
                if ntpath.samestat(_stat(candidate), _stat(path)):
                    return candidate
            except OSError:
                pass
        raise


def _ancestor_facts(raw):
    """``{normcased path: os.stat_result}`` from the lane's JSON."""
    facts = {}
    for key, fields in json.loads(raw or "{}").items():
        facts[key] = os.stat_result(
            fields[:10], {"st_file_attributes": fields[10], "st_reparse_tag": fields[11]}
        )
    return facts


def _brokered(real, facts):
    def call(path, *args, **kwargs):
        try:
            return real(path, *args, **kwargs)
        except PermissionError:
            try:
                key = ntpath.normcase(ntpath.abspath(os.fspath(path)))
            except TypeError:
                key = None
            if key not in facts or args or kwargs.get("dir_fd") is not None:
                raise
            return facts[key]

    return call


if __name__ == "sitecustomize":
    os.mkdir = _mkdir_inheriting
    ntpath._getfinalpathname = _getfinalpathname
    _facts = _ancestor_facts(os.environ.get(_ANCESTORS_ENV))
    os.stat = _brokered(_stat, _facts)
    os.lstat = _brokered(_lstat, _facts)
    if os.environ.get(_SCRATCH_ENV) and os.environ.get("TMPDIR"):
        os.environ["TEMP"] = os.environ["TMP"] = os.environ["TMPDIR"]
