from __future__ import annotations

import errno
import io
import importlib.util
import os
import stat
import threading
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiworkhub import _platform_process, platform_io


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

        def nt_create(*args):
            self.calls.append(args)
            args[0]._obj.value = 0x1_0000_1234
            return status

        def rtl_status_to_dos_error(status_arg):
            self.nt_statuses.append(status_arg)
            return dos_error

        def information(handle, info_class, output, size):
            assert handle == 0x1_0000_1234
            if info_class == 9:
                value = output._obj
                value.FileAttributes = attributes
                value.ReparseTag = reparse_tag
            else:
                assert info_class == 18
                value = output._obj
                value.VolumeSerialNumber = volume
                value.FileId[:] = file_id
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
