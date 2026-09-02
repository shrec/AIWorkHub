"""``lock_fd(blocking=True)`` must WAIT on Windows, not fail after ten seconds.

``msvcrt.LK_LOCK`` looks like the Windows counterpart of ``flock(LOCK_EX)``
but is not: it retries ten times at one-second intervals and then raises
``OSError``.  A lock held longer than that turned into a hard failure on
Windows while the identical POSIX code path simply waited -- one of the
behavioural divergences behind "works on Linux, breaks on Windows".
"""

from __future__ import annotations

import errno
import os
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aiworkhub import platform_io  # noqa: E402

windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows locking semantics")


@windows_only
def test_blocking_lock_retries_instead_of_using_lk_lock(monkeypatch):
    import msvcrt

    calls: list[int] = []
    attempts = {"n": 0}

    def fake_locking(fd, mode, nbytes):
        calls.append(mode)
        attempts["n"] += 1
        if attempts["n"] < 4:
            raise OSError(errno.EDEADLOCK, "Resource deadlock avoided")

    monkeypatch.setattr(msvcrt, "locking", fake_locking)
    monkeypatch.setattr(platform_io, "ADVISORY_LOCK_POLL_SECONDS", 0.0)

    with tempfile.TemporaryDirectory() as tmp:
        fd = os.open(Path(tmp) / "a.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            platform_io.lock_fd(fd, blocking=True)
        finally:
            os.close(fd)

    assert attempts["n"] == 4, "must keep retrying until the holder releases"
    assert msvcrt.LK_LOCK not in calls, "LK_LOCK gives up after ~10s; never use it"
    assert set(calls) == {msvcrt.LK_NBLCK}


@windows_only
def test_blocking_lock_gives_up_instead_of_hanging_forever(monkeypatch):
    """A Windows byte-range lock can be blocked by this same process holding
    another handle, which waiting can never clear. Bound the wait so the
    caller fails visibly rather than freezing (that froze the dashboard on
    "Connecting" with no way out)."""

    import msvcrt

    def always_contended(fd, mode, nbytes):
        raise OSError(errno.EDEADLOCK, "Resource deadlock avoided")

    monkeypatch.setattr(msvcrt, "locking", always_contended)
    monkeypatch.setattr(platform_io, "ADVISORY_LOCK_POLL_SECONDS", 0.0)
    monkeypatch.setattr(platform_io, "ADVISORY_LOCK_MAX_WAIT_SECONDS", 0.15)

    with tempfile.TemporaryDirectory() as tmp:
        fd = os.open(Path(tmp) / "stuck.lock", os.O_CREAT | os.O_RDWR, 0o600)
        started = time.monotonic()
        try:
            with pytest.raises(TimeoutError):
                platform_io.lock_fd(fd, blocking=True)
        finally:
            os.close(fd)
        assert time.monotonic() - started < 10, "must not wait anywhere near forever"


@windows_only
def test_blocking_lock_propagates_a_real_error(monkeypatch):
    import msvcrt

    def fake_locking(fd, mode, nbytes):
        raise OSError(errno.EBADF, "Bad file descriptor")

    monkeypatch.setattr(msvcrt, "locking", fake_locking)
    with tempfile.TemporaryDirectory() as tmp:
        fd = os.open(Path(tmp) / "b.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            with pytest.raises(OSError) as excinfo:
                platform_io.lock_fd(fd, blocking=True)
        finally:
            os.close(fd)
    assert excinfo.value.errno == errno.EBADF


@windows_only
def test_non_blocking_lock_makes_exactly_one_attempt(monkeypatch):
    import msvcrt

    calls: list[int] = []
    monkeypatch.setattr(msvcrt, "locking", lambda fd, mode, n: calls.append(mode))
    with tempfile.TemporaryDirectory() as tmp:
        fd = os.open(Path(tmp) / "c.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            platform_io.lock_fd(fd, blocking=False)
        finally:
            os.close(fd)
    assert calls == [msvcrt.LK_NBLCK]


def test_blocking_lock_timeout_names_the_errno_that_ended_the_wait(
    tmp_path, monkeypatch
):
    """A permission-shaped EACCES retried to the deadline must yield a timeout
    that both exposes ``.errno`` and names it symbolically -- so the launcher's
    ``except AdvisoryLockTimeout`` recovery, the operator and the audit ledger
    can read "not permitted" instead of blindly "someone else holds the lock".

    Uses a fake ``msvcrt`` so the Windows lock path is exercised on any host."""

    def contended(fd, mode, nbytes):
        raise OSError(errno.EACCES, "permission denied on the lock byte")

    fake_msvcrt = SimpleNamespace(
        LK_LOCK=1, LK_NBLCK=2, LK_UNLCK=3, locking=contended
    )
    monkeypatch.setitem(sys.modules, "msvcrt", fake_msvcrt)
    monkeypatch.setattr(platform_io.os, "name", "nt")
    monkeypatch.setattr(platform_io, "ADVISORY_LOCK_MAX_WAIT_SECONDS", 0.0)
    monkeypatch.setattr(platform_io.time, "monotonic", lambda: 10.0)

    fd = os.open(tmp_path / "named.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        with pytest.raises(platform_io.AdvisoryLockTimeout) as excinfo:
            platform_io.lock_fd(fd, blocking=True)
    finally:
        os.close(fd)

    assert excinfo.value.errno == errno.EACCES
    assert "EACCES" in str(excinfo.value)


def test_blocking_lock_waits_for_a_real_cross_process_holder(tmp_path):
    """End-to-end: a second process must block, then acquire once released."""

    lock_path = tmp_path / "shared.lock"
    holder_src = textwrap.dedent(
        f"""
        import os, sys, time
        sys.path.insert(0, {str(Path(__file__).resolve().parents[1] / "src")!r})
        from aiworkhub import platform_io
        fd = os.open({str(lock_path)!r}, os.O_CREAT | os.O_RDWR, 0o600)
        platform_io.lock_fd(fd, blocking=True)
        print("HELD", flush=True)
        time.sleep(1.5)
        platform_io.unlock_fd(fd)
        os.close(fd)
        """
    )
    holder = subprocess.Popen([sys.executable, "-c", holder_src],
                              stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "HELD"
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            started = time.monotonic()
            platform_io.lock_fd(fd, blocking=True)
            waited = time.monotonic() - started
            platform_io.unlock_fd(fd)
        finally:
            os.close(fd)
    finally:
        holder.wait(timeout=30)

    # It genuinely waited for the holder rather than erroring out.
    assert waited > 0.2, f"expected to block for the holder, waited {waited:.3f}s"
