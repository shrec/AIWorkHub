"""NF-2026-02423: a Manager Chat turn's teardown spares broken-away supervisors.

On Windows the turn's CLI carries a Job that permits breakaway; turn teardown
ends that Job (TerminateJobObject) instead of the ppid tree kill, so a worker
supervisor the turn's MCP server started with ``CREATE_BREAKAWAY_FROM_JOB``
survives the turn while every plain descendant still dies. Without a job --
POSIX, failed job setup, fakes -- teardown keeps the tree kill unchanged.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import manager_loop_backends as mlb  # noqa: E402
from aiworkhub import platform_io  # noqa: E402

_JOB = 0x5A5A


class _FakeKernel32:
    """Records every Job-object call; each step's result is configurable."""

    def __init__(self, *, create: int = _JOB, set_ok: int = 1, assign_ok: int = 1) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self._create = create
        self._set_ok = set_ok
        self._assign_ok = assign_ok

    def CreateJobObjectW(self, _attributes: object, _name: object) -> int:  # noqa: N802
        self.calls.append(("CreateJobObjectW",))
        return self._create

    def SetInformationJobObject(self, job: int, klass: int, _info: object, _size: int) -> int:  # noqa: N802
        self.calls.append(("SetInformationJobObject", job, klass))
        return self._set_ok

    def AssignProcessToJobObject(self, job: int, handle: int) -> int:  # noqa: N802
        self.calls.append(("AssignProcessToJobObject", job, handle))
        return self._assign_ok

    def TerminateJobObject(self, job: int, exit_code: int) -> int:  # noqa: N802
        self.calls.append(("TerminateJobObject", job, exit_code))
        return 1

    def CloseHandle(self, handle: int) -> int:  # noqa: N802
        self.calls.append(("CloseHandle", handle))
        return 1

    def count(self, name: str) -> int:
        return sum(1 for call in self.calls if call[0] == name)


class _FakeProcess:
    """A Popen stand-in: alive until killed, or until waited on if ``exits_on_wait``."""

    pid = 4242

    def __init__(self, *, handle: int | None = 77, exits_on_wait: bool = False) -> None:
        if handle is not None:
            self._handle = handle
        self.returncode: int | None = None
        self.kills = 0
        self.waits: list[float | None] = []
        self._exits_on_wait = exits_on_wait

    def poll(self) -> int | None:
        return self.returncode

    def kill(self) -> None:
        self.kills += 1
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        self.waits.append(timeout)
        if self.returncode is None and self._exits_on_wait:
            self.returncode = 0
        if self.returncode is None:
            raise subprocess.TimeoutExpired("fake", timeout or 0.0)
        return self.returncode


@pytest.fixture
def tree_kills(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, dict[str, Any]]]:
    calls: list[tuple[int, dict[str, Any]]] = []

    def _record(pid: int, **options: Any) -> bool:
        calls.append((pid, options))
        return True

    monkeypatch.setattr(platform_io, "terminate_process_tree", _record)
    return calls


def _use_kernel32(monkeypatch: pytest.MonkeyPatch, fake: _FakeKernel32) -> None:
    monkeypatch.setattr(platform_io, "_windows_job_kernel32", lambda: fake)


# --- the job path ------------------------------------------------------------


def test_a_turn_with_a_job_ends_by_the_job_and_never_by_the_tree_kill(monkeypatch, tree_kills):
    fake = _FakeKernel32()
    _use_kernel32(monkeypatch, fake)
    process = _FakeProcess()

    mlb._bind_turn_job(process)
    assert getattr(process, mlb._TURN_JOB_ATTR) == _JOB
    assert ("AssignProcessToJobObject", _JOB, 77) in fake.calls
    assert fake.count("CloseHandle") == 0, "a bound job stays open until teardown"

    mlb._end_process(process)

    assert fake.count("TerminateJobObject") == 1
    assert fake.count("CloseHandle") == 1
    assert ("TerminateJobObject", _JOB, 1) in fake.calls
    assert tree_kills == [], "the ppid tree kill reaches the broken-away supervisor"
    assert process.poll() is not None
    assert getattr(process, mlb._TURN_JOB_ATTR) == 0, "the ended job stays marked"

    mlb._end_process(process)  # a second teardown never closes the handle again
    assert fake.count("TerminateJobObject") == 1
    assert fake.count("CloseHandle") == 1
    assert tree_kills == [], "a second teardown never falls back to the ppid tree kill"


def test_the_job_limit_is_breakaway_ok_only(monkeypatch):
    seen: list[int] = []

    class _Recording(_FakeKernel32):
        def SetInformationJobObject(self, job, klass, info, size):  # noqa: N802
            seen.append(int(info._obj.BasicLimitInformation.LimitFlags))
            return super().SetInformationJobObject(job, klass, info, size)

    fake = _Recording()
    _use_kernel32(monkeypatch, fake)

    assert platform_io.attach_windows_breakaway_job(77) == _JOB
    assert seen == [0x00000800], "BREAKAWAY_OK only: no KILL_ON_JOB_CLOSE, no SILENT_BREAKAWAY_OK"


def test_the_grace_window_lets_the_root_exit_before_the_job_ends(monkeypatch, tree_kills):
    fake = _FakeKernel32()
    _use_kernel32(monkeypatch, fake)
    process = _FakeProcess(exits_on_wait=True)
    mlb._bind_turn_job(process)

    mlb._end_process(process, grace_seconds=0.5)

    assert process.waits[0] == 0.5
    assert process.kills == 0, "a root that exited within the grace window is not killed"
    assert fake.count("TerminateJobObject") == 1 and fake.count("CloseHandle") == 1
    assert tree_kills == []


# --- failed job setup keeps today's path -------------------------------------


@pytest.mark.parametrize(
    ("failure", "closes"),
    [
        ({"create": 0}, 0),
        ({"set_ok": 0}, 1),
        ({"assign_ok": 0}, 1),
    ],
    ids=["CreateJobObjectW", "SetInformationJobObject", "AssignProcessToJobObject"],
)
def test_a_failed_job_setup_leaves_no_job_and_keeps_the_tree_kill(
    monkeypatch, tree_kills, failure, closes
):
    fake = _FakeKernel32(**failure)
    _use_kernel32(monkeypatch, fake)
    process = _FakeProcess()

    mlb._bind_turn_job(process)

    assert getattr(process, mlb._TURN_JOB_ATTR, None) is None
    assert fake.count("CloseHandle") == closes, "a created job handle is closed on failure"

    mlb._end_process(process)

    assert tree_kills == [(process.pid, {})]
    assert fake.count("TerminateJobObject") == 0
    assert process.kills == 1


def test_an_unloadable_kernel32_leaves_no_job(monkeypatch):
    def _refuse() -> Any:
        raise OSError("kernel32 unavailable")

    monkeypatch.setattr(platform_io, "_windows_job_kernel32", _refuse)
    process = _FakeProcess()

    mlb._bind_turn_job(process)

    assert getattr(process, mlb._TURN_JOB_ATTR, None) is None


def test_without_a_job_an_exited_windows_root_keeps_the_native_snapshot_seam(
    monkeypatch, tree_kills
):
    monkeypatch.setattr(platform_io, "is_windows", lambda _name=None: True)
    process = _FakeProcess(handle=None)
    process.returncode = 0

    mlb._end_process(process, grace_seconds=1.0)

    assert tree_kills == [(process.pid, {"timeout": 1.0, "run": mlb._native_tree_kill_only})]


# --- the real thing on Windows -----------------------------------------------

_ROOT = r"""
import json, subprocess, sys, time
child = [sys.executable, "-c", "import time; time.sleep(60)"]
quiet = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
report = {}
try:
    report["breakaway"] = subprocess.Popen(
        child, creationflags=subprocess.CREATE_BREAKAWAY_FROM_JOB, **quiet
    ).pid
except PermissionError as exc:
    if getattr(exc, "winerror", None) != 5:
        raise
    report["breakaway_refused"] = True
report["plain"] = subprocess.Popen(child, **quiet).pid
print(json.dumps(report), flush=True)
time.sleep(60)
"""


def _gone_within(pid: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not platform_io.windows_pid_is_alive(pid):
            return True
        time.sleep(0.05)
    return not platform_io.windows_pid_is_alive(pid)


@pytest.mark.skipif(not platform_io.is_windows(), reason="Windows Job objects only")
def test_turn_teardown_spares_a_broken_away_grandchild_and_ends_the_rest():
    # The base interpreter, not a venv launcher, so each reported pid is the real process.
    python = getattr(sys, "_base_executable", None) or sys.executable
    root = mlb._spawn_cli([python, "-c", _ROOT], None)
    report: dict[str, Any] = {}
    try:
        report = json.loads(root.stdout.readline())
        if report.get("breakaway_refused"):
            pytest.skip(
                "the host job refuses CREATE_BREAKAWAY_FROM_JOB (winerror 5), "
                "so no grandchild can break away here"
            )
        assert getattr(root, mlb._TURN_JOB_ATTR, None) is not None, "the turn carries its job"
        assert platform_io.windows_pid_is_alive(report["breakaway"])

        mlb._end_process(root)

        assert root.poll() is not None, "the root is ended"
        assert _gone_within(report["plain"], 5.0), "a plain grandchild dies with the turn"
        assert platform_io.windows_pid_is_alive(report["breakaway"]), (
            "the broken-away grandchild (the worker supervisor) survives the turn"
        )
    finally:
        mlb._end_process(root)
        for key in ("breakaway", "plain"):
            pid = report.get(key)
            if isinstance(pid, int) and platform_io.windows_pid_is_alive(pid):
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
