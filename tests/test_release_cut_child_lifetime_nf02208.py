"""scripts/release_cut.py: a stopped cut takes the process tree it started with it.

NF-2026-02208: after the 0.12.95 cut was stopped, its pytest workers stayed alive
and held 12 cores until they were ended by hand. On native Windows the cut now
binds itself to a kill-on-close Job Object. Two tests here start real processes
and kill a real driver; the others replace kernel32. None of them runs a cut.
"""

from __future__ import annotations

import contextlib
import ctypes
import importlib.util
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

# Imported, not only loaded: the validation worktree materializes the import
# closure, and the by-path load inside the script is invisible to that closure.
from aiworkhub.windows_job_structures import JOBOBJECT_EXTENDED_LIMIT_INFORMATION


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "release_cut.py"
SPEC = importlib.util.spec_from_file_location("aiworkhub_release_cut", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
release_cut = importlib.util.module_from_spec(SPEC)
# Its dataclasses resolve their string annotations through sys.modules.
sys.modules[SPEC.name] = release_cut
SPEC.loader.exec_module(release_cut)

windows_only = pytest.mark.skipif(os.name != "nt", reason="a Windows Job Object")

WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 0x102
SYNCHRONIZE = 0x00100000
PROCESS_TERMINATE = 0x0001
KILL_ON_JOB_CLOSE = 0x00002000
JOB = 71
# What GetCurrentProcess answers on 64-bit once its restype is HANDLE.
CURRENT_PROCESS = 2**64 - 1
LAST_ERROR = 5
PUBLISH_TIMEOUT_SECONDS = 60.0
VERSION = "0.13.0"

# Every sleep is bounded, so even a test killed before its finally block cannot
# leave a process behind for long.
_CHILD = """\
import os
import subprocess
import sys
import time

pid_file = sys.argv[1]
grandchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
with open(pid_file + ".tmp", "w", encoding="ascii") as sink:
    sink.write(f"{grandchild.pid} {os.getpid()}")
os.replace(pid_file + ".tmp", pid_file)
time.sleep(300)
"""

_DRIVER = """\
import importlib.util
import sys
from pathlib import Path

script, pid_file, mode, child = sys.argv[1:]
spec = importlib.util.spec_from_file_location("aiworkhub_release_cut", script)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
if mode == "bind" and not module._bind_children_to_this_process():
    sys.exit(3)
module.run_subprocess([sys.executable, "-c", child, pid_file], cwd=Path(pid_file).parent)
"""


def _kernel32() -> Any:
    """A private kernel32, so these declarations stay out of the shared ctypes.windll."""

    import ctypes.wintypes as wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


def _published_pids(pid_file: Path, driver: subprocess.Popen[bytes], log: Path) -> list[int]:
    """Wait, bounded, for the grandchild and child pids the child publishes."""

    deadline = time.monotonic() + PUBLISH_TIMEOUT_SECONDS
    while time.monotonic() < deadline and driver.poll() is None:
        # A file renamed into place a moment ago can refuse its first open on Windows.
        with contextlib.suppress(FileNotFoundError, PermissionError):
            return [int(token) for token in pid_file.read_text(encoding="ascii").split()]
        time.sleep(0.05)
    pytest.fail(f"no pids, driver returncode {driver.poll()}: {log.read_bytes()!r}")


def _terminate_survivor(kernel32: Any, pid: int, handle: int) -> None:
    """End ``pid`` if it still runs; the handle held open keeps the pid from being reused."""

    if kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT:
        terminator = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
        if terminator:
            kernel32.TerminateProcess(terminator, 1)
            kernel32.CloseHandle(terminator)
    kernel32.CloseHandle(handle)


@contextlib.contextmanager
def _process_tree(tmp_path: Path, *, bind: bool) -> Iterator[tuple[Any, int, Any]]:
    """Start driver, child and grandchild; yield (driver, grandchild handle, kernel32)."""

    kernel32 = _kernel32()
    pid_file, log = tmp_path / "pids", tmp_path / "driver.log"
    mode = "bind" if bind else "no-bind"
    argv = [sys.executable, "-c", _DRIVER, str(SCRIPT), str(pid_file), mode, _CHILD]
    opened: list[tuple[int, int]] = []
    with log.open("wb") as sink, subprocess.Popen(
        argv, cwd=tmp_path, stdin=subprocess.DEVNULL, stdout=sink, stderr=subprocess.STDOUT
    ) as driver:
        try:
            for pid in _published_pids(pid_file, driver, log):
                handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
                assert handle, f"OpenProcess({pid}): GetLastError {ctypes.get_last_error()}"
                opened.append((pid, handle))
            yield driver, opened[0][1], kernel32
        finally:
            driver.kill()
            for pid, handle in opened:
                _terminate_survivor(kernel32, pid, handle)


class _Export:
    """One fake kernel32 export: logs each call, then answers or raises its result."""

    def __init__(self, name: str, result: Any, log: list[str]) -> None:
        self.name = name
        self.result = result
        self.log = log
        self.calls: list[tuple[Any, ...]] = []
        self.argtypes: Any = None
        self.restype: Any = None

    def __call__(self, *args: Any) -> Any:
        self.log.append(self.name)
        self.calls.append(args)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _fake_kernel32(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> SimpleNamespace:
    """Replace ctypes.windll with fake exports that all succeed unless overridden."""

    log: list[str] = []
    results = {
        "CreateJobObjectW": JOB,
        "SetInformationJobObject": 1,
        "GetCurrentProcess": CURRENT_PROCESS,
        "AssignProcessToJobObject": 1,
        "CloseHandle": 1,
        "GetLastError": LAST_ERROR,
        **overrides,
    }
    exports = {name: _Export(name, result, log) for name, result in results.items()}
    kernel32 = SimpleNamespace(log=log, exports=exports, **exports)
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(kernel32=kernel32), raising=False)
    return kernel32


def _assert_one_failure_line(capsys: pytest.CaptureFixture[str], step: str) -> None:
    captured = capsys.readouterr()
    lines = (captured.out + captured.err).splitlines()
    assert len(lines) == 1, lines
    assert f"{step} failed" in lines[0]
    assert f"GetLastError {LAST_ERROR}" in lines[0]


class _RecordingWindll:
    """Stands in for ctypes.windll and records every attribute anyone asks it for."""

    def __init__(self) -> None:
        self.touched: list[str] = []

    def __getattr__(self, name: str) -> Any:
        self.touched.append(name)
        raise AttributeError(name)


@windows_only
def test_killing_the_bound_cut_ends_its_grandchild(tmp_path: Path) -> None:
    with _process_tree(tmp_path, bind=True) as (driver, grandchild, kernel32):
        driver.kill()
        driver.wait(timeout=30)
        assert kernel32.WaitForSingleObject(grandchild, 30000) == WAIT_OBJECT_0


@windows_only
def test_killing_an_unbound_driver_leaves_its_grandchild_running(tmp_path: Path) -> None:
    """The reproduction: without the bind the tree outlives the process that started it."""

    with _process_tree(tmp_path, bind=False) as (driver, grandchild, kernel32):
        driver.kill()
        driver.wait(timeout=30)
        assert kernel32.WaitForSingleObject(grandchild, 2000) == WAIT_TIMEOUT


@windows_only
def test_failed_assign_closes_the_job_and_names_the_step(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    kernel32 = _fake_kernel32(monkeypatch, AssignProcessToJobObject=0)

    assert release_cut._bind_children_to_this_process() is False

    handle, info_class, buffer, size = kernel32.SetInformationJobObject.calls[0]
    assert (handle, info_class) == (JOB, 9)
    assert size == ctypes.sizeof(JOBOBJECT_EXTENDED_LIMIT_INFORMATION)
    assert buffer._obj.BasicLimitInformation.LimitFlags == KILL_ON_JOB_CLOSE
    assert kernel32.AssignProcessToJobObject.calls == [(JOB, CURRENT_PROCESS)]
    assert kernel32.CloseHandle.calls == [(JOB,)]
    # CloseHandle overwrites the code the line has to report.
    assert kernel32.log.index("GetLastError") < kernel32.log.index("CloseHandle")
    _assert_one_failure_line(capsys, "assign")


@windows_only
@pytest.mark.parametrize(
    ("export", "result", "step", "closed"),
    [
        ("CreateJobObjectW", None, "create", []),
        ("SetInformationJobObject", 0, "set information", [(JOB,)]),
        ("SetInformationJobObject", OSError("refused"), "set information", [(JOB,)]),
        ("AssignProcessToJobObject", OSError("refused"), "assign", [(JOB,)]),
    ],
)
def test_every_later_failure_reports_once_and_never_raises(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    export: str,
    result: Any,
    step: str,
    closed: list[tuple[int]],
) -> None:
    kernel32 = _fake_kernel32(monkeypatch, **{export: result})

    assert release_cut._bind_children_to_this_process() is False

    assert kernel32.CloseHandle.calls == closed
    _assert_one_failure_line(capsys, step)


@windows_only
def test_failed_load_names_the_step_and_creates_no_job(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    kernel32 = _fake_kernel32(monkeypatch)

    def refuse(*_args: Any) -> None:
        raise FileNotFoundError("windows_job_structures.py")

    monkeypatch.setattr(release_cut, "_load_sibling", refuse)

    assert release_cut._bind_children_to_this_process() is False

    assert kernel32.CreateJobObjectW.calls == []
    assert kernel32.CloseHandle.calls == []
    _assert_one_failure_line(capsys, "load")


@windows_only
def test_bound_process_keeps_its_job_handle_and_declares_every_call(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import ctypes.wintypes as wintypes

    kernel32 = _fake_kernel32(monkeypatch)

    assert release_cut._bind_children_to_this_process() is True

    assert kernel32.CloseHandle.calls == []
    assert kernel32.AssignProcessToJobObject.calls == [(JOB, CURRENT_PROCESS)]
    assert capsys.readouterr().out == ""
    undeclared = [
        name
        for name, export in kernel32.exports.items()
        if export.argtypes is None or export.restype is None
    ]
    assert undeclared == []
    assert kernel32.GetCurrentProcess.restype is wintypes.HANDLE
    assert kernel32.CreateJobObjectW.restype is wintypes.HANDLE
    assert kernel32.AssignProcessToJobObject.argtypes == [wintypes.HANDLE, wintypes.HANDLE]


@pytest.mark.skipif(os.name == "nt", reason="the POSIX branch of the bind")
def test_posix_host_binds_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    windll = _RecordingWindll()
    monkeypatch.setattr(ctypes, "windll", windll, raising=False)

    assert release_cut._bind_children_to_this_process() is False

    assert windll.touched == []
    assert capsys.readouterr().out == ""


def test_main_binds_once_before_run_and_only_for_the_real_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[Any] = []

    class FakeCut:
        def __init__(self, _options: Any, host: Any) -> None:
            self.host = host

        def run(self) -> int:
            events.append(("run", self.host))
            return 0

    current_host = SimpleNamespace(name="current")
    monkeypatch.setattr(release_cut, "ReleaseCut", FakeCut)
    monkeypatch.setattr(release_cut.Host, "current", classmethod(lambda _cls: current_host))
    monkeypatch.setattr(
        release_cut, "_bind_children_to_this_process", lambda: events.append("bind")
    )

    injected = SimpleNamespace(name="injected")
    assert release_cut.main([VERSION], host=injected) == 0
    assert events == [("run", injected)]

    events.clear()
    assert release_cut.main([VERSION]) == 0
    assert events == ["bind", ("run", current_host)]


def test_loading_the_script_and_the_current_host_never_bind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A test process that loads the module must never end up in the job."""

    windll = _RecordingWindll()
    monkeypatch.setattr(ctypes, "windll", windll, raising=False)
    spec = importlib.util.spec_from_file_location("aiworkhub_release_cut_fresh", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)

    module.Host.current()

    assert windll.touched == []


def test_script_declares_no_second_job_layout() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "class JOBOBJECT_EXTENDED_LIMIT_INFORMATION" not in source
