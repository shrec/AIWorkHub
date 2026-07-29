from __future__ import annotations

import os
import sys
from types import SimpleNamespace

from aiworkhub import platform_io


def test_posix_lock_round_trip(tmp_path):
    path = tmp_path / "runtime.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        platform_io.chmod_fd(fd, 0o600)
        platform_io.lock_fd(fd, blocking=False)
        platform_io.unlock_fd(fd)
    finally:
        os.close(fd)


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
