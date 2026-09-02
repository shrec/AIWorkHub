"""One policy decided by one predicate, and one observable offset contract.

``platform_io`` exists so the rest of AIWorkHub sees the same behaviour on
Linux, macOS and Windows. Three places quietly broke that promise:

* ``chmod_fd`` decided "do POSIX mode bits apply here" with a
  ``getattr(os, "fchmod")`` capability probe while ``chmod_path`` decided the
  same question with ``posix_path_modes_supported(platform_name)``. Two rules
  for one policy, and because only ``chmod_path`` took a ``platform_name``
  override, the Windows branch of ``chmod_fd`` -- 55 call sites -- could not be
  tested at all.
* ``lock_fd``/``unlock_fd`` seek to the lock byte on the Windows branch and
  never put the caller's file offset back. POSIX ``flock`` touches neither the
  content nor the offset, so the observable behaviour differed by platform.
* ``GlobalMemoryStatusEx`` was called with no ``argtypes``/``restype`` while
  every other ctypes entry point in the module sets them.

These tests drive all three against the module's own Windows branch, faked the
same way the existing platform_io suite fakes it.
"""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import platform_io  # noqa: E402


def _fake_msvcrt(calls: list[tuple[int, int, int]]) -> SimpleNamespace:
    return SimpleNamespace(
        LK_LOCK=1,
        LK_NBLCK=2,
        LK_UNLCK=3,
        locking=lambda fd, mode, count: calls.append((fd, mode, count)),
    )


# --- one policy, one predicate --------------------------------------------


def test_chmod_fd_accepts_platform_name_and_no_ops_on_windows(tmp_path):
    """The Windows branch of chmod_fd is reachable from a test at all."""
    path = tmp_path / "mode.txt"
    path.write_bytes(b"x")
    fd = os.open(path, os.O_RDWR)
    try:
        os.fchmod(fd, 0o600)
        platform_io.chmod_fd(fd, 0o644, platform_name="nt")
        assert (os.fstat(fd).st_mode & 0o777) == 0o600
    finally:
        os.close(fd)


def test_chmod_fd_still_applies_the_mode_on_a_posix_platform_name(tmp_path):
    path = tmp_path / "mode.txt"
    path.write_bytes(b"x")
    fd = os.open(path, os.O_RDWR)
    try:
        os.fchmod(fd, 0o600)
        platform_io.chmod_fd(fd, 0o640, platform_name="posix")
        assert (os.fstat(fd).st_mode & 0o777) == 0o640
    finally:
        os.close(fd)


@pytest.mark.parametrize("platform_name", ["nt", "posix", None])
def test_chmod_fd_and_chmod_path_agree_on_applicability(tmp_path, platform_name):
    """Both entry points must answer the one policy question identically."""
    expected = platform_io.posix_path_modes_supported(platform_name)

    path = tmp_path / "agree.txt"
    path.write_bytes(b"x")
    os.chmod(path, 0o600)
    platform_io.chmod_path(path, 0o640, platform_name=platform_name)
    path_applied = (path.stat().st_mode & 0o777) == 0o640

    fd_path = tmp_path / "agree-fd.txt"
    fd_path.write_bytes(b"x")
    fd = os.open(fd_path, os.O_RDWR)
    try:
        os.fchmod(fd, 0o600)
        platform_io.chmod_fd(fd, 0o640, platform_name=platform_name)
        fd_applied = (os.fstat(fd).st_mode & 0o777) == 0o640
    finally:
        os.close(fd)

    assert path_applied is expected
    assert fd_applied is expected


def test_chmod_fd_defaults_to_the_host_when_no_platform_name_is_given(tmp_path):
    path = tmp_path / "default.txt"
    path.write_bytes(b"x")
    fd = os.open(path, os.O_RDWR)
    try:
        os.fchmod(fd, 0o600)
        platform_io.chmod_fd(fd, 0o640)
        expected = 0o640 if platform_io.posix_path_modes_supported() else 0o600
        assert (os.fstat(fd).st_mode & 0o777) == expected
    finally:
        os.close(fd)


# --- one observable file-offset contract -----------------------------------


def test_windows_lock_and_unlock_preserve_the_caller_offset(tmp_path, monkeypatch):
    """POSIX flock never moves the offset; the Windows branch must not either."""
    calls: list[tuple[int, int, int]] = []
    monkeypatch.setitem(sys.modules, "msvcrt", _fake_msvcrt(calls))
    monkeypatch.setattr(platform_io.os, "name", "nt")

    path = tmp_path / "offset.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.write(fd, b"0123456789")
        os.lseek(fd, 4, os.SEEK_SET)
        assert os.lseek(fd, 0, os.SEEK_CUR) == 4

        platform_io.lock_fd(fd, blocking=False)
        assert os.lseek(fd, 0, os.SEEK_CUR) == 4, "lock_fd rewound the caller"

        platform_io.unlock_fd(fd)
        assert os.lseek(fd, 0, os.SEEK_CUR) == 4, "unlock_fd rewound the caller"
    finally:
        os.close(fd)

    assert [mode for _fd, mode, _count in calls] == [2, 3]


def test_windows_lock_still_locks_byte_zero_of_an_empty_file(tmp_path, monkeypatch):
    """Preserving the offset must not stop the lock byte from being addressed."""
    calls: list[tuple[int, int, int]] = []
    monkeypatch.setitem(sys.modules, "msvcrt", _fake_msvcrt(calls))
    monkeypatch.setattr(platform_io.os, "name", "nt")

    path = tmp_path / "empty.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        platform_io.lock_fd(fd, blocking=False)
        assert os.fstat(fd).st_size == 1
        assert os.lseek(fd, 0, os.SEEK_CUR) == 0
        platform_io.unlock_fd(fd)
    finally:
        os.close(fd)

    assert calls == [(fd, 2, 1), (fd, 3, 1)]


def test_windows_lock_restores_the_offset_even_when_locking_raises(
    tmp_path, monkeypatch
):
    """A failed acquisition must not leave the caller's offset moved."""

    def _boom(_fd: int, _mode: int, _count: int) -> None:
        raise OSError(1, "not permitted")

    fake = SimpleNamespace(LK_LOCK=1, LK_NBLCK=2, LK_UNLCK=3, locking=_boom)
    monkeypatch.setitem(sys.modules, "msvcrt", fake)
    monkeypatch.setattr(platform_io.os, "name", "nt")

    path = tmp_path / "raise.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.write(fd, b"0123456789")
        os.lseek(fd, 7, os.SEEK_SET)
        with pytest.raises(OSError):
            platform_io.lock_fd(fd, blocking=False)
        assert os.lseek(fd, 0, os.SEEK_CUR) == 7
    finally:
        os.close(fd)


def test_posix_lock_leaves_the_offset_untouched(tmp_path):
    """The behaviour the Windows branch is being aligned to."""
    if os.name == "nt":  # pragma: no cover - POSIX-only assertion
        pytest.skip("posix-only")
    path = tmp_path / "posix.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.write(fd, b"0123456789")
        os.lseek(fd, 3, os.SEEK_SET)
        platform_io.lock_fd(fd, blocking=False)
        assert os.lseek(fd, 0, os.SEEK_CUR) == 3
        platform_io.unlock_fd(fd)
        assert os.lseek(fd, 0, os.SEEK_CUR) == 3
    finally:
        os.close(fd)


def test_windows_lock_byte_helper_documents_the_writable_fd_requirement():
    """POSIX accepts a read-only fd for flock; the Windows branch cannot."""
    doc = platform_io._prepare_windows_lock_byte.__doc__ or ""
    assert "WRITABLE" in doc


# --- ctypes entry points declare their signature ---------------------------


def test_global_memory_status_declares_argtypes_and_restype(monkeypatch):
    """Every other ctypes entry point in this module sets them; so must this."""
    recorded: dict[str, object] = {}

    class _Proc:
        argtypes = None
        restype = None

        def __call__(self, _pointer: object) -> int:
            recorded["argtypes"] = self.argtypes
            recorded["restype"] = self.restype
            return 0  # failure path: available_memory_bytes returns None

    kernel32 = SimpleNamespace(GlobalMemoryStatusEx=_Proc())
    monkeypatch.setattr(platform_io, "is_linux", lambda _name=None: False)
    monkeypatch.setattr(platform_io, "is_windows", lambda _name=None: True)
    monkeypatch.setattr(
        platform_io.ctypes, "windll", SimpleNamespace(kernel32=kernel32), raising=False
    )

    assert platform_io.available_memory_bytes("nt") is None
    assert recorded["restype"] is ctypes.c_int
    argtypes = recorded["argtypes"]
    assert argtypes is not None and len(argtypes) == 1
