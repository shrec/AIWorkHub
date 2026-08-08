from __future__ import annotations

import os
import stat
import subprocess
import sys
from types import SimpleNamespace

from aiworkhub import platform_io


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


def test_chmod_path_applies_posix_mode(tmp_path):
    target = tmp_path / "private-file"
    target.write_text("ok", encoding="utf-8")

    platform_io.chmod_path(target, 0o600, platform_name="posix")

    assert stat.S_IMODE(target.stat().st_mode) == 0o600


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
