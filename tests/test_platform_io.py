from __future__ import annotations

import ast
import dataclasses
import errno
import io
import importlib.util
import json
import os
import stat
import tempfile
import threading
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiworkhub import _platform_process, platform_io, windows_appcontainer, windows_mxc


def test_current_user_uid_never_probes_posix_uid_on_windows(monkeypatch):
    monkeypatch.setattr(
        platform_io.os,
        "getuid",
        lambda: pytest.fail("Windows must not probe os.getuid"),
        raising=False,
    )

    assert platform_io.current_user_uid("windows") is None
    assert platform_io.stat_owned_by_current_user(
        SimpleNamespace(st_uid=123), "windows"
    )


def test_current_user_uid_fails_closed_when_posix_authority_is_missing(monkeypatch):
    monkeypatch.delattr(platform_io.os, "getuid", raising=False)

    with pytest.raises(OSError, match="current_user_uid_unavailable"):
        platform_io.current_user_uid("linux")
    assert not platform_io.stat_owned_by_current_user(
        SimpleNamespace(st_uid=123), "linux"
    )


def test_stat_owned_by_current_user_requires_exact_posix_uid(monkeypatch):
    monkeypatch.setattr(platform_io.os, "getuid", lambda: 123, raising=False)

    assert platform_io.current_user_uid("linux") == 123
    assert platform_io.stat_owned_by_current_user(
        SimpleNamespace(st_uid=123), "linux"
    )
    assert not platform_io.stat_owned_by_current_user(
        SimpleNamespace(st_uid=124), "linux"
    )


def test_available_memory_bytes_reads_linux_memavailable(monkeypatch):
    monkeypatch.setattr(
        "builtins.open",
        lambda *_args, **_kwargs: io.StringIO(
            "MemTotal:       8192 kB\nMemAvailable:   4096 kB\n"
        ),
    )
    assert platform_io.available_memory_bytes("linux") == 4096 * 1024


def test_available_memory_bytes_fails_closed_for_invalid_linux_value(monkeypatch):
    monkeypatch.setattr(
        "builtins.open",
        lambda *_args, **_kwargs: io.StringIO("MemAvailable: unknown kB\n"),
    )
    assert platform_io.available_memory_bytes("linux") is None


def test_available_memory_bytes_uses_posix_sysconf(monkeypatch):
    values = {"SC_AVPHYS_PAGES": 100, "SC_PAGE_SIZE": 4096}
    monkeypatch.setattr(
        platform_io.os, "sysconf", values.__getitem__, raising=False
    )
    assert platform_io.available_memory_bytes("freebsd") == 409_600


def test_available_memory_bytes_reads_windows_status(monkeypatch):
    def global_memory_status(status_pointer):
        status_pointer._obj.available_physical = 123_456
        return 1

    monkeypatch.setattr(
        platform_io.ctypes,
        "windll",
        SimpleNamespace(
            kernel32=SimpleNamespace(GlobalMemoryStatusEx=global_memory_status)
        ),
        raising=False,
    )
    assert platform_io.available_memory_bytes("windows") == 123_456


class _FakeWindowsFunction:
    def __init__(self, function):
        self.function = function
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self.function(*args)


class _FakeWindowsLibraries:
    def __init__(
        self,
        *,
        status=0,
        dos_error=5,
        attributes=0x10,
        reparse_tag=0,
        volume=7,
        file_id=bytes(range(1, 17)),
        disposition_results=(1,),
    ):
        self.calls = []
        self.closes = []
        self.close_results = [1]
        self.last_error = 5
        self.disposition_calls = []
        self.disposition_results = list(disposition_results)
        self.nt_statuses = []
        self.information_calls = []

        def nt_create(*args):
            self.calls.append(args)
            args[0]._obj.value = 0x1_0000_1234
            return status

        def rtl_status_to_dos_error(status_arg):
            self.nt_statuses.append(status_arg)
            return dos_error

        def information(handle, info_class, output, size):
            assert handle == 0x1_0000_1234
            self.information_calls.append((info_class, type(output._obj), size))
            if info_class == 9:
                value = output._obj
                value.FileAttributes = attributes
                value.ReparseTag = reparse_tag
            else:
                assert info_class == 18
                value = output._obj
                value.volume_serial_number = volume
                value.file_id[:] = file_id
            return 1

        def set_file_information(handle, info_class, output, size):
            value = output._obj
            self.disposition_calls.append(
                (
                    getattr(handle, "value", handle),
                    info_class,
                    value.Flags if info_class == 21 else value.DeleteFile,
                )
            )
            return self.disposition_results.pop(0)

        def close(handle):
            self.closes.append(handle.value)
            return self.close_results.pop(0)

        self.ntdll = SimpleNamespace(
            NtCreateFile=_FakeWindowsFunction(nt_create),
            RtlNtStatusToDosError=_FakeWindowsFunction(rtl_status_to_dos_error),
        )
        self.kernel32 = SimpleNamespace(
            GetFileInformationByHandleEx=_FakeWindowsFunction(information),
            SetFileInformationByHandle=_FakeWindowsFunction(set_file_information),
            CloseHandle=_FakeWindowsFunction(close),
        )

    def windll(self, name, **_kwargs):
        return self.ntdll if name == "ntdll" else self.kernel32

    def get_last_error(self):
        return self.last_error


def test_canonical_segment_rejects_ambiguous_windows_names():
    rejected = [
        "",
        ".",
        "..",
        "a/b",
        "a\\b",
        "a\x00b",
        "tail.",
        "tail ",
        "CON",
        "nul.txt",
        "COM¹",
        "COM²",
        "COM³",
        "LPT¹",
        "LPT²",
        "LPT³",
        "com¹.txt",
        "LPT².log",
        "e\u0301",
        "bad:name",
        "bad*name",
        "bad?name",
        "bad<name",
        "bad>name",
        'bad"name',
        "bad|name",
        "bad\x01name",
        "bad\x1fname",
    ]
    for child_name in rejected:
        with pytest.raises(ValueError):
            platform_io._canonical_windows_child_segment(child_name)
    with pytest.raises(ValueError):
        platform_io._canonical_windows_child_segment("x" * 32767)
    assert platform_io._canonical_windows_child_segment("é") == ("é", 2)


@pytest.mark.parametrize("parent", [0, platform_io._INVALID_HANDLE_VALUE])
@pytest.mark.parametrize(
    "opener",
    [
        platform_io.open_windows_relative_child_directory,
        platform_io.open_windows_relative_child_disposition,
        platform_io.open_windows_relative_regular_file_descriptor,
    ],
)
def test_relative_child_rejects_invalid_parent_before_native_call(monkeypatch, parent, opener):
    monkeypatch.setattr(
        platform_io.ctypes,
        "WinDLL",
        lambda *_args, **_kwargs: pytest.fail("native call"),
        raising=False,
    )
    with pytest.raises(ValueError):
        opener(parent, "child")


@pytest.mark.parametrize(
    "child_name",
    [
        "COM¹",
        "COM²",
        "COM³",
        "LPT¹",
        "LPT²",
        "LPT³",
        "com¹.txt",
        "COM².log",
        "lpt³.bak",
    ],
)
@pytest.mark.parametrize(
    "opener",
    [
        platform_io.open_windows_relative_child_directory,
        platform_io.open_windows_relative_child_disposition,
        platform_io.open_windows_relative_regular_file_descriptor,
    ],
)
def test_reserved_superscript_names_no_native_call(monkeypatch, child_name, opener):
    monkeypatch.setattr(
        platform_io.ctypes,
        "WinDLL",
        lambda *_args, **_kwargs: pytest.fail("native call"),
        raising=False,
    )
    with pytest.raises(ValueError):
        opener(0x1_0000_0009, child_name)


def test_ntcreatefile_directory_child_preserves_exact_abi_and_authority(monkeypatch):
    fake = _FakeWindowsLibraries()
    monkeypatch.setattr(platform_io.ctypes, "WinDLL", fake.windll, raising=False)
    monkeypatch.setattr(
        platform_io.ctypes,
        "get_last_error",
        fake.get_last_error,
        raising=False,
    )
    owned = platform_io.open_windows_relative_child_directory(0x1_0000_0009, "child")
    assert owned.value == 0x1_0000_1234
    assert len(fake.ntdll.NtCreateFile.argtypes) == 11
    args = fake.calls[0]
    object_attributes = args[2]._obj
    assert object_attributes.RootDirectory == 0x1_0000_0009
    assert object_attributes.ObjectName.contents.Length == 10
    assert object_attributes.ObjectName.contents.MaximumLength == 12
    assert args[1:2] == (0x00100081,)
    assert args[5:9] == (0, 0x7, 0x1, 0x200021)
    assert fake.information_calls == [
        (9, platform_io._FileAttributeTagInfo, 8),
        (18, platform_io.FILE_ID_INFO, 24),
    ]
    owned.close()
    owned.close()
    assert fake.closes == [0x1_0000_1234]


def test_ntcreatefile_disposition_child_preserves_exact_abi_and_authority(monkeypatch):
    fake = _FakeWindowsLibraries()
    monkeypatch.setattr(platform_io.ctypes, "WinDLL", fake.windll, raising=False)
    monkeypatch.setattr(
        platform_io.ctypes,
        "get_last_error",
        fake.get_last_error,
        raising=False,
    )
    authority = platform_io.open_windows_relative_child_disposition(
        0x1_0000_0009, "child"
    )
    assert authority.handle.value == 0x1_0000_1234
    assert len(fake.ntdll.NtCreateFile.argtypes) == 11
    args = fake.calls[0]
    object_attributes = args[2]._obj
    assert object_attributes.RootDirectory == 0x1_0000_0009
    assert object_attributes.ObjectName.contents.Length == 10
    assert object_attributes.ObjectName.contents.MaximumLength == 12
    assert args[1:2] == (0x00110080,)
    assert args[5:9] == (0, 0x3, 0x1, 0x200020)
    assert fake.information_calls == [
        (9, platform_io._FileAttributeTagInfo, 8),
        (18, platform_io.FILE_ID_INFO, 24),
    ]
    authority.close()
    authority.close()
    assert fake.closes == [0x1_0000_1234]


@pytest.mark.parametrize(
    ("attributes", "tag", "volume", "file_id"),
    [
        (0, 0, 7, b"1" * 16),
        (0x10, 1, 7, b"1" * 16),
        (0x10, 0, 0, b"1" * 16),
        (0x10, 0, 7, b"\0" * 16),
    ],
)
def test_handle_validation_closes_every_rejected_child(
    monkeypatch,
    attributes,
    tag,
    volume,
    file_id,
):
    fake = _FakeWindowsLibraries(
        attributes=attributes,
        reparse_tag=tag,
        volume=volume,
        file_id=file_id,
    )
    monkeypatch.setattr(platform_io.ctypes, "WinDLL", fake.windll, raising=False)
    monkeypatch.setattr(
        platform_io.ctypes,
        "get_last_error",
        fake.get_last_error,
        raising=False,
    )
    with pytest.raises(OSError):
        platform_io.open_windows_relative_child_directory(99, "child")
    assert fake.closes == [0x1_0000_1234]


@pytest.mark.parametrize(
    ("attributes", "tag", "volume", "file_id"),
    [
        (0, 1, 7, b"1" * 16),
        (0x10, 1, 7, b"1" * 16),
        (0x40, 0, 7, b"1" * 16),
        (0, 0, 0, b"1" * 16),
        (0x10, 0, 0, b"1" * 16),
        (0, 0, 7, b"\0" * 16),
        (0x10, 0, 7, b"\0" * 16),
    ],
)
def test_disposition_handle_validation_closes_every_rejected_child(
    monkeypatch,
    attributes,
    tag,
    volume,
    file_id,
):
    fake = _FakeWindowsLibraries(
        attributes=attributes,
        reparse_tag=tag,
        volume=volume,
        file_id=file_id,
    )
    monkeypatch.setattr(platform_io.ctypes, "WinDLL", fake.windll, raising=False)
    monkeypatch.setattr(
        platform_io.ctypes,
        "get_last_error",
        fake.get_last_error,
        raising=False,
    )
    with pytest.raises(OSError):
        platform_io.open_windows_relative_child_disposition(99, "child")
    assert fake.closes == [0x1_0000_1234]


@pytest.mark.parametrize(
    ("attributes", "expected_is_directory"),
    [(0, False), (0x10, True)],
)
def test_disposition_opener_authenticates_file_and_directory_type(
    monkeypatch,
    attributes,
    expected_is_directory,
):
    fake = _FakeWindowsLibraries(
        attributes=attributes,
        reparse_tag=0,
        volume=7,
        file_id=b"1" * 16,
    )
    monkeypatch.setattr(platform_io.ctypes, "WinDLL", fake.windll, raising=False)
    monkeypatch.setattr(
        platform_io.ctypes,
        "get_last_error",
        fake.get_last_error,
        raising=False,
    )
    authority = platform_io.open_windows_relative_child_disposition(
        0x1_0000_0009, "child"
    )
    assert authority.is_directory is expected_is_directory
    assert authority.volume_serial_number == 7
    assert authority.file_id == b"1" * 16


def test_disposition_metadata_failure_closes_exactly_once(monkeypatch):
    fake = _FakeWindowsLibraries()
    monkeypatch.setattr(platform_io.ctypes, "WinDLL", fake.windll, raising=False)
    monkeypatch.setattr(
        platform_io.ctypes,
        "get_last_error",
        fake.get_last_error,
        raising=False,
    )
    fake.kernel32.GetFileInformationByHandleEx = _FakeWindowsFunction(
        lambda *_args: 0
    )
    fake.last_error = 5
    with pytest.raises(OSError) as raised:
        platform_io.open_windows_relative_child_disposition(0x1_0000_0009, "child")
    assert (
        raised.value.winerror == 5
        if sys.platform == "win32"
        else raised.value.errno == 5
    )
    assert fake.closes == [0x1_0000_1234]


def test_owned_handle_failed_close_remains_retryable(monkeypatch):
    fake = _FakeWindowsLibraries()
    fake.close_results = [0, 1]
    monkeypatch.setattr(
        platform_io.ctypes,
        "WinError",
        lambda code: OSError(code, "close failed"),
        raising=False,
    )
    handle = platform_io.OwnedWindowsHandle(
        0x1_0000_1234, fake.kernel32.CloseHandle, fake.get_last_error
    )
    with pytest.raises(OSError):
        handle.close()
    assert handle.value == 0x1_0000_1234
    handle.close()
    handle.close()
    assert handle.closed
    assert fake.closes == [0x1_0000_1234, 0x1_0000_1234]


def test_owned_handle_close_failure_uses_default_last_error_on_posix_ctypes(monkeypatch):
    fake = _FakeWindowsLibraries()
    fake.close_results = [0, 1]
    monkeypatch.delattr(platform_io.ctypes, "get_last_error", raising=False)
    monkeypatch.setattr(
        platform_io.ctypes,
        "WinError",
        lambda code: OSError(code, "close failed"),
        raising=False,
    )
    handle = platform_io.OwnedWindowsHandle(
        0x1_0000_1234,
        fake.kernel32.CloseHandle,
        platform_io._windows_last_error_getter(),
    )
    with pytest.raises(OSError) as raised:
        handle.close()
    assert raised.value.errno == 0
    assert handle.value == 0x1_0000_1234
    handle.close()
    assert fake.closes == [0x1_0000_1234, 0x1_0000_1234]


def test_ntcreatefile_negative_status_converts_to_dos_error(monkeypatch):
    fake = _FakeWindowsLibraries(status=-0xC0000034, dos_error=3)
    monkeypatch.setattr(platform_io.ctypes, "WinDLL", fake.windll, raising=False)
    monkeypatch.setattr(
        platform_io.ctypes,
        "get_last_error",
        fake.get_last_error,
        raising=False,
    )
    with pytest.raises(OSError) as raised:
        platform_io.open_windows_relative_child_directory(0x1_0000_0009, "child")
    assert fake.nt_statuses == [-0xC0000034]
    assert raised.value.errno == 3


def test_disposition_marks_exact_handle_with_posix_delete_flags(monkeypatch):
    fake = _FakeWindowsLibraries(attributes=0)
    monkeypatch.setattr(platform_io.ctypes, "WinDLL", fake.windll, raising=False)
    monkeypatch.setattr(
        platform_io.ctypes,
        "get_last_error",
        fake.get_last_error,
        raising=False,
    )
    authority = platform_io.open_windows_relative_child_disposition(
        0x1_0000_0009, "child"
    )
    platform_io.mark_windows_relative_child_disposition(authority)
    assert len(fake.kernel32.SetFileInformationByHandle.argtypes) == 4
    assert fake.disposition_calls == [(0x1_0000_1234, 21, 0x3)]


def test_disposition_marks_directory_with_delete_only_flag(monkeypatch):
    fake = _FakeWindowsLibraries(attributes=0x10)
    monkeypatch.setattr(platform_io.ctypes, "WinDLL", fake.windll, raising=False)
    monkeypatch.setattr(
        platform_io.ctypes,
        "get_last_error",
        fake.get_last_error,
        raising=False,
    )
    authority = platform_io.open_windows_relative_child_disposition(
        0x1_0000_0009, "child"
    )
    platform_io.mark_windows_relative_child_disposition(authority)
    assert fake.disposition_calls == [(0x1_0000_1234, 21, 0x1)]


def test_disposition_falls_back_exactly_on_unsupported_error(monkeypatch):
    fake = _FakeWindowsLibraries(attributes=0, disposition_results=(0, 1))
    monkeypatch.setattr(platform_io.ctypes, "WinDLL", fake.windll, raising=False)
    monkeypatch.setattr(
        platform_io.ctypes,
        "get_last_error",
        fake.get_last_error,
        raising=False,
    )
    authority = platform_io.open_windows_relative_child_disposition(
        0x1_0000_0009, "child"
    )
    fake.last_error = 87
    platform_io.mark_windows_relative_child_disposition(authority)
    assert fake.disposition_calls == [
        (0x1_0000_1234, 21, 0x3),
        (0x1_0000_1234, 4, 1),
    ]


def test_disposition_hard_failure_never_falls_back(monkeypatch):
    fake = _FakeWindowsLibraries(attributes=0, disposition_results=(0,))
    monkeypatch.setattr(platform_io.ctypes, "WinDLL", fake.windll, raising=False)
    monkeypatch.setattr(
        platform_io.ctypes,
        "get_last_error",
        fake.get_last_error,
        raising=False,
    )
    authority = platform_io.open_windows_relative_child_disposition(
        0x1_0000_0009, "child"
    )
    fake.last_error = 5
    with pytest.raises(OSError) as raised:
        platform_io.mark_windows_relative_child_disposition(authority)
    assert (
        raised.value.winerror == 5
        if sys.platform == "win32"
        else raised.value.errno == 5
    )
    assert fake.disposition_calls == [(0x1_0000_1234, 21, 0x3)]


def test_disposition_on_closed_handle_raises_without_native_call(monkeypatch):
    fake = _FakeWindowsLibraries()
    monkeypatch.setattr(platform_io.ctypes, "WinDLL", fake.windll, raising=False)
    monkeypatch.setattr(
        platform_io.ctypes,
        "get_last_error",
        fake.get_last_error,
        raising=False,
    )
    authority = platform_io.open_windows_relative_child_disposition(
        0x1_0000_0009, "child"
    )
    authority.close()
    with pytest.raises(ValueError):
        platform_io.mark_windows_relative_child_disposition(authority)
    assert fake.disposition_calls == []


def test_disposition_fallback_refuses_after_identity_drift(monkeypatch):
    fake = _FakeWindowsLibraries(disposition_results=(0,))
    monkeypatch.setattr(platform_io.ctypes, "WinDLL", fake.windll, raising=False)
    monkeypatch.setattr(
        platform_io.ctypes,
        "get_last_error",
        fake.get_last_error,
        raising=False,
    )
    authority = platform_io.open_windows_relative_child_disposition(
        0x1_0000_0009, "child"
    )
    fake.last_error = 87
    original = fake.kernel32.SetFileInformationByHandle

    def close_then_fail(handle, info_class, output, size):
        authority.close()
        return original(handle, info_class, output, size)

    fake.kernel32.SetFileInformationByHandle = _FakeWindowsFunction(close_then_fail)
    with pytest.raises(OSError, match="closed before disposition fallback"):
        platform_io.mark_windows_relative_child_disposition(authority)


def _native_disposition_canary(tmp_path, child_kind):
    child = tmp_path / "child"
    if child_kind == "directory":
        child.mkdir()
    else:
        child.write_text("child", encoding="utf-8")
    sibling = tmp_path / "sibling"
    sibling.write_text("sibling", encoding="utf-8")
    kernel32 = getattr(platform_io.ctypes, "WinDLL")(
        "kernel32", use_last_error=True
    )
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        platform_io.ctypes.c_wchar_p,
        platform_io.ctypes.c_uint32,
        platform_io.ctypes.c_uint32,
        platform_io.ctypes.c_void_p,
        platform_io.ctypes.c_uint32,
        platform_io.ctypes.c_uint32,
        platform_io.ctypes.c_void_p,
    )
    create_file.restype = platform_io.ctypes.c_void_p
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (platform_io.ctypes.c_void_p,)
    close_handle.restype = platform_io.ctypes.c_int
    parent_handle = platform_io._handle_value(
        create_file(str(tmp_path), 0x81, 0x3, None, 3, 0x02200000, None)
    )
    assert parent_handle not in (0, platform_io._INVALID_HANDLE_VALUE)
    try:
        authority = platform_io.open_windows_relative_child_disposition(
            parent_handle,
            "child",
        )
        assert authority.handle.value not in (0, platform_io._INVALID_HANDLE_VALUE)
        assert authority.is_directory is (child_kind == "directory")
        assert authority.volume_serial_number != 0
        assert any(authority.file_id)
        platform_io.mark_windows_relative_child_disposition(authority)
        authority.close()
        authority.close()
        assert not child.exists()
        assert sibling.exists()
    finally:
        assert close_handle(parent_handle)


@pytest.mark.parametrize("child_kind", ["file", "directory"])
def test_native_canary_relative_child_disposition(tmp_path, child_kind):
    if os.name != "nt":
        pytest.skip("native Windows canary")
    _native_disposition_canary(tmp_path, child_kind)


def test_background_process_launch_kwargs_dispatches_exactly_by_platform(monkeypatch):
    startup = SimpleNamespace(dwFlags=0, wShowWindow=None)
    monkeypatch.setattr(
        _platform_process.subprocess, "STARTUPINFO", lambda: startup, raising=False
    )
    monkeypatch.setattr(
        _platform_process.subprocess, "STARTF_USESHOWWINDOW", 4, raising=False
    )
    monkeypatch.setattr(_platform_process.subprocess, "SW_HIDE", 0, raising=False)
    monkeypatch.setattr(
        _platform_process.subprocess, "CREATE_NO_WINDOW", 8, raising=False
    )

    assert platform_io.background_process_launch_kwargs("nt") == {
        "creationflags": 8,
        "startupinfo": startup,
    }
    assert startup.dwFlags == 4
    assert startup.wShowWindow == 0
    assert platform_io.background_process_launch_kwargs("posix") == {
        "start_new_session": True,
    }


def test_process_backend_selection_tracks_current_platform(monkeypatch):
    monkeypatch.setattr(_platform_process.os, "name", "posix")
    assert platform_io.background_process_launch_kwargs() == {
        "start_new_session": True,
    }
    startup = SimpleNamespace(dwFlags=0, wShowWindow=None)
    monkeypatch.setattr(
        _platform_process.subprocess, "STARTUPINFO", lambda: startup, raising=False
    )
    monkeypatch.setattr(
        _platform_process.subprocess, "STARTF_USESHOWWINDOW", 4, raising=False
    )
    monkeypatch.setattr(_platform_process.subprocess, "SW_HIDE", 0, raising=False)
    monkeypatch.setattr(
        _platform_process.subprocess, "CREATE_NO_WINDOW", 8, raising=False
    )
    monkeypatch.setattr(_platform_process.os, "name", "nt")
    assert platform_io.background_process_launch_kwargs()["creationflags"] == 8


@pytest.mark.parametrize(("pid", "expected"), [(0, False), (-1, False), (True, False)])
def test_process_is_alive_rejects_invalid_pid(monkeypatch, pid, expected):
    monkeypatch.setattr(_platform_process.os, "name", "posix")
    monkeypatch.setattr(
        _platform_process.os, "kill", lambda *_args: pytest.fail("kill")
    )
    assert platform_io.process_is_alive(pid) is expected


@pytest.mark.parametrize(
    ("error", "expected"),
    [(ProcessLookupError(), False), (PermissionError(), True), (OSError(), True)],
)
def test_posix_process_liveness_preserves_probe_semantics(monkeypatch, error, expected):
    monkeypatch.setattr(_platform_process.os, "name", "posix")

    def raise_probe_error(*_args):
        raise error

    monkeypatch.setattr(_platform_process.os, "kill", raise_probe_error)
    assert platform_io.process_is_alive(42) is expected


def test_windows_process_liveness_closes_handle_and_reads_exit_code(monkeypatch):
    calls: list[object] = []
    exit_code = 259

    def get_exit_code(handle, output):
        assert handle == 123
        output._obj.value = exit_code
        return 1

    kernel32 = SimpleNamespace(
        OpenProcess=_FakeWindowsFunction(lambda access, inherit, pid: 123),
        GetExitCodeProcess=_FakeWindowsFunction(get_exit_code),
        CloseHandle=_FakeWindowsFunction(lambda handle: calls.append(handle) or 1),
    )
    monkeypatch.setattr(
        _platform_process.ctypes,
        "WinDLL",
        lambda *_args, **_kwargs: kernel32,
        raising=False,
    )
    monkeypatch.setattr(
        _platform_process.ctypes, "get_last_error", lambda: 5, raising=False
    )
    monkeypatch.setattr(_platform_process.os, "name", "nt")

    assert platform_io.process_is_alive(42)
    assert calls == [123]


def test_windows_process_liveness_treats_access_denied_as_alive(monkeypatch):
    kernel32 = SimpleNamespace(
        OpenProcess=_FakeWindowsFunction(lambda *_args: 0),
        GetExitCodeProcess=_FakeWindowsFunction(
            lambda *_args: pytest.fail("exit code")
        ),
        CloseHandle=_FakeWindowsFunction(lambda *_args: pytest.fail("close")),
    )
    monkeypatch.setattr(
        _platform_process.ctypes,
        "WinDLL",
        lambda *_args, **_kwargs: kernel32,
        raising=False,
    )
    monkeypatch.setattr(
        _platform_process.ctypes, "get_last_error", lambda: 5, raising=False
    )
    assert _platform_process.windows_process_is_alive(42)


def test_windows_pid_probe_is_non_signalling():
    if os.name != "nt":
        return
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        assert platform_io.windows_pid_is_alive(child.pid)
        assert child.poll() is None, "liveness probe terminated the target process"
    finally:
        child.terminate()
        child.wait(timeout=10)
    assert not platform_io.windows_pid_is_alive(child.pid)


def test_deadlock_errno_accepts_macos_posix_spelling_without_windows_alias():
    assert platform_io._deadlock_errno(SimpleNamespace(EDEADLK=35)) == 35
    assert platform_io._deadlock_errno(SimpleNamespace(EDEADLOCK=36, EDEADLK=35)) == 36


def test_posix_lock_round_trip(tmp_path):
    path = tmp_path / "runtime.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        platform_io.chmod_fd(fd, 0o600)
        platform_io.lock_fd(fd, blocking=False)
        platform_io.unlock_fd(fd)
    finally:
        os.close(fd)


def test_chmod_path_skips_posix_mode_on_windows(monkeypatch, tmp_path):
    target = tmp_path / "owner-acl-file"
    target.write_text("ok", encoding="utf-8")

    def denied(_path, _mode):
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(platform_io.os, "chmod", denied)
    platform_io.chmod_path(target, 0o600, platform_name="nt")


def test_chmod_path_applies_posix_mode(monkeypatch, tmp_path):
    target = tmp_path / "private-file"
    target.write_text("ok", encoding="utf-8")
    calls: list[tuple[object, int]] = []

    monkeypatch.setattr(
        platform_io.os,
        "chmod",
        lambda path, mode: calls.append((path, mode)),
    )

    platform_io.chmod_path(target, 0o600, platform_name="posix")

    assert calls == [(target, 0o600)]


def test_atomic_replace_retries_transient_windows_sharing_violation(monkeypatch):
    calls: list[tuple[object, object]] = []
    # platform_io.os IS the os module, so this patch is process-wide. Only this
    # thread's replaces are the subject: a background thread writing atomically
    # would otherwise consume the one call that is supposed to fail.
    caller = threading.get_ident()
    real_replace = os.replace

    def transient_replace(source, destination):
        if threading.get_ident() != caller:
            return real_replace(source, destination)
        calls.append((source, destination))
        if len(calls) == 1:
            raise PermissionError(32, "file is being used by another process")

    monotonic_values = iter((10.0, 10.1))
    monkeypatch.setattr(platform_io.os, "name", "nt")
    monkeypatch.setattr(platform_io.os, "replace", transient_replace)
    monkeypatch.setattr(platform_io.time, "monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(platform_io.time, "sleep", lambda _seconds: None)

    platform_io.atomic_replace("seed.tmp", "AGENTS.md")

    assert calls == [("seed.tmp", "AGENTS.md"), ("seed.tmp", "AGENTS.md")]


def test_durable_atomic_replace_directory_open_failure_preserves_paths(
    tmp_path, monkeypatch
):
    if os.name == "nt":
        pytest.skip("Windows has no directory-fsync contract")
    source = tmp_path / "source.tmp"
    destination = tmp_path / "canonical"
    source.write_bytes(b"new")
    destination.write_bytes(b"prior")
    destination_directory = os.fspath(tmp_path)
    real_open = os.open

    def fail_destination_directory_open(path, flags, mode=0o777):
        if os.fspath(path) == destination_directory and flags & os.O_DIRECTORY:
            raise OSError(errno.EACCES, "directory open failed")
        return real_open(path, flags, mode)

    monkeypatch.setattr(platform_io.os, "open", fail_destination_directory_open)

    with pytest.raises(OSError, match="directory open failed"):
        platform_io.durable_atomic_replace(source, destination)

    assert destination.read_bytes() == b"prior"
    assert source.read_bytes() == b"new"


def test_durable_atomic_replace_reports_post_commit_directory_sync_failure(
    tmp_path, monkeypatch
):
    if os.name == "nt":
        pytest.skip("Windows has no directory-fsync contract")
    source = tmp_path / "source.tmp"
    destination = tmp_path / "canonical"
    source.write_bytes(b"new")
    destination.write_bytes(b"prior")
    real_fsync = os.fsync

    def fail_directory_fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "directory sync failed")
        real_fsync(fd)

    monkeypatch.setattr(platform_io.os, "fsync", fail_directory_fsync)

    with pytest.raises(platform_io.PublicationDurabilityError) as raised:
        platform_io.durable_atomic_replace(source, destination)

    assert raised.value.replacement_committed is True
    assert raised.value.published is True
    assert destination.read_bytes() == b"new"


def test_identity_bound_replace_rejects_source_substitution_before_publication(
    tmp_path, monkeypatch
):
    if os.name == "nt":
        pytest.skip("descriptor-bound publication is POSIX-only")
    source = tmp_path / "source.tmp"
    original = tmp_path / "original.saved"
    destination = tmp_path / "canonical"
    source.write_bytes(b"verified")
    destination.write_bytes(b"prior")
    real_replace = os.replace
    swapped = False

    def swap_before_quarantine(left, right, **kwargs):
        nonlocal swapped
        if not swapped and os.fspath(left) in {os.fspath(source), source.name}:
            swapped = True
            real_replace(source, original)
            source.write_bytes(b"substitute")
        return real_replace(left, right, **kwargs)

    monkeypatch.setattr(platform_io.os, "replace", swap_before_quarantine)

    with pytest.raises(
        platform_io.IdentityBoundPublicationError,
        match="publication_source_identity_changed",
    ):
        platform_io.identity_bound_durable_atomic_replace(source, destination)

    assert swapped
    assert destination.read_bytes() == b"prior"
    assert source.read_bytes() == b"substitute"
    assert original.read_bytes() == b"verified"


def test_identity_bound_replace_fails_closed_on_unsupported_platform(
    tmp_path, monkeypatch
):
    source = tmp_path / "source.tmp"
    destination = tmp_path / "canonical"
    source.write_bytes(b"new")
    destination.write_bytes(b"prior")
    # A host that is neither Linux nor macOS has no descriptor-bound namespace
    # operation this module can guarantee, so publication must fail closed.
    monkeypatch.setattr(platform_io, "is_linux", lambda _name=None: False)
    monkeypatch.setattr(platform_io, "is_macos", lambda _name=None: False)

    with pytest.raises(platform_io.IdentityBoundPublicationError) as raised:
        platform_io.identity_bound_durable_atomic_replace(source, destination)

    assert raised.value.errno == errno.ENOTSUP
    assert destination.read_bytes() == b"prior"
    assert source.read_bytes() == b"new"


def _force_macos_publication_branch(monkeypatch):
    """Drive the macOS staging-link branch on any POSIX host with linkat.

    The macOS branch relies only on portable ``linkat``/``renameat``/``fstatat``
    semantics that Linux also provides, so its control flow and identity
    re-verification are exercisable here. Native macOS filesystem behaviour
    still requires the macOS qualification job for final proof.
    """

    if os.name == "nt":
        pytest.skip("macOS dir-fd branch requires a POSIX host")
    monkeypatch.setattr(platform_io, "is_linux", lambda _name=None: False)
    monkeypatch.setattr(platform_io, "is_macos", lambda _name=None: True)


def test_identity_bound_replace_publishes_verified_identity_on_macos_branch(
    tmp_path, monkeypatch
):
    source = tmp_path / "source.tmp"
    destination = tmp_path / "canonical"
    source.write_bytes(b"verified")
    destination.write_bytes(b"prior")
    _force_macos_publication_branch(monkeypatch)

    platform_io.identity_bound_durable_atomic_replace(source, destination)

    assert destination.read_bytes() == b"verified"
    assert not source.exists()
    assert list(tmp_path.glob(".canonical.publish-*")) == []


def test_macos_branch_closes_only_valid_publication_descriptors(
    tmp_path, monkeypatch
):
    source = tmp_path / "source.tmp"
    destination = tmp_path / "canonical"
    source.write_bytes(b"verified")
    destination.write_bytes(b"prior")
    _force_macos_publication_branch(monkeypatch)
    real_close = os.close
    closed: list[int] = []

    def close_valid_fd(fd):
        assert fd >= 0
        closed.append(fd)
        real_close(fd)

    monkeypatch.setattr(platform_io.os, "close", close_valid_fd)

    platform_io.identity_bound_durable_atomic_replace(source, destination)

    assert destination.read_bytes() == b"verified"
    assert not source.exists()
    assert len(closed) == 4


def test_macos_branch_rejects_source_substitution_at_staging_link(
    tmp_path, monkeypatch
):
    source = tmp_path / "source.tmp"
    original = tmp_path / "original.saved"
    destination = tmp_path / "canonical"
    source.write_bytes(b"verified")
    destination.write_bytes(b"prior")
    _force_macos_publication_branch(monkeypatch)
    real_link = os.link
    swapped = False

    def swap_before_link(src, dst, **kwargs):
        nonlocal swapped
        if not swapped:
            swapped = True
            # Substitute the source path after it was verified but before the
            # staging link binds it: the linked inode must no longer match.
            os.replace(source, original)
            source.write_bytes(b"substitute")
        return real_link(src, dst, **kwargs)

    monkeypatch.setattr(platform_io.os, "link", swap_before_link)

    with pytest.raises(
        platform_io.IdentityBoundPublicationError,
        match="publication_source_identity_changed",
    ):
        platform_io.identity_bound_durable_atomic_replace(source, destination)

    assert swapped
    assert destination.read_bytes() == b"prior"
    assert original.read_bytes() == b"verified"
    assert list(tmp_path.glob(".canonical.publish-*")) == []


def test_macos_branch_fails_closed_on_cross_device_staging(tmp_path, monkeypatch):
    source = tmp_path / "source.tmp"
    destination = tmp_path / "canonical"
    source.write_bytes(b"new")
    destination.write_bytes(b"prior")
    _force_macos_publication_branch(monkeypatch)

    def cross_device_link(*_args, **_kwargs):
        raise OSError(errno.EXDEV, "cross-device link")

    monkeypatch.setattr(platform_io.os, "link", cross_device_link)

    with pytest.raises(platform_io.IdentityBoundPublicationError) as raised:
        platform_io.identity_bound_durable_atomic_replace(source, destination)

    assert raised.value.errno == errno.ENOTSUP
    assert raised.value.published is False
    assert destination.read_bytes() == b"prior"
    assert source.read_bytes() == b"new"


def test_identity_bound_replace_ignores_public_staging_namespace_substitution(
    tmp_path, monkeypatch
):
    if not platform_io.is_linux():
        pytest.skip("procfs descriptor-link substitution is Linux-specific")
    source = tmp_path / "source.tmp"
    destination = tmp_path / "canonical"
    displaced_staging = tmp_path / "displaced-staging"
    source.write_bytes(b"verified")
    destination.write_bytes(b"prior")
    real_link_open_file = platform_io._link_open_file

    def substitute_staging(directory_fd, target_directory_fd, name):
        real_link_open_file(directory_fd, target_directory_fd, name)
        staging_directory = os.readlink(f"/proc/self/fd/{target_directory_fd}")
        os.replace(staging_directory, displaced_staging)
        os.mkdir(staging_directory)
        Path(staging_directory, name).write_bytes(b"substitute")

    monkeypatch.setattr(platform_io, "_link_open_file", substitute_staging)

    platform_io.identity_bound_durable_atomic_replace(source, destination)

    assert destination.read_bytes() == b"verified"
    assert displaced_staging.exists()


def test_identity_bound_replace_commits_through_open_destination_directory(
    tmp_path, monkeypatch
):
    if os.name == "nt":
        pytest.skip("descriptor-relative rename requires a POSIX host")
    source = tmp_path / "source.tmp"
    destination_directory = tmp_path / "destination"
    displaced_directory = tmp_path / "destination.displaced"
    destination_directory.mkdir()
    destination = destination_directory / "canonical"
    source.write_bytes(b"verified")
    destination.write_bytes(b"prior")
    real_replace = os.replace
    swapped = False

    def swap_destination_before_commit(left, right, **kwargs):
        nonlocal swapped
        if not swapped and kwargs.get("src_dir_fd") != kwargs.get("dst_dir_fd"):
            swapped = True
            real_replace(destination_directory, displaced_directory)
            destination_directory.mkdir()
            (destination_directory / "canonical").write_bytes(b"substitute")
        return real_replace(left, right, **kwargs)

    monkeypatch.setattr(platform_io.os, "replace", swap_destination_before_commit)

    platform_io.identity_bound_durable_atomic_replace(source, destination)

    assert swapped
    assert (displaced_directory / "canonical").read_bytes() == b"verified"
    assert destination.read_bytes() == b"substitute"


def test_identity_bound_replace_removes_staging_for_hard_link_alias(tmp_path):
    if os.name == "nt":
        pytest.skip("descriptor-bound publication is POSIX-only")
    source = tmp_path / "source.tmp"
    destination = tmp_path / "canonical"
    source.write_bytes(b"verified")
    os.link(source, destination)

    platform_io.identity_bound_durable_atomic_replace(source, destination)

    assert destination.read_bytes() == b"verified"
    assert not source.exists()
    assert list(tmp_path.glob(".canonical.publish-*")) == []


def test_identity_bound_replace_fails_closed_on_cross_device_staging(
    tmp_path, monkeypatch
):
    if not platform_io.is_linux():
        pytest.skip("linkat injection exercises the Linux publication branch")
    source = tmp_path / "source.tmp"
    destination = tmp_path / "canonical"
    source.write_bytes(b"new")
    destination.write_bytes(b"prior")
    monkeypatch.setattr(platform_io.ctypes, "get_errno", lambda: errno.EXDEV)

    class _LinkAt:
        argtypes = None
        restype = None

        def __call__(self, *_args):
            return -1

    monkeypatch.setattr(
        platform_io.ctypes,
        "CDLL",
        lambda *_args, **_kwargs: SimpleNamespace(linkat=_LinkAt()),
    )

    with pytest.raises(platform_io.IdentityBoundPublicationError) as raised:
        platform_io.identity_bound_durable_atomic_replace(source, destination)

    assert raised.value.errno == errno.ENOTSUP
    assert raised.value.published is False
    assert destination.read_bytes() == b"prior"
    assert source.read_bytes() == b"new"


def test_windows_lock_backend_uses_one_byte_region(tmp_path, monkeypatch):
    calls: list[tuple[int, int, int]] = []
    fake_msvcrt = SimpleNamespace(
        LK_LOCK=1,
        LK_NBLCK=2,
        LK_UNLCK=3,
        locking=lambda fd, mode, count: calls.append((fd, mode, count)),
    )
    monkeypatch.setitem(sys.modules, "msvcrt", fake_msvcrt)
    monkeypatch.setattr(platform_io.os, "name", "nt")

    path = tmp_path / "windows-runtime.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        platform_io.lock_fd(fd, blocking=False)
        platform_io.unlock_fd(fd)
        assert os.fstat(fd).st_size == 1
    finally:
        os.close(fd)

    assert [mode for _, mode, _ in calls] == [2, 3]
    assert all(count == 1 for _, _, count in calls)


def test_windows_blocking_lock_timeout_is_classified_as_contention(
    tmp_path, monkeypatch
):
    attempts: list[tuple[int, int, int]] = []

    def contended(fd, mode, count):
        attempts.append((fd, mode, count))
        raise OSError(errno.EACCES, "lock is owned by another finalizer")

    fake_msvcrt = SimpleNamespace(
        LK_LOCK=1,
        LK_NBLCK=2,
        LK_UNLCK=3,
        locking=contended,
    )
    monkeypatch.setitem(sys.modules, "msvcrt", fake_msvcrt)
    monkeypatch.setattr(platform_io.os, "name", "nt")
    monkeypatch.setattr(platform_io, "ADVISORY_LOCK_MAX_WAIT_SECONDS", 0.0)
    monkeypatch.setattr(platform_io.time, "monotonic", lambda: 10.0)

    path = tmp_path / "contended-windows-runtime.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        with pytest.raises(
            platform_io.AdvisoryLockTimeout,
            match="windows_advisory_lock_timeout",
        ):
            platform_io.lock_fd(fd, blocking=True)
    finally:
        os.close(fd)

    target_attempts = [attempt for attempt in attempts if attempt[0] == fd]
    assert target_attempts == [(fd, fake_msvcrt.LK_NBLCK, 1)]


def test_windows_blocking_lock_preserves_unexpected_os_error(tmp_path, monkeypatch):
    def failed_lock(_fd, _mode, _count):
        raise OSError(errno.EIO, "device failure")

    fake_msvcrt = SimpleNamespace(
        LK_LOCK=1,
        LK_NBLCK=2,
        LK_UNLCK=3,
        locking=failed_lock,
    )
    monkeypatch.setitem(sys.modules, "msvcrt", fake_msvcrt)
    monkeypatch.setattr(platform_io.os, "name", "nt")

    path = tmp_path / "failed-windows-runtime.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        with pytest.raises(OSError) as raised:
            platform_io.lock_fd(fd, blocking=True)
    finally:
        os.close(fd)

    assert raised.value.errno == errno.EIO
    assert not isinstance(raised.value, platform_io.AdvisoryLockTimeout)


def _drive_windows_lock_timeout(tmp_path, monkeypatch, contended_errno):
    """Drive the Windows blocking-lock path to timeout on one contended errno."""

    def contended(fd, mode, count):
        raise OSError(contended_errno, "contended byte range")

    fake_msvcrt = SimpleNamespace(
        LK_LOCK=1, LK_NBLCK=2, LK_UNLCK=3, locking=contended
    )
    monkeypatch.setitem(sys.modules, "msvcrt", fake_msvcrt)
    monkeypatch.setattr(platform_io.os, "name", "nt")
    monkeypatch.setattr(platform_io, "ADVISORY_LOCK_MAX_WAIT_SECONDS", 0.0)
    monkeypatch.setattr(platform_io.time, "monotonic", lambda: 10.0)

    path = tmp_path / "contended-windows.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        with pytest.raises(platform_io.AdvisoryLockTimeout) as raised:
            platform_io.lock_fd(fd, blocking=True)
    finally:
        os.close(fd)
    return raised.value


def test_windows_lock_timeout_distinguishes_permission_from_contention(
    tmp_path, monkeypatch
):
    """REPRODUCTION: EACCES (permission-shaped) and the deadlock errno both time
    out through the merged Windows contended set. Before the fix both raise an
    ``AdvisoryLockTimeout`` that only says it timed out, so the two outcomes are
    indistinguishable; the timeout must now report which errno ended the wait."""
    permission = _drive_windows_lock_timeout(tmp_path, monkeypatch, errno.EACCES)
    contention = _drive_windows_lock_timeout(
        tmp_path, monkeypatch, platform_io._deadlock_errno()
    )

    assert permission.errno == errno.EACCES
    assert contention.errno == platform_io._deadlock_errno()
    assert permission.errno != contention.errno
    assert "EACCES" in str(permission)


def test_windows_lock_timeout_exposes_last_observed_errno(tmp_path, monkeypatch):
    """A timed-out wait exposes the last observed errno as a structured attribute
    and names it symbolically, not only in prose."""
    value = _drive_windows_lock_timeout(tmp_path, monkeypatch, errno.EACCES)

    assert isinstance(value, platform_io.AdvisoryLockTimeout)
    assert value.errno == errno.EACCES
    assert "windows_advisory_lock_timeout" in str(value)
    assert "EACCES" in str(value)


def test_windows_unrecognized_errno_propagates_immediately_without_retry(
    tmp_path, monkeypatch
):
    """An unrecognized errno still propagates immediately as ``OSError`` -- never
    retried, never reshaped into an ``AdvisoryLockTimeout``."""
    attempts: list[int] = []

    def failing(fd, mode, count):
        attempts.append(mode)
        raise OSError(errno.EIO, "device failure")

    fake_msvcrt = SimpleNamespace(
        LK_LOCK=1, LK_NBLCK=2, LK_UNLCK=3, locking=failing
    )
    monkeypatch.setitem(sys.modules, "msvcrt", fake_msvcrt)
    monkeypatch.setattr(platform_io.os, "name", "nt")
    monkeypatch.setattr(
        platform_io.time,
        "sleep",
        lambda _s: pytest.fail("unrecognized errno must not be retried"),
    )

    path = tmp_path / "io-error-windows.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        with pytest.raises(OSError) as raised:
            platform_io.lock_fd(fd, blocking=True)
    finally:
        os.close(fd)

    assert raised.value.errno == errno.EIO
    assert not isinstance(raised.value, platform_io.AdvisoryLockTimeout)
    assert attempts == [fake_msvcrt.LK_NBLCK]


@pytest.mark.parametrize(
    ("name", "windows", "linux", "macos"),
    [
        ("win32", True, False, False),
        ("nt", True, False, False),
        ("linux2", False, True, False),
        ("posix", False, True, False),
        ("darwin", False, False, True),
    ],
)
def test_platform_predicates_share_one_normalized_contract(name, windows, linux, macos):
    assert platform_io.is_windows(name) is windows
    assert platform_io.is_linux(name) is linux
    assert platform_io.is_macos(name) is macos


def test_process_group_launch_kwargs_uses_named_windows_flag(monkeypatch):
    monkeypatch.setattr(
        platform_io.subprocess, "CREATE_NEW_PROCESS_GROUP", 512, raising=False
    )
    assert platform_io.process_group_launch_kwargs("windows") == {
        "creationflags": 512
    }
    assert platform_io.process_group_launch_kwargs("linux") == {
        "start_new_session": True
    }


@pytest.mark.parametrize("identity", [0, -1, True, False, 1.5, "42", None])
def test_process_group_primitives_reject_unsafe_identities(identity):
    assert not platform_io.probe_process_group(
        identity, killpg=lambda *_args: pytest.fail("killpg")
    )
    assert not platform_io.terminate_process_tree(
        identity,
        killpg=lambda *_args: pytest.fail("killpg"),
        run=lambda *_args, **_kwargs: pytest.fail("run"),
    )


@pytest.mark.parametrize("argument", ["timeout", "poll_interval"])
@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
def test_process_tree_rejects_nonfinite_waits_before_process_actions(argument, value):
    calls = []
    assert not platform_io.terminate_process_tree(
        42,
        platform_name="windows",
        killpg=lambda *_args: calls.append("killpg"),
        probe=lambda *_args: calls.append("probe") or True,
        run=lambda *_args, **_kwargs: calls.append("run"),
        sleep=lambda *_args: calls.append("sleep"),
        **{argument: value},
    )
    assert calls == []


def test_posix_process_group_probe_is_nondestructive_and_fails_closed():
    calls = []

    def probe(pgid, sig):
        calls.append((pgid, sig))
        raise PermissionError

    assert platform_io.probe_process_group(42, platform_name="linux", killpg=probe)
    assert calls == [(42, 0)]


def test_posix_process_group_probe_fails_closed_for_other_oserror():
    def probe(_pgid, _sig):
        raise OSError(errno.EIO, "fake I/O failure")

    assert not platform_io.probe_process_group(
        42, platform_name="linux", killpg=probe
    )


def test_posix_process_tree_terminates_without_escalation():
    signals = []
    states = iter((True, False))
    assert platform_io.terminate_process_tree(
        42,
        platform_name="linux",
        timeout=1.0,
        poll_interval=0.01,
        killpg=lambda pgid, sig: signals.append((pgid, sig)),
        probe=lambda _pgid: next(states),
        monotonic=lambda: 0.0,
        sleep=lambda _delay: None,
    )
    assert signals == [(42, platform_io.signal.SIGTERM)]


def test_posix_process_tree_polls_after_kill_until_group_disappears():
    signals = []
    sleeps = []
    clock = iter((10.0, 10.1, 10.1, 10.1))
    states = iter((True, True, False))
    assert platform_io.terminate_process_tree(
        42,
        platform_name="macos",
        timeout=0.1,
        poll_interval=0.1,
        killpg=lambda pgid, sig: signals.append((pgid, sig)),
        probe=lambda _pgid: next(states),
        monotonic=lambda: next(clock),
        sleep=lambda delay: sleeps.append(delay),
    )
    assert signals == [
        (42, platform_io.signal.SIGTERM),
        (42, platform_io._POSIX_SIGKILL),
    ]
    assert sleeps == [pytest.approx(0.1)]


def test_posix_process_tree_reports_persistent_survivor_at_post_kill_deadline():
    signals = []
    sleeps = []
    clock = iter((10.0, 10.1, 10.1, 10.1, 10.2))
    assert not platform_io.terminate_process_tree(
        42,
        platform_name="linux",
        timeout=0.1,
        poll_interval=0.1,
        killpg=lambda pgid, sig: signals.append((pgid, sig)),
        probe=lambda _pgid: True,
        monotonic=lambda: next(clock),
        sleep=lambda delay: sleeps.append(delay),
    )
    assert signals == [
        (42, platform_io.signal.SIGTERM),
        (42, platform_io._POSIX_SIGKILL),
    ]
    assert sleeps == [pytest.approx(0.1)]


def test_posix_process_tree_zero_timeout_escalates_without_sleeping():
    signals = []
    probes = []
    sleeps = []
    assert not platform_io.terminate_process_tree(
        42,
        platform_name="linux",
        timeout=0.0,
        poll_interval=0.0,
        killpg=lambda pgid, sig: signals.append((pgid, sig)),
        probe=lambda pgid: probes.append(pgid) or True,
        monotonic=lambda: 10.0,
        sleep=lambda delay: sleeps.append(delay),
    )
    assert signals == [
        (42, platform_io.signal.SIGTERM),
        (42, platform_io._POSIX_SIGKILL),
    ]
    assert probes == [42, 42]
    assert sleeps == []


def test_windows_process_tree_uses_graceful_taskkill_tree_command():
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0)

    assert platform_io.terminate_process_tree(
        42,
        platform_name="windows",
        timeout=2.5,
        run=run,
        probe=lambda _pid: False,
    )
    assert calls == [
        (
            ["taskkill", "/PID", "42", "/T"],
            {"check": False, "shell": False, "timeout": 2.5},
        )
    ]


def test_windows_process_tree_forces_survivor_after_grace_period():
    calls = []
    probes = iter((True, True))
    clock = iter((10.0, 10.1, 10.1))

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0)

    assert platform_io.terminate_process_tree(
        42,
        platform_name="windows",
        timeout=0.1,
        poll_interval=0.1,
        run=run,
        probe=lambda _pid: next(probes),
        monotonic=lambda: next(clock),
        sleep=lambda _delay: None,
    )
    assert calls == [
        (
            ["taskkill", "/PID", "42", "/T"],
            {"check": False, "shell": False, "timeout": 0.1},
        ),
        (
            ["taskkill", "/F", "/PID", "42", "/T"],
            {"check": False, "shell": False, "timeout": 0.1},
        ),
    ]


@pytest.mark.parametrize(
    "error",
    [
        subprocess.TimeoutExpired(cmd="taskkill", timeout=2.5),
        OSError(errno.ENOENT, "fake missing taskkill"),
    ],
)
def test_windows_process_tree_fails_closed_when_taskkill_cannot_complete(error):
    def run(*_args, **_kwargs):
        raise error

    assert not platform_io.terminate_process_tree(
        42, platform_name="windows", timeout=2.5, run=run
    )


def test_path_identity_and_executable_names_are_platform_specific():
    assert platform_io.executable_name("worker", "windows") == "worker.exe"
    assert platform_io.executable_name("WORKER.EXE", "windows") == "WORKER.EXE"
    assert platform_io.executable_name("worker", "linux") == "worker"
    assert platform_io.paths_equal(r"C:\\Temp\\..\\Work", r"c:\\work", "win32")
    assert not platform_io.paths_equal("Work", "work", "linux")
    assert platform_io.path_key("a/../b", "linux").endswith("/b")


def test_windows_module_import_and_process_branches_do_not_require_killpg(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "aiworkhub._platform_io_windows_import", platform_io.__file__
    )
    assert spec is not None
    assert spec.loader is not None
    imported = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, imported)
    monkeypatch.delattr(os, "killpg", raising=False)
    spec.loader.exec_module(imported)

    assert imported.is_windows("win32")
    assert imported.probe_process_group(
        42, platform_name="windows", windows_probe=lambda pid: pid == 42
    )
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0)

    assert imported.terminate_process_tree(
        42,
        platform_name="windows",
        timeout=1.5,
        run=run,
        probe=lambda _pid: False,
    )
    assert calls == [
        (
            ["taskkill", "/PID", "42", "/T"],
            {"check": False, "shell": False, "timeout": 1.5},
        )
    ]
    assert not imported.probe_process_group(42, platform_name="linux")
    assert not imported.terminate_process_tree(42, platform_name="linux")


def test_signal_process_group_uses_exact_posix_group_signal(monkeypatch):
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(
        platform_io.os,
        "killpg",
        lambda pgid, sig: calls.append((pgid, sig)),
        raising=False,
    )

    platform_io.signal_process_group(42, graceful=True)
    platform_io.signal_process_group(42, graceful=False)

    assert calls == [
        (42, platform_io.signal.SIGTERM),
        (42, platform_io._POSIX_SIGKILL),
    ]


def test_pipe_write_end_probe_fails_closed_on_windows(monkeypatch):
    monkeypatch.setattr(platform_io.sys, "platform", "win32")
    assert platform_io.pipe_write_end_still_open(()) is True


def test_directory_descriptor_backend_names_what_the_host_can_actually_pin():
    """Windows cannot hold a descriptor on a directory; say so, do not guess.

    The reconciler used to assemble its own mask with
    ``hasattr(os, "O_DIRECTORY")`` / ``hasattr(os, "O_NOFOLLOW")``. Neither
    constant exists on Windows, so the mask silently degraded to a bare
    ``os.O_RDONLY`` -- and ``os.open`` of a DIRECTORY with that mask raises
    ``PermissionError`` on Windows, every time, forever.
    """

    assert (
        platform_io.directory_descriptor_backend("windows")
        == platform_io.DIRECTORY_DESCRIPTOR_BACKEND_NONE
    )
    assert (
        platform_io.directory_descriptor_backend("linux")
        == platform_io.DIRECTORY_DESCRIPTOR_BACKEND_POSIX
    )
    assert (
        platform_io.directory_descriptor_backend("macos")
        == platform_io.DIRECTORY_DESCRIPTOR_BACKEND_POSIX
    )


def test_open_directory_descriptor_refuses_rather_than_degrading_on_windows(tmp_path):
    """Injection selects the BRANCH; it cannot change the host's syscalls.

    The Windows half is host-independent: the function decides from the
    injected name and returns before touching the filesystem, so it asserts
    the same thing everywhere. The POSIX half is not -- it performs a real
    ``os.open`` on a directory, which only a POSIX host can serve. Forcing
    ``"linux"`` on a Windows runner therefore takes the POSIX branch into a
    syscall Windows rejects, and release qualification failed exactly there
    with ``PermissionError: [Errno 13] ... \\locks``. That is the very defect
    this module exists to prevent, reproduced in the test that guards it.
    """

    target = tmp_path / "locks"
    target.mkdir()

    assert platform_io.open_directory_descriptor(target, "windows") is None
    # ``None`` must be safe to hand straight back to the closer.
    platform_io.close_directory_descriptor(None)

    if os.name == "nt":
        # A Windows host cannot serve the POSIX branch, so the branch decision
        # is all that can be asserted here. `directory_open_flags` is checked
        # against its constants by its own test, on every platform.
        return
    descriptor = platform_io.open_directory_descriptor(target, "linux")
    try:
        assert isinstance(descriptor, int)
        assert stat.S_ISDIR(os.fstat(descriptor).st_mode)
    finally:
        platform_io.close_directory_descriptor(descriptor)


def test_directory_open_flags_never_reduce_to_a_bare_read_only_open():
    """A directory open must carry O_DIRECTORY wherever the host defines it."""

    flags = platform_io.directory_open_flags()
    for name in ("O_DIRECTORY", "O_NOFOLLOW"):
        constant = getattr(os, name, None)
        if constant is not None:
            assert flags & constant == constant


def test_nofollow_flag_is_read_at_call_time_not_captured_at_import(monkeypatch):
    """A caller that removes the constant must be observed by every reader."""

    assert platform_io.nofollow_open_flag() == getattr(os, "O_NOFOLLOW", 0)
    monkeypatch.delattr(platform_io.os, "O_NOFOLLOW", raising=False)
    assert platform_io.nofollow_open_flag() == 0
    assert platform_io.lock_file_open_flags(nofollow=True) == (
        platform_io.lock_file_open_flags()
    )


def test_lock_file_open_flags_are_the_single_source_for_open_lock_file(tmp_path):
    flags = platform_io.lock_file_open_flags()
    assert flags & os.O_CREAT and flags & os.O_RDWR
    for name in ("O_CLOEXEC", "O_BINARY"):
        constant = getattr(os, name, None)
        if constant is not None:
            assert flags & constant == constant
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    assert platform_io.lock_file_open_flags(nofollow=True) == flags | nofollow

    descriptor = platform_io.open_lock_file(tmp_path / "nested" / "a.lock")
    try:
        assert (tmp_path / "nested" / "a.lock").is_file()
    finally:
        os.close(descriptor)


def test_directory_privacy_is_a_mode_question_on_posix_and_unmeasured_on_windows():
    """Windows has no POSIX mode bits, so a mode test reports nothing at all.

    Python synthesizes ``st_mode`` on Windows from the read-only attribute:
    0o777 when writable, 0o555 when read-only. ``0o777 & 0o077 == 0o077`` and
    ``0o555 & 0o077 == 0o055`` -- both non-zero -- so the old inline check
    raised "not private" for EVERY directory on that host, and ``os.chmod``
    could not clear it because there it only toggles read-only.
    """

    assert (
        platform_io.directory_privacy_backend("windows")
        == platform_io.DIRECTORY_PRIVACY_BACKEND_NONE
    )
    assert (
        platform_io.directory_privacy_backend("linux")
        == platform_io.DIRECTORY_PRIVACY_BACKEND_POSIX_MODE
    )

    for windows_mode in (stat.S_IFDIR | 0o777, stat.S_IFDIR | 0o555):
        metadata = SimpleNamespace(st_mode=windows_mode)
        # The POSIX reading of the very same synthesized mode is a hard "no".
        assert (
            platform_io.directory_is_private_to_current_user(metadata, "linux") is False
        )
        # Windows withholds a verdict instead of inventing one.
        assert (
            platform_io.directory_is_private_to_current_user(metadata, "windows") is None
        )


def test_directory_privacy_never_returns_true_for_an_unmeasured_host():
    """``None`` must stay distinct from ``True``: a pass has to be earned."""

    private = SimpleNamespace(st_mode=stat.S_IFDIR | 0o700)
    assert platform_io.directory_is_private_to_current_user(private, "linux") is True
    assert platform_io.directory_is_private_to_current_user(private, "windows") is None
    assert platform_io.directory_is_private_to_current_user(private, "windows") is not True
    for group_or_other in (0o750, 0o705, 0o770, 0o707, 0o777):
        metadata = SimpleNamespace(st_mode=stat.S_IFDIR | group_or_other)
        assert (
            platform_io.directory_is_private_to_current_user(metadata, "linux") is False
        )


def _readable_windows_libraries(monkeypatch, **kwargs):
    fake = _FakeWindowsLibraries(attributes=0x20, **kwargs)
    monkeypatch.setattr(platform_io.ctypes, "WinDLL", fake.windll, raising=False)
    monkeypatch.setattr(platform_io.ctypes, "get_last_error", fake.get_last_error, raising=False)
    return fake


def test_windows_relative_regular_file_transfers_exact_read_handle(monkeypatch):
    fake = _readable_windows_libraries(monkeypatch)
    conversions = []
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(
        open_osfhandle=lambda handle, flags: conversions.append((handle, flags)) or 321,
    ))
    descriptor = platform_io.open_windows_relative_regular_file_descriptor(0x1_0000_0009, "file.py")
    assert descriptor == 321
    args = fake.calls[0]
    assert args[2]._obj.RootDirectory == 0x1_0000_0009
    assert args[1] == 0x00100081  # READ_DATA | READ_ATTRIBUTES | SYNCHRONIZE
    assert args[5:9] == (0, 0x7, 0x1, 0x200060)
    assert conversions == [(0x1_0000_1234, os.O_RDONLY | getattr(os, "O_BINARY", 0))]
    assert [entry[0] for entry in fake.information_calls] == [9, 18]
    assert fake.closes == []  # ownership transferred once; CRT caller closes


@pytest.mark.parametrize("failure", ["directory", "device", "reparse", "unknown_reparse", "identity", "attributes", "conversion", "native"])
def test_windows_relative_read_failure_closes_only_acquired_handle(monkeypatch, failure):
    kwargs = {"status": -1} if failure == "native" else {}
    fake = _readable_windows_libraries(monkeypatch, **kwargs)
    original = fake.kernel32.GetFileInformationByHandleEx.function

    def information(handle, info_class, output, size):
        result = original(handle, info_class, output, size)
        if info_class == 9:
            if failure == "directory":
                output._obj.FileAttributes = 0x10
            elif failure == "device":
                output._obj.FileAttributes = 0x40
            elif failure == "reparse":
                output._obj.ReparseTag = 0xA000000C
            elif failure == "unknown_reparse":
                output._obj.FileAttributes = 0x400
            elif failure == "attributes":
                return 0
        elif failure == "identity":
            output._obj.volume_serial_number = 0
        return result

    fake.kernel32.GetFileInformationByHandleEx.function = information
    conversions = []

    def convert(handle, flags):
        conversions.append((handle, flags))
        raise OSError("CRT conversion failed")

    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(open_osfhandle=convert))
    with pytest.raises(OSError):
        platform_io.open_windows_relative_regular_file_descriptor(0x1_0000_0009, "file.py")
    assert fake.closes == ([] if failure == "native" else [0x1_0000_1234])
    assert bool(conversions) is (failure == "conversion")


def test_owned_windows_handle_detach_transfers_once():
    closes = []
    owned = platform_io.OwnedWindowsHandle(0x1_0000_1234, lambda handle: closes.append(handle.value) or 1, lambda: 5)
    assert owned.detach() == 0x1_0000_1234
    owned.close()
    assert owned.closed and closes == []
    with pytest.raises(ValueError):
        owned.detach()


# ---------------------------------------------------------------------------
# Windows sandbox policy facade.  platform_io owns the typed decisions;
# windows_mxc and windows_appcontainer own the probing.  Every Windows path
# below stubs the primitives or builds a real package tree for windows_mxc, so
# a Windows host and a Linux host give the same answer.
# ---------------------------------------------------------------------------

_NOT_WINDOWS = "platform_not_windows"
_APPCONTAINER_UNAVAILABLE = "win32_appcontainer_unavailable"
_MXC_NOT_READY = "mxc_runtime_not_ready"
_READINESS_CASES = [
    pytest.param(False, True, (_APPCONTAINER_UNAVAILABLE,), id="appcontainer-unavailable"),
    pytest.param(True, False, (_MXC_NOT_READY,), id="mxc-not-ready"),
    pytest.param(False, False, (_APPCONTAINER_UNAVAILABLE, _MXC_NOT_READY), id="both-missing"),
]


def _appcontainer_probe(available):
    reason = None if available else windows_appcontainer.AppContainerReason.PLATFORM_UNSUPPORTED
    return windows_appcontainer.AppContainerProbe(
        available=available, reason=reason, detail="stubbed host probe"
    )


def _mxc_readiness(root, runtime, *, ready=None):
    return windows_mxc.MxcReadiness(
        ready=runtime is not None if ready is None else ready,
        package_root=root,
        pinned_sdk_name=windows_mxc.PINNED_MXC_SDK_NAME,
        pinned_sdk_version=windows_mxc.PINNED_MXC_SDK_VERSION,
        sdk_name=windows_mxc.PINNED_MXC_SDK_NAME,
        sdk_version=windows_mxc.PINNED_MXC_SDK_VERSION,
        architecture="win32-x64",
        wxc_path=runtime,
        host_prep_path=None,
        failures=() if runtime is not None else ("stubbed runtime is not ready",),
        evidence=(),
    )


def _stub_primitives(monkeypatch, appcontainer, mxc):
    calls = []

    def probe():
        calls.append(("appcontainer",))
        return appcontainer

    def probe_mxc_runtime(package_root, host_machine=None, **_kwargs):
        calls.append(("mxc", package_root, host_machine))
        return mxc

    monkeypatch.setattr(windows_appcontainer, "probe", probe)
    monkeypatch.setattr(windows_mxc, "probe_mxc_runtime", probe_mxc_runtime)
    return calls


def _forbid_child_environment(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("no child environment may be built for this decision")

    monkeypatch.setattr(windows_appcontainer, "appcontainer_child_environment", forbidden)


def _write_mxc_package(root, *, version=None, architecture_directory="x64"):
    binary = root / "bin" / architecture_directory / windows_mxc.WXC_EXEC_BINARY_NAME
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"MZ" + bytes(62))
    metadata = {
        "name": windows_mxc.PINNED_MXC_SDK_NAME,
        "version": version or windows_mxc.PINNED_MXC_SDK_VERSION,
    }
    (root / "package.json").write_text(json.dumps(metadata), encoding="utf-8")
    return root


@pytest.mark.parametrize("platform_name", ["linux", "macos", "freebsd"])
def test_windows_sandbox_readiness_off_windows_fails_closed_without_probing(
    monkeypatch, tmp_path, platform_name
):
    runtime = tmp_path / windows_mxc.WXC_EXEC_BINARY_NAME
    calls = _stub_primitives(
        monkeypatch, _appcontainer_probe(True), _mxc_readiness(tmp_path, runtime)
    )

    readiness = platform_io.windows_sandbox_readiness(
        tmp_path / "missing-sdk", platform_name=platform_name
    )

    assert calls == []
    assert readiness.ready is False
    assert readiness.causes == (_NOT_WINDOWS,)
    assert readiness.appcontainer is None
    assert readiness.mxc is None
    assert readiness.runtime_path is None


def test_windows_sandbox_readiness_delegates_to_both_primitives(monkeypatch, tmp_path):
    runtime = tmp_path / "bin" / "x64" / windows_mxc.WXC_EXEC_BINARY_NAME
    appcontainer = _appcontainer_probe(True)
    mxc = _mxc_readiness(tmp_path, runtime)
    calls = _stub_primitives(monkeypatch, appcontainer, mxc)
    sdk_root = tmp_path / "sdk"

    readiness = platform_io.windows_sandbox_readiness(
        sdk_root, platform_name="windows", host_machine="ARM64"
    )

    assert calls == [("appcontainer",), ("mxc", sdk_root, "ARM64")]
    assert readiness.ready is True
    assert readiness.causes == ()
    assert readiness.appcontainer is appcontainer
    assert readiness.mxc is mxc
    assert readiness.runtime_path == runtime


@pytest.mark.parametrize(("appcontainer_available", "mxc_ready", "causes"), _READINESS_CASES)
def test_windows_sandbox_readiness_needs_appcontainer_and_mxc_together(
    monkeypatch, tmp_path, appcontainer_available, mxc_ready, causes
):
    runtime = tmp_path / windows_mxc.WXC_EXEC_BINARY_NAME if mxc_ready else None
    _stub_primitives(
        monkeypatch,
        _appcontainer_probe(appcontainer_available),
        _mxc_readiness(tmp_path, runtime),
    )

    readiness = platform_io.windows_sandbox_readiness(tmp_path, platform_name="windows")

    assert readiness.ready is False
    assert readiness.causes == causes
    assert readiness.runtime_path is None


def test_windows_sandbox_readiness_rejects_a_ready_mxc_result_without_a_runtime_path(
    monkeypatch, tmp_path
):
    _stub_primitives(
        monkeypatch, _appcontainer_probe(True), _mxc_readiness(tmp_path, None, ready=True)
    )

    readiness = platform_io.windows_sandbox_readiness(tmp_path, platform_name="windows")

    assert readiness.ready is False
    assert readiness.causes == (_MXC_NOT_READY,)
    assert readiness.runtime_path is None


@pytest.mark.parametrize(
    "error",
    [
        ImportError("primitive missing"),
        OSError(errno.EIO, "probe io"),
        RuntimeError("probe broke"),
        TypeError("probe misused"),
        ValueError("probe rejected"),
        AttributeError("probe api drift"),
    ],
    ids=lambda error: type(error).__name__,
)
def test_windows_sandbox_readiness_treats_a_raising_primitive_as_not_ready(
    monkeypatch, tmp_path, error
):
    def raising(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(windows_appcontainer, "probe", raising)
    monkeypatch.setattr(windows_mxc, "probe_mxc_runtime", raising)

    readiness = platform_io.windows_sandbox_readiness(tmp_path, platform_name="windows")

    assert readiness.ready is False
    assert readiness.causes == (_APPCONTAINER_UNAVAILABLE, _MXC_NOT_READY)
    assert readiness.appcontainer is None
    assert readiness.mxc is None


def test_windows_sandbox_readiness_probes_each_primitive_independently(monkeypatch, tmp_path):
    mxc = _mxc_readiness(tmp_path, tmp_path / windows_mxc.WXC_EXEC_BINARY_NAME)
    _stub_primitives(monkeypatch, _appcontainer_probe(True), mxc)

    def raising():
        raise OSError(errno.EIO, "probe io")

    monkeypatch.setattr(windows_appcontainer, "probe", raising)

    readiness = platform_io.windows_sandbox_readiness(tmp_path, platform_name="windows")

    assert readiness.ready is False
    assert readiness.causes == (_APPCONTAINER_UNAVAILABLE,)
    assert readiness.appcontainer is None
    assert readiness.mxc is mxc
    assert readiness.runtime_path is None


def test_windows_sandbox_readiness_reports_the_real_pinned_mxc_runtime(monkeypatch, tmp_path):
    monkeypatch.setattr(windows_appcontainer, "probe", lambda: _appcontainer_probe(True))
    root = _write_mxc_package(tmp_path / "sdk")

    readiness = platform_io.windows_sandbox_readiness(
        root, platform_name="windows", host_machine="AMD64"
    )

    expected = (root / "bin" / "x64" / windows_mxc.WXC_EXEC_BINARY_NAME).resolve()
    assert readiness.ready is True
    assert readiness.mxc == windows_mxc.probe_mxc_runtime(root, "AMD64")
    assert readiness.runtime_path == expected


@pytest.mark.parametrize(
    ("version", "architecture_directory", "host_machine", "failure"),
    [
        pytest.param("0.6.0", "x64", "AMD64", "pinned SDK version mismatch", id="sdk-version"),
        pytest.param(
            None, "x64", "ARM64", "exists only for architecture win32-x64", id="architecture"
        ),
        pytest.param(None, "x64", "x86", "unsupported host architecture", id="unsupported-host"),
    ],
)
def test_windows_sandbox_readiness_fails_closed_on_real_mxc_mismatches(
    monkeypatch, tmp_path, version, architecture_directory, host_machine, failure
):
    monkeypatch.setattr(windows_appcontainer, "probe", lambda: _appcontainer_probe(True))
    root = _write_mxc_package(
        tmp_path / "sdk", version=version, architecture_directory=architecture_directory
    )

    readiness = platform_io.windows_sandbox_readiness(
        root, platform_name="windows", host_machine=host_machine
    )

    assert readiness.ready is False
    assert readiness.causes == (_MXC_NOT_READY,)
    assert readiness.runtime_path is None
    assert any(failure in reported for reported in readiness.mxc.failures)


@pytest.mark.parametrize(
    ("relative_root", "failure"),
    [
        pytest.param("", "package root is not configured", id="unconfigured"),
        pytest.param("absent-sdk", "missing SDK package metadata", id="absent"),
    ],
)
def test_windows_sandbox_readiness_fails_closed_without_an_installed_sdk(
    monkeypatch, tmp_path, relative_root, failure
):
    monkeypatch.setattr(windows_appcontainer, "probe", lambda: _appcontainer_probe(True))
    package_root = tmp_path / relative_root if relative_root else relative_root

    readiness = platform_io.windows_sandbox_readiness(
        package_root, platform_name="windows", host_machine="AMD64"
    )

    assert readiness.ready is False
    assert readiness.causes == (_MXC_NOT_READY,)
    assert any(failure in reported for reported in readiness.mxc.failures)


def test_windows_sandbox_environment_delegates_to_appcontainer_policy(monkeypatch):
    seen = []
    adjusted = {"PATH": "p", "LOCALAPPDATA": "L"}

    def child_environment(environment, **_kwargs):
        seen.append(environment)
        return adjusted

    monkeypatch.setattr(windows_appcontainer, "appcontainer_child_environment", child_environment)
    supplied = {"PATH": "p"}

    assert platform_io.windows_sandbox_environment(supplied, platform_name="windows") is adjusted
    assert platform_io.windows_sandbox_environment(None, platform_name="windows") is adjusted
    assert seen == [supplied, None]


@pytest.mark.parametrize("platform_name", ["linux", "macos"])
def test_windows_sandbox_environment_is_untouched_off_windows(monkeypatch, platform_name):
    _forbid_child_environment(monkeypatch)
    supplied = {"PATH": "/usr/bin"}

    assert platform_io.windows_sandbox_environment(supplied, platform_name=platform_name) is supplied
    assert supplied == {"PATH": "/usr/bin"}
    assert platform_io.windows_sandbox_environment(None, platform_name=platform_name) is None


def test_windows_sandbox_environment_never_falls_back_to_the_unadjusted_environment(monkeypatch):
    def failing(*_args, **_kwargs):
        raise OSError(errno.EIO, "child environment unavailable")

    monkeypatch.setattr(windows_appcontainer, "appcontainer_child_environment", failing)

    with pytest.raises(OSError, match="child environment unavailable"):
        platform_io.windows_sandbox_environment({"PATH": "p"}, platform_name="windows")


def test_windows_sandbox_launch_decision_allows_a_ready_windows_stack(monkeypatch, tmp_path):
    runtime = tmp_path / "bin" / "x64" / windows_mxc.WXC_EXEC_BINARY_NAME
    _stub_primitives(monkeypatch, _appcontainer_probe(True), _mxc_readiness(tmp_path, runtime))
    adjusted = {"PATH": "p", "LOCALAPPDATA": "L"}
    monkeypatch.setattr(
        windows_appcontainer, "appcontainer_child_environment", lambda environment, **_kw: adjusted
    )

    decision = platform_io.windows_sandbox_launch_decision(
        tmp_path / "sdk", {"PATH": "p"}, platform_name="windows"
    )

    assert decision.allowed is True
    assert decision.causes == ()
    assert decision.executable == runtime
    assert decision.environment is adjusted
    assert decision.readiness.ready is True


@pytest.mark.parametrize(("appcontainer_available", "mxc_ready", "causes"), _READINESS_CASES)
def test_windows_sandbox_launch_decision_denies_without_side_effects_when_not_ready(
    monkeypatch, tmp_path, appcontainer_available, mxc_ready, causes
):
    runtime = tmp_path / windows_mxc.WXC_EXEC_BINARY_NAME if mxc_ready else None
    _stub_primitives(
        monkeypatch,
        _appcontainer_probe(appcontainer_available),
        _mxc_readiness(tmp_path, runtime),
    )
    _forbid_child_environment(monkeypatch)
    supplied = {"PATH": "p"}

    decision = platform_io.windows_sandbox_launch_decision(
        tmp_path, supplied, platform_name="windows"
    )

    assert decision.allowed is False
    assert decision.causes == causes
    assert decision.executable is None
    assert decision.environment is supplied
    assert decision.readiness.ready is False


@pytest.mark.parametrize("platform_name", ["linux", "macos"])
def test_windows_sandbox_launch_decision_leaves_off_windows_launches_alone(
    monkeypatch, tmp_path, platform_name
):
    runtime = tmp_path / windows_mxc.WXC_EXEC_BINARY_NAME
    calls = _stub_primitives(
        monkeypatch, _appcontainer_probe(True), _mxc_readiness(tmp_path, runtime)
    )
    _forbid_child_environment(monkeypatch)
    supplied = {"PATH": "/usr/bin"}

    decision = platform_io.windows_sandbox_launch_decision(
        tmp_path, supplied, platform_name=platform_name
    )

    assert calls == []
    assert decision.allowed is False
    assert decision.causes == (_NOT_WINDOWS,)
    assert decision.executable is None
    assert decision.environment is supplied
    assert supplied == {"PATH": "/usr/bin"}


def test_windows_sandbox_records_are_typed_and_immutable(tmp_path):
    assert platform_io.WINDOWS_SANDBOX_CAUSE_PLATFORM_NOT_WINDOWS == _NOT_WINDOWS
    assert platform_io.WINDOWS_SANDBOX_CAUSE_APPCONTAINER_UNAVAILABLE == _APPCONTAINER_UNAVAILABLE
    assert platform_io.WINDOWS_SANDBOX_CAUSE_MXC_NOT_READY == _MXC_NOT_READY
    readiness = platform_io.windows_sandbox_readiness(tmp_path, platform_name="linux")
    decision = platform_io.windows_sandbox_launch_decision(tmp_path, None, platform_name="linux")

    assert isinstance(readiness, platform_io.WindowsSandboxReadiness)
    assert isinstance(decision, platform_io.WindowsSandboxLaunchDecision)
    with pytest.raises(AttributeError):
        readiness.ready = True
    with pytest.raises(AttributeError):
        decision.allowed = True


def _import_time_imports(tree):
    names = set()
    pending = list(tree.body)
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Name)
            and node.test.id == "TYPE_CHECKING"
        ):
            pending.extend(node.orelse)
            continue
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
            names.update(alias.name for alias in node.names)
        pending.extend(ast.iter_child_nodes(node))
    return names


def test_windows_primitives_load_lazily_so_off_windows_imports_are_unchanged():
    tree = ast.parse(Path(platform_io.__file__).read_text(encoding="utf-8"))

    eager = {name.rsplit(".", 1)[-1] for name in _import_time_imports(tree)}

    assert not eager & {"windows_appcontainer", "windows_mxc"}


def test_platform_io_delegates_to_the_primitives_instead_of_restating_their_policy():
    source = Path(platform_io.__file__).read_text(encoding="utf-8")

    for delegated in ("probe_mxc_runtime", "appcontainer_child_environment"):
        assert delegated in source
    for owned_by_a_primitive in (
        "mxcRuntime",
        "package.json",
        "@microsoft/mxc-sdk",
        "CreateAppContainerProfile",
    ):
        assert owned_by_a_primitive not in source


# --- repo-local worker sandbox: plan, slot lease and containment -----------

_SANDBOX_STORAGE_NAMES = (
    "home",
    "temp",
    "state",
    "cache",
    "config",
    "workspace",
    "logs",
)


def _sandbox_slot_repo(tmp_path):
    """One repository root on whatever volume pytest handed us.

    Never a fixed drive and never the user profile: the model under test has to
    work wherever the repository actually lives.
    """

    repo = tmp_path / "repo"
    repo.mkdir()
    return repo


def _sandbox_slot_link_dir(link, target):
    """Plant a directory link at ``link`` pointing at ``target``.

    A Windows junction needs no privilege, which is why it is tried first
    there; ``os.symlink`` covers every other host.  Only the privilege denial
    (winerror 1314) is a genuine capability gap and skips; every other error is
    re-raised, because a skip that swallows a real failure reports a negative
    fixture as satisfied when it never ran.
    """

    if os.name == "nt":
        create_junction = getattr(importlib.import_module("_winapi"), "CreateJunction", None)
        if create_junction is not None:
            create_junction(str(target), str(link))
            return
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError as exc:
        if getattr(exc, "winerror", None) != 1314:
            raise
        pytest.skip("sandbox_capability_denied:symlink (winerror 1314)")


def _sandbox_slot_config_sentinel(repo):
    """A canonical ``.aiworkhub/config`` file cleanup must never reach."""

    config = repo / ".aiworkhub" / "config"
    config.mkdir(parents=True)
    sentinel = config / "settings.json"
    sentinel.write_text("{}", encoding="utf-8")
    return config, sentinel


def _sandbox_slot_containment_key(path):
    return os.path.normcase(os.path.realpath(path, strict=True))


def test_sandbox_slot_dirs_and_root_are_repo_local_on_any_volume(tmp_path):
    repo = _sandbox_slot_repo(tmp_path)

    assert platform_io.SANDBOX_SLOT_DIRS == _SANDBOX_STORAGE_NAMES
    expected = Path(os.path.realpath(repo)) / ".aiworkhub" / "runtime" / "sandboxes"
    assert platform_io.sandbox_root(repo) == expected
    assert platform_io.sandbox_root(str(repo)) == expected


def test_sandbox_plan_reason_codes_are_exactly_the_declared_typed_set():
    assert platform_io.SANDBOX_REASONS == (
        "sandbox_slots_exhausted",
        "sandbox_path_escapes_root",
        "sandbox_reparse_point",
        "sandbox_unc_path",
        "sandbox_case_alias",
        "sandbox_lease_conflict",
    )

    refused = platform_io.SandboxUnavailable("sandbox_slots_exhausted", "4_slots_all_held")

    assert isinstance(refused, RuntimeError)
    assert refused.reason == "sandbox_slots_exhausted"
    assert "sandbox_slots_exhausted" in str(refused)


@pytest.mark.parametrize(("cores", "expected"), [(1, 1), (2, 1), (8, 7), (32, 31)])
def test_sandbox_slot_count_leaves_a_core_of_headroom(cores, expected):
    observed = platform_io.sandbox_slot_count(cores)

    assert observed == expected
    assert observed >= 1
    if cores >= 2:
        assert cores - observed >= 1


def test_sandbox_slot_count_is_derived_from_the_observed_core_count(monkeypatch):
    monkeypatch.setattr(platform_io.os, "cpu_count", lambda: 12)
    assert platform_io.sandbox_slot_count() == 11

    monkeypatch.setattr(platform_io.os, "cpu_count", lambda: None)
    assert platform_io.sandbox_slot_count() == 1

    monkeypatch.setattr(platform_io.os, "cpu_count", lambda: 1)
    assert platform_io.sandbox_slot_count() == 1


def test_sandbox_plan_paths_and_environment_resolve_inside_the_slot(tmp_path):
    repo = _sandbox_slot_repo(tmp_path)
    lease = platform_io.lease_sandbox_slot(repo, "request-a", slot_count=2)
    try:
        plan = lease.plan

        assert tuple(field.name for field in dataclasses.fields(plan)) == (
            "repo_root",
            "sandbox_root",
            "slot_id",
            "slot_root",
            *_SANDBOX_STORAGE_NAMES,
            "request_id",
        )
        assert plan.repo_root == Path(os.path.realpath(repo))
        assert plan.sandbox_root == platform_io.sandbox_root(repo)
        assert plan.request_id == "request-a"
        assert plan.slot_id == "slot-00"

        # repo_root, sandbox_root and slot_root are the anchors containment is
        # measured against; every storage path and every exported value has to
        # resolve strictly inside the slot.
        slot_key = _sandbox_slot_containment_key(plan.slot_root)
        slots_key = _sandbox_slot_containment_key(plan.sandbox_root / "slots")
        assert slot_key.startswith(slots_key + os.sep)
        candidates = [getattr(plan, name) for name in platform_io.SANDBOX_SLOT_DIRS]
        candidates.append(plan.xdg_data_home)
        candidates.extend(Path(value) for value in plan.environment().values())
        for candidate in candidates:
            resolved = _sandbox_slot_containment_key(candidate)
            assert resolved != slot_key
            assert resolved.startswith(slot_key + os.sep)

        environment = plan.environment()
        expected_keys = {
            "HOME",
            "TEMP",
            "TMP",
            "TMPDIR",
            "XDG_CONFIG_HOME",
            "XDG_CACHE_HOME",
            "XDG_STATE_HOME",
            "XDG_DATA_HOME",
        }
        if platform_io.is_windows():
            expected_keys.add("USERPROFILE")
            assert environment["USERPROFILE"] == str(plan.home)
        assert set(environment) == expected_keys
        assert environment["HOME"] == str(plan.home)
        assert environment["TEMP"] == str(plan.temp)
        assert environment["TMP"] == str(plan.temp)
        assert environment["TMPDIR"] == str(plan.temp)
        assert environment["XDG_CONFIG_HOME"] == str(plan.config)
        assert environment["XDG_CACHE_HOME"] == str(plan.cache)
        assert environment["XDG_STATE_HOME"] == str(plan.state)
        assert environment["XDG_DATA_HOME"] == str(plan.home / ".local" / "share")

        with pytest.raises(dataclasses.FrozenInstanceError):
            plan.home = tmp_path
    finally:
        lease.release()


def test_sandbox_plan_slots_are_mutually_isolated(tmp_path):
    repo = _sandbox_slot_repo(tmp_path)
    first = platform_io.lease_sandbox_slot(repo, "request-a", slot_count=2)
    second = platform_io.lease_sandbox_slot(repo, "request-b", slot_count=2)
    try:
        assert first.plan.slot_id != second.plan.slot_id
        first_key = _sandbox_slot_containment_key(first.plan.slot_root)
        second_key = _sandbox_slot_containment_key(second.plan.slot_root)

        assert not first_key.startswith(second_key + os.sep)
        assert not second_key.startswith(first_key + os.sep)
        assert set(first.plan.environment().values()) & set(second.plan.environment().values()) == set()
    finally:
        second.release()
        first.release()


@pytest.mark.skipif(
    not platform_io.posix_path_modes_supported(),
    reason="POSIX mode bits are not an authority on Windows; this card writes no ACL",
)
def test_sandbox_slot_dirs_are_created_owner_only(tmp_path):
    repo = _sandbox_slot_repo(tmp_path)
    lease = platform_io.lease_sandbox_slot(repo, "request", slot_count=1)
    try:
        for name in platform_io.SANDBOX_SLOT_DIRS:
            directory = getattr(lease.plan, name)
            assert stat.S_IMODE(os.stat(directory).st_mode) == 0o700
        assert stat.S_IMODE(os.stat(lease.plan.slot_root).st_mode) == 0o700
    finally:
        lease.release()


def test_sandbox_slot_lease_never_hands_one_slot_to_two_live_leases(tmp_path):
    repo = _sandbox_slot_repo(tmp_path)
    workers = 8
    start = threading.Barrier(workers)
    guard = threading.Lock()
    leases = []
    failures = []

    def claim(index):
        try:
            start.wait(timeout=60)
            lease = platform_io.lease_sandbox_slot(
                repo, f"request-{index}", slot_count=workers
            )
        except BaseException as exc:  # recorded, then asserted on the main thread
            with guard:
                failures.append(repr(exc))
            return
        with guard:
            leases.append(lease)

    threads = [threading.Thread(target=claim, args=(index,)) for index in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)

    try:
        assert failures == []
        assert [thread.is_alive() for thread in threads] == [False] * workers
        slot_ids = [lease.plan.slot_id for lease in leases]
        assert len(slot_ids) == workers
        assert len(set(slot_ids)) == workers
        assert len({str(lease.plan.slot_root) for lease in leases}) == workers

        with pytest.raises(platform_io.SandboxUnavailable) as exhausted:
            platform_io.lease_sandbox_slot(repo, "one-too-many", slot_count=workers)

        assert exhausted.value.reason == "sandbox_slots_exhausted"
    finally:
        for lease in leases:
            lease.release()


def test_sandbox_slot_lease_reuses_a_slot_once_its_lease_is_released(tmp_path):
    repo = _sandbox_slot_repo(tmp_path)
    first = platform_io.lease_sandbox_slot(repo, "request-a", slot_count=1)
    leftover = first.plan.workspace / "leftover.txt"
    leftover.write_text("residue", encoding="utf-8")
    first.release()

    assert not first.lease_path.exists()

    second = platform_io.lease_sandbox_slot(repo, "request-b", slot_count=1)
    try:
        assert second.plan.slot_id == first.plan.slot_id
        assert not leftover.exists()
        assert second.lease_path.exists()
    finally:
        second.release()


def test_sandbox_slot_lease_reclaims_a_provably_dead_holder_and_cleans_the_slot(
    tmp_path, monkeypatch
):
    repo = _sandbox_slot_repo(tmp_path)
    root = platform_io.sandbox_root(repo)
    (root / "leases").mkdir(parents=True)
    residue_dir = root / "slots" / "slot-00" / "logs" / "nested"
    residue_dir.mkdir(parents=True)
    residue = residue_dir / "crash.log"
    residue.write_text("residue", encoding="utf-8")
    (root / "leases" / "slot-00.lease").write_text(
        json.dumps({"pid": 4242424, "request_id": "dead-request", "slot_id": "slot-00"}),
        encoding="utf-8",
    )
    probed = []

    def never_alive(pid):
        probed.append(pid)
        return False

    monkeypatch.setattr(platform_io, "process_is_alive", never_alive)

    lease = platform_io.lease_sandbox_slot(repo, "next-request", slot_count=1)
    try:
        assert probed == [4242424]
        assert lease.plan.slot_id == "slot-00"
        assert not residue.exists()
        assert not residue_dir.exists()
        assert list(lease.plan.logs.iterdir()) == []
        claimed = json.loads(lease.lease_path.read_text(encoding="utf-8"))
        assert claimed["pid"] == os.getpid()
        assert claimed["request_id"] == "next-request"
        assert claimed["slot_id"] == "slot-00"
        assert isinstance(claimed["claimed_at_epoch"], float)
    finally:
        lease.release()


def test_sandbox_slot_lease_never_reclaims_a_lease_held_by_a_live_pid(tmp_path):
    repo = _sandbox_slot_repo(tmp_path)
    root = platform_io.sandbox_root(repo)
    (root / "leases").mkdir(parents=True)
    held = root / "leases" / "slot-00.lease"
    record = json.dumps({"pid": os.getpid(), "request_id": "live-request", "slot_id": "slot-00"})
    held.write_text(record, encoding="utf-8")

    with pytest.raises(platform_io.SandboxUnavailable) as exhausted:
        platform_io.lease_sandbox_slot(repo, "second-request", slot_count=1)

    assert exhausted.value.reason == "sandbox_slots_exhausted"
    assert held.read_text(encoding="utf-8") == record


def test_sandbox_slot_lease_never_reclaims_a_lease_that_names_nobody(tmp_path):
    repo = _sandbox_slot_repo(tmp_path)
    root = platform_io.sandbox_root(repo)
    (root / "leases").mkdir(parents=True)
    unreadable = root / "leases" / "slot-00.lease"
    unreadable.write_text("not-a-lease-record", encoding="utf-8")

    with pytest.raises(platform_io.SandboxUnavailable) as exhausted:
        platform_io.lease_sandbox_slot(repo, "request", slot_count=1)

    assert exhausted.value.reason == "sandbox_slots_exhausted"
    assert unreadable.read_text(encoding="utf-8") == "not-a-lease-record"


def test_sandbox_slot_liveness_never_raises_a_signal_at_the_holder():
    source = Path(platform_io.__file__).read_text(encoding="utf-8")
    marker = "AIWORKHUB_SANDBOX_SLOT_SECTION"

    assert source.count(marker) == 1
    section = source.split(marker, 1)[1]

    assert "os.kill" not in section
    assert "signal." not in section
    assert "killpg" not in section
    assert "process_is_alive" in section
    # The section's own prose names the host locations it refuses, so the scan
    # looks for the calls that would actually read them, not for the words.
    for host_derived in (
        "os.environ",
        "os.getenv",
        "environ[",
        "expanduser",
        "tempfile.",
        "Path.home(",
    ):
        assert host_derived not in section


def test_sandbox_slot_lease_reports_a_lease_conflict_when_the_claim_is_not_ours(
    tmp_path, monkeypatch
):
    repo = _sandbox_slot_repo(tmp_path)

    def foreign(_request_id, slot_id):
        return json.dumps(
            {"pid": os.getpid(), "request_id": "someone-else", "slot_id": slot_id}
        ).encode("utf-8")

    monkeypatch.setattr(platform_io, "_sandbox_lease_record", foreign)

    with pytest.raises(platform_io.SandboxUnavailable) as conflict:
        platform_io.lease_sandbox_slot(repo, "request", slot_count=1)

    assert conflict.value.reason == "sandbox_lease_conflict"


def test_sandbox_slot_lease_refuses_an_unnamed_request(tmp_path):
    repo = _sandbox_slot_repo(tmp_path)

    with pytest.raises(platform_io.SandboxUnavailable) as refused:
        platform_io.lease_sandbox_slot(repo, "", slot_count=1)

    assert refused.value.reason == "sandbox_lease_conflict"
    assert not (repo / ".aiworkhub").exists()


@pytest.mark.requires_symlink
def test_sandbox_slot_lease_refuses_a_reparse_point_planted_at_the_slot(tmp_path):
    repo = _sandbox_slot_repo(tmp_path)
    config, sentinel = _sandbox_slot_config_sentinel(repo)
    slots = platform_io.sandbox_root(repo) / "slots"
    slots.mkdir(parents=True)
    _sandbox_slot_link_dir(slots / "slot-00", config)

    with pytest.raises(platform_io.SandboxUnavailable) as refused:
        platform_io.lease_sandbox_slot(repo, "request", slot_count=1)

    assert refused.value.reason == "sandbox_reparse_point"
    assert sentinel.read_text(encoding="utf-8") == "{}"
    assert sorted(entry.name for entry in config.iterdir()) == ["settings.json"]
    assert list((platform_io.sandbox_root(repo) / "leases").glob("*.lease")) == []


@pytest.mark.requires_symlink
def test_sandbox_slot_lease_refuses_a_reparse_point_under_the_sandbox_root(tmp_path):
    repo = _sandbox_slot_repo(tmp_path)
    config, sentinel = _sandbox_slot_config_sentinel(repo)
    root = platform_io.sandbox_root(repo)
    root.mkdir(parents=True)
    _sandbox_slot_link_dir(root / "slots", config)

    with pytest.raises(platform_io.SandboxUnavailable) as refused:
        platform_io.lease_sandbox_slot(repo, "request", slot_count=1)

    assert refused.value.reason == "sandbox_reparse_point"
    assert sentinel.read_text(encoding="utf-8") == "{}"
    assert sorted(entry.name for entry in config.iterdir()) == ["settings.json"]


@pytest.mark.requires_symlink
def test_sandbox_slot_release_never_follows_a_reparse_point_out_of_the_slot(tmp_path):
    repo = _sandbox_slot_repo(tmp_path)
    config, sentinel = _sandbox_slot_config_sentinel(repo)
    lease = platform_io.lease_sandbox_slot(repo, "request", slot_count=1)
    _sandbox_slot_link_dir(lease.plan.workspace / "escape", config)

    with pytest.raises(platform_io.SandboxUnavailable) as refused:
        lease.release()

    assert refused.value.reason == "sandbox_reparse_point"
    assert sentinel.read_text(encoding="utf-8") == "{}"
    assert not lease.lease_path.exists()


@pytest.mark.parametrize(
    "root",
    [
        pytest.param(r"\\aiworkhub-share\repo", id="unc"),
        pytest.param(r"\\?\C:\repo", id="extended-device"),
        pytest.param(r"\\.\C:\repo", id="dos-device"),
        pytest.param("//aiworkhub-share/repo", id="unc-forward-slashes"),
    ],
)
def test_sandbox_slot_lease_refuses_a_unc_or_device_prefixed_repo_root(root):
    with pytest.raises(platform_io.SandboxUnavailable) as refused:
        platform_io.lease_sandbox_slot(root, "request", slot_count=1)

    assert refused.value.reason == "sandbox_unc_path"

    with pytest.raises(platform_io.SandboxUnavailable) as root_refused:
        platform_io.sandbox_root(root)

    assert root_refused.value.reason == "sandbox_unc_path"


def test_sandbox_slot_lease_refuses_a_case_alias_repo_root(tmp_path):
    repo = _sandbox_slot_repo(tmp_path)
    alias = tmp_path / repo.name.upper()

    with pytest.raises(platform_io.SandboxUnavailable) as refused:
        platform_io.lease_sandbox_slot(alias, "request", slot_count=1)

    assert refused.value.reason == "sandbox_case_alias"
    assert not (repo / ".aiworkhub").exists()
    assert sorted(entry.name for entry in tmp_path.iterdir()) == ["repo"]


def test_sandbox_slot_release_empties_the_seven_dirs_and_nothing_else(tmp_path):
    repo = _sandbox_slot_repo(tmp_path)
    config, config_sentinel = _sandbox_slot_config_sentinel(repo)
    repo_sentinel = repo / "README.md"
    repo_sentinel.write_text("repository root", encoding="utf-8")
    lease = platform_io.lease_sandbox_slot(repo, "request", slot_count=1)
    plan = lease.plan
    for name in platform_io.SANDBOX_SLOT_DIRS:
        (getattr(plan, name) / "scratch.txt").write_text(name, encoding="utf-8")
    nested = plan.workspace / "checkout" / "src"
    nested.mkdir(parents=True)
    (nested / "module.py").write_text("x = 1\n", encoding="utf-8")

    lease.release()

    for name in platform_io.SANDBOX_SLOT_DIRS:
        directory = getattr(plan, name)
        assert directory.is_dir()
        assert list(directory.iterdir()) == []
    assert plan.slot_root.is_dir()
    assert not lease.lease_path.exists()
    assert config_sentinel.read_text(encoding="utf-8") == "{}"
    assert sorted(entry.name for entry in config.iterdir()) == ["settings.json"]
    assert repo_sentinel.read_text(encoding="utf-8") == "repository root"
    assert "repo" in {entry.name for entry in tmp_path.iterdir()}

    lease.release()

    assert plan.slot_root.is_dir()


def test_sandbox_plan_never_derives_a_path_from_the_user_profile_or_temp(tmp_path, monkeypatch):
    repo = _sandbox_slot_repo(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    def refuse(*_args, **_kwargs):
        raise AssertionError("a sandbox path must never come from the host profile")

    monkeypatch.setattr(tempfile, "gettempdir", refuse)
    monkeypatch.setattr(Path, "home", refuse)
    for name in ("USERPROFILE", "LOCALAPPDATA", "HOME", "TEMP", "TMP", "TMPDIR"):
        monkeypatch.setenv(name, str(elsewhere))

    lease = platform_io.lease_sandbox_slot(repo, "request", slot_count=1)
    try:
        plan = lease.plan
        slot_key = _sandbox_slot_containment_key(plan.slot_root)
        values = [str(getattr(plan, name)) for name in platform_io.SANDBOX_SLOT_DIRS]
        values.extend(plan.environment().values())

        for value in values:
            assert str(elsewhere) not in value
            assert _sandbox_slot_containment_key(value).startswith(slot_key + os.sep)
        assert str(elsewhere) not in str(plan.slot_root)
        assert plan.environment()["TEMP"] == str(plan.temp)
    finally:
        lease.release()


def test_sandbox_slot_lease_reclaim_of_a_dead_holder_is_atomic_under_one_claim(
    tmp_path, monkeypatch
):
    """REPRODUCTION: reclaiming a stale lease was check-then-unlink by path, so
    two claimers that had both read the same dead lease were handed one slot and
    the second wiped the first one's workspace. One claimer is parked right after
    its liveness check and the other is started behind it: the reclaim has to be
    serialized per slot, not merely retried."""

    repo = _sandbox_slot_repo(tmp_path)
    root = platform_io.sandbox_root(repo)
    (root / "leases").mkdir(parents=True)
    (root / "leases" / "slot-00.lease").write_text(
        json.dumps({"pid": 4242424, "request_id": "dead-request", "slot_id": "slot-00"}),
        encoding="utf-8",
    )
    # Only this process is alive: the seeded holder reads dead, so the slot is
    # genuinely reclaimable, while a lease a competing claimer has just written
    # reads live and must never be reclaimed out from under them.
    monkeypatch.setattr(platform_io, "process_is_alive", lambda pid: pid == os.getpid())

    real_reclaimable = platform_io._sandbox_lease_is_reclaimable
    parked = threading.Event()
    resume = threading.Event()

    def park_the_first_checker(lease_path):
        verdict = real_reclaimable(lease_path)
        if threading.current_thread().name == "parked-claimer" and not parked.is_set():
            parked.set()
            assert resume.wait(timeout=60)
        return verdict

    monkeypatch.setattr(
        platform_io, "_sandbox_lease_is_reclaimable", park_the_first_checker
    )

    guard = threading.Lock()
    leases = {}
    refusals = {}

    def claim(name):
        try:
            lease = platform_io.lease_sandbox_slot(repo, name, slot_count=1)
        except platform_io.SandboxUnavailable as exc:
            with guard:
                refusals[name] = exc.reason
            return
        except BaseException as exc:  # recorded, then asserted on the main thread
            with guard:
                refusals[name] = repr(exc)
            return
        with guard:
            leases[name] = lease
        try:
            (lease.plan.workspace / "live-work.txt").write_text(name, encoding="utf-8")
        except BaseException as exc:  # recorded, then asserted on the main thread
            with guard:
                refusals[name] = repr(exc)

    first = threading.Thread(target=claim, args=("req-parked",), name="parked-claimer")
    second = threading.Thread(target=claim, args=("req-racer",), name="racing-claimer")
    first.start()
    assert parked.wait(timeout=60)
    second.start()
    # The racer either completes its own reclaim while the first claimer is still
    # parked -- the defect -- or waits for that claimer to finish with the slot
    # -- the fix. One bounded wait tells the two apart without making the test
    # depend on winning a race.
    second.join(timeout=0.75)
    resume.set()
    first.join(timeout=60)
    second.join(timeout=60)

    try:
        assert not first.is_alive()
        assert not second.is_alive()
        assert len(leases) == 1, f"one slot handed to two live leases: {sorted(leases)}"
        [(winner, lease)] = leases.items()
        loser = ({"req-parked", "req-racer"} - {winner}).pop()
        assert refusals == {loser: "sandbox_slots_exhausted"}
        assert lease.plan.slot_id == "slot-00"
        claimed = json.loads(lease.lease_path.read_text(encoding="utf-8"))
        assert claimed["request_id"] == winner
        assert claimed["pid"] == os.getpid()
        assert (lease.plan.workspace / "live-work.txt").read_text(encoding="utf-8") == winner
    finally:
        for held in leases.values():
            held.release()
