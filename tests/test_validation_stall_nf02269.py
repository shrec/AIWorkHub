"""NF-2026-02269: a silent, idle validation command ends validation_hung; progress never does."""

from __future__ import annotations

import json
import subprocess
import sys
import time

import pytest

from aiworkhub import core, mcp_summary_folds, validation_stall, windows_private_session


def _python(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return validation_stall.run_watched(
        argv,
        text=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
        check=False,
        shell=False,
    )


@pytest.fixture
def short_stall(monkeypatch: pytest.MonkeyPatch) -> float:
    monkeypatch.setenv(validation_stall.STALL_SECONDS_ENV, "1.5")
    return 1.5


def test_stall_window_comes_from_env_or_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(validation_stall.STALL_SECONDS_ENV, raising=False)
    assert validation_stall.stall_seconds() == validation_stall.STALL_SECONDS
    for bad in ("", "abc", "0", "-3"):
        monkeypatch.setenv(validation_stall.STALL_SECONDS_ENV, bad)
        assert validation_stall.stall_seconds() == validation_stall.STALL_SECONDS
    monkeypatch.setenv(validation_stall.STALL_SECONDS_ENV, "2.5")
    assert validation_stall.stall_seconds() == 2.5


def test_watchdog_window_restarts_on_every_progress_advance() -> None:
    now = [0.0]
    cpu: list[float | None] = [None]
    dog = validation_stall.StallWatchdog(10, cpu_probe=lambda: cpu[0], clock=lambda: now[0])
    now[0] = 9.0
    assert dog.check() is False
    dog.note_output(5)
    now[0] = 15.0
    assert dog.check() is False  # bytes advanced at t=15: window restarts
    now[0] = 24.0
    cpu[0] = 1.0
    assert dog.check() is False  # first CPU sample is progress
    now[0] = 33.0
    cpu[0] = 2.0
    assert dog.check() is False  # CPU advanced
    now[0] = 42.9
    assert dog.check() is False
    now[0] = 43.0
    assert dog.check() is True  # neither advanced for 10 s


def test_silent_idle_command_is_killed_as_hung(short_stall: float) -> None:
    started = time.monotonic()
    with pytest.raises(validation_stall.ValidationStalled) as caught:
        _run(_python("import sys, time; print('waiting on lock', flush=True); time.sleep(120)"))
    elapsed = time.monotonic() - started
    assert elapsed < 30, elapsed
    assert caught.value.stall_seconds == short_stall
    assert "waiting on lock" in str(caught.value.output)
    assert isinstance(caught.value, subprocess.TimeoutExpired)
    assert str(caught.value).startswith("validation_hung")
    assert validation_stall.hung_reason("flutter test", short_stall) == "validation_hung:flutter test:1.5"


def test_command_that_keeps_printing_past_the_window_is_not_killed(short_stall: float) -> None:
    code = (
        "import time\n"
        "for i in range(25):\n"
        "    print('tick', i, flush=True)\n"
        "    time.sleep(0.2)\n"
    )
    result = _run(_python(code))
    assert result.returncode == 0
    assert result.stdout.count("tick") == 25


@pytest.mark.skipif(
    validation_stall.process_tree_cpu_seconds(__import__("os").getpid()) is None,
    reason="no dependency-free CPU probe on this platform; output bytes alone decide",
)
def test_silent_command_burning_cpu_past_the_window_is_not_killed(short_stall: float) -> None:
    code = (
        "import time\n"
        "end = time.monotonic() + 4.0\n"
        "n = 0\n"
        "while time.monotonic() < end:\n"
        "    n += 1\n"
        "print('done')\n"
    )
    result = _run(_python(code))
    assert result.returncode == 0
    assert result.stdout.strip() == "done"


def test_wall_timeout_still_applies_and_is_not_a_hang() -> None:
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        validation_stall.run_watched(
            _python("import time\nwhile True:\n    print('x', flush=True)\n    time.sleep(0.1)\n"),
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=2,
            check=False, shell=False,
        )
    assert not isinstance(caught.value, validation_stall.ValidationStalled)


def test_substituted_runner_receives_the_identical_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = validation_stall.run_watched(["tool", "--flag"], cwd="x", timeout=5, check=False)
    assert result.stdout == "ok"
    assert seen == [(["tool", "--flag"], {"cwd": "x", "timeout": 5, "check": False})]
    with pytest.raises(TypeError, match="run_watched_unsupported_arguments:preexec_fn"):
        validation_stall.run_watched(["tool"], preexec_fn=None)


def _hung_row() -> dict[str, object]:
    return {
        "command": "flutter test",
        "returncode": None,
        "timed_out": True,
        "stalled": True,
        "stall_seconds": 600.0,
        "failure_reason": "validation_hung:flutter test:600",
        "stdout_tail": "Waiting for another flutter command to release the startup lock...",
        "stderr_tail": "",
    }


def test_task_status_first_failure_names_the_failed_command_from_terminal_evidence() -> None:
    passed = {"command": "flutter analyze", "returncode": 0, "stdout_tail": "No issues"}
    card = {
        "terminal_failure": {
            "category": "validation_failed",
            "reason": "validation_hung:flutter test:600:stdout=...",
            "evidence": {"request_id": "r1", "validation": [passed, _hung_row()]},
        }
    }
    first = mcp_summary_folds.first_failure_from_status({}, card)
    assert first is not None
    assert first["command"] == "flutter test"
    assert first["index"] == 1
    assert first["failure_class"] == "validation_hung"
    assert first["reason"] == "validation_hung:flutter test:600"
    assert "startup lock" in first["tail"]

    plain_failure = {"command": "pytest -q", "returncode": 1, "stderr_tail": "E assert 1 == 2"}
    card["terminal_failure"]["evidence"]["validation"] = [plain_failure]
    first = mcp_summary_folds.first_failure_from_status({"validation": []}, card)
    assert first is not None and first["command"] == "pytest -q" and first["exit_code"] == 1


def test_evidence_fold_reports_the_hung_reason() -> None:
    folded = core._fold_evidence("validation", [_hung_row()])
    assert folded["verdict"] == "failed"
    assert folded["first_failure"]["command"] == "flutter test"
    assert folded["first_failure"]["error"].startswith("validation_hung:")


def test_evidence_fold_regression_for_rows_without_a_failure_reason() -> None:
    """The failure_reason preference must not change the folded text of ordinary rows."""

    passed = {"command": "ruff check", "returncode": 0}
    assert core._fold_evidence("validation", [passed])["verdict"] == "passed"
    assert core._fold_evidence("validation", [])["verdict"] == "empty"
    with_error = {"command": "pytest -q", "returncode": 1, "error": "boom", "stderr_tail": "E x"}
    folded = core._fold_evidence("validation", [passed, with_error])
    assert folded["failed_count"] == 1 and folded["command_count"] == 2
    assert folded["first_failure"] == {"command": "pytest -q", "rc": 1, "error": "boom"}
    stderr_only = {"command": "pytest -q", "returncode": 2, "stderr_tail": "E assert 1 == 2"}
    folded = core._fold_evidence("validation", [stderr_only])
    assert folded["first_failure"]["error"] == "E assert 1 == 2"
    timed_out = {"command": "slow", "returncode": None, "timed_out": True, "stdout_tail": "tick"}
    folded = core._fold_evidence("validation", [timed_out])
    assert folded["first_failure"]["error"] == "tick"


def test_worker_workspace_script_mode_binds_the_stall_names(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib.util
    from pathlib import Path

    source_dir = Path(validation_stall.__file__).resolve().parent
    monkeypatch.syspath_prepend(str(source_dir))
    monkeypatch.delitem(sys.modules, "validation_stall", raising=False)
    name = "worker_workspace_script_mode_nf02269"
    spec = importlib.util.spec_from_file_location(name, source_dir / "worker_workspace.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    assert not module.__package__
    for bound in ("run_watched", "StallWatchdog", "wait_appcontainer_watched",
                  "annotate_stalled", "hung_reason_detail"):
        assert callable(getattr(module, bound)), bound


class _IdleLaunch:
    pid = None

    def __init__(self, dog: validation_stall.StallWatchdog) -> None:
        self.dog = dog
        self.first_check: bool | None = None
        self.terminated = False

    def wait(self, timeout_ms: int) -> int:
        self.first_check = self.dog.check()  # the first poll, before any progress
        return 0

    def terminate(self) -> None:
        self.terminated = True


def test_appcontainer_setup_time_does_not_count_as_no_progress() -> None:
    now = [0.0]
    dog = validation_stall.StallWatchdog(10, cpu_probe=lambda: None, clock=lambda: now[0])
    now[0] = 50.0  # constructed, then setup idles far past the window
    launch = _IdleLaunch(dog)
    outcome = validation_stall.wait_appcontainer_watched(
        launch, 1_000, [], {"stdout": b"", "stderr": b""}, ["flutter", "test"],
        watchdog=dog, error_type=OSError, join_seconds=0.1,
    )
    assert outcome == 0
    assert launch.first_check is False
    assert dog.fired is False and launch.terminated is False
    now[0] = 61.0  # the window still fires once the command itself is idle
    assert dog.check() is True


class _RecordingPlatformIO:
    def __init__(self, windows: bool) -> None:
        self.windows = windows
        self.group_kills: list[tuple[int, bool]] = []

    def is_windows(self) -> bool:
        return self.windows

    def is_linux(self) -> bool:
        return not self.windows

    def signal_process_group(self, pid: int, *, graceful: bool) -> None:
        self.group_kills.append((pid, graceful))


class _TreeProc:
    def __init__(self, pid: int, returncode: int | None) -> None:
        self.pid = pid
        self.returncode = returncode
        self.root_killed = False

    def kill(self) -> None:
        self.root_killed = True


def _patch_tree(
    monkeypatch: pytest.MonkeyPatch, windows: bool
) -> tuple[_RecordingPlatformIO, list[int], list[int]]:
    root, same, reused, gone = 100, 201, 202, 203
    listed = [(root, 1), (same, 10), (reused, 20), (gone, 30)]
    current = {root: 1, same: 10, reused: 99, gone: None}
    killed: list[int] = []
    listed_for: list[int] = []
    recorder = _RecordingPlatformIO(windows)

    def _identities(pid: int) -> list[tuple[int, int]]:
        listed_for.append(pid)
        return list(listed)

    monkeypatch.setattr(validation_stall, "platform_io", recorder)
    monkeypatch.setattr(validation_stall, "_tree_identities", _identities)
    monkeypatch.setattr(validation_stall, "_creation_stamp", lambda pid: current[pid])
    monkeypatch.setattr(validation_stall, "_terminate_pid", lambda pid, stamp: killed.append(pid))
    return recorder, killed, listed_for


@pytest.mark.parametrize("windows", [False, True], ids=["posix", "windows"])
def test_kill_tree_skips_a_descendant_whose_creation_time_changed(
    monkeypatch: pytest.MonkeyPatch, windows: bool
) -> None:
    recorder, killed, listed_for = _patch_tree(monkeypatch, windows)
    proc = _TreeProc(100, returncode=None)
    validation_stall.kill_process_tree(proc)  # type: ignore[arg-type]
    assert listed_for == [100]
    assert killed == [201]  # 202 has a stale stamp (reused pid), 203 vanished
    assert 202 not in killed and 203 not in killed
    assert recorder.group_kills == ([] if windows else [(100, False)])
    assert proc.root_killed is True


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only branch: a reaped root's pid may be reused")
def test_kill_tree_does_not_list_descendants_of_a_reaped_posix_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _recorder, killed, listed_for = _patch_tree(monkeypatch, windows=False)
    proc = _TreeProc(100, returncode=0)
    validation_stall.kill_process_tree(proc)  # type: ignore[arg-type]
    assert listed_for == []
    assert killed == []
    assert proc.root_killed is True


def test_cpu_drop_after_a_heavy_child_exits_is_a_new_baseline_not_a_stall() -> None:
    now = [0.0]
    cpu = [10.0]
    dog = validation_stall.StallWatchdog(10, cpu_probe=lambda: cpu[0], clock=lambda: now[0])
    assert dog.check() is False  # first sample: 10 s of CPU
    for t, value in ((5.0, 2.0), (9.0, 3.0), (18.0, 4.0), (27.0, 5.0), (36.0, 6.0)):
        now[0], cpu[0] = t, value  # the heavy child exited (10 -> 2); a new one keeps working
        assert dog.check() is False, (t, value)
    now[0] = 45.9  # CPU now flat at 6.0 and no output bytes
    assert dog.check() is False
    now[0] = 46.0
    assert dog.check() is True


def test_script_mode_platform_io_ignores_a_decoy_package_on_sys_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    import importlib.util
    from pathlib import Path

    decoy = Path(str(tmp_path)) / "aiworkhub"
    decoy.mkdir()
    (decoy / "__init__.py").write_text("", encoding="utf-8")
    (decoy / "platform_io.py").write_text("DECOY = True\n", encoding="utf-8")
    source = Path(validation_stall.__file__).resolve()
    # Script mode runs with the module's own directory on sys.path (that is how
    # platform_io finds its private siblings); the decoy package still comes first.
    monkeypatch.syspath_prepend(str(source.parent))
    monkeypatch.syspath_prepend(str(decoy.parent))
    monkeypatch.delitem(sys.modules, "aiworkhub.platform_io", raising=False)
    monkeypatch.delitem(sys.modules, "aiworkhub", raising=False)
    monkeypatch.delitem(sys.modules, "platform_io", raising=False)
    before = set(sys.modules)
    name = "validation_stall_script_mode_nf02269"
    spec = importlib.util.spec_from_file_location(name, source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    try:
        spec.loader.exec_module(module)
        assert not module.__package__
        resolved = module.platform_io._resolve()
        assert not hasattr(resolved, "DECOY")
        assert Path(resolved.__file__).resolve() == source.with_name("platform_io.py")
        assert callable(module.platform_io.is_windows)
        assert module.platform_io._resolve() is resolved  # cached: resolved once
    finally:
        for added in set(sys.modules) - before - {name}:
            sys.modules.pop(added, None)


def test_private_session_relay_keeps_a_hang_a_hang() -> None:
    record = {
        "schema_id": windows_private_session._VALIDATION_RELAY_SCHEMA,
        "kind": "stalled",
        "args": ["flutter", "test"],
        "stall_seconds": 600.0,
        "stdout": "lock",
        "stderr": "",
    }
    raw = json.dumps(record).encode("utf-8")
    with pytest.raises(validation_stall.ValidationStalled) as caught:
        windows_private_session._relayed_validation_result(0, (raw, False), b"")
    assert caught.value.stall_seconds == 600.0
    assert caught.value.output == "lock"
