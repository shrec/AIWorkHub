"""scripts/release_cut.py: a suite run that stops making progress is ended with its tree.

NF-2026-02312: on 2026-11-17 host_tests stopped advancing at 06:29 (no new basetemp
entries across 12 xdist workers) and the cut waited until it was cancelled at 08:08;
--timeout=600 did not end it. The one runner now watches every suite run: output that
grows or a basetemp entry that changes is progress, and a run with neither for the stall
budget is ended with its whole process tree and fails its step, naming the stall and the
basetemp entries touched last. The real-process tests run under a small, monkeypatched
budget with a fake ``pytest`` module on PYTHONPATH as the suite; none of them runs one.
"""

from __future__ import annotations

import ctypes
import importlib.util
import os
import re
import signal
import sys
import time
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any

import pytest

# Imported, not only loaded: the validation worktree materializes the import closure, and
# the script's by-path load of the job layouts is invisible to that closure.
from aiworkhub.windows_job_structures import JOBOBJECT_EXTENDED_LIMIT_INFORMATION


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "release_cut.py"
SPEC = importlib.util.spec_from_file_location("aiworkhub_release_cut_nf02312", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
release_cut = importlib.util.module_from_spec(SPEC)
# Its dataclasses resolve their string annotations through sys.modules.
sys.modules[SPEC.name] = release_cut
SPEC.loader.exec_module(release_cut)

windows_only = pytest.mark.skipif(os.name != "nt", reason="a Windows Job Object")
posix_only = pytest.mark.skipif(os.name == "nt", reason="a POSIX process group")

VERSION = "0.13.0"
NODE = "tests/test_x.py::test_y"
JOB = 71
CHILD_PID = 4242
CHILD_HANDLE = 0x1F4
KILL_ON_JOB_CLOSE = 0x00002000
LAST_ERROR = 5
POLL_SECONDS = 0.1
# Long enough for a loaded host to start two interpreters before the fake goes quiet.
HANG_BUDGET_SECONDS = 3.0
TOUCH_BUDGET_SECONDS = 2.0
# Ten heartbeats of a worker that still runs.
QUIET_SECONDS = 0.5

# ``python -m pytest`` finds this module first on PYTHONPATH. "hang" starts a worker that
# beats into a file, says so and goes quiet; "touch" adds one basetemp entry per tick and
# prints nothing more until its summary. Every sleep is bounded, so even a failed test
# cannot leave a process behind for long.
_FAKE_PYTEST = """\
import os
import subprocess
import sys
import time
from pathlib import Path

work = Path(os.environ["FAKE_PYTEST_WORK"])
basetemp = Path(next(a for a in sys.argv if a.startswith("--basetemp=")).split("=", 1)[1])
sys.stdout.buffer.write(b"collecting\\n")
sys.stdout.flush()
if os.environ["FAKE_PYTEST_MODE"] == "hang":
    beat = work / "heartbeat"
    worker = subprocess.Popen([sys.executable, str(work / "worker.py"), str(beat)])
    (work / "worker.pid").write_text(str(worker.pid), encoding="ascii")
    deadline = time.monotonic() + 60
    while not beat.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    sys.stdout.buffer.write(b"ready\\n")
    sys.stdout.flush()
    time.sleep(60)
else:
    ticks = int(os.environ["FAKE_PYTEST_TICKS"])
    for tick in range(ticks):
        (basetemp / "popen-gw0" / f"test_tick{tick}").mkdir(parents=True)
        time.sleep(float(os.environ["FAKE_PYTEST_TICK_SECONDS"]))
    sys.stdout.buffer.write(f"{ticks} passed in 1.00s\\n".encode())
    sys.exit(3)
"""

_WORKER = """\
import sys
import time

with open(sys.argv[1], "ab", buffering=0) as beat:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        beat.write(b".")
        time.sleep(0.05)
"""


def _cut(tmp_path: Path, mode: str, **env: str) -> tuple[Any, Path]:
    """A cut whose host suite is the fake pytest under the real runner, and its work dir."""

    root, work, fake = tmp_path / "repo", tmp_path / "work", tmp_path / "fake"
    for folder in (root, work, fake):
        folder.mkdir()
    (fake / "pytest.py").write_text(_FAKE_PYTEST, encoding="utf-8")
    (work / "worker.py").write_text(_WORKER, encoding="utf-8")
    host = release_cut.Host(
        root=root,
        runner=release_cut.run_subprocess,
        python=sys.executable,
        cpu_count=1,
        env={
            **os.environ,
            "PYTHONPATH": str(fake),
            "FAKE_PYTEST_MODE": mode,
            "FAKE_PYTEST_WORK": str(work),
            **env,
        },
        is_windows=os.name == "nt",
        out=lambda _line: None,
    )
    options = release_cut.Options(version=VERSION, full_suite=True)
    return release_cut.ReleaseCut(options, host), work


def _budget(monkeypatch: pytest.MonkeyPatch, seconds: float) -> None:
    monkeypatch.setattr(release_cut, "STALL_BUDGET_SECONDS", seconds)
    monkeypatch.setattr(release_cut, "STALL_POLL_SECONDS", POLL_SECONDS)


def _beats(work: Path) -> int:
    beat = work / "heartbeat"
    return len(beat.read_bytes()) if beat.exists() else 0


def _surviving_worker(work: Path) -> int | None:
    """The worker's pid, ended here, when its heartbeat still advances; else None."""

    before = _beats(work)
    time.sleep(QUIET_SECONDS)
    if _beats(work) == before:
        return None
    pid = int((work / "worker.pid").read_text(encoding="ascii"))
    os.kill(pid, signal.SIGTERM if os.name == "nt" else signal.SIGKILL)
    return pid


class _Export:
    """One fake kernel32 export: records each call and answers ``result``."""

    def __init__(self, result: Any) -> None:
        self.result = result
        self.calls: list[tuple[Any, ...]] = []
        self.argtypes: Any = None
        self.restype: Any = None

    def __call__(self, *args: Any) -> Any:
        self.calls.append(args)
        return self.result


class _Child:
    """What _ProcessTree uses of a suite run's Popen."""

    def __init__(self) -> None:
        self.pid = CHILD_PID
        self._handle = CHILD_HANDLE
        self.kills = 0

    def kill(self) -> None:
        self.kills += 1


def _recording_kernel32(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> SimpleNamespace:
    """Replace ctypes.windll with exports that all succeed unless overridden."""

    results = {
        "CreateJobObjectW": JOB,
        "SetInformationJobObject": 1,
        "GetCurrentProcess": 2**64 - 1,
        "AssignProcessToJobObject": 1,
        "CloseHandle": 1,
        "GetLastError": LAST_ERROR,
        **overrides,
    }
    kernel32 = SimpleNamespace(**{name: _Export(result) for name, result in results.items()})
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(kernel32=kernel32), raising=False)
    return kernel32


def test_the_stall_budget_is_twice_the_per_test_limit_and_looks_come_seconds_apart() -> None:
    assert release_cut.STALL_BUDGET_SECONDS == 2 * release_cut.PYTEST_TIMEOUT_SECONDS
    assert 1 <= release_cut.STALL_POLL_SECONDS <= release_cut.STALL_BUDGET_SECONDS / 10


def test_the_runner_watches_every_suite_run_and_no_other_command(tmp_path: Path) -> None:
    seen: list[list[str]] = []

    def recording(argv: Any, *, cwd: Path, env: Any = None, timeout: Any = None) -> Any:
        seen.append(list(argv))
        return release_cut.Completed(0)

    host = release_cut.Host(
        root=tmp_path,
        runner=recording,
        python=sys.executable,
        cpu_count=8,
        env={},
        is_windows=True,
        out=lambda _line: None,
    )
    cut = release_cut.ReleaseCut(release_cut.Options(version=VERSION), host)
    tree = PurePosixPath(f"/mnt/c/repo/.scratch/release/{VERSION}/tree")

    cut._run(cut._host_test_argv())
    cut._run(cut._host_test_argv([NODE]))
    cut._wsl_in(tree, cut._wsl_test_command())
    cut._wsl_in(tree, cut._wsl_test_command([NODE]))

    assert [release_cut._pytest_basetemp(argv) for argv in seen] == [
        str(cut.scratch / "b"),
        str(cut.scratch / "br"),
        str(cut.wsl_basetemp),
        str(cut.wsl_basetemp),
    ]

    seen.clear()
    cut._git("status", "--porcelain=v1", "-z", "--untracked-files=all")
    cut._run(cut._package_argv())
    cut._run(cut._receipt_argv())
    cut._wsl("-e", "wslpath", "-a", str(cut.scratch))
    cut._wsl("--cd", str(tmp_path), "-e", "python3", "-c", release_cut.WSL_IMPORT_PROBE)
    cut._wsl_in(tree, ("rm", "-rf", "--", str(tree), str(cut.wsl_basetemp)))
    for argv in release_cut.WSL_TREE_GIT_COMMANDS:
        cut._wsl_in(tree, argv)

    assert len(seen) == 6 + len(release_cut.WSL_TREE_GIT_COMMANDS)
    assert [release_cut._pytest_basetemp(argv) for argv in seen] == [None] * len(seen)


def test_a_silent_suite_run_is_ended_with_its_tree_and_fails_naming_the_tests_still_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _budget(monkeypatch, HANG_BUDGET_SECONDS)
    cut, work = _cut(tmp_path, "hang")
    # What workers that sit in their current tests leave: each worker's newest directory
    # names its running test, and an older one names a test that already finished.
    for relative in ("popen-gw0/test_finished0", "popen-gw0/test_stuck1", "popen-gw1/test_slow0"):
        (cut.scratch / "b" / relative).mkdir(parents=True)
        time.sleep(0.05)

    try:
        result = cut._run_step("host_tests")
    finally:
        survivor = _surviving_worker(work)

    assert result.status == "failed"
    match = re.fullmatch(
        r"pytest exited -1: stalled \d+ s \(no new output, no basetemp change\), process "
        r"tree ended; last touched: (?P<touched>.+) "
        rf"\(log \.scratch/release/{re.escape(VERSION)}/host_tests\.log\)",
        result.detail,
    )
    assert match, result.detail
    # Each worker's newest directory, never the test that already finished.
    touched = sorted(match["touched"].split(", "))
    assert touched == ["popen-gw0/test_stuck1", "popen-gw1/test_slow0"]
    log = (cut.scratch / "host_tests.log").read_text(encoding="utf-8")
    assert log.startswith("collecting\nready\n"), log
    # The worker beat before the stall and nothing beats after it: the kill reached it.
    assert _beats(work) > 0
    assert survivor is None


def test_a_run_that_keeps_touching_its_basetemp_past_the_budget_keeps_its_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _budget(monkeypatch, TOUCH_BUDGET_SECONDS)
    ticks, tick_seconds = 16, 0.25  # 4 s without output, twice the budget
    cut, _work = _cut(
        tmp_path,
        "touch",
        FAKE_PYTEST_TICKS=str(ticks),
        FAKE_PYTEST_TICK_SECONDS=str(tick_seconds),
    )

    started = time.monotonic()
    result = cut._run_step("host_tests")

    assert time.monotonic() - started > TOUCH_BUDGET_SECONDS
    assert result.status == "failed"
    assert result.detail == (
        f"pytest exited 3: {ticks} passed in 1.00s "
        f"(log .scratch/release/{VERSION}/host_tests.log)"
    )
    log = (cut.scratch / "host_tests.log").read_text(encoding="utf-8")
    assert log == f"collecting\n{ticks} passed in 1.00s\n"
    assert len(list((cut.scratch / "b" / "popen-gw0").iterdir())) == ticks


def test_a_watched_run_that_ends_by_itself_answers_exactly_what_the_plain_runner_does(
    tmp_path: Path,
) -> None:
    code = (
        "import sys; sys.stdout.buffer.write(b'a\\x00b\\r\\nc'); "
        "sys.stderr.buffer.write(b'warning\\n'); sys.exit(5)"
    )

    plain = release_cut.run_subprocess([sys.executable, "-c", code], cwd=tmp_path)
    watched = release_cut.run_subprocess(
        [sys.executable, "-c", code, f"--basetemp={tmp_path / 'b'}"], cwd=tmp_path
    )

    assert plain == release_cut.Completed(5, "a\x00b\r\nc", "warning\n")
    assert watched == plain


def test_a_watched_run_past_its_timeout_answers_what_the_plain_runner_does(
    tmp_path: Path,
) -> None:
    code = "import time; time.sleep(60)"

    plain = release_cut.run_subprocess([sys.executable, "-c", code], cwd=tmp_path, timeout=0.5)
    watched = release_cut.run_subprocess(
        [sys.executable, "-c", code, f"--basetemp={tmp_path / 'b'}"], cwd=tmp_path, timeout=0.5
    )

    assert watched == plain == release_cut.Completed(-1, "", "\ntimed out after 0.5 s")


def test_a_wsl_suite_run_is_judged_by_its_output_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 2 s: room for a fresh interpreter's start under the cut's own xdist load.
    _budget(monkeypatch, 2.0)
    linux = f"/tmp/aiworkhub-release-{VERSION}"

    def run(code: str) -> Any:
        return release_cut._run_watched(
            [sys.executable, "-c", code],
            cwd=tmp_path,
            env=None,
            timeout=None,
            wsl=True,
            basetemp=linux,
        )

    # 6 s, three budgets, kept alive by nothing but its output.
    printing = run(
        "import time\nfor _ in range(30):\n    print('.', flush=True)\n    time.sleep(0.2)"
    )
    silent = run("import time; time.sleep(60)")

    assert printing.returncode == 0
    assert printing.stdout.count(".") == 30
    assert silent.returncode == -1
    assert silent.stdout == ""
    assert re.fullmatch(
        rf"\nstalled \d+ s \(no new output; the WSL basetemp {re.escape(linux)} is not "
        r"visible from this host\), process tree ended",
        silent.stderr,
    ), silent.stderr


def test_one_look_stays_within_its_bound_and_an_absent_basetemp_adds_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for index in range(5):
        (tmp_path / "b" / f"popen-gw{index}" / "test_x0").mkdir(parents=True)
    monkeypatch.setattr(release_cut, "STALL_SCAN_MAX_ENTRIES", 4)

    newest, touched = release_cut._basetemp_scan(tmp_path / "b")

    assert newest is not None
    assert len(touched) == 4
    assert release_cut._basetemp_scan(tmp_path / "absent") == (None, [])


@windows_only
def test_a_suite_run_joins_a_kill_on_close_job_of_its_own_that_ending_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel32 = _recording_kernel32(monkeypatch)
    child = _Child()

    tree = release_cut._ProcessTree(child)

    handle, info_class, buffer, size = kernel32.SetInformationJobObject.calls[0]
    assert (handle, info_class) == (JOB, 9)
    assert size == ctypes.sizeof(JOBOBJECT_EXTENDED_LIMIT_INFORMATION)
    assert buffer._obj.BasicLimitInformation.LimitFlags == KILL_ON_JOB_CLOSE
    # The run's own child joins this job, never the cut.
    assert kernel32.AssignProcessToJobObject.calls == [(JOB, CHILD_HANDLE)]
    assert kernel32.GetCurrentProcess.calls == []
    assert kernel32.CloseHandle.calls == []
    assert tree.reach == "process tree ended"

    tree.end()

    assert kernel32.CloseHandle.calls == [(JOB,)]
    assert child.kills == 1
    tree.end()
    assert kernel32.CloseHandle.calls == [(JOB,)]


@windows_only
def test_a_suite_run_whose_bind_fails_names_it_and_still_ends_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel32 = _recording_kernel32(monkeypatch, AssignProcessToJobObject=0)
    child = _Child()

    tree = release_cut._ProcessTree(child)
    tree.end()

    # Closed once, by the failed bind itself.
    assert kernel32.CloseHandle.calls == [(JOB,)]
    assert child.kills == 1
    assert tree.reach == (
        f"process ended, not its unbound tree (assign failed (GetLastError {LAST_ERROR}))"
    )


@posix_only
def test_ending_a_suite_run_kills_the_process_group_its_child_leads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signalled: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "killpg", lambda group, number: signalled.append((group, number)))
    child = _Child()

    release_cut._ProcessTree(child).end()

    assert signalled == [(CHILD_PID, signal.SIGKILL)]
    assert child.kills == 1
