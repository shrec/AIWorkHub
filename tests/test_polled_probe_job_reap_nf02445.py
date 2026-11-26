"""NF-2026-02445: a polled probe child never leaves a Windows descendant alive.

On Windows every ``_PolledChild`` child is held in a KILL_ON_JOB_CLOSE Job
object; the job is terminated and closed on cancel, on timeout and once a
finished child's outputs are taken, so a grandchild cannot keep the reader's
working directory pinned.  A job that cannot be had leaves the probe running
with the old tree kill.  POSIX never touches a job.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from aiworkhub import platform_io, repo_policy

FAKE_JOB = 0x4A0B
FAKE_HANDLE = 0x1234


class _FakeKernel32:
    def __init__(self, *, assign_ok: bool = True) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.limit_flags: list[int] = []
        self._assign_ok = assign_ok

    def CreateJobObjectW(self, attributes: Any, name: Any) -> int:
        self.calls.append(("create",))
        return FAKE_JOB

    def SetInformationJobObject(self, job: int, klass: int, info: Any, size: int) -> int:
        self.calls.append(("limit", job, klass))
        self.limit_flags.append(int(info._obj.BasicLimitInformation.LimitFlags))
        return 1

    def AssignProcessToJobObject(self, job: int, handle: int) -> int:
        self.calls.append(("assign", job, handle))
        return 1 if self._assign_ok else 0

    def TerminateJobObject(self, job: int, code: int) -> int:
        self.calls.append(("terminate", job))
        return 1

    def CloseHandle(self, job: int) -> int:
        self.calls.append(("close", job))
        return 1


class _FakeChild:
    pid = 515151
    _handle = FAKE_HANDLE

    def __init__(self, returncode: int | None) -> None:
        self.returncode = returncode

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        self.returncode = -9 if self.returncode is None else self.returncode
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9


@pytest.fixture
def fake_windows(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"kernel32": _FakeKernel32(), "returncode": None}
    monkeypatch.setattr(repo_policy, "_is_windows_host", lambda: True)
    monkeypatch.setattr(platform_io, "_windows_job_kernel32", lambda: state["kernel32"])
    monkeypatch.setattr(platform_io, "terminate_process_tree", lambda *_a, **_k: True)
    monkeypatch.setattr(
        subprocess, "Popen", lambda *_a, **_k: _FakeChild(state["returncode"])
    )
    return state


def _spawned(tmp_path: Path) -> repo_policy._PolledOpencodeModelsProbe:
    probe = repo_policy._PolledOpencodeModelsProbe("opencode-fake", tmp_path / "rt")
    assert probe._start(30) is None
    return probe


def _job_calls(kernel32: _FakeKernel32) -> list[tuple[Any, ...]]:
    return [call for call in kernel32.calls if call[0] in ("terminate", "close")]


def test_job_is_kill_on_close_and_holds_the_child(tmp_path: Path, fake_windows: dict[str, Any]) -> None:
    kernel32 = fake_windows["kernel32"]
    probe = _spawned(tmp_path)
    assert kernel32.limit_flags == [platform_io._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE]
    assert ("limit", FAKE_JOB, platform_io._JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS) in kernel32.calls
    assert ("assign", FAKE_JOB, FAKE_HANDLE) in kernel32.calls
    assert probe._job == (kernel32, FAKE_JOB)
    assert _job_calls(kernel32) == []
    probe.cancel()


def test_cancel_terminates_and_closes_the_job(tmp_path: Path, fake_windows: dict[str, Any]) -> None:
    kernel32 = fake_windows["kernel32"]
    probe = _spawned(tmp_path)
    probe.cancel()
    assert _job_calls(kernel32) == [("terminate", FAKE_JOB), ("close", FAKE_JOB)]
    assert probe._job is None
    assert list((tmp_path / "rt").iterdir()) == []


def test_normal_finish_terminates_and_closes_the_job(tmp_path: Path, fake_windows: dict[str, Any]) -> None:
    fake_windows["returncode"] = 0
    kernel32 = fake_windows["kernel32"]
    probe = _spawned(tmp_path)
    probe._collect(0)
    assert _job_calls(kernel32) == [("terminate", FAKE_JOB), ("close", FAKE_JOB)]
    assert probe._job is None
    # A second release is a no-op: the handle is closed exactly once.
    probe.cancel()
    assert _job_calls(kernel32) == [("terminate", FAKE_JOB), ("close", FAKE_JOB)]


def test_failed_assign_falls_back_without_failing_the_probe(
    tmp_path: Path, fake_windows: dict[str, Any]
) -> None:
    kernel32 = _FakeKernel32(assign_ok=False)
    fake_windows["kernel32"] = kernel32
    probe = _spawned(tmp_path)
    assert probe._process is not None
    assert probe._job is None and probe._job_refusal == "assign"
    assert ("close", FAKE_JOB) in kernel32.calls
    probe.cancel()
    assert ("terminate", FAKE_JOB) not in kernel32.calls


def test_failed_load_falls_back_without_failing_the_probe(
    tmp_path: Path, fake_windows: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse() -> Any:
        raise OSError("no kernel32")

    monkeypatch.setattr(platform_io, "_windows_job_kernel32", refuse)
    probe = _spawned(tmp_path)
    assert probe._process is not None
    assert probe._job is None and probe._job_refusal == "load"
    probe.cancel()


def test_posix_never_opens_a_job(tmp_path: Path, fake_windows: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repo_policy, "_is_windows_host", lambda: False)
    monkeypatch.setattr(
        platform_io, "_windows_job_kernel32", lambda: pytest.fail("POSIX must not load kernel32")
    )
    probe = _spawned(tmp_path)
    assert probe._job is None and probe._job_refusal is None
    probe.cancel()


# A root that starts a 60 s grandchild in its cwd, reports it, then either
# exits at once or keeps running.
_ROOT = (
    "import subprocess, sys, time\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], cwd=sys.argv[1])\n"
    "sys.stdout.write(str(child.pid)); sys.stdout.flush()\n"
    "if sys.argv[2] == 'hold':\n"
    "    time.sleep(60)\n"
)


def _removable_within(path: Path, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while True:
        try:
            shutil.rmtree(path)
            return True
        except FileNotFoundError:
            return True
        except OSError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)


def _wait_for(predicate: Any, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while not predicate():
        assert time.monotonic() < deadline, "polled child never got that far"
        time.sleep(0.05)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job object contract")
@pytest.mark.parametrize("ending", ["finish", "cancel"])
def test_real_grandchild_does_not_pin_the_cwd(tmp_path: Path, ending: str) -> None:
    held = tmp_path / "held"
    held.mkdir()
    child = repo_policy._PolledChild(tmp_path / "rt")
    mode = "exit" if ending == "finish" else "hold"
    assert child._spawn(
        [sys.executable, "-c", _ROOT, str(held), mode],
        60.0,
        prefix="nf02445-",
        suffixes=(".out",),
        cwd=str(held),
    ) is None
    try:
        assert child._job is not None, child._job_refusal
        output = child._outputs[0]
        _wait_for(lambda: output.stat().st_size > 0, 30.0)
        if ending == "finish":
            process = child._process
            assert process is not None
            _wait_for(lambda: process.poll() is not None, 30.0)
            repo_policy._remove_probe_outputs(child._take_outputs(), 5.0)
        else:
            child.cancel()
        assert _removable_within(held, 5.0), "a probe descendant still holds its cwd"
    finally:
        child.cancel()


# NF-2026-02447: both ``opencode models`` spawn sites run from the resolved
# directory of the executable, never from the caller's working directory.


def test_both_opencode_probe_sites_run_from_the_executable_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = sys.executable
    expected = str(Path(executable).resolve().parent)
    assert repo_policy._opencode_probe_cwd(executable) == expected

    popen_kwargs: list[dict[str, Any]] = []
    run_kwargs: list[dict[str, Any]] = []

    def fake_popen(*_a: Any, **kwargs: Any) -> _FakeChild:
        popen_kwargs.append(kwargs)
        return _FakeChild(None)

    def fake_run(*_a: Any, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        run_kwargs.append(kwargs)
        return subprocess.CompletedProcess([executable, "models"], 0, b"", b"")

    monkeypatch.setattr(repo_policy, "_is_windows_host", lambda: False)
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(subprocess, "run", fake_run)

    probe = repo_policy._PolledOpencodeModelsProbe(executable, tmp_path / "rt")
    assert probe._start(30) is None
    probe._process = None  # the fake child is never really reaped
    probe.cancel()
    repo_policy._opencode_models_probe_once(executable, 30)

    assert [kwargs.get("cwd") for kwargs in popen_kwargs] == [expected]
    assert [kwargs.get("cwd") for kwargs in run_kwargs] == [expected]


def test_probe_cwd_falls_back_to_inherit_without_a_directory(tmp_path: Path) -> None:
    missing = tmp_path / "absent" / "opencode"
    assert repo_policy._opencode_probe_cwd(str(missing)) is None
    # A relative path would resolve against (and run under) the caller's cwd.
    assert repo_policy._opencode_probe_cwd(os.path.join("sub", "opencode")) is None


@pytest.mark.skipif(
    sys.platform != "win32" or shutil.which("opencode") is None,
    reason="needs a real opencode on Windows",
)
def test_real_opencode_probe_leaves_the_caller_cwd_removable(tmp_path: Path) -> None:
    executable = shutil.which("opencode")
    assert executable is not None
    scratch = tmp_path / "caller-cwd"
    scratch.mkdir()
    probe = repo_policy._PolledOpencodeModelsProbe(executable, tmp_path / "rt")
    # Bounded by the probe's own timeout plus one retry.
    deadline = time.monotonic() + 2 * (
        repo_policy._OPENCODE_PROBE_TIMEOUT_SECONDS
        + repo_policy._OPENCODE_PROBE_RETRY_TIMEOUT_SECONDS
    )
    previous = Path.cwd()
    os.chdir(scratch)
    try:
        while not probe.poll():
            assert time.monotonic() < deadline, "opencode probe never finished"
            time.sleep(0.05)
    finally:
        os.chdir(previous)
        probe.cancel()
    shutil.rmtree(scratch)
    assert not scratch.exists()
