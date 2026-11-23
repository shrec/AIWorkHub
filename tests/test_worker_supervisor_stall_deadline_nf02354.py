"""NF-2026-02354: the supervisor deadline is stall-gated, not a wall kill.

Past timeout_seconds a child is terminated as timed_out only after
stall_window_seconds with no output growth, meaningful progress or usage
change. spec hard_wall_deadline=True keeps the exact old wall kill.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import worker_supervisor  # noqa: E402
from aiworkhub.worker_workspace import write_json_0600  # noqa: E402

_REAL_SLEEP = time.sleep
_REAL_MONOTONIC = time.monotonic
_HANG_GUARD_SECONDS = 60
_TIMEOUT_SECONDS = 2
_STALL_WINDOW_SECONDS = 60


class _FakeClock:
    def __init__(self) -> None:
        self.epoch = 1_700_000_000.0
        self.mono = 1_000.0

    def time(self) -> float:
        return self.epoch

    def monotonic(self) -> float:
        return self.mono

    def sleep(self, seconds: float) -> None:
        delta = float(seconds)
        self.epoch += delta
        self.mono += delta


def _install_fake_clock(
    monkeypatch: pytest.MonkeyPatch, clock: _FakeClock
) -> None:
    monkeypatch.setattr(worker_supervisor.time, "time", clock.time)
    monkeypatch.setattr(worker_supervisor.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(worker_supervisor.time, "sleep", clock.sleep)


def _spec(tmp_path: Path, argv: list[str], timeout: int = 10) -> tuple[Path, dict]:
    process_dir = tmp_path / "processes"
    process_dir.mkdir(mode=0o700)
    spec_path = process_dir / "request.spec.json"
    payload = {
        "argv": argv,
        "cwd": str(tmp_path),
        "timeout_seconds": timeout,
        "status_path": str(process_dir / "status.json"),
        "cancel_path": str(process_dir / "cancel.json"),
        "stdout_path": str(process_dir / "stdout.log"),
        "stderr_path": str(process_dir / "stderr.log"),
    }
    write_json_0600(spec_path, payload)
    return spec_path, payload


def _read_status(spec: dict) -> dict:
    return json.loads(Path(spec["status_path"]).read_text(encoding="utf-8"))


def _assert_deadline_unchanged(status: dict) -> None:
    assert status["deadline_epoch"] == status["started_at_epoch"] + _TIMEOUT_SECONDS
    assert status["timeout_enforced"] is True
    assert status["timeout_seconds"] == _TIMEOUT_SECONDS


def _wrap_terminate(
    monkeypatch: pytest.MonkeyPatch, clock: _FakeClock
) -> dict:
    calls: dict = {"n": 0, "mono": None, "epoch": None}
    original_terminate = worker_supervisor._terminate_child

    def wrapped_terminate(child):
        calls["n"] += 1
        calls["mono"] = clock.mono
        calls["epoch"] = clock.epoch
        return original_terminate(child)

    monkeypatch.setattr(worker_supervisor, "_terminate_child", wrapped_terminate)
    return calls


def _silent_child_spec(tmp_path: Path, go_file: Path | None = None) -> dict:
    """A child that prints one line, then stays silent.

    With ``go_file`` the child prints only once that file exists, so the test
    decides when the line may appear relative to the fake clock.
    """
    gate = ""
    if go_file is not None:
        gate = (
            "import pathlib, time\n"
            f"go = pathlib.Path({str(go_file)!r})\n"
            "while not go.exists():\n"
            "    time.sleep(0.005)\n"
        )
    script = gate + "print('only-line', flush=True)\nimport time\ntime.sleep(600)\n"
    _, spec = _spec(tmp_path, [sys.executable, "-c", script], timeout=_TIMEOUT_SECONDS)
    spec["heartbeat_interval_seconds"] = 0.05
    spec["stall_window_seconds"] = _STALL_WINDOW_SECONDS
    return spec


def _wait_for_output(path: Path) -> None:
    guard = _REAL_MONOTONIC() + _HANG_GUARD_SECONDS
    while not (path.exists() and path.stat().st_size > 0):
        if _REAL_MONOTONIC() >= guard:
            raise AssertionError("child never produced output")
        _REAL_SLEEP(0.01)


def test_growing_output_past_deadline_is_not_terminated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(a) REPRODUCTION: a live child is never killed by the wall clock."""
    clock = _FakeClock()
    _install_fake_clock(monkeypatch, clock)
    stop_marker = tmp_path / "stop.marker"
    script = (
        "import pathlib, time\n"
        f"marker = pathlib.Path({str(stop_marker)!r})\n"
        "while not marker.exists():\n"
        "    print('still-working', flush=True)\n"
        "    time.sleep(0.002)\n"
    )
    _, spec = _spec(tmp_path, [sys.executable, "-c", script], timeout=_TIMEOUT_SECONDS)
    spec["heartbeat_interval_seconds"] = 0.05
    spec["stall_window_seconds"] = _STALL_WINDOW_SECONDS
    stdout_path = Path(spec["stdout_path"])
    started_mono = clock.mono
    # Run well past deadline + stall window while the output keeps growing.
    stop_after = _TIMEOUT_SECONDS + 3 * _STALL_WINDOW_SECONDS
    calls = _wrap_terminate(monkeypatch, clock)

    def fake_sleep(seconds: float) -> None:
        if clock.mono - started_mono >= stop_after:
            if not stop_marker.exists():
                stop_marker.write_text("stop", encoding="utf-8")
            _REAL_SLEEP(0.01)
            return
        _wait_for_output(stdout_path)
        before = stdout_path.stat().st_size
        guard = _REAL_MONOTONIC() + _HANG_GUARD_SECONDS
        # Advance fake time only once the child has demonstrably produced
        # more output, so every fake second carries real output growth.
        while stdout_path.stat().st_size == before:
            if _REAL_MONOTONIC() >= guard:
                raise AssertionError("child output stopped growing")
            _REAL_SLEEP(0.005)
        clock.sleep(1.0)

    monkeypatch.setattr(worker_supervisor.time, "sleep", fake_sleep)

    code = worker_supervisor.supervise(spec)

    assert code == 0
    assert calls["n"] == 0
    assert clock.mono - started_mono >= stop_after
    status = _read_status(spec)
    assert status["state"] == "exited"
    assert status["exit_code"] == 0
    assert status["timeout_policy"] == "stall_gated"
    assert status["stall_window_seconds"] == _STALL_WINDOW_SECONDS
    assert "stall_idle_seconds" not in status
    _assert_deadline_unchanged(status)


def test_silent_child_is_timed_out_after_stall_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(b) idle for stall_window past the deadline -> timed_out."""
    clock = _FakeClock()
    _install_fake_clock(monkeypatch, clock)
    go_file = tmp_path / "child.go"
    spec = _silent_child_spec(tmp_path, go_file)
    stdout_path = Path(spec["stdout_path"])
    started_mono = clock.mono
    calls = _wrap_terminate(monkeypatch, clock)

    def fake_sleep(seconds: float) -> None:
        # Deterministic ordering: the child may print only after fake time
        # has advanced past the start epoch, so its one line can never be
        # stamped at started_at_epoch however the supervisor's polls race it.
        clock.sleep(1.0)
        if not go_file.exists():
            go_file.write_text("go", encoding="utf-8")
        # Freeze fake time until the single line is captured.
        _wait_for_output(stdout_path)
        _REAL_SLEEP(0.001)

    monkeypatch.setattr(worker_supervisor.time, "sleep", fake_sleep)

    code = worker_supervisor.supervise(spec)

    assert code == 124
    assert calls["n"] == 1
    assert calls["mono"] - started_mono >= _TIMEOUT_SECONDS
    status = _read_status(spec)
    assert status["state"] == "timed_out"
    assert status["timeout_policy"] == "stall_gated"
    assert status["stall_window_seconds"] == _STALL_WINDOW_SECONDS
    assert isinstance(status["stall_idle_seconds"], int)
    assert status["stall_idle_seconds"] >= _STALL_WINDOW_SECONDS
    last_activity = max(
        status["last_output_change_epoch"], status["last_meaningful_progress_epoch"]
    )
    assert last_activity > status["started_at_epoch"]
    assert calls["epoch"] - last_activity >= _STALL_WINDOW_SECONDS
    _assert_deadline_unchanged(status)


def test_hard_wall_deadline_keeps_exact_wall_kill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(c) hard_wall_deadline True -> killed at the deadline."""
    clock = _FakeClock()
    _install_fake_clock(monkeypatch, clock)
    spec = _silent_child_spec(tmp_path)
    spec["hard_wall_deadline"] = True
    started_mono = clock.mono
    calls = _wrap_terminate(monkeypatch, clock)

    code = worker_supervisor.supervise(spec)

    assert code == 124
    assert calls["n"] == 1
    delta = calls["mono"] - started_mono
    assert delta >= _TIMEOUT_SECONDS
    assert delta < _TIMEOUT_SECONDS + worker_supervisor.POLL_SECONDS + 1e-9
    status = _read_status(spec)
    assert status["state"] == "timed_out"
    assert status["timeout_policy"] == "hard_wall"
    assert status["stall_window_seconds"] == _STALL_WINDOW_SECONDS
    assert isinstance(status["stall_idle_seconds"], int)
    _assert_deadline_unchanged(status)


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        (None, worker_supervisor.DEFAULT_STALL_WINDOW_SECONDS),
        (0, worker_supervisor.DEFAULT_STALL_WINDOW_SECONDS),
        ("not-a-number", worker_supervisor.DEFAULT_STALL_WINDOW_SECONDS),
        (5, 60),
        (59, 60),
        (600, 600),
        (10**9, 86_400),
    ],
)
def test_stall_window_default_and_clamp(
    tmp_path: Path, configured: object, expected: int
) -> None:
    """(d) default 1800, clamped to [60, 86400]."""
    assert worker_supervisor.DEFAULT_STALL_WINDOW_SECONDS == 1800
    _, spec = _spec(tmp_path, [sys.executable, "-c", "pass"], timeout=_TIMEOUT_SECONDS)
    spec["heartbeat_interval_seconds"] = 0.05
    if configured is not None:
        spec["stall_window_seconds"] = configured

    code = worker_supervisor.supervise(spec)

    assert code == 0
    status = _read_status(spec)
    assert status["state"] == "exited"
    assert status["stall_window_seconds"] == expected
    assert status["timeout_policy"] == "stall_gated"
    _assert_deadline_unchanged(status)
