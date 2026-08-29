from __future__ import annotations

import errno
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from aiworkhub import platform_io


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
