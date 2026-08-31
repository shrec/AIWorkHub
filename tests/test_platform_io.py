from __future__ import annotations

import errno
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from aiworkhub import platform_io


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
        attributes=0x10,
        reparse_tag=0,
        volume=7,
        file_id=bytes(range(1, 17)),
    ):
        self.calls = []
        self.closes = []
        self.close_results = [1]
        self.last_error = 5

        def nt_create(*args):
            self.calls.append(args)
            args[0]._obj.value = 0x1_0000_1234
            return status

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

        def close(handle):
            self.closes.append(handle.value)
            return self.close_results.pop(0)

        self.ntdll = SimpleNamespace(
            NtCreateFile=_FakeWindowsFunction(nt_create),
            RtlNtStatusToDosError=_FakeWindowsFunction(lambda _status: 5),
        )
        self.kernel32 = SimpleNamespace(
            GetFileInformationByHandleEx=_FakeWindowsFunction(information),
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
def test_relative_child_rejects_invalid_parent_before_native_call(monkeypatch, parent):
    monkeypatch.setattr(
        platform_io.ctypes,
        "WinDLL",
        lambda *_args, **_kwargs: pytest.fail("native call"),
        raising=False,
    )
    with pytest.raises(ValueError):
        platform_io.open_windows_relative_child_directory(parent, "child")


def test_ntcreatefile_relative_child_preserves_exact_abi_and_authority(monkeypatch):
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


def test_native_canary_relative_child_directory(tmp_path):
    if os.name != "nt":
        pytest.skip("native Windows canary")
    child = tmp_path / "child"
    child.mkdir()
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
        create_file(str(tmp_path), 0x80, 0x7, None, 3, 0x02200000, None)
    )
    assert parent_handle not in (0, platform_io._INVALID_HANDLE_VALUE)
    try:
        owned = platform_io.open_windows_relative_child_directory(
            parent_handle,
            "child",
        )
        assert owned.value not in (0, platform_io._INVALID_HANDLE_VALUE)
        owned.close()
        owned.close()
        marker = child / "usable.txt"
        marker.write_text("ok", encoding="utf-8")
        assert marker.read_text(encoding="utf-8") == "ok"
        assert [path.name for path in child.iterdir()] == ["usable.txt"]
    finally:
        assert close_handle(parent_handle)


def test_background_process_launch_kwargs_dispatches_exactly_by_platform(monkeypatch):
    startup = SimpleNamespace(dwFlags=0, wShowWindow=None)
    monkeypatch.setattr(
        platform_io.subprocess, "STARTUPINFO", lambda: startup, raising=False
    )
    monkeypatch.setattr(
        platform_io.subprocess, "STARTF_USESHOWWINDOW", 4, raising=False
    )
    monkeypatch.setattr(platform_io.subprocess, "SW_HIDE", 0, raising=False)
    monkeypatch.setattr(platform_io.subprocess, "CREATE_NO_WINDOW", 8, raising=False)

    assert platform_io.background_process_launch_kwargs("nt") == {
        "creationflags": 8,
        "startupinfo": startup,
    }
    assert startup.dwFlags == 4
    assert startup.wShowWindow == 0
    assert platform_io.background_process_launch_kwargs("posix") == {
        "start_new_session": True,
    }


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

    def transient_replace(source, destination):
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
